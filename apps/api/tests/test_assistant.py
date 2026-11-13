"""The assistant (enterprise capability `assistant`; discovery/assistant.py): suggestions for undecided
candidates, validated against the records offered; the monthly cap (refused before it is passed, the month in the
organization's timezone); the fingerprint skip; refusals, cut-off answers and every error class; Accept, Reject and
the decision recorded against the suggestion; the settings, usage and batch API with roles, the capability and
tenancy; the data sent (no secret, no credential); the key never leaving the server; and the cost arithmetic. Also two
discovery-run checks that were left for the assistant work.

The Anthropic SDK is never called: `discovery.assistant.make_client` is patched with a fake whose `messages.count_tokens`
and `messages.create` return objects shaped like the SDK's (stop_reason, stop_details, content, usage). No network."""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from catalog import services
from catalog.models import Resource
from core.capabilities import set_edition
from core.models import AuditEvent, Job, JobStatus
from core.problems import Conflict
from discovery import assistant, entra
from discovery import engine as discovery
from discovery.models import (
    AssistantBatch,
    AssistantSettings,
    AssistantSuggestion,
    AssistantUsage,
    ConnectorRun,
    DecisionKind,
    DiscoveredCredential,
    Observation,
    ReconciliationDecision,
    RunStatus,
)
from tests.conftest import drain_jobs, make_org, revoke_capability
from tests.test_runs import make_connector

KEY = "sk-ant-api03-planted-assistant-key-4242"
JOB = assistant.JOB
REAL_MAKE_CLIENT = assistant.make_client  # the production factory, taken before conftest's guard replaces it


# ---- the fake SDK ------------------------------------------------------------------------------------------------------


def reply(suggestions=None, *, stop="end_turn", category=None, usage=(1200, 180, 0, 0), text=None):
    """A Message-shaped object: content, stop_reason, stop_details, usage."""
    body = text if text is not None else json.dumps({"suggestions": suggestions or []})
    inp, out, read, write = usage
    return SimpleNamespace(
        stop_reason=stop,
        stop_details=SimpleNamespace(type="refusal", category=category) if stop == "refusal" else None,
        content=[SimpleNamespace(type="text", text=body)],
        usage=SimpleNamespace(
            input_tokens=inp, output_tokens=out, cache_read_input_tokens=read, cache_creation_input_tokens=write
        ),
    )


def candidates_in(kwargs):
    content = kwargs["messages"][0]["content"]
    return json.loads(content.split("\n", 1)[1])["candidates"]


def sensible(kwargs):
    """Links a candidate to its first offered record, else creates it; one short reason each."""
    return reply(sensible_rows(kwargs))


def sensible_rows(kwargs):
    rows = []
    for c in candidates_in(kwargs):
        if c["records"]:
            rows.append(
                {
                    "candidate": c["candidate"],
                    "action": "link",
                    "record": c["records"][0]["record"],
                    "confidence": 0.9,
                    "reason": "Same name.",
                }
            )
        else:
            rows.append(
                {
                    "candidate": c["candidate"],
                    "action": "create",
                    "record": "",
                    "confidence": 0.7,
                    "reason": "Real software.",
                }
            )
    return rows


class FakeMessages:
    def __init__(self, answer=sensible, counted=1000, count_error=None):
        self.answer, self.counted, self.count_error = answer, counted, count_error
        self.calls: list[dict] = []
        self.counts: list[dict] = []

    def count_tokens(self, **kwargs):
        self.counts.append(kwargs)
        if self.count_error is not None:
            raise self.count_error
        return SimpleNamespace(input_tokens=self.counted)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answer.pop(0) if isinstance(self.answer, list) else self.answer
        if isinstance(answer, Exception):
            raise answer
        return answer(kwargs) if callable(answer) else answer


@pytest.fixture
def fake(monkeypatch):
    """Patch the client factory; returns the fake's messages (its calls) and the keys the factory was given."""
    messages = FakeMessages()
    keys: list[str] = []

    def factory(key):
        keys.append(key)
        client = SimpleNamespace(messages=messages)
        client.with_options = lambda **options: (messages.options.append(options), client)[1]  # type: ignore[attr-defined]
        return client

    messages.options = []  # type: ignore[attr-defined]

    monkeypatch.setattr(assistant, "make_client", factory)
    messages.keys = keys  # type: ignore[attr-defined]
    return messages


# ---- fixtures ---------------------------------------------------------------------------------------------------------


def fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest()


def observe(connector, name, kind="application", **data):
    now = timezone.now()
    return Observation.objects.create(
        organization_id=connector.organization_id,
        connector=connector,
        kind=kind,
        external_id=name.lower().replace(" ", "-"),
        display_name=name,
        data={"description": f"{name} for the team", "publisher": "Contoso", **data},
        fingerprint=fingerprint(name),
        first_seen_at=now,
        last_seen_at=now,
        last_changed_at=now,
    )


def app(org, actor, name):
    return services.create_resource(org, actor, {"name": name, "kind": "application", "lifecycle": "active"})


def enable(org, actor, **extra):
    return assistant.update_settings(org, actor, {"enabled": True, "api_key": KEY, **extra})


@pytest.fixture
def ent(a):
    a["org"], _ = set_edition(a["org"], "enterprise")
    return a


@pytest.fixture
def setup(ent):
    """An enterprise organization with the assistant on, a connector, a record named Zoom and three candidates."""
    org, admin = ent["org"], ent["users"]["admin"]
    connector = make_connector(org, admin)
    zoom_record = app(org, admin, "Zoom")
    enable(org, admin)
    zoom = observe(connector, "Zoom")
    hub = observe(connector, "Expense Hub")
    test = observe(connector, "Test registration 7")
    return {**ent, "connector": connector, "zoom_record": zoom_record, "zoom": zoom, "hub": hub, "test": test}


def batch(setup, actor=None, **data):
    made = assistant.request_batch(setup["org"], actor or setup["users"]["editor"], data)
    drain_jobs(JOB)
    return AssistantBatch.objects.get(id=made.id)


def suggestion(obs):
    return AssistantSuggestion.objects.filter(observation=obs).order_by("-created_at").first()


def sid(obs):
    """The ID of the candidate's newest suggestion, as the SPA sends it with Accept and Reject."""
    found = suggestion(obs)
    return str(found.id) if found else "00000000-0000-4000-8000-000000000000"


# ---- suggestions -------------------------------------------------------------------------------------------------------


def test_a_batch_suggests_for_each_undecided_candidate(setup, fake):
    done = batch(setup)
    assert done.status == "succeeded" and done.stop_reason == ""
    assert done.counts == {
        "skipped": 0,
        "suggested": 3,
        "refused": 0,
        "failed": 0,
        "calls": 1,
        "unchanged": 0,
        "candidates": 3,
        "remaining": 0,
    }
    zoom = suggestion(setup["zoom"])
    assert (zoom.state, zoom.action, zoom.target_id, zoom.confidence) == (
        "ready",
        "link",
        setup["zoom_record"].id,
        Decimal("0.900"),
    )
    assert zoom.reason == "Same name." and zoom.outcome == "pending" and zoom.model == "claude-sonnet-5-5"
    assert suggestion(setup["hub"]).action == "create"
    # One call, the SDK's arguments exactly: the model from settings, effort low and the JSON schema, max_tokens;
    # no thinking, no temperature, top_p or top_k, no prefill, no forced tool choice.
    [call] = fake.calls
    assert set(call) == {"model", "system", "messages", "output_config", "max_tokens"}
    assert call["model"] == "claude-sonnet-5-5" and call["max_tokens"] == assistant.MAX_TOKENS
    assert call["output_config"]["effort"] == "low" and call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert [m["role"] for m in call["messages"]] == ["user"]  # the candidates last, after the stable system prompt
    # The system prompt is below the cacheable minimum: no cache_control marker.
    assert call["system"] == [{"type": "text", "text": assistant.SYSTEM}]
    assert len(assistant.SYSTEM) // 4 < assistant.CACHE_MIN_TOKENS
    assert fake.options == [{"max_retries": 0}]  # create never retries inside the SDK
    assert fake.counts == [{k: v for k, v in call.items() if k != "max_tokens"}]  # the same input was counted first
    assert fake.keys == [KEY]
    # The usage log: one row with all four token counts and its cost.
    usage = AssistantUsage.objects.get()
    assert (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_input_tokens,
        usage.cache_creation_input_tokens,
    ) == (1200, 180, 0, 0)
    assert usage.cost_usd == Decimal("0.004200") and usage.outcome == "ok" and usage.candidate_count == 3
    price = assistant.prices()
    assert price is not None
    assert usage.estimated_cost_usd == assistant.worst_case(price, 1000)
    # The job carried the batch's ID only.
    job = Job.objects.get(kind=JOB)
    assert job.payload == {"batch_id": str(done.id)} and job.status == JobStatus.SUCCEEDED
    assert AuditEvent.objects.filter(action="assistant_batch.finished", target_id=done.id).exists()


