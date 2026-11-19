"""The demo organization's dependency scenario: every case the dependency screens show is in it, and a second run
changes nothing."""

import io

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from catalog.management.commands.seed_demo import SAMPLE_CSV
from catalog.models import AIModel, AIProvider, Integration, Relationship, Resource, SavedFilter, Tag
from core.demo import DEMO_PASSWORD, DEMO_USERNAMES
from core.models import Organization
from discovery import engine as discovery
from discovery.models import Connector, Observation, ReconciliationDecision
from imports import importer as imports
from imports.models import ImportRun, ImportSource

pytestmark = pytest.mark.django_db


def seed(**kw):
    call_command("seed_demo", stdout=io.StringIO(), **kw)
    return Organization.objects.get(name="Contoso (synthetic)")


def client(api, email):
    return api(get_user_model().objects.get(username=email))


def test_accounts_only_makes_no_records():
    org = seed(accounts_only=True)
    assert not Resource.objects.filter(organization=org).exists()


def test_accounts_only_makes_a_platform_staff_account_on_an_e2e_database(monkeypatch):
    """`platform@example.test` (demo password) signs in to the console only, made on a Playwright (_e2e)
    database only, and granted once: a rerun leaves an operator's revocation alone."""
    from catalog import platform_admin
    from catalog.management.commands import seed_demo
    from core.models import Membership, PlatformAuditEvent, PlatformStaff

    email = seed_demo.PLATFORM_STAFF[0]
    out = io.StringIO()
    call_command("seed_demo", accounts_only=True, stdout=out)  # the test database's name doesn't end in _e2e
    assert not get_user_model().objects.filter(username=email).exists()
    assert f"Skipped {email}" in out.getvalue()

    monkeypatch.setattr(seed_demo, "e2e_database", lambda: True)
    seed(accounts_only=True)
    seed(accounts_only=True)
    user = get_user_model().objects.get(username=email)
    assert user.email == "platform@example.test" and user.is_active and user.check_password(DEMO_PASSWORD)
    assert PlatformStaff.objects.filter(user=user).exists()
    assert not Membership.objects.filter(user=user).exists()
    assert PlatformAuditEvent.objects.filter(action="staff.granted").count() == 1  # granted once, audited once
    assert not PlatformStaff.objects.filter(user__username__in=[u[0] for u in seed_demo.USERS]).exists()
    # An operator revokes it: a rerun doesn't grant it back.
    platform_admin.revoke_staff(user)
    seed(accounts_only=True)
    assert not PlatformStaff.objects.filter(user=user).exists()


def test_scenario_has_every_case_and_a_second_run_changes_nothing(api):
    org = seed()
    count = Resource.objects.filter(organization=org).count()
    # Eight links between the dependency records, and two AI agents depending on records.
    assert Relationship.objects.filter(organization=org).count() == 10 and Integration.objects.count() == 2
    seed()
    assert Resource.objects.filter(organization=org).count() == count
    # The full demo, which may run on a shared demo server, makes no platform staff account (it would reach every org).
    assert not get_user_model().objects.filter(username="platform@example.test").exists()
    viewer = client(api, "viewer@example.test")
    by_name = {r.name: r for r in Resource.objects.filter(organization=org)}

    # A chain through a component, a cycle, and a retired dependent (left out until asked for).
    workday = by_name["Workday HR"]
    body = viewer.get(f"dependencies?target={workday.id}&depth=all&include_retired=true").json_body
    depth = {n["name"]: n["depth"] for n in body["nodes"] if not n["cycle"]}
    assert depth["Nightly Payroll Export"] == 1 and depth["Finance Ledger"] == 2 and depth["Executive Dashboard"] == 3
    assert by_name["Nightly Payroll Export"].kind == "component"
    assert body["summary"]["cycles"] == 1 and any(n["cycle"] for n in body["nodes"])
    assert "Legacy Timesheets" in depth
    hidden = viewer.get(f"dependencies?target={workday.id}&depth=all").json_body
    assert "Legacy Timesheets" not in {n["name"] for n in hidden["nodes"]}

    # An integration with an implementer and a vendor-native one.
    flows = {f["name"]: f for f in body["data_flows"]}
    assert [c["name"] for c in flows["Workday to Finance Ledger payroll feed"]["implemented_by"]] == [
        "Nightly Payroll Export"
    ]
    assert flows["Workday to Finance Ledger payroll feed"]["vendor_native"] is False
    assert flows["Workday to Zoom user provisioning"]["vendor_native"] is True

    # Link states: confirmed, overdue and changed (needs verification), and never confirmed.
    states = {r["link_state"] for r in viewer.get("relationships").json_body}
    assert states == {"confirmed", "needs_verification", "unconfirmed"}

    # An assessment made stale by an owner change, and a current one; Needs attention has something to show.
    listed = {a["title"]: a for a in viewer.get("assessments?status=draft,in_review").json_body}
    assert listed["Workday 2027R1 upgrade"]["stale"] and not listed["Retire Budget Planner"]["stale"]
    attention = client(api, "editor@example.test").get("needs-attention?scope=all").json_body
    assert attention["links_to_verify"] and attention["stale_assessments"]
    assert any(r["name"] == "Employee Timesheets" for r in attention["ownership_gaps"])


