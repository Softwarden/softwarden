"""A synthetic Power Platform tenant for the powerplatform source (discovery/powerplatform.py): two Dataverse
environments answering `GET /api/data/v9.2/bots` and `/workflows` (category 5, modern flows) and the Power Platform
admin API's environment list, as Microsoft Learn documents them.
Selected by the tenant ID `simulated`; tokens and the shared checks are discovery/simulator_base.py's (a token is
issued per environment host, and a token for one environment is refused by another).

Environments: Contoso (https://contoso.crm.dynamics.com, the default environment) with three agents (one deleted,
componentstate 2) and two cloud flows plus a classic workflow (category 0, never asked for); Contoso Dev
(https://contoso-dev.crm4.dynamics.com, a sandbox) with one agent; and, in the admin API's list only, a Teams
environment with no Dataverse database.

Config keys (tests only), besides simulator_base's:

- `simulator_changes`: edits in order, {"op", "id" (agent or flow name), "value"}: rename, remove, state (statecode),
  add (value {"host", "type": "bot"|"flow"}).
- `simulated_app_users`: the hosts where the app has an application user (default: both). Elsewhere every read is
  401, as Dataverse answers a caller that isn't a user of the environment.
- `simulated_privileges`: {"host": ["prvReadbot", "prvReadWorkflow"]} (default: both everywhere). A table read
  without its privilege is 403 with a message naming the privilege (the wording is UNVERIFIED on Learn).
- `simulated_bap_registered`: the app is registered as a Power Platform management app (default True); off, the
  admin API answers 403.
- `simulated_foreign_instance`: the admin API lists an environment whose instance URL is on another host.
- `simulated_no_instances`: the admin API lists only environments without a Dataverse database.
- `simulated_ignore_select`: rows come back with every column (a flow's `clientdata` included), whatever `$select`
  asked for.
- `simulated_bap_status`: the admin API answers this status (401 or 403) when not registered (default 403).
- `simulated_unreachable`: hosts whose reads fail as if the connection broke (no answer).
- `simulated_no_ids`: an environment's first agent comes back without a `botid`."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping
from typing import Any

from core.graph import Reply, Unreachable

from .simulator_base import MicrosoftSimulator, error

CONTOSO = "contoso.crm.dynamics.com"
CONTOSO_DEV = "contoso-dev.crm4.dynamics.com"
HOSTS = (CONTOSO, CONTOSO_DEV)
ENVIRONMENTS = [
    # (environment id, display name, sku, host or "")
    ("Default-00000000-0000-4000-8000-00000000c0de", "Contoso (default)", "Default", CONTOSO),
    ("6d1a2b3c-0000-4000-8000-0000000000d1", "Contoso Dev", "Sandbox", CONTOSO_DEV),
    ("7e2b3c4d-0000-4000-8000-0000000000e2", "Contoso Teams", "Teams", ""),
]
PRIVILEGES = {"bots": "prvReadbot", "workflows": "prvReadWorkflow"}
# (host, type, name, extra)
BASE: list[tuple[str, str, str, dict[str, Any]]] = [
    (CONTOSO, "bot", "HR Helpdesk", {"schemaname": "cr6f1_hrHelpdesk", "authenticationmode": 2, "statecode": 0}),
    (CONTOSO, "bot", "IT Service Desk", {"schemaname": "cr6f1_itServiceDesk", "authenticationmode": 1, "statecode": 0}),
    (CONTOSO, "bot", "Old FAQ bot", {"schemaname": "cr6f1_oldFaq", "componentstate": 2, "statecode": 1}),
    (CONTOSO_DEV, "bot", "Benefits Pilot", {"schemaname": "cr7a2_benefitsPilot", "authenticationmode": 3}),
    (CONTOSO, "flow", "Approve expenses", {"statecode": 1, "description": "Routes expense claims for approval."}),
    (CONTOSO, "flow", "Sync contacts", {"statecode": 0, "description": ""}),
    (CONTOSO, "flow", "Classic workflow", {"statecode": 1, "category": 0}),
]


def _id(host: str, name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"simulated-dataverse/{host}/{name}"))


def tenant(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every agent and workflow after the config's changes: [{"host", "type", "row"}]."""
    rows: list[list[Any]] = [
        [host, kind, name, copy.deepcopy(extra), _id(host, name)] for host, kind, name, extra in BASE
    ]
    for change in config.get("simulator_changes") or []:
        op, name, value = change.get("op"), change.get("id"), change.get("value")
        target = next((r for r in rows if r[2] == name), None)
        if op == "add":
            value = value or {}
            host = value.get("host", CONTOSO)
            rows.append([host, value.get("type", "bot"), name, {}, _id(host, name)])
        elif target is None:
            continue
        elif op == "rename":
            target[2] = value  # the row keeps its ID: Dataverse renames in place
        elif op == "remove":
            rows.remove(target)
        elif op == "state":
            target[3]["statecode"] = value
    out = []
    for host, kind, name, extra, row_id in rows:
        if kind == "bot":
            row = {
                "botid": row_id,
                "name": name,
                "schemaname": extra.get("schemaname", f"cr000_{name.replace(' ', '')}"),
                "statecode": extra.get("statecode", 0),
                "statuscode": 1,
                "authenticationmode": extra.get("authenticationmode", 2),
                "accesscontrolpolicy": 0,
                "language": 1033,
                "publishedon": "2026-09-01T10:00:00Z",
                "createdon": "2026-01-01T10:00:00Z",
                "modifiedon": "2026-10-01T10:00:00Z",
                "componentstate": extra.get("componentstate", 0),
                "_ownerid_value": _id(host, "owner"),
                "_ownerid_value@OData.Community.Display.V1.FormattedValue": "Priya Raman",
            }
        else:
            row = {
                "workflowid": row_id,
                "name": name,
                "category": extra.get("category", 5),
                "statecode": extra.get("statecode", 1),
                "description": extra.get("description", ""),
                "modifiedon": "2026-10-01T10:00:00Z",
                "_ownerid_value": _id(host, "owner"),
                "_ownerid_value@OData.Community.Display.V1.FormattedValue": "Lee Chen",
                "clientdata": '{"properties": {"connectionReferences": {"secret": "flow-client-data-secret"}}}',
            }
        out.append({"host": host, "type": kind, "row": row})
    return out


