"""The data-flow map's routes (catalog/data_flows.py). Routes are in catalog/urls.py.

- `GET /reports/data-flows`: where classified data goes: every live integration as a flow with its ends, their
  classifications and vendors, its categories and authentication, and its risks; with a summary.
- `GET /reports/data-flows/graph`: the same flows as a graph (nodes are records, edges are flows) for Cytoscape.
- `GET /reports/data-flows/export.csv`: the flows as CSV, audited as an export and refused while impersonating
  (core/impersonation_routes.RESTRICTED).

Filters, the same on all three: `min_classification` (a classification and every higher one), `category`
(comma-separated: flows carrying any), `source`, `target` and `record` (either end) as record IDs, and `risk`
(comma-separated: flows with any). A record ID of another organization answers 404.

Roles: every member reads and exports. Integration fields (classification, categories, authentication) are edited
through the integration routes (Editor). Every route needs the `data_flows` capability."""

from __future__ import annotations

from typing import Any

from django import forms
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response

from core.capabilities import DATA_FLOWS
from core.dates import org_today
from core.drf import CSV_FILE, PROBLEMS, UNKNOWN_FILTER, VIEWER, RoleAPIView, choice, choices_csv, policy, uuid_value
from core.problems import FieldErrors
from imports import csvio

from . import data_flows, services
from .data_flow_serializers import DataFlowGraphSerializer, DataFlowReportSerializer
from .filters import in_org_or_404
from .models import DataCategory, DataClassification, Resource
from .viewsets import csv_response

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
RECORD_FILTERS = ("source", "target", "record")
PARAMS = [
    OpenApiParameter(
        "min_classification",
        str,
        enum=DataClassification.values,
        description="Flows classified at least this (confidential: confidential and restricted).",
    ),
    OpenApiParameter(
        "category", str, description=f"Comma-separated data categories ({', '.join(DataCategory.values)}): any."
    ),
    OpenApiParameter("source", str, description="Record ID at the source end."),
    OpenApiParameter("target", str, description="Record ID at the target end."),
    OpenApiParameter("record", str, description="Record ID at either end."),
    OpenApiParameter(
        "risk", str, description=f"Comma-separated risks ({', '.join(data_flows.RISKS)}): flows with any of them."
    ),
]
HELP = (
    "Live integrations (not archived, not retired) as data flows from source to target; a two-way flow also carries "
    "data back to the source. Risks are judged per receiving end: no_dpa (confidential or restricted data to an end "
    "whose vendor has no DPA; needs the vendor_risk capability), no_auth_method (data to an external vendor's system "
    "with authentication not recorded, or none) and down_classification (the flow is classified higher than the "
    "receiving record). Any role."
)


def parse_filters(org, params) -> tuple[data_flows.Filters, dict[str, str]]:
    errors: dict[str, list[str]] = {}
    for name in params:
        if name not in {"min_classification", "category", "risk", *RECORD_FILTERS}:
            errors[name] = [UNKNOWN_FILTER]
    parsers: dict[str, Any] = {
        "min_classification": choice(DataClassification.values),
        "category": choices_csv(DataCategory.values),
        "risk": choices_csv(data_flows.RISKS),
        **{name: uuid_value for name in RECORD_FILTERS},
    }
    found: dict[str, Any] = {}
    for name, parse in parsers.items():
        if name in params:
            try:
                found[name] = parse(params[name])
            except forms.ValidationError as e:
                errors[name] = list(e.messages)
    if errors:
        raise FieldErrors(errors)
    for name in RECORD_FILTERS:
        if name in found:
            in_org_or_404(Resource, org, found[name])
    filters = data_flows.Filters(
        min_classification=found.get("min_classification"),
        categories=tuple(found.get("category", ())),
        source=found.get("source"),
        target=found.get("target"),
        record=found.get("record"),
        risks=tuple(found.get("risk", ())),
    )
    return filters, {k: params[k] for k in params}


class DataFlowReportView(RoleAPIView):
    role_policy = policy(VIEWER, capability=DATA_FLOWS)

    @extend_schema(
        summary="Where does classified data go",
        description=HELP,
        parameters=PARAMS,
        responses={200: DataFlowReportSerializer, **ERRORS},
    )
    def get(self, request):
        org = self.organization
        filters, _ = parse_filters(org, request.query_params)
        found = data_flows.report(org, filters)
        found["flows"] = [data_flows.public(r) for r in found["flows"]]
        return Response(found)


