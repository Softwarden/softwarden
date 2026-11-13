"""The spend reports' data path (reporting performance). money/reporting.load reads columns, not model
instances, and splits with contracts.split. These tests hold it to the algorithm it replaced: on randomised contracts
(fixed and floating shares, fixed shares over and under 100, no lines, Signed, archived, other organizations), every
allocation row, every group of every dimension (labels and counts too), every detail row of the report and its CSV,
and the full per-currency commitments are the ones the model-instance path gave, to the cent. They also keep the path free of model instances and its query count flat."""

import random
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from catalog import services
from catalog.models import Application, Department, Kind, Resource, SoftwareCategory, Vendor
from core.models import Membership
from money import contracts, reporting
from money.models import LIVE_STATUSES, OBLIGATION_STATUSES, Contract, ContractItem, ContractStatus

pytestmark = pytest.mark.django_db

CENT = Decimal("0.01")
HUNDRED = Decimal("100")
ZERO = Decimal("0")


# ---- the model-instance algorithm the reports used before (money/contracts.py and money/reporting.py) ------


def old_split_cents(cost: Decimal, shares: list[Decimal]) -> list[Decimal]:
    exact = [cost * share / HUNDRED for share in shares]
    floors = [(e / CENT).to_integral_value(rounding=ROUND_FLOOR) * CENT for e in exact]
    leftover = int(((cost - sum(floors, ZERO)) / CENT).to_integral_value())
    order = sorted(range(len(shares)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[: max(leftover, 0)]:
        floors[i] += CENT
    return floors


def old_allocations(terms, items):
    for contract in terms:
        cost = (contract.annualized_cost or ZERO).quantize(CENT)
        lines = items.get(contract.id, [])
        if not lines:
            yield contract, None, HUNDRED, cost
            continue
        fixed = [i.allocation_percent for i in lines if i.allocation_percent is not None]
        floating = [i for i in lines if i.allocation_percent is None]
        fixed_total = sum(fixed, ZERO)
        scale = HUNDRED / fixed_total if fixed_total > HUNDRED else Decimal(1)
        remainder = max(HUNDRED - fixed_total, ZERO)
        each = remainder / len(floating) if floating else ZERO
        rows: list[tuple[Any, Decimal]] = [
            (i, i.allocation_percent * scale if i.allocation_percent is not None else each) for i in lines
        ]
        if not floating and remainder > 0:
            rows.append((None, remainder))
        for (item, share), amount in zip(rows, old_split_cents(cost, [s for _i, s in rows]), strict=True):
            yield contract, item, share, amount


def old_load(org):
    """(the obligation terms as model instances, the allocation rows (contract, item, share, amount))."""
    terms = list(
        Contract.objects.filter(
            organization=org, status__in=OBLIGATION_STATUSES, archived_at__isnull=True, annualized_cost__isnull=False
        )
        .select_related("vendor")
        .order_by("name", "id")
    )
    live = [c for c in terms if c.status in LIVE_STATUSES]
    items: dict[Any, list[ContractItem]] = {}
    for item in (
        ContractItem.objects.filter(organization=org, contract_id__in=[c.id for c in live])
        .select_related("resource__department", "resource__application__category")
        .order_by("created_at", "id")
    ):
        items.setdefault(item.contract_id, []).append(item)
    return terms, list(old_allocations(live, items))


GAPS = reporting.GAPS


def old_group(by, contract, item):
    """(key, label, gap), as reporting.group_of did on model instances."""
    resource = item.resource if item is not None else None
    if by == "vendor":
        vendor = contract.vendor
        return (str(vendor.id), vendor.name, None) if vendor else (None, GAPS["no_vendor"], "no_vendor")
    if by == "cost_center":
        text = (resource.cost_center if resource is not None else "") or contract.cost_center
        return (text, text, None) if text else (None, GAPS["no_cost_center"], "no_cost_center")
    if resource is None:
        return (None, GAPS["unallocated"], "unallocated")
    if by == "department":
        department = resource.department
        if department is None:
            return (None, GAPS["no_department"], "no_department")
        return (str(department.id), department.name, None)
    if resource.kind != Kind.APPLICATION:
        return (None, GAPS["not_application"], "not_application")
    application = getattr(resource, "application", None)
    category = application.category if application is not None else None
    if category is None:
        return (None, GAPS["no_category"], "no_category")
    return (str(category.id), category.name, None)


def old_groups(by, rows):
    found: dict[tuple[str, tuple[Any, ...]], dict[str, Any]] = {}
    for contract, item, _share, amount in rows:
        group = old_group(by, contract, item)
        entry = found.setdefault((contract.currency, group), {"amount": ZERO, "contracts": set(), "records": set()})
        entry["amount"] += amount
        entry["contracts"].add(contract.id)
        if item is not None:
            entry["records"].add(item.resource_id)
    out = [
        {
            "key": key,
            "label": label,
            "gap": gap,
            "currency": currency,
            "amount": reporting.money(entry["amount"]),
            "contracts": len(entry["contracts"]),
            "records": len(entry["records"]),
            "_amount": entry["amount"],
        }
        for (currency, (key, label, gap)), entry in found.items()
    ]
    out.sort(key=lambda r: (r["currency"], -r["_amount"], r["gap"] is not None, r["label"].casefold()))
    for r in out:
        del r["_amount"]
    return out


def old_allocation_rows(by, rows):
    out = []
    for contract, item, share, amount in rows:
        key, label, gap = old_group(by, contract, item)
        resource = item.resource if item is not None else None
        out.append(
            {
                "key": key,
                "label": label,
                "gap": gap,
                "currency": contract.currency,
                "amount": reporting.money(amount),
                "percent": str(share.quantize(Decimal("0.01"))),
                "contract": {"id": str(contract.id), "name": contract.name},
                "vendor": {"id": str(contract.vendor.id), "name": contract.vendor.name} if contract.vendor else None,
                "record": (
                    {"id": str(resource.id), "name": resource.name, "kind": resource.kind}
                    if resource is not None
                    else None
                ),
                "cost_center": (resource.cost_center if resource is not None else "") or contract.cost_center,
            }
        )
    out.sort(key=lambda r: (r["gap"] is not None, r["label"].casefold(), r["contract"]["name"].casefold()))
    return out


# ---- randomised books ------------------------------------------------------------------------------------------------


def random_books(org, rng: random.Random, n: int) -> None:
    """Contracts written directly (as older data could be: fixed shares over 100 are refused on every write now)."""
    departments = [Department.objects.create(organization=org, name=f"Dept {i}") for i in range(3)]
    categories = list(SoftwareCategory.objects.filter(organization=org)[:3])
    vendors = [Vendor.objects.create(organization=org, name=f"Vendor {i}") for i in range(4)]
    records = []
    for i in range(10):
        kind = rng.choice([Kind.APPLICATION, Kind.APPLICATION, Kind.COMPONENT, Kind.AI_AGENT])
        r = Resource.objects.create(
            organization=org,
            name=f"Record {i}",
            slug=f"record-{i}",
            kind=kind,
            lifecycle="active",
            department=rng.choice([*departments, None]),
            cost_center=rng.choice(["", "", "CC-R"]),
        )
        if kind == Kind.APPLICATION and i % 5:  # some applications have no extension row (older data)
            Application.objects.create(resource=r, organization=org, category=rng.choice([*categories, None]))
        records.append(r)
    Contract.objects.create(  # a Signed term that started already: future and next_start, whatever the seed
        organization=org,
        name="Signed earlier",
        currency="USD",
        annualized_cost=Decimal("1234.56"),
        status=ContractStatus.SIGNED,
        start_date=date(2025, 3, 1),
    )
    for i in range(n):
        c = Contract.objects.create(
            organization=org,
            name=rng.choice(["Alpha", "Beta", "Gamma"]) + f" {i % 7}",  # ties on name: ordered by id
            vendor=rng.choice([*vendors, None]),
            currency=rng.choice(["USD", "EUR", "GBP"]),
            billing_frequency=rng.choice(["annual", "one_time", "monthly"]),
            annualized_cost=rng.choice([None, Decimal(rng.randint(0, 10_000_000)) / 100, Decimal("0.01")]),
            status=rng.choice([*ContractStatus.values, ContractStatus.ACTIVE, ContractStatus.ACTIVE]),
            start_date=rng.choice([None, date(2025, rng.randint(1, 12), 1), date(2027, rng.randint(1, 12), 1)]),
            cost_center=rng.choice(["", "CC-1", "CC-2"]),
        )
        if rng.random() < 0.1:
            Contract.objects.filter(id=c.id).update(archived_at=c.created_at)
        for r in rng.sample(records, rng.choice([0, 1, 2, 3, 3, 5])):
            percent = rng.choice([None, None, Decimal("33.33"), Decimal("50"), Decimal("60"), Decimal("12.5")])
            ContractItem.objects.create(organization=org, contract=c, resource=r, allocation_percent=percent)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_the_column_path_matches_the_model_path(a, b, seed):
    rng = random.Random(seed)  # noqa: S311 - test data, not secrets
    random_books(a["org"], rng, 60)
    random_books(b["org"], rng, 10)  # another organization's books stay out
    org = a["org"]
    terms, old = old_load(org)
    new = reporting.load(org)
    assert [(c.id, i.resource_id if i else None, s, amt) for c, i, s, amt in old] == [
        (r.term.id, r.line.resource_id if r.line else None, r.share, r.amount) for r in new.allocations
    ]
    # contracts.allocations (models in) gives the same rows as well: one algorithm, two inputs
    seen = list(dict.fromkeys(c for c, *_ in old))
    assert [(r.contract.id, r.item.id if r.item else None, r.amount) for r in contracts.allocations(seen)] == [
        (c.id, i.id if i else None, amt) for c, i, _s, amt in old
    ]
    for by in reporting.DIMENSIONS:
        # every group (key, label, gap, amount, contract and record counts) in the same order, and every detail row
        # (percent, vendor and record names, the cost centre fallback) in the same order: the report and its CSV
        assert reporting.groups(by, new.allocations) == old_groups(by, old), by
        assert reporting.allocation_rows(by, new.allocations) == old_allocation_rows(by, old), by
    # the full commitments (run rate, one-time, future, future one-time, next start, contracts) from model instances
    assert reporting.commitments(terms) == new.commitments
    assert any(t["future"] != "0.00" and t["next_start"] for t in new.commitments)  # Signed terms are exercised


def test_the_spend_path_builds_no_model_instances(a, monkeypatch):
    """Building a model instance per contract and line was most of the dashboard's time at 1,500 contracts."""
    random_books(a["org"], random.Random(9), 20)  # noqa: S311 - test data

    def refuse(*args, **kwargs):
        raise AssertionError("the spend path built a model instance")

    for model in (Contract, ContractItem, Resource, Vendor, Department, Application, SoftwareCategory):
        monkeypatch.setattr(model, "from_db", classmethod(refuse))
    for by in reporting.DIMENSIONS:
        assert reporting.spend_report(a["org"], by)["totals"]


def add_contracts(a, n: int, start: int) -> None:
    org, editor = a["org"], a["users"]["editor"]
    category = SoftwareCategory.objects.filter(organization=org).first()
    for i in range(start, start + n):
        vendor = Vendor.objects.create(organization=org, name=f"V{i}")
        dept = Department.objects.create(organization=org, name=f"D{i}")
        app = services.create_resource(org, editor, {"name": f"App {i}", "kind": "application", "lifecycle": "active"})
        Resource.objects.filter(id=app.id).update(department=dept, cost_center=f"CC{i}")
        Application.objects.filter(resource=app).update(category=category)
        job = services.create_resource(org, editor, {"name": f"Job {i}", "kind": "component", "lifecycle": "active"})
        c = contracts.create_contract(
            org, editor, {"name": f"C{i}", "vendor": str(vendor.id), "amount": "120.00", "currency": "EUR"}
        )
        contracts.add_item(org, editor, c, {"record": str(app.id), "allocation_percent": "60"})
        contracts.add_item(org, editor, c, {"record": str(job.id)})


def test_spend_queries_have_a_ceiling_that_does_not_grow(a):
    """Two queries for the spend data (contracts, then lines with their records), at 3 contracts and at 15, for
    every dimension; and the dashboard's count stays the same too."""
    org = a["org"]
    member = Membership.objects.get(organization=org, user=a["users"]["viewer"])
    today = services.org_today(org)

    def counts() -> tuple[list[int], int]:
        found = []
        for by in reporting.DIMENSIONS:
            with CaptureQueriesContext(connection) as ctx:
                reporting.spend_report(org, by)
            found.append(len(ctx.captured_queries))
        with CaptureQueriesContext(connection) as ctx:
            reporting.dashboard(org, member, today)
        return found, len(ctx.captured_queries)

    add_contracts(a, 3, 0)
    counts()  # the first call also fills per-request caches
    spend, dashboard = counts()
    assert spend == [2] * len(reporting.DIMENSIONS)
    add_contracts(a, 12, 3)
    assert counts() == (spend, dashboard)
    assert dashboard <= 30, dashboard


# ---- the timing command (money/management/commands/report_timing.py) ------------------------------------------------


def test_report_timing_tops_up_and_times(settings):
    import io

    from django.core.management import call_command

    from core.models import Organization
    from money.management.commands.report_timing import percentile

    settings.DEBUG = True  # a development database
    call_command("seed_scale", scale=0.05, stdout=io.StringIO())
    org = Organization.objects.get(name="Scale Test (synthetic)")
    out = io.StringIO()
    call_command("report_timing", contracts=12, samples=2, stdout=out)
    assert Contract.objects.filter(organization=org).count() == 12
    assert ContractItem.objects.filter(organization=org).count() == 24
    assert not Contract.objects.filter(organization=org, amount__isnull=False, annualized_cost__isnull=True).exclude(
        billing_frequency="usage_based"
    )  # derived like the services derive them
    body = out.getvalue()
    assert "12 contracts (12 added), 24 lines" in body and "spend by cost_center" in body and "dashboard" in body
    call_command("report_timing", contracts=12, samples=1, stdout=io.StringIO())
    assert Contract.objects.filter(organization=org).count() == 12  # already there: nothing added
    assert percentile([1.0, 2.0, 3.0, 4.0], 95) == 4.0 and percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0


def test_report_timing_refuses_what_may_not_be_its_synthetic_organization(a, settings):
    import io

    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.core.management.base import CommandError

    from catalog.management.commands.seed_scale import ADMIN_EMAIL, ORG_NAME
    from core.models import Organization, Role

    def run(**kw):
        call_command("report_timing", contracts=2, samples=1, stdout=io.StringIO(), **kw)

    settings.DEBUG = True
    with pytest.raises(CommandError, match="No user admin@scale.example.test"):
        run()
    admin = get_user_model().objects.create_user(username=ADMIN_EMAIL, email=ADMIN_EMAIL, password="x" * 16)
    with pytest.raises(CommandError, match="Run seed_scale first"):
        run()
    # An organization that only shares the name: seed_scale's Admin isn't a member, so nothing is written into it.
    lookalike = Organization.objects.create(name=ORG_NAME)
    with pytest.raises(CommandError, match="doesn't have seed_scale's"):
        run()
    Membership.objects.create(organization=lookalike, user=admin, role=Role.ADMIN)
    Organization.objects.create(name=ORG_NAME)
    with pytest.raises(CommandError, match="More than one organization"):
        run()
    assert not Contract.objects.exists()
    # Production settings (SOFTWARDEN_DEBUG off): refused before anything is read, unless the flag says otherwise.
    settings.DEBUG = False
    Organization.objects.filter(name=ORG_NAME).exclude(id=lookalike.id).update(name="Renamed")
    with pytest.raises(CommandError, match="--synthetic-only-i-understand"):
        run()
    assert not Contract.objects.exists()
    with pytest.raises(CommandError, match="needs records first"):  # past the check; this one has no records
        run(synthetic_only_i_understand=True)
