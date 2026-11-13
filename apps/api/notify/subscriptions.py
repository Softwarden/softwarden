"""Saved view subscriptions (on notify/deliveries.py): a weekly email of a saved view's rows to one member.

**Who.** A member subscribes themselves to a view they can see (shared, or their own); an Admin can also subscribe
another member, to a view that member can see (and the Admin can see). One subscription per member and view. A
member sees, changes and removes their own subscriptions; an Admin sees and manages everyone's (a private view's
name is shown only to someone who can see the view). Every change is audited.

**When.** On the subscription's weekday of the organization's local date, once its local time has reached the
notification send time (notify/rules.send_time: the digest time, at the latest 23:00). The `saved_views.subscriptions`
schedule runs hourly at :26, a minute after notification rules, so it reaches the same local times as they do.

**What.** The view's rows as the member's own list shows them now (catalog/saved_views.run_list runs the list
endpoint's viewset with the member's membership: their role, the organization's capabilities, the same filters,
order and serializers), the view's columns, at most ROWS_MAX rows then "and N more", and a link to the SPA list with
the view's query (catalog/saved_views.list_link), which reproduces the filter. An empty result still sends ("no
records match"), so the member knows the view ran.

**Once.** Each send is a Delivery of kind `subscription` under the key `sv:{subscription}:{local date}`: a rerun, a
second worker or a later hour of the same day sends nothing. Nothing is sent, and a `skipped` delivery under the same
key says why, when the view was deleted, the member is deactivated or has no address, they turned saved view emails
off (NotificationPreference.subscriptions_enabled), they can no longer see the view (it was made private), their role
or the organization's capabilities no longer allow the list, or the view's filter has problems (a custom field it
names was changed). A turned-off subscription (`active` false) records nothing. Job payloads carry delivery ids only.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework import exceptions

from catalog import saved_filters, saved_views
from catalog.models import Kind, SavedFilter
from catalog.services import audit, is_uuid
from core.drf import timestamp
from core.models import Membership, Role
from core.problems import Conflict, FieldErrors

from . import deliveries, render, rules
from .models import Channel, Delivery, DeliveryKind, DeliveryStatus, NotificationPreference, SavedViewSubscription

log = logging.getLogger("softwarden.notify")

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
NOUNS: dict[str, tuple[str, str]] = {
    Kind.APPLICATION: ("application", "applications"),
    Kind.COMPONENT: ("component", "components"),
    Kind.AI_AGENT: ("AI agent", "AI agents"),
}
ROWS_MAX = saved_views.ROWS_MAX
FIELDS = ("saved_view", "user", "weekday", "active")
EDITABLE = ("weekday", "active")
SCHEDULE_KEYS = ("sent", "skipped", "failed")


def delivery_key(sub: SavedViewSubscription, day: date) -> str:
    """One weekly email per subscription and local date."""
    return f"sv:{sub.id.hex}:{day.isoformat()}"


# ---- reading --------------------------------------------------------------------------------------------------------


def visible(org, user, role):
    """The subscriptions `user` sees: an Admin all of the organization's, anyone else their own."""
    rows = SavedViewSubscription.objects.filter(organization=org).select_related(
        "saved_view", "saved_view__user", "membership__user"
    )
    return rows if role == Role.ADMIN else rows.filter(membership__user=user)


def can_manage(sub: SavedViewSubscription, user, role: str) -> bool:
    return role == Role.ADMIN or sub.membership.user_id == user.id


def subscription_json(org, sub: SavedViewSubscription, user, role: str) -> dict[str, Any]:
    """A subscription as `user` sees it. The view's name only when `user` can see the view."""
    view = sub.saved_view
    view_json = None
    if view is not None:
        seen = saved_filters.can_see(view, user)
        view_json = {"id": str(view.id), "name": view.name if seen else None, "kind": view.kind, "shared": view.shared}
    member = sub.membership.user
    return {
        "id": str(sub.id),
        "saved_view": view_json,
        "user": {"id": member.id, "name": member.get_full_name() or member.email},
        "mine": member.id == user.id,
        "weekday": sub.weekday,
        "weekday_name": WEEKDAYS[sub.weekday],
        "active": sub.active,
        "last_sent": sub.last_sent.isoformat() if sub.last_sent else None,
        "send_time": rules.send_time(org).strftime("%H:%M"),
        "timezone": org.timezone,
        "can_edit": can_manage(sub, user, role),
        "created_at": timestamp(sub.created_at),
        "updated_at": timestamp(sub.updated_at),
    }


