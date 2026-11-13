"""Each connector run is its own `discovery.run` job with a lease, the
`discovery.connectors` schedule only queues, the worker runs long jobs beside the tick and fairly between
organizations, a run whose lease expired is failed with a reason and a late finisher never overwrites the newer run,
one queued or running run per connector, Run now's 202, the run history API (filters, roles, tenancy), the `live`
marker, and the follow-ups to the Entra sources and reconciliation. Threaded tests use real
separate database connections. Nothing here reaches the network."""

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from catalog.models import Resource
from core import connections, jobs, worker
from core.graph import Reply
from core.models import AuditEvent, Job, JobStatus, Schedule
from core.problems import Conflict
from discovery import access, entra, entra_simulator, graph_source, missing
from discovery import engine as discovery
from discovery.graph_source import RUN_DEADLINE
from discovery.models import Connector, ConnectorRun, Observation, RunStatus
from tests.conftest import drain_jobs, make_org, no_digest


def make_connector(org, actor, name="Entra", **scope):
    connector = discovery.create_connector(
        org,
        actor,
        {"name": name, "tenant_id": "simulated", "client_id": "", "secret": "synthetic-secret", **scope},
    )
    connections.run_test(org, actor, connector.microsoft_connection)
    return Connector.objects.select_related("organization").get(pk=connector.pk)


def configure(connector, **config):
    connector.config = {**connector.config, **config}
    connector.save(update_fields=["config"])
    return connector


@pytest.fixture
def entra_connector(a):
    return make_connector(a["org"], a["users"]["admin"])


# ---- runs are jobs with leases ----------------------------------------------------------------------------------------


def test_run_now_queues_one_job_that_carries_the_connector_id_only(a, entra_connector):
    org, admin = a["org"], a["users"]["admin"]
    queued = discovery.request_run(org, admin, entra_connector)
    assert queued.status == RunStatus.QUEUED and queued.started_at is None and queued.queued_at is not None
    job = Job.objects.get(id=queued.job_id)
    assert (job.kind, job.organization_id, job.payload) == (
        "discovery.run",
        org.id,
        {"connector_id": str(entra_connector.id)},
    )
    assert queued.lease_token is None  # the lease starts when the job starts the run

    claimed = jobs.claim()
    assert claimed.id == job.id and claimed.lease_expires - timezone.now() > timedelta(seconds=RUN_DEADLINE)
    assert jobs.run(claimed) == JobStatus.SUCCEEDED
    run = ConnectorRun.objects.get(id=queued.id)
    assert (run.status, run.trigger, run.requested_by) == (RunStatus.SUCCEEDED, "manual", admin)
    assert run.started_at and run.queued_at and run.finished_at
    assert run.started_at >= run.queued_at and run.finished_at >= run.started_at
    assert run.lease_token is None and run.lease_expires is None and run.counts["new"] > 0
    job.refresh_from_db()
    assert job.result == {"run": str(run.id), "status": "succeeded"}  # never the source's counts or a value


def test_the_job_kind_runs_in_the_long_lane_with_a_lease_past_the_run_deadline():
    kind = jobs.JOB_KINDS[discovery.RUN_JOB]
    assert kind.func is discovery.run_job and kind.lane == jobs.LONG_LANE and kind.max_attempts == 1
    assert kind.lease == discovery.RUN_LEASE > timedelta(seconds=RUN_DEADLINE) + timedelta(minutes=5)


def test_the_schedule_only_queues_and_never_fetches(a, monkeypatch):
    org, admin = a["org"], a["users"]["admin"]
    connector = make_connector(org, admin, schedule="daily")
    other = make_connector(org, admin, name="Second", schedule="daily")

    real = entra.fetch

    def fetch(*args, **kwargs):
        raise AssertionError("the schedule must not run a connector inline")

    monkeypatch.setattr(entra, "fetch", fetch)
    at_three = datetime(2026, 11, 3, 3, 0, tzinfo=UTC)  # after 02:00 in the organization's timezone (UTC)
    assert discovery.scheduled_runs(at_three) == {"connector_runs_queued": 2}
    assert discovery.scheduled_runs(at_three + timedelta(minutes=1)) == {"connector_runs_queued": 0}
    runs = ConnectorRun.objects.filter(status=RunStatus.QUEUED).order_by("connector__name")
    assert [(r.connector_id, r.trigger) for r in runs] == [(connector.id, "scheduled"), (other.id, "scheduled")]
    assert sorted(j.payload["connector_id"] for j in Job.objects.filter(kind="discovery.run")) == sorted(
        [str(connector.id), str(other.id)]
    )
    monkeypatch.setattr(entra, "fetch", real)
    assert len(drain_jobs()) == 2
    assert ConnectorRun.objects.filter(status=RunStatus.SUCCEEDED, trigger="scheduled").count() == 2


