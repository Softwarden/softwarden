"""Public signup (cloud): an email, a name, an organization name and a password become, once the email is
confirmed, a new account, a new organization in the signup edition (cloud, core/capabilities.SIGNUP_EDITION) and its
Admin membership. Routes in catalog/signup_views.py.

Off unless the installation turns it on (SOFTWARDEN_SIGNUP_ENABLED), and refused while the installation can't send
email (SOFTWARDEN_EMAIL_CONFIGURED): confirming the email is the point.

- Nothing exists until the email is confirmed: a PendingSignup row holds the request (core/models.py). An inactive
  User would claim the address, so an invitation or the real owner's own signup would find an "account" nobody can use.
- The confirmation link is `{SOFTWARDEN_BASE_URL}/signup/verify#<token>`: single use, SIGNUP_HOURS, only its SHA-256
  stored, in a fragment and then a request body, never a logged path (as invitations). Signing up again or resending
  rotates the token, so earlier links stop working.
- Confirming asks for the password again. Without that, someone who knows your address could sign up as you with a
  password of their choosing and wait for you to click the link they caused to be sent.
- No account enumeration: signing up with an email that has an account answers exactly as a new one does, and emails
  the account a short "you already have an account" note instead of a link. Resending answers the same whatever the
  email. Each address gets at most one message per RESEND_COOLDOWN and SEND_LIMIT per SEND_WINDOW; an expired
  signup starts afresh, so nobody can lock an address out for good.
- The message is sent inside the signup's transaction: when the server refuses it, the new link, its expiry and
  the cooldown are rolled back, and the answer is 503 /problems/email-failed.
- A deactivated account in no organization (an organization's deletion leaves those), and an account an organization
  made through single sign-on or SCIM that never had a password (invitations.managed), don't hold their
  address: signing up with it works as for a new address. Confirming retires the old row (it stays, inactive, as
  `retired+<id>@invalid`, for other organizations' history) and creates a fresh account; the release is audited in
  the new organization as `user.address_released`.

Every write is in one transaction; the organization's first events (`user.created` with `via: signup`, `user.login`)
are written in the new organization. A pending signup is no organization's, so its own writes aren't audited, like
the first-run setup token's (core/bootstrap.py)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from zoneinfo import available_timezones

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import exceptions, status

from core import capabilities, email_tokens, limits
from core.input import WEB_ADDRESS_MESSAGE, has_web_address
from core.models import Membership, Organization, PendingSignup, Role
from core.problems import Conflict, FieldErrors

from .invitations import account_for, audit_release, holder_for, record_proof, release_address
from .services import audit

SIGNUP_HOURS = 24
RESEND_COOLDOWN = timedelta(seconds=60)
SEND_LIMIT = 5  # messages per address per SEND_WINDOW, resends included
SEND_WINDOW = timedelta(hours=1)
EMAIL_MAX = 254
NAME_MAX = 200
VERIFY_PATH = "/signup/verify"
NOTE_CACHE = "softwarden:signup-note:"  # + sha256(email): the "you already have an account" note's cooldown

DISABLED = "/problems/signup-disabled"
UNAVAILABLE = "/problems/signup-unavailable"
LINK_UNAVAILABLE = "/problems/signup-link-unavailable"
EMAIL_FAILED = "/problems/email-failed"
PAUSED = "/problems/signup-paused"
CEILING_WINDOW = timedelta(hours=24)


class SignupDisabled(exceptions.NotFound):
    def __init__(self):
        super().__init__(
            "Signup isn't available on this installation. Ask an Admin of your organization to invite you."
        )
        self.extra = {"type": DISABLED}


class SignupUnavailable(exceptions.APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    def __init__(self):
        super().__init__(
            "Signup is on, but this installation can't send email, so it can't confirm your address. The operator "
            "needs to set SOFTWARDEN_SMTP_HOST."
        )
        self.extra = {"type": UNAVAILABLE}


class EmailFailed(exceptions.APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    def __init__(self):
        super().__init__("We couldn't send the email just now. Try again in a few minutes.")
        self.extra = {"type": EMAIL_FAILED}


class SignupPaused(exceptions.APIException):
    """503: the installation's daily ceiling on new organizations (SOFTWARDEN_SIGNUP_DAILY_LIMIT) is reached."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    def __init__(self):
        super().__init__(
            "We aren't creating new organizations for a few hours. Try again later; a confirmation link you already "
            "have keeps working until it expires."
        )
        self.extra = {"type": PAUSED}


