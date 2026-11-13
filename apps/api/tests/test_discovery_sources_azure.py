"""The Azure, Power Platform and Halo sources on the simulated tenants: the azure source (Logic Apps, Function Apps, App Services, runbooks, from
per-type ARM lists), the powerplatform source (Copilot Studio agents and cloud flows from Dataverse, all environments
through the admin API), the halo source (assets), their access checks, their capabilities, per-audience tokens
and host allow-lists, tenancy and roles, and that no token or secret leaks. Nothing here reaches the network."""

import json
import logging
import uuid
from typing import Any

import pytest

from catalog.models import Resource
from core import capabilities as caps
from core import connections, graph, halo
from core.models import AuditEvent
from core.problems import CapabilityRequired, FieldErrors
from discovery import access, azure, azure_simulator, entra_simulator, graph_source, halo_simulator, powerplatform
from discovery import engine as discovery
from discovery import powerplatform_simulator as pp_sim
from discovery.models import Connector, ConnectorRun, Observation, ObservationKind, RunStatus
from discovery.simulator_base import ARM_TOKEN, BAP_TOKEN, DATAVERSE_TOKEN

SECRET = "synthetic-sources-b-secret"
PROD, DEV, LEGACY = azure_simulator.PROD, azure_simulator.DEV, azure_simulator.LEGACY
CONTOSO, CONTOSO_DEV = pp_sim.CONTOSO, pp_sim.CONTOSO_DEV
URLS = [f"https://{CONTOSO}", f"https://{CONTOSO_DEV}"]


def connection(org, actor, kind):
    if kind == "halo":
        data = {
            "name": f"Halo {uuid.uuid4().hex[:6]}",
            "base_url": halo.SIMULATED_URL,
            "client_id": "softwarden",
            "secret": SECRET,
        }
        conn = connections.create_connection(org, actor, connections.HALO, data)
    else:
        data = {"name": f"Microsoft {uuid.uuid4().hex[:6]}", "tenant_id": "simulated", "secret": SECRET}
        conn = connections.create_connection(org, actor, connections.MICROSOFT, data)
    connections.run_test(org, actor, conn)
    return conn


def make(org, actor, source, scope=None, conn=None, name=None):
    kind = "halo" if source == "halo" else "microsoft"
    conn = conn or connection(org, actor, kind)
    data: dict[str, Any] = {"source": source, "name": name or f"{source} connector", f"{kind}_connection": str(conn.id)}
    if scope is not None:
        data["scope"] = scope
    return Connector.objects.get(pk=discovery.create_connector(org, actor, data).pk)


def configure(connector, **config):
    connector.config = {**connector.config, **config}
    connector.save(update_fields=["config"])
    return connector


def change(connector, *changes):
    return configure(connector, simulator_changes=[*connector.config.get("simulator_changes", []), *changes])


def obs(connector, name):
    return Observation.objects.get(connector=connector, display_name=name)


def names(connector, live=True):
    qs = Observation.objects.filter(connector=connector)
    return sorted(qs.filter(gone_at__isnull=True).values_list("display_name", flat=True) if live else qs)


def steps(result, state=None):
    return [s for s in result["steps"] if state is None or s["state"] == state]


@pytest.fixture
def ent(a):
    a["org"], _ = caps.set_edition(a["org"], "enterprise")
    return a


@pytest.fixture
def az(ent):
    return make(ent["org"], ent["users"]["admin"], "azure")


@pytest.fixture
def pp(ent):
    return make(ent["org"], ent["users"]["admin"], "powerplatform", {"environment_urls": URLS})


@pytest.fixture
def hl(ent):
    return make(ent["org"], ent["users"]["admin"], "halo")


@pytest.fixture
def no_sleep(monkeypatch):
    clock: dict[str, Any] = {"now": 1000.0, "slept": []}

    def sleep(seconds):
        clock["slept"].append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(graph, "sleep", sleep)
    monkeypatch.setattr(graph, "monotonic", lambda: clock["now"])
    return clock


# ---- azure: listing --------------------------------------------------------------------------------------------------

AZURE_KEPT = [
    "Cleanup-Temp",
    "Rotate-Keys",
    "contoso-workflows",
    "hr-sync",
    "invoice-intake",
    "invoice-intake-dev",
    "orders-func",
    "payroll-func",
    "test-func",
]


def test_azure_lists_logic_apps_function_apps_and_runbooks_with_their_properties(az):
    run = discovery.run(az)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert (run.counts["seen"], run.counts["kept"], run.counts["new"]) == (11, 9, 9)
    assert run.counts["skipped"] == {"app_services": 2}  # App Services are off by default
    assert names(az) == AZURE_KEPT
    intake = obs(az, "invoice-intake")
    rid = azure_simulator.resource_id(PROD, "rg-integration", "logic", "invoice-intake")
    assert intake.kind == ObservationKind.COMPONENT and intake.suggested_component_type == "logic_app"
    assert intake.data["resource_id"] == rid and intake.data["subscription_id"] == PROD
    assert (
        intake.data["subscription_name"] == "Contoso Production" and intake.data["resource_group"] == "rg-integration"
    )
    assert intake.data["location"] == "westeurope" and intake.data["state"] == "Enabled"
    assert intake.data["tags"] == {"environment": "Production", "costCenter": "FIN-01"}
    assert intake.data["environment"] == "prod" and intake.data["identity"] == "SystemAssigned"
    assert obs(az, "hr-sync").data["state"] == "Disabled"
    func = obs(az, "orders-func")
    assert func.suggested_component_type == "function" and func.data["site_kind"] == "functionapp,linux"
    standard = obs(az, "contoso-workflows")
    assert standard.suggested_component_type == "logic_app" and standard.data["azure_type"] == "logic_app"
    runbook = obs(az, "Rotate-Keys")
    assert runbook.suggested_component_type == "runbook" and runbook.data["runbook_type"] == "PowerShell72"
    assert runbook.data["automation_account"] == "ops-automation" and runbook.data["description"] == "Rotates keys."
    # The disabled subscription isn't read, and the Standard Logic App kind is noted for live checking.
    notes = " ".join(run.counts["notes"])
    assert "Contoso Legacy (Disabled)" in notes and "'workflowapp'" in notes
    assert not Observation.objects.filter(display_name="legacy-ftp-import").exists()
    # Logic App definitions and site settings are never kept.
    stored = json.dumps(list(Observation.objects.values("data")))
    assert azure_simulator.DEFINITION_SECRET not in stored and "definition" not in stored


