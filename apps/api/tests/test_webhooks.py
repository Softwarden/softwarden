"""Webhooks (notify/webhooks.py): endpoints with event subscriptions,
emission after commit, signed delivery through the delivery engine, retries with backoff, the attempt log,
410 and auto-disable, secret rotation with dual signatures, the SSRF guard, ping, resend, roles, tokens and tenancy.

The receiver is a real HTTP server on 127.0.0.1 in a thread. Webhook addresses must be https on a public host
(core/outbound.py), so the configured address stays public (`https://hooks.example.test/...`) and the test transport
(`channels.post_https`, the hook the delivery tests use) first runs the same address check, then forwards the exact body and
headers over a socket to the local server and returns its answer. The SSRF tests use the real transport with a fake
DNS answer, so nothing leaves the machine."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import socket
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from django.db import transaction
from django.utils import timezone

from catalog import services
from core import graph, outbound, worker
from core.dates import org_today
from core.models import AuditEvent, Job
from money import renewals
from notify import channels, deliveries, reminders, webhooks
from notify import services as reminder_services
from notify.models import Delivery, DeliveryAttempt, DeliveryStatus, Reminder, WebhookDestination, WebhookEvent
from tests.conftest import drain_jobs, no_digest, snapshot_problems

URL = "https://hooks.example.test/softwarden/in?token=RECEIVER-BEARER-0001"
SECRET = "webhook-signing-secret-0123456789"
NEW_SECRET = "webhook-rotated-secret-9876543210"
SECRETS = (URL, "RECEIVER-BEARER-0001", SECRET, NEW_SECRET)


# ---- the documented verification (docs/API.md "Verifying a webhook"), as a receiver would write it --------------


def verify(secret: str, header: str, body: bytes, now: float | None = None, tolerance: int = 300) -> bool:
    """True when one v1= signature in `header` is HMAC-SHA256("<t>." + body) under `secret` and t is within
    `tolerance` seconds of now (the replay window). Only ASCII digits make a timestamp and only 64 hex digits a
    signature."""
    pairs = [part.split("=", 1) for part in header.split(",") if "=" in part]
    stamps = [value for key, value in pairs if key == "t"]
    if len(stamps) != 1 or not (stamps[0].isascii() and stamps[0].isdigit()):
        return False
    stamp = int(stamps[0])
    if abs((time.time() if now is None else now) - stamp) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    given = [v for k, v in pairs if k == "v1" and len(v) == 64 and all(c in "0123456789abcdef" for c in v)]
    return any(hmac.compare_digest(expected, value) for value in given)


# ---- the local receiver ------------------------------------------------------------------------------------------


class Receiver:
    """Requests received (path, headers, body) and the answers to give: (status, headers, body), the last repeated."""

    def __init__(self):
        self.requests: list[dict[str, Any]] = []
        self.answers: list[tuple[int, dict[str, str], bytes]] = [(200, {}, b"ok")]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _handler(self):
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 (http.server's name)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                receiver.requests.append({"path": self.path, "headers": dict(self.headers.items()), "body": body})
                status, headers, answer = receiver.answers.pop(0) if len(receiver.answers) > 1 else receiver.answers[0]
                if callable(answer):
                    answer = answer(self.headers, body)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args):
                pass

        return Handler

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def post(self, url, body, headers, timeout=15):
        """channels.post_https for tests: the real address check, then the exact request to the local server."""
        parts = outbound.check_url(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request("POST", path, body=body, headers=dict(headers))
            reply = conn.getresponse()
            return graph.Reply(reply.status, {k.lower(): v for k, v in reply.getheaders()}, reply.read())
        finally:
            conn.close()

    def json(self, i: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[i]["body"])


@pytest.fixture
def receiver(monkeypatch):
    found = Receiver()
    found.thread.start()
    monkeypatch.setattr(channels, "post_https", found.post)
    try:
        yield found
    finally:
        found.server.shutdown()
        found.server.server_close()
        found.thread.join(5)


@pytest.fixture
def committed(django_capture_on_commit_callbacks):
    """A block whose commit hooks run as if it committed (reminder regeneration still uses them), followed by a drain
    of the webhook outbox, as the worker's next tick does (`webhooks.outbox`). The test's transaction never commits;
    the outbox doesn't need it to."""

    @contextmanager
    def block():
        with django_capture_on_commit_callbacks(execute=True):
            yield
        webhooks.drain()

    return block


@pytest.fixture
def steady(monkeypatch):
    """Retries without jitter, so the schedule can be asserted exactly."""
    monkeypatch.setattr(deliveries, "jitter", lambda: 1.0)


def endpoint(f, api, events=("record.*",), name="Receiver", **extra) -> dict[str, Any]:
    body = {"name": name, "url": URL, "signing_secret": SECRET, "events": list(events), **extra}
    response = api(f["users"]["admin"]).post("webhooks", body)
    assert response.status_code == 201, response.json_body
    return response.json_body


def no_secret_in(text: str) -> bool:
    return not any(s in text for s in SECRETS)


def attempt(delivery_id, now=None) -> Delivery:
    """One attempt (deliveries.attempt) at a delivery that is due."""
    done = deliveries.attempt(delivery_id, now)
    assert done is not None
    return done


def due(delivery: Delivery):
    assert delivery.next_attempt_at is not None
    return delivery.next_attempt_at


def event_deliveries(org=None):
    qs = Delivery.objects.filter(channel="webhook", kind="event")
    return qs.filter(organization=org) if org is not None else qs


# ---- acceptance check ------------------------------------------------------------------------------------------------


def test_a_record_update_reaches_the_receiver_signed_within_one_tick(a, api, receiver):
    no_digest()
    hook = endpoint(a, api, events=["record.updated"])
    a["users"]["editor"].first_name, a["users"]["editor"].last_name = "Erin", "Editor"
    a["users"]["editor"].save(update_fields=["first_name", "last_name"])
    editor = api(a["users"]["editor"])
    etag = editor.get(f"applications/{a['app'].id}")["ETag"]
    response = editor.client.patch(
        f"/api/v1/applications/{a['app'].id}",
        data=json.dumps({"name": "Payroll Cloud"}),
        content_type="application/json",
        HTTP_IF_MATCH=etag,
    )
    assert response.status_code == 200
    # The event is in the outbox, written with the change; nothing is queued or posted yet (the request didn't wait).
    [recorded] = WebhookEvent.objects.all()
    assert recorded.event_type == "record.updated" and recorded.organization_id == a["org"].id
    assert not event_deliveries().exists() and receiver.requests == []

    worker.tick()  # one worker tick: the outbox is drained, then the delivery job runs

    [queued] = event_deliveries()
    assert not WebhookEvent.objects.exists() and queued.event_id == recorded.id
    [request] = receiver.requests
    headers, body = request["headers"], request["body"]
    assert request["path"] == "/softwarden/in?token=RECEIVER-BEARER-0001"
    assert verify(SECRET, headers["X-Softwarden-Signature"], body)
    assert not verify("some-other-secret-value", headers["X-Softwarden-Signature"], body)
    assert not verify(SECRET, headers["X-Softwarden-Signature"], body + b" ")
    assert headers["X-Softwarden-Event"] == "record.updated" and headers["X-Softwarden-Delivery"] == str(queued.id)
    assert headers["Content-Type"] == "application/json"
    envelope = json.loads(body)
    assert set(envelope) == {"id", "type", "occurred_at", "organization", "actor", "data"}
    assert envelope["type"] == "record.updated" and envelope["id"] == str(queued.event_id)
    assert envelope["occurred_at"].endswith("Z") and envelope["organization"] == {"id": str(a["org"].id)}
    editor_user = a["users"]["editor"]
    assert envelope["actor"] == {"id": editor_user.id, "name": "Erin Editor"}  # never the email address
    assert envelope["data"]["record"] == {
        "id": str(a["app"].id),
        "kind": "application",
        "name": "Payroll Cloud",
        "lifecycle": "active",
        "archived": False,
        "path": f"/applications/{a['app'].id}",
    }
    assert envelope["data"]["changes"] == {"name": {"before": "orga App", "after": "Payroll Cloud"}}

    log = api(a["users"]["admin"]).get(f"webhook-deliveries/{queued.id}").json_body
    assert log["status"] == "sent" and log["attempts"] == 1 and log["last_status_code"] == 200
    assert log["webhook"] == {"id": hook["id"], "name": "Receiver"} and log["event_type"] == "record.updated"
    assert log["payload"] == envelope
    [row] = log["attempt_log"]
    assert row["outcome"] == "sent" and row["status_code"] == 200 and row["response_excerpt"] == "ok"
    assert row["duration_ms"] is not None and row["next_attempt_at"] is None
    summary = api(a["users"]["admin"]).get(f"webhooks/{hook['id']}").json_body
    assert summary["last_delivery"]["outcome"] == "sent" and summary["last_delivery"]["status_code"] == 200
    assert summary["consecutive_failures"] == 0