class LinkUnavailable(exceptions.NotFound):
    """One answer for an unknown, used, replaced or expired link."""

    def __init__(self):
        super().__init__("This confirmation link isn't valid any more. Sign up again to get a new one.")
        self.extra = {"type": LINK_UNAVAILABLE}


def enabled() -> bool:
    return bool(settings.SOFTWARDEN_SIGNUP_ENABLED)


def available() -> bool:
    """Signup is offered: turned on, and email can be sent (auth/options `signup_enabled`)."""
    from notify import transactional

    return enabled() and transactional.configured()


def require_available() -> None:
    from notify import transactional

    if not enabled():
        raise SignupDisabled()
    if not transactional.configured():
        raise SignupUnavailable()


def require_room_today() -> None:
    """At most SOFTWARDEN_SIGNUP_DAILY_LIMIT organizations created in the last CEILING_WINDOW (0: no ceiling),
    counted from the database so every app process and pod agrees. Checked when a signup starts and again when it is
    confirmed (the creation itself); two confirmations at the same moment may pass it together, which a ceiling (not a
    quota) allows."""
    ceiling = int(getattr(settings, "SOFTWARDEN_SIGNUP_DAILY_LIMIT", 0) or 0)
    if ceiling <= 0:
        return
    if Organization.objects.filter(created_at__gt=timezone.now() - CEILING_WINDOW).count() >= ceiling:
        raise SignupPaused()


hash_token = email_tokens.hash_token  # the emailed-link handling every flow shares (core/email_tokens.py)


def link(token: str) -> str:
    return email_tokens.fragment_link(VERIFY_PATH, token)


def _email(raw: Any) -> str | None:
    email = raw.strip().lower() if isinstance(raw, str) else ""
    try:
        EmailValidator()(email)
    except ValidationError:
        return None
    return email if len(email) <= EMAIL_MAX else None


CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _text(data: dict[str, Any], key: str, missing: str, errors: dict[str, list[str]]) -> str:
    value = data.get(key)
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        errors[key] = [missing]
    elif len(value) > NAME_MAX:
        errors[key] = [f"Use {NAME_MAX} characters or fewer."]
    elif CONTROL.search(value):  # it ends up in email subjects and headers
        errors[key] = ["Use one line of text, without tabs or line breaks."]
    elif has_web_address(value):  # and in a mail to an address the sender may not own
        errors[key] = [WEB_ADDRESS_MESSAGE]
    return value


def _split_name(name: str) -> tuple[str, str]:
    first, _, last = name.strip().partition(" ")
    return first[:150], last.strip()[:150]


# ---- messages -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    """An email to send. `kind` is for the log only."""

    kind: str
    to: str
    subject: str
    text: str


def _confirmation(row: PendingSignup, token: str) -> Message:
    hours = SIGNUP_HOURS
    text = (
        f"Hello {row.name},\n\n"
        f"Confirm your email to finish creating {row.organization_name} on Softwarden:\n\n"
        f"{link(token)}\n\n"
        f"The link works once, for {hours} hours. You'll be asked for the password you chose.\n\n"
        "If you didn't sign up, ignore this email: nothing is created until the address is confirmed.\n"
    )
    return Message("signup.confirm", row.email, "Confirm your email for Softwarden", text)


