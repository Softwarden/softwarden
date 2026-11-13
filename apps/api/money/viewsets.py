"""The renewals viewset. Routes are in money/urls.py with the same URLs and response shapes
as the function views they replaced; the derived state comes from money/renewals.py and renewal_rules.py.

Roles: Viewer reads; Editor records renewals, edits the open cycle, decides, completes, archives and restores; Admin
deletes. Every write goes through money/renewals.py."""

from typing import Any

from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from catalog import services
from core.drf import ADMIN, EDITOR, PROBLEMS, VIEWER, TenantViewSet, filter_errors, policy, revision, unknown_params
from core.problems import FieldErrors
from tickets import reads as ticket_reads

from . import renewal_rules, renewals
from .filters import RenewalFilterSet, filter_states
from .models import AutoRenew, RenewalCycle, RenewalCycleApplication, RenewalObligation
from .renewals import obligations_for, renewal_states
from .serializers import (
    DeadlineSerializer,
    RenewalCompletionInputSerializer,
    RenewalCycleInputSerializer,
    RenewalDecisionInputSerializer,
    RenewalInputSerializer,
    RenewalListSerializer,
    RenewalSerializer,
    deadline_json,
    renewal_json,
)

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409)}
ONE = {200: RenewalSerializer, **WRITE_ERRORS}


def my_snooze(org, user, state, today):
    """The signed-in user's snooze or acknowledgment of this renewal's action or gaps, while it holds."""
    from notify import digest

    held = digest.active_snoozes(org, today, user).get(user.id, {})
    for queue in ("renewal_actions", "renewal_gaps"):
        s = held.get((queue, str(state.obligation.id)))
        row = {"deadline": state.deadline.date.isoformat() if state.deadline.date else None}
        if s is not None and digest.is_quiet(s, row, org, today):
            return {"id": str(s.id), "kind": s.kind, "until": s.until and s.until.isoformat(), "queue": queue}
    return None


def sort_key(state):
    """Action deadline ascending; no deadline (or a completed cycle) last, then by name."""
    d = state.deadline.date if state.open else None
    return (d is None, d or 0, state.obligation.name.casefold())


