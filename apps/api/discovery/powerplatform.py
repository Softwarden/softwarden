"""The Power Platform source (capability `discovery.powerplatform`): Copilot Studio agents from each environment's Dataverse,
as AI agent candidates (platform Copilot Studio), and optionally cloud flows as components.

Microsoft Learn:

- Environments are chosen by URL (`environment_urls`, the least-privileged default): each is read with a token for
  that environment (`https://<env>.crm[N].dynamics.com/.default`), which goes only to that host.
- `GET {env}/api/data/v9.2/bots?$select=...` (table Agent, entity set `bots`; verified): botid, name, schemaname,
  statecode (0 Active, 1 Inactive), statuscode, authenticationmode (0 Unspecified, 1 None, 2 Integrated, 3 Custom
  Entra ID, 4 Generic OAuth2), accesscontrolpolicy, language, publishedon, componentstate (2 and 3 are deleted: skipped,
  as the research suggests), _ownerid_value with its formatted value (`Prefer:
  odata.include-annotations="OData.Community.Display.V1.FormattedValue"`). Paged by `@odata.nextLink` with
  `odata.maxpagesize=500`.
- With `include_cloud_flows`: `GET {env}/api/data/v9.2/workflows?$filter=category eq 5` (table Process; category 5 is
  Modern Flow, verified). workflow statecode 0 Draft, 1 Activated, 2 Suspended is NOT verified on Learn. Flows the app
  can't read are a note, not a failure (the privilege is optional, like Graph's optional permissions).
- "All environments" (`all_environments`, opt-in): the Power Platform admin API (BAP), `GET
  https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments?api-version=2020-10-01`
  with a token for https://service.powerapps.com/ (generally available, no retirement date on Learn). Its
  service-principal registration is admin-equivalent (Microsoft treats the app like a Power Platform Administrator and
  it can't be limited); the newer Power Platform API's service-principal roles weren't verifiable, so BAP is used.
  An environment's `instanceUrl` is used only when it is a Dataverse host; any other is noted
  and never sent a token. If the list fails, the run fails (an environment it would have read could look gone).

Grant: an application user in each environment with a read-only security role that can read Agent (`prvReadbot`) and,
for flows, Process (`prvReadWorkflow`). These privilege names follow Dataverse's naming but are UNVERIFIED as enough
for this inventory; an earlier version needed the predefined Service Reader role on a fresh install. Dataverse answers a caller that
isn't a user of the environment with 401 and a missing privilege with 403 (`PrivilegeDenied`, `AccessDenied`, ...):
both are named as "needs an application user with read on ...", with the privilege when Dataverse names it.

An environment that can't be read (no application user, a refused token) is noted and its observations keep their
state; the run fails only when no environment could be read. Agents are keyed `<host>:<botid>` and flows
`<host>:<workflowid>`, so the same environment found by URL or by the admin API is one key; a rename keeps the row
(changed, not new). A flow's client data (its definition, which can hold connection details) is never asked for."""

from __future__ import annotations

import dataclasses
import urllib.parse
from typing import Any

from catalog.models import ComponentType

from . import access, scopes
from .graph_source import (
    BAP_AUDIENCE,
    BAP_HOST,
    DeadlineReached,
    Fetched,
    Permission,
    Probe,
    Session,
    SourceError,
    dataverse,
    dataverse_host,
    transports,
)
from .models import ObservationKind

DATAVERSE_API = "v9.2"
BAP_API = "2020-10-01"
BAP_ENVIRONMENTS = (
    f"https://{BAP_HOST}/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments?api-version={BAP_API}"
)
PREFER = {"Prefer": 'odata.include-annotations="OData.Community.Display.V1.FormattedValue",odata.maxpagesize=500'}
OWNER = "_ownerid_value@OData.Community.Display.V1.FormattedValue"
BOT_FIELDS = (
    "botid,name,schemaname,statecode,statuscode,authenticationmode,accesscontrolpolicy,language,publishedon,"
    "componentstate,_ownerid_value"
)
FLOW_FIELDS = "workflowid,name,statecode,description,_ownerid_value"
FLOW_FILTER = urllib.parse.quote("category eq 5")
PAGE_LIMIT = 200
# Nothing listed while observations are live fails the run (the empty-listing guard, discovery/engine._apply);
# so does one listing of several that comes back empty (Fetched.empty, engine._guard_empty_listings).
EMPTY_LISTING_NAME = "Dataverse's agent and flow listing"
ENVIRONMENTS_MAX = 50
DELETED = (2, 3)  # componentstate Deleted, Deleted Unpublished