# ---- writes ---------------------------------------------------------------------------------------------------------


def _role(org, user) -> str:
    return Membership.objects.get(organization=org, user=user).role


def _weekday(value, errors) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6:
        errors["weekday"] = ["Choose a weekday from 0 (Monday) to 6 (Sunday)."]
        return None
    return value


def _active(value, errors) -> bool:
    if not isinstance(value, bool):
        errors["active"] = ["Send true or false."]
        return True
    return value


def _audited(sub: SavedViewSubscription) -> dict[str, Any]:
    view = sub.saved_view
    return {
        "saved_view": str(sub.saved_view_id) if sub.saved_view_id else None,
        "view_name": view.name if view is not None and view.shared else None,  # a private view's name stays private
        "user": sub.membership.user_id,
        "weekday": sub.weekday,
        "active": sub.active,
    }


@transaction.atomic
def create(org, actor, data: dict[str, Any]) -> SavedViewSubscription:
    role = _role(org, actor)
    errors: dict[str, list[str]] = {name: ["This isn't a field of a subscription."] for name in set(data) - set(FIELDS)}
    weekday = _weekday(data.get("weekday"), errors)
    active = _active(data.get("active", True), errors)
    user_id = data.get("user", actor.id)
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        errors["user"] = ["Choose a member by their user id."]
        user_id = None
    elif user_id != actor.id and role != Role.ADMIN:
        raise exceptions.PermissionDenied("Only Admins can subscribe someone else.")
    membership = None
    if user_id is not None:
        membership = Membership.objects.select_related("user").filter(organization=org, user_id=user_id).first()
        if membership is None:
            errors["user"] = ["Choose a member of this organization."]
    raw = data.get("saved_view")
    view = None
    if isinstance(raw, str) and raw.startswith(saved_filters.BUILTIN_PREFIX):
        errors["saved_view"] = ["Built-in filters can't be subscribed to. Save it as your own view first."]
    else:
        view = SavedFilter.objects.filter(organization=org, id=str(raw)).first() if is_uuid(raw) else None
        if view is None or not saved_filters.can_see(view, actor):
            errors["saved_view"] = ["Choose a saved view you can see."]
            view = None
    if view is not None and membership is not None and not saved_filters.can_see(view, membership.user):
        errors["saved_view"] = ["This member can't see this view: it is private to its owner. Share it first."]
    if errors:
        raise FieldErrors(errors)
    assert membership is not None and view is not None and weekday is not None
    if SavedViewSubscription.objects.filter(saved_view=view, membership=membership).exists():
        raise FieldErrors({"saved_view": ["Already subscribed to this view. Change that subscription's weekday."]})
    sub = SavedViewSubscription.objects.create(
        organization=org, saved_view=view, membership=membership, weekday=weekday, active=active, created_by=actor
    )
    audit(org, actor, "saved_view_subscription.created", sub, _audited(sub))
    return sub


def find(org, actor, pk, role: str | None = None) -> SavedViewSubscription:
    """A subscription the actor sees, or 404 (someone else's doesn't exist for a non-Admin)."""
    if not is_uuid(pk):
        raise Http404
    sub = visible(org, actor, role or _role(org, actor)).filter(id=pk).first()
    if sub is None:
        raise Http404
    return sub


@transaction.atomic
def update(org, actor, sub: SavedViewSubscription, data: dict[str, Any]) -> SavedViewSubscription:
    """Change the weekday or turn it off and on. Send only what changes."""
    role = _role(org, actor)
    list(SavedViewSubscription.objects.select_for_update().filter(organization=org, id=sub.id).values_list("id"))
    sub = find(org, actor, sub.id, role)  # read after the lock
    errors: dict[str, list[str]] = {name: ["This can't be changed."] for name in set(data) - set(EDITABLE)}
    values: dict[str, Any] = {}
    if "weekday" in data:
        values["weekday"] = _weekday(data["weekday"], errors)
    if "active" in data:
        values["active"] = _active(data["active"], errors)
    if errors:
        raise FieldErrors(errors)
    changes = {k: [getattr(sub, k), v] for k, v in values.items() if getattr(sub, k) != v}
    if changes:
        for k, v in values.items():
            setattr(sub, k, v)
        sub.save()
        audit(org, actor, "saved_view_subscription.updated", sub, {**_audited(sub), "fields": sorted(changes)})
    return sub


@transaction.atomic
def delete(org, actor, sub: SavedViewSubscription) -> None:
    sub = find(org, actor, sub.id)
    audit(org, actor, "saved_view_subscription.deleted", sub, _audited(sub))
    sub.delete()


