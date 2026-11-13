"""Snapshot staleness: what flags a snapshot, what doesn't, and that nothing
refreshes by itself."""

from catalog import assessments, services
from catalog.models import AssessmentSnapshot, ChangeAssessment, Relationship, SnapshotNode
from core.models import AuditEvent


def record(ctx, name, kind="application", lifecycle="active", **extra):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": lifecycle, **extra}
    )


def depends(ctx, source, target):
    """source depends on target."""
    return services.create_relationship(
        ctx["org"], ctx["users"]["editor"], {"type": "depends_on", "source": str(source.id), "target": str(target.id)}
    )


def flow(ctx, name, source, target):
    return services.create_integration(
        ctx["org"],
        ctx["users"]["editor"],
        {"name": name, "lifecycle": "active", "source": str(source.id), "target": str(target.id)},
    )


def implements(ctx, component, integration):
    return services.create_relationship(
        ctx["org"],
        ctx["users"]["editor"],
        {"type": "implements", "source": str(component.id), "target": str(integration.id)},
    )


def edit(ctx, resource, **data):
    return services.update_resource(ctx["org"], ctx["users"]["editor"], resource, data)


def assess(ctx, target, **extra):
    return assessments.create_assessment(
        ctx["org"], ctx["users"]["editor"], {"target": str(target.id), "change_type": "change", "title": "T", **extra}
    )


def current(assessment):
    return AssessmentSnapshot.objects.get(assessment=assessment, is_current=True)


def scenario(ctx):
    """Core <- Mid (component) <- Top; Mid also has a data flow to Ledger, implemented by Export (a component)."""
    core, mid, top = record(ctx, "Core"), record(ctx, "Mid", "component"), record(ctx, "Top")
    depends(ctx, mid, core)
    depends(ctx, top, mid)
    ledger, export = record(ctx, "Ledger"), record(ctx, "Export", "component")
    feed = flow(ctx, "Mid to Ledger", mid, ledger)
    implements(ctx, export, feed)
    return {"core": core, "mid": mid, "top": top, "ledger": ledger, "export": export, "feed": feed}


# ---- components and the integrations they implement are in the assessment -------------------------------


