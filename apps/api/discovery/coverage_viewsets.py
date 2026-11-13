"""The SSO and MFA coverage report (discovery/coverage.py): every live application
(not archived, not retired) with whether it is behind single sign-on, whether SCIM provisioning is on, whether
conditional access requires MFA, the matching policies, and gap chips. Read-only: `entra_coverage` runs write the
coverage, and the record's own fields are edited as any record.

Roles: any member reads the report and exports it as CSV (audited as `export`, counts and filters only; refused while
platform staff impersonate, core/impersonation_routes.py). Capability discovery.entra, as the source."""

from typing import Any
from urllib.parse import urlencode

from django import forms
from django.db.models import Q
from django.http import QueryDict
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.decorators import action

from catalog import services
from catalog.models import Criticality, Kind, Lifecycle, Resource
from catalog.viewsets import csv_response
from core.capabilities import DISCOVERY_ENTRA
from core.dates import org_today
from core.drf import (
    CSV_FILE,
    PAGING_PARAMS,
    PROBLEMS,
    VIEWER,
    KeysetPagination,
    ParamFilter,
    TenantFilterSet,
    TenantViewSet,
    choices_csv,
    filter_errors,
    policy,
    stripped,
)
from core.problems import FieldErrors
from imports import csvio

from . import coverage
from .coverage_serializers import SsoCoverageRowSerializer
from .models import MfaCoverage, ProvisioningState, SsoMode

EXPORT_MAX = 10_000
ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
GAPS = ("no_sso", "no_scim", "no_mfa", "mfa_partial", "unknown")
GAP_Q = {
    "no_sso": Q(sso=SsoMode.NONE),
    "no_scim": Q(scim=ProvisioningState.OFF),
    "no_mfa": Q(mfa=MfaCoverage.NONE),
    "mfa_partial": Q(mfa=MfaCoverage.SOME_USERS),
    "unknown": Q(sso=coverage.UNKNOWN) | Q(scim=coverage.UNKNOWN) | Q(mfa=coverage.UNKNOWN),
}
CRITICALITIES = (*Criticality.values, "none")


def _yes_no(value: str) -> bool:
    if value not in ("true", "false"):
        raise forms.ValidationError("Use true or false.")
    return value == "true"


class SsoCoverageFilterSet(TenantFilterSet):
    sso = ParamFilter(
        parse=choices_csv(SsoMode.values),
        field_name="sso",
        lookup_expr="in",
        choices=SsoMode.choices,
        help_text="Comma-separated: saml, oidc, password, none, unknown.",
    )
    scim = ParamFilter(
        parse=choices_csv(ProvisioningState.values),
        field_name="scim",
        lookup_expr="in",
        choices=ProvisioningState.choices,
        help_text="Comma-separated: on, off, unknown.",
    )
    mfa = ParamFilter(
        parse=choices_csv(MfaCoverage.values),
        field_name="mfa",
        lookup_expr="in",
        choices=MfaCoverage.choices,
        help_text="Comma-separated: all_users, some_users, none, unknown, blocked.",
    )
    criticality = ParamFilter(
        parse=choices_csv(CRITICALITIES),
        method="filter_criticality",
        choices=[(c, c) for c in CRITICALITIES],
        help_text="Comma-separated: critical, high, medium, low, none (not recorded).",
    )
    gap = ParamFilter(
        parse=choices_csv(GAPS),
        method="filter_gap",
        choices=[(g, g) for g in GAPS],
        help_text="Comma-separated, any of: no_sso, no_scim, no_mfa, mfa_partial, unknown.",
    )
    linked = ParamFilter(
        parse=_yes_no, method="filter_linked", help_text="true: only applications matched to an Entra app."
    )
    q = ParamFilter(parse=stripped, method="filter_q", help_text="Search the name or the Entra application ID.")

    class Meta:
        model = Resource
        fields: list[str] = []

    def filter_criticality(self, qs, name, value):
        wanted = [c for c in value if c != "none"]
        match = Q(criticality__in=wanted)
        if "none" in value:
            match |= Q(criticality="") | Q(criticality__isnull=True)
        return qs.filter(match)

    def filter_gap(self, qs, name, value):
        match = Q(pk__isnull=True)
        for gap in value:
            match |= GAP_Q[gap]
        return qs.filter(match)

    def filter_linked(self, qs, name, value):
        return qs.filter(entra_app__isnull=not value)

    def filter_q(self, qs, name, value):
        return qs.filter(Q(name__icontains=value) | Q(entra_app=value.lower()))


