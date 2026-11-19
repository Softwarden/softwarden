"""Sending one Delivery on its channel. notify/deliveries.py
claims the delivery first and records what these functions report:

- return normally: the channel accepted the message (Graph 202, SMTP accepted it, any 2xx from a webhook);
- raise DeliveryFailure with an `outcome`:
  - RETRY: nothing was accepted and trying again can help (Graph or a webhook answered 429 or 503, a connection
    couldn't be made, SMTP answered with a 4xx). `retry_after` carries the server's Retry-After when it sent one;
  - AMBIGUOUS: the request may have been acted on (a timeout or a dropped connection after the request was written, a
    5xx other than 503, an unexpected error mid-send). Never retried automatically: Graph has no idempotency header,
    and neither have Teams Workflows or SMTP. An Admin can check and resend;
  - PERMANENT: retrying can't help (bad settings or credentials, access denied, a refused recipient, a 4xx);
  - SKIPPED: nothing to send with (email not configured, a destination disabled or deleted);
  - BUSY: four Graph sends from this mailbox are already in flight (Graph's limit is 4 concurrent requests per app and
    mailbox): try again shortly without counting an attempt.

Webhooks are judged by `webhook_failure` instead: the post names its delivery (X-Softwarden-Delivery), so a
timeout, a connection failure and any answer but 2xx are RETRY, except a 410 Gone (PERMANENT, and the endpoint is
turned off), a redirect (PERMANENT, never followed) and an address the SSRF guard refuses (PERMANENT). The body is the
envelope of `webhook_body`, signed with every signing secret in force (`signature`, two `v1=` during a rotation).
Teams and webhook sends return the server's Answer (status and body) for the attempt log; a DeliveryFailure carries
them too.

Messages are written here, never copied from a server, and never contain a secret, a webhook URL or a password.
Email transports: an organization's EmailSettings (notify/models.py), or the installation's SOFTWARDEN_SMTP_* when it
has none or chose `installation_default`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import smtplib
import ssl
import threading
import time
import uuid
import zlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.db import connection

from core import credentials, graph, outbound, simulation
from core.drf import timestamp

from .models import Channel, Delivery, EmailSettings, EmailTransport, SmtpSecurity
from .render import TEAMS_LIMIT, teams_body
from .smtp import SmtpGuard

log = logging.getLogger("softwarden.notify")

SMTP_TIMEOUT = 20
GRAPH_CONCURRENCY = 4  # per app and mailbox (Microsoft Learn)
TEAMS_MIN_INTERVAL = 0.3  # seconds between posts to one destination: Teams throttles more than 4 requests a second
SIGNATURE_HEADER = "X-Softwarden-Signature"
NOTIFICATION_HEADER = "x-softwarden-notification-id"
USER_AGENT = "Softwarden-Notifications/1"
TOKEN_REUSE_MARGIN = 300  # seconds before expiry a cached Graph token is replaced


class Outcome:
    RETRY = "retry"
    AMBIGUOUS = "ambiguous"
    PERMANENT = "permanent"
    SKIPPED = "skipped"
    BUSY = "busy"


class DeliveryFailure(Exception):
    """`status_code` and `body` are an HTTP channel's answer, for the attempt log (notify/deliveries.py scrubs the body
    before keeping an excerpt). `gone`: a webhook endpoint answered 410, so it is turned off (notify/webhooks.py)."""

    def __init__(
        self,
        message: str,
        outcome: str = Outcome.PERMANENT,
        retry_after: float | None = None,
        *,
        status_code: int | None = None,
        body: str = "",
        gone: bool = False,
    ):
        super().__init__(message)
        self.outcome = outcome
        self.retry_after = retry_after
        self.status_code = status_code
        self.body = body
        self.gone = gone


@dataclass(frozen=True)
class Answer:
    """What an HTTP channel's server answered to an accepted message (the attempt log): its status and body, raw."""

    status_code: int | None = None
    body: str = ""


