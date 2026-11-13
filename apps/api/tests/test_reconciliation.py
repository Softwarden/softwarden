"""Reconciliation on the simulated tenants: the queue (scored name suggestions, link, create for every source
kind, dismiss with a reason, undo with its create rule), "source differs" per field across Entra, Azure, Power Platform
and Halo (shown, applied as an ordinary audited edit under the record's If-Match, ignored until the source changes,
recomputed on runs and record edits, never applied on its own), "no longer seen in source" (Needs attention, keep,
archive, the automatic clear, the not_read guard), the licence seat sync (bound vs unbound pools, proposals, create and
bind), the acceptance check items end to end, tenancy, roles and audit content. Nothing here reaches the network."""

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.utils import timezone

from catalog import services
from catalog.models import Resource
from core import capabilities as caps
from core import connections, halo
from core.models import AuditEvent
from core.problems import CapabilityRequired, Conflict, FieldErrors
from discovery import drift, entra_simulator, missing
from discovery import engine as discovery
from discovery import halo as halo_source
from discovery import powerplatform_simulator as pp_sim
from discovery.models import (
    Connector,
    Observation,
    ReconciliationDecision,
    RunStatus,
    SourceDifference,
)
from money import contracts, licences
from money.models import LicensePool
from notify import digest
from notify.attention import QUEUES, UNCOUNTED, attention_queues
from notify.serializers import AttentionDiscoveryMissingSerializer

SECRET = "synthetic-reconciliation-secret"
E5 = "06ebc4ee-1bb5-47dd-8120-11324bc54e06"
E3 = "05e9a617-0261-4cee-bb44-138d3ef5d965"
FLOW_FREE = "f30db892-07e9-47e9-837c-80727f46fd3d"
DEVPACK = "c42b9cae-ea4f-4ab7-9717-81576235ccac"


# ---- helpers -----------------------------------------------------------------------------------------------------------


def connection(org, actor, kind="microsoft"):
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


def make(f, source="entra", scope=None, conn=None, name=None):
    kind = "halo" if source == "halo" else "microsoft"
    conn = conn or connection(f["org"], f["users"]["admin"], kind)
    data: dict[str, Any] = {"source": source, "name": name or f"{source} {uuid.uuid4().hex[:4]}"}
    data[f"{kind}_connection"] = str(conn.id)
    if scope is not None:
        data["scope"] = scope
    return Connector.objects.get(pk=discovery.create_connector(f["org"], f["users"]["admin"], data).pk)


def change(connector, *changes):
    connector.refresh_from_db()
    connector.config = {
        **connector.config,
        "simulator_changes": [*connector.config.get("simulator_changes", []), *changes],
    }
    connector.save(update_fields=["config"])
    return connector


def configure(connector, **config):
    connector.refresh_from_db()
    connector.config = {**connector.config, **config}
    connector.save(update_fields=["config"])
    return connector


def run(connector):
    connector.refresh_from_db()
    result = discovery.run(connector)
    assert result is not None and result.status == RunStatus.SUCCEEDED, result and result.error
    return result


def obs(connector, name):
    return Observation.objects.get(connector=connector, display_name=name)


def record(f, name, kind="application", **extra):
    return services.create_resource(
        f["org"], f["users"]["editor"], {"name": name, "kind": kind, "lifecycle": "active", **extra}
    )


def post_if_match(client, url, data, tag):
    response = client.client.post(
        "/api/v1/" + url, json.dumps(data), content_type="application/json", HTTP_IF_MATCH=tag
    )
    response.json_body = response.json() if response.content else None
    return response


def seen(client, oid, fields=None):
    """The source values a person reviewed (GET .../drift), for `fields` or every difference nobody ignored."""
    rows = client.get(f"discovery/observations/{oid}/drift").json_body["differences"]
    return {d["field"]: d["source"] for d in rows if (d["field"] in fields if fields else not d["ignored"])}


def all_audit_text():
    rows = AuditEvent.objects.values_list("changes", "prechange", "postchange")
    return json.dumps([list(r) for r in rows], default=str)


@pytest.fixture
def ent(a):
    a["org"], _ = caps.set_edition(a["org"], "enterprise")
    return a


@pytest.fixture
def entra(a):
    connector = make(a, "entra", name="Entra")
    run(connector)
    return connector


# ---- queue: suggestions ------------------------------------------------------------------------------------------------


def test_suggestions_rank_compatible_records_with_a_score(a, api, entra):
    zoom = record(a, "zoom")  # same name, other case
    record(a, "Zoom Sync", kind="component")  # not an application: never suggested for an application
    workday = record(a, "Workday")
    record(a, "Archived Zoom")
    Resource.objects.filter(name="Archived Zoom").update(archived_at=timezone.now())
    found = discovery.suggestions(a["org"], obs(entra, "Zoom"), 5)
    assert found[0] == {"id": str(zoom.id), "kind": "application", "name": "zoom", "score": 1.0}
    assert all(r["kind"] == "application" and r["name"] != "Archived Zoom" for r in found)
    # The record's name inside a longer source name scores high (word similarity).
    renamed = change(entra, {"op": "rename", "id": "Workday", "value": "Workday HCM Production"})
    run(renamed)
    [best, *_] = discovery.suggestions(a["org"], obs(entra, "Workday HCM Production"), 3)
    assert best["id"] == str(workday.id) and best["score"] >= 0.6
    viewer = api(a["users"]["viewer"])
    oid = obs(entra, "Zoom").id
    body = viewer.get(f"discovery/observations/{oid}/suggestions?limit=1").json_body
    assert body == {"results": [{"id": str(zoom.id), "kind": "application", "name": "zoom", "score": 1.0}]}
    assert viewer.get(f"discovery/observations/{oid}/suggestions?limit=11").status_code == 400
    assert viewer.get(f"discovery/observations/{oid}/suggestions?nope=1").status_code == 400
    page = viewer.get("discovery/candidates?q=zoom").json_body
    assert page["results"][0]["suggested_matches"][0] == {
        "id": str(zoom.id),
        "kind": "application",
        "name": "zoom",
        "score": 1.0,
    }


def test_ai_agent_suggestions_include_applications(a, entra):
    agent = obs(entra, "HR Helpdesk Agent")
    assert agent.kind == "ai_agent"
    app = record(a, "HR Helpdesk")
    bot = record(a, "HR Helpdesk Agent", kind="ai_agent")
    found = {r["id"]: r for r in discovery.suggestions(a["org"], agent, 5)}
    assert str(bot.id) in found and str(app.id) in found and found[str(bot.id)]["score"] == 1.0


# ---- queue: decisions and undo ------------------------------------------------------------------------------------------


def test_link_dismiss_and_their_undo(a, api, entra):
    editor = api(a["users"]["editor"])
    oid = obs(entra, "Zoom").id
    assert editor.post(f"discovery/observations/{oid}/link", {"resource": str(a["app"].id)}).status_code == 200
    assert SourceDifference.objects.filter(observation_id=oid).exists()  # differences from the moment it's linked
    r = editor.post(f"discovery/observations/{oid}/reopen")
    assert r.status_code == 200 and r.json_body["status"] == "new" and r.json_body["differences"] == []
    assert not SourceDifference.objects.filter(observation_id=oid).exists()
    assert Resource.objects.get(id=a["app"].id).archived_at is None  # undoing a link never touches the record
    r = editor.post(f"discovery/observations/{oid}/dismiss", {"reason": " "})
    assert r.status_code == 400 and "reason" in r.json_body["errors"]
    assert (
        editor.post(f"discovery/observations/{oid}/dismiss", {"reason": "Not ours"}).json_body["status"] == "dismissed"
    )
    assert editor.post(f"discovery/observations/{oid}/reopen").json_body["status"] == "new"
    assert editor.post(f"discovery/observations/{oid}/reopen").status_code == 409  # nothing left to undo
    actions = list(AuditEvent.objects.order_by("created_at").values_list("action", flat=True))
    assert [x for x in actions if x.startswith("observation.")] == [
        "observation.linked",
        "observation.reopened",
        "observation.dismissed",
        "observation.reopened",
    ]


