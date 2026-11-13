"""The platform admin console's API (services in catalog/platform_admin.py). Every route is `platform`:
a signed-in session of platform staff, never an API token (core/platform_access.py). Routed under /api/v1/platform/
(catalog/platform_urls.py):

    GET    platform/session                                         you as staff, and your impersonation
    GET    platform/organizations                                   organizations with usage (keyset pages)
    GET    platform/organizations/{id}                              one organization: members, capabilities, usage
    POST   platform/organizations/{id}/edition                      {edition, reason}
    POST   platform/organizations/{id}/cancel-deletion              {reason}
    POST   platform/organizations/{id}/suspend                      {reason}
    POST   platform/organizations/{id}/unsuspend                    {reason}
    GET    platform/organizations/{id}/capability-overrides         the organization's feature flags
    POST   platform/organizations/{id}/capability-overrides         {capability, effect, reason, expires_at?}
    POST   platform/organizations/{id}/capability-overrides/{oid}/remove   {reason}
    GET    platform/organizations/{id}/limits                       limits, usage and overrides
    POST   platform/organizations/{id}/limits                       {limit, value (null: unlimited), reason, expires_at?}
    POST   platform/organizations/{id}/limits/{name}/remove         {reason}
    POST   platform/organizations/{id}/exports                      {reason}: an export the organization asked for
    GET    platform/audit-events                                    the platform audit stream (keyset pages)
    GET    platform/capabilities                                    every capability name and the editions that have it
    GET    platform/system                                          the installation's System status
    POST   platform/system/backup/acknowledge-key                   the generated backup key is copied off the host
    GET    platform/impersonation                                   the session's impersonation, if any
    POST   platform/impersonation                                   start {organization_id, user_id, reason, password}
    DELETE platform/impersonation                                   stop

Responses never carry secrets: no credential ciphertexts, token hashes, password hashes, connector configuration or
session values. Only the session, and the impersonation's own status and stop, answer while impersonating. An
export made here is downloadable only by the organization's own Admins: the platform API has no download."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_field
from rest_framework import exceptions, serializers, status
from rest_framework.response import Response

from core import backup, capabilities, limits, signin_limits
from core.drf import (
    PLATFORM,
    PROBLEMS,
    KeysetPagination,
    ProblemSerializer,
    ReadOnlySerializer,
    RoleAPIView,
    UserBriefSerializer,
    policy,
    refuse_unknown_params,
    timestamp,
)
from core.input import whole_number
from core.models import (
    CapabilityOverride,
    Edition,
    Membership,
    Organization,
    OrganizationLimitOverride,
    OrganizationUsage,
    OverrideEffect,
    PlatformAuditEvent,
    Role,
)
from core.platform_access import impersonator, real_user
from core.problems import Conflict, FieldErrors, ImpersonationRestricted
from core.system_views import BackupStatusSerializer, SystemStatusSerializer, installation_status
from core.throttling import ScopedThrottle, with_api_limits

from . import org_lifecycle, platform_admin, staff_second_factor, usage
from .account_serializers import LimitSerializer
from .lifecycle_views import OrgExportSerializer

ERRORS = {c: PROBLEMS[c] for c in (403, 404)}
WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409)}
MEMBERS_SHOWN = 500
RECENT_EVENTS = 20
REASON_HELP = "Why, in a sentence; the organization's Admins see it in their audit log. Up to 500 characters."


def iso(value) -> str | None:
    return timestamp(value)


# ---- serializers ----------------------------------------------------------------------------------------------------


class UsageCountsSerializer(serializers.Serializer):
    records = serializers.DictField(child=serializers.IntegerField(), help_text="Rows per record kind.")
    records_total = serializers.IntegerField()
    members = serializers.IntegerField()
    active_members = serializers.IntegerField()
    connectors = serializers.IntegerField()
    api_tokens = serializers.IntegerField(help_text="Not revoked and not expired.")
    attachments = serializers.IntegerField()
    attachment_bytes = serializers.IntegerField()
    exports = serializers.IntegerField()
    jobs_30d = serializers.IntegerField(help_text="Background jobs finished in the last 30 days.")


class UsageSerializer(serializers.Serializer):
    computed_at = serializers.DateTimeField(help_text="When these counts were taken (the hourly snapshot, or now).")
    counts = UsageCountsSerializer()
    last_activity_at = serializers.DateTimeField(allow_null=True, help_text="The newest audit event.")
    last_sign_in_at = serializers.DateTimeField(allow_null=True, help_text="The newest member sign-in.")


class DeletionStateSerializer(serializers.Serializer):
    requested_at = serializers.DateTimeField(allow_null=True)
    due_at = serializers.DateTimeField()
    requested_by = UserBriefSerializer(allow_null=True)
    paused = serializers.BooleanField(
        help_text="The organization is suspended, so the deletion waits: nothing is purged while suspended, and "
        "unsuspending moves `due_at` to give back the time that was left when the suspension began (at least a day)."
    )
    purge_attempted_at = serializers.DateTimeField(
        allow_null=True, help_text="When the purge last tried this organization and removed nothing (null: never)."
    )
    purge_failures = serializers.IntegerField(
        help_text="Purge attempts in a row that failed. From 3 the org.purge_deleted schedule fails until it is fixed; "
        "the worker log has the error."
    )


def deletion_of(org) -> dict[str, Any] | None:
    if org.deletion_due_at is None:
        return None
    return DeletionStateSerializer(
        {
            "requested_at": org.deletion_requested_at,
            "due_at": org.deletion_due_at,
            "requested_by": org.deletion_requested_by,
            "paused": org.suspended_at is not None,
            "purge_attempted_at": org.purge_attempted_at,
            "purge_failures": org.purge_failures,
        }
    ).data


def usage_of(found: OrganizationUsage | None) -> dict[str, Any] | None:
    return UsageSerializer(found).data if found is not None else None


class PlatformOrganizationSerializer(ReadOnlySerializer):
    """One organization in the platform list. Context: `usage` {organization id: OrganizationUsage}."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    slug = serializers.CharField(read_only=True)
    edition = serializers.ChoiceField(Edition.choices, read_only=True)
    timezone = serializers.CharField(read_only=True, help_text="The organization's IANA time zone (its dates).")
    created_at = serializers.DateTimeField(read_only=True)
    member_count = serializers.IntegerField(read_only=True)
    last_activity_at = serializers.SerializerMethodField(help_text="The newest audit event (usage snapshot).")
    deletion = serializers.SerializerMethodField(help_text="Set while the organization is scheduled for deletion.")
    suspended_at = serializers.DateTimeField(read_only=True, allow_null=True)
    usage = serializers.SerializerMethodField(help_text="The hourly usage snapshot; null before its first run.")

    def _usage(self, org) -> OrganizationUsage | None:
        return self.context.get("usage", {}).get(org.pk)

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_last_activity_at(self, org) -> str | None:
        found = self._usage(org)
        return iso(found.last_activity_at) if found else None

    @extend_schema_field(DeletionStateSerializer(allow_null=True))
    def get_deletion(self, org) -> dict[str, Any] | None:
        return deletion_of(org)

    @extend_schema_field(UsageSerializer(allow_null=True))
    def get_usage(self, org) -> dict[str, Any] | None:
        return usage_of(self._usage(org))


