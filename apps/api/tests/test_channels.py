"""Channels (API): the shared Graph client (core/graph.py), the SSRF guard (core/outbound.py), email settings,
Teams and webhook destinations, connection tests and test sends, the notification log and resend; roles, secrets and
tenancy. Transports are mocked: the Graph simulator (core/graph_simulator.py), an in-process SMTP server
(tests/fake_smtp.py), Django's in-memory mail backend, and a fake for the webhook POST. Nothing contacts Microsoft or
Teams."""

import json
import socket
from datetime import timedelta
from typing import Any

import pytest
from django.core import mail
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from catalog.audit_log import LABELS
from core import credentials, graph, graph_simulator, outbound
from core.models import AuditEvent, Job
from discovery import entra
from notify import channel_services, channels, deliveries
from notify.models import (
    Delivery,
    DeliveryKind,
    DeliveryStatus,
    EmailSettings,
    Reminder,
    TeamsDestination,
)
from tests.conftest import snapshot_problems
from tests.fake_smtp import FakeSmtp, Script

GRAPH_SECRET = "graph-client-secret-value-123"
SMTP_PASSWORD = "smtp-password-value-456"
TEAMS_URL = (
    "https://prod-00.westus.logic.azure.example/workflows/abc/triggers/manual/paths/invoke?sig=TEAMS-SIGNATURE-xyz"
)
WEBHOOK_URL = "https://hooks.example.test/softwarden?token=WEBHOOK-BEARER-789"
SIGNING_SECRET = "signing-secret-value-0123456789"
SECRETS = (GRAPH_SECRET, SMTP_PASSWORD, TEAMS_URL, WEBHOOK_URL, SIGNING_SECRET, "TEAMS-SIGNATURE", "WEBHOOK-BEARER")
GRAPH_SETTINGS = {
    "transport": "graph",
    "graph_tenant_id": "simulated",
    "graph_client_id": "11111111-2222-4333-8444-555555555555",
    "graph_secret": GRAPH_SECRET,
    "sender_mailbox": "softwarden@contoso.example",
}


RELAY = "mail.example.test"  # an organization's SMTP relay name, pinned to the in-process server on loopback


@pytest.fixture(autouse=True)
def relay_on_loopback(monkeypatch):
    """Organization SMTP hosts may not be loopback (core/outbound.smtp_address); the fake server is, so the relay's
    name resolves to it here. Every other host is checked for real."""
    real = outbound.smtp_address
    monkeypatch.setattr(outbound, "smtp_address", lambda host, port: "127.0.0.1" if host == RELAY else real(host, port))


@pytest.fixture(autouse=True)
def clean_simulator():
    graph_simulator.OUTBOX.clear()
    channels.forget_tokens()
    yield
    graph_simulator.OUTBOX.clear()
    channels.forget_tokens()


@pytest.fixture
def posts(monkeypatch):
    """The webhook POST, faked: records (url, body, headers) and answers from `replies` (default 202)."""
    calls: list = []
    replies: list = []

    def fake(url, body, headers, timeout=15):
        outbound.check_url(url)
        calls.append((url, body, dict(headers)))
        if replies:
            step = replies.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step
        return graph.Reply(202, {}, b"")

    monkeypatch.setattr(channels, "post_https", fake)
    fake.calls = calls  # type: ignore[attr-defined]
    fake.replies = replies  # type: ignore[attr-defined]
    return fake


def no_secret_in(text: str) -> bool:
    return not any(s in text for s in SECRETS)


def all_events_clean(org):
    for event in AuditEvent.objects.filter(organization=org):
        assert snapshot_problems(event) == []
        stored = json.dumps([event.changes, event.prechange, event.postchange])
        assert no_secret_in(stored), event.action


# ---- core/graph.py ----------------------------------------------------------------------------------------------------


def test_retry_after_reads_seconds_and_http_dates():
    from datetime import UTC, datetime

    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    assert graph.parse_retry_after("120") == 120.0
    assert graph.parse_retry_after("Thu, 05 Nov 2026 12:00:30 GMT", now) == 30.0
    assert graph.parse_retry_after("Thu, 05 Nov 2026 11:00:00 GMT", now) == 0.0
    assert graph.parse_retry_after("soon") is None and graph.parse_retry_after(None) is None


def test_graph_errors_are_read_case_insensitively():
    body = {"Error": {"Code": "ErrorAccessDenied", "Message": "Access is denied.", "InnerError": {"date": "x"}}}
    error = graph.error_of(graph.Reply(403, {"retry-after": "7"}, json.dumps(body).encode()))
    assert (error.status, error.code, error.message, error.retry_after) == (
        403,
        "ErrorAccessDenied",
        "Access is denied.",
        7,
    )
    lower = graph.error_of(graph.Reply(429, {}, b'{"error":{"code":"TooManyRequests","innererror":{}}}'))
    assert lower.code == "TooManyRequests" and lower.throttled
    oauth = graph.error_of(graph.Reply(401, {}, b'{"error":"invalid_client","error_description":"bad secret"}'))
    assert oauth.code == "invalid_client" and oauth.message == "bad secret"
    assert graph.error_of(graph.Reply(502, {}, b"<html>gateway</html>")).code == ""


def test_an_empty_202_is_success_and_carries_the_client_request_id():
    transport = graph_simulator.Transport()
    token = graph.request_token(transport, "simulated", "client", "secret")["access_token"]
    reply = graph.send_mail(transport, token, "softwarden@contoso.example", {"subject": "x"}, "abc-123")
    assert reply.status == 202 and reply.body == b"" and reply.json() is None
    method, url, headers = transport.calls[-1]
    assert url == "https://graph.microsoft.com/v1.0/users/softwarden@contoso.example/sendMail"
    assert headers["client-request-id"] == "abc-123" and headers["return-client-request-id"] == "true"
    sent = graph_simulator.OUTBOX[-1]
    assert sent["client_request_id"] == "abc-123"


def test_save_to_sent_items_is_a_real_json_boolean():
    transport = graph_simulator.Transport()
    graph.send_mail(transport, graph_simulator.TOKEN, "a@contoso.example", {"subject": "x"}, "id")
    # The simulator refuses anything but the boolean false; check the bytes too.
    assert graph_simulator.OUTBOX[-1]["message"] == {"subject": "x"}


def test_the_transport_refuses_other_hosts_and_plain_http():
    transport = graph.HttpTransport()
    for url in (
        "https://example.test/v1.0/me",
        "http://graph.microsoft.com/v1.0/me",
        "https://user:pw@graph.microsoft.com/v1.0/me",
    ):
        with pytest.raises(graph.HostRefused):
            transport.send("GET", url, {})


