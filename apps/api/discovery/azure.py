"""The Azure source (capability
`discovery.azure`, every edition): Logic Apps, Function Apps, optionally App Services, and Automation runbooks, read-only
from Azure Resource Manager with the connector's Microsoft connection (a token for https://management.azure.com).

Microsoft Learn: each
resource type is read through its own list call, which returns its properties (the generic Resources list returns none),
following `nextLink` (only on management.azure.com):

- `GET /subscriptions?api-version=2022-12-01` (verified) when no subscription is chosen: every subscription the app
  has a role on, so Reader on a management group covers all the subscriptions under it; else
  `GET /subscriptions/{id}?api-version=2022-12-01` for each chosen one. Only Enabled, Warned and PastDue subscriptions
  are read; the rest are noted and their observations keep their state.
- `GET /subscriptions/{id}/providers/Microsoft.Logic/workflows?api-version=2019-05-01` (verified): Consumption Logic
  Apps, `properties.state`.
- `GET /subscriptions/{id}/providers/Microsoft.Web/sites?api-version=2024-04-01` (Web Apps - List; the api-version is
  NOT in the research: confirm on Learn): function apps, App Services and Standard Logic Apps, told apart by `kind`
  (comma-separated tokens): `workflowapp` is a Standard Logic App (Learn shows only the APP_KIND setting value
  `workflowApp`: to confirm live), `functionapp` a function app (Learn: `functionapp`, `functionapp,linux`), `app` or
  `api` (or no kind) an App Service. Any other kind is skipped as "not recognised" and the run notes the kind strings,
  for live checking (Flex Consumption among them).
- `GET /subscriptions/{id}/providers/Microsoft.Automation/automationAccounts?api-version=2024-10-23` (the list's
  api-version is assumed from the runbooks page: confirm on Learn), then for each account
  `GET {account id}/runbooks?api-version=2024-10-23` (verified): `runbookType`, `state`, `description`.

Grant: the Azure role Reader (role definition acdd72a7-3385-48ef-bd42-f606fba81ae7, `*/read`; verified) on each
subscription or a management group above them. A refused read (403 AuthorizationFailed) names the action and scope
ARM gave and the fix: Reader on that subscription.

A subscription, or one resource type in it (`sub:X/type:logic|sites|automation`, and an Automation account's
runbooks under its type), that can't be read is noted and its observations keep their state ("not read", never "no
longer seen"), while the other types still update; the run fails only when nothing could be read. A resource provider
the subscription never registered (409 MissingSubscriptionRegistration) is an empty list, noted. Subscriptions - Get
answers 404 SubscriptionNotFound for a subscription the app has no role on: that is a missing Reader. Logic App
definitions and site settings (which can hold secrets) are never kept: only names, IDs, location, tags, state and kind.
Observations are components (Logic App, Function, Service, Runbook), keyed by a UUID derived from the lower-cased
resource ID (`data.resource_id` keeps the ID itself); ARM can't rename a resource, so a renamed one is a new resource
and the old one is no longer seen."""

from __future__ import annotations

import re
import uuid
from typing import Any

from catalog.models import ComponentType

from . import access, scopes
from .graph_source import (
    ARM_AUDIENCE,
    ARM_HOST,
    Fetched,
    Permission,
    Probe,
    Refused,
    Session,
    SourceError,
    reader_advice,
    transports,
)
from .models import ObservationKind

ARM = f"https://{ARM_HOST}"
SUBSCRIPTIONS_API = "2022-12-01"  # verified
LOGIC_API = "2019-05-01"  # verified
WEB_API = "2024-04-01"  # NOT verified in the research: confirm on Microsoft Learn (Web Apps - List)
AUTOMATION_API = "2024-10-23"  # verified for runbooks; assumed for the automation accounts list
READABLE = ("Enabled", "Warned", "PastDue")
NOT_REGISTERED = "MissingSubscriptionRegistration"  # 409: the subscription never registered the resource provider
PAGE_LIMIT = 200
# Nothing listed while observations are live fails the run (the empty-listing guard, discovery/engine._apply);
# so does one listing of several that comes back empty (Fetched.empty, engine._guard_empty_listings).
EMPTY_LISTING_NAME = "Azure Resource Manager's resource listing"
SUBSCRIPTIONS_MAX = 50
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ACCOUNT_ID = re.compile(
    r"^/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[A-Za-z0-9._()-]{1,90}/providers/"
    r"Microsoft\.Automation/automationAccounts/[A-Za-z0-9-]{1,50}$",
    re.I,
)
KIND_TEXT = re.compile(r"^[A-Za-z0-9,._-]{1,60}$")

