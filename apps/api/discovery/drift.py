"""Drift: "source differs" per field (reconciliation; accept_drift; reject_drift).

For an observation linked to a record (a linked or created decision), each source declares which curated fields it
can speak for (`drift_values(observation, record_kind)` in its module; name and description by default). A field
whose source value is set and differs from the record's is stored as a SourceDifference with `since` (when this source
value started to differ). Rows are recomputed on every run of the connector, on every decision and on every record
edit (catalog.services.update_resource calls `record_changed`), so they are current whatever changed.

Discovery never writes a curated field on its own. Applying a difference is an ordinary audited record edit
through catalog.services.update_resource (`resource.updated`, with `discovery` naming the observation and connector);
ignoring one hides it until the source value changes again (`observation.drift_ignored`). A gone observation has no
differences: it is "no longer seen" instead (discovery/missing.py)."""

from collections.abc import Iterable
from typing import Any

from django.db import transaction
from django.utils import timezone

from catalog import services
from catalog.models import Environment, Resource
from core.problems import Conflict, FieldErrors

from .models import DecisionKind, Observation, ReconciliationDecision, SourceDifference

# The curated fields a source may speak for, in display order, with their labels.
LABELS = {
    "name": "Name",
    "description": "Description",
    "documentation_url": "Documentation URL",
    "entra_app_id": "Entra application (client) ID",
    "service_principal_id": "Entra service principal ID",
    "azure_resource_id": "Azure resource ID",
    "environment": "Environment",
    "pp_environment_id": "Power Platform environment ID",
    "external_id": "External ID",
}
RESOURCE_FIELDS = ("name", "description")
# Identifiers compared case-insensitively (GUIDs, ARM resource IDs).
CASELESS = {"entra_app_id", "service_principal_id", "azure_resource_id", "pp_environment_id"}
# Longest value each field takes (catalog.services limits), so a difference can always be applied.
MAX = {
    "name": services.NAME_MAX,
    "description": services.TEXT_MAX,
    "documentation_url": services.URL_MAX["documentation_url"],
    **{f: services.SHORT_TEXT_MAX[f] for f in ("azure_resource_id", "pp_environment_id", "external_id")},
    "entra_app_id": 36,
    "service_principal_id": 36,
    "environment": 10,
}


def fields_of(kind: str) -> list[str]:
    """The fields of LABELS a record of this kind has and an editor can set."""
    editable = {f for k, fields in services.EXTENSION_TEXT.items() if k == kind for f in fields}
    editable |= {f for k, fields in services.EXTENSION_OTHER.items() if k == kind for f in fields}
    return [f for f in LABELS if f in RESOURCE_FIELDS or f in editable]


def default_values(observation: Observation, kind: str) -> dict[str, str]:
    data = observation.data if isinstance(observation.data, dict) else {}
    return {"name": observation.display_name, "description": str(data.get("description") or "")}


def _module(observation: Observation):
    from .engine import SOURCES

    return next((m for s, m in SOURCES.items() if s == observation.connector.source), None)


def _valid(field: str, value: str) -> bool:
    if not value or len(value) > MAX[field]:
        return False
    if field == "documentation_url":
        return value.startswith("https://")
    if field in ("entra_app_id", "service_principal_id"):
        return services.is_uuid(value.lower())
    if field == "environment":
        return value in Environment.values
    return True


def source_values(observation: Observation, kind: str) -> dict[str, str]:
    """{field: the source's value} for the fields a record of `kind` has; empty and unusable values left out (a source
    never proposes clearing a curated value)."""
    declared = getattr(_module(observation), "drift_values", None)
    raw = declared(observation, kind) if declared else default_values(observation, kind)
    if "name" in raw:
        raw["name"] = str(raw["name"] or "")[: services.NAME_MAX]
    wanted = fields_of(kind)
    out = {}
    for field in wanted:
        value = raw.get(field)
        # Kept as the source cased it (an ARM resource ID's casing is the source's); compared caselessly (same()).
        value = value.strip() if isinstance(value, str) else ""
        if _valid(field, value):
            out[field] = value
    return out


def curated_value(resource: Resource, field: str) -> str:
    holder = resource if field in RESOURCE_FIELDS else resource.extension
    value = getattr(holder, field, "") if holder is not None else ""
    return value if isinstance(value, str) else ("" if value is None else str(value))


def same(field: str, a: str, b: str) -> bool:
    a, b = (a or "").strip(), (b or "").strip()
    return a.lower() == b.lower() if field in CASELESS else a == b


