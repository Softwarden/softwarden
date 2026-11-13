"""Jobs, schedules and the worker tick: the worker picks up a job within one tick and
abandons a job whose lease expired. Threaded tests use real separate database connections."""

import ast
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.utils import timezone

from core import cron, jobs, worker
from core.models import Job, JobStatus, Schedule, WorkerHeartbeat
from tests.conftest import TICK_MOMENTS, quiet_schedules
from tests.test_concurrency import run_together

# ---- cron ------------------------------------------------------------------------------------------------------------


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@pytest.mark.parametrize(
    "expression,moment,following",
    [
        ("* * * * *", "2026-11-04 10:00:30", "2026-11-04 10:01"),
        ("40 3 * * *", "2026-11-04 10:00", "2026-11-05 03:40"),
        ("*/15 * * * *", "2026-11-04 10:14", "2026-11-04 10:15"),
        ("0 9-17/4 * * *", "2026-11-04 13:00", "2026-11-04 17:00"),
        ("0 0 1 * *", "2026-11-04 00:00", "2026-12-01 00:00"),
        ("30 2 * * 0", "2026-11-04 00:00", "2026-11-08 02:30"),  # Sunday; 4 Nov is a Wednesday
        ("30 2 * * 7", "2026-11-04 00:00", "2026-11-08 02:30"),  # 7 is Sunday too
        # Day of month and day of week both restricted: the 13th or a Friday (6 Nov).
        ("0 12 13 * 5", "2026-11-04 00:00", "2026-11-06 12:00"),
        # Vixie: a field starting with `*` is unrestricted, so these are "Mondays" and "the 1st", not an OR.
        ("0 0 */2 * 1", "2026-11-04 00:00", "2026-11-09 00:00"),
        ("0 0 1 * */2", "2026-11-04 00:00", "2026-12-01 00:00"),
        ("0 0 29 2 *", "2026-11-04 00:00", "2028-02-29 00:00"),
        ("5,10 1 * 1,6 *", "2026-11-04 00:00", "2027-01-01 01:05"),
    ],
)
def test_cron_next_fire(expression, moment, following):
    assert cron.parse(expression).next_after(at(moment)) == at(following)


def fires(expression, moment, count):
    parsed, out, when = cron.parse(expression), [], at(moment)
    for _ in range(count):
        when = parsed.next_after(when)
        out.append(when)
    return out


@pytest.mark.parametrize(
    "expression,moment,expected",
    [
        # A starred field keeps its step: odd days of the month, not every day.
        ("0 0 */2 * *", "2026-11-04 00:00", ["2026-11-05", "2026-11-07", "2026-11-09"]),
        ("0 0 */10 * *", "2026-11-04 00:00", ["2026-11-11", "2026-11-21", "2026-12-01"]),
        # Day of week 0,2,4,6: Sunday, Tuesday, Thursday, Saturday (4 Nov 2026 is a Wednesday).
        ("0 0 * * */2", "2026-11-04 00:00", ["2026-11-05", "2026-11-07", "2026-11-08", "2026-11-10"]),
        # Starred day of month AND Monday: Mondays on odd days only (9 Nov, 23 Nov, 7 Dec).
        ("0 0 */2 * 1", "2026-11-04 00:00", ["2026-11-09", "2026-11-23", "2026-12-07"]),
        # Both restricted: the 13th OR a Friday.
        ("0 0 13 * 5", "2026-11-04 00:00", ["2026-11-06", "2026-11-13", "2026-11-20"]),
    ],
)
def test_cron_fire_sequences(expression, moment, expected):
    assert fires(expression, moment, len(expected)) == [at(f"{day} 00:00") for day in expected]


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "* * * *",
        "60 * * * *",
        "* 24 * * *",
        "0 0 31 2 *",
        "*/0 * * * *",
        "a b c d e",
        "\u00b2 * * * *",
        "*/\u00b2 * * * *",
        "1-\u00b2 * * * *",
    ],
)
def test_cron_refuses_bad_expressions(expression):
    with pytest.raises(cron.CronError):
        cron.parse(expression)


# ---- registry and enqueue --------------------------------------------------------------------------------------------


@pytest.fixture
def kinds(monkeypatch):
    """Register test job kinds for one test only."""
    monkeypatch.setattr(jobs, "JOB_KINDS", dict(jobs.JOB_KINDS))

    def register(kind, func, **options):
        jobs.register_job(kind, func, **options)
        return kind

    return register


