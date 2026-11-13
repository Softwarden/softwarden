"""Notification channels and the notification log. Routes are in notify/urls.py.

Roles: Admin only, reads included (the settings hold who is emailed and where; the log shows recipient addresses).
Every write goes through notify/channel_services.py. Secrets are write-only: no response, ETag or audit
event carries one. Test sends run in the request, outside any transaction, because a delivery's claim must commit
before anything is sent (notify/deliveries.py)."""

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django import forms
from django.db import transaction
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

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
    etag,
    policy,
    refuse_unknown_params,
    require_match,
    revision,
    uuid_value,
)

from . import channel_services, channels
from .channel_serializers import (
    DeliveryDetailSerializer,
    DeliverySerializer,
    EmailSettingsInputSerializer,
    EmailSettingsSerializer,
    TeamsDestinationInputSerializer,
    TeamsDestinationSerializer,
    TestResultSerializer,
    TestSendResultSerializer,
    WebhookDestinationInputSerializer,
    WebhookDestinationSerializer,
)
from .models import (
    Channel,
    Delivery,
    DeliveryKind,
    DeliveryStatus,
    EmailSettings,
    TeamsDestination,
    WebhookDestination,
)

ERRORS = {c: PROBLEMS[c] for c in (400, 403)}
WRITE_ERRORS = {c: PROBLEMS[c] for c in (400, 403, 409)}
# Secret slots never feed an ETag. Replacing one still moves the ETag through its `_set_at`.
EMAIL_SECRET_FIELDS = (
    "graph_secret_ciphertext",
    "graph_secret_key_id",
    "smtp_password_ciphertext",
    "smtp_password_key_id",
)
DESTINATION_SECRET_FIELDS = (
    "url_ciphertext",
    "url_key_id",
    "signing_secret_ciphertext",
    "signing_secret_key_id",
    "previous_signing_secret_ciphertext",
    "previous_signing_secret_key_id",
)
# What the worker keeps on a webhook (notify/webhooks.record_outcome): not settings, so it doesn't move the ETag, and
# an Admin's edit isn't refused because a delivery ran meanwhile.
DESTINATION_SUMMARY_FIELDS = (
    "consecutive_failures",
    "last_delivery_at",
    "last_delivery_outcome",
    "last_delivery_status_code",
    "last_delivery_id",
)
DESTINATION_SKIP = DESTINATION_SECRET_FIELDS + DESTINATION_SUMMARY_FIELDS


def _test_send_response(delivery: Delivery) -> Response:
    ok = delivery.status == DeliveryStatus.SENT
    detail = "Sent." if ok else (delivery.last_error or f"Not sent ({delivery.status}).")
    return Response({"ok": ok, "detail": detail, "delivery": DeliverySerializer(delivery).data})


# ---- email settings (one per organization) ------------------------------------------------------------------------


def email_revision(row: EmailSettings | None) -> str:
    return revision(row, skip=EMAIL_SECRET_FIELDS) if row is not None else revision(extra={"email_settings": None})


