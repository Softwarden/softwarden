"""Contract term statuses that follow the dates.

The renew action sets statuses only when a decision is recorded. This moves every organization's terms along their
dates afterwards, in the organization's timezone. A *counting* next term is one that isn't cancelled and isn't
archived (an expired or renewed next term still renewed this one; `counting_successor`):

- **Signed → Active** on the start date (a future commitment becomes current spend), or, with no start date, once the
  end date has passed (then on to its end state below);
- **Active / Pending renewal → Renewed** once the end date has passed and a counting next term renews it;
- **Active / Pending renewal → Expired** once the end date has passed with no counting next term and auto-renewal not
  "yes" (no or unknown: nothing says it carries on). A term that auto-renews stays as it is: it rolls over until
  someone records a decision;
- **Renewed → Expired** once its end date has passed when its next term no longer counts (cancelled, archived or
  deleted): nothing renewed it after all, whatever its auto-renewal. The job never moves a Renewed term back to Active
  (it would drive again and write its older end date into the renewal); that is a person's edit. A Renewed term that
  hasn't ended (only after a manual edit) is left alone.

A Signed term whose whole term is already past goes through Active to its end state in the same run. Archived
contracts are left alone. Each change is audited as `contract.updated` (status pair, `"by": "term dates"`, no actor:
the schedule), then the contract's renewal is re-synced (`contracts.resync`), so an Expired term stops driving it.
`due` selects in SQL only contracts whose status is behind their dates (an ended auto-renewing term is not selected
again), so a rerun changes nothing and costs one query per organization. Each contract is its own transaction: one
that fails is logged (by id) and counted, and the rest go on.

The `contracts.advance_terms` schedule (money/apps.py) runs hourly, so each organization's local midnight is acted on
within the hour. The same run re-derives the stored cancellation deadline of every live term whose stored date differs
from its computation (`contracts.refresh_deadlines`, not audited, as when the lead time changes), so a drifted date
is put right within the hour instead of being warned about on every report (money/reporting.py)."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Exists, OuterRef, Q

from catalog.services import audit

from . import contracts
from .contracts import counting_successors
from .models import LIVE_STATUSES, AutoRenew, Contract, ContractStatus

log = logging.getLogger("softwarden.worker")
BY = "term dates"
KEYS = ("started", "renewed", "expired", "failed")
SCHEDULE_KEYS = (*KEYS, "deadlines")


def counting_successor(contract: Contract) -> bool:
    return counting_successors().filter(organization_id=contract.organization_id, predecessor_id=contract.id).exists()


def _ended(contract: Contract, today: date) -> bool:
    return contract.end_date is not None and contract.end_date < today


def next_status(contract: Contract, today: date, renewed: bool) -> str | None:
    """The status the contract's dates call for after its current one, or None when it is where it should be.
    `renewed`: a counting next term renews it."""
    if contract.archived_at is not None:
        return None
    if contract.status == ContractStatus.SIGNED:
        started = contract.start_date is not None and contract.start_date <= today
        if started or (contract.start_date is None and _ended(contract, today)):
            return ContractStatus.ACTIVE
        return None
    if contract.status in LIVE_STATUSES and _ended(contract, today):
        if renewed:
            return ContractStatus.RENEWED
        if contract.auto_renew != AutoRenew.YES:
            return ContractStatus.EXPIRED
        return None
    if contract.status == ContractStatus.RENEWED and not renewed and _ended(contract, today):
        # Nothing renewed it after all, and its term is over: it expired, whatever its auto-renewal. Never back to
        # Active: a Renewed term doesn't drive, and making it count again would write its older end date back into
        # the renewal. Bringing it back is a person's edit (takeover_error and the "renewed by" guard).
        return ContractStatus.EXPIRED
    return None


def due(org, today: date):
    """The organization's contracts whose status is behind their dates, in one query (next_status still decides,
    under the lock)."""
    # Scoped to the organization (a successor is always in its predecessor's), so the subquery reads its rows only.
    renewed = Exists(counting_successors().filter(organization=org, predecessor_id=OuterRef("pk")))
    signed = Q(status=ContractStatus.SIGNED) & (
        Q(start_date__lte=today) | Q(start_date__isnull=True, end_date__lt=today)
    )
    ended = Q(status__in=LIVE_STATUSES, end_date__lt=today) & (~Q(auto_renew=AutoRenew.YES) | renewed)
    unrenewed = Q(status=ContractStatus.RENEWED, end_date__lt=today) & ~renewed
    return Contract.objects.filter(organization=org, archived_at__isnull=True).filter(signed | ended | unrenewed)


@transaction.atomic
def advance_contract(org, contract: Contract, today: date) -> list[tuple[str, str]]:
    """Move one contract to the status its dates call for: [(from, to), ...], empty when nothing changed. Locks the
    contract, then its renewal (the documented order), and re-reads both, so it is safe beside any other write: a
    contract deleted, archived or edited since `due` read it is judged as it is now (or skipped)."""
    locked = Contract.objects.select_for_update().filter(id=contract.id).first()
    if locked is None:  # deleted meanwhile
        return []
    contract = locked
    contracts.lock_obligations(org, contract.renewal_obligation_id)
    renewed = counting_successor(contract)
    moved: list[tuple[str, str]] = []
    while (status := next_status(contract, today, renewed)) is not None:
        moved.append((contract.status, status))
        before = contract.status
        contract.status = status
        contract.save(update_fields=["status", "updated_at"])
        audit(org, None, "contract.updated", contract, {"status": [before, status], "by": BY})
    if moved:
        contracts.resync(org, None, contract.renewal_obligation_id)
    return moved


def advance_terms(org, today: date) -> dict[str, int]:
    """Every contract of the organization whose status is behind its dates on `today` (organization-local).
    Returns counts: started, renewed, expired, and failed (contracts whose change raised; logged by id)."""
    counts: Counter[str] = Counter()
    names: dict[str, str] = {
        ContractStatus.ACTIVE: "started",
        ContractStatus.RENEWED: "renewed",
        ContractStatus.EXPIRED: "expired",
    }
    for contract in due(org, today).order_by("id").only("id"):
        try:
            moved = advance_contract(org, contract, today)
        except Exception:
            counts["failed"] += 1
            log.exception("contract term status failed contract=%s", contract.id)
            continue
        for before, after in moved:
            # Renewed → Active is a rollover, not a start; count it with the term's new state.
            counts["started" if before == ContractStatus.SIGNED else names[after]] += 1
    return {k: counts[k] for k in KEYS}


def local_today(org, now: datetime) -> date:
    return now.astimezone(ZoneInfo(org.timezone)).date()


def scheduled_advance(now: datetime) -> dict[str, int]:
    """The `contracts.advance_terms` schedule: every organization, each on its own local date, then its live terms'
    drifted deadlines re-derived (`deadlines` counts them). One organization's failure doesn't stop the others
    (logged with its id, never its data); `failed` counts organizations and contracts that failed."""
    from core.models import active_organizations
    from notify import reminders

    total: Counter[str] = Counter()
    for org in active_organizations():  # a suspended organization is skipped
        try:
            # Each contract commits on its own; its reminders are regenerated once for the organization at the end of
            # the block, not once per contract (a full renewals-and-contracts pass each time).
            with reminders.deferred():
                total.update(advance_terms(org, local_today(org, now)))
                with transaction.atomic():
                    refreshed = contracts.refresh_deadlines(org, live_only=True)
                total["deadlines"] += refreshed
                if refreshed:  # not audited, so nothing else notes it
                    reminders.note_money(org)
        except Exception:
            total["failed"] += 1
            log.exception("contract term statuses failed organization=%s", org.id)
    return {k: total[k] for k in SCHEDULE_KEYS}