def test_discovery_keeps_its_messages_on_the_shared_client(monkeypatch):
    """discovery/entra.py moved onto core/graph.py with no change in what a run reports."""
    answers: list = []
    monkeypatch.setattr(graph.HttpTransport, "send", lambda self, *a, **k: answers.pop(0))
    t = entra.HttpTransport()
    cases = [
        (graph.Reply(401, {}, b""), "Entra refused the request (401)"),
        (graph.Reply(403, {}, b""), "Entra refused the request (403)"),
        (graph.Reply(429, {"retry-after": "5"}, b""), "Entra is throttling requests (429)"),
        (graph.Reply(500, {}, b""), "Entra answered 500."),
        (graph.Reply(302, {"location": "https://evil.test"}, b""), "Entra answered 302."),
        (graph.Reply(200, {}, b"not json"), "Couldn't reach Entra from this server."),
        (graph.Unreachable("TimeoutError", ambiguous=True), "Couldn't reach Entra from this server."),
    ]
    for reply, message in cases:

        def answer(self, *a, reply=reply, **k):
            if isinstance(reply, BaseException):
                raise reply
            return reply

        monkeypatch.setattr(graph.HttpTransport, "send", answer)
        with pytest.raises(entra.SourceError, match=message.replace("(", r"\(").replace(")", r"\)")):
            t.get("https://graph.microsoft.com/v1.0/servicePrincipals", {})
    monkeypatch.setattr(graph.HttpTransport, "send", lambda self, *a, **k: graph.Reply(200, {}, b'{"value": []}'))
    assert t.get("https://graph.microsoft.com/v1.0/servicePrincipals", {}) == {"value": []}
    assert t.post_form("https://login.microsoftonline.com/x/oauth2/v2.0/token", {"a": "b"}) == {"value": []}


# ---- core/outbound.py: SSRF ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.test/x",
        "https://localhost/x",
        "https://127.0.0.1/x",
        "https://10.0.0.5/x",
        "https://192.168.1.1/x",
        "https://172.16.0.1/x",
        "https://100.64.0.1/x",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/x",
        "https://[fe80::1]/x",
        "https://[fd00::1]/x",
        "https://[::ffff:127.0.0.1]/x",
        "https://0.0.0.0/x",
        # IPv6 forms that embed or translate IPv4
        "https://[::127.0.0.1]/x",
        "https://[::7f00:1]/x",
        "https://[64:ff9b::7f00:1]/x",
        "https://[64:ff9b::a9fe:a9fe]/x",
        "https://[64:ff9b::a00:1]/x",
        "https://[64:ff9b:1::a00:1]/x",
        "https://[::ffff:0:7f00:1]/x",
        "https://[::ffff:169.254.169.254]/x",
        "https://[2002:7f00:1::]/x",
        "https://[2001:0:4136:e378::1]/x",
        "https://a..b/x",
        "https://user:pass@hooks.example.test/x",
        "https://intranet/x",
        "https://printer.local/x",
        "https://metadata.internal/x",
        "ftp://hooks.example.test/x",
        "https://" + "a" * 2050 + ".test/",
        "",
    ],
)
def test_unsafe_addresses_are_refused(url):
    with pytest.raises(outbound.UnsafeDestination) as refused:
        outbound.check_url(url)
    assert "TEAMS" not in str(refused.value) and (not url or url not in str(refused.value))


def test_public_https_addresses_pass():
    for url in (
        "https://hooks.example.test/x?sig=1",
        "https://8.8.8.8/x",
        "https://prod-1.westus.logic.azure.com:443/w",
    ):
        assert outbound.check_url(url).scheme == "https"


def test_a_name_resolving_to_a_private_address_is_refused_at_send(monkeypatch):
    def resolve(host, port, *args, **kwargs):
        addresses = {"evil.example.test": ["10.1.2.3"], "mixed.example.test": ["93.184.216.34", "127.0.0.1"]}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses[host]]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    connected: list = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: connected.append(a))
    for host in ("evil.example.test", "mixed.example.test"):
        with pytest.raises(outbound.UnsafeDestination):
            outbound.post(f"https://{host}/hook", b"{}", {})
    with pytest.raises(outbound.UnsafeDestination):
        outbound.post("https://127.0.0.1/hook", b"{}", {})
    assert connected == []  # refused before any connection


