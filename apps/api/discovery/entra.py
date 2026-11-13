"""The Microsoft Entra ID source:
read-only enterprise applications (service principals) and app registrations (applications), merged by application
(client) ID into one observation each, with their owners and the expiry dates of their client secrets and
certificates.

Microsoft Learn, checked 2026-11-06 (docs/PERMISSIONS.md):

- `GET /v1.0/servicePrincipals` (page size 100) and `GET /v1.0/applications` (100 here; 999 is the maximum), following
  `@odata.nextLink`. Application permission Application.Read.All (least privileged; admin consent).
- `keyCredentials` and `passwordCredentials` come with the default properties. Asking for keyCredentials with `$select`
  is throttled to 150 requests a minute per tenant, so the lists use no `$select`; key material is
  never returned on a list, and the secret's `hint` (its first characters) is never stored.
- Owners are expanded on each list. With Application.Read.All alone a user owner comes back with its id and type only;
  the optional User.ReadBasic.All fills its name and email. Owners are matched to People by Entra object ID (the
  Person's user's Entra identity) and by email, and an owner without a name is the named gap "owner not resolved".
- Microsoft first-party apps are those whose `appOwnerOrganizationId` is in the scope's `first_party_tenants`, by
  default the Microsoft Services tenant (verified on Learn) and Microsoft's corporate tenant (kept from an earlier version; not on
  Learn). They are skipped and counted under "published by Microsoft".
- AI agents: a service principal tagged `AgentCreatedBy:CopilotStudio`,
  `AgenticApp`, `AIAgentBuilder`, `AgenticInstance` or `power-virtual-agents-*` (Copilot Studio), or an Entra Agent ID
  agent identity (`servicePrincipalType` ServiceIdentity, `#microsoft.graph.agentIdentity`), becomes an `ai_agent`
  observation. Agent identities are listed through `GET /v1.0/servicePrincipals/microsoft.graph.agentIdentity` only
  when the scope's `include_agent_identities` is on; it needs the optional AgentIdentity.Read.All, and without it the
  run notes that agent identities weren't read rather than failing, and the agent identities seen before are "not read"
  (`unread_scope`), never "no longer seen", so linked AI agents don't go missing. An agent identity is keyed by its
  object ID.
- A full listing every run (no delta query): an object not listed is "no longer seen".

The tenant ID `simulated` selects the synthetic tenant in discovery/entra_simulator.py, which tests and the demo use."""

import json
import urllib.parse
from datetime import date
from typing import Any

from core import graph, simulation

from . import scopes
from .graph_source import (
    AGENT_IDENTITY_READ_ALL,
    APPLICATION_READ_ALL,
    GRAPH,
    SIMULATED,
    USER_READBASIC_ALL,
    Client,
    Fetched,
    Probe,
    Refused,
    SourceError,
    graph_transport,
)
from .models import CredentialOrigin, CredentialType, ObservationKind

__all__ = ["SIMULATED", "SourceError"]

