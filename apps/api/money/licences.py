"""Licence pools beyond their own fields: utilisation totals, the "proposed seat count" review and the seat
sync path.

Utilisation. A pool's utilisation is assigned ÷ purchased (`contracts.utilization`); assigned may exceed purchased
(overuse is shown, never refused). `totals` adds pools up per application and SKU (the SKU ID when set, else the SKU
name, case-insensitive) for the reports.

Seat review. What a source says about a SKU's seats arrives as a discovery observation of kind `license` (one per SKU
per connector; `data`: sku_id, sku_part_number, seats_purchased, seats_assigned; produced by the Entra licences
source, synthetic in tests). Discovery never overwrites curated data: when an observation's counts differ from a
pool bound to that SKU (its `sku_id`), the difference is a *proposal*, listed in a review queue like discovery's
candidates. Applying one is an ordinary audited pool edit (`license_pool.updated`, naming the observation) through
`contracts.update_pool`; dismissing one is recorded (SeatCountReview, `license_pool.seat_count_dismissed`) and holds
until the source's counts change. Proposals are derived on every read, never stored.

Sync path. A pool with `seat_sync` is the one exception to discovery never overwriting curated data: every Entra licences run (`sync_observed` through
discovery/entra_licenses.after_apply) calls `sync_seats` for each licence observation it saw, which overwrites that
pool's counts and sets `last_synced`. Only a pool bound to the SKU (its `sku_id`) with `seat_sync` on is ever written;
a bound pool without it gets proposals; a pool without a SKU ID is never touched. When two enabled connectors (two
tenants) report the same SKU the syncing pool is left alone and the run says so: its counts become proposals, one per
source, until one connector is turned off or its SKU excluded. Nothing else sets `last_synced`: people can't
(contracts._pool_fields refuses it), and applying a proposal by hand doesn't either.

Unbound SKUs. A SKU a source reports and no pool has (`unbound`) is a proposal of its own: create a pool for it
(`create_from_observation`, an ordinary audited `license_pool.created` naming the source) or bind an existing pool to
it (`bind`, an audited `license_pool.updated` setting the pool's SKU ID). Neither copies seat counts on its own: a
created pool starts with the observed counts as its initial values (the person chose to create it from them), a bound
pool keeps its own and shows any difference as a proposal (or syncs from the next run if `seat_sync` is chosen)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from catalog.services import _text, audit
from core.problems import Conflict, FieldErrors
from discovery.models import Observation, ObservationKind

from . import contracts
from .models import LicensePool, SeatCountReview, SeatReviewDecision

SEAT_FIELDS = ("seats_purchased", "seats_assigned")
REASON_MAX = 500


# ---- utilisation ----------------------------------------------------------------------------------------------------


def sku_key(pool: LicensePool) -> str:
    """What makes two pools the same SKU in the totals: the SKU ID when set, else the SKU name; case-insensitive."""
    return f"id:{pool.sku_id.strip().lower()}" if pool.sku_id.strip() else f"name:{pool.sku_name.strip().lower()}"


def _utilisation(purchased: int, assigned: int) -> dict[str, Any]:
    return {
        "seats_purchased": purchased,
        "seats_assigned": assigned,
        "seats_available": purchased - assigned,
        "utilization": round(100 * assigned / purchased, 1) if purchased else None,
        "overused": assigned > purchased,
    }


def totals(org) -> dict[str, Any]:
    """Seats per application and SKU, and for the organization: {"rows": [...], "total": {...}}. Each row: the
    application, the SKU (name of its first pool, the SKU ID), how many pools, and seats purchased, assigned, available
    (negative when overused), utilisation (one decimal; null with none purchased) and overuse. Rows by application name,
    then SKU name. Pools of archived applications count (the report shows them flagged)."""
    pools = (
        LicensePool.objects.filter(organization=org)
        .select_related("application")
        .order_by("application__name", "sku_name", "id")
    )
    grouped: dict[tuple[Any, str], list[LicensePool]] = {}
    for pool in pools:
        grouped.setdefault((pool.application_id, sku_key(pool)), []).append(pool)
    rows: list[dict[str, Any]] = []
    for group in grouped.values():
        first = group[0]
        purchased = sum(p.seats_purchased for p in group)
        assigned = sum(p.seats_assigned for p in group)
        rows.append(
            {
                "application": {
                    "id": str(first.application_id),
                    "name": first.application.name,
                    "archived": first.application.archived_at is not None,
                },
                "sku_name": first.sku_name,
                "sku_id": first.sku_id,
                "pools": len(group),
                **_utilisation(purchased, assigned),
            }
        )
    purchased = sum(r["seats_purchased"] for r in rows)
    assigned = sum(r["seats_assigned"] for r in rows)
    total = {
        "pools": sum(r["pools"] for r in rows),
        **_utilisation(purchased, assigned),
        "overused_rows": sum(1 for r in rows if r["overused"]),
    }
    return {"rows": rows, "total": total}


# ---- observed seat counts --------------------------------------------------------------------------------------------


def observed_sku(observation: Observation) -> str:
    """The SKU an observation is about (`data.sku_id`, else its external ID), lower-cased for matching."""
    raw = observation.data.get("sku_id") if isinstance(observation.data, dict) else None
    return str(raw or observation.external_id).strip().lower()


def observed_seats(observation: Observation) -> dict[str, int | None]:
    """{seats_purchased, seats_assigned} as the source said them; None for a missing or unusable value (never a
    proposal to change that field)."""
    data = observation.data if isinstance(observation.data, dict) else {}
    found: dict[str, int | None] = {}
    for field in SEAT_FIELDS:
        value = data.get(field)
        ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= contracts.SEATS_MAX
        found[field] = value if ok else None
    return found


def fingerprint(observed: dict[str, int | None]) -> str:
    """The observed counts a decision was taken on; a dismissed proposal comes back when they change."""
    return ":".join("-" if observed[f] is None else str(observed[f]) for f in SEAT_FIELDS)


def license_observations(org):
    """The organization's licence observations that are still seen, from enabled connectors."""
    return Observation.objects.filter(
        organization=org, kind=ObservationKind.LICENSE, gone_at__isnull=True, connector__enabled=True
    ).select_related("connector")


