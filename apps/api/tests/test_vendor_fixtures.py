"""Vendor payload replays: responses in the shapes each vendor documents
(tests/fixtures/vendors, provenance in each folder's README) fed through the REAL clients and parsers. The fake
transport answers at the HTTP boundary only (status, headers, body bytes), never through a simulator, so a parser that
assumes a shape the vendor doesn't send fails here even when its simulator agrees with it.

Each test asserts what the parser made of the payload (names, IDs, counts, statuses, the pages followed), so renaming a
field the parser reads, in the fixture or in the parser, fails it. Where a documented shape is something the code
mishandles, the test either records today's behavior (said so in its docstring) or is a strict xfail naming the
discrepancy ("VF-n", listed in tests/fixtures/vendors/README.md). Nothing here reaches the network or the database."""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from core import connections, google_workspace, graph, oidc_discovery, okta, outbound
from discovery import azure, entra, entra_licenses, halo, powerplatform, rest_source
from discovery import google_workspace as gw_source
from discovery import okta as okta_source
from discovery.graph_source import GRAPH, Refused, SourceError
from tickets import clients as tickets
from tickets.models import TicketStatus, TicketSystem

FIXTURES = Path(__file__).parent / "fixtures" / "vendors"
TENANT = "00000000-0000-0000-0000-0000000000a0"  # the fixtures' synthetic tenant
MICROSOFT = connections.MicrosoftCredential(TENANT, "00000000-0000-0000-0000-000000007001", "replay-secret")
TOKEN = "replay-access-token"  # noqa: S105 (what the fake token endpoint issues)


# ---- the HTTP boundary -------------------------------------------------------------------------------------------


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def load(name: str) -> Any:
    return json.loads(fixture(name).decode("utf-8"))


def sidecar(name: str) -> tuple[int, dict[str, str]]:
    """(status, headers) of a `*.headers.json` sidecar, the headers as the real transports hand them over: names lower
    cased, and a header sent on several lines (a list in the sidecar) joined as core/outbound.merged_headers does."""
    data = load(name)
    status = int(str(data.pop("_status")).split()[1])
    pairs = [(k, v) for k, values in data.items() for v in (values if isinstance(values, list) else [values])]
    return status, outbound.merged_headers(pairs)


def reply(body: str | bytes | Any, status: int = 200, headers: dict[str, str] | None = None) -> graph.Reply:
    """A fixture file's bytes (a name ending .json), raw bytes, or a test-built object as JSON."""
    if isinstance(body, str) and body.endswith(".json"):
        raw = fixture(body)
    elif isinstance(body, bytes):
        raw = body
    else:
        raw = json.dumps(body).encode()
    return graph.Reply(status, headers or {"content-type": "application/json"}, raw)


def with_sidecar(body: Any, headers_name: str) -> graph.Reply:
    status, headers = sidecar(headers_name)
    return reply(body, status, headers)


# A terminal page the test adds where a documented page carries a next link but the vendor shows no next page: an
# empty collection, so the replay proves the link was followed exactly as given.
EMPTY_ODATA_PAGE: dict[str, list[Any]] = {"value": []}


