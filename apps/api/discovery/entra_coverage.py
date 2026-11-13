"""SSO and MFA coverage from Microsoft Entra ID: for every catalog application
linked to an Entra app, whether it is behind single sign-on, whether SCIM provisioning is on, and whether conditional
access requires multifactor authentication, for all users or only some. Its own source (`entra_coverage`, capability
discovery.entra), so an organization without Entra ID P1 or without these consents can leave it off; the Entra source
(`entra`) is unchanged.

Microsoft Learn, checked (docs/PERMISSIONS.md, "Discovery source:
`entra_coverage`"):

- Single sign-on: the service principal's `preferredSingleSignOnMode` (`password`, `saml`, `notSupported`, `oidc`),
  listed with `$select` (`GET /v1.0/servicePrincipals`, Application.Read.All). It is null for older SAML and OIDC apps,
  so null is "unknown", never "no SSO"; a SAML token signing certificate (`preferredTokenSigningKeyThumbprint`, or
  `samlSingleSignOnSettings` set) makes a null mode SAML (inferred). `password` is Entra's password-based single
  sign-on: behind the My Apps portal, but not federated.
- Provisioning: `GET /v1.0/servicePrincipals/{id}/synchronization/jobs` with Synchronization.Read.All (never
  Application.ReadWrite.OwnedBy, which writes). On when a job's `schedule.state` is Active and its `status.code` is
  neither Paused nor Quarantine. One call per service principal, so a run reads at most SCIM_LIMIT apps, least
  recently read first; the others keep their last answer until their turn.
- Conditional access: `GET /v1.0/identity/conditionalAccess/policies` with Policy.Read.All, once per run. The tenant
  needs Microsoft Entra ID P1. Throttled at one request per second per tenant, and a 429 there carries no Retry-After:
  pages are read CA_PACE seconds apart and a throttled read backs off on its own (2, 4, 8, 16 seconds) before the run
  gives up on it. A refusal (no P1, no Policy.Read.All) or giving up never fails the run: single sign-on and
  provisioning are still reported, MFA is "unknown" with the reason, and the run's notes say so.

How a policy is judged for one app (evaluate_policy): it must be `enabled` (report-only and disabled policies don't
enforce anything; report-only ones are listed); the app must be in `includeApplications` (its app ID, or `All`) and
not in `excludeApplications`; `Office365` and `MicrosoftAdminPortals` may cover a Microsoft-published app (unknown) and
never a third-party one; an `applicationFilter` can't be evaluated, so a policy whose coverage of the app depends on
one is unknown. Users: `includeUsers` All is everyone (exclusions such as break-glass accounts are counted and shown,
not judged); roles, groups, guests or listed users are some users, as is a policy limited to some locations,
platforms, client apps or risk levels. MFA is required when `grantControls.builtInControls` has `mfa` or the
authentication strength's `requirementsSatisfied` is `mfa`, unless the operator is OR with another control beside it
(then MFA is one option among others).

The run lists every service principal and keeps those whose application ID a catalog application carries (its
`entra_app_id`, or a linked or created candidate of an `entra` or `entra_shadow_it` connector). It writes no
observation: coverage rows (AppCoverage), in the run's transaction (`apply_fetched`). Nothing writes to Entra or to a
catalog record. If the listing comes back empty while the connector has coverage rows, the run is refused rather
than clearing them (a tenant can't lose every enterprise application between two runs; a broken read can).

The tenant ID `simulated` selects the synthetic tenant (discovery/entra_simulator.py)."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from django.db.models.functions import Lower
from django.utils import timezone

from . import entra
from .entra_shadow_it import Pacer
from .graph_source import (
    APPLICATION_READ_ALL,
    GRAPH,
    GRAPH_AUDIENCE,
    POLICY_READ_ALL,
    SIMULATED,
    SYNCHRONIZATION_READ_ALL,
    Client,
    DeadlineReached,
    Fetched,
    Probe,
    Refused,
    SignInRefused,
    SourceError,
    Throttled,
    coverage_simulator,
    transports,
)
from .models import AppCoverage, DecisionKind, MfaCoverage, Observation, ObservationKind, ProvisioningState, Source
from .models import SsoMode as Sso

# ---- the connector's contract with the engine ---------------------------------------------------------------------

KINDS: list[str] = []  # no observations: coverage rows (apply_fetched)
CONNECTION = "microsoft"
CONFIG_FIELDS: tuple[str, ...] = ()
SCOPE_DEFAULTS: dict[str, Any] = {"provisioning": True, "conditional_access": True}
FILTERS: dict[str, str] = {}
PERMISSIONS = [APPLICATION_READ_ALL, SYNCHRONIZATION_READ_ALL, POLICY_READ_ALL]
CA_URL = f"{GRAPH}/identity/conditionalAccess/policies"
PROBES = [
    Probe("Read enterprise applications", f"{GRAPH}/servicePrincipals?$top=1&$select=id", APPLICATION_READ_ALL),
    Probe(
        "Read provisioning jobs",
        f"{GRAPH}/servicePrincipals/{{id}}/synchronization/jobs",
        SYNCHRONIZATION_READ_ALL,
        when="provisioning",
    ),
    Probe("Read conditional access policies", CA_URL, POLICY_READ_ALL, when="conditional_access"),
]
simulator = coverage_simulator

# The sources whose linked or created candidates say which Entra app a record is (their external ID is the app ID).
APP_SOURCES = (Source.ENTRA, Source.ENTRA_SHADOW_IT)
SP_FIELDS = (
    "id,appId,displayName,appOwnerOrganizationId,servicePrincipalType,preferredSingleSignOnMode,"
    "preferredTokenSigningKeyThumbprint,samlSingleSignOnSettings"
)
PAGE_LIMIT = 500
SCIM_LIMIT = 100  # provisioning reads a run makes at most (one call per service principal)
CA_PACE = 1.1  # seconds between conditional access reads on a real tenant: one request a second per tenant
CA_BACKOFF = 2.0  # seconds before the first retry of a throttled conditional access read; doubles each time
CA_ATTEMPTS = 4  # retries of a throttled conditional access read before the run gives up on it
POLICIES_MAX = 20  # policies kept per app
# How long a known answer is kept when a later run can't read it for a transient reason (throttling, a server error,
# Graph unreachable). Older, it becomes unknown rather than look current.
CARRY_MAX = timedelta(days=7)
# What Graph answers a tenant without P1. Learn doesn't name the code for conditional access; this is the sign-in logs'
# (shadow IT). Any 403 is reported the same way, naming both the licence and the permission.
LICENCE_CODES = frozenset({"Authentication_RequestFromNonPremiumTenantOrB2CTenant"})
MICROSOFT_GROUPS = {"office365": "Office 365", "microsoftadminportals": "Microsoft Admin Portals"}

# Module-level so tests can set the time.
clock = timezone.now


def scope_of(connector) -> dict[str, Any]:
    return dict(SCOPE_DEFAULTS) | dict(connector.scope or {})


def validate(config, scope, errors):
    for key in SCOPE_DEFAULTS:
        if key in scope and not isinstance(scope[key], bool):
            errors[f"scope.{key}"] = ["Choose yes or no."]


# ---- single sign-on -------------------------------------------------------------------------------------------------

SSO_MODES = {"saml": Sso.SAML, "oidc": Sso.OIDC, "password": Sso.PASSWORD, "notsupported": Sso.NONE}


def sso_of(sp: dict[str, Any]) -> tuple[str, str, str]:
    """(sso, the raw mode, why) for one service principal."""
    raw = sp.get("preferredSingleSignOnMode")
    mode = raw.strip()[:40] if isinstance(raw, str) else ""
    found = SSO_MODES.get(mode.lower())
    if found == Sso.SAML:
        return found, mode, "Entra: SAML single sign-on."
    if found == Sso.OIDC:
        return found, mode, "Entra: OpenID Connect single sign-on."
    if found == Sso.PASSWORD:
        return (
            found,
            mode,
            "Entra: password-based single sign-on (My Apps stores and replays a password; not federated).",
        )
    if found == Sso.NONE:
        return found, mode, "Entra: single sign-on isn't set up for it (preferredSingleSignOnMode notSupported)."
    if mode:
        return Sso.UNKNOWN, mode, f"Entra gave a single sign-on mode Softwarden doesn't know ({mode})."
    settings = sp.get("samlSingleSignOnSettings")
    relay = settings.get("relayState") if isinstance(settings, dict) else None
    if sp.get("preferredTokenSigningKeyThumbprint"):
        return Sso.SAML, "", "Entra gives no preferred mode, but the app has a SAML token signing certificate: SAML."
    if isinstance(relay, str) and relay.strip():
        return Sso.SAML, "", "Entra gives no preferred mode, but the app has SAML settings (a relay state): SAML."
    return (
        Sso.UNKNOWN,
        "",
        "Entra gives no preferred single sign-on mode (usual for older SAML and OIDC apps), so it isn't known.",
    )


# ---- provisioning ---------------------------------------------------------------------------------------------------


def provisioning_of(jobs: Any) -> tuple[str, str]:
    """(on or off, why) from a service principal's synchronization jobs."""
    rows = [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []
    if not rows:
        return ProvisioningState.OFF, "No provisioning job."

    def state(job: dict[str, Any], part: str, key: str) -> str:
        value = job.get(part)
        return str(value.get(key) or "").lower() if isinstance(value, dict) else ""

    running = [
        j
        for j in rows
        if state(j, "schedule", "state") == "active" and state(j, "status", "code") not in ("paused", "quarantine")
    ]
    if running:
        return ProvisioningState.ON, f"Provisioning is on ({len(running)} job(s) running on schedule)."
    codes = {state(j, "status", "code") for j in rows} | {state(j, "schedule", "state") for j in rows}
    if "quarantine" in codes:
        return ProvisioningState.OFF, "Provisioning is in quarantine: Entra stopped it after repeated errors."
    if "paused" in codes:
        return ProvisioningState.OFF, "Provisioning is paused."
    return ProvisioningState.OFF, "Provisioning is set up but its schedule isn't active."


# ---- conditional access ---------------------------------------------------------------------------------------------


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _lower(values: Any) -> list[str]:
    return [str(v).strip().lower() for v in values] if isinstance(values, list) else []


def requires_mfa(grant: Any) -> tuple[bool, bool]:
    """(MFA required, MFA only one option among others) from a policy's grantControls."""
    if not isinstance(grant, dict):
        return False, False
    builtin = _lower(grant.get("builtInControls"))
    strength = grant.get("authenticationStrength")
    by_strength = isinstance(strength, dict) and str(strength.get("requirementsSatisfied") or "").lower() == "mfa"
    if "mfa" not in builtin and not by_strength:
        return False, False
    controls = (
        len(builtin)
        + len(grant.get("customAuthenticationFactors") or [])
        + len(grant.get("termsOfUse") or [])
        + (1 if isinstance(strength, dict) else 0)
    )
    if str(grant.get("operator") or "OR").upper() == "OR" and controls > 1:
        return False, True
    return True, False


# The conditions evaluate_policy understands. Any other condition with a value narrows a policy to some sign-ins, so
# a shape Softwarden doesn't know can never make an app "MFA for all users".
KNOWN_CONDITIONS = frozenset(
    {
        "applications",
        "users",
        "clientApplications",
        "clientAppTypes",
        "platforms",
        "locations",
        "signInRiskLevels",
        "userRiskLevels",
        "servicePrincipalRiskLevels",
        "insiderRiskLevels",
        "authenticationFlows",
        "devices",
    }
)


def _filled(value: Any) -> bool:
    """A condition value that says something: not null, blank, empty, false or made only of such values."""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | tuple):
        return any(_filled(v) for v in value)
    if isinstance(value, dict):
        return any(_filled(v) for k, v in value.items() if not str(k).startswith("@"))
    return True


