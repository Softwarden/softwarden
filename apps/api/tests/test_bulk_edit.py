"""Bulk edit."""

import uuid
from datetime import date, timedelta

import pytest

from catalog import bulk, services
from catalog.models import RecordReview, Resource
from catalog.state import review_state
from core.models import AuditEvent

ROLES = ["admin", "editor", "viewer"]


def make(a, name, kind="application", **extra):
    return services.create_resource(
        a["org"], a["users"]["editor"], {"name": name, "lifecycle": "active", "kind": kind, **extra}
    )


def review(a, resource, **extra):
    return services.record_review(
        a["org"],
        a["users"]["editor"],
        resource,
        {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Email", **extra},
    )


def body(ids, kind="application", **changes):
    return {"kind": kind, "ids": [str(i) for i in ids], "changes": changes}


def state(a, resource):
    resource = Resource.objects.get(id=resource.id)
    latest = RecordReview.objects.filter(resource=resource).order_by("-confirmation_date", "-created_at").first()
    return review_state(resource, latest, services.org_today(a["org"]))[0]


@pytest.mark.parametrize("path", ["bulk-edit/preview", "bulk-edit/apply"])
@pytest.mark.parametrize("role,status", [("admin", 200), ("editor", 200), ("viewer", 403)])
def test_role_matrix(a, api, path, role, status):
    response = api(a["users"][role]).post(path, body([a["app"].id], support_team=str(a["team"].id)))
    assert response.status_code == status
    if status == 403:
        assert Resource.objects.get(id=a["app"].id).support_team is None


@pytest.mark.parametrize("path", ["bulk-edit/preview", "bulk-edit/apply"])
def test_cross_tenant_ids_and_references(a, b, api, path):
    editor = api(a["users"]["editor"])
    mine = make(a, "Mine")
    # another organization's record
    assert editor.post(path, body([mine.id, b["app"].id], add_tags=["X"])).status_code == 404
    # unknown id
    assert editor.post(path, body([uuid.uuid4()], add_tags=["X"])).status_code == 404
    # another organization's person or team
    for field, value in (("business_owner", b["people"]["dana"].id), ("support_team", b["team"].id)):
        refused = editor.post(path, body([mine.id], **{field: str(value)}))
        assert refused.status_code == 400 and field in refused.json_body["errors"]
    # a record of the other kind is not found either
    assert editor.post(path, body([mine.id], kind="component", add_tags=["X"])).status_code == 404
    assert not Resource.objects.get(id=mine.id).tag_links.exists()
    assert not AuditEvent.objects.filter(action="resource.updated", target_id=b["app"].id).exists()


def test_validation(a, api):
    editor, rid = api(a["users"]["editor"]), a["app"].id
    path = "bulk-edit/preview"
    assert editor.post(path, body([rid], lifecycle="gone")).status_code == 400
    assert editor.post(path, body([rid], description="x")).status_code == 400  # only the listed fields
    assert editor.post(path, body([rid], name="x")).status_code == 400
    assert editor.post(path, body([rid])).status_code == 400
    assert editor.post(path, body([])).status_code == 400
    assert editor.post(path, body([rid], kind="integration", add_tags=["X"])).status_code == 400
    assert editor.post(path, body([rid], business_owner=str(a["people"]["gone"].id))).status_code == 400  # inactive
    assert editor.post(path, body([rid], next_review_date="soon")).status_code == 400
    assert editor.post(path, body([rid], add_tags="X")).status_code == 400
    assert editor.post(path, body([uuid.uuid4() for _ in range(201)], add_tags=["X"])).status_code == 400
    assert (
        editor.post(path, {"kind": "application", "ids": ["nope"], "changes": {"add_tags": ["X"]}}).status_code == 400
    )


def test_preview_shows_before_and_after_and_writes_nothing(a, api):
    one, two = make(a, "One"), make(a, "Two", technical_owner=str(a["people"]["dana"].id))
    review(a, one)
    review(a, two)
    two.refresh_from_db()
    editor = api(a["users"]["editor"])
    response = editor.post(
        "bulk-edit/preview",
        body([one.id, two.id], technical_owner=str(a["people"]["dana"].id), add_tags=["Finance"]),
    )
    assert response.status_code == 200
    got = response.json_body
    assert got["kind"] == "application"
    assert got["summary"] == {"total": 2, "changed": 2, "unchanged": 0, "errors": 0, "will_need_confirmation": 1}
    rows = {r["id"]: r for r in got["rows"]}
    first, second = rows[str(one.id)], rows[str(two.id)]
    assert first["name"] == "One" and first["errors"] == [] and first["unchanged"] is False
    assert first["changes"]["technical_owner"] == {
        "before": None,
        "after": {"id": str(a["people"]["dana"].id), "name": "Dana Ortiz"},
    }
    assert first["changes"]["tags"] == {"before": [], "after": ["Finance"]}
    assert first["will_need_confirmation"] is True  # reviewed with an empty technical owner
    assert "technical_owner" not in second["changes"] and second["will_need_confirmation"] is False
    assert second["changes"] == {"tags": {"before": [], "after": ["Finance"]}}
    assert Resource.objects.get(id=one.id).technical_owner is None
    assert not AuditEvent.objects.filter(action="resource.updated", target_id=one.id).exists()
    assert not Resource.objects.get(id=one.id).tag_links.exists()


def test_unreviewed_records_do_not_need_confirmation(a, api):
    one = make(a, "One")
    got = api(a["users"]["editor"]).post("bulk-edit/preview", body([one.id], support_team=str(a["team"].id)))
    assert got.json_body["rows"][0]["will_need_confirmation"] is False


def test_apply_changes_each_record_with_its_own_bulk_event(a, api):
    editor = api(a["users"]["editor"])
    one, two = make(a, "One"), make(a, "Two", tags=["Old", "Keep"])
    review(a, one)
    ids = [one.id, two.id]
    response = editor.post(
        "bulk-edit/apply",
        body(
            ids, business_owner=str(a["people"]["priya"].id), support_team=None, add_tags=["New"], remove_tags=["old"]
        ),
    )
    assert response.status_code == 200
    got = response.json_body
    assert (got["updated"], got["unchanged"]) == (2, 0)
    one.refresh_from_db()
    two.refresh_from_db()
    assert one.business_owner == a["people"]["priya"] and two.business_owner == a["people"]["priya"]
    assert editor.get(f"applications/{two.id}").json_body["tags"] == ["Keep", "New"]
    assert editor.get(f"applications/{one.id}").json_body["tags"] == ["New"]
    for rid in ids:
        events = AuditEvent.objects.filter(action="resource.updated", target_id=rid, changes__bulk=True)
        assert events.count() == 1
        changes = events.get().changes
        assert changes["business_owner"] == [None, str(a["people"]["priya"].id)]
        assert changes["bulk"] is True and events.get().actor == a["users"]["editor"]
    assert AuditEvent.objects.get(target_id=two.id, changes__bulk=True).changes["tags"] == [
        ["Keep", "Old"],
        ["Keep", "New"],
    ]
    assert state(a, one) == "needs_confirmation"  # a confirmed field changed after its review
    assert state(a, two) == "unreviewed"


def test_identical_values_are_unchanged_and_leave_review_state(a, api):
    editor = api(a["users"]["editor"])
    app = make(a, "Owned", business_owner=str(a["people"]["dana"].id), tags=["Finance"])
    review(a, app)
    before_events = AuditEvent.objects.filter(target_id=app.id).count()
    changes = {"business_owner": str(a["people"]["dana"].id), "add_tags": ["finance"], "remove_tags": ["absent"]}
    preview = editor.post("bulk-edit/preview", body([app.id], **changes)).json_body
    assert preview["rows"][0]["unchanged"] is True and preview["rows"][0]["changes"] == {}
    assert preview["summary"]["unchanged"] == 1 and preview["summary"]["will_need_confirmation"] == 0
    result = editor.post("bulk-edit/apply", body([app.id], **changes)).json_body
    assert (result["updated"], result["unchanged"]) == (0, 1)
    assert AuditEvent.objects.filter(target_id=app.id).count() == before_events
    assert state(a, app) == "confirmed"


def test_next_review_date_edits_the_latest_review_without_a_new_one(a, api):
    editor = api(a["users"]["editor"])
    app = make(a, "Reviewed")
    older = review(a, app, confirmation_date=(date.today() - timedelta(days=30)).isoformat())
    latest = review(a, app)
    target = (latest.next_review_date + timedelta(days=45)).isoformat()
    preview = editor.post("bulk-edit/preview", body([app.id], next_review_date=target)).json_body
    assert preview["rows"][0]["changes"] == {
        "next_review_date": {"before": latest.next_review_date.isoformat(), "after": target}
    }
    assert preview["rows"][0]["will_need_confirmation"] is False
    assert editor.post("bulk-edit/apply", body([app.id], next_review_date=target)).status_code == 200
    assert RecordReview.objects.filter(resource=app).count() == 2
    latest.refresh_from_db()
    older.refresh_from_db()
    assert latest.next_review_date.isoformat() == target
    assert older.next_review_date != latest.next_review_date
    assert editor.get(f"applications/{app.id}").json_body["next_review_date"] == target
    event = AuditEvent.objects.get(target_id=app.id, changes__bulk=True)
    assert event.action == "resource.updated" and event.changes["next_review_date"][1] == target
    assert state(a, app) == "confirmed"
    # the same date again is unchanged
    again = editor.post("bulk-edit/apply", body([app.id], next_review_date=target)).json_body
    assert again["unchanged"] == 1


def test_records_without_a_review_cannot_take_a_next_review_date(a, api):
    editor = api(a["users"]["editor"])
    reviewed, bare = make(a, "Reviewed"), make(a, "Bare")
    review(a, reviewed)
    target = (date.today() + timedelta(days=200)).isoformat()
    request = body([reviewed.id, bare.id], next_review_date=target, add_tags=["Cleanup"])
    preview = editor.post("bulk-edit/preview", request).json_body
    rows = {r["name"]: r for r in preview["rows"]}
    assert rows["Bare"]["errors"] and "no review" in rows["Bare"]["errors"][0]
    assert rows["Reviewed"]["errors"] == [] and preview["summary"]["errors"] == 1
    refused = editor.post("bulk-edit/apply", request)
    assert refused.status_code == 400
    assert refused.json_body["rows"] == [
        {
            "id": str(bare.id),
            "name": "Bare",
            "errors": rows["Bare"]["errors"],
            "codes": ["no_review"],
            "field_errors": {},
        }
    ]
    assert not Resource.objects.get(id=reviewed.id).tag_links.exists()  # all or nothing
    assert not AuditEvent.objects.filter(action="resource.updated", changes__bulk=True).exists()


def test_next_review_date_must_follow_the_last_confirmation(a, api):
    app = make(a, "Reviewed")
    review(a, app)
    preview = api(a["users"]["editor"]).post(
        "bulk-edit/preview", body([app.id], next_review_date=(date.today() - timedelta(days=1)).isoformat())
    )
    assert "after the last confirmation" in preview.json_body["rows"][0]["errors"][0]


def test_archived_records_are_refused(a, api):
    editor = api(a["users"]["editor"])
    live, gone = make(a, "Live"), make(a, "Gone")
    services.archive_resource(a["org"], a["users"]["editor"], gone)
    request = body([live.id, gone.id], add_tags=["X"])
    preview = editor.post("bulk-edit/preview", request).json_body
    assert preview["summary"]["errors"] == 1
    assert [r["name"] for r in preview["rows"] if r["errors"]] == ["Gone"]
    assert editor.post("bulk-edit/apply", request).status_code == 400
    assert not Resource.objects.get(id=live.id).tag_links.exists()


def test_apply_is_one_transaction(a, monkeypatch):
    one, two = make(a, "One"), make(a, "Two")
    real = services.update_resource
    calls = []

    def flaky(org, actor, resource, data, audit_extra=None):
        calls.append(resource.id)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(org, actor, resource, data, audit_extra=audit_extra)

    monkeypatch.setattr(services, "update_resource", flaky)
    with pytest.raises(RuntimeError):
        bulk.apply(a["org"], a["users"]["editor"], body([one.id, two.id], support_team=str(a["team"].id)))
    assert Resource.objects.filter(organization=a["org"], support_team__isnull=False).count() == 0
    assert not AuditEvent.objects.filter(action="resource.updated", changes__bulk=True).exists()


def test_components_work_too(a, api):
    component = make(a, "Job", "component")
    got = api(a["users"]["admin"]).post(
        "bulk-edit/apply", body([component.id], kind="component", technical_owner=str(a["people"]["priya"].id))
    )
    assert got.status_code == 200 and got.json_body["updated"] == 1
    assert api(a["users"]["admin"]).get(f"components/{component.id}").json_body["technical_owner"]["name"] == (
        "Priya Raman"
    )


def test_unauthenticated(api):
    assert api().post("bulk-edit/preview", {}).status_code in (401, 403)
    assert api().post("bulk-edit/apply", {}).status_code in (401, 403)
