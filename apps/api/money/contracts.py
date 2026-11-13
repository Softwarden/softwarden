"""Contracts, contract lines and licence pools. Every write validates, scopes to the organization and writes its
AuditEvent in one transaction.

Money. An amount is a Decimal with at most two places and a three-letter currency code (ISO 4217's shape; the code
itself isn't checked against a list). Currencies are never converted. The annualised cost is derived from the amount
and the billing frequency (`annualize`) and stored on every write, so lists, filters and reports read a column.

Deadline. A contract's cancellation deadline is the renewal action deadline (money/renewal_rules.action_deadline) with
the contract's end date as the renewal date: the end date minus the notice period, month-end clamped (core/dates.py,
31 March minus one month is 28 or 29 February) when it auto-renews or that is unknown and a notice
period is set; else the end date minus the organization's renewal lead time, labelled Assumed (cancelling) or Renewal
action (it doesn't auto-renew). It is stored too, and re-derived when the organization's lead time changes.

One source of truth. A contract may point at one RenewalObligation. While it does, and it is the newest term
linked to that obligation (no linked successor), its end date, notice period and auto-renewal are written into the
obligation's open cycle and onto the obligation in the same transaction (`sync_obligation`, audited as cycle and
renewal changes), the cycle's entered deadline is cleared, and money/renewals.py refuses to change those inputs on the
renewal itself. The obligation's reminders are computed from its open cycle by the same function with the same inputs,
so they and the contract's deadline can't disagree (tests/test_contracts.py, "one source of truth"). Creating or
editing a contract can link an existing obligation or create one (`create_renewal_obligation`).

Allocation. A contract line's fixed `allocation_percent`s sum to at most 100 per contract. Every write that sets one
locks the contract row first (SELECT ... FOR UPDATE), so two concurrent edits are checked one after the other instead
of both passing against the same old total. `allocations` splits a contract's
annualised cost over its lines to the cent, so the rows always add up exactly.

Licence pools. Seats assigned may exceed seats purchased: overuse is a real state, shown and never refused. Only one
pool per SKU of an organization may sync seat counts from Entra, and only one with a SKU ID."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from catalog import customfields
from catalog.models import RECORD_KINDS, Kind, Person, Resource, Vendor
from catalog.services import NAME_MAX, TEXT_MAX, _in_org, _text, audit, is_uuid, org_today
from core import dates, limits
from core.input import whole_number
from core.problems import Conflict, FieldErrors

from . import renewal_rules
from .models import (
    AutoRenew,
    BillingFrequency,
    Contract,
    ContractItem,
    ContractStatus,
    LicensePool,
    RenewalCycle,
    RenewalObligation,
)

KIND = "contract"  # the custom fields kind (catalog/models.CUSTOM_FIELD_KINDS)
POOL_KIND = "license_pool"
CENT = Decimal("0.01")
ZERO = Decimal("0")
HUNDRED = Decimal("100")
AMOUNT_MAX = Decimal("999999999999.99")  # Decimal(14, 2)
NUMBER_MAX = 100  # contract and PO numbers
COST_CENTER_MAX = 50
SKU_ID_MAX = 64
SEATS_MAX = 10_000_000
CURRENCY = re.compile(r"^[A-Z]{3}$")
DEFAULT_CURRENCY = "USD"
DERIVED = ("annualized_cost", "cancellation_deadline", "predecessor", "last_synced")


# ---- money and dates: pure functions --------------------------------------------------------------------------------


def term_months(start: date | None, end: date | None) -> int | None:
    """A term's length in whole months, rounded (inclusive days ÷ 365.25 × 12), at least 12; None without
    both dates or when the end isn't after the start. 2025-01-01 to 2027-12-31 is 36."""
    if start is None or end is None or end <= start:
        return None
    months = round(Decimal((end - start).days + 1) / Decimal("365.25") * 12)
    return max(int(months), 12)


def annualize(amount, frequency: str, start: date | None = None, end: date | None = None) -> Decimal | None:
    """The annual cost of `amount` billed at `frequency`, to the cent (half up). Monthly × 12, quarterly × 4,
    every six months × 2, annual × 1; multi-year: the term's amount ÷ its years (term_months ÷ 12), or the whole amount
    when the dates are missing; one-time: counted once, in full; usage-based: the amount is the annual estimate."""
    if amount is None:
        return None
    value = Decimal(amount)
    if frequency == BillingFrequency.MONTHLY:
        value *= 12
    elif frequency == BillingFrequency.QUARTERLY:
        value *= 4
    elif frequency == BillingFrequency.SEMIANNUAL:
        value *= 2
    elif frequency == BillingFrequency.MULTI_YEAR:
        months = term_months(start, end)
        if months is not None:
            value = value / (Decimal(months) / 12)
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def annualization_note(amount, frequency: str, start: date | None, end: date | None) -> str:
    """How the annualised cost follows from the amount, in words."""
    if amount is None:
        return ""
    if frequency == BillingFrequency.MULTI_YEAR:
        months = term_months(start, end)
        if months is None:
            return "Term total, counted in full (add start and end dates to spread it)"
        return f"Term total ÷ {(Decimal(months) / 12).normalize():f} years"
    notes: dict[str, str] = {
        BillingFrequency.MONTHLY: "Monthly amount × 12",
        BillingFrequency.QUARTERLY: "Quarterly amount × 4",
        BillingFrequency.SEMIANNUAL: "Six-monthly amount × 2",
        BillingFrequency.ANNUAL: "Annual amount",
        BillingFrequency.ONE_TIME: "One-time amount, counted in full",
        BillingFrequency.USAGE_BASED: "Usage-based: the amount is your annual estimate",
    }
    return notes.get(frequency, "")


def deadline_of(auto_renew: str, end_date: date | None, notice_value, notice_unit: str, lead_days: int):
    """The contract's deadline: exactly the renewal action deadline of a cycle whose renewal date is the end date and
    that has no entered deadline (renewal_rules.Deadline: date, label, explanation)."""
    return renewal_rules.action_deadline(auto_renew, end_date, notice_value, notice_unit or "", None, lead_days)


