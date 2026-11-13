"""Discovery API: roles, tenancy, the write-only secret, and the decisions over HTTP."""

import json

import pytest

from core import connections
from core.models import AuditEvent
from discovery import engine as discovery
from discovery.models import Connector, Observation
from tests.conftest import drain_jobs

NEW = {"tenant_id": "simulated", "client_id": "", "secret": "synthetic-secret"}


def connector_tested(a, connector):
    """Test the connector's own connection (a run needs one tested at its current revision)."""
    connections.run_test(a["org"], a["users"]["admin"], connector.microsoft_connection)
    return connector


@pytest.fixture
def ran(a):
    connector = connector_tested(a, discovery.create_connector(a["org"], a["users"]["admin"], dict(NEW)))
    discovery.run(connector)
    return connector


def obs_id(connector, name):
    return str(Observation.objects.get(connector=connector, display_name=name).id)


def test_admin_configures_and_the_secret_never_comes_back(a, api):
    admin = api(a["users"]["admin"])
    r = admin.post("connectors", {**NEW, "name": "Entra (synthetic)", "reviewer": a["users"]["editor"].id})
    assert r.status_code == 201, r.json_body
    body = r.json_body
    assert body["secret_set_on"] and body["simulated"] and body["reviewer"]["id"] == a["users"]["editor"].id
    assert "secret" not in body and "synthetic-secret" not in json.dumps(body)
    cid = body["id"]
    for user in a["users"].values():
        for url in ("connectors", f"connectors/{cid}", f"connectors/{cid}/runs"):
            assert "synthetic-secret" not in json.dumps(api(user).get(url).json_body)
    r = admin.patch(f"connectors/{cid}", {"scope": {"name_exclusions": ["Graph "]}, "secret": "replaced-secret"})
    assert r.status_code == 200 and r.json_body["scope"]["name_exclusions"] == ["Graph"]
    replaced = AuditEvent.objects.filter(action="microsoft_connection.updated").get()  # the connection's secret
    assert replaced.changes["replaced"] == ["secret"] and replaced.changes["revision"] == [1, 2]
    assert "replaced-secret" not in json.dumps(list(AuditEvent.objects.values_list("changes", flat=True)))


def test_connector_validation(a, api):
    admin = api(a["users"]["admin"])
    r = admin.post("connectors", {"tenant_id": "http://169.254.169.254", "client_id": "x"})
    assert r.status_code == 400
    assert set(r.json_body["errors"]) == {"tenant_id", "client_id", "secret"}
    r = admin.post("connectors", {**NEW, "scope": {"environments": ["x"]}, "reviewer": a["users"]["viewer"].id})
    assert r.status_code == 400 and set(r.json_body["errors"]) == {"scope", "reviewer"}


def test_role_matrix(a, api, ran):
    cid = str(ran.id)
    oid = obs_id(ran, "Zoom")
    writes = {
        ("post", "connectors", json.dumps({**NEW, "name": "Second"})): "admin",
        ("patch", f"connectors/{cid}", json.dumps({"schedule": "daily"})): "admin",
        ("post", f"connectors/{cid}/run", "{}"): "admin",
        ("post", f"connectors/{cid}/access-check", "{}"): "admin",
        ("post", f"discovery/observations/{oid}/dismiss", json.dumps({"reason": "Not ours"})): "editor",
    }
    for (method, url, body), lowest in writes.items():
        for role, user in a["users"].items():
            allowed = role == "admin" or (lowest == "editor" and role == "editor")
            r = api(user)._call(method, url, json.loads(body))
            assert (r.status_code < 400) == allowed or r.status_code == 409, (method, url, role, r.status_code)
            if r.status_code < 400 and method == "post" and url.endswith("dismiss"):
                api(a["users"]["admin"]).post(f"discovery/observations/{oid}/reopen")
    for user in a["users"].values():
        reader = api(user)
        assert reader.get("connectors").status_code == 200
        assert reader.get("discovery/candidates").status_code == 200
        assert reader.get("discovery/credentials").status_code == 200
        assert reader.get(f"applications/{a['app'].id}/discovery-sources").status_code == 200
    assert api(a["users"]["editor"]).delete(f"connectors/{cid}").status_code == 403
    assert api().get("connectors").status_code in (401, 403)


def test_cross_tenant(a, b, api, ran):
    other = api(b["users"]["admin"])
    oid = obs_id(ran, "Zoom")
    assert other.get(f"connectors/{ran.id}").status_code == 404
    assert other.patch(f"connectors/{ran.id}", {"name": "Mine now"}).status_code == 404
    assert other.post(f"connectors/{ran.id}/run").status_code == 404
    assert other.post(f"connectors/{ran.id}/access-check").status_code == 404
    assert other.get("discovery/credentials").json_body == []
    assert other.post(f"discovery/observations/{oid}/dismiss", {"reason": "x"}).status_code == 404
    assert other.get("discovery/candidates").json_body["counts"]["candidates"] == 0
    assert other.get(f"applications/{a['app'].id}/discovery-sources").status_code == 404
    # Linking to another organization's record looks like an unknown record.
    r = api(a["users"]["editor"]).post(f"discovery/observations/{oid}/link", {"resource": str(b["app"].id)})
    assert r.status_code == 400