class PlatformMemberSerializer(ReadOnlySerializer):
    id = serializers.IntegerField(source="user_id", read_only=True, help_text="The user's ID (impersonate by it).")
    name = serializers.SerializerMethodField()
    email = serializers.CharField(source="user.email", read_only=True)
    role = serializers.ChoiceField(Role.choices, read_only=True)
    active = serializers.BooleanField(source="user.is_active", read_only=True)
    last_login = serializers.DateTimeField(source="user.last_login", read_only=True, allow_null=True)
    platform_staff = serializers.SerializerMethodField(help_text="Platform staff can't be impersonated.")

    def get_name(self, m) -> str:
        return m.user.get_full_name() or m.user.email

    def get_platform_staff(self, m) -> bool:
        return m.user_id in self.context.get("staff", set())


class CapabilityOverrideSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    capability = serializers.CharField(read_only=True)
    effect = serializers.ChoiceField(OverrideEffect.choices, read_only=True)
    reason = serializers.CharField(read_only=True)
    expires_at = serializers.DateTimeField(read_only=True, allow_null=True)
    live = serializers.SerializerMethodField(help_text="False once expires_at has passed: it no longer applies.")
    created_by = UserBriefSerializer(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def get_live(self, override) -> bool:
        return override.expires_at is None or override.expires_at > timezone.now()


class CapabilitiesSerializer(serializers.Serializer):
    edition = serializers.ListField(child=serializers.CharField(), help_text="The edition's capability names.")
    effective = serializers.ListField(
        child=serializers.CharField(), help_text="What the organization has now: edition, plus grants, minus revokes."
    )
    overrides = CapabilityOverrideSerializer(many=True)


class LimitOverrideSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    limit = serializers.ChoiceField(limits.NAMES, read_only=True)
    value = serializers.IntegerField(read_only=True, allow_null=True, help_text="Null: unlimited.")
    reason = serializers.CharField(read_only=True)
    expires_at = serializers.DateTimeField(read_only=True, allow_null=True)
    live = serializers.SerializerMethodField(help_text="False once expires_at has passed: the default applies again.")
    created_by = UserBriefSerializer(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def get_live(self, override) -> bool:
        return override.expires_at is None or override.expires_at > timezone.now()


class PlatformLimitsSerializer(serializers.Serializer):
    limits = LimitSerializer(many=True, help_text="Each limit now, its edition default and the live usage.")
    overrides = LimitOverrideSerializer(many=True)


def limits_of(org) -> dict[str, Any]:
    overrides = OrganizationLimitOverride.objects.select_related("created_by").filter(organization=org)
    return {"limits": limits.report(org), "overrides": LimitOverrideSerializer(overrides, many=True).data}


class PlatformAuditEventSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    at = serializers.DateTimeField(source="created_at", read_only=True)
    actor = UserBriefSerializer(read_only=True, allow_null=True, help_text="Null for the CLI or a removed account.")
    actor_email = serializers.CharField(read_only=True, help_text="Kept when the account is later removed.")
    action = serializers.CharField(read_only=True)
    label = serializers.SerializerMethodField()  # type: ignore[assignment]  # the API field, not Field.label
    organization_id = serializers.UUIDField(read_only=True, allow_null=True)
    details = serializers.DictField(read_only=True)
    request_id = serializers.UUIDField(read_only=True, allow_null=True)

    def get_label(self, e) -> str:
        return platform_admin.PLATFORM_ACTIONS.get(e.action, e.action)


def capabilities_of(org) -> dict[str, Any]:
    """The organization's capabilities for the console: the edition's, the effective ones, and the overrides."""
    overrides = CapabilityOverride.objects.select_related("created_by").filter(organization=org)
    return {
        "edition": sorted(capabilities.edition_capabilities(org)),
        "effective": sorted(capabilities.capabilities(org)),
        "overrides": CapabilityOverrideSerializer(overrides, many=True).data,
    }


class PlatformOrganizationDetailSerializer(PlatformOrganizationSerializer):
    suspended_reason = serializers.CharField(read_only=True)
    members = serializers.SerializerMethodField(help_text=f"Members by email, at most {MEMBERS_SHOWN}.")
    capabilities = serializers.SerializerMethodField()
    live_usage = serializers.SerializerMethodField(help_text="The counts computed for this response.")
    recent_events = serializers.SerializerMethodField(help_text=f"The newest {RECENT_EVENTS} platform audit events.")

    @extend_schema_field(PlatformMemberSerializer(many=True))
    def get_members(self, org) -> list[dict[str, Any]]:
        queryset = Membership.objects.select_related("user").filter(organization=org).order_by("user__email")
        rows = list(queryset[:MEMBERS_SHOWN])
        from core.models import PlatformStaff

        staff = set(
            PlatformStaff.objects.filter(user_id__in=[m.user_id for m in rows]).values_list("user_id", flat=True)
        )
        return list(PlatformMemberSerializer(rows, many=True, context={"staff": staff}).data)

    @extend_schema_field(CapabilitiesSerializer)
    def get_capabilities(self, org) -> dict[str, Any]:
        return capabilities_of(org)

    @extend_schema_field(UsageSerializer)
    def get_live_usage(self, org) -> dict[str, Any]:
        now = timezone.now()
        return UsageSerializer(
            {
                "computed_at": now,
                "counts": usage.counts(org.pk, now),
                "last_activity_at": usage.last_activity(org.pk),
                "last_sign_in_at": usage.last_sign_in(org.pk),
            }
        ).data

    @extend_schema_field(PlatformAuditEventSerializer(many=True))
    def get_recent_events(self, org) -> list[dict[str, Any]]:
        events = PlatformAuditEvent.objects.select_related("actor").filter(organization_id=org.pk)
        return list(PlatformAuditEventSerializer(events.order_by("-created_at", "-id")[:RECENT_EVENTS], many=True).data)


class ImpersonationSerializer(serializers.Serializer):
    id = serializers.UUIDField(
        help_text="This impersonation. Send it as X-Softwarden-Impersonation on writes (`none` when not "
        "impersonating), so a tab left open from another impersonation is refused."
    )
    user = UserBriefSerializer(help_text="The member you act as.")
    organization = serializers.DictField(help_text="{id, name, slug} of the organization.")
    by = UserBriefSerializer(help_text="The platform staff account.")
    reason = serializers.CharField()
    started_at = serializers.DateTimeField()
    expires_at = serializers.DateTimeField(help_text="It ends on the first request after this.")


class ImpersonationStateSerializer(serializers.Serializer):
    active = serializers.BooleanField()
    impersonation = ImpersonationSerializer(allow_null=True)
    minutes = serializers.IntegerField(
        help_text="How long a new impersonation lasts (SOFTWARDEN_IMPERSONATION_MINUTES)."
    )


class SecondFactorStateSerializer(serializers.Serializer):
    # A declared field named like Field.required: DRF's metaclass moves it off the class, so `self.required` stays.
    required = serializers.BooleanField(  # type: ignore[assignment]
        help_text="Whether the console needs a TOTP code in this session (SOFTWARDEN_PLATFORM_TOTP; on in cloud mode)."
    )
    enrolled = serializers.BooleanField(help_text="Whether this staff account has set up its second factor.")
    verified = serializers.BooleanField(help_text="Whether this session sent a current code for it.")


class PlatformSessionSerializer(serializers.Serializer):
    user = UserBriefSerializer(help_text="The platform staff account signed in (not the member while impersonating).")
    platform_staff = serializers.BooleanField()
    impersonation = ImpersonationSerializer(allow_null=True)
    second_factor = SecondFactorStateSerializer(
        help_text="With `required` true and `verified` false, every other platform route answers 403 "
        "/problems/second-factor-required until platform/second-factor/verify accepts a code (enroll first when "
        "`enrolled` is false)."
    )


class SecondFactorEnrollmentSerializer(serializers.Serializer):
    secret = serializers.CharField(help_text="The TOTP secret, base32. Shown this once; never returned again.")
    otpauth_uri = serializers.CharField(help_text="The same secret as an otpauth:// URI, to show as a QR code.")


class SecondFactorCodeSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="The current 6-digit code from the authenticator app. Never logged.")


class ReasonInputSerializer(serializers.Serializer):
    reason = serializers.CharField(help_text=REASON_HELP)


class EditionInputSerializer(ReasonInputSerializer):
    edition = serializers.ChoiceField(Edition.choices)


class OverrideInputSerializer(ReasonInputSerializer):
    capability = serializers.CharField(help_text="A capability name (docs/CAPABILITIES.md).")
    effect = serializers.ChoiceField(OverrideEffect.choices, help_text="grant adds it; revoke takes it away.")
    expires_at = serializers.DateTimeField(required=False, allow_null=True, help_text="Leave out for no expiry.")


class LimitInputSerializer(ReasonInputSerializer):
    limit = serializers.ChoiceField(limits.NAMES)
    value = serializers.IntegerField(
        allow_null=True,
        min_value=0,
        help_text="The maximum (records, connectors, members, bytes, or requests per minute); null for unlimited. "
        "The API rates are at least 1. Below the current usage nothing is removed: new creates are refused until usage is under it.",
    )
    expires_at = serializers.DateTimeField(required=False, allow_null=True, help_text="Leave out for no expiry.")


class ImpersonationInputSerializer(ReasonInputSerializer):
    organization_id = serializers.UUIDField()
    user_id = serializers.IntegerField(help_text="The member's user ID (platform/organizations/{id} members).")
    password = serializers.CharField(
        required=False,
        help_text="Your current password. An account that signs in only with single sign-on leaves it out and must "
        "have signed in within the last 15 minutes. Never returned or logged.",
    )
    code = serializers.CharField(
        required=False,
        help_text="The current code from your authenticator app, required while SOFTWARDEN_PLATFORM_TOTP is on "
        "(a code is accepted once: use the next one if you just signed in with it). Never logged.",
    )


# ---- helpers --------------------------------------------------------------------------------------------------------


def impersonation_body(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """The session's impersonation record for ImpersonationSerializer (accounts as objects), or None."""
    if state is None:
        return None
    from django.contrib.auth import get_user_model

    member_id, staff_id = state.get("user"), state.get("by")
    users: dict[Any, Any] = {u.pk: u for u in get_user_model().objects.filter(pk__in=[member_id, staff_id])}
    org = Organization.objects.filter(pk=str(state.get("organization"))).only("id", "name", "slug").first()
    member, staff = users.get(member_id), users.get(staff_id)
    if member is None or staff is None or org is None:
        return None
    return {
        "id": state.get("id"),
        "user": member,
        "organization": {"id": str(org.pk), "name": org.name, "slug": org.slug},
        "by": staff,
        "reason": state.get("reason", ""),
        "started_at": state.get("started_at"),
        "expires_at": state.get("expires_at"),
    }


def platform_session_body(request) -> dict[str, Any]:
    """`platform/session`, also the sign-in response of a staff account with no membership (catalog/views.py)."""
    return PlatformSessionSerializer(
        {
            "user": real_user(request),
            "platform_staff": True,
            "impersonation": impersonation_body(_request_state(request)),
            "second_factor": staff_second_factor.state(request, real_user(request)),
        }
    ).data


def _request_state(request) -> dict[str, Any] | None:
    """The live impersonation of this request (the middleware applied it), else None."""
    django_request = getattr(request, "_request", request)
    return getattr(django_request, "softwarden_impersonation", None)


def _check_params(request, allowed: set[str]) -> None:
    unknown = sorted(set(request.query_params) - allowed - KeysetPagination.query_params - {"ordering"})
    if unknown:
        raise FieldErrors({name: ["This filter isn't available here."] for name in unknown})


class PlatformView(RoleAPIView):
    role_policy = policy(PLATFORM)

    def staff(self):
        return real_user(self.request)

    def organization_or_404(self, pk) -> Organization:
        return get_object_or_404(Organization.objects.select_related("deletion_requested_by"), pk=pk)

    def detail(self, org: Organization, status_code: int = 200) -> Response:
        org = (
            Organization.objects.select_related("deletion_requested_by")
            .annotate(member_count=Count("memberships"))
            .get(pk=org.pk)
        )
        found = OrganizationUsage.objects.filter(organization=org).first()
        context = {"usage": {org.pk: found} if found else {}}
        return Response(PlatformOrganizationDetailSerializer(org, context=context).data, status=status_code)


# ---- views ----------------------------------------------------------------------------------------------------------


class PlatformSessionView(PlatformView):
    allowed_while_impersonating = True
    before_second_factor = True  # the console reads `second_factor` here to know what to ask for

    @extend_schema(
        summary="You as platform staff (platform staff)",
        description="The staff account signed in and its impersonation, if any. For a staff account with no "
        "membership, `auth/me` answers 403; the console reads this instead.",
        responses={200: PlatformSessionSerializer, **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request)
        return Response(platform_session_body(request))


ORG_FILTERS = [
    OpenApiParameter("q", str, description="Part of the name or slug."),
    OpenApiParameter("edition", str, enum=Edition.values),
    OpenApiParameter("deletion", str, enum=["scheduled", "none"], description="Scheduled for deletion or not."),
    OpenApiParameter("suspended", str, enum=["true", "false"]),
]


class PlatformOrganizationListView(PlatformView):
    pagination_class = KeysetPagination  # what get() pages with; the schema declares {results, next, previous} from it
    ordering_fields = ("name", "slug", "created_at")
    default_ordering = ("name",)

    @extend_schema(
        summary="Organizations (platform staff)",
        description="Every organization with its edition, time zone, creation time, member count, deletion and "
        "suspension state and the hourly usage snapshot. Keyset pages like every list (`cursor`, `page_size`, `ordering` of name, slug "
        "or created_at).",
        parameters=ORG_FILTERS,
        responses={200: PlatformOrganizationSerializer(many=True), 400: PROBLEMS[400], **ERRORS},
    )
    def get(self, request):
        _check_params(request, {"q", "edition", "deletion", "suspended"})
        params = request.query_params
        queryset = Organization.objects.select_related("deletion_requested_by").annotate(
            member_count=Count("memberships")
        )
        if params.get("q"):
            q = params["q"].strip()
            queryset = queryset.filter(Q(name__icontains=q) | Q(slug__icontains=q))
        if params.get("edition"):
            queryset = capabilities.in_edition(queryset, params["edition"])
        deletion = params.get("deletion")
        if deletion not in (None, "", "scheduled", "none"):
            raise FieldErrors({"deletion": ["Use scheduled or none."]})
        if deletion:
            queryset = queryset.filter(deletion_due_at__isnull=deletion == "none")
        suspended = params.get("suspended")
        if suspended not in (None, "", "true", "false"):
            raise FieldErrors({"suspended": ["Use true or false."]})
        if suspended:
            queryset = queryset.filter(suspended_at__isnull=suspended == "false")
        paginator = KeysetPagination()
        rows = paginator.paginate_queryset(queryset, request, self)
        found = {u.organization_id: u for u in OrganizationUsage.objects.filter(organization__in=[r.pk for r in rows])}
        data = PlatformOrganizationSerializer(rows, many=True, context={"usage": found}).data
        return paginator.get_paginated_response(data)


class PlatformOrganizationDetailView(PlatformView):
    @extend_schema(
        summary="One organization (platform staff)",
        description="Its members (by user ID, for impersonation), capabilities (edition, effective, overrides), the "
        "usage snapshot and counts computed now, and the newest platform audit events about it.",
        responses={200: PlatformOrganizationDetailSerializer, **ERRORS},
    )
    def get(self, request, pk):
        refuse_unknown_params(request)
        return self.detail(self.organization_or_404(pk))


class PlatformEditionView(PlatformView):
    @extend_schema(
        summary="Change an organization's edition (platform staff)",
        description="Audited in the platform stream and in the organization's audit log. The same edition changes "
        "nothing.",
        request=EditionInputSerializer,
        responses={200: PlatformOrganizationDetailSerializer, **WRITE_ERRORS},
    )
    def post(self, request, pk):
        org = self.organization_or_404(pk)
        with transaction.atomic():
            org, _ = platform_admin.change_edition(self.staff(), org, self.payload())
            return self.detail(org)


class PlatformCancelDeletionView(PlatformView):
    @extend_schema(
        summary="Cancel an organization's scheduled deletion (platform staff)",
        description="409 when none is scheduled. Audited in both streams.",
        request=ReasonInputSerializer,
        responses={200: PlatformOrganizationDetailSerializer, **WRITE_ERRORS},
    )
    def post(self, request, pk):
        org = self.organization_or_404(pk)
        with transaction.atomic():
            org = platform_admin.cancel_deletion(self.staff(), org, self.payload())
            return self.detail(org)


class PlatformSuspendView(PlatformView):
    @extend_schema(
        summary="Suspend an organization (platform staff)",
        description="Its members get /problems/organization-suspended on every request but `auth/me`, and the worker "
        "leaves its jobs queued. Nothing is removed: a scheduled deletion is paused (`deletion.paused`) and resumes "
        "on unsuspend with the time it had left, at least a day. 409 when already suspended. Audited in both streams.",
        request=ReasonInputSerializer,
        responses={200: PlatformOrganizationDetailSerializer, **WRITE_ERRORS},
    )
    def post(self, request, pk):
        org = self.organization_or_404(pk)
        with transaction.atomic():
            org = platform_admin.suspend(self.staff(), org, self.payload())
            return self.detail(org)


class PlatformUnsuspendView(PlatformView):
    @extend_schema(
        summary="Unsuspend an organization (platform staff)",
        description="A deletion the suspension paused resumes: `deletion.due_at` becomes now plus the time it had left "
        "when the suspension began, at least a day (audited as organization.deletion_postponed). 409 when it isn't "
        "suspended. Audited in both streams.",
        request=ReasonInputSerializer,
        responses={200: PlatformOrganizationDetailSerializer, **WRITE_ERRORS},
    )
    def post(self, request, pk):
        org = self.organization_or_404(pk)
        with transaction.atomic():
            org = platform_admin.unsuspend(self.staff(), org, self.payload())
            return self.detail(org)


class PlatformOverridesView(PlatformView):
    @extend_schema(
        summary="An organization's capability overrides (platform staff)",
        responses={200: CapabilitiesSerializer, **ERRORS},
    )
    def get(self, request, pk):
        refuse_unknown_params(request)
        org = self.organization_or_404(pk)
        return Response(capabilities_of(org))

    @extend_schema(
        summary="Grant or revoke a capability for one organization (platform staff)",
        description="Replaces an override of the same capability. Applies from the next request: `capabilities` is "
        "the edition's set, plus live grants, minus live revokes. Unknown names are refused. Audited in both streams.",
        request=OverrideInputSerializer,
        responses={201: CapabilityOverrideSerializer, **WRITE_ERRORS},
    )
    def post(self, request, pk):
        org = self.organization_or_404(pk)
        with transaction.atomic():
            override = platform_admin.set_override(self.staff(), org, self.payload())
            return Response(CapabilityOverrideSerializer(override).data, status=status.HTTP_201_CREATED)


class PlatformOverrideRemoveView(PlatformView):
    @extend_schema(
        summary="Remove a capability override (platform staff)",
        description="The capability follows the edition again from the next request. Audited in both streams.",
        request=ReasonInputSerializer,
        responses={204: None, **WRITE_ERRORS},
    )
    def post(self, request, pk, override_pk):
        org = self.organization_or_404(pk)
        override = get_object_or_404(CapabilityOverride, organization=org, pk=override_pk)
        with transaction.atomic():
            platform_admin.remove_override(self.staff(), org, override, self.payload())
        return Response(status=status.HTTP_204_NO_CONTENT)


class PlatformLimitsView(PlatformView):
    @extend_schema(
        summary="An organization's limits (platform staff)",
        description="Each limit with its value now, the edition's default and the usage counted now, and the "
        "organization's overrides.",
        responses={200: PlatformLimitsSerializer, **ERRORS},
    )
    def get(self, request, pk):
        refuse_unknown_params(request)
        return Response(limits_of(self.organization_or_404(pk)))

    @extend_schema(
        summary="Set one of an organization's limits (platform staff)",
        description="Replaces an override of the same limit; `value` null is unlimited. Applies from the next request. "
        "A value below the current usage removes nothing; creates are refused until usage is under it. Audited in "
        "both streams.",
        request=LimitInputSerializer,
        responses={201: LimitOverrideSerializer, **WRITE_ERRORS},
    )
    def post(self, request, pk):
        org = self.organization_or_404(pk)
        with transaction.atomic():
            override = platform_admin.set_limit(self.staff(), org, self.payload())
            return Response(LimitOverrideSerializer(override).data, status=status.HTTP_201_CREATED)


class PlatformLimitRemoveView(PlatformView):
    @extend_schema(
        summary="Remove a limit override (platform staff)",
        description="The edition's default applies again from the next request. 404 when there is no override of "
        "that limit. Audited in both streams.",
        request=ReasonInputSerializer,
        responses={204: None, **WRITE_ERRORS},
    )
    def post(self, request, pk, name):
        org = self.organization_or_404(pk)
        if name not in limits.LIMITS:
            raise exceptions.NotFound("There is no limit of that name.")
        with transaction.atomic():
            platform_admin.remove_limit(self.staff(), org, name, self.payload())
        return Response(status=status.HTTP_204_NO_CONTENT)


class PlatformExportView(PlatformView):
    @extend_schema(
        summary="Make a data export an organization asked for (platform staff)",
        description="Queues the same export an Admin asks for, with purpose `operator`, for an organization that asked "
        "through support. It appears in the organization's exports and only its Admins can download it; the platform "
        "API never returns the file. 409 while another export is being prepared. Audited in both streams.",
        request=ReasonInputSerializer,
        responses={201: OrgExportSerializer, **WRITE_ERRORS},
    )
    def post(self, request, pk):
        org = self.organization_or_404(pk)
        with transaction.atomic():
            export = platform_admin.request_export(self.staff(), org, self.payload())
            return Response(OrgExportSerializer(export).data, status=status.HTTP_201_CREATED)


# ---- sign-in domains ------------------------------------------------------------------


class PlatformDomainOrganizationSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    slug = serializers.CharField()


class PlatformDomainClaimSerializer(ReadOnlySerializer):
    domain = serializers.CharField()
    organization = PlatformDomainOrganizationSerializer()
    status = serializers.SerializerMethodField()
    claimed_at = serializers.DateTimeField(source="created_at")
    verified_at = serializers.DateTimeField(allow_null=True)

    def get_status(self, row) -> str:
        return "pending" if row.verified_at is None else "verified"


class DomainMoveInputSerializer(ReasonInputSerializer):
    domain = serializers.CharField()
    organization_id = serializers.UUIDField(help_text="The organization that gets the domain, verified.")


class DomainReleaseInputSerializer(ReasonInputSerializer):
    domain = serializers.CharField()


class PlatformDomainClaimsView(PlatformView):
    @extend_schema(
        summary="Who claims a sign-in domain (platform staff)",
        description="Every organization's claim of the domain: the one that holds it verified (it routes sign-ins), "
        "and pending claims (each waiting for its DNS TXT record, lapsing 14 days after it was made; re-adding a domain makes a new claim).",
        parameters=[OpenApiParameter("domain", str, required=True, description="A domain name, such as example.com.")],
        responses={200: PlatformDomainClaimSerializer(many=True), **ERRORS, 400: PROBLEMS[400]},
    )
    def get(self, request):
        refuse_unknown_params(request, allowed={"domain"})
        rows = platform_admin.sign_in_domain_claims(request.query_params.get("domain", ""))
        return Response(PlatformDomainClaimSerializer(rows, many=True).data)


class PlatformDomainMoveView(PlatformView):
    @extend_schema(
        summary="Move a sign-in domain to an organization (platform staff)",
        description="The organization gets the domain verified (its own pending claim is verified, or a new one is "
        "made); the organization that held it verified loses it. Use it for a squatted or disputed domain after "
        "checking who controls it. Audited in both organizations and the platform stream.",
        request=DomainMoveInputSerializer,
        responses={200: PlatformDomainClaimSerializer, **WRITE_ERRORS},
    )
    def post(self, request):
        row = platform_admin.move_sign_in_domain(self.staff(), self.payload())
        return Response(PlatformDomainClaimSerializer(row).data)


class PlatformDomainReleaseView(PlatformView):
    @extend_schema(
        summary="Release a sign-in domain (platform staff)",
        description="Removes the verified claim (or, when nobody holds it verified, the oldest pending one), so another "
        "organization can verify it. Audited in the organization that had it and the platform stream. 404 when nobody "
        "claims it.",
        request=DomainReleaseInputSerializer,
        responses={204: None, **WRITE_ERRORS},
    )
    def post(self, request):
        if platform_admin.release_sign_in_domain(self.staff(), self.payload()) is None:
            raise exceptions.NotFound("No organization claims this domain.")
        return Response(status=status.HTTP_204_NO_CONTENT)


def _uuid(name: str, value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise FieldErrors({name: ["Use an ID."]}) from None


class PlatformAuditView(PlatformView):
    pagination_class = KeysetPagination  # as PlatformOrganizationListView
    ordering_fields = ("created_at",)
    default_ordering = ("-created_at",)

    @extend_schema(
        summary="The platform audit stream (platform staff)",
        description="Every platform action, newest first: staff granted or revoked, editions, suspensions, deletion "
        "canceled, capability overrides, impersonations. Filters: organization (ID), action, actor (user ID).",
        parameters=[
            OpenApiParameter("organization", OpenApiTypes.UUID),
            OpenApiParameter("action", str, enum=sorted(platform_admin.PLATFORM_ACTIONS)),
            OpenApiParameter("actor", int),
        ],
        responses={200: PlatformAuditEventSerializer(many=True), 400: PROBLEMS[400], **ERRORS},
    )
    def get(self, request):
        _check_params(request, {"organization", "action", "actor"})
        params = request.query_params
        queryset = PlatformAuditEvent.objects.select_related("actor")
        if params.get("organization"):
            queryset = queryset.filter(organization_id=_uuid("organization", params["organization"]))
        if params.get("action"):
            queryset = queryset.filter(action=params["action"])
        if params.get("actor"):
            actor = whole_number(params["actor"], 1)
            if actor is None:
                raise FieldErrors({"actor": ["Use a user ID."]})
            queryset = queryset.filter(actor_id=actor)
        paginator = KeysetPagination()
        rows = paginator.paginate_queryset(queryset, request, self)
        return paginator.get_paginated_response(PlatformAuditEventSerializer(rows, many=True).data)


class PlatformSecondFactorView(PlatformView):
    """The platform staff second factor (catalog/staff_second_factor.py). Reachable before the second factor: it is how staff get past it."""

    before_second_factor = True

    @extend_schema(
        summary="Your second factor (platform staff)",
        description="Whether the console needs a TOTP code, whether you have set one up, and whether this session "
        "sent one.",
        responses={200: SecondFactorStateSerializer, **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request)
        return Response(staff_second_factor.state(request, self.staff()))


class PlatformSecondFactorEnrollmentView(PlatformView):
    before_second_factor = True

    @extend_schema(
        summary="Set up your second factor (platform staff)",
        description="A new TOTP secret for an authenticator app, shown this once (base32 and an otpauth:// URI for a "
        "QR code), stored encrypted. Confirm it with platform/second-factor/verify. Asking again before confirming "
        "replaces it; once confirmed, 409: the operator resets a lost one (manage.py platform_staff "
        "reset-second-factor). Audited (staff.second_factor_enrollment_started).",
        request=None,
        responses={201: SecondFactorEnrollmentSerializer, 403: PROBLEMS[403], 409: PROBLEMS[409]},
    )
    def post(self, request):
        return Response(staff_second_factor.start_enrollment(self.staff()), status=status.HTTP_201_CREATED)


class PlatformSecondFactorVerifyView(PlatformView):
    before_second_factor = True

    @extend_schema(
        summary="Send a code from your authenticator app (platform staff)",
        description="Accepts a current code once (within 30 seconds either side): confirms a new enrollment and opens "
        "the console in this session. 400 for a wrong or used code; 429 after five wrong codes in a row, until five minutes after the last. "
        "Audited (staff.second_factor_enrolled, staff.second_factor_verified, staff.second_factor_failed).",
        request=SecondFactorCodeSerializer,
        responses={
            200: SecondFactorStateSerializer,
            400: PROBLEMS[400],
            403: PROBLEMS[403],
            429: OpenApiResponse(ProblemSerializer, description="Too many wrong codes; Retry-After says when to retry"),
        },
    )
    def post(self, request):
        return Response(staff_second_factor.verify_session(request, self.staff(), self.payload().get("code")))


class PlatformCapabilitySerializer(serializers.Serializer):
    name = serializers.CharField(help_text="The capability name (docs/CAPABILITIES.md), as overrides take it.")
    editions = serializers.ListField(
        child=serializers.ChoiceField(Edition.choices),
        help_text="The editions that include it: community, cloud, enterprise.",
    )
    gated = serializers.BooleanField(help_text="True when community lacks it.")


class PlatformCapabilitiesView(PlatformView):
    @extend_schema(
        summary="Capability names and their editions (platform staff)",
        description="Every capability an override may grant or revoke, by name, with the editions that include it "
        "(community, cloud, enterprise). Read-only: the code defines them.",
        responses={200: PlatformCapabilitySerializer(many=True), **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request)
        return Response(PlatformCapabilitySerializer(capabilities.catalogue(), many=True).data)


class PlatformSystemView(PlatformView):
    @extend_schema(
        summary="The installation's System status (platform staff)",
        description="The System page's `installation` view (core/system_views.py): worker, schedules, the backup and "
        "its warnings, installation-wide jobs and every warning. No organization's jobs (`jobs.organization` is "
        "absent): each organization's own are on its System page.",
        responses={200: SystemStatusSerializer, 400: PROBLEMS[400], **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request)
        return Response(SystemStatusSerializer(installation_status(None)).data)


class PlatformBackupKeyAcknowledgeView(PlatformView):
    @extend_schema(
        summary="Acknowledge that the generated backup key is copied off the host (platform staff)",
        description="As `system/backup/acknowledge-key`, for staff without a membership. Audited in the platform "
        "stream and, with no actor and `via: platform`, in every organization; a repeat for the same key changes "
        "nothing. 409 when the key is set with SOFTWARDEN_BACKUP_KEY_FILE or doesn't exist yet.",
        request=None,
        responses={200: BackupStatusSerializer, **{c: PROBLEMS[c] for c in (403, 409)}},
    )
    def post(self, request):
        platform_admin.acknowledge_backup_key(self.staff())
        return Response(BackupStatusSerializer(backup.status()).data)


class _PostOnlyLoginThrottle(ScopedThrottle):
    """The sign-in rate, for starting an impersonation only (it checks the staff password)."""

    def allow_request(self, request, view):
        if request.method != "POST":
            return True
        return super().allow_request(request, view)


class PlatformImpersonationView(PlatformView):
    allowed_while_impersonating = True  # status and stop; starting is refused while impersonating (the service)
    throttle_classes = with_api_limits(_PostOnlyLoginThrottle)
    throttle_scope = "login"

    def state(self, request) -> dict[str, Any]:
        return ImpersonationStateSerializer(
            {
                "active": _request_state(request) is not None,
                "impersonation": impersonation_body(_request_state(request)),
                "minutes": platform_admin.impersonation_minutes(),
            }
        ).data

    @extend_schema(
        summary="The session's impersonation (platform staff)",
        responses={200: ImpersonationStateSerializer, **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request)
        return Response(self.state(request))

    @extend_schema(
        summary="Impersonate a member (platform staff)",
        description="Needs your current password (or a single sign-on sign-in in this session within 15 minutes), a "
        "reason, and the member's organization and user ID. Lasts SOFTWARDEN_IMPERSONATION_MINUTES (30 by default) "
        "and ends on the first request after that. In this browser session you then act as the member; credentials, "
        "identities, single sign-on, tokens, secrets and deletion are refused (/problems/impersonation-restricted), "
        "and every write is audited with `impersonated_by`. Refused for platform staff, deactivated accounts and "
        "while already impersonating. The session key and CSRF token change: read the CSRF cookie again. Audited in "
        "both streams.",
        request=ImpersonationInputSerializer,
        responses={
            201: ImpersonationStateSerializer,
            **WRITE_ERRORS,
            429: OpenApiResponse(ProblemSerializer, description="Too many attempts; Retry-After says when to retry"),
        },
    )
    def post(self, request):
        if _request_state(request) is not None:  # no impersonation inside an impersonation
            raise ImpersonationRestricted("You are already impersonating someone; stop first.")
        django_request = getattr(request, "_request", request)
        staff = self.staff()
        # The password here counts with the staff account's sign-in attempts (core/signin_limits.py), counted
        # before it is checked, from whichever process. A wrong password keeps the count; a refusal for any other
        # reason takes the attempt back; a success forgets it.
        signin_limits.attempt_account(staff.username)
        try:
            if staff_second_factor.required():  # A current code, checked (and counted) first
                staff_second_factor.check_code(staff, self.payload().get("code"), "impersonation")
            platform_admin.start_impersonation(django_request, staff, self.payload())
        except org_lifecycle.ReauthenticationRequired:
            raise
        except Exception:
            signin_limits.release_account(staff.username)
            raise
        signin_limits.forget_account(staff.username)
        django_request.softwarden_impersonation = impersonation_state(django_request)
        return Response(self.state(request), status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="Stop impersonating (platform staff)",
        description="Your own staff session comes back exactly as it was before you started. The session key and "
        "CSRF token change. 409 when you aren't impersonating. Audited in both streams.",
        request=None,
        responses={200: ImpersonationStateSerializer, **WRITE_ERRORS},
    )
    def delete(self, request):
        if _request_state(request) is None:
            raise Conflict("You aren't impersonating anyone.")
        django_request = getattr(request, "_request", request)
        platform_admin.end_impersonation(django_request, impersonator(request), "stopped")
        return Response(self.state(request))


def impersonation_state(django_request) -> dict[str, Any] | None:
    from core import impersonation

    return impersonation.state_of(django_request)
