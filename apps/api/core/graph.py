"""Microsoft Graph and the Microsoft identity platform over HTTPS: the one client discovery (discovery/entra.py),
sign-in (core/oidc.py, through entra.HttpTransport) and Graph mail (notify/channels.py) share.

Verified on Microsoft Learn on 2026-11-05 (docs/PERMISSIONS.md, "Email via Microsoft Graph"):

- Tokens: client credentials at `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`, scope
  `https://graph.microsoft.com/.default`. A token is issued even when no permission is granted: only a call proves one.
- `POST /v1.0/users/{sender}/sendMail` answers **202 Accepted with an empty body**: accepted, not delivered.
- Errors are `{"error": {"code", "message", "innerError" | "innererror"}}`; keys are read case-insensitively and code
  should be matched on `error.code`.
- 429 and 503 carry `Retry-After` (seconds; an HTTP date is accepted too).
- There is no idempotency header. A caller may send its own GUID as `client-request-id` (Graph echoes it in logs and
  responses) but must dedupe itself.

The transport is urllib with fixed hosts, https only, no redirects and a 30 s timeout. It returns every HTTP answer as a
`Reply` (status, lower-cased headers, body bytes) and raises only when there is no answer: `HostRefused` before
anything is sent, `Unreachable` otherwise. `Unreachable.ambiguous` is True when the request may have reached the
server (the connection broke or timed out while waiting for the answer), so a caller sending mail must not retry it.
"""

from __future__ import annotations

import email.utils
import http.client
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

LOGIN_HOST = "login.microsoftonline.com"
# A directory (tenant) ID is a GUID or one of the tenant's domain names; it is the only input that shapes a token URL.
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
TENANT_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)
GRAPH_HOST = "graph.microsoft.com"
HOSTS = frozenset({LOGIN_HOST, GRAPH_HOST})
GRAPH_SCOPE = f"https://{GRAPH_HOST}/.default"
TIMEOUT = 30  # per socket operation
DEADLINE = 60.0  # seconds to read a whole answer once the request is written (hosts are Microsoft's, fixed)
MAX_BODY = 16 * 1024 * 1024  # Graph pages are far smaller; refuse to buffer anything larger
# Of Microsoft's error message kept in GraphError.message, in memory only: it is never stored, shown or logged. Long
# enough for ARM's AuthorizationFailed text, whose action and scope discovery/graph_source.py parses out.
MESSAGE_MAX = 1000


@dataclass(frozen=True)
class Reply:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)  # keys lower-cased
    body: bytes = b""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        """The body as JSON, or None when it is empty or isn't JSON (a 202 from sendMail has no body)."""
        if not self.body.strip():
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None


class HostRefused(Exception):
    """The URL isn't https on one of the allowed hosts. Nothing was sent."""


class Unreachable(Exception):
    """No HTTP answer. `ambiguous` is True when the request may already have reached the server (the answer timed out
    or the connection dropped after the request was written): its outcome is unknown. The message names the failure's
    type only."""

    def __init__(self, reason: str, *, ambiguous: bool):
        super().__init__(reason)
        self.ambiguous = ambiguous


class GraphError(Exception):
    """A non-2xx answer from Graph or the token endpoint, parsed from `{"error": {...}}` (case-insensitive keys). The
    token endpoint's OAuth errors (`{"error": "invalid_client", "error_description": ...}`) are read too."""

    def __init__(self, status: int, code: str = "", message: str = "", retry_after: float | None = None):
        self.status = status
        self.code = code
        self.message = message[:MESSAGE_MAX]
        self.retry_after = retry_after
        super().__init__(f"{status} {code}".strip())

    @property
    def throttled(self) -> bool:
        """429 or 503: Graph didn't accept the request and says when to try again (Retry-After)."""
        return self.status in (429, 503)


def _lower(mapping: Any) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in mapping.items()} if isinstance(mapping, dict) else {}


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds to wait from a Retry-After header: delta-seconds or an HTTP date. None when absent or unreadable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def error_of(reply: Reply) -> GraphError:
    body = _lower(reply.json())
    retry_after = parse_retry_after(reply.headers.get("retry-after"))
    error = body.get("error")
    if isinstance(error, dict):
        inner = _lower(error)
        return GraphError(reply.status, str(inner.get("code") or ""), str(inner.get("message") or ""), retry_after)
    if isinstance(error, str):  # the token endpoint's OAuth error
        return GraphError(reply.status, error, str(body.get("error_description") or ""), retry_after)
    return GraphError(reply.status, "", "", retry_after)


# ---- transports -------------------------------------------------------------------------------------------------


class Transport(Protocol):
    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Reply: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a redirect could lead anywhere (SSRF)
        return None