def test_an_unresolvable_host_is_retryable_not_unsafe(monkeypatch):
    def fail(*a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(graph.Unreachable) as e:
        outbound.post("https://nowhere.example.test/hook", b"{}", {})
    assert e.value.ambiguous is False


# ---- email settings ---------------------------------------------------------------------------------------------------


def test_email_settings_default_to_the_installation(a, api):
    body = api(a["users"]["admin"]).get("notification-settings/email").json_body
    assert body["saved"] is False and body["transport"] == "installation_default"
    assert set(body["installation_default"]) == {"configured", "from_address"}


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_channels_and_the_log_are_admin_only(a, api, role):
    client = api(a["users"][role])
    for path in (
        "notification-settings/email",
        "notification-settings/teams-destinations",
        "notification-settings/webhook-destinations",
        "notification-log",
    ):
        assert client.get(path).status_code == 403, path
    assert client.patch("notification-settings/email", {"transport": "smtp"}).status_code == 403
    assert client.delete("notification-settings/email").status_code == 403
    assert client.post("notification-settings/email/test-connection").status_code == 403
    assert client.post("notification-settings/email/test-send").status_code == 403
    assert client.post("notification-settings/teams-destinations", {"name": "x"}).status_code == 403
    assert not EmailSettings.objects.exists() and not TeamsDestination.objects.exists()


def test_graph_settings_store_the_secret_encrypted_and_never_return_it(a, api):
    admin = api(a["users"]["admin"])
    response = admin.patch("notification-settings/email", GRAPH_SETTINGS)
    assert response.status_code == 200, response.json_body
    body = response.json_body
    assert body["saved"] and body["transport"] == "graph" and body["graph_secret_set"] is True
    assert body["graph_secret_set_on"] == timezone.now().date().isoformat()
    row = EmailSettings.objects.get(organization=a["org"])
    assert row.graph_secret_ciphertext and GRAPH_SECRET not in row.graph_secret_ciphertext
    assert credentials.slot_value(row, "graph_secret") == GRAPH_SECRET
    assert no_secret_in(response.content.decode()) and row.graph_secret_ciphertext not in response.content.decode()
    assert no_secret_in(admin.get("notification-settings/email").content.decode())
    event = AuditEvent.objects.get(action="email_settings.updated")
    assert event.changes["replaced"] == ["graph_secret"] and "transport" in event.changes["fields"]
    post: Any = event.postchange
    assert post["transport"] == "graph" and post["sender_mailbox"] == GRAPH_SETTINGS["sender_mailbox"]
    all_events_clean(a["org"])


def test_graph_settings_must_be_complete_and_valid(a, api):
    admin = api(a["users"]["admin"])
    missing = admin.patch("notification-settings/email", {"transport": "graph"})
    assert missing.status_code == 400
    assert {"graph_tenant_id", "graph_client_id", "sender_mailbox", "graph_secret"} <= set(missing.json_body["errors"])
    bad = admin.patch(
        "notification-settings/email",
        {**GRAPH_SETTINGS, "graph_client_id": "not-a-guid", "sender_mailbox": "nope", "graph_tenant_id": "169.254.1.1"},
    )
    assert {"graph_client_id", "sender_mailbox", "graph_tenant_id"} <= set(bad.json_body["errors"])
    assert admin.patch("notification-settings/email", {"graph_secret": "  "}).status_code == 400
    assert admin.patch("notification-settings/email", {"colour": "red"}).status_code == 400
    assert not EmailSettings.objects.exists()


def test_smtp_settings_and_clearing_the_password(a, api):
    admin = api(a["users"]["admin"])
    smtp = {
        "transport": "smtp",
        "smtp_host": "smtp.contoso.example",
        "smtp_security": "starttls",
        "smtp_username": "mailer",
        "smtp_password": SMTP_PASSWORD,
        "from_address": "softwarden@contoso.example",
    }
    body = admin.patch("notification-settings/email", smtp).json_body
    assert body["smtp_password_set"] and body["smtp_port"] is None and body["smtp_username"] == "mailer"
    assert admin.patch("notification-settings/email", {"smtp_host": "https://smtp.x"}).status_code == 400
    assert admin.patch("notification-settings/email", {"smtp_port": 70000}).status_code == 400
    assert admin.patch("notification-settings/email", {"smtp_security": "ssl3"}).status_code == 400
    cleared = admin.patch("notification-settings/email", {"smtp_password": None, "smtp_port": 2525}).json_body
    assert cleared["smtp_password_set"] is False and cleared["smtp_port"] == 2525
    assert EmailSettings.objects.get().smtp_password_ciphertext == ""
    all_events_clean(a["org"])


def test_cloud_mode_allows_mail_ports_only(a, api, settings):
    """In cloud mode an organization's SMTP port is a mail port (25, 465, 587, 2525)
    unless SOFTWARDEN_SMTP_ANY_PORT widens it, so the test connection can't map the server's network. Self-hosted
    takes any port (the default above)."""
    settings.SOFTWARDEN_SMTP_ANY_PORT = False
    admin = api(a["users"]["admin"])
    smtp = {
        "transport": "smtp",
        "smtp_host": "smtp.contoso.example",
        "smtp_security": "none",
        "from_address": "softwarden@contoso.example",
    }
    refused = admin.patch("notification-settings/email", {**smtp, "smtp_port": 8934})
    assert refused.status_code == 400 and "smtp_port" in refused.json_body["errors"]
    assert "587" in refused.json_body["errors"]["smtp_port"][0]
    assert not EmailSettings.objects.exists()
    for port in (25, 465, 587, 2525, None):
        assert admin.patch("notification-settings/email", {**smtp, "smtp_port": port}).status_code == 200, port
    settings.SOFTWARDEN_SMTP_ANY_PORT = True
    assert admin.patch("notification-settings/email", {"smtp_port": 8934}).status_code == 200


def test_email_settings_honour_if_match_and_delete_goes_back_to_the_default(a, api):
    admin = api(a["users"]["admin"])
    first = admin.get("notification-settings/email")
    saved = admin.client.patch(
        "/api/v1/notification-settings/email",
        json.dumps(GRAPH_SETTINGS),
        content_type="application/json",
        HTTP_IF_MATCH=first["ETag"],
    )
    assert saved.status_code == 200 and saved["ETag"] != first["ETag"]
    stale = admin.client.patch(
        "/api/v1/notification-settings/email",
        json.dumps({"sender_mailbox": "other@contoso.example"}),
        content_type="application/json",
        HTTP_IF_MATCH=first["ETag"],
    )
    assert stale.status_code == 412
    assert admin.delete("notification-settings/email").status_code == 204
    assert admin.get("notification-settings/email").json_body["saved"] is False
    assert AuditEvent.objects.filter(action="email_settings.deleted").count() == 1
    all_events_clean(a["org"])


def test_connection_test_gets_a_token_only_and_says_so(a, api):
    admin = api(a["users"]["admin"])
    admin.patch("notification-settings/email", GRAPH_SETTINGS)
    ok = admin.post("notification-settings/email/test-connection").json_body
    assert ok["ok"] is True and "Only a test email proves" in ok["detail"]
    assert graph_simulator.OUTBOX == []  # nothing was sent
    channel_services.update_email_settings(a["org"], a["users"]["admin"], {"graph_secret": "wrong-secret"})
    refused = admin.post("notification-settings/email/test-connection").json_body
    assert refused["ok"] is False and "invalid_client" in refused["detail"]
    row = EmailSettings.objects.get()
    assert row.last_test_ok is False and row.last_test_at is not None
    assert AuditEvent.objects.filter(action="email_settings.tested").count() == 2
    all_events_clean(a["org"])


def test_connection_test_against_an_smtp_server_signs_in(a, api):
    admin = api(a["users"]["admin"])
    with FakeSmtp(Script(username="mailer", password=SMTP_PASSWORD)) as server:
        channel_services.update_email_settings(
            a["org"],
            a["users"]["admin"],
            {
                "transport": "smtp",
                "smtp_host": RELAY,
                "smtp_port": server.port,
                "smtp_security": "none",
                "smtp_username": "mailer",
                "smtp_password": SMTP_PASSWORD,
                "from_address": "softwarden@contoso.example",
            },
        )
        assert admin.post("notification-settings/email/test-connection").json_body["ok"] is True
        channel_services.update_email_settings(a["org"], a["users"]["admin"], {"smtp_password": "wrong"})
        refused = admin.post("notification-settings/email/test-connection").json_body
    assert refused["ok"] is False and "refused the user name or password (535)" in refused["detail"]
    assert server.script.messages == []


def test_a_port_saved_before_cloud_mode_is_refused_at_test_and_send(a, api, settings):
    """A row saved while any port was allowed isn't used on another port once
    SOFTWARDEN_SMTP_ANY_PORT is off. The test connection and a send both refuse it with the save's message, before
    connecting."""
    from types import SimpleNamespace

    from notify import channels

    admin = api(a["users"]["admin"])
    with FakeSmtp(Script()) as server:
        settings.SOFTWARDEN_SMTP_ANY_PORT = True
        channel_services.update_email_settings(
            a["org"],
            a["users"]["admin"],
            {
                "transport": "smtp",
                "smtp_host": RELAY,
                "smtp_port": server.port,
                "smtp_security": "none",
                "from_address": "softwarden@contoso.example",
            },
        )
        assert server.port not in channels.MAIL_PORTS
        assert admin.post("notification-settings/email/test-connection").json_body["ok"] is True
        settings.SOFTWARDEN_SMTP_ANY_PORT = False
        refused = admin.post("notification-settings/email/test-connection").json_body
        assert refused == {**refused, "ok": False, "detail": channels.SMTP_PORT_REFUSED}
        delivery = SimpleNamespace(id=1, recipient="pat@orga.test", subject="s", content={"text": "t"})
        with pytest.raises(channels.DeliveryFailure) as failed:
            channels.send_email(a["org"], delivery)  # type: ignore[arg-type]
        assert str(failed.value) == channels.SMTP_PORT_REFUSED
    assert server.script.messages == []


def test_test_send_through_graph_reaches_the_admin(a, api):
    admin = api(a["users"]["admin"])
    admin.patch("notification-settings/email", GRAPH_SETTINGS)
    body = admin.post("notification-settings/email/test-send").json_body
    assert body["ok"] is True and body["detail"] == "Sent."
    delivery = body["delivery"]
    assert delivery["kind"] == "test" and delivery["status"] == "sent" and delivery["recipient"] == "admin@orga.test"
    sent = graph_simulator.OUTBOX[-1]
    assert sent["sender"] == GRAPH_SETTINGS["sender_mailbox"]
    assert sent["message"]["toRecipients"] == [{"emailAddress": {"address": "admin@orga.test"}}]
    assert sent["message"]["internetMessageHeaders"] == [
        {"name": "x-softwarden-notification-id", "value": delivery["id"]}
    ]
    assert sent["client_request_id"] == delivery["client_request_id"]
    assert EmailSettings.objects.get().last_test_ok is True
    assert not Job.objects.filter(kind=deliveries.JOB_KIND).exists()  # sent in the request, no job
    all_events_clean(a["org"])


def test_test_send_reports_a_mailbox_outside_the_rbac_scope(a, api):
    admin = api(a["users"]["admin"])
    admin.patch("notification-settings/email", {**GRAPH_SETTINGS, "sender_mailbox": "denied@contoso.example"})
    body = admin.post("notification-settings/email/test-send").json_body
    assert body["ok"] is False and "ErrorAccessDenied" in body["detail"] and "Exchange RBAC" in body["detail"]
    assert body["delivery"]["status"] == "failed"


def test_test_send_through_the_installation_default(a, api, settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    body = api(a["users"]["admin"]).post("notification-settings/email/test-send").json_body
    assert body["ok"] is True
    assert [m.to for m in mail.outbox] == [["admin@orga.test"]]
    message: Any = mail.outbox[0]
    assert message.subject == "Softwarden test email" and message.extra_headers["x-softwarden-notification-id"]
    assert message.alternatives[0][1] == "text/html"


def test_the_installation_defaults_last_test_is_recorded_without_a_settings_row(a, b, api, settings):
    """With no EmailSettings row, a test of the installation default is kept on the organization (not as a row: the
    settings stay unsaved), so the status no longer reads "Never tested"; a failing test replaces it."""
    from notify.models import EmailSettings

    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    admin = api(a["users"]["admin"])
    before = admin.get("notification-settings/email")
    assert before.json_body["last_test_at"] is None and before.json_body["last_test_ok"] is None
    assert admin.post("notification-settings/email/test-send").json_body["ok"] is True
    after = admin.get("notification-settings/email")
    body = after.json_body
    assert body["saved"] is False and not EmailSettings.objects.filter(organization=a["org"]).exists()
    assert body["last_test_ok"] is True and body["last_test_at"] and body["last_test_detail"] == "Sent."
    assert after["ETag"] == before["ETag"]  # a test changes no setting
    settings.SOFTWARDEN_EMAIL_CONFIGURED = False
    admin.post("notification-settings/email/test-send")
    body = admin.get("notification-settings/email").json_body
    assert body["last_test_ok"] is False and "SOFTWARDEN_SMTP_HOST" in body["last_test_detail"]
    assert api(b["users"]["admin"]).get("notification-settings/email").json_body["last_test_at"] is None  # its own


def test_test_send_without_any_email_configured_is_skipped(a, api, settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = False
    body = api(a["users"]["admin"]).post("notification-settings/email/test-send").json_body
    assert body["ok"] is False and body["delivery"]["status"] == "skipped"
    assert "SOFTWARDEN_SMTP_HOST" in body["detail"]


def test_test_send_through_an_smtp_server(a, api):
    with FakeSmtp() as server:
        channel_services.update_email_settings(
            a["org"],
            a["users"]["admin"],
            {
                "transport": "smtp",
                "smtp_host": RELAY,
                "smtp_port": server.port,
                "smtp_security": "none",
                "from_address": "softwarden@contoso.example",
            },
        )
        body = api(a["users"]["admin"]).post("notification-settings/email/test-send").json_body
    assert body["ok"] is True
    [message] = server.script.messages
    assert message["to"] == ["<admin@orga.test>"] and "x-softwarden-notification-id" in message["data"]


# ---- Teams and webhook destinations -------------------------------------------------------------------------------


def test_teams_destination_url_is_write_only(a, api):
    admin = api(a["users"]["admin"])
    created = admin.post("notification-settings/teams-destinations", {"name": "IT Ops", "url": TEAMS_URL})
    assert created.status_code == 201, created.json_body
    body = created.json_body
    assert body["url_host"] == "prod-00.westus.logic.azure.example" and body["enabled"] is True
    assert "url" not in body and no_secret_in(created.content.decode())
    row = TeamsDestination.objects.get()
    assert credentials.slot_value(row, "url") == TEAMS_URL and TEAMS_URL not in row.url_ciphertext
    assert no_secret_in(admin.get("notification-settings/teams-destinations").content.decode())
    assert no_secret_in(admin.get(f"notification-settings/teams-destinations/{row.id}").content.decode())
    event = AuditEvent.objects.get(action="teams_destination.created")
    assert event.changes == {"name": "IT Ops", "replaced": ["url"]}
    assert event.postchange == {"name": "IT Ops", "enabled": True, "url_host": row.url_host, "last_test_ok": None}
    all_events_clean(a["org"])


def test_teams_destinations_refuse_unsafe_and_retired_addresses(a, api):
    admin = api(a["users"]["admin"])
    for url, words in (
        ("http://example.test/x", "https://"),
        ("https://10.0.0.1/x", "private"),
        ("https://contoso.webhook.office.com/webhookb2/abc", "retired"),
        ("https://outlook.office.com/webhook/abc", "retired"),
        ("", "Enter"),
    ):
        refused = admin.post("notification-settings/teams-destinations", {"name": "X", "url": url})
        assert refused.status_code == 400 and words in refused.json_body["errors"]["url"][0], url
    admin.post("notification-settings/teams-destinations", {"name": "IT Ops", "url": TEAMS_URL})
    clash = admin.post("notification-settings/teams-destinations", {"name": "it ops", "url": TEAMS_URL})
    assert clash.status_code == 400 and "name" in clash.json_body["errors"]
    assert TeamsDestination.objects.count() == 1


def test_replacing_a_teams_url_resets_its_test_and_renaming_keeps_it(a, api, posts):
    admin = api(a["users"]["admin"])
    row_id = admin.post("notification-settings/teams-destinations", {"name": "IT Ops", "url": TEAMS_URL}).json_body[
        "id"
    ]
    assert admin.post(f"notification-settings/teams-destinations/{row_id}/test-send").json_body["ok"] is True
    renamed = admin.patch(f"notification-settings/teams-destinations/{row_id}", {"name": "Ops"}).json_body
    assert renamed["name"] == "Ops" and renamed["last_test_ok"] is True
    replaced = admin.patch(
        f"notification-settings/teams-destinations/{row_id}", {"url": TEAMS_URL.replace("abc", "def")}
    ).json_body
    assert replaced["last_test_ok"] is None
    event: Any = AuditEvent.objects.filter(action="teams_destination.updated").order_by("created_at").last()
    assert event.changes["replaced"] == ["url"]
    all_events_clean(a["org"])


def test_teams_test_send_posts_an_adaptive_card(a, api, posts):
    admin = api(a["users"]["admin"])
    row_id = admin.post("notification-settings/teams-destinations", {"name": "IT Ops", "url": TEAMS_URL}).json_body[
        "id"
    ]
    body = admin.post(f"notification-settings/teams-destinations/{row_id}/test-send").json_body
    assert body["ok"] is True and body["delivery"]["channel"] == "teams"
    assert body["delivery"]["destination"] == {"id": row_id, "name": "IT Ops"}
    [(url, payload, headers)] = posts.calls
    assert url == TEAMS_URL and headers["Content-Type"] == "application/json"
    envelope = json.loads(payload)
    assert envelope["type"] == "message"
    [attachment] = envelope["attachments"]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive" and attachment["contentUrl"] is None
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.2"
    assert card["$schema"] == "http://adaptivecards.io/schemas/adaptive-card.json"
    assert "Action.Submit" not in payload.decode() and "<" not in json.dumps(card["body"])
    assert AuditEvent.objects.filter(action="teams_destination.tested").count() == 1
    all_events_clean(a["org"])


def test_a_turned_off_destination_refuses_a_test(a, api, posts):
    admin = api(a["users"]["admin"])
    row_id = admin.post(
        "notification-settings/teams-destinations", {"name": "IT Ops", "url": TEAMS_URL, "enabled": False}
    ).json_body["id"]
    assert admin.post(f"notification-settings/teams-destinations/{row_id}/test-send").status_code == 409
    assert posts.calls == []


def test_webhook_destinations_need_a_signing_secret_and_sign_their_test(a, api, posts):
    import hashlib
    import hmac

    admin = api(a["users"]["admin"])
    short = admin.post(
        "notification-settings/webhook-destinations", {"name": "Hook", "url": WEBHOOK_URL, "signing_secret": "short"}
    )
    assert short.status_code == 400 and "signing_secret" in short.json_body["errors"]
    created = admin.post(
        "notification-settings/webhook-destinations",
        {"name": "Hook", "url": WEBHOOK_URL, "signing_secret": SIGNING_SECRET},
    )
    assert created.status_code == 201 and created.json_body["signing_secret_set_on"]
    assert no_secret_in(created.content.decode())
    body = admin.post(f"notification-settings/webhook-destinations/{created.json_body['id']}/test-send").json_body
    assert body["ok"] is True
    [(url, payload, headers)] = posts.calls
    assert url == WEBHOOK_URL
    stamp, digest = (part.split("=", 1)[1] for part in headers["X-Softwarden-Signature"].split(","))
    expected = hmac.new(SIGNING_SECRET.encode(), stamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(digest, expected)
    envelope = json.loads(payload)  # the formalised envelope (docs/API.md "Webhooks"); a ping is a test
    assert envelope["type"] == "ping" and envelope["id"] == body["delivery"]["id"] == headers["X-Softwarden-Delivery"]
    assert headers["X-Softwarden-Event"] == "ping" and envelope["organization"] == {"id": str(a["org"].id)}
    assert envelope["actor"] == "system" and envelope["occurred_at"].endswith("Z")
    all_events_clean(a["org"])


def test_deleting_a_destination_keeps_its_log_rows(a, api, posts):
    admin = api(a["users"]["admin"])
    row_id = admin.post("notification-settings/teams-destinations", {"name": "IT Ops", "url": TEAMS_URL}).json_body[
        "id"
    ]
    sent = admin.post(f"notification-settings/teams-destinations/{row_id}/test-send").json_body["delivery"]
    assert admin.delete(f"notification-settings/teams-destinations/{row_id}").status_code == 204
    row = admin.get(f"notification-log/{sent['id']}").json_body
    assert row["destination"] == {"id": None, "name": "IT Ops"}
    resend = admin.post(f"notification-log/{sent['id']}/resend")
    assert resend.status_code == 409 and "deleted" in resend.json_body["detail"]


# ---- the notification log ---------------------------------------------------------------------------------------------


def _email(org, key, **kwargs):
    defaults = {
        "channel": "email",
        "dedupe_key": key,
        "subject": f"Subject {key}",
        "content": {"text": "Hello", "html": "<p>Hello</p>"},
        "recipient": "someone@orga.test",
    }
    return deliveries.enqueue(org, **{**defaults, **kwargs})[0]


def test_the_log_lists_newest_first_with_filters(a, api, posts):
    admin = api(a["users"]["admin"])
    reminder = Reminder.objects.create(
        organization=a["org"],
        source_type="periodic_review",
        source_id=a["app"].id,
        resource=a["app"],
        due_date=timezone.now().date() + timedelta(days=30),
        title="Payroll",
        label="Periodic review due",
    )
    first = _email(a["org"], "k1", reminder=reminder, rule_name="Renewals", lead_time_days=30)
    second = _email(a["org"], "k2", kind=DeliveryKind.DIGEST)
    Delivery.objects.filter(id=first.id).update(created_at=timezone.now() - timedelta(days=3))
    rows = admin.get("notification-log").json_body["results"]
    assert [r["id"] for r in rows] == [str(second.id), str(first.id)]
    assert rows[1]["reminder"]["title"] == "Payroll" and rows[1]["rule_name"] == "Renewals"
    assert rows[1]["lead_time_days"] == 30 and rows[0]["recipient"] == "someone@orga.test"
    assert [r["id"] for r in admin.get(f"notification-log?reminder={reminder.id}").json_body["results"]] == [
        str(first.id)
    ]
    assert [r["id"] for r in admin.get("notification-log?kind=digest").json_body["results"]] == [str(second.id)]
    assert admin.get("notification-log?channel=teams").json_body["results"] == []
    assert len(admin.get("notification-log?status=queued,sent").json_body["results"]) == 2
    today = timezone.now().date()
    recent = admin.get(f"notification-log?from={today - timedelta(days=1)}").json_body["results"]
    assert [r["id"] for r in recent] == [str(second.id)]
    older = admin.get(f"notification-log?to={today - timedelta(days=2)}").json_body["results"]
    assert [r["id"] for r in older] == [str(first.id)]
    assert admin.get("notification-log?channel=fax").status_code == 400
    assert admin.get("notification-log?from=yesterday").status_code == 400
    detail = admin.get(f"notification-log/{first.id}").json_body
    assert detail["content"] == {"text": "Hello", "html": "<p>Hello</p>"} and detail["resends"] == []


def test_resend_makes_a_new_delivery_with_a_fresh_key(a, api, settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    admin = api(a["users"]["admin"])
    original = _email(a["org"], "reminder:x")
    assert admin.post(f"notification-log/{original.id}/resend").status_code == 409  # still queued
    deliveries.attempt(original.id)
    response = admin.post(f"notification-log/{original.id}/resend")
    assert response.status_code == 202
    copy = Delivery.objects.get(id=response.json_body["id"])
    assert copy.resend_of_id == original.id and copy.dedupe_key != original.dedupe_key
    assert copy.status == DeliveryStatus.QUEUED and copy.requested_by == a["users"]["admin"]
    assert copy.content == original.content and copy.recipient == original.recipient
    assert Job.objects.filter(kind=deliveries.JOB_KIND, payload={"delivery_id": str(copy.id)}).exists()
    assert admin.get(f"notification-log/{original.id}").json_body["resends"] == [str(copy.id)]
    event = AuditEvent.objects.get(action="notification.resent")
    assert event.target_id == copy.id and event.changes == {"of": str(original.id), "channel": "email"}
    deliveries.attempt(copy.id)
    assert len(mail.outbox) == 2
    all_events_clean(a["org"])


def test_another_organizations_log_and_destinations_stay_hidden(a, b, api, posts):
    theirs = _email(b["org"], "theirs")
    destination = channel_services.create_destination(
        b["org"], b["users"]["admin"], TeamsDestination, {"name": "B", "url": TEAMS_URL}
    )
    admin = api(a["users"]["admin"])
    assert admin.get("notification-log").json_body["results"] == []
    assert admin.get(f"notification-log/{theirs.id}").status_code == 404
    assert admin.post(f"notification-log/{theirs.id}/resend").status_code == 404
    assert admin.get("notification-settings/teams-destinations").json_body == []
    assert admin.post(f"notification-settings/teams-destinations/{destination.id}/test-send").status_code == 404
    assert posts.calls == []
    # b's email settings are b's alone
    channel_services.update_email_settings(b["org"], b["users"]["admin"], GRAPH_SETTINGS)
    assert admin.get("notification-settings/email").json_body["saved"] is False


def test_one_email_settings_row_per_organization(a):
    EmailSettings.objects.create(organization=a["org"])
    with pytest.raises(IntegrityError), transaction.atomic():
        EmailSettings.objects.create(organization=a["org"])
        connection.check_constraints()


def test_every_channel_action_has_a_label():
    for action in (
        "email_settings.updated",
        "email_settings.deleted",
        "email_settings.tested",
        "teams_destination.created",
        "webhook_destination.tested",
        "notification.resent",
    ):
        assert action in LABELS


def test_secrets_never_reach_a_response_an_event_a_job_or_the_log(a, api, posts, caplog):
    caplog.set_level("DEBUG")
    admin = api(a["users"]["admin"])
    responses = [
        admin.patch("notification-settings/email", GRAPH_SETTINGS),
        admin.post("notification-settings/email/test-send"),
        admin.post("notification-settings/teams-destinations", {"name": "IT Ops", "url": TEAMS_URL}),
        admin.post(
            "notification-settings/webhook-destinations",
            {"name": "Hook", "url": WEBHOOK_URL, "signing_secret": SIGNING_SECRET},
        ),
    ]
    teams_id = responses[2].json_body["id"]
    hook_id = responses[3].json_body["id"]
    posts.replies.append(graph.Reply(500, {}, TEAMS_URL.encode()))  # a server that echoes the URL
    responses.append(admin.post(f"notification-settings/teams-destinations/{teams_id}/test-send"))
    posts.replies.append(RuntimeError(f"boom while posting to {WEBHOOK_URL} with {SIGNING_SECRET}"))
    responses.append(admin.post(f"notification-settings/webhook-destinations/{hook_id}/test-send"))
    for delivery in Delivery.objects.all():
        responses.append(admin.post(f"notification-log/{delivery.id}/resend"))
    responses += [
        admin.get("notification-log"),
        admin.get("notification-settings/email"),
        admin.get("notification-settings/teams-destinations"),
        admin.get("notification-settings/webhook-destinations"),
    ]
    responses += [admin.get(f"notification-log/{d.id}") for d in Delivery.objects.all()]
    for response in responses:
        assert response.status_code < 500
        assert no_secret_in(response.content.decode()), response.content[:200]
    failed = Delivery.objects.filter(status=DeliveryStatus.FAILED)
    assert failed.count() == 2 and all(no_secret_in(d.last_error) for d in failed)
    assert any("[redacted]" in d.last_error for d in failed)
    for job in Job.objects.all():
        assert set(job.payload) == {"delivery_id"} and no_secret_in(json.dumps(job.payload))
    assert no_secret_in(caplog.text)
    all_events_clean(a["org"])


def test_the_schema_documents_channels_and_keeps_secrets_write_only(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    paths, schemas = doc["paths"], doc["components"]["schemas"]
    assert {
        "/api/v1/notification-settings/email",
        "/api/v1/notification-settings/email/test-connection",
        "/api/v1/notification-settings/email/test-send",
        "/api/v1/notification-settings/teams-destinations",
        "/api/v1/notification-settings/teams-destinations/{id}/test-send",
        "/api/v1/notification-settings/webhook-destinations/{id}",
        "/api/v1/notification-log",
        "/api/v1/notification-log/{id}/resend",
    } <= set(paths)
    for name, fields in (
        ("PatchedEmailSettingsInputRequest", ("graph_secret", "smtp_password")),
        ("PatchedTeamsDestinationInputRequest", ("url",)),
        ("TeamsDestinationInputRequest", ("url",)),
        ("WebhookDestinationInputRequest", ("url", "signing_secret")),
    ):
        for field in fields:
            assert schemas[name]["properties"][field].get("writeOnly") is True, (name, field)
    for name in ("EmailSettings", "TeamsDestination", "WebhookDestination", "Delivery", "DeliveryDetail"):
        assert not {"graph_secret", "smtp_password", "url", "signing_secret"} & set(schemas[name]["properties"]), name
    assert {"cursor", "channel", "status", "kind", "reminder", "from", "to"} <= {
        p["name"] for p in paths["/api/v1/notification-log"]["get"]["parameters"]
    }
    assert "202" in paths["/api/v1/notification-log/{id}/resend"]["post"]["responses"]


def test_channel_events_read_in_words_in_the_audit_log(a, api, posts):
    admin = api(a["users"]["admin"])
    admin.patch("notification-settings/email", GRAPH_SETTINGS)
    row_id = admin.post("notification-settings/teams-destinations", {"name": "IT Ops", "url": TEAMS_URL}).json_body[
        "id"
    ]
    admin.post(f"notification-settings/teams-destinations/{row_id}/test-send")
    rows = admin.get("audit-events").json_body["results"]
    by_action = {r["action"]: r for r in rows}
    email_row = json.dumps(by_action["email_settings.updated"])
    assert "replaced graph secret" in email_row and no_secret_in(email_row)
    assert "send succeeded" in json.dumps(by_action["teams_destination.tested"])
    assert no_secret_in(json.dumps(rows))


# ---- embedded IPv4, SMTP hosts, DNS rebinding, IDN, Markdown -----------------------------------------------------


@pytest.mark.parametrize(
    "answer", ["64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "::127.0.0.1", "::ffff:0:a00:1", "::ffff:10.0.0.1", "fd00::1"]
)
def test_a_hostile_aaaa_record_is_refused_at_send(monkeypatch, answer):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (answer, port, 0, 0))],
    )
    connected: list = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: connected.append(a))
    with pytest.raises(outbound.UnsafeDestination):
        outbound.post("https://hooks.example.test/in", b"{}", {})
    assert connected == []


def test_public_ipv6_still_passes():
    import ipaddress

    for value in ("2606:4700:4700::1111", "2001:4860:4860::8888", "::ffff:8.8.8.8"):
        assert outbound.public_ip(ipaddress.ip_address(value)), value


@pytest.mark.parametrize(
    "value,allowed",
    [
        ("10.0.0.25", True),
        ("192.168.1.10", True),
        ("172.20.0.5", True),
        ("fd00::25", True),
        ("::ffff:10.0.0.25", True),
        ("203.0.113.9", True),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("0.0.0.0", False),  # noqa: S104 (a refusal case, not a bind)
        ("0.1.2.3", False),
        ("224.0.0.1", False),
        ("240.0.0.1", False),
        ("::1", False),
        ("::", False),
        ("fe80::1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:169.254.169.254", False),
        ("::127.0.0.1", False),
        ("::ffff:0:a9fe:a9fe", False),
        ("64:ff9b::7f00:1", False),
        ("64:ff9b::a9fe:a9fe", False),
        ("64:ff9b:1::a00:1", False),
        ("2002:a9fe:a9fe::", False),
    ],
)
def test_organization_smtp_relays_may_be_private_but_never_local(value, allowed):
    import ipaddress

    assert outbound.smtp_ip_allowed(ipaddress.ip_address(value)) is allowed


def test_smtp_hosts_are_checked_on_save(a, api):
    admin = api(a["users"]["admin"])
    base = {"transport": "smtp", "from_address": "softwarden@contoso.example"}
    for host in ("127.0.0.1", "169.254.169.254", "0.0.0.0", "localhost", "relay.localhost"):  # noqa: S104
        refused = admin.patch("notification-settings/email", {**base, "smtp_host": host})
        assert refused.status_code == 400 and "isn't allowed" in refused.json_body["errors"]["smtp_host"][0], host
    assert admin.patch("notification-settings/email", {**base, "smtp_host": "10.0.0.25"}).status_code == 200


def test_an_smtp_name_resolving_to_metadata_is_refused_when_sending(a, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))],
    )
    channel_services.update_email_settings(
        a["org"],
        a["users"]["admin"],
        {
            "transport": "smtp",
            "smtp_host": "relay.example.test",
            "smtp_security": "none",
            "from_address": "s@x.example",
        },
    )
    delivery, _ = deliveries.enqueue(
        a["org"], channel="email", dedupe_key="meta", subject="x", content={"text": "x"}, recipient="d@orga.test"
    )
    done = deliveries.attempt(delivery.id)
    assert done is not None and done.status == DeliveryStatus.FAILED and "isn't allowed" in done.last_error
    result = channels.test_email_connection(a["org"])
    assert result.ok is False and "isn't allowed" in result.detail


