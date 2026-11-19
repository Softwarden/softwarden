"""The DRF layer's foundation ("DRF layer").

Every route declares the minimum role it needs, and that declaration is also what enforces it:

- `min_role(read, write=None, **overrides)` on a function view (under `@api_view`), or `role_policy = policy(...)` on a
  viewset, binds a `RolePolicy` to `RolePermission`. `public` marks a route that answers without signing in.
- `DenyUndeclared` is the project's default permission class, so a view that forgets to declare a role is refused for
  everyone instead of falling back to "any signed-in user". tests/test_url_coverage.py walks every route and fails on
  one without a declaration.

Roles, lowest first: `public` (no account), `account` (any signed-in account, even one that must change its password or
has no membership), then the membership roles Viewer < Editor < Admin of the current organization (core/access.py).
`platform` is apart from that ladder: platform staff signed in with a session, never an API token, whatever their
memberships (core/platform_access.py; the routes under /api/v1/platform/). A suspended organization's
members are refused on every membership route (/problems/organization-suspended) except a view that sets
`allowed_when_suspended`.
The organization and role always come from core.access, never from the client. A read-only API token is refused
on any write to a signed-in route (403 "This token is read-only.") before the role check; public routes don't refuse
it, and a method the route doesn't have answers 405 first. Any API token is refused (403) on a route that issues or
changes sign-in authority (core/impersonation_routes.SESSION_ONLY_ROUTES): a leaked token must not become a
browser session that outlives it. `SessionOnly` and `require_session` refuse tokens on other session-only routes.

`TenantViewSet` is the base for every model viewset:

- the queryset is always filtered to the request organization, so another organization's ID is a 404 problem;
- create, update and destroy call service hooks (`create_object`, `update_object`, `destroy_object`) that validate,
  scope and audit in one transaction; the base never calls `serializer.save()` and the serializers are read-only;
- retrieve and every write response carry `ETag: "<revision>"`; PATCH and DELETE with `If-Match` apply only while the
  record still has that revision (412 problem otherwise). No header is a 428 for a signed-in session
  (SOFTWARDEN_REQUIRE_IF_MATCH, on by default) and no check for an API token;
- lists are keyset-paginated as `{results, next, previous}` (the shape the SPA reads), filtered by a django-filter
  FilterSet whose unknown parameters are refused, and ordered from a per-viewset whitelist.
"""

from __future__ import annotations

import base64
import datetime
import functools
import hashlib
import json
import types
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, overload

from django import forms
from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404
from django_filters import rest_framework as filters
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse
from rest_framework import exceptions, permissions, serializers, status, viewsets
from rest_framework.pagination import BasePagination
from rest_framework.response import Response
from rest_framework.views import APIView

from core.input import whole_number

from . import access
from . import openapi as _openapi  # noqa: F401  (registers the session scheme for OpenAPI)
from .capabilities import require_capability
from .models import Role
from .permissions import DenyUndeclared  # noqa: F401  (the default permission; re-exported)
from .problems import PRECONDITION_FAILED, PRECONDITION_REQUIRED, FieldErrors, OrganizationSuspended, ReadOnlyToken

# ---- roles and declarations ---------------------------------------------------------------------------------------

PUBLIC = "public"
ACCOUNT = "account"
VIEWER = Role.VIEWER.value
EDITOR = Role.EDITOR.value
ADMIN = Role.ADMIN.value
PLATFORM = (
    "platform"  # platform staff (core/platform_access.py); not above Admin in any organization, beside the ladder
)
LEVELS = {PUBLIC: 0, ACCOUNT: 1, VIEWER: 2, EDITOR: 3, ADMIN: 4, PLATFORM: 5}
SAFE = frozenset(permissions.SAFE_METHODS)


@dataclass(frozen=True)
class RolePolicy:
    """The minimum role per request. `overrides` maps a lowercase HTTP method or a viewset action name to a role; an
    action override wins over a method override, which wins over read (safe methods) or write (the rest)."""

    read: str
    write: str
    overrides: Mapping[str, str] = field(default_factory=dict)
    capability: str | None = None

    def __post_init__(self):
        roles = (self.read, self.write, *self.overrides.values())
        for role in roles:
            if role not in LEVELS:
                raise ValueError(f"Unknown role {role!r}; use one of {', '.join(LEVELS)}.")
        if PLATFORM in roles and set(roles) != {PLATFORM}:
            raise ValueError("A platform route is platform for every method; don't mix it with tenant roles.")

    def role_for(self, method: str, action: str | None = None) -> str:
        if action and action in self.overrides:
            return self.overrides[action]
        method = method.lower()
        if method in self.overrides:
            return self.overrides[method]
        return self.read if method.upper() in SAFE else self.write


def policy(read: str, write: str | None = None, *, capability: str | None = None, **overrides: str) -> RolePolicy:
    """`write` defaults to the stricter of `read` and Editor, so a GET-only declaration never lets a Viewer write if
    a write method is added later. `public` is the only way to make writes public."""
    if read not in LEVELS:
        raise ValueError(f"Unknown role {read!r}; use one of {', '.join(LEVELS)}.")
    if write is None:
        write = read if read == PUBLIC or LEVELS[read] >= LEVELS[EDITOR] else EDITOR
    return RolePolicy(read, write, overrides, capability)