LOGIN_HOST = graph.LOGIN_HOST
GRAPH_HOST = graph.GRAPH_HOST
MICROSOFT_SERVICES_TENANT = "f8cdef31-a31e-4b4a-93e4-5f571e91255a"  # verified on Microsoft Learn
MICROSOFT_CORP_TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"  # from an earlier version; not found on Microsoft Learn
FIRST_PARTY_TENANTS = [MICROSOFT_SERVICES_TENANT, MICROSOFT_CORP_TENANT]
MICROSOFT_TENANTS = set(FIRST_PARTY_TENANTS)
AGENT_TAGS = {"agentcreatedby:copilotstudio", "agenticapp", "aiagentbuilder", "agenticinstance"}
AGENT_TAG_PREFIXES = ("power-virtual-agents-",)
AGENT_IDENTITY_TYPE = "ServiceIdentity"
AGENT_IDENTITY_ODATA = "#microsoft.graph.agentIdentity"
# Agent identity blueprints (Learn, 2026-11-06): #microsoft.graph.agentIdentityBlueprint in /applications and
# #microsoft.graph.agentIdentityBlueprintPrincipal in /servicePrincipals. A blueprint is the template agent identities
# are made from, not an agent: it stays an application observation, flagged `agent_blueprint`, and the run notes how
# many it saw. LIVE ACCEPTANCE: confirm on the dev tenant how blueprints and their principals list (type, tags) and
# whether they should become their own candidates.
BLUEPRINT_ODATA = ("#microsoft.graph.agentIdentityBlueprint", "#microsoft.graph.agentIdentityBlueprintPrincipal")
KEPT_TYPES = ("Application", AGENT_IDENTITY_TYPE)
GUID = graph.GUID
DOMAIN = graph.TENANT_DOMAIN
OWNER_FIELDS = "id,displayName,mail,userPrincipalName"
OWNERS = f"$expand=owners($select={OWNER_FIELDS})"
# $expand returns at most 20 related objects and no next link (Graph's documented $expand limit): an object with 20
# expanded owners may have more, so its owners are read from /{applications|servicePrincipals}/{id}/owners, paged.
EXPAND_LIMIT = 20
PAGE_LIMIT = 500  # 100 per page: 50,000 objects is far beyond a typical tenant
# Nothing listed while observations are live fails the run (the empty-listing guard, discovery/engine._apply);
# so does one listing of several that comes back empty (Fetched.empty, engine._guard_empty_listings).
EMPTY_LISTING_NAME = "Entra's application listing"
FIRST_PARTY_MAX = 20
DESCRIPTION_MAX = 2000


def valid_tenant(value):
    """A GUID or domain, or the synthetic tenant `simulated` where core/simulation.allowed()."""
    return (value == SIMULATED and simulation.allowed()) or graph.valid_tenant(value)


# ---- the connector's contract with the engine ------------------------------------------------------

KINDS = [ObservationKind.APPLICATION, ObservationKind.AI_AGENT]
# The connection kind a connector of this source signs in through (core/connections.py). The tenant, client
# and secret are the connection's; the source has no settings of its own beyond its scope.
CONNECTION = "microsoft"
CONFIG_FIELDS: tuple[str, ...] = ()
SCOPE_DEFAULTS: dict[str, Any] = {
    "exclude_microsoft": True,
    "exclude_disabled": True,
    "name_exclusions": [],
    "first_party_tenants": FIRST_PARTY_TENANTS,
    "include_agent_identities": False,
}
# Labels read inside a sentence ("4 published by Microsoft"), so they start lowercase.
FILTERS = {
    "not_application": "not an application (managed identity or legacy)",
    "agent_identities": "agent identity (not included)",
    "microsoft": "published by Microsoft",
    "disabled": "disabled in Entra",
    "name_exclusions": "name excluded",
}
PERMISSIONS = [APPLICATION_READ_ALL, USER_READBASIC_ALL, AGENT_IDENTITY_READ_ALL]
# One-item reads the access check makes (discovery/access.py), each proving one permission.
PROBES = [
    Probe("Read app registrations", f"{GRAPH}/applications?$top=1&$select=id", APPLICATION_READ_ALL),
    Probe("Read enterprise applications", f"{GRAPH}/servicePrincipals?$top=1&$select=id", APPLICATION_READ_ALL),
    Probe("Read owners' names", f"{GRAPH}/users?$top=1&$select=id", USER_READBASIC_ALL),
    Probe(
        "Read agent identities",
        f"{GRAPH}/servicePrincipals/microsoft.graph.agentIdentity?$top=1&$select=id",
        AGENT_IDENTITY_READ_ALL,
        when="include_agent_identities",
    ),
]


# The two listings, as Fetched.empty names them (the empty-listing guard, engine._guard_empty_listings).
SERVICE_PRINCIPALS = "listing:servicePrincipals"
APPLICATIONS = "listing:applications"


