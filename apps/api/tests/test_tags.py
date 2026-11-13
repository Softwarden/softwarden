"""Tags on records: set on create and update, shown on detail and lists, filtered, listed in use."""

from catalog import services
from catalog.models import ResourceTag, Tag
from core.models import AuditEvent


def test_tags_on_create_update_detail_and_list(a, api):
    client = api(a["users"]["editor"])
    body = {"name": "Ledger", "lifecycle": "active", "tags": ["Finance", " finance ", "Core"]}
    created = client.post("applications", body)
    assert created.status_code == 201
    assert created.json_body["tags"] == ["Core", "Finance"]
    rid = created.json_body["id"]
    event = AuditEvent.objects.get(action="resource.created", target_id=rid)
    assert sorted(event.changes["tags"]) == ["Core", "Finance"]

    updated = client.patch(f"applications/{rid}", {"tags": ["finance", "Legal"]})
    assert updated.status_code == 200 and updated.json_body["tags"] == ["Finance", "Legal"]  # existing spelling kept
    change = AuditEvent.objects.filter(action="resource.updated", target_id=rid).latest("created_at").changes
    assert change["tags"] == [["Core", "Finance"], ["Finance", "Legal"]]
    assert client.get(f"applications/{rid}").json_body["tags"] == ["Finance", "Legal"]
    rows = {r["id"]: r for r in client.get("applications").json_body["results"]}
    assert rows[rid]["tags"] == ["Finance", "Legal"] and rows[str(a["app"].id)]["tags"] == []
    assert Tag.objects.filter(organization=a["org"]).count() == 3  # Finance, Core, Legal (one Finance only)


def test_identical_tags_change_nothing(a, api):
    client = api(a["users"]["editor"])
    rid = client.post("components", {"name": "Job", "lifecycle": "active", "tags": ["Ops"]}).json_body["id"]
    client.patch(f"components/{rid}", {"tags": ["ops"]})
    event = AuditEvent.objects.filter(target_id=rid, action="resource.updated").latest("created_at")
    assert event.changes == {}
    assert ResourceTag.objects.filter(resource_id=rid).count() == 1


def test_tag_validation(a, api):
    client = api(a["users"]["editor"])
    assert client.patch(f"applications/{a['app'].id}", {"tags": "Finance"}).status_code == 400
    assert client.patch(f"applications/{a['app'].id}", {"tags": ["x" * 61]}).json_body["errors"]["tags"]
    assert client.patch(f"applications/{a['app'].id}", {"tags": [f"t{i}" for i in range(21)]}).status_code == 400
    assert client.post("applications", {"name": "N", "lifecycle": "active", "tags": [1]}).status_code == 400
    assert not Tag.objects.exists()


def test_viewer_cannot_set_tags(a, api):
    assert api(a["users"]["viewer"]).patch(f"applications/{a['app'].id}", {"tags": ["X"]}).status_code == 403


def test_tag_filter_is_case_insensitive_and_scoped(a, b, api):
    client = api(a["users"]["editor"])
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"tags": ["Finance"]})
    services.update_resource(b["org"], b["users"]["editor"], b["app"], {"tags": ["Finance"]})
    got = client.get("applications?tag=FINANCE").json_body["results"]
    assert [r["id"] for r in got] == [str(a["app"].id)]
    assert client.get("applications?tag=Nope").json_body["results"] == []
    assert client.get("components?tag=Finance").json_body["results"] == []
    assert client.get("integrations?tag=Finance").status_code == 400  # not an integration filter


def test_tags_endpoint_lists_tags_in_use_for_every_role(a, b, api):
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"tags": ["Finance", "Core"]})
    services.update_resource(b["org"], b["users"]["editor"], b["app"], {"tags": ["Other org"]})
    Tag.objects.create(organization=a["org"], name="Unused")
    for role in ("admin", "editor", "viewer"):
        got = api(a["users"][role]).get("tags")
        assert got.status_code == 200
        assert [(t["name"], t["count"]) for t in got.json_body] == [("Core", 1), ("Finance", 1)]
    assert api().get("tags").status_code in (401, 403)