READER = Permission(
    "Reader",
    "acdd72a7-3385-48ef-bd42-f606fba81ae7",
    True,
    "Azure role on each subscription to read, or a management group above them: list subscriptions, Logic Apps, web "
    "apps (Function Apps, App Services), Automation accounts and runbooks (action */read). Read only.",
    True,
    kind="azure_rbac",
    advice=reader_advice(),
)

KINDS = [ObservationKind.COMPONENT]
CONNECTION = "microsoft"
CONFIG_FIELDS: tuple[str, ...] = ()
SCOPE_DEFAULTS: dict[str, Any] = {
    "subscriptions": [],
    "include_logic_apps": True,
    "include_function_apps": True,
    "include_app_services": False,
    "include_runbooks": True,
    "name_exclusions": [],
}
FLAGS = ("include_logic_apps", "include_function_apps", "include_app_services", "include_runbooks")
FILTERS = {
    "logic_apps": "Logic App (not included)",
    "function_apps": "Function App (not included)",
    "app_services": "App Service (not included)",
    "unknown_kind": "web app of a kind not recognized",
    "name_exclusions": "name excluded",
    "runbooks": "runbook (not included)",
    "subscriptions": "subscription not in the scope",
}
# The scope flag that reads each resource type: with it off, its observations are out of scope (scope_reason).
TYPE_FLAG = {
    "logic_app": ("include_logic_apps", "logic_apps"),
    "function_app": ("include_function_apps", "function_apps"),
    "app_service": ("include_app_services", "app_services"),
    "runbook": ("include_runbooks", "runbooks"),
}
PERMISSIONS = [READER]
PROBES: list[Probe] = []  # built from the scope (access_steps)
# The observation data the API shows as the candidate's details (discovery/serializers.py).
DETAILS = (
    "azure_type",
    "resource_id",
    "subscription_id",
    "subscription_name",
    "resource_group",
    "location",
    "state",
    "site_kind",
    "runbook_type",
    "automation_account",
    "environment",
    "tags",
)
COMPONENT_TYPE = {
    "logic_app": ComponentType.LOGIC_APP,
    "function_app": ComponentType.FUNCTION,
    "app_service": ComponentType.SERVICE,
    "runbook": ComponentType.RUNBOOK,
    "unknown": ComponentType.OTHER,
}
LABEL = {"logic_app": "Logic App", "function_app": "Function App", "app_service": "App Service", "runbook": "Runbook"}
ENV_TAGS = ("environment", "env")
ENV_MAP = {
    "prod": "prod",
    "production": "prod",
    "prd": "prod",
    "test": "test",
    "tst": "test",
    "qa": "test",
    "uat": "test",
    "staging": "test",
    "stage": "test",
    "dev": "dev",
    "development": "dev",
}
TAGS_MAX = 50


def simulator(connector):
    from . import azure_simulator

    return azure_simulator.Transport(connector)


def scope_of(connector) -> dict[str, Any]:
    return dict(SCOPE_DEFAULTS) | dict(connector.scope or {})


def validate(config, scope, errors):
    for key in FLAGS:
        if key in scope and not isinstance(scope[key], bool):
            errors[f"scope.{key}"] = ["Choose yes or no."]
    subs = scope.get("subscriptions", [])
    if not isinstance(subs, list) or not all(isinstance(s, str) and GUID.fullmatch(s.strip().lower()) for s in subs):
        errors["scope.subscriptions"] = ["Enter one subscription ID (a GUID) per line, or none for every subscription."]
    elif len(subs) > SUBSCRIPTIONS_MAX:
        errors["scope.subscriptions"] = [f"Use {SUBSCRIPTIONS_MAX} subscriptions or fewer."]
    else:
        scope["subscriptions"] = sorted({s.strip().lower() for s in subs})
    scopes.validate_names(scope, errors)