def _account_note(email: str) -> Message:
    text = (
        "Someone, probably you, tried to sign up for Softwarden with this email address.\n\n"
        f"You already have an account. Sign in at {settings.SOFTWARDEN_BASE_URL}/login.\n\n"
        "To join another organization, ask one of its Admins to invite you. If this wasn't you, you can ignore this "
        "email; nothing has changed.\n"
    )
    return Message("signup.account_exists", email, "You already have a Softwarden account", text)


def _deliver(message: Message) -> None:
    """Send now, inside the caller's transaction: a refusal raises EmailFailed and rolls the caller's writes back."""
    from notify import transactional

    if not transactional.send(message.kind, message.to, message.subject, message.text):
        raise EmailFailed()


def _note_key(email: str) -> str:
    return NOTE_CACHE + hashlib.sha256(email.encode()).hexdigest()


def _send_note(email: str) -> None:
    """At most one "already have an account" note per address per RESEND_COOLDOWN (process cache, as throttles); the
    cooldown starts only once a note was sent."""
    key = _note_key(email)
    if cache.get(key):
        return
    _deliver(_account_note(email))
    cache.set(key, 1, int(RESEND_COOLDOWN.total_seconds()))


def _fresh_window(row: PendingSignup, now) -> None:
    """An expired signup starts afresh, and the send count is per SEND_WINDOW."""
    if row.expires_at <= now or row.window_started_at <= now - SEND_WINDOW:
        row.send_count = 0
        row.window_started_at = now


def _sendable(row: PendingSignup, now) -> bool:
    return row.send_count < SEND_LIMIT and row.last_sent_at <= now - RESEND_COOLDOWN


def _rotate_and_send(row: PendingSignup, now) -> None:
    """A new token, expiry and send record, saved, then the link sent; a refused send rolls them back (the caller's
    transaction).

    The transaction, and the row lock, are held while the message is sent: up to notify/transactional.DEADLINE seconds
    (20) for a slow SMTP server. So PostgreSQL's `idle_in_transaction_session_timeout` must never be set below that for
    the app's role, or a slow send would end the session and fail the signup (docs/ENV.md "Database")."""
    token, row.token_hash = email_tokens.new_token()
    row.expires_at = now + timedelta(hours=SIGNUP_HOURS)
    row.last_sent_at = now
    row.send_count += 1
    row.save()
    _deliver(_confirmation(row, token))


# ---- services -----------------------------------------------------------------------------------------------------


@transaction.atomic
def start(data: dict[str, Any]) -> None:
    """Validate a signup, record it and send its link (or the note for an existing account; nothing when this address
    was just sent one or is at its limit). The validation never depends on whether the email has an account, so
    neither does the answer."""
    require_available()
    errors: dict[str, list[str]] = {}
    email = _email(data.get("email"))
    if email is None:
        errors["email"] = ["Enter an email address."]
    name = _text(data, "name", "Enter your name.", errors)
    org_name = _text(data, "organization_name", "Enter your organization's name.", errors)
    zone = data.get("timezone") or "UTC"
    if not isinstance(zone, str) or zone not in available_timezones():
        errors["timezone"] = ["Choose a timezone such as America/New_York."]
    password = data.get("password")
    if not isinstance(password, str) or not password:
        errors["password"] = ["Choose a password."]
    else:
        first, last = _split_name(name)
        probe = get_user_model()(username=email or "", email=email or "", first_name=first, last_name=last)
        try:
            validate_password(password, probe)
        except ValidationError as e:
            errors["password"] = list(e.messages)
    if errors:
        raise FieldErrors(errors)
    require_room_today()  # the same answer whether or not the email has an account
    assert email is not None and isinstance(password, str)
    # Hashed on every path, so the time an answer takes doesn't tell an existing account from a new address.
    password_hash = make_password(password)
    if holder_for(email) is not None:
        PendingSignup.objects.filter(email=email).delete()  # an account exists by now; its signup can't finish
        _send_note(email)
        return
    now = timezone.now()
    row = PendingSignup.objects.select_for_update().filter(email=email).first()
    fields = {"name": name, "organization_name": org_name, "timezone": zone, "password_hash": password_hash}
    if row is None:
        row = PendingSignup(email=email, send_count=0, window_started_at=now, last_sent_at=now, **fields)
        try:
            with transaction.atomic():
                _rotate_and_send(row, now)
        except IntegrityError:  # the same email signed up a moment ago in another request: that one sends the link
            pass
        return
    _fresh_window(row, now)
    if not _sendable(row, now):
        return
    for key, value in fields.items():
        setattr(row, key, value)
    _rotate_and_send(row, now)