def test_a_successful_discovery_run_queues_a_batch_for_its_connector(setup, fake):
    run = discovery.run(setup["connector"])
    assert run.status == RunStatus.SUCCEEDED and run.counts["new"] > 0
    queued = AssistantBatch.objects.get()
    assert (queued.trigger, queued.connector_id, queued.connector_run_id, queued.status) == (
        "after_run",
        setup["connector"].id,
        run.id,
        "queued",
    )
    drain_jobs(JOB)
    queued.refresh_from_db()
    assert queued.status == "succeeded" and queued.counts["suggested"] == queued.counts["candidates"] > 3


def test_no_batch_is_queued_when_the_assistant_is_off_or_the_organization_lacks_it(setup, fake):
    AssistantSettings.objects.update(enabled=False)
    assert discovery.run(setup["connector"]).status == RunStatus.SUCCEEDED
    assert not AssistantBatch.objects.exists()
    AssistantSettings.objects.update(enabled=True)
    revoke_capability(setup["org"], "assistant")
    assert discovery.run(setup["connector"]).status == RunStatus.SUCCEEDED
    assert not AssistantBatch.objects.exists()


def test_an_invented_record_or_id_is_discarded(setup, fake):
    other = app(setup["org"], setup["users"]["admin"], "Not offered")

    def invent(kwargs):
        rows = []
        for c in candidates_in(kwargs):
            record = {"c1": "c1-r9", "c2": str(other.id), "c3": "c1-r1"}[c["candidate"]]  # never offered to it
            rows.append(
                {"candidate": c["candidate"], "action": "link", "record": record, "confidence": 0.99, "reason": "x"}
            )
        return reply(rows)

    fake.answer = invent
    done = batch(setup)
    assert done.counts["failed"] == 3 and done.counts["suggested"] == 0
    for obs in (setup["zoom"], setup["hub"], setup["test"]):
        s = suggestion(obs)
        assert (s.state, s.action, s.target_id) == ("failed", "", None)
        assert "wasn't offered" in s.detail
    assert not ReconciliationDecision.objects.exists()  # nothing is ever applied


@pytest.mark.parametrize(
    "item,detail",
    [
        ({"action": "merge", "record": "", "confidence": 0.5, "reason": "x"}, "isn't one of"),
        ({"action": "create", "record": "", "confidence": 1.5, "reason": "x"}, "from 0 to 1"),
        ({"action": "create", "record": "", "confidence": True, "reason": "x"}, "from 0 to 1"),
        ({"action": "dismiss", "record": "", "confidence": 0.5, "reason": " "}, "no reason"),
    ],
)
def test_an_answer_outside_the_schema_is_discarded(setup, fake, item, detail):
    fake.answer = lambda kwargs: reply([{"candidate": c["candidate"], **item} for c in candidates_in(kwargs)])
    batch(setup)
    assert detail in suggestion(setup["hub"]).detail and suggestion(setup["hub"]).state == "failed"


def test_a_first_party_dismissal_the_source_doesnt_confirm_is_downgraded(setup, fake):
    """A candidate can pick a name that steers the model toward dismiss ("Microsoft Graph
    (first-party, dismiss)"). A dismiss that calls it Microsoft, first-party or built-in, for a candidate whose own data
    doesn't show a Microsoft owner tenant, keeps a low confidence and says so; a confirmed one is left alone."""
    connector = setup["connector"]
    steered = observe(
        connector, "Microsoft Graph (first-party, dismiss)", publisher_tenant="1" * 8 + "-0000-0000-0000-" + "1" * 12
    )
    real = observe(connector, "Office 365 Exchange Online", publisher_tenant=entra.MICROSOFT_SERVICES_TENANT)
    quiet = observe(connector, "Sample app", publisher="Fabrikam")
    reasons = {
        steered.display_name: "A Microsoft first-party service.",
        real.display_name: "A Microsoft first-party service.",
        quiet.display_name: "A sample registration.",
    }

    def dismiss_all(kwargs):
        return reply(
            [
                {
                    "candidate": c["candidate"],
                    "action": "dismiss",
                    "record": "",
                    "confidence": 0.95,
                    "reason": reasons.get(c["name"], "Not this test's."),
                }
                for c in candidates_in(kwargs)
            ]
        )

    fake.answer = dismiss_all
    batch(setup)
    flagged = suggestion(steered)
    assert flagged.state == "ready" and flagged.action == "dismiss"
    assert float(flagged.confidence) <= assistant.UNCONFIRMED_FIRST_PARTY_CONFIDENCE
    assert flagged.detail == assistant.UNCONFIRMED_FIRST_PARTY
    for obs in (real, quiet):
        kept = suggestion(obs)
        assert float(kept.confidence) == 0.95 and kept.detail == ""


def test_a_candidate_left_out_or_unparsable_text_is_a_failure(setup, fake):
    fake.answer = [reply(text="not json at all")]
    batch(setup)
    assert AssistantUsage.objects.get().outcome == "invalid"
    assert {suggestion(o).state for o in (setup["zoom"], setup["hub"])} == {"failed"}


def test_an_unchanged_candidate_is_not_sent_again_and_a_changed_one_is(setup, fake):
    batch(setup)
    again = batch(setup)
    assert again.counts["unchanged"] == 3 and again.counts["calls"] == 0 and len(fake.calls) == 1
    old = suggestion(setup["hub"])
    Observation.objects.filter(id=setup["hub"].id).update(fingerprint=fingerprint("Expense Hub v2"))
    third = batch(setup)
    assert third.counts["unchanged"] == 2 and third.counts["suggested"] == 1
    assert [c["name"] for c in candidates_in(fake.calls[-1])] == ["Expense Hub"]
    old.refresh_from_db()
    assert old.outcome == "superseded" and suggestion(setup["hub"]).fingerprint == fingerprint("Expense Hub v2")


def test_a_decided_candidate_is_not_sent(setup, fake):
    discovery.dismiss(setup["org"], setup["users"]["editor"], setup["test"], {"reason": "Test app"})
    batch(setup)
    assert {c["name"] for c in candidates_in(fake.calls[0])} == {"Zoom", "Expense Hub"}


def test_a_batch_for_one_connector_sends_only_its_candidates(setup, fake):
    other = make_connector(setup["org"], setup["users"]["admin"], name="Second")
    observe(other, "Other tenant app")
    batch(setup, connector=str(other.id))
    assert [c["name"] for c in candidates_in(fake.calls[0])] == ["Other tenant app"]


def test_large_queues_are_sent_a_chunk_at_a_time_up_to_the_batch_limit(setup, fake, monkeypatch):
    monkeypatch.setattr(assistant, "BATCH_MAX", 7)
    for n in range(10):
        observe(setup["connector"], f"Extra app {n}")
    done = batch(setup)
    assert done.counts["remaining"] == 6 and done.counts["candidates"] == 13
    # 7 in this batch, then a follow-up takes the other 6 (drained in the same pass).
    assert [len(candidates_in(c)) for c in fake.calls] == [5, 2, 5, 1]
    assert list(AssistantBatch.objects.order_by("queued_at").values_list("trigger", flat=True)) == [
        "manual",
        "follow_up",
    ]


# ---- refusals, cut-off answers and errors --------------------------------------------------------------------------------


def test_a_refusal_records_the_suggestions_as_unavailable_with_its_category(setup, fake):
    fake.answer = [reply(stop="refusal", category="cyber", usage=(900, 5, 0, 0))]
    done = batch(setup)
    assert done.status == "succeeded" and done.counts["refused"] == 3
    s = suggestion(setup["zoom"])
    assert s.state == "refused" and "cyber" in s.detail and s.action == ""
    usage = AssistantUsage.objects.get()
    assert usage.outcome == "refusal" and usage.cost_usd == Decimal("0.001850")
    assert batch(setup).counts["calls"] == 0  # a refusal at this fingerprint isn't sent again


def test_a_cut_off_answer_is_a_failure_that_a_later_batch_retries(setup, fake):
    fake.answer = [reply(stop="max_tokens", usage=(1000, 2048, 0, 0)), sensible]
    first = batch(setup)
    assert first.counts["failed"] == 3 and suggestion(setup["zoom"]).state == "failed"
    assert "cut off" in suggestion(setup["zoom"]).detail
    assert AssistantUsage.objects.get(batch=first).outcome == "max_tokens"
    second = batch(setup)
    assert second.counts["suggested"] == 3 and suggestion(setup["zoom"]).state == "ready"


def status_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("refused", response=httpx2.Response(status, request=request), body=None)


def connection_error():
    return anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


