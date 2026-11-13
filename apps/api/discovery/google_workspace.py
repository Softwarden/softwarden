"""The Google Workspace source (capability `discovery.google`): the
third-party apps people in the organization granted access to their Google Workspace data, as application candidates
with how many people granted access and with which scopes, through the connector's GoogleWorkspaceConnection
(core/google_workspace.py: a service account with domain-wide delegation acting as a Workspace administrator).

Not Microsoft, so nothing here is on Microsoft Learn. Checked on 2026-11-10 against Google's Admin SDK documentation
(docs/PERMISSIONS.md, "Discovery source: `google_workspace`"):

- Reports API `activities.list`: `GET https://admin.googleapis.com/admin/reports/v1/activity/users/all/applications/
  token`, scope `https://www.googleapis.com/auth/admin.reports.audit.readonly`, with `eventName=authorize` (a user
  authorized an application's access to their data; parameters `app_name`, `client_id`, `client_type`, `scope`),
  `startTime` and `endTime` (RFC 3339), `maxResults` up to 1000, `pageToken` from `nextPageToken`. Activity data goes
  back 180 days. The OAuth token audit log is the least-privileged way to see third-party apps: the Directory API's
  per-user `tokens.list` would need `admin.directory.user.security`, which can also revoke tokens, and a call per user.
- Limits: 2,400 queries per minute per project, and 250 filter queries (`eventName` is one) per minute for
  activities.list; quota errors are 503 (Reports API limits page), 429 `rateLimitExceeded`, and 403
  `userRateLimitExceeded` or `quotaExceeded` (Directory API limits page), all retried with exponential backoff from 5
  seconds (discovery/rest_source.py).

A run reads the authorize events of the last `days` days (30 by default, 1 to 180) and keeps, per OAuth client ID,
only aggregates: the app's name and client type, how many distinct people authorized it in the window (`users`), how
many authorizations (`grants`), when the last one was, and the scopes granted with the high-risk ones. Privacy: no
event, user email, profile ID or IP address is stored, logged or kept past the run. Distinct people are counted in
memory with HMAC-SHA256 under a random key made for the run and thrown away with it, so even the run's memory holds no
user identifier after the event is read; only the counts are stored. The window is re-read every run, so nothing
incremental (no sketch) is kept either.

`authorize` fires only when someone consents, so a window with no authorization at all is normal in a small tenant:
such a run succeeds with a note and changes nothing (every observation keeps its state).

An app nobody authorized in the window isn't "no longer seen": its grants may still be live, so it is out of scope
("no authorization in the reporting window") and keeps its state, like the shadow IT source's inactive apps. Usage
numbers don't count as a change (VOLATILE); a dismissed candidate comes back on a new name or a new high-risk scope.
An administrator address in `google.simulated.invalid` selects the synthetic Workspace
(discovery/google_workspace_simulator.py)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

from core import connections, google_workspace, simulation

from . import access, rest_source, scopes
from .graph_source import Fetched, Permission, SignInRefused, SourceError
from .models import ObservationKind

ALL_UNREAD = "*"  # discovery/engine.ALL_UNREAD: nothing of the connector becomes out of scope or gone this run
REPORTS = f"https://{google_workspace.REPORTS_HOST}/admin/reports/v1/activity/users/all/applications/token"
PAGE_SIZE = 1000  # activities.list's maximum
PAGE_LIMIT = 500  # 500,000 authorizations in one window at most
DAYS_MAX = 180  # Google keeps activity data 180 days
SCOPES_MAX = 100
# Scopes Softwarden marks high-risk: full mail (the Gmail scopes Google treats as restricted), full Drive, admin APIs
# and all of Google Cloud. Softwarden's judgement, not Google's list; a candidate shows every scope either way.
HIGH_RISK = frozenset(
    {
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/cloud-platform",
    }
)
HIGH_RISK_PREFIXES = ("https://www.googleapis.com/auth/gmail.", "https://www.googleapis.com/auth/admin.")
NOT_HIGH_RISK = frozenset({"https://www.googleapis.com/auth/gmail.labels"})

REPORTS_SCOPE = Permission(
    google_workspace.REPORTS_SCOPE,
    "",
    True,
    "Read the OAuth token audit log (Reports API, activities of the application token): which third-party apps "
    "people authorized, with which scopes. Granted to the service account's client ID through domain-wide "
    "delegation, a tenant-wide grant only a super administrator can make. Read only.",
    False,  # not Microsoft: there is no Learn page; Google's documentation is the reference
    kind="google_scope",
    advice="A super administrator adds the service account's client ID under Security, Access and data control, API "
    f"controls, Domain-wide delegation, with only the scope {google_workspace.REPORTS_SCOPE}. Then test the "
    "connection and check again.",
)
REPORTS_ADMIN = Permission(
    "Reports admin privilege",
    "",
    True,
    "The administrator the service account acts as must be allowed to read audit reports: a super administrator, or "
    "an admin role with the Reports privilege.",
    False,
    kind="google_admin_role",
    advice="In the Google Admin console (Account, Admin roles), give the administrator the connection acts as a role "
    "with the Reports privilege, or use a super administrator; or the Admin SDK API isn't enabled in the service "
    "account's Google Cloud project (APIs & Services, enable Admin SDK API). Then check again.",
)

KINDS = [ObservationKind.APPLICATION]
CONNECTION = "google_workspace"
CONFIG_FIELDS: tuple[str, ...] = ()
SCOPE_DEFAULTS: dict[str, Any] = {"days": 30, "min_users": 1, "name_exclusions": []}
FILTERS = {
    "no_id": "no OAuth client ID",
    "few_users": "fewer people than the scope's minimum",
    "not_recent": "no authorization in the reporting window",
    "name_exclusions": "name excluded",
}
PERMISSIONS = [REPORTS_SCOPE, REPORTS_ADMIN]
DETAILS = (
    "client_id",
    "client_type",
    "users",
    "grants",
    "last_authorized_at",
    "days",
    "scopes",
    "risky_scopes",
    "risky",
)
VOLATILE = ("users", "grants", "last_authorized_at", "days")


def is_risky(scope: str) -> bool:
    if scope in NOT_HIGH_RISK:
        return False
    return scope in HIGH_RISK or scope.startswith(HIGH_RISK_PREFIXES)


def simulator(connector):
    from . import google_workspace_simulator

    return google_workspace_simulator.Transport(connector)


def scope_of(connector) -> dict[str, Any]:
    return dict(SCOPE_DEFAULTS) | dict(connector.scope or {})


def validate(config, scope, errors):
    days = scope.get("days")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= DAYS_MAX:
        errors["scope.days"] = [f"Enter a number of days from 1 to {DAYS_MAX} (Google keeps 180 days)."]
    users = scope.get("min_users")
    if isinstance(users, bool) or not isinstance(users, int) or not 1 <= users <= 100_000:
        errors["scope.min_users"] = ["Enter a whole number of people, 1 or more."]
    scopes.validate_names(scope, errors)


# ---- reading -------------------------------------------------------------------------------------------------------


def _error_code(body: Any) -> str:
    """Google's error body: {"error": {"code", "message", "status", "errors": [{"reason"}]}}; the status (or the first
    reason) is read, never the message."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return ""
    reasons = error.get("errors")
    reason = reasons[0].get("reason") if isinstance(reasons, list) and reasons and isinstance(reasons[0], dict) else ""
    return str(error.get("status") or reason or "")