@extend_schema_view(
    list=extend_schema(summary="List renewals, by action deadline (none last)"),
    create=extend_schema(
        summary="Record a renewal and its first cycle (Editor, Admin)",
        responses={201: RenewalSerializer, **WRITE_ERRORS},
    ),
    retrieve=extend_schema(summary="Get a renewal with its cycle history"),
    partial_update=extend_schema(
        summary="Edit a renewal's name, vendor, responsible person, follow-up or auto-renewal (Editor, Admin)"
    ),
    destroy=extend_schema(summary="Delete a renewal (Admin); refused once a decision is recorded"),
    archive=extend_schema(summary="Archive a renewal (Editor, Admin)", request=None, responses=ONE),
    restore=extend_schema(summary="Restore a renewal (Editor, Admin)", request=None, responses=ONE),
    cycles=extend_schema(
        summary="Establish the next cycle once the current one is complete (Editor, Admin)",
        description="Defaults: renewal date plus the term, the same notice period, term and covered applications.",
        request=RenewalCycleInputSerializer,
        responses={201: RenewalSerializer, **WRITE_ERRORS},
    ),
    cycle=extend_schema(
        summary="Edit the open cycle's terms and covered applications (Editor, Admin)",
        description="If-Match takes the renewal's ETag, which covers its cycles.",
        request=RenewalCycleInputSerializer,
        responses={**ONE, 412: PROBLEMS[412]},
    ),
    decision=extend_schema(
        summary="Record or clear the cycle's decision (Editor, Admin)",
        request=RenewalDecisionInputSerializer,
        responses=ONE,
    ),
    completion=extend_schema(
        summary="Record what was done, which completes the cycle (Editor, Admin)",
        request=RenewalCompletionInputSerializer,
        responses=ONE,
    ),
    preview=extend_schema(
        summary="The action deadline a cycle with these values would get",
        parameters=[
            OpenApiParameter("auto_renew", str, enum=AutoRenew.values),
            OpenApiParameter("renewal_date", str),
            OpenApiParameter("notice_value", int),
            OpenApiParameter("notice_unit", str, enum=["days", "months", "years"]),
            OpenApiParameter("explicit_deadline", str),
        ],
        responses={200: DeadlineSerializer, **ERRORS},
        filters=False,
    ),
)
class RenewalViewSet(TenantViewSet):
    """Renewal obligations and their cycles. The list is a plain array sorted by action deadline; the deadline,
    timing and gaps are derived, so filtering runs over the derived states (money/filters.py)."""

    role_policy = policy(VIEWER, EDITOR, destroy=ADMIN)
    pagination_class = None
    serializer_class = RenewalListSerializer
    detail_serializer_class = RenewalSerializer
    input_serializer_class = RenewalInputSerializer
    filterset_class = RenewalFilterSet
    ordering_fields = ()  # by action deadline, then name: derived, so not a choice
    if_match_actions = ("cycle",)

    def base_queryset(self):
        return RenewalObligation.objects.select_related("vendor", "responsible__department", "followup")

    def revision_of(self, obj) -> str:
        """The obligation, its cycles and their covered applications: a cycle edit moves the renewal's ETag."""
        cycles = list(RenewalCycle.objects.filter(obligation=obj).order_by("sequence"))
        covered = sorted(
            f"{c}:{a}"
            for c, a in RenewalCycleApplication.objects.filter(cycle__obligation=obj).values_list(
                "cycle_id", "application_id"
            )
        )
        return revision(obj, *cycles, extra={"coverage": covered})

    def body(self, obligation, user=None) -> dict[str, Any]:
        """The renewal with its history; with `user`, also that user's snooze of it (GET and PATCH, as before)."""
        org = self.organization
        today = services.org_today(org)
        state = renewal_states(org, obligations_for(org).filter(id=obligation.id), today, history=True)[0]
        data = renewal_json(org, state, today, history=True)
        data["ticket_links"] = ticket_reads.for_renewal(org, obligation)  # ticket links
        if user is not None:
            data["snooze"] = my_snooze(org, user, state, today)
        return data

    def render(self, rows, *, many: bool, serializer_class=None) -> Any:
        if many:
            raise NotImplementedError  # list builds its own rows
        user = self.request.user if self.action in ("retrieve", "partial_update") else None
        return self.body(rows, user)

    def list(self, request, *args, **kwargs):
        filterset = self.filterset_class(request.query_params, queryset=self.get_queryset(), request=request)
        errors = filter_errors(filterset, request.query_params)
        if "ordering" in request.query_params:
            errors["ordering"] = ["This list is ordered by action deadline, then name."]
        if errors:
            raise FieldErrors(errors)
        org = self.organization
        today = services.org_today(org)
        states = filter_states(renewal_states(org, filterset.qs, today), filterset.form.cleaned_data, request.user, org)
        states.sort(key=sort_key)
        return Response([renewal_json(org, s, today) for s in states])

    # -- service hooks (money/renewals.py validates, scopes and audits)

    def create_object(self, data):
        return renewals.create_renewal(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        renewals.update_renewal(self.organization, self.request.user, obj, data)
        return obj

    def destroy_object(self, obj):
        renewals.delete_renewal(self.organization, self.request.user, obj)

    # -- actions

    def _write(self, change, status_code=200):
        with transaction.atomic():  # the response inside the write's transaction, as TenantViewSet.create
            obj = self.get_object()
            change(obj)
            return self.detail_response(self.fresh(obj), status_code=status_code)

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        return self._write(lambda o: renewals.archive_renewal(self.organization, request.user, o))

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        return self._write(lambda o: renewals.restore_renewal(self.organization, request.user, o))

    @action(detail=True, methods=["post"])
    def cycles(self, request, pk=None):
        """Establish the next cycle (the cycles themselves are in GET /renewals/<id> as `cycle` and `history`)."""
        data = self.payload()
        return self._write(
            lambda o: renewals.establish_next_cycle(self.organization, request.user, o, data),
            status_code=status.HTTP_201_CREATED,
        )

    def _cycle(self, obligation, cycle_pk):
        return get_object_or_404(RenewalCycle, organization=self.organization, obligation=obligation, id=cycle_pk)

    @action(detail=True, methods=["patch"])
    def cycle(self, request, pk=None, cycle_pk=None):
        data = self.payload()
        with transaction.atomic():
            obligation = self.get_object()
            found = self._cycle(obligation, cycle_pk)
            obligation = self.check_if_match(obligation)
            renewals.update_cycle(self.organization, request.user, obligation, found.id, data)
            return self.detail_response(self.fresh(obligation))

    @action(detail=True, methods=["post"])
    def decision(self, request, pk=None, cycle_pk=None):
        data = self.payload()
        with transaction.atomic():
            obligation = self.get_object()
            found = self._cycle(obligation, cycle_pk)
            renewals.record_decision(self.organization, request.user, obligation, found.id, data)
            return self.detail_response(self.fresh(obligation))

    @action(detail=True, methods=["post"])
    def completion(self, request, pk=None, cycle_pk=None):
        data = self.payload()
        with transaction.atomic():
            obligation = self.get_object()
            found = self._cycle(obligation, cycle_pk)
            renewals.record_completion(self.organization, request.user, obligation, found.id, data)
            return self.detail_response(self.fresh(obligation))

    @action(detail=False, methods=["get"])
    def preview(self, request):
        """The deadline the form would get, so the screen never re-implements the rules."""
        org = self.organization
        params = request.query_params
        errors: dict = {}
        values: dict = {}
        for field in ("renewal_date", "explicit_deadline"):
            renewals._date(params, field, errors, values)
        renewals._period(params, "notice", "notice period", errors, values)
        errors.update(
            unknown_params(params, ("auto_renew", "renewal_date", "notice_value", "notice_unit", "explicit_deadline"))
        )
        auto_renew = params.get("auto_renew") or AutoRenew.UNKNOWN
        if auto_renew not in AutoRenew.values:
            errors["auto_renew"] = ["Choose yes, no or unknown."]
        if errors:
            raise FieldErrors(errors)
        deadline = renewal_rules.action_deadline(
            auto_renew,
            values.get("renewal_date"),
            values.get("notice_value"),
            values.get("notice_unit") or "",
            values.get("explicit_deadline"),
            org.renewal_lead_days,
        )
        return Response(deadline_json(deadline, renewal_rules.timing(deadline.date, services.org_today(org))))
