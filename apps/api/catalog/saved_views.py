"""Saved views: the columns a view may show, the list's rows for one member,
and the link that opens the view in the SPA. catalog/saved_filters.py keeps the views themselves (create, change,
delete, who sees what); notify/subscriptions.py emails the rows weekly.

**One source of truth.** `run_list` doesn't rebuild the list's query: it runs the list endpoint's own viewset
(catalog/viewsets.py) for the member, with the member's membership and the view's query as the request, so the
role and capability check (core.drf.RolePermission), the organization scope, the FilterSet (custom-field filters
included), the default ordering and the serializers are exactly the ones `GET /applications?...` uses. Values come
from the list serializers (which never carry a secret; an object custom field holding a secret-looking key is shown
as empty) and only the view's columns are kept (`COLUMNS`, the SPA's column ids).

**Columns.** The ids the SPA's record tables use: the record's own columns, the shared ones, `completeness` with
the `data_quality` capability, the compliance columns while the profile is on, and `cf_<name>` for every custom field
that applies to the kind. A view stores an ordered list; empty means the list's defaults. `name` is always shown
first (it links to the record). A column the organization no longer has (a deleted field, the profile turned off) is
left out when the rows are shown; deleting a custom field also removes its column from views
(catalog/customfields.py)."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.http import Http404, HttpRequest, QueryDict
from rest_framework import exceptions
from rest_framework.request import Request

from core.capabilities import DATA_QUALITY, has_capability
from core.problems import FieldErrors

from . import compliance, customfields
from .models import (
    KIND_PATHS,
    AgentChannel,
    AgentPlatform,
    AgentRisk,
    AuthenticationMethod,
    ComponentType,
    Criticality,
    DataClassification,
    DeploymentModel,
    Environment,
    Hosting,
    Kind,
    Lifecycle,
    RiskTier,
    ValidationStatus,
)

COLUMNS_MAX = 30
ROWS_MAX = 50  # rows an email shows; the rest are "and N more" with the link

REVIEW_STATES = {
    "confirmed": "Confirmed",
    "unreviewed": "Unreviewed",
    "needs_confirmation": "Needs confirmation",
    "overdue": "Overdue",
}
# Columns whose values are choice codes: shown by their label.
LABELS: dict[str, dict[str, str]] = {
    "lifecycle": dict(Lifecycle.choices),
    "criticality": dict(Criticality.choices),
    "review_state": REVIEW_STATES,
    "deployment_model": dict(DeploymentModel.choices),
    "authentication_method": dict(AuthenticationMethod.choices),
    "component_type": dict(ComponentType.choices),
    "hosting": dict(Hosting.choices),
    "environment": dict(Environment.choices),
    "platform": dict(AgentPlatform.choices),
    "channels": dict(AgentChannel.choices),
    "risk_classification": dict(AgentRisk.choices),
    "data_classification": dict(DataClassification.choices),
    "validation_status": dict(ValidationStatus.choices),
    "risk_tier": dict(RiskTier.choices),
}

# (id, label, shown by default), in the SPA's order (apps/web catalog-list.tsx recordColumns).
OWN: dict[str, list[tuple[str, str, bool]]] = {
    Kind.APPLICATION: [
        ("vendor", "Vendor", False),
        ("category", "Category", False),
        ("deployment_model", "Deployment model", False),
        ("authentication_method", "Authentication", False),
        ("sso_enabled", "SSO", False),
    ],
    Kind.COMPONENT: [
        ("component_type", "Type", True),
        ("hosting", "Hosting", False),
        ("environment", "Environment", False),
    ],
    Kind.AI_AGENT: [
        ("platform", "Platform", True),
        ("models_used", "Models", True),
        ("risk_classification", "Risk", True),
        ("channels", "Channels", False),
        ("human_in_loop", "Human in the loop", False),
        ("actions_can_write", "Actions can write", False),
        ("environment", "Environment", False),
    ],
}
SHARED_HEAD = [
    ("lifecycle", "Lifecycle", True),
    ("criticality", "Criticality", False),
    ("review_state", "Review state", True),
    ("gaps", "Gaps", True),
]
SHARED_TAIL = [
    ("business_owner", "Business owner", False),
    ("technical_owner", "Technical owner", False),
    ("support_team", "Support team", False),
    ("it_steward", "IT steward", False),
    ("next_review_date", "Next review", False),
]
SHARED_END = [
    ("department", "Department", False),
    ("data_classification", "Data classification", False),
]
COMPLIANCE_COLUMNS = [
    ("validation_status", "Validation status", False),
    ("risk_tier", "Risk tier", False),
]


@dataclass(frozen=True)
class Column:
    id: str
    label: str
    default: bool


def columns_for(org, kind: str) -> dict[str, Column]:
    """The columns a view of `kind` may show in this organization now, in the list's order."""
    own = OWN[kind]
    found = [("name", "Name", True), *[c for c in own if c[2]], *SHARED_HEAD]
    if has_capability(org, DATA_QUALITY):
        found.append(("completeness", "Completeness", True))
    found += SHARED_TAIL
    found += [c for c in own if not c[2]]
    found += SHARED_END
    if compliance.enabled(org):
        found += COMPLIANCE_COLUMNS
    found += [(customfields.column(cf), cf.label, False) for cf in customfields.for_kind(org, kind)]
    return {cid: Column(cid, label, default) for cid, label, default in found}


