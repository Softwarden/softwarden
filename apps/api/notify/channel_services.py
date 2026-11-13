"""Writes for notification channels: the organization's email settings, Teams and webhook destinations, their
connection tests and test sends, and resending a delivery. Admin only (the views declare it); each write is validated,
scoped to the organization and audited in one transaction. Secrets (the Graph client secret, the SMTP password,
webhook URLs, the signing secret) are Fernet-encrypted slots (core/credentials.py), write-only: an audit event records
which were replaced, never a value, and responses only say whether one is set."""

from __future__ import annotations

import re
import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone

from catalog.services import audit
from core import credentials, limits, outbound, simulation
from core.drf import timestamp
from core.problems import Conflict, FieldErrors
from discovery.entra import GUID, valid_tenant

from . import channels, deliveries, render
from .models import (
    Channel,
    Delivery,
    DeliveryKind,
    DeliveryStatus,
    EmailSettings,
    EmailTransport,
    SmtpSecurity,
    TeamsDestination,
    WebhookDestination,
)

NAME_MAX = 100
DESCRIPTION_MAX = 500
SECRET_MAX = 1000
SIGNING_SECRET_MIN = 16
HOST = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
# Office 365 connectors (incoming webhooks on these hosts) were retired in May 2026 (Microsoft Learn, 2026-11-05).
RETIRED_TEAMS_HOSTS = ("webhook.office.com", "outlook.office.com", "outlook.office365.com")
EMAIL_FIELDS = (
    "transport",
    "graph_tenant_id",
    "graph_client_id",
    "sender_mailbox",
    "smtp_host",
    "smtp_port",
    "smtp_security",
    "smtp_username",
    "from_address",
)
EMAIL_SECRETS = ("graph_secret", "smtp_password")


def _installation_host(host: str) -> bool:
    """The host is the installation's own SMTP server (SOFTWARDEN_SMTP_HOST), compared without case or a final dot."""
    own = (settings.EMAIL_HOST or "").strip().lower().rstrip(".")
    return bool(own) and host.strip().lower().rstrip(".") == own


