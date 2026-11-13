"""The assessment tables: composite foreign keys, one current snapshot, PROTECT on records."""

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError

from catalog import services
from catalog.models import AssessmentSnapshot, ChangeAssessment, SnapshotLink, SnapshotNode


def record(ctx, name, kind="application"):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": "active"}
    )


def assessment(ctx, target=None, **kw):
    return ChangeAssessment.objects.create(
        organization=ctx["org"],
        target=target or ctx["app"],
        change_type="change",
        title="Move to the new host",
        owner=ctx["users"]["editor"],
        **kw,
    )


def snapshot(ctx, parent, **kw):
    return AssessmentSnapshot.objects.create(organization=ctx["org"], assessment=parent, **kw)


def node(ctx, snap, resource, **kw):
    fields = {"role": "dependent", "depth": 1, "path": [], "lifecycle_then": "active", "owners_then": {}}
    return SnapshotNode.objects.create(organization=ctx["org"], snapshot=snap, resource=resource, **{**fields, **kw})


def test_database_rejects_a_target_in_another_organization(a, b):
    with pytest.raises(IntegrityError), transaction.atomic():
        assessment(a, target=b["app"])
        connection.check_constraints()  # the composite foreign keys are deferred; force the check


def test_database_rejects_a_snapshot_or_node_in_another_organization(a, b):
    mine = assessment(a)
    with pytest.raises(IntegrityError), transaction.atomic():
        AssessmentSnapshot.objects.create(organization=b["org"], assessment=mine)
        connection.check_constraints()
    snap = snapshot(a, mine)
    with pytest.raises(IntegrityError), transaction.atomic():
        node(a, snap, b["app"])  # another organization's record
        connection.check_constraints()
    with pytest.raises(IntegrityError), transaction.atomic():
        SnapshotNode.objects.create(
            organization=b["org"],
            snapshot=snap,
            resource=b["app"],
            role="dependent",
            depth=1,
            path=[],
            lifecycle_then="active",
            owners_then={},
        )
        connection.check_constraints()


def test_database_rejects_a_snapshot_link_in_another_organization(a, b):
    theirs = services.create_relationship(
        b["org"],
        b["users"]["editor"],
        {"type": "depends_on", "source": str(b["app"].id), "target": str(record(b, "J", "component").id)},
    )
    snap = snapshot(a, assessment(a))
    with pytest.raises(IntegrityError), transaction.atomic():
        SnapshotLink.objects.create(
            organization=a["org"], snapshot=snap, link_kind="relationship", relationship=theirs, signature_then={}
        )
        connection.check_constraints()


def test_only_one_snapshot_is_current(a):
    parent = assessment(a)
    snapshot(a, parent)
    with pytest.raises(IntegrityError), transaction.atomic():
        snapshot(a, parent)
    snapshot(a, parent, is_current=False)
    snapshot(a, assessment(a))  # another assessment has its own current snapshot


def test_a_snapshot_link_names_a_relationship_or_an_integration_not_both(a):
    job = record(a, "Job", "component")
    rel = services.create_relationship(
        a["org"], a["users"]["editor"], {"type": "depends_on", "source": str(a["app"].id), "target": str(job.id)}
    )
    feed = services.create_integration(
        a["org"],
        a["users"]["editor"],
        {"name": "Feed", "lifecycle": "active", "source": str(a["app"].id), "target": str(job.id)},
    )
    snap = snapshot(a, assessment(a))
    with pytest.raises(IntegrityError), transaction.atomic():
        SnapshotLink.objects.create(
            organization=a["org"],
            snapshot=snap,
            link_kind="relationship",
            relationship=rel,
            integration=feed.integration,
            signature_then={},
        )


def test_snapshots_protect_their_records_and_keep_a_removed_link_as_a_null(a):
    job = record(a, "Job", "component")
    rel = services.create_relationship(
        a["org"], a["users"]["editor"], {"type": "depends_on", "source": str(job.id), "target": str(a["app"].id)}
    )
    snap = snapshot(a, assessment(a))
    node(a, snap, job)
    link = SnapshotLink.objects.create(
        organization=a["org"],
        snapshot=snap,
        link_kind="relationship",
        relationship=rel,
        signature_then={"type": "depends_on"},
    )
    with pytest.raises(ProtectedError):
        job.delete()
    with pytest.raises(ProtectedError):
        a["app"].delete()  # the assessment's target
    services.delete_relationship(a["org"], a["users"]["editor"], rel)
    link.refresh_from_db()
    assert link.relationship_id is None and link.link_kind == "relationship" and link.signature_then


def test_hard_delete_is_refused_with_nouns_while_an_assessment_or_snapshot_names_the_record(a):
    job = record(a, "Job", "component")
    snap = snapshot(a, assessment(a))
    node(a, snap, job)
    with pytest.raises(services.Conflict) as refused:
        services.delete_resource(a["org"], a["users"]["admin"], job)
    assert refused.value.extra["blocking_references"] == {"snapshot node": 1}
    with pytest.raises(services.Conflict) as refused:
        services.delete_resource(a["org"], a["users"]["admin"], a["app"])
    assert refused.value.extra["blocking_references"] == {"change assessment": 1}
