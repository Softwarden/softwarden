"""The data-flow map: where an organization's classified data goes.

An integration is a data flow from its source record to its target record (catalog/models.Integration); a two-way flow
also carries data back to the source. Each flow carries the integration's data classification (how sensitive the data
is, the Resource's `data_classification`) and its data categories (what the data is: personal, special-category,
financial, credentials, health, customer content, other; `Integration.data_categories`). Records keep their own
classification; an end's vendor is its application's vendor (components have none).

Read only. `report` builds GET /reports/data-flows and its CSV; `graph` the Cytoscape form (the record map's node and
edge shape, catalog/record_map.py); `attention_rows` the Needs attention "Data flows" queue. Live flows only: an
archived or retired integration no longer moves data and is left out. Ends that are archived are shown and flagged.

**Receivers.** The target receives the data; on a two-way flow the source does too. Each risk is judged per receiving
end and names it.

**Risks** (RISKS, in this order):

- `no_dpa`: confidential or restricted data, or personal data at any classification (the flow's data categories
  include personal, special-category personal or health data), goes to an end whose vendor has no DPA (signed on its risk
  profile, or an unexpired DPA document: catalog/vendor_risk.has_dpa, the definition the vendor's own gap uses). A DPA
  marked not applicable answers for confidential data that isn't personal, never for personal data, as the vendor's
  own `dpa_missing` gap treats it. Judged only with the `vendor_risk` capability; without it the vendor's DPA isn't
  known and the flag isn't raised (`vendor_risk` false in the response says so).
- `no_auth_method`: data goes to an external vendor's system (an end with a vendor) and the integration's
  authentication is not recorded, or is recorded as none.
- `down_classification`: the flow is classified higher than the receiving record's own classification, so the data
  lands somewhere treated as less sensitive. A receiving record with no classification isn't compared (the
  data-quality rule on records already lists it).

A flow with no classification has no `down_classification` risk, and `no_dpa` only for personal data; the `flow_classification` data-quality
rule lists it (catalog/quality.py). Every query is scoped to the organization; a Resource is only ever reached through
an Integration of the organization, and composite foreign keys keep both ends in it."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db.models import Q

from core.capabilities import DATA_FLOWS, VENDOR_RISK, has_capability
from core.dates import org_today

from . import vendor_risk
from .models import (
    KIND_PATHS,
    DataCategory,
    DataClassification,
    DpaStatus,
    Integration,
    IntegrationAuthMethod,
    IntegrationDirection,
    Kind,
    Lifecycle,
)
from .record_map import resource_node

RANK: dict[str, int] = {
    DataClassification.PUBLIC: 1,
    DataClassification.INTERNAL: 2,
    DataClassification.CONFIDENTIAL: 3,
    DataClassification.RESTRICTED: 4,
}
CONFIDENTIAL_AND_UP = (DataClassification.CONFIDENTIAL, DataClassification.RESTRICTED)
UNCLASSIFIED = "none"  # the summary's key for a flow with no classification
CATEGORY_LABELS = dict(DataCategory.choices)
CLASSIFICATION_LABELS = dict(DataClassification.choices)
NO_AUTH = ("", IntegrationAuthMethod.NONE)

RISK_LABELS = {
    "no_dpa": "Confidential or personal data to a vendor with no DPA",
    "no_auth_method": "No authentication to an external vendor",
    "down_classification": "Classified above the receiving record",
}
RISKS = tuple(RISK_LABELS)
MAX_GRAPH_EDGES = 1000


@dataclass(frozen=True)
class Filters:
    """What GET /reports/data-flows narrows to. Record IDs are checked to be the organization's by the view."""

    min_classification: str | None = None  # a DataClassification value: it and every higher one
    categories: tuple[str, ...] = ()  # flows carrying any of them
    source: str | None = None
    target: str | None = None
    record: str | None = None  # either end
    risks: tuple[str, ...] = ()  # flows with any of them


def at_least(classification: str) -> list[str]:
    """The classification values at or above `classification`."""
    return [c for c, r in RANK.items() if r >= RANK[classification]]


