"""Self-service password reset routes (catalog/password_reset.py). Public and
CSRF-checked even with nobody signed in (the signup routes' pattern), throttled per client address: asking for a link
on the `password_reset` rate (SOFTWARDEN_PASSWORD_RESET_THROTTLE_RATE), using one on the sign-in rate.

    POST auth/password-reset          {email}             202, the same for any email
    POST auth/password-reset/confirm  {token, password}   204, every session of the account ends; nobody is signed in

The installation can't send email: 503 /problems/password-reset-unavailable.

Minimum role: none (public); tests/test_permissions.py lists them in PUBLIC."""

from django.views.decorators.cache import never_cache
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, throttle_classes
from rest_framework.response import Response

from core.drf import PROBLEMS, ProblemSerializer, public
from core.setup_views import _CsrfSession
from core.throttling import ScopedThrottle

from . import password_reset

SENT = (
    "Check your email. If this address has an account that signs in with a password, we've sent it a link to choose "
    f"a new one; it works once, for {password_reset.RESET_MINUTES} minutes."
)


class PasswordResetInputSerializer(serializers.Serializer):
    email = serializers.EmailField(help_text="The email you sign in with.")


class PasswordResetConfirmInputSerializer(serializers.Serializer):
    token = serializers.CharField(help_text="The token from the emailed link's fragment (/reset-password#<token>).")
    password = serializers.CharField(help_text="The new password: 12 characters or more, not a common password.")


class PasswordResetSentSerializer(serializers.Serializer):
    detail = serializers.CharField(help_text="What to do next. The same whether or not the email has an account.")


THROTTLED = OpenApiResponse(description="Too many attempts from this address; Retry-After says when to try again")
NO_EMAIL = OpenApiResponse(
    ProblemSerializer,
    description="This installation can't send email, so it can't reset passwords by email "
    "(/problems/password-reset-unavailable)",
)
CSRF_NOTE = " Needs the CSRF token (GET auth/csrf sets the cookie; send it as X-CSRFToken)."


def _body(request) -> dict:
    _CsrfSession().check(request)
    return request.data if isinstance(request.data, dict) else {}


@extend_schema(
    summary="Ask for a password reset link (public)",
    description="Emails a single-use link, good for an hour, to an active account that signs in with a password. "
    "The answer is the same for any email: one with no account, one that signs in through single sign-on only, one "
    "that was sent a link a moment ago and one that was sent a link now. At most 3 links per account an hour."
    + CSRF_NOTE,
    auth=[],
    request=PasswordResetInputSerializer,
    responses={202: PasswordResetSentSerializer, 400: PROBLEMS[400], 403: PROBLEMS[403], 429: THROTTLED, 503: NO_EMAIL},
)
@never_cache
@api_view(["POST"])
@authentication_classes([])
@throttle_classes([ScopedThrottle])
@public
def password_reset_request(request):
    data = _body(request)
    password_reset.request_reset(dict(data))
    return Response({"detail": SENT}, status=202)


@extend_schema(
    summary="Set a new password with a reset link's token (public)",
    description="Sets the password, ends every session of the account (each is signed out on its next request) and "
    "every other open reset link, and clears a forced password change. Nobody is signed in: sign in with the new "
    "password. 404 /problems/password-reset-link-unavailable for an unknown, used, replaced or expired link; 400 for "
    "a password the validators refuse (the link stays usable)." + CSRF_NOTE,
    auth=[],
    request=PasswordResetConfirmInputSerializer,
    responses={
        204: None,
        400: PROBLEMS[400],
        403: PROBLEMS[403],
        404: OpenApiResponse(ProblemSerializer, description="The link isn't valid any more"),
        429: THROTTLED,
        503: NO_EMAIL,
    },
)
@never_cache
@api_view(["POST"])
@authentication_classes([])
@throttle_classes([ScopedThrottle])
@public
def password_reset_confirm(request):
    data = _body(request)
    password_reset.confirm_reset(dict(data))
    return Response(status=204)


password_reset_request.cls.throttle_scope = "password_reset"  # type: ignore[attr-defined]
password_reset_confirm.cls.throttle_scope = "login"  # type: ignore[attr-defined]
