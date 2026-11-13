"""A synthetic Okta org for the okta source (discovery/okta.py): the token endpoint (core/okta.SimulatedTokens) and
`GET /api/v1/apps` and `GET /api/v1/apps/{id}/users`, answering JSON arrays with Okta's `link` header (rel="self",
rel="next" with an `after` cursor) and `X-Rate-Limit-*` headers. Selected by the org address
https://okta.simulated.invalid; nothing leaves the machine. Implements core/graph.Transport (`send`).

Nine apps: Salesforce, Slack, Zoom and Atlassian Cloud (SAML or OIDC, active, with users), Okta's own Admin Console
and Dashboard, an inactive Legacy Wiki (SWA), an Expenses Portal bookmark and Workday (provisioned).

Config keys (tests only):

- `simulator_changes`: edits in order, {"op", "id" (app label), "value"}: rename (the app keeps its ID), remove,
  inactive (value true or false), add (value: the sign-on mode).
- `simulated_size`: that many generated apps more (paging); `simulated_page`: apps per page at most.
- `simulated_users`: {label: count} replacing an app's assigned users.
- `simulated_empty`: the listing is empty. `simulated_foreign_next`: next links point at another host.
- `simulated_repeat_next`: the next link is the page itself.
- `simulated_denied`: the app's admin role can't read apps (403 E0000006); `simulated_users_denied`: nor users.
- `simulated_rate_remaining`: X-Rate-Limit-Remaining is 0 from this request number on (then Reset is 30 s away).
- `simulated_rate_left`: X-Rate-Limit-Remaining is always this (of a limit of 100; Reset 60 s away unless it is 0).
- `simulated_token_echo`: the token endpoint's refusal repeats the client assertion.
- `simulated_errors`: {"path": status}, the answer echoing the bearer token; `simulated_throttle`: {"path", "times",
  "retry_after"}: 429 with Retry-After when given, else with X-Rate-Limit-Reset 7 seconds away."""

from __future__ import annotations

import copy
import json
import urllib.parse
from collections.abc import Mapping
from typing import Any

from core import okta
from core.graph import Reply

from . import rest_source

HOST = okta.SIMULATED_HOST
BASE: list[dict[str, Any]] = [
    {"id": "0oa1salesforce", "name": "salesforce", "label": "Salesforce", "signOnMode": "SAML_2_0", "users": 42},
    {"id": "0oa2slack", "name": "slack", "label": "Slack", "signOnMode": "SAML_2_0", "users": 120},
    {"id": "0oa3zoom", "name": "zoomus", "label": "Zoom", "signOnMode": "OPENID_CONNECT", "users": 300},
    {"id": "0oa4admin", "name": "saasure", "label": "Okta Admin Console", "signOnMode": "OPENID_CONNECT", "users": 3},
    {"id": "0oa5dash", "name": "okta_enduser", "label": "Okta Dashboard", "signOnMode": "OPENID_CONNECT", "users": 9},
    {"id": "0oa6wiki", "name": "template_swa", "label": "Legacy Wiki", "signOnMode": "AUTO_LOGIN", "users": 2},
    {"id": "0oa7expenses", "name": "bookmark", "label": "Expenses Portal", "signOnMode": "BOOKMARK", "users": 10},
    {"id": "0oa8atlassian", "name": "atlassian", "label": "Atlassian Cloud", "signOnMode": "SAML_2_0", "users": 75},
    {"id": "0oa9workday", "name": "workday", "label": "Workday", "signOnMode": "SAML_2_0", "users": 640},
]
INACTIVE = {"0oa6wiki"}
PROVISIONED = {"0oa1salesforce": ["PUSH_NEW_USERS", "PUSH_USER_DEACTIVATION"], "0oa9workday": ["PUSH_NEW_USERS"]}
RATE_LIMIT = 100


