"""OpenID Connect providers from their discovery document (generalised from an earlier Entra-only core/oidc.py).

A provider is a preset over the same discovery-document client:

- `entra`: the tenant GUID (never `common`, `organizations` or `consumers`) gives the issuer
  `https://login.microsoftonline.com/{tenant}/v2.0` and its discovery document under `/.well-known/openid-configuration`.
  An identity is pinned on `tid` + `oid` (the ID token's `tid` must be the tenant); `sub` is pairwise per app.
- `okta`: the Okta domain, plus an optional custom authorization server id: issuer `https://{domain}` (the org server)
  or `https://{domain}/oauth2/{id}`. Pinned on `iss` + `sub`; `email_verified` is required.
- `google`: `https://accounts.google.com/.well-known/openid-configuration`; the ID token's `iss` may be
  `https://accounts.google.com` or `accounts.google.com` (both are accepted and pinned as the first); `hd` must be one
  of the configured Workspace domains and `email_verified` is required (it may arrive as the string "true").
- `oidc`: any https discovery URL ending in `/.well-known/openid-configuration`; its issuer must be the URL without
  that suffix (OpenID Connect Discovery 1.0 §4.3). Pinned on `iss` + `sub`; an `email_verified` that is present must
  be true.

Every discovery document's `issuer` must equal the expected issuer, its endpoints must be https and it must list RS256
or ES256 among `id_token_signing_alg_values_supported`; only those two algorithms are accepted, and only the ones the
document lists. Discovery documents and key sets are fetched with a short timeout and, once checked, cached for CACHE_TTL seconds
(a failure for FAILURE_TTL); a
token signed with a key id the cached set lacks refetches the set once (key rotation).

Fetches go through core/outbound (the service rule: https, no redirects, private networks only where the installation
allows them, loopback and metadata addresses never, a wall-clock deadline). The in-process simulator
(core/oidc_simulator.py) answers its own issuers' URLs instead, only where it is allowed (DEBUG or the Playwright
server). `ProviderError` carries a short code and Softwarden's own sentence; a provider's own text is never stored,
shown or logged."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import jwt

from . import graph, outbound, simulation

log = logging.getLogger("softwarden.oidc")

ENTRA_HOST = graph.LOGIN_HOST
GUID = graph.GUID
WELL_KNOWN = "/.well-known/openid-configuration"
GOOGLE_ISSUER = "https://accounts.google.com"
GOOGLE_ISSUERS = frozenset({GOOGLE_ISSUER, "accounts.google.com"})
GOOGLE_DISCOVERY = GOOGLE_ISSUER + WELL_KNOWN
ALGORITHMS = ("RS256", "ES256")
KEY_TYPES = {"RS256": "RSA", "ES256": "EC"}
SCOPE = "openid profile email"
LEEWAY = 120  # seconds of clock skew accepted on exp, iat and nbf
CACHE_TTL = 300  # seconds a discovery document or key set is reused
FAILURE_TTL = 30  # seconds a failed fetch or check is answered from the cache
TIMEOUT = 10  # per socket operation
DEADLINE = 20.0  # for a whole fetch
BODY_MAX = 512 * 1024
SIMULATED = simulation.SIMULATED  # the Entra tenant that selects the simulator (as for Microsoft connections)
NOT_TENANTS = frozenset({"common", "organizations", "consumers"})
DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$")
OKTA_SERVER = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
ERROR_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")


class ProviderError(Exception):
    """`code` is a short refusal code; the message is Softwarden's own words, safe to store, show and log."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Provider:
    """One configuration's identity provider (an organization's active or draft settings, or the legacy env)."""

    preset: str
    discovery_url: str
    issuer: str
    client_id: str
    tenant: str = ""  # Entra: the tenant GUID the ID token's tid must equal (lower case)
    hosted_domains: tuple[str, ...] = ()  # Google: the Workspace domains `hd` must be one of


@dataclass(frozen=True)
class Metadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    algorithms: tuple[str, ...]
    token_auth_methods: tuple[str, ...]


# ---- presets -------------------------------------------------------------------------------------------------------


def entra_tenant(tenant: str) -> str:
    """The tenant GUID a configured tenant stands for (`simulated` is the simulator's synthetic tenant)."""
    if tenant == SIMULATED:
        from .oidc_simulator import TENANT_ID

        return TENANT_ID
    return tenant.strip().lower()


