"""The vendor risk file's routes (catalog/vendor_risk.py). Routes are in catalog/urls.py.

- `GET, PATCH /vendors/{id}/risk`: the vendor's risk profile (the first PATCH creates it; a vendor without one reads
  as the defaults with `id` null). ETag and If-Match as on every record.
- `GET, POST /vendors/{id}/risk-documents`: the vendor's documents (a plain array, the soonest expiry first), and
  adding one.
- `GET /vendor-risk-documents`: every vendor's documents, keyset pages, with filters (vendor, kind, status, tier,
  expiring_within); `/vendor-risk-documents/export.csv` the same rows as CSV; `GET, PATCH, DELETE
  /vendor-risk-documents/{id}` one document.
- `GET /vendors/risk-overview`: every vendor with its tier, assurance, document statuses and gaps, the riskiest
  first, with filters (tier, gap, expiring_within, expired); `/vendors/risk-overview/export.csv` as CSV.

Roles: Viewer reads and exports; Editor writes (and deletes documents). Every route needs the `vendor_risk`
capability. Writes go through catalog/vendor_risk.py; exports are audited (`export`)."""

import hashlib
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from django import forms
from django.db.models import Q, Value
from django.db.models.functions import Coalesce
from django.http import QueryDict
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from core.capabilities import VENDOR_RISK
from core.dates import org_today
from core.drf import (
    CSV_FILE,
    EDITOR,
    PAGING_PARAMS,
    PROBLEMS,
    UNKNOWN_FILTER,
    VIEWER,
    ParamFilter,
    RoleAPIView,
    TenantFilterSet,
    TenantViewSet,
    choice,
    choices_csv,
    filter_errors,
    policy,
    revision,
    uuid_value,
)
from core.input import whole_number
from core.problems import FieldErrors
from imports import csvio

from . import services
from . import vendor_risk as risk
from .models import RiskDocumentKind, Vendor, VendorRiskDocument, VendorRiskProfile
from .vendor_risk_serializers import (
    VendorRiskDocumentInputSerializer,
    VendorRiskDocumentSerializer,
    VendorRiskOverviewRowSerializer,
    VendorRiskProfileInputSerializer,
    VendorRiskProfileSerializer,
)
from .viewsets import csv_response

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409, 412)}
FAR = date(9999, 12, 31)  # an undated document sorts after every dated one
DAYS_MAX = 3650


def days_value(value: str) -> int:
    number = whole_number(value, 0, DAYS_MAX)
    if number is None:
        raise forms.ValidationError(f"Use a whole number of days from 0 to {DAYS_MAX}.")
    return number


def tier_q(prefix: str, tiers: list[str]) -> Q:
    """Rows whose vendor's tier is one of `tiers` (`none`: no profile, or no tier set)."""
    q = Q(**{f"{prefix}risk_profile__risk_tier__in": [t for t in tiers if t != risk.NO_TIER]})
    if risk.NO_TIER in tiers:
        q |= Q(**{f"{prefix}risk_profile__isnull": True}) | Q(**{f"{prefix}risk_profile__risk_tier": ""})
    return q


# ---- the profile -------------------------------------------------------------------------------------------------


def absent_revision(vendor_id) -> str:
    """The revision of a vendor whose risk file was never written: fixed per vendor, so an If-Match from that read
    refuses a write once someone else has created the profile."""
    return hashlib.sha256(f"vendor-risk-absent:{vendor_id}".encode()).hexdigest()[:32]


def _revision(profile: VendorRiskProfile | None, vendor_id) -> str:
    return revision(profile) if profile is not None else absent_revision(vendor_id)


@extend_schema_view(
    retrieve=extend_schema(
        summary="Get a vendor's risk profile",
        description="A vendor whose risk file was never written reads as the defaults with `id` null.",
    ),
    partial_update=extend_schema(
        summary="Change a vendor's risk profile (Editor, Admin); the first change creates it",
        description="If-Match takes the ETag of your read (also before the profile exists); 412 if it changed.",
    ),
)
class VendorRiskProfileViewSet(TenantViewSet):
    """A vendor's risk profile, keyed by the vendor: read by every role, written by Editors and Admins. One per vendor,
    never deleted on its own (it goes with the vendor)."""

    role_policy = policy(VIEWER, EDITOR)
    required_capability = VENDOR_RISK
    pagination_class = None
    serializer_class = VendorRiskProfileSerializer
    input_serializer_class = VendorRiskProfileInputSerializer

    def base_queryset(self):
        return Vendor.objects.all()

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        org = self.organization
        return {"today": org_today(org), "days": risk.horizon(org), "files": risk.files(org, rows)}

    def revision_of(self, obj) -> str:
        return _revision(VendorRiskProfile.objects.filter(organization=self.organization, vendor=obj).first(), obj.id)

    def update_object(self, obj, data):
        risk.update_profile(self.organization, self.request.user, obj, data)
        return obj


