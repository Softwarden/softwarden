"""Licence usage from Microsoft Entra ID (licence waste): which paid licences are
assigned to whom, and when each licensed user last signed in successfully, so the licence waste report
(discovery/licence_waste.py) can show seats tied up in accounts nobody uses. Its own source (`entra_licence_usage`,
capability discovery.entra) because it is the first to read per-user data and needs tenant-wide consents an
organization that only wants seat counts (`entra_licenses`) never grants.

Microsoft Learn, checked (docs/PERMISSIONS.md, "Discovery source:
`entra_licence_usage`"):

- `GET /v1.0/users?$select=id,accountEnabled,assignedLicenses,userType,createdDateTime,mail,userPrincipalName`,
  paged by `@odata.nextLink`, with User.Read.All. `assignedLicenses` comes back only when selected; `licenseDetails`
  has no application permission, so it is never read.
- `signInActivity` in the same `$select` also needs AuditLog.Read.All, and the tenant needs Microsoft Entra ID P1 (a
  tenant without it answers 403 Authentication_RequestFromNonPremiumTenantOrB2CTenant). With it a page holds at most
  500 users, and Graph allows ten such requests a minute per tenant: pages are read PACE seconds apart, and a 429 is
  waited out by Retry-After (core/graph.Budget). `lastSuccessfulSignInDateTime` is the activity date (interactive or
  not; `lastSignInDateTime` counts failures too); Entra doesn't backfill it before December 2023.
- Without P1 or the AuditLog consent (or with `sign_in_activity` off in the scope) the run reads the users again
  without signInActivity: licences are still counted, every last sign-in is unknown, and the run's notes say why.

Privacy: only licensed users are kept, one row
each, keyed by a keyed hash of the object ID (`user_key`), never the object ID, a name, an email address or a user
principal name. Mail and user principal name are read only to link a user to a Person here (by email) and to tell
service accounts by the scope's prefixes; the object ID only to link through the Person's Entra sign-in identity and
to hash. The last sign-in and creation are kept as dates. Every run rewrites the connector's rows, so a user who is no
longer licensed, or no longer in the tenant, goes with the next run.

Scope: `sign_in_activity` (default on), `service_account_prefixes` (user principal name prefixes such as `svc-`; such
users are counted apart and never as waste) and `excluded_users` (object IDs to count the same way). Entra has no
"service account" flag, so the organization names them.

If the listing comes back empty, or with no licensed user, while the connector has rows, the run is refused rather
than clearing them (a tenant doesn't lose every licence between two runs; a broken read can). The tenant ID
`simulated` selects the synthetic tenant (discovery/entra_simulator.py)."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.crypto import salted_hmac

from core import graph
from core.graph import LOGIN_HOST

from .entra_shadow_it import Pacer
from .graph_source import (
    AUDIT_LOG_SIGN_IN_ACTIVITY,
    GRAPH,
    GRAPH_AUDIENCE,
    SIMULATED,
    USER_READ_ALL,
    Client,
    Fetched,
    Permission,
    Probe,
    Refused,
    SourceError,
    licence_usage_simulator,
    transports,
)
from .models import LicensedUser

# ---- the connector's contract with the engine ---------------------------------------------------------------------

KINDS: list[str] = []  # no observations: licensed user rows (apply_fetched)
CONNECTION = "microsoft"
CONFIG_FIELDS: tuple[str, ...] = ()
SCOPE_DEFAULTS: dict[str, Any] = {"sign_in_activity": True, "service_account_prefixes": [], "excluded_users": []}
FILTERS: dict[str, str] = {}
PERMISSIONS = [USER_READ_ALL, AUDIT_LOG_SIGN_IN_ACTIVITY]
BASE_FIELDS = (
    "id,accountEnabled,assignedLicenses,licenseAssignmentStates,userType,createdDateTime,mail,userPrincipalName"
)
ACTIVITY_FIELDS = f"{BASE_FIELDS},signInActivity"
TOP = 999  # Graph's largest page of users
ACTIVITY_TOP = 500  # with signInActivity; Graph may return fewer a page (older Learn text said 120)
MAX_USERS = 100_000  # users a run reads; more stops the run with a readable error and changes nothing
PACE = 6.5  # seconds between pages with signInActivity on a real tenant: ten requests a minute per tenant
# How long the read with signInActivity may take (paced, so about 230 pages: 115,000 users at 500 a page, 27,000 at
# 120). Past it the run reads the users again without signInActivity, which is fast and unpaced, within the rest of the
# run's RUN_DEADLINE (40 minutes): licences are still counted and the last sign-in is unknown that run.
ACTIVITY_READ_FOR = 25 * 60.0
# A ONE_PER_TENANT source allows one connector per Microsoft connection and tenant ID (discovery/engine.py): two would
# count every licensed user twice, since each connector keys its rows with its own hash.
ONE_PER_TENANT = True
PROBES = [
    Probe("Read users and their licenses", f"{GRAPH}/users?$top=1&$select=id,assignedLicenses", USER_READ_ALL),
    Probe(
        "Read last sign-in",
        f"{GRAPH}/users?$top=1&$select=id,signInActivity",
        AUDIT_LOG_SIGN_IN_ACTIVITY,
        when="sign_in_activity",
    ),
]
simulator = licence_usage_simulator
LICENCE_CODES = frozenset({"Authentication_RequestFromNonPremiumTenantOrB2CTenant"})
UNKNOWN = "unknown (needs Entra ID P1 / AuditLog.Read.All)"
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PREFIX_MAX = 50
EXCLUDED_MAX = 1000
KEY_SALT = "softwarden.discovery.licence-usage"

# Module-level so tests can set the time.
clock = timezone.now


def scope_of(connector) -> dict[str, Any]:
    return dict(SCOPE_DEFAULTS) | dict(connector.scope or {})


def validate(config, scope, errors):
    if "sign_in_activity" in scope and not isinstance(scope["sign_in_activity"], bool):
        errors["scope.sign_in_activity"] = ["Choose yes or no."]
    prefixes = scope.get("service_account_prefixes", [])
    if not isinstance(prefixes, list) or not all(isinstance(p, str) and 0 < len(p.strip()) <= 100 for p in prefixes):
        errors["scope.service_account_prefixes"] = ["Enter one prefix per line, 100 characters at most."]
    elif len(prefixes) > PREFIX_MAX:
        errors["scope.service_account_prefixes"] = [f"Use {PREFIX_MAX} prefixes or fewer."]
    else:
        scope["service_account_prefixes"] = sorted({p.strip().lower() for p in prefixes})
    excluded = scope.get("excluded_users", [])
    if not isinstance(excluded, list) or not all(
        isinstance(u, str) and GUID.match(u.strip().lower()) for u in excluded
    ):
        errors["scope.excluded_users"] = ["Enter one user object ID (a GUID) per line."]
    elif len(excluded) > EXCLUDED_MAX:
        errors["scope.excluded_users"] = [f"Use {EXCLUDED_MAX} object IDs or fewer."]
    else:
        scope["excluded_users"] = sorted({u.strip().lower() for u in excluded})


# ---- one user -------------------------------------------------------------------------------------------------------


def user_key(connector_id, object_id: str) -> str:
    """The row's key: HMAC-SHA256 of the object ID, keyed by the installation's secret key and the connector. Stable
    across runs (so a user keeps their short hash), never reversible, and not testable for a known object ID without
    the installation's key."""
    return salted_hmac(KEY_SALT, f"{connector_id}:{object_id.lower()}", algorithm="sha256").hexdigest()


