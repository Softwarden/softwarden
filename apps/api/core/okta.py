"""Okta's org authorization server for an OktaConnection's test (core/connections.py) and the Okta discovery source
(discovery/okta.py). Not Microsoft, so not on Microsoft Learn: checked against Okta's developer
documentation on 2026-11-10 (docs/PERMISSIONS.md, "Discovery source: `okta`").

Auth is OAuth 2.0 for Okta with a service app (https://developer.okta.com/docs/guides/implement-oauth-for-okta-
serviceapp/main/): client credentials at `POST https://{okta domain}/oauth2/v1/token` with a form body
`grant_type=client_credentials`, `scope` (space-separated Okta scopes, read-only here: `okta.apps.read` by default),
`client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer` and `client_assertion`, a JWT signed
with the app's private key whose `iss` and `sub` are the client ID and `aud` the token URL, valid for at most an hour.
`private_key_jwt` is the only client authentication Okta allows for service apps that get Okta-scoped tokens. The
access token (an hour) goes in `Authorization: Bearer`. Softwarden doesn't send DPoP proofs: a service app with
"Require Demonstrating Proof of Possession (DPoP) header in token requests" on is refused (`invalid_dpop_proof`) and
the test says to turn it off. SSWS API tokens are not offered (docs/PERMISSIONS.md says why).

The private key is the connection's secret slot: a PEM private key (PKCS#8 or PKCS#1) or a private JWK, RSA (2048
bits or more, RS256) or EC P-256 (ES256). It never leaves this process: only the signed assertion is sent.

The org address is https on a public host: an Okta-managed one (`*.okta.com`, `*.oktapreview.com`,
`*.okta-emea.com`, never the `-admin` console host) or a custom domain. Every call goes through core/outbound's public
rule (outbound.PublicTransport): a name that resolves to a loopback, private, link-local or metadata address is refused
before anything is sent, on every call, with no redirects. The host `okta.simulated.invalid` (a reserved name that
never resolves) selects the synthetic Okta (SimulatedTokens here, discovery/okta_simulator.py for reads), so tests and
the demo never leave the machine. Errors never carry the key, an assertion, a token or Okta's text."""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from . import outbound, simulation
from .graph import HostRefused, Reply, Unreachable

SIMULATED_HOST = "okta.simulated.invalid"
SIMULATED_URL = f"https://{SIMULATED_HOST}"
TOKEN_PATH = "/oauth2/v1/token"  # noqa: S105 (a URL path, not a password)
APPS_PATH = "/api/v1/apps"
DEFAULT_SCOPE = "okta.apps.read"
# Read-only Okta scopes only: the source reads applications (and their user counts, the same scope); okta.users.read is
# accepted for an Admin who granted it, never needed.
READ_SCOPES = frozenset({"okta.apps.read", "okta.users.read"})
MANAGED_SUFFIXES = (".okta.com", ".oktapreview.com", ".okta-emea.com")
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ASSERTION_SECONDS = 300  # Okta allows up to an hour; five minutes is plenty for one token request
CODE = re.compile(r"^[A-Za-z0-9_]{1,60}$")
CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
KEY_ID = re.compile(r"^[A-Za-z0-9._~+/=-]{1,200}$")
TOKEN = "simulated-okta-token"  # noqa: S105 (a synthetic value the simulator issues)
KEY_MAX = 12_000

# Overridable in tests (the assertion's iat and exp).
clock = time.time


class OktaError(Exception):
    """The token request failed. `status` is the HTTP status (0 when nothing answered), `code` the OAuth error code
    Okta sent (`invalid_client`, `invalid_scope`, `invalid_dpop_proof`, ...), `refused` True when the address check
    refused the host (`reason` says why, in Softwarden's words)."""

    def __init__(self, status: int = 0, code: str = "", *, refused: bool = False, reason: str = ""):
        self.status = status
        self.code = code
        self.refused = refused
        self.reason = reason
        super().__init__(f"{status} {code}".strip() or reason)


class KeyProblem(ValueError):
    """The private key can't be used. The message is safe to show: it never repeats the key."""


# ---- the org address ----------------------------------------------------------------------------------------------