def test_a_failing_endpoint_is_retried_with_backoff_and_every_attempt_is_logged(a, api, receiver, steady):
    hook = endpoint(a, api)
    receiver.answers = [(500, {}, b"internal error")]
    webhooks.deliver(a["org"].pk, [webhooks.make_event("record.updated", {"record": {"id": "x"}})])
    [delivery] = event_deliveries()
    assert delivery.max_attempts == 6
    now = timezone.now()
    waits = []
    for _ in range(6):
        done = attempt(delivery.id, now)
        if done.status == DeliveryStatus.QUEUED:
            waits.append(due(done) - now)
            # The retry is a job due at that time, not before.
            assert Job.objects.filter(
                kind=deliveries.JOB_KIND, payload={"delivery_id": str(delivery.id)}, run_after=due(done)
            ).exists()
            assert deliveries.claim(delivery.id, due(done) - timedelta(seconds=1)) is None
            now = due(done)
    assert waits == [
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=30),
        timedelta(hours=2),
        timedelta(hours=6),
    ]
    assert done.status == DeliveryStatus.FAILED and "Gave up after 6 attempts" in done.last_error
    assert len(receiver.requests) == 6
    # Every attempt is a new delivery attempt of the same delivery: the same id, signed afresh.
    assert {r["headers"]["X-Softwarden-Delivery"] for r in receiver.requests} == {str(delivery.id)}

    log = api(a["users"]["admin"]).get(f"webhook-deliveries/{delivery.id}").json_body
    assert [r["number"] for r in log["attempt_log"]] == [1, 2, 3, 4, 5, 6]
    assert {r["status_code"] for r in log["attempt_log"]} == {500}
    assert [r["outcome"] for r in log["attempt_log"]] == ["retrying"] * 5 + ["failed"]
    assert all(r["response_excerpt"] == "internal error" for r in log["attempt_log"])
    nexts = [r["next_attempt_at"] for r in log["attempt_log"]]
    assert all(n is not None for n in nexts[:5]) and nexts[5] is None
    endpoint_now = WebhookDestination.objects.get(id=hook["id"])
    assert endpoint_now.consecutive_failures == 1 and endpoint_now.enabled
    assert endpoint_now.last_delivery_outcome == "failed" and endpoint_now.last_delivery_status_code == 500


def test_retries_are_jittered_within_ten_percent(a, api):
    hook = WebhookDestination.objects.get(id=endpoint(a, api)["id"])
    delivery = Delivery(organization=a["org"], channel="webhook", webhook_destination=hook, attempts=3)
    for _ in range(50):
        wait = deliveries.retry_delay(delivery, None)
        assert timedelta(minutes=27) <= wait <= timedelta(minutes=33)
    delivery.attempts = 9  # past the schedule: 6 hours each
    assert timedelta(hours=5.4) <= deliveries.retry_delay(delivery, None) <= timedelta(hours=6.6)
    assert deliveries.retry_delay(delivery, 30) == timedelta(seconds=30)  # Retry-After wins


@pytest.mark.parametrize(
    "answer",
    [
        (503, {"retry-after": "120"}, b""),
        (429, {}, b""),
        (404, {}, b"no such route"),
        graph.Unreachable("TimeoutError", ambiguous=True),
        graph.Unreachable("ConnectionRefusedError", ambiguous=False),
    ],
)
def test_timeouts_and_other_answers_are_retried(a, api, receiver, monkeypatch, steady, answer):
    endpoint(a, api)
    if isinstance(answer, BaseException):

        def fail(*args, **kwargs):
            raise answer

        monkeypatch.setattr(channels, "post_https", fail)
    else:
        receiver.answers = [answer]
    webhooks.deliver(a["org"].pk, [webhooks.make_event("record.updated", {})])
    now = timezone.now()
    done = attempt(event_deliveries().get().id, now)
    assert done.status == DeliveryStatus.QUEUED
    expected = timedelta(seconds=120) if not isinstance(answer, BaseException) and answer[0] == 503 else None
    assert due(done) - now == (expected or timedelta(minutes=1))


def test_a_410_turns_the_endpoint_off_and_tells_the_admins(a, api, receiver, committed):
    hook = endpoint(a, api)
    receiver.answers = [(410, {}, b"gone")]
    webhooks.deliver(a["org"].pk, [webhooks.make_event("record.created", {})])
    done = attempt(event_deliveries().get().id)
    assert done.status == DeliveryStatus.FAILED and "410" in done.last_error and done.attempts == 1
    row = WebhookDestination.objects.get(id=hook["id"])
    assert not row.enabled and row.disabled_at is not None and "410 Gone" in row.disabled_reason
    event = AuditEvent.objects.get(action="webhook_destination.disabled")
    assert event.actor is None and event.target_id == row.id and snapshot_problems(event) == []
    me = api(a["users"]["admin"]).get("auth/me").json_body["webhook_problem"]
    assert me["disabled"] == 1 and me["endpoints"][0]["id"] == hook["id"] and "410" in me["endpoints"][0]["reason"]
    assert api(a["users"]["editor"]).get("auth/me").json_body["webhook_problem"] is None
    # Turned off: new events aren't queued for it.
    with committed():
        services.archive_resource(a["org"], a["users"]["editor"], a["app"])
    assert event_deliveries().count() == 1
    # Turning it back on clears the reason and the count.
    admin = api(a["users"]["admin"])
    etag = admin.get(f"webhooks/{hook['id']}")["ETag"]
    on = admin.client.patch(
        f"/api/v1/webhooks/{hook['id']}", json.dumps({"enabled": True}), "application/json", HTTP_IF_MATCH=etag
    ).json()
    assert on["enabled"] and on["disabled_at"] is None and on["disabled_reason"] == ""
    assert admin.get("auth/me").json_body["webhook_problem"] is None


def test_an_endpoint_failing_n_deliveries_in_a_row_is_turned_off(a, api, receiver, settings):
    settings.SOFTWARDEN_WEBHOOK_MAX_ATTEMPTS = 1
    settings.SOFTWARDEN_WEBHOOK_DISABLE_AFTER = 3
    hook = endpoint(a, api)

    def one(status):
        receiver.answers = [(status, {}, b"")]
        webhooks.deliver(a["org"].pk, [webhooks.make_event("record.updated", {})])
        latest = event_deliveries().order_by("-created_at").first()
        return attempt(latest.id)

    one(500)
    one(500)
    one(200)  # a success resets the count
    assert WebhookDestination.objects.get(id=hook["id"]).consecutive_failures == 0
    for _ in range(3):
        one(502)
    row = WebhookDestination.objects.get(id=hook["id"])
    assert not row.enabled and row.consecutive_failures == 3 and "last 3 deliveries failed" in row.disabled_reason
    assert AuditEvent.objects.filter(action="webhook_destination.disabled").count() == 1
    # A ping that fails doesn't count, nor turn anything off.
    settings.SOFTWARDEN_WEBHOOK_DISABLE_AFTER = 1
    other = WebhookDestination.objects.get(id=endpoint(a, api, name="Other")["id"])
    receiver.answers = [(500, {}, b"")]
    pinged = api(a["users"]["admin"]).post(f"webhooks/{other.id}/ping").json_body
    assert pinged["ok"] is False
    other.refresh_from_db()
    assert other.enabled and other.consecutive_failures == 0 and other.last_delivery_outcome == "failed"


