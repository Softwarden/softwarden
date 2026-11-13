"""Request context: which organization a request acts in, with which role, and the role
checks every view starts with. Moved from catalog/views.py so that every app depends on core, not on
catalog, for them.

The organization never comes from the client's payload. It is, in order:

1. An API token's own organization. A token belongs to one organization; the session's choice never applies to it,
   and a token whose owner is no longer a member there is refused when it authenticates (api/tokens.py).
2. The session's current organization, when it names one of the user's memberships.
3. Otherwise the user's oldest membership (by `created_at`, then `id`), which is then stored in the session. With one
   membership that is the auto-select of the community edition; with several it is a deterministic default. A stale
   session value (the membership was removed) falls back the same way and never reaches the old organization.

A session signed in through an organization's single sign-on (core/oidc_views.callback) carries that
organization in SSO_ORGANIZATION and is confined to it: `resolve` lists and selects only that membership, so the
organization switch can't reach the account's other organizations. Reaching them needs their own sign-in; a password
sign-in clears the confinement (catalog/views.login_view). Org B's Admin can invite any existing account and point B's
SSO at an identity provider they control, so without this B's provider could reach the account's other organizations.
"""

from rest_framework import exceptions

from .models import Membership, Role
from .request_context import note_log

CURRENT_ORGANIZATION = "softwarden_current_organization"  # session key: the current organization's UUID as text
SSO_ORGANIZATION = "softwarden_sso_org"  # session key: the organization whose SSO signed this session in (UUID text)
# Session key: when this session's person last authenticated (ISO 8601, UTC), set by every sign-in path through Django's
# `user_logged_in` signal (password, OIDC, SAML, first-run setup, invitation acceptance, signup confirmation). Used for
# re-authentication by recency (catalog/org_lifecycle.reauthenticate); `last_login` is the account's, not the session's.
AUTHENTICATED_AT = "softwarden_authenticated_at"

WRITE_ROLES = {Role.ADMIN, Role.EDITOR}


def remember_authentication(sender, request=None, user=None, **kwargs):
    """`user_logged_in` receiver (core/apps.py): note in the session when its person authenticated."""
    from django.utils import timezone

    session = getattr(request, "session", None)
    if session is not None:
        session[AUTHENTICATED_AT] = timezone.now().isoformat()


def authenticated_at(request):
    """When this session's person last authenticated, or None (an API token, or a session from an earlier version)."""
    from datetime import datetime

    if pinned_organization_id(request) is not None:
        return None
    raw = getattr(request, "session", {}).get(AUTHENTICATED_AT)
    try:
        return datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def memberships_of(user):
    """The user's memberships with their organizations, oldest first. One query."""
    return list(Membership.objects.select_related("organization").filter(user=user).order_by("created_at", "id"))


def pinned_organization_id(request):
    """The organization an authenticator ties the request to, or None. An API token is such an auth object: it
    carries `organization_id`. Session requests have `request.auth = None`."""
    return getattr(request.auth, "organization_id", None)


def sso_confinement(request):
    """The organization an SSO-signed-in session is confined to (UUID text), or None. API tokens are never confined:
    a token has its own organization."""
    session = getattr(request, "session", None)
    return session.get(SSO_ORGANIZATION) if session is not None else None


def impersonation_confinement(request):
    """The organization an impersonation by platform staff is for (UUID text), or None
    (core/impersonation.py): the impersonated member sees and acts in that organization only, whatever else they
    belong to."""
    django_request = getattr(request, "_request", request)
    state = getattr(django_request, "softwarden_impersonation", None)
    return str(state.get("organization")) if isinstance(state, dict) and state.get("organization") else None


def resolve(request):
    """(current membership or None, the user's memberships this request may use). Stores the fallback choice in the
    session. An SSO session sees only the membership of the organization that signed it in; an impersonated member
    only the membership of the organization the impersonation is for."""
    rows = memberships_of(request.user)
    pinned = pinned_organization_id(request)
    if pinned is not None:
        return next((m for m in rows if m.organization_id == pinned), None), rows
    for confined in (impersonation_confinement(request), sso_confinement(request)):
        if confined:
            rows = [m for m in rows if str(m.organization_id) == confined]
    chosen = request.session.get(CURRENT_ORGANIZATION)
    current = next((m for m in rows if str(m.organization_id) == chosen), rows[0] if rows else None)
    if current is not None and chosen != str(current.organization_id):
        request.session[CURRENT_ORGANIZATION] = str(current.organization_id)
    return current, rows


def must_change_password(rows):
    """The flag is about the account: set on any membership, it holds in every organization."""
    return any(m.must_change_password for m in rows)


def current_membership(request):
    """The membership the request acts through, or 403 when there is none or the password must change first. Kept on
    the request once found (core.drf.RolePermission finds it first), so a view asking again costs no query."""
    cached = getattr(request, "softwarden_membership", None)
    if cached is not None:
        return cached
    current, rows = resolve(request)
    if current is None:
        raise exceptions.PermissionDenied("Your account isn't part of an organization.")
    confined = sso_confinement(request) or impersonation_confinement(request)
    if must_change_password(rows if not confined else memberships_of(request.user)):
        raise exceptions.PermissionDenied("Change your password to continue.")
    request.softwarden_membership = current
    note_log(org=current.organization_id, user=current.user_id)  # ids only, for the request's log lines
    return current


def org_role(request):
    """Organization and role come from the signed-in user's current membership, never from the client."""
    membership = current_membership(request)
    return membership.organization, membership.role


def require_write(role):
    if role not in WRITE_ROLES:
        raise exceptions.PermissionDenied("Editors and Admins can make changes; your role is Viewer.")


def require_admin(role):
    if role != Role.ADMIN:
        raise exceptions.PermissionDenied("Only Admins can do this.")