def _read(response: Any, started: float) -> bytes:
    """The body in chunks, refusing (TimeoutError) once DEADLINE has passed since the request began, so an answer
    trickled a byte at a time can't hold a worker for long."""
    chunks: list[bytes] = []
    size = 0
    while size < MAX_BODY:
        chunk = response.read(min(65536, MAX_BODY - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if time.monotonic() - started > DEADLINE:
            raise TimeoutError("the answer took too long")
    return b"".join(chunks)


class HttpTransport:
    """urllib with fixed hosts, https only, no redirects, a timeout, and no proxies beyond the system's."""

    def __init__(self, hosts: frozenset[str] = HOSTS, timeout: float = TIMEOUT):
        self.hosts = hosts
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_NoRedirect)

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Reply:
        parts = urllib.parse.urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            port = -1
        if (
            parts.scheme != "https"
            or parts.hostname not in self.hosts
            or parts.username
            or parts.password
            or port not in (None, 443)
        ):
            raise HostRefused("Refused to call a host other than Microsoft's.")
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)  # noqa: S310 (checked above)
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:  # noqa: S310 (https and fixed hosts only)
                return Reply(response.status, _lower(dict(response.headers)), _read(response, started))
        except urllib.error.HTTPError as e:  # every status is an answer, including 3xx (redirects are not followed)
            try:
                content = _read(e, started)
            except (OSError, http.client.HTTPException):
                content = b""
            return Reply(e.code, _lower(dict(e.headers or {})), content)
        except urllib.error.URLError as e:
            # Raised while connecting or writing the request (urllib wraps those): the server never answered, and an
            # incomplete request isn't acted on.
            reason = e.reason
            if isinstance(reason, ssl.SSLError | ssl.CertificateError):
                raise Unreachable("TLS handshake failed", ambiguous=False) from None
            raise Unreachable(
                type(reason).__name__ if isinstance(reason, BaseException) else "URLError", ambiguous=False
            ) from None
        except (TimeoutError, OSError, http.client.HTTPException) as e:
            # Raised while waiting for or reading the answer: the request was written and may have been acted on.
            raise Unreachable(type(e).__name__, ambiguous=True) from None


# ---- calls ------------------------------------------------------------------------------------------------------


def valid_tenant(value: str) -> bool:
    """A GUID or a domain name (the synthetic tenant `simulated` is the caller's to allow)."""
    return bool(GUID.match(value or "")) or bool(TENANT_DOMAIN.match(value or ""))


def token_url(tenant: str) -> str:
    return f"https://{LOGIN_HOST}/{urllib.parse.quote(tenant, safe='')}/oauth2/v2.0/token"


def request_token(
    transport: Transport, tenant: str, client_id: str, secret: str, scope: str = GRAPH_SCOPE
) -> dict[str, Any]:
    """The token endpoint's JSON for client credentials (`access_token`, `expires_in`) for one resource's `scope`
    (`{resource}/.default`: Graph by default; Azure Resource Manager, a Dataverse environment or the Power Platform
    admin API for discovery, discovery/graph_source.Audience). GraphError for a refusal or a reply without a token;
    Unreachable when nothing answered."""
    form = urllib.parse.urlencode(
        {"client_id": client_id, "client_secret": secret, "scope": scope, "grant_type": "client_credentials"}
    ).encode()
    reply = transport.send(
        "POST",
        token_url(tenant),
        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        form,
    )
    if not reply.ok:
        raise error_of(reply)
    body = reply.json()
    if not isinstance(body, dict) or not body.get("access_token"):
        raise GraphError(reply.status, "no_token", "The token endpoint answered without a token.")
    return body


def post_json(
    transport: Transport,
    url: str,
    bearer: str,
    payload: Any,
    client_request_id: uuid.UUID | str | None = None,
) -> Reply:
    """POST JSON with a bearer token. Returns the reply for any 2xx (an empty 202 included); GraphError otherwise. Sends
    `client-request-id` (and asks Graph to echo it) when one is given."""
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json", "Accept": "application/json"}
    if client_request_id:
        headers["client-request-id"] = str(client_request_id)
        headers["return-client-request-id"] = "true"
    reply = transport.send("POST", url, headers, json.dumps(payload).encode("utf-8"))
    if not reply.ok:
        raise error_of(reply)
    return reply


def get_json(transport: Transport, url: str, headers: Mapping[str, str]) -> Any:
    """GET and parse JSON. GraphError for a non-2xx answer."""
    reply = transport.send("GET", url, {"Accept": "application/json", **headers})
    if not reply.ok:
        raise error_of(reply)
    return reply.json()


def send_mail_url(sender: str) -> str:
    return f"https://{GRAPH_HOST}/v1.0/users/{urllib.parse.quote(sender, safe='@')}/sendMail"


