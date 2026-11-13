"""Request ids: every HTTP request, job, schedule run and command is one unit of work whose id
is written on its audit events and returned as X-Request-ID."""

import contextvars
import uuid

from django.core.management import call_command
from django.test import Client

from core import jobs
from core.models import AuditEvent, Job
from core.request_context import parse_request_id

FORWARDED = "6f1c1d1e-8d84-4b4e-9d61-2a7c2b3d4e5f"


def login(client):
    return client.post(
        "/api/v1/auth/login",
        {"email": "editor@orga.test", "password": "correct-horse-battery"},
        content_type="application/json",
    )


def test_each_request_gets_an_id_returned_and_written_on_its_events(a):
    client = Client()
    first, second = login(client), login(client)
    ids = [uuid.UUID(r["X-Request-ID"]) for r in (first, second)]
    assert ids[0] != ids[1]
    events = AuditEvent.objects.filter(action="user.login").order_by("created_at")
    assert [e.request_id for e in events] == ids
    assert uuid.UUID(client.get("/api/v1/health")["X-Request-ID"])  # every response carries one


def test_a_forwarded_id_is_honoured_only_from_a_trusted_proxy(a, settings):
    settings.TRUSTED_PROXIES = []
    untrusted = login(Client(HTTP_X_REQUEST_ID=FORWARDED))
    assert untrusted["X-Request-ID"] != FORWARDED

    settings.TRUSTED_PROXIES = ["127.0.0.1"]  # the test client's REMOTE_ADDR
    trusted = login(Client(HTTP_X_REQUEST_ID=FORWARDED))
    assert trusted["X-Request-ID"] == FORWARDED
    assert AuditEvent.objects.filter(request_id=FORWARDED, action="user.login").count() == 1
    # Not a UUID (or too long): a new id instead.
    assert login(Client(HTTP_X_REQUEST_ID="'; drop table")).get("X-Request-ID") != "'; drop table"


def test_parse_request_id_accepts_uuids_only():
    assert parse_request_id(FORWARDED) == uuid.UUID(FORWARDED)
    assert parse_request_id(uuid.UUID(FORWARDED).hex) == uuid.UUID(FORWARDED)  # nginx's 32 hex digits
    assert parse_request_id("") is None and parse_request_id("x" * 80) is None and parse_request_id("abc") is None


def test_a_job_uses_its_own_id(a, monkeypatch):
    from catalog import services

    monkeypatch.setattr(jobs, "JOB_KINDS", dict(jobs.JOB_KINDS))
    jobs.register_job("test.audits", lambda job, ctx: services.audit(a["org"], None, "export", None, {"rows": 0}))
    job = jobs.enqueue("test.audits", organization=a["org"])
    claimed = jobs.claim()
    assert claimed is not None and claimed.id == job.id
    jobs.run(claimed)
    assert Job.objects.get(id=job.id).status == "succeeded"
    assert AuditEvent.objects.get(action="export").request_id == job.id


def test_a_command_shares_one_id_across_its_events(a, b):
    """Outside any unit of work (a command's process: run here in empty contexts, without conftest's unit), the first
    event picks an id and the rest of the context keeps it; another process gets another."""

    def command(edition, *slugs):
        for slug in slugs:
            call_command("set_edition", slug, edition)

    contextvars.Context().run(command, "enterprise", "orga", "orgb")
    first = {e.request_id for e in AuditEvent.objects.filter(action="organization.edition_changed")}
    assert len(first) == 1 and None not in first
    call_command("set_edition", "orga", "community")  # inside conftest's unit: that unit's id
    contextvars.Context().run(command, "community", "orgb")
    ids = [
        e.request_id for e in AuditEvent.objects.filter(action="organization.edition_changed").order_by("created_at")
    ]
    assert len(ids) == 4 and len(set(ids)) == 3 and ids[3] not in first
