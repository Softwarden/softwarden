"""`GET /api/v1/composite/...`: compact read endpoints for scripts and the MCP server (api/composite.py).

Why a namespace of their own rather than the UI's endpoints: those answer the screens (full serializers, grouped search
results, allocation detail, keyset pages of full rows) and change with them. These are one stable, compact contract
for tools with a token budget: the same envelope everywhere (`as_of`, `count`, `truncated`, `results`), references as
names and ids with a `path`, and owners spelled out. They call the same service functions the UI's endpoints call, so
the numbers are the UI's numbers.

Roles: any member (Viewer and up), with a session or an API token, read-only tokens included (every route is GET).
Capabilities: deadlines and renewals need renewals or contracts, spend contracts, data gaps data_quality, a dependency
walk from an AI agent ai_agents; a missing one is a 403 problem naming it. Unknown parameters and bad values are a 400
problem naming each one. Search is rate limited like GET /search (the same per-user scope)."""

from __future__ import annotations

from typing import Any

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.response import Response

from catalog import graph, quality
from catalog.models import RECORD_KIND_CHOICES, RECORD_KINDS, Resource
from catalog.services import is_uuid
from core import capabilities as caps
from core import search
from core.dates import org_today
from core.drf import PROBLEMS, VIEWER, RoleAPIView, filter_errors, policy
from core.input import whole_number
from core.problems import FieldErrors
from core.search_views import SEARCH_PARAMS, SearchThrottle, search_options
from core.throttling import with_api_limits
from money import reporting

from . import composite
from . import composite_serializers as cs

ERRORS = {c: PROBLEMS[c] for c in (400, 403)}
LIMIT_HELP = f"At most this many results, 1 to {composite.LIMIT_MAX} (default {composite.LIMIT_DEFAULT})."


def whole_param(name: str, low: int, high: int, default: int, description: str) -> OpenApiParameter:
    """An integer query parameter with its bounds and default in the schema (a client generated from it can check)."""
    schema = {"type": "integer", "minimum": low, "maximum": high, "default": default}
    return OpenApiParameter(name, schema, description=description)


LIMIT = whole_param("limit", 1, composite.LIMIT_MAX, composite.LIMIT_DEFAULT, LIMIT_HELP)
ROLE_NOTE = " Any role, with a session or an API token (read-only tokens included)."


def whole(params, name: str, default: int, low: int, high: int, errors: dict[str, list[str]]) -> int:
    raw = params.get(name)
    if raw in (None, ""):
        return default
    number = whole_number(raw, low, high)
    if number is not None:
        return number
    errors[name] = [f"Enter a whole number from {low} to {high}."]
    return default


class CompositeView(RoleAPIView):
    role_policy = policy(VIEWER)
    params: tuple[str, ...] = ()

    def checked(self) -> tuple[Any, dict[str, list[str]]]:
        """The query parameters and the errors for the ones this route doesn't take."""
        params = self.request.query_params
        return params, filter_errors(None, params, self.params)

    def limit(self, params, errors) -> int:
        return whole(params, "limit", composite.LIMIT_DEFAULT, 1, composite.LIMIT_MAX, errors)


def example(name: str, value: dict[str, Any]) -> list[OpenApiExample]:
    return [OpenApiExample(name, value=value, response_only=True, status_codes=["200"])]


