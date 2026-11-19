"""API tokens for Admins: list, create, read and revoke. Tokens are managed from a signed-in
session, never with a token: a request authenticated by an API token is refused here, after the read-only and role
checks every route makes. api/tokens.py creates, revokes and audits.

A token's secret is in the create response only; its hash is never in a response, ETag or audit event. Tokens are
revoked, not deleted or edited, so the audit log keeps naming them."""

from datetime import date

from django.db import transaction
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action

from core.drf import ADMIN, PROBLEMS, SessionOnly, TenantViewSet, policy, revision
from core.input import whole_number
from core.models import Membership
from core.problems import FieldErrors

from . import tokens
from .models import ApiToken
from .serializers import ApiTokenCreatedSerializer, ApiTokenInputSerializer, ApiTokenSerializer

SESSION_ONLY = "Manage API tokens from a signed-in session, not with a token."
# The hash is never an ETag input; last_used_at stays in, since it is shown.
SECRET_FIELDS = ("token_hash",)


def parse_token(org, data) -> tuple[Membership, str, bool, date]:
    """(owner membership, name, read_only, expires_on) from a create request, or 400 naming each field."""
    errors: dict[str, list[str]] = {}
    owner = None
    raw_user = whole_number(data.get("user"), 1)
    if raw_user is None:
        errors["user"] = ["Choose a member of the organization."]
    else:
        owner = (
            Membership.objects.select_related("user")
            .filter(organization=org, user_id=raw_user, user__is_active=True)
            .first()
        )
        if owner is None:
            errors["user"] = ["Choose an active member of the organization."]
    name = data.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        errors["name"] = ["Enter a name."]
    elif len(name) > 100:
        errors["name"] = ["Use 100 characters or fewer."]
    read_only = data.get("read_only", True)
    if not isinstance(read_only, bool):
        errors["read_only"] = ["Use true or false."]
    expires_on = None
    raw_date = data.get("expires_on")
    if not isinstance(raw_date, str):
        errors["expires_on"] = ["Choose an expiry date."]
    else:
        try:
            expires_on = date.fromisoformat(raw_date)
        except ValueError:
            errors["expires_on"] = ["Use a date like 2027-01-31."]
    if expires_on is not None:
        earliest, latest = tokens.expiry_window(org)
        if expires_on < earliest:
            errors["expires_on"] = ["Choose a date after today."]
        elif expires_on > latest:
            errors["expires_on"] = [f"Choose a date within {tokens.MAX_LIFETIME_DAYS} days."]
    if errors or owner is None or expires_on is None:
        raise FieldErrors(errors)
    return owner, name, read_only, expires_on


@extend_schema_view(
    list=extend_schema(summary="List API tokens (Admin, signed-in session)"),
    create=extend_schema(
        summary="Create an API token (Admin, signed-in session)",
        description="The secret is in this response only.",
        responses={201: ApiTokenCreatedSerializer, **{c: PROBLEMS[c] for c in (400, 403, 404, 409)}},
    ),
    retrieve=extend_schema(summary="Get an API token (Admin, signed-in session); never its secret"),
    revoke=extend_schema(
        summary="Revoke an API token (Admin, signed-in session)",
        request=None,
        responses={200: ApiTokenSerializer, **{c: PROBLEMS[c] for c in (400, 403, 404)}},
    ),
)
class ApiTokenViewSet(SessionOnly, TenantViewSet):
    role_policy = policy(ADMIN)
    session_only_detail = SESSION_ONLY
    pagination_class = None
    serializer_class = ApiTokenSerializer
    input_serializer_class = ApiTokenInputSerializer
    ordering_fields = ("created_at", "name", "expires_on")
    default_ordering = ("-created_at",)

    def base_queryset(self):
        return ApiToken.objects.select_related("user", "created_by")

    def revision_of(self, obj) -> str:
        return revision(obj, skip=SECRET_FIELDS)

    def create(self, request, *args, **kwargs):
        owner, name, read_only, expires_on = parse_token(self.organization, self.payload())
        with transaction.atomic():
            token, secret = tokens.create_token(self.organization, request.user, owner, name, read_only, expires_on)
            response = self.detail_response(self.fresh(token), status_code=status.HTTP_201_CREATED)
        response.data = {**response.data, "secret": secret}
        return response

    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        with transaction.atomic():
            token = self.get_object()
            tokens.revoke_token(self.organization, request.user, token)
            return self.detail_response(self.fresh(token))
