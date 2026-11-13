"""The Entra sources on the simulated tenant: the Entra source (enterprise apps and app registrations merged by
application ID, owners, credentials, first-party skip, agent detection), DiscoveredCredential feeding reminders and
the dashboard, the Entra licences source, access checks, throttling, and that no secret or token leaks."""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction

from catalog.models import Resource
from core import connections, graph
from core.dates import org_today
from core.models import AuditEvent, UserIdentity
from core.problems import FieldErrors
from discovery import access, entra, entra_simulator
from discovery import engine as discovery
from discovery.models import (
    Connector,
    ConnectorRun,
    DiscoveredCredential,
    Observation,
    ObservationKind,
    ReconciliationDecision,
    RunStatus,
)
from notify import reminders
from notify.models import Reminder, ReminderStatus

SECRET = "synthetic-secret"


def make_connector(org, actor, **extra):
    data = {"tenant_id": "simulated", "client_id": "", "secret": SECRET, **extra}
    connector = discovery.create_connector(org, actor, data)
    connections.run_test(org, actor, connector.microsoft_connection)
    return Connector.objects.get(pk=connector.pk)


def configure(connector, **config):
    connector.config = {**connector.config, **config}
    connector.save(update_fields=["config"])
    return connector


def change(connector, *changes):
    return configure(connector, simulator_changes=[*connector.config.get("simulator_changes", []), *changes])


def obs(connector, name):
    return Observation.objects.get(connector=connector, display_name=name)


def utc_today():
    return datetime.now(UTC).date()


def creds(connector, name, live=True):
    qs = DiscoveredCredential.objects.filter(observation__connector=connector, observation__display_name=name)
    return list(qs.filter(removed_at__isnull=True) if live else qs)


@pytest.fixture
def entra_connector(a):
    return make_connector(a["org"], a["users"]["admin"])


@pytest.fixture
def no_sleep(monkeypatch):
    """A fake clock: graph.sleep advances it, so budgets are tested without waiting."""
    clock: dict[str, Any] = {"now": 1000.0, "slept": []}

    def sleep(seconds):
        clock["slept"].append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(graph, "sleep", sleep)
    monkeypatch.setattr(graph, "monotonic", lambda: clock["now"])
    return clock


# ---- listing ---------------------------------------------------------------------------------------------------------


def test_a_run_lists_enterprise_apps_and_registrations_with_credential_expiry(entra_connector):
    run = discovery.run(entra_connector)
    assert run.status == RunStatus.SUCCEEDED, run.error
    # 15 merged applications; the same counts as before registrations were read (merged by application ID).
    assert run.counts["seen"] == 15 and run.counts["kept"] == 9
    assert run.counts["skipped"] == {"not_application": 1, "microsoft": 4, "disabled": 1}
    ledger = obs(entra_connector, "Finance Ledger SSO")
    assert ledger.external_id == entra_simulator._id("app/Finance Ledger SSO")  # keyed by application (client) ID
    assert ledger.data["registration"] and ledger.data["enterprise_app"]
    workday = obs(entra_connector, "Workday")
    assert workday.data["enterprise_app"] and not workday.data["registration"]  # a gallery app: no registration here
    [secret] = creds(entra_connector, "Finance Ledger SSO")
    assert (secret.credential_type, secret.display_name, secret.origin) == (
        "secret",
        "Ledger API secret",
        "app_registration",
    )
    assert secret.expires_on == utc_today() + timedelta(days=20)
    assert secret.object_id == entra_simulator._id("application/Finance Ledger SSO")
    # A certificate-backed credential is listed as a certificate and a secret with one keyId: stored once.
    [cert] = creds(entra_connector, "Workday")
    assert cert.credential_type == "certificate" and cert.origin == "enterprise_app"
    assert {c.credential_type for c in creds(entra_connector, "Payroll Sync Daemon")} == {"secret", "certificate"}
    assert creds(entra_connector, "Graph Explorer Test")[0].expires_on < utc_today()  # expired, still listed
    # Skipped apps leave no credentials behind (Microsoft Teams has a certificate in the tenant).
    assert not DiscoveredCredential.objects.filter(observation__display_name="Microsoft Teams").exists()
    assert run.counts["credentials"] == DiscoveredCredential.objects.count() == 7
    # Never the hint or key material.
    stored = json.dumps(list(DiscoveredCredential.objects.values()), default=str)
    assert '"s3c"' not in stored and "hint" not in stored


def test_a_registration_without_an_enterprise_app_is_listed(entra_connector):
    change(entra_connector, {"op": "add_registration", "id": "Build Pipeline", "value": "Build Pipeline"})
    run = discovery.run(entra_connector)
    assert run.counts["seen"] == 16 and run.counts["new"] == 10
    built = obs(entra_connector, "Build Pipeline")
    assert built.data["registration"] and not built.data["enterprise_app"] and built.data["type"] == "Application"


def test_first_party_apps_are_skipped_by_default_and_counted(a, entra_connector):
    run = discovery.run(entra_connector)
    assert run.counts["skipped"]["microsoft"] == 4
    assert not Observation.objects.filter(connector=entra_connector, display_name="Microsoft Graph").exists()
    assert sorted(entra_connector.scope.get("first_party_tenants")) == sorted(entra.FIRST_PARTY_TENANTS)
    # The list is configurable: with only another tenant, Microsoft's apps are candidates.
    discovery.update_connector(
        a["org"], a["users"]["admin"], entra_connector, {"scope": {"first_party_tenants": [entra_simulator.OUR_TENANT]}}
    )
    entra_connector.refresh_from_db()
    run = discovery.run(entra_connector)
    assert obs(entra_connector, "Microsoft Graph").gone_at is None
    # Our own apps now count as first party: they're skipped, and the earlier ones are out of scope, not gone (the
    # tenant still has them; reconciliation).
    assert run.counts["gone"] == 0 and run.counts["skipped"]["microsoft"] == 7 and run.counts["out_of_scope"] == 6
    with pytest.raises(FieldErrors) as e:
        discovery.update_connector(
            a["org"], a["users"]["admin"], entra_connector, {"scope": {"first_party_tenants": ["not-a-guid"]}}
        )
    assert "scope.first_party_tenants" in e.value.errors


