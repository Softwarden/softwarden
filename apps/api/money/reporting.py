"""Spend, deadlines and the dashboard (reports; re-implemented). Read-only:
nothing here writes, so nothing is audited except the exports (money/report_views.py).

Spend. The annualised cost of *live* contracts (Active and Pending renewal, not archived, with a cost; Signed terms
are future commitments and Renewed, Expired and Cancelled ones are over), split over each contract's lines by
`contracts.allocations` to the cent. Every allocation row lands in exactly one group of a dimension, so each
dimension's groups add up, per currency, to the same total. Currencies are never converted:
every total is per currency. The dimensions:
- vendor: the contract's vendor (gap `no_vendor`);
- department: the line's record's department (gaps `no_department`, `unallocated` for cost not on a record);
- category: the line's application's software category (gaps `no_category`, `not_application` for components and
  AI agents, `unallocated`);
- cost_center: the line's record's cost centre, else the contract's (the record that uses the money is more
  specific than the agreement); cost not on a record takes the contract's; gap `no_cost_center`.
`commitments` keeps these concepts apart per currency: run rate (recurring), one-time, their sum (the annualised
total), and Signed terms not started yet (future, with the next start date). The data is read as columns, not model
instances (two queries), and split by `contracts.split`, the maths `contracts.allocations` runs on models: at 1,500
contracts, building instances was most of the dashboard's time (tests/test_spend_path.py).

Deadlines. One source of truth: the dates are the ones reminders use. A renewal's row is its open cycle's action
deadline from `renewals.queue_states` (the states Needs attention and the digest read); a renewal that a contract
drives shows that contract on its row and the contract gets no row of its own (its `cancellation_deadline` is the
same date by construction, money/contracts.py). A contract that drives no renewal (none linked, or its renewal is
archived) and is Signed, Active or Pending renewal shows its stored `cancellation_deadline`, derived by the same
`renewal_rules.action_deadline`, unless a next term (not cancelled, not archived) renews it: renewed or renegotiated,
its action is taken. Only open actions are listed: a renewal whose latest cycle is complete has none."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import Exists, OuterRef, Q

from catalog.models import Kind
from core import capabilities as caps
from core.drf import UserBriefSerializer, represent
from core.models import Role

from . import contracts, renewal_rules
from .models import LIVE_STATUSES, OBLIGATION_STATUSES, BillingFrequency, Contract, ContractItem, ContractStatus

ZERO = Decimal("0")
# A contract row whose stored deadline its computation doesn't explain gets the neutral words for its action, the
# same ones the calendar feed's event titles use (report_views.SUMMARY). The hourly term-status job re-derives such
# dates (money/terms.py), so this only shows until it next runs.
NEUTRAL_LABELS = {"cancel_by": "Cancellation deadline", "renew_by": "Renewal action due"}  # every deadline action
# The `kind` of an expiring_credentials row: an API token, or a client secret or certificate discovery found.
CREDENTIAL_KINDS = ["api_token", "discovered_credential"]
log = logging.getLogger("softwarden.reports")
DIMENSIONS = ("vendor", "department", "category", "cost_center")
GAPS = {
    "no_vendor": "No vendor",
    "unallocated": "Not on a record",
    "no_department": "No department",
    "no_category": "No category",
    "not_application": "Not an application",
    "no_cost_center": "No cost center",
    "other": "Everything else",
}
WINDOWS = (30, 60, 90)
CALENDAR_MONTHS = 12
CALENDAR_MONTHS_MAX = 24
CREDENTIAL_DAYS = 30
DASHBOARD_TOP = 8  # groups per currency on the dashboard's spend charts; the rest is one "Everything else" row
DASHBOARD_DEADLINES = 10


def money(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(contracts.CENT))


# ---- spend ----------------------------------------------------------------------------------------------------------


def obligation_terms(org):
    """Contracts with obligations ahead or running now (Signed, Active, Pending renewal), not archived, with a cost."""
    return Contract.objects.filter(
        organization=org, status__in=OBLIGATION_STATUSES, archived_at__isnull=True, annualized_cost__isnull=False
    )


@dataclass(frozen=True, slots=True)
class Term:
    """The columns of a contract the spend maths and its rows read (not a model instance: building ~1,500 of those on
    every dashboard load was most of its time)."""

    id: Any
    name: str
    status: str
    currency: str
    billing_frequency: str
    annualized_cost: Decimal | None
    start_date: date | None
    cost_center: str
    vendor_id: Any
    vendor_name: str | None


@dataclass(frozen=True, slots=True)
class Line:
    """The columns of a contract line and its record the spend maths and its groups read."""

    resource_id: Any
    allocation_percent: Decimal | None
    name: str
    kind: str
    cost_center: str
    department_id: Any
    department_name: str | None
    category_id: Any
    category_name: str | None


@dataclass(frozen=True, slots=True)
class Row:
    """One allocation row (contracts.allocations' Allocation on plain values): `line` None is the unallocated
    remainder, or a contract with no lines."""

    term: Term
    line: Line | None
    share: Decimal
    amount: Decimal


TERM_FIELDS = (
    "id",
    "name",
    "status",
    "currency",
    "billing_frequency",
    "annualized_cost",
    "start_date",
    "cost_center",
    "vendor_id",
    "vendor__name",
)
LINE_FIELDS = (
    "contract_id",
    "resource_id",
    "allocation_percent",
    "resource__name",
    "resource__kind",
    "resource__cost_center",
    "resource__department_id",
    "resource__department__name",
    "resource__application__category_id",
    "resource__application__category__name",
)


def _lines(org, live) -> dict[Any, list[Line]]:
    """{contract id: its lines, oldest first} for the contracts in queryset `live`, in one query."""
    found: dict[Any, list[Line]] = defaultdict(list)
    rows = (
        ContractItem.objects.filter(organization=org, contract__in=live.values("id"))
        .order_by("created_at", "id")
        .values_list(*LINE_FIELDS)
    )
    for contract_id, *values in rows:
        found[contract_id].append(Line(*values))
    return found


@dataclass(frozen=True)
class Spend:
    """The live contracts' allocation rows and the commitments per currency, from two queries."""

    allocations: list[Row]
    commitments: list[dict[str, Any]]


def load(org) -> Spend:
    """Two column-limited queries (no model instances) and the allocation maths of contracts.split, so each row is the
    one contracts.allocations gives for the same contract and line (tests/test_spend_path.py checks this on random
    data)."""
    terms_qs = obligation_terms(org)
    terms = [Term(*values) for values in terms_qs.order_by("name", "id").values_list(*TERM_FIELDS)]
    lines = _lines(org, terms_qs.filter(status__in=LIVE_STATUSES))
    rows: list[Row] = []
    for term in terms:
        if term.status not in LIVE_STATUSES:
            continue
        mine = lines.get(term.id, [])
        for index, share, amount in contracts.split(term.annualized_cost, [x.allocation_percent for x in mine]):
            rows.append(Row(term, mine[index] if index is not None else None, share, amount))
    return Spend(rows, commitments(terms))


def commitments(terms: Iterable[Term]) -> list[dict[str, Any]]:
    """Per currency (never converted), concepts kept apart: run_rate (recurring annualised cost of live terms),
    one_time (one-time charges on live terms, counted once in full), annualized (their sum: the spend total every
    dimension adds up to), future and future_one_time (Signed terms not started yet), next_start (the earliest start
    of those), and how many live contracts."""
    rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "run_rate": ZERO,
            "one_time": ZERO,
            "future": ZERO,
            "future_one_time": ZERO,
            "next_start": None,
            "contracts": 0,
        }
    )
    for c in terms:
        row = rows[c.currency]
        one_time = c.billing_frequency == BillingFrequency.ONE_TIME
        cost = c.annualized_cost or ZERO
        if c.status == ContractStatus.SIGNED:
            row["future_one_time" if one_time else "future"] += cost
            if c.start_date and (row["next_start"] is None or c.start_date < row["next_start"]):
                row["next_start"] = c.start_date
        else:
            row["one_time" if one_time else "run_rate"] += cost
            row["contracts"] += 1
    return [
        {
            "currency": currency,
            "annualized": money(row["run_rate"] + row["one_time"]),
            "run_rate": money(row["run_rate"]),
            "one_time": money(row["one_time"]),
            "future": money(row["future"]),
            "future_one_time": money(row["future_one_time"]),
            "next_start": row["next_start"].isoformat() if row["next_start"] else None,
            "contracts": row["contracts"],
        }
        for currency, row in sorted(rows.items())
    ]


