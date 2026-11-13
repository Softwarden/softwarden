"""The MCP acceptance check end to end: "which contracts renew in the next 90 days and who owns them", asked through the MCP
server's renewals_with_owners tool (mcp/softwarden_mcp/tools.py) against a real API server (Django's live server
thread on a free loopback port, real HTTP, the test database) with a read-only token, as Claude Desktop would.

The MCP package lives at the repository's mcp/ with its own environment; only its `client` and `tools` modules are
imported here, and they need nothing but httpx2, which the API's environment already has (through anthropic). The
MCP protocol layer itself (stdio and HTTP sessions with the SDK's client) is tested in mcp/tests."""

import importlib
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from api import tokens
from catalog.models import Vendor
from core.models import Membership
from money import renewals
from tests.test_reports import contract, iso, line, record, today

MCP = Path(__file__).resolve().parents[3] / "mcp"

pytestmark = pytest.mark.skipif(not (MCP / "softwarden_mcp" / "tools.py").exists(), reason="needs the repo's mcp/")


@pytest.fixture
def mcp():
    """softwarden_mcp.client and .tools, imported from mcp/ for this test only."""
    sys.path.insert(0, str(MCP))
    try:
        client = importlib.import_module("softwarden_mcp.client")
        tools = importlib.import_module("softwarden_mcp.tools")
    finally:
        sys.path.remove(str(MCP))
    return client, tools


@pytest.fixture
def api_server(transactional_db):
    """Django's LiveServerThread without the static-files wrapper (there is no STATIC_URL; the SPA is WhiteNoise's),
    so committed rows are visible to it. Stopped after the test."""
    from django.test.testcases import LiveServerThread

    # In place of the static-files handler class: the app itself answers every path.
    thread = LiveServerThread("127.0.0.1", cast(Any, lambda app: app), port=0)
    thread.daemon = True
    thread.start()
    thread.is_ready.wait()
    if thread.error:
        raise thread.error
    yield f"http://127.0.0.1:{thread.port}"
    thread.terminate()


@pytest.fixture
def renewing(a):
    """A contract whose term ends in 60 days with a renewal owner and a covered application with a business owner,
    one whose term ends in 120 days but must be cancelled within 60 (in the API's deadline window, not the term-end
    one), a renewal (not a contract) whose renewal date is in 45 days with its responsible person and IT follow-up,
    one whose entered deadline (200 days out) is after its renewal date (30 days), and one far away."""
    org, people = a["org"], a["people"]
    atlassian = Vendor.objects.create(organization=org, name="Atlassian", account_manager=people["dana"])
    jira = record(a, "Jira", business_owner=people["dana"])
    soon = contract(a, "Atlassian Cloud", vendor=str(atlassian.id), end_date=iso(a, 60), notice_value=30,
                    notice_unit="days", auto_renew="yes", amount="38000.00",
                    renewal_owner=str(people["priya"].id))  # fmt: skip
    line(a, soon, jira)
    contract(a, "Long notice", end_date=iso(a, 120), notice_value=60, notice_unit="days", auto_renew="yes",
             renewal_owner=str(people["dana"].id))  # fmt: skip
    contract(a, "Far away", end_date=iso(a, 400), notice_value=30, notice_unit="days", auto_renew="yes")
    editor = a["users"]["editor"]
    renewals.create_renewal(org, editor, {"name": "Domain names", "renewal_date": iso(a, 45), "auto_renew": "yes",
                                          "notice_value": 10, "notice_unit": "days",
                                          "responsible": str(people["dana"].id), "followup": editor.id})  # fmt: skip
    renewals.create_renewal(org, editor, {"name": "Entered late", "renewal_date": iso(a, 30), "auto_renew": "yes",
                                          "explicit_deadline": iso(a, 200),
                                          "responsible": str(people["priya"].id)})  # fmt: skip
    return soon


def read_only_secret(a):
    owner = Membership.objects.get(organization=a["org"], user=a["users"]["viewer"])
    _, secret = tokens.create_token(
        a["org"], a["users"]["admin"], owner, "Claude Desktop", True, today(a) + timedelta(days=30)
    )
    return secret