@dataclass(frozen=True)
class Proposal:
    """An observation's seat counts that differ from a pool bound to its SKU."""

    pool: LicensePool
    observation: Observation
    observed: dict[str, int | None]
    differences: list[dict[str, Any]]  # [{field, current, proposed}] for each field that differs
    review: SeatCountReview | None  # the current decision on this pair, if any

    @property
    def dismissed(self) -> bool:
        r = self.review
        return (
            r is not None and r.decision == SeatReviewDecision.DISMISSED and r.fingerprint == fingerprint(self.observed)
        )

    @property
    def status(self) -> str:
        return "dismissed" if self.dismissed else "open"


def differences(pool: LicensePool, observed: dict[str, int | None]) -> list[dict[str, Any]]:
    return [
        {"field": f, "current": getattr(pool, f), "proposed": observed[f]}
        for f in SEAT_FIELDS
        if observed[f] is not None and observed[f] != getattr(pool, f)
    ]


def proposals(org, *, pools: Iterable[Any] | None = None, include_dismissed: bool = False) -> list[Proposal]:
    """Every pool whose counts a source disagrees with: one proposal per pool and licence observation of its SKU that
    differs, syncing pools included (until the sync path catches up). Dismissed ones only when asked; by application,
    SKU name, then source. `pools` limits it to those pool IDs. Two queries when nothing is observed: one."""
    observations = list(license_observations(org))
    if not observations:
        return []
    by_sku: dict[str, list[Observation]] = {}
    for o in observations:
        by_sku.setdefault(observed_sku(o), []).append(o)
    qs = LicensePool.objects.filter(organization=org).exclude(sku_id="").select_related("application", "contract")
    if pools is not None:
        qs = qs.filter(id__in=list(pools))
    candidates = [p for p in qs if p.sku_id.strip().lower() in by_sku]
    if not candidates:
        return []
    reviews = {
        (r.pool_id, r.observation_id): r
        for r in SeatCountReview.objects.filter(organization=org, pool__in=candidates).select_related("decided_by")
    }
    found = []
    for pool in candidates:
        for o in by_sku[pool.sku_id.strip().lower()]:
            observed = observed_seats(o)
            diff = differences(pool, observed)
            if not diff:
                continue
            proposal = Proposal(pool, o, observed, diff, reviews.get((pool.id, o.id)))
            if proposal.dismissed and not include_dismissed:
                continue
            found.append(proposal)
    found.sort(key=lambda p: (p.pool.application.name.lower(), p.pool.sku_name.lower(), p.observation.connector.name))
    return found


def unbound(org) -> list[Observation]:
    """Licence observations whose SKU no pool of the organization has (as its SKU ID): shown beside the queue so
    someone can add the pool (unbound SKUs appear as proposals)."""
    observations = list(license_observations(org))
    if not observations:
        return []
    known = {
        s.strip().lower()
        for s in LicensePool.objects.filter(organization=org).exclude(sku_id="").values_list("sku_id", flat=True)
    }
    return sorted((o for o in observations if observed_sku(o) not in known), key=lambda o: (o.display_name, str(o.id)))