class RolePermission(permissions.BasePermission):
    """Enforces the view's `RolePolicy`: bound on the class by `min_role`/`public`, or read from the viewset's
    `role_policy`. Order: signed in, read-only token, membership, role, then capability (a Viewer refused for role
    sees the role message, as in core/capabilities)."""

    policy: ClassVar[RolePolicy | None] = None
    message = "You don't have access to this."

    @classmethod
    def bound(cls, role_policy: RolePolicy) -> type[RolePermission]:
        return type("MinRole", (cls,), {"policy": role_policy})

    @classmethod
    def policy_of(cls, view: Any) -> RolePolicy | None:
        return cls.policy or getattr(view, "role_policy", None)

    def has_permission(self, request, view):
        role_policy = self.policy_of(view)
        if role_policy is None:  # a RolePermission with nothing to enforce is a programming error: refuse
            raise exceptions.PermissionDenied("This endpoint doesn't declare who may use it.")
        if getattr(view, request.method.lower(), None) is None:
            return True  # not a method this route has; dispatch answers 405
        role = role_policy.role_for(request.method, getattr(view, "action", None))
        if role == PLATFORM:
            from .platform_access import check_platform

            return check_platform(request, view)
        if role == PUBLIC:
            return True
        if not (request.user and request.user.is_authenticated):
            return False
        token = request.auth
        if getattr(token, "read_only", False) and request.method not in SAFE:
            raise ReadOnlyToken()
        if is_token_request(request) and session_only_route(request):
            raise exceptions.PermissionDenied(SIGN_IN_AUTHORITY)  # Before the role, for any role
        if role == ACCOUNT:
            return True
        membership = access.current_membership(request)  # 403 without one, or while the password must change
        if membership.organization.suspended_at is not None and not getattr(view, "allowed_when_suspended", False):
            raise OrganizationSuspended()
        if LEVELS[membership.role] < LEVELS[role]:
            (access.require_admin if role == ADMIN else access.require_write)(membership.role)
        capability = role_policy.capability or getattr(view, "required_capability", None)
        if capability:
            require_capability(membership.organization, capability)
        return True


# ---- signed-in sessions only ----------------------------------------------------------------------------------------

SESSION_ONLY = "This needs a signed-in session, not an API token."
SIGN_IN_AUTHORITY = (
    "This changes who can sign in, so it needs a signed-in session, not an API token. Do it in Softwarden itself."
)


def is_token_request(request) -> bool:
    """Whether the request is authenticated by an API token rather than a session: a token is an auth object tied to
    one organization (core/access.pinned_organization_id); a session request has no auth object."""
    return access.pinned_organization_id(request) is not None


def require_session(request, detail: str = SESSION_ONLY) -> None:
    """403 with `detail` for a request authenticated by an API token."""
    if is_token_request(request):
        raise exceptions.PermissionDenied(detail)


def session_only_route(request) -> bool:
    """Whether the request's (method, route) issues or changes sign-in authority (core/impersonation_routes
    .SESSION_ONLY_ROUTES), which RolePermission refuses to an API token."""
    from .impersonation_routes import SESSION_ONLY_ROUTES

    match = getattr(request, "resolver_match", None)
    route = getattr(match, "route", None)
    return route is not None and (request.method, route) in SESSION_ONLY_ROUTES


# Mixin, before TenantViewSet or RoleAPIView: every method the view has needs a signed-in session; an API token is
# refused with `session_only_detail` (403). Checked after the role (signed out, a read-only token's write and a role
# refusal answer first) and only for a method the view has (405 first, as on every route). A comment, not a docstring:
# a view's inherited docstring becomes its OpenAPI description.
class SessionOnly:
    session_only_detail: ClassVar[str] = SESSION_ONLY

    def check_permissions(self, request):
        super().check_permissions(request)  # type: ignore[misc]
        if getattr(self, request.method.lower(), None) is not None:
            require_session(request, self.session_only_detail)


def _check_order(func: Any, name: str) -> None:
    if hasattr(func, "cls") or hasattr(func, "view_class"):
        raise TypeError(f"@{name} must come after (below) @api_view.")


def min_role(read: str, write: str | None = None, *, capability: str | None = None, **overrides: str):
    """Declare and enforce a function view's minimum role. Place it under `@api_view`:

    @api_view(["GET", "POST"])
    @min_role(VIEWER, EDITOR)
    def people(request): ...
    """
    role_policy = policy(read, write, capability=capability, **overrides)

    def decorate(func):
        _check_order(func, "min_role")
        func.permission_classes = [RolePermission.bound(role_policy)]
        func.role_policy = role_policy
        return func

    return decorate


PUBLIC_POLICY = RolePolicy(PUBLIC, PUBLIC)


def public(func):
    """A route that answers without signing in (minimum role: none). On a DRF function view place it under
    `@api_view`; on a plain Django view (probes, the SPA) it only records the declaration, since such views have no
    DRF authentication at all. Every public route is listed in tests/test_permissions.py PUBLIC."""
    func.permission_classes = [RolePermission.bound(PUBLIC_POLICY)]
    func.role_policy = PUBLIC_POLICY
    return func


def declared_policy(view: Any) -> RolePolicy | None:
    """The policy a DRF view enforces, or None when it has none (tests/test_url_coverage.py). `view` is a view class
    or the callable `as_view()` returned, whose `permission_classes=` initkwarg wins over the class's."""
    view_class = getattr(view, "cls", view)
    initkwargs = getattr(view, "initkwargs", None) or {}
    classes = initkwargs.get("permission_classes") or getattr(view_class, "permission_classes", None) or ()
    for permission in classes:
        if isinstance(permission, type) and issubclass(permission, RolePermission):
            return permission.policy_of(view_class)
    return None


def request_membership(request):
    """The membership RolePermission resolved for this request (core.access caches it on the request)."""
    return access.current_membership(request)


# ---- problems -----------------------------------------------------------------------------------------------------


class PreconditionFailed(exceptions.APIException):
    """412: the If-Match revision is not the record's current one. Nothing was changed."""

    status_code = status.HTTP_412_PRECONDITION_FAILED
    default_detail = "This record changed since you read it."

    def __init__(self, current: str):
        super().__init__(
            "This record changed since you read it; nothing was changed. Fetch it again and reapply your change."
        )
        self.extra = {"type": PRECONDITION_FAILED, "revision": current}
        self.headers = {"ETag": etag(current)}


