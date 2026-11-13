"""Background jobs and schedules: PostgreSQL-backed, no broker.

Registry. Apps register their work here from `AppConfig.ready`, so core never imports them:

- `register_job(kind, func, ...)`: a callable run for one queued Job row, `func(job, ctx)`. `ctx.renew()` extends the
  lease at checkpoints of long work; it raises LeaseLost once another worker has taken the job over. A kind in the
  `long` lane (discovery runs, up to 40 minutes against a slow tenant) is run by the worker in threads of its own
  (core/worker.LongLane), so it never holds up the tick's schedules or the short jobs; the lane claims the oldest job
  of an organization that has none running first, so one organization's runs can't take every slot.
- `register_schedule(name, func, cron, label)`: a recurring task, `func(now)`, fired by the worker when its cron time
  comes. It runs in the worker's tick and its outcome is recorded on the Schedule row.

Jobs. `enqueue` validates the kind and the payload (JSON, small, no secret-looking keys: task arguments
never carry secrets; pass the id of the row that holds the ciphertext). `claim` takes the oldest due job with
`SELECT ... FOR UPDATE SKIP LOCKED`, so concurrent workers never claim the same row, and gives it a fresh lease token.
Only the holder of a live token records the outcome. A failure is retried with backoff until
`max_attempts`; `PermanentError` fails at once. A running job whose lease expired is marked ABANDONED by the next
tick and not retried: a job that kills its worker would otherwise kill the next one too.

Schedules. A schedule fires when `next_due` has passed and no live lease holds it. Firing is one conditional UPDATE
that moves `next_due` to the next cron time and takes the lease, so with two workers exactly one runs each fire.
Missed fires are not caught up: after downtime a schedule runs once and then follows its cron.

Nothing here writes an AuditEvent: jobs and schedules are worker bookkeeping, not organization records. A job that
changes records does so through the owning app's services, which audit as usual."""

import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import InterfaceError, OperationalError, connection, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from . import credentials
from . import cron as cronlib
from .models import Job, JobStatus, Schedule, suspended_organization_ids
from .request_context import current_impersonator, impersonating, log_context, unit_of_work

log = logging.getLogger("softwarden.worker")

DEFAULT_LEASE = timedelta(minutes=10)
# A schedule run holds a short lease, renewed every KEEPER_INTERVAL by the worker's heartbeat thread while the run
# lasts, so a killed worker frees its schedule within minutes. Renewal stops at SCHEDULE_CEILING after the start: a
# run still going then lets its lease lapse and shows as stuck (as discovery's STUCK_AFTER).
SCHEDULE_LEASE = timedelta(minutes=5)
SCHEDULE_CEILING = timedelta(hours=1)
BACKOFF = timedelta(minutes=1)  # retry n waits BACKOFF * 4^(n-1): 1, 4, 16 minutes
PAYLOAD_MAX_BYTES = 16 * 1024
ERROR_MAX = 500
RESULT_KEYS = 30
RESULT_TEXT = 200
KEEP_FINISHED = timedelta(days=30)

# Payload keys that look like a secret (the audit denylist's words, catalog/services.py, plus a few more). A payload
# holding one is refused; keys like `connector_id` are what a job should carry instead.
# Keys are matched after splitting camelCase into snake_case (`smtpPass` -> `smtp_pass`), so `pass` is caught at a
# word or camelCase boundary but not inside `bypass` or `compass`. A key ending in `_id` (`token_id`) names a row,
# not a secret, and is allowed, except a session id: a session key is itself a bearer secret. Webhook URLs are
# secrets too.
SECRET_KEY_PATTERN = re.compile(
    r"password|passwd|(?:^|[^a-z])pass(?:$|[^a-z])|secret|token|api_?key|private_?key|credential|"
    r"authorization|cookie|session|csrf|ciphertext|licen[cs]e_?key|webhook"
)


def _normalized_key(key):
    snake = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", str(key))  # SMTPPass -> SMTP_Pass
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", snake)  # smtpPass -> smtp_Pass
    return re.sub(r"[^a-z0-9]+", "_", snake.lower())