def differences(observation: Observation, resource: Resource) -> dict[str, str]:
    """{field: source value} where the source differs from the record now. None while the observation is gone or out
    of scope (its values are from before the scope left it out)."""
    if observation.gone_at is not None or observation.out_of_scope:
        return {}
    return {
        field: value
        for field, value in source_values(observation, resource.kind).items()
        if not same(field, value, curated_value(resource, field))
    }


def _linked(org, observation_ids: Iterable[Any]):
    return (
        Observation.objects.filter(organization=org, id__in=list(observation_ids))
        .exclude(decision__isnull=True)
        .exclude(decision__decision=DecisionKind.DISMISSED)
        .select_related(
            "connector",
            "decision__resource__application",
            "decision__resource__component",
            "decision__resource__ai_agent",
        )
    )


def recompute(org, observation_ids: Iterable[Any], now=None) -> int:
    """Bring the stored differences of these observations up to date: new ones from `now`, a changed source value
    restarts `since` and ends an ignore, gone ones are deleted, as are those of observations no longer linked. Returns
    how many differences are open (not ignored) afterwards."""
    now = now or timezone.now()
    ids = list(observation_ids)
    if not ids:
        return 0
    existing = {
        (d.observation_id, d.field): d
        for d in SourceDifference.objects.filter(organization=org, observation_id__in=ids)
    }
    to_create, to_update = [], []
    open_ = 0
    for obs in _linked(org, ids):
        for field, value in differences(obs, obs.decision.resource).items():
            row = existing.pop((obs.id, field), None)
            if row is None:
                to_create.append(
                    SourceDifference(organization=org, observation=obs, field=field, source_value=value, since=now)
                )
                open_ += 1
                continue
            if not same(field, row.source_value, value):
                row.source_value, row.since, row.ignored_at, row.ignored_by = value, now, None, None
                to_update.append(row)
            elif row.source_value != value:
                row.source_value = value  # spacing or case only: the same difference
                to_update.append(row)
            open_ += row.ignored_at is None
    if existing:
        SourceDifference.objects.filter(id__in=[d.id for d in existing.values()]).delete()
    SourceDifference.objects.bulk_create(to_create, batch_size=500)
    SourceDifference.objects.bulk_update(
        to_update, ["source_value", "since", "ignored_at", "ignored_by"], batch_size=500
    )
    return open_


def recompute_connector(connector, now=None) -> int:
    """After a run: every decided observation of the connector (cleans up rows of undecided ones too)."""
    ids = list(Observation.objects.filter(connector=connector).values_list("id", flat=True))
    return recompute(connector.organization, ids, now)


def record_changed(org, resource) -> None:
    """After a record edit (catalog.services.update_resource): the observations linked to it."""
    ids = list(
        ReconciliationDecision.objects.filter(organization=org, resource_id=resource.id)
        .exclude(decision=DecisionKind.DISMISSED)
        .values_list("observation_id", flat=True)
    )
    if ids:
        recompute(org, ids)


def forget(org, observation) -> None:
    """The observation is no longer linked (undo): its differences go."""
    SourceDifference.objects.filter(organization=org, observation_id=observation.id).delete()


# ---- reading ---------------------------------------------------------------------------------------------------------


def entries(observation: Observation, resource: Resource, rows: Iterable[SourceDifference]) -> list[dict[str, Any]]:
    """The observation's stored differences as API rows, checked against the record as it is now (a row a write path
    missed is never shown), in LABELS order: field, label, curated, source, since, ignored."""
    order = list(LABELS)
    out = []
    for d in sorted(rows, key=lambda d: order.index(d.field) if d.field in order else len(order)):
        curated = curated_value(resource, d.field)
        if observation.gone_at is not None or observation.out_of_scope or same(d.field, d.source_value, curated):
            continue
        out.append(
            {
                "field": d.field,
                "label": LABELS.get(d.field, d.field),
                "curated": curated,
                "source": d.source_value,
                "since": d.since,
                "ignored": d.ignored_at is not None,
                "ignored_at": d.ignored_at,
                "ignored_by": d.ignored_by,
            }
        )
    return out


# ---- decisions -------------------------------------------------------------------------------------------------------