def _date(value: Any) -> date | None:
    """The UTC date of a Graph timestamp, or None (missing, unreadable, or the year-1 placeholder)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).date() if when.year >= 2000 else None


def map_user(raw: dict[str, Any], connector_id, known: bool, prefixes: list[str], excluded: set[str]) -> dict | None:
    """One user as the run keeps it while it applies (`emails` and `object_id` are used for linking only and never
    stored), or None when the user has no licence or no usable object ID."""
    oid = str(raw.get("id") or "").strip().lower()
    licences = raw.get("assignedLicenses")
    if not GUID.match(oid) or not isinstance(licences, list):
        return None
    skus = sorted(
        {
            sku
            for item in licences
            if isinstance(item, dict) and GUID.match(sku := str(item.get("skuId") or "").strip().lower())
        }
    )
    if not skus:
        return None
    # A licence assigned only through groups (every assignment state of the SKU names a group): reclaiming it means
    # changing the user's group membership, not removing a direct assignment. assignedLicenses lists both kinds.
    states = raw.get("licenseAssignmentStates")
    direct: set[str] = set()
    grouped: set[str] = set()
    for state in states if isinstance(states, list) else []:
        if not isinstance(state, dict):
            continue
        sku = str(state.get("skuId") or "").strip().lower()
        (grouped if state.get("assignedByGroup") else direct).add(sku)
    upn = str(raw.get("userPrincipalName") or "").strip().lower()
    mail = str(raw.get("mail") or "").strip().lower()
    guest = str(raw.get("userType") or "").lower() == "guest" or "#ext#" in upn
    activity = raw.get("signInActivity") if known else None
    activity = activity if isinstance(activity, dict) else {}
    last = _date(activity.get("lastSuccessfulSignInDateTime"))
    attempted = last is not None or any(
        _date(activity.get(k)) is not None for k in ("lastSignInDateTime", "lastNonInteractiveSignInDateTime")
    )
    return {
        "user_key": user_key(connector_id, oid),
        "object_id": oid,
        # Linking by email (link_people): members only, the user principal name first; never stored.
        "upn": upn if "@" in upn and "#ext#" not in upn and not guest else "",
        "mail": mail if "@" in mail and not guest else "",
        "sku_ids": skus,
        "group_sku_ids": sorted(s for s in grouped - direct if s in skus),
        "account_enabled": raw.get("accountEnabled") is not False,
        "guest": guest,
        "service_account": oid in excluded or any(upn.startswith(p) for p in prefixes),
        "sign_in_known": known,
        "last_sign_in_on": last,
        "attempted": attempted,
        "created_on": _date(raw.get("createdDateTime")),
    }


# ---- the run -------------------------------------------------------------------------------------------------------


@dataclass
class Usage:
    """What `apply_fetched` writes in the run's transaction: the licensed users (with their transient link keys), how
    many users were listed, whether the last sign-in was read, and the tenant (to link sign-in identities)."""

    users: list[dict[str, Any]] = field(default_factory=list)
    listed: int = 0
    sign_in_known: bool = False
    tenant_id: str = ""  # the token's tid when it has one, else the connection's tenant ID, lower case
    tenant_key: str = ""  # tenant_key(): stored in the run's counts for other_reader


class UsageFetched(Fetched):
    usage: Usage | None = None


class OutOfTime(SourceError):
    """The read with signInActivity didn't finish within ACTIVITY_READ_FOR: `users` read in `pages` pages so far."""

    def __init__(self, users: int, pages: int):
        super().__init__(f"{users:,} users read in {pages:,} pages")
        self.users = users
        self.pages = pages


