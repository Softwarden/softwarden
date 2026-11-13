"""First-run setup API (core/bootstrap.py). Public: nobody can be signed in before the first
account exists. Writes need the CSRF token (GET /api/v1/auth/csrf, or this endpoint's GET, sets the cookie) and the
one-time token printed by `manage.py bootstrap_admin` on the host.

    GET  /api/v1/setup   {"pending": bool}
    POST /api/v1/setup   creates the first organization and Admin, signs them in, returns `me`

Once setup is closed POST answers 409, and so does a hosted (cloud) installation, which has no web setup: its operator
creates platform staff with `manage.py bootstrap_admin --create` (core/bootstrap.web_setup_open). Five bad tokens from
one client address within five minutes (counted per server process) block further attempts from it for the rest of
those five minutes (429).

Minimum role: none (public); tests/test_permissions.py lists it in PUBLIC."""

from django.conf import settings
from django.contrib.auth import login
from django.db import transaction
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import exceptions, serializers
from rest_framework.decorators import api_view, authentication_classes
from rest_framework.response import Response

from catalog import admin_services, services
from catalog.account_serializers import MeSerializer, me_body

from . import bootstrap
from .authentication import SessionAuthentication
from .drf import PROBLEMS, public, refuse_unknown_params
from .middleware import client_ip
from .problems import Conflict, FieldErrors

CLOSED = "Initial setup is already done. Sign in, or use `manage.py reset_admin` on the server to recover an Admin."
REFUSED = (
    "That setup token is wrong or has expired. Run `manage.py bootstrap_admin` on the server for the current token."
)
CLOUD = (
    "This hosted installation has no web setup. Sign up for an organization instead; the operator creates platform staff "
    "with `manage.py bootstrap_admin --create` on the server."
)
# The service validates `org_name`; the API calls it `organization_name`.
FIELD_NAMES = {"org_name": "organization_name"}


class SetupStateSerializer(serializers.Serializer):
    pending = serializers.BooleanField(help_text="True until the first organization and Admin exist.")


class SetupInputSerializer(serializers.Serializer):
    token = serializers.CharField(help_text="The one-time token printed by `manage.py bootstrap_admin` (one hour).")
    organization_name = serializers.CharField()
    timezone = serializers.CharField(help_text="An IANA timezone such as America/New_York.")
    email = serializers.EmailField(help_text="The Admin's email; they sign in with it.")
    name = serializers.CharField(help_text="The Admin's display name.")
    password = serializers.CharField(help_text="12 characters or more, not a common password. Never returned.")


class _CsrfSession(SessionAuthentication):
    """DRF checks CSRF only for signed-in sessions; setup has no session yet but must still need the token."""

    def check(self, request):
        self.enforce_csrf(request)


@extend_schema(
    methods=["GET"],
    summary="Whether first-run setup is open (public)",
    auth=[],
    responses={200: SetupStateSerializer},
)
@extend_schema(
    methods=["POST"],
    summary="Create the first organization and Admin with the one-time setup token (public)",
    description=(
        "Needs the CSRF token and the token printed by `manage.py bootstrap_admin`. Signs the new Admin in and "
        "returns `me`. 403 for a wrong or expired token, 409 once setup is done, 429 after five bad tokens from one "
        "address in five minutes (per server process)."
    ),
    auth=[],
    request=SetupInputSerializer,
    responses={
        200: MeSerializer,
        **{c: PROBLEMS[c] for c in (400, 403, 409)},
        429: OpenApiResponse(description="Too many bad tokens; Retry-After says when to try again"),
    },
)
@never_cache
@ensure_csrf_cookie
@api_view(["GET", "POST"])
@authentication_classes([])
@public
def setup(request):
    if request.method == "GET":
        refuse_unknown_params(request)
        return Response(SetupStateSerializer({"pending": bootstrap.web_setup_open()}).data)
    _CsrfSession().check(request)
    if settings.SOFTWARDEN_CLOUD:
        raise Conflict(CLOUD)
    if not bootstrap.pending():
        raise Conflict(CLOSED)
    ip = client_ip(request)
    wait = bootstrap.blocked_for(ip)
    if wait:
        raise exceptions.Throttled(wait=wait)
    data = request.data if isinstance(request.data, dict) else {}
    if not bootstrap.matches(data.get("token")):
        bootstrap.record_failure(ip)
        raise exceptions.PermissionDenied(REFUSED)
    fields = {
        "org_name": data.get("organization_name"),
        "timezone": data.get("timezone"),
        "email": data.get("email"),
        "name": data.get("name"),
        "password": data.get("password"),
    }
    if not all(isinstance(v, str) or v is None for v in fields.values()):
        raise FieldErrors({"non_field": ["Every field is text."]})
    try:
        with transaction.atomic():
            membership = admin_services.bootstrap_admin(fields, via="setup")
            services.audit(membership.organization, membership.user, "user.login", None, {"user": membership.user.id})
    except FieldErrors as e:
        raise FieldErrors({FIELD_NAMES.get(k, k): v for k, v in e.errors.items()}) from None
    bootstrap.discard_token()  # committed: the token has done its job
    login(request, membership.user, backend="django.contrib.auth.backends.ModelBackend")
    return Response(me_body(request))
