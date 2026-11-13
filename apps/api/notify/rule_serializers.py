"""Serializers for notification rules: the rule as stored, its preview and its test send. Read-only renderings
built as plain data; the input serializer only documents the request body (notify/rule_services.py validates)."""

from typing import Any

from rest_framework import serializers

from catalog.models import RECORD_KIND_CHOICES, Criticality
from core.drf import ReadOnlySerializer, timestamp
from core.models import Role

from .channel_serializers import DeliverySerializer
from .models import DeliveryKind, ReminderSource, ReminderStatus, RuleKind

LEADS_HELP = "Days before the due date, 0 to 365 (0 = on the day), at most 10; each is sent once, largest first."
STEPS_HELP = "Days after the due date, 1 to 365, at most 10; each is sent once while the reminder is still open."
REPEAT_HELP = (
    "Repeat every this many days after the due date (1 to 365) while the reminder is still open: an escalation step at "
    "every multiple of it, beside escalation_days. Null: no repeat."
)


class RefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class DestinationRefSerializer(RefSerializer):
    enabled = serializers.BooleanField(help_text="A destination that is off is left out when the rule sends.")


def _ref(obj) -> dict[str, Any] | None:
    return {"id": str(obj.pk), "name": obj.name} if obj is not None else None


def _refs(rows) -> list[dict[str, Any]]:
    return sorted(({"id": str(r.pk), "name": r.name} for r in rows), key=lambda e: (e["name"].lower(), e["id"]))


def _destinations(rows) -> list[dict[str, Any]]:
    return [
        {"id": str(d.pk), "name": d.name, "enabled": d.enabled}
        for d in sorted(rows, key=lambda d: (d.name.lower(), str(d.pk)))
    ]


class NotificationRuleSerializer(ReadOnlySerializer):
    """A rule as stored. Rows are prefetched by the viewset; the context carries the organization (`digest_time`)."""

    id = serializers.UUIDField()
    name = serializers.CharField()
    kind = serializers.ChoiceField(RuleKind.choices, help_text="reminders, or digest for the built-in Daily digest.")
    builtin = serializers.BooleanField(help_text="Seeded with the organization: editable, never deleted.")
    builtin_key = serializers.CharField(help_text="Which built-in (renewals_contracts, daily_digest, ...); else empty.")
    enabled = serializers.BooleanField()
    source_types = serializers.ListField(child=serializers.ChoiceField(ReminderSource.choices))
    lead_times_days = serializers.ListField(child=serializers.IntegerField(), help_text=LEADS_HELP)
    escalation_days = serializers.ListField(child=serializers.IntegerField(), help_text=STEPS_HELP)
    repeat_every_days = serializers.IntegerField(allow_null=True, help_text=REPEAT_HELP)
    escalate_when_acknowledged = serializers.BooleanField(
        help_text="Acknowledging stops the advance notices; with this on, escalation steps and repeats still go out."
    )
    send_email = serializers.BooleanField()
    teams_destinations = DestinationRefSerializer(many=True)
    webhook_destinations = DestinationRefSerializer(many=True)
    notify_routed = serializers.BooleanField(
        help_text="Email the reminder's routed owner (a record's IT steward, a renewal's IT follow-up, ...); when "
        "routing finds nobody, the fallback team or person, else the IT coordinator."
    )
    people = RefSerializer(many=True)
    team = RefSerializer(allow_null=True, help_text="Every member of the team.")
    roles = serializers.ListField(child=serializers.ChoiceField(Role.choices), help_text="Every active member with it.")
    escalation_people = RefSerializer(many=True)
    escalation_team = RefSerializer(allow_null=True)
    escalation_roles = serializers.ListField(child=serializers.ChoiceField(Role.choices))
    fallback_team = RefSerializer(allow_null=True)
    fallback_person = RefSerializer(allow_null=True)
    criticalities = serializers.ListField(
        child=serializers.ChoiceField(Criticality.choices),
        help_text="Only reminders about a record (or covering one) with one of these criticalities.",
    )
    record_kinds = serializers.ListField(child=serializers.ChoiceField(RECORD_KIND_CHOICES))
    vendor = RefSerializer(allow_null=True, help_text="Only renewals, contracts, pools and records of this vendor.")
    min_annual_value = serializers.DecimalField(
        max_digits=14, decimal_places=2, allow_null=True, help_text="Only contracts and renewals worth this a year."
    )
    digest_time = serializers.CharField(
        allow_null=True, help_text="The daily digest only: HH:MM in the organization's timezone."
    )
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    def to_representation(self, r):
        org = self.context.get("organization")
        digest = r.kind == RuleKind.DIGEST
        return {
            "id": str(r.id),
            "name": r.name,
            "kind": r.kind,
            "builtin": r.is_builtin,
            "builtin_key": r.builtin_key,
            "enabled": r.enabled,
            "source_types": list(r.source_types),
            "lead_times_days": list(r.lead_times_days),
            "escalation_days": list(r.escalation_days),
            "repeat_every_days": r.repeat_every_days,
            "escalate_when_acknowledged": r.escalate_when_acknowledged,
            "send_email": r.send_email,
            "teams_destinations": _destinations(r.teams_destinations.all()),
            "webhook_destinations": _destinations(r.webhook_destinations.all()),
            "notify_routed": r.notify_routed,
            "people": _refs(r.people.all()),
            "team": _ref(r.team),
            "roles": list(r.roles),
            "escalation_people": _refs(r.escalation_people.all()),
            "escalation_team": _ref(r.escalation_team),
            "escalation_roles": list(r.escalation_roles),
            "fallback_team": _ref(r.fallback_team),
            "fallback_person": _ref(r.fallback_person),
            "criticalities": list(r.criticalities),
            "record_kinds": list(r.record_kinds),
            "vendor": _ref(r.vendor),
            "min_annual_value": str(r.min_annual_value) if r.min_annual_value is not None else None,
            "digest_time": org.digest_time.strftime("%H:%M") if digest and org is not None else None,
            "created_at": timestamp(r.created_at),
            "updated_at": timestamp(r.updated_at),
        }