def test_a_legacy_run_request_is_queued_by_the_schedule(a, entra_connector):
    """A connector with `run_requested_at` (Run now from an earlier version) is queued once, by whom asked."""
    Connector.objects.filter(pk=entra_connector.pk).update(
        run_requested_at=timezone.now(), run_requested_by=a["users"]["admin"]
    )
    assert discovery.due_runs() == 1 and discovery.due_runs() == 0
    run = ConnectorRun.objects.get()
    assert (run.status, run.requested_by) == (RunStatus.QUEUED, a["users"]["admin"])
    assert Connector.objects.get(pk=entra_connector.pk).run_requested_at is None


def test_progress_renews_the_run_and_the_job_lease(a, entra_connector, monkeypatch):
    monkeypatch.setattr(discovery, "RENEW_EVERY", timedelta(0))
    real = entra.fetch
    seen: list = []

    def fetch(connector, credential, transport=None):
        run = ConnectorRun.objects.get(connector=connector, status=RunStatus.RUNNING)
        assert run.job_id is not None
        job = Job.objects.get(id=run.job_id)
        graph_source.at_checkpoint()  # as every Graph request does
        seen.append(
            (
                run.lease_expires,
                ConnectorRun.objects.get(id=run.id).lease_expires,
                job.lease_expires,
                Job.objects.get(id=job.id).lease_expires,
            )
        )
        return real(connector, credential, transport)

    monkeypatch.setattr(entra, "fetch", fetch)
    discovery.request_run(a["org"], a["users"]["admin"], entra_connector)
    drain_jobs()
    (run_before, run_after, job_before, job_after) = seen[0]
    assert run_after > run_before and job_after > job_before
    assert ConnectorRun.objects.get().status == RunStatus.SUCCEEDED


def test_a_run_that_lost_its_lease_stops_at_its_next_request(a, entra_connector, monkeypatch):
    real = entra.fetch
    calls: list = []

    def fetch(connector, credential, transport=None):
        # The sweep took the run over meanwhile (its lease ran out while this worker stalled).
        ConnectorRun.objects.filter(connector=connector).update(status=RunStatus.FAILED, lease_token=None)
        simulator = entra_simulator.Transport(connector)
        calls.append(simulator)
        return real(connector, credential, simulator)

    monkeypatch.setattr(discovery, "RENEW_EVERY", timedelta(0))
    monkeypatch.setattr(entra, "fetch", fetch)
    run = discovery.run(entra_connector)
    assert calls[0].calls == []  # stopped before its first request, token included
    assert run.status == RunStatus.FAILED and not Observation.objects.exists()


def test_an_expired_lease_fails_the_run_and_a_late_finisher_never_overwrites_the_newer_run(
    a, entra_connector, monkeypatch
):
    real = entra.fetch
    state: dict = {}

    def fetch(connector, credential, transport=None):
        if state:
            return real(connector, credential, transport)
        state["first"] = ConnectorRun.objects.get(connector=connector, status=RunStatus.RUNNING)
        # This worker stalls past the lease: the sweep fails the run, and a second run starts and finishes.
        later = timezone.now() + discovery.RUN_LEASE + timedelta(minutes=1)
        assert discovery.fail_stuck(later) == 1
        state["second"] = discovery.run(connector, now=later)
        # When the first finally answers, its tenant looks empty: applied, it would mark everything gone.
        return graph_source.Fetched([])

    monkeypatch.setattr(entra, "fetch", fetch)
    first = discovery.run(entra_connector)
    second = ConnectorRun.objects.get(id=state["second"].id)
    assert first.id == state["first"].id and first.status == RunStatus.FAILED
    assert "lease ran out" in first.error and "Nothing it read was applied" in first.error
    assert second.status == RunStatus.SUCCEEDED and second.counts["new"] > 0
    assert Observation.objects.filter(gone_at__isnull=False).count() == 0  # the late result was discarded
    assert ConnectorRun.objects.get(id=first.id).error == first.error  # and wrote nothing over its own row


def test_a_late_outcome_is_compare_and_set_on_the_lease_token(a, entra_connector):
    run = discovery.run(entra_connector)
    stale = ConnectorRun.objects.get(id=run.id)
    ConnectorRun.objects.filter(id=run.id).update(status=RunStatus.RUNNING, lease_token=uuid.uuid4())  # another's
    stale.status = RunStatus.RUNNING
    assert discovery._finish(stale, RunStatus.FAILED, error="late") is False
    assert ConnectorRun.objects.get(id=run.id).error == ""


def test_the_sweep_fails_a_queued_run_whose_job_ended_and_a_pre_lease_run_after_an_hour(a, entra_connector):
    org, admin = a["org"], a["users"]["admin"]
    queued = discovery.request_run(org, admin, entra_connector)
    assert discovery.fail_stuck() == 0  # its job is still queued
    Job.objects.filter(id=queued.job_id).update(status=JobStatus.ABANDONED)
    assert discovery.fail_stuck() == 1
    failed = ConnectorRun.objects.get(id=queued.id)
    assert failed.status == RunStatus.FAILED and "job ended without starting it" in failed.error
    assert discovery.request_run(org, admin, entra_connector).status == RunStatus.QUEUED  # a new one can start
    ConnectorRun.objects.filter(status=RunStatus.QUEUED).delete()

    old = ConnectorRun.objects.create(
        organization=org, connector=entra_connector, trigger="manual", started_at=timezone.now() - timedelta(minutes=30)
    )  # a row from before leases: an hour, as before
    assert discovery.fail_stuck() == 0 and discovery.run(entra_connector) is None
    assert discovery.fail_stuck(timezone.now() + timedelta(minutes=31)) == 1
    assert "worker stopped during this run" in ConnectorRun.objects.get(id=old.id).error


