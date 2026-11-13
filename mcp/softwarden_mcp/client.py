"""The Softwarden REST API client the tools use, and how its problem details read as tool errors.

Every request carries `Authorization: Bearer <API token>` (docs/API.md "Signing in"); the token is never logged, never
put in a message and never sent anywhere but SOFTWARDEN_URL. Errors are RFC 9457 problem details; `ApiError` turns
each one into a sentence a model can act on (the `type` decides, not the `detail`)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any
from urllib.parse import quote

import httpx2

from . import __version__

API_PREFIX = "/api/v1/"
TIMEOUT_SECONDS = 30.0
USER_AGENT = f"softwarden-mcp/{__version__}"

# The capability names an edition may lack (docs/CAPABILITIES.md) in words, for "your edition doesn't include X".
CAPABILITY_WORDS = {
    "ai_agents": "AI agents",
    "assistant": "the discovery assistant",
    "contracts": "contracts",
    "data_quality": "data quality",
    "renewals": "renewals",
    "webhooks": "webhooks",
}


class SoftwardenError(Exception):
    """A refusal or failure, worded for the person (or model) reading the tool's answer."""


class ConfigError(SoftwardenError):
    """SOFTWARDEN_URL or SOFTWARDEN_TOKEN is missing or malformed."""


@dataclass
class ApiError(SoftwardenError):
    """An error response from the API, with the problem's `type` slug (`invalid-token`, `read-only-token`, ...)."""

    status: int
    problem: str
    message: str
    body: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


def problem_slug(body: Mapping[str, Any]) -> str:
    raw = str(body.get("type") or "")
    return raw.rsplit("/", 1)[-1] if raw else ""


def field_errors(errors: Any) -> str:
    """`{"name": ["Enter a name."]}` as `name: Enter a name.`, every field, in order."""
    if not isinstance(errors, Mapping):
        return ""
    parts = []
    for name, messages in errors.items():
        text = " ".join(str(m) for m in messages) if isinstance(messages, list) else str(messages)
        parts.append(text if name == "non_field" else f"{name}: {text}")
    return "; ".join(parts)


def explain(status: int, body: Mapping[str, Any], headers: Mapping[str, str]) -> str:
    """The problem as a sentence that says what to do next. Never echoes the token or the request."""
    slug = problem_slug(body)
    detail = str(body.get("detail") or "").strip()
    if status == 401 or slug == "invalid-token":
        return (
            "Softwarden refused the API token: it is unknown, revoked or expired, or its owner is no longer an "
            "active member. Create a new token in Administration > API tokens and configure it here."
        )
    if slug == "read-only-token":
        return (
            "This API token is read-only, so Softwarden refused the change; nothing was changed. Use a token that "
            "isn't read-only, owned by an Editor or Admin, to make changes."
        )
    if slug == "capability-required":
        name = str(body.get("capability") or "")
        words = CAPABILITY_WORDS.get(name, name.replace("_", " ") or "this feature")
        return f"Your Softwarden edition doesn't include {words} (capability `{name}`), so this isn't available."
    if slug == "not-signed-in":
        return "Softwarden received no API token. Set SOFTWARDEN_TOKEN (stdio) or send a bearer token (HTTP)."
    if slug == "forbidden" or (status == 403 and not slug):
        return f"Your role in Softwarden can't do this. {detail}".strip()
    if status == 404:
        return "Softwarden has no such item in this organization (check the ID and the kind)."
    if slug == "precondition-failed" or status == 412:
        # Never the current revision (the problem's `revision`, the ETag): with it in hand a model could retry at once
        # and overwrite an edit it never saw. The only way to the new revision is reading the item again.
        return (
            "The item changed since it was read, so nothing was changed. Read it again with get_item, check the "
            "change still makes sense, and retry with the revision get_item returns."
        )
    if status == 429:
        wait = headers.get("retry-after", "")
        hint = f"retry in {wait} seconds" if wait.isdigit() else "wait a minute and retry"
        return f"Softwarden's rate limit was reached; {hint}."
    if status == 400:
        named = field_errors(body.get("errors"))
        return "Softwarden refused the request: " + (named or detail or "the request needs fixing.")
    if status == 409:
        return f"Softwarden refused the change: {detail or 'it conflicts with what is stored.'}"
    if status >= 500:
        request_id = headers.get("x-request-id", "")
        return "Softwarden had an internal error" + (f" (request {request_id})" if request_id else "") + "; try later."
    return f"Softwarden answered {status}: {detail or 'the request was refused.'}"


def verify_setting(raw: str | None) -> bool | str:
    """SOFTWARDEN_VERIFY_TLS: true (default), false (development only) or the path of a CA bundle (an internal CA)."""
    value = (raw or "true").strip()
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    return value


def normalize_base(url: str) -> str:
    base = url.strip().rstrip("/")
    if base.endswith("/api/v1"):
        base = base.removesuffix("/api/v1")
    if not base.startswith(("https://", "http://")):
        raise ConfigError("SOFTWARDEN_URL must start with https:// (or http:// for a local development server).")
    return base


