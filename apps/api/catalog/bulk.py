"""Bulk edit and bulk delete on up to 200 selected records of one kind.

Bulk edit: owners and the IT steward, support team, department, lifecycle,
criticality, tags, custom fields and the next review date. Preview computes each record's before and after without
writing; apply runs the same plan in one transaction, each record through services.update_resource so it gets its own
resource.updated event (marked bulk) and the single-edit rules (field validation, the retirement check). A value a
record already holds is unchanged and leaves its review state alone. The next review date edits the latest
review. Apply is all or nothing: any row that can't take the change refuses the whole request (BulkRefused).

Bulk delete (Admin): preview says per record whether it can be deleted and what blocks it, by the same
reference rules as a single delete; apply deletes every record through services.delete_resource (its own
resource.deleted event marked bulk, attachments and exceptions removed and audited) or, if any is blocked, nothing."""

from functools import partial
from typing import Any

from django.db import transaction
from django.db.models import Count, F
from django.db.models.functions import Lower
from rest_framework import exceptions

from core import dates
from core.capabilities import AI_AGENTS, require_capability
from core.models import Membership, Role
from core.problems import Conflict, FieldErrors

from . import customfields, services, tags
from .models import (
    RECORD_KINDS,
    Attachment,
    Criticality,
    Department,
    Kind,
    Lifecycle,
    Person,
    QualityException,
    RecordReview,
    Relationship,
    Resource,
    Tag,
    Team,
)
from .state import ACTIVE_LIFECYCLES

MAX_IDS = 200
OWNER_FIELDS = ("business_owner", "technical_owner")
# Reference fields resolved to a row of this organization; people must be active.
REFERENCE_MODELS: dict[str, Any] = {
    "business_owner": Person,
    "technical_owner": Person,
    "support_team": Team,
    "department": Department,
}
REFERENCE_FIELDS = tuple(REFERENCE_MODELS)
CHOICE_FIELDS = ("lifecycle", "criticality")
# Fields a record review confirms (catalog/state.CONFIRMED_FIELDS): changing one away from the confirmed value means
# the record needs confirming again.
CONFIRMED = ("business_owner", "technical_owner", "support_team", "lifecycle", "criticality")
CF = customfields.KEY
ALLOWED = {*REFERENCE_FIELDS, *CHOICE_FIELDS, "it_steward", CF, "add_tags", "remove_tags", "next_review_date"}
NO_REVIEW = "This record has no review yet, so there is no next review date to change."
ARCHIVED = "Archived records can't be edited. Restore it first."
NAMES_IN_MESSAGE = 5


# Row error codes: `codes[i]` is the stable code of `errors[i]`, so a client keys off the code, not the words.
CODE_ARCHIVED = "archived"
CODE_RETIRE_DEPENDENTS = "retire_dependents"  # active records outside the selection depend on it
CODE_RETIRE_LOOP = "retire_dependency_loop"  # selected records depend on it in a loop
CODE_TOO_MANY_TAGS = "too_many_tags"
CODE_NO_REVIEW = "no_review"
CODE_REVIEW_DATE = "review_before_confirmation"
CODE_CONFLICT = "conflict"  # the record's own write was refused with another 409 (its message is in errors)
ROW_CODES = (
    CODE_ARCHIVED,
    CODE_RETIRE_DEPENDENTS,
    CODE_RETIRE_LOOP,
    CODE_TOO_MANY_TAGS,
    CODE_NO_REVIEW,
    CODE_REVIEW_DATE,
    CODE_CONFLICT,
)


class BulkRefused(FieldErrors):
    """Apply refused: at least one record can't take the change. Nothing was saved. `rows` says which and why:
    `errors` are messages with their `codes` (same order), `field_errors` the single-edit field errors a record's
    write was refused with."""

    def __init__(self, rows):
        super().__init__({"ids": ["Some selected records can't take this change. Nothing was saved."]})
        self.extra = {"rows": rows}


def _role(org, actor):
    return Membership.objects.get(user=actor, organization=org).role


def _require_write(org, actor):
    if _role(org, actor) not in services.WRITE_ROLES:
        raise exceptions.PermissionDenied("Editors and Admins can make changes; your role is Viewer.")


