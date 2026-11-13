"""Limits that follow an account or a refused credential, not a view, in the shared
counters (core/counters.py), so they hold whichever process serves the request.

Failed sign-ins per account. The sign-in throttle counts per client address, so one account could be guessed at from
any number of addresses. Every password attempt for an email (an account or not: the answer must not tell them
apart) is also counted under the email, *before* the password is checked (`attempt_account`, under the counter row's
lock, so parallel attempts are decided one after the other), and a successful sign-in forgets the count
(`forget_account`). The first ACCOUNT_FREE attempts cost nothing; after that each must wait `delay(count)` seconds
after the last: 1, 2, 4, 8 ... doubling, at most ACCOUNT_MAX_DELAY. An attempt inside the wait is refused with 429
and Retry-After before the password is checked, the right password included. The count is not tied to clock-aligned
windows: it lapses only after ACCOUNT_IDLE seconds with no attempt for that email, so an attacker gains nothing by
timing a window boundary. Ten failures take at least 31 seconds, so an eleventh can't come within a minute of the
first; sustained guessing is held to one try per ACCOUNT_MAX_DELAY, from however many addresses. Used by password
sign-in (catalog/views.login_view) and the re-authentication of POST platform/impersonation.

Accepted trade-off: anyone who knows an email can make its owner wait up to
ACCOUNT_MAX_DELAY between sign-in attempts, and unknown emails are delayed the same way, so the delay is no oracle for
which emails have accounts. An operator clears one account's count with `manage.py signin_limits forget <email>
--reason ...` (audited in the platform stream).

Refused bearer tokens per client address. An unknown, revoked or expired API or SCIM token is counted under the
client's address; past INVALID_TOKEN_RATE (default 30 a minute) the refusal is a 429 with Retry-After instead of a
401 until the window ends, so a script guessing tokens is told to stop and costs one lookup and one upsert per try.
A valid token is looked up first and never counted or refused, so an address shared with a guesser (an office NAT,
the MCP server, which calls the API for every client) keeps working."""

from __future__ import annotations

from django.conf import settings
from rest_framework import exceptions

from . import counters

ACCOUNT_SCOPE = "signin-account"
ACCOUNT_IDLE = 900  # seconds without an attempt before an account's count lapses
ACCOUNT_FREE = 5  # attempts before any wait
ACCOUNT_MAX_DELAY = 300  # seconds: the longest wait between two attempts
BAD_TOKEN_SCOPE = "bad-token"  # noqa: S105  (a counter scope, not a secret)


def delay(count: int) -> int:
    """Seconds an attempt must wait after the last one, with `count` attempts counted so far."""
    if count < ACCOUNT_FREE:
        return 0
    return min(2 ** (count - ACCOUNT_FREE), ACCOUNT_MAX_DELAY)


def _account(email: str) -> str:
    return (email or "").strip().lower()


def attempt_account(email: str) -> None:
    """Count a password attempt for this account now, or refuse it (429, Retry-After) while it must wait."""
    result = counters.attempt(ACCOUNT_SCOPE, _account(email), ACCOUNT_IDLE, delay)
    if not result.allowed:
        raise exceptions.Throttled(
            wait=result.retry_after, detail="Too many failed sign-ins for this account. Wait before you try again."
        )


def forget_account(email: str) -> int:
    """The attempt succeeded (or an operator said so): forget the account's count. Returns the rows deleted."""
    return counters.forget(ACCOUNT_SCOPE, _account(email))


def release_account(email: str) -> None:
    """The counted attempt never checked a password (the request failed for another reason): take it back."""
    counters.release(ACCOUNT_SCOPE, _account(email))


# ---- refused bearer tokens per address -------------------------------------------------------------------------------


def _bad_token_rate() -> tuple[int, int] | None:
    return counters.parse_rate(settings.INVALID_TOKEN_RATE)


def record_bad_token(address: str) -> int:
    """Count one refused token for this address: 0 while the address is within the rate, else the seconds until its
    window ends (answer 429 instead of 401)."""
    rate = _bad_token_rate()
    if rate is None:
        return 0
    limit, period = rate
    result = counters.hit(BAD_TOKEN_SCOPE, address, period, limit)
    return 0 if result.allowed else result.retry_after