def entra_issuer(tenant_id: str) -> str:
    return f"https://{ENTRA_HOST}/{tenant_id}/v2.0"


def okta_issuer(domain: str, server: str = "") -> str:
    return f"https://{domain}" + (f"/oauth2/{server}" if server else "")


def urls(preset: str, tenant: str = "", okta_domain: str = "", okta_server: str = "", discovery_url: str = ""):
    """(discovery URL, expected issuer) for a preset's settings."""
    if preset == "entra":
        issuer = entra_issuer(entra_tenant(tenant))
        return issuer + WELL_KNOWN, issuer
    if preset == "okta":
        issuer = okta_issuer(okta_domain, okta_server)
        return issuer + WELL_KNOWN, issuer
    if preset == "google":
        return GOOGLE_DISCOVERY, GOOGLE_ISSUER
    return discovery_url, discovery_url[: -len(WELL_KNOWN)] if discovery_url.endswith(WELL_KNOWN) else ""


# ---- HTTP ----------------------------------------------------------------------------------------------------------


def send(method: str, url: str, body: bytes | None = None, headers: Mapping[str, str] | None = None) -> graph.Reply:
    """One request to an identity provider. The simulator answers its own URLs where it is allowed."""
    from . import oidc_simulator

    if oidc_simulator.allowed():
        simulated = oidc_simulator.answer(method, url, body, dict(headers or {}))
        if simulated is not None:
            return simulated
    try:
        return network(method, url, body, dict(headers or {}))
    except outbound.UnsafeDestination:
        raise ProviderError(
            "unsafe_address", "The identity provider's address isn't allowed from this server."
        ) from None
    except graph.Unreachable:
        raise ProviderError("unreachable", "Couldn't reach the identity provider from this server.") from None


