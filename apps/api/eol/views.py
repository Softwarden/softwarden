"""End-of-life tracking endpoints (capability `eol`). Roles: every member reads (Viewer); Editors and Admins set and
clear a mapping. Nothing here touches the network: everything reads the installation's cache (eol/cache.py).

- `GET|PATCH|DELETE /applications/{id}/end-of-life`, `/components/{id}/end-of-life`: the record's tracking.
- `GET|PATCH|DELETE /applications/{id}/instances/{instance_id}/end-of-life`: one instance's own version.
- Revisions: GET and PATCH return the tracking row's `ETag`; PATCH and DELETE of a tracked row take `If-Match`
  (412 when it changed since, 428 from a signed-in session without it while SOFTWARDEN_REQUIRE_IF_MATCH is on). A PATCH
  that starts the tracking is a create and takes none; one that sends an If-Match (it read a row) is refused with 412 if
  the row is gone.
- `GET /end-of-life`: every tracked version of live records (not archived, not retired) with filters; and its CSV
  (`/end-of-life/export.csv`, audited as an export, refused while impersonating: core/impersonation_routes.py).
- `GET /end-of-life/products?search=`: the cached product list, for an autocomplete; `/end-of-life/products/{name}`:
  one product's release cycles."""

from __future__ import annotations

from typing import Any, ClassVar

from django import forms
from django.db import transaction
from django.db.models import Case, IntegerField, Q, QuerySet, Value, When
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.response import Response

from catalog.models import ApplicationInstance, Criticality, Kind, Resource
from catalog.services import audit
from catalog.viewsets import csv_response
from core.capabilities import EOL
from core.dates import org_today
from core.drf import (
    CSV_FILE,
    EDITOR,
    IF_MATCH,
    PROBLEMS,
    VIEWER,
    RoleAPIView,
    choices_csv,
    etag,
    iso,
    policy,
    refuse_unknown_params,
    require_match,
    revision,
    uuid_value,
)
from core.input import whole_number
from core.problems import FieldErrors
from imports import csvio

from . import reads, services
from .models import EolProduct, EolRelease, TrackedVersion
from .serializers import (
    OverviewSerializer,
    ProductDetailSerializer,
    ProductSerializer,
    RecordEndOfLifeSerializer,
    TrackedVersionSerializer,
    TrackInputSerializer,
    TrackInstanceInputSerializer,
)
from .status import END_OF_LIFE, STATUSES, derive

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409)}
CONDITIONAL_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409, 412)}
# The revision of "not tracked": an If-Match read from a row that is gone since never matches it.
UNTRACKED = revision(extra={"end_of_life": None})
NOT_TRACKED = "This record's end of life isn't tracked."
DAYS_MAX = 3650
SEARCH_LIMIT = 20


def _not_tracked() -> NotFound:
    return NotFound(NOT_TRACKED)


def _check(request, row: TrackedVersion | None) -> None:
    """If-Match against the tracking row, under its row lock (the caller selected it for update). No row: a create,
    unconditional unless an If-Match was sent (then 412: what it read is gone)."""
    if row is not None:
        require_match(request, revision(row))
    elif request.headers.get("If-Match") is not None:
        require_match(request, UNTRACKED)


def _with_etag(response: Response, row: TrackedVersion | None) -> Response:
    if row is not None:
        response["ETag"] = etag(revision(row))
    return response


class RecordEndOfLifeView(RoleAPIView):
    """`/<applications|components>/{id}/end-of-life`; the URLconf passes `kind`."""

    role_policy = policy(VIEWER, EDITOR, capability=EOL)
    kind: ClassVar[str] = Kind.APPLICATION

    def record(self, pk) -> Resource:
        return get_object_or_404(Resource, organization=self.organization, kind=self.kind, id=pk)

    def row(self, record, lock=False) -> TrackedVersion | None:
        rows = TrackedVersion.objects.filter(organization=self.organization, resource=record, instance__isnull=True)
        return (rows.select_for_update() if lock else rows).first()

    def _body(self, record) -> dict[str, Any]:
        block = reads.record_block(self.organization, record, org_today(self.organization))
        if block is None:
            raise _not_tracked()
        return block

    @extend_schema(
        summary="End-of-life status of this record",
        description="The record's endoflife.date product, version, release cycle and derived status, with its "
        "instances' own rows (applications). 404 when it isn't tracked.",
        responses={200: RecordEndOfLifeSerializer, **ERRORS},
    )
    def get(self, request, pk):
        refuse_unknown_params(request)
        record = self.record(pk)
        return _with_etag(Response(self._body(record)), self.row(record))

    @extend_schema(
        summary="Track this record's end of life (Editor, Admin)",
        description="Creates or changes the tracking. `product` is required the first time and must be in the cached "
        "endoflife.date product list (an alias is accepted and stored as the product's name; any well-formed name "
        "while no list is cached). `cycle` empty or null derives it from `version`, or from the application's own "
        "version field when `version` is empty. A product whose release cycles aren't cached yet is queued for the "
        "worker; its status is unknown until then.",
        parameters=[IF_MATCH],
        request=TrackInputSerializer,
        responses={200: RecordEndOfLifeSerializer, **CONDITIONAL_ERRORS},
    )
    def patch(self, request, pk):
        record = self.record(pk)
        data = self.payload()
        with transaction.atomic():
            _check(request, self.row(record, lock=True))
            services.track(self.organization, request.user, record, data)
        return _with_etag(Response(self._body(record)), self.row(record))

    @extend_schema(
        summary="Stop tracking this record's end of life (Editor, Admin)",
        description="Removes the record's tracking and its instances'. 404 when it isn't tracked.",
        parameters=[IF_MATCH],
        responses={204: None, **ERRORS, 412: PROBLEMS[412]},
    )
    def delete(self, request, pk):
        record = self.record(pk)
        with transaction.atomic():
            row = self.row(record, lock=True)
            if row is None:
                raise _not_tracked()
            require_match(request, revision(row))
            services.untrack(self.organization, request.user, record)
        return Response(status=204)