@transaction.atomic
def resend(data: dict[str, Any]) -> None:
    """A new link for a pending signup (the old one stops working). Nothing, and the same answer, for an email with no
    pending signup, one sent a moment ago, or one at its limit."""
    require_available()
    email = _email(data.get("email"))
    if email is None:
        raise FieldErrors({"email": ["Enter an email address."]})
    now = timezone.now()
    row = PendingSignup.objects.select_for_update().filter(email=email).first()
    if row is None or holder_for(email) is not None:
        return
    _fresh_window(row, now)
    if _sendable(row, now):
        _rotate_and_send(row, now)


def _live(token: Any) -> PendingSignup | None:
    if not email_tokens.plausible(token):
        return None
    row = PendingSignup.objects.select_for_update().filter(token_hash=hash_token(token)).first()
    if row is None or row.expires_at <= timezone.now():
        return None
    return row


TAKEN = "This email has an account already. Sign in instead."


def verify(data: dict[str, Any]) -> Membership:
    """Confirm the email: the password chosen at signup must be given again. Creates the account (with that password's
    hash; a reclaimable account holding the address is retired first), the organization in the signup edition, and
    the account's Admin
    membership; the pending signup is deleted. Returns the membership; the view signs the account in. An email that
    got an account meanwhile (an invitation) ends the signup: its row is deleted and the answer is 409. Two
    confirmations at once: the row lock makes the second find no live link (404)."""
    require_available()
    with transaction.atomic():
        row = _live(data.get("token"))
        if row is None:
            raise LinkUnavailable()
        password = data.get("password")
        if not isinstance(password, str) or not password or not check_password(password, row.password_hash):
            raise FieldErrors({"password": ["Enter the password you chose when you signed up."]})
        if holder_for(row.email) is None:
            require_room_today()  # the signup ceiling; the pending signup stays, to be confirmed later
            return _create(row)
        row.delete()  # committed before the refusal below
    raise Conflict(TAKEN)


def _create(row: PendingSignup) -> Membership:
    first, last = _split_name(row.name)
    leftover = account_for(row.email)
    released = release_address(leftover, "signup") if leftover is not None else None
    user = get_user_model()(username=row.email, email=row.email, first_name=first, last_name=last)
    user.password = row.password_hash  # already hashed by Django's hasher at signup
    try:
        with transaction.atomic():
            user.save()
    except IntegrityError:  # claimed a moment ago (an invitation for the same address)
        raise Conflict(TAKEN) from None
    org = capabilities.create_signup_organization(name=row.organization_name, timezone=row.timezone, coordinator=user)
    limits.check_room(org, limits.MEMBERS)  # a new organization: room for its first Admin under any members limit
    membership = Membership.objects.create(user=user, organization=org, role=Role.ADMIN)
    record_proof(user, "signup")
    row.delete()
    if released is not None:
        audit_release(org, user, released, "signup")
    audit(org, user, "user.created", None, {"user": user.id, "role": membership.role, "via": "signup"})
    audit(org, user, "user.login", None, {"user": user.id})
    return membership


def purge_expired(now) -> dict[str, int]:
    """The `signup.purge` schedule: delete pending signups past their expiry."""
    removed, _ = PendingSignup.objects.filter(expires_at__lte=now).delete()
    return {"deleted": removed}
