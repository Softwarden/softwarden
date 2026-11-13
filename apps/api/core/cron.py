"""A small five-field cron parser for the worker's schedules. No dependency: schedules are
defined in code, so only the common syntax is needed.

Fields: minute (0-59), hour (0-23), day of month (1-31), month (1-12), day of week (0-6, Sunday is 0; 7 is also
Sunday). Each field is `*`, an ASCII number, a range `a-b`, a step `*/n` or `a-b/n`, or a comma-separated list of
those. Names (`mon`, `jan`) and the `@daily` shortcuts are not supported. As in Vixie cron, when both day of month and
day of week are restricted (neither starts with `*`), a day matches if either does; otherwise both must match.

Times are evaluated in UTC. Schedules whose timing depends on an organization's local time (the daily digest) run
every minute and decide for themselves whether each organization is due."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

SEARCH_DAYS = 366 * 5  # a valid expression always fires within five years (29 February needs four)


class CronError(ValueError):
    pass


def _digits(text):
    """ASCII digits only: str.isdigit() also accepts '²' and other digits int() can't read."""
    return text.isascii() and text.isdigit()


def _field(text, low, high):
    values: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not _digits(step_text) or int(step_text) < 1:
                raise CronError(f"Bad step in '{text}'.")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (_digits(a) and _digits(b)):
                raise CronError(f"Bad range in '{text}'.")
            start, end = int(a), int(b)
        elif _digits(part):
            start = end = int(part)
            if step != 1:
                end = high
        else:
            raise CronError(f"Can't read '{text}'.")
        if start < low or end > high or start > end:
            raise CronError(f"'{text}' is outside {low}-{high}.")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    any_day: bool
    any_weekday: bool

    def _day_matches(self, day):
        if self.months and day.month not in self.months:
            return False
        weekday = (day.weekday() + 1) % 7  # Python: Monday 0; cron: Sunday 0
        dom, dow = day.day in self.days, weekday in self.weekdays
        # Vixie: if either field starts with `*` both must match (a plain `*` holds every value, and `*/2` keeps
        # its step); only when both are restricted does a day match on either.
        if self.any_day or self.any_weekday:
            return dom and dow
        return dom or dow

    def next_after(self, moment):
        """The first fire time strictly after `moment` (an aware datetime), in UTC."""
        start = moment.astimezone(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = start.replace(hour=0, minute=0)
        for _ in range(SEARCH_DAYS):
            if self._day_matches(day):
                for hour in sorted(self.hours):
                    for minute in sorted(self.minutes):
                        candidate = day.replace(hour=hour, minute=minute)
                        if candidate >= start:
                            return candidate
            day += timedelta(days=1)
        raise CronError("This expression never fires.")


def parse(expression):
    parts = expression.split()
    if len(parts) != 5:
        raise CronError("A cron expression has five fields: minute hour day month weekday.")
    minute, hour, day, month, weekday = parts
    weekdays = _field(weekday, 0, 7)
    if 7 in weekdays:
        weekdays = (weekdays - {7}) | {0}
    cron = Cron(
        minutes=_field(minute, 0, 59),
        hours=_field(hour, 0, 23),
        days=_field(day, 1, 31),
        months=_field(month, 1, 12),
        weekdays=frozenset(weekdays),
        # Vixie cron: a field starting with `*` (`*`, `*/2`) counts as unrestricted for the day-of-month OR
        # day-of-week rule.
        any_day=day.startswith("*"),
        any_weekday=weekday.startswith("*"),
    )
    cron.next_after(datetime(2000, 1, 1, tzinfo=UTC))  # refuses an expression that never fires (31 February)
    return cron