PERSON = {
    "id": "8d0f1d5e-6b1e-4a7c-9f43-2a3c4b5d6e7f",
    "name": "Dana Ortiz",
    "email": "dana@example.com",
    "active": True,
}
STEWARD = {"id": 7, "name": "Sam Lee", "email": "sam@example.com"}
TEAM = {"id": "0b9a8c7d-1e2f-4a3b-8c4d-5e6f7a8b9c0d", "name": "IT Applications"}
RECORD = {
    "id": "3f2a1b0c-9d8e-4f7a-8b6c-5d4e3f2a1b0c",
    "kind": "application",
    "name": "Salesforce",
    "lifecycle": "active",
    "archived": False,
    "path": "applications/3f2a1b0c-9d8e-4f7a-8b6c-5d4e3f2a1b0c",
}
OWNED = {**RECORD, "business_owner": PERSON, "technical_owner": None, "it_steward": STEWARD, "support_team": TEAM}
DEADLINE = {
    "source": "renewal",
    "id": "5a4b3c2d-1e0f-4a9b-8c7d-6e5f4a3b2c1d",
    "name": "Salesforce Sales Cloud",
    "path": "renewals/5a4b3c2d-1e0f-4a9b-8c7d-6e5f4a3b2c1d",
    "date": "2026-12-01",
    "days": 24,
    "overdue": False,
    "action": "cancel_by",
    "label": "Calculated",
    "explanation": "Renewal date 2027-01-31 minus 60 days' notice.",
    "term_ends": "2027-01-31",
    "auto_renew": "yes",
    "decision": None,
    "vendor_name": "Salesforce",
    "contract": {"id": "7c6b5a4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d", "name": "Salesforce 2026"},
    "annualized_cost": "48000.00",
    "currency": "USD",
}


# ---- search ----------------------------------------------------------------------------------------------------------


class CompositeSearchView(CompositeView):
    params = SEARCH_PARAMS
    throttle_classes = with_api_limits(SearchThrottle)

    @extend_schema(
        summary="Search everything (compact)",
        description="GET /search's matching, ranking, capability rules and parameters, flattened into one list: kinds "
        "in a fixed order, best first within each, the snippet as plain text. `count` is every match and `by_kind` "
        "per kind; `truncated` when a kind had more than `limit` (per kind). Rate limited per user with GET /search "
        "(429 with Retry-After)." + ROLE_NOTE,
        parameters=[
            OpenApiParameter("q", str, required=True, description="Words to find; the last one is a prefix."),
            OpenApiParameter("kinds", str, description="Comma-separated; default every kind the organization has."),
            whole_param(
                "limit",
                1,
                search.LIMIT_MAX,
                search.LIMIT_DEFAULT,
                f"Results per kind, 1 to {search.LIMIT_MAX} (default {search.LIMIT_DEFAULT}).",
            ),
            OpenApiParameter("include_archived", bool, description="Default false."),
        ],
        responses={
            200: cs.CompositeSearchSerializer,
            **ERRORS,
            429: OpenApiResponse(description="Too many searches; Retry-After says when to try again"),
        },
        examples=example(
            "Search",
            {
                "query": "sales",
                "ignored": [],
                "fuzzy": False,
                "count": 1,
                "by_kind": {"application": 1, "vendor": 0},
                "truncated": False,
                "results": [
                    {
                        **{k: RECORD[k] for k in ("kind", "id", "name", "path", "lifecycle", "archived")},
                        "snippet": None,
                        "parent": None,
                    }
                ],
            },
        ),
    )
    def get(self, request):
        org = self.organization
        return Response(composite.search_results(org, *search_options(org, request.query_params)))


# ---- deadlines and renewals -------------------------------------------------------------------------------------------

WITHIN_DAYS = whole_param(
    "within_days",
    0,
    composite.DEADLINE_DAYS_MAX,
    composite.DEADLINE_DAYS,
    f"Action deadlines (`date`: the day to cancel or renew by) up to this many days ahead, 0 to "
    f"{composite.DEADLINE_DAYS_MAX} (default {composite.DEADLINE_DAYS}); overdue ones are always included. It filters "
    "on the action deadline, not on `term_ends`: a term ending in 120 days with 60 days' notice is in a 90-day "
    "window. To ask about renewal or end dates, filter the results on `term_ends` (composite/renewals takes "
    "basis=term_end for that).",
)
DEADLINE_HELP = (
    "Every open action deadline up to `within_days` ahead, overdue ones included, soonest first: a renewal's open "
    "cycle (with the contract that drives it, if any) and contracts that drive no renewal, from the dates reminders, "
    "the dashboard and the renewals calendar use. Renewals need the renewals capability and contracts the contracts "
    "capability; without either, 403."
)


