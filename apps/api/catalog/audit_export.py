"""The organization's audit log as a download: CSV for everyone with the log (community
`audit.log`), and JSON lines with the full snapshots for a SIEM (`audit.export`). Routes are in
catalog/audit_viewsets.py; both take the log's filters.

Rows are streamed in keyset batches of BATCH events, each batch named in a fixed number of queries (audit_log.Names),
so memory and query counts stay bounded however long the range. An export holds at most MAX_ROWS events (a larger
range is refused with the count, so the caller narrows it), and only events written before it started: its own
`export` event and anything written while it streams are left for the next one.

Nothing secret leaves here: payloads were redacted when written (catalog/services.audit) and snapshots copy declared
fields only (catalog/snapshots.py); both are redacted again on the way out with the snapshot word list (the
stricter one), and compliance fields are dropped while the profile is off, as on screen.

`rows` in the export's audit event is the count when the export started (it is written before streaming), so a client
that disconnects early is still recorded as having asked for them all. The cutoff is by `created_at`, which is insert
time, not commit time: a transaction that inserted an event before the export started but commits while it streams
can add a row the count didn't include, or miss a later range pull that started after it. A SIEM pulling by date
range should overlap its windows and dedupe on `id`."""

import csv
import io
import json
from collections.abc import Iterator
from datetime import UTC
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models import Q, QuerySet

from core.models import AuditEvent
from imports.csvio import escape_cell

from . import compliance
from .audit_log import LABELS, Names, summary, target_of
from .redaction import SNAPSHOT_DENYLIST, redact

BATCH = 500
MAX_ROWS = 100_000
SCHEMA_VERSION = 1  # JSON lines: bumped only if a field is renamed or removed (adding one doesn't)

CSV_COLUMNS = [
    "id",
    "time_utc",
    "time_local",
    "actor_id",
    "actor",
    "action",
    "label",
    "target_kind",
    "target_name",
    "target_type",
    "target_id",
    "related_target_type",
    "related_target_id",
    "summary",
    "request_id",
    # Appended so existing column positions don't move: the platform staff account that acted as `actor`
    "impersonated_by_id",
    "impersonated_by",
]


def batches(events: QuerySet, *, newest_first: bool) -> Iterator[list[AuditEvent]]:
    """The events in pages of BATCH by (created_at, id), so each page is one indexed query from where the last ended."""
    sign = "-" if newest_first else ""
    ordered = events.order_by(f"{sign}created_at", f"{sign}id")
    last: AuditEvent | None = None
    while True:
        page = ordered
        if last is not None:
            if newest_first:
                after = Q(created_at__lt=last.created_at) | Q(created_at=last.created_at, id__lt=last.id)
            else:
                after = Q(created_at__gt=last.created_at) | Q(created_at=last.created_at, id__gt=last.id)
            page = page.filter(after)
        rows = list(page[:BATCH])
        if not rows:
            return
        yield rows
        if len(rows) < BATCH:
            return
        last = rows[-1]


def _named(org, rows: list[AuditEvent]) -> Names:
    if not compliance.enabled(org):  # as on screen: compliance values stay hidden while the profile is off
        for event in rows:
            compliance.hide(event)
    return Names(org, rows)


def _actor_name(e: AuditEvent) -> str:
    return (e.actor.get_full_name() or e.actor.email) if e.actor else ""


def _impersonator_name(e: AuditEvent) -> str:
    by = e.impersonated_by
    return (by.get_full_name() or by.email) if by else ""


def _utc(e: AuditEvent, timespec: str = "seconds") -> str:
    return e.created_at.astimezone(UTC).isoformat(timespec=timespec).replace("+00:00", "Z")


def csv_rows(org, events: QuerySet) -> Iterator[bytes]:
    """The CSV file in chunks: a BOM and the header, then one chunk per batch. Every cell is formula-escaped
    (imports/csvio.escape_cell), as in the record exports."""
    zone = ZoneInfo(org.timezone)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")

    def flush() -> bytes:
        data = out.getvalue().encode("utf-8")
        out.seek(0)
        out.truncate()
        return data

    writer.writerow(CSV_COLUMNS)
    yield "﻿".encode() + flush()
    for rows in batches(events, newest_first=True):
        names = _named(org, rows)
        for e in rows:
            target = target_of(e, names) or {}
            values = {
                "id": e.id,
                "time_utc": _utc(e),
                "time_local": e.created_at.astimezone(zone).isoformat(timespec="seconds"),
                "actor_id": e.actor_id or "",
                "actor": _actor_name(e),
                "impersonated_by_id": e.impersonated_by_id or "",
                "impersonated_by": _impersonator_name(e),
                "action": e.action,
                "label": LABELS.get(e.action, e.action),
                "target_kind": target.get("kind", ""),
                "target_name": target.get("name", ""),
                "target_type": e.target_type,
                "target_id": e.target_id or "",
                "related_target_type": e.related_target_type,
                "related_target_id": e.related_target_id or "",
                "summary": summary(e, names),
                "request_id": e.request_id or "",
            }
            writer.writerow([escape_cell(values[c]) for c in CSV_COLUMNS])
        yield flush()


def jsonl_record(e: AuditEvent, names: Names, org) -> dict[str, Any]:
    """One event for a SIEM. Field names are stable (SCHEMA_VERSION); times are UTC with microseconds. People in
    snapshots are references as stored ({id, display: null}); `target.name` is the current name."""
    target = target_of(e, names)
    return {
        "schema_version": SCHEMA_VERSION,
        "id": str(e.id),
        "organization": {"id": str(org.id), "slug": org.slug},
        "time": _utc(e, "microseconds"),
        "action": e.action,
        "label": LABELS.get(e.action, e.action),
        "actor": {"id": e.actor_id, "name": _actor_name(e)} if e.actor else None,
        # The platform staff account that made this write while impersonating `actor`, else null. Added
        # without a version bump (adding a field doesn't change SCHEMA_VERSION).
        "impersonated_by": (
            {"id": e.impersonated_by_id, "name": _impersonator_name(e)} if e.impersonated_by_id else None
        ),
        "target": {
            "type": e.target_type or None,
            "id": str(e.target_id) if e.target_id else None,
            "kind": target["kind"] if target else None,
            "name": target["name"] if target else None,
        },
        "related_target": (
            {"type": e.related_target_type, "id": str(e.related_target_id)} if e.related_target_id else None
        ),
        "request_id": str(e.request_id) if e.request_id else None,
        "summary": summary(e, names),
        "changes": redact(e.changes or {}, SNAPSHOT_DENYLIST),
        "prechange": redact(e.prechange, SNAPSHOT_DENYLIST) if e.prechange is not None else None,
        "postchange": redact(e.postchange, SNAPSHOT_DENYLIST) if e.postchange is not None else None,
    }


def jsonl_rows(org, events: QuerySet) -> Iterator[bytes]:
    """JSON lines, oldest first (the order a SIEM ingests), one chunk per batch."""
    for rows in batches(events, newest_first=False):
        names = _named(org, rows)
        yield "".join(
            json.dumps(jsonl_record(e, names, org), ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
            for e in rows
        ).encode("utf-8")