def body_text(reply: graph.Reply) -> str:
    """A reply's body as text for the attempt log (already capped at 64 KB by core/outbound.py); scrubbed later."""
    return reply.body.decode("utf-8", errors="replace")


# ---- email settings -------------------------------------------------------------------------------------------------


def email_settings(org) -> EmailSettings | None:
    return EmailSettings.objects.filter(organization=org).first()


def effective_transport(row: EmailSettings | None) -> str:
    return row.transport if row is not None else EmailTransport.INSTALLATION_DEFAULT


def installation_configured() -> bool:
    return bool(settings.SOFTWARDEN_EMAIL_CONFIGURED)


def email_configured(org, row: EmailSettings | None = None) -> bool:
    """Whether the organization's email can be sent at all: Graph or SMTP settings filled in, or the installation's
    SMTP settings for the default. Not a connection test."""
    row = row if row is not None else email_settings(org)
    transport = effective_transport(row)
    if transport == EmailTransport.GRAPH:
        assert row is not None
        return bool(row.graph_tenant_id and row.graph_client_id and row.graph_secret_ciphertext and row.sender_mailbox)
    if transport == EmailTransport.SMTP:
        assert row is not None
        return bool(row.smtp_host and row.from_address)
    return installation_configured()


SIMULATED = simulation.SIMULATED


def simulation_refused(tenant: str) -> bool:
    """The synthetic tenant on a server that doesn't allow simulation (core/simulation.py)."""
    return tenant == SIMULATED and not simulation.allowed()


# Overridable in tests: tenant -> transport. The tenant `simulated` uses core/graph_simulator.py, as discovery does
# (simulation.NotAllowed unless core/simulation.allowed(); the callers check first and say so in words).
def graph_transport(tenant: str) -> graph.Transport:
    if tenant == SIMULATED:
        simulation.require()
        from core import graph_simulator

        return graph_simulator.Transport()
    return graph.HttpTransport()


_tokens: dict[str, tuple[str, float]] = {}
_tokens_lock = threading.Lock()


def _token_key(tenant: str, client_id: str, secret: str) -> str:
    return hashlib.sha256(f"{tenant}\x00{client_id}\x00{secret}".encode()).hexdigest()


def graph_token(transport: graph.Transport, tenant: str, client_id: str, secret: str, reuse: bool = True) -> str:
    """A token for Graph, reused in this process until shortly before it expires. GraphError or Unreachable."""
    key = _token_key(tenant, client_id, secret)
    now = time.monotonic()
    if reuse:
        with _tokens_lock:
            held = _tokens.get(key)
        if held is not None and held[1] > now:
            return held[0]
    body = graph.request_token(transport, tenant, client_id, secret)
    try:
        lifetime = float(body.get("expires_in") or 0)
    except (TypeError, ValueError):
        lifetime = 0.0
    token = str(body["access_token"])
    if lifetime > TOKEN_REUSE_MARGIN:
        with _tokens_lock:
            _tokens[key] = (token, now + lifetime - TOKEN_REUSE_MARGIN)
    return token


def forget_tokens() -> None:
    with _tokens_lock:
        _tokens.clear()


def token_failure(error: graph.GraphError | graph.Unreachable) -> DeliveryFailure:
    """A failed token request. Nothing was sent yet, so anything transient is safe to retry."""
    if isinstance(error, graph.Unreachable):
        return DeliveryFailure("Couldn't reach Microsoft's sign-in service from this server.", Outcome.RETRY)
    if error.status == 429 or error.status >= 500:
        return DeliveryFailure(
            f"Microsoft's sign-in service answered {error.status}; trying again.", Outcome.RETRY, error.retry_after
        )
    code = f" ({error.code})" if error.code else ""
    return DeliveryFailure(
        f"Microsoft didn't issue a token{code}. Check the tenant ID, the client ID and the client secret.",
        Outcome.PERMANENT,
    )


