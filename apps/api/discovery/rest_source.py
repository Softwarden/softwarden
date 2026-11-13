"""What the Okta and Google Workspace sources share (discovery/okta.py, discovery/google_workspace.py): a
read-only JSON client for one vendor API per run, the way discovery/graph_source.Client is for Microsoft, plus the
access check's steps for it.

- Sign-in is the source's own (`sign_in`, a callable that returns an access token or raises SignInRefused); a token is
  recorded for redaction (graph_source.note_issued) and fetched again once after a 401.
- Every URL, first page or next-page link, must be https on one of the service's origins before the token is attached;
  a link to another host stops the run.
- The wall-clock deadline and the throttling budget are core/graph's (graph.Budget), checked before every request, and
  the run's lease is renewed at every request (graph_source.at_checkpoint).
- Rate limits are honoured, not just survived. Okta answers every call with `X-Rate-Limit-Limit`,
  `X-Rate-Limit-Remaining` and `X-Rate-Limit-Reset` (UTC epoch seconds; developer.okta.com/docs/reference/
  rl-best-practices): the limit is shared by every integration of the org, so once Remaining falls below 10 % of
  Limit the next request waits until Reset, leaving the rest for others. A 429 (Okta) or 503 (Google's quota
  answer, developers.google.com/admin-sdk/reports/v1/limits) waits Retry-After when given, else until Okta's Reset,
  else backs off exponentially from the service's base (Google suggests 5 seconds), within the run's budget; a wait
  longer than graph.MAX_WAIT stops the run rather than retry early.
- Failures are SourceError (Refused for an error status) in Softwarden's words with the HTTP status and the service's
  error code only, never its message text, a token or a key.
- Okta pages through the `link` header's rel="next" URL (followed as given, never built; Okta's pagination guide);
  Google through `nextPageToken` (the source passes `pageToken`)."""

from __future__ import annotations

import math
import re
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from core import connections, graph

from . import access
from .graph_source import (
    RUN_DEADLINE,
    DeadlineReached,
    Permission,
    Refused,
    SignInRefused,
    SourceError,
    at_checkpoint,
    note_issued,
)

# Overridable in tests: the wall clock Okta's X-Rate-Limit-Reset is compared with.
wall = time.time
# Okta's rate limit is the org's: once fewer than this share of X-Rate-Limit-Limit remain, the next request waits
# for X-Rate-Limit-Reset (without a limit header, once none remain).
PACE_BELOW = 0.1
LINK = re.compile(r"<([^<>]*)>([^,<]*)")
NEXT = re.compile(r"""\brel\s*=\s*"?next"?""", re.I)


@dataclass(frozen=True)
class Service:
    """One vendor API: its name in messages, the (host, port) pairs a token may go to, the first backoff without
    Retry-After, how to read an error code out of an error body, and which other answers (beyond 429 and 503) are
    the vendor's documented quota errors, retried as throttling rather than reported as a refusal."""

    label: str
    origins: frozenset[tuple[str, int]]
    backoff: float
    error_code: Callable[[Any], str] = field(repr=False)
    throttled: Callable[[int, Any], bool] = field(default=lambda status, body: False, repr=False)

    def allows(self, url: str) -> bool:
        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port or 443
        except ValueError:
            return False
        host = (parts.hostname or "").lower()
        return (
            parts.scheme == "https"
            and parts.username is None
            and parts.password is None
            and (host, port) in self.origins
        )


@dataclass(frozen=True)
class Answer:
    body: Any
    headers: Mapping[str, str]


def next_link(header: str) -> str:
    """The rel="next" URL of a `link` header (one or several values, joined with commas), or ""."""
    for found in LINK.finditer(header or ""):
        if NEXT.search(found.group(2)):
            return found.group(1).strip()
    return ""