# ---- signing and rotation ----------------------------------------------------------------------------------------


def test_rotation_signs_with_both_secrets_during_the_grace_period(a, api, receiver):
    hook = endpoint(a, api)
    admin = api(a["users"]["admin"])
    etag = admin.get(f"webhooks/{hook['id']}")["ETag"]
    stale = admin.client.post(
        f"/api/v1/webhooks/{hook['id']}/rotate-secret",
        json.dumps({"signing_secret": NEW_SECRET}),
        "application/json",
        HTTP_IF_MATCH='"0000"',
    )
    assert stale.status_code == 412
    rotated = admin.client.post(
        f"/api/v1/webhooks/{hook['id']}/rotate-secret",
        json.dumps({"signing_secret": NEW_SECRET, "grace_period_hours": 2}),
        "application/json",
        HTTP_IF_MATCH=etag,
    )
    assert rotated.status_code == 200 and rotated["ETag"] != etag and no_secret_in(rotated.content.decode())
    body = rotated.json()
    assert body["previous_secret_expires_at"].endswith("Z")
    assert admin.post(f"webhooks/{hook['id']}/ping").json_body["ok"] is True
    header = receiver.requests[-1]["headers"]["X-Softwarden-Signature"]
    assert header.count("v1=") == 2
    payload = receiver.requests[-1]["body"]
    assert verify(NEW_SECRET, header, payload) and verify(SECRET, header, payload)
    assert header.split(",")[1] == channels.signature(NEW_SECRET, int(header.split(",")[0][2:]), payload).split(",")[1]
    # After the grace period only the new secret signs.
    WebhookDestination.objects.filter(id=hook["id"]).update(previous_secret_expires_at=timezone.now())
    admin.post(f"webhooks/{hook['id']}/ping")
    header = receiver.requests[-1]["headers"]["X-Softwarden-Signature"]
    assert header.count("v1=") == 1 and verify(NEW_SECRET, header, receiver.requests[-1]["body"])
    assert not verify(SECRET, header, receiver.requests[-1]["body"])
    # No grace: the old secret stops at once; replacing the secret with PATCH ends a grace period too.
    etag = admin.get(f"webhooks/{hook['id']}")["ETag"]
    admin.client.post(
        f"/api/v1/webhooks/{hook['id']}/rotate-secret",
        json.dumps({"signing_secret": SECRET, "grace_period_hours": 0}),
        "application/json",
        HTTP_IF_MATCH=etag,
    )
    row = WebhookDestination.objects.get(id=hook["id"])
    assert row.previous_secret_expires_at is None and row.previous_signing_secret_ciphertext == ""
    event = AuditEvent.objects.filter(action="webhook_destination.secret_rotated").first()
    assert event is not None and no_secret_in(json.dumps([event.changes, event.prechange, event.postchange]))
    refused = admin.post(f"webhooks/{hook['id']}/rotate-secret", {"signing_secret": "short", "grace_period_hours": 999})
    assert refused.status_code == 400 and {"signing_secret", "grace_period_hours"} <= set(refused.json_body["errors"])


def test_the_replay_window_refuses_an_old_signature():
    body = b'{"id":"x"}'
    old = int(time.time()) - 600
    header = channels.signature(SECRET, old, body)
    assert verify(SECRET, header, body, now=old + 10)
    assert not verify(SECRET, header, body)  # ten minutes later: outside the five-minute window


# ---- the SSRF guard ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.test/in",
        "https://10.0.0.5/in",
        "https://169.254.169.254/latest/meta-data",
        "https://[fd00:ec2::254]/",
        "https://localhost/in",
        "https://user:pass@hooks.example.test/in",
    ],
)
def test_unsafe_addresses_are_refused_on_save(a, api, url):
    response = api(a["users"]["admin"]).post(
        "webhooks", {"name": "Bad", "url": url, "signing_secret": SECRET, "events": ["record.*"]}
    )
    assert response.status_code == 400 and "url" in response.json_body["errors"]
    assert url not in response.content.decode()
    assert not WebhookDestination.objects.exists()


@pytest.mark.parametrize("resolved", ["10.0.0.5", "169.254.169.254", "127.0.0.1"])
def test_a_name_resolving_to_a_private_or_metadata_address_is_refused_at_send(a, api, monkeypatch, resolved):
    endpoint(a, api)  # the real transport: channels.post_https is core/outbound.post here
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolved, port))],
    )
    connected: list = []
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: connected.append(args))
    webhooks.deliver(a["org"].pk, [webhooks.make_event("record.updated", {})])
    done = attempt(event_deliveries().get().id)
    assert done.status == DeliveryStatus.FAILED and done.attempts == 1 and "isn't allowed" in done.last_error
    assert connected == [] and no_secret_in(done.last_error)


def test_a_redirect_is_never_followed(a, api, receiver):
    endpoint(a, api)
    receiver.answers = [(302, {"Location": "https://169.254.169.254/latest/meta-data"}, b"")]
    webhooks.deliver(a["org"].pk, [webhooks.make_event("record.updated", {})])
    done = attempt(event_deliveries().get().id)
    assert done.status == DeliveryStatus.FAILED and "redirect" in done.last_error and len(receiver.requests) == 1
    assert DeliveryAttempt.objects.get(delivery=done).status_code == 302


# ---- events and subscriptions ------------------------------------------------------------------------------------


def test_only_enabled_endpoints_subscribed_to_the_type_get_a_delivery(a, api, receiver, committed):
    records = endpoint(a, api, events=["record.*"], name="Records")
    links = endpoint(a, api, events=["relationship.created"], name="Links")
    off = endpoint(a, api, events=["record.*", "relationship.*"], name="Off", enabled=False)
    nothing = endpoint(a, api, events=[], name="Nothing")
    editor, org = a["users"]["editor"], a["org"]
    other = services.create_resource(org, editor, {"name": "Ledger", "lifecycle": "active"})
    webhooks.drain()  # the write above is posted first, then its delivery forgotten
    Delivery.objects.filter(channel="webhook").delete()
    with committed():
        services.archive_resource(org, editor, a["app"])
        services.create_relationship(
            org, editor, {"type": "depends_on", "source": str(a["app"].id), "target": str(other.id)}
        )
    by_endpoint = {str(d.webhook_destination_id): d.event_type for d in event_deliveries().order_by("created_at")}
    assert by_endpoint == {records["id"]: "record.archived", links["id"]: "relationship.created"}
    assert off["id"] not in by_endpoint and nothing["id"] not in by_endpoint
    # One event, two endpoints: one event id, two delivery ids.
    endpoint(a, api, events=["record.archived"], name="Also")
    with committed():
        services.restore_resource(org, editor, a["app"])
        services.archive_resource(org, editor, a["app"])
    copies = event_deliveries().filter(event_type="record.archived").order_by("created_at")[1:]
    assert len(copies) == 2 and copies[0].event_id == copies[1].event_id and copies[0].id != copies[1].id