# Google's documented quota errors that come as a 403 (Admin SDK "Limits and quotas", Directory API page, checked
# 2026-11-12): `userRateLimitExceeded` (the per-user rate) and `quotaExceeded` (concurrent requests), both "retry using
# exponential back-off"; `rateLimitExceeded` is the 429 form, and older Google APIs send it as a 403 too. A 403 with
# any other reason (`forbidden`, `insufficientPermissions`) is a refusal.
QUOTA_REASONS = frozenset({"userRateLimitExceeded", "quotaExceeded", "rateLimitExceeded"})


def _throttled(status: int, body: Any) -> bool:
    """A 403 whose error reasons are all quota reasons."""
    if status != 403:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    reasons = error.get("errors") if isinstance(error, dict) else None
    found = [r.get("reason") for r in reasons if isinstance(r, dict)] if isinstance(reasons, list) else []
    return bool(found) and all(reason in QUOTA_REASONS for reason in found)


SERVICE = rest_source.Service(
    "Google Workspace", frozenset({(google_workspace.REPORTS_HOST, 443)}), 5.0, _error_code, _throttled
)


def transport_for(connector, credential, transport=None):
    """`transport` (tests), the synthetic Workspace for its reserved domain (SourceError unless
    core/simulation.allowed()), else core/outbound under the public rule."""
    if transport is not None:
        return transport
    if google_workspace.is_simulated(credential.admin_email):
        if not simulation.allowed():
            raise SourceError(simulation.MESSAGE)
        return simulator(connector)
    return google_workspace.transport_for(credential.admin_email)


