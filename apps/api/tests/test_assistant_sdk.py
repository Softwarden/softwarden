"""The assistant against the real Anthropic SDK (discovery/assistant.make_client) on a mock HTTP transport: the JSON
bodies the SDK actually sends, a real refusal body, the SDK's own exception mapping for 401, 429, 500 and 529, one
reservation per HTTP attempt (messages.create never retries inside the SDK), and nothing of the prompt in the logs at
DEBUG. Also what is sent from Entra data (no notes, no query strings, no tenant domain) and a race-safe first save.
The transport answers every request itself: nothing reaches the network."""

import json
import logging
import threading
from datetime import timedelta
from decimal import Decimal

import anthropic
import httpx2
import pytest
from django.db import connection
from django.utils import timezone

from core.capabilities import set_edition
from core.models import Job, JobStatus
from discovery import assistant, entra
from discovery.models import AssistantBatch, AssistantSettings, AssistantSuggestion, AssistantUsage, Observation
from tests.conftest import drain_jobs, make_org
from tests.test_assistant import KEY, REAL_MAKE_CLIENT, app, enable, observe, sensible_rows
from tests.test_runs import make_connector

JOB = assistant.JOB


def message(rows=None, *, stop="end_turn", category=None, usage=(1200, 180, 0, 0)):
    inp, out, read, write = usage
    return {
        "id": "msg_01mock",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": json.dumps({"suggestions": rows or []})}],
        "stop_reason": stop,
        "stop_sequence": None,
        "stop_details": {"type": "refusal", "category": category, "explanation": None} if stop == "refusal" else None,
        "usage": {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write,
        },
    }


def error(status, kind):
    return status, {"type": "error", "error": {"type": kind, "message": f"mock {kind}"}}


class Wire:
    """A mock transport: count_tokens always answers; /v1/messages answers from `replies` (status, body), then with a
    sensible answer built from the request it got."""

    def __init__(self):
        self.replies: list[tuple[int, dict]] = []
        self.messages: list[dict] = []
        self.counts: list[dict] = []
        self.requests: list[httpx2.Request] = []
        self.keys: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        if request.url.path == "/v1/messages/count_tokens":
            self.counts.append(body)
            return httpx2.Response(200, json={"input_tokens": 1000})
        assert request.url.path == "/v1/messages", request.url
        self.messages.append(body)
        if self.replies:
            status, payload = self.replies.pop(0)
        else:
            status, payload = 200, message(sensible_rows(body))
        return httpx2.Response(status, json=payload, headers={"x-should-retry": "true"} if status >= 429 else {})


@pytest.fixture
def wire(monkeypatch):
    found = Wire()

    def factory(key):
        found.keys.append(key)
        return REAL_MAKE_CLIENT(key, http_client=httpx2.Client(transport=httpx2.MockTransport(found)))

    monkeypatch.setattr(assistant, "make_client", factory)
    return found


@pytest.fixture
def setup(a):
    a["org"], _ = set_edition(a["org"], "enterprise")
    org, admin = a["org"], a["users"]["admin"]
    connector = make_connector(org, admin)
    record = app(org, admin, "Zoom")
    enable(org, admin)
    return {
        **a,
        "connector": connector,
        "record": record,
        "zoom": observe(connector, "Zoom"),
        "hub": observe(connector, "Expense Hub"),
    }


def run_batch(setup):
    made = assistant.request_batch(setup["org"], setup["users"]["editor"], {})
    drain_jobs(JOB)
    made.refresh_from_db()
    return made


def retry_now():
    Job.objects.filter(kind=JOB, status=JobStatus.QUEUED).update(run_after=timezone.now() - timedelta(seconds=1))
    drain_jobs(JOB)


# ---- the bodies the real SDK sends ------------------------------------------------------------------------------------