SEND_MAIL_CODES = {
    "ErrorAccessDenied": "Graph refused to send as the sender mailbox (ErrorAccessDenied). Check that the app holds the "
    "Exchange RBAC for Applications role 'Application Mail.Send' with a management scope that includes this mailbox "
    "(changes take 30 minutes to 2 hours).",
    "ErrorInvalidUser": "Graph doesn't know the sender mailbox (ErrorInvalidUser). Check the address.",
    "MailboxNotEnabledForRESTAPI": "The sender mailbox can't send through Graph (MailboxNotEnabledForRESTAPI). Use an "
    "Exchange Online mailbox.",
    "ErrorSendAsDenied": "Graph refused to send as the sender mailbox (ErrorSendAsDenied).",
    "InvalidAuthenticationToken": "Graph refused the token (InvalidAuthenticationToken).",
}


def send_mail_failure(error: graph.GraphError | graph.Unreachable) -> DeliveryFailure:
    """A failed sendMail. Retried only when Graph said it didn't accept the request (429, 503)."""
    if isinstance(error, graph.Unreachable):
        if error.ambiguous:
            return DeliveryFailure(
                f"Graph's answer didn't arrive ({error}); the message may have been accepted.", Outcome.AMBIGUOUS
            )
        return DeliveryFailure("Couldn't reach Microsoft Graph from this server.", Outcome.RETRY)
    if error.throttled:
        return DeliveryFailure(
            f"Graph didn't accept the message ({error.status} {error.code}); trying again.".replace(" )", ")"),
            Outcome.RETRY,
            error.retry_after,
        )
    if error.status >= 500:
        return DeliveryFailure(
            f"Graph answered {error.status} {error.code}; the message may have been accepted.".replace("  ", " "),
            Outcome.AMBIGUOUS,
        )
    known = SEND_MAIL_CODES.get(error.code)
    if known:
        return DeliveryFailure(known, Outcome.PERMANENT)
    code = f" {error.code}" if error.code else ""
    return DeliveryFailure(f"Graph refused the message ({error.status}{code}).", Outcome.PERMANENT)


@contextmanager
def graph_slot(sender: str) -> Iterator[None]:
    """One of GRAPH_CONCURRENCY session advisory locks for this sender mailbox across every worker, or BUSY."""
    group = zlib.crc32(f"softwarden-graph-mail:{sender.lower()}".encode()) - 2**31  # an int4 key
    held = None
    with connection.cursor() as cursor:
        for slot in range(GRAPH_CONCURRENCY):
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [group, slot])
            if cursor.fetchone()[0]:
                held = slot
                break
    if held is None:
        raise DeliveryFailure("Four messages are already being sent from this mailbox.", Outcome.BUSY, 15)
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [group, held])


def graph_message(delivery: Delivery) -> dict[str, Any]:
    return {
        "subject": delivery.subject,
        "body": {"contentType": "HTML", "content": delivery.content.get("html") or ""},
        "toRecipients": [{"emailAddress": {"address": delivery.recipient}}],
        "internetMessageHeaders": [{"name": NOTIFICATION_HEADER, "value": str(delivery.id)}],
    }


def _send_graph(row: EmailSettings, delivery: Delivery) -> None:
    if not (row.graph_tenant_id and row.graph_client_id and row.sender_mailbox):
        raise DeliveryFailure("Graph email settings are incomplete: tenant ID, client ID and sender mailbox.")
    if simulation_refused(row.graph_tenant_id):
        raise DeliveryFailure(f"The email settings use the simulated tenant. {simulation.MESSAGE}")
    try:
        secret = credentials.slot_value(row, "graph_secret")
    except credentials.CredentialError as e:
        raise DeliveryFailure(str(e)) from None
    transport = graph_transport(row.graph_tenant_id)
    with graph_slot(row.sender_mailbox):
        try:
            bearer = graph_token(transport, row.graph_tenant_id, row.graph_client_id, secret)
        except (graph.GraphError, graph.Unreachable) as e:
            raise token_failure(e) from None
        try:
            graph.send_mail(
                transport,
                bearer,
                row.sender_mailbox,
                graph_message(delivery),
                delivery.client_request_id or uuid.uuid4(),
            )
        except (graph.GraphError, graph.Unreachable) as e:
            raise send_mail_failure(e) from None