def test_azure_sends_the_verified_api_versions_and_per_type_lists(az):
    transport = azure_simulator.Transport(az)
    discovery.run(az, transport=transport)
    reads = [url for method, url in transport.calls if method == "GET"]
    assert f"https://management.azure.com/subscriptions?api-version={azure.SUBSCRIPTIONS_API}" in reads
    assert any("/providers/Microsoft.Logic/workflows?api-version=2019-05-01" in u for u in reads)
    assert any("/providers/Microsoft.Web/sites?api-version=2024-04-01" in u for u in reads)
    assert any("/automationAccounts/ops-automation/runbooks?api-version=2024-10-23" in u for u in reads)
    assert not any(u.split("?")[0].endswith("/resources") for u in reads)  # never the generic Resources list


def test_azure_a_second_run_is_unchanged(az):
    discovery.run(az)
    second = discovery.run(az)
    assert (second.counts["new"], second.counts["changed"], second.counts["gone"], second.counts["unchanged"]) == (
        0,
        0,
        0,
        9,
    )


def test_azure_a_state_change_gives_a_changed_candidate(ent, az):
    discovery.run(az)
    discovery.dismiss(ent["org"], ent["users"]["editor"], obs(az, "invoice-intake"), {"reason": "Known"})
    change(az, {"op": "state", "id": "invoice-intake", "value": "Disabled"})
    run = discovery.run(az)
    assert run.counts["changed"] == 1 and discovery.status_of(obs(az, "invoice-intake")) == "changed"
    # A tag edit is a change in the source but doesn't bring a dismissal back.
    discovery.dismiss(ent["org"], ent["users"]["editor"], obs(az, "invoice-intake"), {"reason": "Known"})
    change(az, {"op": "tag", "id": "invoice-intake", "value": {"owner": "finance"}})
    run = discovery.run(az)
    assert run.counts["changed"] == 1 and discovery.status_of(obs(az, "invoice-intake")) == "dismissed"


def test_azure_a_rename_is_a_new_resource_and_a_delete_is_gone(az):
    discovery.run(az)
    change(az, {"op": "rename", "id": "payroll-func", "value": "payroll-func-v2"}, {"op": "remove", "id": "hr-sync"})
    run = discovery.run(az)
    assert (run.counts["new"], run.counts["gone"]) == (1, 2)  # ARM can't rename: a new ID, the old one gone
    assert discovery.status_of(obs(az, "payroll-func")) == "gone" and discovery.status_of(obs(az, "hr-sync")) == "gone"
    assert discovery.status_of(obs(az, "payroll-func-v2")) == "new"


def test_azure_app_services_are_optional_and_scope_flags_skip_calls(ent, az):
    discovery.update_connector(ent["org"], ent["users"]["admin"], az, {"scope": {"include_app_services": True}})
    az.refresh_from_db()
    run = discovery.run(az)
    assert run.counts["kept"] == 11 and obs(az, "intranet-web").suggested_component_type == "service"
    discovery.update_connector(
        ent["org"], ent["users"]["admin"], az, {"scope": {"include_runbooks": False, "include_logic_apps": False}}
    )
    az.refresh_from_db()
    transport = azure_simulator.Transport(az)
    run = discovery.run(az, transport=transport)
    assert run.counts["skipped"] == {"logic_apps": 1}  # the Standard Logic App, read with the web apps
    assert not any("Logic/workflows" in u or "automationAccounts" in u for _, u in transport.calls)
    # Runbooks aren't read any more: out of scope, never "no longer seen" (reconciliation).
    assert discovery.status_of(obs(az, "Rotate-Keys")) == "out_of_scope"


def test_azure_an_unknown_site_kind_is_skipped_and_noted(az):
    change(az, {"op": "add", "id": "flex-thing", "value": {"type": "site", "kind": "hyperscale,widget"}})
    run = discovery.run(az)
    assert run.status == RunStatus.SUCCEEDED and run.counts["skipped"]["unknown_kind"] == 1
    assert any("hyperscale,widget" in n and "doesn't recognize" in n for n in run.counts["notes"])
    assert not Observation.objects.filter(display_name="flex-thing").exists()
    assert azure.site_type("functionapp,linux,container") == "function_app" and azure.site_type("") == "app_service"
    assert azure.site_type("app,linux,container") == "app_service" and azure.site_type("api") == "app_service"


def test_azure_chosen_subscriptions_and_validation(ent, az):
    with pytest.raises(FieldErrors) as e:
        discovery.update_connector(ent["org"], ent["users"]["admin"], az, {"scope": {"subscriptions": ["nope"]}})
    assert "scope.subscriptions" in e.value.errors
    discovery.update_connector(ent["org"], ent["users"]["admin"], az, {"scope": {"subscriptions": [DEV.upper()]}})
    az.refresh_from_db()
    assert az.scope["subscriptions"] == [DEV]
    run = discovery.run(az)
    assert names(az) == ["invoice-intake-dev", "test-func"] and "notes" not in run.counts


def test_azure_paging_follows_next_links(az):
    configure(az, simulated_page=1)
    transport = azure_simulator.Transport(az)
    run = discovery.run(az, transport=transport)
    assert run.counts["kept"] == 9 and sum("$skiptoken" in u for _, u in transport.calls) >= 5


# ---- azure: access and errors ----------------------------------------------------------------------------------------


def test_azure_access_check_all_granted(ent, az):
    result = access.evaluate(az)
    assert result["ok"] and result["missing"] == []
    by_name = {s["name"]: s for s in result["steps"]}
    assert by_name["Sign in to Azure Resource Manager"]["state"] == "ok"
    assert by_name["List subscriptions"]["permission"] == "Reader"
    for name in (
        "Read Logic Apps in Contoso Production",
        "Read web apps in Contoso Production",
        "Read Automation accounts in Contoso Production",
        "Read runbooks in ops-automation",
        "Read Logic Apps in Contoso Development",
    ):
        assert by_name[name]["state"] == "ok", name
    assert by_name["Subscription Contoso Legacy"]["state"] == "warning"