def network(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> graph.Reply:
    """The real request (the test suite replaces this so nothing can leave the machine)."""
    return outbound.request(method, url, body, headers, TIMEOUT, DEADLINE, private_ok=True, body_max=BODY_MAX)


def _get_json(url: str, what: str) -> dict[str, Any]:
    reply = send("GET", url, None, {"Accept": "application/json"})
    if not reply.ok:
        raise ProviderError("fetch_failed", f"The identity provider answered {reply.status} for its {what}.")
    body = reply.json()
    if not isinstance(body, dict):
        raise ProviderError("fetch_failed", f"The identity provider's {what} isn't JSON.")
    return body


_CACHE: dict[str, tuple[float, Any]] = {}  # url: (expires, a checked value or the ProviderError it failed with)
_LOCK = threading.Lock()


def _cached(url: str, fetch: Callable[[], Any], fresh: bool = False) -> tuple[Any, bool]:
    """(value, whether it came from the cache). `fetch` fetches and checks; only a checked value is kept, for
    CACHE_TTL. A ProviderError is kept for FAILURE_TTL and raised again meanwhile, so a slow or broken provider costs
    one fetch per 30 s instead of one per request. `fresh` skips both."""
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(url)
    if hit is not None and not fresh and hit[0] > now:
        if isinstance(hit[1], ProviderError):
            raise ProviderError(hit[1].code, str(hit[1]))  # a fresh instance: the cached one's traceback must not grow
        return hit[1], True
    try:
        value = fetch()
    except ProviderError as error:
        with _LOCK:
            _CACHE[url] = (time.monotonic() + FAILURE_TTL, error)
        raise
    with _LOCK:
        _CACHE[url] = (time.monotonic() + CACHE_TTL, value)
    return value, False


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


# ---- discovery and keys --------------------------------------------------------------------------------------------


def _https(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 2000:
        return False
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname)


def _origin(url: str) -> tuple[str, str, int | None]:
    """(scheme, host, port) of a URL, the port made explicit for https and http."""
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if port is None:
        port = {"https": 443, "http": 80}.get(parts.scheme.lower())
    return parts.scheme.lower(), (parts.hostname or "").lower(), port


def _simulator_page(value: Any) -> bool:
    """The simulator's sign-in page is on this site (http in development), allowed only where the simulator is."""
    from django.conf import settings

    from . import oidc_simulator

    page = f"{settings.SOFTWARDEN_BASE_URL}/api/v1/auth/oidc/simulator?"
    return oidc_simulator.allowed() and isinstance(value, str) and value.startswith(page)


def metadata(provider: Provider) -> Metadata:
    """The provider's checked discovery document. Raises ProviderError."""
    if not provider.discovery_url or not provider.issuer:
        raise ProviderError("not_configured", "The sign-in settings don't name an identity provider.")
    meta, _ = _cached(f"{provider.discovery_url} {provider.issuer}", lambda: _checked(provider))
    return meta


def _checked(provider: Provider) -> Metadata:
    """Fetch and check the discovery document (cached only once it passes)."""
    doc = _get_json(provider.discovery_url, "discovery document")
    if doc.get("issuer") != provider.issuer:
        raise ProviderError(
            "issuer_mismatch",
            "The discovery document names a different issuer than this configuration expects. Check the tenant, "
            "domain or address.",
        )
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not (_https(doc.get(key)) or (key == "authorization_endpoint" and _simulator_page(doc.get(key)))):
            raise ProviderError("discovery_invalid", f"The discovery document has no https {key.replace('_', ' ')}.")
    if provider.preset == "oidc" and _origin(doc["token_endpoint"]) != _origin(provider.discovery_url):
        # The client secret goes to the token endpoint. For a generic provider, whose discovery URL an Admin
        # types, it must be on that URL's own origin, so a document at another path of a shared host can't send the
        # secret elsewhere. The named presets' hosts are fixed by their providers (Google's differ).
        token_host = urllib.parse.urlsplit(doc["token_endpoint"]).netloc or "another host"
        discovery_host = urllib.parse.urlsplit(provider.discovery_url).netloc
        raise ProviderError(
            "discovery_invalid",
            f"The discovery document's token endpoint is on {token_host}, not on {discovery_host}, where the discovery "
            "address is. Softwarden sends the client secret only to the discovery address's own host, so a document "
            "served from another path can't send it elsewhere; a provider that splits them (Amazon Cognito) can't be "
            "used with the generic OpenID Connect preset.",
        )
    listed = doc.get("id_token_signing_alg_values_supported")
    algorithms = tuple(a for a in ALGORITHMS if isinstance(listed, list) and a in listed)
    if not algorithms:
        raise ProviderError("unsupported_algorithm", "The provider doesn't sign ID tokens with RS256 or ES256.")
    methods = doc.get("token_endpoint_auth_methods_supported")
    if not isinstance(methods, list) or not methods:
        methods = ["client_secret_basic"]  # the default when the document lists none (Discovery 1.0 §3)
    return Metadata(
        issuer=doc["issuer"],
        authorization_endpoint=doc["authorization_endpoint"],
        token_endpoint=doc["token_endpoint"],
        jwks_uri=doc["jwks_uri"],
        algorithms=algorithms,
        token_auth_methods=tuple(m for m in methods if isinstance(m, str)),
    )


def _find_key(keys: Any, kid: str | None, alg: str) -> dict[str, Any] | None:
    if not isinstance(keys, list):
        return None
    usable = [
        k
        for k in keys
        if isinstance(k, dict) and k.get("kty") == KEY_TYPES[alg] and k.get("use") in (None, "sig")
        if k.get("alg") in (None, alg)
    ]
    if kid is not None:
        return next((k for k in usable if k.get("kid") == kid), None)
    return usable[0] if len(usable) == 1 else None


def signing_key(meta: Metadata, raw: str) -> tuple[Any, str]:
    """(public key, algorithm) for an ID token. An unknown key id refetches the cached key set once."""
    try:
        header = jwt.get_unverified_header(raw)
    except jwt.PyJWTError:
        raise ProviderError("token_invalid", "The ID token isn't a signed JWT.") from None
    alg = header.get("alg")
    if alg not in meta.algorithms:
        raise ProviderError("unsupported_algorithm", "The ID token is signed with an algorithm that isn't accepted.")
    kid = header.get("kid") if isinstance(header.get("kid"), str) else None
    keys, from_cache = _cached(meta.jwks_uri, lambda: _get_json(meta.jwks_uri, "signing keys"))
    found = _find_key(keys.get("keys"), kid, alg)
    if found is None and from_cache:
        keys, _ = _cached(meta.jwks_uri, lambda: _get_json(meta.jwks_uri, "signing keys"), fresh=True)
        found = _find_key(keys.get("keys"), kid, alg)
    if found is None:
        raise ProviderError("unknown_key", "The ID token is signed with a key the provider doesn't publish.")
    try:
        return jwt.PyJWK(found, algorithm=alg).key, alg
    except jwt.PyJWTError:
        raise ProviderError("unknown_key", "The provider's signing key can't be read.") from None


# ---- the authorization code flow -----------------------------------------------------------------------------------


def authorize_url(meta: Metadata, params: Mapping[str, str]) -> str:
    """The authorization endpoint with the request's parameters (keeping any query it already has)."""
    parts = urllib.parse.urlsplit(meta.authorization_endpoint)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True) + list(params.items())
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def exchange(provider: Provider, meta: Metadata, secret: str, code: str, verifier: str, redirect_uri: str) -> str:
    """Redeem the authorization code (with the PKCE verifier) for the raw ID token. Raises ProviderError."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "client_id": provider.client_id,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    if provider.preset == "entra":
        form["scope"] = SCOPE  # as earlier versions sent it; Entra takes the secret in the form (client_secret_post)
        form["client_secret"] = secret
    elif "client_secret_basic" in meta.token_auth_methods:
        pair = f"{urllib.parse.quote(provider.client_id, safe='')}:{urllib.parse.quote(secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    else:
        form["client_secret"] = secret
    reply = send("POST", meta.token_endpoint, urllib.parse.urlencode(form).encode(), headers)
    body = reply.json()
    if not reply.ok:
        error = body.get("error") if isinstance(body, dict) else None
        named = error if isinstance(error, str) and ERROR_CODE.match(error) else ""
        raise ProviderError(
            "token_refused",
            "The identity provider refused the sign-in code" + (f" ({named})." if named else f" ({reply.status})."),
        )
    raw = body.get("id_token") if isinstance(body, dict) else None
    if not isinstance(raw, str) or not raw:
        raise ProviderError("token_invalid", "The identity provider's answer has no ID token.")
    return raw


def verified(value: Any) -> bool:
    """`email_verified` is true: a JSON true or the string "true" (Google and some others send a string)."""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def validate(provider: Provider, meta: Metadata, raw: str, nonce: str) -> dict[str, Any]:
    """The ID token's claims after every check: signature (RS256 or ES256, a published key), audience, issuer, expiry,
    nonce, and the preset's own rules. Raises ProviderError with the failing check's code."""
    key, alg = signing_key(meta, raw)
    try:
        claims = jwt.decode(
            raw,
            key,
            algorithms=[alg],
            audience=provider.client_id,
            leeway=LEEWAY,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"], "verify_iss": False},
        )
    except jwt.InvalidAudienceError:
        raise ProviderError("audience", "The ID token is for another application.") from None
    except jwt.ExpiredSignatureError:
        raise ProviderError("token_expired", "The ID token has expired.") from None
    except jwt.MissingRequiredClaimError:
        raise ProviderError("missing_claim", "The ID token lacks a required claim.") from None
    except jwt.InvalidSignatureError:
        raise ProviderError("signature", "The ID token's signature doesn't verify.") from None
    except jwt.PyJWTError:
        raise ProviderError("token_invalid", "The ID token isn't valid.") from None
    issuers = GOOGLE_ISSUERS if provider.preset == "google" else {meta.issuer}
    if claims.get("iss") not in issuers:
        raise ProviderError("issuer", "The ID token comes from another issuer.")
    aud = claims.get("aud")
    if ("azp" in claims or (isinstance(aud, list) and len(aud) > 1)) and claims.get("azp") != provider.client_id:
        raise ProviderError("audience", "The ID token was issued to another application.")
    if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(claims["nonce"], nonce):
        raise ProviderError("nonce", "The ID token doesn't answer this sign-in (nonce).")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise ProviderError("missing_claim", "The ID token lacks a subject.")
    if provider.preset == "entra":
        tid, oid = claims.get("tid"), claims.get("oid")
        if not (isinstance(tid, str) and tid and isinstance(oid, str) and oid):
            raise ProviderError("missing_claim", "The ID token lacks the tenant or object ID.")
        if tid.lower() != provider.tenant:
            raise ProviderError("tenant", "The ID token is from another Entra tenant.")
    elif provider.preset in ("okta", "google"):
        if not verified(claims.get("email_verified")):
            raise ProviderError("email_unverified", "The identity provider hasn't verified this email address.")
    elif "email_verified" in claims and not verified(claims["email_verified"]):
        raise ProviderError("email_unverified", "The identity provider hasn't verified this email address.")
    if provider.preset == "google":
        hd = claims.get("hd")
        if not isinstance(hd, str) or hd.lower() not in provider.hosted_domains:
            raise ProviderError("hosted_domain", "The Google account isn't in this organization's Workspace domain.")
    return claims


def identity_key(provider: Provider, claims: Mapping[str, Any]) -> tuple[str, str]:
    """(issuer, subject) an identity is pinned on (core.models.UserIdentity)."""
    if provider.preset == "entra":
        return entra_issuer(str(claims["tid"]).lower()), str(claims["oid"])
    if provider.preset == "google":
        return GOOGLE_ISSUER, str(claims["sub"])
    return str(claims["iss"]), str(claims["sub"])


def email_of(provider: Provider, claims: Mapping[str, Any]) -> str:
    """The address shown for an identity (a pending access request, a test sign-in). Entra may only have
    `preferred_username`. Never used to link an existing member: that is link_email."""
    value = claims.get("email")
    if not value and provider.preset == "entra":
        value = claims.get("preferred_username")
    return value.strip().lower() if isinstance(value, str) else ""


def entra_guest(provider: Provider, claims: Mapping[str, Any]) -> bool:
    """Whether an Entra ID token is a guest's of the pinned tenant (Microsoft Learn, "ID token claims reference":
    `idp` is the same as `iss` unless the account isn't in the issuer's tenant, guests for instance, and absent means
    `iss`; "Optional claims reference": `acct` is 0 for a member of the tenant and 1 for a guest). An `idp` naming the
    pinned tenant (the v1 STS form) is a member's."""
    acct = claims.get("acct")
    if acct is not None and str(acct).strip() != "0":
        return True
    idp = claims.get("idp")
    if idp is None or idp == "":
        return False
    if not isinstance(idp, str):
        return True
    return idp != claims.get("iss") and (not provider.tenant or provider.tenant.lower() not in idp.lower())


