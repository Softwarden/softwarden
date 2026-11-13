"""Webhooks (notify/webhooks.py): endpoints with their event subscriptions, a ping, secret rotation, and the
webhook delivery log with every attempt and resend. Routes are in notify/urls.py.

Roles: Admin only, reads included (an endpoint names where data goes; the log shows what was sent), with a session or
an Admin's API token. Capability `webhooks` (community). Every write goes through notify/channel_services.py or
notify/webhooks.py. The address and the signing secrets are write-only: no response, ETag or audit event
carries one. An endpoint is the same row as a webhook destination (`/notification-settings/webhook-
destinations`), so notification rules can name it too."""

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django import forms
from django.db import transaction
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from core.capabilities import WEBHOOKS
from core.dates import filter_day
from core.drf import (
    ADMIN,
    IF_MATCH,
    PROBLEMS,
    ParamFilter,
    RoleAPIView,
    TenantFilterSet,
    TenantViewSet,
    choices_csv,
    policy,
    refuse_unknown_params,
    uuid_value,
)

from . import channel_services, webhooks
from .channel_viewsets import DestinationViewSet
from .models import Channel, Delivery, DeliveryAttempt, DeliveryKind, DeliveryStatus, WebhookDestination
from .webhook_serializers import (
    EventCatalogSerializer,
    PingResultSerializer,
    RotateSecretInputSerializer,
    WebhookDeliveryDetailSerializer,
    WebhookDeliverySerializer,
    WebhookInputSerializer,
    WebhookSerializer,
)

ADMIN_WEBHOOKS = policy(ADMIN, ADMIN, capability=WEBHOOKS)
ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}
WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404, 409)}


@extend_schema_view(
    list=extend_schema(summary="Webhook endpoints (Admin)"),
    create=extend_schema(
        summary="Add a webhook endpoint (Admin)",
        description="An https address (checked against private, loopback, link-local and metadata addresses now and "
        "again at every send), a signing secret and the events it receives. Both secrets are write-only.",
    ),
    retrieve=extend_schema(summary="Get a webhook endpoint (Admin)"),
    partial_update=extend_schema(
        summary="Change a webhook endpoint (Admin); send url or signing_secret only to replace them",
        description="Turning an endpoint Softwarden switched off back on clears its failure count and reason.",
    ),
    destroy=extend_schema(
        summary="Delete a webhook endpoint (Admin); its delivery log stays",
        description="409 while a notification rule names it.",
    ),
    ping=extend_schema(
        summary="Send a signed ping now and see the endpoint's answer (Admin)",
        description="One attempt in the request, recorded in the delivery log (kind test, event ping). 409 while the "
        "endpoint is off.",
        request=None,
        responses={200: PingResultSerializer, **WRITE_ERRORS},
    ),
    rotate_secret=extend_schema(
        summary="Rotate the signing secret (Admin)",
        description="The new secret signs at once; the previous one keeps signing beside it (a second v1= in "
        "X-Softwarden-Signature) for grace_period_hours, so receivers can switch without missing events.",
        parameters=[IF_MATCH],
        request=RotateSecretInputSerializer,
        responses={200: WebhookSerializer, **WRITE_ERRORS, 412: PROBLEMS[412]},
    ),
)
class WebhookViewSet(DestinationViewSet):
    """Signed webhook endpoints and the events each receives."""

    role_policy = ADMIN_WEBHOOKS
    model = WebhookDestination
    serializer_class = WebhookSerializer
    input_serializer_class = WebhookInputSerializer
    if_match_actions = ("rotate_secret",)

    def create_object(self, data):
        return channel_services.create_destination(
            self.organization, self.request.user, self.model, data, subscriptions=True
        )

    def update_object(self, obj, data):
        return channel_services.update_destination(self.organization, self.request.user, obj, data, subscriptions=True)

    @action(detail=True, methods=["post"])
    def ping(self, request, pk=None):
        delivery = channel_services.test_send_destination(self.organization, request.user, self.get_object())
        ok = delivery.status == DeliveryStatus.SENT
        detail = "The endpoint answered 2xx." if ok else (delivery.last_error or f"Not sent ({delivery.status}).")
        fresh = Delivery.objects.select_related("requested_by").get(pk=delivery.pk)
        body = WebhookDeliveryDetailSerializer(fresh).data
        return Response({"ok": ok, "detail": detail, "delivery": body})

    @action(detail=True, methods=["post"], url_path="rotate-secret")
    def rotate_secret(self, request, pk=None):
        data = self.payload()
        with transaction.atomic():
            endpoint = self.check_if_match(self.get_object())
            endpoint = webhooks.rotate_secret(self.organization, request.user, endpoint, data)
            return self.detail_response(self.fresh(endpoint))


class WebhookEventTypesView(RoleAPIView):
    role_policy = ADMIN_WEBHOOKS

    @extend_schema(
        summary="The events a webhook can subscribe to (Admin)",
        description="Each type with its family; subscribe to a whole family with <family>.*.",
        responses={200: EventCatalogSerializer, **{c: PROBLEMS[c] for c in (403,)}},
    )
    def get(self, request):
        refuse_unknown_params(request)
        return Response(
            {
                "types": [
                    {"type": t, "family": webhooks.family_of(t), "description": webhooks.EVENT_DESCRIPTIONS[t]}
                    for t in webhooks.EVENT_TYPES
                ],
                "wildcards": list(webhooks.WILDCARDS),
            }
        )