def test_azure_a_missing_role_names_the_action_scope_and_fix(ent, az):
    configure(az, simulated_denied=[f"{PROD}/Microsoft.Web/sites"])
    result = access.evaluate(az)
    assert not result["ok"] and result["missing"] == ["Reader"]
    [failed] = steps(result, "failed")
    assert failed["name"] == "Read web apps in Contoso Production" and failed["problem"] == "missing_permission"
    assert failed["code"] == "AuthorizationFailed"
    assert "Microsoft.Web/sites/read" in failed["detail"] and f"/subscriptions/{PROD}" in failed["detail"]
    assert f"Reader to the connection's app on subscription {PROD}" in failed["next"]
    assert azure_simulator.CLIENT_ID not in json.dumps(result)  # Microsoft's message isn't kept
    # A run reads the other subscription and keeps the refused one's observations as they were.
    configure(az, simulated_denied=[])
    discovery.run(az)
    configure(az, simulated_denied=[f"{PROD}/Microsoft.Web/sites"])
    run = discovery.run(az)
    # Only the web apps of that subscription are unread; its Logic Apps and runbooks are still read.
    assert run.status == RunStatus.SUCCEEDED and run.counts["gone"] == 0 and run.counts["not_read"] == 3
    assert any("Microsoft.Web/sites/read" in n and "Reader" in n for n in run.counts["notes"])
    assert obs(az, "payroll-func").data["scope"] == f"sub:{PROD}/type:sites"
    # Refused everywhere it reads: the run fails, naming the action and the fix.
    configure(az, simulated_denied=[PROD, DEV])
    discovery.update_connector(ent["org"], ent["users"]["admin"], az, {"scope": {"subscriptions": [PROD, DEV]}})
    az.refresh_from_db()
    run = discovery.run(az)
    assert run.status == RunStatus.FAILED and "Microsoft.Resources/subscriptions/read" in run.error
    configure(
        az,
        simulated_denied=[
            f"{sub}/{t}"
            for sub in (PROD, DEV)
            for t in ("Microsoft.Web/sites", "Microsoft.Logic/workflows", "Microsoft.Automation/automationAccounts")
        ],
    )
    run = discovery.run(az)
    assert run.status == RunStatus.FAILED and "AuthorizationFailed" in run.error and "Reader" in run.error


def test_azure_other_resource_types_still_update_when_one_is_refused(ent, az):
    discovery.run(az)
    configure(az, simulated_denied=[f"{PROD}/Microsoft.Web/sites"])
    change(az, {"op": "state", "id": "invoice-intake", "value": "Disabled"}, {"op": "remove", "id": "payroll-func"})
    run = discovery.run(az)
    assert run.counts["changed"] == 1 and obs(az, "invoice-intake").data["state"] == "Disabled"
    assert obs(az, "payroll-func").gone_at is None  # its list wasn't read: not "no longer seen"
    assert run.counts["not_read"] == 3 and run.counts["gone"] == 0


def test_azure_an_unregistered_resource_provider_is_an_empty_list(ent, az):
    discovery.run(az)
    configure(az, simulated_unregistered=[f"{PROD}/Microsoft.Automation/automationAccounts"])
    run = discovery.run(az)
    assert run.status == RunStatus.SUCCEEDED and run.counts["gone"] == 2  # no provider, so no runbooks
    assert any("resource provider isn't registered" in n for n in run.counts["notes"])
    result = access.evaluate(az)
    assert result["ok"]
    step = next(s for s in result["steps"] if s["name"] == "Read Automation accounts in Contoso Production")
    assert step["state"] == "ok" and "isn't registered" in step["detail"]


def test_azure_subscription_not_found_is_a_missing_reader(ent, az):
    """Microsoft Learn: Subscriptions - Get answers 404 SubscriptionNotFound for a subscription the app has no role on."""
    discovery.update_connector(ent["org"], ent["users"]["admin"], az, {"scope": {"subscriptions": [PROD, DEV]}})
    az.refresh_from_db()
    configure(az, simulated_denied=[DEV], simulated_not_found=True)
    result = access.evaluate(az)
    [failed] = steps(result, "failed")
    assert failed["name"] == f"Read subscription {DEV}" and failed["problem"] == "missing_permission"
    assert failed["code"] == "SubscriptionNotFound" and "no Reader on this subscription" in failed["detail"]
    assert f"subscription {DEV}" in failed["next"] and result["missing"] == ["Reader"]
    run = discovery.run(az)
    assert run.status == RunStatus.SUCCEEDED
    assert any("no Reader on this subscription, or the ID is wrong" in n and DEV in n for n in run.counts["notes"])


def test_azure_a_chosen_subscription_without_a_role_is_noted(ent, az):
    discovery.update_connector(ent["org"], ent["users"]["admin"], az, {"scope": {"subscriptions": [PROD, DEV]}})
    az.refresh_from_db()
    configure(az, simulated_denied=[DEV])
    result = access.evaluate(az)
    failed = {s["name"]: s for s in steps(result, "failed")}
    assert "Microsoft.Resources/subscriptions/read" in failed[f"Read subscription {DEV}"]["detail"]
    run = discovery.run(az)
    assert run.status == RunStatus.SUCCEEDED and "invoice-intake" in names(az)
    assert any(DEV in n for n in run.counts["notes"])


def test_azure_no_role_anywhere(ent, az):
    configure(az, simulated_no_role=True)
    run = discovery.run(az)
    assert run.status == RunStatus.FAILED and "no subscription" in run.error and "Reader" in run.error
    result = access.evaluate(az)
    [failed] = steps(result, "failed")
    assert failed["name"] == "List subscriptions" and failed["problem"] == "missing_permission"


def test_azure_429_honours_retry_after(az, no_sleep):
    configure(
        az,
        simulated_throttle={
            "path": f"/subscriptions/{PROD}/providers/Microsoft.Logic/workflows",
            "times": 2,
            "retry_after": "5",
        },
    )
    run = discovery.run(az)
    assert run.status == RunStatus.SUCCEEDED and no_sleep["slept"] == [5.0, 5.0]
    configure(az, simulated_throttle={"path": "/subscriptions", "times": 1, "retry_after": "900"})
    run = discovery.run(az)
    assert run.status == RunStatus.FAILED and "Azure Resource Manager asked to wait 900 seconds" in run.error


def test_azure_a_next_link_to_another_host_is_refused(az):
    configure(az, simulated_page=1, simulated_foreign_next=True)
    transport = azure_simulator.Transport(az)
    run = discovery.run(az, transport=transport)
    assert run.status == RunStatus.FAILED and "unexpected host" in run.error
    assert not any("evil.example.com" in url for _, url in transport.calls)
    assert Observation.objects.filter(connector=az).count() == 0


