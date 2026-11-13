"""Change assessments: create, edit, status, refresh, history, filters, scope."""

import json

from catalog import services
from catalog.models import AssessmentSnapshot, ChangeAssessment, Relationship, Resource, SnapshotNode
from core.models import AuditEvent


def record(ctx, name, kind="application", lifecycle="active", **extra):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": lifecycle, **extra}
    )


def depends(ctx, source, target):
    """source depends on target."""
    return services.create_relationship(
        ctx["org"],
        ctx["users"]["editor"],
        {"type": "depends_on", "source": str(source.id), "target": str(target.id)},
    )


def flow(ctx, name, source, target):
    return services.create_integration(
        ctx["org"],
        ctx["users"]["editor"],
        {"name": name, "lifecycle": "active", "source": str(source.id), "target": str(target.id)},
    )


def start(client, target, /, **extra):
    """Create an assessment of target; extra overrides any body field (including target)."""
    body = {"target": str(target.id), "change_type": "change", "title": "Move to the new host", **extra}
    return client.post("assessments", body)


def chain(ctx):
    """Core <- Mid <- Top, with Mid a component."""
    core, mid, top = record(ctx, "Core"), record(ctx, "Mid", "component"), record(ctx, "Top")
    depends(ctx, mid, core)
    depends(ctx, top, mid)
    return core, mid, top


# ---- create ---------------------------------------------------------------------------------------------------


def test_create_computes_the_first_snapshot_and_audits(a, api):
    core, mid, top = chain(a)
    out = flow(a, "Core to Ledger", core, record(a, "Ledger"))
    client = api(a["users"]["editor"])
    response = start(
        client, core, description="Host move", findings="Check Top", ticket_url="https://tickets.example.test/CHG-1"
    )
    assert response.status_code == 201, response.json_body
    assert "nodes" not in response.json_body["snapshot"] and "history" not in response.json_body  # rows come from GET
    body = client.get(f"assessments/{response.json_body['id']}").json_body
    assert body["status"] == "draft" and body["owner"]["id"] == a["users"]["editor"].id  # defaults
    assert body["target"]["name"] == "Core" and body["change_type"] == "change" and not body["stale"]
    snap = body["snapshot"]
    assert snap["is_current"] and snap["taken_by"]["id"] == a["users"]["editor"].id and not snap["stale"]
    assert snap["counts"] == {"dependents": 2, "cycles": 0, "data_flows": 1, "implementers": 0, "links": 3}
    nodes = {n["resource"]["name"]: n for n in snap["nodes"]}
    assert [s["name"] for s in nodes["Top"]["path"]] == ["Core", "Mid", "Top"] and nodes["Top"]["depth"] == 2
    assert nodes["Core to Ledger"]["role"] == "data_flow"
    assert nodes["Mid"]["lifecycle_then"] == nodes["Mid"]["lifecycle_now"] == "active"
    assert nodes["Mid"]["changed"] == {"lifecycle": False, "owners": False}
    assert str(out.id) in {link["id"] for link in snap["links"]}
    assert body["history"] == [{k: v for k, v in snap.items() if k not in ("nodes", "links")}]
    event = AuditEvent.objects.get(action="assessment.created")
    assert event.target_id == ChangeAssessment.objects.get().id and event.changes["nodes"] == 3


def test_create_checks_its_fields(a, b, api):
    client = api(a["users"]["editor"])
    assert client.post("assessments", {}).json_body["errors"].keys() >= {"target", "change_type", "title"}
    cases = [
        ({"target": str(b["app"].id)}, "target", "Choose an existing record."),  # another organization's record
        ({"target": "nope"}, "target", "Choose an existing record."),
        ({"change_type": "sideways"}, "change_type", "Choose change or retirement."),
        ({"title": "  "}, "title", "Enter a title."),
        ({"title": "x" * 201}, "title", "Use 200 characters or fewer."),
        ({"status": "approved"}, "status", "Choose draft, in review, ready, closed or canceled."),
        ({"owner": a["users"]["viewer"].id}, "owner", "Choose an Admin or Editor in this organization."),
        ({"owner": b["users"]["editor"].id}, "owner", "Choose an Admin or Editor in this organization."),
        ({"target_date": "soon"}, "target_date", "Enter a date as YYYY-MM-DD."),
        (
            {"ticket_url": "javascript:alert(1)"},
            "ticket_url",
            "Enter a full web address starting with http:// or https://.",
        ),
        ({"description": "d" * 5001}, "description", "Use 5000 characters or fewer."),
    ]
    for extra, field, message in cases:
        response = start(client, a["app"], **extra)
        assert response.status_code == 400, extra
        assert response["Content-Type"] == "application/problem+json"
        assert response.json_body["errors"][field] == [message], extra
    assert ChangeAssessment.objects.count() == 0 and AssessmentSnapshot.objects.count() == 0