def _narrowed(conditions: dict[str, Any]) -> str:
    """Why a policy covers only some sign-ins (locations, platforms, client apps, risk, authentication flows, devices,
    workload identities, or a condition Softwarden doesn't know), or ""."""
    reasons = []
    locations = conditions.get("locations")
    if isinstance(locations, dict) and (
        _lower(locations.get("includeLocations")) not in ([], ["all"]) or _lower(locations.get("excludeLocations"))
    ):
        reasons.append("locations")
    platforms = conditions.get("platforms")
    if isinstance(platforms, dict) and (
        _lower(platforms.get("includePlatforms")) not in ([], ["all"]) or _lower(platforms.get("excludePlatforms"))
    ):
        reasons.append("device platforms")
    client_apps = _lower(conditions.get("clientAppTypes"))
    if client_apps and "all" not in client_apps:
        reasons.append("client apps")
    if any(_filled(conditions.get(k)) for k in ("signInRiskLevels", "userRiskLevels", "servicePrincipalRiskLevels")):
        reasons.append("risk levels")
    if _filled(conditions.get("insiderRiskLevels")):
        reasons.append("insider risk")
    if _filled(conditions.get("authenticationFlows")):
        reasons.append("authentication flows")
    devices = conditions.get("devices")
    if isinstance(devices, dict) and (
        _filled(devices.get("deviceFilter"))
        or _lower(devices.get("includeDevices")) not in ([], ["all"])
        or _filled(devices.get("excludeDevices"))
    ):
        reasons.append("devices")
    if _filled(conditions.get("clientApplications")):
        reasons.append("workload identities")
    unknown = sorted(
        k for k, v in conditions.items() if k not in KNOWN_CONDITIONS and not str(k).startswith("@") and _filled(v)
    )
    if unknown:
        reasons.append(f"other conditions: {', '.join(unknown)[:80]}")
    return ", ".join(reasons)