def test_a_second_run_reports_unchanged(entra_connector):
    discovery.run(entra_connector)
    second = discovery.run(entra_connector)
    assert (second.counts["new"], second.counts["changed"], second.counts["gone"]) == (0, 0, 0)
    assert second.counts["unchanged"] == 9 and second.counts["credentials_removed"] == 0
    assert DiscoveredCredential.objects.count() == 7


def test_renaming_an_app_gives_a_changed_candidate(a, entra_connector):
    discovery.run(entra_connector)
    discovery.dismiss(a["org"], a["users"]["editor"], obs(entra_connector, "Contract Vault"), {"reason": "Legal"})
    call_command("simulate_entra_change", "rename", "Contract Vault", "Legal Vault")
    entra_connector.refresh_from_db()
    run = discovery.run(entra_connector)
    assert run.counts["changed"] == 1 and run.counts["new"] == 0
    renamed = obs(entra_connector, "Legal Vault")
    assert discovery.status_of(renamed) == "changed"


def test_deleting_an_app_gives_no_longer_seen_and_removes_its_credentials(entra_connector):
    discovery.run(entra_connector)
    call_command("simulate_entra_change", "remove", "Payroll Sync Daemon")
    entra_connector.refresh_from_db()
    run = discovery.run(entra_connector)
    assert run.counts["gone"] == 1 and run.counts["credentials_removed"] == 2
    payroll = obs(entra_connector, "Payroll Sync Daemon")
    assert discovery.status_of(payroll) == "gone"
    assert all(c.removed_at is not None for c in creds(entra_connector, "Payroll Sync Daemon", live=False))
    # It comes back: the observation and its credentials are live again, the same rows.
    configure(entra_connector, simulator_changes=[])
    run = discovery.run(entra_connector)
    assert run.counts["changed"] == 1 and obs(entra_connector, "Payroll Sync Daemon").gone_at is None
    assert len(creds(entra_connector, "Payroll Sync Daemon")) == 2 and DiscoveredCredential.objects.count() == 7


def test_a_removed_credential_is_marked_and_a_new_one_appears(entra_connector):
    discovery.run(entra_connector)
    change(
        entra_connector,
        {"op": "remove_credentials", "id": "Finance Ledger SSO"},
        {"op": "secret", "id": "Finance Ledger SSO", "value": {"name": "Rotated", "days": 365}},
    )
    run = discovery.run(entra_connector)
    assert run.counts["credentials_removed"] == 1
    assert [c.display_name for c in creds(entra_connector, "Finance Ledger SSO")] == ["Rotated"]
    assert run.counts["changed"] == 0  # credentials aren't the observation's data


# ---- agents ----------------------------------------------------------------------------------------------------------


def test_copilot_studio_agents_and_agent_identities_become_ai_agent_candidates(a, entra_connector):
    discovery.run(entra_connector)
    agent = obs(entra_connector, "HR Helpdesk Agent")
    assert agent.kind == ObservationKind.AI_AGENT and agent.ai_agent and agent.data["agent"] == "copilot_studio"
    assert discovery.status_of(agent) == "new"
    # Agent identities are read only when the scope asks.
    assert not Observation.objects.filter(display_name="Benefits Agent").exists()
    discovery.update_connector(
        a["org"], a["users"]["admin"], entra_connector, {"scope": {"include_agent_identities": True}}
    )
    entra_connector.refresh_from_db()
    run = discovery.run(entra_connector)
    assert run.status == RunStatus.SUCCEEDED and run.counts["new"] == 1 and "notes" not in run.counts
    identity = obs(entra_connector, "Benefits Agent")
    assert identity.kind == ObservationKind.AI_AGENT and identity.data["agent"] == "agent_identity"
    assert identity.external_id == entra_simulator._id("agent/Benefits Agent")  # keyed by object ID
    # Any tagged service principal is an agent too.
    call_command("simulate_entra_change", "tag", "Expense Hub", "AgenticApp")
    entra_connector.refresh_from_db()
    run = discovery.run(entra_connector)
    assert obs(entra_connector, "Expense Hub").kind == ObservationKind.AI_AGENT
    # Created from the queue, an agent candidate becomes an AI agent record.
    _, record = discovery.create_record(a["org"], a["users"]["editor"], agent, {})
    assert record.kind == "ai_agent" and record.ai_agent.platform == "copilot_studio"
    assert record.ai_agent.external_id == agent.data["app_id"]


def test_agent_identities_without_their_permission_are_a_note_not_a_failure(a, entra_connector):
    configure(entra_connector, simulated_permissions=["Application.Read.All", "User.ReadBasic.All"])
    discovery.update_connector(
        a["org"], a["users"]["admin"], entra_connector, {"scope": {"include_agent_identities": True}}
    )
    entra_connector.refresh_from_db()
    run = discovery.run(entra_connector)
    assert run.status == RunStatus.SUCCEEDED and run.counts["kept"] == 9
    assert any("AgentIdentity.Read.All" in n for n in run.counts["notes"])


# ---- owners ----------------------------------------------------------------------------------------------------------


def test_owners_resolved_with_user_read(a, entra_connector):
    discovery.run(entra_connector)
    workday = obs(entra_connector, "Workday")
    owners = discovery.suggested_owners(a["org"], [workday])[workday.id]
    assert {o["name"] for o in owners} == {"Dana Ortiz", "Priya Raman"} and all(o["resolved"] for o in owners)


