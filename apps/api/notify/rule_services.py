"""Writes for notification rules: create, change and delete a rule, seed the built-ins, and send a test.
Admin only (the views declare it); every write is validated, scoped to the organization and audited in one transaction.
Built-in rules are seeded with every organization (no audit event: part of creating it, like its relationship
types) and can be changed but not deleted. The daily digest's rule takes only its name, `enabled` and `digest_time`,
which is the organization's digest time (one column, also set in organization settings).

Defaults (BUILTINS): renewals and contracts at 90, 60, 30 and 7 days before; record reviews on their due date;
periodic reviews 30 days before and on the date; custom dates 30 and 7 days before; credential expiry 30, 7 and 1
days before; vendor risk documents 60, 30 and 7 days before; each emails the routed owner (else the IT
coordinator), no Teams or webhook, no escalation (the daily digest already lists overdue work every day); and the
daily digest, on. The exception is End of life: 90, 30 and 7 days before, then every 30 days while the
version is past its end of life (`repeat_every_days`, notify 0013; End of life isn't in the digest), until it is
acknowledged (`escalate_when_acknowledged` off), done, or no longer end of life."""

from __future__ import annotations

import uuid
from datetime import time
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models.signals import post_save

from catalog.models import RECORD_KIND_CHOICES, Criticality, Person, Team, Vendor
from catalog.services import audit
from core import limits
from core.dates import org_today
from core.models import Organization, Role
from core.problems import Conflict, FieldErrors

from . import deliveries, render, rules
from .models import (
    Channel,
    Delivery,
    DeliveryKind,
    DeliveryStatus,
    NotificationRule,
    NotificationRuleEscalationPerson,
    NotificationRulePerson,
    NotificationRuleTeamsDestination,
    NotificationRuleWebhookDestination,
    ReminderSource,
    RuleKind,
    TeamsDestination,
    WebhookDestination,
)

NAME_MAX = 200
LIST_MAX = 10
DAYS_MAX = 365
VALUE_MAX = Decimal("999999999999.99")
DIGEST_KEY = "daily_digest"

# (builtin_key, name, kind, source types, lead times, escalation days). Edit for new organizations only: migration
# notify 0005 keeps its own frozen copy, and existing organizations keep what they have.
BUILTINS: tuple[tuple[str, str, str, list[str], list[int], list[int]], ...] = (
    (
        "renewals_contracts",
        "Renewals and contracts",
        RuleKind.REMINDERS,
        [ReminderSource.RENEWAL_CYCLE, ReminderSource.CONTRACT],
        [90, 60, 30, 7],
        [],
    ),
    ("record_reviews", "Record reviews", RuleKind.REMINDERS, [ReminderSource.RECORD_REVIEW], [0], []),
    ("periodic_reviews", "Periodic reviews", RuleKind.REMINDERS, [ReminderSource.PERIODIC_REVIEW], [30, 0], []),
    ("custom_dates", "Custom dates", RuleKind.REMINDERS, [ReminderSource.CUSTOM_DATE], [30, 7], []),
    ("credentials", "Credential expiry", RuleKind.REMINDERS, [ReminderSource.CREDENTIAL_EXPIRY], [30, 7, 1], []),
    # (notify 0011 seeds it for the organizations that existed before).
    ("vendor_risk", "Vendor risk documents", RuleKind.REMINDERS, [ReminderSource.VENDOR_RISK], [60, 30, 7], []),
    # (notify 0012 seeds it for the organizations that existed before). End of life left the digest with it.
    ("end_of_life", "End of life", RuleKind.REMINDERS, [ReminderSource.END_OF_LIFE], [90, 30, 7], []),
    (DIGEST_KEY, "Daily digest", RuleKind.DIGEST, [], [], []),
)
# Settings a built-in has besides the defaults above (notify 0013 gives them to the End of life rules that exist).
BUILTIN_EXTRAS: dict[str, dict[str, Any]] = {
    "end_of_life": {"repeat_every_days": 30, "escalate_when_acknowledged": False},
}

