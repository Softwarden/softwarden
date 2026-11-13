"""AI agents, AI providers and AI models: the ai_agent record kind through the shared
record service path, the reference entities with delete-in-use refusals, the models-in-use report, the agent filters
and ordering, Needs attention's AI agent gaps, exports, relationships, bulk edit and saved filters, and the audit
trail with snapshots. Cross-tenant checks for the new tables are here and in test_tenancy.py; the sweeps in
test_drf_resources.py cover every new route."""

import csv
import io
from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction
from openpyxl import load_workbook

from catalog import ai, services
from catalog.audit_log import TARGET_TYPES
from catalog.models import AgentModel, AIAgent, AIModel, AIProvider, Resource
from core import connections
from core.models import AuditEvent
from notify.models import NotificationSnooze

pytestmark = pytest.mark.django_db


# ---- helpers --------------------------------------------------------------------------------------------------------


def workbook(content):
    return load_workbook(io.BytesIO(content))


def provider(f, name="Fabrikam AI", **extra):
    return ai.create_provider(f["org"], f["users"]["editor"], {"name": name, **extra})


def model(f, prov, name="Fabrikam Large", **extra):
    return ai.create_model(f["org"], f["users"]["editor"], {"provider": str(prov.id), "name": name, **extra})


def agent(f, name="Helper", **data):
    return services.create_resource(
        f["org"], f["users"]["editor"], {"name": name, "kind": "ai_agent", "lifecycle": "active", **data}
    )


@pytest.fixture
def models(a):
    prov = provider(a)
    return {
        "large": model(a, prov, "Fabrikam Large", version="2", modality="text", hosting_region="EU"),
        "vision": model(a, prov, "Fabrikam Vision", version="1", modality="multimodal", hosting_region="US"),
    }


def complete(f, models, **extra):
    """Every agent field filled in, as the acceptance check Copilot Studio agent."""
    return {
        "name": "HR Policy Assistant",
        "lifecycle": "active",
        "criticality": "medium",
        "business_owner": str(f["people"]["priya"].id),
        "technical_owner": str(f["people"]["dana"].id),
        "support_team": str(f["team"].id),
        "purpose": "Answers HR policy questions.",
        "platform": "copilot_studio",
        "models_used": [str(models["large"].id), str(models["vision"].id)],
        "environment": "prod",
        "pp_environment_id": "3f2a9c1e-5b7d-4e8f-a1c2-d3e4f5a6b7c8",
        "channels": ["m365_copilot", "teams"],
        "knowledge_sources": "SharePoint: HR Policies",
        "tools_connectors": "Workday connector (read)",
        "auth_mode": "user",
        "run_as_identity": "The signed-in employee",
        "data_access_scope": "HR policy documents only.",
        "human_in_loop": True,
        "actions_can_write": False,
        "risk_classification": "limited",
        "repo_url": "https://git.example.test/hr-assistant",
        "external_id": "cr1a2_hrPolicyAssistant",
        "tags": ["HR"],
        **extra,
    }


# ---- create, read, update through the API -----------------------------------------------------------------------------


def test_an_editor_creates_a_copilot_studio_agent_with_models_channels_and_owners(a, api, models):
    client = api(a["users"]["editor"])
    created = client.post("ai-agents", complete(a, models))
    assert created.status_code == 201, created.json_body
    body = created.json_body
    assert body["kind"] == "ai_agent" and body["platform"] == "copilot_studio"
    assert body["channels"] == ["teams", "m365_copilot"]  # choice order, whatever order was sent
    assert [m["name"] for m in body["models_used"]] == ["Fabrikam Large", "Fabrikam Vision"]
    assert body["models_used"][0] == {
        "id": str(models["large"].id),
        "name": "Fabrikam Large",
        "version": "2",
        "provider": {"id": str(models["large"].provider_id), "name": "Fabrikam AI"},
    }
    assert body["business_owner"]["name"] == "Priya Raman" and body["support_team"]["name"] == "IT Applications"
    assert body["human_in_loop"] is True and body["actions_can_write"] is False
    assert body["tags"] == ["HR"] and body["gaps"] == [] and body["review_state"] == "unreviewed"
    detail = client.get(f"ai-agents/{body['id']}")
    assert detail.status_code == 200 and detail.json_body["archived_at"] is None and detail["ETag"]
    row = AIAgent.objects.get(resource_id=body["id"])
    assert row.organization_id == a["org"].id and row.channels == ["teams", "m365_copilot"]


def test_the_list_shape_matches_applications_and_components_plus_the_agents_fields(a, api, models):
    """The SPA's RecordTable reads the shared record keys; an agent row has all of them."""
    agent(a, "Bot", models_used=[str(models["large"].id)])
    client = api(a["users"]["viewer"])
    row = client.get("ai-agents").json_body["results"][0]
    app_row = client.get("applications").json_body["results"][0]
    shared = set(app_row) - {
        "vendor",
        "built_by",
        "license_model",
        "documentation_url",
        "support_url",
        "category",
        "deployment_model",
        "version",
        "primary_url",
        "admin_url",
        "authentication_method",
        "sso_enabled",
        "scim_provisioning",
        "entra_app_id",
        "service_principal_id",
    }
    assert shared <= set(row)
    assert {"platform", "models_used", "channels", "risk_classification", "human_in_loop"} <= set(row)
    assert set(client.get("ai-agents").json_body) == {"results", "next", "previous"}


