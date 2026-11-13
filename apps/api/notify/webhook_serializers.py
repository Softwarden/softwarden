"""Serializers for webhooks (notify/webhooks.py): endpoints, their delivery log and its attempts. Read-only
renderings; the input serializers only document request bodies (notify/channel_services.py and notify/webhooks.py
validate). The address and the signing secrets are write-only: an endpoint is shown by its address's host and
says since when each secret is set, never a value."""

from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.drf import ReadOnlySerializer, UserBriefSerializer, iso, represent

from . import channels
from .models import AttemptOutcome, DeliveryKind, DeliveryStatus
from .webhooks import EVENT_TYPES, WILDCARDS

SUBSCRIPTION_CHOICES = [*WILDCARDS, *EVENT_TYPES]
OUTCOME_CHOICES = [("sent", "Sent"), ("retrying", "Failed, will retry"), ("failed", "Failed")]


def _set_on(value) -> str | None:
    return value.date().isoformat() if value else None


class LastDeliverySerializer(serializers.Serializer):
    at = serializers.DateTimeField()
    outcome = serializers.ChoiceField(OUTCOME_CHOICES)
    status_code = serializers.IntegerField(allow_null=True, help_text="The endpoint's HTTP status; null: no answer.")
    delivery = serializers.UUIDField(help_text="The delivery in the webhook delivery log.")


class WebhookSerializer(ReadOnlySerializer):
    """A signed webhook endpoint and its event subscriptions (Admin)."""

    id = serializers.UUIDField()
    name = serializers.CharField()
    description = serializers.CharField()
    enabled = serializers.BooleanField()
    url_host = serializers.CharField(help_text="The address's host only: the full address is a secret.")
    url_set_on = serializers.DateField(allow_null=True)
    signing_secret_set_on = serializers.DateField(allow_null=True, help_text="The value is never returned.")
    previous_secret_expires_at = serializers.DateTimeField(
        allow_null=True,
        help_text="After a rotation, the previous secret also signs until then (a second v1= in the signature).",
    )
    events = serializers.ListField(
        child=serializers.ChoiceField(SUBSCRIPTION_CHOICES), help_text="Event types and family wildcards."
    )
    disabled_at = serializers.DateTimeField(
        allow_null=True, help_text="When Softwarden turned the endpoint off (410 Gone or too many failures)."
    )
    disabled_reason = serializers.CharField()
    consecutive_failures = serializers.IntegerField(help_text="Event deliveries that ended failed in a row.")
    last_delivery = LastDeliverySerializer(allow_null=True)
    last_test_at = serializers.DateTimeField(allow_null=True)
    last_test_ok = serializers.BooleanField(allow_null=True)
    last_test_detail = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    def to_representation(self, instance):
        o = instance
        last = None
        if o.last_delivery_at is not None and o.last_delivery_id is not None:
            last = {
                "at": iso(o.last_delivery_at),
                "outcome": o.last_delivery_outcome,
                "status_code": o.last_delivery_status_code,
                "delivery": str(o.last_delivery_id),
            }
        return {
            "id": str(o.id),
            "name": o.name,
            "description": o.description,
            "enabled": o.enabled,
            "url_host": o.url_host,
            "url_set_on": _set_on(o.url_set_at),
            "signing_secret_set_on": _set_on(o.signing_secret_set_at),
            "previous_secret_expires_at": iso(o.previous_secret_expires_at),
            "events": list(o.events),
            "disabled_at": iso(o.disabled_at),
            "disabled_reason": o.disabled_reason,
            "consecutive_failures": o.consecutive_failures,
            "last_delivery": last,
            "last_test_at": iso(o.last_test_at),
            "last_test_ok": o.last_test_ok,
            "last_test_detail": o.last_test_detail,
            "created_at": iso(o.created_at),
            "updated_at": iso(o.updated_at),
        }


class WebhookInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, help_text="Unique in the organization.")
    description = serializers.CharField(max_length=500, required=False, allow_blank=True)
    enabled = serializers.BooleanField(required=False)
    url = serializers.CharField(
        write_only=True,
        help_text="https only; private, loopback, link-local and metadata addresses are refused, on save and again "
        "when sending. Required on create; send it again only to replace it. Never returned.",
    )
    signing_secret = serializers.CharField(
        write_only=True,
        min_length=16,
        help_text="HMAC SHA-256 key for X-Softwarden-Signature (16 to 1,000 characters). Required on create; sending it "
        "on a change replaces it at once (use rotate-secret for a grace period). Never returned.",
    )
    events = serializers.ListField(
        child=serializers.ChoiceField(SUBSCRIPTION_CHOICES),
        required=False,
        help_text="What the endpoint receives: event types, or a family such as record.*. Default: none.",
    )


class RotateSecretInputSerializer(serializers.Serializer):
    signing_secret = serializers.CharField(write_only=True, min_length=16, help_text="The new secret. Never returned.")
    grace_period_hours = serializers.IntegerField(
        required=False,
        min_value=0,
        max_value=168,
        default=24,
        help_text="How long the previous secret keeps signing beside the new one (0: stop at once).",
    )


class EventTypeSerializer(serializers.Serializer):
    type = serializers.CharField()
    family = serializers.CharField(help_text="Subscribe to every type of a family with <family>.*.")
    description = serializers.CharField()


