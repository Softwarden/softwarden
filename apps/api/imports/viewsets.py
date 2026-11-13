"""Import viewsets (E29): import sources, and CSV import runs with their rows, commit,
cancel, rerun, template and error report. Routes are in imports/urls.py with the URLs and response shapes of the
function views they replaced; imports/importer.py validates, scopes and audits every write.

Kinds: applications, components, contracts and licence pools (imports/money_import.py; those two
need the `contracts` capability).

Roles: Editors and Admins use every import endpoint (Viewers have no access, not even to read); only Admins
create, rename and delete import sources, though Editors list them. Another organization's run or source is a 404."""

from typing import Any

from django import forms
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from catalog.filters import worded
from core.drf import (
    ADMIN,
    EDITOR,
    PROBLEMS,
    ParamFilter,
    ProblemSerializer,
    TenantFilterSet,
    TenantViewSet,
    filter_errors,
    policy,
    refuse_unknown_params,
    revision,
)
from core.input import whole_number
from core.parsers import MultiPartParser
from core.problems import FieldErrors

from . import csvio, importer
from .models import (
    KIND_CHOICES,
    KINDS,
    ExternalIdentifier,
    ImportRow,
    ImportRun,
    ImportSource,
    ImportStatus,
    RowOutcome,
)
from .serializers import (
    ImportRowPageSerializer,
    ImportRowSerializer,
    ImportRunDetailSerializer,
    ImportRunSerializer,
    ImportSourceInputSerializer,
    ImportSourceSerializer,
    ImportUpdateSerializer,
    ImportUploadSerializer,
)

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409)}
GONE = OpenApiResponse(ProblemSerializer, description="The file was purged after 30 days")
CSV_TYPE = "text/csv; charset=utf-8"
RUNS_SHOWN = 200
ROW_PAGE = 200
ROW_PAGE_MAX = 1000
KIND_MESSAGE = importer.KIND_MESSAGE
KIND_HELP = "application, component, contract or license_pool"


def record_kind(value: str) -> str:
    """application, component, contract or license_pool; also takes the plural, any case, hyphens and the British
    spelling (?kind=Applications, ?kind=licence-pools)."""
    kind = (value or "").strip().lower().replace("-", "_").replace(" ", "_").replace("licence", "license")
    kind = kind[:-1] if kind.endswith("s") else kind
    if kind not in KINDS:
        raise forms.ValidationError(KIND_MESSAGE)
    return kind


def csv_response(data, name):
    response = HttpResponse(data, content_type=CSV_TYPE)
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


# ---- import sources -------------------------------------------------------------------------------------------------


class ImportSourceFilterSet(TenantFilterSet):
    kind = ParamFilter(
        parse=worded(record_kind, KIND_MESSAGE),
        field_name="kind",
        choices=KIND_CHOICES,
        help_text=KIND_HELP,
    )

    class Meta:
        model = ImportSource
        fields: list[str] = []