def _seconds(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


class Api:
    """Reads one vendor API for one run (or one access check)."""

    def __init__(
        self,
        transport,
        service: Service,
        sign_in: Callable[[], str],
        budget_seconds: float = graph.RUN_BUDGET,
        deadline: float = RUN_DEADLINE,
    ):
        self.transport = transport
        self.service = service
        self.sign_in = sign_in
        self.limit = deadline
        self.deadline = graph.monotonic() + deadline
        self.budget = graph.Budget(budget_seconds, until=self.deadline)
        self.paused_until = 0.0  # monotonic: Okta said the rate-limit window is used up until then
        self._token = ""

    @property
    def label(self) -> str:
        return self.service.label

    def token(self) -> str:
        if not self._token:
            self._token = self.sign_in()
            note_issued(self._token)
        return self._token

    # ---- rate limits ------------------------------------------------------------------------------------------------

    def _reset_in(self, headers: Mapping[str, str]) -> int | None:
        """Seconds until Okta's X-Rate-Limit-Reset, at least 1, or None without one."""
        reset = _seconds(headers.get("x-rate-limit-reset"))
        if reset is None:
            return None
        return max(1, math.ceil(reset - wall()))

    def _note_limits(self, headers: Mapping[str, str]) -> None:
        """Pause until the window resets once fewer than PACE_BELOW of the limit remain: the limit is the org's, shared
        with every other integration, so Softwarden leaves them a tenth of it rather than spend it to 0."""
        remaining = _seconds(headers.get("x-rate-limit-remaining"))
        if remaining is None:
            return
        limit = _seconds(headers.get("x-rate-limit-limit"))
        floor = limit * PACE_BELOW if limit else 1
        if remaining < floor:
            wait = self._reset_in(headers)
            if wait is not None:
                self.paused_until = graph.monotonic() + wait

    def _pace(self, what: str) -> None:
        """Wait until the rate-limit window Okta said was used up resets, within the budget."""
        wait = self.paused_until - graph.monotonic()
        if wait <= 0:
            return
        if wait > self.budget.max_wait:
            raise SourceError(
                f"{self.label}'s rate limit for this app resets in {round(wait)} seconds, too long to wait while "
                f"trying to {what}; the run stopped. The next run will try again."
            )
        if graph.monotonic() + wait > self.budget.deadline:
            raise SourceError(
                f"{self.label} kept the run at its rate limit past this run's waiting budget while trying to {what}. "
                "The next run will try again."
            )
        graph.sleep(wait)
        self.budget.waited += wait
        self.paused_until = 0.0

    def _throttled_out(self, status: int, reason: str, wait: float | None, what: str) -> SourceError:
        if reason == "long_wait":
            return SourceError(
                f"{self.label} asked to wait {round(wait or 0)} seconds ({status}) while trying to {what}; the run "
                "stopped rather than retry early. The next run will try again."
            )
        if reason == "attempts":
            return SourceError(
                f"{self.label} kept throttling one request ({status}) while trying to {what}. The next run will try "
                "again."
            )
        return SourceError(
            f"{self.label} kept throttling requests ({status}) past this run's waiting budget. The next run will try "
            "again."
        )

    # ---- reads ------------------------------------------------------------------------------------------------------

    def get(self, url: str, what: str, permission: Permission | None) -> Answer:
        """One GET as JSON. SourceError on any failure, in words that name the missing grant."""
        if not self.service.allows(url):
            raise SourceError(f"{self.label} returned a next-page link to an unexpected host; the run stopped.")
        attempt, signed_again = 0, False
        while True:
            at_checkpoint()
            if graph.monotonic() > self.deadline:
                raise DeadlineReached(
                    f"The run stopped at its {round(self.limit / 60)}-minute limit while trying to {what} "
                    f"({self.label} was slow or throttling). The next run will try again."
                )
            self._pace(what)
            headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token()}"}
            try:
                reply = self.transport.send("GET", url, headers)
            except graph.HostRefused as e:
                raise SourceError(f"Refused to call the {self.label} address: {e}") from None
            except graph.Unreachable:
                raise SourceError(f"Couldn't reach {self.label} from this server.") from None
            self._note_limits(reply.headers)
            if reply.ok:
                body = reply.json()
                if body is None:
                    raise SourceError(f"{self.label} answered without the expected JSON while trying to {what}.")
                return Answer(body, reply.headers)
            body = reply.json()
            code = connections.safe_code(self.service.error_code(body))
            if reply.status == 401 and not signed_again:  # an expired or revoked token: sign in again, once
                self._token, signed_again = "", True
                continue
            if reply.status in graph.RETRY_STATUSES or self.service.throttled(reply.status, body):
                given = graph.parse_retry_after(reply.headers.get("retry-after"))
                if given is None and reply.status == 429:
                    reset = self._reset_in(reply.headers)
                    given = float(reset) if reset is not None else None
                wait = given if given is not None else min(self.service.backoff * (2**attempt), graph.MAX_WAIT)
                reason = self.budget.why_not(attempt, wait)
                if reason or not self.budget.wait(attempt, wait):
                    raise self._throttled_out(reply.status, reason or "budget", given, what)
                attempt += 1
                continue
            raise self.refusal(reply.status, code, what, permission)

    def refusal(self, status: int, code: str, what: str, permission: Permission | None) -> Refused:
        tag = f"{status} {code}".strip()
        label = self.label
        if status == 401:
            return Refused(
                f"{label} didn't accept the token while trying to {what} ({tag}). Test the connection.", status, code
            )
        if status == 403:
            advice = f" {permission.advice}" if permission and permission.advice else ""
            named = f": it needs {permission.name}" if permission else ""
            return Refused(f"{label} refused to {what} ({tag}){named}.{advice}", status, code, missing=True)
        if status == 404:
            return Refused(f"{label} couldn't find what it was asked to {what} ({tag}).", status, code)
        return Refused(f"{label} refused to {what} ({tag}).", status, code)

    def time_left(self) -> float:
        """Seconds until the run's deadline."""
        return self.deadline - graph.monotonic()

    def get_link_pages(self, url: str, what: str, permission: Permission | None, page_limit: int) -> list[dict]:
        """Every object of a JSON-array collection paged through the `link` header (Okta)."""
        out: list[dict] = []
        seen: set[str] = set()
        for _ in range(page_limit):
            seen.add(url)
            answer = self.get(url, what, permission)
            if not isinstance(answer.body, list):
                raise SourceError(f"{self.label} answered without the expected list while trying to {what}.")
            out.extend(item for item in answer.body if isinstance(item, dict))
            url = next_link(answer.headers.get("link", ""))
            if not url:
                return out
            if url in seen:
                raise SourceError(f"{self.label} answered with the same page again while trying to {what}; stopped.")
        raise SourceError(f"{self.label} has more than a run reads while trying to {what}; narrow the scope.")


