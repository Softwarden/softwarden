"""The audit log: the organization's log for Admins and each record's History tab. Routes are in
catalog/urls.py with the URLs, filters and response shapes of the function views they replaced.

Audit events are append-only (services write them in the same transaction as the change). The routes here only
read: list and retrieve on the log, a list on each record. No route updates or deletes an event, and
tests/test_drf_resources.py checks that none exists.

Pages are newest first, 50 at a time, as `{results, next}`; `next` is the last event's ID, passed back as ?cursor=.
Names are resolved when read (catalog/audit_log.py), so a page shows each reference as it is called now.

A row carries the raw references too (target and related target type and id, request id); one event read on its own
(the log's retrieve, or a record's history retrieve) adds the prechange / postchange snapshots and a field-level
diff (catalog/audit_log.event_diff)."""

import uuid
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from django import forms
from django.db import transaction
from django.db.models import Q
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import (
    OpenApiParameter,
    extend_schema,
    extend_schema_field,
    extend_schema_view,
)
from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail
from rest_framework.pagination import BasePagination
from rest_framework.response import Response

from core.capabilities import AI_AGENTS, AUDIT_EXPORT, AUDIT_LOG, CONTRACTS, has_capability
from core.dates import filter_day, org_today
from core.drf import (
    ADMIN,
    CSV_FILE,
    PROBLEMS,
    VIEWER,
    ParamFilter,
    ReadOnlySerializer,
    TenantFilterSet,
    TenantViewSet,
    choice,
    file_response,
    policy,
    uuid_value,
)
from core.models import AuditEvent
from core.problems import FieldErrors
from money.models import Contract, LicensePool, RenewalObligation

from . import audit_export, compliance, services
from .audit_log import (
    CHILD_KINDS,
    LABELS,
    TARGET_TYPES,
    Names,
    child_of,
    contract_events,
    event_diff,
    integration_events,
    own_events,
    record_events,
    renewal_events,
    summary,
    target_of,
)
from .models import RECORD_KIND_CHOICES, Kind
from .querysets import integrations_for, resources_for

PAGE_SIZE = 50
EXPIRED = "This page link has expired. Start from the first page."
ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
TARGET_KINDS = [
    "application",
    "component",
    "ai_agent",
    "integration",
    "person",
    "relationship",
    "assessment",
    "renewal",
    "vendor",
    "contract",
    "license_pool",
]


# ---- serializers --------------------------------------------------------------------------------------------------


class AuditRecordRefSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(RECORD_KIND_CHOICES)
    id = serializers.UUIDField()


class AuditTargetSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(TARGET_KINDS)
    id = serializers.CharField()
    name = serializers.CharField(help_text="The current name: a link reads 'Portal depends on Sync'.")
    record = AuditRecordRefSerializer(
        required=False,
        allow_null=True,
        help_text="For a relationship: the source record (kind and id), where the link is listed.",
    )


class AuditActorSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class AuditEventSerializer(ReadOnlySerializer):
    """Context: `names` (catalog/audit_log.Names for the page) and `tz`, the organization's timezone."""

    id = serializers.UUIDField(read_only=True)
    at = serializers.SerializerMethodField(help_text="ISO 8601 to the minute, in the organization's timezone.")
    actor = serializers.SerializerMethodField(help_text="Null for the system and the CLI.")
    action = serializers.CharField(
        read_only=True, help_text="For example resource.updated, relationship.created, assessment.stale."
    )
    label = serializers.SerializerMethodField()  # type: ignore[assignment]  # the API field, not Field.label
    target = serializers.SerializerMethodField()
    summary = serializers.SerializerMethodField(help_text="One line with every reference resolved to its current name.")

    def get_at(self, e) -> str:
        return e.created_at.astimezone(self.context["tz"]).isoformat(timespec="minutes")

    @extend_schema_field(AuditActorSerializer(allow_null=True))
    def get_actor(self, e) -> dict[str, Any] | None:
        return e.actor and {"id": e.actor.id, "name": e.actor.get_full_name() or e.actor.email}

    impersonated_by = serializers.SerializerMethodField(
        help_text="The service operator's platform staff account that made this change while acting as `actor` "
        "(impersonation); null otherwise."
    )

    @extend_schema_field(AuditActorSerializer(allow_null=True))
    def get_impersonated_by(self, e) -> dict[str, Any] | None:
        by = e.impersonated_by
        return by and {"id": by.id, "name": by.get_full_name() or by.email}

    def get_label(self, e) -> str:
        return LABELS.get(e.action, e.action)

    @extend_schema_field(AuditTargetSerializer(allow_null=True))
    def get_target(self, e) -> dict[str, Any] | None:
        return target_of(e, self.context["names"])

    def get_summary(self, e) -> str:
        return summary(e, self.context["names"])

    target_type = serializers.CharField(read_only=True, help_text="The target's type, such as Resource; empty if none.")
    target_id = serializers.UUIDField(read_only=True, allow_null=True)
    related_target_type = serializers.CharField(
        read_only=True,
        help_text="For a child row (an instance, a review, a link confirmation, a renewal cycle): its parent's type.",
    )
    related_target_id = serializers.UUIDField(read_only=True, allow_null=True)
    request_id = serializers.UUIDField(
        read_only=True,
        allow_null=True,
        help_text="Shared by the events of one request, job or command (the X-Request-ID response header).",
    )


