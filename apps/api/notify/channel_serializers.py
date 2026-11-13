"""Serializers for notification channels and the notification log. Read-only renderings of model rows; the
input serializers only document request bodies (notify/channel_services.py validates). Secrets are write-only:
responses say whether one is set and since when, never the value; a webhook URL is shown by its host only."""

from typing import Any

from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.drf import ReadOnlySerializer, UserBriefSerializer, iso, represent

from .models import Channel, DeliveryKind, DeliveryStatus, EmailTransport, SmtpSecurity


def _set_on(value) -> str | None:
    return value.date().isoformat() if value else None


class InstallationDefaultSerializer(serializers.Serializer):
    configured = serializers.BooleanField(help_text="The server has SMTP settings (SOFTWARDEN_SMTP_HOST).")
    from_address = serializers.CharField(help_text="SOFTWARDEN_EMAIL_FROM.")


class EmailSettingsSerializer(ReadOnlySerializer):
    """The organization's email settings, or the defaults when it has none (`saved` false)."""

    saved = serializers.BooleanField(help_text="False until an Admin saves settings: the installation default applies.")
    transport = serializers.ChoiceField(EmailTransport.choices)
    graph_tenant_id = serializers.CharField()
    graph_client_id = serializers.CharField()
    graph_secret_set = serializers.BooleanField(help_text="A client secret is stored. The value is never returned.")
    graph_secret_set_on = serializers.DateField(allow_null=True)
    sender_mailbox = serializers.CharField()
    smtp_host = serializers.CharField()
    smtp_port = serializers.IntegerField(allow_null=True, help_text="Null: 587 for STARTTLS, 465 for TLS, 25 for none.")
    smtp_security = serializers.ChoiceField(SmtpSecurity.choices)
    smtp_username = serializers.CharField()
    smtp_password_set = serializers.BooleanField(help_text="A password is stored. The value is never returned.")
    smtp_password_set_on = serializers.DateField(allow_null=True)
    from_address = serializers.CharField()
    installation_default = InstallationDefaultSerializer()
    last_test_at = serializers.DateTimeField(allow_null=True)
    last_test_ok = serializers.BooleanField(allow_null=True)
    last_test_detail = serializers.CharField()
    updated_at = serializers.DateTimeField(allow_null=True)

    def _default_test(self) -> dict[str, Any]:
        """The installation default's last test (notify/channel_services.DEFAULT_TEST_KEY), with no settings row."""
        from .channel_services import DEFAULT_TEST_KEY

        org = self.context.get("organization")
        found = ((org.settings or {}) if org is not None else {}).get(DEFAULT_TEST_KEY) or {}
        return {
            "last_test_at": found.get("at"),
            "last_test_ok": found.get("ok"),
            "last_test_detail": str(found.get("detail") or ""),
        }

    def to_representation(self, instance):
        row = instance  # an EmailSettings, or None for the defaults (context: `organization`)
        installation = {
            "configured": bool(settings.SOFTWARDEN_EMAIL_CONFIGURED),
            "from_address": settings.DEFAULT_FROM_EMAIL,
        }
        if row is None:
            return {
                "saved": False,
                "transport": EmailTransport.INSTALLATION_DEFAULT,
                "graph_tenant_id": "",
                "graph_client_id": "",
                "graph_secret_set": False,
                "graph_secret_set_on": None,
                "sender_mailbox": "",
                "smtp_host": "",
                "smtp_port": None,
                "smtp_security": SmtpSecurity.STARTTLS,
                "smtp_username": "",
                "smtp_password_set": False,
                "smtp_password_set_on": None,
                "from_address": "",
                "installation_default": installation,
                **self._default_test(),
                "updated_at": None,
            }
        return {
            "saved": True,
            "transport": row.transport,
            "graph_tenant_id": row.graph_tenant_id,
            "graph_client_id": row.graph_client_id,
            "graph_secret_set": bool(row.graph_secret_ciphertext),
            "graph_secret_set_on": _set_on(row.graph_secret_set_at),
            "sender_mailbox": row.sender_mailbox,
            "smtp_host": row.smtp_host,
            "smtp_port": row.smtp_port,
            "smtp_security": row.smtp_security,
            "smtp_username": row.smtp_username,
            "smtp_password_set": bool(row.smtp_password_ciphertext),
            "smtp_password_set_on": _set_on(row.smtp_password_set_at),
            "from_address": row.from_address,
            "installation_default": installation,
            "last_test_at": iso(row.last_test_at),
            "last_test_ok": row.last_test_ok,
            "last_test_detail": row.last_test_detail,
            "updated_at": iso(row.updated_at),
        }