def contract_deadline(contract: Contract, lead_days: int):
    return deadline_of(contract.auto_renew, contract.end_date, contract.notice_value, contract.notice_unit, lead_days)


def derive(contract: Contract, lead_days: int) -> None:
    """Set the stored derived values from the contract's own fields."""
    contract.annualized_cost = annualize(
        contract.amount, contract.billing_frequency, contract.start_date, contract.end_date
    )
    contract.cancellation_deadline = contract_deadline(contract, lead_days).date


def _split_cents(cost: Decimal, shares: list[Decimal]) -> list[Decimal]:
    """Amounts for percentage `shares` that sum exactly to `cost` (largest remainder; ties go to the earlier row, so
    the result is deterministic)."""
    exact = [cost * share / HUNDRED for share in shares]
    floors = [(e / CENT).to_integral_value(rounding=ROUND_FLOOR) * CENT for e in exact]
    leftover = int(((cost - sum(floors, ZERO)) / CENT).to_integral_value())
    order = sorted(range(len(shares)), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in order[: max(leftover, 0)]:
        floors[i] += CENT
    return floors


@dataclass(frozen=True)
class Allocation:
    contract: Contract
    item: ContractItem | None  # None: the unallocated remainder (fixed shares under 100 and no floating line)
    share: Decimal  # percent of the annualised cost
    amount: Decimal  # to the cent; a contract's rows sum exactly to its annualised cost


def allocations(
    contracts: Iterable[Contract], items: dict[Any, list[ContractItem]] | None = None
) -> Iterator[Allocation]:
    """A contract's annualised cost split over its lines: by `allocation_percent` where
    set, the rest shared evenly by lines without one. A contract with no lines is one unallocated row; fixed shares
    under 100 with no floating line leave an unallocated remainder row. Older data with fixed shares over 100 (refused
    on every write) is scaled down to 100 rather than inflating totals. `items` ({contract id: lines}) saves a query
    per contract."""
    for contract in contracts:
        lines = items.get(contract.id, []) if items is not None else list(contract.items.order_by("created_at", "id"))
        for index, share, amount in split(contract.annualized_cost, [i.allocation_percent for i in lines]):
            yield Allocation(contract, lines[index] if index is not None else None, share, amount)


def split(annualized_cost: Decimal | None, percents: list[Decimal | None]) -> list[tuple[int | None, Decimal, Decimal]]:
    """The allocation maths on plain values, for `allocations` and the spend reports (money/reporting.py), which read
    columns rather than model instances: one (line index, share, amount) per line in order, then (None, share, amount)
    for an unallocated remainder. `percents` are the lines' `allocation_percent` values (None: shares the rest)."""
    cost = (annualized_cost or ZERO).quantize(CENT)
    if not percents:
        return [(None, HUNDRED, cost)]
    fixed_total = sum((p for p in percents if p is not None), ZERO)
    floating = sum(1 for p in percents if p is None)
    scale = HUNDRED / fixed_total if fixed_total > HUNDRED else Decimal(1)
    remainder = max(HUNDRED - fixed_total, ZERO)
    each = remainder / floating if floating else ZERO
    rows: list[tuple[int | None, Decimal]] = [(i, p * scale if p is not None else each) for i, p in enumerate(percents)]
    if not floating and remainder > 0:
        rows.append((None, remainder))
    amounts = _split_cents(cost, [s for _i, s in rows])
    return [(i, share, amount) for (i, share), amount in zip(rows, amounts, strict=True)]


# ---- validation -----------------------------------------------------------------------------------------------------


def _date(data, field, errors) -> date | None:
    raw = data.get(field)
    if raw in (None, ""):
        return None
    try:
        return dates.parse_stored(raw)
    except ValueError as exc:
        errors[field] = [str(exc)]
    return None


def _short(data, field, limit, errors) -> str:
    raw = data.get(field)
    if raw is not None and not isinstance(raw, str):
        errors[field] = ["Enter text."]
        return ""
    value = _text(raw)
    if len(value) > limit:
        errors[field] = [f"Use {limit} characters or fewer."]
    return value


def decimal_value(raw, field, errors, *, maximum: Decimal, label: str) -> Decimal | None:
    """A non-negative number with at most two decimal places, from a JSON number or a string; None for null or ""."""
    if raw in (None, ""):
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        errors[field] = [f"Enter the {label} as a number, such as 1200.50."]
        return None
    try:
        value = Decimal(str(raw).strip().replace(",", ""))
    except InvalidOperation:
        errors[field] = [f"Enter the {label} as a number, such as 1200.50."]
        return None
    if not value.is_finite():
        errors[field] = [f"Enter the {label} as a number, such as 1200.50."]
        return None
    if value == 0:
        value = Decimal(0)  # "-0" is zero
    # Bounds before quantize: quantize raises InvalidOperation past the context's 28 digits ("1e30").
    if value < 0:
        errors[field] = [f"The {label} can't be negative."]
    elif value > maximum:
        errors[field] = [f"Enter a {label} no larger than {maximum:,}."]
    elif value != value.quantize(CENT):
        errors[field] = [f"Use at most two decimal places for the {label}."]
    else:
        return value.quantize(CENT)
    return None


def _choice(data, field, choices, errors, default=None):
    value = data.get(field, default)
    if value not in choices.values:
        errors[field] = [f"Choose one of: {', '.join(choices.values)}."]
        return default
    return value


def _period(data, errors, values) -> None:
    from .renewals import _period as renewal_period

    renewal_period(data, "notice", "notice period", errors, values)


def _contract_fields(org, data, contract: Contract | None) -> tuple[dict[str, Any], dict[str, list[str]]]:
    creating = contract is None
    errors: dict[str, list[str]] = {}
    values: dict[str, Any] = {}
    for field in DERIVED:
        if field in data:
            errors[field] = ["This is worked out from the contract's other fields; it can't be set."]
    if "name" in data or creating:
        name = _text(data.get("name")) if isinstance(data.get("name"), str) else ""
        if not name:
            errors["name"] = ["Enter a name."]
        elif len(name) > NAME_MAX:
            errors["name"] = [f"Use {NAME_MAX} characters or fewer."]
        values["name"] = name
    if "vendor" in data:
        values["vendor"] = _in_org(Vendor, org, data.get("vendor"), "vendor", errors)
    if "renewal_owner" in data:
        values["renewal_owner"] = _in_org(Person, org, data.get("renewal_owner"), "renewal_owner", errors, True)
        if "renewal_owner" in errors and errors["renewal_owner"][0].startswith("Inactive"):
            errors["renewal_owner"] = ["Choose an active person."]
    for field, limit in (("contract_number", NUMBER_MAX), ("po_number", NUMBER_MAX), ("cost_center", COST_CENTER_MAX)):
        if field in data:
            values[field] = _short(data, field, limit, errors)
    if "notes" in data:
        values["notes"] = _short(data, "notes", TEXT_MAX, errors)
    if "amount" in data:
        values["amount"] = decimal_value(data.get("amount"), "amount", errors, maximum=AMOUNT_MAX, label="amount")
    if "currency" in data or creating:
        raw = data.get("currency")
        currency = raw.strip().upper() if isinstance(raw, str) and raw.strip() else DEFAULT_CURRENCY
        if raw not in (None, "") and not (isinstance(raw, str) and CURRENCY.match(currency)):
            errors["currency"] = ["Enter a three-letter currency code, such as USD or EUR."]
        values["currency"] = currency
    if "billing_frequency" in data or creating:
        values["billing_frequency"] = _choice(data, "billing_frequency", BillingFrequency, errors, "annual")
    if "status" in data or creating:
        values["status"] = _choice(data, "status", ContractStatus, errors, ContractStatus.ACTIVE)
    if "auto_renew" in data or creating:
        values["auto_renew"] = _choice(data, "auto_renew", AutoRenew, errors, AutoRenew.UNKNOWN)
    for field in ("start_date", "end_date"):
        if field in data:
            values[field] = _date(data, field, errors)
    _period(data, errors, values)
    start = values.get("start_date", contract.start_date if contract else None)
    end = values.get("end_date", contract.end_date if contract else None)
    if start and end and end < start and "start_date" not in errors and "end_date" not in errors:
        errors["end_date"] = ["The end date is before the start date."]
    return values, errors


# ---- the obligation link ----------------------------------------------------------------------------------------------

LINK, CREATE = "renewal_obligation", "create_renewal_obligation"
NEEDS_END = "Add the contract's end date first: the renewal's dates come from it."


@dataclass(frozen=True)
class LinkChange:
    create: bool = False
    obligation: RenewalObligation | None = None
    changes: bool = False  # False: the request didn't touch the link


def chain_of(contract: Contract) -> set[Any]:
    """The IDs of the terms this one renews, oldest included (its predecessor, theirs, ...)."""
    ids: set[Any] = set()
    node_id = contract.predecessor_id
    while node_id is not None and node_id not in ids:
        ids.add(node_id)
        node_id = Contract.objects.filter(id=node_id).values_list("predecessor_id", flat=True).first()
    return ids


# A contract that is archived, cancelled, expired or renewed keeps its link (history) but no longer drives its renewal.
# Renewed: its term is over and a next term took over, so if that next term stops counting the renewal keeps the next
# term's last dates (and is editable again) instead of falling back to this term's older end date.
NOT_DRIVING = (ContractStatus.CANCELLED, ContractStatus.EXPIRED, ContractStatus.RENEWED)


def counting_successors():
    """Next terms that count as having renewed their predecessor: not cancelled and not archived (an expired or
    renewed next term still renewed it). Used by the term-status job (money/terms.py) and the "renewed by" guard."""
    return Contract.objects.filter(archived_at__isnull=True).exclude(status=ContractStatus.CANCELLED)


def can_drive(contract: Contract) -> bool:
    return contract.archived_at is None and contract.status not in NOT_DRIVING


def _newest(linked: list[Contract]) -> Contract | None:
    """Of the contracts linked to one obligation, the one that drives it: among those that can drive, the one no other
    such contract renews (the newest term). An archived, cancelled or expired successor hands it back to its
    predecessor while that one still counts (not yet Renewed); None when none can drive."""
    eligible = [c for c in linked if can_drive(c)]
    renewed = {c.predecessor_id for c in eligible if c.predecessor_id is not None}
    newest = [c for c in eligible if c.id not in renewed]
    # At most one: every way two unrelated contracts could both be able to drive one renewal is refused (linking while
    # another drives, in _link_fields; restoring or re-activating one while another drives, in takeover_error). The
    # tie-break only keeps the answer deterministic if older data breaks that.
    return min(newest, key=lambda c: (c.created_at, str(c.id))) if newest else None


def driver(obligation: RenewalObligation | Any) -> Contract | None:
    """The contract whose dates an obligation's open cycle takes (`_newest` of its linked contracts), or None."""
    return _newest(list(Contract.objects.filter(renewal_obligation_id=getattr(obligation, "pk", obligation))))


def drivers(org, obligation_ids: Iterable[Any]) -> dict[Any, Contract]:
    """{obligation id: its driving contract} for many obligations, in one query."""
    linked = Contract.objects.filter(organization=org, renewal_obligation_id__in=list(obligation_ids)).only(
        "id", "name", "renewal_obligation_id", "predecessor_id", "created_at", "archived_at", "status"
    )
    grouped: dict[Any, list[Contract]] = {}
    for c in linked:
        grouped.setdefault(c.renewal_obligation_id, []).append(c)
    found: dict[Any, Contract] = {}
    for obligation_id, contracts in grouped.items():
        newest = _newest(contracts)
        if newest is not None:
            found[obligation_id] = newest
    return found


def same_chain(contract: Contract, other: Contract) -> bool:
    """True when the two are the same contract or terms of one agreement: one renews the other, directly or through
    the terms between them. The one test for "outside its own chain of terms", used both when linking a renewal
    (`_link_fields`) and when a contract counts again (`takeover_error`), so the two rules can't drift apart."""
    return contract.id == other.id or other.id in chain_of(contract) or contract.id in chain_of(other)


def takeover_error(contract: Contract) -> str | None:
    """Why `contract`, about to count again (restored, or set back from cancelled, expired or renewed), may not: its
    renewal is now driven by a contract outside its own chain of terms (one it renews, or one that renews it), linked
    while it didn't count. Reviving it would silently take the renewal's dates back. Call with the obligation locked."""
    if contract.renewal_obligation_id is None:
        return None
    current = driver(contract.renewal_obligation_id)
    if current is None or same_chain(contract, current):
        return None
    return f"This renewal now takes its dates from the contract {current.name}. Unlink one of them first."


def lock_obligations(org, *ids) -> None:
    """Lock these obligations (those of `org` among the ids; None and non-UUIDs skipped) in id order, so two writes
    re-pointing contracts between the same two renewals in opposite directions wait instead of deadlocking."""
    wanted = sorted({str(i) for i in ids if i is not None and is_uuid(str(i))})
    if wanted:
        list(
            RenewalObligation.objects.select_for_update()
            .filter(organization=org, id__in=wanted)
            .order_by("id")
            .values_list("id", flat=True)
        )


def _link_fields(org, data, contract: Contract | None, end_date: date | None, errors) -> LinkChange:
    create = data.get(CREATE)
    if create not in (None, True, False):
        errors[CREATE] = ["Use true or false."]
        return LinkChange()
    if create and data.get(LINK) not in (None, ""):
        errors[CREATE] = ["Either link an existing renewal or create one, not both."]
        return LinkChange()
    if create:
        if contract is not None and contract.renewal_obligation_id is not None:
            errors[CREATE] = ["This contract already has a renewal."]
        elif end_date is None:
            errors[CREATE] = [NEEDS_END]
        return LinkChange(create=True, changes=True)
    if LINK not in data:
        return LinkChange()
    raw = data.get(LINK)
    if raw in (None, ""):
        return LinkChange(obligation=None, changes=True)
    obligation = _in_org(RenewalObligation, org, raw, LINK, errors)
    if obligation is None:
        return LinkChange()
    if contract is not None and contract.renewal_obligation_id == obligation.id:
        return LinkChange(obligation=obligation, changes=True)
    if end_date is None:
        errors[LINK] = [NEEDS_END]
    elif obligation.archived_at is not None:
        errors[LINK] = ["This renewal is archived. Restore it first."]
    elif not RenewalCycle.objects.filter(obligation=obligation, completed_on__isnull=True).exists():
        errors[LINK] = ["This renewal has no open cycle. Establish its next cycle first."]
    else:
        # Locked until the write commits, so two contracts linking at once are checked one after the other.
        RenewalObligation.objects.select_for_update().filter(id=obligation.id).first()
        current = driver(obligation)
        # Allowed when the driver is a term of this contract's own agreement, either way round (`same_chain`, as
        # takeover_error): an earlier term this one renews (this one then drives), or a later term that renews this
        # one (it keeps driving; the link records which renewal this term belonged to).
        if current is not None and (contract is None or not same_chain(contract, current)):
            errors[LINK] = [f"This renewal already takes its dates from the contract {current.name}."]
    return LinkChange(obligation=obligation, changes=True)


def _create_obligation(org, actor, contract: Contract, note: dict[str, Any] | None = None) -> RenewalObligation:
    """A renewal for the contract, through money/renewals.create_renewal (audited there, with `note`), covering the
    contract's applications; its first cycle takes the contract's dates."""
    from . import renewals

    applications = list(
        ContractItem.objects.filter(contract=contract, resource__kind=Kind.APPLICATION)
        .order_by("created_at", "id")
        .values_list("resource_id", flat=True)
    )
    data: dict[str, Any] = {
        "name": contract.name,
        "vendor": str(contract.vendor_id) if contract.vendor_id else None,
        "auto_renew": contract.auto_renew,
        "renewal_date": contract.end_date.isoformat() if contract.end_date else None,
        "applications": [str(a) for a in applications],
    }
    if contract.renewal_owner_id is not None and Person.objects.filter(id=contract.renewal_owner_id, active=True):
        data["responsible"] = str(contract.renewal_owner_id)
    if contract.notice_value is not None:
        data["notice_value"], data["notice_unit"] = contract.notice_value, contract.notice_unit
    return renewals.create_renewal(org, actor, data, note=note)


def sync_obligation(org, actor, obligation_id: Any, note: dict[str, Any] | None = None) -> None:
    """Write the obligation's driving contract's deadline inputs into its open cycle and its auto-renewal onto the
    obligation. Called after every write that can change what drives it: a contract created, edited (dates, status,
    link), archived, restored or deleted, for the obligation it points at now and the one it pointed at before. Each
    change is audited on the renewal (with the contract's ID), and a changed deadline input resets the cycle's
    deadline_set_on, as an edit on the renewal does (reminder thresholds already passed today don't fire). With no
    driver left the renewal keeps the dates it has, and can be edited again. `note` adds context to the events (the
    import names its run)."""
    from .renewals import _diff, cycle_audit_values, obligation_audit_values

    if obligation_id is None:
        return
    obligation = RenewalObligation.objects.select_for_update().filter(id=obligation_id).first()
    if obligation is None:
        return
    contract = driver(obligation)
    if contract is None:
        return
    if obligation.auto_renew != contract.auto_renew:
        before = obligation_audit_values(obligation)
        obligation.auto_renew = contract.auto_renew
        obligation.save()
        RenewalCycle.objects.filter(obligation=obligation, completed_on__isnull=True).update(
            deadline_set_on=org_today(org)
        )
        audit(
            org,
            actor,
            "renewal.updated",
            obligation,
            {**_diff(before, obligation_audit_values(obligation)), "contract": str(contract.id), **(note or {})},
        )
    cycle = RenewalCycle.objects.filter(obligation=obligation, completed_on__isnull=True).first()
    if cycle is None:  # completed and the next not established yet: it takes the dates when it is (renewals.py)
        return
    wanted = {
        "renewal_date": contract.end_date,
        "notice_value": contract.notice_value,
        "notice_unit": contract.notice_unit or "",
        "explicit_deadline": None,
    }
    if all(getattr(cycle, k) == v for k, v in wanted.items()):
        return
    before = cycle_audit_values(cycle)
    for key, value in wanted.items():
        setattr(cycle, key, value)
    cycle.deadline_set_on = org_today(org)
    cycle.save()
    audit(
        org,
        actor,
        "renewal_cycle.updated",
        cycle,
        {
            "cycle": str(cycle.id),
            **_diff(before, cycle_audit_values(cycle)),
            "contract": str(contract.id),
            **(note or {}),
        },
    )


def _apply_link(
    org, actor, contract: Contract, link: LinkChange, previous: Any = None, note: dict[str, Any] | None = None
) -> None:
    if link.create:
        obligation = _create_obligation(org, actor, contract, note)
        contract.renewal_obligation = obligation
        contract.save(update_fields=["renewal_obligation", "updated_at"])
        audit(
            org,
            actor,
            "contract.updated",
            contract,
            {"renewal_obligation": [None, str(obligation.id)], **(note or {})},
        )
    resync(org, actor, previous, contract.renewal_obligation_id, note=note)


def resync(org, actor, *obligation_ids, note: dict[str, Any] | None = None) -> None:
    """sync_obligation for each distinct obligation (the one a contract pointed at before a write and the one it
    points at now), so a renewal a contract left falls back to its new driver."""
    for obligation_id in dict.fromkeys(i for i in obligation_ids if i is not None):
        sync_obligation(org, actor, obligation_id, note)


# ---- contracts --------------------------------------------------------------------------------------------------------


def _id(value) -> str | None:
    return str(value) if value else None


def _money(value) -> str | None:
    return None if value is None else str(value)


def audit_values(org, contract: Contract) -> dict[str, Any]:
    """A contract's fields in audit payloads (references as IDs; money as strings)."""
    return {
        "name": contract.name,
        "vendor": _id(contract.vendor_id),
        "contract_number": contract.contract_number,
        "po_number": contract.po_number,
        "amount": _money(contract.amount),
        "currency": contract.currency,
        "billing_frequency": contract.billing_frequency,
        "annualized_cost": _money(contract.annualized_cost),
        "start_date": contract.start_date.isoformat() if contract.start_date else None,
        "end_date": contract.end_date.isoformat() if contract.end_date else None,
        "auto_renew": contract.auto_renew,
        "notice": [contract.notice_value, contract.notice_unit] if contract.notice_value else None,
        "renewal_owner": _id(contract.renewal_owner_id),
        "status": contract.status,
        "cost_center": contract.cost_center,
        "notes": contract.notes,
        "renewal_obligation": _id(contract.renewal_obligation_id),
        **customfields.audit_values(org, KIND, contract.custom_field_data),
    }


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[Any]]:
    return {k: [before.get(k), v] for k, v in after.items() if before.get(k) != v}


