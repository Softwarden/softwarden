"""The delivery engine (a claim, send, record protocol on core/jobs.py).

`enqueue(...)` writes one Delivery whose `dedupe_key` is unique per organization and queues a `notify.deliver` job
for it, in the caller's transaction. A second enqueue with the same key changes nothing and returns the first row, so
a generator that runs again (or two at once) never sends twice. Job payloads carry the delivery's id only.

Each attempt (`attempt`):

1. **Claim**: under a row lock, a queued delivery whose time has come becomes `sending` with a fresh claim token, a
   lease (`claimed_until`) and a fresh `client_request_id`; `attempts` counts it. This commits **before** anything is
   sent: another worker skips the locked or claimed row, and the key was already unique before the call, which is how
   Softwarden dedupes given that Graph has no idempotency header.
2. **Send** on the channel (notify/channels.py), outside any transaction.
3. **Record** the outcome, only while the claim token still matches:
   - accepted: `sent`;
   - RETRY: `queued` again after the server's Retry-After (429, 503; capped at an hour) or else an exponential backoff
     of 1, 4, 16 minutes, until `max_attempts`, then `failed`; a new job is queued for the retry time;
   - AMBIGUOUS (a timeout or a broken connection after the request was written, a 5xx other than 503, an unexpected
     error): `failed` with the reason, never retried, since the message may have arrived;
   - PERMANENT: `failed`; SKIPPED (email not configured, destination off or deleted): `skipped`;
   - BUSY (four Graph sends in flight from the mailbox): `queued` again in 15 seconds, the attempt not counted.

Webhooks differ in two ways, because every post names its delivery (X-Softwarden-Delivery) so a receiver
can drop a repeat: an ambiguous outcome is retried like any other, and retries wait WEBHOOK_BACKOFF (1 minute,
5 minutes, 30 minutes, 2 hours, 6 hours, then 6 hours each) with up to 10 % jitter either way. A webhook delivery's
attempts are SOFTWARDEN_WEBHOOK_MAX_ATTEMPTS when it is an event (notify/webhooks.py).

Every attempt is logged as a DeliveryAttempt row (outcome, status code, duration, a scrubbed response excerpt, the
error and the next attempt's time), and a webhook's outcome updates its endpoint's summary and may turn it off
(notify/webhooks.record_outcome).

A claim whose lease runs out without an outcome (the worker died mid-send) is ambiguous too: the `notify.deliveries`
schedule marks it failed and doesn't retry it, as core/jobs.py abandons jobs. If the late worker does report back,
its outcome still wins (its claim token still matches). The same schedule re-queues a job for a queued delivery
whose job is gone. Not audited: deliveries are the log of what the system sent; a person's test send or resend is
audited where it is asked for (notify/channel_services.py)."""

from __future__ import annotations

import logging
import random
import re
import time
import urllib.parse
import uuid
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from core import jobs
from core.models import Job, JobStatus

from . import channels
from .channels import DeliveryFailure, Outcome
from .models import AttemptOutcome, Channel, Delivery, DeliveryAttempt, DeliveryKind, DeliveryStatus

log = logging.getLogger("softwarden.notify")

JOB_KIND = "notify.deliver"
MAX_ATTEMPTS = 4
BACKOFF = timedelta(minutes=1)  # retry n waits BACKOFF * 4^(n-1): 1, 4, 16 minutes
RETRY_AFTER_MIN = timedelta(seconds=1)
RETRY_AFTER_MAX = timedelta(hours=1)
BUSY_DELAY = timedelta(seconds=15)
LEASE = timedelta(minutes=5)
ORPHAN_AFTER = timedelta(minutes=10)
SWEEP_LIMIT = 200
KEY_MAX = 200
CONTENT_MAX_BYTES = 256 * 1024
ERROR_MAX = 500
EXCERPT_MAX = 500
# Webhook retries: retry n waits WEBHOOK_BACKOFF[n - 1] (the last one again after that), jittered by JITTER either way.
WEBHOOK_BACKOFF = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=6),
)
JITTER = 0.1
AMBIGUOUS_NOTE = "Not retried: the outcome is unknown. Check whether it arrived before you resend."
# A dedupe key names its audience by id or hash, never by address or URL.
ADDRESS_LIKE = re.compile(r"@|://")


