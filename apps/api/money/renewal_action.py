"""The contract renew action (re-implemented for the service layer).

One decision per term, in one transaction:

- **renew** or **renegotiate**: a next term is created (a new contract with `predecessor` set, the same vendor, renewal
  owner, cost centre and custom fields, and the terms sent or defaulted from this one), the chosen lines are copied to
  it through `contracts.set_items` (allocations re-validated) and the chosen licence pools moved to it (or copied, so
  seats are counted on both only when asked). If this contract is linked to a renewal, the next term is linked to the
  same one and, being its newest term, drives it: the open cycle records the decision with the money and dates before
  and after (`terms_change`), is completed (the next term is the
  evidence), and the next cycle is established with the next term's dates. A term whose end date has passed becomes
  Renewed; otherwise it stays as it is until it ends: it is still what is being paid for.
- **cancel**: no next term; the contract becomes Cancelled, so it no longer drives its
  renewal, whose open cycle records the decision and is completed. A reason is required.

Refused: an archived contract or one that isn't signed, active or pending renewal (cancelled, expired, already
renewed); a contract that already has a next term (a double submit, or two renews at once: the contract row is locked
first, so the second sees the first's successor); a renewal driven by a contract outside this one's chain of terms; an
archived renewal; renewing without this contract's end date; a next term that doesn't end after this one (a stale end
date), or that starts before this one ends without `overlap_confirmed`.

Locks, in the documented order: the contract, then its renewal (obligation), then the chosen pools in id order; the
renewal services lock the obligation before the cycle, so the order is contract, obligation, pools, cycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from django.db import transaction

from catalog import customfields
from catalog.models import Person
from catalog.services import EVIDENCE_MAX, TEXT_MAX, _text, audit, is_uuid, org_today
from core import dates
from core.input import text_value
from core.problems import Conflict, FieldErrors

from . import contracts, renewals
from .models import (
    OBLIGATION_STATUSES,
    Contract,
    ContractItem,
    ContractStatus,
    Decision,
    LicensePool,
    RenewalCycle,
    RenewalObligation,
)

RENEWING = (Decision.RENEW, Decision.RENEGOTIATE)
POOL_ACTIONS = ("move", "copy")
# What a request may say about the next term; anything not sent is defaulted from this contract (`defaults`).
TERM_FIELDS = (
    "name",
    "contract_number",
    "po_number",
    "amount",
    "currency",
    "billing_frequency",
    "start_date",
    "end_date",
    "auto_renew",
    "notice_value",
    "notice_unit",
    "notes",
)
NEXT_TERM_ONLY = (*TERM_FIELDS, "custom_field_data", "lines", "pools", "pool_action", "overlap_confirmed")
INPUTS = ("decision", "decision_notes", "decided_on", "completion_evidence", *NEXT_TERM_ONLY)


def _month_end(day: date) -> bool:
    return (day + timedelta(days=1)).day == 1


def next_end(start: date | None, end: date, next_start: date) -> date | None:
    """The default end of a next term starting `next_start`: as long as this one, in whole months when it is.

    A term is whole months when the day after its end is its start moved by whole months (1 April to 31 March,
    15 January to 14 July), or when it starts and ends on the last day of a month (29 February 2024 to 28 February
    2025, 31 January to 30 April: month ends, which month-end clamping can't move back to). The next term then runs the
    same number of months from its own start (core/dates.add_months, clamped), less a day: a year stays a year across
    29 February and month ends stay month ends. Any other term runs the same number of days; without a start date, a
    year. None when that end is past the last date there is (a contract stored with an end in year 9999)."""
    try:
        return _next_end(start, end, next_start)
    except (OverflowError, ValueError):
        return None


def _next_end(start: date | None, end: date, next_start: date) -> date | None:
    def months_on(months: int) -> date | None:
        day = dates.shift(next_start, months, "months")
        return None if day is None else day - timedelta(days=1)

    if start is None or end <= start:
        return months_on(12)
    after = end + timedelta(days=1)
    months = (after.year - start.year) * 12 + after.month - start.month
    if months > 0 and dates.shift(start, months, "months") == after:
        return months_on(months)
    if _month_end(start) and _month_end(end):
        months = (end.year - start.year) * 12 + end.month - start.month
        if months > 0:
            return months_on(months)
    return next_start + (end - start)


def refusal(contract: Contract) -> str | None:
    """Why this contract can't be decided now (409), or None. Call with the contract (and its renewal) locked for a
    write; the renew form reads it unlocked to say so up front."""
    if contract.archived_at is not None:
        return "This contract is archived. Restore it before deciding its renewal."
    successor = Contract.objects.filter(predecessor_id=contract.id).values_list("name", "status").first()
    if successor is not None:
        name, status = successor
        if status in (ContractStatus.CANCELLED, ContractStatus.EXPIRED):  # it ended without carrying the agreement on
            label = ContractStatus(status).label.lower()
            return (
                f"This term already has a next term, {name}, which is {label}. A term is renewed once: create a new "
                "contract and link it to the renewal instead."
            )
        return f"This term already has a next term, {name}. Edit that contract instead of renewing again."
    if contract.status not in OBLIGATION_STATUSES:
        label = ContractStatus(contract.status).label.lower()
        return f"A {label} contract can't be renewed or canceled."
    if contract.renewal_obligation_id is not None:
        obligation = RenewalObligation.objects.filter(id=contract.renewal_obligation_id).first()
        if obligation is not None and obligation.archived_at is not None:
            return f"Its renewal {obligation.name} is archived. Restore the renewal first."
        current = contracts.driver(contract.renewal_obligation_id)
        if current is not None and not contracts.same_chain(contract, current):
            return f"Its renewal takes its dates from the contract {current.name}. Unlink one of them first."
    return None


def defaults(org, contract: Contract) -> dict[str, Any]:
    """The next term as it would be by default: it starts the day after this one ends and runs as
    long (`next_end`; a year when the start date is missing), at the same price, billing, notice period and auto-renewal; every
    line and every licence pool carried over, pools moved. Without an end date there is no default start or end."""
    start = end = None
    if contract.end_date is not None:
        start = dates.shift(contract.end_date, 1, "days")
        if start is not None and not dates.in_window(start):
            start = None  # a term ending 9998-12-31 has no next start the API would accept: left empty
        end = next_end(contract.start_date, contract.end_date, start) if start is not None else None
        if end is not None and not dates.in_window(end):
            end = None
    return {
        "name": contract.name,
        "contract_number": "",
        "po_number": "",
        "amount": None if contract.amount is None else str(contract.amount),
        "currency": contract.currency,
        "billing_frequency": contract.billing_frequency,
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
        "auto_renew": contract.auto_renew,
        "notice_value": contract.notice_value,
        "notice_unit": contract.notice_unit,
        "notes": f"Renewal of {contract.name}.",
        "lines": [
            str(i)
            for i in ContractItem.objects.filter(contract=contract)
            .order_by("created_at", "id")
            .values_list("id", flat=True)
        ],  # fmt: skip
        "pools": [
            str(p)
            for p in LicensePool.objects.filter(contract=contract)
            .order_by("sku_name", "id")
            .values_list("id", flat=True)
        ],  # fmt: skip
        "pool_action": "move",
        "overlap_confirmed": False,
    }


@dataclass(frozen=True)
class Outcome:
    contract: Contract  # the decided term, as stored after the decision
    successor: Contract | None  # the next term (renew, renegotiate)
    cycle: RenewalCycle | None  # the cycle the decision was recorded on, if the contract has a renewal
    next_cycle: RenewalCycle | None  # the cycle established with the next term's dates


def _ids(raw, field, errors, allowed: dict[str, Any], noun: str) -> list[Any]:
    """The chosen rows of `allowed` ({id: row}), in the order sent; a field error for anything else."""
    if raw is None:
        return list(allowed.values())
    if not isinstance(raw, list) or not all(isinstance(v, str) and is_uuid(v) for v in raw):
        errors[field] = [f"Send a list of this contract's {noun} IDs."]
        return []
    wanted = list(dict.fromkeys(str(v).lower() for v in raw))
    unknown = [v for v in wanted if v not in allowed]
    if unknown:
        errors[field] = [f"Choose {noun} of this contract only."]
        return []
    return [allowed[v] for v in wanted]


def _decision_fields(org, data, errors) -> tuple[str, str, date | None]:
    decision = data.get("decision")
    if decision not in Decision.values:
        errors["decision"] = ["Choose renew, renegotiate or cancel."]
        decision = ""
    raw_notes = data.get("decision_notes")
    notes = _text(raw_notes) if isinstance(raw_notes, str) else ""
    if raw_notes is not None and not isinstance(raw_notes, str):
        errors["decision_notes"] = ["Enter text."]
    elif len(notes) > TEXT_MAX:
        errors["decision_notes"] = [f"Use {TEXT_MAX} characters or fewer."]
    if decision == Decision.CANCEL and not notes:
        errors["decision_notes"] = ["Say why it isn't being renewed."]
    values: dict[str, Any] = {}
    today = org_today(org)
    if data.get("decided_on") in (None, ""):
        values["decided_on"] = today
    else:
        renewals._date(data, "decided_on", errors, values, allow_future=False, today=today)
    evidence = text_value(data.get("completion_evidence"), "completion_evidence", errors)
    if len(evidence) > EVIDENCE_MAX:
        errors["completion_evidence"] = [f"Use {EVIDENCE_MAX} characters or fewer."]
    return decision, notes, values.get("decided_on")


def _next_term(org, contract: Contract, data, errors) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """The next term's validated contract fields: what was sent over the defaults, through the contract service's
    own validation (contracts._contract_fields), plus the next-term rules (an end date after this one's, an overlap
    confirmed)."""
    base = defaults(org, contract)
    if "start_date" in data and "end_date" not in data and contract.end_date is not None:
        # A start sent without an end: the default end follows the sent start (as long as this term).
        try:
            sent: date | None = dates.parse_stored(data["start_date"])
        except ValueError:
            sent = None  # contracts._contract_fields names the error
        following = next_end(contract.start_date, contract.end_date, sent) if sent is not None else None
        if following is not None:
            base["end_date"] = following.isoformat()
    wanted = {k: data[k] if k in data else base[k] for k in TERM_FIELDS}
    # The same vendor, cost centre and renewal owner (unless that person has left: the next term then has none).
    wanted["vendor"] = str(contract.vendor_id) if contract.vendor_id else None
    owner = contract.renewal_owner_id
    active = owner is not None and Person.objects.filter(id=owner, active=True).exists()
    wanted["renewal_owner"] = str(owner) if active else None
    wanted["cost_center"] = contract.cost_center
    wanted["status"] = ContractStatus.ACTIVE  # set below from the start date
    values, term_errors = contracts._contract_fields(org, wanted, None)
    start, end = values.get("start_date"), values.get("end_date")
    for field in ("start_date", "end_date"):
        if values.get(field) is None and field not in term_errors:
            term_errors[field] = ["Needed for the next term."]
    old_end = contract.end_date
    today = org_today(org)
    if end is not None and "end_date" not in term_errors and old_end is not None and end <= old_end:
        term_errors["end_date"] = [
            f"The next term has to end after this one ({old_end.isoformat()}); a renewal that ends on or before it "
            "would leave the renewal's dates behind."
        ]
    elif end is not None and "end_date" not in term_errors and end < today:
        term_errors["end_date"] = ["The next term would already have ended."]
    confirmed = data.get("overlap_confirmed", False)
    if not isinstance(confirmed, bool):
        term_errors["overlap_confirmed"] = ["Use true or false."]
    elif (
        start is not None
        and old_end is not None
        and start <= old_end
        and not confirmed
        and "start_date" not in term_errors
    ):
        term_errors["start_date"] = [
            f"Starts before this term ends ({old_end.isoformat()}), so both terms would count while they overlap. "
            "Confirm the overlap, or start it the day after."
        ]
    if start is not None:
        values["status"] = ContractStatus.ACTIVE if start <= today else ContractStatus.SIGNED
    # This term's custom field values, with any sent over them, validated as on any new contract: a field made
    # required since this term was entered must be given (custom_field_data in the request).
    sent = data.get("custom_field_data")
    if sent is not None and not isinstance(sent, dict):
        term_errors["custom_field_data"] = ["Send custom fields as an object of field names and values."]
    else:
        merged = {**carried(org, contracts.KIND, contract.custom_field_data), **(sent or {})}
        values["custom_field_data"] = (
            customfields.clean(org, contracts.KIND, None, {"custom_field_data": merged}, term_errors, creating=True)
            or {}
        )
    errors.update(term_errors)
    return values, term_errors


def _terms(contract: Contract, successor: Contract | None) -> dict[str, Any]:
    """The money and dates before and after the decision, for the cycle's terms_change (money 0005)."""
    terms: dict[str, Any] = {
        "old_amount": contract.amount,
        "old_currency": contract.currency,
        "old_billing_frequency": contract.billing_frequency,
        "old_annualized_cost": contract.annualized_cost,
        "old_start_date": contract.start_date,
        "old_end_date": contract.end_date,
    }
    if successor is not None:
        terms.update(
            new_amount=successor.amount,
            new_currency=successor.currency,
            new_billing_frequency=successor.billing_frequency,
            new_annualized_cost=successor.annualized_cost,
            new_start_date=successor.start_date,
            new_end_date=successor.end_date,
        )
    return terms


def _set_status(org, actor, contract: Contract, status: str) -> None:
    if contract.status != status:
        before = contract.status
        contract.status = status
        contract.save(update_fields=["status", "updated_at"])
        audit(org, actor, "contract.updated", contract, {"status": [before, status]})


@transaction.atomic
def renew(org, actor, contract: Contract, data) -> Outcome:
    """Record the decision on this contract's term and carry it out (module docstring). Field errors are 400s; the
    refusals above are 409s. Nothing is written unless everything succeeds."""
    contract = contracts.lock(contract)
    contracts.lock_obligations(org, contract.renewal_obligation_id)
    message = refusal(contract)
    if message:
        raise Conflict(message)
    errors: dict[str, list[str]] = {}
    unknown = [k for k in data if k not in INPUTS]
    for key in unknown:
        errors[key] = ["The renew action doesn't take this field."]
    decision, notes, decided_on = _decision_fields(org, data, errors)
    renewing = decision in RENEWING
    values: dict[str, Any] = {}
    lines: list[ContractItem] = []
    pools: list[LicensePool] = []
    pool_action = "move"
    if renewing:
        if contract.end_date is None:
            raise Conflict("Add this contract's end date first: the next term starts after it.")
        values, _ = _next_term(org, contract, data, errors)
        current_lines = {
            str(i.id): i
            for i in ContractItem.objects.filter(contract=contract)
            .select_related("resource")
            .order_by("created_at", "id")
        }
        lines = _ids(data.get("lines"), "lines", errors, current_lines, "line")
        # Locked in id order after the contract and the renewal (the documented order).
        current_pools = {
            str(p.id): p for p in LicensePool.objects.select_for_update().filter(contract=contract).order_by("id")
        }
        pools = _ids(data.get("pools"), "pools", errors, current_pools, "license pool")
        pool_action = data.get("pool_action") or "move"
        if pool_action not in POOL_ACTIONS:
            errors["pool_action"] = ["Choose move or copy."]
    elif decision:
        for key in NEXT_TERM_ONLY:
            if key in data:
                errors[key] = ["Only renewing or renegotiating creates a next term."]
    if errors:
        raise FieldErrors(errors)

    obligation = (
        RenewalObligation.objects.get(id=contract.renewal_obligation_id) if contract.renewal_obligation_id else None
    )
    successor = None
    if renewing:
        successor = _create_successor(org, actor, contract, values, obligation)
        contracts.set_items(org, actor, successor, [(line.resource, line.allocation_percent) for line in lines])
        _carry_pools(org, actor, contract, successor, pools, pool_action)
    audit(
        org,
        actor,
        "contract.decided",
        contract,
        {
            "name": contract.name,
            "decision": decision,
            "decided_on": decided_on.isoformat() if decided_on else None,
            **({"successor": str(successor.id), "successor_name": successor.name} if successor else {}),
        },
    )
    today = org_today(org)
    if decision == Decision.CANCEL:
        _set_status(org, actor, contract, ContractStatus.CANCELLED)
    elif contract.end_date is not None and contract.end_date < today:
        _set_status(org, actor, contract, ContractStatus.RENEWED)  # an ended, renewed term

    cycle = next_cycle = None
    if obligation is not None:
        cycle, next_cycle = _cycle_work(org, actor, obligation, contract, successor, decision, notes, decided_on, data)
        contracts.resync(org, actor, obligation.id)
    contract.refresh_from_db()
    return Outcome(contract, successor, cycle, next_cycle)


def carried(org, kind: str, stored: dict | None) -> dict[str, Any]:
    """A record's stored custom field values that still apply to `kind`: a field whose kinds no longer include it may
    leave values behind (catalog/customfields.py), which a new record can't take."""
    names = {cf.name for cf in customfields.for_kind(org, kind)}
    return {k: v for k, v in (stored or {}).items() if k in names}


def _create_successor(org, actor, contract: Contract, values, obligation) -> Contract:
    successor = Contract(
        organization=org,
        created_by=actor,
        predecessor=contract,
        renewal_obligation=obligation,
        **values,
    )
    contracts.derive(successor, org.renewal_lead_days)
    successor.save()
    audit(
        org,
        actor,
        "contract.created",
        successor,
        {
            **contracts.audit_values(org, successor),
            "predecessor": str(contract.id),
            "renewed_from": contract.name,
        },
    )
    return successor


def _carry_pools(org, actor, contract: Contract, successor: Contract, pools, action: str) -> None:
    """Move each chosen pool to the next term (its seats are counted once), or copy it: a new pool with the same
    SKU, seats and custom field values on the next term, not syncing (only one pool per SKU may sync) and never
    synced."""
    for pool in pools:
        if action == "move":
            contracts.update_pool(
                org, actor, pool, {"contract": str(successor.id)}, note={"renewed_from": str(contract.id)}
            )
        else:
            _copy_pool(org, actor, pool, successor)


def _copy_pool(org, actor, pool: LicensePool, successor: Contract) -> None:
    """A copy of the pool on the next term. A pool custom field the copy can't satisfy (a field made required since,
    with no value on the pool) is reported under `pools`, naming the pool, not as the contract's own field."""
    try:
        contracts.create_pool(
            org,
            actor,
            {
                "application": str(pool.application_id),
                "contract": str(successor.id),
                "sku_name": pool.sku_name,
                "sku_id": pool.sku_id,
                "seats_purchased": pool.seats_purchased,
                "seats_assigned": pool.seats_assigned,
                "notes": pool.notes,
                "custom_field_data": carried(org, contracts.POOL_KIND, pool.custom_field_data),
            },
            note={"copied_from": str(pool.id)},
        )
    except FieldErrors as e:
        labels = {f"{customfields.PREFIX}{cf.name}": cf.label for cf in customfields.for_kind(org, contracts.POOL_KIND)}
        problems = " ".join(
            m if labels.get(key, "") and m.startswith(labels[key]) else f"{labels.get(key, key)}: {m}"
            for key, messages in e.errors.items()
            for m in messages
        )
        raise FieldErrors(
            {
                "pools": [
                    f"License pool {pool.sku_name} can't be copied: {problems} Fill it on the pool, or move the pool "
                    "instead."
                ]
            }
        ) from None


def _cycle_work(org, actor, obligation, contract, successor, decision, notes, decided_on, data):
    """Record the decision and its terms on the renewal's open cycle, complete it, and (renewing) establish the next
    cycle, which takes the next term's dates (it drives the renewal now). A renewal whose latest cycle is already
    complete has no open cycle to record on: its history isn't rewritten, and renewing only establishes the next
    cycle."""
    open_ = RenewalCycle.objects.filter(obligation=obligation, completed_on__isnull=True).first()
    cycle = None
    if open_ is not None:
        cycle = renewals.record_decision(
            org,
            actor,
            obligation,
            open_.id,
            {"decision": decision, "decision_notes": notes, "decided_on": decided_on.isoformat() if decided_on else ""},
            terms=_terms(contract, successor),  # the decision changes only its status, not its money or dates
        )
        evidence = text_value(data.get("completion_evidence"), "completion_evidence", {}) or (
            f"Renewed as the contract {successor.name}." if successor else f"Contract {contract.name} cancelled."
        )
        cycle = renewals.record_completion(
            org,
            actor,
            obligation,
            open_.id,
            {
                "completed_on": (decided_on or org_today(org)).isoformat(),
                "completion_evidence": evidence[:EVIDENCE_MAX],
            },
        )
    next_cycle = renewals.establish_next_cycle(org, actor, obligation, {}) if successor is not None else None
    return cycle, next_cycle