@dataclass(frozen=True)
class Group:
    key: str | None  # the vendor, department or category id, or the cost centre text; None for a gap
    label: str
    gap: str | None


def group_of(by: str, row: Row) -> Group:
    """The group an allocation row belongs to in dimension `by`."""
    term, line = row.term, row.line
    if by == "vendor":
        if term.vendor_id is None:
            return Group(None, GAPS["no_vendor"], "no_vendor")
        return Group(str(term.vendor_id), term.vendor_name or "", None)
    if by == "cost_center":
        text = (line.cost_center if line is not None else "") or term.cost_center
        return Group(text, text, None) if text else Group(None, GAPS["no_cost_center"], "no_cost_center")
    if line is None:
        return Group(None, GAPS["unallocated"], "unallocated")
    if by == "department":
        if line.department_id is None:
            return Group(None, GAPS["no_department"], "no_department")
        return Group(str(line.department_id), line.department_name or "", None)
    if by == "category":
        if line.kind != Kind.APPLICATION:
            return Group(None, GAPS["not_application"], "not_application")
        if line.category_id is None:  # no category, or (older data) no application row
            return Group(None, GAPS["no_category"], "no_category")
        return Group(str(line.category_id), line.category_name or "", None)
    raise ValueError(by)


def groups(by: str, rows: Iterable[Row]) -> list[dict[str, Any]]:
    """Spend per group and currency, largest first within each currency: {key, label, gap, currency, amount,
    contracts, records}. Each currency's amounts add up to its annualised total."""
    found: dict[tuple[str, Group], dict[str, Any]] = {}
    for row in rows:
        group = group_of(by, row)
        entry = found.get((row.term.currency, group))
        if entry is None:
            entry = found[(row.term.currency, group)] = {"amount": ZERO, "contracts": set(), "records": set()}
        entry["amount"] += row.amount
        entry["contracts"].add(row.term.id)
        if row.line is not None:
            entry["records"].add(row.line.resource_id)
    out = [
        {
            "key": group.key,
            "label": group.label,
            "gap": group.gap,
            "currency": currency,
            "amount": money(entry["amount"]),
            "contracts": len(entry["contracts"]),
            "records": len(entry["records"]),
            "_amount": entry["amount"],
        }
        for (currency, group), entry in found.items()
    ]
    out.sort(key=lambda r: (r["currency"], -r["_amount"], r["gap"] is not None, r["label"].casefold()))
    for r in out:
        del r["_amount"]
    return out


