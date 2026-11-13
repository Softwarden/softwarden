"""A second factor for platform staff: TOTP (core/totp.py), required to use the platform
console when SOFTWARDEN_PLATFORM_TOTP is on (the default in cloud mode).

- A staff account signs in with its password as before (catalog/views.login_view). Its session then reaches only the
  routes that set `before_second_factor` (platform/session, platform/second-factor and its enrollment and verify):
  every other platform route answers 403 /problems/second-factor-required (core/platform_access.check_platform), with
  `enrolled` saying which of the two steps comes next.
- Enrollment, at the first console sign-in: POST platform/second-factor/enrollment returns a new secret once (base32 and
  an otpauth:// URI for a QR code) and keeps it Fernet-encrypted on the PlatformStaff row (credentials slot `totp`).
  Asking again before confirming replaces it. POST platform/second-factor/verify with a current code confirms it and
  verifies the session. Once confirmed, enrollment is refused (409): only the operator can reset it.
- Every later sign-in: POST platform/second-factor/verify with a current code verifies the session. The session
  remembers which enrollment it verified (SESSION_KEY), so resetting the enrollment unverifies every session at once.
- POST platform/impersonation needs a current code as well (`code`), checked before anything else happens.
- A code is accepted once (the step is recorded), within one step of now either side. Every code is counted per staff
  account in the shared counters before it is checked (core/counters.attempt, under a row lock, so parallel requests
  are decided one after the other) and an accepted one forgets the count: after CODE_FAILURES in a row, 429 until
  CODE_WINDOW after the last one counted, from any process. The count isn't tied to clock-aligned windows.
- A staff account can also be enrolled from the host (`manage.py platform_staff enroll <email>`), which prints the
  secret once; the first accepted code confirms it as usual.
- Recovery, when a staff member loses the device: the operator runs `manage.py platform_staff reset-second-factor
  <email> --reason "..."`, which clears the enrollment (audited in the platform stream, via cli); the staff member
  enrolls again at their next console sign-in. docs/04-development/platform-admin-runbook.md "Second factor".

Every write is audited in the platform stream: staff.second_factor_enrollment_started, staff.second_factor_enrolled,
staff.second_factor_verified, staff.second_factor_failed and staff.second_factor_reset. The secret and the codes are
never in an event, a log or any response but the enrollment's own."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import exceptions

from core import counters, credentials, totp
from core.models import PlatformStaff
from core.problems import Conflict, FieldErrors

from .platform_admin import platform_audit

SESSION_KEY = "softwarden_second_factor"
SLOT = "totp"
CODE_SCOPE = "second-factor"
CODE_FAILURES = 5  # wrong codes in a row per staff account, then 429 ...
CODE_WINDOW = 300  # ... until five minutes after the last one counted (not a clock-aligned window)
WRONG_CODE = "That code isn't right. Enter the current 6-digit code from your authenticator app."
USED_CODE = "That code was already used. Wait for the next one."


def required() -> bool:
    return bool(settings.SOFTWARDEN_PLATFORM_TOTP)


def staff_row(user, lock: bool = False) -> PlatformStaff | None:
    pk = getattr(user, "pk", None)
    if pk is None:
        return None
    rows = PlatformStaff.objects.filter(user_id=pk)
    return (rows.select_for_update() if lock else rows).first()


def enrolled(row: PlatformStaff | None) -> bool:
    return row is not None and row.totp_confirmed_at is not None and credentials.slot_configured(row, SLOT)


def _marker(row: PlatformStaff) -> dict[str, Any]:
    confirmed = row.totp_confirmed_at.isoformat() if row.totp_confirmed_at else ""
    return {"user": row.user_id, "enrollment": confirmed}


def _session_value(request) -> Any:
    """The session's marker: its own, or while impersonating the one the impersonation kept aside."""
    from core.impersonation import SESSION_KEY as IMPERSONATION

    session = getattr(getattr(request, "_request", request), "session", None)
    if session is None:
        return None
    if session.get(SESSION_KEY) is not None:
        return session.get(SESSION_KEY)
    state = session.get(IMPERSONATION)
    return (state.get("saved") or {}).get(SESSION_KEY) if isinstance(state, dict) else None


def verified(request, user) -> bool:
    """Whether this session verified the staff account's current enrollment."""
    row = staff_row(user)
    return enrolled(row) and row is not None and _session_value(request) == _marker(row)


def state(request, user) -> dict[str, bool]:
    row = staff_row(user)
    return {"required": required(), "enrolled": enrolled(row), "verified": verified(request, user)}


# ---- enrollment ----------------------------------------------------------------------------------------------------


