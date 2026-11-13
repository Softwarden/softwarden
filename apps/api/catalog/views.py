"""Sign-in, sign-out and the signed-in account (`auth/*`) and the health probe. Function views, since each is one
small step of the session flow rather than a model's rows; each declares its role and is described in OpenAPI by
the serializers in catalog/account_serializers.py. Every other catalog route is a viewset or a RoleAPIView
(catalog/viewsets.py, directory_viewsets.py, curation_viewsets.py, account_viewsets.py, audit_viewsets.py)."""

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.sessions.exceptions import SessionInterrupted
from django.db import connection, transaction
from django.middleware.csrf import rotate_token
from django.views.decorators.csrf import ensure_csrf_cookie
from drf_spectacular.utils import OpenApiResponse, PolymorphicProxySerializer, extend_schema, inline_serializer
from rest_framework import exceptions, serializers
from rest_framework.decorators import api_view, throttle_classes
from rest_framework.response import Response

from core import access, sessions, signin_limits
from core.drf import ACCOUNT, PROBLEMS, VIEWER, ProblemSerializer, min_role, public, refuse_unknown_params
from core.models import Membership, PlatformStaff
from core.platform_access import is_platform_staff
from core.problems import FieldErrors, InvalidCredentials
from core.throttling import ScopedThrottle

from . import admin_services, platform_admin, services
from .account_serializers import LoginInputSerializer, MeSerializer, SwitchOrganizationInputSerializer, me_body
from .platform_views import PlatformSessionSerializer, platform_session_body

HealthSerializer = inline_serializer("Health", {"status": serializers.ChoiceField(["ok"])})
THROTTLED = OpenApiResponse(
    description="Too many sign-in attempts from this address, or too many failed ones for this account from any "
    "address; Retry-After says when to try again"
)


@extend_schema(
    summary="The API process can reach the database (public)",
    auth=[],
    responses={200: HealthSerializer},
)
@api_view(["GET"])
@public
def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return Response({"status": "ok"})


@extend_schema(
    summary="Set the CSRF cookie (public)",
    description="Writes from a session send the cookie's value back as X-CSRFToken.",
    auth=[],
    responses={204: None},
)
@api_view(["GET"])
@ensure_csrf_cookie
@public
def csrf(request):
    refuse_unknown_params(request)
    return Response(status=204)


@extend_schema(
    summary="Sign in with email and password (public)",
    description="Starts a session and returns `me`. Fetch auth/csrf again afterwards: the CSRF token rotates. A "
    "platform staff account with no membership is signed in for the platform console only and gets "
    "`platform/session`'s body instead (`platform_staff` true, no `organization`).",
    auth=[],
    request=LoginInputSerializer,
    responses={
        200: PolymorphicProxySerializer(
            component_name="LoginResult",
            serializers=[MeSerializer, PlatformSessionSerializer],
            resource_type_field_name=None,
            many=False,
        ),
        403: OpenApiResponse(
            ProblemSerializer,
            description="The email and password don't match an active account (type /problems/invalid-credentials), "
            "or the account belongs to no organization",
        ),
        429: THROTTLED,
    },
)
@api_view(["POST"])
@throttle_classes([ScopedThrottle])
@public
def login_view(request):
    raw_email, raw_password = request.data.get("email"), request.data.get("password")
    email = raw_email.strip().lower() if isinstance(raw_email, str) else ""  # another type matches no account
    # Every attempt is counted per account, from any address, before the password is checked; past five
    # each must wait longer (core/signin_limits.py). A success forgets the count.
    signin_limits.attempt_account(email)
    user = authenticate(request, username=email, password=raw_password if isinstance(raw_password, str) else "")
    if user is None:
        # Audited only when the email belongs to an account, in each of its organizations; the email itself isn't
        # stored. Unknown emails leave no trace beyond the throttle.
        known = get_user_model().objects.filter(username=email).first()
        if known is not None:
            services.audit_account(known, None, "user.login_failed", {"user": known.id})
            if PlatformStaff.objects.filter(user=known).exists():  # also in the platform stream
                # No actor: nobody proved who they are; the account tried is in the details.
                platform_admin.platform_audit(None, "staff.sign_in_failed", details={"user": known.id})
        raise InvalidCredentials()
    signin_limits.forget_account(email)
    staff = is_platform_staff(user)
    if not Membership.objects.filter(user=user).exists():
        if not staff:
            raise exceptions.PermissionDenied("Your account isn't part of an organization.")
        # Platform staff with no membership: signed in for the console only (catalog/platform_views.py).
        login(request, user)
        request.session.pop(access.SSO_ORGANIZATION, None)
        platform_admin.platform_audit(user, "staff.signed_in", details={"user": user.id})
        return Response(platform_session_body(request))
    login(request, user)
    if staff:
        platform_admin.platform_audit(user, "staff.signed_in", details={"user": user.id})
    request.session.pop(access.SSO_ORGANIZATION, None)  # a password sign-in reaches every membership
    current, _ = access.resolve(request)
    services.audit(current.organization, user, "user.login", None, {"user": user.id})
    return Response(me_body(request))


