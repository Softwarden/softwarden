"""Catalog viewsets: one Resource viewset per kind (applications, components,
integrations) and viewsets for reviews, application instances, relationships and change assessments, plus the
dependency explorer as a read-only composite. Routes are in catalog/urls.py with the same URLs and response shapes as
the function views they replaced.

Roles: Viewer reads; Editor creates, updates, archives, restores and confirms; Admin deletes records and integrations.
Relationships and instances are removed by Editors, as before. Every write goes through a service function."""

from typing import Any, ClassVar
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import F, Prefetch
from django.http import HttpResponse, QueryDict
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    PolymorphicProxySerializer,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from core.capabilities import AI_AGENTS
from core.dates import org_today
from core.drf import (
    ADMIN,
    CSV_FILE,
    EDITOR,
    PAGING_PARAMS,
    PROBLEMS,
    VIEWER,
    KeysetPagination,
    RoleAPIView,
    TenantViewSet,
    file_response,
    filter_errors,
    policy,
    refuse_unknown_params,
    revision,
    unknown_params,
)
from core.problems import FieldErrors
from imports import csvio, xlsxio

from . import assessment_render, compliance, customfields, graph, links, quality, record_map, services
from . import assessments as assessments_service
from . import tags as tags_service
from .filters import (
    AIAgentFilterSet,
    ApplicationFilterSet,
    AssessmentFilterSet,
    ComponentFilterSet,
    IntegrationFilterSet,
    RecordFilterSet,
    RelationshipFilterSet,
    latest_reviews,
    relationship_limit,
)
from .models import (
    KIND_PATHS,
    RECORD_KINDS,
    AgentModel,
    AIModel,
    ApplicationInstance,
    AssessmentSnapshot,
    ChangeAssessment,
    CustomFieldType,
    Kind,
    LinkConfirmation,
    RecordReview,
    Relationship,
    RelationshipKind,
    Resource,
    ResourceTag,
)
from .querysets import INTEGRATION_RELATED, RELATIONSHIP_RELATED, resources_for
from .serializers import (
    AIAgentDetailSerializer,
    AIAgentInputSerializer,
    AIAgentSerializer,
    ApplicationDetailSerializer,
    ApplicationInputSerializer,
    ApplicationSerializer,
    AssessmentInputSerializer,
    AssessmentListSerializer,
    AssessmentSerializer,
    AssessmentStatusInputSerializer,
    ComponentDetailSerializer,
    ComponentInputSerializer,
    ComponentSerializer,
    ConfirmationInputSerializer,
    ConfirmationSerializer,
    DependenciesSerializer,
    InstanceInputSerializer,
    InstanceSerializer,
    IntegrationDetailSerializer,
    IntegrationInputSerializer,
    IntegrationSerializer,
    RecordMapSerializer,
    RelationshipInputSerializer,
    RelationshipLimitedSerializer,
    RelationshipSerializer,
    ReviewInputSerializer,
    ReviewSerializer,
    SnapshotSerializer,
    SnapshotSummarySerializer,
)
from .state import review_state

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409)}


# ---- Link confirmations (relationships and integrations) ------------------------------------------------------------


class ConfirmationsMixin:
    """GET the confirmation history (newest first) or POST a new confirmation (Editor, Admin) of the link this
    viewset's record is. `link_of(obj)` and `confirmation_column` name the link."""

    confirmation_column: ClassVar[str]

    def link_of(self, obj):
        return obj

    def confirmations_response(self: Any, request):
        obj = self.get_object()
        link = self.link_of(obj)
        if request.method == "POST":
            data = self.payload()
            with transaction.atomic():
                confirmation = services.confirm_link(self.organization, request.user, link, data)
                return Response(ConfirmationSerializer(confirmation).data, status=status.HTTP_201_CREATED)
        refuse_unknown_params(request)
        history = LinkConfirmation.objects.filter(
            organization=self.organization, **{self.confirmation_column: link.pk}
        ).select_related("confirmed_by__department", "recorded_by")
        return Response(ConfirmationSerializer(history, many=True).data)


def confirmations_docs(noun: str):
    """OpenAPI for a `confirmations` action: GET the history, POST a confirmation."""

    def decorate(func):
        func = extend_schema(
            methods=["POST"],
            summary=f"Confirm {noun} (Editor, Admin)",
            request=ConfirmationInputSerializer,
            responses={201: ConfirmationSerializer, **WRITE_ERRORS},
        )(func)
        return extend_schema(
            methods=["GET"],
            summary=f"Confirmation history of {noun}, newest first",
            responses={200: ConfirmationSerializer(many=True), **ERRORS},
        )(func)

    return decorate