def test_the_mcp_answers_what_renews_in_90_days_and_who_owns_it(a, renewing, api_server, mcp):
    client_module, tools = mcp
    client = client_module.SoftwardenClient(api_server, read_only_secret(a))
    try:
        answer = tools.renewals_with_owners(client, within_days=90)
    finally:
        client.close()
    rows = {r["name"]: r for r in answer.data["results"]}
    # Softwarden filtered on the term end: the entered-late renewal is in although its deadline is 200 days out.
    assert list(rows) == ["Entered late", "Domain names", "Atlassian Cloud"], answer.text
    assert answer.data["count"] == 3 and answer.data["date_basis"] == "term_end"
    domain = rows["Domain names"]
    assert domain["source"] == "renewal" and domain["term_ends"] == iso(a, 45)
    assert domain["renewal_owner"]["name"] == "Dana Ortiz" and domain["follow_up"]["name"]
    late = rows["Entered late"]
    assert late["action_deadline"] == iso(a, 200) and late["renewal_owner"]["name"] == "Priya Raman"
    row = rows["Atlassian Cloud"]
    assert row["source"] == "contract" and row["term_ends"] == iso(a, 60) and row["days_to_term_end"] == 60
    assert row["renewal_owner"]["name"] == "Priya Raman"
    assert row["contract_owner"]["name"] == "Priya Raman"
    assert row["business_owners"] == ["Dana Ortiz"] and row["covers"] == ["Jira"]
    assert row["vendor"] == "Atlassian" and row["annualized_cost"] == "38000.00"
    # The text a model reads names the contract, its end, its owner, and what the filter left out.
    assert "Atlassian Cloud" in answer.text and "Priya Raman" in answer.text and iso(a, 60) in answer.text
    assert "filtered on the term end" in answer.text
    assert "Far away" not in answer.text and "Long notice" not in answer.text


def test_the_action_deadline_basis_and_a_refused_write_with_a_read_only_token(a, renewing, api_server, mcp):
    client_module, tools = mcp
    client = client_module.SoftwardenClient(api_server, read_only_secret(a))
    try:
        by_deadline = tools.renewals_with_owners(client, within_days=90, date_basis="action_deadline")
        assert {r["name"] for r in by_deadline.data["results"]} == {"Atlassian Cloud", "Long notice", "Domain names"}
        item = tools.get_item(client, "application", str(a["app"].id))
        assert item.data["revision"]
        with pytest.raises(client_module.ApiError) as refused:
            tools.update_record(
                client, "application", str(a["app"].id), "lifecycle", "deprecated", item.data["revision"]
            )
    finally:
        client.close()
    assert refused.value.status == 403 and refused.value.problem == "read-only-token"
    assert "read-only" in str(refused.value) and "nothing was changed" in str(refused.value)
    a["app"].refresh_from_db()
    assert a["app"].lifecycle == "active"


def test_a_bad_token_reads_as_a_bad_token(a, api_server, mcp):
    client_module, tools = mcp
    client = client_module.SoftwardenClient(api_server, "sw_" + "x" * 40)
    try:
        with pytest.raises(client_module.ApiError) as refused:
            tools.renewals_with_owners(client)
    finally:
        client.close()
    assert refused.value.status == 401 and "API token" in str(refused.value)


def server_tools() -> set[str]:
    """The tool functions mcp/softwarden_mcp/server.py uses: every lowercase `tools.<name>` in its source (types and
    constants are capitalized; `tools.py` is its docstring naming the module)."""
    import re

    source = (MCP / "softwarden_mcp" / "server.py").read_text(encoding="utf-8")
    return set(re.findall(r"\btools\.([a-z][a-z_]*)\b", source)) - {"py"}


