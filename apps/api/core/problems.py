"""RFC 9457 problem details for every API error.

Every error the API answers is `application/problem+json` with `type`, `title`, `status` and `detail`, plus `errors`
(`{field or parameter: [messages]}`) when the request had fields or parameters to fix, and the extension members a
type documents (`revision`, `capability`, `dependents`, ...). `type` is one of PROBLEM_TYPES: a relative URI that
names what went wrong, so a client can branch on it instead of on the message; `title` is the same for every problem
of a type, and `detail` says what happened this time. docs/API.md lists them.

Three paths produce problems:
- `problem_handler`, DRF's exception handler, for anything a DRF view raises (validation, roles, throttles, 404s,
  If-Match, conflicts, malformed or unsupported bodies);
- the Django-level views below, for what never reaches a DRF view under /api/: a URL that matches no route
  (`handler404`), an unhandled exception (`handler500`, logged by Django and never echoed), a request Django refuses
  before routing (`handler400`) and a CSRF failure on a non-DRF view (`CSRF_FAILURE_VIEW`); other paths keep Django's
  own pages (the SPA catch-all answers every non-API path anyway);
- core/upload_limit.py, which refuses oversize bodies before anything reads them (411, 413).
"""

from __future__ import annotations

import logging
import math
import traceback
from http import HTTPStatus
from typing import Any

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import RequestDataTooBig
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DataError
from django.http import Http404, JsonResponse
from django.views import defaults
from rest_framework import exceptions, status

logger = logging.getLogger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"

# ---- problem types ------------------------------------------------------------------------------------------------
# {type: (status, title)}. A type keeps its title everywhere; the status is the usual one (the signed-out problem is
# 403 for the session API, see NOT_SIGNED_IN). docs/API.md and the OpenAPI Problem schema list these.

INVALID_REQUEST = "/problems/invalid-request"
MALFORMED_REQUEST = "/problems/malformed-request"
INVALID_CREDENTIALS = "/problems/invalid-credentials"
INVALID_TOKEN = "/problems/invalid-token"  # noqa: S105  (a problem type, not a secret)
NOT_SIGNED_IN = "/problems/not-signed-in"
FORBIDDEN = "/problems/forbidden"
READ_ONLY_TOKEN = "/problems/read-only-token"  # noqa: S105  (a problem type, not a secret)
CSRF_FAILED = "/problems/csrf-failed"
CAPABILITY_REQUIRED = "/problems/capability-required"
NOT_FOUND = "/problems/not-found"
METHOD_NOT_ALLOWED = "/problems/method-not-allowed"
NOT_ACCEPTABLE = "/problems/not-acceptable"
CONFLICT = "/problems/conflict"
LENGTH_REQUIRED = "/problems/length-required"
PRECONDITION_FAILED = "/problems/precondition-failed"
PRECONDITION_REQUIRED = "/problems/precondition-required"
CONTENT_TOO_LARGE = "/problems/content-too-large"
UNSUPPORTED_MEDIA_TYPE = "/problems/unsupported-media-type"
RATE_LIMITED = "/problems/rate-limited"
SERVER_ERROR = "/problems/server-error"
DATABASE_UNAVAILABLE = "/problems/database-unavailable"
IMPERSONATION_RESTRICTED = "/problems/impersonation-restricted"
IMPERSONATION_ENDED = "/problems/impersonation-ended"
IMPERSONATION_STARTED = "/problems/impersonation-started"
ORGANIZATION_SUSPENDED = "/problems/organization-suspended"
LIMIT_REACHED = "/problems/limit-reached"
SECOND_FACTOR_REQUIRED = "/problems/second-factor-required"

