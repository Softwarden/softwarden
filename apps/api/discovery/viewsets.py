"""Discovery viewsets: connectors and their runs, the candidate queue with its decisions, and
the sources linked to a record. Routes are in discovery/urls.py with the URLs and response shapes of the function views
they replaced; the rules are in discovery/engine.py, which validates, scopes and audits every write.

Runs: Run now queues a run as its own job and answers 202 with the connector (its `active_run`) and a
Location header for the run; `discovery/runs` is the run history across connectors, newest first, paginated and
filtered; `connectors/{id}/runs` stays the connector's last 50 as a plain array (the shape the SPA reads).

Roles: anyone signed in reads connectors, runs, candidates, suggestions, differences, missing items and sources (never
a secret); Editors and Admins decide candidates, undo, apply or ignore differences and keep or archive a missing
record (archive is an Editor action, as on the record itself); only Admins configure connectors and start runs.
Creating a connector checks the source's capability (core.capabilities.SOURCE_CAPABILITY). Discovery
never writes a curated field on its own: a decision links or creates a record, a source value that differs is
reported (`differences`), and applying one is an ordinary audited record edit under the record's If-Match."""

import base64
import json
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from django import forms
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from catalog.filters import in_org_or_404, worded
from catalog.models import RECORD_KINDS, Kind, Resource
from catalog.services import is_uuid
from catalog.viewsets import record_revision
from core.capabilities import (
    AI_AGENTS,
    ASSISTANT,
    PREVIEW,
    PREVIEW_ONLY,
    SOURCE_CAPABILITY,
    has_capability,
    require_capability,
    source_offered,
)
from core.connections import SECRET_COLUMNS
from core.dates import filter_day
from core.drf import (
    ADMIN,
    EDITOR,
    PROBLEMS,
    VIEWER,
    ParamFilter,
    RoleAPIView,
    TenantFilterSet,
    TenantViewSet,
    choices_csv,
    etag,
    filter_errors,
    policy,
    refuse_unknown_params,
    require_match,
    revision,
    stripped,
    uuid_value,
)
from core.input import whole_number
from core.problems import Conflict, FieldErrors

from . import access, assistant, drift, engine, missing
from .assistant_serializers import AssistantAcceptInputSerializer, AssistantRejectInputSerializer
from .credentials import live as discovered_live
from .models import (
    NOT_CANDIDATES,
    Connector,
    ConnectorRun,
    DecisionKind,
    DiscoveredCredential,
    Observation,
    ReconciliationDecision,
    RunStatus,
    RunTrigger,
    SourceDifference,
)
from .serializers import (
    CandidatePageSerializer,
    ConnectorInputSerializer,
    ConnectorSerializer,
    CreateFromInputSerializer,
    DiscoveredCredentialSerializer,
    DiscoverySourceSerializer,
    DismissInputSerializer,
    DriftInputSerializer,
    DriftSerializer,
    LinkInputSerializer,
    MissingInputSerializer,
    ObservationSerializer,
    ReopenInputSerializer,
    RunRequestSerializer,
    RunSerializer,
    SourceInfoSerializer,
    SuggestionsSerializer,
    connection_of,
    difference_rows,
)

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409)}
RUNS_SHOWN = 50
CANDIDATE_PAGE = 50
CURSOR_MAX = 10**12  # past any queue; keeps OFFSET plus a page inside PostgreSQL's bigint


# ---- connectors -------------------------------------------------------------------------------------------------------