def _flows(org, filters: Filters):
    qs = (
        Integration.objects.filter(organization=org, resource__archived_at__isnull=True)
        .exclude(resource__lifecycle=Lifecycle.RETIRED)
        .select_related(
            "resource",
            "resource__technical_owner",
            "source_resource__application__vendor",
            "target_resource__application__vendor",
            "source_instance",
            "target_instance",
        )
        .order_by("resource__name", "resource_id")
    )
    if filters.min_classification:
        qs = qs.filter(resource__data_classification__in=at_least(filters.min_classification))
    if filters.categories:
        qs = qs.filter(data_categories__overlap=list(filters.categories))
    if filters.source:
        qs = qs.filter(source_resource_id=uuid.UUID(filters.source))
    if filters.target:
        qs = qs.filter(target_resource_id=uuid.UUID(filters.target))
    if filters.record:
        record = uuid.UUID(filters.record)
        qs = qs.filter(Q(source_resource_id=record) | Q(target_resource_id=record))
    return list(qs)


def _vendor_of(resource):
    if resource.kind != Kind.APPLICATION:
        return None
    application = getattr(resource, "application", None)  # a missing extension row reads as no vendor
    return application.vendor if application is not None else None


def _vendor_facts(org, vendors: Iterable[Any], today: date) -> dict[Any, dict[str, Any]]:
    """{vendor id: its risk facts} from the vendor risk file, in two queries."""
    out = {}
    for vendor_id, f in vendor_risk.files(org, vendors).items():
        p = f.profile
        out[vendor_id] = {
            "has_profile": p is not None,
            "risk_tier": (p.risk_tier if p else "") or None,
            "processes_personal_data": bool(p and p.processes_personal_data),
            "dpa_status": (p.dpa_status if p else "") or None,
            "dpa": vendor_risk.has_dpa(p, f.documents, today),
            "assurance": vendor_risk.has_assurance(p, f.documents, today),
        }
    return out


def _instance(instance) -> dict[str, str] | None:
    return {"id": str(instance.id), "name": instance.name} if instance is not None else None


def _end(resource, instance, facts: dict[Any, dict[str, Any]] | None) -> dict[str, Any]:
    vendor = _vendor_of(resource)
    return {
        "id": str(resource.id),
        "kind": resource.kind,
        "name": resource.name,
        "lifecycle": resource.lifecycle,
        "archived": resource.archived_at is not None,
        "path": f"{KIND_PATHS[resource.kind]}/{resource.id}",
        "classification": resource.data_classification or None,
        "instance": _instance(instance),
        "vendor": {"id": str(vendor.id), "name": vendor.name} if vendor is not None else None,
        "vendor_risk": facts.get(vendor.id) if facts is not None and vendor is not None else None,
    }


def _label(classification: str | None) -> str:
    return CLASSIFICATION_LABELS[classification] if classification else "Unclassified"


def needs_dpa(classification: str | None, personal: bool, facts: dict[str, Any] | None) -> bool:
    """Whether a flow to a vendor with these risk facts raises `no_dpa` (module docstring): confidential or restricted
    data, or personal data at any classification, and the vendor has no DPA. A DPA marked not applicable answers for
    confidential data that isn't personal, never for personal data (as the vendor's own `dpa_missing` gap)."""
    if not (classification in CONFIDENTIAL_AND_UP or personal):
        return False
    if facts is not None and facts["dpa"]:
        return False
    not_applicable = facts is not None and facts["dpa_status"] == DpaStatus.NOT_APPLICABLE
    return personal or not not_applicable


