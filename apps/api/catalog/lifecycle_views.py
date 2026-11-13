"""The organization's data export and self-service deletion (catalog/org_lifecycle.py). Admin only.

    GET    organization/exports                 the organization's exports, newest first (with nightly backups and
                                                 exports the operator made on request; see `purpose`)
    POST   organization/exports                 export all data now (capability org.export); 201 with the export
    GET    organization/exports/{id}            one export
    GET    organization/exports/{id}/download   the zip, while it is ready and not expired (audited)
    GET    organization/deletion                whether deletion is scheduled, when, and its final export
    POST   organization/deletion                schedule it {password, confirm} (capability org.self_delete)
    DELETE organization/deletion                cancel it during the grace period

Requesting and downloading an export, and asking for or canceling deletion, need a signed-in session: an API token
gets 403. Asking for deletion shares the sign-in rate per account (LOGIN_THROTTLE_RATE), since it checks a password."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import transaction
from django.http import FileResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import exceptions, serializers, status
from rest_framework.decorators import action
from rest_framework.response import Response

from core import access
from core.capabilities import ORG_EXPORT, ORG_SELF_DELETE, has_capability, require_capability
from core.drf import (
    ADMIN,
    PROBLEMS,
    ProblemSerializer,
    ReadOnlySerializer,
    RoleAPIView,
    TenantViewSet,
    UserBriefSerializer,
    policy,
    refuse_unknown_params,
)
from core.models import ExportPurpose, ExportStatus, Organization, OrgExport
from core.throttling import ScopedThrottle, with_api_limits

from . import org_lifecycle

WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409)}


class OrgExportSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    purpose = serializers.ChoiceField(
        ExportPurpose.choices,
        read_only=True,
        help_text="export: an Admin asked for it; deletion: made when deletion was asked for; backup: the nightly "
        "backup (org.tenant_backups), kept until newer ones replace it; operator: made by the service operator because "
        "the organization asked through support.",
    )
    status = serializers.ChoiceField(ExportStatus.choices, read_only=True)
    requested_by = UserBriefSerializer(read_only=True, allow_null=True)
    size = serializers.IntegerField(read_only=True, allow_null=True, help_text="Bytes, once ready.")
    sha256 = serializers.CharField(read_only=True, help_text="The zip's SHA-256, once ready.")
    counts = serializers.DictField(
        child=serializers.IntegerField(), read_only=True, help_text="Rows per model, users and attachment files."
    )
    error = serializers.CharField(read_only=True, help_text="Why it failed, in Softwarden's words.")
    created_at = serializers.DateTimeField(read_only=True)
    finished_at = serializers.DateTimeField(read_only=True, allow_null=True)
    expires_at = serializers.DateTimeField(
        read_only=True,
        allow_null=True,
        help_text=f"{org_lifecycle.EXPORT_DAYS} days after it is ready; null for a backup, which is kept until the "
        "newest SOFTWARDEN_TENANT_BACKUP_RETENTION backups replace it.",
    )
    download_url = serializers.SerializerMethodField(
        help_text="The download route while the export is ready and not expired, else null."
    )

    def get_download_url(self, export) -> str | None:
        return org_lifecycle.download_url(export)


class OrganizationDeletionStateSerializer(serializers.Serializer):
    available = serializers.BooleanField(
        help_text="Whether this organization can delete itself (capability org.self_delete)."
    )
    scheduled = serializers.BooleanField()
    requested_at = serializers.DateTimeField(allow_null=True)
    due_at = serializers.DateTimeField(allow_null=True, help_text="When everything is removed; cancel before then.")
    requested_by = UserBriefSerializer(allow_null=True)
    grace_days = serializers.IntegerField(help_text="Days between asking and removal (the installation's setting).")
    backups_kept_days = serializers.IntegerField(
        help_text="For about how many days after removal the installation's own backups, taken before it, still hold "
        "the organization's data (their retention; the operator's database provider may keep its own history too)."
    )
    export = OrgExportSerializer(
        allow_null=True, help_text="The export made when deletion was last asked for; download it before due_at."
    )


class OrganizationDeletionInputSerializer(serializers.Serializer):
    confirm = serializers.CharField(help_text="The organization's slug, typed exactly.")
    password = serializers.CharField(
        required=False,
        help_text="Your current password. An account that signs in only with single sign-on leaves it out and must "
        "have signed in within the last 15 minutes. Never returned or logged.",
    )


class _PostOnlyLoginThrottle(ScopedThrottle):
    """The sign-in rate, for the deletion request only (it checks a password)."""

    def allow_request(self, request, view):
        if request.method != "POST":
            return True
        return super().allow_request(request, view)


@extend_schema_view(
    list=extend_schema(summary="Organization data exports (Admin)"),
    retrieve=extend_schema(summary="Get an organization data export (Admin)"),
    create=extend_schema(
        summary="Export all of the organization's data (Admin)",
        description="Queues a zip of every record as JSON lines plus the attachment files; the worker builds it. "
        f"Download it from `download_url` for {org_lifecycle.EXPORT_DAYS} days once it is ready. Secrets (credential "
        "ciphertexts, token hashes, password hashes) are never included. 409 while another export is being prepared. "
        "A signed-in session only: an API token gets 403.",
        request=None,
        responses={201: OrgExportSerializer, **WRITE_ERRORS},
    ),
    download=extend_schema(
        summary="Download an organization data export (Admin)",
        description="The zip, while the export is ready and not expired; audited. 404 otherwise. A signed-in session "
        "only: an API token, read-only or not, gets 403.",
        responses={
            (200, "application/zip"): OpenApiResponse(OpenApiTypes.BINARY, description="The export zip"),
            403: PROBLEMS[403],
            404: PROBLEMS[404],
        },
    ),
)
class OrgExportViewSet(TenantViewSet):
    role_policy = policy(ADMIN, ADMIN)
    serializer_class = OrgExportSerializer
    ordering_fields = ("created_at",)
    default_ordering = ("-created_at",)

    def base_queryset(self):
        """The exports this organization's Admins may see now (org_lifecycle.visible_purposes): an operator's always,
        backups with org.tenant_backups or org.export, the others with org.export. Another purpose is a 404."""
        rows = OrgExport.objects.select_related("requested_by")
        if getattr(self, "swagger_fake_view", False):  # OpenAPI generation: no request organization
            return rows
        return rows.filter(purpose__in=org_lifecycle.visible_purposes(self.organization))

    def check_permissions(self, request):
        super().check_permissions(request)  # the role, and a read-only token's write refusal, first
        if self.action in ("create", "download") and access.pinned_organization_id(request) is not None:
            raise exceptions.PermissionDenied(org_lifecycle.SESSION_ONLY)

    def perform_content_negotiation(self, request, force=False):
        # A download answers with the file whatever the client accepts; errors are still problem JSON.
        return super().perform_content_negotiation(request, force=force or self.action == "download")

    def create(self, request, *args, **kwargs):
        require_capability(self.organization, ORG_EXPORT)
        with transaction.atomic():
            export = org_lifecycle.request_export(self.organization, request.user)
            return self.detail_response(self.fresh(export), status_code=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        refuse_unknown_params(request)
        export = self.get_object()
        handle = org_lifecycle.open_export(export)
        if handle is None:
            raise exceptions.NotFound("This export isn't ready, has expired, or its file is gone. Ask for a new one.")
        org_lifecycle.record_download(self.organization, request.user, export)
        day = (export.finished_at or timezone.now()).date().isoformat()
        response = FileResponse(handle, content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="softwarden-export-{self.organization.slug}-{day}.zip"'
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response


def _session_only(request) -> None:
    """Deleting an organization (or cancelling) is a person's decision, re-authenticated: never an API token's."""
    if access.pinned_organization_id(request) is not None:
        raise exceptions.PermissionDenied(
            "Asking for or canceling deletion needs a signed-in session, not an API token."
        )


class OrganizationDeletionView(RoleAPIView):
    role_policy = policy(ADMIN, ADMIN)
    throttle_classes = with_api_limits(_PostOnlyLoginThrottle)
    throttle_scope = "login"

    def state(self, org: Organization) -> dict[str, Any]:
        org = Organization.objects.select_related("deletion_requested_by").get(pk=org.pk)
        export = org_lifecycle.deletion_export(org)
        return OrganizationDeletionStateSerializer(
            {
                "available": has_capability(org, ORG_SELF_DELETE),
                "scheduled": org.deletion_due_at is not None,
                "requested_at": org.deletion_requested_at,
                "due_at": org.deletion_due_at,
                "requested_by": org.deletion_requested_by,
                "grace_days": org_lifecycle.grace().days,
                "backups_kept_days": org_lifecycle.backups_kept_days(),
                "export": export,
            }
        ).data

    @extend_schema(
        summary="Whether the organization is scheduled for deletion (Admin)",
        responses={200: OrganizationDeletionStateSerializer, 403: PROBLEMS[403]},
    )
    def get(self, request):
        refuse_unknown_params(request)
        return Response(self.state(self.organization))

    @extend_schema(
        summary="Delete this organization after a grace period (Admin, cloud)",
        description="Needs capability org.self_delete, your current password (or a single sign-on sign-in within the "
        "last 15 minutes for an account without one) and the organization's slug typed in `confirm`. Schedules the "
        f"deletion SOFTWARDEN_ORG_DELETION_GRACE_DAYS ({settings.SOFTWARDEN_ORG_DELETION_GRACE_DAYS} by default) "
        "days ahead and queues a final export; any Admin can cancel until then. Afterwards every record, file and the "
        "audit log are removed; accounts left with no organization are deactivated. 403 "
        "/problems/reauthentication-required for a wrong password or an old sign-in; 409 when already scheduled.",
        request=OrganizationDeletionInputSerializer,
        responses={
            200: OrganizationDeletionStateSerializer,
            **WRITE_ERRORS,
            429: OpenApiResponse(ProblemSerializer, description="Too many attempts; Retry-After says when to retry"),
        },
    )
    def post(self, request):
        _session_only(request)
        data = self.payload()
        with transaction.atomic():
            org = org_lifecycle.request_deletion(
                self.organization, request.user, data, access.authenticated_at(request)
            )
            return Response(self.state(org))

    @extend_schema(
        summary="Cancel the organization's scheduled deletion (Admin)",
        request=None,
        responses={200: OrganizationDeletionStateSerializer, **WRITE_ERRORS},
    )
    def delete(self, request):
        _session_only(request)
        with transaction.atomic():
            org = org_lifecycle.cancel_deletion(self.organization, request.user)
            return Response(self.state(org))