def _proposal(org, pool: LicensePool, observation: Observation) -> Proposal:
    """The live proposal for this pair, or 409 when there is none to act on."""
    if observation.kind != ObservationKind.LICENSE or observed_sku(observation) != pool.sku_id.strip().lower():
        raise Conflict("This source doesn't report this pool's SKU.")
    if observation.gone_at is not None or not observation.connector.enabled:
        raise Conflict("The source no longer reports this SKU, so there is nothing to apply.")
    observed = observed_seats(observation)
    diff = differences(pool, observed)
    if not diff:
        raise Conflict("The pool already has the seat counts the source reports.")
    review = SeatCountReview.objects.filter(pool=pool, observation=observation).first()
    return Proposal(pool, observation, observed, diff, review)


def _record(org, actor, proposal: Proposal, decision: str, reason: str = "") -> None:
    SeatCountReview.objects.update_or_create(
        pool=proposal.pool,
        observation=proposal.observation,
        defaults={
            "organization": org,
            "decision": decision,
            "fingerprint": fingerprint(proposal.observed),
            "reason": reason,
            "decided_by": actor,
        },
    )


def _locked(org, pool: LicensePool, observation: Observation) -> tuple[LicensePool, Observation]:
    """The pool (locked: the pool row is what every seat write serialises on) and the observation, as stored now."""
    pool = LicensePool.objects.select_for_update().select_related("application").get(organization=org, id=pool.id)
    observation = Observation.objects.select_related("connector").get(organization=org, id=observation.id)
    return pool, observation


@transaction.atomic
def apply(org, actor, pool: LicensePool, observation: Observation, data) -> LicensePool:
    """Take the source's counts into the pool: every count that differs, or those in `counts`. An ordinary audited
    edit (`license_pool.updated` with the changed counts and the observation it came from); `last_synced` is left
    alone (only the sync path sets it). Recorded as applied."""
    pool, observation = _locked(org, pool, observation)
    proposal = _proposal(org, pool, observation)
    differing = [d["field"] for d in proposal.differences]
    raw = data.get("counts")
    if raw is None:
        fields = differing
    elif not isinstance(raw, list) or not raw or not all(f in differing for f in raw):
        raise FieldErrors({"counts": [f"Choose from the counts that differ: {', '.join(differing)}."]})
    else:
        fields = list(dict.fromkeys(raw))
    pool = contracts.update_pool(
        org,
        actor,
        pool,
        {f: proposal.observed[f] for f in fields},
        note={"observation": str(observation.id), "source": observation.connector.name},
    )
    _record(org, actor, proposal, SeatReviewDecision.APPLIED)
    return pool


@transaction.atomic
def dismiss(org, actor, pool: LicensePool, observation: Observation, data) -> LicensePool:
    """Keep the pool's counts and say why; the proposal stays dismissed until the source's counts change."""
    pool, observation = _locked(org, pool, observation)
    proposal = _proposal(org, pool, observation)
    if proposal.dismissed:
        raise Conflict("This proposal is already dismissed.")
    raw = data.get("reason")
    reason = _text(raw) if isinstance(raw, str) else ""
    if not reason:
        raise FieldErrors({"reason": ["Say why the pool keeps its counts."]})
    if len(reason) > REASON_MAX:
        raise FieldErrors({"reason": [f"Use {REASON_MAX} characters or fewer."]})
    _record(org, actor, proposal, SeatReviewDecision.DISMISSED, reason)
    audit(
        org,
        actor,
        "license_pool.seat_count_dismissed",
        pool,
        {
            "name": pool.sku_name,
            "observation": str(observation.id),
            "source": observation.connector.name,
            "proposed": {d["field"]: d["proposed"] for d in proposal.differences},
            "kept": {d["field"]: d["current"] for d in proposal.differences},
            "reason": reason,
        },
    )
    return pool


