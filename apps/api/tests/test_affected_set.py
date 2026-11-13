"""The affected set and the snapshot writer."""

from django.db import connection
from django.test.utils import CaptureQueriesContext

from catalog import assessments, services
from catalog.models import ChangeAssessment, Resource, SnapshotLink, SnapshotNode


def record(ctx, name, kind="application", lifecycle="active", **extra):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": lifecycle, **extra}
    )


def depends(ctx, source, target, **extra):
    """source depends on target."""
    return services.create_relationship(
        ctx["org"],
        ctx["users"]["editor"],
        {"type": "depends_on", "source": str(source.id), "target": str(target.id), **extra},
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


def readable(ctx, computed):
    """(name, role, depth, path of names, in_cycle) per node."""
    names = {str(r.id): r.name for r in Resource.objects.filter(organization=ctx["org"])}
    return [
        (names[n["id"]], n["role"], n["depth"], [names[i] for i in n["path"]], n["in_cycle"]) for n in computed["nodes"]
    ]


def test_dependents_at_any_depth_with_paths_and_the_target_left_out(a):
    core, mid, top = record(a, "Core"), record(a, "Mid", "component"), record(a, "Top")
    depends(a, mid, core)
    depends(a, top, mid)
    elsewhere = record(a, "Elsewhere")
    depends(a, core, elsewhere)  # something the target depends on is not affected
    got = readable(a, assessments.affected_set(a["org"], core))
    assert got == [
        ("Mid", "dependent", 1, ["Core", "Mid"], False),
        ("Top", "dependent", 2, ["Core", "Mid", "Top"], False),
    ]


def test_a_cycle_back_to_the_target_is_flagged_not_a_member(a):
    core, mid = record(a, "Core"), record(a, "Mid")
    depends(a, mid, core)
    depends(a, core, mid)  # the target depends on its own dependent
    got = readable(a, assessments.affected_set(a["org"], core))
    assert ("Mid", "dependent", 1, ["Core", "Mid"], False) in got
    assert ("Core", "dependent", 2, ["Core", "Mid", "Core"], True) in got
    assert [g for g in got if g[0] == "Core" and not g[4]] == []  # never a real member


def test_a_loop_between_branches_is_flagged_in_the_snapshot(a):
    root, left, right = record(a, "Root"), record(a, "A"), record(a, "B")
    for side in (left, right):
        depends(a, side, root)
    depends(a, left, right)
    depends(a, right, left)
    got = readable(a, assessments.affected_set(a["org"], root))
    assert got == [
        ("A", "dependent", 1, ["Root", "A"], False),
        ("B", "dependent", 1, ["Root", "B"], False),
        ("B", "dependent", 2, ["Root", "A", "B"], True),
    ]


def test_data_flows_one_hop_with_implementers_and_their_links(a):
    core, mid = record(a, "Core"), record(a, "Mid", "component")
    depends(a, mid, core)
    ledger, job = record(a, "Ledger"), record(a, "Export job", "component")
    out = flow(a, "Core to Ledger", core, ledger)
    via_mid = flow(a, "Mid to Ledger", mid, ledger)
    implements(a, job, out)
    computed = assessments.affected_set(a["org"], core)
    got = readable(a, computed)
    assert ("Core to Ledger", "data_flow", 1, ["Core", "Core to Ledger"], False) in got
    assert ("Mid to Ledger", "data_flow", 2, ["Core", "Mid", "Mid to Ledger"], False) in got
    assert ("Export job", "implementer", 2, ["Core", "Core to Ledger", "Export job"], False) in got
    assert not any(g[0] == "Ledger" for g in got)  # the far end of a data flow is not an affected record
    kinds = sorted(link["kind"] for link in computed["links"])
    assert kinds == ["integration", "integration", "relationship", "relationship"]  # mid->core, job implements out
    assert str(via_mid.id) in {link["id"] for link in computed["links"]}


def test_retired_dependents_are_kept_and_tagged_by_lifecycle(a):
    core = record(a, "Core")
    old = record(a, "Old", lifecycle="retired")
    depends(a, old, core)
    got = assessments.affected_set(a["org"], core)
    assert readable(a, got) == [("Old", "dependent", 1, ["Core", "Old"], False)]
    assert got["nodes"][0]["lifecycle_then"] == "retired"


def test_then_values_are_copied_as_ids_and_signatures_hold_the_owner(a):
    dana, priya = a["people"]["dana"], a["people"]["priya"]
    core = record(a, "Core")
    mid = record(a, "Mid", technical_owner=str(dana.id), business_owner=str(priya.id))
    rel = depends(a, mid, core)
    computed = assessments.affected_set(a["org"], core)
    node = computed["nodes"][0]
    assert node["lifecycle_then"] == "active"
    assert node["owners_then"] == {"business_owner": str(priya.id), "technical_owner": str(dana.id)}
    link = computed["links"][0]
    assert link["id"] == str(rel.id) and link["signature"]["owner"] == str(dana.id)


def test_write_snapshot_stores_nodes_and_links_and_keeps_history(a):
    core, mid = record(a, "Core"), record(a, "Mid")
    rel = depends(a, mid, core)
    saved = ChangeAssessment.objects.create(
        organization=a["org"], target=core, change_type="change", title="T", owner=a["users"]["editor"]
    )
    first = assessments.write_snapshot(a["org"], a["users"]["editor"], saved)
    assert first.is_current and first.taken_by == a["users"]["editor"] and first.stale_since is None
    node = SnapshotNode.objects.get(snapshot=first)
    assert node.resource_id == mid.id and node.path == [str(core.id), str(mid.id)] and not node.in_cycle
    link = SnapshotLink.objects.get(snapshot=first)
    assert link.relationship_id == rel.id and link.link_kind == "relationship"
    assert link.signature_then["type"] == "depends_on"
    depends(a, record(a, "Late"), core)
    second = assessments.write_snapshot(a["org"], a["users"]["editor"], saved)
    first.refresh_from_db()
    assert second.is_current and not first.is_current
    assert SnapshotNode.objects.filter(snapshot=first).count() == 1  # the old snapshot is untouched
    assert SnapshotNode.objects.filter(snapshot=second).count() == 2


def test_the_organization_boundary_holds_when_the_other_organization_has_the_same_shape(a, b):
    for ctx in (a, b):
        mid = record(ctx, "Mid")
        depends(ctx, mid, ctx["app"])
        flow(ctx, "Feed", ctx["app"], mid)
    got = assessments.affected_set(a["org"], a["app"])
    ours = set(map(str, Resource.objects.filter(organization=a["org"]).values_list("id", flat=True)))
    assert {n["id"] for n in got["nodes"]} <= ours and len(got["nodes"]) == 2


def test_the_query_count_does_not_grow_with_the_graph(a):
    def build(count):
        core = record(a, f"Hub {count}")
        for i in range(count):
            depends(a, record(a, f"Leaf {count}-{i}"), core)
        return core

    small, large = build(2), build(25)
    with CaptureQueriesContext(connection) as few:
        assessments.affected_set(a["org"], small)
    with CaptureQueriesContext(connection) as many:
        assessments.affected_set(a["org"], large)
    assert len(many) <= len(few) + 1


# ---- The type's labels on a snapshot's link rows ------------------------------------------------------------------


def test_snapshot_links_carry_the_type_name_even_after_the_type_is_deleted(a, api):
    from catalog import relationship_types
    from catalog.models import RelationshipType

    core, host, user = record(a, "Core"), record(a, "Host", "component"), record(a, "User")
    rtype = relationship_types.create_type(
        a["org"], a["users"]["admin"], {"name": "Runs on", "inverse_name": "Hosts", "is_dependency": True}
    )
    runs_on = services.create_relationship(
        a["org"], a["users"]["editor"], {"type": rtype.slug, "source": str(host.id), "target": str(core.id)}
    )
    depends(a, user, core)
    flow(a, "Feed", core, user)
    saved = ChangeAssessment.objects.create(
        organization=a["org"], target=core, change_type="change", title="T", owner=a["users"]["editor"]
    )
    snapshot = assessments.write_snapshot(a["org"], a["users"]["editor"], saved)
    stored = SnapshotLink.objects.get(snapshot=snapshot, relationship=runs_on)
    assert (stored.type_name_then, stored.inverse_name_then) == ("Runs on", "Hosts")
    viewer = api(a["users"]["viewer"])

    def labels():
        rows = viewer.get(f"assessments/{saved.id}").json_body["snapshot"]["links"]
        return {(r["kind"], r["type"]): (r["type_name"], r["inverse_name"]) for r in rows}

    assert labels() == {
        ("relationship", "runs_on"): ("Runs on", "Hosts"),
        ("relationship", "depends_on"): ("Depends on", "Is needed by"),
        ("integration", "integration"): (None, None),
    }
    # Renamed: the current labels, as every other screen words the link; nothing reads as changed.
    relationship_types.update_type(a["org"], a["users"]["admin"], rtype, {"name": "Is hosted on"})
    assert labels()[("relationship", "runs_on")] == ("Is hosted on", "Hosts")
    rows = viewer.get(f"assessments/{saved.id}").json_body["snapshot"]["links"]
    assert not any(r["changed"] for r in rows)
    # Deleted (its link first): the labels saved with the snapshot.
    services.delete_relationship(a["org"], a["users"]["editor"], runs_on)
    relationship_types.delete_type(a["org"], a["users"]["admin"], RelationshipType.objects.get(id=rtype.id))
    assert labels()[("relationship", "runs_on")] == ("Runs on", "Hosts")
    # A snapshot taken before the labels were saved falls back to the slug.
    SnapshotLink.objects.filter(snapshot=snapshot, link_kind="relationship").update(
        type_name_then="", inverse_name_then=""
    )
    assert labels()[("relationship", "runs_on")] == ("runs_on", "")
    assert labels()[("relationship", "depends_on")] == ("Depends on", "Is needed by")