def test_a_new_agent_has_no_human_in_loop_decision_and_shows_its_gaps(a, api):
    created = api(a["users"]["editor"]).post("ai-agents", {"name": "Bare", "lifecycle": "active"}).json_body
    assert created["human_in_loop"] is None and created["actions_can_write"] is None
    assert created["models_used"] == [] and created["channels"] == [] and created["platform"] == ""
    assert created["gaps"] == [
        "No business owner",
        "No technical owner",
        "No support team",
        "No criticality",
        "No data access scope",
        "No human-in-loop decision",
    ]


def test_updates_replace_models_and_channels_and_are_audited_with_a_diff(a, api, models):
    client = api(a["users"]["editor"])
    created = client.post("ai-agents", complete(a, models)).json_body
    url = f"ai-agents/{created['id']}"
    changed = client.patch(url, {"models_used": [str(models["vision"].id)], "channels": ["web"], "human_in_loop": None})
    assert changed.status_code == 200, changed.json_body
    body = changed.json_body
    assert [m["name"] for m in body["models_used"]] == ["Fabrikam Vision"] and body["channels"] == ["web"]
    assert body["human_in_loop"] is None
    assert list(AgentModel.objects.filter(agent_id=created["id"]).values_list("model_id", flat=True)) == [
        models["vision"].id
    ]
    event = AuditEvent.objects.filter(action="resource.updated", target_id=created["id"]).latest("created_at")
    assert event.changes["models_used"] == [
        sorted([str(models["large"].id), str(models["vision"].id)]),
        [str(models["vision"].id)],
    ]
    assert event.changes["channels"] == [["teams", "m365_copilot"], ["web"]]
    assert event.changes["human_in_loop"] == [True, None]
    # The snapshots carry the models as identity entries; the History diff shows what left.
    assert event.prechange is not None and event.postchange is not None
    # An AI model is named "name version" in snapshots, so two versions of one model differ in a diff.
    assert {m["display"] for m in event.prechange["models_used"]} == {"Fabrikam Large 2", "Fabrikam Vision 1"}
    assert event.postchange["models_used"] == [{"id": str(models["vision"].id), "display": "Fabrikam Vision 1"}]
    history = client.get(f"{url}/audit").json_body["results"]
    models_part = next(p for p in history[0]["summary"].split("; ") if p.startswith("Models: "))
    before, after = models_part.removeprefix("Models: ").split(" → ")
    assert set(before.split(", ")) == {"Fabrikam Large 2", "Fabrikam Vision 1"} and after == "Fabrikam Vision 1"
    assert "Channels: Teams, Microsoft 365 Copilot → Web" in history[0]["summary"]
    diff = client.get(f"{url}/audit/{event.id}").json_body["diff"]
    rows = {r["field"]: r for r in diff}
    assert [m["display"] for m in rows["models_used"]["removed"]] == ["Fabrikam Large 2"]
    assert rows["channels"]["before"] == ["teams", "m365_copilot"] and rows["channels"]["after"] == ["web"]


def test_creation_is_audited_with_a_snapshot_of_every_agent_field(a, api, models):
    created = api(a["users"]["editor"]).post("ai-agents", complete(a, models)).json_body
    event = AuditEvent.objects.get(action="resource.created", target_id=created["id"])
    assert event.prechange is None and event.postchange is not None
    post = event.postchange
    assert post["kind"] == "ai_agent" and post["platform"] == "copilot_studio"
    assert post["channels"] == ["teams", "m365_copilot"] and post["data_access_scope"] == "HR policy documents only."
    assert len(post["models_used"]) == 2 and post["tags"][0]["display"] == "HR"
    assert post["business_owner"] == {"id": str(a["people"]["priya"].id), "display": None}
    assert "built_by" not in post and "vendor" not in post


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("platform", "skynet", "Choose one of the listed values."),
        ("risk_classification", "extreme", "Choose one of the listed values."),
        ("auth_mode", "password", "Choose one of the listed values."),
        ("environment", "staging", "Choose one of the listed values."),
        ("channels", ["teams", "fax"], "Use any of: teams, m365_copilot, web, api, email, other."),
        ("channels", "teams", "Send channels as a list."),
        ("channels", None, None),  # null clears them
        ("models_used", ["not-a-uuid"], "Choose existing AI models."),
        ("models_used", "x", "Send models as a list of AI model IDs."),
        ("human_in_loop", "maybe", "Use true or false, or leave it empty."),
        ("repo_url", "ftp://example.test/repo", "Enter a full web address starting with http:// or https://."),
        ("run_as_identity", "x" * 201, "Use 200 characters or fewer."),
        ("data_access_scope", "x" * 5001, "Use 5000 characters or fewer."),
    ],
)
def test_agent_fields_are_validated_by_the_service(a, api, field, value, message):
    response = api(a["users"]["editor"]).post("ai-agents", {"name": "Bot", "lifecycle": "active", field: value})
    if message is None:
        assert response.status_code == 201 and response.json_body["channels"] == []
    else:
        assert response.status_code == 400 and response.json_body["errors"][field] == [message]
        assert not Resource.objects.filter(kind="ai_agent").exists()


def test_fields_of_other_kinds_are_ignored_on_an_agent(a, api):
    created = api(a["users"]["editor"]).post(
        "ai-agents", {"name": "Bot", "lifecycle": "active", "vendor": "x", "component_type": "script"}
    )
    assert created.status_code == 201 and "vendor" not in created.json_body