class LinkConfirmationViewSet(TenantViewSet):
    """One confirmation of a link, by ID, under the link it confirms (the history and recording are the link's
    `confirmations` action). Confirmations are never edited or deleted; removing the link removes them."""

    role_policy = policy(VIEWER)
    pagination_class = None
    serializer_class = ConfirmationSerializer
    lookup_url_kwarg = "confirmation_pk"
    confirmation_column: ClassVar[str]

    def base_queryset(self):
        return LinkConfirmation.objects.select_related("confirmed_by__department", "recorded_by")

    def link_id(self):
        raise NotImplementedError

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, "swagger_fake_view", False):
            return qs
        return qs.filter(**{self.confirmation_column: self.link_id()})


@extend_schema_view(retrieve=extend_schema(summary="Get one confirmation of a relationship"))
class RelationshipConfirmationViewSet(LinkConfirmationViewSet):
    confirmation_column = "relationship"

    def link_id(self):
        return get_object_or_404(Relationship, organization=self.organization, id=self.kwargs["pk"]).pk


@extend_schema_view(retrieve=extend_schema(summary="Get one confirmation of an integration"))
class IntegrationConfirmationViewSet(LinkConfirmationViewSet):
    confirmation_column = "integration"

    def link_id(self):
        found = get_object_or_404(Resource, organization=self.organization, kind=Kind.INTEGRATION, id=self.kwargs["pk"])
        return found.pk  # the Integration row's key is its Resource's


def record_revision(obj) -> str:
    """A record's ETag revision: the row, its extension and its tags, and an AI agent's models. Shared with the writes
    other apps make to a record under If-Match (discovery's drift apply and missing archive)."""
    extra: dict[str, Any] = {
        "tags": sorted(str(t) for t in ResourceTag.objects.filter(resource=obj).values_list("tag_id", flat=True))
    }
    if obj.kind == Kind.AI_AGENT:
        extra["models_used"] = sorted(
            str(m) for m in AgentModel.objects.filter(agent_id=obj.pk).values_list("model_id", flat=True)
        )
    return revision(obj, obj.extension, extra=extra)


# ---- applications, components and integrations: one viewset per Resource kind -------------------------------------


class ResourceViewSet(TenantViewSet):
    """What every Resource kind shares: the kind's rows, archive and restore (Editors and Admins, idempotent),
    delete for Admins, and a revision over the row, its extension and its tags."""

    kind: ClassVar[Kind]
    role_policy = policy(VIEWER, EDITOR, destroy=ADMIN)
    # Non-null columns only (keyset pagination). Without ?ordering: relevance when searching, then name.
    ordering_fields: ClassVar[tuple[str, ...]] = ("name", "lifecycle", "created_at", "updated_at")
    default_ordering = ("rank_bucket", "name")
    # Sorted by meaning, not alphabetically (core.drf.KeysetPagination.ranked).
    ordering_ranks: ClassVar[dict[str, tuple[str, ...]]] = {
        "lifecycle": ("proposed", "evaluating", "active", "deprecated", "retired"),
        "criticality": ("critical", "high", "medium", "low"),
    }

    def revision_of(self, obj) -> str:
        return record_revision(obj)

    def destroy_object(self, obj):
        services.delete_resource(self.organization, self.request.user, obj)

    def _set_archived(self, archive: bool):
        with transaction.atomic():
            obj = self.get_object()
            (services.archive_resource if archive else services.restore_resource)(
                self.organization, self.request.user, obj
            )
            return self.detail_response(self.fresh(obj))

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        return self._set_archived(True)

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        return self._set_archived(False)


CSV_TYPE = "text/csv; charset=utf-8"
# Typed Excel columns (the rest are text): yes/no as booleans, dates as dates.
XLSX_TYPES = {
    "sso_enabled": xlsxio.BOOLEAN,
    "scim_provisioning": xlsxio.BOOLEAN,
    "is_gxp": xlsxio.BOOLEAN,
    "last_periodic_review": xlsxio.DATE,
    "next_periodic_review": xlsxio.DATE,
    "human_in_loop": xlsxio.BOOLEAN,
    "actions_can_write": xlsxio.BOOLEAN,
}
# Custom field types with a typed Excel cell (the rest are text).
CUSTOM_XLSX_TYPES: dict[str, str] = {
    CustomFieldType.INTEGER: xlsxio.NUMBER,
    CustomFieldType.DECIMAL: xlsxio.NUMBER,
    CustomFieldType.BOOLEAN: xlsxio.BOOLEAN,
    CustomFieldType.DATE: xlsxio.DATE,
}
SHEET_NAMES: dict[str, str] = {
    Kind.APPLICATION: "Applications",
    Kind.COMPONENT: "Components",
    Kind.AI_AGENT: "AI agents",
}


def csv_response(data, name):
    response = HttpResponse(data, content_type=CSV_TYPE)
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