class EmailSettingsView(RoleAPIView):
    """The organization's email transport. GET answers the defaults (installation_default) until an Admin saves."""

    role_policy = policy(ADMIN, ADMIN)

    def respond(self, row: EmailSettings | None) -> Response:
        serializer = EmailSettingsSerializer(context={"organization": self.organization})
        response = Response(serializer.to_representation(row))
        response["ETag"] = etag(email_revision(row))
        return response

    @extend_schema(summary="Email settings (Admin)", responses={200: EmailSettingsSerializer, **ERRORS})
    def get(self, request):
        refuse_unknown_params(request)
        return self.respond(channels.email_settings(self.organization))

    @extend_schema(
        summary="Change email settings (Admin); send a secret only to replace it",
        description="Graph sends as the app from the sender mailbox: grant Mail.Send only through Exchange RBAC for "
        "Applications scoped to that mailbox, never as an Entra API permission (docs/PERMISSIONS.md). SMTP is the "
        "fallback for other servers.",
        parameters=[IF_MATCH],
        request=EmailSettingsInputSerializer,
        responses={200: EmailSettingsSerializer, **WRITE_ERRORS, 412: PROBLEMS[412]},
    )
    def patch(self, request):
        data = self.payload()
        with transaction.atomic():
            row = EmailSettings.objects.select_for_update().filter(organization=self.organization).first()
            require_match(request, email_revision(row))
            channel_services.update_email_settings(self.organization, request.user, data)
            return self.respond(channels.email_settings(self.organization))

    @extend_schema(
        summary="Forget the email settings and their secrets (Admin): back to the installation default",
        parameters=[IF_MATCH],
        responses={204: None, **ERRORS, 412: PROBLEMS[412]},
    )
    def delete(self, request):
        with transaction.atomic():
            row = EmailSettings.objects.select_for_update().filter(organization=self.organization).first()
            require_match(request, email_revision(row))
            channel_services.delete_email_settings(self.organization, request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class EmailTestConnectionView(RoleAPIView):
    role_policy = policy(ADMIN, ADMIN)

    @extend_schema(
        summary="Check the email settings without sending (Admin)",
        description="Graph: Microsoft issues a token (this doesn't prove the mailbox scope; a test email does). SMTP: "
        "connect, start TLS and sign in.",
        request=None,
        responses={200: TestResultSerializer, **ERRORS},
    )
    def post(self, request):
        result = channel_services.test_email_connection(self.organization, request.user)
        return Response({"ok": result.ok, "detail": result.detail})


class EmailTestSendView(RoleAPIView):
    role_policy = policy(ADMIN, ADMIN)

    @extend_schema(
        summary="Send a test email to yourself through the configured transport (Admin)",
        description="A real message to your account's address, recorded in the notification log (kind test).",
        request=None,
        responses={200: TestSendResultSerializer, **ERRORS},
    )
    def post(self, request):
        delivery = channel_services.test_send_email(self.organization, request.user)
        return _test_send_response(delivery)


# ---- Teams and webhook destinations --------------------------------------------------------------------------------


class DestinationViewSet(TenantViewSet):
    role_policy = policy(ADMIN, ADMIN)
    pagination_class = None
    ordering_fields = ("name", "created_at")
    default_ordering = ("name",)
    model: Any = None

    def base_queryset(self):
        return self.model.objects.all()

    def revision_of(self, obj) -> str:
        return revision(obj, skip=DESTINATION_SKIP)

    def create_object(self, data):
        return channel_services.create_destination(self.organization, self.request.user, self.model, data)

    def update_object(self, obj, data):
        return channel_services.update_destination(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        channel_services.delete_destination(self.organization, self.request.user, obj)

    @action(detail=True, methods=["post"], url_path="test-send")
    def test_send(self, request, pk=None):
        delivery = channel_services.test_send_destination(self.organization, request.user, self.get_object())
        return _test_send_response(delivery)


TEST_SEND_RESPONSES: dict[int | str, Any] = {
    200: TestSendResultSerializer,
    **{c: PROBLEMS[c] for c in (400, 403, 404, 409)},
}


@extend_schema_view(
    list=extend_schema(summary="Teams destinations (Admin)"),
    create=extend_schema(summary="Add a Teams destination: a channel's Workflows webhook (Admin)"),
    retrieve=extend_schema(summary="Get a Teams destination (Admin)"),
    partial_update=extend_schema(summary="Change a Teams destination (Admin); send url only to replace it"),
    destroy=extend_schema(summary="Delete a Teams destination (Admin); its log rows stay"),
    test_send=extend_schema(
        summary="Post a test card to the channel (Admin)", request=None, responses=TEST_SEND_RESPONSES
    ),
)
class TeamsDestinationViewSet(DestinationViewSet):
    """Workflows webhooks ("Send webhook alerts to a channel"). The address is a bearer secret: write-only."""

    model = TeamsDestination
    serializer_class = TeamsDestinationSerializer
    input_serializer_class = TeamsDestinationInputSerializer


@extend_schema_view(
    list=extend_schema(summary="Webhook destinations (Admin)"),
    create=extend_schema(summary="Add a webhook destination (Admin)"),
    retrieve=extend_schema(summary="Get a webhook destination (Admin)"),
    partial_update=extend_schema(summary="Change a webhook destination (Admin); send secrets only to replace them"),
    destroy=extend_schema(summary="Delete a webhook destination (Admin); its log rows stay"),
    test_send=extend_schema(summary="Post a signed test event (Admin)", request=None, responses=TEST_SEND_RESPONSES),
)
class WebhookDestinationViewSet(DestinationViewSet):
    """Generic HTTPS webhooks, signed with HMAC SHA-256 (X-Softwarden-Signature: t=<unix>,v1=<hex>)."""

    model = WebhookDestination
    serializer_class = WebhookDestinationSerializer
    input_serializer_class = WebhookDestinationInputSerializer


# ---- the notification log --------------------------------------------------------------------------------------------


def _flag(value: str) -> bool | None:
    if value == "":
        return None
    if value not in ("true", "false"):
        raise forms.ValidationError("Use true or false.")
    return value == "true"


def _day(value: str) -> date | None:
    try:
        return filter_day(value)  # clamped, so the inclusive end's extra day always fits
    except ValueError:
        raise forms.ValidationError("Enter a date as YYYY-MM-DD.") from None


class DeliveryFilterSet(TenantFilterSet):
    channel = ParamFilter(
        parse=choices_csv(Channel.values),
        field_name="channel",
        lookup_expr="in",
        choices=Channel.choices,
        help_text="Comma-separated: email, teams, webhook.",
    )
    status = ParamFilter(
        parse=choices_csv(DeliveryStatus.values),
        field_name="status",
        lookup_expr="in",
        choices=DeliveryStatus.choices,
        help_text="Comma-separated: queued, sending, sent, failed, skipped.",
    )
    kind = ParamFilter(
        parse=choices_csv(DeliveryKind.values),
        field_name="kind",
        lookup_expr="in",
        choices=DeliveryKind.choices,
        help_text="Comma-separated: reminder, escalation, digest, subscription, test.",
    )
    reminder = ParamFilter(parse=uuid_value, field_name="reminder_id", help_text="Deliveries for one reminder.")
    rule = ParamFilter(parse=uuid_value, field_name="rule_id", help_text="Deliveries of one notification rule.")
    baseline = ParamFilter(
        parse=_flag,
        field_name="baseline",
        help_text="true: only baselines (recorded when a rule started or widened, never sent); false: without them. "
        "Default: both.",
    )
    to = ParamFilter(
        parse=_day, method="filter_to", help_text="Queued on or before (YYYY-MM-DD, organization timezone)."
    )

    class Meta:
        model = Delivery
        fields: list[str] = []

    def _start_of(self, day: date) -> datetime:
        return datetime.combine(day, time.min, tzinfo=ZoneInfo(self.membership.organization.timezone))

    def filter_from(self, qs, name, value):
        return qs.filter(created_at__gte=self._start_of(value)) if value else qs

    def filter_to(self, qs, name, value):
        return qs.filter(created_at__lt=self._start_of(value + timedelta(days=1))) if value else qs


# `from` is a Python keyword, so it is added after the class body (as the audit log's).
DeliveryFilterSet.base_filters["from"] = ParamFilter(
    parse=_day,
    method="filter_from",
    field_name="created_at",
    help_text="Queued on or after (YYYY-MM-DD, organization timezone).",
)


@extend_schema_view(
    list=extend_schema(
        summary="Notification log (Admin): every delivery, newest first",
        parameters=[OpenApiParameter("cursor", str, description="The next value from a page.")],
    ),
    retrieve=extend_schema(summary="One delivery with its rendered message (Admin)"),
    resend=extend_schema(
        summary="Resend a delivery as a new one (Admin)",
        description="The same message, queued now with a fresh key and resend_of set; the worker sends it within a "
        "minute. 409 while the original is still queued or sending, or when its destination was deleted.",
        request=None,
        responses={202: DeliverySerializer, **{c: PROBLEMS[c] for c in (400, 403, 404, 409)}},
    ),
)
class NotificationLogViewSet(TenantViewSet):
    role_policy = policy(ADMIN, ADMIN)
    serializer_class = DeliverySerializer
    detail_serializer_class = DeliveryDetailSerializer
    filterset_class = DeliveryFilterSet
    ordering_fields = ("created_at",)
    default_ordering = ("-created_at",)

    def base_queryset(self):
        return Delivery.objects.select_related("requested_by", "reminder")

    @action(detail=True, methods=["post"])
    def resend(self, request, pk=None):
        with transaction.atomic():
            copy = channel_services.resend(self.organization, request.user, self.get_object())
            return self.detail_response(
                self.fresh(copy), status_code=status.HTTP_202_ACCEPTED, serializer_class=DeliverySerializer
            )
