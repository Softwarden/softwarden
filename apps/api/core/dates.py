"""Calendar arithmetic shared by review state, renewal deadlines, next cycles and reminders.

Month and year steps clamp to the last day of a shorter month: two months before March 31 is January 31,
one month before March 31 is February 28 (29 in a leap year), and February 29 plus one year is February 28.
Dates are judged in the organization's timezone, never the server's.

Every date the API stores is inside a window, EARLIEST to LATEST: `parse_stored` refuses anything else, so
deadline arithmetic always has room. `shift` is total all the same: a row stored before the window existed (or put in
the database by hand) gives None rather than an OverflowError, so one bad row can never fail a list."""

import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

UNITS = ("days", "months", "years")

EARLIEST = date(1900, 1, 1)
LATEST = date(9998, 12, 31)  # a year short of date.max, so a term or a notice period added to it still fits
FORMAT_MESSAGE = "Enter a date as YYYY-MM-DD."
WINDOW_MESSAGE = f"Enter a date between {EARLIEST.isoformat()} and {LATEST.isoformat()}."


def in_window(day: date) -> bool:
    return EARLIEST <= day <= LATEST


def parse_stored(raw: object) -> date:
    """A date the API will store: an ISO `YYYY-MM-DD` string inside the window. Raises ValueError with the message
    to show for the field (FORMAT_MESSAGE or WINDOW_MESSAGE)."""
    if not isinstance(raw, str):
        raise ValueError(FORMAT_MESSAGE)
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        raise ValueError(FORMAT_MESSAGE) from None
    if not in_window(day):
        raise ValueError(WINDOW_MESSAGE)
    return day


def filter_day(value: str) -> date | None:
    """A `?from=` or `?to=` day for a list filter: ISO `YYYY-MM-DD`, empty for no filter, clamped into the window:
    every stored timestamp is inside it, so the rows match the same, and a day before 1900 or after 9998
    leaves room for the timezone shift and the day added to an inclusive end. Raises ValueError on a bad format."""
    if value == "":
        return None
    day = date.fromisoformat(value)
    return min(max(day, EARLIEST), LATEST)


def check_window(day: date | None, field: str, errors: dict[str, list[str]]) -> date | None:
    """`day` when it is None or inside the window; otherwise records WINDOW_MESSAGE for `field` and returns None.
    For dates that arrive already parsed (a spreadsheet cell, a DRF field)."""
    if day is not None and not in_window(day):
        errors[field] = [WINDOW_MESSAGE]
        return None
    return day


def add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def shift(day: date, value: int, unit: str) -> date | None:
    """`day` moved by value units (negative moves back), with month-end clamping for months and years; None when the
    result is outside the dates Python can represent (year 1 to 9999). An unknown unit is a programming error."""
    if unit not in UNITS:
        raise ValueError(f"Unknown unit {unit!r}")
    try:
        if unit == "days":
            return day + timedelta(days=value)
        return add_months(day, value if unit == "months" else value * 12)
    except (OverflowError, ValueError):
        return None


def describe(value: int, unit: str) -> str:
    """'2 months', '1 year', '90 days'."""
    return f"{value} {unit[:-1] if value == 1 else unit}"


def org_today(org) -> date:
    """Today in the organization's timezone."""
    return datetime.now(ZoneInfo(org.timezone)).date()


def is_overdue(deadline: date, today: date) -> bool:
    """The deadline is the last on-time day; overdue starts the next local day."""
    return today > deadline