DJANGO_SMTP = "django.core.mail.backends.smtp.EmailBackend"
GUARDED_SMTP = "notify.smtp.GuardedEmailBackend"


def smtp_connection(row: EmailSettings | None, guard: SmtpGuard | None = None):
    """A Django SMTP connection for the organization's settings, or the installation's default connection. Real SMTP
    goes through notify/smtp.py's guarded backend: a total deadline always, and for an organization's own host a
    checked, pinned address (the installation's host is the operator's and isn't restricted)."""
    if row is None or row.transport != EmailTransport.SMTP:
        if settings.EMAIL_BACKEND == DJANGO_SMTP:
            return get_connection(GUARDED_SMTP, fail_silently=False, guard=guard or SmtpGuard(restrict=False))
        return get_connection(fail_silently=False)  # locmem in tests, console or a custom backend
    # Blank, never None: Django's backend reads None as "use the installation's EMAIL_HOST_USER / _PASSWORD",
    # which would send them to the organization's host. No user name or no password means no sign-in.
    password = credentials.slot_value(row, "smtp_password") if row.smtp_password_ciphertext else ""
    return get_connection(
        GUARDED_SMTP,
        host=row.smtp_host,
        port=row.smtp_port or default_port(row.smtp_security),
        username=row.smtp_username or "",
        password=password or "",
        use_tls=row.smtp_security == SmtpSecurity.STARTTLS,
        use_ssl=row.smtp_security == SmtpSecurity.TLS,
        timeout=SMTP_TIMEOUT,
        fail_silently=False,
        guard=guard or SmtpGuard(restrict=True),
    )


# With SOFTWARDEN_SMTP_ANY_PORT off (cloud mode's default) an organization's SMTP server is reached on a mail port
# only, so its test connection can't map ports on the installation's network. Checked on save (channel_services) and
# again where the port is resolved for a test or a send, for rows saved before the setting.
MAIL_PORTS = frozenset({25, 465, 587, 2525})
SMTP_PORT_REFUSED = (
    "Use a mail port: 587 (STARTTLS), 465 (TLS), 25 or 2525, or null for the security setting's usual port. The "
    "installation's operator can allow other ports (SOFTWARDEN_SMTP_ANY_PORT)."
)


def smtp_port_allowed(port: int) -> bool:
    return port in MAIL_PORTS or bool(settings.SOFTWARDEN_SMTP_ANY_PORT)


def smtp_port_refusal(row: EmailSettings | None) -> str:
    """SMTP_PORT_REFUSED when the organization's own SMTP settings name a port it may not use, else ""."""
    if row is None or row.transport != EmailTransport.SMTP:
        return ""
    port = row.smtp_port or default_port(row.smtp_security)
    return "" if smtp_port_allowed(port) else SMTP_PORT_REFUSED


def default_port(security: str) -> int:
    ports: dict[str, int] = {"starttls": 587, "tls": 465}
    return ports.get(str(security), 25)


SMTP_SIGN_IN_REFUSED = "The SMTP server refused the user name or password"
SMTP_NO_CONNECTION = "Couldn't connect to the SMTP server with these settings (host, port and security)."


