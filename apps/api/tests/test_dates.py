"""Calendar arithmetic: month-end clamping, leap years, organization-local today."""

from datetime import UTC, date, datetime

import pytest

from core import dates
from core.models import Organization


@pytest.mark.parametrize(
    "start,value,unit,expected",
    [
        (date(2027, 3, 31), -2, "months", date(2027, 1, 31)),
        (date(2027, 3, 31), -1, "months", date(2027, 2, 28)),
        (date(2028, 3, 31), -1, "months", date(2028, 2, 29)),
        (date(2028, 2, 29), 1, "years", date(2029, 2, 28)),
        (date(2028, 2, 29), 4, "years", date(2032, 2, 29)),
        (date(2027, 1, 31), 1, "months", date(2027, 2, 28)),
        (date(2027, 1, 15), -1, "months", date(2026, 12, 15)),
        (date(2027, 12, 31), 2, "months", date(2028, 2, 29)),
        (date(2027, 1, 31), -30, "days", date(2027, 1, 1)),
        (date(2027, 3, 1), -1, "days", date(2027, 2, 28)),
        (date(2027, 3, 31), -12, "months", date(2026, 3, 31)),
    ],
)
def test_shift_clamps_to_month_end(start, value, unit, expected):
    assert dates.shift(start, value, unit) == expected


def test_shift_refuses_unknown_unit():
    with pytest.raises(ValueError):
        dates.shift(date(2027, 1, 1), 1, "weeks")


def test_describe():
    assert dates.describe(1, "years") == "1 year"
    assert dates.describe(2, "months") == "2 months"


@pytest.mark.parametrize(
    "zone,expected",
    [("America/Los_Angeles", date(2026, 11, 2)), ("UTC", date(2026, 11, 3)), ("Pacific/Auckland", date(2026, 11, 3))],
)
def test_org_today_is_local(zone, expected, monkeypatch):
    class At(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 3, 6, 0, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(dates, "datetime", At)
    assert dates.org_today(Organization(name="x", timezone=zone)) == expected


def test_overdue_starts_the_next_day():
    assert not dates.is_overdue(date(2026, 11, 30), date(2026, 11, 30))
    assert dates.is_overdue(date(2026, 11, 30), date(2026, 12, 1))