def sync_sources(org, pools: Iterable[LicensePool]) -> dict[Any, dict[str, Any]]:
    """{pool id: {synced_from, sync_held}} for the syncing pools among `pools` (seat_sync on, a SKU ID): the connectors
    whose live licence observations (enabled connectors, `license_observations`) report the pool's SKU. `synced_from` is
    {id, name} of the one connector when exactly one reports it and it is an Entra licences connector (the only source
    whose runs sync), else None; `sync_held` is true when two or more report it (`sync_observed` then leaves the pool
    alone). Other pools get neither. One query, none without a syncing pool."""
    from discovery.models import Source

    syncing = {p.id: p.sku_id.strip().lower() for p in pools if p.seat_sync and p.sku_id.strip()}
    if not syncing:
        return {}
    reporters: dict[str, dict[Any, Any]] = {}
    for o in license_observations(org).only(
        "id", "external_id", "data", "connector__id", "connector__name", "connector__source"
    ):
        reporters.setdefault(observed_sku(o), {})[o.connector_id] = o.connector
    out: dict[Any, dict[str, Any]] = {}
    for pid, sku in syncing.items():
        found = list(reporters.get(sku, {}).values())
        one = found[0] if len(found) == 1 and found[0].source == Source.ENTRA_LICENSES else None
        out[pid] = {
            "synced_from": {"id": str(one.id), "name": one.name} if one is not None else None,
            "sync_held": len(found) >= 2,
        }
    return out


@transaction.atomic
def sync_seats(org, observation: Observation, now: datetime | None = None) -> LicensePool | None:
    """The seat sync path (the one exception to discovery never overwriting curated data; called by the discovery run, `sync_observed`, for each licence
    observation it records): the organization's syncing pool for the observation's SKU takes the observed counts and
    `last_synced`. Audited as `license_pool.synced` (no actor: the source) when a count changed. None when no pool syncs
    the SKU (its proposals stay in the review queue for pools that don't sync)."""
    if observation.kind != ObservationKind.LICENSE or observation.organization_id != org.id:
        raise ValueError("sync_seats takes one of the organization's license observations.")
    sku = observed_sku(observation)
    pool = LicensePool.objects.select_for_update().filter(organization=org, seat_sync=True, sku_id__iexact=sku).first()
    if pool is None:
        return None
    observed = observed_seats(observation)
    changes = {f: [getattr(pool, f), observed[f]] for f in SEAT_FIELDS if observed[f] not in (None, getattr(pool, f))}
    for field, (_before, after) in changes.items():
        setattr(pool, field, after)
    pool.last_synced = now or timezone.now()
    pool.save()
    if changes:
        audit(
            org,
            None,
            "license_pool.synced",
            pool,
            {
                "name": pool.sku_name,
                **changes,
                "observation": str(observation.id),
                "source": observation.connector.name,
            },
        )
    return pool


def sync_observed(connector, observations: Iterable[Observation], now: datetime | None = None) -> dict[str, Any]:
    """After an Entra licences run, in its transaction: each live licence observation it saw syncs its SKU's syncing
    pool (`sync_seats`), unless another enabled connector reports the same SKU too (two tenants: which one the pool
    follows is a person's call, so it is held and the run notes it). Returns the run's counts: `pools_synced` (pools
    whose counts changed), `pools_held`, `pools_to_review` (open proposals on this connector's observations) and
    `unbound` (SKUs no pool has), plus `sync_notes` for the run's notes."""
    org = connector.organization
    seen = [o for o in observations if o.kind == ObservationKind.LICENSE and o.gone_at is None]
    elsewhere: dict[str, set[Any]] = {}
    for o in license_observations(org).exclude(connector=connector).only("id", "external_id", "data", "connector_id"):
        elsewhere.setdefault(observed_sku(o), set()).add(o.connector_id)
    syncing = {
        s.strip().lower()
        for s in LicensePool.objects.filter(organization=org, seat_sync=True).values_list("sku_id", flat=True)
    }
    synced = held = 0
    notes: list[str] = []
    for o in seen:
        sku = observed_sku(o)
        if sku not in syncing:
            continue
        if elsewhere.get(sku):
            held += 1
            notes.append(
                f"{o.display_name}: another connector reports this SKU too, so its syncing pool kept its counts; "
                "review them as proposals."
            )
            continue
        before = LicensePool.objects.filter(organization=org, seat_sync=True, sku_id__iexact=sku).values_list(
            *SEAT_FIELDS
        )
        counts = before.first()
        pool = sync_seats(org, o, now)
        if pool is not None and counts != tuple(getattr(pool, f) for f in SEAT_FIELDS):
            synced += 1
    ids = {o.id for o in seen}
    review = sum(1 for p in proposals(org) if p.observation.id in ids)
    unbound_count = sum(1 for o in unbound(org) if o.id in ids)
    return {
        "pools_synced": synced,
        "pools_held": held,
        "pools_to_review": review,
        "unbound": unbound_count,
        "sync_notes": notes[:5],
    }


# ---- unbound SKUs: create a pool, or bind one -------------------------------------------------------------------------