def check_key(key: str) -> str:
    if not isinstance(key, str) or not key or len(key) > KEY_MAX:
        raise ValueError(f"A dedupe key is 1 to {KEY_MAX} characters.")
    if ADDRESS_LIKE.search(key):
        raise ValueError("A dedupe key names its audience by id or hash, never by an address or URL.")
    return key


def _queue_job(delivery: Delivery, run_after=None) -> Job:
    return jobs.enqueue(
        JOB_KIND,
        {"delivery_id": str(delivery.id)},
        organization=delivery.organization,
        run_after=run_after,
        max_attempts=1,
    )


def enqueue(
    org,
    *,
    channel: str,
    dedupe_key: str,
    subject: str,
    content: dict[str, Any],
    kind: str = DeliveryKind.REMINDER,
    recipient: str = "",
    teams_destination=None,
    webhook_destination=None,
    reminder=None,
    rule=None,
    rule_name: str = "",
    lead_time_days: int | None = None,
    overdue_step: int | None = None,
    due_date=None,
    requested_by=None,
    resend_of: Delivery | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    queue_job: bool = True,
    event_id: uuid.UUID | None = None,
) -> tuple[Delivery, bool]:
    """(delivery, created). Idempotent on `dedupe_key`: an existing delivery with that key is returned unchanged."""
    import json

    check_key(dedupe_key)
    if channel not in Channel.values:
        raise ValueError(f"Unknown channel {channel}.")
    if kind not in DeliveryKind.values:
        raise ValueError(f"Unknown delivery kind {kind}.")
    if channel == Channel.EMAIL and not recipient:
        raise ValueError("An email delivery needs a recipient.")
    if channel == Channel.TEAMS and teams_destination is None:
        raise ValueError("A Teams delivery needs a destination.")
    if channel == Channel.WEBHOOK and webhook_destination is None:
        raise ValueError("A webhook delivery needs a destination.")
    for destination in (teams_destination, webhook_destination, reminder, rule, resend_of):
        if destination is not None and destination.organization_id != org.id:
            raise ValueError("A delivery's references belong to its organization.")
    if len(json.dumps(content).encode()) > CONTENT_MAX_BYTES:
        raise ValueError("The rendered message is too large.")
    destination = teams_destination or webhook_destination
    with transaction.atomic():
        existing = Delivery.objects.filter(organization=org, dedupe_key=dedupe_key).first()
        if existing is not None:
            return existing, False
        try:
            with transaction.atomic():
                delivery = Delivery.objects.create(
                    organization=org,
                    channel=channel,
                    kind=kind,
                    dedupe_key=dedupe_key,
                    recipient=recipient if channel == Channel.EMAIL else "",
                    teams_destination=teams_destination if channel == Channel.TEAMS else None,
                    webhook_destination=webhook_destination if channel == Channel.WEBHOOK else None,
                    destination_name=getattr(destination, "name", "") if channel != Channel.EMAIL else "",
                    subject=subject[:300],
                    content=content,
                    reminder=reminder,
                    rule=rule,
                    rule_name=rule_name[:200],
                    lead_time_days=lead_time_days,
                    overdue_step=overdue_step,
                    due_date=due_date,
                    requested_by=requested_by,
                    resend_of=resend_of,
                    max_attempts=max_attempts,
                    next_attempt_at=timezone.now(),
                    event_type=str(content.get("event") or "")[:60] if channel == Channel.WEBHOOK else "",
                    event_id=event_id if channel == Channel.WEBHOOK else None,
                )
        except IntegrityError:  # another transaction wrote the key first
            return Delivery.objects.get(organization=org, dedupe_key=dedupe_key), False
        if queue_job:
            _queue_job(delivery)
    return delivery, True


# ---- claim, send, record --------------------------------------------------------------------------------------------