def test_the_sdk_sends_the_schema_and_no_unsupported_parameters(setup, wire):
    done = run_batch(setup)
    assert done.status == "succeeded" and done.counts["suggested"] == 2
    [sent] = wire.messages
    [counted] = wire.counts
    for body in (sent, counted):
        assert body["model"] == "claude-sonnet-5-5"
        assert body["output_config"]["format"]["type"] == "json_schema"
        assert body["output_config"]["effort"] == "low"
        assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
        assert not {"thinking", "temperature", "top_p", "top_k", "tool_choice", "tools"} & set(body)
        assert [m["role"] for m in body["messages"]] == ["user"]  # no assistant prefill
        assert body["system"] == [{"type": "text", "text": assistant.SYSTEM}]
    assert sent["max_tokens"] == assistant.MAX_TOKENS and "max_tokens" not in counted
    request = next(r for r in wire.requests if r.url.path == "/v1/messages")
    assert request.url.host == "api.anthropic.com" and request.url.scheme == "https"
    assert request.headers["x-api-key"] == KEY and wire.keys == [KEY]
    zoom = AssistantSuggestion.objects.get(observation=setup["zoom"])
    assert (zoom.action, zoom.target_id) == ("link", setup["record"].id)
    usage = AssistantUsage.objects.get()
    assert (usage.input_tokens, usage.output_tokens, usage.cost_usd, usage.outcome) == (
        1200,
        180,
        Decimal("0.004200"),
        "ok",
    )


def test_a_real_refusal_body_is_parsed_with_its_category(setup, wire):
    wire.replies = [(200, message(stop="refusal", category="frontier_llm", usage=(900, 3, 0, 0)))]
    done = run_batch(setup)
    assert done.counts["refused"] == 2
    s = AssistantSuggestion.objects.get(observation=setup["zoom"])
    assert s.state == "refused" and "frontier_llm" in s.detail
    assert AssistantUsage.objects.get().outcome == "refusal"


def test_a_real_cut_off_answer_is_a_failure(setup, wire):
    body = message(stop="max_tokens", usage=(1000, 2048, 0, 0))
    body["content"][0]["text"] = '{"suggestions": [{"candidate": "c1", "act'
    wire.replies = [(200, body)]
    done = run_batch(setup)
    assert done.counts["failed"] == 2 and AssistantUsage.objects.get().outcome == "max_tokens"


def test_401_is_the_sdks_authentication_error_and_turns_the_assistant_off(setup, wire):
    wire.replies = [error(401, "authentication_error")]
    done = run_batch(setup)
    assert (done.status, done.stop_reason) == ("failed", "key_refused") and len(wire.messages) == 1
    row = AssistantSettings.objects.get()
    assert not row.enabled and "HTTP 401" in row.key_refused_detail


@pytest.mark.parametrize(
    "status,kind,outcome",
    [
        (429, "rate_limit_error", "rate_limited"),
        (500, "api_error", "api_error"),
        (529, "overloaded_error", "api_error"),
    ],
)
def test_429_500_and_529_are_retried_by_the_job_with_one_reservation_per_http_attempt(
    setup, wire, status, kind, outcome
):
    wire.replies = [error(status, kind), error(status, kind)]  # the third attempt is answered
    made = assistant.request_batch(setup["org"], setup["users"]["editor"], {})
    drain_jobs(JOB)
    assert len(wire.messages) == 1  # the SDK didn't retry the call itself (max_retries=0, despite x-should-retry)
    retry_now()
    assert len(wire.messages) == 2
    retry_now()
    made.refresh_from_db()
    assert made.status == "succeeded" and made.counts["suggested"] == 2
    usages = list(AssistantUsage.objects.filter(batch=made).order_by("created_at"))
    assert len(usages) == len(wire.messages) == 3  # one reservation per HTTP attempt
    assert [u.outcome for u in usages] == [outcome, outcome, "ok"]
    assert [u.cost_usd for u in usages[:2]] == [Decimal("0"), Decimal("0")]