def read_users(
    client: Client, select: str, top: int, permission: Permission, pace: float, read_for: float | None = None
) -> list[dict[str, Any]]:
    """Every user with the selected properties, PACE seconds between pages when asked; Graph's throttling is waited
    out within the run's budget (Retry-After, core/graph.Budget). Bounded by users read (MAX_USERS, whatever Graph's
    page size) and, with `read_for`, by time (OutOfTime before a page that would start past it); the run's own
    deadline stops it anyway (DeadlineReached)."""
    url = f"{GRAPH}/users?$select={select}&$top={top}"
    pacer = Pacer(pace)
    until = graph.monotonic() + read_for if read_for is not None else None
    out: list[dict[str, Any]] = []
    pages = 0
    while url:
        if until is not None and graph.monotonic() + pace > until:
            raise OutOfTime(len(out), pages)
        pacer.wait()
        page = client.get(url, "list users with their licenses", permission)
        pages += 1
        out.extend(v for v in page.get("value") or [] if isinstance(v, dict))
        if len(out) > MAX_USERS:
            raise SourceError(
                f"The tenant lists more than {MAX_USERS:,} users ({len(out):,} read in {pages:,} pages), more than a "
                "run reads; the run stopped and nothing was changed."
            )
        url = page.get("@odata.nextLink") or ""
    return out


def not_read_note(e: Refused) -> str:
    tag = f"{e.status} {e.code}".strip()
    if e.code in LICENCE_CODES:
        why = f"it needs a Microsoft Entra ID P1 license in the tenant ({tag})"
    else:
        why = f"grant AuditLog.Read.All with admin consent; the tenant also needs Microsoft Entra ID P1 ({tag})"
    return (
        f"Last sign-in wasn't read: {why}. Licenses are counted; every last sign-in shows as {UNKNOWN}, so nothing "
        "is counted as inactive."
    )