def claim(delivery_id, now=None) -> Delivery | None:
    """Take a queued delivery whose time has come, or None (sent, failed, claimed by another worker, not due). Commits
    the claim before returning."""
    now = now or timezone.now()
    with transaction.atomic():
        delivery = (
            Delivery.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("organization", "teams_destination", "webhook_destination")
            .filter(id=delivery_id, status=DeliveryStatus.QUEUED)
            .first()
        )
        if delivery is None or (delivery.next_attempt_at is not None and delivery.next_attempt_at > now):
            return None
        delivery.status = DeliveryStatus.SENDING
        delivery.claim_token = uuid.uuid4()
        delivery.claimed_until = now + LEASE
        delivery.client_request_id = uuid.uuid4()
        delivery.attempts += 1
        delivery.save(
            update_fields=["status", "claim_token", "claimed_until", "client_request_id", "attempts", "updated_at"]
        )
    return delivery


QUERY_VALUE_MIN = 16  # a query parameter value this long is treated as a secret on its own (a signature, a token)


def _variants(secret: str) -> set[str]:
    """A secret and, for a URL, its path with query on its own and its query on its own (http.client and servers echo
    the request line, `POST /path?sig=...`, without the scheme and host), each also percent-decoded (a server may
    echo the decoded form), and every query parameter value of QUERY_VALUE_MIN characters or more (a server may
    quote just the signature)."""
    found = {secret}
    try:
        parts = urllib.parse.urlsplit(secret)
    except ValueError:
        return found
    if parts.scheme and parts.netloc:
        tail = secret.split(parts.netloc, 1)[1]
        found |= {tail, parts.query}
        for _name, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=False):
            if len(value) >= QUERY_VALUE_MIN:
                found.add(value)
                found.add(urllib.parse.quote(value, safe=""))
    found |= {urllib.parse.unquote(v) for v in list(found)}
    return {v for v in found if len(v) >= 8 or v == secret}


def scrub(text: str, secrets: list[str]) -> str:
    """`text` with the channel's secrets (and their URL parts) and then every configured secret replaced. Never cut:
    redact the whole message first, then shorten it (`_cut`), so a secret can't survive as a prefix at the cut."""
    values = set().union(*(_variants(s) for s in secrets if s and len(s) >= 4)) if secrets else set()
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return jobs.redact_configured(text)


def _cut(message: str, note: str = "") -> str:
    """Shorten an already-scrubbed message to ERROR_MAX, keeping `note` whole at the end."""
    if not note:
        return message[:ERROR_MAX]
    room = ERROR_MAX - len(note) - 1
    return f"{message[:room]} {note}"


def jitter() -> float:
    """A factor within 1 ± JITTER (overridable in tests)."""
    return random.uniform(1 - JITTER, 1 + JITTER)  # noqa: S311 (spreading retries, not a secret)


def retry_delay(delivery: Delivery, retry_after: float | None) -> timedelta:
    if retry_after is not None:
        return min(max(timedelta(seconds=retry_after), RETRY_AFTER_MIN), RETRY_AFTER_MAX)
    if delivery.channel == Channel.WEBHOOK:
        base = WEBHOOK_BACKOFF[min(max(delivery.attempts - 1, 0), len(WEBHOOK_BACKOFF) - 1)]
        return base * jitter()
    return BACKOFF * (4 ** max(delivery.attempts - 1, 0))


def _record(delivery: Delivery, token: uuid.UUID, **fields) -> bool:
    """Write the outcome if this attempt's claim still holds (a sweep that gave up on it keeps the token, so a late
    report still lands). False when another attempt owns the row now."""
    fields.setdefault("claimed_until", None)
    fields["updated_at"] = timezone.now()
    updated = Delivery.objects.filter(
        id=delivery.id, claim_token=token, status__in=[DeliveryStatus.SENDING, DeliveryStatus.FAILED]
    ).update(**fields)
    if not updated:
        log.warning("delivery outcome arrived after its claim moved on id=%s", delivery.id)
    return bool(updated)