def test_a_write_that_rolls_back_sends_nothing_and_a_failure_to_queue_never_fails_the_write(
    a, api, receiver, monkeypatch, committed
):
    endpoint(a, api)
    editor = a["users"]["editor"]
    with committed():
        with pytest.raises(RuntimeError), transaction.atomic():
            services.update_resource(a["org"], editor, a["app"], {"name": "Never saved"})
            assert WebhookEvent.objects.count() == 1  # recorded with the write...
            raise RuntimeError("rolled back")
    assert not WebhookEvent.objects.exists()  # ...and rolled back with it
    assert not event_deliveries().exists()

    def broken(*args, **kwargs):
        raise RuntimeError("the queue is broken")

    # The drain can't queue deliveries: the write is saved anyway, and the event is dropped with a log line.
    monkeypatch.setattr(deliveries, "enqueue", broken)
    monkeypatch.setattr(Delivery.objects, "bulk_create", broken)
    etag = api(editor).get(f"applications/{a['app'].id}")["ETag"]
    with committed():
        response = api(editor).client.patch(
            f"/api/v1/applications/{a['app'].id}",
            json.dumps({"name": "Saved anyway"}),
            "application/json",
            HTTP_IF_MATCH=etag,
        )
    assert response.status_code == 200 and response.json()["name"] == "Saved anyway"
    assert not event_deliveries().exists()
    # An event that can't be built for the outbox: the write is saved anyway, and nothing is recorded.
    monkeypatch.setattr(webhooks, "fitted", broken)
    etag = api(editor).get(f"applications/{a['app'].id}")["ETag"]
    response = api(editor).client.patch(
        f"/api/v1/applications/{a['app'].id}", json.dumps({"name": "Again"}), "application/json", HTTP_IF_MATCH=etag
    )
    assert response.status_code == 200 and not WebhookEvent.objects.exists()


def test_an_edit_that_changes_nothing_is_no_event(a, api, committed):
    endpoint(a, api)
    with committed():
        services.update_resource(a["org"], a["users"]["editor"], a["app"], {"name": a["app"].name})
    assert not event_deliveries().exists()


def test_event_payloads_for_every_family(a, api, committed):
    from tests.test_discovery import make_connector

    endpoint(a, api, events=list(webhooks.WILDCARDS))
    org, editor = a["org"], a["users"]["editor"]
    today = org_today(org)
    with committed():
        made = services.create_resource(org, editor, {"name": "Ledger", "lifecycle": "active"})
        rel = services.create_relationship(
            org, editor, {"type": "depends_on", "source": str(a["app"].id), "target": str(made.id)}
        )
        services.update_relationship(org, editor, rel, {"purpose": "Payments"})
        services.delete_relationship(org, editor, rel)
        services.delete_resource(org, a["users"]["admin"], made)
        renewal = renewals.create_renewal(
            org,
            editor,
            {"name": "Domain", "renewal_date": (today + timedelta(days=20)).isoformat(), "auto_renew": "no"},
        )
        renewals.record_decision(org, editor, renewal, None, {"decision": "renew"})
    with committed():
        reminders.generate(org, today)
    reminder = Reminder.objects.filter(organization=org, status="open").first()
    assert reminder is not None
    with committed():
        reminder_services.snooze_reminder(org, editor, reminder, {"until": (today + timedelta(days=5)).isoformat()})
    connector = make_connector(org, a["users"]["admin"])
    from discovery import engine as discovery

    with committed():
        discovery.run(connector)

    rows = {d.event_type: d.content for d in event_deliveries().order_by("created_at")}
    assert {
        "record.created",
        "record.deleted",
        "relationship.created",
        "relationship.updated",
        "relationship.deleted",
        "renewal.created",
        "renewal.decided",
        "reminder.created",
        "reminder.snoozed",
        "discovery.candidate",
    } <= set(rows)
    assert rows["record.created"]["data"]["record"]["name"] == "Ledger"
    assert rows["record.deleted"]["data"]["record"]["path"] is None
    rel_data = rows["relationship.updated"]["data"]
    assert rel_data["relationship"]["type"] == "depends_on"
    assert rel_data["relationship"]["source"] == {"id": str(a["app"].id), "kind": "application", "name": "orga App"}
    assert rel_data["changes"] == {"purpose": {"before": "", "after": "Payments"}}
    decided = rows["renewal.decided"]["data"]
    assert decided["renewal"]["name"] == "Domain" and decided["cycle"]["decision"] == "renew"
    assert decided["renewal"]["path"] == f"/renewals/{renewal.id}"
    snoozed = rows["reminder.snoozed"]["data"]["reminder"]
    assert snoozed["id"] == str(reminder.id) and snoozed["status"] == "snoozed" and snoozed["snoozed_until"]
    candidate = rows["discovery.candidate"]["data"]["observation"]
    assert candidate["connector"] == {"id": str(connector.id), "name": connector.name, "source": "entra"}
    assert candidate["path"].startswith("/discovery/observations/") and rows["discovery.candidate"]["actor"] == "system"
    assert rows["reminder.created"]["actor"] == "system"
    for content in rows.values():
        assert set(content) == {"event", "id", "occurred_at", "actor", "data"}


def test_payloads_leave_out_anything_named_like_a_secret():
    found = webhooks.changes_json(
        {
            "name": ["A", "B"],
            "api_key": ["one", "two"],
            "client_secret": ["x", "y"],
            "webhook_url": ["https://a", "https://b"],
            "token_hash": ["h1", "h2"],
            "undo": True,
        }
    )
    assert found["name"] == {"before": "A", "after": "B"} and "undo" not in found
    assert set(found) == {"name"}  # redacted to "[redacted]" by key name, so no before/after pair is left
    assert "one" not in json.dumps(found) and "https://a" not in json.dumps(found) and "h1" not in json.dumps(found)


def test_subscriptions_are_validated(a, api):
    admin = api(a["users"]["admin"])
    bad = admin.post(
        "webhooks", {"name": "Bad", "url": URL, "signing_secret": SECRET, "events": ["record.updated", "contract.*"]}
    )
    assert bad.status_code == 400 and "contract.*" in bad.json_body["errors"]["events"][0]
    assert (
        admin.post("webhooks", {"name": "Bad", "url": URL, "signing_secret": SECRET, "events": "record.*"}).status_code
        == 400
    )
    assert (
        admin.post("webhooks", {"name": "Bad", "url": URL, "signing_secret": SECRET, "events": ["ping"]}).status_code
        == 400
    )
    made = endpoint(a, api, events=["reminder.created", "record.*", "record.*"])
    assert made["events"] == ["record.*", "reminder.created"]
    catalog = admin.get("webhooks/event-types").json_body
    assert [t["type"] for t in catalog["types"]] == list(webhooks.EVENT_TYPES)
    assert "record.*" in catalog["wildcards"] and all(t["description"] for t in catalog["types"])


# ---- ping, resend and the log ------------------------------------------------------------------------------------


def test_ping_sends_now_and_returns_the_answer(a, api, receiver):
    hook = endpoint(a, api)
    admin = api(a["users"]["admin"])
    receiver.answers = [(200, {}, b"pong")]
    body = admin.post(f"webhooks/{hook['id']}/ping").json_body
    assert body["ok"] is True and body["delivery"]["event_type"] == "ping" and body["delivery"]["kind"] == "test"
    assert body["delivery"]["attempt_log"][0]["response_excerpt"] == "pong"
    envelope = receiver.json()
    assert envelope["type"] == "ping" and envelope["data"]["webhook"] == {"id": hook["id"], "name": "Receiver"}
    assert verify(SECRET, receiver.requests[-1]["headers"]["X-Softwarden-Signature"], receiver.requests[-1]["body"])
    receiver.answers = [(500, {}, b"down")]
    failed = admin.post(f"webhooks/{hook['id']}/ping").json_body
    assert failed["ok"] is False and "500" in failed["detail"] and failed["delivery"]["attempts"] == 1
    assert failed["delivery"]["status"] == "failed"  # a ping is tried once, never retried
    shown = admin.get(f"webhooks/{hook['id']}").json_body
    assert shown["last_test_ok"] is False and shown["last_delivery"]["delivery"] == failed["delivery"]["id"]
    etag = admin.get(f"webhooks/{hook['id']}")["ETag"]
    admin.client.patch(
        f"/api/v1/webhooks/{hook['id']}", json.dumps({"enabled": False}), "application/json", HTTP_IF_MATCH=etag
    )
    assert admin.post(f"webhooks/{hook['id']}/ping").status_code == 409


