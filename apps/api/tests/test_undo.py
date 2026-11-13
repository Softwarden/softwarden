"""Undo of an ownership change: refused when the field changed since."""

from core.models import AuditEvent


def test_undo_restores_the_previous_owner_when_unchanged(a, api):
    editor = api(a["users"]["editor"])
    app, dana, priya = a["app"].id, str(a["people"]["dana"].id), str(a["people"]["priya"].id)
    editor.patch(f"applications/{app}", {"technical_owner": dana})
    editor.patch(f"applications/{app}", {"technical_owner": priya})
    undone = editor.patch(
        f"applications/{app}", {"technical_owner": dana, "expected": {"technical_owner": priya}, "undo": True}
    )
    assert undone.status_code == 200 and undone.json_body["technical_owner"]["id"] == dana
    assert AuditEvent.objects.filter(action="resource.updated", changes__undo=True).count() == 1


def test_undo_is_refused_after_another_editor_changed_the_field(a, api):
    sam, lee = api(a["users"]["editor"]), api(a["users"]["admin"])
    app, dana, priya = a["app"].id, str(a["people"]["dana"].id), str(a["people"]["priya"].id)
    sam.patch(f"applications/{app}", {"technical_owner": dana})
    lee.patch(f"applications/{app}", {"technical_owner": priya})  # the second editor, before Sam's Undo
    refused = sam.patch(
        f"applications/{app}", {"technical_owner": None, "expected": {"technical_owner": dana}, "undo": True}
    )
    assert refused.status_code == 409 and refused.json_body["type"] == "/problems/stale-value"
    assert refused.json_body["detail"].startswith("Not undone: admin@orga.test changed Technical owner at ")
    assert sam.get(f"applications/{app}").json_body["technical_owner"]["id"] == priya  # the newer owner is kept
    actions = list(AuditEvent.objects.filter(target_id=app).order_by("created_at").values_list("action", flat=True))
    assert actions[-3:] == ["resource.updated", "resource.updated", "resource.undo_refused"]


def test_expected_accepts_only_ownership_fields(a, api):
    bad = api(a["users"]["editor"]).patch(f"applications/{a['app'].id}", {"name": "x", "expected": {"name": "y"}})
    assert bad.status_code == 400 and "expected" in bad.json_body["errors"]
