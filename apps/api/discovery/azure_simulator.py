"""A synthetic Azure tenant for the azure source (discovery/azure.py): Azure Resource Manager's subscription, Logic
Apps, web apps (Microsoft.Web/sites), Automation account and runbook lists, answered as Microsoft Learn documents them,
with the api-versions the source must send (any other is a 400
InvalidApiVersionParameter, so a wrong version fails a test). Selected by the tenant ID `simulated`; tokens and the
shared checks are discovery/simulator_base.py's.

Three subscriptions: Contoso Production and Contoso Development (Enabled) and Contoso Legacy (Disabled). Production
has two Logic Apps, five web apps (two function apps, two App Services and a Standard Logic App, kind
`functionapp,workflowapp`) and an Automation account with two runbooks; Development one Logic App and one function app.

Config keys (tests only), besides simulator_base's:

- `simulator_changes`: edits in order, {"op", "id" (resource name), "value"}: state (properties.state), tag (value
  {key: value} merged into tags), kind (a web app's kind), rename (a new resource ID: ARM can't rename, so the old one
  is gone and a new one appears), remove, add (value {"type": logic|site|runbook, "sub": subscription, "kind"}).
- `simulated_denied`: ["<subscription id>/<provider type>"] (e.g. ".../Microsoft.Web/sites"): that list answers 403
  AuthorizationFailed naming the action (`<type>/read`) and the scope (`/subscriptions/<id>`), as a role without that
  action would. ["<subscription id>"] denies every read in the subscription, and the subscription isn't listed.
- `simulated_no_role`: the app has no role anywhere: Subscriptions - List is empty.
- `simulated_not_found`: a subscription the app has no role on answers Subscriptions - Get with 404
  SubscriptionNotFound (as Microsoft Learn says ARM does) instead of 403 AuthorizationFailed.
- `simulated_unregistered`: ["<subscription id>/<provider type>"]: that list answers 409
  MissingSubscriptionRegistration (the resource provider was never registered in the subscription)."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from core.graph import Reply

from .simulator_base import MicrosoftSimulator, error, json_reply

PROD = "11111111-1111-4111-8111-111111111111"
DEV = "22222222-2222-4222-8222-222222222222"
LEGACY = "33333333-3333-4333-8333-333333333333"
TENANT = "00000000-0000-4000-8000-00000000c0de"
CLIENT_ID = "0a0a0a0a-0000-4000-8000-0000000000aa"
OBJECT_ID = "0b0b0b0b-0000-4000-8000-0000000000bb"
SUBSCRIPTIONS = [
    (PROD, "Contoso Production", "Enabled"),
    (DEV, "Contoso Development", "Enabled"),
    (LEGACY, "Contoso Legacy", "Disabled"),
]
API = {
    "subscriptions": "2022-12-01",
    "Microsoft.Logic/workflows": "2019-05-01",
    "Microsoft.Web/sites": "2024-04-01",
    "Microsoft.Automation/automationAccounts": "2024-10-23",
    "runbooks": "2024-10-23",
}
# (subscription, resource group, type, name, extra)
BASE: list[tuple[str, str, str, str, dict[str, Any]]] = [
    (
        PROD,
        "rg-integration",
        "logic",
        "invoice-intake",
        {"state": "Enabled", "tags": {"environment": "Production", "costCenter": "FIN-01"}},
    ),
    (PROD, "rg-integration", "logic", "hr-sync", {"state": "Disabled", "tags": {}}),
    (PROD, "rg-apps", "site", "payroll-func", {"kind": "functionapp", "state": "Running", "tags": {"env": "prod"}}),
    (PROD, "rg-apps", "site", "orders-func", {"kind": "functionapp,linux", "state": "Running", "tags": {}}),
    (PROD, "rg-apps", "site", "intranet-web", {"kind": "app", "state": "Running", "tags": {"env": "prod"}}),
    (PROD, "rg-apps", "site", "partner-api", {"kind": "app,linux", "state": "Stopped", "tags": {}}),
    (PROD, "rg-integration", "site", "contoso-workflows", {"kind": "functionapp,workflowapp", "state": "Running"}),
    (PROD, "rg-ops", "account", "ops-automation", {}),
    (
        PROD,
        "rg-ops",
        "runbook",
        "Rotate-Keys",
        {
            "account": "ops-automation",
            "runbookType": "PowerShell72",
            "state": "Published",
            "description": "Rotates keys.",
        },
    ),
    (
        PROD,
        "rg-ops",
        "runbook",
        "Cleanup-Temp",
        {"account": "ops-automation", "runbookType": "Python3", "state": "Published", "description": ""},
    ),
    (DEV, "rg-dev", "logic", "invoice-intake-dev", {"state": "Enabled", "tags": {"environment": "dev"}}),
    (DEV, "rg-dev", "site", "test-func", {"kind": "functionapp", "state": "Running", "tags": {"env": "test"}}),
    (LEGACY, "rg-old", "logic", "legacy-ftp-import", {"state": "Enabled", "tags": {}}),
]
PROVIDER = {
    "logic": "Microsoft.Logic/workflows",
    "site": "Microsoft.Web/sites",
    "account": "Microsoft.Automation/automationAccounts",
}
LIST = re.compile(r"^/subscriptions/([0-9a-f-]{36})/providers/(Microsoft\.[A-Za-z]+/[A-Za-z]+)$")
SUBSCRIPTION = re.compile(r"^/subscriptions/([0-9a-f-]{36})$")
RUNBOOKS = re.compile(
    r"^/subscriptions/([0-9a-f-]{36})/resourceGroups/([^/]+)/providers/Microsoft\.Automation/automationAccounts/"
    r"([^/]+)/runbooks$"
)
# A secret inside a Logic App's definition and a site's settings: the source must never keep either.
DEFINITION_SECRET = "logic-definition-secret-9f2"  # noqa: S105 (a synthetic value)


def resource_id(sub: str, rg: str, kind: str, name: str, account: str = "") -> str:
    if kind == "runbook":
        return f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Automation/automationAccounts/{account}/runbooks/{name}"
    return f"/subscriptions/{sub}/resourceGroups/{rg}/providers/{PROVIDER[kind]}/{name}"


def _resource(sub: str, rg: str, kind: str, name: str, extra: dict[str, Any]) -> dict[str, Any]:
    rid = resource_id(sub, rg, kind, name, extra.get("account", ""))
    common: dict[str, Any] = {"id": rid, "name": name, "location": "westeurope", "tags": dict(extra.get("tags") or {})}
    if kind == "logic":
        return {
            **common,
            "type": "Microsoft.Logic/workflows",
            "identity": {"type": "SystemAssigned", "principalId": "0c0c0c0c-0000-4000-8000-0000000000cc"},
            "properties": {
                "state": extra.get("state", "Enabled"),
                "createdTime": "2025-01-01T00:00:00Z",
                "changedTime": "2026-10-01T00:00:00Z",
                "definition": {"parameters": {"apiKey": {"defaultValue": DEFINITION_SECRET}}},
            },
        }
    if kind == "site":
        return {
            **common,
            "type": "Microsoft.Web/sites",
            "kind": extra.get("kind", "app"),
            "properties": {
                "state": extra.get("state", "Running"),
                "defaultHostName": f"{name}.azurewebsites.net",
                "enabled": True,
                "siteConfig": {"appSettings": [{"name": "API_KEY", "value": DEFINITION_SECRET}]},
            },
        }
    if kind == "account":
        return {**common, "type": "Microsoft.Automation/AutomationAccounts", "properties": {"state": "Ok"}}
    return {
        **common,
        "type": "Microsoft.Automation/AutomationAccounts/Runbooks",
        "properties": {
            "runbookType": extra.get("runbookType", "PowerShell"),
            "state": extra.get("state", "Published"),
            "description": extra.get("description", ""),
            "lastModifiedTime": "2026-10-01T00:00:00Z",
        },
    }


def tenant(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every resource after the config's changes: [{"sub", "rg", "kind", "account", "resource"}]."""
    rows: list[list[Any]] = [[sub, rg, kind, name, copy.deepcopy(extra)] for sub, rg, kind, name, extra in BASE]
    for change in config.get("simulator_changes") or []:
        op, name, value = change.get("op"), change.get("id"), change.get("value")
        target = next((r for r in rows if r[3] == name), None)
        if op == "add":
            value = value or {}
            kind = value.get("type", "logic")
            sub = value.get("sub", PROD)
            extra = {k: v for k, v in value.items() if k not in ("type", "sub")}
            if kind == "runbook":
                extra.setdefault("account", "ops-automation")
            rows.append([sub, value.get("rg", "rg-new"), kind, name, extra])
        elif target is None:
            continue
        elif op == "state":
            target[4]["state"] = value
        elif op == "tag":
            target[4]["tags"] = {**(target[4].get("tags") or {}), **value}
        elif op == "kind":
            target[4]["kind"] = value
        elif op == "rename":
            target[3] = value
        elif op == "remove":
            rows.remove(target)
    return [
        {
            "sub": sub,
            "rg": rg,
            "kind": kind,
            "account": extra.get("account", ""),
            "resource": _resource(sub, rg, kind, name, extra),
        }
        for sub, rg, kind, name, extra in rows
    ]