def evaluate_policy(policy: dict[str, Any], app_id: str, microsoft: bool) -> dict[str, Any] | None:
    """What one policy does for one app, or None when it doesn't cover it (or is disabled): {id, name, state,
    applies (yes or maybe), users (all or some), mfa, effect, exclusions, why}."""
    state = str(policy.get("state") or "")
    if state not in ("enabled", "enabledForReportingButNotEnforced"):
        return None
    conditions = _dict(policy.get("conditions"))
    apps = _dict(conditions.get("applications"))
    include, exclude = set(_lower(apps.get("includeApplications"))), set(_lower(apps.get("excludeApplications")))
    if app_id in exclude:
        return None
    why = ""
    if "all" in include or app_id in include:
        applies = "yes"
    elif microsoft and include & set(MICROSOFT_GROUPS):
        group = next(MICROSOFT_GROUPS[g] for g in sorted(include) if g in MICROSOFT_GROUPS)
        applies, why = "maybe", f"it targets the {group} app group, which may include this app"
    else:
        applies = "no"
    excluded_groups = sorted(exclude & set(MICROSOFT_GROUPS))
    if microsoft and excluded_groups and applies == "yes":
        group = MICROSOFT_GROUPS[excluded_groups[0]]
        applies, why = "maybe", f"it excludes the {group} app group, which may include this app"
    flt = apps.get("applicationFilter")
    mode = str(flt.get("mode") or "").lower() if isinstance(flt, dict) and flt.get("rule") else ""
    if mode == "exclude" and applies == "yes":
        applies, why = "maybe", "an application filter may exclude this app, and filters can't be evaluated"
    elif mode == "include" and applies == "no":
        applies, why = "maybe", "an application filter may include this app, and filters can't be evaluated"
    if applies == "no":
        return None
    users = _dict(conditions.get("users"))
    included = _lower(users.get("includeUsers"))
    others = _lower(users.get("includeGroups")) + _lower(users.get("includeRoles"))
    guests = bool(users.get("includeGuestsOrExternalUsers"))
    if "all" in included:
        audience = "all"
    elif not [u for u in included if u != "none"] and not others and not guests:
        return None  # nobody: a policy for workload identities, or users "None"
    else:
        audience = "some"
        why = why or "it covers only some users (roles, groups, guests or listed people)"
    narrowed = _narrowed(conditions)
    if narrowed and audience == "all":
        audience = "some"
        why = why or f"it applies only to some sign-ins ({narrowed})"
    exclusions: int = sum(len(_lower(users.get(key))) for key in ("excludeUsers", "excludeGroups", "excludeRoles"))
    exclusions += 1 if users.get("excludeGuestsOrExternalUsers") else 0
    mfa, optional = requires_mfa(policy.get("grantControls"))
    blocked = "block" in _lower(_dict(policy.get("grantControls")).get("builtInControls"))
    if state != "enabled":
        effect = "report_only"
    elif blocked:
        effect, mfa = "blocked", False
    elif not mfa:
        effect = "mfa_optional" if optional else "no_mfa"
    elif applies == "maybe":
        effect = "may_require_mfa"
    else:
        effect = "mfa_all_users" if audience == "all" else "mfa_some_users"
    return {
        "id": str(policy.get("id") or "")[:64],
        "name": str(policy.get("displayName") or "")[:200],
        "state": state,
        "applies": applies,
        "users": audience,
        "mfa": mfa,
        "effect": effect,
        "exclusions": exclusions,
        "why": why[:200],
    }


