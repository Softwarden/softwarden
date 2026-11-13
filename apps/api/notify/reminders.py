"""Reminders (jobs.py, signals.py and ownership.py, re-implemented).

A reminder is a dated obligation computed from a source; nothing here sends anything. The NotificationRule package
turns live reminders into deliveries at its lead times: it reads `Reminder` rows (status, `effective_status`,
`due_date`, `notify_cycle`) and owns every channel, audience and dedupe key. The generator only keeps the rows true.

Sources, each read through the computation the rest of the app already uses, so a reminder and the screen it points at
can't disagree:

- **renewal_cycle** and **contract**: `money.reporting.deadline_rows`, the rows the dashboard, the renewals calendar
  and its ICS feed show. A renewal's open cycle gives its action deadline (`renewals.queue_states`); a contract that
  drives a renewal is represented by that renewal only, and a contract with no live renewal gives its stored
  `cancellation_deadline` (one source of truth, money/contracts.py). Capabilities `renewals` and `contracts`.
- **periodic_review**: `next_periodic_review` of live records (not archived, not retired) while the organization's
  compliance profile is on (catalog/compliance.enabled).
- **record_review**: the newest review's `next_review_date` of live active or deprecated records (the records the
  Needs attention review queue judges). Capability `reviews`.
- **custom_date**: `catalog.customfields.reminder_sources`, dates in fields flagged `is_reminder_source` (contracts
  and licence pools only with `contracts`). Capability `custom_fields`.
- **vendor_risk**: the expiry dates in vendors' risk files (catalog/vendor_risk.reminder_facts): every
  risk document's, and the profile's assurance report's, unless a later one of the same kind supersedes it. The source
  id is the vendor, the field `document:<id>` or `assurance`. Capability `vendor_risk`.
- **credential_expiry**: whatever `register_credential_source` registers: the client secrets and
  certificates discovery finds (discovery/credentials.reminder_facts, registered by DiscoveryConfig.ready), live ones
  only (not removed, on a live, undismissed observation of an enabled connector), routed to the app's record. A
  connector run, a decision on a candidate and a connector change regenerate them once committed (`note_credentials`).
- **end_of_life** (notify 0012): the end-of-life date of every tracked version of a live active or deprecated
  record (eol/reminders.facts), the record's own and each instance's. The source id is the record, the field
  `tracked_version:<id>`. Capability `eol`. A write to the record, one of its instances or its tracking regenerates
  them; the daily endoflife.date refresh, which moves dates without an audited write, is picked up by the hourly run.

`sync` makes the stored rows match: a missing occurrence is created; an open or snoozed one whose source date moved
moves with it (same row, so links stay); an acknowledged one for the old date is closed as obsolete and a new open one
created for the new date; a done one stays done (a person said so); an occurrence the source no longer produces
(deleted, archived, completed, capability or profile off) is closed as obsolete. When the source produces the same
date again (restored, the capability or profile back on, the date moved back), the obsolete row comes back: as
**acknowledged**, with its acknowledger and `notify_cycle` unchanged, if it was acknowledged when it was closed (it
keeps `acknowledged_at`, which nothing else does once a reminder leaves acknowledged); otherwise as open in a new
`notify_cycle`, so its lead times notify again. Done reminders are never reopened by the generator. A snooze that has
run out (its date passed, organization-local) returns the reminder to open. These are derived writes: like the stored
cancellation deadline, they are not audited one by one (the person actions in notify/services.py are). Only live rows
and closed rows with a date a source produces now are read, so closed history never slows a run.

When: the `reminders.generate` schedule every hour, each organization on its own local date (as the contract term job),
so its local midnight is acted on within the hour and reruns change nothing; and after every write that can move a
source, once its transaction commits (`after_write`, called by catalog/services.audit), for just that source's family:
a record's own review and custom dates, a vendor's, contract's or pool's custom dates, or the organization's renewals
and contracts together (a contract's reminder depends on whether a renewal it drives exists, so they are recomputed
together: a handful of column queries). A job that writes many sources one transaction at a time (the contract term
job) wraps its work in `deferred()`, so its writes regenerate once at the end instead of once per commit."""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from django.db import connection, transaction
from django.db.models import Case, F, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.request_context import snapshot_store