# ---- documents -----------------------------------------------------------------------------------------------------


class VendorRiskDocumentFilterSet(TenantFilterSet):
    vendor = ParamFilter(parse=uuid_value, field_name="vendor_id", help_text="A vendor ID.")
    kind = ParamFilter(
        parse=choices_csv(RiskDocumentKind.values),
        field_name="kind",
        lookup_expr="in",
        choices=RiskDocumentKind.choices,
        help_text="One or more kinds, comma-separated.",
    )
    status = ParamFilter(
        parse=choices_csv(risk.DOCUMENT_STATUSES),
        method="filter_status",
        help_text="One or more of current, expiring, expired (comma-separated), as of the organization's today.",
    )
    tier = ParamFilter(
        parse=choices_csv([*risk.VendorRiskTier.values, risk.NO_TIER]),
        method="filter_tier",
        help_text="The vendor's risk tier, one or more comma-separated; none for vendors not assessed.",
    )
    expiring_within = ParamFilter(
        parse=days_value,
        method="filter_expiring_within",
        help_text="Documents expiring from today through today plus this many days (expired ones left out).",
    )
    superseded = ParamFilter(
        parse=choice(["true", "false"]),
        method="filter_superseded",
        help_text="false: only the documents in force (no later one of their family replaces them), what reminders, "
        "counts and the overview read; true: only the replaced ones. Absent: both, so `status` and `expiring_within` "
        "also match replaced documents.",
    )

    class Meta:
        model = VendorRiskDocument
        fields: list[str] = []

    def _today(self):
        org = self.membership.organization
        return org_today(org), risk.horizon(org)

    def filter_status(self, queryset, name, value):
        today, days = self._today()
        soon = today + timedelta(days=days)
        q = Q(pk__isnull=True)
        if risk.EXPIRED in value:
            q |= Q(expires_on__lt=today)
        if risk.EXPIRING in value:
            q |= Q(expires_on__gte=today, expires_on__lte=soon)
        if risk.CURRENT in value:
            q |= Q(expires_on__isnull=True) | Q(expires_on__gt=soon)
        return queryset.filter(q)

    def filter_superseded(self, queryset, name, value):
        # The one supersession rule (catalog/vendor_risk.superseded_keys), over the organization's risk files.
        replaced = risk.superseded_for(self.membership.organization)
        return queryset.filter(id__in=replaced) if value == "true" else queryset.exclude(id__in=replaced)

    def filter_tier(self, queryset, name, value):
        return queryset.filter(tier_q("vendor__", value))

    def filter_expiring_within(self, queryset, name, value):
        today, _ = self._today()
        return queryset.filter(expires_on__gte=today, expires_on__lte=today + timedelta(days=value))


DOCUMENT_COLUMNS = [
    "vendor",
    "kind",
    "title",
    "issued_on",
    "validity_months",
    "expires_on",
    "status",
    "superseded",
    "attachment",
    "notes",
]


def _document_docs():
    return extend_schema_view(
        list=extend_schema(
            summary="List vendor risk documents",
            description="Ordering: expiry (the expiry date, undated last; the default), created_at, updated_at.",
        ),
        create=extend_schema(
            summary="Add a risk document to a vendor (Editor, Admin)",
            responses={201: VendorRiskDocumentSerializer, **{c: PROBLEMS[c] for c in (400, 403, 404, 409)}},
        ),
        retrieve=extend_schema(summary="Get a vendor risk document"),
        partial_update=extend_schema(summary="Change a vendor risk document (Editor, Admin)"),
        destroy=extend_schema(summary="Remove a vendor risk document (Editor, Admin)"),
        export=extend_schema(
            summary="Vendor risk documents as CSV (the list's filters)",
            responses={**CSV_FILE, **ERRORS},
        ),
    )