class NotificationRuleInputSerializer(serializers.Serializer):
    """Send only what changes. The daily digest's rule takes name, enabled and digest_time only."""

    name = serializers.CharField(max_length=200, help_text="Unique in the organization.")
    enabled = serializers.BooleanField(required=False)
    source_types = serializers.ListField(child=serializers.ChoiceField(ReminderSource.choices), required=False)
    lead_times_days = serializers.ListField(child=serializers.IntegerField(), required=False, help_text=LEADS_HELP)
    escalation_days = serializers.ListField(child=serializers.IntegerField(), required=False, help_text=STEPS_HELP)
    repeat_every_days = serializers.IntegerField(required=False, allow_null=True, help_text=REPEAT_HELP)
    escalate_when_acknowledged = serializers.BooleanField(required=False)
    send_email = serializers.BooleanField(required=False)
    teams_destinations = serializers.ListField(child=serializers.UUIDField(), required=False)
    webhook_destinations = serializers.ListField(child=serializers.UUIDField(), required=False)
    notify_routed = serializers.BooleanField(required=False)
    people = serializers.ListField(child=serializers.UUIDField(), required=False, help_text="Active people.")
    team = serializers.UUIDField(required=False, allow_null=True)
    roles = serializers.ListField(child=serializers.ChoiceField(Role.choices), required=False)
    escalation_people = serializers.ListField(child=serializers.UUIDField(), required=False)
    escalation_team = serializers.UUIDField(required=False, allow_null=True)
    escalation_roles = serializers.ListField(child=serializers.ChoiceField(Role.choices), required=False)
    fallback_team = serializers.UUIDField(required=False, allow_null=True)
    fallback_person = serializers.UUIDField(required=False, allow_null=True)
    criticalities = serializers.ListField(child=serializers.ChoiceField(Criticality.choices), required=False)
    record_kinds = serializers.ListField(child=serializers.ChoiceField(RECORD_KIND_CHOICES), required=False)
    vendor = serializers.UUIDField(required=False, allow_null=True)
    min_annual_value = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True)
    digest_time = serializers.CharField(required=False, help_text="The daily digest only: HH:MM.")


# ---- preview ---------------------------------------------------------------------------------------------------------


class PreviewReminderSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    source_type = serializers.ChoiceField(ReminderSource.choices)
    title = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # the API field
    due_date = serializers.DateField()
    status = serializers.ChoiceField(ReminderStatus.choices, help_text="As stored now.")
    snoozed_until = serializers.DateField(allow_null=True)


class PreviewRecipientSerializer(serializers.Serializer):
    name = serializers.CharField()
    email = serializers.CharField()
    user = serializers.IntegerField(allow_null=True, help_text="The user account, when the recipient has one.")
    person = serializers.UUIDField(allow_null=True, help_text="The person, when reached as a person.")


class PreviewDestinationSerializer(serializers.Serializer):
    channel = serializers.ChoiceField(["teams", "webhook"])
    id = serializers.UUIDField()
    name = serializers.CharField()


class PreviewEventSerializer(serializers.Serializer):
    date = serializers.DateField(help_text="The day it would go out (organization-local).")
    kind = serializers.ChoiceField([DeliveryKind.REMINDER, DeliveryKind.ESCALATION])
    lead_time_days = serializers.IntegerField(allow_null=True)
    overdue_step = serializers.IntegerField(allow_null=True, help_text="Escalations: the step, in days after due.")
    reminder = PreviewReminderSerializer()
    recipients = PreviewRecipientSerializer(many=True, help_text="Email recipients that haven't had it.")
    destinations = PreviewDestinationSerializer(many=True)
    opted_out = serializers.ListField(
        child=serializers.CharField(), help_text="Members who turned reminder emails off."
    )
    fallback = serializers.ChoiceField(
        ["", "rule", "coordinator"],
        help_text="Who stood in for the routed owner: the rule's fallback, the IT coordinator, or nobody did.",
    )


class PreviewSerializer(serializers.Serializer):
    rule = serializers.UUIDField()
    start = serializers.DateField()
    days = serializers.IntegerField()
    events = PreviewEventSerializer(many=True)


# ---- test send -------------------------------------------------------------------------------------------------------


class RuleTestSendInputSerializer(serializers.Serializer):
    email = serializers.BooleanField(required=False, help_text="To your own address (default true).")
    teams_destination = serializers.UUIDField(required=False, allow_null=True)
    webhook_destination = serializers.UUIDField(required=False, allow_null=True)


class RuleTestResultSerializer(serializers.Serializer):
    ok = serializers.BooleanField(help_text="The channel accepted the message.")
    detail = serializers.CharField()
    delivery = DeliverySerializer(help_text="The test delivery, also in the notification log (kind test).")


class RuleTestSendSerializer(serializers.Serializer):
    ok = serializers.BooleanField(help_text="Every channel accepted its message.")
    results = RuleTestResultSerializer(many=True)
