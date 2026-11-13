"""Simulated identity providers for local development, Playwright and the test suite. Never
Microsoft-, Okta- or Google-branded and never reachable on a production server: `allowed()` is true only with DEBUG on or
in the Playwright server (SOFTWARDEN_E2E=1 with an `_e2e` database; core/simulation.development, never through
SOFTWARDEN_ALLOW_SIMULATED), and both core/oidc_discovery.send and the page below refuse otherwise.

Three issuers at once, each with a discovery document, a key set, a token endpoint and its own claim shape:

- `entra`: issuer `https://login.microsoftonline.com/{TENANT_ID}/v2.0` for the synthetic tenant only (an Entra preset
  with the tenant `simulated` or TENANT_ID), RS256, claims `tid` and `oid`;
- `okta`: issuer `https://okta.simulated.invalid/oauth2/default` (an Okta preset with the domain
  `okta.simulated.invalid` and the server `default`, or a generic preset with that discovery URL), RS256, `sub` and
  `email_verified`;
- `google`: Google's own discovery URL and issuer, answered here only while the simulator is allowed, ES256, `sub`,
  `hd` (the email's domain) and `email_verified`. Its token and key endpoints are on `*.simulated.invalid`.

`.invalid` names never resolve (RFC 2606), so nothing here can reach a real host. Keys are made when the process
starts; authorization codes are kept in memory, so it works within one server process (runserver, the e2e server and
the test suite). Tests can rotate a key (`rotate`), count key-set fetches (`Idp.key_fetches`) and change what the
discovery document or a token says (`Idp.doc_overrides`, `issue_code(..., overrides=...)`; the override `_alg`
HS256 or none signs the token with the client secret or not at all)."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import uuid
import warnings
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from . import simulation
from .drf import public
from .graph import Reply

TENANT_ID = "00000000-0000-4000-8000-00000000c0de"
OKTA_DOMAIN = "okta.simulated.invalid"
REJECTED_SECRET = "rejected-secret"  # noqa: S105  (a client secret the simulated token endpoints refuse: invalid_client)
CODE_TTL = 120
WELL_KNOWN = "/.well-known/openid-configuration"
_LOCK = threading.Lock()
_CODES: dict[str, dict[str, Any]] = {}


def allowed() -> bool:
    """DEBUG or the Playwright server only (core/simulation.development). SOFTWARDEN_ALLOW_SIMULATED, which allows the
    simulated connections, never turns on a sign-in simulator: it would sign anyone in as any email."""
    return simulation.development()


def object_id(email):
    """A stable synthetic object ID per email, so a second sign-in is the same identity."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"simulated-oidc/{email.lower()}"))


def subject(idp: str, email: str) -> str:
    return hashlib.sha256(f"simulated-oidc/{idp}/{email.lower()}".encode()).hexdigest()[:20]