def secret_looking(key):
    name = _normalized_key(key)
    if name.endswith("_id") and "session" not in name:
        return False
    return SECRET_KEY_PATTERN.search(name) is not None


class LeaseLost(Exception):
    """This worker no longer holds the job's lease: it must stop and record nothing."""


class PermanentError(Exception):
    """Raised by a job that retrying can't fix (a missing row, an invalid payload): fail now, no retry."""


DEFAULT_LANE = "default"
LONG_LANE = "long"
LANES = (DEFAULT_LANE, LONG_LANE)


@dataclass(frozen=True)
class JobKind:
    kind: str
    func: Callable[..., Any]
    lease: timedelta
    max_attempts: int
    lane: str = DEFAULT_LANE


@dataclass(frozen=True)
class ScheduleDef:
    name: str
    func: Callable[..., Any]
    cron: str
    label: str
    lease: timedelta


JOB_KINDS: dict[str, JobKind] = {}
SCHEDULES: dict[str, ScheduleDef] = {}  # in registration order, which is the order they fire in a tick


def register_job(kind, func, *, lease=DEFAULT_LEASE, max_attempts=3, lane=DEFAULT_LANE):
    if not kind or len(kind) > 80:
        raise ValueError("A job kind is 1 to 80 characters.")
    if lane not in LANES:
        raise ValueError(f"A job lane is one of {', '.join(LANES)}.")
    existing = JOB_KINDS.get(kind)
    if existing is not None and existing.func is not func:
        raise ValueError(f"Job kind {kind} is already registered.")
    JOB_KINDS[kind] = JobKind(kind, func, lease, max_attempts, lane)


def lane_kinds(lane):
    """The registered kinds of a lane. A kind this version doesn't register is in the default lane (it fails at once)."""
    return sorted(k.kind for k in JOB_KINDS.values() if k.lane == lane)


def register_schedule(name, func, cron, label, *, lease=SCHEDULE_LEASE):
    if not name or len(name) > 80:
        raise ValueError("A schedule name is 1 to 80 characters.")
    cronlib.parse(cron)  # a bad expression fails at start-up, not at its first fire
    existing = SCHEDULES.get(name)
    if existing is not None and existing.func is not func:
        raise ValueError(f"Schedule {name} is already registered.")
    SCHEDULES[name] = ScheduleDef(name, func, cron, label, lease)


# ---- errors and results ---------------------------------------------------------------------------------------------


def _secret_values():
    """Configured secrets that an exception message might echo (a password in an SMTP error, a key in a URL)."""
    values: list[str] = [
        settings.SECRET_KEY,
        getattr(settings, "EMAIL_HOST_PASSWORD", ""),
        os.environ.get("SOFTWARDEN_OIDC_CLIENT_SECRET", ""),
        os.environ.get("POSTGRES_PASSWORD", ""),
        str(settings.DATABASES["default"].get("PASSWORD", "")),
        getattr(settings, "SOFTWARDEN_ASSISTANT_INSTALLATION_KEY", ""),  # ANTHROPIC_API_KEY (discovery/assistant.py)
        getattr(settings, "OPS_TOKEN", ""),  # the /healthz/ops bearer token (softwarden/ops.py)
    ]
    # Key lists are comma-separated (core/credentials.py): each key on its own as well as the whole list, plus the
    # keys read from a key file or the generated secrets volume.
    for name in ("SOFTWARDEN_SECRETS_KEY", "SOFTWARDEN_CREDENTIAL_KEYS"):
        raw = os.environ.get(name, "")
        values += [part.strip() for part in raw.split(",")] + [raw]
    values += credentials.configured_key_values()
    from . import backup  # the backup key (core/backup.py imports this module)

    values.append(backup.key_value())
    # Longest first, so a whole list is replaced before its parts.
    return sorted({v for v in values if v and len(v) >= 6}, key=len, reverse=True)


def safe_error(error):
    """`Type: message`, with configured secrets replaced and cut to ERROR_MAX characters. Stored on a job and shown to
    Admins; a schedule stores only the type and logs this text (see run_schedule)."""
    text = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
    return redact_configured(text)[:ERROR_MAX]