class CompositeDeadlinesView(CompositeView):
    params = ("within_days", "limit")

    @extend_schema(
        summary="Upcoming action deadlines (compact)",
        description=DEADLINE_HELP + " `windows` count those due within 30, 60 and 90 days; with within_days=90 they "
        "and `count` are the dashboard's." + ROLE_NOTE,
        parameters=[WITHIN_DAYS, LIMIT],
        responses={200: cs.CompositeDeadlinesSerializer, **ERRORS},
        examples=example(
            "Deadlines",
            {
                "as_of": "2026-11-07",
                "within_days": 90,
                "overdue": 0,
                "windows": [{"days": 30, "count": 1}, {"days": 60, "count": 1}, {"days": 90, "count": 1}],
                "count": 1,
                "truncated": False,
                "results": [{**DEADLINE, "follow_up": STEWARD}],
            },
        ),
    )
    def get(self, request):
        params, errors = self.checked()
        days = whole(params, "within_days", composite.DEADLINE_DAYS, 0, composite.DEADLINE_DAYS_MAX, errors)
        limit = self.limit(params, errors)
        if errors:
            raise FieldErrors(errors)
        org = self.organization
        return Response(composite.deadlines(org, org_today(org), days, limit))


BASIS = OpenApiParameter(
    "basis",
    str,
    enum=list(composite.BASES),
    description="action_deadline (default): `within_days` filters the action deadline, overdue ones included. "
    "term_end: it filters `term_ends` instead (a renewal's renewal date, a contract's end date), ended terms with an "
    "open action included, whenever their deadline falls; rows with no term end are left out; soonest term end first. "
    'Use term_end for "what renews in the next N days".',
)


class CompositeRenewalsView(CompositeView):
    params = ("within_days", "limit", "basis")

    @extend_schema(
        summary="Upcoming renewals with their owners",
        description=DEADLINE_HELP + " Each with who owns it: `owners.renewal_owner` (a renewal's responsible person, "
        "a contract's renewal owner), `owners.contract_owner` (the renewal owner of the contract on the row: the one "
        "driving a renewal, or the contract itself; read it when renewal_owner is null), `owners.follow_up` (a "
        "renewal's IT follow-up), the vendor's account manager "
        "and support email, and the records it covers (a renewal's applications plus its driving contract's lines; a "
        "contract's lines) with their business and technical owners, IT steward and support team, also gathered per "
        "role in `owners`. Nobody is ever silently null: `gaps` names what is missing." + ROLE_NOTE,
        parameters=[WITHIN_DAYS, LIMIT, BASIS],
        responses={200: cs.CompositeRenewalsSerializer, **ERRORS},
        examples=example(
            "Renewals",
            {
                "as_of": "2026-11-07",
                "within_days": 90,
                "basis": "action_deadline",
                "count": 1,
                "truncated": False,
                "results": [
                    {
                        **DEADLINE,
                        "vendor": {
                            "id": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
                            "name": "Salesforce",
                            "account_manager": None,
                            "support_email": "support@example.com",
                        },
                        "owners": {
                            "renewal_owner": None,
                            "contract_owner": PERSON,
                            "follow_up": STEWARD,
                            "business_owners": [PERSON],
                            "technical_owners": [],
                            "it_stewards": [STEWARD],
                            "support_teams": [TEAM],
                        },
                        "records": [OWNED],
                        "records_total": 1,
                        "gaps": [],
                    }
                ],
            },
        ),
    )
    def get(self, request):
        params, errors = self.checked()
        days = whole(params, "within_days", composite.DEADLINE_DAYS, 0, composite.DEADLINE_DAYS_MAX, errors)
        limit = self.limit(params, errors)
        basis = params.get("basis") or "action_deadline"
        if basis not in composite.BASES:
            errors["basis"] = [f"Use one of: {', '.join(composite.BASES)}."]
        if errors:
            raise FieldErrors(errors)
        org = self.organization
        return Response(composite.renewals(org, org_today(org), days, limit, basis))


# ---- spend -----------------------------------------------------------------------------------------------------------