def test_the_installations_own_smtp_host_is_not_restricted(a, settings):
    with FakeSmtp() as server:
        settings.SOFTWARDEN_EMAIL_CONFIGURED = True
        settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
        settings.EMAIL_HOST, settings.EMAIL_PORT, settings.EMAIL_USE_TLS = "127.0.0.1", server.port, False
        settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD = "", ""
        delivery, _ = deliveries.enqueue(
            a["org"], channel="email", dedupe_key="env", subject="x", content={"text": "x"}, recipient="d@orga.test"
        )
        done = deliveries.attempt(delivery.id)
    assert done is not None and done.status == DeliveryStatus.SENT and len(server.script.messages) == 1


# ---- An organization's SMTP connection never borrows the installation's credentials --------------------------

INSTALLATION_USER = "installation-relay-user"
INSTALLATION_PASSWORD = "installation-relay-password-789"  # noqa: S105 (a test value)


@pytest.fixture
def installation_credentials(settings):
    """The installation's relay signs in (SOFTWARDEN_SMTP_USER / _PASSWORD set): the point of these tests."""
    settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD = INSTALLATION_USER, INSTALLATION_PASSWORD


def _installation_relay() -> FakeSmtp:
    """A server that offers AUTH and would accept the installation's credentials, so a leak would succeed."""
    return FakeSmtp(Script(username=INSTALLATION_USER, password=INSTALLATION_PASSWORD))


