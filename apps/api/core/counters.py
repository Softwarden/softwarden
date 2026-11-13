"""Shared rate-limit counters (the table is core.models.RateCounter).

Every rate limit in the installation counts here, in PostgreSQL, instead of in Django's per-process cache: with three
gunicorn workers, or two to six pods of three, a per-process limit of 10 sign-ins a minute was 30 to 180. Now a limit
is the installation's, whichever process serves the request.

A counter is a fixed window: `period` seconds aligned to the epoch, numbered `int(now // period)`. `hit` counts one
event with one statement,

    INSERT ... VALUES (..., 1, ...) ON CONFLICT (id) DO UPDATE SET count = count + 1 ... RETURNING count

which PostgreSQL runs atomically under the row's lock, so two processes counting at once both land (the per-process
cache's add-then-incr could lose an update under a concurrent burst). It commits with the
statement when the caller isn't in a transaction (requests aren't: ATOMIC_REQUESTS is off), so a refused request is
counted even though its response rolls back nothing else.

Cost per counted event: one single-row upsert on a primary key (a HOT update in place after the first, the table's
fillfactor is 70). `peek` is one primary-key read. What each request pays is listed in docs/API.md "Rate limits".

`attempt` is the other kind, for limits that must not reset on the clock (failed sign-ins per account, wrong TOTP
codes): one row per key with no window in it, whose count lapses only after `ttl` seconds without an attempt. It
counts the attempt *before* the caller checks the password or code, under the row's lock, and refuses it when the
count so far asks for a wait: so twenty requests at once can't all pass a check made before any of them is
recorded. The caller `forget`s the key on success.

The key is a keyed hash of what is counted (an address, an email, a token id): HMAC-SHA256 under SECRET_KEY with this
module's own salt (Django's salted_hmac), cut to 32 hex digits. No address or email is stored, and the stored keys
can't be matched against a list of addresses or emails without the secret key. Rows outlive their window by
nothing: `prune` (the `counters.prune` schedule, every 10 minutes) deletes every row whose window has ended or whose
`ttl` has lapsed.

`clock` is the one source of time here, so tests can move it."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from django.db import connection, transaction
from django.utils.crypto import salted_hmac

KEY_SALT = "softwarden.core.counters"

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def clock() -> float:
    return time.time()


def parse_rate(rate: str | None) -> tuple[int, int] | None:
    """DRF rate syntax (`10/min`, `600/m`, `10/hour`) -> (events, seconds); None (no limit) for an empty rate."""
    if not rate:
        return None
    count, _, period = str(rate).partition("/")
    return int(count), _UNITS[period.strip()[:1].lower()]


def digest(ident: str) -> str:
    value = str(ident).encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    return salted_hmac(KEY_SALT, value, algorithm="sha256").hexdigest()[:32]


def _row_id(scope: str, key: str, window: int) -> str:
    return f"{scope}:{key}:{window}"


def _at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


@dataclass(frozen=True)
class Hit:
    """One counted event: the window's count including it, the limit, and the seconds until the window ends."""

    count: int
    limit: int | None
    retry_after: int

    @property
    def allowed(self) -> bool:
        return self.limit is None or self.count <= self.limit


def hit(scope: str, ident: str, period: int, limit: int | None = None, now: float | None = None) -> Hit:
    """Count one event for `ident` in `scope`'s current window of `period` seconds; returns the new count."""
    now = clock() if now is None else now
    window = int(now // period)
    key = digest(ident)
    ends = (window + 1) * period
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO core_ratecounter (id, scope, key, window_index, count, updated_at, expires_at) "
            "VALUES (%s, %s, %s, %s, 1, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET count = core_ratecounter.count + 1, updated_at = EXCLUDED.updated_at "
            "RETURNING count",
            [_row_id(scope, key, window), scope, key, window, _at(now), _at(ends)],
        )
        (count,) = cursor.fetchone()
    return Hit(int(count), limit, max(1, math.ceil(ends - now)))