def test_dismiss_takes_if_match_with_the_candidates_etag(a, api, entra):
    """A dismissal applies only to the candidate as it was read; without If-Match it
    stays unconditional, as before."""
    editor = api(a["users"]["editor"])
    zoom, workday = obs(entra, "Zoom"), obs(entra, "Workday")
    read = editor.client.get(f"/api/v1/discovery/observations/{zoom.id}")
    tag = read["ETag"]
    assert read.status_code == 200 and tag
    # Someone decides (and undoes) in between: the candidate's revision moved, so the stale dismissal is refused.
    discovery.link(a["org"], a["users"]["editor"], zoom, {"resource": str(a["app"].id)})
    stale = post_if_match(editor, f"discovery/observations/{zoom.id}/dismiss", {"reason": "Not ours"}, tag)
    assert stale.status_code == 412 and stale.json_body["type"] == "/problems/precondition-failed"
    assert stale["ETag"] == f'"{stale.json_body["revision"]}"'
    assert Observation.objects.get(id=zoom.id).decision.decision == "linked"  # nothing changed
    assert not AuditEvent.objects.filter(action="observation.dismissed").exists()
    # The current ETag dismisses; the answer carries the new one.
    current = editor.client.get(f"/api/v1/discovery/observations/{workday.id}")["ETag"]
    ok = post_if_match(editor, f"discovery/observations/{workday.id}/dismiss", {"reason": "Not ours"}, current)
    assert ok.status_code == 200 and ok.json_body["status"] == "dismissed" and ok["ETag"] != current
    # The OpenAPI description says so.
    from drf_spectacular.generators import SchemaGenerator

    paths = SchemaGenerator().get_schema(public=True)["paths"]
    operation = paths["/api/v1/discovery/observations/{id}/dismiss"]["post"]
    assert "412" in operation["responses"]
    assert any(p["name"] == "If-Match" and p["in"] == "header" for p in operation["parameters"])


def test_undo_keeps_the_record_unless_archive_is_asked(a, api, entra):
    org, editor = a["org"], a["users"]["editor"]
    client = api(editor)
    # Plain undo (the record page's Unlink): the decision goes, the created record stays live.
    sn, created = discovery.create_record(org, editor, obs(entra, "ServiceNow"), {})
    r = client.post(f"discovery/observations/{sn.id}/reopen")
    assert r.status_code == 200 and r.json_body["status"] == "new"
    created.refresh_from_db()
    assert created.archived_at is None
    assert "archived" not in AuditEvent.objects.get(action="observation.reopened", target_id=created.id).changes
    # archive: true archives the untouched record created from the candidate.
    sn, created = discovery.create_record(org, editor, obs(entra, "ServiceNow"), {})
    assert client.post(f"discovery/observations/{sn.id}/reopen", {"archive": "yes"}).status_code == 400
    r = client.post(f"discovery/observations/{sn.id}/reopen", {"archive": True})
    assert r.status_code == 200
    created.refresh_from_db()
    assert created.archived_at is not None
    event = AuditEvent.objects.filter(action="observation.reopened", target_id=created.id).latest("created_at")
    assert event.changes["archived"] is True and event.changes["was"] == "created"
    assert AuditEvent.objects.filter(action="resource.archived", target_id=created.id).exists()
    # archive on a link (not a create) is refused; nothing changes.
    zoom = obs(entra, "Zoom")
    discovery.link(org, editor, zoom, {"resource": str(a["app"].id)})
    assert client.post(f"discovery/observations/{zoom.id}/reopen", {"archive": True}).status_code == 400
    assert ReconciliationDecision.objects.filter(observation=zoom).exists()
    # Edited since it was created: archive is refused (409) and nothing changes.
    hub, edited = discovery.create_record(org, editor, obs(entra, "Expense Hub"), {})
    services.update_resource(org, editor, edited, {"criticality": "high"})
    r = client.post(f"discovery/observations/{hub.id}/reopen", {"archive": True})
    assert r.status_code == 409 and "changed after it was created" in r.json_body["detail"]
    edited.refresh_from_db()
    assert edited.archived_at is None and ReconciliationDecision.objects.filter(observation=hub).exists()
    # Already archived by hand: undo with archive goes ahead and it stays archived.
    services.archive_resource(org, editor, edited)
    assert client.post(f"discovery/observations/{hub.id}/reopen", {"archive": True}).status_code == 200


def references(a, kind, record):
    """Make one thing reference the record: a contract line, a licence pool, a relationship with it as the target,
    an integration end, or another observation linked to it."""
    org, editor = a["org"], a["users"]["editor"]
    if kind == "contract_line":
        contract = contracts.create_contract(org, editor, {"name": "Held", "end_date": "2027-03-31"})
        contracts.add_item(org, editor, contract, {"record": str(record.id)})
    elif kind == "licence_pool":
        contracts.create_pool(org, editor, {"application": str(record.id), "sku_name": "Seats"})
    elif kind == "relationship_target":
        source = record_(a, "Depends on it")
        services.create_relationship(
            org, editor, {"type": "depends_on", "source": str(source.id), "target": str(record.id)}
        )
    elif kind == "integration_end":
        other = record_(a, "Other end")
        services.create_integration(
            org, editor, {"name": "Feed", "lifecycle": "active", "source": str(record.id), "target": str(other.id)}
        )


def record_(a, name):
    return services.create_resource(a["org"], a["users"]["editor"], {"name": name, "lifecycle": "active"})


@pytest.mark.parametrize("kind", ["contract_line", "licence_pool", "relationship_target", "integration_end", "linked"])
def test_undo_with_archive_is_refused_while_anything_references_the_record(a, api, entra, kind):
    org, editor = a["org"], a["users"]["editor"]
    sn, created = discovery.create_record(org, editor, obs(entra, "ServiceNow"), {})
    if kind == "linked":
        discovery.link(org, editor, obs(entra, "Workday"), {"resource": str(created.id)})
    else:
        references(a, kind, created)
    r = api(editor).post(f"discovery/observations/{sn.id}/reopen", {"archive": True})
    assert r.status_code == 409, (kind, r.json_body)
    assert r.json_body["blocking_references"]
    created.refresh_from_db()
    assert created.archived_at is None and ReconciliationDecision.objects.filter(observation=sn).exists()
    # Without archive the undo goes ahead and the record stays.
    assert api(editor).post(f"discovery/observations/{sn.id}/reopen").status_code == 200