def client(connector, credential, transport=None, **limits) -> rest_source.Api:
    transport = transport_for(connector, credential, transport)

    def sign_in() -> str:
        try:
            body = google_workspace.request_token(
                transport, credential.client_email, credential.secret, credential.admin_email
            )
        except google_workspace.KeyProblem as e:
            raise SignInRefused(str(e), "not_configured") from None
        except google_workspace.GoogleError as e:
            result = connections.classify_google(e, credential.client_id)
            raise SignInRefused(
                f"Google refused to sign in: {result.detail} {result.next}".strip(), result.problem, result.code
            ) from None
        return str(body["access_token"])

    return rest_source.Api(transport, SERVICE, sign_in, **limits)


def stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def authorize_url(start: datetime, end: datetime, page_token: str = "", size: int = PAGE_SIZE) -> str:
    query = {"eventName": "authorize", "startTime": stamp(start), "endTime": stamp(end), "maxResults": str(size)}
    if page_token:
        query["pageToken"] = page_token
    return f"{REPORTS}?{urllib.parse.urlencode(query)}"


def _parameters(event: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in event.get("parameters") or []:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str):
            continue
        if isinstance(p.get("multiValue"), list):
            out[p["name"]] = [str(v) for v in p["multiValue"]]
        elif "value" in p:
            out[p["name"]] = str(p["value"])
    return out


class Tally:
    """One run's aggregates per OAuth client. `key` is the run's random HMAC key: actors are hashed with it as soon as
    they are read and the hashes only live in `people` until the run ends; nothing of them is returned."""

    def __init__(self) -> None:
        self.key = secrets.token_bytes(32)
        self.apps: dict[str, dict[str, Any]] = {}
        self.people: dict[str, set[bytes]] = {}

    def add(self, activity: dict[str, Any]) -> None:
        actor: dict[str, Any] = activity["actor"] if isinstance(activity.get("actor"), dict) else {}
        who = str(actor.get("profileId") or actor.get("email") or "").strip().lower()
        ident: dict[str, Any] = activity["id"] if isinstance(activity.get("id"), dict) else {}
        when = str(ident.get("time") or "")[:40]
        for event in activity.get("events") or []:
            if not isinstance(event, dict) or event.get("name") != "authorize":
                continue
            params = _parameters(event)
            client_id = str(params.get("client_id") or "").strip()[:200]
            scope = params.get("scope") or []
            granted = scope if isinstance(scope, list) else str(scope).split()
            app = self.apps.setdefault(
                client_id,
                {"client_id": client_id, "app_name": "", "client_type": "", "grants": 0, "last": "", "scopes": set()},
            )
            app["grants"] += 1
            if when >= app["last"]:
                app["last"] = when
                app["app_name"] = str(params.get("app_name") or app["app_name"])[:400]
                app["client_type"] = str(params.get("client_type") or app["client_type"])[:40]
            app["scopes"].update(s[:300] for s in granted if s)
            if who:
                digest = hmac.new(self.key, who.encode(), hashlib.sha256).digest()[:16]
                self.people.setdefault(client_id, set()).add(digest)

    def results(self, days: int) -> list[dict[str, Any]]:
        out = []
        for client_id, app in sorted(self.apps.items()):
            granted = sorted(app["scopes"])
            out.append(
                {
                    "client_id": client_id,
                    "app_name": app["app_name"],
                    "client_type": app["client_type"],
                    "users": len(self.people.get(client_id, ())),
                    "grants": app["grants"],
                    "last_authorized_at": app["last"],
                    "scopes": granted[:SCOPES_MAX],
                    "days": days,
                }
            )
        self.people.clear()
        self.key = b""
        return out


