"""The change assessment responses: an assessment with its current snapshot's summary, the
snapshot in full beside how things are now, and the snapshot history. Built as plain data from a few grouped queries
(a hub's snapshot has about 1,200 nodes and links); catalog/serializers.py describes each shape for OpenAPI."""

from types import SimpleNamespace

from django.db.models import Count

from core.drf import UserBriefSerializer, represent

from . import links, services
from .models import (
    AssessmentSnapshot,
    Integration,
    Person,
    Relationship,
    RelationshipType,
    Resource,
    SnapshotLink,
    SnapshotNode,
)
from .state import link_signature

COUNT_KEYS = {"dependent": "dependents", "data_flow": "data_flows", "implementer": "implementers"}


def snapshot_counts(org, ids):
    """{snapshot_id: counts}, from two grouped queries. A cycle row counts as a cycle, not as a dependent."""
    counts = {i: {"dependents": 0, "cycles": 0, "data_flows": 0, "implementers": 0, "links": 0} for i in ids}
    nodes = (
        SnapshotNode.objects.filter(organization=org, snapshot_id__in=ids)
        .values("snapshot_id", "role", "in_cycle")
        .annotate(n=Count("id"))
    )
    for row in nodes:
        key = "cycles" if row["in_cycle"] else COUNT_KEYS[row["role"]]
        counts[row["snapshot_id"]][key] += row["n"]
    links = (
        SnapshotLink.objects.filter(organization=org, snapshot_id__in=ids).values("snapshot_id").annotate(n=Count("id"))
    )
    for link_row in links:
        counts[link_row["snapshot_id"]]["links"] = link_row["n"]
    return counts


def snapshot_summary_json(s, counts):
    return {
        "id": str(s.id),
        "taken_at": s.taken_at,
        "taken_by": represent(UserBriefSerializer, s.taken_by),
        "is_current": s.is_current,
        "stale": s.stale_since is not None,
        "stale_since": s.stale_since,
        "stale_reasons": s.stale_reasons,
        "counts": counts,
    }


def snapshot_detail_json(org, s, counts):
    """A snapshot with its nodes and links, then beside now. Names and archived flags are current; lifecycle and
    owners are given both as they were and as they are, so the screen can show what changed."""
    # Plain rows, not models: a hub's snapshot has about 1,200 of each and this runs on every GET.
    nodes = [
        SimpleNamespace(**row)
        for row in s.nodes.values(
            "id", "resource_id", "role", "depth", "path", "in_cycle", "lifecycle_then", "owners_then"
        )
    ]
    links = [
        SimpleNamespace(**row)
        for row in s.links.values(
            "link_kind", "relationship_id", "integration_id", "signature_then", "type_name_then", "inverse_name_then"
        )
    ]
    wanted = {str(n.resource_id) for n in nodes} | {i for n in nodes for i in n.path}
    for link in links:
        wanted |= {link.signature_then[end] for end in ("source", "target")}
    resources = {
        str(r.id): r
        for r in Resource.objects.filter(organization=org, id__in=wanted).only(
            "name", "kind", "lifecycle", "archived_at", "business_owner_id", "technical_owner_id"
        )
    }
    person_ids = {p for n in nodes for p in n.owners_then.values() if p} | {
        str(pid) for r in resources.values() for pid in (r.business_owner_id, r.technical_owner_id) if pid
    }
    people_by_id = {str(p.id): p for p in Person.objects.filter(organization=org, id__in=person_ids)}
    refs = {i: {"id": i, "name": r.name, "kind": r.kind} for i, r in resources.items()}  # shared by every row

    def ref(resource_id):
        return refs[str(resource_id)]

    def owner(person_id):
        p = people_by_id.get(str(person_id)) if person_id else None
        return p and {"id": str(p.id), "name": p.name, "active": p.active}

    def owners(business, technical):
        return {"business_owner": owner(business), "technical_owner": owner(technical)}

    role_order = {"dependent": 0, "data_flow": 1, "implementer": 2}
    out_nodes = []
    for n in sorted(
        nodes, key=lambda n: (role_order[n.role], n.depth, resources[str(n.resource_id)].name.lower(), n.id)
    ):
        r = resources[str(n.resource_id)]
        now = owners(r.business_owner_id, r.technical_owner_id)
        then = owners(n.owners_then.get("business_owner"), n.owners_then.get("technical_owner"))
        then_ids = {k: n.owners_then.get(k) or None for k in ("business_owner", "technical_owner")}
        now_ids = {
            "business_owner": str(r.business_owner_id) if r.business_owner_id else None,
            "technical_owner": str(r.technical_owner_id) if r.technical_owner_id else None,
        }
        out_nodes.append(
            {
                "id": str(n.id),
                "resource": {**ref(r.id), "archived": r.archived_at is not None},
                "role": n.role,
                "depth": n.depth,
                "path": [ref(i) for i in n.path],
                "in_cycle": n.in_cycle,
                "lifecycle_then": n.lifecycle_then,
                "lifecycle_now": r.lifecycle,
                "owners_then": then,
                "owners_now": now,
                "changed": {
                    "lifecycle": n.lifecycle_then != r.lifecycle,
                    "owners": then_ids != now_ids,
                },
            }
        )
    current = current_link_states(org, s)
    # Type labels: the type's current ones while it exists (as every other screen words links), else those saved
    # with the snapshot, else the slug (a type deleted before snapshots saved them).
    type_labels = {
        slug: (name, inverse)
        for slug, name, inverse in RelationshipType.objects.filter(
            organization=org,
            slug__in={link.signature_then["type"] for link in links if link.link_kind == "relationship"},
        ).values_list("slug", "name", "inverse_name")
    }

    def labels(link):
        if link.link_kind != "relationship":
            return None, None
        slug = link.signature_then["type"]
        if slug in type_labels:
            return type_labels[slug]
        return link.type_name_then or slug, link.inverse_name_then

    out_links = []
    for link in links:
        link_id = link.relationship_id or link.integration_id
        signature = link.signature_then
        now = current.get((link.link_kind, link_id))
        type_name, inverse_name = labels(link)
        out_links.append(
            {
                "kind": link.link_kind,
                "id": str(link_id) if link_id else None,
                "removed": link_id is None,
                "changed": link_id is None or now is None or now[0] != signature,
                "type": signature["type"],
                "type_name": type_name,
                "inverse_name": inverse_name,
                "source": ref(signature["source"]),
                "target": ref(signature["target"]),
                "signature_then": signature,
                # The link's confirmation state now, apart from what changed since the snapshot.
                # A removed link has none.
                "link_state": now[1]["link_state"] if now else None,
                "link_why": now[1]["link_why"] if now else None,
                "last_confirmed_on": now[1]["last_confirmed_on"] if now else None,
            }
        )
    out_links.sort(key=lambda x: (x["kind"], x["source"]["name"].lower(), x["target"]["name"].lower()))
    return {**snapshot_summary_json(s, counts), "nodes": out_nodes, "links": out_links}