def test_create_maps_every_source_kind(ent, api):
    org, editor = ent["org"], ent["users"]["editor"]
    entra = make(ent, "entra")
    run(entra)
    _, app = discovery.create_record(org, editor, obs(entra, "Zoom"), {})
    zoom = obs(entra, "Zoom")
    assert app.kind == "application" and app.application.entra_app_id == zoom.data["app_id"]
    assert app.application.service_principal_id == zoom.data["service_principal_id"]
    _, agent = discovery.create_record(org, editor, obs(entra, "HR Helpdesk Agent"), {})
    assert agent.kind == "ai_agent" and agent.ai_agent.platform == "copilot_studio" and agent.ai_agent.external_id
    az = make(ent, "azure")
    run(az)
    payroll = obs(az, "payroll-func")
    _, component = discovery.create_record(org, editor, payroll, {})
    assert component.kind == "component" and component.component.azure_resource_id == payroll.data["resource_id"]
    assert component.component.environment == "prod"
    pp = make(ent, "powerplatform", {"environment_urls": [f"https://{pp_sim.CONTOSO}"], "include_cloud_flows": True})
    run(pp)
    _, bot = discovery.create_record(org, editor, obs(pp, "IT Service Desk"), {})
    assert bot.kind == "ai_agent" and bot.ai_agent.external_id == "cr6f1_itServiceDesk"
    _, flow = discovery.create_record(org, editor, obs(pp, "Approve expenses"), {})
    assert flow.kind == "component" and flow.component.component_type == "automation_flow"
    hl = make(ent, "halo")
    run(hl)
    _, asset = discovery.create_record(org, editor, obs(hl, "Salesforce"), {})
    assert asset.kind == "application"
    # None of them starts out differing from its source.
    assert not SourceDifference.objects.filter(organization=org).exists()
    # A licence SKU never becomes a record: it isn't a candidate at all.
    lic = make(ent, "entra_licenses", conn=entra.microsoft_connection)
    run(lic)
    sku = Observation.objects.get(connector=lic, external_id=E5)
    client = api(editor)
    assert client.post(f"discovery/observations/{sku.id}/create", {}).status_code == 404
    assert client.post(f"discovery/observations/{sku.id}/link", {"resource": str(app.id)}).status_code == 404


# ---- source differs -----------------------------------------------------------------------------------------------------


def test_entra_drift_is_shown_and_applied_as_an_audited_edit_under_if_match(a, api, entra):
    editor = api(a["users"]["editor"])
    oid = obs(entra, "Zoom").id
    editor.post(f"discovery/observations/{oid}/link", {"resource": str(a["app"].id)})
    body = api(a["users"]["viewer"]).get(f"discovery/observations/{oid}/drift").json_body
    fields = {d["field"]: d for d in body["differences"]}
    assert list(fields) == ["name", "description", "documentation_url", "entra_app_id", "service_principal_id"]
    assert fields["name"] == {
        **fields["name"],
        "label": "Name",
        "curated": "orga App",
        "source": "Zoom",
        "ignored": False,
    }
    assert fields["documentation_url"]["source"] == "https://example.test/zoom" and fields["name"]["since"]
    assert body["record"] == {"id": str(a["app"].id), "kind": "application", "name": "orga App"}
    record_tag = editor.get(f"applications/{a['app'].id}")["ETag"]
    assert body["record_etag"] == record_tag.strip('"')
    # The record changed since the ETag was read: 412, nothing written.
    services.update_resource(a["org"], a["users"]["admin"], a["app"], {"criticality": "low"})
    stale = post_if_match(
        editor, f"discovery/observations/{oid}/drift/apply", {"fields": ["name"], "seen": {"name": "Zoom"}}, record_tag
    )
    assert stale.status_code == 412 and Resource.objects.get(id=a["app"].id).name == "orga App"
    # The source value the person reviewed changed since: 409.
    tag = editor.get(f"applications/{a['app'].id}")["ETag"]
    r = post_if_match(
        editor, f"discovery/observations/{oid}/drift/apply", {"fields": ["name"], "seen": {"name": "Zoom 2"}}, tag
    )
    assert r.status_code == 409
    r = post_if_match(editor, f"discovery/observations/{oid}/drift/apply", {"fields": ["nope"], "seen": {}}, tag)
    assert r.status_code == 400 and "fields" in r.json_body["errors"]
    # `seen` is required for every chosen field.
    for body in ({"fields": ["name"]}, {"fields": ["name", "documentation_url"], "seen": {"name": "Zoom"}}, {}):
        r = post_if_match(editor, f"discovery/observations/{oid}/drift/apply", body, tag)
        assert r.status_code == 400 and "seen" in r.json_body["errors"], body
    chosen = ["name", "documentation_url"]
    r = post_if_match(
        editor,
        f"discovery/observations/{oid}/drift/apply",
        {"fields": chosen, "seen": seen(editor, oid, chosen)},
        tag,
    )
    assert r.status_code == 200, r.json_body
    assert [d["field"] for d in r.json_body["differences"]] == ["description", "entra_app_id", "service_principal_id"]
    assert r["ETag"] == editor.get(f"applications/{a['app'].id}")["ETag"]
    app = Resource.objects.get(id=a["app"].id)
    assert app.name == "Zoom" and app.application.documentation_url == "https://example.test/zoom"
    event = AuditEvent.objects.filter(action="resource.updated", target_id=app.id).latest("created_at")
    assert event.actor_id == a["users"]["editor"].id and event.changes["name"] == ["orga App", "Zoom"]
    assert event.changes["discovery"] == {
        "observation": str(oid),
        "source_name": "Zoom",
        "connector": "Entra",
        "fields": ["name", "documentation_url"],
    }
    from catalog.audit_log import Names, summary

    assert summary(event, Names(a["org"], [event])).endswith("(from Entra)")
    # The record's sources list says the same.
    sources = editor.get(f"applications/{a['app'].id}/discovery-sources").json_body
    assert sources[0]["differs"] == ["description", "entra_app_id", "service_principal_id"]


def test_drift_ignore_holds_until_the_source_changes(a, api, entra):
    editor = api(a["users"]["editor"])
    oid = obs(entra, "Zoom").id
    editor.post(f"discovery/observations/{oid}/link", {"resource": str(a["app"].id)})
    assert editor.post(f"discovery/observations/{oid}/drift/ignore", {"fields": ["name"]}).status_code == 400
    chosen = ["name", "description"]
    r = editor.post(f"discovery/observations/{oid}/drift/ignore", {"fields": chosen, "seen": seen(editor, oid, chosen)})
    assert r.status_code == 200
    ignored = {d["field"]: d for d in r.json_body["differences"] if d["ignored"]}
    assert set(ignored) == {"name", "description"} and ignored["name"]["ignored_by"]["id"] == a["users"]["editor"].id
    event = AuditEvent.objects.get(action="observation.drift_ignored")
    assert event.target_id == a["app"].id and event.changes["fields"]["name"] == {
        "curated": "orga App",
        "source": "Zoom",
    }
    # Applying with no field list takes only what isn't ignored.
    tag = editor.get(f"applications/{a['app'].id}")["ETag"]
    assert (
        post_if_match(editor, f"discovery/observations/{oid}/drift/apply", {"seen": seen(editor, oid)}, tag).status_code
        == 200
    )
    app = Resource.objects.get(id=a["app"].id)
    assert app.name == "orga App" and app.application.documentation_url == "https://example.test/zoom"
    # Only ignored ones left: not counted as differing.
    assert editor.get("discovery/candidates?status=differs").json_body["counts"]["differs"] == 0
    before = SourceDifference.objects.get(observation_id=oid, field="name")
    run(entra)  # unchanged source: the ignore holds
    assert SourceDifference.objects.get(observation_id=oid, field="name").ignored_at == before.ignored_at
    call_command("simulate_entra_change", "rename", "Zoom", "Zoom Workplace")
    run(entra)
    name = SourceDifference.objects.get(observation_id=oid, field="name")
    assert name.source_value == "Zoom Workplace" and name.ignored_at is None and name.since > before.since
    assert SourceDifference.objects.get(observation_id=oid, field="description").ignored_at is not None
    assert editor.get("discovery/candidates?status=differs").json_body["counts"]["differs"] == 1