def test_a_job_whose_run_was_failed_meanwhile_does_nothing(a, entra_connector):
    queued = discovery.request_run(a["org"], a["users"]["admin"], entra_connector)
    ConnectorRun.objects.filter(id=queued.id).update(status=RunStatus.FAILED)
    drain_jobs()
    assert Job.objects.get(id=queued.job_id).result == {"skipped": "run_not_queued"}
    assert not Observation.objects.exists()


def test_one_queued_or_running_run_per_connector(a, entra_connector):
    org, admin = a["org"], a["users"]["admin"]
    discovery.request_run(org, admin, entra_connector)
    with pytest.raises(Conflict, match="already queued or in progress"):
        discovery.request_run(org, admin, entra_connector)
    assert discovery.run(entra_connector) is None
    assert discovery.queue_run(entra_connector, "scheduled") is None
    assert Job.objects.filter(kind="discovery.run").count() == 1
    with pytest.raises(IntegrityError), transaction.atomic():  # the database is the backstop
        ConnectorRun.objects.create(organization=org, connector=entra_connector, trigger="manual")


def test_two_run_nows_at_once_queue_one_run(transactional_db):
    from tests.test_concurrency import run_together

    f = make_org("racing")
    connector = make_connector(f["org"], f["users"]["admin"])
    outcomes: list = []

    def press():
        outcomes.append(discovery.request_run(f["org"], f["users"]["admin"], Connector.objects.get(pk=connector.pk)))

    errors = run_together(press, press)
    assert sorted(type(e).__name__ for e in errors if e) == ["Conflict"] and len(outcomes) == 1
    assert ConnectorRun.objects.filter(connector=connector).count() == 1


def test_a_secret_in_a_source_error_is_redacted_before_it_is_stored_or_logged(a, entra_connector, monkeypatch, caplog):
    secret = "synthetic-secret"

    def fetch(connector, credential, transport=None):
        raise graph_source.SourceError(f"Microsoft said: bad secret {credential.secret}")

    monkeypatch.setattr(entra, "fetch", fetch)
    run = discovery.run(entra_connector)
    assert run.status == RunStatus.FAILED and "[redacted]" in run.error and secret not in run.error
    assert secret not in caplog.text

    def boom(connector, credential, transport=None):
        raise RuntimeError(f"unexpected {credential.secret}")

    monkeypatch.setattr(entra, "fetch", boom)
    caplog.clear()
    run = discovery.run(entra_connector)
    assert run.error == "Unexpected error (RuntimeError). See the worker log."
    assert secret not in caplog.text and any("unexpected error" in r.getMessage() for r in caplog.records)
    assert any("type=RuntimeError" in r.getMessage() and "test_runs.py" in r.getMessage() for r in caplog.records)
    assert all(secret not in json.dumps(getattr(r, "error", "")) for r in caplog.records)


# ---- the worker: long jobs beside the tick, fair between organizations -------------------------------------------------


@pytest.fixture
def kinds(monkeypatch):
    monkeypatch.setattr(jobs, "JOB_KINDS", dict(jobs.JOB_KINDS))

    def register(kind, func, **options):
        jobs.register_job(kind, func, **options)
        return kind

    return register


def test_the_long_lane_claims_an_organization_with_nothing_running_first(a, b, kinds):
    long_kind = kinds("test.long", lambda job, ctx: None, lane=jobs.LONG_LANE)
    kinds("test.short", lambda job, ctx: None)
    busy = jobs.enqueue(long_kind, organization=a["org"])
    Job.objects.filter(id=busy.id).update(status=JobStatus.RUNNING, lease_expires=timezone.now() + timedelta(hours=1))
    older = jobs.enqueue(long_kind, organization=a["org"], run_after=timezone.now() - timedelta(minutes=5))
    newer = jobs.enqueue(long_kind, organization=b["org"])
    short = jobs.enqueue("test.short")
    assert jobs.claim(lane=jobs.DEFAULT_LANE).id == short.id  # the tick's own claim never takes a long job
    assert jobs.claim(lane=jobs.DEFAULT_LANE) is None
    assert jobs.claim(lane=jobs.LONG_LANE).id == newer.id  # b has none running; a has one
    assert jobs.claim(lane=jobs.LONG_LANE).id == older.id
    assert jobs.claim(lane=jobs.LONG_LANE) is None