# ---- reading -------------------------------------------------------------------------------------------------------


def site_type(kind: Any) -> str:
    """logic_app (a Standard Logic App), function_app, app_service or unknown, from a web app's `kind`."""
    tokens = {t.strip().lower() for t in str(kind or "").split(",") if t.strip()}
    if "workflowapp" in tokens:
        return "logic_app"
    if "functionapp" in tokens:
        return "function_app"
    if not tokens or tokens & {"app", "api"}:
        return "app_service"
    return "unknown"


def subscription(value: Any) -> dict[str, str] | None:
    """{id, name, state} of a subscription from Subscriptions - List or Get, or None without a valid ID."""
    if not isinstance(value, dict):
        return None
    sid = str(value.get("subscriptionId") or "").lower()
    if not GUID.fullmatch(sid):
        return None
    return {"id": sid, "name": str(value.get("displayName") or sid)[:200], "state": str(value.get("state") or "")}


def _subscriptions(client, scope, found: Fetched, failures: list[SourceError]) -> list[dict[str, str]]:
    """[{id, name, state}] to read: the chosen ones (each read, so a missing role is named), else every one listed."""
    chosen = scope.get("subscriptions") or []
    if not chosen:
        listed = client.get_all(
            f"{ARM}/subscriptions?api-version={SUBSCRIPTIONS_API}",
            "list subscriptions",
            READER,
            PAGE_LIMIT,
            None,
            "nextLink",
        )
        subs = [s for s in map(subscription, listed) if s]
        if not subs:
            raise SourceError(
                f"Azure Resource Manager lists no subscription for the app: it has no role on any. {reader_advice()}"
            )
        return subs
    subs = []
    for sid in chosen:
        try:
            body = client.get(
                f"{ARM}/subscriptions/{sid}?api-version={SUBSCRIPTIONS_API}", f"read subscription {sid}", READER
            )
        except Refused as e:
            found.unread.add(f"sub:{sid}")
            found.notes.append(f"Subscription {sid} wasn't read: {e}")
            failures.append(e)
            continue
        subs.append(subscription({"subscriptionId": sid, **body}) or {"id": sid, "name": sid, "state": ""})
    return subs


def _item(type_: str, resource: dict[str, Any], sub: dict[str, str], scope: str, account=None) -> dict[str, Any]:
    return {
        "type": type_,
        "resource": resource,
        "subscription": {"id": sub["id"], "name": sub["name"]},
        "account": account,
        "scope": scope,
    }


def _list(client, url: str, what: str, found: Fetched, unread: str, failures: list[SourceError]):
    """One resource list, or None when it was refused: its scope is then unread (its candidates keep their state)
    and the run says why. A resource provider the subscription never registered (409 MissingSubscriptionRegistration)
    has nothing to list: an empty list, noted."""
    try:
        rows = client.get_all(url, what, READER, PAGE_LIMIT, None, "nextLink")
        if not rows:
            found.empty[unread] = f"Azure Resource Manager's list of {what.removeprefix('list ')}"
        return rows
    except Refused as e:
        if e.status == 409 and e.code == NOT_REGISTERED:
            found.notes.append(
                f"Couldn't {what}: its resource provider isn't registered in the subscription, so there are none."
            )
            return []
        found.unread.add(unread)
        found.notes.append(f"Not read, their candidates keep their state: {e}")
        failures.append(e)
        return None


