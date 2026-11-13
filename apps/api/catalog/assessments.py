"""Change assessments: the affected set and the snapshot that stores it.

The walk is graph.collect(), the same one the dependency explorer uses, with no cap and retired records included
(they are shown tagged). A snapshot copies what it needs from the catalog: each record's
lifecycle and owners then, the path to it, and the signature of every link on the paths, so it stays readable and
comparable after the catalog moves on. A relationship link also keeps its type's name and inverse name then, so a
type deleted since still reads in words.
"""

import json
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import connection, transaction
from django.utils import timezone

from core import dates
from core.input import text_value, whole_number
from core.problems import FieldErrors

from . import graph
from .models import (
    RECORD_KINDS,
    AssessmentSnapshot,
    AssessmentStatus,
    ChangeAssessment,
    ChangeType,
    Lifecycle,
    NodeRole,
    Relationship,
    RelationshipKind,
    RelationshipType,
    Resource,
    SnapshotLink,
    SnapshotNode,
)
from .services import NAME_MAX, TEXT_MAX, WRITE_ROLES, _in_org, audit, retirement_check
from .state import link_signature


def owners_then(business_owner_id, technical_owner_id):
    """Person IDs only: names would outlive an anonymized Person."""
    return {
        "business_owner": str(business_owner_id) if business_owner_id else None,
        "technical_owner": str(technical_owner_id) if technical_owner_id else None,
    }


def affected_set(org, target):
    """The affected set of target (an application or component of org) as plain dicts, not saved:

    - `nodes`: every dependent through `depends_on` at any depth (role "dependent"), nearest first, each with its
      path of record IDs from the target; the target is not in its own set, but a path that leads back to a record
      already on it (the target included) is kept as a row with `in_cycle` true. Then the integrations of the target
      and of each dependent, one hop out (role "data_flow"), and the components that implement those (role
      "implementer").
    - `links`: the `depends_on` link of every row, each integration, and each `implements` link, with the signature
      it has now (kind, id, signature).

    Each node carries `lifecycle_then` and `owners_then`."""
    found = graph.collect(org, target, "dependents", None, True, True)
    target_id, records = found["target_id"], found["records"]
    walk = found["passes"]["dependents"]

    nodes, links, path_of, seen_links = [], [], {target_id: [target_id]}, set()

    def add_link(kind, link_id, signature):
        if (kind, link_id) not in seen_links:
            seen_links.add((kind, link_id))
            links.append({"kind": kind, "id": link_id, "signature": signature})

    for row in sorted(walk.rows, key=lambda r: (r["depth"], records[r["id"]][0].lower(), r["id"])):
        steps = walk.path(row)
        path = [step for step, _ in steps]
        if not row["cycle"]:
            path_of[row["id"]] = path
        nodes.append(
            {"id": row["id"], "role": NodeRole.DEPENDENT, "depth": row["depth"], "path": path, "in_cycle": row["cycle"]}
        )
        edge = row["edge"]
        add_link(
            "relationship",
            edge["id"],
            link_signature(
                edge["type"],
                edge["source_id"],
                edge["source_instance_id"],
                edge["target_id"],
                edge["target_instance_id"],
                records[edge["source_id"]][2],
            ),
        )

    flow_paths, flow_depth = {}, {}
    for flow in found["flow_rows"]:
        path = [*path_of[flow["attached_to"]], flow["id"]]
        flow_paths[flow["id"]] = path
        flow_depth[flow["id"]] = flow["depth"]
        nodes.append(
            {"id": flow["id"], "role": NodeRole.DATA_FLOW, "depth": flow["depth"], "path": path, "in_cycle": False}
        )
        add_link(
            "integration",
            flow["id"],
            link_signature(
                "integration",
                flow["source_resource_id"],
                flow["source_instance_id"],
                flow["target_resource_id"],
                flow["target_instance_id"],
                flow["owner_id"],
            ),
        )
    implements = (
        Relationship.objects.filter(
            organization=org, type__slug=RelationshipKind.IMPLEMENTS, target_id__in=list(flow_paths)
        )
        .select_related("type")
        .order_by("created_at", "id")
    )
    for rel in implements:
        flow_id, source_id = str(rel.target_id), str(rel.source_id)
        nodes.append(
            {
                "id": source_id,
                "role": NodeRole.IMPLEMENTER,
                "depth": flow_depth[flow_id] + 1,
                "path": [*flow_paths[flow_id], source_id],
                "in_cycle": False,
            }
        )
        add_link(
            "relationship",
            str(rel.id),
            link_signature(
                rel.type.slug, source_id, rel.source_instance_id, flow_id, rel.target_instance_id, records[source_id][2]
            ),
        )

    then = {
        str(rid): (lifecycle, owners_then(business, technical))
        for rid, lifecycle, business, technical in Resource.objects.filter(
            organization=org, id__in={n["id"] for n in nodes}
        ).values_list("id", "lifecycle", "business_owner_id", "technical_owner_id")
    }
    for n in nodes:
        n["lifecycle_then"], n["owners_then"] = then[n["id"]]
    return {"nodes": nodes, "links": links}