@transaction.atomic
def create_contract(org, actor, data, note: dict[str, Any] | None = None) -> Contract:
    """`note` adds context to the contract.created event (the import names its run)."""
    values, errors = _contract_fields(org, data, None)
    custom = customfields.clean(org, KIND, None, data, errors, creating=True)
    link = _link_fields(org, data, None, values.get("end_date"), errors)
    if errors:
        raise FieldErrors(errors)
    contract = Contract(organization=org, created_by=actor, custom_field_data=custom or {}, **values)
    if link.obligation is not None:
        contract.renewal_obligation = link.obligation
    derive(contract, org.renewal_lead_days)
    limits.check_room(org, limits.OTHER_RECORDS)
    contract.save()
    audit(org, actor, "contract.created", contract, {**audit_values(org, contract), **(note or {})})
    _apply_link(org, actor, contract, link, note=note)
    return contract


def lock(contract: Contract) -> Contract:
    """The contract as stored now, locked until the transaction ends (allocation checks and edits serialise here)."""
    return Contract.objects.select_for_update().get(id=contract.id)


@transaction.atomic
def update_contract(org, actor, contract: Contract, data, note: dict[str, Any] | None = None) -> Contract:
    """`note` adds context to the contract.updated event (the import names its run)."""
    contract = lock(contract)
    lock_obligations(org, contract.renewal_obligation_id, data.get(LINK) if isinstance(data.get(LINK), str) else None)
    values, errors = _contract_fields(org, data, contract)
    custom = customfields.clean(org, KIND, contract.custom_field_data, data, errors)
    end = values.get("end_date", contract.end_date)
    link = _link_fields(org, data, contract, end, errors)
    previous = contract.renewal_obligation_id
    # Resending the link it already has (a form sends every field back) is not a re-link.
    relinking = link.changes and not link.create and (link.obligation.id if link.obligation else None) != previous
    target = (link.obligation.id if link.obligation else None) if relinking else previous
    if end is None and target is not None and "end_date" not in errors and LINK not in errors:
        # Linked, driving or not: a renewal that falls back to this contract must find an end date on it.
        errors["end_date"] = [NEEDS_END]
    status = values.get("status", contract.status)
    reviving = contract.status in NOT_DRIVING and status not in NOT_DRIVING and contract.archived_at is None
    ended = any(d is not None and d < org_today(org) for d in (contract.end_date, end))
    if reviving and "status" not in errors:
        # A term with a counting next term may come back while its term hasn't ended (renewed early, or cancelled by
        # mistake) and that next term still drives. It comes back only when nothing renews it any more once it has
        # ended (by its stored or its new end date, organization time), when it comes straight from Renewed (so
        # Renewed -> Expired -> Active in two edits can't get round the guard), or when the next term can't drive
        # (set to Expired, say): a revived old term must never drive while a next term renews it.
        found = (
            counting_successors()
            .filter(organization_id=contract.organization_id, predecessor_id=contract.id)
            .values_list("name", "status")
            .first()
        )
        if found is not None:
            successor, successor_status = found
            if ended or contract.status == ContractStatus.RENEWED or successor_status in NOT_DRIVING:
                errors["status"] = [f"This term was renewed by {successor}. Edit {successor} instead."]
    if reviving and not relinking and "status" not in errors:
        message = takeover_error(contract)
        if message:
            errors["status"] = [message]
    if errors:
        raise FieldErrors(errors)
    before = audit_values(org, contract)
    for field, value in values.items():
        setattr(contract, field, value)
    if custom is not None:
        contract.custom_field_data = custom
    if link.changes and not link.create:
        contract.renewal_obligation = link.obligation
    derive(contract, org.renewal_lead_days)
    contract.save()
    changes = _diff(before, audit_values(org, contract))
    if changes:
        audit(org, actor, "contract.updated", contract, {**changes, **(note or {})})
    _apply_link(org, actor, contract, link, previous, note)
    return contract


