"""Snapshot staleness.

The relationship, integration and resource services call these after a change. A current snapshot of an open
assessment (Draft, In review or Ready; a Closed or Cancelled one is finished and isn't nagged) is flagged, with a
readable reason, when:

- a link on its paths is removed, or retyped, or its ends or instances change;
- a `depends_on` link (or one of an added type that counts as a dependency) is added that touches a record in the
  snapshot (a node, or the target: it may be a new path), an `implements` link is added to an integration in it, or
  an integration is added or re-pointed at one;
- an added relationship type starts or stops counting as a dependency while a link of that type touches a record in
  it;
- a snapshot node's lifecycle or business or technical owner now differs from the value saved with the snapshot.

Anything else (names, purposes, descriptions, support team, criticality, archiving, link confirmations) does not
flag. A flag is only cleared by refreshing the snapshot (assessments.refresh_snapshot); nothing refreshes by itself.
The target's own lifecycle and owners are not checked: retiring the target is what a retirement assessment is for,
and it would flag itself.
"""

from django.utils import timezone

from .models import (
    AssessmentSnapshot,
    AssessmentStatus,
    ChangeAssessment,
    Integration,
    Kind,
    Lifecycle,
    NodeRole,
    Relationship,
    RelationshipKind,
    Resource,
    SnapshotLink,
    SnapshotNode,
)

OPEN_STATUSES = [AssessmentStatus.DRAFT, AssessmentStatus.IN_REVIEW, AssessmentStatus.READY]
MAX_REASONS = 20
OWNER_LABELS = {"business_owner": "business owner", "technical_owner": "technical owner"}
LINK_PARTS = {
    "type": "type",
    "source": "source",
    "source_instance": "source instance",
    "target": "target",
    "target_instance": "target instance",
}


def _nodes(org):
    """Snapshot nodes (not cycle rows) in the current snapshot of an open assessment."""
    return SnapshotNode.objects.filter(
        organization=org,
        in_cycle=False,
        snapshot__is_current=True,
        snapshot__assessment__status__in=OPEN_STATUSES,
    )


def _links(org):
    return SnapshotLink.objects.filter(
        organization=org, snapshot__is_current=True, snapshot__assessment__status__in=OPEN_STATUSES
    )


def _snapshots_touching(org, resource_ids, roles=None):
    """Current snapshots of open assessments that contain any of these records as a node, or have one as the target
    (unless roles limits the match to certain node roles)."""
    nodes = _nodes(org).filter(resource_id__in=resource_ids)
    if roles:
        nodes = nodes.filter(role__in=roles)
    found = set(nodes.values_list("snapshot_id", flat=True))
    if not roles:
        found |= set(
            AssessmentSnapshot.objects.filter(
                organization=org,
                is_current=True,
                assessment__in=ChangeAssessment.objects.filter(
                    organization=org, target_id__in=resource_ids, status__in=OPEN_STATUSES
                ),
            ).values_list("id", flat=True)
        )
    return found


def _names(org, ids):
    return dict(Resource.objects.filter(organization=org, id__in=[i for i in ids if i]).values_list("id", "name"))


def _add(found, snapshot_ids, reason):
    for snapshot_id in snapshot_ids:
        found.setdefault(snapshot_id, []).append(reason)


def flag(org, actor, found):
    """Mark snapshots stale. found is {snapshot_id: [reasons]}. A reason already stored is not repeated; a snapshot
    that is already stale keeps its first `stale_since` and gains the new reasons. Audit `assessment.stale` the
    first time a snapshot goes stale."""
    from .services import audit

    if not found:
        return
    now = timezone.now()
    snapshots = (
        AssessmentSnapshot.objects.select_for_update()
        .filter(organization=org, id__in=list(found), is_current=True)
        .select_related("assessment")
    )
    for snapshot in snapshots:
        new = [r for r in dict.fromkeys(found[snapshot.id]) if r not in snapshot.stale_reasons]
        if not new:
            continue
        first = snapshot.stale_since is None
        if first:
            snapshot.stale_since = now
        snapshot.stale_reasons = [*snapshot.stale_reasons, *new][:MAX_REASONS]
        snapshot.save(update_fields=["stale_since", "stale_reasons", "updated_at"])
        if first:
            audit(org, actor, "assessment.stale", snapshot.assessment, {"snapshot": str(snapshot.id), "reasons": new})


# ---- relationships ---------------------------------------------------------------------------------------------


def _added(org, found, rtype, source_id, target_id):
    """Snapshots a new link of this shape would change: see the module docstring. rtype is the RelationshipType."""
    from .relationship_types import phrase

    names = _names(org, [source_id, target_id])
    if rtype.is_dependency:
        reason = f"A {rtype.slug} link was added: {names[source_id]} {phrase(rtype)} {names[target_id]}."
        _add(found, _snapshots_touching(org, [source_id, target_id]), reason)
    elif rtype.is_core and rtype.slug == RelationshipKind.IMPLEMENTS:
        reason = f"{names[source_id]} now implements the integration {names[target_id]}."
        _add(found, _snapshots_touching(org, [target_id], roles=[NodeRole.DATA_FLOW]), reason)


