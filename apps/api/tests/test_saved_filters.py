"""Saved filters. Saved views (owner, sharing, columns, edits) are in
tests/test_saved_views.py."""

import uuid

import pytest

from catalog import services
from catalog.models import SavedFilter
from core.models import AuditEvent
from imports.models import ImportRun

ROLES = ["admin", "editor", "viewer"]
BUILTIN_IDS = ["builtin:missing-owners", "builtin:overdue-reviews", "builtin:unreviewed-imports", "builtin:active"]


def save(client, name="Mine", kind="application", query=None, shared=False):
    return client.post(
        "saved-filters", {"name": name, "kind": kind, "query": query or {"lifecycle": "active"}, "shared": shared}
    )


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("kind", ["application", "component"])
def test_every_role_sees_the_four_built_ins(a, api, role, kind):
    got = api(a["users"][role]).get(f"saved-filters?kind={kind}")
    assert got.status_code == 200
    assert [f["id"] for f in got.json_body] == BUILTIN_IDS
    assert all(f["builtin"] and f["shared"] and not f["can_delete"] and f["kind"] == kind for f in got.json_body)


def test_kind_is_required(a, api):
    assert api(a["users"]["viewer"]).get("saved-filters").status_code == 400
    assert api(a["users"]["viewer"]).get("saved-filters?kind=integration").status_code == 400


def test_built_in_queries_are_valid_list_queries_and_find_the_right_records(a, api):
    editor = a["users"]["editor"]
    client = api(editor)
    run = ImportRun.objects.create(
        organization=a["org"], kind="application", filename="x.csv", purge_after="2027-01-01"
    )
    imported = services.create_resource(a["org"], editor, {"name": "Imported", "lifecycle": "active"})
    imported.created_by_import = run
    imported.save()
    queries = {f["id"]: f["query"] for f in client.get("saved-filters?kind=application").json_body}
    for query in queries.values():
        qs = "&".join(f"{k}={v}" for k, v in query.items())
        assert client.get(f"applications?{qs}").status_code == 200

    def found(builtin):
        qs = "&".join(f"{k}={v}" for k, v in queries[builtin].items())
        return {r["name"] for r in client.get(f"applications?{qs}").json_body["results"]}

    assert found("builtin:unreviewed-imports") == {"Imported"}
    assert found("builtin:missing-owners") == {"orga App", "Imported"}
    assert found("builtin:overdue-reviews") == set()
    assert found("builtin:active") == {"orga App", "Imported"}
    a["app"].lifecycle = "retired"
    a["app"].save()
    assert found("builtin:active") == {"Imported"}


def test_personal_filter_is_private_shared_is_visible(a, api):
    editor, viewer = api(a["users"]["editor"]), api(a["users"]["viewer"])
    mine = save(editor, "Editor private")
    shared = save(editor, "Team view", query={"gap": "true"}, shared=True)
    assert mine.status_code == 201 and shared.status_code == 201
    assert mine.json_body["shared"] is False and mine.json_body["mine"] is True and mine.json_body["can_delete"]
    assert shared.json_body["shared"] is True
    seen_by_viewer = [f["name"] for f in viewer.get("saved-filters?kind=application").json_body if not f["builtin"]]
    assert seen_by_viewer == ["Team view"]
    seen_by_editor = [f["name"] for f in editor.get("saved-filters?kind=application").json_body if not f["builtin"]]
    assert seen_by_editor == ["Team view", "Editor private"]
    assert [f for f in editor.get("saved-filters?kind=component").json_body if not f["builtin"]] == []
    assert SavedFilter.objects.get(name="Editor private").user == a["users"]["editor"]
    team_view = SavedFilter.objects.get(name="Team view")
    assert team_view.user == a["users"]["editor"] and team_view.shared  # shared views keep their owner


def test_viewer_can_save_personal_not_shared(a, api):
    viewer = api(a["users"]["viewer"])
    assert save(viewer, "Viewer own").status_code == 201
    refused = save(viewer, "Viewer shared", shared=True)
    assert refused.status_code == 403
    assert not SavedFilter.objects.filter(name="Viewer shared").exists()


