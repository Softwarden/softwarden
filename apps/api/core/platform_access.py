"""Who may use the platform admin console (routes in catalog/platform_urls.py under /api/v1/platform/).

A route declared `platform` (core/drf.PLATFORM, `policy(PLATFORM)`) answers only:

- a signed-in session, never an API token (an API token belongs to one organization and acts as a member there);
- whose real person is platform staff (core.models.PlatformStaff, granted by `manage.py platform_staff`). While staff
  impersonate a member (core/impersonation.py) the request's user is the member, so the check reads the staff account
  the impersonation recorded;
- signed in with their password: a session confined by an organization's single sign-on is refused, since that
  organization's Admin controls its identity provider (core/oidc.match_user refuses staff anyway);
- whose password needn't change first (a temporary password is an Admin's or an operator's, not the person's own);
- with SOFTWARDEN_PLATFORM_TOTP on, whose session sent a current TOTP code (catalog/staff_second_factor.py),
  except on views that set `before_second_factor` (the session and the second factor's own routes);
- and, while impersonating, only views that set `allowed_while_impersonating` (the impersonation's own status and
  stop). Everything else, starting another impersonation included, is /problems/impersonation-restricted.

Being staff confers no role in any organization: tenant routes still need a membership (core/access.py)."""

from __future__ import annotations

from typing import Any

from rest_framework import exceptions

from .models import Membership, PlatformStaff
from .problems import ImpersonationRestricted, SecondFactorRequired

TOKEN_REFUSED = "Platform routes need a signed-in session; API tokens can't use them."  # noqa: S105  (a message)
NOT_STAFF = "Only platform staff can do this."
SSO_REFUSED = (
    "The platform console needs a password sign-in; this session signed in through an organization's single sign-on."
)
CHANGE_PASSWORD = "Change your password to continue."  # noqa: S105  (a message)


def is_platform_staff(user: Any) -> bool:
    """Whether `user` is platform staff now. Remembered on the user object for the rest of the request."""
    if user is None or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return False
    cached = getattr(user, "_softwarden_platform_staff", None)
    if cached is None:
        cached = PlatformStaff.objects.filter(user_id=user.pk).exists()
        user._softwarden_platform_staff = cached
    return bool(cached)


def django_request(request: Any) -> Any:
    """The Django HttpRequest under a DRF Request (or the request itself)."""
    return getattr(request, "_request", request)


def impersonator(request: Any) -> Any:
    """The staff account impersonating on this request, or None (core/impersonation.Middleware sets it)."""
    return getattr(django_request(request), "softwarden_impersonator", None)


def real_user(request: Any) -> Any:
    """The person actually signed in: the staff account while impersonating, else the request's user."""
    return impersonator(request) or request.user


def sso_confined(request: Any) -> bool:
    """Whether the real session signed in through an organization's SSO: its own value, or while impersonating the
    value the impersonation kept aside to put back."""
    from .access import SSO_ORGANIZATION
    from .impersonation import SESSION_KEY

    session = getattr(django_request(request), "session", None)
    if session is None:
        return False
    if session.get(SSO_ORGANIZATION):
        return True
    state = session.get(SESSION_KEY)
    return isinstance(state, dict) and bool((state.get("saved") or {}).get(SSO_ORGANIZATION))


def staff_session(request: Any) -> bool:
    """Whether this request would pass `check_platform` outside the platform routes: a signed-in session (no API token)
    whose real person is platform staff, signed in with their password, not impersonating. Never raises. Tenant routes
    that show installation-wide detail to staff only use it (core/system_views.py)."""
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated) or getattr(request, "auth", None) is not None:
        return False
    if impersonator(request) is not None or sso_confined(request):
        return False
    real = real_user(request)
    if not is_platform_staff(real):
        return False
    return not Membership.objects.filter(user_id=real.pk, must_change_password=True).exists()


def check_platform(request: Any, view: Any) -> bool:
    """RolePermission's check for a `platform` route (core/drf.py)."""
    if not (request.user and request.user.is_authenticated):
        return False  # DRF answers not signed in
    if request.auth is not None:
        raise exceptions.PermissionDenied(TOKEN_REFUSED)
    real = real_user(request)
    if not is_platform_staff(real):
        raise exceptions.PermissionDenied(NOT_STAFF)
    if sso_confined(request):
        raise exceptions.PermissionDenied(SSO_REFUSED)
    if Membership.objects.filter(user_id=real.pk, must_change_password=True).exists():
        raise exceptions.PermissionDenied(CHANGE_PASSWORD)
    if not getattr(view, "before_second_factor", False):
        from catalog import staff_second_factor  # catalog depends on core: not at import time

        if staff_second_factor.required() and not staff_second_factor.verified(request, real):
            raise SecondFactorRequired(staff_second_factor.enrolled(staff_second_factor.staff_row(real)))
    if impersonator(request) is not None and not getattr(view, "allowed_while_impersonating", False):
        raise ImpersonationRestricted("Stop impersonating to use the platform console.")
    return True
