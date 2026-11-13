"""Curation: tags in use, saved filters, bulk edit and bulk delete. CSV export and the import template
are actions on the application and component viewsets (catalog/viewsets.py). The rules are in saved_filters.py, bulk.py
and tags.py.

Everyone signed in lists tags and keeps personal saved filters; Editors and Admins share filters and bulk edit; Admins
bulk delete."""

from django.db.models import Count, Q
from django.http import Http404
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.response import Response

from core.drf import (
    ADMIN,
    EDITOR,
    PROBLEMS,
    VIEWER,
    RoleAPIView,
    TenantViewSet,
    etag,
    filter_errors,
    policy,
    revision,
    set_location,
)
from core.problems import FieldErrors

from . import admin_services, bulk, saved_filters, services
from .models import RECORD_KINDS, SavedFilter, Tag
from .serializers import (
    BulkDeleteInputSerializer,
    BulkDeletePreviewSerializer,
    BulkDeleteResultSerializer,
    BulkInputSerializer,
    BulkPreviewSerializer,
    BulkRefusedSerializer,
    BulkResultSerializer,
    SavedFilterInputSerializer,
    SavedFilterSerializer,
    TagInputSerializer,
    TagSerializer,
)

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
DELETE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409)}


@extend_schema_view(
    list=extend_schema(summary="Tags in use, for pickers and filters"),
    retrieve=extend_schema(summary="Get a tag in use, with how many records carry it"),
    partial_update=extend_schema(summary="Set a tag's color and description (Editor, Admin)"),
)
class TagViewSet(TenantViewSet):
    """Tags that at least one record carries, by name. Tags are created by using them on a record
    (catalog/tags.py) and disappear from this list when nothing carries them. Editors and Admins set a tag's color
    (a palette name, not a hex value) and description."""

    role_policy = policy(VIEWER, EDITOR)
    pagination_class = None
    serializer_class = TagSerializer
    input_serializer_class = TagInputSerializer
    ordering_fields = ("name",)
    default_ordering = ("name",)

    def base_queryset(self):
        return Tag.objects.annotate(count=Count("links")).filter(count__gt=0)

    def update_object(self, obj, data):
        return admin_services.update_tag(self.organization, self.request.user, obj, data)


@extend_schema_view(
    list=extend_schema(
        summary="Built-in, shared and personal saved views",
        parameters=[OpenApiParameter("kind", str, enum=list(RECORD_KINDS), required=True)],
        responses={200: SavedFilterSerializer(many=True), **ERRORS},
    ),
    create=extend_schema(
        summary="Save a view (its filter and, optionally, its columns)",
        description="Private to you unless `shared` (Editors and Admins). `columns` are the list's column ids in order "
        "(empty: the list's defaults).",
        responses={201: SavedFilterSerializer, **ERRORS},
    ),
    retrieve=extend_schema(
        summary="Get a shared or personal saved view",
        description="Built-ins are in the list only. Someone else's personal view doesn't exist for you.",
        responses={200: SavedFilterSerializer, **ERRORS},
    ),
    partial_update=extend_schema(
        summary="Change a saved view's name, filter, columns or sharing",
        description="Personal views: their owner. Shared views: their owner while an Editor or Admin, or any Admin. "
        "Sharing needs Editor or Admin. Send only what changes; takes If-Match.",
        responses={200: SavedFilterSerializer, **ERRORS},
    ),
    destroy=extend_schema(
        summary="Delete a saved view",
        description="Personal views: their owner. Shared views: their owner while an Editor or Admin, or any Admin. "
        "Built-ins can't be deleted. Subscriptions to it stay and record a skipped email on their next send day.",
    ),
)
class SavedFilterViewSet(TenantViewSet):
    """Saved views: a name, a list's query parameters (never results) and
    optionally its columns. Built-ins live in code; personal views belong to one user and someone else's personal view
    doesn't exist for you; shared ones are seen by everyone and changed by their owner or an Admin
    (catalog/saved_filters.py enforces it). Any member may keep personal views, hence Viewer writes."""

    role_policy = policy(VIEWER, VIEWER)
    pagination_class = None
    serializer_class = SavedFilterSerializer
    input_serializer_class = SavedFilterInputSerializer
    ordering_fields = ()

    def base_queryset(self):
        return SavedFilter.objects.select_related("user")

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, "swagger_fake_view", False):
            return qs
        return qs.filter(Q(shared=True) | Q(user=self.request.user))

    def get_object(self):
        pk = str(self.kwargs["pk"])
        if pk.startswith(saved_filters.BUILTIN_PREFIX):
            if self.action in ("destroy", "partial_update"):
                raise FieldErrors({"id": ["Built-in filters can't be changed or deleted."]})
            raise Http404  # built-ins live in code and are listed, not fetched by ID
        if not services.is_uuid(pk):
            raise Http404
        found = super().get_object()
        if self.action in ("destroy", "partial_update"):
            # Refused before If-Match is compared, so a refusal for role reads as one rather than a 412.
            saved_filters.require_edit(self.organization, self.request.user, found, self.membership.role)
        return found

    def render(self, rows, *, many: bool, serializer_class=None):
        org, user, role = self.organization, self.request.user, self.membership.role
        if many:
            return [saved_filters.filter_json(f, user, role, saved_filters.problems_of(f, org)) for f in rows]
        return saved_filters.view_json(org, rows, user, role)

    def fresh(self, obj):
        """After a change, by ID in the organization: a view an Admin just made private is its owner's now, but the
        response still shows what was saved."""
        return SavedFilter.objects.select_related("user").get(organization=self.organization, pk=obj.pk)

    def update_object(self, obj, data):
        return saved_filters.update(self.organization, self.request.user, obj, data)

    def list(self, request, *args, **kwargs):
        errors = filter_errors(None, request.query_params, {"kind"})
        kind = request.query_params.get("kind")
        if kind not in RECORD_KINDS:
            errors["kind"] = ["Use application, component or ai_agent."]
        if errors:
            raise FieldErrors(errors)
        return Response(saved_filters.listing_for(self.organization, request.user, kind))

    def create(self, request, *args, **kwargs):
        saved = saved_filters.create(self.organization, request.user, self.payload())
        response = Response(
            saved_filters.filter_json(saved, request.user, self.membership.role), status=status.HTTP_201_CREATED
        )
        response["ETag"] = etag(revision(saved))
        set_location(request, response, f"{request.path_info.rstrip('/')}/{saved.pk}")
        return response

    def destroy_object(self, obj):
        saved_filters.delete(self.organization, self.request.user, obj.pk)