@_document_docs()
class VendorRiskDocumentViewSet(TenantViewSet):
    """Every vendor's risk documents, and one by ID."""

    role_policy = policy(VIEWER, EDITOR)
    required_capability = VENDOR_RISK
    serializer_class = VendorRiskDocumentSerializer
    input_serializer_class = VendorRiskDocumentInputSerializer
    filterset_class = VendorRiskDocumentFilterSet
    ordering_fields = ("expiry", "created_at", "updated_at")
    default_ordering = ("expiry",)

    def base_queryset(self):
        return VendorRiskDocument.objects.select_related("vendor", "attachment")

    def get_queryset(self):
        qs = super().get_queryset()
        # The expiry date as a non-null key (keyset pages skip nulls): undated documents last.
        return qs.annotate(expiry=Coalesce("expires_on", Value(FAR)))

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        org = self.organization
        superseded = risk.superseded_for(org, {r.vendor_id for r in rows})
        return {"today": org_today(org), "days": risk.horizon(org), "superseded": superseded}

    def update_object(self, obj, data):
        return risk.update_document(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        risk.delete_document(self.organization, self.request.user, obj)

    def export_rows(self, request) -> tuple[list[VendorRiskDocument], dict[str, str]]:
        params = {k: request.query_params[k] for k in request.query_params}
        query = QueryDict(urlencode(params))
        filterset = self.filterset_class(query, queryset=self.get_queryset(), request=request)
        errors = filter_errors(filterset, params, PAGING_PARAMS)
        if errors:
            raise FieldErrors(errors)
        rows = list(filterset.qs.order_by("vendor__name", "expiry", "kind", "id"))
        return rows, {k: v for k, v in params.items() if k not in PAGING_PARAMS}

    @action(detail=False, methods=["get"], url_path="export.csv")
    def export(self, request):
        rows, filters = self.export_rows(request)
        context = self.page_context(rows)
        today, days, superseded = context["today"], context["days"], context["superseded"]
        data = csvio.write_csv(
            DOCUMENT_COLUMNS,
            (
                {
                    "vendor": d.vendor.name,
                    "kind": d.kind,
                    "title": d.title,
                    "issued_on": d.issued_on.isoformat() if d.issued_on else "",
                    "validity_months": d.validity_months or "",
                    "expires_on": d.expires_on.isoformat() if d.expires_on else "",
                    "status": risk.status_of(d.expires_on, today, days),
                    "superseded": "yes" if d.id in superseded else "no",
                    "attachment": d.attachment.title if d.attachment else "",
                    "notes": d.notes,
                }
                for d in rows
            ),
        )
        changes = {"kind": "vendor_risk_document", "rows": len(rows), "filters": filters}
        services.audit(self.organization, request.user, "export", None, changes)
        return csv_response(data, f"softwarden-vendor-risk-documents-{today.isoformat()}.csv")


@extend_schema_view(
    list=extend_schema(summary="List a vendor's risk documents, the soonest expiry first"),
    create=extend_schema(
        summary="Add a risk document to a vendor (Editor, Admin)",
        responses={201: VendorRiskDocumentSerializer, **{c: PROBLEMS[c] for c in (400, 403, 404, 409)}},
    ),
)
class VendorRiskDocumentsOfVendorViewSet(VendorRiskDocumentViewSet):
    """One vendor's documents (a plain array) and adding one."""

    pagination_class = None
    filterset_class = None  # type: ignore[assignment]

    def vendor(self) -> Vendor:
        if not hasattr(self, "_vendor"):
            self._vendor = get_object_or_404(Vendor, organization=self.organization, id=self.kwargs["pk"])
        return self._vendor

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, "swagger_fake_view", False):
            return qs
        return qs.filter(vendor=self.vendor())

    def list(self, request, *args, **kwargs):
        self.vendor()  # 404 for another organization's vendor
        return super().list(request, *args, **kwargs)

    def create_object(self, data):
        return risk.create_document(self.organization, self.request.user, self.vendor(), data)


# ---- the overview ------------------------------------------------------------------------------------------------

OVERVIEW_PARAMS = [
    OpenApiParameter(
        "tier",
        str,
        description="The vendor's risk tier, one or more of low, medium, high, critical, none (not assessed), "
        "comma-separated.",
    ),
    OpenApiParameter("gap", str, description="One or more gaps, comma-separated: " + ", ".join(risk.GAPS) + "."),
    OpenApiParameter(
        "expiring_within",
        int,
        description="Vendors with a document or assurance report (not superseded) expiring from today through today "
        f"plus this many days (0 to {DAYS_MAX}).",
    ),
    OpenApiParameter(
        "expired", bool, description="true: vendors with an expired document or report (not superseded); false: none."
    ),
]
OVERVIEW_COLUMNS = [
    "vendor",
    "risk_tier",
    "processes_personal_data",
    "questionnaire_status",
    "questionnaire_requested_on",
    "questionnaire_reviewed_on",
    "assurance_type",
    "assurance_issued_on",
    "assurance_expires_on",
    "assurance_status",
    "dpa_status",
    "dpa_signed_on",
    "subprocessor_url",
    "subprocessors_reviewed_on",
    "documents_current",
    "documents_expiring",
    "documents_expired",
    "next_expiry",
    "gaps",
]