def top(rows: list[dict[str, Any]], limit: int = DASHBOARD_TOP) -> list[dict[str, Any]]:
    """Per currency: the largest `limit` real groups, every named gap (unallocated, no department and so on: always
    listed, never folded), and one "Everything else" row for the remaining real groups, so the sum is unchanged."""
    out: list[dict[str, Any]] = []
    by_currency: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_currency[r["currency"]].append(r)
    for currency, items in sorted(by_currency.items()):
        named = [r for r in items if r["gap"] is None]
        out.extend(named[:limit])
        out.extend(r for r in items if r["gap"] is not None)
        rest = named[limit:]
        if rest:
            out.append(
                {
                    "key": None,
                    "label": GAPS["other"],
                    "gap": "other",
                    "currency": currency,
                    "amount": money(sum((Decimal(r["amount"]) for r in rest), ZERO)),
                    "contracts": None,  # a contract can be in several of the folded groups
                    "records": None,
                    "groups": len(rest),
                }
            )
    return out


def allocation_rows(by: str, rows: Iterable[Row]) -> list[dict[str, Any]]:
    """Each allocation row with its group in dimension `by`: the report's detail and its CSV."""
    out: list[dict[str, Any]] = []
    percent = Decimal("0.01")
    for row in rows:
        group = group_of(by, row)
        term, line = row.term, row.line
        out.append(
            {
                "key": group.key,
                "label": group.label,
                "gap": group.gap,
                "currency": term.currency,
                "amount": money(row.amount),
                "percent": str(row.share.quantize(percent)),
                "contract": {"id": str(term.id), "name": term.name},
                "vendor": {"id": str(term.vendor_id), "name": term.vendor_name} if term.vendor_id else None,
                "record": (
                    {"id": str(line.resource_id), "name": line.name, "kind": line.kind} if line is not None else None
                ),
                "cost_center": (line.cost_center if line is not None else "") or term.cost_center,
            }
        )
    out.sort(key=lambda r: (r["gap"] is not None, r["label"].casefold(), r["contract"]["name"].casefold()))
    return out