EXPORT_COLUMNS = [
    "name",
    "record_id",
    "criticality",
    "lifecycle",
    "entra_app_id",
    "linked_by",
    "sso",
    "sso_detail",
    "scim",
    "scim_detail",
    "scim_checked_at",
    "mfa",
    "mfa_detail",
    "mfa_checked_at",
    "policies",
    "gaps",
    "differs",
    "connector",
    "seen_at",
]


@extend_schema_view(
    list=extend_schema(
        summary="SSO and MFA coverage: every application's single sign-on, SCIM and conditional access",
        description="One row per live application (not archived, not retired), most critical first. Coverage comes "
        "from the organization's SSO and MFA coverage connectors (source entra_coverage) for the Entra app the "
        "application is matched to (its entra_app_id, else a linked Entra or shadow IT candidate); unknown, with the "
        "reason, when it isn't matched or not read. `gaps` are the chips (No SSO, No SCIM, No MFA policy, MFA for some "
        "users only, Coverage unknown); `differs` names the record's own sso_enabled and scim_provisioning where "
        "Entra says otherwise. Nothing here changes a record.",
        filters=True,
    ),
    export=extend_schema(
        summary="The SSO and MFA coverage report as CSV",
        description="The list's filters (cursor and page_size are ignored), at most 10,000 rows, most critical first. "
        "UTF-8 with a BOM, formula-safe cells. Audited as `export` (kind sso_coverage, counts and filters, never "
        "contents). Refused while platform staff impersonate.",
        filters=True,
        responses={**CSV_FILE, **ERRORS},
    ),
)
class SsoCoverageViewSet(TenantViewSet):
    role_policy = policy(VIEWER, capability=DISCOVERY_ENTRA)
    serializer_class = SsoCoverageRowSerializer
    filterset_class = SsoCoverageFilterSet
    ordering_fields = ("name", "criticality", "sso", "scim", "mfa")
    default_ordering = ("criticality", "name")
    # Sorted by meaning (core.drf.KeysetPagination.ranked): the worst coverage first when ascending.
    ordering_ranks = {
        "criticality": ("critical", "high", "medium", "low"),
        "sso": ("none", "password", "unknown", "saml", "oidc"),
        "scim": ("off", "unknown", "on"),
        "mfa": ("none", "some_users", "unknown", "all_users", "blocked"),
    }

    def base_queryset(self):
        return coverage.annotate(
            Resource.objects.filter(kind=Kind.APPLICATION, archived_at__isnull=True)
            .exclude(lifecycle=Lifecycle.RETIRED)
            .select_related("application")
        )

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        return {"coverage": coverage.blocks(self.organization, rows)}

    @action(detail=False, methods=["get"], url_path="export.csv")
    def export(self, request):
        org = self.organization
        ignored = PAGING_PARAMS | {"ordering"}  # an export has its own order
        params = {k: request.query_params[k] for k in request.query_params if k not in ignored}
        filterset = SsoCoverageFilterSet(QueryDict(urlencode(params)), queryset=self.get_queryset(), request=request)
        errors = filter_errors(filterset, params)
        if errors:
            raise FieldErrors(errors)
        qs, keys = KeysetPagination.ranked(filterset.qs, [("criticality", False), ("name", False), ("id", False)], self)
        rows = list(qs.order_by(*[name for name, _desc in keys])[:EXPORT_MAX])
        blocks = coverage.blocks(org, rows)
        data = csvio.write_csv(
            EXPORT_COLUMNS,
            (
                {
                    "name": r.name,
                    "record_id": str(r.id),
                    "criticality": r.criticality or "",
                    "lifecycle": r.lifecycle,
                    "entra_app_id": b["entra_app_id"] or "",
                    "linked_by": b["linked_by"],
                    "sso": b["sso"],
                    "sso_detail": b["sso_detail"],
                    "scim": b["scim"],
                    "scim_detail": b["scim_detail"],
                    "mfa": b["mfa"],
                    "scim_checked_at": b["scim_checked_at"] or "",
                    "mfa_detail": b["mfa_detail"],
                    "mfa_checked_at": b["mfa_checked_at"] or "",
                    "policies": "; ".join(f"{p['name']} ({p['effect']})" for p in b["policies"]),
                    "gaps": "; ".join(b["gaps"]),
                    "differs": "; ".join(d["label"] for d in b["differs"]),
                    "connector": (b["connector"] or {}).get("name", ""),
                    "seen_at": b["seen_at"] or "",
                }
                for r in rows
                for b in (blocks[r.id],)
            ),
        )
        services.audit(
            org, request.user, "export", None, {"kind": "sso_coverage", "rows": len(rows), "filters": params}
        )
        return csv_response(data, f"softwarden-sso-coverage-{org_today(org).isoformat()}.csv")
