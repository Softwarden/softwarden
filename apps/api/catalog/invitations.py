"""Invitations, pending access and unlinking one identity. Every write validates, scopes to the organization and audits in one transaction.

Invitations are links. Inviting stores the SHA-256 of a random token and returns `{SOFTWARDEN_BASE_URL}/invite#<token>`
once; resending rotates the token. The token is only ever in a URL fragment and in request bodies, never in a path or
a query string, because gunicorn's access log and Django's request log write paths. Nothing is added at invite time: the membership is created when the invitee
accepts (catalog/access_viewsets.py), as a new account that sets its name and password, or as the existing account
signed in as itself. So no account joins an organization without its owner's consent.

Proof of the mailbox: on an installation with more than one organization, or in cloud
mode (`proof_required`), the link an Admin is shown can't create an account. It still joins an account that exists
(accepting while signed in as it proves that), but a new account is created only through the link emailed to the
invited address (`mailbox_token`): the same token followed by an HMAC of it under SECRET_KEY, which only the server
can make. That link only ever goes through the installation's own mail server (`delivery_route`): an organization's
own Graph or SMTP settings are its Admin's, who could read the message there, so through them the invitation carries
the copy-once link alone. Where proof is required and the installation can't send email, no invitation link creates
an account. Otherwise any organization's Admin could create the global account for any address with a password of their
choosing, and the real owner could never sign up. The copy-once link's holder learns nothing about the address: its
preview has no `account_exists`, and accepting with it while nobody is signed in gets one answer
(/problems/invitation-emailed-link-required) whether the address has an account or not. An installation with one
organization keeps the copy-once link as it was: its Admins run the installation.

Accounts an organization made: pending-access approval and SCIM create the global account for whatever address
the organization's own identity provider or SCIM client sends, with no password and no proof of the mailbox. Such an
account (`managed`: not `proven`) doesn't hold its address, however many organizations it belongs to: a signup or an
emailed invitation for the address, each its own proof of the mailbox, takes it over. It joins another organization
only through an emailed invitation link, which proves its mailbox (core.models.MailboxProof). The old account is
retired in its organization (`release_address`: deactivated, its address released, its identities unlinked; audited
there as `user.address_reclaimed`) and the owner gets a fresh account. No Admin can give it a password
(admin_services.reset_password), so it never comes to hold the address.

Pending access is the other way in: with the organization's `sso_auto_provision` on, an unknown single sign-on identity
waits as a PendingAccess row (core/oidc.request_access) until an Admin approves it with a role or declines it."""

from __future__ import annotations

import base64
import secrets
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db import IntegrityError, transaction
from django.dispatch import Signal
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from rest_framework import exceptions

from core import email_tokens, limits, oidc_discovery
from core.input import has_web_address
from core.models import (
    Invitation,
    MailboxProof,
    Membership,
    Organization,
    PendingAccess,
    PlatformStaff,
    Role,
    UserIdentity,
)
from core.problems import NOT_SIGNED_IN, Conflict, FieldErrors

from .admin_services import _other_organizations, shared_refusal
from .services import audit

INVITATION_DAYS = 7
INVITE_NEXT = "/invite"  # the SPA's invitation page; the token stays in the fragment and sessionStorage
# Session key: the invitation an SSO sign-in may accept, as {"id": <its id>, "hash": <the first HASH_PREFIX hex
# characters of its token_hash>}. Never the token. The prefix ties the entry to the link previewed: a resend rotates
# token_hash, so the entry stops matching (live_by_id). core/oidc_views.py imports it from here.
SESSION_INVITATION = "softwarden_invitation"
HASH_PREFIX = 16
EMAIL_MAX = 254
NAME_MAX = 200

# Sent inside the acceptance's transaction, once the membership exists: sender Invitation, kwargs `invitation`,
# `user`, `membership`. SCIM, from the optional ee package, links the provisioned user the invitation was made for.
invitation_accepted = Signal()

UNAVAILABLE = "/problems/invitation-unavailable"
WRONG_ACCOUNT = "/problems/invitation-wrong-account"
INVITATION_REQUIRED = "/problems/invitation-required"
OTHER_SIGN_IN = "/problems/invitation-other-sign-in"
EMAILED_LINK_REQUIRED = "/problems/invitation-emailed-link-required"
MAILBOX_SALT = "softwarden.invitation.mailbox"
TOKEN_LENGTH = 43  # email_tokens.new_token(): token_urlsafe(32)
SIGNATURE_LENGTH = 43  # an unpadded URL-safe base64 SHA-256 HMAC