class Transport(MicrosoftSimulator):
    """Azure Resource Manager (and the token endpoint) from the synthetic tenant. `calls` records (method, url)."""

    def denied(self) -> list[str]:
        return [str(d).lower() for d in self.config.get("simulated_denied") or []]

    def _authorization_failed(self, action: str, scope: str) -> Reply:
        return error(
            403,
            "AuthorizationFailed",
            f"The client '{CLIENT_ID}' with object id '{OBJECT_ID}' does not have authorization to perform action "
            f"'{action}' over scope '{scope}' or the scope is invalid. If access was recently granted, please refresh "
            "your credentials.",
        )

    def answer(self, host: str, path: str, query: dict[str, list[str]], headers: Mapping[str, str]) -> Reply:
        version = (query.get("api-version") or [""])[0]
        denied = self.denied()
        visible = [s for s in SUBSCRIPTIONS if s[0] not in denied and not self.config.get("simulated_no_role")]
        if path == "/subscriptions":
            if version != API["subscriptions"]:
                return error(400, "InvalidApiVersionParameter", "The api-version is invalid.")
            return self.page(host, path, query, [self._subscription(*s) for s in visible], "nextLink")
        found = SUBSCRIPTION.match(path)
        if found:
            if version != API["subscriptions"]:
                return error(400, "InvalidApiVersionParameter", "The api-version is invalid.")
            sub = next((s for s in visible if s[0] == found.group(1)), None)
            if sub is None and self.config.get("simulated_not_found"):  # how Microsoft Learn says ARM answers
                return error(404, "SubscriptionNotFound", f"The subscription '{found.group(1)}' could not be found.")
            if sub is None:
                return self._authorization_failed(
                    "Microsoft.Resources/subscriptions/read", f"/subscriptions/{found.group(1)}"
                )
            return json_reply(200, self._subscription(*sub))
        found = LIST.match(path)
        runbooks = RUNBOOKS.match(path)
        if not found and not runbooks:
            return error(404, "InvalidResourceType", "The resource type could not be found.")
        sub_id = (found or runbooks).group(1)  # type: ignore[union-attr]
        provider = found.group(2) if found else "runbooks"
        if version != API.get(provider):
            return error(400, "InvalidApiVersionParameter", "The api-version is invalid.")
        if sub_id not in {s[0] for s in SUBSCRIPTIONS}:
            return error(404, "SubscriptionNotFound", "The subscription could not be found.")
        type_ = "Microsoft.Automation/automationAccounts/runbooks" if runbooks else provider
        if sub_id in denied or f"{sub_id}/{type_.lower()}" in denied or self.config.get("simulated_no_role"):
            return self._authorization_failed(f"{type_}/read", f"/subscriptions/{sub_id}")
        unregistered = [str(u).lower() for u in self.config.get("simulated_unregistered") or []]
        if f"{sub_id}/{type_.lower()}" in unregistered:
            namespace = type_.split("/")[0]
            return error(
                409,
                "MissingSubscriptionRegistration",
                f"The subscription is not registered to use namespace '{namespace}'.",
            )
        rows = tenant(self.config)
        if runbooks:
            items = [
                r["resource"]
                for r in rows
                if r["kind"] == "runbook" and r["sub"] == sub_id and r["account"].lower() == runbooks.group(3).lower()
            ]
        else:
            kind = next(k for k, p in PROVIDER.items() if p == provider)
            items = [r["resource"] for r in rows if r["kind"] == kind and r["sub"] == sub_id]
        return self.page(host, path, query, items, "nextLink")

    @staticmethod
    def _subscription(sub: str, name: str, state: str) -> dict[str, Any]:
        return {
            "id": f"/subscriptions/{sub}",
            "subscriptionId": sub,
            "displayName": name,
            "state": state,
            "tenantId": TENANT,
            "authorizationSource": "RoleBased",
            "tags": {},
        }
