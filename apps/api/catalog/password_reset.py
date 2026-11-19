"""Self-service password reset. Routes in catalog/password_reset_views.py.

    POST auth/password-reset           {email}            202, the same answer for any email
    POST auth/password-reset/confirm   {token, password}  204: the password is set and every session of the account ends

Offered while the installation can send email (SOFTWARDEN_EMAIL_CONFIGURED; auth/options `password_reset_enabled`);
otherwise 503 /problems/password-reset-unavailable and an operator resets with `manage.py reset_admin`.

- Who gets a link: an active account with a password that belongs to at least one organization. Not an account that
  signs in only through single sign-on or SCIM (it has no password to reset: a link would hand a password to whoever
  reads the mailbox of an account an organization made), and not platform staff (the operator's accounts
  reset through the shell, `manage.py reset_admin`, so one mailbox never opens the platform console).
- No account enumeration: the answer is the same 202 for an unknown address, an address that gets no link, one at its
  limit and one that was sent a link, and a refused message doesn't change it (it is logged by kind only). Nor does
  the time it takes: every request does the same work, one indexed lookup and one queued `password_reset.send` job
  whose payload is the account's id or null (never an address or a token); the worker decides, makes the link and
  sends it on its next tick (`send_link`).
- The link is `{SOFTWARDEN_BASE_URL}/reset-password#<token>` (core/email_tokens.py: the token in the fragment, only its
  SHA-256 stored), single use, good for RESET_MINUTES. Each account gets at most SEND_LIMIT links per SEND_WINDOW and
  one per COOLDOWN, counted from its PasswordReset rows, so every app process sees the same count.
- Using a link sets the password (Django's validators), marks every open link of the account used, clears a forced
  password change in every organization, and ends every session of the account: the session hash Django keeps in
  each session is derived from the password hash, so each other session is signed out on its next request. Nobody is
  signed in by the reset; the person signs in with the new password. API tokens are the organizations' and stay.
- Audited in every organization of the account (`user.password_reset_requested` when a link is sent, with no actor;
  `user.password_reset_completed` when one is used), never with the token or the password."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db import transaction
from django.utils import timezone
from rest_framework import exceptions, status

from core import email_tokens
from core.models import Membership, PasswordReset, PlatformStaff
from core.problems import FieldErrors

from .services import audit_account

log = logging.getLogger("softwarden.email")

RESET_MINUTES = 60
RESET_PATH = "/reset-password"
SEND_LIMIT = 3  # links per account per SEND_WINDOW
SEND_WINDOW = timedelta(hours=1)
COOLDOWN = timedelta(seconds=60)
KEEP = timedelta(days=1)  # rows older than this are deleted when the next reset is asked for
EMAIL_MAX = 254

UNAVAILABLE = "/problems/password-reset-unavailable"
LINK_UNAVAILABLE = "/problems/password-reset-link-unavailable"


class ResetUnavailable(exceptions.APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    def __init__(self):
        super().__init__(
            "This installation can't send email, so it can't reset passwords by email. Ask your administrator to "
            "reset yours."
        )
        self.extra = {"type": UNAVAILABLE}


class LinkUnavailable(exceptions.NotFound):
    """One answer for an unknown, used, replaced or expired link, and for an account that can't use it any more."""

    def __init__(self):
        super().__init__("This password reset link isn't valid any more. Ask for a new one.")
        self.extra = {"type": LINK_UNAVAILABLE}


def available() -> bool:
    from notify import transactional

    return transactional.configured()


def require_available() -> None:
    if not available():
        raise ResetUnavailable()


def link(token: str) -> str:
    return email_tokens.fragment_link(RESET_PATH, token)


def _email(raw: Any) -> str | None:
    email = raw.strip().lower() if isinstance(raw, str) else ""
    try:
        EmailValidator()(email)
    except ValidationError:
        return None
    return email if len(email) <= EMAIL_MAX else None


def resettable(user) -> bool:
    """An account a reset link may be sent to (the module docstring's "Who gets a link")."""
    return (
        user is not None
        and user.is_active
        and user.has_usable_password()
        and Membership.objects.filter(user=user).exists()
        and not PlatformStaff.objects.filter(user=user).exists()
    )