class Replay:
    """core/graph.Transport (and Halo's post_form) answering exact URLs from `routes`. A list answers in turn (a 429,
    then the page). The Microsoft and Halo token endpoints issue TOKEN. An unexpected request fails the test."""

    def __init__(self, routes: dict[str, graph.Reply | list[graph.Reply]]):
        self.routes = {url: list(r) if isinstance(r, list) else [r] for url, r in routes.items()}
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def send(self, method: str, url: str, headers, body: bytes | None = None) -> graph.Reply:
        self.calls.append((method, url, dict(headers)))
        if method == "POST" and url == graph.token_url(TENANT):
            return reply({"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3599})
        answers = self.routes.get(url)
        if method != "GET" or not answers:
            raise AssertionError(f"unexpected request {method} {url}")
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def post_form(self, url: str, form) -> graph.Reply:
        self.calls.append(("POST", url, {}))
        if not url.endswith("/auth/token"):
            raise AssertionError(f"unexpected token request {url}")
        return reply("halo/token_response.json")

    def gets(self) -> list[str]:
        return [url for method, url, _ in self.calls if method == "GET"]


class Clock:
    """graph.monotonic and graph.sleep together: sleeping moves the clock, so every wait is counted once."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(graph, "monotonic", c.monotonic)
    monkeypatch.setattr(graph, "sleep", c.sleep)
    return c


def connector(**scope: Any) -> Any:
    return SimpleNamespace(scope=scope)


def by_id(values: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {v["external_id"]: v for v in values}


# ---- Microsoft Graph ------------------------------------------------------------------------------------------------

APPS_URL = f"{GRAPH}/applications?{entra.OWNERS}&$top=100"
SPS_URL = f"{GRAPH}/servicePrincipals?{entra.OWNERS}&$top=100"


def entra_routes(**changes: graph.Reply) -> dict[str, Any]:
    routes: dict[str, Any] = {
        SPS_URL: reply("graph/servicePrincipals_list.json"),
        APPS_URL: reply("graph/applications_list_page1.json"),
        load("graph/applications_list_page1.json")["@odata.nextLink"]: reply("graph/applications_list_page2.json"),
    }
    routes.update(changes)
    return routes


def test_graph_applications_and_service_principals_replay_through_the_entra_source(clock):
    transport = Replay(entra_routes())
    found = entra.fetch(connector(), MICROSOFT, transport)

    next_link = load("graph/applications_list_page1.json")["@odata.nextLink"]
    assert transport.gets() == [SPS_URL, APPS_URL, next_link]  # @odata.nextLink followed as given, then stopped
    assert all(h["Authorization"] == f"Bearer {TOKEN}" for m, _, h in transport.calls if m == "GET")
    assert found.notes == [] and found.empty == {}

    values = by_id([entra.map_object(raw) for raw in found])
    assert sorted(values) == [
        "00000000-0000-0000-0000-000000000201",
        "00000000-0000-0000-0000-000000000202",
        "00000000-0000-0000-0000-000000000203",
    ]
    web = values["00000000-0000-0000-0000-000000000201"]
    assert web["display_name"] == "Contoso Web"
    assert web["data"]["registration"] and web["data"]["enterprise_app"]
    assert web["data"]["application_id"] == "00000000-0000-0000-0000-000000000101"
    assert web["data"]["service_principal_id"] == "00000000-0000-0000-0000-000000000501"
    assert web["data"]["homepage"] == "https://app1.example.com/"
    assert web["data"]["publisher"] == "Example Publisher"
    assert web["data"]["publisher_tenant"] == TENANT
    assert web["data"]["sign_in_audience"] == "AzureADMyOrg"
    assert web["data"]["tags"] == ["WindowsAzureActiveDirectoryIntegratedApp"]
    assert web["data"]["owners"] == [
        {
            "external_id": "00000000-0000-0000-0000-000000000401",
            "name": "Alex Example",
            "email": "alex@example.com",
            "type": "user",
            "resolved": True,
        }
    ]
    # The client secret's expiry comes through; its hint ("abc") never does.
    assert web["credentials"] == [
        {
            "key_id": "00000000-0000-0000-0000-000000000301",
            "credential_type": "secret",
            "display_name": "Synthetic secret",
            "origin": "app_registration",
            "object_id": "00000000-0000-0000-0000-000000000101",
            "expires_on": "2027-01-01",
        }
    ]
    # The enterprise app's name wins over the registration's; the second page's app is merged too.
    assert values["00000000-0000-0000-0000-000000000202"]["display_name"] == "Slack"
    assert values["00000000-0000-0000-0000-000000000202"]["data"]["publisher_tenant"] == (
        "00000000-0000-0000-0000-000000009001"
    )
    last = values["00000000-0000-0000-0000-000000000203"]
    assert last["data"]["type"] == "ManagedIdentity" and last["data"]["homepage"] == "https://app3.example.com/"
    assert [entra.skip_reason(values[k], {}) for k in sorted(values)] == [None, None, "not_application"]


def test_graph_service_principal_without_publisher_name_falls_back_to_the_publisher_domain(clock):
    """`publisherName` isn't in the servicePrincipal resource's documented properties (only a $select
    value in a list example). Today's behavior without it: the registration's documented `publisherDomain`."""
    sps = load("graph/servicePrincipals_list.json")
    for sp in sps["value"]:
        del sp["publisherName"]
    transport = Replay(entra_routes(**{SPS_URL: reply(json.dumps(sps).encode())}))
    values = by_id([entra.map_object(raw) for raw in entra.fetch(connector(), MICROSOFT, transport)])
    assert {v["data"]["publisher"] for v in values.values()} == {"example.com"}


def test_graph_subscribed_skus_replay_after_a_documented_429_with_retry_after(clock):
    status, headers = sidecar("graph/error_429_throttling.headers.json")
    url = f"{GRAPH}/subscribedSkus?$select={entra_licenses.SELECT}"
    transport = Replay(
        {url: [reply("graph/error_429_throttling.json", status, headers), reply("graph/subscribedSkus_list.json")]}
    )
    found = entra_licenses.fetch(connector(), MICROSOFT, transport)

    assert clock.slept == [10.0]  # Retry-After: 10, waited once, then the same request again
    assert transport.gets() == [url, url]
    values = by_id([entra_licenses.map_object(raw) for raw in found])
    assert values["00000000-0000-0000-0000-000000001002"]["display_name"] == "ENTERPRISEPREMIUM"
    assert values["00000000-0000-0000-0000-000000001002"]["data"] == {
        "sku_id": "00000000-0000-0000-0000-000000001002",
        "sku_part_number": "ENTERPRISEPREMIUM",
        "seats_purchased": 25,
        "seats_assigned": 14,
        "seats_warning": 0,
        "seats_suspended": 0,
        "seats_locked_out": 0,
        "capability_status": "Enabled",
        "applies_to": "User",
    }
    crm = values["00000000-0000-0000-0000-000000001006"]["data"]
    assert (crm["seats_purchased"], crm["seats_suspended"], crm["capability_status"]) == (0, 25, "Suspended")


def test_graph_documented_error_shapes_parse(clock):
    status, headers = sidecar("graph/error_429_throttling.headers.json")
    throttled = graph.error_of(reply("graph/error_429_throttling.json", status, headers))
    assert (throttled.status, throttled.code, throttled.retry_after, throttled.throttled) == (
        429,
        "TooManyRequests",
        10.0,
        True,
    )
    documented = graph.error_of(reply("graph/error_shape_documented.json", 400))
    assert (documented.code, documented.message) == ("badRequest", "Uploaded fragment overlaps with existing data.")
    schema = graph.error_of(reply("graph/error_resource_type_schema.json", 400))  # spells innererror, has details
    assert (schema.code, schema.message) == ("string", "string")


# ---- Azure Resource Manager -----------------------------------------------------------------------------------------

ARM = azure.ARM
SUB_A = "00000000-0000-0000-0000-000000003001"
SUB_B = "00000000-0000-0000-0000-000000003002"
ACCOUNT = f"/subscriptions/{SUB_A}/resourceGroups/rg1/providers/Microsoft.Automation/automationAccounts/JPEDDeployDSC1"


def arm_routes() -> dict[str, Any]:
    subs = load("arm/subscriptions_list.json")
    sites = load("arm/web_sites_list.json")
    routes: dict[str, Any] = {
        f"{ARM}/subscriptions?api-version={azure.SUBSCRIPTIONS_API}": reply("arm/subscriptions_list.json"),
        subs["nextLink"]: reply(EMPTY_ODATA_PAGE),
        sites["nextLink"]: reply(EMPTY_ODATA_PAGE),
        f"{ARM}{ACCOUNT}/runbooks?api-version={azure.AUTOMATION_API}": reply("arm/automation_runbooks_list.json"),
    }
    for sub, logic, web, accounts in (
        (SUB_A, "arm/logic_workflows_list.json", "arm/web_sites_list.json", "arm/automation_accounts_list.json"),
        (SUB_B, EMPTY_ODATA_PAGE, EMPTY_ODATA_PAGE, EMPTY_ODATA_PAGE),  # the second subscription is empty
    ):
        base = f"{ARM}/subscriptions/{sub}/providers"
        routes[f"{base}/Microsoft.Logic/workflows?api-version={azure.LOGIC_API}"] = reply(logic)
        routes[f"{base}/Microsoft.Web/sites?api-version={azure.WEB_API}"] = reply(web)
        routes[f"{base}/Microsoft.Automation/automationAccounts?api-version={azure.AUTOMATION_API}"] = reply(accounts)
    return routes


def test_arm_lists_replay_through_the_azure_source(clock):
    transport = Replay(arm_routes())
    found = azure.fetch(connector(), MICROSOFT, transport)

    a, b = f"{ARM}/subscriptions/{SUB_A}/providers", f"{ARM}/subscriptions/{SUB_B}/providers"
    assert transport.gets() == [
        f"{ARM}/subscriptions?api-version={azure.SUBSCRIPTIONS_API}",
        load("arm/subscriptions_list.json")["nextLink"],  # nextLink followed as given
        f"{a}/Microsoft.Logic/workflows?api-version={azure.LOGIC_API}",
        f"{a}/Microsoft.Web/sites?api-version={azure.WEB_API}",
        load("arm/web_sites_list.json")["nextLink"],
        f"{a}/Microsoft.Automation/automationAccounts?api-version={azure.AUTOMATION_API}",
        f"{ARM}{ACCOUNT}/runbooks?api-version={azure.AUTOMATION_API}",
        f"{b}/Microsoft.Logic/workflows?api-version={azure.LOGIC_API}",
        f"{b}/Microsoft.Web/sites?api-version={azure.WEB_API}",
        f"{b}/Microsoft.Automation/automationAccounts?api-version={azure.AUTOMATION_API}",
    ]
    values = sorted((azure.map_object(raw) for raw in found), key=lambda v: v["display_name"])
    summary = [
        (
            v["display_name"],
            v["data"]["azure_type"],
            v["data"]["subscription_name"],
            v["data"]["resource_group"],
            v["data"]["state"],
        )
        for v in values
    ]
    assert summary == [
        ("ASR-AddPublicIp", "runbook", "Example Subscription", "rg", "Published"),
        ("func-sample", "function_app", "Example Subscription", "test-rg", "Running"),
        ("sitef6141", "app_service", "Example Subscription", "test-rg", "Running"),
        ("std-logic", "logic_app", "Example Subscription", "test-rg", "Running"),
        ("test-workflow", "logic_app", "Example Subscription", "test-rg", "Enabled"),
    ]
    runbook = values[0]["data"]
    assert (runbook["runbook_type"], runbook["automation_account"], runbook["location"]) == (
        "PowerShellWorkflow",
        "JPEDDeployDSC1",
        "East US 2",
    )
    assert values[1]["data"]["site_kind"] == "functionapp,linux"
    assert values[4]["data"]["location"] == "brazilsouth" and values[4]["data"]["site_kind"] == ""
    # App Services are off by default; the rest are kept.
    assert [azure.skip_reason(v, {}) for v in values] == [None, None, "app_services", None, None]
    assert any("kind 'workflowapp'" in note for note in found.notes)


def test_arm_documented_error_schema_is_read_for_its_code(clock):
    """Learn shows only the generic ErrorResponse (code, message, target, details, additionalInfo);
    the AuthorizationFailed, SubscriptionNotFound and MissingSubscriptionRegistration bodies the source branches on are
    not on any page read. What the replay proves: a 403 in the documented shape is read for error.code and becomes a
    refusal naming the Reader role."""
    routes = arm_routes()
    routes[f"{ARM}/subscriptions?api-version={azure.SUBSCRIPTIONS_API}"] = reply("arm/error_response_schema.json", 403)
    with pytest.raises(Refused) as refused:
        azure.fetch(connector(), MICROSOFT, Replay(routes))
    assert (refused.value.status, refused.value.code, refused.value.missing) == (403, "string", True)
    assert "Reader" in str(refused.value)


# ---- Power Platform -------------------------------------------------------------------------------------------------

DATAVERSE = "org00000001.crm.dynamics.com"
BOTS_URL = f"https://{DATAVERSE}/api/data/v9.2/bots?$select={powerplatform.BOT_FIELDS}"


def test_power_platform_environments_and_dataverse_bots_replay(clock):
    next_link = load("powerplatform/dataverse_bots_page1.json")["@odata.nextLink"]
    transport = Replay(
        {
            powerplatform.BAP_ENVIRONMENTS: reply("powerplatform/bap_environments_list.json"),
            BOTS_URL: with_sidecar(
                "powerplatform/dataverse_bots_page1.json", "powerplatform/dataverse_page_headers.json"
            ),
            next_link: with_sidecar(EMPTY_ODATA_PAGE, "powerplatform/dataverse_page_headers.json"),
        }
    )
    found = powerplatform.fetch(connector(all_environments=True), MICROSOFT, transport)

    bot_calls = [(url, h) for m, url, h in transport.calls if m == "GET" and DATAVERSE in url]
    assert [url for url, _ in bot_calls] == [BOTS_URL, next_link]  # the next link unchanged, as the docs require
    assert all(h["Prefer"] == powerplatform.PREFER["Prefer"] for _, h in bot_calls)  # same maxpagesize every page
    [agent] = [powerplatform.map_object(raw) for raw in found]
    assert agent["external_id"] == f"{DATAVERSE}:00000000-0000-0000-0000-000000004101"
    assert agent["display_name"] == "Synthetic HR Helper"
    data = agent["data"]
    assert data["environment_name"] == "My Power Platform Environment"
    assert data["environment_id"] == "00000000-0000-0000-0000-000000004001"
    assert (data["schema_name"], data["state"], data["status"], data["authentication"], data["user_auth"]) == (
        "cr123_hrHelper",
        "Active",
        "Provisioned",
        "Integrated",
        True,
    )
    assert (data["owner_name"], data["language"], data["published_on"]) == (
        "Alex Example",
        1033,
        "2025-03-01T12:00:00Z",
    )
    assert powerplatform.skip_reason(agent, {}) is None


# ---- Okta -----------------------------------------------------------------------------------------------------------

OKTA_BASE = "https://example.okta.com"
OKTA = connections.OktaCredential(OKTA_BASE, "0oa00000000000000009", "kid", "okta.apps.read", "replay-key")


@pytest.fixture
def okta_signed_in(monkeypatch):
    """Okta's token endpoint (a signed client assertion) isn't what these fixtures cover: sign-in answers a token."""
    monkeypatch.setattr(okta, "request_token", lambda *args, **kwargs: {"access_token": TOKEN})


def test_okta_apps_replay_through_two_link_header_lines(clock, okta_signed_in, monkeypatch):
    monkeypatch.setattr(rest_source, "wall", lambda: 1760000000.0)
    page1_url = okta_source.apps_url(OKTA_BASE)
    _, page1_headers = sidecar("okta/apps_list_page1.headers.json")
    next_url = rest_source.next_link(page1_headers["link"])
    routes: dict[str, Any] = {
        page1_url: with_sidecar("okta/apps_list_page1.json", "okta/apps_list_page1.headers.json"),
        next_url: with_sidecar("okta/apps_list_page2.json", "okta/apps_list_page2.headers.json"),
    }
    for app_id in ("0oa00000000000000001", "0oa00000000000000002", "0oa00000000000000003"):
        routes[okta_source.users_url(OKTA_BASE, app_id)] = reply("okta/app_users_list.json")
    transport = Replay(routes)
    found = okta_source.fetch(connector(), OKTA, transport)

    assert next_url == f"{OKTA_BASE}/api/v1/apps?after=0oa00000000000000002&limit=200&includeNonDeleted=true"
    assert transport.gets()[:2] == [page1_url, next_url]  # rel="next" from the second link line, then no more
    assert clock.slept == []  # 99 of 100 left: no pause
    values = by_id([okta_source.map_object(raw) for raw in found])
    assert sorted((v["display_name"], v["data"]["assigned_users"]) for v in values.values()) == [
        ("hrportal1", 1),
        ("hrportal2", 1),
        ("page2app", 1),
    ]
    first = values["0oa00000000000000001"]["data"]
    assert (first["app_name"], first["sign_on_mode"], first["status"], first["inactive"]) == (
        "workday",
        "SAML_2_0",
        "ACTIVE",
        False,
    )
    assert first["created"] == "2021-05-17T23:10:49.000Z"


def test_okta_429_without_retry_after_waits_for_the_rate_limit_reset(clock, okta_signed_in, monkeypatch):
    monkeypatch.setattr(rest_source, "wall", lambda: 1760000050.0 + (clock.now - 1000.0))
    status, headers = sidecar("okta/error_429.headers.json")
    url = okta_source.apps_url(OKTA_BASE)
    transport = Replay({url: [reply("okta/error_429.json", status, headers), reply("okta/apps_list_page2.json")]})
    api = okta_source.client(connector(), OKTA, transport)
    apps = api.get_link_pages(url, "list applications", okta_source.READ_ONLY_ADMIN, 5)

    assert clock.slept == [10.0]  # X-Rate-Limit-Reset is 10 s away; no Retry-After is documented, none needed
    assert transport.gets() == [url, url]
    assert [a["label"] for a in apps] == ["page2app"]


def test_okta_documented_403_names_the_admin_role(clock, okta_signed_in):
    url = okta_source.apps_url(OKTA_BASE)
    api = okta_source.client(connector(), OKTA, Replay({url: reply("okta/error_403.json", 403)}))
    with pytest.raises(Refused) as refused:
        api.get(url, "list applications", okta_source.READ_ONLY_ADMIN)
    assert (refused.value.status, refused.value.code, refused.value.missing) == (403, "E0000006", True)
    assert okta_source.READ_ONLY_ADMIN.name in str(refused.value)


# ---- Google Workspace -----------------------------------------------------------------------------------------------

GOOGLE = connections.GoogleCredential("admin@example.com", "sa@example.iam.gserviceaccount.com", "1", "replay-key")
GOOGLE_NOW = 1_794_000_000.0  # 2026-11-06, after the fixture's authorize event


@pytest.fixture
def google_signed_in(monkeypatch):
    """Google's token exchange (a signed JWT assertion) isn't what these fixtures cover: sign-in answers a token."""
    monkeypatch.setattr(google_workspace, "request_token", lambda *args, **kwargs: {"access_token": TOKEN})
    monkeypatch.setattr(google_workspace, "clock", lambda: GOOGLE_NOW)


def google_urls() -> tuple[str, str]:
    from datetime import UTC, datetime, timedelta

    end = datetime.fromtimestamp(GOOGLE_NOW, UTC)
    start = end - timedelta(days=gw_source.SCOPE_DEFAULTS["days"])
    page_token = load("google/activities_list_token_authorize.json")["nextPageToken"]
    return gw_source.authorize_url(start, end), gw_source.authorize_url(start, end, page_token)


LAST_PAGE = {"kind": "admin#reports#activities", "etag": '"SYNTHETIC-ETAG-2"', "items": []}


def test_google_token_authorize_activities_replay(clock, google_signed_in):
    first, second = google_urls()
    transport = Replay({first: reply("google/activities_list_token_authorize.json"), second: reply(LAST_PAGE)})
    found = gw_source.fetch(connector(), GOOGLE, transport)

    assert transport.gets() == [first, second]  # nextPageToken passed as pageToken
    [app] = [gw_source.map_object(raw) for raw in found]
    assert app["external_id"] == "000000000000-synthetic.apps.googleusercontent.com"
    assert app["display_name"] == "Synthetic Notes App"
    data = app["data"]
    assert (data["client_type"], data["users"], data["grants"], data["last_authorized_at"]) == (
        "WEB",
        1,
        1,
        "2026-11-01T09:30:00.000Z",
    )
    assert data["scopes"] == [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]
    assert data["risky_scopes"] == data["scopes"] and data["risky"]
    assert "user@example.com" not in json.dumps(found)  # only aggregates leave the run


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [
        ("google/error_quota_429.json", 429, "rateLimitExceeded"),
        ("google/error_503_reports_quota.json", 503, "UNAVAILABLE"),
    ],
)
def test_google_quota_answers_are_retried_with_backoff(clock, google_signed_in, name, status, code):
    assert gw_source._error_code(load(name)) == code
    first, second = google_urls()
    transport = Replay(
        {first: [reply(name, status), reply("google/activities_list_token_authorize.json")], second: reply(LAST_PAGE)}
    )
    found = gw_source.fetch(connector(), GOOGLE, transport)
    assert clock.slept == [5.0]  # no Retry-After: Google's 5-second backoff
    assert [raw["app_name"] for raw in found] == ["Synthetic Notes App"]


@pytest.mark.parametrize("reason", ["userRateLimitExceeded", "quotaExceeded"])
def test_google_403_quota_answer_is_retried_not_reported_as_a_missing_permission(clock, google_signed_in, reason):
    # Google's Directory API limits page documents 403 userRateLimitExceeded and 403 quotaExceeded as quota
    # errors to retry with exponential back-off, not as a missing privilege.
    quota = load("google/error_quota_429.json")  # the same format, with the documented 403 reason
    quota["error"].update(code=403, message="User Rate Limit Exceeded")
    quota["error"]["errors"][0].update(reason=reason, message="User Rate Limit Exceeded")
    first, second = google_urls()
    transport = Replay(
        {first: [reply(quota, 403), reply("google/activities_list_token_authorize.json")], second: reply(LAST_PAGE)}
    )
    found = gw_source.fetch(connector(), GOOGLE, transport)
    assert clock.slept == [5.0]
    assert [raw["app_name"] for raw in found] == ["Synthetic Notes App"]


def test_google_403_without_a_quota_reason_is_still_a_missing_permission(clock, google_signed_in):
    refused = load("google/error_quota_429.json")
    refused["error"].update(code=403, message="Not Authorized to access this resource/api")
    refused["error"]["errors"][0].update(reason="forbidden", domain="global", message="Not Authorized")
    first, _ = google_urls()
    with pytest.raises(Refused) as e:
        gw_source.fetch(connector(), GOOGLE, Replay({first: reply(refused, 403)}))
    assert e.value.missing and clock.slept == []


# ---- tickets: Jira, ServiceNow, Halo ----------------------------------------------------------------------------------

JIRA = tickets.Credential(TicketSystem.JIRA, "https://example.atlassian.net", "user@example.com", "replay-token")
JIRA_ISSUE = "https://example.atlassian.net/rest/api/3/issue/ED-1?fields=status,summary"
SERVICENOW = tickets.Credential(TicketSystem.SERVICENOW, "https://example.service-now.com", "user", "replay-password")


def servicenow_url(table: str, number: str) -> str:
    return (
        f"https://example.service-now.com/api/now/table/{table}?sysparm_query=number={number}"
        "&sysparm_fields=number,state,short_description&sysparm_display_value=all&sysparm_limit=1"
    )


def test_jira_issue_status_and_summary_replay():
    transport = Replay({JIRA_ISSUE: reply("jira/issue_get_fields_status_summary.json")})
    state = tickets.fetch(JIRA, "ED-1", transport)
    assert state == tickets.TicketState(TicketStatus.IN_PROGRESS, "In Progress", "Synthetic ticket title")
    assert transport.calls[0][2]["Authorization"].startswith("Basic ")


def test_jira_official_issue_example_without_status_reads_as_unknown():
    """Atlassian's own getIssue example has no fields.status or fields.summary (the client asks for
    exactly those). Today's behavior on that documented answer: unknown status, no title, no error."""
    state = tickets.fetch(JIRA, "ED-1", Replay({JIRA_ISSUE: reply("jira/issue_get_official_example.json")}))
    assert state == tickets.TicketState(TicketStatus.UNKNOWN, "", "")


def test_jira_create_and_documented_400_replay():
    url = "https://example.atlassian.net/rest/api/3/issue"

    class Post(Replay):
        def send(self, method, url_, headers, body=None):
            self.calls.append((method, url_, dict(headers)))
            assert (method, url_) == ("POST", url)
            return self.routes[url].pop(0)

    transport = Post({url: [reply("jira/create_issue_201.json", 201), reply("jira/error_400.json", 400)]})
    created = tickets.create(JIRA, target="ED", issue_type="Task", title="New", description="", transport=transport)
    assert (created.key, created.url) == ("ED-24", "https://example.atlassian.net/browse/ED-24")
    with pytest.raises(tickets.TicketError) as refused:
        tickets.create(JIRA, target="ED", issue_type="Task", title="New", description="", transport=transport)
    assert refused.value.kind == "bad_answer" and "priority" not in str(refused.value)  # never the server's text


def test_servicenow_display_value_all_replay():
    url = servicenow_url("incident", "INC0010001")
    state = tickets.fetch(SERVICENOW, "INC0010001", Replay({url: reply("servicenow/table_get_display_value_all.json")}))
    assert state == tickets.TicketState(TicketStatus.IN_PROGRESS, "In Progress", "Synthetic incident title")


def test_servicenow_official_stored_value_answer_on_the_problem_table():
    """The documented example is the default sysparm_display_value=false form, where `state` is the
    stored value. Softwarden asks for `all`, so this answer only comes from an instance that ignores it. Today's
    behavior: the title is read, the state "4" of a problem is unknown (stored values are mapped for incident only)."""
    url = servicenow_url("problem", "PRB0000050")
    status, headers = sidecar("servicenow/table_get_official.headers.json")
    answer = reply("servicenow/table_get_official_display_value_false.json", status, headers)
    state = tickets.fetch(SERVICENOW, "PRB0000050", Replay({url: answer}))
    assert state == tickets.TicketState(TicketStatus.UNKNOWN, "4", "Switch occasionally drops connections")


def test_halo_token_replay_for_ticket_reads():
    """No public page shows a Halo ticket (its reference is tenant-hosted), so only the token step is replayed."""
    credential = tickets.Credential(
        TicketSystem.HALO, "https://halo.example.com", "", "replay-secret", "client", "read:tickets"
    )
    assert tickets.halo_token(credential, Replay({})) == "SYNTHETIC-TOKEN"


# ---- Halo discovery -------------------------------------------------------------------------------------------------


def test_halo_asset_page_replay(clock, monkeypatch):
    # The page is full at a page size of 2, so the short-page rule can't end the run: only `record_count` (2) does.
    # Without it the source asks for page 2, which the replay refuses.
    monkeypatch.setattr(halo, "PAGE_SIZE", 2)
    credential = connections.HaloCredential("https://halo.example.com", "client", "replay-secret")
    url = "https://halo.example.com/api/Asset?paginate=true&page_size=100&page_no=1"
    transport = Replay({url: reply("halo/assets_list_page.json")})
    found = halo.fetch(connector(), credential, transport)

    assert transport.gets() == [url]  # a full page, and record_count reached: no second request
    assert transport.calls[-1][2]["Authorization"] == "Bearer SYNTHETIC-TOKEN"
    values = by_id([halo.map_object(raw) for raw in found])
    assert values["101"]["display_name"] == "Jira Cloud"
    assert values["101"]["data"] == {
        "halo_asset_id": "101",
        "asset_type": "Application",
        "inventory_number": "INV-0001",
        "client": "Example Client",
        "site": "Main Site",
        "inactive": False,
        "description": "",
        "enterprise_app": False,
    }
    assert values["102"]["data"]["inactive"] is True
    assert [halo.skip_reason(values[k], {}) for k in ("101", "102")] == [None, "inactive"]


# ---- ID tokens through core/oidc_discovery ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def sign(name: str, key, **added: Any) -> tuple[str, dict[str, Any]]:
    """The fixture's claims signed with `key` under the fixture's header, its timestamps moved to now (same offsets),
    plus `added` claims."""
    data = load(name)
    claims = dict(data["payload"])
    shift = int(time.time()) - int(claims["iat"])
    for claim in ("iat", "nbf", "exp", "auth_time"):
        if claim in claims:
            claims[claim] += shift
    claims.update(added)
    header = {k: v for k, v in data["header"].items() if k != "alg"}
    return jwt.encode(claims, key, algorithm=data["header"]["alg"], headers=header), claims


def publish(monkeypatch, key, jwks_uri: str) -> None:
    jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True) | {"kid": "SYNTHETIC-KID", "use": "sig"}

    def network(method, url, body, headers):
        assert (method, url) == ("GET", jwks_uri)
        return reply({"keys": [jwk]})

    monkeypatch.setattr(oidc_discovery, "network", network)


