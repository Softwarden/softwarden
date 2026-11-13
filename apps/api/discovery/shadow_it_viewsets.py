"""The shadow IT overview (discovery/entra_shadow_it.py): every third-party app the shadow IT connectors saw
people sign in to or consent to, with users and sign-ins over 30 days, the consent type, how many people consented,
the scopes and the high-risk ones, and whether the catalog has it (status new, linked or dismissed). Read-only: runs
write the rows, and an app is decided through its discovery candidate (link, create, dismiss), whose ID is on the row.

Roles: any member reads the overview and exports it as CSV (audited as `export`, counts and filters only; refused
while platform staff impersonate, core/impersonation_routes.py). Capability discovery.entra, as the source."""

from typing import Any
from urllib.parse import urlencode

from django import forms
from django.db.models import Case, CharField, Exists, F, OuterRef, Q, Value, When
from django.db.models.functions import Lower
from django.http import QueryDict
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework.decorators import action

from catalog import services
from catalog.filters import in_org_or_404, worded
from catalog.models import Application
from catalog.viewsets import csv_response
from core.capabilities import DISCOVERY_ENTRA
from core.dates import org_today
from core.drf import (
    CSV_FILE,
    PAGING_PARAMS,
    PROBLEMS,
    VIEWER,
    ParamFilter,
    TenantFilterSet,
    TenantViewSet,
    choices_csv,
    filter_errors,
    iso,
    policy,
    stripped,
    uuid_value,
)
from core.input import whole_number
from core.problems import FieldErrors
from imports import csvio

from .models import (
    Connector,
    ConsentType,
    DecisionKind,
    Observation,
    ObservationKind,
    ReconciliationDecision,
    ShadowItApp,
    Source,
)
from .serializers import ShadowItAppSerializer

STATUSES = ("new", "linked", "dismissed")
# The observations that speak for an app's place in the catalog: the Entra source's and the shadow IT source's.
OBSERVED_SOURCES = (Source.ENTRA, Source.ENTRA_SHADOW_IT)
OBSERVED_KINDS = (ObservationKind.APPLICATION, ObservationKind.AI_AGENT)
EXPORT_MAX = 10_000
USERS_MAX = 1_000_000
ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}


def with_status(qs):
    """`status` on each row, as the API documents it: linked when a live application record carries the app's Entra
    application ID or a candidate for it (Entra or shadow IT, any connector of the organization) was linked or created;
    dismissed when such a candidate was dismissed and hasn't changed since; new otherwise."""
    records = (
        Application.objects.filter(organization_id=OuterRef("organization_id"), resource__archived_at__isnull=True)
        .annotate(app=Lower("entra_app_id"))
        .filter(app=OuterRef("app_id"))
    )
    decided = ReconciliationDecision.objects.filter(
        organization_id=OuterRef("organization_id"),
        observation__external_id=OuterRef("app_id"),
        observation__connector__source__in=OBSERVED_SOURCES,
        observation__kind__in=OBSERVED_KINDS,
    )
    return qs.annotate(
        has_record=Exists(records),
        has_link=Exists(decided.exclude(decision=DecisionKind.DISMISSED)),
        has_dismissal=Exists(
            decided.filter(decision=DecisionKind.DISMISSED, fingerprint=F("observation__fingerprint"))
        ),
    ).annotate(
        status=Case(
            When(Q(has_record=True) | Q(has_link=True), then=Value("linked")),
            When(has_dismissal=True, then=Value("dismissed")),
            default=Value("new"),
            output_field=CharField(),
        )
    )


def shadow_context(org, rows: list[Any]) -> dict[str, Any]:
    """{records: {app ID: record}, candidates: {app ID: [observations]}} for a page of rows, in two queries."""
    apps = {r.app_id for r in rows}
    if not apps:
        return {"records": {}, "candidates": {}}
    records: dict[str, dict[str, Any]] = {}
    for app, rid, kind, name in (
        Application.objects.filter(organization=org, resource__archived_at__isnull=True)
        .annotate(app=Lower("entra_app_id"))
        .filter(app__in=apps)
        .order_by("resource__name", "resource_id")
        .values_list("app", "resource_id", "resource__kind", "resource__name")
    ):
        records.setdefault(app, {"id": str(rid), "kind": kind, "name": name})
    candidates: dict[str, list[Observation]] = {}
    observations = (
        Observation.objects.filter(
            organization=org, external_id__in=apps, connector__source__in=OBSERVED_SOURCES, kind__in=OBSERVED_KINDS
        )
        .select_related("connector", "decision", "decision__resource")
        .order_by("created_at", "id")
    )
    for o in observations:
        candidates.setdefault(o.external_id, []).append(o)
        d = getattr(o, "decision", None)
        if d is not None and d.decision != DecisionKind.DISMISSED and d.resource is not None:
            records.setdefault(
                o.external_id, {"id": str(d.resource.id), "kind": d.resource.kind, "name": d.resource.name}
            )
    return {"records": records, "candidates": candidates}


def _yes_no(value: str) -> bool:
    if value not in ("true", "false"):
        raise forms.ValidationError("Use true or false.")
    return value == "true"


def _users(value: str) -> int:
    number = whole_number(value, 0, USERS_MAX)
    if number is None:
        raise forms.ValidationError(f"Enter a whole number from 0 to {USERS_MAX:,}.")
    return number