@extend_schema_view(
    list=extend_schema(summary="List import sources (Editor, Admin)"),
    create=extend_schema(summary="Create an import source (Admin)"),
    retrieve=extend_schema(summary="Get an import source (Editor, Admin)"),
    partial_update=extend_schema(summary="Rename an import source (Admin)"),
    destroy=extend_schema(summary="Delete an import source (Admin; refused while external keys exist)"),
)
class ImportSourceViewSet(TenantViewSet):
    """Where CSVs come from: each source's external keys map to one record each. External keys
    (ExternalIdentifier) are counted here and written only by an import commit; they have no route of their own."""

    role_policy = policy(EDITOR, ADMIN)
    pagination_class = None
    serializer_class = ImportSourceSerializer
    input_serializer_class = ImportSourceInputSerializer
    filterset_class = ImportSourceFilterSet
    ordering_fields = ("name", "created_at")
    default_ordering = ("name",)

    def base_queryset(self):
        return ImportSource.objects.all()

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        counts = (
            ExternalIdentifier.objects.filter(organization=self.organization, source__in=rows)
            .values("source_id")
            .annotate(n=Count("id"))
        )
        return {"identifiers": {row["source_id"]: row["n"] for row in counts}}

    def create_object(self, data):
        return importer.create_source(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        return importer.update_source(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        importer.delete_source(self.organization, self.request.user, obj)


# ---- runs ---------------------------------------------------------------------------------------------------------


def _status(value: str) -> str:
    if value not in ImportStatus.values:
        raise forms.ValidationError("Choose uploaded, previewed, committed, refused or canceled.")
    return value


class ImportRunFilterSet(TenantFilterSet):
    kind = ParamFilter(parse=worded(record_kind, KIND_MESSAGE), field_name="kind", choices=KIND_CHOICES)
    status = ParamFilter(
        parse=worded(_status, "Choose uploaded, previewed, committed, refused or canceled."),
        field_name="status",
        choices=ImportStatus.choices,
    )

    class Meta:
        model = ImportRun
        fields: list[str] = []


def _outcome(value: str) -> str:
    if value not in RowOutcome.values:
        raise forms.ValidationError("Choose create, update, unchanged, error or excluded.")
    return value


class ImportRowFilterSet(TenantFilterSet):
    outcome = ParamFilter(
        parse=worded(_outcome, "Choose create, update, unchanged, error or excluded."),
        field_name="outcome",
        choices=RowOutcome.choices,
    )
    q = ParamFilter(parse=lambda v: v or None, method="search", help_text="Search the row's name (a pool's SKU name)")

    class Meta:
        model = ImportRow
        fields: list[str] = []

    def search(self, queryset, name, value):
        return queryset.filter(Q(raw__name__icontains=value) | Q(raw__sku_name__icontains=value))


# Row paging is by offset: the preview pages through a fixed, numbered set of rows. `rev` is the SPA's reload key.
ROW_PARAMS = {"offset", "limit", "rev"}
CURSOR_MAX = 10**12  # past any import; keeps OFFSET plus a page inside PostgreSQL's bigint


def _row_window(params) -> tuple[int, int, dict[str, list[str]]]:
    numbers, errors = {}, {}
    for key, default in (("offset", 0), ("limit", ROW_PAGE)):
        number = whole_number(params.get(key, str(default)), 0, ROW_PAGE_MAX if key == "limit" else CURSOR_MAX)
        if number is None:
            errors[key] = ["Use a whole number" + (f" up to {ROW_PAGE_MAX}." if key == "limit" else ".")]
        else:
            numbers[key] = number
    return numbers.get("offset", 0), numbers.get("limit", ROW_PAGE), errors


DETAIL = {200: ImportRunDetailSerializer, **WRITE_ERRORS}


@extend_schema_view(
    list=extend_schema(summary="Run history, newest first, at most 200 (Editor, Admin)"),
    create=extend_schema(
        summary="Upload a CSV and preview it (Editor, Admin)",
        description="Multipart: file, kind (application, component, contract or license_pool; the last two need "
        "the contracts capability) and optionally source. With the template's "
        "headings the file is read, validated and matched in this request and the run is previewed. With other "
        "headings the run is uploaded (needs_mapping, headers, suggested_mapping, fields); PATCH the mapping to "
        "preview it.",
        request={"multipart/form-data": ImportUploadSerializer},
        responses={201: ImportRunDetailSerializer, **WRITE_ERRORS},
    ),
    retrieve=extend_schema(summary="One run with its counts, choices needed and results (Editor, Admin)"),
    partial_update=extend_schema(
        summary="Record choices and exclusions, or the column mapping, then preview again (Editor, Admin)",
        description="decisions: {people|teams|vendors: {key: create|gap|null}, matches: {row number: create|match|"
        "null}}; exclude / include: row numbers of rows that have errors. On an uploaded run, mapping: {heading: "
        "column | external_key | ignore} (every heading; errors under mapping) and save_mapping: true to remember "
        "it on the run's source; a mapping on a previewed run is a 409.",
        responses={**DETAIL, 410: GONE, 412: PROBLEMS[412]},
    ),
    rows=extend_schema(
        summary="The run's rows (Editor, Admin)",
        parameters=[
            OpenApiParameter("outcome", str, enum=RowOutcome.values),
            OpenApiParameter("q", str, description="Search the row's name"),
            OpenApiParameter("offset", int),
            OpenApiParameter("limit", int, description=f"Default {ROW_PAGE}, at most {ROW_PAGE_MAX}"),
            OpenApiParameter("rev", str, description="Ignored; lets a client reload the same page."),
        ],
        responses={200: ImportRowPageSerializer, **ERRORS},
        filters=False,
    ),
    commit=extend_schema(
        summary="Commit the previewed run in one transaction (Editor, Admin)",
        description="Refused with 409 (type /problems/import-refused, run status refused, nothing saved) when a row "
        "has an error that isn't excluded, a choice is missing, or a matched record changed after the preview.",
        request=None,
        responses={**DETAIL, 410: GONE},
    ),
    cancel=extend_schema(
        summary="Cancel a run that hasn't been committed (Editor, Admin)", request=None, responses=DETAIL
    ),
    rerun=extend_schema(
        summary="Start a new run from the same file and choices (Editor, Admin)",
        description="For 'Rerun preview' after a refusal. Needs the file, so not after the purge.",
        request=None,
        responses={201: ImportRunDetailSerializer, **WRITE_ERRORS},
    ),
    template=extend_schema(
        summary="Download the template CSV (Editor, Admin)",
        parameters=[OpenApiParameter("kind", str, required=True, description=KIND_HELP)],
        responses={
            (200, "text/csv"): OpenApiResponse(OpenApiTypes.BINARY, description="Headings only, UTF-8 with BOM"),
            **ERRORS,
        },
    ),
    error_report=extend_schema(
        summary="Download the error report: rows with errors or excluded (Editor, Admin)",
        responses={
            (200, "text/csv"): OpenApiResponse(OpenApiTypes.BINARY, description="UTF-8 with BOM, formula-escaped"),
            **ERRORS,
            410: GONE,
        },
    ),
)
class ImportRunViewSet(TenantViewSet):
    """CSV import runs. The upload itself (`file_bytes`) never leaves the server and isn't an ETag input. Rows
    (ImportRow) are listed under their run by row number and change only through the run's PATCH; they have no route
    of their own. Runs are never deleted through the API: the 30-day purge removes the file and rows' raw values."""

    role_policy = policy(EDITOR)
    pagination_class = None
    serializer_class = ImportRunSerializer
    detail_serializer_class = ImportRunDetailSerializer
    input_serializer_class = ImportUpdateSerializer
    filterset_class = ImportRunFilterSet
    ordering_fields = ("created_at",)
    default_ordering = ("-created_at",)
    list_limit = RUNS_SHOWN

    def get_parsers(self):
        """The upload is multipart; every other write is JSON."""
        request = getattr(self, "request", None)
        if request is not None and request.method == "POST" and self.action_map.get("post") == "create":
            return [MultiPartParser()]
        return super().get_parsers()

    def base_queryset(self):
        return ImportRun.objects.select_related("source", "uploaded_by", "organization")

    def revision_of(self, obj) -> str:
        return revision(obj, skip=("file_bytes",))

    def create(self, request, *args, **kwargs):
        org = self.organization
        try:
            kind = record_kind(request.data.get("kind"))
        except forms.ValidationError:
            raise FieldErrors({"kind": [KIND_MESSAGE]}) from None
        upload = request.FILES.get("file")
        if upload is None:
            raise FieldErrors({"file": ["Choose a CSV file."]})
        if upload.size > csvio.MAX_BYTES:
            raise importer.FileRejected("The file is larger than 5 MB. Split it into smaller files.")
        source = None
        if request.data.get("source"):
            source = ImportSource.objects.filter(
                organization=org, id=importer._uuid(str(request.data["source"]))
            ).first()
            if source is None:
                raise FieldErrors({"source": ["Choose an existing import source."]})
        content = upload.read()
        parsed = importer.parse_run(org, kind, content, source)  # parsing holds no transaction open
        with transaction.atomic():  # the response inside the write's transaction
            run = importer.create_run(org, request.user, kind, upload.name, content, source, parsed=parsed)
            return self.detail_response(
                self.fresh(run), status_code=status.HTTP_201_CREATED, serializer_class=ImportRunDetailSerializer
            )

    def update_object(self, obj, data):
        if "mapping" in data:
            importer.map_run(self.organization, self.request.user, obj, data)
        else:
            importer.update_run(self.organization, self.request.user, obj, data)
        return obj

    @action(detail=True, methods=["get"])
    def rows(self, request, pk=None):
        run = self.get_object()
        params = request.query_params
        qs = ImportRow.objects.filter(run=run).select_related(
            "run",
            "matched_resource",
            "possible_match",
            "result_resource",
            "matched_contract",
            "possible_contract",
            "result_contract",
            "matched_license_pool",
            "result_license_pool",
        )
        filterset = ImportRowFilterSet(params, queryset=qs, request=request)
        offset, limit, errors = _row_window(params)
        errors = {**filter_errors(filterset, params, ROW_PARAMS), **errors}
        if errors:
            raise FieldErrors(errors)
        rows = filterset.qs
        page = rows.order_by("row_number")[offset : offset + limit]
        return Response({"count": rows.count(), "results": ImportRowSerializer(page, many=True).data})

    @action(detail=True, methods=["post"])
    def commit(self, request, pk=None):
        # Not inside an outer transaction: a refusal marks the run refused and keeps that while raising the 409.
        run = self.get_object()
        importer.commit_run(self.organization, request.user, run)
        return self.detail_response(self.fresh(run))

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        with transaction.atomic():
            run = self.get_object()
            importer.cancel_run(self.organization, request.user, run)
            return self.detail_response(self.fresh(run))

    @action(detail=True, methods=["post"])
    def rerun(self, request, pk=None):
        with transaction.atomic():
            new = importer.rerun(self.organization, request.user, self.get_object())
            return self.detail_response(
                self.fresh(new),
                status_code=status.HTTP_201_CREATED,
                location=f"{request.path_info.rsplit('/', 2)[0]}/{new.pk}",
            )

    @action(detail=False, methods=["get"], url_path="template.csv")
    def template(self, request):
        errors = filter_errors(None, request.query_params, {"kind"})
        try:
            kind = record_kind(request.query_params.get("kind", ""))
        except forms.ValidationError:
            errors["kind"] = [KIND_MESSAGE]
        if errors:
            raise FieldErrors(errors)
        importer.require_kind(self.organization, kind)
        return csv_response(importer.template_file(kind, self.organization), f"softwarden-{kind}-template.csv")

    @action(detail=True, methods=["get"], url_path="errors.csv")
    def error_report(self, request, pk=None):
        refuse_unknown_params(request)
        run = self.get_object()
        return csv_response(importer.error_report(run), f"import-{str(run.id)[:8]}-errors.csv")