class BulkPreviewView(RoleAPIView):
    role_policy = policy(EDITOR)

    @extend_schema(
        summary="Preview a bulk edit (Editors and Admins)",
        description="Per record before and after, whether it is unchanged, and whether it will need confirmation. "
        "Writes nothing. A record with errors (archived, or no review to date) is listed with its errors.",
        request=BulkInputSerializer,
        responses={200: BulkPreviewSerializer, **ERRORS},
    )
    def post(self, request):
        return Response(bulk.preview(self.organization, request.user, self.payload()))


class BulkApplyView(RoleAPIView):
    role_policy = policy(EDITOR)

    @extend_schema(
        summary="Apply a bulk edit (Editors and Admins)",
        description="One transaction; each changed record gets its own `resource.updated` audit event marked bulk. "
        "The selected records are locked while it runs, so a concurrent If-Match write waits and then sees the new "
        "revision. If any record has errors, nothing is saved and the 400 problem lists them in `rows` "
        "(`{id, name, errors, codes, field_errors}`; `codes` gives each message a stable code, such as "
        "retire_dependents). Retiring records go dependents first.",
        request=BulkInputSerializer,
        responses={
            200: BulkResultSerializer,
            **ERRORS,
            400: OpenApiResponse(BulkRefusedSerializer, description="Field errors, or refused records in `rows`"),
        },
    )
    def post(self, request):
        return Response(bulk.apply(self.organization, request.user, self.payload()))


class BulkDeletePreviewView(RoleAPIView):
    role_policy = policy(ADMIN)

    @extend_schema(
        summary="Preview a bulk delete (Admin)",
        description="Per record whether it can be deleted, the references that block it (the single delete's rule: "
        "reviews, instances, relationships, integrations, assessments, discovery links and so on) and the attachments "
        "and data-quality exceptions that would be deleted with it. Writes nothing. Up to 200 records of one kind.",
        request=BulkDeleteInputSerializer,
        responses={200: BulkDeletePreviewSerializer, **ERRORS},
    )
    def post(self, request):
        return Response(bulk.delete_preview(self.organization, request.user, self.payload()))


class BulkDeleteApplyView(RoleAPIView):
    role_policy = policy(ADMIN)

    @extend_schema(
        summary="Delete records in bulk (Admin)",
        description="One transaction; each record is deleted as a single delete would be (its attachments and "
        "exceptions removed and audited) and gets its own `resource.deleted` audit event marked bulk. If any record "
        "is referenced, nothing is deleted and the 409 problem lists each blocked record with its "
        "`blocking_references` in `rows`.",
        request=BulkDeleteInputSerializer,
        responses={200: BulkDeleteResultSerializer, **DELETE_ERRORS},
    )
    def post(self, request):
        return Response(bulk.delete_apply(self.organization, request.user, self.payload()))