def test_an_integration_is_not_a_target(a, api):
    feed = flow(a, "Feed", a["app"], record(a, "Job", "component"))
    response = start(api(a["users"]["editor"]), feed)
    assert response.status_code == 400 and response.json_body["errors"]["target"] == [
        "Choose an application, component or AI agent."
    ]


def test_no_dependents_still_saves_an_empty_snapshot(a, api):
    client = api(a["users"]["editor"])
    body = client.get(f"assessments/{start(client, a['app']).json_body['id']}").json_body
    assert body["snapshot"]["nodes"] == [] and body["snapshot"]["counts"]["dependents"] == 0


# ---- edit and status ------------------------------------------------------------------------------------------


def test_edit_changes_text_and_audits_before_and_after_without_touching_the_snapshot(a, api):
    client = api(a["users"]["editor"])
    made = start(client, a["app"]).json_body
    response = client.patch(
        f"assessments/{made['id']}",
        {
            "title": "Move the host",
            "findings": "Vendor confirmed",
            "target_date": "2026-12-15",
            "owner": a["users"]["admin"].id,
        },
    )
    assert response.status_code == 200, response.json_body
    assert response.json_body["title"] == "Move the host" and response.json_body["target_date"] == "2026-12-15"
    assert response.json_body["owner"]["id"] == a["users"]["admin"].id
    assert response.json_body["snapshot"]["id"] == made["snapshot"]["id"]
    event = AuditEvent.objects.get(action="assessment.updated")
    assert event.changes["title"] == ["Move to the new host", "Move the host"]
    assert event.changes["findings"] == ["", "Vendor confirmed"]
    cleared = client.patch(f"assessments/{made['id']}", {"owner": None, "target_date": None})
    assert cleared.json_body["owner"] is None and cleared.json_body["target_date"] is None


def test_target_type_and_status_are_not_edited_in_place(a, api):
    client = api(a["users"]["editor"])
    made = start(client, a["app"]).json_body
    other = record(a, "Other")
    response = client.patch(
        f"assessments/{made['id']}", {"target": str(other.id), "change_type": "retirement", "status": "closed"}
    )
    assert response.status_code == 400
    assert response.json_body["errors"].keys() == {"target", "change_type", "status"}
    assert ChangeAssessment.objects.get().status == "draft"


def test_status_moves_through_d37_and_is_audited(a, api):
    client = api(a["users"]["editor"])
    made = start(client, a["app"]).json_body
    for status in ("in_review", "ready", "closed", "cancelled", "draft"):
        response = client.post(f"assessments/{made['id']}/status", {"status": status})
        assert response.status_code == 200 and response.json_body["status"] == status
    moves = [
        e.changes["status"]
        for e in AuditEvent.objects.filter(action="assessment.status_changed").order_by("created_at")
    ]
    assert moves == [
        ["draft", "in_review"],
        ["in_review", "ready"],
        ["ready", "closed"],
        ["closed", "cancelled"],
        ["cancelled", "draft"],
    ]
    again = client.post(f"assessments/{made['id']}/status", {"status": "draft"})  # no change: no new event
    assert again.status_code == 200 and AuditEvent.objects.filter(action="assessment.status_changed").count() == 5
    bad = client.post(f"assessments/{made['id']}/status", {"status": "approved"})
    assert bad.status_code == 400 and "status" in bad.json_body["errors"]


# ---- refresh and history ----------------------------------------------------------------------------------------


def test_refresh_adds_a_current_snapshot_and_keeps_the_old_one_readable(a, api):
    core, mid, top = chain(a)
    client = api(a["users"]["editor"])
    made = start(client, core).json_body
    first = made["snapshot"]["id"]
    depends(a, record(a, "Late"), core)
    refreshed = client.post(f"assessments/{made['id']}/refresh")
    assert refreshed.status_code == 201, refreshed.json_body
    assert refreshed.json_body["snapshot"]["id"] != first
    body = client.get(f"assessments/{made['id']}").json_body
    assert body["snapshot"]["counts"]["dependents"] == 3
    assert [h["id"] for h in body["history"]][1] == first and [h["is_current"] for h in body["history"]] == [
        True,
        False,
    ]
    old = client.get(f"assessments/{made['id']}/snapshots/{first}")
    assert old.status_code == 200 and old.json_body["counts"]["dependents"] == 2 and not old.json_body["is_current"]
    assert len(client.get(f"assessments/{made['id']}/snapshots").json_body) == 2
    event = AuditEvent.objects.get(action="assessment.snapshot_refreshed")
    assert event.changes["previous"] == first and event.changes["was_stale"] is True  # Late flagged it