EMPTY_LISTING = (
    "Microsoft Graph listed {what}, but this connector has {count} licensed user(s) from earlier runs. Nothing was "
    "changed: a tenant doesn't lose every licensed user between two runs, so the listing is more likely incomplete. "
    "Check the connection and User.Read.All, then run it again, or delete the connector to start again."
)


TENANT_KEY_SALT = "softwarden.discovery.licence-usage.tenant"


def token_tenant(token: str) -> str:
    """The `tid` claim of an access token, lower case, or "" when it has none (not a JWT, no GUID). The token comes
    straight from Microsoft's token endpoint over TLS; its payload is read, not verified, and only to name the tenant
    it was issued in, so two connections naming one tenant differently (a domain and its GUID) are told apart."""
    parts = token.split(".")
    if len(parts) != 3:
        return ""
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:  # bad base64 (binascii.Error), bad UTF-8 or bad JSON
        return ""
    tid = str(payload.get("tid") or "").strip().lower() if isinstance(payload, dict) else ""
    return tid if GUID.match(tid) else ""


def tenant_key(org_id, tenant: str) -> str:
    """What a run stores in its counts to say which tenant it read: a keyed hash, so the tenant ID (which only Admins
    see, on the connection) isn't shown to every role with the run."""
    return salted_hmac(TENANT_KEY_SALT, f"{org_id}:{tenant}", algorithm="sha256").hexdigest()[:16]


def other_reader(connector, key: str) -> str | None:
    """The name of another enabled licence usage connector of the organization whose last successful run read the
    same tenant, or None. The check at creation compares connections and tenant IDs as typed; this one compares the
    tenant the token was issued in, which a domain name or a later change in Connections can't hide."""
    from .models import Connector, ConnectorRun, RunStatus

    others = (
        Connector.objects.filter(organization_id=connector.organization_id, source=connector.source, enabled=True)
        .exclude(pk=connector.pk)
        .order_by("name", "id")
    )
    for other in others:
        last = (
            ConnectorRun.objects.filter(connector=other, status=RunStatus.SUCCEEDED)
            .order_by("-finished_at", "-id")
            .values_list("counts", flat=True)
            .first()
        )
        if isinstance(last, dict) and last.get("tenant_key") == key:
            return other.name
    return None


def fetch(connector, credential, transport=None):
    """The licensed users (UsageFetched.usage) for apply_fetched; no observation. Refused before reading anything when
    another enabled licence usage connector last read the same tenant (it would count every licence twice)."""
    scope = scope_of(connector)
    client = Client(
        transports(connector, credential, licence_usage_simulator, transport)(GRAPH_AUDIENCE.hosts), credential
    )
    tenant = token_tenant(client.token()) or str(credential.tenant_id or "").strip().lower()
    key = tenant_key(connector.organization_id, tenant)
    duplicate = other_reader(connector, key)
    if duplicate is not None:
        raise SourceError(
            f"The connector “{duplicate}” already reads this tenant's licensed users; this run would count every "
            "license twice, so it stopped and nothing was changed. Turn one of them off or delete it."
        )
    notes: list[str] = []
    raw: list[dict[str, Any]] | None = None
    known = False
    if scope.get("sign_in_activity", True):
        pace = 0.0 if credential.tenant_id == SIMULATED else PACE
        try:
            raw = read_users(client, ACTIVITY_FIELDS, ACTIVITY_TOP, AUDIT_LOG_SIGN_IN_ACTIVITY, pace, ACTIVITY_READ_FOR)
            known = True
        except OutOfTime as e:
            notes.append(
                f"Last sign-in wasn't read: reading it took longer than {round(ACTIVITY_READ_FOR / 60)} minutes "
                f"({e.users:,} users in {e.pages:,} pages; Graph allows ten such pages a minute). Licenses are "
                f"counted; every last sign-in shows as {UNKNOWN} this run."
            )
        except Refused as e:
            if e.status != 403:
                raise
            notes.append(not_read_note(e))
    else:
        notes.append(
            f"Last sign-in isn't read (turned off in the connector's scope). Licenses are counted; every last sign-in "
            f"shows as {UNKNOWN}."
        )
    if raw is None:
        raw = read_users(client, BASE_FIELDS, TOP, USER_READ_ALL, 0.0)
    previous = LicensedUser.objects.filter(connector=connector).count()
    if raw and not any("assignedLicenses" in u for u in raw):
        raise SourceError(
            "Microsoft Graph listed users without their assigned licenses; nothing was changed. Check that the app "
            "has User.Read.All (User.ReadBasic.All doesn't include licenses), then run it again."
        )
    scope_prefixes = [str(p).lower() for p in scope.get("service_account_prefixes") or [] if str(p).strip()]
    excluded = {str(u).lower() for u in scope.get("excluded_users") or []}
    users: dict[str, dict[str, Any]] = {}
    for item in raw:
        mapped = map_user(item, connector.id, known, scope_prefixes, excluded)
        if mapped is not None:
            users.setdefault(mapped["user_key"], mapped)
    if previous and not raw:
        raise SourceError(EMPTY_LISTING.format(what="no users", count=previous))
    if previous and not users:
        raise SourceError(EMPTY_LISTING.format(what="no user with a license", count=previous))
    if known and users and not any(u["attempted"] for u in users.values()):
        notes.append(
            "No licensed user has a recorded sign-in. If that's unexpected, check that the tenant has Microsoft Entra "
            "ID P1."
        )
    found = UsageFetched(notes=notes)
    found.usage = Usage(list(users.values()), len(raw), known, tenant, key)
    return found