def test_query_is_validated_like_the_list(a, api):
    editor = api(a["users"]["editor"])
    assert save(editor, "Bad", query={"nonsense": "1"}).status_code == 400
    assert save(editor, "Bad", query={"lifecycle": "bogus"}).status_code == 400
    assert save(editor, "Bad", query={"vendor": "x"}, kind="component").status_code == 400
    assert save(editor, "Bad", query={"gap": True}).status_code == 400
    assert save(editor, "Bad", kind="integration").status_code == 400
    assert save(editor, "  ").status_code == 400
    assert save(editor, "x" * 101).status_code == 400
    ok = save(editor, "Paged", query={"q": "ledger", "cursor": "abc", "page_size": "10", "tag": "Finance"})
    assert ok.status_code == 201 and ok.json_body["query"] == {"q": "ledger", "tag": "Finance"}


def test_names_must_be_unique_where_visible(a, api):
    editor = api(a["users"]["editor"])
    assert save(editor, "Missing OWNERS").status_code == 400  # a built-in's name
    assert save(editor, "Same").status_code == 201
    assert save(editor, "same").status_code == 400
    assert save(editor, "Same", kind="component").status_code == 201


def test_delete_rules(a, api):
    admin, editor, viewer = (api(a["users"][r]) for r in ROLES)
    other_personal = save(editor, "Editor private").json_body["id"]
    shared = save(editor, "Shared", shared=True).json_body["id"]
    own = save(viewer, "Viewer own").json_body["id"]

    assert viewer.delete(f"saved-filters/{shared}").status_code == 403  # shared: its owner or an Admin
    assert admin.delete(f"saved-filters/{other_personal}").status_code == 404  # someone else's personal
    assert viewer.delete(f"saved-filters/{other_personal}").status_code == 404
    assert editor.delete("saved-filters/builtin:active").status_code == 400
    assert editor.delete("saved-filters/not-an-id").status_code == 404
    assert editor.delete(f"saved-filters/{uuid.uuid4()}").status_code == 404

    assert viewer.delete(f"saved-filters/{own}").status_code == 204
    assert admin.delete(f"saved-filters/{shared}").status_code == 204
    assert editor.delete(f"saved-filters/{other_personal}").status_code == 204
    assert not SavedFilter.objects.exists()


def test_audit_events_carry_no_results(a, api):
    editor = api(a["users"]["editor"])
    fid = save(editor, "Audited", shared=True, query={"q": "ledger"}).json_body["id"]
    editor.delete(f"saved-filters/{fid}")
    created = AuditEvent.objects.get(action="saved_filter.created")
    assert created.changes == {
        "name": "Audited",
        "kind": "application",
        "shared": True,
        "query": {"q": "ledger"},
        "columns": [],
    }
    assert created.target_id == uuid.UUID(fid) and created.actor == a["users"]["editor"]
    deleted = AuditEvent.objects.get(action="saved_filter.deleted")
    assert deleted.changes["name"] == "Audited" and deleted.target_id == uuid.UUID(fid)


def test_cross_tenant(a, b, api):
    shared_b = save(api(b["users"]["editor"]), "B shared", shared=True).json_body["id"]
    save(api(b["users"]["editor"]), "B personal")
    names = [f["name"] for f in api(a["users"]["admin"]).get("saved-filters?kind=application").json_body]
    assert "B shared" not in names and "B personal" not in names
    assert api(a["users"]["admin"]).delete(f"saved-filters/{shared_b}").status_code == 404
    assert SavedFilter.objects.filter(id=shared_b).exists()
    # the same name is free in another organization
    assert save(api(a["users"]["editor"]), "B shared", shared=True).status_code == 201


def test_unauthenticated(api):
    assert api().get("saved-filters?kind=application").status_code in (401, 403)
    assert api().post("saved-filters", {}).status_code in (401, 403)