def smtp_open_failure(error: BaseException) -> DeliveryFailure:
    """A failure while connecting, starting TLS or signing in: nothing was sent. Deliberately coarse: "refused sign-in"
    or "couldn't connect", never which port answered what, so the settings form can't be used to scan the network."""
    if isinstance(error, outbound.UnsafeDestination):
        return DeliveryFailure(str(error))
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return DeliveryFailure(f"{SMTP_SIGN_IN_REFUSED} ({error.smtp_code}).")
    if _settings_problem(error):
        # Retrying can't help (a certificate that doesn't verify, a plain-text port asked for TLS, no STARTTLS, a
        # server that greets with 5xx). Same words as any other connection failure: no port-state oracle.
        return DeliveryFailure(SMTP_NO_CONNECTION)
    return DeliveryFailure(SMTP_NO_CONNECTION, Outcome.RETRY)


def _settings_problem(error: BaseException) -> bool:
    """A failure to connect that the same settings will meet again: TLS and certificate errors (not a connection
    dropped mid-handshake), a missing STARTTLS extension, a 5xx greeting or reply before the message."""
    if isinstance(error, ssl.SSLEOFError | ssl.SSLZeroReturnError | ssl.SSLWantReadError | ssl.SSLWantWriteError):
        return False
    if isinstance(error, ssl.SSLError | ssl.CertificateError | smtplib.SMTPNotSupportedError):
        return True
    return isinstance(error, smtplib.SMTPResponseException) and 500 <= error.smtp_code < 600


def smtp_send_failure(error: BaseException) -> DeliveryFailure:
    """A failure once the connection was open: a refusal says nothing was accepted; anything else is ambiguous."""
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        codes = sorted({code for code, _msg in error.recipients.values()})
        transient = codes and all(400 <= c < 500 for c in codes)
        return DeliveryFailure(
            f"The SMTP server refused the recipient ({', '.join(str(c) for c in codes)}).",
            Outcome.RETRY if transient else Outcome.PERMANENT,
        )
    if isinstance(error, smtplib.SMTPResponseException):
        code = error.smtp_code
        if 400 <= code < 500:
            return DeliveryFailure(f"The SMTP server deferred the message ({code}); trying again.", Outcome.RETRY)
        if 500 <= code < 600:
            return DeliveryFailure(f"The SMTP server refused the message ({code}).")
    return DeliveryFailure(
        f"The SMTP conversation broke off ({type(error).__name__}); the message may have been accepted.",
        Outcome.AMBIGUOUS,
    )


def _send_smtp(row: EmailSettings | None, delivery: Delivery) -> None:
    if row is not None and row.transport == EmailTransport.SMTP:
        if not (row.smtp_host and row.from_address):
            raise DeliveryFailure("SMTP settings are incomplete: host and from address.")
        sender = row.from_address
    else:
        if not installation_configured():
            raise DeliveryFailure(
                "Email isn't configured: set SOFTWARDEN_SMTP_HOST on the server or choose Graph or SMTP in Email settings.",
                Outcome.SKIPPED,
            )
        sender = settings.DEFAULT_FROM_EMAIL
    restrict = row is not None and row.transport == EmailTransport.SMTP
    refused = smtp_port_refusal(row)
    if refused:
        raise DeliveryFailure(refused)
    guard = SmtpGuard(restrict=restrict)
    try:
        conn = smtp_connection(row, guard)
    except credentials.CredentialError as e:
        raise DeliveryFailure(str(e)) from None
    message = EmailMultiAlternatives(
        delivery.subject,
        delivery.content.get("text") or "",
        sender,
        [delivery.recipient],
        connection=conn,
        headers={NOTIFICATION_HEADER: str(delivery.id)},
    )
    message.attach_alternative(delivery.content.get("html") or "", "text/html")
    with guard:
        try:
            conn.open()
        except Exception as e:  # connecting, TLS, signing in: nothing was sent
            raise smtp_open_failure(e) from None
        try:
            sent = conn.send_messages([message])
        except Exception as e:
            if guard.fired:
                raise DeliveryFailure(
                    f"The SMTP server didn't finish within {guard.deadline:g} s; the message may have been accepted.",
                    Outcome.AMBIGUOUS,
                ) from None
            raise smtp_send_failure(e) from None
        finally:
            try:
                conn.close()
            except Exception:  # noqa: S110 (closing after the outcome is known changes nothing)
                pass
    if not sent:
        raise DeliveryFailure("The SMTP server accepted no recipient.")


