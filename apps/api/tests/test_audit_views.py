"""Record audit activity and the Admin audit log."""

from core.models import AuditEvent


def test_record_activity_shows_every_change_with_current_names(a, api):
    editor = api(a["users"]["editor"])
    app, dana = a["app"].id, a["people"]["dana"]
    editor.patch(f"applications/{app}", {"technical_owner": str(dana.id), "purpose": "Pays people", "name": "Payroll"})
    editor.post(f"applications/{app}/instances", {"name": "Production"})
    editor.post(f"applications/{app}/reviews", {"confirmed_by": str(dana.id), "evidence": "Email"})
    api(a["users"]["admin"]).post(f"people/{dana.id}/anonymize", {"confirm_name": "Dana Ortiz"})

    body = api(a["users"]["viewer"]).get(f"applications/{app}/audit").json_body  # any role
    labels = [e["label"] for e in body["results"]]
    assert labels == ["Review recorded", "Instance added", "Fields changed"]
    changed = body["results"][2]["summary"]
    assert "Technical owner: not set → Former person #1" in changed  # resolved through the Person ID
    assert "Name: orga App → Payroll" in changed and "Purpose changed" in changed
    assert body["results"][0]["summary"] == "Confirmed by Former person #1"
    assert body["results"][0]["actor"]["name"] == "editor@orga.test"
    assert body["next"] is None


def test_audit_log_is_admin_only_filtered_and_paged(a, api):
    admin = api(a["users"]["admin"])
    assert api(a["users"]["editor"]).get("audit-events").status_code == 403
    for i in range(55):
        AuditEvent.objects.create(
            organization=a["org"], actor=a["users"]["editor"], action="team.created", changes={"name": f"T{i}"}
        )
    first = admin.get("audit-events?action=team").json_body
    assert len(first["results"]) == 50 and first["next"]
    second = admin.get(f"audit-events?action=team&cursor={first['next']}").json_body
    assert len(second["results"]) == 5 and second["next"] is None
    assert {r["id"] for r in first["results"]}.isdisjoint(r["id"] for r in second["results"])
    assert admin.get(f"audit-events?actor={a['users']['admin'].id}&action=team.created").json_body["results"] == []
    assert admin.get("audit-events?from=2000-01-01&to=2000-01-02").json_body["results"] == []
    assert admin.get("audit-events?from=yesterday").status_code == 400


def test_sign_in_success_and_failure_are_audited_without_the_email(a, api):
    client = api()
    client.post("auth/login", {"email": "editor@orga.test", "password": "wrong-password-here"})
    client.post("auth/login", {"email": "nobody@orga.test", "password": "wrong-password-here"})
    client.post("auth/login", {"email": "editor@orga.test", "password": "correct-horse-battery"})
    events = AuditEvent.objects.filter(action__startswith="user.login").order_by("created_at")
    assert [e.action for e in events] == ["user.login_failed", "user.login"]
    assert all(e.changes == {"user": a["users"]["editor"].id} for e in events)
    log = api(a["users"]["admin"]).get("audit-events?action=user.login_failed").json_body["results"]
    assert log[0]["label"] == "Sign-in failed" and log[0]["summary"] == "editor@orga.test"


def test_other_org_record_activity_is_404(a, b, api):
    assert api(a["users"]["admin"]).get(f"applications/{b['app'].id}/audit").status_code == 404


def test_integration_activity_shows_edits_confirmations_and_implementers(a, b, api):
    from tests.test_integrations import flow, implement, instance, record

    app = a["app"]
    prod = instance(a, app)
    integration = flow(
        a, "Payroll feed", source=app, target=record(a, "Job", "component"), source_instance=str(prod.id)
    )
    editor, dana = api(a["users"]["editor"]), a["people"]["dana"]
    editor.patch(f"integrations/{integration.id}", {"protocol": "SFTP", "source_instance": None})
    editor.post(f"integrations/{integration.id}/confirmations", {"confirmed_by": str(dana.id), "evidence": "Email"})
    implement(a, record(a, "Sync", "component"), integration)

    body = api(a["users"]["viewer"]).get(f"integrations/{integration.id}/audit").json_body  # any role
    assert [e["label"] for e in body["results"]] == [
        "Relationship added",
        "Link confirmed",
        "Fields changed",
        "Record created",
    ]
    assert body["results"][0]["summary"] == "implements: Sync → Payroll feed"
    assert body["results"][1]["summary"].startswith("Confirmed by Dana Ortiz on ")
    changed = body["results"][2]["summary"]
    assert "Protocol: " in changed and "Source instance: Production → not set" in changed
    assert body["results"][3]["target"] == {"kind": "integration", "id": str(integration.id), "name": "Payroll feed"}
    # Another organization's integration, and an application's ID, are 404.
    other = flow(b, "Theirs")
    assert api(a["users"]["admin"]).get(f"integrations/{other.id}/audit").status_code == 404
    assert api(a["users"]["admin"]).get(f"integrations/{app.id}/audit").status_code == 404


# ---- Dependency target types: relationships, link confirmations, assessments, the retirement acknowledgment --------------


def link_fixture(ctx):
    from tests.test_dependencies_api import depends, record

    portal, sync, hr = record(ctx, "Portal"), record(ctx, "Sync", "component"), record(ctx, "Workday")
    return portal, sync, hr, depends(ctx, portal, sync, purpose="Reads the nightly file")