def test_candidates_and_decisions(a, api, ran):
    editor = api(a["users"]["editor"])
    page = editor.get("discovery/candidates").json_body
    assert page["counts"]["candidates"] == page["counts"]["new"] == 9 and len(page["results"]) == 9
    zoom = next(r for r in page["results"] if r["name"] == "Zoom")
    assert zoom["status"] == "new" and zoom["suggested_owners"][0]["email"] == "dana.ortiz@example.test"
    r = editor.post(f"discovery/observations/{zoom['id']}/link", {"resource": str(a["app"].id)})
    assert r.status_code == 200 and r.json_body["status"] == "linked"
    assert r.json_body["decision"]["record"]["id"] == str(a["app"].id)
    r = editor.post(f"discovery/observations/{obs_id(ran, 'ServiceNow')}/create", {})
    assert r.status_code == 201 and r.json_body["decision"]["decision"] == "created"
    r = editor.post(f"discovery/observations/{obs_id(ran, 'Contract Vault')}/dismiss", {"reason": "Legal's"})
    assert r.json_body["status"] == "dismissed"
    counts = editor.get("discovery/candidates?status=linked").json_body["counts"]
    assert counts == {
        "new": 6,
        "changed": 0,
        "linked": 2,
        "dismissed": 1,
        "gone": 0,
        "candidates": 6,
        "out_of_scope": 0,
        "missing": 0,
        "differs": 1,  # Zoom against the curated app; ServiceNow was created from the source, so it doesn't differ
    }
    assert editor.get("discovery/candidates?status=nope").status_code == 400
    assert len(editor.get("discovery/candidates?q=zoo&status=all").json_body["results"]) == 1
    sources = editor.get(f"applications/{a['app'].id}/discovery-sources").json_body
    assert len(sources) == 1 and sources[0]["differs"] == [
        "name",
        "description",
        "documentation_url",
        "entra_app_id",
        "service_principal_id",
    ]
    assert sources[0]["source_values"]["name"] == "Zoom"


def test_paging(a, api):
    connector = connector_tested(a, discovery.create_connector(a["org"], a["users"]["admin"], dict(NEW)))
    connector.config = {**connector.config, "simulated_size": 120}
    connector.save()
    discovery.run(connector)
    viewer = api(a["users"]["viewer"])
    first = viewer.get("discovery/candidates").json_body
    assert len(first["results"]) == 50 and first["next"] == "50"
    third = viewer.get("discovery/candidates?cursor=100").json_body
    assert third["next"] is None and len(third["results"]) == first["counts"]["candidates"] - 100


def test_run_now_queues_for_the_worker(a, api, ran):
    admin = api(a["users"]["admin"])
    r = admin.post(f"connectors/{ran.id}/run")
    assert r.status_code == 202 and r.json_body["run_requested"]
    assert r.json_body["active_run"]["status"] == "queued" and r.json_body["active_run"]["job_id"]
    assert r["Location"] == f"http://testserver/api/v1/connectors/{ran.id}/runs/{r.json_body['active_run']['id']}"
    assert admin.post(f"connectors/{ran.id}/run").status_code == 409  # one queued or running run per connector
    assert discovery.due_runs() == 0  # already queued
    assert len(drain_jobs()) == 1
    body = admin.get(f"connectors/{ran.id}").json_body
    assert not body["run_requested"] and body["last_run"]["requested_by"]["id"] == a["users"]["admin"].id
    admin.patch(f"connectors/{ran.id}", {"enabled": False})
    assert admin.post(f"connectors/{ran.id}/run").status_code == 409


def test_delete_keeps_records(a, api, ran):
    editor = api(a["users"]["editor"])
    created = editor.post(f"discovery/observations/{obs_id(ran, 'ServiceNow')}/create", {}).json_body
    rid = created["decision"]["record"]["id"]
    assert api(a["users"]["admin"]).delete(f"connectors/{ran.id}").status_code == 204
    assert not Connector.objects.filter(id=ran.id).exists()
    assert editor.get(f"applications/{rid}").status_code == 200


# ---- the sources ---------------------------------------------------------------------------------------


