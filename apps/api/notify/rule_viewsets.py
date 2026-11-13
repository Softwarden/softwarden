"""Notification rules: `/notification-rules`, their preview and test send. Routes are in notify/urls.py.

Roles: Admin only, reads included: a rule names who is emailed and where (people, teams, destinations), and its
preview lists recipients' addresses, as the notification log does. Every write goes through notify/rule_services.py.
Core feature: no capability. A built-in rule can't be deleted (409); the daily digest's rule takes only its
name, `enabled` and `digest_time`. Test sends run in the request, outside any transaction, because a delivery's claim
must commit before anything is sent (notify/deliveries.py)."""

from typing import Any

from django.db.models import Prefetch
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import exceptions
from rest_framework.decorators import action
from rest_framework.response import Response

from catalog.models import Person
from core.dates import org_today
from core.drf import ADMIN, PROBLEMS, TenantViewSet, policy, refuse_unknown_params, revision
from core.input import whole_number

from . import rule_services, rules
from .channel_serializers import DeliverySerializer
from .models import DeliveryStatus, NotificationRule
from .rule_serializers import (
    NotificationRuleInputSerializer,
    NotificationRuleSerializer,
    PreviewSerializer,
    RuleTestSendInputSerializer,
    RuleTestSendSerializer,
)

PREVIEW_DEFAULT_DAYS = 30


@extend_schema_view(
    list=extend_schema(
        summary="Notification rules (Admin), by name",
        description="By name. The built-ins are seeded with the organization.",
    ),
    create=extend_schema(
        summary="Add a notification rule (Admin)",
        description="A reminders rule: which reminder types, lead times, escalation steps, channels, audience, "
        "fallback and filters. The rule must be able to send something.",
    ),
    retrieve=extend_schema(summary="Get a notification rule (Admin)"),
    partial_update=extend_schema(
        summary="Change a notification rule (Admin)",
        description="Send only what changes; lists replace the stored list. The daily digest's rule takes name, "
        "enabled and digest_time (the organization's digest time).",
    ),
    destroy=extend_schema(
        summary="Delete a notification rule (Admin)",
        description="Refused (409) for a built-in rule: turn it off instead. Its deliveries stay in the log.",
    ),
    preview=extend_schema(
        summary="What the rule would send in the next days (Admin; no writes)",
        description="One entry per reminder and lead time or escalation step, on the day it would go out if nothing "
        "else changed (snoozes end on their date; nobody acknowledges or resolves meanwhile), with the recipients and "
        "destinations that haven't had it. A disabled rule previews as if it were on.",
        parameters=[OpenApiParameter("days", int, description=f"1 to {rules.PREVIEW_MAX_DAYS}; default 30.")],
        responses={200: PreviewSerializer, **{c: PROBLEMS[c] for c in (400, 403, 404)}},
    ),
    test_send=extend_schema(
        summary="Send a sample of the rule now (Admin)",
        description="Rendered for the rule (its soonest reminder, else an example): by email to your own address "
        "(email, default true) and to a Teams or webhook destination you choose. The daily digest's rule sends your "
        "own digest as it would be built now, by email only. Recorded in the notification log as kind test.",
        request=RuleTestSendInputSerializer,
        responses={200: RuleTestSendSerializer, **{c: PROBLEMS[c] for c in (400, 403, 404)}},
    ),
)
class NotificationRuleViewSet(TenantViewSet):
    role_policy = policy(ADMIN, ADMIN)
    pagination_class = None
    serializer_class = NotificationRuleSerializer
    input_serializer_class = NotificationRuleInputSerializer
    ordering_fields = ("name", "created_at")
    default_ordering = ("name",)

    def base_queryset(self):
        people = Person.objects.only("id", "name")
        return NotificationRule.objects.select_related(
            "team", "escalation_team", "fallback_team", "fallback_person", "vendor"
        ).prefetch_related(
            Prefetch("people", queryset=people),
            Prefetch("escalation_people", queryset=people),
            "teams_destinations",
            "webhook_destinations",
        )

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        return {"organization": self.organization}

    def revision_of(self, obj) -> str:
        return revision(obj, extra={"digest_time": self.organization.digest_time.isoformat()})

    def create_object(self, data):
        return rule_services.create_rule(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        return rule_services.update_rule(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        rule_services.delete_rule(self.organization, self.request.user, obj)

    @action(detail=True, methods=["get"])
    def preview(self, request, pk=None):
        refuse_unknown_params(request, ("days",))
        rule = self.get_object()
        raw = request.query_params.get("days", str(PREVIEW_DEFAULT_DAYS))
        days = whole_number(raw, 1, rules.PREVIEW_MAX_DAYS)
        if days is None:
            raise exceptions.ValidationError({"days": [f"Use 1 to {rules.PREVIEW_MAX_DAYS} days."]})
        today = org_today(self.organization)
        events = rules.preview(self.organization, rule, today, days)
        return Response({"rule": str(rule.id), "start": today.isoformat(), "days": days, "events": events})

    @action(detail=True, methods=["post"], url_path="test-send")
    def test_send(self, request, pk=None):
        done = rule_services.test_send(self.organization, request.user, self.get_object(), self.payload())
        results = []
        for d in done:
            ok = d.status == DeliveryStatus.SENT
            detail = "Sent." if ok else (d.last_error or f"Not sent ({d.status}).")
            results.append({"ok": ok, "detail": detail, "delivery": DeliverySerializer(d).data})
        return Response({"ok": all(r["ok"] for r in results), "results": results})
