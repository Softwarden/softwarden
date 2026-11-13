"""Public signup routes (catalog/signup.py). Public, CSRF-checked even with nobody signed in (the setup
endpoint's pattern: confirming signs a browser in, so a forged request could sign a victim into an attacker's account),
and throttled per client address: signing up and resending share the `signup` rate (SOFTWARDEN_SIGNUP_THROTTLE_RATE),
confirming uses the sign-in rate.

    POST auth/signup          {email, name, organization_name, password, timezone?}  202, the same for any email
    POST auth/signup/resend   {email}                                                 202, the same for any email
    POST auth/signup/verify   {token, password}                                       200 `me`, signed in

Signup off: 404 /problems/signup-disabled. On, but the installation can't send email: 503 /problems/signup-unavailable.

Minimum role: none (public); tests/test_permissions.py lists them in PUBLIC."""

from django.contrib.auth import login
from django.views.decorators.cache import never_cache
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, throttle_classes
from rest_framework.response import Response

from core import access
from core.drf import PROBLEMS, ProblemSerializer, public
from core.setup_views import _CsrfSession
from core.throttling import ScopedThrottle

from . import signup
from .account_serializers import MeSerializer, me_body

ACCEPTED = (
    "Check your email. If this address can sign up, we've sent a link to confirm it; it works for 24 hours. If it "
    "already has an account, we've sent a note saying so instead."
)
RESENT = "If this address has a signup waiting for confirmation, we've sent a new link. Earlier links stop working."


class SignupInputSerializer(serializers.Serializer):
    email = serializers.EmailField(help_text="Your work email: you sign in with it once it is confirmed.")
    name = serializers.CharField(help_text="Your display name.")
    organization_name = serializers.CharField(help_text="The organization to create; you become its Admin.")
    password = serializers.CharField(help_text="12 characters or more, not a common password. Never returned.")
    timezone = serializers.CharField(
        required=False, help_text="The organization's IANA timezone, such as Europe/London. Default UTC."
    )


class SignupResendInputSerializer(serializers.Serializer):
    email = serializers.EmailField()


class SignupVerifyInputSerializer(serializers.Serializer):
    token = serializers.CharField(help_text="The token from the confirmation link's fragment (/signup/verify#<token>).")
    password = serializers.CharField(help_text="The password chosen at signup, asked again to confirm it is you.")


class SignupAcceptedSerializer(serializers.Serializer):
    detail = serializers.CharField(help_text="What to do next. The same whether or not the email has an account.")


THROTTLED = OpenApiResponse(description="Too many attempts from this address; Retry-After says when to try again")
OFF = OpenApiResponse(ProblemSerializer, description="Signup is off on this installation (/problems/signup-disabled)")
NO_EMAIL = OpenApiResponse(
    ProblemSerializer,
    description="This installation can't send email (/problems/signup-unavailable), or the email couldn't be sent "
    "just now (/problems/email-failed), or the installation's daily ceiling on new organizations is reached "
    "(/problems/signup-paused; SOFTWARDEN_SIGNUP_DAILY_LIMIT)",
)
CSRF_NOTE = " Needs the CSRF token (GET auth/csrf sets the cookie; send it as X-CSRFToken)."


def _body(request) -> dict:
    _CsrfSession().check(request)
    return request.data if isinstance(request.data, dict) else {}


@extend_schema(
    summary="Sign up: create an account and a new organization once the email is confirmed (public)",
    description="Emails a confirmation link (24 hours). Nothing is created until it is confirmed. The answer is the "
    "same whether or not the email has an account: an existing account is emailed a note instead of a link. "
    "Signing up again replaces the earlier link." + CSRF_NOTE,
    auth=[],
    request=SignupInputSerializer,
    responses={
        202: SignupAcceptedSerializer,
        400: PROBLEMS[400],
        403: PROBLEMS[403],
        404: OFF,
        429: THROTTLED,
        503: NO_EMAIL,
    },
)
@never_cache
@api_view(["POST"])
@authentication_classes([])
@throttle_classes([ScopedThrottle])
@public
def signup_view(request):
    data = _body(request)
    signup.start(dict(data))  # sends inside its transaction; a refused message is a 503 and nothing is kept
    return Response({"detail": ACCEPTED}, status=202)


@extend_schema(
    summary="Send a new confirmation link for a signup (public)",
    description="The earlier link stops working. The answer is the same for any email." + CSRF_NOTE,
    auth=[],
    request=SignupResendInputSerializer,
    responses={
        202: SignupAcceptedSerializer,
        400: PROBLEMS[400],
        403: PROBLEMS[403],
        404: OFF,
        429: THROTTLED,
        503: NO_EMAIL,
    },
)
@never_cache
@api_view(["POST"])
@authentication_classes([])
@throttle_classes([ScopedThrottle])
@public
def signup_resend(request):
    data = _body(request)
    signup.resend(dict(data))
    return Response({"detail": RESENT}, status=202)


@extend_schema(
    summary="Confirm a signup's email with its link's token and the password (public)",
    description="Creates the account, the organization (you are its Admin) and signs this browser in; answers `me`. "
    "404 /problems/signup-link-unavailable for an unknown, used, replaced or expired link; 400 for a password that "
    "isn't the one chosen at signup; 409 when the email got an account meanwhile." + CSRF_NOTE,
    auth=[],
    request=SignupVerifyInputSerializer,
    responses={
        200: MeSerializer,
        400: PROBLEMS[400],
        403: PROBLEMS[403],
        404: OpenApiResponse(
            ProblemSerializer,
            description="Signup is off (/problems/signup-disabled), or the link isn't valid any more "
            "(/problems/signup-link-unavailable)",
        ),
        409: PROBLEMS[409],
        429: THROTTLED,
        503: NO_EMAIL,
    },
)
@never_cache
@api_view(["POST"])
@authentication_classes([])
@throttle_classes([ScopedThrottle])
@public
def signup_verify(request):
    data = _body(request)
    membership = signup.verify(dict(data))
    login(request, membership.user, backend="django.contrib.auth.backends.ModelBackend")
    request.session.pop(access.SSO_ORGANIZATION, None)  # a password session: no confinement
    request.session[access.CURRENT_ORGANIZATION] = str(membership.organization_id)
    return Response(me_body(request))


signup_view.cls.throttle_scope = "signup"  # type: ignore[attr-defined]
signup_resend.cls.throttle_scope = "signup"  # type: ignore[attr-defined]
signup_verify.cls.throttle_scope = "login"  # type: ignore[attr-defined]