APP_USER = (
    "Add the connection's app as an application user in the environment (Power Platform admin center > Environment > "
    "Settings > Users + permissions > Application users) with a read-only security role that can read {table} "
    "({privilege}, organization level). If that isn't enough, add the predefined Service Reader role."
)
PRV_READ_BOT = Permission(
    "prvReadbot",
    "",
    True,
    "Read Copilot Studio agents (the Agent table, bot) in each environment, through an application user's security "
    "role. The privilege name is unverified on Microsoft Learn (docs/PERMISSIONS.md).",
    False,
    kind="dataverse_privilege",
    advice=APP_USER.format(table="Agent (bot) rows", privilege="prvReadbot"),
)
PRV_READ_WORKFLOW = Permission(
    "prvReadWorkflow",
    "",
    False,
    "Optional: read cloud flows (the Process table, workflow) when the scope includes them. Unverified on Microsoft "
    "Learn, as prvReadbot.",
    False,
    kind="dataverse_privilege",
    advice=APP_USER.format(table="Process (workflow) rows", privilege="prvReadWorkflow"),
)
BAP_REGISTRATION = Permission(
    "Power Platform management app",
    "",
    False,
    "Optional, only for All environments: list the tenant's environments through the Power Platform admin API. "
    "Microsoft treats such an app like a Power Platform Administrator, and it can't be limited.",
    True,
    kind="power_platform_registration",
    advice="An administrator registers the app for the Power Platform admin API (PUT https://api.bap.microsoft.com/"
    "providers/Microsoft.BusinessAppPlatform/adminApplications/{client id}?api-version=2020-10-01, or pac admin "
    "create-service-principal). It is admin-equivalent: to avoid it, enter the environments' URLs instead.",
)

KINDS = [ObservationKind.AI_AGENT, ObservationKind.COMPONENT]
CONNECTION = "microsoft"
CONFIG_FIELDS: tuple[str, ...] = ()
SCOPE_DEFAULTS: dict[str, Any] = {
    "environment_urls": [],
    "all_environments": False,
    "include_cloud_flows": False,
    "name_exclusions": [],
}
FILTERS = {
    "no_id": "no ID in Dataverse",
    "deleted": "deleted in Dataverse",
    "name_exclusions": "name excluded",
    "cloud_flows": "cloud flow (not included)",
    "environments": "environment not in the scope",
}
# Skips that mean the object really is gone: Dataverse marked it deleted (discovery/engine._apply).
GONE_REASONS = ("deleted",)
PERMISSIONS = [PRV_READ_BOT, PRV_READ_WORKFLOW, BAP_REGISTRATION]
PROBES: list[Probe] = []  # built from the scope (access_steps)
DETAILS = (
    "environment_name",
    "environment_url",
    "environment_id",
    "schema_name",
    "state",
    "status",
    "authentication",
    "language",
    "published_on",
    "owner_name",
)
STATUS = {1: "Provisioned", 2: "Deprovisioned", 3: "Provisioning", 4: "Provision failed", 5: "Missing license"}
AUTHENTICATION = {0: "Unspecified", 1: "None", 2: "Integrated", 3: "Custom Entra ID", 4: "Generic OAuth2"}
USER_AUTH = (2, 3, 4)  # an agent acting as the signed-in user (catalog AgentAuthMode.USER)
FLOW_STATE = {0: "Draft", 1: "Activated", 2: "Suspended"}  # UNVERIFIED on Learn


def simulator(connector):
    from . import powerplatform_simulator

    return powerplatform_simulator.Transport(connector)


def scope_of(connector) -> dict[str, Any]:
    return dict(SCOPE_DEFAULTS) | dict(connector.scope or {})