def test_drift_is_recomputed_on_record_edits_and_never_applied_by_a_run(a, api, entra):
    org, editor = a["org"], a["users"]["editor"]
    zoom = obs(entra, "Zoom")
    discovery.link(org, editor, zoom, {"resource": str(a["app"].id)})
    before = Resource.objects.values().get(id=a["app"].id)
    for _ in range(2):
        run(entra)
    after = Resource.objects.values().get(id=a["app"].id)
    assert {k: v for k, v in after.items() if k != "updated_at"} == {
        k: v for k, v in before.items() if k != "updated_at"
    }
    assert not AuditEvent.objects.filter(action="resource.updated", target_id=a["app"].id).exists()
    # An ordinary edit to the source's value makes the difference go; another value brings it back.
    api(editor).patch(f"applications/{a['app'].id}", {"name": "Zoom"})
    assert not SourceDifference.objects.filter(observation=zoom, field="name").exists()
    services.update_resource(org, editor, Resource.objects.get(id=a["app"].id), {"name": "Zoom (old)"})
    assert SourceDifference.objects.get(observation=zoom, field="name").source_value == "Zoom"
    # Gone: no differences, only "no longer seen".
    call_command("simulate_entra_change", "remove", "Zoom")
    run(entra)
    assert not SourceDifference.objects.filter(observation=zoom).exists()
    assert api(editor).get(f"discovery/observations/{zoom.id}/drift").json_body["differences"] == []
    r = api(editor).post(f"discovery/observations/{zoom.id}/drift/apply", {"seen": {}})
    assert r.status_code == 409


def test_drift_across_azure_power_platform_and_halo(ent, api):
    org, editor = ent["org"], ent["users"]["editor"]
    client = api(editor)
    # Azure: the resource ID and the environment tag; a tag change restarts `since`.
    az = make(ent, "azure")
    run(az)
    invoice = record(ent, "Invoice intake", kind="component", component_type="logic_app")
    discovery.link(org, editor, obs(az, "invoice-intake"), {"resource": str(invoice.id)})
    oid = obs(az, "invoice-intake").id
    fields = {d["field"]: d for d in client.get(f"discovery/observations/{oid}/drift").json_body["differences"]}
    assert set(fields) == {"name", "azure_resource_id", "environment"}
    assert fields["environment"]["source"] == "prod" and fields["environment"]["curated"] == ""
    first = SourceDifference.objects.get(observation_id=oid, field="environment").since
    change(az, {"op": "tag", "id": "invoice-intake", "value": {"environment": "test"}})
    run(az)
    moved = SourceDifference.objects.get(observation_id=oid, field="environment")
    assert moved.source_value == "test" and moved.since > first
    tag = client.get(f"components/{invoice.id}")["ETag"]
    chosen = ["environment", "azure_resource_id"]
    r = post_if_match(
        client, f"discovery/observations/{oid}/drift/apply", {"fields": chosen, "seen": seen(client, oid, chosen)}, tag
    )
    assert r.status_code == 200 and [d["field"] for d in r.json_body["differences"]] == ["name"]
    invoice.refresh_from_db()
    assert invoice.component.environment == "test" and invoice.component.azure_resource_id.endswith("/invoice-intake")
    # Power Platform: a renamed agent differs on its name.
    pp = make(ent, "powerplatform", {"environment_urls": [f"https://{pp_sim.CONTOSO}"]})
    run(pp)
    _, desk = discovery.create_record(org, editor, obs(pp, "IT Service Desk"), {})
    change(pp, {"op": "rename", "id": "IT Service Desk", "value": "IT Help"})
    run(pp)
    pid = obs(pp, "IT Help").id
    [only] = client.get(f"discovery/observations/{pid}/drift").json_body["differences"]
    assert (only["field"], only["curated"], only["source"]) == ("name", "IT Service Desk", "IT Help")
    # Halo: a renamed asset differs on its name, and that's all Halo speaks for.
    hl = make(ent, "halo")
    run(hl)
    _, sf = discovery.create_record(org, editor, obs(hl, "Salesforce"), {})
    change(hl, {"op": "rename", "id": "Salesforce", "value": "Salesforce Sales Cloud"})
    run(hl)
    hid = obs(hl, "Salesforce Sales Cloud").id
    [only] = client.get(f"discovery/observations/{hid}/drift").json_body["differences"]
    assert (only["field"], only["source"]) == ("name", "Salesforce Sales Cloud")
    assert client.post(f"discovery/observations/{hid}/drift/ignore", {"seen": seen(client, hid)}).status_code == 200
    # Nothing was applied by any run: the agent and the asset keep their curated names.
    assert Resource.objects.get(id=desk.id).name == "IT Service Desk"
    assert Resource.objects.get(id=sf.id).name == "Salesforce"


# ---- no longer seen in source --------------------------------------------------------------------------------------------


def missing_rows(f, user=None):
    queues, counts = attention_queues(f["org"], user or f["users"]["admin"], timezone.localdate())
    return queues["discovery_missing"], counts, queues


def test_missing_is_needs_attention_until_kept_and_clears_when_back(a, api, entra):
    org, editor = a["org"], a["users"]["editor"]
    discovery.link(org, editor, obs(entra, "Zoom"), {"resource": str(a["app"].id)})
    call_command("simulate_entra_change", "remove", "Zoom")
    run(entra)
    zoom = obs(entra, "Zoom")
    rows, counts, queues = missing_rows(a)
    [row] = rows
    assert row["record"] == {"id": str(a["app"].id), "kind": "application", "name": "orga App"}
    assert row["connector"]["name"] == "Entra" and row["observation"]["name"] == "Zoom"
    assert zoom.gone_at is not None
    assert row["since"] == zoom.gone_at.astimezone(ZoneInfo(org.timezone)).date().isoformat()
    assert "No longer seen in Entra" in row["why"] and row["kind"] == "discovery_missing"
    # Counted, and a digest section after Discovery candidates renders the queue.
    assert "discovery_missing" in QUEUES and "discovery_missing" not in UNCOUNTED
    assert counts["all"] == sum(len(q) for name, q in queues.items() if name not in UNCOUNTED)
    # Over HTTP, routed to the record's IT steward (the editor), with the serializer's fields.
    body = api(editor).get("needs-attention?scope=mine").json_body
    [mine] = body["discovery_missing"]
    assert set(mine) == set(AttentionDiscoveryMissingSerializer().fields)
    assert mine["steward"]["id"] == editor.id
    assert digest.SECTION_NAMES["discovery_missing"] == "No longer seen in source"
    assert "discovery_missing" not in digest.PENDING_SECTIONS
    names = [q for q, _ in digest.SECTIONS]
    assert names.index("discovery_missing") == names.index("discovery_candidates") + 1
    assert digest._path("discovery_missing", row) == f"/discovery?status=missing&connector={entra.id}"
    assert digest._line("discovery_missing", row).startswith("orga App — No longer seen in Entra")
    listed = api(a["users"]["viewer"]).get("discovery/candidates?status=missing").json_body
    assert listed["counts"]["missing"] == 1 and listed["results"][0]["missing"] is True
    # Keep: the row goes; the observation stays linked and gone.
    r = api(editor).post(f"discovery/observations/{zoom.id}/missing/acknowledge", {"note": "Moved to another tenant"})
    assert r.status_code == 200 and r.json_body["decision"]["gone_resolution"] == "kept"
    assert r.json_body["missing"] is False and missing_rows(a)[0] == []
    event = AuditEvent.objects.get(action="observation.missing_resolved")
    assert event.target_id == a["app"].id and event.changes["resolution"] == "kept"
    assert api(editor).post(f"discovery/observations/{zoom.id}/missing/acknowledge").status_code == 409
    # Back in the source: the resolution clears; gone again: it asks again.
    call_command("simulate_entra_change", "reset")
    back = run(entra)
    assert back.counts["returned"] == 1
    decision = ReconciliationDecision.objects.get(observation=zoom)
    assert decision.gone_acknowledged_at is None and decision.gone_resolution == ""
    call_command("simulate_entra_change", "remove", "Zoom")
    run(entra)
    assert len(missing_rows(a)[0]) == 1


