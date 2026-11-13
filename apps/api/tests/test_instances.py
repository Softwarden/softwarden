"""Application instances."""

from catalog import services
from catalog.models import ApplicationInstance
from core.models import AuditEvent


def test_create_edit_delete_are_audited(a, api):
    client = api(a["users"]["editor"])
    base = f"applications/{a['app'].id}/instances"
    created = client.post(
        base,
        {"name": "Production", "environment": "Production", "url": "https://erp.example.test", "status": "operational"},
    )
    assert created.status_code == 201
    body = created.json_body
    assert body["name"] == "Production" and body["status"] == "operational" and body["application"] == str(a["app"].id)
    assert str(AuditEvent.objects.get(action="instance.created").target_id) == body["id"]

    edited = client.patch(f"{base}/{body['id']}", {"status": "degraded"})
    assert edited.status_code == 200 and edited.json_body["status"] == "degraded"
    assert AuditEvent.objects.get(action="instance.updated").changes["status"] == ["operational", "degraded"]

    assert client.delete(f"{base}/{body['id']}").status_code == 204
    assert not ApplicationInstance.objects.exists()
    event = AuditEvent.objects.get(action="instance.deleted")
    assert event.actor == a["users"]["editor"] and event.organization == a["org"]


def test_defaults_and_listing(a, api):
    client = api(a["users"]["viewer"])
    assert client.get(f"applications/{a['app'].id}/instances").json_body == []
    api(a["users"]["editor"]).post(f"applications/{a['app'].id}/instances", {"name": "Sandbox"})
    listed = client.get(f"applications/{a['app'].id}/instances").json_body
    assert [i["name"] for i in listed] == ["Sandbox"] and listed[0]["status"] == "unknown"


def test_application_detail_includes_instances(a, api):
    client = api(a["users"]["editor"])
    client.post(f"applications/{a['app'].id}/instances", {"name": "Production"})
    detail = client.get(f"applications/{a['app'].id}").json_body
    assert [i["name"] for i in detail["instances"]] == ["Production"]
    assert "instances" not in client.get("applications").json_body["results"][0]


def test_duplicate_name_is_a_field_problem(a, api):
    client = api(a["users"]["editor"])
    base = f"applications/{a['app'].id}/instances"
    client.post(base, {"name": "Production"})
    response = client.post(base, {"name": "production"})
    assert response.status_code == 400
    assert response["Content-Type"] == "application/problem+json"
    assert "production" in response.json_body["errors"]["name"][0]
    assert ApplicationInstance.objects.count() == 1


def test_same_name_allowed_on_another_application_and_when_renaming_itself(a, api):
    editor = a["users"]["editor"]
    other = services.create_resource(a["org"], editor, {"name": "Other", "lifecycle": "active"})
    client = api(editor)
    first = client.post(f"applications/{a['app'].id}/instances", {"name": "Production"}).json_body
    assert client.post(f"applications/{other.id}/instances", {"name": "Production"}).status_code == 201
    assert (
        client.patch(f"applications/{a['app'].id}/instances/{first['id']}", {"name": "PRODUCTION"}).status_code == 200
    )
    second = client.post(f"applications/{a['app'].id}/instances", {"name": "Sandbox"}).json_body
    clash = client.patch(f"applications/{a['app'].id}/instances/{second['id']}", {"name": "production"})
    assert clash.status_code == 400 and "name" in clash.json_body["errors"]


def test_validation_errors(a, api):
    client = api(a["users"]["editor"])
    response = client.post(
        f"applications/{a['app'].id}/instances", {"name": " ", "url": "not a url", "status": "broken"}
    )
    assert response.status_code == 400
    assert set(response.json_body["errors"]) == {"name", "url", "status"}
    assert not ApplicationInstance.objects.exists()