def test_resend_keeps_the_event_id_and_gets_a_new_delivery_id(a, api, receiver):
    endpoint(a, api)
    receiver.answers = [(500, {}, b"")]
    webhooks.deliver(a["org"].pk, [webhooks.make_event("record.updated", {"record": {"id": "r"}})])
    original = event_deliveries().get()
    admin = api(a["users"]["admin"])
    assert admin.post(f"webhook-deliveries/{original.id}/resend").status_code == 409  # still queued
    Delivery.objects.filter(id=original.id).update(status="failed")
    response = admin.post(f"webhook-deliveries/{original.id}/resend")
    assert response.status_code == 202 and response.json_body["resend_of"] == str(original.id)
    copy = Delivery.objects.get(id=response.json_body["id"])
    assert copy.event_id == original.event_id and copy.max_attempts == 6 and copy.kind == "event"
    receiver.answers = [(200, {}, b"")]
    drain_jobs(deliveries.JOB_KIND)
    resent = [r for r in receiver.requests if r["headers"]["X-Softwarden-Delivery"] == str(copy.id)]
    assert len(resent) == 1 and json.loads(resent[0]["body"])["id"] == str(original.event_id)
    assert admin.get(f"webhook-deliveries/{original.id}").json_body["resends"] == [str(copy.id)]


def test_the_delivery_log_filters(a, api, receiver):
    hook = endpoint(a, api)
    second = endpoint(a, api, name="Second", events=["reminder.*"])
    webhooks.deliver(a["org"].pk, [webhooks.make_event("record.updated", {})])
    webhooks.deliver(a["org"].pk, [webhooks.make_event("reminder.snoozed", {})])
    old = event_deliveries().get(event_type="record.updated")
    Delivery.objects.filter(id=old.id).update(created_at=timezone.now() - timedelta(days=3))
    attempt(old.id)
    admin = api(a["users"]["admin"])

    def ids(query):
        response = admin.get(f"webhook-deliveries{query}")
        assert response.status_code == 200, response.json_body
        return {r["id"] for r in response.json_body["results"]}

    snoozed = str(event_deliveries().get(event_type="reminder.snoozed").id)
    assert ids("") == {str(old.id), snoozed}
    assert ids("?status=sent") == {str(old.id)}
    assert ids("?event=reminder.*") == {snoozed} == ids("?event=reminder.snoozed,record.archived")
    assert ids(f"?webhook={hook['id']}") == {str(old.id)} and ids(f"?webhook={second['id']}") == {snoozed}
    today = timezone.now().date()
    assert ids(f"?from={today - timedelta(days=1)}") == {snoozed}
    assert ids(f"?to={today - timedelta(days=2)}") == {str(old.id)}
    assert ids("?kind=event") == {str(old.id), snoozed} and ids("?kind=test") == set()
    for bad in ("?status=lost", "?event=<script>", "?from=yesterday", "?webhook=nope"):
        assert admin.get(f"webhook-deliveries{bad}").status_code == 400, bad
    # Email deliveries aren't in the webhook log.
    deliveries.enqueue(
        a["org"], channel="email", dedupe_key="e1", subject="s", content={"text": "t"}, recipient="x@orga.test"
    )
    assert len(ids("")) == 2


def test_a_server_echoing_secrets_leaves_none_in_the_log(a, api, receiver):
    endpoint(a, api)

    def echo(headers, body):
        return f"{URL} {headers['X-Softwarden-Signature']} {SECRET}".encode()

    receiver.answers = [(500, {}, echo)]
    webhooks.deliver(a["org"].pk, [webhooks.make_event("record.updated", {})])
    done = attempt(event_deliveries().get().id)
    logged = DeliveryAttempt.objects.get(delivery=done)
    assert "[redacted]" in logged.response_excerpt and no_secret_in(logged.response_excerpt)
    assert "RECEIVER-BEARER" not in logged.response_excerpt
    detail = api(a["users"]["admin"]).get(f"webhook-deliveries/{done.id}")
    assert no_secret_in(detail.content.decode())


def test_the_etag_ignores_the_delivery_summary(a, api, receiver):
    hook = endpoint(a, api)
    admin = api(a["users"]["admin"])
    before = admin.get(f"webhooks/{hook['id']}")["ETag"]
    webhooks.deliver(a["org"].pk, [webhooks.make_event("record.updated", {})])
    attempt(event_deliveries().get().id)
    assert admin.get(f"webhooks/{hook['id']}")["ETag"] == before


# ---- roles, tokens, tenancy, secrets -----------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_webhooks_are_admin_only(a, api, role):
    hook = endpoint(a, api)
    client = api(a["users"][role])
    for path in ("webhooks", f"webhooks/{hook['id']}", "webhooks/event-types", "webhook-deliveries"):
        assert client.get(path).status_code == 403, path
    assert client.post("webhooks", {"name": "x"}).status_code == 403
    assert client.post(f"webhooks/{hook['id']}/ping").status_code == 403
    assert client.post(f"webhooks/{hook['id']}/rotate-secret", {"signing_secret": NEW_SECRET}).status_code == 403


def test_an_admin_token_manages_webhooks_and_a_read_only_token_only_reads(a, receiver):
    from tests.test_api_lifecycle import TokenClient

    writer, reader = TokenClient(a, "admin"), TokenClient(a, "admin", read_only=True)
    made = writer.call("post", "webhooks", {"name": "Token", "url": URL, "signing_secret": SECRET, "events": []})
    assert made.status_code == 201
    pk = made.body["id"]
    assert writer.call("post", f"webhooks/{pk}/ping").status_code == 200
    assert reader.call("get", f"webhooks/{pk}").status_code == 200
    assert reader.call("get", "webhook-deliveries").status_code == 200
    for path, body in ((f"webhooks/{pk}/ping", None), (f"webhooks/{pk}/rotate-secret", {"signing_secret": NEW_SECRET})):
        refused = reader.call("post", path, body)
        assert refused.status_code == 403 and refused.body["type"] == "/problems/read-only-token"
    assert writer.call("post", f"webhooks/{pk}/rotate-secret", {"signing_secret": NEW_SECRET}).status_code == 200
    assert TokenClient(a, "editor").call("get", "webhooks").status_code == 403


def test_another_organizations_webhooks_and_events_stay_apart(a, b, api, receiver, committed):
    theirs = endpoint(b, api, name="Theirs")
    ours = endpoint(a, api, name="Ours")
    with committed():
        services.archive_resource(a["org"], a["users"]["editor"], a["app"])
    [delivery] = event_deliveries()
    assert str(delivery.webhook_destination_id) == ours["id"] and delivery.organization_id == a["org"].id
    admin = api(a["users"]["admin"])
    assert [w["id"] for w in admin.get("webhooks").json_body] == [ours["id"]]
    assert admin.get(f"webhooks/{theirs['id']}").status_code == 404
    assert admin.post(f"webhooks/{theirs['id']}/ping").status_code == 404
    assert admin.post(f"webhooks/{theirs['id']}/rotate-secret", {"signing_secret": NEW_SECRET}).status_code == 404
    their_admin = api(b["users"]["admin"])
    assert their_admin.get("webhook-deliveries").json_body["results"] == []
    assert their_admin.get(f"webhook-deliveries/{delivery.id}").status_code == 404
    assert their_admin.post(f"webhook-deliveries/{delivery.id}/resend").status_code == 404
    assert their_admin.get(f"webhook-deliveries?webhook={ours['id']}").json_body["results"] == []


