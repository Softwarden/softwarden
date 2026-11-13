"""The installation-wide endoflife.date cache: the daily refresh, the offline import and cycle re-derivation.

Refresh (`eol.refresh`, a job in the worker's long lane; the `eol.refresh` schedule queues it daily at 04:45, after
endoflife.date regenerates its data around 00:10 UTC). It fetches the product list, then, for the products some
organization tracks (and only those), each product's release cycles. Both send the ETag they last got as
If-None-Match, so an unchanged document costs a 304. A product that fails keeps its cached releases and records why
(`EolProduct.error`); the others go on. The job fails (and core/jobs retries it, 1 and 4 minutes later) only when the
product list couldn't be read and nothing is cached at all. A new mapping to a product whose releases aren't cached yet
queues the same job for just that product (`request_product`), so its status appears within a worker tick; while no
product list has ever been read (`list_cached`), it queues a full refresh instead, so the list arrives with it. With
SOFTWARDEN_EOL_ENABLED=0 nothing is fetched or queued.

"The list is cached" is decided by EolSyncState (`list_fetched_at`, set by a refresh or by importing a list or a full
document), never by EolProduct rows: a product fetched or imported on its own, or one whose fetch failed, gets a row
with `listed` false, so it can't make every other product look unknown. A product answer that redirects to another
name (endoflife.date renamed it) keeps its releases under the name organizations track and notes the new name in
`renamed_to`, so the screen can offer to re-map.

Offline import (`manage.py eol_import FILE`): a document the API returns, saved to a file on a connected machine:
`/api/v1/products/full` (every product with its releases: the whole cache in one file), `/api/v1/products` (the list
only) or `/api/v1/products/{name}` (one product). The same parser as the refresh reads it.

Parsing is defensive: endoflife.date's data is public but still input. Unknown fields are ignored, text is cut to the
column, a malformed date or timestamp is treated as absent, and a release or product entry of the wrong shape is
skipped. A product document that can't be stored at all is that product's error; the refresh goes on with the others.

After either, every derived cycle of the products touched is re-derived (`rederive`) across organizations: a derived
column, not an audited edit (as reminders and stored deadlines are)."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core import jobs
from core.models import Job, JobStatus

from . import client
from .models import EolProduct, EolRelease, EolSyncState, TrackedVersion
from .status import derive_cycle

log = logging.getLogger("softwarden.eol")

JOB = "eol.refresh"
SCHEDULE = "eol.refresh"
CRON = "45 4 * * *"
LEASE_SECONDS = 600
API = "api"
IMPORT = "import"


class RefreshFailed(Exception):
    """Nothing could be read and nothing is cached: the job is retried."""


def enabled() -> bool:
    return bool(getattr(settings, "SOFTWARDEN_EOL_ENABLED", True))


def state() -> EolSyncState:
    row, _ = EolSyncState.objects.get_or_create(pk=1)
    return row


def list_cached() -> bool:
    """Whether a product list has ever been read (a refresh, or an import of a list or full document)."""
    return EolSyncState.objects.filter(pk=1, list_fetched_at__isnull=False).exists()


# ---- parsing ---------------------------------------------------------------------------------------------------------


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_datetime(value)
    except ValueError:  # well formed but impossible, such as month 13
        return None


def _url(value: Any) -> str:
    text = _text(value, 500)
    return text if text.startswith(("https://", "http://")) else ""


def _names(value: Any, limit: int = 100) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v.strip()[:limit] for v in value if isinstance(v, str) and v.strip()][:50]


def _product_name(value: Any) -> str:
    name = _text(value, 100).lower()
    return name if name and all(c.isalnum() or c in "._+-" for c in name) else ""


def summary_fields(item: dict[str, Any]) -> dict[str, Any]:
    links = item.get("links") if isinstance(item.get("links"), dict) else {}
    return {
        "label": _text(item.get("label"), 200),
        "category": _text(item.get("category"), 60),
        "aliases": [a.lower() for a in _names(item.get("aliases"))],
        "tags": _names(item.get("tags"), 60),
        "link": _url(links.get("html")) if links else "",
        "release_policy": _url(links.get("releasePolicy")) if links else "",
    }


def parse_release(item: Any, position: int) -> dict[str, Any] | None:
    """One v1 ProductRelease as EolRelease fields, or None when it isn't one."""
    if not isinstance(item, dict):
        return None
    name = _text(item.get("name"), 100)
    if not name:
        return None
    latest = item.get("latest") if isinstance(item.get("latest"), dict) else {}
    is_eoes = _bool(item.get("isEoes"))
    return {
        "name": name,
        "label": _text(item.get("label"), 200),
        "codename": _text(item.get("codename"), 200),
        "position": position,
        "release_date": _date(item.get("releaseDate")),
        "is_lts": bool(_bool(item.get("isLts"))),
        "lts_from": _date(item.get("ltsFrom")),
        "has_eoas": "isEoas" in item or "eoasFrom" in item,
        "is_eoas": _bool(item.get("isEoas")),
        "eoas_from": _date(item.get("eoasFrom")),
        "is_eol": bool(_bool(item.get("isEol"))),
        "eol_from": _date(item.get("eolFrom")),
        "has_eoes": "isEoes" in item or "eoesFrom" in item,
        "is_eoes": is_eoes,
        "eoes_from": _date(item.get("eoesFrom")) if is_eoes is not None else None,
        "is_maintained": bool(_bool(item.get("isMaintained"))),
        "latest": _text(latest.get("name"), 100) if latest else "",
        "latest_date": _date(latest.get("date")) if latest else None,
        "link": _url(latest.get("link")) if latest else "",
    }