@extend_schema_view(
    list=extend_schema(summary="Discovery connectors with their last run"),
    create=extend_schema(
        summary="Add a connector (Admin)",
        description="Each source needs a capability (discovery.entra, discovery.azure, ...); one the organization's "
        "edition lacks is refused with 403, type /problems/capability-required, naming the capability.",
    ),
    retrieve=extend_schema(summary="Get a connector"),
    partial_update=extend_schema(summary="Change a connector (Admin); send secret only to replace it"),
    destroy=extend_schema(summary="Delete a connector, its runs and observations (Admin); linked records stay"),
    run=extend_schema(
        summary="Queue a run now (Admin); the worker starts it within a minute",
        description="Queues a run and its own discovery.run job (the job carries the connector's ID only). 202 with "
        "the connector, whose active_run is the queued run (id, job_id), and a Location header for the run "
        "(connectors/{id}/runs/{run id}). 403 naming the capability when the organization lacks the source's; 409 "
        "when the connector is off, already has a queued or running run, or its connection isn't tested at its "
        "current revision. The body is optional: accept_empty_listing true makes this one run skip the empty-listing "
        "guard, so a listing that really came back empty marks its observations no longer seen and removes their "
        "credentials (recorded on the run's counts and the connector.run_requested audit event; scheduled runs never "
        "set it).",
        request=RunRequestSerializer,
        responses={202: ConnectorSerializer, **WRITE_ERRORS},
    ),
    access_check=extend_schema(
        summary="Check the connector's permissions now (Admin)",
        description="Signs in with the connector's connection and makes one one-item read per permission the source "
        "uses, outside any transaction, then stores the result as access_check and audits connector.access_checked. "
        "A refused read names the missing grant: a Graph permission (403 Authorization_RequestDenied), the Azure "
        "role Reader with the action and scope ARM named (403 AuthorizationFailed), a Dataverse privilege through an "
        "application user (401/403), the Power Platform management app registration, a Halo scope. Optional ones "
        "(User.ReadBasic.All, AgentIdentity.Read.All, prvReadWorkflow) that aren't granted are reported as "
        "optional_missing and don't make ok false. Azure and Power Platform sign in once per resource (ARM, each "
        "Dataverse environment, the admin API). Needs no successful connection test. 403 naming the capability when "
        "the organization lacks the source's (discovery.azure, discovery.powerplatform, discovery.halo).",
        request=None,
        responses={200: ConnectorSerializer, **ERRORS},
    ),
)
class ConnectorViewSet(TenantViewSet):
    """Read-only sources of observations. Each signs in through a connection (core/connection_viewsets.py); the
    legacy tenant_id, client_id and secret fields create or change the connector's own Microsoft connection. The
    secret is write-only: it is in no response, ETag or audit event; `secret_set_on` says when the connection's
    was last replaced. A run is refused (409, and by the engine) until the connection passed its test at its current
    revision."""

    role_policy = policy(VIEWER, ADMIN)
    pagination_class = None
    serializer_class = ConnectorSerializer
    input_serializer_class = ConnectorInputSerializer
    ordering_fields = ("name", "created_at")
    default_ordering = ("name",)

    def base_queryset(self):
        return Connector.objects.select_related(
            "reviewer",
            "microsoft_connection__tested_by",
            "halo_connection__tested_by",
            "okta_connection__tested_by",
            "google_workspace_connection__tested_by",
            "access_checked_by",
        )

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        by_id = {c.id: c for c in rows}
        runs = ConnectorRun.objects.filter(connector__in=rows).select_related("requested_by")
        # The newest run that is neither queued nor running (those are active_run), by creation, so a run that failed
        # before it started (its connector turned off, its job gone) shows too.
        ended = runs.exclude(status__in=[RunStatus.QUEUED, RunStatus.RUNNING]).order_by("connector_id", "-created_at")
        last = {r.connector_id: r for r in ended.distinct("connector_id")}
        active = {r.connector_id: r for r in runs.filter(status__in=[RunStatus.QUEUED, RunStatus.RUNNING])}
        for r in [*last.values(), *active.values()]:
            r.connector = by_id[r.connector_id]  # the run serializer reads the source's filter labels
        org = self.organization
        return {
            "last_runs": last,
            "active_runs": active,
            "counts": engine.candidate_counts(org),
            "available_sources": {s for s, cap in SOURCE_CAPABILITY.items() if has_capability(org, cap)},
        }

    def revision_of(self, obj) -> str:
        # The connection's state counts (the legacy credential fields change it); never its secret slot.
        connection = connection_of(obj)
        return revision(obj, *([connection] if connection else []), skip=SECRET_COLUMNS)

    def create_object(self, data):
        return engine.create_connector(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        return engine.update_connector(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        engine.delete_connector(self.organization, self.request.user, obj)

    @action(detail=True, methods=["post"])
    def run(self, request, pk=None):
        accept = request.data.get("accept_empty_listing", False) if isinstance(request.data, dict) else False
        if not isinstance(accept, bool):
            raise FieldErrors({"accept_empty_listing": ["Use true or false."]})
        with transaction.atomic():
            connector = self.get_object()
            queued = engine.request_run(self.organization, request.user, connector, accept_empty_listing=accept)
            response = self.detail_response(self.fresh(connector), status_code=status.HTTP_202_ACCEPTED)
        # Relative to this request (…/connectors/{id}/run), so the API prefix and host come from the request.
        response["Location"] = request.build_absolute_uri(f"runs/{queued.id}")
        return response

    @action(detail=True, methods=["post"], url_path="access-check")
    def access_check(self, request, pk=None):
        connector = access.check_access(self.organization, request.user, self.get_object())
        return self.detail_response(self.fresh(connector))


def _day(value: str) -> date | None:
    try:
        return filter_day(value)  # clamped, so the inclusive end's extra day always fits
    except ValueError:
        raise forms.ValidationError("Enter a date as YYYY-MM-DD.") from None


class RunFilterSet(TenantFilterSet):
    """A connector's runs: by status, trigger and the day they were queued or started (organization timezone)."""

    status = ParamFilter(
        parse=choices_csv(RunStatus.values),
        field_name="status",
        lookup_expr="in",
        choices=RunStatus.choices,
        help_text="Comma-separated: queued, running, succeeded, failed.",
    )
    trigger = ParamFilter(
        parse=choices_csv(RunTrigger.values),
        field_name="trigger",
        lookup_expr="in",
        choices=RunTrigger.choices,
        help_text="Comma-separated: manual (Run now), scheduled.",
    )
    to = ParamFilter(
        parse=_day, method="filter_to", help_text="Queued or started on or before (YYYY-MM-DD, organization timezone)."
    )

    class Meta:
        model = ConnectorRun
        fields: list[str] = []

    def _start_of(self, day: date) -> datetime:
        return datetime.combine(day, time.min, tzinfo=ZoneInfo(self.membership.organization.timezone))

    def filter_from(self, qs, name, value):
        return qs.filter(created_at__gte=self._start_of(value)) if value else qs

    def filter_to(self, qs, name, value):
        return qs.filter(created_at__lt=self._start_of(value + timedelta(days=1))) if value else qs


# `from` is a Python keyword, so it is added after the class body (as the audit log's).
RunFilterSet.base_filters["from"] = ParamFilter(
    parse=_day,
    method="filter_from",
    field_name="created_at",
    help_text="Queued or started on or after (YYYY-MM-DD, organization timezone).",
)


class RunHistoryFilterSet(RunFilterSet):
    connector = ParamFilter(
        parse=worded(uuid_value, "Choose a connector from the list."),
        method="filter_connector",
        help_text="Connector ID: one connector's history.",
    )

    class Meta:
        model = ConnectorRun
        fields: list[str] = []

    def filter_connector(self, qs, name, value):
        in_org_or_404(Connector, self.membership.organization, value)
        return qs.filter(connector_id=value)


RunHistoryFilterSet.base_filters["from"] = RunFilterSet.base_filters["from"]


@extend_schema_view(
    list=extend_schema(
        summary="The connector's last 50 runs, newest first",
        description="A plain array (the shape the connector page reads). For the whole history, paged, use "
        "GET discovery/runs?connector={id}.",
        filters=True,
    ),
    retrieve=extend_schema(summary="Get one run of a connector (poll it for a queued run's progress)"),
)
class ConnectorRunViewSet(TenantViewSet):
    """A connector's runs. The worker writes them (discovery/engine.py); nothing here does."""

    role_policy = policy(VIEWER)
    pagination_class = None
    serializer_class = RunSerializer
    filterset_class = RunFilterSet
    lookup_url_kwarg = "run_pk"
    ordering_fields = ("created_at",)
    default_ordering = ("-created_at",)
    list_limit = RUNS_SHOWN

    def connector(self):
        if not hasattr(self, "_connector"):
            self._connector = get_object_or_404(Connector, organization=self.organization, id=self.kwargs["pk"])
        return self._connector

    def base_queryset(self):
        return ConnectorRun.objects.select_related("requested_by", "connector")

    def get_queryset(self):
        qs = super().get_queryset()
        return qs if getattr(self, "swagger_fake_view", False) else qs.filter(connector=self.connector())


@extend_schema_view(
    retrieve=extend_schema(
        summary="Get one run (any connector of the organization; poll it for a queued run's progress)",
        description="The same run as connectors/{connector id}/runs/{id}, by its own ID. 404 for another "
        "organization's run.",
    ),
    list=extend_schema(
        summary="Run history across the organization's connectors, newest first",
        description="Every run of every connector (or one, with connector), paged with cursors: when it was queued, "
        "started and finished, its duration, trigger (manual or scheduled), who started it, status, the error in "
        "Softwarden's words, the counts, what each filter skipped (labeled) and the run's notes. Filter by status, "
        "trigger and day (from, to; organization timezone).",
        parameters=[OpenApiParameter("cursor", str, description="The next value from a page.")],
        filters=True,
    ),
)
class RunHistoryViewSet(TenantViewSet):
    """Run history. Read-only; the worker writes runs."""

    role_policy = policy(VIEWER)
    serializer_class = RunSerializer
    filterset_class = RunHistoryFilterSet
    ordering_fields = ("created_at",)
    default_ordering = ("-created_at",)

    def base_queryset(self):
        return ConnectorRun.objects.select_related("requested_by", "connector")


# ---- sources ------------------------------------------------------------------------------------------------------------


class DiscoverySourcesView(RoleAPIView):
    """What each discovery source needs before a connector of it exists: the guided setup's Prepare step
    shows the first connector's permissions from here. The same for every organization, except `available`."""

    role_policy = policy(VIEWER)

    @extend_schema(
        summary="The discovery sources, with their capability, connection kind, permissions, scope and filters",
        description="One entry per source, in the order the setup lists them. `available` says whether this "
        "organization's edition includes the source's capability (creating a connector without it is a 403); "
        "the preview sources (Okta, Google Workspace, and Microsoft Entra license usage until its report has a "
        "screen) are left out while the installation keeps them off. "
        "`permissions` has the shape of a connector's; `scope_defaults` the scope a new connector starts with; "
        "`filters` each scope filter's words, as run counts' skipped name them.",
        responses={200: SourceInfoSerializer(many=True), **ERRORS},
    )
    def get(self, request):
        refuse_unknown_params(request)
        org = self.organization
        rows = []
        for source, module in engine.SOURCES.items():
            capability = SOURCE_CAPABILITY[source]
            if (capability in PREVIEW_ONLY and not has_capability(org, PREVIEW)) or not source_offered(org, source):
                continue  # a preview source while the installation keeps them off: not offered at all
            rows.append(
                {
                    "id": source.value,
                    "label": str(source.label),
                    "capability": capability,
                    "available": has_capability(org, capability),
                    "connection_kind": module.CONNECTION,
                    "permissions": [p.as_dict() for p in getattr(module, "PERMISSIONS", [])],
                    "scope_defaults": dict(module.SCOPE_DEFAULTS),
                    "filters": dict(module.FILTERS),
                }
            )
        return Response(rows)


# ---- candidates and decisions -------------------------------------------------------------------------------------------

CANDIDATE_STATUSES = ("candidates", *engine.STATUSES, *engine.LINKED_FILTERS, "all")


def _status(value: str) -> str:
    if value not in CANDIDATE_STATUSES:
        raise forms.ValidationError("Use candidates, new, changed, linked, dismissed, gone, missing, differs or all.")
    return value


def _offset(value: str) -> int:
    offset = whole_number(value or "0", 0, CURSOR_MAX)
    if offset is None:
        raise FieldErrors({"cursor": ["Pass back the next value from the previous page."]})
    return offset


# `after` pages by the last row's (display_name, id), the list's own order, so a row decided while walking (it
# leaves the "candidates" list) never makes the walk skip the one after it, as the offset `cursor` does.


def after_token(row) -> str:
    raw = json.dumps([row.display_name, str(row.id)], ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _after(value: str) -> Q:
    try:
        name, pk = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if not isinstance(name, str) or not is_uuid(pk):
            raise ValueError
    except (ValueError, TypeError, UnicodeDecodeError):
        raise FieldErrors({"after": ["Pass back next_after from the previous page."]}) from None
    return Q(display_name__gt=name) | Q(display_name=name, id__gt=pk)


class CandidateFilterSet(TenantFilterSet):
    status = ParamFilter(
        parse=_status,
        method="filter_status",
        choices=[(s, s) for s in CANDIDATE_STATUSES],
        help_text="candidates (new and changed, the default), one status, missing (linked and no longer seen, not yet "
        "kept or archived), differs (linked with a source difference nobody ignored), or all.",
    )
    connector = ParamFilter(
        parse=worded(uuid_value, "Choose a connector from the list."),
        method="filter_connector",
        help_text="Connector ID.",
    )
    q = ParamFilter(
        parse=stripped, field_name="display_name", lookup_expr="icontains", help_text="Search source names."
    )

    class Meta:
        model = Observation
        fields: list[str] = []

    def filter_status(self, qs, name, value):
        return qs  # applied after the per-status counts, which ignore it (ObservationViewSet.list)

    def filter_connector(self, qs, name, value):
        in_org_or_404(Connector, self.membership.organization, value)
        return qs.filter(connector_id=value)


def _decision_docs(summary, request, created=False, description=None):
    return extend_schema(
        summary=summary,
        description=description,
        request=request,
        responses={201 if created else 200: ObservationSerializer, **WRITE_ERRORS},
    )


IF_MATCH_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409, 412)}
RECORD_IF_MATCH = (
    "If-Match takes the linked record's ETag (from GET /applications/{id}, /components/{id}, /ai-agents/{id} or "
    "record_etag from GET .../drift), not the observation's: 412 when the record changed since."
)


@extend_schema_view(
    list=extend_schema(
        summary="Discovery candidates and decided observations",
        description="50 at a time by source name. Two ways to page: `after` (the previous page's next_after) is a "
        "keyset and stays stable while candidates are decided, so scripts walking the queue use it; `cursor` (the "
        "previous page's next) is an offset the SPA shows positions with, and skips a row when one before it is "
        "decided meanwhile. Send one or the other.",
        parameters=[
            OpenApiParameter("cursor", str, description="The next value from the previous page (an offset)."),
            OpenApiParameter("after", str, description="The next_after value from the previous page (a keyset)."),
        ],
        responses={200: CandidatePageSerializer, **ERRORS},
        filters=True,
    ),
    retrieve=extend_schema(summary="Get one observation with its decision"),
    link=_decision_docs("Link a candidate to an existing record (Editor, Admin)", LinkInputSerializer),
    create_record=_decision_docs(
        "Create a record from a candidate and link it (Editor, Admin)", CreateFromInputSerializer, created=True
    ),
    dismiss=extend_schema(
        summary="Dismiss a candidate with a reason (Editor, Admin)",
        description="Takes If-Match with the candidate's ETag (GET discovery/observations/{id}): 412 when it changed "
        "since (so the MCP server's dismiss_candidate never acts on a stale read).",
        request=DismissInputSerializer,
        responses={200: ObservationSerializer, **IF_MATCH_ERRORS},
    ),
    reopen=_decision_docs(
        "Undo the decision (Editor, Admin)",
        ReopenInputSerializer,
        description="Removes the link, the created link or the dismissal; every record stays. With "
        "`archive: true` the record created from this candidate is archived too (audited; observation.reopened carries "
        "archived), only while it is still the candidate's own: 409 when it was changed since it was created, when "
        "anything references it (blocking_references: contract lines, license pools, relationships, integration ends, "
        "reviews, other linked sources), and nothing is changed; 400 when the decision wasn't a create. An already "
        "archived record stays archived.",
    ),
    assistant_accept=_decision_docs(
        "Accept the assistant's suggestion (Editor, Admin; capability assistant)",
        AssistantAcceptInputSerializer,
        description="Performs the suggested action through the ordinary decision (observation.linked, .created or "
        ".dismissed, audited as usual) and records the suggestion as accepted (assistant_suggestion.recorded). Link "
        "takes no fields; create takes the create form's fields; dismiss takes a reason (default: the suggestion's). "
        "409 when the candidate has no ready, undecided suggestion for its current fingerprint, or it was already "
        "decided. 403 naming the capability without it.",
    ),
    assistant_reject=_decision_docs(
        "Reject the assistant's suggestion (Editor, Admin; capability assistant)",
        AssistantRejectInputSerializer,
        description="Records a no on the suggestion (assistant_suggestion.rejected) with an optional note; the "
        "candidate stays undecided. 409 as for accept.",
    ),
    suggestions=extend_schema(
        summary="Records with a name like the observation's, best first, with a score",
        description="Live records of compatible kinds (application for an application, component for a component, "
        "AI agent or application for an AI agent) found by pg_trgm similarity or word similarity, at most `limit` "
        "(1 to 10, default 5). Only suggestions: nothing links on its own.",
        parameters=[OpenApiParameter("limit", int, description="1 to 10, default 5.")],
        request=None,
        responses={200: SuggestionsSerializer, **ERRORS},
    ),
    drift=extend_schema(
        summary="What the source says differently from the linked record, per field",
        description="Linked or created observations only (409 otherwise). Each difference has the record's value, the "
        "source's, since when, and whether someone ignored it. Never applied on its own. The response's ETag header "
        "and record_etag are the record's.",
        request=None,
        responses={200: DriftSerializer, **WRITE_ERRORS},
    ),
    drift_apply=extend_schema(
        summary="Take the source's value into the record (Editor, Admin)",
        description="An ordinary audited record edit (resource.updated, with `discovery` naming the observation and "
        "connector) of each chosen field, default every difference nobody ignored. " + RECORD_IF_MATCH + " `seen` "
        "guards the source side: 409 if a run changed a value you reviewed. 409 when the record is archived or the "
        "observation is gone or not linked.",
        request=DriftInputSerializer,
        responses={200: DriftSerializer, **IF_MATCH_ERRORS},
    ),
    drift_ignore=extend_schema(
        summary="Keep the record's value until the source changes again (Editor, Admin)",
        description="Hides each chosen difference (default every one not ignored) until the source's value changes; "
        "audited as observation.drift_ignored on the record.",
        request=DriftInputSerializer,
        responses={200: DriftSerializer, **WRITE_ERRORS},
    ),
    missing_acknowledge=_decision_docs(
        "Keep the record the source no longer lists (Editor, Admin)",
        MissingInputSerializer,
        description="Resolves the Needs attention row (observation.missing_resolved, kept). The observation stays "
        "linked; if the object comes back, the resolution clears so a later disappearance asks again.",
    ),
    missing_archive=extend_schema(
        summary="Archive the record the source no longer lists (Editor, Admin)",
        description="Archives the record (resource.archived) and resolves the row (observation.missing_resolved, "
        "archived). " + RECORD_IF_MATCH + " 409 while another source linked to the record still sees it.",
        request=MissingInputSerializer,
        responses={200: ObservationSerializer, **IF_MATCH_ERRORS},
    ),
)
class ObservationViewSet(TenantViewSet):
    """What sources last said, kept apart from curated records (ADR-007). The list is `GET discovery/candidates`: 50
    at a time by source name, with the count per status, under an offset cursor the SPA counts with."""

    role_policy = policy(VIEWER, EDITOR)
    pagination_class = None
    serializer_class = ObservationSerializer
    filterset_class = CandidateFilterSet
    ordering_fields = ()
    if_match_actions = ("drift_apply", "missing_archive", "dismiss")

    def base_queryset(self):
        # Licence seat counts never become records: they feed the licence pools' seat review (money/licences.py).
        return (
            Observation.objects.exclude(kind__in=NOT_CANDIDATES)
            .select_related(
                "connector",
                "decision",
                "decision__resource",
                "decision__resource__application",
                "decision__resource__component",
                "decision__resource__ai_agent",
                "decision__decided_by",
                "decision__gone_acknowledged_by",
            )
            .prefetch_related("credentials")
        )

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        org = self.organization
        open_ = [o for o in rows if engine.status_of(o) in ("new", "changed")]
        return {
            "owners": engine.suggested_owners(org, rows),
            "matches": engine.suggested_matches(org, open_),
            "differences": differences_of(org, rows),
            # The assistant's suggestions, only with its capability (discovery/assistant.py).
            "assistant": assistant.latest_of(org, rows) if has_capability(org, ASSISTANT) else {},
        }

    def revision_of(self, obj) -> str:
        decision = getattr(obj, "decision", None)
        return revision(obj, *([decision] if decision is not None else []))

    def list(self, request, *args, **kwargs):
        params = request.query_params
        filterset = CandidateFilterSet(params, queryset=self.get_queryset(), request=request)
        errors = filter_errors(filterset, params, allowed={"cursor", "after"})
        if "cursor" in params and "after" in params:
            errors["after"] = ["Send cursor or after, not both."]
        if errors:
            raise FieldErrors(errors)
        wanted = filterset.form.cleaned_data.get("status") or "candidates"
        offset = 0 if "after" in params else _offset(params.get("cursor") or "")
        qs = filterset.qs
        # Every status count in one query (the list has a 500 ms budget).
        totals = engine.status_counts(qs)
        page = qs.filter(engine.status_q(wanted))
        if "after" in params:
            page = page.filter(_after(params["after"]))
        rows = list(page.order_by("display_name", "id")[offset : offset + CANDIDATE_PAGE + 1])
        more = len(rows) > CANDIDATE_PAGE
        rows = rows[:CANDIDATE_PAGE]
        return Response(
            {
                "status": wanted,
                "counts": totals,
                "results": self.render(rows, many=True),
                # An offset (the SPA shows "from number N" with it); null after an `after` page.
                "next": str(offset + CANDIDATE_PAGE) if more and "after" not in params else None,
                "next_after": after_token(rows[-1]) if more else None,
            }
        )

    def _decide(self, change, status_code=200):
        data = self.payload()
        with transaction.atomic():
            obs = change(self.get_object(), data)
            return self.detail_response(self.fresh(obs), status_code=status_code)

    @action(detail=True, methods=["post"])
    def link(self, request, pk=None):
        return self._decide(lambda obs, data: engine.link(self.organization, request.user, obs, data))

    @action(detail=True, methods=["post"], url_path="create")
    def create_record(self, request, pk=None):
        return self._decide(
            lambda obs, data: engine.create_record(self.organization, request.user, obs, data)[0],
            status_code=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def dismiss(self, request, pk=None):
        # If-Match compares the candidate's own revision under its row lock (check_if_match), then the ordinary
        # decision runs on the row as stored now.
        return self._decide(
            lambda obs, data: engine.dismiss(self.organization, request.user, self.check_if_match(obs), data)
        )

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        return self._decide(lambda obs, data: engine.reopen(self.organization, request.user, obs, data))

    # -- suggestions (reconciliation)

    @action(detail=True, methods=["get"])
    def suggestions(self, request, pk=None):
        raw = request.query_params.get("limit") or "5"
        unknown = set(request.query_params) - {"limit"}
        errors = {k: ["This list doesn't take this parameter."] for k in unknown}
        limit = whole_number(raw, 1, engine.SUGGESTIONS_MAX)
        if limit is None:
            errors["limit"] = [f"Use a whole number from 1 to {engine.SUGGESTIONS_MAX}."]
        if errors or limit is None:
            raise FieldErrors(errors)
        return Response({"results": engine.suggestions(self.organization, self.get_object(), limit)})

    # -- the assistant's suggestion (capability assistant)

    @action(detail=True, methods=["post"], url_path="assistant/accept")
    def assistant_accept(self, request, pk=None):
        return self._decide(lambda obs, data: assistant.accept(self.organization, request.user, obs, data))

    @action(detail=True, methods=["post"], url_path="assistant/reject")
    def assistant_reject(self, request, pk=None):
        return self._decide(lambda obs, data: assistant.reject(self.organization, request.user, obs, data))

    # -- source differs (reconciliation)

    def _record(self, obs):
        d = getattr(obs, "decision", None)
        if d is None or d.decision == DecisionKind.DISMISSED or d.resource_id is None:
            raise Conflict("This observation isn't linked to a record.")
        resource = (
            Resource.objects.filter(organization=self.organization, id=d.resource_id)
            .select_related("application", "component", "ai_agent")
            .get()
        )
        return self._capable(resource)

    def _capable(self, resource):
        """An AI agent's drift is the agent's data, which needs ai_agents like every other AI agent route."""
        if resource.kind == Kind.AI_AGENT:
            require_capability(self.organization, AI_AGENTS)
        return resource

    def _drift_response(self, obs):
        obs = self.fresh(obs)
        resource = self._record(obs)
        rows = SourceDifference.objects.filter(observation=obs).select_related("ignored_by")
        rev = record_revision(resource)
        response = Response(
            {
                "observation": str(obs.id),
                "name": obs.display_name,
                "connector": {"id": str(obs.connector_id), "name": obs.connector.name},
                "record": {"id": str(resource.id), "kind": resource.kind, "name": resource.name},
                "record_etag": rev,
                "differences": difference_rows(drift.entries(obs, resource, rows)),
            }
        )
        response["ETag"] = etag(rev)
        return response

    def _locked_record(self, obs):
        """Inside the write's transaction: lock the observation first, then the record its decision (read after that
        lock) links to, then compare If-Match with the record's revision. The same order as the services and reopen
        (observation, then record), so two writes can't wait on each other."""
        list(
            Observation.objects.select_for_update().filter(organization=self.organization, id=obs.id).values_list("id")
        )
        d = ReconciliationDecision.objects.filter(observation_id=obs.id).first()
        if d is None or d.decision == DecisionKind.DISMISSED or d.resource_id is None:
            raise Conflict("This observation isn't linked to a record.")
        resource = (
            Resource.objects.select_for_update(of=("self",))
            .select_related("application", "component", "ai_agent")
            .get(organization=self.organization, id=d.resource_id)
        )
        self._capable(resource)
        require_match(self.request, record_revision(resource))
        return resource

    @action(detail=True, methods=["get"])
    def drift(self, request, pk=None):
        refuse_unknown_params(request)
        return self._drift_response(self.get_object())

    @action(detail=True, methods=["post"], url_path="drift/apply")
    def drift_apply(self, request, pk=None):
        data = self.payload()
        with transaction.atomic():
            obs = self.get_object()
            resource = self._locked_record(obs)
            drift.apply(self.organization, request.user, obs, data, resource=resource)
            return self._drift_response(obs)

    @action(detail=True, methods=["post"], url_path="drift/ignore")
    def drift_ignore(self, request, pk=None):
        data = self.payload()
        with transaction.atomic():
            obs = self.get_object()
            self._record(obs)  # linked, and readable by this organization, before anything is written
            drift.ignore(self.organization, request.user, obs, data)
            return self._drift_response(obs)

    # -- no longer seen in source (reconciliation)

    @action(detail=True, methods=["post"], url_path="missing/acknowledge")
    def missing_acknowledge(self, request, pk=None):
        return self._decide(lambda obs, data: missing.acknowledge(self.organization, request.user, obs, data))

    @action(detail=True, methods=["post"], url_path="missing/archive")
    def missing_archive(self, request, pk=None):
        def archive(obs, data):
            resource = self._locked_record(obs)
            return missing.archive(self.organization, request.user, obs, data, resource=resource)

        return self._decide(archive)


def differences_of(org, rows: list[Any]) -> dict[Any, list[dict[str, Any]]]:
    """{observation id: drift entries} for the linked and created observations among `rows` (one query). An AI agent's
    differences (its curated values) are left out without ai_agents, as its drift route refuses them."""
    agents = has_capability(org, AI_AGENTS)
    linked = {
        o.id: o
        for o in rows
        if getattr(o, "decision", None) is not None
        and o.decision.decision != DecisionKind.DISMISSED
        and o.decision.resource_id is not None
        and (agents or o.decision.resource.kind != Kind.AI_AGENT)
    }
    if not linked:
        return {}
    found: dict[Any, list[SourceDifference]] = {}
    for d in SourceDifference.objects.filter(organization=org, observation_id__in=list(linked)).select_related(
        "ignored_by"
    ):
        found.setdefault(d.observation_id, []).append(d)
    return {oid: drift.entries(linked[oid], linked[oid].decision.resource, found.get(oid, [])) for oid in linked}


# ---- A record's discovery sources ---------------------------------------------------------------------------------------


@extend_schema_view(list=extend_schema(summary="Source observations linked to a record, with the fields that differ"))
class RecordSourceViewSet(TenantViewSet):
    """The linked and created decisions on one application, component or AI agent, oldest first. A decision is read
    here or inside its observation (discovery/observations/<id>); it has no route of its own. Each route passes the
    record kind its URL names (`kind`), so another kind's ID is a 404, as on the record's other subroutes."""

    role_policy = policy(VIEWER)
    pagination_class = None
    serializer_class = DiscoverySourceSerializer
    ordering_fields = ("created_at",)
    default_ordering = ("created_at",)
    kind: ClassVar[str | None] = None  # set per route through as_view(kind=...)

    def record(self):
        if not hasattr(self, "_record"):
            kinds = [self.kind] if self.kind else RECORD_KINDS
            self._record = get_object_or_404(
                Resource.objects.filter(organization=self.organization, kind__in=kinds).select_related(
                    "application", "component", "ai_agent"
                ),
                id=self.kwargs["pk"],
            )
            if self._record.kind == Kind.AI_AGENT:
                require_capability(self.organization, AI_AGENTS)
        return self._record

    def base_queryset(self):
        return ReconciliationDecision.objects.select_related("observation__connector", "decided_by").exclude(
            decision=DecisionKind.DISMISSED
        )

    def get_queryset(self):
        qs = super().get_queryset()
        return qs if getattr(self, "swagger_fake_view", False) else qs.filter(resource=self.record())

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        r = self.record()
        observations = {d.observation_id: d.observation for d in rows}
        found: dict[Any, list[SourceDifference]] = {}
        for d in SourceDifference.objects.filter(
            organization=self.organization, observation_id__in=list(observations)
        ).select_related("ignored_by"):
            found.setdefault(d.observation_id, []).append(d)
        return {
            "differences": {oid: drift.entries(o, r, found.get(oid, [])) for oid, o in observations.items()},
        }


# ---- discovered credentials ---------------------------------------------------------------------------------

CREDENTIAL_STATES = ("live", "removed", "all")
CREDENTIALS_SHOWN = 500


def _credential_state(value: str) -> str:
    if value not in CREDENTIAL_STATES:
        raise forms.ValidationError("Use live, removed or all.")
    return value


def _days(value: str) -> int:
    days = whole_number(value.strip(), 0, 3650)
    if days is None:
        raise forms.ValidationError("Enter a whole number of days from 0 to 3650.")
    return days


class CredentialFilterSet(TenantFilterSet):
    state = ParamFilter(
        parse=_credential_state,
        method="filter_state",
        choices=[(s, s) for s in CREDENTIAL_STATES],
        help_text="live (the default: still seen, on an app still seen and not dismissed), removed, or all.",
    )
    connector = ParamFilter(
        parse=worded(uuid_value, "Choose a connector from the list."),
        method="filter_connector",
        help_text="Connector ID.",
    )
    expires_within = ParamFilter(
        parse=_days, method="filter_expires_within", help_text="End date within this many days (expired included)."
    )
    q = ParamFilter(parse=stripped, method="filter_q", help_text="Search the credential's or the application's name.")

    class Meta:
        model = DiscoveredCredential
        fields: list[str] = []

    def filter_state(self, qs, name, value):
        if value == "removed":
            return qs.filter(removed_at__isnull=False)
        if value == "all":
            return qs
        return qs.filter(pk__in=discovered_live(self.membership.organization).values("pk"))

    def filter_connector(self, qs, name, value):
        in_org_or_404(Connector, self.membership.organization, value)
        return qs.filter(observation__connector_id=value)

    def filter_expires_within(self, qs, name, value):
        from core.dates import org_today

        return qs.filter(expires_on__lte=org_today(self.membership.organization) + timedelta(days=value))

    def filter_q(self, qs, name, value):
        return qs.filter(Q(display_name__icontains=value) | Q(observation__display_name__icontains=value))


@extend_schema_view(
    list=extend_schema(
        summary="Client secrets and certificates discovery found, soonest end date first (at most 500)",
        description="Live by default: still seen, on an observed app that is still seen, not dismissed, and whose "
        "connector is on; the same set the credential expiry reminders and the dashboard read. Never key material.",
        filters=True,
    ),
)
class DiscoveredCredentialViewSet(TenantViewSet):
    """Credentials discovery saw (discovery/credentials.py). Runs write them; nothing here does."""

    role_policy = policy(VIEWER)
    pagination_class = None
    serializer_class = DiscoveredCredentialSerializer
    filterset_class = CredentialFilterSet
    ordering_fields = ("expires_on",)
    default_ordering = ("expires_on", "display_name", "id")
    list_limit = CREDENTIALS_SHOWN

    def base_queryset(self):
        return DiscoveredCredential.objects.select_related(
            "observation__connector", "observation__decision", "observation__decision__resource"
        )

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, "swagger_fake_view", False) or "state" in self.request.query_params:
            return qs
        return qs.filter(pk__in=discovered_live(self.organization).values("pk"))

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        from core.dates import org_today

        return {"today": org_today(self.organization)}