def fetch(connector, credential, transport=None):
    """The window's authorize events, page by page, as app-level aggregates (Tally)."""
    scope = scope_of(connector)
    days = int(scope["days"])
    end = datetime.fromtimestamp(google_workspace.clock(), UTC).replace(microsecond=0)
    start = end - timedelta(days=days)
    api = client(connector, credential, transport)
    tally = Tally()
    token = ""
    for _ in range(PAGE_LIMIT):
        body = api.get(authorize_url(start, end, token), "read the OAuth token audit log", REPORTS_ADMIN).body
        if not isinstance(body, dict):
            raise SourceError("Google Workspace answered without the expected report while reading the token log.")
        for activity in body.get("items") or []:
            if isinstance(activity, dict):
                tally.add(activity)
        token = str(body.get("nextPageToken") or "")
        if not token:
            break
    else:
        raise SourceError(
            f"The token audit log has more than a run reads in {days} days; shorten the window in the scope."
        )
    apps = tally.results(days)
    if not apps:
        # `authorize` fires only when someone consents, so a quiet window is normal in a small tenant. Nothing is
        # learnt about the apps seen before: they keep their state (the whole source counts as unread this run).
        return Fetched(
            [],
            [f"No authorization in the last {days} days: nothing was changed."],
            unread=[ALL_UNREAD],
        )
    return Fetched(apps)


# ---- mapping -------------------------------------------------------------------------------------------------------


def map_object(raw):
    client_id = str(raw.get("client_id") or "")[:200]
    granted = [s for s in raw.get("scopes") or [] if isinstance(s, str)]
    risky = [s for s in granted if is_risky(s)]
    return {
        "kind": ObservationKind.APPLICATION,
        "external_id": client_id,
        "display_name": (str(raw.get("app_name") or "") or f"OAuth client {client_id}")[:400],
        "ai_agent": False,
        "data": {
            "client_id": client_id,
            "client_type": str(raw.get("client_type") or ""),
            "users": int(raw.get("users") or 0),
            "grants": int(raw.get("grants") or 0),
            "last_authorized_at": str(raw.get("last_authorized_at") or ""),
            "days": int(raw.get("days") or 0),
            "scopes": granted,
            "risky_scopes": risky,
            "risky": bool(risky),
            "description": "",
            "enterprise_app": False,
        },
    }


def skip_reason(values, scope):
    scope = dict(SCOPE_DEFAULTS) | dict(scope or {})
    data = values["data"]
    if not data["client_id"]:
        return "no_id"
    if data["users"] < int(scope.get("min_users") or 1):
        return "few_users"
    return "name_exclusions" if scopes.excluded_by_name(values["display_name"], scope) else None


def scope_reason(observation, scope) -> str:
    """An observed app the window no longer shows: nobody authorized it lately, which doesn't mean its grants are
    gone, so it is out of scope rather than "no longer seen"."""
    return "not_recent"


def relevant(values):
    """What brings a dismissed candidate back: its name or a high-risk scope it didn't have."""
    return {"name": values["display_name"], "risky_scopes": sorted(values["data"]["risky_scopes"])}


def drift_values(observation, kind) -> dict[str, Any]:
    return {"name": observation.display_name}


# ---- the access check ------------------------------------------------------------------------------------------------


def check_steps(connector, scope, credential, transport=None) -> list[dict[str, Any]]:
    """Sign in to Google acting as the administrator, then read one authorize event of the last day."""
    api = client(
        connector, credential, transport, budget_seconds=access.BUDGET_SECONDS, deadline=access.DEADLINE_SECONDS
    )
    steps = [rest_source.sign_in_step(api, REPORTS_SCOPE)]
    if steps[-1]["state"] != "ok":
        return steps + [access.skipped("Read the OAuth token audit log", REPORTS_ADMIN)]
    end = datetime.fromtimestamp(google_workspace.clock(), UTC).replace(microsecond=0)
    url = authorize_url(end - timedelta(days=1), end, size=1)
    return steps + [rest_source.probe(api, "Read the OAuth token audit log", url, REPORTS_ADMIN)[0]]