class RecordViewSet(ResourceViewSet):
    """Applications and components (records): review state, tags, CSV export and the import template."""

    related: ClassVar[tuple[str, ...]]
    filterset_class: ClassVar[type[RecordFilterSet]]
    role_policy = policy(VIEWER, EDITOR, destroy=ADMIN, template=EDITOR)
    ordering_fields: ClassVar[tuple[str, ...]] = (
        "name",
        "lifecycle",
        "criticality",
        "created_at",
        "updated_at",
        "completeness",
    )

    def filter_queryset(self, queryset):
        """The data-quality score is computed only when the ordering asks for it (filters annotate it themselves)."""
        qs = super().filter_queryset(queryset)
        wanted = {p.strip().lstrip("-") for p in self.request.query_params.get("ordering", "").split(",")}
        return quality.annotate_score(qs, self.organization) if "completeness" in wanted else qs

    def base_queryset(self):
        return Resource.objects.filter(kind=self.kind).select_related(
            "business_owner__department",
            "technical_owner__department",
            "support_team",
            "it_steward",
            "department",
            *self.related,
        )

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        ids = [r.id for r in rows]
        return {
            "today": org_today(self.organization),
            "latest": latest_reviews(self.organization, ids),
            "tags": tags_service.names_for(ids),
            "compliance": compliance.enabled(self.organization),
            "custom_fields": customfields.for_kind(self.organization, self.kind),
            "quality": quality.evaluate(self.organization, ids),
        }

    # -- service hooks (catalog/services.py validates, scopes and audits)

    def create_object(self, data):
        services.warn_duplicate_name(self.organization, self.kind, data)
        return services.create_resource(self.organization, self.request.user, {**data, "kind": self.kind})

    def update_object(self, obj, data):
        if "name" in data:
            services.warn_duplicate_name(self.organization, self.kind, data, obj)
        return services.update_resource(self.organization, self.request.user, obj, data)

    def partial_update(self, request, *args, **kwargs):
        try:
            return super().partial_update(request, *args, **kwargs)
        except services.StaleValue as refused:
            # The refused Undo is audited too, after the update's transaction rolled back.
            services.audit(
                self.organization, request.user, "resource.undo_refused", self.get_object(), {"fields": refused.fields}
            )
            raise

    # -- CSV and Excel export (any role) and the import template (Editors and Admins)

    def export_rows(self, request) -> tuple[list[Any], dict[str, str]]:
        """The records an export holds and the filters it used: the list's filters (validated by the list's FilterSet)
        plus include_archived; paging parameters don't apply to an export."""
        params = {k: request.query_params[k] for k in request.query_params}
        include = params.pop("include_archived", None)
        if include not in (None, "true", "false"):
            raise FieldErrors({"include_archived": ["Use one of: true, false."]})
        if include == "true" and "archived" not in params:
            params["archived"] = "all"
        query = QueryDict(urlencode(params))
        filterset = self.filterset_class(query, queryset=self.get_queryset(), request=request)
        errors = filter_errors(filterset, params, PAGING_PARAMS)
        if errors:
            raise FieldErrors(errors)
        rows = list(filterset.qs.order_by("name", "id"))
        return rows, {k: v for k, v in params.items() if k not in PAGING_PARAMS}

    def audit_export(self, rows, filters, extra=None):
        """Audited as `export` with counts and filters, never contents."""
        changes = {"kind": self.kind, "rows": len(rows), "filters": filters, **(extra or {})}
        services.audit(self.organization, self.request.user, "export", None, changes)

    def export_name(self, suffix: str) -> str:
        return f"softwarden-{KIND_PATHS[self.kind]}-{org_today(self.organization).isoformat()}.{suffix}"

    @action(detail=False, methods=["get"], url_path="export.csv")
    def export(self, request):
        rows, filters = self.export_rows(request)
        names = tags_service.names_for(r.id for r in rows)
        with_compliance = compliance.enabled(self.organization)
        custom = customfields.for_kind(self.organization, self.kind)
        data = csvio.write_csv(
            csvio.columns(self.kind, with_compliance, custom),
            (csvio.record_values(r, names.get(r.id, ()), with_compliance, custom) for r in rows),
        )
        self.audit_export(rows, filters)
        return csv_response(data, self.export_name("csv"))

    @action(detail=False, methods=["get"], url_path="export.xlsx")
    def export_xlsx(self, request):
        """The CSV export's rows and columns as a typed workbook, plus the review state, next review date and last
        change; further sheets from xlsxio.EXTRA_SHEETS (custom fields)."""
        rows, filters = self.export_rows(request)
        org = self.organization
        names = tags_service.names_for(r.id for r in rows)
        latest = latest_reviews(org, [r.id for r in rows])
        today = org_today(org)
        zone = ZoneInfo(org.timezone)
        with_compliance = compliance.enabled(org)
        custom = customfields.for_kind(org, self.kind)
        columns = [xlsxio.Column(c, XLSX_TYPES.get(c, xlsxio.TEXT)) for c in csvio.columns(self.kind, with_compliance)]
        columns += [
            xlsxio.Column(customfields.column(cf), CUSTOM_XLSX_TYPES.get(cf.type, xlsxio.TEXT)) for cf in custom
        ]
        columns += [
            xlsxio.Column("review_state"),
            xlsxio.Column("next_review_date", xlsxio.DATE),
            xlsxio.Column("updated_at", xlsxio.DATETIME),
        ]

        def values(r):
            review = latest.get(r.id)
            stored = r.custom_field_data or {}
            return {
                **csvio.record_values(r, names.get(r.id, ()), with_compliance),
                **{customfields.column(cf): customfields.excel_value(cf, stored.get(cf.name)) for cf in custom},
                "review_state": review_state(r, review, today)[0],
                "next_review_date": review.next_review_date if review else None,
                "updated_at": r.updated_at.astimezone(zone),
            }

        main = xlsxio.Sheet(SHEET_NAMES[self.kind], columns, (values(r) for r in rows))
        data = xlsxio.write_xlsx([main, *xlsxio.extra_sheets(org, self.kind, rows)])
        self.audit_export(rows, filters, {"format": "xlsx"})
        response = HttpResponse(data, content_type=xlsxio.XLSX_TYPE)
        response["Content-Disposition"] = f'attachment; filename="{self.export_name("xlsx")}"'
        return response

    @action(detail=False, methods=["get"], url_path="template.csv")
    def template(self, request):
        refuse_unknown_params(request)
        return csv_response(
            csvio.write_csv(csvio.columns_for(self.organization, self.kind), []),
            f"softwarden-{KIND_PATHS[self.kind]}-template.csv",
        )