def _require_admin(org, actor):
    if _role(org, actor) != Role.ADMIN:
        raise exceptions.PermissionDenied("Only Admins can delete records.")


def _kind_and_ids(org, data, errors):
    """The kind and the de-duplicated IDs from a request body, adding problems to `errors`. An AI agent selection
    needs the ai_agents capability (403 problem naming it)."""
    kind = data.get("kind")
    if kind not in RECORD_KINDS:
        errors["kind"] = ["Choose application, component or ai_agent."]
        kind = None
    elif kind == Kind.AI_AGENT:
        require_capability(org, AI_AGENTS)
    ids = data.get("ids")
    if not isinstance(ids, list) or not ids:
        errors["ids"] = ["Select at least one record."]
    elif len(ids) > MAX_IDS:
        errors["ids"] = [f"Select {MAX_IDS} records or fewer."]
    elif not all(isinstance(i, str) and services.is_uuid(i) for i in ids):
        errors["ids"] = ["Send record IDs as text."]
    else:
        return kind, list(dict.fromkeys(ids))
    return kind, []


def _custom_fields(org, kind, raw, errors, lock):
    """{name: cleaned value or None} for the custom fields to set or clear, validated exactly as a single edit
    validates them (customfields.clean: known and applicable names, type, choices, required on clear; under FOR SHARE
    on the definitions when applying). Field errors are keyed custom_field_data.<name>, as on a single edit."""
    if not isinstance(raw, dict) or not raw:
        errors[CF] = ["Send custom fields as an object of field names and values."]
        return None
    if kind is None:
        return None
    found: dict[str, list[str]] = {}
    customfields.clean(org, kind, None, {CF: raw}, found, lock=lock)
    if found:
        errors.update(found)
        return None
    fields = {cf.name: cf for cf in customfields.for_kind(org, kind)}
    return {name: customfields.clean_value(fields[name], value) for name, value in raw.items()}


def parse(org, data, lock=False):
    """(kind, ids, changes) from a request body, or FieldErrors. References are resolved to rows here, so another
    organization's ID is simply unknown."""
    errors: dict[str, list[str]] = {}
    kind, ids = _kind_and_ids(org, data, errors)
    raw = data.get("changes")
    changes: dict = {}
    if not isinstance(raw, dict) or not raw:
        errors["changes"] = ["Choose at least one change."]
    else:
        unknown = sorted(set(raw) - ALLOWED)
        if unknown:
            errors["changes"] = [f"Bulk edit can't change {', '.join(unknown)}."]
        for field, model in REFERENCE_MODELS.items():
            if field not in raw:
                continue
            value = raw[field]
            obj = None
            if value not in (None, ""):
                obj = model.objects.filter(organization=org, id=value).first() if services.is_uuid(value) else None
                if obj is None:
                    errors[field] = ["Choose an existing record."]
                elif isinstance(obj, Person) and not obj.active:
                    errors[field] = ["Inactive people can't be owners."]
            changes[field] = obj
        if "it_steward" in raw:
            value = raw["it_steward"]
            user = None
            if value not in (None, ""):
                user = services.steward_user(org, value)
                if user is None:
                    errors["it_steward"] = [services.STEWARD_MESSAGE]
            changes["it_steward"] = user
        if "lifecycle" in raw:
            if raw["lifecycle"] not in Lifecycle.values:
                errors["lifecycle"] = ["Choose a lifecycle status."]
            else:
                changes["lifecycle"] = raw["lifecycle"]
        if "criticality" in raw:
            value = raw["criticality"] or ""
            if value != "" and value not in Criticality.values:
                errors["criticality"] = ["Choose low, medium, high or critical."]
            else:
                changes["criticality"] = value
        if CF in raw:
            custom = _custom_fields(org, kind, raw[CF], errors, lock)
            if custom is not None:
                changes[CF] = custom
        for field in ("add_tags", "remove_tags"):
            if field in raw:
                names = tags.clean(raw[field], errors, field)
                if names is not None:
                    changes[field] = names
        if "next_review_date" in raw:
            try:
                changes["next_review_date"] = dates.parse_stored(raw["next_review_date"])
            except ValueError as exc:
                errors["next_review_date"] = [str(exc)]
        if not changes and "changes" not in errors and not errors:
            errors["changes"] = ["Choose at least one change."]
    if errors:
        raise FieldErrors(errors)
    return kind, ids, changes


