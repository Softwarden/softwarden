"""Reading tracked versions with their derived status: the record detail, the overview and its CSV, Needs attention.
A few queries per call whatever the number of rows (the rows, their products, their releases)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

from django.db.models import F, QuerySet

from catalog.models import Lifecycle
from core.capabilities import EOL, has_capability
from core.drf import iso

from . import cache
from .models import EolProduct, EolRelease, EolSyncState, TrackedVersion
from .status import STATUSES, UNKNOWN, Derived, derive


def tracked(org) -> QuerySet[TrackedVersion]:
    return (
        TrackedVersion.objects.filter(organization=org)
        .select_related("resource", "instance", "resource__business_owner", "resource__technical_owner")
        .annotate(app_version=F("resource__application__version"))
    )


def live(org) -> QuerySet[TrackedVersion]:
    """Rows of records that are neither archived nor retired: what the overview and Needs attention look at."""
    return tracked(org).filter(resource__archived_at__isnull=True).exclude(resource__lifecycle=Lifecycle.RETIRED)


# Why a version's status is unknown, machine-readable (`unknown_reason`). The first three are the installation's (the
# product's data isn't there yet), the last three the record's own gap (its version or cycle).
INSTALLATION_REASONS = ("not_downloaded", "download_failed", "downloads_off")
RECORD_REASONS = ("cycle_not_listed", "no_version_match", "no_version")
UNKNOWN_REASONS = (*INSTALLATION_REASONS, *RECORD_REASONS)


def _unknown(row: TrackedVersion, product: EolProduct | None, version: str) -> tuple[str, str]:
    """(reason, why in words) for a version whose status would be unknown."""
    if product is None or product.releases_fetched_at is None:
        if product is not None and product.error:
            return "download_failed", f"endoflife.date data for {row.product} couldn't be read: {product.error}"
        if not cache.enabled():
            return (
                "downloads_off",
                f"No endoflife.date data for {row.product}: downloads are off on this installation (import a snapshot).",
            )
        return (
            "not_downloaded",
            f"endoflife.date data for {row.product} hasn't been downloaded yet; it comes with the next refresh.",
        )
    label = product.label or product.name
    if row.cycle:
        return "cycle_not_listed", f"endoflife.date lists no release cycle {row.cycle!r} for {label}."
    if version:
        return "no_version_match", f"Version {version!r} matches no {label} release cycle: set the cycle."
    return "no_version", f"No version recorded: set the version or the release cycle of {label}."


def _person(person) -> dict[str, str] | None:
    return {"id": str(person.id), "name": person.name} if person is not None else None


def rows(org, today: date, queryset: Iterable[TrackedVersion] | None = None) -> list[dict[str, Any]]:
    """One plain dict per tracked version: the record, the instance, the product, the version and cycle (with where
    they came from) and the derived status. Sorted by end-of-life date (unknown last), record name, instance name."""
    found = list(queryset if queryset is not None else live(org))
    if not found:
        return []
    names = {r.product for r in found}
    products = {p.name: p for p in EolProduct.objects.filter(name__in=names)}
    releases = {
        (r.product_id, r.name): r
        for r in EolRelease.objects.filter(product_id__in=names).order_by("product", "position")
    }
    out: list[dict[str, Any]] = []
    for row in found:
        product = products.get(row.product)
        label = (product.label if product else "") or row.product
        version, version_source = cache.effective_version(row, getattr(row, "app_version", None))
        release = releases.get((row.product, row.cycle)) if row.cycle else None
        reason, why = _unknown(row, product, version) if release is None else (None, "")
        derived: Derived = derive(release, today, product_label=label, cycle=row.cycle, unknown_why=why)
        resource = row.resource
        out.append(
            {
                "id": str(row.id),
                "record": {
                    "id": str(resource.id),
                    "name": resource.name,
                    "kind": resource.kind,
                    "criticality": resource.criticality or None,
                    "business_owner": _person(resource.business_owner),
                    "technical_owner": _person(resource.technical_owner),
                },
                "instance": {"id": str(row.instance.id), "name": row.instance.name} if row.instance else None,
                "product": row.product,
                "product_label": label,
                "product_link": product.link if product else "",
                "product_renamed_to": (product.renamed_to or None) if product else None,
                "version": version,
                "version_source": version_source,
                "cycle": row.cycle,
                "cycle_source": row.cycle_source,
                "status": derived.status,
                "unknown_reason": reason if derived.status == UNKNOWN else None,
                "why": derived.why,
                "eol_date": derived.eol_date,
                "days_remaining": derived.days_remaining,
                "support_until": derived.support_until,
                "extended_support_until": derived.extended_support_until,
                "lts": release.is_lts if release else None,
                "latest": (release.latest or None) if release else None,
                "latest_date": release.latest_date if release else None,
                "release_date": release.release_date if release else None,
                "checked_at": product.releases_checked_at if product else None,
                "_steward_id": resource.it_steward_id,
            }
        )
    out.sort(
        key=lambda r: (
            r["eol_date"] is None,
            r["eol_date"] or date.max,
            r["record"]["name"].lower(),
            (r["instance"] or {}).get("name", "").lower(),
            r["id"],
        )
    )
    return out


def public(row: dict[str, Any]) -> dict[str, Any]:
    """The row as the API shows it: dates as YYYY-MM-DD, timestamps in UTC, the routing keys left out."""
    return {k: iso(v) if isinstance(v, date) else v for k, v in row.items() if not k.startswith("_")}


def record_block(org, record, today: date) -> dict[str, Any] | None:
    """The record detail's `end_of_life`: its own row with `instances` (rows of its instances), or None when it isn't
    tracked or the organization lacks the `eol` capability."""
    if not has_capability(org, EOL):
        return None
    found = rows(org, today, tracked(org).filter(resource=record))
    own = next((r for r in found if r["instance"] is None), None)
    if own is None:
        return None
    instances = sorted((r for r in found if r["instance"] is not None), key=lambda r: r["instance"]["name"].lower())
    return public(own) | {"instances": [public(r) for r in instances]}


def counts(found: list[dict[str, Any]]) -> dict[str, int]:
    tally = dict.fromkeys(STATUSES, 0)
    for r in found:
        tally[r["status"]] = tally.get(r["status"], 0) + 1
    return tally


def cache_status() -> dict[str, Any]:
    row = EolSyncState.objects.filter(pk=1).first() or EolSyncState()  # read only: no row before the first run
    return {
        "enabled": cache.enabled(),
        "source": row.source or None,
        "products": EolProduct.objects.filter(listed=True).count(),
        "last_run_at": iso(row.last_run_at),
        "last_success_at": iso(row.last_success_at),
        "imported_at": iso(row.imported_at),
        "last_error": row.last_error or None,
    }
