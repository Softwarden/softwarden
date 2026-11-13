"""The access review report: every live record with its owners, flagged.

Records: applications, components, AI agents (with the ai_agents capability) and integrations that aren't archived;
retired ones only with `include_retired`. Flags (a record can carry several):

- `inactive_owner`: its business or technical owner is a Person marked inactive (anonymized people included). The same
  condition as the "Inactive owner" ownership gap, its data-quality rule and the Ownership gaps queue in Needs
  attention (catalog/state.py, catalog/quality.py), so it reuses that label and adds no queue of its own.
- `owner_left`: an owner is still an active Person, but the user account linked to them was deactivated or removed
  from the organisation: they have probably left and nobody has updated the Person yet. People have no departure
  date, so these two flags are how "left" shows.
- `no_owner`: neither owner (an integration has no business owner, so: no technical owner).
- `entra_owner_mismatch` (informational): the record is linked to a discovered Entra app whose user owners aren't all
  the record's catalog owners: an owner Entra lists who isn't matched to a Person, or is matched to someone who isn't
  the business or technical owner. Matching is discovery's (discovery.engine.suggested_owners: Entra object ID, else
  email). Service principals that own an app are left out.

Only stored data is read: discovery's observations as of their last run; nothing here calls Microsoft."""

from typing import Any

from django.db.models import Q

from catalog.models import Kind, Lifecycle, Resource
from catalog.state import OWNERSHIP_LABELS
from core.capabilities import AI_AGENTS, has_capability
from core.models import Membership
from core.problems import FieldErrors
from discovery.engine import suggested_owners
from discovery.models import Observation, Source

from .footprint import record_path

FLAGS: dict[str, str] = {
    "inactive_owner": OWNERSHIP_LABELS["inactive_owner"],
    "owner_left": "Owner left",
    "no_owner": "No owner",
    "entra_owner_mismatch": "Entra owners differ",
}
KINDS = (Kind.APPLICATION, Kind.COMPONENT, Kind.AI_AGENT, Kind.INTEGRATION)
FILTERS = ("kind", "flag", "owner", "include_retired")
UNKNOWN_FILTER = "This filter isn't available here."
OWNER_FIELDS = (("business_owner", "Business owner"), ("technical_owner", "Technical owner"))


def _csv_values(params, name, allowed, errors) -> list[str] | None:
    raw = params.get(name, "")
    if not raw:
        return None
    values = [v.strip() for v in raw.split(",") if v.strip()]
    if not values or any(v not in allowed for v in values):
        errors[name] = [f"Choose from {', '.join(allowed)}, comma-separated."]
        return None
    return list(dict.fromkeys(values))


def parse_filters(params) -> dict[str, Any]:
    errors: dict[str, list[str]] = {}
    for key in sorted(k for k in params if k not in FILTERS):
        errors[key] = [UNKNOWN_FILTER]
    found: dict[str, Any] = {}
    kinds = _csv_values(params, "kind", [str(k) for k in KINDS], errors)
    if kinds:
        found["kind"] = kinds
    flags = _csv_values(params, "flag", list(FLAGS), errors)
    if flags:
        found["flag"] = flags
    owner = params.get("owner", "")
    if owner:
        from catalog.services import is_uuid

        if not is_uuid(owner):
            errors["owner"] = ["Send a person's ID."]
        else:
            found["owner"] = owner
    raw = params.get("include_retired", "")
    if raw:
        if raw not in ("true", "false"):
            errors["include_retired"] = ["Use one of: true, false."]
        else:
            found["include_retired"] = raw == "true"
    if errors:
        raise FieldErrors(errors)
    return found


def _owner_json(person, members: set[int]) -> dict[str, Any] | None:
    if person is None:
        return None
    if not person.active:
        status = "inactive"
    elif person.user_id is not None and person.user_id not in members:
        status = "left"
    else:
        status = "active"
    return {"id": str(person.id), "name": person.name, "active": person.active, "status": status}