def relationship_added(org, actor, rel):
    found: dict = {}
    _added(org, found, rel.type, rel.source_id, rel.target_id)
    flag(org, actor, found)


def relationship_removed(org, actor, rel):
    """Call before the link is deleted, while the snapshot links still point at it."""
    found: dict = {}
    names = _names(org, [rel.source_id, rel.target_id])
    reason = f"A {rel.type.slug} link was removed: {names[rel.source_id]} to {names[rel.target_id]}."
    _add(found, _links(org).filter(relationship_id=rel.id).values_list("snapshot_id", flat=True), reason)
    flag(org, actor, found)


def relationship_changed(org, actor, rel, before, after):
    """before and after are relationship_audit_values (strings). Retyping, re-pointing or changing an instance
    changes the link in every snapshot that has it, and the new shape may be a new path in others."""
    parts = [label for key, label in LINK_PARTS.items() if before[key] != after[key]]
    if not parts:
        return
    found: dict = {}
    names = _names(org, [rel.source_id, rel.target_id])
    reason = (
        f"The {before['type']} link from {names[rel.source_id]} to {names[rel.target_id]} was changed "
        f"({', '.join(parts)})."
    )
    _add(found, _links(org).filter(relationship_id=rel.id).values_list("snapshot_id", flat=True), reason)
    _added(org, found, rel.type, rel.source_id, rel.target_id)
    flag(org, actor, found)


def relationship_type_changed(org, actor, rtype):
    """An added type started or stopped counting as a dependency: every open snapshot that has one of its links, or
    touches a record at either end of one, may now have a different affected set."""
    links = Relationship.objects.filter(organization=org, type=rtype)
    ends = {i for pair in links.values_list("source_id", "target_id") for i in pair}
    if not ends:
        return
    found: dict = {}
    now = "now counts" if rtype.is_dependency else "no longer counts"
    reason = f"The relationship type {rtype.name} {now} as a dependency."
    _add(found, _links(org).filter(relationship__type=rtype).values_list("snapshot_id", flat=True), reason)
    _add(found, _snapshots_touching(org, list(ends)), reason)
    flag(org, actor, found)


# ---- integrations --------------------------------------------------------------------------------------------------


def integration_added(org, actor, resource):
    ext = resource.integration
    found: dict = {}
    reason = f"A data flow was added: {resource.name}."
    _add(found, _snapshots_touching(org, [ext.source_resource_id, ext.target_resource_id]), reason)
    flag(org, actor, found)


def integration_ends_changed(org, actor, resource, before, after):
    """before and after are audit snapshots of the integration. Re-pointing it changes it in every snapshot that has
    it, and a snapshot that now touches it has a new data flow."""
    parts = [label for key, label in LINK_PARTS.items() if key != "type" and before[key] != after[key]]
    if not parts:
        return
    found: dict = {}
    reason = f"The data flow {resource.name} was changed ({', '.join(parts)})."
    _add(found, _links(org).filter(integration_id=resource.id).values_list("snapshot_id", flat=True), reason)
    ext = Integration.objects.get(resource_id=resource.id)
    _add(
        found,
        _snapshots_touching(org, [ext.source_resource_id, ext.target_resource_id]),
        f"A data flow was added: {resource.name}.",
    )
    flag(org, actor, found)


# ---- records ---------------------------------------------------------------------------------------------------------


def resource_changed(org, actor, resource, before, after):
    """Called by update_resource with the audit snapshots from before and after the save: a node whose lifecycle or
    owners now differ from the values saved with the snapshot."""
    if resource.kind == Kind.INTEGRATION:
        integration_ends_changed(org, actor, resource, before, after)
    if not any(before.get(k) != after.get(k) for k in ("lifecycle", *OWNER_LABELS)):
        return
    found: dict = {}
    now_owners = {
        k: (str(getattr(resource, f"{k}_id")) if getattr(resource, f"{k}_id") else None) for k in OWNER_LABELS
    }
    for node in _nodes(org).filter(resource_id=resource.id):
        reasons = []
        if node.lifecycle_then != resource.lifecycle:
            then, now = Lifecycle(node.lifecycle_then).label, Lifecycle(resource.lifecycle).label
            reasons.append(f"{resource.name} lifecycle changed ({then} to {now}).")
        for key, label in OWNER_LABELS.items():
            if (node.owners_then.get(key) or None) != now_owners[key]:
                reasons.append(f"{resource.name} {label} changed.")
        if reasons:
            _add(found, [node.snapshot_id], " ".join(reasons))
    flag(org, actor, found)