class Transport(MicrosoftSimulator):
    """Dataverse environments, the Power Platform admin API and the token endpoint. `calls` records (method, url)."""

    def dataverse_hosts(self) -> set[str]:
        return set(HOSTS)

    def answer(self, host: str, path: str, query: dict[str, list[str]], headers: Mapping[str, str]) -> Reply:
        if host == "api.bap.microsoft.com":
            return self._environments(host, path, query)
        if host in (self.config.get("simulated_unreachable") or []):
            raise Unreachable("ConnectionResetError", ambiguous=True)
        users = self.config.get("simulated_app_users", list(HOSTS))
        if host not in users:
            return error(401, "", "The user is not a member of the organization.")
        table = path.removeprefix("/api/data/v9.2/")
        if table not in PRIVILEGES:
            return error(404, "0x8006088a", "Resource not found for the segment.")
        granted = (self.config.get("simulated_privileges") or {}).get(host, list(PRIVILEGES.values()))
        if PRIVILEGES[table] not in granted:
            return error(
                403,
                "0x80040220",
                f"Principal user (Id=00000000-0000-0000-0000-000000000001, type=8) is missing {PRIVILEGES[table]} "
                "privilege (Id=00000000-0000-0000-0000-000000000002) on OTC=10185.",
            )
        kind = "bot" if table == "bots" else "flow"
        rows = [r["row"] for r in tenant(self.config) if r["host"] == host and r["type"] == kind]
        if kind == "bot" and rows and self.config.get("simulated_no_ids"):
            rows[0] = {**rows[0], "botid": None}
        if kind == "flow":
            wanted = (query.get("$filter") or [""])[0].replace(" ", "")
            if wanted != "category" + "eq5":
                return error(400, "0x80060888", "The simulator only lists modern flows (category eq 5).")
            rows = [r for r in rows if r["category"] == 5]
        select = [s for s in (query.get("$select") or [""])[0].split(",") if s]
        annotated = "FormattedValue" in headers.get("Prefer", "")
        if select and not self.config.get("simulated_ignore_select"):
            annotations = tuple(f"{s}@" for s in select) if annotated else ()
            rows = [
                {k: v for k, v in r.items() if k in select or (annotations and k.startswith(annotations))} for r in rows
            ]
        return self.page(host, path, query, rows, "@odata.nextLink", default=500)

    def _environments(self, host: str, path: str, query: dict[str, list[str]]) -> Reply:
        if path != "/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments":
            return error(404, "NotFound", "Not found.")
        if (query.get("api-version") or [""])[0] != "2020-10-01":
            return error(400, "InvalidApiVersionParameter", "The api-version is invalid.")
        if not self.config.get("simulated_bap_registered", True):
            status = int(self.config.get("simulated_bap_status", 403))
            return error(status, "Forbidden", "The service principal is not registered for the admin API.")
        envs: list[dict[str, Any]] = [
            {
                "id": f"/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments/{env_id}",
                "name": env_id,
                "properties": {
                    "displayName": name,
                    "environmentSku": sku,
                    **({"linkedEnvironmentMetadata": {"instanceUrl": f"https://{env_host}/"}} if env_host else {}),
                },
            }
            for env_id, name, sku, env_host in ENVIRONMENTS
        ]
        if self.config.get("simulated_no_instances"):
            for env in envs:
                env["properties"].pop("linkedEnvironmentMetadata", None)
        if self.config.get("simulated_foreign_instance"):
            envs.append(
                {
                    "name": "8f3c4d5e-0000-4000-8000-0000000000f3",
                    "properties": {
                        "displayName": "Elsewhere",
                        "environmentSku": "Production",
                        "linkedEnvironmentMetadata": {"instanceUrl": "https://contoso.crm.dynamics.com.evil.example/"},
                    },
                }
            )
        return self.page(host, path, query, envs, "nextLink")