def _copy(model, columns, rows):
    """Insert rows with COPY (one round trip). Values are already text, UUID strings, numbers or datetimes."""
    with connection.cursor() as cursor:
        with cursor.cursor.copy(f"COPY {model._meta.db_table} ({', '.join(columns)}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)


@transaction.atomic
def write_snapshot(org, actor, assessment):
    """Compute the affected set of the assessment's target and save it as the assessment's current snapshot. An
    earlier current snapshot stays, read-only history. Returns the new snapshot."""
    ChangeAssessment.objects.select_for_update().filter(id=assessment.id).first()  # one refresh at a time
    computed = affected_set(org, assessment.target)
    AssessmentSnapshot.objects.filter(assessment=assessment, is_current=True).update(is_current=False)
    snapshot = AssessmentSnapshot.objects.create(
        organization=org, assessment=assessment, taken_at=timezone.now(), taken_by=actor, is_current=True
    )
    now = timezone.now()
    # COPY, not INSERT: a hub's snapshot is over a thousand node rows and as many links, and one multi-row INSERT
    # per 500 costs most of the request (the target is under a second with ten writers at once).
    org_id, snapshot_id = str(org.id), str(snapshot.id)
    _copy(
        SnapshotNode,
        ("id", "created_at", "updated_at", "organization_id", "snapshot_id", "resource_id", "role", "depth", "path")
        + ("lifecycle_then", "owners_then", "in_cycle"),
        (
            (
                str(uuid.uuid4()),
                now,
                now,
                org_id,
                snapshot_id,
                n["id"],
                n["role"],
                n["depth"],
                json.dumps(n["path"]),
                n["lifecycle_then"],
                json.dumps(n["owners_then"]),
                n["in_cycle"],
            )
            for n in computed["nodes"]
        ),
    )
    slugs = {link["signature"]["type"] for link in computed["links"] if link["kind"] == "relationship"}
    labels = {
        slug: (name, inverse)
        for slug, name, inverse in RelationshipType.objects.filter(organization=org, slug__in=slugs).values_list(
            "slug", "name", "inverse_name"
        )
    }
    _copy(
        SnapshotLink,
        ("id", "created_at", "updated_at", "organization_id", "snapshot_id", "link_kind")
        + ("relationship_id", "integration_id", "signature_then", "type_name_then", "inverse_name_then"),
        (
            (
                str(uuid.uuid4()),
                now,
                now,
                org_id,
                snapshot_id,
                link["kind"],
                link["id"] if link["kind"] == "relationship" else None,
                link["id"] if link["kind"] == "integration" else None,
                json.dumps(link["signature"]),
                *(labels.get(link["signature"]["type"], ("", "")) if link["kind"] == "relationship" else ("", "")),
            )
            for link in computed["links"]
        ),
    )
    return snapshot


# ---- The assessment service ------------------------------------------------------------------------------


def assessment_audit_values(assessment):
    """The editable fields as audit payload values; references are IDs."""
    return {
        "title": assessment.title,
        "description": assessment.description,
        "owner": assessment.owner_id,
        "target_date": assessment.target_date.isoformat() if assessment.target_date else None,
        "ticket_url": assessment.ticket_url,
        "findings": assessment.findings,
    }


def _assessment_fields(org, actor, data, creating):
    """Validate input for a new assessment or an edit; only keys present in data are returned (on create the
    required ones must be present). Target, change type and status are set when creating and not edited here: the
    target and type define the snapshot, and the status has its own action (it carries the retirement rule)."""
    errors: dict[str, list[str]] = {}
    values: dict = {}
    if creating:
        target = _in_org(Resource, org, data.get("target"), "target", errors)
        if target is None and "target" not in errors:
            errors["target"] = ["Choose an application, component or AI agent."]
        elif target is not None and target.kind not in RECORD_KINDS:
            errors["target"] = ["Choose an application, component or AI agent."]
        else:
            values["target"] = target
        if data.get("change_type") not in ChangeType.values:
            errors["change_type"] = ["Choose change or retirement."]
        else:
            values["change_type"] = data["change_type"]
        status = data.get("status") or AssessmentStatus.DRAFT
        if status not in AssessmentStatus.values:
            errors["status"] = ["Choose draft, in review, ready, closed or canceled."]
        else:
            values["status"] = status
    else:
        for field, message in (
            ("target", "The target can't be changed; make a new assessment."),
            ("change_type", "The type can't be changed; make a new assessment."),
            ("status", "Change the status with the status action."),
        ):
            if field in data:
                errors[field] = [message]
    if "title" in data or creating:
        title = text_value(data.get("title"), "title", errors)
        if not title:
            errors.setdefault("title", ["Enter a title."])
        elif len(title) > NAME_MAX:
            errors["title"] = [f"Use {NAME_MAX} characters or fewer."]
        else:
            values["title"] = title
    for field in ("description", "findings"):
        if field in data:
            text = text_value(data.get(field), field, errors)
            if len(text) > TEXT_MAX:
                errors[field] = [f"Use {TEXT_MAX} characters or fewer."]
            elif field not in errors:
                values[field] = text
    if "owner" in data or creating:
        owner = data.get("owner") if "owner" in data else actor.id
        user = None
        if owner not in (None, ""):
            owner_id = whole_number(owner, 1)
            user = (
                get_user_model()
                .objects.filter(id=owner_id, membership__organization=org, membership__role__in=WRITE_ROLES)
                .first()
                if owner_id is not None
                else None
            )
            if user is None:
                errors["owner"] = ["Choose an Admin or Editor in this organization."]
        values["owner"] = user
    if "target_date" in data:
        raw = data["target_date"]
        if raw in (None, ""):
            values["target_date"] = None
        else:
            try:
                values["target_date"] = dates.parse_stored(raw)
            except ValueError as exc:
                values["target_date"] = None
                errors["target_date"] = [str(exc)]
    if "ticket_url" in data:
        url = text_value(data.get("ticket_url"), "ticket_url", errors)
        if "ticket_url" in errors:
            pass
        elif len(url) > NAME_MAX:
            errors["ticket_url"] = [f"Use {NAME_MAX} characters or fewer."]
        else:
            try:
                if url:
                    URLValidator(schemes=["http", "https"])(url)
                values["ticket_url"] = url
            except ValidationError:
                errors["ticket_url"] = ["Enter a full web address starting with http:// or https://."]
    if errors:
        raise FieldErrors(errors)
    return values


def _retirement_ack(org, target, change_type, status, previous, data):
    """The retirement acknowledgment for an assessment: a retirement moving to Ready for external execution is the point where the retirement
    is decided, so it takes the same acknowledgment and reason as retiring the record (and saves the reason on the
    assessment). Not needed when it is already Ready, the target is already Retired, or nothing active depends on it."""
    if (
        change_type != ChangeType.RETIREMENT
        or status != AssessmentStatus.READY
        or previous == AssessmentStatus.READY
        or target.lifecycle == Lifecycle.RETIRED
    ):
        return None
    return retirement_check(org, target, data)


@transaction.atomic
def create_assessment(org, actor, data):
    """Save an assessment and compute its first snapshot."""
    values = _assessment_fields(org, actor, data, creating=True)
    acknowledged = _retirement_ack(org, values["target"], values["change_type"], values["status"], None, data)
    if acknowledged:
        values["retirement_ack_reason"] = acknowledged["reason"]
    assessment = ChangeAssessment.objects.create(organization=org, **values)
    snapshot = write_snapshot(org, actor, assessment)
    audit(
        org,
        actor,
        "assessment.created",
        assessment,
        {
            "target": str(assessment.target_id),
            "change_type": assessment.change_type,
            "status": assessment.status,
            **assessment_audit_values(assessment),
            **({"retirement_ack": acknowledged} if acknowledged else {}),
            "snapshot": str(snapshot.id),
            "nodes": snapshot.nodes.count(),
        },
    )
    return assessment


@transaction.atomic
def update_assessment(org, actor, assessment, data):
    """Edit text, owner, date and ticket. Never touches the snapshot or its staleness."""
    values = _assessment_fields(org, actor, data, creating=False)
    before = assessment_audit_values(assessment)
    for field, value in values.items():
        setattr(assessment, field, value)
    assessment.save()
    after = assessment_audit_values(assessment)
    audit(
        org, actor, "assessment.updated", assessment, {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    )
    return assessment


@transaction.atomic
def change_status(org, actor, assessment, data):
    """Move to another assessment status. Documentation, not approval: any status may follow any other."""
    status = data.get("status")
    if status not in AssessmentStatus.values:
        raise FieldErrors({"status": ["Choose draft, in review, ready, closed or canceled."]})
    if status == assessment.status:
        return assessment
    before = assessment.status
    acknowledged = _retirement_ack(org, assessment.target, assessment.change_type, status, before, data)
    assessment.status = status
    if acknowledged:
        assessment.retirement_ack_reason = acknowledged["reason"]
    assessment.save()
    changes = {"status": [before, status], **({"retirement_ack": acknowledged} if acknowledged else {})}
    audit(org, actor, "assessment.status_changed", assessment, changes)
    return assessment


@transaction.atomic
def refresh_snapshot(org, actor, assessment):
    """A new current snapshot from the catalog as it is now; the old one stays in history. The only way
    a stale flag is cleared: nothing refreshes by itself."""
    previous = AssessmentSnapshot.objects.filter(assessment=assessment, is_current=True).first()
    snapshot = write_snapshot(org, actor, assessment)
    assessment.save()  # touches updated_at, so the list shows recent activity
    audit(
        org,
        actor,
        "assessment.snapshot_refreshed",
        assessment,
        {
            "snapshot": str(snapshot.id),
            "previous": str(previous.id) if previous else None,
            "was_stale": bool(previous and previous.stale_since),
            "nodes": snapshot.nodes.count(),
        },
    )
    return snapshot