class PreconditionRequired(exceptions.APIException):
    """428: a signed-in session's conditional write without If-Match while SOFTWARDEN_REQUIRE_IF_MATCH is on.
    Nothing was changed."""

    status_code = 428
    default_detail = "Send If-Match with the ETag you read."

    def __init__(self):
        super().__init__(
            "Send If-Match with the record's ETag from when you read it, so a change someone else saved since isn't "
            "overwritten. Nothing was changed."
        )
        self.extra = {"type": PRECONDITION_REQUIRED}


# ---- revisions (ETag / If-Match) ------------------------------------------------------------------------------------

REVISION_LENGTH = 32
REVISION_SKIP = frozenset({"created_at", "updated_at"})


def _state(obj: Any, skip: Iterable[str] = ()) -> dict[str, Any]:
    left_out = REVISION_SKIP | set(skip)
    return {
        f"{type(obj)._meta.label}.{f.attname}": f.value_from_object(obj)
        for f in type(obj)._meta.concrete_fields
        if f.name not in left_out
    }


def revision(*objects: Any, extra: Mapping[str, Any] | None = None, skip: Iterable[str] = ()) -> str:
    """A fingerprint of the stored state of `objects` (their concrete fields, timestamps left out) plus `extra` (for
    example a record's tag IDs). Computed from the row rather than a counter, so it moves on a change made by any
    path. `skip` names fields to leave out: secret slots (a ciphertext or a token
    hash is never an ETag input; its `_set_at` column still moves the revision when it is replaced) and blobs."""
    state: dict[str, Any] = {}
    for obj in objects:
        state |= _state(obj, skip)
    state |= dict(extra or {})
    payload = json.dumps(state, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:REVISION_LENGTH]


def etag(rev: str) -> str:
    return f'"{rev}"'


def if_match_tags(header: str) -> set[str]:
    """Entity tags from If-Match. If-Match uses strong comparison (RFC 9110 §13.1.1), so weak tags never match."""
    tags = (t.strip() for t in header.split(","))
    return {t.strip('"') for t in tags if t and not t.startswith("W/")}


def if_match_required(request) -> bool:
    """Whether a conditional write must carry If-Match: SOFTWARDEN_REQUIRE_IF_MATCH is on and the request is a
    signed-in browser session. API tokens (scripts, the MCP server, which sends If-Match on its own) are never held to
    it, so turning it on breaks no integration."""
    if not getattr(settings, "SOFTWARDEN_REQUIRE_IF_MATCH", False):
        return False
    from .authentication import SessionAuthentication

    return isinstance(getattr(request, "successful_authenticator", None), SessionAuthentication)


def require_match(request, current: str) -> None:
    """412 unless the request's If-Match names `current` (or *). No If-Match header: no check, or 428 when
    if_match_required. The caller holds the row lock, so the write that follows applies to the revision compared
    here."""
    header = request.headers.get("If-Match")
    if header is None:
        if if_match_required(request):
            raise PreconditionRequired()
        return
    tags = if_match_tags(header)
    if "*" not in tags and current not in tags:
        raise PreconditionFailed(current)


IF_MATCH = OpenApiParameter(
    "If-Match",
    location=OpenApiParameter.HEADER,
    required=False,
    description='The ETag from a GET (for example "3f2a..."). The write applies only if the record is unchanged '
    "since; otherwise 412 with the current revision. Required from a signed-in session (428 without it, unless the "
    "installation sets SOFTWARDEN_REQUIRE_IF_MATCH=0); an API token may omit it to write unconditionally. `*` matches "
    "any revision.",
)


# ---- brief nested serializers -------------------------------------------------------------------------------------


class ReadOnlySerializer(serializers.Serializer):
    """Serializers describe and render; they never write. Writes go through each app's service functions."""

    def create(self, validated_data):
        raise NotImplementedError("Writes go through service functions, not serializers.")

    def update(self, instance, validated_data):
        raise NotImplementedError("Writes go through service functions, not serializers.")


_TIMESTAMP = serializers.DateTimeField()


def timestamp(value: datetime.datetime | None) -> str | None:
    """A timestamp as every serializer renders it (DRF's DateTimeField: RFC 3339 in UTC with `Z`), for the responses
    built as plain data. None stays None. Fields meant in the organization's timezone (an audit row's `at`, the next
    digest) keep their offset and don't use this."""
    return None if value is None else _TIMESTAMP.to_representation(value)


def iso(value: datetime.datetime | datetime.date | None) -> str | None:
    """The one helper for plain-data responses: a timestamp as `timestamp` renders it (UTC, `Z`), a date as
    YYYY-MM-DD, None as None."""
    if isinstance(value, datetime.datetime):
        return timestamp(value)
    return value.isoformat() if value else None


# The brief serializers render directly instead of through DRF's per-field machinery: a list row nests several of them
# (owners, their departments, team, steward, vendor), and the generic path was most of a list's render time. The
# declared fields still document them; tests/test_list_performance.py pins each direct rendering to the generic one. A
# subclass that adds fields takes the generic path. (Comments, not docstrings: docstrings become OpenAPI text.)