def validate(config, scope, errors):
    for key in ("all_environments", "include_cloud_flows"):
        if key in scope and not isinstance(scope[key], bool):
            errors[f"scope.{key}"] = ["Choose yes or no."]
    urls = scope.get("environment_urls", [])
    if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
        errors["scope.environment_urls"] = ["Enter one environment URL per line."]
    else:
        hosts = [dataverse_host(u) for u in urls if u.strip()]
        if not all(hosts):
            errors["scope.environment_urls"] = [
                "Enter each environment's Dataverse URL, such as https://contoso.crm.dynamics.com (Power Platform "
                "admin center > Environment > Environment URL)."
            ]
        elif len(set(hosts)) > ENVIRONMENTS_MAX:
            errors["scope.environment_urls"] = [f"Use {ENVIRONMENTS_MAX} environments or fewer."]
        else:
            scope["environment_urls"] = [f"https://{h}" for h in sorted(set(hosts))]
    scopes.validate_names(scope, errors)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# ---- reading -------------------------------------------------------------------------------------------------------


def parse_environments(listed: list[Any]) -> tuple[list[dict[str, str]], int, int]:
    """The admin API's environments with a Dataverse host, [{host, name, id}], and how many had no database and how
    many gave an instance URL that isn't a Dataverse host (never sent a token)."""
    envs, no_database, elsewhere = [], 0, 0
    for env in listed:
        props = _dict(_dict(env).get("properties"))
        url = str(_dict(props.get("linkedEnvironmentMetadata")).get("instanceUrl") or "")
        host = dataverse_host(url)
        if not url:
            no_database += 1
        elif not host:
            elsewhere += 1
        else:
            name = str(props.get("displayName") or host)[:200]
            envs.append({"host": host, "name": name, "id": str(_dict(env).get("name") or "")[:64]})
    return envs, no_database, elsewhere


def _bap_environments(session: Session, found: Fetched) -> list[dict[str, str]]:
    """The tenant's environments with a Dataverse database, from the admin API (a refusal fails the run)."""
    client = session.client(BAP_AUDIENCE)
    required = dataclasses.replace(BAP_REGISTRATION, required=True)
    listed = client.get_all(BAP_ENVIRONMENTS, "list the tenant's environments", required, PAGE_LIMIT, None, "nextLink")
    envs, no_database, elsewhere = parse_environments(listed)
    if no_database:
        found.notes.append(f"{no_database} environment(s) have no Dataverse database, so no agents to read.")
    if elsewhere:
        found.notes.append(
            f"{elsewhere} environment(s) gave an address that isn't a Dataverse host; they weren't read."
        )
    return envs


def environments(session: Session, scope, found: Fetched) -> list[dict[str, str]]:
    """[{host, name, id}] to read: the admin API's (when All environments is on), then the URLs entered."""
    envs = _bap_environments(session, found) if scope.get("all_environments") else []
    seen = {e["host"] for e in envs}
    for url in scope.get("environment_urls") or []:
        host = dataverse_host(url)
        if host and host not in seen:
            seen.add(host)
            envs.append({"host": host, "name": host, "id": ""})
    return envs


def _base(host: str) -> str:
    return f"https://{host}/api/data/{DATAVERSE_API}"


def fetch(connector, credential, transport=None):
    """Agents (and flows) of every environment: [{"type": "agent"|"flow", "env": {...}, "row": {...}}]."""
    scope = scope_of(connector)
    if not scope.get("all_environments") and not scope.get("environment_urls"):
        raise SourceError(
            "No environment is chosen. Enter the environments' Dataverse URLs, or turn on All environments."
        )
    session = Session(transports(connector, credential, simulator, transport), credential)
    found = Fetched()
    envs = environments(session, scope, found)
    if not envs:  # All environments found none to read: nothing is known about the agents seen before
        raise SourceError(
            " ".join(["The admin API listed no environment with a Dataverse database to read.", *found.notes])
        )
    failures: list[SourceError] = []
    read = 0
    for env in envs:
        host, name = env["host"], env["name"]
        client = session.client(dataverse(host))
        try:
            bots = client.get_all(
                f"{_base(host)}/bots?$select={BOT_FIELDS}",
                f"list Copilot Studio agents in {name}",
                PRV_READ_BOT,
                PAGE_LIMIT,
                PREFER,
            )
        except DeadlineReached:
            raise
        except SourceError as e:  # refused, no token, unreachable, throttled: this environment only
            found.unread.add(f"env:{host}")
            found.notes.append(f"Environment {name} wasn't read: {e}")
            failures.append(e)
            continue
        read += 1
        if not bots:
            found.empty[f"env:{host}/agents"] = f"Dataverse's agent listing in {name}"
        found.extend({"type": "agent", "env": env, "row": b} for b in bots)
        if not scope.get("include_cloud_flows"):
            continue
        try:
            flows = client.get_all(
                f"{_base(host)}/workflows?$select={FLOW_FIELDS}&$filter={FLOW_FILTER}",
                f"list cloud flows in {name}",
                PRV_READ_WORKFLOW,
                PAGE_LIMIT,
                PREFER,
            )
        except DeadlineReached:
            raise
        except SourceError as e:
            found.unread.add(f"env:{host}/flows")
            found.notes.append(f"Cloud flows in {name} weren't read: {e}")
            continue
        if not flows:
            found.empty[f"env:{host}/flows"] = f"Dataverse's cloud flow listing in {name}"
        found.extend({"type": "flow", "env": env, "row": f} for f in flows)
    if failures and not read:
        raise failures[0]
    return found