@transaction.atomic
def archive_contract(org, actor, contract: Contract) -> Contract:
    if contract.archived_at is None:
        contract.archived_at = timezone.now()
        contract.save(update_fields=["archived_at", "updated_at"])
        audit(org, actor, "contract.archived", contract, {"name": contract.name})
        resync(org, actor, contract.renewal_obligation_id)  # an archived contract no longer drives its renewal
    return contract


@transaction.atomic
def restore_contract(org, actor, contract: Contract) -> Contract:
    contract = lock(contract)
    lock_obligations(org, contract.renewal_obligation_id)
    if contract.archived_at is not None:
        if can_drive(Contract(status=contract.status, archived_at=None)):
            message = takeover_error(contract)
            if message:
                raise Conflict(message)
        contract.archived_at = None
        contract.save(update_fields=["archived_at", "updated_at"])
        audit(org, actor, "contract.restored", contract, {"name": contract.name})
        resync(org, actor, contract.renewal_obligation_id)
    return contract


def contract_references(contract: Contract) -> dict[str, int]:
    """What blocks a contract's delete: licence pools bought under it and a next term that renews it. Its lines and
    attachments go with it."""
    refs: dict[str, int] = {}
    pools = LicensePool.objects.filter(contract=contract).count()
    if pools:
        refs["license pool"] = pools
    if Contract.objects.filter(predecessor=contract).exists():
        refs["next term"] = 1
    return refs