def _smtp_settings(a, port: int, **extra) -> None:
    channel_services.update_email_settings(
        a["org"],
        a["users"]["admin"],
        {
            "transport": "smtp",
            "smtp_host": RELAY,
            "smtp_port": port,
            "smtp_security": "none",
            "from_address": "softwarden@contoso.example",
            **extra,
        },
    )


def _send(a, key: str):
    delivery, _ = deliveries.enqueue(
        a["org"], channel="email", dedupe_key=key, subject="x", content={"text": "x"}, recipient="d@orga.test"
    )
    return deliveries.attempt(delivery.id)


def test_an_organization_relay_without_credentials_never_gets_the_installations(a, api, installation_credentials):
    with _installation_relay() as server:
        _smtp_settings(a, server.port)
        done = _send(a, "sec1-blank")
        tested = api(a["users"]["admin"]).post("notification-settings/email/test-connection").json_body
    assert done is not None and done.status == DeliveryStatus.SENT, done and done.last_error
    assert tested["ok"] is True
    assert server.script.auths == []  # no sign-in at all, so the installation's user and password never left
    assert len(server.script.messages) == 1


def test_an_organization_user_name_without_a_password_never_gets_the_installations_password(
    a, installation_credentials
):
    with _installation_relay() as server:
        _smtp_settings(a, server.port, smtp_username="org-mailer")
        done = _send(a, "sec1-user")
        tested = channels.test_email_connection(a["org"])
    assert done is not None and done.status == DeliveryStatus.SENT, done and done.last_error
    assert tested.ok is True
    assert server.script.auths == []  # a user name with no password is no sign-in, not the installation's password