def send_email(org, delivery: Delivery) -> None:
    if not delivery.recipient:
        raise DeliveryFailure("No recipient address.")
    row = email_settings(org)
    if effective_transport(row) == EmailTransport.GRAPH:
        assert row is not None
        _send_graph(row, delivery)
    else:
        _send_smtp(row, delivery)


# ---- Teams and webhooks -----------------------------------------------------------------------------------------------

# Overridable in tests (and nowhere else): the SSRF-guarded POST.
post_https = outbound.post

_last_post: dict[uuid.UUID, float] = {}
_pace_lock = threading.Lock()


def _pace(destination_id: uuid.UUID) -> None:
    """At most one post per TEAMS_MIN_INTERVAL to one destination from this process."""
    with _pace_lock:
        wait = _last_post.get(destination_id, 0.0) + TEAMS_MIN_INTERVAL - time.monotonic()
        _last_post[destination_id] = time.monotonic() + max(wait, 0.0)
    if wait > 0:
        time.sleep(wait)


def http_failure(
    reply_or_error: graph.Reply | graph.Unreachable | outbound.UnsafeDestination, what: str
) -> DeliveryFailure:
    if isinstance(reply_or_error, outbound.UnsafeDestination):
        return DeliveryFailure(f"The {what} address isn't allowed: {reply_or_error}")
    if isinstance(reply_or_error, graph.Unreachable):
        if reply_or_error.ambiguous:
            return DeliveryFailure(
                f"The {what} didn't answer in time ({reply_or_error}); the message may have been posted.",
                Outcome.AMBIGUOUS,
            )
        return DeliveryFailure(f"Couldn't reach the {what} ({reply_or_error}).", Outcome.RETRY)
    status = reply_or_error.status
    if status in (429, 503):
        retry_after = graph.parse_retry_after(reply_or_error.headers.get("retry-after"))
        return DeliveryFailure(
            f"The {what} is throttling requests ({status}); trying again.", Outcome.RETRY, retry_after
        )
    if status >= 500:
        return DeliveryFailure(f"The {what} answered {status}; the message may have been posted.", Outcome.AMBIGUOUS)
    if 300 <= status < 400:
        return DeliveryFailure(f"The {what} answered with a redirect ({status}), which isn't followed.")
    return DeliveryFailure(f"The {what} refused the message ({status}). Check the address is still valid.")


def _post(destination, body: bytes, headers: dict[str, str], what: str) -> Answer:
    try:
        url = credentials.slot_value(destination, "url")
    except credentials.CredentialError as e:
        raise DeliveryFailure(str(e)) from None
    _pace(destination.id)
    try:
        reply = post_https(url, body, headers)
    except (graph.Unreachable, outbound.UnsafeDestination) as e:
        raise http_failure(e, what) from None
    if not reply.ok:
        failure = http_failure(reply, what)
        failure.status_code, failure.body = reply.status, body_text(reply)
        raise failure
    return Answer(reply.status, body_text(reply))


def send_teams(delivery: Delivery) -> Answer:
    destination = delivery.teams_destination
    if destination is None:
        raise DeliveryFailure("The Teams destination was deleted.", Outcome.SKIPPED)
    if not destination.enabled:
        raise DeliveryFailure(f"The Teams destination {destination.name} is turned off.", Outcome.SKIPPED)
    body = teams_body(delivery.content.get("card") or {})
    if len(body) > TEAMS_LIMIT:
        raise DeliveryFailure("The card is larger than Teams' 28 KB limit.")
    return _post(destination, body, {"Content-Type": "application/json", "User-Agent": USER_AGENT}, "Teams workflow")