def test_every_tool_runs_against_the_real_api(a, renewing, api_server, mcp):
    """Each of the server's tools called once against a real API server, not the fake in mcp/tests: the reads
    with a read-only token, the writes (snooze, acknowledge, dismiss, update) with a read-write one, each on a
    revision get_item returned. A route or shape the API changed fails here."""
    from discovery import engine as discovery
    from discovery.models import Observation
    from notify import reminders
    from tests.test_discovery import make_connector

    client_module, tools = mcp
    org = a["org"]
    reminders.generate(org, today(a))
    discovery.run(make_connector(org, a["users"]["admin"]))
    assert Observation.objects.filter(connector__organization=org).exists()
    editor = Membership.objects.get(organization=org, user=a["users"]["editor"])
    _, writer_secret = tokens.create_token(
        org, a["users"]["admin"], editor, "MCP writes", False, today(a) + timedelta(days=30)
    )
    called: set[str] = set()

    def call(name, client, *args, **kwargs):
        called.add(name)
        answer = getattr(tools, name)(client, *args, **kwargs)
        assert isinstance(answer.text, str) and answer.text and isinstance(answer.data, dict), name
        return answer

    reader = client_module.SoftwardenClient(api_server, read_only_secret(a))
    writer = client_module.SoftwardenClient(api_server, writer_secret)
    try:
        found = call("search", reader, "Jira")
        assert any(r["name"] == "Jira" for r in found.data["results"]), found.text
        call("get_item", reader, "application", str(a["app"].id))
        call("renewals_with_owners", reader, within_days=90)
        call("dependencies", reader, str(a["app"].id))
        call("spend", reader, by="vendor")
        call("expiring_credentials", reader, within_days=365)
        call("data_gaps", reader)
        call("agents_by_model", reader)
        call("agents_by_model", reader, model="gpt")
        call("agents_by_model", reader, provider="Anthropic")
        call("discovery_summary", reader)
        listed = call("list_reminders", reader, status="open,snoozed", limit=10)
        assert len(listed.data["results"]) >= 2, listed.text
        candidates = call("list_candidates", reader, limit=5)
        assert candidates.data["results"], candidates.text

        # Writes, each on the revision get_item gave.
        first, second = (r["id"] for r in listed.data["results"][:2])
        until = (today(a) + timedelta(days=10)).isoformat()
        revision = tools.get_item(writer, "reminder", first).data["revision"]
        snoozed = call("snooze_reminder", writer, first, until, revision)
        assert snoozed.data["snoozed_until"] == until, snoozed.text
        revision = tools.get_item(writer, "reminder", second).data["revision"]
        acknowledged = call("acknowledge_reminder", writer, second, revision)
        assert acknowledged.data["id"] == second, acknowledged.text
        candidate = candidates.data["results"][0]["id"]
        revision = tools.get_item(writer, "candidate", candidate).data["revision"]
        dismissed = call("dismiss_candidate", writer, candidate, "test registration", revision)
        assert dismissed.data["status"] == "dismissed", dismissed.text
        revision = tools.get_item(writer, "application", str(a["app"].id)).data["revision"]
        updated = call("update_record", writer, "application", str(a["app"].id), "description", "From MCP", revision)
        assert updated.data["value"] == "From MCP", updated.text
    finally:
        reader.close()
        writer.close()
    a["app"].refresh_from_db()
    assert a["app"].description == "From MCP"
    assert server_tools() and called == server_tools(), server_tools() ^ called


def test_every_reminder_status_list_reminders_describes_is_one_the_api_takes(a, api_server, mcp):
    """The tool's description names tools.REMINDER_STATUSES; each, alone and all together, is a status the
    reminder list accepts (an unknown one is a 400, as `resolved` was)."""
    client_module, tools = mcp
    client = client_module.SoftwardenClient(api_server, read_only_secret(a))
    try:
        for status in [*tools.REMINDER_STATUSES, ",".join(tools.REMINDER_STATUSES)]:
            answer = tools.list_reminders(client, status=status)
            assert answer.data["status"] == status
        with pytest.raises(client_module.ApiError) as refused:
            tools.list_reminders(client, status="resolved")
    finally:
        client.close()
    assert refused.value.status == 400 and "status" in str(refused.value)