def test_a_slow_long_job_never_holds_up_the_tick_or_other_organizations(transactional_db, kinds, monkeypatch):
    """A slow tenant's run (here a job that waits) runs in the lane's thread: the tick returns, fires its schedule and
    runs short jobs, the next tick does again, and another organization's long job takes the second slot."""
    release = threading.Event()
    started: list = []

    def slow(job, ctx):
        started.append(job.organization_id)
        release.wait(20)
        return {"slow": True}

    long_kind = kinds("test.slow", slow, lane=jobs.LONG_LANE)
    kinds("test.short", lambda job, ctx: {"ok": True})
    monkeypatch.setattr(jobs, "SCHEDULES", {})
    jobs.register_schedule("test.every", lambda now: {"fired": 1}, "* * * * *", "Every minute")
    one, two = make_org("slowone"), make_org("slowtwo")
    first = jobs.enqueue(long_kind, organization=one["org"])
    second_same = jobs.enqueue(long_kind, organization=one["org"])
    other = jobs.enqueue(long_kind, organization=two["org"])
    quick = jobs.enqueue("test.short")
    lane = worker.LongLane(2)
    try:
        summary = worker.tick(lanes=lane)
        assert summary["fired"] == 1 and summary["jobs"] == {"succeeded": 1} and summary["long_jobs_started"] == 2
        assert Job.objects.get(id=quick.id).status == JobStatus.SUCCEEDED
        statuses = {j.id: j.status for j in Job.objects.filter(kind=long_kind)}
        # one organization's second run waits; the other organization's run took the second slot
        assert statuses == {first.id: JobStatus.RUNNING, other.id: JobStatus.RUNNING, second_same.id: JobStatus.QUEUED}
        later = jobs.enqueue("test.short")
        Schedule.objects.filter(name="test.every").update(next_due=timezone.now())
        summary = worker.tick(lanes=lane)  # the next tick isn't held up either
        assert summary["fired"] == 1 and summary["long_jobs_started"] == 0
        assert Job.objects.get(id=later.id).status == JobStatus.SUCCEEDED
    finally:
        release.set()
        assert lane.wait(20) == 0
    assert {j.status for j in Job.objects.filter(id__in=[first.id, other.id])} == {JobStatus.SUCCEEDED}
    worker.tick(lanes=lane)
    assert lane.wait(20) == 0
    assert Job.objects.get(id=second_same.id).status == JobStatus.SUCCEEDED


def test_discovery_runs_go_through_the_long_lane(transactional_db):
    f = make_org("lanes")
    no_digest()  # the digest is a short job of its own; this test counts the tick's
    connector = make_connector(f["org"], f["users"]["admin"])
    discovery.request_run(f["org"], f["users"]["admin"], connector)
    lane = worker.LongLane(1)
    summary = worker.tick(lanes=lane)
    assert summary["long_jobs_started"] == 1 and summary["jobs"] == {}  # not run inline by the tick
    assert lane.wait(60) == 0
    run = ConnectorRun.objects.get(connector=connector)
    assert run.status == RunStatus.SUCCEEDED, run.error


# ---- Run now over the API ---------------------------------------------------------------------------------------------


def test_run_now_answers_202_with_the_queued_run(a, b, api, entra_connector):
    admin = api(a["users"]["admin"])
    r = admin.post(f"connectors/{entra_connector.id}/run")
    assert r.status_code == 202
    active = r.json_body["active_run"]
    assert (
        active["status"] == "queued"
        and active["started_at"] is None
        and active["requested_by"]["id"] == a["users"]["admin"].id
    )
    assert r.json_body["run_requested"] and r.json_body["last_run"] is None  # last_run is the last that started
    assert r["Location"] == f"http://testserver/api/v1/connectors/{entra_connector.id}/runs/{active['id']}"
    location = r["Location"].removeprefix("http://testserver/api/v1/")
    assert api(a["users"]["viewer"]).get(location).json_body["status"] == "queued"
    assert Job.objects.get(id=active["job_id"]).payload == {"connector_id": str(entra_connector.id)}
    for role in ("editor", "viewer"):
        assert api(a["users"][role]).post(f"connectors/{entra_connector.id}/run").status_code == 403
    assert api(b["users"]["admin"]).post(f"connectors/{entra_connector.id}/run").status_code == 404
    drain_jobs()
    body = admin.get(f"connectors/{entra_connector.id}").json_body
    assert body["active_run"] is None and not body["run_requested"] and body["last_run"]["status"] == "succeeded"
    assert body["last_run"]["duration_seconds"] is not None


# ---- run history ------------------------------------------------------------------------------------------------------


@pytest.fixture
def history(a, b):
    org, admin = a["org"], a["users"]["admin"]
    entra_c = make_connector(org, admin)
    other = make_connector(org, admin, name="Second")
    ok = discovery.run(entra_c, requested_by=admin)
    configure(entra_c, simulated_permissions=[])  # Application.Read.All refused: the run fails readably
    failed = discovery.run(entra_c, "scheduled")
    queued = discovery.request_run(org, admin, other)
    theirs = discovery.run(make_connector(b["org"], b["users"]["admin"]))
    return {"entra": entra_c, "other": other, "ok": ok, "failed": failed, "queued": queued, "theirs": theirs}


