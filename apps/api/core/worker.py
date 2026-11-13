"""The background worker: PostgreSQL-backed, no broker. `manage.py run_worker` calls
tick() every minute. A tick:

1. records a heartbeat (`/readyz` and the System page read it);
2. gives every registered schedule a row (core.jobs.sync_schedules);
3. marks jobs and schedule runs whose lease expired as abandoned;
4. fires each due schedule once (digests, discovery connectors, import purges, ... registered by their apps);
5. claims and runs due jobs until none is left or the tick's time budget is spent;
6. with a long lane (the long-running command), starts long jobs (discovery runs) in threads of their own, up to the
   lane's size, so a slow tenant's run never holds up the schedules, the short jobs or the next tick. Without one
   (`run_worker --once`, tests), long jobs run inline after the short ones, as any job.

Core never imports the apps whose work it runs: each app registers its schedules and job kinds in core.jobs from
its AppConfig.ready (tests/test_jobs.py checks the imports). Two workers may tick at once: a schedule fire is taken
by one conditional UPDATE and jobs are claimed with SKIP LOCKED, so neither runs anything twice.

The long-running command also keeps the heartbeat fresh from a background thread while a long job runs, so the
heartbeat means "the worker process is alive and reaches the database"; a stalled job shows up through its lease
and the schedule states instead."""

import logging
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

from django.db import close_old_connections, connection
from django.utils import timezone

from . import jobs
from .models import JobStatus, WorkerHeartbeat
from .request_context import unit_of_work

log = logging.getLogger("softwarden.worker")

HEARTBEAT = "worker"
JOB_BUDGET = timedelta(seconds=50)  # stop claiming new jobs after this, so the next tick's heartbeat isn't late
KEEPER_INTERVAL = 30.0


# This process's own heartbeat, beside the shared one in the database (cloud deployment). Every worker writes
# the same database row, so with several replicas (Helm `worker.replicas`) the row stays fresh while any one of them
# runs, and can't tell a liveness probe which one is stuck. The long-running command also touches a file in its
# container's /tmp; `run_worker --check --local` (the Helm liveness probe) fails once it is older than
# health.STALE_AFTER (180 s).
#
# Who touches it: the main loop at the start of each tick, and the HeartbeatKeeper thread every KEEPER_INTERVAL (30 s)
# after a beat that reached the database, but only while the main loop is responsive: between ticks for no longer
# than the loop's interval plus IDLE_MARGIN, or busy with one step (the tick's housekeeping, one schedule run, one job) for at most MAIN_LOOP_BOUND. A schedule may run for an
# hour (core.jobs.SCHEDULE_CEILING, its lease renewed by the keeper meanwhile) and a default-lane job holds a
# DEFAULT_LEASE of 10 minutes, so a probe tied to the tick's start alone would kill legitimate long runs. Past the bound
# the step is stuck by the jobs module's own rules (a schedule's lease isn't renewed beyond its ceiling, so another
# worker may fire it again), the keeper stops touching the file, and the pod is restarted about STALE_AFTER later.
LOCAL_HEARTBEAT_NAME = "softwarden-worker-heartbeat"
_local_heartbeat: Path | None = None
# The longest one main-loop step may legitimately take: a schedule run up to its ceiling, plus one schedule lease of
# margin (the run's last lease can't outlast that either). It is also well above a default-lane job's 10-minute lease.
MAIN_LOOP_BOUND = jobs.SCHEDULE_CEILING + jobs.SCHEDULE_LEASE
# Between ticks the long-running loop only sleeps for its interval and reconnects, so an idle spell longer than the
# interval plus IDLE_MARGIN means the loop is stuck outside tick() (in run_worker's own loop): unresponsive too.
IDLE_MARGIN = timedelta(seconds=60)
# Monotonic time the main loop's current step started, or None between ticks; and the time the last tick ended, or
# None while one runs (or before the loop starts). Written by the main thread, read by the keeper thread (one float
# reference each: no lock needed; see _mark_busy and _mark_idle for the order).
_busy_since: float | None = None
_idle_since: float | None = None
# Seconds an idle spell may last: set by start_main_loop (the long-running command); None leaves idleness unbounded
# (tick() alone, `run_worker --once`, tests).
_idle_bound: float | None = None
_clock = time.monotonic  # replaced in tests


def _mark_busy() -> None:
    """The main loop starts a step (the tick itself, a schedule run, a job)."""
    global _busy_since, _idle_since
    _busy_since = _clock()  # set before idle is cleared, so the keeper never sees neither
    _idle_since = None


def _mark_idle() -> None:
    global _busy_since, _idle_since
    _idle_since = _clock()  # set before busy is cleared, as above
    _busy_since = None