# ---- the delivery log --------------------------------------------------------------------------------------------

EVENT_FILTER_MAX = 60


def _events(value: str) -> list[str] | None:
    """A comma-separated list of event types (a family wildcard selects its family)."""
    if value == "":
        return None
    found = [v.strip() for v in value.split(",") if v.strip()]
    if not found or any(
        len(v) > EVENT_FILTER_MAX or not v.replace(".", "").replace("_", "").replace("*", "").isalnum() for v in found
    ):
        raise forms.ValidationError("Use event types such as record.updated, or a family such as record.*.")
    return found


def _day(value: str) -> date | None:
    try:
        return filter_day(value)  # clamped, so the inclusive end's extra day always fits
    except ValueError:
        raise forms.ValidationError("Enter a date as YYYY-MM-DD.") from None


class WebhookDeliveryFilterSet(TenantFilterSet):
    webhook = ParamFilter(parse=uuid_value, field_name="webhook_destination_id", help_text="One endpoint's deliveries.")
    status = ParamFilter(
        parse=choices_csv(DeliveryStatus.values),
        field_name="status",
        lookup_expr="in",
        choices=DeliveryStatus.choices,
        help_text="Comma-separated: queued, sending, sent, failed, skipped.",
    )
    event = ParamFilter(
        parse=_events,
        method="filter_event",
        help_text="Comma-separated event types (record.updated, ping, reminder.due, ...) or families (record.*).",
    )
    kind = ParamFilter(
        parse=choices_csv(DeliveryKind.values),
        field_name="kind",
        lookup_expr="in",
        choices=DeliveryKind.choices,
        help_text="Comma-separated: event, test (pings), reminder, escalation.",
    )
    to = ParamFilter(
        parse=_day, method="filter_to", help_text="Queued on or before (YYYY-MM-DD, organization timezone)."
    )

    class Meta:
        model = Delivery
        fields: list[str] = []

    def filter_event(self, qs, name, value):
        if not value:
            return qs
        from django.db.models import Q

        match = Q()
        for v in value:
            match |= Q(event_type__startswith=v[:-1]) if v.endswith(".*") else Q(event_type=v)
        return qs.filter(match)

    def _start_of(self, day: date) -> datetime:
        return datetime.combine(day, time.min, tzinfo=ZoneInfo(self.membership.organization.timezone))

    def filter_from(self, qs, name, value):
        return qs.filter(created_at__gte=self._start_of(value)) if value else qs

    def filter_to(self, qs, name, value):
        return qs.filter(created_at__lt=self._start_of(value + timedelta(days=1))) if value else qs


WebhookDeliveryFilterSet.base_filters["from"] = ParamFilter(
    parse=_day,
    method="filter_from",
    field_name="created_at",
    help_text="Queued on or after (YYYY-MM-DD, organization timezone).",
)


@extend_schema_view(
    list=extend_schema(
        summary="Webhook delivery log (Admin): every post to an endpoint, newest first",
        parameters=[OpenApiParameter("cursor", str, description="The next value from a page.")],
    ),
    retrieve=extend_schema(summary="One webhook delivery with its payload and every attempt (Admin)"),
    resend=extend_schema(
        summary="Resend a webhook delivery as a new one (Admin)",
        description="The same payload (the same event id) with a new delivery id, queued now; the worker sends it on "
        "its next tick. 409 while the original is still queued or sending, or when its endpoint was deleted.",
        request=None,
        responses={202: WebhookDeliverySerializer, **WRITE_ERRORS},
    ),
)
class WebhookDeliveryViewSet(TenantViewSet):
    role_policy = ADMIN_WEBHOOKS
    serializer_class = WebhookDeliverySerializer
    detail_serializer_class = WebhookDeliveryDetailSerializer
    filterset_class = WebhookDeliveryFilterSet
    ordering_fields = ("created_at",)
    default_ordering = ("-created_at",)

    def base_queryset(self):
        qs = Delivery.objects.filter(channel=Channel.WEBHOOK).select_related("requested_by")
        # A list row never reads the payload; the detail does.
        return qs.defer("content") if self.action == "list" else qs

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        """The last attempt's status code of every delivery on the page, in one query."""
        codes: dict[Any, int | None] = {}
        attempts = DeliveryAttempt.objects.filter(
            organization=self.organization, delivery_id__in=[r.id for r in rows]
        ).order_by("delivery_id", "-started_at", "-number")
        for delivery_id, code in attempts.distinct("delivery_id").values_list("delivery_id", "status_code"):
            codes[delivery_id] = code
        return {"last_status_codes": codes}

    @action(detail=True, methods=["post"])
    def resend(self, request, pk=None):
        with transaction.atomic():
            copy = channel_services.resend(self.organization, request.user, self.get_object())
            return self.detail_response(
                self.fresh(copy), status_code=status.HTTP_202_ACCEPTED, serializer_class=WebhookDeliverySerializer
            )
