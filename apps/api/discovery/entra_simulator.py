"""A synthetic Entra tenant: answers the token request and the Graph reads the Entra sources make
(service principals, app registrations, agent identities, subscribed SKUs, and the access checks' one-item reads)
with made-up data, the way Microsoft Learn documents them, so tests and the demo never call Microsoft. Selected by the
tenant ID `simulated`. It implements core/graph.Transport (`send` returns a Reply).

The tenant is deterministic; credential end dates are counted from today (UTC), so a test sees the same days left on
any day. Connector config keys (set by tests and `manage.py simulate_entra_change`, never through the API):

- `simulated_size`: that many generated line-of-business apps (for timing).
- `simulator_changes`: edits applied in order, {"op", "id" (display name or object ID), "value"}: rename, disable,
  enable, remove, add (a new enterprise app), add_registration (an app registration with no service principal),
  owners (emails, on both objects), sp_owners (emails, the service principal's only; [] for none), secret (value
  {"name", "days"}: add a client secret to the registration), remove_credentials, tag (value: a tag added to the
  service principal).
- `$expand=owners` returns at most 20 owners (Graph's $expand limit, no next link); `/{applications|servicePrincipals}
  /{id}/owners` returns them all, paged.
- `simulated_agents_in_list`: the agent identities are also returned by /servicePrincipals.
- `simulated_blueprint`: an agent identity blueprint (#microsoft.graph.agentIdentityBlueprint) and its principal.
- `simulated_token_expires`: the first Graph read answers 401 InvalidAuthenticationToken (an expired token).
- `simulated_token_echo`: the token endpoint refuses with an error_description that echoes the client secret.
- `simulated_permissions`: the application permissions granted (default: all four the sources use). A read whose
  permission is missing answers 403 Authorization_RequestDenied; without User.ReadBasic.All owners come back with
  their id and type only.
- `simulated_throttle`: {"path": "/v1.0/servicePrincipals", "times": 2, "retry_after": "3" or null, "status": 429}:
  the first `times` reads of that path answer throttled, with Retry-After when given.
- `simulated_errors`: {"path": status}: that read answers the status with an error body that echoes the request's
  bearer token, so a test can prove no message ever carries it.
- `simulated_empty`: paths (matched at the end, e.g. "/servicePrincipals") whose read answers 200 with an empty
  `value`, as Graph might on revoked consent or an upstream fault (the empty-listing guard, discovery/engine.py).

Shadow IT (discovery/entra_shadow_it.py):

- `GET /v1.0/organization` (the tenant's own ID), `GET /v1.0/oauth2PermissionGrants` (GRANTS, paged by
  `simulated_grant_page`, default 100) and `GET /v1.0/auditLogs/signIns`. Sign-ins follow a fixed schedule per app
  (SIGN_INS, counted from EPOCH), so any time window has a known answer (`expected_sign_ins`). The read must filter
  `createdDateTime ge ...` (and may filter `le ...`), as on a real tenant where an unfiltered read times out; it answers
  newest first, at most `$top` (1000 at most) and `simulated_signin_page` (default 100) per page with a next link, and
  nothing older than 30 days (the P1/P2 retention) or later than `clock()`. Some sign-ins failed (status.errorCode).
- The transport the shadow IT source uses (`shadow=True`, or the config key `simulated_shadow_apps` for another
  source) also lists SHADOW_APPS: third-party enterprise apps nobody registered in the catalog.
- `simulated_fail_after`: {"path", "after", "status", "times"}: after `after` answered reads of that path, the next
  `times` (default 1) answer `status` (a 500, a 504 on a later page, ...).
- `simulated_no_premium`: the tenant has no Entra ID P1 or P2: the sign-in logs answer 403
  Authentication_RequestFromNonPremiumTenantOrB2CTenant.
- `/v1.0/servicePrincipals` (and the other application reads) also answer to Directory.Read.All, as Graph does.

SSO and MFA coverage (discovery/entra_coverage.py):

- Every service principal carries `preferredSingleSignOnMode` (SSO_MODES: saml, oidc, password, notSupported, or null
  as on older SAML and OIDC apps), `preferredTokenSigningKeyThumbprint` and `samlSingleSignOnSettings` (set for the
  SAML apps, including one whose mode is null).
- `GET /v1.0/servicePrincipals/{id}/synchronization/jobs` (Synchronization.Read.All): SYNC_JOBS, each job's
  `schedule.state` and `status.code` (Active, Paused, Quarantine, Disabled, NotRun); an app without jobs answers an
  empty list, an unknown ID 404.
- `GET /v1.0/identity/conditionalAccess/policies` (Policy.Read.All): ca_policies(), or the config's
  `simulated_ca_policies` (raw policies) instead, paged by `simulated_ca_page` (default 100). With
  `simulated_no_premium` it answers 403 Authentication_RequestFromNonPremiumTenantOrB2CTenant, as the sign-in logs do
  (the code a real tenant without P1 gives here isn't on Learn; any 403 is handled the same). A 429 without Retry-After
  comes from `simulated_throttle` with `retry_after` null, as conditional access throttles on a real tenant.

Licence usage (discovery/entra_licence_usage.py):

- `GET /v1.0/users?$select=...assignedLicenses...` (User.Read.All, or Directory.Read.All) lists LICENSED_USERS (every
  combination of enabled or disabled, member or guest, no, one or several licences, and a last sign-in that is
  recent, old, never or failed only; plus named people, a service account and a new starter), only the properties
  `$select` names, as Graph does (assignedLicenses and signInActivity come back only when selected). Sign-in dates are
  counted back from `clock()`. Paged by `simulated_user_page` (default 100); `$top` above 999, or above 500 with
  signInActivity, is a 400, as on Graph.
- Selecting signInActivity also needs AuditLog.Read.All (403 Authorization_RequestDenied without it) and Entra ID P1:
  with `simulated_no_premium` it answers 403 Authentication_RequestFromNonPremiumTenantOrB2CTenant.
- `simulated_licensed_users`: that many more licensed members (SPE_E5; signed in 5 or 200 days ago, alternately);
  `simulated_remove_users`: user principal names left out of the listing; `simulated_no_users`: the listing is empty;
  `simulated_users_without_licences`: users come back without assignedLicenses (a broken read).
- A `$select` without assignedLicenses or signInActivity is the Entra source's owner-name probe (User.ReadBasic.All).

Anything else is refused with a 400, so a stray call fails a test rather than passing silently."""