def check_domain(url: Any) -> str:
    """The org's base URL (`https://{host}`), or outbound.UnsafeDestination with a reason that never repeats the URL.
    https on port 443, a host name (never an IP address), no user, path, query or fragment; the public-host rule of
    core/outbound.check_url; an Okta-managed host is one label under its suffix and not the `-admin` console host."""
    if not isinstance(url, str) or not url.strip():
        raise outbound.UnsafeDestination("Enter your Okta address, such as https://yourcompany.okta.com.")
    text = url.strip().rstrip("/")
    if text.lower() == SIMULATED_URL:
        return SIMULATED_URL
    parts = outbound.check_url(text)
    host = (parts.hostname or "").lower()
    host = host[:-1] if host.endswith(".") else host  # a fully qualified name's one trailing dot names the same host
    if parts.port not in (None, 443):
        raise outbound.UnsafeDestination("Use the Okta address without a port.")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise outbound.UnsafeDestination("Use the Okta org address only, without a path, ? or #.")
    if outbound._literal(host) is not None:
        raise outbound.UnsafeDestination("Use the Okta org's host name, not an IP address.")
    for suffix in MANAGED_SUFFIXES:
        if host == suffix[1:]:
            raise outbound.UnsafeDestination(f"Use your org's address, such as https://yourcompany{suffix}.")
        if host.endswith(suffix):
            label = host[: -len(suffix)]
            if not label or "." in label:
                raise outbound.UnsafeDestination(f"Use your org's address, such as https://yourcompany{suffix}.")
            if label.endswith("-admin"):
                raise outbound.UnsafeDestination(
                    f"Use the org address https://{label.removesuffix('-admin')}{suffix}, not the Admin Console's."
                )
    return f"https://{outbound.ascii_host(host)}"


def is_simulated(base_url: str) -> bool:
    return (urllib.parse.urlsplit(base_url or "").hostname or "").lower() == SIMULATED_HOST


def token_url(base_url: str) -> str:
    return base_url.rstrip("/") + TOKEN_PATH


def check_scope(value: str) -> str:
    """The scopes, normalised (one space, sorted), when every one is a read-only Okta scope; ValueError otherwise."""
    scopes = sorted(set((value or DEFAULT_SCOPE).split()))
    if not scopes or any(s not in READ_SCOPES for s in scopes):
        raise ValueError(f"Use read-only Okta scopes only: {', '.join(sorted(READ_SCOPES))}.")
    if "okta.apps.read" not in scopes:
        raise ValueError("Include okta.apps.read: it is what the source reads.")
    return " ".join(scopes)


# ---- the key --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SigningKey:
    algorithm: str  # RS256 or ES256
    key: Any = field(repr=False)
    jwk_kid: str = ""  # the JWK's own `kid`, when the key was a JWK


def load_key(text: Any) -> SigningKey:
    """The connection's private key: a private JWK (JSON) or a PEM private key, RSA of 2048 bits or more or EC P-256.
    KeyProblem otherwise (never quoting the key)."""
    if not isinstance(text, str) or not text.strip():
        raise KeyProblem("Paste the service app's private key (PEM or JWK).")
    value = text.strip()
    if len(value) > KEY_MAX:
        raise KeyProblem(f"The key is too long ({KEY_MAX} characters at most).")
    kid = ""
    key: Any
    if value.startswith("{"):
        try:
            jwk = json.loads(value)
        except ValueError:
            raise KeyProblem("The key looks like a JWK but isn't valid JSON.") from None
        if not isinstance(jwk, dict) or "d" not in jwk:
            raise KeyProblem("Paste the private JWK (with its private part), not the public key.")
        try:
            key = (RSAAlgorithm if jwk.get("kty") == "RSA" else ECAlgorithm).from_jwk(jwk)
        except Exception:
            raise KeyProblem("The JWK isn't a usable RSA or EC private key.") from None
        kid = str(jwk["kid"]) if isinstance(jwk.get("kid"), str) and KEY_ID.match(jwk["kid"]) else ""
    else:
        try:
            key = serialization.load_pem_private_key(value.encode(), password=None)
        except (ValueError, TypeError):
            raise KeyProblem(
                "The key isn't an unencrypted PEM private key (-----BEGIN PRIVATE KEY----- ...) or a JWK."
            ) from None
    if isinstance(key, rsa.RSAPrivateKey):
        if key.key_size < 2048:
            raise KeyProblem("Use an RSA key of 2048 bits or more.")
        return SigningKey("RS256", key, kid)
    if isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1):
        return SigningKey("ES256", key, kid)
    raise KeyProblem("Use an RSA (2048 bits or more) or EC P-256 private key.")