def test_azure_creates_a_component_prefilled_from_the_resource(ent, az):
    discovery.run(az)
    func = obs(az, "payroll-func")
    _, record = discovery.create_record(ent["org"], ent["users"]["editor"], func, {})
    record = Resource.objects.select_related("component").get(id=record.id)
    assert record.kind == "component" and record.component.component_type == "function"
    assert record.component.hosting == "azure" and record.component.environment == "prod"
    assert record.component.azure_resource_id == func.data["resource_id"]


# ---- powerplatform ---------------------------------------------------------------------------------------------------


def test_powerplatform_lists_copilot_studio_agents_as_ai_agent_candidates(pp):
    run = discovery.run(pp)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert (run.counts["seen"], run.counts["kept"], run.counts["skipped"]) == (4, 3, {"deleted": 1})
    assert names(pp) == ["Benefits Pilot", "HR Helpdesk", "IT Service Desk"]
    hr = obs(pp, "HR Helpdesk")
    assert hr.kind == ObservationKind.AI_AGENT and hr.ai_agent and hr.data["agent"] == "copilot_studio"
    assert hr.external_id == f"{CONTOSO}:{pp_sim._id(CONTOSO, 'HR Helpdesk')}"
    assert hr.data["environment_url"] == f"https://{CONTOSO}" and hr.data["schema_name"] == "cr6f1_hrHelpdesk"
    assert (hr.data["state"], hr.data["status"], hr.data["authentication"]) == ("Active", "Provisioned", "Integrated")
    assert hr.data["owner_name"] == "Priya Raman"
    assert not Observation.objects.filter(display_name="Old FAQ bot").exists()
    assert not Observation.objects.filter(kind=ObservationKind.COMPONENT).exists()  # flows are off by default


def test_powerplatform_cloud_flows_are_optional_components(ent, pp):
    discovery.update_connector(ent["org"], ent["users"]["admin"], pp, {"scope": {"include_cloud_flows": True}})
    pp.refresh_from_db()
    configure(pp, simulated_ignore_select=True)  # rows with every column, clientdata included
    transport = pp_sim.Transport(pp)
    run = discovery.run(pp, transport=transport)
    assert run.counts["kept"] == 5
    flow = obs(pp, "Approve expenses")
    assert flow.kind == ObservationKind.COMPONENT and flow.suggested_component_type == "automation_flow"
    assert flow.data["state"] == "Activated" and flow.data["description"] == "Routes expense claims for approval."
    assert not Observation.objects.filter(display_name="Classic workflow").exists()
    assert any("category%20eq%205" in url for _, url in transport.calls)
    assert "flow-client-data-secret" not in json.dumps(list(Observation.objects.values("data")))


def test_powerplatform_rename_is_changed_and_delete_is_gone(ent, pp):
    discovery.run(pp)
    discovery.dismiss(ent["org"], ent["users"]["editor"], obs(pp, "IT Service Desk"), {"reason": "Pilot"})
    assert discovery.run(pp).counts["unchanged"] == 3
    # HR Helpdesk, not the dev environment's only agent: an environment whose agent list comes back empty while it
    # had agents is refused by the empty-listing guard (tests/test_discovery_empty_listing.py).
    change(pp, {"op": "rename", "id": "IT Service Desk", "value": "IT Help"}, {"op": "remove", "id": "HR Helpdesk"})
    run = discovery.run(pp)
    assert (run.counts["new"], run.counts["changed"], run.counts["gone"]) == (0, 1, 1)
    assert discovery.status_of(obs(pp, "IT Help")) == "changed"
    assert discovery.status_of(obs(pp, "HR Helpdesk")) == "gone"


def test_powerplatform_an_environment_without_an_application_user_keeps_its_agents(pp):
    discovery.run(pp)
    configure(pp, simulated_app_users=[CONTOSO])
    run = discovery.run(pp)
    assert run.status == RunStatus.SUCCEEDED and run.counts["gone"] == 0 and run.counts["not_read"] == 1
    assert obs(pp, "Benefits Pilot").gone_at is None
    [note] = [n for n in run.counts["notes"] if CONTOSO_DEV in n]
    assert "application user" in note and "401" in note
    configure(pp, simulated_app_users=[])
    run = discovery.run(pp)
    assert run.status == RunStatus.FAILED and "application user" in run.error


def test_powerplatform_a_missing_privilege_is_named(ent, pp):
    configure(pp, simulated_privileges={CONTOSO: [], CONTOSO_DEV: []})
    run = discovery.run(pp)
    assert run.status == RunStatus.FAILED and "prvReadbot" in run.error and "application user" in run.error
    result = access.evaluate(pp)
    assert not result["ok"] and result["missing"] == ["prvReadbot"]
    failed = steps(result, "failed")
    assert {s["name"] for s in failed} == {f"Read agents in {CONTOSO}", f"Read agents in {CONTOSO_DEV}"}
    assert all("missing privilege prvReadbot" in s["detail"] and "Application users" in s["next"] for s in failed)


def test_powerplatform_unreadable_flows_are_a_note(ent, pp):
    discovery.update_connector(ent["org"], ent["users"]["admin"], pp, {"scope": {"include_cloud_flows": True}})
    pp.refresh_from_db()
    configure(pp, simulated_privileges={CONTOSO: ["prvReadbot"]})
    run = discovery.run(pp)
    assert run.status == RunStatus.SUCCEEDED and run.counts["kept"] == 3
    assert any("Cloud flows" in n and "prvReadWorkflow" in n for n in run.counts["notes"])
    result = access.evaluate(pp)
    assert result["ok"] and result["optional_missing"] == ["prvReadWorkflow"]


def test_powerplatform_access_check(ent, pp):
    # The Dataverse privileges are unverified on Microsoft Learn and shown as such; the admin API is optional.
    assert [(p.name, p.verified, p.required) for p in powerplatform.PERMISSIONS] == [
        ("prvReadbot", False, True),
        ("prvReadWorkflow", False, False),
        ("Power Platform management app", True, False),
    ]
    result = access.evaluate(pp)
    assert result["ok"], result
    names_ = [s["name"] for s in result["steps"]]
    assert f"Sign in to Dataverse ({CONTOSO})" in names_ and f"Sign in to Dataverse ({CONTOSO_DEV})" in names_
    assert {s["name"] for s in steps(result, "not_used")} == {
        f"Read cloud flows in {CONTOSO}",
        f"Read cloud flows in {CONTOSO_DEV}",
    }
    configure(pp, simulated_app_users=[CONTOSO])
    result = access.evaluate(pp)
    [failed] = steps(result, "failed")
    assert failed["name"] == f"Read agents in {CONTOSO_DEV}" and failed["problem"] == "missing_permission"
    assert failed["permission"] == "prvReadbot"