def listings_of(observation) -> set[str]:
    """The listings an observation came from: the enterprise app list (agent identities aside: they have their own
    listing) and the app registration list."""
    data = observation.data if isinstance(observation.data, dict) else {}
    found = set()
    if data.get("enterprise_app") and data.get("agent") != "agent_identity":
        found.add(SERVICE_PRINCIPALS)
    if data.get("registration"):
        found.add(APPLICATIONS)
    return found


def credential_listing(credential) -> str | None:
    """The listing a discovered credential came from."""
    return {CredentialOrigin.APP_REGISTRATION: APPLICATIONS, CredentialOrigin.ENTERPRISE_APP: SERVICE_PRINCIPALS}.get(
        credential.origin
    )


def scope_of(connector) -> dict[str, Any]:
    return dict(SCOPE_DEFAULTS) | dict(connector.scope or {})


def validate(config, scope, errors):
    for key in ("exclude_microsoft", "exclude_disabled", "include_agent_identities"):
        if key in scope and not isinstance(scope[key], bool):
            errors[f"scope.{key}"] = ["Choose yes or no."]
    scopes.validate_names(scope, errors)
    tenants = scope.get("first_party_tenants", FIRST_PARTY_TENANTS)
    if not isinstance(tenants, list) or not all(isinstance(t, str) and GUID.match(t.strip()) for t in tenants):
        errors["scope.first_party_tenants"] = ["Enter one tenant ID (a GUID) per line."]
    elif len(tenants) > FIRST_PARTY_MAX:
        errors["scope.first_party_tenants"] = [f"Use {FIRST_PARTY_MAX} tenants or fewer."]
    else:
        scope["first_party_tenants"] = sorted({t.strip().lower() for t in tenants})


def fetch(connector, credential, transport=None):
    """Every enterprise application and app registration in the tenant (plus agent identities when the scope asks),
    merged by application ID: [{"key", "application"?, "servicePrincipal"?}]. `credential` is the connector's tested
    connection's (core/connections.MicrosoftCredential)."""
    client = Client(graph_transport(connector, credential, transport), credential)
    sps = client.get_all(
        f"{GRAPH}/servicePrincipals?{OWNERS}&$top=100", "list enterprise applications", APPLICATION_READ_ALL, PAGE_LIMIT
    )
    apps = client.get_all(
        f"{GRAPH}/applications?{OWNERS}&$top=100", "list app registrations", APPLICATION_READ_ALL, PAGE_LIMIT
    )
    notes = []
    unread: set[str] = set()
    agents: list[dict[str, Any]] = []
    if scope_of(connector).get("include_agent_identities"):
        try:
            agents = client.get_all(
                f"{GRAPH}/servicePrincipals/microsoft.graph.agentIdentity?$top=100",
                "list agent identities",
                AGENT_IDENTITY_READ_ALL,
                PAGE_LIMIT,
            )
        except Refused as e:
            if e.status not in (401, 403):
                raise
            notes.append(
                "Agent identities weren't read: the app doesn't have the optional permission AgentIdentity.Read.All."
            )
            # Not read is not gone: agent identities seen before keep their state (and linked AI agents never become
            # "no longer seen") until a run can read them again (unread_scope).
            unread.add(AGENT_IDENTITIES_SCOPE)
    for collection, objects in (("applications", apps), ("servicePrincipals", sps)):
        _complete_owners(client, collection, objects)
    merged: dict[str, dict[str, Any]] = {}
    for app in apps:
        key = (app.get("appId") or "").lower() or f"application:{app.get('id', '')}"
        merged.setdefault(key, {})["application"] = app
    for sp in [*sps, *agents]:
        key = (
            f"agent:{sp.get('id', '')}"
            if is_agent_identity(sp)
            else (sp.get("appId") or "").lower() or f"sp:{sp.get('id', '')}"
        )
        merged.setdefault(key, {})["servicePrincipal"] = sp  # an agent identity listed twice is the same object
    empty = {}
    if not sps:
        empty[SERVICE_PRINCIPALS] = "Entra's enterprise application listing"
    if not apps:
        empty[APPLICATIONS] = "Entra's app registration listing"
    found = Fetched([{"key": key, **parts} for key, parts in merged.items() if parts], notes, unread, empty)
    found.notes.extend(owner_notes(found))
    blueprints = sum(1 for o in found if is_blueprint(o.get("servicePrincipal")) or is_blueprint(o.get("application")))
    if blueprints:
        found.notes.append(
            f"{blueprints} agent identity blueprint(s) are listed as applications, not as AI agents (the agent "
            "identities made from them are)."
        )
    return found