def test_kinds_do_not_cross_endpoints(a, api):
    bot = agent(a)
    client = api(a["users"]["editor"])
    assert client.get(f"applications/{bot.id}").status_code == 404
    assert client.get(f"ai-agents/{a['app'].id}").status_code == 404
    assert client.get(f"components/{bot.id}/reviews").status_code == 404
    assert client.get(f"ai-agents/{a['app'].id}/audit").status_code == 404
    assert client.post("applications", {"name": "X", "lifecycle": "active", "kind": "ai_agent"}).json_body["kind"] == (
        "application"
    )


def test_reviews_archive_restore_and_delete(a, api):
    bot = agent(a)
    editor, admin = api(a["users"]["editor"]), api(a["users"]["admin"])
    review = editor.post(f"ai-agents/{bot.id}/reviews", {"confirmed_by": str(a["people"]["dana"].id), "evidence": "x"})
    assert review.status_code == 201
    assert editor.get(f"ai-agents/{bot.id}").json_body["review_state"] == "confirmed"
    assert editor.post(f"ai-agents/{bot.id}/archive").json_body["archived"] is True
    assert editor.get("ai-agents").json_body["results"] == []
    assert editor.post(f"ai-agents/{bot.id}/restore").json_body["archived"] is False
    assert editor.delete(f"ai-agents/{bot.id}").status_code == 403  # Admins delete records
    refused = admin.delete(f"ai-agents/{bot.id}")
    assert refused.status_code == 409 and refused.json_body["blocking_references"] == {"review": 1}
    spare = agent(a, "Spare")
    assert admin.delete(f"ai-agents/{spare.id}").status_code == 204
    assert not AIAgent.objects.filter(resource_id=spare.id).exists()


# ---- roles ----------------------------------------------------------------------------------------------------------


def test_roles_on_agents_providers_and_models(a, api, models):
    viewer, editor = api(a["users"]["viewer"]), api(a["users"]["editor"])
    assert viewer.post("ai-agents", {"name": "Bot", "lifecycle": "active"}).status_code == 403
    assert viewer.post("ai-providers", {"name": "Contoso Models"}).status_code == 403
    assert viewer.post("ai-models", {"provider": str(models["large"].provider_id), "name": "X"}).status_code == 403
    assert viewer.patch(f"ai-models/{models['large'].id}", {"name": "Renamed"}).status_code == 403
    assert viewer.delete(f"ai-providers/{models['large'].provider_id}").status_code == 403
    for url in ("ai-agents", "ai-providers", "ai-models", "ai-models/in-use", f"ai-models/{models['large'].id}"):
        assert viewer.get(url).status_code == 200, url
    created = editor.post("ai-providers", {"name": "Contoso Models", "website": "https://contoso.example.test"})
    assert created.status_code == 201 and created.json_body["usage"] == 0
    assert editor.delete(f"ai-providers/{created.json_body['id']}").status_code == 204


# ---- providers and models ---------------------------------------------------------------------------------------------


def test_provider_and_model_validation(a, api, models):
    client = api(a["users"]["editor"])
    prov = models["large"].provider
    assert client.post("ai-providers", {"name": "fabrikam ai"}).json_body["errors"]["name"] == [
        "An AI provider named Fabrikam AI already exists."
    ]
    assert client.post("ai-providers", {"name": "X", "website": "not a url"}).json_body["errors"] == {
        "website": ["Enter a full web address starting with http:// or https://."]
    }
    assert client.post("ai-providers", {}).json_body["errors"] == {"name": ["Enter a name."]}
    clash = client.post("ai-models", {"provider": str(prov.id), "name": "FABRIKAM LARGE", "version": "2"})
    assert clash.status_code == 400 and clash.json_body["errors"]["name"] == [
        "Fabrikam AI already has a model named FABRIKAM LARGE 2."
    ]
    other_version = client.post("ai-models", {"provider": str(prov.id), "name": "Fabrikam Large", "version": "3"})
    assert other_version.status_code == 201
    assert client.post("ai-models", {"name": "X"}).json_body["errors"] == {"provider": ["Choose a provider."]}
    bad = client.post("ai-models", {"provider": str(prov.id), "name": "Y", "modality": "smell"})
    assert bad.json_body["errors"]["modality"] == [
        "Use one of: text, multimodal, image, audio, embedding, other, or leave it empty."
    ]
    # A PATCH is judged as a whole: moving the version onto an existing one clashes.
    moved = client.patch(f"ai-models/{other_version.json_body['id']}", {"version": "2"})
    assert moved.status_code == 400 and "name" in moved.json_body["errors"]


def test_provider_and_model_writes_are_audited_with_snapshots(a, api, models):
    client = api(a["users"]["editor"])
    url = f"ai-models/{models['large'].id}"
    assert client.patch(url, {"hosting_region": "Sweden Central"}).status_code == 200
    event = AuditEvent.objects.filter(action="ai_model.updated").latest("created_at")
    assert event.target_type == "AIModel" and event.changes["hosting_region"] == ["EU", "Sweden Central"]
    assert event.prechange is not None and event.postchange is not None
    assert event.prechange["hosting_region"] == "EU" and event.postchange["hosting_region"] == "Sweden Central"
    assert event.postchange["provider"] == {"id": str(models["large"].provider_id), "display": "Fabrikam AI"}
    created = AuditEvent.objects.get(action="ai_provider.created")
    assert created.postchange == {"name": "Fabrikam AI", "website": ""}
    assert {"AIProvider", "AIModel"} <= set(TARGET_TYPES)
    log = api(a["users"]["admin"]).get("audit-events?target_type=AIModel").json_body["results"]
    assert {e["label"] for e in log} == {"AI model added", "AI model changed"}


