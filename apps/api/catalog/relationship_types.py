"""Relationship types: per organization, seeded with the
three core types when the organization is created, and by migration catalog 0033 for the organizations that existed
before. Admins add more, and edit or delete them; every role reads them (catalog/relationship_type_viewsets.py).

Rules:
- `slug` is a relationship's `type` on the wire ("depends_on"); it is set once, on create (derived from the name when
  not given), and never changes: link confirmations, filters, imports and saved signatures key on it.
- A core type (depends_on, implements, replaces) can't be deleted, and its `is_dependency` can't change: the endpoint
  matrix, the `implements` data-flow rules and the `replaces` cycle rule are written against those three. An Admin may
  change its name, inverse name and description (the labels the UI shows).
- An added type follows the `depends_on` endpoint rules (any record at either end; one record links to itself only
  through two different instances). `is_dependency` decides whether dependency traversal, retirement checks,
  affected sets and staleness follow it; changing it on a type in use flags the open assessments it could change.
- A type in use can't be deleted: the 409 names how many relationships use it.

Seeding is part of creating the organization (like its software categories), so it writes no audit event."""

import re

from django.db import IntegrityError, transaction
from django.db.models import Count
from django.db.models.signals import post_save

from core.models import Organization
from core.problems import Conflict, FieldErrors

from .models import Relationship, RelationshipKind, RelationshipType

# (slug, name, inverse name, description, is_dependency). Edit for new organizations only; migration 0033 keeps its
# own frozen copy, and existing organizations keep their labels.
CORE_TYPES = (
    (
        RelationshipKind.DEPENDS_ON.value,
        "Depends on",
        "Is needed by",
        "The source needs the target to work.",
        True,
    ),
    (
        RelationshipKind.IMPLEMENTS.value,
        "Implements",
        "Is implemented by",
        "A component that carries out an integration's data flow.",
        False,
    ),
    (
        RelationshipKind.REPLACES.value,
        "Replaces",
        "Is replaced by",
        "The source takes over from the target, which is being retired.",
        False,
    ),
)
CORE_SLUGS = frozenset(slug for slug, *_ in CORE_TYPES)

SLUG_MAX = 50
NAME_MAX = 100
DESCRIPTION_MAX = 200
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
SLUG_MESSAGE = "Use lower-case letters, digits and underscores, starting with a letter."
FIELDS = ("slug", "name", "inverse_name", "description", "is_dependency")


def seed(org) -> int:
    """Add the core types the organization doesn't have yet (by slug). Returns how many."""
    have = set(RelationshipType.objects.filter(organization=org).values_list("slug", flat=True))
    rows = [
        RelationshipType(
            organization=org,
            slug=slug,
            name=name,
            inverse_name=inverse,
            description=description,
            is_dependency=is_dependency,
            is_core=True,
        )
        for slug, name, inverse, description, is_dependency in CORE_TYPES
        if slug not in have
    ]
    RelationshipType.objects.bulk_create(rows)
    return len(rows)


def _on_organization_saved(sender, instance, created=False, raw=False, **kwargs):
    if created and not raw:
        seed(instance)


def connect() -> None:
    """Called once from CatalogConfig.ready: every new organization starts with the core types."""
    post_save.connect(_on_organization_saved, sender=Organization, dispatch_uid="catalog-seed-relationship-types")


# ---- reads ----------------------------------------------------------------------------------------------------------


def by_slug(org, slug) -> RelationshipType | None:
    if not isinstance(slug, str) or not slug:
        return None
    return RelationshipType.objects.filter(organization=org, slug=slug).first()


def for_link(org, slug) -> RelationshipType | None:
    """The organization's type with this slug, locked FOR KEY SHARE until the caller's transaction ends, for a link
    about to use it. The link's foreign key is checked only at commit (deferred), so without the lock an Admin's
    delete_type could commit between this read and the link's commit and turn the save into a 500. With it, a delete
    that got there first makes this return None (the type is gone: a field error), and one that comes after waits for
    the link and then counts it (409)."""
    if not isinstance(slug, str) or not slug:
        return None
    table = RelationshipType._meta.db_table
    rows = RelationshipType.objects.raw(
        f"SELECT * FROM {table} WHERE organization_id = %s AND slug = %s FOR KEY SHARE",  # noqa: S608 - fixed table
        [org.id, slug],
    )
    return next(iter(rows), None)


def usage(org) -> dict:
    """{type_id: number of relationships of that type}, in one query."""
    rows = Relationship.objects.filter(organization=org).values("type_id").annotate(n=Count("pk"))
    return {row["type_id"]: row["n"] for row in rows}


def phrase_of(name: str, slug: str = "") -> str:
    """How a link reads between two names, from its type's current name: "Depends on" reads "depends on". The first
    letter is lower-cased only when the second isn't a capital, so "SAML trust for" stays as written. Every place that
    puts a link in words (audit log, digest, staleness reasons) uses this, so a renamed core type reads the same
    everywhere."""
    if not name:
        return slug.replace("_", " ")
    if len(name) > 1 and name[1].isupper():
        return name
    return name[:1].lower() + name[1:]


def phrase(rtype) -> str:
    return phrase_of(rtype.name, rtype.slug)


# ---- writes ---------------------------------------------------------------------------------------------------------


def _audited(rtype) -> dict:
    return {f: getattr(rtype, f) for f in FIELDS}


