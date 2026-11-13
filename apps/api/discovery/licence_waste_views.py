"""The licence waste routes (discovery/licence_waste.py). Routes in
discovery/urls.py.

- `GET /reports/licence-waste`: per SKU, licences assigned, active, inactive, never signed in, disabled, unknown, and
  the money tied up in the reclaimable ones against the bound pool's contract, with a summary per currency.
- `GET /reports/licence-waste/export.csv`: the same rows as CSV.
- `GET /reports/licence-waste/{sku_id}`: the drill-down, one SKU's users in the chosen categories.
- `GET /reports/licence-waste/{sku_id}/export.csv`: the drill-down as CSV.
- `GET /license-pools/{id}/licence-waste` and `GET /contracts/{id}/licence-waste`: the waste of the SKUs a pool or a
  contract's pools are bound to, for their detail pages.

Roles: any member reads the report and the pool and contract blocks, which are
counts and money, never a person. The drill-down is Admin-only: it says who hasn't signed in, which is personal data
about people's work, and Admins are the role that already manages people's accounts. Even there only a linked
Person's name is shown, or "Unlinked user" and a short hash; no object ID, name or email is stored to show. Both CSVs
are audited as `export` (counts and filters, never contents) and, with the drill-down, refused while platform staff
impersonate (core/impersonation_routes.py). Capability discovery.entra, as the source; the money also needs
`contracts`."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django import forms
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response

from catalog import services
from catalog.filters import in_org_or_404
from catalog.models import Kind, Resource, Vendor
from catalog.viewsets import csv_response
from core.capabilities import CONTRACTS, DISCOVERY_ENTRA, require_capability
from core.dates import org_today
from core.drf import (
    ADMIN,
    CSV_FILE,
    PROBLEMS,
    UNKNOWN_FILTER,
    VIEWER,
    RoleAPIView,
    choice,
    choices_csv,
    policy,
    uuid_value,
)
from core.input import whole_number
from core.problems import FieldErrors
from imports import csvio
from money.models import Contract, LicensePool

from . import licence_waste as waste
from .licence_waste_serializers import (
    LicenceWasteContractSerializer,
    LicenceWastePoolDetailSerializer,
    LicenceWasteReportSerializer,
    LicenceWasteUsersSerializer,
)

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
QUERY_DAYS_MIN = waste.DAYS_MIN  # the same range as the organization setting


def _days(value: str) -> int:
    days = whole_number(value, QUERY_DAYS_MIN, waste.DAYS_MAX)
    if days is None:
        raise forms.ValidationError(f"Enter a number of days from {QUERY_DAYS_MIN} to {waste.DAYS_MAX}.")
    return days


def _count(value: str) -> int:
    n = whole_number(value, 0, 10_000_000)
    if n is None:
        raise forms.ValidationError("Enter a whole number, 0 or more.")
    return n


def _skus(value: str) -> list[str]:
    found = [v.strip().lower() for v in value.split(",") if v.strip()]
    if not found or len(found) > 50 or any(len(v) > 100 for v in found):
        raise forms.ValidationError("Give one to 50 SKU IDs or part numbers, separated by commas.")
    return found


DAYS = OpenApiParameter(
    "days",
    int,
    description=f"N: no successful sign-in within this many days is inactive ({QUERY_DAYS_MIN} to {waste.DAYS_MAX}). "
    "Default: the organization's inactive_seat_days (90 unless an Admin changed it).",
)
REPORT_PARAMS = [
    DAYS,
    OpenApiParameter("sku", str, description="Comma-separated SKU IDs or part numbers (SPE_E5): any of them."),
    OpenApiParameter("application", str, description="Application ID: SKUs bound to its license pools."),
    OpenApiParameter("vendor", str, description="Vendor ID: SKUs whose pools' contract or application it is."),
    OpenApiParameter("min_inactive", int, description="Only SKUs with at least this many inactive members."),
]
USER_PARAMS = [
    DAYS,
    OpenApiParameter(
        "status",
        str,
        description=f"Comma-separated, any of {', '.join(waste.STATUSES)}. Default inactive,disabled (reclaimable).",
    ),
    OpenApiParameter("user_type", str, enum=list(waste.USER_TYPES), description="member (the default), guest, or all."),
]
HELP = (
    "Paid licenses held by people who haven't signed in for `days` days, or by disabled accounts, per SKU, from the "
    "organization's Microsoft Entra license usage connectors (source entra_licence_usage); seat counts from the Entra "
    "licenses source when there is one. Each licensed member is exactly one of active, new (never signed in, created "
    "within `days`), inactive (never_signed_in among them), disabled, or unknown (the last sign-in couldn't be read: "
    "needs Entra ID P1 and AuditLog.Read.All); reclaimable is inactive plus disabled. Guests and service accounts are "
    "counted apart and never priced. The money is annual, in the contract's currency, never converted: the live "
    "contracts of the pools bound to the SKU, their annualized cost over the seats purchased in those pools, times the "
    "reclaimable (annual_waste) and unassigned (annual_unassigned) seats. No price is a named gap, never 0. Any role."
)


def _parse(params, parsers: dict[str, Any]) -> dict[str, Any]:
    errors: dict[str, list[str]] = {}
    found: dict[str, Any] = {}
    for name in params:
        if name not in parsers:
            errors[name] = [UNKNOWN_FILTER]
    for name, parse in parsers.items():
        if name in params:
            try:
                found[name] = parse(params[name])
            except forms.ValidationError as e:
                errors[name] = list(e.messages)
    if errors:
        raise FieldErrors(errors)
    return found


def report_filters(org, params) -> tuple[waste.Filters, dict[str, str]]:
    found = _parse(
        params,
        {"days": _days, "sku": _skus, "application": uuid_value, "vendor": uuid_value, "min_inactive": _count},
    )
    if "application" in found:
        get_object_or_404(Resource, organization=org, id=found["application"], kind=Kind.APPLICATION)
    if "vendor" in found:
        in_org_or_404(Vendor, org, found["vendor"])
    filters = waste.Filters(
        days=found.get("days") or waste.inactive_days(org),
        skus=tuple(found.get("sku", ())),
        application=UUID(found["application"]) if "application" in found else None,
        vendor=UUID(found["vendor"]) if "vendor" in found else None,
        min_inactive=found.get("min_inactive"),
    )
    return filters, {k: params[k] for k in params}


def _report(org, filters) -> dict[str, Any]:
    found = waste.report(org, filters)
    found["rows"] = [waste.public(r) for r in found["rows"]]
    return found


class LicenceWasteReportView(RoleAPIView):
    role_policy = policy(VIEWER, capability=DISCOVERY_ENTRA)

    @extend_schema(
        summary="License waste: paid licenses held by inactive or disabled accounts, and their cost",
        description=HELP,
        parameters=REPORT_PARAMS,
        responses={200: LicenceWasteReportSerializer, **ERRORS},
    )
    def get(self, request):
        org = self.organization
        filters, _ = report_filters(org, request.query_params)
        return Response(LicenceWasteReportSerializer(_report(org, filters)).data)


COLUMNS = [
    "sku_id",
    "sku_part_number",
    "applications",
    "vendors",
    "seats_purchased",
    "seats_assigned",
    "seats_unassigned",
    *waste.COUNT_FIELDS,
    "currency",
    "annual_unit_price",
    "annual_waste",
    "annual_unassigned",
    "gap",
]


def _cell(value) -> Any:
    return "" if value is None else value


def report_cells(r: dict[str, Any]) -> dict[str, Any]:
    p = r["price"]
    return {
        "sku_id": r["sku_id"],
        "sku_part_number": r["sku_part_number"],
        "applications": "; ".join(a["name"] for a in r["applications"]),
        "vendors": "; ".join(v["name"] for v in r["vendors"]),
        **{k: _cell(r[k]) for k in ("seats_purchased", "seats_assigned", "seats_unassigned")},
        **{k: r[k] for k in waste.COUNT_FIELDS},
        "currency": p["currency"] if p else "",
        "annual_unit_price": p["annual_unit_price"] if p else "",
        "annual_waste": _cell(r["annual_waste"]),
        "annual_unassigned": _cell(r["annual_unassigned"]),
        "gap": r["gap"]["label"] if r["gap"] else "",
    }


class LicenceWasteExportView(RoleAPIView):
    role_policy = policy(VIEWER, capability=DISCOVERY_ENTRA)

    @extend_schema(
        summary="The license waste report as CSV",
        description=HELP + " One row per SKU, in the report's order; money with two decimals, empty with a gap (its "
        "words in `gap`). UTF-8 with a BOM, formula-safe cells. Audited as `export` (kind licence_waste, counts and "
        "filters, never contents). Refused while platform staff impersonate.",
        parameters=REPORT_PARAMS,
        responses={**CSV_FILE, **ERRORS},
    )
    def get(self, request):
        org = self.organization
        filters, used = report_filters(org, request.query_params)
        rows = _report(org, filters)["rows"]
        data = csvio.write_csv(COLUMNS, (report_cells(r) for r in rows))
        services.audit(org, request.user, "export", None, {"kind": "licence_waste", "rows": len(rows), "filters": used})
        return csv_response(data, f"softwarden-licence-waste-{org_today(org).isoformat()}.csv")


# ---- the drill-down (Admin) -------------------------------------------------------------------------------------------

DEFAULT_STATUSES = ("inactive", "disabled")


def user_filters(org, params) -> tuple[dict[str, Any], dict[str, str]]:
    found = _parse(
        params, {"days": _days, "status": choices_csv(waste.STATUSES), "user_type": choice(waste.USER_TYPES)}
    )
    return {
        "days": found.get("days") or waste.inactive_days(org),
        "statuses": tuple(found.get("status") or DEFAULT_STATUSES),
        "user_type": found.get("user_type") or "member",
    }, {k: params[k] for k in params}


USERS_HELP = (
    "One SKU's licensed users in the chosen categories (default: inactive and disabled members, the reclaimable "
    "ones), never-signed-in first, then the longest since a sign-in. A user linked to a Person here shows the "
    "Person's name; any other is “Unlinked user” and `ref`, a short stable hash. No object ID, name or email is "
    "stored, so none is shown. Admin only: who hasn't signed in is personal data. Refused while platform staff "
    "impersonate. At most 5,000 users (`truncated`)."
)


class LicenceWasteUsersView(RoleAPIView):
    role_policy = policy(ADMIN, capability=DISCOVERY_ENTRA)

    @extend_schema(
        operation_id="reports_licence_waste_users_retrieve",
        summary="License waste for one SKU: which users hold it without using it",
        description=USERS_HELP,
        parameters=USER_PARAMS,
        responses={200: LicenceWasteUsersSerializer, **ERRORS},
    )
    def get(self, request, sku_id):
        org = self.organization
        found, _ = user_filters(org, request.query_params)
        body = waste.drill(org, str(sku_id).lower(), found["days"], found["statuses"], found["user_type"])
        return Response(LicenceWasteUsersSerializer(body).data)


USER_COLUMNS = [
    "user",
    "ref",
    "status",
    "last_sign_in",
    "days_since",
    "account_enabled",
    "guest",
    "group_assigned",
    "connector",
]


class LicenceWasteUsersExportView(RoleAPIView):
    role_policy = policy(ADMIN, capability=DISCOVERY_ENTRA)

    @extend_schema(
        operation_id="reports_licence_waste_users_export.csv_retrieve",
        summary="License waste for one SKU as CSV",
        description=USERS_HELP + " As CSV: the person's name or “Unlinked user”, the short hash, the category, the "
        "last sign-in (a date, never signed in, no successful sign-in recorded, or unknown), days since, whether the "
        "account is enabled, guest, whether the license comes only through a group, and the connector. Never an object ID, a user principal name or an email. Audited "
        "as `export` (kind licence_waste_users, the SKU, counts and filters).",
        parameters=USER_PARAMS,
        responses={**CSV_FILE, **ERRORS},
    )
    def get(self, request, sku_id):
        org = self.organization
        found, used = user_filters(org, request.query_params)
        sku = str(sku_id).lower()
        body = waste.drill(org, sku, found["days"], found["statuses"], found["user_type"])
        data = csvio.write_csv(
            USER_COLUMNS,
            (
                {
                    "user": u["name"],
                    "ref": u["ref"],
                    "status": u["status"],
                    "last_sign_in": u["last_sign_in"],
                    "days_since": _cell(u["days_since"]),
                    "account_enabled": "yes" if u["account_enabled"] else "no",
                    "guest": "yes" if u["guest"] else "no",
                    "group_assigned": "yes" if u["group_assigned"] else "no",
                    "connector": u["connector"]["name"],
                }
                for u in body["users"]
            ),
        )
        changes = {"kind": "licence_waste_users", "sku": sku, "rows": len(body["users"]), "filters": used}
        services.audit(org, request.user, "export", None, changes)
        return csv_response(data, f"softwarden-licence-waste-{sku}-{org_today(org).isoformat()}.csv")


# ---- on a pool's and a contract's page ------------------------------------------------------------------------------


class LicencePoolWasteView(RoleAPIView):
    role_policy = policy(VIEWER, capability=DISCOVERY_ENTRA)

    @extend_schema(
        summary="License waste of the SKU this license pool is bound to",
        description="The license waste report's row for the pool's SKU (its sku_id), for the pool's page: the SKU's "
        "counts and money across every pool bound to it. `gap` no_sku_id when the pool has no SKU ID; `row` null when "
        "nothing reports the SKU. Any role; a pool of another organization is 404.",
        parameters=[DAYS],
        responses={200: LicenceWastePoolDetailSerializer, **ERRORS},
    )
    def get(self, request, pk):
        org = self.organization
        require_capability(org, CONTRACTS)  # pools are part of contracts
        found = _parse(request.query_params, {"days": _days})
        pool = get_object_or_404(LicensePool, organization=org, pk=pk)
        body = waste.pool_waste(org, pool, found.get("days") or waste.inactive_days(org))
        return Response(LicenceWastePoolDetailSerializer(body).data)


class ContractWasteView(RoleAPIView):
    role_policy = policy(VIEWER, capability=DISCOVERY_ENTRA)

    @extend_schema(
        summary="License waste of the SKUs this contract's license pools are bound to",
        description="The license waste report's rows for the SKUs of the contract's pools, for the contract's page. "
        "Each row is the SKU's whole row (every pool and contract of it) plus `contract_share`: this contract's part "
        "of the row's money, weighted by its own annualized cost: its cost × the reclaimable seats ÷ the seats of every "
        "contract pricing the SKU (null when this contract doesn't price it). `contract_totals` sums those shares; `summary` is the SKUs' whole rows. "
        "Empty when no pool of the contract has a SKU ID. Any role; a contract of another organization is 404.",
        parameters=[DAYS],
        responses={200: LicenceWasteContractSerializer, **ERRORS},
    )
    def get(self, request, pk):
        org = self.organization
        require_capability(org, CONTRACTS)
        found = _parse(request.query_params, {"days": _days})
        contract = get_object_or_404(Contract, organization=org, pk=pk)
        body = waste.contract_waste(org, contract, found.get("days") or waste.inactive_days(org))
        return Response(LicenceWasteContractSerializer(body).data)