from __future__ import annotations

import copy
import json
import math
import re
import urllib.parse
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from core.graph import GRAPH_HOST, LOGIN_HOST, Reply

MICROSOFT = "f8cdef31-a31e-4b4a-93e4-5f571e91255a"
OUR_TENANT = "00000000-0000-4000-8000-00000000c0de"
VENDOR_TENANT = "0000000a-0000-4000-8000-00000000000a"  # a SaaS vendor that publishes multi-tenant apps
TOKEN = "simulated-token"  # noqa: S105 (a synthetic value the simulator issues)
PAGE = 100
EXPAND_LIMIT = 20
OWNERS_PATH = re.compile(r"^/v1\.0/(applications|servicePrincipals)/([0-9a-f-]{36})/owners$")
GRANTED = (
    "Application.Read.All",
    "LicenseAssignment.Read.All",
    "User.ReadBasic.All",
    "User.Read.All",
    "AgentIdentity.Read.All",
    "AuditLog.Read.All",
    "Directory.Read.All",
    "Policy.Read.All",
    "Synchronization.Read.All",
)
APPLICATION_READ = ("Application.Read.All", "Directory.Read.All")  # either lists applications and their owners
# The permissions any one of which allows a read.
NEEDS: dict[str, tuple[str, ...]] = {
    "/v1.0/servicePrincipals": APPLICATION_READ,
    "/v1.0/applications": APPLICATION_READ,
    "/v1.0/servicePrincipals/microsoft.graph.agentIdentity": ("AgentIdentity.Read.All",),
    "/v1.0/subscribedSkus": ("LicenseAssignment.Read.All",),
    "/v1.0/users": ("User.ReadBasic.All",),
    "/v1.0/organization": ("Directory.Read.All",),
    "/v1.0/oauth2PermissionGrants": ("Directory.Read.All",),
    "/v1.0/auditLogs/signIns": ("AuditLog.Read.All",),
    "/v1.0/identity/conditionalAccess/policies": ("Policy.Read.All",),
}
SYNC_PATH = re.compile(r"^/v1\.0/servicePrincipals/([0-9a-f-]{36})/synchronization/jobs$")
CA_PATH = "/v1.0/identity/conditionalAccess/policies"
NON_PREMIUM = "Authentication_RequestFromNonPremiumTenantOrB2CTenant"


def _id(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"simulated-entra/{name}"))


def _owner(name, email):
    return {
        "@odata.type": "#microsoft.graph.user",
        "id": _id(f"user/{email}"),
        "displayName": name,
        "mail": email,
        "userPrincipalName": email,
    }


DANA = _owner("Dana Ortiz", "dana.ortiz@example.test")
PRIYA = _owner("Priya Raman", "priya.raman@example.test")
LEE = _owner("Lee Chen", "lee.chen@example.test")
CONTRACTOR = _owner("Jordan Blake", "jordan.blake@contractor.example.test")

# Credentials: (on "app" or "sp", "secret" or "certificate", display name, days from today, key name). A certificate
# and a secret with the same key name share a keyId (a certificate-backed credential), as Entra lists them.
# (name, publisher tenant, enabled, type, homepage, description, tags, owners, registration, credentials)
BASE: list[tuple[Any, ...]] = [
    (
        "Workday",
        VENDOR_TENANT,
        True,
        "Application",
        "https://example.test/workday",
        "Single sign-on to Workday HR.",
        [],
        [PRIYA, DANA],
        False,
        [
            ("sp", "certificate", "CN=Workday SAML signing", 90, "workday-saml"),
            ("sp", "secret", "", 90, "workday-saml"),
        ],
    ),
    (
        "Zoom",
        VENDOR_TENANT,
        True,
        "Application",
        "https://example.test/zoom",
        "Zoom single sign-on and provisioning.",
        [],
        [DANA],
        False,
        [("sp", "certificate", "CN=Zoom SAML signing", 400, "zoom-saml")],
    ),
    (
        "Finance Ledger SSO",
        OUR_TENANT,
        True,
        "Application",
        "",
        "",
        [],
        [LEE],
        True,
        [("app", "secret", "Ledger API secret", 20, "ledger-secret")],
    ),
    (
        "ServiceNow",
        VENDOR_TENANT,
        True,
        "Application",
        "https://example.test/servicenow",
        "IT service management.",
        [],
        [DANA],
        False,
        [],
    ),
    (
        "Expense Hub",
        OUR_TENANT,
        True,
        "Application",
        "https://example.test/expenses",
        "Expense claims.",
        [],
        [LEE, CONTRACTOR],
        True,
        [("app", "certificate", "CN=Expense Hub client", 200, "expense-cert")],
    ),
    ("Contract Vault", OUR_TENANT, True, "Application", "", "Contract repository used by Legal.", [], [], True, []),
    (
        "HR Helpdesk Agent",
        OUR_TENANT,
        True,
        "Application",
        "",
        "Copilot Studio agent answering HR questions.",
        ["AgentCreatedBy:CopilotStudio", "power-virtual-agents-6f1c2d3e"],
        [PRIYA],
        True,
        [],
    ),
    (
        "Payroll Sync Daemon",
        OUR_TENANT,
        True,
        "Application",
        "",
        "Background job identity for the payroll export.",
        [],
        [DANA],
        True,
        [
            ("app", "secret", "Payroll export", 5, "payroll-secret"),
            ("app", "certificate", "CN=Payroll daemon", 300, "payroll-cert"),
        ],
    ),
    ("Old Intranet", OUR_TENANT, False, "Application", "", "Decommissioned intranet.", [], [], True, []),
    (
        "Graph Explorer Test",
        OUR_TENANT,
        True,
        "Application",
        "",
        "A developer's test registration.",
        [],
        [CONTRACTOR],
        True,
        [("app", "secret", "dev secret", -3, "graph-explorer-secret")],
    ),
    ("backup-vault-identity", OUR_TENANT, True, "ManagedIdentity", "", "", [], [], False, []),
    (
        "Microsoft Teams",
        MICROSOFT,
        True,
        "Application",
        "",
        "",
        [],
        [],
        False,
        [("sp", "certificate", "", 30, "teams-cert")],
    ),
    ("Office 365 Exchange Online", MICROSOFT, True, "Application", "", "", [], [], False, []),
    ("Microsoft Graph", MICROSOFT, True, "Application", "", "", [], [], False, []),
    ("Azure Portal", MICROSOFT, True, "Application", "", "", [], [], False, []),
]