def test_link_events_show_on_both_ends_with_readable_names(a, api):
    portal, sync, hr, link = link_fixture(a)
    editor, dana = api(a["users"]["editor"]), a["people"]["dana"]
    editor.post(f"relationships/{link.id}/confirmations", {"confirmed_by": str(dana.id), "evidence": "Email"})
    editor.patch(f"relationships/{link.id}", {"purpose": "Reads the file", "target": str(hr.id)})  # re-pointed

    viewer = api(a["users"]["viewer"])
    source = viewer.get(f"applications/{portal.id}/audit").json_body["results"]
    assert [e["label"] for e in source] == [
        "Relationship changed",
        "Link confirmed",
        "Relationship added",
        "Record created",
    ]
    assert source[0]["summary"].startswith("Portal depends on Workday. ")
    assert "Target: Sync → Workday" in source[0]["summary"] and "Purpose changed" in source[0]["summary"]
    assert source[1]["summary"].startswith("Confirmed by Dana Ortiz on ") and source[1]["summary"].endswith(
        ": Portal depends on Workday"
    )
    assert source[2]["summary"] == "depends on: Portal → Sync"
    # The old target saw the link added and confirmed before it moved; the new one sees it as it is now.
    old = [e["label"] for e in viewer.get(f"components/{sync.id}/audit").json_body["results"]]
    assert old == ["Relationship changed", "Link confirmed", "Relationship added", "Record created"]
    new = [e["label"] for e in viewer.get(f"applications/{hr.id}/audit").json_body["results"]]
    assert new == [
        "Relationship changed",
        "Link confirmed",
        "Relationship added",
        "Record created",
    ]  # the link's history

    # Removing the link keeps its events findable, still in words.
    editor.delete(f"relationships/{link.id}")
    gone = viewer.get(f"applications/{portal.id}/audit").json_body["results"]
    assert gone[0]["label"] == "Relationship removed" and gone[0]["summary"] == "depends on: Portal → Workday"
    assert any(e["label"] == "Link confirmed" and e["summary"].endswith(": Portal depends on Sync") for e in gone)


def test_admin_log_names_relationship_and_assessment_targets(a, b, api):
    portal, sync, hr, link = link_fixture(a)
    editor, admin, dana = api(a["users"]["editor"]), api(a["users"]["admin"]), a["people"]["dana"]
    editor.post(f"relationships/{link.id}/confirmations", {"confirmed_by": str(dana.id), "evidence": "Email"})
    made = editor.post(
        "assessments", {"target": str(sync.id), "change_type": "retirement", "title": "Retire Sync"}
    ).json_body
    editor.patch(f"assessments/{made['id']}", {"title": "Retire Sync job", "owner": a["users"]["admin"].id})
    editor.post(f"assessments/{made['id']}/status", {"status": "in_review"})
    from tests.test_dependencies_api import depends

    depends(a, hr, portal)  # adds a dependent of Sync's dependent: the snapshot is flagged stale
    editor.post(f"assessments/{made['id']}/refresh")

    log = admin.get("audit-events").json_body["results"]
    by_action = {e["action"]: e for e in log}  # newest first, so the oldest of each wins
    rel = by_action["relationship.created"]
    assert rel["target"]["kind"] == "relationship" and rel["target"]["name"] == "Portal depends on Sync"
    assert rel["target"]["record"] == {"kind": "application", "id": str(portal.id)}
    assert by_action["link.confirmed"]["target"]["name"] == "Portal depends on Sync"
    created = by_action["assessment.created"]
    assert created["target"] == {"kind": "assessment", "id": made["id"], "name": "Retire Sync job"}  # current title
    assert created["summary"] == "Retirement of Sync, draft"
    changed = by_action["assessment.updated"]["summary"].split("; ")  # the stored JSON reorders its keys
    assert sorted(changed) == ["Owner: editor@orga.test → admin@orga.test", "Title: Retire Sync → Retire Sync job"]
    assert by_action["assessment.status_changed"]["summary"] == "Status: draft → in review"
    stale = by_action["assessment.stale"]
    assert stale["label"] == "Snapshot flagged stale" and "A depends_on link was added" in stale["summary"]
    refreshed = by_action["assessment.snapshot_refreshed"]
    assert refreshed["summary"] == "New snapshot, 2 affected. It was flagged stale"
    assert admin.get("audit-events?action=assessment").json_body["results"][0]["target"]["name"] == "Retire Sync job"
    assert admin.get("audit-events?action=relationship").json_body["results"]
    # Another organization's events never show up here.
    from tests.test_dependencies_api import depends as other_depends
    from tests.test_dependencies_api import record as other_record

    other_depends(b, other_record(b, "Theirs"), other_record(b, "Theirs 2"))
    names = {e["target"]["name"] for e in admin.get("audit-events").json_body["results"] if e["target"]}
    assert not any("Theirs" in n for n in names)


def test_retirement_acknowledgment_reason_is_in_the_updated_summary(a, api):
    from tests.test_dependencies_api import depends, record

    core, mid = record(a, "Core"), record(a, "Mid")
    depends(a, mid, core)
    editor = api(a["users"]["editor"])
    body = {"lifecycle": "retired", "retirement_ack": True, "retirement_ack_reason": "Replaced by Core 2"}
    assert editor.patch(f"applications/{core.id}", body).status_code == 200
    summary = api(a["users"]["viewer"]).get(f"applications/{core.id}/audit").json_body["results"][0]["summary"]
    assert summary == (
        "Lifecycle: active → retired; Retirement acknowledged for 1 active dependent (Mid). Reason: Replaced by Core 2"
    )