REMINDER_FIELDS = (
    "name",
    "enabled",
    "source_types",
    "lead_times_days",
    "escalation_days",
    "repeat_every_days",
    "escalate_when_acknowledged",
    "send_email",
    "teams_destinations",
    "webhook_destinations",
    "notify_routed",
    "people",
    "team",
    "roles",
    "escalation_people",
    "escalation_team",
    "escalation_roles",
    "fallback_team",
    "fallback_person",
    "criticalities",
    "record_kinds",
    "vendor",
    "min_annual_value",
)
DIGEST_FIELDS = ("name", "enabled", "digest_time")
M2M = {
    "people": Person,
    "escalation_people": Person,
    "teams_destinations": TeamsDestination,
    "webhook_destinations": WebhookDestination,
}
THROUGH: dict[str, tuple[Any, str]] = {
    "people": (NotificationRulePerson, "person_id"),
    "escalation_people": (NotificationRuleEscalationPerson, "person_id"),
    "teams_destinations": (NotificationRuleTeamsDestination, "destination_id"),
    "webhook_destinations": (NotificationRuleWebhookDestination, "destination_id"),
}
FKS = {
    "team": Team,
    "escalation_team": Team,
    "fallback_team": Team,
    "fallback_person": Person,
    "vendor": Vendor,
}


# ---- seeding -------------------------------------------------------------------------------------------------------


def seed(org) -> int:
    """Add the built-in rules the organization doesn't have yet (by key), already baselined. Returns how many."""
    from django.utils import timezone

    now = timezone.now()
    have = set(
        NotificationRule.objects.filter(organization=org).exclude(builtin_key="").values_list("builtin_key", flat=True)
    )
    taken = {n.lower() for n in NotificationRule.objects.filter(organization=org).values_list("name", flat=True)}
    rows = []
    for key, name, kind, types, leads, steps in BUILTINS:
        if key in have:
            continue
        label = name if name.lower() not in taken else f"{name} (built-in)"
        rows.append(
            NotificationRule(
                organization=org,
                builtin_key=key,
                name=label,
                kind=kind,
                source_types=list(types),
                lead_times_days=list(leads),
                escalation_days=list(steps),
                baselined_at=now,  # a new organization has nothing in flight
                **BUILTIN_EXTRAS.get(key, {}),
            )
        )
    NotificationRule.objects.bulk_create(rows)
    return len(rows)


def _on_organization_saved(sender, instance, created=False, raw=False, **kwargs):
    if created and not raw:
        seed(instance)


def connect() -> None:
    """Called once from NotifyConfig.ready: every new organization starts with the built-in rules."""
    post_save.connect(_on_organization_saved, sender=Organization, dispatch_uid="notify-seed-rules")


def digest_rule(org) -> NotificationRule | None:
    return NotificationRule.objects.filter(organization=org, builtin_key=DIGEST_KEY).first()


# ---- validation ----------------------------------------------------------------------------------------------------


def _bool(data, name, errors) -> bool | None:
    value = data[name]
    if not isinstance(value, bool):
        errors[name] = ["Use true or false."]
        return None
    return value


def _days(data, name, errors, low: int) -> list[int] | None:
    value = data[name]
    if not isinstance(value, list) or any(isinstance(v, bool) or not isinstance(v, int) for v in value):
        errors[name] = ["Enter a list of whole numbers of days."]
        return None
    if any(v < low or v > DAYS_MAX for v in value):
        errors[name] = [f"Use days from {low} to {DAYS_MAX}."]
        return None
    if len(set(value)) > LIST_MAX:
        errors[name] = [f"Use at most {LIST_MAX} values."]
        return None
    return sorted(set(value), reverse=name == "lead_times_days")


def _choices(data, name, errors, allowed: list[str], what: str) -> list[str] | None:
    value = data[name]
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        errors[name] = [f"Enter a list of {what}."]
        return None
    wrong = [v for v in value if v not in allowed]
    if wrong:
        errors[name] = [f"Use {', '.join(allowed)}."]
        return None
    return [v for v in allowed if v in value]  # stable order, no repeats


def _row(org, model, value, name, errors, keep=frozenset()):
    """The organization's row with this id, or a field error. A person must be active, unless the rule already names
    them (`keep`): deactivating someone doesn't make every later edit of the rule fail."""
    if value is None or value == "":
        return None
    try:
        pk = uuid.UUID(str(value))
    except ValueError:
        errors[name] = ["Choose one from this organization."]
        return None
    found = model.objects.filter(organization=org, pk=pk).first()
    if found is None:
        errors[name] = ["Choose one from this organization."]
    elif model is Person and (found.anonymized_at is not None or not found.active) and found.pk not in keep:
        errors[name] = ["This person is inactive."]
        return None
    return found