def metadata(issuer: str, jwks_uri: str) -> oidc_discovery.Metadata:
    return oidc_discovery.Metadata(issuer, f"{issuer}/authorize", f"{issuer}/token", jwks_uri, ("RS256",), ())


def test_entra_v2_id_token_claims_validate(monkeypatch, signing_key):
    claims = load("entra/id_token_claims_v2.json")["payload"]
    issuer = oidc_discovery.entra_issuer(TENANT)
    assert claims["iss"] == issuer
    jwks = f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"
    publish(monkeypatch, signing_key, jwks)
    provider = oidc_discovery.Provider("entra", issuer + oidc_discovery.WELL_KNOWN, issuer, claims["aud"], TENANT)
    raw, _ = sign("entra/id_token_claims_v2.json", signing_key)  # the fixture carries the nonce Softwarden sent

    validated = oidc_discovery.validate(provider, metadata(issuer, jwks), raw, "synthetic-nonce")
    assert oidc_discovery.identity_key(provider, validated) == (issuer, "00000000-0000-0000-0000-000000007002")
    # VF-2 (fixed): no `email` in the default v2 claims. The mutable `preferred_username` is shown on a pending access
    # request but never links a member: the first sign-in goes to pending access (tests/test_invitations.py).
    assert "email" not in validated
    assert oidc_discovery.email_of(provider, validated) == "alex@example.com"
    assert oidc_discovery.link_email(provider, validated) == ""
    member = {**validated, "email": "Alex@example.com"}
    assert oidc_discovery.link_email(provider, member) == "alex@example.com"
    assert oidc_discovery.link_email(provider, {**member, "idp": "https://sts.windows.net/" + "0" * 8 + "/"}) == ""
    with pytest.raises(oidc_discovery.ProviderError) as other_tenant:
        oidc_discovery.validate(
            oidc_discovery.Provider("entra", "", issuer, claims["aud"], "00000000-0000-0000-0000-0000000000b0"),
            metadata(issuer, jwks),
            raw,
            "synthetic-nonce",
        )
    assert other_tenant.value.code == "tenant"


