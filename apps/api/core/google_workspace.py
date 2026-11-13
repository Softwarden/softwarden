"""Google's OAuth 2.0 token endpoint for a service account with domain-wide delegation, for a
GoogleWorkspaceConnection's test (core/connections.py) and the Google Workspace discovery source
(discovery/google_workspace.py). Not Microsoft, so not on Microsoft Learn: checked against Google's developer
documentation on 2026-11-10 (docs/PERMISSIONS.md, "Discovery source: `google_workspace`").

https://developers.google.com/identity/protocols/oauth2/service-account: `POST https://oauth2.googleapis.com/token`
with `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer` and `assertion`, a JWT signed RS256 with the service
account's private key (header `kid` the key's ID), claims `iss` the service account's email, `sub` the user it acts
for (here a Workspace administrator: domain-wide delegation), `scope`, `aud` the token URL, `iat` and `exp` (at most an
hour later). The one scope asked is `https://www.googleapis.com/auth/admin.reports.audit.readonly` (the Reports API's
activities.list, applicationName `token`). Errors: `unauthorized_client` (the client ID isn't authorized for the scope
under domain-wide delegation), `access_denied` (a scope isn't authorized), `invalid_grant` (the subject doesn't exist,
the key is wrong, deleted or expired, or the clock is off), `invalid_client`.

The key is the JSON file Google Cloud gives for a service account key. Only `client_email`, `client_id`,
`private_key_id` and `private_key` are read; the email and the numeric client ID are stored in clear on the connection
(the client ID is what an administrator authorizes under domain-wide delegation), the private key and its ID go into
the secret slot. The private key never leaves this process: only the signed assertion is sent.

Hosts are Google's and fixed (`oauth2.googleapis.com`, `admin.googleapis.com`); calls still go through core/outbound's
public rule (outbound.PublicTransport). An administrator address in the domain `google.simulated.invalid` selects the
synthetic Workspace (SimulatedTokens here, discovery/google_workspace_simulator.py for reads)."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from . import outbound, simulation
from .graph import HostRefused, Reply, Unreachable

TOKEN_HOST = "oauth2.googleapis.com"  # noqa: S105 (a host name, not a password)
TOKEN_URL = f"https://{TOKEN_HOST}/token"  # noqa: S105 (a URL, not a password)
REPORTS_HOST = "admin.googleapis.com"
REPORTS_SCOPE = "https://www.googleapis.com/auth/admin.reports.audit.readonly"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
SIMULATED_DOMAIN = "google.simulated.invalid"
ASSERTION_SECONDS = 600  # Google allows an hour
CODE = re.compile(r"^[A-Za-z0-9_]{1,60}$")
EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@([A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}$")
SERVICE_ACCOUNT = re.compile(r"^[a-z0-9._-]{1,100}@[a-z0-9.-]{1,150}\.gserviceaccount\.com$")
CLIENT_ID = re.compile(r"^[0-9]{6,40}$")
KEY_ID = re.compile(r"^[0-9a-f]{20,64}$")
KEY_MAX = 12_000
TOKEN = "simulated-google-token"  # noqa: S105 (a synthetic value the simulator issues)

clock = time.time  # overridable in tests


class GoogleError(Exception):
    """The token request failed. `status` the HTTP status (0 when nothing answered), `code` the OAuth error code,
    `refused` True when the address check refused the host."""

    def __init__(self, status: int = 0, code: str = "", *, refused: bool = False, reason: str = ""):
        self.status = status
        self.code = code
        self.refused = refused
        self.reason = reason
        super().__init__(f"{status} {code}".strip() or reason)


class KeyProblem(ValueError):
    """The service account key can't be used. The message is safe to show: it never repeats the key."""


@dataclass(frozen=True)
class ServiceAccountKey:
    client_email: str
    client_id: str
    private_key_id: str
    private_key: str = field(repr=False)

    def secret(self) -> str:
        """What the connection's secret slot keeps: the key and its ID, never the rest of the file."""
        return json.dumps({"private_key_id": self.private_key_id, "private_key": self.private_key}, sort_keys=True)


def parse_key_file(text: Any) -> ServiceAccountKey:
    """A service account key file (JSON) as Google Cloud downloads it. KeyProblem otherwise, never quoting it."""
    if not isinstance(text, str) or not text.strip():
        raise KeyProblem("Paste the service account's key file (JSON).")
    if len(text) > KEY_MAX:
        raise KeyProblem(f"The key file is too long ({KEY_MAX} characters at most).")
    try:
        data = json.loads(text)
    except ValueError:
        raise KeyProblem("The key file isn't valid JSON. Paste the whole file Google Cloud downloaded.") from None
    if not isinstance(data, dict) or data.get("type") != "service_account":
        raise KeyProblem("This isn't a service account key file (its type must be service_account).")
    email = str(data.get("client_email") or "").strip().lower()
    client_id = str(data.get("client_id") or "").strip()
    key_id = str(data.get("private_key_id") or "").strip().lower()
    pem = str(data.get("private_key") or "")
    if not SERVICE_ACCOUNT.match(email):
        raise KeyProblem("The key file's client_email isn't a service account address (…@….gserviceaccount.com).")
    if not CLIENT_ID.match(client_id):
        raise KeyProblem("The key file's client_id isn't a numeric client ID.")
    if not KEY_ID.match(key_id):
        raise KeyProblem("The key file's private_key_id isn't a key ID.")
    _private_key(pem)
    return ServiceAccountKey(email, client_id, key_id, pem)