def _message(token: str) -> tuple[str, str]:
    text = (
        "Someone, probably you, asked to reset the password of your Softwarden account.\n\n"
        f"Choose a new password here:\n\n{link(token)}\n\n"
        f"The link works once, for {RESET_MINUTES} minutes. Using it signs your account out everywhere.\n\n"
        "If you didn't ask for this, ignore this email: your password stays as it is.\n"
    )
    return "Reset your Softwarden password", text


SEND_JOB = "password_reset.send"


@transaction.atomic
def request_reset(data: dict[str, Any]) -> None:
    """Queue the reset for the worker: the account's id, or null for an address with no account. The same work and
    the same answer for every address (sending in the request made a known address slower)."""
    from core import jobs

    require_available()
    email = _email(data.get("email"))
    if email is None:
        raise FieldErrors({"email": ["Enter an email address."]})
    user_id = get_user_model().objects.filter(username=email).values_list("pk", flat=True).first()
    jobs.enqueue(SEND_JOB, {"user": user_id})


def send_link(job, ctx) -> dict[str, Any]:
    """The `password_reset.send` job: send a reset link when the account is resettable and under its limits; nothing
    otherwise. A message the server refuses keeps no link and no event."""
    user_id = (job.payload or {}).get("user")
    if not isinstance(user_id, int):
        return {"sent": False}
    return {"sent": _send(user_id)}


@transaction.atomic
def _send(user_id: int) -> bool:
    from notify import transactional

    now = timezone.now()
    PasswordReset.objects.filter(created_at__lt=now - KEEP).delete()
    # The account row, locked: two jobs for one account count its links one after the other.
    user = get_user_model().objects.select_for_update().filter(pk=user_id).first()
    if user is None or not resettable(user):
        return False
    recent = PasswordReset.objects.filter(user=user, created_at__gt=now - SEND_WINDOW)
    if recent.count() >= SEND_LIMIT or recent.filter(created_at__gt=now - COOLDOWN).exists():
        return False
    token, digest = email_tokens.new_token()
    with transaction.atomic():  # a refused message keeps no link and no event
        PasswordReset.objects.create(user=user, token_hash=digest, expires_at=now + timedelta(minutes=RESET_MINUTES))
        audit_account(user, None, "user.password_reset_requested", {"user": user.id})
        subject, text = _message(token)
        if not transactional.send("password_reset", user.email or user.username, subject, text):
            transaction.set_rollback(True)
            return False
    return True


def register() -> None:
    from core import jobs

    jobs.register_job(SEND_JOB, send_link, max_attempts=1)


@transaction.atomic
def confirm_reset(data: dict[str, Any]):
    """Set the account's password from a live link. Returns the user. 404 /problems/password-reset-link-unavailable
    for a link that can't be used; 400 for a password Django's validators refuse (the link stays usable)."""
    require_available()
    token = data.get("token")
    if not email_tokens.plausible(token):
        raise LinkUnavailable()
    assert isinstance(token, str)
    row = (
        PasswordReset.objects.select_for_update(of=("self",))
        .select_related("user")
        .filter(token_hash=email_tokens.hash_token(token))
        .first()
    )
    now = timezone.now()
    if row is None or row.used_at is not None or row.expires_at <= now or not resettable(row.user):
        raise LinkUnavailable()
    user = row.user
    password = data.get("password")
    if not isinstance(password, str) or not password:
        raise FieldErrors({"password": ["Choose a password."]})
    try:
        validate_password(password, user)
    except ValidationError as e:
        raise FieldErrors({"password": list(e.messages)}) from e
    user.set_password(password)
    user.save(update_fields=["password"])
    PasswordReset.objects.filter(user=user, used_at__isnull=True).update(used_at=now)
    Membership.objects.filter(user=user, must_change_password=True).update(must_change_password=False)
    audit_account(user, user, "user.password_reset_completed", {"user": user.id})
    from .admin_services import revoke_calendar_feeds  # admin_services imports this module
    from .invitations import record_proof  # invitations imports admin_services, which imports this module

    revoke_calendar_feeds(user, user, "password_reset")  # The feeds' secret URLs go with the old password
    record_proof(user, "password_reset")
    return user


def end_open_links(user) -> None:
    """Mark every open reset link of the account used: its password was just set some other way."""
    PasswordReset.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())