def test_an_organization_row_naming_the_installation_host_still_never_borrows_its_credentials(
    a, api, settings, installation_credentials
):
    """Saving refuses the installation's host; a row saved before the operator moved the installation onto that
    host still connects as the organization (restricted, its own blank credentials), never with the installation's."""
    with _installation_relay() as server:
        _smtp_settings(a, server.port)
        settings.EMAIL_HOST, settings.EMAIL_PORT = RELAY, server.port  # the installation's relay is now that host
        assert channel_services._installation_host(EmailSettings.objects.get(organization=a["org"]).smtp_host)
        done = _send(a, "sec1-same-host")
        tested = api(a["users"]["admin"]).post("notification-settings/email/test-connection").json_body
    assert done is not None and done.status == DeliveryStatus.SENT, done and done.last_error
    assert tested["ok"] is True
    assert server.script.auths == []


def test_an_organization_password_is_still_sent_as_its_own(a, installation_credentials):
    with FakeSmtp(Script(username="org-mailer", password=SMTP_PASSWORD)) as server:
        _smtp_settings(a, server.port, smtp_username="org-mailer", smtp_password=SMTP_PASSWORD)
        done = _send(a, "sec1-own")
    assert done is not None and done.status == DeliveryStatus.SENT, done and done.last_error
    assert server.script.auths == [("org-mailer", SMTP_PASSWORD)]