def _rows(org, model, data, name, errors, keep=frozenset()) -> list[Any] | None:
    value = data[name]
    if not isinstance(value, list) or len(value) > 100:
        errors[name] = ["Enter a list of at most 100 IDs."]
        return None
    found = []
    for item in value:
        row = _row(org, model, item, name, errors, keep)
        if name in errors:
            return None
        if row is not None and row not in found:
            found.append(row)
    return found


def _decimal(value, name, errors) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        errors[name] = ["Enter an amount such as 10000."]
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        errors[name] = ["Enter an amount such as 10000."]
        return None
    if not amount.is_finite():  # "NaN" quantizes without complaint, then refuses every comparison
        errors[name] = ["Enter an amount such as 10000."]
        return None
    if amount < 0 or amount > VALUE_MAX:
        errors[name] = ["Enter an amount of 0 or more."]
        return None
    return amount


def _apply(org, rule: NotificationRule, data: dict[str, Any], creating: bool) -> tuple[dict[str, list[Any]], Any]:
    """Validate and set the fields present. Returns the many-to-many sets to write after the save, and a new digest
    time (or None)."""
    errors: dict[str, list[str]] = {}
    digest = rule.kind == RuleKind.DIGEST
    allowed = DIGEST_FIELDS if digest else REMINDER_FIELDS
    for name in sorted(set(data) - set(allowed)):
        errors[name] = [
            "The daily digest rule has only a name, on or off, and a time."
            if digest
            else "This field isn't a setting of the rule."
        ]
    if "name" in data or creating:
        value = data.get("name")
        name = value.strip() if isinstance(value, str) else ""
        if not name:
            errors["name"] = ["Enter a name."]
        elif len(name) > NAME_MAX:
            errors["name"] = [f"Use {NAME_MAX} characters or fewer."]
        elif NotificationRule.objects.filter(organization=org, name__iexact=name).exclude(pk=rule.pk).exists():
            errors["name"] = ["Another rule already has this name."]
        rule.name = name
    for name in ("enabled", "escalate_when_acknowledged", "send_email", "notify_routed"):
        if name in data and name in allowed:
            value = _bool(data, name, errors)
            if value is not None:
                setattr(rule, name, value)
    if "source_types" in data:
        found = _choices(data, "source_types", errors, list(ReminderSource.values), "reminder types")
        if found is not None:
            rule.source_types = found
    for name, low in (("lead_times_days", 0), ("escalation_days", 1)):
        if name in data:
            days = _days(data, name, errors, low)
            if days is not None:
                setattr(rule, name, days)
    if "repeat_every_days" in data:
        value = data["repeat_every_days"]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= DAYS_MAX):
            errors["repeat_every_days"] = [f"Enter a whole number of days from 1 to {DAYS_MAX}, or nothing."]
        else:
            rule.repeat_every_days = value
    for name in ("roles", "escalation_roles"):
        if name in data:
            found = _choices(data, name, errors, list(Role.values), "roles")
            if found is not None:
                setattr(rule, name, found)
    if "criticalities" in data:
        found = _choices(data, "criticalities", errors, list(Criticality.values), "criticalities")
        if found is not None:
            rule.criticalities = found
    if "record_kinds" in data:
        found = _choices(data, "record_kinds", errors, list(RECORD_KIND_CHOICES), "record kinds")
        if found is not None:
            rule.record_kinds = found
    for name, model in FKS.items():
        if name in data and name in allowed:
            row = _row(org, model, data[name], name, errors, {getattr(rule, f"{name}_id")} - {None})
            if name not in errors:
                setattr(rule, name, row)
    if "min_annual_value" in data:
        amount = _decimal(data["min_annual_value"], "min_annual_value", errors)
        if "min_annual_value" not in errors:
            rule.min_annual_value = amount
    sets: dict[str, list[Any]] = {}
    for name, model in M2M.items():
        if name in data and name in allowed:
            keep = frozenset() if rule._state.adding else frozenset(getattr(rule, name).values_list("pk", flat=True))
            found = _rows(org, model, data, name, errors, keep)
            if found is not None:
                sets[name] = found
    new_time = None
    if digest and "digest_time" in data:
        value = data["digest_time"]
        try:
            new_time = time.fromisoformat(value) if isinstance(value, str) and len(value) >= 4 else None
        except ValueError:
            new_time = None
        if new_time is None:
            errors["digest_time"] = ["Enter a time such as 08:00."]
    if not digest and not errors:
        _check_whole(rule, sets, errors)
    if errors:
        raise FieldErrors(errors)
    return sets, new_time