# Third-party SaaS apps people use that nobody registered (shadow IT): enterprise apps from a vendor tenant, listed only
# for the shadow IT source (or with `simulated_shadow_apps`). (name, verified publisher or "")
VENDOR_B = "0000000b-0000-4000-8000-00000000000b"
SHADOW_APPS = [
    ("MeetNote AI", "MeetNote Inc. (synthetic)"),
    ("FileBridge Sync", ""),
    ("ChartPilot", "ChartPilot Ltd (synthetic)"),
    ("QuickPoll", ""),
]
SHADOW_ROWS: list[tuple[Any, ...]] = [
    (name, VENDOR_B, True, "Application", f"https://example.test/{name.split()[0].lower()}", "", [], [], False, [])
    for name, _publisher in SHADOW_APPS
]
VERIFIED = {name: publisher for name, publisher in SHADOW_APPS if publisher}

# Sign-ins (shadow IT): (app, distinct users, hours between sign-ins, minutes after EPOCH of the first, every Nth fails
# or 0). Sign-in k is by user k % users of a shared pool (sign_in_user). An app named here with no enterprise app in the
# tenant (Legacy Portal) is how a Microsoft app without a service principal looks.
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
RETENTION_DAYS = 30
SIGN_INS: list[tuple[str, int, int, int, int]] = [
    ("MeetNote AI", 6, 5, 7, 4),
    ("FileBridge Sync", 4, 9, 13, 0),
    ("ChartPilot", 9, 3, 1, 0),
    ("Workday", 5, 12, 30, 0),
    ("Zoom", 3, 24, 0, 0),  # at midnight UTC: lands on run boundaries in the tests
    ("Microsoft Teams", 8, 2, 5, 0),
    ("Expense Hub", 3, 8, 20, 0),
    ("Legacy Portal", 2, 24, 50, 0),
]
FRACTIONAL = {"ChartPilot"}  # createdDateTime with seven fractional digits, as Graph sometimes writes it
# OAuth consent grants: (client app, consentType, the consenting user's index or None, scope as Graph writes it).
GRANTS: list[tuple[str, str, int | None, str]] = [
    ("MeetNote AI", "Principal", 0, "openid profile offline_access Mail.ReadWrite Calendars.Read"),
    ("MeetNote AI", "Principal", 1, "openid profile offline_access Mail.ReadWrite "),
    ("MeetNote AI", "Principal", 2, " openid profile offline_access Mail.ReadWrite Calendars.Read"),
    ("FileBridge Sync", "AllPrincipals", None, "Files.ReadWrite.All Sites.Read.All offline_access"),
    ("QuickPoll", "Principal", 3, "openid User.Read Directory.Read.All"),
    ("Zoom", "AllPrincipals", None, "User.Read openid profile email"),
    ("Azure Portal", "AllPrincipals", None, "user_impersonation"),
]


# SSO and MFA coverage. preferredSingleSignOnMode per app (absent: null, as Graph leaves it for older apps), and the
# apps with a SAML token signing certificate (the thumbprint and samlSingleSignOnSettings are set).
SSO_MODES: dict[str, str | None] = {
    "Workday": "saml",
    "Zoom": None,  # an older SAML app: no preferred mode, but a SAML signing certificate
    "ServiceNow": None,  # nothing says: unknown
    "Expense Hub": "oidc",
    "Finance Ledger SSO": "password",
    "Contract Vault": "notSupported",
    "Payroll Sync Daemon": "notSupported",
}
SAML_SIGNED = {"Workday", "Zoom"}
# Provisioning jobs per app: (schedule.state, status.code). An app not listed has none (an empty list).
SYNC_JOBS: dict[str, list[tuple[str, str]]] = {
    "Workday": [("Active", "Active")],
    "Zoom": [("Active", "Paused")],
    "ServiceNow": [("Active", "Quarantine")],
    "Expense Hub": [("Disabled", "NotRun")],
    "Finance Ledger SSO": [("Active", "NotRun")],
}
GLOBAL_ADMIN_ROLE = "62e90394-69f5-4237-9190-012177145e10"  # the Global Administrator role template
BREAK_GLASS = _id("user/breakglass@example.test")


def ca_policy(name, state, apps, users, grant_controls, conditions=None, **extra) -> dict[str, Any]:
    """A conditional access policy as Graph returns it: conditions.applications and conditions.users (lists filled in
    as Graph does), grantControls; `conditions` adds or replaces other conditions."""
    applications: dict[str, Any] = {"includeApplications": [], "excludeApplications": [], "includeUserActions": []}
    applications.update(apps)
    people: dict[str, Any] = {
        "includeUsers": [],
        "excludeUsers": [],
        "includeGroups": [],
        "excludeGroups": [],
        "includeRoles": [],
        "excludeRoles": [],
    }
    people.update(users)
    return {
        "id": _id(f"policy/{name}"),
        "displayName": name,
        "state": state,
        "createdDateTime": "2026-01-01T00:00:00Z",
        "conditions": {
            "applications": applications,
            "users": people,
            "clientAppTypes": ["all"],
            "platforms": None,
            "locations": None,
            "signInRiskLevels": [],
            "userRiskLevels": [],
            **(conditions or {}),
        },
        "grantControls": grant_controls,
        "sessionControls": None,
        **extra,
    }


def grant(*controls: str, operator: str = "OR", strength: dict[str, Any] | None = None) -> dict[str, Any]:
    """grantControls as Graph writes them (operator OR by default, even with one control)."""
    return {
        "operator": operator,
        "builtInControls": list(controls),
        "customAuthenticationFactors": [],
        "termsOfUse": [],
        "authenticationStrength": strength,
    }