def _documented(noun: str, detail, plural: str):
    article = "an" if noun[0].lower() in "aeiou" else "a"
    ok = {200: detail, **ERRORS}
    csv = {**CSV_FILE, **ERRORS}
    xlsx = {**file_response(xlsxio.XLSX_TYPE, "An Excel workbook attachment."), **ERRORS}
    return extend_schema_view(
        list=extend_schema(summary=f"List {plural}", description=customfields.FILTERS_DOC),
        create=extend_schema(summary=f"Create {article} {noun} (Editor, Admin)"),
        retrieve=extend_schema(summary=f"Get {article} {noun}"),
        partial_update=extend_schema(summary=f"Update {article} {noun} (Editor, Admin)"),
        destroy=extend_schema(summary=f"Delete {article} {noun} (Admin; refused while referenced)"),
        archive=extend_schema(summary=f"Archive {article} {noun} (Editor, Admin)", responses=ok),
        restore=extend_schema(summary=f"Restore an archived {noun} (Editor, Admin)", responses=ok),
        export=extend_schema(
            summary=f"Export the filtered {plural} as CSV",
            description="Same filter parameters as the list (cursor and page_size are ignored) plus "
            "include_archived, and the cf_<name> custom field filters. UTF-8 with a BOM, template columns including "
            "softwarden_id and one cf_<name> column per custom field, formula-safe cells. Audited as `export` (counts "
            "and filters, never contents).",
            parameters=[OpenApiParameter("include_archived", bool, description="Include archived records.")],
            responses=csv,
            filters=True,
        ),
        export_xlsx=extend_schema(
            summary=f"Export the filtered {plural} as an Excel workbook",
            description="The CSV export's filters, rows and columns (cursor and page_size are ignored; "
            "include_archived), plus review_state, next_review_date and updated_at. A bold, frozen header row with a "
            "filter; dates are dates and yes/no fields booleans; text cells are formula-safe (the CSV escape, stored "
            "as text). Compliance profile columns only while the profile is on. One typed cf_<name> column per custom "
            "field, and a second sheet, Custom fields, describing them. Audited as `export` with format xlsx.",
            parameters=[OpenApiParameter("include_archived", bool, description="Include archived records.")],
            responses=xlsx,
            filters=True,
        ),
        template=extend_schema(summary=f"Download the {noun} import template (headings only)", responses=csv),
    )


@_documented("application", ApplicationDetailSerializer, "applications")
class ApplicationViewSet(RecordViewSet):
    kind = Kind.APPLICATION
    related = ("application__vendor", "application__category")
    serializer_class = ApplicationSerializer
    detail_serializer_class = ApplicationDetailSerializer
    input_serializer_class = ApplicationInputSerializer
    filterset_class = ApplicationFilterSet


@_documented("component", ComponentDetailSerializer, "components")
class ComponentViewSet(RecordViewSet):
    kind = Kind.COMPONENT
    related = ("component",)
    serializer_class = ComponentSerializer
    detail_serializer_class = ComponentDetailSerializer
    input_serializer_class = ComponentInputSerializer
    filterset_class = ComponentFilterSet


# The risk classes from most to least severe; a blank one sorts last either way (core.drf.KeysetPagination.ranked).
RISK_RANK = ("unacceptable", "high", "limited", "minimal")


