"""A synthetic Google Workspace for the google_workspace source (discovery/google_workspace.py): the token endpoint
(core/google_workspace.SimulatedTokens) and the Reports API's `activities.list` for the application `token`
(`GET https://admin.googleapis.com/admin/reports/v1/activity/users/all/applications/token`), answering
`{"kind": "reports#activities", "items": [...], "nextPageToken"}` with authorize events shaped as Google documents them
(`id.time`, `actor.email` and `actor.profileId`, `events[].parameters` with `app_name`, `client_id`, `client_type` and
the multi-valued `scope`). Selected by an administrator address in google.simulated.invalid; nothing leaves the machine.

Six third-party apps, authorized by people at Contoso over the last weeks: Notion (Drive file access, 3 people),
Calendly (calendar, 2), Zapier (Gmail and Drive: high-risk, 2), a Dropbox Chrome extension (1), Grammarly (1) and an
old Lucidchart grant 45 days ago (outside the default 30-day window). Events carry real-looking actor emails and
profile IDs, so a test can prove none of them is stored.

Config keys (tests only): `simulator_changes` ({"op", "id" (app name), "value"}: rename, remove, add (value: a scope),
grant (one more person authorizes it today; value: their email)); `simulated_size` (that many generated apps more);
`simulated_page` (activities per page); `simulated_empty` (no events); `simulated_denied` (the administrator lacks
the Reports privilege: reads answer 403); `simulated_errors` ({"path": status}, echoing the bearer token);
`simulated_throttle` ({"path", "times", "status", "retry_after"}: 503 quota errors by default);
`simulated_token_echo`."""

from __future__ import annotations

import copy
import json
import urllib.parse
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from core import google_workspace
from core.graph import Reply

PATH = "/admin/reports/v1/activity/users/all/applications/token"
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
DRIVE = "https://www.googleapis.com/auth/drive"
CALENDAR = "https://www.googleapis.com/auth/calendar.events"
PROFILE = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)
PEOPLE = {
    "ana": ("ana.lima@contoso.example", "104000000000000000001"),
    "ben": ("ben.okafor@contoso.example", "104000000000000000002"),
    "chi": ("chi.nguyen@contoso.example", "104000000000000000003"),
    "dee": ("dee.martin@contoso.example", "104000000000000000004"),
}
# (app name, client ID, client type, scopes, [(person, days ago), ...])
BASE: list[dict[str, Any]] = [
    {
        "name": "Notion",
        "client_id": "1001-notion.apps.googleusercontent.com",
        "type": "WEB",
        "scopes": [*PROFILE, DRIVE_FILE],
        "grants": [("ana", 2), ("ben", 5), ("chi", 9), ("ana", 20)],
    },
    {
        "name": "Calendly",
        "client_id": "1002-calendly.apps.googleusercontent.com",
        "type": "WEB",
        "scopes": [*PROFILE, CALENDAR],
        "grants": [("ben", 1), ("dee", 12)],
    },
    {
        "name": "Zapier",
        "client_id": "1003-zapier.apps.googleusercontent.com",
        "type": "WEB",
        "scopes": [*PROFILE, GMAIL_MODIFY, DRIVE],
        "grants": [("chi", 3), ("dee", 4)],
    },
    {
        "name": "Dropbox for Gmail",
        "client_id": "1004-dropbox.apps.googleusercontent.com",
        "type": "NATIVE_CHROME_EXTENSION",
        "scopes": [*PROFILE],
        "grants": [("ana", 7)],
    },
    {
        "name": "Grammarly",
        "client_id": "1005-grammarly.apps.googleusercontent.com",
        "type": "WEB",
        "scopes": [*PROFILE],
        "grants": [("dee", 15)],
    },
    {
        "name": "Lucidchart",
        "client_id": "1006-lucid.apps.googleusercontent.com",
        "type": "WEB",
        "scopes": [*PROFILE, DRIVE_FILE],
        "grants": [("ben", 45)],
    },
]