def ids(body):
    return [r["id"] for r in body["results"]]


def test_history_lists_every_run_of_the_organization_newest_first(a, api, history):
    body = api(a["users"]["viewer"]).get("discovery/runs").json_body
    assert ids(body) == [str(history[k].id) for k in ("queued", "failed", "ok")]  # never another organization's
    queued, failed, ok = body["results"]
    assert queued["status"] == "queued" and queued["started_at"] is None and queued["duration_seconds"] is None
    assert queued["connector"] == {"id": str(history["other"].id), "name": "Second", "source": "entra"}
    assert failed["trigger"] == "scheduled" and failed["requested_by"] is None
    assert "Application.Read.All" in failed["error"] and failed["skipped"] == []
    assert ok["trigger"] == "manual" and ok["requested_by"]["id"] == a["users"]["admin"].id
    assert ok["duration_seconds"] >= 0 and ok["counts"]["new"] == ok["counts"]["kept"] > 0
    labels = {s["filter"]: (s["label"], s["count"]) for s in ok["skipped"]}
    assert (
        labels["microsoft"][0] == entra.FILTERS["microsoft"]
        and labels["microsoft"][1] == ok["counts"]["skipped"]["microsoft"]
    )
    assert [s["count"] for s in ok["skipped"]] == sorted((s["count"] for s in ok["skipped"]), reverse=True)
    assert isinstance(ok["notes"], list)


def test_history_filters(a, api, history):
    viewer = api(a["users"]["viewer"])
    assert ids(viewer.get("discovery/runs?status=failed").json_body) == [str(history["failed"].id)]
    assert ids(viewer.get("discovery/runs?status=queued,succeeded").json_body) == [
        str(history["queued"].id),
        str(history["ok"].id),
    ]
    assert ids(viewer.get("discovery/runs?trigger=scheduled").json_body) == [str(history["failed"].id)]
    assert ids(viewer.get(f"discovery/runs?connector={history['other'].id}").json_body) == [str(history["queued"].id)]
    today = timezone.now().date()
    assert len(viewer.get(f"discovery/runs?from={today}&to={today}").json_body["results"]) == 3
    assert viewer.get(f"discovery/runs?from={today + timedelta(days=1)}").json_body["results"] == []
    assert viewer.get(f"discovery/runs?to={today - timedelta(days=1)}").json_body["results"] == []
    per_connector = viewer.get(f"connectors/{history['entra'].id}/runs?status=succeeded").json_body
    assert [r["id"] for r in per_connector] == [str(history["ok"].id)]  # the connector's array, filtered too
    for bad in ("status=done", "trigger=e2e", "from=yesterday", "to=2026-13-01", "connector=nope", "colour=red"):
        r = viewer.get(f"discovery/runs?{bad}")
        assert r.status_code == 400, bad


def test_history_pages_with_cursors(a, api, history):
    viewer = api(a["users"]["viewer"])
    first = viewer.get("discovery/runs?page_size=2").json_body
    assert len(first["results"]) == 2 and first["next"]
    rest = viewer.get(f"discovery/runs?page_size=2&cursor={first['next']}").json_body
    assert ids(first) + ids(rest) == [str(history[k].id) for k in ("queued", "failed", "ok")] and rest["next"] is None


def test_history_tenancy_and_roles(a, b, api, history):
    assert api(b["users"]["admin"]).get(f"discovery/runs?connector={history['entra'].id}").status_code == 404
    assert ids(api(b["users"]["viewer"]).get("discovery/runs").json_body) == [str(history["theirs"].id)]
    theirs = f"connectors/{history['theirs'].connector_id}/runs/{history['theirs'].id}"
    assert api(a["users"]["admin"]).get(theirs).status_code == 404
    for role in ("viewer", "editor", "admin"):
        assert api(a["users"][role]).get("discovery/runs").status_code == 200
    assert api().get("discovery/runs").status_code in (401, 403)
    assert api(a["users"]["admin"]).post("discovery/runs").status_code == 405


def test_one_run_by_its_own_id_for_every_role_of_its_organization_only(a, b, api, history):
    """GET discovery/runs/{id}, the run page's address without the connector."""
    for role in ("viewer", "editor", "admin"):
        r = api(a["users"][role]).get(f"discovery/runs/{history['failed'].id}")
        assert r.status_code == 200, role
        assert r.json_body["id"] == str(history["failed"].id) and r.json_body["status"] == "failed"
        assert r.json_body["connector"]["id"] == str(history["entra"].id)
    same = api(a["users"]["viewer"]).get(f"connectors/{history['entra'].id}/runs/{history['failed'].id}").json_body
    assert api(a["users"]["viewer"]).get(f"discovery/runs/{history['failed'].id}").json_body == same
    r = api(a["users"]["admin"]).get(f"discovery/runs/{history['theirs'].id}")
    assert r.status_code == 404 and r["Content-Type"] == "application/problem+json"
    assert api(b["users"]["viewer"]).get(f"discovery/runs/{history['theirs'].id}").status_code == 200
    assert api(a["users"]["admin"]).get(f"discovery/runs/{uuid.uuid4()}").status_code == 404
    assert api().get(f"discovery/runs/{history['ok'].id}").status_code in (401, 403)
    assert api(a["users"]["admin"]).patch(f"discovery/runs/{history['ok'].id}", {}).status_code == 405