def _unbound_observation(org, observation: Observation) -> Observation:
    """The licence observation as stored now, live and still unbound, or 409."""
    o = Observation.objects.select_related("connector").get(organization=org, id=observation.id)
    if o.kind != ObservationKind.LICENSE:
        raise Conflict("This observation isn't a license SKU.")
    if o.gone_at is not None or not o.connector.enabled:
        raise Conflict("The source no longer reports this SKU.")
    sku = observed_sku(o)
    bound = LicensePool.objects.filter(organization=org, sku_id__iexact=sku).first()
    if bound is not None:
        raise Conflict(f"The pool {bound.sku_name} already has this SKU. Review its seat counts instead.")
    return o


def _source_note(o: Observation) -> dict[str, Any]:
    return {"observation": str(o.id), "source": o.connector.name, "sku_part_number": o.display_name}


@transaction.atomic
def create_from_observation(org, actor, observation: Observation, data) -> LicensePool:
    """A pool for an unbound SKU: the SKU ID and name and the observed seat counts filled in from the source, the rest
    (application, contract, seat_sync, notes, custom fields) from `data`; validated as any pool is
    (contracts.create_pool, audited as `license_pool.created` naming the source)."""
    o = _unbound_observation(org, observation)
    observed = observed_seats(o)
    values: dict[str, Any] = {
        "sku_name": str(o.data.get("sku_part_number") or o.display_name)[: contracts.NAME_MAX],
        "sku_id": observed_sku(o),
        **{f: v for f, v in observed.items() if v is not None},
    }
    allowed = {"application", "contract", "sku_name", "seat_sync", "notes", "custom_field_data"}
    unknown = set(data) - allowed
    if unknown:
        raise FieldErrors({k: ["This can't be set here; the source gives it."] for k in sorted(unknown)})
    values |= {k: v for k, v in data.items() if k in allowed}
    return contracts.create_pool(org, actor, values, note=_source_note(o))


@transaction.atomic
def bind(org, actor, pool: LicensePool, observation: Observation, data) -> LicensePool:
    """Bind an existing pool without a SKU ID to an unbound SKU (an audited `license_pool.updated` setting `sku_id`,
    naming the source), optionally turning `seat_sync` on. The pool's counts are kept; any difference becomes a
    proposal, or is synced by the next run when `seat_sync` is on. A pool already bound to another SKU is refused:
    change its SKU ID on the pool itself."""
    pool = LicensePool.objects.select_for_update().get(organization=org, id=pool.id)
    o = _unbound_observation(org, observation)
    if pool.sku_id.strip():
        raise Conflict(f"This pool is already bound to SKU {pool.sku_id}. Change its SKU ID on the pool instead.")
    values: dict[str, Any] = {"sku_id": observed_sku(o)}
    if "seat_sync" in data:
        values["seat_sync"] = data["seat_sync"]
    unknown = set(data) - {"seat_sync", "observation"}
    if unknown:
        raise FieldErrors(
            {k: ["Binding sets the SKU ID and, if you choose, seat sync; nothing else."] for k in unknown}
        )
    return contracts.update_pool(org, actor, pool, values, note=_source_note(o))


# ---- Needs attention -------------------------------------------------------------------------------------------------


def attention_rows(org) -> list[dict[str, Any]]:
    """One Needs attention row per licence pool with open proposals (`seat_counts`), routed to its application's IT
    steward; pools of archived or retired applications are left out. Empty without the `contracts` capability.
    One query when nothing is observed (the badge reads this on every page)."""
    from core.capabilities import CONTRACTS, has_capability

    if not has_capability(org, CONTRACTS):
        return []
    grouped: dict[Any, list[Proposal]] = {}
    for p in proposals(org):
        app = p.pool.application
        if app.archived_at is None and app.lifecycle != "retired":
            grouped.setdefault(p.pool.id, []).append(p)
    rows: list[dict[str, Any]] = []
    for found in grouped.values():
        pool = found[0].pool
        if len(found) == 1:
            said = ", ".join(f"{d['proposed']} {d['field'].removeprefix('seats_')}" for d in found[0].differences)
            why = f"{found[0].observation.connector.name} reports {said}; the pool doesn't match."
        else:
            why = f"{len(found)} sources report seat counts the pool doesn't match."
        rows.append(
            {
                "id": str(pool.id),
                "kind": "seat_count",
                "name": pool.sku_name,
                "application": {"id": str(pool.application_id), "name": pool.application.name},
                "proposals": len(found),
                "why": why,
                "steward_id": pool.application.it_steward_id,
            }
        )
    rows.sort(key=lambda r: (r["application"]["name"].lower(), r["name"].lower(), r["id"]))
    return rows