def clean_columns(value: Any, kind: str, org) -> list[str]:
    """The columns as stored: known ids of the kind, each once, at most COLUMNS_MAX. Field errors name the rest."""
    if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
        raise FieldErrors({"columns": ["Send the columns as a list of column ids."]})
    known = columns_for(org, kind)
    unknown = [c for c in value if c not in known]
    if unknown:
        raise FieldErrors({"columns": [f"Not a column of this list: {', '.join(unknown)}."]})
    if len(set(value)) != len(value):
        raise FieldErrors({"columns": ["Name each column once."]})
    if len(value) > COLUMNS_MAX:
        raise FieldErrors({"columns": [f"Choose at most {COLUMNS_MAX} columns."]})
    return list(value)


def shown_columns(org, kind: str, stored: list[str]) -> list[Column]:
    """The columns a view shows now: its own that still exist (else the list's defaults), `name` first."""
    known = columns_for(org, kind)
    chosen = [known[c] for c in stored if c in known] or [c for c in known.values() if c.default]
    return [known["name"], *[c for c in chosen if c.id != "name"]]


# ---- query keys ---------------------------------------------------------------------------------------------------
# The list parameters a saved view may hold: exactly those the SPA's list applies from its URL, so the link in a
# subscription email shows the same records as the email. Keep in step with apps/web/src/features/catalog/
# catalog-list.tsx (SHARED_KEYS, KIND_KEYS, COMPLIANCE_KEYS, QUALITY_KEYS, i.e. RAW_KEYS, plus `q`, `tag` and the
# `cf_<name>` filters); tests/test_saved_views.py pins this list. The list's chips
# also filter by owner, support team and IT steward, and by periodic_review_due under the compliance profile.
SHARED_QUERY_KEYS = (
    "lifecycle",
    "archived",
    "gap",
    "review_state",
    "imported",
    "criticality",
    "department",
    "data_classification",
    "owner",
    "support_team",
    "it_steward",
)
COMPLIANCE_QUERY_KEYS = ("is_gxp", "validation_status", "risk_tier", "periodic_review_due")
QUALITY_QUERY_KEYS = ("completeness__lt", "completeness__gte", "quality_rule")
KIND_QUERY_KEYS: dict[str, tuple[str, ...]] = {
    Kind.APPLICATION: (
        "category",
        "deployment_model",
        "vendor",
        "authentication_method",
        "sso_enabled",
        "scim_provisioning",
    ),
    Kind.COMPONENT: ("component_type", "hosting", "environment"),
    Kind.AI_AGENT: (
        "platform",
        "channel",
        "risk_classification",
        "human_in_loop",
        "actions_can_write",
        "model",
        "provider",
        "auth_mode",
        "environment",
    ),
}
SEARCH_QUERY_KEYS = ("q", "tag")


def query_keys(kind: str) -> frozenset[str]:
    """The fixed keys a view of `kind` may hold (`cf_<name>` filters are allowed besides)."""
    return frozenset(
        (*SHARED_QUERY_KEYS, *KIND_QUERY_KEYS[kind], *COMPLIANCE_QUERY_KEYS, *QUALITY_QUERY_KEYS, *SEARCH_QUERY_KEYS)
    )


def query_key_errors(kind: str, query: dict[str, Any]) -> dict[str, list[str]]:
    """{parameter: [message]} for keys a saved view can't hold, even where the list API takes them."""
    allowed = query_keys(kind)
    return {
        key: ["A saved view can't hold this filter: the list page doesn't apply it."]
        for key in query
        if key not in allowed and not key.startswith(customfields.COLUMN)
    }


# ---- values ---------------------------------------------------------------------------------------------------------


def _text(value: Any, labels: dict[str, str] | None = None) -> str:
    if value is None or value == "" or value == []:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        name = value.get("name")
        if name is None:
            return "—"
        return f"{name} (inactive)" if value.get("active") is False else str(name)
    if isinstance(value, list):
        return ", ".join(_text(v, labels) for v in value)
    if labels is not None:
        return labels.get(str(value), str(value))
    return str(value)