# ---- what is sent ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Composed:
    subject: str
    content: dict[str, Any]
    total: int


@dataclass(frozen=True)
class Skip:
    reason: str


# The key a private view's email carries in its stored content: only that user may read the message in the log
# (notify/channel_serializers.DeliveryDetailSerializer); for everyone else, Admins included, it is redacted.
PRIVATE_TO = "private_to"


def _view_subject(view: SavedFilter | None) -> str:
    """The stored subject: a shared view's name, never a private view's (the notification log shows subjects to
    Admins, who can't see someone else's private view)."""
    return f"Saved view: {view.name}" if view is not None and view.shared else "Saved view subscription"


def compose(org, sub: SavedViewSubscription, day: date) -> Composed | Skip:
    """The weekly email for `sub` on `day`, or why nothing can be sent. Reads only."""
    view = sub.saved_view
    membership = sub.membership
    user = membership.user
    if view is None:
        return Skip("Not sent: the saved view was deleted. Remove this subscription.")
    if not user.is_active:
        return Skip("Not sent: the member's account is deactivated.")
    if not user.email:
        return Skip("Not sent: the member has no email address.")
    pref = NotificationPreference.objects.filter(membership=membership).first()
    if pref is not None and not pref.subscriptions_enabled:
        return Skip("Not sent: the member turned off saved view emails.")
    if not saved_filters.can_see(view, user):
        return Skip("Not sent: the member can no longer see this view (it is private to its owner).")
    problems = saved_filters.problems_of(view, org)
    if problems:
        detail = f" ({'; '.join(problems)})" if view.shared else ""  # a private view's query stays private
        return Skip(f"Not sent: the view's filter has problems{detail}. Fix or delete the view.")
    query = view.query if isinstance(view.query, dict) else {}
    try:
        found = saved_views.run_list(membership, view.kind, query, ROWS_MAX)
    except saved_views.NotAllowed as refused:
        why = str(refused) or "your role doesn't allow it"
        return Skip(f"Not sent: the member can no longer open this list ({why}).")
    except saved_views.BadQuery as refused:
        detail = f" ({refused})" if view.shared else ""
        return Skip(f"Not sent: the view's filter has problems{detail}. Fix or delete the view.")
    columns = saved_views.shown_columns(org, view.kind, list(view.columns))
    rows = [
        (
            str(r.get("name", "")),
            saved_views.record_link(view.kind, str(r["id"])),
            [saved_views.cell(r, c.id) for c in columns[1:]],
        )
        for r in found.rows
    ]
    one, many = NOUNS[view.kind]
    noun = one if found.total == 1 else many
    subject = f"{_view_subject(view)} ({found.total} {noun})"
    heading = f"Saved view: {view.name} ({found.total} {noun})"  # the subscriber's own email names it
    weekday = WEEKDAYS[sub.weekday]
    if found.total:
        intro = f"{found.total} {noun} match {view.name} on {day.isoformat()}."
    else:
        intro = f"No {many} match {view.name} on {day.isoformat()}."
    more = f"and {found.total - len(rows)} more" if found.total > len(rows) else ""
    footer = (
        f"You get this email every {weekday} because you are subscribed to this saved view. Change or remove the "
        "subscription in Softwarden, or turn off saved view emails in your profile."
    )
    content: dict[str, Any] = render.email_table_content(
        heading,
        [c.label for c in columns],
        rows,
        intro=intro,
        more=more,
        footer=footer,
        link=saved_views.list_link(view),
        link_label="Open the view in Softwarden",
    )
    if not view.shared:
        content[PRIVATE_TO] = view.user_id
    return Composed(subject=subject[:300], content=content, total=found.total)


def _record_skip(org, sub: SavedViewSubscription, day: date, reason: str) -> None:
    Delivery.objects.bulk_create(
        [
            Delivery(
                organization=org,
                channel=Channel.EMAIL,
                kind=DeliveryKind.SUBSCRIPTION,
                status=DeliveryStatus.SKIPPED,
                dedupe_key=delivery_key(sub, day),
                recipient=sub.membership.user.email or "",
                subject=_view_subject(sub.saved_view)[:300],
                content={},
                last_error=reason[: deliveries.ERROR_MAX],
            )
        ],
        ignore_conflicts=True,  # a key already there (sent or skipped) stays
    )