@pytest.fixture
def only_schedules(monkeypatch):
    """Replace the registered schedules with the test's own, so ticks don't run the apps' schedules."""
    registry: dict = {}
    monkeypatch.setattr(jobs, "SCHEDULES", registry)
    return registry


def test_enqueue_refuses_unknown_kinds(db):
    with pytest.raises(ValueError, match="Unknown job kind"):
        jobs.enqueue("no.such.kind")
    assert not Job.objects.exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"client_secret": "x"},
        {"connector": {"password": "x"}},
        {"items": [{"id": 1}, {"apiKey": "x"}]},
        {"Authorization": "Bearer x"},
        {"secret_ciphertext": "gAAAA"},
        {"licence_key": "x"},
        {"access_token": "x"},
    ],
)
def test_payloads_never_carry_secrets(db, kinds, payload):
    """Task arguments never carry secrets; a job carries the id of the row that holds the ciphertext."""
    kind = kinds("test.noop", lambda job, ctx: None)
    with pytest.raises(ValueError, match="never carry secrets"):
        jobs.enqueue(kind, payload)
    assert not Job.objects.exists()


def test_payloads_are_small_json_objects(db, kinds):
    kind = kinds("test.noop", lambda job, ctx: None)
    with pytest.raises(ValueError, match="JSON"):
        jobs.enqueue(kind, {"when": datetime.now(UTC)})
    with pytest.raises(ValueError, match="at most"):
        jobs.enqueue(kind, {"rows": "x" * (jobs.PAYLOAD_MAX_BYTES + 1)})
    with pytest.raises(ValueError, match="JSON object"):
        jobs.enqueue(kind, ["connector"])
    job = jobs.enqueue(kind, {"connector_id": "4d7c", "trigger": "manual"})
    assert job.status == JobStatus.QUEUED and job.attempts == 0 and job.max_attempts == 3


def test_a_kind_cannot_be_registered_twice_with_different_functions(kinds):
    kinds("test.once", lambda job, ctx: None)
    with pytest.raises(ValueError, match="already registered"):
        jobs.register_job("test.once", lambda job, ctx: None)


def test_a_schedule_with_a_bad_cron_fails_at_registration(only_schedules):
    with pytest.raises(cron.CronError):
        jobs.register_schedule("test.bad", lambda now: None, "61 * * * *", "Bad")


# ---- claiming and running --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("clock", TICK_MOMENTS, indirect=True)
def test_the_worker_runs_a_job_within_one_tick(a, kinds, clock):
    seen = []

    def record(job, ctx):
        seen.append(job.payload["n"])
        return {"done": 1, "ids": [1, 2]}

    quiet_schedules()  # the job counts below are the test's own, whatever the time of day (eol.refresh at 04:45)
    kind = kinds("test.record", record)
    job = jobs.enqueue(kind, {"n": 7}, organization=a["org"])
    summary = worker.tick()
    job.refresh_from_db()
    assert seen == [7]
    assert job.status == JobStatus.SUCCEEDED and job.attempts == 1 and job.lease_token is None
    assert job.result == {"done": 1, "ids": {"count": 2}}
    assert job.started_at is not None and job.finished_at is not None
    assert summary["jobs"] == {"succeeded": 1}


def test_a_job_waits_for_its_run_after(db, kinds):
    kind = kinds("test.noop", lambda job, ctx: None)
    later = timezone.now() + timedelta(minutes=10)
    job = jobs.enqueue(kind, run_after=later)
    assert jobs.claim() is None
    claimed = jobs.claim(later)
    assert claimed is not None and claimed.id == job.id and claimed.status == JobStatus.RUNNING


def test_jobs_are_claimed_oldest_first(db, kinds):
    kind = kinds("test.noop", lambda job, ctx: None)
    now = timezone.now()
    second = jobs.enqueue(kind, run_after=now - timedelta(minutes=1))
    first = jobs.enqueue(kind, run_after=now - timedelta(minutes=5))
    assert [jobs.claim(now).id, jobs.claim(now).id] == [first.id, second.id]


def test_a_failed_job_is_retried_with_backoff_then_fails(db, kinds):
    def boom(job, ctx):
        raise RuntimeError("upstream said no")

    kind = kinds("test.boom", boom)
    job = jobs.enqueue(kind)
    now = timezone.now()
    worker.tick(now)
    job.refresh_from_db()
    assert job.status == JobStatus.QUEUED and job.attempts == 1
    assert job.run_after == now + timedelta(minutes=1)
    assert job.last_error == "RuntimeError: upstream said no" and job.lease_token is None
    worker.tick(now + timedelta(seconds=30))  # not yet
    job.refresh_from_db()
    assert job.attempts == 1
    second = now + timedelta(minutes=1)
    worker.tick(second)
    job.refresh_from_db()
    assert job.status == JobStatus.QUEUED and job.attempts == 2 and job.run_after == second + timedelta(minutes=4)
    worker.tick(second + timedelta(minutes=4))
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED and job.attempts == 3 and job.finished_at is not None


