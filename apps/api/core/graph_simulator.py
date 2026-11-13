"""A synthetic Microsoft Graph for mail (like discovery/entra_simulator.py for discovery): answers the token request and
`sendMail` the way Microsoft Learn documents them, so tests and the demo never call Microsoft. Selected by the tenant
ID `simulated` in an organization's email settings (notify/channels.py).

- The token endpoint issues `simulated-token`, except for the client secret `wrong-secret` (401 `invalid_client`).
- `sendMail` answers 202 with an empty body and records the message in OUTBOX (newest last, at most 200), except for
  a sender mailbox starting `denied@` (403 `ErrorAccessDenied`, as when the Exchange RBAC for Applications scope
  doesn't cover the mailbox) or `missing@` (404 `ErrorInvalidUser`).
- `script` lets a test queue answers (a Reply or an exception to raise) for the next calls, in order, to exercise 429
  with Retry-After, 503, 5xx, timeouts and so on.
Anything else is refused with a 400, so a stray call fails a test rather than passing silently."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping
from typing import Any

from . import simulation
from .graph import GRAPH_HOST, LOGIN_HOST, Reply

SIMULATED = simulation.SIMULATED
TOKEN = "simulated-token"  # noqa: S105 (a synthetic value the simulator issues)
OUTBOX: list[dict[str, Any]] = []
OUTBOX_MAX = 200


def _error(status: int, code: str, message: str, headers: Mapping[str, str] | None = None) -> Reply:
    body = json.dumps({"error": {"code": code, "message": message, "innerError": {"request-id": "simulated"}}})
    return Reply(status, {"content-type": "application/json", **(headers or {})}, body.encode())


class Transport:
    def __init__(self, script: list[Any] | None = None):
        self.script = list(script or [])
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Reply:
        parts = urllib.parse.urlsplit(url)
        self.calls.append((method, url, dict(headers)))
        if parts.scheme != "https" or parts.hostname not in (LOGIN_HOST, GRAPH_HOST):
            return _error(400, "BadRequest", "The simulator only answers Microsoft hosts.")
        if parts.hostname == LOGIN_HOST and parts.path.endswith("/oauth2/v2.0/token"):
            return self._token(body or b"")
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step
        if method == "POST" and parts.path.startswith("/v1.0/users/") and parts.path.endswith("/sendMail"):
            return self._send_mail(urllib.parse.unquote(parts.path.split("/")[3]), headers, body or b"")
        return _error(400, "BadRequest", "The simulator only sends mail.")

    def _token(self, body: bytes) -> Reply:
        form = urllib.parse.parse_qs(body.decode())
        secret = (form.get("client_secret") or [""])[0]
        if (form.get("grant_type") or [""])[0] != "client_credentials" or not secret:
            return Reply(400, {}, json.dumps({"error": "invalid_request"}).encode())
        if secret == "wrong-secret":  # noqa: S105 (lets a test see a refused secret)
            return Reply(
                401,
                {},
                json.dumps(
                    {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided."}
                ).encode(),
            )
        return Reply(200, {}, json.dumps({"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3599}).encode())

    def _send_mail(self, sender: str, headers: Mapping[str, str], body: bytes) -> Reply:
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return _error(401, "InvalidAuthenticationToken", "Access token is empty or invalid.")
        if sender.startswith("denied@"):
            return _error(403, "ErrorAccessDenied", "Access is denied. Check credentials and try again.")
        if sender.startswith("missing@"):
            return _error(404, "ErrorInvalidUser", f"The requested user '{sender}' is invalid.")
        payload = json.loads(body.decode())
        if payload.get("saveToSentItems") is not False or not isinstance(payload.get("message"), dict):
            return _error(400, "ErrorInvalidRequest", "The request is invalid.")
        OUTBOX.append(
            {"sender": sender, "client_request_id": headers.get("client-request-id"), "message": payload["message"]}
        )
        del OUTBOX[:-OUTBOX_MAX]
        return Reply(202, {"client-request-id": headers.get("client-request-id", "")}, b"")
