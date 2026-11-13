"""Writes for a member's own notification settings and snoozes: validated, scoped to the membership or user,
audited in one transaction."""

from datetime import date, timedelta

from django.db import transaction
from django.http import Http404
from django.utils import timezone

from catalog.services import audit
from core.dates import org_today
from core.problems import Conflict, FieldErrors
from money import renewal_rules

from . import digest
from .models import (
    NotificationPreference,
    NotificationSnooze,
    Reminder,
    ReminderSource,
    ReminderStatus,
    ResolutionKind,
    SnoozeKind,
)

PREFERENCES = ("digest_enabled", "reminders_enabled", "subscriptions_enabled")


@transaction.atomic
def set_preferences(membership, actor, data):
    """Turn the daily digest, reminder emails from notification rules or saved view emails on
    or off for one membership. A user in two organizations sets each one. Send either or both."""
    errors = {name: ["This isn't a notification preference."] for name in sorted(set(data) - set(PREFERENCES))}
    wanted = {name: data[name] for name in PREFERENCES if name in data}
    for name, value in wanted.items():
        if not isinstance(value, bool):
            errors[name] = ["Use true or false."]
    if not wanted and not errors:
        errors["digest_enabled"] = ["Use true or false."]
    if errors:
        raise FieldErrors(errors)
    pref, _ = NotificationPreference.objects.select_for_update().get_or_create(
        membership=membership, defaults={"organization_id": membership.organization_id}
    )
    changed = {name: value for name, value in wanted.items() if getattr(pref, name) != value}
    if changed:
        for name, value in changed.items():
            setattr(pref, name, value)
        pref.save()
        audit(membership.organization, actor, "preference.updated", None, changed)
    return pref


def set_digest_enabled(membership, actor, enabled):
    """Turn the daily digest on or off for one membership."""
    return set_preferences(membership, actor, {"digest_enabled": enabled})


SNOOZE_MAX_DAYS = 90


def _find_row(org, user, today, subject_type, subject_id):
    """The Needs attention row the snooze names, or a field error: you can only quiet an item that is there."""
    from .attention import attention_queues

    queues, _ = attention_queues(org, user, today)
    for row in queues.get(subject_type, []):
        if digest.subject_of(subject_type, row) == (subject_type, subject_id):
            return row
    raise FieldErrors({"subject_id": ["This item isn't in Needs attention any more."]})


@transaction.atomic
def snooze(org, actor, data):
    """Snooze an item until a date, or acknowledge a renewal action until its next reminder threshold.
    Either way only this user's notifications change; the item stays in Needs attention."""
    from .attention import QUEUES

    today = org_today(org)
    errors: dict = {}
    kind = data.get("kind")
    subject_type = data.get("subject_type")
    subject_id = str(data.get("subject_id") or "")
    if kind not in SnoozeKind.values:
        errors["kind"] = ["Choose snooze or acknowledge."]
    if subject_type not in QUEUES:
        errors["subject_type"] = ["Choose a Needs attention queue."]
    elif subject_type == "reminders":
        errors["subject_type"] = ["Snooze or acknowledge a reminder itself (POST /reminders/{id}/snooze)."]
    elif kind == SnoozeKind.ACKNOWLEDGE and subject_type != "renewal_actions":
        errors["kind"] = ["Only renewal actions can be acknowledged; snooze other items."]
    until = None
    if kind == SnoozeKind.SNOOZE:
        try:
            until = date.fromisoformat(data.get("until") or "")
        except (TypeError, ValueError):
            errors["until"] = ["Enter a date as YYYY-MM-DD."]
        else:
            if until <= today:
                errors["until"] = ["Choose a date after today."]
            elif until > today + timedelta(days=SNOOZE_MAX_DAYS):
                errors["until"] = [f"Snooze for {SNOOZE_MAX_DAYS} days at most."]
    if errors:
        raise FieldErrors(errors)
    row = _find_row(org, actor, today, subject_type, subject_id)
    threshold = None
    if kind == SnoozeKind.ACKNOWLEDGE:
        deadline = date.fromisoformat(row["deadline"])
        threshold = renewal_rules.bucket(deadline, today, org.reminder_thresholds)
        if threshold == renewal_rules.OVERDUE:
            until = today  # an overdue acknowledgment holds for today only: overdue actions keep appearing
    found, _ = NotificationSnooze.objects.update_or_create(
        organization=org,
        user=actor,
        subject_type=subject_type,
        subject_id=subject_id,
        defaults={"kind": kind, "until": until, "threshold": threshold},
    )
    audit(
        org,
        actor,
        f"notification.{'snoozed' if kind == SnoozeKind.SNOOZE else 'acknowledged'}",
        None,
        {"subject_type": subject_type, "subject_id": subject_id, "until": until and until.isoformat()},
    )
    return found