def test_an_archived_node_stays_in_the_snapshot_and_its_record_cannot_be_deleted(a, api):
    core, mid, top = chain(a)
    client = api(a["users"]["admin"])
    made = start(client, core).json_body
    services.archive_resource(a["org"], a["users"]["editor"], mid)
    got = client.get(f"assessments/{made['id']}").json_body["snapshot"]["nodes"]
    assert {n["resource"]["name"]: n["resource"]["archived"] for n in got} == {"Mid": True, "Top": False}
    assert client.delete(f"components/{mid.id}").status_code == 409


def test_then_beside_now_for_a_changed_node(a, api):
    core, mid, top = chain(a)
    client = api(a["users"]["editor"])
    made = start(client, core).json_body
    services.update_resource(
        a["org"], a["users"]["editor"], top, {"lifecycle": "deprecated", "technical_owner": str(a["people"]["dana"].id)}
    )
    node = next(
        n
        for n in client.get(f"assessments/{made['id']}").json_body["snapshot"]["nodes"]
        if n["resource"]["name"] == "Top"
    )
    assert node["lifecycle_then"] == "active" and node["lifecycle_now"] == "deprecated"
    assert (
        node["owners_then"]["technical_owner"] is None and node["owners_now"]["technical_owner"]["name"] == "Dana Ortiz"
    )
    assert node["changed"] == {"lifecycle": True, "owners": True}


def test_a_removed_link_stays_in_the_snapshot_marked_removed(a, api):
    core, mid, top = chain(a)
    client = api(a["users"]["editor"])
    made = start(client, core).json_body
    before = client.get(f"assessments/{made['id']}").json_body["snapshot"]["links"]
    rel = next(link for link in before if link["source"]["name"] == "Mid")
    services.delete_relationship(a["org"], a["users"]["editor"], Relationship.objects.get(id=rel["id"]))
    links = client.get(f"assessments/{made['id']}").json_body["snapshot"]["links"]
    gone = next(link for link in links if link["source"]["name"] == "Mid")
    assert gone["removed"] and gone["changed"] and gone["id"] is None and gone["type"] == "depends_on"
    assert not next(link for link in links if link["source"]["name"] == "Top")["changed"]


def test_each_snapshot_link_carries_its_link_state_now(a, api):
    core, mid, top = chain(a)
    feed = flow(a, "Core feed", core, record(a, "Ledger"))
    client, dana = api(a["users"]["editor"]), a["people"]["dana"]
    made = start(client, core).json_body

    def links():
        got = client.get(f"assessments/{made['id']}").json_body["snapshot"]["links"]
        return {(link["kind"], link["source"]["name"]): link for link in got}

    before = links()
    assert before["relationship", "Mid"]["link_state"] == "unconfirmed"
    assert before["relationship", "Mid"]["link_why"] == "Never confirmed."
    assert before["relationship", "Mid"]["last_confirmed_on"] is None
    # Confirmations made after the snapshot are read live; the link itself is unchanged.
    client.post(
        f"relationships/{Relationship.objects.get(source=mid).id}/confirmations",
        {"confirmed_by": str(dana.id), "evidence": "Email"},
    )
    client.post(f"integrations/{feed.id}/confirmations", {"confirmed_by": str(dana.id), "evidence": "Email"})
    confirmed = links()
    assert (
        confirmed["relationship", "Mid"]["link_state"] == "confirmed"
        and not confirmed["relationship", "Mid"]["changed"]
    )
    assert confirmed["relationship", "Mid"]["last_confirmed_on"]
    assert confirmed["integration", "Core"]["link_state"] == "confirmed"
    assert confirmed["relationship", "Top"]["link_state"] == "unconfirmed"
    # The source's owner changes: the link needs verification now, and is also changed since the snapshot.
    services.update_resource(a["org"], a["users"]["editor"], mid, {"technical_owner": str(dana.id)})
    changed = links()["relationship", "Mid"]
    assert changed["link_state"] == "needs_verification" and changed["link_why"] == "Owner changed since confirmation."
    assert changed["changed"]


def test_a_removed_snapshot_link_has_no_link_state(a, api):
    core, mid, top = chain(a)
    client = api(a["users"]["editor"])
    made = start(client, core).json_body
    link = Relationship.objects.get(source=mid)
    services.delete_relationship(a["org"], a["users"]["editor"], link)
    got = client.get(f"assessments/{made['id']}").json_body["snapshot"]["links"]
    gone = next(x for x in got if x["source"]["name"] == "Mid")
    assert gone["link_state"] is None and gone["link_why"] is None and gone["last_confirmed_on"] is None