def signature(secrets: str | Sequence[str], timestamp: int, body: bytes) -> str:
    """`t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>." + body>`, with one `v1=` per signing secret: during a rotation's
    grace period the new secret's comes first, then the previous one's (docs/API.md "Verifying a webhook")."""
    keys = [secrets] if isinstance(secrets, str) else list(secrets)
    signed = str(timestamp).encode() + b"." + body
    digests = [hmac.new(key.encode(), signed, hashlib.sha256).hexdigest() for key in keys]
    return ",".join([f"t={timestamp}", *(f"v1={d}" for d in digests)])


def event_type(delivery: Delivery) -> str:
    return str(delivery.content.get("event") or delivery.event_type or "notification")


def webhook_body(delivery: Delivery) -> bytes:
    """The envelope every webhook post carries: `id` (the event's id, shared by every endpoint it went to; a
    notification's or ping's own delivery id), `type`, `occurred_at` (UTC, Z), `organization` {id}, `actor` ({id,
    name} or "system") and `data`. The delivery's id is the X-Softwarden-Delivery header; a resend keeps the event's
    id and gets a new delivery id."""
    content = delivery.content
    envelope = {
        "id": str(content.get("id") or delivery.id),
        "type": event_type(delivery),
        "occurred_at": content.get("occurred_at") or timestamp(delivery.created_at),
        "organization": {"id": str(delivery.organization_id)},
        "actor": content.get("actor") or "system",
        "data": content.get("data") or {},
    }
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


WEBHOOK_TIMEOUT = 10  # seconds per socket operation; core/outbound.DEADLINE (30 s) bounds the whole post


def webhook_failure(
    reply_or_error: graph.Reply | graph.Unreachable | outbound.UnsafeDestination,
) -> DeliveryFailure:
    """A webhook post that wasn't accepted. Unlike Graph, Teams or SMTP, a webhook post carries its delivery id
    (X-Softwarden-Delivery) for the receiver to drop a repeat, so a timeout or a 5xx is retried: any answer but 2xx is
    retried except a 410 Gone (the endpoint is turned off) and a redirect (never followed: the address is the one an
    Admin saved); an address that isn't allowed fails at once."""
    if isinstance(reply_or_error, outbound.UnsafeDestination):
        return DeliveryFailure(f"The webhook address isn't allowed: {reply_or_error}")
    if isinstance(reply_or_error, graph.Unreachable):
        return DeliveryFailure(f"Couldn't deliver to the webhook ({reply_or_error}); trying again.", Outcome.RETRY)
    status = reply_or_error.status
    body = body_text(reply_or_error)
    if status == 410:
        return DeliveryFailure(
            "The webhook answered 410 Gone: the endpoint was removed.", status_code=status, body=body, gone=True
        )
    if 300 <= status < 400:
        return DeliveryFailure(
            f"The webhook answered with a redirect ({status}), which isn't followed.", status_code=status, body=body
        )
    retry_after = graph.parse_retry_after(reply_or_error.headers.get("retry-after")) if status in (429, 503) else None
    return DeliveryFailure(
        f"The webhook answered {status}; trying again.", Outcome.RETRY, retry_after, status_code=status, body=body
    )


def send_webhook(delivery: Delivery) -> Answer:
    from .webhooks import signing_secrets

    destination = delivery.webhook_destination
    if destination is None:
        raise DeliveryFailure("The webhook destination was deleted.", Outcome.SKIPPED)
    if not destination.enabled:
        raise DeliveryFailure(f"The webhook {destination.name} is turned off.", Outcome.SKIPPED)
    try:
        url = credentials.slot_value(destination, "url")
        keys = signing_secrets(destination)
    except credentials.CredentialError as e:
        raise DeliveryFailure(str(e)) from None
    now = int(time.time())
    body = webhook_body(delivery)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-Softwarden-Delivery": str(delivery.id),
        "X-Softwarden-Event": event_type(delivery),
        SIGNATURE_HEADER: signature(keys, now, body),
    }
    try:
        reply = post_https(url, body, headers, timeout=WEBHOOK_TIMEOUT)
    except (graph.Unreachable, outbound.UnsafeDestination) as e:
        raise webhook_failure(e) from None
    if not reply.ok:
        raise webhook_failure(reply)
    return Answer(reply.status, body_text(reply))