def test_discovery_scenario_has_decisions_and_candidates_and_is_not_repeated():
    org = seed()
    connector = Connector.objects.get(organization=org)
    assert connector.reviewer is not None
    assert connector.name == "Microsoft Entra ID" and connector.reviewer.username == "editor@example.test"
    assert connector.runs.filter(status="succeeded").count() == 1
    decisions = {
        d.observation.display_name: d
        for d in ReconciliationDecision.objects.filter(organization=org).select_related("observation", "resource")
    }
    assert set(decisions) == {"Workday", "Zoom", "Graph Explorer Test"}
    workday, zoom = decisions["Workday"].resource, decisions["Zoom"].resource
    assert workday is not None and zoom is not None
    assert workday.name == "Workday HR" and zoom.name == "Zoom Meetings"
    assert decisions["Graph Explorer Test"].decision == "dismissed"
    assert decisions["Graph Explorer Test"].reason == "A developer's test registration"
    assert Observation.objects.filter(connector=connector).filter(discovery.status_q("candidates")).exists()
    seed()
    assert Connector.objects.filter(organization=org).count() == 1
    assert ReconciliationDecision.objects.filter(organization=org).count() == 3


def test_accounts_only_makes_no_connector():
    org = seed(accounts_only=True)
    assert not Connector.objects.filter(organization=org).exists()


def test_imports_scenario_has_source_tags_and_shared_filter_and_is_not_repeated():
    org = seed()
    source = ImportSource.objects.get(organization=org)
    assert source.name == "IT spreadsheet" and source.kind == "application"
    assert ImportRun.objects.filter(organization=org).count() == 0
    tagged = {r.name: sorted(t.name for t in r.tags.all()) for r in Resource.objects.filter(organization=org)}
    assert tagged["Finance Ledger"] == ["Finance"] and tagged["Zoom Meetings"] == ["Collaboration"]
    assert tagged["Workday HR"] == ["Finance", "HR"] and tagged["Employee Timesheets"] == ["HR"]
    shared = SavedFilter.objects.get(organization=org, name="Finance apps")
    assert shared.shared and shared.kind == "application" and shared.query == {"tag": "Finance"}
    assert shared.user is not None and shared.user.username == "editor@example.test"  # the owner
    assert shared.created_by is not None and shared.created_by.username == "editor@example.test"
    seed()
    assert ImportSource.objects.filter(organization=org).count() == 1
    assert SavedFilter.objects.filter(organization=org).count() == 1


def test_accounts_only_makes_no_import_source_tags_or_filter():
    org = seed(accounts_only=True)
    assert not ImportSource.objects.filter(organization=org).exists()
    assert not SavedFilter.objects.filter(organization=org).exists()
    assert not Tag.objects.filter(organization=org).exists()