@transaction.atomic
def delete_contract(org, actor, contract: Contract) -> None:
    """Hard delete (Admin): refused while a licence pool or a next term points at it (archive it instead). Its lines
    go with it (counted in the event) and its attachments too (each audited, files retired). A linked renewal stays:
    it takes the dates of its next driver (the term this one renewed, if linked and still counting), or keeps the ones
    it had. A Renewed term this one renewed doesn't drive; the term-status job (money/terms.py) then moves it to
    Expired once its end date has passed."""
    from catalog import attachments
    from catalog.services import refuse_while_referenced

    contract = lock(contract)
    refuse_while_referenced(contract, contract_references(contract), "Archive it instead.")
    attachments.remove_for(org, actor, contract)
    lines = ContractItem.objects.filter(contract=contract).count()
    audit(org, actor, "contract.deleted", contract, {"name": contract.name, "lines": lines})
    previous = contract.renewal_obligation_id
    contract.delete()
    resync(org, actor, previous)  # the renewal falls back to the term this one renewed, if linked and not Renewed


def refresh_deadlines(org, live_only: bool = False) -> int:
    """Re-derive contracts' stored deadlines: every contract's after the organization's renewal lead time changed
    (only Assumed and Renewal action deadlines depend on it), and, from the hourly term-status job (money/terms.py,
    `live_only`), those of unarchived Signed, Active and Pending renewal terms, so a stored date that drifted from its
    computation is put right instead of being warned about on every report. A derived column, not an edit: no event
    per contract (the organization.updated event records the lead time). Returns how many changed."""
    from .models import OBLIGATION_STATUSES

    changed = 0
    rows = Contract.objects.filter(organization=org)
    if live_only:
        rows = rows.filter(archived_at__isnull=True, status__in=OBLIGATION_STATUSES)
    rows = rows.only("id", "auto_renew", "end_date", "notice_value", "notice_unit", "cancellation_deadline")
    for c in rows:
        wanted = contract_deadline(c, org.renewal_lead_days).date
        if wanted != c.cancellation_deadline:
            # Only if the inputs read are still the stored ones: a contract edited meanwhile was derived by its own
            # write (update_contract), and this stale value must not overwrite it.
            changed += Contract.objects.filter(
                id=c.id,
                auto_renew=c.auto_renew,
                end_date=c.end_date,
                notice_value=c.notice_value,
                notice_unit=c.notice_unit,
            ).update(cancellation_deadline=wanted)
    return changed