def redact_configured(text):
    """`text` with every configured secret replaced, not shortened: callers that add their own secrets redact the
    whole text first and cut it last, so a secret near the cut can't survive as a prefix."""
    for value in _secret_values():
        text = text.replace(value, "[redacted]")
    return text


def summarize(result, depth=2):
    """A small JSON-safe summary of a callable's return value: scalars and nested dicts two
    levels deep; lists become counts; keys and text are capped. Anything else is dropped."""

    def value(v, level):
        if isinstance(v, bool | int | float) or v is None:
            return v
        if isinstance(v, str):
            return v[:RESULT_TEXT]
        if isinstance(v, dict):
            if level >= depth:
                return {"count": len(v)}
            return {str(k)[:RESULT_TEXT]: value(x, level + 1) for k, x in list(v.items())[:RESULT_KEYS]}
        if isinstance(v, list | tuple | set):
            return {"count": len(v)}
        return None

    if isinstance(result, dict):
        return {str(k)[:RESULT_TEXT]: value(v, 1) for k, v in list(result.items())[:RESULT_KEYS]}
    if isinstance(result, list | tuple):
        return {"count": len(result)}
    if isinstance(result, bool | int | float | str):
        return {"value": value(result, 1)}
    return {}


# ---- enqueue ----------------------------------------------------------------------------------------------------------


def _secret_keys(value, path=""):
    if isinstance(value, dict):
        for k, v in value.items():
            here = f"{path}.{k}" if path else str(k)
            if secret_looking(k):
                yield here
            yield from _secret_keys(v, here)
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            yield from _secret_keys(v, f"{path}[{i}]")


def check_payload(payload):
    """ValueError unless the payload is a small JSON object with no secret-looking key."""
    if not isinstance(payload, dict):
        raise ValueError("A job payload is a JSON object.")
    try:
        encoded = json.dumps(payload)
    except (TypeError, ValueError) as error:
        raise ValueError("A job payload must be JSON: pass ids and plain values.") from error
    if len(encoded.encode()) > PAYLOAD_MAX_BYTES:
        raise ValueError(f"A job payload is at most {PAYLOAD_MAX_BYTES} bytes; pass the id of a stored row instead.")
    found = list(_secret_keys(payload))
    if found:
        raise ValueError(
            f"Job payloads never carry secrets; refused key {found[0]}. Pass the id of the row that holds it."
        )


def enqueue(kind, payload=None, organization=None, run_after=None, max_attempts=None):
    """Queue one job. The worker picks it up within a tick of `run_after` (default now). Returns the Job."""
    registered = JOB_KINDS.get(kind)
    if registered is None:
        raise ValueError(f"Unknown job kind {kind}.")
    payload = payload or {}
    staff = current_impersonator()
    if staff is not None:  # the job's audit events keep who queued it while impersonating
        payload = {**payload, IMPERSONATED_BY: staff}
    check_payload(payload)
    return Job.objects.create(
        organization=organization,
        kind=kind,
        payload=payload,
        run_after=run_after or timezone.now(),
        max_attempts=max_attempts or registered.max_attempts,
    )


# ---- claim, renew, finish -------------------------------------------------------------------------------------------


def _lease_for(kind):
    registered = JOB_KINDS.get(kind)
    return registered.lease if registered is not None else DEFAULT_LEASE


