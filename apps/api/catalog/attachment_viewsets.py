"""Attachments: files and links on records and vendors (catalog/attachments.py).

Routes:
- `/<applications|components|ai-agents|integrations|vendors|contracts>/{id}/attachments`: the parent's attachments (a plain
  array) and adding one: multipart/form-data with `file` uploads a file, a JSON body adds a link.
- `/attachments/{id}`: one attachment; PATCH changes its title, category or a link's address; DELETE removes it.
- `/attachments/{id}/download`: the stored file, always as a download (never inline).

Roles: every role of the organization lists, reads and downloads; Editors and Admins add, change and remove (as with
instances, the record's other child rows). Every route needs the `attachments` capability, and an AI agent's the
`ai_agents` one too. An ID from another organization, or one that doesn't exist, is the same 404, so attachment IDs
(random UUIDs) can't be probed. File uploads are rate-limited per user (SOFTWARDEN_ATTACHMENT_UPLOAD_RATE)."""

from typing import Any, ClassVar

from django.conf import settings
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import exceptions, status
from rest_framework.decorators import action

from core.capabilities import AI_AGENTS, ATTACHMENTS, CONTRACTS, require_capability
from core.drf import (
    EDITOR,
    PROBLEMS,
    VIEWER,
    ParamFilter,
    TenantFilterSet,
    TenantViewSet,
    choice,
    choices_csv,
    policy,
    refuse_unknown_params,
)
from core.parsers import JSONParser, MultiPartParser
from core.problems import FieldErrors
from core.throttling import UserThrottle

from . import attachments
from .models import Attachment, AttachmentCategory, AttachmentKind, Kind, Resource, Vendor
from .serializers import (
    AttachmentLinkInputSerializer,
    AttachmentSerializer,
    AttachmentUpdateInputSerializer,
    AttachmentUploadInputSerializer,
)

SANDBOX_CSP = "default-src 'none'; sandbox"


class UploadThrottle(UserThrottle):
    """File uploads per user (DRF's user throttle, in the same per-process cache as the sign-in throttle). Links and
    every other request are not counted."""

    scope = "attachment_upload"

    def get_rate(self):
        return settings.ATTACHMENT_UPLOAD_RATE

    def allow_request(self, request, view):
        if request.method != "POST" or not (request.content_type or "").startswith("multipart/"):
            return True
        return super().allow_request(request, view)


class AttachmentFilterSet(TenantFilterSet):
    kind = ParamFilter(parse=choice(AttachmentKind.values), field_name="kind", choices=AttachmentKind.choices)
    category = ParamFilter(
        parse=choices_csv(AttachmentCategory.values),
        field_name="category",
        lookup_expr="in",
        help_text="One or more categories, comma-separated.",
    )

    class Meta:
        model = Attachment
        fields: list[str] = []


def _agent_capability(org, resource) -> None:
    if resource is not None and resource.kind == Kind.AI_AGENT:
        require_capability(org, AI_AGENTS)