def test_sample_csv_previews_with_the_documented_outcomes():
    org = seed()
    source = ImportSource.objects.get(organization=org)
    editor = get_user_model().objects.get(username="editor@example.test")
    run = imports.create_run(org, editor, "application", "applications.csv", SAMPLE_CSV.read_bytes(), source=source)
    assert run.status == "previewed"
    rows = {r.raw["name"]: r for r in run.rows.all()}
    assert len(rows) == 8
    assert run.counts["error"] == 1 and run.counts["create"] == 7
    assert [e["column"] for e in rows["Time Clock Kiosk"].errors if e["level"] == "error"] == ["lifecycle"]
    assert rows["Budget Planner"].possible_match.name == "Budget Planner"
    panels = run.decisions["derived"]["panels"]
    assert [m["candidate"]["name"] for m in panels["matches"]] == ["Budget Planner"]
    assert [p["email"] for p in panels["people"]] == ["facilities.lead@example.test"]
    assert [t["name"] for t in panels["teams"]] == ["Procurement"]
    assert run.counts["new_keys"] == 7 and run.counts["pending_decisions"] == 3


def test_ai_agents_scenario(api):
    """Providers and models, a complete Copilot Studio agent with models, channels and owners, and two
    agents that show under AI agent gaps; a second run adds nothing."""
    org = seed()
    seed()
    viewer = client(api, "viewer@example.test")
    assert AIProvider.objects.filter(organization=org).count() == 3
    assert AIModel.objects.filter(organization=org).count() == 4
    agents = {a["name"]: a for a in viewer.get("ai-agents").json_body["results"]}
    assert set(agents) == {"HR Policy Assistant", "Invoice Triage Agent", "Service Desk Helper"}
    hr = agents["HR Policy Assistant"]
    assert hr["platform"] == "copilot_studio" and hr["channels"] == ["teams", "m365_copilot"]
    assert [m["name"] for m in hr["models_used"]] == ["GPT-4o", "text-embedding-3-large"]
    assert hr["business_owner"]["name"] == "Priya Raman" and hr["review_state"] == "confirmed"
    gaps = {r["name"]: r["gaps"] for r in viewer.get("needs-attention?scope=all").json_body["agent_gaps"]}
    assert gaps == {"Invoice Triage Agent": ["No human-in-loop decision"], "Service Desk Helper": ["No owner"]}
    in_use = viewer.get("ai-models/in-use").json_body
    assert {m["name"]: m["agents"] for m in in_use} == {
        "GPT-4o": 1,
        "text-embedding-3-large": 1,
        "Claude Sonnet": 1,
        "GPT-4o mini": 1,
    }


def test_custom_fields_scenario(api):
    """Five custom fields, none required, with "Business case" on the HR agent; a second run adds nothing."""
    from catalog.models import CustomField

    org = seed()
    seed()
    assert CustomField.objects.filter(organization=org).count() == 5
    assert not CustomField.objects.filter(organization=org, required=True).exists()
    viewer = client(api, "viewer@example.test")
    agents = {a["name"]: a for a in viewer.get("ai-agents").json_body["results"]}
    assert agents["HR Policy Assistant"]["custom_field_data"]["business_case"].startswith("Answers routine HR")
    workday = viewer.get("applications?cf_sox_scope=true").json_body["results"]
    assert {r["name"] for r in workday} == {"Workday HR", "Finance Ledger"}
    assert viewer.get("applications?cf_certificate_expiry__lte=2027-12-31").json_body["results"][0]["name"] == (
        "Workday HR"
    )