class AuditDiffRowSerializer(serializers.Serializer):
    field = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # the API field, not Field.label
    change = serializers.ChoiceField(["added", "removed", "changed"])
    before = serializers.JSONField(allow_null=True, help_text="A value, an {id, display} reference, or a list of them.")
    after = serializers.JSONField(allow_null=True)
    added = serializers.ListField(
        child=serializers.JSONField(), required=False, help_text="For a set (tags, applications): what joined."
    )
    removed = serializers.ListField(child=serializers.JSONField(), required=False, help_text="What left the set.")


class AuditEventDetailSerializer(AuditEventSerializer):
    """One event with its snapshots and diff. Context also needs `org` (names of people in the diff)."""

    prechange = serializers.JSONField(
        read_only=True, allow_null=True, help_text="The target before the write; null for a creation or no snapshot."
    )
    postchange = serializers.JSONField(
        read_only=True, allow_null=True, help_text="The target after the write; null for a deletion or no snapshot."
    )
    diff_source = serializers.SerializerMethodField(
        help_text="snapshots; changes for an event written before snapshots (its before/after pairs); none."
    )
    diff = serializers.SerializerMethodField()

    def _diff(self, e):
        if not hasattr(e, "computed_diff"):  # both fields read it; computed once per event
            e.computed_diff = event_diff(e, self.context["names"], self.context["org"])
        return e.computed_diff

    @extend_schema_field(serializers.ChoiceField(["snapshots", "changes", "none"]))
    def get_diff_source(self, e) -> str:
        return self._diff(e)[0]

    @extend_schema_field(AuditDiffRowSerializer(many=True))
    def get_diff(self, e) -> list[dict[str, Any]]:
        return self._diff(e)[1]


class AuditPageSerializer(serializers.Serializer):
    results = AuditEventSerializer(many=True, help_text="Newest first.")
    next = serializers.CharField(allow_null=True, help_text="Pass as cursor for the next page.")


class AuditChildSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(list(CHILD_KINDS))
    kind_label = serializers.CharField(help_text="The kind in words, such as Attachment or Integration.")
    id = serializers.UUIDField(
        allow_null=True, help_text="The child row's ID; null for an older event that targeted the record itself."
    )
    name = serializers.CharField(help_text="The child as named now (or when removed): a title, the other end's name.")
    sentence = serializers.CharField(
        help_text="The event in words, naming the child: Added attachment 'Contract'; Linked to Payroll API: depends on."
    )


class ChildMixin(serializers.Serializer):
    """A record's History row: `child` says which child of the record the event is about. Context: `parent_id`."""

    child = serializers.SerializerMethodField(
        help_text="Null for the record's own event; else the child (an attachment, instance, link, link confirmation, "
        "review, data-quality exception, integration it is an end of, or renewal cycle) and a sentence about it."
    )

    @extend_schema_field(AuditChildSerializer(allow_null=True))
    def get_child(self, e) -> dict[str, Any] | None:
        return child_of(e, self.context["parent_id"], self.context["names"])