def spend_report(org, by: str) -> dict[str, Any]:
    if by not in DIMENSIONS:
        raise ValueError(by)
    spend = load(org)
    return {
        "by": by,
        "totals": spend.commitments,
        "rows": groups(by, spend.allocations),
        "allocations": allocation_rows(by, spend.allocations),
    }


# ---- deadlines ------------------------------------------------------------------------------------------------------


DEADLINE_FIELDS = (
    "id",
    "name",
    "vendor__name",
    "cancellation_deadline",
    "auto_renew",
    "end_date",
    "notice_value",
    "notice_unit",
    "annualized_cost",
    "currency",
)


def _timing(day: date, today: date) -> dict[str, Any]:
    timing = renewal_rules.timing(day, today)
    assert timing is not None
    return timing


def deadline_rows(
    org, today: date, *, until: date | None = None, states: list[Any] | None = None
) -> list[dict[str, Any]]:
    """Every open action deadline on or before `until` (all of them without one), overdue ones included, soonest
    first. Renewals (capability renewals) from their open cycles; contracts (capability contracts) that drive no
    renewal from their stored cancellation deadline. See the module docstring for the one source of truth. `states`
    is renewals.queue_states(org, today) when the caller already has it (the dashboard shares it with Needs
    attention)."""
    from . import renewals

    has = caps.capabilities(org)
    rows: list[dict[str, Any]] = []
    covered: set[Any] = set()  # contracts whose dates a renewal row already shows
    if caps.RENEWALS in has:
        if states is None:
            states = renewals.queue_states(org, today)
        drivers = contracts.drivers(org, [s.obligation.id for s in states]) if caps.CONTRACTS in has else {}
        covered = {c.id for c in drivers.values()}
        costs = {
            cid: (cost, currency)
            for cid, cost, currency in Contract.objects.filter(organization=org, id__in=covered).values_list(
                "id", "annualized_cost", "currency"
            )
        }
        users = get_user_model().objects.in_bulk({s.obligation.followup_id for s in states} - {None})
        for s in states:
            day = s.deadline.date
            if not s.open or day is None or (until is not None and day > until):
                continue
            o, driver = s.obligation, drivers.get(s.obligation.id)
            cost, currency = costs.get(driver.id, (None, None)) if driver else (None, None)
            rows.append(
                {
                    "uid": f"renewal-cycle-{s.cycle.id}@softwarden",
                    "source": "renewal",
                    "id": str(o.id),
                    "cycle": str(s.cycle.id),
                    "name": o.name,
                    "vendor": o.vendor_name,
                    "date": day.isoformat(),
                    "action": "cancel_by" if o.auto_renew != "no" else "renew_by",
                    "label": s.deadline.label,
                    "label_text": renewal_rules.LABELS.get(s.deadline.label),
                    "explanation": s.deadline.explanation,
                    "term_ends": s.cycle.renewal_date.isoformat() if s.cycle.renewal_date else None,
                    "auto_renew": o.auto_renew,
                    "decision": s.cycle.decision or None,
                    "timing": _timing(day, today),
                    "contract": {"id": str(driver.id), "name": driver.name} if driver else None,
                    "annualized_cost": money(cost),
                    "currency": currency,
                    "owner": represent(UserBriefSerializer, users.get(o.followup_id)),
                    "path": f"/renewals/{o.id}",
                }
            )
    if caps.CONTRACTS in has:
        candidates = Contract.objects.filter(
            organization=org,
            status__in=OBLIGATION_STATUSES,
            archived_at__isnull=True,
            cancellation_deadline__isnull=False,
        )
        if until is not None:
            candidates = candidates.filter(cancellation_deadline__lte=until)
        # A term a next term renews (renewed or renegotiated: the renew action leaves it Active until its end date)
        # has no action left, whether or not renewals are on.
        candidates = candidates.exclude(
            Exists(contracts.counting_successors().filter(organization_id=org.id, predecessor_id=OuterRef("pk")))
        )
        if caps.RENEWALS in has:
            # A contract linked to a renewal that isn't archived never has a row of its own: the renewal's row is the
            # action (with this contract on it while it drives; an older term a successor took over has no action).
            candidates = candidates.filter(
                Q(renewal_obligation__isnull=True) | Q(renewal_obligation__archived_at__isnull=False)
            )
        # Columns, not model instances (a dashboard at 1,500 contracts lists hundreds of these).
        for c in candidates.values_list(*DEADLINE_FIELDS, named=True):
            # The date is the stored deadline (the one the filters and reminders read). The same computation gives
            # its label and explanation and agrees with it by construction (money/contracts.derive); if a stored date
            # is ever stale, the row keeps the stored date with a neutral label rather than failing the whole report.
            day = c.cancellation_deadline
            if day is None:
                continue
            computed = contracts.deadline_of(
                c.auto_renew, c.end_date, c.notice_value, c.notice_unit, org.renewal_lead_days
            )
            agrees = computed.date == day
            if not agrees:
                log.warning("contract deadline differs from its computation contract=%s", c.id)
            action = "cancel_by" if c.auto_renew != "no" else "renew_by"
            rows.append(
                {
                    "uid": f"contract-{c.id}@softwarden",
                    "source": "contract",
                    "id": str(c.id),
                    "cycle": None,
                    "name": c.name,
                    "vendor": c.vendor__name,
                    "date": day.isoformat(),
                    "action": action,
                    "label": computed.label if agrees else None,
                    "label_text": renewal_rules.LABELS.get(computed.label) if agrees else NEUTRAL_LABELS[action],
                    "explanation": computed.explanation if agrees else "",
                    "term_ends": c.end_date.isoformat() if c.end_date else None,
                    "auto_renew": c.auto_renew,
                    "decision": None,
                    "timing": _timing(day, today),
                    "contract": {"id": str(c.id), "name": c.name},
                    "annualized_cost": money(c.annualized_cost),
                    "currency": c.currency,
                    "owner": None,
                    "path": f"/contracts/{c.id}",
                }
            )
    rows.sort(key=lambda r: (r["date"], r["name"].casefold(), r["uid"]))
    return rows