def test_lists_count_usage_and_filter_models(a, api, models):
    prov2 = provider(a, "Contoso Models")
    model(a, prov2, "Contoso Speech", modality="audio", hosting_region="eu")
    agent(a, "Bot", models_used=[str(models["large"].id)])
    client = api(a["users"]["viewer"])
    providers = {p["name"]: p for p in client.get("ai-providers").json_body}
    assert providers["Fabrikam AI"]["usage"] == 2 and providers["Contoso Models"]["usage"] == 1
    rows = client.get("ai-models").json_body
    assert [m["name"] for m in rows] == ["Contoso Speech", "Fabrikam Large", "Fabrikam Vision"]
    assert {m["name"]: m["usage"] for m in rows}["Fabrikam Large"] == 1

    def names(query):
        response = client.get(f"ai-models?{query}")
        assert response.status_code == 200, response.json_body
        return [m["name"] for m in response.json_body]

    assert names(f"provider={prov2.id}") == ["Contoso Speech"]
    assert names("modality=text,audio") == ["Contoso Speech", "Fabrikam Large"]
    assert names("hosting_region=EU") == ["Contoso Speech", "Fabrikam Large"]  # ignoring case
    assert names("in_use=true") == ["Fabrikam Large"]
    assert names("in_use=false") == ["Contoso Speech", "Fabrikam Vision"]
    assert names("q=vision") == ["Fabrikam Vision"]
    assert names("ordering=-name") == ["Fabrikam Vision", "Fabrikam Large", "Contoso Speech"]
    assert client.get("ai-models?modality=smell").status_code == 400
    assert client.get("ai-models?colour=red").status_code == 400


def test_a_provider_with_models_and_a_model_in_use_are_not_deleted(a, api, models):
    client = api(a["users"]["editor"])
    prov = models["large"].provider
    refused = client.delete(f"ai-providers/{prov.id}")
    assert refused.status_code == 409
    assert refused.json_body["blocking_references"] == {"AI models": 2}
    assert refused.json_body["blocking_names"] == ["Fabrikam Large 2", "Fabrikam Vision 1"]
    bot = agent(a, "Bot", models_used=[str(models["large"].id)])
    services.archive_resource(a["org"], a["users"]["editor"], bot)  # archived agents still count
    in_use = client.delete(f"ai-models/{models['large'].id}")
    assert in_use.status_code == 409 and in_use.json_body["blocking_references"] == {"AI agents": 1}
    assert in_use.json_body["detail"] == (
        "Fabrikam Large can't be deleted: 1 AI agent uses it (Bot). Remove it from them first."
    )
    assert client.delete(f"ai-models/{models['vision'].id}").status_code == 204
    assert client.patch(f"ai-agents/{bot.id}", {"models_used": []}).status_code == 200
    assert client.delete(f"ai-models/{models['large'].id}").status_code == 204
    assert client.delete(f"ai-providers/{prov.id}").status_code == 204
    assert not AIProvider.objects.filter(organization=a["org"]).exists()
    assert {e.action for e in AuditEvent.objects.filter(action__endswith=".deleted")} == {
        "ai_model.deleted",
        "ai_provider.deleted",
    }


# ---- the models-in-use report ------------------------------------------------------------------------------------------


def test_models_in_use_counts_unarchived_agents_most_used_first(a, b, api, models):
    prov = models["large"].provider
    small = model(a, prov, "Fabrikam Small")
    large, vision = str(models["large"].id), str(models["vision"].id)
    agent(a, "One", models_used=[large, vision])
    agent(a, "Two", models_used=[large])
    agent(a, "Three", models_used=[large], lifecycle="evaluating")
    archived = agent(a, "Gone", models_used=[vision, str(small.id)])
    services.archive_resource(a["org"], a["users"]["editor"], archived)
    theirs = provider(b, "Fabrikam AI")
    agent(b, "Theirs", models_used=[str(model(b, theirs, "Fabrikam Large").id)])
    report = api(a["users"]["viewer"]).get("ai-models/in-use")
    assert report.status_code == 200
    assert [(m["name"], m["agents"], m["live_agents"]) for m in report.json_body] == [
        ("Fabrikam Large", 3, 2),
        ("Fabrikam Vision", 1, 1),
    ]  # Fabrikam Small is used only by an archived agent; the other organization's agent never counts
    assert report.json_body[0]["provider"] == {"id": str(prov.id), "name": "Fabrikam AI"}
    assert api(a["users"]["viewer"]).get("ai-models/in-use?provider=x").status_code == 400


# ---- agent filters and ordering ------------------------------------------------------------------------------------------


@pytest.fixture
def fleet(a, models):
    large, vision = str(models["large"].id), str(models["vision"].id)
    return {
        "hr": agent(
            a,
            "HR Assistant",
            platform="copilot_studio",
            channels=["teams"],
            risk_classification="limited",
            human_in_loop=True,
            actions_can_write=False,
            models_used=[large],
            purpose="Answers payroll calendar questions",
        ),
        "invoices": agent(
            a,
            "Invoice Triage",
            platform="claude",
            channels=["email", "api"],
            risk_classification="high",
            actions_can_write=True,
            models_used=[vision],
        ),
        "desk": agent(a, "Desk Helper", platform="openai", channels=["web"], risk_classification="minimal"),
        "rogue": agent(a, "Rogue", platform="custom", risk_classification="unacceptable", human_in_loop=False),
    }