class RecordAuditEventSerializer(ChildMixin, AuditEventSerializer):
    pass


class RecordAuditEventDetailSerializer(ChildMixin, AuditEventDetailSerializer):
    pass


class RecordAuditPageSerializer(serializers.Serializer):
    results = RecordAuditEventSerializer(many=True, help_text="Newest first.")
    next = serializers.CharField(allow_null=True, help_text="Pass as cursor for the next page.")


class AuditActionSerializer(serializers.Serializer):
    value = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # the API field, not Field.label


class AuditLogPageSerializer(AuditPageSerializer):
    actions = AuditActionSerializer(many=True, help_text="Every action the log can filter by.")
    target_types = AuditActionSerializer(many=True, help_text="Every target type the log can filter by.")


# ---- paging -------------------------------------------------------------------------------------------------------


class AuditPagination(BasePagination):
    """Newest first, PAGE_SIZE at a time. The cursor is the last event's ID; events at the same time follow by ID. An
    ID that isn't one of this organization's events is the expired-link problem."""

    query_params: ClassVar[frozenset[str]] = frozenset({"cursor"})

    def __init__(self):
        self.next_cursor: str | None = None

    def paginate_queryset(self, queryset, request, view=None):
        events = queryset.order_by("-created_at", "-id")
        cursor = request.query_params.get("cursor")
        if cursor:
            last = None
            try:
                last = AuditEvent.objects.filter(organization=view.organization, id=uuid.UUID(cursor)).first()
            except ValueError:
                pass
            if last is None:
                raise FieldErrors({"cursor": [EXPIRED]})
            events = events.filter(Q(created_at__lt=last.created_at) | Q(created_at=last.created_at, id__lt=last.id))
        rows = list(events[: PAGE_SIZE + 1])
        more, rows = len(rows) > PAGE_SIZE, rows[:PAGE_SIZE]
        self.next_cursor = str(rows[-1].id) if more else None
        return rows

    def get_paginated_response(self, data):
        return Response({"results": data, "next": self.next_cursor})

    def get_paginated_response_schema(self, schema):
        return {
            "type": "object",
            "required": ["results", "next"],
            "properties": {
                "results": schema,
                "next": {"type": "string", "nullable": True, "description": "Pass as cursor for the next page."},
            },
        }

    def get_schema_operation_parameters(self, view):
        return [
            {
                "name": "cursor",
                "in": "query",
                "required": False,
                "description": "The next value from a page.",
                "schema": {"type": "string"},
            }
        ]


CURSOR = OpenApiParameter("cursor", str, description="The next value from a page.")


# ---- the organization's log (Admin) -------------------------------------------------------------------------------


def _actor(value: str) -> int | None:
    if value == "":
        return None
    uid = services.user_id(value)  # "²" passes isdigit() but not int(); a huge number overflows the key
    if uid is None:
        raise forms.ValidationError("Choose a user.")
    return uid


def _day(value: str) -> date | None:
    try:
        return filter_day(value)  # clamped, so the inclusive end's extra day always fits
    except ValueError:
        raise forms.ValidationError("Enter a date as YYYY-MM-DD.") from None


class AuditEventFilterSet(TenantFilterSet):
    actor = ParamFilter(parse=_actor, field_name="actor_id", help_text="User ID.")
    action = ParamFilter(
        parse=lambda v: v or None,
        method="filter_action",
        help_text="An action, or a group such as relationship, link or assessment.",
    )
    to = ParamFilter(parse=_day, method="filter_to", help_text="YYYY-MM-DD, organization timezone, inclusive.")
    target_type = ParamFilter(
        parse=choice(TARGET_TYPES),
        choices=list(TARGET_TYPES.items()),
        field_name="target_type",
        help_text="The target's type, such as Resource or RenewalCycle (target_types in the response).",
    )
    target = ParamFilter(
        parse=uuid_value,
        field_name="target_id",
        help_text="The row the event wrote; use related_target for a record's child events.",
    )
    related_target = ParamFilter(
        parse=uuid_value,
        field_name="related_target_id",
        help_text="The parent record's ID: an application's instance and review events, a renewal's cycle events.",
    )
    request_id = ParamFilter(parse=uuid_value, field_name="request_id", help_text="Events of one request.")

    class Meta:
        model = AuditEvent
        fields: list[str] = []

    def filter_action(self, qs, name, value):
        return qs.filter(action=value) if value in LABELS else qs.filter(action__startswith=value + ".")

    def _start_of(self, day: date) -> datetime:
        return datetime.combine(day, time.min, tzinfo=ZoneInfo(self.membership.organization.timezone))

    def filter_from(self, qs, name, value):
        return qs.filter(created_at__gte=self._start_of(value))

    def filter_to(self, qs, name, value):
        return qs.filter(created_at__lt=self._start_of(value + timedelta(days=1)))


