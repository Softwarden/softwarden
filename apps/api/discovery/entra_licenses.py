"""The Microsoft Entra licences source: one `license` observation
per subscribed SKU with its seat counts. A licence observation never becomes a record: it feeds the licence pools'
seat review (money/licences.py), where a pool bound to the SKU (its `sku_id`) gets a proposed seat count and, with
`seat_sync` on, is synced by every run (`after_apply`); a SKU no pool has is listed as unbound, to create a pool for or
bind a pool to (money/licences.py).

Microsoft Learn, checked 2026-11-06 (docs/PERMISSIONS.md):
`GET /v1.0/subscribedSkus`, which supports `$select` only (no `$filter`); application permission
LicenseAssignment.Read.All (least privileged). Fields: skuId, skuPartNumber, appliesTo, capabilityStatus,
consumedUnits, prepaidUnits {enabled, suspended, warning, lockedOut}.

Seats: purchased is `prepaidUnits.enabled` and assigned is `consumedUnits`;
units in warning, suspended and locked out are kept beside them and never counted as purchased."""

from typing import Any

from .graph_source import GRAPH, LICENSE_ASSIGNMENT_READ_ALL, Client, Fetched, Probe, graph_transport
from .models import ObservationKind

KINDS = [ObservationKind.LICENSE]
CONNECTION = "microsoft"
CONFIG_FIELDS: tuple[str, ...] = ()
SCOPE_DEFAULTS: dict[str, Any] = {"sku_exclusions": []}
FILTERS = {"sku_exclusions": "SKU excluded"}
PERMISSIONS = [LICENSE_ASSIGNMENT_READ_ALL]
SELECT = "skuId,skuPartNumber,appliesTo,capabilityStatus,consumedUnits,prepaidUnits"
PROBES = [Probe("Read subscribed SKUs", f"{GRAPH}/subscribedSkus?$select=skuId", LICENSE_ASSIGNMENT_READ_ALL)]
PAGE_LIMIT = 20  # Learn shows no paging for this list; follow a next link if one ever comes, within reason
# Nothing listed while observations are live fails the run (the empty-listing guard, discovery/engine._apply);
# so does one listing of several that comes back empty (Fetched.empty, engine._guard_empty_listings).
EMPTY_LISTING_NAME = "Microsoft Graph's subscribed SKU listing"


def validate(config, scope, errors):
    names = scope.get("sku_exclusions", [])
    if not isinstance(names, list) or not all(isinstance(n, str) and 0 < len(n.strip()) <= 100 for n in names):
        errors["scope.sku_exclusions"] = ["Enter one SKU part number or prefix per line, 100 characters at most."]
    elif len(names) > 100:
        errors["scope.sku_exclusions"] = ["Use 100 exclusions or fewer."]
    else:
        scope["sku_exclusions"] = [n.strip() for n in names]


def fetch(connector, credential, transport=None):
    client = Client(graph_transport(connector, credential, transport), credential)
    return Fetched(
        client.get_all(
            f"{GRAPH}/subscribedSkus?$select={SELECT}", "read subscribed SKUs", LICENSE_ASSIGNMENT_READ_ALL, PAGE_LIMIT
        )
    )


def _units(value) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def map_object(raw):
    prepaid = raw.get("prepaidUnits") if isinstance(raw.get("prepaidUnits"), dict) else {}
    sku_id = str(raw.get("skuId") or "").lower()
    part = str(raw.get("skuPartNumber") or sku_id)
    return {
        "kind": ObservationKind.LICENSE,
        "external_id": sku_id[:200],
        "display_name": part[:400],
        "ai_agent": False,
        "data": {
            "sku_id": sku_id,
            "sku_part_number": part,
            "seats_purchased": _units(prepaid.get("enabled")),
            "seats_assigned": _units(raw.get("consumedUnits")),
            "seats_warning": _units(prepaid.get("warning")),
            "seats_suspended": _units(prepaid.get("suspended")),
            "seats_locked_out": _units(prepaid.get("lockedOut")),
            "capability_status": str(raw.get("capabilityStatus") or ""),
            "applies_to": str(raw.get("appliesTo") or ""),
        },
    }


def skip_reason(values, scope):
    prefixes = [n.lower() for n in (scope or {}).get("sku_exclusions") or [] if isinstance(n, str)]
    if any(values["display_name"].lower().startswith(p) for p in prefixes if p):
        return "sku_exclusions"
    return None


def relevant(values):
    data = values["data"]
    return {k: data[k] for k in ("seats_purchased", "seats_assigned", "seats_warning", "seats_suspended")}


def after_apply(connector, observations, now) -> dict[str, Any]:
    """In the run's transaction, once the observations are written: sync the pools that sync their SKU, and count what
    is left to review (money/licences.sync_observed). Pools without a SKU ID never change."""
    from money import licences

    return licences.sync_observed(connector, observations, now)