@pytest.mark.parametrize(
    "query,expected",
    [
        ("platform=copilot_studio,claude", ["HR Assistant", "Invoice Triage"]),
        ("channel=teams,api", ["HR Assistant", "Invoice Triage"]),
        ("channel=web", ["Desk Helper"]),
        ("risk_classification=high,unacceptable", ["Invoice Triage", "Rogue"]),
        ("human_in_loop=true", ["HR Assistant"]),
        ("human_in_loop=false", ["Rogue"]),
        ("human_in_loop=unknown", ["Desk Helper", "Invoice Triage"]),
        ("actions_can_write=true", ["Invoice Triage"]),
        ("actions_can_write=unknown", ["Desk Helper", "Rogue"]),
        ("q=payroll", ["HR Assistant"]),  # purpose is searched like an application's
        ("ordering=risk_classification", ["Rogue", "Invoice Triage", "HR Assistant", "Desk Helper"]),
        ("ordering=-risk_classification", ["Desk Helper", "HR Assistant", "Invoice Triage", "Rogue"]),
        ("lifecycle=active&gap=true", ["Desk Helper", "HR Assistant", "Invoice Triage", "Rogue"]),
    ],
)
def test_agent_filters(a, api, fleet, query, expected):
    response = api(a["users"]["viewer"]).get(f"ai-agents?{query}")
    assert response.status_code == 200, response.json_body
    names = [r["name"] for r in response.json_body["results"]]
    assert names == expected if "ordering" in query else sorted(names) == expected


def test_model_and_provider_filters(a, b, api, fleet, models):
    client = api(a["users"]["viewer"])
    assert [r["name"] for r in client.get(f"ai-agents?model={models['vision'].id}").json_body["results"]] == [
        "Invoice Triage"
    ]
    by_provider = client.get(f"ai-agents?provider={models['large'].provider_id}").json_body["results"]
    assert sorted(r["name"] for r in by_provider) == ["HR Assistant", "Invoice Triage"]
    theirs = model(b, provider(b), "Theirs")
    assert client.get(f"ai-agents?model={theirs.id}").json_body["results"] == []  # filters to nothing, no leak
    for bad in ("platform=skynet", "channel=fax", "human_in_loop=maybe", "model=x", "ordering=platform"):
        assert client.get(f"ai-agents?{bad}").status_code == 400, bad


def test_ordering_by_risk_pages_with_cursors(a, api, fleet):
    client = api(a["users"]["viewer"])
    first = client.get("ai-agents?ordering=risk_classification&page_size=2").json_body
    second = client.get(f"ai-agents?ordering=risk_classification&page_size=2&cursor={first['next']}").json_body
    names = [r["name"] for r in first["results"] + second["results"]]
    assert names == ["Rogue", "Invoice Triage", "HR Assistant", "Desk Helper"]


def test_if_match_covers_models_and_channels(a, api, fleet, models):
    client = api(a["users"]["editor"])
    url = f"ai-agents/{fleet['hr'].id}"
    seen = client.get(url)["ETag"]
    services.update_resource(a["org"], a["users"]["admin"], fleet["hr"], {"models_used": [str(models["vision"].id)]})
    stale = client.client.patch(
        "/api/v1/" + url, '{"channels": ["web"]}', content_type="application/json", HTTP_IF_MATCH=seen
    )
    assert stale.status_code == 412


# ---- Needs attention -------------------------------------------------------------------------------------------------------


def test_agent_gaps_queue_lists_agents_missing_an_owner_a_scope_or_a_decision(a, api, models):
    owners = {"business_owner": str(a["people"]["dana"].id), "support_team": str(a["team"].id)}
    agent(a, "Complete", data_access_scope="One site.", human_in_loop=False, criticality="low", **owners)
    agent(a, "No owner", data_access_scope="One site.", human_in_loop=True, criticality="high", support_team=None)
    agent(a, "No scope", human_in_loop=True, criticality="critical", **owners)
    agent(a, "Undecided", data_access_scope="Mail.", **owners)
    agent(a, "Retired", lifecycle="retired")
    services.archive_resource(a["org"], a["users"]["editor"], agent(a, "Archived"))
    body = api(a["users"]["viewer"]).get("needs-attention?scope=all").json_body
    rows = {r["name"]: r for r in body["agent_gaps"]}
    assert list(rows) == ["No scope", "No owner", "Undecided"]  # by criticality, then name
    assert rows["No owner"]["gaps"] == ["No owner"]
    assert rows["No owner"]["why"] == "Nobody owns it: no business or technical owner."
    assert rows["No scope"]["gaps"] == ["No data access scope"]
    assert rows["Undecided"]["gaps"] == ["No human-in-loop decision"]
    assert rows["Undecided"]["why"] == "Whether a person approves its actions isn't decided."
    assert rows["Undecided"]["kind"] == "ai_agent" and rows["Undecided"]["steward"]["id"] == a["users"]["editor"].id
    # Agents are records: they are in the ownership and review queues too.
    assert "No owner" in {r["name"] for r in body["ownership_gaps"]}
    assert {"Complete", "Undecided"} <= {r["name"] for r in body["reviews"]}
    assert body["counts"]["all"] >= len(body["agent_gaps"]) + len(body["ownership_gaps"])