# `from` is a Python keyword, so it is added after the class body; it documents and validates like the others.
AuditEventFilterSet.base_filters["from"] = ParamFilter(
    parse=_day, method="filter_from", field_name="created_at", help_text="YYYY-MM-DD, organization timezone, inclusive."
)


class AuditViewSet(TenantViewSet):
    """Shared by the log and the History tabs: events render with the names and timezone of their page."""

    pagination_class = AuditPagination
    serializer_class = AuditEventSerializer
    ordering_fields = ()

    detail_serializer_class = AuditEventDetailSerializer

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        org = self.organization
        if not compliance.enabled(org):  # values recorded while the profile was on stay hidden while it is off
            for event in rows:
                compliance.hide(event)
        return {"names": Names(org, rows), "tz": ZoneInfo(org.timezone), "org": org}


@extend_schema_view(
    list=extend_schema(
        summary="Organization audit log (Admin)",
        parameters=[CURSOR],
        responses={200: AuditLogPageSerializer, **ERRORS},
        filters=True,
    ),
    retrieve=extend_schema(
        summary="One audit event (Admin)",
        description="With the target's prechange and postchange snapshots and a field-level diff.",
        responses={200: AuditEventDetailSerializer, **ERRORS},
    ),
)
class AuditEventViewSet(AuditViewSet):
    """Every change, sign-in and account event in the organization, Admin only. Filters: actor (user ID), action (or
    a group), from and to (dates in the organization timezone, inclusive), target_type, target, related_target and
    request_id."""

    role_policy = policy(ADMIN)
    filterset_class = AuditEventFilterSet

    def base_queryset(self):
        return AuditEvent.objects.select_related("actor", "impersonated_by")

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        response.data["actions"] = [{"value": k, "label": v} for k, v in LABELS.items()]
        response.data["target_types"] = [{"value": k, "label": v} for k, v in TARGET_TYPES.items()]
        return response


# ---- the organization's log as a download (Admin; catalog/audit_export.py) ---------------------------------------

TOO_MANY = (
    "{count:,} events match; one export holds at most {limit:,}. Narrow the date range (from, to) or the filters."
)
TOO_MANY_TYPE = "/problems/too-many-events"


class TooManyEvents(FieldErrors):
    """400 `/problems/too-many-events` with `count` (events matching) and `limit` (one export's most). The message is
    the detail and, as before, `errors.non_field`."""

    def __init__(self, count: int, limit: int):
        message = TOO_MANY.format(count=count, limit=limit)
        super().__init__({"non_field": [message]})
        self.detail = ErrorDetail(message, code="too_many_events")
        self.extra = {"type": TOO_MANY_TYPE, "title": "Too many events to export", "count": count, "limit": limit}


EXPORT_DOC = (
    "The log's filters (actor, action, from, to, target_type, target, related_target, request_id); cursor and "
    "ordering are refused (400). Streamed in batches; at most "
    f"{audit_export.MAX_ROWS:,} events (more is a 400 of type {TOO_MANY_TYPE} with `count` and `limit`), and only events inserted before the export "
    "started (a transaction committing while it streams can still add one; overlap date ranges and dedupe on id). "
    "Audited as `export` with kind audit_event, the format, the filters and the row count when it started (written "
    "before streaming, so it is the count requested)."
)