def test_owners_without_user_read_are_matched_by_object_id(a, entra_connector):
    configure(entra_connector, simulated_permissions=["Application.Read.All"])
    run = discovery.run(entra_connector)
    assert any("owner not resolved" in n for n in run.counts["notes"])
    zoom = obs(entra_connector, "Zoom")
    [owner] = discovery.suggested_owners(a["org"], [zoom])[zoom.id]
    assert owner == {
        "name": "",
        "email": "",
        "external_id": entra_simulator.DANA["id"],
        "resolved": False,
        "person": None,
    }
    # Dana's user signs in with Entra under that object ID: her Person is the match.
    dana = a["people"]["dana"]
    dana.user = a["users"]["viewer"]
    dana.save()
    UserIdentity.objects.create(
        user=a["users"]["viewer"], issuer="https://login.microsoftonline.com/t/v2.0", subject=entra_simulator.DANA["id"]
    )
    [owner] = discovery.suggested_owners(a["org"], [zoom])[zoom.id]
    assert owner["person"]["id"] == str(dana.id) and not owner["resolved"]
    # Granting User.ReadBasic.All later fills names without bringing dismissed candidates back.
    discovery.dismiss(a["org"], a["users"]["editor"], zoom, {"reason": "Known"})
    configure(entra_connector, simulated_permissions=list(entra_simulator.GRANTED))
    discovery.run(entra_connector)
    assert discovery.status_of(obs(entra_connector, "Zoom")) == "dismissed"


def test_owner_change_brings_a_dismissed_candidate_back(a, entra_connector):
    discovery.run(entra_connector)
    discovery.dismiss(a["org"], a["users"]["editor"], obs(entra_connector, "Contract Vault"), {"reason": "Legal"})
    call_command("simulate_entra_change", "owners", "Contract Vault", "lee.chen@example.test")
    entra_connector.refresh_from_db()
    discovery.run(entra_connector)
    assert discovery.status_of(obs(entra_connector, "Contract Vault")) == "changed"


# ---- credentials feed reminders and the dashboard ----------------------------------------------------------------------


def credential_reminders(org, live=True):
    qs = Reminder.objects.filter(organization=org, source_type="credential_expiry")
    return qs.exclude(status=ReminderStatus.OBSOLETE) if live else qs


def test_credentials_feed_reminders_once_and_follow_decisions(a, entra_connector, django_capture_on_commit_callbacks):
    org = a["org"]
    with django_capture_on_commit_callbacks(execute=True):
        discovery.run(entra_connector)
    rows = credential_reminders(org)
    assert rows.count() == 7
    ledger = creds(entra_connector, "Finance Ledger SSO")[0]
    reminder = rows.get(source_id=ledger.id)
    assert (reminder.source_field, reminder.due_date, reminder.title) == (
        ledger.key_id,
        ledger.expires_on,
        "Finance Ledger SSO",
    )
    assert reminder.label == "Client secret “Ledger API secret” expires" and reminder.resource_id is None
    # Dedupe: a rerun and the hourly generation create nothing.
    with django_capture_on_commit_callbacks(execute=True):
        discovery.run(entra_connector)
    assert reminders.generate(org, org_today(org))["created"] == 0 and credential_reminders(org).count() == 7
    # Linking routes the reminder to the record (its IT steward); dismissing closes the app's reminders.
    with django_capture_on_commit_callbacks(execute=True):
        discovery.link(
            org, a["users"]["editor"], obs(entra_connector, "Finance Ledger SSO"), {"resource": str(a["app"].id)}
        )
    assert credential_reminders(org).get(source_id=ledger.id).resource_id == a["app"].id
    with django_capture_on_commit_callbacks(execute=True):
        discovery.dismiss(org, a["users"]["editor"], obs(entra_connector, "Payroll Sync Daemon"), {"reason": "Test"})
    assert credential_reminders(org).count() == 5
    # A credential removed in the tenant closes its reminder.
    change(entra_connector, {"op": "remove_credentials", "id": "Finance Ledger SSO"})
    with django_capture_on_commit_callbacks(execute=True):
        discovery.run(entra_connector)
    assert credential_reminders(org, live=False).get(source_id=ledger.id).status == ReminderStatus.OBSOLETE
    # A connector turned off stops them all; deleting it closes them for good.
    with django_capture_on_commit_callbacks(execute=True):
        discovery.update_connector(org, a["users"]["admin"], entra_connector, {"enabled": False})
    assert credential_reminders(org).count() == 0
    with django_capture_on_commit_callbacks(execute=True):
        discovery.update_connector(org, a["users"]["admin"], entra_connector, {"enabled": True})
    assert credential_reminders(org).count() == 4
    with django_capture_on_commit_callbacks(execute=True):
        discovery.delete_connector(org, a["users"]["admin"], entra_connector)
    assert credential_reminders(org).count() == 0


