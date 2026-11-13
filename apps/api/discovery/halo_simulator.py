"""A synthetic Halo instance for the halo source (discovery/halo.py): the token endpoint (core/halo.SimulatedTransport's
rules) and `GET /api/Asset?paginate=true&page_size=N&page_no=N`, answering `{"page_no", "page_size", "record_count",
"assets": [...]}`. Selected by the connection address https://halo.simulated.invalid; nothing leaves
the machine. It implements both halo.Transport (`post_form`, the token) and core/graph.Transport (`send`, the reads).

Seven assets: five software or application assets (one inactive), a server and a laptop.

Config keys (tests only):

- `simulator_changes`: edits in order, {"op", "id" (asset name), "value"}: rename (the asset keeps its ID), remove,
  inactive (value true or false), add (value: the asset type).
- `simulated_size`: that many generated software assets more (paging).
- `simulated_ignore_page_no`: every page is the first page (an instance that ignores paging).
- `simulated_no_id`: one more asset without an ID.
- `simulated_denied`: the API application lacks read:assets: reads answer 403.
- `simulated_token_echo`: the token endpoint refuses with a message that repeats the client secret.
- `simulated_errors`: {"path": status}, the answer echoing the bearer token; `simulated_throttle`: {"path", "times",
  "retry_after"}.
- `simulated_empty`: the asset listing answers no assets at all (record_count 0)."""

from __future__ import annotations

import copy
import json
import urllib.parse
from collections.abc import Mapping
from typing import Any

from core import halo
from core.graph import Reply

BASE: list[dict[str, Any]] = [
    {"id": 101, "key_field": "Adobe Acrobat Pro", "assettype_name": "Software", "inactive": False},
    {"id": 102, "key_field": "Salesforce", "assettype_name": "Software", "inactive": False},
    {"id": 103, "key_field": "Payroll Server 01", "assettype_name": "Server", "inactive": False},
    {"id": 104, "key_field": "Visio 2016", "assettype_name": "Software", "inactive": True},
    {"id": 105, "key_field": "Jira Cloud", "assettype_name": "Application", "inactive": False},
    {"id": 106, "key_field": "", "inventory_number": "LAPTOP-0042", "assettype_name": "Laptop", "inactive": False},
    {"id": 107, "key_field": "ServiceNow", "assettype_name": "Software", "inactive": False},
]


def assets(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = copy.deepcopy(BASE)
    for i in range(int(config.get("simulated_size") or 0)):
        rows.append({"id": 1000 + i, "key_field": f"Licensed tool {i + 1:04d}", "assettype_name": "Software"})
    for change in config.get("simulator_changes") or []:
        op, name, value = change.get("op"), change.get("id"), change.get("value")
        target = next((r for r in rows if (r.get("key_field") or r.get("inventory_number")) == name), None)
        if op == "add":
            rows.append(
                {"id": max(r["id"] for r in rows) + 1, "key_field": name, "assettype_name": value or "Software"}
            )
        elif target is None:
            continue
        elif op == "rename":
            target["key_field"] = value
        elif op == "remove":
            rows.remove(target)
        elif op == "inactive":
            target["inactive"] = bool(value)
    if config.get("simulated_no_id"):
        rows.append({"id": None, "key_field": "Mystery asset", "assettype_name": "Software"})
    for row in rows:
        row.setdefault("inventory_number", f"INV-{row['id']}")
        row.setdefault("inactive", False)
        row |= {"client_name": "Contoso", "site_name": "Head office", "assettype_id": 1}
    return rows


def _json(status: int, body: Any, headers: Mapping[str, str] | None = None) -> Reply:
    return Reply(status, {"content-type": "application/json", **(headers or {})}, json.dumps(body).encode())


class Transport:
    """The synthetic Halo. `calls` records (method, url); `sent` (host, Authorization) for every read."""

    def __init__(self, connector=None, config=None):
        self.config = dict(config if config is not None else getattr(connector, "config", {}) or {})
        self.tokens = halo.SimulatedTransport(echo=bool(self.config.get("simulated_token_echo")))
        self.calls: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.throttled = 0

    def post_form(self, url: str, form: Mapping[str, str]) -> Reply:
        self.calls.append(("POST", url))
        return self.tokens.post_form(url, form)

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Reply:
        parts = urllib.parse.urlsplit(url)
        self.calls.append((method, url))
        bearer = headers.get("Authorization", "")
        self.sent.append((parts.hostname or "", bearer))
        if method != "GET" or parts.hostname != halo.SIMULATED_HOST:
            return _json(400, {"error": "bad_request"})
        if bearer != f"Bearer {halo.TOKEN}":
            return _json(401, {"error": "invalid_token"})
        errors = self.config.get("simulated_errors") or {}
        if parts.path in errors:
            return _json(int(errors[parts.path]), {"error": "bad_request", "message": f"Bad request with {bearer}"})
        throttle = self.config.get("simulated_throttle") or {}
        if throttle.get("path") == parts.path and self.throttled < int(throttle.get("times", 1)):
            self.throttled += 1
            retry = throttle.get("retry_after")
            return _json(429, {"error": "too_many_requests"}, {"retry-after": str(retry)} if retry else {})
        if parts.path != halo.ASSETS_PATH:
            return _json(404, {"error": "not_found"})
        if self.config.get("simulated_denied"):
            return _json(403, {"error": "insufficient_scope"})
        query = urllib.parse.parse_qs(parts.query)
        size = int((query.get("page_size") or ["50"])[0])
        page = int((query.get("page_no") or ["1"])[0])
        rows = [] if self.config.get("simulated_empty") else assets(self.config)
        if (query.get("paginate") or [""])[0] != "true":
            return _json(200, {"record_count": len(rows), "assets": rows})
        if self.config.get("simulated_ignore_page_no"):  # an instance that ignores page_no
            page = 1
        chunk = rows[(page - 1) * size : page * size]
        return _json(200, {"page_no": page, "page_size": size, "record_count": len(rows), "assets": chunk})