# ---- list and filters ---------------------------------------------------------------------------------------------


def test_list_filters_by_status_stale_type_owner_target_and_search(a, api):
    core, mid, top = chain(a)
    client = api(a["users"]["viewer"])
    editor = api(a["users"]["editor"])
    one = start(editor, core, title="Retire Core", change_type="retirement").json_body
    two = start(editor, top, title="Patch Top", owner=a["users"]["admin"].id).json_body
    editor.post(f"assessments/{two['id']}/status", {"status": "in_review"})
    AssessmentSnapshot.objects.filter(assessment_id=one["id"]).update(stale_since="2026-11-01T10:00:00Z")

    def ids(query):
        response = client.get(f"assessments{query}")
        assert response.status_code == 200, response.json_body
        return {r["id"] for r in response.json_body}

    assert ids("") == {one["id"], two["id"]}
    assert ids("?status=in_review") == {two["id"]}
    assert ids("?status=draft,in_review") == {one["id"], two["id"]}
    assert ids("?stale=true") == {one["id"]} and ids("?stale=false") == {two["id"]}
    assert ids("?change_type=retirement") == {one["id"]}
    assert ids(f"?owner={a['users']['admin'].id}") == {two["id"]}
    assert ids(f"?target={top.id}") == {two["id"]}
    assert ids("?q=patch") == {two["id"]} and ids("?q=core") == {one["id"]}  # title or target name
    row = next(r for r in client.get("assessments").json_body if r["id"] == one["id"])
    assert row["stale"] and row["snapshot"]["stale"] and "nodes" not in row["snapshot"]
    for bad in ("?status=nope", "?stale=maybe", "?change_type=x", "?owner=x", "?target=x"):
        assert client.get(f"assessments{bad}").status_code == 400, bad


# ---- other organizations --------------------------------------------------------------------------------------------


def test_another_organizations_assessments_are_404_and_never_listed(a, b, api):
    # Both organizations have the same shape: a chain, so a leak would show up as an extra node.
    for ctx in (a, b):
        core, mid, top = chain(ctx)
        ctx["made"] = start(api(ctx["users"]["editor"]), core).json_body
    mine = api(a["users"]["admin"])
    theirs = b["made"]
    assert [r["id"] for r in mine.get("assessments").json_body] == [a["made"]["id"]]
    assert mine.get(f"assessments/{a['made']['id']}").json_body["snapshot"]["counts"]["dependents"] == 2
    ours = {str(r) for r in Resource.objects.filter(organization=a["org"]).values_list("id", flat=True)}
    got = mine.get(f"assessments/{a['made']['id']}").json_body["snapshot"]
    assert {n["resource"]["id"] for n in got["nodes"]} <= ours
    assert {s["id"] for n in got["nodes"] for s in n["path"]} <= ours
    snapshot_id = theirs["snapshot"]["id"]
    for method, url, payload in (
        ("get", f"assessments/{theirs['id']}", None),
        ("patch", f"assessments/{theirs['id']}", {"title": "Mine now"}),
        ("post", f"assessments/{theirs['id']}/status", {"status": "closed"}),
        ("post", f"assessments/{theirs['id']}/refresh", None),
        ("get", f"assessments/{theirs['id']}/snapshots", None),
        ("get", f"assessments/{theirs['id']}/snapshots/{snapshot_id}", None),
        ("get", f"assessments/{a['made']['id']}/snapshots/{snapshot_id}", None),  # their snapshot, my assessment
        ("get", f"assessments?target={b['app'].id}", None),
        ("post", "assessments", {"target": str(b["app"].id), "change_type": "change", "title": "x"}),
    ):
        response = getattr(mine, method)(url, payload) if payload is not None else getattr(mine, method)(url)
        expected = 400 if method == "post" and url == "assessments" else 404
        assert response.status_code == expected, (method, url, response.status_code)
    assert ChangeAssessment.objects.get(id=theirs["id"]).title == "Move to the new host"
    assert SnapshotNode.objects.filter(snapshot_id=snapshot_id).count() == 2


def test_snapshot_rows_of_one_assessment_stay_out_of_another(a, api):
    core, mid, top = chain(a)
    editor = api(a["users"]["editor"])
    first = start(editor, core).json_body
    second = start(editor, top).json_body
    assert second["snapshot"]["counts"]["dependents"] == 0
    assert editor.get(f"assessments/{second['id']}/snapshots/{first['snapshot']['id']}").status_code == 404


def test_the_schema_is_valid_json(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    assert "/api/v1/assessments" in doc["paths"]
