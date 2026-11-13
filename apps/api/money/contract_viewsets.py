"""Contracts, their lines and licence pools. Routes are in money/urls.py.

Roles: Viewer reads (and exports, and reads the renew form's defaults, the pool totals and the seat review); Editor
creates, edits, archives, restores and renews contracts, adds, changes and removes their lines, creates and edits
licence pools and applies or dismisses proposed seat counts; Admin deletes contracts and licence pools (as records).
Every route needs the `contracts` capability. Every write goes through money/contracts.py, money/renewal_action.py or
money/licences.py."""

from datetime import date
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Count, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse, QueryDict
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from catalog import customfields, services
from catalog.viewsets import CUSTOM_XLSX_TYPES, csv_response
from core.capabilities import CONTRACTS
from core.dates import org_today
from core.drf import (
    ADMIN,
    CSV_FILE,
    EDITOR,
    PAGING_PARAMS,
    PROBLEMS,
    VIEWER,
    TenantViewSet,
    UserBriefSerializer,
    file_response,
    filter_errors,
    policy,
    refuse_unknown_params,
    represent,
    revision,
    timestamp,
    unknown_params,
)
from core.problems import FieldErrors
from discovery.models import Observation, ObservationKind
from imports import csvio, money_import, xlsxio

from . import contracts, licences, renewal_action, renewal_rules
from .contract_filters import ContractFilterSet, LicensePoolFilterSet
from .contract_serializers import (
    BindInputSerializer,
    ContractDetailSerializer,
    ContractInputSerializer,
    ContractLineInputSerializer,
    ContractLineSerializer,
    ContractPreviewSerializer,
    ContractSerializer,
    LicensePoolInputSerializer,
    LicensePoolSerializer,
    PoolTotalsSerializer,
    RenewInputSerializer,
    RenewTermsSerializer,
    SeatApplyInputSerializer,
    SeatDismissInputSerializer,
    SeatReviewSerializer,
    UnboundCreateInputSerializer,
)
from .models import AutoRenew, BillingFrequency, Contract, ContractItem, LicensePool, RenewalCycle
from .renewals import _date as renewal_date
from .renewals import _period as renewal_period
from .renewals import obligations_for, renewal_states
from .serializers import deadline_json, renewal_json

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409)}
FAR = date(9999, 12, 31)  # a missing end or deadline sorts after every real one


def line_counts(org, ids) -> dict[Any, tuple[int, int]]:
    """{contract id: (lines, licence pools)} for a page, in two queries."""
    lines = dict(
        ContractItem.objects.filter(organization=org, contract_id__in=ids)
        .values_list("contract_id")
        .annotate(n=Count("id"))
        .values_list("contract_id", "n")
    )
    pools = dict(
        LicensePool.objects.filter(organization=org, contract_id__in=ids)
        .values_list("contract_id")
        .annotate(n=Count("id"))
        .values_list("contract_id", "n")
    )
    return {i: (lines.get(i, 0), pools.get(i, 0)) for i in ids}


# ---- export -------------------------------------------------------------------------------------------------------------

# The contracts import's template less external_key and create_renewal_obligation, so an export re-imports with every
# row unchanged (imports/money_import.py writes the cells: lines and renewals are named so they read back).
COLUMNS = money_import.EXPORT_COLUMNS[money_import.CONTRACT]
POOL_COLUMNS = money_import.EXPORT_COLUMNS[money_import.LICENSE_POOL]
XLSX_TYPES = {
    "amount": xlsxio.NUMBER,
    "annualized_cost": xlsxio.NUMBER,
    "notice_value": xlsxio.NUMBER,
    "start_date": xlsxio.DATE,
    "end_date": xlsxio.DATE,
    "cancellation_deadline": xlsxio.DATE,
}

