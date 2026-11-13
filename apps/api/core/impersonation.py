"""Impersonation by platform staff (platform admin console; services in catalog/platform_admin.py).

Staff start an impersonation of one member of one organization (POST /api/v1/platform/impersonation: their password,
or a single sign-on sign-in in this session within 15 minutes; a written reason; a time limit of
SOFTWARDEN_IMPERSONATION_MINUTES, default 30). It works in the same browser session:

- The session stays signed in as the staff account (Django's auth keys are untouched), so the staff account's own
  password change or removal still ends everything. SESSION_KEY records who is impersonated, by whom, why, until when,
  and a copy of every other session value as it was (`saved`).
- On each request, Middleware swaps `request.user` for the member, so every view, DRF's session authentication and
  core/access see the member, in the organization the impersonation is for. `request.softwarden_impersonator` keeps
  the staff account and every audit event written meanwhile records it as `impersonated_by`.
- Stopping (DELETE /api/v1/platform/impersonation), expiry (noticed on the next request), the staff account losing
  platform staff, or the member becoming unavailable (deactivated, removed from the organization, made staff) ends
  it: the session values are put back exactly as `saved`, the session key and the CSRF token are rotated, and the end
  is audited in both streams. Signing out flushes the whole session, as always, after auditing the end.

While impersonating, the impersonated member sees and acts in the impersonation's organization only (core/access.resolve
confines their memberships to it, as an SSO sign-in does). RESTRICTED routes, and any write not in ALLOWED_WRITES
(core/impersonation_routes.py: closed by default), are refused before the view runs with
/problems/impersonation-restricted; platform routes other than the impersonation's own status and stop are refused by
core/platform_access.py. A write on the request that notices the impersonation has ended is refused with 409
/problems/impersonation-ended instead of running as the staff account. When a read notices it instead (the SPA's
`auth/me` poll, another tab), the restored session is marked (ENDED_NOTICE) and the next tenant write, however much
later, is refused the same way, once, then the mark clears: a form left open in the member's organization can't save
into the staff account's own organization by surprise. Signing out is never refused (and flushes the mark with the
session); platform console writes are neither refused nor clear it.

Each impersonation has an id (a UUID, in the session's record; `POST platform/impersonation` and `auth/me` return it). A
write may name the impersonation its tab believes in with the header X-Softwarden-Impersonation (HEADER): the id, or
`none` when the tab believes it isn't impersonating. Tabs share the session and the CSRF cookie, so this is what tells a
tab left open from an earlier impersonation, or from before Stop, from the current one. On a write (not a read, not
signing out, not a platform console route, which keep their own rules: Stop works with any header):

- an id that isn't the session's current impersonation (another one, or none at all, or not an id) answers 409
  /problems/impersonation-ended without running. It leaves the ENDED_NOTICE mark alone: the tab refused here sent the
  header, while the mark protects a tab that doesn't (an older SPA bundle), which may still be open beside it;
- `none` while the session is impersonating answers 409 /problems/impersonation-started without running;
- `none` while it isn't, or the matching id, passes. A tab that sends the header has said which impersonation it is
  in, so the mark (kept for clients that don't send it) neither refuses nor is cleared by it;
- no header (API tokens, scripts, older clients): exactly as before, the mark included.

A write on the request that notices the end is refused as before, whatever the header says. An impersonation
record without an id (none is written without one; a hand-edited or corrupted session) matches no header: both `none`
and every id are refused while it lasts.

Accepted: these refusals happen in this middleware, before DRF's CSRF check runs in the view, so a cross-site write
with a forged header gets a 409 instead of a 403. It still does nothing, and a 409 tells the sender nothing it couldn't
learn by sending the header itself."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY
from django.contrib.auth import SESSION_KEY as AUTH_SESSION_KEY
from django.contrib.sessions.exceptions import SessionInterrupted
from django.middleware.csrf import rotate_token
from django.utils import timezone

from .impersonation_routes import ALLOWED_WRITES, E2E_ALLOWED, RESTRICTED_ROUTES
from .problems import IMPERSONATION_ENDED, IMPERSONATION_RESTRICTED, IMPERSONATION_STARTED, problem_response
from .request_context import impersonating

SESSION_KEY = "softwarden_impersonation"
AUTH_KEYS = frozenset({AUTH_SESSION_KEY, BACKEND_SESSION_KEY, HASH_SESSION_KEY})

RESTRICTED_DETAIL = (
    "This isn't allowed while impersonating someone: credentials, identities, single sign-on, tokens, secrets and the "
    "organization's deletion stay with the member and their Admins. Stop impersonating first."
)


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ENDED_DETAIL = (
    "Your impersonation ended (its time limit, or access changed) before this request, so nothing was done. You are "
    "signed in as yourself again; reload."
)
# A write's claim about the impersonation its tab is in: the impersonation's id, or HEADER_NONE (none).
HEADER = "HTTP_X_SOFTWARDEN_IMPERSONATION"
HEADER_NONE = "none"
# Stands in for the id of an impersonation record that has none, so no header value matches it.
MISSING_ID = "missing"
STALE_DETAIL = (
    "This tab was opened for an impersonation that has ended (or before another one started), so nothing was done. "
    "Reload to see who you are signed in as."
)
STARTED_DETAIL = (
    "This tab doesn't know about the impersonation that started in this browser since it was opened, so nothing was "
    "done. Reload."
)
# Still answered when an impersonation has just ended: signing out never needs a reload first.
ENDED_EXEMPT = frozenset({"/api/v1/auth/logout"})
# Session key: when a read (GET, HEAD, OPTIONS) noticed that the impersonation ended (ISO time, for the record). The next
# tenant write answers 409 /problems/impersonation-ended without running, and the key is removed. No time limit: a form
# saved an hour later is the case it is for. Signing out flushes it with the session.
ENDED_NOTICE = "softwarden_impersonation_ended"
PLATFORM_PREFIX = "/api/v1/platform/"


def is_restricted(method: str, route: str) -> bool:
    """Whether an impersonated request may not use this route (core/impersonation_routes.py: closed by default)."""
    method = method.upper()
    if method == "HEAD":
        method = "GET"  # a HEAD runs the GET view; it must not get past a refused GET
    if not route.startswith("api/v1/") or route.startswith("api/v1/platform/"):
        return False
    if (method, route) in RESTRICTED_ROUTES:
        return True
    if (method, route) in E2E_ALLOWED:
        return False
    if method in SAFE_METHODS:
        return False
    return (method, route) not in ALLOWED_WRITES


def claimed(request: Any) -> str:
    """The write's X-Softwarden-Impersonation value, stripped; empty when absent."""
    return str(request.META.get(HEADER) or "").strip()