def _locked(org, observation, resource=None):
    """The observation (locked first: the order reopen and missing use too), its decision, and the record: the one the
    caller locked for If-Match, which must be the decision's (409 otherwise), else locked here."""
    obs = Observation.objects.select_for_update().select_related("connector").get(organization=org, id=observation.id)
    decision = ReconciliationDecision.objects.filter(observation=obs).first()
    if decision is None or decision.decision == DecisionKind.DISMISSED or decision.resource_id is None:
        raise Conflict("This observation isn't linked to a record, so nothing differs.")
    if resource is not None and resource.id != decision.resource_id:
        raise Conflict("The observation is linked to another record now. Reload and review it again.")
    if obs.gone_at is not None:
        raise Conflict("The source no longer lists this object, so there is nothing to apply.")
    if obs.out_of_scope:
        raise Conflict("The connector's scope leaves this object out, so there is nothing to apply.")
    if resource is None:
        resource = (
            Resource.objects.select_for_update(of=("self",))
            .select_related("application", "component", "ai_agent")
            .get(organization=org, id=decision.resource_id)
        )
    return obs, decision, resource


def _chosen(data, current: dict[str, str], ignored: set[str], name="fields") -> list[str]:
    raw = data.get(name)
    if raw is None:
        fields = [f for f in current if f not in ignored]
        if not fields:
            raise Conflict("Nothing differs from the source now.")
        return fields
    if not isinstance(raw, list) or not raw or not all(isinstance(f, str) and f in current for f in raw):
        choices = ", ".join(current) or "none"
        raise FieldErrors({name: [f"Choose from the fields that differ: {choices}."]})
    return list(dict.fromkeys(raw))


def _check_seen(data, current: dict[str, str], fields: list[str]) -> None:
    """`seen` {field: source value} is what the person reviewed, required for every chosen field (400 otherwise); a
    run since that changed one refuses the write (409)."""
    seen = data.get("seen")
    if not isinstance(seen, dict) or not all(isinstance(seen.get(f), str) for f in fields):
        raise FieldErrors({"seen": [f"Send the source value you reviewed for each field: {', '.join(fields)}."]})
    stale = [f for f in fields if not same(f, seen[f], current[f])]
    if stale:
        raise Conflict(
            f"The source changed {', '.join(LABELS.get(f, f) for f in stale)} while you were reviewing. Nothing was "
            "changed; review it again."
        )


@transaction.atomic
def apply(org, actor, observation, data, resource=None):
    """Take the source's value of each chosen field (default: every difference not ignored) into the record: an
    ordinary record edit (catalog.services.update_resource), audited as `resource.updated` with `discovery` naming the
    observation and connector. `resource` is the record as the caller locked it for If-Match. Returns the record."""
    obs, decision, resource = _locked(org, observation, resource)
    if resource.archived_at is not None:
        raise Conflict(f"{resource.name} is archived. Restore it first.")
    current = differences(obs, resource)
    ignored = set(
        SourceDifference.objects.filter(observation=obs, ignored_at__isnull=False).values_list("field", flat=True)
    )
    fields = _chosen(data, current, ignored)
    _check_seen(data, current, fields)
    marker = {
        "discovery": {
            "observation": str(obs.id),
            "source_name": obs.display_name,
            "connector": obs.connector.name,
            "fields": fields,
        }
    }
    resource = services.update_resource(org, actor, resource, {f: current[f] for f in fields}, audit_extra=marker)
    recompute(org, [obs.id])  # update_resource did too; this one also covers a record whose edit changed nothing
    return resource


@transaction.atomic
def ignore(org, actor, observation, data):
    """Keep the record's value of each chosen field (default: every difference not ignored) until the source's value
    changes again. Audited as `observation.drift_ignored` on the record."""
    obs, decision, resource = _locked(org, observation)
    recompute(org, [obs.id])
    rows = {d.field: d for d in SourceDifference.objects.select_for_update().filter(observation=obs)}
    differing = differences(obs, resource)
    current = {f: d.source_value for f, d in rows.items() if f in differing}
    ignored = {f for f, d in rows.items() if d.ignored_at is not None}
    fields = _chosen(data, current, ignored)
    _check_seen(data, current, fields)
    now = timezone.now()
    SourceDifference.objects.filter(id__in=[rows[f].id for f in fields]).update(ignored_at=now, ignored_by=actor)
    services.audit(
        org,
        actor,
        "observation.drift_ignored",
        resource,
        {
            "observation": str(obs.id),
            "source_name": obs.display_name,
            "connector": obs.connector.name,
            "fields": {f: {"curated": curated_value(resource, f), "source": current[f]} for f in fields},
        },
    )
    return obs