def test_the_database_refuses_an_attempt_of_another_organizations_delivery(a, b):
    from django.db import IntegrityError, connection

    delivery, _ = deliveries.enqueue(
        b["org"], channel="email", dedupe_key="b", subject="s", content={}, recipient="x@orgb.test"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        DeliveryAttempt.objects.create(
            organization=a["org"], delivery=delivery, number=1, started_at=timezone.now(), outcome="sent"
        )
        connection.check_constraints()


def test_secrets_never_reach_a_response_an_event_a_job_or_the_log(a, api, receiver, caplog):
    caplog.set_level("DEBUG")
    hook = endpoint(a, api)
    admin = api(a["users"]["admin"])
    responses = [admin.get("webhooks"), admin.get(f"webhooks/{hook['id']}")]
    etag = responses[-1]["ETag"]
    rotated = admin.client.post(
        f"/api/v1/webhooks/{hook['id']}/rotate-secret",
        json.dumps({"signing_secret": NEW_SECRET}),
        "application/json",
        HTTP_IF_MATCH=etag,
    )
    responses += [rotated, admin.post(f"webhooks/{hook['id']}/ping")]
    receiver.answers = [(500, {}, URL.encode())]
    webhooks.deliver(a["org"].pk, [webhooks.make_event("record.updated", {})])
    drain_jobs(deliveries.JOB_KIND)
    responses += [admin.get("webhook-deliveries")]
    responses += [admin.get(f"webhook-deliveries/{d.id}") for d in Delivery.objects.all()]
    responses += [admin.get("notification-settings/webhook-destinations"), admin.get("auth/me")]
    for response in responses:
        assert response.status_code < 400, response.content[:200]
        assert no_secret_in(response.content.decode()) and no_secret_in(json.dumps(dict(response.items())))
    for job in Job.objects.all():
        assert no_secret_in(json.dumps(job.payload)) and set(job.payload) == {"delivery_id"}
    for event in AuditEvent.objects.all():
        assert no_secret_in(json.dumps([event.changes, event.prechange, event.postchange]))
        assert snapshot_problems(event) == []
    stored = json.dumps([list(Delivery.objects.values()), list(DeliveryAttempt.objects.values())], default=str)
    assert no_secret_in(stored) and no_secret_in(caplog.text)


def test_the_schema_documents_webhooks_and_keeps_secrets_write_only(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    paths, schemas = doc["paths"], doc["components"]["schemas"]
    assert {
        "/api/v1/webhooks",
        "/api/v1/webhooks/{id}",
        "/api/v1/webhooks/{id}/ping",
        "/api/v1/webhooks/{id}/rotate-secret",
        "/api/v1/webhooks/event-types",
        "/api/v1/webhook-deliveries",
        "/api/v1/webhook-deliveries/{id}",
        "/api/v1/webhook-deliveries/{id}/resend",
    } <= set(paths)
    for name, fields in (
        ("WebhookInputRequest", ("url", "signing_secret")),
        ("PatchedWebhookInputRequest", ("url", "signing_secret")),
        ("RotateSecretInputRequest", ("signing_secret",)),
    ):
        for field in fields:
            assert schemas[name]["properties"][field].get("writeOnly") is True, (name, field)
    for name in ("Webhook", "WebhookDelivery", "WebhookDeliveryDetail", "DeliveryAttempt"):
        assert not {"url", "signing_secret", "secret"} & set(schemas[name]["properties"]), name
    assert {"cursor", "status", "event", "webhook", "kind", "from", "to"} <= {
        p["name"] for p in paths["/api/v1/webhook-deliveries"]["get"]["parameters"]
    }
    assert "If-Match" in {p["name"] for p in paths["/api/v1/webhooks/{id}/rotate-secret"]["post"]["parameters"]}


def test_webhooks_are_a_community_capability(a):
    from core.capabilities import COMMUNITY, WEBHOOKS

    assert WEBHOOKS in COMMUNITY


def test_the_query_string_of_the_address_is_kept_for_sending_but_never_shown(a, api, receiver):
    hook = endpoint(a, api)
    assert hook["url_host"] == "hooks.example.test" and "token" not in json.dumps(hook)
    api(a["users"]["admin"]).post(f"webhooks/{hook['id']}/ping")
    assert urllib.parse.urlsplit(receiver.requests[0]["path"]).query == "token=RECEIVER-BEARER-0001"


# ---- review follow-ups: scale, robustness, counting, the sweep, retention -----------------------------------------


def _bulk_tag(a, ids):
    from catalog import bulk

    return bulk.apply(
        a["org"],
        a["users"]["editor"],
        {"kind": "application", "ids": [str(i) for i in ids], "changes": {"add_tags": [f"t{uuid.uuid4().hex[:6]}"]}},
    )


def _apps(a, n):
    return [
        services.create_resource(a["org"], a["users"]["editor"], {"name": f"Bulk {i}", "lifecycle": "active"}).id
        for i in range(n)
    ]


def _queries(work) -> int:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from core.request_context import unit_of_work

    with unit_of_work(), CaptureQueriesContext(connection) as seen:  # its own unit, as a request has
        work()
    return len(seen.captured_queries)


def test_a_bulk_edit_costs_one_insert_per_event_and_the_drain_a_fixed_number_of_queries(a, api, monkeypatch):
    ids = _apps(a, 20)
    webhooks.drain()
    real = webhooks.after_audit
    monkeypatch.setattr(webhooks, "after_audit", lambda *args, **kwargs: None)
    base = _queries(lambda: _bulk_tag(a, ids))
    monkeypatch.setattr(webhooks, "after_audit", real)
    unsubscribed = _queries(lambda: _bulk_tag(a, ids))
    assert unsubscribed - base <= 1, unsubscribed - base  # one cached subscription check for the unit of work
    assert not WebhookEvent.objects.exists()
    endpoint(a, api, events=["record.updated"])
    subscribed_cost = _queries(lambda: _bulk_tag(a, ids))
    assert subscribed_cost - base <= 21, subscribed_cost - base  # the subscription check, one outbox INSERT per event
    assert WebhookEvent.objects.count() == 20 and not event_deliveries().exists()
    drain_cost = _queries(webhooks.drain)
    # A savepoint, the lock, the batch, the endpoints, the organization, two bulk inserts in a savepoint, the delete,
    # then the empty second batch: the same whatever the number of events.
    assert drain_cost <= 16, drain_cost
    assert event_deliveries().count() == 20 and not WebhookEvent.objects.exists()
    assert Job.objects.filter(kind=deliveries.JOB_KIND).count() >= 20


def test_a_bad_event_is_skipped_and_an_oversize_one_is_cut(a, api, monkeypatch, caplog):
    endpoint(a, api)
    bad = webhooks.make_event("record.updated", {"record": {"id": "bad"}})
    good = webhooks.make_event("record.updated", {"record": {"id": "good"}})
    changes = {f"field_{i}": ["x" * 5000, "y"] for i in range(80)}
    huge = webhooks.make_event("record.updated", {"record": {"id": "huge"}, "changes": webhooks.changes_json(changes)})
    real = webhooks.fitted

    def fitted(event):
        if event.id == bad.id:
            raise ValueError("broken event")
        return real(event)

    monkeypatch.setattr(webhooks, "fitted", fitted)
    assert webhooks.deliver(a["org"].pk, [bad, good, huge]) == 2
    rows = {d.event_id: d for d in event_deliveries()}
    assert set(rows) == {good.id, huge.id}
    cut = rows[huge.id].content["data"]["changes"]
    assert cut["truncated"] is True and len(cut["fields"]) == 80 and "field_0" in cut["fields"]
    assert str(bad.id) in caplog.text and "ValueError" in caplog.text and "broken event" not in caplog.text


def test_an_inner_savepoint_that_rolls_back_drops_only_its_own_events(a, api, committed):
    endpoint(a, api)
    org, editor = a["org"], a["users"]["editor"]
    with committed():
        with transaction.atomic():
            services.archive_resource(org, editor, a["app"])
            with pytest.raises(RuntimeError), transaction.atomic():
                services.create_resource(org, editor, {"name": "Never", "lifecycle": "active"})
                raise RuntimeError("inner rollback")
    assert list(event_deliveries().values_list("event_type", flat=True)) == ["record.archived"]


def test_without_the_capability_the_routes_refuse_and_nothing_is_queued(a, api, monkeypatch, committed):
    from core import capabilities
    from core.models import Edition

    hook = endpoint(a, api)
    monkeypatch.setitem(
        capabilities.EDITION_CAPABILITIES, Edition.COMMUNITY, capabilities.COMMUNITY - {capabilities.WEBHOOKS}
    )
    admin = api(a["users"]["admin"])
    for path in ("webhooks", f"webhooks/{hook['id']}", "webhooks/event-types", "webhook-deliveries"):
        refused = admin.get(path)
        assert refused.status_code == 403 and refused.json_body["type"] == "/problems/capability-required", path
    assert admin.post(f"webhooks/{hook['id']}/ping").status_code == 403
    with committed():
        services.archive_resource(a["org"], a["users"]["editor"], a["app"])
    assert not event_deliveries().exists()
    assert webhooks.deliver(a["org"].pk, [webhooks.make_event("record.updated", {})]) == 0


def _notice(a, hook_id):
    """A notification rule's notice to the webhook (kind reminder), as notify/rules.py queues it."""
    from notify import render

    return deliveries.enqueue(
        a["org"],
        channel="webhook",
        dedupe_key=f"r:notice:{uuid.uuid4().hex}",
        subject="Deadline",
        content=render.webhook_content("reminder.due", {"title": "Payroll"}),
        webhook_destination=WebhookDestination.objects.get(id=hook_id),
    )[0]


def _until_done(delivery_id, now):
    """Attempt a delivery at each of its retry times until it is no longer queued."""
    done = attempt(delivery_id, now)
    waits = []
    while done.status == DeliveryStatus.QUEUED:
        waits.append(due(done) - now)
        now = due(done)
        done = attempt(delivery_id, now)
    return done, waits


def test_only_event_deliveries_turn_an_endpoint_off(a, api, receiver, settings):
    settings.SOFTWARDEN_WEBHOOK_DISABLE_AFTER = 1
    hook = endpoint(a, api)
    receiver.answers = [(410, {}, b"gone")]
    done = attempt(_notice(a, hook["id"]).id)
    assert done.status == DeliveryStatus.FAILED and done.attempts == 1  # the notice itself fails
    row = WebhookDestination.objects.get(id=hook["id"])
    assert row.enabled and row.consecutive_failures == 0 and row.last_delivery_status_code == 410
    receiver.answers = [(500, {}, b"")]
    for _ in range(2):
        assert _until_done(_notice(a, hook["id"]).id, timezone.now())[0].status == DeliveryStatus.FAILED
    assert WebhookDestination.objects.get(id=hook["id"]).enabled
    assert not AuditEvent.objects.filter(action="webhook_destination.disabled").exists()


def test_rule_notices_keep_four_attempts_on_the_webhook_backoff(a, api, receiver, steady):
    hook = endpoint(a, api)
    receiver.answers = [(500, {}, b"")]
    notice = _notice(a, hook["id"])
    assert notice.max_attempts == deliveries.MAX_ATTEMPTS == 4 and notice.event_type == "reminder.due"
    done, waits = _until_done(notice.id, timezone.now())
    assert waits == [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30)]
    assert done.status == DeliveryStatus.FAILED and done.attempts == 4


def test_a_webhook_delivery_whose_worker_stopped_is_retried_and_logged(a, api, receiver, steady):
    hook = endpoint(a, api)
    webhooks.deliver(a["org"].pk, [webhooks.make_event("record.updated", {})])
    delivery = event_deliveries().get()
    assert deliveries.claim(delivery.id) is not None
    later = timezone.now() + deliveries.LEASE + timedelta(seconds=1)
    assert deliveries.sweep(later)["deliveries_expired"] == 1
    row = Delivery.objects.get(id=delivery.id)
    assert row.status == DeliveryStatus.QUEUED and due(row) - later == timedelta(minutes=1)
    assert Job.objects.filter(kind=deliveries.JOB_KIND, run_after=due(row)).exists()
    [logged] = DeliveryAttempt.objects.filter(delivery=row)
    assert logged.outcome == "retrying" and "worker stopped" in logged.error and logged.next_attempt_at == due(row)
    assert WebhookDestination.objects.get(id=hook["id"]).last_delivery_outcome == "retrying"
    assert attempt(row.id, due(row)).status == DeliveryStatus.SENT and len(receiver.requests) == 1
    # Out of attempts: failed, logged and counted.
    Delivery.objects.filter(id=row.id).update(status="queued", attempts=5, next_attempt_at=timezone.now())
    assert deliveries.claim(row.id) is not None
    deliveries.sweep(later + timedelta(hours=1))
    stopped = Delivery.objects.get(id=row.id)
    assert stopped.status == DeliveryStatus.FAILED and "Gave up" in stopped.last_error
    assert DeliveryAttempt.objects.filter(delivery=row, outcome="failed").exists()
    assert WebhookDestination.objects.get(id=hook["id"]).consecutive_failures == 1


def test_finished_deliveries_are_purged_after_their_retention(a, settings):
    from core import jobs
    from notify.models import Digest, DigestRun

    settings.SOFTWARDEN_DELIVERY_RETENTION_SENT_DAYS = 30
    settings.SOFTWARDEN_DELIVERY_RETENTION_FAILED_DAYS = 90
    now = timezone.now()

    def made(key, status, days, **extra):
        row = deliveries.enqueue(
            a["org"],
            channel="email",
            dedupe_key=key,
            subject="s",
            content={"text": "t"},
            recipient="x@orga.test",
            **extra,
        )[0]
        Delivery.objects.filter(id=row.id).update(status=status, created_at=now - timedelta(days=days))
        DeliveryAttempt.objects.create(organization=a["org"], delivery=row, number=1, started_at=now, outcome="sent")
        return row.id

    old_sent = made("old-sent", "sent", 31)
    new_sent = made("new-sent", "sent", 29)
    old_failed = made("old-failed", "failed", 91)
    young_failed = made("young-failed", "failed", 60)
    old_queued = made("old-queued", "queued", 400)
    live = Reminder.objects.create(
        organization=a["org"],
        source_type="periodic_review",
        source_id=a["app"].id,
        due_date=now.date(),
        title="t",
        label="l",
    )
    ledger = made("ledger", "sent", 200, reminder=live)
    kept_for_digest = made("digest", "sent", 200)
    run = DigestRun.objects.create(organization=a["org"], for_date=now.date())
    Digest.objects.create(
        organization=a["org"], run=run, user=a["users"]["admin"], for_date=now.date(), delivery_id=kept_for_digest
    )
    assert deliveries.purge(now) == {"deliveries_purged": 2}
    left = set(Delivery.objects.values_list("id", flat=True))
    assert {old_sent, old_failed} & left == set()
    assert {new_sent, young_failed, old_queued, ledger, kept_for_digest} <= left
    assert not DeliveryAttempt.objects.filter(delivery_id__in=[old_sent, old_failed]).exists()
    assert jobs.SCHEDULES["notify.retention"].func is deliveries.purge


def test_turning_an_endpoint_off_by_hand_clears_what_softwarden_recorded(a, api):
    hook = endpoint(a, api)
    WebhookDestination.objects.filter(id=hook["id"]).update(disabled_at=timezone.now(), disabled_reason="Old reason.")
    admin = api(a["users"]["admin"])
    etag = admin.get(f"webhooks/{hook['id']}")["ETag"]
    off = admin.client.patch(
        f"/api/v1/webhooks/{hook['id']}", json.dumps({"enabled": False}), "application/json", HTTP_IF_MATCH=etag
    ).json()
    assert off["enabled"] is False and off["disabled_at"] is None and off["disabled_reason"] == ""


def test_the_destinations_route_refuses_events_and_description(a, api):
    admin = api(a["users"]["admin"])
    refused = admin.post(
        "notification-settings/webhook-destinations",
        {"name": "Old", "url": URL, "signing_secret": SECRET, "events": ["record.*"], "description": "x"},
    )
    assert refused.status_code == 400 and {"events", "description"} <= set(refused.json_body["errors"])
    assert "/api/v1/webhooks" in refused.json_body["errors"]["events"][0]
    made = admin.post(
        "notification-settings/webhook-destinations", {"name": "Old", "url": URL, "signing_secret": SECRET}
    )
    assert made.status_code == 201
    path = f"notification-settings/webhook-destinations/{made.json_body['id']}"
    assert admin.patch(path, {"events": []}).status_code == 400


def test_the_migration_back_fills_the_event_type_of_older_webhook_deliveries(a, api):
    import importlib

    from django.db import connection

    migration = importlib.import_module("notify.migrations.0009_webhooks").Migration
    backfill = migration.operations[-1].sql
    hook = WebhookDestination.objects.get(id=endpoint(a, api)["id"])
    old = _notice(a, hook.id)
    Delivery.objects.filter(id=old.id).update(event_type="")
    email = deliveries.enqueue(
        a["org"], channel="email", dedupe_key="mail", subject="s", content={"event": "x"}, recipient="x@orga.test"
    )[0]
    with connection.cursor() as cursor:
        cursor.execute(backfill)
    assert Delivery.objects.get(id=old.id).event_type == "reminder.due"
    assert Delivery.objects.get(id=email.id).event_type == ""


def test_the_list_doesnt_load_payloads(a, api, receiver):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    endpoint(a, api)
    webhooks.deliver(a["org"].pk, [webhooks.make_event("record.updated", {"record": {"id": "r"}})])
    with CaptureQueriesContext(connection) as seen:
        rows = api(a["users"]["admin"]).get("webhook-deliveries").json_body["results"]
    assert rows[0]["event_type"] == "record.updated"
    listed = [q["sql"] for q in seen.captured_queries if 'FROM "notify_delivery"' in q["sql"]]
    assert listed and all('"notify_delivery"."content"' not in sql for sql in listed)


def test_a_thousand_events_in_one_transaction_are_queued_in_order(a, api):
    hook = endpoint(a, api)
    with transaction.atomic():
        for i in range(1000):
            with transaction.atomic():  # a savepoint each, as one service call per row makes
                webhooks.queue(a["org"], [webhooks.make_event("record.updated", {"record": {"id": str(i)}})])
    assert WebhookEvent.objects.count() == 1000
    # Two batches (DRAIN_BATCH 500), oldest first.
    assert webhooks.drain() == {"webhook_events_drained": 1000}
    rows = list(event_deliveries().filter(webhook_destination_id=hook["id"]))
    assert len(rows) == 1000
    jobs_in_claim_order = Job.objects.filter(kind=deliveries.JOB_KIND).order_by("run_after", "created_at")
    by_delivery = {str(d.id): d.content["data"]["record"]["id"] for d in rows}
    assert [by_delivery[j.payload["delivery_id"]] for j in jobs_in_claim_order] == [str(i) for i in range(1000)]


def test_events_of_several_organizations_keep_their_order_and_stay_apart(a, b, api):
    ours, theirs = endpoint(a, api, name="Ours"), endpoint(b, api, name="Theirs")
    for i in range(6):
        f = a if i % 2 == 0 else b
        webhooks.queue(f["org"], [webhooks.make_event("record.updated", {"record": {"id": str(i)}})])
    webhooks.drain()
    for f, hook, expected in ((a, ours, ["0", "2", "4"]), (b, theirs, ["1", "3", "5"])):
        jobs_in_order = Job.objects.filter(kind=deliveries.JOB_KIND, organization=f["org"]).order_by("run_after")
        rows = {str(d.id): d for d in event_deliveries(f["org"])}
        assert [rows[j.payload["delivery_id"]].content["data"]["record"]["id"] for j in jobs_in_order] == expected
        assert {str(d.webhook_destination_id) for d in rows.values()} == {hook["id"]}


def test_hooks_registered_inside_a_capture_are_only_the_reminders(
    a, api, monkeypatch, django_capture_on_commit_callbacks
):
    """Webhooks register no commit hook: a write before a capture block can't affect what runs inside
    it, and every event is in the outbox whether or not a commit hook ever runs."""
    endpoint(a, api)
    org, editor = a["org"], a["users"]["editor"]
    flushed: list = []
    real = reminders.flush

    def counted(store):
        flushed.append(1)
        return real(store)

    monkeypatch.setattr(reminders, "flush", counted)
    services.archive_resource(org, editor, a["app"])  # before the block: its reminder hook waits for a commit
    with django_capture_on_commit_callbacks(execute=True) as inside:
        services.update_resource(org, editor, a["app"], {"name": "Renamed inside"})
    assert flushed == [1] and len(inside) == 1  # the reminder flush, nothing of the webhooks
    webhooks.drain()
    assert sorted(event_deliveries().values_list("event_type", flat=True)) == ["record.archived", "record.updated"]


def test_the_drain_skips_while_another_session_holds_its_lock(a, api):
    from django.db import connections

    endpoint(a, api)
    webhooks.queue(a["org"], [webhooks.make_event("record.updated", {})])
    other = connections.create_connection("default")  # a second session, as a second worker
    try:
        with other.cursor() as cursor:
            cursor.execute("BEGIN")
            cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [webhooks.DRAIN_LOCK])
            assert cursor.fetchone()[0] is True
            assert webhooks.drain_once() is None  # held elsewhere: nothing taken
            assert WebhookEvent.objects.count() == 1
            cursor.execute("ROLLBACK")
    finally:
        other.close()
    assert webhooks.drain_once() == 1 and event_deliveries().count() == 1