class BriefSerializer(ReadOnlySerializer):
    """A reference to another record: its ID and name."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)

    def to_representation(self, instance):
        if type(self) is not BriefSerializer:
            return super().to_representation(instance)
        return {"id": str(instance.id), "name": str(instance.name)}


class UserBriefSerializer(ReadOnlySerializer):
    """A reference to a user account (integer ID), named by full name or else email."""

    id = serializers.IntegerField(read_only=True)
    name = serializers.SerializerMethodField()
    email = serializers.CharField(read_only=True)

    def get_name(self, user) -> str:
        return user.get_full_name() or user.email

    def to_representation(self, instance):
        if type(self) is not UserBriefSerializer:
            return super().to_representation(instance)
        return {"id": int(instance.id), "name": self.get_name(instance), "email": str(instance.email)}


@functools.cache
def _shared_instance(serializer_class: type[serializers.Serializer]) -> serializers.Serializer:
    # A read-only context: a serializer that writes to it fails instead of carrying state across rows and requests.
    return serializer_class(context=types.MappingProxyType({}))


@overload
def represent(serializer_class: type[serializers.Serializer], obj: None) -> None: ...
@overload
def represent(serializer_class: type[serializers.Serializer], obj: object) -> dict[str, Any]: ...
def represent(serializer_class: type[serializers.Serializer], obj: Any) -> dict[str, Any] | None:
    """`serializer_class(obj).data` for one object inside a list's row builder or a SerializerMethodField, through one
    shared instance per class. Building a serializer per row deep-copies all its declared fields every time, which
    made the renewals list about twice as slow as the plain builders it replaced (list performance).
    Only for serializers that don't read `self.context`. None stays None."""
    if obj is None:
        return None
    return _shared_instance(serializer_class).to_representation(obj)


# ---- the problem schema (OpenAPI) ------------------------------------------------------------------------------


class ProblemSerializer(serializers.Serializer):
    """RFC 9457 problem details."""

    type = serializers.CharField()
    title = serializers.CharField()
    status = serializers.IntegerField()
    detail = serializers.CharField()
    capability = serializers.CharField(
        required=False,
        help_text="With type /problems/capability-required (403): the capability the organization's edition lacks.",
    )
    revision = serializers.CharField(
        required=False, help_text="With type /problems/precondition-failed (412): the record's current revision."
    )
    errors = serializers.DictField(  # type: ignore[assignment]  # the problem member, not Serializer.errors
        child=serializers.ListField(child=serializers.CharField()), required=False, help_text="Field errors."
    )
    dependents = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        help_text="With type /problems/retirement-needs-acknowledgment: the active dependents (id, name, kind, "
        "lifecycle) that must be acknowledged. Retired, archived and not-yet-active ones are in not_required.",
    )
    not_required = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        help_text="With type /problems/retirement-needs-acknowledgment: dependents that need no acknowledgment, "
        "each with a reason (archived, or its lifecycle).",
    )
    count = serializers.IntegerField(
        required=False, help_text="With type /problems/too-many-events (400): how many events the export matched."
    )
    limit = serializers.IntegerField(
        required=False,
        help_text="With type /problems/too-many-events (400): the most one export holds. A /problems/limit-reached "
        "409 names its limit here as text instead: LimitReachedProblem (ProblemOrLimitReached).",
    )
    blocking_references = serializers.DictField(
        child=serializers.IntegerField(),
        required=False,
        help_text="With a refused delete (409): how many records of each kind still refer to this one.",
    )
    blocking_names = serializers.ListField(
        child=serializers.CharField(), required=False, help_text="With a refused delete (409): the first ten of them."
    )
    duplicate_of = BriefSerializer(
        required=False, help_text="With type /problems/duplicate-name (409): the record that already has the name."
    )
    fields = serializers.ListField(  # type: ignore[assignment]  # the problem member, not Serializer.fields
        child=serializers.CharField(),
        required=False,
        help_text="With type /problems/stale-value (409): the fields someone changed since they were read.",
    )


PROBLEMS = {
    400: OpenApiResponse(ProblemSerializer, description="Field errors"),
    403: OpenApiResponse(
        ProblemSerializer,
        description="Signed out, the role can't do this, a read-only token tried to write, or the edition lacks a "
        "capability (type /problems/capability-required)",
    ),
    404: OpenApiResponse(ProblemSerializer, description="Not found, or in another organization"),
    409: OpenApiResponse(
        ProblemSerializer,
        description="Conflict (duplicate name, stale value, refused delete, retirement needing acknowledgment)",
    ),
    412: OpenApiResponse(
        ProblemSerializer, description="If-Match didn't match the current revision (type /problems/precondition-failed)"
    ),
}


def file_response(media_type: str, description: str, code: int = 200) -> dict[Any, OpenApiResponse]:
    """A download's response for `@extend_schema(responses=...)`: the media type the view answers with and a binary
    body, so a generated client reads it as a file rather than as "no content"."""
    return {(code, media_type): OpenApiResponse(OpenApiTypes.BINARY, description=description)}


CSV_FILE = file_response("text/csv", "A CSV attachment (UTF-8).")


# ---- filtering ----------------------------------------------------------------------------------------------------

UNKNOWN_FILTER = "This filter isn't available here."


class ParamField(forms.Field):
    """A query parameter cleaned by `parse(value)`, which returns the value to filter on (None: no filter) or raises
    ValidationError with the message the API shows. An absent parameter is None; a present but empty one is parsed,
    so `parse` decides whether empty is an error (choices) or no filter (search text)."""

    def __init__(self, *, parse: Callable[[str], Any], choices: Any = None, **kwargs):
        kwargs.pop("required", None)
        super().__init__(required=False, **kwargs)
        self.parse = parse

    def clean(self, value):
        if value is None:
            return None
        return self.parse(value)


class ParamFilter(filters.CharFilter):
    """A django-filter filter with one parser for validation (and its exact message) and a method or lookup for SQL.
    Pass `choices` to document the allowed values in OpenAPI."""

    field_class = ParamField


def choice(values: Iterable[str]) -> Callable[[str], str]:
    allowed = list(values)

    def parse(value: str) -> str:
        if value not in allowed:
            raise forms.ValidationError(f"Use one of: {', '.join(allowed)}.")
        return value

    return parse