def test_data_quality_scenario(api):
    """Two data-quality exceptions on records the demo leaves without a vendor and without documentation; a
    second run adds none, and the summary shows them excepted."""
    from catalog.models import QualityException

    org = seed()
    seed()
    made = QualityException.objects.filter(organization=org).values_list("rule", "resource__name")
    assert sorted(made) == [("documentation", "Executive Dashboard"), ("vendor", "Budget Planner")]
    viewer = client(api, "viewer@example.test")
    summary = viewer.get("data-quality/summary").json_body
    assert summary["excepted"] == 2 and summary["records"] > 0 and summary["score"] is not None
    assert len(viewer.get("data-quality/exceptions").json_body["results"]) == 2


def test_contracts_scenario_feeds_the_dashboard_and_reports_and_is_not_repeated(api):
    """Imports package: a fresh demo has contracts, lines and licence pools, so spend, deadlines and licence
    utilisation all have rows; a second run adds none."""
    from money.models import Contract, ContractItem, LicensePool

    org = seed()
    assert Contract.objects.filter(organization=org).count() == 5
    assert LicensePool.objects.filter(organization=org).count() == 2
    assert ContractItem.objects.filter(organization=org).count() == 8
    platform = Contract.objects.get(organization=org, name="Finance Ledger platform")
    assert platform.renewal_obligation is not None and str(platform.annualized_cost) == "90000.00"
    seed()
    assert Contract.objects.filter(organization=org).count() == 5
    viewer = client(api, "viewer@example.test")
    spend = viewer.get("reports/spend?by=vendor").json_body
    assert {t["currency"] for t in spend["totals"]} == {"EUR", "USD"}
    usd = next(t for t in spend["totals"] if t["currency"] == "USD")
    # 120,000 + 1,500 x 12 + 270,000 / 3 live; the signed analytics term is a future commitment.
    assert (usd["annualized"], usd["future"]) == ("228000.00", "18000.00")
    assert any(r["gap"] == "no_vendor" for r in spend["rows"])
    dashboard = client(api, "admin@example.test").get("dashboard").json_body
    names = {r["name"] for r in dashboard["deadlines"]["rows"]}
    assert {"Zoom Business", "ServiceDesk Plus license"} <= names


def test_a_rerun_never_resets_an_existing_accounts_password_or_membership():
    """New demo accounts get the demo password; one that already exists keeps whatever password it has, and
    an existing membership keeps its role and must-change-password flag."""
    from catalog.management.commands.seed_demo import USERS
    from core.models import Membership, Role

    org = seed(accounts_only=True)
    User = get_user_model()
    admin = User.objects.get(username=USERS[0][0])
    assert admin.check_password(DEMO_PASSWORD)
    admin.set_password("the-admins-own-long-password")
    admin.save()
    seed(accounts_only=True)
    seed()
    admin.refresh_from_db()
    assert admin.check_password("the-admins-own-long-password") and not admin.check_password(DEMO_PASSWORD)
    assert User.objects.get(username=USERS[1][0]).check_password(DEMO_PASSWORD)  # untouched, still the demo one
    # An Admin demotes the demo Admin and makes the Editor change their password: a rerun leaves both alone.
    Membership.objects.filter(user=admin, organization=org).update(role=Role.VIEWER)
    editor = User.objects.get(username=USERS[1][0])
    Membership.objects.filter(user=editor, organization=org).update(must_change_password=True)
    seed(accounts_only=True)
    assert Membership.objects.get(user=admin, organization=org).role == Role.VIEWER
    assert Membership.objects.get(user=editor, organization=org).must_change_password is True
    # A demo account without a membership (removed from the organization) gets one back, with its demo role.
    Membership.objects.filter(user=editor, organization=org).delete()
    seed(accounts_only=True)
    assert Membership.objects.get(user=editor, organization=org).role == Role.EDITOR


def test_the_demo_password_check_knows_every_account_seed_demo_makes():
    """core/demo.py checks seed_demo's accounts by name: the two lists stay in step."""
    from catalog.management.commands.seed_demo import PLATFORM_STAFF, USERS

    assert set(DEMO_USERNAMES) == {u[0] for u in USERS} | {PLATFORM_STAFF[0]}