class ParentAttachmentViewSet(TenantViewSet):
    """A record's or vendor's attachments, by category then title. Subclasses name the parent's kind."""

    parent_kind: ClassVar[str]  # a Resource kind, "vendor" or "contract"
    role_policy = policy(VIEWER, EDITOR)
    required_capability = ATTACHMENTS
    pagination_class = None
    serializer_class = AttachmentSerializer
    input_serializer_class = AttachmentLinkInputSerializer
    filterset_class = AttachmentFilterSet
    ordering_fields = ("title", "category", "created_at")
    default_ordering = ("category", "title")

    def get_parsers(self):
        """Adding takes a file (multipart/form-data) or a link (JSON)."""
        request = getattr(self, "request", None)
        if request is not None and request.method == "POST":
            return [JSONParser(), MultiPartParser()]
        return super().get_parsers()

    def get_throttles(self):
        own = [UploadThrottle()] if self.action == "create" else []
        return [*super().get_throttles(), *own]  # the API bucket (core/throttling.py) and the upload limit

    _parent: Any

    def parent(self) -> Any:
        if not hasattr(self, "_parent"):
            pk = self.kwargs["pk"]
            if self.parent_kind == "vendor":
                self._parent = get_object_or_404(Vendor, organization=self.organization, id=pk)
            elif self.parent_kind == "contract":
                from money.models import Contract

                require_capability(self.organization, CONTRACTS)
                self._parent = get_object_or_404(Contract, organization=self.organization, id=pk)
            else:
                self._parent = get_object_or_404(
                    Resource.objects.filter(organization=self.organization, kind=self.parent_kind), id=pk
                )
                _agent_capability(self.organization, self._parent)
        return self._parent

    def base_queryset(self):
        return Attachment.objects.select_related("resource", "vendor", "contract", "uploaded_by")

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, "swagger_fake_view", False):
            return qs
        return qs.filter(**attachments.parent_fields(self.parent()))

    def list(self, request, *args, **kwargs):
        self.parent()  # 404 for another organization's parent, before filters are read
        return super().list(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        parent = self.parent()
        org = self.organization
        if (request.content_type or "").startswith("multipart/"):
            limit = attachments.limits_for(org).max_size_mb
            try:
                length = int(request.META.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            # The organization's own limit, before this view reads the body. Anything over the installation's request
            # limit never got here: core/upload_limit.py refused it before CSRF could parse it.
            if length > limit * attachments.MB + attachments.MULTIPART_FRAMING:
                raise FieldErrors({"file": [f"The file is larger than {limit} MB."]})
            upload = request.FILES.get("file")
            data = {k: request.data.get(k) for k in ("title", "category") if k in request.data}
            obj = attachments.upload_file(org, request.user, parent, upload, data)  # atomic, as add_link is
        else:
            obj = attachments.add_link(org, request.user, parent, self.payload())
        return self.detail_response(
            self.fresh(obj), status_code=status.HTTP_201_CREATED, serializer_class=self.serializer_class
        )


def _parent_docs(noun: str):
    return extend_schema_view(
        list=extend_schema(summary=f"Attachments of {noun}", description="By category, then title. Any role."),
        create=extend_schema(
            summary=f"Attach a file or a link to {noun} (Editor, Admin)",
            description="multipart/form-data with `file` (and optionally `title`, `category`) uploads a file; a JSON "
            "body with `url` adds a link. A file's extension must be allowed for the organization and its content "
            "must match it; the size limit and the organization's quota apply (GET /organization `attachments`). "
            "File uploads are rate-limited per user (429 with Retry-After).",
            request={
                "multipart/form-data": AttachmentUploadInputSerializer,
                "application/json": AttachmentLinkInputSerializer,
            },
            responses={201: AttachmentSerializer, **{c: PROBLEMS[c] for c in (400, 403, 404, 409)}},
        ),
    )


@_parent_docs("an application")
class ApplicationAttachmentViewSet(ParentAttachmentViewSet):
    parent_kind = Kind.APPLICATION


@_parent_docs("a component")
class ComponentAttachmentViewSet(ParentAttachmentViewSet):
    parent_kind = Kind.COMPONENT


@_parent_docs("an AI agent")
class AIAgentAttachmentViewSet(ParentAttachmentViewSet):
    parent_kind = Kind.AI_AGENT


@_parent_docs("an integration")
class IntegrationAttachmentViewSet(ParentAttachmentViewSet):
    parent_kind = Kind.INTEGRATION


@_parent_docs("a vendor")
class VendorAttachmentViewSet(ParentAttachmentViewSet):
    parent_kind = "vendor"


@_parent_docs("a contract")
class ContractAttachmentViewSet(ParentAttachmentViewSet):
    """A contract's documents: needs the `contracts` capability as well."""

    parent_kind = "contract"


@extend_schema_view(
    retrieve=extend_schema(summary="Get an attachment"),
    partial_update=extend_schema(
        summary="Change an attachment's title, category or link address (Editor, Admin)",
        description="A file's content never changes; upload a new file and remove the old one instead.",
    ),
    destroy=extend_schema(
        summary="Remove an attachment (Editor, Admin)",
        description="A file is kept for the retention window (SOFTWARDEN_ATTACHMENT_RETENTION_DAYS, so backups stay "
        "consistent) and then deleted by the nightly sweep.",
    ),
    download=extend_schema(
        summary="Download an attachment's file",
        description="Always a download (Content-Disposition: attachment), never shown inline, with the type found by "
        "the content check, X-Content-Type-Options: nosniff, a sandboxing Content-Security-Policy and Cache-Control: "
        "private, no-store. A link has no file (404).",
        responses={
            (200, "*/*"): OpenApiResponse(
                OpenApiTypes.BINARY, description="The file, with the type the content check found."
            ),
            **{c: PROBLEMS[c] for c in (403, 404)},
        },
    ),
)
class AttachmentViewSet(TenantViewSet):
    """One attachment, by ID within the organization."""

    role_policy = policy(VIEWER, EDITOR)
    required_capability = ATTACHMENTS
    pagination_class = None
    serializer_class = AttachmentSerializer
    input_serializer_class = AttachmentUpdateInputSerializer

    def base_queryset(self):
        return Attachment.objects.select_related("resource", "vendor", "contract", "uploaded_by")

    def get_object(self):
        obj = super().get_object()
        _agent_capability(self.organization, obj.resource)
        if obj.contract_id is not None:
            require_capability(self.organization, CONTRACTS)
        return obj

    def perform_content_negotiation(self, request, force=False):
        # A download answers with the file whatever the client accepts; errors are still problem JSON.
        return super().perform_content_negotiation(request, force=force or self.action == "download")

    def update_object(self, obj, data):
        return attachments.update_attachment(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        attachments.delete_attachment(self.organization, self.request.user, obj)

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        refuse_unknown_params(request)
        attachment = self.get_object()
        if attachment.kind != AttachmentKind.FILE:
            raise exceptions.NotFound("This attachment is a link; open its url instead.")
        handle = attachments.open_file(attachment)
        if handle is None:
            raise exceptions.NotFound("The stored file is missing. An Admin can run manage.py verify_attachments.")
        response = FileResponse(handle, content_type=attachment.content_type or "application/octet-stream")
        response["Content-Disposition"] = attachments.content_disposition(attachment.filename)
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = SANDBOX_CSP
        response["Cache-Control"] = "private, no-store"
        response["Cross-Origin-Resource-Policy"] = "same-origin"
        return response