EFFECT_ORDER = (
    "blocked",
    "mfa_all_users",
    "mfa_some_users",
    "may_require_mfa",
    "mfa_optional",
    "no_mfa",
    "report_only",
)


def mfa_coverage(policies: list[dict[str, Any]], app_id: str, microsoft: bool) -> tuple[str, str, list[dict]]:
    """(mfa, why, the policies that cover or may cover the app) from the tenant's policies."""
    found = [m for p in policies if isinstance(p, dict) and (m := evaluate_policy(p, app_id, microsoft))]
    found.sort(key=lambda m: (EFFECT_ORDER.index(m["effect"]), m["name"].lower(), m["id"]))
    by = {e: [m for m in found if m["effect"] == e] for e in EFFECT_ORDER}
    kept = found[:POLICIES_MAX]
    # Blocked only when nobody is let through: a block "for all users except a group" is how one group is allowed
    # in, so it decides nothing and the other policies say what that group gets.
    everyone_blocked = [
        m for m in by["blocked"] if m["applies"] == "yes" and m["users"] == "all" and not m["exclusions"]
    ]
    if everyone_blocked:
        m = everyone_blocked[0]
        return MfaCoverage.BLOCKED, f"Sign-in is blocked for all users by “{m['name']}”.", kept
    if by["mfa_all_users"]:
        m = by["mfa_all_users"][0]
        but = f", with {m['exclusions']} exclusion(s)" if m["exclusions"] else ""
        return MfaCoverage.ALL_USERS, f"Required for all users by “{m['name']}”{but}.", kept
    if by["mfa_some_users"]:
        m = by["mfa_some_users"][0]
        return MfaCoverage.SOME_USERS, f"Required only for some users: “{m['name']}” ({m['why']}).", kept
    if by["may_require_mfa"]:
        m = by["may_require_mfa"][0]
        return MfaCoverage.UNKNOWN, f"“{m['name']}” may require it: {m['why']}.", kept
    if by["mfa_optional"]:
        m = by["mfa_optional"][0]
        return MfaCoverage.NONE, f"No enabled policy requires MFA: “{m['name']}” accepts MFA or another control.", kept
    if [m for m in by["report_only"] if m["mfa"]]:
        m = next(m for m in by["report_only"] if m["mfa"])
        return MfaCoverage.NONE, f"Only a report-only policy asks for MFA (“{m['name']}”); it isn't enforced.", kept
    return MfaCoverage.NONE, "No enabled conditional access policy requires MFA for it.", kept