def _read(client, sub: dict[str, str], scope, found: Fetched, failures: list[SourceError]) -> tuple[list, bool]:
    """The subscription's in-scope resources, each resource type on its own (`sub:X/type:T`), and whether any list was
    read at all."""
    base, name, key = f"{ARM}/subscriptions/{sub['id']}", sub["name"], f"sub:{sub['id']}"
    items: list[dict[str, Any]] = []
    read = False
    if scope["include_logic_apps"]:
        rows = _list(
            client,
            f"{base}/providers/Microsoft.Logic/workflows?api-version={LOGIC_API}",
            f"list Logic Apps in subscription {name}",
            found,
            f"{key}/type:logic",
            failures,
        )
        if rows is not None:
            read = True
            items.extend(_item("logic_app", w, sub, f"{key}/type:logic") for w in rows)
    if scope["include_logic_apps"] or scope["include_function_apps"] or scope["include_app_services"]:
        rows = _list(
            client,
            f"{base}/providers/Microsoft.Web/sites?api-version={WEB_API}",
            f"list web apps in subscription {name}",
            found,
            f"{key}/type:sites",
            failures,
        )
        if rows is not None:
            read = True
            items.extend(_item(site_type(s.get("kind")), s, sub, f"{key}/type:sites") for s in rows)
    if scope["include_runbooks"]:
        automation = f"{key}/type:automation"
        accounts = _list(
            client,
            f"{base}/providers/Microsoft.Automation/automationAccounts?api-version={AUTOMATION_API}",
            f"list Automation accounts in subscription {name}",
            found,
            automation,
            failures,
        )
        if accounts is not None:
            read = True
        for account in accounts or []:
            account_id = str(account.get("id") or "")
            if not ACCOUNT_ID.fullmatch(account_id):
                continue
            label = str(account.get("name") or account_id.rsplit("/", 1)[-1])[:100]
            account_key = f"{automation}/account:{account_id.lower()}"
            runbooks = _list(
                client,
                f"{ARM}{account_id}/runbooks?api-version={AUTOMATION_API}",
                f"list runbooks in Automation account {label}",
                found,
                account_key,
                [],  # one account's runbooks not read doesn't count against the subscription
            )
            items.extend(
                _item("runbook", rb, sub, account_key, {"id": account_id, "name": label}) for rb in runbooks or []
            )
    return items, read


def fetch(connector, credential, transport=None):
    """Every in-scope resource of every readable subscription: [{"type", "resource", "subscription", "account",
    "scope"}]."""
    scope = scope_of(connector)
    session = Session(transports(connector, credential, simulator, transport), credential)
    client = session.client(ARM_AUDIENCE)
    found = Fetched()
    failures: list[SourceError] = []
    subs = _subscriptions(client, scope, found, failures)
    read = 0
    inactive = []
    for sub in subs:
        if sub["state"] not in READABLE:
            found.unread.add(f"sub:{sub['id']}")
            inactive.append(f"{sub['name']} ({sub['state'] or 'no state'})")
            continue
        items, any_read = _read(client, sub, scope, found, failures)
        found.extend(items)
        read += any_read
    if failures and not read:
        raise failures[0]
    if inactive:
        found.notes.append(
            f"{len(inactive)} subscription(s) aren't active and weren't read: {', '.join(inactive[:5])}. Their "
            "resources keep their state."
        )
    _note_kinds(found)
    return found


def _note_kinds(found: Fetched) -> None:
    """Kind strings to check on a live tenant: Standard Logic Apps and unrecognised kinds."""
    sites = [i for i in found if str(i["resource"].get("type") or "").lower() == "microsoft.web/sites"]
    standard = [i for i in sites if i["type"] == "logic_app"]
    unknown = sorted({_kind_text(i["resource"].get("kind")) for i in sites if i["type"] == "unknown"})
    if standard:
        found.notes.append(
            f"{len(standard)} web app(s) of kind 'workflowapp' were read as Standard Logic Apps (the kind string is "
            "to be confirmed on a live tenant)."
        )
    if unknown:
        found.notes.append(
            f"{sum(1 for i in sites if i['type'] == 'unknown')} web app(s) had a kind Softwarden doesn't recognize "
            f"({', '.join(unknown[:5])}) and weren't kept; report it so the kind can be mapped."
        )


def _kind_text(kind: Any) -> str:
    text = str(kind or "")
    return text if KIND_TEXT.fullmatch(text) else "unreadable kind"


# ---- the engine's contract ------------------------------------------------------------------------------------------


def _tags(tags: Any) -> dict[str, str]:
    if not isinstance(tags, dict):
        return {}
    return {str(k)[:128]: str(v)[:256] for k, v in list(tags.items())[:TAGS_MAX]}


def _environment(tags: dict[str, str]) -> str:
    for key, value in tags.items():
        if key.lower() in ENV_TAGS:
            return ENV_MAP.get(value.strip().lower(), "")
    return ""