def test_a_permanent_error_fails_at_once(db, kinds):
    def gone(job, ctx):
        raise jobs.PermanentError("The connector was deleted.")

    job = jobs.enqueue(kinds("test.gone", gone))
    worker.tick()
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED and job.attempts == 1
    assert job.last_error == "PermanentError: The connector was deleted."


def test_an_unregistered_kind_fails_without_retry(db):
    """A job queued by a version that registered a kind this version doesn't have."""
    job = Job.objects.create(kind="retired.kind")
    worker.tick()
    job.refresh_from_db()
    assert job.status == JobStatus.FAILED and "No job kind retired.kind" in job.last_error


def test_errors_are_stored_without_configured_secrets(db, kinds, settings):
    settings.EMAIL_HOST_PASSWORD = "hunter2-smtp-password"

    def leaky(job, ctx):
        raise RuntimeError("535 auth failed for user x password hunter2-smtp-password")

    job = jobs.enqueue(kinds("test.leaky", leaky, max_attempts=1))
    worker.tick()
    job.refresh_from_db()
    assert "hunter2" not in job.last_error and "[redacted]" in job.last_error
    assert len(jobs.safe_error(RuntimeError("x" * 2000))) == jobs.ERROR_MAX


def test_an_expired_lease_is_abandoned_and_not_rerun(db, kinds):
    ran = []
    kind = kinds("test.record", lambda job, ctx: ran.append(job.id))
    job = jobs.enqueue(kind)
    claimed = jobs.claim()  # a worker took it, then died
    assert claimed is not None
    now = timezone.now()
    Job.objects.filter(id=job.id).update(lease_expires=now - timedelta(seconds=1))
    summary = worker.tick(now)
    job.refresh_from_db()
    assert job.status == JobStatus.ABANDONED and job.lease_token is None and job.finished_at == now
    assert job.last_error.startswith("Abandoned: attempt 1 started")
    assert ran == [] and summary["abandoned"] == {"jobs": 1, "schedules": 0}
    # The dead worker's late outcome is discarded.
    with pytest.raises(jobs.LeaseLost):
        jobs.succeed(claimed, {"late": True})
    with pytest.raises(jobs.LeaseLost):
        jobs.renew(claimed)
    job.refresh_from_db()
    assert job.status == JobStatus.ABANDONED and job.result == {}


def test_a_live_lease_is_not_abandoned(db, kinds):
    job = jobs.enqueue(kinds("test.noop", lambda job, ctx: None))
    jobs.claim()
    worker.tick()
    job.refresh_from_db()
    assert job.status == JobStatus.RUNNING


def test_a_long_job_renews_its_lease(db, kinds):
    seen = {}

    def long(job, ctx):
        before = Job.objects.get(id=job.id).lease_expires
        time.sleep(0.01)
        ctx.renew()
        after = Job.objects.get(id=job.id).lease_expires
        seen["extended"] = before is not None and after is not None and after > before
        return None

    job = jobs.enqueue(kinds("test.long", long))
    worker.tick()
    job.refresh_from_db()
    assert seen == {"extended": True} and job.status == JobStatus.SUCCEEDED


@pytest.mark.parametrize("clock", TICK_MOMENTS, indirect=True)
def test_a_job_that_lost_its_lease_records_nothing(db, kinds, clock):
    quiet_schedules()  # no app schedule's job in the counts, whatever the time of day

    def taken_over(job, ctx):
        Job.objects.filter(id=job.id).update(status=JobStatus.ABANDONED, lease_token=None)  # another worker's sweep
        ctx.renew()

    job = jobs.enqueue(kinds("test.taken", taken_over))
    summary = worker.tick()
    job.refresh_from_db()
    assert job.status == JobStatus.ABANDONED and job.last_error == "" and summary["jobs"] == {}


@pytest.mark.parametrize("clock", TICK_MOMENTS, indirect=True)
def test_the_tick_stops_claiming_after_its_budget(db, kinds, clock):
    quiet_schedules()  # no app schedule queues a job of its own meanwhile, whatever the time of day
    kind = kinds("test.noop", lambda job, ctx: None)
    for _ in range(3):
        jobs.enqueue(kind)
    worker.tick(job_budget=timedelta(0))
    assert Job.objects.filter(status=JobStatus.QUEUED).count() == 3