class EventCatalogSerializer(serializers.Serializer):
    types = EventTypeSerializer(many=True)
    wildcards = serializers.ListField(child=serializers.CharField())


# ---- the delivery log ------------------------------------------------------------------------------------------------


class WebhookBriefSerializer(serializers.Serializer):
    id = serializers.UUIDField(allow_null=True, help_text="Null once the endpoint was deleted.")
    name = serializers.CharField()


class WebhookDeliverySerializer(ReadOnlySerializer):
    """One post to a webhook endpoint: an event, a ping or a notification rule's notice (Admin)."""

    id = serializers.UUIDField(help_text="Also the X-Softwarden-Delivery header.")
    webhook = WebhookBriefSerializer()
    event_type = serializers.CharField(help_text="record.updated, ping, reminder.due, ...")
    event_id = serializers.UUIDField(allow_null=True, help_text="The envelope's id; shared by every endpoint's copy.")
    kind = serializers.ChoiceField(DeliveryKind.choices, help_text="event, test (a ping), reminder or escalation.")
    status = serializers.ChoiceField(DeliveryStatus.choices)
    attempts = serializers.IntegerField()
    max_attempts = serializers.IntegerField()
    next_attempt_at = serializers.DateTimeField(allow_null=True, help_text="When queued: the next attempt.")
    last_status_code = serializers.IntegerField(allow_null=True, help_text="The last attempt's HTTP status.")
    last_error = serializers.CharField(help_text="Why the last attempt failed; never contains a secret.")
    sent_at = serializers.DateTimeField(allow_null=True, help_text="When the endpoint answered 2xx.")
    resend_of = serializers.UUIDField(allow_null=True)
    requested_by = UserBriefSerializer(allow_null=True, help_text="Who asked for a ping or a resend.")
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    def base(self, d) -> dict[str, Any]:
        codes = self.context.get("last_status_codes")
        if codes is None:
            latest = d.attempt_log.order_by("-started_at", "-number").values_list("status_code", flat=True).first()
        else:
            latest = codes.get(d.id)
        return {
            "id": str(d.id),
            "webhook": {
                "id": str(d.webhook_destination_id) if d.webhook_destination_id else None,
                "name": d.destination_name,
            },
            "event_type": d.event_type or "notification",
            "event_id": str(d.event_id) if d.event_id else None,
            "kind": d.kind,
            "status": d.status,
            "attempts": d.attempts,
            "max_attempts": d.max_attempts,
            "next_attempt_at": iso(d.next_attempt_at) if d.status == DeliveryStatus.QUEUED else None,
            "last_status_code": latest,
            "last_error": d.last_error,
            "sent_at": iso(d.sent_at),
            "resend_of": str(d.resend_of_id) if d.resend_of_id else None,
            "requested_by": represent(UserBriefSerializer, d.requested_by),
            "created_at": iso(d.created_at),
            "updated_at": iso(d.updated_at),
        }

    def to_representation(self, instance):
        return self.base(instance)


class DeliveryAttemptSerializer(ReadOnlySerializer):
    number = serializers.IntegerField()
    started_at = serializers.DateTimeField()
    duration_ms = serializers.IntegerField(allow_null=True)
    outcome = serializers.ChoiceField(AttemptOutcome.choices)
    status_code = serializers.IntegerField(allow_null=True, help_text="The endpoint's answer; null: none arrived.")
    response_excerpt = serializers.CharField(help_text="The answer's start, at most 500 characters, secrets removed.")
    error = serializers.CharField()
    next_attempt_at = serializers.DateTimeField(allow_null=True, help_text="When this attempt scheduled a retry.")

    def to_representation(self, instance):
        a = instance
        return {
            "number": a.number,
            "started_at": iso(a.started_at),
            "duration_ms": a.duration_ms,
            "outcome": a.outcome,
            "status_code": a.status_code,
            "response_excerpt": a.response_excerpt,
            "error": a.error,
            "next_attempt_at": iso(a.next_attempt_at),
        }


class WebhookDeliveryDetailSerializer(WebhookDeliverySerializer):
    payload = serializers.SerializerMethodField(
        help_text="The JSON body posted (its envelope), rendered from the stored content now; the signature varies. "
        "A delivery from an older release is re-rendered in the current envelope, so it can differ from what was sent."
    )
    attempt_log = DeliveryAttemptSerializer(many=True, help_text="Every attempt, oldest first.")
    resends = serializers.ListField(child=serializers.UUIDField(), help_text="Deliveries that resent this one.")

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_payload(self, d) -> dict[str, Any]:
        import json

        return json.loads(channels.webhook_body(d))

    def to_representation(self, instance):
        return {
            **self.base(instance),
            "payload": self.get_payload(instance),
            "attempt_log": [DeliveryAttemptSerializer(a).data for a in instance.attempt_log.all()],
            "resends": [str(pk) for pk in instance.resends.order_by("created_at").values_list("id", flat=True)],
        }


class PingResultSerializer(serializers.Serializer):
    ok = serializers.BooleanField(help_text="The endpoint answered 2xx.")
    detail = serializers.CharField()
    delivery = WebhookDeliveryDetailSerializer(help_text="The ping, also in the webhook delivery log.")
