"""Delivery engine (notify/deliveries.py): dedupe keys, jobs, claims and leases, retries with Retry-After and
backoff, no retry after an ambiguous failure, the sweep, rendering under Teams' 28 KB, and SMTP outcomes against an
in-process server. Transports are mocked; nothing contacts Microsoft or Teams."""

import json
import threading
from datetime import timedelta
from typing import Any

import pytest
from django.core import mail
from django.utils import timezone

from core import graph, graph_simulator, jobs, outbound, worker
from core.models import Job, JobStatus
from notify import channel_services, channels, deliveries, render
from notify.models import Delivery, DeliveryStatus, TeamsDestination, WebhookDestination
from tests.conftest import TICK_MOMENTS, quiet_schedules
from tests.fake_smtp import FakeSmtp, Script
from tests.test_concurrency import run_together

GRAPH = {
    "transport": "graph",
    "graph_tenant_id": "simulated",
    "graph_client_id": "11111111-2222-4333-8444-555555555555",
    "graph_secret": "graph-secret-value",
    "sender_mailbox": "softwarden@contoso.example",
}
TEAMS_URL = "https://prod-00.westus.logic.azure.example/workflows/abc?sig=SECRET-SIG"


RELAY = "mail.example.test"  # an organization's SMTP relay name, pinned to the in-process server on loopback


@pytest.fixture(autouse=True)
def relay_on_loopback(monkeypatch):
    """Organization SMTP hosts may not be loopback (core/outbound.smtp_address); the fake server is, so the relay's
    name resolves to it here. Every other host is checked for real."""
    real = outbound.smtp_address
    monkeypatch.setattr(outbound, "smtp_address", lambda host, port: "127.0.0.1" if host == RELAY else real(host, port))


@pytest.fixture(autouse=True)
def clean():
    graph_simulator.OUTBOX.clear()
    channels.forget_tokens()
    yield
    graph_simulator.OUTBOX.clear()
    channels.forget_tokens()


@pytest.fixture
def scripted(monkeypatch):
    """The Graph simulator with a queue of answers for sendMail (a Reply or an exception), shared by every send."""
    transport = graph_simulator.Transport()
    monkeypatch.setattr(channels, "graph_transport", lambda tenant: transport)
    return transport


@pytest.fixture
def posts(monkeypatch):
    calls: list = []
    replies: list = []

    def fake(url, body, headers, timeout=15):
        calls.append((url, body, dict(headers)))
        if replies:
            step = replies.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step
        return graph.Reply(200, {}, b"1")

    monkeypatch.setattr(channels, "post_https", fake)
    monkeypatch.setattr(channels, "TEAMS_MIN_INTERVAL", 0)
    fake.calls = calls  # type: ignore[attr-defined]
    fake.replies = replies  # type: ignore[attr-defined]
    return fake