def _current(rule: NotificationRule, sets: dict[str, list[Any]], name: str) -> list[Any]:
    if name in sets:
        return sets[name]
    return [] if rule._state.adding else list(getattr(rule, name).all())


def _check_whole(rule: NotificationRule, sets: dict[str, list[Any]], errors: dict[str, list[str]]) -> None:
    """The rule as a whole must be able to send something."""
    if not rule.source_types:
        errors["source_types"] = ["Choose at least one kind of reminder."]
    if not rule.lead_times_days and not rule.escalation_days and not rule.repeat_every_days:
        errors["lead_times_days"] = ["Enter at least one lead time, escalation step or repeat."]
    teams = _current(rule, sets, "teams_destinations")
    hooks = _current(rule, sets, "webhook_destinations")
    if not (rule.send_email or teams or hooks):
        errors["send_email"] = ["Choose at least one channel: email, a Teams channel or a webhook."]
    if rule.send_email:
        anyone = (
            rule.notify_routed
            or rule.team_id
            or rule.roles
            or _current(rule, sets, "people")
            or rule.escalation_team_id
            or rule.escalation_roles
            or _current(rule, sets, "escalation_people")
        )
        if not anyone:
            errors["notify_routed"] = ["Choose who gets the email: the routed owner, people, a team or a role."]


def _named(rows) -> list[dict[str, str]]:
    """Destinations as the audit log names them: id and name, never the address."""
    return sorted(({"id": str(r.pk), "name": r.name} for r in rows), key=lambda e: (e["name"].lower(), e["id"]))


def _write_sets(org, rule: NotificationRule, sets: dict[str, list[Any]]) -> None:
    for name, rows in sets.items():
        getattr(rule, name).set(rows, through_defaults={"organization": org})


def _set_digest_time(org, actor, new_time) -> None:
    fresh = Organization.objects.select_for_update().get(pk=org.pk)
    if new_time is None or fresh.digest_time == new_time:
        return
    before = fresh.digest_time
    fresh.digest_time = new_time
    fresh.save(update_fields=["digest_time"])
    org.digest_time = new_time
    audit(
        fresh,
        actor,
        "organization.updated",
        fresh,
        {"digest_time": [before.strftime("%H:%M"), new_time.strftime("%H:%M")]},
    )


# ---- writes --------------------------------------------------------------------------------------------------------


@transaction.atomic
def create_rule(org, actor, data: dict[str, Any]) -> NotificationRule:
    """A reminders rule (the daily digest is a built-in; there is only one)."""
    if data.get("kind", RuleKind.REMINDERS) != RuleKind.REMINDERS:
        raise FieldErrors({"kind": ["Only reminder rules can be added; the daily digest is built in."]})
    data = {k: v for k, v in data.items() if k != "kind"}
    rule = NotificationRule(organization=org, kind=RuleKind.REMINDERS)
    sets, _time = _apply(org, rule, data, creating=True)
    limits.check_room(org, limits.OTHER_RECORDS)
    try:
        with transaction.atomic():
            rule.save()
    except IntegrityError:
        raise FieldErrors({"name": ["Another rule already has this name."]}) from None
    _write_sets(org, rule, sets)
    changes: dict[str, Any] = {"name": rule.name}
    if sets.get("webhook_destinations"):
        changes["webhook_destinations"] = _named(sets["webhook_destinations"])
    audit(org, actor, "notification_rule.created", rule, changes)
    if rule.enabled:  # reminders already inside a stage don't all send at once (rules.baseline)
        rules.baseline(org, rule, org_today(org))
    return rule