def test_reminders_and_the_dashboard_read_the_same_credentials(
    a, b, api, entra_connector, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        discovery.run(entra_connector)
    make_connector(b["org"], b["users"]["admin"])  # another organization's tenant, never shown here
    discovery.run(Connector.objects.get(organization=b["org"]))
    for role in ("admin", "viewer"):
        body = api(a["users"][role]).get("dashboard").json_body["expiring_credentials"]
        rows = [r for r in body["rows"] if r["kind"] == "discovered_credential"]
        today = org_today(a["org"])
        window = {
            str(r.source_id)
            for r in credential_reminders(a["org"])
            if today - timedelta(days=30) <= r.due_date <= today + timedelta(days=30)
        }
        assert {r["id"] for r in rows} == window and len(rows) == 3  # 20 days, 5 days, and expired 3 days ago
        ledger = next(r for r in rows if r["application"]["name"] == "Finance Ledger SSO")
        assert ledger["credential_type"] == "secret" and ledger["record"] is None and ledger["prefix"] == ""
        assert [r["expires_on"] for r in body["rows"]] == sorted(r["expires_on"] for r in body["rows"])


def test_the_credentials_list(a, b, api, entra_connector):
    discovery.run(entra_connector)
    viewer = api(a["users"]["viewer"])
    body = viewer.get("discovery/credentials").json_body
    assert len(body) == 7 and body[0]["days"] == -3 and body[0]["observation"]["name"] == "Graph Explorer Test"
    assert viewer.get("discovery/credentials?expires_within=10").status_code == 200
    assert {r["observation"]["name"] for r in viewer.get("discovery/credentials?expires_within=10").json_body} == {
        "Graph Explorer Test",
        "Payroll Sync Daemon",
    }
    assert viewer.get("discovery/credentials?q=ledger").json_body[0]["display_name"] == "Ledger API secret"
    assert viewer.get("discovery/credentials?state=removed").json_body == []
    assert viewer.get("discovery/credentials?state=nope").status_code == 400
    assert viewer.get("discovery/credentials?expires_within=x").status_code == 400
    other = make_connector(b["org"], b["users"]["admin"])
    assert viewer.get(f"discovery/credentials?connector={other.id}").status_code == 404
    assert api(b["users"]["viewer"]).get("discovery/credentials").json_body == []
    # The observation carries its credentials too.
    ledger = obs(entra_connector, "Finance Ledger SSO")
    detail = viewer.get(f"discovery/observations/{ledger.id}").json_body
    assert [c["display_name"] for c in detail["credentials"]] == ["Ledger API secret"]
    assert detail["registration"] and detail["enterprise_app"] and detail["agent"] == ""


# ---- licences --------------------------------------------------------------------------------------------------------


def licence_connector(a, entra_connector, **scope):
    data: dict[str, Any] = {
        "source": "entra_licenses",
        "name": "Licences",
        "microsoft_connection": str(entra_connector.microsoft_connection_id),
    }
    if scope:
        data["scope"] = scope
    return discovery.create_connector(a["org"], a["users"]["admin"], data)


def test_licence_observations(a, entra_connector):
    from money import licences

    connector = licence_connector(a, entra_connector)
    run = discovery.run(connector)
    assert run.status == RunStatus.SUCCEEDED and run.counts["kept"] == 4 and "credentials" not in run.counts
    e3 = Observation.objects.get(connector=connector, display_name="SPE_E3")
    assert e3.kind == ObservationKind.LICENSE and e3.external_id == "05e9a617-0261-4cee-bb44-138d3ef5d965"
    assert {
        k: e3.data[k] for k in ("sku_id", "sku_part_number", "seats_purchased", "seats_assigned", "seats_warning")
    } == {
        "sku_id": "05e9a617-0261-4cee-bb44-138d3ef5d965",
        "sku_part_number": "SPE_E3",
        "seats_purchased": 100,
        "seats_assigned": 97,
        "seats_warning": 10,
    }
    suspended = Observation.objects.get(connector=connector, display_name="DEVELOPERPACK_E5")
    assert suspended.data["seats_purchased"] == 0 and suspended.data["seats_suspended"] == 25
    assert suspended.data["capability_status"] == "Suspended"
    # Never a candidate; unbound SKUs are the seat review's proposals to make.
    assert discovery.candidate_counts(a["org"]).get(connector.id) is None
    assert {o.display_name for o in licences.unbound(a["org"])} >= {"SPE_E3", "SPE_E5"}
    second = discovery.run(connector)
    assert second.counts["unchanged"] == 4 and second.counts["changed"] == 0
    discovery.update_connector(a["org"], a["users"]["admin"], connector, {"scope": {"sku_exclusions": ["FLOW_"]}})
    connector.refresh_from_db()
    assert discovery.run(connector).counts["skipped"] == {"sku_exclusions": 1}


def test_licences_without_their_permission_fail_naming_it(a, entra_connector):
    configure(entra_connector, simulated_permissions=["Application.Read.All"])
    connector = licence_connector(a, entra_connector)
    configure(connector, simulated_permissions=["Application.Read.All"])
    run = discovery.run(connector)
    assert run.status == RunStatus.FAILED
    assert "LicenseAssignment.Read.All" in run.error and "403 Authorization_RequestDenied" in run.error


# ---- access checks ---------------------------------------------------------------------------------------------------


def steps_by_permission(result):
    return {s["permission"]: s for s in result["steps"] if s["group"] == "api"}


def test_a_change_of_connection_or_scope_clears_the_last_access_check(a, entra_connector):
    """A check made for another connection or scope says nothing about the new one, so changing
    either clears it in the same audited write; a change of name, schedule or state keeps it."""
    org, admin = a["org"], a["users"]["admin"]

    def checked():
        access.check_access(org, admin, entra_connector)
        row = Connector.objects.get(pk=entra_connector.pk)
        assert row.access_check["ok"] and row.access_checked_at and row.access_checked_by == admin
        return row

    def cleared(row):
        row = Connector.objects.get(pk=row.pk)
        return row.access_check == {} and row.access_checked_at is None and row.access_checked_by is None

    row = discovery.update_connector(org, admin, checked(), {"name": "Entra renamed", "schedule": "daily"})
    assert not cleared(row)
    row = discovery.update_connector(org, admin, row, {"scope": {"exclude_disabled": True}})  # the same scope
    assert not cleared(row)

    row = discovery.update_connector(org, admin, checked(), {"scope": {"include_agent_identities": True}})
    assert cleared(row)
    event = AuditEvent.objects.filter(action="connector.updated").order_by("-created_at").first()
    assert event is not None
    assert event.changes["access_check"] == {"before": "checked", "after": "cleared"}

    other = connections.create_connection(
        org,
        admin,
        connections.MICROSOFT,
        {"name": "Second app", "tenant_id": "simulated", "client_id": "", "secret": SECRET},
    )
    connections.run_test(org, admin, other)
    row = discovery.update_connector(org, admin, checked(), {"microsoft_connection": str(other.id)})
    assert cleared(row) and row.microsoft_connection_id == other.id


def test_access_check_all_granted(a, entra_connector):
    row = access.check_access(a["org"], a["users"]["admin"], entra_connector)
    assert row.access_check["ok"] and row.access_check["missing"] == [] and row.access_check["optional_missing"] == []
    assert [s["name"] for s in row.access_check["steps"]] == [
        "Connection",
        "Sign in to Microsoft Graph",
        "Read app registrations",
        "Read enterprise applications",
        "Read owners' names",
        "Read agent identities",
    ]
    by = steps_by_permission(row.access_check)
    assert (
        by["Application.Read.All"]["detail"]
        == "Reading enterprise applications works (Application.Read.All or broader)."
    )
    # Agent identities are off in the scope: that probe isn't made.
    assert by["AgentIdentity.Read.All"]["state"] == "not_used"
    event = AuditEvent.objects.get(action="connector.access_checked")
    assert event.changes == {"ok": True, "missing": [], "optional_missing": []}
    # Only the connection's name: Viewers read this step (GET connectors/{id}).
    assert (
        row.access_check["steps"][0]["detail"] == f"Uses the connection “{entra_connector.microsoft_connection.name}”."
    )


def include_agents(a, connector):
    discovery.update_connector(a["org"], a["users"]["admin"], connector, {"scope": {"include_agent_identities": True}})
    connector.refresh_from_db()
    return connector


def test_the_agent_identity_probe_follows_the_scope(a, entra_connector):
    transport = entra_simulator.Transport(entra_connector)
    access.check_access(a["org"], a["users"]["admin"], entra_connector, transport=transport)
    assert not any("agentIdentity" in url for _, url in transport.calls)
    include_agents(a, entra_connector)
    transport = entra_simulator.Transport(entra_connector)
    result = access.check_access(a["org"], a["users"]["admin"], entra_connector, transport=transport).access_check
    assert any("agentIdentity" in url for _, url in transport.calls)
    assert steps_by_permission(result)["AgentIdentity.Read.All"]["state"] == "ok"


def test_an_optional_probe_that_fails_otherwise_is_a_warning(a, entra_connector, no_sleep):
    include_agents(a, entra_connector)
    for status in (400, 404, 500):
        configure(entra_connector, simulated_errors={"/v1.0/servicePrincipals/microsoft.graph.agentIdentity": status})
        result = access.check_access(a["org"], a["users"]["admin"], entra_connector).access_check
        step = steps_by_permission(result)["AgentIdentity.Read.All"]
        assert result["ok"] and step["state"] == "warning" and f"({status}" in step["detail"], step
    configure(entra_connector, simulated_errors={"/v1.0/applications": 404})
    result = access.check_access(a["org"], a["users"]["admin"], entra_connector).access_check
    step = next(s for s in result["steps"] if s["name"] == "Read app registrations")
    assert not result["ok"] and step["state"] == "failed" and step["problem"] == "unavailable"


def test_a_401_on_a_read_is_authentication_not_a_missing_permission(a, entra_connector):
    """On a required read a 401 fails the check as authentication; on an optional one it is a warning: the check
    stays ok."""
    configure(entra_connector, simulated_errors={"/v1.0/applications": 401})
    result = access.check_access(a["org"], a["users"]["admin"], entra_connector).access_check
    step = next(s for s in result["steps"] if s["name"] == "Read app registrations")
    assert step["state"] == "failed" and step["problem"] == "wrong_credentials" and "didn't accept" in step["detail"]
    assert not result["ok"] and result["missing"] == []

    configure(entra_connector, simulated_errors={"/v1.0/users": 401})
    result = access.check_access(a["org"], a["users"]["admin"], entra_connector).access_check
    step = steps_by_permission(result)["User.ReadBasic.All"]
    assert step["state"] == "warning" and step["problem"] == "wrong_credentials" and "didn't accept" in step["detail"]
    assert result["ok"] and result["missing"] == [] and result["optional_missing"] == []


def test_an_expired_token_is_refreshed_once_during_a_run(entra_connector):
    transport = entra_simulator.Transport(configure(entra_connector, simulated_token_expires=True))
    run = discovery.run(entra_connector, transport=transport)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert sum(1 for method, _ in transport.calls if method == "POST") == 2


def test_the_sign_in_step_uses_the_connection_tests_classification(a, entra_connector):
    configure(entra_connector, simulated_token_echo=True)
    result = access.check_access(a["org"], a["users"]["admin"], entra_connector).access_check
    sign_in = next(s for s in result["steps"] if s["name"] == "Sign in to Microsoft Graph")
    assert sign_in["problem"] == "error" and sign_in["code"] == "AADSTS900144"  # not "wrong credentials" by default
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize(
    "granted, ok, missing, optional",
    [
        (["User.ReadBasic.All", "AgentIdentity.Read.All"], False, ["Application.Read.All"], []),
        (["Application.Read.All", "AgentIdentity.Read.All"], True, [], ["User.ReadBasic.All"]),
        (["Application.Read.All", "User.ReadBasic.All"], True, [], ["AgentIdentity.Read.All"]),
        ([], False, ["Application.Read.All"], ["AgentIdentity.Read.All", "User.ReadBasic.All"]),
    ],
)
def test_access_check_names_each_missing_permission(a, entra_connector, granted, ok, missing, optional):
    include_agents(a, entra_connector)
    configure(entra_connector, simulated_permissions=granted)
    result = access.check_access(a["org"], a["users"]["admin"], entra_connector).access_check
    assert (result["ok"], result["missing"], result["optional_missing"]) == (ok, missing, optional)
    by = steps_by_permission(result)
    for name in missing:
        assert by[name]["state"] == "failed" and by[name]["problem"] == "missing_permission"
        assert by[name]["code"] == "Authorization_RequestDenied" and name in by[name]["detail"]
        assert "admin consent" in by[name]["next"]
    for name in optional:
        assert by[name]["state"] == "optional_missing" and "Optional, not granted" in by[name]["detail"]


def test_access_check_for_licences(a, entra_connector):
    connector = licence_connector(a, entra_connector)
    assert access.check_access(a["org"], a["users"]["admin"], connector).access_check["ok"]
    configure(connector, simulated_permissions=["Application.Read.All"])
    result = access.check_access(a["org"], a["users"]["admin"], connector).access_check
    assert result["missing"] == ["LicenseAssignment.Read.All"] and not result["ok"]


def test_access_check_wrong_secret_stops_at_sign_in(a, entra_connector):
    connections.update_connection(
        a["org"], a["users"]["admin"], entra_connector.microsoft_connection, {"secret": "wrong-secret"}
    )
    result = access.check_access(a["org"], a["users"]["admin"], entra_connector).access_check
    sign_in = next(s for s in result["steps"] if s["name"] == "Sign in to Microsoft Graph")
    assert sign_in["state"] == "failed" and sign_in["problem"] == "wrong_credentials"
    assert "client secret is wrong" in sign_in["detail"]
    assert {s["state"] for s in result["steps"] if s["group"] == "api"} == {"skipped"} and not result["ok"]
    assert "wrong-secret" not in json.dumps(result)


def test_access_check_without_a_usable_connection(a, entra_connector):
    connections.update_connection(
        a["org"], a["users"]["admin"], entra_connector.microsoft_connection, {"enabled": False}
    )
    result = access.check_access(a["org"], a["users"]["admin"], entra_connector).access_check
    assert result["steps"][0]["problem"] == "disabled" and not result["ok"]


def test_access_check_over_the_api(a, b, api, entra_connector):
    url = f"connectors/{entra_connector.id}/access-check"
    for role in ("editor", "viewer"):
        assert api(a["users"][role]).post(url).status_code == 403
    assert api(b["users"]["admin"]).post(url).status_code == 404  # another organization's connector
    configure(entra_connector, simulated_permissions=["Application.Read.All"])
    r = api(a["users"]["admin"]).post(url)
    assert r.status_code == 200
    check = r.json_body["access_check"]
    assert check["ok"] and check["optional_missing"] == ["User.ReadBasic.All"]
    assert check["checked_by"]["id"] == a["users"]["admin"].id
    body = api(a["users"]["viewer"]).get(f"connectors/{entra_connector.id}").json_body
    assert body["access_check"]["steps"] == check["steps"]
    assert {p["name"]: p["required"] for p in body["permissions"]} == {
        "Application.Read.All": True,
        "User.ReadBasic.All": False,
        "AgentIdentity.Read.All": False,
    }
    assert next(p for p in body["permissions"] if p["name"] == "Application.Read.All")["role_id"] == (
        "9a5d68dd-52b0-4cc2-bd40-abcf44ac3a30"
    )


# ---- throttling ------------------------------------------------------------------------------------------------------


def test_429_honours_retry_after(entra_connector, no_sleep):
    configure(entra_connector, simulated_throttle={"path": "/v1.0/servicePrincipals", "times": 2, "retry_after": "7"})
    run = discovery.run(entra_connector)
    assert run.status == RunStatus.SUCCEEDED and no_sleep["slept"] == [7.0, 7.0]


def test_backoff_is_exponential_without_retry_after(entra_connector, no_sleep):
    configure(entra_connector, simulated_throttle={"path": "/v1.0/applications", "times": 3, "status": 503})
    run = discovery.run(entra_connector)
    assert run.status == RunStatus.SUCCEEDED and no_sleep["slept"] == [2.0, 4.0, 8.0]


def test_throttling_past_the_budget_stops_the_run(entra_connector, no_sleep):
    configure(
        entra_connector, simulated_throttle={"path": "/v1.0/servicePrincipals", "times": 99, "retry_after": "120"}
    )
    run = discovery.run(entra_connector)
    assert run.status == RunStatus.FAILED and "kept throttling" in run.error
    assert sum(no_sleep["slept"]) <= graph.RUN_BUDGET
    assert Observation.objects.filter(connector=entra_connector).count() == 0


def test_a_retry_after_longer_than_the_cap_stops_the_run(entra_connector, no_sleep):
    budget = graph.Budget()
    assert budget.why_not(0, 3600) == "long_wait" and budget.delay(3, None) == 16.0
    assert budget.delay(9, None) == graph.MAX_WAIT  # backoff without Retry-After is capped
    assert graph.Budget(max_attempts=0).why_not(0, 1) == "attempts"
    configure(entra_connector, simulated_throttle={"path": "/v1.0/applications", "times": 1, "retry_after": "600"})
    run = discovery.run(entra_connector)
    assert run.status == RunStatus.FAILED and "asked to wait 600 seconds" in run.error and no_sleep["slept"] == []


def test_a_run_stops_at_its_wall_clock_deadline(entra_connector, monkeypatch, no_sleep):
    """Slow answers (no throttling) still can't hold the worker past the run's 40 minutes."""
    original = entra_simulator.Transport.send

    def slow(self, method, url, headers, body=None):
        no_sleep["now"] += 25 * 60  # every answer takes 25 minutes
        return original(self, method, url, headers, body)

    monkeypatch.setattr(entra_simulator.Transport, "send", slow)
    run = discovery.run(entra_connector)
    assert run.status == RunStatus.FAILED and "40-minute limit" in run.error
    assert Observation.objects.filter(connector=entra_connector).count() == 0


# ---- no secret or token leaks ------------------------------------------------------------------------------------------


def test_no_secret_or_token_in_run_errors_logs_or_responses(a, api, entra_connector, caplog):
    caplog.set_level(logging.DEBUG)
    configure(entra_connector, simulated_errors={"/v1.0/applications": 400})
    run = discovery.run(entra_connector)
    assert run.status == RunStatus.FAILED and "400" in run.error
    configure(entra_connector, simulated_errors={"/v1.0/users": 400})
    access.check_access(a["org"], a["users"]["admin"], entra_connector)
    configure(entra_connector, simulated_errors={}, simulated_token_echo=True)
    echoed = discovery.run(entra_connector)
    assert echoed.status == RunStatus.FAILED and SECRET not in echoed.error
    access.check_access(a["org"], a["users"]["admin"], entra_connector)
    configure(entra_connector, simulated_token_echo=False)
    discovery.run(entra_connector)
    reminders.generate(a["org"], org_today(a["org"]))
    from core import jobs
    from core.models import Job, Schedule

    discovery.request_run(a["org"], a["users"]["admin"], entra_connector)
    summary = jobs.summarize(discovery.scheduled_runs(datetime.now(UTC)))
    texts = [
        json.dumps(summary),
        json.dumps(list(Reminder.objects.values("title", "label", "source_field")), default=str),
        json.dumps(list(Schedule.objects.values("last_result", "last_error")), default=str),
        json.dumps(list(Job.objects.values("payload", "result", "last_error")), default=str),
        caplog.text,
        json.dumps(list(ConnectorRun.objects.values("error", "counts"))),
        json.dumps(list(Connector.objects.values("access_check", "config", "scope"))),
        json.dumps(list(AuditEvent.objects.values("changes", "prechange", "postchange")), default=str),
        json.dumps(list(Observation.objects.values("data")), default=str),
    ]
    for user in a["users"].values():
        for url in (
            "connectors",
            f"connectors/{entra_connector.id}",
            f"connectors/{entra_connector.id}/runs",
            "discovery/candidates?status=all",
            "discovery/credentials?state=all",
            "dashboard",
        ):
            texts.append(json.dumps(api(user).get(url).json_body))
    for text in texts:
        assert entra_simulator.TOKEN not in text and SECRET not in text and "Bearer" not in text


# ---- tenancy ---------------------------------------------------------------------------------------------------------


def test_database_rejects_a_credential_on_another_organizations_observation(a, b, entra_connector):
    discovery.run(entra_connector)
    theirs = Observation.objects.filter(connector=entra_connector).earliest("created_at")
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError), transaction.atomic():
        DiscoveredCredential.objects.create(
            organization=b["org"],
            observation=theirs,
            key_id="k",
            credential_type="secret",
            origin="app_registration",
            first_seen_at=now,
            last_seen_at=now,
        )
        connection.check_constraints()