def overview_filters(params) -> tuple[risk.OverviewFilters, dict[str, str]]:
    errors: dict[str, list[str]] = {}
    known = {"tier", "gap", "expiring_within", "expired"}
    for name in params:
        if name not in known:
            errors[name] = [UNKNOWN_FILTER]
    tiers: list[str] = []
    gaps: list[str] = []
    within = expired = None
    parsers = {
        "tier": choices_csv([*risk.VendorRiskTier.values, risk.NO_TIER]),
        "gap": choices_csv(risk.GAPS),
        "expiring_within": days_value,
    }
    found: dict[str, Any] = {}
    for name, parse in parsers.items():
        if name in params:
            try:
                found[name] = parse(params[name])
            except forms.ValidationError as e:
                errors[name] = list(e.messages)
    if "expired" in params:
        if params["expired"] in ("true", "false"):
            expired = params["expired"] == "true"
        else:
            errors["expired"] = ["Use true or false."]
    if errors:
        raise FieldErrors(errors)
    tiers, gaps, within = found.get("tier", []), found.get("gap", []), found.get("expiring_within")
    filters = risk.OverviewFilters(tuple(tiers), tuple(gaps), within, expired)
    return filters, {k: params[k] for k in params}


class VendorRiskOverviewView(RoleAPIView):
    """Every vendor with its risk tier, assurance report, document statuses and gaps, the riskiest first (critical,
    high, medium, low, not assessed; then by name). A plain array: an organization has hundreds of vendors at most."""

    role_policy = policy(VIEWER, capability=VENDOR_RISK)

    @extend_schema(
        summary="Vendor risk overview",
        parameters=OVERVIEW_PARAMS,
        responses={200: VendorRiskOverviewRowSerializer(many=True), **ERRORS},
    )
    def get(self, request):
        filters, _ = overview_filters(request.query_params)
        rows = risk.overview(self.organization, filters)
        for row in rows:
            row.pop("_profile")
        return Response(rows)


class VendorRiskOverviewExportView(RoleAPIView):
    """The overview as CSV: one row per vendor, the overview's filters and order."""

    role_policy = policy(VIEWER, capability=VENDOR_RISK)

    @extend_schema(
        summary="Vendor risk overview as CSV",
        parameters=OVERVIEW_PARAMS,
        responses={**CSV_FILE, **ERRORS},
    )
    def get(self, request):
        org = self.organization
        filters, used = overview_filters(request.query_params)
        rows = risk.overview(org, filters)

        def cells(row):
            p = row["_profile"]

            def day(name):
                value = getattr(p, name) if p is not None else None
                return value.isoformat() if value else ""

            return {
                "vendor": row["vendor"]["name"],
                "risk_tier": row["risk_tier"] or "",
                "processes_personal_data": "yes" if row["processes_personal_data"] else "no",
                "questionnaire_status": row["questionnaire_status"],
                "questionnaire_requested_on": day("questionnaire_requested_on"),
                "questionnaire_reviewed_on": day("questionnaire_reviewed_on"),
                "assurance_type": row["assurance"]["type"] or "",
                "assurance_issued_on": day("assurance_issued_on"),
                "assurance_expires_on": row["assurance"]["expires_on"] or "",
                "assurance_status": row["assurance"]["status"],
                "dpa_status": row["dpa_status"] or "",
                "dpa_signed_on": day("dpa_signed_on"),
                "subprocessor_url": p.subprocessor_url if p is not None else "",
                "subprocessors_reviewed_on": day("subprocessors_reviewed_on"),
                "documents_current": row["documents"]["current"],
                "documents_expiring": row["documents"]["expiring"],
                "documents_expired": row["documents"]["expired"],
                "next_expiry": row["next_expiry"] or "",
                "gaps": "; ".join(g["label"] for g in row["gaps"]),
            }

        data = csvio.write_csv(OVERVIEW_COLUMNS, (cells(r) for r in rows))
        services.audit(
            org, request.user, "export", None, {"kind": "vendor_risk_file", "rows": len(rows), "filters": used}
        )
        return csv_response(data, f"softwarden-vendor-risk-{org_today(org).isoformat()}.csv")