class EmailSettingsInputSerializer(serializers.Serializer):
    transport = serializers.ChoiceField(
        EmailTransport.choices,
        required=False,
        help_text="installation_default uses the server's SOFTWARDEN_SMTP_* settings. SMTP AUTH with a password "
        "against Exchange Online is being retired (off by default for existing tenants from the end of December "
        "2026): for Microsoft 365 use graph.",
    )
    graph_tenant_id = serializers.CharField(required=False, allow_blank=True, help_text="GUID or tenant domain.")
    graph_client_id = serializers.CharField(required=False, allow_blank=True, help_text="Application (client) ID.")
    graph_secret = serializers.CharField(
        required=False, write_only=True, help_text="Client secret; replaces the stored one. Never returned."
    )
    sender_mailbox = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="The mailbox Graph sends as; the app's Exchange RBAC for Applications scope must cover it.",
    )
    smtp_host = serializers.CharField(required=False, allow_blank=True)
    smtp_port = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=65535)
    smtp_security = serializers.ChoiceField(SmtpSecurity.choices, required=False)
    smtp_username = serializers.CharField(required=False, allow_blank=True)
    smtp_password = serializers.CharField(
        required=False,
        allow_null=True,
        write_only=True,
        help_text="Replaces the stored password; null removes it (no sign-in). Never returned.",
    )
    from_address = serializers.CharField(required=False, allow_blank=True)


class TestResultSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    detail = serializers.CharField(help_text="What was checked, in words; never contains a secret.")


# ---- destinations ---------------------------------------------------------------------------------------------------


class _DestinationSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    enabled = serializers.BooleanField()
    url_host = serializers.CharField(help_text="The address's host only: the full address is a secret.")
    url_set_on = serializers.DateField(allow_null=True)
    last_test_at = serializers.DateTimeField(allow_null=True)
    last_test_ok = serializers.BooleanField(allow_null=True)
    last_test_detail = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    def base(self, obj) -> dict[str, Any]:
        return {
            "id": str(obj.id),
            "name": obj.name,
            "enabled": obj.enabled,
            "url_host": obj.url_host,
            "url_set_on": _set_on(obj.url_set_at),
            "last_test_at": iso(obj.last_test_at),
            "last_test_ok": obj.last_test_ok,
            "last_test_detail": obj.last_test_detail,
            "created_at": iso(obj.created_at),
            "updated_at": iso(obj.updated_at),
        }

    def to_representation(self, instance):
        return self.base(instance)


class TeamsDestinationSerializer(_DestinationSerializer):
    """A Teams channel's Workflows webhook."""


class WebhookDestinationSerializer(_DestinationSerializer):
    """A generic HTTPS webhook; bodies are signed (X-Softwarden-Signature)."""

    signing_secret_set_on = serializers.DateField(allow_null=True)

    def to_representation(self, instance):
        return {**self.base(instance), "signing_secret_set_on": _set_on(instance.signing_secret_set_at)}


class TeamsDestinationInputSerializer(serializers.Serializer):
    name = serializers.CharField(
        max_length=100, help_text="Unique in the organization; rules refer to the destination."
    )
    enabled = serializers.BooleanField(required=False)
    url = serializers.CharField(
        write_only=True,
        help_text="The Workflows 'Send webhook alerts to a channel' address (https). Office 365 connector addresses "
        "are refused (retired May 2026). Required on create; send it again only to replace it. Never returned.",
    )


class WebhookDestinationInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    enabled = serializers.BooleanField(required=False)
    url = serializers.CharField(
        write_only=True,
        help_text="https only; private, loopback and link-local addresses are refused. Never returned.",
    )
    signing_secret = serializers.CharField(
        write_only=True,
        min_length=16,
        help_text="HMAC SHA-256 key for X-Softwarden-Signature (16 characters or more). Never returned.",
    )


# ---- the notification log ---------------------------------------------------------------------------------------------


class DestinationBriefSerializer(serializers.Serializer):
    id = serializers.UUIDField(allow_null=True, help_text="Null once the destination was deleted.")
    name = serializers.CharField()


class DeliveryReminderSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    title = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # the API field
    due_date = serializers.DateField()