def test_prune_deletes_old_finished_jobs_only(db, kinds):
    kind = kinds("test.noop", lambda job, ctx: None)
    now = timezone.now()
    old = now - jobs.KEEP_FINISHED - timedelta(days=1)
    stale = Job.objects.create(kind=kind, status=JobStatus.SUCCEEDED, finished_at=old)
    failed = Job.objects.create(kind=kind, status=JobStatus.FAILED, finished_at=old)
    recent = Job.objects.create(kind=kind, status=JobStatus.SUCCEEDED, finished_at=now)
    waiting = Job.objects.create(kind=kind, status=JobStatus.QUEUED, run_after=old)
    assert jobs.prune(now) == {"jobs_pruned": 2}
    assert set(Job.objects.values_list("id", flat=True)) == {recent.id, waiting.id}
    assert not Job.objects.filter(id__in=[stale.id, failed.id]).exists()


# ---- concurrency ---------------------------------------------------------------------------------------------------


@pytest.fixture
def threaded_kinds(transactional_db, monkeypatch):
    monkeypatch.setattr(jobs, "JOB_KINDS", dict(jobs.JOB_KINDS))
    return transactional_db


def test_two_claimers_never_take_the_same_job(threaded_kinds):
    jobs.register_job("test.noop", lambda job, ctx: None)
    job = jobs.enqueue("test.noop")
    claimed: list = []
    lock = threading.Lock()

    def claimer():
        got = jobs.claim()
        with lock:
            claimed.append(got.id if got is not None else None)

    assert run_together(claimer, claimer) == [None, None]
    assert sorted(claimed, key=str) == sorted([job.id, None], key=str)
    assert Job.objects.get(id=job.id).attempts == 1


def test_concurrent_workers_run_each_job_exactly_once(threaded_kinds):
    ran: list = []
    lock = threading.Lock()

    def record(job, ctx):
        time.sleep(0.005)
        with lock:
            ran.append(job.payload["n"])

    jobs.register_job("test.record", record)
    for n in range(12):
        jobs.enqueue("test.record", {"n": n})

    def drain():
        while (job := jobs.claim()) is not None:
            jobs.run(job)

    assert run_together(drain, drain, drain) == [None, None, None]
    assert sorted(ran) == list(range(12))
    assert Job.objects.filter(status=JobStatus.SUCCEEDED, attempts=1).count() == 12


def test_a_schedule_fires_once_under_two_workers(transactional_db, monkeypatch):
    registry: dict = {}
    monkeypatch.setattr(jobs, "SCHEDULES", registry)
    fired: list = []
    lock = threading.Lock()

    def slow(now):
        time.sleep(0.2)
        with lock:
            fired.append(now)
        return {"fired": 1}

    jobs.register_schedule("test.minute", slow, "* * * * *", "Every minute")
    now = timezone.now()
    jobs.sync_schedules(now)
    outcomes: list = []

    def tick():
        outcomes.append(jobs.run_schedule(registry["test.minute"], now)[0])

    assert run_together(tick, tick) == [None, None]
    assert len(fired) == 1 and sorted(outcomes) == ["skipped", "succeeded"]


def test_two_whole_ticks_at_once_fire_each_schedule_once(transactional_db, monkeypatch):
    registry: dict = {}
    monkeypatch.setattr(jobs, "SCHEDULES", registry)
    fired: list = []
    lock = threading.Lock()

    def count(now):
        with lock:
            fired.append(1)

    jobs.register_schedule("test.minute", count, "* * * * *", "Every minute")
    now = timezone.now()
    assert run_together(lambda: worker.tick(now), lambda: worker.tick(now)) == [None, None]
    assert len(fired) == 1 and Schedule.objects.count() == 1


# ---- schedules -------------------------------------------------------------------------------------------------------


def test_every_minute_schedules_fire_on_the_first_tick_and_nightly_ones_wait(db, only_schedules):
    fired: list = []
    jobs.register_schedule("test.minute", lambda now: fired.append("minute"), "* * * * *", "Every minute")
    jobs.register_schedule("test.nightly", lambda now: fired.append("nightly"), "40 3 * * *", "Nightly")
    now = at("2026-11-04 10:00:30")
    worker.tick(now)
    assert fired == ["minute"]
    nightly = Schedule.objects.get(name="test.nightly")
    assert nightly.next_due == at("2026-11-05 03:40") and nightly.last_started is None
    minute = Schedule.objects.get(name="test.minute")
    assert minute.next_due == at("2026-11-04 10:01") and minute.last_succeeded is not None
    worker.tick(now + timedelta(seconds=20))  # same minute: nothing more
    assert fired == ["minute"]
    worker.tick(at("2026-11-05 03:40:05"))
    assert fired == ["minute", "minute", "nightly"]
    assert Schedule.objects.get(name="test.nightly").next_due == at("2026-11-06 03:40")