class InvitationUnavailable(exceptions.NotFound):
    """404 for an unknown, expired, used or revoked link, and for a deactivated invitee: one answer for all of them,
    so the link's holder learns nothing about which."""

    def __init__(self):
        super().__init__("This invitation link isn't valid any more. Ask the person who invited you for a new one.")
        self.extra = {"type": UNAVAILABLE}


class WrongAccount(Conflict):
    def __init__(self):
        super().__init__(
            "You're signed in as another account. Sign out, then open the invitation link again.",
            {"type": WRONG_ACCOUNT},
        )


class ConfinedElsewhere(Conflict):
    """An SSO session confined to another organization can't accept: the invitation joins the account to
    an organization that session doesn't reach."""

    def __init__(self):
        super().__init__(
            "You signed in through another organization's single sign-on. Sign in with your password, or with the "
            "inviting organization's single sign-on, then accept.",
            {"type": OTHER_SIGN_IN},
        )


class EmailedLinkRequired(exceptions.PermissionDenied):
    """The copy-once link, with nobody signed in, where a new account needs proof of the mailbox. One answer
    whether or not the address has an account, so the link's holder learns nothing about it."""

    def __init__(self):
        super().__init__(
            "To create your account, open the invitation from the email sent to the invited address. If you already "
            "have an account, sign in first, then open this link again."
        )
        self.extra = {"type": EMAILED_LINK_REQUIRED}


class SignInFirst(exceptions.PermissionDenied):
    def __init__(self):
        super().__init__("This email already has an account. Sign in as that account first, then accept.")
        self.extra = {"type": NOT_SIGNED_IN}


# ---- small helpers ------------------------------------------------------------------------------------------------


hash_token = email_tokens.hash_token  # the emailed-link handling every flow shares (core/email_tokens.py)


def link(token: str) -> str:
    return email_tokens.fragment_link(INVITE_NEXT, token)


def proof_required() -> bool:
    """A new account through an invitation needs the emailed link: in cloud mode, and on any installation with
    more than one organization, whose Admins needn't trust each other."""
    return bool(settings.SOFTWARDEN_CLOUD) or Organization.objects.all()[:2].count() > 1


def _signature(token: str, secret: str) -> str:
    digest = salted_hmac(MAILBOX_SALT, token, secret=secret, algorithm="sha256").digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def mailbox_token(token: str) -> str:
    """The emailed link's token: the invitation's token and its HMAC under SECRET_KEY. Only the server can make it, so
    holding it proves the mailbox it was sent to. A resend rotates the token, so the emailed link changes with it."""
    return token + _signature(token, settings.SECRET_KEY)