def _entra_owners(org, record_ids) -> dict[str, list[dict[str, Any]]]:
    """{record id: [Entra user owners of the Entra observations linked to it, each once]} with the Person each
    matches. Other sources' owners (Power Platform's are names only) aren't compared."""
    observations = list(
        Observation.objects.filter(
            organization=org,
            connector__source=Source.ENTRA,
            decision__resource_id__in=record_ids,
            data__has_key="owners",
            gone_at__isnull=True,
        )
        .exclude(data__owners=[])
        .select_related("connector", "decision")
    )
    matched = suggested_owners(org, observations)
    out: dict[str, list[dict[str, Any]]] = {}
    for obs in observations:
        raw = obs.data.get("owners") or []
        seen = out.setdefault(str(obs.decision.resource_id), [])
        for original, row in zip(raw, matched.get(obs.id, []), strict=False):
            if isinstance(original, dict) and original.get("type") == "service_principal":
                continue
            key = row["external_id"] or row["email"].lower() or row["name"]
            if any((o["external_id"] or o["email"].lower() or o["name"]) == key for o in seen):
                continue
            seen.append({**row, "source": obs.connector.name})
    return out


def rows(org, filters: dict[str, Any]) -> list[dict[str, Any]]:
    """Every record in scope (the kind, owner and include_retired filters), flagged; `flagged` applies the flag filter."""
    kinds = [k for k in filters.get("kind", KINDS) if k != Kind.AI_AGENT or has_capability(org, AI_AGENTS)]
    qs = Resource.objects.filter(organization=org, kind__in=kinds, archived_at__isnull=True).select_related(
        "business_owner", "technical_owner"
    )
    if not filters.get("include_retired"):
        qs = qs.exclude(lifecycle=Lifecycle.RETIRED)
    if "owner" in filters:
        qs = qs.filter(Q(business_owner_id=filters["owner"]) | Q(technical_owner_id=filters["owner"]))
    records = list(qs.order_by("name", "id"))
    members = set(Membership.objects.filter(organization=org, user__is_active=True).values_list("user_id", flat=True))
    entra = _entra_owners(org, [r.id for r in records])
    out = []
    for r in records:
        owners = {f: _owner_json(getattr(r, f), members) for f, _ in OWNER_FIELDS}
        if r.kind == Kind.INTEGRATION:
            owners["business_owner"] = None  # none applies, whatever an old row holds
        present: list[tuple[str, dict[str, Any]]] = [
            (label, owner) for f, label in OWNER_FIELDS if (owner := owners[f]) is not None
        ]
        flags: list[str] = []
        why: list[str] = []
        if any(o["status"] == "inactive" for _, o in present):
            flags.append("inactive_owner")
            why += [f"{label} {o['name']} is inactive." for label, o in present if o["status"] == "inactive"]
        if any(o["status"] == "left" for _, o in present):
            flags.append("owner_left")
            why += [
                f"{label} {o['name']}'s account was deactivated or removed from the organization."
                for label, o in present
                if o["status"] == "left"
            ]
        if not present:
            flags.append("no_owner")
            why.append("No technical owner." if r.kind == Kind.INTEGRATION else "No business or technical owner.")
        catalog_ids = {o["id"] for _, o in present}
        listed = entra.get(str(r.id), [])
        strangers = [o for o in listed if o["person"] is None or o["person"]["id"] not in catalog_ids]
        if strangers:
            flags.append("entra_owner_mismatch")
            names = ", ".join(
                o["person"]["name"] if o["person"] else (o["name"] or "an unresolved owner") for o in strangers
            )
            why.append(f"Entra also lists {names}, not a catalog owner.")
        out.append(
            {
                "record": {
                    "id": str(r.id),
                    "kind": r.kind,
                    "name": r.name,
                    "lifecycle": r.lifecycle,
                    "path": record_path(r.id, r.kind),
                },
                "business_owner": owners["business_owner"],
                "technical_owner": owners["technical_owner"],
                "flags": flags,
                "entra_owners": [
                    {
                        "name": o["name"],
                        "email": o["email"],
                        "resolved": o["resolved"],
                        "source": o["source"],
                        "person": o["person"],
                    }
                    for o in listed
                ],
                "why": " ".join(why),
            }
        )
    return out


def flagged(found: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
    """The rows carrying any of the `flag` filter's flags (all rows without that filter)."""
    if "flag" not in filters:
        return found
    wanted = set(filters["flag"])
    return [row for row in found if wanted & set(row["flags"])]


def counts(found: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(found),
        "flagged": sum(1 for r in found if r["flags"]),
        **{flag: sum(1 for r in found if flag in r["flags"]) for flag in FLAGS},
    }