def same_id(value: str, current: Any) -> bool:
    """Whether a header value names the impersonation `current` (dashed or hex UUID forms alike)."""
    try:
        return uuid.UUID(value) == uuid.UUID(str(current))
    except ValueError:
        return False


def state_of(request: Any) -> dict[str, Any] | None:
    """The session's impersonation record, or None."""
    session = getattr(request, "session", None)
    if session is None:
        return None
    value = session.get(SESSION_KEY)
    return value if isinstance(value, dict) else None


def expires_at(state: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(state["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None


def begin(request: Any, state: dict[str, Any], organization_id: str) -> None:
    """Put the session into impersonation: keep every non-auth value aside, start the member's values afresh in the
    organization the impersonation is for, rotate the session key and the CSRF token."""
    session = request.session
    saved = {k: v for k, v in session.items() if k not in AUTH_KEYS and k not in (SESSION_KEY, ENDED_NOTICE)}
    for key in list(saved):
        del session[key]
    # An earlier impersonation's end mark goes: a new one is begun on purpose, and its own end marks afresh.
    session.pop(ENDED_NOTICE, None)
    from .access import CURRENT_ORGANIZATION

    session[CURRENT_ORGANIZATION] = organization_id
    session[SESSION_KEY] = {**state, "saved": saved}
    session.cycle_key()
    rotate_token(request)


def finish(request: Any) -> dict[str, Any] | None:
    """End the impersonation in the session: put back exactly the values `begin` kept aside. Returns the record."""
    session = request.session
    state = session.get(SESSION_KEY)
    if not isinstance(state, dict):
        return None
    for key in [k for k in session.keys() if k not in AUTH_KEYS]:
        del session[key]
    session.update(state.get("saved") or {})
    session.cycle_key()
    rotate_token(request)
    for attr in ("softwarden_impersonator", "softwarden_impersonation", "softwarden_membership"):
        request.__dict__.pop(attr, None)
    return state


class Middleware:
    """After AuthenticationMiddleware (settings.MIDDLEWARE): applies, expires or ends the session's impersonation, and
    refuses RESTRICTED routes while it lasts."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        state = state_of(request)
        if state is None:
            refused = self.header_refusal(request, None)
            if refused is None and not claimed(request):
                refused = self.after_notice(request)
            return refused if refused is not None else self.get_response(request)
        try:
            staff, member = self.resolve(request, state)
        except SessionInterrupted:
            # Another request (another tab) ended or changed this session's impersonation first, and this one's end
            # was rolled back, unaudited. Signing out goes ahead (catalog/views.logout_view deletes the cookie);
            # anything else is told the impersonation ended, so the SPA reloads.
            if request.path_info in ENDED_EXEMPT:
                return self.get_response(request)
            return problem_response(409, ENDED_DETAIL, IMPERSONATION_ENDED)
        if member is None:
            # It ended on this request. A write meant for the member mustn't run as the staff account instead.
            if request.method not in SAFE_METHODS and request.path_info not in ENDED_EXEMPT:
                return problem_response(409, ENDED_DETAIL, IMPERSONATION_ENDED)
            # A read noticed it: the next write is refused once (after_notice), as this one would have been.
            if request.method in SAFE_METHODS:
                request.session[ENDED_NOTICE] = timezone.now().isoformat()
            return self.get_response(request)
        refused = self.header_refusal(request, state.get("id") or MISSING_ID)
        if refused is not None:
            return refused
        request.user = member
        request.softwarden_impersonator = staff
        request.softwarden_impersonation = state
        with impersonating(staff.pk):
            return self.get_response(request)

    @staticmethod
    def header_refusal(request, current: Any):
        """The 409 for a write whose X-Softwarden-Impersonation doesn't match the session (see the module docstring),
        else None. `current`: the session's live impersonation id, or None when it isn't impersonating."""
        value = claimed(request)
        if (
            not value
            or request.method in SAFE_METHODS
            or request.path_info in ENDED_EXEMPT
            or request.path_info.startswith(PLATFORM_PREFIX)
        ):
            return None
        if value.lower() == HEADER_NONE:
            return None if current is None else problem_response(409, STARTED_DETAIL, IMPERSONATION_STARTED)
        if current is not None and same_id(value, current):
            return None
        return problem_response(409, STALE_DETAIL, IMPERSONATION_ENDED)

    @staticmethod
    def after_notice(request):
        """The 409 for the first tenant write after a read ended the impersonation (ENDED_NOTICE), else None. Reads,
        signing out and platform console writes (api/v1/platform/, the banner's Stop included) leave the mark in place,
        unrefused: none of them can save into an organization's records, and a stale member tab may still be open. The
        first other write removes the mark and is refused."""
        session = getattr(request, "session", None)
        if session is None or session.get(ENDED_NOTICE) is None:
            return None
        if request.method in SAFE_METHODS or request.path_info in ENDED_EXEMPT:
            return None
        if request.path_info.startswith(PLATFORM_PREFIX):
            return None
        del session[ENDED_NOTICE]
        return problem_response(409, ENDED_DETAIL, IMPERSONATION_ENDED)

    @staticmethod
    def resolve(request, state):
        """(staff, member) when the impersonation holds; otherwise end it (audited) and return (staff, None)."""
        from django.contrib.auth import get_user_model

        from catalog import platform_admin  # catalog depends on core; imported here, as core/capabilities does

        from .models import Membership, PlatformStaff
        from .platform_access import is_platform_staff

        staff = request.user
        if not is_platform_staff(staff) or staff.pk != state.get("by"):
            platform_admin.end_impersonation(request, staff if staff.is_authenticated else None, "staff_unavailable")
            return staff, None
        ends = expires_at(state)
        if ends is None or ends <= timezone.now():
            platform_admin.end_impersonation(request, staff, "expired")
            return staff, None
        member = get_user_model().objects.filter(pk=state.get("user"), is_active=True).first()
        if (
            member is None
            or PlatformStaff.objects.filter(user_id=member.pk).exists()
            or not Membership.objects.filter(user=member, organization_id=state.get("organization")).exists()
        ):
            platform_admin.end_impersonation(request, staff, "member_unavailable")
            return staff, None
        return staff, member

    def process_view(self, request, view_func, view_args, view_kwargs):
        if getattr(request, "softwarden_impersonator", None) is None:
            return None
        match = getattr(request, "resolver_match", None)
        if match is not None and is_restricted(request.method, match.route):
            return problem_response(403, RESTRICTED_DETAIL, IMPERSONATION_RESTRICTED)
        return None