def start_main_loop(interval: float) -> None:
    """The long-running command starts its loop, a tick every `interval` seconds: from now on an idle spell longer than
    the interval plus IDLE_MARGIN counts as unresponsive."""
    global _idle_bound
    _idle_bound = float(interval) + IDLE_MARGIN.total_seconds()
    _mark_idle()


def stop_main_loop() -> None:
    global _idle_bound
    _idle_bound = None


def main_loop_responsive() -> bool:
    """In a step that has run no longer than MAIN_LOOP_BOUND, or between ticks for no longer than the loop's interval
    plus IDLE_MARGIN (unbounded outside the long-running loop)."""
    started = _busy_since
    if started is not None:
        return _clock() - started <= MAIN_LOOP_BOUND.total_seconds()
    idle, bound = _idle_since, _idle_bound
    return idle is None or bound is None or _clock() - idle <= bound


def local_heartbeat_path() -> Path:
    """The file in this container's temporary directory, resolved when asked (not at import)."""
    return Path(tempfile.gettempdir()) / LOCAL_HEARTBEAT_NAME


def keep_local_heartbeat(path: Path | None = None, *, on: bool = True) -> None:
    """Touch `path` (default local_heartbeat_path()) from now on, as the comment above says; on=False stops. Only the
    long-running command turns it on."""
    global _local_heartbeat
    _local_heartbeat = (path or local_heartbeat_path()) if on else None


def local_heartbeat_age(path: Path | None = None) -> float | None:
    """Seconds since this container's worker last touched its file, or None if it never has (no file)."""
    try:
        return max(0.0, time.time() - (path or local_heartbeat_path()).stat().st_mtime)
    except OSError:
        return None


def _touch_local_heartbeat() -> None:
    path = _local_heartbeat
    if path is not None:
        try:
            path.touch()  # an existing file gets the current modification time
        except OSError:  # a full or read-only /tmp: the probe then restarts the pod, which is the right outcome
            log.warning("local heartbeat file could not be written")


def beat(now=None):
    WorkerHeartbeat.objects.update_or_create(name=HEARTBEAT, defaults={"last_tick": now or timezone.now()})


def tick(now=None, job_budget=JOB_BUDGET, should_stop=None, lanes=None):
    """One pass (see _tick), in a unit of work of its own: a line the tick logs outside a job or schedule run (its
    summary, an abandonment) carries this tick's id, never one left behind by an earlier tick or by something that
    started a unit for the rest of the process."""
    _mark_busy()
    try:
        with unit_of_work():
            return _tick(now, job_budget, should_stop, lanes)
    finally:
        _mark_idle()


def _tick(now=None, job_budget=JOB_BUDGET, should_stop=None, lanes=None):
    """One pass. Returns a summary: each fired schedule's result merged in (for example `runs`, `sent`,
    `connector_runs_queued`), plus `schedules_failed` and `jobs` counts, and `long_jobs_started` with a long lane. When
    `should_stop()` turns true (SIGTERM), no further schedule is fired and no further job claimed; the one in progress
    finishes. `lanes` is the command's LongLane: short jobs only are claimed here and the lane takes the long ones."""
    stop = should_stop or (lambda: False)
    given = now  # jobs are claimed as of this time when a caller (a test) fixes it, else as of each claim
    now = now or timezone.now()
    clock = time.monotonic()
    beat(now)
    _touch_local_heartbeat()  # this pod's liveness file (the keeper keeps it fresh while the loop stays responsive)
    jobs.sync_schedules(now)
    abandoned_jobs, abandoned_schedules = jobs.abandon_expired(now)
    summary: dict = {}
    fired, failed = [], []
    for definition in list(jobs.SCHEDULES.values()):
        if stop():
            break
        # Each schedule sees the time it starts (a caller's fixed time in tests), not the tick's: one after a slow
        # schedule would otherwise get a stale `now`.
        _mark_busy()  # each schedule run is one step for the liveness bound
        outcome, result = jobs.run_schedule(definition, given or timezone.now())
        if outcome == "skipped":
            continue
        fired.append(definition.name)
        if outcome == "failed":
            failed.append(definition.name)
        elif isinstance(result, dict):
            summary.update(result)
    _mark_busy()  # claiming jobs and filling lanes is a step of its own, not the last schedule's
    counts = {status: 0 for status in JobStatus.values}
    deadline = time.monotonic() + job_budget.total_seconds()
    while time.monotonic() < deadline and not stop():
        job = jobs.claim(given, lane=jobs.DEFAULT_LANE if lanes is not None else None)
        if job is None:
            break
        _mark_busy()
        status = jobs.run(job, given)
        if status is not None:
            counts[status] += 1
    ran = {k: v for k, v in counts.items() if v}
    summary["schedules_failed"] = failed
    summary["jobs"] = ran
    if lanes is not None:
        summary["long_jobs_started"] = 0 if stop() else lanes.fill(given)
    if abandoned_jobs or abandoned_schedules:
        summary["abandoned"] = {"jobs": abandoned_jobs, "schedules": abandoned_schedules}
    log.info(
        "worker tick schedules=%s failed=%s jobs=%s abandoned=%s/%s took=%.2fs",
        len(fired),
        ",".join(failed) or "none",
        sum(ran.values()),
        abandoned_jobs,
        abandoned_schedules,
        time.monotonic() - clock,
    )
    return summary