def _private_key(pem: str) -> rsa.RSAPrivateKey:
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError):
        raise KeyProblem("The key file's private_key isn't a PEM private key.") from None
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise KeyProblem("The key file's private_key isn't an RSA key of 2048 bits or more.")
    return key


def stored_key(secret: str) -> tuple[str, rsa.RSAPrivateKey]:
    """(private key ID, private key) from the secret slot's value (ServiceAccountKey.secret)."""
    try:
        data = json.loads(secret)
        key_id, pem = str(data["private_key_id"]), str(data["private_key"])
    except (ValueError, KeyError, TypeError):
        raise KeyProblem("The stored key can't be read. Enter the key file again.") from None
    return key_id, _private_key(pem)


def check_admin_email(value: Any) -> str:
    """The administrator to act as, lower-cased; ValueError otherwise."""
    email = value.strip().lower() if isinstance(value, str) else ""
    if not EMAIL.match(email):
        raise ValueError("Enter a Google Workspace administrator's email address.")
    domain = email.split("@", 1)[1]
    if domain != SIMULATED_DOMAIN and (domain == "localhost" or domain.endswith(outbound.BLOCKED_NAMES)):
        raise ValueError("Enter an address in your Google Workspace domain.")
    return email


def is_simulated(admin_email: str) -> bool:
    return (admin_email or "").lower().endswith(f"@{SIMULATED_DOMAIN}")


def assertion(client_email: str, key_id: str, key: rsa.RSAPrivateKey, subject: str, scope: str) -> str:
    now = int(clock())
    claims = {
        "iss": client_email,
        "sub": subject,
        "scope": scope,
        "aud": TOKEN_URL,
        "iat": now,
        "exp": now + ASSERTION_SECONDS,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": key_id})


def request_token(
    transport, client_email: str, secret: str, subject: str, scope: str = REPORTS_SCOPE
) -> dict[str, Any]:
    """The token endpoint's JSON (`access_token`, `expires_in`). GoogleError otherwise; never carries the key or the
    assertion. KeyProblem when the stored key can't be used."""
    key_id, key = stored_key(secret)
    form = {"grant_type": GRANT_TYPE, "assertion": assertion(client_email, key_id, key, subject, scope)}
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    try:
        reply = transport.send("POST", TOKEN_URL, headers, urllib.parse.urlencode(form).encode())
    except HostRefused as e:
        raise GoogleError(refused=True, reason=str(e)) from None
    except Unreachable as e:
        raise GoogleError(reason=str(e)) from None
    body = reply.json()
    if not reply.ok:
        error = body.get("error") if isinstance(body, dict) else None
        raise GoogleError(reply.status, error if isinstance(error, str) and CODE.match(error) else "")
    if not isinstance(body, dict) or not body.get("access_token"):
        raise GoogleError(reply.status, "no_token")
    return body


def transport_for(admin_email: str):
    """The synthetic token endpoint for the reserved domain (refused unless core/simulation.allowed()), else
    core/outbound under the public rule."""
    if is_simulated(admin_email):
        if not simulation.allowed():
            raise GoogleError(refused=True, reason=simulation.MESSAGE)
        return SimulatedTokens()
    return outbound.PublicTransport()


class SimulatedTokens:
    """The synthetic Google token endpoint: `simulated-google-token` for a well-formed assertion (claims checked, not
    the signature) whose subject is in the simulated domain and whose scope is the Reports scope. The subject
    `nobody@…` gets `invalid_grant` (no such user), `not-delegated@…` `unauthorized_client` (the client ID isn't
    authorized under domain-wide delegation), any other scope `unauthorized_client`, and the key ID of forty `f`s
    `invalid_grant` (a deleted key). With `echo` a refusal repeats the assertion (tests only)."""

    WRONG_KEY = "f" * 40

    def __init__(self, echo: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.echo = echo

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Reply:
        self.calls.append((method, url))
        if method != "POST" or url != TOKEN_URL:
            return _oauth_error(404, "not_found")
        form = {k: v[0] for k, v in urllib.parse.parse_qs((body or b"").decode()).items()}
        signed = form.get("assertion", "")
        if form.get("grant_type") != GRANT_TYPE:
            return _oauth_error(400, "unsupported_grant_type")
        try:
            claims = jwt.decode(signed, options={"verify_signature": False, "verify_exp": False})
            header = jwt.get_unverified_header(signed)
        except jwt.PyJWTError:
            return _oauth_error(400, "invalid_grant")
        if self.echo:
            reply = {"error": "invalid_grant", "error_description": f"Bad assertion {signed}"}
            return Reply(400, {"content-type": "application/json"}, json.dumps(reply).encode())
        subject = str(claims.get("sub") or "")
        if (
            claims.get("aud") != TOKEN_URL
            or header.get("alg") != "RS256"
            or not isinstance(claims.get("exp"), int)
            or claims["exp"] < int(clock())
            or not SERVICE_ACCOUNT.match(str(claims.get("iss") or ""))
        ):
            return _oauth_error(400, "invalid_grant")
        if header.get("kid") == self.WRONG_KEY or not subject.endswith(f"@{SIMULATED_DOMAIN}"):
            return _oauth_error(400, "invalid_grant")
        if subject.startswith("nobody@"):
            return _oauth_error(400, "invalid_grant")
        if subject.startswith("not-delegated@") or claims.get("scope") != REPORTS_SCOPE:
            return _oauth_error(401, "unauthorized_client")
        return Reply(
            200,
            {"content-type": "application/json"},
            json.dumps({"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3599}).encode(),
        )


def _oauth_error(status: int, code: str) -> Reply:
    return Reply(status, {"content-type": "application/json"}, json.dumps({"error": code}).encode())