class ShadowItFilterSet(TenantFilterSet):
    connector = ParamFilter(
        parse=worded(uuid_value, "Choose a connector from the list."),
        method="filter_connector",
        help_text="Connector ID: one shadow IT connector's apps.",
    )
    status = ParamFilter(
        parse=choices_csv(STATUSES),
        field_name="status",
        lookup_expr="in",
        choices=[(s, s) for s in STATUSES],
        help_text="Comma-separated: new, linked, dismissed.",
    )
    consent = ParamFilter(
        parse=choices_csv(ConsentType.values),
        field_name="consent",
        lookup_expr="in",
        choices=ConsentType.choices,
        help_text="Comma-separated: admin, user, none.",
    )
    risky = ParamFilter(parse=_yes_no, field_name="risky", help_text="true: only apps granted a high-risk scope.")
    min_users = ParamFilter(
        parse=_users, field_name="users_30d", lookup_expr="gte", help_text="At least this many users in 30 days."
    )
    q = ParamFilter(parse=stripped, method="filter_q", help_text="Search the name, the publisher or the app ID.")

    class Meta:
        model = ShadowItApp
        fields: list[str] = []

    def filter_connector(self, qs, name, value):
        in_org_or_404(Connector, self.membership.organization, value)
        return qs.filter(connector_id=value)

    def filter_q(self, qs, name, value):
        return qs.filter(
            Q(display_name__icontains=value) | Q(publisher__icontains=value) | Q(app_id__iexact=value.lower())
        )


EXPORT_COLUMNS = [
    "name",
    "app_id",
    "publisher",
    "verified_publisher",
    "status",
    "users_30d",
    "sign_ins_30d",
    "last_sign_in_at",
    "consent",
    "consenting_users",
    "risky_scopes",
    "scopes",
    "record",
    "record_id",
    "connector",
    "first_seen_at",
    "last_seen_at",
]


@extend_schema_view(
    list=extend_schema(
        summary="Shadow IT: third-party apps people use or consented to, with usage and consent",
        description="One row per app and shadow IT connector, most users first. Users and sign-ins are the last 30 "
        "days of successful interactive sign-ins (users estimated from a sketch, exact for small numbers); consent "
        "is admin, user or none, with how many people consented and the scopes granted (high-risk ones in "
        "risky_scopes). `status`: new, linked or dismissed (see the field). Decide an app through `candidate` "
        "(discovery/observations/{id}/link, /create, /dismiss). Never who signed in or consented.",
        filters=True,
    ),
    export=extend_schema(
        summary="The shadow IT overview as CSV",
        description="The list's filters (cursor and page_size are ignored), at most 10,000 rows, most users first. "
        "UTF-8 with a BOM, formula-safe cells. Audited as `export` (kind shadow_it, counts and filters, never "
        "contents). Refused while platform staff impersonate.",
        filters=True,
        responses={**CSV_FILE, **ERRORS},
    ),
)
class ShadowItViewSet(TenantViewSet):
    role_policy = policy(VIEWER, capability=DISCOVERY_ENTRA)
    serializer_class = ShadowItAppSerializer
    filterset_class = ShadowItFilterSet
    ordering_fields = ("users_30d", "sign_ins_30d", "display_name", "consenting_users", "first_seen_at")
    default_ordering = ("-users_30d", "display_name")

    def base_queryset(self):
        return with_status(ShadowItApp.objects.select_related("connector"))

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        return shadow_context(self.organization, rows)

    @action(detail=False, methods=["get"], url_path="export.csv")
    def export(self, request):
        org = self.organization
        ignored = PAGING_PARAMS | {"ordering"}  # an export has its own order
        params = {k: request.query_params[k] for k in request.query_params if k not in ignored}
        filterset = ShadowItFilterSet(QueryDict(urlencode(params)), queryset=self.get_queryset(), request=request)
        errors = filter_errors(filterset, params)
        if errors:
            raise FieldErrors(errors)
        rows = list(filterset.qs.order_by("-users_30d", "display_name", "id")[:EXPORT_MAX])
        context = shadow_context(org, rows)
        data = csvio.write_csv(
            EXPORT_COLUMNS,
            (
                {
                    "name": r.display_name,
                    "app_id": r.app_id,
                    "publisher": r.publisher,
                    "verified_publisher": r.verified_publisher,
                    "status": r.status,
                    "users_30d": r.users_30d,
                    "sign_ins_30d": r.sign_ins_30d,
                    "last_sign_in_at": iso(r.last_sign_in_at) or "",
                    "consent": r.consent,
                    "consenting_users": r.consenting_users,
                    "risky_scopes": " ".join(r.risky_scopes or []),
                    "scopes": " ".join(r.scopes or []),
                    "record": (context["records"].get(r.app_id) or {}).get("name", ""),
                    "record_id": (context["records"].get(r.app_id) or {}).get("id", ""),
                    "connector": r.connector.name,
                    "first_seen_at": iso(r.first_seen_at),
                    "last_seen_at": iso(r.last_seen_at),
                }
                for r in rows
            ),
        )
        services.audit(org, request.user, "export", None, {"kind": "shadow_it", "rows": len(rows), "filters": params})
        return csv_response(data, f"softwarden-shadow-it-{org_today(org).isoformat()}.csv")