def _result(document: Any) -> Any:
    return document.get("result") if isinstance(document, dict) else None


# ---- storing ---------------------------------------------------------------------------------------------------------


def store_list(items: Any, now: datetime) -> int:
    """Upsert the product list (summaries or full details); products no longer in it are marked unlisted. Returns how
    many products it carried. Releases are left alone (a full document's are stored by store_releases)."""
    if not isinstance(items, list):
        raise client.FetchError("the product list has an unexpected shape")
    rows: dict[str, EolProduct] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _product_name(item.get("name"))
        if name and name not in rows:
            rows[name] = EolProduct(name=name, listed=True, **summary_fields(item))
    if not rows and items:
        raise client.FetchError("the product list has no products")
    with transaction.atomic():
        EolProduct.objects.bulk_create(
            list(rows.values()),
            update_conflicts=True,
            unique_fields=["name"],
            update_fields=["label", "category", "aliases", "tags", "listed", "link", "release_policy", "updated_at"],
        )
        EolProduct.objects.exclude(name__in=list(rows)).update(listed=False, updated_at=now)
    return len(rows)


def store_releases(
    name: str, details: Any, now: datetime, *, etag: str = "", modified: Any = None, renamed_to: str = ""
) -> int:
    """Replace one product's releases with those of a v1 ProductDetails. Returns how many it has. A product row it
    creates is unlisted until a product list says otherwise (list_cached)."""
    if not isinstance(details, dict) or not isinstance(details.get("releases"), list):
        raise client.FetchError("the product has an unexpected shape")
    releases: dict[str, dict[str, Any]] = {}
    for position, item in enumerate(details["releases"]):
        parsed = parse_release(item, position)
        if parsed is not None:
            releases.setdefault(parsed["name"], parsed)
    with transaction.atomic():
        EolProduct.objects.get_or_create(name=name, defaults={"listed": False})
        product = EolProduct.objects.select_for_update().get(pk=name)
        fields = summary_fields(details)
        for key, value in fields.items():
            if value or key in ("aliases", "tags"):
                setattr(product, key, value)
        product.releases_etag = etag
        product.releases_fetched_at = now
        product.releases_checked_at = now
        product.source_modified_at = _datetime(modified)
        product.renamed_to = renamed_to
        product.error = ""
        product.save()
        EolRelease.objects.filter(product=product).delete()
        EolRelease.objects.bulk_create([EolRelease(product=product, **r) for r in releases.values()])
    return len(releases)


def _product_error(name: str, message: str, now: datetime, *, unlisted: bool = False) -> None:
    product, _ = EolProduct.objects.get_or_create(name=name, defaults={"listed": False})
    product.error = message[:300]
    product.releases_checked_at = now
    fields = ["error", "releases_checked_at", "updated_at"]
    if unlisted:
        product.listed = False
        fields.append("listed")
    product.save(update_fields=fields)


# ---- cycles ------------------------------------------------------------------------------------------------------------