EVERYONE = {"includeUsers": ["All"], "excludeUsers": [BREAK_GLASS]}
PHISHING_RESISTANT = {
    "id": "00000000-0000-0000-0000-000000000004",
    "displayName": "Phishing-resistant MFA",
    "requirementsSatisfied": "mfa",
    "allowedCombinations": ["windowsHelloForBusiness", "fido2"],
}


def ca_policies() -> list[dict[str, Any]]:
    """The tenant's conditional access policies: one shape per outcome the coverage source tells apart."""

    def app(name):
        return _id(f"app/{name}")

    return [
        ca_policy(
            "Require MFA for Workday", "enabled", {"includeApplications": [app("Workday")]}, EVERYONE, grant("mfa")
        ),
        ca_policy(
            "Require MFA for administrators",
            "enabled",
            {"includeApplications": [app("Finance Ledger SSO")]},
            {"includeRoles": [GLOBAL_ADMIN_ROLE]},
            grant("mfa", operator="AND"),
        ),
        ca_policy(
            "Report-only: MFA for Zoom",
            "enabledForReportingButNotEnforced",
            {"includeApplications": [app("Zoom")]},
            EVERYONE,
            grant("mfa"),
        ),
        ca_policy(
            "MFA or a compliant device for ServiceNow",
            "enabled",
            {"includeApplications": [app("ServiceNow")]},
            EVERYONE,
            grant("mfa", "compliantDevice"),
        ),
        ca_policy(
            "Phishing-resistant MFA for Expense Hub",
            "enabled",
            {"includeApplications": [app("Expense Hub")]},
            EVERYONE,
            grant(strength=PHISHING_RESISTANT),
        ),
        ca_policy(
            "MFA for Contract Vault unless tagged",
            "enabled",
            {
                "includeApplications": [app("Contract Vault")],
                "applicationFilter": {"mode": "exclude", "rule": 'CustomSecurityAttribute.Apps_Tier -eq "Low"'},
            },
            EVERYONE,
            grant("mfa"),
        ),
        ca_policy(
            "Block legacy authentication", "disabled", {"includeApplications": ["All"]}, EVERYONE, grant("block")
        ),
        ca_policy(
            "Require MFA for Office 365", "enabled", {"includeApplications": ["Office365"]}, EVERYONE, grant("mfa")
        ),
    ]


def sync_jobs(name: str) -> list[dict[str, Any]]:
    """The provisioning jobs of a simulated app, as `GET /servicePrincipals/{id}/synchronization/jobs` lists them."""
    return [
        {
            "id": f"scim.{_id(name)[:8]}.{i}",
            "templateId": "scim",
            "schedule": {"expiration": None, "interval": "PT40M", "state": schedule},
            "status": {"code": code, "lastExecution": None, "quarantine": None},
        }
        for i, (schedule, code) in enumerate(SYNC_JOBS.get(name, []))
    ]


def _thumbprint(name: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"simulated-entra/thumbprint/{name}").hex.upper() + "00000000"


def clock() -> datetime:
    """The simulated tenant's now (tests replace it): no sign-in is later, none older than RETENTION_DAYS."""
    return datetime.now(UTC)


def sign_in_user(k: int) -> str:
    return _id(f"signin-user/{k}")


def _events(name, users, hours, offset, fail_every, lo, hi):
    """(k, createdDateTime) of the app's sign-ins with lo <= t <= hi."""
    period = timedelta(hours=hours)
    first = EPOCH + timedelta(minutes=offset)
    if name in FRACTIONAL:
        first += timedelta(microseconds=123456)
    k = max(0, math.floor((lo - first) / period))
    while (t := first + k * period) <= hi:
        if t >= lo:
            yield k, t
        k += 1


def _failed(k, fail_every) -> bool:
    return bool(fail_every) and k % fail_every == fail_every - 1


def sign_ins(start: datetime, end: datetime, now: datetime | None = None) -> list[dict[str, Any]]:
    """Every simulated sign-in with start <= createdDateTime <= end (Graph's ge and le), within the retention, newest
    first."""
    now = now or clock()
    lo, hi = max(start, now - timedelta(days=RETENTION_DAYS)), min(end, now)
    found = []
    for name, users, hours, offset, fail_every in SIGN_INS:
        for k, t in _events(name, users, hours, offset, fail_every, lo, hi):
            user = k % users
            stamp = t.strftime("%Y-%m-%dT%H:%M:%S") + (f".{t.microsecond:06d}0Z" if t.microsecond else "Z")
            found.append(
                (
                    t,
                    {
                        "id": _id(f"signin/{name}/{k}"),
                        "createdDateTime": stamp,
                        "userDisplayName": f"Synthetic User {user}",
                        "userPrincipalName": f"user{user}@contoso.example.test",
                        "userId": sign_in_user(user),
                        "appId": _id(f"app/{name}"),
                        "appDisplayName": name,
                        "ipAddress": f"203.0.113.{10 + user}",
                        "clientAppUsed": "Browser",
                        "correlationId": _id(f"correlation/{name}/{k}"),
                        "isInteractive": True,
                        "resourceDisplayName": "Microsoft Graph",
                        "resourceId": _id("app/Microsoft Graph"),
                        "status": (
                            {"errorCode": 50126, "failureReason": "Invalid username or password."}
                            if _failed(k, fail_every)
                            else {"errorCode": 0, "failureReason": None}
                        ),
                        "location": {"city": "Synthetic City", "countryOrRegion": "ZZ"},
                    },
                )
            )
    found.sort(key=lambda pair: (pair[0], pair[1]["id"]), reverse=True)
    return [event for _t, event in found]


def expected_sign_ins(name: str, start: datetime, end: datetime, now: datetime | None = None) -> tuple[int, int]:
    """(successful sign-ins, distinct users among them) of one app with start <= createdDateTime < end, within the
    retention: what the shadow IT source should count for that span (tests)."""
    now = now or clock()
    lo, hi = max(start, now - timedelta(days=RETENTION_DAYS)), min(end, now)
    row = next(r for r in SIGN_INS if r[0] == name)
    users, fail_every = row[1], row[4]
    count, seen = 0, set()
    for k, t in _events(*row, lo, hi):
        if t >= end or _failed(k, fail_every):
            continue
        count += 1
        seen.add(k % users)
    return count, len(seen)