def windows(rows: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """Counts of deadlines due within 30, 60 and 90 days (today included) and overdue."""
    days = [(date.fromisoformat(r["date"]) - today).days for r in rows]
    return {
        "overdue": sum(1 for d in days if d < 0),
        "windows": [{"days": w, "count": sum(1 for d in days if 0 <= d <= w)} for w in WINDOWS],
    }


def month_end(today: date, months: int) -> date:
    """The last day of the month `months - 1` after today's (months=1: the end of this month)."""
    year, month = divmod(today.month - 1 + months, 12)
    return date(today.year + year, month + 1, 1) - timedelta(days=1)


def calendar(org, today: date, months: int) -> dict[str, Any]:
    """The renewals calendar: every open deadline to the end of the `months`th month (this month first), overdue ones
    included, with counts per month (the overdue are counted in their own months, which may be before this one)."""
    until = month_end(today, months)
    rows = deadline_rows(org, today, until=until)
    per_month: dict[str, int] = defaultdict(int)
    for r in rows:
        per_month[r["date"][:7]] += 1
    start = today.replace(day=1)
    months_list = []
    for i in range(months):
        year, month = divmod(start.month - 1 + i, 12)
        key = f"{start.year + year:04d}-{month + 1:02d}"
        months_list.append({"month": key, "count": per_month.get(key, 0)})
    return {
        "months": months,
        "from": start.isoformat(),
        "to": until.isoformat(),
        "overdue": sum(1 for r in rows if r["timing"]["overdue"]),
        "by_month": months_list,
        "rows": rows,
    }


# ---- expiring credentials -------------------------------------------------------------------------------------------


def expiring_credentials(org, user, role: str, today: date, days: int = CREDENTIAL_DAYS) -> dict[str, Any]:
    """Credentials that expire within `days` (or expired in the last `days`), soonest first: API tokens that aren't
    revoked (all of the organization's for Admins, who manage them; one's own for others) and the
    client secrets and certificates discovery finds (kind `discovered_credential`, every role: the live set the
    credential expiry reminders read, discovery/credentials.live)."""
    from api.models import ApiToken
    from discovery import credentials as discovered

    rows: list[dict[str, Any]] = []
    if caps.API_TOKENS in caps.capabilities(org):
        qs = ApiToken.objects.filter(
            organization=org,
            revoked_at__isnull=True,
            expires_on__lte=today + timedelta(days=days),
            expires_on__gte=today - timedelta(days=days),
        ).select_related("user")
        if role != Role.ADMIN:
            qs = qs.filter(user=user)
        for token in qs.order_by("expires_on", "name", "id"):
            left = (token.expires_on - today).days
            rows.append(
                {
                    "kind": "api_token",
                    "id": str(token.id),
                    "name": token.name,
                    "prefix": token.prefix,
                    "owner": represent(UserBriefSerializer, token.user),
                    "expires_on": token.expires_on.isoformat(),
                    "days": left,
                    "expired": left < 0,
                    "credential_type": None,
                    "key_id": None,
                    "application": None,
                    "record": None,
                }
            )
    rows.extend(discovered.expiring_rows(org, today, days))
    rows.sort(key=lambda r: (r["expires_on"], r["name"].lower(), r["id"]))
    return {"days": days, "count": len(rows), "rows": rows}


# ---- the dashboard --------------------------------------------------------------------------------------------------


def needs_attention(org, user, today: date, *, renewal_states: list[Any] | None = None) -> dict[str, Any]:
    """The Needs attention summary: the badge counts and each queue's size per scope, from the same function the page,
    the badge and the digest read (notify/attention.py)."""
    from notify.attention import UNCOUNTED, attention_queues

    queues, counts = attention_queues(org, user, today, renewal_states=renewal_states)
    return {
        "counts": counts,
        "queues": [
            {
                "queue": name,
                "all": len(rows),
                "mine": sum(1 for r in rows if r["_flags"]["mine"]),
                "unassigned": sum(1 for r in rows if r["_flags"]["unassigned"]),
                "counted": name not in UNCOUNTED,
            }
            for name, rows in queues.items()
        ],
    }


def dashboard(org, membership, today: date) -> dict[str, Any]:
    """Every dashboard block. A block whose capability the organization lacks is null: contracts off means no spend
    and no licence utilisation, and the deadlines leave contracts out; data_quality off means no data-quality block."""
    from catalog import quality

    from . import licences, renewals

    has = caps.capabilities(org)
    user = membership.user
    # Needs attention and the deadlines both read every renewal's queue state: load them once (one query and the
    # deadline maths per renewal) rather than twice.
    states = renewals.queue_states(org, today)
    body: dict[str, Any] = {
        "as_of": today.isoformat(),
        "needs_attention": needs_attention(org, user, today, renewal_states=states),
        "deadlines": None,
        "expiring_credentials": expiring_credentials(org, user, membership.role, today),
        "spend": None,
        "license_utilization": None,
        "data_quality": quality.summary_json(org, today) if caps.DATA_QUALITY in has else None,
    }
    if caps.RENEWALS in has or caps.CONTRACTS in has:
        rows = deadline_rows(org, today, until=today + timedelta(days=max(WINDOWS)), states=states)
        body["deadlines"] = {**windows(rows, today), "rows": rows[:DASHBOARD_DEADLINES], "total": len(rows)}
    if caps.CONTRACTS in has:
        spend = load(org)
        body["spend"] = {
            "totals": spend.commitments,
            **{f"by_{by}": top(groups(by, spend.allocations)) for by in ("vendor", "department", "category")},
        }
        found = licences.totals(org)
        ranked = sorted(
            found["rows"],
            key=lambda r: (not r["overused"], -(r["utilization"] if r["utilization"] is not None else -1)),
        )
        body["license_utilization"] = {"total": found["total"], "rows": ranked[:DASHBOARD_TOP]}
    return body