@pytest.mark.parametrize("cls,status", [(anthropic.AuthenticationError, 401), (anthropic.PermissionDeniedError, 403)])
def test_a_refused_key_turns_the_assistant_off_and_tells_the_admin(setup, fake, api, cls, status):
    fake.answer = [status_error(cls, status)]
    done = batch(setup)
    assert (done.status, done.stop_reason) == ("failed", "key_refused")
    row = AssistantSettings.objects.get()
    assert not row.enabled and row.key_refused_at is not None and f"HTTP {status}" in row.key_refused_detail
    assert AuditEvent.objects.filter(action="assistant_settings.key_refused").exists()
    assert AssistantUsage.objects.get().outcome == "key_refused" and AssistantUsage.objects.get().cost_usd == 0
    body = api(setup["users"]["admin"]).get("assistant/settings").json_body
    assert body["key_refused_at"] and not body["enabled"]
    # A new key clears the notice; the Admin turns it on again.
    api(setup["users"]["admin"]).patch("assistant/settings", {"api_key": "sk-ant-new-key-0002", "enabled": True})
    row.refresh_from_db()
    assert row.enabled and row.key_refused_at is None and row.key_refused_detail == ""


@pytest.mark.parametrize(
    "error,outcome",
    [
        (lambda: status_error(anthropic.RateLimitError, 429), "rate_limited"),
        (lambda: status_error(anthropic.InternalServerError, 500), "api_error"),
        (connection_error, "connection_error"),
    ],
)
def test_a_rate_limit_a_5xx_or_an_unreachable_api_is_retried_by_the_job(setup, fake, error, outcome):
    fake.answer = [error(), error(), error()]
    made = assistant.request_batch(setup["org"], setup["users"]["editor"], {})
    drain_jobs(JOB)
    made.refresh_from_db()
    job = Job.objects.get(kind=JOB)
    assert made.status == "queued" and job.status == JobStatus.QUEUED and job.attempts == 1
    assert "try" in job.last_error and KEY not in job.last_error
    for _ in range(2):
        Job.objects.filter(id=job.id).update(run_after=timezone.now() - timedelta(seconds=1))
        drain_jobs(JOB)
    made.refresh_from_db()
    job.refresh_from_db()
    assert made.status == "failed" and made.stop_reason in ("rate_limited", "api_error", "unreachable")
    assert job.status == JobStatus.SUCCEEDED and job.attempts == 3  # the batch records its own failure
    usages = list(AssistantUsage.objects.all())
    assert len(usages) == 3 and {u.outcome for u in usages} == {outcome}
    if outcome == "connection_error":  # it may have been billed: the reservation stays as its cost
        assert all(u.cost_usd == u.estimated_cost_usd > 0 for u in usages)
    else:
        assert all(u.cost_usd == 0 for u in usages)


def test_a_retry_carries_on_where_it_stopped(setup, fake, monkeypatch):
    monkeypatch.setattr(assistant, "CHUNK", 2)
    fake.answer = [sensible, status_error(anthropic.RateLimitError, 429), sensible]
    made = assistant.request_batch(setup["org"], setup["users"]["editor"], {})
    drain_jobs(JOB)
    Job.objects.filter(kind=JOB).update(run_after=timezone.now() - timedelta(seconds=1))
    drain_jobs(JOB)
    made.refresh_from_db()
    assert made.status == "succeeded" and made.counts["suggested"] == 3 and made.counts["calls"] == 3
    assert made.counts["candidates"] == 3 and made.counts["unchanged"] == 0  # stable across the retry
    assert [len(candidates_in(c)) for c in fake.calls] == [2, 1, 1]  # the first chunk wasn't sent again


@pytest.mark.parametrize(
    "error,reason",
    [
        (lambda: status_error(anthropic.BadRequestError, 400), "bad_request"),
        (lambda: status_error(anthropic.NotFoundError, 404), "api_error"),
    ],
)
def test_a_400_or_another_status_stops_the_batch(setup, fake, error, reason):
    fake.answer = [error()]
    done = batch(setup)
    assert (done.status, done.stop_reason) == ("failed", reason) and AssistantSettings.objects.get().enabled
    assert Job.objects.get(kind=JOB).status == JobStatus.SUCCEEDED


def test_an_error_while_counting_is_handled_like_the_call(setup, fake):
    fake.count_error = status_error(anthropic.BadRequestError, 400)
    done = batch(setup)
    assert done.stop_reason == "bad_request" and fake.calls == []
    assert AssistantUsage.objects.get().outcome == "bad_request"


def test_errors_are_logged_by_type_and_status_only(setup, fake, caplog):
    caplog.set_level(logging.DEBUG)
    error = status_error(anthropic.BadRequestError, 400)
    error.message = f"echoing {KEY} and Expense Hub for the team"
    fake.answer = [error]
    batch(setup)
    text = caplog.text + json.dumps([r.__dict__ for r in caplog.records], default=str)
    assert "BadRequestError" in text and "status=400" in text
    assert KEY not in text and "Expense Hub for the team" not in text


def test_an_unexpected_error_fails_the_batch_without_its_message(setup, fake, caplog):
    def boom(kwargs):
        raise RuntimeError(f"secret {KEY}")

    fake.answer = boom
    caplog.set_level(logging.DEBUG)
    done = batch(setup)
    assert (done.status, done.stop_reason) == ("failed", "unexpected") and done.error.startswith(
        "Unexpected error (RuntimeError)"
    )
    assert KEY not in caplog.text


def test_a_stopping_worker_stops_the_batch(setup, fake):
    made = assistant.request_batch(setup["org"], setup["users"]["editor"], {})
    from core import jobs

    job = jobs.claim(ids=[made.job_id])
    jobs.run(job, should_stop=lambda: True)
    made.refresh_from_db()
    assert (made.status, made.stop_reason) == ("stopped", "worker_stopped") and fake.calls == []


def test_a_batch_whose_job_is_gone_lapses_and_frees_the_queue(setup, fake):
    made = assistant.request_batch(setup["org"], setup["users"]["editor"], {})
    Job.objects.filter(id=str(made.job_id)).update(status=JobStatus.ABANDONED)
    again = assistant.request_batch(setup["org"], setup["users"]["editor"], {})
    made.refresh_from_db()
    assert made.status == "failed" and made.stop_reason == "lapsed" and again.status == "queued"


def test_one_queued_or_running_batch_per_organization(setup, fake):
    assistant.request_batch(setup["org"], setup["users"]["editor"], {})
    with pytest.raises(Conflict, match="already working"):
        assistant.request_batch(setup["org"], setup["users"]["editor"], {})
    with pytest.raises(IntegrityError), transaction.atomic():
        AssistantBatch.objects.create(organization=setup["org"], trigger="manual")


# ---- the cap ---------------------------------------------------------------------------------------------------------------


def test_the_cap_is_refused_before_it_would_be_passed(setup, fake, api):
    price = assistant.prices()
    assert price is not None
    estimate = assistant.worst_case(price, 1000)
    AssistantSettings.objects.update(monthly_cap_usd=Decimal("0.05"))
    AssistantUsage.objects.create(
        organization=setup["org"], model="claude-sonnet-5-5", cost_usd=Decimal("0.05") - estimate, outcome="ok"
    )
    done = batch(setup)  # exactly at the cap: allowed
    assert done.status == "succeeded" and len(fake.calls) == 1
    Observation.objects.filter(id=setup["hub"].id).update(fingerprint=fingerprint("changed"))
    stopped = batch(setup)
    assert (stopped.status, stopped.stop_reason) == ("stopped", "cap_reached") and len(fake.calls) == 1
    assert "Cap reached" in stopped.error
    assert AssistantUsage.objects.count() == 2  # nothing reserved or sent for the refused call
    body = api(setup["users"]["admin"]).get("assistant/settings").json_body
    assert body["month"]["cap_reached"] and body["month"]["cap_reached_at"]
    assert api(setup["users"]["viewer"]).get("assistant/batches").json_body[0]["stop_reason"] == "cap_reached"


def test_a_call_in_progress_counts_at_its_reserved_worst_case(setup, fake):
    AssistantUsage.objects.create(
        organization=setup["org"], model="claude-sonnet-5-5", cost_usd=Decimal("9.99"), outcome="pending"
    )
    assert batch(setup).stop_reason == "cap_reached" and fake.calls == []


def test_the_month_rolls_over_in_the_organizations_timezone(setup, fake):
    org = setup["org"]
    org.timezone = "Pacific/Auckland"  # UTC+13 in November
    org.save(update_fields=["timezone"])
    late_october_utc = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)  # 1 November, 01:00 in Auckland
    row = AssistantUsage.objects.create(organization=org, model="m", cost_usd=Decimal("4.000000"), outcome="ok")
    AssistantUsage.objects.filter(id=row.id).update(created_at=late_october_utc)
    november = datetime(2026, 11, 15, tzinfo=UTC)
    assert assistant.month_start(org, november).isoformat() == "2026-11-01T00:00:00+13:00"
    assert assistant.month_to_date(org, november) == Decimal("4")
    org.timezone = "UTC"
    org.save(update_fields=["timezone"])
    assert assistant.month_to_date(org, november) == Decimal("0")  # October in UTC
    december = datetime(2026, 12, 1, 0, 30, tzinfo=UTC)
    assert assistant.month_start(org, december).date().isoformat() == "2026-12-01"
    assert assistant.next_month(assistant.month_start(org, december)).date().isoformat() == "2027-01-01"