def risks_of(
    classification: str | None, auth_method: str, receivers, risk_on: bool, personal: bool = False
) -> list[dict[str, str]]:
    """The risks of a flow (module docstring), for its receiving ends [(side, end)], in RISKS order then side order.
    `personal`: the flow carries personal data (its data categories)."""
    found: dict[str, list[dict[str, str]]] = {code: [] for code in RISKS}
    rank = RANK.get(classification or "", 0)
    for side, end in receivers:
        vendor = end["vendor"]
        if vendor is not None:
            facts = end["vendor_risk"]
            if risk_on and needs_dpa(classification, personal, facts):
                what = "Personal" if personal else _label(classification)
                why = f"{what} data goes to {end['name']}, and its vendor {vendor['name']} has no DPA."
                found["no_dpa"].append({"code": "no_dpa", "end": side, "why": why})
            if auth_method in NO_AUTH:
                how = "with no authentication" if auth_method else "and how it authenticates isn't recorded"
                why = f"Data goes to {end['name']} at the vendor {vendor['name']} {how}."
                found["no_auth_method"].append({"code": "no_auth_method", "end": side, "why": why})
        end_rank = RANK.get(end["classification"] or "", 0)
        if end_rank and rank > end_rank:
            why = (
                f"{_label(classification)} data goes to {end['name']}, which is classified "
                f"{_label(end['classification']).lower()}."
            )
            found["down_classification"].append({"code": "down_classification", "end": side, "why": why})
    return [{**r, "label": RISK_LABELS[r["code"]]} for code in RISKS for r in found[code]]


def _row(ext, facts: dict[Any, dict[str, Any]] | None, risk_on: bool) -> dict[str, Any]:
    resource = ext.resource
    source = _end(ext.source_resource, ext.source_instance, facts)
    target = _end(ext.target_resource, ext.target_instance, facts)
    receivers = [("target", target)]
    if ext.direction == IntegrationDirection.TWO_WAY:
        receivers.append(("source", source))
    classification = resource.data_classification or None
    owner = resource.technical_owner
    return {
        "id": str(resource.id),
        "name": resource.name,
        "lifecycle": resource.lifecycle,
        "path": f"integrations/{resource.id}",
        "source": source,
        "target": target,
        "direction": ext.direction or None,
        "classification": classification,
        "categories": list(ext.data_categories),
        "contains_personal_data": ext.contains_personal_data,
        "auth_method": ext.auth_method or None,
        "data_purpose": ext.data_purpose,
        "technical_owner": {"id": str(owner.id), "name": owner.name} if owner is not None else None,
        "risks": risks_of(classification, ext.auth_method, receivers, risk_on, ext.contains_personal_data),
        "_steward_id": resource.it_steward_id,
    }


def report(org, filters: Filters, today: date | None = None) -> dict[str, Any]:
    """Every live flow matching `filters`, the most sensitive first (restricted to unclassified, then by name), with
    a summary over those flows. Rows carry `_steward_id` for the Needs attention queue; callers drop it."""
    today = today or org_today(org)
    risk_on = has_capability(org, VENDOR_RISK)
    flows = _flows(org, filters)
    facts = None
    if risk_on:
        vendors = {v.id: v for ext in flows for r in (ext.source_resource, ext.target_resource) if (v := _vendor_of(r))}
        facts = _vendor_facts(org, vendors.values(), today)
    rows = [_row(ext, facts, risk_on) for ext in flows]
    if filters.risks:
        rows = [r for r in rows if any(x["code"] in filters.risks for x in r["risks"])]
    rows.sort(key=lambda r: (-RANK.get(r["classification"] or "", 0), r["name"].lower(), r["id"]))
    by_classification = {c: 0 for c in [*reversed(DataClassification.values), UNCLASSIFIED]}
    by_risk = dict.fromkeys(RISKS, 0)
    for r in rows:
        by_classification[r["classification"] or UNCLASSIFIED] += 1
        for code in {x["code"] for x in r["risks"]}:
            by_risk[code] += 1
    return {
        "as_of": today.isoformat(),
        "vendor_risk": risk_on,
        "summary": {
            "flows": len(rows),
            "risky": sum(1 for r in rows if r["risks"]),
            "personal_data": sum(1 for r in rows if r["contains_personal_data"]),
            "by_classification": by_classification,
            "by_risk": by_risk,
        },
        "flows": rows,
    }


