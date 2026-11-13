"""Shadow IT from Microsoft Entra ID: third-party apps people sign in to, or have
consented to, that nobody registered in the catalog, as discovery candidates with usage counts. Its own source
(`entra_shadow_it`, capability discovery.entra), so an organization without Entra ID P1 or without the consents can
leave it off.

Microsoft Learn, checked (docs/PERMISSIONS.md, "Discovery source:
`entra_shadow_it`"):

- `GET /v1.0/auditLogs/signIns`: application permission AuditLog.Read.All, and the tenant needs Microsoft Entra ID P1
  or P2 (without it Graph answers 403 Authentication_RequestFromNonPremiumTenantOrB2CTenant). Kept 30 days with P1/P2.
  Always filtered by a `createdDateTime` window (an unfiltered read times out); `$top` at most 1000; newest first;
  follow `@odata.nextLink`. `$select` isn't documented, so none is sent. Throttled at 5 requests per 10 seconds per app
  per tenant: reads are paced 2.2 seconds apart (a margin for the Entra connector on the same app registration);
  a throttled read waits Retry-After and is read again; windows are 3 days, halve when a read times out (504), has
  too many pages or runs out of the run's time, and double back after a complete one when the doubled window fits in
  the time left; the size a run ends with is stored for the next run.
  v1.0 returns interactive (and federated) sign-ins only, which is what is wanted. Nothing aggregates on the server.
- `GET /v1.0/oauth2PermissionGrants`: Directory.Read.All (least privileged). `consentType` AllPrincipals is admin
  consent (principalId null), Principal one user's. `clientId` is the client's service principal object ID, not its
  application ID; `scope` is space-separated. Followed by next link.
- Third-party: the service principal's `appOwnerOrganizationId` isn't the tenant's own ID (`GET /v1.0/organization`)
  and isn't a Microsoft first-party tenant (the scope's `first_party_tenants`, the Entra source's list). Service
  principals are listed with `$select` (`GET /v1.0/servicePrincipals`, which Directory.Read.All also allows).

A run reads the sign-ins from the connector's cursor (`ShadowItState.read_until`) up to 15 minutes ago, never further
back than the rolling window (30 UTC days, within Entra's 30-day retention), window by window; a window counts only once it was read completely, and the cursor
moves to the end of the last complete one, so a second run never counts a sign-in twice. Usage is rolling: users and
sign-ins over the last 30 UTC days, recomputed from the stored daily aggregates (ShadowItUsageDay), never from raw rows.

Privacy: no sign-in row and no user identifier is stored. Per app per UTC day only the successful sign-in count, the
distinct users that day and a HyperLogLog sketch of HMAC-SHA256(per-connector random key, user object ID): enough to
count distinct users over any span of days, not to list them. Consents are counted (how many people), never named.

An observed app that drops out of the rolling window with no consent grant (or, with `sign_ins` off, has no consent
grant) is "inactive" or "not read": out of scope, never "no longer seen", so a linked record isn't flagged as gone.
An app whose enterprise app was deleted from the tenant goes at the next run, whatever its usage and with or without
the sign-in logs. Usage numbers don't count as a change of a candidate
(VOLATILE), and a dismissed candidate comes back only on a high-risk scope or a consent type it never had before.

Apps the catalog already has (a live application record with the app's Entra application ID, or an observation of an
Entra connector for it) are not candidates: they are skipped as "already in the catalog" and kept in the overview
(ShadowItApp, `in_catalog`) with their usage. An app this connector's own candidate was linked or created from stays
its candidate, so its values keep updating. Nothing here writes to Entra or to a curated record.

The tenant ID `simulated` selects the synthetic tenant (discovery/entra_simulator.py with its shadow IT apps)."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import urllib.parse
import zlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any

from django.db.models.functions import Lower
from django.utils import timezone

from core import graph

from . import entra, scopes
from .graph_source import (
    AUDIT_LOG_READ_ALL,
    DIRECTORY_READ_ALL,
    GRAPH,
    GRAPH_AUDIENCE,
    SIMULATED,
    Client,
    DeadlineReached,
    Fetched,
    Probe,
    Refused,
    SignInRefused,
    SourceError,
    Throttled,
    shadow_simulator,
    throttled,
    transports,
)
from .models import (
    ConsentType,
    DecisionKind,
    Observation,
    ObservationKind,
    ShadowItApp,
    ShadowItState,
    ShadowItUsageDay,
    Source,
)

# ---- the connector's contract with the engine ---------------------------------------------------------------------

KINDS = [ObservationKind.APPLICATION]
CONNECTION = "microsoft"
CONFIG_FIELDS: tuple[str, ...] = ()
SCOPE_DEFAULTS: dict[str, Any] = {
    "sign_ins": True,
    "exclude_microsoft": True,
    "first_party_tenants": entra.FIRST_PARTY_TENANTS,
    "name_exclusions": [],
}
# Labels read inside a sentence ("4 published by Microsoft"), so they start lowercase.
FILTERS = {
    "no_service_principal": "no enterprise app in the tenant (usually a Microsoft app)",
    "not_application": "not an application (managed identity or legacy)",
    "microsoft": "published by Microsoft",
    "own_tenant": "published by this tenant, or publisher unknown",
    "in_catalog": "already in the catalog or found by the Entra source",
    "inactive": "no sign-in in 30 days and no consent grant",
    "not_read": "no consent grant, and the sign-in logs aren't read",
    "name_exclusions": "name excluded",
}
PERMISSIONS = [AUDIT_LOG_READ_ALL, DIRECTORY_READ_ALL]
PROBES: list[Probe] = []  # built when the check runs (access_steps): the sign-in read needs a time window
# The observation data keys the candidates API shows (`details`).
DETAILS = (
    "users_30d",
    "sign_ins_30d",
    "last_sign_in_at",
    "sign_ins_read",
    "consent",
    "consenting_users",
    "scopes",
    "risky_scopes",
    "verified_publisher",
)
# Data keys that change with use, not with the app: a candidate isn't "changed" when only these differ
# (discovery/engine._apply), so runs don't report every candidate changed or queue the assistant for nothing.
VOLATILE = ("users_30d", "sign_ins_30d", "last_sign_in_at", "sign_ins_read")
CONSENT_RANK = {ConsentType.NONE: 0, ConsentType.USER: 1, ConsentType.ADMIN: 2}
simulator = shadow_simulator
record_fields = entra.record_fields  # a record created from a candidate carries its Entra application ID
drift_values = entra.drift_values  # name, description, home page and the Entra IDs, as for the Entra source

# ---- reading -------------------------------------------------------------------------------------------------------

RETENTION = timedelta(days=30)  # sign-in logs with Entra ID P1 or P2 (Learn)
ROLLING_DAYS = 30
WINDOW = timedelta(days=3)
MIN_WINDOW = timedelta(hours=1)
# Sign-ins reach the log some minutes after they happen. Reading up to 15 minutes ago keeps a late one from falling
# behind the cursor. Assumed, not a figure from Learn; to confirm on a live tenant.
LAG = timedelta(minutes=15)
READ_FOR = 30 * 60.0  # seconds a run reads sign-ins, checked before every page (graph_source.RUN_DEADLINE is 40 min)
PACE = 2.2  # seconds between sign-in log reads on a real tenant: 5 per 10 s per app per tenant, with a margin
SIGN_IN_TOP = 1000
WINDOW_PAGES = 2000  # pages of 1,000 in one window, at most
PAGE_LIMIT = 500  # pages of service principals or grants
SHRINK_STATUSES = (504,)  # a window too large to answer in time, on any page: halved
TRANSIENT_STATUSES = (500, 502)  # after a complete window: keep it, note, and stop
LICENCE_CODE = "Authentication_RequestFromNonPremiumTenantOrB2CTenant"
SP_FIELDS = (
    "id,appId,displayName,appOwnerOrganizationId,publisherName,verifiedPublisher,servicePrincipalType,homepage,"
    "accountEnabled,description"
)
DESCRIPTION_MAX = 2000
SCOPES_MAX = 100

# High-risk delegated scopes: an app holding one can read or change mail, files, sites or the directory as the user,
# or keep access without the user (offline_access). Compared case-insensitively.
HIGH_RISK = frozenset(
    s.lower()
    for s in (
        "offline_access",
        "Mail.Read",
        "Mail.ReadWrite",
        "Mail.Send",
        "Mail.Read.Shared",
        "Mail.ReadWrite.Shared",
        "Mail.Send.Shared",
        "MailboxSettings.ReadWrite",
        "Files.Read.All",
        "Files.ReadWrite.All",
        "Sites.Read.All",
        "Sites.ReadWrite.All",
        "Sites.Manage.All",
        "Sites.FullControl.All",
        "Notes.ReadWrite.All",
        "Calendars.ReadWrite",
        "Contacts.ReadWrite",
        "User.ReadWrite.All",
        "Group.ReadWrite.All",
        "Application.ReadWrite.All",
        "AppRoleAssignment.ReadWrite.All",
        "full_access_as_user",
        "EWS.AccessAsUser.All",
    )
)
HIGH_RISK_PREFIXES = ("directory.", "rolemanagement.")

# Module-level so tests can set the time (the simulator has its own clock, entra_simulator.clock).
clock = timezone.now


def is_risky(scope: str) -> bool:
    lowered = scope.lower()
    return lowered in HIGH_RISK or lowered.startswith(HIGH_RISK_PREFIXES)


def scope_of(connector) -> dict[str, Any]:
    return dict(SCOPE_DEFAULTS) | dict(connector.scope or {})


def validate(config, scope, errors):
    for key in ("sign_ins", "exclude_microsoft"):
        if key in scope and not isinstance(scope[key], bool):
            errors[f"scope.{key}"] = ["Choose yes or no."]
    scopes.validate_names(scope, errors)
    tenants = scope.get("first_party_tenants", entra.FIRST_PARTY_TENANTS)
    if not isinstance(tenants, list) or not all(isinstance(t, str) and entra.GUID.match(t.strip()) for t in tenants):
        errors["scope.first_party_tenants"] = ["Enter one tenant ID (a GUID) per line."]
    elif len(tenants) > entra.FIRST_PARTY_MAX:
        errors["scope.first_party_tenants"] = [f"Use {entra.FIRST_PARTY_MAX} tenants or fewer."]
    else:
        scope["first_party_tenants"] = sorted({t.strip().lower() for t in tenants})


# ---- time ---------------------------------------------------------------------------------------------------------

STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$")


def parse_time(value: Any) -> datetime | None:
    """A Graph createdDateTime (seconds, or up to seven fractional digits; Z or an offset) as an aware UTC datetime."""
    if not isinstance(value, str):
        return None
    found = STAMP.match(value.strip())
    if not found:
        return None
    base, fraction, zone = found.groups()
    try:
        when = datetime.fromisoformat(base)
    except ValueError:
        return None
    if fraction:
        when = when.replace(microsecond=int(fraction[:6].ljust(6, "0")))
    offset = timedelta()
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        offset = sign * timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
    return (when - offset).replace(tzinfo=UTC)


def stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shown(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def sign_ins_url(start: datetime, end: datetime, top: int = SIGN_IN_TOP) -> str:
    query = urllib.parse.urlencode(
        {"$filter": f"createdDateTime ge {stamp(start)} and createdDateTime le {stamp(end)}", "$top": str(top)},
        quote_via=urllib.parse.quote,
        safe="$:",
    )
    return f"{GRAPH}/auditLogs/signIns?{query}"


# ---- distinct users without keeping them --------------------------------------------------------------------------

SKETCH_P = 12
SKETCH_M = 1 << SKETCH_P  # registers
_ALPHA = 0.7213 / (1 + 1.079 / SKETCH_M)


class Sketch:
    """A HyperLogLog sketch (4,096 one-byte registers, standard error about 1.6%; linear counting while it is sparse,
    which is exact in practice for the few dozen users of a typical shadow IT app). It holds no identifier: each user
    sets one register to the rank of a keyed hash, and sketches merge by taking each register's maximum."""

    __slots__ = ("registers",)

    def __init__(self, registers: bytearray | None = None):
        self.registers = registers if registers is not None else bytearray(SKETCH_M)

    @classmethod
    def load(cls, blob: Any) -> Sketch:
        try:
            raw = zlib.decompress(bytes(blob))
        except (zlib.error, TypeError):
            raw = b""
        return cls(bytearray(raw)) if len(raw) == SKETCH_M else cls()

    def dump(self) -> bytes:
        return zlib.compress(bytes(self.registers), 9)

    def add(self, digest: bytes) -> None:
        x = int.from_bytes(digest[:8], "big")
        index = x >> (64 - SKETCH_P)
        rest = x & ((1 << (64 - SKETCH_P)) - 1)
        rank = (64 - SKETCH_P) - rest.bit_length() + 1
        if rank > self.registers[index]:
            self.registers[index] = rank

    def merge(self, other: Sketch) -> None:
        self.registers = bytearray(map(max, self.registers, other.registers))

    def estimate(self) -> int:
        zeros = self.registers.count(0)
        if zeros == SKETCH_M:
            return 0
        raw = _ALPHA * SKETCH_M * SKETCH_M / sum(2.0**-r for r in self.registers)
        if raw <= 2.5 * SKETCH_M and zeros:
            return round(SKETCH_M * math.log(SKETCH_M / zeros))
        return round(raw)


