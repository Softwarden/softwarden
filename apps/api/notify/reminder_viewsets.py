"""Reminders: the list and detail, and snooze, acknowledge, reopen and resolve. Routes are in notify/urls.py.

Roles: every member reads; Editors and Admins act, as they snooze Needs attention items. Every write goes through
notify/services.py; the reminders themselves are generated (notify/reminders.py), never created by hand."""

from datetime import date
from functools import cached_property

from django import forms
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F, Q
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action

from catalog.models import Vendor
from core.dates import org_today
from core.drf import (
    EDITOR,
    PROBLEMS,
    VIEWER,
    ParamFilter,
    TenantFilterSet,
    TenantViewSet,
    choice,
    choices_csv,
    policy,
    uuid_value,
)

from . import services as notify_services
from .attention import SCOPES
from .models import Reminder, ReminderSource, ReminderStatus
from .reminders import effective_status, routed_to
from .serializers import ReminderResolveInputSerializer, ReminderSerializer, ReminderSnoozeInputSerializer


def day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise forms.ValidationError("Enter a date as YYYY-MM-DD.") from None


class ReminderFilterSet(TenantFilterSet):
    status = ParamFilter(
        parse=choices_csv(ReminderStatus.values),
        field_name="effective",
        lookup_expr="in",
        choices=ReminderStatus.choices,
        help_text="Comma-separated, as of today (a snooze whose date has passed is open), e.g. "
        "open,snoozed,acknowledged for the live ones. Default: every status.",
    )
    source_type = ParamFilter(
        parse=choices_csv(ReminderSource.values),
        field_name="source_type",
        lookup_expr="in",
        choices=ReminderSource.choices,
        help_text="Comma-separated source types.",
    )
    source_id = ParamFilter(
        parse=uuid_value, field_name="source_id", help_text="One source: a renewal cycle, contract, record, ..."
    )
    due_after = ParamFilter(
        parse=day, field_name="due_date", lookup_expr="gte", help_text="Due on or after (YYYY-MM-DD)."
    )
    due_before = ParamFilter(
        parse=day, field_name="due_date", lookup_expr="lte", help_text="Due on or before (YYYY-MM-DD)."
    )
    scope = ParamFilter(
        parse=choice(SCOPES),
        method="filter_scope",
        choices=[(s, s) for s in SCOPES],
        help_text="mine (routed to me, plus the unrouted ones when I am the IT coordinator), all (default) or "
        "unassigned (no steward or owner of its own), as Needs attention cuts its queues.",
    )

    class Meta:
        model = Reminder
        fields: list[str] = []

    def filter_scope(self, queryset, name, value):
        if value == "unassigned":
            return queryset.filter(routed__isnull=True)
        if value == "mine":
            user = self.request.user
            mine = Q(routed=user.id)
            if self.membership.organization.coordinator_id == user.id:
                mine |= Q(routed__isnull=True)
            return queryset.filter(mine)
        return queryset


ACTIONS = ("snooze", "acknowledge", "reopen", "resolve")
WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409, 412)}


@extend_schema_view(
    list=extend_schema(summary="Reminders: dated obligations generated from deadlines, soonest first"),
    retrieve=extend_schema(summary="Get one reminder"),
    snooze=extend_schema(
        summary="Snooze a reminder's notifications through a date (Editor, Admin)",
        description="For everyone it reaches; the row stays in Needs attention with its status. The day after "
        "the date it is open again.",
        request=ReminderSnoozeInputSerializer,
        responses={200: ReminderSerializer, **WRITE_ERRORS},
    ),
    acknowledge=extend_schema(
        summary="Acknowledge a reminder: its advance notices stop (Editor, Admin)",
        request=None,
        responses={200: ReminderSerializer, **WRITE_ERRORS},
    ),
    reopen=extend_schema(
        summary="Reopen a snoozed, acknowledged or resolved reminder (Editor, Admin)",
        request=None,
        responses={200: ReminderSerializer, **WRITE_ERRORS},
    ),
    resolve=extend_schema(
        summary="Resolve a reminder by hand, saying how (Editor, Admin)",
        description="Doesn't change the source: a renewal still needs its decision recorded.",
        request=ReminderResolveInputSerializer,
        responses={200: ReminderSerializer, 409: OpenApiResponse(description="Already closed."), **WRITE_ERRORS},
    ),
)
class ReminderViewSet(TenantViewSet):
    role_policy = policy(VIEWER, EDITOR)
    serializer_class = ReminderSerializer
    filterset_class = ReminderFilterSet
    ordering_fields = ("due_date", "created_at", "updated_at")
    default_ordering = ("due_date",)
    if_match_actions = ACTIONS

    @cached_property
    def today(self) -> date:
        return org_today(self.organization)

    def base_queryset(self):
        return Reminder.objects.select_related("acknowledged_by", "resolved_by")

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, "swagger_fake_view", False):
            return qs
        return qs.annotate(
            effective=effective_status(self.today),
            routed=routed_to(),
            resource_kind=F("resource__kind"),
            obligation_id=F("cycle__obligation_id"),
        )

    def page_context(self, rows):
        org = self.organization
        routed = {r.routed for r in rows} - {None}
        candidates = [
            r.source_id for r in rows if r.source_type in (ReminderSource.CUSTOM_DATE, ReminderSource.VENDOR_RISK)
        ]
        return {
            "today": self.today,
            "users": get_user_model().objects.in_bulk(routed) if routed else {},
            "coordinator": org.coordinator,
            "vendors": set(Vendor.objects.filter(organization=org, id__in=candidates).values_list("id", flat=True))
            if candidates
            else set(),
        }

    def _act(self, service):
        data = self.payload()
        with transaction.atomic():
            obj = self.check_if_match(self.get_object())
            service(self.organization, self.request.user, obj, data)
            return self.detail_response(self.fresh(obj))

    @action(detail=True, methods=["post"])
    def snooze(self, request, pk=None):
        return self._act(notify_services.snooze_reminder)

    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):
        return self._act(notify_services.acknowledge_reminder)

    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        return self._act(notify_services.reopen_reminder)

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        return self._act(notify_services.resolve_reminder)
