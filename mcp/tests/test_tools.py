"""Each tool against the fake Softwarden API: what it asks for, the compact text a model reads and the structured
data alongside it; and the guards that run before any request (IDs, revisions)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from conftest import (
    APP_ID,
    CANDIDATE,
    CANDIDATE_ID,
    GPT,
    GPT_ID,
    OPENAI_ID,
    REMINDER_ID,
    REVISION,
    FakeSoftwarden,
)

from softwarden_mcp import tools
from softwarden_mcp.client import ApiError, SoftwardenClient, SoftwardenError, api_path


def asked(fake: FakeSoftwarden, path: str) -> list[dict[str, str]]:
    return [s.params for s in fake.seen if s.path == path]


# ---- renewals with owners -----------------------------------------------------------------------------


def test_renewals_ask_softwarden_to_filter_on_the_term_end(client: SoftwardenClient, fake: FakeSoftwarden) -> None:
    answer = tools.renewals_with_owners(client)  # within_days=90
    assert asked(fake, "composite/renewals") == [{"within_days": "90", "limit": "50", "basis": "term_end"}]
    rows = answer.data["results"]
    # Softwarden's term_end order: Lapsed (ended 6 days ago, action still open), Entered late (ends in 30 days; its
    # entered deadline is 200 days out, so a deadline window would miss it), Atlassian (ends in 60). Long notice ends in
    # 120 and Domain has no term end: not in this view.
    assert [r["name"] for r in rows] == ["Lapsed", "Entered late", "Atlassian Cloud"]
    assert answer.data["date_basis"] == "term_end" and answer.data["until"] == "2027-02-05"
    assert answer.data["count"] == 3 and answer.data["shown"] == 3 and answer.data["truncated"] is False
    atlassian = rows[2]
    assert atlassian["renewal_owner"] == {"name": "Priya Shah", "email": "priya@example.com"}
    assert atlassian["business_owners"] == ["Dana Ortiz"] and atlassian["covers"] == ["Jira"]
    assert atlassian["days_to_term_end"] == 60 and atlassian["days_to_deadline"] == 30
    assert atlassian["vendor_contact"] == {"name": "Dana Ortiz"}
    late = rows[1]
    assert late["source"] == "renewal" and late["action_deadline"] == "2027-05-26"
    text = answer.text
    assert text.startswith("3 renewals with a term ending by 2027-02-05 (the next 90 days from 2026-11-07) or already ")
    assert "filtered on the term end" in text and "no term end recorded aren't in this view" in text
    assert '- "Atlassian Cloud" (contract, vendor "Atlassian"); term ends 2027-01-06 (in 60 days)' in text
    assert 'owner: "Priya Shah" <priya@example.com> (renewal owner)' in text
    assert 'owner: "Dana Ortiz" (contract owner)' in text  # Lapsed: no renewal owner, the contract owner is named
    assert 'IT follow-up: "Sam Lee" <sam@example.com>' in text
    assert 'covers: "Jira"' in text and "gaps: no_it_steward" in text


def test_the_headline_says_already_ended_only_when_a_term_has(client: SoftwardenClient, fake: FakeSoftwarden) -> None:
    answer = tools.renewals_with_owners(client, within_days=90)
    assert "or already ended with the action still open" in answer.text
    answer_of = fake.gets["composite/renewals"]

    def without_lapsed(params: dict[str, str]) -> dict[str, Any]:
        found = answer_of(params)
        rows = [r for r in found["results"] if r["name"] != "Lapsed"]
        return {**found, "count": len(rows), "results": rows}

    fake.gets["composite/renewals"] = without_lapsed
    fresh = tools.renewals_with_owners(client, within_days=90)
    assert fresh.text.startswith("2 renewals with a term ending by 2027-02-05 (the next 90 days from 2026-11-07), ")
    assert "already ended" not in fresh.text


def test_renewals_by_action_deadline(client: SoftwardenClient, fake: FakeSoftwarden) -> None:
    answer = tools.renewals_with_owners(client, within_days=90, date_basis="action_deadline")
    assert asked(fake, "composite/renewals")[-1]["basis"] == "action_deadline"
    names = [r["name"] for r in answer.data["results"]]
    assert names == ["Lapsed", "Domain", "Atlassian Cloud", "Long notice"]  # soonest deadline first, as the API
    assert "filtered on the action deadline" in answer.text
    domain = next(r for r in answer.data["results"] if r["name"] == "Domain")
    assert domain["overdue"] is True and "no_renewal_owner" in domain["gaps"]
    assert '- "Domain" (renewal); no term end recorded; decide by 2026-11-01 (6 days ago) OVERDUE' in answer.text
    assert "owner: none named" in answer.text
    long_notice = next(r for r in answer.data["results"] if r["name"] == "Long notice")
    assert long_notice["contract_owner"] == {"name": "Former person", "inactive": True}


def test_a_truncated_answer_gives_softwardens_full_count(client: SoftwardenClient, fake: FakeSoftwarden) -> None:
    answer = tools.renewals_with_owners(client, within_days=5000, limit=2, date_basis="action_deadline")
    assert asked(fake, "composite/renewals")[0] == {"within_days": "730", "limit": "2", "basis": "action_deadline"}
    assert answer.data["count"] == 5 and answer.data["shown"] == 2 and answer.data["truncated"] is True
    assert "Softwarden counts 5 in all; only the first 2 are shown (overdue ones come first and count toward" in (
        answer.text
    )
    by_term = tools.renewals_with_owners(client, limit=1)
    assert "(ended terms come first and count toward that limit)" in by_term.text


# ---- the other read tools ---------------------------------------------------------------------------------------------


def test_search(client: SoftwardenClient, fake: FakeSoftwarden) -> None:
    answer = tools.search(client, "jira", kinds=["application", "vendor"], limit=5)
    assert asked(fake, "composite/search") == [{"q": "jira", "kinds": "application,vendor", "limit": "5"}]
    assert answer.data["results"] == [
        {
            "kind": "application",
            "id": APP_ID,
            "name": "Jira",
            "lifecycle": "active",
            "matched": "Issue tracking",
            "path": f"applications/{APP_ID}",
        }
    ]
    assert f'- application "Jira" (id {APP_ID}, active), matched "Issue tracking"' in answer.text


def test_get_item_returns_the_revision_and_drops_empty_fields(client: SoftwardenClient, fake: FakeSoftwarden) -> None:
    answer = tools.get_item(client, "application", APP_ID.upper())
    assert fake.seen[-1].path == f"applications/{APP_ID}"
    assert answer.data["revision"] == REVISION
    item = answer.data["item"]
    assert item["business_owner"] == {"id": "p-2", "name": "Dana Ortiz"}
    assert "technical_owner" not in item and "tags" not in item and "criticality" not in item
    assert answer.text.startswith(f'application "Jira" (id {APP_ID}), revision {REVISION}')
    with pytest.raises(SoftwardenError, match="Unknown kind"):
        tools.get_item(client, "api_token", APP_ID)


def test_dependencies(client: SoftwardenClient, fake: FakeSoftwarden) -> None:
    answer = tools.dependencies(client, APP_ID, depth=3)
    assert asked(fake, "composite/dependencies") == [
        {"target": APP_ID, "direction": "both", "depth": "3", "limit": "50"}
    ]
    assert answer.data["depends_on"][0] == {
        "kind": "application",
        "id": "n-1",
        "name": "Okta",
        "hops": 1,
        "via": "Depends on",
        "lifecycle": "active",
        "link_state": "confirmed",
    }
    assert 'Depends on: "Okta" (application, 1 hop, via "Depends on")' in answer.text
    assert 'Depended on by: "Triage bot" (ai_agent, 2 hops, via "Depends on", through "CRM")' in answer.text
    assert 'Data flows: "Jira to Slack" ("Jira" -> "Slack")' in answer.text


def test_spend_names_gaps_as_groups(client: SoftwardenClient, fake: FakeSoftwarden) -> None:
    answer = tools.spend(client, by="department", groups=3)
    assert asked(fake, "composite/spend") == [{"by": "department", "groups": "3"}]
    assert "Total 1000.00 USD across 2 contracts" in answer.text
    assert '- "Microsoft": 800.00 USD (1 contracts)' in answer.text
    assert "- gap no_vendor: 200.00 USD (1 contracts)" in answer.text
    assert answer.data["groups"][1] == {"label": "no_vendor", "gap": "no_vendor", "currency": "USD", "amount": "200.00",
                                        "contracts": 1}  # fmt: skip


def test_expiring_credentials(client: SoftwardenClient, fake: FakeSoftwarden) -> None:
    answer = tools.expiring_credentials(client, within_days=14, kind="discovered_credential")
    assert asked(fake, "composite/expiring-credentials") == [
        {"within_days": "14", "kind": "discovered_credential", "limit": "50"}
    ]
    row = answer.data["results"][0]
    assert row["application"] == "Salesforce SSO" and row["technical_owner"]["name"] == "Priya Shah"
    assert 'secret "Graph sync secret" on app "Salesforce SSO" expires 2026-11-20 (in 13 days)' in answer.text


def test_data_gaps(client: SoftwardenClient, fake: FakeSoftwarden) -> None:
    answer = tools.data_gaps(client, rule="business_owner", kind="application", limit=1)
    assert asked(fake, "composite/data-gaps") == [{"rule": "business_owner", "kind": "application", "limit": "1"}]
    assert "Data quality score 82 (64 of 120 records complete" in answer.text
    assert "No business owner: 9" in answer.text and "Showing 1 of 9 records" in answer.text
    assert answer.data["results"][0]["failing"] == ["No business owner"]
    assert f'- application "Jira" (id {APP_ID}): No business owner' in answer.text


def test_agents_by_model_summary_model_and_provider(client: SoftwardenClient, fake: FakeSoftwarden) -> None:
    summary = tools.agents_by_model(client)
    assert '"gpt-4o 2024-08" ("OpenAI"): 3 agents, 2 live' in summary.text
    fake.gets["ai-agents"] = {"results": [{"id": "a-1", "name": "Triage bot", "lifecycle": "active",
                                           "platform": "copilot_studio", "models_used": [], "business_owner":
                                           {"id": "p-2", "name": "Dana Ortiz"}}], "next": None}  # fmt: skip
    by_model = tools.agents_by_model(client, model="gpt")
    assert asked(fake, "ai-models") == [{"q": "gpt"}]
    assert asked(fake, "ai-agents")[-1] == {"page_size": "50", "model": GPT_ID}
    assert by_model.data["groups"][0]["model"] == "gpt-4o 2024-08"
    assert '"gpt-4o 2024-08" ("OpenAI"): 1 agents' in by_model.text
    assert '- "Triage bot" (id a-1, active, copilot_studio, owner "Dana Ortiz")' in by_model.text
    by_provider = tools.agents_by_model(client, provider="openai")
    assert asked(fake, "ai-agents")[-1] == {"page_size": "50", "provider": OPENAI_ID}
    assert by_provider.data["groups"][0]["provider"] == "OpenAI"
    none = tools.agents_by_model(client, provider="anthropic")
    assert none.data == {"matched": 0, "groups": []} and "No AI providers match 'anthropic'" in none.text


def test_a_model_with_no_provider_says_none_not_none(client: SoftwardenClient, fake: FakeSoftwarden) -> None:
    # A model without a provider was printed as '"gpt-4o 2024-08" ("None")'.
    fake.gets["ai-models/in-use"] = [{**GPT, "provider": None, "agents": 1, "live_agents": 1}]
    assert '- "gpt-4o 2024-08": 1 agents, 1 live' in tools.agents_by_model(client).text
    fake.gets["ai-models"] = [{**GPT, "provider": None}]
    fake.gets["ai-agents"] = {"results": [], "next": None}
    answer = tools.agents_by_model(client, model="gpt")
    assert '"gpt-4o 2024-08": 0 agents' in answer.text and "None" not in answer.text


def test_model_facing_text_uses_american_spelling() -> None:
    # The instructions and tool descriptions the model reads, in the product's spelling.
    import inspect

    from softwarden_mcp import server

    text = inspect.getsource(server) + inspect.getsource(tools)
    for word in ("licence pool", "Annualised", "cost centre"):
        assert word not in text, word


def test_agents_follow_pages_up_to_the_limit(client: SoftwardenClient, fake: FakeSoftwarden) -> None:
    pages = {None: {"results": [{"id": "a-1", "name": "One"}], "next": "c2"},
             "c2": {"results": [{"id": "a-2", "name": "Two"}], "next": "c3"}}  # fmt: skip
    fake.gets["ai-agents"] = lambda params: pages[params.get("cursor")]
    answer = tools.agents_by_model(client, model="gpt", limit=2)
    assert [a["name"] for a in answer.data["groups"][0]["agents"]] == ["One", "Two"]
    assert [p.get("cursor") for p in asked(fake, "ai-agents")] == [None, "c2"]


def test_discovery_and_reminders(client: SoftwardenClient, fake: FakeSoftwarden) -> None:
    summary = tools.discovery_summary(client)
    assert "3 candidates to decide (2 new, 1 changed), 1 linked records no longer seen" in summary.text
    assert '- "Entra" (entra): 3 candidates; last run succeeded' in summary.text
    reminders = tools.list_reminders(client)
    assert asked(fake, "reminders") == [{"page_size": "25", "status": "open,snoozed", "ordering": "due_date"}]
    assert f'"Cancellation deadline": "Atlassian Cloud" due 2026-12-07 [open] (id {REMINDER_ID})' in reminders.text


def test_candidates_follow_the_cursor_and_say_when_there_are_more(
    client: SoftwardenClient, fake: FakeSoftwarden
) -> None:
    one = tools.list_candidates(client, q_="zo")
    assert asked(fake, "discovery/candidates") == [{"status": "candidates", "q": "zo"}]
    assert f'- "Zoom" (enterprise_app, from "Entra") [new] (id {CANDIDATE_ID}); looks like "Zoom Workplace"' in one.text
    assert one.data["more"] is False and "More candidates" not in one.text

    def page(params: dict[str, str]) -> dict[str, Any]:
        # The keyset: after names the previous page's last row; the offset next is never followed.
        start = int(params["after"].removeprefix("k-")) + 1 if "after" in params else 0
        rows = [{**CANDIDATE, "id": f"c-{i}", "name": f"App {i}"} for i in range(start, min(start + 50, 120))]
        more = start + 50 < 120
        return {"status": "candidates", "counts": {"new": 120}, "results": rows, "next": "999" if more else None,
                "next_after": f"k-{start + 49}" if more else None}  # fmt: skip

    fake.gets["discovery/candidates"] = page
    paged = tools.list_candidates(client, limit=60)
    assert [p.get("after") for p in asked(fake, "discovery/candidates")[1:]] == [None, "k-49"]
    assert all("cursor" not in p for p in asked(fake, "discovery/candidates"))
    assert len(paged.data["results"]) == 60 and paged.data["more"] is True
    assert "More candidates than the 60 shown" in paged.text
    every = tools.list_candidates(client, limit=200)
    assert len(every.data["results"]) == 120 and every.data["more"] is False


# ---- write tools ------------------------------------------------------------------------------------------------------


def test_writes_send_if_match_and_describe_the_effect(writer: SoftwardenClient, fake: FakeSoftwarden) -> None:
    changed = tools.update_record(writer, "application", APP_ID, "lifecycle", "deprecated", REVISION)
    sent = fake.seen[-1]
    assert (sent.method, sent.path, sent.body) == ("PATCH", f"applications/{APP_ID}", {"lifecycle": "deprecated"})
    assert sent.headers["if-match"] == f'"{REVISION}"'
    assert changed.text == (
        'Changed lifecycle of application "Jira" to "deprecated". New revision: rev3. Audited in its History.'
    )
    cleared = tools.update_record(writer, "application", APP_ID, "technical_owner", "", f'"{REVISION}"')
    assert fake.seen[-1].body == {"technical_owner": None}  # an empty value clears an optional reference
    assert cleared.data["value"] is None
    tools.update_record(writer, "application", APP_ID, "it_steward", "7", REVISION)
    assert fake.seen[-1].body == {"it_steward": 7}
    snoozed = tools.snooze_reminder(writer, REMINDER_ID, "2026-12-01", REVISION)
    assert fake.seen[-1].path == f"reminders/{REMINDER_ID}/snooze"
    assert fake.seen[-1].body == {"until": "2026-12-01"} and fake.seen[-1].headers["if-match"] == '"rev1"'
    assert 'Snoozed "Cancellation deadline" for "Atlassian Cloud" through 2026-12-01' in snoozed.text
    acked = tools.acknowledge_reminder(writer, REMINDER_ID, REVISION)
    assert acked.data["status"] == "acknowledged" and fake.seen[-1].path.endswith("/acknowledge")
    dismissed = tools.dismiss_candidate(writer, CANDIDATE_ID, "  test registration ", REVISION)
    assert fake.seen[-1].path == f"discovery/observations/{CANDIDATE_ID}/dismiss"
    assert fake.seen[-1].body == {"reason": "test registration"} and fake.seen[-1].headers["if-match"] == '"rev1"'
    assert 'Dismissed the discovery candidate "Zoom" (enterprise_app) with the reason "test registration"' in (
        dismissed.text
    )


def test_writes_refuse_bad_input_before_calling(writer: SoftwardenClient, fake: FakeSoftwarden) -> None:
    calls = len(fake.seen)
    with pytest.raises(SoftwardenError, match="revision get_item returned"):
        tools.update_record(writer, "application", APP_ID, "lifecycle", "active", "  ")
    with pytest.raises(SoftwardenError, match="can't be changed here"):
        tools.update_record(writer, "application", APP_ID, "name", "x", REVISION)  # type: ignore[arg-type]
    with pytest.raises(SoftwardenError, match="numeric ID"):
        tools.update_record(writer, "application", APP_ID, "it_steward", "sam", REVISION)
    with pytest.raises(SoftwardenError, match="YYYY-MM-DD"):
        tools.snooze_reminder(writer, REMINDER_ID, "next week", REVISION)
    with pytest.raises(SoftwardenError, match="Say why"):
        tools.dismiss_candidate(writer, CANDIDATE_ID, " ", REVISION)
    assert len(fake.seen) == calls


ATTACKS = [
    f"../applications/{APP_ID}/archive?",
    f"../api-tokens/{APP_ID}/revoke?",
    f"{APP_ID}/../../api-tokens",
    f"{APP_ID}?x=1",
    f"{APP_ID}#frag",
    "..",
    "",
    "not-an-id",
]


@pytest.mark.parametrize("bad", ATTACKS)
def test_ids_that_are_not_uuids_are_refused_before_any_request(
    writer: SoftwardenClient, fake: FakeSoftwarden, bad: str
) -> None:
    calls: list[Callable[[], object]] = [
        lambda: tools.get_item(writer, "reminder", bad),
        lambda: tools.dependencies(writer, bad),
        lambda: tools.update_record(writer, "application", bad, "lifecycle", "deprecated", REVISION),
        lambda: tools.snooze_reminder(writer, bad, "2026-12-01", REVISION),
        lambda: tools.acknowledge_reminder(writer, bad, REVISION),
        lambda: tools.dismiss_candidate(writer, bad, "reason", REVISION),
    ]
    for call in calls:
        with pytest.raises(SoftwardenError, match="must be a Softwarden ID"):
            call()
    assert fake.seen == []


def test_the_client_quotes_every_value_segment_and_refuses_odd_routes() -> None:
    assert api_path(("reminders", "a/b?c#d", "snooze")) == "reminders/a%2Fb%3Fc%23d/snooze"
    assert api_path("composite/renewals") == "composite/renewals"
    for route in ("../api-tokens", "composite/renewals?x", "Composite", "a//b", "/abs"):
        with pytest.raises(SoftwardenError, match="unexpected API route"):
            api_path(route)
    for parts in (("reminders", ".."), ("reminders", ""), ("reminders", ".")):
        with pytest.raises(SoftwardenError, match="dot segment"):
            api_path(parts)


@pytest.mark.parametrize("bad", ["*", '"*"', "rev1,rev2", 'W/"rev1"', "rev 1", "a" * 65, "rev1\r\nX-Evil: 1"])
def test_a_revision_that_could_widen_if_match_is_refused(
    writer: SoftwardenClient, fake: FakeSoftwarden, bad: str
) -> None:
    with pytest.raises(SoftwardenError, match="revision get_item returned"):
        tools.update_record(writer, "application", APP_ID, "lifecycle", "deprecated", bad)
    with pytest.raises(SoftwardenError, match="revision get_item returned"):
        writer.write("POST", ("reminders", REMINDER_ID, "acknowledge"), {}, revision=bad)
    assert fake.seen == []


def test_a_stale_revision_changes_nothing_and_says_what_to_do(writer: SoftwardenClient) -> None:
    with pytest.raises(ApiError) as stale:
        tools.update_record(writer, "application", APP_ID, "lifecycle", "deprecated", "old")
    assert stale.value.status == 412
    assert "changed since it was read, so nothing was changed." in str(stale.value)
    assert "rev2" not in str(stale.value)  # the new revision comes only from reading the item again
    assert "Read it again with get_item" in str(stale.value)


def test_a_read_only_token_is_refused_with_a_clear_message(client: SoftwardenClient) -> None:
    with pytest.raises(ApiError) as refused:
        tools.snooze_reminder(client, REMINDER_ID, "2026-12-01", REVISION)
    assert refused.value.problem == "read-only-token"
    assert str(refused.value).startswith("This API token is read-only, so Softwarden refused the change")