def test_last_months_spend_doesnt_count_against_this_month(setup, fake):
    row = AssistantUsage.objects.create(organization=setup["org"], model="m", cost_usd=Decimal("9.99"), outcome="ok")
    AssistantUsage.objects.filter(id=row.id).update(
        created_at=assistant.month_start(setup["org"]) - timedelta(seconds=1)
    )
    assert batch(setup).status == "succeeded"


# ---- cost arithmetic -----------------------------------------------------------------------------------------------------


def test_cost_arithmetic():
    price = assistant.prices("claude-sonnet-5-5")
    assert price == {
        "input": Decimal("2.00"),
        "output": Decimal("10.00"),
        "cache_read": Decimal("0.20"),
        "cache_write": Decimal("2.50"),
    }
    million = 1_000_000
    assert assistant.cost(price, input_tokens=million) == Decimal("2.000000")
    assert assistant.cost(price, output_tokens=million) == Decimal("10.000000")
    assert assistant.cost(price, cache_read=million) == Decimal("0.200000")
    assert assistant.cost(price, cache_write=million) == Decimal("2.500000")
    assert assistant.cost(price, input_tokens=1200, output_tokens=180, cache_read=4000, cache_write=100) == Decimal(
        "0.005250"
    )
    assert assistant.cost(price, input_tokens=1) == Decimal("0.000002")
    assert assistant.cost(price, cache_read=1) == Decimal("0.000000")  # 0.0000002 rounds half up to 0
    assert assistant.cost(price, cache_read=3) == Decimal("0.000001")
    # Worst case: input plus 5% and 200 tokens (1000 -> 1250) at the dearer of input and cache write, plus
    # max_tokens of output.
    assert assistant.worst_case(price, 1000) == Decimal("0.003125") + Decimal("0.020480")
    assert assistant.worst_case(price, 0) == Decimal("0.000500") + Decimal("0.020480")
    assert assistant.worst_case(price, 1) == Decimal("0.000505") + Decimal("0.020480")  # ceil(1.05) = 2, plus 200
    assert isinstance(assistant.cost(price, input_tokens=7), Decimal)


def test_prices_can_be_configured_and_an_unknown_model_is_refused(setup, fake, settings, api):
    assert assistant.prices("claude-unknown-9") is None
    settings.SOFTWARDEN_ASSISTANT_PRICES = json.dumps({"claude-unknown-9": {"input": "1", "output": "5"}})
    assert assistant.prices("claude-unknown-9") == {
        "input": Decimal("1"),
        "output": Decimal("5"),
        "cache_read": Decimal("0.1"),
        "cache_write": Decimal("1.25"),
    }
    settings.SOFTWARDEN_ASSISTANT_PRICES = "not json"
    settings.SOFTWARDEN_ASSISTANT_MODEL = "claude-unknown-9"
    assert assistant.ready_to_run(setup["org"]) == "unpriced_model"
    with pytest.raises(Conflict, match="no price"):
        assistant.request_batch(setup["org"], setup["users"]["editor"], {})
    AssistantSettings.objects.update(enabled=False)
    response = api(setup["users"]["admin"]).patch("assistant/settings", {"enabled": True})
    assert response.status_code == 400 and "no price" in response.json_body["errors"]["enabled"][0]
    assert api(setup["users"]["admin"]).get("assistant/settings").json_body["model_priced"] is False


# ---- decisions recorded against the suggestion -----------------------------------------------------------------------------


def test_accept_performs_the_suggested_link_and_records_it(setup, fake, api):
    batch(setup)
    editor = api(setup["users"]["editor"])
    response = editor.post(
        f"discovery/observations/{setup['zoom'].id}/assistant/accept", {"suggestion": sid(setup["zoom"])}
    )
    assert response.status_code == 200, response.json_body
    assert (
        response.json_body["status"] == "linked" and response.json_body["assistant_suggestion"]["outcome"] == "accepted"
    )
    d = ReconciliationDecision.objects.get(observation=setup["zoom"])
    assert (d.decision, d.resource_id, d.decided_by) == (
        DecisionKind.LINKED,
        setup["zoom_record"].id,
        setup["users"]["editor"],
    )
    s = suggestion(setup["zoom"])
    assert (s.outcome, s.via_accept, s.decided_action, s.decided_resource_id, s.outcome_by) == (
        "accepted",
        True,
        "link",
        setup["zoom_record"].id,
        setup["users"]["editor"],
    )
    actions = list(
        AuditEvent.objects.filter(request_id__isnull=False).order_by("created_at").values_list("action", flat=True)
    )
    assert "observation.linked" in actions and "assistant_suggestion.recorded" in actions
    again = editor.post(
        f"discovery/observations/{setup['zoom'].id}/assistant/accept", {"suggestion": sid(setup["zoom"])}
    )
    assert again.status_code == 409


def test_accept_creates_or_dismisses_through_the_engine(setup, fake, api):
    def plan(kwargs):
        rows = [
            {
                "candidate": c["candidate"],
                "action": "dismiss" if c["name"].startswith("Test") else "create",
                "record": "",
                "confidence": 0.8,
                "reason": "Looks like a test app.",
            }
            for c in candidates_in(kwargs)
        ]
        return reply(rows)

    fake.answer = plan
    batch(setup)
    editor = api(setup["users"]["editor"])
    created = editor.post(
        f"discovery/observations/{setup['hub'].id}/assistant/accept",
        {"suggestion": sid(setup["hub"]), "name": "Expense Hub (finance)"},
    )
    assert created.status_code == 200 and created.json_body["status"] == "linked"
    assert Resource.objects.filter(name="Expense Hub (finance)").exists()
    assert suggestion(setup["hub"]).outcome == "accepted"
    dismissed = editor.post(
        f"discovery/observations/{setup['test'].id}/assistant/accept", {"suggestion": sid(setup["test"])}
    )
    assert dismissed.status_code == 200 and dismissed.json_body["status"] == "dismissed"
    assert (
        ReconciliationDecision.objects.get(observation=setup["test"]).reason
        == "Assistant suggestion accepted: Looks like a test app."
    )
    refused = editor.post(
        f"discovery/observations/{setup['zoom'].id}/assistant/accept", {"suggestion": sid(setup["zoom"]), "reason": "x"}
    )
    assert refused.status_code == 400 and "reason" in refused.json_body["errors"]  # create takes no reason


def test_a_different_decision_overrides_and_a_stale_suggestion_is_ignored(setup, fake):
    batch(setup)
    org, editor = setup["org"], setup["users"]["editor"]
    discovery.dismiss(org, editor, setup["zoom"], {"reason": "Not ours"})
    s = suggestion(setup["zoom"])
    assert (s.outcome, s.decided_action, s.via_accept) == ("overridden", "dismiss", False)
    other = app(org, editor, "Expense tracker")
    discovery.link(org, editor, setup["hub"], {"resource": str(other.id)})
    assert suggestion(setup["hub"]).outcome == "overridden"  # suggested create, linked instead
    Observation.objects.filter(id=setup["test"].id).update(fingerprint=fingerprint("changed since"))
    discovery.dismiss(org, editor, Observation.objects.get(id=setup["test"].id), {"reason": "Old"})
    assert suggestion(setup["test"]).outcome == "ignored"
    event = AuditEvent.objects.filter(action="assistant_suggestion.recorded").latest("created_at")
    assert event.changes["outcome"] == "ignored" and event.changes["decided"] == "dismiss"


def test_linking_to_the_suggested_record_by_hand_counts_as_accepted(setup, fake):
    batch(setup)
    discovery.link(setup["org"], setup["users"]["editor"], setup["zoom"], {"resource": str(setup["zoom_record"].id)})
    s = suggestion(setup["zoom"])
    assert s.outcome == "accepted" and not s.via_accept


def test_reject_records_a_no_and_a_later_decision_keeps_it(setup, fake, api):
    batch(setup)
    editor = api(setup["users"]["editor"])
    response = editor.post(
        f"discovery/observations/{setup['zoom'].id}/assistant/reject",
        {"suggestion": sid(setup["zoom"]), "note": "Different Zoom"},
    )
    assert response.status_code == 200 and response.json_body["status"] == "new"
    s = suggestion(setup["zoom"])
    assert (s.outcome, s.outcome_note, s.outcome_by) == ("rejected", "Different Zoom", setup["users"]["editor"])
    assert AuditEvent.objects.filter(action="assistant_suggestion.rejected", target_id=s.id).exists()
    assert (
        editor.post(
            f"discovery/observations/{setup['zoom'].id}/assistant/accept", {"suggestion": sid(setup["zoom"])}
        ).status_code
        == 409
    )
    discovery.create_record(setup["org"], setup["users"]["editor"], setup["zoom"], {"name": "Zoom Rooms"})
    s.refresh_from_db()
    assert (s.outcome, s.decided_action) == ("rejected", "create")