def record_failure(
    delivery: Delivery, token: uuid.UUID, failure: DeliveryFailure, secrets: list[str], now=None
) -> tuple[str, Any]:
    """Record a failed attempt. Returns (the delivery's status now, the next attempt's time or None)."""
    now = now or timezone.now()
    message = scrub(str(failure), secrets)  # whole; cut only when written below
    outcome = failure.outcome
    if delivery.channel == Channel.WEBHOOK and outcome == Outcome.AMBIGUOUS:
        outcome = Outcome.RETRY  # a receiver drops a repeat by its X-Softwarden-Delivery id
    with transaction.atomic():
        if outcome == Outcome.BUSY:
            when = now + BUSY_DELAY
            if _record(
                delivery,
                token,
                status=DeliveryStatus.QUEUED,
                attempts=max(delivery.attempts - 1, 0),
                next_attempt_at=when,
                claim_token=None,
            ):
                _queue_job(delivery, when)
            return DeliveryStatus.QUEUED, when
        if outcome == Outcome.RETRY and delivery.attempts < delivery.max_attempts:
            when = now + retry_delay(delivery, failure.retry_after)
            if _record(
                delivery,
                token,
                status=DeliveryStatus.QUEUED,
                next_attempt_at=when,
                last_error=_cut(message),
                claim_token=None,
            ):
                _queue_job(delivery, when)
            return DeliveryStatus.QUEUED, when
        if outcome == Outcome.SKIPPED:
            _record(delivery, token, status=DeliveryStatus.SKIPPED, next_attempt_at=None, last_error=_cut(message))
            return DeliveryStatus.SKIPPED, None
        note = ""
        if outcome == Outcome.AMBIGUOUS:
            note = AMBIGUOUS_NOTE
        elif outcome == Outcome.RETRY:
            note = f"Gave up after {delivery.attempts} attempts."
        _record(delivery, token, status=DeliveryStatus.FAILED, next_attempt_at=None, last_error=_cut(message, note))
        return DeliveryStatus.FAILED, None


def excerpt(body: str, secrets: list[str]) -> str:
    """A server's answer for the attempt log: scrubbed whole, then cut (as `_cut`)."""
    return scrub(body, secrets)[:EXCERPT_MAX] if body else ""


ATTEMPT_OUTCOMES: dict[str, str] = {
    DeliveryStatus.SENT: AttemptOutcome.SENT,
    DeliveryStatus.QUEUED: AttemptOutcome.RETRYING,
    DeliveryStatus.FAILED: AttemptOutcome.FAILED,
    DeliveryStatus.SKIPPED: AttemptOutcome.SKIPPED,
}


def log_attempt(
    delivery: Delivery,
    started_at,
    clock: float | None,
    status: str,
    *,
    status_code: int | None = None,
    body: str = "",
    error: str = "",
    next_attempt_at=None,
    secrets: list[str] | None = None,
) -> None:
    """One row of the attempt log. Never raises: the delivery's outcome is already recorded."""
    try:
        DeliveryAttempt.objects.create(
            organization_id=delivery.organization_id,
            delivery_id=delivery.id,
            number=delivery.attempts,
            started_at=started_at,
            duration_ms=None if clock is None else max(int((time.monotonic() - clock) * 1000), 0),
            outcome=ATTEMPT_OUTCOMES[status],
            status_code=status_code,
            response_excerpt=excerpt(body, secrets or []),
            error=error[:ERROR_MAX],
            next_attempt_at=next_attempt_at,
        )
    except Exception as e:
        log.warning("delivery attempt not logged id=%s error=%s", delivery.id, type(e).__name__)


