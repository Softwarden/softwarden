"""Background operational status: is the worker alive, and is every
schedule running on time? Read by `/readyz` (softwarden/ops.py) and the System status API (core/system_views.py).

Worker: `ok` when the heartbeat is younger than STALE_AFTER, `stale` when older, `never` when no worker has ever
ticked against this database.

Schedule states, worst first:
- stuck: a run holds a lease that has expired, or has run longer than SCHEDULE_CEILING (the next tick marks an
  expired one abandoned, which then shows as failing).
- overdue: enabled, and its due time passed more than LATE_GRACE ago without a fire: the
  previous fire time is overdue. Every-minute schedules are judged by their oldest unserved fire, so a dead worker
  makes them overdue too, not only the nightly ones.
- failing: the last run finished with an error (or was abandoned).
- running: a run is in progress under a live lease.
- never_run: registered, no run yet, not overdue (a fresh install or a schedule added by an upgrade).
- disabled: an operator turned it off. Not a fault.
- ok.

Backups (core/backup.py) are reported beside these as `backup` and `warnings`, and never change the overall state.

Overall: `unhealthy` when the worker isn't ok or a schedule is stuck or overdue; `degraded` when a schedule is
failing (usually an outside service such as SMTP, retried by the schedule itself); otherwise `ok`. `/readyz` answers
503 only when unhealthy.

`ops_detail` adds the job queue per lane, recent failures and disk space for the operator view, `/healthz/ops`."""

import os
import shutil
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db.models import Count, Min, Q
from django.utils import timezone

from . import backup, jobs
from .models import Job, JobStatus, Schedule, WorkerHeartbeat

STALE_AFTER = timedelta(seconds=180)  # three missed one-minute ticks
LATE_GRACE = timedelta(minutes=30)
UNHEALTHY = frozenset({"stuck", "overdue"})
DEGRADED = frozenset({"failing"})
STATES = ("stuck", "overdue", "failing", "running", "never_run", "disabled", "ok")


def worker_check(now=None):
    now = now or timezone.now()
    beat = WorkerHeartbeat.objects.filter(name="worker").first()
    if beat is None:
        return {"state": "never", "last_tick": None, "age_seconds": None, "stale_after_seconds": _seconds(STALE_AFTER)}
    age = now - beat.last_tick
    return {
        "state": "ok" if age <= STALE_AFTER else "stale",
        "last_tick": beat.last_tick,
        "age_seconds": max(0, int(age.total_seconds())),
        "stale_after_seconds": _seconds(STALE_AFTER),
    }


def _seconds(delta):
    return int(delta.total_seconds())


def _when(moment):
    return f"{moment:%Y-%m-%d %H:%M} UTC" if moment is not None else "(time not recorded)"


def schedule_state(row, now):
    """(state, detail) for one schedule's row (None when the worker hasn't created it yet)."""
    if row is None:
        return "never_run", "The worker hasn't seen this schedule yet."
    if not row.enabled:
        return "disabled", "Turned off by an operator."
    if row.lease_token is not None and row.lease_expires is not None:
        started = row.last_started
        if row.lease_expires < now or (started is not None and now - started > jobs.SCHEDULE_CEILING):
            return "stuck", f"Started {_when(started)}; no outcome before its lease expired."
        return "running", f"Running since {_when(row.last_started)}."
    if row.next_due + LATE_GRACE < now:
        return "overdue", f"Due at {_when(row.next_due)}; it hasn't started."
    if row.last_finished is not None and (row.last_succeeded is None or row.last_succeeded < row.last_finished):
        return "failing", row.last_error or "The last run failed."
    if row.last_started is None:
        return "never_run", ""
    return "ok", ""


def schedule_checks(now=None):
    """One check per registered schedule, in registration order, then any row the code no longer registers (shown
    as `retired`, never counted against health)."""
    now = now or timezone.now()
    rows = {row.name: row for row in Schedule.objects.all()}
    checks = []
    for definition in jobs.SCHEDULES.values():
        row = rows.pop(definition.name, None)
        state, detail = schedule_state(row, now)
        checks.append(_check(definition.name, definition.label, definition.cron, row, state, detail))
    for row in sorted(rows.values(), key=lambda r: r.name):
        checks.append(_check(row.name, row.name, row.cron, row, "retired", "No longer part of this version."))
    return checks


def _check(name, label, cron, row, state, detail):
    return {
        "name": name,
        "label": label,
        "cron": cron,
        "enabled": row.enabled if row is not None else True,
        "state": state,
        "detail": detail[: jobs.ERROR_MAX],
        "next_due": row.next_due if row is not None else None,
        "last_started": row.last_started if row is not None else None,
        "last_finished": row.last_finished if row is not None else None,
        "last_succeeded": row.last_succeeded if row is not None else None,
        "last_error": (row.last_error if row is not None else "")[: jobs.ERROR_MAX],
        "last_result": row.last_result if row is not None else {},
    }


def overall(worker, checks):
    states = {c["state"] for c in checks}
    if worker["state"] != "ok" or states & UNHEALTHY:
        return "unhealthy"
    if states & DEGRADED:
        return "degraded"
    return "ok"


