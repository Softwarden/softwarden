"""Members of the organization, organization settings and your password (multi-organization
membership). Routes are in catalog/urls.py with the URLs and response shapes of the function views they replaced;
catalog/admin_services.py and core/oidc.py validate, scope and audit every write.

Members join by accepting an invitation (catalog/access_viewsets.py); `POST users` makes one, as `POST invitations`
does. Roles: members, their passwords and sign-in identities are Admin-only; anyone in the organization reads its settings and
only Admins change them; any signed-in account changes its own password."""

from typing import Any

from django import forms
from django.contrib.auth import update_session_auth_hash
from django.db import transaction
from django.db.models import F
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import exceptions, status
from rest_framework.decorators import action
from rest_framework.response import Response

from core import limits, oidc
from core.drf import (
    ACCOUNT,
    ADMIN,
    IF_MATCH,
    PROBLEMS,
    SIGN_IN_AUTHORITY,
    VIEWER,
    ParamFilter,
    PreconditionRequired,
    ProblemSerializer,
    RoleAPIView,
    TenantFilterSet,
    TenantViewSet,
    etag,
    if_match_required,
    policy,
    refuse_unknown_params,
    require_match,
    require_session,
    revision,
)
from core.impersonation_routes import RESTRICTED_SETTINGS
from core.models import Membership, Organization, Role, UserIdentity

from . import admin_services, invitations
from .account_serializers import (
    InvitationCreatedSerializer,
    InviteInputSerializer,
    LimitsSerializer,
    MemberInputSerializer,
    MemberSerializer,
    MemberWithPasswordSerializer,
    OrganizationInputSerializer,
    OrganizationSerializer,
    PasswordInputSerializer,
)
from .filters import TRUE_FALSE, worded

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409)}


def _one_of(values):
    def parse(value: str) -> str:
        if value not in values:
            raise forms.ValidationError("not one of the values")
        return value

    return parse


def member_context(user_ids) -> dict[str, Any]:
    """What MemberSerializer needs for these users, in three queries: shared accounts, forced password changes and
    the sign-in identities."""
    ids = list(user_ids)
    shared, must_change = admin_services.account_facts(ids)
    identities: dict[int, list[UserIdentity]] = {}
    for identity in UserIdentity.objects.filter(user_id__in=ids).order_by("issuer", "id"):
        identities.setdefault(identity.user_id, []).append(identity)
    return {"shared": shared, "must_change": must_change, "linked": set(identities), "identities": identities}


def member_body(membership) -> dict[str, Any]:
    membership = Membership.objects.select_related("user").get(pk=membership.pk)
    return dict(MemberSerializer(membership, context=member_context([membership.user_id])).data)


class MemberFilterSet(TenantFilterSet):
    role = ParamFilter(
        parse=worded(_one_of(Role.values), "Choose admin, editor or viewer."), field_name="role", choices=Role.choices
    )
    active = ParamFilter(
        parse=worded(_one_of(TRUE_FALSE), "Use true or false."),
        method="filter_active",
        help_text="true: active accounts only; false: deactivated ones.",
    )

    class Meta:
        model = Membership
        fields: list[str] = []

    def filter_active(self, qs, name, value):
        return qs.filter(user__is_active=value == "true")