def _text(value: Any, field: str, errors: dict, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        errors[field] = ["Enter text."]
        return ""
    value = value.strip()
    if len(value) > limit:
        errors[field] = [f"Use {limit} characters or fewer."]
    elif required and not value:
        errors[field] = ["This is required."]
    return value


def _email_address(value: str, field: str, errors: dict) -> None:
    if value:
        try:
            validate_email(value)
        except ValidationError:
            errors[field] = ["Enter an email address such as softwarden@contoso.com."]


def _secret(value: Any, field: str, errors: dict, minimum: int = 1) -> str | None:
    """The new secret, or None to leave the stored one. A blank string is refused (send nothing to keep it)."""
    if not isinstance(value, str) or not value.strip():
        errors[field] = ["Enter the value, or leave the field out to keep the stored one."]
        return None
    value = value.strip()
    if len(value) > SECRET_MAX:
        errors[field] = [f"Use {SECRET_MAX} characters or fewer."]
        return None
    if len(value) < minimum:
        errors[field] = [f"Use at least {minimum} characters."]
        return None
    return value


def _store(obj, prefix: str, value: str, errors: dict, field: str) -> None:
    try:
        credentials.slot_set(obj, prefix, value)
    except credentials.CredentialError as e:
        errors[field] = [str(e)]


def _clear_slot(obj, prefix: str) -> None:
    setattr(obj, f"{prefix}_ciphertext", "")
    setattr(obj, f"{prefix}_key_id", "")
    setattr(obj, f"{prefix}_set_at", None)


def _reset_test(obj) -> None:
    obj.last_test_at = None
    obj.last_test_ok = None
    obj.last_test_detail = ""


def _audit_values(obj, fields: tuple[str, ...]) -> dict[str, Any]:
    return {f: getattr(obj, f) for f in fields}


# ---- email settings -------------------------------------------------------------------------------------------------


@transaction.atomic
def update_email_settings(org, actor, data: dict[str, Any]) -> EmailSettings:
    """Create or change the organization's email settings from the fields present. Secrets: a string replaces the
    stored value; `smtp_password: null` removes it (no sign-in). The transport's required fields must be complete."""
    row = EmailSettings.objects.select_for_update().filter(organization=org).first()
    creating = row is None
    if row is None:
        row = EmailSettings(organization=org)
    before = _audit_values(row, EMAIL_FIELDS)
    smtp_before, had_password = _smtp_destination(row), bool(row.smtp_password_ciphertext)
    errors: dict[str, list[str]] = {}
    unknown = set(data) - set(EMAIL_FIELDS) - set(EMAIL_SECRETS)
    for name in sorted(unknown):
        errors[name] = ["This field isn't a setting."]
    if "transport" in data:
        if data["transport"] not in EmailTransport.values:
            errors["transport"] = [f"Choose one of {', '.join(EmailTransport.values)}."]
        else:
            row.transport = data["transport"]
    if "graph_tenant_id" in data:
        row.graph_tenant_id = _text(data["graph_tenant_id"], "graph_tenant_id", errors, 253)
        if row.graph_tenant_id and not channels.simulation_refused(row.graph_tenant_id):
            if not valid_tenant(row.graph_tenant_id):
                errors["graph_tenant_id"] = ["Enter the directory (tenant) ID, a GUID, or the tenant's domain name."]
    # The tenant the settings end up with, whether or not this request sent it: Graph on the simulated tenant is
    # refused unless core/simulation.allowed() (kept settings on another transport are left alone).
    if (
        "graph_tenant_id" not in errors
        and (row.transport == EmailTransport.GRAPH or "graph_tenant_id" in data)
        and channels.simulation_refused(row.graph_tenant_id)
    ):
        errors["graph_tenant_id"] = [simulation.MESSAGE]
    if "graph_client_id" in data:
        row.graph_client_id = _text(data["graph_client_id"], "graph_client_id", errors, 36)
        if row.graph_client_id and not GUID.match(row.graph_client_id):
            errors["graph_client_id"] = ["Enter the application (client) ID, a GUID."]
    if "sender_mailbox" in data:
        row.sender_mailbox = _text(data["sender_mailbox"], "sender_mailbox", errors, 254)
        _email_address(row.sender_mailbox, "sender_mailbox", errors)
    if "smtp_host" in data:
        row.smtp_host = _text(data["smtp_host"], "smtp_host", errors, 253)
        if row.smtp_host and not HOST.match(row.smtp_host):
            errors["smtp_host"] = ["Enter a host name or IP address, without https:// or a port."]
        elif row.smtp_host and _installation_host(row.smtp_host):
            # The installation's own server through the organization's settings would dodge its send budget.
            errors["smtp_host"] = [
                "This is the service's own mail server. Choose the installation default instead, or your own server."
            ]
        elif row.smtp_host:
            try:
                outbound.check_smtp_host(row.smtp_host)  # names are resolved and checked again when sending
            except outbound.UnsafeDestination as e:
                errors["smtp_host"] = [str(e)]
    if "smtp_port" in data:
        port = data["smtp_port"]
        if port is None:
            row.smtp_port = None
        elif isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            errors["smtp_port"] = ["Enter a port from 1 to 65535, or null for the security setting's usual port."]
        elif not channels.smtp_port_allowed(port):
            # A hosted installation's test connection mustn't map ports on its own network.
            errors["smtp_port"] = [channels.SMTP_PORT_REFUSED]
        else:
            row.smtp_port = port
    if "smtp_security" in data:
        if data["smtp_security"] not in SmtpSecurity.values:
            errors["smtp_security"] = [f"Choose one of {', '.join(SmtpSecurity.values)}."]
        else:
            row.smtp_security = data["smtp_security"]
    if "smtp_username" in data:
        row.smtp_username = _text(data["smtp_username"], "smtp_username", errors, 254)
    if "from_address" in data:
        row.from_address = _text(data["from_address"], "from_address", errors, 254)
        _email_address(row.from_address, "from_address", errors)
    replaced: list[str] = []
    if "graph_secret" in data:
        value = _secret(data["graph_secret"], "graph_secret", errors)
        if value is not None:
            _store(row, "graph_secret", value, errors, "graph_secret")
            replaced.append("graph_secret")
    if "smtp_password" in data:
        if data["smtp_password"] is None:
            if row.smtp_password_ciphertext:
                _clear_slot(row, "smtp_password")
                replaced.append("smtp_password")
        else:
            value = _secret(data["smtp_password"], "smtp_password", errors)
            if value is not None:
                _store(row, "smtp_password", value, errors, "smtp_password")
                replaced.append("smtp_password")
    if had_password and "smtp_password" not in data and not {"smtp_host", "smtp_port", "smtp_security"} & set(errors):
        reason = _smtp_moved(smtp_before, _smtp_destination(row))
        if reason:
            errors["smtp_password"] = [reason]
    if not errors:
        _require_complete(row, errors)
    if errors:
        raise FieldErrors(errors)
    after = _audit_values(row, EMAIL_FIELDS)
    changed = [f for f in EMAIL_FIELDS if before[f] != after[f]]
    if not creating and not changed and not replaced:
        return row
    _reset_test(row)
    row.save()
    audit(org, actor, "email_settings.updated", row, {"fields": changed, "replaced": replaced, "created": creating})
    return row


def _smtp_destination(row: EmailSettings) -> tuple[str, int, str] | None:
    """Where the SMTP password goes: (host, port, security), the host lowercased without a trailing dot and the port
    the one sending uses (the security setting's usual port when none is set). None without a host."""
    host = (row.smtp_host or "").strip().lower().rstrip(".")
    if not host:
        return None
    return host, row.smtp_port or channels.default_port(row.smtp_security), row.smtp_security


def _smtp_moved(before: tuple[str, int, str] | None, after: tuple[str, int, str] | None) -> str:
    """Why a change from `before` to `after` needs the stored SMTP password entered again, or "" when it doesn't.
    Another host or port is another destination; security turned to none would send the password in clear."""
    if after is None:
        return ""
    if before is None or before[:2] != after[:2]:
        return "The SMTP server changed: enter the password again (or null to sign in without one)."
    if after[2] == SmtpSecurity.NONE and before[2] != SmtpSecurity.NONE:
        return "Without TLS the password is sent in clear: enter it again to confirm (or null to sign in without one)."
    return ""


def _require_complete(row: EmailSettings, errors: dict) -> None:
    if row.transport == EmailTransport.GRAPH:
        for field in ("graph_tenant_id", "graph_client_id", "sender_mailbox"):
            if not getattr(row, field):
                errors[field] = ["Required to send through Microsoft Graph."]
        if not row.graph_secret_ciphertext:
            errors["graph_secret"] = ["Required to send through Microsoft Graph."]
    elif row.transport == EmailTransport.SMTP:
        for field in ("smtp_host", "from_address"):
            if not getattr(row, field):
                errors[field] = ["Required to send through an SMTP server."]
        if row.smtp_password_ciphertext and not row.smtp_username:
            errors["smtp_username"] = ["Enter the user name that goes with the password."]


@transaction.atomic
def delete_email_settings(org, actor) -> None:
    """Forget the organization's settings (and their secrets): email goes back to the installation default."""
    row = EmailSettings.objects.select_for_update().filter(organization=org).first()
    if row is None:
        return
    audit(org, actor, "email_settings.deleted", row, {"transport": row.transport})
    row.delete()


def _save_test(org, actor, obj, action: str, result: channels.TestResult, extra: dict[str, Any]) -> None:
    obj.last_test_at = timezone.now()
    obj.last_test_ok = result.ok
    obj.last_test_detail = result.detail[:500]
    obj.save(update_fields=["last_test_at", "last_test_ok", "last_test_detail", "updated_at"])
    audit(org, actor, action, obj, {"ok": result.ok, **extra})


# Organization.settings key holding the last test of the installation default while the organization has no
# EmailSettings row (a row is "saved settings"; a test shouldn't create one): {"at", "ok", "detail"}, secret-free
# (connection test details and delivery errors are scrubbed). Not in snapshots.ORG_SETTINGS_AUDITED: the
# `email_settings.tested` event records the outcome.
DEFAULT_TEST_KEY = "email_installation_default_test"


def _save_default_test(org, result: channels.TestResult) -> None:
    from core.models import Organization

    locked = Organization.objects.select_for_update().get(pk=org.pk)
    stored = dict(locked.settings or {})
    stored[DEFAULT_TEST_KEY] = {"at": timestamp(timezone.now()), "ok": result.ok, "detail": result.detail[:500]}
    Organization.objects.filter(pk=org.pk).update(settings=stored)
    org.settings = stored


def test_email_connection(org, actor) -> channels.TestResult:
    """Check the settings without sending anything (Graph: a token; SMTP: connect, TLS and sign in)."""
    result = channels.test_email_connection(org)
    with transaction.atomic():
        row = EmailSettings.objects.select_for_update().filter(organization=org).first()
        if row is not None:
            _save_test(org, actor, row, "email_settings.tested", result, {"test": "connection"})
        else:
            _save_default_test(org, result)
            audit(org, actor, "email_settings.tested", None, {"ok": result.ok, "test": "connection"})
    return result


def _test_send(org, actor, delivery: Delivery, obj, action: str, extra: dict[str, Any]) -> Delivery:
    """Send a test delivery now, outside any transaction (its claim commits first), then record the result on the
    settings row and audit it."""
    done = deliveries.attempt(delivery.id) or Delivery.objects.get(id=delivery.id)
    ok = done.status == DeliveryStatus.SENT
    detail = "Sent." if ok else (done.last_error or f"Not sent ({done.status}).")
    result = channels.TestResult(ok, detail)
    with transaction.atomic():
        if obj is not None:
            obj = type(obj).objects.select_for_update().get(pk=obj.pk)
            _save_test(org, actor, obj, action, result, {"test": "send", "delivery": str(done.id), **extra})
        else:
            if action == "email_settings.tested":  # the installation default, no settings row
                _save_default_test(org, result)
            audit(org, actor, action, done, {"ok": ok, "test": "send", "delivery": str(done.id), **extra})
    return done


def test_send_email(org, actor) -> Delivery:
    """A real test email through the configured transport to the requesting Admin's own address."""
    if not actor.email:
        raise FieldErrors({"recipient": ["Your account has no email address to send the test to."]})
    row = channels.email_settings(org)
    transport = channels.effective_transport(row)
    label = EmailTransport(transport).label
    content = render.email_content(
        "Softwarden test email",
        [],
        intro=f"This is a test email from Softwarden for {org.name}, sent through {label}. "
        "If you can read it, notifications will reach people the same way.",
        footer="Nothing needs doing.",
    )
    delivery, _ = deliveries.enqueue(
        org,
        channel=Channel.EMAIL,
        kind=DeliveryKind.TEST,
        dedupe_key=f"test:email:{uuid.uuid4().hex}",
        subject="Softwarden test email",
        content=content,
        recipient=actor.email,
        requested_by=actor,
        max_attempts=1,
        queue_job=False,
    )
    return _test_send(org, actor, delivery, row, "email_settings.tested", {"transport": transport})


# ---- Teams and webhook destinations ------------------------------------------------------------------------------------


def _url(value: Any, field: str, errors: dict, teams: bool) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors[field] = ["Enter the webhook address (https://...)."]
        return None
    try:
        parts = outbound.check_url(value.strip())
    except outbound.UnsafeDestination as e:
        errors[field] = [str(e)]
        return None
    host = (parts.hostname or "").lower()
    if teams and any(host == h or host.endswith("." + h) for h in RETIRED_TEAMS_HOSTS):
        errors[field] = [
            'Office 365 connectors were retired in May 2026. In the channel, open Workflows, choose "Send webhook '
            "alerts to a channel\", and paste that workflow's address."
        ]
        return None
    return value.strip()


def _apply_destination(
    org, obj, data: dict[str, Any], model, creating: bool, teams: bool, subscriptions: bool = False
) -> list[str]:
    """Validate and set the fields present. Returns which secrets were replaced. A webhook also takes a description
    and its event subscriptions (notify/webhooks.py); replacing its signing secret here ends any rotation's
    grace period (use rotate-secret to keep the old one signing for a while). Turning a webhook back on clears what
    turned it off."""
    from . import webhooks

    errors: dict[str, list[str]] = {}
    allowed = {"name", "enabled", "url"} | (set() if teams else {"signing_secret"})
    if subscriptions:  # /webhooks: the one place a webhook's events and description are set
        allowed |= {"description", "events"}
    for name in sorted(set(data) - allowed):
        if not teams and name in ("description", "events"):
            errors[name] = ["Set a webhook's events and description at /api/v1/webhooks."]
        else:
            errors[name] = ["This field isn't a setting of the destination."]
    if "name" in data or creating:
        obj.name = _text(data.get("name"), "name", errors, NAME_MAX, required=True)
        if obj.name and (model.objects.filter(organization=org, name__iexact=obj.name).exclude(pk=obj.pk).exists()):
            errors["name"] = ["Another destination already has this name."]
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            errors["enabled"] = ["Use true or false."]
        else:
            if data["enabled"] != obj.enabled and not teams:
                # An Admin's choice either way replaces what Softwarden recorded when it turned the endpoint off;
                # turning it on also starts the failure count again.
                obj.disabled_at, obj.disabled_reason = None, ""
                if data["enabled"]:
                    obj.consecutive_failures = 0
            obj.enabled = data["enabled"]
    if not teams and "description" in data:
        obj.description = _text(data.get("description"), "description", errors, DESCRIPTION_MAX)
    if not teams and "events" in data:
        events = webhooks.clean_events(data["events"], errors)
        if events is not None:
            obj.events = events
    replaced: list[str] = []
    if "url" in data or creating:
        url = _url(data.get("url"), "url", errors, teams)
        if url is not None:
            _store(obj, "url", url, errors, "url")
            obj.url_host = outbound.check_url(url).hostname or ""
            replaced.append("url")
    if not teams and ("signing_secret" in data or creating):
        value = _secret(data.get("signing_secret"), "signing_secret", errors, SIGNING_SECRET_MIN)
        if value is not None:
            _store(obj, "signing_secret", value, errors, "signing_secret")
            webhooks.clear_previous(obj)
            replaced.append("signing_secret")
    if errors:
        raise FieldErrors(errors)
    return replaced


DESTINATIONS = {
    TeamsDestination: ("teams_destination", True),
    WebhookDestination: ("webhook_destination", False),
}


@transaction.atomic
def create_destination(org, actor, model, data: dict[str, Any], *, subscriptions: bool = False):
    noun, teams = DESTINATIONS[model]
    obj = model(organization=org)
    replaced = _apply_destination(org, obj, data, model, True, teams, subscriptions)
    limits.check_room(org, limits.OTHER_RECORDS)
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError:
        raise FieldErrors({"name": ["Another destination already has this name."]}) from None
    extra = {} if teams else {"events": list(obj.events)}
    audit(org, actor, f"{noun}.created", obj, {"name": obj.name, "replaced": replaced, **extra})
    _subscriptions_changed(org, teams)
    return obj


def _subscriptions_changed(org, teams: bool) -> None:
    """A webhook was added, changed or removed: this transaction's cached subscriptions are read again."""
    if not teams:
        from . import webhooks

        webhooks.forget_subscribers(org)


@transaction.atomic
def update_destination(org, actor, obj, data: dict[str, Any], *, subscriptions: bool = False):
    model = type(obj)
    noun, teams = DESTINATIONS[model]
    fields = ("name", "enabled", "url_host") if teams else ("name", "enabled", "url_host", "description", "events")
    before = tuple(list(v) if isinstance(v, list) else v for v in (getattr(obj, f) for f in fields))
    replaced = _apply_destination(org, obj, data, model, False, teams, subscriptions)
    changed = [f for f, old in zip(fields, before, strict=True) if getattr(obj, f) != old]
    if not changed and not replaced:
        return obj
    if replaced:
        _reset_test(obj)
    try:
        with transaction.atomic():
            obj.save()
    except IntegrityError:
        raise FieldErrors({"name": ["Another destination already has this name."]}) from None
    payload: dict[str, Any] = {"name": obj.name, "fields": changed, "replaced": replaced}
    if "events" in changed:
        payload["events"] = [before[fields.index("events")], list(obj.events)]
    audit(org, actor, f"{noun}.updated", obj, payload)
    _subscriptions_changed(org, teams)
    return obj


@transaction.atomic
def delete_destination(org, actor, obj) -> None:
    """Delete the destination and its secrets. Deliveries keep its name; queued ones are skipped when their turn comes."""
    from . import rule_services

    noun, _teams = DESTINATIONS[type(obj)]
    field = "teams_destinations" if isinstance(obj, TeamsDestination) else "webhook_destinations"
    rules = rule_services.using(org, field, obj)
    if rules:
        shown = ", ".join(rules[:3]) + (f" and {len(rules) - 3} more" if len(rules) > 3 else "")
        uses = "1 notification rule uses it" if len(rules) == 1 else f"{len(rules)} notification rules use it"
        raise Conflict(
            f"{obj.name} can't be deleted: {uses} ({shown}). Remove it from the rules first.",
            extra={"blocking_references": {"notification_rules": len(rules)}, "blocking_names": rules[:10]},
        )
    audit(org, actor, f"{noun}.deleted", obj, {"name": obj.name})
    obj.delete()
    _subscriptions_changed(org, isinstance(obj, TeamsDestination))


def test_send_destination(org, actor, obj) -> Delivery:
    """A real test card (Teams) or a signed `ping` event (webhook) to this destination, sent now (one attempt, in the
    request; its result is returned and kept as the destination's last test)."""
    if not obj.enabled:
        raise Conflict("Turn the destination on before sending a test.")
    from . import webhooks

    noun, teams = DESTINATIONS[type(obj)]
    heading = "Softwarden test notification"
    intro = f"This is a test from Softwarden for {org.name}. Notifications for {obj.name} will look like this."
    if teams:
        content = render.card_content(heading, [], intro=intro)
        kwargs: dict[str, Any] = {"channel": Channel.TEAMS, "teams_destination": obj}
    else:
        content = render.webhook_content(
            webhooks.PING, {"message": intro, "webhook": {"id": str(obj.id), "name": obj.name}}
        )
        kwargs = {"channel": Channel.WEBHOOK, "webhook_destination": obj}
    delivery, _ = deliveries.enqueue(
        org,
        kind=DeliveryKind.TEST,
        dedupe_key=f"test:{noun}:{obj.id}:{uuid.uuid4().hex}",
        subject=heading,
        content=content,
        requested_by=actor,
        max_attempts=1,
        queue_job=False,
        **kwargs,
    )
    return _test_send(org, actor, delivery, obj, f"{noun}.tested", {})


# ---- resend ---------------------------------------------------------------------------------------------------------

RESENDABLE = (DeliveryStatus.SENT, DeliveryStatus.FAILED, DeliveryStatus.SKIPPED)


@transaction.atomic
def resend(org, actor, delivery: Delivery) -> Delivery:
    """Queue the same message again as a new delivery (`resend_of`, a fresh key), sent by the worker within a minute.
    Only a finished delivery is resent; its destination must still exist."""
    from . import webhooks

    delivery = Delivery.objects.select_for_update().get(pk=delivery.pk, organization=org)
    if delivery.baseline:
        raise Conflict("This notice was recorded when the rule started and was never written; nothing to resend.")
    if delivery.status not in RESENDABLE:
        raise Conflict("This delivery is still queued or being sent; wait for its outcome before resending.")
    if not delivery.content:
        # A skipped saved view email (notify/subscriptions.py) records why it wasn't written, not a message.
        raise Conflict("Nothing was written for this delivery, so there is nothing to resend.")
    if delivery.channel == Channel.TEAMS and delivery.teams_destination_id is None:
        raise Conflict("Its Teams destination was deleted, so it can't be resent.")
    if delivery.channel == Channel.WEBHOOK and delivery.webhook_destination_id is None:
        raise Conflict("Its webhook destination was deleted, so it can't be resent.")
    copy, _ = deliveries.enqueue(
        org,
        channel=delivery.channel,
        kind=delivery.kind,
        dedupe_key=f"resend:{delivery.id}:{uuid.uuid4().hex}",
        subject=delivery.subject,
        content=delivery.content,
        recipient=delivery.recipient,
        teams_destination=delivery.teams_destination,
        webhook_destination=delivery.webhook_destination,
        reminder=delivery.reminder,
        rule=delivery.rule,
        rule_name=delivery.rule_name,
        lead_time_days=delivery.lead_time_days,
        overdue_step=delivery.overdue_step,
        due_date=delivery.due_date,
        requested_by=actor,
        resend_of=delivery,
        event_id=delivery.event_id,
        max_attempts=webhooks.max_attempts() if delivery.kind == DeliveryKind.EVENT else deliveries.MAX_ATTEMPTS,
    )
    audit(org, actor, "notification.resent", copy, {"of": str(delivery.id), "channel": delivery.channel})
    return copy