def attempt(delivery_id, now=None) -> Delivery | None:
    """One attempt at a delivery: claim, send, record, log. Returns the delivery as stored afterwards, or None when
    there was nothing to claim."""
    from . import webhooks

    delivery = claim(delivery_id, now)
    if delivery is None:
        return None
    token = delivery.claim_token
    assert token is not None
    org = delivery.organization
    started_at, clock = timezone.now(), time.monotonic()
    failure: DeliveryFailure | None = None
    answer = None
    try:
        answer = channels.send(org, delivery)
    except DeliveryFailure as caught:
        failure = caught
    except Exception as error:  # a bug or an unexpected library error mid-send: the outcome is unknown
        # The raw text goes only to record_failure, which scrubs all of it before cutting.
        failure = DeliveryFailure(
            f"Unexpected error while sending ({type(error).__name__}: {error}).", Outcome.AMBIGUOUS
        )
        log.warning("delivery crashed id=%s channel=%s error=%s", delivery.id, delivery.channel, type(error).__name__)
    if failure is not None:
        secrets = channels.secrets_of(org, delivery)
        status, when = record_failure(delivery, token, failure, secrets, now)
        log.info(
            "delivery not sent id=%s channel=%s attempt=%s outcome=%s status=%s",
            delivery.id,
            delivery.channel,
            delivery.attempts,
            failure.outcome,
            status,
        )
        if failure.outcome != Outcome.BUSY:  # a busy mailbox isn't an attempt
            log_attempt(
                delivery,
                started_at,
                clock,
                status,
                status_code=failure.status_code,
                body=failure.body,
                error=_cut(scrub(str(failure), secrets)),
                next_attempt_at=when,
                secrets=secrets,
            )
        if delivery.channel == Channel.WEBHOOK and status != DeliveryStatus.SKIPPED:
            webhooks.record_outcome(
                delivery,
                "retrying" if status == DeliveryStatus.QUEUED else "failed",
                failure.status_code,
                final_failure=status == DeliveryStatus.FAILED,
                gone=failure.gone,
            )
    else:
        _record(
            delivery,
            token,
            status=DeliveryStatus.SENT,
            sent_at=timezone.now(),
            next_attempt_at=None,
            last_error="",
        )
        log.info("delivery sent id=%s channel=%s attempt=%s", delivery.id, delivery.channel, delivery.attempts)
        code = answer.status_code if answer is not None else None
        log_attempt(
            delivery,
            started_at,
            clock,
            DeliveryStatus.SENT,
            status_code=code,
            body=answer.body if answer is not None else "",
            secrets=channels.secrets_of(org, delivery) if answer is not None and answer.body else [],
        )
        if delivery.channel == Channel.WEBHOOK:
            webhooks.record_outcome(delivery, "sent", code, final_failure=False, gone=False)
    return Delivery.objects.select_related("teams_destination", "webhook_destination").get(id=delivery.id)


def run_job(job, ctx) -> dict[str, Any]:
    """The `notify.deliver` job: one attempt at the delivery named by the payload. A delivery that isn't due or is
    owned by another attempt is left alone."""
    delivery_id = (job.payload or {}).get("delivery_id")
    if not delivery_id:
        raise jobs.PermanentError("The job names no delivery.")
    done = attempt(delivery_id)
    return {"status": done.status if done is not None else "not_claimed"}


# ---- the schedule ---------------------------------------------------------------------------------------------------


