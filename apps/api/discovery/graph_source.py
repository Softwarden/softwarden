"""What the Microsoft discovery sources share (discovery/entra.py, entra_licenses.py, azure.py, powerplatform.py), and
the Halo source too (discovery/halo.py): a client that signs in with the connector's tested connection, reads pages
within a throttling budget (core/graph.Budget: Retry-After, else exponential backoff, with a wall-clock cap per run)
and turns every failure into a readable SourceError, and the one-item probes the access checks make
(discovery/access.py).

Audiences. One Microsoft connection gets a token per resource it reads: Microsoft Graph, Azure
Resource Manager, each Dataverse environment, the Power Platform admin API (`Audience`; scope `{resource}/.default`).
A token is only ever sent to its audience's hosts: every request, first page or next-page link, is checked against
them (https, port 443, no user info) before the token is attached, and the real transport for an audience is built
with only the sign-in host and those hosts. A Dataverse audience's host is the environment's own, validated as
`<name>.crm[N].dynamics.com`. Halo is an audience too, whose one origin is the Halo connection's address.

Nothing here writes to Microsoft or Halo: the one POST is the client-credentials token request, and every read is a
GET. Messages are Softwarden's own words with the HTTP status and the service's error code, never the service's message
text, a token or a secret, so they are safe on the run, in the log and in the API. The only things read out of a
message are identifiers matched against strict patterns: ARM's AuthorizationFailed action and scope, and the name of
the Dataverse privilege that is missing (`prvReadbot`)."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from core import connections, graph, halo, outbound, simulation

GRAPH = f"https://{graph.GRAPH_HOST}/v1.0"
SIMULATED = simulation.SIMULATED
# A run's wall-clock limit, requests and throttling waits together, checked before every request: a throttled or slow
# tenant can't hold its run past the lease (discovery/engine.RUN_LEASE, this plus a margin), after which the run is
# failed as stuck and another run can start.
RUN_DEADLINE = 40 * 60.0

# The run's progress hook (discovery/engine.py): called before every request, it renews the run's and its job's lease
# (at most once a minute) and raises core.jobs.LeaseLost once the run was taken over, which stops the fetch. Unset for
# access checks and tests that fetch directly.
_CHECKPOINT: ContextVar[Callable[[], None] | None] = ContextVar("discovery_checkpoint", default=None)
# The access tokens issued during the run (every audience), so the engine can redact them from anything it stores or
# logs, as defence in depth: no message is meant to carry one.
_ISSUED: ContextVar[list[str] | None] = ContextVar("discovery_issued_tokens", default=None)


@contextmanager
def checkpoint(func: Callable[[], None], issued: list[str] | None = None) -> Iterator[None]:
    token = _CHECKPOINT.set(func)
    tokens = _ISSUED.set(issued)
    try:
        yield
    finally:
        _ISSUED.reset(tokens)
        _CHECKPOINT.reset(token)


def at_checkpoint() -> None:
    func = _CHECKPOINT.get()
    if func is not None:
        func()


def note_issued(token: str) -> None:
    """Record an access token issued during the run, so the engine redacts it from anything it stores or logs (the
    Okta and Google sources' client, discovery/rest_source.py, signs in on its own)."""
    issued = _ISSUED.get()
    if issued is not None:
        issued.append(token)


class SourceError(Exception):
    """A readable, secret-free reason a run failed."""


class DeadlineReached(SourceError):
    """The run's wall-clock limit passed: the whole run stops (a source that reads scope by scope doesn't go on)."""


class Refused(SourceError):
    """A read was answered with an error status. `status` and `code` (the service's error code) let a caller treat an
    optional permission's refusal as a note rather than a failure. `missing` is True when the refusal means a grant is
    missing (403; for Dataverse also 401 and its privilege errors). `action` and `resource_scope` are ARM's
    AuthorizationFailed action and scope when they could be read; `privilege` is the Dataverse privilege named."""

    def __init__(
        self,
        message: str,
        status: int,
        code: str,
        *,
        missing: bool | None = None,
        action: str = "",
        resource_scope: str = "",
        privilege: str = "",
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.missing = status == 403 if missing is None else missing
        self.action = action
        self.resource_scope = resource_scope
        self.privilege = privilege


class Throttled(SourceError):
    """A throttled answer (429 or 503) that the caller asked to handle itself (`Client.get(retry_throttled=False)`):
    `retry_after` is what the service asked for, if anything."""

    def __init__(self, message: str, status: int, retry_after: float | None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class SignInRefused(SourceError):
    """Microsoft (or Halo) didn't issue a token. `problem` is connections.PROBLEMS' key (wrong_credentials, disabled,
    missing_permission for consent, unreachable, unavailable, error) and `code` the AADSTS or OAuth code."""

    def __init__(self, message: str, problem: str, code: str = ""):
        super().__init__(message)
        self.problem = problem
        self.code = code


class Fetched(list):
    """A source's raw objects, plus `notes`: things the run should say without failing (an optional permission that
    isn't granted, so a part of the source wasn't read), and `unread`: the scopes (a subscription, an environment's
    agents) a source couldn't read this run. An observation whose `data["scope"]` is one of them, or inside one
    (`sub:X/account:Y` inside `sub:X`), keeps its state instead of becoming "no longer seen" (discovery/engine.py).
    `empty`: listings that answered with nothing at all, {scope: what the refusal calls it}; a run fails rather than
    lose the observations or discovered credentials that came from one (engine._guard_empty_listings)."""

    def __init__(self, items=(), notes=(), unread=(), empty=None):
        super().__init__(items)
        self.notes: list[str] = list(notes)
        self.unread: set[str] = set(unread)
        self.empty: dict[str, str] = dict(empty or {})


@dataclass(frozen=True)
class Permission:
    """A grant a source needs. `kind` says which sort: a Microsoft Graph application permission (`graph`), an Azure
    role (`azure_rbac`), a Dataverse privilege held through an application user's security role
    (`dataverse_privilege`), the Power Platform management app registration (`power_platform_registration`) or a Halo
    API scope (`halo_scope`). `role_id` is the app role or role definition ID admins see, empty until it is confirmed
    on Microsoft Learn; `verified` says whether the grant itself (that it is enough and the least privileged for the
    calls) was confirmed there (docs/PERMISSIONS.md). `advice` is how to grant it, shown when it's missing (Graph
    permissions use the standard admin-consent advice)."""

    name: str
    role_id: str
    required: bool
    why: str
    verified: bool
    kind: str = "graph"
    advice: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "role_id": self.role_id,
            "required": self.required,
            "why": self.why,
            "verified": self.verified,
        }


APPLICATION_READ_ALL = Permission(
    "Application.Read.All",
    "9a5d68dd-52b0-4cc2-bd40-abcf44ac3a30",
    True,
    "List app registrations and enterprise applications with their owners and credential expiry dates.",
    True,
)
USER_READBASIC_ALL = Permission(
    "User.ReadBasic.All",
    "",  # not yet confirmed on Microsoft Learn: shown once it is
    False,
    "Optional: show owners' names and emails. Without it owners are matched by object ID only (owner not resolved).",
    False,
)
AGENT_IDENTITY_READ_ALL = Permission(
    "AgentIdentity.Read.All",
    "",  # not looked up on Microsoft Learn yet
    False,
    "Optional: list Entra Agent ID agent identities (only when the connector's scope includes them).",
    True,
)
# Shadow IT (discovery/entra_shadow_it.py). Both verified on Microsoft Learn by the research
# (docs/PERMISSIONS.md): AuditLog.Read.All is the least-privileged application permission for GET /auditLogs/signIns,
# and Directory.Read.All for GET /oauth2PermissionGrants. Role IDs from Learn's permissions reference (2026-11-09);
# Directory.Read.All's isn't confirmed yet, so it stays empty.
AUDIT_LOG_READ_ALL = Permission(
    "AuditLog.Read.All",
    "b0afded3-3588-46d8-8b3d-9842eff778da",  # Learn permissions-reference, 2026-11-09
    True,
    "Read the sign-in logs (interactive sign-ins, last 30 days) to count each third-party app's users and sign-ins. "
    "Needs Microsoft Entra ID P1 or P2 in the tenant; turn off Read sign-in logs in the scope without it.",
    True,
)
DIRECTORY_READ_ALL = Permission(
    "Directory.Read.All",
    "",
    True,
    "Read OAuth consent grants (who consented to which app, with which scopes), the enterprise applications they "
    "name, and the tenant's own ID.",
    True,
)
# SSO and MFA coverage (discovery/entra_coverage.py). Both verified on Microsoft Learn by the research
# (docs/PERMISSIONS.md): Policy.Read.All for GET /identity/conditionalAccess/policies (the tenant needs Entra ID P1),
# Synchronization.Read.All for GET /servicePrincipals/{id}/synchronization/jobs (never Application.ReadWrite.OwnedBy).
# Required while the scope reads them; a run without either still reports single sign-on and says what it couldn't
# read. Role IDs from Learn's permissions reference (2026-11-09).
POLICY_READ_ALL = Permission(
    "Policy.Read.All",
    "246dd0d5-5bd0-4def-940b-0421030a5b68",  # Learn permissions-reference, 2026-11-09
    True,
    "Read conditional access policies, to tell which apps require multifactor authentication and for whom. Needs "
    "Microsoft Entra ID P1 in the tenant; without it the run still reports single sign-on and provisioning.",
    True,
)
SYNCHRONIZATION_READ_ALL = Permission(
    "Synchronization.Read.All",
    "5ba43d2f-fa88-4db2-bd1c-a67c5f0fb1ce",  # Learn permissions-reference, 2026-11-09
    True,
    "Read each linked enterprise application's provisioning (SCIM) jobs, to tell whether provisioning is on.",
    True,
)
LICENSE_ASSIGNMENT_READ_ALL = Permission(
    "LicenseAssignment.Read.All",
    "e2f98668-2877-4f38-a2f4-8202e0717aa1",  # Learn permissions-reference, 2026-11-09
    True,
    "Read the tenant's subscribed SKUs: seats enabled, consumed, in warning and suspended.",
    True,
)
# Licence waste (discovery/entra_licence_usage.py). Verified on Microsoft Learn by the research
# (docs/PERMISSIONS.md, "Discovery source: entra_licence_usage"): GET /users with $select=assignedLicenses needs
# User.Read.All (User.ReadBasic.All doesn't return it; licenseDetails has no application permission), and
# signInActivity needs AuditLog.Read.All as well, and Microsoft Entra ID P1 in the tenant. Both are tenant-wide admin
# consents. AuditLog.Read.All is optional here: without it (or without P1) a run still counts the licences and says
# the last sign-in is unknown. Role IDs from Learn's permissions reference (2026-11-09).
USER_READ_ALL = Permission(
    "User.Read.All",
    "df021288-bdef-4463-88db-98f22de89214",  # Learn permissions-reference, 2026-11-09
    True,
    "Read every user's assigned licenses, whether the account is enabled and whether it is a member or a guest. "
    "Names and email addresses are read only to link a user to a person here and are never stored.",
    True,
)
AUDIT_LOG_SIGN_IN_ACTIVITY = Permission(
    "AuditLog.Read.All",
    "b0afded3-3588-46d8-8b3d-9842eff778da",  # Learn permissions-reference, 2026-11-09
    False,
    "Optional: read each licensed user's last successful sign-in (signInActivity), to tell active from inactive "
    "licenses. Needs Microsoft Entra ID P1 in the tenant; without it the last sign-in shows as unknown.",
    True,
)


# ---- audiences ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Audience:
    """A resource a connection gets tokens for. `scope` is the client-credentials scope (`{resource}/.default`; empty
    for Halo, whose scope is the connection's), `origins` the (host, port) pairs a token for it may be sent to, and
    `label` the service's name in messages ("Azure Resource Manager refused to ...")."""

    key: str
    label: str
    kind: str  # graph, arm, dataverse, bap, halo
    scope: str
    origins: frozenset[tuple[str, int]]

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset(h for h, _ in self.origins)

    def allows(self, url: str) -> bool:
        """`url` is https on one of this audience's origins, with no user name or password."""
        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port or 443
        except ValueError:
            return False
        host = (parts.hostname or "").lower()
        return (
            parts.scheme == "https"
            and parts.username is None
            and parts.password is None
            and (host, port) in self.origins
        )


def _origins(*hosts: str) -> frozenset[tuple[str, int]]:
    return frozenset((h, 443) for h in hosts)


ARM_HOST = "management.azure.com"
BAP_HOST = "api.bap.microsoft.com"
GRAPH_AUDIENCE = Audience("graph", "Microsoft Graph", "graph", graph.GRAPH_SCOPE, _origins(graph.GRAPH_HOST))
ARM_AUDIENCE = Audience("arm", "Azure Resource Manager", "arm", f"https://{ARM_HOST}/.default", _origins(ARM_HOST))
# The legacy Power Platform admin API (BAP) takes tokens for the PowerApps service, https://service.powerapps.com/.
BAP_AUDIENCE = Audience(
    "bap", "Power Platform admin API", "bap", "https://service.powerapps.com/.default", _origins(BAP_HOST)
)
# A Dataverse environment's host in the global cloud: <name>.crm.dynamics.com or a regional crmN (crm4, crm11, ...).
DATAVERSE_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.crm[0-9]{0,2}\.dynamics\.com$")


def dataverse_host(url: Any) -> str:
    """The environment host of a Dataverse URL (`https://contoso.crm.dynamics.com`, a trailing slash allowed), or ""
    when it isn't one: https only, port 443, no path, user info, query or fragment, a `*.crm[N].dynamics.com` host."""
    if not isinstance(url, str) or len(url) > 300:
        return ""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or not DATAVERSE_HOST.fullmatch(host)
    ):
        return ""
    return host


def dataverse(host: str) -> Audience:
    """The audience of one Dataverse environment: its token is for that environment only and goes nowhere else."""
    if not DATAVERSE_HOST.fullmatch(host):
        raise SourceError("That isn't a Dataverse environment address.")
    return Audience(f"dataverse:{host}", f"Dataverse ({host})", "dataverse", f"https://{host}/.default", _origins(host))


def halo_audience(base_url: str) -> Audience:
    """Halo's one origin: the connection's host and port (an on-premises Halo may use another port)."""
    parts = urllib.parse.urlsplit(base_url)
    return Audience("halo", "Halo", "halo", "", frozenset({((parts.hostname or "").lower(), parts.port or 443)}))


# ---- transports ---------------------------------------------------------------------------------------------------


def entra_simulator(connector):
    """The synthetic Entra tenant (Graph), the simulator of the entra and entra_licenses sources."""
    from . import entra_simulator as simulated

    return simulated.Transport(connector)


MakeTransport = Callable[[frozenset[str]], Any]


def transports(connector, credential, simulator, transport=None) -> MakeTransport:
    """A transport per audience: `transport` (tests) for every audience; the source's `simulator(connector)`, one
    instance for the whole run, for the synthetic tenant (`simulated`) or the synthetic Halo
    (`halo.simulated.invalid`), refused (SourceError) unless core/simulation.allowed(); else a real transport that
    can reach only the sign-in host and the audience's hosts (Halo: core/outbound under the service rule)."""
    if transport is not None:
        return lambda hosts: transport
    if isinstance(credential, connections.HaloCredential):
        if halo.is_simulated(credential.base_url):
            if not simulation.allowed():
                raise SourceError(simulation.MESSAGE)
            simulated = simulator(connector)
            return lambda hosts: simulated
        real = halo.HttpTransport()
        return lambda hosts: real
    if credential.tenant_id == SIMULATED:
        if not simulation.allowed():
            raise SourceError(simulation.MESSAGE)
        simulated = simulator(connector)
        return lambda hosts: simulated
    return lambda hosts: graph.HttpTransport(hosts=frozenset({graph.LOGIN_HOST}) | frozenset(hosts))


def shadow_simulator(connector):
    """The synthetic Entra tenant as the shadow IT source sees it: with the third-party apps nobody registered."""
    from . import entra_simulator as simulated

    return simulated.Transport(connector, shadow=True)


def coverage_simulator(connector):
    """The synthetic Entra tenant as the SSO and MFA coverage source sees it: with conditional access policies and
    provisioning jobs."""
    from . import entra_simulator as simulated

    return simulated.Transport(connector)


def licence_usage_simulator(connector):
    """The synthetic Entra tenant as the licence usage source sees it: with its licensed users."""
    from . import entra_simulator as simulated

    return simulated.Transport(connector)


def graph_transport(connector, credential, transport=None):
    """The transport of a Graph-only source (entra, entra_licenses): `transports` for the Graph audience."""
    return transports(connector, credential, entra_simulator, transport)(GRAPH_AUDIENCE.hosts)


# ---- errors in Softwarden's words -----------------------------------------------------------------------------------

AADSTS_UNKNOWN_RESOURCE = "AADSTS500011"  # the resource principal isn't in the tenant (not verified on Learn)


def token_problem(error: graph.GraphError, audience: Audience = GRAPH_AUDIENCE) -> SignInRefused:
    """A refused token request in Softwarden's words, classified like the connection test (by AADSTS code)."""
    result = connections.classify_microsoft(error)
    if result.code == AADSTS_UNKNOWN_RESOURCE:
        return SignInRefused(
            f"Microsoft couldn't issue a token for {audience.label} ({result.code}: the resource isn't known in this "
            "tenant). Check the address, and that it belongs to the connection's tenant.",
            "unavailable",
            result.code,
        )
    return SignInRefused(
        f"Microsoft refused to sign in: {result.detail} {result.next}".strip(), result.problem, result.code
    )


# ARM's AuthorizationFailed message: "... does not have authorization to perform action '<action>' over scope
# '<scope>' or the scope is invalid. ...". Only the action and the scope are read, and only when
# they look like an ARM action and a resource ID.
ARM_DENIED = re.compile(r"perform action '(?P<action>[^']{1,200})' over scope '(?P<scope>[^']{1,600})'")
ARM_ACTION = re.compile(r"^Microsoft\.[A-Za-z0-9.]{1,80}(/[A-Za-z0-9.]{1,80}){1,8}$")
ARM_SCOPE = re.compile(r"^/subscriptions/[0-9a-fA-F-]{36}(/[A-Za-z0-9._()-]{1,90}){0,10}$")
# Dataverse 403 types meaning the caller lacks access; its error codes are often empty or hex.
DATAVERSE_DENIED = frozenset(
    {"AccessDenied", "PrivilegeDenied", "PrincipalPrivilegeDenied", "CrmSecurityError", "InvalidAccessRights"}
)
PRIVILEGE = re.compile(r"\b(prv[A-Z][A-Za-z]{1,60})\b")
DATAVERSE_APP_USER = (
    "Add the connection's app as an application user in the environment (Power Platform admin center > Environment "
    "> Settings > Users + permissions > Application users) with a read-only security role that can read the table."
)


def authorization_failed(message: str) -> tuple[str, str]:
    """(action, scope) from ARM's AuthorizationFailed message, or ("", "") unless both look right."""
    found = ARM_DENIED.search(message or "")
    if not found:
        return "", ""
    action, scope = found.group("action"), found.group("scope")
    if ARM_ACTION.fullmatch(action) and ARM_SCOPE.fullmatch(scope):
        return action, scope
    return "", ""


def subscription_of(scope: str) -> str:
    """The subscription ID of an ARM scope (`/subscriptions/{id}/...`), or ""."""
    parts = (scope or "").split("/")
    return parts[2].lower() if len(parts) > 2 and parts[1].lower() == "subscriptions" else ""


def reader_advice(subscription: str = "") -> str:
    where = f"subscription {subscription}" if subscription else "each subscription to read"
    return (
        f"Assign the Azure role Reader to the connection's app on {where} (Access control (IAM) > Add role "
        "assignment), or on a management group above it. Role changes can take a few minutes to apply."
    )


def refusal(
    status: int,
    code: str,
    message: str,
    what: str,
    permission: Permission | None,
    audience: Audience,
    url: str = "",
) -> Refused:
    """The run's error for a refused read, in Softwarden's words: names the missing grant, and for ARM the action and
    scope Microsoft named, for Dataverse the privilege. `message` (the service's) is only searched for those."""
    code = connections.safe_code(code)
    tag = f"{status} {code}".strip()
    label = audience.label
    if audience.kind == "dataverse" and (status in (401, 403) or code in DATAVERSE_DENIED):
        found = PRIVILEGE.search(message or "")
        privilege = found.group(1) if found else ""
        advice = permission.advice if permission and permission.advice else DATAVERSE_APP_USER
        named = f", missing privilege {privilege}" if privilege else ""
        return Refused(
            f"{label} refused to {what} ({tag}{named}): the app needs an application user in this environment with "
            f"read access. {advice}",
            status,
            code,
            missing=True,
            privilege=privilege,
        )
    if audience.kind == "arm" and status == 403:
        action, scope = authorization_failed(message) if code == "AuthorizationFailed" else ("", "")
        lacks = f": the app lacks {action} over {scope}" if action else ": the app has no role that allows it"
        return Refused(
            f"{label} refused to {what} ({tag}){lacks}. {reader_advice(subscription_of(scope))}",
            status,
            code,
            missing=True,
            action=action,
            resource_scope=scope,
        )
    if audience.kind == "arm" and status == 404 and code == "SubscriptionNotFound":
        # A subscription the app has no role on reads as not found: a missing Reader, or a wrong ID.
        sub = subscription_of(urllib.parse.urlsplit(url).path)
        return Refused(
            f"{label} refused to {what} ({tag}): the app has no Reader on this subscription, or the ID is wrong. "
            f"{reader_advice(sub)}",
            status,
            code,
            missing=True,
            resource_scope=f"/subscriptions/{sub}" if sub else "",
        )
    if audience.kind == "bap" and status in (401, 403):
        # The admin API answers an app that isn't registered for it 401 or 403 (a 401 survives a fresh token).
        advice = permission.advice if permission else ""
        return Refused(
            f"{label} refused to {what} ({tag}): the app isn't registered for it. {advice}".strip(),
            status,
            code,
            missing=True,
        )
    if audience.kind == "halo" and status == 403:
        advice = permission.advice if permission else ""
        return Refused(f"{label} refused to {what} ({tag}). {advice}".strip(), status, code, missing=True)
    return Refused(refused(status, code, what, permission, audience), status, code)


def refused(
    status: int, code: str, what: str, permission: Permission | None, audience: Audience = GRAPH_AUDIENCE
) -> str:
    """The run's message for a refused read: names the permission when Graph says the app lacks one."""
    code = connections.safe_code(code)
    tag = f"{status} {code}".strip()
    label = audience.label
    if status == 401:  # the token itself wasn't accepted: authentication, not a permission
        return (
            f"{label} didn't accept the app's token while trying to {what} ({tag}). Test the connection; if it "
            "passes, check that the app is enabled in the tenant."
        )
    if status == 403 and permission is not None:
        return (
            f"{label} refused to {what} ({tag}): the app needs the application permission {permission.name} with "
            "admin consent. Grant it, then run the connector's access check."
        )
    if status == 404:
        return f"{label} couldn't find what it was asked to {what} ({tag})."
    if status in (429, 503):
        return f"{label} is busy ({tag}). The next run will try again."
    return f"{label} refused to {what} ({tag})."


def throttled(e: graph.ThrottledOut, what: str, audience: Audience = GRAPH_AUDIENCE) -> str:
    label = audience.label
    if e.reason == "long_wait":
        return (
            f"{label} asked to wait {round(e.retry_after or 0)} seconds ({e.status}) while trying to {what}; "
            "the run stopped rather than retry early. The next run will try again."
        )
    if e.reason == "attempts":
        return f"{label} kept throttling one request ({e.status}) while trying to {what}. The next run will try again."
    return (
        f"{label} kept throttling requests ({e.status}) past this run's limit of "
        f"{round(graph.RUN_BUDGET / 60)} minutes of waiting. The next run will try again."
    )


# ---- the client -----------------------------------------------------------------------------------------------------


class Client:
    """Reads from one audience (Microsoft Graph unless told otherwise) for one run: a token for that audience from the
    connection's credential, a wall-clock deadline (RUN_DEADLINE, or `deadline` seconds) checked before every request,
    and a throttling budget shared by every request of the run that never waits past that deadline. The clients of one
    run share the deadline and the budget (`Session`, through `shared`)."""

    def __init__(
        self,
        transport,
        credential,
        budget_seconds: float = graph.RUN_BUDGET,
        deadline: float = RUN_DEADLINE,
        audience: Audience = GRAPH_AUDIENCE,
        *,
        shared: tuple[float, float, graph.Budget] | None = None,
    ):
        if audience.kind != "halo" and credential.tenant_id == SIMULATED and not simulation.allowed():
            raise SourceError(simulation.MESSAGE)
        if audience.kind != "halo" and not (
            credential.tenant_id == SIMULATED or graph.valid_tenant(credential.tenant_id)
        ):
            raise SourceError("The connection's tenant ID isn't valid. Check the connection.")
        self.transport = transport
        self.credential = credential
        self.audience = audience
        if shared is not None:
            self.limit, self.deadline, self.budget = shared
        else:
            self.limit = deadline
            self.deadline = graph.monotonic() + deadline
            self.budget = graph.Budget(budget_seconds, until=self.deadline)
        self._token = ""

    def token(self) -> str:
        if not self._token:
            self._token = self._halo_token() if self.audience.kind == "halo" else self._microsoft_token()
            issued = _ISSUED.get()
            if issued is not None:
                issued.append(self._token)
        return self._token

    def _microsoft_token(self) -> str:
        c = self.credential
        try:
            body = graph.request_token(self.transport, c.tenant_id, c.client_id, c.secret, self.audience.scope)
        except graph.GraphError as e:
            raise token_problem(e, self.audience) from None
        except graph.HostRefused:
            raise SignInRefused("Refused to call a host other than Microsoft's.", "refused") from None
        except graph.Unreachable:
            raise SignInRefused(
                "Couldn't reach the Microsoft sign-in service from this server.", "unreachable"
            ) from None
        return str(body["access_token"])

    def _halo_token(self) -> str:
        c = self.credential
        try:
            body = halo.request_token(self.transport, c.base_url, c.client_id, c.secret, c.scope)
        except halo.HaloError as e:
            result = connections.classify_halo(e)
            raise SignInRefused(
                f"Halo refused to sign in: {result.detail} {result.next}".strip(), result.problem, result.code
            ) from None
        return str(body["access_token"])

    def get(
        self,
        url: str,
        what: str,
        permission: Permission | None,
        headers: Mapping[str, str] | None = None,
        *,
        retry_throttled: bool = True,
    ) -> Any:
        """One GET as JSON (dict). SourceError on any failure, in words that name the missing permission. The token is
        attached only after `url` passed the audience's allow-list (a next link to another host stops the run). With
        `retry_throttled` False a 429 or 503 is raised as Throttled at once instead of waited out and retried."""
        label = self.audience.label
        if not self.audience.allows(url):  # a next link to another host (SSRF, or a token sent elsewhere)
            raise SourceError(f"{label} returned a next-page link to an unexpected host; the run stopped.")
        body: Any = None
        for attempt in (1, 2):
            at_checkpoint()
            if graph.monotonic() > self.deadline:
                raise DeadlineReached(
                    f"The run stopped at its {round(self.limit / 60)}-minute limit while trying to {what} ({label} "
                    "was slow or throttling). The next run will try again."
                )
            try:
                body = graph.get_json_retrying(
                    self.transport,
                    url,
                    {"Authorization": f"Bearer {self.token()}", **(headers or {})},
                    self.budget,
                    retry=retry_throttled,
                )
                break
            except graph.GraphError as e:
                if e.status == 401 and attempt == 1:  # an expired or revoked token: sign in again, once
                    self._token = ""
                    continue
                if not retry_throttled and e.throttled:
                    raise Throttled(
                        refused(e.status, e.code, what, permission, self.audience), e.status, e.retry_after
                    ) from None
                raise refusal(e.status, e.code, e.message, what, permission, self.audience, url) from None
            except graph.ThrottledOut as e:
                raise SourceError(throttled(e, what, self.audience)) from None
            except graph.HostRefused:
                if self.audience.kind == "halo":
                    raise SourceError(f"Refused to call the Halo address. {outbound.SERVICE_REFUSED}") from None
                raise SourceError("Refused to call a host other than Microsoft's.") from None
            except graph.Unreachable:
                raise SourceError(f"Couldn't reach {label} from this server.") from None
        if not isinstance(body, dict):
            raise SourceError(f"{label} answered without the expected JSON while trying to {what}.")
        return body

    def get_all(
        self,
        url: str,
        what: str,
        permission: Permission | None,
        page_limit: int,
        headers: Mapping[str, str] | None = None,
        next_key: str = "@odata.nextLink",
    ) -> list[dict[str, Any]]:
        """Every item of a collection, following `next_key` (`@odata.nextLink`; ARM's `nextLink`), only to this
        audience's hosts."""
        out: list[dict[str, Any]] = []
        for _ in range(page_limit):
            page = self.get(url, what, permission, headers)
            out.extend(v for v in page.get("value") or [] if isinstance(v, dict))
            url = page.get(next_key) or ""
            if not url:
                return out
        raise SourceError(f"The tenant has more objects than a run reads while trying to {what}; narrow the scope.")


class Session:
    """One run's (or one access check's) clients: one per audience, created on first use, sharing a wall-clock
    deadline and a throttling budget. `make_transport(hosts)` gives each audience its transport (`transports`)."""

    def __init__(
        self,
        make_transport: MakeTransport,
        credential,
        budget_seconds: float = graph.RUN_BUDGET,
        deadline: float = RUN_DEADLINE,
    ):
        self.make_transport = make_transport
        self.credential = credential
        self.limit = deadline
        self.until = graph.monotonic() + deadline
        self.budget = graph.Budget(budget_seconds, until=self.until)
        self._clients: dict[str, Client] = {}

    def client(self, audience: Audience) -> Client:
        if audience.key not in self._clients:
            self._clients[audience.key] = Client(
                self.make_transport(audience.hosts),
                self.credential,
                audience=audience,
                shared=(self.limit, self.until, self.budget),
            )
        return self._clients[audience.key]


# ---- one-item probes (access checks) ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    """One read an access check makes: the call, what it proves and the permission it needs."""

    name: str
    url: str
    permission: Permission
    # A scope key that must be on for the source to use this permission (agent identities); off, the step is "not used"
    when: str = ""