def test_archive_the_missing_record_under_if_match(a, api, entra):
    org, editor = a["org"], a["users"]["editor"]
    client = api(editor)
    _, hub = discovery.create_record(org, editor, obs(entra, "Expense Hub"), {})
    discovery.link(org, editor, obs(entra, "Payroll Sync Daemon"), {"resource": str(hub.id)})
    call_command("simulate_entra_change", "remove", "Expense Hub")
    run(entra)
    oid = obs(entra, "Expense Hub").id
    tag = client.get(f"applications/{hub.id}")["ETag"]
    # Another source linked to the record still sees it: archiving is refused.
    r = post_if_match(client, f"discovery/observations/{oid}/missing/archive", {}, tag)
    assert r.status_code == 409 and "still sees" in r.json_body["detail"]
    call_command("simulate_entra_change", "remove", "Payroll Sync Daemon")
    run(entra)
    assert len(missing_rows(a)[0]) == 2
    assert post_if_match(client, f"discovery/observations/{oid}/missing/archive", {}, '"stale"').status_code == 412
    assert api(a["users"]["viewer"]).post(f"discovery/observations/{oid}/missing/archive").status_code == 403
    r = post_if_match(client, f"discovery/observations/{oid}/missing/archive", {"note": "Retired"}, tag)
    assert r.status_code == 200 and r.json_body["decision"]["gone_resolution"] == "archived"
    hub.refresh_from_db()
    assert hub.archived_at is not None
    assert AuditEvent.objects.filter(action="resource.archived", target_id=hub.id).exists()
    resolved = AuditEvent.objects.get(action="observation.missing_resolved")
    assert resolved.changes["resolution"] == "archived" and resolved.changes["reason"] == "Retired"
    assert missing_rows(a)[0] == []  # an archived record leaves the queue, the other row with it


def test_an_unread_scope_never_makes_a_linked_record_missing(ent, api):
    org, editor = ent["org"], ent["users"]["editor"]
    az = make(ent, "azure")
    run(az)
    payroll = record(ent, "Payroll function", kind="component")
    discovery.link(org, editor, obs(az, "payroll-func"), {"resource": str(payroll.id)})
    from discovery import azure_simulator

    configure(az, simulated_denied=[f"{azure_simulator.PROD}/Microsoft.Web/sites"])
    change(az, {"op": "remove", "id": "payroll-func"})
    result = run(az)
    assert result.counts["not_read"] >= 1 and result.counts["gone"] == 0
    assert obs(az, "payroll-func").gone_at is None and missing_rows(ent)[0] == []
    # Read again, it really is gone: now it's missing.
    configure(az, simulated_denied=[])
    run(az)
    assert [r["record"]["id"] for r in missing_rows(ent)[0]] == [str(payroll.id)]


def test_missing_leaves_out_turned_off_connectors_and_dismissed_or_unlinked_observations(a, entra):
    org, editor = a["org"], a["users"]["editor"]
    discovery.link(org, editor, obs(entra, "Zoom"), {"resource": str(a["app"].id)})
    discovery.dismiss(org, editor, obs(entra, "Workday"), {"reason": "Not ours"})
    call_command("simulate_entra_change", "remove", "Zoom")
    call_command("simulate_entra_change", "remove", "Workday")
    call_command("simulate_entra_change", "remove", "ServiceNow")
    run(entra)
    assert [r["observation"]["name"] for r in missing_rows(a)[0]] == ["Zoom"]
    discovery.update_connector(org, a["users"]["admin"], entra, {"enabled": False})
    assert missing_rows(a)[0] == []


# ---- the acceptance check items, end to end on the simulator --------------------------------------------------------------------


def test_done_when_rename_is_changed_and_delete_is_no_longer_seen_in_needs_attention(a, api, entra):
    """ "Renaming an app in Entra produces a 'changed' candidate, and deleting one produces 'no longer seen'", which
    surfaces in Needs attention."""
    org, editor = a["org"], a["users"]["editor"]
    discovery.dismiss(org, editor, obs(entra, "Contract Vault"), {"reason": "Legal's"})
    discovery.link(org, editor, obs(entra, "Zoom"), {"resource": str(a["app"].id)})
    call_command("simulate_entra_change", "rename", "Contract Vault", "Contract Vault 2")
    call_command("simulate_entra_change", "rename", "Zoom", "Zoom Workplace")
    call_command("simulate_entra_change", "remove", "Expense Hub")
    result = run(entra)
    assert result.counts["changed"] == 2 and result.counts["gone"] == 1
    # The dismissed app is a "changed" candidate; the linked one shows its new name as a source difference.
    assert discovery.status_of(obs(entra, "Contract Vault 2")) == "changed"
    client = api(a["users"]["viewer"])
    page = client.get("discovery/candidates?status=changed").json_body
    assert [r["name"] for r in page["results"]] == ["Contract Vault 2"]
    zoom = obs(entra, "Zoom Workplace")
    names = [
        d
        for d in client.get(f"discovery/observations/{zoom.id}/drift").json_body["differences"]
        if d["field"] == "name"
    ]
    assert names[0]["source"] == "Zoom Workplace" and names[0]["curated"] == "orga App"
    # A deleted candidate is gone; a deleted linked app is "no longer seen" in Needs attention.
    assert discovery.status_of(obs(entra, "Expense Hub")) == "gone"
    _, hub = discovery.create_record(org, editor, obs(entra, "Workday"), {})
    call_command("simulate_entra_change", "remove", "Workday")
    run(entra)
    body = api(a["users"]["admin"]).get("needs-attention?scope=all").json_body
    [row] = body["discovery_missing"]
    assert row["record"]["id"] == str(hub.id) and row["observation"]["name"] == "Workday"
    assert body["counts"]["all"] == sum(len(body[q]) for q in QUEUES if q not in UNCOUNTED)


def test_done_when_seat_counts_update_only_pools_bound_to_the_sku(a, api, monkeypatch):
    """ "Licence seat counts update only pools bound to the SKU; unbound SKUs appear as proposals."""
    org = a["org"]
    entra = make(a, "entra", name="Entra")
    lic = make(a, "entra_licenses", conn=entra.microsoft_connection, name="Licences")
    editor = a["users"]["editor"]
    synced = contracts.create_pool(
        org, editor,
        {"application": str(a["app"].id), "sku_name": "M365 E5", "sku_id": E5.upper(), "seats_purchased": 1, "seats_assigned": 1, "seat_sync": True},
    )  # fmt: skip
    bound = contracts.create_pool(
        org, editor,
        {"application": str(a["app"].id), "sku_name": "M365 E3", "sku_id": E3, "seats_purchased": 50, "seats_assigned": 40},
    )  # fmt: skip
    unbound_pool = contracts.create_pool(
        org, editor,
        {"application": str(a["app"].id), "sku_name": "Flow", "sku_id": "", "seats_purchased": 7, "seats_assigned": 7},
    )  # fmt: skip
    result = run(lic)
    assert (result.counts["pools_synced"], result.counts["pools_held"]) == (1, 0)
    assert (result.counts["pools_to_review"], result.counts["unbound"]) == (1, 2)
    synced.refresh_from_db()
    assert (synced.seats_purchased, synced.seats_assigned) == (250, 231) and synced.last_synced is not None
    event = AuditEvent.objects.get(action="license_pool.synced", target_id=synced.id)
    assert event.actor_id is None and event.changes["seats_purchased"] == [1, 250]
    bound.refresh_from_db()
    assert (bound.seats_purchased, bound.seats_assigned, bound.last_synced) == (50, 40, None)
    unbound_pool.refresh_from_db()
    assert (unbound_pool.seats_purchased, unbound_pool.seats_assigned, unbound_pool.sku_id) == (7, 7, "")
    review = api(a["users"]["viewer"]).get("license-pools/seat-review").json_body
    assert [p["pool"]["id"] for p in review["results"]] == [str(bound.id)]
    assert sorted(u["sku_part_number"] for u in review["unbound"]) == ["DEVELOPERPACK_E5", "FLOW_FREE"]
    # The tenant's counts change: the syncing pool follows, nothing else moves.
    skus = [(E5, "SPE_E5", "Enabled", 260, 0, 0, 240), *entra_simulator.SKUS[1:]]
    monkeypatch.setattr(entra_simulator, "SKUS", skus)
    run(lic)
    synced.refresh_from_db()
    bound.refresh_from_db()
    unbound_pool.refresh_from_db()
    assert (synced.seats_purchased, synced.seats_assigned) == (260, 240)
    assert (bound.seats_purchased, unbound_pool.seats_purchased) == (50, 7)
    assert LicensePool.objects.filter(organization=org).count() == 3