def test_powerplatform_needs_an_environment(ent):
    org, admin = ent["org"], ent["users"]["admin"]
    connector = make(org, admin, "powerplatform")
    run = discovery.run(connector)
    assert run.status == RunStatus.FAILED and "No environment is chosen" in run.error
    assert [s["problem"] for s in steps(access.evaluate(connector), "failed")] == ["not_configured"]
    for bad in (
        "https://evil.example.com",
        "http://contoso.crm.dynamics.com",
        "https://contoso.crm.dynamics.com/main.aspx",
        "https://user@contoso.crm.dynamics.com",
        "https://contoso.crm.dynamics.com:8443",
        "https://contoso.crm.dynamics.com.evil.example",
    ):
        with pytest.raises(FieldErrors) as e:
            discovery.update_connector(org, admin, connector, {"scope": {"environment_urls": [bad]}})
        assert "scope.environment_urls" in e.value.errors, bad
    discovery.update_connector(org, admin, connector, {"scope": {"environment_urls": [f"https://{CONTOSO.upper()}/"]}})
    connector.refresh_from_db()
    assert connector.scope["environment_urls"] == [f"https://{CONTOSO}"]


def test_powerplatform_all_environments_through_the_admin_api(ent):
    org, admin = ent["org"], ent["users"]["admin"]
    connector = make(org, admin, "powerplatform", {"all_environments": True})
    transport = pp_sim.Transport(connector)
    run = discovery.run(connector, transport=transport)
    assert run.status == RunStatus.SUCCEEDED and run.counts["kept"] == 3
    assert any("no Dataverse database" in n for n in run.counts["notes"])
    hr = obs(connector, "HR Helpdesk")
    assert hr.data["environment_name"] == "Contoso (default)"
    assert hr.data["environment_id"] == pp_sim.ENVIRONMENTS[0][0]
    _, record = discovery.create_record(org, ent["users"]["editor"], hr, {})
    agent = Resource.objects.select_related("ai_agent").get(id=record.id).ai_agent
    assert (agent.platform, agent.external_id, agent.pp_environment_id, agent.auth_mode) == (
        "copilot_studio",
        "cr6f1_hrHelpdesk",
        pp_sim.ENVIRONMENTS[0][0],
        "user",
    )
    # An instance URL that isn't a Dataverse host is never called.
    configure(connector, simulated_foreign_instance=True)
    transport = pp_sim.Transport(connector)
    run = discovery.run(connector, transport=transport)
    assert any("isn't a Dataverse host" in n for n in run.counts["notes"])
    assert not any("evil.example" in url for _, url in transport.calls)
    # Not registered for the admin API: the run fails (nothing would be known to be gone) and the check names it.
    configure(connector, simulated_bap_registered=False)
    run = discovery.run(connector)
    assert run.status == RunStatus.FAILED and "Power Platform admin API refused" in run.error
    result = access.evaluate(connector)
    assert not result["ok"] and result["missing"] == ["Power Platform management app"]


def test_powerplatform_an_unknown_environment_host_is_aadsts500011(ent):
    connector = make(
        ent["org"], ent["users"]["admin"], "powerplatform", {"environment_urls": ["https://nope.crm.dynamics.com"]}
    )
    run = discovery.run(connector)
    assert run.status == RunStatus.FAILED and "AADSTS500011" in run.error
    sign_in = next(s for s in access.evaluate(connector)["steps"] if s["name"].startswith("Sign in to Dataverse"))
    assert sign_in["state"] == "failed" and sign_in["problem"] == "unavailable"


def test_powerplatform_a_next_link_to_another_host_is_refused(pp):
    configure(pp, simulated_page=1, simulated_foreign_next=True)
    transport = pp_sim.Transport(pp)
    run = discovery.run(pp, transport=transport)
    # Contoso has three agents (a next link, refused); Contoso Dev one (no next link, read).
    assert run.status == RunStatus.SUCCEEDED and names(pp) == ["Benefits Pilot"]
    assert any(CONTOSO in n and "unexpected host" in n for n in run.counts["notes"])
    assert not any("evil.example.com" in url for _, url in transport.calls)
    configure(pp, simulated_app_users=[CONTOSO])  # and nothing else readable: the run fails
    assert discovery.run(pp).status == RunStatus.FAILED


def test_powerplatform_one_unreachable_environment_doesnt_fail_the_run(pp):
    discovery.run(pp)
    configure(pp, simulated_unreachable=[CONTOSO_DEV])
    run = discovery.run(pp)
    assert run.status == RunStatus.SUCCEEDED and run.counts["not_read"] == 1 and run.counts["gone"] == 0
    assert any(f"Couldn't reach Dataverse ({CONTOSO_DEV})" in n for n in run.counts["notes"])


def test_powerplatform_the_run_deadline_still_stops_the_whole_run(pp, monkeypatch, no_sleep):
    original = pp_sim.Transport.send

    def slow(self, method, url, headers, body=None):
        no_sleep["now"] += 25 * 60  # every answer takes 25 minutes
        return original(self, method, url, headers, body)

    monkeypatch.setattr(pp_sim.Transport, "send", slow)
    run = discovery.run(pp)
    assert run.status == RunStatus.FAILED and "40-minute limit" in run.error


def test_powerplatform_all_environments_with_none_to_read_fails(ent):
    org, admin = ent["org"], ent["users"]["admin"]
    connector = make(org, admin, "powerplatform", {"all_environments": True})
    discovery.run(connector)
    configure(connector, simulated_no_instances=True)
    run = discovery.run(connector)
    assert run.status == RunStatus.FAILED and "no environment with a Dataverse database" in run.error
    assert "no Dataverse database" in run.error.split("read.", 1)[1]  # the notes come with it
    assert not Observation.objects.filter(connector=connector, gone_at__isnull=False).exists()


def test_powerplatform_admin_api_401_is_a_missing_registration(ent):
    connector = make(ent["org"], ent["users"]["admin"], "powerplatform", {"all_environments": True})
    configure(connector, simulated_bap_registered=False, simulated_bap_status=401)
    run = discovery.run(connector)
    assert run.status == RunStatus.FAILED and "isn't registered" in run.error and "adminApplications" in run.error
    result = access.evaluate(connector)
    [failed] = steps(result, "failed")
    assert failed["problem"] == "missing_permission" and result["missing"] == ["Power Platform management app"]


