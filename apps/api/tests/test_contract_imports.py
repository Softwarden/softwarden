"""Imports: contracts and licence pools as import kinds (imports/money_import.py).

The acceptance check: importing an earlier version's demo contracts (tests/fixtures/demo_contracts.csv, built from its
seed_demo contracts with fixed dates plus rows for the billing frequencies and month-end clamps its demo didn't have)
yields the annualised totals and deadlines worked out by hand below from its rules, independently of the code; the spend
report reconciles; a renewal-linked row agrees with its renewal (one source of truth). Also: re-imports, the items
column (allocations, reshuffles, names), the services' refusals as row errors, licence pools (seat sync, last_synced),
templates, exports that re-import unchanged, roles and tenancy.

How the fixture differs from that seed_demo, on purpose:
- dates are fixed (the earlier ones are relative to today: end = today + n, start = end - 364 days);
- notice periods: Atlassian 1 month (was 30 days) and Falcon 2 months (was 45 days), to exercise month-end clamping;
  SAP blank (was 0 days, which this model refuses: a notice period is 1-1000); the rest as before;
- auto-renewal: the earlier one is a boolean, true only on the E5 agreement. Here E5 yes, Atlassian unknown, Veeva and
  Falcon yes, SAP and Anthropic no, so every deadline label appears;
- Veeva's multi-year term is three years (was start = end - 364 days, a one-year span), and is in EUR;
- three rows are added: Okta (quarterly, GBP), Logic Apps (every six months, leap-year clamp), Vault validation
  (one-time, EUR); the earlier InternalSolution "New Hire Onboarding" is a component here, its agents AI agents;
- the Atlassian row creates a renewal and the Falcon row links an existing one (the earlier demo had no renewal links)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.db.models import Sum
from django.db.models.signals import post_save

from catalog import customfields, services
from catalog.models import CustomField, Person, Resource, Vendor
from core.models import AuditEvent
from imports import csvio, money_import
from imports.models import ExternalIdentifier, ImportRow, ImportRun, ImportSource
from money import contracts, renewal_rules, renewals, terms
from money.models import Contract, ContractItem, LicensePool, RenewalCycle, RenewalObligation
from tests.test_imports import rows_of, run_of, upload

pytestmark = [pytest.mark.django_db, pytest.mark.one_source_of_truth]  # tests/conftest.py
D = Decimal
FIXTURE = Path(__file__).parent / "fixtures" / "demo_contracts.csv"

# Worked out by hand from the earlier rules (annualize; the deadline is the end date minus the
# notice period, month-end clamped, when it auto-renews or that is unknown; otherwise the end date minus the
# organization's 90-day lead time):
# name: (currency, annualised, cancellation deadline, deadline label)
EXPECTED = {
    # 52,000 a month x 12. 28 Feb 2027 minus 60 days: 31 Jan is 28 days back, 31 Dec 59, 30 Dec 60.
    "Microsoft 365 E5 EA": ("USD", "624000.00", "2027-02-28", "2026-12-30", "calculated"),
    # Annual. Auto-renewal unknown counts as cancelling: 31 Mar 2027 minus one month is 31 Feb, clamped to 28 Feb.
    "Atlassian Cloud Premium": ("USD", "38000.00", "2027-03-31", "2027-02-28", "calculated"),
    # 180,000 over 1 Jan 2025 - 31 Dec 2027: 1,095 days / 365.25 x 12 = 35.98, rounds to 36 months = 3 years.
    # 31 Dec 2027 minus 90 days: 1 Dec is 30 back, 31 Oct 61, 2 Oct 90.
    "Veeva Vault QMS subscription": ("EUR", "60000.00", "2027-12-31", "2027-10-02", "calculated"),
    # Annual; doesn't auto-renew, so renew 90 days before 31 Dec 2026: 2 Oct 2026.
    "SAP maintenance": ("USD", "95000.00", "2026-12-31", "2026-10-02", "renewal_action"),
    # 4,100 a month x 12. 30 Apr 2027 minus two months is 30 Feb, clamped to 28 Feb.
    "Falcon Enterprise": ("USD", "49200.00", "2027-04-30", "2027-02-28", "calculated"),
    # Usage-based: the amount is the annual estimate. No auto-renewal: 30 Jun 2027 minus 90 days = 1 Apr 2027
    # (31 May is 30 back, 30 Apr 61, 1 Apr 90); the 30-day notice doesn't apply.
    "Anthropic API": ("USD", "18000.00", "2027-06-30", "2027-04-01", "renewal_action"),
    # 7,500 a quarter x 4. 31 Aug 2026 minus six months is 31 Feb, clamped to 28 Feb 2026.
    "Okta Workforce Identity": ("GBP", "30000.00", "2026-08-31", "2026-02-28", "calculated"),
    # 3,000 every six months x 2. 31 May 2028 minus three months is 31 Feb, clamped to 29 Feb (a leap year).
    "Logic Apps consumption": ("USD", "6000.00", "2028-05-31", "2028-02-29", "calculated"),
    # One-time: counted once, in full. No auto-renewal: 15 Dec 2026 minus 90 days = 16 Sep 2026.
    "Vault validation package": ("EUR", "25000.00", "2026-12-15", "2026-09-16", "renewal_action"),
}
# Per currency: (annualised total, run rate, one-time, live contracts).
EXPECTED_TOTALS = {
    "EUR": ("85000.00", "60000.00", "25000.00", 2),
    "GBP": ("30000.00", "30000.00", "0.00", 1),
    "USD": ("830200.00", "830200.00", "0.00", 6),  # 624,000 + 38,000 + 95,000 + 49,200 + 18,000 + 6,000
}
# Line shares of the annualised cost, by hand: 70/30 of 624,000; 60 % of 38,000 and the rest shared; 50/50 of 25,000.
EXPECTED_LINES = {
    "Microsoft 365 E5 EA": {"Microsoft 365": "436800.00", "Entra ID": "187200.00"},
    "Atlassian Cloud Premium": {"Jira": "22800.00", "Confluence": "15200.00"},
    "Vault validation package": {"Veeva Vault QMS": "12500.00", "SAP S/4HANA": "12500.00"},
}


@pytest.fixture
def admin(a, api):
    return api(a["users"]["admin"])


@pytest.fixture
def editor(a, api):
    return api(a["users"]["editor"])


def record(a, name, kind="application"):
    return services.create_resource(a["org"], a["users"]["editor"], {"name": name, "kind": kind, "lifecycle": "active"})


@pytest.fixture
def demo(a):
    """The demo records the contracts name, and the renewal the Falcon row links to."""
    made = {
        name: record(a, name)
        for name in (
            "Microsoft 365",
            "Entra ID",
            "Jira",
            "Confluence",
            "Veeva Vault QMS",
            "SAP S/4HANA",
            "Falcon EDR",
        )
    }
    made["IT Ticket Triage"] = record(a, "IT Ticket Triage", "ai_agent")
    made["New Hire Onboarding"] = record(a, "New Hire Onboarding", "component")
    falcon = renewals.create_renewal(
        a["org"],
        a["users"]["editor"],
        {
            "name": "Falcon renewal",
            "auto_renew": "unknown",
            "renewal_date": "2027-01-31",
            "applications": [str(made["Falcon EDR"].id)],
        },
    )
    return {"records": made, "falcon": falcon}


def contract_csv(*rows, columns=None):
    return csvio.write_csv(columns or csvio.TEMPLATE["contract"], list(rows))


def pool_csv(*rows, columns=None):
    return csvio.write_csv(columns or csvio.TEMPLATE["license_pool"], list(rows))


def decide_all(who, run_id, **tables):
    """Create every vendor and Person the preview asks about."""
    panels = run_of(who, run_id)["panels"]
    decisions = {t: {e["key"]: "create" for e in panels[t]} for t in ("vendors", "people") if panels[t]}
    decisions.update(tables)
    response = who.patch(f"imports/{run_id}", {"decisions": decisions})
    assert response.status_code == 200, response.json_body
    return response.json_body


def import_fixture(editor):
    run = upload(editor, FIXTURE.read_bytes(), kind="contract", name="demo_contracts.csv").json_body
    decide_all(editor, run["id"])
    return run


def commit(who, run_id):
    response = who.post(f"imports/{run_id}/commit")
    assert response.status_code == 200, response.json_body
    return response.json_body


# ---- The acceptance check: the earlier demo contracts ------------------------------------------------------------------------


def test_the_demo_contracts_preview_then_apply_with_the_hand_worked_totals_and_deadlines(a, editor, demo):
    created = upload(editor, FIXTURE.read_bytes(), kind="contract", name="demo_contracts.csv")
    assert created.status_code == 201, created.json_body
    run = created.json_body
    assert run["status"] == "previewed" and run["counts"]["total"] == 9
    panels = run["panels"]
    assert sorted(e["name"] for e in panels["vendors"]) == [
        "Anthropic",
        "Atlassian",
        "CrowdStrike",
        "Microsoft",
        "Okta",
        "SAP",
        "Veeva",
    ]
    assert [e["email"] for e in panels["people"]] == ["priya.shah@example.com"]
    assert run["counts"]["pending_decisions"] == 8 and not run["can_commit"]

    body = decide_all(editor, run["id"])
    assert body["counts"]["create"] == 9 and body["counts"]["error"] == 0 and body["can_commit"]
    # The rehearsal was rolled back: nothing exists yet.
    assert not Contract.objects.exists() and not Vendor.objects.filter(name="Okta").exists()
    assert not Person.objects.filter(email="priya.shah@example.com").exists()
    assert not AuditEvent.objects.filter(action__startswith="contract").exists()

    rows = {r["name"]: r for r in rows_of(editor, run["id"])}
    for name, (currency, annual, _end, deadline, label) in EXPECTED.items():
        row = rows[name]
        assert row["outcome"] == "create" and row["errors"] == [], (name, row["errors"])
        computed = row["computed"]
        assert (computed["currency"], computed["annualized_cost"]) == (currency, annual), name
        assert (computed["cancellation_deadline"], computed["deadline_label"]) == (deadline, label), name
        assert row["changes"]["annualized_cost"] == ["", annual]
        assert row["changes"]["cancellation_deadline"] == ["", deadline]
    shares = {a["record"]["name"]: a["amount"] for a in rows["Microsoft 365 E5 EA"]["computed"]["allocation"]}
    assert shares == EXPECTED_LINES["Microsoft 365 E5 EA"]

    done = commit(editor, run["id"])
    assert done["status"] == "committed" and done["counts"]["created"] == 9
    contracts_by_name = {c.name: c for c in Contract.objects.filter(organization=a["org"])}
    assert set(contracts_by_name) == set(EXPECTED)
    for name, (currency, annual, end, deadline, _label) in EXPECTED.items():
        c = contracts_by_name[name]
        assert (c.currency, c.annualized_cost, c.end_date) == (currency, D(annual), date.fromisoformat(end)), name
        assert c.cancellation_deadline == date.fromisoformat(deadline), name
        assert c.renewal_owner is None or c.renewal_owner.email == "priya.shah@example.com"
    assert contracts_by_name["SAP maintenance"].status == "pending_renewal"
    for name, wanted in EXPECTED_LINES.items():
        got = {
            row.item.resource.name: str(row.amount)
            for row in contracts.allocations([contracts_by_name[name]])
            if row.item is not None
        }
        assert got == wanted, name
    triage = contracts_by_name["Anthropic API"].items.get()
    assert triage.resource.kind == "ai_agent" and triage.allocation_percent is None
    rows = {r["name"]: r for r in rows_of(editor, run["id"])}
    assert rows["Okta Workforce Identity"]["result"] == {
        "id": str(contracts_by_name["Okta Workforce Identity"].id),
        "name": "Okta Workforce Identity",
        "kind": "contract",
    }
    # Every write went through the services, audited and naming the run.
    events = AuditEvent.objects.filter(action="contract.created")
    assert events.count() == 9 and all(e.changes["import_run"] == run["id"] for e in events)
    assert AuditEvent.objects.filter(action="contract_item.created").count() == 12  # 2+2+1+1+1+1+1+1+2
    assert AuditEvent.objects.filter(action="import.committed").count() == 1

    # The spend report reconciles with the hand-worked totals, in every dimension.
    for by in ("vendor", "department", "category", "cost_center"):
        report = editor.get(f"reports/spend?by={by}").json_body
        totals = {t["currency"]: t for t in report["totals"]}
        assert {c: (t["annualized"], t["run_rate"], t["one_time"], t["contracts"]) for c, t in totals.items()} == (
            EXPECTED_TOTALS
        )
        sums: dict[str, Decimal] = {}
        for g in report["rows"]:
            sums[g["currency"]] = sums.get(g["currency"], D(0)) + D(g["amount"])
        assert {c: str(v) for c, v in sums.items()} == {c: t[0] for c, t in EXPECTED_TOTALS.items()}, by


def test_the_renewal_linked_rows_agree_with_their_renewals(a, editor, demo):
    import_fixture(editor)
    run = ImportRun.objects.get()
    commit(editor, str(run.id))
    falcon = Contract.objects.get(name="Falcon Enterprise")
    atlassian = Contract.objects.get(name="Atlassian Cloud Premium")
    assert falcon.renewal_obligation_id == demo["falcon"].id
    created = atlassian.renewal_obligation
    assert created is not None and created.name == "Atlassian Cloud Premium"
    # The created renewal covers the applications on the contract's lines (the lines are set before it is made).
    cycle = RenewalCycle.objects.get(obligation=created, completed_on__isnull=True)
    assert {r.name for r in cycle.applications.all()} == {"Jira", "Confluence"}
    for contract in (falcon, atlassian):
        cycle = RenewalCycle.objects.get(obligation_id=contract.renewal_obligation_id, completed_on__isnull=True)
        obligation = cycle.obligation
        assert (cycle.renewal_date, cycle.notice_value, cycle.notice_unit) == (
            contract.end_date,
            contract.notice_value,
            contract.notice_unit,
        )
        reminder = renewal_rules.action_deadline(
            obligation.auto_renew,
            cycle.renewal_date,
            cycle.notice_value,
            cycle.notice_unit,
            cycle.explicit_deadline,
            a["org"].renewal_lead_days,
        )
        assert reminder.date == contract.cancellation_deadline
    driving = contracts.driver(demo["falcon"])
    assert driving is not None and driving.id == falcon.id
    # Deadline rows: a contract that drives a renewal shows only as the renewal's row.
    calendar = editor.get("reports/renewals-calendar?months=24").json_body["rows"]
    ids = {r["id"] for r in calendar}
    assert str(falcon.id) not in ids and str(demo["falcon"].id) in ids


def test_the_rehearsal_writes_nothing_and_a_commit_is_refused_when_a_matched_contract_changed(a, editor, demo):
    first = import_fixture(editor)
    commit(editor, first["id"])
    data = FIXTURE.read_text(encoding="utf-8").replace("52000,USD", "53000,USD").encode()
    run = upload(editor, data, kind="contract").json_body
    assert run["counts"]["update"] == 1 and run["counts"]["unchanged"] == 8
    before = AuditEvent.objects.count()
    contract = Contract.objects.get(name="Microsoft 365 E5 EA")
    contracts.update_contract(a["org"], a["users"]["admin"], contract, {"po_number": "PO-1"})
    refused = editor.post(f"imports/{run['id']}/commit")
    assert refused.status_code == 409 and refused.json_body["type"] == "/problems/import-refused"
    assert "Microsoft 365 E5 EA" in refused.json_body["detail"]
    contract.refresh_from_db()
    assert contract.amount == D("52000.00")
    assert AuditEvent.objects.count() == before + 2  # the edit and the refusal


# ---- re-imports -----------------------------------------------------------------------------------------------------


def test_reimporting_the_same_file_or_the_export_is_a_no_op(a, admin, editor, demo):
    field = customfields.create_field(
        a["org"],
        a["users"]["admin"],
        {"name": "budget_owner", "label": "Budget owner", "type": "text", "applies_to": ["contract", "license_pool"]},
    )
    first = import_fixture(editor)
    commit(editor, first["id"])
    sap = Contract.objects.get(name="SAP maintenance")
    contracts.update_contract(a["org"], a["users"]["editor"], sap, {"custom_field_data": {"budget_owner": "Finance"}})
    again = upload(editor, FIXTURE.read_bytes(), kind="contract").json_body
    assert again["counts"]["unchanged"] == 9 and not again["can_commit"], [
        (r["name"], r["changes"]) for r in rows_of(editor, again["id"]) if r["changes"]
    ]
    exported = editor.get("contracts/export.csv")
    assert exported.status_code == 200
    headers, cells = csvio.read_csv(exported.content)
    assert customfields.column(field) in headers
    assert next(r for r in cells if r["name"] == "SAP maintenance")["cf_budget_owner"] == "Finance"
    reimport = upload(editor, exported.content, kind="contract").json_body
    assert reimport["status"] == "previewed", reimport
    assert reimport["counts"]["unchanged"] == 9 and reimport["counts"]["error"] == 0, [
        (r["name"], r["changes"], r["errors"]) for r in rows_of(editor, reimport["id"])
    ]
    assert not reimport["can_commit"]
    assert {r["matched"]["name"] for r in rows_of(editor, reimport["id"])} == set(EXPECTED)


def test_an_update_changes_only_what_changed(a, editor, demo):
    first = import_fixture(editor)
    commit(editor, first["id"])
    text = (
        FIXTURE.read_text(encoding="utf-8")
        .replace("Jira=60; Confluence,Demo", "Jira=50; Confluence,Demo")
        .replace("4100,USD,monthly", "4300,USD,monthly")
    )
    run = upload(editor, text.encode(), kind="contract").json_body
    assert run["counts"]["update"] == 2 and run["counts"]["unchanged"] == 7
    rows = {r["name"]: r for r in rows_of(editor, run["id"], outcome="update")}
    assert rows["Falcon Enterprise"]["changes"] == {
        "amount": ["4100.00", "4300.00"],
        "annualized_cost": ["49200.00", "51600.00"],
    }
    assert rows["Atlassian Cloud Premium"]["changes"] == {"items": ["Jira=60; Confluence", "Jira=50; Confluence"]}
    marker = AuditEvent.objects.count()
    commit(editor, run["id"])
    new = list(AuditEvent.objects.order_by("created_at")[marker:].values_list("action", flat=True))
    # An amount and a line share: no date changed, so the linked renewal is left alone.
    assert sorted(new) == ["contract.updated", "contract_item.updated", "import.committed"]
    falcon = Contract.objects.get(name="Falcon Enterprise")
    assert (falcon.amount, falcon.annualized_cost) == (D("4300.00"), D("51600.00"))
    jira = ContractItem.objects.get(contract__name="Atlassian Cloud Premium", resource__name="Jira")
    assert jira.allocation_percent == D("50.00")


def test_re_imports_match_on_external_keys(a, admin, editor, demo):
    source = admin.post("import-sources", {"name": "Finance sheet", "kind": "contract"}).json_body
    row = {"external_key": "FIN-1", "name": "Zoom Business", "amount": "1200", "end_date": "2027-01-31"}
    run = upload(editor, contract_csv(row), kind="contract", source=ImportSource.objects.get(id=source["id"]))
    commit(editor, run.json_body["id"])
    contract = Contract.objects.get(name="Zoom Business")
    ident = ExternalIdentifier.objects.get(external_key="FIN-1")
    assert ident.contract_id == contract.id and ident.resource_id is None
    renamed = {**row, "name": "Zoom Business Plus"}
    again = upload(editor, contract_csv(renamed), kind="contract", source=ImportSource.objects.get(id=source["id"]))
    body = again.json_body
    assert body["counts"]["update"] == 1
    assert rows_of(editor, body["id"])[0]["matched"] == {"id": str(contract.id), "name": "Zoom Business"}
    commit(editor, body["id"])
    contract.refresh_from_db()
    assert contract.name == "Zoom Business Plus"
    # A source can't be deleted while it holds keys; deleting the contract takes its key with it.
    assert admin.delete(f"import-sources/{source['id']}").status_code == 409
    contracts.delete_contract(a["org"], a["users"]["admin"], contract)
    assert not ExternalIdentifier.objects.filter(external_key="FIN-1").exists()


def test_a_same_named_contract_without_a_number_asks_to_create_or_match(a, editor, demo):
    existing = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Zoom Business", "amount": "100"})
    run = upload(editor, contract_csv({"name": "Zoom Business", "amount": "200"}), kind="contract").json_body
    assert run["counts"]["pending_decisions"] == 1
    assert run["panels"]["matches"][0]["candidate"]["id"] == str(existing.id)
    editor.patch(f"imports/{run['id']}", {"decisions": {"matches": {"2": "match"}}})
    body = run_of(editor, run["id"])
    assert body["counts"]["update"] == 1 and body["can_commit"]
    commit(editor, run["id"])
    existing.refresh_from_db()
    assert existing.amount == D("200.00") and Contract.objects.count() == 1


# ---- the items column ---------------------------------------------------------------------------------------------------


def test_fixed_allocations_over_100_are_a_row_error(a, editor, demo):
    run = upload(
        editor,
        contract_csv({"name": "Atlassian", "amount": "100", "items": "Jira=80; Confluence=30"}),
        kind="contract",
    ).json_body
    row = rows_of(editor, run["id"])[0]
    assert row["outcome"] == "error"
    assert row["errors"] == [{"column": "items", "message": "Fixed allocations would total 110% (max 100%)."}]


def test_unknown_ambiguous_and_badly_written_lines_are_row_errors(a, b, editor, demo):
    record(a, "Jira", "component")  # now "Jira" names an application and a component
    record(b, "Theirs")
    cells = {
        "Nope=10": 'No application, component or AI agent is named "Nope".',
        "Theirs": 'No application, component or AI agent is named "Theirs".',
        "Jira": '2 records are named "Jira". Write it as application:Jira, component:Jira or agent:Jira, or use its '
        "Softwarden ID.",
        "agent:Jira": 'No AI agent is named "Jira".',
        "Confluence=abc": '"Confluence=abc": Enter the allocation as a number, such as 1200.50.',
        "Confluence=-5": '"Confluence=-5": The allocation can\'t be negative.',
        "Confluence; Confluence": "Confluence is listed twice.",
        str(b["app"].id): f"No application, component or AI agent has the Softwarden ID {b['app'].id}.",
    }
    rows = [{"name": f"C{i}", "items": cell} for i, cell in enumerate(cells)]
    run = upload(editor, contract_csv(*rows), kind="contract").json_body
    got = {r["name"]: r["errors"] for r in rows_of(editor, run["id"])}
    for i, message in enumerate(cells.values()):
        assert got[f"C{i}"] == [{"column": "items", "message": message}], (i, got[f"C{i}"])
    ok = upload(
        editor,
        contract_csv(
            {"name": "OK", "items": f"application:Jira=40; component:Jira; {demo['records']['Confluence'].id}"}
        ),
        kind="contract",
    ).json_body
    row = rows_of(editor, ok["id"])[0]
    assert row["errors"] == [] and row["outcome"] == "create"
    assert row["changes"]["items"] == ["", "application:Jira=40; component:Jira; Confluence"]


def test_a_reshuffle_from_80_20_to_20_80_applies_without_an_over_allocated_state(a, editor, demo):
    jira, confluence = demo["records"]["Jira"], demo["records"]["Confluence"]
    contract = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Atlassian", "amount": "1000"})
    contracts.set_items(a["org"], a["users"]["editor"], contract, [(jira, D(80)), (confluence, D(20))])
    seen: list[Decimal] = []

    def check(sender, instance, **kwargs):
        total = ContractItem.objects.filter(contract_id=instance.contract_id).aggregate(t=Sum("allocation_percent"))
        seen.append(total["t"] or D(0))
        assert (total["t"] or D(0)) <= 100

    post_save.connect(check, sender=ContractItem)
    try:
        # Listed in the order that would pass through 160 % if the raise came first.
        row = {"softwarden_id": str(contract.id), "items": "Confluence=80; Jira=20"}
        run = upload(editor, contract_csv(row), kind="contract").json_body
        assert run["counts"]["update"] == 1 and run["can_commit"]
        commit(editor, run["id"])
    finally:
        post_save.disconnect(check, sender=ContractItem)
    assert seen and max(seen) <= 100
    shares = dict(ContractItem.objects.filter(contract=contract).values_list("resource__name", "allocation_percent"))
    assert shares == {"Jira": D("20.00"), "Confluence": D("80.00")}
    assert AuditEvent.objects.filter(action="contract_item.updated").count() == 2


def test_set_items_lowers_shares_before_raising_them(a, demo):
    jira, confluence = demo["records"]["Jira"], demo["records"]["Confluence"]
    contract = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Atlassian"})
    contracts.set_items(a["org"], a["users"]["editor"], contract, [(jira, D(80)), (confluence, D(20))])
    order = []

    def track(sender, instance, **kwargs):
        order.append((instance.resource_id, instance.allocation_percent))

    post_save.connect(track, sender=ContractItem)
    try:
        contracts.set_items(a["org"], a["users"]["editor"], contract, [(confluence, D(80)), (jira, None)])
    finally:
        post_save.disconnect(track, sender=ContractItem)
    assert order == [(jira.id, None), (confluence.id, D(80))]


# ---- the services' refusals as row errors ----------------------------------------------------------------------------


def test_driving_renewal_rules_surface_as_row_errors(a, editor, demo):
    driver = contracts.create_contract(
        a["org"],
        a["users"]["editor"],
        {"name": "Falcon 2026", "end_date": "2027-01-31", "renewal_obligation": str(demo["falcon"].id)},
    )
    old = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Old", "end_date": "2026-12-31"})
    contracts.update_contract(a["org"], a["users"]["editor"], old, {"status": "cancelled"})
    Contract.objects.filter(id=old.id).update(renewal_obligation=demo["falcon"])  # linked while it didn't count
    rows = [
        {"name": "Another", "end_date": "2027-06-30", "renewal": "Falcon renewal"},
        {"name": "No end", "renewal": "Falcon renewal"},
        {"name": "Both", "end_date": "2027-06-30", "renewal": "Falcon renewal", "create_renewal_obligation": "true"},
        {"name": "Create no end", "create_renewal_obligation": "yes"},
        {"softwarden_id": str(old.id), "status": "active"},
        {"name": "Nowhere", "end_date": "2027-06-30", "renewal": "Missing renewal"},
    ]
    run = upload(editor, contract_csv(*rows), kind="contract").json_body
    got = {r["row_number"]: r["errors"] for r in rows_of(editor, run["id"])}
    taken = f"This renewal already takes its dates from the contract {driver.name}."
    assert got[2] == [{"column": "renewal", "message": taken}]
    assert got[3] == [{"column": "renewal", "message": contracts.NEEDS_END}]
    assert got[4] == [
        {"column": "create_renewal_obligation", "message": "Either link an existing renewal or create one, not both."}
    ]
    assert got[5] == [{"column": "create_renewal_obligation", "message": contracts.NEEDS_END}]
    assert got[6] == [
        {
            "column": "status",
            "message": f"This renewal now takes its dates from the contract {driver.name}. Unlink one of them first.",
        }
    ]
    assert got[7] == [
        {"column": "renewal", "message": 'No renewal is named "Missing renewal". Add it under Renewals first, or leave '
         "it empty."}
    ]  # fmt: skip
    assert run["counts"]["error"] == 6 and not run["can_commit"]
    assert editor.post(f"imports/{run['id']}/commit").status_code == 409
    old.refresh_from_db()
    assert old.status == "cancelled" and Contract.objects.count() == 2


def test_two_rows_linking_one_renewal_are_checked_in_order(a, editor, demo):
    rows = [
        {"name": "First", "end_date": "2027-06-30", "renewal": "Falcon renewal"},
        {"name": "Second", "end_date": "2027-07-31", "renewal": "Falcon renewal"},
    ]
    run = upload(editor, contract_csv(*rows), kind="contract").json_body
    got = {r["name"]: r["errors"] for r in rows_of(editor, run["id"])}
    assert got["First"] == []
    assert got["Second"] == [
        {"column": "renewal", "message": "This renewal already takes its dates from the contract First."}
    ]


def test_cell_errors_name_their_column(a, editor, demo):
    row = {
        "name": "Bad",
        "amount": "12,000.123",
        "currency": "dollars",
        "billing_frequency": "fortnightly",
        "start_date": "31/12/2026",
        "auto_renew": "maybe",
        "notice_value": "two",
        "status": "live",
    }
    run = upload(editor, contract_csv(row), kind="contract").json_body
    columns = {e["column"] for e in rows_of(editor, run["id"])[0]["errors"]}
    assert columns == {
        "amount",
        "currency",
        "billing_frequency",
        "start_date",
        "auto_renew",
        "notice_value",
        "status",
    }


def test_derived_columns_are_ignored_and_a_notice_without_unit_is_in_days(a, editor, demo):
    row = {
        "name": "Derived",
        "amount": "100",
        "billing_frequency": "Monthly",
        "end_date": "2027-03-31",
        "auto_renew": "true",
        "notice_value": "30",
        "annualized_cost": "1",
        "cancellation_deadline": "2020-01-01",
    }
    run = upload(editor, contract_csv(row), kind="contract").json_body
    commit(editor, run["id"])
    c = Contract.objects.get(name="Derived")
    assert (c.annualized_cost, c.notice_value, c.notice_unit, c.auto_renew) == (D("1200.00"), 30, "days", "yes")
    assert c.cancellation_deadline == date(2027, 3, 1)


# ---- licence pools ----------------------------------------------------------------------------------------------------


def test_pools_import_with_seat_sync_rules_and_never_set_last_synced(a, editor, demo):
    m365 = demo["records"]["Microsoft 365"]
    e5 = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "E5 EA", "contract_number": "MS-EA-2026"})
    contracts.create_pool(
        a["org"],
        a["users"]["editor"],
        {"application": str(m365.id), "sku_name": "Visio", "sku_id": "visio-sku", "seat_sync": True},
    )
    rows = [
        # the earlier demo's pools
        {
            "application": "Microsoft 365",
            "contract": "MS-EA-2026",
            "sku_name": "Microsoft 365 E5",
            "sku_id": "06ebc4ee-1bb5-47dd-8120-11324bc54e06",
            "seats_purchased": "450",
            "seats_assigned": "431",
            "seat_sync": "true",
            "last_synced": "2026-11-01T00:00:00+00:00",
        },
        {"application": "Microsoft 365", "contract": "E5 EA", "sku_name": "Power BI Pro", "seats_purchased": "120",
         "seats_assigned": "77"},
        {"application": "Jira", "sku_name": "Jira Premium", "seats_purchased": "200", "seats_assigned": "214"},
        # refusals
        {"application": "Jira", "sku_name": "No SKU", "seat_sync": "true"},
        {"application": "Confluence", "sku_name": "Visio again", "sku_id": "VISIO-SKU", "seat_sync": "yes"},
        {"application": "Confluence", "sku_name": "E5 twice", "sku_id": "06ebc4ee-1bb5-47dd-8120-11324bc54e06",
         "seat_sync": "true"},
        {"application": "IT Ticket Triage", "sku_name": "Agent seats"},
        {"application": "Jira", "contract": "Nope", "sku_name": "X", "seats_purchased": "many"},
    ]  # fmt: skip
    run = upload(editor, pool_csv(*rows), kind="license_pool").json_body
    got = {r["row_number"]: r for r in rows_of(editor, run["id"])}
    assert [got[n]["outcome"] for n in (2, 3, 4)] == ["create"] * 3
    assert got[4]["computed"] == {"seats_purchased": 200, "seats_assigned": 214, "utilization": 107.0}
    assert got[5]["errors"] == [{"column": "seat_sync", "message": "Set the SKU ID to sync seat counts from Entra."}]
    assert got[6]["errors"] == [
        {"column": "seat_sync", "message": "The pool Visio of Microsoft 365 already syncs this SKU; only one pool can."}
    ]
    assert got[7]["errors"] == [
        {
            "column": "seat_sync",
            "message": "The pool Microsoft 365 E5 of Microsoft 365 already syncs this SKU; only one pool can.",
        }
    ]
    assert got[8]["errors"] == [{"column": "application", "message": 'No application is named "IT Ticket Triage".'}]
    assert {e["column"] for e in got[9]["errors"]} == {"contract", "seats_purchased"}
    assert editor.patch(f"imports/{run['id']}", {"exclude": [5, 6, 7, 8, 9]}).status_code == 200
    commit(editor, run["id"])
    pools = {p.sku_name: p for p in LicensePool.objects.filter(organization=a["org"])}
    assert pools["Microsoft 365 E5"].contract_id == e5.id and pools["Microsoft 365 E5"].seat_sync
    assert pools["Power BI Pro"].contract_id == e5.id
    assert (pools["Jira Premium"].seats_purchased, pools["Jira Premium"].seats_assigned) == (200, 214)  # overuse
    assert all(p.last_synced is None for p in pools.values())
    created = AuditEvent.objects.filter(action="license_pool.created", changes__import_run=run["id"])
    assert sorted(e.changes["name"] for e in created) == ["Jira Premium", "Microsoft 365 E5", "Power BI Pro"]


def test_pools_match_by_application_and_sku_and_their_export_reimports_unchanged(a, editor, demo):
    field = customfields.create_field(
        a["org"],
        a["users"]["admin"],
        {"name": "licence_ref", "label": "Licence ref", "type": "text", "applies_to": ["license_pool"]},
    )
    m365 = demo["records"]["Microsoft 365"]
    record(a, "Microsoft 365", "component")  # a same-named component doesn't make the application ambiguous here
    pool = contracts.create_pool(
        a["org"],
        a["users"]["editor"],
        {"application": str(m365.id), "sku_name": "Power BI Pro", "seats_purchased": "120", "seats_assigned": "77"},
    )
    LicensePool.objects.filter(id=pool.id).update(last_synced="2026-11-01T00:00:00Z")
    row = {"application": "Microsoft 365", "sku_name": "Power BI Pro", "seats_assigned": "80", "cf_licence_ref": "L-1"}
    run = upload(editor, pool_csv(row, columns=[*csvio.TEMPLATE["license_pool"], "cf_licence_ref"]),
                 kind="license_pool").json_body  # fmt: skip
    found = rows_of(editor, run["id"])[0]
    assert found["matched"] == {"id": str(pool.id), "name": "Power BI Pro"}
    assert found["changes"] == {"seats_assigned": ["77", "80"], "cf_licence_ref": ["", "L-1"]}
    commit(editor, run["id"])
    pool.refresh_from_db()
    assert (pool.seats_assigned, pool.custom_field_data) == (80, {"licence_ref": "L-1"})
    assert pool.last_synced is not None  # untouched by the import
    exported = editor.get("license-pools/export.csv")
    assert exported.status_code == 200
    headers, cells = csvio.read_csv(exported.content)
    assert headers == [*money_import.EXPORT_COLUMNS["license_pool"], customfields.column(field)]
    assert cells[0]["application"] == "Microsoft 365"  # only applications count for a pool's application
    again = upload(editor, exported.content, kind="license_pool").json_body
    assert again["counts"]["unchanged"] == 1 and not again["can_commit"], rows_of(editor, again["id"])
    assert AuditEvent.objects.filter(action="export", changes__kind="license_pool").count() == 1


# ---- templates, mapping, roles, tenancy -------------------------------------------------------------------------------


def test_templates_list_the_kinds_columns_and_custom_fields(a, editor):
    CustomField.objects.create(
        organization=a["org"], name="budget_owner", label="Budget owner", type="text", applies_to=["contract"]
    )
    customfields.forget(a["org"])
    for kind, url_kind in (("contract", "contracts"), ("license_pool", "licence-pools")):
        response = editor.get(f"imports/template.csv?kind={url_kind}")
        assert response.status_code == 200
        headers = response.content.decode("utf-8-sig").splitlines()[0].split(",")
        expected = [*csvio.TEMPLATE[kind], *(["cf_budget_owner"] if kind == "contract" else [])]
        assert headers == expected
    assert editor.get("imports/template.csv?kind=integration").status_code == 400


def test_pool_columns_are_mapped_with_the_sku_name_required(a, editor, demo):
    data = csvio.write_csv(["App", "Plan", "Seats"], [{"App": "Jira", "Plan": "Jira Premium", "Seats": "10"}])
    run = upload(editor, data, kind="license_pool").json_body
    assert run["needs_mapping"]
    required = {f["value"] for f in run["fields"] if f["required"]}
    assert required == {"sku_name"}
    refused = editor.patch(
        f"imports/{run['id']}", {"mapping": {"App": "application", "Plan": "ignore", "Seats": "ignore"}}
    )
    assert refused.status_code == 400 and refused.json_body["errors"]["mapping"] == [
        "Map a column to SKU name to continue."
    ]
    mapped = editor.patch(
        f"imports/{run['id']}", {"mapping": {"App": "application", "Plan": "sku_name", "Seats": "seats_purchased"}}
    )
    assert mapped.status_code == 200 and mapped.json_body["counts"]["create"] == 1
    assert rows_of(editor, run["id"], q="premium")[0]["name"] == "Jira Premium"


def test_roles_capability_and_tenancy(a, b, api, editor, demo, monkeypatch):
    data = contract_csv({"name": "X"})
    assert upload(api(a["users"]["viewer"]), data, kind="contract").status_code == 403
    assert api(a["users"]["viewer"]).get("imports/template.csv?kind=contract").status_code == 403
    assert api(a["users"]["viewer"]).get("license-pools/export.csv").status_code == 200  # exports are any role's
    theirs = contracts.create_contract(b["org"], b["users"]["editor"], {"name": "Theirs", "contract_number": "T-1"})
    rows = [{"softwarden_id": str(theirs.id), "name": "Hijack"}, {"name": "Mine", "contract_number": "T-1"}]
    run = upload(editor, contract_csv(*rows), kind="contract").json_body
    got = rows_of(editor, run["id"])
    assert got[0]["errors"] == [
        {"column": "softwarden_id", "message": "No contract in this organization has that Softwarden ID."}
    ]
    assert got[1]["outcome"] == "create" and got[1]["matched"] is None
    pool_run = upload(editor, pool_csv({"application": "orgb App", "sku_name": "S"}), kind="license_pool").json_body
    assert rows_of(editor, pool_run["id"])[0]["errors"] == [
        {"column": "application", "message": 'No application is named "orgb App".'}
    ]
    theirs_run = ImportRun.objects.create(
        organization=b["org"], kind="contract", filename="x.csv", purge_after=date(2030, 1, 1)
    )
    assert editor.get(f"imports/{theirs_run.id}").status_code == 404
    from core import capabilities

    monkeypatch.setattr(capabilities, "capabilities", lambda org: frozenset({"imports", "catalog"}))
    refused = upload(editor, data, kind="contract")
    assert refused.status_code == 403 and refused.json_body["type"] == "/problems/capability-required"
    assert editor.post("import-sources", {"name": "S", "kind": "license_pool"}).status_code in (403,)
    assert editor.get("imports/template.csv?kind=contract").status_code == 403


def test_purging_a_contract_run_clears_what_the_preview_computed(a, editor, demo):
    from imports import importer

    run = upload(editor, contract_csv({"name": "C", "amount": "10"}), kind="contract").json_body
    stored = ImportRow.objects.get(run_id=run["id"])
    assert stored.computed["annualized_cost"] == "10.00"
    importer.purge_run(ImportRun.objects.get(id=run["id"]), "expired")
    stored.refresh_from_db()
    assert stored.computed == {} and stored.raw == {}


# ---- the reports review nits --------------------------------------------------------------------------------------------


def test_the_hourly_job_re_derives_drifted_deadlines(a, b, api):
    c = contracts.create_contract(
        a["org"], a["users"]["editor"], {"name": "Drift", "end_date": "2027-03-31", "auto_renew": "yes"}
    )
    gone = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Gone", "end_date": "2027-03-31"})
    contracts.archive_contract(a["org"], a["users"]["editor"], gone)
    right = c.cancellation_deadline
    assert right is not None
    Contract.objects.filter(id__in=[c.id, gone.id]).update(cancellation_deadline=right + timedelta(days=5))
    marker = AuditEvent.objects.count()
    result = terms.scheduled_advance(datetime(2026, 11, 5, 12, 0, tzinfo=UTC))
    assert result["deadlines"] == 1
    c.refresh_from_db()
    gone.refresh_from_db()
    assert c.cancellation_deadline == right  # live: put right
    assert gone.cancellation_deadline == right + timedelta(days=5)  # archived: left alone
    assert AuditEvent.objects.count() == marker  # a derived column, not an edit (refresh_deadlines)
    assert terms.scheduled_advance(datetime(2026, 11, 5, 13, 0, tzinfo=UTC))["deadlines"] == 0
    rows = api(a["users"]["viewer"]).get("reports/renewals-calendar?months=24").json_body["rows"]
    row = next(r for r in rows if r["id"] == str(c.id))
    assert (row["action"], row["label_text"]) == ("cancel_by", "Assumed")


def test_a_stale_cancel_by_row_is_labelled_cancellation_deadline(a, api):
    from money import reporting

    c = contracts.create_contract(
        a["org"], a["users"]["editor"], {"name": "Stale", "end_date": "2027-03-31", "auto_renew": "yes"}
    )
    Contract.objects.filter(id=c.id).update(cancellation_deadline=date(2027, 1, 2))
    rows = reporting.deadline_rows(a["org"], date(2026, 11, 5))
    row = next(r for r in rows if r["id"] == str(c.id))
    assert (row["action"], row["label"], row["label_text"]) == ("cancel_by", None, "Cancellation deadline")


def test_imported_contracts_store_their_derived_values(a, editor, demo):
    run = upload(
        editor,
        contract_csv({"name": "Derived", "amount": "250", "billing_frequency": "quarterly", "end_date": "2027-01-31"}),
        kind="contract",
    ).json_body
    commit(editor, run["id"])
    c = Contract.objects.get(name="Derived")
    assert c.annualized_cost == contracts.annualize(c.amount, c.billing_frequency, c.start_date, c.end_date)
    assert c.cancellation_deadline == contracts.contract_deadline(c, a["org"].renewal_lead_days).date
    assert RenewalObligation.objects.count() == 1 and Resource.objects.filter(organization=a["org"]).count() == 10


# ---- review follow-ups -------------------------------------------------------------------------------------------------


def test_large_files_skip_the_rehearsal_and_say_so(a, editor, demo, monkeypatch):
    monkeypatch.setattr(money_import, "REHEARSE_MAX", 2)
    rows = [
        {"name": "A", "amount": "10", "end_date": "2027-01-31", "renewal": "Falcon renewal"},
        {"name": "B", "amount": "10", "end_date": "2027-02-28", "renewal": "Falcon renewal"},
        {"name": "C", "amount": "10"},
    ]
    run = upload(editor, contract_csv(*rows), kind="contract").json_body
    # Not rehearsed: B's clash with A over the renewal isn't seen until the commit, and the preview says so.
    assert run["counts"]["rehearsed"] == 0 and run["counts"]["error"] == 0 and run["can_commit"]
    assert run["panels"]["notices"] == [money_import.LARGE_FILE_NOTICE]
    assert all(r["computed"] == {} for r in rows_of(editor, run["id"]))
    refused = editor.post(f"imports/{run['id']}/commit")
    assert refused.status_code == 409
    assert refused.json_body["detail"].startswith("Row 3 couldn't be saved: This renewal already takes its dates")
    assert not Contract.objects.exists()
    small = upload(editor, contract_csv(*rows[:2]), kind="contract").json_body
    assert small["counts"]["rehearsed"] == 1 and small["panels"]["notices"] == [] and small["counts"]["error"] == 1


def test_every_event_a_commit_writes_names_the_run(a, editor, demo):
    run = upload(editor, FIXTURE.read_bytes(), kind="contract").json_body
    decide_all(editor, run["id"])
    marker = set(AuditEvent.objects.values_list("id", flat=True))
    commit(editor, run["id"])
    events = AuditEvent.objects.exclude(id__in=marker)
    actions = set(events.values_list("action", flat=True))
    assert {"vendor.created", "person.created", "contract.created", "contract_item.created", "renewal.created",
            "contract.updated", "renewal_cycle.updated", "import.committed"} <= actions  # fmt: skip
    unnamed = [e.action for e in events if e.action != "import.committed" and e.changes.get("import_run") != run["id"]]
    assert unnamed == []
    pool_run = upload(editor, pool_csv({"application": "Jira", "sku_name": "Jira Premium"}), kind="license_pool")
    marker = set(AuditEvent.objects.values_list("id", flat=True))
    commit(editor, pool_run.json_body["id"])
    events = AuditEvent.objects.exclude(id__in=marker).exclude(action="import.committed")
    assert [e.changes.get("import_run") for e in events] == [pool_run.json_body["id"]]


def test_rows_creating_one_natural_key_twice_are_errors(a, editor, demo):
    rows = [
        {"name": "A", "contract_number": "N-1"},
        {"name": "B", "contract_number": "n-1"},
        {"name": "C", "contract_number": "N-2"},
    ]
    run = upload(editor, contract_csv(*rows), kind="contract").json_body
    got = {r["name"]: r["errors"] for r in rows_of(editor, run["id"])}
    assert got["A"] == [{"column": "contract_number", "message": "The same contract number is on row 3."}]
    assert got["B"] == [{"column": "contract_number", "message": "The same contract number is on row 2."}]
    assert got["C"] == []
    pools = [
        {"application": "Jira", "sku_name": "Jira Premium"},
        {"application": "jira", "sku_name": "JIRA premium"},
        {"application": "Confluence", "sku_name": "Jira Premium"},
    ]
    run = upload(editor, pool_csv(*pools), kind="license_pool").json_body
    got_pools = {r["row_number"]: r["errors"] for r in rows_of(editor, run["id"])}
    message = "The same application and SKU name is on row {}."
    assert got_pools[2] == [{"column": "sku_name", "message": message.format(3)}]
    assert got_pools[3] == [{"column": "sku_name", "message": message.format(2)}]
    assert got_pools[4] == []


def test_a_prefixed_application_name_round_trips_in_the_pool_export(a, editor, demo):
    odd = record(a, "Agent: HR bot")
    contracts.create_pool(a["org"], a["users"]["editor"], {"application": str(odd.id), "sku_name": "Bot seats"})
    _headers, cells = csvio.read_csv(editor.get("license-pools/export.csv").content)
    assert cells[0]["application"] == "application:Agent: HR bot"
    again = upload(editor, editor.get("license-pools/export.csv").content, kind="license_pool").json_body
    assert again["counts"]["unchanged"] == 1 and again["counts"]["error"] == 0
    typed = upload(
        editor, pool_csv({"application": "application:Jira", "sku_name": "Jira Premium"}), kind="license_pool"
    ).json_body
    assert rows_of(editor, typed["id"])[0]["errors"] == []


def test_commit_locks_the_matched_contracts_before_planning(a, editor, demo, monkeypatch):
    from imports import importer

    contract = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Locked", "amount": "10"})
    run = upload(editor, contract_csv({"softwarden_id": str(contract.id), "amount": "20"}), kind="contract").json_body
    seen = []
    real = importer._lock_matched

    def spy(model, field, stored):
        seen.append(
            (model.__name__, field, sorted(str(getattr(r, field)) for r in stored.values() if getattr(r, field)))
        )
        return real(model, field, stored)

    monkeypatch.setattr(importer, "_lock_matched", spy)
    commit(editor, run["id"])
    assert ("Contract", "matched_contract_id", [str(contract.id)]) in seen


def test_every_run_step_needs_the_capability(a, editor, demo, monkeypatch):
    from core import capabilities

    run = upload(editor, contract_csv({"name": "X"}), kind="contract").json_body
    monkeypatch.setattr(capabilities, "capabilities", lambda org: frozenset({"imports", "catalog"}))
    for method, url, body in (
        ("patch", f"imports/{run['id']}", {"exclude": []}),
        ("post", f"imports/{run['id']}/commit", None),
        ("post", f"imports/{run['id']}/rerun", None),
    ):
        response = getattr(editor, method)(url, body)
        assert response.status_code == 403 and response.json_body["type"] == "/problems/capability-required", url
    assert not Contract.objects.exists()


def test_the_column_mapping_step_needs_the_capability(a, editor, demo, monkeypatch):
    from core import capabilities

    data = csvio.write_csv(["Agreement", "Supplier"], [{"Agreement": "Mapped", "Supplier": "Acme"}])
    run = upload(editor, data, kind="contract").json_body
    assert run["needs_mapping"]  # not the template: the next step is mapping its columns
    mapping = {"mapping": {"Agreement": "name", "Supplier": "vendor"}}
    monkeypatch.setattr(capabilities, "capabilities", lambda org: frozenset({"imports", "catalog"}))
    response = editor.patch(f"imports/{run['id']}", mapping)
    assert response.status_code == 403 and response.json_body["type"] == "/problems/capability-required"
    assert response.json_body["capability"] == "contracts"
    assert ImportRun.objects.get(id=run["id"]).mapping == {}  # nothing was saved
    monkeypatch.undo()
    assert editor.patch(f"imports/{run['id']}", mapping).status_code == 200  # the same step, with the capability
    assert not Contract.objects.exists()


def test_the_hourly_refresh_doesnt_overwrite_a_concurrent_edit(a, monkeypatch):
    c = contracts.create_contract(
        a["org"], a["users"]["editor"], {"name": "Race", "end_date": "2027-03-31", "auto_renew": "yes"}
    )
    Contract.objects.filter(id=c.id).update(cancellation_deadline=date(2020, 1, 1))
    real = contracts.contract_deadline

    def edited_meanwhile(contract, lead_days):
        # update_contract lands between the job's read and its write.
        Contract.objects.filter(id=c.id).update(end_date=date(2027, 6, 30), cancellation_deadline=date(2027, 4, 1))
        return real(contract, lead_days)

    monkeypatch.setattr(contracts, "contract_deadline", edited_meanwhile)
    assert contracts.refresh_deadlines(a["org"], live_only=True) == 0
    c.refresh_from_db()
    assert (c.end_date, c.cancellation_deadline) == (date(2027, 6, 30), date(2027, 4, 1))
