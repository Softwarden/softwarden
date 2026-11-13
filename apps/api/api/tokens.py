"""API tokens: create, hash and authenticate. The secret is `sw_` plus 40 URL-safe
characters, shown once; only its SHA-256 is stored (the secret is high-entropy, so a slow hash adds nothing)."""

import hashlib
import secrets
from datetime import timedelta
from typing import NoReturn

from django.db import transaction
from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import authentication, exceptions, status

from catalog.services import audit
from core import limits, signin_limits
from core.dates import org_today
from core.middleware import client_ip
from core.models import Membership, Role
from core.request_context import note_via

from .models import ApiToken

SECRET_PREFIX = "sw_"  # noqa: S105  (a format marker, not a password)
SECRET_CHARS = 40
PREFIX_LENGTH = 11  # "sw_" plus 8 characters: enough to tell tokens apart, far too little to guess the rest
MAX_LIFETIME_DAYS = 365
LAST_USED_RESOLUTION = timedelta(minutes=1)  # last_used_at moves at most once a minute, not one write per request


def hash_secret(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def generate_secret():
    return SECRET_PREFIX + secrets.token_urlsafe(SECRET_CHARS)[:SECRET_CHARS]


class InvalidToken(exceptions.APIException):
    """401. Not AuthenticationFailed: sessions come first and have no WWW-Authenticate challenge, so DRF would turn
    that into a 403."""

    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "This API token isn't valid."
    auth_header = 'Bearer realm="softwarden", error="invalid_token"'  # RFC 6750 §3: a 401 names its scheme


class TokenAuthentication(authentication.BaseAuthentication):
    """`Authorization: Bearer sw_...` only. Query strings and cookies are never read. Session authentication runs first
    and is unchanged; token requests carry no cookie, so there is nothing for CSRF to protect."""

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        scheme, _, value = header.partition(" ")
        value = value.strip()
        if scheme.lower() != "bearer" or not value.startswith(SECRET_PREFIX):
            return None  # not ours (another scheme, or a bearer value that isn't an API token)
        address = client_ip(request)
        token = ApiToken.objects.select_related("user", "organization").filter(token_hash=hash_secret(value)).first()
        if token is None:
            refuse(address, "This API token isn't recognized.")
        if token.revoked_at is not None:
            refuse(address, "This API token was revoked.")
        if token.expires_on < org_today(token.organization):
            refuse(address, "This API token has expired.")
        user = token.user
        # The token acts in its own organization only (core/access.py ignores the session for it), so its owner must
        # still be a member there; membership elsewhere doesn't count.
        member = Membership.objects.filter(user=user, organization_id=token.organization_id).exists()
        if not user.is_active or not member:
            refuse(address, "The account that owns this API token is no longer active.")
        # A token an Admin made for someone else stands on that Admin's authority too. Leaving Admin, the
        # organization or the active state revokes such tokens (revoke_created_for_others); this refuses them on any
        # path that changed the creator without going through those services (SCIM role mapping, an operator's SQL).
        if token.created_by_id is not None and token.created_by_id != user.pk and not _creator_is_admin(token):
            refuse(address, "The Admin who created this API token is no longer an active Admin here.")
        now = timezone.now()
        if token.last_used_at is None or now - token.last_used_at >= LAST_USED_RESOLUTION:
            ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
            token.last_used_at = now
        # Every audit event this request writes names the token beside its owner (the actor). Keys avoid the audit
        # denylist word "token", which would redact them.
        note_via(api_client=token.name, api_client_id=str(token.pk))
        return user, token


def refuse(address: str, message: str) -> NoReturn:
    """401 for a token that isn't (or is no longer) valid, counted against the client's address: past
    SOFTWARDEN_INVALID_TOKEN_RATE in the window it is a 429 instead, with Retry-After. A valid token is never
    counted or refused here, so an address shared with someone guessing (an office, the MCP server) keeps working."""
    wait = signin_limits.record_bad_token(address)
    if wait:
        raise exceptions.Throttled(wait=wait, detail="Too many refused API tokens from this address.")
    raise InvalidToken(message)


def _creator_is_admin(token) -> bool:
    return Membership.objects.filter(
        user_id=token.created_by_id,
        user__is_active=True,
        organization_id=token.organization_id,
        role=Role.ADMIN,
    ).exists()


@transaction.atomic
def create_token(org, actor, owner_membership, name, read_only, expires_on):
    """Returns (token, secret). The secret exists only in this return value."""
    limits.check_room(org, limits.OTHER_RECORDS)
    secret = generate_secret()
    token = ApiToken.objects.create(
        organization=org,
        user=owner_membership.user,
        name=name,
        token_hash=hash_secret(secret),
        prefix=secret[:PREFIX_LENGTH],
        read_only=read_only,
        expires_on=expires_on,
        created_by=actor,
    )
    # Keys avoid the audit denylist word "token", which would redact them.
    audit(org, actor, "token.created", token, _audit_fields(token))
    return token, secret


def revoke_token(org, actor, token):
    if token.revoked_at is None:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at", "updated_at"])
        audit(org, actor, "token.revoked", token, _audit_fields(token))
    return token


def revoke_created_for_others(org, actor, creator, reason) -> int:
    """Revoke the live tokens `creator` made in `org` for other members, when the creator stops being an active
    Admin there: demoted, deactivated or removed. Each is audited as token.revoked with the reason. Tokens the creator
    owns are left to the owner checks (a deactivated or removed owner's tokens stop; a demoted owner's act at the new
    role). Returns how many were revoked. Runs in the caller's transaction."""
    now = timezone.now()
    count = 0
    live = ApiToken.objects.select_for_update().filter(organization=org, created_by=creator, revoked_at=None)
    for token in live.exclude(user=creator).order_by("created_at", "id"):
        token.revoked_at = now
        token.save(update_fields=["revoked_at", "updated_at"])
        audit(org, actor, "token.revoked", token, {**_audit_fields(token), "reason": reason})
        count += 1
    return count


def _audit_fields(token):
    return {
        "name": token.name,
        "owner": token.user_id,
        "read_only": token.read_only,
        "expires_on": token.expires_on.isoformat(),
    }


def expiry_window(org):
    today = org_today(org)
    return today + timedelta(days=1), today + timedelta(days=MAX_LIFETIME_DAYS)


class TokenScheme(OpenApiAuthenticationExtension):
    """Describes Bearer tokens in the OpenAPI document (and silences drf-spectacular's unknown-authenticator warning)."""

    target_class = "api.tokens.TokenAuthentication"
    name = "apiToken"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "description": "An API token, sw_…, created by an Admin."}
