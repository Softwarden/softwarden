"""A fake Softwarden API for the tests: canned composite and list answers shaped like the real ones (the shapes are
checked against the real API in apps/api/tests/test_mcp_e2e.py), the API's token rules (unknown token 401, read-only
token 403 on writes) and its If-Match rule (412 with the current revision). It serves both as an httpx2
MockTransport handler (unit tests) and as a real HTTP server on a loopback port (the stdio and HTTP transport tests,
where the MCP server is a separate process or a uvicorn server)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx2
import pytest

from softwarden_mcp.client import SoftwardenClient

READ_ONLY = "sw_" + "r" * 40
WRITER = "sw_" + "w" * 40
BAD = "sw_" + "b" * 40
APP_ID = "3f2a1b0c-9d8e-4f7a-8b6c-5d4e3f2a1b0c"
REMINDER_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
CANDIDATE_ID = "1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e"
GPT_ID = "7d6c5b4a-3e2f-4a1b-9c8d-7e6f5a4b3c2d"
OPENAI_ID = "6c5b4a3d-2e1f-4a0b-8c9d-6e5f4a3b2c1d"
REVISION = "rev1"

PRIYA = {"id": "p-1", "name": "Priya Shah", "email": "priya@example.com", "active": True}
DANA = {"id": "p-2", "name": "Dana Ortiz", "email": None, "active": True}
GONE = {"id": "p-3", "name": "Former person", "email": None, "active": False}


def owners(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "renewal_owner": None,
        "contract_owner": None,
        "follow_up": None,
        "business_owners": [],
        "technical_owners": [],
        "it_stewards": [],
        "support_teams": [],
    }
    return {**base, **kw}


def renewal_row(name: str, term_ends: str | None, date: str, days: int, **kw: Any) -> dict[str, Any]:
    row = {
        "source": "contract",
        "id": f"c-{name}",
        "name": name,
        "path": f"contracts/c-{name}",
        "date": date,
        "days": days,
        "overdue": days < 0,
        "action": "cancel_by",
        "label": "Calculated",
        "explanation": None,
        "term_ends": term_ends,
        "auto_renew": "yes",
        "decision": None,
        "vendor_name": "Atlassian",
        "contract": {"id": f"c-{name}", "name": name},
        "annualized_cost": "38000.00",
        "currency": "USD",
        "vendor": {"id": "v-1", "name": "Atlassian", "account_manager": DANA, "support_email": None},
        "owners": owners(),
        "records": [],
        "records_total": 0,
        "gaps": [],
    }
    return {**row, **kw}


AS_OF = date(2026, 11, 7)
RENEWAL_ROWS = [
    renewal_row(
        "Atlassian Cloud",
        "2027-01-06",
        "2026-12-07",
        30,
        owners=owners(renewal_owner=PRIYA, contract_owner=PRIYA, business_owners=[DANA]),
        records=[{"id": APP_ID, "kind": "application", "name": "Jira"}],
        records_total=1,
        gaps=["no_it_steward"],
    ),
    renewal_row(
        "Long notice",
        "2027-03-07",
        "2027-01-06",
        60,
        owners=owners(contract_owner=GONE),
        gaps=["renewal_owner_inactive"],
    ),  # fmt: skip
    renewal_row(
        "Domain",
        None,
        "2026-11-01",
        -6,
        source="renewal",
        path="renewals/r-1",
        action="renew_by",
        vendor=None,
        vendor_name=None,
        contract=None,
        gaps=["no_renewal_owner", "no_vendor"],
    ),
    # A term that already ended while its action is still open.
    renewal_row("Lapsed", "2026-11-01", "2026-10-21", -17, owners=owners(contract_owner=DANA)),
    # A renewal whose entered deadline is after its renewal date (renewal validation allows it).
    renewal_row(
        "Entered late",
        "2026-12-07",
        "2027-05-26",
        200,
        source="renewal",
        path="renewals/r-2",
        contract=None,
        owners=owners(renewal_owner=DANA, follow_up={"id": 7, "name": "Sam Lee", "email": "sam@example.com"}),
    ),
]


def renewals_answer(params: dict[str, str]) -> dict[str, Any]:
    """GET composite/renewals as the API answers it: `basis` action_deadline (default; `date` within the window,
    overdue included, soonest first) or term_end (`term_ends` within the window or past, soonest term end first),
    capped by `limit` with the full `count`."""
    days, limit = int(params.get("within_days", "90")), int(params.get("limit", "50"))
    basis = params.get("basis", "action_deadline")
    until = (AS_OF + timedelta(days=days)).isoformat()
    if basis == "term_end":
        rows = sorted(
            (r for r in RENEWAL_ROWS if r["term_ends"] and r["term_ends"] <= until),
            key=lambda r: (r["term_ends"], r["name"].casefold()),
        )
    else:
        rows = sorted((r for r in RENEWAL_ROWS if r["date"] <= until), key=lambda r: (r["date"], r["name"].casefold()))
    return {
        "as_of": AS_OF.isoformat(),
        "within_days": days,
        "basis": basis,
        "count": len(rows),
        "truncated": len(rows) > limit,
        "results": rows[:limit],
    }


SEARCH = {
    "query": "jira",
    "ignored": [],
    "fuzzy": False,
    "count": 1,
    "by_kind": {"application": 1},
    "truncated": False,
    "results": [
        {
            "kind": "application",
            "id": APP_ID,
            "name": "Jira",
            "path": f"applications/{APP_ID}",
            "lifecycle": "active",
            "archived": False,
            "snippet": {"field": "description", "label": "Description", "text": "Issue tracking"},
            "parent": None,
        }
    ],
}

NODE = {
    "id": "n-1",
    "kind": "application",
    "name": "Okta",
    "path": "applications/n-1",
    "lifecycle": "active",
    "archived": False,
    "hops": 1,
    "via": "Depends on",
    "through": [],
    "cycle": False,
    "link_state": "confirmed",
    "technical_owner": None,
}
DEPENDENCIES = {
    "target": {"id": APP_ID, "kind": "application", "name": "Jira", "technical_owner": None},
    "direction": "both",
    "depth": "2",
    "include_retired": False,
    "count": 2,
    "truncated": False,
    "summary": {"nodes": 2, "data_flows": 1, "cycles": 0, "hidden_retired": 0},
    "depends_on": [NODE],
    "dependents": [{**NODE, "id": "n-2", "name": "Triage bot", "kind": "ai_agent", "hops": 2, "through": ["CRM"]}],
    "data_flows": [
        {
            "id": "f-1",
            "name": "Jira to Slack",
            "path": "integrations/f-1",
            "lifecycle": "active",
            "archived": False,
            "source": {"id": APP_ID, "kind": "application", "name": "Jira"},
            "target": {"id": "s", "kind": "application", "name": "Slack"},
            "protocol": "REST",
            "link_state": "confirmed",
            "technical_owner": None,
        }
    ],
}

SPEND = {
    "as_of": "2026-11-07",
    "by": "vendor",
    "totals": [{"currency": "USD", "annualized": "1000.00", "run_rate": "1000.00", "contracts": 2}],
    "groups": [
        {"key": "v-1", "label": "Microsoft", "gap": None, "currency": "USD", "amount": "800.00", "contracts": 1},
        {"key": None, "label": None, "gap": "no_vendor", "currency": "USD", "amount": "200.00", "contracts": 1},
    ],
    "truncated": False,
}

CREDENTIALS = {
    "as_of": "2026-11-07",
    "within_days": 30,
    "count": 1,
    "truncated": False,
    "results": [
        {
            "kind": "discovered_credential",
            "id": "k-1",
            "name": "Graph sync secret",
            "prefix": None,
            "credential_type": "secret",
            "key_id": "a1b2",
            "expires_on": "2026-11-20",
            "days": 13,
            "expired": False,
            "owner": None,
            "application": {"id": "e-1", "name": "Salesforce SSO"},
            "record": {"id": APP_ID, "kind": "application", "name": "Salesforce", "technical_owner": PRIYA},
        }
    ],
}

DATA_GAPS = {
    "as_of": "2026-11-07",
    "score": 82,
    "records": 120,
    "complete": 64,
    "by_rule": [
        {"id": "business_owner", "label": "No business owner", "severity": "high", "records": 120, "failing": 9}
    ],
    "rule": None,
    "kind": None,
    "count": 9,
    "truncated": True,
    "results": [
        {
            "id": APP_ID,
            "kind": "application",
            "name": "Jira",
            "score": 75,
            "failing": [{"id": "business_owner", "label": "No business owner", "severity": "high"}],
            "business_owner": None,
            "it_steward": {"id": 7, "name": "Sam Lee"},
        }
    ],
}

DISCOVERY = {
    "as_of": "2026-11-07",
    "counts": {"candidates": 3, "new": 2, "changed": 1, "missing": 1, "differs": 2},
    "connectors_total": 1,
    "truncated": False,
    "connectors": [
        {
            "id": "k",
            "name": "Entra",
            "source": "entra",
            "enabled": True,
            "counts": {"candidates": 3},
            "last_run": {"status": "succeeded", "finished_at": "2026-11-07T08:00:00Z", "error": None},
            "active_run": None,
        }
    ],
}

GPT = {"id": GPT_ID, "name": "gpt-4o", "version": "2024-08", "provider": {"id": OPENAI_ID, "name": "OpenAI"}}
AGENT = {
    "id": "a-1",
    "name": "Triage bot",
    "lifecycle": "active",
    "platform": "copilot_studio",
    "models_used": [GPT],
    "risk_classification": "limited",
    "business_owner": {"id": "p-2", "name": "Dana Ortiz"},
    "technical_owner": None,
    "gaps": ["No technical owner"],
}

APPLICATION = {
    "id": APP_ID,
    "kind": "application",
    "name": "Jira",
    "description": "Issue tracking",
    "lifecycle": "active",
    "criticality": None,
    "business_owner": {"id": "p-2", "name": "Dana Ortiz"},
    "technical_owner": None,
    "tags": [],
    "gaps": ["No technical owner"],
}
REMINDER = {
    "id": REMINDER_ID,
    "title": "Atlassian Cloud",
    "label": "Cancellation deadline",
    "due_date": "2026-12-07",
    "status": "open",
    "snoozed_until": None,
    "source_type": "contract",
    "steward": {"id": 1, "name": "Sam Lee"},
}
CANDIDATE = {
    "id": CANDIDATE_ID,
    "name": "Zoom",
    "kind": "enterprise_app",
    "source": "entra",
    "connector": {"id": "k", "name": "Entra"},
    "status": "new",
    "ai_agent": False,
    "first_seen_at": "2026-11-01T08:00:00Z",
    "suggested_matches": [{"id": APP_ID, "name": "Zoom Workplace", "score": 0.8}],
}

GETS: dict[str, Any] = {
    "auth/me": {"user": {"id": 2, "name": "Viewer"}, "role": "viewer", "organization": {"id": "o", "name": "Org A"}},
    "composite/renewals": renewals_answer,
    "composite/search": SEARCH,
    "composite/dependencies": DEPENDENCIES,
    "composite/spend": SPEND,
    "composite/expiring-credentials": CREDENTIALS,
    "composite/data-gaps": DATA_GAPS,
    "composite/discovery-summary": DISCOVERY,
    "ai-models/in-use": [{**GPT, "modality": "text", "hosting_region": "", "agents": 3, "live_agents": 2}],
    "ai-models": [GPT],
    "ai-providers": [{"id": OPENAI_ID, "name": "OpenAI", "website": "", "usage": 1}],
    "reminders": {"results": [REMINDER], "next": None, "previous": None},
    "discovery/candidates": {"status": "candidates", "counts": {"new": 1}, "results": [CANDIDATE], "next": None},
    f"applications/{APP_ID}": APPLICATION,
    f"reminders/{REMINDER_ID}": REMINDER,
    f"discovery/observations/{CANDIDATE_ID}": CANDIDATE,
}
WRITES: dict[tuple[str, str], Any] = {
    ("PATCH", f"applications/{APP_ID}"): lambda body: {**APPLICATION, **body},
    ("POST", f"reminders/{REMINDER_ID}/snooze"): lambda body: {
        **REMINDER,
        "status": "snoozed",
        "snoozed_until": body["until"],
    },
    ("POST", f"reminders/{REMINDER_ID}/acknowledge"): lambda body: {**REMINDER, "status": "acknowledged"},
    ("POST", f"discovery/observations/{CANDIDATE_ID}/dismiss"): lambda body: {**CANDIDATE, "status": "dismissed"},
}


def problem(status: int, slug: str, detail: str, **extra: Any) -> tuple[int, dict[str, str], Any]:
    body = {"type": f"/problems/{slug}", "title": slug, "status": status, "detail": detail, **extra}
    return status, {"Content-Type": "application/problem+json"}, body


@dataclass
class Seen:
    method: str
    path: str
    params: dict[str, str]
    headers: dict[str, str]
    body: Any


@dataclass
class FakeSoftwarden:
    seen: list[Seen] = field(default_factory=list)
    gets: dict[str, Any] = field(default_factory=lambda: dict(GETS))
    revision: str = REVISION

    def handle(
        self, method: str, path: str, params: dict[str, str], headers: dict[str, str], raw: bytes
    ) -> tuple[int, dict[str, str], Any]:
        body = json.loads(raw) if raw else None
        path = path.removeprefix("/api/v1/").strip("/")
        self.seen.append(Seen(method, path, params, headers, body))
        token = headers.get("authorization", "").removeprefix("Bearer ")
        if token not in (READ_ONLY, WRITER):
            return problem(401, "invalid-token", "This API token isn't recognized.")
        if method == "GET":
            if path not in self.gets:
                return problem(404, "not-found", "Not found.")
            found = self.gets[path]
            if callable(found):  # computed from the query, as the real route does
                found = found(params)
            if isinstance(found, tuple):  # (status, headers, body): a canned error
                return found
            extra = {"ETag": f'"{self.revision}"'} if path in DETAILS else {}
            return 200, extra, found
        if token == READ_ONLY:
            return problem(403, "read-only-token", "This token is read-only.")
        write = WRITES.get((method, path))
        if write is None:
            return problem(404, "not-found", "Not found.")
        if headers.get("if-match") != f'"{self.revision}"':
            status, h, b = problem(412, "precondition-failed", "Changed since.", revision="rev2")
            return status, {**h, "ETag": '"rev2"'}, b
        return 200, {"ETag": '"rev3"'}, write(body)

    # -- as an httpx2 MockTransport handler

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        params = dict(request.url.params)
        headers = {k.lower(): v for k, v in request.headers.items()}
        status, extra, body = self.handle(request.method, request.url.path, params, headers, request.content)
        return httpx2.Response(status, json=body, headers=extra)


DETAILS = {f"applications/{APP_ID}", f"reminders/{REMINDER_ID}", f"discovery/observations/{CANDIDATE_ID}"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fake() -> FakeSoftwarden:
    return FakeSoftwarden()


def make_client(fake: FakeSoftwarden, token: str = READ_ONLY) -> SoftwardenClient:
    return SoftwardenClient("https://softwarden.test", token, transport=httpx2.MockTransport(fake))


@pytest.fixture
def client(fake: FakeSoftwarden) -> Iterator[SoftwardenClient]:
    made = make_client(fake)
    yield made
    made.close()


@pytest.fixture
def writer(fake: FakeSoftwarden) -> Iterator[SoftwardenClient]:
    made = make_client(fake, WRITER)
    yield made
    made.close()


@pytest.fixture
def fake_server(fake: FakeSoftwarden) -> Iterator[tuple[str, FakeSoftwarden]]:
    """The fake on a real loopback HTTP port, for an MCP server in another process or thread. Stopped after."""

    class Handler(BaseHTTPRequestHandler):
        def _any(self) -> None:
            url = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, extra, body = fake.handle(self.command, url.path, dict(parse_qsl(url.query)), headers, raw)
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", extra.pop("Content-Type", "application/json"))
            for k, v in extra.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PATCH = _any

        def log_message(self, format: str, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", fake
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