# Every source, the preview ones (Okta, Google Workspace) included; tests/test_preview_features.py covers them off.
@pytest.mark.usefixtures("preview_on")
def test_the_sources_say_what_each_needs_before_a_connector_exists(a, api):
    from discovery import azure, entra, halo

    body = api(a["users"]["viewer"]).get("discovery/sources").json_body
    assert [s["id"] for s in body] == [
        "entra",
        "entra_licenses",
        "azure",
        "powerplatform",
        "halo",
        "entra_shadow_it",
        "entra_coverage",
        "okta",
        "google_workspace",
        "entra_licence_usage",
    ]
    by = {s["id"]: s for s in body}
    assert by["entra"]["label"] == "Microsoft Entra ID" and by["entra"]["capability"] == "discovery.entra"
    assert by["entra"]["connection_kind"] == "microsoft" and by["halo"]["connection_kind"] == "halo"
    assert by["entra"]["permissions"] == [p.as_dict() for p in entra.PERMISSIONS]
    assert {p["name"] for p in by["entra"]["permissions"]} >= {"Application.Read.All"}
    assert by["azure"]["permissions"] == [p.as_dict() for p in azure.PERMISSIONS]
    assert by["entra"]["scope_defaults"] == entra.SCOPE_DEFAULTS and by["halo"]["filters"] == halo.FILTERS
    # A community organization: every source (Okta and Google Workspace with preview features on, as in this module).
    assert {s["id"]: s["available"] for s in body} == {
        "entra": True,
        "entra_licenses": True,
        "azure": True,
        "powerplatform": True,
        "halo": True,
        "entra_shadow_it": True,
        "entra_coverage": True,
        "okta": True,
        "google_workspace": True,
        "entra_licence_usage": True,
    }
    assert Connector.objects.filter(organization=a["org"]).count() == 0  # nothing was needed, nothing was made


# Every route and capability, the preview features (Okta, Google Workspace, ServiceNow, Halo) included.
@pytest.mark.usefixtures("preview_on")
def test_the_sources_follow_the_capabilities_for_every_role(a, b, api):
    from tests.conftest import revoke_capability

    for name in ("discovery.okta", "discovery.google"):
        revoke_capability(a["org"], name)
    for role in ("viewer", "editor", "admin"):
        mine = api(a["users"][role]).get("discovery/sources")
        theirs = api(b["users"][role]).get("discovery/sources")
        assert mine.status_code == theirs.status_code == 200, role
        assert all(s["available"] for s in mine.json_body if s["id"] in ("azure", "powerplatform", "halo"))
        assert not any(s["available"] for s in mine.json_body if s["id"] in ("okta", "google_workspace"))
        assert all(s["available"] for s in theirs.json_body)  # every source, in community too
    assert api().get("discovery/sources").status_code in (401, 403)
    assert api(a["users"]["admin"]).post("discovery/sources", {}).status_code == 405


def test_a_connection_of_another_kind_is_refused_not_ignored(a, api, ran):
    # An Entra connector signs in with a Microsoft connection; an okta_connection in the PATCH is a field error.
    admin = api(a["users"]["admin"])
    before = Connector.objects.values().get(id=ran.id)
    for field in ("okta_connection", "halo_connection", "google_workspace_connection"):
        r = admin.patch(f"connectors/{ran.id}", {"name": "Renamed", field: "00000000-0000-0000-0000-000000000001"})
        assert r.status_code == 400 and field in r.json_body["errors"], r.json_body
    assert Connector.objects.values().get(id=ran.id) == before


def test_walking_the_queue_with_after_while_dismissing_misses_nothing(a, api, ran):
    # 120 candidates, walked 50 at a time with next_after while two are dismissed: every one not dismissed
    # before it was reached comes back exactly once (the offset cursor skipped one per dismissal).
    from django.utils import timezone

    now = timezone.now()
    Observation.objects.filter(connector=ran).delete()
    Observation.objects.bulk_create(
        Observation(
            organization=a["org"],
            connector=ran,
            kind="application",
            external_id=f"walk-{i}",
            display_name=f"Walk {i:03d}",
            data={},
            fingerprint=f"walk-{i}",
            first_seen_at=now,
            last_seen_at=now,
            last_changed_at=now,
        )
        for i in range(120)
    )
    editor = api(a["users"]["editor"])
    seen, after, pages = [], None, 0
    while True:
        body = editor.get("discovery/candidates" + (f"?after={after}" if after else "")).json_body
        seen += [o["name"] for o in body["results"]]
        pages += 1
        if pages == 1:  # two already-seen rows are decided before the next page is asked for
            for name in ("Walk 000", "Walk 010"):
                editor.post(f"discovery/observations/{obs_id(ran, name)}/dismiss", {"reason": "Not ours"})
        after = body["next_after"]
        if not after:
            break
    assert seen == [f"Walk {i:03d}" for i in range(120)] and pages == 3
    refused = editor.get("discovery/candidates?after=not-a-token")
    assert refused.status_code == 400 and "after" in refused.json_body["errors"]
    assert editor.get("discovery/candidates?after=x&cursor=50").status_code == 400
