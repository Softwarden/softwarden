"""End-of-life dates as reminder facts: the `end_of_life` reminder source (notify/reminders.PROVIDERS, notify 0012).

One fact per tracked version (the record's own row and each instance's) of a live active or deprecated record, the
records the Needs attention "End of life" queue judges (eol/attention.py), whose release cycle has an end-of-life
date: due that date. A cycle that endoflife.date flags end of life with no date is due the day the version was tracked
(the tracking row's `created_at`, organization-local; notify 0013): a stored date that never moves, so the reminder's
repeats keep their rhythm. Not the day the flag was first seen: the cache doesn't keep one (it is shared by every
organization and an imported snapshot arrives with the flag already set), and the reminder only exists from the first
run that sees the flag anyway. Such a fact is worded for what it is: labelled "End of life (no date published):
<product> <cycle>", and its notices and its Needs attention fold say "End of life (no date published), tracked since
<date>" (`undated_text`), never that the version reached end of life on that date. Its `source_field` names the cycle
too (`tracked_version:<id>:undated:<cycle>`), so an acknowledgement doesn't carry over to another flag-only cycle the
version is re-tracked to, and a date endoflife.date publishes later makes a new, dated occurrence.

The built-in "End of life" notification rule (notify/rule_services.BUILTINS) announces them 90, 30 and 7 days before
and then every 30 days while the version is still end of life (`repeat_every_days`): the reminder stays live as long
as the version is tracked on an end-of-life cycle, and is closed as obsolete once it isn't (upgraded, untracked, the
record retired or archived).

Keyed by the record: `source_id` is the record (so a write to the record or its tracking regenerates just its
reminders, as for its review and custom dates), `source_field` is `tracked_version:<id>` (one occurrence per tracked
version; a flag-only one also names the cycle, above), `resource_id` the record (routing and the page it points at). The Needs attention queue folds a version's
reminder into its row. Empty without the `eol` capability, so turning it off closes every one as obsolete."""

from __future__ import annotations

import hashlib
import uuid
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from catalog.state import ACTIVE_LIFECYCLES
from core.capabilities import EOL, has_capability

from . import reads
from .status import END_OF_LIFE

SOURCE_TYPE = "end_of_life"
LABEL = "End of life"
UNDATED_LABEL = "End of life (no date published)"
FIELD_PREFIX = "tracked_version:"
UNDATED = ":undated:"
CYCLE_KEY_MAX = 30  # source_field is 100 characters: prefix 16, id 36, marker 9


def source_field(tracked_version_id: Any, undated_cycle: str | None = None) -> str:
    """`tracked_version:<id>`, or for a cycle flagged end of life with no date `tracked_version:<id>:undated:<cycle>`
    (a longer cycle name by a hash of it)."""
    field = f"{FIELD_PREFIX}{tracked_version_id}"
    if undated_cycle is None:
        return field
    cycle = undated_cycle
    if len(cycle) > CYCLE_KEY_MAX:
        cycle = hashlib.sha256(cycle.encode()).hexdigest()[:CYCLE_KEY_MAX]
    return f"{field}{UNDATED}{cycle}"


def tracked_version_of(field: str) -> str | None:
    """The tracked version's id a reminder's `source_field` names, or None."""
    return field.removeprefix(FIELD_PREFIX).split(":", 1)[0] if field.startswith(FIELD_PREFIX) else None


def is_undated(source_type: str, field: str) -> bool:
    """Whether the reminder is a flag-only end of life (due the day the version was tracked, not an end-of-life date)."""
    return source_type == SOURCE_TYPE and field.startswith(FIELD_PREFIX) and UNDATED in field


def undated_text(tracked_on: date) -> str:
    return f"{UNDATED_LABEL}, tracked since {tracked_on.isoformat()}"


def facts(org, today: date, ids: Any = None) -> list[Any]:
    """The end-of-life facts of the organization's records (only of the records `ids`, when given)."""
    from notify.reminders import Fact

    if not has_capability(org, EOL):
        return []
    qs = reads.live(org).filter(resource__lifecycle__in=ACTIVE_LIFECYCLES)
    if ids is not None:
        qs = qs.filter(resource_id__in=list(ids))
    versions = list(qs)
    zone = ZoneInfo(org.timezone)
    tracked_on = {str(v.id): v.created_at.astimezone(zone).date() for v in versions}
    found = []
    for row in reads.rows(org, today, versions):
        due = row["eol_date"]
        undated = due is None and row["status"] == END_OF_LIFE  # flagged end of life with no date
        if undated:
            due = tracked_on[row["id"]]
        if due is None:
            continue
        record = uuid.UUID(row["record"]["id"])
        title = row["record"]["name"] + (f" ({row['instance']['name']})" if row["instance"] else "")
        found.append(
            Fact(
                SOURCE_TYPE,
                record,
                due,
                title=title,
                label=f"{UNDATED_LABEL if undated else LABEL}: {row['product_label']} {row['cycle']}".strip(),
                source_field=source_field(row["id"], row["cycle"] if undated else None),
                resource_id=record,
            )
        )
    return found