def user_hash(key: bytes, user_id: str) -> bytes:
    return hmac.new(key, user_id.strip().lower().encode(), hashlib.sha256).digest()


# ---- one window of sign-ins ------------------------------------------------------------------------------------------


@dataclass
class Usage:
    """Sign-ins read (in memory only): per (app ID, UTC day) [count, sketch], and each app's last sign-in."""

    days: dict[tuple[str, date], list[Any]] = field(default_factory=dict)
    last: dict[str, datetime] = field(default_factory=dict)
    counted: int = 0
    failed: int = 0

    def add(self, event: dict[str, Any], start: datetime, end: datetime, key: bytes) -> None:
        """One sign-in, if it is in [start, end): its user's ID is hashed into the day's sketch and dropped."""
        when = parse_time(event.get("createdDateTime"))
        app = str(event.get("appId") or "").strip().lower()
        if when is None or not (start <= when < end) or not entra.GUID.match(app):
            return
        status = event.get("status")
        if isinstance(status, dict) and status.get("errorCode") not in (0, "0", None):
            self.failed += 1  # a failed sign-in isn't use
            return
        slot = self.days.setdefault((app, when.date()), [0, Sketch()])
        slot[0] += 1
        user = event.get("userId")
        if isinstance(user, str) and user.strip():
            slot[1].add(user_hash(key, user))
        if app not in self.last or when > self.last[app]:
            self.last[app] = when
        self.counted += 1

    def merge(self, other: Usage) -> None:
        for k, (count, sketch) in other.days.items():
            slot = self.days.setdefault(k, [0, Sketch()])
            slot[0] += count
            slot[1].merge(sketch)
        for app, when in other.last.items():
            if app not in self.last or when > self.last[app]:
                self.last[app] = when
        self.counted += other.counted
        self.failed += other.failed


