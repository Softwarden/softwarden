"""The hourly status refresh of ticket links (`tickets.refresh`).

The schedule (at :35 past every hour) queues one `tickets.refresh` job per organization that has an enabled ticket
connection tested at its current revision and at least one live link due for a read (open, in progress or unknown,
with a key, not read in the last STALE_AFTER). Suspended organizations are skipped, and so are systems the
organization can't use (ServiceNow and Halo while the installation's preview features are off).

The job runs in the worker's long lane (core/worker.LongLane), never inline in the tick, so a slow or black-holed
ticket system can't hold up schedules or other organizations' work. Each run is bounded:

- a wall-clock budget (BUDGET): no new read starts after it, so a run ends within BUDGET plus one request's deadline
  (tickets/clients.DEADLINE);
- per system, up to PER_RUN links, the longest unread first; the first failure that says the system itself is in
  trouble (refused credentials, a refused address, a timeout, no answer, busy, access refused) stops that system's
  reads for this run: every further read would most likely fail the same way, slowly;
- a Halo token is requested once per run, not per link;
- the worker's stop flag (`ctx.stopping()`) is checked between reads.

Each read goes through tickets/services.read_status (core/outbound, the connection's own address, never the link's
URL) and is recorded with store_status. Resolved and closed links are never read again unless their key or system
changes or an Editor sets them back to open. Job payloads hold no secret (core/jobs checks): the job reads the
credentials from the connection row when it runs.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from django.db.models import F, Q
from django.utils import timezone

from core import jobs
from core.capabilities import TICKET_LINKS, has_capability
from core.models import Job, JobStatus, suspended_organization_ids

from . import clients, services
from .models import LIVE_STATUSES, TicketConnection, TicketLink, TicketSystem

JOB = "tickets.refresh"
SCHEDULE = "tickets.refresh"
CRON = "35 * * * *"
PER_RUN = 200
STALE_AFTER = timedelta(minutes=50)
BUDGET = 180.0  # seconds of reading per run
# A failure about the system rather than the ticket: stop reading that system for this run. not_found (one ticket)
# and bad_answer (one odd answer) don't stop it.
STOP_KINDS = ("auth", "refused", "timeout", "unreachable", "busy", "forbidden")


def due(org_id, system: str, now) -> Any:
    return (
        TicketLink.objects.filter(organization_id=org_id, system=system, status__in=LIVE_STATUSES)
        .exclude(external_key="")
        .filter(Q(status_checked_at__isnull=True) | Q(status_checked_at__lt=now - STALE_AFTER))
    )


def usable_connections():
    return TicketConnection.objects.filter(enabled=True, tested_revision=F("revision")).exclude(
        organization_id__in=suspended_organization_ids()
    )


def scheduled(now) -> dict[str, Any]:
    queued = 0
    pending = set(
        Job.objects.filter(kind=JOB, status__in=[JobStatus.QUEUED, JobStatus.RUNNING]).values_list(
            "organization_id", flat=True
        )
    )
    seen: set = set()
    for conn in usable_connections().select_related("organization").order_by("organization_id", "system"):
        org = conn.organization
        if org.pk in seen or org.pk in pending:
            continue
        if not has_capability(org, TICKET_LINKS) or not services.system_available(org, conn.system):
            continue  # a preview system the installation keeps off is never read (its connection and links stay)
        if not due(org.pk, conn.system, now).exists():
            continue
        seen.add(org.pk)
        jobs.enqueue(JOB, {}, organization=org)
        queued += 1
    return {"queued": queued}


def refresh_org(
    org,
    now=None,
    renew: Callable[[], Any] | None = None,
    limit: int = PER_RUN,
    *,
    stopping: Callable[[], bool] | None = None,
    budget: float = BUDGET,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    """Read the organization's due links, system by system, within `budget` seconds. Returns counts."""
    now = now or timezone.now()
    counts = {"read": 0, "changed": 0, "failed": 0, "skipped_systems": 0, "stopped_systems": 0, "out_of_time": 0}
    if not has_capability(org, TICKET_LINKS):
        return counts
    deadline = clock() + budget

    def must_end() -> bool:
        return clock() >= deadline or bool(stopping and stopping())

    for system in services.available_systems(org):  # never a preview system the installation keeps off
        if must_end():
            counts["out_of_time"] = 1
            break
        conn = services.connection_for(org, system)
        if conn is None:
            continue
        try:
            credential = services.credential_of(conn)
        except services.NotUsable:
            counts["skipped_systems"] += 1
            continue
        rows = list(due(org.pk, system, now).order_by(F("status_checked_at").asc(nulls_first=True), "id")[:limit])
        if not rows:
            continue
        token = None
        if system == TicketSystem.HALO:
            try:
                token = clients.halo_token(credential)
            except clients.TicketError:
                counts["stopped_systems"] += 1
                continue
        for link in rows:
            if must_end():
                counts["out_of_time"] = 1
                break
            before = link.status
            state, error, kind = services.read_status(link, credential, token=token)
            stored = services.store_status(org, None, link.pk, state, error, now=timezone.now())
            if state is None:
                counts["failed"] += 1
            else:
                counts["read"] += 1
                if stored is not None and stored.status != before:
                    counts["changed"] += 1
            if renew is not None:
                renew()
            if kind in STOP_KINDS:
                counts["stopped_systems"] += 1
                break
        if counts["out_of_time"]:
            break
    return counts


def run_job(job, ctx) -> dict[str, int]:
    org = job.organization
    if org is None:
        raise jobs.PermanentError("A ticket refresh needs an organization.")
    return refresh_org(org, renew=ctx.renew, stopping=ctx.stopping)


def register() -> None:
    # The long lane: reads wait on other organizations' servers, so they never run inline in the worker's tick.
    jobs.register_job(JOB, run_job, lease=timedelta(minutes=15), max_attempts=1, lane=jobs.LONG_LANE)
    jobs.register_schedule(SCHEDULE, scheduled, CRON, "Read the status of open ticket links")