PROBLEM_TYPES: dict[str, tuple[int, str]] = {
    INVALID_REQUEST: (400, "Invalid request"),
    MALFORMED_REQUEST: (400, "Malformed request"),
    "/problems/too-many-events": (400, "Too many events to export"),
    "/problems/catalog-code-invalid": (400, "Catalog-as-code files have errors"),
    INVALID_TOKEN: (401, "Invalid API token"),
    INVALID_CREDENTIALS: (403, "Invalid email or password"),
    NOT_SIGNED_IN: (403, "Not signed in"),
    FORBIDDEN: (403, "Not allowed"),
    READ_ONLY_TOKEN: (403, "Read-only token"),
    CSRF_FAILED: (403, "CSRF check failed"),
    CAPABILITY_REQUIRED: (403, "Capability required"),
    "/problems/reauthentication-required": (403, "Re-authentication required"),
    IMPERSONATION_RESTRICTED: (403, "Not allowed while impersonating"),
    ORGANIZATION_SUSPENDED: (403, "Organization suspended"),
    SECOND_FACTOR_REQUIRED: (403, "Second factor required"),
    NOT_FOUND: (404, "Not found"),
    "/problems/invitation-unavailable": (404, "Invitation unavailable"),
    "/problems/signup-disabled": (404, "Signup is off"),
    "/problems/signup-link-unavailable": (404, "Signup link unavailable"),
    METHOD_NOT_ALLOWED: (405, "Method not allowed"),
    NOT_ACCEPTABLE: (406, "Not acceptable"),
    CONFLICT: (409, "Conflict"),
    IMPERSONATION_ENDED: (409, "Impersonation ended"),
    IMPERSONATION_STARTED: (409, "Impersonation started"),
    "/problems/duplicate-name": (409, "Duplicate name"),
    "/problems/duplicate-relationship": (409, "Duplicate relationship"),
    "/problems/stale-value": (409, "Stale value"),
    "/problems/retirement-needs-acknowledgment": (409, "Retirement needs acknowledgment"),
    "/problems/core-relationship-type": (409, "Core relationship type"),
    "/problems/import-refused": (409, "Import refused"),
    "/problems/catalog-code-stale": (409, "Catalog-as-code plan is stale"),
    "/problems/reassign-stale": (409, "Reassign plan is stale"),
    "/problems/sso-draft-changed": (409, "Sign-in draft changed"),
    "/problems/sso-test-required": (409, "Sign-in test required"),
    "/problems/sso-local-only": (409, "Local sign-in only"),
    "/problems/invitation-wrong-account": (409, "Signed in as another account"),
    "/problems/invitation-required": (409, "Invitation required"),
    "/problems/invitation-other-sign-in": (409, "Signed in through another organization"),
    LIMIT_REACHED: (409, "Limit reached"),
    LENGTH_REQUIRED: (411, "Length required"),
    PRECONDITION_FAILED: (412, "Precondition failed"),
    PRECONDITION_REQUIRED: (428, "Precondition required"),
    CONTENT_TOO_LARGE: (413, "Content too large"),
    UNSUPPORTED_MEDIA_TYPE: (415, "Unsupported media type"),
    RATE_LIMITED: (429, "Too many requests"),
    SERVER_ERROR: (500, "Server error"),
    DATABASE_UNAVAILABLE: (503, "Database unavailable"),
    "/problems/signup-unavailable": (503, "Signup unavailable"),
    "/problems/email-failed": (503, "Email not sent"),
    "/problems/signup-paused": (503, "Signup paused"),
}

# The default type of a problem by status, when the exception names none.
STATUS_TYPES: dict[int, str] = {
    400: INVALID_REQUEST,
    401: INVALID_TOKEN,
    403: FORBIDDEN,
    404: NOT_FOUND,
    405: METHOD_NOT_ALLOWED,
    406: NOT_ACCEPTABLE,
    409: CONFLICT,
    411: LENGTH_REQUIRED,
    412: PRECONDITION_FAILED,
    428: PRECONDITION_REQUIRED,
    413: CONTENT_TOO_LARGE,
    415: UNSUPPORTED_MEDIA_TYPE,
    429: RATE_LIMITED,
    500: SERVER_ERROR,
}

NOT_FOUND_DETAIL = "This record doesn't exist or you don't have access."
NO_ROUTE_DETAIL = "There is no API endpoint at this path."
SERVER_ERROR_DETAIL = "Something went wrong on the server. Nothing in the response says what; the server log does."
UNUSABLE_INPUT_DETAIL = (
    "A value in this request can't be stored or computed with. Check the fields and parameters sent."
)

# Exceptions that only input can cause: text PostgreSQL refuses or a value past a column's range (DataError),
# a string that isn't valid Unicode (UnicodeEncodeError), nesting past the recursion limit (RecursionError) and
# arithmetic past what a date or number can hold (OverflowError). The parsers and core/input.py refuse these inputs
# by name first; this is the net for one that gets past them, so it is a 400 rather than a 500.
UNUSABLE_INPUT = (DataError, UnicodeEncodeError, RecursionError, OverflowError)


