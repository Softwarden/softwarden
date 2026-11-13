"""What the synthetic Azure and Power Platform tenants share (discovery/azure_simulator.py,
discovery/powerplatform_simulator.py): the token endpoint, which issues one token per audience the way Microsoft does
(the client-credentials `scope` names the resource), and the checks every read makes before it is answered.

- A read whose bearer token isn't the one issued for its host's audience is refused 401 InvalidAuthenticationToken,
  and `sent` records (host, Authorization header) for every read, so a test can prove a token never reached another
  audience's host.
- Config keys shared by both (set by tests, never through the API): `simulated_token_expires` (the first read answers
  401), `simulated_token_echo` (the token endpoint's refusal repeats the client secret), `simulated_errors`
  ({"path": status}: that read answers the status with a message echoing the bearer token), `simulated_throttle`
  ({"path", "times", "retry_after", "status"}), `simulated_page` (items per page; next links follow),
  `simulated_empty` (paths, matched at the end: that read answers 200 with an empty `value`).
- The secret `wrong-secret` is refused (AADSTS7000215). A Dataverse host the tenant doesn't have gets AADSTS500011.

Nothing here reaches the network: the transport implements core/graph.Transport and answers from memory."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping
from typing import Any

from core.graph import GRAPH_SCOPE, LOGIN_HOST, Reply

from . import entra_simulator
from .graph_source import DATAVERSE_HOST

ARM_TOKEN = "simulated-arm-token"  # noqa: S105 (synthetic values the simulator issues)
BAP_TOKEN = "simulated-bap-token"  # noqa: S105
DATAVERSE_TOKEN = "simulated-dataverse-token-"  # noqa: S105 (followed by the environment's host)
TOKENS = (ARM_TOKEN, BAP_TOKEN, DATAVERSE_TOKEN, entra_simulator.TOKEN)


def json_reply(status: int, body: Any, headers: Mapping[str, str] | None = None) -> Reply:
    return Reply(status, {"content-type": "application/json", **(headers or {})}, json.dumps(body).encode())


def error(status: int, code: str, message: str, headers: Mapping[str, str] | None = None) -> Reply:
    return json_reply(status, {"error": {"code": code, "message": message}}, headers)


class MicrosoftSimulator:
    """The token endpoint and the shared checks; a subclass answers its hosts (`answer`) and says which hosts it
    serves and which Dataverse environments exist (`dataverse_hosts`)."""

    HOSTS: frozenset[str] = frozenset()

    def __init__(self, connector=None, config=None):
        self.config = dict(config if config is not None else getattr(connector, "config", {}) or {})
        self.calls: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []
        self.throttled = 0
        self.expired = False

    def dataverse_hosts(self) -> set[str]:
        return set()

    def token_for_scope(self, scope: str) -> str | None:
        if scope == GRAPH_SCOPE:
            return entra_simulator.TOKEN
        if scope == "https://management.azure.com/.default":
            return ARM_TOKEN
        if scope == "https://service.powerapps.com/.default":
            return BAP_TOKEN
        host = urllib.parse.urlsplit(scope).hostname or ""
        if scope == f"https://{host}/.default" and host in self.dataverse_hosts():
            return DATAVERSE_TOKEN + host
        return None

    def token_for_host(self, host: str) -> str | None:
        if host == "management.azure.com":
            return ARM_TOKEN
        if host == "api.bap.microsoft.com":
            return BAP_TOKEN
        if DATAVERSE_HOST.fullmatch(host) and host in self.dataverse_hosts():
            return DATAVERSE_TOKEN + host
        return None

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Reply:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        self.calls.append((method, url))
        if parts.scheme != "https":
            return error(400, "BadRequest", "The simulator only answers https.")
        if host == LOGIN_HOST:
            if method == "POST" and parts.path.endswith("/oauth2/v2.0/token"):
                return self._token(body or b"")
            return error(400, "BadRequest", "The simulator only issues tokens.")
        if method != "GET":
            return error(400, "BadRequest", "The simulator only reads.")
        bearer = headers.get("Authorization", "")
        self.sent.append((host, bearer))
        expected = self.token_for_host(host)
        if expected is None:
            return error(400, "BadRequest", "The simulator doesn't answer this host.")
        if bearer != f"Bearer {expected}":
            return error(401, "InvalidAuthenticationToken", "The access token is invalid for this audience.")
        if self.config.get("simulated_token_expires") and not self.expired:
            self.expired = True
            return error(401, "ExpiredAuthenticationToken", "The access token expiry has passed.")
        path = parts.path
        errors = self.config.get("simulated_errors") or {}
        if path in errors:  # echoes the bearer token, which must never reach a message
            return error(int(errors[path]), "BadRequest", f"Bad request with {bearer}")
        throttle = self.config.get("simulated_throttle") or {}
        if throttle.get("path") == path and self.throttled < int(throttle.get("times", 1)):
            self.throttled += 1
            retry = throttle.get("retry_after")
            status = int(throttle.get("status", 429))
            return error(status, "TooManyRequests", "Too many requests.", {"retry-after": str(retry)} if retry else {})
        if any(path.endswith(p) for p in self.config.get("simulated_empty") or ()):
            return json_reply(200, {"value": []})
        return self.answer(host, path, urllib.parse.parse_qs(parts.query), headers)

    def answer(self, host: str, path: str, query: dict[str, list[str]], headers: Mapping[str, str]) -> Reply:
        raise NotImplementedError

    def page(
        self, host: str, path: str, query: dict[str, list[str]], items: list[Any], next_key: str, default: int = 100
    ) -> Reply:
        """`items` a page at a time (`simulated_page`, or `$top` when smaller), with `next_key` to the rest; the
        next link points at another host when `simulated_foreign_next` is on."""
        size = int(self.config.get("simulated_page") or default)
        top = query.get("$top")
        if top:
            size = min(size, int(top[0]))
        skip = int((query.get("$skiptoken") or ["0"])[0])
        out: dict[str, Any] = {"value": items[skip : skip + size]}
        if skip + size < len(items) and not top:
            rest = {k: v[0] for k, v in query.items() if k != "$skiptoken"}
            rest["$skiptoken"] = str(skip + size)
            target = "evil.example.com" if self.config.get("simulated_foreign_next") else host
            out[next_key] = f"https://{target}{path}?{urllib.parse.urlencode(rest, safe='$(),=')}"
        return json_reply(200, out)

    def _token(self, body: bytes) -> Reply:
        form = urllib.parse.parse_qs(body.decode())
        secret = (form.get("client_secret") or [""])[0]
        scope = (form.get("scope") or [""])[0]
        if (form.get("grant_type") or [""])[0] != "client_credentials" or not secret:
            return json_reply(400, {"error": "invalid_request"})
        if self.config.get("simulated_token_echo"):  # a refusal that repeats the secret: it must never be kept
            return json_reply(400, {"error": "invalid_request", "error_description": f"AADSTS900144: bad {secret}"})
        if secret == "wrong-secret":  # noqa: S105 (lets a test see a refused secret)
            return json_reply(
                401, {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided."}
            )
        token = self.token_for_scope(scope)
        if token is None:
            return json_reply(
                400,
                {
                    "error": "invalid_resource",
                    "error_description": f"AADSTS500011: The resource principal named {scope} was not found.",
                },
            )
        return json_reply(200, {"access_token": token, "token_type": "Bearer", "expires_in": 3599})