def public(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


# ---- the graph -------------------------------------------------------------------------------------------------------


def categories_label(categories: Iterable[str]) -> str:
    return ", ".join(CATEGORY_LABELS[c] for c in categories)


def graph(org, filters: Filters, today: date | None = None) -> dict[str, Any]:
    """The report's flows as a graph for Cytoscape, in the record map's shape (catalog/record_map.py): nodes are the
    records at the ends, edges the flows, from source to target. An edge's `weight` is its classification's rank
    (0 unclassified to 4 restricted) for colour and width, its `label` its data categories (its name when it has
    none). A node is `risky` when it receives data through a flow with a risk. At most MAX_GRAPH_EDGES flows, the most
    sensitive first; `truncated` says when more matched."""
    found = report(org, filters, today)
    rows = found["flows"]
    shown = rows[:MAX_GRAPH_EDGES]
    nodes: dict[str, dict[str, Any]] = {}
    edges = []
    for r in shown:
        for end in (r["source"], r["target"]):
            if end["id"] not in nodes:
                node = resource_node(end["id"], end["kind"], end["name"], end["lifecycle"], end["archived"])
                node.update(
                    classification=end["classification"],
                    weight=RANK.get(end["classification"] or "", 0),
                    vendor=end["vendor"],
                    risky=False,
                )
                node["detail"] = {"vendor_risk": end["vendor_risk"]}
                nodes[end["id"]] = node
        for risk in r["risks"]:
            nodes[r[risk["end"]]["id"]]["risky"] = True
        two_way = r["direction"] == IntegrationDirection.TWO_WAY
        edges.append(
            {
                "id": f"integration:{r['id']}",
                "source": r["source"]["id"],
                "target": r["target"]["id"],
                "relation": "integration",
                "category": "integration",
                "label": categories_label(r["categories"]) or r["name"],
                "reads": "Exchanges data with" if two_way else "Sends data to",
                "reads_inverse": "Exchanges data with" if two_way else "Receives data from",
                "archived": False,
                "retired": False,
                "classification": r["classification"],
                "weight": RANK.get(r["classification"] or "", 0),
                "categories": r["categories"],
                "contains_personal_data": r["contains_personal_data"],
                "risks": sorted({x["code"] for x in r["risks"]}, key=RISKS.index),
                "detail": {
                    "integration": {"id": r["id"], "name": r["name"], "lifecycle": r["lifecycle"], "path": r["path"]},
                    "direction": r["direction"],
                    "auth_method": r["auth_method"],
                    "source_instance": r["source"]["instance"],
                    "target_instance": r["target"]["instance"],
                },
            }
        )
    return {
        "as_of": found["as_of"],
        "vendor_risk": found["vendor_risk"],
        "nodes": sorted(nodes.values(), key=lambda n: (n["name"].lower(), n["id"])),
        "edges": edges,
        "truncated": len(rows) > len(shown),
        "legend": [
            {"classification": c or None, "label": _label(c or None), "weight": RANK.get(c, 0)}
            for c in [*reversed(DataClassification.values), ""]
        ],
    }


# ---- Needs attention -------------------------------------------------------------------------------------------------


def attention_rows(org, today: date) -> list[dict[str, Any]]:
    """The "Data flows" queue: one row per live flow with a risk, routed to the integration's IT steward (else the
    coordinator), the most sensitive first. Empty without the `data_flows` capability."""
    if not has_capability(org, DATA_FLOWS):
        return []
    out = []
    for r in report(org, Filters(), today)["flows"]:
        if not r["risks"]:
            continue
        out.append(
            {
                "id": r["id"],
                "kind": "data_flow",
                "name": r["name"],
                "classification": r["classification"],
                "source": {"id": r["source"]["id"], "kind": r["source"]["kind"], "name": r["source"]["name"]},
                "target": {"id": r["target"]["id"], "kind": r["target"]["kind"], "name": r["target"]["name"]},
                "risks": [{"code": x["code"], "label": x["label"]} for x in r["risks"]],
                "why": " ".join(x["why"] for x in r["risks"]),
                "steward_id": r["_steward_id"],
            }
        )
    return out