@transaction.atomic
def update_rule(org, actor, rule: NotificationRule, data: dict[str, Any]) -> NotificationRule:
    if "kind" in data and data["kind"] != rule.kind:
        raise FieldErrors({"kind": ["A rule's kind can't change."]})
    data = {k: v for k, v in data.items() if k != "kind"}
    rule = NotificationRule.objects.select_for_update().get(pk=rule.pk, organization=org)
    was_enabled = rule.enabled
    today = org_today(org)
    # What the rule owed today before the change: only messages the change adds are baselined (no burst when a
    # destination, person, type, lead or looser filter is added), while a stage already owed still sends.
    owed_before = rules.planned_keys(org, rule, today) if was_enabled else set()
    hooks_before = _named(rule.webhook_destinations.all())
    before = {f: getattr(rule, f) for f in REMINDER_FIELDS if f not in M2M and f not in FKS}
    before.update({f: getattr(rule, f"{f}_id") for f in FKS})
    sets, new_time = _apply(org, rule, data, creating=False)
    after = {f: getattr(rule, f) for f in REMINDER_FIELDS if f not in M2M and f not in FKS}
    after.update({f: getattr(rule, f"{f}_id") for f in FKS})
    changed = [f for f in after if after[f] != before[f]]
    moved = [n for n, rows in sets.items() if {r.pk for r in rows} != {r.pk for r in getattr(rule, n).all()}]
    if changed or moved:
        try:
            with transaction.atomic():
                rule.save()
        except IntegrityError:
            raise FieldErrors({"name": ["Another rule already has this name."]}) from None
        _write_sets(org, rule, {n: sets[n] for n in moved})
        changes: dict[str, Any] = {"name": rule.name, "fields": sorted(changed + moved)}
        if "webhook_destinations" in moved:  # the snapshot can't carry them ("webhook" is on its denylist)
            changes["webhook_destinations"] = [hooks_before, _named(sets["webhook_destinations"])]
        audit(org, actor, "notification_rule.updated", rule, changes)
    if rule.kind == RuleKind.REMINDERS and rule.enabled and (changed or moved):
        rules.baseline(org, rule, today, before=owed_before)  # turned on or widened: no burst
    if rule.kind == RuleKind.DIGEST:
        _set_digest_time(org, actor, new_time)
    return rule


@transaction.atomic
def delete_rule(org, actor, rule: NotificationRule) -> None:
    """Delete an added rule; its deliveries stay in the log with its name. A built-in can't be deleted."""
    if rule.is_builtin:
        raise Conflict("A built-in rule can't be deleted. Turn it off instead.")
    audit(org, actor, "notification_rule.deleted", rule, {"name": rule.name})
    rule.delete()


def using(org, field: str, obj) -> list[str]:
    """Names of the organization's rules that name `obj` in `field` (a delete guard's message)."""
    if field in THROUGH:
        through, column = THROUGH[field]
        ids = through.objects.filter(organization=org, **{column: obj.pk}).values_list("rule_id", flat=True)
        return list(NotificationRule.objects.filter(id__in=ids).order_by("name").values_list("name", flat=True))
    return list(
        NotificationRule.objects.filter(organization=org, **{field: obj})
        .order_by("name")
        .values_list("name", flat=True)
    )


def rules_naming_team(org, team) -> list[str]:
    names = {n for f in ("team", "escalation_team", "fallback_team") for n in using(org, f, team)}
    return sorted(names, key=str.lower)


# ---- test send -----------------------------------------------------------------------------------------------------


def _sample(org, rule: NotificationRule):
    """A reminder to render the test with: the rule's soonest covered reminder, else a made-up one."""
    from datetime import timedelta
    from types import SimpleNamespace

    today = org_today(org)
    lead = rule.lead_times_days[0] if rule.lead_times_days else None
    stage = (
        rules.Stage(DeliveryKind.REMINDER, lead)
        if lead is not None
        else rules.Stage(DeliveryKind.ESCALATION, (rule.escalation_days or [rule.repeat_every_days or 1])[0])
    )
    ctx = rules.Context(org, [rule], today, today)
    covered = ctx.covered(rule)
    if covered:
        return covered[0], stage, today
    due = today + timedelta(days=lead if lead is not None else -stage.days)
    source = (rule.source_types or [ReminderSource.CONTRACT])[0]
    sample = SimpleNamespace(
        id=uuid.UUID(int=0),
        source_type=source,
        source_id=uuid.UUID(int=0),
        title="Example: Contoso CRM",
        label=ReminderSource(source).label,
        due_date=due,
        status="open",
        resource_id=None,
        contract_id=None,
        license_pool_id=None,
        resource_kind=None,
        obligation_id=None,
    )
    return sample, stage, today