def test_the_sdk_maps_each_status_to_its_own_exception(setup, monkeypatch):
    """The classes failed_call relies on come from the SDK's own mapping, not from the tests."""
    seen = []

    def handler(request):
        status = int(request.headers["x-test-status"])
        return httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "m"}})

    client = REAL_MAKE_CLIENT(KEY, http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    plain = client.with_options(max_retries=0)
    for status, cls in (
        (401, anthropic.AuthenticationError),
        (403, anthropic.PermissionDeniedError),
        (400, anthropic.BadRequestError),
        (429, anthropic.RateLimitError),
        (500, anthropic.InternalServerError),
        (529, anthropic.OverloadedError),
    ):
        with pytest.raises(cls) as raised:
            plain.messages.create(
                model="claude-sonnet-5-5",
                max_tokens=10,
                messages=[{"role": "user", "content": "x"}],
                extra_headers={"x-test-status": str(status)},
            )
        assert raised.value.status_code == status and isinstance(raised.value, anthropic.APIStatusError)
        seen.append(status)
    assert seen == [401, 403, 400, 429, 500, 529]


def test_an_unreachable_api_keeps_the_reservation_as_cost(setup, monkeypatch):
    def unreachable(request):
        if request.url.path == "/v1/messages/count_tokens":
            return httpx2.Response(200, json={"input_tokens": 1000})
        raise httpx2.ConnectError("no route", request=request)

    def factory(key):
        return REAL_MAKE_CLIENT(key, http_client=httpx2.Client(transport=httpx2.MockTransport(unreachable)))

    monkeypatch.setattr(assistant, "make_client", factory)
    made = assistant.request_batch(setup["org"], setup["users"]["editor"], {})
    drain_jobs(JOB)
    made.refresh_from_db()
    usage = AssistantUsage.objects.get()
    assert usage.outcome == "connection_error" and usage.cost_usd == usage.estimated_cost_usd > 0
    assert made.status == "queued"


# ---- logging -------------------------------------------------------------------------------------------------------------


def test_a_debug_level_real_call_logs_no_candidate_text(setup, wire, caplog):
    caplog.set_level(logging.DEBUG)
    run_batch(setup)
    text = caplog.text + json.dumps([r.__dict__ for r in caplog.records], default=str)
    assert "Expense Hub for the team" not in text and KEY not in text
    for name in ("anthropic", "httpx", "httpcore", "httpx2", "httpcore2"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING, name


def test_the_debug_check_would_catch_the_sdks_request_log(setup, wire, caplog, monkeypatch):
    """Positive control: with the pin lifted, the SDK logs the request at DEBUG and the check above would fail."""
    monkeypatch.setattr(logging.getLogger("anthropic"), "level", logging.DEBUG)
    monkeypatch.setattr(logging.getLogger("anthropic._base_client"), "level", logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    run_batch(setup)
    assert "Expense Hub for the team" in caplog.text


# ---- what is sent from Entra data ----------------------------------------------------------------------------------------


def test_entra_notes_query_strings_and_the_tenant_domain_are_never_sent(setup, wire):
    mapped = entra.map_object(
        {
            "servicePrincipal": {
                "id": "sp-1",
                "appId": "11111111-1111-1111-1111-111111111111",
                "displayName": "Notes app",
                "notes": "INTERNAL-NOTES-MARKER: owner is on leave",
                "homepage": "https://notes.example/app?tenant=TENANT-QUERY-MARKER#frag",
            },
            "application": {"publisherDomain": "contoso.onmicrosoft.com"},
        }
    )
    assert mapped["data"]["description_from"] == "notes"
    described = entra.map_object({"servicePrincipal": {"id": "sp-2", "description": "Real description", "notes": "n"}})
    assert "description_from" not in described["data"]
    obs = observe(setup["connector"], "Notes app")
    obs.data = mapped["data"]
    obs.save()
    # A home page with credentials in it is dropped whole; GUID-shaped path segments become {id}.
    secret_url = observe(setup["connector"], "Credential app")
    secret_url.data = {**secret_url.data, "homepage": "https://USER-MARKER:PASS-MARKER@intranet.example/app"}
    secret_url.save()
    guid_path = observe(setup["connector"], "Guid app")
    guid_path.data = {
        **guid_path.data,
        "homepage": "https://portal.example:8443/tenants/0d2e3f4a-1b2c-4d5e-8f90-a1b2c3d4e5f6/apps/{AB12CD34-0000-4000-8000-1234567890AB}/",
    }
    guid_path.save()
    run_batch(setup)
    sent = json.dumps(wire.messages + wire.counts)
    for marker in (
        "INTERNAL-NOTES-MARKER",
        "TENANT-QUERY-MARKER",
        "frag",
        "onmicrosoft.com",
        "USER-MARKER",
        "PASS-MARKER",
        "intranet.example",
        "0d2e3f4a-1b2c-4d5e-8f90-a1b2c3d4e5f6",
        "AB12CD34",
    ):
        assert marker not in sent, marker
    items = {
        c["name"]: c
        for body in wire.messages
        for c in json.loads(body["messages"][0]["content"].split("\n", 1)[1])["candidates"]
    }
    assert "homepage" not in items["Credential app"]
    assert items["Guid app"]["homepage"] == "https://portal.example:8443/tenants/{id}/apps/{id}/"
    item = next(
        c
        for body in wire.messages
        for c in json.loads(body["messages"][0]["content"].split("\n", 1)[1])["candidates"]
        if c["name"] == "Notes app"
    )
    assert item["homepage"] == "https://notes.example/app" and "description" not in item and "publisher" not in item


@pytest.mark.parametrize(
    "url,sent",
    [
        ("https://a.example/x?y=1#z", "https://a.example/x"),
        ("https://user@a.example/x", ""),
        ("https://:pw@a.example/x", ""),
        ("https://a.example:99999/x", ""),  # an invalid port
        ("ftp://a.example/x", ""),
        ("https://[2001:db8::1]:8080/p", "https://[2001:db8::1]:8080/p"),
        ("https://A.Example/Apps/12345678-1234-1234-1234-123456789012", "https://a.example/Apps/{id}"),
    ],
)
def test_homepages_are_reduced_to_scheme_host_port_and_path(url, sent):
    assert assistant._homepage(url) == sent


def test_sdk_environment_variables_cant_turn_on_tracing_content_or_debug_logs(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content")
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY_PROPAGATION", "true")
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")
    from anthropic._utils import _logs

    for name in ("anthropic", "httpx2"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.getLogger(name).level)  # restored afterwards
    _logs.setup_logging()  # what importing the SDK does with ANTHROPIC_LOG=debug
    assert logging.getLogger("anthropic").level == logging.DEBUG
    client = REAL_MAKE_CLIENT(
        KEY, http_client=httpx2.Client(transport=httpx2.MockTransport(lambda r: httpx2.Response(200)))
    )
    assert logging.getLogger("anthropic").level == logging.WARNING
    assert logging.getLogger("httpx2").level == logging.WARNING
    resolved = client._open_telemetry
    assert resolved["propagation"] is False
    assert resolved["traces"]["enabled"] is False and resolved["traces"]["content_mode"] == "metadata_only"
    # A copy (with_options, as messages.create uses) keeps it.
    copied = client.with_options(max_retries=0)._open_telemetry
    assert copied["propagation"] is False and copied["traces"]["enabled"] is False


def test_a_publisher_that_isnt_a_tenant_domain_is_sent(setup, wire):
    run_batch(setup)
    assert '"publisher":"Contoso"' in wire.messages[0]["messages"][0]["content"]


# ---- the first save is race-safe -----------------------------------------------------------------------------------------


def test_two_first_saves_at_once_both_succeed(transactional_db):
    f = make_org("racing")
    f["org"], _ = set_edition(f["org"], "enterprise")
    barrier, errors = threading.Barrier(2), []

    def save(cap):
        try:
            barrier.wait(10)
            assistant.update_settings(f["org"], f["users"]["admin"], {"monthly_cap_usd": cap})
        except Exception as e:  # noqa: BLE001  (reported below)
            errors.append(type(e).__name__)
        finally:
            connection.close()

    threads = [threading.Thread(target=save, args=(cap,)) for cap in ("20", "30")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert errors == []
    assert AssistantSettings.objects.filter(organization=f["org"]).count() == 1
    assert AssistantSettings.objects.get(organization=f["org"]).monthly_cap_usd in (Decimal("20"), Decimal("30"))


def test_nothing_here_reaches_the_network(setup, wire):
    run_batch(setup)
    assert all(r.url.host == "api.anthropic.com" for r in wire.requests) and wire.requests  # all answered by the mock
    assert not AssistantBatch.objects.filter(status="failed").exists()
    assert Observation.objects.count() == 2