from .models import LIVE_REMINDER_STATUSES, Reminder, ReminderSource, ReminderStatus, ResolutionKind

log = logging.getLogger("softwarden.reminders")

S = ReminderSource
MONEY = (S.RENEWAL_CYCLE, S.CONTRACT)
RECORD_TYPES = (S.PERIODIC_REVIEW, S.RECORD_REVIEW, S.CUSTOM_DATE, S.END_OF_LIFE)  # keyed by the record
ALL_TYPES = tuple(S.values)
KEYS = ("created", "moved", "reopened", "obsoleted", "unsnoozed", "refreshed")
SCHEDULE_KEYS = (*KEYS, "failed")
TITLE_MAX = LABEL_MAX = 300

LABELS = {
    S.PERIODIC_REVIEW: "Periodic review due",
    S.RECORD_REVIEW: "Record review due",
}


@dataclass(frozen=True)
class Fact:
    """One occurrence a source produces now: its key (type, source id, field), the date and the words for it, and the
    key columns of the source's table."""

    source_type: str
    source_id: uuid.UUID
    due_date: date
    title: str
    label: str
    source_field: str = ""
    deadline_label: str = ""
    cycle_id: Any = None
    contract_id: Any = None
    resource_id: Any = None
    license_pool_id: Any = None

    @property
    def key(self) -> tuple[str, uuid.UUID, str]:
        return (self.source_type, self.source_id, self.source_field)

    def columns(self) -> dict[str, Any]:
        return {
            "title": self.title[:TITLE_MAX],
            "label": self.label[:LABEL_MAX],
            "deadline_label": self.deadline_label,
            "cycle_id": self.cycle_id,
            "contract_id": self.contract_id,
            "resource_id": self.resource_id,
            "license_pool_id": self.license_pool_id,
        }