def _ref(obj):
    return {"id": str(obj.id), "name": obj.name} if obj else None


def _user_ref(user):
    return {"id": str(user.id), "name": user.get_full_name() or user.email} if user else None


def _final_tags(current, add, remove):
    gone = {n.lower() for n in remove}
    names = [n for n in current if n.lower() not in gone]
    return names + [n for n in add if n.lower() not in {x.lower() for x in names}]


def _confirmed_value(field, changes):
    """A changed field's new value as RecordReview.confirmed_values stores it."""
    value = changes[field]
    if field in REFERENCE_MODELS:
        return str(value.id) if value else None
    return value or None


def _names(records):
    shown = ", ".join(r.name for r in records[:NAMES_IN_MESSAGE])
    more = len(records) - NAMES_IN_MESSAGE
    return shown + (f" and {more} more" if more > 0 else "")


def _retirements(org, by_id, retiring):
    """For records about to be Retired: ({id: (code, error)}, order). The single-edit rule refuses retiring a record
    that active records depend on unless acknowledged with a reason, which a bulk edit doesn't take; so a record with
    an active dependent outside the selection is refused, naming them. Dependents in the selection are retired by the
    same edit, so they don't block, and apply retires them first (`order`: dependents before what they depend on).
    Records that depend on each other in a loop can't be ordered and are refused. One query for the whole selection."""
    if not retiring:
        return {}, []
    links = (
        Relationship.objects.filter(organization=org, type__is_dependency=True, target_id__in=retiring)
        .exclude(source_id=F("target_id"))
        .select_related("source")
        .order_by("source__name", "source_id")
    )
    outside: dict = {rid: {} for rid in retiring}
    inside: dict = {rid: set() for rid in retiring}  # target -> dependents in the selection still to retire
    for link in links:
        target, source = str(link.target_id), link.source
        if source.archived_at is not None or source.lifecycle not in ACTIVE_LIFECYCLES:
            continue
        if str(source.id) in retiring:
            inside[target].add(str(source.id))
        else:
            outside[target].setdefault(source.id, source)
    errors = {}
    for rid, found in outside.items():
        if found:
            active = list(found.values())
            errors[rid] = (
                CODE_RETIRE_DEPENDENTS,
                f"Still depended on by {len(active)} active {'record' if len(active) == 1 else 'records'}: "
                f"{_names(active)}. Retire it on its own page, where you can acknowledge them, or select them too.",
            )
    # Dependents first (Kahn's algorithm over the selection, in selection order for ties).
    waiting = {rid: set(deps) for rid, deps in inside.items()}
    order: list[str] = []
    ready = [rid for rid in retiring if not waiting[rid]]
    while ready:
        rid = ready.pop(0)
        order.append(rid)
        for other in retiring:
            if rid in waiting[other]:
                waiting[other].discard(rid)
                if not waiting[other] and other not in order and other not in ready:
                    ready.append(other)
    for rid in retiring:
        if rid not in order and rid not in errors:
            loop = [by_id[d] for d in sorted(inside[rid] - set(order), key=lambda d: by_id[d].name)]
            errors[rid] = (
                CODE_RETIRE_LOOP,
                f"Depended on by selected records that can't be retired before it: {_names(loop)} (they depend on "
                "each other in a loop). Retire them one at a time on their own pages.",
            )
    return errors, order


def _refuse(errors: list[str], codes: list[str], code: str, message: str) -> None:
    errors.append(message)
    codes.append(code)