def test_undo_puts_the_suggestion_back_to_undecided(setup, fake, api):
    batch(setup)
    editor = api(setup["users"]["editor"])
    editor.post(f"discovery/observations/{setup['zoom'].id}/assistant/accept", {"suggestion": sid(setup["zoom"])})
    editor.post(f"discovery/observations/{setup['zoom'].id}/reopen", {})
    s = suggestion(setup["zoom"])
    assert (s.outcome, s.decided_action, s.outcome_by, s.via_accept) == ("pending", "", None, False)
    event = AuditEvent.objects.filter(action="observation.reopened").latest("created_at")
    assert event.changes["assistant_suggestion"] == "undecided again"
    assert (
        editor.post(
            f"discovery/observations/{setup['zoom'].id}/assistant/accept", {"suggestion": sid(setup["zoom"])}
        ).status_code
        == 200
    )


def test_a_stale_or_unavailable_suggestion_cant_be_accepted(setup, fake, api):
    fake.answer = [reply(stop="refusal", category="general_harms")]
    batch(setup)
    editor = api(setup["users"]["editor"])
    assert (
        editor.post(
            f"discovery/observations/{setup['zoom'].id}/assistant/accept", {"suggestion": sid(setup["zoom"])}
        ).status_code
        == 409
    )
    fake.answer = sensible
    Observation.objects.filter(id=setup["hub"].id).update(fingerprint=fingerprint("again"))
    batch(setup)
    Observation.objects.filter(id=setup["hub"].id).update(fingerprint=fingerprint("and again"))
    assert (
        editor.post(
            f"discovery/observations/{setup['hub'].id}/assistant/accept", {"suggestion": sid(setup["hub"])}
        ).status_code
        == 409
    )


def test_accept_and_reject_act_only_on_the_suggestion_the_editor_saw(setup, fake, api):
    """A run changes the candidate and an after-run batch writes a newer suggestion (here a link where
    the Editor saw a dismiss). The Editor's page still holds the old one: accepting or rejecting it is a 409 in words,
    and neither the candidate nor the newer suggestion changes."""

    def dismiss_all(kwargs):
        return reply([dict(r, action="dismiss", record="") for r in sensible_rows(kwargs)])

    fake.answer = dismiss_all
    batch(setup)
    seen = sid(setup["zoom"])  # what the Editor's page shows: dismiss
    Observation.objects.filter(id=setup["zoom"].id).update(fingerprint=fingerprint("renamed in the source"))
    fake.answer = sensible  # the newer suggestion: link to Zoom
    batch(setup)
    newer = suggestion(setup["zoom"])
    assert str(newer.id) != seen and newer.action == "link" and newer.outcome == "pending"
    assert AssistantSuggestion.objects.get(id=seen).outcome == "superseded"
    editor = api(setup["users"]["editor"])
    url = f"discovery/observations/{setup['zoom'].id}/assistant"
    for action, body in (("accept", {}), ("accept", {"reason": "x"}), ("reject", {"note": "no"})):
        r = editor.post(f"{url}/{action}", {"suggestion": seen, **body})
        assert r.status_code == 409, (action, r.json_body)
        assert "a newer suggestion replaced the one you saw" in r.json_body["detail"]
    assert not ReconciliationDecision.objects.filter(observation=setup["zoom"]).exists()
    newer.refresh_from_db()
    assert newer.outcome == "pending"
    # Without the suggestion's ID the API doesn't guess.
    missing = editor.post(f"{url}/accept", {})
    assert missing.status_code == 400 and "suggestion" in missing.json_body["errors"]
    assert editor.post(f"{url}/reject", {"suggestion": "nope"}).status_code == 400
    # The one shown now is accepted as usual.
    assert editor.post(f"{url}/accept", {"suggestion": str(newer.id)}).status_code == 200
    assert ReconciliationDecision.objects.get(observation=setup["zoom"]).resource_id == setup["zoom_record"].id


def test_the_candidate_list_shows_the_suggestion(setup, fake, api):
    batch(setup)
    rows = api(setup["users"]["viewer"]).get("discovery/candidates").json_body["results"]
    zoom = next(r for r in rows if r["name"] == "Zoom")
    s = zoom["assistant_suggestion"]
    assert s["action"] == "link" and s["record"]["id"] == str(setup["zoom_record"].id) and s["confidence"] == 0.9
    assert s["current"] and s["outcome"] == "pending" and s["state"] == "ready"
    revoke_capability(setup["org"], "assistant")
    rows = api(setup["users"]["viewer"]).get("discovery/candidates").json_body["results"]
    assert all(r["assistant_suggestion"] is None for r in rows)


# ---- the API: settings, usage, batches; roles, capability, tenancy ------------------------------------------------------------


def test_settings_defaults_and_a_write_only_key(ent, api):
    admin = api(ent["users"]["admin"])
    body = admin.get("assistant/settings").json_body
    assert body["saved"] is False and body["enabled"] is False and body["monthly_cap_usd"] == "10.00"
    assert body["api_key_source"] == "none" and body["model"] == "claude-sonnet-5-5" and body["model_priced"]
    refused = admin.patch("assistant/settings", {"enabled": True})
    assert refused.status_code == 400 and "API key" in refused.json_body["errors"]["enabled"][0]
    saved = admin.patch("assistant/settings", {"enabled": True, "api_key": KEY, "monthly_cap_usd": "25.50"})
    assert saved.status_code == 200, saved.json_body
    body = saved.json_body
    assert body["api_key_set"] and body["api_key_set_on"] == timezone.now().date().isoformat()
    assert body["api_key_source"] == "organization" and body["monthly_cap_usd"] == "25.50" and body["enabled"]
    assert KEY not in saved.content.decode() and "api_key" not in {
        k for k in body if k not in ("api_key_set", "api_key_set_on", "api_key_source")
    }
    row = AssistantSettings.objects.get()
    assert row.api_key_ciphertext and KEY not in row.api_key_ciphertext
    event = AuditEvent.objects.get(action="assistant_settings.updated")
    assert event.changes["replaced"] == ["api_key"] and KEY not in json.dumps(
        [event.changes, event.prechange, event.postchange], default=str
    )


@pytest.mark.parametrize(
    "data,field",
    [
        ({"monthly_cap_usd": "0"}, "monthly_cap_usd"),
        ({"monthly_cap_usd": "-5"}, "monthly_cap_usd"),
        ({"monthly_cap_usd": "10001"}, "monthly_cap_usd"),
        ({"monthly_cap_usd": "1.005"}, "monthly_cap_usd"),
        ({"monthly_cap_usd": "ten"}, "monthly_cap_usd"),
        ({"monthly_cap_usd": True}, "monthly_cap_usd"),
        ({"enabled": "yes"}, "enabled"),
        ({"api_key": ""}, "api_key"),
        ({"api_key": "has spaces in it"}, "api_key"),
        ({"model": "claude-opus"}, "model"),
    ],
)
def test_settings_validation(ent, api, data, field):
    response = api(ent["users"]["admin"]).patch("assistant/settings", data)
    assert response.status_code == 400 and field in response.json_body["errors"]


def test_the_installation_key_needs_its_ceiling(ent, api, settings):
    settings.SOFTWARDEN_ASSISTANT_INSTALLATION_KEY = "sk-ant-installation-key-7777"
    admin = api(ent["users"]["admin"])
    body = admin.get("assistant/settings").json_body
    assert body["installation_key_available"] is False and body["api_key_source"] == "none"
    assert body["installation_cap_usd"] is None
    refused = admin.patch("assistant/settings", {"enabled": True})
    assert (
        refused.status_code == 400
        and "SOFTWARDEN_ASSISTANT_INSTALLATION_CAP_USD" in refused.json_body["errors"]["enabled"][0]
    )
    settings.SOFTWARDEN_ASSISTANT_INSTALLATION_CAP_USD = "not money"
    assert assistant.installation_cap() is None and not assistant.installation_key_available()