def send(org, delivery: Delivery) -> Answer | None:
    """Send on the delivery's channel. Returns the HTTP answer for Teams and webhooks, None for email."""
    if delivery.channel == Channel.EMAIL:
        send_email(org, delivery)
        return None
    if delivery.channel == Channel.TEAMS:
        return send_teams(delivery)
    if delivery.channel == Channel.WEBHOOK:
        return send_webhook(delivery)
    raise DeliveryFailure(f"Unknown channel {delivery.channel}.")


def secrets_of(org, delivery: Delivery) -> list[str]:
    """Every secret this delivery's send could touch, for scrubbing an unexpected error's text. Never raises."""
    found: list[str] = []
    rows: list[tuple[Any, tuple[str, ...]]] = []
    if delivery.channel == Channel.EMAIL:
        row = email_settings(org)
        if row is not None:
            rows.append((row, ("graph_secret", "smtp_password")))
    elif delivery.channel == Channel.TEAMS and delivery.teams_destination is not None:
        rows.append((delivery.teams_destination, ("url",)))
    elif delivery.channel == Channel.WEBHOOK and delivery.webhook_destination is not None:
        rows.append((delivery.webhook_destination, ("url", "signing_secret", "previous_signing_secret")))
    for obj, prefixes in rows:
        for prefix in prefixes:
            if credentials.slot_configured(obj, prefix):
                try:
                    found.append(credentials.slot_value(obj, prefix))
                except credentials.CredentialError:
                    pass
    return [s for s in found if s]


@dataclass(frozen=True)
class TestResult:
    ok: bool
    detail: str


def test_email_connection(org) -> TestResult:
    """Check the email settings without sending: Graph gets a token (which doesn't prove the mailbox scope), SMTP
    connects, starts TLS and signs in, the installation default reports whether it is configured."""
    row = email_settings(org)
    transport = effective_transport(row)
    if transport == EmailTransport.GRAPH:
        assert row is not None
        if simulation_refused(row.graph_tenant_id):
            return TestResult(False, f"The email settings use the simulated tenant. {simulation.MESSAGE}")
        try:
            secret = credentials.slot_value(row, "graph_secret")
            graph_token(
                graph_transport(row.graph_tenant_id), row.graph_tenant_id, row.graph_client_id, secret, reuse=False
            )
        except credentials.CredentialError as e:
            return TestResult(False, str(e))
        except (graph.GraphError, graph.Unreachable) as e:
            return TestResult(False, str(token_failure(e)))
        return TestResult(
            True,
            f"Microsoft issued a token. Only a test email proves the app may send as {row.sender_mailbox} "
            "(Exchange RBAC for Applications).",
        )
    if transport == EmailTransport.SMTP:
        assert row is not None
        if not row.smtp_host:
            return TestResult(False, "Enter the SMTP host.")
        if smtp_port_refusal(row):
            return TestResult(False, SMTP_PORT_REFUSED)
    elif not installation_configured():
        return TestResult(False, "The server has no SMTP settings (SOFTWARDEN_SMTP_HOST).")
    guard = SmtpGuard(restrict=transport == EmailTransport.SMTP)
    try:
        conn = smtp_connection(row, guard)
        with guard:
            conn.open()
            try:
                conn.close()
            except Exception:  # noqa: S110
                pass
    except credentials.CredentialError as e:
        return TestResult(False, str(e))
    except Exception as e:
        return TestResult(False, str(smtp_open_failure(e)))
    where = "the server's SMTP settings" if transport == EmailTransport.INSTALLATION_DEFAULT else "the SMTP server"
    return TestResult(True, f"Connected to {where}. A test email proves delivery.")