def test_rows_without_an_id_are_skipped_and_counted(ent, pp, hl):
    configure(pp, simulated_no_ids=True)
    run = discovery.run(pp)
    assert run.counts["skipped"]["no_id"] == 2 and run.counts["kept"] == 1  # each environment's first agent
    configure(hl, simulated_no_id=True)
    run = discovery.run(hl)
    assert run.counts["skipped"]["no_id"] == 1 and not Observation.objects.filter(display_name="Mystery asset").exists()


def test_halo_paging_stops_on_a_repeated_page(hl):
    configure(hl, simulated_size=150, simulated_ignore_page_no=True)
    run = discovery.run(hl)
    assert run.status == RunStatus.SUCCEEDED and run.counts["seen"] == 100
    assert any("same assets as page 1" in n for n in run.counts["notes"])


def test_patterns_match_whole_values():
    assert graph_source.DATAVERSE_HOST.fullmatch("contoso.crm.dynamics.com\n") is None
    assert graph_source.dataverse_host("https://contoso.crm.dynamics.com\n.evil") == ""
    assert azure.subscription({"subscriptionId": f"{PROD}\n"}) is None
    assert (
        azure.ACCOUNT_ID.fullmatch(
            f"/subscriptions/{PROD}/resourceGroups/rg/providers/Microsoft.Automation/automationAccounts/a\n"
        )
        is None
    )


def test_powerplatform_429_honours_retry_after(pp, no_sleep):
    configure(pp, simulated_throttle={"path": "/api/data/v9.2/bots", "times": 1, "retry_after": "3"})
    run = discovery.run(pp)
    assert run.status == RunStatus.SUCCEEDED and no_sleep["slept"] == [3.0]


# ---- halo ------------------------------------------------------------------------------------------------------------


def test_halo_lists_assets_with_their_halo_ids(hl):
    run = discovery.run(hl)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert (run.counts["seen"], run.counts["kept"], run.counts["skipped"]) == (7, 6, {"inactive": 1})
    acrobat = obs(hl, "Adobe Acrobat Pro")
    assert acrobat.kind == ObservationKind.APPLICATION and acrobat.external_id == "101"
    assert acrobat.data["halo_asset_id"] == "101" and acrobat.data["asset_type"] == "Software"
    assert obs(hl, "LAPTOP-0042").data["inventory_number"] == "LAPTOP-0042"  # no key field: the inventory number
    assert discovery.run(hl).counts["unchanged"] == 6


def test_halo_pages_and_filters(ent, hl):
    configure(hl, simulated_size=150)
    transport = halo_simulator.Transport(hl)
    run = discovery.run(hl, transport=transport)
    assert run.counts["seen"] == 157 and sum("page_no=" in u for _, u in transport.calls) == 2
    discovery.update_connector(
        ent["org"], ent["users"]["admin"], hl, {"scope": {"asset_types": ["software"], "exclude_inactive": False}}
    )
    hl.refresh_from_db()
    run = discovery.run(hl)
    assert run.counts["skipped"] == {"asset_types": 3} and "Visio 2016" in names(hl)


def test_halo_rename_is_changed_and_delete_is_gone(ent, hl):
    discovery.run(hl)
    discovery.dismiss(ent["org"], ent["users"]["editor"], obs(hl, "Salesforce"), {"reason": "SaaS"})
    change(
        hl,
        {"op": "rename", "id": "Salesforce", "value": "Salesforce Sales Cloud"},
        {"op": "remove", "id": "Jira Cloud"},
    )
    run = discovery.run(hl)
    assert (run.counts["changed"], run.counts["gone"]) == (1, 1)
    assert discovery.status_of(obs(hl, "Salesforce Sales Cloud")) == "changed"
    _, record = discovery.create_record(ent["org"], ent["users"]["editor"], obs(hl, "Adobe Acrobat Pro"), {})
    assert record.kind == "application"


def test_halo_access_check(ent, hl):
    result = access.evaluate(hl)
    assert result["ok"] and [s["name"] for s in result["steps"]] == ["Connection", "Sign in to Halo", "Read assets"]
    configure(hl, simulated_denied=True)
    result = access.evaluate(hl)
    [failed] = steps(result, "failed")
    assert failed["permission"] == "read:assets" and failed["problem"] == "missing_permission"
    run = discovery.run(hl)
    assert run.status == RunStatus.FAILED and "Halo refused to list assets (403" in run.error
    connections.update_connection(ent["org"], ent["users"]["admin"], hl.halo_connection, {"secret": "wrong-secret"})
    result = access.evaluate(Connector.objects.get(pk=hl.pk))
    sign_in = next(s for s in result["steps"] if s["name"] == "Sign in to Halo")
    assert sign_in["state"] == "failed" and sign_in["problem"] == "wrong_credentials"


def test_halo_429_honours_retry_after(hl, no_sleep):
    configure(hl, simulated_throttle={"path": "/api/Asset", "times": 2, "retry_after": "4"})
    assert discovery.run(hl).status == RunStatus.SUCCEEDED and no_sleep["slept"] == [4.0, 4.0]


# ---- capabilities ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "capability"),
    [("azure", "discovery.azure"), ("powerplatform", "discovery.powerplatform"), ("halo", "discovery.halo")],
)
def test_each_source_needs_its_capability(a, api, source, capability):
    """Every edition has these sources (community included); a platform revoke of one takes it away, and every route
    and service of its connectors then refuses, naming it."""
    from core.models import CapabilityOverride

    org, admin = a["org"], a["users"]["admin"]
    assert org.edition == "community"
    connector = make(org, admin, source, {"environment_urls": URLS} if source == "powerplatform" else None)
    CapabilityOverride.objects.create(organization=org, capability=capability, effect="revoke", reason="test")
    caps.forget_organization(org.pk)
    connector = Connector.objects.select_related("organization").get(pk=connector.pk)
    client = api(admin)
    created = client.post("connectors", {"source": source, "name": "Another"})
    for response in (
        created,
        client.post(f"connectors/{connector.id}/run"),
        client.post(f"connectors/{connector.id}/access-check"),
    ):
        assert response.status_code == 403, response.json_body
        assert response.json_body["type"] == "/problems/capability-required"
        assert response.json_body["capability"] == capability
    patched = client.patch(f"connectors/{connector.id}", {"name": "Renamed"})
    assert patched.status_code == 403 and patched.json_body["capability"] == capability
    with pytest.raises(CapabilityRequired):
        discovery.update_connector(org, admin, connector, {"name": "Renamed"})
    with pytest.raises(CapabilityRequired):
        access.evaluate(connector)
    run = discovery.run(connector)
    assert run.status == RunStatus.FAILED and capability in run.error
    assert not Connector.objects.get(pk=connector.pk).run_requested_at


