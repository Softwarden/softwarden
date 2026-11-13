"""No longer seen in source (reconciliation).

A run that no longer lists an object sets its observation's `gone_at` (discovery/engine._apply); a scope the run
couldn't read never does (`not_read`). A gone observation that is linked to a record (a linked or created decision) is
"missing": a Needs attention row (`discovery_missing`, `attention_rows`) with the record, the source, the connector and
since when, until someone resolves it:

- acknowledge: keep the record (`observation.missing_resolved`, resolution kept);
- archive the record: catalog.services.archive_resource (`resource.archived`) and `observation.missing_resolved`,
  resolution archived. Refused while another source linked to the record still sees it.

Both record the resolution on the decision. When the object comes back the engine clears it (the row would have gone
anyway: the observation is no longer gone), so a later disappearance asks again. Nothing here deletes a record."""

from typing import Any
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from catalog import services
from catalog.models import Resource
from core.drf import timestamp
from core.problems import Conflict, FieldErrors

from .models import DecisionKind, GoneResolution, Observation, ReconciliationDecision

NOTE_MAX = 500


def missing(org):
    """The organization's missing observations (engine.missing_q: from enabled connectors, records live), with
    their connector and record."""
    from .engine import missing_q

    return (
        Observation.objects.filter(organization=org)
        .filter(missing_q())
        .select_related("connector", "decision__resource")
    )


def attention_rows(org) -> list[dict[str, Any]]:
    """One Needs attention row per missing observation (a row per object, not per connector: each is a record to keep
    or archive), routed to the record's IT steward, else the connector's reviewer. Oldest first."""
    zone = ZoneInfo(org.timezone)
    rows = []
    for obs in missing(org):
        resource, connector = obs.decision.resource, obs.connector
        assert obs.gone_at is not None  # missing() filters on it
        since = obs.gone_at.astimezone(zone).date().isoformat()
        rows.append(
            {
                "id": str(obs.id),
                "kind": "discovery_missing",
                "name": resource.name,
                "record": {"id": str(resource.id), "kind": resource.kind, "name": resource.name},
                "observation": {"id": str(obs.id), "name": obs.display_name, "external_id": obs.external_id},
                "connector": {"id": str(connector.id), "name": connector.name, "source": connector.source},
                "since": since,
                "why": f"No longer seen in {connector.name} since {since} (“{obs.display_name}”). Keep or archive the "
                "record.",
                "steward_id": resource.it_steward_id or connector.reviewer_id,
            }
        )
    rows.sort(key=lambda r: (r["since"], r["name"].lower(), r["id"]))
    return rows


def _locked(org, observation) -> tuple[Observation, ReconciliationDecision]:
    obs = Observation.objects.select_for_update().select_related("connector").get(organization=org, id=observation.id)
    decision = (
        ReconciliationDecision.objects.select_for_update(of=("self",))
        .filter(observation=obs)
        .select_related("resource")
        .first()
    )
    if decision is None or decision.decision == DecisionKind.DISMISSED or decision.resource is None:
        raise Conflict("This observation isn't linked to a record.")
    if obs.gone_at is None:
        raise Conflict("The source lists this object again, so there is nothing to resolve.")
    if decision.gone_acknowledged_at is not None:
        raise Conflict("Someone already resolved this. Reload to see what they decided.")
    return obs, decision


def _note(data) -> str:
    raw = data.get("note")
    note = raw.strip() if isinstance(raw, str) else ""
    if raw not in (None, "") and not isinstance(raw, str):
        raise FieldErrors({"note": ["Send the note as text."]})
    if len(note) > NOTE_MAX:
        raise FieldErrors({"note": [f"Use {NOTE_MAX} characters or fewer."]})
    return note


def _resolve(org, actor, obs, decision, resolution: str, note: str) -> None:
    decision.gone_acknowledged_at = timezone.now()
    decision.gone_acknowledged_by = actor
    decision.gone_resolution = resolution
    decision.gone_note = note
    decision.save(
        update_fields=["gone_acknowledged_at", "gone_acknowledged_by", "gone_resolution", "gone_note", "updated_at"]
    )
    changes: dict[str, Any] = {
        "observation": str(obs.id),
        "source_name": obs.display_name,
        "connector": obs.connector.name,
        "resolution": resolution,
        "gone_at": timestamp(obs.gone_at),
    }
    if note:
        changes["reason"] = note  # the audit log's observation summary reads `reason`
    services.audit(org, actor, "observation.missing_resolved", decision.resource, changes)


@transaction.atomic
def acknowledge(org, actor, observation, data) -> Observation:
    """Keep the record: the row leaves Needs attention; the observation stays gone (and linked) until it returns."""
    note = _note(data)
    obs, decision = _locked(org, observation)
    _resolve(org, actor, obs, decision, GoneResolution.KEPT, note)
    return obs


@transaction.atomic
def archive(org, actor, observation, data, resource: Resource | None = None) -> Observation:
    """Archive the record (catalog.services.archive_resource, audited) and resolve the row. `resource` is the record as
    the caller locked it for If-Match. Refused (409) while another source linked to the record still sees it."""
    note = _note(data)
    obs, decision = _locked(org, observation)
    assert decision.resource is not None  # _locked refuses a dismissal
    if resource is not None and resource.id != decision.resource.id:
        raise Conflict("The observation is linked to another record now. Reload and review it again.")
    # Locked after the observation, as reopen and drift lock them (the view locked it already for If-Match).
    resource = resource or Resource.objects.select_for_update().get(organization=org, id=decision.resource.id)
    still = (
        Observation.objects.filter(organization=org, decision__resource=resource, gone_at__isnull=True)
        .exclude(decision__decision=DecisionKind.DISMISSED)
        .select_related("connector")
        .first()
    )
    if still is not None:
        if still.out_of_scope:
            # The source still has it; the connector's scope leaves it out, so it isn't read (reconciliation).
            raise Conflict(
                f"{still.connector.name} still lists {resource.name} (as “{still.display_name}”, out of this "
                "connector's scope), so it stays. Acknowledge this instead."
            )
        raise Conflict(
            f"{still.connector.name} still sees {resource.name} (as “{still.display_name}”), so it stays. "
            "Acknowledge this instead."
        )
    services.archive_resource(org, actor, resource)
    decision.resource = resource
    _resolve(org, actor, obs, decision, GoneResolution.ARCHIVED, note)
    return obs