def oauth2_grants() -> list[dict[str, Any]]:
    return [
        {
            "id": f"grant-{i}",
            "clientId": _id(client),
            "consentType": consent,
            "principalId": sign_in_user(user) if user is not None else None,
            "resourceId": _id("Microsoft Graph"),
            "scope": scope,
        }
        for i, (client, consent, user, scope) in enumerate(GRANTS)
    ]


def _when(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# Agent identities (Entra Agent ID), listed only by /servicePrincipals/microsoft.graph.agentIdentity.
AGENT_IDENTITIES = [("Benefits Agent", "Answers benefits questions.", True)]

# Subscribed SKUs: (skuId, part number, capabilityStatus, enabled, suspended, warning, consumed)
SKUS = [
    ("06ebc4ee-1bb5-47dd-8120-11324bc54e06", "SPE_E5", "Enabled", 250, 0, 0, 231),
    ("05e9a617-0261-4cee-bb44-138d3ef5d965", "SPE_E3", "Enabled", 100, 0, 10, 97),
    ("f30db892-07e9-47e9-837c-80727f46fd3d", "FLOW_FREE", "Enabled", 10000, 0, 0, 12),
    ("c42b9cae-ea4f-4ab7-9717-81576235ccac", "DEVELOPERPACK_E5", "Suspended", 0, 25, 0, 3),
]


E5, E3, FLOW = SKUS[0][0], SKUS[1][0], SKUS[2][0]
# Licensed users (licence usage). The licences of each shape: none, one (SPE_E5) or several (SPE_E3 and FLOW_FREE).
LICENCE_SETS: dict[str, list[str]] = {"none": [], "one": [E5], "several": [E3, FLOW]}
# A last sign-in shape: (days ago of the last successful sign-in, days ago of the last attempt), None for none.
ACTIVITY: dict[str, tuple[int | None, int | None]] = {
    "recent": (5, 5),
    "old": (200, 200),
    "never": (None, None),
    "failed": (None, 10),  # only failed attempts recorded: not "never signed in"
}
CREATED_DAYS_AGO = 400
GROUP = _id("group/flow-users")  # FLOW_FREE is assigned through this group (licenseAssignmentStates.assignedByGroup)


def _shape_user(enabled: bool, guest: bool, licences: str, activity: str) -> dict[str, Any]:
    state = "on" if enabled else "off"
    who = "guest" if guest else "member"
    name = f"{who.title()} {licences} {activity} {state}"
    local = f"{who}.{licences}.{activity}.{state}"
    upn = f"{local}_fabrikam.example.test#EXT#@contoso.example.test" if guest else f"{local}@contoso.example.test"
    return {
        "name": name,
        "upn": upn,
        "mail": f"{local}@fabrikam.example.test" if guest else f"{local}@contoso.example.test",
        "enabled": enabled,
        "type": "Guest" if guest else "Member",
        "skus": LICENCE_SETS[licences],
        "activity": activity,
        "created": CREATED_DAYS_AGO,
    }


def licensed_users_base() -> list[dict[str, Any]]:
    """Every shape the licence usage source tells apart, plus people the tests link, a service account and a new
    starter. `activity` names an ACTIVITY shape; `created` is days ago."""
    users = [
        _shape_user(enabled, guest, licences, activity)
        for enabled in (True, False)
        for guest in (False, True)
        for licences in LICENCE_SETS
        for activity in ACTIVITY
    ]
    users += [
        {**_shape_user(True, False, "one", "recent"), "name": DANA["displayName"], "upn": DANA["mail"]},
        {**_shape_user(True, False, "one", "old"), "name": PRIYA["displayName"], "upn": PRIYA["mail"]},
        {
            **_shape_user(True, False, "several", "never"),
            "name": "Backup service",
            "upn": "svc-backup@contoso.example.test",
            "mail": "",
        },
        {**_shape_user(True, False, "one", "never"), "name": "New Starter", "upn": "new.starter@contoso.example.test"},
        # userType null, as on some older accounts: a member
        {**_shape_user(True, False, "one", "recent"), "name": "Legacy Account", "upn": "legacy@contoso.example.test"},
    ]
    users[-4]["mail"] = PRIYA["mail"]
    users[-5]["mail"] = DANA["mail"]
    users[-2]["created"] = 10  # a new starter who hasn't signed in yet
    users[-1]["type"] = None
    return users


def _stamp(days: int | None, now: datetime) -> str | None:
    if days is None:
        return None
    return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def licensed_users(config: Mapping[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    """The tenant's users as Graph returns them with every property the licence usage source can select."""
    now = now or clock()
    users = licensed_users_base()
    for i in range(int(config.get("simulated_licensed_users") or 0)):
        users.append(
            {
                **_shape_user(True, False, "one", "recent" if i % 2 else "old"),
                "name": f"Generated {i:05d}",
                "upn": f"generated.{i:05d}@contoso.example.test",
                "mail": f"generated.{i:05d}@contoso.example.test",
            }
        )
    removed = {str(u).lower() for u in config.get("simulated_remove_users") or []}
    out = []
    for u in users:
        if u["upn"].lower() in removed:
            continue
        success, attempt = ACTIVITY[u["activity"]]
        out.append(
            {
                "id": _id(f"user/{u['upn'].lower()}"),
                "displayName": u["name"],
                "mail": u["mail"] or None,
                "userPrincipalName": u["upn"],
                "accountEnabled": u["enabled"],
                "userType": u["type"],
                "createdDateTime": _stamp(u["created"], now),
                "assignedLicenses": [{"skuId": sku, "disabledPlans": []} for sku in u["skus"]],
                "licenseAssignmentStates": [
                    {
                        "skuId": sku,
                        "assignedByGroup": GROUP if sku == FLOW else None,
                        "state": "Active",
                        "error": "None",
                        "disabledPlans": [],
                    }
                    for sku in u["skus"]
                ],
                "signInActivity": {
                    "lastSignInDateTime": _stamp(attempt, now),
                    "lastSignInRequestId": None,
                    "lastNonInteractiveSignInDateTime": None,
                    "lastNonInteractiveSignInRequestId": None,
                    "lastSuccessfulSignInDateTime": _stamp(success, now),
                    "lastSuccessfulSignInRequestId": None,
                },
            }
        )
    return out


def user_id(upn: str) -> str:
    """A simulated user's object ID."""
    return _id(f"user/{upn.lower()}")


def _credential(name, kind, display, days, key, today):
    end = datetime.combine(today + timedelta(days=days), datetime.min.time(), UTC)
    start = end - timedelta(days=365)
    common = {
        "keyId": _id(f"key/{name}/{key}"),
        "displayName": display or None,
        "startDateTime": start.isoformat().replace("+00:00", "Z"),
        "endDateTime": end.isoformat().replace("+00:00", "Z"),
        "customKeyIdentifier": None,
    }
    if kind == "certificate":
        return "keyCredentials", {**common, "type": "AsymmetricX509Cert", "usage": "Verify", "key": None}
    return "passwordCredentials", {**common, "hint": "s3c", "secretText": None}


def _objects(row, today):
    """(service principal or None, application or None) for one simulated app (a BASE row)."""
    name, publisher, enabled, sp_type, homepage, description, tags, owners, registration, creds = row
    sp: dict[str, Any] | None = None
    app: dict[str, Any] | None = None
    if sp_type != "registration_only":
        sp = {
            "@odata.type": "#microsoft.graph.servicePrincipal",
            "id": _id(name),
            "appId": _id(f"app/{name}"),
            "displayName": name,
            "description": description,
            "homepage": homepage,
            "accountEnabled": enabled,
            "appOwnerOrganizationId": publisher,
            "publisherName": "Microsoft Services" if publisher == MICROSOFT else "Contoso (synthetic)",
            "tags": list(tags),
            "servicePrincipalType": sp_type,
            "preferredSingleSignOnMode": SSO_MODES.get(name),
            "preferredTokenSigningKeyThumbprint": _thumbprint(name) if name in SAML_SIGNED else None,
            "samlSingleSignOnSettings": {"relayState": None} if name in SAML_SIGNED else None,
            "keyCredentials": [],
            "passwordCredentials": [],
            "owners": copy.deepcopy(owners),
        }
    if registration:
        app = {
            "@odata.type": "#microsoft.graph.application",
            "id": _id(f"application/{name}"),
            "appId": _id(f"app/{name}"),
            "displayName": name,
            "description": description,
            "publisherDomain": "contoso.example.test",
            "signInAudience": "AzureADMyOrg",
            "tags": [],
            "web": {"homePageUrl": homepage or None},
            "keyCredentials": [],
            "passwordCredentials": [],
            "owners": copy.deepcopy(owners),
        }
    for on, kind, display, days, key in creds:
        target = app if on == "app" else sp
        if target is not None:
            field, value = _credential(name, kind, display, days, key, today)
            target[field].append(value)
    return sp, app


def tenant(config, today=None, shadow=False):
    """(service principals, applications) after the config's changes; with `shadow`, the SHADOW_APPS too."""
    today = today or datetime.now(UTC).date()
    rows = [list(r) for r in BASE]
    if shadow:
        rows += [list(r) for r in SHADOW_ROWS]
    for i in range(int(config.get("simulated_size") or 0)):
        owner = [DANA, PRIYA, LEE, CONTRACTOR][i % 4]
        rows.append(
            [
                f"Line-of-business app {i + 1:04d}",
                OUR_TENANT,
                i % 10 != 9,
                "Application",
                "",
                "Generated for timing.",
                [],
                [owner],
                False,
                [],
            ]
        )
    by_name = {r[0]: r for r in rows}
    renamed: dict[str, str] = {}
    sp_owners: dict[str, list[dict[str, Any]]] = {}
    for change in config.get("simulator_changes") or []:
        op, target_id = change.get("op"), change.get("id")
        target = by_name.get(target_id) or next((r for r in rows if _id(r[0]) == target_id), None)
        if op in ("add", "add_registration"):
            name = change.get("value") or target_id
            row = [name, OUR_TENANT, True, "registration_only" if op == "add_registration" else "Application"]
            row += ["", "", [], [], op == "add_registration", []]
            rows.append(row)
            by_name[name] = row
        elif target is None:
            continue
        elif op == "rename":
            renamed[target[0]] = change["value"]
        elif op in ("disable", "enable"):
            target[2] = op == "enable"
        elif op == "remove":
            rows.remove(target)
        elif op == "owners":
            target[7] = [_owner(e.split("@")[0].replace(".", " ").title(), e) for e in change["value"]]
        elif op == "sp_owners":
            sp_owners[target[0]] = [_owner(e.split("@")[0].replace(".", " ").title(), e) for e in change["value"]]
        elif op == "secret":
            value = change.get("value") or {}
            target[8] = True
            target[9] = [
                *target[9],
                ("app", "secret", value.get("name", ""), int(value.get("days", 30)), value.get("name") or "new"),
            ]
        elif op == "remove_credentials":
            target[9] = []
        elif op == "tag":
            target[6] = [*target[6], change["value"]]
    sps, apps = [], []
    for row in rows:
        sp, app = _objects(row, today)
        for obj in (sp, app):
            if obj is not None and row[0] in renamed:
                obj["displayName"] = renamed[row[0]]
        if sp is not None and row[0] in sp_owners:
            sp["owners"] = copy.deepcopy(sp_owners[row[0]])
        if sp is not None and row[1] == VENDOR_B:
            sp["publisherName"] = VERIFIED.get(row[0]) or "Unverified publisher (synthetic)"
            sp["verifiedPublisher"] = {"displayName": VERIFIED.get(row[0]) or None}
        if sp is not None:
            sps.append(sp)
        if app is not None:
            apps.append(app)
    if config.get("simulated_blueprint"):
        name = "Benefits Agent Blueprint"
        common = {"appId": _id(f"app/{name}"), "displayName": name, "description": "", "tags": [], "owners": [DANA]}
        apps.append(
            {
                **copy.deepcopy(common),
                "@odata.type": "#microsoft.graph.agentIdentityBlueprint",
                "id": _id(f"application/{name}"),
                "keyCredentials": [],
                "passwordCredentials": [],
            }
        )
        sps.append(
            {
                **copy.deepcopy(common),
                "@odata.type": "#microsoft.graph.agentIdentityBlueprintPrincipal",
                "id": _id(name),
                "accountEnabled": True,
                "appOwnerOrganizationId": OUR_TENANT,
                "servicePrincipalType": "Application",
                "keyCredentials": [],
                "passwordCredentials": [],
            }
        )
    if config.get("simulated_agents_in_list"):
        sps.extend(agent_identities())
    return sps, apps


def agent_identities():
    return [
        {
            "@odata.type": "#microsoft.graph.agentIdentity",
            "id": _id(f"agent/{name}"),
            "displayName": name,
            "description": description,
            "accountEnabled": enabled,
            "servicePrincipalType": "ServiceIdentity",
            "agentIdentityBlueprintId": _id(f"blueprint/{name}"),
            "createdByAppId": _id("app/HR Helpdesk Agent"),
            "tags": [],
        }
        for name, description, enabled in AGENT_IDENTITIES
    ]


def skus():
    return [
        {
            "id": f"{OUR_TENANT}_{sku}",
            "skuId": sku,
            "skuPartNumber": part,
            "appliesTo": "User",
            "capabilityStatus": status,
            "consumedUnits": consumed,
            "prepaidUnits": {"enabled": enabled, "suspended": suspended, "warning": warning, "lockedOut": 0},
        }
        for sku, part, status, enabled, suspended, warning, consumed in SKUS
    ]


USER_FIELDS = (
    "id",
    "displayName",
    "mail",
    "userPrincipalName",
    "accountEnabled",
    "userType",
    "createdDateTime",
    "assignedLicenses",
    "licenseAssignmentStates",
    "signInActivity",
)
LICENCE_FIELDS = frozenset({"assignedlicenses", "signinactivity"})  # a licence usage read, not the owner-name probe


def _selected(query: Mapping[str, list[str]]) -> set[str]:
    return {f.strip().lower() for f in (query.get("$select") or [""])[0].split(",") if f.strip()}


def _json(status: int, body: Any, headers: Mapping[str, str] | None = None) -> Reply:
    return Reply(status, {"content-type": "application/json", **(headers or {})}, json.dumps(body).encode())


def _error(status: int, code: str, message: str, headers: Mapping[str, str] | None = None) -> Reply:
    return _json(
        status, {"error": {"code": code, "message": message, "innerError": {"request-id": "simulated"}}}, headers
    )


def _base_name(sp: dict[str, Any]) -> str:
    """The simulated app's own name (its object ID is derived from it; a rename changes only the display name)."""
    return next((r[0] for r in [*BASE, *SHADOW_ROWS] if _id(r[0]) == sp["id"]), str(sp.get("displayName") or ""))


def _hide_owner(owner):
    """What Graph returns for an owner the caller can't read: the id and type, every other property null."""
    return {
        "@odata.type": owner.get("@odata.type"),
        "id": owner["id"],
        "displayName": None,
        "mail": None,
        "userPrincipalName": None,
    }


class Transport:
    """The token endpoint and the Graph reads, answered from the synthetic tenant. `calls` records (method, url)."""

    def __init__(self, connector=None, config=None, shadow=False):
        self.config = dict(config if config is not None else getattr(connector, "config", {}) or {})
        self.shadow = shadow or bool(self.config.get("simulated_shadow_apps"))
        self.calls: list[tuple[str, str]] = []
        self.throttled = 0
        self.failed = 0
        self.reads: dict[str, int] = {}
        self.expired = False

    def granted(self) -> set[str]:
        return set(self.config.get("simulated_permissions", GRANTED))

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Reply:
        parts = urllib.parse.urlsplit(url)
        self.calls.append((method, url))
        if parts.scheme != "https" or parts.hostname not in (LOGIN_HOST, GRAPH_HOST):
            return _error(400, "BadRequest", "The simulator only answers Microsoft hosts.")
        if parts.hostname == LOGIN_HOST:
            if method == "POST" and parts.path.endswith("/oauth2/v2.0/token"):
                return self._token(body or b"")
            return _error(400, "BadRequest", "The simulator only issues tokens.")
        if method != "GET":
            return _error(400, "BadRequest", "The simulator only reads.")
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return _error(401, "InvalidAuthenticationToken", "Access token is empty or invalid.")
        path = parts.path
        if self.config.get("simulated_token_expires") and not self.expired:
            self.expired = True
            return _error(401, "InvalidAuthenticationToken", "Lifetime validation failed, the token is expired.")
        errors = self.config.get("simulated_errors") or {}
        if path in errors:  # echoes the bearer token, which must never reach a message
            return _error(int(errors[path]), "Request_BadRequest", f"Bad request with {headers.get('Authorization')}")
        fail = self.config.get("simulated_fail_after") or {}
        if fail.get("path") == path:
            self.reads[path] = self.reads.get(path, 0) + 1
            if self.reads[path] > int(fail.get("after", 0)) and self.failed < int(fail.get("times", 1)):
                self.failed += 1
                return _error(int(fail.get("status", 500)), "UnknownError", "The service had a problem.")
        throttle = self.config.get("simulated_throttle") or {}
        if throttle.get("path") == path and self.throttled < int(throttle.get("times", 1)):
            self.throttled += 1
            retry = throttle.get("retry_after")
            status = int(throttle.get("status", 429))
            return _error(status, "TooManyRequests", "Too many requests.", {"retry-after": str(retry)} if retry else {})
        if any(path.endswith(p) for p in self.config.get("simulated_empty") or ()):
            return _json(200, {"value": []})
        query = urllib.parse.parse_qs(parts.query)
        if path == "/v1.0/users" and LICENCE_FIELDS & _selected(query):
            return self._licensed_users(path, query)
        owners_of = OWNERS_PATH.match(path)
        sync_of = SYNC_PATH.match(path)
        needed = NEEDS.get(path) or (APPLICATION_READ if owners_of else None)
        if sync_of:
            needed = ("Synchronization.Read.All",)
        if needed is None:
            return _error(400, "BadRequest", "The simulator doesn't answer this read.")
        if not set(needed) & self.granted():
            return _error(403, "Authorization_RequestDenied", "Insufficient privileges to complete the operation.")
        if path == "/v1.0/organization":
            return _json(200, {"value": [{"id": OUR_TENANT, "displayName": "Contoso (synthetic)"}]})
        if path == "/v1.0/oauth2PermissionGrants":
            return self._paged(path, query, oauth2_grants(), int(self.config.get("simulated_grant_page") or PAGE))
        if path == "/v1.0/auditLogs/signIns":
            return self._sign_ins(path, query)
        if path == CA_PATH:
            if self.config.get("simulated_no_premium"):
                return _error(403, NON_PREMIUM, "Neither tenant is B2C or tenant doesn't have premium license")
            policies = self.config.get("simulated_ca_policies")
            items = list(policies) if isinstance(policies, list) else ca_policies()
            return self._paged(path, query, items, int(self.config.get("simulated_ca_page") or PAGE))
        if sync_of:
            sps, _apps = tenant(self.config, shadow=self.shadow)
            found = next((o for o in sps if o["id"] == sync_of.group(1)), None)
            if found is None:
                return _error(404, "Request_ResourceNotFound", "Resource does not exist.")
            return _json(200, {"value": sync_jobs(_base_name(found))})
        if path == "/v1.0/users":
            return _json(200, {"value": [{"id": DANA["id"]}]})
        if path == "/v1.0/subscribedSkus":
            return _json(200, {"value": skus()})
        if path.endswith("/microsoft.graph.agentIdentity"):
            return _json(200, {"value": agent_identities()})
        sps, apps = tenant(self.config, shadow=self.shadow)
        objects = sps if path.startswith("/v1.0/servicePrincipals") else apps
        if "User.ReadBasic.All" not in self.granted():
            for obj in objects:
                obj["owners"] = [_hide_owner(o) for o in obj.get("owners", [])]
        if owners_of:
            found = next((o for o in objects if o["id"] == owners_of.group(2)), None)
            if found is None:
                return _error(404, "Request_ResourceNotFound", "Resource does not exist.")
            objects = found.get("owners", [])
        elif "$expand" not in query:
            for obj in objects:
                obj.pop("owners", None)
        else:
            for obj in objects:
                obj["owners"] = obj.get("owners", [])[:EXPAND_LIMIT]
        top = int(query.get("$top", [str(PAGE)])[0])
        skip = int(query.get("$skiptoken", ["0"])[0])
        page: dict[str, Any] = {"value": objects[skip : skip + min(top, PAGE)]}
        if skip + min(top, PAGE) < len(objects) and top >= PAGE:
            rest = {k: v[0] for k, v in query.items() if k != "$skiptoken"}
            rest["$skiptoken"] = str(skip + PAGE)
            page["@odata.nextLink"] = f"https://{GRAPH_HOST}{path}?{urllib.parse.urlencode(rest, safe='$(),=')}"
        return _json(200, page)

    def _paged(self, path: str, query: dict[str, list[str]], items: list[Any], size: int) -> Reply:
        """`items` `size` at a time, in the order given, with a next link that keeps the query."""
        skip = int(query.get("$skiptoken", ["0"])[0])
        page: dict[str, Any] = {"value": items[skip : skip + size]}
        if skip + size < len(items):
            rest = {k: v[0] for k, v in query.items() if k != "$skiptoken"}
            rest["$skiptoken"] = str(skip + size)
            page["@odata.nextLink"] = f"https://{GRAPH_HOST}{path}?{urllib.parse.urlencode(rest, safe='$(),=:')}"
        return _json(200, page)

    def _licensed_users(self, path: str, query: dict[str, list[str]]) -> Reply:
        selected = _selected(query)
        granted = self.granted()
        if not {"User.Read.All", "Directory.Read.All"} & granted:
            return _error(403, "Authorization_RequestDenied", "Insufficient privileges to complete the operation.")
        with_activity = "signinactivity" in selected
        if with_activity and "AuditLog.Read.All" not in granted:
            return _error(403, "Authorization_RequestDenied", "Insufficient privileges to complete the operation.")
        if with_activity and self.config.get("simulated_no_premium"):
            return _error(403, NON_PREMIUM, "Neither tenant is B2C or tenant doesn't have premium license")
        top = int((query.get("$top") or ["100"])[0])
        if not 1 <= top <= (500 if with_activity else 999):
            return _error(400, "Request_UnsupportedQuery", "Invalid page size specified.")
        users = [] if self.config.get("simulated_no_users") else licensed_users(self.config)
        wanted = {name for name in USER_FIELDS if name.lower() in selected}
        if self.config.get("simulated_users_without_licences"):
            wanted.discard("assignedLicenses")
        items = [{k: v for k, v in u.items() if k in wanted} for u in users]
        return self._paged(path, query, items, min(top, int(self.config.get("simulated_user_page") or PAGE)))

    def _sign_ins(self, path: str, query: dict[str, list[str]]) -> Reply:
        if self.config.get("simulated_no_premium"):
            return _error(403, NON_PREMIUM, "Neither tenant is B2C or tenant doesn't have premium license")
        text = (query.get("$filter") or [""])[0]
        ge = re.search(r"createdDateTime ge (\S+)", text)
        le = re.search(r"createdDateTime le (\S+)", text)
        start = _when(ge.group(1)) if ge else None
        end = _when(le.group(1)) if le else datetime.max.replace(tzinfo=UTC)
        if start is None or end is None:  # a real tenant times out on an unbounded read
            return _error(400, "Request_UnsupportedQuery", "Filter the sign-ins by createdDateTime.")
        top = int((query.get("$top") or ["1000"])[0])
        if not 1 <= top <= 1000:
            return _error(400, "Request_UnsupportedQuery", "$top must be between 1 and 1000.")
        size = min(top, int(self.config.get("simulated_signin_page") or PAGE))
        return self._paged(path, query, sign_ins(start, end), size)

    def _token(self, body: bytes) -> Reply:
        form = urllib.parse.parse_qs(body.decode())
        secret = (form.get("client_secret") or [""])[0]
        if (form.get("grant_type") or [""])[0] != "client_credentials" or not secret:
            return _json(400, {"error": "invalid_request"})
        if self.config.get("simulated_token_echo"):  # a refusal that repeats the secret: it must never be kept
            return _json(400, {"error": "invalid_request", "error_description": f"AADSTS900144: bad secret {secret}"})
        if secret == "wrong-secret":  # noqa: S105 (lets a test see a refused secret)
            return _json(
                401,
                {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided."},
            )
        return _json(200, {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3599})