def test_a_component_in_the_path_and_the_integration_it_implements_are_in_the_snapshot(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    got = {(n.resource.name, n.role) for n in SnapshotNode.objects.filter(snapshot=current(saved))}
    assert got == {
        ("Mid", "dependent"),
        ("Top", "dependent"),
        ("Mid to Ledger", "data_flow"),
        ("Export", "implementer"),
    }


# ---- flags -------------------------------------------------------------------------------------------------------


def test_a_depends_on_added_to_a_node_flags_with_a_readable_reason(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    assert current(saved).stale_since is None
    depends(a, record(a, "Newcomer"), s["top"])  # a new dependent of a snapshot node: a new path
    snap = current(saved)
    assert snap.stale_since is not None
    assert snap.stale_reasons == ["A depends_on link was added: Newcomer depends on Top."]


def test_a_depends_on_added_to_the_target_flags(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    depends(a, record(a, "Other"), s["core"])
    assert current(saved).stale_reasons == ["A depends_on link was added: Other depends on Core."]


def test_a_depends_on_between_unrelated_records_does_not_flag(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    depends(a, record(a, "Stranger"), record(a, "Island"))
    services.create_relationship(
        a["org"],
        a["users"]["editor"],
        {"type": "replaces", "source": str(record(a, "New Core").id), "target": str(s["core"].id)},
    )  # replaces is not a path
    assert current(saved).stale_since is None


def test_removing_a_link_on_the_paths_flags(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    link = Relationship.objects.get(source=s["top"], target=s["mid"])
    services.delete_relationship(a["org"], a["users"]["editor"], link)
    assert current(saved).stale_reasons == ["A depends_on link was removed: Top to Mid."]


def test_retyping_or_repointing_a_link_on_the_paths_flags(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    link = Relationship.objects.get(source=s["top"], target=s["mid"])
    other = record(a, "Other app")
    services.update_relationship(a["org"], a["users"]["editor"], link, {"source": str(other.id)})
    snap = current(saved)
    assert snap.stale_since is not None
    assert snap.stale_reasons[0] == "The depends_on link from Other app to Mid was changed (source)."


def test_retyping_between_same_kinds_flags(a):
    core, one, two = record(a, "Core"), record(a, "One"), record(a, "Two")
    link = depends(a, one, core)
    saved = assess(a, core)
    services.update_relationship(
        a["org"], a["users"]["editor"], link, {"type": "replaces", "source": str(one.id), "target": str(two.id)}
    )
    snap = current(saved)
    assert "The depends_on link from One to Two was changed (type, target)." in snap.stale_reasons


def test_a_node_lifecycle_change_flags(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    edit(a, s["top"], lifecycle="deprecated")
    assert current(saved).stale_reasons == ["Top lifecycle changed (Active to Deprecated)."]


def test_a_node_owner_change_flags_for_business_and_technical_owners(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    edit(a, s["top"], technical_owner=str(a["people"]["dana"].id))
    edit(a, s["mid"], business_owner=str(a["people"]["priya"].id))
    assert current(saved).stale_reasons == ["Top technical owner changed.", "Mid business owner changed."]


def test_changing_an_owner_back_still_flagged_but_an_unchanged_owner_is_not(a):
    dana = a["people"]["dana"]
    core, mid = record(a, "Core"), record(a, "Mid", technical_owner=str(dana.id))
    depends(a, mid, core)
    saved = assess(a, core)
    edit(a, mid, technical_owner=str(dana.id))  # same owner saved again
    assert current(saved).stale_since is None
    edit(a, mid, technical_owner=str(a["people"]["priya"].id))
    assert current(saved).stale_since is not None


def test_integration_and_implementer_changes_flag(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    edit(a, s["feed"], lifecycle="deprecated")  # the data flow is a node
    edit(a, s["export"], technical_owner=str(a["people"]["dana"].id))  # so is its implementer
    assert current(saved).stale_reasons == [
        "Mid to Ledger lifecycle changed (Active to Deprecated).",
        "Export technical owner changed.",
    ]


def test_a_new_implementer_a_new_data_flow_and_a_repointed_data_flow_flag(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    implements(a, record(a, "Second job", "component"), s["feed"])
    flow(a, "Top to Ledger", s["top"], s["ledger"])
    other = record(a, "Elsewhere")
    edit(a, s["feed"], source=str(other.id))
    assert current(saved).stale_reasons == [
        "Second job now implements the integration Mid to Ledger.",
        "A data flow was added: Top to Ledger.",
        "The data flow Mid to Ledger was changed (source).",
    ]


def test_a_data_flow_far_from_the_snapshot_does_not_flag(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    flow(a, "Elsewhere feed", record(a, "X"), record(a, "Y"))
    implements(a, record(a, "Their job", "component"), flow(a, "Another feed", record(a, "P"), record(a, "Q")))
    assert current(saved).stale_since is None


# ---- no flag -------------------------------------------------------------------------------------------------------


def test_unrelated_edits_do_not_flag(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    rel = Relationship.objects.get(source=s["top"], target=s["mid"])
    edit(a, s["top"], name="Top renamed", description="New words", purpose="New purpose", criticality="high")
    edit(a, s["mid"], support_team=str(a["team"].id), documentation_url="https://docs.example.test/mid")
    edit(a, s["feed"], protocol="SFTP", data_purpose="Nightly totals", name="Renamed feed")
    services.update_relationship(a["org"], a["users"]["editor"], rel, {"purpose": "Calls its API"})
    services.archive_resource(a["org"], a["users"]["editor"], s["top"])
    services.restore_resource(a["org"], a["users"]["editor"], s["top"])
    services.confirm_link(
        a["org"], a["users"]["editor"], rel, {"confirmed_by": str(a["people"]["dana"].id), "evidence": "x"}
    )
    services.record_review(
        a["org"], a["users"]["editor"], s["top"], {"confirmed_by": str(a["people"]["dana"].id), "evidence": "x"}
    )
    edit(a, s["core"], name="Core renamed")
    assert current(saved).stale_since is None and current(saved).stale_reasons == []


def test_the_targets_own_lifecycle_and_owners_do_not_flag(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    edit(a, s["core"], lifecycle="deprecated", technical_owner=str(a["people"]["dana"].id))
    assert current(saved).stale_since is None


def test_a_record_outside_the_snapshot_does_not_flag(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    edit(a, s["ledger"], lifecycle="retired")  # the far end of a data flow is not a node
    edit(a, record(a, "Stranger"), lifecycle="retired")
    assert current(saved).stale_since is None


def test_closed_and_cancelled_assessments_are_not_flagged(a):
    s = scenario(a)
    closed, cancelled, open_one = assess(a, s["core"]), assess(a, s["core"]), assess(a, s["core"])
    assessments.change_status(a["org"], a["users"]["editor"], closed, {"status": "closed"})
    assessments.change_status(a["org"], a["users"]["editor"], cancelled, {"status": "cancelled"})
    edit(a, s["top"], lifecycle="retired")
    assert current(closed).stale_since is None and current(cancelled).stale_since is None
    assert current(open_one).stale_since is not None


def test_another_organizations_changes_never_flag(a, b):
    s = scenario(a)
    saved = assess(a, s["core"])
    other = scenario(b)
    edit(b, other["top"], lifecycle="retired")
    depends(b, record(b, "Newcomer"), other["top"])
    assert current(saved).stale_since is None
    assert AssessmentSnapshot.objects.filter(stale_since__isnull=False).count() == 0


# ---- never auto-refreshes --------------------------------------------------------------------------------------------


def test_a_flagged_snapshot_is_never_replaced_and_keeps_its_first_time_and_adds_reasons(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    before = current(saved)
    edit(a, s["top"], lifecycle="deprecated")
    first = current(saved)
    edit(a, s["mid"], lifecycle="deprecated")
    edit(a, s["mid"], lifecycle="deprecated")  # the same reason twice is stored once
    snap = current(saved)
    assert snap.id == before.id and AssessmentSnapshot.objects.filter(assessment=saved).count() == 1
    assert snap.stale_since == first.stale_since and len(snap.stale_reasons) == 2
    assert SnapshotNode.objects.get(snapshot=snap, resource=s["top"]).lifecycle_then == "active"  # still as saved
    assert AuditEvent.objects.filter(action="assessment.stale", target_id=saved.id).count() == 1


def test_refresh_clears_the_flag_on_the_new_snapshot_and_keeps_the_old_one_stale(a):
    s = scenario(a)
    saved = assess(a, s["core"])
    old = current(saved)
    edit(a, s["top"], lifecycle="deprecated")
    assessments.refresh_snapshot(a["org"], a["users"]["editor"], ChangeAssessment.objects.get(id=saved.id))
    new = current(saved)
    assert new.id != old.id and new.stale_since is None and new.stale_reasons == []
    old.refresh_from_db()
    assert not old.is_current and old.stale_since is not None
    assert SnapshotNode.objects.get(snapshot=new, resource=s["top"]).lifecycle_then == "deprecated"


# ---- through the API --------------------------------------------------------------------------------------------------


def test_the_api_shows_stale_reasons_and_the_stale_filter(a, api):
    s = scenario(a)
    stale, fresh = assess(a, s["core"], title="Stale one"), assess(a, s["ledger"], title="Fresh one")
    edit(a, s["top"], lifecycle="deprecated")
    client = api(a["users"]["viewer"])
    body = client.get(f"assessments/{stale.id}").json_body
    assert body["stale"] and body["snapshot"]["stale"] and body["status"] == "draft"  # a flag, not a status
    assert body["snapshot"]["stale_reasons"] == ["Top lifecycle changed (Active to Deprecated)."]
    assert body["snapshot"]["stale_since"]
    node = next(n for n in body["snapshot"]["nodes"] if n["resource"]["name"] == "Top")
    assert node["lifecycle_then"] == "active" and node["lifecycle_now"] == "deprecated" and node["changed"]["lifecycle"]
    assert [r["id"] for r in client.get("assessments?stale=true").json_body] == [str(stale.id)]
    assert [r["id"] for r in client.get("assessments?stale=false").json_body] == [str(fresh.id)]
    refreshed = api(a["users"]["editor"]).post(f"assessments/{stale.id}/refresh")
    assert refreshed.status_code == 201 and not refreshed.json_body["stale"]
    assert client.get("assessments?stale=true").json_body == []