def test_two_connectors_reporting_one_sku_hold_the_sync(a, monkeypatch):
    org = a["org"]
    first = make(a, "entra_licenses", name="Tenant one")
    second = make(a, "entra_licenses", name="Tenant two")
    pool = contracts.create_pool(
        org, a["users"]["editor"],
        {"application": str(a["app"].id), "sku_name": "E5", "sku_id": E5, "seats_purchased": 1, "seats_assigned": 1, "seat_sync": True},
    )  # fmt: skip
    run(first)
    pool.refresh_from_db()
    assert pool.seats_purchased == 250
    held = run(second)
    assert held.counts["pools_held"] == 1 and held.counts["pools_synced"] == 0
    assert any("another connector reports this SKU" in n for n in held.counts["notes"])
    monkeypatch.setattr(entra_simulator, "SKUS", [(E5, "SPE_E5", "Enabled", 300, 0, 0, 1), *entra_simulator.SKUS[1:]])
    assert run(first).counts["pools_held"] == 1 and run(second).counts["pools_held"] == 1
    pool.refresh_from_db()
    assert pool.seats_purchased == 250  # left alone; both sources' counts are proposals
    assert {p.observation.connector_id for p in licences.proposals(org)} == {first.id, second.id}


def test_unbound_skus_create_a_pool_or_bind_one(a, b, api):
    org = a["org"]
    lic = make(a, "entra_licenses", name="Licences")
    run(lic)
    editor, viewer = api(a["users"]["editor"]), api(a["users"]["viewer"])
    flow = Observation.objects.get(connector=lic, external_id=FLOW_FREE)
    dev = Observation.objects.get(connector=lic, external_id=DEVPACK)
    url = f"license-pools/unbound/{flow.id}/create"
    assert viewer.post(url, {"application": str(a["app"].id)}).status_code == 403
    assert editor.post(url, {}).status_code == 400  # an application is required
    assert editor.post(url, {"application": str(a["app"].id), "seats_purchased": 1}).status_code == 400
    r = editor.post(url, {"application": str(a["app"].id), "seat_sync": True})
    assert r.status_code == 201, r.json_body
    created = r.json_body
    assert r["Location"] == f"http://testserver/api/v1/license-pools/{created['id']}"  # the new pool
    assert (created["sku_id"], created["sku_name"], created["seats_purchased"], created["seats_assigned"]) == (
        FLOW_FREE,
        "FLOW_FREE",
        10000,
        12,
    )
    event = AuditEvent.objects.get(action="license_pool.created", target_id=created["id"])
    assert event.changes["source"] == "Licences" and event.changes["observation"] == str(flow.id)
    assert editor.post(url, {"application": str(a["app"].id)}).status_code == 409  # no longer unbound
    # Bind: an existing pool without a SKU ID, under its If-Match.
    pool = contracts.create_pool(
        org, a["users"]["editor"],
        {"application": str(a["app"].id), "sku_name": "Developer pack", "seats_purchased": 30, "seats_assigned": 2},
    )  # fmt: skip
    tag = editor.get(f"license-pools/{pool.id}")["ETag"]
    assert (
        post_if_match(editor, f"license-pools/{pool.id}/bind", {"observation": str(dev.id)}, '"stale"').status_code
        == 412
    )
    r = post_if_match(editor, f"license-pools/{pool.id}/bind", {"observation": str(dev.id)}, tag)
    assert r.status_code == 200 and r.json_body["sku_id"] == DEVPACK and r.json_body["seats_purchased"] == 30
    bound = AuditEvent.objects.filter(action="license_pool.updated", target_id=pool.id).latest("created_at")
    assert bound.changes["sku_id"] == ["", DEVPACK] and bound.changes["source"] == "Licences"
    unbound = viewer.get("license-pools/seat-review").json_body["unbound"]
    assert sorted(u["sku_part_number"] for u in unbound) == ["SPE_E3", "SPE_E5"]  # the two still without a pool
    assert [p.pool.id for p in licences.proposals(org)] == [pool.id]  # its counts differ: a proposal, not a write
    # A pool already bound is refused; another organization's observation doesn't exist.
    other = contracts.create_pool(
        org, a["users"]["editor"], {"application": str(a["app"].id), "sku_name": "E3", "sku_id": E3}
    )
    assert editor.post(f"license-pools/{other.id}/bind", {"observation": str(dev.id)}).status_code == 409
    theirs = make(b, "entra_licenses", name="Theirs")
    run(theirs)
    their_obs = Observation.objects.get(connector=theirs, external_id=E5)
    assert (
        editor.post(f"license-pools/unbound/{their_obs.id}/create", {"application": str(a["app"].id)}).status_code
        == 404
    )
    lone = contracts.create_pool(org, a["users"]["editor"], {"application": str(a["app"].id), "sku_name": "Lone"})
    assert editor.post(f"license-pools/{lone.id}/bind", {"observation": str(their_obs.id)}).status_code == 404
    assert (
        api(b["users"]["admin"]).post(f"license-pools/{lone.id}/bind", {"observation": str(dev.id)}).status_code == 404
    )


# ---- roles, tenancy and audit content ------------------------------------------------------------------------------------


def test_roles_on_the_reconciliation_endpoints(a, api, entra):
    org = a["org"]
    discovery.link(org, a["users"]["editor"], obs(entra, "Zoom"), {"resource": str(a["app"].id)})
    oid = obs(entra, "Zoom").id
    for user in a["users"].values():
        client = api(user)
        assert client.get(f"discovery/observations/{oid}/suggestions").status_code == 200
        assert client.get(f"discovery/observations/{oid}/drift").status_code == 200
        assert client.get("discovery/candidates?status=differs").status_code == 200
    viewer = api(a["users"]["viewer"])
    for path in ("drift/apply", "drift/ignore", "missing/acknowledge", "missing/archive", "reopen"):
        assert viewer.post(f"discovery/observations/{oid}/{path}", {}).status_code == 403, path
    editor = api(a["users"]["editor"])
    body = {"fields": ["name"], "seen": {"name": "Zoom"}}
    assert editor.post(f"discovery/observations/{oid}/drift/ignore", body).status_code == 200
    assert editor.post(f"discovery/observations/{oid}/missing/acknowledge").status_code == 409  # still seen
    assert api().get(f"discovery/observations/{oid}/drift").status_code in (401, 403)


def test_cross_tenant_reconciliation(a, b, api, entra):
    discovery.link(a["org"], a["users"]["editor"], obs(entra, "Zoom"), {"resource": str(a["app"].id)})
    oid = obs(entra, "Zoom").id
    other = api(b["users"]["admin"])
    assert other.get(f"discovery/observations/{oid}/suggestions").status_code == 404
    assert other.get(f"discovery/observations/{oid}/drift").status_code == 404
    for path in ("drift/apply", "drift/ignore", "missing/acknowledge", "missing/archive"):
        assert other.post(f"discovery/observations/{oid}/{path}", {}).status_code == 404, path
    assert other.get("discovery/candidates?status=differs").json_body["counts"]["differs"] == 0
    assert other.get("needs-attention?scope=all").json_body["discovery_missing"] == []
    # Suggestions never offer another organization's records.
    b_zoom = record(b, "Zoom")
    assert str(b_zoom.id) not in {r["id"] for r in discovery.suggestions(a["org"], obs(entra, "Zoom"), 10)}
    # The database refuses a difference pointing at another organization's observation.
    from django.db import IntegrityError, connection, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        SourceDifference.objects.create(
            organization=b["org"], observation_id=oid, field="name", source_value="x", since=timezone.now()
        )
        connection.check_constraints()


