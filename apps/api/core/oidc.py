"""OpenID Connect sign-in for IT staff (generic OIDC): the authorization code flow
with PKCE (S256), state and nonce, against the provider an organization configured (core/sso.py) or the legacy
installation-wide Entra settings. The provider's discovery document, keys and the ID token checks are in
core/oidc_discovery.py.

A transaction is pinned in the session as {state, nonce, verifier, next, mode, org, revision} (and the Admin for a
test sign-in); the callback refuses (`changed`) if the configuration that started it is no longer the current one.
The redirect URI is always {SOFTWARDEN_BASE_URL}/api/v1/auth/oidc/callback, never derived from the request's Host.

An identity signs in an existing member only: the first sign-in matches one active User with a membership in the
organization by the token's email, and the identity (issuer, subject) is then pinned (UserIdentity); later sign-ins
match on that alone. No User is ever created at sign-in. With the organization's `sso_auto_provision` on, an identity
that matches nobody becomes a PendingAccess row for an Admin (request_access); approving it creates the account
(catalog/invitations.approve_pending). An invitation's link may also be accepted after a sign-in whose identity is
already pinned to the invited account (invitee).

Nothing here talks to a real provider in development or tests: core/oidc_simulator.py answers
its own issuers, only with DEBUG on (local development) or in the Playwright server."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets as pysecrets
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from catalog import services
from catalog.admin_services import SHARED_ACCOUNT

from . import credentials, oidc_discovery, oidc_simulator, sso
from .models import Membership, Organization, PendingAccess, PlatformStaff, UserIdentity
from .oidc_discovery import ProviderError
from .problems import Conflict

log = logging.getLogger("softwarden.oidc")

SESSION_KEY = "oidc"
SIMULATED = oidc_discovery.SIMULATED
PROMPT_SELECT_ACCOUNT = frozenset({"entra", "google"})  # Okta has no select_account prompt
PENDING_ACCESS_MAX = 500  # pending access requests one organization holds; beyond, sign-ins are refused
PENDING_FULL = "access_pending_full"
PENDING = "access_pending"
NAME_MAX = 200


class SignInRefused(Exception):
    """`code` picks the message the sign-in page shows; nothing about the identity is in it. `pending` is the session
    transaction that was refused (a failed test sign-in is recorded from it); `detail` is Softwarden's sentence."""

    def __init__(self, code, user=None, next_path=None, pending=None, detail="", chooser=None, provisionable=False):
        super().__init__(code)
        self.provisionable = provisionable  # nobody matched: auto-provision may record a pending access request
        self.chooser = chooser  # the org or email the sign-in started with, for the way back to the sign-in page
        self.code = code
        self.user = user
        self.next_path = next_path  # where the person was going, so trying again goes there too
        self.pending = pending
        self.detail = detail


def simulator_allowed():
    return oidc_simulator.allowed()


def enabled(org=None) -> bool:
    """Whether SSO is offered for `org` (default: the installation's only organization)."""
    return sso.effective(org if org is not None else sso.legacy_org()) is not None


def redirect_uri():
    return f"{settings.SOFTWARDEN_BASE_URL}/api/v1/auth/oidc/callback"