def _resource_group(resource_id: str) -> str:
    parts = resource_id.split("/")
    for i, part in enumerate(parts[:-1]):
        if part.lower() == "resourcegroups":
            return parts[i + 1][:90]
    return ""


def map_object(raw):
    r = raw["resource"]
    props = r.get("properties") if isinstance(r.get("properties"), dict) else {}
    rid = str(r.get("id") or "")
    type_ = raw["type"]
    sub = raw["subscription"]
    account = raw.get("account") or {}
    tags = _tags(r.get("tags"))
    identity = r.get("identity") if isinstance(r.get("identity"), dict) else {}
    name = str(r.get("name") or rid.rsplit("/", 1)[-1])
    data = {
        "azure_type": type_,
        "resource_id": rid[:500],
        "resource_type": str(r.get("type") or "")[:200],
        "subscription_id": sub["id"],
        "subscription_name": sub["name"],
        "resource_group": _resource_group(rid),
        "location": str(r.get("location") or "")[:60],
        "tags": tags,
        "state": str(props.get("state") or "")[:40],
        "site_kind": _kind_text(r.get("kind")) if str(r.get("type") or "").lower() == "microsoft.web/sites" else "",
        "runbook_type": str(props.get("runbookType") or "")[:40] if type_ == "runbook" else "",
        "automation_account": account.get("name", ""),
        "description": str(props.get("description") or "")[:2000],
        "environment": _environment(tags),
        "identity": str(identity.get("type") or "")[:60],
        "scope": raw["scope"],
    }
    return {
        "kind": ObservationKind.COMPONENT,
        "external_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"azure:{rid.lower()}")),
        "display_name": name[:400],
        "ai_agent": False,
        "suggested_component_type": COMPONENT_TYPE[type_],
        "data": data,
    }


def skip_reason(values, scope):
    scope = dict(SCOPE_DEFAULTS) | dict(scope or {})
    type_ = values["data"]["azure_type"]
    if type_ == "unknown":
        return "unknown_kind"
    if type_ == "logic_app" and not scope["include_logic_apps"]:
        return "logic_apps"
    if type_ == "function_app" and not scope["include_function_apps"]:
        return "function_apps"
    if type_ == "app_service" and not scope["include_app_services"]:
        return "app_services"
    return "name_exclusions" if scopes.excluded_by_name(values["display_name"], scope) else None


def relevant(values):
    """What brings a dismissed candidate back: its name, what it is and its state (a Logic App turned off or on)."""
    data = values["data"]
    return {"name": values["display_name"], "azure_type": data["azure_type"], "state": data["state"]}


def record_fields(observation) -> dict[str, Any]:
    """A component prefilled from the resource (engine.create_record); curated values are the person's."""
    data = observation.data if isinstance(observation.data, dict) else {}
    return {
        "component_type": observation.suggested_component_type or ComponentType.OTHER,
        "hosting": "azure",
        "azure_resource_id": data.get("resource_id", ""),
        "environment": data.get("environment", ""),
    }


def scope_reason(observation, scope) -> str | None:
    """Why an observation the run didn't list is out of scope rather than gone (discovery/engine._apply): its resource
    type is no longer read, or the scope names subscriptions and not its own. A subscription that drops out of an
    unnamed scope (its role removed) still makes its resources gone."""
    data = observation.data if isinstance(observation.data, dict) else {}
    flag = TYPE_FLAG.get(str(data.get("azure_type") or ""))
    if flag is not None and not scope.get(flag[0], True):
        return flag[1]
    named = {str(s).lower() for s in scope.get("subscriptions") or []}
    if named and str(data.get("subscription_id") or "").lower() not in named:
        return "subscriptions"
    return None


def drift_values(observation, kind) -> dict[str, Any]:
    """The curated fields the resource speaks for on a linked record (discovery/drift.py): name, description, the ARM
    resource ID and the environment from its tag."""
    data = observation.data if isinstance(observation.data, dict) else {}
    return {
        "name": observation.display_name,
        "description": data.get("description", ""),
        "azure_resource_id": data.get("resource_id", ""),
        "environment": data.get("environment", ""),
    }


# ---- access check ---------------------------------------------------------------------------------------------------