# ---- contract lines -----------------------------------------------------------------------------------------------------

ITEM_KINDS = tuple(str(k) for k in RECORD_KINDS)


def _percent(data, errors) -> Decimal | None:
    return decimal_value(
        data.get("allocation_percent"), "allocation_percent", errors, maximum=HUNDRED, label="allocation"
    )


def allocated(contract: Contract, exclude: Any = None) -> Decimal:
    """The sum of the contract's fixed allocations, less line `exclude`."""
    qs = ContractItem.objects.filter(contract=contract)
    if exclude is not None:
        qs = qs.exclude(id=exclude)
    return qs.aggregate(total=Sum("allocation_percent"))["total"] or ZERO


def allocation_error(others: Decimal, percent: Decimal | None) -> str | None:
    """Why `percent` would push the contract's fixed allocations over 100, or None."""
    if percent is None:
        return None
    total = others + percent
    if total > HUNDRED:
        return (
            f"Fixed allocations would total {total.normalize():f}% (max 100%); {others.normalize():f}% is already "
            "allocated to other lines."
        )
    return None


def item_audit_values(item: ContractItem) -> dict[str, Any]:
    return {
        "contract": str(item.contract_id),
        "record": str(item.resource_id),
        "allocation_percent": _money(item.allocation_percent),
    }