def _complete_owners(client, collection, objects) -> None:
    """Replace a capped expansion (EXPAND_LIMIT owners) with the full, paged owners list of that object only."""
    for obj in objects:
        owners = obj.get("owners")
        if isinstance(owners, list) and len(owners) >= EXPAND_LIMIT and obj.get("id"):
            oid = urllib.parse.quote(str(obj["id"]), safe="")
            obj["owners"] = client.get_all(
                f"{GRAPH}/{collection}/{oid}/owners?$select={OWNER_FIELDS}&$top=100",
                "list an application's owners",
                APPLICATION_READ_ALL,
                PAGE_LIMIT,
            )


def owner_notes(objects) -> list[str]:
    unresolved = sum(
        1
        for o in objects
        for part in (o.get("application"), o.get("servicePrincipal"))
        if part
        for owner in part.get("owners") or []
        if isinstance(owner, dict) and not (owner.get("displayName") or owner.get("mail"))
    )
    if not unresolved:
        return []
    return [
        f"{unresolved} owner(s) came back without a name (owner not resolved): grant the optional User.ReadBasic.All "
        "to see names, or match them by Entra object ID."
    ]


def is_agent_identity(sp) -> bool:
    return bool(sp) and (
        sp.get("servicePrincipalType") == AGENT_IDENTITY_TYPE or sp.get("@odata.type") == AGENT_IDENTITY_ODATA
    )


def is_blueprint(obj) -> bool:
    return bool(obj) and obj.get("@odata.type") in BLUEPRINT_ODATA


def agent_kind(sp, tags) -> str:
    """copilot_studio, agent_identity or empty (a blueprint is never an agent itself)."""
    if is_blueprint(sp):
        return ""
    if is_agent_identity(sp):
        return "agent_identity"
    lowered = [t.lower() for t in tags]
    if any(t in AGENT_TAGS or t.startswith(AGENT_TAG_PREFIXES) for t in lowered):
        return "copilot_studio"
    return ""


def _owners(*parts):
    """Owners of the registration and the service principal, once each by object ID. A user owner without name or
    email came back unresolved (no User.ReadBasic.All)."""
    seen: dict[str, dict[str, Any]] = {}
    for part in parts:
        for o in (part or {}).get("owners") or []:
            if not isinstance(o, dict) or not o.get("id"):
                continue
            email = (o.get("mail") or o.get("userPrincipalName") or "").strip()
            odata = str(o.get("@odata.type") or "")
            kind = "service_principal" if odata.endswith("servicePrincipal") else "user"
            name = o.get("displayName") or email
            seen.setdefault(
                str(o["id"]).lower(),
                {
                    "external_id": str(o["id"]).lower(),
                    "name": name,
                    "email": email,
                    "type": kind,
                    "resolved": bool(name),
                },
            )
    return sorted(seen.values(), key=lambda o: (o["email"].lower(), o["name"], o["external_id"]))