def test_an_agent_gap_can_be_snoozed_and_still_shows(a, api):
    bot = agent(a, "Undecided", data_access_scope="Mail.")
    client = api(a["users"]["editor"])
    until = (services.org_today(a["org"]) + timedelta(days=7)).isoformat()
    snoozed = client.post(
        "needs-attention/snoozes",
        {"kind": "snooze", "subject_type": "agent_gaps", "subject_id": str(bot.id), "until": until},
    )
    assert snoozed.status_code == 201, snoozed.json_body
    row = client.get("needs-attention?scope=all").json_body["agent_gaps"][0]
    assert row["snooze"]["until"] == until  # shown on the row; never hidden
    assert NotificationSnooze.objects.get(subject_type="agent_gaps").subject_id == str(bot.id)
    owner = str(a["people"]["dana"].id)
    complete_bot = agent(a, "Complete", data_access_scope="Mail.", human_in_loop=True, technical_owner=owner)
    refused = client.post(
        "needs-attention/snoozes",
        {"kind": "snooze", "subject_type": "agent_gaps", "subject_id": str(complete_bot.id), "until": until},
    )
    assert refused.status_code == 400  # only an item that is in the queue


def test_the_digest_lists_agent_gaps_with_a_link_to_the_agent(a, api):
    from notify import digest

    bot = agent(a, "Undecided", data_access_scope="Mail.")
    items, _ = digest.plan(a["org"], services.org_today(a["org"]))
    entries = [(q, r) for q, r, *_ in items[a["users"]["editor"].id] if q == "agent_gaps"]
    assert [r["name"] for _, r in entries] == ["Undecided"]
    assert digest._path("agent_gaps", entries[0][1]) == f"/ai-agents/{bot.id}"
    assert digest._line("agent_gaps", entries[0][1]) == "Undecided — No owner, No human-in-loop decision"


# ---- export --------------------------------------------------------------------------------------------------------------


def test_csv_and_excel_export_write_models_and_channels(a, api, fleet):
    client = api(a["users"]["viewer"])
    response = client.client.get("/api/v1/ai-agents/export.csv?platform=copilot_studio,claude")
    assert response.status_code == 200
    assert 'filename="softwarden-ai-agents-' in response["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"HR Assistant", "Invoice Triage"}
    assert by_name["HR Assistant"]["models_used"] == "Fabrikam AI Fabrikam Large 2"
    assert by_name["Invoice Triage"]["channels"] == "api; email"
    assert by_name["HR Assistant"]["human_in_loop"] == "true" and by_name["Invoice Triage"]["human_in_loop"] == ""
    assert "vendor" not in rows[0] and "data_access_scope" in rows[0]
    export = AuditEvent.objects.get(action="export")
    assert export.changes["kind"] == "ai_agent" and export.changes["rows"] == 2
    xlsx = client.client.get("/api/v1/ai-agents/export.xlsx")
    sheet = workbook(xlsx.content).active
    assert sheet.title == "AI agents"
    header = [c.value for c in sheet[1]]
    hitl = header.index("human_in_loop")
    values = {row[header.index("name")].value: row[hitl].value for row in sheet.iter_rows(min_row=2)}
    assert values["HR Assistant"] is True
    assert client.client.get("/api/v1/ai-agents/template.csv").status_code == 404  # agent import comes later


# ---- relationships, bulk edit, saved filters, dependencies -------------------------------------------------------------


def test_an_agent_depends_on_records_and_is_explored(a, api):
    bot = agent(a)
    client = api(a["users"]["editor"])
    link = client.post("relationships", {"type": "depends_on", "source": str(bot.id), "target": str(a["app"].id)})
    assert link.status_code == 201 and link.json_body["source"]["kind"] == "ai_agent"
    dependents = client.get(f"dependencies?target={a['app'].id}").json_body
    assert [n["name"] for n in dependents["nodes"]] == ["Helper"]
    assert client.get(f"dependencies?target={bot.id}&direction=depends_on").status_code == 200
    job = services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Job", "kind": "component", "lifecycle": "active"}
    )
    feed = client.post(
        "integrations", {"name": "Feed", "lifecycle": "active", "source": str(bot.id), "target": str(job.id)}
    )
    assert feed.status_code == 400
    assert feed.json_body["errors"]["source"] == ["The source of an integration must be an application or a component."]
    implements = client.post("relationships", {"type": "implements", "source": str(bot.id), "target": str(job.id)})
    assert implements.status_code == 400
    replaced = client.post("relationships", {"type": "replaces", "source": str(bot.id), "target": str(a["app"].id)})
    assert replaced.json_body["errors"]["target"] == ["An AI agent can only replace another AI agent."]
    assessment = client.post("assessments", {"target": str(bot.id), "change_type": "retirement", "title": "Retire bot"})
    assert assessment.status_code == 201


def test_bulk_edit_and_saved_filters_take_agents(a, api, fleet):
    client = api(a["users"]["editor"])
    ids = [str(fleet["hr"].id), str(fleet["desk"].id)]
    body = {"kind": "ai_agent", "ids": ids, "changes": {"support_team": str(a["team"].id), "add_tags": ["ai"]}}
    applied = client.post("bulk-edit/apply", body)
    assert applied.status_code == 200 and applied.json_body["updated"] == 2
    assert client.get("ai-agents?tag=ai").json_body["results"][0]["support_team"]["name"] == "IT Applications"
    wrong_kind = client.post("bulk-edit/apply", {**body, "ids": [str(a["app"].id)]})
    assert wrong_kind.status_code == 404
    saved = client.post(
        "saved-filters", {"name": "Risky agents", "kind": "ai_agent", "query": {"risk_classification": "high"}}
    )
    assert saved.status_code == 201
    listed = client.get("saved-filters?kind=ai_agent").json_body
    assert "Risky agents" in {f["name"] for f in listed}
    refused = client.post("saved-filters", {"name": "Bad", "kind": "ai_agent", "query": {"platform": "skynet"}})
    assert refused.status_code == 400