def plan(org, kind, ids, changes, lock=False):
    """One row per selected record, in the order selected. Raises NotFound unless every ID is a record of this kind
    in this organization. Rows carry the record and the values to apply under `_`-prefixed keys."""
    qs = Resource.objects.filter(organization=org, kind=kind, id__in=ids).select_related(
        "business_owner", "technical_owner", "support_team", "department", "it_steward"
    )
    if lock:
        # Lock the Resource rows, in ID order so two bulk edits can't deadlock. Apply changes tags and review dates
        # beside update_resource; holding the rows keeps a concurrent If-Match writer (core/drf.check_if_match, which
        # locks the same row) from reading a revision this transaction is about to change.
        # FOR NO KEY UPDATE: a bulk edit never changes key columns, so rows that reference these (reviews, links)
        # can still be inserted meanwhile; a bulk delete takes FOR UPDATE.
        qs = qs.select_for_update(no_key=True, of=("self",)).order_by("id")
    by_id = {str(r.id): r for r in qs}
    if len(by_id) != len(ids):
        raise exceptions.NotFound("Some of the selected records don't exist or you don't have access.")
    latest: dict = {}
    for rv in RecordReview.objects.filter(organization=org, resource_id__in=ids).order_by(
        "resource_id", "-confirmation_date", "-created_at"
    ):
        latest.setdefault(str(rv.resource_id), rv)
    current_tags = tags.names_for(r.id for r in by_id.values())
    adding = changes.get("add_tags", [])
    spelling = {}
    if adding:
        existing = Tag.objects.filter(organization=org).annotate(lowered=Lower("name"))
        spelling = {t.name.lower(): t.name for t in existing.filter(lowered__in=[n.lower() for n in adding])}
    retiring: list[str] = []
    if changes.get("lifecycle") == Lifecycle.RETIRED:
        retiring = [rid for rid in ids if by_id[rid].lifecycle != Lifecycle.RETIRED]
    retire_errors, retire_order = _retirements(org, by_id, retiring)
    position = {rid: n for n, rid in enumerate(retire_order)}
    custom = changes.get(CF, {})
    fields = {cf.name: cf for cf in customfields.for_kind(org, kind)} if custom else {}
    rows = []
    for rid in ids:
        r = by_id[rid]
        review = latest.get(rid)
        diff: dict = {}
        errors: list[str] = []
        codes: list[str] = []
        refuse = partial(_refuse, errors, codes)
        if r.archived_at is not None:
            refuse(CODE_ARCHIVED, ARCHIVED)
        for field in REFERENCE_FIELDS:
            if field in changes and getattr(r, field + "_id") != (changes[field].id if changes[field] else None):
                diff[field] = {"before": _ref(getattr(r, field)), "after": _ref(changes[field])}
        if "it_steward" in changes:
            wanted = changes["it_steward"]
            if r.it_steward_id != (wanted.id if wanted else None):
                diff["it_steward"] = {"before": _user_ref(r.it_steward), "after": _user_ref(wanted)}
        for field in CHOICE_FIELDS:
            if field in changes and (getattr(r, field) or "") != changes[field]:
                diff[field] = {"before": getattr(r, field) or None, "after": changes[field] or None}
        if rid in retire_errors:
            refuse(*retire_errors[rid])
        cf_values: dict = {}
        stored = r.custom_field_data or {}
        for name, value in custom.items():
            if stored.get(name) != value:
                cf_values[name] = value
                diff[customfields.PREFIX + name] = {
                    "before": customfields.shown(fields[name], stored.get(name)),
                    "after": customfields.shown(fields[name], value),
                }
        final = None
        if "add_tags" in changes or "remove_tags" in changes:
            before = current_tags.get(r.id, [])
            add = [spelling.get(n.lower(), n) for n in adding]
            final = _final_tags(before, add, changes.get("remove_tags", []))
            if len(final) > tags.PER_RECORD_MAX:
                refuse(CODE_TOO_MANY_TAGS, f"This would give the record more than {tags.PER_RECORD_MAX} tags.")
                final = None
            elif {n.lower() for n in final} != {n.lower() for n in before}:
                diff["tags"] = {"before": before, "after": sorted(final, key=str.lower)}
            else:
                final = None
        new_review: RecordReview | None = None
        if "next_review_date" in changes:
            wanted_date = changes["next_review_date"]
            if review is None:
                refuse(CODE_NO_REVIEW, NO_REVIEW)
            elif wanted_date <= review.confirmation_date:
                refuse(
                    CODE_REVIEW_DATE,
                    f"The next review must be after the last confirmation ({review.confirmation_date}).",
                )
            elif wanted_date != review.next_review_date:
                diff["next_review_date"] = {
                    "before": review.next_review_date.isoformat(),
                    "after": wanted_date.isoformat(),
                }
                new_review = review
        # A changed confirmed field that no longer matches what the last review confirmed.
        confirmed = review.confirmed_values if review else {}
        will_need = review is not None and any(
            field in diff and confirmed.get(field) != _confirmed_value(field, changes) for field in CONFIRMED
        )
        rows.append(
            {
                "id": rid,
                "name": r.name,
                "changes": diff,
                "unchanged": not diff and not errors,
                "will_need_confirmation": will_need,
                "errors": errors,
                "codes": codes,
                "_resource": r,
                "_tags": final,
                "_review": new_review,
                "_custom": cf_values,
                "_order": position.get(rid, -1),
            }
        )
    return rows