def test_runs_never_touch_another_organizations_credentials(a, b, entra_connector):
    discovery.run(entra_connector)
    other = make_connector(b["org"], b["users"]["admin"])
    configure(other, simulator_changes=[{"op": "remove", "id": "Finance Ledger SSO"}])
    discovery.run(other)
    assert DiscoveredCredential.objects.filter(organization=a["org"], removed_at__isnull=True).count() == 7
    assert DiscoveredCredential.objects.filter(organization=b["org"], removed_at__isnull=True).count() == 6
    assert (
        credential_reminders(b["org"]).filter(source_id__in=[c.id for c in creds(entra_connector, "Workday")]).count()
        == 0
    )
    assert all(
        f.source_id not in {c.id for c in DiscoveredCredential.objects.filter(organization=a["org"])}
        for f in reminders.credential_facts(b["org"], org_today(b["org"]))
    )


# ---- upgrade -----------------------------------------------------------------------------------------------------------


def _migration():
    import importlib

    return importlib.import_module("discovery.migrations.0010_entra_observation_keys")


def earlier_fingerprint(name, enabled, owners):
    """The earlier version's relevant(): name, enabled, owners by email (else object ID), service principal owners only."""
    import hashlib

    relevant = {
        "name": name,
        "enabled": enabled,
        "owners": sorted(o["email"].lower() or o["external_id"] for o in owners),
    }
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def make_earlier(observation, owners, **data):
    """Rewrite an observation as an earlier version left it: keyed by the service principal's ID, its data shape (no
    service_principal_id, registration flags or owner types) and its fingerprint; a dismissal on that fingerprint."""
    earlier = {
        "app_id": observation.data["app_id"],
        "description": observation.data["description"],
        "homepage": observation.data["homepage"],
        "publisher": observation.data["publisher"],
        "enabled": observation.data["enabled"],
        "type": "Application",
        "microsoft": False,
        "tags": observation.data["tags"],
        "owners": owners,
        **data,
    }
    fp = earlier_fingerprint(observation.display_name, earlier["enabled"], owners)
    Observation.objects.filter(pk=observation.pk).update(
        kind="application",
        external_id=entra_simulator._id(observation.display_name),
        data=earlier,
        fingerprint=fp,
        ai_agent=bool(data.get("ai_agent_flag")),
    )
    ReconciliationDecision.objects.filter(observation=observation).update(fingerprint=fp)
    return fp