LONG_LANE_SIZE = 2  # long jobs at once per worker process (`run_worker --long-jobs`, SOFTWARDEN_WORKER_LONG_JOBS)
STOP_GRACE = 20.0  # seconds a stopping worker waits for its long jobs; the rest are left to their leases


class LaneSizeError(ValueError):
    """The long lane needs at least one slot: with none, discovery runs would wait in the queue for ever."""


class LongLane:
    """Runs long jobs (core.jobs.LONG_LANE: discovery runs) in up to `size` threads beside the tick, each with its
    own database connection. `fill` claims while a slot is free (core.jobs.claim's long lane: an organization without
    a long job running first); a finished thread frees its slot. A job's lease and outcome are core.jobs' as for any
    job: the job renews its lease at its own checkpoints, and a thread killed with the process leaves the lease to
    expire (the job is abandoned and its owner marks its own row, discovery's run, failed).

    `stop()` (the command's SIGTERM) tells the running jobs through their context (`ctx.stopping()`): a discovery run
    then fails itself at its next request, "the worker stopped; run it again", while it still holds its lease, so a
    restart doesn't leave the connector locked until the lease runs out."""

    def __init__(self, size=LONG_LANE_SIZE):
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise LaneSizeError(
                f"The long-job lane needs 1 or more slots (got {size!r}): with none, discovery runs never start. Set "
                "--long-jobs or SOFTWARDEN_WORKER_LONG_JOBS to a whole number of 1 or more."
            )
        self.size = size
        self.threads: list[threading.Thread] = []
        self.stopping = threading.Event()

    def busy(self) -> int:
        self.threads = [t for t in self.threads if t.is_alive()]
        return len(self.threads)

    def fill(self, now=None) -> int:
        started = 0
        while self.busy() < self.size and not self.stopping.is_set():
            job = jobs.claim(now, lane=jobs.LONG_LANE)
            if job is None:
                break
            thread = threading.Thread(target=self._run, args=(job, now), name=f"job-{job.kind}", daemon=True)
            thread.start()
            self.threads.append(thread)
            started += 1
        return started

    def _run(self, job, now):
        try:
            jobs.run(job, now, should_stop=self.stopping.is_set)
        except Exception:  # jobs.run records every failure; this is a database outage while recording it
            log.exception("long job thread failed id=%s kind=%s", job.id, job.kind)
        finally:
            connection.close()

    def stop(self) -> None:
        """Claim no more long jobs and tell the running ones to stop (they see it at their next checkpoint)."""
        self.stopping.set()

    def wait(self, timeout=STOP_GRACE) -> int:
        """Wait up to `timeout` seconds for the running long jobs. Returns how many are still running."""
        deadline = time.monotonic() + timeout
        for thread in list(self.threads):
            thread.join(max(deadline - time.monotonic(), 0))
        return self.busy()


def heartbeat_age(now=None):
    """Seconds since the last heartbeat, or None if the worker has never run."""
    beat_row = WorkerHeartbeat.objects.filter(name=HEARTBEAT).first()
    if beat_row is None:
        return None
    return ((now or timezone.now()) - beat_row.last_tick).total_seconds()


class HeartbeatKeeper:
    """Refreshes the heartbeat every KEEPER_INTERVAL seconds from its own thread (and database connection) while the
    command runs, so a job longer than the stale threshold doesn't make the worker look dead, and renews the lease of
    the schedule run in progress (core.jobs.renew_held_schedules). Only the long-running command starts it; tick()
    alone never does."""

    def __init__(self, interval=KEEPER_INTERVAL):
        self.interval = interval
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)

    def _run(self):
        try:
            while not self.stopping.wait(self.interval):
                try:
                    self.keep_once()
                except Exception:  # a database blip; the next beat reconnects and tries again
                    log.warning("heartbeat failed")
        finally:
            connection.close()

    def keep_once(self):
        # Drops this thread's connection if it is broken or old (CONN_MAX_AGE), so a database restart doesn't leave
        # the keeper failing on a dead connection forever.
        close_old_connections()
        beat()
        if main_loop_responsive():
            _touch_local_heartbeat()
        jobs.renew_held_schedules()

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stopping.set()
        self.thread.join(timeout=self.interval + 5)
