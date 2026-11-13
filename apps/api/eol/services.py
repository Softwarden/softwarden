"""Writes to end-of-life tracking (validated, scoped to the organization and audited in one transaction).

- `track(org, actor, record, data)`: set or change a record's product, version and cycle (PATCH .../end-of-life).
  `product` is required the first time. It must be a product (or an alias of one) in the cached product list; while
  no list has been read (`cache.list_cached`: the first refresh hasn't run, or an air-gapped install hasn't imported a
  list) any well-formed name is accepted, and its status stays unknown until the data arrives. A product whose releases
  aren't cached yet has a refresh queued for it (eol/cache.request_product): never fetched in the request. Two people
  tracking the same record at once: the second gets 409 and tries again.
- `track_instance(org, actor, instance, data)`: one application instance's own version or cycle. Its product is the
  application's, so the application must be tracked first (409 otherwise).
- `untrack(org, actor, record)`: removes the record's tracking and its instances'. `untrack_instance` removes one.

`cycle` empty or null means "derive it from the version" (eol/status.derive_cycle); a cycle given explicitly must be
one of the product's cached releases when they are cached. Changing a record's product moves its instance rows to the
new product; an explicit cycle (the record's or an instance's) is kept only when the new product's cached releases list
it, and is otherwise cleared and derived again.

Audit actions: tracked_version.created, tracked_version.updated, tracked_version.deleted, with the record (or
instance's record) as the related target, so the record's History shows them."""

from __future__ import annotations

import re
from typing import Any

from django.db import IntegrityError, transaction

from catalog.models import ApplicationInstance, Kind, Resource
from catalog.services import audit
from core.problems import Conflict, FieldErrors

from . import cache
from .models import CYCLE_MAX, PRODUCT_MAX, VERSION_MAX, EolProduct, EolRelease, TrackedVersion
from .status import derive_cycle

TRACKED_KINDS = (Kind.APPLICATION, Kind.COMPONENT)
PRODUCT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._+-]*$")
FIELDS = ("product", "version", "cycle")


def resolve_product(value: Any, errors: dict[str, list[str]]) -> str | None:
    """The canonical product name for `value` (a name or an alias, any case), or None with an error."""
    if not isinstance(value, str) or not value.strip():
        errors["product"] = ["Choose an endoflife.date product (GET /end-of-life/products)."]
        return None
    name = value.strip().lower()
    if len(name) > PRODUCT_MAX or not PRODUCT_PATTERN.match(name):
        errors["product"] = ["Use the product's name on endoflife.date, such as postgresql or windows-server."]
        return None
    if not cache.list_cached():
        return name  # no product list read yet: accepted, checked once the data arrives (data quality, status)
    listed = EolProduct.objects.filter(listed=True)
    if listed.filter(name=name).exists():
        return name
    aliased = listed.filter(aliases__contains=[name]).values_list("name", flat=True).first()
    if aliased:
        return aliased
    errors["product"] = [f"endoflife.date has no product {name!r}. Search the list (GET /end-of-life/products)."]
    return None


def _text(data: dict[str, Any], key: str, limit: int, errors: dict[str, list[str]]) -> str | None:
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        errors[key] = ["Enter text."]
        return None
    value = value.strip()
    if len(value) > limit:
        errors[key] = [f"Use {limit} characters or fewer."]
        return None
    return value


def resolve_cycle(product: str, value: str, errors: dict[str, list[str]]) -> str | None:
    """An explicit cycle as endoflife.date names it (any case), when the product's releases are cached; any text up to
    the column's length otherwise."""
    if not value:
        return ""
    fetched = EolProduct.objects.filter(name=product, releases_fetched_at__isnull=False).exists()
    if not fetched:
        return value
    names = list(EolRelease.objects.filter(product_id=product).values_list("name", flat=True))
    match = next((n for n in names if n.lower() == value.lower()), None)
    if match is None:
        shown = ", ".join(names[:8]) + (", ..." if len(names) > 8 else "")
        errors["cycle"] = [f"Choose one of the product's release cycles ({shown}), or leave it empty to derive it."]
        return None
    return match


def kept_cycle(product: str, cycle: str) -> str:
    """After a product change: the explicit cycle as the new product's cached releases name it, or "" (cleared) when
    they don't list it or aren't cached."""
    if not cycle:
        return ""
    names = EolRelease.objects.filter(product_id=product).values_list("name", flat=True)
    return next((n for n in names if n.lower() == cycle.lower()), "")


def _unknown_keys(data: dict[str, Any], allowed: tuple[str, ...]) -> None:
    extra = sorted(k for k in data if k not in allowed)
    if extra:
        raise FieldErrors({k: ["This field isn't accepted here."] for k in extra})


def _derive(row: TrackedVersion, application_version: str | None) -> None:
    if row.cycle_explicit:
        return
    version, _ = cache.effective_version(row, application_version)
    row.cycle = derive_cycle(version, cache.release_names([row.product]).get(row.product, []))


def _application_version(record: Resource) -> str | None:
    if record.kind != Kind.APPLICATION:
        return None
    return Resource.objects.filter(pk=record.pk).values_list("application__version", flat=True).first()


def _values(row: TrackedVersion) -> dict[str, Any]:
    return {
        "record": str(row.resource_id),
        "instance": str(row.instance_id) if row.instance_id else None,
        "product": row.product,
        "version": row.version,
        "cycle": row.cycle,
        "cycle_explicit": row.cycle_explicit,
    }