class InstanceEndOfLifeView(RoleAPIView):
    """`/applications/{id}/instances/{instance_id}/end-of-life`: an instance that runs its own version."""

    role_policy = policy(VIEWER, EDITOR, capability=EOL)

    def instance(self, pk, instance_pk) -> ApplicationInstance:
        return get_object_or_404(
            ApplicationInstance,
            organization=self.organization,
            application__kind=Kind.APPLICATION,
            application_id=pk,
            id=instance_pk,
        )

    def row(self, instance, lock=False) -> TrackedVersion | None:
        rows = TrackedVersion.objects.filter(organization=self.organization, instance=instance)
        return (rows.select_for_update() if lock else rows).first()

    def _body(self, instance) -> dict[str, Any]:
        org = self.organization
        found = reads.rows(org, org_today(org), reads.tracked(org).filter(instance=instance))
        if not found:
            raise _not_tracked()
        return reads.public(found[0])

    @extend_schema(
        summary="End-of-life status of this instance",
        description="404 when the instance has no version of its own (it then follows the application's row).",
        responses={200: TrackedVersionSerializer, **ERRORS},
    )
    def get(self, request, pk, instance_pk):
        refuse_unknown_params(request)
        instance = self.instance(pk, instance_pk)
        return _with_etag(Response(self._body(instance)), self.row(instance))

    @extend_schema(
        summary="Set the version this instance runs (Editor, Admin)",
        description="The product is the application's: track the application first (409 otherwise). `cycle` empty or "
        "null derives it from `version`.",
        parameters=[IF_MATCH],
        request=TrackInstanceInputSerializer,
        responses={200: TrackedVersionSerializer, **CONDITIONAL_ERRORS},
    )
    def patch(self, request, pk, instance_pk):
        instance = self.instance(pk, instance_pk)
        data = self.payload()
        with transaction.atomic():
            _check(request, self.row(instance, lock=True))
            services.track_instance(self.organization, request.user, instance, data)
        return _with_etag(Response(self._body(instance)), self.row(instance))

    @extend_schema(
        summary="Remove this instance's own version (Editor, Admin)",
        description="It follows the application's row again. 404 when it had none.",
        parameters=[IF_MATCH],
        responses={204: None, **ERRORS, 412: PROBLEMS[412]},
    )
    def delete(self, request, pk, instance_pk):
        instance = self.instance(pk, instance_pk)
        with transaction.atomic():
            row = self.row(instance, lock=True)
            if row is None:
                raise _not_tracked()
            require_match(request, revision(row))
            services.untrack_instance(self.organization, request.user, instance)
        return Response(status=204)


# ---- the overview and its CSV ------------------------------------------------------------------------------------

FILTERS = ("status", "days", "product", "kind", "criticality", "owner")
CRITICALITY_VALUES = [*Criticality.values, "none"]
FILTER_PARAMS = [
    OpenApiParameter("status", str, description=f"Comma-separated: {', '.join(STATUSES)}."),
    OpenApiParameter(
        "days",
        int,
        description=f"0 to {DAYS_MAX}: only versions whose end-of-life date is within this many days of today, or "
        "already past (end of life with no date included; other unknown dates are left out).",
    ),
    OpenApiParameter("product", str, description="An endoflife.date product name."),
    OpenApiParameter("kind", str, enum=[Kind.APPLICATION, Kind.COMPONENT]),
    OpenApiParameter(
        "criticality", str, description=f"The record's criticality, comma-separated: {', '.join(CRITICALITY_VALUES)}."
    ),
    OpenApiParameter("owner", str, description="A person's ID: records with them as business or technical owner."),
]


