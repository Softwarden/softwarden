"""HaloITSM's token endpoint and its read API, for a HaloConnection's test (core/connections.py) and the Halo discovery
source (discovery/halo.py; re-implemented on core/outbound.py).

Auth is OAuth 2.0 client credentials: `POST {base_url}/auth/token` with a form body `grant_type=client_credentials`,
`client_id`, `client_secret` and `scope` (default `read:assets`, all the source reads); a success is JSON with
`access_token` and `expires_in`. These conventions come from Halo's integration guides; an instance's
own reference is at `{base_url}/apidoc`. Reads are `GET {base_url}/api/{Resource}` with the bearer token, paged with
`paginate=true&page_size=N&page_no=N`; assets answer `{"record_count", "assets": [...]}` (not Microsoft, so not on
Microsoft Learn: confirm against the instance's /apidoc during live acceptance).

Every call goes through core/outbound (post for the token, get for reads) with `private_ok=True` (an on-premises Halo may be on a private network;
loopback, link-local and metadata addresses are refused, on save and on every resolved address, no redirects, a
deadline). The host `halo.simulated.invalid` (a reserved name that never resolves) selects SimulatedTransport, so
tests and the demo never leave the machine. Errors never carry the secret, the token or the server's text."""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any, Protocol

from . import outbound, simulation
from .graph import HostRefused, Reply, Unreachable

SIMULATED_HOST = "halo.simulated.invalid"
SIMULATED_URL = f"https://{SIMULATED_HOST}"
TOKEN_PATH = "/auth/token"  # noqa: S105 (a URL path, not a password)
DEFAULT_SCOPE = "read:assets"
TIMEOUT = 20
CODE = re.compile(r"^[A-Za-z0-9_]{1,60}$")  # an OAuth error code fit to store and show; anything else is dropped
TOKEN = "simulated-halo-token"  # noqa: S105 (a synthetic value the simulator issues)
ASSETS_PATH = "/api/Asset"
READ_BODY_MAX = 8 * 1024 * 1024  # one page of 100 assets is far smaller


class HaloError(Exception):
    """The token request failed. `status` is the HTTP status (0 when nothing answered), `code` the OAuth error code
    Halo sent (`invalid_client`, `invalid_scope`, ...), `refused` True when the address check refused the host."""

    def __init__(self, status: int = 0, code: str = "", *, refused: bool = False, reason: str = ""):
        self.status = status
        self.code = code
        self.refused = refused
        self.reason = reason
        super().__init__(f"{status} {code}".strip() or reason)


class Transport(Protocol):
    def post_form(self, url: str, form: Mapping[str, str]) -> Reply: ...


class HttpTransport:
    """core/outbound under the service rule (private networks allowed). `send` is core/graph.Transport's shape, so the
    discovery client (discovery/graph_source.Client) reads Halo with the same paging, throttling and deadline code; it
    only reads (GET), and an address the guard refuses is graph.HostRefused (nothing was sent)."""

    def post_form(self, url: str, form: Mapping[str, str]) -> Reply:
        body = urllib.parse.urlencode(dict(form)).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        return outbound.post(url, body, headers, timeout=TIMEOUT, private_ok=True)

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Reply:
        if method != "GET" or body is not None:
            raise HostRefused("Halo is only read.")
        try:
            return outbound.get(url, dict(headers), timeout=TIMEOUT, body_max=READ_BODY_MAX)
        except outbound.UnsafeDestination:
            raise HostRefused(outbound.SERVICE_REFUSED) from None


class SimulatedTransport:
    """A synthetic Halo token endpoint: `simulated-halo-token` for any client, except the secret `wrong-secret` (401
    `invalid_client`) and a scope containing `bad` (400 `invalid_scope`). Anything else is a 404. With `echo` (the
    connector config's `simulated_token_echo`, tests only) every refusal repeats the client secret, so a test can prove
    no message keeps it."""

    def __init__(self, echo: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.echo = echo

    def post_form(self, url: str, form: Mapping[str, str]) -> Reply:
        parts = urllib.parse.urlsplit(url)
        self.calls.append(("POST", url))
        if parts.hostname != SIMULATED_HOST or parts.path != TOKEN_PATH:
            return Reply(404, {}, b"")
        if form.get("grant_type") != "client_credentials" or not form.get("client_secret"):
            return _oauth_error(400, "invalid_request")
        if self.echo:  # a refusal that repeats the secret: it must never be kept
            body = {"error": "invalid_client", "error_description": f"Bad client secret {form['client_secret']}"}
            return Reply(401, {"content-type": "application/json"}, json.dumps(body).encode())
        if form["client_secret"] == "wrong-secret":  # noqa: S105 (lets a test see a refused secret)
            return _oauth_error(401, "invalid_client")
        if "bad" in form.get("scope", ""):
            return _oauth_error(400, "invalid_scope")
        return Reply(200, {}, json.dumps({"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600}).encode())


def _oauth_error(status: int, code: str) -> Reply:
    return Reply(status, {"content-type": "application/json"}, json.dumps({"error": code}).encode())


def is_simulated(base_url: str) -> bool:
    return (urllib.parse.urlsplit(base_url).hostname or "").lower() == SIMULATED_HOST


def transport_for(base_url: str) -> Transport:
    """The synthetic Halo for its reserved host (refused unless core/simulation.allowed()), else the real one."""
    if is_simulated(base_url):
        if not simulation.allowed():
            raise HaloError(refused=True, reason=simulation.MESSAGE)
        return SimulatedTransport()
    return HttpTransport()


def token_url(base_url: str) -> str:
    return base_url.rstrip("/") + TOKEN_PATH


def request_token(
    transport: Transport, base_url: str, client_id: str, secret: str, scope: str = DEFAULT_SCOPE
) -> dict[str, Any]:
    """The token endpoint's JSON (`access_token`, `expires_in`). HaloError otherwise; never includes the secret."""
    form = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret, "scope": scope}
    try:
        reply = transport.post_form(token_url(base_url), form)
    except outbound.UnsafeDestination as e:
        raise HaloError(refused=True, reason=str(e)) from None
    except Unreachable as e:
        raise HaloError(reason=str(e)) from None
    body = reply.json()
    if not reply.ok:
        error = body.get("error") if isinstance(body, dict) else None
        raise HaloError(reply.status, error if isinstance(error, str) and CODE.match(error) else "")
    if not isinstance(body, dict) or not body.get("access_token"):
        raise HaloError(reply.status, "no_token")
    return body
