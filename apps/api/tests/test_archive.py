"""Archive, restore and admin hard delete."""

import pytest

from catalog import services
from catalog.models import RecordReview, Resource
from core.models import AuditEvent


def make_component(a):
    return services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Nightly job", "kind": "component", "lifecycle": "active"}
    )


def review(a, resource):
    services.record_review(
        a["org"], a["users"]["editor"], resource, {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Email"}
    )


@pytest.mark.parametrize("kind", ["application", "component"])
def test_archive_and_restore_round_trip(a, api, kind):
    resource = a["app"] if kind == "application" else make_component(a)
    client = api(a["users"]["editor"])
    base = f"{kind}s/{resource.id}"

    archived = client.post(base + "/archive")
    assert archived.status_code == 200 and archived.json_body["archived"] is True
    assert archived.json_body["lifecycle"] == "active"
    assert resource.id not in {r["id"] for r in client.get(f"{kind}s").json_body["results"]}
    assert resource.id.__str__() in {r["id"] for r in client.get(f"{kind}s?archived=true").json_body["results"]}
    attention = client.get("needs-attention?scope=all").json_body
    assert str(resource.id) not in {r["id"] for r in attention["ownership_gaps"] + attention["reviews"]}

    restored = client.post(base + "/restore")
    assert restored.status_code == 200 and restored.json_body["archived"] is False
    assert str(resource.id) in {r["id"] for r in client.get(f"{kind}s").json_body["results"]}
    assert list(
        AuditEvent.objects.filter(target_id=resource.id).order_by("created_at").values_list("action", flat=True)
    )[-2:] == ["resource.archived", "resource.restored"]


def test_archive_does_not_change_review_state(a, api):
    review(a, a["app"])
    client = api(a["users"]["admin"])
    before = client.get(f"applications/{a['app'].id}").json_body
    after = client.post(f"applications/{a['app'].id}/archive").json_body
    assert after["review_state"] == before["review_state"] != "needs_confirmation"
    assert after["next_review_date"] == before["next_review_date"]


def test_archive_and_restore_are_idempotent(a, api):
    client = api(a["users"]["editor"])
    url = f"applications/{a['app'].id}"
    assert client.post(url + "/restore").status_code == 200  # not archived: nothing happens
    assert client.post(url + "/archive").status_code == 200
    assert client.post(url + "/archive").status_code == 200
    assert AuditEvent.objects.filter(action__startswith="resource.a").count() == 1
    assert not AuditEvent.objects.filter(action="resource.restored").exists()


def test_viewer_cannot_archive(a, api):
    assert api(a["users"]["viewer"]).post(f"applications/{a['app'].id}/archive").status_code == 403
    a["app"].refresh_from_db()
    assert a["app"].archived_at is None


def test_delete_unreferenced_keeps_audit_trail(a, api):
    component = make_component(a)
    client = api(a["users"]["admin"])
    assert client.delete(f"components/{component.id}").status_code == 204
    assert not Resource.objects.filter(id=component.id).exists()
    assert client.get(f"components/{component.id}").status_code == 404
    actions = set(AuditEvent.objects.filter(target_id=component.id).values_list("action", flat=True))
    assert actions == {"resource.created", "resource.deleted"}


def test_delete_refused_names_blocking_references(a, api):
    app = a["app"]
    for _ in range(3):
        review(a, app)
    services.create_instance(a["org"], a["users"]["editor"], app, {"name": "Production"})
    client = api(a["users"]["admin"])
    response = client.delete(f"applications/{app.id}")
    assert response.status_code == 409
    assert response["Content-Type"] == "application/problem+json"
    body = response.json_body
    assert body["detail"] == f"{app.name} can't be deleted because it has 3 reviews and 1 instance. Archive it instead."
    assert body["blocking_references"] == {"review": 3, "instance": 1}
    assert Resource.objects.filter(id=app.id).exists() and RecordReview.objects.count() == 3
    assert not AuditEvent.objects.filter(action="resource.deleted").exists()


def test_editor_cannot_hard_delete(a, api):
    component = make_component(a)
    assert api(a["users"]["editor"]).delete(f"components/{component.id}").status_code == 403
    assert Resource.objects.filter(id=component.id).exists()


@pytest.mark.parametrize("kind", ["application", "component"])
def test_detail_reports_who_and_when_archived(a, api, kind):
    resource = a["app"] if kind == "application" else make_component(a)
    editor = a["users"]["editor"]
    client = api(editor)
    url = f"{kind}s/{resource.id}"
    before = client.get(url).json_body
    assert before["archived_at"] is None and before["archived_by"] is None
    client.post(url + "/archive")
    body = client.get(url).json_body
    assert body["archived"] is True and body["archived_at"]
    assert body["archived_by"] == {"id": editor.id, "name": editor.get_full_name() or editor.email}
    client.post(url + "/restore")
    after = client.get(url).json_body
    assert after["archived_at"] is None and after["archived_by"] is None