class AuditExportViewSet(TenantViewSet):
    """Base of the two downloads: validates the filters, refuses an export over MAX_ROWS, audits it, then streams."""

    role_policy = policy(ADMIN)
    filterset_class = AuditEventFilterSet
    pagination_class = None
    ordering_fields = ()
    serializer_class = AuditEventSerializer  # not rendered; the response is a file
    fmt: ClassVar[str]
    content_type: ClassVar[str]

    def base_queryset(self):
        return AuditEvent.objects.select_related("actor", "impersonated_by")

    def stream(self, events) -> Iterator[bytes]:
        raise NotImplementedError

    def list(self, request, *args, **kwargs):
        org = self.organization
        started = timezone.now()
        # Only events already written: the export's own event, and anything written while it streams, are left out,
        # so the audited count is what the file holds.
        events = self.filter_queryset(self.get_queryset()).filter(created_at__lt=started)
        count = events.count()
        if count > audit_export.MAX_ROWS:
            raise TooManyEvents(count, audit_export.MAX_ROWS)
        filters = {k: request.query_params[k] for k in request.query_params}
        with transaction.atomic():
            services.audit(
                org,
                request.user,
                "export",
                None,
                {"kind": "audit_event", "format": self.fmt, "rows": count, "filters": filters},
            )
        response = StreamingHttpResponse(self.stream(events), content_type=self.content_type)
        name = f"softwarden-audit-log-{org_today(org).isoformat()}.{self.fmt}"
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        response["Cache-Control"] = "no-store"
        return response


@extend_schema_view(
    list=extend_schema(
        summary="Export the organization audit log as CSV (Admin)",
        description="UTF-8 with a BOM, newest first, formula-safe cells. Columns: "
        + ", ".join(audit_export.CSV_COLUMNS)
        + ". time_utc is UTC (Z), time_local the organization's timezone with its offset; target_kind and "
        "target_name are what the event is about as the log names it now. " + EXPORT_DOC,
        responses={**CSV_FILE, **ERRORS},
        filters=True,
    )
)
class AuditCsvExportViewSet(AuditExportViewSet):
    required_capability = AUDIT_LOG
    fmt = "csv"
    content_type = "text/csv; charset=utf-8"

    def stream(self, events) -> Iterator[bytes]:
        return audit_export.csv_rows(self.organization, events)


@extend_schema_view(
    list=extend_schema(
        summary="Export the organization audit log as JSON lines for a SIEM (Admin)",
        description="Capability audit.export. One JSON object per line, oldest first, UTF-8. Stable fields "
        f"(schema_version {audit_export.SCHEMA_VERSION}): schema_version, id, organization {{id, slug}}, time (UTC, "
        "microseconds), action, label, actor {id, name} or null, target {type, id, kind, name}, related_target "
        "{type, id} or null, request_id, summary, changes, prechange, postchange. Snapshots hold declared fields only "
        "and never a secret; people in them are {id, display: null} references. " + EXPORT_DOC,
        responses={**file_response("application/x-ndjson", "One JSON event per line (attachment)."), **ERRORS},
        filters=True,
    )
)
class AuditJsonlExportViewSet(AuditExportViewSet):
    required_capability = AUDIT_EXPORT
    fmt = "jsonl"
    content_type = "application/x-ndjson; charset=utf-8"

    def stream(self, events) -> Iterator[bytes]:
        return audit_export.jsonl_rows(self.organization, events)


# ---- a record's History tab (any role) ----------------------------------------------------------------------------


def _flag(value: str) -> bool:
    if value not in ("true", "false"):
        raise forms.ValidationError("Use one of: true, false.")
    return value == "true"


class RecordAuditFilterSet(TenantFilterSet):
    children = ParamFilter(
        parse=_flag,
        method="filter_children",
        help_text="true (the default): the record's events and its children's; false: the record's own events only.",
    )

    class Meta:
        model = AuditEvent
        fields: list[str] = []

    def filter_children(self, qs, name, value):
        return qs  # applied by RecordAuditViewSet.filter_queryset, which knows the record