def access_steps(connector, scope, session: Session) -> list[dict[str, Any]]:
    """Sign in to ARM; read the chosen subscriptions (or list them); then one read per resource type the scope uses in
    each (up to access.PROBE_CAP subscriptions), and the first Automation account's runbooks."""
    client = session.client(ARM_AUDIENCE)
    steps = [access.sign_in(client)]
    if steps[-1]["state"] != "ok":
        return steps + [access.skipped("Read subscriptions", READER)]
    subs: list[dict[str, str]] = []
    chosen = scope.get("subscriptions") or []
    if chosen:
        for sid in chosen[: access.PROBE_CAP]:
            step, body = access.probe(
                client,
                Probe(f"Read subscription {sid}", f"{ARM}/subscriptions/{sid}?api-version={SUBSCRIPTIONS_API}", READER),
            )
            steps.append(step)
            if body is not None:
                subs.append(subscription({"subscriptionId": sid, **body}) or {"id": sid, "name": sid, "state": ""})
        if len(chosen) > access.PROBE_CAP:
            steps.append(access.not_checked("More subscriptions", len(chosen) - access.PROBE_CAP, "subscription(s)"))
    else:
        step, body = access.probe(
            client, Probe("List subscriptions", f"{ARM}/subscriptions?api-version={SUBSCRIPTIONS_API}", READER)
        )
        steps.append(step)
        subs = [sub for sub in map(subscription, (body or {}).get("value") or []) if sub]
        if body is not None and not subs:
            steps[-1] = access.step(
                "api",
                "List subscriptions",
                "failed",
                permission=READER,
                problem="missing_permission",
                detail="Azure Resource Manager lists no subscription for the app: it has no role on any.",
                next_step=reader_advice(),
            )
        if len(subs) > access.PROBE_CAP:
            steps.append(access.not_checked("More subscriptions", len(subs) - access.PROBE_CAP, "subscription(s)"))
    for sub in subs[: access.PROBE_CAP]:
        steps.extend(_subscription_steps(client, sub, scope))
    return steps


def _subscription_steps(client, sub, scope) -> list[dict[str, Any]]:
    name, base = sub["name"] or sub["id"], f"{ARM}/subscriptions/{sub['id']}"
    if sub["state"] not in READABLE:
        return [
            access.step(
                "api",
                f"Subscription {name}",
                "warning",
                detail=f"State {sub['state'] or 'unknown'}: nothing is read from it until it is active.",
                next_step="Choose only active subscriptions, or reactivate it.",
            )
        ]
    steps = []
    reads = (
        (
            "include_logic_apps",
            f"Read Logic Apps in {name}",
            f"{base}/providers/Microsoft.Logic/workflows?api-version={LOGIC_API}&$top=1",
        ),
        (
            ("include_logic_apps", "include_function_apps", "include_app_services"),
            f"Read web apps in {name}",
            f"{base}/providers/Microsoft.Web/sites?api-version={WEB_API}",
        ),
        (
            "include_runbooks",
            f"Read Automation accounts in {name}",
            f"{base}/providers/Microsoft.Automation/automationAccounts?api-version={AUTOMATION_API}",
        ),
    )
    accounts: list[dict[str, Any]] = []
    for flags, label, url in reads:
        keys = flags if isinstance(flags, tuple) else (flags,)
        if not any(scope.get(k) for k in keys):
            steps.append(access.not_used(label, READER, " or ".join(keys)))
            continue
        step, body = access.probe(client, Probe(label, url, READER))
        if step["state"] == "failed" and step["code"] == NOT_REGISTERED:
            step = access.step(
                "api", label, "ok", permission=READER, detail="Its resource provider isn't registered: none to read."
            )
        steps.append(step)
        if "automationAccounts" in url and body is not None:
            accounts = [
                a
                for a in body.get("value") or []
                if isinstance(a, dict) and ACCOUNT_ID.fullmatch(str(a.get("id") or ""))
            ]
    if scope.get("include_runbooks") and accounts:
        first = accounts[0]
        steps.append(
            access.probe(
                client,
                Probe(
                    f"Read runbooks in {str(first.get('name') or '')[:100]}",
                    f"{ARM}{first['id']}/runbooks?api-version={AUTOMATION_API}",
                    READER,
                ),
            )[0]
        )
    return steps