def send_mail(
    transport: Transport, bearer: str, sender: str, message: dict[str, Any], client_request_id: uuid.UUID | str
) -> Reply:
    """`POST /users/{sender}/sendMail` with `saveToSentItems: false`. Returns the 202 reply (accepted, not delivered)."""
    return post_json(
        transport, send_mail_url(sender), bearer, {"message": message, "saveToSentItems": False}, client_request_id
    )


# ---- Throttling (discovery) -------------------------------------------------------------------------------------
#
# Verified on Microsoft Learn (https://learn.microsoft.com/en-us/graph/throttling): a throttled request is answered 429 TooManyRequests with `Retry-After` in seconds; wait that long
# and retry, and with no Retry-After back off exponentially. 503 may carry Retry-After too. Immediate retries count
# against the limit, so every retry waits.

RETRY_STATUSES = (429, 503)
BACKOFF_BASE = 2.0  # seconds before the first retry when Retry-After is absent; doubles each time
MAX_WAIT = 120.0  # the longest wait Softwarden accepts: a Retry-After above it stops the run instead
MAX_ATTEMPTS = 8  # retries of one request
RUN_BUDGET = 900.0  # seconds a whole run may spend waiting on throttling before it stops

# Module-level so tests can replace them (no test ever sleeps).
sleep = time.sleep
monotonic = time.monotonic


class ThrottledOut(Exception):
    """Graph kept throttling and the run stops rather than wait. `status` is the last answer's (429 or 503); `reason`
    is `budget` (the waiting budget or the run's deadline would pass), `attempts` (too many retries of one request)
    or `long_wait` (Retry-After asked for longer than MAX_WAIT; retrying earlier would only count against the limit).
    `retry_after` is what Graph asked for, if anything. No server text."""

    def __init__(self, status: int, reason: str = "budget", retry_after: float | None = None):
        super().__init__(f"throttled ({status}, {reason})")
        self.status = status
        self.reason = reason
        self.retry_after = retry_after


class Budget:
    """The cap on waiting for one run (or one access check): every retry of every request draws on it. `until` (a
    monotonic time) also stops waits that would pass a deadline the caller holds, such as the run's wall-clock limit.
    `waited` is the total slept so far."""

    def __init__(
        self,
        seconds: float = RUN_BUDGET,
        max_attempts: int = MAX_ATTEMPTS,
        max_wait: float = MAX_WAIT,
        until: float | None = None,
    ):
        self.deadline = monotonic() + seconds
        if until is not None:
            self.deadline = min(self.deadline, until)
        self.max_attempts = max_attempts
        self.max_wait = max_wait
        self.waited = 0.0
        self.retries = 0

    def delay(self, attempt: int, retry_after: float | None) -> float:
        """Retry-After when the answer gave one, else exponential backoff (capped at max_wait)."""
        if retry_after is not None:
            return max(retry_after, 0.0)
        return min(BACKOFF_BASE * (2**attempt), self.max_wait)

    def why_not(self, attempt: int, retry_after: float | None) -> str:
        """Why retry number `attempt` (0-based) can't happen, or "" when it can."""
        if attempt >= self.max_attempts:
            return "attempts"
        if retry_after is not None and retry_after > self.max_wait:
            return "long_wait"
        if monotonic() + self.delay(attempt, retry_after) > self.deadline:
            return "budget"
        return ""

    def wait(self, attempt: int, retry_after: float | None) -> bool:
        """Sleep before retry number `attempt`. False, without sleeping, when it can't happen (`why_not`)."""
        if self.why_not(attempt, retry_after):
            return False
        seconds = self.delay(attempt, retry_after)
        sleep(seconds)
        self.waited += seconds
        self.retries += 1
        return True


def get_json_retrying(
    transport: Transport, url: str, headers: Mapping[str, str], budget: Budget, retry: bool = True
) -> Any:
    """GET and parse JSON, retrying 429 and 503 as Learn says (Retry-After, else exponential backoff) within `budget`.
    GraphError for any other non-2xx answer; ThrottledOut when a retry can't happen (`Budget.why_not`); Unreachable
    when nothing answered (a GET isn't retried on a broken connection: the next run will try again). With `retry`
    False a throttled answer is raised as its GraphError (with `retry_after`) at once, for a caller that changes the
    request before trying again (the sign-in log reader shrinks its time window, discovery/entra_shadow_it.py)."""
    attempt = 0
    while True:
        reply = transport.send("GET", url, {"Accept": "application/json", **headers})
        if reply.ok:
            return reply.json()
        error = error_of(reply)
        if reply.status not in RETRY_STATUSES or not retry:
            raise error
        reason = budget.why_not(attempt, error.retry_after)
        if reason or not budget.wait(attempt, error.retry_after):
            raise ThrottledOut(reply.status, reason or "budget", error.retry_after)
        attempt += 1