def _new_key(alg: str):
    if alg == "ES256":
        return ec.generate_private_key(ec.SECP256R1())
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@dataclass
class Idp:
    name: str
    issuer: str
    discovery_url: str
    token_url: str
    jwks_url: str
    alg: str
    keys: list[tuple[str, Any]] = field(default_factory=list)  # (kid, private key); the last one signs
    doc_overrides: dict[str, Any] = field(default_factory=dict)
    key_fetches: int = 0

    def __post_init__(self):
        if not self.keys:
            self.keys.append((f"{self.name}-1", _new_key(self.alg)))

    def document(self) -> dict[str, Any]:
        doc = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{settings.SOFTWARDEN_BASE_URL}/api/v1/auth/oidc/simulator?idp={self.name}",
            "token_endpoint": self.token_url,
            "jwks_uri": self.jwks_url,
            "response_types_supported": ["code"],
            "subject_types_supported": ["pairwise" if self.name == "entra" else "public"],
            "id_token_signing_alg_values_supported": [self.alg],
            "scopes_supported": ["openid", "profile", "email"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            "code_challenge_methods_supported": ["S256"],
        }
        return {**doc, **self.doc_overrides}

    def jwks(self) -> dict[str, Any]:
        self.key_fetches += 1
        out = []
        for kid, key in self.keys:
            algorithm = ECAlgorithm if self.alg == "ES256" else RSAAlgorithm
            jwk = algorithm.to_jwk(key.public_key(), as_dict=True)
            out.append({**jwk, "kid": kid, "use": "sig", "alg": self.alg})
        return {"keys": out}

    def claims(self, grant: dict[str, Any]) -> dict[str, Any]:
        now, email = int(time.time()), grant["email"]
        base = {"aud": grant["client_id"], "nonce": grant["nonce"], "iat": now, "nbf": now, "exp": now + 600}
        if self.name == "entra":
            oid = object_id(email)
            shaped = {"iss": self.issuer, "sub": subject("entra", email), "oid": oid, "tid": TENANT_ID}
            shaped |= {"email": email, "preferred_username": email, "name": email}
        elif self.name == "google":
            domain = email.rsplit("@", 1)[-1].lower()
            shaped = {"iss": self.issuer, "sub": subject("google", email), "azp": grant["client_id"], "hd": domain}
            shaped |= {"email": email, "email_verified": True, "name": email}
        else:
            shaped = {"iss": self.issuer, "sub": subject(self.name, email), "email": email, "email_verified": True}
            shaped |= {"preferred_username": email, "name": email}
        return {**base, **shaped, **grant.get("overrides", {})}

    def sign(self, claims: dict[str, Any]) -> str:
        kid, key = self.keys[-1]
        return jwt.encode(claims, key, algorithm=self.alg, headers={"kid": kid})


def _build() -> dict[str, Idp]:
    entra = f"https://login.microsoftonline.com/{TENANT_ID}"
    okta = f"https://{OKTA_DOMAIN}/oauth2/default"
    return {
        "entra": Idp(
            "entra",
            f"{entra}/v2.0",
            f"{entra}/v2.0{WELL_KNOWN}",
            f"{entra}/oauth2/v2.0/token",
            f"{entra}/discovery/v2.0/keys",
            "RS256",
        ),
        "okta": Idp("okta", okta, okta + WELL_KNOWN, f"{okta}/v1/token", f"{okta}/v1/keys", "RS256"),
        "google": Idp(
            "google",
            "https://accounts.google.com",
            f"https://accounts.google.com{WELL_KNOWN}",
            "https://oauth2.google.simulated.invalid/token",
            "https://www.google.simulated.invalid/oauth2/v3/certs",
            "ES256",
        ),
    }


IDPS: dict[str, Idp] = _build()


def reset() -> None:
    """Fresh keys, documents and counters (tests)."""
    IDPS.clear()
    IDPS.update(_build())
    with _LOCK:
        _CODES.clear()


def rotate(name: str) -> str:
    """Start signing with a new key (key rotation); the old key stays published. Returns the new key id."""
    idp = IDPS[name]
    kid = f"{name}-{len(idp.keys) + 1}"
    idp.keys.append((kid, _new_key(idp.alg)))
    return kid


# ---- the server side: what core/oidc_discovery.send gets for the simulator's URLs ----------------------------------


def _reply(status: int, body: dict[str, Any]) -> Reply:
    return Reply(status, {"content-type": "application/json"}, json.dumps(body).encode())