def _uuid(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


# ---- sources -------------------------------------------------------------------------------------------------------

Provider = Callable[[Any, date, "frozenset[uuid.UUID] | None"], Iterable[Fact]]


def money_facts(org, today: date, ids=None) -> list[Fact]:
    """Renewal action deadlines and contract cancellation deadlines: exactly the rows of `reporting.deadline_rows`
    (open actions only, capabilities honoured, a driven contract shown as its renewal). Always the whole family."""
    from money import reporting

    found = []
    for row in reporting.deadline_rows(org, today):
        common = {
            "due_date": date.fromisoformat(row["date"]),
            "title": row["name"],
            "label": reporting.NEUTRAL_LABELS[row["action"]],
            "deadline_label": row["label"] or "",
        }
        if row["source"] == "renewal":
            cycle = _uuid(row["cycle"])
            found.append(Fact(S.RENEWAL_CYCLE, cycle, cycle_id=cycle, **common))
        else:
            contract = _uuid(row["id"])
            found.append(Fact(S.CONTRACT, contract, contract_id=contract, **common))
    return found


def _live_records(org, ids, active_only: bool):
    from catalog.models import RECORD_KINDS, Lifecycle, Resource
    from catalog.state import ACTIVE_LIFECYCLES

    qs = Resource.objects.filter(organization=org, kind__in=RECORD_KINDS, archived_at__isnull=True)
    qs = qs.filter(lifecycle__in=ACTIVE_LIFECYCLES) if active_only else qs.exclude(lifecycle=Lifecycle.RETIRED)
    return qs.filter(id__in=list(ids)) if ids is not None else qs


def periodic_review_facts(org, today: date, ids=None) -> list[Fact]:
    """The compliance profile's next periodic review of live records (not archived, not retired), only while the
    profile is on."""
    from catalog import compliance

    if not compliance.enabled(org):
        return []
    rows = (
        _live_records(org, ids, active_only=False)
        .filter(next_periodic_review__isnull=False)
        .values_list("id", "name", "next_periodic_review")
    )
    return [
        Fact(S.PERIODIC_REVIEW, rid, due, title=name, label=LABELS[S.PERIODIC_REVIEW], resource_id=rid)
        for rid, name, due in rows
    ]


def record_review_facts(org, today: date, ids=None) -> list[Fact]:
    """The newest review's next review date of live active or deprecated records (Needs attention's review queue
    judges the same records and the same review)."""
    from catalog.models import RecordReview
    from core.capabilities import REVIEWS, has_capability

    if not has_capability(org, REVIEWS):
        return []
    live = _live_records(org, ids, active_only=True)
    rows = (
        RecordReview.objects.filter(organization=org, resource_id__in=live.values("id"))
        .order_by("resource_id", "-confirmation_date", "-created_at")
        .distinct("resource_id")
        .values_list("resource_id", "resource__name", "next_review_date")
    )
    return [
        Fact(S.RECORD_REVIEW, rid, due, title=name, label=LABELS[S.RECORD_REVIEW], resource_id=rid)
        for rid, name, due in rows
    ]


def custom_date_facts(org, today: date, ids=None) -> list[Fact]:
    """Dates in custom fields flagged as reminder sources (customfields.reminder_sources: unarchived records, vendors,
    unarchived contracts and licence pools, the last two only with `contracts`)."""
    from catalog import customfields
    from catalog.models import Resource, Vendor
    from core.capabilities import CUSTOM_FIELDS, has_capability
    from money.models import Contract, LicensePool

    if not has_capability(org, CUSTOM_FIELDS):
        return []
    found = []
    for source in customfields.reminder_sources(org, ids=ids):
        record = source.record
        keys: dict[str, Any] = {}
        if isinstance(record, Resource):
            title, keys["resource_id"] = record.name, record.id
        elif isinstance(record, Contract):
            title, keys["contract_id"] = record.name, record.id
        elif isinstance(record, LicensePool):
            title, keys["license_pool_id"] = record.sku_name, record.id
        else:
            assert isinstance(record, Vendor)
            title = record.name
        found.append(
            Fact(
                S.CUSTOM_DATE,
                record.id,
                source.due,
                title=title,
                label=source.field.label,
                source_field=source.field.name,
                **keys,
            )
        )
    return found


# Registers discovered credentials here: a callable (org, today, ids or None) -> [Fact] with source_type
# credential_expiry, source_id the credential's id, source_field its key ID, and resource_id the record it belongs to
# (routing). A source that stops producing a credential (removed, ignored) closes its reminder as obsolete.
CREDENTIAL_SOURCES: list[Provider] = []


def register_credential_source(provider: Provider) -> None:
    if provider not in CREDENTIAL_SOURCES:
        CREDENTIAL_SOURCES.append(provider)


def vendor_risk_facts(org, today: date, ids=None) -> list[Fact]:
    """Expiry dates in vendors' risk files (catalog/vendor_risk.py); `ids` are vendor ids."""
    from catalog import vendor_risk

    return vendor_risk.reminder_facts(org, today, ids)


def end_of_life_facts(org, today: date, ids=None) -> list[Fact]:
    """End-of-life dates of tracked versions (eol/reminders.py); `ids` are record ids."""
    from eol import reminders as eol_reminders

    return eol_reminders.facts(org, today, ids)


def credential_facts(org, today: date, ids=None) -> list[Fact]:
    found: list[Fact] = []
    for provider in CREDENTIAL_SOURCES:
        for fact in provider(org, today, ids):
            if fact.source_type != S.CREDENTIAL_EXPIRY:
                raise ValueError(f"A credential source produced a {fact.source_type} reminder.")
            found.append(fact)
    return found


PROVIDERS: dict[str, Provider] = {
    S.RENEWAL_CYCLE: money_facts,  # the money family is computed once for both of its types (desired())
    S.CONTRACT: money_facts,
    S.PERIODIC_REVIEW: periodic_review_facts,
    S.RECORD_REVIEW: record_review_facts,
    S.CUSTOM_DATE: custom_date_facts,
    S.CREDENTIAL_EXPIRY: credential_facts,
    S.VENDOR_RISK: vendor_risk_facts,
    S.END_OF_LIFE: end_of_life_facts,
}


def desired(org, today: date, types: Iterable[str], ids=None) -> dict[tuple[str, uuid.UUID, str], Fact]:
    """{key: fact} for every occurrence the sources of `types` produce now (only those of `ids`, when given)."""
    types = tuple(dict.fromkeys(types))
    found: dict[tuple[str, uuid.UUID, str], Fact] = {}
    seen: set[Provider] = set()
    for source_type in types:
        provider = PROVIDERS[source_type]
        if provider in seen:
            continue
        seen.add(provider)
        for fact in provider(org, today, ids):
            if fact.source_type in types and (ids is None or fact.source_id in ids):
                found.setdefault(fact.key, fact)
    return found


# ---- keeping the rows true -----------------------------------------------------------------------------------------


def _lock(org) -> None:
    """One generation at a time per organization (the schedule and an on-save run may overlap)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"reminders:{org.pk}"])


CLOSE = {
    "status": ReminderStatus.OBSOLETE,
    "snoozed_until": None,
    "resolution_kind": ResolutionKind.SOURCE_CHANGED,
    "resolution_note": "",
    "resolution_evidence": "",
    "resolved_by": None,
}
REOPEN = {
    "status": ReminderStatus.OPEN,
    "snoozed_until": None,
    "resolved_at": None,
    "resolution_kind": "",
    "resolution_note": "",
    "resolution_evidence": "",
    "resolved_by": None,
    "acknowledged_at": None,
    "acknowledged_by": None,
}
# An obsolete reminder that was acknowledged when its source went away comes back acknowledged, in the same cycle.
RESTORE = {**REOPEN, "status": ReminderStatus.ACKNOWLEDGED}
del RESTORE["acknowledged_at"], RESTORE["acknowledged_by"]


def existing_rows(org, types, scope, wanted):
    """The rows `sync` compares with `wanted`: every live row of `types` (of the sources in `scope`), and only those
    closed rows whose source and date a source produces now (a superset; the exact key is matched in Python). Closed
    history for other dates is never read, so it can grow without slowing the run."""
    from django.db.models import Q

    qs = Reminder.objects.filter(organization=org, source_type__in=types)
    if scope is not None:
        qs = qs.filter(source_id__in=scope)
    matching = Q(
        source_id__in={f.source_id for f in wanted.values()}, due_date__in={f.due_date for f in wanted.values()}
    )
    return (
        qs.filter(Q(status__in=LIVE_REMINDER_STATUSES) | matching)
        if wanted
        else qs.filter(status__in=LIVE_REMINDER_STATUSES)
    )


@transaction.atomic
def sync(org, today: date, types: Iterable[str] = ALL_TYPES, ids: Iterable[Any] | None = None) -> dict[str, int]:
    """Make the organization's reminders of `types` (of the sources `ids`, when given) match what their sources produce
    on `today` (organization-local). Returns counts per change; a rerun returns zeros. The money family is always
    synced whole: pass no `ids` with its types."""
    types = tuple(dict.fromkeys(types))
    scope = frozenset(_uuid(i) for i in ids) if ids is not None else None
    if scope is not None and any(t in MONEY for t in types):
        raise ValueError("Renewals and contracts are synced together, without ids.")
    _lock(org)
    wanted = desired(org, today, types, scope)
    grouped: dict[tuple[str, uuid.UUID, str], list[Reminder]] = {}
    for row in existing_rows(org, types, scope, wanted).order_by("due_date", "id"):
        grouped.setdefault((row.source_type, row.source_id, row.source_field), []).append(row)

    counts: Counter[str] = Counter()
    now = timezone.now()
    created: list[Reminder] = []
    closing: list[Any] = []
    for key in {*wanted, *grouped}:
        fact = wanted.get(key)
        rows = grouped.get(key, [])
        keep = None
        if fact is not None:
            changes: dict[str, Any] = {}
            change = ""  # what the update counts as: moved, reopened, refreshed or unsnoozed
            keep = next((r for r in rows if r.due_date == fact.due_date), None)
            if keep is None:
                keep = next((r for r in rows if r.status in (ReminderStatus.OPEN, ReminderStatus.SNOOZED)), None)
                if keep is not None:
                    changes["due_date"] = fact.due_date
                    change = "moved"
            elif keep.status == ReminderStatus.OBSOLETE:
                if keep.acknowledged_at is not None:
                    changes.update(RESTORE)
                else:
                    changes.update(REOPEN, notify_cycle=keep.notify_cycle + 1)
                change = "reopened"
            if keep is None:
                created.append(
                    Reminder(
                        organization=org,
                        source_type=fact.source_type,
                        source_id=fact.source_id,
                        source_field=fact.source_field,
                        due_date=fact.due_date,
                        **fact.columns(),
                    )
                )
                counts["created"] += 1
            else:
                refreshed = {k: v for k, v in fact.columns().items() if getattr(keep, k) != v}
                if refreshed and not changes:
                    change = "refreshed"
                changes.update(refreshed)
                expired = keep.snoozed_until is not None and keep.snoozed_until < today
                if keep.status == ReminderStatus.SNOOZED and expired and "status" not in changes:
                    changes.update(status=ReminderStatus.OPEN, snoozed_until=None)
                    change = change if change == "moved" else "unsnoozed"
                if changes:
                    # Conditional on the status read: a person's snooze or acknowledgment meanwhile wins (and isn't
                    # counted).
                    updated = Reminder.objects.filter(id=keep.id, status=keep.status).update(**changes, updated_at=now)
                    counts[change or "refreshed"] += updated
        closing += [r.id for r in rows if r is not keep and r.status in LIVE_REMINDER_STATUSES]
    if created:
        Reminder.objects.bulk_create(created)
        from . import webhooks

        webhooks.reminders_created(org, created)  # `reminder.created` webhook events, once this commits
    if closing:
        counts["obsoleted"] += Reminder.objects.filter(id__in=closing, status__in=LIVE_REMINDER_STATUSES).update(
            **CLOSE, resolved_at=now, updated_at=now
        )
    return {k: counts[k] for k in KEYS}


def generate(org, today: date) -> dict[str, int]:
    """Every source of the organization (the schedule's work for one organization)."""
    return sync(org, today, ALL_TYPES)


def local_today(org, now: datetime) -> date:
    return now.astimezone(ZoneInfo(org.timezone)).date()


def scheduled_generate(now: datetime) -> dict[str, int]:
    """The `reminders.generate` schedule: every organization on its own local date. One organization's failure doesn't
    stop the others (logged with its id, never its data); `failed` counts them."""
    from core.models import active_organizations

    total: Counter[str] = Counter()
    for org in active_organizations():  # a suspended organization is skipped
        try:
            total.update(generate(org, local_today(org, now)))
        except Exception:
            total["failed"] += 1
            log.exception("reminder generation failed organization=%s", org.id)
    return {k: total[k] for k in SCHEDULE_KEYS}


# ---- on save -------------------------------------------------------------------------------------------------------


@dataclass
class Pending:
    """What one organization's writes in this unit of work touched."""

    everything: bool = False
    money: bool = False
    custom_all: bool = False
    credentials: bool = False  # discovered credentials (a connector run, a decision, a connector change)
    records: set[uuid.UUID] = field(default_factory=set)  # resources: review, custom and end-of-life dates
    holders: set[uuid.UUID] = field(default_factory=set)  # vendors, contracts, licence pools: custom dates
    vendors: set[uuid.UUID] = field(default_factory=set)  # vendors whose risk file (or name) changed


def _pending_store() -> dict[Any, Pending]:
    return snapshot_store().setdefault(("reminders", "pending"), {})


# The audit target types whose writes can move a reminder's source, and what each touches.
WATCHED = {
    "Contract",
    "RenewalObligation",
    "RenewalCycle",
    "Resource",
    "RecordReview",
    "Vendor",
    "LicensePool",
    "CustomField",
    "Organization",
    "VendorRiskProfile",
    "VendorRiskDocument",
    "TrackedVersion",  # its record's end-of-life reminders
    "ApplicationInstance",  # an instance's name is in its end-of-life reminder's title
}


def after_write(org, target) -> None:
    """Called by catalog/services.audit for every audited write: note what the write touched and, once the outermost
    transaction commits, regenerate just that (`flush`). Cheap when the target can't move a reminder. A write whose
    transaction rolls back schedules nothing (Django drops its on-commit callbacks); what it noted is either flushed
    harmlessly by a later commit in the same unit or dropped with the unit."""
    name = type(target).__name__
    if name not in WATCHED or getattr(target, "pk", None) is None:
        return
    store = _pending_store()
    pending = store.setdefault(org.pk, Pending())
    if name == "Organization":  # lead time, compliance profile, edition: anything may change
        pending.everything = True
    elif name in ("RenewalObligation", "RenewalCycle"):
        pending.money = True
    elif name == "Contract":
        pending.money = True
        pending.holders.add(target.pk)
    elif name == "Resource":
        pending.records.add(target.pk)
    elif name in ("RecordReview", "TrackedVersion"):
        pending.records.add(target.resource_id)
    elif name == "ApplicationInstance":
        pending.records.add(target.application_id)
    elif name == "CustomField":
        pending.custom_all = True
    elif name in ("VendorRiskProfile", "VendorRiskDocument"):
        pending.vendors.add(target.vendor_id)
    else:  # Vendor, LicensePool
        pending.holders.add(target.pk)
        if name == "Vendor":  # its risk reminders carry its name
            pending.vendors.add(target.pk)
    if not snapshot_store().get(DEFERRED):
        transaction.on_commit(lambda: flush(store))


DEFERRED = ("reminders", "deferred")


def note_money(org) -> None:
    """Note that the organization's renewal and contract dates may have moved without an audited write (a stored
    deadline re-derived); regenerated with the rest of the unit's notes."""
    _pending_store().setdefault(org.pk, Pending()).money = True


def note_credentials(org) -> None:
    """Discovered credentials of the organization may have changed (a connector run, a decision on a candidate, a
    connector turned off or deleted): regenerate the credential family once the transaction commits."""
    store = _pending_store()
    store.setdefault(org.pk, Pending()).credentials = True
    if not snapshot_store().get(DEFERRED):
        transaction.on_commit(lambda: flush(store))


@contextmanager
def deferred():
    """Writes inside the block only note what they touched; the block's end regenerates it all once (after the
    surrounding transaction commits, if there is one). For jobs that commit source by source: the contract term job
    would otherwise regenerate the organization's renewals and contracts once per contract it moves."""
    store = snapshot_store()
    outer = store.get(DEFERRED, False)
    store[DEFERRED] = True
    try:
        yield
    finally:
        store[DEFERRED] = outer
        if not outer:
            pending = _pending_store()
            if transaction.get_connection().in_atomic_block:
                transaction.on_commit(lambda: flush(pending))
            else:
                flush(pending)


def flush(store: dict[Any, Pending]) -> None:
    """Regenerate what the unit's committed writes touched, each organization in its own transaction. A failure is
    logged (organization id only) and never reaches the request that wrote: the hourly run repairs it."""
    from core.dates import org_today
    from core.models import Organization

    while store:
        org_id, pending = store.popitem()
        try:
            org = Organization.objects.filter(pk=org_id).first()
            if org is None:
                continue
            with transaction.atomic():
                today = org_today(org)
                if pending.everything:
                    sync(org, today, ALL_TYPES)
                    continue
                if pending.money:
                    sync(org, today, MONEY)
                if pending.credentials:
                    sync(org, today, (S.CREDENTIAL_EXPIRY,))
                if pending.custom_all:
                    sync(org, today, (S.CUSTOM_DATE,))
                if pending.records:
                    # Every type keyed by the record; custom dates only when they weren't all synced just now.
                    types = tuple(t for t in RECORD_TYPES if not (pending.custom_all and t == S.CUSTOM_DATE))
                    sync(org, today, types, pending.records)
                if pending.holders and not pending.custom_all:
                    sync(org, today, (S.CUSTOM_DATE,), pending.holders)
                if pending.vendors:
                    sync(org, today, (S.VENDOR_RISK,), pending.vendors)
        except Exception:
            log.exception("reminder regeneration failed organization=%s", org_id)


# ---- reading -------------------------------------------------------------------------------------------------------


def effective_status(today: date):
    """The status as of `today`: a snooze whose date has passed is open again, whether or not the generator has run
    since (it normalises the row on its next run)."""
    return Case(
        When(status=ReminderStatus.SNOOZED, snoozed_until__lt=today, then=Value(ReminderStatus.OPEN.value)),
        default=F("status"),
    )


def routed_to():
    """The user a reminder is routed to, as Needs attention routes its source: a record's IT steward (review and custom
    dates, a credential's owning record), a renewal's IT follow-up, a contract's renewal owner's user account, a
    licence pool's application's IT steward. An end-of-life date goes to the record's IT steward, else its
    technical owner's user account: the person who upgrades it. Null (a vendor's dates, nobody set): unassigned, so the
    IT coordinator catches it. Read from the source on every request, so a changed steward re-routes at once."""
    return Coalesce(
        F("resource__it_steward_id"),
        Case(When(source_type=S.END_OF_LIFE, then=F("resource__technical_owner__user_id"))),
        F("cycle__obligation__followup_id"),
        F("contract__renewal_owner__user_id"),
        F("license_pool__application__it_steward_id"),
    )


def path_of(
    source_type: str,
    source_id,
    *,
    resource_id=None,
    resource_kind=None,
    obligation_id=None,
    contract_id=None,
    license_pool_id=None,
    vendor_exists=True,
) -> str | None:
    """The SPA page a reminder points at: its renewal, contract, licence pool, record (a credential's owning record) or
    vendor (its custom dates and risk documents). None once the source is gone (its key column was set to null; a vendor has none, so the caller says
    whether it still exists)."""
    from catalog.models import KIND_PATHS

    if source_type == S.RENEWAL_CYCLE:
        return f"/renewals/{obligation_id}" if obligation_id else None
    if contract_id is not None:
        return f"/contracts/{contract_id}"
    if license_pool_id is not None:
        return f"/license-pools/{license_pool_id}"
    if resource_id is not None and resource_kind in KIND_PATHS:
        anchor = "#h-eol" if source_type == S.END_OF_LIFE else ""  # the record's End of life panel
        return f"/{KIND_PATHS[resource_kind]}/{resource_id}{anchor}"
    if source_type in (S.CUSTOM_DATE, S.VENDOR_RISK) and vendor_exists:
        return f"/vendors/{source_id}"  # a vendor's custom date or risk document: no key column
    if source_type == S.CREDENTIAL_EXPIRY:
        return "/discovery"  # a credential of an app nobody linked yet: the candidates queue
    return None