@extend_schema_view(
    list=extend_schema(summary="Members of the organization (Admin), by email"),
    create=extend_schema(
        summary="Invite someone by email (Admin, signed-in session); the same as POST invitations",
        description="Returns the invitation with its link, in this response only. Nobody is added until they accept "
        "through the link (a new account sets its name and password; an existing account accepts while signed in). "
        "An API token is refused (403): an invitation issues sign-in authority.",
        request=InviteInputSerializer,
        responses={201: InvitationCreatedSerializer, **WRITE_ERRORS},
    ),
    retrieve=extend_schema(summary="Get a member (Admin)"),
    partial_update=extend_schema(
        summary="Change a member's role or active flag (Admin, signed-in session)",
        description="An API token is refused (403). Demoting, deactivating or removing an Admin revokes the API tokens "
        "they made for others and the invitations they made that are still open.",
    ),
    destroy=extend_schema(
        summary="Remove someone from this organization only (Admin)",
        description="Their account and other memberships stay, and their API tokens here are revoked. Refused for "
        "their only organization (deactivate them instead), the last Admin and the IT coordinator.",
    ),
    reset_password=extend_schema(
        summary="Reset a member's password to a temporary one (Admin, signed-in session)",
        description="The temporary password is in this response only; the member must change it at their next "
        "sign-in, and their calendar feeds are revoked. Refused (409) for your own account (change your password "
        "instead), an account shared with another organization and an account with no password. An API token is "
        "refused (403).",
        request=None,
        responses={200: MemberWithPasswordSerializer, **WRITE_ERRORS},
    ),
    identity=extend_schema(
        summary="Unlink all of a member's single sign-on identities (Admin); the next sign-in matches by email again",
        request=None,
        responses={204: None, **WRITE_ERRORS},
    ),
    unlink_identity=extend_schema(
        summary="Unlink one of a member's single sign-on identities (Admin)",
        description="The identity IDs are in the member's `identities`. The next sign-in from that provider matches "
        "by email again. Refused (409) for an account that also belongs to another organization.",
        request=None,
        responses={204: None, **WRITE_ERRORS},
    ),
)
class UserViewSet(TenantViewSet):
    """The organization's memberships, addressed by user ID. Account-level changes (password, active flag, Entra
    identity) are refused with 409 for an account that also belongs to another organization."""

    role_policy = policy(ADMIN)
    pagination_class = None
    serializer_class = MemberSerializer
    input_serializer_class = MemberInputSerializer
    filterset_class = MemberFilterSet
    ordering_fields = ("email", "role", "created_at")
    default_ordering = ("email",)

    def base_queryset(self):
        return Membership.objects.select_related("user").annotate(email=F("user__email"))

    def get_object(self):
        obj = get_object_or_404(self.get_queryset(), user_id=self.kwargs["pk"])
        self.check_object_permissions(self.request, obj)
        return obj

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        return member_context(m.user_id for m in rows)

    def revision_of(self, obj) -> str:
        user = obj.user
        return revision(obj, extra={"active": user.is_active, "email": user.email, "name": user.get_full_name()})

    def check_if_match(self, obj):
        """The account-change locks first, in the service's order (organization, then the user's memberships), so
        an If-Match writer queues with update_member and remove_member instead of deadlocking with them."""
        if self.request.headers.get("If-Match") is None:
            if if_match_required(self.request):
                raise PreconditionRequired()
            return obj
        admin_services.lock_account_changes(self.organization, obj)
        current = self.fresh(obj)
        require_match(self.request, self.revision_of(current))
        return current

    def with_password(self, membership, password, status_code=200):
        obj = self.fresh(membership)
        obj.temporary_password = password
        return self.detail_response(obj, status_code=status_code, serializer_class=MemberWithPasswordSerializer)

    def create(self, request, *args, **kwargs):
        from .access_viewsets import created_response

        data = self.payload()
        with transaction.atomic():
            invitation, token = invitations.create_invitation(self.organization, request.user, data, emailing=True)
            return created_response(invitation, token, status.HTTP_201_CREATED, request)

    def update_object(self, obj, data):
        return admin_services.update_member(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        admin_services.remove_member(self.organization, self.request.user, obj)

    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        with transaction.atomic():
            membership = self.get_object()
            password = admin_services.reset_password(self.organization, request.user, membership)
            return self.with_password(membership, password)

    @action(detail=True, methods=["delete"])
    def identity(self, request, pk=None):
        with transaction.atomic():  # core/oidc.unlink deletes and audits in one transaction
            oidc.unlink(self.organization, request.user, self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["delete"], url_path="identities")
    def unlink_identity(self, request, pk=None, identity_id=None):
        with transaction.atomic():
            invitations.unlink_identity(self.organization, request.user, self.get_object(), identity_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---- organization settings ----------------------------------------------------------------------------------------

# The licence key slot is never an ETag input; its _set_at column still moves the revision when it changes.
LICENCE_FIELDS = ("licence_key_ciphertext", "licence_key_key_id")


def organization_revision(org) -> str:
    return revision(org, skip=LICENCE_FIELDS)


class OrganizationView(RoleAPIView):
    """The current organization's settings. PATCH saves only the settings columns (admin_services
    .ORGANIZATION_SETTINGS); edition, slug and the license key are operator-only (manage.py set_edition)."""

    role_policy = policy(VIEWER, ADMIN)

    def respond(self, org) -> Response:
        response = Response(OrganizationSerializer(org, context={"role": self.membership.role}).data)
        response["ETag"] = etag(organization_revision(org))
        return response

    @extend_schema(summary="Organization settings", responses={200: OrganizationSerializer, **ERRORS})
    def get(self, request):
        refuse_unknown_params(request)
        return self.respond(self.organization)

    @extend_schema(
        summary="Change organization settings (Admin)",
        description="A body with a sign-in setting (sso_auto_provision, scim_default_role, scim_role_groups) needs a "
        "signed-in session: an API token is refused (403) and nothing is saved. Other settings work with a token.",
        parameters=[IF_MATCH],
        request=OrganizationInputSerializer,
        responses={200: OrganizationSerializer, **WRITE_ERRORS, 412: PROBLEMS[412]},
    )
    def patch(self, request):
        data = self.payload()
        # The sign-in settings (auto-provisioning, SCIM's roles; the keys impersonation refuses too) change who
        # can sign in, so a body with any of them needs a signed-in session; the whole body is refused, nothing saved.
        if RESTRICTED_SETTINGS & set(data):
            require_session(request, SIGN_IN_AUTHORITY)
        with transaction.atomic():
            org = (
                Organization.objects.select_for_update(of=("self",))
                .select_related("coordinator")
                .get(pk=self.organization.pk)
            )
            require_match(request, organization_revision(org))
            admin_services.update_organization(org, request.user, data)
            return self.respond(Organization.objects.select_related("coordinator").get(pk=org.pk))


class OrganizationLimitsView(RoleAPIView):
    """The organization's limits and how much of each is used (core/limits.py): any member reads them, for
    the meters in Settings. Not sensitive: counts of the organization's own records, connectors, members and files."""

    role_policy = policy(VIEWER, VIEWER)

    @extend_schema(
        summary="Organization limits and usage",
        description="Each limit (records, connectors, members, attachment_bytes, api_rate_read, api_rate_write) with "
        "its value now (null: unlimited), the edition's default, whether the operator overrode it, and the usage "
        "counted for this response (null for the API rates). A create that would pass a limit is refused with 409 "
        "/problems/limit-reached; updates and deletes never are.",
        responses={200: LimitsSerializer, 403: PROBLEMS[403]},
    )
    def get(self, request):
        refuse_unknown_params(request)
        return Response(LimitsSerializer({"limits": limits.report(self.organization)}).data)


# ---- your password --------------------------------------------------------------------------------------------------


class PasswordView(RoleAPIView):
    """Any signed-in account, including one that must change its password before anything else."""

    role_policy = policy(ACCOUNT, ACCOUNT)

    @extend_schema(
        summary="Change your password (signed-in session)",
        description="Clears the forced change in every organization, revokes your calendar feeds and keeps this "
        "session signed in. The current password counts with your account's sign-in attempts: past five wrong ones "
        "each attempt must wait longer (429 with Retry-After), and a wrong one is audited. An API token is refused "
        "(403).",
        request=PasswordInputSerializer,
        responses={
            204: None,
            **{c: PROBLEMS[c] for c in (400, 403)},
            429: OpenApiResponse(
                ProblemSerializer,
                description="Too many wrong current passwords for this account, or rate limited per user (type "
                "/problems/rate-limited); Retry-After says when to retry",
            ),
        },
    )
    def post(self, request):
        data = self.payload()
        if not Membership.objects.filter(user=request.user).exists():
            raise exceptions.PermissionDenied("Your account isn't part of an organization.")
        # The session update is inside the password's transaction (`then`): when another request changed the
        # session's key after this one read it, the key change in update_session_auth_hash raises SessionInterrupted
        # (400, core/sessions.py) and the password stays as it was. The current password is checked before that, in a
        # transaction of its own, so a wrong one stays counted. Never with an API token.
        admin_services.change_password(
            request.user,
            data.get("current_password"),
            data.get("new_password"),
            then=lambda: update_session_auth_hash(request, request.user),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