def test_an_agent_is_linked_from_discovery_like_any_record(a, api):
    """Discovery can link a candidate to an agent (Copilot Studio agents have app registrations)."""
    from discovery import engine as discovery
    from discovery.models import Observation

    connector = discovery.create_connector(
        a["org"], a["users"]["admin"], {"tenant_id": "simulated", "client_id": "", "secret": "synthetic-secret"}
    )
    connections.run_test(
        a["org"], a["users"]["admin"], connector.microsoft_connection
    )  # a run needs a tested connection
    discovery.run(connector)
    bot = agent(a)
    obs = Observation.objects.filter(connector=connector).earliest("display_name")
    discovery.link(a["org"], a["users"]["editor"], obs, {"resource": str(bot.id)})
    sources = api(a["users"]["viewer"]).get(f"ai-agents/{bot.id}/discovery-sources")
    assert sources.status_code == 200 and len(sources.json_body) == 1


def test_an_agents_discovery_sources_need_ai_agents(a, api):
    """Found by a capability walk: with ai_agents revoked every AI agent route answered 403 except this one, and
    a record route of another kind named with the agent's ID answered its decisions too."""
    from core import capabilities as caps
    from core.models import CapabilityOverride

    bot = agent(a)
    client = api(a["users"]["viewer"])
    CapabilityOverride.objects.create(organization=a["org"], capability="ai_agents", effect="revoke", reason="test")
    caps.forget_organization(a["org"].pk)
    response = client.get(f"ai-agents/{bot.id}/discovery-sources")
    assert response.status_code == 403 and response.json_body["capability"] == "ai_agents"
    assert client.get(f"applications/{a['app'].id}/discovery-sources").status_code == 200


def test_an_observations_drift_on_an_agent_needs_ai_agents(a, api):
    """An observation's drift view (and its ignore and apply) read the linked record; for an AI agent that
    needs ai_agents, as every other AI agent route does. Nothing is written when refused."""
    from django.utils import timezone

    from core import capabilities as caps
    from core.models import CapabilityOverride
    from discovery import engine
    from discovery.models import Observation, SourceDifference
    from tests.test_runs import make_connector

    bot = agent(a)
    connector = make_connector(a["org"], a["users"]["admin"])
    now = timezone.now()
    obs = Observation.objects.create(
        organization=a["org"],
        connector=connector,
        kind="ai_agent",
        external_id="helper",
        display_name="Helper",
        data={"description": "Changed in the source"},
        fingerprint="f" * 64,
        first_seen_at=now,
        last_seen_at=now,
        last_changed_at=now,
    )
    engine.link(a["org"], a["users"]["editor"], obs, {"resource": str(bot.id)})
    editor = api(a["users"]["editor"])
    base = f"discovery/observations/{obs.id}"
    assert editor.get(f"{base}/drift").status_code == 200

    def listed():
        rows = editor.get("discovery/candidates?status=all").json_body["results"]
        return next(r for r in rows if r["id"] == str(obs.id))["differences"]

    assert listed()  # the agent's curated description against the source's
    CapabilityOverride.objects.create(organization=a["org"], capability="ai_agents", effect="revoke", reason="test")
    caps.forget_organization(a["org"].pk)
    before = SourceDifference.objects.count()
    for method, path, body in (
        ("get", f"{base}/drift", None),
        ("post", f"{base}/drift/ignore", {"fields": ["description"], "seen": {"description": "Changed in the source"}}),
        ("post", f"{base}/drift/apply", {"fields": ["description"]}),
    ):
        response = getattr(editor, method)(path, body) if body is not None else editor.get(path)
        assert response.status_code == 403 and response.json_body["capability"] == "ai_agents", path
    assert SourceDifference.objects.count() == before
    assert listed() == []  # review follow-up: the candidate list leaves the agent's values out too


def test_discovery_sources_answer_only_the_kind_their_url_names(a, api):
    """Like a record's other subroutes (catalog/urls.py graph_routes), another kind's ID is a 404."""
    bot = agent(a)
    job = services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Nightly", "kind": "component", "lifecycle": "active"}
    )
    client = api(a["users"]["viewer"])
    for prefix, own, others in (
        ("applications", a["app"].id, (bot.id, job.id)),
        ("components", job.id, (bot.id, a["app"].id)),
        ("ai-agents", bot.id, (a["app"].id, job.id)),
    ):
        assert client.get(f"{prefix}/{own}/discovery-sources").status_code == 200, prefix
        for other in others:
            assert client.get(f"{prefix}/{other}/discovery-sources").status_code == 404, (prefix, other)


# ---- cross-tenant (the database half is in test_tenancy.py) -------------------------------------------------------------


@pytest.mark.parametrize(
    "method,suffix,payload",
    [
        ("get", "", None),
        ("patch", "", {"name": "Hijacked"}),
        ("post", "/archive", None),
        ("delete", "", None),
        ("get", "/reviews", None),
        ("get", "/audit", None),
    ],
)
def test_another_organizations_agent_is_404(a, b, api, method, suffix, payload):
    theirs = agent(b, "Theirs")
    client = api(a["users"]["admin"])
    url = f"ai-agents/{theirs.id}{suffix}"
    response = getattr(client, method)(url, payload) if payload is not None else getattr(client, method)(url)
    assert response.status_code == 404
    theirs.refresh_from_db()
    assert theirs.name == "Theirs" and theirs.archived_at is None