def current_link_states(org, snapshot):
    """{(kind, id): (the link's signature now, its link state fields now)} for the snapshot's links that still exist,
    from three queries however many links there are (the IDs stay in the database as subqueries)."""
    out = {}
    rel_ids = snapshot.links.filter(relationship_id__isnull=False).values("relationship_id")
    int_ids = snapshot.links.filter(integration_id__isnull=False).values("integration_id")
    latest = links.latest_confirmations(org, relationship_ids=rel_ids, integration_ids=int_ids)
    today = services.org_today(org)
    # Plain tuples, not models: a hub's snapshot has about 1,200 links, and this runs on every GET.
    rows = Relationship.objects.filter(organization=org, id__in=rel_ids).values_list(
        "id",
        "type__slug",
        "source_id",
        "source_instance_id",
        "target_id",
        "target_instance_id",
        "source__technical_owner_id",
    )
    for link_id, rel_type, source, source_instance, target, target_instance, owner in rows:
        signature = link_signature(rel_type, source, source_instance, target, target_instance, owner)
        out[("relationship", link_id)] = (
            signature,
            links.state_fields(org, signature, latest.get(("relationship", link_id)), today),
        )
    flows = Integration.objects.filter(organization=org, resource_id__in=int_ids).values_list(
        "resource_id",
        "source_resource_id",
        "source_instance_id",
        "target_resource_id",
        "target_instance_id",
        "resource__technical_owner_id",
    )
    for link_id, source, source_instance, target, target_instance, owner in flows:
        signature = link_signature("integration", source, source_instance, target, target_instance, owner)
        out[("integration", link_id)] = (
            signature,
            links.state_fields(org, signature, latest.get(("integration", link_id)), today),
        )
    return out


def assessment_json(a, current, counts):
    return {
        "id": str(a.id),
        "target": {
            "id": str(a.target_id),
            "name": a.target.name,
            "kind": a.target.kind,
            "lifecycle": a.target.lifecycle,
            "archived": a.target.archived_at is not None,
        },
        "change_type": a.change_type,
        "title": a.title,
        "description": a.description,
        "owner": represent(UserBriefSerializer, a.owner),
        "target_date": a.target_date.isoformat() if a.target_date else None,
        "ticket_url": a.ticket_url,
        "findings": a.findings,
        "status": a.status,
        "retirement_ack_reason": a.retirement_ack_reason,
        "stale": current is not None and current.stale_since is not None,
        "snapshot": current and snapshot_summary_json(current, counts),
        "created_at": a.created_at,
        "updated_at": a.updated_at,
    }


def assessment_rows(org, rows):
    """List JSON for loaded assessments: the current snapshot's summary, from three queries however many rows."""
    current = {
        s.assessment_id: s
        for s in AssessmentSnapshot.objects.filter(
            organization=org, assessment__in=[a.id for a in rows], is_current=True
        ).select_related("taken_by")
    }
    counts = snapshot_counts(org, [s.id for s in current.values()])
    return [
        assessment_json(a, current.get(a.id), counts.get(current[a.id].id) if a.id in current else None) for a in rows
    ]


def assessment_detail(org, a):
    """The list shape plus the current snapshot in full and the history of earlier snapshots (read-only)."""
    snapshots = list(AssessmentSnapshot.objects.filter(organization=org, assessment=a).select_related("taken_by"))
    counts = snapshot_counts(org, [s.id for s in snapshots])
    current = next((s for s in snapshots if s.is_current), None)
    body = assessment_json(a, current, counts.get(current.id) if current else None)
    body["snapshot"] = current and snapshot_detail_json(org, current, counts[current.id])
    body["history"] = [snapshot_summary_json(s, counts[s.id]) for s in snapshots]
    return body


def snapshot_history(org, assessment):
    """Every snapshot's summary, newest first."""
    snapshots = list(
        AssessmentSnapshot.objects.filter(organization=org, assessment=assessment).select_related("taken_by")
    )
    counts = snapshot_counts(org, [s.id for s in snapshots])
    return [snapshot_summary_json(s, counts[s.id]) for s in snapshots]


def snapshot_detail(org, snapshot):
    return snapshot_detail_json(org, snapshot, snapshot_counts(org, [snapshot.id])[snapshot.id])