@_documented("AI agent", AIAgentDetailSerializer, "AI agents")
class AIAgentViewSet(RecordViewSet):
    """AI agents: records like applications and components, with the agent's platform, models,
    channels and governance fields. Agents are exported as CSV and Excel; CSV import of agents comes later, so there
    is no template route."""

    kind = Kind.AI_AGENT
    related = ("ai_agent",)
    required_capability = AI_AGENTS
    serializer_class = AIAgentSerializer
    detail_serializer_class = AIAgentDetailSerializer
    input_serializer_class = AIAgentInputSerializer
    filterset_class = AIAgentFilterSet
    ordering_fields = (*RecordViewSet.ordering_fields, "risk_classification")
    ordering_ranks = {**ResourceViewSet.ordering_ranks, "risk_classification": RISK_RANK}

    def base_queryset(self):
        # risk_classification is annotated so it can be a ranked sort key; models in one query for the whole page.
        models = AIModel.objects.select_related("provider").order_by("name", "version", "id")
        return (
            super()
            .base_queryset()
            .annotate(risk_classification=F("ai_agent__risk_classification"))
            .prefetch_related(Prefetch("ai_agent__models_used", queryset=models))
        )

    def revision_of(self, obj) -> str:
        return record_revision(obj)


def implementers(org, ids):
    """{integration_id: [implementing components]}. Vendor-native is the absence of any `implements` link."""
    found: dict = {}
    rows = Relationship.objects.filter(organization=org, type__slug=RelationshipKind.IMPLEMENTS, target_id__in=ids)
    for link in rows.select_related("source").order_by("source__name"):
        found.setdefault(link.target_id, []).append(
            {"id": str(link.source_id), "name": link.source.name, "archived": link.source.archived_at is not None}
        )
    return found


@extend_schema_view(
    list=extend_schema(summary="List integrations", description=customfields.FILTERS_DOC),
    create=extend_schema(summary="Create an integration (Editor, Admin)"),
    retrieve=extend_schema(summary="Get an integration"),
    partial_update=extend_schema(summary="Update an integration (Editor, Admin)"),
    destroy=extend_schema(summary="Delete an integration (Admin; refused while a link references it)"),
    archive=extend_schema(
        summary="Archive an integration (Editor, Admin)", responses={200: IntegrationDetailSerializer, **ERRORS}
    ),
    restore=extend_schema(
        summary="Restore an integration (Editor, Admin)", responses={200: IntegrationDetailSerializer, **ERRORS}
    ),
)
class IntegrationViewSet(ConfirmationsMixin, ResourceViewSet):
    """Integrations: a Resource kind with source and target ends, link state and implementers."""

    kind = Kind.INTEGRATION
    serializer_class = IntegrationSerializer
    detail_serializer_class = IntegrationDetailSerializer
    input_serializer_class = IntegrationInputSerializer
    filterset_class = IntegrationFilterSet
    confirmation_column = "integration"

    def base_queryset(self):
        return Resource.objects.filter(kind=self.kind).select_related(*INTEGRATION_RELATED)

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        org = self.organization
        latest = links.latest_confirmations(org, integration_ids=[r.id for r in rows])
        today = services.org_today(org)
        return {
            "custom_fields": customfields.for_kind(org, self.kind),
            "implementers": implementers(org, [r.id for r in rows]),
            "states": {
                r.id: links.integration_state_fields(org, r, latest.get(("integration", r.id)), today) for r in rows
            },
        }

    def get_serializer_class(self):
        if self.action == "create":  # the create response has always carried archived_at and archived_by
            return self.detail_serializer_class
        return super().get_serializer_class()

    def create_object(self, data):
        services.warn_duplicate_name(self.organization, Kind.INTEGRATION, data)
        return services.create_integration(self.organization, self.request.user, data)

    def create(self, request, *args, **kwargs):
        data = self.payload()
        with transaction.atomic():
            obj = self.create_object(data)
            return self.detail_response(self.fresh(obj), status_code=status.HTTP_201_CREATED)

    def update_object(self, obj, data):
        if "name" in data:
            services.warn_duplicate_name(self.organization, Kind.INTEGRATION, data, obj)
        services.update_integration(self.organization, self.request.user, obj, data)
        return obj

    def link_of(self, obj):
        return obj.integration

    @confirmations_docs("an integration")
    @action(detail=True, methods=["get", "post"])
    def confirmations(self, request, pk=None):
        return self.confirmations_response(request)


# ---- relationships ----------------------------------------------------------------------------------------------------