class DataFlowGraphView(RoleAPIView):
    role_policy = policy(VIEWER, capability=DATA_FLOWS)

    @extend_schema(
        summary="Data flows as a graph",
        description=HELP
        + " As a graph for Cytoscape, in the record map's shape: nodes are the records at the ends, edges the flows "
        "(weight is the classification's rank, 0 to 4, label the data categories). A node is risky when it receives "
        f"data through a flow with a risk. At most {data_flows.MAX_GRAPH_EDGES} flows; `truncated` says when more "
        "matched.",
        parameters=PARAMS,
        responses={200: DataFlowGraphSerializer, **ERRORS},
    )
    def get(self, request):
        org = self.organization
        filters, _ = parse_filters(org, request.query_params)
        return Response(data_flows.graph(org, filters))


COLUMNS = [
    "integration",
    "integration_id",
    "lifecycle",
    "classification",
    "categories",
    "contains_personal_data",
    "direction",
    "auth_method",
    "source",
    "source_kind",
    "source_id",
    "source_instance",
    "source_classification",
    "source_vendor",
    "source_vendor_dpa",
    "source_vendor_assurance",
    "target",
    "target_kind",
    "target_id",
    "target_instance",
    "target_classification",
    "target_vendor",
    "target_vendor_dpa",
    "target_vendor_assurance",
    "technical_owner",
    "risks",
]


def _yes_no(end: dict[str, Any], key: str) -> str:
    facts = end["vendor_risk"]
    if facts is None:
        return ""
    return "yes" if facts[key] else "no"


def cells(r: dict[str, Any]) -> dict[str, Any]:
    source, target = r["source"], r["target"]
    return {
        "integration": r["name"],
        "integration_id": r["id"],
        "lifecycle": r["lifecycle"],
        "classification": r["classification"] or "",
        "categories": "; ".join(r["categories"]),
        "contains_personal_data": "yes" if r["contains_personal_data"] else "no",
        "direction": r["direction"] or "",
        "auth_method": r["auth_method"] or "",
        "source": source["name"],
        "source_kind": source["kind"],
        "source_id": source["id"],
        "source_instance": source["instance"]["name"] if source["instance"] else "",
        "source_classification": source["classification"] or "",
        "source_vendor": source["vendor"]["name"] if source["vendor"] else "",
        "source_vendor_dpa": _yes_no(source, "dpa"),
        "source_vendor_assurance": _yes_no(source, "assurance"),
        "target": target["name"],
        "target_kind": target["kind"],
        "target_id": target["id"],
        "target_instance": target["instance"]["name"] if target["instance"] else "",
        "target_classification": target["classification"] or "",
        "target_vendor": target["vendor"]["name"] if target["vendor"] else "",
        "target_vendor_dpa": _yes_no(target, "dpa"),
        "target_vendor_assurance": _yes_no(target, "assurance"),
        "technical_owner": r["technical_owner"]["name"] if r["technical_owner"] else "",
        "risks": "; ".join(f"{x['label']} ({x['end']})" for x in r["risks"]),
    }


class DataFlowExportView(RoleAPIView):
    role_policy = policy(VIEWER, capability=DATA_FLOWS)

    @extend_schema(
        summary="Data flows as CSV",
        description=HELP + " One row per flow, in the report's order. Cells are escaped against spreadsheet formulas; "
        "vendor DPA and assurance cells are yes or no, empty without a vendor or the vendor_risk capability. Audited "
        "as an export. Refused while impersonating.",
        parameters=PARAMS,
        responses={**CSV_FILE, **ERRORS},
    )
    def get(self, request):
        org = self.organization
        filters, used = parse_filters(org, request.query_params)
        rows = data_flows.report(org, filters)["flows"]
        data = csvio.write_csv(COLUMNS, (cells(r) for r in rows))
        services.audit(org, request.user, "export", None, {"kind": "data_flows", "rows": len(rows), "filters": used})
        return csv_response(data, f"softwarden-data-flows-{org_today(org).isoformat()}.csv")
