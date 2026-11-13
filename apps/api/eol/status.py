"""The derived end-of-life status of a tracked version (never stored: it moves on its own as dates pass).

- **end_of_life**: the cycle's end-of-life date is today or earlier; with no date, endoflife.date says it is EOL.
- **security_only**: not end of life, but active support has ended (the product has an active-support phase and its
  date has passed, or with no date endoflife.date says it ended): security fixes only, as Ubuntu's or Django's.
- **supported**: neither.
- **unknown**: no product data cached yet, no cycle (no version, or a version that matches no cycle), or a cycle
  endoflife.date doesn't list.

A date wins over the boolean beside it: the booleans are computed when endoflife.date generates its data (daily), so a
cache a few days old would otherwise be late by those days. `days_remaining` counts from the organization's today to
the end-of-life date: 0 on the day, negative after it, null with no date. Extended support (a paid phase after end of
life, such as Ubuntu Pro) is reported beside the status and doesn't change it."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

SUPPORTED = "supported"
SECURITY_ONLY = "security_only"
END_OF_LIFE = "end_of_life"
UNKNOWN = "unknown"
STATUSES = (SUPPORTED, SECURITY_ONLY, END_OF_LIFE, UNKNOWN)
LABELS = {
    SUPPORTED: "Supported",
    SECURITY_ONLY: "Security fixes only",
    END_OF_LIFE: "End of life",
    UNKNOWN: "Unknown",
}
# How far ahead an end-of-life date puts a version in Needs attention ("near end of life").
NEAR_DAYS = 180

_LEADING_V = re.compile(r"^v(?=\d)")
_SEPARATORS = (".", "-", "+", " ", "_")


def normalize_version(value: str) -> str:
    return _LEADING_V.sub("", (value or "").strip().lower())


def derive_cycle(version: str, names: Iterable[str]) -> str:
    """The release cycle a version belongs to: the longest release name the version equals or starts with followed by
    a separator (16.4 -> 16, 22.04.3 -> 22.04, v3.12.1 -> 3.12; 1.10 is not 1.1). "" when none matches."""
    wanted = normalize_version(version)
    if not wanted:
        return ""
    best = ""
    for name in names:
        candidate = name.strip().lower()
        if not candidate:
            continue
        if wanted == candidate or any(wanted.startswith(candidate + sep) for sep in _SEPARATORS):
            if len(candidate) > len(best.lower()):
                best = name
    return best


@dataclass(frozen=True)
class Derived:
    status: str
    why: str
    eol_date: date | None = None
    days_remaining: int | None = None
    support_until: date | None = None
    extended_support_until: date | None = None


def _reached(flag: bool | None, when: date | None, today: date) -> bool:
    if when is not None:
        return when <= today
    return bool(flag)


def _days(n: int) -> str:
    return f"{n} day{'' if n == 1 else 's'}"


def derive(release: Any, today: date, *, product_label: str = "", cycle: str = "", unknown_why: str = "") -> Derived:
    """The status of one cycle (an EolRelease, or None with `unknown_why` saying why it is unknown)."""
    if release is None:
        return Derived(UNKNOWN, unknown_why or "No end-of-life data for this version.")
    name = f"{product_label or release.product_id} {cycle or release.name}".strip()
    eol_date = release.eol_from
    days = (eol_date - today).days if eol_date is not None else None
    support_until = release.eoas_from if release.has_eoas else None
    extended = release.eoes_from if release.has_eoes and release.is_eoes is not None else None
    common = {
        "eol_date": eol_date,
        "days_remaining": days,
        "support_until": support_until,
        "extended_support_until": extended,
    }
    if _reached(release.is_eol, eol_date, today):
        if eol_date is None:
            why = f"{name} is end of life."
        elif days == 0:
            why = f"{name} reaches end of life today."
        else:
            why = f"{name} reached end of life on {eol_date.isoformat()} ({_days(-(days or 0))} ago)."
        if extended is not None and extended > today:
            why += f" Extended support runs to {extended.isoformat()}."
        return Derived(END_OF_LIFE, why, **common)
    until = f" until {eol_date.isoformat()} ({_days(days or 0)})" if eol_date is not None else ""
    if release.has_eoas and _reached(release.is_eoas, release.eoas_from, today):
        return Derived(SECURITY_ONLY, f"{name} gets security fixes only{until}.", **common)
    return Derived(SUPPORTED, f"{name} is supported{until}.", **common)


def near(derived: Derived, horizon_days: int = NEAR_DAYS) -> bool:
    """End of life already, or its end-of-life date within `horizon_days`."""
    if derived.status == END_OF_LIFE:
        return True
    return derived.days_remaining is not None and derived.days_remaining <= horizon_days