def test_okta_id_token_claims_validate_with_the_nonce_we_send(monkeypatch, signing_key):
    """Okta's documented example has no `nonce`: Softwarden always sends one at authorize (core/oidc.begin) and Okta
    returns it then, so the replay adds it. Without it the token is refused (recorded below)."""
    issuer = OKTA_BASE
    jwks = f"{OKTA_BASE}/oauth2/v1/keys"
    publish(monkeypatch, signing_key, jwks)
    payload = load("okta/id_token_claims.json")["payload"]
    provider = oidc_discovery.Provider("okta", issuer + oidc_discovery.WELL_KNOWN, issuer, payload["aud"])
    raw, _ = sign("okta/id_token_claims.json", signing_key, nonce="sent-at-authorize")

    validated = oidc_discovery.validate(provider, metadata(issuer, jwks), raw, "sent-at-authorize")
    assert oidc_discovery.identity_key(provider, validated) == (issuer, "00u00000000000000001")
    assert oidc_discovery.email_of(provider, validated) == "john.doe@example.com"
    assert validated["ver"] == 1 and validated["email_verified"] is True

    as_documented, _ = sign("okta/id_token_claims.json", signing_key)
    with pytest.raises(oidc_discovery.ProviderError) as missing:
        oidc_discovery.validate(provider, metadata(issuer, jwks), as_documented, "sent-at-authorize")
    assert missing.value.code == "missing_claim"