def test_an_earlier_dismissal_survives_the_first_run_after_the_upgrade(a, entra_connector):
    """discovery 0010 + engine._apply: an in-house app whose service principal has no owners but whose registration has
    two (the earlier version saw no owners; the new source sees two) stays dismissed, as does one with email-only owner
    fields of that version."""
    from django.apps import apps

    change(
        entra_connector,
        {"op": "owners", "id": "Finance Ledger SSO", "value": ["lee.chen@example.test", "dana.ortiz@example.test"]},
        {"op": "sp_owners", "id": "Finance Ledger SSO", "value": []},
    )
    discovery.run(entra_connector)
    ledger, zoom = obs(entra_connector, "Finance Ledger SSO"), obs(entra_connector, "Zoom")
    for o in (ledger, zoom):
        discovery.dismiss(a["org"], a["users"]["editor"], o, {"reason": "Known"})
    make_earlier(ledger, [])
    make_earlier(zoom, [{"external_id": "", "name": "Dana Ortiz", "email": "dana.ortiz@example.test"}])
    _migration().forward(apps, None)
    for o in (ledger, zoom):
        o.refresh_from_db()
        assert o.external_id == o.data["app_id"] and o.data["rekeyed"]
    before = ledger.last_changed_at
    run = discovery.run(entra_connector)
    assert run.counts["new"] == 0 and run.counts["gone"] == 0
    assert "First run since the upgrade" in run.counts["notes"][0]
    for name in ("Finance Ledger SSO", "Zoom"):
        o = obs(entra_connector, name)
        assert discovery.status_of(o) == "dismissed", name
        assert "rekeyed" not in o.data
    assert obs(entra_connector, "Finance Ledger SSO").last_changed_at == before  # the upgrade isn't a source change
    second = discovery.run(entra_connector)
    assert second.counts["changed"] == 0 and "notes" not in second.counts
    # A real change afterwards still brings the dismissal back.
    call_command("simulate_entra_change", "rename", "Zoom", "Zoom Workplace")
    entra_connector.refresh_from_db()
    discovery.run(entra_connector)
    assert discovery.status_of(obs(entra_connector, "Zoom Workplace")) == "changed"