def claim(now=None, *, lane=None, ids=None):
    """Take the oldest due queued job, or None. Rows another transaction has locked are skipped, never waited on,
    so two workers claiming at once get different jobs. The lease starts now; attempts counts this claim.

    `lane` limits the claim to one lane's kinds (None: any job, as `run_worker --once` and tests do). The long lane is
    fair between organizations: a job of an organization with no long job running comes before one whose organization
    already has one, then the oldest first. `ids` limits it to those jobs (the e2e helper runs one organization's)."""
    now = now or timezone.now()
    with transaction.atomic():
        queued = Job.objects.select_for_update(skip_locked=True).filter(status=JobStatus.QUEUED, run_after__lte=now)
        # A suspended organization's jobs stay queued until it is unsuspended (catalog/platform_admin.py).
        queued = queued.exclude(organization_id__in=suspended_organization_ids())
        order: list[Any] = ["run_after", "created_at"]
        if lane == LONG_LANE:
            long_kinds = lane_kinds(LONG_LANE)
            queued = queued.filter(kind__in=long_kinds)
            busy = Exists(
                Job.objects.filter(
                    status=JobStatus.RUNNING, kind__in=long_kinds, organization_id=OuterRef("organization_id")
                )
            )
            order = [busy.asc(), *order]
        elif lane is not None:
            queued = queued.exclude(kind__in=lane_kinds(LONG_LANE))
        if ids is not None:
            queued = queued.filter(id__in=list(ids))
        job = queued.order_by(*order).first()
        if job is None:
            return None
        job.status = JobStatus.RUNNING
        job.lease_token = uuid.uuid4()
        job.lease_expires = now + _lease_for(job.kind)
        job.attempts += 1
        job.started_at = now
        job.finished_at = None
        job.save(update_fields=["status", "lease_token", "lease_expires", "attempts", "started_at", "finished_at"])
    return job


def renew(job, now=None):
    """Extend a running job's lease. LeaseLost if this worker no longer holds it."""
    now = now or timezone.now()
    updated = Job.objects.filter(id=job.id, status=JobStatus.RUNNING, lease_token=job.lease_token).update(
        lease_expires=now + _lease_for(job.kind), updated_at=now
    )
    if not updated:
        raise LeaseLost
    job.lease_expires = now + _lease_for(job.kind)


def _owned(job):
    """Inside a transaction: the job row, locked, if this worker still holds its lease."""
    owned = (
        Job.objects.select_for_update().filter(id=job.id, status=JobStatus.RUNNING, lease_token=job.lease_token).first()
    )
    if owned is None:
        raise LeaseLost
    return owned


def succeed(job, result=None):
    with transaction.atomic():
        owned = _owned(job)
        owned.status = JobStatus.SUCCEEDED
        owned.result = summarize(result)
        owned.last_error = ""
        owned.finished_at = timezone.now()
        owned.lease_token = owned.lease_expires = None
        owned.save(update_fields=["status", "result", "last_error", "finished_at", "lease_token", "lease_expires"])
    return owned


def fail(job, error, now=None):
    """Record a failed attempt: queued again after a backoff while attempts remain, otherwise FAILED."""
    now = now or timezone.now()
    with transaction.atomic():
        owned = _owned(job)
        owned.last_error = safe_error(error)
        owned.lease_token = owned.lease_expires = None
        if isinstance(error, PermanentError) or owned.attempts >= owned.max_attempts:
            owned.status = JobStatus.FAILED
            owned.finished_at = now
        else:
            owned.status = JobStatus.QUEUED
            owned.run_after = now + BACKOFF * (4 ** (owned.attempts - 1))
        owned.save(update_fields=["status", "last_error", "finished_at", "run_after", "lease_token", "lease_expires"])
    return owned


@dataclass
class JobContext:
    """What a running job may ask of the worker: `renew()` its lease, and whether the worker is `stopping()` (its long
    lane was told to stop: a long job should record its own outcome now and return)."""

    job: Job
    should_stop: Callable[[], bool] | None = None

    def renew(self):
        renew(self.job)

    def stopping(self) -> bool:
        return bool(self.should_stop and self.should_stop())


def run(job, now=None, should_stop=None):
    """Run a claimed job and record its outcome. Returns the job's final status, or None if the lease was lost (an
    abandoned job's late result is discarded). `should_stop` is the worker's stop flag, passed to the job's context.
    Every log line written meanwhile carries the job's id as its request_id, and its organization's id."""
    from softwarden import otel

    with log_context(request_id=job.id, org=job.organization_id), otel.job_span(job):
        return _run(job, now, should_stop)


# Payload key: the platform staff account (User id) that queued the job while impersonating (core/impersonation.py).
IMPERSONATED_BY = "audit_impersonated_by"