def test_the_outbox_is_drained_by_its_own_schedule():
    from core import jobs

    definition = jobs.SCHEDULES["webhooks.outbox"]
    assert definition.func is webhooks.drain and definition.cron == "* * * * *"


@pytest.mark.django_db(transaction=True)
def test_an_event_committed_before_a_crash_is_still_delivered(a, api, receiver, monkeypatch):
    """Real commits: the write commits with its outbox row; the process "crashes" (its connection is dropped and no
    Python state survives), the first drain dies before it commits (its fan-out raises), and a later tick still posts
    the event, once."""
    from django.db import connection

    endpoint(a, api, events=["record.archived"])
    services.archive_resource(a["org"], a["users"]["editor"], a["app"])  # autocommit: its own committed transaction
    connection.close()  # the crash: nothing in memory is left to post the event
    assert WebhookEvent.objects.count() == 1

    def crash(*args, **kwargs):
        raise RuntimeError("the worker died mid-drain")

    real = webhooks.deliver
    monkeypatch.setattr(webhooks, "deliver", crash)
    with pytest.raises(RuntimeError):
        webhooks.drain_once()
    assert WebhookEvent.objects.count() == 1 and not event_deliveries().exists()  # rolled back, still in the outbox
    monkeypatch.setattr(webhooks, "deliver", real)

    worker.tick()

    assert not WebhookEvent.objects.exists()
    [delivery] = event_deliveries()
    assert delivery.status == DeliveryStatus.SENT and len(receiver.requests) == 1
    assert receiver.json()["type"] == "record.archived"
    worker.tick()
    assert len(receiver.requests) == 1  # posted once
