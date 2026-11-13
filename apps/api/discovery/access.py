"""Access checks for a discovery connector. A check reads only and imports nothing: it signs in with the connector's connection and makes one one-item
read per permission the source uses, so it says exactly which permission is missing.

Steps, in order, each {group, name, permission, required, state, problem, problem_label, code, detail, next}:

- credentials: the connection is chosen, on and has a secret; then the service issues a token (a Microsoft token is
  classified by AADSTS code as the connection test does, core/connections.classify_microsoft; Halo's by its OAuth
  error). A failure here stops the check.
- api: one read per probe. A refusal that means a missing grant is "missing permission" naming it: Graph's 403
  `Authorization_RequestDenied`; ARM's 403 `AuthorizationFailed`, with the action and scope Microsoft named and the fix
  (Reader on that subscription); Dataverse's 401 or 403 (no application user, or a role without the privilege, named
  when Dataverse names it); the Power Platform admin API's 403 (not registered); Halo's 403 (the API application's
  scope). Otherwise a 401 is the token not being accepted (an authentication problem, after one fresh sign-in): it
  fails the check on a required read and is a `warning` on an optional one. An optional permission
  (User.ReadBasic.All, AgentIdentity.Read.All) that isn't granted is `optional_missing`; any other failure of an
  optional read (401, 404, 429, 5xx, ...) is `warning` with the detail; neither fails the check. `missing` lists
  permission names only: a sign-in refused for missing consent names none, so it adds nothing there. A probe whose
  permission the connector's scope doesn't use (agent identities while `include_agent_identities` is off, cloud flows
  while `include_cloud_flows` is off) is `not_used` and isn't called; reads past a check's cap (more than 10
  subscriptions or environments) are `not_checked`, and a run reports each of them.

Graph sources (entra, entra_licenses) declare fixed PROBES; azure, powerplatform and halo build their steps from the
connector's scope (`access_steps(connector, scope, session)`) with the helpers here, signing in once per audience
(Azure Resource Manager, each Dataverse environment, the Power Platform admin API, Halo).

The source's capability is checked first (`require_capability`, 403 problem). `ok` is true when every required step
passed. The result is stored on the connector (`access_check`, `access_checked_at`, `access_checked_by`) and audited
as `connector.access_checked` with the permissions missing, never a token, a secret or the service's message text. The
calls run outside any transaction; only the write is atomic. A check doesn't need the connection to have passed its
test (it signs in itself), so an Admin can use it while setting up."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from catalog import services
from core import connections, credentials, simulation
from core.capabilities import SOURCE_CAPABILITY, require_capability

from .graph_source import (
    GRAPH_AUDIENCE,
    Client,
    Permission,
    Probe,
    Refused,
    Session,
    SignInRefused,
    SourceError,
    graph_transport,
    reader_advice,
    subscription_of,
    transports,
)
from .models import Connector

BUDGET_SECONDS = 60.0  # a check waits on throttling for a minute at most
DEADLINE_SECONDS = 180.0  # and takes three minutes at most, requests included
PROBE_CAP = 10  # subscriptions or environments probed by one check; the rest are not_checked

STATES = ("ok", "failed", "optional_missing", "warning", "not_used", "not_checked", "skipped")
PASSING = ("ok", "optional_missing", "warning", "not_used", "not_checked")


def step(group, name, state, *, permission=None, problem="", code="", detail="", next_step="") -> dict[str, Any]:
    return {
        "group": group,
        "name": name,
        "permission": permission.name if permission else "",
        "required": bool(permission.required) if permission else True,
        "state": state,
        "problem": problem,
        "problem_label": connections.PROBLEMS.get(problem, ""),
        "code": code,
        "detail": detail[:300],
        "next": next_step[:300],
    }


def _grant_advice(permission: Permission) -> str:
    if permission.advice:
        return permission.advice
    return (
        f"In the app registration, add the Microsoft Graph application permission {permission.name} (Application, not "
        "Delegated), grant admin consent for the tenant, wait a few minutes and check again."
    )


def _reading(name: str) -> str:
    """ "Read app registrations" -> "app registrations"."""
    return name.removeprefix("Read ")


def _missing(client: Client, e: Refused, perm: Permission) -> tuple[str, str]:
    """(detail, next) for a refusal that means `perm` is missing, in the audience's terms."""
    label, tag = client.audience.label, f"{e.status} {e.code}".strip()
    kind = client.audience.kind
    if kind == "graph":
        return f"{label} refused the read ({tag}): the app lacks {perm.name}.", _grant_advice(perm)
    if kind == "arm":
        sub = subscription_of(e.resource_scope)
        if e.code == "SubscriptionNotFound":
            return (
                f"{label} refused the read ({tag}): no Reader on this subscription, or the ID is wrong.",
                reader_advice(sub),
            )
        lacks = f"the app lacks {e.action} over {e.resource_scope}" if e.action else "the app has no role allowing it"
        return f"{label} refused the read ({tag}): {lacks}.", reader_advice(sub)
    if kind == "dataverse":
        named = f", missing privilege {e.privilege}" if e.privilege else ""
        return (
            f"{label} refused the read ({tag}{named}): the app needs an application user here whose security role "
            f"can read {perm.name.removeprefix('Read ')} ({perm.role_id or perm.name}).",
            _grant_advice(perm),
        )
    return f"{label} refused the read ({tag}).", _grant_advice(perm)