def sweep(now=None) -> dict[str, int]:
    """The `notify.deliveries` schedule, every minute: a claim whose lease ran out becomes `failed` (ambiguous, not
    retried), except a webhook delivery with attempts left, which is queued again after its backoff with the stopped
    attempt logged. That can post the same delivery twice (the stopped worker may have reached the endpoint before it
    died, and a late outcome from it is no longer recorded): receivers deduplicate on X-Softwarden-Delivery, which a
    retry keeps; a queued delivery whose time came more than
    ORPHAN_AFTER ago and has no live job gets one."""
    from . import webhooks

    now = now or timezone.now()
    expired = 0
    for delivery in Delivery.objects.filter(status=DeliveryStatus.SENDING, claimed_until__lt=now)[:SWEEP_LIMIT]:
        claimed = Delivery.objects.filter(
            id=delivery.id, status=DeliveryStatus.SENDING, claim_token=delivery.claim_token
        )
        webhook = delivery.channel == Channel.WEBHOOK
        stopped = f"The worker stopped during attempt {delivery.attempts}"
        if webhook and delivery.attempts < delivery.max_attempts:
            when = now + retry_delay(delivery, None)
            with transaction.atomic():
                moved = claimed.update(
                    status=DeliveryStatus.QUEUED,
                    claim_token=None,
                    claimed_until=None,
                    next_attempt_at=when,
                    last_error=f"{stopped}; trying again.",
                    updated_at=now,
                )
                if moved:
                    _queue_job(delivery, when)
            status, error = DeliveryStatus.QUEUED, f"{stopped} mid-send; trying again."
        else:
            moved = claimed.update(
                status=DeliveryStatus.FAILED,
                claimed_until=None,
                next_attempt_at=None,
                last_error=f"{stopped}. {AMBIGUOUS_NOTE}" if not webhook else f"{stopped}. Gave up.",
                updated_at=now,
            )
            status, error, when = DeliveryStatus.FAILED, f"{stopped} mid-send.", None
        expired += moved
        if moved and webhook:
            started = delivery.claimed_until - LEASE if delivery.claimed_until else now
            log_attempt(delivery, started, None, status, error=error, next_attempt_at=when)
            webhooks.record_outcome(
                delivery,
                "retrying" if status == DeliveryStatus.QUEUED else "failed",
                None,
                final_failure=status == DeliveryStatus.FAILED,
                gone=False,
            )
    requeued = 0
    stale = Delivery.objects.filter(
        status=DeliveryStatus.QUEUED, next_attempt_at__lt=now - ORPHAN_AFTER
    ).select_related("organization")[:SWEEP_LIMIT]
    for delivery in stale:
        live = Job.objects.filter(
            kind=JOB_KIND, status__in=[JobStatus.QUEUED, JobStatus.RUNNING], payload__delivery_id=str(delivery.id)
        ).exists()
        if not live:
            _queue_job(delivery)
            requeued += 1
    return {"deliveries_expired": expired, "deliveries_requeued": requeued}


# ---- retention ------------------------------------------------------------------------------------------------------

PURGE_BATCH = 1000


def retention_days() -> tuple[int, int]:
    """(sent, failed or skipped): days a finished delivery and its attempts are kept (SOFTWARDEN_DELIVERY_RETENTION_*;
    at least 1)."""
    from django.conf import settings

    return (
        max(int(settings.SOFTWARDEN_DELIVERY_RETENTION_SENT_DAYS), 1),
        max(int(settings.SOFTWARDEN_DELIVERY_RETENTION_FAILED_DAYS), 1),
    )


def purge(now=None) -> dict[str, int]:
    """The `notify.retention` schedule, daily, every channel: deletes finished deliveries (with their attempt log) once
    they are older than the retention: sent ones after SOFTWARDEN_DELIVERY_RETENTION_SENT_DAYS (30), failed and skipped
    ones after SOFTWARDEN_DELIVERY_RETENTION_FAILED_DAYS (90), by when they were queued. Kept whatever their age:
    queued or sending ones; a live reminder's notices (notification rules read them as the record of which stages were
    sent, notify/rules.Ledger, so deleting them could send a stage again); and a digest's delivery (the digest log reads
    its status from it). Not audited: deliveries are the system's own log."""
    from django.db.models import Q

    from .models import LIVE_REMINDER_STATUSES, Digest

    now = now or timezone.now()
    sent_days, failed_days = retention_days()
    old = Q(status=DeliveryStatus.SENT, created_at__lt=now - timedelta(days=sent_days)) | Q(
        status__in=[DeliveryStatus.FAILED, DeliveryStatus.SKIPPED], created_at__lt=now - timedelta(days=failed_days)
    )
    candidates = (
        Delivery.objects.filter(old)
        .exclude(reminder__status__in=LIVE_REMINDER_STATUSES)
        .exclude(id__in=Digest.objects.filter(delivery__isnull=False).values("delivery_id"))
    )
    removed = 0
    while True:
        ids = list(candidates.values_list("id", flat=True)[:PURGE_BATCH])
        if not ids:
            break
        with transaction.atomic():
            DeliveryAttempt.objects.filter(delivery_id__in=ids).delete()
            Delivery.objects.filter(resend_of_id__in=ids).update(resend_of=None)
            removed += Delivery.objects.filter(id__in=ids).delete()[1].get("notify.Delivery", 0)
    return {"deliveries_purged": removed}