def answer(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> Reply | None:
    """The simulator's answer for one of its URLs, or None for any other URL (which then goes to the network)."""
    for idp in IDPS.values():
        if method == "GET" and url == idp.discovery_url:
            return _reply(200, idp.document())
        if method == "GET" and url == idp.jwks_url:
            return _reply(200, idp.jwks())
        if method == "POST" and url == idp.token_url:
            return _token(idp, body or b"", headers)
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host.endswith(".simulated.invalid") or url.startswith(f"https://login.microsoftonline.com/{TENANT_ID}/"):
        return _reply(404, {"error": "not_found"})
    return None


def _token(idp: Idp, body: bytes, headers: dict[str, str]) -> Reply:
    form = dict(urllib.parse.parse_qsl(body.decode()))
    client_id, secret = form.get("client_id", ""), form.get("client_secret", "")
    auth = {k.lower(): v for k, v in headers.items()}.get("authorization", "")
    if auth.startswith("Basic "):
        pair = base64.b64decode(auth[6:]).decode()
        user, _, password = pair.partition(":")
        client_id, secret = urllib.parse.unquote(user), urllib.parse.unquote(password)
    if not secret or secret == REJECTED_SECRET:
        return _reply(401, {"error": "invalid_client"})
    with _LOCK:
        grant = _CODES.pop(form.get("code", ""), None)
    if (
        grant is None
        or grant["idp"] != idp.name
        or grant["expires"] < time.time()
        or grant["redirect_uri"] != form.get("redirect_uri")
        or grant["client_id"] != client_id
    ):
        return _reply(400, {"error": "invalid_grant"})
    verifier = form.get("code_verifier", "")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    if not verifier or challenge != grant["code_challenge"]:
        return _reply(400, {"error": "invalid_grant"})
    claims = idp.claims(grant)
    alg = claims.pop("_alg", None)  # tests: an ID token signed another way (algorithm confusion)
    if alg == "HS256":
        with warnings.catch_warnings():  # a short client secret as an HMAC key: the point of this forged token
            warnings.simplefilter("ignore")
            token = jwt.encode(claims, secret, algorithm="HS256", headers={"kid": idp.keys[-1][0]})
    elif alg == "none":
        token = jwt.encode(claims, "", algorithm="none")
    else:
        token = idp.sign(claims)
    return _reply(200, {"id_token": token, "token_type": "Bearer", "expires_in": 3600})


def issue_code(params, email, idp="entra", overrides=None):
    """An authorization code for `email` at `idp`, as the sign-in page below issues one. `overrides` replaces claims
    in the ID token the code is redeemed for (tests)."""
    code = secrets.token_urlsafe(24)
    with _LOCK:
        _CODES[code] = {
            "idp": idp,
            "email": email,
            "nonce": params.get("nonce", ""),
            "client_id": params.get("client_id", ""),
            "redirect_uri": params.get("redirect_uri", ""),
            "code_challenge": params.get("code_challenge", ""),
            "expires": time.time() + CODE_TTL,
            "overrides": dict(overrides or {}),
        }
    return code


# ---- the browser side: the sign-in page ------------------------------------------------------------------------------

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Simulated identity provider</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font:16px system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem}}label,input,button{{display:block;width:100%;box-sizing:border-box}}
input{{margin:.4rem 0 1rem;padding:.5rem}}button{{padding:.6rem;margin-bottom:.6rem}}p.note{{color:#555;font-size:14px}}</style></head>
<body><main><h1>Simulated identity provider</h1>
<p class="note">Local testing only ({shape}-shaped). This stands in for a real identity provider; nothing is sent anywhere.</p>
<form method="post">{hidden}<label for="email">Sign in as (email)</label><input id="email" name="email" type="email" required autofocus>
<button type="submit" name="action" value="sign_in">Continue</button>
<button type="submit" name="action" value="cancel" formnovalidate>Cancel</button></form></main></body></html>"""


@public
@csrf_exempt  # a stand-in for another site's login page; the state parameter protects the callback
def authorize(request):
    from . import oidc
    from .problems import problem_response

    if not allowed() or getattr(settings, "SOFTWARDEN_FORCE_LOCAL_LOGIN", False):
        raise Http404
    params = request.GET if request.method == "GET" else request.POST
    name = params.get("idp") or "entra"
    if name not in IDPS:
        raise Http404
    redirect = params.get("redirect_uri", "")
    if redirect != oidc.redirect_uri():
        return problem_response(400, "Unknown redirect URI.")
    if request.method == "GET":
        keep = ("idp", "client_id", "redirect_uri", "state", "nonce", "code_challenge")
        hidden = "".join(f'<input type="hidden" name="{k}" value="{escape(params.get(k, ""))}">' for k in keep)
        return HttpResponse(PAGE.format(hidden=hidden, shape=escape(name.capitalize())))
    state = urllib.parse.quote(params.get("state", ""), safe="")
    if params.get("action") == "cancel":
        return HttpResponseRedirect(f"{redirect}?error=access_denied&state={state}")
    email = (params.get("email") or "").strip()
    code = issue_code(params, email, idp=name)
    return HttpResponseRedirect(f"{redirect}?code={code}&state={state}")