class RecordAuditViewSet(AuditViewSet):
    """The events of one record and of its children, newest first (any role reads them), and one of them with its
    snapshots and diff. Only this history is reachable here: another record's event ID is a 404. `children=false`
    keeps the record's own events."""

    role_policy = policy(VIEWER)
    lookup_url_kwarg = "event_pk"
    filterset_class = RecordAuditFilterSet
    serializer_class = RecordAuditEventSerializer
    detail_serializer_class = RecordAuditEventDetailSerializer

    def parent(self):
        raise NotImplementedError

    def events(self, parent) -> Q:
        raise NotImplementedError

    @property
    def parent_obj(self):
        if not hasattr(self, "_parent"):
            self._parent = self.parent()
        return self._parent

    def base_queryset(self):
        qs = AuditEvent.objects.select_related("actor", "impersonated_by")
        if getattr(self, "swagger_fake_view", False):
            return qs
        return qs.filter(self.events(self.parent_obj))

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)  # validates `children` with the other parameters
        if self.action == "list" and self.request.query_params.get("children") == "false":
            queryset = queryset.filter(own_events(self.parent_obj))
        return queryset

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        return {**super().page_context(rows), "parent_id": self.parent_obj.id}


def _history(summary_text, description=""):
    return extend_schema_view(
        list=extend_schema(
            summary=summary_text,
            description=description,
            responses={200: RecordAuditPageSerializer, **ERRORS},
            filters=True,
        ),
        retrieve=extend_schema(
            summary=summary_text.replace("Audit activity", "One audit event"),
            description="Any role. One event of this history, with its snapshots and a field-level diff.",
            responses={200: RecordAuditEventDetailSerializer, **ERRORS},
        ),
    )


class ResourceAuditViewSet(RecordAuditViewSet):
    kind: ClassVar[str]

    def parent(self):
        return get_object_or_404(resources_for(self.organization, self.kind), id=self.kwargs["pk"])

    def events(self, parent) -> Q:
        return record_events(parent, contracts=has_capability(self.organization, CONTRACTS))


RECORD_HISTORY = (
    "Any role. Events on the record and its children: instances, reviews, attachments, data-quality exceptions, the "
    "links it is an end of (relationships and link confirmations) and the integrations it is an end of. Each child "
    "event's `child` names the child in a sentence; children=false keeps the record's own events."
)


@_history("Audit activity for one application", RECORD_HISTORY)
class ApplicationAuditViewSet(ResourceAuditViewSet):
    kind = Kind.APPLICATION


@_history("Audit activity for one component", RECORD_HISTORY)
class ComponentAuditViewSet(ResourceAuditViewSet):
    kind = Kind.COMPONENT


@_history("Audit activity for one AI agent", RECORD_HISTORY)
class AIAgentAuditViewSet(ResourceAuditViewSet):
    kind = Kind.AI_AGENT
    required_capability = AI_AGENTS


@_history(
    "Audit activity for one integration",
    "Any role. The integration's own events, its children's (confirmations, attachments, data-quality exceptions) and "
    "the relationship events that name it; children=false keeps its own events.",
)
class IntegrationAuditViewSet(RecordAuditViewSet):
    def parent(self):
        return get_object_or_404(integrations_for(self.organization), id=self.kwargs["pk"])

    def events(self, parent) -> Q:
        return integration_events(parent)


@_history("Audit activity of a renewal and its cycles", "Any role. children=false leaves out the cycles' events.")
class RenewalAuditViewSet(RecordAuditViewSet):
    def parent(self):
        return get_object_or_404(RenewalObligation, organization=self.organization, id=self.kwargs["pk"])

    def events(self, parent) -> Q:
        return renewal_events(parent)


@_history(
    "Audit activity of a contract",
    "Any role. The contract's own events and its children's: its lines and attachments (and, in a linked renewal's "
    "History, the cycle changes it made). children=false keeps its own events.",
)
class ContractAuditViewSet(RecordAuditViewSet):
    required_capability = CONTRACTS

    def parent(self):
        return get_object_or_404(Contract, organization=self.organization, id=self.kwargs["pk"])

    def events(self, parent) -> Q:
        return contract_events(parent)


@_history("Audit activity of a license pool", "Any role.")
class LicensePoolAuditViewSet(RecordAuditViewSet):
    required_capability = CONTRACTS

    def parent(self):
        return get_object_or_404(LicensePool, organization=self.organization, id=self.kwargs["pk"])

    def events(self, parent) -> Q:
        return Q(target_type="LicensePool", target_id=parent.id)