def apps(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    if config.get("simulated_empty"):
        return []
    rows = copy.deepcopy(BASE)
    for i in range(int(config.get("simulated_size") or 0)):
        rows.append(
            {
                "name": f"Generated app {i + 1:04d}",
                "client_id": f"9{i:05d}-gen.apps.googleusercontent.com",
                "type": "WEB",
                "scopes": [*PROFILE],
                "grants": [("ana", 1)],
            }
        )
    for change in config.get("simulator_changes") or []:
        op, name, value = change.get("op"), change.get("id"), change.get("value")
        target = next((r for r in rows if r["name"] == name), None)
        if op == "add":
            rows.append(
                {
                    "name": name,
                    "client_id": f"2{len(rows):04d}-added.apps.googleusercontent.com",
                    "type": "WEB",
                    "scopes": [*PROFILE, *([value] if value else [])],
                    "grants": [("ana", 0)],
                }
            )
        elif target is None:
            continue
        elif op == "rename":
            target["name"] = value
        elif op == "remove":
            rows.remove(target)
        elif op == "scope":
            target["scopes"] = [*target["scopes"], value]
        elif op == "grant":
            target["grants"] = [*target["grants"], (value, 0)]
    return rows


def activities(config: Mapping[str, Any], start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Every authorize event in [start, end], newest first, as Google returns them."""
    out: list[dict[str, Any]] = []
    for app in apps(config):
        for n, (who, days_ago) in enumerate(app["grants"]):
            when = end - timedelta(days=days_ago, minutes=n + 1)
            if not start <= when <= end:
                continue
            email, profile = PEOPLE.get(who, (who, ""))
            out.append(
                {
                    "kind": "admin#reports#activity",
                    "id": {
                        "time": when.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "uniqueQualifier": f"-{len(out) + 1:018d}",
                        "applicationName": "token",
                        "customerId": "C0simulated",
                    },
                    "actor": {"email": email, "profileId": profile, "callerType": "USER"},
                    "ipAddress": "203.0.113.7",
                    "events": [
                        {
                            "type": "auth",
                            "name": "authorize",
                            "parameters": [
                                {"name": "app_name", "value": app["name"]},
                                {"name": "client_id", "value": app["client_id"]},
                                {"name": "client_type", "value": app["type"]},
                                {"name": "scope", "multiValue": list(app["scopes"])},
                            ],
                        }
                    ],
                }
            )
    return sorted(out, key=lambda a: a["id"]["time"], reverse=True)


def _json(status: int, body: Any, headers: Mapping[str, str] | None = None) -> Reply:
    return Reply(status, {"content-type": "application/json", **(headers or {})}, json.dumps(body).encode())


def _error(status: int, reason: str, message: str, headers: Mapping[str, str] | None = None) -> Reply:
    statuses = {401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 429: "RESOURCE_EXHAUSTED", 503: "UNAVAILABLE"}
    body = {
        "error": {
            "code": status,
            "message": message,
            "errors": [{"message": message, "domain": "global", "reason": reason}],
            "status": statuses.get(status, "INVALID_ARGUMENT"),
        }
    }
    return _json(status, body, headers)


def _time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


class Transport:
    """The synthetic Workspace. `calls` records (method, url); `sent` (host, Authorization) for every read."""

    def __init__(self, connector=None, config=None):
        self.config = dict(config if config is not None else getattr(connector, "config", {}) or {})
        self.tokens = google_workspace.SimulatedTokens(echo=bool(self.config.get("simulated_token_echo")))
        self.calls: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.throttled = 0

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Reply:
        parts = urllib.parse.urlsplit(url)
        self.calls.append((method, url))
        if method == "POST":
            return self.tokens.send(method, url, headers, body)
        bearer = headers.get("Authorization", "")
        self.sent.append((parts.hostname or "", bearer))
        if method != "GET" or parts.hostname != google_workspace.REPORTS_HOST or parts.scheme != "https":
            return _error(400, "badRequest", "Bad request.")
        if bearer != f"Bearer {google_workspace.TOKEN}":
            return _error(401, "authError", "Invalid Credentials")
        errors = self.config.get("simulated_errors") or {}
        if parts.path in errors:  # echoes the bearer token, which must never reach a message
            return _error(int(errors[parts.path]), "backendError", f"Backend error with {bearer}")
        throttle = self.config.get("simulated_throttle") or {}
        if throttle.get("path") == parts.path and self.throttled < int(throttle.get("times", 1)):
            self.throttled += 1
            retry = throttle.get("retry_after")
            status = int(throttle.get("status", 503))
            return _error(status, "quotaExceeded", "Quota exceeded.", {"retry-after": str(retry)} if retry else {})
        if parts.path != PATH:
            return _error(404, "notFound", "Not Found")
        if self.config.get("simulated_denied"):
            return _error(403, "forbidden", "Not Authorized to access this resource/api")
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        start, end = _time(query.get("startTime", "")), _time(query.get("endTime", ""))
        if query.get("eventName") != "authorize" or start is None or end is None or start >= end:
            return _error(400, "invalid", "Bad request.")
        rows = activities(self.config, start, end)
        size = min(int(query.get("maxResults") or 1000), int(self.config.get("simulated_page") or 1000))
        offset = int(query.get("pageToken") or 0)
        out: dict[str, Any] = {
            "kind": "reports#activities",
            "etag": '"simulated"',
            "items": rows[offset : offset + size],
        }
        if offset + size < len(rows):
            out["nextPageToken"] = str(offset + size)
        return _json(200, out)