PREVIEW_PARAMETERS = [
    OpenApiParameter("amount", str),
    OpenApiParameter("billing_frequency", str, enum=BillingFrequency.values),
    OpenApiParameter("start_date", str),
    OpenApiParameter("end_date", str),
    OpenApiParameter("auto_renew", str, enum=AutoRenew.values),
    OpenApiParameter("notice_value", int),
    OpenApiParameter("notice_unit", str, enum=["days", "months", "years"]),
]
PREVIEW_PARAMS = tuple(p.name for p in PREVIEW_PARAMETERS)  # what the preview reads; anything else is refused


def _docs():
    csv = {**CSV_FILE, **ERRORS}
    xlsx = {**file_response(xlsxio.XLSX_TYPE, "An Excel workbook attachment."), **ERRORS}
    one = {200: ContractDetailSerializer, **WRITE_ERRORS}
    return extend_schema_view(
        list=extend_schema(
            summary="List contracts",
            description="Unarchived by default (archived=true|all). Ordering: name, ends_on (end date, none last), "
            "deadline_on (cancellation deadline, none last), created_at, updated_at. " + customfields.FILTERS_DOC,
        ),
        create=extend_schema(
            summary="Add a contract (Editor, Admin)",
            description="The annualized cost and the cancellation deadline are worked out, never sent. "
            "create_renewal_obligation or renewal_obligation links the contract to a renewal, whose open cycle then "
            "takes the contract's dates (one source of truth).",
            responses={201: ContractSerializer, **WRITE_ERRORS},
        ),
        retrieve=extend_schema(summary="Get a contract with its lines, their share of the cost and its license pools"),
        partial_update=extend_schema(summary="Update a contract (Editor, Admin)"),
        destroy=extend_schema(
            summary="Delete a contract (Admin)",
            description="Refused (409) while a license pool or a next term points at it; archive it instead. Its lines "
            "and attachments go with it; a linked renewal stays.",
        ),
        renewal_terms=extend_schema(
            summary="What renewing this contract would do: whether it can be decided now, and the next term's defaults",
            responses={200: RenewTermsSerializer, **ERRORS},
            filters=False,
        ),
        renew=extend_schema(
            summary="Decide this contract's renewal: renew, renegotiate or cancel (Editor, Admin)",
            description="In one transaction: the decision (with the money and dates before and after, terms_change) "
            "is recorded on the linked renewal's open cycle, which is completed; renewing creates the next term "
            "(predecessor set, linked to the same renewal, which it then drives), copies the chosen lines and moves "
            "(or copies) the chosen license pools, and establishes the renewal's next cycle with the next term's "
            "dates; canceling sets the contract to Canceled. 201 with the next term, or 200 with this contract "
            "when canceled. 409: archived, not signed/active/pending renewal, already has a next term, its renewal "
            "is archived or driven by an unrelated contract, or no end date to renew from. 400: a next term that "
            "doesn't end after this one, or starts before it ends without overlap_confirmed.",
            request=RenewInputSerializer,
            responses={201: ContractDetailSerializer, **one, 412: PROBLEMS[412]},
        ),
        archive=extend_schema(summary="Archive a contract (Editor, Admin)", request=None, responses=one),
        restore=extend_schema(summary="Restore an archived contract (Editor, Admin)", request=None, responses=one),
        preview=extend_schema(
            summary="The annualized cost and deadline a contract with these values would get",
            parameters=PREVIEW_PARAMETERS,
            responses={200: ContractPreviewSerializer, **ERRORS},
            filters=False,
        ),
        export=extend_schema(
            summary="Export the filtered contracts as CSV",
            description="The list's filters (cursor and page_size are ignored). UTF-8 with a BOM, formula-safe "
            "cells; lines as 'Name=40; Name'; one cf_<name> column per custom field of contracts. Audited as `export` "
            "(counts and filters, never contents).",
            responses=csv,
            filters=True,
        ),
        export_xlsx=extend_schema(
            summary="Export the filtered contracts as an Excel workbook",
            description="The CSV export's rows and columns, typed (amounts as numbers, dates as dates), plus a "
            "Custom fields sheet. Audited as `export` with format xlsx.",
            responses=xlsx,
            filters=True,
        ),
    )