def test_the_upgrade_gives_a_shared_app_id_to_the_decided_row(a, entra_connector):
    """Two earlier rows with one appId (an app deleted and re-created): the key goes to the dismissed, live one, even
    when the gone one sorts first."""
    import uuid

    from django.apps import apps

    discovery.run(entra_connector)
    zoom = obs(entra_connector, "Zoom")
    discovery.dismiss(a["org"], a["users"]["editor"], zoom, {"reason": "Known"})
    make_earlier(zoom, [])
    now = datetime.now(UTC)
    old = Observation.objects.create(
        id=uuid.UUID(int=1),  # sorts before any random ID
        organization=a["org"],
        connector=entra_connector,
        kind="application",
        external_id="11111111-1111-4111-8111-111111111111",
        display_name="Zoom (old)",
        data={**zoom.data, "app_id": obs(entra_connector, "Zoom").data["app_id"]},
        fingerprint="f",
        first_seen_at=now - timedelta(days=90),
        last_seen_at=now - timedelta(days=60),
        last_changed_at=now - timedelta(days=60),
        gone_at=now - timedelta(days=59),
    )
    _migration().forward(apps, None)
    zoom.refresh_from_db()
    old.refresh_from_db()
    assert zoom.external_id == zoom.data["app_id"] and old.external_id == "11111111-1111-4111-8111-111111111111"
    run = discovery.run(entra_connector)
    assert run.counts["new"] == 0 and discovery.status_of(obs(entra_connector, "Zoom")) == "dismissed"