def _changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    keep = {"record": after["record"], "instance": after["instance"], "product": after["product"]}
    return keep | {
        k: [before[k], after[k]] for k in ("product", "version", "cycle", "cycle_explicit") if before[k] != after[k]
    }


def _apply(row: TrackedVersion, data: dict[str, Any], errors: dict[str, list[str]], *, product_allowed: bool) -> None:
    if product_allowed and "product" in data:
        product = resolve_product(data["product"], errors)
        if product is not None:
            row.product = product
    if "version" in data:
        version = _text(data, "version", VERSION_MAX, errors)
        if version is not None:
            row.version = version
    if "cycle" in data:
        cycle = _text(data, "cycle", CYCLE_MAX, errors)
        if cycle is not None and row.product:
            cycle = resolve_cycle(row.product, cycle, errors)
        if cycle is not None:
            row.cycle, row.cycle_explicit = cycle, bool(cycle)
    elif product_allowed and "product" in data and row.cycle_explicit and not errors:
        # A new product: an explicit cycle its cached releases don't list is cleared, and the cycle derived again.
        row.cycle = kept_cycle(row.product, row.cycle)
        row.cycle_explicit = bool(row.cycle)


@transaction.atomic
def track(org, actor, record: Resource, data: dict[str, Any]) -> TrackedVersion:
    """Create or change the record's tracking (the record row: no instance)."""
    _unknown_keys(data, FIELDS)
    if record.kind not in TRACKED_KINDS:
        raise FieldErrors({"record": ["Only applications and components are tracked."]})
    row = (
        TrackedVersion.objects.select_for_update()
        .filter(organization=org, resource=record, instance__isnull=True)
        .first()
    )
    creating = row is None
    if row is None:
        if "product" not in data:
            raise FieldErrors({"product": ["Choose an endoflife.date product (GET /end-of-life/products)."]})
        row = TrackedVersion(organization=org, resource=record)
    before = _values(row)
    errors: dict[str, list[str]] = {}
    _apply(row, data, errors, product_allowed=True)
    if errors:
        raise FieldErrors(errors)
    _derive(row, _application_version(record))
    after = _values(row)
    if not creating and after == before:
        return row
    _save(row, creating)
    _audit_write(org, actor, row, creating, _changes(before, after))
    if before["product"] != row.product:
        for child in TrackedVersion.objects.select_for_update().filter(
            organization=org, resource=record, instance__isnull=False
        ):
            child_before = _values(child)
            child.product = row.product
            if child.cycle_explicit:
                child.cycle = kept_cycle(row.product, child.cycle)
                child.cycle_explicit = bool(child.cycle)
            _derive(child, None)
            child.save()
            audit(org, actor, "tracked_version.updated", child, _changes(child_before, _values(child)))
    cache.request_product(row.product)
    return row


@transaction.atomic
def track_instance(org, actor, instance: ApplicationInstance, data: dict[str, Any]) -> TrackedVersion:
    """Create or change one instance's own version or cycle; the product is the application's."""
    _unknown_keys(data, ("version", "cycle"))
    parent = TrackedVersion.objects.filter(
        organization=org, resource_id=instance.application_id, instance__isnull=True
    ).first()
    if parent is None:
        raise Conflict("Track the application first: choose its endoflife.date product.")
    row = TrackedVersion.objects.select_for_update().filter(organization=org, instance=instance).first()
    creating = row is None
    if row is None:
        if not (data.get("version") or data.get("cycle")):
            raise FieldErrors({"version": ["Enter the version this instance runs, or its release cycle."]})
        row = TrackedVersion(organization=org, resource_id=instance.application_id, instance=instance)
    row.product = parent.product
    before = _values(row)
    errors: dict[str, list[str]] = {}
    _apply(row, data, errors, product_allowed=False)
    if errors:
        raise FieldErrors(errors)
    _derive(row, None)
    after = _values(row)
    if not creating and after == before:
        return row
    _save(row, creating)
    _audit_write(org, actor, row, creating, _changes(before, after))
    return row


RACE = "Someone else set this end-of-life tracking a moment ago. Reload it and try again."


def _save(row: TrackedVersion, creating: bool) -> None:
    """Save; a first create that loses a race with another (the record's or instance's unique key) is a 409."""
    if not creating:
        row.save()
        return
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError:
        raise Conflict(RACE) from None


def _audit_write(org, actor, row: TrackedVersion, creating: bool, changes: dict[str, Any]) -> None:
    if creating:
        audit(org, actor, "tracked_version.created", row, changes)
    else:
        audit(org, actor, "tracked_version.updated", row, changes)


def _delete(org, actor, row: TrackedVersion) -> None:
    audit(org, actor, "tracked_version.deleted", row, _values(row))
    row.delete()


@transaction.atomic
def untrack(org, actor, record: Resource) -> int:
    """Remove the record's tracking and its instances'. Returns how many rows went (0: it wasn't tracked)."""
    rows = list(TrackedVersion.objects.select_for_update().filter(organization=org, resource=record))
    rows.sort(key=lambda r: r.instance_id is None)  # instances first, the record row last
    for row in rows:
        _delete(org, actor, row)
    return len(rows)


@transaction.atomic
def untrack_instance(org, actor, instance: ApplicationInstance) -> int:
    rows = list(TrackedVersion.objects.select_for_update().filter(organization=org, instance=instance))
    for row in rows:
        _delete(org, actor, row)
    return len(rows)