class CompositeSpendView(CompositeView):
    # The capability is checked twice on purpose: here, so an organization without it gets the capability problem
    # before any parameter is validated (as the screens' routes answer), and in the builder (api/composite.py), which
    # the MCP server and scripts may call directly without this view.
    role_policy = policy(VIEWER, capability=caps.CONTRACTS)
    params = ("by", "groups")

    @extend_schema(
        summary="Spend summary",
        description="GET /reports/spend's totals and groups without the allocation detail: the annualized cost of "
        "live contracts (Active, Pending renewal; not archived) per currency, never converted, grouped by `by`. Per "
        "currency the largest `groups` real groups, every named gap, and one Everything else row (gap other) for the "
        "rest, so the groups still add up to `totals[].annualized`; with by=vendor and groups=8 these are the "
        "dashboard's rows. Needs the contracts capability." + ROLE_NOTE,
        parameters=[
            OpenApiParameter("by", str, enum=list(reporting.DIMENSIONS), description="Group by; default vendor."),
            whole_param(
                "groups",
                1,
                composite.LIMIT_MAX,
                composite.SPEND_GROUPS,
                f"Real groups per currency, 1 to {composite.LIMIT_MAX} (default {composite.SPEND_GROUPS}).",
            ),
        ],
        responses={200: cs.CompositeSpendSerializer, **ERRORS},
        examples=example(
            "Spend by vendor",
            {
                "as_of": "2026-11-07",
                "by": "vendor",
                "totals": [
                    {
                        "currency": "USD",
                        "annualized": "48000.00",
                        "run_rate": "48000.00",
                        "one_time": "0.00",
                        "future": "0.00",
                        "future_one_time": "0.00",
                        "next_start": None,
                        "contracts": 1,
                    }
                ],
                "groups": [
                    {
                        "key": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
                        "label": "Salesforce",
                        "gap": None,
                        "currency": "USD",
                        "amount": "48000.00",
                        "contracts": 1,
                        "records": 1,
                        "folded": None,
                    }
                ],
                "truncated": False,
            },
        ),
    )
    def get(self, request):
        from money.report_views import by_param

        params, errors = self.checked()
        groups = whole(params, "groups", composite.SPEND_GROUPS, 1, composite.LIMIT_MAX, errors)
        try:
            by = by_param(params.get("by"))
        except FieldErrors as exc:
            errors.update(exc.errors)
        if errors:
            raise FieldErrors(errors)
        org = self.organization
        return Response(composite.spend(org, org_today(org), by, groups))


# ---- dependencies ----------------------------------------------------------------------------------------------------