def _text(data, field, limit, errors, required=False):
    value = data.get(field)
    if value is not None and not isinstance(value, str):
        errors[field] = ["Send text."]
        return ""
    value = (value or "").strip()
    if required and not value:
        errors[field] = ["Enter a name."]
    elif len(value) > limit:
        errors[field] = [f"Use {limit} characters or fewer."]
    return value


def _slug_from(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:SLUG_MAX].rstrip("_")
    return slug if slug[:1].isalpha() else f"type_{slug}"[:SLUG_MAX].rstrip("_")


def _values(org, data, rtype=None) -> dict:
    """Validate a create (rtype None) or a PATCH; returns the fields to set."""
    errors: dict[str, list[str]] = {}
    values: dict = {}
    if "name" in data or rtype is None:
        name = _text(data, "name", NAME_MAX, errors, required=True)
        if name and "name" not in errors:
            clash = RelationshipType.objects.filter(organization=org, name__iexact=name)
            if rtype is not None:
                clash = clash.exclude(id=rtype.id)
            other = clash.first()
            if other is not None:
                errors["name"] = [f"A relationship type named {other.name} already exists."]
        values["name"] = name
    for field in ("inverse_name", "description"):
        if field in data:
            values[field] = _text(data, field, NAME_MAX if field == "inverse_name" else DESCRIPTION_MAX, errors)
    if "slug" in data and rtype is not None:
        if data["slug"] != rtype.slug:
            errors["slug"] = ["The slug can't be changed: relationships, filters and imports refer to it."]
    elif rtype is None:
        given = data.get("slug")
        derived = given in (None, "")
        if derived:
            slug = _slug_from(values.get("name") or "")
        elif not isinstance(given, str):
            errors["slug"] = ["Send text."]
            slug = ""
        else:
            slug = given.strip()
        if "slug" not in errors and slug:
            if len(slug) > SLUG_MAX:
                errors["slug"] = [f"Use {SLUG_MAX} characters or fewer."]
            elif not SLUG_PATTERN.match(slug):
                errors["slug"] = [SLUG_MESSAGE]
            elif RelationshipType.objects.filter(organization=org, slug=slug).exists():
                if derived:  # the user sent only the name: say so on the field they sent
                    errors.setdefault("name", []).append(
                        f"This name makes the slug {slug}, which another type has; give a different name or a slug."
                    )
                else:
                    errors["slug"] = [f"A relationship type with the slug {slug} already exists."]
        elif "slug" not in errors and "name" not in errors:
            errors["slug"] = [SLUG_MESSAGE]
        values["slug"] = slug
    if "is_dependency" in data:
        flag = data["is_dependency"]
        if not isinstance(flag, bool):
            errors["is_dependency"] = ["Send true or false."]
        elif rtype is not None and rtype.is_core and flag != rtype.is_dependency:
            errors["is_dependency"] = [
                f"{rtype.name} is a core type; whether it counts as a dependency can't be changed."
            ]
        else:
            values["is_dependency"] = flag
    if "is_core" in data and data["is_core"] != (rtype.is_core if rtype is not None else False):
        errors["is_core"] = ["Core types are built in; a type can't be made core or stop being core."]
    if errors:
        raise FieldErrors(errors)
    return values


def _save(rtype):
    """Save; a concurrent request that took the slug or name first is a field error, not a 500."""
    try:
        with transaction.atomic():
            rtype.save()
    except IntegrityError as e:
        field = "slug" if "unique_slug" in str(e) else "name"
        raise FieldErrors({field: [f"A relationship type with this {field} already exists."]}) from None


@transaction.atomic
def create_type(org, actor, data):
    from core import limits

    from .services import audit

    rtype = RelationshipType(organization=org, is_core=False, **_values(org, data))
    limits.check_room(org, limits.OTHER_RECORDS)
    _save(rtype)
    audit(org, actor, "relationship_type.created", rtype, _audited(rtype))
    return rtype


@transaction.atomic
def update_type(org, actor, rtype, data):
    from . import staleness
    from .services import audit

    rtype = RelationshipType.objects.select_for_update().get(id=rtype.id, organization=org)
    before = _audited(rtype)
    for field, value in _values(org, data, rtype).items():
        setattr(rtype, field, value)
    after = _audited(rtype)
    changes = {k: [before[k], after[k]] for k in FIELDS if before[k] != after[k]}
    if not changes:  # an edit that changes nothing writes nothing (as attachments, renewals, quality exceptions)
        return rtype
    _save(rtype)
    audit(org, actor, "relationship_type.updated", rtype, {"slug": rtype.slug, "name": rtype.name, **changes})
    if "is_dependency" in changes:
        staleness.relationship_type_changed(org, actor, rtype)
    return rtype


@transaction.atomic
def delete_type(org, actor, rtype):
    """Refused for a core type, and for a type any relationship uses (409, with the count)."""
    from .services import audit

    rtype = RelationshipType.objects.select_for_update().get(id=rtype.id, organization=org)
    if rtype.is_core:
        raise Conflict(
            f"{rtype.name} is a core relationship type and can't be deleted.",
            extra={"type": "/problems/core-relationship-type"},
        )
    used = Relationship.objects.filter(organization=org, type=rtype).count()
    if used:
        uses = "1 relationship uses it" if used == 1 else f"{used} relationships use it"
        raise Conflict(
            f"{rtype.name} can't be deleted: {uses}. Change their type or remove them first.",
            extra={"blocking_references": {"relationships": used}},
        )
    audit(org, actor, "relationship_type.deleted", rtype, {"slug": rtype.slug, "name": rtype.name})
    rtype.delete()
