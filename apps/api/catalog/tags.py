"""Tags on records: plain names, unique per organization ignoring case, created on first use. The record
form, bulk edit and import all set them through here."""

from django.db.models.functions import Lower

from . import snapshots
from .models import ResourceTag, Tag

NAME_MAX = 60
PER_RECORD_MAX = 20


def normalise(name: str) -> str:
    """A tag name as it is stored: runs of whitespace collapsed to one space, trimmed ("Foo  Bar" is "Foo Bar")."""
    return " ".join(name.split())


def clean(names, errors, field="tags"):
    """Validated, de-duplicated names (first spelling wins), or None with errors[field] set."""
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        errors[field] = ["Send tags as a list of names."]
        return None
    out: dict[str, str] = {}
    for name in names:
        name = normalise(name)
        if not name:
            continue
        if len(name) > NAME_MAX:
            errors[field] = [f"Keep each tag to {NAME_MAX} characters."]
            return None
        out.setdefault(name.lower(), name)
    if len(out) > PER_RECORD_MAX:
        errors[field] = [f"Use {PER_RECORD_MAX} tags or fewer on one record."]
        return None
    return list(out.values())


def resolve(org, names):
    """Tag rows for these names, creating the missing ones (an existing tag keeps its spelling)."""
    lowered = [n.lower() for n in names]
    existing: dict[str, Tag] = {
        t.name.lower(): t for t in Tag.objects.filter(organization=org).annotate(l=Lower("name")).filter(l__in=lowered)
    }
    tags = []
    for name in names:
        if name.lower() not in existing:
            existing[name.lower()], _ = Tag.objects.get_or_create(organization=org, name=name)
        tags.append(existing[name.lower()])
    return tags


def names_of(resource):
    return sorted((t.name for t in Tag.objects.filter(links__resource=resource)), key=str.lower)


def names_for(resource_ids):
    """{resource id: [tag names]} in one query, for lists and exports."""
    out: dict = {}
    for rid, name in ResourceTag.objects.filter(resource_id__in=list(resource_ids)).values_list(
        "resource_id", "tag__name"
    ):
        out.setdefault(rid, []).append(name)
    return {rid: sorted(names, key=str.lower) for rid, names in out.items()}


def set_tags(org, resource, names):
    """Make the record's tags exactly `names` (already cleaned). Returns (before, after) name lists, sorted."""
    before = names_of(resource)
    snapshots.touch(resource)  # the bulk insert below sends no signal; the record's snapshot lists its tags
    wanted = resolve(org, names)
    ResourceTag.objects.filter(resource=resource).exclude(tag__in=wanted).delete()
    have = set(ResourceTag.objects.filter(resource=resource).values_list("tag_id", flat=True))
    ResourceTag.objects.bulk_create(
        [ResourceTag(organization=org, resource=resource, tag=t) for t in wanted if t.id not in have]
    )
    return before, sorted((t.name for t in wanted), key=str.lower)


def add_remove(org, resource, add=(), remove=()):
    """Bulk edit: add some tags and remove others, keeping the rest."""
    current = names_of(resource)
    gone = {n.lower() for n in remove}
    names = [n for n in current if n.lower() not in gone]
    names += [n for n in add if n.lower() not in {x.lower() for x in names}]
    return set_tags(org, resource, names)