def _impersonator_of(job) -> int | None:
    value = (job.payload or {}).get(IMPERSONATED_BY)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _run(job, now, should_stop):
    registered = JOB_KINDS.get(job.kind)
    clock = time.monotonic()
    try:
        if registered is None:
            raise PermanentError(f"No job kind {job.kind} is registered in this version.")
        # The job's audit events carry its id as their request id, and the platform staff account that queued it while
        # impersonating as `impersonated_by`.
        with unit_of_work(job.id), impersonating(_impersonator_of(job)):
            result = registered.func(job, JobContext(job, should_stop))
    except LeaseLost:
        log.warning("job lost its lease id=%s kind=%s; outcome discarded", job.id, job.kind)
        return None
    except Exception as error:  # every failure is recorded on the job, never raised to the worker loop
        try:
            outcome = fail(job, error, now)
        except LeaseLost:
            log.warning("job failed after losing its lease id=%s kind=%s", job.id, job.kind)
            return None
        log.warning(
            "job %s id=%s kind=%s attempt=%s/%s error=%s took=%.2fs",
            "failed" if outcome.status == JobStatus.FAILED else "will retry",
            job.id,
            job.kind,
            outcome.attempts,
            outcome.max_attempts,
            type(error).__name__,
            time.monotonic() - clock,
        )
        return outcome.status
    try:
        succeed(job, result)
    except LeaseLost:
        log.warning("job finished after losing its lease id=%s kind=%s; result discarded", job.id, job.kind)
        return None
    log.info(
        "job succeeded id=%s kind=%s attempt=%s took=%.2fs", job.id, job.kind, job.attempts, time.monotonic() - clock
    )
    return JobStatus.SUCCEEDED


def abandon_expired(now=None):
    """Mark running jobs and schedule runs whose lease expired as abandoned. Returns (jobs, schedules)."""
    now = now or timezone.now()
    jobs = 0
    with transaction.atomic():
        expired = list(
            Job.objects.select_for_update(skip_locked=True).filter(status=JobStatus.RUNNING, lease_expires__lt=now)
        )
        for job in expired:
            job.status = JobStatus.ABANDONED
            job.finished_at = now
            when = f" started {job.started_at:%Y-%m-%d %H:%M} UTC" if job.started_at else ""
            job.last_error = (
                f"Abandoned: attempt {job.attempts}{when} and its lease expired without an outcome (the worker "
                "stopped or stalled)."
            )
            job.lease_token = job.lease_expires = None
            job.save(update_fields=["status", "finished_at", "last_error", "lease_token", "lease_expires"])
            log.warning("job abandoned id=%s kind=%s attempt=%s", job.id, job.kind, job.attempts)
            jobs += 1
    schedules = 0
    for schedule in Schedule.objects.filter(lease_token__isnull=False, lease_expires__lt=now):
        started = schedule.last_started
        updated = Schedule.objects.filter(id=schedule.id, lease_token=schedule.lease_token).update(
            lease_token=None,
            lease_expires=None,
            last_finished=now,
            last_error=(
                f"Abandoned: the run started {started:%Y-%m-%d %H:%M} UTC had no outcome when its lease expired "
                "(the worker stopped or stalled)."
                if started
                else "Abandoned: the run had no outcome when its lease expired."
            ),
            updated_at=now,
        )
        if updated:
            log.warning("schedule run abandoned name=%s", schedule.name)
            schedules += 1
    return jobs, schedules


def prune(now=None):
    """Delete finished jobs older than KEEP_FINISHED. A schedule (core/apps.py)."""
    now = now or timezone.now()
    done = [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.ABANDONED]
    deleted, _ = Job.objects.filter(status__in=done, finished_at__lt=now - KEEP_FINISHED).delete()
    return {"jobs_pruned": deleted}


# ---- schedules ------------------------------------------------------------------------------------------------------


def sync_schedules(now=None):
    """Give every registered schedule a row and keep its cron in step with the code. A new row's first fire is the
    first cron time within the current minute or later, so an every-minute schedule fires on the tick that creates
    it and a nightly one waits for the night. A changed cron moves `next_due` to its next time."""
    now = now or timezone.now()
    rows = {s.name: s for s in Schedule.objects.filter(name__in=list(SCHEDULES))}
    for definition in SCHEDULES.values():
        parsed = cronlib.parse(definition.cron)
        row = rows.get(definition.name)
        if row is None:
            Schedule.objects.get_or_create(
                name=definition.name,
                defaults={"cron": definition.cron, "next_due": parsed.next_after(now - timedelta(minutes=1))},
            )
        elif row.cron != definition.cron:
            Schedule.objects.filter(id=row.id).update(cron=definition.cron, next_due=parsed.next_after(now))