def test_the_installation_key_is_used_without_an_organization_key(ent, api, settings, fake):
    settings.SOFTWARDEN_ASSISTANT_INSTALLATION_KEY = "sk-ant-installation-key-7777"
    settings.SOFTWARDEN_ASSISTANT_INSTALLATION_CAP_USD = "4"
    admin = api(ent["users"]["admin"])
    body = admin.patch("assistant/settings", {"enabled": True}).json_body
    assert body["api_key_source"] == "installation" and body["installation_key_available"] and not body["api_key_set"]
    # Held to the lower of the organization's cap (10.00) and the installation's ceiling (4).
    assert body["installation_cap_usd"] == "4.00" and body["month"]["cap_usd"] == "4.00"
    assert body["monthly_cap_usd"] == "10.00"
    assert admin.patch("assistant/settings", {"monthly_cap_usd": "2"}).json_body["month"]["cap_usd"] == "2.00"
    admin.patch("assistant/settings", {"monthly_cap_usd": "10"})
    assert "sk-ant-installation-key-7777" not in json.dumps(body)
    connector = make_connector(ent["org"], ent["users"]["admin"])
    observe(connector, "Something")
    assistant.request_batch(ent["org"], ent["users"]["editor"], {})
    drain_jobs(JOB)
    assert fake.keys == ["sk-ant-installation-key-7777"]
    # The ceiling holds the installation key's calls.
    row = AssistantUsage.objects.create(organization=ent["org"], model="m", cost_usd=Decimal("3.99"), outcome="ok")
    observe(connector, "Something else")
    stopped = assistant.request_batch(ent["org"], ent["users"]["editor"], {})
    drain_jobs(JOB)
    stopped.refresh_from_db()
    assert stopped.stop_reason == "cap_reached"
    # An organization key wins, with the organization's own cap; removing it falls back again.
    admin.patch("assistant/settings", {"api_key": KEY})
    body = admin.get("assistant/settings").json_body
    assert body["api_key_source"] == "organization" and body["month"]["cap_usd"] == "10.00"
    row.delete()
    assert admin.patch("assistant/settings", {"api_key": None}).json_body["api_key_source"] == "installation"


def patch_if(client, data, tag):
    return client.client.patch(
        "/api/v1/assistant/settings", data=json.dumps(data), content_type="application/json", HTTP_IF_MATCH=tag
    )


def test_settings_etag_and_if_match(ent, api):
    admin = api(ent["users"]["admin"])
    tag = admin.get("assistant/settings")["ETag"]
    assert patch_if(admin, {"monthly_cap_usd": "20"}, tag).status_code == 200
    stale = patch_if(admin, {"monthly_cap_usd": "30"}, tag)
    assert stale.status_code == 412
    assert AssistantSettings.objects.get().monthly_cap_usd == Decimal("20.00")
    current = admin.get("assistant/settings")["ETag"]
    admin.patch("assistant/settings", {"api_key": KEY})
    assert admin.get("assistant/settings")["ETag"] != current  # a replaced key moves it (through set_at only)


def test_roles(setup, fake, api):
    users = setup["users"]
    for role in ("editor", "viewer"):
        client = api(users[role])
        assert client.get("assistant/settings").status_code == 403
        assert client.patch("assistant/settings", {"enabled": False}).status_code == 403
        assert client.get("assistant/usage").status_code == 403
        assert client.get("assistant/batches").status_code == 200
    viewer = api(users["viewer"])
    assert viewer.post("assistant/batches", {}).status_code == 403
    assert (
        viewer.post(
            f"discovery/observations/{setup['zoom'].id}/assistant/accept", {"suggestion": sid(setup["zoom"])}
        ).status_code
        == 403
    )
    assert (
        viewer.post(
            f"discovery/observations/{setup['zoom'].id}/assistant/reject", {"suggestion": sid(setup["zoom"])}
        ).status_code
        == 403
    )
    queued = api(users["editor"]).post("assistant/batches", {"connector": str(setup["connector"].id)})
    assert queued.status_code == 202 and queued["Location"].endswith(
        f"/api/v1/assistant/batches/{queued.json_body['id']}"
    )
    assert queued.json_body["connector"]["id"] == str(setup["connector"].id) and queued.json_body["status"] == "queued"
    assert api(users["viewer"]).get(f"assistant/batches/{queued.json_body['id']}").status_code == 200
    assert api(users["editor"]).post("assistant/batches", {}).status_code == 409  # one at a time
    assert AuditEvent.objects.filter(action="assistant_batch.requested").count() == 1


def test_a_batch_request_is_refused_when_the_assistant_cant_run(setup, api):
    AssistantSettings.objects.update(enabled=False)
    response = api(setup["users"]["editor"]).post("assistant/batches", {})
    assert response.status_code == 409 and "turned off" in response.json_body["detail"]
    bad = api(setup["users"]["editor"]).post("assistant/batches", {"connector": "nope", "extra": 1})
    assert bad.status_code == 400 and set(bad.json_body["errors"]) == {"connector", "extra"}


ROUTES = [
    ("admin", "get", "assistant/settings"),
    ("admin", "patch", "assistant/settings"),
    ("admin", "get", "assistant/usage"),
    ("viewer", "get", "assistant/batches"),
    ("editor", "post", "assistant/batches"),
    ("editor", "post", "discovery/observations/{obs}/assistant/accept"),
    ("editor", "post", "discovery/observations/{obs}/assistant/reject"),
]


@pytest.mark.parametrize("role,method,path", ROUTES)
def test_every_route_needs_the_capability(a, api, role, method, path):
    revoke_capability(a["org"], "assistant")
    connector = make_connector(a["org"], a["users"]["admin"])
    obs = observe(connector, "Zoom")
    client = api(a["users"][role])
    url = path.format(obs=obs.id)
    response = client.get(url) if method == "get" else getattr(client, method)(url, {})
    assert response.status_code == 403, response.json_body
    assert (
        response.json_body["type"] == "/problems/capability-required"
        and response.json_body["capability"] == "assistant"
    )


def test_usage_log_and_month_total(setup, fake, api):
    batch(setup)
    body = api(setup["users"]["admin"]).get("assistant/usage").json_body
    [row] = body["results"]
    assert (
        row["input_tokens"],
        row["output_tokens"],
        row["cache_read_input_tokens"],
        row["cache_creation_input_tokens"],
    ) == (1200, 180, 0, 0)
    assert (
        row["cost_usd"] == "0.004200"
        and row["outcome"] == "ok"
        and row["candidate_count"] == 3
        and row["model"] == "claude-sonnet-5-5"
    )
    assert body["month"]["to_date_usd"] == "0.004200" and body["month"]["cap_usd"] == "10.00"
    assert (
        body["month"]["remaining_usd"] == "9.995800" and body["month"]["cap_reached"] is False and not body["truncated"]
    )
    earlier = api(setup["users"]["admin"]).get("assistant/usage?month=2026-01").json_body
    assert earlier["results"] == [] and earlier["month"]["start"] == "2026-01-01"
    for bad in ("2026-13", "26-01", "x"):
        assert api(setup["users"]["admin"]).get(f"assistant/usage?month={bad}").status_code == 400
    assert api(setup["users"]["admin"]).get("assistant/usage?page=2").status_code == 400


def test_another_organizations_assistant_rows_dont_exist(setup, fake, api):
    batch(setup)
    other = make_org("other")
    other["org"], _ = set_edition(other["org"], "enterprise")
    outsider = api(other["users"]["admin"])
    mine = AssistantBatch.objects.get()
    assert outsider.get(f"assistant/batches/{mine.id}").status_code == 404
    assert outsider.get("assistant/batches").json_body == []
    assert outsider.get("assistant/usage").json_body["results"] == []
    assert outsider.get("assistant/settings").json_body["saved"] is False
    other_editor = api(other["users"]["editor"])
    assert (
        other_editor.post(
            f"discovery/observations/{setup['zoom'].id}/assistant/accept", {"suggestion": sid(setup["zoom"])}
        ).status_code
        == 404
    )
    assert (
        other_editor.post(
            f"discovery/observations/{setup['zoom'].id}/assistant/reject", {"suggestion": sid(setup["zoom"])}
        ).status_code
        == 404
    )
    assert suggestion(setup["zoom"]).outcome == "pending"
    # A batch for another organization's connector is refused as unknown.
    response = api(other["users"]["editor"]).post("assistant/batches", {"connector": str(setup["connector"].id)})
    assert response.status_code == 400 and "connector" in response.json_body["errors"]


def _rejected(write):
    with pytest.raises(IntegrityError), transaction.atomic():
        write()
        connection.check_constraints()


def test_the_database_keeps_suggestions_in_their_organization(setup, fake):
    batch(setup)
    other = make_org("elsewhere")
    foreign = app(other["org"], other["users"]["admin"], "Foreign")
    s = suggestion(setup["zoom"])
    _rejected(lambda: AssistantSuggestion.objects.filter(id=s.id).update(target=foreign))
    _rejected(lambda: AssistantSuggestion.objects.filter(id=s.id).update(decided_resource=foreign))
    _rejected(lambda: AssistantSuggestion.objects.filter(id=s.id).update(organization=other["org"]))
    _rejected(lambda: AssistantUsage.objects.filter(batch__isnull=False).update(organization=other["org"]))


# ---- data minimisation and the key -------------------------------------------------------------------------------------------