def public(row):
    return {k: v for k, v in row.items() if not k.startswith("_")}


def summary(rows):
    return {
        "total": len(rows),
        "changed": sum(1 for r in rows if r["changes"] and not r["errors"]),
        "unchanged": sum(1 for r in rows if r["unchanged"]),
        "errors": sum(1 for r in rows if r["errors"]),
        "will_need_confirmation": sum(1 for r in rows if r["will_need_confirmation"] and not r["errors"]),
    }


def preview(org, actor, data):
    _require_write(org, actor)
    kind, ids, changes = parse(org, data)
    rows = plan(org, kind, ids, changes)
    return {"kind": kind, "summary": summary(rows), "rows": [public(r) for r in rows]}


def _values(row, changes):
    """What services.update_resource gets for one row: only the fields that change on it."""
    diff = row["changes"]
    values: dict = {f: str(changes[f].id) if changes[f] else None for f in REFERENCE_FIELDS if f in diff}
    if "it_steward" in diff:
        values["it_steward"] = str(changes["it_steward"].id) if changes["it_steward"] else None
    for field in CHOICE_FIELDS:
        if field in diff:
            values[field] = changes[field]
    if row["_custom"]:
        values[CF] = row["_custom"]
    if row["_tags"] is not None:
        values["tags"] = row["_tags"]
    return values


def _refused(row, errors=None, codes=None, field_errors=None):
    return {
        "id": row["id"],
        "name": row["name"],
        "errors": errors or [],
        "codes": codes or [],
        "field_errors": field_errors or {},
    }


@transaction.atomic
def apply(org, actor, data):
    _require_write(org, actor)
    kind, ids, changes = parse(org, data, lock=True)
    rows = plan(org, kind, ids, changes, lock=True)
    if any(r["errors"] for r in rows):
        raise BulkRefused([_refused(r, r["errors"], r["codes"]) for r in rows if r["errors"]])
    refused = []
    # Retiring records go dependents first, so each one's own retirement check sees them retired already.
    for row in sorted(rows, key=lambda r: r["_order"]):
        if row["unchanged"]:
            continue
        diff = row["changes"]
        extra: dict = {"bulk": True}
        if "next_review_date" in diff:
            change = diff["next_review_date"]
            extra["next_review_date"] = [change["before"], change["after"]]
            RecordReview.objects.filter(id=row["_review"].id).update(next_review_date=changes["next_review_date"])
        try:
            # update_resource is atomic itself, so a refused row rolls back to its savepoint and the loop goes on
            # to collect every refusal; the whole transaction is then rolled back by BulkRefused.
            services.update_resource(org, actor, row["_resource"], _values(row, changes), audit_extra=extra)
        except FieldErrors as e:
            refused.append(_refused(row, field_errors=e.errors))
        except Conflict as e:
            # The record's own retirement check caught a dependent recorded since the plan: the same refusal.
            retire = e.extra.get("type") == services.RETIREMENT_NEEDS_ACK
            refused.append(_refused(row, [str(e.detail)], [CODE_RETIRE_DEPENDENTS if retire else CODE_CONFLICT]))
    if refused:
        order = {rid: n for n, rid in enumerate(ids)}
        raise BulkRefused(sorted(refused, key=lambda r: order[r["id"]]))
    return {
        "kind": kind,
        "updated": sum(1 for r in rows if not r["unchanged"]),
        "unchanged": sum(1 for r in rows if r["unchanged"]),
        "summary": summary(rows),
        "rows": [public(r) for r in rows],
    }