def test_another_organizations_models_and_providers_are_unknown(a, b, api, models):
    theirs_provider = provider(b, "Theirs AI")
    theirs = model(b, theirs_provider, "Their model")
    client = api(a["users"]["editor"])
    assert client.get(f"ai-models/{theirs.id}").status_code == 404
    assert client.get(f"ai-providers/{theirs_provider.id}").status_code == 404
    refused = client.post("ai-agents", {"name": "Bot", "lifecycle": "active", "models_used": [str(theirs.id)]})
    assert refused.status_code == 400 and refused.json_body["errors"] == {"models_used": ["Choose existing AI models."]}
    smuggled = client.post("ai-models", {"provider": str(theirs_provider.id), "name": "Smuggled"})
    assert smuggled.status_code == 400 and smuggled.json_body["errors"] == {"provider": ["Choose an existing record."]}
    moved = client.patch(f"ai-models/{models['large'].id}", {"provider": str(theirs_provider.id)})
    assert moved.status_code == 400
    assert client.get(f"ai-models?provider={theirs_provider.id}").json_body == []
    assert {m["name"] for m in client.get("ai-models").json_body} == {"Fabrikam Large", "Fabrikam Vision"}
    assert [p["name"] for p in client.get("ai-providers").json_body] == ["Fabrikam AI"]


def _rejected(write):
    with pytest.raises(IntegrityError), transaction.atomic():
        write()
        connection.check_constraints()  # the composite keys are deferred; force the check


def test_the_database_keeps_ai_rows_in_one_organization(a, b, models):
    theirs = model(b, provider(b), "Theirs")
    bot = agent(a)
    bare = Resource.objects.create(organization=a["org"], name="Bare", slug="bare", kind="ai_agent", lifecycle="active")
    _rejected(lambda: AIAgent.objects.create(resource=bare, organization=b["org"]))
    _rejected(lambda: AIModel.objects.filter(id=models["large"].id).update(provider=theirs.provider))
    _rejected(lambda: AgentModel.objects.create(organization=a["org"], agent_id=bot.id, model=theirs))
    _rejected(lambda: AgentModel.objects.create(organization=b["org"], agent_id=bot.id, model=theirs))


def test_an_admin_deletes_an_agent_with_models_and_its_links_go_with_it(a, api, models):
    bot = agent(a, "Bot", models_used=[str(models["large"].id), str(models["vision"].id)])
    response = api(a["users"]["admin"]).delete(f"ai-agents/{bot.id}")
    assert response.status_code == 204
    assert not AgentModel.objects.filter(agent_id=bot.id).exists() and AIModel.objects.count() == 2
    event = AuditEvent.objects.get(action="resource.deleted", target_id=bot.id)
    assert event.prechange is not None and len(event.prechange["models_used"]) == 2 and event.postchange is None


def test_two_versions_of_one_model_differ_in_the_diff(a, api, models):
    newer = model(a, models["large"].provider, "Fabrikam Large", version="3")
    client = api(a["users"]["editor"])
    bot = agent(a, "Bot", models_used=[str(models["large"].id)])
    client.patch(f"ai-agents/{bot.id}", {"models_used": [str(newer.id)]})
    event = AuditEvent.objects.filter(action="resource.updated", target_id=bot.id).latest("created_at")
    diff = {r["field"]: r for r in client.get(f"ai-agents/{bot.id}/audit/{event.id}").json_body["diff"]}
    assert [m["display"] for m in diff["models_used"]["removed"]] == ["Fabrikam Large 2"]
    assert [m["display"] for m in diff["models_used"]["added"]] == ["Fabrikam Large 3"]


def test_every_ai_route_needs_the_ai_agents_capability(a, api, models, monkeypatch):
    """ai_agents is a community capability (on in every edition); an edition without it is refused on every AI route
    with the capability problem, after the role check."""
    from core import capabilities as caps

    bot = agent(a, "Bot")
    monkeypatch.setitem(caps.EDITION_CAPABILITIES, "community", caps.COMMUNITY - {caps.AI_AGENTS})
    client = api(a["users"]["viewer"])
    for url in (
        "ai-agents",
        f"ai-agents/{bot.id}",
        f"ai-agents/{bot.id}/reviews",
        f"ai-agents/{bot.id}/audit",
        f"ai-agents/{bot.id}/dependencies",
        f"ai-agents/{bot.id}/map",
        "ai-agents/export.csv",
        "ai-agents/export.xlsx",
        "ai-providers",
        f"ai-providers/{models['large'].provider_id}",
        "ai-models",
        f"ai-models/{models['large'].id}",
        "ai-models/in-use",
    ):
        response = client.client.get("/api/v1/" + url)
        assert response.status_code == 403 and response.json()["capability"] == "ai_agents", url
    refused = client.post("ai-agents", {"name": "X", "lifecycle": "active"})
    assert refused.status_code == 403 and "capability" not in refused.json_body  # the role is checked first
    assert api(a["users"]["editor"]).post("ai-providers", {"name": "X"}).json_body["capability"] == "ai_agents"
    assert client.get("applications").status_code == 200  # other record kinds are unaffected