@dataclass
class PolicyRead:
    policies: list[dict[str, Any]] | None  # None: not read (`why`)
    why: str = ""
    note: str = ""
    retries: int = 0
    transient: bool = False  # throttled out, unreachable, a server error: last run's answers are kept


def read_policies(client: Client, pace: float) -> PolicyRead:
    """Every conditional access policy, once, paced; a throttled read backs off on its own (no Retry-After there). A
    refusal or throttling it can't wait out is a PolicyRead without policies, with the reason: never a failed run."""
    out: list[dict[str, Any]] = []
    url, attempt, retries = CA_URL, 0, 0
    pacer = Pacer(pace)
    for _ in range(PAGE_LIMIT * (CA_ATTEMPTS + 1)):
        pacer.wait()
        try:
            page = client.get(url, "read conditional access policies", POLICY_READ_ALL, retry_throttled=False)
        except Throttled as e:
            delay = e.retry_after if e.retry_after is not None else CA_BACKOFF * (2**attempt)
            if attempt >= CA_ATTEMPTS or not client.budget.wait(attempt, delay):
                why = "Unknown: Microsoft Graph kept throttling the conditional access read; the next run tries again."
                note = (
                    "Microsoft Graph kept throttling the conditional access read; MFA shows the last run's answers "
                    "where there were any. The next run tries again."
                )
                return PolicyRead(None, why, note, retries, transient=True)
            attempt += 1
            retries += 1
            continue
        except Refused as e:
            tag = f"{e.status} {e.code}".strip()
            if e.status == 403 and e.code in LICENCE_CODES:
                why = "Unknown: needs Microsoft Entra ID P1 (conditional access couldn't be read)."
                note = (
                    f"Conditional access wasn't read ({tag}): it needs a Microsoft Entra ID P1 license in the tenant. "
                    "MFA is unknown; single sign-on and provisioning are reported."
                )
            elif e.status == 403:
                why = "Unknown: conditional access couldn't be read (Policy.Read.All, Entra ID P1)."
                note = (
                    f"Conditional access wasn't read ({tag}): grant Policy.Read.All with admin consent; the tenant "
                    "also needs Microsoft Entra ID P1. MFA is unknown; single sign-on and provisioning are reported."
                )
            elif e.status == 401:
                why = f"Unknown: conditional access couldn't be read ({tag})."
                note = f"Conditional access wasn't read ({tag}): the app's token wasn't accepted. MFA is unknown."
            else:
                why = f"Unknown: conditional access couldn't be read ({tag})."
                note = (
                    f"Conditional access wasn't read ({tag}); MFA shows the last run's answers where there were any. "
                    "The next run tries again."
                )
                return PolicyRead(None, why, note, retries, transient=True)
            return PolicyRead(None, why, note, retries)
        except (DeadlineReached, SignInRefused):
            raise
        except SourceError as e:  # unreachable, a broken answer
            why = "Unknown: conditional access couldn't be read this run."
            note = f"{e} MFA shows the last run's answers where there were any; the next run tries again."
            return PolicyRead(None, why, note, retries, transient=True)
        attempt = 0
        out.extend(v for v in page.get("value") or [] if isinstance(v, dict))
        url = page.get("@odata.nextLink") or ""
        if not url:
            return PolicyRead(out, retries=retries)
    why = "Unknown: the tenant has more conditional access policies than a run reads."
    return PolicyRead(None, why, why)


# ---- the run -------------------------------------------------------------------------------------------------------


@dataclass
class Coverage:
    """What `apply_fetched` writes in the run's transaction: {app ID: row fields}, and counts."""

    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    listed: int = 0
    policies: int | None = None
    scim_read: int = 0
    ca_retries: int = 0
    carried_mfa: int = 0
    carried_scim: int = 0


class CoverageFetched(Fetched):
    coverage: Coverage | None = None


def linked_app_ids(org_id) -> set[str]:
    """The Entra application IDs the organization's live applications carry: their own `entra_app_id`, or a linked or
    created candidate of an Entra or shadow IT connector."""
    from catalog.models import Application, Kind

    records = (
        Application.objects.filter(organization_id=org_id, resource__archived_at__isnull=True)
        .exclude(entra_app_id="")
        .annotate(app=Lower("entra_app_id"))
        .values_list("app", flat=True)
    )
    linked = (
        Observation.objects.filter(
            organization_id=org_id,
            kind=ObservationKind.APPLICATION,
            connector__source__in=APP_SOURCES,
            decision__resource__kind=Kind.APPLICATION,
            decision__resource__archived_at__isnull=True,
        )
        .exclude(decision__decision=DecisionKind.DISMISSED)
        .values_list("external_id", flat=True)
    )
    return {a for a in [*records, *linked] if entra.GUID.match(a or "")}