def _identities(tenant_id: str, oids: list[str]) -> dict[str, int]:
    """{object ID: account} from the Entra sign-in identities whose subject is one of the object IDs. The issuer is
    `https://login.microsoftonline.com/{tenant GUID}/v2.0` (core/oidc stores the GUID in lower case); a tenant given
    by domain name (contoso.onmicrosoft.com) can't be matched to it, so then any Entra issuer counts: object IDs are
    GUIDs, unique across tenants."""
    from core.models import UserIdentity

    tenant = (tenant_id or "").strip().lower()
    rows = UserIdentity.objects.all()
    if GUID.match(tenant):
        rows = rows.filter(issuer=f"https://{LOGIN_HOST}/{tenant}/v2.0")
    else:
        rows = rows.filter(issuer__startswith=f"https://{LOGIN_HOST}/")
    return dict(rows.annotate(oid=Lower("subject")).filter(oid__in=oids).values_list("oid", "user_id"))


def link_people(org_id, tenant_id: str, users: list[dict[str, Any]]) -> dict[str, Any]:
    """{user_key: person id} for the users a Person here can be told by, strongest first:

    1. identity: the Person's account signs in with an Entra identity whose object ID is the user's (guests too);
    2. user principal name: a member's UPN is the Person's email;
    3. mail: a member's mail is the Person's email.

    Guests are linked by identity only: their mail is another organization's address. A user whose UPN and mail name
    two different people is left unlinked, and so is every user claiming a Person that a stronger match already
    claimed, or that another user claims at the same strength (an `adm-jane` account whose mail is jane@ never takes
    Jane from her own account). Anonymized people are never linked."""
    from catalog.models import Person

    if not users:
        return {}
    people = Person.objects.filter(organization_id=org_id, anonymized_at__isnull=True)
    accounts = _identities(tenant_id, sorted({u["object_id"] for u in users}))
    person_of_user = (
        dict(people.filter(user_id__in=set(accounts.values())).values_list("user_id", "id")) if accounts else {}
    )
    emails = sorted({e for u in users for e in (u["upn"], u["mail"]) if e})
    by_email = dict(
        people.exclude(email="")
        .annotate(lowered=Lower("email"))
        .filter(lowered__in=emails)
        .values_list("lowered", "id")
    )
    # (strength, user, person): 0 identity, 1 user principal name, 2 mail.
    claims: list[tuple[int, str, Any]] = []
    for u in users:
        account = accounts.get(u["object_id"])
        if account is not None and account in person_of_user:
            claims.append((0, u["user_key"], person_of_user[account]))
            continue
        by_upn, by_mail = by_email.get(u["upn"]) if u["upn"] else None, by_email.get(u["mail"]) if u["mail"] else None
        if by_upn is not None and by_mail is not None and by_upn != by_mail:
            continue  # two people: ambiguous
        if by_upn is not None:
            claims.append((1, u["user_key"], by_upn))
        elif by_mail is not None:
            claims.append((2, u["user_key"], by_mail))
    out: dict[str, Any] = {}
    taken: set[Any] = set()
    for strength in (0, 1, 2):
        level = [(key, person) for s, key, person in claims if s == strength]
        count: dict[Any, int] = {}
        for _key, person in level:
            count[person] = count.get(person, 0) + 1
        for key, person in level:
            if person not in taken and count[person] == 1:
                out[key] = person
        taken |= set(count)
    return out


