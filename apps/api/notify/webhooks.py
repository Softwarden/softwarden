"""Signed event webhooks. The plan's Webhook is notify.WebhookDestination: one model,
one delivery engine (notify/deliveries.py), one SSRF path (core/outbound.py) and one log (Delivery and its
DeliveryAttempt rows) for everything Softwarden posts to an HTTPS endpoint. What webhooks add is here:

- **The event catalog** (`EVENT_TYPES`, `FAMILIES`): what an endpoint may subscribe to, one type or a family
  wildcard (`record.*`). `ping` is the test event and can't be subscribed to. Notification rules post `reminder.due`
  and `reminder.overdue` to the destinations a rule names; those aren't subscriptions and aren't listed here.
- **Emission.** catalog/services.audit calls `after_audit` for every audited write; the actions in `AUDIT_EVENTS`
  become events. Discovery (`candidates_found`) and the reminder generator (`reminders_created`) emit theirs
  directly, since they write without an audit event. The payload is built while the write's transaction is open (the
  target may be deleted next) and recorded in the **outbox** (a WebhookEvent row) in that same transaction:
  a write that rolls back leaves no event, a write that commits always has one (nothing is lost between the commit
  and the queueing), and the request never waits on an endpoint. A failure to build an event is logged by type and
  never reaches the request. The worker's `webhooks.outbox` schedule drains the outbox every minute, before the tick
  claims jobs: each subscribed, enabled endpoint gets one Delivery (kind `event`, key `ev:<event>:<endpoint>`), queued
  in event order, whose `notify.deliver` job the same tick runs. Nothing relies on Django's commit hooks.
- **Payloads** never carry a secret: `changes` are the audit event's own, already redacted by key name
  (catalog/redaction.py), and redacted again with the snapshot denylist; people are referenced by id.
- **Endpoint services**: subscriptions are validated by `clean_events` (notify/channel_services.py saves them), secret
  rotation with a grace period during which both secrets sign (`rotate_secret`, `signing_secrets`), the delivery
  summary the worker keeps (`record_outcome`) and turning an endpoint off after a 410 Gone or N failed deliveries in a
  row (`auto_disable`, audited with no actor)."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.db.models import F
from django.utils import timezone

from core import credentials
from core.drf import timestamp
from core.problems import FieldErrors

log = logging.getLogger("softwarden.notify")

PING = "ping"
FAMILIES: dict[str, tuple[str, ...]] = {
    "record": ("record.created", "record.updated", "record.archived", "record.restored", "record.deleted"),
    "relationship": ("relationship.created", "relationship.updated", "relationship.deleted"),
    "renewal": (
        "renewal.created",
        "renewal.updated",
        "renewal.archived",
        "renewal.restored",
        "renewal.deleted",
        "renewal.decided",
        "renewal.completed",
    ),
    "discovery": ("discovery.candidate",),
    "reminder": (
        "reminder.created",
        "reminder.snoozed",
        "reminder.acknowledged",
        "reminder.reopened",
        "reminder.resolved",
    ),
}
EVENT_TYPES: tuple[str, ...] = tuple(t for types in FAMILIES.values() for t in types)
WILDCARDS: tuple[str, ...] = tuple(f"{family}.*" for family in FAMILIES)
SUBSCRIBABLE = frozenset(EVENT_TYPES) | frozenset(WILDCARDS)
EVENT_DESCRIPTIONS: dict[str, str] = {
    "record.created": "An application, component, integration or AI agent was added.",
    "record.updated": "A record was changed; `changes` names each field with its value before and after.",
    "record.archived": "A record was archived.",
    "record.restored": "A record was restored from the archive.",
    "record.deleted": "A record was deleted.",
    "relationship.created": "A relationship between two records was added.",
    "relationship.updated": "A relationship was changed.",
    "relationship.deleted": "A relationship was removed.",
    "renewal.created": "A renewal was added.",
    "renewal.updated": "A renewal or its open cycle was changed.",
    "renewal.archived": "A renewal was archived.",
    "renewal.restored": "A renewal was restored from the archive.",
    "renewal.deleted": "A renewal was deleted.",
    "renewal.decided": "A renewal decision was recorded on a cycle.",
    "renewal.completed": "A renewal cycle was completed.",
    "discovery.candidate": "A discovery source found an object that isn't in the catalog yet.",
    "reminder.created": "A reminder opened for a deadline.",
    "reminder.snoozed": "Someone snoozed a reminder.",
    "reminder.acknowledged": "Someone acknowledged a reminder.",
    "reminder.reopened": "Someone reopened a reminder.",
    "reminder.resolved": "Someone resolved a reminder.",
}
EVENTS_MAX = len(SUBSCRIBABLE)
GRACE_DEFAULT_HOURS = 24
GRACE_MAX_HOURS = 168
SIGNING_SECRET_MIN = 16
SECRET_MAX = 1000


def family_of(event_type: str) -> str:
    return event_type.split(".", 1)[0]


def subscribed(events: Iterable[str], event_type: str) -> bool:
    wanted = set(events)
    return event_type in wanted or f"{family_of(event_type)}.*" in wanted


def clean_events(value: Any, errors: dict[str, list[str]]) -> list[str] | None:
    """The subscriptions as stored (unique, in catalog order: wildcards first), or None with `errors["events"]` set."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors["events"] = ['Send a list of event types, such as ["record.updated", "reminder.*"].']
        return None
    unknown = sorted({v for v in value if v not in SUBSCRIBABLE})
    if unknown:
        errors["events"] = [
            f"Unknown event type: {', '.join(unknown)}. Choose from {', '.join(EVENT_TYPES)} or a family such as "
            "record.*."
        ]
        return None
    order = {name: i for i, name in enumerate((*WILDCARDS, *EVENT_TYPES))}
    return sorted(set(value), key=order.__getitem__)