def send_one(org, sub: SavedViewSubscription, day: date) -> str:
    """Queue (or record as skipped) `sub`'s email for `day`. Returns sent, skipped or "" (already handled)."""
    composed = compose(org, sub, day)
    with transaction.atomic():
        if isinstance(composed, Skip):
            _record_skip(org, sub, day, composed.reason)
            return DeliveryStatus.SKIPPED
        _delivery, created = deliveries.enqueue(
            org,
            channel=Channel.EMAIL,
            kind=DeliveryKind.SUBSCRIPTION,
            dedupe_key=delivery_key(sub, day),
            subject=composed.subject,
            content=composed.content,
            recipient=sub.membership.user.email,
        )
        if created:
            SavedViewSubscription.objects.filter(id=sub.id).update(last_sent=day, updated_at=timezone.now())
    return DeliveryStatus.SENT if created else ""


def run_for_org(org, now: datetime) -> Counter[str]:
    """The organization's subscriptions due on its local today, once its local time has reached the send time."""
    counts: Counter[str] = Counter()
    local = now.astimezone(ZoneInfo(org.timezone))
    if local.time() < rules.send_time(org):
        return counts
    day = local.date()
    subs = list(
        SavedViewSubscription.objects.filter(organization=org, active=True, weekday=day.weekday()).select_related(
            "saved_view", "saved_view__user", "membership__user"
        )
    )
    if not subs:
        return counts
    done = set(
        Delivery.objects.filter(organization=org, dedupe_key__in=[delivery_key(s, day) for s in subs]).values_list(
            "dedupe_key", flat=True
        )
    )
    for sub in subs:
        if delivery_key(sub, day) in done:
            continue
        try:
            outcome = send_one(org, sub, day)
        except Exception:  # one subscription's failure doesn't stop the others; logged by id only
            counts["failed"] += 1
            log.exception("saved view subscription failed organization=%s subscription=%s", org.id, sub.id)
            continue
        if outcome:
            counts[outcome] += 1
    return counts


def scheduled_run(now: datetime) -> dict[str, int]:
    """The `saved_views.subscriptions` schedule, hourly at :26: every organization on its local date. One
    organization's failure doesn't stop the others (logged with its id only)."""
    from core.models import active_organizations

    total: Counter[str] = Counter()
    for org in active_organizations():  # a suspended organization is skipped
        try:
            total.update(run_for_org(org, now))
        except Exception:
            total["failed"] += 1
            log.exception("saved view subscriptions failed organization=%s", org.id)
    return {f"subscriptions_{k}": total[k] for k in SCHEDULE_KEYS}


# ---- test send ------------------------------------------------------------------------------------------------------


TEST_SEND_INTERVAL = timedelta(minutes=1)  # one test email per subscription a minute (429 otherwise)


def _test_prefix(sub: SavedViewSubscription) -> str:
    return f"test:subscription:{sub.id.hex}"


def test_send(org, actor, sub: SavedViewSubscription) -> Delivery:
    """Send the email as it would go out today, now, to the subscriber (one attempt, kind `test`). Refused (409) with
    the reason when the weekly email wouldn't be sent, and (429) within a minute of the last test of this
    subscription, whoever asked. The subscriber or an Admin may ask."""
    from core.dates import org_today

    sub = find(org, actor, sub.id)
    since = timezone.now() - TEST_SEND_INTERVAL
    last = (
        Delivery.objects.filter(
            organization=org,
            kind=DeliveryKind.TEST,
            dedupe_key__startswith=f"{_test_prefix(sub)}:",
            created_at__gte=since,
        )
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    if last is not None:
        wait = max(1, int((last + TEST_SEND_INTERVAL - timezone.now()).total_seconds()) + 1)
        raise exceptions.Throttled(
            wait=wait, detail=f"A test email for this subscription was just sent. Try again in {wait} seconds."
        )
    composed = compose(org, sub, org_today(org))
    if isinstance(composed, Skip):
        raise Conflict(composed.reason.replace("Not sent: ", "Can't send: ", 1))
    delivery, _ = deliveries.enqueue(
        org,
        channel=Channel.EMAIL,
        kind=DeliveryKind.TEST,
        dedupe_key=f"{_test_prefix(sub)}:{uuid.uuid4().hex}",
        subject=composed.subject,
        content=composed.content,
        recipient=sub.membership.user.email,
        requested_by=actor,
        max_attempts=1,
        queue_job=False,
    )
    done = deliveries.attempt(delivery.id) or Delivery.objects.get(id=delivery.id)
    with transaction.atomic():
        audit(
            org,
            actor,
            "saved_view_subscription.tested",
            sub,
            {**_audited(sub), "ok": done.status == DeliveryStatus.SENT, "delivery": str(done.id)},
        )
    return done