# ---- bulk delete --------------------------------------------------------------------------------------------


def parse_delete(org, data):
    errors: dict[str, list[str]] = {}
    unknown = sorted(set(data) - {"kind", "ids"})
    if unknown:
        errors["non_field"] = [f"Bulk delete doesn't take {', '.join(unknown)}."]
    kind, ids = _kind_and_ids(org, data, errors)
    if errors:
        raise FieldErrors(errors)
    return kind, ids


def _owned_counts(model, org, ids):
    rows = model.objects.filter(organization=org, resource_id__in=ids).values("resource_id").annotate(n=Count("id"))
    return {str(row["resource_id"]): row["n"] for row in rows}


def delete_plan(org, kind, ids, lock=False):
    """One row per selected record, in the order selected: whether it can be deleted, the references that block it
    (services.references_to_many, the single delete's rule, one query per referencing table), and the attachments and
    data-quality exceptions that would go with it. NotFound unless every ID is a record of this kind here."""
    qs = Resource.objects.filter(organization=org, kind=kind, id__in=ids)
    if lock:
        qs = qs.select_for_update(of=("self",)).order_by("id")  # ID order, as bulk edit, so the two can't deadlock
    by_id = {str(r.id): r for r in qs}
    if len(by_id) != len(ids):
        raise exceptions.NotFound("Some of the selected records don't exist or you don't have access.")
    refs = services.references_to_many(by_id.values())
    attachments = _owned_counts(Attachment, org, ids)
    exceptions_ = _owned_counts(QualityException, org, ids)
    rows = []
    for rid in ids:
        r = by_id[rid]
        blocking = refs[r.pk]
        errors = []
        if blocking:
            errors.append(
                f"It has {services.references_phrase(blocking)}, so it can't be deleted. "
                + services.RESOURCE_DELETE_INSTEAD
            )
        rows.append(
            {
                "id": rid,
                "name": r.name,
                "can_delete": not blocking,
                "blocking_references": blocking,
                "removes": {"attachments": attachments.get(rid, 0), "quality_exceptions": exceptions_.get(rid, 0)},
                "errors": errors,
                "_resource": r,
            }
        )
    return rows


def delete_summary(rows):
    return {
        "total": len(rows),
        "deletable": sum(1 for r in rows if r["can_delete"]),
        "blocked": sum(1 for r in rows if not r["can_delete"]),
    }


def delete_preview(org, actor, data):
    _require_admin(org, actor)
    kind, ids = parse_delete(org, data)
    rows = delete_plan(org, kind, ids)
    return {"kind": kind, "summary": delete_summary(rows), "rows": [public(r) for r in rows]}


@transaction.atomic
def delete_apply(org, actor, data):
    """All or nothing: 409 listing each blocked record's `blocking_references` when any is referenced; otherwise
    each record goes through services.delete_resource with the bulk marker on its resource.deleted event."""
    _require_admin(org, actor)
    kind, ids = parse_delete(org, data)
    rows = delete_plan(org, kind, ids, lock=True)
    blocked = [r for r in rows if not r["can_delete"]]
    if blocked:
        raise Conflict(
            f"{len(blocked)} of the selected records can't be deleted because other records reference them. "
            "Nothing was deleted.",
            extra={
                "rows": [
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "blocking_references": r["blocking_references"],
                        "errors": r["errors"],
                    }
                    for r in blocked
                ]
            },
        )
    for row in rows:
        services.delete_resource(org, actor, row["_resource"], audit_extra={"bulk": True})
    return {"kind": kind, "deleted": len(rows), "rows": [{"id": r["id"], "name": r["name"]} for r in rows]}