def title_of(problem_type: str, status_code: int) -> str:
    known = PROBLEM_TYPES.get(problem_type)
    if known is not None:
        return known[1]
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def problem_body(status_code: int, detail: str, problem_type: str | None = None, **extra: Any) -> dict[str, Any]:
    problem_type = problem_type or STATUS_TYPES.get(status_code) or "about:blank"
    return {
        "type": problem_type,
        "title": title_of(problem_type, status_code),
        "status": status_code,
        "detail": detail,
        **extra,
    }


def problem_json(status_code: int, body: dict[str, Any]) -> JsonResponse:
    response = JsonResponse(
        body, status=status_code, content_type=PROBLEM_CONTENT_TYPE, json_dumps_params={"ensure_ascii": False}
    )
    response["X-Content-Type-Options"] = "nosniff"  # also when answering before SecurityMiddleware (upload_limit)
    return response


def problem_response(status_code: int, detail: str, problem_type: str | None = None, **extra: Any) -> JsonResponse:
    """A problem from plain Django code: the Django-level handlers and middleware."""
    return problem_json(status_code, problem_body(status_code, detail, problem_type, **extra))


# ---- exceptions ---------------------------------------------------------------------------------------------------


class FieldErrors(exceptions.APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Some fields need attention."

    def __init__(self, errors):
        super().__init__(self.default_detail)
        self.errors = errors


class Conflict(exceptions.APIException):
    """409. `extra` members are added to the problem body (RFC 9457 extension members)."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "This change conflicts with existing records."

    def __init__(self, detail=None, extra=None):
        super().__init__(detail)
        self.extra = extra or {}


class LimitReached(Conflict):
    """409: a create would take the organization past one of its limits (core/limits.py). The problem names the
    limit, its maximum, the current usage and how many the request would add, so the SPA can say which meter is full."""

    def __init__(self, limit: str, maximum: int, usage: int, adding: int, detail: str):
        super().__init__(
            detail, extra={"type": LIMIT_REACHED, "limit": limit, "maximum": maximum, "usage": usage, "adding": adding}
        )
        self.limit = limit


class CapabilityRequired(exceptions.PermissionDenied):
    """403 for a feature the current organization's edition doesn't include (core/capabilities.py). The problem
    names the capability, so the SPA and scripts can tell it apart from a role refusal."""

    default_detail = "This feature isn't enabled for this organization."
    TYPE = CAPABILITY_REQUIRED

    def __init__(self, capability, preview: bool = False):
        # preview: a preview feature this installation keeps off (SOFTWARDEN_PREVIEW_FEATURES), whatever the edition.
        super().__init__(
            f"This needs the {capability} capability, a preview feature this installation hasn't turned on."
            if preview
            else f"This needs the {capability} capability, which this organization's edition doesn't include."
        )
        self.extra = {"type": self.TYPE, "capability": capability}


class ReadOnlyToken(exceptions.PermissionDenied):
    """403: a read-only API token tried to write (core/drf.RolePermission)."""

    default_detail = "This token is read-only."

    def __init__(self):
        super().__init__(self.default_detail)
        self.extra = {"type": READ_ONLY_TOKEN}


class ImpersonationRestricted(exceptions.PermissionDenied):
    """403: platform staff impersonating a member asked for something an impersonation may never do (core/impersonation
    .RESTRICTED: credentials, identities, single sign-on, tokens, secrets, deletion, another impersonation)."""

    default_detail = "This isn't allowed while impersonating someone."

    def __init__(self, detail=None):
        super().__init__(detail or self.default_detail)
        self.extra = {"type": IMPERSONATION_RESTRICTED}


class SecondFactorRequired(exceptions.PermissionDenied):
    """403: platform staff signed in with a password but haven't sent a TOTP code in this session
    (catalog/staff_second_factor.py). `enrolled` says whether to verify (true) or enroll first (false)."""

    def __init__(self, enrolled: bool):
        step = "send a code from your authenticator app" if enrolled else "set up your second factor"
        super().__init__(f"The platform console needs a second factor: {step} (platform/second-factor).")
        self.extra = {"type": SECOND_FACTOR_REQUIRED, "enrolled": enrolled}


class OrganizationSuspended(exceptions.PermissionDenied):
    """403: the current organization is suspended by the platform operator (catalog/platform_admin.suspend)."""

    default_detail = (
        "This organization is suspended by the service operator. Your data is kept; contact the operator to restore "
        "access."
    )

    def __init__(self):
        super().__init__(self.default_detail)
        self.extra = {"type": ORGANIZATION_SUSPENDED}


class InvalidCredentials(exceptions.AuthenticationFailed):
    """Sign-in with an email and password that don't match an active account (catalog/views.login_view). 403 like
    every refusal of the session API (it has no authentication challenge to send)."""

    def __init__(self):
        super().__init__("That email and password don't match an active account.")
        self.extra = {"type": INVALID_CREDENTIALS}


class CsrfFailed(exceptions.PermissionDenied):
    """403: a signed-in session wrote without a valid CSRF token (core/authentication.SessionAuthentication)."""

    def __init__(self, reason: str):
        super().__init__(f"CSRF Failed: {reason}")
        self.extra = {"type": CSRF_FAILED}


# ---- DRF ----------------------------------------------------------------------------------------------------------


def _type_for(exc: exceptions.APIException) -> str:
    if isinstance(exc, exceptions.NotAuthenticated | exceptions.AuthenticationFailed):
        return NOT_SIGNED_IN
    if isinstance(exc, exceptions.ParseError):
        return MALFORMED_REQUEST
    return STATUS_TYPES.get(exc.status_code, "about:blank")


def _detail(exc: exceptions.APIException) -> str:
    """The message; a serializer-style detail ({field: [...]}, [...]) goes to `errors` instead."""
    if isinstance(exc.detail, dict | list):
        return str(exc.default_detail)
    return str(exc.detail)


def _own_frame(exc: BaseException):
    """The deepest frame of the exception's traceback in this application's code (not a dependency's), else the
    deepest frame: where the input should have been refused."""
    from django.conf import settings

    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    base = str(settings.BASE_DIR)
    own = [
        f
        for f in frames
        if f.filename.startswith(base) and ".venv" not in f.filename and "site-packages" not in f.filename
    ]
    found = own or frames
    return found[-1] if found else None


def problem_handler(exc, context):
    """DRF's EXCEPTION_HANDLER. Anything that isn't an API error (a bug) is left to Django, whose handler500 logs it
    and answers the server-error problem without the exception's text."""
    from rest_framework.views import set_rollback  # not at import time: DRF loads this module while defining APIView

    if isinstance(exc, Http404):
        exc = exceptions.NotFound(NOT_FOUND_DETAIL)
    elif isinstance(exc, DjangoPermissionDenied):
        exc = exceptions.PermissionDenied()
    elif isinstance(exc, DjangoValidationError):
        exc = FieldErrors(exc.message_dict if hasattr(exc, "error_dict") else {"non_field": exc.messages})
    elif isinstance(exc, RequestDataTooBig):
        exc = exceptions.APIException("The request body is larger than this installation accepts.")
        exc.status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    elif isinstance(exc, UNUSABLE_INPUT):
        # Logged without the exception's message, which can quote the value (a secret, say): the type and the line
        # are enough to find the parser that should have refused it.
        where = _own_frame(exc)
        logger.warning(
            "Refused unusable input with 400: %s at %s",
            type(exc).__name__,
            f"{where.filename}:{where.lineno}" if where else "?",
        )
        exc = exceptions.APIException(UNUSABLE_INPUT_DETAIL)
        exc.status_code = status.HTTP_400_BAD_REQUEST
    if not isinstance(exc, exceptions.APIException):
        return None
    extra = dict(getattr(exc, "extra", {}))
    problem_type = extra.pop("type", None) or _type_for(exc)
    body = problem_body(exc.status_code, _detail(exc), problem_type)
    if isinstance(exc, FieldErrors):
        body["errors"] = exc.errors
    elif isinstance(exc.detail, dict):
        body["errors"] = {k: [str(m) for m in (v if isinstance(v, list) else [v])] for k, v in exc.detail.items()}
    elif isinstance(exc.detail, list):
        body["errors"] = {"non_field": [str(m) for m in exc.detail]}
    body.update(extra)
    set_rollback()
    # A plain JsonResponse, not a DRF Response: the view's renderer (YAML for the schema, CSV for an export) would
    # otherwise render the problem; DRF's finalize_response still adds Allow and Vary.
    response = problem_json(exc.status_code, body)
    for name, value in getattr(exc, "headers", {}).items():  # e.g. the current ETag with a 412
        response[name] = value
    auth_header = getattr(exc, "auth_header", None)
    if auth_header:
        response["WWW-Authenticate"] = auth_header
    if isinstance(exc, exceptions.Throttled):  # seconds until the bucket has room again; at least 1
        response["Retry-After"] = str(max(1, math.ceil(getattr(exc, "wait", None) or 1)))
    return response


# ---- Django-level handlers (softwarden/urls.py, settings.CSRF_FAILURE_VIEW) -----------------------------------------


def is_api(request) -> bool:
    return request.path.startswith("/api/")


def bad_request(request, exception=None, template_name="400.html"):
    from django.contrib.sessions.exceptions import SessionInterrupted

    if not is_api(request):
        return defaults.bad_request(request, exception, template_name)
    if isinstance(exception, SessionInterrupted):  # our message or Django's, never request data: it says to reload
        return problem_response(400, str(exception), MALFORMED_REQUEST)
    return problem_response(400, "The server couldn't accept this request.", MALFORMED_REQUEST)


def permission_denied(request, exception=None, template_name="403.html"):
    if not is_api(request):
        return defaults.permission_denied(request, exception, template_name)
    return problem_response(403, "You don't have access to this.")


NIL_UUID = "00000000-0000-0000-0000-000000000000"


def _names_a_record(path: str) -> bool:
    """Whether `path` is a record route with something other than a UUID where the ID goes (`/applications/x`): it
    resolves once one of its segments is replaced by a UUID."""
    from django.urls import Resolver404, resolve

    parts = path.split("/")
    for i, part in enumerate(parts):
        if not part or part in ("api", "v1"):
            continue
        try:
            resolve("/".join([*parts[:i], NIL_UUID, *parts[i + 1 :]]))
        except Resolver404:
            continue
        return True
    return False


def page_not_found(request, exception=None, template_name="404.html"):
    if not is_api(request):
        return defaults.page_not_found(request, exception, template_name)
    if _names_a_record(request.path):  # said as an unknown UUID is, not "no endpoint"
        return problem_response(404, NOT_FOUND_DETAIL)
    return problem_response(404, NO_ROUTE_DETAIL)


DATABASE_UNAVAILABLE_DETAIL = "The database can't be reached right now. Try again in a moment."
DATABASE_RETRY_AFTER = 30  # seconds: a restarting database is usually back within this


def database_unreachable(exc: BaseException | None) -> bool:
    """Whether `exc` means the database can't be reached: Django's InterfaceError, or an OperationalError with no
    SQLSTATE (the connection was refused or dropped before the server answered) or a connection-class one: 08xxx
    (connection exception), 57P0x (the server is shutting down, starting or crashed) and 53300 (too many connections).
    A deadlock (40P01), a serialization failure (40001), a full disk (53100) or out of memory (53200) is still a 500."""
    from django.db import InterfaceError, OperationalError

    if isinstance(exc, InterfaceError):
        return True
    if not isinstance(exc, OperationalError):
        return False
    sqlstate = getattr(exc.__cause__, "sqlstate", None)
    return not sqlstate or sqlstate.startswith(("08", "57P0")) or sqlstate == "53300"


def server_error(request, template_name="500.html"):
    """handler500. An API request that failed because the database can't be reached (database_unreachable) is a 503
    with Retry-After, not a 500; Django has already logged the exception either way."""
    import sys

    if not is_api(request):
        return defaults.server_error(request, template_name)
    if database_unreachable(sys.exc_info()[1]):
        response = problem_response(503, DATABASE_UNAVAILABLE_DETAIL, DATABASE_UNAVAILABLE)
        response["Retry-After"] = str(DATABASE_RETRY_AFTER)
        return response
    return problem_response(500, SERVER_ERROR_DETAIL)


def csrf_failure(request, reason=""):
    """CSRF_FAILURE_VIEW: a session POST to a non-DRF view without a valid CSRF token. Under /api/ it is the
    csrf-failed problem (DRF views report their own through core/authentication.py with the same type); elsewhere
    Django's own page."""
    if not is_api(request):
        from django.views.csrf import csrf_failure as django_csrf_failure

        return django_csrf_failure(request, reason)
    return problem_response(403, f"CSRF Failed: {reason}" if reason else "CSRF Failed.", CSRF_FAILED)