def probe(client: Client, p: Probe, headers=None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """One one-item read as a step, and the answer's JSON when it worked (a source may read the first page: the
    subscriptions or environments it then probes)."""
    perm = p.permission
    label = client.audience.label
    try:
        body = client.get(p.url, p.name[0].lower() + p.name[1:], perm, headers)
    except Refused as e:
        if e.missing:
            detail, advice = _missing(client, e, perm)
            if not perm.required:
                return step(
                    "api",
                    p.name,
                    "optional_missing",
                    permission=perm,
                    problem="missing_permission",
                    code=e.code,
                    detail=f"Optional, not granted: {perm.name}. {perm.why}",
                    next_step=advice,
                ), None
            return step(
                "api",
                p.name,
                "failed",
                permission=perm,
                problem="missing_permission",
                code=e.code,
                detail=detail.replace("  ", " "),
                next_step=advice,
            ), None
        if e.status == 401:
            return step(
                "api",
                p.name,
                "failed" if perm.required else "warning",
                permission=perm,
                problem="wrong_credentials",
                code=e.code,
                detail=str(e),
                next_step="Test the connection; if it passes, check the app is enabled in the tenant, then check again.",
            ), None
        if e.status == 404:
            problem, advice = "unavailable", ""
        elif e.status in (429, 500, 502, 503, 504):
            problem, advice = "unreachable", f"{label} is busy. Try again in a few minutes."
        else:
            problem, advice = "error", ""
        state = "failed" if perm.required else "warning"
        return step(
            "api", p.name, state, permission=perm, problem=problem, code=e.code, detail=str(e), next_step=advice
        ), None
    except SourceError as e:
        return step(
            "api",
            p.name,
            "failed" if perm.required else "warning",
            permission=perm,
            problem="unreachable",
            detail=str(e),
            next_step="Check the server's outbound internet access and try again.",
        ), None
    return step(
        "api", p.name, "ok", permission=perm, detail=f"Reading {_reading(p.name)} works ({perm.name} or broader)."
    ), body


def sign_in(client: Client) -> dict[str, Any]:
    """The token for the client's audience, as a step named after it ("Sign in to Azure Resource Manager")."""
    name = f"Sign in to {client.audience.label}"
    try:
        client.token()
    except SignInRefused as e:
        return step("credentials", name, "failed", problem=e.problem, code=e.code, detail=str(e))
    except SourceError as e:
        return step("credentials", name, "failed", problem="error", detail=str(e))
    accepted = "Halo accepted" if client.audience.kind == "halo" else "The tenant accepted"
    return step("credentials", name, "ok", detail=f"{accepted} the credentials.")


def skipped(name: str, permission: Permission | None = None, why: str = "sign-in failed") -> dict[str, Any]:
    return step("api", name, "skipped", permission=permission, detail=f"Not checked: {why}.")


def not_used(name: str, permission: Permission, turn_on: str) -> dict[str, Any]:
    return step(
        "api",
        name,
        "not_used",
        permission=permission,
        detail=f"Not checked: the connector's scope doesn't use {permission.name} (turn on {turn_on}).",
    )


def not_checked(name: str, count: int, what: str) -> dict[str, Any]:
    return step(
        "api",
        name,
        "not_checked",
        detail=f"Not checked here: {count} more {what}. A run reports each one it can't read.",
    )


def _credential_steps(conn) -> tuple[Any, list[dict[str, Any]]]:
    """The connection's sign-in values (Microsoft or Halo) with its decrypted secret, and the Connection step."""
    if conn is None:
        return None, [
            step(
                "credentials",
                "Connection",
                "failed",
                problem="not_configured",
                detail="No connection is chosen for this connector.",
                next_step="Choose a connection in the connector's settings.",
            )
        ]
    if not conn.enabled:
        return None, [
            step(
                "credentials",
                "Connection",
                "failed",
                problem="disabled",
                detail=f"The connection “{conn.name}” is turned off.",
                next_step="Turn it on, or choose another connection.",
            )
        ]
    if connections.is_simulated(conn) and not simulation.allowed():
        return None, [
            step(
                "credentials",
                "Connection",
                "failed",
                problem="simulation_off",
                detail=f"The connection “{conn.name}” is simulated, and this server doesn't allow simulated tenants "
                "and hosts.",
                next_step=simulation.NEXT,
            )
        ]
    if not conn.secret_ciphertext:
        return None, [
            step(
                "credentials",
                "Connection",
                "failed",
                problem="not_configured",
                detail=f"The connection “{conn.name}” has no {connections.kind_of(conn).secret_label}.",
                next_step=f"Enter the {connections.kind_of(conn).secret_label} on the connection.",
            )
        ]
    try:
        secret = credentials.slot_value(conn, "secret")
    except credentials.CredentialError as e:
        return None, [
            step(
                "credentials",
                "Connection",
                "failed",
                problem="not_configured",
                detail=str(e),
                next_step=f"Enter the {connections.kind_of(conn).secret_label} on the connection again.",
            )
        ]
    credential = connections.credential_for(conn, secret)
    # Only the connection's name: the step is shown to every role (GET connectors/{id}), and the tenant and client IDs
    # are on the connection, which only Admins read.
    return credential, [step("credentials", "Connection", "ok", detail=f"Uses the connection “{conn.name}”.")]


def _graph_steps(connector, source, scope, credential, transport) -> list[dict[str, Any]]:
    """The fixed-probe Graph sources (entra, entra_licenses)."""
    try:
        client = Client(
            graph_transport(connector, credential, transport),
            credential,
            budget_seconds=BUDGET_SECONDS,
            deadline=DEADLINE_SECONDS,
            audience=GRAPH_AUDIENCE,
        )
    except SourceError as e:
        steps = [step("credentials", "Sign in to Microsoft Graph", "failed", problem="not_configured", detail=str(e))]
    else:
        steps = [sign_in(client)]
        if steps[-1]["state"] == "ok":
            return steps + [
                probe(client, p)[0] if not p.when or scope.get(p.when) else not_used(p.name, p.permission, p.when)
                for p in source.PROBES
            ]
    return steps + [skipped(p.name, p.permission) for p in source.PROBES]


def evaluate(connector, transport=None) -> dict[str, Any]:
    """The check's result (no write). `transport` replaces the connection's (tests)."""
    from .engine import connection_of, module

    require_capability(connector.organization, SOURCE_CAPABILITY[connector.source])
    source = module(connector)
    scope = dict(source.SCOPE_DEFAULTS) | dict(connector.scope or {})
    credential, steps = _credential_steps(connection_of(connector))
    if credential is not None and hasattr(source, "check_steps"):
        # Okta and Google Workspace sign in and read through their own client (discovery/rest_source.py).
        steps.extend(source.check_steps(connector, scope, credential, transport))
    elif credential is not None and hasattr(source, "access_steps"):
        try:
            session = Session(
                transports(connector, credential, source.simulator, transport),
                credential,
                budget_seconds=BUDGET_SECONDS,
                deadline=DEADLINE_SECONDS,
            )
            steps.extend(source.access_steps(connector, scope, session))
        except SourceError as e:  # the tenant ID isn't valid: nothing was called
            steps.append(step("credentials", "Sign in", "failed", problem="not_configured", detail=str(e)))
    elif credential is not None:
        steps.extend(_graph_steps(connector, source, scope, credential, transport))
    elif not hasattr(source, "access_steps") and not hasattr(source, "check_steps"):
        steps.extend(skipped(p.name, p.permission) for p in source.PROBES)
    ok = all(s["state"] in PASSING for s in steps)
    return {
        "ok": ok,
        "missing": sorted(
            {
                s["permission"]
                for s in steps
                if s["state"] == "failed" and s["problem"] == "missing_permission" and s["permission"]
            }
        ),
        "optional_missing": sorted({s["permission"] for s in steps if s["state"] == "optional_missing"}),
        "steps": steps,
    }


def check_access(org, actor, connector, transport=None) -> Connector:
    """Run the check outside any transaction, then store and audit the result (Admin; core/drf on the route). 403
    problem, naming it, when the organization lacks the source's capability."""
    require_capability(org, SOURCE_CAPABILITY[connector.source])
    result = evaluate(connector, transport)
    with transaction.atomic():
        row = Connector.objects.select_for_update().filter(organization=org, pk=connector.pk).first()
        if row is None:
            from django.http import Http404

            raise Http404
        row.access_check = result
        row.access_checked_at = timezone.now()
        row.access_checked_by = actor
        row.save(update_fields=["access_check", "access_checked_at", "access_checked_by", "updated_at"])
        services.audit(
            org,
            actor,
            "connector.access_checked",
            row,
            {"ok": result["ok"], "missing": result["missing"], "optional_missing": result["optional_missing"]},
        )
    return row