# ---- per-audience tokens and host allow-lists ------------------------------------------------------------------------


def test_each_audience_gets_its_own_token_and_it_goes_only_to_its_hosts(ent):
    org, admin = ent["org"], ent["users"]["admin"]
    connector = make(org, admin, "powerplatform", {"all_environments": True, "include_cloud_flows": True})
    transport = pp_sim.Transport(connector)
    assert discovery.run(connector, transport=transport).status == RunStatus.SUCCEEDED
    scopes = [url for method, url in transport.calls if method == "POST"]
    assert len(scopes) == 3  # one token each: the admin API and two environments
    for host, bearer in transport.sent:
        token = bearer.removeprefix("Bearer ")
        if host == "api.bap.microsoft.com":
            assert token == BAP_TOKEN
        else:
            assert token == DATAVERSE_TOKEN + host
    assert {h for h, _ in transport.sent} == {"api.bap.microsoft.com", CONTOSO, CONTOSO_DEV}
    azure_transport = azure_simulator.Transport()
    az = make(org, admin, "azure", conn=connector.microsoft_connection, name="Azure")
    discovery.run(az, transport=azure_transport)
    assert {(h, b) for h, b in azure_transport.sent} == {("management.azure.com", f"Bearer {ARM_TOKEN}")}


def test_a_token_is_refused_for_another_audience_by_the_simulator():
    transport = pp_sim.Transport(config={})
    reply = transport.send(
        "GET", f"https://{CONTOSO}/api/data/v9.2/bots", {"Authorization": f"Bearer {DATAVERSE_TOKEN}{CONTOSO_DEV}"}
    )
    assert reply.status == 401


def test_audiences_allow_only_their_own_origins():
    arm = graph_source.ARM_AUDIENCE
    assert arm.allows("https://management.azure.com/subscriptions?api-version=1")
    for url in (
        "http://management.azure.com/subscriptions",
        "https://management.azure.com:8443/subscriptions",
        "https://user:pw@management.azure.com/subscriptions",
        "https://graph.microsoft.com/v1.0/applications",
        "https://management.azure.com.evil.example/x",
        "https://evil.example/https://management.azure.com/",
    ):
        assert not arm.allows(url), url
    contoso = graph_source.dataverse(CONTOSO)
    assert contoso.scope == f"https://{CONTOSO}/.default" and contoso.hosts == {CONTOSO}
    assert not contoso.allows(f"https://{CONTOSO_DEV}/api/data/v9.2/bots")
    assert graph_source.BAP_AUDIENCE.scope == "https://service.powerapps.com/.default"
    assert graph_source.BAP_AUDIENCE.hosts == {"api.bap.microsoft.com"}
    with pytest.raises(graph_source.SourceError):
        graph_source.dataverse("evil.example.com")
    on_prem = graph_source.halo_audience("https://halo.internal.example:8443/halo")
    assert on_prem.allows("https://halo.internal.example:8443/halo/api/Asset")
    assert not on_prem.allows("https://halo.internal.example/halo/api/Asset")
    assert graph_source.dataverse_host("https://org.crm11.dynamics.com/") == "org.crm11.dynamics.com"
    assert graph_source.dataverse_host("https://org.dynamics.com") == ""


def test_the_real_transport_for_an_audience_reaches_only_sign_in_and_its_hosts():
    credential = connections.MicrosoftCredential("00000000-0000-4000-8000-000000000001", "c", "s")
    make_transport = graph_source.transports(None, credential, azure.simulator)
    real = make_transport(graph_source.ARM_AUDIENCE.hosts)
    assert isinstance(real, graph.HttpTransport) and real.hosts == {graph.LOGIN_HOST, "management.azure.com"}
    for url in ("https://graph.microsoft.com/v1.0/users", "https://management.azure.com:444/subscriptions"):
        with pytest.raises(graph.HostRefused):
            real.send("GET", url, {})
    dataverse = make_transport(graph_source.dataverse(CONTOSO).hosts)
    assert dataverse.hosts == {graph.LOGIN_HOST, CONTOSO}


def test_a_client_never_sends_its_token_off_its_audience():
    transport = azure_simulator.Transport(config={})
    credential = connections.MicrosoftCredential("simulated", "", SECRET)
    client = graph_source.Client(transport, credential, audience=graph_source.ARM_AUDIENCE)
    with pytest.raises(graph_source.SourceError, match="unexpected host"):
        client.get(f"https://{CONTOSO}/api/data/v9.2/bots", "read", None)
    assert transport.calls == []  # refused before signing in or sending anything
    client.get("https://management.azure.com/subscriptions?api-version=2022-12-01", "list subscriptions", None)
    token_form = [url for method, url in transport.calls if method == "POST"]
    assert len(token_form) == 1 and transport.sent == [("management.azure.com", f"Bearer {ARM_TOKEN}")]


def test_arm_authorization_failed_parsing_is_strict():
    ok = (
        "The client 'x' with object id 'y' does not have authorization to perform action "
        f"'Microsoft.Web/sites/read' over scope '/subscriptions/{PROD}/resourceGroups/rg' or the scope is invalid."
    )
    assert graph_source.authorization_failed(ok) == (
        "Microsoft.Web/sites/read",
        f"/subscriptions/{PROD}/resourceGroups/rg",
    )
    sneaky = ok.replace("Microsoft.Web/sites/read", "Bearer eyJ0eXAi.secret")
    assert graph_source.authorization_failed(sneaky) == ("", "")
    assert graph_source.subscription_of(f"/subscriptions/{PROD}/resourceGroups/rg") == PROD


# ---- roles, tenancy, API shapes --------------------------------------------------------------------------------------