@extend_schema_view(
    list=extend_schema(
        summary="List relationships",
        description="Without `limit`, a plain array of every link the filters match (oldest first unless "
        "`ordering` says otherwise). With `limit` (1 to 500), an object: `results` holds the first `limit` links in "
        "the same order (ties broken by id), `count` every matching link and `has_more` whether there are more.",
        responses={
            200: PolymorphicProxySerializer(
                component_name="RelationshipList",
                serializers=[RelationshipSerializer(many=True), RelationshipLimitedSerializer],
                resource_type_field_name=None,
                many=False,
            ),
            **ERRORS,
        },
    ),
    create=extend_schema(summary="Create a relationship (Editor, Admin)"),
    retrieve=extend_schema(summary="Get a relationship"),
    partial_update=extend_schema(summary="Update a relationship (Editor, Admin)"),
    destroy=extend_schema(summary="Remove a relationship (Editor, Admin)"),
)
class RelationshipViewSet(ConfirmationsMixin, TenantViewSet):
    """Typed, directed links between records. A plain array, oldest first, filtered by record and type."""

    role_policy = policy(VIEWER, EDITOR)
    pagination_class = None
    serializer_class = RelationshipSerializer
    input_serializer_class = RelationshipInputSerializer
    filterset_class = RelationshipFilterSet
    ordering_fields = ("created_at", "updated_at")
    default_ordering = ("created_at",)
    confirmation_column = "relationship"

    def base_queryset(self):
        return Relationship.objects.select_related(*RELATIONSHIP_RELATED)

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        org = self.organization
        latest = links.latest_confirmations(org, relationship_ids=[r.id for r in rows])
        today = services.org_today(org)
        return {
            "states": {
                r.id: links.relationship_state_fields(org, r, latest.get(("relationship", r.id)), today) for r in rows
            }
        }

    def list(self, request, *args, **kwargs):
        """The plain array, or with `?limit=` the first links and the total (the relationship-type in-use
        dialog shows 20 of what may be thousands). The filter backend has validated limit by then."""
        if "limit" not in request.query_params:
            return super().list(request, *args, **kwargs)
        queryset = self.filter_queryset(self.get_queryset())
        limit = relationship_limit(request.query_params["limit"])
        ranked, keys = KeysetPagination.ranked(queryset, KeysetPagination.ordering(request, self), self)
        rows = list(ranked.order_by(*KeysetPagination._order_by(keys))[:limit])
        count = len(rows) if len(rows) < limit else queryset.count()
        return Response({"results": self.render(rows, many=True), "count": count, "has_more": count > len(rows)})

    def create_object(self, data):
        return services.create_relationship(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        services.update_relationship(self.organization, self.request.user, obj, data)
        return obj

    def destroy_object(self, obj):
        services.delete_relationship(self.organization, self.request.user, obj)

    @confirmations_docs("a relationship")
    @action(detail=True, methods=["get", "post"])
    def confirmations(self, request, pk=None):
        return self.confirmations_response(request)


# ---- records' nested rows: reviews and application instances ---------------------------------------------------------


class NestedRecordMixin:
    """A viewset under /<records>/<pk>/...: the parent record of `parent_kind` in the request organization, or 404."""

    parent_kind: ClassVar[str]

    def parent(self: Any):
        if not hasattr(self, "_parent"):
            self._parent = get_object_or_404(resources_for(self.organization, self.parent_kind), id=self.kwargs["pk"])
        return self._parent


class ReviewViewSet(NestedRecordMixin, TenantViewSet):
    """A record's review history (newest first) and recording a review (Editor, Admin). Identical for both kinds;
    reviews are never edited or deleted."""

    role_policy = policy(VIEWER, EDITOR)
    pagination_class = None
    serializer_class = ReviewSerializer
    input_serializer_class = ReviewInputSerializer
    lookup_url_kwarg = "review_pk"
    ordering_fields = ("confirmation_date", "created_at")
    default_ordering = ("-confirmation_date", "-created_at")

    def base_queryset(self):
        return RecordReview.objects.select_related("confirmed_by__department", "recorded_by")

    def get_queryset(self):
        qs = super().get_queryset()
        return qs if getattr(self, "swagger_fake_view", False) else qs.filter(resource=self.parent())

    def create_object(self, data):
        return services.record_review(self.organization, self.request.user, self.parent(), data)


@extend_schema_view(
    list=extend_schema(summary="Review history of an application, newest first"),
    create=extend_schema(summary="Record a review of an application (Editor, Admin)"),
    retrieve=extend_schema(summary="Get one review of an application"),
)
class ApplicationReviewViewSet(ReviewViewSet):
    parent_kind = Kind.APPLICATION


@extend_schema_view(
    list=extend_schema(summary="Review history of a component, newest first"),
    create=extend_schema(summary="Record a review of a component (Editor, Admin)"),
    retrieve=extend_schema(summary="Get one review of a component"),
)
class ComponentReviewViewSet(ReviewViewSet):
    parent_kind = Kind.COMPONENT


@extend_schema_view(
    list=extend_schema(summary="Review history of an AI agent, newest first"),
    create=extend_schema(summary="Record a review of an AI agent (Editor, Admin)"),
    retrieve=extend_schema(summary="Get one review of an AI agent"),
)
class AIAgentReviewViewSet(ReviewViewSet):
    parent_kind = Kind.AI_AGENT
    required_capability = AI_AGENTS


@extend_schema_view(
    list=extend_schema(summary="Instances of an application"),
    create=extend_schema(summary="Add an instance (Editor, Admin)"),
    retrieve=extend_schema(summary="Get an instance"),
    partial_update=extend_schema(summary="Update an instance (Editor, Admin)"),
    destroy=extend_schema(summary="Remove an instance (Editor, Admin; refused while a link names it)"),
)
class InstanceViewSet(NestedRecordMixin, TenantViewSet):
    """Deployments of an application, such as Production or Sandbox; Editors and Admins add, edit and remove them."""

    parent_kind = Kind.APPLICATION
    role_policy = policy(VIEWER, EDITOR)
    pagination_class = None
    serializer_class = InstanceSerializer
    input_serializer_class = InstanceInputSerializer
    lookup_url_kwarg = "instance_pk"
    ordering_fields = ("name", "created_at")
    default_ordering = ("name",)

    def base_queryset(self):
        return ApplicationInstance.objects.select_related("application")

    def get_queryset(self):
        qs = super().get_queryset()
        return qs if getattr(self, "swagger_fake_view", False) else qs.filter(application=self.parent())

    def create_object(self, data):
        return services.create_instance(self.organization, self.request.user, self.parent(), data)

    def update_object(self, obj, data):
        return services.update_instance(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        services.delete_instance(self.organization, self.request.user, obj)


# ---- change assessments ------------------------------------------------------------------------------------------------

SUMMARY = {200: AssessmentListSerializer, **WRITE_ERRORS}


@extend_schema_view(
    list=extend_schema(summary="List change assessments"),
    create=extend_schema(
        summary="Create an assessment and compute its first snapshot (Editor, Admin)",
        description="Returns the assessment with the snapshot summary; GET the assessment for the snapshot rows.",
    ),
    retrieve=extend_schema(
        summary="Get an assessment with its current snapshot and snapshot history",
        description="A missing path is not evidence that a change is safe.",
    ),
    partial_update=extend_schema(
        summary="Edit an assessment's text, owner, date or ticket (Editor, Admin)",
        responses={**SUMMARY, 412: PROBLEMS[412]},
    ),
    status=extend_schema(
        summary="Change an assessment's status (Editor, Admin)",
        request=AssessmentStatusInputSerializer,
        responses=SUMMARY,
    ),
    refresh=extend_schema(
        summary="Refresh the snapshot (Editor, Admin)",
        description="Takes a new current snapshot and keeps the old one. This is the only thing that clears a stale "
        "flag.",
        request=None,
        responses={201: AssessmentListSerializer, **WRITE_ERRORS},
    ),
    snapshots=extend_schema(
        summary="Snapshot history of an assessment, newest first",
        responses={200: SnapshotSummarySerializer(many=True), **ERRORS},
    ),
    snapshot=extend_schema(
        summary="One snapshot with its nodes and links (read-only)", responses={200: SnapshotSerializer, **ERRORS}
    ),
)
class AssessmentViewSet(TenantViewSet):
    """Change and retirement assessments. The list, create and every write return the list
    shape (the current snapshot's summary); GET returns the snapshot rows and history too
    (catalog/assessment_render.py). Assessments are canceled, never deleted."""

    role_policy = policy(VIEWER, EDITOR)
    pagination_class = None
    serializer_class = AssessmentListSerializer
    detail_serializer_class = AssessmentSerializer
    input_serializer_class = AssessmentInputSerializer
    filterset_class = AssessmentFilterSet
    ordering_fields = ("updated_at", "created_at", "title")
    default_ordering = ("-updated_at",)

    def base_queryset(self):
        return ChangeAssessment.objects.select_related("target", "owner")

    def render(self, rows, *, many: bool, serializer_class=None) -> Any:
        rows = list(rows) if many else [rows]
        if not many and self.action == "retrieve":
            from tickets import reads as ticket_reads

            body = assessment_render.assessment_detail(self.organization, rows[0])
            body["ticket_links"] = ticket_reads.for_assessment(self.organization, rows[0])
            return body
        data = assessment_render.assessment_rows(self.organization, rows)
        return data if many else data[0]

    def create_object(self, data):
        return assessments_service.create_assessment(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        assessments_service.update_assessment(self.organization, self.request.user, obj, data)
        return obj

    @action(detail=True, methods=["post"])
    def status(self, request, pk=None):
        data = self.payload()
        with transaction.atomic():
            obj = self.get_object()
            assessments_service.change_status(self.organization, request.user, obj, data)
            return self.detail_response(self.fresh(obj))

    @action(detail=True, methods=["post"])
    def refresh(self, request, pk=None):
        obj = self.get_object()
        assessments_service.refresh_snapshot(self.organization, request.user, obj)
        return self.detail_response(self.fresh(obj), status_code=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def snapshots(self, request, pk=None):
        refuse_unknown_params(request)
        return Response(assessment_render.snapshot_history(self.organization, self.get_object()))

    @action(detail=True, methods=["get"])
    def snapshot(self, request, pk=None, snapshot_pk=None):
        refuse_unknown_params(request)
        assessment = self.get_object()
        found = get_object_or_404(
            AssessmentSnapshot.objects.select_related("taken_by"),
            organization=self.organization,
            assessment=assessment,
            id=snapshot_pk,
        )
        return Response(assessment_render.snapshot_detail(self.organization, found))


# ---- the dependency explorer and a record's Map (read-only composites) -------------------------------------------------

DEPENDENCY_DOC = (
    "Dependents (what depends on the target), depends-on (what it depends on) or both, through links whose "
    "relationship type has `is_dependency` (the core `depends_on` and any type an Admin marked as a dependency); "
    "cycle-safe, one shortest path per record. A link that leads back to a record already on the path is a row with "
    "`cycle` true, and every such link is also in `cycles`, so a graph can mark the edges that close a loop. "
    "Integrations touching the target or a listed record are returned one hop out as data flows with their "
    "implementers. A missing path is not evidence that a change is safe."
)
DEPENDENCY_PARAMS = [
    OpenApiParameter("direction", str, enum=["dependents", "depends_on", "both"], description="Default dependents."),
    OpenApiParameter(
        "depth", str, description=f"1 to {graph.MAX_DEPTH} links from the target, or all (no limit). Default 1."
    ),
    OpenApiParameter("include_retired", bool, description="Default false: retired records are left out."),
    OpenApiParameter("show_instances", bool, description="Default false: add instance names to each path step."),
    OpenApiParameter("data_flows", bool, description="Default true."),
]
DEPENDENCY_OPTIONS = tuple(p.name for p in DEPENDENCY_PARAMS)  # what parse_options reads; anything else is refused


class DependenciesView(RoleAPIView):
    """Dependents, depends-on or both of any record, through dependency links (relationship types with
    `is_dependency`); cycle-safe, one shortest path per record (catalog/graph.py). Integrations touching a listed
    record are data flows. Each record's own `/dependencies` (RecordDependenciesView) answers the same."""

    role_policy = policy(VIEWER)

    @extend_schema(
        summary="Explore dependencies of an application, component or AI agent",
        description=DEPENDENCY_DOC,
        parameters=[
            OpenApiParameter("target", str, required=True, description="Application, component or AI agent ID."),
            *DEPENDENCY_PARAMS,
        ],
        responses={200: DependenciesSerializer, **ERRORS},
    )
    def get(self, request):
        org = self.organization
        params = request.query_params
        options, errors = graph.parse_options(params)
        errors.update(unknown_params(params, ("target", *DEPENDENCY_OPTIONS)))
        if not params.get("target"):
            errors["target"] = ["Choose an application, component or AI agent."]
        elif not services.is_uuid(params["target"]):
            errors["target"] = ["Enter a record ID."]
        if errors:
            raise FieldErrors(errors)
        target = get_object_or_404(Resource, organization=org, id=params["target"])  # another organization's: 404
        if target.kind not in RECORD_KINDS:
            raise FieldErrors({"target": ["Choose an application, component or AI agent."]})
        return Response(graph.traverse(org, target, **options))


class RecordView(RoleAPIView):
    """A read-only composite under /<applications|components|ai-agents>/<pk>/. The URLconf passes `kind` (and the
    AI agents capability for agents); another organization's record, or one of another kind, is a 404."""

    role_policy = policy(VIEWER)
    kind: ClassVar[str] = Kind.APPLICATION
    required_capability: ClassVar[str | None] = None

    def record(self, pk) -> Resource:
        return get_object_or_404(Resource, organization=self.organization, kind=self.kind, id=pk)


class RecordDependenciesView(RecordView):
    """`GET /<records>/<pk>/dependencies`: the explorer with this record as the target (same parameters, same body)."""

    @extend_schema(
        summary="Dependencies of this record",
        description=DEPENDENCY_DOC + " The same answer as `GET /dependencies?target=<id>` with the same parameters.",
        parameters=DEPENDENCY_PARAMS,
        responses={200: DependenciesSerializer, **ERRORS},
    )
    def get(self, request, pk):
        options, errors = graph.parse_options(request.query_params)
        errors.update(unknown_params(request.query_params, DEPENDENCY_OPTIONS))
        if errors:
            raise FieldErrors(errors)
        return Response(graph.traverse(self.organization, self.record(pk), **options))


class RecordMapView(RecordView):
    """`GET /<records>/<pk>/map?depth=1|2`: everything the record is connected to (catalog/record_map.py)."""

    @extend_schema(
        summary="Map of everything this record is connected to",
        description="Relationships of every type in both directions, integrations (as edges between their ends), "
        "instances and linked discovery sources (and contracts and license pools), one or two steps "
        "out, as a graph (`nodes`, `edges`) and as the table beside it (`rows`, grouped by `categories`). Only "
        "records are expanded at depth 2. Archived and retired nodes are flagged, not hidden. At most "
        f"{record_map.MAX_NODES} nodes; `truncated` says when it stopped.",
        parameters=[OpenApiParameter("depth", int, enum=[1, 2], description="Default 1.")],
        responses={200: RecordMapSerializer, **ERRORS},
    )
    def get(self, request, pk):
        refuse_unknown_params(request, ("depth",))
        raw = request.query_params.get("depth", "1")
        if raw not in ("1", "2"):
            raise FieldErrors({"depth": ["Choose 1 or 2."]})
        return Response(record_map.build(self.organization, self.record(pk), int(raw)))