def test_no_secret_reaches_audit_or_responses(ent, api):
    org, editor = ent["org"], ent["users"]["editor"]
    entra = make(ent, "entra")
    run(entra)
    hl = make(ent, "halo")
    run(hl)
    discovery.link(org, editor, obs(entra, "Zoom"), {"resource": str(ent["app"].id)})
    client = api(editor)
    oid = obs(entra, "Zoom").id
    responses = [
        client.post(
            f"discovery/observations/{oid}/drift/ignore",
            {"fields": ["description"], "seen": seen(client, oid, ["description"])},
        ),
        client.get(f"discovery/observations/{oid}/drift"),
        client.get(f"discovery/observations/{oid}/suggestions"),
        client.get("discovery/candidates?status=all"),
        client.get("needs-attention?scope=all"),
    ]
    tag = client.get(f"applications/{ent['app'].id}")["ETag"]
    body = {"fields": ["name"], "seen": {"name": "Zoom"}}
    responses.append(post_if_match(client, f"discovery/observations/{oid}/drift/apply", body, tag))
    for r in responses:
        assert r.status_code == 200 and SECRET not in r.content.decode()
    assert SECRET not in all_audit_text()
    assert "secret" not in json.dumps(AuditEvent.objects.get(action="observation.drift_ignored").changes).lower()


# ---- Azure, Power Platform and Halo sources ------------------------------------------------------------------------------


def test_halo_paging_ignores_pages_whose_first_asset_has_no_id(monkeypatch):
    pages: list[list[dict[str, Any]]] = [
        [{"id": None, "key_field": "No ID"}] + [{"id": i, "key_field": f"A{i}"} for i in range(1, 100)],
        [{"id": None, "key_field": "No ID either"}] + [{"id": i, "key_field": f"B{i}"} for i in range(100, 199)],
        [{"id": 500, "key_field": "Last"}],
    ]

    class Client:
        def get(self, url, what, permission):
            page = int(url.rsplit("page_no=", 1)[1])
            return {"assets": pages[page - 1] if page <= len(pages) else []}

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        def client(self, audience):
            return Client()

    monkeypatch.setattr(halo_source, "Session", Session)
    monkeypatch.setattr(halo_source, "transports", lambda *args, **kwargs: None)
    found = halo_source.fetch(SimpleNamespace(config={}), SimpleNamespace(base_url="https://halo.example.test"))
    assert len(found) == 201 and list(getattr(found, "notes", [])) == []
    # The same first ID twice still stops the run reading the same page over and over.
    pages[0][0] = {"id": 1, "key_field": "A0"}
    pages[1] = [{"id": 1, "key_field": "A0"}] + pages[1][1:]
    found = halo_source.fetch(SimpleNamespace(config={}), SimpleNamespace(base_url="https://halo.example.test"))
    assert len(found) == 100 and "same assets" in found.notes[0] and found.unread == {"*"}


def test_a_connector_whose_capability_was_lost_can_still_be_turned_off(ent):
    org, admin = ent["org"], ent["users"]["admin"]
    az = make(ent, "azure")
    # Every edition has discovery.azure; a platform revoke takes it away.
    from core.models import CapabilityOverride

    CapabilityOverride.objects.create(organization=org, capability="discovery.azure", effect="revoke", reason="test")
    caps.forget_organization(org.pk)
    with pytest.raises(CapabilityRequired):
        discovery.update_connector(org, admin, az, {"name": "Renamed"})
    with pytest.raises(CapabilityRequired):
        discovery.update_connector(org, admin, az, {"enabled": False, "name": "Renamed"})
    discovery.update_connector(org, admin, az, {"enabled": False})
    az.refresh_from_db()
    assert az.enabled is False
    with pytest.raises(CapabilityRequired):
        discovery.update_connector(org, admin, az, {"enabled": True})


def test_entra_uses_the_shared_name_rules(a):
    connector = make(a, "entra", scope={"name_exclusions": ["  graph explorer  ", "Old "]})
    connector.refresh_from_db()
    assert connector.scope["name_exclusions"] == ["graph explorer", "Old"]
    result = run(connector)
    assert result.counts["skipped"]["name_exclusions"] == 1  # Graph Explorer Test (Old Intranet is disabled)
    with pytest.raises(FieldErrors) as refused:
        discovery.update_connector(
            a["org"], a["users"]["admin"], connector, {"scope": {"name_exclusions": ["x" * 201]}}
        )
    assert refused.value.errors["scope.name_exclusions"] == [
        "Enter one name or prefix per line, 200 characters at most."
    ]
    from discovery import scopes

    assert scopes.excluded_by_name("Graph Explorer Test", {"name_exclusions": ["  graph "]})
    assert not scopes.excluded_by_name("Anything", {"name_exclusions": ["   "]})


def test_drift_module_rules():
    """A source never proposes clearing a value or one the record can't take."""
    assert drift._valid("documentation_url", "http://insecure.test") is False
    assert drift._valid("entra_app_id", "not-a-guid") is False
    assert drift._valid("environment", "Production") is False and drift._valid("environment", "prod") is True
    assert drift.same("azure_resource_id", "/SUBSCRIPTIONS/A", "/subscriptions/a")
    assert not drift.same("name", "Zoom", "zoom")
    assert drift.fields_of("application")[:3] == ["name", "description", "documentation_url"]
    assert "azure_resource_id" in drift.fields_of("component") and "entra_app_id" not in drift.fields_of("component")


def test_missing_rows_without_the_record_steward_go_to_the_connector_reviewer(a, entra):
    org = a["org"]
    lonely = record(a, "Nobody's app", it_steward=None)
    discovery.update_connector(org, a["users"]["admin"], entra, {"reviewer": a["users"]["admin"].id})
    discovery.link(org, a["users"]["editor"], obs(entra, "ServiceNow"), {"resource": str(lonely.id)})
    call_command("simulate_entra_change", "remove", "ServiceNow")
    run(entra)
    [row] = missing.attention_rows(org)
    assert row["steward_id"] == a["users"]["admin"].id
    assert missing.missing(org).count() == 1
    assert row["since"] <= (timezone.localdate() + timedelta(days=1)).isoformat()


# ---- out of scope is never "no longer seen" -------------------------------------------------------------------------------


def test_a_disabled_app_is_out_of_scope_not_missing_and_comes_back(a, api, entra):
    org, editor = a["org"], a["users"]["editor"]
    discovery.link(org, editor, obs(entra, "Zoom"), {"resource": str(a["app"].id)})
    call_command("simulate_entra_change", "disable", "Zoom")
    result = run(entra)
    zoom = obs(entra, "Zoom")
    assert result.counts["gone"] == 0 and result.counts["out_of_scope"] == 1
    assert zoom.gone_at is None and zoom.out_of_scope == "disabled" and zoom.out_of_scope_at is not None
    assert missing_rows(a)[0] == [] and not SourceDifference.objects.filter(observation=zoom).exists()
    body = api(a["users"]["viewer"]).get(f"discovery/observations/{zoom.id}").json_body
    assert body["out_of_scope"] == "disabled" and body["out_of_scope_label"] == "disabled in Entra"
    assert body["status"] == "linked" and body["missing"] is False
    # An undecided candidate that is disabled is out of scope too, and isn't a candidate.
    call_command("simulate_entra_change", "disable", "Workday")
    run(entra)
    page = api(a["users"]["viewer"]).get("discovery/candidates?status=out_of_scope").json_body
    assert [r["name"] for r in page["results"]] == ["Workday"] and page["counts"]["out_of_scope"] == 1
    assert "Workday" not in [
        r["name"] for r in api(a["users"]["viewer"]).get("discovery/candidates").json_body["results"]
    ]
    # Back in scope: cleared, and the differences come back.
    call_command("simulate_entra_change", "reset")
    run(entra)
    zoom.refresh_from_db()
    assert zoom.out_of_scope == "" and zoom.out_of_scope_at is None
    assert SourceDifference.objects.filter(observation=zoom).exists()