def parse_filters(params) -> dict[str, Any]:
    errors: dict[str, list[str]] = {}
    unknown = sorted(k for k in params if k not in FILTERS)
    for key in unknown:
        errors[key] = ["This filter isn't available here."]
    found: dict[str, Any] = {}
    raw = params.get("status", "")
    if raw:
        values = [v.strip() for v in raw.split(",") if v.strip()]
        if not values or any(v not in STATUSES for v in values):
            errors["status"] = [f"Choose from {', '.join(STATUSES)}, comma-separated."]
        else:
            found["status"] = values
    raw = params.get("days", "")
    if raw:
        days = whole_number(raw, 0, DAYS_MAX)
        if days is None:
            errors["days"] = [f"Enter a number of days from 0 to {DAYS_MAX}."]
        else:
            found["days"] = days
    raw = params.get("product", "")
    if raw:
        found["product"] = raw.strip().lower()
    raw = params.get("kind", "")
    if raw:
        if raw not in (Kind.APPLICATION, Kind.COMPONENT):
            errors["kind"] = ["Choose application or component."]
        else:
            found["kind"] = raw
    raw = params.get("criticality", "")
    if raw:
        try:
            found["criticality"] = choices_csv(CRITICALITY_VALUES)(raw)
        except forms.ValidationError as e:
            errors["criticality"] = list(e.messages)
    raw = params.get("owner", "")
    if raw:
        try:
            found["owner"] = uuid_value(raw)
        except forms.ValidationError as e:
            errors["owner"] = list(e.messages)
    if errors:
        raise FieldErrors(errors)
    return found


def filtered_rows(org, filters: dict[str, Any]) -> list[dict[str, Any]]:
    qs = reads.live(org)
    if "product" in filters:
        qs = qs.filter(product=filters["product"])
    if "kind" in filters:
        qs = qs.filter(resource__kind=filters["kind"])
    if "criticality" in filters:
        wanted = [c for c in filters["criticality"] if c != "none"]
        crit = Q(resource__criticality__in=wanted)
        if "none" in filters["criticality"]:
            crit |= Q(resource__criticality="")
        qs = qs.filter(crit)
    if "owner" in filters:
        qs = qs.filter(
            Q(resource__business_owner_id=filters["owner"]) | Q(resource__technical_owner_id=filters["owner"])
        )
    found = reads.rows(org, org_today(org), qs)
    if "status" in filters:
        found = [r for r in found if r["status"] in filters["status"]]
    if "days" in filters:
        # End of life with no date (endoflife.date says so without one) is past whatever the window.
        found = [
            r
            for r in found
            if r["status"] == END_OF_LIFE
            or (r["days_remaining"] is not None and r["days_remaining"] <= filters["days"])
        ]
    return found


class OverviewView(RoleAPIView):
    role_policy = policy(VIEWER, capability=EOL)

    @extend_schema(
        summary="End-of-life overview",
        description="Every tracked version of live records (not archived, not retired), soonest end of life first "
        "(unknown dates last), with the cache's state. Filters combine.",
        parameters=FILTER_PARAMS,
        responses={200: OverviewSerializer, **ERRORS},
    )
    def get(self, request):
        org = self.organization
        found = filtered_rows(org, parse_filters(request.query_params))
        return Response(
            {
                "today": iso(org_today(org)),
                "cache": reads.cache_status(),
                "counts": reads.counts(found),
                "results": [reads.public(r) for r in found],
            }
        )


CSV_COLUMNS = [
    "record",
    "record_kind",
    "record_id",
    "criticality",
    "business_owner",
    "technical_owner",
    "instance",
    "instance_id",
    "product",
    "product_label",
    "version",
    "version_source",
    "cycle",
    "cycle_source",
    "status",
    "unknown_reason",
    "eol_date",
    "days_remaining",
    "support_until",
    "extended_support_until",
    "latest",
    "why",
]