# ---- access check steps ---------------------------------------------------------------------------------------------


def sign_in_step(api: Api, permission: Permission | None = None) -> dict[str, Any]:
    """The token as a step ("Sign in to Okta"); a refusal that means a grant is missing names `permission`."""
    name = f"Sign in to {api.label}"
    try:
        api.token()
    except SignInRefused as e:
        named = permission if e.problem == "missing_permission" else None
        return access.step(
            "credentials", name, "failed", permission=named, problem=e.problem, code=e.code, detail=str(e)
        )
    except SourceError as e:
        return access.step("credentials", name, "failed", problem="error", detail=str(e))
    return access.step("credentials", name, "ok", detail=f"{api.label} accepted the credentials.")


def probe(api: Api, name: str, url: str, permission: Permission) -> tuple[dict[str, Any], Any]:
    """One one-item read as a step, and the answer's body when it worked."""
    try:
        answer = api.get(url, name[0].lower() + name[1:], permission)
    except Refused as e:
        if e.missing:
            return access.step(
                "api",
                name,
                "failed",
                permission=permission,
                problem="missing_permission",
                code=e.code,
                detail=str(e)[:300],
                next_step=permission.advice,
            ), None
        problem = {401: "wrong_credentials", 404: "unavailable"}.get(e.status, "error")
        if e.status in (429, 500, 502, 503, 504):
            problem = "unreachable"
        return access.step(
            "api", name, "failed", permission=permission, problem=problem, code=e.code, detail=str(e)
        ), None
    except SourceError as e:
        return access.step(
            "api",
            name,
            "failed",
            permission=permission,
            problem="unreachable",
            detail=str(e),
            next_step="Check the server's outbound internet access and try again.",
        ), None
    return access.step("api", name, "ok", permission=permission, detail=f"{name} works."), answer.body


# The run's error keeps 500 characters, so the whole refusal (listing name included) stays well under that.
EMPTY_LISTING_REMEDY = (
    'If they really were deleted, an Admin can run it once with "accept empty listing" (later runs are checked '
    "again); deleting and re-adding the connector instead loses its record links and dismissals."
)

# A run an Admin started with accept_empty_listing (discovery/engine._execute): the empty-listing guards stand aside
# for that run only.
_accepting_empty: ContextVar[bool] = ContextVar("accepting_empty_listing", default=False)


@contextmanager
def accepting_empty_listing(on: bool) -> Iterator[None]:
    token = _accepting_empty.set(on)
    try:
        yield
    finally:
        _accepting_empty.reset(token)


def empty_listing_accepted() -> bool:
    return _accepting_empty.get()


class EmptyListing(SourceError):
    """The empty-listing guard's refusal. Its run records `failure_code` EMPTY_LISTING, so a
    client can offer the way through (a run with accept_empty_listing) without reading the words."""

    CODE = "empty_listing"


def empty_listing_error(what: str, count: int, things: str = "") -> EmptyListing:
    """The empty-listing guard's refusal: `what` ("Okta's application listing") came back empty while `count` live
    observations would otherwise have gone "no longer seen" (`things` says it instead when it is more than that:
    "3 live observation(s) and 2 discovered credential(s) from it"). It ends with the way through when the listing
    really is empty (EMPTY_LISTING_REMEDY)."""
    things = things or f"{count} live observation(s) from earlier runs"
    return EmptyListing(
        f"{what} came back empty, but this connector has {things}. Nothing was changed: the listing is more likely "
        f"incomplete. Check the connection, then run it again. {EMPTY_LISTING_REMEDY}"
    )


def guard_empty(connector, listed: int, what: str) -> None:
    """The empty-listing guard (Okta): a listing with nothing in it while the connector has live observations (not
    gone) is far more likely incomplete than true, so the run is refused and nothing changes, rather than every
    observation going "no longer seen". (Google Workspace needs none: an empty window changes nothing there.) The
    Microsoft and Halo sources get the same guard from the engine (`EMPTY_LISTING_NAME`, discovery/engine._apply)."""
    from .models import Observation

    if listed or empty_listing_accepted():
        return
    count = Observation.objects.filter(connector=connector, gone_at__isnull=True).count()
    if count:
        raise empty_listing_error(what, count)