def choices_csv(values: Iterable[str]) -> Callable[[str], list[str]]:
    """Comma-separated values from a fixed list, deduplicated in order (lifecycle=active,deprecated)."""
    allowed = list(values)

    def parse(value: str) -> list[str]:
        wanted = [v.strip() for v in value.split(",")]
        if not all(v in allowed for v in wanted):
            raise forms.ValidationError(f"Use one or more of: {', '.join(allowed)} (comma-separated).")
        return list(dict.fromkeys(wanted))

    return parse


def uuid_value(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise forms.ValidationError("Use an ID from the list.") from None


BIGINT_MAX = 2**63 - 1


def user_id(value: str) -> int:
    """ASCII digits within the bigint key range ("²" passes str.isdigit() but int() refuses it)."""
    number = whole_number(value, 1, BIGINT_MAX)
    if number is None:
        raise forms.ValidationError("Use a user ID from the list.")
    return number


def stripped(value: str) -> str | None:
    """Text as sent, without surrounding whitespace; empty means no filter (search terms)."""
    return value.strip() or None


def text(value: str) -> str | None:
    """Free text: stripped, inner whitespace collapsed; empty means no filter."""
    value = " ".join(value.split())
    return value or None


class TenantFilterSet(filters.FilterSet):
    """Base FilterSet. Filters are `ParamFilter`s so their messages match the rest of the API; the backend refuses
    parameters the FilterSet doesn't declare. `self.request` carries the request organization."""

    @property
    def membership(self):
        return request_membership(self.request)


PAGING_PARAMS = frozenset({"cursor", "page_size"})


def filter_errors(filterset: Any, names: Iterable[str], allowed: Iterable[str] = ()) -> dict[str, list[str]]:
    """{parameter: [messages]} for the parameters a FilterSet doesn't declare (and `allowed` doesn't name) and the
    values it refuses. List endpoints, CSV export and saved filters all validate a query through this, so a query the
    list refuses is refused with the same messages everywhere."""
    known = set(filterset.filters if filterset is not None else ()) | set(allowed)
    errors = {name: [UNKNOWN_FILTER] for name in names if name not in known}
    if filterset is not None and not filterset.is_valid():
        errors.update({name: [str(m) for m in messages] for name, messages in filterset.errors.items()})
    return errors


def unknown_params(names: Iterable[str], allowed: Iterable[str] = ()) -> dict[str, list[str]]:
    """{parameter: [UNKNOWN_FILTER]} for each query parameter in `names` that `allowed` doesn't name. A view that
    reads its parameters itself merges this into its own field errors, so one 400 names every bad parameter."""
    known = set(allowed)
    return {name: [UNKNOWN_FILTER] for name in names if name not in known}


def refuse_unknown_params(request, allowed: Iterable[str] = ()) -> None:
    """400 naming every query parameter the read doesn't take: a typo (`?bye=vendor`, `?vendr=x`) is refused
    instead of silently answering the default or everything. Every GET that reads `query_params` itself, or takes
    none, calls this first; tests/test_unknown_params.py walks every GET route to hold that."""
    errors = unknown_params(request.query_params, allowed)
    if errors:
        raise FieldErrors(errors)


class FilterBackend(filters.DjangoFilterBackend):
    """django-filter, but with the API's problem shape: unknown parameters and invalid values are one 400 problem
    whose `errors` name each parameter (the FieldErrors shape every other endpoint uses). A list without a FilterSet
    takes no filters, and a list takes only the paging parameters its paginator names (`query_params`): none for a
    plain array, cursor and page_size for keyset pages, cursor alone for the audit log."""

    def filter_queryset(self, request, queryset, view):
        filterset = self.get_filterset(request, queryset, view)
        paginator = getattr(view, "paginator", None)
        allowed = {"ordering"} | set(getattr(paginator, "query_params", ()))
        errors = filter_errors(filterset, request.query_params, allowed)
        # ordering and page_size are checked again where they are used; here they join the same 400.
        checks: list[Callable[[], object]] = [lambda: KeysetPagination.ordering(request, view)]
        if isinstance(paginator, KeysetPagination):
            checks.append(lambda: KeysetPagination.page_size(request))
        for check in checks:
            try:
                check()
            except FieldErrors as e:
                errors.update(e.errors)
        if errors:
            raise FieldErrors(errors)
        return filterset.qs if filterset is not None else queryset


# ---- pagination and ordering ---------------------------------------------------------------------------------------


def set_location(request, response, path_info: str | None) -> None:
    """Location on a 201: the created record's own URL. `path_info` is resolved against the URLconf (so only a
    real route is named) and the URL is built with the request's script prefix, so an installation served under a
    sub-path still gets the address the client can use."""
    from django.urls import Resolver404, resolve

    if not path_info:
        return
    try:
        resolve(path_info)
    except Resolver404:
        return
    prefix = request.path.removesuffix(request.path_info) if request.path.endswith(request.path_info) else ""
    response["Location"] = request.build_absolute_uri(prefix + path_info)


PAGE_DEFAULT, PAGE_MAX = 50, 200
BAD_CURSOR = "This cursor isn't valid. Start again from the first page."


class KeysetPagination(BasePagination):
    """`{results, next, previous}`: opaque cursors that hold the direction, the ordering and the boundary row's
    ordering values. Keyset paging stays stable while rows are added or removed between requests, and the comparison
    runs in SQL with the ORDER BY's collation.

    Ordering: `?ordering=name` or `-created_at` (comma-separated for several), each from the view's
    `ordering_fields` whitelist; `id` always breaks ties. Without it the view's `default_ordering` applies. Ordered
    fields must be non-null columns or annotations, since keyset comparison skips NULLs."""

    query_params: ClassVar[frozenset[str]] = PAGING_PARAMS

    def __init__(self):
        self.next_cursor: str | None = None
        self.previous_cursor: str | None = None

    # -- request parsing

    @staticmethod
    def page_size(request) -> int:
        size = request.query_params.get("page_size")
        if size is None:
            return PAGE_DEFAULT
        number = whole_number(size, 1, PAGE_MAX)  # above the maximum is refused, never clamped
        if number is not None:
            return number
        raise FieldErrors({"page_size": [f"Use a whole number from 1 to {PAGE_MAX}."]})

    @staticmethod
    def ordering(request, view) -> list[tuple[str, bool]]:
        """[(field, descending)], ending with id."""
        raw = request.query_params.get("ordering")
        allowed = tuple(getattr(view, "ordering_fields", ()))
        if raw is None:
            keys = [(f.lstrip("-"), f.startswith("-")) for f in getattr(view, "default_ordering", ("name",))]
        else:
            wanted = [part.strip() for part in raw.split(",")]
            if not wanted or not all(w.lstrip("-") in allowed for w in wanted):
                options = ", ".join(allowed) or "none"
                raise FieldErrors({"ordering": [f"Order by one of: {options} (prefix - for descending)."]})
            keys = [(w.lstrip("-"), w.startswith("-")) for w in dict.fromkeys(wanted)]
        keys = [(f, desc) for f, desc in keys if f != "id"]
        return [*keys, ("id", keys[-1][1] if keys else False)]

    @staticmethod
    def ranked(queryset, keys: list[tuple[str, bool]], view):
        """Sort fields whose values have a meaningful order (`view.ordering_ranks`: field -> values in order) by that
        rank instead of alphabetically: an annotation `<field>_rank` replaces the field in the keys, so cursors carry
        the rank. A value not in the list (blank criticality) sorts last in both directions."""
        ranks: Mapping[str, Iterable[str]] = getattr(view, "ordering_ranks", None) or {}
        out = []
        for name, desc in keys:
            if name in ranks:
                order = list(ranks[name])
                alias = f"{name}_rank"
                rank = Case(
                    *[When(**{name: value}, then=Value(i)) for i, value in enumerate(order)],
                    default=Value(-1 if desc else len(order)),
                    output_field=IntegerField(),
                )
                queryset = queryset.annotate(**{alias: rank})
                name = alias
            out.append((name, desc))
        return queryset, out

    # -- cursors

    @staticmethod
    def _signature(keys: list[tuple[str, bool]]) -> str:
        return ",".join(("-" if desc else "") + f for f, desc in keys)

    @staticmethod
    def _plain(value: Any) -> Any:
        if isinstance(value, datetime.datetime | datetime.date | uuid.UUID):
            return str(value)
        return value

    def _encode(self, direction: str, keys, row) -> str:
        values = [self._plain(getattr(row, f)) for f, _ in keys]
        raw = json.dumps([direction, self._signature(keys), *values]).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _field(queryset, name):
        annotation = queryset.query.annotations.get(name)
        return annotation.output_field if annotation is not None else queryset.model._meta.get_field(name)

    def _decode(self, cursor: str, keys, queryset) -> tuple[str, list[Any]]:
        """The direction and the boundary values, each parsed by its ordering field. A value must read back exactly
        as this class writes it, so a tampered cursor (a text rank, a number for a name, a bad date) is the cursor
        problem rather than a database error."""
        try:
            direction, signature, *values = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if direction not in ("a", "b") or signature != self._signature(keys) or len(values) != len(keys):
                raise ValueError
            parsed = []
            for (name, _), value in zip(keys, values, strict=True):
                if not isinstance(value, str | int) or isinstance(value, bool):
                    raise ValueError
                typed = self._field(queryset, name).to_python(value)
                if typed is None or self._plain(typed) != value:
                    raise ValueError
                parsed.append(typed)
            return direction, parsed
        except (ValueError, TypeError, AttributeError, OverflowError, DjangoValidationError, FieldDoesNotExist):
            raise FieldErrors({"cursor": [BAD_CURSOR]}) from None

    @staticmethod
    def _beyond(keys, values, forward: bool) -> Q:
        """Rows after (forward) or before the boundary in the ordering: a lexicographic comparison over the keys."""
        match = Q()
        equal: dict[str, Any] = {}
        for (name, desc), value in zip(keys, values, strict=True):
            lookup = "gt" if forward != desc else "lt"
            match |= Q(**equal, **{f"{name}__{lookup}": value})
            equal[name] = value
        return match

    @staticmethod
    def _order_by(keys, reverse: bool = False) -> list[str]:
        return [("-" if desc != reverse else "") + name for name, desc in keys]

    def paginate_queryset(self, queryset, request, view=None):
        size = self.page_size(request)
        queryset, keys = self.ranked(queryset, self.ordering(request, view), view)
        cursor = request.query_params.get("cursor") or None
        if cursor is None:
            rows = list(queryset.order_by(*self._order_by(keys))[: size + 1])
            more, rows = len(rows) > size, rows[:size]
            self.next_cursor = self._encode("a", keys, rows[-1]) if more else None
            self.previous_cursor = None
            return rows
        direction, values = self._decode(cursor, keys, queryset)
        if direction == "a":
            rows = list(queryset.filter(self._beyond(keys, values, True)).order_by(*self._order_by(keys))[: size + 1])
            more, rows = len(rows) > size, rows[:size]
            self.next_cursor = self._encode("a", keys, rows[-1]) if more else None
            self.previous_cursor = self._encode("b", keys, rows[0]) if rows else None
            return rows
        ordered = queryset.filter(self._beyond(keys, values, False)).order_by(*self._order_by(keys, reverse=True))
        rows = list(ordered[: size + 1])
        more, rows = len(rows) > size, rows[:size][::-1]
        self.next_cursor = self._encode("a", keys, rows[-1]) if rows else None
        self.previous_cursor = self._encode("b", keys, rows[0]) if more else None
        return rows

    def get_paginated_response(self, data):
        return Response({"results": data, "next": self.next_cursor, "previous": self.previous_cursor})

    def get_paginated_response_schema(self, schema):
        return {
            "type": "object",
            "required": ["results", "next", "previous"],
            "properties": {
                "results": schema,
                "next": {"type": "string", "nullable": True, "description": "Pass back as ?cursor= for the next page."},
                "previous": {"type": "string", "nullable": True, "description": "Pass back as ?cursor= to go back."},
            },
        }

    def get_schema_operation_parameters(self, view):
        allowed = list(getattr(view, "ordering_fields", ()))
        return [
            {
                "name": "cursor",
                "in": "query",
                "required": False,
                "description": "The next or previous value from a page.",
                "schema": {"type": "string"},
            },
            {
                "name": "page_size",
                "in": "query",
                "required": False,
                "description": f"Default {PAGE_DEFAULT}, at most {PAGE_MAX}.",
                "schema": {"type": "integer", "minimum": 1, "maximum": PAGE_MAX},
            },
            {
                "name": "ordering",
                "in": "query",
                "required": False,
                "description": f"One or more of {', '.join(allowed) or 'none'}, comma-separated; prefix - for "
                "descending. Default: relevance when searching, then name. lifecycle sorts in lifecycle order "
                "(proposed to retired) and criticality from critical to low; blank values sort last either way.",
                "schema": {"type": "string"},
            },
        ]


# ---- OpenAPI ------------------------------------------------------------------------------------------------------


class TenantAutoSchema(AutoSchema):
    """Fills in what every TenantViewSet operation shares: the input serializer as the request body, problem
    responses, and If-Match on PATCH and DELETE. An `@extend_schema` on the method still wins where it sets a value."""

    # Every operation documents 400, 403 and 404 (a filter or body naming another organization's record is a 404,
    # as is a nested route under one); writes add 409, and the If-Match writes 412.
    PROBLEM_CODES: ClassVar[dict[str, tuple[int, ...]]] = {
        "list": (400, 403, 404),
        "retrieve": (400, 403, 404),
        "create": (400, 403, 404, 409),
        "partial_update": (400, 403, 404, 409, 412),
        "destroy": (400, 403, 404, 409, 412),
    }

    def _is_the_list(self) -> bool:
        return getattr(self.view, "action", None) == "list"

    # Only the viewset's own list is filtered and paged (TenantViewSet.list). Another action that answers an array (a
    # link's confirmations, a record's snapshots) is a plain array that takes none of the list's filters.

    def get_filter_backends(self):
        return super().get_filter_backends() if self._is_the_list() else []

    def _get_paginator(self):
        return super()._get_paginator() if self._is_the_list() else None

    def _is_list_view(self, serializer=None):
        # A list documented with its page serializer ({results, next, ...}, such as the audit log's) is the page
        # already: wrapping it again as an array or a page would describe a body the API never sends.
        if serializer is not None and not isinstance(serializer, dict):
            instance = serializer() if isinstance(serializer, type) else serializer
            if isinstance(instance, serializers.Serializer) and "results" in instance.fields:
                return False
        return super()._is_list_view(serializer)

    def get_request_serializer(self):
        action = getattr(self.view, "action", None)
        documented = getattr(self.view, "input_serializer_class", None)
        if action in ("create", "partial_update") and documented is not None:
            return documented
        return super().get_request_serializer()

    def get_response_serializers(self):
        found = super().get_response_serializers()
        if isinstance(found, dict):
            return found
        action = getattr(self.view, "action", None)
        if action == "destroy":
            ok: dict[int, Any] = {204: None}
        else:
            ok = {201 if action == "create" else 200: found}
        codes = self.PROBLEM_CODES.get(action or "", (400, 403, 404, 409))
        if action in getattr(self.view, "if_match_actions", ()):
            codes = (*codes, 412)
        return {**ok, **{code: PROBLEMS[code] for code in codes}}

    def get_override_parameters(self):
        params = list(super().get_override_parameters())
        action = getattr(self.view, "action", None)
        if action in ("partial_update", "destroy") or action in getattr(self.view, "if_match_actions", ()):
            params.append(IF_MATCH)
        ordering = tuple(getattr(self.view, "ordering_fields", ()))
        if action == "list" and getattr(self.view, "pagination_class", None) is None and ordering:
            params.append(
                OpenApiParameter(
                    "ordering",
                    str,
                    description=f"One or more of {', '.join(ordering)}, comma-separated; prefix - for descending.",
                )
            )
        return params


# ---- the base viewset ----------------------------------------------------------------------------------------------


class TenantViewSet(viewsets.GenericViewSet):
    """Base for every model viewset. Subclasses set `role_policy`, `serializer_class` (list rows and the create
    response), optionally `detail_serializer_class` (retrieve and update responses), `input_serializer_class`
    (documents the request body; the service validates), `filterset_class`, `ordering_fields`, `default_ordering`,
    and implement `base_queryset()` and the service hooks for the writes they allow. Routes map only the actions a
    viewset supports (`as_view({...})` in the app's urls.py), so an unmapped write is a 405.

    `pagination_class = None` makes the list a plain array (the shape the SPA has always read for short lists such as
    people and teams), still filtered and ordered. An action other than PATCH and DELETE that changes the record (a
    renewal's cycle) honors If-Match too when it is listed in `if_match_actions` and calls `check_if_match`."""

    permission_classes = [RolePermission]
    role_policy: ClassVar[RolePolicy] = policy(VIEWER, EDITOR, destroy=ADMIN)
    required_capability: ClassVar[str | None] = None
    pagination_class: type[BasePagination] | None = KeysetPagination
    filter_backends = [FilterBackend]
    schema = TenantAutoSchema()
    ordering_fields: ClassVar[tuple[str, ...]] = ("name", "created_at", "updated_at")
    default_ordering: ClassVar[tuple[str, ...]] = ("name",)
    detail_serializer_class: ClassVar[type[serializers.BaseSerializer] | None] = None
    input_serializer_class: ClassVar[type[serializers.BaseSerializer] | None] = None
    if_match_actions: ClassVar[tuple[str, ...]] = ()
    list_limit: ClassVar[int | None] = None  # an unpaginated list's cap (the digest log shows the newest 200)
    lookup_field = "pk"

    # -- organization and queryset

    @property
    def membership(self):
        return request_membership(self.request)

    @property
    def organization(self):
        return self.membership.organization

    def base_queryset(self):
        raise NotImplementedError

    def get_queryset(self):
        qs = self.base_queryset()
        if getattr(self, "swagger_fake_view", False):  # OpenAPI generation: no request organization
            return qs.none()
        return qs.filter(organization=self.organization)

    def get_object(self):
        """By ID within the organization only: list filters (such as hiding archived records) never apply to a
        detail route, and another organization's ID is a 404."""
        lookup = self.lookup_url_kwarg or self.lookup_field
        obj = get_object_or_404(self.get_queryset(), pk=self.kwargs[lookup])
        self.check_object_permissions(self.request, obj)
        return obj

    def get_serializer_class(self):
        if self.action in ("list", "create"):
            return self.serializer_class
        return self.detail_serializer_class or self.serializer_class

    # -- rendering

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        """Extra serializer context computed once for a page (or one record), e.g. latest reviews in one query."""
        return {}

    def render(self, rows, *, many: bool, serializer_class=None) -> Any:
        rows = list(rows) if many else [rows]
        if not rows:
            return []
        cls = serializer_class or self.get_serializer_class()
        context = {**self.get_serializer_context(), **self.page_context(rows)}
        data = cls(rows, many=True, context=context).data
        return data if many else data[0]

    def revision_of(self, obj) -> str:
        return revision(obj)

    def detail_response(self, obj, *, status_code=200, serializer_class=None, location: str | None = None) -> Response:
        """`location`: the created record's path (path_info form, "/api/v1/contracts/<id>") for a 201 that isn't the
        view's own `create`; a `create` answers with the collection's path plus the new ID."""
        response = Response(self.render(obj, many=False, serializer_class=serializer_class), status=status_code)
        response["ETag"] = etag(self.revision_of(obj))
        if status_code == status.HTTP_201_CREATED:
            if location is None and getattr(self, "action", None) == "create":
                location = f"{self.request.path_info.rstrip('/')}/{obj.pk}"
            set_location(self.request, response, location)
        return response

    def fresh(self, obj):
        return self.get_queryset().get(pk=obj.pk)

    def payload(self) -> dict[str, Any]:
        data = self.request.data
        if not isinstance(data, dict):
            raise FieldErrors({"non_field": ["Send a JSON object."]})
        return dict(data)

    # -- If-Match

    def check_if_match(self, obj):
        """Under a row lock held until the write's transaction commits, compare If-Match with the stored revision.
        Returns the record as stored now (fetched after the lock), or raises PreconditionFailed."""
        if self.request.headers.get("If-Match") is None:
            if if_match_required(self.request):
                raise PreconditionRequired()
            return obj
        model = type(obj)
        list(model._base_manager.select_for_update().filter(pk=obj.pk).values_list("pk", flat=True))
        current_obj = self.fresh(obj)
        require_match(self.request, self.revision_of(current_obj))
        return current_obj

    # -- service hooks: validate, scope to self.organization and audit in one transaction

    def create_object(self, data: dict[str, Any]):
        raise exceptions.MethodNotAllowed(str(self.request.method))

    def update_object(self, obj, data: dict[str, Any]):
        raise exceptions.MethodNotAllowed(str(self.request.method))

    def destroy_object(self, obj) -> None:
        raise exceptions.MethodNotAllowed(str(self.request.method))

    # -- actions

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        if self.paginator is None:
            queryset, keys = KeysetPagination.ranked(queryset, KeysetPagination.ordering(request, self), self)
            ordered = queryset.order_by(*KeysetPagination._order_by(keys))
            return Response(self.render(ordered[: self.list_limit] if self.list_limit else ordered, many=True))
        rows = self.paginate_queryset(queryset)
        return self.get_paginated_response(self.render(rows, many=True))

    def retrieve(self, request, *args, **kwargs):
        refuse_unknown_params(request)  # a record read takes no parameters
        return self.detail_response(self.get_object())

    # The response is built inside the write's transaction: if rendering the stored record fails, the request
    # answers 500 and has written nothing, rather than storing a row the client never got an id for.

    def create(self, request, *args, **kwargs):
        data = self.payload()
        with transaction.atomic():
            obj = self.create_object(data)
            return self.detail_response(
                self.fresh(obj), status_code=status.HTTP_201_CREATED, serializer_class=self.serializer_class
            )

    def partial_update(self, request, *args, **kwargs):
        data = self.payload()
        with transaction.atomic():
            obj = self.check_if_match(self.get_object())
            obj = self.update_object(obj, data)
            return self.detail_response(self.fresh(obj))

    def destroy(self, request, *args, **kwargs):
        with transaction.atomic():
            obj = self.check_if_match(self.get_object())
            self.destroy_object(obj)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---- composite endpoints ------------------------------------------------------------------------------------------


class RoleAPIView(APIView):
    """Base for an endpoint that is not one model's rows: Needs attention, the dependency explorer, bulk edit, your
    own preferences, a renewal deadline preview. It declares `role_policy` like a viewset; serializers describe its
    request and response in OpenAPI, and every write still goes through a service function."""

    permission_classes = [RolePermission]
    role_policy: ClassVar[RolePolicy]

    @property
    def membership(self):
        return request_membership(self.request)

    @property
    def organization(self):
        return self.membership.organization

    def payload(self) -> dict[str, Any]:
        data = self.request.data
        if not isinstance(data, dict):
            raise FieldErrors({"non_field": ["Send a JSON object."]})
        return dict(data)