# ---- payloads -------------------------------------------------------------------------------------------------------


def actor_json(actor) -> dict[str, Any] | str:
    """The person who made the change, or "system" for the worker, discovery and the reminder generator."""
    if actor is None or getattr(actor, "pk", None) is None:
        return "system"
    return {"id": int(actor.pk), "name": actor.get_full_name()}  # no email: an address isn't sent out


def _iso_date(value) -> str | None:
    return value.isoformat() if value else None


def changes_json(changes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{field: {"before", "after"}} from an audit payload's `[before, after]` pairs; anything else in the payload
    (markers such as `undo`, an acknowledgment's reason) is left out. Redacted again with the snapshot denylist, so a
    field named like a key, a hash or a credential never reaches an endpoint."""
    from catalog.redaction import SNAPSHOT_DENYLIST, redact

    found = {}
    for key, value in redact(changes or {}, SNAPSHOT_DENYLIST).items():
        if isinstance(value, list) and len(value) == 2:
            found[str(key)] = {"before": value[0], "after": value[1]}
    return found


def record_json(resource, *, deleted: bool = False) -> dict[str, Any]:
    from catalog.search import PATHS

    segment = PATHS.get(resource.kind)
    return {
        "id": str(resource.id),
        "kind": resource.kind,
        "name": resource.name,
        "lifecycle": resource.lifecycle,
        "archived": resource.archived_at is not None,
        "path": None if deleted or segment is None else f"/{segment}/{resource.id}",
    }


def _record_ref(resource) -> dict[str, Any] | None:
    if resource is None:
        return None
    return {"id": str(resource.id), "kind": resource.kind, "name": resource.name}


def relationship_json(rel) -> dict[str, Any]:
    return {
        "id": str(rel.id),
        "type": rel.type.slug,
        "source": _record_ref(rel.source),
        "source_instance": str(rel.source_instance_id) if rel.source_instance_id else None,
        "target": _record_ref(rel.target),
        "target_instance": str(rel.target_instance_id) if rel.target_instance_id else None,
        "purpose": rel.purpose,
    }


def renewal_json(obligation, *, deleted: bool = False) -> dict[str, Any]:
    vendor = obligation.vendor
    return {
        "id": str(obligation.id),
        "name": obligation.name,
        "vendor": {"id": str(vendor.id), "name": vendor.name} if vendor is not None else None,
        "auto_renew": obligation.auto_renew,
        "archived": obligation.archived_at is not None,
        "path": None if deleted else f"/renewals/{obligation.id}",
    }


def cycle_json(cycle) -> dict[str, Any]:
    return {
        "id": str(cycle.id),
        "renewal_date": _iso_date(cycle.renewal_date),
        "decision": cycle.decision or None,
        "decided_on": _iso_date(cycle.decided_on),
        "completed_on": _iso_date(cycle.completed_on),
    }


def reminder_json(reminder) -> dict[str, Any]:
    return {
        "id": str(reminder.id),
        "source_type": reminder.source_type,
        "source_id": str(reminder.source_id),
        "source_field": reminder.source_field,
        "label": reminder.label,
        "title": reminder.title,
        "due_date": _iso_date(reminder.due_date),
        "status": reminder.status,
        "snoozed_until": _iso_date(reminder.snoozed_until),
    }


def observation_json(observation, connector) -> dict[str, Any]:
    return {
        "id": str(observation.id),
        "kind": observation.kind,
        "display_name": observation.display_name,
        "external_id": observation.external_id,
        "ai_agent": observation.ai_agent,
        "first_seen_at": timestamp(observation.first_seen_at),
        "connector": {"id": str(connector.id), "name": connector.name, "source": connector.source},
        "path": f"/discovery/observations/{observation.id}",
    }


def _record_event(target, changes, action) -> dict[str, Any] | None:
    data: dict[str, Any] = {"record": record_json(target, deleted=action.endswith(".deleted"))}
    if action.endswith(".updated"):
        data["changes"] = changes_json(changes)
        if not data["changes"]:
            return None  # an edit that changed nothing
    return data


def _relationship_event(target, changes, action) -> dict[str, Any] | None:
    data: dict[str, Any] = {"relationship": relationship_json(target)}
    if action.endswith(".updated"):
        data["changes"] = changes_json(changes)
        if not data["changes"]:
            return None
    return data


def _renewal_event(target, changes, action) -> dict[str, Any] | None:
    from money.models import RenewalCycle

    if isinstance(target, RenewalCycle):
        data: dict[str, Any] = {"renewal": renewal_json(target.obligation), "cycle": cycle_json(target)}
    else:
        data = {"renewal": renewal_json(target, deleted=action.endswith(".deleted")), "cycle": None}
    if action.endswith(".updated") or action == "renewal_cycle.decided":
        data["changes"] = changes_json(changes)
        if not data["changes"] and action.endswith(".updated"):
            return None
    return data


def _reminder_event(target, changes, action) -> dict[str, Any] | None:
    return {"reminder": reminder_json(target)}


Builder = Callable[[Any, dict[str, Any], str], dict[str, Any] | None]
# Audit action -> (event type, payload builder(target, the audit's changes, action)).
AUDIT_EVENTS: dict[str, tuple[str, Builder]] = {
    "resource.created": ("record.created", _record_event),
    "resource.updated": ("record.updated", _record_event),
    "resource.archived": ("record.archived", _record_event),
    "resource.restored": ("record.restored", _record_event),
    "resource.deleted": ("record.deleted", _record_event),
    "relationship.created": ("relationship.created", _relationship_event),
    "relationship.updated": ("relationship.updated", _relationship_event),
    "relationship.deleted": ("relationship.deleted", _relationship_event),
    "renewal.created": ("renewal.created", _renewal_event),
    "renewal.updated": ("renewal.updated", _renewal_event),
    "renewal.archived": ("renewal.archived", _renewal_event),
    "renewal.restored": ("renewal.restored", _renewal_event),
    "renewal.deleted": ("renewal.deleted", _renewal_event),
    "renewal_cycle.updated": ("renewal.updated", _renewal_event),
    "renewal_cycle.decided": ("renewal.decided", _renewal_event),
    "renewal_cycle.completed": ("renewal.completed", _renewal_event),
    "reminder.snoozed": ("reminder.snoozed", _reminder_event),
    "reminder.acknowledged": ("reminder.acknowledged", _reminder_event),
    "reminder.reopened": ("reminder.reopened", _reminder_event),
    "reminder.resolved": ("reminder.resolved", _reminder_event),
}


# Builders that read only the target as loaded (no query), so they need no savepoint.
LOADED_BUILDERS = frozenset({_record_event, _reminder_event})


# ---- emission -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    id: uuid.UUID
    type: str
    occurred_at: str
    actor: dict[str, Any] | str
    data: dict[str, Any]

    def content(self) -> dict[str, Any]:
        """A delivery's `content`: what notify/channels.webhook_body wraps in the envelope."""
        return {
            "event": self.type,
            "id": str(self.id),
            "occurred_at": self.occurred_at,
            "actor": self.actor,
            "data": self.data,
        }


def make_event(event_type: str, data: dict[str, Any], actor=None) -> Event:
    return Event(uuid.uuid4(), event_type, timestamp(timezone.now()) or "", actor_json(actor), data)


# Seconds a unit of work trusts its cached subscriptions: another process may add an endpoint meanwhile, and a write
# in that window isn't posted to it (the per-transaction cache of earlier versions trusted it for the whole transaction).
SUBSCRIBERS_TTL = 5.0


def forget_subscribers(org) -> None:
    """An endpoint of `org` changed: the current unit of work reads its subscriptions again."""
    from core.request_context import organization_cache

    organization_cache(org.pk).pop("webhook_subscriptions", None)


def _subscribed_types(org_id) -> frozenset[str]:
    from .models import WebhookDestination

    found: set[str] = set()
    for events in (
        WebhookDestination.objects.filter(organization_id=org_id, enabled=True)
        .exclude(events=[])
        .values_list("events", flat=True)
    ):
        found.update(events)
    return frozenset(found)


def wanted(org, event_type: str) -> bool:
    """Whether any enabled endpoint of `org` subscribes to `event_type`, checked before any payload is built. The
    answer is cached for the unit of work (a request, a job, a schedule run; core/request_context) for up to
    SUBSCRIBERS_TTL seconds, so a bulk edit costs one query per organization; an endpoint saved in the same unit calls
    forget_subscribers. No query at all without the `webhooks` capability."""
    from core.capabilities import WEBHOOKS, has_capability
    from core.request_context import organization_cache

    if not has_capability(org, WEBHOOKS):
        return False
    cache = organization_cache(org.pk)
    held = cache.get("webhook_subscriptions")
    clock = time.monotonic()
    if held is None or clock - held[0] > SUBSCRIBERS_TTL:
        held = (clock, _subscribed_types(org.pk))
        cache["webhook_subscriptions"] = held
    return subscribed(held[1], event_type)


def after_audit(org, actor, action: str, target, changes: dict[str, Any]) -> None:
    """Called by catalog/services.audit inside the write's transaction with the audit event's payload (already
    redacted). When an endpoint wants the event, builds it now (a builder that reads related rows runs in a
    savepoint, so a failure can't spoil the write's transaction) and records it in the outbox, in the same
    transaction. A failure to build is logged and never reaches the write."""
    spec = AUDIT_EVENTS.get(action)
    if spec is None or target is None:
        return
    event_type, build = spec
    try:
        if not wanted(org, event_type):
            return
        if build in LOADED_BUILDERS:
            data = build(target, changes, action)
        else:  # it reads related rows: a database error stays inside its savepoint
            with transaction.atomic():
                data = build(target, changes, action)
    except Exception as error:  # an event is never worth failing the user's write
        log.warning("webhook event not built action=%s error=%s", action, type(error).__name__)
        return
    if data is not None:
        queue(org, [make_event(event_type, data, actor)])


def queue(org, events: list[Event]) -> int:
    """Record `events` in the outbox (WebhookEvent rows, one INSERT) in the current transaction, or at once outside
    one; the worker's `webhooks.outbox` schedule posts them. A write that rolls back (or the savepoint around it)
    takes its events with it, and a write that commits always has them: there is no gap between the commit and the
    queueing. An event too large to store even with its `changes` cut is logged and left out. Returns how many were
    recorded.

    The INSERT is part of the write and has no savepoint of its own: a database error here fails the write, as an
    error on any of its own rows would (a savepoint would cost two more queries per event, and a transaction that
    can't insert one small row can't commit the write either). Anything else is logged and never reaches the write."""
    from .models import WebhookEvent

    if not events:
        return 0
    rows: list[WebhookEvent] = []
    for event in events:
        try:
            fit = fitted(event)
            if fit is not None:
                rows.append(WebhookEvent(id=fit.id, organization_id=org.pk, event_type=fit.type, content=fit.content()))
        except Exception as error:
            log.warning("webhook event skipped type=%s id=%s error=%s", event.type, event.id, type(error).__name__)
    if rows:
        WebhookEvent.objects.bulk_create(rows, batch_size=500)
    return len(rows)


CONTENT_MAX_BYTES = 256 * 1024  # notify/deliveries.CONTENT_MAX_BYTES


def fitted(event: Event) -> Event | None:
    """The event, with `changes` cut to the field names when the payload is too large to store; None when it is still
    too large."""
    if len(json.dumps(event.content(), default=str).encode()) <= CONTENT_MAX_BYTES:
        return event
    data = dict(event.data)
    if isinstance(data.get("changes"), dict):
        data["changes"] = {"truncated": True, "fields": sorted(data["changes"])}
    smaller = Event(event.id, event.type, event.occurred_at, event.actor, data)
    if len(json.dumps(smaller.content(), default=str).encode()) <= CONTENT_MAX_BYTES:
        log.warning("webhook event truncated type=%s id=%s", event.type, event.id)
        return smaller
    log.warning("webhook event too large type=%s id=%s", event.type, event.id)
    return None


def deliver(org_id, events: list[Event]) -> int:
    """Queue one delivery per event and subscribed, enabled endpoint (the outbox drain calls it with one
    organization's events, oldest first): the organization, its capability and its endpoints read once, the deliveries
    and their jobs written with two bulk inserts, in event order. Each job is due a microsecond after the one before,
    so the worker (which claims the earliest due job first) starts each endpoint's deliveries in the order the events
    happened; a retry waits its backoff and doesn't hold back later events. Returns how many were queued. One bad
    event or endpoint is logged (type and id) and skipped; nothing else is raised."""
    from core.capabilities import WEBHOOKS, has_capability
    from core.models import Job, Organization

    from . import deliveries
    from .models import Channel, Delivery, DeliveryKind, WebhookDestination

    try:
        types = {t for e in events for t in (e.type, f"{family_of(e.type)}.*")}
        endpoints = list(
            WebhookDestination.objects.filter(organization_id=org_id, enabled=True, events__overlap=sorted(types))
        )
        if not endpoints:
            return 0
        org = Organization.objects.filter(pk=org_id).first()
        if org is None or not has_capability(org, WEBHOOKS):
            return 0
    except Exception as error:
        log.warning("webhook events not queued organization=%s error=%s", org_id, type(error).__name__)
        return 0
    now = timezone.now()
    attempts = max_attempts()
    rows: list[Delivery] = []
    for event in events:
        try:
            fit = fitted(event)
            if fit is None:
                continue
            content = fit.content()
            for endpoint in endpoints:
                try:
                    if subscribed(endpoint.events, event.type):
                        rows.append(
                            Delivery(
                                organization_id=org_id,
                                channel=Channel.WEBHOOK,
                                kind=DeliveryKind.EVENT,
                                dedupe_key=f"ev:{event.id.hex}:{endpoint.id.hex}",
                                webhook_destination=endpoint,
                                destination_name=endpoint.name,
                                subject=event.type,
                                content=content,
                                max_attempts=attempts,
                                next_attempt_at=now,
                                event_type=event.type,
                                event_id=event.id,
                            )
                        )
                except Exception as error:
                    log.warning(
                        "webhook delivery not built event=%s endpoint=%s error=%s",
                        event.id,
                        endpoint.id,
                        type(error).__name__,
                    )
        except Exception as error:
            log.warning("webhook event skipped type=%s id=%s error=%s", event.type, event.id, type(error).__name__)
    if not rows:
        return 0
    try:
        with transaction.atomic():
            Delivery.objects.bulk_create(rows, batch_size=500)
            Job.objects.bulk_create(
                [
                    Job(
                        organization_id=org_id,
                        kind=deliveries.JOB_KIND,
                        payload={"delivery_id": str(row.id)},
                        run_after=now + timedelta(microseconds=position),
                        max_attempts=1,
                    )
                    for position, row in enumerate(rows)
                ],
                batch_size=500,
            )
        return len(rows)
    except Exception as error:  # a clash in the batch: one at a time, so the others still go
        log.warning("webhook bulk queue failed organization=%s error=%s", org_id, type(error).__name__)
    queued = 0
    for row in rows:
        try:
            _delivery, created = deliveries.enqueue(
                org,
                channel=Channel.WEBHOOK,
                kind=DeliveryKind.EVENT,
                dedupe_key=row.dedupe_key,
                subject=row.subject,
                content=row.content,
                webhook_destination=row.webhook_destination,
                event_id=row.event_id,
                max_attempts=attempts,
            )
            queued += int(created)
        except Exception as error:
            log.warning("webhook delivery not queued event=%s error=%s", row.event_id, type(error).__name__)
    return queued


def candidates_found(connector, observations: list[Any]) -> None:
    """discovery/engine._apply: new candidates a run found, one `discovery.candidate` event each, recorded in the
    run's transaction. Raises only a database error (the transaction is broken then anyway)."""
    try:
        org = connector.organization
        if observations and wanted(org, "discovery.candidate"):
            queue(
                org,
                [
                    make_event("discovery.candidate", {"observation": observation_json(o, connector)})
                    for o in observations
                ],
            )
    except DatabaseError:
        raise  # the transaction is broken: the run fails as it would on its own rows (queue)
    except Exception as error:
        log.warning("discovery.candidate events not built connector=%s error=%s", connector.pk, type(error).__name__)


def reminders_created(org, reminders: list[Any]) -> None:
    """notify/reminders.sync: reminders the generator opened, one `reminder.created` event each. Raises only a
    database error, as candidates_found."""
    try:
        if reminders and wanted(org, "reminder.created"):
            queue(org, [make_event("reminder.created", {"reminder": reminder_json(r)}) for r in reminders])
    except DatabaseError:
        raise  # as candidates_found
    except Exception as error:
        log.warning("reminder.created events not built organization=%s error=%s", org.pk, type(error).__name__)


# ---- the outbox drain (the worker) -------------------------------------------------------------------------------

DRAIN_BATCH = 500
DRAIN_BUDGET = 30.0  # seconds one schedule run keeps draining; what is left waits for the next tick
# pg_try_advisory_xact_lock key ("swhookob"): one drain at a time, so two workers never fan out out of order.
DRAIN_LOCK = 0x7377686F6F6B6F62


def _event_of(row) -> Event:
    content = row.content or {}
    return Event(
        row.id,
        row.event_type,
        str(content.get("occurred_at") or ""),
        content.get("actor") or "system",
        content.get("data") or {},
    )


def drain_once(organization_id=None, limit: int = DRAIN_BATCH) -> int | None:
    """One batch of the outbox, oldest first: each organization's events fanned out into deliveries (deliver) and the
    rows deleted, in one transaction, so an event becomes its deliveries exactly once: a crash before the commit leaves
    the rows for the next drain, and nothing is posted twice. Returns how many events were taken, or None when another
    drain holds the lock. `organization_id` limits it to one organization (the e2e helper)."""
    from .models import WebhookEvent

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [DRAIN_LOCK])
            if not cursor.fetchone()[0]:
                return None
        # A suspended organization's events stay in the outbox until it is unsuspended.
        from core.models import suspended_organization_ids

        rows = WebhookEvent.objects.exclude(organization_id__in=suspended_organization_ids()).order_by("sequence")
        if organization_id is not None:
            rows = rows.filter(organization_id=organization_id)
        taken = list(rows[:limit])
        if not taken:
            return 0
        group: list[Event] = []
        group_org = taken[0].organization_id
        for row in taken:
            if row.organization_id != group_org:  # consecutive runs of one organization keep the overall order
                deliver(group_org, group)
                group, group_org = [], row.organization_id
            group.append(_event_of(row))
        deliver(group_org, group)
        WebhookEvent.objects.filter(id__in=[row.id for row in taken]).delete()
    return len(taken)


def drain(now=None, organization_id=None) -> dict[str, int]:
    """The `webhooks.outbox` schedule, every minute and before the tick claims jobs, so an event committed before a
    tick is posted by that tick (as commit hooks did in earlier versions). Drains batches until the outbox is empty or
    DRAIN_BUDGET is spent."""
    clock = time.monotonic()
    drained = 0
    while time.monotonic() - clock < DRAIN_BUDGET:
        taken = drain_once(organization_id)
        if not taken:
            break
        drained += taken
    return {"webhook_events_drained": drained}


# ---- settings -------------------------------------------------------------------------------------------------------


def max_attempts() -> int:
    """Attempts per event delivery (SOFTWARDEN_WEBHOOK_MAX_ATTEMPTS, default 6: the first and five retries)."""
    return min(max(int(settings.SOFTWARDEN_WEBHOOK_MAX_ATTEMPTS), 1), 20)


def disable_after() -> int:
    """Failed event deliveries in a row that turn an endpoint off (SOFTWARDEN_WEBHOOK_DISABLE_AFTER, default 5; 0
    never)."""
    return max(int(settings.SOFTWARDEN_WEBHOOK_DISABLE_AFTER), 0)


# ---- signing secrets and rotation ----------------------------------------------------------------------------------


def signing_secrets(destination, now=None) -> list[str]:
    """The secrets that sign a post now: the current one, then the previous one while its grace period lasts."""
    now = now or timezone.now()
    found = [credentials.slot_value(destination, "signing_secret")]
    expires = destination.previous_secret_expires_at
    if expires is not None and expires > now and credentials.slot_configured(destination, "previous_signing_secret"):
        found.append(credentials.slot_value(destination, "previous_signing_secret"))
    return found


def clear_previous(destination) -> None:
    destination.previous_signing_secret_ciphertext = ""
    destination.previous_signing_secret_key_id = ""
    destination.previous_signing_secret_set_at = None
    destination.previous_secret_expires_at = None


@transaction.atomic
def rotate_secret(org, actor, destination, data: dict[str, Any]):
    """Replace the signing secret, keeping the old one signing beside it for `grace_period_hours` (0 to 168, default
    24; 0 stops it at once), so receivers can switch without dropping events. Audited (which slots, never values)."""
    from catalog.services import audit

    from .channel_services import _secret, _store
    from .models import WebhookDestination

    errors: dict[str, list[str]] = {}
    for name in sorted(set(data) - {"signing_secret", "grace_period_hours"}):
        errors[name] = ["This field isn't accepted here."]
    grace = data.get("grace_period_hours", GRACE_DEFAULT_HOURS)
    if isinstance(grace, bool) or not isinstance(grace, int) or not 0 <= grace <= GRACE_MAX_HOURS:
        errors["grace_period_hours"] = [f"Enter whole hours from 0 to {GRACE_MAX_HOURS}."]
    value = _secret(data.get("signing_secret"), "signing_secret", errors, SIGNING_SECRET_MIN)
    if errors:
        raise FieldErrors(errors)
    destination = WebhookDestination.objects.select_for_update().get(pk=destination.pk, organization=org)
    now = timezone.now()
    if grace and destination.signing_secret_ciphertext:
        destination.previous_signing_secret_ciphertext = destination.signing_secret_ciphertext
        destination.previous_signing_secret_key_id = destination.signing_secret_key_id
        destination.previous_signing_secret_set_at = destination.signing_secret_set_at
        destination.previous_secret_expires_at = now + timedelta(hours=grace)
    else:
        clear_previous(destination)
    assert value is not None
    _store(destination, "signing_secret", value, errors, "signing_secret")
    if errors:
        raise FieldErrors(errors)
    destination.save()
    audit(
        org,
        actor,
        "webhook_destination.secret_rotated",
        destination,
        {
            "name": destination.name,
            "replaced": ["signing_secret"],
            "grace_period_hours": grace,
            "previous_until": timestamp(destination.previous_secret_expires_at),
        },
    )
    return destination


# ---- delivery summary and auto-disable (the worker) --------------------------------------------------------------


def record_outcome(delivery, outcome: str, status_code: int | None, *, final_failure: bool, gone: bool) -> None:
    """After an attempt at a webhook delivery: the endpoint's last delivery summary, and for an event delivery the
    failure count (reset by a success; one per delivery that ended failed). A 410 Gone, or reaching
    `disable_after()` failures in a row, turns the endpoint off; both only for an event delivery (kind `event`): a
    ping or a notification rule's notice that fails, even with a 410, changes only the summary. Never raises: the delivery's own outcome is already recorded."""
    from .models import DeliveryKind, WebhookDestination

    endpoint_id = delivery.webhook_destination_id
    if endpoint_id is None:
        return
    try:
        fields: dict[str, Any] = {
            "last_delivery_at": timezone.now(),
            "last_delivery_outcome": outcome,
            "last_delivery_status_code": status_code,
            "last_delivery_id": delivery.id,
        }
        counted = delivery.kind == DeliveryKind.EVENT  # pings, resent pings and rule notices change the summary only
        if counted and outcome == "sent":
            fields["consecutive_failures"] = 0
        elif counted and final_failure:
            fields["consecutive_failures"] = F("consecutive_failures") + 1
        WebhookDestination.objects.filter(id=endpoint_id).update(**fields)
        if not (counted and (final_failure or gone)):
            return
        endpoint = WebhookDestination.objects.filter(id=endpoint_id).first()
        if endpoint is None or not endpoint.enabled:
            return
        limit = disable_after()
        if gone:
            auto_disable(endpoint, "The endpoint answered 410 Gone, so Softwarden stopped sending to it.")
        elif limit and endpoint.consecutive_failures >= limit:
            auto_disable(
                endpoint,
                f"The last {endpoint.consecutive_failures} deliveries failed after their retries, so Softwarden "
                "stopped sending to this endpoint.",
            )
    except Exception as error:
        log.warning("webhook summary not recorded delivery=%s error=%s", delivery.id, type(error).__name__)


@transaction.atomic
def auto_disable(endpoint, reason: str) -> bool:
    """Turn an endpoint off with the reason (shown on the endpoint and in the Admin banner); audited with no actor.
    False when it was already off."""
    from catalog.services import audit

    from .models import WebhookDestination

    row = WebhookDestination.objects.select_for_update().select_related("organization").get(pk=endpoint.pk)
    if not row.enabled:
        return False
    row.enabled = False
    row.disabled_at = timezone.now()
    row.disabled_reason = reason[:300]
    row.save(update_fields=["enabled", "disabled_at", "disabled_reason", "updated_at"])
    forget_subscribers(row.organization)
    audit(
        row.organization,
        None,
        "webhook_destination.disabled",
        row,
        {"name": row.name, "reason": row.disabled_reason, "failures": row.consecutive_failures},
    )
    log.info("webhook endpoint turned off id=%s failures=%s", row.id, row.consecutive_failures)
    return True


def problem(org) -> dict[str, Any] | None:
    """For the Admin banner (`auth/me` `webhook_problem`): endpoints Softwarden turned off, or None."""
    from .models import WebhookDestination

    off = WebhookDestination.objects.filter(organization=org, enabled=False, disabled_at__isnull=False)
    rows = list(off.order_by("-disabled_at").values("id", "name", "disabled_at", "disabled_reason")[:10])
    if not rows:
        return None
    return {
        "disabled": off.count(),
        "endpoints": [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "disabled_at": timestamp(r["disabled_at"]),
                "reason": r["disabled_reason"],
            }
            for r in rows
        ],
    }