def test_the_guarded_backend_never_falls_back_to_the_installations_credentials_when_restricted(
    installation_credentials, settings
):
    from notify.smtp import GuardedEmailBackend, SmtpGuard

    settings.EMAIL_SSL_KEYFILE, settings.EMAIL_SSL_CERTFILE = "/installation/client.key", "/installation/client.crt"

    restricted = [
        GuardedEmailBackend(host=RELAY, guard=SmtpGuard(restrict=True)),
        GuardedEmailBackend(host=RELAY, username=None, password=None, guard=SmtpGuard(restrict=True)),
        GuardedEmailBackend(RELAY, 25, None, None, guard=SmtpGuard(restrict=True)),
        GuardedEmailBackend(host=RELAY, username="org-mailer", guard=SmtpGuard(restrict=True)),
    ]
    for backend in restricted:
        assert backend.password == "" and backend.username in ("", "org-mailer")
        assert not backend.ssl_keyfile and not backend.ssl_certfile
    kept = GuardedEmailBackend(host=RELAY, username="org-mailer", password="own", guard=SmtpGuard(restrict=True))
    assert (kept.username, kept.password) == ("org-mailer", "own")
    installation = GuardedEmailBackend(guard=SmtpGuard(restrict=False))
    assert (installation.username, installation.password) == (INSTALLATION_USER, INSTALLATION_PASSWORD)
    assert installation.ssl_certfile == "/installation/client.crt"
    default = GuardedEmailBackend()  # no guard: the installation's
    assert (default.username, default.password) == (INSTALLATION_USER, INSTALLATION_PASSWORD)