class OverviewExportView(RoleAPIView):
    role_policy = policy(VIEWER, capability=EOL)

    @extend_schema(
        summary="End-of-life overview as CSV",
        description="The overview's rows with the same filters. Cells are escaped against spreadsheet formulas. "
        "Audited as an export; refused while platform staff impersonate a member.",
        parameters=FILTER_PARAMS,
        responses={**CSV_FILE, **ERRORS},
    )
    def get(self, request):
        org = self.organization
        filters = parse_filters(request.query_params)
        found = [reads.public(r) for r in filtered_rows(org, filters)]
        data = csvio.write_csv(
            CSV_COLUMNS,
            [
                {
                    "record": r["record"]["name"],
                    "record_kind": r["record"]["kind"],
                    "record_id": r["record"]["id"],
                    "criticality": r["record"]["criticality"] or "",
                    "business_owner": (r["record"]["business_owner"] or {}).get("name", ""),
                    "technical_owner": (r["record"]["technical_owner"] or {}).get("name", ""),
                    "instance": (r["instance"] or {}).get("name", ""),
                    "instance_id": (r["instance"] or {}).get("id", ""),
                    **{k: "" if r[k] is None else r[k] for k in CSV_COLUMNS[CSV_COLUMNS.index("product") :]},
                }
                for r in found
            ],
        )
        audit(org, request.user, "export", None, {"kind": "tracked_version", "rows": len(found), "filters": filters})
        return csv_response(data, f"softwarden-end-of-life-{org_today(org).isoformat()}.csv")


# ---- the product list ------------------------------------------------------------------------------------------------


def tracked_products(org) -> set[str]:
    return set(TrackedVersion.objects.filter(organization=org).values_list("product", flat=True))


def product_json(p: EolProduct, mine: set[str]) -> dict[str, Any]:
    """A product as the list and the detail show it. The cache is shared by every organization of the installation and
    holds release cycles only for products someone tracks, so whether they are cached, when they were checked, the
    last error and a rename are shown only for the products the requesting organization tracks itself (null for the
    others): they would otherwise tell one organization what another runs."""
    own = p.name in mine
    return {
        "name": p.name,
        "label": p.label or p.name,
        "category": p.category,
        "aliases": list(p.aliases or []),
        "link": p.link,
        "tracked": own,
        "releases_cached": (p.releases_fetched_at is not None) if own else None,
        "renamed_to": (p.renamed_to or None) if own else None,
    }


def visible_products(org) -> tuple[QuerySet[EolProduct], set[str]]:
    """The products this organization may see: the cached list, and the products it tracks itself (a product the list
    doesn't carry, or every product it tracks before a list is read). A row that exists only because another
    organization tracks it stays hidden."""
    mine = tracked_products(org)
    return EolProduct.objects.filter(Q(listed=True) | Q(name__in=mine)), mine


class ProductListView(RoleAPIView):
    role_policy = policy(VIEWER, capability=EOL)

    @extend_schema(
        summary="Search endoflife.date products",
        description=f"Products in the cached list (and the ones this organization tracks) whose name, label or alias "
        f"contains `search` (names starting with it first), at most {SEARCH_LIMIT}. Empty until the first refresh or "
        "import. `releases_cached` and `renamed_to` are given only for products this organization tracks.",
        parameters=[OpenApiParameter("search", str, description="At least one character; case doesn't matter.")],
        responses={200: ProductSerializer(many=True), **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request, ("search",))
        text = (request.query_params.get("search") or "").strip().lower()[:100]
        qs, mine = visible_products(self.organization)
        if text:
            qs = qs.filter(Q(name__icontains=text) | Q(label__icontains=text) | Q(aliases__icontains=text))
        qs = qs.annotate(
            rank=Case(
                When(name=text, then=Value(0)),
                When(name__startswith=text, then=Value(1)),
                When(label__istartswith=text, then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            )
        ).order_by("rank", "name")
        return Response([product_json(p, mine) for p in qs[:SEARCH_LIMIT]])


class ProductDetailView(RoleAPIView):
    role_policy = policy(VIEWER, capability=EOL)

    @extend_schema(
        summary="One endoflife.date product and, when this organization tracks it, its release cycles",
        description="`releases`, `releases_cached`, `checked_at`, `error` and `renamed_to` are given only for a product "
        "this organization tracks (empty or null otherwise), and its cycles are cached once it is tracked. A product "
        "neither in the cached list nor tracked here is a 404.",
        responses={200: ProductDetailSerializer, **ERRORS},
    )
    def get(self, request, name):
        refuse_unknown_params(request)
        visible, mine = visible_products(self.organization)
        product = get_object_or_404(visible, name=name.strip().lower())
        own = product.name in mine
        today = org_today(self.organization)
        releases = []
        for r in EolRelease.objects.filter(product=product).order_by("position") if own else []:
            derived = derive(r, today, product_label=product.label)
            releases.append(
                {
                    "name": r.name,
                    "label": r.label,
                    "codename": r.codename,
                    "release_date": iso(r.release_date),
                    "lts": r.is_lts,
                    "eol_date": iso(derived.eol_date),
                    "support_until": iso(derived.support_until),
                    "extended_support_until": iso(derived.extended_support_until),
                    "latest": r.latest,
                    "status": derived.status,
                }
            )
        return Response(
            product_json(product, mine)
            | {
                "checked_at": iso(product.releases_checked_at) if own else None,
                "error": (product.error or None) if own else None,
                "releases": releases,
            }
        )