# ---- the engine's contract ------------------------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def map_object(raw):
    env, row = raw["env"], raw["row"]
    host = env["host"]
    owner = str(row.get(OWNER) or "")[:200]
    common = {
        "environment_host": host,
        "environment_url": f"https://{host}",
        "environment_name": env["name"],
        "environment_id": env["id"],
        "owner_name": owner,
        "owners": [{"name": owner, "email": "", "external_id": ""}] if owner else [],
    }
    if raw["type"] == "agent":
        bot_id = str(row.get("botid") or "").lower()
        name = str(row.get("name") or row.get("schemaname") or bot_id)
        state, status, auth = (
            _int(row.get("statecode")),
            _int(row.get("statuscode")),
            _int(row.get("authenticationmode")),
        )
        return {
            "kind": ObservationKind.AI_AGENT,
            "external_id": f"{host}:{bot_id}"[:200],
            "display_name": name[:400],
            "ai_agent": True,
            "data": {
                **common,
                "agent": "copilot_studio",
                "bot_id": bot_id,
                "schema_name": str(row.get("schemaname") or "")[:100],
                "state": {0: "Active", 1: "Inactive"}.get(state, "") if state is not None else "",
                "status": STATUS.get(status, "") if status is not None else "",
                "authentication": AUTHENTICATION.get(auth, "") if auth is not None else "",
                "user_auth": auth in USER_AUTH,
                "language": _int(row.get("language")),
                "published_on": str(row.get("publishedon") or "")[:40],
                "component_state": _int(row.get("componentstate")) or 0,
                "description": f"Copilot Studio agent in {env['name']}.",
                "scope": f"env:{host}/agents",
            },
        }
    flow_id = str(row.get("workflowid") or "").lower()
    state = _int(row.get("statecode"))
    return {
        "kind": ObservationKind.COMPONENT,
        "external_id": f"{host}:{flow_id}"[:200],
        "display_name": str(row.get("name") or flow_id)[:400],
        "ai_agent": False,
        "suggested_component_type": ComponentType.AUTOMATION_FLOW,
        "data": {
            **common,
            "workflow_id": flow_id,
            "state": FLOW_STATE.get(state, "") if state is not None else "",
            "description": str(row.get("description") or "")[:2000],
            "component_state": 0,
            "scope": f"env:{host}/flows",
        },
    }


def skip_reason(values, scope):
    data = values["data"]
    if not (data.get("bot_id") or data.get("workflow_id")):
        return "no_id"
    if data.get("component_state") in DELETED:
        return "deleted"
    return "name_exclusions" if scopes.excluded_by_name(values["display_name"], scope) else None


def relevant(values):
    """What brings a dismissed candidate back: its name and state (an agent turned off or on)."""
    data = values["data"]
    return {"name": values["display_name"], "state": data["state"], "status": data.get("status", "")}


def record_fields(observation) -> dict[str, Any]:
    """An AI agent (Copilot Studio) or a component (cloud flow) prefilled from Dataverse (engine.create_record)."""
    data = observation.data if isinstance(observation.data, dict) else {}
    fields: dict[str, Any] = {"pp_environment_id": data.get("environment_id", "")[:64]}
    if observation.kind == ObservationKind.AI_AGENT:
        fields |= {
            "platform": "copilot_studio",
            "external_id": data.get("schema_name") or data.get("bot_id", ""),
            "auth_mode": "user" if data.get("user_auth") else "",
        }
    else:
        fields |= {
            "component_type": ComponentType.AUTOMATION_FLOW,
            "hosting": "power_platform",
            "external_id": data.get("workflow_id", ""),
        }
    return fields