def split_token(token: Any) -> tuple[str, bool] | None:
    """(the invitation's own token, whether this is the emailed form), or None for something that can't be either.
    The signature is checked under SECRET_KEY and each of SECRET_KEY_FALLBACKS, so rotating the key with a fallback
    keeps the links already sent."""
    if not email_tokens.plausible(token):
        return None
    assert isinstance(token, str)
    if len(token) != TOKEN_LENGTH + SIGNATURE_LENGTH:
        return token, False
    base, signature = token[:TOKEN_LENGTH], token[TOKEN_LENGTH:]
    for secret in (settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", ())):
        if constant_time_compare(signature, _signature(base, secret)):
            return base, True
    return None


INSTALLATION_SUBJECT = "Your invitation to Softwarden"


def installation_transport(org) -> bool:
    """The organization's email goes out through the installation's own mail server (SOFTWARDEN_SMTP_*): it has no
    mail server of its own and the installation can send (the service's reputation, so a budget applies)."""
    from notify import channels
    from notify.models import EmailTransport

    row = channels.email_settings(org)
    return channels.effective_transport(row) == EmailTransport.INSTALLATION_DEFAULT and channels.email_configured(
        org, row
    )


INSTALLATION = "installation"
ORGANIZATION = "organization"


def delivery_route(org) -> str | None:
    """How the organization's invitation emails go out: INSTALLATION (the installation's own mail server, which alone
    may carry the link that proves the mailbox), ORGANIZATION (its own Graph or SMTP settings, which its Admin
    controls), or None (no email). Where a new account needs proof of the mailbox, the installation's server is
    used whenever it can send, even for an organization with a server of its own."""
    from notify import channels, transactional

    if installation_transport(org):
        return INSTALLATION
    if proof_required() and transactional.configured():
        return INSTALLATION
    if channels.email_configured(org):
        return ORGANIZATION
    return None


KEEP_SENT = timedelta(days=2)  # InstallationEmail rows older than this are deleted on the next send


def reserve_email(org, kind: str) -> None:
    """Count one invitation email through the installation's mail server against the organization's send budget
    (core/limits.py invitation_emails_hour and _day), or refuse it (409 /problems/limit-reached) before anything
    is written. Nothing to count when the organization has its own mail server, the installation can't send, or both
    budgets are unlimited. Call inside the write's transaction: the limits' locks are held until it ends, so two
    invitations at once can't both take the last place."""
    if delivery_route(org) != INSTALLATION:
        return
    if all(limits.effective(org, name) is None for name in limits.WINDOWS):
        return
    for name in limits.WINDOWS:
        limits.check_room(org, name)
    from core.models import InstallationEmail

    now = timezone.now()
    InstallationEmail.objects.filter(organization=org, created_at__lt=now - KEEP_SENT).delete()
    InstallationEmail.objects.create(organization=org, kind=kind)


COPY_ONCE_NOTE = (
    "This link joins an existing Softwarden account: sign in first. It can't create a new account; if you don't have "
    "one, ask the person who invited you.\n"
)


def proof_via(org) -> bool:
    """Whether the link that proves the mailbox will go out through the organization's own mail server: only while
    no proof is required (one organization); recorded on the invitation (Invitation.proof_via_organization)."""
    return delivery_route(org) == ORGANIZATION and not proof_required()


def email_link(invitation: Invitation, token: str) -> bool:
    """Email the invitation's link to the invitee once the invitation is committed, by `delivery_route`:
    through the installation's mail server with the link that proves the mailbox; through the organization's own
    channel with that link only where no proof is required, else with the copy-once link and a note that it can't
    create an account; or not at all. True when the server accepted it; otherwise the Admin copies the link
    from the response as before. The token is never logged or stored beyond its hash."""
    from notify import transactional

    org = invitation.organization
    route = delivery_route(org)
    if route is None:
        return False
    inviter = invitation.invited_by
    # The organization's own server carries the proof link only when that was decided, and recorded, with the token.
    proof_by_mail = route == INSTALLATION or invitation.proof_via_organization
    # Through the installation's server an organization name or an inviter's display name that reads as a web address
    # isn't repeated: the service's From address mustn't carry a link its customer typed. The inviter is then
    # named by their sign-in address.
    org_name = "an organization" if route == INSTALLATION and has_web_address(org.name) else org.name
    if inviter is None:
        who = "An Admin"
    else:
        name = inviter.get_full_name()
        if name and route == INSTALLATION and has_web_address(name):
            name = ""
        who = name or inviter.email
    text = (
        f"{who} invited you to join {org_name} on Softwarden as {Role(invitation.role).label}.\n\n"
        f"Accept the invitation here:\n\n{link(mailbox_token(token) if proof_by_mail else token)}\n\n"
        f"{'' if proof_by_mail else COPY_ONCE_NOTE}"
        f"The link works once and expires in {INVITATION_DAYS} days. If you weren't expecting this, ignore it.\n"
    )
    if route == INSTALLATION:
        # The Subject names no organization: an organization's name is whatever its Admin typed, and the
        # service's From address mustn't carry it in a Subject line.
        return transactional.send("invitation", invitation.email, INSTALLATION_SUBJECT, text)
    subject = f"You're invited to {org.name} on Softwarden"
    return transactional.send_for_org(org, "invitation", invitation.email, subject, text)


def mask_email(email: str) -> str:
    """j***@example.com: enough to recognise an address, not to read it off a screen."""
    local, at, domain = (email or "").partition("@")
    if not at:
        return "***"
    return f"{local[:1]}***@{domain}"


def status_of(invitation: Invitation, now=None) -> str:
    if invitation.accepted_at is not None:
        return "accepted"
    if invitation.revoked_at is not None:
        return "revoked"
    if invitation.expires_at <= (now or timezone.now()):
        return "expired"
    return "pending"


def account_for(email: str):
    """The account an email signs in as (usernames are the lower-case email), or None."""
    return get_user_model().objects.filter(username=(email or "").strip().lower()).first() if email else None


def reclaimable(account) -> bool:
    """A deactivated account that belongs to no organization: what an organization's deletion leaves behind
    (catalog/org_lifecycle.py), or SCIM's unlinked leftovers. Its address is available again: signing up or accepting an
    invitation retires the old row (`release_address`) and makes a fresh account for the new owner."""
    return account is not None and not account.is_active and not Membership.objects.filter(user=account).exists()


def proven(account) -> bool:
    """The account's owner proved its mailbox (a MailboxProof), or it has a password (every way to set one proves the
    mailbox or is the operator's: signup, an emailed invitation link, a reset link, the CLI; an Admin can't give one
    to an account without, admin_services.reset_password), or it is platform staff."""
    return (
        account.has_usable_password()
        or MailboxProof.objects.filter(user=account).exists()
        or PlatformStaff.objects.filter(user=account).exists()
    )


def record_proof(user, via: str) -> None:
    """Record that the account's owner proved its mailbox (or the operator vouched for it)."""
    MailboxProof.objects.get_or_create(user=user, defaults={"via": via})


def managed(account) -> bool:
    """An account an organization made that never proved its mailbox: pending-access approval and SCIM make
    them, with no password. Its organization's provider asserted the address; nobody showed they can read its mail.
    However many organizations it belongs to (joining another needs an emailed link, which proves it)."""
    return account is not None and not proven(account)


def organization_account(email: str) -> dict[str, str] | None:
    """For the emailed link's preview (only the mailbox's owner holds it): the organization that made an account for
    this address that doesn't hold it, and where to sign in through that organization's single sign-on. Creating an
    account from the link retires that one."""
    account = account_for(email)
    if account is None or not account.is_active or not managed(account):
        return None
    org = Organization.objects.filter(memberships__user=account).order_by("name").first()
    if org is None:
        return None
    return {"organization": org.name, "sign_in_url": f"/login?org={org.slug}"}


def releasable(account) -> bool:
    """An account that doesn't hold its address: reclaimable, or made by an organization (managed)."""
    return reclaimable(account) or managed(account)


def holder_for(email: str):
    """The account that holds an email: as account_for, but None for a reclaimable or organization-made one, whose
    address a signup or an emailed invitation may take over (release_address)."""
    account = account_for(email)
    return None if account is None or releasable(account) else account


RETIRED_DOMAIN = "invalid"  # RFC 2606: never deliverable


def retired_address(user_id: int) -> str:
    return f"retired+{user_id}@{RETIRED_DOMAIN}"


def release_address(account, via: str) -> int | None:
    """Free a releasable account's address for a new owner who just proved the mailbox (also for
    an organization-made account): the old row stays, inactive, as the person other organizations' history names
    (audit actors, record owners, saved views), but its username and email become `retired+<id>@invalid`, its
    password unusable and its sign-in identities removed. Nothing of it passes to the new owner, who gets a fresh
    account. An organization-made account keeps its membership (its organization's history) but is deactivated there,
    and that organization records `user.address_reclaimed` (the old account's id and `via`; not who took it). Locks
    the row and checks again; returns the retired row's id, or None when it isn't releasable under that address any
    more (claimed a moment ago). The caller audits the release in the new owner's organization (audit_release).

    A row with no name gets its old address as its first name, so references to it keep showing what they showed."""
    user = get_user_model().objects.select_for_update().filter(pk=account.pk).first()
    if user is None or user.username != account.username or not releasable(user):
        return None
    was_active = user.is_active
    if not user.get_full_name():
        user.first_name = (user.email or user.username)[:150]
    user.username = user.email = retired_address(user.pk)
    user.set_unusable_password()
    user.is_active = False
    user.save(update_fields=["first_name", "username", "email", "password", "is_active"])
    UserIdentity.objects.filter(user=user).delete()
    for org in Organization.objects.filter(memberships__user=user).distinct():
        changes: dict[str, Any] = {"user": user.pk, "via": via}
        if was_active:
            changes["active"] = [True, False]
        audit(org, None, "user.address_reclaimed", None, changes)
    return user.pk


def lookup(token: Any, lock: bool = False) -> tuple[Invitation, bool] | None:
    """(the invitation a link names while it can still be accepted, whether the link proves the mailbox), or None.
    Live: not used, revoked or expired. A link that proves the mailbox (the emailed one, or either one where no proof
    is required) is also refused when the email is a deactivated account's; the copy-once link isn't told that
    (its holder may be any organization's Admin), and can't create an account anyway."""
    parts = split_token(token)
    if parts is None:
        return None
    token, emailed = parts
    rows = Invitation.objects.select_related("organization")
    if lock:
        # The organization first, then the invitation: the order create_invitation and SCIM provisioning (which
        # revokes and makes invitations) take them in, so an acceptance never waits on them the other way round.
        found = rows.filter(token_hash=hash_token(token)).values_list("organization_id", flat=True).first()
        if found is None:
            return None
        Organization.objects.select_for_update().filter(pk=found).values_list("pk", flat=True).first()
        rows = rows.select_for_update(of=("self",))
    invitation = rows.filter(token_hash=hash_token(token)).first()
    if invitation is None or status_of(invitation) != "pending":
        return None
    # The emailed link proves the mailbox where proof is required only if the installation's server carried it: one
    # an organization's own server carried (allowed while there was one organization) was its Admin's to read.
    proves = not proof_required() or (emailed and not invitation.proof_via_organization)
    if proves:
        account = holder_for(invitation.email)
        if account is not None and not account.is_active:
            return None
    return invitation, proves


def find_live(token: Any, lock: bool = False) -> Invitation | None:
    """As lookup, the invitation alone."""
    found = lookup(token, lock)
    return found[0] if found is not None else None


def remember(invitation: Invitation) -> dict[str, str]:
    """The SESSION_INVITATION entry for an invitation: its id and a prefix of its token hash (never the token)."""
    return {"id": str(invitation.id), "hash": invitation.token_hash[:HASH_PREFIX]}


def live_by_id(remembered: Any) -> Invitation | None:
    """As find_live, by the SESSION_INVITATION entry an invitation preview kept (core/oidc.invitee). None unless the
    invitation's token hash still starts with the remembered prefix: a resend (a new token) ends the entry."""
    if not isinstance(remembered, dict):
        return None
    invitation_id, prefix = remembered.get("id"), remembered.get("hash")
    if not isinstance(invitation_id, str) or not isinstance(prefix, str) or len(prefix) != HASH_PREFIX:
        return None
    try:
        invitation = Invitation.objects.select_related("organization").filter(pk=invitation_id).first()
    except ValidationError:  # not a UUID
        return None
    if invitation is None or not secrets.compare_digest(invitation.token_hash[:HASH_PREFIX], prefix):
        return None
    if status_of(invitation) != "pending":
        return None
    account = holder_for(invitation.email)
    if account is not None and not account.is_active:
        return None
    return invitation


def _role(data: dict[str, Any], errors: dict[str, list[str]]) -> str:
    role = data.get("role")
    if role not in Role.values:
        errors["role"] = ["Choose a role."]
        return ""
    return str(role)


def _split_name(name: str) -> tuple[str, str]:
    first, _, last = name.strip().partition(" ")
    return first[:150], last.strip()[:150]


# ---- invitations ----------------------------------------------------------------------------------------------------


def _new_token() -> tuple[str, str]:
    return email_tokens.new_token()


@transaction.atomic
def create_invitation(org, actor, data, emailing: bool = False) -> tuple[Invitation, str]:
    """A new invitation and its token (shown once, as a link). A live invitation for the same email is revoked: one
    live invitation per email per organization. `emailing`: the caller emails the link once this commits (the
    routes do; SCIM doesn't), so it counts against the send budget first (reserve_email). Refused for a member. Anyone else is invited alike, a deactivated
    account elsewhere included (the answer never says whether an address has an account elsewhere); such an
    invitation's emailed link answers as unavailable."""
    raw = data.get("email")
    email = raw.strip().lower() if isinstance(raw, str) else ""
    errors: dict[str, list[str]] = {}
    existing = None
    try:
        EmailValidator()(email)
        if len(email) > EMAIL_MAX:
            raise ValidationError("too long")
    except ValidationError:
        errors["email"] = ["Enter an email address."]
    else:
        # Any account with the address, an organization-made one included: inside its own organization it is the
        # member (a releasable account elsewhere accepts as a new one).
        existing = account_for(email)
        if existing is not None and Membership.objects.filter(user=existing, organization=org).exists():
            errors["email"] = ["This person is already a member of this organization."]
    name = data.get("name") or ""
    if not isinstance(name, str) or len(name.strip()) > NAME_MAX:
        errors["name"] = [f"Use {NAME_MAX} characters or fewer."]
    role = _role(data, errors)
    if errors:
        raise FieldErrors(errors)
    # One invitation at a time per organization: the replace-then-create below can't race another for the same email.
    Organization.objects.select_for_update().filter(pk=org.pk).values_list("pk", flat=True).first()
    # No room for another member: refused now rather than when the invitee accepts (which checks again).
    limits.check_room(org, limits.MEMBERS)
    if emailing:
        reserve_email(org, "invitation")
    now = timezone.now()
    replaced = Invitation.objects.filter(
        organization=org, email=email, accepted_at__isnull=True, revoked_at__isnull=True
    ).update(revoked_at=now)
    token, digest = _new_token()
    invitation = Invitation.objects.create(
        organization=org,
        email=email,
        name=name.strip(),
        role=role,
        token_hash=digest,
        invited_by=actor,
        expires_at=now + timedelta(days=INVITATION_DAYS),
        proof_via_organization=emailing and proof_via(org),
    )
    # Nothing about an existing account (its id, so its name, or that it exists) before it consents; the acceptance
    # records the user.
    changes = {"role": role, "email": mask_email(email), "replaced": bool(replaced)}
    audit(org, actor, "user.invited", invitation, changes)
    return invitation, token


def _locked(invitation: Invitation) -> Invitation:
    return Invitation.objects.select_for_update(of=("self",)).get(pk=invitation.pk)


@transaction.atomic
def revoke_invitation(org, actor, invitation: Invitation) -> Invitation:
    invitation = _locked(invitation)
    if invitation.accepted_at is not None or invitation.revoked_at is not None:
        raise Conflict(f"This invitation was already {status_of(invitation)}.")
    invitation.revoked_at = timezone.now()
    invitation.save(update_fields=["revoked_at", "updated_at"])
    audit(org, actor, "invitation.revoked", invitation, {"email": mask_email(invitation.email)})
    return invitation


@transaction.atomic
def resend_invitation(org, actor, invitation: Invitation) -> tuple[Invitation, str]:
    """A new token (the old link stops working) and a fresh expiry, for an invitation not yet used or revoked. The
    route emails the new link, so it counts against the send budget (reserve_email)."""
    Organization.objects.select_for_update().filter(pk=org.pk).values_list("pk", flat=True).first()
    invitation = _locked(invitation)
    if invitation.accepted_at is not None or invitation.revoked_at is not None:
        raise Conflict(f"This invitation was already {status_of(invitation)}. Invite them again instead.")
    reserve_email(org, "invitation.resent")
    token, digest = _new_token()
    invitation.token_hash = digest
    invitation.expires_at = timezone.now() + timedelta(days=INVITATION_DAYS)
    invitation.proof_via_organization = proof_via(org)
    invitation.save(update_fields=["token_hash", "expires_at", "proof_via_organization", "updated_at"])
    audit(org, actor, "invitation.resent", invitation, {"email": mask_email(invitation.email)})
    return invitation, token


def _accepted(invitation: Invitation, user, new_account: bool, already_member: bool = False) -> None:
    invitation.accepted_at = timezone.now()
    invitation.accepted_by = user
    invitation.save(update_fields=["accepted_at", "accepted_by", "updated_at"])
    changes: dict[str, Any] = {"user": user.id, "role": invitation.role, "new_account": new_account}
    if already_member:
        changes["already_member"] = True
    audit(invitation.organization, user, "invitation.accepted", invitation, changes)
    membership = Membership.objects.get(user=user, organization=invitation.organization)
    invitation_accepted.send(sender=Invitation, invitation=invitation, user=user, membership=membership)


@transaction.atomic
def accept_as_new_account(token: str, data: dict[str, Any]):
    """Create the invitee's account (their name and password, Django's validators) and its membership. Returns the
    user. Only from a link that proves the mailbox (EmailedLinkRequired otherwise). An email that has an account
    by now must sign in and accept as that account instead; a reclaimable account (deactivated, in no organization) is
    retired first and the invitee gets a fresh account."""
    found = lookup(token, lock=True)
    if found is None:
        raise InvitationUnavailable()
    invitation, proves = found
    if not proves:
        raise EmailedLinkRequired()
    if holder_for(invitation.email) is not None:
        raise SignInFirst()
    leftover = account_for(invitation.email)
    errors: dict[str, list[str]] = {}
    name = data.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        errors["name"] = ["Enter your name."]
    elif len(name) > NAME_MAX:
        errors["name"] = [f"Use {NAME_MAX} characters or fewer."]
    password = data.get("password")
    first, last = _split_name(name)
    user = get_user_model()(username=invitation.email, email=invitation.email, first_name=first, last_name=last)
    if not isinstance(password, str) or not password:
        errors["password"] = ["Choose a password."]
    else:
        try:
            validate_password(password, user)
        except ValidationError as e:
            errors["password"] = list(e.messages)
    if errors:
        raise FieldErrors(errors)
    limits.check_room(invitation.organization, limits.MEMBERS)
    released = release_address(leftover, "invitation") if leftover is not None else None
    user.set_password(password)
    try:
        with transaction.atomic():
            user.save()
    except IntegrityError:  # the same email got an account a moment ago (another organization's invitation)
        raise SignInFirst() from None
    Membership.objects.create(user=user, organization=invitation.organization, role=invitation.role)
    record_proof(user, "invitation")
    if released is not None:
        audit_release(invitation.organization, user, released, "invitation")
    _accepted(invitation, user, new_account=True)
    return user


def audit_release(org, user, released: int, via: str) -> None:
    """The organization the new owner joins records that a retired account's address was handed on (its id only)."""
    audit(org, user, "user.address_released", None, {"user": user.id, "retired_user": released, "via": via})


@transaction.atomic
def accept_as_existing_account(token: str, user) -> Invitation:
    """The signed-in account accepts: it must be the invited email's account. Creates the membership (or, when they
    joined meanwhile, only marks the invitation used). An account an organization made (`managed`) joins only through
    a link that proves the mailbox, which then counts as its proof: otherwise any organization with the same
    identity provider could invite it, sign in as it through that provider and so make it hold the address."""
    found = lookup(token, lock=True)
    if found is None:
        raise InvitationUnavailable()
    invitation, proves = found
    if (user.username or "").lower() != invitation.email or not user.is_active:
        raise WrongAccount()
    if managed(user):
        if not proves:
            raise EmailedLinkRequired()
        record_proof(user, "invitation")
    if not Membership.objects.filter(user=user, organization=invitation.organization).exists():
        limits.check_room(invitation.organization, limits.MEMBERS)
    _, created = Membership.objects.get_or_create(
        user=user, organization=invitation.organization, defaults={"role": invitation.role}
    )
    _accepted(invitation, user, new_account=False, already_member=not created)
    return invitation


# ---- pending access -----------------------------------------------------------------------------------------------


def _pin(user, row: PendingAccess) -> None:
    """Pin the pending identity to `user`, as a first sign-in would (core/oidc.match_user)."""
    if UserIdentity.objects.filter(user=user, issuer=row.issuer).exclude(subject=row.subject).exists():
        raise Conflict(
            "This account already signs in with another identity at this provider. Unlink that one first "
            "(Users, the person's sign-in identities)."
        )
    UserIdentity.objects.get_or_create(user=user, issuer=row.issuer, subject=row.subject)


NEEDS_INVITATION = (
    "An account with this email already exists on this Softwarden instance, so it can't be approved from here. "
    "Invite the person instead: they accept while signed in to their account."
)


@transaction.atomic
def approve_pending(org, actor, row: PendingAccess, data: dict[str, Any]) -> Membership:
    """Give a pending identity access with a role:
    - no account with its email: a new account (username = email, unusable password), the membership and the pinned
      identity;
    - an account that is a member here already: the identity is pinned and the role left as it is;
    - an account with any membership in another organization (a member here too included), with no membership at
      all (it has a password or another way in of its own), or deactivated: 409 /problems/invitation-required,
      since pinning it through this organization's provider is what confining a session to its organization forbids.
    The row is removed in every successful case."""
    errors: dict[str, list[str]] = {}
    role = _role(data, errors)
    if errors:
        raise FieldErrors(errors)
    Organization.objects.select_for_update().filter(pk=org.pk).values_list("pk", flat=True).first()
    found = PendingAccess.objects.select_for_update().filter(pk=row.pk, organization=org).first()
    if found is None:
        raise exceptions.NotFound("This request for access was already approved or declined.")
    row = found
    if not row.email:
        raise Conflict("The identity provider sent no email for this person, so no account can be made for them.")
    taken = UserIdentity.objects.filter(issuer=row.issuer, subject=row.subject).first()
    account = account_for(row.email)
    if taken is not None and (account is None or taken.user_id != account.pk):
        raise Conflict(NEEDS_INVITATION, {"type": INVITATION_REQUIRED})
    if account is not None and PlatformStaff.objects.filter(user=account).exists():
        # As SCIM (ee/scim/provisioning.py): no organization's provider takes over the operator's account.
        raise Conflict(NEEDS_INVITATION, {"type": INVITATION_REQUIRED})
    new_account = account is None
    if account is None:
        first, last = _split_name(row.name)
        limits.check_room(org, limits.MEMBERS)
        account = get_user_model().objects.create_user(
            username=row.email, email=row.email, password=None, first_name=first, last_name=last
        )
        membership = Membership.objects.create(user=account, organization=org, role=role)
    else:
        memberships = list(Membership.objects.select_for_update().filter(user=account).order_by("id"))
        here = next((m for m in memberships if m.organization_id == org.pk), None)
        elsewhere = any(m.organization_id != org.pk for m in memberships)
        # An account with no membership here (none anywhere included) keeps its own way in; it is invited.
        if not account.is_active or elsewhere or here is None:
            raise Conflict(NEEDS_INVITATION, {"type": INVITATION_REQUIRED})
        membership = here
    _pin(account, row)
    provider = oidc_discovery.provider_label(row.issuer)
    row.delete()
    changes: dict[str, Any] = {"user": account.id, "role": membership.role, "provider": provider}
    changes["new_account"] = new_account
    audit(org, actor, "pending_access.approved", membership, changes)  # the member's membership: their History
    return membership


@transaction.atomic
def decline_pending(org, actor, row: PendingAccess) -> None:
    """Delete the request, under the same row lock approve takes, so a decline and an approval can't both happen."""
    found = PendingAccess.objects.select_for_update().filter(pk=row.pk, organization=org).first()
    if found is None:
        raise exceptions.NotFound("This request for access was already approved or declined.")
    changes = {"email": mask_email(found.email), "provider": oidc_discovery.provider_label(found.issuer)}
    found.delete()
    found.pk = row.pk  # Django clears it on delete; the event names the request that was declined
    audit(org, actor, "pending_access.declined", found, changes)


# ---- sign-in identities -------------------------------------------------------------------------------------------


@transaction.atomic
def unlink_identity(org, actor, membership, identity_id) -> None:
    """An Admin unlinks one of a member's sign-in identities; the next sign-in from that provider matches by email
    again. Refused for an account that also belongs to another organization (SHARED_ACCOUNT, as the unlink of all)."""
    if _other_organizations(membership):
        raise Conflict(shared_refusal(membership))
    identity = UserIdentity.objects.filter(user_id=membership.user_id, pk=identity_id).first()
    if identity is None:
        raise exceptions.NotFound("This member has no sign-in identity with this ID.")
    issuer = identity.issuer
    identity.delete()
    changes = {"user": membership.user_id, "method": "sso", "issuers": [issuer]}
    audit(org, actor, "user.identity_unlinked", None, changes)


def configured_issuers(org_ids) -> set[str]:
    """The issuers any of these organizations' sign-in settings name (active or draft), plus the installation's legacy
    Entra settings. An identity at one of them still has an organization that answers for it."""
    from core import sso
    from core.models import SsoSettings

    found: set[str] = set()
    for active, draft in SsoSettings.objects.filter(organization_id__in=list(org_ids)).values_list(
        "issuer", "draft_issuer"
    ):
        found |= {i for i in (active, draft) if i}
    env = sso.legacy_env()
    if env is not None:
        found.add(oidc_discovery.urls("entra", tenant=env["tenant"])[1])
    return found


def unlink_after_removal(org, actor, user) -> None:
    """Someone left `org` (removed by an Admin): unlink their identities whose issuer no organization they still
    belong to configures, and no legacy environment setting, so the old organization's provider keeps no foothold
    on the account. Audited in `org` with reason membership_removed. Call inside the removal's
    transaction, after the membership is gone."""
    remaining = Membership.objects.filter(user=user).values_list("organization_id", flat=True)
    kept = configured_issuers(remaining)
    rows = UserIdentity.objects.filter(user=user).exclude(issuer__in=kept)
    issuers = sorted(set(rows.values_list("issuer", flat=True)))
    if not issuers:
        return
    rows.delete()
    changes = {"user": user.id, "method": "sso", "issuers": issuers, "reason": "membership_removed"}
    audit(org, actor, "user.identity_unlinked", None, changes)
