"""Discovered credentials (and their reminder source): the client secrets and
certificates a source saw on observed apps, with their end dates. Never key material, a secret or its hint.

- `upsert` runs inside a connector run's transaction: every credential the run saw is created or refreshed (and
  revived if it had been removed); every other credential of the connector is marked `removed_at`.
- `live` is the one definition of a credential that matters now: not removed, with an end date, on a live observation
  (not gone) of an enabled connector that nobody dismissed. Its owning record is the observation's linked or created
  record. The reminder source (`credential_expiry`, notify/reminders.py) and the dashboard's expiring credentials
  (money/reporting.expiring_credentials) both read it, so they can't disagree.

These are derived rows, written by a run like observations are: not audited one by one (the run is the record)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.db.models import Q

from .models import CredentialType, DecisionKind, DiscoveredCredential

FIELDS = ("credential_type", "display_name", "origin", "object_id", "expires_on")


def upsert(connector, observations: dict[Any, Any], found: dict[Any, list[dict[str, Any]]], now) -> dict[str, int]:
    """`observations` {key: Observation} kept this run, `found` {key: [credential values]}. Returns counts."""
    existing = {
        (c.observation_id, c.key_id): c for c in DiscoveredCredential.objects.filter(observation__connector=connector)
    }
    seen: set[tuple[Any, str]] = set()
    to_create, to_update = [], []
    for key, values in found.items():
        obs = observations.get(key)
        if obs is None:
            continue
        for v in values:
            ident = (obs.id, v["key_id"])
            if ident in seen:
                continue
            seen.add(ident)
            expires = date.fromisoformat(v["expires_on"]) if v.get("expires_on") else None
            row = existing.get(ident)
            if row is None:
                to_create.append(
                    DiscoveredCredential(
                        organization_id=connector.organization_id,
                        observation=obs,
                        key_id=v["key_id"],
                        credential_type=v["credential_type"],
                        display_name=v.get("display_name", ""),
                        origin=v["origin"],
                        object_id=v.get("object_id", ""),
                        expires_on=expires,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
                continue
            row.credential_type, row.display_name, row.origin = (
                v["credential_type"],
                v.get("display_name", ""),
                v["origin"],
            )
            row.object_id, row.expires_on, row.last_seen_at, row.removed_at = v.get("object_id", ""), expires, now, None
            to_update.append(row)
    removed = 0
    for ident, row in existing.items():
        if ident not in seen and row.removed_at is None:
            row.removed_at = now
            to_update.append(row)
            removed += 1
    DiscoveredCredential.objects.bulk_create(to_create, batch_size=500)
    DiscoveredCredential.objects.bulk_update(
        to_update, [*FIELDS, "last_seen_at", "removed_at", "updated_at"], batch_size=500
    )
    return {"credentials": len(seen), "credentials_removed": removed}


def live(org):
    """Credentials that matter now (see the module docstring), with their observation, connector and decision."""
    return (
        DiscoveredCredential.objects.filter(
            organization=org,
            removed_at__isnull=True,
            expires_on__isnull=False,
            observation__gone_at__isnull=True,
            observation__connector__enabled=True,
        )
        .exclude(Q(observation__decision__decision=DecisionKind.DISMISSED))
        .select_related("observation__connector", "observation__decision")
    )


def unique_live(org) -> list[Any]:  # every expires_on is set (live)
    """`live`, once per (tenant, key ID): two connectors on one tenant see the same credential, which is one reminder
    and one dashboard row. The copy kept is the one whose app is linked to a record (routing), else the oldest
    connector's."""
    rows = live(org).select_related("observation__connector__microsoft_connection", "observation__decision__resource")
    best: dict[tuple[str, str], Any] = {}

    def rank(c):
        return (record_of(c) is None, c.observation.connector.created_at, str(c.id))

    for c in rows:
        connector = c.observation.connector
        conn = connector.microsoft_connection
        tenant = (conn.tenant_id or "").lower() if conn is not None else f"connector:{connector.id}"
        key = (tenant, c.key_id)
        if key not in best or rank(c) < rank(best[key]):
            best[key] = c
    return list(best.values())


def record_of(credential) -> Any:
    """The record the credential's app is linked to or was created as, or None."""
    decision = getattr(credential.observation, "decision", None)
    return decision.resource_id if decision is not None and decision.decision != DecisionKind.DISMISSED else None


def label(credential) -> str:
    what = "Certificate" if credential.credential_type == CredentialType.CERTIFICATE else "Client secret"
    name = credential.display_name or credential.key_id
    return f"{what} “{name}” expires"


def reminder_facts(org, today: date, ids=None) -> list[Any]:
    """The `credential_expiry` reminder source (notify/reminders.register_credential_source): one reminder per live
    credential, due on its end date, routed to its app's record (its IT steward) when there is one."""
    from notify.models import ReminderSource
    from notify.reminders import Fact

    rows = unique_live(org)
    if ids is not None:
        wanted = set(ids)
        rows = [c for c in rows if c.id in wanted]
    return [
        Fact(
            ReminderSource.CREDENTIAL_EXPIRY,
            c.id,
            c.expires_on,
            title=c.observation.display_name,
            label=label(c),
            source_field=c.key_id,
            resource_id=record_of(c),
        )
        for c in rows
    ]


def expiring_rows(org, today: date, days: int) -> list[dict[str, Any]]:
    """Dashboard rows: live credentials ending within `days` (or ended in the last `days`), soonest first."""
    rows = sorted(
        (c for c in unique_live(org) if today - timedelta(days=days) <= c.expires_on <= today + timedelta(days=days)),
        key=lambda c: (c.expires_on, c.display_name, str(c.id)),
    )
    out = []
    for c in rows:
        left = (c.expires_on - today).days
        decision = getattr(c.observation, "decision", None)
        resource = decision.resource if decision is not None and decision.resource_id else None
        out.append(
            {
                "kind": "discovered_credential",
                "id": str(c.id),
                "name": c.display_name or c.key_id,
                "prefix": "",
                "owner": None,
                "expires_on": c.expires_on.isoformat(),
                "days": left,
                "expired": left < 0,
                "credential_type": c.credential_type,
                "key_id": c.key_id,
                "application": {"id": str(c.observation_id), "name": c.observation.display_name},
                "record": (
                    {"id": str(resource.id), "kind": resource.kind, "name": resource.name}
                    if resource is not None
                    else None
                ),
            }
        )
    return out
