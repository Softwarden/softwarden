"""The HaloITSM source (core/halo.py; capability `discovery.halo`): Halo assets as application candidates, through the connector's
HaloConnection.

Not Microsoft, so nothing here is on Microsoft Learn: the API's conventions are from Halo's integration guides
(confirm against the instance's own /apidoc during live acceptance). Client credentials at `{base_url}/auth/token`
(core/halo.py), then `GET {base_url}/api/Asset?paginate=true&page_size=100&page_no=N` with the bearer token, page by
page until a short page or `record_count` is reached. Every call goes through core/outbound's service rule (private
networks allowed for an on-premises Halo; loopback, link-local and metadata addresses refused on every resolved
address, no redirects); the token goes only to the connection's own host and port.

Each asset is an `application` observation keyed by its Halo ID (`data.halo_asset_id`), named by its key field (else
its inventory number), with its asset type, client, site and whether it is inactive. Inactive assets are skipped by
default; `asset_types` limits the run to the types listed. A record linked to or created from one keeps
the Halo asset ID through its discovery sources (`GET applications/{id}/discovery-sources`, `external_id`); a column on the record waits
for the Halo links feature."""

from __future__ import annotations

from typing import Any

from core import halo as halo_api

from . import access, scopes
from .graph_source import Fetched, Permission, Probe, Session, SourceError, halo_audience, transports
from .models import ObservationKind

PAGE_SIZE = 100
PAGE_LIMIT = 500  # 50,000 assets
# Nothing listed while observations are live fails the run (the empty-listing guard, discovery/engine._apply);
# so does one listing of several that comes back empty (Fetched.empty, engine._guard_empty_listings).
EMPTY_LISTING_NAME = "Halo's asset listing"
TYPES_MAX = 50

READ_ASSETS = Permission(
    "read:assets",
    "",
    True,
    "List Halo assets: the Halo API application's read access to assets, requested with the connection's scope "
    "(default read:assets). Read only.",
    False,  # not Microsoft: there is no Learn page; Halo's /apidoc is the reference
    kind="halo_scope",
    advice="In Halo (Configuration > Integrations > Halo API), give the API application read access to assets, keep "
    "read:assets in the connection's scope, test the connection, then check again.",
)

KINDS = [ObservationKind.APPLICATION]
CONNECTION = "halo"
CONFIG_FIELDS: tuple[str, ...] = ()
SCOPE_DEFAULTS: dict[str, Any] = {"asset_types": [], "exclude_inactive": True, "name_exclusions": []}
FILTERS = {
    "no_id": "no ID in Halo",
    "asset_types": "asset type not included",
    "inactive": "inactive in Halo",
    "name_exclusions": "name excluded",
}
PERMISSIONS = [READ_ASSETS]
PROBES: list[Probe] = []  # built from the connection (access_steps)
DETAILS = ("halo_asset_id", "asset_type", "inventory_number", "client", "site", "inactive")


def simulator(connector):
    from . import halo_simulator

    return halo_simulator.Transport(connector)


def scope_of(connector) -> dict[str, Any]:
    return dict(SCOPE_DEFAULTS) | dict(connector.scope or {})


def validate(config, scope, errors):
    if "exclude_inactive" in scope and not isinstance(scope["exclude_inactive"], bool):
        errors["scope.exclude_inactive"] = ["Choose yes or no."]
    types = scope.get("asset_types", [])
    if not isinstance(types, list) or not all(isinstance(t, str) and 0 < len(t.strip()) <= 100 for t in types):
        errors["scope.asset_types"] = ["Enter one Halo asset type per line, 100 characters at most."]
    elif len(types) > TYPES_MAX:
        errors["scope.asset_types"] = [f"Use {TYPES_MAX} asset types or fewer."]
    else:
        scope["asset_types"] = sorted({t.strip() for t in types}, key=str.lower)
    scopes.validate_names(scope, errors)