def test_the_installation_default_still_signs_in_with_its_settings(a, api, settings, installation_credentials):
    with _installation_relay() as server:
        settings.SOFTWARDEN_EMAIL_CONFIGURED = True
        settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
        settings.EMAIL_HOST, settings.EMAIL_PORT, settings.EMAIL_USE_TLS = "127.0.0.1", server.port, False
        done = _send(a, "sec1-installation")
        tested = api(a["users"]["admin"]).post("notification-settings/email/test-connection").json_body
    assert done is not None and done.status == DeliveryStatus.SENT, done and done.last_error
    assert tested["ok"] is True
    assert server.script.auths == [(INSTALLATION_USER, INSTALLATION_PASSWORD)] * 2


def test_connection_test_errors_never_tell_ports_apart(a):
    """Nothing listening, a server that hangs up at once, a non-SMTP answer: the same words (no port-scan oracle)."""
    import socketserver
    import threading

    class HangUp(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.close()

    class NotSmtp(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    ports = [closed_port]
    servers = []
    for handler in (HangUp, NotSmtp):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        ports.append(server.server_address[1])
    details = []
    try:
        for port in ports:
            channel_services.update_email_settings(
                a["org"],
                a["users"]["admin"],
                {
                    "transport": "smtp",
                    "smtp_host": RELAY,
                    "smtp_port": port,
                    "smtp_security": "starttls",
                    "from_address": "s@x.example",
                },
            )
            result = channels.test_email_connection(a["org"])
            assert result.ok is False
            details.append(result.detail)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    assert set(details) == {channels.SMTP_NO_CONNECTION}


def test_the_pinned_connection_uses_the_checked_address_and_the_host_name(monkeypatch):
    """DNS rebinding: one resolution; the TCP connection goes to the address checked, while SNI, the certificate
    check and the Host header use the (IDNA-encoded) host name. The path is percent-encoded."""
    import io
    import ssl

    answers = iter(["93.184.216.34", "127.0.0.1"])  # a rebinding resolver: public first, loopback next
    lookups: list = []

    def resolve(host, port, *a, **k):
        lookups.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), port))]

    class FakeSocket:
        def __init__(self):
            self.sent = b""

        def sendall(self, data):
            self.sent += data

        def makefile(self, mode, *a, **k):
            return io.BytesIO(b"HTTP/1.1 202 Accepted\r\nContent-Length: 2\r\n\r\nok")

        def close(self):
            pass

    fake = FakeSocket()
    connected: list = []
    wrapped: list = []

    def connect(address, timeout=None, *a, **k):
        connected.append(address)
        return "raw"

    def wrap(self, sock, server_hostname=None, **k):
        wrapped.append((sock, server_hostname))
        return fake

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", wrap)
    reply = outbound.post(
        "https://bücher.example.test:8443/in/(x)/ä?sig=a b", b"{}", {"Content-Type": "application/json"}
    )
    assert reply.status == 202 and reply.body == b"ok"
    assert lookups == ["xn--bcher-kva.example.test"]
    assert connected == [("93.184.216.34", 8443)]
    assert wrapped == [("raw", "xn--bcher-kva.example.test")]
    request = fake.sent.decode("ascii")
    assert request.startswith("POST /in/(x)/%C3%A4?sig=a%20b HTTP/1.1\r\n")
    assert "\r\nHost: xn--bcher-kva.example.test:8443\r\n" in request


def test_markdown_links_survive_parentheses():
    from notify import render

    block = render.card_content("h", [{"title": "t", "url": "https://sw.test/a_(b) c"}])["card"]["body"][1]
    assert block["items"][0]["text"] == "[t](https://sw.test/a_%28b%29%20c)"


def test_card_text_cannot_add_links_or_emphasis():
    # A record's name with a bare address or Markdown emphasis shows as text, not a link or bold.
    from notify import render

    item: render.Item = {
        "title": "**Pay** at https://evil.example/x or www.evil.example",
        "detail": "see _evil.example_",
    }
    body = render.card_content("h", [item])["card"]["body"][1]["items"]
    shown = body[0]["text"] + body[1]["text"]
    for marker in ("*", "_", "://", "evil.example", "www."):
        assert marker not in shown, marker
    assert "evil․example" in shown and "Pay" in shown


# ---- Channels review nits (rules package) ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["https://localhost./x", "https://foo.local./x", "https://intranet./x"])
def test_a_trailing_dot_doesnt_get_past_the_name_checks(url):
    with pytest.raises(outbound.UnsafeDestination):
        outbound.check_url(url)
    assert outbound.check_url("https://hooks.example.test./x").hostname == "hooks.example.test."


def test_deprecated_site_local_ipv6_is_refused():
    import ipaddress

    assert not outbound.public_ip(ipaddress.ip_address("fec0::1"))
    assert not outbound.public_ip(ipaddress.ip_address("feff:ffff::1"))
    with pytest.raises(outbound.UnsafeDestination):
        outbound.check_url("https://[fec0::1]/x")


def test_scrub_variants_cover_the_decoded_url_and_long_query_values():
    url = "https://hooks.example.test/p%20ath?sig=AbCdEfGhIjKlMnOp%2Fqrs&v=1"
    variants = deliveries._variants(url)
    assert "https://hooks.example.test/p ath?sig=AbCdEfGhIjKlMnOp/qrs&v=1" in variants  # percent-decoded
    assert "AbCdEfGhIjKlMnOp/qrs" in variants and "AbCdEfGhIjKlMnOp%2Fqrs" in variants  # the value alone
    assert "1" not in variants  # short values aren't secrets on their own
    text = "server said: bad signature AbCdEfGhIjKlMnOp/qrs for /p ath"
    assert "AbCdEfGhIjKlMnOp" not in deliveries.scrub(text, [url])


def test_tls_errors_and_5xx_greetings_are_permanent_with_the_same_words():
    import smtplib
    import ssl

    from notify.channels import Outcome

    permanent = [
        ssl.SSLCertVerificationError("certificate verify failed"),
        ssl.SSLError("wrong version number"),
        smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server."),
        smtplib.SMTPConnectError(554, b"No service"),
    ]
    retried = [ConnectionRefusedError(), smtplib.SMTPServerDisconnected(), ssl.SSLEOFError(), TimeoutError()]
    for error in permanent:
        failure = channels.smtp_open_failure(error)
        assert failure.outcome == Outcome.PERMANENT and str(failure) == channels.SMTP_NO_CONNECTION, error
    for error in retried:
        failure = channels.smtp_open_failure(error)
        assert failure.outcome == Outcome.RETRY and str(failure) == channels.SMTP_NO_CONNECTION, error
    assert channels.smtp_open_failure(smtplib.SMTPConnectError(421, b"busy")).outcome == Outcome.RETRY