# A fixed route written in this package's code: lowercase words, digits, dashes and slashes, nothing else.
REVISION = re.compile(r"^[0-9A-Za-z_-]{1,64}$")  # Softwarden's revisions are 32 hex digits
ROUTE = re.compile(r"^[a-z0-9-]+(/[a-z0-9-]+)*$")
Path = str | Sequence[str]


def api_path(path: Path) -> str:
    """The request path under /api/v1/. A string is one of the package's fixed routes (checked against ROUTE); a
    sequence is route words then values, where every value (an ID from a caller) is percent-encoded as one segment,
    so `..`, `/`, `?` or `#` in it can never reach another route."""
    if isinstance(path, str):
        if not ROUTE.match(path):
            raise SoftwardenError(f"Refused an unexpected API route {path!r}.")
        return path
    parts = [quote(str(part), safe="") for part in path]
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise SoftwardenError("Refused an API path with an empty or dot segment.")
    return "/".join(parts)


def no_cookies() -> CookieJar:
    """A jar that never stores a cookie. The API is bearer-token only; a cookie kept by a client that checks other
    people's tokens (the HTTP transport's shared checker) must never ride along with the next person's check."""
    return CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))


@dataclass
class Page:
    """A JSON answer and its ETag (the revision a later If-Match write sends back)."""

    data: Any
    etag: str | None = None


class SoftwardenClient:
    """One API token against one Softwarden. Thread-safe for the sync tools the SDK runs on worker threads."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        verify: bool | str = True,
        host_header: str | None = None,
        transport: httpx2.BaseTransport | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        token = token.strip()
        if not token:
            raise ConfigError("Set SOFTWARDEN_TOKEN to a Softwarden API token (sw_...).")
        self.base_url = normalize_base(base_url)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if host_header:
            headers["Host"] = host_header  # compose: the app is reached as app:8000 under the public host name
        self.http = httpx2.Client(
            base_url=self.base_url + API_PREFIX,
            headers=headers,
            verify=verify,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,  # a redirect would be a misconfiguration; never re-send the token elsewhere
            cookies=no_cookies(),
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, token: str | None = None) -> SoftwardenClient:
        env = os.environ if env is None else env
        url = env.get("SOFTWARDEN_URL", "")
        if not url:
            raise ConfigError("Set SOFTWARDEN_URL to Softwarden's address, for example https://softwarden.example.com.")
        return cls(
            url,
            env.get("SOFTWARDEN_TOKEN", "") if token is None else token,
            verify=verify_setting(env.get("SOFTWARDEN_VERIFY_TLS")),
            host_header=env.get("SOFTWARDEN_HOST_HEADER") or None,
        )

    def close(self) -> None:
        self.http.close()

    def drop_token(self) -> SoftwardenClient:
        """This client without its own token: from now on only `me(token)` (which names one) is meant for it. The HTTP
        transport checks every unknown bearer token through one such client (one connection pool), instead of
        building a client per token before Softwarden has accepted it."""
        self.http.headers.pop("Authorization", None)
        return self

    def me(self, token: str | None = None) -> Page:
        """GET auth/me: who `token` (default: this client's own) acts as."""
        headers = {"Authorization": f"Bearer {token}"} if token is not None else None
        return self._answer(self._send("GET", "auth/me", headers=headers))

    # -- requests

    def _send(self, method: str, path: Path, **kwargs: Any) -> httpx2.Response:
        target = api_path(path)
        try:
            return self.http.request(method, target, **kwargs)
        except httpx2.TimeoutException:
            raise SoftwardenError(f"Softwarden at {self.base_url} didn't answer in time; try again.") from None
        except httpx2.HTTPError as exc:
            # The exception names the URL only (the token is a header); the class says what failed.
            raise SoftwardenError(f"Couldn't reach Softwarden at {self.base_url} ({type(exc).__name__}).") from None

    def _answer(self, response: httpx2.Response) -> Page:
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            slug = problem_slug(body)
            raise ApiError(response.status_code, slug, explain(response.status_code, body, response.headers), body)
        if 300 <= response.status_code < 400:
            raise SoftwardenError(
                f"Softwarden at {self.base_url} redirected the request; set SOFTWARDEN_URL to its final address."
            )
        data = response.json() if response.content else None
        etag = response.headers.get("etag")
        return Page(data, etag.strip('"') if etag else None)

    def get(self, path: Path, **params: Any) -> Page:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        return self._answer(self._send("GET", path, params=clean))

    def write(self, method: str, path: Path, body: Mapping[str, Any], *, revision: str) -> Page:
        """A guarded write: `If-Match` names exactly the one revision the caller read (never `*`, a list or a weak
        tag), so a change made since is a 412."""
        value = revision.strip().strip('"')
        if not REVISION.match(value):
            raise SoftwardenError("Pass the revision get_item returned, exactly as it was returned.")
        headers = {"If-Match": f'"{value}"', "Content-Type": "application/json"}
        return self._answer(self._send(method, path, json=dict(body), headers=headers))