def _assets_url(base_url: str, page: int, size: int = PAGE_SIZE) -> str:
    return f"{base_url.rstrip('/')}{halo_api.ASSETS_PATH}?paginate=true&page_size={size}&page_no={page}"


def fetch(connector, credential, transport=None):
    """Every asset, page by page: the Halo asset objects as Halo returns them."""
    session = Session(transports(connector, credential, simulator, transport), credential)
    client = session.client(halo_audience(credential.base_url))
    out: list[dict[str, Any]] = []
    first: Any = None
    for page in range(1, PAGE_LIMIT + 1):
        body = client.get(_assets_url(credential.base_url, page), "list assets", READ_ASSETS)
        batch = [a for a in body.get("assets") or [] if isinstance(a, dict)]
        if batch and page > 1 and first is not None and batch[0].get("id") == first:
            # The instance ignored page_no and answered the same page again: stop rather than read it 500 times. A page
            # whose first asset has no ID proves nothing (two pages may both start with one), so it is never compared.
            # What was read is only part of the assets, so the whole source counts as unread: nothing becomes "no
            # longer seen" this run (engine.ALL_UNREAD).
            return Fetched(
                out,
                [
                    f"Halo answered page {page} with the same assets as page {page - 1}; paging stopped, and nothing "
                    "is marked no longer seen this run."
                ],
                unread=["*"],
            )
        first = batch[0].get("id") if batch else None
        out.extend(batch)
        total = body.get("record_count")
        if len(batch) < PAGE_SIZE or (isinstance(total, int) and len(out) >= total):
            return Fetched(out)
    raise SourceError("Halo has more assets than a run reads; limit the run to some asset types.")


def map_object(raw):
    asset_id = str(raw.get("id") or "")[:50]
    name = str(raw.get("key_field") or raw.get("inventory_number") or f"Halo asset {asset_id}")
    return {
        "kind": ObservationKind.APPLICATION,
        "external_id": asset_id,
        "display_name": name[:400],
        "ai_agent": False,
        "data": {
            "halo_asset_id": asset_id,
            "asset_type": str(raw.get("assettype_name") or "")[:100],
            "inventory_number": str(raw.get("inventory_number") or "")[:100],
            "client": str(raw.get("client_name") or "")[:200],
            "site": str(raw.get("site_name") or "")[:200],
            "inactive": bool(raw.get("inactive")),
            "description": "",
            "enterprise_app": False,
        },
    }


def skip_reason(values, scope):
    scope = dict(SCOPE_DEFAULTS) | dict(scope or {})
    data = values["data"]
    if not data["halo_asset_id"]:
        return "no_id"
    if scope["exclude_inactive"] and data["inactive"]:
        return "inactive"
    wanted = {t.lower() for t in scope.get("asset_types") or [] if isinstance(t, str)}
    if wanted and data["asset_type"].lower() not in wanted:
        return "asset_types"
    return "name_exclusions" if scopes.excluded_by_name(values["display_name"], scope) else None


def relevant(values):
    """What brings a dismissed candidate back: its name, its type, or its becoming active again."""
    data = values["data"]
    return {"name": values["display_name"], "asset_type": data["asset_type"], "inactive": data["inactive"]}


def drift_values(observation, kind) -> dict[str, Any]:
    """Halo speaks only for the name on a linked record (discovery/drift.py): an asset has no description, and its ID
    stays in observation data until the Halo links feature."""
    return {"name": observation.display_name}


def access_steps(connector, scope, session: Session) -> list[dict[str, Any]]:
    """Sign in to Halo, then read one asset."""
    base_url = session.credential.base_url
    client = session.client(halo_audience(base_url))
    steps = [access.sign_in(client)]
    if steps[-1]["state"] != "ok":
        return steps + [access.skipped("Read assets", READ_ASSETS)]
    return steps + [access.probe(client, Probe("Read assets", _assets_url(base_url, 1, 1), READ_ASSETS))[0]]