EMPTY_LISTING = (
    "Microsoft Graph listed no enterprise applications, but this connector has coverage for {count} application(s) "
    "from earlier runs. Nothing was changed: a tenant doesn't lose every enterprise application between two runs, so "
    "the listing is more likely incomplete. Check the connection and Application.Read.All, then run it again."
)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _day(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%d")


def _read_provisioning(client, coverage, previous, now, notes) -> int:
    """Each app's provisioning jobs, at most SCIM_LIMIT reads, least recently read first; the others keep their last
    answer. A 401 or 403 stops the reads and makes the rest unknown (with why). A transient failure (a server error on
    one app; throttling past the budget or an unreachable Graph, which stop the reads) keeps the last run's answer for
    the apps it hit. Returns the reads made."""
    items = coverage.items
    order = sorted(items, key=lambda a: ((previous[a].scim_checked_at or EPOCH) if a in previous else EPOCH, a))
    read, refused, transient = 0, "", ""
    capped = f"Not read yet: a run reads at most {SCIM_LIMIT} apps' provisioning, least recently read first."

    def keep(item, before, why) -> None:
        """The last run's answer, if it read one for the same service principal (a tenant change or a re-created app
        is another one), and, after a transient failure, at most CARRY_MAX old; else unknown with `why`. Apps past
        the cap (`capped`, a round-robin) keep it whatever its age."""
        read_at = (
            before.scim_checked_at
            if before is not None and before.service_principal_id == item["service_principal_id"]
            else None
        )
        if read_at is not None and why != capped and now - read_at >= CARRY_MAX:
            item.update(
                scim=ProvisioningState.UNKNOWN,
                scim_detail=f"Unknown: provisioning hasn't been read since {_day(read_at)} "
                f"(over {CARRY_MAX.days} days); {why.removeprefix('Unknown: ')}",
            )
            return
        if before is not None and read_at is not None:
            item.update(scim=before.scim, scim_detail=before.scim_detail, scim_checked_at=read_at)
            if why != capped:
                coverage.carried_scim += 1
        else:
            item.update(scim=ProvisioningState.UNKNOWN, scim_detail=why)

    for app in order:
        item, before = items[app], previous.get(app)
        if refused:
            item.update(scim=ProvisioningState.UNKNOWN, scim_detail=refused)
            continue
        if transient or read >= SCIM_LIMIT:
            keep(item, before, transient or capped)
            continue
        sp = urllib.parse.quote(item["service_principal_id"], safe="")
        try:
            body = client.get(
                f"{GRAPH}/servicePrincipals/{sp}/synchronization/jobs",
                "read an enterprise application's provisioning jobs",
                SYNCHRONIZATION_READ_ALL,
            )
        except (DeadlineReached, SignInRefused):
            raise
        except Refused as e:
            read += 1
            tag = f"{e.status} {e.code}".strip()
            if e.status == 404:
                item.update(scim=ProvisioningState.OFF, scim_detail="No provisioning job.", scim_checked_at=now)
            elif e.status in (401, 403):
                refused = f"Unknown: provisioning jobs couldn't be read ({tag}; Synchronization.Read.All)."
                notes.append(
                    f"Provisioning wasn't read ({tag}): grant Synchronization.Read.All with admin consent. "
                    "Provisioning is unknown; single sign-on and MFA are reported."
                )
                item.update(scim=ProvisioningState.UNKNOWN, scim_detail=refused)
            else:
                keep(item, before, f"Unknown: couldn't be read ({tag}); the next run tries again.")
            continue
        except SourceError as e:  # throttled past the budget, unreachable: the rest wait for the next run
            transient = "Unknown: provisioning couldn't be read this run; the next run tries again."
            notes.append(f"Provisioning reads stopped: {e}")
            keep(item, before, transient)
            continue
        read += 1
        scim, why = provisioning_of(body.get("value"))
        item.update(scim=scim, scim_detail=why, scim_checked_at=now)
    if coverage.carried_scim:
        notes.append(
            f"Provisioning couldn't be read for {coverage.carried_scim} app(s) this run; they show the last run's answer."
        )
    if len(items) > SCIM_LIMIT and not refused and not transient:
        notes.append(
            f"Provisioning was read for {read} of {len(items)} apps (at most {SCIM_LIMIT} a run, least recently read "
            "first); the others show their last reading."
        )
    return read


def fetch(connector, credential, transport=None):
    """The coverage of every enterprise application a catalog application is linked to (CoverageFetched.coverage),
    for apply_fetched; no observation."""
    scope = scope_of(connector)
    client = Client(transports(connector, credential, coverage_simulator, transport)(GRAPH_AUDIENCE.hosts), credential)
    now = clock()
    sps = client.get_all(
        f"{GRAPH}/servicePrincipals?$select={SP_FIELDS}&$top=999",
        "list enterprise applications",
        APPLICATION_READ_ALL,
        PAGE_LIMIT,
    )
    previous = {row.app_id: row for row in AppCoverage.objects.filter(connector=connector)}
    if not sps and previous:
        raise SourceError(EMPTY_LISTING.format(count=len(previous)))
    wanted = linked_app_ids(connector.organization_id)
    by_app: dict[str, dict[str, Any]] = {}
    for sp in sps:
        app = str(sp.get("appId") or "").lower()
        if app in wanted and sp.get("id"):
            by_app.setdefault(app, sp)
    first_party = set(entra.FIRST_PARTY_TENANTS)
    notes: list[str] = []
    coverage = Coverage(listed=len(sps))
    for app, sp in sorted(by_app.items()):
        sso, mode, sso_detail = sso_of(sp)
        coverage.items[app] = {
            "service_principal_id": str(sp.get("id") or "").lower()[:36],
            "display_name": str(sp.get("displayName") or app)[:400],
            "microsoft": str(sp.get("appOwnerOrganizationId") or "").lower() in first_party,
            "sso": sso,
            "sso_mode": mode,
            "sso_detail": sso_detail,
            "scim": ProvisioningState.UNKNOWN,
            "scim_detail": "",
            "scim_checked_at": None,
            "mfa": MfaCoverage.UNKNOWN,
            "mfa_detail": "",
            "mfa_checked_at": None,
            "policies": [],
        }
    if scope.get("provisioning", True):
        coverage.scim_read = _read_provisioning(client, coverage, previous, now, notes)
    else:
        for item in coverage.items.values():
            item["scim_detail"] = "Unknown: provisioning isn't read (turned off in the connector's scope)."
    if scope.get("conditional_access", True) and coverage.items:
        result = read_policies(client, 0.0 if credential.tenant_id == SIMULATED else CA_PACE)
        coverage.ca_retries = result.retries
        if result.policies is None:
            notes.append(result.note)
            for app, item in coverage.items.items():
                before = previous.get(app)
                # Kept only after a transient failure, for the same service principal (a tenant change or a
                # re-created app is another one), from a read at most CARRY_MAX old.
                read_at = (
                    before.mfa_checked_at
                    if result.transient
                    and before is not None
                    and before.service_principal_id == item["service_principal_id"]
                    else None
                )
                if before is not None and read_at is not None and now - read_at < CARRY_MAX:
                    item.update(
                        mfa=before.mfa,
                        mfa_detail=before.mfa_detail,
                        policies=list(before.policies or []),
                        mfa_checked_at=read_at,
                    )
                    coverage.carried_mfa += 1
                elif read_at is not None:
                    item["mfa_detail"] = (
                        f"Unknown: conditional access hasn't been read since {_day(read_at)} "
                        f"(over {CARRY_MAX.days} days); {result.why.removeprefix('Unknown: ')}"
                    )
                else:
                    item["mfa_detail"] = result.why
        else:
            coverage.policies = len(result.policies)
            for app, item in coverage.items.items():
                item["mfa"], item["mfa_detail"], item["policies"] = mfa_coverage(
                    result.policies, app, item["microsoft"]
                )
                item["mfa_checked_at"] = now
    elif not scope.get("conditional_access", True):
        for item in coverage.items.values():
            item["mfa_detail"] = "Unknown: conditional access isn't read (turned off in the connector's scope)."
    if not wanted:
        notes.append(
            "No catalog application is linked to an Entra app yet: set an application's Entra application ID, or link "
            "its Entra candidate, and run this connector again."
        )
    found = CoverageFetched(notes=notes)
    found.coverage = coverage
    return found


ROW_FIELDS = (
    "service_principal_id",
    "display_name",
    "sso",
    "sso_mode",
    "sso_detail",
    "scim",
    "scim_detail",
    "scim_checked_at",
    "mfa",
    "mfa_detail",
    "mfa_checked_at",
    "policies",
)


def apply_fetched(connector, raw, observations, now) -> dict[str, Any]:
    """Rewrite the connector's coverage rows in the run's transaction: one per app read, the others deleted."""
    coverage = getattr(raw, "coverage", None)
    if coverage is None:
        return {}
    rows = {row.app_id: row for row in AppCoverage.objects.select_for_update().filter(connector=connector)}
    created, updated = [], []
    for app, item in coverage.items.items():
        values = {k: item[k] for k in ROW_FIELDS}
        values["sso_detail"] = values["sso_detail"][:300]
        values["scim_detail"] = values["scim_detail"][:300]
        values["mfa_detail"] = values["mfa_detail"][:300]
        row = rows.pop(app, None)
        if row is None:
            created.append(
                AppCoverage(
                    organization_id=connector.organization_id,
                    connector=connector,
                    app_id=app,
                    first_seen_at=now,
                    seen_at=now,
                    **values,
                )
            )
            continue
        for name, value in values.items():
            setattr(row, name, value)
        row.seen_at = now
        row.updated_at = now
        updated.append(row)
    AppCoverage.objects.bulk_create(created, batch_size=500)
    AppCoverage.objects.bulk_update(updated, [*ROW_FIELDS, "seen_at", "updated_at"], batch_size=500)
    removed, _ = AppCoverage.objects.filter(id__in=[row.id for row in rows.values()]).delete()
    items = coverage.items.values()
    counts: dict[str, Any] = {
        "seen": coverage.listed,
        "kept": len(coverage.items),
        "coverage_removed": removed,
        "sso_none": sum(1 for i in items if i["sso"] == Sso.NONE),
        "sso_unknown": sum(1 for i in items if i["sso"] == Sso.UNKNOWN),
        "scim_read": coverage.scim_read,
        "mfa_none": sum(1 for i in items if i["mfa"] == MfaCoverage.NONE),
        "mfa_unknown": sum(1 for i in items if i["mfa"] == MfaCoverage.UNKNOWN),
    }
    if coverage.policies is not None:
        counts["ca_policies"] = coverage.policies
    if coverage.ca_retries:
        counts["ca_retries"] = coverage.ca_retries
    if coverage.carried_mfa:
        counts["mfa_carried"] = coverage.carried_mfa
    if coverage.carried_scim:
        counts["scim_carried"] = coverage.carried_scim
    return counts


# ---- access check ----------------------------------------------------------------------------------------------------


def access_steps(connector, scope, session) -> list[dict[str, Any]]:
    """Sign in to Graph, then one small read per permission: the service principals (and with them an ID to read the
    provisioning jobs of) and the conditional access policies. A tenant without P1 is a warning with the problem
    `missing_licence`: the run still reports single sign-on and provisioning."""
    from . import access

    client = session.client(GRAPH_AUDIENCE)
    steps = [access.sign_in(client)]
    if steps[0]["state"] != "ok":
        return steps + [access.skipped(p.name, p.permission) for p in PROBES]
    listing, scim_probe, ca_probe = PROBES
    step, body = access.probe(client, listing)
    steps.append(step)
    if not scope.get("provisioning", True):
        steps.append(access.not_used(scim_probe.name, scim_probe.permission, "Read provisioning (SCIM) jobs"))
    elif body is None:
        steps.append(
            access.skipped(scim_probe.name, scim_probe.permission, "enterprise applications couldn't be listed")
        )
    else:
        first = next((str(v.get("id")) for v in body.get("value") or [] if isinstance(v, dict) and v.get("id")), "")
        if not first:
            steps.append(
                access.step(
                    "api",
                    scim_probe.name,
                    "not_checked",
                    permission=scim_probe.permission,
                    detail="Not checked here: the tenant lists no enterprise application to read the jobs of.",
                )
            )
        else:
            url = scim_probe.url.replace("{id}", urllib.parse.quote(first, safe=""))
            step, _body = access.probe(client, Probe(scim_probe.name, url, scim_probe.permission))
            if step["state"] == "failed" and step["problem"] == "unavailable":  # 404: that app has no provisioning
                step = access.step(
                    "api",
                    scim_probe.name,
                    "ok",
                    permission=scim_probe.permission,
                    detail="Reading provisioning jobs works (the first app has none).",
                )
            steps.append(step)
    if not scope.get("conditional_access", True):
        steps.append(access.not_used(ca_probe.name, ca_probe.permission, "Read conditional access policies"))
        return steps
    step, _body = access.probe(client, ca_probe)
    if step["state"] == "failed" and step["problem"] == "missing_permission" and step["code"] not in LICENCE_CODES:
        step.update(
            detail=(
                f"Microsoft Graph refused the read (403 {step['code']}): the app lacks Policy.Read.All, or the tenant "
                "has no Microsoft Entra ID P1 license, which conditional access also needs."
            ),
            next=(
                "Grant Policy.Read.All (Application) with admin consent; if it is granted, check that the tenant has "
                "Entra ID P1. Until then runs report MFA as unknown."
            ),
        )
    if step["code"] in LICENCE_CODES:
        step.update(
            state="warning",
            problem="missing_licence",
            problem_label="Missing license",
            detail=(
                f"Microsoft Graph refused the read (403 {step['code']}): the tenant has no Microsoft Entra ID P1 "
                "license, which conditional access needs. Runs report MFA as unknown."
            ),
            next=(
                "Add an Entra ID P1 license to the tenant to see MFA coverage, or turn off Read conditional access "
                "policies in the connector's scope."
            ),
        )
    steps.append(step)
    return steps
