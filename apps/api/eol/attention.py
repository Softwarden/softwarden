"""The Needs attention "End of life" queue: one row per tracked version of a live active or deprecated record that is
end of life or reaches it within status.NEAR_DAYS, routed to the record's IT steward (else the coordinator). Empty
without the `eol` capability. Counted in the badge since the EOL calendar's SPA package. Not a digest section since
notify 0012: each version's end-of-life reminder (eol/reminders.py), folded into its row as `reminder`, is emailed by
the built-in "End of life" notification rule instead (notify/digest.NOT_SECTIONS)."""

from __future__ import annotations

from datetime import date
from typing import Any

from catalog.state import ACTIVE_LIFECYCLES
from core.capabilities import EOL, has_capability

from . import reads
from .status import END_OF_LIFE, NEAR_DAYS


def attention_rows(org, today: date) -> list[dict[str, Any]]:
    if not has_capability(org, EOL):
        return []
    qs = reads.live(org).filter(resource__lifecycle__in=ACTIVE_LIFECYCLES)
    found = []
    for row in reads.rows(org, today, qs):
        days = row["days_remaining"]
        if row["status"] != END_OF_LIFE and (days is None or days > NEAR_DAYS):
            continue
        public = reads.public(row)
        found.append(
            {
                "id": public["id"],
                "kind": "end_of_life",
                "record": {k: public["record"][k] for k in ("id", "name", "kind")},
                "instance": public["instance"],
                "product": public["product"],
                "product_label": public["product_label"],
                "cycle": public["cycle"],
                "status": public["status"],
                "eol_date": public["eol_date"],
                "days_remaining": days,
                "why": public["why"],
                "steward_id": row["_steward_id"],
            }
        )
    return found  # soonest end of life first (reads.rows' order)