def test_missed_fires_are_not_caught_up(db, only_schedules):
    fired: list = []
    jobs.register_schedule("test.hourly", lambda now: fired.append(now), "0 * * * *", "Hourly")
    jobs.sync_schedules(at("2026-11-04 09:59:30"))
    worker.tick(at("2026-11-04 15:20"))  # the worker was down for five fires
    worker.tick(at("2026-11-04 15:21"))
    assert len(fired) == 1
    assert Schedule.objects.get(name="test.hourly").next_due == at("2026-11-04 16:00")


def test_a_running_schedule_is_not_fired_again(db, only_schedules):
    fired: list = []
    jobs.register_schedule("test.minute", lambda now: fired.append(now), "* * * * *", "Every minute")
    now = timezone.now()
    jobs.sync_schedules(now)
    later = now + timedelta(minutes=5)
    Schedule.objects.filter(name="test.minute").update(
        lease_token="1f0b3c2e-0000-4000-8000-000000000000", lease_expires=later + timedelta(minutes=30)
    )
    assert jobs.run_schedule(only_schedules["test.minute"], later) == ("skipped", None)
    assert fired == []


def test_a_disabled_schedule_does_not_fire(db, only_schedules):
    fired: list = []
    jobs.register_schedule("test.minute", lambda now: fired.append(now), "* * * * *", "Every minute")
    now = timezone.now()
    jobs.sync_schedules(now)
    Schedule.objects.filter(name="test.minute").update(enabled=False)
    worker.tick(now)
    assert fired == []
    jobs.sync_schedules(now)  # the worker never turns an operator's switch back on
    assert Schedule.objects.get(name="test.minute").enabled is False


def test_a_changed_cron_moves_next_due(db, only_schedules):
    jobs.register_schedule("test.s", lambda now: None, "0 4 * * *", "S")
    jobs.sync_schedules(at("2026-11-04 10:00"))
    assert Schedule.objects.get(name="test.s").next_due == at("2026-11-05 04:00")
    only_schedules.clear()
    jobs.register_schedule("test.s", lambda now: None, "30 11 * * *", "S")
    jobs.sync_schedules(at("2026-11-04 10:00"))
    row = Schedule.objects.get(name="test.s")
    assert row.cron == "30 11 * * *" and row.next_due == at("2026-11-04 11:30")


def test_a_failing_schedule_records_its_error_and_recovers(db, only_schedules):
    state = {"fail": True}

    def flaky(now):
        if state["fail"]:
            raise ConnectionError("SMTP down")
        return {"sent": 2}

    jobs.register_schedule("test.flaky", flaky, "* * * * *", "Flaky")
    now = timezone.now()
    summary = worker.tick(now)
    row = Schedule.objects.get(name="test.flaky")
    assert summary["schedules_failed"] == ["test.flaky"]
    assert row.last_error == "ConnectionError" and row.last_succeeded is None and row.lease_token is None
    state["fail"] = False
    summary = worker.tick(now + timedelta(minutes=1))
    row.refresh_from_db()
    assert summary["sent"] == 2 and row.last_error == "" and row.last_result == {"sent": 2}
    assert row.last_succeeded == row.last_finished


def test_an_expired_schedule_lease_is_abandoned(db, only_schedules):
    jobs.register_schedule("test.minute", lambda now: None, "0 0 1 1 *", "Yearly")
    now = timezone.now()
    jobs.sync_schedules(now)
    Schedule.objects.filter(name="test.minute").update(
        lease_token="1f0b3c2e-0000-4000-8000-000000000000",
        lease_expires=now - timedelta(seconds=1),
        last_started=now - timedelta(hours=1),
    )
    assert jobs.abandon_expired(now) == (0, 1)
    row = Schedule.objects.get(name="test.minute")
    assert row.lease_token is None and row.last_finished == now and row.last_error.startswith("Abandoned:")


