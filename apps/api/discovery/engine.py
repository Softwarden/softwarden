"""Discovery (ADR-007): connectors, the run engine, and reconciliation decisions.

A source module (discovery/entra.py, entra_licenses.py, azure.py, powerplatform.py, halo.py, okta.py,
google_workspace.py, ...) declares its observation kinds, its config fields, its scope filters and how to map one source object; this engine is the same for every source,
so new sources add modules, not tables. A source that reads several scopes (Azure subscriptions, Power Platform
environments) lists the ones it couldn't read (`Fetched.unread`): their observations keep their state ("not read")
rather than becoming "no longer seen". Observations
are kept apart from curated records and never written back to the source. A decision links an observation to a
record, creates one from it, or dismisses it; nothing here changes a curated value.

Reconciliation adds, for linked and created observations: "source differs" per field (discovery/drift.py,
recomputed after every run, decision and record edit; applying one is an ordinary audited record edit), "no longer
seen in source" (discovery/missing.py: a gone observation is a Needs attention row until someone keeps or archives the
record; it clears itself when the object comes back), and, for licence observations, the seat sync onto pools that
sync their SKU (money/licences.sync_observed).

Each run is its own `discovery.run` job under a lease (below, "runs as jobs"); the schedule only queues."""

import hashlib
import json
import logging
import traceback
import uuid
from datetime import time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, F, FloatField, Func, OuterRef, Q, Value
from django.db.models.functions import Greatest
from django.utils import timezone

from catalog import services
from catalog.models import RECORD_KINDS, Kind, Lifecycle, Resource
from core import connections, jobs, limits, simulation
from core.capabilities import (
    PREVIEW,
    PREVIEW_SOURCES,
    SOURCE_CAPABILITY,
    has_capability,
    hidden_preview,
    require_capability,
)
from core.input import text_value, whole_number
from core.models import Job, JobStatus, Membership
from core.problems import Conflict, FieldErrors

from . import (
    azure,
    drift,
    entra,
    entra_coverage,
    entra_licence_usage,
    entra_licenses,
    entra_shadow_it,
    google_workspace,
    graph_source,
    halo,
    okta,
    powerplatform,
    rest_source,
)
from . import credentials as discovered
from .graph_source import RUN_DEADLINE, SourceError
from .models import (
    ACTIVE_RUN_STATUSES,
    NOT_CANDIDATES,
    Connector,
    ConnectorRun,
    ConnectorSchedule,
    DecisionKind,
    DiscoveredCredential,
    Observation,
    ObservationKind,
    ReconciliationDecision,
    RunStatus,
    RunTrigger,
    Source,
    SourceDifference,
)

log = logging.getLogger("softwarden.discovery")

SOURCES = {
    Source.ENTRA: entra,
    Source.ENTRA_LICENSES: entra_licenses,
    Source.AZURE: azure,
    Source.POWERPLATFORM: powerplatform,
    Source.HALO: halo,
    Source.ENTRA_SHADOW_IT: entra_shadow_it,
    Source.ENTRA_COVERAGE: entra_coverage,
    Source.OKTA: okta,
    Source.GOOGLE_WORKSPACE: google_workspace,
    Source.ENTRA_LICENCE_USAGE: entra_licence_usage,
}
SCHEDULED_AT = time(2, 0)  # daily runs start after 02:00 organization time
STUCK_AFTER = timedelta(hours=1)  # a run from before leases, still "running" this long after it started, died
NAME_MAX = 200
REASON_MAX = 500


def module(connector):
    return SOURCES[connector.source]


def connection_of(connector):
    """The connector's connection as stored now (never a copy cached on the connector), or None."""
    kind = connections.KINDS[module(connector).CONNECTION]
    pk = getattr(connector, f"{kind.key}_connection_id")
    return kind.model.objects.filter(organization_id=connector.organization_id, pk=pk).first() if pk else None