def test_the_request_carries_only_minimal_non_secret_data(setup, fake):
    zoom = setup["zoom"]
    zoom.data = {
        **zoom.data,
        "app_id": "APPID-MARKER-0001",
        "owners": [{"name": "Owner Marker", "email": "owner-marker@contoso.example", "external_id": "OID-MARKER"}],
        "tenant_id": "TENANT-MARKER",
        "credentials": [{"key_id": "KEYID-MARKER", "hint": "HINT-MARKER"}],
        "homepage": "https://zoom.example",
        "description": f"Video calls; someone pasted {KEY} here",
    }
    zoom.external_id = "EXTID-MARKER-9"
    zoom.save()
    now = timezone.now()
    DiscoveredCredential.objects.create(
        organization=setup["org"],
        observation=zoom,
        key_id="CREDKEY-MARKER",
        credential_type="secret",
        display_name="CREDNAME-MARKER",
        origin="app_registration",
        first_seen_at=now,
        last_seen_at=now,
    )
    batch(setup)
    sent = json.dumps([*fake.counts, *fake.calls], default=str)
    for marker in (
        "APPID-MARKER",
        "Owner Marker",
        "owner-marker@",
        "OID-MARKER",
        "TENANT-MARKER",
        "KEYID-MARKER",
        "HINT-MARKER",
        "CREDKEY-MARKER",
        "CREDNAME-MARKER",
        KEY,
        "synthetic-secret",
        str(zoom.id),
        str(setup["zoom_record"].id),
        str(setup["connector"].id),
        zoom.external_id,
    ):
        assert marker not in sent, marker
    item = next(c for c in candidates_in(fake.calls[0]) if c["name"] == "Zoom")
    assert set(item) <= {
        "candidate",
        "name",
        "kind",
        "source",
        "description",
        "publisher",
        "homepage",
        "agent",
        "enabled",
        "component_type",
        "records",
    }
    assert item["records"] == [{"record": "c1-r1", "name": "Zoom", "kind": "application", "similarity": 1.0}]
    assert item["description"].startswith("Video calls") and "[redacted]" in item["description"]


def test_the_key_never_leaves_the_server(setup, fake, api, caplog):
    caplog.set_level(logging.DEBUG)
    batch(setup)
    fake.answer = [status_error(anthropic.AuthenticationError, 401)]
    Observation.objects.filter(id=setup["hub"].id).update(fingerprint=fingerprint("new"))
    batch(setup)
    assert fake.keys == [KEY, KEY]  # it was used
    texts = []
    for role in ("admin", "editor", "viewer"):
        client = api(setup["users"][role]).client
        for path in (
            "assistant/settings",
            "assistant/usage",
            "assistant/batches",
            "discovery/candidates?status=all",
            f"discovery/observations/{setup['zoom'].id}",
            "audit-events",
            "jobs",
        ):
            response = client.get(f"/api/v1/{path}")
            texts.append(response.content.decode() + json.dumps(dict(response.items())))
    events = AuditEvent.objects.all()
    stored = {
        "responses": "\n".join(texts),
        "audit": json.dumps([[e.action, e.changes, e.prechange, e.postchange] for e in events], default=str),
        "jobs": json.dumps(list(Job.objects.values("payload", "result", "last_error")), default=str),
        "batches": json.dumps(list(AssistantBatch.objects.values()), default=str),
        "usage": json.dumps(list(AssistantUsage.objects.values()), default=str),
        "suggestions": json.dumps(list(AssistantSuggestion.objects.values()), default=str),
        "logs": caplog.text + json.dumps([r.__dict__ for r in caplog.records], default=str),
    }
    ciphertext = AssistantSettings.objects.get().api_key_ciphertext[:40]
    for where, text in stored.items():
        assert KEY not in text, where
        assert ciphertext not in text, where
    assert all(set(j.payload) == {"batch_id"} for j in Job.objects.filter(kind=JOB))


def test_a_job_payload_can_never_carry_the_key():
    from core import jobs

    with pytest.raises(ValueError, match="never carry secrets"):
        jobs.check_payload({"batch_id": "x", "api_key": KEY})


# ---- review fixes: lapsed batches, abandoned calls, mid-batch decisions, follow-ups, what is sent ----------------------


def test_a_lapsed_batch_is_failed_by_the_sweep_and_on_the_next_queue_never_by_a_read(setup, fake, api):
    made = assistant.request_batch(setup["org"], setup["users"]["editor"], {})
    Job.objects.filter(id=str(made.job_id)).update(status=JobStatus.ABANDONED)
    viewer = api(setup["users"]["viewer"])
    assert viewer.get(f"assistant/batches/{made.id}").json_body["status"] == "queued"  # a read writes nothing
    assert viewer.get("assistant/batches").status_code == 200
    assert not AuditEvent.objects.filter(action="assistant_batch.finished", target_id=made.id).exists()
    assert assistant.scheduled_sweep(timezone.now())["assistant_batches_lapsed"] == 1
    body = viewer.get(f"assistant/batches/{made.id}").json_body
    assert (body["status"], body["stop_reason"]) == ("failed", "lapsed")
    assert AuditEvent.objects.filter(action="assistant_batch.finished", target_id=made.id).exists()
    other = assistant.request_batch(setup["org"], setup["users"]["editor"], {})
    Job.objects.filter(id=str(other.job_id)).update(status=JobStatus.FAILED)
    assert assistant.scheduled_sweep(timezone.now()) == {"assistant_batches_lapsed": 1, "assistant_calls_abandoned": 0}
    other.refresh_from_db()
    assert other.stop_reason == "lapsed"
    from core import jobs

    assert jobs.SCHEDULES["discovery.assistant"].func is assistant.scheduled_sweep
    assert jobs.JOB_KINDS[JOB].lease == assistant.JOB_LEASE >= timedelta(minutes=20)


def test_a_reservation_still_pending_after_the_lease_is_abandoned_and_keeps_its_cost(setup):
    old = AssistantUsage.objects.create(
        organization=setup["org"], model="m", cost_usd=Decimal("0.5"), outcome="pending"
    )
    fresh = AssistantUsage.objects.create(
        organization=setup["org"], model="m", cost_usd=Decimal("0.5"), outcome="pending"
    )
    AssistantUsage.objects.filter(id=old.id).update(
        created_at=timezone.now() - assistant.JOB_LEASE - timedelta(minutes=1)
    )
    assert assistant.abandon_pending() == 1
    old.refresh_from_db()
    fresh.refresh_from_db()
    assert (old.outcome, old.cost_usd) == ("abandoned", Decimal("0.500000")) and fresh.outcome == "pending"
    assert assistant.month_to_date(setup["org"]) == Decimal("1")


def test_the_lease_is_renewed_between_counting_and_the_call(setup, fake, monkeypatch):
    from core import jobs

    events: list[str] = []
    real_count, real_renew = fake.count_tokens, jobs.renew

    def count(**kwargs):
        events.append("count")
        return real_count(**kwargs)

    def renew(job, now=None):
        events.append("renew")
        return real_renew(job, now)

    monkeypatch.setattr(fake, "count_tokens", count)
    monkeypatch.setattr(jobs, "renew", renew)
    made = assistant.request_batch(setup["org"], setup["users"]["editor"], {})
    jobs.run(jobs.claim(ids=[made.job_id]))
    assert events == ["renew", "count", "renew"]  # before the chunk, then between count_tokens and create


def test_a_candidate_decided_while_the_call_was_out_gets_no_suggestion(setup, fake):
    def decide_then_answer(kwargs):
        discovery.dismiss(setup["org"], setup["users"]["editor"], setup["zoom"], {"reason": "Decided meanwhile"})
        return sensible(kwargs)

    fake.answer = decide_then_answer
    done = batch(setup)
    assert suggestion(setup["zoom"]) is None and done.counts["skipped"] == 1 and done.counts["suggested"] == 2


def test_a_batch_with_candidates_left_queues_a_follow_up(setup, fake, monkeypatch):
    monkeypatch.setattr(assistant, "BATCH_MAX", 2)
    first = assistant.request_batch(setup["org"], setup["users"]["editor"], {})
    drain_jobs(JOB)  # the follow-up is a job too: drained in the same pass
    batches = list(AssistantBatch.objects.order_by("queued_at"))
    assert [b.trigger for b in batches] == ["manual", "follow_up"] and batches[0].id == first.id
    assert batches[0].counts["remaining"] == 1 and batches[1].counts["remaining"] == 0
    assert all(b.status == "succeeded" for b in batches)
    assert {suggestion(o).state for o in (setup["zoom"], setup["hub"], setup["test"])} == {"ready"}


def test_no_follow_up_without_progress(setup, fake, monkeypatch):
    monkeypatch.setattr(assistant, "BATCH_MAX", 2)
    fake.answer = lambda kwargs: reply(stop="max_tokens")
    batch(setup)
    assert AssistantBatch.objects.count() == 1  # nothing was suggested: a failing model never loops


def test_no_follow_up_when_the_cap_wouldnt_allow_a_call(setup, fake, monkeypatch):
    monkeypatch.setattr(assistant, "BATCH_MAX", 2)
    AssistantSettings.objects.update(monthly_cap_usd=Decimal("0.03"))
    fake.answer = lambda kwargs: reply(sensible_rows(kwargs), usage=(1000, 900, 0, 0))  # costs 0.011
    done = batch(setup)  # reserved 0.0236 <= 0.03; then 0.011 + a call's least worst case (0.02098) > 0.03
    assert done.status == "succeeded" and done.counts["remaining"] == 1
    assert AssistantBatch.objects.count() == 1