def unlink_anonymized(org, person) -> dict[str, int]:
    """catalog/admin_services.ANONYMIZE_HOOKS: an anonymized Person is no longer linked to any licensed user (the next
    run wouldn't link them either)."""
    unlinked = LicensedUser.objects.filter(organization=org, person=person).update(person=None)
    return {"licensed_users": unlinked} if unlinked else {}  # named in the event only when there was any


ROW_FIELDS = (
    "sku_ids",
    "account_enabled",
    "guest",
    "service_account",
    "sign_in_known",
    "last_sign_in_on",
    "attempted",
    "created_on",
    "group_sku_ids",
)


def apply_fetched(connector, raw, observations, now) -> dict[str, Any]:
    """Rewrite the connector's licensed users in the run's transaction: the users read, linked to people now; every
    other row (a user no longer licensed or no longer in the tenant) is deleted. Nothing else is stored."""
    usage = getattr(raw, "usage", None)
    if usage is None:
        return {}
    before = set(LicensedUser.objects.filter(connector=connector).values_list("user_key", flat=True))
    links = link_people(connector.organization_id, usage.tenant_id, usage.users)
    LicensedUser.objects.filter(connector=connector).delete()
    LicensedUser.objects.bulk_create(
        [
            LicensedUser(
                organization_id=connector.organization_id,
                connector=connector,
                user_key=u["user_key"],
                person_id=links.get(u["user_key"]),
                seen_at=now,
                **{k: u[k] for k in ROW_FIELDS},
            )
            for u in usage.users
        ],
        batch_size=1000,
    )
    users = usage.users
    kept = {u["user_key"] for u in users}
    counts: dict[str, Any] = {
        "seen": usage.listed,
        "kept": len(users),
        "licensed_users": len(users),
        "users_removed": len(before - kept),
        "linked": len(links),
        "guests": sum(1 for u in users if u["guest"]),
        "disabled": sum(1 for u in users if not u["account_enabled"]),
        "service_accounts": sum(1 for u in users if u["service_account"]),
        "tenant_key": usage.tenant_key,  # which tenant this run read (other_reader); a hash, not the ID
    }
    if not usage.sign_in_known:
        counts["sign_in_unknown"] = len(users)
    return counts


# ---- access check ----------------------------------------------------------------------------------------------------


def access_steps(connector, scope, session) -> list[dict[str, Any]]:
    """Sign in to Graph, then one small read per permission: the users with their licences (User.Read.All), then their
    sign-in activity (AuditLog.Read.All and Entra ID P1). A tenant without P1 is a warning with the problem
    `missing_licence`: runs still count the licences."""
    from . import access

    client = session.client(GRAPH_AUDIENCE)
    steps = [access.sign_in(client)]
    if steps[0]["state"] != "ok":
        return steps + [access.skipped(p.name, p.permission) for p in PROBES]
    users_probe, activity_probe = PROBES
    step, _body = access.probe(client, users_probe)
    steps.append(step)
    if not scope.get("sign_in_activity", True):
        steps.append(access.not_used(activity_probe.name, activity_probe.permission, "Read last sign-in"))
        return steps
    if step["state"] != "ok":
        steps.append(access.skipped(activity_probe.name, activity_probe.permission, "users couldn't be read"))
        return steps
    step, _body = access.probe(client, activity_probe)
    if step["code"] in LICENCE_CODES:
        step.update(
            state="warning",
            problem="missing_licence",
            problem_label="Missing license",
            detail=(
                f"Microsoft Graph refused the read (403 {step['code']}): the tenant has no Microsoft Entra ID P1 "
                f"license, which sign-in activity needs. Runs count licenses and show the last sign-in as {UNKNOWN}."
            ),
            next=(
                "Add an Entra ID P1 license to the tenant to see inactive licenses, or turn off Read last sign-in in "
                "the connector's scope."
            ),
        )
    steps.append(step)
    return steps