def apps(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    if config.get("simulated_empty"):
        return []
    rows = copy.deepcopy(BASE)
    for i in range(int(config.get("simulated_size") or 0)):
        rows.append({"id": f"0oagen{i:05d}", "name": "template_saml_2_0", "label": f"Generated app {i + 1:04d}"})
        rows[-1] |= {"signOnMode": "SAML_2_0", "users": 1}
    for change in config.get("simulator_changes") or []:
        op, label, value = change.get("op"), change.get("id"), change.get("value")
        target = next((r for r in rows if r["label"] == label), None)
        if op == "add":
            rows.append({"id": f"0oaadd{len(rows):04d}", "name": "template", "label": label})
            rows[-1] |= {"signOnMode": value or "SAML_2_0", "users": 5}
        elif target is None:
            continue
        elif op == "rename":
            target["label"] = value
        elif op == "remove":
            rows.remove(target)
        elif op == "inactive":
            target["status"] = "INACTIVE" if value else "ACTIVE"
    counts = config.get("simulated_users") or {}
    for row in rows:
        row.setdefault("status", "INACTIVE" if row["id"] in INACTIVE else "ACTIVE")
        row["features"] = PROVISIONED.get(row["id"], [])
        row["created"] = "2024-03-01T09:00:00.000Z"
        row["lastUpdated"] = "2026-10-01T09:00:00.000Z"
        if row["label"] in counts:
            row["users"] = int(counts[row["label"]])
        row["_links"] = {"appLinks": [{"name": row["name"], "href": f"https://{HOST}/home/{row['name']}/{row['id']}"}]}
    return rows


def _json(status: int, body: Any, headers: Mapping[str, str] | None = None) -> Reply:
    return Reply(status, {"content-type": "application/json", **(headers or {})}, json.dumps(body).encode())


def _error(status: int, code: str, summary: str, headers: Mapping[str, str] | None = None) -> Reply:
    return _json(status, {"errorCode": code, "errorSummary": summary, "errorId": "oaeSimulated"}, headers)


class Transport:
    """The synthetic Okta. `calls` records (method, url); `sent` (host, Authorization) for every read."""

    def __init__(self, connector=None, config=None):
        self.config = dict(config if config is not None else getattr(connector, "config", {}) or {})
        self.tokens = okta.SimulatedTokens(echo=bool(self.config.get("simulated_token_echo")))
        self.calls: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.throttled = 0
        self.reads = 0

    def _limits(self) -> dict[str, str]:
        self.reads += 1
        zero_from = self.config.get("simulated_rate_remaining")
        remaining = 0 if zero_from and self.reads >= int(zero_from) else max(RATE_LIMIT - self.reads, 1)
        if self.config.get("simulated_rate_left") is not None:
            remaining = int(self.config["simulated_rate_left"])
        reset = int(rest_source.wall()) + (30 if remaining == 0 else 60)
        return {
            "x-rate-limit-limit": str(RATE_LIMIT),
            "x-rate-limit-remaining": str(remaining),
            "x-rate-limit-reset": str(reset),
        }

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Reply:
        parts = urllib.parse.urlsplit(url)
        self.calls.append((method, url))
        if method == "POST":
            return self.tokens.send(method, url, headers, body)
        bearer = headers.get("Authorization", "")
        self.sent.append((parts.hostname or "", bearer))
        if method != "GET" or parts.hostname != HOST or parts.scheme != "https":
            return _error(400, "E0000003", "The request body was not well-formed.")
        if bearer != f"Bearer {okta.TOKEN}":
            return _error(401, "E0000011", "Invalid token provided")
        path = parts.path
        errors = self.config.get("simulated_errors") or {}
        if path in errors:  # echoes the bearer token, which must never reach a message
            return _error(int(errors[path]), "E0000009", f"Internal Server Error with {bearer}")
        throttle = self.config.get("simulated_throttle") or {}
        if throttle.get("path") == path and self.throttled < int(throttle.get("times", 1)):
            self.throttled += 1
            retry = throttle.get("retry_after")
            extra = {"retry-after": str(retry)} if retry else {"x-rate-limit-reset": str(int(rest_source.wall()) + 7)}
            return _error(429, "E0000047", "API call exceeded rate limit due to too many requests.", extra)
        limits = self._limits()
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        rows = apps(self.config)
        if path == okta.APPS_PATH:
            if self.config.get("simulated_denied"):
                return _error(403, "E0000006", "You do not have permission to perform the requested action")
            visible = [{k: v for k, v in r.items() if k != "users"} for r in rows]
            if query.get("includeNonDeleted") != "true":
                visible = [r for r in visible if r["status"] == "ACTIVE"]
            size = min(int(query.get("limit") or 20), int(self.config.get("simulated_page") or 200))
            return self._page(path, query, visible, size, limits)
        prefix = f"{okta.APPS_PATH}/"
        if path.startswith(prefix) and path.endswith("/users"):
            if self.config.get("simulated_users_denied"):
                return _error(403, "E0000006", "You do not have permission to perform the requested action")
            app_id = path[len(prefix) : -len("/users")]
            app = next((r for r in rows if r["id"] == app_id), None)
            if app is None:
                return _error(404, "E0000007", f"Not found: Resource not found: {app_id} (AppInstance)")
            users = [
                {"id": f"00u{app_id[-4:]}{i:05d}", "scope": "GROUP" if i % 3 else "USER", "status": "ACTIVE"}
                for i in range(int(app.get("users") or 0))
            ]
            size = min(int(query.get("limit") or 50), 500)
            return self._page(path, query, users, size, limits)
        return _error(404, "E0000022", "The endpoint does not support the provided HTTP method")

    def _page(self, path, query, items, size, limits) -> Reply:
        start = int(query.get("after") or 0)
        chunk = items[start : start + size]
        base = {k: v for k, v in query.items() if k != "after"}
        links = [f'<https://{HOST}{path}?{urllib.parse.urlencode(base)}>; rel="self"']
        if start + size < len(items):
            host = "evil.example.com" if self.config.get("simulated_foreign_next") else HOST
            following = start if self.config.get("simulated_repeat_next") else start + size
            after = urllib.parse.urlencode({**base, "after": following}) if following else urllib.parse.urlencode(base)
            links.append(f'<https://{host}{path}?{after}>; rel="next"')
        return _json(200, chunk, {**limits, "link": ", ".join(links)})