# ---- discovery-run checks left for the assistant work ---------------------------------------------------------------------


def test_turning_a_connector_off_finishes_its_queued_job_in_the_same_transaction(a):
    org, admin = a["org"], a["users"]["admin"]
    connector = make_connector(org, admin)
    queued = discovery.request_run(org, admin, connector)
    with pytest.raises(RuntimeError), transaction.atomic():  # rolled back together
        discovery.update_connector(org, admin, connector, {"enabled": False})
        raise RuntimeError
    assert Job.objects.get(id=queued.job_id).status == JobStatus.QUEUED
    assert ConnectorRun.objects.get(id=queued.id).status == RunStatus.QUEUED
    discovery.update_connector(org, admin, connector, {"enabled": False})
    job = Job.objects.get(id=queued.job_id)
    assert (job.status, job.result) == (JobStatus.SUCCEEDED, {"skipped": "connector_off"}) and job.finished_at
    assert ConnectorRun.objects.get(id=queued.id).error == discovery.TURNED_OFF
    assert drain_jobs() == []


def test_a_scheduled_run_the_worker_stopped_is_queued_again_the_same_day(a):
    org, admin = a["org"], a["users"]["admin"]
    connector = make_connector(org, admin, schedule="daily")
    at_three = datetime(2026, 11, 3, 3, 0, tzinfo=UTC)
    assert discovery.due_runs(at_three) == 1
    run = ConnectorRun.objects.get(connector=connector)
    ConnectorRun.objects.filter(id=run.id).update(
        status=RunStatus.FAILED, error=discovery.WORKER_STOPPED, finished_at=at_three
    )
    Job.objects.filter(id=str(run.job_id)).update(status=JobStatus.SUCCEEDED)
    assert discovery.due_runs(at_three + timedelta(minutes=5)) == 1  # the same day
    again = ConnectorRun.objects.filter(connector=connector).latest("created_at")
    assert again.trigger == "scheduled" and again.status == RunStatus.QUEUED
    # Any other failure waits for the next day.
    ConnectorRun.objects.filter(id=again.id).update(
        status=RunStatus.FAILED, error="Graph said no.", finished_at=at_three
    )
    Job.objects.filter(id=str(again.job_id)).update(status=JobStatus.SUCCEEDED)
    assert discovery.due_runs(at_three + timedelta(minutes=10)) == 0
    assert discovery.due_runs(at_three + timedelta(days=1)) == 1


# ---- the Playwright fixture (api/e2e_views.assistant_org) -----------------------------------------------
# Routed only for Playwright's _e2e database; this module's URLconf routes it for the tests below. The e2e server's
# fake client (installed by e2e_views.install_hooks) answers only the assistant spec organization's own key.


def _urlpatterns():
    from django.urls import include, path

    from api import e2e_views

    return [path("api/v1/e2e/assistant-org", e2e_views.assistant_org), path("api/v1/", include("api.urls"))]


urlpatterns = _urlpatterns()


def test_importing_the_e2e_views_or_the_urls_without_the_flag_installs_no_fake_client(monkeypatch):
    """Only install_hooks() installs the fake, from the gated block in api/urls.py (SOFTWARDEN_E2E=1 and an _e2e
    database). conftest replaces make_client for every test, so the production factory is put back first and the
    modules are imported again: each import must leave it, and the channels' post, as they were."""
    import importlib

    from api import e2e_views
    from api import urls as api_urls
    from notify import channels

    monkeypatch.setattr(assistant, "make_client", REAL_MAKE_CLIENT)
    post = channels.post_https
    importlib.reload(e2e_views)
    assert assistant.make_client is REAL_MAKE_CLIENT and channels.post_https is post
    monkeypatch.delenv("SOFTWARDEN_E2E", raising=False)
    importlib.reload(api_urls)
    assert assistant.make_client is REAL_MAKE_CLIENT and channels.post_https is post
    assert not any("e2e/" in str(getattr(p, "pattern", "")) for p in api_urls.urlpatterns)
    # The hook itself does install it (what the e2e server runs).
    monkeypatch.setattr(channels, "post_https", post)
    e2e_views.install_hooks()
    try:
        assert assistant.make_client is e2e_views._e2e_make_client
    finally:
        channels.post_https = post


@pytest.mark.urls("tests.test_assistant")
def test_the_e2e_fixture_runs_batches_through_the_fake_client_for_its_own_organization_only(a, api, monkeypatch):
    from django.contrib.auth import get_user_model

    from api import e2e_views

    monkeypatch.setattr(assistant, "make_client", e2e_views._e2e_make_client)
    made = api(a["users"]["admin"]).post("e2e/assistant-org", {"setup": True})
    assert made.status_code == 200, made.json_body
    users = get_user_model().objects
    admin = api(users.get(username=made.json_body["accounts"]["admin"]))
    editor = api(users.get(username=made.json_body["accounts"]["editor"]))
    assert admin.get("assistant/settings").json_body["enabled"] is False  # the capability, off until an Admin saves
    assert admin.patch("assistant/settings", {"api_key": "sk-ant-e2e-fake-1", "enabled": True}).status_code == 200
    assert editor.post("assistant/batches", {}).status_code == 202
    assert admin.post("e2e/assistant-org", {"run": True}).status_code == 200
    [done] = editor.get("assistant/batches").json_body
    assert done["status"] == "succeeded" and done["counts"]["suggested"] == 4 and done["counts"]["failed"] == 1
    rows = {o["name"]: o["assistant_suggestion"] for o in editor.get("discovery/candidates").json_body["results"]}
    assert (rows["Zoom"]["action"], rows["Zoom"]["record"]["name"]) == ("link", "Zoom")
    assert rows["Expense Hub"]["action"] == "create" and rows["Test registration 7"]["action"] == "dismiss"
    assert rows["Mystery Widget"]["state"] == "failed" and "wasn't offered" in rows["Mystery Widget"]["detail"]
    # A source change puts a suggestion out of date; a call carrying a "Declined ..." candidate is refused whole.
    assert admin.post("e2e/assistant-org", {"change": ["Old Notes App"]}).json_body == {"changed": 1}
    rows = {o["name"]: o["assistant_suggestion"] for o in editor.get("discovery/candidates").json_body["results"]}
    assert rows["Old Notes App"]["current"] is False and rows["Old Notes App"]["outcome"] == "pending"
    admin.post("e2e/assistant-org", {"observe": ["Declined Thing"]})
    editor.post("assistant/batches", {})
    admin.post("e2e/assistant-org", {"run": True})
    rows = {o["name"]: o["assistant_suggestion"] for o in editor.get("discovery/candidates").json_body["results"]}
    assert rows["Declined Thing"]["state"] == "refused" and "refusal: e2e" in rows["Declined Thing"]["detail"]
    # The cap: 0.01 USD is below this call and the next worst case, so a new candidate's batch stops at it.
    assert admin.patch("assistant/settings", {"monthly_cap_usd": "0.01"}).status_code == 200
    admin.post("e2e/assistant-org", {"observe": ["Later Tool"]})
    editor.post("assistant/batches", {})
    admin.post("e2e/assistant-org", {"run": True})
    assert editor.get("assistant/batches").json_body[0]["stop_reason"] == "cap_reached"
    assert admin.get("assistant/settings").json_body["month"]["cap_reached"] is True
    # A rerun starts again: the assistant off, no batches or calls.
    api(a["users"]["admin"]).post("e2e/assistant-org", {"setup": True})
    assert admin.get("assistant/settings").json_body["saved"] is False
    assert admin.get("assistant/usage").json_body["results"] == []
    # The community organization: a candidate, no capability.
    community = api(a["users"]["admin"]).post("e2e/assistant-org", {"community": True}).json_body
    viewer = api(users.get(username=community["accounts"]["viewer"]))
    [row] = viewer.get("discovery/candidates").json_body["results"]
    assert row["name"] == "Zoom" and row["assistant_suggestion"] is None
    assert viewer.get("assistant/batches").status_code == 403
    # Only that organization's Admin observes or runs, and the fixture is Admin only.
    assert (
        api(users.get(username=community["accounts"]["admin"])).post("e2e/assistant-org", {"run": True}).status_code
        == 403
    )
    assert api(a["users"]["admin"]).post("e2e/assistant-org", {"run": True}).status_code == 403
    assert editor.post("e2e/assistant-org", {"run": True}).status_code == 403


def test_the_e2e_fake_client_refuses_any_other_key(a):
    from api import e2e_views

    e2e_views.make_assistant_org()
    with pytest.raises(RuntimeError, match="never calls the Anthropic API"):
        e2e_views._e2e_make_client(KEY)  # no key stored for the spec organization yet
    a["org"], _ = set_edition(a["org"], "enterprise")
    enable(a["org"], a["users"]["admin"])  # another organization's own key
    with pytest.raises(RuntimeError):
        e2e_views._e2e_make_client(KEY)