@transaction.atomic
def unsnooze(org, actor, found):
    """Undo a snooze or acknowledgment: only your own (the caller looks it up among the actor's)."""
    if found.organization_id != org.id or found.user_id != actor.id:
        raise Http404  # someone else's snooze doesn't exist for you
    audit(
        org,
        actor,
        "notification.unsnoozed",
        None,
        {"subject_type": found.subject_type, "subject_id": found.subject_id},
    )
    found.delete()


# ---- reminders: snooze, acknowledge, reopen, resolve -------------------------------------------------------
#
# A reminder's status belongs to the reminder, not to one person: snoozing quiets its notifications for everyone it
# reaches through the date, acknowledging stops its advance notices (no evidence the work was done), resolving closes
# it with a reason. None of them changes the source (the renewal, contract or record) or hides the row from Needs
# attention. Editors and Admins act; each action locks the row and is audited with its snapshots.

NOTE_MAX = 2000
EVIDENCE_MAX = 500
# Closing one of these by hand needs a reason: missing it costs money (a contract renewing unnoticed), compliance (a
# skipped review) or an outage (an expired credential).
CRITICAL_SOURCES = (
    ReminderSource.RENEWAL_CYCLE,
    ReminderSource.CONTRACT,
    ReminderSource.PERIODIC_REVIEW,
    ReminderSource.CREDENTIAL_EXPIRY,
    ReminderSource.VENDOR_RISK,  # an expired SOC 2 or DPA is an audit finding
    ReminderSource.END_OF_LIFE,  # software past end of life gets no security fixes
)
MANUAL_RESOLUTIONS = (ResolutionKind.DONE, ResolutionKind.RISK_ACCEPTED, ResolutionKind.NO_LONGER_APPLIES)
NOT_ACCEPTED = "This field isn't accepted here."
GONE = "This reminder no longer applies: its source changed or is gone."


def _locked(org, reminder) -> Reminder:
    found = Reminder.objects.select_for_update().filter(organization=org, id=reminder.id).first()
    if found is None:
        raise Http404
    return found


def _about(reminder, **extra) -> dict:
    """A reminder event's payload: what the reminder is about, then what was done."""
    return {
        "title": reminder.title,
        "label": reminder.label,
        "due_date": reminder.due_date.isoformat(),
        "source_type": reminder.source_type,
        **extra,
    }


def _refuse_closed(reminder) -> None:
    if reminder.status == ReminderStatus.OBSOLETE:
        raise Conflict(GONE)
    if reminder.status == ReminderStatus.DONE:
        raise Conflict("This reminder is resolved. Reopen it first.")


def _no_fields(data) -> None:
    if data:
        raise FieldErrors({k: [NOT_ACCEPTED] for k in sorted(data)})


def _forget_acknowledgment(reminder) -> None:
    """A reminder that leaves acknowledged (snoozed, reopened, resolved) no longer says who acknowledged it; the audit
    log keeps that. Only an obsolete reminder keeps it, so the generator can restore the acknowledgment if the source
    produces the same date again (notify/reminders.py)."""
    reminder.acknowledged_at, reminder.acknowledged_by = None, None


def status_now(reminder, today: date) -> str:
    """The status as of `today` (organization-local): a snooze whose date has passed is open again."""
    if reminder.status == ReminderStatus.SNOOZED and reminder.snoozed_until and reminder.snoozed_until < today:
        return ReminderStatus.OPEN
    return reminder.status


@transaction.atomic
def snooze_reminder(org, actor, reminder, data):
    """Quiet a live reminder's notifications through `until` (a date after today, at most 90 days ahead, judged in the
    organization's timezone); the day after, it is open again. Snoozing an acknowledged reminder replaces the
    acknowledgment; snoozing a snoozed one moves the date."""
    today = org_today(org)
    errors: dict = {k: [NOT_ACCEPTED] for k in sorted(set(data) - {"until"})}
    until = None
    try:
        until = date.fromisoformat(data.get("until") or "")
    except (TypeError, ValueError):
        errors["until"] = ["Enter a date as YYYY-MM-DD."]
    else:
        if until <= today:
            errors["until"] = ["Choose a date after today."]
        elif until > today + timedelta(days=SNOOZE_MAX_DAYS):
            errors["until"] = [f"Snooze for {SNOOZE_MAX_DAYS} days at most."]
    reminder = _locked(org, reminder)
    _refuse_closed(reminder)
    if errors:
        raise FieldErrors(errors)
    assert until is not None
    reminder.status, reminder.snoozed_until = ReminderStatus.SNOOZED, until
    _forget_acknowledgment(reminder)
    reminder.save(update_fields=["status", "snoozed_until", "acknowledged_at", "acknowledged_by", "updated_at"])
    audit(org, actor, "reminder.snoozed", reminder, _about(reminder, until=until.isoformat()))
    return reminder