# ---- housekeeping -----------------------------------------------------------------------------------------------------


def test_every_fixture_is_replayed_and_carries_no_annotation():
    """A fixture nobody reads proves nothing, and annotations belong in the folder's README."""
    source = Path(__file__).read_text(encoding="utf-8")
    for path in sorted(FIXTURES.rglob("*.json")):
        name = path.relative_to(FIXTURES).as_posix()
        data = json.loads(path.read_text(encoding="utf-8"))
        keys = data.keys() if isinstance(data, dict) else []
        assert not [k for k in keys if k.startswith(("_note", "_synthesized"))], name
        if name not in DOCUMENTED_ONLY:
            assert f'"{name}"' in source, f"{name} isn't replayed by any test"
        assert (path.parent / "README.md").exists(), name


# Kept for the record (a schema, or a shape no Softwarden code path reads), described in their folder's README.
DOCUMENTED_ONLY = {
    "arm/throttling_429.headers.json",  # headers only, with Learn's placeholder value; the Graph 429 covers the path
    "jira/error_schema_ErrorCollection.json",
}


def test_replay_helpers_resolve_the_okta_link_lines_as_the_real_transport_does():
    _, headers = sidecar("okta/apps_list_page1.headers.json")
    assert headers["link"].count("rel=") == 2  # both lines kept (core/outbound.merged_headers)
    _, last = sidecar("okta/apps_list_page2.headers.json")
    assert rest_source.next_link(last["link"]) == ""
    assert urllib.parse.urlsplit(rest_source.next_link(headers["link"])).hostname == "example.okta.com"


def test_unexpected_requests_fail_the_replay():
    with pytest.raises(AssertionError):
        Replay({}).send("GET", "https://graph.microsoft.com/v1.0/users", {})
    with pytest.raises(SourceError):
        entra.fetch(
            connector(), MICROSOFT, Replay({SPS_URL: reply({"value": [], "@odata.nextLink": "https://evil.example/"})})
        )