login_view.cls.throttle_scope = "login"  # type: ignore[attr-defined]


@extend_schema(summary="Sign out", request=None, responses={204: None, 403: PROBLEMS[403]})
@api_view(["POST"])
@min_role(ACCOUNT, ACCOUNT)
def logout_view(request):
    from core.platform_access import impersonator

    staff = impersonator(request)
    session = request._request.session
    if staff is not None:  # the impersonation's end is audited before the session goes
        try:
            platform_admin.end_impersonation(request._request, staff, "signed_out")
        except SessionInterrupted:
            pass  # another request (a Stop in another tab) changed the key first: signing out goes ahead regardless
        request.user = staff  # the sign-out (and user_logged_out) is the staff account's, not the member's
    logout(request)
    response = Response(status=204)
    # Another request changed the session's key after this one read it live: in end_impersonation above, in
    # core/impersonation.Middleware noticing an expiry, or in logout's flush (an organization switch in another tab).
    # The browser's current key is that change's successor. Only `interrupted`, never a cookie that merely loaded as a
    # tombstone (anyone holding an old key could otherwise end the session it leads to), and only for a session
    # request: an API token's sign-out never reaches a browser's sessions through a cookie sent beside it.
    if getattr(session, "interrupted", False) and request.auth is None:
        # Signing out always signs out: end the session the key change created, and delete the cookie, which
        # core/sessions.SessionMiddleware leaves alone for a rotated-away session otherwise.
        sessions.end_successors(request._request.COOKIES.get(settings.SESSION_COOKIE_NAME))
        sessions.delete_cookie(response)
    return response


@extend_schema(summary="You and your current organization", responses={200: MeSerializer, 403: PROBLEMS[403]})
@api_view(["GET"])
@min_role(ACCOUNT)
def me(request):
    refuse_unknown_params(request)
    return Response(me_body(request))


@extend_schema(
    summary="Switch your current organization (any member)",
    description=(
        "Sessions only; an API token belongs to one organization. Audited in both organizations. Another "
        "organization's ID is 404. Rotates the session key and the CSRF token."
    ),
    request=SwitchOrganizationInputSerializer,
    responses={200: MeSerializer, **{c: PROBLEMS[c] for c in (400, 403, 404, 409)}},
)
@api_view(["POST"])
@min_role(VIEWER, VIEWER)
def me_organization(request):
    """Switch the session's current organization (Viewer and up: any member). Another organization's ID, or one that
    isn't an ID, is 404 as if it didn't exist. API tokens belong to one organization and can't switch."""
    if access.pinned_organization_id(request) is not None:
        raise exceptions.PermissionDenied("An API token belongs to one organization; it can't switch.")
    current = access.current_membership(request)
    raw = request.data.get("organization_id") if isinstance(request.data, dict) else None
    if not isinstance(raw, str) or not raw:
        raise FieldErrors({"organization_id": ["Choose an organization."]})
    # resolve's rows: an SSO-signed-in session reaches only the organization that signed it in (core/access.py).
    target = next((m for m in access.resolve(request)[1] if str(m.organization_id) == raw), None)
    if target is None:
        raise exceptions.NotFound("No organization with this ID.")
    if target.organization_id != current.organization_id:
        # One transaction: when another request (a sign-out in another tab) rotated the session after this one read
        # it, cycle_key raises SessionInterrupted (400, core/sessions.py) and the switch isn't audited.
        with transaction.atomic():
            admin_services.switch_organization(request.user, current, target)
            request.session[access.CURRENT_ORGANIZATION] = str(target.organization_id)
            # A new session key and CSRF token for the new organization, as at sign-in.
            request.session.cycle_key()
        rotate_token(request._request)
    return Response(me_body(request))


# A member of a suspended organization can still switch to another of their organizations.
me_organization.cls.allowed_when_suspended = True  # type: ignore[attr-defined]