def _date(value) -> str | None:
    """The UTC date of a Graph endDateTime (ISO 8601), as YYYY-MM-DD, or None."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _credentials(obj, origin) -> list[dict[str, Any]]:
    """Certificates first, then client secrets: a certificate-backed credential appears in both lists with
    the same keyId, and the certificate is what it is. Never key material and never the secret's hint."""
    out = []
    for field, ctype in (
        ("keyCredentials", CredentialType.CERTIFICATE),
        ("passwordCredentials", CredentialType.SECRET),
    ):
        for c in (obj or {}).get(field) or []:
            if not isinstance(c, dict) or not c.get("keyId"):
                continue
            out.append(
                {
                    "key_id": str(c["keyId"]).lower()[:100],
                    "credential_type": ctype.value,
                    "display_name": str(c.get("displayName") or "")[:300],
                    "origin": origin.value,
                    "object_id": str((obj or {}).get("id") or "")[:64],
                    "expires_on": _date(c.get("endDateTime")),
                }
            )
    return out


def credentials_of(raw) -> list[dict[str, Any]]:
    """One credential per keyId: the registration's before the service principal's, certificates before secrets."""
    found: dict[str, dict[str, Any]] = {}
    for c in [
        *_credentials(raw.get("application"), CredentialOrigin.APP_REGISTRATION),
        *_credentials(raw.get("servicePrincipal"), CredentialOrigin.ENTERPRISE_APP),
    ]:
        found.setdefault(c["key_id"], c)
    return list(found.values())


def map_object(raw):
    """Observation values for one merged application. `owners` are suggestions only; `credentials` go to
    DiscoveredCredential (discovery/credentials.py), not into the observation's data."""
    app = raw.get("application") or {}
    sp = raw.get("servicePrincipal") or {}
    primary = sp or app
    tags: list[str] = []
    for t in [*(sp.get("tags") or []), *(app.get("tags") or [])]:
        if isinstance(t, str) and t not in tags:
            tags.append(t)
    agent = agent_kind(sp, tags)
    app_id = str(primary.get("appId") or app.get("appId") or "").lower()
    external_id = str(sp.get("id")) if agent == "agent_identity" else (app_id or str(primary.get("id", "")))
    web = app.get("web") if isinstance(app.get("web"), dict) else {}
    return {
        "kind": ObservationKind.AI_AGENT if agent else ObservationKind.APPLICATION,
        "external_id": external_id.lower()[:200],
        "display_name": (primary.get("displayName") or app.get("displayName") or app_id or external_id)[:400],
        "ai_agent": bool(agent),
        "credentials": credentials_of(raw),
        "data": {
            "app_id": app_id,
            "service_principal_id": str(sp.get("id") or "").lower(),
            "application_id": str(app.get("id") or "").lower(),
            "registration": bool(app),
            "enterprise_app": bool(sp),
            "description": (sp.get("description") or sp.get("notes") or app.get("description") or "")[:DESCRIPTION_MAX],
            # The assistant never sends a service principal's internal notes (discovery/assistant.py).
            **({"description_from": "notes"} if not sp.get("description") and sp.get("notes") else {}),
            "homepage": sp.get("homepage") or (web or {}).get("homePageUrl") or "",
            "publisher": sp.get("publisherName") or app.get("publisherDomain") or "",
            "publisher_tenant": str(sp.get("appOwnerOrganizationId") or "").lower(),
            "enabled": bool(sp.get("accountEnabled", True)) if sp else True,
            "type": sp.get("servicePrincipalType") or "Application",
            "sign_in_audience": app.get("signInAudience") or sp.get("signInAudience") or "",
            "tags": tags[:50],
            "agent": agent,
            "agent_blueprint_id": str(sp.get("agentIdentityBlueprintId") or "").lower(),
            "agent_blueprint": is_blueprint(sp) or is_blueprint(app),
            "owners": _owners(app, sp),
        },
    }


def skip_reason(values, scope):
    data = values["data"]
    scope = dict(SCOPE_DEFAULTS) | dict(scope or {})
    if data["type"] not in KEPT_TYPES:
        return "not_application"
    # The scope decides on agent identities wherever they were listed (/servicePrincipals may return them too).
    if data.get("agent") == "agent_identity" and not scope.get("include_agent_identities"):
        return "agent_identities"
    first_party = {t.lower() for t in scope.get("first_party_tenants") or []}
    if scope.get("exclude_microsoft", True) and data.get("publisher_tenant") in first_party:
        return "microsoft"
    if scope.get("exclude_disabled", True) and not data["enabled"]:
        return "disabled"
    return "name_exclusions" if scopes.excluded_by_name(values["display_name"], scope) else None