# ---- the live marker --------------------------------------------------------------------------------------------------


def test_live_tests_are_skipped_without_the_flag_and_the_credentials(pytestconfig, monkeypatch):
    from tests import conftest

    assert any(line.startswith("live:") for line in pytestconfig.getini("markers"))
    for name in conftest.LIVE_REQUIRED:
        monkeypatch.delenv(name, raising=False)
    assert "--live" in conftest.live_skip_reason(live_flag=False)
    assert conftest.LIVE_REQUIRED[0] in conftest.live_skip_reason(live_flag=True)
    for name in conftest.LIVE_REQUIRED:
        monkeypatch.setenv(name, "x")
    assert conftest.live_skip_reason(live_flag=True) == ""
    assert "--live" in conftest.live_skip_reason(live_flag=False)  # credentials alone never run them


# ---- deferred items ---------------------------------------------------------------------------------------------------


class ConsentRefused:
    """Microsoft refuses the token: nobody consented to the app in this tenant (AADSTS65001)."""

    def send(self, method, url, headers, body=None):
        description = "AADSTS65001: The user or administrator has not consented to use the application."
        return Reply(400, {}, json.dumps({"error": "invalid_grant", "error_description": description}).encode())


def test_a_consent_refusal_at_sign_in_lists_no_empty_permission_as_missing(a, entra_connector):
    result = access.evaluate(entra_connector, transport=ConsentRefused())
    sign_in = next(s for s in result["steps"] if s["name"] == "Sign in to Microsoft Graph")
    assert sign_in["state"] == "failed" and sign_in["problem"] == "missing_permission" and sign_in["permission"] == ""
    assert not result["ok"] and result["missing"] == [] and "" not in result["optional_missing"]


def test_refused_agent_identities_are_not_read_so_linked_agents_never_go_missing(a, entra_connector):
    org, admin, editor = a["org"], a["users"]["admin"], a["users"]["editor"]
    discovery.update_connector(org, admin, entra_connector, {"scope": {"include_agent_identities": True}})
    connector = Connector.objects.get(pk=entra_connector.pk)
    assert discovery.run(connector).status == RunStatus.SUCCEEDED
    agents = Observation.objects.filter(connector=connector, data__agent="agent_identity", gone_at__isnull=True)
    agent = agents.first()
    assert agent is not None
    discovery.create_record(org, editor, agent, {})
    granted = [p for p in entra_simulator.GRANTED if p != "AgentIdentity.Read.All"]
    run = discovery.run(configure(connector, simulated_permissions=granted))
    assert run.status == RunStatus.SUCCEEDED and run.counts["not_read"] == agents.count() and run.counts["gone"] == 0
    assert any("AgentIdentity.Read.All" in n for n in run.counts["notes"])
    assert not Observation.objects.filter(
        connector=connector, data__agent="agent_identity", gone_at__isnull=False
    ).exists()
    assert not Observation.objects.filter(organization=org).filter(discovery.missing_q()).exists()
    run = discovery.run(configure(connector, simulated_permissions=list(entra_simulator.GRANTED)))
    assert run.counts["gone"] == 0 and "not_read" not in run.counts  # read again: unchanged


def test_archiving_is_refused_while_another_observation_still_lists_the_record_out_of_scope(a, entra_connector):
    org, editor = a["org"], a["users"]["editor"]
    discovery.run(entra_connector)
    hub_obs = Observation.objects.get(connector=entra_connector, display_name="Expense Hub")
    _, hub = discovery.create_record(org, editor, hub_obs, {})
    daemon = Observation.objects.get(connector=entra_connector, display_name="Payroll Sync Daemon")
    discovery.link(org, editor, daemon, {"resource": str(hub.id)})
    configure(
        entra_connector,
        simulator_changes=[{"op": "remove", "id": "Expense Hub"}, {"op": "disable", "id": "Payroll Sync Daemon"}],
    )
    discovery.run(entra_connector)
    assert Observation.objects.get(id=daemon.id).out_of_scope == "disabled"
    with pytest.raises(Conflict) as refused:
        missing.archive(org, editor, Observation.objects.get(id=hub_obs.id), {})
    assert "still lists Expense Hub" in str(refused.value) and "out of this connector's scope" in str(refused.value)
    assert Resource.objects.get(id=hub.id).archived_at is None