def status(now=None):
    """Worker check, schedule checks, the overall state, and the backup block with its warnings (core/backup.py:
    `backup_stale` after SOFTWARDEN_BACKUP_STALE_HOURS, `backup_failed`, `backup_key_missing`,
    `backup_key_unacknowledged`). Warnings never change the overall state, so `/readyz` is unaffected by backups."""
    now = now or timezone.now()
    worker = worker_check(now)
    checks = schedule_checks(now)
    backup_block = backup.status(now)
    return {
        "status": overall(worker, checks),
        "checked_at": now,
        "worker": worker,
        "schedules": checks,
        "backup": backup_block,
        "warnings": backup.warnings(backup_block) + secrets_warnings(),
    }


def secrets_warnings(organization_id=None) -> list[dict[str, str]]:
    """`stored_secrets_unreadable` while any stored secret was encrypted under a key that isn't configured (a restore
    into a stack whose secrets volume has a new credential key). Installation-wide like the
    backup warnings, so the message gives no count; with `organization_id` (the System page's organization view)
    only that organization's own secrets count."""
    from . import credentials

    if not credentials.unreadable_count(organization_id):
        return []
    return [
        {
            "code": "stored_secrets_unreadable",
            "message": "Some stored secrets were encrypted under a credential key this installation doesn't have, "
            "so they can't be read.",
        }
    ]


# ---- /healthz/ops (softwarden/ops.py) -------------------------------------------------------------------------------

FAILED_WINDOW = timedelta(hours=24)
# The backup block fields the operator view repeats: state and timings, never the key or anything derived from it.
OPS_BACKUP_FIELDS = (
    "state",
    "last_success_at",
    "age_seconds",
    "stale_after_seconds",
    "last_failure_at",
    "last_failure",
    "running",
    "key_present",
    "key_acknowledged",
)


def queue_by_lane(now):
    """{lane: {"queued", "oldest_age_seconds"}} over every organization's jobs that are due (run_after passed). A kind
    this version doesn't register counts in the default lane, as the worker claims it there."""
    long_kinds = jobs.lane_kinds(jobs.LONG_LANE)
    in_long = Q(kind__in=long_kinds)
    row = Job.objects.filter(status=JobStatus.QUEUED, run_after__lte=now).aggregate(
        long_count=Count("id", filter=in_long),
        long_oldest=Min("run_after", filter=in_long),
        default_count=Count("id", filter=~in_long),
        default_oldest=Min("run_after", filter=~in_long),
    )
    lanes = {}
    for lane, prefix in ((jobs.DEFAULT_LANE, "default"), (jobs.LONG_LANE, "long")):
        oldest = row[f"{prefix}_oldest"]
        lanes[lane] = {
            "queued": row[f"{prefix}_count"],
            "oldest_age_seconds": max(0, int((now - oldest).total_seconds())) if oldest else None,
        }
    return lanes


def finished_badly(now):
    """Jobs (every organization's and installation-wide) that failed or were abandoned in the last FAILED_WINDOW."""
    row = Job.objects.filter(finished_at__gte=now - FAILED_WINDOW).aggregate(
        failed=Count("id", filter=Q(status=JobStatus.FAILED)),
        abandoned=Count("id", filter=Q(status=JobStatus.ABANDONED)),
    )
    return {"failed_24h": row["failed"], "abandoned_24h": row["abandoned"]}


def disk():
    """Free space on the media and backup paths, where this container has them. The backup path counts only when it
    is a mount point (unless SOFTWARDEN_BACKUP_REQUIRE_MOUNT=0): in the default compose only the worker mounts the
    backup volume, and the app's empty folder would report the image's own disk."""
    paths = {"media": Path(settings.MEDIA_ROOT)}
    destination = backup.destination()
    if destination is not None and (os.path.ismount(destination) or not settings.BACKUP_REQUIRE_MOUNT):
        paths["backups"] = destination
    found = {}
    for name, path in paths.items():
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        found[name] = {
            "path": str(path),
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "free_percent": round(100 * usage.free / usage.total, 1) if usage.total else None,
        }
    return found


def ops_detail(now=None):
    """Everything on /healthz/ops after the database checks: installation-wide figures only, no organization's name
    or data, no secrets. Schedules are listed by name (code-defined) when they are in a bad state."""
    now = now or timezone.now()
    worker = worker_check(now)
    checks = schedule_checks(now)
    block = backup.status(now)
    problems = {
        state: sorted(c["name"] for c in checks if c["state"] == state) for state in ("stuck", "overdue", "failing")
    }
    return {
        "status": overall(worker, checks),
        "worker": {k: worker[k] for k in ("state", "last_tick", "age_seconds", "stale_after_seconds")},
        "schedules": {"count": len(checks), **problems},
        "jobs": {"lanes": queue_by_lane(now), **finished_badly(now)},
        "backup": {k: block[k] for k in OPS_BACKUP_FIELDS},
        "disk": disk(),
    }