def link_email(provider: Provider, claims: Mapping[str, Any]) -> str:
    """The email an existing member may be linked by on a first sign-in, or "".
    Only the `email` claim, never `preferred_username` (mutable; Learn: not for authorization decisions). For Entra
    only a member of the pinned tenant's (validate() has checked `tid`), never a guest's: a guest's email is whatever
    their home directory says. "" sends the identity to pending access, where an Admin's approval links it."""
    value = claims.get("email")
    if provider.preset == "entra" and entra_guest(provider, claims):
        return ""
    return value.strip().lower() if isinstance(value, str) else ""


OKTA_HOSTS = (".okta.com", ".oktapreview.com", ".okta-emea.com", ".okta.simulated.invalid")
# SAML identities are pinned on `saml:<organization id>:<IdP entity ID>`: an entity
# ID is free text, trusted only through the certificate the same Admin enters, so it is only ever matched for the
# organization that configured it. An OpenID Connect issuer is an https URL, so it never starts with `saml:`.
SAML_PREFIX = "saml:"
SAML_NAMESPACE = len(SAML_PREFIX) + 36 + 1  # the prefix, a UUID and ":" before the entity ID


def saml_issuer(org_id: Any, entity_id: str) -> str:
    """The issuer a SAML identity of `org_id`'s IdP `entity_id` is pinned on."""
    return f"{SAML_PREFIX}{org_id}:{entity_id}"


def display_issuer(issuer: str) -> str:
    """An issuer for people: a SAML identity's IdP entity ID without the organization namespace."""
    if issuer.startswith(SAML_PREFIX):
        parts = issuer.split(":", 2)
        return parts[2] if len(parts) == 3 else issuer
    return issuer


def provider_label(issuer: str) -> str:
    """A pinned identity's provider for people: SAML, or Entra, Okta or Google by the issuer's host, else the host
    itself."""
    if issuer.startswith(SAML_PREFIX):
        return "SAML"
    host = (urllib.parse.urlsplit(issuer).hostname or "").lower() if "://" in issuer else issuer.lower()
    if host == ENTRA_HOST:
        return "Entra"
    if host == "accounts.google.com":
        return "Google"
    if any(f".{host}".endswith(suffix) for suffix in OKTA_HOSTS):
        return "Okta"
    return host or "Single sign-on"


def describe(value: Any) -> str:
    """A short, printable form of a claim for a test detail (no control characters)."""
    text = json.dumps(value) if not isinstance(value, str) else value
    return re.sub(r"[\x00-\x1f]", "", text)[:120]
