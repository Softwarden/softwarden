"""Synthetic Jira, ServiceNow and HaloITSM ticket APIs (tickets/clients.py), selected by the reserved addresses
https://jira.simulated.invalid, https://servicenow.simulated.invalid and https://halo.simulated.invalid (a reserved
name that never resolves), the way core/halo.py and the Graph simulator work. Nothing leaves the machine.

Credentials: any user name with any secret, except the secret `wrong-secret` (401). Halo tokens follow
core/halo.SimulatedTransport (a scope containing `bad` is refused).

Tickets (every status mapping has one):

- Jira: ITOPS-1 To Do (new), ITOPS-2 In Progress (indeterminate), ITOPS-3 Done (done), ITOPS-4 Resolved (done),
  ITOPS-5 a status with an unknown category.
- ServiceNow: INC0000001 New, INC0000002 In Progress, INC0000003 On Hold, INC0000006 Resolved, INC0000007 Closed,
  INC0000008 Canceled, INC0000062 state value 6 without a display value, CHG0000001 Scheduled, RITM0000001 Closed
  Complete, INC0000099 a state label nobody maps.
- Halo: 1 status 1 (New), 2 status 2, 9 status 9 (Closed), 5 status name "Resolved".

Failures by key, on every system (the number after the prefix, or the Halo ID): 404 not found, 403 forbidden, 500
server error, 429 throttled (each answer's body repeats the Authorization header, so a test can prove it is never
kept), 408 a timeout (core.graph.Unreachable as core/outbound raises it), 422 a body that isn't JSON.

Creates: Jira answers `ITOPS-100`, ServiceNow `INC0001001`, Halo ticket 5001. A Jira project key `NOPE`, a ServiceNow
table `nope` or a Halo ticket type 404 answer 404. `calls` records (method, url, body) for every request."""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

from core import halo
from core.graph import Reply, Unreachable

JIRA_HOST = "jira.simulated.invalid"
SERVICENOW_HOST = "servicenow.simulated.invalid"
HALO_HOST = halo.SIMULATED_HOST
JIRA_URL = f"https://{JIRA_HOST}"
SERVICENOW_URL = f"https://{SERVICENOW_HOST}"
HALO_URL = halo.SIMULATED_URL
WRONG_SECRET = "wrong-secret"  # noqa: S105 (lets a test see refused credentials)

JIRA_ISSUES = {
    "ITOPS-1": ("Renew Adobe licences", "To Do", "new"),
    "ITOPS-2": ("Assess Salesforce change", "In Progress", "indeterminate"),
    "ITOPS-3": ("Retire Visio 2016", "Done", "done"),
    "ITOPS-4": ("Payroll upgrade", "Resolved", "done"),
    "ITOPS-5": ("Odd workflow", "Parked", "undefined"),
}
SERVICENOW_TICKETS: dict[str, tuple[str, Any]] = {
    "INC0000001": ("Laptop rollout", "New"),
    "INC0000002": ("Renewal quote", "In Progress"),
    "INC0000003": ("Vendor call", "On Hold"),
    "INC0000006": ("Licence true-up", "Resolved"),
    "INC0000007": ("Old request", "Closed"),
    "INC0000008": ("Withdrawn", "Canceled"),
    "INC0000062": ("Raw state", {"value": "6", "display_value": ""}),
    "CHG0000001": ("Upgrade window", "Scheduled"),
    "RITM0000001": ("New seats", "Closed Complete"),
    "INC0000099": ("Custom state", "Waiting for Godot"),
}
HALO_TICKETS: dict[int, dict[str, Any]] = {
    1: {"id": 1, "summary": "Renew Halo-tracked contract", "status_id": 1},
    2: {"id": 2, "summary": "Change assessment follow-up", "status_id": 2},
    9: {"id": 9, "summary": "Closed ticket", "status_id": 9},
    5: {"id": 5, "summary": "Named status", "status_id": 21, "status_name": "Resolved"},
}
FAILURES = {404, 403, 500, 429, 408, 422}


def _json(status: int, body: Any) -> Reply:
    return Reply(status, {"content-type": "application/json"}, json.dumps(body).encode())


def _failure(code: int, headers: Mapping[str, str]) -> Reply | None:
    if code not in FAILURES:
        return None
    if code == 408:
        raise Unreachable("TimeoutError", ambiguous=True)
    if code == 422:
        return Reply(200, {"content-type": "text/html"}, b"<html>not json</html>")
    echo = {"error": f"failure {code}", "seen_authorization": headers.get("Authorization", "")}
    return _json(code, echo)


def _number(text: str) -> int:
    digits = re.sub(r"^[A-Z]+-?", "", text)
    return int(digits) if digits.isdigit() else -1


class Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.tokens = halo.SimulatedTransport()

    # The Halo token endpoint (core/halo.request_token).
    def post_form(self, url: str, form: Mapping[str, str]) -> Reply:
        self.calls.append(("POST", url, None))
        return self.tokens.post_form(url, form)

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Reply:
        payload = json.loads(body.decode()) if body else None
        self.calls.append((method, url, payload))
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if host == JIRA_HOST:
            return self._jira(method, parts, headers, payload)
        if host == SERVICENOW_HOST:
            return self._servicenow(method, parts, headers, payload)
        if host == HALO_HOST:
            return self._halo(method, parts, headers, payload)
        return Reply(404, {}, b"")

    @staticmethod
    def _basic_ok(headers: Mapping[str, str]) -> bool:
        import base64

        value = headers.get("Authorization", "")
        if not value.startswith("Basic "):
            return False
        user, _, secret = base64.b64decode(value[6:]).decode().partition(":")
        return bool(user) and bool(secret) and secret != WRONG_SECRET

    def _jira(self, method: str, parts, headers: Mapping[str, str], payload: Any) -> Reply:
        if not self._basic_ok(headers):
            return _json(401, {"errorMessages": ["Unauthorized"], "seen": headers.get("Authorization", "")})
        path = urllib.parse.unquote(parts.path)
        if method == "GET" and path == "/rest/api/3/myself":
            return _json(200, {"accountId": "sim-1", "emailAddress": "it@example.test"})
        if method == "POST" and path == "/rest/api/3/issue":
            fields = (payload or {}).get("fields", {})
            if fields.get("project", {}).get("key") == "NOPE":
                return _json(404, {"errorMessages": ["No project"]})
            return _json(201, {"id": "10100", "key": "ITOPS-100", "self": f"{JIRA_URL}/rest/api/3/issue/10100"})
        match = re.fullmatch(r"/rest/api/3/issue/([A-Z][A-Z0-9_]*-\d+)", path)
        if method != "GET" or match is None:
            return Reply(404, {}, b"")
        key = match.group(1)
        failed = _failure(_number(key), headers)
        if failed is not None:
            return failed
        if key not in JIRA_ISSUES:
            return _json(404, {"errorMessages": ["Issue does not exist"]})
        summary, name, category = JIRA_ISSUES[key]
        status = {"name": name, "statusCategory": {"key": category, "name": category.title()}}
        return _json(200, {"key": key, "fields": {"summary": summary, "status": status}})

    def _servicenow(self, method: str, parts, headers: Mapping[str, str], payload: Any) -> Reply:
        if not self._basic_ok(headers):
            return _json(401, {"error": {"message": "User Not Authenticated", "seen": headers.get("Authorization")}})
        match = re.fullmatch(r"/api/now/table/([a-z_]+)", parts.path)
        if match is None:
            return Reply(404, {}, b"")
        table = match.group(1)
        query = urllib.parse.parse_qs(parts.query)
        if method == "POST":
            if table == "nope":
                return _json(404, {"error": {"message": "Invalid table"}})
            row = {
                "number": {"value": "INC0001001", "display_value": "INC0001001"},
                "sys_id": {"value": "a1b2c3d4e5f60718293a4b5c6d7e8f90", "display_value": "a1b2c3"},
                "state": {"value": "1", "display_value": "New"},
                "short_description": (payload or {}).get("short_description", ""),
            }
            return _json(201, {"result": row})
        if table == "task" and "sysparm_query" not in query:
            return _json(200, {"result": [{"number": "TASK0000001"}]})
        number = query.get("sysparm_query", [""])[0].removeprefix("number=")
        failed = _failure(_number(number), headers)
        if failed is not None:
            return failed
        found = SERVICENOW_TICKETS.get(number)
        if found is None:
            return _json(200, {"result": []})
        summary, state = found
        if isinstance(state, str):
            state = {"value": "x", "display_value": state}
        return _json(200, {"result": [{"number": number, "state": state, "short_description": summary}]})

    def _halo(self, method: str, parts, headers: Mapping[str, str], payload: Any) -> Reply:
        if headers.get("Authorization") != f"Bearer {halo.TOKEN}":
            return _json(401, {"error": "unauthorized"})
        path = parts.path
        if method == "POST" and path.lower() == "/api/tickets":
            ticket = (payload or [{}])[0]
            if ticket.get("tickettype_id") == 404:
                return _json(404, {"error": "no such ticket type"})
            return _json(201, {"id": 5001, "summary": ticket.get("summary", ""), "status_id": 1})
        match = re.fullmatch(r"/api/[Tt]ickets/(\d+)", path)
        if method != "GET" or match is None:
            return Reply(404, {}, b"")
        ticket_id = int(match.group(1))
        failed = _failure(ticket_id, headers)
        if failed is not None:
            return failed
        if ticket_id not in HALO_TICKETS:
            return _json(404, {"error": "not found"})
        return _json(200, HALO_TICKETS[ticket_id])