def test_the_simulated_tenant_stays_the_default_transport(entra_connector):
    """The simulated tenant: a connection whose tenant is `simulated` never builds a network transport."""
    credential = connections.credential(entra_connector.microsoft_connection)
    assert isinstance(graph_source.graph_transport(entra_connector, credential), entra_simulator.Transport)


# ---- the Playwright helper (api/e2e_views.run_connectors) ---------------------------------------------------------------
# Routed only for Playwright's _e2e database; this module's URLconf routes it for the test below.


def _urlpatterns():
    from django.urls import include, path

    from api import e2e_views

    return [path("api/v1/e2e/run-connectors", e2e_views.run_connectors), path("api/v1/", include("api.urls"))]


urlpatterns = _urlpatterns()


@pytest.mark.urls("tests.test_runs")
def test_the_e2e_helper_runs_the_queued_runs_through_their_jobs(a, b, api, entra_connector):
    admin = api(a["users"]["admin"])
    assert admin.post(f"connectors/{entra_connector.id}/run").status_code == 202
    theirs = make_connector(b["org"], b["users"]["admin"])
    discovery.request_run(b["org"], b["users"]["admin"], theirs)
    ran = admin.post("e2e/run-connectors").json_body
    assert [r["connector"] for r in ran] == [str(entra_connector.id)] and ran[0]["status"] == "succeeded"
    run = ConnectorRun.objects.get(connector=entra_connector)
    assert (run.trigger, run.requested_by) == ("manual", a["users"]["admin"])
    assert ConnectorRun.objects.get(connector=theirs).status == RunStatus.QUEUED  # never another organization's
    assert api(a["users"]["editor"]).post("e2e/run-connectors").status_code == 403


# ---- a connector turned off, a stopping worker, the lane size, the clock ----------------------------------------------


def test_turning_a_connector_off_fails_its_queued_run_in_the_same_audited_change(a, api, entra_connector):
    org, admin = a["org"], a["users"]["admin"]
    queued = discovery.request_run(org, admin, entra_connector)
    assert api(admin).patch(f"connectors/{entra_connector.id}", {"enabled": False}).status_code == 200
    run = ConnectorRun.objects.get(id=queued.id)
    assert run.status == RunStatus.FAILED and run.error == discovery.TURNED_OFF and run.started_at is None
    event = AuditEvent.objects.filter(action="connector.updated", target_id=entra_connector.id).latest("created_at")
    assert event.changes["enabled"] == {"before": True, "after": False}
    drain_jobs()  # its job reads nothing
    assert Job.objects.get(id=queued.job_id).result == {"skipped": "connector_off"}
    assert not Observation.objects.exists()
    body = api(a["users"]["viewer"]).get(f"connectors/{entra_connector.id}").json_body
    assert body["last_run"]["id"] == str(queued.id) and body["last_run"]["status"] == "failed"  # failed before start
    assert body["active_run"] is None and not body["run_requested"]
    api(admin).patch(f"connectors/{entra_connector.id}", {"enabled": True})
    assert api(admin).post(f"connectors/{entra_connector.id}/run").status_code == 202  # not locked


def test_a_job_whose_connector_was_turned_off_meanwhile_reads_nothing(a, entra_connector, monkeypatch):
    queued = discovery.request_run(a["org"], a["users"]["admin"], entra_connector)
    Connector.objects.filter(pk=entra_connector.pk).update(enabled=False)  # any path that skipped the service

    def fetch(*args, **kwargs):
        raise AssertionError("a connector that is off is never read")

    monkeypatch.setattr(entra, "fetch", fetch)
    drain_jobs()
    run = ConnectorRun.objects.get(id=queued.id)
    assert (run.status, run.error) == (RunStatus.FAILED, discovery.TURNED_OFF)
    assert Job.objects.get(id=queued.job_id).result == {"skipped": "connector_off"}
    assert not ConnectorRun.objects.filter(status__in=["queued", "running"]).exists()


@pytest.mark.urls("tests.test_runs")
def test_the_e2e_helper_fails_a_turned_off_connectors_queued_run(a, api, entra_connector):
    admin = api(a["users"]["admin"])
    assert admin.post(f"connectors/{entra_connector.id}/run").status_code == 202
    Connector.objects.filter(pk=entra_connector.pk).update(enabled=False)
    ran = admin.post("e2e/run-connectors").json_body
    assert [r["status"] for r in ran] == ["failed"]
    assert ConnectorRun.objects.get(connector=entra_connector).error == discovery.TURNED_OFF
    Connector.objects.filter(pk=entra_connector.pk).update(enabled=True)
    assert admin.post(f"connectors/{entra_connector.id}/run").status_code == 202