AGENT_IDENTITIES_SCOPE = "agent_identities"


def unread_scope(observation) -> str | None:
    """The scope a run may not read for this observation (engine._in_unread): agent identities, listed through their
    own optional permission."""
    data = observation.data if isinstance(observation.data, dict) else {}
    return AGENT_IDENTITIES_SCOPE if data.get("agent") == "agent_identity" else None


def relevant(values):
    """The fields whose change brings a dismissed candidate back. Owners by object ID, so a later grant of
    User.ReadBasic.All (names appear) doesn't bring every dismissal back."""
    data = values["data"]
    return {
        "name": values["display_name"],
        "enabled": data["enabled"],
        "owners": sorted(o["external_id"] for o in data["owners"]),
    }


def record_fields(observation) -> dict[str, Any]:
    """An application created from the candidate (engine.create_record) carries its Entra IDs, so it doesn't start out
    differing from the source on them; a kind without these fields ignores them."""
    data = observation.data if isinstance(observation.data, dict) else {}
    return {
        "entra_app_id": data.get("app_id", ""),
        "service_principal_id": data.get("service_principal_id", ""),
    }


def scope_reason(observation, scope) -> str | None:
    """Why an observation the run didn't list is out of scope rather than gone: agent identities are only listed with
    include_agent_identities on."""
    data = observation.data if isinstance(observation.data, dict) else {}
    if data.get("agent") == "agent_identity" and not scope.get("include_agent_identities"):
        return "agent_identities"
    return None


def drift_values(observation, kind) -> dict[str, Any]:
    """The curated fields the source speaks for on a linked record (discovery/drift.py): name, description, the home
    page as documentation URL (https only, as create does), an application's Entra IDs, an AI agent's external ID (the
    app ID, as create does)."""
    data = observation.data if isinstance(observation.data, dict) else {}
    values = {
        "name": observation.display_name,
        "description": data.get("description", ""),
        "documentation_url": data.get("homepage", ""),
        "entra_app_id": data.get("app_id", ""),
        "service_principal_id": data.get("service_principal_id", ""),
    }
    if kind == "ai_agent":
        values["external_id"] = data.get("app_id") or observation.external_id
    return values


# ---- HTTP for Entra sign-in ----------------------------------------------------------------------------------------

TIMEOUT = 30


class HttpTransport:
    """Entra sign-in's (core/oidc.py) view of core/graph.py's transport: fixed hosts, https only, no redirects, a 30 s
    timeout. Answers are parsed as JSON and failures become readable SourceError texts. The discovery sources read
    through graph_source.Client instead (throttling, permission-naming errors)."""

    def __init__(self):
        self.http = graph.HttpTransport(timeout=TIMEOUT)

    def _call(self, method, url, headers, body=None):
        try:
            reply = self.http.send(method, url, headers, body)
        except graph.HostRefused:
            raise SourceError("Refused to call a host other than Microsoft's.") from None
        except graph.Unreachable:
            raise SourceError("Couldn't reach Entra from this server.") from None
        if reply.status in (401, 403):
            raise SourceError(
                f"Entra refused the request ({reply.status}). Check the secret and that Application.Read.All has admin consent."
            )
        if reply.status == 429:
            raise SourceError("Entra is throttling requests (429). The next run will try again.")
        if not reply.ok:
            raise SourceError(f"Entra answered {reply.status}.")
        try:
            return json.loads(reply.body.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SourceError("Couldn't reach Entra from this server.") from None

    def get(self, url, headers):
        return self._call("GET", url, headers)

    def post_form(self, url, form):
        data = urllib.parse.urlencode(form).encode()
        return self._call("POST", url, {"Content-Type": "application/x-www-form-urlencoded"}, data)