def release_names(products: Iterable[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for product, name in EolRelease.objects.filter(product_id__in=list(products)).values_list("product_id", "name"):
        found.setdefault(product, []).append(name)
    return found


def effective_version(row: TrackedVersion, application_version: str | None) -> tuple[str, str]:
    """(version, where it came from: tracking, application or ""). The record row of an application falls back to the
    application's own version field."""
    if row.version:
        return row.version, "tracking"
    if row.instance_id is None and application_version:
        return application_version, "application"
    return "", ""


REDERIVE_CHUNK = 2000
REDERIVE_BATCH = 500


def _write_cycles(changed: dict[str, list[Any]]) -> int:
    """One UPDATE per new cycle value, in batches of ids, and only while the row's cycle is still derived: a person
    who set an explicit cycle meanwhile wins."""
    now = timezone.now()
    done = 0
    for cycle, ids in changed.items():
        for start in range(0, len(ids), REDERIVE_BATCH):
            batch = ids[start : start + REDERIVE_BATCH]
            done += TrackedVersion.objects.filter(pk__in=batch, cycle_explicit=False).update(
                cycle=cycle, updated_at=now
            )
    return done


def rederive(products: Iterable[str] | None = None, *, rows: Iterable[TrackedVersion] | None = None) -> int:
    """Recompute the derived cycle of tracked versions (all organizations; those of `products`, or just `rows`).
    Returns how many changed. Explicit cycles are left alone. The rows are streamed (REDERIVE_CHUNK at a time) and
    written by grouped conditional updates, so an installation-wide run holds no more than a chunk in memory."""
    if rows is None:
        qs = TrackedVersion.objects.filter(cycle_explicit=False)
        if products is not None:
            wanted = list(products)
            if not wanted:
                return 0
            qs = qs.filter(product__in=wanted)
        source: Iterable[TrackedVersion] = (
            qs.annotate(app_version=F("resource__application__version"))
            .only("id", "product", "version", "cycle", "cycle_explicit", "instance_id")
            .iterator(chunk_size=REDERIVE_CHUNK)
        )
    else:
        source = [r for r in rows if not r.cycle_explicit]
    names: dict[str, list[str]] = {}
    changed: dict[str, list[Any]] = {}
    total = 0
    pending = 0
    for row in source:
        if row.product not in names:
            names[row.product] = release_names([row.product]).get(row.product, [])
        version, _ = effective_version(row, getattr(row, "app_version", None))
        cycle = derive_cycle(version, names[row.product])
        if cycle != row.cycle:
            row.cycle = cycle
            changed.setdefault(cycle, []).append(row.pk)
            pending += 1
            if pending >= REDERIVE_CHUNK:
                total += _write_cycles(changed)
                changed, pending = {}, 0
    if changed:
        total += _write_cycles(changed)
    return total


def record_changed(org, resource) -> None:
    """catalog/services.RECORD_CHANGED: an application's version may have moved its record row's derived cycle."""
    rows = list(
        TrackedVersion.objects.filter(
            organization=org, resource=resource, instance__isnull=True, cycle_explicit=False
        ).annotate(app_version=F("resource__application__version"))
    )
    if rows:
        rederive(rows=rows)


def mapped_products() -> list[str]:
    return sorted(set(TrackedVersion.objects.values_list("product", flat=True)))


# ---- refresh ---------------------------------------------------------------------------------------------------------


def _safe(error: Exception) -> str:
    return str(error)[:300] or type(error).__name__


def refresh_product(name: str, now: datetime, counts: Counter) -> None:
    product = EolProduct.objects.filter(name=name).first()
    etag = product.releases_etag if product is not None and product.releases_fetched_at is not None else ""
    try:
        got = client.fetch(client.product_url(name), etag=etag, body_max=client.PRODUCT_MAX)
    except client.NotFound as error:
        _product_error(name, _safe(error), now, unlisted=True)
        counts["not_found"] += 1
        return
    except client.FetchError as error:
        _product_error(name, _safe(error), now)
        counts["failed"] += 1
        log.warning("eol product refresh failed product=%s error=%s", name, _safe(error))
        return
    if got.not_modified:
        EolProduct.objects.filter(name=name).update(releases_checked_at=now, error="", updated_at=now)
        counts["unchanged"] += 1
        return
    document = got.data if isinstance(got.data, dict) else {}
    details = _result(document)
    canonical = _product_name(details.get("name")) if isinstance(details, dict) else ""
    try:
        store_releases(
            name,
            details,
            now,
            etag=got.etag,
            modified=document.get("last_modified"),
            renamed_to=canonical if canonical and canonical != name else "",
        )
    except Exception as error:  # a malformed answer is this product's error; the run goes on with the others
        message = (
            _safe(error) if isinstance(error, client.FetchError) else f"unreadable answer ({type(error).__name__})"
        )
        _product_error(name, message, now)
        counts["failed"] += 1
        log.warning("eol product refresh failed product=%s error=%s", name, message)
        return
    counts["updated"] += 1


def refresh(products: list[str] | None = None, *, now: datetime | None = None, renew: Callable[[], Any] | None = None):
    """Refresh the cache: the product list and every tracked product's releases, or (with `products`) just those
    products' releases. Returns counts. Does nothing with SOFTWARDEN_EOL_ENABLED off."""
    if not enabled():
        return {"skipped": "disabled"}
    now = now or timezone.now()
    row = state()
    counts: Counter[str] = Counter()
    list_error = ""
    if products is None:
        try:
            got = client.fetch(client.products_url(), etag=row.list_etag, body_max=client.LIST_MAX)
        except client.FetchError as error:
            list_error = _safe(error)
            log.warning("eol product list refresh failed error=%s", list_error)
            if not EolProduct.objects.exists():
                row.last_run_at, row.last_error = now, f"product list: {list_error}"[:300]
                row.save()
                raise RefreshFailed(list_error) from None
        else:
            if got.not_modified:
                counts["list_unchanged"] += 1
            else:
                try:
                    counts["products"] = store_list(_result(got.data), now)
                    row.list_etag, row.list_fetched_at = got.etag, now
                except Exception as error:  # a malformed list keeps the cached one; the products still refresh
                    list_error = _safe(error) if isinstance(error, client.FetchError) else "unreadable product list"
            row.list_checked_at = now
        names = mapped_products()
    else:
        names = sorted({n for n in products if _product_name(n)})
    for name in names:
        if renew is not None:
            renew()
        refresh_product(name, now, counts)
    counts["rederived"] = rederive(names)
    if products is not None:
        return dict(counts)  # a few products for a new mapping: the daily run's bookkeeping stays as it was
    failures = counts["failed"] + counts["not_found"]
    row.last_run_at = now
    if list_error:
        row.last_error = f"product list: {list_error}"[:300]
    else:
        row.last_error = f"{failures} product(s) failed" if failures else ""
    if not list_error and not counts["failed"]:
        row.last_success_at = now
    row.source = API
    row.save()
    return dict(counts)


def run_job(job: Job, ctx) -> dict[str, Any]:
    products = job.payload.get("products")
    names = [str(p) for p in products] if isinstance(products, list) else None
    try:
        return refresh(names, renew=ctx.renew)
    except RefreshFailed as error:
        raise RuntimeError(f"endoflife.date unreachable: {error}") from None


def scheduled(now: datetime) -> dict[str, Any]:
    """The daily `eol.refresh` schedule: queue the refresh job unless disabled or one is queued already."""
    if not enabled():
        return {"skipped": "disabled"}
    if Job.objects.filter(kind=JOB, status=JobStatus.QUEUED, payload__products__isnull=True).exists():
        return {"skipped": "already queued"}
    jobs.enqueue(JOB, {})
    return {"queued": 1}


def request_product(name: str) -> bool:
    """Queue a refresh for a new mapping, unless one that covers it is queued: a full refresh while no product list
    has ever been read (it reads the list and every tracked product, this one included), else one of just this
    product's releases when they aren't cached. Never blocks on the network. True when it queued one."""
    if not enabled():
        return False
    full = not list_cached()
    if not full and EolProduct.objects.filter(name=name, releases_fetched_at__isnull=False).exists():
        return False
    queued = Job.objects.filter(kind=JOB, status=JobStatus.QUEUED)
    for payload in queued.values_list("payload", flat=True):
        wanted = payload.get("products") if isinstance(payload, dict) else None
        if wanted is None or (not full and name in wanted):
            return False
    jobs.enqueue(JOB, {} if full else {"products": [name]})
    return True


def register() -> None:
    jobs.register_job(JOB, run_job, lease=timedelta(seconds=LEASE_SECONDS), max_attempts=3, lane=jobs.LONG_LANE)
    jobs.register_schedule(SCHEDULE, scheduled, CRON, "Refresh the end-of-life data from endoflife.date")


# ---- offline import ----------------------------------------------------------------------------------------------------


def import_document(document: Any, *, now: datetime | None = None) -> dict[str, int]:
    """Load a saved API document (see the module docstring). Returns counts. Works whether or not the refresh is on."""
    now = now or timezone.now()
    try:
        return _import(document, now)
    except client.FetchError:
        raise
    except Exception as error:  # a document of an unexpected shape inside: refused in words, nothing half-stored
        raise client.FetchError(f"The file couldn't be read as endoflife.date data ({type(error).__name__}).") from None


@transaction.atomic
def _import(document: Any, now: datetime) -> dict[str, int]:
    result = _result(document)
    counts: Counter[str] = Counter()
    touched: list[str] = []
    if isinstance(result, list):
        counts["products"] = store_list(result, now)
        for item in result:
            if isinstance(item, dict) and isinstance(item.get("releases"), list):
                name = _product_name(item.get("name"))
                if name:
                    counts["releases"] += store_releases(name, item, now)
                    touched.append(name)
    elif isinstance(result, dict) and isinstance(result.get("releases"), list):
        name = _product_name(result.get("name"))
        if not name:
            raise client.FetchError("the product has no name")
        counts["releases"] = store_releases(name, result, now, modified=document.get("last_modified"))
        counts["products"] = 1
        touched.append(name)
    else:
        raise client.FetchError(
            "Not an endoflife.date v1 document: save /api/v1/products/full, /api/v1/products or /api/v1/products/NAME."
        )
    counts["rederived"] = rederive(touched)
    row = state()
    row.source, row.imported_at = IMPORT, now
    if isinstance(result, list):
        row.list_fetched_at, row.list_etag = now, ""  # the next refresh fetches the list again in full
    row.save()
    return dict(counts)