def test_a_stopping_worker_fails_its_running_run_and_releases_the_connector(transactional_db, monkeypatch):
    f = make_org("stopping")
    no_digest()
    connector = make_connector(f["org"], f["users"]["admin"])
    reading, real = threading.Event(), entra.fetch
    lane = worker.LongLane(1)

    def fetch(c, credential, transport=None):
        reading.set()
        assert lane.stopping.wait(20)  # the worker got SIGTERM while this run was reading
        return real(c, credential, transport)  # its next request sees it

    monkeypatch.setattr(entra, "fetch", fetch)
    queued = discovery.request_run(f["org"], f["users"]["admin"], connector)
    assert worker.tick(lanes=lane)["long_jobs_started"] == 1
    assert reading.wait(20)
    lane.stop()
    assert lane.wait(20) == 0
    run = ConnectorRun.objects.get(id=queued.id)
    assert (run.status, run.error, run.lease_token) == (RunStatus.FAILED, discovery.WORKER_STOPPED, None)
    assert Job.objects.get(id=queued.job_id).status == JobStatus.SUCCEEDED
    assert not ConnectorRun.objects.filter(status__in=["queued", "running"]).exists()
    assert not Observation.objects.exists()
    assert lane.fill() == 0  # a stopping lane claims nothing more
    monkeypatch.setattr(entra, "fetch", real)
    assert discovery.request_run(f["org"], f["users"]["admin"], connector).status == RunStatus.QUEUED


@pytest.mark.parametrize("value", ["0", "-1", "two"])
def test_the_long_lane_size_below_one_is_refused_at_start_up(db, monkeypatch, value):
    with pytest.raises(CommandError, match="1 or more"):
        call_command("run_worker", "--once", "--long-jobs", value)
    monkeypatch.setenv("SOFTWARDEN_WORKER_LONG_JOBS", value)
    with pytest.raises(CommandError, match="SOFTWARDEN_WORKER_LONG_JOBS"):
        call_command("run_worker", "--once")
    with pytest.raises(worker.LaneSizeError):
        worker.LongLane(0)


def test_a_long_run_that_keeps_renewing_is_never_failed_as_stuck(a, entra_connector):
    long_ago = timezone.now() - timedelta(hours=2)
    run = ConnectorRun.objects.create(
        organization=a["org"],
        connector=entra_connector,
        trigger="manual",
        started_at=long_ago,
        lease_token=uuid.uuid4(),
        lease_expires=timezone.now() + timedelta(minutes=30),
    )
    assert discovery.fail_stuck() == 0
    assert discovery.run(entra_connector) is None and discovery.queue_run(entra_connector, "manual") is None
    assert ConnectorRun.objects.get(id=run.id).status == RunStatus.RUNNING


def test_the_lane_wait_counts_what_still_runs_and_threads_close_their_connections(kinds, monkeypatch):
    release = threading.Event()
    closed: list[str] = []

    class Connection:
        def close(self):
            closed.append(threading.current_thread().name)

    monkeypatch.setattr(worker, "connection", Connection())
    monkeypatch.setattr(jobs, "run", lambda job, now=None, should_stop=None: release.wait(20))
    lane = worker.LongLane(2)
    claimed = iter([type("J", (), {"id": 1, "kind": "test.a"})(), type("J", (), {"id": 2, "kind": "test.b"})(), None])
    monkeypatch.setattr(jobs, "claim", lambda now=None, lane=None, ids=None: next(claimed))
    assert lane.fill() == 2
    assert lane.wait(0.2) == 2  # both still running after the grace
    release.set()
    assert lane.wait(20) == 0
    assert sorted(closed) == ["job-test.a", "job-test.b"]


def test_an_in_process_run_with_an_injected_clock_finishes_on_that_clock(entra_connector):
    then = timezone.now() - timedelta(days=3)
    run = discovery.run(entra_connector, now=then)
    assert run.started_at == then and run.finished_at is not None
    assert timedelta(0) <= run.finished_at - then < timedelta(minutes=1)


def test_run_history_is_pruned_to_the_newest_runs_per_connector(a, entra_connector, monkeypatch):
    # A daily schedule keeps the newest KEEP_RUNS finished runs per connector, every active run and the newest
    # successful one; another connector keeps its own.
    monkeypatch.setattr(discovery, "KEEP_RUNS", 3)
    org, admin = a["org"], a["users"]["admin"]
    other = make_connector(org, admin, name="Second")
    start = timezone.now() - timedelta(days=30)

    def made(connector, n, status):
        run = ConnectorRun.objects.create(organization=org, connector=connector, trigger="scheduled", status=status)
        ConnectorRun.objects.filter(id=run.id).update(
            created_at=start + timedelta(days=n), finished_at=start + timedelta(days=n)
        )
        return run.id

    succeeded = made(entra_connector, 0, RunStatus.SUCCEEDED)
    failed = [made(entra_connector, n, RunStatus.FAILED) for n in range(1, 7)]
    running = made(entra_connector, 7, RunStatus.RUNNING)
    others = [made(other, n, RunStatus.FAILED) for n in range(3)]
    assert discovery.prune_runs() == {"connector_runs_pruned": 3}
    kept = set(ConnectorRun.objects.values_list("id", flat=True))
    assert kept == {succeeded, *failed[3:], running, *others}
    assert discovery.prune_runs() == {"connector_runs_pruned": 0}
    assert "discovery.runs_prune" in jobs.SCHEDULES