def fingerprint(source, values):
    raw = json.dumps(source.relevant(values), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


# ---- connectors -----------------------------------------------------------------------------------


def _connector_fields(org, data, connector, errors):
    """Validate and apply the fields present in data. Returns the audit values (never the secret)."""
    source = SOURCES[connector.source]
    if "name" in data:
        name = text_value(data.get("name"), "name", errors)
        if not name:
            errors.setdefault("name", ["Enter a name."])
        elif len(name) > NAME_MAX:
            errors["name"] = [f"Use {NAME_MAX} characters or fewer."]
        elif Connector.objects.filter(organization=org, name__iexact=name).exclude(id=connector.id).exists():
            errors["name"] = ["Another connector has this name."]
        connector.name = name
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            errors["enabled"] = ["Choose yes or no."]
        else:
            connector.enabled = data["enabled"]
    config = dict(connector.config)
    for key in source.CONFIG_FIELDS:
        if key in data:
            config[key] = str(data.get(key) or "").strip()
    scope = dict(source.SCOPE_DEFAULTS) | dict(connector.scope)
    if "scope" in data:
        if not isinstance(data["scope"], dict):
            errors["scope"] = ["Send the scope as an object."]
        else:
            unknown = set(data["scope"]) - set(source.SCOPE_DEFAULTS)
            if unknown:
                errors["scope"] = [f"This source has no {', '.join(sorted(unknown))} filter."]
            scope |= data["scope"]
            names = scope.get("name_exclusions")
            if isinstance(names, list) and all(isinstance(n, str) for n in names):
                scope["name_exclusions"] = [n.strip() for n in names if n.strip()]
    source.validate(config, scope, errors)
    connector.config, connector.scope = config, scope
    if "schedule" in data:
        if data["schedule"] not in ConnectorSchedule.values:
            errors["schedule"] = ["Choose Only when run or Daily."]
        else:
            connector.schedule = data["schedule"]
    if "reviewer" in data:
        reviewer = data.get("reviewer")
        if reviewer in (None, ""):
            connector.reviewer = None
        else:
            reviewer_id = whole_number(reviewer, 1)
            member = (
                Membership.objects.filter(organization=org, user_id=reviewer_id, user__is_active=True).first()
                if reviewer_id is not None
                else None
            )
            if member is None or member.role not in services.WRITE_ROLES:
                errors["reviewer"] = ["Choose an active Admin or Editor."]
            else:
                connector.reviewer_id = member.user_id
    kind = connections.KINDS[source.CONNECTION]
    attr = f"{kind.key}_connection"
    for other in connections.KINDS.values():  # another source's connection is refused, not ignored
        if other is not kind and f"{other.key}_connection" in data:
            errors[f"{other.key}_connection"] = [f"This source doesn't take {other.key}_connection; it uses {attr}."]
    if attr in data:
        if INLINE & set(data):
            errors[attr] = ["Choose a connection or enter credentials, not both."]
        elif data[attr] in (None, ""):
            errors[attr] = [f"Choose a {kind.label.lower()}."]
        else:
            chosen = connections.resolve(org, kind, data[attr], errors, attr)
            if chosen is not None:
                setattr(connector, attr, chosen)
    return {
        "name": connector.name,
        "enabled": connector.enabled,
        **{k: config.get(k, "") for k in source.CONFIG_FIELDS},
        "scope": scope,
        "schedule": connector.schedule,
        "reviewer": connector.reviewer_id,
        attr: str(getattr(connector, f"{attr}_id") or "") or None,
    }


# The connector form's credential fields from before connections. Still accepted, so the existing connector
# screen keeps saving until the guided walkthrough replaces it: they create or change the connector's Microsoft
# connection through core/connections.py (audited there), which then has to be tested before a run.
INLINE = {"tenant_id", "client_id", "secret"}


def _inline_connection(org, actor, connector, data, errors):
    """Apply the legacy inline credentials, if any, to the connector's own Microsoft connection (created on first use,
    named after the connector). Field errors keep their names (tenant_id, client_id, secret)."""
    inline = {k: data[k] for k in INLINE if k in data}
    if not inline:
        return
    if module(connector).CONNECTION != connections.MICROSOFT.key:
        errors.update({k: ["This source doesn't sign in with Microsoft credentials."] for k in inline})
        return
    try:
        if connector.microsoft_connection_id is None:
            name = _free_connection_name(org, connector.name or Source(connector.source).label)
            connector.microsoft_connection = connections.create_connection(
                org, actor, connections.MICROSOFT, {"name": name, **inline}
            )
        else:
            # A shared connection is edited as itself, never through one of its connectors: the others would change
            # tenant or secret with nothing on their own timeline. The connection is locked first, so a connector
            # pointed at it meanwhile (connections.resolve locks it too) waits and is counted.
            from core.models import MicrosoftConnection

            MicrosoftConnection.objects.select_for_update().filter(
                organization=org, pk=connector.microsoft_connection_id
            ).first()
            others = list(
                Connector.objects.filter(microsoft_connection_id=connector.microsoft_connection_id)
                .exclude(pk=connector.pk)
                .order_by("name")
                .values_list("name", flat=True)
            )
            if others:
                shared = (
                    f"The connection “{connector.microsoft_connection.name}” is also used by {', '.join(others)}. "
                    "Change it in its own settings (Connections), or choose another connection for this connector."
                )
                errors.update({k: [shared] for k in inline})
                return
            connections.update_connection(org, actor, connector.microsoft_connection, inline)
    except FieldErrors as e:
        errors.update(e.errors)


def _one_per_tenant(org, connector, errors):
    """A source that declares ONE_PER_TENANT (licence usage: two of them would count every licensed user
    twice) allows one connector per Microsoft connection and per tenant ID in the organization, whatever its state.
    The organization row is locked first, so two connectors added at once are judged one after the other."""
    if not getattr(module(connector), "ONE_PER_TENANT", False) or connector.microsoft_connection_id is None:
        return
    from core.models import MicrosoftConnection, Organization

    list(Organization.objects.select_for_update().filter(pk=org.pk).values_list("pk", flat=True))
    tenant = (
        (
            MicrosoftConnection.objects.filter(organization=org, pk=connector.microsoft_connection_id)
            .values_list("tenant_id", flat=True)
            .first()
            or ""
        )
        .strip()
        .lower()
    )
    others = (
        Connector.objects.filter(organization=org, source=connector.source)
        .exclude(pk=connector.pk)
        .select_related("microsoft_connection")
        .order_by("name", "id")
    )
    for other in others:
        same_connection = other.microsoft_connection_id == connector.microsoft_connection_id
        theirs = (other.microsoft_connection.tenant_id if other.microsoft_connection else "").strip().lower()
        if same_connection or (tenant and theirs == tenant):
            errors["microsoft_connection"] = [
                f"The connector “{other.name}” already reads this tenant's licensed users; a second one would count "
                "every license twice. Use that connector, or delete it first."
            ]
            return


def _simulated_connection(connector, errors):
    """A connector on a stored simulated connection is refused while core/simulation.allowed() is false: it
    could never test or run, and saving it would make the simulated connection look in use. Connectors already on one
    are kept (runs skip them), as the connections themselves are."""
    attr = f"{module(connector).CONNECTION}_connection"
    if not simulation.allowed() and connections.is_simulated(connection_of(connector)):
        errors[attr] = [simulation.MESSAGE]


def _free_connection_name(org, wanted):
    from core.models import MicrosoftConnection

    base = wanted.strip()[: connections.NAME_MAX] or "Microsoft connection"
    name, n = base, 2
    while MicrosoftConnection.objects.filter(organization=org, name__iexact=name).exists():
        suffix = f" ({n})"
        name, n = base[: connections.NAME_MAX - len(suffix)] + suffix, n + 1
    return name


@transaction.atomic
def create_connector(org, actor, data):
    source = data.get("source") or Source.ENTRA
    # Each source is a capability (entra, azure, powerplatform and halo in every edition; okta and google too, as previews).
    # Checked before support, so an organization asking for a source it lacks is told which capability it
    # lacks even before that source is built.
    if isinstance(source, str) and source in SOURCE_CAPABILITY:
        require_capability(org, SOURCE_CAPABILITY[source])
        # A preview source (core/capabilities.PREVIEW_SOURCES) needs `preview` too, for new connectors only.
        if source in PREVIEW_SOURCES:
            require_capability(org, PREVIEW)
    if source not in SOURCES:
        raise FieldErrors({"source": ["Choose a supported source."]})
    connector = Connector(organization=org, source=source, name="")
    errors: dict = {}
    data = {"name": Source(source).label, **data}
    values = _connector_fields(org, data, connector, errors)
    if not errors:
        limits.check_room(org, limits.CONNECTORS)  # before the inline connection, so a refusal writes nothing
        _inline_connection(org, actor, connector, data, errors)  # audited as the connection's own creation
    attr = f"{module(connector).CONNECTION}_connection"
    if getattr(connector, f"{attr}_id") is None and not errors:
        errors[attr] = ["Choose a connection for this connector."]
    if not errors:
        _simulated_connection(connector, errors)
    if not errors:
        _one_per_tenant(org, connector, errors)
    if errors:
        raise FieldErrors(errors)
    values[attr] = str(getattr(connector, f"{attr}_id"))
    try:
        with transaction.atomic():
            connector.save()
    except IntegrityError:
        raise FieldErrors({"name": ["Another connector has this name."]}) from None
    services.audit(org, actor, "connector.created", connector, {"source": source, **values})
    return connector


@transaction.atomic
def update_connector(org, actor, connector, data):
    # The source's capability, as on create (403 problem naming it); except turning the connector off and nothing else,
    # so an Admin can stop a source the organization's edition no longer includes.
    if data != {"enabled": False}:
        require_capability(org, SOURCE_CAPABILITY[connector.source])
    if getattr(module(connector), "ONE_PER_TENANT", False):
        # The organization before the connector, the order the organization purge takes them in (_one_per_tenant
        # locks the organization again below, which then holds already).
        from core.models import Organization

        list(Organization.objects.select_for_update().filter(pk=org.pk).values_list("pk", flat=True))
    connector = Connector.objects.select_for_update().get(id=connector.id)
    errors: dict = {}
    before = _connector_fields(org, {}, connector, {})
    after = _connector_fields(org, data, connector, errors)
    if not errors:
        _inline_connection(org, actor, connector, data, errors)  # audited as the connection's own change
    connection_sent = f"{module(connector).CONNECTION}_connection" in data or bool(INLINE & set(data))
    if not errors and connection_sent:  # renaming or turning off is never refused; runs check the tenant (fetch)
        _simulated_connection(connector, errors)
    if not errors and connection_sent:
        _one_per_tenant(org, connector, errors)
    if errors:
        raise FieldErrors(errors)
    after = {**after, **_connector_fields(org, {}, connector, {})}
    changed = {k: {"before": before[k], "after": after[k]} for k in after if before[k] != after[k]}
    # The last access check described another connection or scope: it no longer says whether
    # this connector can read what it now reads, so it is cleared with the change, in the same audited write.
    attr = f"{module(connector).CONNECTION}_connection"
    stale_check = connector.access_checked_at is not None and bool({"scope", attr} & set(changed))
    if stale_check:
        connector.access_check, connector.access_checked_at, connector.access_checked_by = {}, None, None
    connector.save()
    if changed:
        services.audit(
            org,
            actor,
            "connector.updated",
            connector,
            {**changed, **({"access_check": {"before": "checked", "after": "cleared"}} if stale_check else {})},
        )
    if "enabled" in changed and not connector.enabled:
        fail_queued(connector, TURNED_OFF)  # in this transaction: a queued run never reads a connector that is off
    if "enabled" in changed:
        note_credentials(org)  # a connector turned off stops its credentials' reminders; back on, they return
    return connector


@transaction.atomic
def delete_connector(org, actor, connector):
    """Removes the connector, its runs and observations. Records created or linked from it stay as they are."""
    linked = ReconciliationDecision.objects.filter(observation__connector=connector, resource__isnull=False).count()
    services.audit(org, actor, "connector.deleted", connector, {"name": connector.name, "linked_records": linked})
    ReconciliationDecision.objects.filter(observation__connector=connector).delete()
    connector.delete()
    note_credentials(org)  # its credentials' reminders close once this commits


@transaction.atomic
def request_run(org, actor, connector, accept_empty_listing=False):
    """Run now (Admin): queue a manual run and its `discovery.run` job; the worker starts it within a minute. Returns
    the queued run. 409 when the connector is off, already has a queued or running run, or its connection isn't tested
    at its current revision; 403 naming the capability when the organization lacks the source's.

    `accept_empty_listing`: this one run skips the empty-listing guards, so a listing that really is empty
    (the last resource of a type deleted) marks its observations no longer seen. It travels in the job's payload,
    is recorded on the run (counts) and on the audit event; scheduled runs never set it."""
    # The source's capability, as on create and on every run (an edition change after the connector was added).
    require_capability(org, SOURCE_CAPABILITY[connector.source])
    if not connector.enabled:
        raise Conflict("This connector is turned off. Turn it on to run it.")
    try:
        connections.check_usable(connection_of(connector))
    except connections.NotUsable as e:
        raise Conflict(str(e)) from None
    run_ = queue_run(connector, RunTrigger.MANUAL, actor, accept_empty_listing=accept_empty_listing)
    if run_ is None:
        raise Conflict("A run is already queued or in progress. Wait for it to finish.")
    changes: dict[str, Any] = {"run": str(run_.id)}
    if accept_empty_listing:
        changes["accept_empty_listing"] = True
    services.audit(org, actor, "connector.run_requested", connector, changes)
    return run_


# ---- Runs as jobs ---------------------------------------------------------------------------------------------------
#
# Run now and the daily schedule queue a run and its own `discovery.run` job (payload: the connector's ID only, never a
# secret). The job starts the run under a lease (the run's `lease_token` until `lease_expires`, RUN_LEASE: the
# fetch's RUN_DEADLINE plus a margin), renewed with the job's lease at every request (graph_source.checkpoint, at most
# once a minute) and before the write. Only the holder of the live token applies observations and writes the outcome
# (compare-and-set on the token), so a run whose lease expired, failed by the stuck sweep while a newer run took over,
# discards its result if it ever finishes. Discovery jobs run in the worker's long lane (core/worker.LongLane): a slow
# tenant never holds up the schedules, the short jobs or another organization's runs.

RUN_JOB = "discovery.run"
RUN_LEASE = timedelta(seconds=RUN_DEADLINE) + timedelta(minutes=10)
RENEW_EVERY = timedelta(minutes=1)
TURNED_OFF = "This connector was turned off before the run started; nothing was read."
WORKER_STOPPED = "The worker stopped during this run; run it again. Nothing it read was applied."


class RunStopped(Exception):
    """The worker is stopping (core/worker.LongLane.stop): the run records itself failed and returns. Not a
    SourceError, so no source treats it as one scope's failure and goes on."""


@transaction.atomic
def fail_queued(connector, reason, now=None, skipped="connector_off") -> int:
    """Fail the connector's queued runs (not started, so nothing was read) and finish their queued jobs, in one
    transaction, so no job is left to start a run that already failed. A job is finished as
    succeeded with `{"skipped": skipped}`, as run_job reports a run it skips. Returns how many runs."""
    now = now or timezone.now()
    queued = ConnectorRun.objects.select_for_update().filter(connector=connector, status=RunStatus.QUEUED)
    job_ids = [j for j in queued.values_list("job_id", flat=True) if j is not None]
    failed = queued.update(
        status=RunStatus.FAILED, finished_at=now, error=reason[:500], lease_token=None, lease_expires=None
    )
    if job_ids:
        Job.objects.filter(id__in=job_ids, status=JobStatus.QUEUED).update(
            status=JobStatus.SUCCEEDED, finished_at=now, result={"skipped": skipped}, updated_at=now
        )
    return failed


def _lapsed(run_, now) -> str:
    """Why an active run no longer holds its connector, in Softwarden's words, or "" while it does."""
    if run_.status == RunStatus.RUNNING:
        if run_.lease_expires is not None:
            if run_.lease_expires >= now:
                return ""
            return (
                "The run stopped without an outcome: its worker stopped or stalled, and its lease ran out at "
                f"{run_.lease_expires:%Y-%m-%d %H:%M} UTC. Nothing it read was applied; the next run starts as usual."
            )
        if run_.started_at is not None and run_.started_at >= now - STUCK_AFTER:
            return ""  # a run from before leases: an hour, as before
        return "The worker stopped during this run. Nothing it read was applied; the next run starts as usual."
    if (
        run_.job_id is not None
        and Job.objects.filter(id=run_.job_id, status__in=[JobStatus.QUEUED, JobStatus.RUNNING]).exists()
    ):
        return ""
    return "The run was queued, but its job ended without starting it (the worker stopped). Run it again."


def _fail_lapsed(run_, reason, now) -> bool:
    """Fail an active run that lost its connector, compare-and-set on its state, so a run that started or finished
    meanwhile is left alone. Returns whether it was failed."""
    updated = ConnectorRun.objects.filter(
        id=run_.id, status=run_.status, lease_token=run_.lease_token, job_id=run_.job_id
    ).update(status=RunStatus.FAILED, finished_at=now, error=reason[:500], lease_token=None, lease_expires=None)
    if updated:
        log.warning("connector run failed as stuck connector=%s run=%s", run_.connector_id, run_.id)
    return bool(updated)


def fail_stuck(now=None) -> int:
    """Fail every queued or running run that no longer holds its connector (_lapsed). Each minute, from the schedule;
    a new run's claim does it for its own connector too. Returns how many were failed."""
    now = now or timezone.now()
    failed = 0
    for run_ in ConnectorRun.objects.filter(status__in=ACTIVE_RUN_STATUSES):
        reason = _lapsed(run_, now)
        if reason and _fail_lapsed(run_, reason, now):
            failed += 1
    return failed


def _claim(connector, trigger, requested_by, now, *, queued, accept_empty_listing=False):
    """The connector's new active run, queued (with its job) or running (with a lease), or None while another run
    holds the connector. A previous run that lapsed is failed first, with its reason. The connector row is locked
    meanwhile, so two claims (Run now twice, Run now and the schedule) take turns; the one-active constraint is the
    backstop."""
    try:
        with transaction.atomic():
            Connector.objects.select_for_update().filter(id=connector.id).first()
            for active in ConnectorRun.objects.filter(connector=connector, status__in=ACTIVE_RUN_STATUSES):
                reason = _lapsed(active, now)
                if not reason or not _fail_lapsed(active, reason, now):
                    return None
            fields: dict[str, Any] = {
                "organization_id": connector.organization_id,
                "connector": connector,
                "trigger": trigger,
                "requested_by": requested_by,
            }
            if queued:
                run_ = ConnectorRun.objects.create(**fields, status=RunStatus.QUEUED, queued_at=now, started_at=None)
                payload: dict[str, Any] = {"connector_id": str(connector.id)}
                if accept_empty_listing:
                    payload["accept_empty_listing"] = True
                job = jobs.enqueue(RUN_JOB, payload, organization=connector.organization)
                run_.job_id = job.id
                run_.save(update_fields=["job_id"])
            else:
                run_ = ConnectorRun.objects.create(
                    **fields,
                    status=RunStatus.RUNNING,
                    started_at=now,
                    lease_token=uuid.uuid4(),
                    lease_expires=now + RUN_LEASE,
                )
    except IntegrityError:  # another claim won the race the row lock didn't cover
        return None
    return run_


def queue_run(connector, trigger, requested_by=None, now=None, accept_empty_listing=False):
    """Queue a run and its `discovery.run` job. None while another run is queued or running."""
    return _claim(
        connector, trigger, requested_by, now or timezone.now(), queued=True, accept_empty_listing=accept_empty_listing
    )


class _Lease:
    """The running run's lease, and its job's when a job runs it: renewed at most every RENEW_EVERY (or now, when
    forced). Raises core.jobs.LeaseLost once the run (or the job) was taken over."""

    def __init__(self, run_, ctx=None):
        self.run, self.ctx = run_, ctx
        self.renewed = timezone.now()

    def renew(self, force=False):
        if self.ctx is not None and self.ctx.stopping():
            raise RunStopped
        now = timezone.now()
        if not force and now - self.renewed < RENEW_EVERY:
            return
        if self.ctx is not None:
            self.ctx.renew()
        updated = ConnectorRun.objects.filter(
            id=self.run.id, status=RunStatus.RUNNING, lease_token=self.run.lease_token
        ).update(lease_expires=now + RUN_LEASE)
        if not updated:
            raise jobs.LeaseLost
        self.run.lease_expires, self.renewed = now + RUN_LEASE, now

    def hold(self):
        """Inside the write's transaction: lock the run row while this run still holds it (LeaseLost otherwise), so no
        sweep or newer run can take over until the write commits."""
        owned = (
            ConnectorRun.objects.select_for_update()
            .filter(id=self.run.id, status=RunStatus.RUNNING, lease_token=self.run.lease_token)
            .first()
        )
        if owned is None:
            raise jobs.LeaseLost


def run(connector, trigger=RunTrigger.MANUAL, requested_by=None, now=None, transport=None, accept_empty_listing=False):
    """One pass over the source, now, in this process (the demo seed, timings, tests). Returns the run, or None if
    another run of this connector is queued or running. A failure is recorded on the run and changes no observation.
    `accept_empty_listing`: as request_run's."""
    now = now or timezone.now()
    run_ = _claim(connector, trigger, requested_by, now, queued=False)
    if run_ is None:
        return None
    return _execute(connector, run_, now, transport, _Lease(run_), accept_empty_listing)


def run_job(job, ctx):
    """The `discovery.run` job: start the connector's run queued with this job, under a lease. The run holds the
    counts and the error; the job's result is the run's ID and status only."""
    connector_id = (job.payload or {}).get("connector_id")
    connector = None
    if services.is_uuid(connector_id):
        connector = (
            Connector.objects.select_related("organization")
            .filter(organization_id=job.organization_id, id=str(connector_id))
            .first()
        )
    if connector is None:
        return {"skipped": "connector_deleted"}
    now = timezone.now()
    queued = ConnectorRun.objects.filter(connector=connector, job_id=job.id, status=RunStatus.QUEUED)
    if not connector.enabled:  # turned off while the run waited: it never reads the tenant
        queued.update(status=RunStatus.FAILED, finished_at=now, error=TURNED_OFF)
        return {"skipped": "connector_off"}
    started = ConnectorRun.objects.filter(connector=connector, job_id=job.id, status=RunStatus.QUEUED).update(
        status=RunStatus.RUNNING, started_at=now, lease_token=uuid.uuid4(), lease_expires=now + RUN_LEASE
    )
    if not started:  # failed meanwhile (its job looked gone to the sweep)
        return {"skipped": "run_not_queued"}
    run_ = ConnectorRun.objects.get(connector=connector, job_id=job.id)
    accept = (job.payload or {}).get("accept_empty_listing") is True
    run_ = _execute(connector, run_, now, None, _Lease(run_, ctx), accept)
    return {"run": str(run_.id), "status": run_.status}


def _redact(text, credential=None, tokens=()):
    """`text` with the connection's own secret, the access tokens issued during the run and the installation's
    configured secrets replaced (per-organization secrets aren't in core/jobs' list)."""
    values = [*connections.redact_values(credential), *tokens]
    for value in sorted({v for v in values if len(v) >= 4}, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return jobs.redact_configured(text)


def _execute(connector, run_, now, transport, lease, accept_empty_listing=False):
    """Fetch outside any lock, then apply and record under the run's lease (lease.hold) in one transaction. With
    `accept_empty_listing` the empty-listing guards stand aside for this run (rest_source.accepting_empty_listing)."""
    source = module(connector)
    credential = None
    issued: list[str] = []
    offset = now - timezone.now()  # finished_at on the caller's clock (a run started with an injected `now`)

    def finish(status, counts=None, error="", failure_code=""):
        return _finish(
            run_, status, counts=counts, error=error, finished_at=timezone.now() + offset, failure_code=failure_code
        )

    try:
        with rest_source.accepting_empty_listing(accept_empty_listing), graph_source.checkpoint(lease.renew, issued):
            # Enforced here, whatever started the run: the organization still has the source's capability (an edition
            # change after the connector was added) and the connection passed its test at its current revision.
            capability = SOURCE_CAPABILITY.get(connector.source)
            if capability is None or not has_capability(connector.organization, capability):
                if capability is not None and hidden_preview(capability):
                    raise SourceError(
                        f"The {capability} capability this source needs is a preview feature this installation "
                        "hasn't turned on, so the run didn't start."
                    )
                raise SourceError(
                    f"This organization's edition doesn't include the {capability or connector.source} capability "
                    "this source needs, so the run didn't start."
                )
            try:
                credential = connections.credential(connection_of(connector))
            except connections.NotUsable as e:
                raise SourceError(str(e)) from None
            raw = source.fetch(connector, credential, transport)
        lease.renew(force=True)
        with transaction.atomic(), rest_source.accepting_empty_listing(accept_empty_listing):
            lease.hold()
            counts = _apply(connector, source, raw, now)
            if accept_empty_listing:
                counts["accept_empty_listing"] = True  # on the run: an Admin accepted an empty listing for it
            finish(RunStatus.SUCCEEDED, counts=counts)
    except RunStopped:
        finish(RunStatus.FAILED, error=WORKER_STOPPED)
        log.warning("connector run stopped with its worker connector=%s run=%s", connector.id, run_.id)
        return run_
    except jobs.LeaseLost:
        log.warning("connector run lost its lease; outcome discarded connector=%s run=%s", connector.id, run_.id)
        run_.refresh_from_db()
        return run_
    except SourceError as e:
        error = _redact(str(e), credential, issued)[:500]
        # A coded refusal (rest_source.EmptyListing) says so on the run, for clients.
        finish(RunStatus.FAILED, error=error, failure_code=getattr(e, "CODE", ""))
        log.warning("connector run failed connector=%s run=%s error=%s", connector.id, run_.id, error)
        return run_
    except Exception as e:
        # An unexpected exception's message is never stored or logged: it could carry anything (a token, a secret,
        # a response body). Only its type, and the stack as file, line and code, which are the program's own text.
        kind = type(e).__name__
        finish(RunStatus.FAILED, error=f"Unexpected error ({kind}). See the worker log.")
        stack = "".join(traceback.format_list(traceback.extract_tb(e.__traceback__)))
        log.error(
            "connector run failed on an unexpected error connector=%s run=%s type=%s stack:\n%s",
            connector.id,
            run_.id,
            kind,
            _redact(stack, credential, issued),
        )
        return run_
    numbers = " ".join(f"{k}={v}" for k, v in counts.items() if isinstance(v, int) and not isinstance(v, bool))
    log.info("connector run connector=%s run=%s %s", connector.id, run_.id, numbers)
    if run_.status == RunStatus.SUCCEEDED:
        _after_success(connector, run_)
    return run_


def _after_success(connector, run_) -> None:
    """After a successful run commits: the assistant (capability `assistant`) queues suggestions for its new
    and changed candidates when the organization has it on (discovery/assistant.after_run). A failure here never
    fails the run: only its type is logged."""
    from . import assistant

    try:
        assistant.after_run(connector, run_)
    except Exception as e:
        log.error(
            "assistant batch not queued after run connector=%s run=%s type=%s", connector.id, run_.id, type(e).__name__
        )


def _finish(run_, status, counts=None, error="", finished_at=None, failure_code=""):
    """Record the outcome while the run holds its lease (compare-and-set on the token); a run that lost it records
    nothing. Returns whether it was recorded."""
    fields: dict[str, Any] = {
        "status": status,
        "finished_at": finished_at or timezone.now(),
        "error": error,
        "failure_code": failure_code,
        "lease_token": None,
        "lease_expires": None,
    }
    if counts is not None:
        fields["counts"] = counts
    updated = ConnectorRun.objects.filter(id=run_.id, status=RunStatus.RUNNING, lease_token=run_.lease_token).update(
        **fields
    )
    if not updated:
        log.warning("connector run finished after losing its lease; outcome discarded run=%s", run_.id)
        run_.refresh_from_db()
        return False
    for key, value in fields.items():
        setattr(run_, key, value)
    return True


# ---- applying a run -------------------------------------------------------------------------------


@transaction.atomic
def _apply(connector, source, raw, now):
    """Upsert every in-scope object by its stable ID; count the rest per filter; mark the unseen ones gone.

    An object the source returned but a filter skipped, or one the scope no longer reads at all (the source's
    `scope_reason`: cloud flows turned off, a subscription or environment left out), is *out of scope*: its observation
    keeps its state and is never "no longer seen" (reconciliation). A source's `GONE_REASONS` are skips that
    mean the object is really gone (Power Platform's "deleted in Dataverse")."""
    skipped = dict.fromkeys(source.FILTERS, 0)
    kept = {}
    found_credentials = {}
    skipped_ids: dict[str, str] = {}  # external ID -> filter, for objects the source still has
    gone_reasons = set(getattr(source, "GONE_REASONS", ()))
    for obj in raw:
        values = source.map_object(obj)
        reason = source.skip_reason(values, connector.scope)
        if reason:
            skipped[reason] += 1
            if reason not in gone_reasons and values.get("external_id"):
                skipped_ids.setdefault(str(values["external_id"]), reason)
        else:
            key = (values["kind"], values["external_id"])
            kept[key] = values
            if "credentials" in values:
                found_credentials[key] = values["credentials"]
    existing = {
        (o.kind, o.external_id): o for o in Observation.objects.filter(connector=connector, kind__in=source.KINDS)
    }
    # An object whose kind changed (an app tagged as a Copilot Studio agent becomes an ai_agent observation) keeps its
    # row, so its decision stays: matched by external ID among the source's kinds.
    by_external: dict[str, list[Observation]] = {}
    for o in existing.values():
        by_external.setdefault(o.external_id, []).append(o)
    _guard_empty_listings(connector, source, raw, existing, skipped_ids)
    claimed: set[Any] = set()
    # Rows discovery 0010 re-keyed carry data["rekeyed"]: their fingerprint is the earlier one (service principal owners only),
    # so a decision that matched it moves to this run's fingerprint instead of bringing the dismissal back.
    carried: list[tuple[Any, str, str]] = []
    returned: list[Any] = []  # back after being gone: "no longer seen" clears itself
    new = changed = unchanged = gone = 0
    # Data keys that change with use, not with the object (shadow IT's usage counts): updated, never a change.
    volatile = tuple(getattr(source, "VOLATILE", ()))
    current = {}
    to_create, to_update = [], []
    for key, values in kept.items():
        fp = fingerprint(source, values)
        obs = existing.get(key)
        if obs is None:
            obs = next(
                (
                    o
                    for o in by_external.get(values["external_id"], [])
                    if (o.kind, o.external_id) not in kept and o.id not in claimed
                ),
                None,
            )
        if obs is None:
            new += 1
            current[key] = Observation(
                organization_id=connector.organization_id,
                connector=connector,
                kind=values["kind"],
                external_id=values["external_id"],
                display_name=values["display_name"],
                data=values["data"],
                fingerprint=fp,
                ai_agent=values["ai_agent"],
                suggested_component_type=values.get("suggested_component_type", ""),
                first_seen_at=now,
                last_seen_at=now,
                last_changed_at=now,
            )
            to_create.append(current[key])
            continue
        claimed.add(obs.id)
        if obs.gone_at is not None:
            returned.append(obs.id)
        back_in_scope = bool(obs.out_of_scope)
        obs.out_of_scope, obs.out_of_scope_at = "", None
        rekeyed = isinstance(obs.data, dict) and bool(obs.data.get("rekeyed"))
        if rekeyed:
            carried.append((obs.id, obs.fingerprint, fp))
        differs = (
            _stable(obs.data, volatile) != _stable(values["data"], volatile)
            or obs.display_name != values["display_name"]
            or obs.gone_at is not None
            or back_in_scope
            or obs.kind != values["kind"]
        )
        obs.kind = values["kind"]
        if differs:
            changed += 1
            if not rekeyed:  # the upgrade's new fields aren't a change in the source
                obs.last_changed_at = now
        else:
            unchanged += 1
        obs.display_name, obs.data, obs.fingerprint, obs.ai_agent = (
            values["display_name"],
            values["data"],
            fp,
            values["ai_agent"],
        )
        obs.suggested_component_type = values.get("suggested_component_type", "")
        obs.last_seen_at, obs.gone_at = now, None
        to_update.append(obs)
        current[key] = obs
    # A scope the source couldn't read this run (a subscription, an environment) keeps its observations as they were:
    # "not read", never "no longer seen" (Fetched.unread).
    unread = set(getattr(raw, "unread", ()))
    unread_scope = getattr(source, "unread_scope", None)
    not_read = out_of_scope = 0
    scope_reason = getattr(source, "scope_reason", None)
    scope = dict(getattr(source, "SCOPE_DEFAULTS", {})) | dict(connector.scope or {})
    for obs in existing.values():
        if obs.id in claimed:
            continue
        reason = skipped_ids.get(obs.external_id)
        if reason is not None:
            # Still in the source, skipped by a filter: seen, so not gone (and no longer gone if it was).
            if obs.gone_at is not None:
                returned.append(obs.id)
            out_of_scope += 1
            obs.out_of_scope_at = obs.out_of_scope_at if obs.out_of_scope == reason else now
            obs.out_of_scope, obs.last_seen_at, obs.gone_at = reason, now, None
            to_update.append(obs)
            continue
        if obs.gone_at is not None:
            continue
        if unread and _in_unread(obs, unread, unread_scope):
            not_read += 1
            continue
        reason = scope_reason(obs, scope) if scope_reason else None
        if reason:
            # The scope no longer reads it at all: nothing was learnt about it, so it keeps its state.
            out_of_scope += 1
            if obs.out_of_scope != reason:
                obs.out_of_scope, obs.out_of_scope_at = reason, now
                to_update.append(obs)
            continue
        gone += 1
        obs.gone_at = now
        obs.out_of_scope, obs.out_of_scope_at = "", None
        to_update.append(obs)
    # The empty-listing guard: a source whose listing defines what is gone (`EMPTY_LISTING_NAME` names it) and that
    # answered nothing at all, while observations would go "no longer seen", is far more likely incomplete (revoked
    # consent, a filtered or faulty page) than everything deleted. The run fails before anything is written: no
    # observation is marked gone, no discovered credential is removed. Unread and out-of-scope observations don't count.
    empty_listing = getattr(source, "EMPTY_LISTING_NAME", None)
    if empty_listing and gone and not raw and not rest_source.empty_listing_accepted():
        raise rest_source.empty_listing_error(empty_listing, gone)
    Observation.objects.bulk_create(to_create, batch_size=500)
    # Each new candidate is a `discovery.candidate` webhook event, posted once the run's transaction commits.
    from notify import webhooks

    webhooks.candidates_found(connector, [o for o in to_create if o.kind not in NOT_CANDIDATES])
    Observation.objects.bulk_update(
        to_update,
        [
            "kind",
            "display_name",
            "data",
            "fingerprint",
            "ai_agent",
            "suggested_component_type",
            "last_seen_at",
            "last_changed_at",
            "gone_at",
            "out_of_scope",
            "out_of_scope_at",
        ],
        batch_size=500,
    )
    for obs_id, before, after in carried:
        ReconciliationDecision.objects.filter(observation_id=obs_id, fingerprint=before).update(fingerprint=after)
    if returned:
        ReconciliationDecision.objects.filter(observation_id__in=returned).update(
            gone_acknowledged_at=None, gone_acknowledged_by=None, gone_resolution="", gone_note=""
        )
    counts = {
        "seen": len(raw),
        "kept": len(kept),
        "skipped": {k: v for k, v in skipped.items() if v},
        "new": new,
        "changed": changed,
        "unchanged": unchanged,
        "gone": gone,
    }
    if unread:
        counts["not_read"] = not_read
    if out_of_scope:
        counts["out_of_scope"] = out_of_scope
    if returned:
        counts["returned"] = len(returned)
    # Linked observations: "source differs" per field, never applied (discovery/drift.py).
    if set(source.KINDS) - set(NOT_CANDIDATES):
        counts["differs"] = drift.recompute_connector(connector, now)
    # Licence observations: pools that sync their SKU take the counts; the rest are proposals (money/licences.py).
    after_apply = getattr(source, "after_apply", None)
    if after_apply is not None:
        counts.update(after_apply(connector, list(current.values()), now))
    # Shadow IT: what the fetch read besides objects (daily sign-in aggregates, the cursor), stored in this
    # transaction so a failed run stores none of it.
    apply_fetched = getattr(source, "apply_fetched", None)
    if apply_fetched is not None:
        counts.update(apply_fetched(connector, raw, list(current.values()), now))
    if hasattr(source, "credentials_of") or found_credentials:
        counts.update(discovered.upsert(connector, current, found_credentials, now))
        note_credentials(connector.organization)  # the credential reminders follow once the run commits
    sync_notes: Any = counts.pop("sync_notes", [])
    notes = list(getattr(raw, "notes", [])) + list(sync_notes)
    if carried:
        notes.insert(
            0,
            f"First run since the upgrade: {len(carried)} observation(s) gained new fields and count as changed this "
            "once; their decisions are kept and dismissed candidates stay dismissed.",
        )
    if notes:
        counts["notes"] = notes[:10]
    return counts


def _scope_listings(observation) -> set[str]:
    """The listing an observation came from, by default its `data["scope"]` (Azure, Power Platform)."""
    scope = observation.data.get("scope") if isinstance(observation.data, dict) else None
    return {scope} if isinstance(scope, str) and scope else set()


def _guard_empty_listings(connector, source, raw, existing, skipped_ids) -> None:
    """The empty-listing guard per listing: a listing of several that answered with nothing at all
    (`Fetched.empty`, {scope: name}) while live observations, or live discovered credentials, came from it fails the
    run before anything is written. An observation is "from" a listing when one of the source's `listings_of` (by
    default its data["scope"]) is the listing's scope or inside it. Observations already gone, listed this run but
    skipped by a filter, in a scope the run couldn't read, or out of the connector's scope don't count."""
    empty = dict(getattr(raw, "empty", None) or {})
    if not empty or rest_source.empty_listing_accepted():
        return  # nothing came back empty, or an Admin accepted an empty listing for this run
    if not raw and getattr(source, "EMPTY_LISTING_NAME", None):
        return  # nothing listed at all: the whole-listing guard decides (after the gone count, in _apply)
    listings_of = getattr(source, "listings_of", _scope_listings)
    unread = set(getattr(raw, "unread", ()))
    unread_scope = getattr(source, "unread_scope", None)
    scope_reason = getattr(source, "scope_reason", None)
    scope = dict(getattr(source, "SCOPE_DEFAULTS", {})) | dict(connector.scope or {})
    lost: dict[str, list[int]] = {key: [0, 0] for key in empty}  # [observations, credentials]
    counted: set[Any] = set()  # the observations that count, whose credentials count too
    for obs in existing.values():
        if obs.gone_at is not None or obs.external_id in skipped_ids:
            continue
        if unread and _in_unread(obs, unread, unread_scope):
            continue
        if scope_reason is not None and scope_reason(obs, scope):
            continue
        counted.add(obs.id)
        sources = listings_of(obs)
        for key in empty:
            if any(s == key or s.startswith(f"{key}/") for s in sources):
                lost[key][0] += 1
    credential_listing = getattr(source, "credential_listing", None)
    if credential_listing is not None:
        live = DiscoveredCredential.objects.filter(observation_id__in=counted, removed_at__isnull=True)
        for credential in live.only("origin"):
            key = credential_listing(credential)
            if key in lost:
                lost[key][1] += 1
    for key, (observations, credentials) in lost.items():
        if observations or credentials:
            things = " and ".join(
                part
                for part in (
                    f"{observations} live observation(s)" if observations else "",
                    f"{credentials} discovered credential(s)" if credentials else "",
                )
                if part
            )
            raise rest_source.empty_listing_error(empty[key], observations + credentials, things=f"{things} from it")


def _stable(data, volatile):
    """An observation's data without its source's VOLATILE keys, to tell whether it changed."""
    if not volatile or not isinstance(data, dict):
        return data
    return {k: v for k, v in data.items() if k not in volatile}


# Fetched.unread entry meaning the whole source wasn't read completely this run (Halo answering the same page twice):
# nothing of the connector becomes "no longer seen".
ALL_UNREAD = "*"


def _in_unread(obs, unread, unread_scope=None):
    """The observation is in a scope the run couldn't read: the scope the source names for it (`unread_scope(obs)`,
    Entra's agent identities, read through their own optional permission), else its `data["scope"]`."""
    if ALL_UNREAD in unread:
        return True
    scope = unread_scope(obs) if unread_scope is not None else None
    if scope is None:
        scope = obs.data.get("scope") if isinstance(obs.data, dict) else None
    return isinstance(scope, str) and any(scope == u or scope.startswith(f"{u}/") for u in unread)


def note_credentials(org):
    from notify import reminders

    reminders.note_credentials(org)


def due_runs(now=None):
    """The schedule's work each minute: fail the runs that lost their lease (fail_stuck), then queue (never run) the
    runs that are due: a run requested the older way (`run_requested_at`, left by an upgrade) and each daily
    connector's run after 02:00 organization time, once a day. Returns how many were queued.

    A connector whose source the organization's capabilities no longer include (an edition change, a platform revoke,
    or Okta and Google Workspace while the installation's preview features are off) is skipped: nothing is queued, so
    no run fails every day for it, and the connector, its runs and its observations stay as they are. So is one whose
    connection is simulated while core/simulation.allowed() is false (a demo install promoted to production)."""
    now = now or timezone.now()
    fail_stuck(now)
    queued = 0
    simulated_ok = simulation.allowed()
    connectors = Connector.objects.filter(enabled=True, organization__suspended_at__isnull=True)
    for connector in connectors.select_related("organization", "run_requested_by"):
        capability = SOURCE_CAPABILITY.get(connector.source)
        if capability is None or not has_capability(connector.organization, capability):
            continue
        if not simulated_ok and connections.is_simulated(connection_of(connector)):
            continue
        trigger, by = None, None
        if connector.run_requested_at is not None:
            trigger, by = RunTrigger.MANUAL, connector.run_requested_by
        elif connector.schedule == ConnectorSchedule.DAILY:
            local = timezone.localtime(now, ZoneInfo(connector.organization.timezone))
            if local.time() >= SCHEDULED_AT:
                start = local.replace(hour=SCHEDULED_AT.hour, minute=SCHEDULED_AT.minute, second=0, microsecond=0)
                # A run the worker stopped (WORKER_STOPPED: a restart or deploy) doesn't count, so the day's run is
                # queued again the same day; any other failure waits for the next day.
                already = (
                    ConnectorRun.objects.filter(connector=connector, trigger=RunTrigger.SCHEDULED)
                    .filter(Q(started_at__gte=start) | Q(queued_at__gte=start))
                    .exclude(status=RunStatus.FAILED, error=WORKER_STOPPED)
                )
                if not already.exists():
                    trigger = RunTrigger.SCHEDULED
        if trigger is None:
            continue
        if queue_run(connector, trigger, by, now) is not None:
            queued += 1
            if connector.run_requested_at is not None:
                Connector.objects.filter(id=connector.id).update(run_requested_at=None, run_requested_by=None)
    return queued


# ---- candidates and decisions ---------------------------------------------------------------------

STATUSES = ("new", "changed", "linked", "dismissed", "gone", "out_of_scope")
# Filters over linked observations (reconciliation), counted beside the statuses: `missing`, linked and no
# longer seen and nobody resolved it yet; `differs`, linked, still seen, with a source difference nobody ignored.
LINKED_FILTERS = ("missing", "differs")


# Every count the candidate queue shows beside its list (GET discovery/candidates `counts`), in that order.
COUNTED = (*STATUSES, "candidates", *LINKED_FILTERS)


def _count_aggregates() -> dict[str, Any]:
    # Aliased: an alias named like a field (out_of_scope) would shadow it inside the filters.
    return {f"n_{s}": Count("id", filter=status_q(s)) for s in COUNTED}


def status_counts(qs) -> dict[str, int]:
    """{status or linked filter: observations in qs}, one query: the candidate queue's `counts` (discovery/viewsets.py)
    and the composite discovery summary's totals (api/composite.py) both read this."""
    found = qs.aggregate(**_count_aggregates())
    return {s: found[f"n_{s}"] for s in COUNTED}


def status_counts_by(qs, field: str) -> dict[Any, dict[str, int]]:
    """status_counts per value of `field` (a connector id, say), one query; values with no observations are absent."""
    out: dict[Any, dict[str, int]] = {}
    for row in qs.values(field).annotate(**_count_aggregates()).order_by(field):
        found: dict[str, Any] = dict(row)
        out[found[field]] = {s: found[f"n_{s}"] for s in COUNTED}
    return out


def status_of(obs):
    d = getattr(obs, "decision", None)
    if d is not None and d.decision != DecisionKind.DISMISSED:
        return "linked"
    if obs.gone_at is not None:
        return "gone"
    if obs.out_of_scope:
        return "out_of_scope"
    if d is None:
        return "new"
    return "changed" if d.fingerprint != obs.fingerprint else "dismissed"


def missing_q():
    """Linked or created, no longer seen in source, nobody resolved it yet, from an enabled connector, and its record
    still live (not archived, not retired). The one rule the candidates filter, Needs attention and the digest
    share (discovery/missing.py)."""
    linked = Q(decision__isnull=False) & ~Q(decision__decision=DecisionKind.DISMISSED)
    return (
        linked
        & Q(gone_at__isnull=False)
        & Q(decision__gone_acknowledged_at__isnull=True)
        & Q(connector__enabled=True)
        & Q(decision__resource__archived_at__isnull=True)
        & ~Q(decision__resource__lifecycle=Lifecycle.RETIRED)
    )


def status_q(status):
    """The same rule as status_of, as a filter."""
    linked = Q(decision__isnull=False) & ~Q(decision__decision=DecisionKind.DISMISSED)
    dismissed = Q(decision__decision=DecisionKind.DISMISSED)
    seen = Q(gone_at__isnull=True)
    alive = seen & Q(out_of_scope="")  # seen and in scope
    if status == "linked":
        return linked
    if status == "gone":
        return ~linked & ~seen
    if status == "out_of_scope":
        return ~linked & seen & ~Q(out_of_scope="")
    if status == "new":
        return alive & Q(decision__isnull=True)
    if status == "missing":
        return missing_q()
    if status == "differs":
        open_ = SourceDifference.objects.filter(observation_id=OuterRef("pk"), ignored_at__isnull=True)
        return linked & alive & Exists(open_)
    same = Q(decision__fingerprint=F("fingerprint"))
    if status == "changed":
        return alive & dismissed & ~same
    if status == "dismissed":
        return alive & dismissed & same
    if status == "candidates":
        return alive & (Q(decision__isnull=True) | (dismissed & ~same))
    return Q()


def candidate_counts(org):
    """{connector id: (new, changed)} for connectors with candidates; one query (Needs attention)."""
    rows = (
        Observation.objects.filter(organization=org, connector__enabled=True)
        .exclude(kind__in=NOT_CANDIDATES)
        .filter(status_q("candidates"))
        .values("connector_id")
        .annotate(new=Count("id", filter=Q(decision__isnull=True)), total=Count("id"))
    )
    return {r["connector_id"]: (r["new"], r["total"] - r["new"]) for r in rows}


def _locked(org, observation):
    return Observation.objects.select_for_update().select_related("connector").get(organization=org, id=observation.id)


def _undecided(obs):
    d = ReconciliationDecision.objects.filter(observation=obs).first()
    if d is not None and not (d.decision == DecisionKind.DISMISSED and d.fingerprint != obs.fingerprint):
        raise Conflict("Someone already decided this candidate. Reload to see the decision.")
    if d is not None:
        d.delete()  # a dismissal the source has since changed: the new decision replaces it


def _decide(org, actor, obs, decision, resource=None, reason=""):
    return ReconciliationDecision.objects.create(
        organization=org,
        observation=obs,
        decision=decision,
        resource=resource,
        reason=reason,
        fingerprint=obs.fingerprint,
        decided_by=actor,
    )


def _record_suggestion(org, actor, obs, decision, resource=None, accepting=False):
    """The decision is recorded against the candidate's assistant suggestion, if it has one (discovery/assistant.py)."""
    from . import assistant

    assistant.record_decision(org, actor, obs, decision, resource, accepting=accepting)


@transaction.atomic
def link(org, actor, observation, data, *, accepting=False):
    obs = _locked(org, observation)
    rid = data.get("resource")
    resource = (
        Resource.objects.filter(organization=org, id=rid, kind__in=RECORD_KINDS).first()
        if services.is_uuid(rid)
        else None
    )
    if resource is None:
        raise FieldErrors({"resource": ["Choose an existing application, component or AI agent."]})
    if resource.archived_at is not None:
        raise FieldErrors({"resource": [f"{resource.name} is archived. Restore it first, or choose another record."]})
    _undecided(obs)
    _decide(org, actor, obs, DecisionKind.LINKED, resource)
    services.audit(
        org, actor, "observation.linked", resource, {"observation": str(obs.id), "source_name": obs.display_name}
    )
    _record_suggestion(org, actor, obs, DecisionKind.LINKED, resource, accepting)
    drift.recompute(org, [obs.id])  # what the source says differently from the record, from now
    note_credentials(org)  # the app's credential reminders now route to the record
    return obs


@transaction.atomic
def create_record(org, actor, observation, data, *, accepting=False):
    """A new record from the candidate, prefilled from the source and editable in the request. It starts Active with
    no owners, so it lands in the ownership gaps queue; suggested owners are only ever applied by an owner edit."""
    obs = _locked(org, observation)
    _undecided(obs)
    homepage = obs.data.get("homepage", "")
    default = {ObservationKind.AI_AGENT: Kind.AI_AGENT, ObservationKind.COMPONENT: Kind.COMPONENT}.get(
        obs.kind, Kind.APPLICATION
    )
    fields = {
        "kind": data.get("kind") or default,
        "name": data.get("name") or obs.display_name[:NAME_MAX],
        "lifecycle": data.get("lifecycle") or Lifecycle.ACTIVE,
        "description": data.get("description", obs.data.get("description", "")),
    }
    if homepage.startswith("https://") and len(homepage) <= 200:
        fields["documentation_url"] = homepage
    # Prefilled from the source (an Azure resource ID, a Power Platform environment): only fields the chosen kind has
    # are kept by the service; empty values are left out. The record's own fields are the person's to add.
    source = SOURCES.get(obs.connector.source)
    record_fields = getattr(source, "record_fields", None)
    for key, value in (record_fields(obs) if record_fields else {}).items():
        if value not in ("", None):
            fields.setdefault(key, value)
    if fields["kind"] == Kind.COMPONENT and obs.suggested_component_type:
        fields.setdefault("component_type", obs.suggested_component_type)
    if fields["kind"] == Kind.AI_AGENT:
        if obs.data.get("agent") == "copilot_studio":
            fields.setdefault("platform", "copilot_studio")
        external = obs.data.get("app_id") or obs.external_id
        if external:
            fields.setdefault("external_id", external[:200])
    if "custom_field_data" in data:  # values for the kind's custom fields (required ones need one)
        fields["custom_field_data"] = data["custom_field_data"]
    resource = services.create_resource(org, actor, fields)
    _decide(org, actor, obs, DecisionKind.CREATED, resource)
    services.audit(
        org, actor, "observation.created", resource, {"observation": str(obs.id), "source_name": obs.display_name}
    )
    _record_suggestion(org, actor, obs, DecisionKind.CREATED, resource, accepting)
    drift.recompute(org, [obs.id])  # a value the person changed on create differs from the source
    note_credentials(org)
    return obs, resource


@transaction.atomic
def dismiss(org, actor, observation, data, *, accepting=False):
    obs = _locked(org, observation)
    problems: dict[str, list[str]] = {}
    reason = text_value(data.get("reason"), "reason", problems)
    if problems:
        raise FieldErrors(problems)
    if not reason:
        raise FieldErrors({"reason": ["Say why, so the next person knows (for example: test registration)."]})
    if len(reason) > REASON_MAX:
        raise FieldErrors({"reason": [f"Use {REASON_MAX} characters or fewer."]})
    _undecided(obs)
    _decide(org, actor, obs, DecisionKind.DISMISSED, reason=reason)
    services.audit(org, actor, "observation.dismissed", obs, {"source_name": obs.display_name, "reason": reason})
    _record_suggestion(org, actor, obs, DecisionKind.DISMISSED, None, accepting)
    note_credentials(org)  # a dismissed app's credentials stop reminding
    return obs


# Audited actions on a created record that are not edits of it (reopen's archive check).
NOT_EDITS = {
    "resource.created",
    "observation.created",
    "observation.linked",
    "observation.reopened",
    "observation.drift_ignored",
    "observation.missing_resolved",
}


def edited_since(org, resource, since) -> bool:
    """Someone changed the record (or anything recorded against it) after `since`."""
    from core.models import AuditEvent

    return (
        AuditEvent.objects.filter(organization=org, created_at__gte=since)
        .filter(Q(target_id=resource.id) | Q(related_target_id=resource.id))
        .exclude(action__in=NOT_EDITS)
        .exists()
    )


def _archive_created(org, actor, obs, d) -> None:
    """Archive the record created from this candidate, only while it is still the candidate's own: not changed since,
    referenced by nothing (contract lines, licence pools, relationships either way, integration ends, reviews ...:
    catalog.services.references_to_many) and linked from no other observation. 409 otherwise, nothing changed."""
    if d.decision != DecisionKind.CREATED or d.resource is None:
        raise FieldErrors({"archive": ["Only a record created from this candidate can be archived when undoing."]})
    resource = Resource.objects.select_for_update().get(organization=org, id=d.resource.id)
    if resource.archived_at is not None:
        return
    refs = services.references_to_many([resource])[resource.pk]
    # Discovery's own links are counted below without this candidate's decision (it goes with the undo).
    refs.pop(str(ReconciliationDecision._meta.verbose_name), None)
    shared = ReconciliationDecision.objects.filter(resource=resource).exclude(id=d.id).count()
    if shared:
        refs = {**refs, "linked discovery source": shared}
    if refs:
        raise Conflict(
            f"{resource.name} isn't archived because it has {services.references_phrase(refs)}. Undo without "
            "archiving to keep it.",
            extra={"blocking_references": refs},
        )
    if edited_since(org, resource, d.created_at):
        raise Conflict(
            f"{resource.name} was changed after it was created from this candidate, so it isn't archived. Undo "
            "without archiving to keep it, or archive it from its own page."
        )
    services.archive_resource(org, actor, resource)


@transaction.atomic
def reopen(org, actor, observation, data=None):
    """Undo the observation's decision (its only one: a decision replaces the last): the link, the created link or the
    dismissal is removed and every record stays. With `archive: true`, the record created from the candidate
    is archived too (audited), only while it is still the candidate's own (`_archive_created`; 409 otherwise)."""
    data = data or {}
    archive = data.get("archive", False)
    if not isinstance(archive, bool):
        raise FieldErrors({"archive": ["Choose yes or no."]})
    obs = _locked(org, observation)
    d = ReconciliationDecision.objects.filter(observation=obs).select_related("resource").first()
    if d is None:
        raise Conflict("This candidate has no decision to undo.")
    if archive:
        _archive_created(org, actor, obs, d)
    target = d.resource if d.resource_id else obs
    changes: dict[str, Any] = {"observation": str(obs.id), "source_name": obs.display_name, "was": d.decision}
    if archive:
        changes["archived"] = True
    from . import assistant

    if assistant.on_reopen(org, obs):
        changes["assistant_suggestion"] = "undecided again"  # what was recorded on it is in its own audit events
    services.audit(org, actor, "observation.reopened", target, changes)
    d.delete()
    drift.forget(org, obs)
    note_credentials(org)
    return obs


def suggested_owners(org, observations):
    """{observation id: [{name, email, external_id, resolved, person}]}: each owner matched to a Person by Entra object
    ID (the Person's user's Entra identity: core.UserIdentity with an Entra issuer, whose subject is the object ID) and else by email, case-insensitive. An owner the
    source couldn't name (no User.ReadBasic.All) has `resolved` false: the named gap "owner not resolved". Only a
    suggestion: nothing assigns it."""
    from catalog.models import Person
    from core.graph import LOGIN_HOST as ENTRA_LOGIN_HOST
    from core.models import UserIdentity

    emails = {o["email"].lower() for obs in observations for o in obs.data.get("owners", []) if o.get("email")}
    ids = {
        str(o.get("external_id") or "").lower()
        for obs in observations
        for o in obs.data.get("owners", [])
        if o.get("external_id")
    }
    living = Person.objects.filter(organization=org, anonymized_at__isnull=True)
    people = {p.email.lower(): p for p in living.exclude(email="") if p.email.lower() in emails}
    by_object: dict[str, Any] = {}
    if ids:
        users = {
            user_id: object_id.lower()
            for user_id, object_id in UserIdentity.objects.filter(
                issuer__startswith=f"https://{ENTRA_LOGIN_HOST}/", subject__in=ids | {i.upper() for i in ids}
            ).values_list("user_id", "subject")
        }
        for person in living.filter(user_id__in=list(users)):
            if person.user_id is not None:
                by_object[users[person.user_id]] = person
    out = {}
    for obs in observations:
        rows = []
        for o in obs.data.get("owners", []):
            oid = str(o.get("external_id") or "").lower()
            match = by_object.get(oid) or people.get((o.get("email") or "").lower())
            name = o.get("name") or o.get("email") or ""
            rows.append(
                {
                    "name": name,
                    "email": o.get("email", ""),
                    "external_id": oid,
                    "resolved": bool(name),
                    "person": {"id": str(match.id), "name": match.name, "active": match.active} if match else None,
                }
            )
        out[obs.id] = rows
    return out


# The record kinds a candidate is suggested to link to: its own, and for an AI agent an application too (an Entra app
# that became an agent may already be catalogued as an application). Linking itself takes any record kind.
COMPATIBLE = {
    ObservationKind.APPLICATION: (Kind.APPLICATION,),
    ObservationKind.COMPONENT: (Kind.COMPONENT,),
    ObservationKind.AI_AGENT: (Kind.AI_AGENT, Kind.APPLICATION),
}
SUGGESTIONS_MAX = 10


def suggestions(org, obs, limit=3):
    """Live records of compatible kinds whose name is like the source name (pg_trgm `%`, threshold 0.3, or the same
    name ignoring case), best first, each with a score from 0 to 1: the larger of the trigram similarity and the word
    similarity of the record's name within the source's (so "Zoom" scores 1 against "Zoom Workplace"); the same name
    scores 1. Only a suggestion: nothing links on its own."""
    from django.contrib.postgres.search import TrigramSimilarity

    name = obs.display_name
    # Only ranks the few `%` finds: word_similarity(name, source) as a filter would be `name <% source`, which the
    # trigram index can't serve with the column on that side.
    word = Func(F("name"), Value(name), function="word_similarity", output_field=FloatField())
    rows = (
        Resource.objects.filter(
            organization=org, kind__in=COMPATIBLE.get(obs.kind, RECORD_KINDS), archived_at__isnull=True
        )
        # `%` uses the name's trigram index; the scores order the few found.
        .filter(Q(name__trigram_similar=name) | Q(name__iexact=name))
        .annotate(score=Greatest(TrigramSimilarity("name", name), word))
        .order_by("-score", "name", "id")
        .values_list("id", "kind", "name", "score")[:limit]
    )
    return [
        {"id": str(i), "kind": k, "name": n, "score": 1.0 if n.lower() == name.lower() else round(min(s, 1.0), 2)}
        for i, k, n, s in rows
    ]


def suggested_matches(org, observations, limit=3):
    """{observation id: [records with a score]} (suggestions), for the candidates on a page."""
    return {obs.id: suggestions(org, obs, limit) for obs in observations}


def reviewers(org):
    """Users a connector's candidates can be routed to: active Admins and Editors."""
    return get_user_model().objects.filter(
        is_active=True, membership__organization=org, membership__role__in=services.WRITE_ROLES
    )


def scheduled_runs(now):
    """The `discovery.connectors` schedule (discovery/apps.py), every minute: fail runs that lost their lease and queue
    the requested and daily connector runs, each a `discovery.run` job. It runs none itself, so one slow tenant can't
    hold up this schedule, the others or another connector."""
    return {"connector_runs_queued": due_runs(now)}


# ---- run history retention -----------------------------------------------------------------------------------

KEEP_RUNS = 100  # finished runs kept per connector: about three months of daily runs


def prune_runs(now=None):
    """The `discovery.runs_prune` schedule (discovery/apps.py), daily: delete each connector's finished runs beyond its
    newest KEEP_RUNS. A queued or running run is never deleted, nor a connector's newest successful run (the licence
    usage source compares tenants through it). Not audited, as jobs.prune: run history is the worker's bookkeeping;
    what a run changed is on the observations and in the audit log."""
    from django.db.models import Window
    from django.db.models.functions import RowNumber

    finished = ConnectorRun.objects.exclude(status__in=ACTIVE_RUN_STATUSES)
    newest = [F("created_at").desc(), F("id").desc()]
    ranked = finished.annotate(n=Window(RowNumber(), partition_by=[F("connector_id")], order_by=newest))
    doomed = set(ranked.filter(n__gt=KEEP_RUNS).values_list("id", flat=True))
    if doomed:
        succeeded = finished.filter(status=RunStatus.SUCCEEDED).order_by("connector_id", "-finished_at", "-id")
        doomed -= set(succeeded.distinct("connector_id").values_list("id", flat=True))
    ids = sorted(doomed)
    deleted = 0
    for start in range(0, len(ids), 1000):
        deleted += ConnectorRun.objects.filter(id__in=ids[start : start + 1000]).delete()[0]
    return {"connector_runs_pruned": deleted}