def test_the_upgrade_sets_agent_kind_only_from_the_new_tags(a, entra_connector):
    from django.apps import apps

    discovery.run(entra_connector)
    agent, expense = obs(entra_connector, "HR Helpdesk Agent"), obs(entra_connector, "Expense Hub")
    make_earlier(agent, [])  # tags AgentCreatedBy:CopilotStudio: an agent
    make_earlier(expense, [], tags=["AIAgent"], ai_agent_flag=True)  # the earlier version's own tag: not carried over
    _migration().forward(apps, None)
    agent.refresh_from_db()
    expense.refresh_from_db()
    assert agent.kind == ObservationKind.AI_AGENT and agent.ai_agent
    assert expense.kind == ObservationKind.APPLICATION and not expense.ai_agent


def test_create_record_for_an_application_still_defaults_to_application(a, entra_connector):
    discovery.run(entra_connector)
    _, record = discovery.create_record(a["org"], a["users"]["editor"], obs(entra_connector, "Zoom"), {})
    assert Resource.objects.get(id=record.id).kind == "application"


def test_owners_beyond_the_expand_limit_are_paged(a, entra_connector):
    emails = [f"owner{i:02d}@example.test" for i in range(25)]
    change(entra_connector, {"op": "owners", "id": "Contract Vault", "value": emails})
    transport = entra_simulator.Transport(entra_connector)
    discovery.run(entra_connector, transport=transport)
    vault = obs(entra_connector, "Contract Vault")
    assert sorted(o["email"] for o in vault.data["owners"]) == emails
    paged = [url for _, url in transport.calls if url.split("?")[0].endswith("/owners")]
    # Only the capped object (its registration and its enterprise app), nothing else.
    ids = {entra_simulator._id("Contract Vault"), entra_simulator._id("application/Contract Vault")}
    assert len(paged) == 2 and all(any(i in u for i in ids) for u in paged)


def test_a_run_rechecks_the_sources_capability(a, api, entra_connector, monkeypatch):
    from core import capabilities

    monkeypatch.setitem(capabilities.SOURCE_CAPABILITY, "entra", capabilities.BILLING)  # one community lacks
    run = discovery.run(entra_connector)
    assert run.status == RunStatus.FAILED and "billing" in run.error
    r = api(a["users"]["admin"]).post(f"connectors/{entra_connector.id}/access-check")
    assert r.status_code == 403 and "billing" in json.dumps(r.json_body)


def test_two_connectors_on_one_tenant_give_one_reminder_and_one_row(
    a, api, entra_connector, django_capture_on_commit_callbacks
):
    second = discovery.create_connector(
        a["org"],
        a["users"]["admin"],
        {"name": "Entra again", "microsoft_connection": str(entra_connector.microsoft_connection_id)},
    )
    with django_capture_on_commit_callbacks(execute=True):
        discovery.run(entra_connector)
        discovery.run(second)
    assert DiscoveredCredential.objects.count() == 14
    assert credential_reminders(a["org"]).count() == 7
    rows = api(a["users"]["viewer"]).get("dashboard").json_body["expiring_credentials"]["rows"]
    assert len([r for r in rows if r["kind"] == "discovered_credential"]) == 3
    # Linking the second connector's copy routes the one reminder through it.
    with django_capture_on_commit_callbacks(execute=True):
        discovery.link(
            a["org"], a["users"]["editor"], obs(second, "Finance Ledger SSO"), {"resource": str(a["app"].id)}
        )
    ledger = credential_reminders(a["org"]).get(title="Finance Ledger SSO")
    assert ledger.resource_id == a["app"].id and credential_reminders(a["org"]).count() == 7


def test_agent_identities_listed_by_service_principals_follow_the_scope(a, entra_connector):
    configure(entra_connector, simulated_agents_in_list=True)
    run = discovery.run(entra_connector)
    assert run.counts["skipped"]["agent_identities"] == 1 and run.counts["kept"] == 9
    include_agents(a, entra_connector)
    run = discovery.run(entra_connector)
    assert run.counts["new"] == 1 and Observation.objects.filter(display_name="Benefits Agent").count() == 1


def test_agent_identity_blueprints_are_applications_with_a_note(entra_connector):
    configure(entra_connector, simulated_blueprint=True)
    run = discovery.run(entra_connector)
    blueprint = obs(entra_connector, "Benefits Agent Blueprint")
    assert blueprint.kind == ObservationKind.APPLICATION and blueprint.data["agent_blueprint"]
    assert any("blueprint" in n for n in run.counts["notes"])