class DeliverySerializer(ReadOnlySerializer):
    """One delivery in the notification log (Admins only, so the recipient address is shown)."""

    id = serializers.UUIDField()
    channel = serializers.ChoiceField(Channel.choices)
    kind = serializers.ChoiceField(DeliveryKind.choices)
    status = serializers.ChoiceField(DeliveryStatus.choices)
    recipient = serializers.CharField(help_text="Email only; empty for Teams and webhooks.")
    destination = DestinationBriefSerializer(allow_null=True, help_text="Teams and webhooks only.")
    subject = serializers.CharField()
    reminder = DeliveryReminderSerializer(allow_null=True)
    rule = serializers.UUIDField(allow_null=True, help_text="The notification rule that sent it; null once deleted.")
    rule_name = serializers.CharField(help_text="The rule's name when it was queued.")
    lead_time_days = serializers.IntegerField(allow_null=True)
    overdue_step = serializers.IntegerField(allow_null=True)
    due_date = serializers.DateField(allow_null=True, help_text="Rules: the reminder's due date the notice was for.")
    baseline = serializers.BooleanField(
        help_text="Recorded, never sent: the rule started or widened after this notice was due (status skipped)."
    )
    attempts = serializers.IntegerField()
    max_attempts = serializers.IntegerField()
    next_attempt_at = serializers.DateTimeField(allow_null=True, help_text="When queued: the next attempt.")
    last_error = serializers.CharField(help_text="Why the last attempt didn't send; never contains a secret.")
    sent_at = serializers.DateTimeField(allow_null=True, help_text="Accepted by the channel (not proof of delivery).")
    client_request_id = serializers.UUIDField(
        allow_null=True, help_text="The last attempt's id (Graph client-request-id)."
    )
    resend_of = serializers.UUIDField(allow_null=True)
    requested_by = UserBriefSerializer(allow_null=True, help_text="Who asked for a test send or a resend.")
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    def base(self, d) -> dict[str, Any]:
        destination = None
        if d.channel == Channel.TEAMS:
            destination = {
                "id": str(d.teams_destination_id) if d.teams_destination_id else None,
                "name": d.destination_name,
            }
        elif d.channel == Channel.WEBHOOK:
            destination = {
                "id": str(d.webhook_destination_id) if d.webhook_destination_id else None,
                "name": d.destination_name,
            }
        reminder = None
        if d.reminder_id is not None and d.reminder is not None:
            reminder = {
                "id": str(d.reminder.id),
                "title": d.reminder.title,
                "label": d.reminder.label,
                "due_date": d.reminder.due_date.isoformat(),
            }
        return {
            "id": str(d.id),
            "channel": d.channel,
            "kind": d.kind,
            "status": d.status,
            "recipient": d.recipient,
            "destination": destination,
            "subject": d.subject,
            "reminder": reminder,
            "rule": str(d.rule_id) if d.rule_id else None,
            "rule_name": d.rule_name,
            "lead_time_days": d.lead_time_days,
            "overdue_step": d.overdue_step,
            "due_date": d.due_date.isoformat() if d.due_date else None,
            "baseline": d.baseline,
            "attempts": d.attempts,
            "max_attempts": d.max_attempts,
            "next_attempt_at": iso(d.next_attempt_at) if d.status == DeliveryStatus.QUEUED else None,
            "last_error": d.last_error,
            "sent_at": iso(d.sent_at),
            "client_request_id": str(d.client_request_id) if d.client_request_id else None,
            "resend_of": str(d.resend_of_id) if d.resend_of_id else None,
            "requested_by": represent(UserBriefSerializer, d.requested_by),
            "created_at": iso(d.created_at),
            "updated_at": iso(d.updated_at),
        }

    def to_representation(self, instance):
        return self.base(instance)


PRIVATE_NOTE = "This email shows someone's private saved view; only its owner can read it."


def readable_content(content: dict[str, Any], request) -> dict[str, Any]:
    """A delivery's content as the reader may see it: a private saved view's email (notify/subscriptions.PRIVATE_TO)
    is redacted for everyone but the view's owner, so its name, query and link don't reach Admins through the log.
    Deliberate: if the owner is later deleted, nobody can read that log entry's content again."""
    owner = content.get("private_to") if isinstance(content, dict) else None
    if owner is None:
        return content
    reader = getattr(getattr(request, "user", None), "id", None)
    if reader == owner:
        return content
    return {"text": "", "html": "", "redacted": PRIVATE_NOTE}


class DeliveryDetailSerializer(DeliverySerializer):
    content = serializers.SerializerMethodField(
        help_text="The rendered message: email {text, html}; Teams {card} (Adaptive Card 1.2); webhook {event, data}."
    )
    resends = serializers.ListField(child=serializers.UUIDField(), help_text="Deliveries that resent this one.")

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_content(self, d) -> dict[str, Any]:
        return d.content

    def to_representation(self, instance):
        return {
            **self.base(instance),
            "content": readable_content(instance.content, self.context.get("request")),
            "resends": [str(pk) for pk in instance.resends.order_by("created_at").values_list("id", flat=True)],
        }


class TestSendResultSerializer(serializers.Serializer):
    ok = serializers.BooleanField(help_text="The channel accepted the message.")
    detail = serializers.CharField()
    delivery = DeliverySerializer(help_text="The test delivery, also in the notification log.")