def begin(session, config: sso.Config, next_path, admin_id=None, login_hint: str = "", chooser=None) -> str:
    """The authorize URL for `config`; the transaction waits in the session for the callback. Raises ProviderError
    when the provider's discovery document can't be used."""
    meta = oidc_discovery.metadata(config.provider)
    verifier = pysecrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, nonce = pysecrets.token_urlsafe(24), pysecrets.token_urlsafe(24)
    session[SESSION_KEY] = {
        "state": state,
        "nonce": nonce,
        "verifier": verifier,
        "next": next_path,
        "mode": config.mode,
        "org": config.org_id,
        "revision": config.revision,
        "admin": admin_id,
        "chooser": dict(chooser or {}),  # the org or email the sign-in page started with, for a refusal's way back
    }
    params = {
        "client_id": config.provider.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "response_mode": "query",
        "scope": oidc_discovery.SCOPE,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if config.provider.preset in PROMPT_SELECT_ACCOUNT:
        params["prompt"] = "select_account"
    if login_hint:
        params["login_hint"] = login_hint
    return oidc_discovery.authorize_url(meta, params)


def start(session, org, next_path, login_hint: str = "", chooser=None) -> str:
    """Start a member's sign-in for `org`. `chooser` is the {"org"} or {"email"} parameter the page sent."""
    config = sso.effective(org) if org is not None else None
    if config is None or config.saml is not None:  # SAML sign-in has its own start (the optional ee package)
        raise SignInRefused("not_configured", None, next_path)
    try:
        return begin(session, config, next_path, login_hint=login_hint, chooser=chooser)
    except ProviderError as e:
        log.warning("oidc start refused", extra={"reason": e.code})
        raise SignInRefused("provider_error", None, next_path) from None


def finish(session, params) -> tuple[dict[str, Any], str, sso.Config, dict[str, Any]]:
    """Check the callback, exchange the code and validate the ID token. Returns (claims, next path, configuration,
    the pinned transaction)."""
    pending = session.pop(SESSION_KEY, None)
    state = params.get("state")
    if not isinstance(pending, dict) or not state or not hmac.compare_digest(str(state), str(pending.get("state"))):
        chooser = pending.get("chooser") if isinstance(pending, dict) else None
        raise SignInRefused("expired", chooser=chooser)
    next_path = pending.get("next") or "/"
    if sso.force_local():
        raise SignInRefused("not_configured", None, next_path, pending, "Single sign-on is turned off here.")
    config = sso.config_for(pending)
    if config is None or config.saml is not None:
        raise SignInRefused(
            "changed", None, next_path, pending, "The sign-in settings changed during the sign-in. Try again."
        )
    if params.get("error"):
        code = "cancelled" if params.get("error") == "access_denied" else "provider_error"
        raise SignInRefused(code, None, next_path, pending, "The identity provider didn't complete the sign-in.")
    code = params.get("code")
    if not code:
        raise SignInRefused("provider_error", None, next_path, pending, "The provider sent no sign-in code.")
    try:
        meta = oidc_discovery.metadata(config.provider)
        raw = oidc_discovery.exchange(config.provider, meta, config.secret(), code, pending["verifier"], redirect_uri())
        claims = oidc_discovery.validate(config.provider, meta, raw, pending["nonce"])
    except ProviderError as e:
        log.warning("oidc token rejected", extra={"reason": e.code})
        raise SignInRefused(e.code, None, next_path, pending, str(e)) from None
    except credentials.CredentialError:
        log.error("oidc client secret unreadable")
        raise SignInRefused(
            "not_configured", None, next_path, pending, "The stored client secret can't be read."
        ) from None
    return claims, next_path, config, pending


def _members(org_id, email: str):
    users = get_user_model().objects.filter(membership__organization_id=org_id)
    found = list(users.filter(email__iexact=email) | users.filter(username__iexact=email))
    return list({u.id: u for u in found}.values())


@transaction.atomic
def match_user(claims, config: sso.Config):
    """The one User this identity may sign in to `config`'s organization, pinning the identity on first use.
    Never creates a User."""
    issuer, subject = oidc_discovery.identity_key(config.provider, claims)
    identity = (
        UserIdentity.objects.select_for_update().select_related("user").filter(issuer=issuer, subject=subject).first()
    )
    if identity is not None:
        user = identity.user
        if PlatformStaff.objects.filter(user=user).exists():
            raise SignInRefused("platform_staff", user)  # staff sign in with their password only
        if not user.is_active or not Membership.objects.filter(user=user).exists():
            raise SignInRefused("inactive", user)
        if not Membership.objects.filter(user=user, organization_id=config.org_id).exists():
            raise SignInRefused("no_account")
    else:
        # Only an email that may link (Entra: the `email` claim of a member of the pinned tenant, never
        # preferred_username or a guest's); otherwise pending access, when there is an address an Admin could approve.
        email = oidc_discovery.link_email(config.provider, claims)
        if not email:
            raise SignInRefused("no_account", provisionable=bool(oidc_discovery.email_of(config.provider, claims)))
        users = _members(config.org_id, email)
        if not users:
            raise SignInRefused("no_account", provisionable=True)
        if len(users) != 1:
            raise SignInRefused("no_account")
        user = users[0]
        if PlatformStaff.objects.filter(user=user).exists():
            # Never linked by email, and never signed in through an organization's provider: that
            # organization's Admin controls the provider and could otherwise reach the platform console.
            raise SignInRefused("platform_staff", user)
        if Membership.objects.filter(user=user).exclude(organization_id=config.org_id).exists():
            # An account in other organizations too is never linked by email through this organization's provider
            # (as SHARED_ACCOUNT): its Admin could point the provider at any address.
            raise SignInRefused("no_account")
        if not user.is_active:
            raise SignInRefused("inactive", user)
        if UserIdentity.objects.filter(user=user, issuer=issuer).exists():
            # This account already signs in with another identity at this provider; an Admin must unlink it first.
            raise SignInRefused("other_identity", user)
        identity = UserIdentity.objects.create(user=user, issuer=issuer, subject=subject)
        method = config.provider.preset
        services.audit_account(user, user, "user.identity_linked", {"user": user.id, "method": method})
    identity.last_login_at = timezone.now()
    identity.save(update_fields=["last_login_at"])
    return user


def _display_name(claims) -> str:
    name = claims.get("name")
    if not isinstance(name, str) or not name.strip():
        parts = [claims.get(k) for k in ("given_name", "family_name")]
        name = " ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    return oidc_discovery.describe(name.strip())[:NAME_MAX] if name else ""


@transaction.atomic
def request_access(claims, config: sso.Config) -> str | None:
    """Record an identity that matched no member as waiting for an Admin, when the organization's auto-provision is on.
    Returns the refusal code for the sign-in page (access_pending, or access_pending_full at
    PENDING_ACCESS_MAX), or None when auto-provision is off. Creates no User and signs nobody in."""
    if config.mode == "test":
        return None
    org = Organization.objects.select_for_update().filter(pk=config.org_id).first()  # serialises the cap check
    if org is None or not sso.auto_provision(org):
        return None
    issuer, subject = oidc_discovery.identity_key(config.provider, claims)
    email = oidc_discovery.email_of(config.provider, claims)[:254]
    name = _display_name(claims)
    now = timezone.now()
    row = PendingAccess.objects.filter(organization=org, issuer=issuer, subject=subject).first()
    if row is not None:
        if row.email != email:  # approve uses the email: an Admin sees that it changed
            from catalog.invitations import mask_email

            changes = {"email": [mask_email(row.email), mask_email(email)]}
            services.audit(org, None, "pending_access.updated", row, changes)
        row.email, row.name, row.last_seen = email, name, now
        row.save(update_fields=["email", "name", "last_seen", "updated_at"])
        return PENDING
    if PendingAccess.objects.filter(organization=org).count() >= PENDING_ACCESS_MAX:
        log.warning("pending access full", extra={"organization": str(org.pk)})
        return PENDING_FULL
    row = PendingAccess.objects.create(
        organization=org, issuer=issuer, subject=subject, email=email, name=name, first_seen=now, last_seen=now
    )
    provider = oidc_discovery.provider_label(issuer)
    services.audit(org, None, "pending_access.created", row, {"provider": provider})
    return PENDING


@transaction.atomic
def invitee(claims, config: sso.Config, next_path, remembered):
    """The account to sign in when a sign-in that matched no member was started from the invitation page
    (`next=/invite`; the invitation's id and token-hash prefix kept in the session by its preview, never its token,
    so a resend ends it: catalog/invitations.live_by_id): only when the identity
    is already pinned to the invited, active account and the invitation is live and from this organization. Never
    links by email: accepting through single sign-on proves the account by an identity it already signs in with. The
    session is confined to the organization like any SSO sign-in, and has no membership until the invitation is
    accepted."""
    from catalog import invitations

    if next_path != invitations.INVITE_NEXT or not remembered or config.mode == "test":
        return None
    issuer, subject = oidc_discovery.identity_key(config.provider, claims)
    identity = UserIdentity.objects.select_related("user").filter(issuer=issuer, subject=subject).first()
    if identity is None or not identity.user.is_active:
        return None
    if PlatformStaff.objects.filter(user=identity.user).exists():  # staff never sign in through SSO
        return None
    invitation = invitations.live_by_id(remembered)
    if invitation is None or str(invitation.organization_id) != str(config.org_id):
        return None
    if (identity.user.username or "").lower() != invitation.email:
        return None
    identity.last_login_at = timezone.now()
    identity.save(update_fields=["last_login_at"])
    return identity.user


def own_account(claims, config: sso.Config, admin) -> tuple[bool, str, str]:
    """A test sign-in proves the draft signs in the Admin's own account: (ok, code, detail). Nothing is pinned."""
    issuer, subject = oidc_discovery.identity_key(config.provider, claims)
    email = oidc_discovery.link_email(config.provider, claims)  # as the real first sign-in would link
    shown = oidc_discovery.email_of(config.provider, claims)
    who = oidc_discovery.describe(shown or subject)
    pinned = UserIdentity.objects.filter(issuer=issuer, subject=subject).first()
    if pinned is not None:
        mine = pinned.user_id == admin.pk
    else:
        own = {(admin.email or "").lower(), (admin.username or "").lower()} - {""}
        mine = bool(email) and email in own and not UserIdentity.objects.filter(user=admin, issuer=issuer).exists()
        if not mine and not email and shown in own:
            return (
                False,
                "other_account",
                f"Signed in as {who}, but the ID token has no email claim Softwarden may link by (it never uses "
                "preferred_username, nor a guest's email). Give the account a mail address in the tenant and test "
                "again; until then its first sign-in waits for an Admin's approval.",
            )
    if not mine:
        return (
            False,
            "other_account",
            f"Signed in as {who}, which isn't your Softwarden account. Sign in to the provider as yourself and test "
            "again.",
        )
    return True, "", f"Signed in as {who} through {oidc_discovery.describe(oidc_discovery.display_issuer(issuer))}."


def unlink(org, actor, membership):
    """An Admin unlinks a member's sign-in identities (the audit event names their issuers). Refused for an account that is also in another organization:
    the identity belongs to the account, not to this organization (catalog/admin_services.py, SHARED_ACCOUNT)."""
    if Membership.objects.filter(user_id=membership.user_id).exclude(pk=membership.pk).exists():
        raise Conflict(SHARED_ACCOUNT)
    if PlatformStaff.objects.filter(user_id=membership.user_id).exists():  # not this organization's account
        from catalog.admin_services import STAFF_ACCOUNT

        raise Conflict(STAFF_ACCOUNT)
    with transaction.atomic():  # the delete and its audit event commit together, or neither does
        rows = UserIdentity.objects.filter(user=membership.user)
        issuers = sorted(set(rows.values_list("issuer", flat=True)))
        removed, _ = rows.delete()
        if removed:
            changes = {"user": membership.user_id, "method": "sso", "issuers": issuers}
            services.audit(org, actor, "user.identity_unlinked", None, changes)
    return removed