@_docs()
class ContractViewSet(TenantViewSet):
    role_policy = policy(VIEWER, EDITOR, destroy=ADMIN)
    required_capability = CONTRACTS
    serializer_class = ContractSerializer
    detail_serializer_class = ContractDetailSerializer
    input_serializer_class = ContractInputSerializer
    filterset_class = ContractFilterSet
    ordering_fields = ("name", "ends_on", "deadline_on", "created_at", "updated_at")
    default_ordering = ("name",)
    if_match_actions = ("renew",)

    def base_queryset(self):
        return Contract.objects.select_related(
            "vendor", "renewal_owner__department", "predecessor", "successor", "renewal_obligation"
        ).annotate(
            ends_on=Coalesce("end_date", Value(FAR)),
            deadline_on=Coalesce("cancellation_deadline", Value(FAR)),
        )

    def revision_of(self, obj) -> str:
        """The contract and its lines (their records and allocations): a line change moves the contract's ETag."""
        lines = sorted(
            f"{i}:{r}:{p}"
            for i, r, p in ContractItem.objects.filter(contract=obj).values_list(
                "id", "resource_id", "allocation_percent"
            )
        )
        return revision(obj, extra={"lines": lines}, skip=("cancellation_deadline",))

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        org = self.organization
        ids = [c.id for c in rows]
        obligations = {c.renewal_obligation_id for c in rows if c.renewal_obligation_id}
        context: dict[str, Any] = {
            "today": org_today(org),
            "lead_days": org.renewal_lead_days,
            "custom_fields": customfields.for_kind(org, contracts.KIND),
            "counts": line_counts(org, ids),
            "drivers": {c.id for c in contracts.drivers(org, obligations).values()},
        }
        if self.action not in ("list", "create", "export", "export_xlsx"):
            lines: dict[Any, list[ContractItem]] = {}
            for item in (
                ContractItem.objects.filter(organization=org, contract_id__in=ids)
                .select_related("resource", "contract")
                .order_by("created_at", "id")
            ):
                lines.setdefault(item.contract_id, []).append(item)
            allocation, unallocated = {}, {}
            for row in contracts.allocations(rows, lines):
                if row.item is None:
                    unallocated[row.contract.id] = row
                else:
                    allocation[row.item.id] = row
            pools: dict[Any, list[LicensePool]] = {}
            for pool in (
                LicensePool.objects.filter(organization=org, contract_id__in=ids)
                .select_related("application")
                .order_by("sku_name", "id")
            ):
                pools.setdefault(pool.contract_id, []).append(pool)
            today = context["today"]
            linked = obligations_for(org).filter(id__in=obligations)
            renewal_bodies = {
                state.obligation.id: renewal_json(org, state, today, history=True)
                for state in renewal_states(org, linked, today, history=True)
            }
            context.update(
                lines=lines, allocation=allocation, unallocated=unallocated, pools=pools, renewals=renewal_bodies
            )
        return context

    # -- service hooks (money/contracts.py validates, scopes and audits)

    def create_object(self, data):
        return contracts.create_contract(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        return contracts.update_contract(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        contracts.delete_contract(self.organization, self.request.user, obj)

    def _set_archived(self, archive: bool):
        with transaction.atomic():
            obj = self.get_object()
            (contracts.archive_contract if archive else contracts.restore_contract)(
                self.organization, self.request.user, obj
            )
            return self.detail_response(self.fresh(obj))

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        return self._set_archived(True)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        return self._set_archived(False)

    @action(detail=True, methods=["get"], url_path="renew")
    def renewal_terms(self, request, pk=None):
        """The renew form: whether POST would be refused and why, the renewal and cycle it records on, and the next
        term's defaults, so the screen never re-implements them."""
        refuse_unknown_params(request)
        contract = self.get_object()
        org = self.organization
        refusal = renewal_action.refusal(contract)
        cycle = (
            RenewalCycle.objects.filter(obligation_id=contract.renewal_obligation_id, completed_on__isnull=True).first()
            if contract.renewal_obligation_id
            else None
        )
        if refusal is None and contract.end_date is None:
            refusal = "Add this contract's end date first: the next term starts after it. (It can still be canceled.)"
        obligation = contract.renewal_obligation
        return Response(
            {
                "can_renew": refusal is None,
                "refusal": refusal,
                "renewal": {"id": str(obligation.id), "name": obligation.name} if obligation else None,
                "open_cycle": None
                if cycle is None
                else {
                    "id": str(cycle.id),
                    "sequence": cycle.sequence,
                    "renewal_date": cycle.renewal_date.isoformat() if cycle.renewal_date else None,
                    "decision": cycle.decision or None,
                },
                "defaults": renewal_action.defaults(org, contract),
            }
        )

    @action(detail=True, methods=["post"])
    def renew(self, request, pk=None):
        data = self.payload()
        with transaction.atomic():
            contract = self.check_if_match(self.get_object())
            outcome = renewal_action.renew(self.organization, request.user, contract, data)
        if outcome.successor is not None:
            return self.detail_response(
                self.fresh(outcome.successor),
                status_code=status.HTTP_201_CREATED,
                location=f"{request.path_info.rsplit('/', 2)[0]}/{outcome.successor.pk}",
            )
        return self.detail_response(self.fresh(outcome.contract))

    @action(detail=False, methods=["get"])
    def preview(self, request):
        """What the form would get, so the screen never re-implements the rules (as renewals/preview)."""
        org = self.organization
        params = request.query_params
        errors: dict[str, list[str]] = {}
        values: dict[str, Any] = {}
        for field in ("start_date", "end_date"):
            renewal_date(params, field, errors, values)
        renewal_period(params, "notice", "notice period", errors, values)
        errors.update(unknown_params(params, PREVIEW_PARAMS))
        amount = contracts.decimal_value(
            params.get("amount"), "amount", errors, maximum=contracts.AMOUNT_MAX, label="amount"
        )
        frequency = params.get("billing_frequency") or BillingFrequency.ANNUAL
        if frequency not in BillingFrequency.values:
            errors["billing_frequency"] = [f"Choose one of: {', '.join(BillingFrequency.values)}."]
        auto_renew = params.get("auto_renew") or AutoRenew.UNKNOWN
        if auto_renew not in AutoRenew.values:
            errors["auto_renew"] = ["Choose yes, no or unknown."]
        if errors:
            raise FieldErrors(errors)
        start, end = values.get("start_date"), values.get("end_date")
        cost = contracts.annualize(amount, frequency, start, end)
        deadline = contracts.deadline_of(
            auto_renew, end, values.get("notice_value"), values.get("notice_unit") or "", org.renewal_lead_days
        )
        return Response(
            {
                "annualized_cost": None if cost is None else str(cost),
                "annualization_note": contracts.annualization_note(amount, frequency, start, end),
                "term_months": contracts.term_months(start, end),
                "deadline": deadline_json(deadline, renewal_rules.timing(deadline.date, org_today(org))),
            }
        )

    # -- CSV and Excel export (any role)

    def export_rows(self, request) -> tuple[list[Contract], dict[str, str]]:
        params = {k: request.query_params[k] for k in request.query_params}
        query = QueryDict(urlencode(params))
        filterset = self.filterset_class(query, queryset=self.get_queryset(), request=request)
        errors = filter_errors(filterset, params, PAGING_PARAMS)
        if errors:
            raise FieldErrors(errors)
        rows = list(filterset.qs.order_by("name", "id"))
        return rows, {k: v for k, v in params.items() if k not in PAGING_PARAMS}

    def _lines(self, rows) -> dict[Any, list[ContractItem]]:
        found: dict[Any, list[ContractItem]] = {}
        items = (
            ContractItem.objects.filter(organization=self.organization, contract_id__in=[c.id for c in rows])
            .select_related("resource")
            .order_by("created_at", "id")
        )
        for item in items:
            found.setdefault(item.contract_id, []).append(item)
        return found

    def _audit_export(self, rows, filters, fmt=None):
        changes: dict[str, Any] = {"kind": "contract", "rows": len(rows), "filters": filters}
        if fmt:
            changes["format"] = fmt
        services.audit(self.organization, self.request.user, "export", None, changes)

    def _name(self, suffix: str) -> str:
        return f"softwarden-contracts-{org_today(self.organization).isoformat()}.{suffix}"

    @action(detail=False, methods=["get"], url_path="export.csv")
    def export(self, request):
        rows, filters = self.export_rows(request)
        custom = customfields.for_kind(self.organization, contracts.KIND)
        lines = self._lines(rows)
        columns = COLUMNS + [customfields.column(cf) for cf in custom]
        data = csvio.write_csv(columns, money_import.export_contract_rows(self.organization, rows, lines, custom))
        self._audit_export(rows, filters)
        return csv_response(data, self._name("csv"))

    @action(detail=False, methods=["get"], url_path="export.xlsx")
    def export_xlsx(self, request):
        rows, filters = self.export_rows(request)
        org = self.organization
        custom = customfields.for_kind(org, contracts.KIND)
        lines = self._lines(rows)
        zone = ZoneInfo(org.timezone)
        columns = [xlsxio.Column(c, XLSX_TYPES.get(c, xlsxio.TEXT)) for c in COLUMNS]
        columns += [
            xlsxio.Column(customfields.column(cf), CUSTOM_XLSX_TYPES.get(cf.type, xlsxio.TEXT)) for cf in custom
        ]
        columns.append(xlsxio.Column("updated_at", xlsxio.DATETIME))

        def values(c, cells):
            stored = c.custom_field_data or {}
            return {
                **cells,
                **{customfields.column(cf): customfields.excel_value(cf, stored.get(cf.name)) for cf in custom},
                "updated_at": c.updated_at.astimezone(zone),
            }

        cells = money_import.export_contract_rows(org, rows, lines, ())
        main = xlsxio.Sheet("Contracts", columns, (values(c, v) for c, v in zip(rows, cells, strict=True)))
        data = xlsxio.write_xlsx([main, *xlsxio.extra_sheets(org, contracts.KIND, rows)])
        self._audit_export(rows, filters, "xlsx")
        response = HttpResponse(data, content_type=xlsxio.XLSX_TYPE)
        response["Content-Disposition"] = f'attachment; filename="{self._name("xlsx")}"'
        return response


@extend_schema_view(
    list=extend_schema(summary="Lines of a contract, oldest first, with their share of the annualized cost"),
    create=extend_schema(
        summary="Put a record on a contract (Editor, Admin)",
        description="One line per record. A fixed allocation that would take the contract's fixed total over 100% is "
        "a field error naming the total; checked under a lock on the contract, so two concurrent edits can't both "
        "pass.",
        responses={201: ContractLineSerializer, **WRITE_ERRORS},
    ),
    retrieve=extend_schema(summary="Get a contract line"),
    partial_update=extend_schema(summary="Change a line's allocation (Editor, Admin)"),
    destroy=extend_schema(summary="Remove a line from a contract (Editor, Admin)"),
)
class ContractLineViewSet(TenantViewSet):
    """A contract's lines. The If-Match on a line's PATCH or DELETE takes the line's ETag."""

    role_policy = policy(VIEWER, EDITOR)
    required_capability = CONTRACTS
    pagination_class = None
    serializer_class = ContractLineSerializer
    input_serializer_class = ContractLineInputSerializer
    lookup_url_kwarg = "item_pk"
    ordering_fields = ("created_at",)
    default_ordering = ("created_at",)

    _parent: Contract

    def parent(self) -> Contract:
        if not hasattr(self, "_parent"):
            self._parent = get_object_or_404(Contract, organization=self.organization, id=self.kwargs["pk"])
        return self._parent

    def base_queryset(self):
        return ContractItem.objects.select_related("resource", "contract")

    def get_queryset(self):
        qs = super().get_queryset()
        return qs if getattr(self, "swagger_fake_view", False) else qs.filter(contract=self.parent())

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        contract = self.parent()
        lines = list(ContractItem.objects.filter(contract=contract).order_by("created_at", "id"))
        return {"allocation": {r.item.id: r for r in contracts.allocations([contract], {contract.id: lines}) if r.item}}

    def list(self, request, *args, **kwargs):
        self.parent()  # 404 for another organization's contract, before filters are read
        return super().list(request, *args, **kwargs)

    def create_object(self, data):
        return contracts.add_item(self.organization, self.request.user, self.parent(), data)

    def update_object(self, obj, data):
        return contracts.update_item(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        contracts.remove_item(self.organization, self.request.user, obj)


SEAT_REVIEW_STATUSES = ("open", "dismissed", "all")


def _source_json(o: Observation) -> dict[str, Any]:
    return {
        "id": str(o.id),
        "display_name": o.display_name,
        "connector": {"id": str(o.connector.id), "name": o.connector.name},
        "last_seen_at": timestamp(o.last_seen_at),
    }


def proposal_json(p: licences.Proposal) -> dict[str, Any]:
    pool, review = p.pool, p.review
    return {
        "pool": {
            "id": str(pool.id),
            "sku_name": pool.sku_name,
            "sku_id": pool.sku_id,
            "application": {"id": str(pool.application_id), "name": pool.application.name},
            "contract": {"id": str(pool.contract.id), "name": pool.contract.name} if pool.contract else None,
            "seats_purchased": pool.seats_purchased,
            "seats_assigned": pool.seats_assigned,
            "seat_sync": pool.seat_sync,
            "last_synced": pool.last_synced.isoformat() if pool.last_synced else None,
        },
        "observation": _source_json(p.observation),
        "observed": p.observed,
        "differences": p.differences,
        "status": p.status,
        "dismissal": {
            "reason": review.reason,
            "by": represent(UserBriefSerializer, review.decided_by),
            "at": timestamp(review.updated_at),
        }
        if p.dismissed and review is not None
        else None,
    }


def unbound_json(o: Observation) -> dict[str, Any]:
    data = o.data if isinstance(o.data, dict) else {}
    return {
        "observation": _source_json(o),
        "sku_id": str(data.get("sku_id") or o.external_id),
        "sku_part_number": str(data.get("sku_part_number") or ""),
        "observed": licences.observed_seats(o),
    }


@extend_schema_view(
    list=extend_schema(summary="List license pools", description=customfields.FILTERS_DOC),
    create=extend_schema(summary="Add a license pool (Editor, Admin)"),
    retrieve=extend_schema(summary="Get a license pool"),
    partial_update=extend_schema(
        summary="Update a license pool (Editor, Admin)",
        description="Seats assigned may exceed seats purchased: overuse is recorded, not refused. last_synced can't be "
        "set: only the seat sync path writes it.",
    ),
    destroy=extend_schema(summary="Delete a license pool (Admin)"),
    export=extend_schema(
        summary="Export the filtered license pools as CSV",
        description="The list's filters (cursor and page_size are ignored). The license pools import's columns less "
        "external_key: the application and the contract named so they read back (name, contract number, or ID when "
        "a name repeats), last_synced for reference (an import never sets it), one cf_<name> column per custom field "
        "of license pools. UTF-8 with a BOM, formula-safe cells. Audited as `export` (counts and filters only).",
        responses={**CSV_FILE, **ERRORS},
        filters=True,
    ),
    totals=extend_schema(
        summary="Seats per application and SKU, and for the organization (license utilization)",
        responses={200: PoolTotalsSerializer, **ERRORS},
        filters=False,
    ),
    seat_review=extend_schema(
        summary="Proposed seat counts: pools whose counts a source disagrees with",
        description="One row per pool and license observation of its SKU (sku_id) whose purchased or assigned seats "
        "differ from the pool's; dismissed ones come back when the source's counts change. `unbound` lists observed "
        "SKUs no pool has.",
        parameters=[
            OpenApiParameter("status", str, enum=list(SEAT_REVIEW_STATUSES), description="Default open."),
            OpenApiParameter("pool", str, description="One pool's proposals."),
        ],
        responses={200: SeatReviewSerializer, **ERRORS},
        filters=False,
    ),
    apply_seat_count=extend_schema(
        summary="Take a source's seat counts into the pool (Editor, Admin)",
        description="An ordinary audited pool edit naming the source; last_synced is not set. 409 when the counts no "
        "longer differ or the source no longer reports the SKU.",
        request=SeatApplyInputSerializer,
        responses={200: LicensePoolSerializer, **WRITE_ERRORS},
    ),
    dismiss_seat_count=extend_schema(
        summary="Keep the pool's counts and say why (Editor, Admin)",
        description="Recorded and audited; the proposal comes back when the source's counts change.",
        request=SeatDismissInputSerializer,
        responses={200: LicensePoolSerializer, **WRITE_ERRORS},
    ),
    create_from_unbound=extend_schema(
        summary="Add a pool for an unbound SKU (Editor, Admin)",
        description="The SKU ID, the SKU name (unless given) and the observed seat counts come from the license "
        "observation; application (required), contract, seat_sync, notes and custom fields from the body. Audited as "
        "license_pool.created naming the source. 409 when a pool already has the SKU or the source no longer reports "
        "it.",
        request=UnboundCreateInputSerializer,
        responses={201: LicensePoolSerializer, **WRITE_ERRORS},
    ),
    bind=extend_schema(
        summary="Bind this pool to an unbound SKU (Editor, Admin)",
        description="Sets the pool's SKU ID from the license observation (license_pool.updated naming the source), "
        "optionally seat_sync. The counts are kept: any difference becomes a proposal, or is synced by the next run "
        "with seat_sync on. 409 when the pool already has a SKU ID, another pool has this SKU, or the source no longer "
        "reports it. If-Match takes the pool's ETag.",
        request=BindInputSerializer,
        responses={200: LicensePoolSerializer, **{c: PROBLEMS[c] for c in (400, 403, 404, 409, 412)}},
    ),
)
class LicensePoolViewSet(TenantViewSet):
    """Seats of an application's SKU, optionally under a contract, with utilization totals and the review of
    seat counts a source disagrees with (money/licences.py)."""

    role_policy = policy(VIEWER, EDITOR, destroy=ADMIN)
    required_capability = CONTRACTS
    serializer_class = LicensePoolSerializer
    input_serializer_class = LicensePoolInputSerializer
    filterset_class = LicensePoolFilterSet
    if_match_actions = ("bind",)
    ordering_fields = ("sku_name", "created_at", "updated_at")
    default_ordering = ("sku_name",)

    def base_queryset(self):
        return LicensePool.objects.select_related("application", "contract")

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        counts: dict[Any, int] = {}
        for p in licences.proposals(self.organization, pools=[r.id for r in rows]):
            counts[p.pool.id] = counts.get(p.pool.id, 0) + 1
        return {
            "custom_fields": customfields.for_kind(self.organization, contracts.POOL_KIND),
            "seat_proposals": counts,
            "sync_sources": licences.sync_sources(self.organization, rows),
        }

    def create_object(self, data):
        return contracts.create_pool(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        return contracts.update_pool(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        contracts.delete_pool(self.organization, self.request.user, obj)

    @action(detail=False, methods=["get"], url_path="export.csv")
    def export(self, request):
        params = {k: request.query_params[k] for k in request.query_params}
        filterset = self.filterset_class(QueryDict(urlencode(params)), queryset=self.get_queryset(), request=request)
        errors = filter_errors(filterset, params, PAGING_PARAMS)
        if errors:
            raise FieldErrors(errors)
        org = self.organization
        rows = list(filterset.qs.order_by("sku_name", "id"))
        custom = customfields.for_kind(org, contracts.POOL_KIND)
        columns = POOL_COLUMNS + [customfields.column(cf) for cf in custom]
        data = csvio.write_csv(columns, money_import.export_pool_rows(org, rows, custom))
        filters = {k: v for k, v in params.items() if k not in PAGING_PARAMS}
        services.audit(
            org, request.user, "export", None, {"kind": "license_pool", "rows": len(rows), "filters": filters}
        )
        return csv_response(data, f"softwarden-license-pools-{org_today(org).isoformat()}.csv")

    @action(detail=False, methods=["get"])
    def totals(self, request):
        refuse_unknown_params(request)
        return Response(licences.totals(self.organization))

    @action(detail=False, methods=["get"], url_path="seat-review")
    def seat_review(self, request):
        params = request.query_params
        errors: dict[str, list[str]] = {}
        wanted = params.get("status") or "open"
        if wanted not in SEAT_REVIEW_STATUSES:
            errors["status"] = ["Use open, dismissed or all."]
        pool = params.get("pool")
        if pool and not services.is_uuid(pool):
            errors["pool"] = ["Choose a license pool from the list."]
        unknown = set(params) - {"status", "pool"}
        errors.update({k: ["This list doesn't take this parameter."] for k in unknown})
        if errors:
            raise FieldErrors(errors)
        if pool:
            get_object_or_404(LicensePool, organization=self.organization, id=pool)
        found = licences.proposals(
            self.organization, pools=[pool] if pool else None, include_dismissed=wanted != "open"
        )
        if wanted == "dismissed":
            found = [p for p in found if p.dismissed]
        unbound = [] if pool else licences.unbound(self.organization)
        return Response(
            {
                "status": wanted,
                "results": [proposal_json(p) for p in found],
                "unbound": [unbound_json(o) for o in unbound],
            }
        )

    def _seat_decision(self, decide):
        data = self.payload()
        with transaction.atomic():
            pool = self.get_object()
            observation = get_object_or_404(
                Observation.objects.select_related("connector"),
                organization=self.organization,
                kind=ObservationKind.LICENSE,
                id=self.kwargs["observation_pk"],
            )
            pool = decide(self.organization, self.request.user, pool, observation, data)
            return self.detail_response(self.fresh(pool))

    @action(detail=True, methods=["post"])
    def apply_seat_count(self, request, pk=None, observation_pk=None):
        return self._seat_decision(licences.apply)

    @action(detail=True, methods=["post"])
    def dismiss_seat_count(self, request, pk=None, observation_pk=None):
        return self._seat_decision(licences.dismiss)

    def _licence_observation(self, observation_id):
        if not services.is_uuid(observation_id):
            raise FieldErrors({"observation": ["Choose an unbound SKU from the seat review."]})
        return get_object_or_404(
            Observation.objects.select_related("connector"),
            organization=self.organization,
            kind=ObservationKind.LICENSE,
            id=observation_id,
        )

    @action(detail=False, methods=["post"])
    def create_from_unbound(self, request, observation_pk=None):
        data = self.payload()
        with transaction.atomic():
            observation = self._licence_observation(str(observation_pk))
            pool = licences.create_from_observation(self.organization, request.user, observation, data)
            return self.detail_response(
                self.fresh(pool),
                status_code=status.HTTP_201_CREATED,
                location=f"{request.path_info.split('/unbound/')[0]}/{pool.pk}",
            )

    @action(detail=True, methods=["post"])
    def bind(self, request, pk=None):
        data = self.payload()
        with transaction.atomic():
            pool = self.check_if_match(self.get_object())
            observation = self._licence_observation(data.get("observation"))
            pool = licences.bind(self.organization, request.user, pool, observation, data)
            return self.detail_response(self.fresh(pool))