def client_assertion(client_id: str, url: str, key: SigningKey, key_id: str = "") -> str:
    """The signed JWT Okta's token endpoint takes as `client_assertion`."""
    now = int(clock())
    claims = {"iss": client_id, "sub": client_id, "aud": url, "iat": now, "exp": now + ASSERTION_SECONDS}
    claims["jti"] = uuid.uuid4().hex
    kid = key_id or key.jwk_kid
    return jwt.encode(claims, key.key, algorithm=key.algorithm, headers={"kid": kid} if kid else None)


# ---- the token -------------------------------------------------------------------------------------------------------


def request_token(
    transport, base_url: str, client_id: str, key_text: str, key_id: str = "", scope: str = DEFAULT_SCOPE
) -> dict[str, Any]:
    """The token endpoint's JSON (`access_token`, `expires_in`). OktaError otherwise; never carries the key or the
    assertion. KeyProblem when the stored key can't be used."""
    key = load_key(key_text)
    url = token_url(base_url)
    form = {
        "grant_type": "client_credentials",
        "scope": scope or DEFAULT_SCOPE,
        "client_assertion_type": ASSERTION_TYPE,
        "client_assertion": client_assertion(client_id, url, key, key_id),
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    try:
        reply = transport.send("POST", url, headers, urllib.parse.urlencode(form).encode())
    except HostRefused as e:
        raise OktaError(refused=True, reason=str(e) or outbound.SERVICE_REFUSED) from None
    except Unreachable as e:
        raise OktaError(reason=str(e)) from None
    body = reply.json()
    if not reply.ok:
        error = body.get("error") if isinstance(body, dict) else None
        raise OktaError(reply.status, error if isinstance(error, str) and CODE.match(error) else "")
    if not isinstance(body, dict) or not body.get("access_token"):
        raise OktaError(reply.status, "no_token")
    return body


def transport_for(base_url: str):
    """The synthetic token endpoint for the reserved host (refused unless core/simulation.allowed()), else
    core/outbound under the public rule."""
    if is_simulated(base_url):
        if not simulation.allowed():
            raise OktaError(refused=True, reason=simulation.MESSAGE)
        return SimulatedTokens()
    return outbound.PublicTransport()


# ---- the synthetic token endpoint ----------------------------------------------------------------------------------


class SimulatedTokens:
    """The synthetic Okta's token endpoint: `simulated-okta-token` for a well-formed client assertion (its claims are
    checked, not its signature: the simulator never has the key), except the client ID `wrong-client` (401
    `invalid_client`), `dpop-client` (400 `invalid_dpop_proof`) and a scope that isn't read-only Okta (400
    `invalid_scope`). With `echo` (the connector config's `simulated_token_echo`, tests only) a refusal repeats the
    client assertion, so a test can prove no message keeps it."""

    def __init__(self, echo: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.echo = echo

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Reply:
        parts = urllib.parse.urlsplit(url)
        self.calls.append((method, url))
        if method != "POST" or parts.hostname != SIMULATED_HOST or parts.path != TOKEN_PATH:
            return _oauth_error(404, "not_found")
        form = {k: v[0] for k, v in urllib.parse.parse_qs((body or b"").decode()).items()}
        assertion = form.get("client_assertion", "")
        if form.get("grant_type") != "client_credentials" or form.get("client_assertion_type") != ASSERTION_TYPE:
            return _oauth_error(400, "invalid_request")
        try:
            claims = jwt.decode(assertion, options={"verify_signature": False, "verify_exp": False})
            header = jwt.get_unverified_header(assertion)
        except jwt.PyJWTError:
            return _oauth_error(401, "invalid_client")
        client = claims.get("iss")
        if self.echo:
            body_ = {"error": "invalid_client", "error_description": f"Bad assertion {assertion}"}
            return Reply(401, {"content-type": "application/json"}, json.dumps(body_).encode())
        if (
            client != claims.get("sub")
            or claims.get("aud") != f"{SIMULATED_URL}{TOKEN_PATH}"
            or not isinstance(claims.get("exp"), int)
            or claims["exp"] < int(clock())
            or header.get("alg") not in ("RS256", "ES256")
        ):
            return _oauth_error(401, "invalid_client")
        if client == "wrong-client":
            return _oauth_error(401, "invalid_client")
        if client == "dpop-client":
            return _oauth_error(400, "invalid_dpop_proof")
        if any(s not in READ_SCOPES for s in form.get("scope", "").split()):
            return _oauth_error(400, "invalid_scope")
        return Reply(
            200,
            {"content-type": "application/json"},
            json.dumps({"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600}).encode(),
        )


def _oauth_error(status: int, code: str) -> Reply:
    return Reply(status, {"content-type": "application/json"}, json.dumps({"error": code}).encode())