def test_the_apps_register_the_former_tick_work_as_schedules():
    """The work an earlier version's tick called directly is now registered by each app (notify, discovery, imports).
    Digests are deliveries (notify.deliver jobs), so there is no digest.send schedule."""
    assert {"digest.build", "notify.deliveries", "discovery.connectors", "imports.purge", "jobs.prune"} <= set(
        jobs.SCHEDULES
    )
    assert "digest.send" not in jobs.SCHEDULES
    names = list(jobs.SCHEDULES)
    assert names.index("reminders.generate") < names.index("notify.rules")  # rules read the reminders just made


def test_core_job_machinery_does_not_import_the_apps_it_runs():
    """Layering: the worker takes its work from the registry, so these core modules never
    import catalog, money, notify, discovery, imports, api, eol, tickets, offboarding, catalog_code or ee, at module
    level or inside functions."""
    # Every app the worker runs work for (tickets, offboarding, catalog_code and ee were missing).
    forbidden = {
        "catalog",
        "money",
        "notify",
        "discovery",
        "imports",
        "api",
        "eol",
        "tickets",
        "offboarding",
        "catalog_code",
        "ee",
    }
    core = Path(__file__).resolve().parent.parent / "core"
    modules = ["worker.py", "jobs.py", "cron.py", "health.py", "system_views.py", "models.py", "apps.py"]
    modules.append("management/commands/run_worker.py")
    for name in modules:
        tree = ast.parse((core / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots = {node.module.split(".")[0]}
            else:
                continue
            assert not roots & forbidden, f"core/{name} imports {roots & forbidden}"


# ---- the worker process ------------------------------------------------------------------------------------------------


def test_the_tick_logs_one_line_without_payloads(db, kinds, caplog):
    kind = kinds("test.noop", lambda job, ctx: None)
    jobs.enqueue(kind, {"note": "payload-marker-7f3"})
    with caplog.at_level(logging.INFO, logger="softwarden.worker"):
        worker.tick()
    lines = [r.getMessage() for r in caplog.records if r.name == "softwarden.worker"]
    assert sum(line.startswith("worker tick ") for line in lines) == 1
    assert any(line.startswith("job succeeded") and "kind=test.noop" in line for line in lines)
    assert not any("payload-marker-7f3" in line for line in lines)


def test_run_worker_once_prints_the_tick(db, monkeypatch):
    # close_old_connections would close the test's transaction (autocommit is off inside it).
    monkeypatch.setattr("core.management.commands.run_worker.close_old_connections", lambda: None)
    out = StringIO()
    call_command("run_worker", "--once", stdout=out)
    assert out.getvalue().startswith("Tick: {")
    assert WorkerHeartbeat.objects.filter(name="worker").exists()


def test_the_heartbeat_keeper_beats_from_its_own_thread(transactional_db):
    keeper = worker.HeartbeatKeeper(interval=0.05).start()
    try:
        deadline = time.monotonic() + 5
        while not WorkerHeartbeat.objects.filter(name="worker").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        keeper.stop()
    assert WorkerHeartbeat.objects.filter(name="worker").exists()
    assert not keeper.thread.is_alive()


# ---- review fixes: leases, shutdown, reconnects, secrets -----------------------------------------------------------


def test_a_lease_left_by_a_killed_worker_frees_within_the_short_lease(db, only_schedules):
    from core import health

    fired: list = []
    jobs.register_schedule("test.minute", lambda now: fired.append(now), "* * * * *", "Every minute")
    now = timezone.now()
    jobs.sync_schedules(now)
    assert jobs._take(only_schedules["test.minute"], now) is not None  # a worker took the fire, then was killed
    assert jobs.SCHEDULE_LEASE == timedelta(minutes=5)
    check = next(c for c in health.schedule_checks(now + timedelta(minutes=2)) if c["name"] == "test.minute")
    assert check["state"] == "running"
    assert jobs.run_schedule(only_schedules["test.minute"], now + timedelta(minutes=2)) == ("skipped", None)
    freed = now + jobs.SCHEDULE_LEASE + timedelta(seconds=1)
    check = next(c for c in health.schedule_checks(freed) if c["name"] == "test.minute")
    assert check["state"] == "stuck"
    summary = worker.tick(freed)
    assert summary["abandoned"] == {"jobs": 0, "schedules": 1} and len(fired) == 1


def test_the_keeper_renews_a_running_schedule_up_to_the_ceiling(db, only_schedules):
    seen = {}

    def long(now):
        seen["renewed"] = jobs.renew_held_schedules(now + timedelta(minutes=4))
        seen["lease"] = Schedule.objects.get(name="test.long").lease_expires
        seen["past_ceiling"] = jobs.renew_held_schedules(now + jobs.SCHEDULE_CEILING)

    jobs.register_schedule("test.long", long, "* * * * *", "Long")
    now = timezone.now()
    jobs.sync_schedules(now)
    assert jobs.run_schedule(only_schedules["test.long"], now)[0] == "succeeded"
    assert seen == {
        "renewed": 1,
        "lease": now + timedelta(minutes=4) + jobs.SCHEDULE_LEASE,
        "past_ceiling": 0,
    }
    assert jobs.renew_held_schedules() == 0  # released when the run ended


def test_should_stop_stops_firing_and_claiming(db, kinds, only_schedules):
    fired: list = []
    jobs.register_schedule("test.minute", lambda now: fired.append(now), "* * * * *", "Every minute")
    kind = kinds("test.noop", lambda job, ctx: None)
    for _ in range(3):
        jobs.enqueue(kind)
    worker.tick(should_stop=lambda: True)
    assert fired == [] and Job.objects.filter(status=JobStatus.QUEUED).count() == 3
    # SIGTERM arriving mid-tick: the job in progress finishes, nothing more is claimed.
    summary = worker.tick(should_stop=lambda: Job.objects.filter(status=JobStatus.SUCCEEDED).exists())
    assert fired != [] and summary["jobs"] == {"succeeded": 1}
    assert Job.objects.filter(status=JobStatus.QUEUED).count() == 2


def test_a_schedule_outcome_is_written_after_a_reconnect(transactional_db, monkeypatch):
    from django.db import OperationalError
    from django.db.models.query import QuerySet

    registry: dict = {}
    monkeypatch.setattr(jobs, "SCHEDULES", registry)
    jobs.register_schedule("test.minute", lambda now: {"done": 1}, "* * * * *", "Every minute")
    now = timezone.now()
    jobs.sync_schedules(now)
    real_update = QuerySet.update
    failures: list[int] = []

    def flaky(self, **fields):
        if "last_finished" in fields and not failures:
            failures.append(1)
            raise OperationalError("server closed the connection unexpectedly")
        return real_update(self, **fields)

    monkeypatch.setattr(QuerySet, "update", flaky)
    assert jobs.run_schedule(registry["test.minute"], now)[0] == "succeeded"
    row = Schedule.objects.get(name="test.minute")
    assert failures == [1] and row.lease_token is None and row.last_result == {"done": 1}


def test_a_schedule_stores_only_the_error_type(db, only_schedules, caplog):
    def leaky(now):
        raise RuntimeError("duplicate key: DETAIL: Key (name)=(orga Payroll) already exists.")

    jobs.register_schedule("test.leaky", leaky, "* * * * *", "Leaky")
    with caplog.at_level(logging.WARNING, logger="softwarden.worker"):
        worker.tick()
    assert Schedule.objects.get(name="test.leaky").last_error == "RuntimeError"
    assert any("orga Payroll" in r.getMessage() for r in caplog.records)  # the detail stays in the logs


def test_import_purge_carries_on_past_one_organizations_failure(a, b, monkeypatch, caplog):
    from datetime import date

    from imports import importer
    from imports.models import ImportRun

    runs = {
        org["org"].id: ImportRun.objects.create(
            organization=org["org"], kind="application", filename="x.csv", purge_after=date(2020, 1, 1)
        )
        for org in (a, b)
    }
    real = importer.purge_run

    def purge(run, reason):
        if run.organization_id == a["org"].id:
            raise RuntimeError("orga detail")
        return real(run, reason)

    monkeypatch.setattr(importer, "purge_run", purge)
    with caplog.at_level(logging.ERROR, logger="softwarden.imports"):
        assert importer.purge_expired() == 1
    assert ImportRun.objects.get(id=runs[b["org"].id].id).purged_at is not None
    assert ImportRun.objects.get(id=runs[a["org"].id].id).purged_at is None
    assert any(r.getMessage() == f"import purge failed organization={a['org'].id}" for r in caplog.records)


def test_the_keeper_recovers_after_the_database_drops_its_connection(transactional_db, monkeypatch):
    from django.db import connection

    keeper = worker.HeartbeatKeeper()
    connection.ensure_connection()
    connection.connection.close()  # as after a database restart
    keeper.keep_once()
    assert WorkerHeartbeat.objects.filter(name="worker").exists()
    # Without dropping the dead connection first, the beat fails.
    connection.ensure_connection()
    connection.connection.close()
    monkeypatch.setattr(worker, "close_old_connections", lambda: None)
    with pytest.raises(Exception):  # noqa: B017 - InterfaceError or OperationalError, depending on the driver
        keeper.keep_once()
    connection.close()


@pytest.mark.parametrize(
    "variable,value",
    [
        ("SOFTWARDEN_OIDC_CLIENT_SECRET", "oidc-client-secret-value"),
        ("POSTGRES_PASSWORD", "postgres-password-value"),
        ("SOFTWARDEN_SECRETS_KEY", "first-fernet-key-aaaa,second-fernet-key-bbbb"),
        ("SOFTWARDEN_CREDENTIAL_KEYS", "cred-key-one-aaaa, cred-key-two-bbbb"),
    ],
)
def test_errors_never_keep_configured_secrets(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    parts = [p.strip() for p in value.split(",")]
    for used in [value, *parts]:
        text = jobs.safe_error(RuntimeError(f"connect failed using {used} at host"))
        assert not any(secret in text for secret in parts), (variable, text)
        assert "[redacted]" in text


def test_payload_guard_refuses_webhooks_and_allows_ids(db, kinds):
    kind = kinds("test.noop", lambda job, ctx: None)
    refused_keys = [
        "webhook_url",
        "teamsWebhook",
        "pass",
        "smtpPass",
        "smtp_pass",
        "SMTPPass",
        "session_id",
        "sessionId",
    ]
    for key in refused_keys:
        with pytest.raises(ValueError, match="never carry secrets"):
            jobs.enqueue(kind, {key: "x"})
    allowed = {"token_id": "1", "tokenId": "1", "webhook_id": "3", "bypass_cache": True, "compass": "n", "passage": "p"}
    assert jobs.enqueue(kind, allowed).payload == allowed


def test_missing_start_times_do_not_break_abandonment_or_health(db, only_schedules):
    from core import health

    jobs.register_schedule("test.s", lambda now: None, "0 0 1 1 *", "Yearly")
    now = timezone.now()
    jobs.sync_schedules(now)
    Schedule.objects.filter(name="test.s").update(
        lease_token="1f0b3c2e-0000-4000-8000-000000000000", lease_expires=now + timedelta(minutes=1)
    )
    states = [
        next(c for c in health.schedule_checks(t) if c["name"] == "test.s")["state"]
        for t in (now, now + timedelta(minutes=2))
    ]
    assert states == ["running", "stuck"]
    job = Job.objects.create(
        kind="test.x",
        status=JobStatus.RUNNING,
        lease_token="2f0b3c2e-0000-4000-8000-000000000000",
        lease_expires=now - timedelta(seconds=1),
    )
    assert jobs.abandon_expired(now + timedelta(minutes=2)) == (1, 1)
    job.refresh_from_db()
    assert job.status == JobStatus.ABANDONED and job.last_error.startswith("Abandoned: attempt 0 and its lease")


def test_an_organizations_jobs_never_block_deleting_it(a):
    """CASCADE, not PROTECT or SET_NULL, so a deleted
    organization's jobs go with it and never run as installation-wide jobs."""
    from django.db import models

    from core.models import Job

    assert Job._meta.get_field("organization").remote_field.on_delete is models.CASCADE


def test_the_orphan_check_uses_the_delivery_index(a):
    """The sweep's orphan check (notify/deliveries.sweep) can find a delivery's live job through `core_job_delivery`.
    The plan is read from real statistics: an empty, never analysed table left the planner
    choosing between the delivery index and `core_job_org_status` on its default guesses, so the test sometimes saw the
    other. Here the table holds what a busy sweep sees, many live delivery jobs of one organization, and is analysed,
    so the index that names one delivery is the selective one; the plan must use it with the delivery as its index
    condition, not merely mention it."""
    import uuid

    from django.db import connection

    from core.models import Job, JobStatus

    Job.objects.bulk_create(
        Job(
            kind="notify.deliver",
            organization=a["org"],
            status=JobStatus.QUEUED if n % 2 else JobStatus.RUNNING,
            payload={"delivery_id": str(uuid.uuid4())},
        )
        for n in range(2000)
    )
    wanted = str(uuid.uuid4())
    query = Job.objects.filter(
        kind="notify.deliver", status__in=[JobStatus.QUEUED, JobStatus.RUNNING], payload__delivery_id=wanted
    )
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE core_job")
        cursor.execute("SET LOCAL enable_seqscan = off")
        sql, params = query.query.sql_with_params()
        cursor.execute(f"EXPLAIN {sql}", params)
        plan = "\n".join(row[0] for row in cursor.fetchall())
    assert "core_job_delivery" in plan, plan
    assert "Index Cond: ((payload -> 'delivery_id'::text) =" in plan, plan