def cell(row: dict[str, Any], column: str) -> str:
    """One column of one serialized list row, in words."""
    if column == "completeness":
        quality = row.get("quality")
        return f"{quality['score']}%" if isinstance(quality, dict) and "score" in quality else "—"
    if column.startswith(customfields.COLUMN):
        values = row.get(customfields.KEY) or {}
        return _text(values.get(column[len(customfields.COLUMN) :]))
    if column in ("sso_enabled", "human_in_loop", "actions_can_write") and row.get(column) is None:
        return "Not recorded"
    return _text(row.get(column), LABELS.get(column))


# ---- the list, run for one member ---------------------------------------------------------------------------------


def problem_text(errors: dict[str, Any]) -> str:
    """Field errors as 'parameter: message' lines joined (the `problems` wording of saved filter listings)."""
    return "; ".join(
        f"{name}: {' '.join(str(m) for m in (msgs if isinstance(msgs, list) else [msgs]))}"
        for name, msgs in errors.items()
    )


class NotAllowed(Exception):
    """The member may not open the list (role or capability)."""


class BadQuery(Exception):
    """The list refuses the view's query now (a 400 or a 404 from its filters)."""


def _viewset(kind: str):
    from .viewsets import AIAgentViewSet, ApplicationViewSet, ComponentViewSet

    viewsets: dict[str, type] = {
        Kind.APPLICATION: ApplicationViewSet,
        Kind.COMPONENT: ComponentViewSet,
        Kind.AI_AGENT: AIAgentViewSet,
    }
    return viewsets[kind]


def _request(membership, query: dict[str, str]) -> Request:
    http = HttpRequest()
    http.method = "GET"
    http.GET = QueryDict(urllib.parse.urlencode(query))
    http.softwarden_membership = membership  # type: ignore[attr-defined]  # core.access.current_membership reads it
    request = Request(http)
    request.auth = None  # set before user: reading `auth` unset would authenticate (and find nobody)
    request.user = membership.user
    return request


@dataclass
class ListRows:
    rows: list[dict[str, Any]]  # serialized list rows, at most `limit`
    total: int


def run_list(membership, kind: str, query: dict[str, str], limit: int = ROWS_MAX) -> ListRows:
    """The first `limit` rows `GET /<kind list>?<query>` gives `membership` (its default order) and how many there are.
    Raises NotAllowed when the view's permission classes refuse the member (its role, the organization's capabilities,
    anything else the list declares), BadQuery when the list refuses the query.

    The permission step is DRF's own (`check_permissions`, every class the viewset declares). What it can't see:
    the request is the worker's, not a session or a token, so sign-in state doesn't apply, and `must_change_password`
    isn't checked (the membership is given, not resolved from a session): a member who must change their password
    still gets their weekly email, as they still get the digest."""
    from core.drf import KeysetPagination

    view = _viewset(kind)()
    request = _request(membership, query)
    view.request = request
    view.args, view.kwargs = (), {}
    view.action = "list"
    view.action_map = {"get": "list"}
    view.get = view.list  # as_view binds the action to the method; RolePermission skips a method the view lacks
    view.format_kwarg = None
    view.headers = {}
    try:
        view.check_permissions(request)
    except (exceptions.PermissionDenied, exceptions.NotAuthenticated) as refused:
        raise NotAllowed(str(refused.detail)) from None
    try:
        queryset = view.filter_queryset(view.get_queryset())
    except FieldErrors as refused:
        raise BadQuery(problem_text(refused.errors)) from None
    except (exceptions.ValidationError, Http404):
        raise BadQuery("A filter refers to something that no longer exists.") from None
    queryset, keys = KeysetPagination.ranked(queryset, KeysetPagination.ordering(request, view), view)
    ordered = queryset.order_by(*KeysetPagination._order_by(keys))
    total = ordered.count()
    rows = view.render(list(ordered[:limit]), many=True)
    return ListRows(rows=list(rows), total=total)


# ---- links --------------------------------------------------------------------------------------------------------


def base_url() -> str:
    """The address links in notifications start with (SOFTWARDEN_BASE_URL, as the digest and rules use)."""
    return settings.SOFTWARDEN_BASE_URL.rstrip("/")


def list_link(view) -> str:
    """The SPA list route with the view's query, which reproduces its filter: components are the Applications page
    with `kind=components`; `preset=all` makes the list use exactly these parameters (without it, a query of only
    search or tag would add the Active preset); `view` names the view for the page."""
    params: dict[str, str] = {}
    if view.kind == Kind.COMPONENT:
        params["kind"] = "components"
    params.update(view.query if isinstance(view.query, dict) else {})
    params["preset"] = "all"
    params["view"] = str(view.id)
    path = "/applications" if view.kind in (Kind.APPLICATION, Kind.COMPONENT) else f"/{KIND_PATHS[view.kind]}"
    return f"{base_url()}{path}?{urllib.parse.urlencode(params)}"


def record_link(kind: str, record_id: str) -> str:
    return f"{base_url()}/{KIND_PATHS[kind]}/{record_id}"