def _line_refs(item: ContractItem) -> dict[str, str]:
    """The contract and record a line event names, so it is listed in both Histories (catalog/audit_log.py)."""
    return {"contract": str(item.contract_id), "record": str(item.resource_id)}


def _record(org, raw, errors) -> Resource | None:
    found = (
        Resource.objects.filter(organization=org, id=raw, kind__in=ITEM_KINDS).first()
        if isinstance(raw, str) and is_uuid(raw)
        else None
    )
    if found is None:
        errors["record"] = ["Choose an application, component or AI agent."]
    return found


def _touch(contract: Contract) -> None:
    """A line change moves the contract's updated_at (its ETag covers its lines)."""
    Contract.objects.filter(id=contract.id).update(updated_at=timezone.now())


@transaction.atomic
def add_item(org, actor, contract: Contract, data) -> ContractItem:
    """A record on the contract, with an optional fixed allocation. One line per record."""
    contract = lock(contract)
    errors: dict[str, list[str]] = {}
    record = _record(org, data.get("record"), errors)
    percent = _percent(data, errors)
    if record is not None and ContractItem.objects.filter(contract=contract, resource=record).exists():
        errors["record"] = [f"{record.name} is already on this contract."]
    if "allocation_percent" not in errors:
        message = allocation_error(allocated(contract), percent)
        if message:
            errors["allocation_percent"] = [message]
    if errors or record is None:
        raise FieldErrors(errors)
    limits.check_room(org, limits.OTHER_RECORDS)
    item = ContractItem.objects.create(organization=org, contract=contract, resource=record, allocation_percent=percent)
    _touch(contract)
    audit(org, actor, "contract_item.created", item, {**item_audit_values(item), "name": record.name})
    return item


@transaction.atomic
def update_item(org, actor, item: ContractItem, data) -> ContractItem:
    """A line's allocation (null: shares the rest evenly). The record of a line never changes: remove it and add
    another."""
    contract = lock(item.contract)
    item = ContractItem.objects.select_related("resource").get(id=item.id)
    errors: dict[str, list[str]] = {}
    for key in data:
        if key != "allocation_percent":
            errors[key] = ["Only the allocation of a line can change."]
    percent = _percent(data, errors) if "allocation_percent" in data else item.allocation_percent
    if "allocation_percent" not in errors:
        message = allocation_error(allocated(contract, exclude=item.id), percent)
        if message:
            errors["allocation_percent"] = [message]
    if errors:
        raise FieldErrors(errors)
    if percent != item.allocation_percent:
        before = _money(item.allocation_percent)
        item.allocation_percent = percent
        item.save()
        _touch(contract)
        audit(
            org,
            actor,
            "contract_item.updated",
            item,
            {**_line_refs(item), "name": item.resource.name, "allocation_percent": [before, _money(percent)]},
        )
    return item


@transaction.atomic
def remove_item(org, actor, item: ContractItem) -> None:
    contract = lock(item.contract)
    audit(org, actor, "contract_item.deleted", item, {**item_audit_values(item), "name": item.resource.name})
    item.delete()
    _touch(contract)


@transaction.atomic
def set_items(
    org,
    actor,
    contract: Contract,
    lines: list[tuple[Resource, Decimal | None]],
    note: dict[str, Any] | None = None,
) -> list[ContractItem]:
    """Make the contract's lines exactly `lines` (record, percent), as one change: the import's "Name=40; Name"
    column (imports/money_import.py). Only the final total is checked, so swapping two lines' shares doesn't trip over
    its own intermediate state, and the rows never
    pass through one either: lines are removed first, then shares that go down (or become shared) are lowered, and
    only then are shares raised and lines added, so the stored fixed total never exceeds the larger of the old and the
    new one. Each added, changed or removed line is audited. The caller must pass records
    already scoped to the organization and of ITEM_KINDS, which is asserted here. `note` adds context to the line
    events (the import names its run)."""
    if not all(r.organization_id == org.id and r.kind in ITEM_KINDS for r, _p in lines):
        raise ValueError("set_items takes the organization's applications, components and AI agents only.")
    contract = lock(contract)
    by_record = {r.id: (r, p) for r, p in lines}
    if len(by_record) != len(lines):
        raise FieldErrors({"items": ["A record can be on a contract once."]})
    total = sum((p for _r, p in by_record.values() if p is not None), ZERO)
    if total > HUNDRED:
        raise FieldErrors({"items": [f"Fixed allocations would total {total.normalize():f}% (max 100%)."]})
    extra = note or {}
    current = {i.resource_id: i for i in ContractItem.objects.filter(contract=contract).select_related("resource")}
    for rid, item in current.items():
        if rid not in by_record:
            payload = {**item_audit_values(item), "name": item.resource.name, **extra}
            audit(org, actor, "contract_item.deleted", item, payload)
            item.delete()
    # After the removals, so replacing lines at the limit isn't refused (the import, a renewal's successor
    # and the seed all add lines here).
    limits.check_room(org, limits.OTHER_RECORDS, sum(1 for rid in by_record if rid not in current))

    def lowers(item: ContractItem, percent: Decimal | None) -> bool:
        return (item.allocation_percent or ZERO) > (percent or ZERO)

    def change(item: ContractItem, record: Resource, percent: Decimal | None) -> None:
        before = _money(item.allocation_percent)
        item.allocation_percent = percent
        item.save()
        audit(
            org,
            actor,
            "contract_item.updated",
            item,
            {**_line_refs(item), "name": record.name, "allocation_percent": [before, _money(percent)], **extra},
        )

    for record, percent in by_record.values():  # shares going down first
        found = current.get(record.id)
        if found is not None and found.allocation_percent != percent and lowers(found, percent):
            change(found, record, percent)
    kept = []
    for record, percent in by_record.values():
        found = current.get(record.id)
        if found is None:
            item = ContractItem.objects.create(
                organization=org, contract=contract, resource=record, allocation_percent=percent
            )
            audit(org, actor, "contract_item.created", item, {**item_audit_values(item), "name": record.name, **extra})
        else:
            item = found
            if item.allocation_percent != percent:
                change(item, record, percent)
        kept.append(item)
    _touch(contract)
    return kept