def scope_reason(observation, scope) -> str | None:
    """Why an observation the run didn't list is out of scope rather than gone: cloud flows are no longer read, or its
    environment is no longer in the scope (neither listed nor covered by all_environments)."""
    data = observation.data if isinstance(observation.data, dict) else {}
    if observation.kind == ObservationKind.COMPONENT and not scope.get("include_cloud_flows"):
        return "cloud_flows"
    hosts = {str(u).lower().removeprefix("https://").rstrip("/") for u in scope.get("environment_urls") or []}
    if not scope.get("all_environments") and str(data.get("environment_host") or "").lower() not in hosts:
        return "environments"
    return None


def drift_values(observation, kind) -> dict[str, Any]:
    """The curated fields Dataverse speaks for on a linked record (discovery/drift.py): name, the environment ID and the
    external ID (an agent's schema name, a flow's workflow ID), as create prefills them; a flow's description (an
    agent's is Softwarden's own sentence, not the source's)."""
    data = observation.data if isinstance(observation.data, dict) else {}
    values = {"name": observation.display_name, "pp_environment_id": data.get("environment_id", "")}
    if observation.kind == ObservationKind.AI_AGENT:
        values["external_id"] = data.get("schema_name") or data.get("bot_id", "")
    else:
        values |= {"external_id": data.get("workflow_id", ""), "description": data.get("description", "")}
    return values


# ---- access check ---------------------------------------------------------------------------------------------------


def access_steps(connector, scope, session: Session) -> list[dict[str, Any]]:
    """The admin API's environment list (only with All environments), then per environment (up to PROBE_CAP): sign in
    to its Dataverse, read one agent, and one cloud flow when the scope includes them."""
    steps: list[dict[str, Any]] = []
    envs: list[dict[str, str]] = []
    if scope.get("all_environments"):
        client = session.client(BAP_AUDIENCE)
        required = dataclasses.replace(BAP_REGISTRATION, required=True)
        steps.append(access.sign_in(client))
        if steps[-1]["state"] == "ok":
            step, body = access.probe(client, Probe("List environments", BAP_ENVIRONMENTS, required))
            steps.append(step)
            envs = parse_environments((body or {}).get("value") or [])[0]
        else:
            steps.append(access.skipped("List environments", required))
    seen = {e["host"] for e in envs}
    for url in scope.get("environment_urls") or []:
        host = dataverse_host(url)
        if host and host not in seen:
            seen.add(host)
            envs.append({"host": host, "name": host})
    if not envs and not scope.get("all_environments"):
        return steps + [
            access.step(
                "api",
                "Environments",
                "failed",
                problem="not_configured",
                detail="No environment is chosen.",
                next_step="Enter the environments' Dataverse URLs, such as https://contoso.crm.dynamics.com, or turn "
                "on All environments.",
            )
        ]
    for env in envs[: access.PROBE_CAP]:
        host, name = env["host"], env["name"]
        client = session.client(dataverse(host))
        steps.append(access.sign_in(client))
        agents = f"Read agents in {name}"
        flows = f"Read cloud flows in {name}"
        if steps[-1]["state"] != "ok":
            steps.append(access.skipped(agents, PRV_READ_BOT))
            continue
        steps.append(access.probe(client, Probe(agents, f"{_base(host)}/bots?$top=1&$select=botid", PRV_READ_BOT))[0])
        if scope.get("include_cloud_flows"):
            url = f"{_base(host)}/workflows?$top=1&$select=workflowid&$filter={FLOW_FILTER}"
            steps.append(access.probe(client, Probe(flows, url, PRV_READ_WORKFLOW))[0])
        else:
            steps.append(access.not_used(flows, PRV_READ_WORKFLOW, "include_cloud_flows"))
    if len(envs) > access.PROBE_CAP:
        steps.append(access.not_checked("More environments", len(envs) - access.PROBE_CAP, "environment(s)"))
    return steps