@dataclass(frozen=True)
class Seen:
    """A window's count without counting: how many, when the last one was counted (epoch seconds, None for none) and
    the seconds until the window ends."""

    count: int
    last: float | None
    retry_after: int


def peek(scope: str, ident: str, period: int, now: float | None = None) -> Seen:
    now = clock() if now is None else now
    window = int(now // period)
    ends = (window + 1) * period
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count, updated_at FROM core_ratecounter WHERE id = %s",
            [_row_id(scope, digest(ident), window)],
        )
        row = cursor.fetchone()
    retry = max(1, math.ceil(ends - now))
    if row is None:
        return Seen(0, None, retry)
    return Seen(int(row[0]), row[1].timestamp(), retry)


@dataclass(frozen=True)
class Attempt:
    """One attempt counted by `attempt`: whether it may go ahead, the count including it (or, refused, the count so
    far), and the seconds to wait when refused."""

    allowed: bool
    count: int
    retry_after: int


def attempt(scope: str, ident: str, ttl: int, wait_for: Callable[[int], float], now: float | None = None) -> Attempt:
    """Count one attempt for `ident` in a counter not aligned to the clock, unless it must wait.

    The row (`<scope>:<key>`, no window) holds the attempts so far and when the last was counted; it lapses `ttl`
    seconds after the last one. `wait_for(count)` is how long after the last attempt the next may come, given the
    count so far. Under the row's lock (SELECT ... FOR UPDATE, in a transaction of its own unless the caller has one):
    if that wait hasn't passed the attempt is refused and nothing changes, else it is counted and `updated_at` and
    `expires_at` move. Concurrent attempts are therefore decided one after the other. The caller forgets the key when
    the attempt succeeds, so the count is of failures (and attempts still being checked)."""
    now = clock() if now is None else now
    key = digest(ident)
    row_id = f"{scope}:{key}"
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO core_ratecounter (id, scope, key, window_index, count, updated_at, expires_at) "
            "VALUES (%s, %s, %s, 0, 0, %s, %s) ON CONFLICT (id) DO NOTHING",
            [row_id, scope, key, _at(now), _at(now + ttl)],
        )
        cursor.execute("SELECT count, updated_at, expires_at FROM core_ratecounter WHERE id = %s FOR UPDATE", [row_id])
        count, last, expires = cursor.fetchone()
        count = int(count) if expires.timestamp() > now else 0  # lapsed: start afresh
        if count:
            wait = float(wait_for(count)) - (now - last.timestamp())
            if wait > 0:
                return Attempt(False, count, max(1, math.ceil(wait)))
        cursor.execute(
            "UPDATE core_ratecounter SET count = %s, updated_at = %s, expires_at = %s WHERE id = %s",
            [count + 1, _at(now), _at(now + ttl), row_id],
        )
    return Attempt(True, count + 1, 0)


def forget(scope: str, ident: str) -> int:
    """Delete every row of one key (a successful sign-in clears its account's failures). Returns how many."""
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM core_ratecounter WHERE scope = %s AND key = %s", [scope, digest(ident)])
        return cursor.rowcount


def release(scope: str, ident: str) -> None:
    """Take back one attempt counted by `attempt` that turned out not to be one (the request failed for another
    reason before any secret was checked)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core_ratecounter SET count = GREATEST(count - 1, 0) WHERE id = %s",
            [f"{scope}:{digest(ident)}"],
        )


def reset() -> None:
    """Delete every counter (tests only; one account's sign-in failures: `manage.py signin_limits forget`)."""
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM core_ratecounter")


def prune(now) -> dict[str, int]:
    """The `counters.prune` schedule: delete the counters whose window has ended."""
    from .models import RateCounter

    deleted, _ = RateCounter.objects.filter(expires_at__lte=now).delete()
    return {"rate_counters_pruned": deleted}


def register() -> None:
    from . import jobs

    jobs.register_schedule("counters.prune", prune, "*/10 * * * *", "Delete rate-limit counters whose window ended")