def _send_now(deliveries_made: list[Delivery]) -> list[Delivery]:
    """Attempt each test delivery now, outside any transaction (its claim commits first)."""
    return [deliveries.attempt(d.id) or Delivery.objects.get(id=d.id) for d in deliveries_made]


def test_send(org, actor, rule: NotificationRule, data: dict[str, Any]) -> list[Delivery]:
    """A sample rendered for the rule, sent now: by email to the requesting Admin's own address (`email`, default
    true) and to a chosen Teams or webhook destination of the organization. Recorded in the log (kind test) and
    audited."""
    errors: dict[str, list[str]] = {}
    for name in sorted(set(data) - {"email", "teams_destination", "webhook_destination"}):
        errors[name] = ["Send email, teams_destination or webhook_destination."]
    email = data.get("email", True)
    if not isinstance(email, bool):
        errors["email"] = ["Use true or false."]
    teams = _row(org, TeamsDestination, data.get("teams_destination"), "teams_destination", errors)
    hook = _row(org, WebhookDestination, data.get("webhook_destination"), "webhook_destination", errors)
    if email is True and not actor.email:
        errors["email"] = ["Your account has no email address to send the test to."]
    if not errors and not (email is True or teams or hook):
        errors["email"] = ["Choose where to send the test: your email, a Teams channel or a webhook."]
    for name, dest in (("teams_destination", teams), ("webhook_destination", hook)):
        if dest is not None and not dest.enabled:
            errors[name] = ["Turn the destination on before sending a test."]
    if errors:
        raise FieldErrors(errors)
    made = []
    with transaction.atomic():
        common = {
            "kind": DeliveryKind.TEST,
            "rule": rule,
            "rule_name": rule.name,
            "requested_by": actor,
            "max_attempts": 1,
            "queue_job": False,
        }
        if rule.kind == RuleKind.DIGEST:
            subject, content = _digest_sample(org, actor)
            if email is True:
                made.append(_test_delivery(org, rule, Channel.EMAIL, subject, content, common, recipient=actor.email))
            teams = hook = None  # the digest is email only
        else:
            sample, stage, today = _sample(org, rule)
            msg = rules.message(rule, sample, stage, today)
            subject = f"[Test] {msg.subject}"[:300]
            note = "This is a test of the notification rule; nothing needs doing."
            if email is True:
                content = render.email_content(
                    f"[Test] {msg.heading}",
                    msg.items,
                    intro=f"{note} {msg.intro}",
                    footer=rules.footer(rule),
                    link=msg.link,
                )
                made.append(_test_delivery(org, rule, Channel.EMAIL, subject, content, common, recipient=actor.email))
            if teams is not None:
                content = render.card_content(
                    f"[Test] {msg.heading}", msg.items, intro=f"{note} {msg.intro}", link=msg.link
                )
                made.append(_test_delivery(org, rule, Channel.TEAMS, subject, content, common, teams_destination=teams))
            if hook is not None:
                content = render.webhook_content("test", {"sample": msg.data, "message": note})
                made.append(
                    _test_delivery(org, rule, Channel.WEBHOOK, subject, content, common, webhook_destination=hook)
                )
    done = _send_now(made)
    with transaction.atomic():
        audit(
            org,
            actor,
            "notification_rule.tested",
            rule,
            {
                "name": rule.name,
                "ok": all(d.status == DeliveryStatus.SENT for d in done),
                "deliveries": [str(d.id) for d in done],
                "channels": [d.channel for d in done],
            },
        )
    return done


def _test_delivery(org, rule, channel, subject, content, common, **target) -> Delivery:
    delivery, _ = deliveries.enqueue(
        org,
        channel=channel,
        dedupe_key=f"test:rule:{rule.id.hex}:{uuid.uuid4().hex}",
        subject=subject,
        content=content,
        **common,
        **target,
    )
    return delivery


def _digest_sample(org, actor) -> tuple[str, dict[str, str]]:
    """The requesting Admin's own digest as it would be built now (an empty one when nothing is routed to them)."""

    from . import digest

    today = org_today(org)
    items, _fires = digest.plan(org, today)
    subject, text, html = digest.render(org, actor, items.get(actor.id, []), today)
    return f"[Test] {subject}"[:300], {"text": text, "html": html}