@transaction.atomic
def acknowledge_reminder(org, actor, reminder, data=None):
    """Seen and being handled: its advance notices stop (an overdue escalation, from the rules package, still may go
    out). From open or snoozed; acknowledging an acknowledged reminder changes nothing."""
    _no_fields(data)
    reminder = _locked(org, reminder)
    _refuse_closed(reminder)
    if reminder.status == ReminderStatus.ACKNOWLEDGED:
        return reminder
    reminder.status, reminder.snoozed_until = ReminderStatus.ACKNOWLEDGED, None
    reminder.acknowledged_at, reminder.acknowledged_by = timezone.now(), actor
    reminder.save(update_fields=["status", "snoozed_until", "acknowledged_at", "acknowledged_by", "updated_at"])
    audit(org, actor, "reminder.acknowledged", reminder, _about(reminder))
    return reminder


@transaction.atomic
def reopen_reminder(org, actor, reminder, data=None):
    """Back to open from snoozed, acknowledged or done. Reopening a resolved one starts a new notification cycle (its
    current lead times notify again); an obsolete one can't be reopened (its source no longer produces it; the generator
    reopens it if the source produces the same date again). Reopening an open one changes nothing."""
    _no_fields(data)
    reminder = _locked(org, reminder)
    if reminder.status == ReminderStatus.OBSOLETE:
        raise Conflict(GONE)
    was = status_now(reminder, org_today(org))
    if was == ReminderStatus.OPEN:  # open, or a snooze that has run out: nothing to reopen, nothing to audit
        return reminder
    if reminder.status == ReminderStatus.DONE:
        reminder.notify_cycle += 1
    reminder.status, reminder.snoozed_until = ReminderStatus.OPEN, None
    _forget_acknowledgment(reminder)
    reminder.resolved_at, reminder.resolved_by = None, None
    reminder.resolution_kind = reminder.resolution_note = reminder.resolution_evidence = ""
    reminder.save()
    audit(org, actor, "reminder.reopened", reminder, _about(reminder, **{"from": was}))
    return reminder


def _text(data, name, limit, errors) -> str:
    value = data.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        errors[name] = ["Enter text."]
        return ""
    value = value.strip()
    if len(value) > limit:
        errors[name] = [f"Use {limit:,} characters or fewer."]
    return value


@transaction.atomic
def resolve_reminder(org, actor, reminder, data):
    """Close a live reminder by hand, saying how (`kind`: done, risk_accepted or no_longer_applies). A reason (`note`)
    is required for an accepted risk and for a renewal, contract, periodic review or credential reminder; `evidence` is
    a reference (a ticket, a document). The source isn't changed: a renewal still needs its decision recorded, and a
    later date from the same source is a new reminder."""
    errors: dict = {k: [NOT_ACCEPTED] for k in sorted(set(data) - {"kind", "note", "evidence"})}
    kind = data.get("kind")
    if kind not in MANUAL_RESOLUTIONS:
        errors["kind"] = ["Say how it was resolved: done, risk_accepted or no_longer_applies."]
    note = _text(data, "note", NOTE_MAX, errors)
    evidence = _text(data, "evidence", EVIDENCE_MAX, errors)
    reminder = _locked(org, reminder)
    _refuse_closed(reminder)
    critical = kind == ResolutionKind.RISK_ACCEPTED or reminder.source_type in CRITICAL_SOURCES
    if not note and "note" not in errors and "kind" not in errors and critical:
        errors["note"] = [
            "Give a reason: this reminder is about money, compliance, access or security, or accepts a risk."
        ]
    if errors:
        raise FieldErrors(errors)
    reminder.status, reminder.snoozed_until = ReminderStatus.DONE, None
    _forget_acknowledgment(reminder)
    reminder.resolved_at, reminder.resolved_by = timezone.now(), actor
    reminder.resolution_kind, reminder.resolution_note, reminder.resolution_evidence = kind, note, evidence
    reminder.save()
    audit(org, actor, "reminder.resolved", reminder, _about(reminder, resolution_kind=kind))
    return reminder