def test_roles_on_the_new_sources(ent, api, az, hl):
    for user in ("viewer", "editor"):
        client = api(ent["users"][user])
        assert client.post("connectors", {"source": "azure", "name": "x"}).status_code == 403
        assert client.post(f"connectors/{az.id}/run").status_code == 403
        assert client.post(f"connectors/{hl.id}/access-check").status_code == 403
        body = client.get(f"connectors/{az.id}").json_body
        assert body["source"] == "azure" and body["permissions"][0]["kind"] == "azure_rbac"
        assert body["permissions"][0]["role_id"] == "acdd72a7-3385-48ef-bd42-f606fba81ae7"
    admin = api(ent["users"]["admin"])
    assert admin.post(f"connectors/{az.id}/run").status_code == 202
    checked = admin.post(f"connectors/{hl.id}/access-check")
    assert checked.status_code == 200 and checked.json_body["access_check"]["ok"]
    halo_body = admin.get(f"connectors/{hl.id}").json_body
    assert halo_body["connection"]["kind"] == "halo" and halo_body["simulated"]
    assert halo_body["permissions"][0]["name"] == "read:assets" and not halo_body["permissions"][0]["verified"]
    assert set(halo_body["filters"]) == {"no_id", "asset_types", "inactive", "name_exclusions"}


def test_the_api_creates_sources_b_connectors_and_shows_candidates(ent, api):
    org, admin = ent["org"], ent["users"]["admin"]
    ms, ha = connection(org, admin, "microsoft"), connection(org, admin, "halo")
    client = api(admin)
    made = client.post(
        "connectors",
        {"source": "powerplatform", "microsoft_connection": str(ms.id), "scope": {"environment_urls": URLS}},
    )
    assert made.status_code == 201, made.json_body
    assert (
        made.json_body["scope"]["environment_urls"] == sorted(URLS) and not made.json_body["scope"]["all_environments"]
    )
    halo_made = client.post("connectors", {"source": "halo", "halo_connection": str(ha.id)})
    assert halo_made.status_code == 201, halo_made.json_body
    wrong = client.post("connectors", {"source": "halo", "name": "Wrong", "microsoft_connection": str(ms.id)})
    assert wrong.status_code == 400
    discovery.run(Connector.objects.get(pk=made.json_body["id"]))
    page = api(ent["users"]["viewer"]).get(f"discovery/candidates?connector={made.json_body['id']}").json_body
    hr = next(r for r in page["results"] if r["name"] == "HR Helpdesk")
    assert hr["kind"] == "ai_agent" and hr["agent"] == "copilot_studio" and hr["source"] == "powerplatform"
    assert hr["details"]["environment_url"] == f"https://{CONTOSO}" and hr["enterprise_app"] is False


def test_another_organization_cannot_use_or_see_the_connectors(ent, b, api, az, hl):
    other = b["org"]
    other, _ = caps.set_edition(other, "enterprise")
    with pytest.raises(FieldErrors) as e:
        discovery.create_connector(
            other, b["users"]["admin"], {"source": "azure", "microsoft_connection": str(az.microsoft_connection_id)}
        )
    assert "microsoft_connection" in e.value.errors
    with pytest.raises(FieldErrors):
        discovery.create_connector(
            other, b["users"]["admin"], {"source": "halo", "halo_connection": str(hl.halo_connection_id)}
        )
    for url in (f"connectors/{az.id}", f"connectors/{hl.id}/runs"):
        assert api(b["users"]["admin"]).get(url).status_code == 404
    assert api(b["users"]["admin"]).post(f"connectors/{az.id}/access-check").status_code == 404
    discovery.run(az)
    theirs = make(other, b["users"]["admin"], "azure")
    configure(theirs, simulator_changes=[{"op": "remove", "id": "invoice-intake"}])
    discovery.run(theirs)
    assert Observation.objects.filter(organization=ent["org"], gone_at__isnull=False).count() == 0
    assert Observation.objects.filter(organization=other, display_name="invoice-intake").count() == 0
    assert all(o.organization_id == other.id for o in Observation.objects.filter(connector=theirs))


# ---- no token or secret leaks ----------------------------------------------------------------------------------------


def test_no_token_or_secret_in_runs_logs_audit_or_responses(ent, api, az, pp, hl, caplog):
    caplog.set_level(logging.DEBUG)
    org, admin = ent["org"], ent["users"]["admin"]
    configure(az, simulated_errors={f"/subscriptions/{PROD}/providers/Microsoft.Logic/workflows": 400})
    configure(pp, simulated_errors={"/api/data/v9.2/bots": 400})
    configure(hl, simulated_errors={"/api/Asset": 400})
    for connector in (az, pp, hl):
        run = discovery.run(connector)
        assert run.status in (RunStatus.FAILED, RunStatus.SUCCEEDED) and "400" in (run.error + json.dumps(run.counts))
        access.check_access(org, admin, connector)
    for connector in (az, pp):
        configure(connector, simulated_errors={}, simulated_token_echo=True)
        echoed = discovery.run(connector)
        assert echoed.status == RunStatus.FAILED and SECRET not in echoed.error
        access.check_access(org, admin, connector)
        configure(connector, simulated_token_echo=False)
    configure(hl, simulated_errors={})
    for connector in (az, pp, hl):
        discovery.run(connector)
        discovery.request_run(org, admin, connector)
    from core import jobs
    from core.models import Job, Schedule

    summary = jobs.summarize(discovery.scheduled_runs(None))
    texts = [
        json.dumps(summary, default=str),
        caplog.text,
        json.dumps(list(ConnectorRun.objects.values("error", "counts"))),
        json.dumps(list(Connector.objects.values("access_check", "config", "scope"))),
        json.dumps(list(AuditEvent.objects.values("changes", "prechange", "postchange")), default=str),
        json.dumps(list(Observation.objects.values("data", "display_name", "external_id")), default=str),
        json.dumps(list(Schedule.objects.values("last_result", "last_error")), default=str),
        json.dumps(list(Job.objects.values("payload", "result", "last_error")), default=str),
    ]
    for user in ent["users"].values():
        for url in (
            "connectors",
            *(f"connectors/{c.id}/runs" for c in (az, pp, hl)),
            "discovery/candidates?status=all",
        ):
            texts.append(json.dumps(api(user).get(url).json_body))
    secrets = (
        SECRET,
        ARM_TOKEN,
        BAP_TOKEN,
        DATAVERSE_TOKEN,
        halo.TOKEN,
        entra_simulator.TOKEN,
        azure_simulator.DEFINITION_SECRET,
        "flow-client-data-secret",
        "Bearer",
    )
    for text in texts:
        for secret in secrets:
            assert secret not in text, secret