def test_a_new_name_exclusion_makes_nothing_missing(a, entra):
    org = a["org"]
    discovery.link(org, a["users"]["editor"], obs(entra, "Zoom"), {"resource": str(a["app"].id)})
    discovery.update_connector(org, a["users"]["admin"], entra, {"scope": {"name_exclusions": ["Zoom"]}})
    result = run(entra)
    assert result.counts["gone"] == 0 and obs(entra, "Zoom").out_of_scope == "name_exclusions"
    assert missing_rows(a)[0] == []
    # Really deleted while excluded: now it is gone.
    call_command("simulate_entra_change", "remove", "Zoom")
    run(entra)
    assert obs(entra, "Zoom").gone_at is not None and len(missing_rows(a)[0]) == 1


def test_halo_repeated_page_stop_marks_nothing_gone(ent):
    hl = make(ent, "halo")
    configure(hl, simulated_size=150)
    run(hl)
    late = obs(hl, "Licensed tool 0150")
    discovery.link(ent["org"], ent["users"]["editor"], late, {"resource": str(ent["app"].id)})
    configure(hl, simulated_ignore_page_no=True)
    result = run(hl)
    assert result.counts["gone"] == 0 and result.counts["not_read"] > 0
    assert any("nothing is marked no longer seen" in n for n in result.counts["notes"])
    late.refresh_from_db()
    assert late.gone_at is None and missing_rows(ent)[0] == []


def test_scope_left_out_kinds_and_subscriptions_are_out_of_scope(ent):
    org, editor = ent["org"], ent["users"]["editor"]
    pp = make(ent, "powerplatform", {"environment_urls": [f"https://{pp_sim.CONTOSO}"], "include_cloud_flows": True})
    run(pp)
    discovery.create_record(org, editor, obs(pp, "Approve expenses"), {})
    discovery.update_connector(org, ent["users"]["admin"], pp, {"scope": {"include_cloud_flows": False}})
    result = run(pp)
    assert result.counts["gone"] == 0 and obs(pp, "Approve expenses").out_of_scope == "cloud_flows"
    from discovery import azure_simulator

    az = make(ent, "azure")
    run(az)
    discovery.create_record(org, editor, obs(az, "invoice-intake-dev"), {})
    discovery.update_connector(org, ent["users"]["admin"], az, {"scope": {"subscriptions": [azure_simulator.PROD]}})
    result = run(az)
    assert result.counts["gone"] == 0 and obs(az, "invoice-intake-dev").out_of_scope == "subscriptions"
    discovery.update_connector(
        org, ent["users"]["admin"], az, {"scope": {"subscriptions": [], "include_runbooks": False}}
    )
    run(az)
    assert obs(az, "invoice-intake-dev").out_of_scope == ""
    assert missing_rows(ent)[0] == []


def test_services_refuse_a_record_that_is_not_the_decisions(a, entra):
    org, editor = a["org"], a["users"]["editor"]
    zoom = obs(entra, "Zoom")
    discovery.link(org, editor, zoom, {"resource": str(a["app"].id)})
    other = record_(a, "Another record")
    with pytest.raises(Conflict, match="linked to another record"):
        drift.apply(org, editor, zoom, {"fields": ["name"], "seen": {"name": "Zoom"}}, resource=other)
    call_command("simulate_entra_change", "remove", "Zoom")
    run(entra)
    with pytest.raises(Conflict, match="linked to another record"):
        missing.archive(org, editor, obs(entra, "Zoom"), {}, resource=other)
    other.refresh_from_db()
    assert other.archived_at is None and other.name == "Another record"


def test_an_azure_resource_id_keeps_the_sources_casing(ent):
    az = make(ent, "azure")
    run(az)
    invoice = record(ent, "Invoice intake", kind="component")
    target = obs(az, "invoice-intake")
    discovery.link(ent["org"], ent["users"]["editor"], target, {"resource": str(invoice.id)})
    stored = SourceDifference.objects.get(observation=target, field="azure_resource_id")
    assert stored.source_value == target.data["resource_id"] and stored.source_value != stored.source_value.lower()
    services.update_resource(
        ent["org"], ent["users"]["editor"], invoice, {"azure_resource_id": stored.source_value.lower()}
    )
    assert not SourceDifference.objects.filter(observation=target, field="azure_resource_id").exists()


def test_a_syncing_pool_names_its_source_and_says_when_the_sync_is_held(a, api):
    """`synced_from`: the one Entra licences connector reporting the pool's SKU; `sync_held` with two."""
    org, editor = a["org"], a["users"]["editor"]
    client = api(a["users"]["viewer"])
    first = make(a, "entra_licenses", name="Tenant one")

    def pool(sku, sync=True, name="Pool"):
        values = {"application": str(a["app"].id), "sku_name": name, "sku_id": sku, "seat_sync": sync}
        return contracts.create_pool(org, editor, {**values, "seats_purchased": 1, "seats_assigned": 1})

    synced, by_hand, unreported = pool(E5.upper()), pool(E3, sync=False), pool("not-a-reported-sku")
    run(first)
    body = client.get(f"license-pools/{synced.id}").json_body
    assert body["synced_from"] == {"id": str(first.id), "name": "Tenant one"} and body["sync_held"] is False
    for other in (by_hand, unreported):
        body = client.get(f"license-pools/{other.id}").json_body
        assert (body["synced_from"], body["sync_held"]) == (None, False)
    listed = {r["id"]: r for r in client.get("license-pools").json_body["results"]}
    assert listed[str(synced.id)]["synced_from"]["name"] == "Tenant one"
    # A second tenant reports the same SKU: the sync is held and names no source.
    second = make(a, "entra_licenses", name="Tenant two")
    run(second)
    body = client.get(f"license-pools/{synced.id}").json_body
    assert (body["synced_from"], body["sync_held"]) == (None, True)
    # Turned off, the second no longer counts.
    second.enabled = False
    second.save(update_fields=["enabled"])
    body = client.get(f"license-pools/{synced.id}").json_body
    assert body["synced_from"]["id"] == str(first.id) and body["sync_held"] is False


def test_pools_without_a_sku_id_are_one_filter_and_page(a, b, api):
    org, editor = a["org"], a["users"]["editor"]
    client = api(a["users"]["viewer"])
    for i, sku in enumerate(["", "  ", "", E5]):
        contracts.create_pool(
            org, editor,
            {"application": str(a["app"].id), "sku_name": f"Pool {i}", "sku_id": sku, "seats_purchased": 1, "seats_assigned": 0},
        )  # fmt: skip
    contracts.create_pool(
        b["org"], b["users"]["editor"],
        {"application": str(b["app"].id), "sku_name": "Other org", "sku_id": "", "seats_purchased": 1, "seats_assigned": 0},
    )  # fmt: skip
    body = client.get("license-pools?sku_id=none").json_body
    names = sorted(r["name"] if "name" in r else r["sku_name"] for r in body["results"])
    assert len(names) == 3 and "Other org" not in names and all(not r["sku_id"].strip() for r in body["results"])
    first = client.get("license-pools?sku_id=none&page_size=2").json_body
    assert len(first["results"]) == 2 and first["next"]
    r = client.get("license-pools?sku_id=some")
    assert r.status_code == 400 and "sku_id" in r.json_body["errors"]