class CompositeDependenciesView(CompositeView):
    params = ("target", "direction", "depth", "include_retired", "limit")

    @extend_schema(
        summary="Dependencies of a record (compact)",
        description="The dependency explorer (GET /dependencies) for an application, component or AI agent: what "
        "it relies on (`depends_on`) and what relies on it (`dependents`), each record once by its shortest path, "
        "nearest first, with the relationship of the last link, the records in between and the link's confirmation "
        "state, plus the integrations touching them (`data_flows`). Follows every relationship type marked as a "
        "dependency; cycle-safe. Retired records are left out unless include_retired. Another organization's "
        "record is a 404; an AI agent needs the ai_agents capability." + ROLE_NOTE,
        parameters=[
            OpenApiParameter("target", str, required=True, description="Application, component or AI agent ID."),
            OpenApiParameter("direction", str, enum=list(graph.DIRECTIONS), description="Default both."),
            OpenApiParameter("depth", str, description=f"1 to {graph.MAX_DEPTH}, or all (default 2)."),
            OpenApiParameter("include_retired", bool, description="Default false."),
            LIMIT,
        ],
        responses={200: cs.CompositeDependenciesSerializer, **ERRORS, 404: PROBLEMS[404]},
        examples=example(
            "Dependencies",
            {
                "target": {**RECORD, "technical_owner": None},
                "direction": "both",
                "depth": "2",
                "include_retired": False,
                "count": 1,
                "truncated": False,
                "summary": {"nodes": 1, "data_flows": 0, "cycles": 0, "hidden_retired": 0},
                "depends_on": [
                    {
                        "id": "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b",
                        "kind": "application",
                        "name": "Okta",
                        "path": "applications/9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b",
                        "lifecycle": "active",
                        "archived": False,
                        "hops": 1,
                        "via": "Depends on",
                        "through": [],
                        "cycle": False,
                        "link_state": "confirmed",
                        "technical_owner": None,
                    }
                ],
                "dependents": [],
                "data_flows": [],
            },
        ),
    )
    def get(self, request):
        params, errors = self.checked()
        options, option_errors = graph.parse_options(
            {
                "direction": params.get("direction") or "both",
                "depth": params.get("depth") or "2",
                **({"include_retired": params["include_retired"]} if params.get("include_retired") else {}),
            }
        )
        errors.update(option_errors)
        limit = self.limit(params, errors)
        target_id = params.get("target")
        if not target_id:
            errors["target"] = ["Choose an application, component or AI agent."]
        elif not is_uuid(target_id):
            errors["target"] = ["Enter a record ID."]
        if errors:
            raise FieldErrors(errors)
        org = self.organization
        target = get_object_or_404(
            Resource.objects.select_related("technical_owner"), organization=org, id=target_id
        )  # another organization's: 404
        if target.kind not in RECORD_KINDS:
            raise FieldErrors({"target": ["Choose an application, component or AI agent."]})
        return Response(
            composite.dependencies(
                org, target, options["direction"], options["depth"], options["include_retired"], limit
            )
        )


# ---- discovery summary -----------------------------------------------------------------------------------------------


class CompositeDiscoveryView(CompositeView):
    params = ()

    @extend_schema(
        summary="Discovery summary",
        description="What discovery is waiting on, per connector and in total: candidates to link, create or dismiss "
        "(new, and changed since dismissed), linked records no longer seen in the source (missing) or with a "
        "difference nobody ignored (differs), and the other statuses, by the same rules as GET discovery/candidates "
        "`counts`; plus each connector's last finished run and the run queued or running now. Never a secret or a "
        "connection detail." + ROLE_NOTE,
        responses={200: cs.CompositeDiscoverySerializer, **ERRORS},
        examples=example(
            "Discovery",
            {
                "as_of": "2026-11-07",
                "counts": {
                    "candidates": 3,
                    "new": 2,
                    "changed": 1,
                    "linked": 40,
                    "dismissed": 5,
                    "gone": 0,
                    "out_of_scope": 0,
                    "missing": 1,
                    "differs": 2,
                },
                "connectors_total": 1,
                "truncated": False,
                "connectors": [],
            },
        ),
    )
    def get(self, request):
        _, errors = self.checked()
        if errors:
            raise FieldErrors(errors)
        org = self.organization
        return Response(composite.discovery_summary(org, org_today(org)))


# ---- data gaps -------------------------------------------------------------------------------------------------------


