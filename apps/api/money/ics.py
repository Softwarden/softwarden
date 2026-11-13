"""A minimal iCalendar writer for the renewals calendar feed (RFC 5545), written for Outlook, Google Calendar and Apple
Calendar subscriptions. No dependency: the feed only needs all-day events.

What the clients need and what this writes:
- VCALENDAR with VERSION:2.0, PRODID, CALSCALE:GREGORIAN and METHOD:PUBLISH (a published calendar, not an invitation),
  plus X-WR-CALNAME (the name Outlook and Apple show) and X-PUBLISHED-TTL / REFRESH-INTERVAL (RFC 7986) asking for a
  refresh every few hours (clients may refresh less often; Outlook.com decides its own interval);
- one VEVENT per deadline: a stable UID (the same deadline keeps its UID across fetches, so an edited date moves the
  event instead of adding one), DTSTAMP in UTC, all-day DTSTART;VALUE=DATE and DTEND;VALUE=DATE the next day
  (exclusive end), SUMMARY, DESCRIPTION, URL, TRANSP:TRANSPARENT (a deadline doesn't make anyone busy);
- CRLF line endings, lines folded at 75 octets without splitting a UTF-8 character, and TEXT values escaped
  (backslash, semicolon, comma and newline). Control characters other than tab are dropped;
- with no deadlines, a UTC VTIMEZONE, since a calendar needs at least one component."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

CRLF = "\r\n"
LINE_OCTETS = 75
CONTENT_TYPE = "text/calendar; charset=utf-8"
EMPTY_COMPONENT = [
    "BEGIN:VTIMEZONE",
    "TZID:UTC",
    "BEGIN:STANDARD",
    "DTSTART:19700101T000000",
    "TZOFFSETFROM:+0000",
    "TZOFFSETTO:+0000",
    "TZNAME:UTC",
    "END:STANDARD",
    "END:VTIMEZONE",
]


@dataclass(frozen=True)
class Event:
    uid: str
    day: date
    summary: str
    description: str
    stamp: datetime  # aware; written in UTC
    url: str = ""


def escape_text(value: str) -> str:
    """A TEXT value: backslash, semicolon and comma escaped, newlines as \\n, other controls dropped."""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) != 127))
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line: str) -> str:
    """Fold a content line so no physical line is longer than 75 octets: continuation lines start with one
    space, which counts toward their 75. A multi-octet UTF-8 character is never split."""
    out: list[str] = []
    current: list[str] = []
    size, limit = 0, LINE_OCTETS
    for ch in line:
        width = len(ch.encode("utf-8"))
        if size + width > limit:
            out.append("".join(current))
            current, size, limit = [], 0, LINE_OCTETS - 1
        current.append(ch)
        size += width
    out.append("".join(current))
    return (CRLF + " ").join(out)


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _day(day: date) -> str:
    return day.strftime("%Y%m%d")


def calendar(name: str, events: Iterable[Event], *, prodid: str = "-//Softwarden//Renewals calendar//EN") -> bytes:
    """The whole calendar as UTF-8 bytes."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{prodid}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_text(name)}",
        "X-PUBLISHED-TTL:PT6H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
    ]
    written = 0
    for e in events:
        written += 1
        lines += [
            "BEGIN:VEVENT",
            f"UID:{escape_text(e.uid)}",
            f"DTSTAMP:{_utc(e.stamp)}",
            f"DTSTART;VALUE=DATE:{_day(e.day)}",
            f"DTEND;VALUE=DATE:{_day(e.day + timedelta(days=1))}",
            f"SUMMARY:{escape_text(e.summary)}",
            f"DESCRIPTION:{escape_text(e.description)}",
            *([f"URL:{e.url}"] if e.url else []),
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    if not written:
        # RFC 5545 §3.6: a calendar has at least one component. With no deadlines, a UTC time zone definition is the
        # smallest valid one, and it adds nothing to anyone's calendar.
        lines += EMPTY_COMPONENT
    lines.append("END:VCALENDAR")
    return (CRLF.join(fold(line) for line in lines) + CRLF).encode("utf-8")