@pytest.fixture
def locmem(settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


def email(org, key="reminder:1:30:email:u1", **kwargs):
    values = {
        "channel": "email",
        "dedupe_key": key,
        "subject": "Contract cancellation deadline in 30 days",
        "content": render.email_content("Heads up", [{"title": "Payroll", "detail": "Cancel by 2026-12-05"}]),
        "recipient": "dana@orga.test",
    }
    return deliveries.enqueue(org, **{**values, **kwargs})


def graph_org(f):
    channel_services.update_email_settings(f["org"], f["users"]["admin"], GRAPH)


def teams(f, **data):
    return channel_services.create_destination(
        f["org"], f["users"]["admin"], TeamsDestination, {"name": "IT Ops", "url": TEAMS_URL, **data}
    )


def error(status, code="", headers=None):
    body = json.dumps({"error": {"code": code, "message": "m"}}).encode()
    return graph.Reply(status, headers or {}, body)


def attempt(delivery_id, now=None) -> Any:
    """notify.deliveries.attempt, typed loosely for the assertions."""
    return deliveries.attempt(delivery_id, now)


def claim(delivery_id, now=None) -> Any:
    return deliveries.claim(delivery_id, now)


def drain(now=None):
    """Run every due job (as the worker's tick does)."""
    ran = 0
    while (job := jobs.claim(now)) is not None:
        jobs.run(job, now)
        ran += 1
    return ran


# ---- enqueue and dedupe -----------------------------------------------------------------------------------------------


def test_enqueue_is_idempotent_on_the_dedupe_key(a):
    first, created = email(a["org"])
    again, created_again = email(a["org"], subject="Something else")
    assert created and not created_again and again.id == first.id and again.subject == first.subject
    assert Delivery.objects.count() == 1
    assert Job.objects.filter(kind=deliveries.JOB_KIND).count() == 1


def test_the_same_key_in_another_organization_is_another_delivery(a, b):
    email(a["org"])
    email(b["org"], recipient="dana@orgb.test")
    assert Delivery.objects.count() == 2


def test_keys_never_hold_an_address_or_url(a):
    for key in ("reminder:dana@orga.test", "teams:https://x.test", "", "k" * 201):
        with pytest.raises(ValueError):
            email(a["org"], key=key)
    assert not Delivery.objects.exists()


def test_job_payloads_carry_the_delivery_id_only(a):
    delivery, _ = email(a["org"])
    job = Job.objects.get(kind=deliveries.JOB_KIND)
    assert job.payload == {"delivery_id": str(delivery.id)} and job.max_attempts == 1
    jobs.check_payload(job.payload)  # passes the no-secrets guard


@pytest.mark.parametrize("clock", TICK_MOMENTS, indirect=True)
def test_the_worker_sends_once_and_a_rerun_sends_nothing(a, locmem, clock):
    # The job counts below are the test's own, whatever the time of day: no digest (08:00), no eol.refresh (04:45).
    quiet_schedules()
    delivery, _ = email(a["org"])
    summary = worker.tick(job_budget=timedelta(seconds=5))
    assert summary["jobs"] == {"succeeded": 1}
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["dana@orga.test"]
    row = Delivery.objects.get(id=delivery.id)
    assert row.status == DeliveryStatus.SENT and row.attempts == 1 and row.sent_at and row.claim_token
    # Enqueue again (the rules job reruns), run the old job again, tick again: nothing more goes out.
    email(a["org"])
    attempt(delivery.id)
    jobs.enqueue(deliveries.JOB_KIND, {"delivery_id": str(delivery.id)}, organization=a["org"])
    worker.tick(job_budget=timedelta(seconds=5))
    assert len(mail.outbox) == 1
    assert Delivery.objects.get(id=delivery.id).attempts == 1


def test_two_workers_never_send_the_same_delivery(transactional_db, monkeypatch, settings):
    from tests.conftest import make_org

    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    f = make_org("race")
    sent: list = []
    lock = threading.Lock()

    def slow_send(org, delivery):
        import time

        time.sleep(0.05)
        with lock:
            sent.append(delivery.id)

    monkeypatch.setattr(channels, "send", slow_send)
    rows = [email(f["org"], key=f"race:{n}")[0] for n in range(5)]
    for row in rows:  # a duplicate job for each delivery as well
        jobs.enqueue(deliveries.JOB_KIND, {"delivery_id": str(row.id)}, organization=f["org"])

    def work():
        while (job := jobs.claim()) is not None:
            jobs.run(job)

    assert run_together(work, work, work) == [None, None, None]
    assert sorted(sent, key=str) == sorted((r.id for r in rows), key=str)
    assert set(Delivery.objects.values_list("status", flat=True)) == {DeliveryStatus.SENT}


def test_a_row_another_worker_has_locked_is_skipped(transactional_db, settings):
    from django.db import connection, transaction

    from tests.conftest import make_org

    f = make_org("locked")
    delivery, _ = email(f["org"])
    holding, release = threading.Event(), threading.Event()

    def hold():
        try:
            with transaction.atomic():
                list(Delivery.objects.select_for_update().filter(id=delivery.id))
                holding.set()
                release.wait(5)
        finally:
            connection.close()

    thread = threading.Thread(target=hold)
    thread.start()
    holding.wait(5)
    try:
        assert claim(delivery.id) is None  # skipped, not waited on
    finally:
        release.set()
        thread.join()
    assert claim(delivery.id) is not None


def test_a_claimed_delivery_is_not_claimed_again(a):
    delivery, _ = email(a["org"])
    first = claim(delivery.id)
    assert first is not None and first.status == DeliveryStatus.SENDING and first.claimed_until
    assert claim(delivery.id) is None
    later = timezone.now() + timedelta(minutes=1)
    assert claim(delivery.id, later) is None


# ---- Graph outcomes ---------------------------------------------------------------------------------------------------


def test_graph_202_is_sent_with_the_attempts_client_request_id(a, scripted):
    graph_org(a)
    delivery, _ = email(a["org"])
    done = attempt(delivery.id)
    assert done.status == DeliveryStatus.SENT
    method, url, headers = scripted.calls[-1]
    assert url.endswith("/users/softwarden@contoso.example/sendMail")
    assert headers["client-request-id"] == str(done.client_request_id)
    message = graph_simulator.OUTBOX[-1]["message"]
    assert message["body"]["contentType"] == "HTML" and "Payroll" in message["body"]["content"]


def test_graph_429_waits_for_retry_after_then_sends(a, scripted):
    graph_org(a)
    delivery, _ = email(a["org"])
    scripted.script.append(error(429, "TooManyRequests", {"retry-after": "120"}))
    now = timezone.now()
    done = attempt(delivery.id, now)
    assert done.status == DeliveryStatus.QUEUED and done.attempts == 1 and "429" in done.last_error
    assert abs((done.next_attempt_at - now).total_seconds() - 120) < 1
    retry: Any = Job.objects.filter(kind=deliveries.JOB_KIND, status=JobStatus.QUEUED).order_by("-run_after").first()
    assert retry.run_after == done.next_attempt_at
    assert attempt(delivery.id, now + timedelta(seconds=60)) is None  # not due yet
    done = attempt(delivery.id, now + timedelta(seconds=121))
    assert done.status == DeliveryStatus.SENT and done.attempts == 2 and done.last_error == ""
    assert len(graph_simulator.OUTBOX) == 1


def test_graph_503_backs_off_exponentially_then_gives_up(a, scripted):
    graph_org(a)
    delivery, _ = email(a["org"])
    scripted.script += [error(503, "ServiceUnavailable")] * 4
    now = timezone.now()
    waits = []
    for _ in range(3):
        done = attempt(delivery.id, now)
        waits.append(round((done.next_attempt_at - now).total_seconds()))
        now = done.next_attempt_at
    assert waits == [60, 240, 960]
    done = attempt(delivery.id, now)
    assert done.status == DeliveryStatus.FAILED and done.attempts == 4 and "Gave up after 4 attempts" in done.last_error
    assert graph_simulator.OUTBOX == []


def test_retry_after_is_capped_at_an_hour(a, scripted):
    graph_org(a)
    delivery, _ = email(a["org"])
    scripted.script.append(error(503, "", {"retry-after": "86400"}))
    now = timezone.now()
    done = attempt(delivery.id, now)
    assert done.next_attempt_at - now == timedelta(hours=1)


@pytest.mark.parametrize(
    "answer",
    [
        error(500, "InternalServerError"),
        error(502),
        error(504, "GatewayTimeout"),
        graph.Unreachable("TimeoutError", ambiguous=True),
        graph.Unreachable("RemoteDisconnected", ambiguous=True),
    ],
)
def test_an_ambiguous_failure_is_never_retried(a, scripted, answer):
    graph_org(a)
    delivery, _ = email(a["org"])
    scripted.script.append(answer)
    done = attempt(delivery.id)
    assert done.status == DeliveryStatus.FAILED and done.attempts == 1
    assert "outcome is unknown" in done.last_error
    # Only the job that was queued with the delivery: no retry job was added.
    assert Job.objects.filter(kind=deliveries.JOB_KIND, payload={"delivery_id": str(delivery.id)}).count() == 1


def test_a_connection_that_never_opened_is_retried(a, scripted):
    graph_org(a)
    delivery, _ = email(a["org"])
    scripted.script.append(graph.Unreachable("ConnectionRefusedError", ambiguous=False))
    assert attempt(delivery.id).status == DeliveryStatus.QUEUED


@pytest.mark.parametrize(
    "sender,code,words",
    [
        ("denied@contoso.example", "ErrorAccessDenied", "Exchange RBAC for Applications"),
        ("missing@contoso.example", "ErrorInvalidUser", "doesn't know the sender mailbox"),
    ],
)
def test_graph_refusals_fail_at_once_with_a_readable_reason(a, scripted, sender, code, words):
    channel_services.update_email_settings(a["org"], a["users"]["admin"], {**GRAPH, "sender_mailbox": sender})
    delivery, _ = email(a["org"])
    done = attempt(delivery.id)
    assert done.status == DeliveryStatus.FAILED and code in done.last_error and words in done.last_error
    assert done.attempts == 1


def test_a_refused_client_secret_fails_without_sending(a, scripted):
    channel_services.update_email_settings(a["org"], a["users"]["admin"], {**GRAPH, "graph_secret": "wrong-secret"})
    delivery, _ = email(a["org"])
    done = attempt(delivery.id)
    assert done.status == DeliveryStatus.FAILED and "didn't issue a token (invalid_client)" in done.last_error
    assert "wrong-secret" not in done.last_error and graph_simulator.OUTBOX == []


def test_a_throttled_token_request_is_retried(a, monkeypatch):
    graph_org(a)

    class Throttled(graph_simulator.Transport):
        def _token(self, body):
            return graph.Reply(429, {"retry-after": "30"}, b'{"error":"temporarily_unavailable"}')

    monkeypatch.setattr(channels, "graph_transport", lambda tenant: Throttled())
    delivery, _ = email(a["org"])
    now = timezone.now()
    done = attempt(delivery.id, now)
    assert done.status == DeliveryStatus.QUEUED and (done.next_attempt_at - now).total_seconds() == 30


def test_tokens_are_reused_until_they_near_expiry(a, scripted):
    graph_org(a)
    for n in range(3):
        attempt(email(a["org"], key=f"t:{n}")[0].id)
    tokens = [c for c in scripted.calls if "oauth2" in c[1]]
    assert len(tokens) == 1 and len(graph_simulator.OUTBOX) == 3


def test_four_sends_in_flight_from_a_mailbox_make_the_next_wait(a, scripted, monkeypatch):
    graph_org(a)
    monkeypatch.setattr(channels, "GRAPH_CONCURRENCY", 0)  # every slot taken
    delivery, _ = email(a["org"])
    now = timezone.now()
    done = attempt(delivery.id, now)
    assert done.status == DeliveryStatus.QUEUED and done.attempts == 0
    assert (done.next_attempt_at - now) == deliveries.BUSY_DELAY and graph_simulator.OUTBOX == []


def test_the_graph_slot_locks_are_released(a, scripted):
    from django.db import connection

    graph_org(a)
    attempt(email(a["org"])[0].id)
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()")
        assert cursor.fetchone()[0] == 0


# ---- SMTP outcomes ----------------------------------------------------------------------------------------------------


def smtp_org(f, port, **extra):
    channel_services.update_email_settings(
        f["org"],
        f["users"]["admin"],
        {
            "transport": "smtp",
            "smtp_host": RELAY,
            "smtp_port": port,
            "smtp_security": "none",
            "from_address": "softwarden@contoso.example",
            **extra,
        },
    )


def test_smtp_sends_with_the_notification_header(a):
    with FakeSmtp(Script(username="mailer", password="smtp-pass-1")) as server:
        smtp_org(a, server.port, smtp_username="mailer", smtp_password="smtp-pass-1")
        delivery, _ = email(a["org"])
        assert attempt(delivery.id).status == DeliveryStatus.SENT
    [message] = server.script.messages
    assert message["from"] == "<softwarden@contoso.example>" and message["to"] == ["<dana@orga.test>"]
    assert f"x-softwarden-notification-id: {delivery.id}" in message["data"]


@pytest.mark.parametrize(
    "script,status,words",
    [
        (Script(rcpt_reply="451 4.7.1 Try again later"), DeliveryStatus.QUEUED, "refused the recipient (451)"),
        (Script(rcpt_reply="550 5.1.1 No such user"), DeliveryStatus.FAILED, "refused the recipient (550)"),
        (Script(data_reply="452 4.3.1 Insufficient storage"), DeliveryStatus.QUEUED, "deferred the message (452)"),
        (Script(data_reply="554 5.7.1 Rejected"), DeliveryStatus.FAILED, "refused the message (554)"),
        (Script(drop_after_data=True), DeliveryStatus.FAILED, "outcome is unknown"),
    ],
)
def test_smtp_outcomes(a, script, status, words):
    with FakeSmtp(script) as server:
        smtp_org(a, server.port)
        delivery, _ = email(a["org"])
        done = attempt(delivery.id)
    assert done.status == status and words in done.last_error, done.last_error


def test_smtp_refused_credentials_fail_without_leaking_the_password(a):
    with FakeSmtp(Script(username="mailer", password="right-password")) as server:
        smtp_org(a, server.port, smtp_username="mailer", smtp_password="wrong-password-xyz")
        done = attempt(email(a["org"])[0].id)
    assert done.status == DeliveryStatus.FAILED and "535" in done.last_error
    assert "wrong-password-xyz" not in done.last_error and server.script.messages == []


def test_an_smtp_server_that_isnt_there_is_retried(a):
    import socket

    with socket.socket() as probe:  # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    smtp_org(a, port)
    done = attempt(email(a["org"])[0].id)
    assert done.status == DeliveryStatus.QUEUED and "Couldn't connect" in done.last_error


def test_without_email_configured_a_delivery_is_skipped(a, settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = False
    done = attempt(email(a["org"])[0].id)
    assert done.status == DeliveryStatus.SKIPPED and mail.outbox == []


# ---- Teams and webhook outcomes --------------------------------------------------------------------------------------


def teams_delivery(f, destination, key="reminder:1:30:teams", items: Any = None):
    content = render.card_content("Contract cancellation deadlines", items or [{"title": "Payroll", "detail": "x"}])
    return deliveries.enqueue(
        f["org"], channel="teams", dedupe_key=key, subject="Deadlines", content=content, teams_destination=destination
    )[0]


@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_any_2xx_from_a_workflow_is_success(a, posts, status):
    posts.replies.append(graph.Reply(status, {}, b""))
    assert attempt(teams_delivery(a, teams(a)).id).status == DeliveryStatus.SENT


def test_teams_429_honours_retry_after_and_4xx_fails(a, posts):
    destination = teams(a)
    queued = teams_delivery(a, destination)
    now = timezone.now()
    posts.replies.append(graph.Reply(429, {"retry-after": "9"}, b""))
    done = attempt(queued.id, now)
    assert done.status == DeliveryStatus.QUEUED and (done.next_attempt_at - now).total_seconds() == 9
    posts.replies.append(graph.Reply(404, {}, b"Not found"))
    done = attempt(teams_delivery(a, destination, key="other").id)
    assert done.status == DeliveryStatus.FAILED and "refused the message (404)" in done.last_error
    posts.replies.append(graph.Reply(302, {"location": "https://evil.test"}, b""))
    done = attempt(teams_delivery(a, destination, key="redirect").id)
    assert done.status == DeliveryStatus.FAILED and "redirect" in done.last_error


def test_turned_off_or_deleted_destinations_are_skipped(a, posts):
    destination = teams(a)
    first = teams_delivery(a, destination)
    second = teams_delivery(a, destination, key="two")
    channel_services.update_destination(a["org"], a["users"]["admin"], destination, {"enabled": False})
    assert attempt(first.id).status == DeliveryStatus.SKIPPED
    channel_services.delete_destination(a["org"], a["users"]["admin"], TeamsDestination.objects.get())
    done = attempt(second.id)
    assert done.status == DeliveryStatus.SKIPPED and "deleted" in done.last_error
    assert posts.calls == []


def test_a_destination_resolving_to_a_private_network_fails_at_send(a, monkeypatch):
    import socket

    destination = teams(a)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port))],
    )
    done = attempt(teams_delivery(a, destination).id)
    assert done.status == DeliveryStatus.FAILED and "isn't allowed" in done.last_error
    assert "SECRET-SIG" not in done.last_error