# Schedule leases this process holds now: token -> (name, started, lease). The heartbeat thread renews them.
_held: dict[uuid.UUID, tuple[str, Any, timedelta]] = {}
_held_lock = threading.Lock()


def renew_held_schedules(now=None):
    """Extend the lease of every schedule run in progress in this process, up to SCHEDULE_CEILING after its start.
    Called by the worker's heartbeat thread. Returns how many were renewed."""
    now = now or timezone.now()
    with _held_lock:
        held = list(_held.items())
    renewed = 0
    for token, (name, started, lease) in held:
        if now - started >= SCHEDULE_CEILING:
            continue  # let it lapse: health shows the run as stuck
        renewed += Schedule.objects.filter(name=name, lease_token=token).update(
            lease_expires=min(now + lease, started + SCHEDULE_CEILING), updated_at=now
        )
    return renewed


def _take(definition, now):
    """Fire one schedule if it is due: its new lease token, or None when it isn't due, is disabled or another worker
    holds it. One UPDATE, so two workers can't both take the same fire."""
    token = uuid.uuid4()
    next_due = cronlib.parse(definition.cron).next_after(now)
    free = Q(lease_token__isnull=True) | Q(lease_expires__lt=now)
    taken = Schedule.objects.filter(free, name=definition.name, enabled=True, next_due__lte=now).update(
        next_due=next_due,
        lease_token=token,
        lease_expires=now + definition.lease,
        last_started=now,
        updated_at=now,
    )
    return token if taken else None


def _write_outcome(name, token, **fields):
    """Record a schedule run's outcome. The run may have left the connection dead (a database restart mid-run): on a
    connection error, reconnect and try once more, so the lease is released now rather than when it lapses."""
    for attempt in (1, 2):
        try:
            return Schedule.objects.filter(name=name, lease_token=token).update(**fields)
        except (InterfaceError, OperationalError):
            if attempt == 2 or connection.in_atomic_block:
                raise
            log.warning("schedule outcome not written name=%s; reconnecting", name)
            connection.close()
    return 0


def run_schedule(definition, now):
    """Fire `definition` if due and record the outcome. Returns ("skipped" | "succeeded" | "failed", result).

    A failure is stored on the schedule as the exception type only: schedules are installation-wide and every
    organization's Admins see them, while the message could carry one organization's data. The message, with
    configured secrets removed, goes to the log."""
    token = _take(definition, now)
    if token is None:
        return "skipped", None
    with _held_lock:
        _held[token] = (definition.name, now, definition.lease)
    clock = time.monotonic()
    run_id = uuid.uuid4()  # the run's audit events and log lines share it
    try:
        try:
            with log_context(request_id=run_id), unit_of_work(run_id):
                result = definition.func(now)
        except Exception as error:  # recorded on the schedule; the tick carries on with the others
            log.warning(
                "schedule failed name=%s error=%s took=%.2fs",
                definition.name,
                safe_error(error),
                time.monotonic() - clock,
                extra={"request_id": run_id},
            )
            _write_outcome(
                definition.name,
                token,
                lease_token=None,
                lease_expires=None,
                last_finished=timezone.now(),
                last_error=type(error).__name__,
                updated_at=timezone.now(),
            )
            return "failed", None
        finished = timezone.now()
        updated = _write_outcome(
            definition.name,
            token,
            lease_token=None,
            lease_expires=None,
            last_finished=finished,
            last_succeeded=finished,
            last_error="",
            last_result=summarize(result),
            updated_at=finished,
        )
        if not updated:
            log.warning(
                "schedule finished after losing its lease name=%s; outcome discarded",
                definition.name,
                extra={"request_id": run_id},
            )
        return "succeeded", result
    finally:
        with _held_lock:
            _held.pop(token, None)