class CompositeDataGapsView(CompositeView):
    # The capability is checked twice on purpose: here, so an organization without it gets the capability problem
    # before any parameter is validated (as the screens' routes answer), and in the builder (api/composite.py), which
    # the MCP server and scripts may call directly without this view.
    role_policy = policy(VIEWER, capability=caps.DATA_QUALITY)
    params = ("rule", "kind", "limit")

    @extend_schema(
        summary="Data gaps",
        description="The data-quality summary (as GET /data-quality/summary: the compliance score, records "
        "checked and complete, counts per rule that is on) and the live records (not archived, not retired) failing "
        "`rule`, or any rule, by name, each with the checks it fails, its completeness and its owners. Needs the "
        "data_quality capability." + ROLE_NOTE,
        parameters=[
            OpenApiParameter("rule", str, description="A rule id that is on (GET /data-quality/rules); default any."),
            OpenApiParameter("kind", str, enum=list(RECORD_KIND_CHOICES), description="A record kind; default all."),
            LIMIT,
        ],
        responses={200: cs.CompositeDataGapsSerializer, **ERRORS},
        examples=example(
            "Data gaps",
            {
                "as_of": "2026-11-07",
                "score": 82,
                "records": 120,
                "complete": 64,
                "by_rule": [
                    {
                        "id": "business_owner",
                        "label": "No business owner",
                        "severity": "high",
                        "records": 120,
                        "failing": 9,
                        "excepted": 0,
                    }
                ],
                "rule": "business_owner",
                "kind": None,
                "count": 9,
                "truncated": False,
                "results": [
                    {
                        **OWNED,
                        "business_owner": None,
                        "criticality": "high",
                        "score": 75,
                        "failing": [{"id": "business_owner", "label": "No business owner", "severity": "high"}],
                    }
                ],
            },
        ),
    )
    def get(self, request):
        params, errors = self.checked()
        limit = self.limit(params, errors)
        org = self.organization
        rule = None
        wanted = (params.get("rule") or "").strip()
        if wanted:
            rule = quality.BY_ID.get(wanted)
            if rule is None or quality.status(rule, org)[0] != quality.ON:
                errors["rule"] = ["Choose a data-quality rule that is on (GET /data-quality/rules)."]
        kind = params.get("kind") or None
        if kind is not None and kind not in RECORD_KIND_CHOICES:
            errors["kind"] = [f"Use one of: {', '.join(RECORD_KIND_CHOICES)}."]
        if errors:
            raise FieldErrors(errors)
        return Response(composite.data_gaps(org, org_today(org), rule, kind, limit))


# ---- expiring credentials --------------------------------------------------------------------------------------------


class CompositeCredentialsView(CompositeView):
    params = ("within_days", "kind", "limit")

    @extend_schema(
        summary="Expiring credentials",
        description="Credentials ending within `within_days` or ended in the last `within_days`, soonest first, as "
        "the dashboard's block (with within_days=30, its rows): API tokens that aren't revoked (an Admin sees the "
        "organization's, others their own) and the client secrets and certificates discovery found on live app "
        "registrations, with the owners of the record each app is linked to. Never key material, a hash or a "
        "secret." + ROLE_NOTE,
        parameters=[
            whole_param(
                "within_days",
                1,
                composite.CREDENTIAL_DAYS_MAX,
                reporting.CREDENTIAL_DAYS,
                f"Ending within this many days, or ended in the last as many; 1 to {composite.CREDENTIAL_DAYS_MAX} "
                f"(default {reporting.CREDENTIAL_DAYS}).",
            ),
            OpenApiParameter("kind", str, enum=list(cs.CREDENTIAL_KINDS), description="Default both."),
            LIMIT,
        ],
        responses={200: cs.CompositeCredentialsSerializer, **ERRORS},
        examples=example(
            "Expiring credentials",
            {
                "as_of": "2026-11-07",
                "within_days": 30,
                "count": 1,
                "truncated": False,
                "results": [
                    {
                        "kind": "discovered_credential",
                        "id": "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e",
                        "name": "Graph sync secret",
                        "prefix": None,
                        "credential_type": "secret",
                        "key_id": "a1b2c3d4-0000-4000-8000-000000000001",
                        "expires_on": "2026-11-20",
                        "days": 13,
                        "expired": False,
                        "owner": None,
                        "application": {"id": "4c5d6e7f-8a9b-4c0d-9e1f-2a3b4c5d6e7f", "name": "Salesforce SSO"},
                        "record": OWNED,
                    }
                ],
            },
        ),
    )
    def get(self, request):
        params, errors = self.checked()
        days = whole(params, "within_days", reporting.CREDENTIAL_DAYS, 1, composite.CREDENTIAL_DAYS_MAX, errors)
        limit = self.limit(params, errors)
        kind = params.get("kind") or None
        if kind is not None and kind not in cs.CREDENTIAL_KINDS:
            errors["kind"] = [f"Use one of: {', '.join(cs.CREDENTIAL_KINDS)}."]
        if errors:
            raise FieldErrors(errors)
        org = self.organization
        return Response(composite.expiring_credentials(org, self.membership, org_today(org), days, kind, limit))