def test_large_cards_are_truncated_under_28_kb(a, posts):
    items: Any = [
        {"title": f"Contract {n:03d} " + "x" * 150, "detail": "d" * 300, "url": f"https://sw.test/c/{n}"}
        for n in range(400)
    ]
    content = render.card_content("Many deadlines", items)
    body = render.teams_body(content["card"])
    assert len(body) <= render.TEAMS_LIMIT
    last = content["card"]["body"][-1]
    shown = sum(1 for block in content["card"]["body"] if block["type"] == "Container")
    assert last["text"] == f"and {400 - shown} more" and 0 < shown < 400
    small = render.card_content("Few", items[:3])
    assert all(block.get("text") != "and 0 more" for block in small["card"]["body"])
    delivery = teams_delivery(a, teams(a), items=items)
    assert attempt(delivery.id).status == DeliveryStatus.SENT
    assert len(posts.calls[0][1]) <= render.TEAMS_LIMIT


def test_card_text_is_markdown_never_html():
    content = render.card_content("<b>Bold</b> [x](y)", [{"title": "<script>", "detail": "a [b] c"}])
    text = json.dumps(content)
    assert "<" not in text and "[x](y)" not in text


def test_webhook_signature_is_verifiable(a, posts):
    import hashlib
    import hmac

    hook = channel_services.create_destination(
        a["org"],
        a["users"]["admin"],
        WebhookDestination,
        {"name": "Hook", "url": "https://hooks.example.test/in", "signing_secret": "k" * 32},
    )
    delivery = deliveries.enqueue(
        a["org"],
        channel="webhook",
        dedupe_key="reminder:1:30:webhook",
        subject="Deadline",
        content=render.webhook_content("reminder.due", {"title": "Payroll"}),
        webhook_destination=hook,
    )[0]
    assert attempt(delivery.id).status == DeliveryStatus.SENT
    _url, body, headers = posts.calls[0]
    stamp = headers[channels.SIGNATURE_HEADER].split(",")[0][2:]
    assert headers[channels.SIGNATURE_HEADER] == channels.signature("k" * 32, int(stamp), body)
    expected = hmac.new(b"k" * 32, f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    assert headers[channels.SIGNATURE_HEADER].endswith(expected)
    assert json.loads(body)["data"] == {"title": "Payroll"} and json.loads(body)["type"] == "reminder.due"


# ---- unexpected errors and the sweep ----------------------------------------------------------------------------------


def test_an_unexpected_error_mid_send_is_ambiguous_and_scrubbed(a, posts):
    destination = teams(a)
    posts.replies.append(ValueError(f"exploded posting to {TEAMS_URL}"))
    done = attempt(teams_delivery(a, destination).id)
    assert done.status == DeliveryStatus.FAILED and "outcome is unknown" in done.last_error
    assert "SECRET-SIG" not in done.last_error and "[redacted]" in done.last_error


def test_the_sweep_fails_an_expired_claim_and_a_late_outcome_still_wins(a, locmem):
    delivery, _ = email(a["org"])
    claimed = claim(delivery.id)
    later = timezone.now() + deliveries.LEASE + timedelta(seconds=1)
    assert deliveries.sweep(later)["deliveries_expired"] == 1
    row = Delivery.objects.get(id=delivery.id)
    assert row.status == DeliveryStatus.FAILED and "worker stopped during attempt 1" in row.last_error
    assert claim(delivery.id) is None  # never retried automatically
    deliveries._record(claimed, claimed.claim_token, status=DeliveryStatus.SENT, sent_at=timezone.now())
    assert Delivery.objects.get(id=delivery.id).status == DeliveryStatus.SENT


def test_the_sweep_requeues_a_queued_delivery_whose_job_is_gone(a):
    delivery, _ = email(a["org"])
    Job.objects.filter(kind=deliveries.JOB_KIND).delete()
    assert deliveries.sweep()["deliveries_requeued"] == 0  # not stale yet
    later = timezone.now() + deliveries.ORPHAN_AFTER + timedelta(minutes=1)
    assert deliveries.sweep(later)["deliveries_requeued"] == 1
    assert deliveries.sweep(later)["deliveries_requeued"] == 0  # its job exists now
    assert Job.objects.get(kind=deliveries.JOB_KIND).payload == {"delivery_id": str(delivery.id)}


def test_the_sweep_and_the_job_are_registered():
    assert jobs.JOB_KINDS[deliveries.JOB_KIND].func is deliveries.run_job
    assert jobs.JOB_KINDS[deliveries.JOB_KIND].max_attempts == 1
    assert jobs.SCHEDULES["notify.deliveries"].func is deliveries.sweep


def test_a_job_without_a_delivery_fails_permanently(a):
    job = jobs.enqueue(deliveries.JOB_KIND, {}, organization=a["org"])
    claimed = jobs.claim()
    assert claimed.id == job.id
    assert jobs.run(claimed) == JobStatus.FAILED


# ---- wall-clock deadlines, scrubbing before the cut, Graph's text ---------------------------------------------------


@pytest.fixture
def trickling_http(monkeypatch):
    """A plain-TCP server on loopback that answers a status line and then a byte every 0.2 s, with post() pointed
    at it (TLS and the address checks replaced: this test is about the deadline only)."""
    import socket
    import socketserver
    import time

    class Trickle(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(65536)
            try:
                self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
                for _ in range(200):
                    self.request.sendall(b"x")
                    time.sleep(0.2)
            except OSError:
                return

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Trickle)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def plain_connect(self):
        self._raw = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._raw

    monkeypatch.setattr(outbound, "resolve_public", lambda host, port: "127.0.0.1")
    monkeypatch.setattr(outbound._PinnedHTTPSConnection, "connect", plain_connect)
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_a_trickling_webhook_is_cut_off_at_the_deadline(trickling_http):
    import time

    started = time.monotonic()
    with pytest.raises(graph.Unreachable) as cut:
        outbound.post(f"https://hooks.example.test:{trickling_http}/in", b"{}", {}, timeout=5, deadline=1.0)
    assert time.monotonic() - started < 4
    assert cut.value.ambiguous is True and "within 1 s" in str(cut.value)


def test_a_trickling_smtp_greeting_is_cut_off(a, monkeypatch):
    import time

    from notify import smtp

    monkeypatch.setattr(smtp, "DEADLINE", 1.0)
    with FakeSmtp(Script(trickle_greeting=True)) as server:
        smtp_org(a, server.port)
        started = time.monotonic()
        result = channels.test_email_connection(a["org"])
        assert time.monotonic() - started < 5
        assert result.ok is False and result.detail == channels.SMTP_NO_CONNECTION
        done = attempt(email(a["org"])[0].id)
    assert done.status == DeliveryStatus.QUEUED  # nothing was sent: retried


def test_a_trickling_answer_to_the_message_is_ambiguous(a, monkeypatch):
    import time

    from notify import smtp

    monkeypatch.setattr(smtp, "DEADLINE", 1.0)
    with FakeSmtp(Script(trickle_after_data=True)) as server:
        smtp_org(a, server.port)
        started = time.monotonic()
        done = attempt(email(a["org"])[0].id)
        assert time.monotonic() - started < 5
    assert done.status == DeliveryStatus.FAILED and "didn't finish within 1 s" in done.last_error
    assert "outcome is unknown" in done.last_error


def test_secrets_are_scrubbed_before_the_message_is_cut(a, posts):
    """A long prefix pushes the URL past the 500-character cut: no part of it may survive, nor its path alone."""
    long_url = (
        "https://prod-00.westus.logic.azure.example/workflows/"
        + "w" * 60
        + "/triggers/manual/paths/invoke?api-version=2016-06-01&sp=%2Ftriggers&sv=1.0&sig=LONG-SIGNATURE-"
        + "s" * 40
    )
    assert len(long_url) > 183
    destination = channel_services.create_destination(
        a["org"], a["users"]["admin"], TeamsDestination, {"name": "Long", "url": long_url}
    )
    path_query = long_url.split("logic.azure.example", 1)[1]
    posts.replies.append(RuntimeError("p" * 350 + " " + long_url + " then the request line POST " + path_query))
    done = attempt(teams_delivery(a, destination).id)
    assert done.status == DeliveryStatus.FAILED and len(done.last_error) <= deliveries.ERROR_MAX
    for fragment in ("LONG-SIGNATURE", "sig=", "workflows/www", "logic.azure.example/workflows"):
        assert fragment not in done.last_error, fragment
    assert done.last_error.endswith(deliveries.AMBIGUOUS_NOTE)
    posts.replies.append(RuntimeError("q" * 600 + path_query))
    sent = channel_services.test_send_destination(a["org"], a["users"]["admin"], destination)
    assert "LONG-SIGNATURE" not in sent.last_error and "sig=" not in sent.last_error
    destination.refresh_from_db()
    assert "LONG-SIGNATURE" not in destination.last_test_detail


def test_graphs_own_error_text_is_not_kept(a, scripted):
    graph_org(a)
    delivery, _ = email(a["org"])
    body = json.dumps({"error": {"code": "ErrorInvalidRecipients", "message": "Server says dana@orga.test is odd"}})
    scripted.script.append(graph.Reply(400, {}, body.encode()))
    done = attempt(delivery.id)
    assert done.status == DeliveryStatus.FAILED
    assert done.last_error == "Graph refused the message (400 ErrorInvalidRecipients)."