class Pacer:
    """Spaces requests `seconds` apart (graph.sleep and graph.monotonic, which tests replace)."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.last: float | None = None

    def wait(self) -> None:
        if self.seconds and self.last is not None:
            gap = self.seconds - (graph.monotonic() - self.last)
            if gap > 0:
                graph.sleep(gap)
        self.last = graph.monotonic()


class ReadTime:
    """The time a run has for reading sign-ins (READ_FOR from when it starts reading them) and the time a page has
    taken so far, so a window stops before a page would run past it: the pages a window may read are capped by the
    time left as well as by WINDOW_PAGES."""

    def __init__(self, seconds: float):
        self.until = graph.monotonic() + seconds
        self.pages = 0
        self.spent = 0.0

    def left(self) -> float:
        return self.until - graph.monotonic()

    def per_page(self) -> float:
        return self.spent / self.pages if self.pages else 0.0

    def page_fits(self) -> bool:
        left = self.left()
        return left > 0 and left >= self.per_page()

    def fits(self, pages: int) -> bool:
        """`pages` more pages fit in the time left, at the time a page has taken so far."""
        return self.left() >= pages * self.per_page()

    def record(self, seconds: float) -> None:
        self.pages += 1
        self.spent += seconds


class WindowTooBig(Exception):
    """A window couldn't be read completely: more than WINDOW_PAGES pages (`pages`), or not within the run's time
    (`time`, the run's hard deadline included)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


LICENCE_MESSAGE = (
    "Microsoft Graph refused to read the sign-in logs (403 {code}): reading them needs a Microsoft Entra ID P1 or P2 "
    "license in the tenant, and this tenant has none. Add one, or turn off Read sign-in logs in this connector's scope "
    "to find shadow IT from consent grants only."
)


def read_window(client: Client, start: datetime, end: datetime, key: bytes, pacer: Pacer, time: ReadTime) -> Usage:
    """Every sign-in in [start, end), all pages, or WindowTooBig. A throttled first page raises Throttled (the caller
    waits and reads the window again); later pages wait and retry as every Graph read does. The licence refusal is said as such."""
    found = Usage()
    url = sign_ins_url(start, end)
    for page_number in range(WINDOW_PAGES):
        if not time.page_fits():
            raise WindowTooBig("time")
        began = graph.monotonic()
        pacer.wait()
        try:
            page = client.get(url, "read the sign-in logs", AUDIT_LOG_READ_ALL, retry_throttled=page_number > 0)
        except DeadlineReached:
            raise WindowTooBig("time") from None
        except Refused as e:
            if e.code == LICENCE_CODE:
                raise Refused(LICENCE_MESSAGE.format(code=LICENCE_CODE), e.status, e.code, missing=False) from None
            raise
        time.record(graph.monotonic() - began)
        for event in page.get("value") or []:
            if isinstance(event, dict):
                found.add(event, start, end, key)
        url = page.get("@odata.nextLink") or ""
        if not url:
            return found
    raise WindowTooBig("pages")


@dataclass
class SignInRead:
    usage: Usage
    start: datetime
    until: datetime  # every sign-in in [start, until) was counted
    window: timedelta  # the next window to read; stored for the next run
    windows: int = 0
    shrinks: int = 0
    smallest: timedelta = WINDOW  # the smallest window read completely
    notes: list[str] = field(default_factory=list)

    def shrink(self) -> bool:
        """Halve the window, down to MIN_WINDOW. False when it is that small already."""
        if self.window <= MIN_WINDOW:
            return False
        self.window = max(self.window / 2, MIN_WINDOW)
        self.shrinks += 1
        return True

    def grow(self, pages: int, time: ReadTime) -> None:
        """After a complete window of `pages` pages, back towards WINDOW (doubling), but only when a window twice as
        long (about twice the pages) still fits in the run's time left."""
        if self.window < WINDOW and time.fits(2 * pages):
            self.window = min(self.window * 2, WINDOW)


def _hours(span: timedelta) -> str:
    return f"{span.total_seconds() / 3600:g} hours"


def window_start(now: datetime) -> datetime:
    """The start of the rolling window: midnight UTC ROLLING_DAYS - 1 days before today, so it holds exactly 30 UTC
    days (today and the 29 before). Always within Entra's retention."""
    return datetime.combine(now.astimezone(UTC).date() - timedelta(days=ROLLING_DAYS - 1), dtime.min, UTC)


def read_sign_ins(
    client: Client, cursor: datetime | None, now: datetime, key: bytes, pace: float, window: timedelta = WINDOW
) -> SignInRead:
    """The sign-ins from the cursor (at most the rolling window back) to now − LAG, window by window, starting with
    `window` (the size the last run ended with). A window that can't be read whole (too many pages, the run's time, a
    504) is halved; a throttled first page is waited out and read again at the same size. After a complete window the
    size doubles back towards WINDOW when the doubled window fits in the time left. A failure after at
    least one complete window keeps what was read (the cursor stops there, with a note); a refusal always fails the
    run. Running out of time before any window completed isn't a failure: the halved window is stored for the next
    run, so a large tenant gets smaller windows run by run until they fit."""
    floor = window_start(now)
    until = now - LAG
    start = cursor if cursor is not None and cursor > floor else floor
    read = SignInRead(Usage(), start, start, min(max(window, MIN_WINDOW), WINDOW))
    if cursor is None:
        read.notes.append(
            "First read of the sign-in logs: the last 30 days (Entra ID keeps them that long with P1 or P2)."
        )
    elif cursor < now - RETENTION:
        read.notes.append(
            f"Sign-ins from {_shown(cursor)} to {_shown(now - RETENTION)} were past Entra's 30-day retention and "
            "couldn't be read: the connector hadn't run since. Their users aren't counted."
        )
    pacer = Pacer(pace)
    time = ReadTime(READ_FOR)
    attempt = 0
    while read.until < until:
        end = min(read.until + read.window, until)
        pages_before = time.pages
        try:
            part = read_window(client, read.until, end, key, pacer, time)
        except WindowTooBig as e:
            if e.reason == "pages":
                if read.shrink():
                    continue
                raise SourceError(
                    f"More than {WINDOW_PAGES * SIGN_IN_TOP:,} sign-ins in one hour from {_shown(read.until)}; the "
                    "run stopped. The next run tries again."
                ) from None
            # Out of time: the window is halved for the next run, which starts from the cursor.
            if not read.shrink() and read.windows == 0:
                raise SourceError(
                    f"Not even an hour of sign-ins from {_shown(read.until)} could be read within the run's time. "
                    "The next run tries again."
                ) from None
            done = (
                f"Sign-ins were counted up to {_shown(read.until)}"
                if read.windows
                else "No complete window of sign-ins fitted in this run's time"
            )
            read.notes.append(f"{done}; the next run continues from there, reading {_hours(read.window)} at a time.")
            break
        except (Throttled, Refused) as e:
            status = e.status
            retry_after = e.retry_after if isinstance(e, Throttled) else None
            if isinstance(e, Refused) and status not in SHRINK_STATUSES:
                if status in TRANSIENT_STATUSES and read.windows:
                    read.notes.append(f"{e} Sign-ins were counted up to {_shown(read.until)}; the next run continues.")
                    break
                raise
            if isinstance(e, Refused):  # a 504: too big to answer in time. A throttle (429, 503) is waited out.
                read.shrink()
            reason = client.budget.why_not(attempt, retry_after)
            if reason or not client.budget.wait(attempt, retry_after):
                message = throttled(
                    graph.ThrottledOut(status, reason or "budget", retry_after), "read the sign-in logs"
                )
                if read.windows == 0:
                    raise SourceError(message) from None
                read.notes.append(f"{message} Sign-ins were counted up to {_shown(read.until)}.")
                break
            attempt += 1
            continue
        except SignInRefused:  # the tenant stopped issuing tokens: a configuration problem, never a partial read
            raise
        except SourceError as e:
            if read.windows == 0:
                raise
            read.notes.append(f"{e} Sign-ins were counted up to {_shown(read.until)}; the next run continues.")
            break
        attempt = 0
        read.usage.merge(part)
        read.until = end
        read.windows += 1
        read.smallest = min(read.smallest, read.window)
        read.grow(time.pages - pages_before, time)
    if read.smallest < WINDOW:
        read.notes.append(
            "Microsoft Graph timed out or had too many sign-ins for one read, or a run ran out of time (in this run or "
            "an earlier one); this run read some of them in "
            f"windows of {_hours(read.smallest)} instead of {WINDOW.days} days."
        )
    return read


# ---- the run -------------------------------------------------------------------------------------------------------


@dataclass
class Reading:
    """What `apply_fetched` writes in the run's transaction, besides the observations."""

    key: bytes
    cursor: datetime | None  # the state's cursor when the run read it
    read_until: datetime | None  # the new cursor; None when no complete window was read
    window: timedelta  # the window the next run starts with
    cutoff: date  # the oldest day the rolling window keeps
    usage: Usage
    windows: int


class ShadowFetched(Fetched):
    reading: Reading | None = None


def _own_tenant(client: Client) -> str:
    body = client.get(f"{GRAPH}/organization?$select=id", "read the tenant's ID", DIRECTORY_READ_ALL)
    rows = [r for r in body.get("value") or [] if isinstance(r, dict)]
    tenant = str(rows[0].get("id") or "").lower() if rows else ""
    if not entra.GUID.match(tenant):
        raise SourceError("Microsoft Graph didn't say the tenant's ID (GET /organization); the run stopped.")
    return tenant


def _scopes(text: Any) -> list[str]:
    return [s[:200] for s in str(text or "").split() if s]


def consents(grants: list[dict[str, Any]], app_of: dict[str, str]) -> dict[str, dict[str, Any]]:
    """{app ID: {consent, consenting_users, scopes, risky_scopes}} from the grants. Who consented is counted (distinct
    principal IDs, in memory) and never kept."""
    found: dict[str, dict[str, Any]] = {}
    for grant in grants:
        app = app_of.get(str(grant.get("clientId") or "").lower())
        if not app:
            continue
        row = found.setdefault(app, {"admin": False, "people": set(), "scopes": {}})
        if grant.get("consentType") == "AllPrincipals":
            row["admin"] = True
        elif grant.get("consentType") == "Principal" and grant.get("principalId"):
            row["people"].add(str(grant["principalId"]).lower())
        for scope in _scopes(grant.get("scope")):
            row["scopes"].setdefault(scope.lower(), scope)
    out = {}
    for app, row in found.items():
        names = sorted(row["scopes"].values(), key=str.lower)[:SCOPES_MAX]
        out[app] = {
            "consent": ConsentType.ADMIN if row["admin"] else ConsentType.USER if row["people"] else ConsentType.NONE,
            "consenting_users": len(row["people"]),
            "scopes": names,
            "risky_scopes": [s for s in names if is_risky(s)],
        }
    return out


def catalog_of(connector, app_ids: set[str]) -> dict[str, str]:
    """{app ID: "record" or "entra"} for the apps the catalog already has: a live application record with the app's
    Entra application ID, else an observation of an Entra connector of the organization (decided or not: it is in the
    review queue already). An app this connector's own candidate was linked or created from isn't listed: it stays
    this connector's candidate."""
    from catalog.models import Application

    if not app_ids:
        return {}
    org_id = connector.organization_id
    ours = set(
        Observation.objects.filter(
            connector=connector, kind=ObservationKind.APPLICATION, external_id__in=app_ids, decision__isnull=False
        )
        .exclude(decision__decision=DecisionKind.DISMISSED)
        .values_list("external_id", flat=True)
    )
    records = set(
        Application.objects.filter(organization_id=org_id, resource__archived_at__isnull=True)
        .annotate(app=Lower("entra_app_id"))
        .filter(app__in=app_ids)
        .values_list("app", flat=True)
    )
    observed = set(
        Observation.objects.filter(
            organization_id=org_id, connector__source=Source.ENTRA, gone_at__isnull=True, external_id__in=app_ids
        ).values_list("external_id", flat=True)
    )
    out = {}
    for app in app_ids - ours:
        if app in records:
            out[app] = "record"
        elif app in observed:
            out[app] = "entra"
    return out


def fetch(connector, credential, transport=None):
    """One object per app seen in the rolling window's sign-ins (stored and newly read) or in a consent grant, with
    its service principal, usage, consent and catalog match; plus, marked `inactive`, each app this connector observed
    before whose enterprise app is still in the tenant but which has neither now (out of scope, not gone). The new
    sign-in aggregates, the cursor and the window size ride along (`reading`) for apply_fetched, in the run's
    transaction."""
    scope = scope_of(connector)
    client = Client(transports(connector, credential, shadow_simulator, transport)(GRAPH_AUDIENCE.hosts), credential)
    now = clock().astimezone(UTC).replace(microsecond=0)
    state = ShadowItState.objects.filter(connector=connector).first()
    key = bytes(state.sketch_key) if state is not None else secrets.token_bytes(32)
    cursor = state.read_until if state is not None else None
    window = timedelta(minutes=state.window_minutes) if state is not None else WINDOW
    tenant = _own_tenant(client)
    sps = client.get_all(
        f"{GRAPH}/servicePrincipals?$select={SP_FIELDS}&$top=999",
        "list enterprise applications",
        DIRECTORY_READ_ALL,
        PAGE_LIMIT,
    )
    grants = client.get_all(
        f"{GRAPH}/oauth2PermissionGrants", "list OAuth consent grants", DIRECTORY_READ_ALL, PAGE_LIMIT
    )
    notes: list[str] = []
    read: SignInRead | None = None
    if scope.get("sign_ins", True):
        pace = 0.0 if credential.tenant_id == SIMULATED else PACE
        read = read_sign_ins(client, cursor, now, key, pace, window)
        notes.extend(read.notes)
    else:
        notes.append(
            "Sign-in logs weren't read (turned off in the scope): users and sign-ins are what earlier runs counted."
        )
    usage = read.usage if read is not None else Usage()
    cutoff = window_start(now).date()

    by_app: dict[str, dict[str, Any]] = {}
    app_of: dict[str, str] = {}
    for sp in sps:
        app = str(sp.get("appId") or "").lower()
        if entra.GUID.match(app):
            by_app.setdefault(app, sp)
            app_of[str(sp.get("id") or "").lower()] = app
    consent = consents(grants, app_of)

    # The rolling window: the stored days plus what this run read, per app.
    totals: dict[str, list[Any]] = {}
    for app, count, blob in ShadowItUsageDay.objects.filter(connector=connector, day__gte=cutoff).values_list(
        "app_id", "sign_ins", "users_sketch"
    ):
        slot = totals.setdefault(app, [0, Sketch()])
        slot[0] += count
        slot[1].merge(Sketch.load(blob))
    for (app, day), (count, sketch) in usage.days.items():
        if day >= cutoff:
            slot = totals.setdefault(app, [0, Sketch()])
            slot[0] += count
            slot[1].merge(sketch)
    last: dict[str, datetime] = {
        app: when
        for app, when in ShadowItApp.objects.filter(connector=connector).values_list("app_id", "last_sign_in_at")
        if when is not None
    }
    for app, when in usage.last.items():
        if app not in last or when > last[app]:
            last[app] = when
    # This connector's observations: their high-water marks of risk, and which are now inactive.
    observed: dict[str, tuple[dict[str, Any], bool]] = {
        external_id: (data if isinstance(data, dict) else {}, gone_at is None)
        for external_id, data, gone_at in Observation.objects.filter(
            connector=connector, kind=ObservationKind.APPLICATION
        ).values_list("external_id", "data", "gone_at")
    }

    # An app this connector observed whose enterprise app is no longer in the tenant isn't listed, whatever usage it
    # has: the engine marks it "no longer seen" (a linked record then shows as missing). Unobserved apps without one
    # stay listed, so the run counts them under "no enterprise app".
    apps = {app for app in set(totals) | set(consent) if app in by_app or app not in observed}
    inactive = {app for app, (_data, live) in observed.items() if live and app not in apps and app in by_app}
    matched = catalog_of(connector, apps)
    found = ShadowFetched(notes=notes)
    for app in sorted(apps | inactive):
        count, sketch = totals.get(app, [0, None])
        found.append(
            {
                "app_id": app,
                "servicePrincipal": by_app.get(app),
                "sign_ins": count,
                "users": sketch.estimate() if sketch is not None else 0,
                "last_sign_in_at": stamp(last[app]) if app in last else "",
                "sign_ins_read": read is not None,
                "consent": consent.get(app),
                "risk_seen": observed.get(app, ({}, False))[0].get("risk_seen"),
                "inactive": app in inactive,
                "catalog": matched.get(app, ""),
                "tenant": tenant,
            }
        )
    found.reading = Reading(
        key=key,
        cursor=cursor,
        read_until=read.until if read is not None and read.windows else None,
        window=read.window if read is not None else window,
        cutoff=cutoff,
        usage=usage,
        windows=read.windows if read is not None else 0,
    )
    return found


def _risk_seen(previous: Any, consent: str, risky: list[str]) -> dict[str, Any]:
    """The high-water mark of an app's risk: the most permissive consent type and every high-risk scope ever seen on
    it. Only a rise changes it, so only a rise brings a dismissed candidate back."""
    before = previous if isinstance(previous, dict) else {}
    old = before.get("consent", ConsentType.NONE)
    most = max((old, consent), key=lambda c: CONSENT_RANK.get(c, 0))
    scopes_seen: dict[str, str] = {}
    for scope in [*(before.get("risky_scopes") or []), *risky]:
        if isinstance(scope, str):
            scopes_seen.setdefault(scope.lower(), scope)
    return {"consent": str(most), "risky_scopes": sorted(scopes_seen.values(), key=str.lower)}


def map_object(raw):
    sp = raw.get("servicePrincipal") or {}
    consent = raw.get("consent") or {}
    publisher = sp.get("verifiedPublisher")
    verified: dict[str, Any] = publisher if isinstance(publisher, dict) else {}
    app = raw["app_id"]
    name = sp.get("displayName") or app
    consent_type = consent.get("consent", ConsentType.NONE)
    risky = list(consent.get("risky_scopes") or [])
    return {
        "kind": ObservationKind.APPLICATION,
        "external_id": app,
        "display_name": str(name)[:400],
        "ai_agent": False,
        # Read by skip_reason, not stored: the catalog match, the tenant's own ID, and no use or consent this run.
        "catalog": raw.get("catalog", ""),
        "tenant": raw.get("tenant", ""),
        "inactive": bool(raw.get("inactive")),
        "data": {
            "app_id": app,
            "service_principal_id": str(sp.get("id") or "").lower(),
            "enterprise_app": bool(sp),
            "description": str(sp.get("description") or "")[:DESCRIPTION_MAX],
            "homepage": str(sp.get("homepage") or ""),
            "publisher": str(sp.get("publisherName") or verified.get("displayName") or "")[:300],
            "verified_publisher": str(verified.get("displayName") or "")[:300],
            "publisher_tenant": str(sp.get("appOwnerOrganizationId") or "").lower(),
            "enabled": bool(sp.get("accountEnabled", True)) if sp else True,
            "type": str(sp.get("servicePrincipalType") or ("Application" if sp else "")),
            "users_30d": int(raw.get("users") or 0),
            "sign_ins_30d": int(raw.get("sign_ins") or 0),
            "last_sign_in_at": raw.get("last_sign_in_at") or "",
            "sign_ins_read": bool(raw.get("sign_ins_read")),
            "consent": consent_type,
            "consenting_users": int(consent.get("consenting_users") or 0),
            "scopes": list(consent.get("scopes") or []),
            "risky_scopes": risky,
            "risk_seen": _risk_seen(raw.get("risk_seen"), consent_type, risky),
        },
    }


def skip_reason(values, scope):
    data = values["data"]
    scope = dict(SCOPE_DEFAULTS) | dict(scope or {})
    if not data["service_principal_id"]:
        return "no_service_principal"
    if data["type"] != "Application":
        return "not_application"
    first_party = {t.lower() for t in scope.get("first_party_tenants") or []}
    if scope.get("exclude_microsoft", True) and data["publisher_tenant"] in first_party:
        return "microsoft"
    if not data["publisher_tenant"] or data["publisher_tenant"] == values.get("tenant"):
        return "own_tenant"
    if values.get("inactive"):
        return "inactive" if scope.get("sign_ins", True) else "not_read"
    if values.get("catalog"):
        return "in_catalog"
    return "name_exclusions" if scopes.excluded_by_name(values["display_name"], scope) else None


def relevant(values):
    """What brings a dismissed candidate back: a new name, or a rise of its risk (a consent type more permissive, or a
    high-risk scope, than it ever had). Usage, and losing a scope or a consent, never do."""
    return {"name": values["display_name"], "risk": values["data"]["risk_seen"]}


# ---- in the run's transaction (discovery/engine._apply) -----------------------------------------------------------


def apply_fetched(connector, raw, observations, now) -> dict[str, Any]:
    """Store what the run read, in its transaction: merge the new daily aggregates into ShadowItUsageDay (count added,
    sketches merged), drop days older than the rolling window, rewrite the ShadowItApp summaries of the third-party
    apps kept (candidates and apps already in the catalog), and move the cursor and store the window size. Nothing
    about an app a filter skipped (Microsoft's, the tenant's own, name exclusions) is written; usage days it had from
    before stay until they age out of the rolling window."""
    reading = getattr(raw, "reading", None)
    if reading is None:
        return {}
    org_id = connector.organization_id
    state = ShadowItState.objects.select_for_update().filter(connector=connector).first()
    if state is None:
        state = ShadowItState.objects.create(organization_id=org_id, connector=connector, sketch_key=reading.key)
    elif bytes(state.sketch_key) != reading.key or state.read_until != reading.cursor:
        raise SourceError("Another run of this connector read sign-ins meanwhile; nothing was applied. Run it again.")
    scope = scope_of(connector)
    kept: dict[str, tuple[dict[str, Any], str | None]] = {}
    for obj in raw:
        values = map_object(obj)
        reason = skip_reason(values, scope)
        if reason in (None, "in_catalog"):
            kept[values["external_id"]] = (values, reason)

    days = {k: v for k, v in reading.usage.days.items() if k[0] in kept and k[1] >= reading.cutoff}
    stored = {
        (day_row.app_id, day_row.day): day_row
        for day_row in ShadowItUsageDay.objects.select_for_update().filter(
            connector=connector, app_id__in={a for a, _ in days}, day__in={d for _, d in days}
        )
    }
    created, updated = [], []
    for (app, day), (count, sketch) in days.items():
        day_row = stored.get((app, day))
        if day_row is None:
            created.append(
                ShadowItUsageDay(
                    organization_id=org_id,
                    connector=connector,
                    app_id=app,
                    day=day,
                    sign_ins=count,
                    users=sketch.estimate(),
                    users_sketch=sketch.dump(),
                )
            )
            continue
        merged = Sketch.load(day_row.users_sketch)
        merged.merge(sketch)
        day_row.sign_ins += count
        day_row.users = merged.estimate()
        day_row.users_sketch = merged.dump()
        day_row.updated_at = now
        updated.append(day_row)
    ShadowItUsageDay.objects.bulk_create(created, batch_size=500)
    ShadowItUsageDay.objects.bulk_update(updated, ["sign_ins", "users", "users_sketch", "updated_at"], batch_size=500)
    pruned, _ = ShadowItUsageDay.objects.filter(connector=connector, day__lt=reading.cutoff).delete()

    rows = {row.app_id: row for row in ShadowItApp.objects.select_for_update().filter(connector=connector)}
    new_rows, changed_rows = [], []
    for app, (values, reason) in kept.items():
        fields = _app_fields(values, reason, now)
        row = rows.pop(app, None)
        if row is None:
            new_rows.append(
                ShadowItApp(organization_id=org_id, connector=connector, app_id=app, first_seen_at=now, **fields)
            )
            continue
        for name, value in fields.items():
            setattr(row, name, value)
        row.updated_at = now
        changed_rows.append(row)
    ShadowItApp.objects.bulk_create(new_rows, batch_size=500)
    ShadowItApp.objects.bulk_update(changed_rows, [*APP_FIELDS, "updated_at"], batch_size=500)
    ShadowItApp.objects.filter(id__in=[row.id for row in rows.values()]).delete()
    if reading.read_until is not None:
        state.read_until = reading.read_until
    state.window_minutes = int(reading.window.total_seconds() // 60)
    state.save(update_fields=["read_until", "window_minutes", "updated_at"])
    return {
        "sign_ins_counted": reading.usage.counted,
        "sign_ins_failed": reading.usage.failed,
        "sign_in_windows": reading.windows,
        "in_catalog": sum(1 for _values, reason in kept.values() if reason == "in_catalog"),
        "usage_days_pruned": pruned,
    }


# The ShadowItApp fields a run writes (besides the keys and first_seen_at).
APP_FIELDS = (
    "service_principal_id",
    "display_name",
    "publisher",
    "verified_publisher",
    "users_30d",
    "sign_ins_30d",
    "last_sign_in_at",
    "consent",
    "consenting_users",
    "scopes",
    "risky_scopes",
    "risky",
    "in_catalog",
    "last_seen_at",
)


def _app_fields(values, reason, now) -> dict[str, Any]:
    data = values["data"]
    fields = {
        "service_principal_id": data["service_principal_id"][:36],
        "display_name": values["display_name"],
        "publisher": data["publisher"],
        "verified_publisher": data["verified_publisher"],
        "users_30d": data["users_30d"],
        "sign_ins_30d": data["sign_ins_30d"],
        "last_sign_in_at": parse_time(data["last_sign_in_at"]),
        "consent": data["consent"],
        "consenting_users": data["consenting_users"],
        "scopes": data["scopes"],
        "risky_scopes": data["risky_scopes"],
        "risky": bool(data["risky_scopes"]),
        "in_catalog": reason == "in_catalog",
        "last_seen_at": now,
    }
    return fields


# ---- access check ----------------------------------------------------------------------------------------------------


def probes(scope) -> list[Probe]:
    since = clock() - timedelta(hours=1)
    return [
        Probe("Read the tenant's ID", f"{GRAPH}/organization?$select=id", DIRECTORY_READ_ALL),
        Probe("Read enterprise applications", f"{GRAPH}/servicePrincipals?$top=1&$select=id", DIRECTORY_READ_ALL),
        Probe("Read OAuth consent grants", f"{GRAPH}/oauth2PermissionGrants", DIRECTORY_READ_ALL),
        Probe(
            "Read sign-in logs",
            sign_ins_url(since, clock(), top=1),
            AUDIT_LOG_READ_ALL,
            when="sign_ins",
        ),
    ]


def access_steps(connector, scope, session) -> list[dict[str, Any]]:
    """Sign in to Graph, then one small read per permission. A tenant without Entra ID P1 or P2 is a failed step
    with the problem `missing_licence` (not a missing permission): the sign-in logs need the licence."""
    from . import access

    client = session.client(GRAPH_AUDIENCE)
    steps = [access.sign_in(client)]
    wanted = probes(scope)
    if steps[0]["state"] != "ok":
        return steps + [access.skipped(p.name, p.permission) for p in wanted]
    for p in wanted:
        if p.when and not scope.get(p.when, True):
            steps.append(access.not_used(p.name, p.permission, "Read sign-in logs"))
            continue
        step, _body = access.probe(client, p)
        if step["code"] == LICENCE_CODE:
            step.update(
                problem="missing_licence",
                problem_label="Missing license",
                detail=(
                    f"Microsoft Graph refused the read (403 {LICENCE_CODE}): the tenant has no Microsoft Entra ID P1 "
                    "or P2 license, which reading sign-in logs needs."
                ),
                next=(
                    "Add an Entra ID P1 or P2 license to the tenant, or turn off Read sign-in logs in the connector's "
                    "scope to use consent grants only."
                ),
            )
        steps.append(step)
    return steps