@transaction.atomic
def start_enrollment(user, via: str = "console") -> dict[str, str]:
    """A new secret for an account not enrolled yet: stored encrypted, returned once. 409 once enrolled. `via` "cli"
    (manage.py platform_staff enroll) audits with no actor, as the other commands do."""
    row = staff_row(user, lock=True)
    if row is None:
        raise exceptions.PermissionDenied("Only platform staff can do this.")
    if enrolled(row):
        raise Conflict(
            "Your second factor is already set up. If you lost the device, ask the operator to reset it "
            "(manage.py platform_staff reset-second-factor)."
        )
    secret = totp.new_secret()
    credentials.slot_set(row, SLOT, secret)
    row.totp_confirmed_at = None
    row.totp_last_step = None
    row.save(update_fields=["totp_ciphertext", "totp_key_id", "totp_set_at", "totp_confirmed_at", "totp_last_step"])
    platform_audit(
        None if via == "cli" else user,
        "staff.second_factor_enrollment_started",
        details={"user": user.pk, "via": via},
    )
    return {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret, user.email or user.get_username())}


# ---- codes --------------------------------------------------------------------------------------------------------


def _code_wait(count: int) -> int:
    """Seconds after the last counted code before another may be tried: none until CODE_FAILURES, then the rest of
    CODE_WINDOW (the count lapses CODE_WINDOW after the last one counted)."""
    return CODE_WINDOW if count >= CODE_FAILURES else 0


def _count_attempt(user) -> None:
    """Count this code before it is checked (a check made before counting let parallel requests all
    through), or refuse it with 429 while the account has had CODE_FAILURES in a row."""
    result = counters.attempt(CODE_SCOPE, str(user.pk), CODE_WINDOW, _code_wait)
    if not result.allowed:
        raise exceptions.Throttled(wait=result.retry_after, detail="Too many wrong codes. Wait before you try again.")


def _failed(user, purpose: str, message: str, field: str = "code") -> FieldErrors:
    """Audit a wrong code (it was counted before the check, outside any transaction); the error to raise."""
    platform_audit(user, "staff.second_factor_failed", details={"user": user.pk, "purpose": purpose})
    return FieldErrors({field: [message]})


def check_code(user, code: Any, purpose: str) -> PlatformStaff:
    """Accept a current code once: the step is recorded under the row's lock, so the same code can't pass twice, even
    on two processes at once. Every code is counted before it is checked and an accepted one forgets the count.
    Raises 400 (wrong, used, not enrolled), 409 (the stored secret can't be read), 429 (too many wrong codes)."""
    row = staff_row(user)
    if row is None or not credentials.slot_configured(row, SLOT):
        raise FieldErrors({"code": ["Set up your second factor first (platform/second-factor/enrollment)."]})
    _count_attempt(user)
    try:
        secret = credentials.slot_value(row, SLOT)
    except credentials.CredentialError:
        counters.release(CODE_SCOPE, str(user.pk))
        raise Conflict(
            "Your second factor can't be read (the installation's credential keys changed). Ask the operator to "
            "reset it: manage.py platform_staff reset-second-factor."
        ) from None
    step = totp.matching_step(secret, code)
    if step is None:
        raise _failed(user, purpose, WRONG_CODE)
    with transaction.atomic():
        row = staff_row(user, lock=True)
        assert row is not None  # noqa: S101  (it was there a moment ago; a revoke deletes it under this lock)
        replay = row.totp_last_step is not None and step <= row.totp_last_step
        if not replay:
            first = row.totp_confirmed_at is None
            row.totp_last_step = step
            fields = ["totp_last_step"]
            if first:
                row.totp_confirmed_at = timezone.now()
                fields.append("totp_confirmed_at")
            row.save(update_fields=fields)
            if first:
                platform_audit(user, "staff.second_factor_enrolled", details={"user": user.pk, "purpose": purpose})
            else:
                platform_audit(user, "staff.second_factor_verified", details={"user": user.pk, "purpose": purpose})
    if replay:
        raise _failed(user, purpose, USED_CODE)
    counters.forget(CODE_SCOPE, str(user.pk))
    return row


def verify_session(request, user, code: Any) -> dict[str, bool]:
    """POST platform/second-factor/verify: confirm the enrollment if it is new, and mark this session verified."""
    row = check_code(user, code, "sign_in")
    session = getattr(request, "_request", request).session
    session[SESSION_KEY] = _marker(row)
    return state(request, user)


# ---- recovery (the operator, manage.py platform_staff reset-second-factor) -----------------------------------------


@transaction.atomic
def reset(user, reason: str) -> bool:
    """Clear the staff account's enrollment: every session it verified needs the second factor again, and the staff
    member enrolls afresh at their next console sign-in. Audited in the platform stream with the operator's reason.
    Returns whether there was anything to clear."""
    row = staff_row(user, lock=True)
    if row is None:
        raise FieldErrors({"email": ["This account isn't platform staff."]})
    reason = (reason or "").strip()
    if not reason:
        raise FieldErrors({"reason": ["Say why (for example: lost phone, identity checked by a call)."]})
    had = credentials.slot_configured(row, SLOT)
    row.totp_ciphertext, row.totp_key_id, row.totp_set_at = "", "", None
    row.totp_confirmed_at = None
    row.totp_last_step = None
    row.save(update_fields=["totp_ciphertext", "totp_key_id", "totp_set_at", "totp_confirmed_at", "totp_last_step"])
    platform_audit(
        None,
        "staff.second_factor_reset",
        details={"user": user.pk, "email": user.email, "via": "cli", "reason": reason[:500], "had_factor": had},
    )
    return had