# ---- licence pools --------------------------------------------------------------------------------------------------------


def _seats(data, field, errors) -> int:
    raw = data.get(field)
    if raw in (None, ""):
        return 0
    value = whole_number(raw)
    if value is None:
        errors[field] = ["Enter a whole number of seats."]
        return 0
    if not 0 <= value <= SEATS_MAX:
        errors[field] = [f"Enter a number of seats from 0 to {SEATS_MAX:,}."]
    return value


def _pool_fields(org, data, pool: LicensePool | None) -> tuple[dict[str, Any], dict[str, list[str]]]:
    creating = pool is None
    errors: dict[str, list[str]] = {}
    values: dict[str, Any] = {}
    if "last_synced" in data:
        errors["last_synced"] = ["Set by discovery when it syncs the seat counts; it can't be set here."]
    if "application" in data or creating:
        raw = data.get("application")
        app = (
            Resource.objects.filter(organization=org, kind=Kind.APPLICATION, id=raw).first()
            if isinstance(raw, str) and is_uuid(raw)
            else None
        )
        if app is None:
            errors["application"] = ["Choose an application."]
        values["application"] = app
    if "contract" in data:
        values["contract"] = _in_org(Contract, org, data.get("contract"), "contract", errors)
    if "sku_name" in data or creating:
        name = _short(data, "sku_name", NAME_MAX, errors)
        if not name and "sku_name" not in errors:
            errors["sku_name"] = ["Enter the SKU or plan name, such as Microsoft 365 E5."]
        values["sku_name"] = name
    if "sku_id" in data:
        values["sku_id"] = _short(data, "sku_id", SKU_ID_MAX, errors)
    for field in ("seats_purchased", "seats_assigned"):
        if field in data:
            values[field] = _seats(data, field, errors)
    if "seat_sync" in data:
        raw = data.get("seat_sync")
        if not isinstance(raw, bool):
            errors["seat_sync"] = ["Use true or false."]
        else:
            values["seat_sync"] = raw
    if "notes" in data:
        values["notes"] = _short(data, "notes", TEXT_MAX, errors)
    sync = values.get("seat_sync", pool.seat_sync if pool else False)
    sku = values.get("sku_id", pool.sku_id if pool else "")
    if sync and not sku and "seat_sync" not in errors:
        errors["seat_sync"] = ["Set the SKU ID to sync seat counts from Entra."]
    elif sync and "seat_sync" not in errors:
        others = LicensePool.objects.filter(organization=org, seat_sync=True, sku_id__iexact=sku)
        if pool is not None:
            others = others.exclude(id=pool.id)
        other = others.select_related("application").first()
        if other is not None:
            errors["seat_sync"] = [
                f"The pool {other.sku_name} of {other.application.name} already syncs this SKU; only one pool can."
            ]
    return values, errors


def pool_audit_values(org, pool: LicensePool) -> dict[str, Any]:
    return {
        "application": _id(pool.application_id),
        "contract": _id(pool.contract_id),
        "sku_name": pool.sku_name,
        "sku_id": pool.sku_id,
        "seats_purchased": pool.seats_purchased,
        "seats_assigned": pool.seats_assigned,
        "seat_sync": pool.seat_sync,
        "notes": pool.notes,
        **customfields.audit_values(org, POOL_KIND, pool.custom_field_data),
    }


def _save_pool(pool: LicensePool) -> None:
    """Save, with a concurrent request that turned sync on for the same SKU reported as the field error it would have
    met had it come second (the partial unique index decides)."""
    try:
        with transaction.atomic():
            pool.save()
    except IntegrityError as e:
        if "money_licensepool_one_synced_per_sku" in str(e):
            raise FieldErrors({"seat_sync": ["Another pool already syncs this SKU; only one pool can."]}) from None
        raise


@transaction.atomic
def create_pool(org, actor, data, note: dict[str, Any] | None = None) -> LicensePool:
    """A licence pool. `note` adds context to the event (the renew action's copy names the pool it copied)."""
    values, errors = _pool_fields(org, data, None)
    custom = customfields.clean(org, POOL_KIND, None, data, errors, creating=True)
    if errors:
        raise FieldErrors(errors)
    pool = LicensePool(organization=org, custom_field_data=custom or {}, **values)
    limits.check_room(org, limits.OTHER_RECORDS)
    _save_pool(pool)
    audit(
        org,
        actor,
        "license_pool.created",
        pool,
        {"name": pool.sku_name, **pool_audit_values(org, pool), **(note or {})},
    )
    return pool


@transaction.atomic
def update_pool(org, actor, pool: LicensePool, data, note: dict[str, Any] | None = None) -> LicensePool:
    """A person's edit of a pool. `note` adds context to the event (a seat proposal applied names its observation; the
    renew action names the next term a pool moved to)."""
    pool = LicensePool.objects.select_for_update().get(id=pool.id)
    values, errors = _pool_fields(org, data, pool)
    custom = customfields.clean(org, POOL_KIND, pool.custom_field_data, data, errors)
    if errors:
        raise FieldErrors(errors)
    before = pool_audit_values(org, pool)
    for field, value in values.items():
        setattr(pool, field, value)
    if custom is not None:
        pool.custom_field_data = custom
    _save_pool(pool)
    changes = _diff(before, pool_audit_values(org, pool))
    if changes:
        audit(org, actor, "license_pool.updated", pool, {"name": pool.sku_name, **changes, **(note or {})})
    return pool


@transaction.atomic
def delete_pool(org, actor, pool: LicensePool) -> None:
    audit(org, actor, "license_pool.deleted", pool, {"name": pool.sku_name, "application": _id(pool.application_id)})
    pool.delete()


def utilization(pool: LicensePool) -> float | None:
    """Percent of purchased seats assigned (one decimal), None when none are purchased. Over 100 is overuse."""
    if not pool.seats_purchased:
        return None
    return round(100 * pool.seats_assigned / pool.seats_purchased, 1)
