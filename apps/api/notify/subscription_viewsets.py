"""Saved view subscriptions: `/saved-view-subscriptions`, their changes and a test send. Routes are in
notify/urls.py; the rules are in notify/subscriptions.py.

Roles: every member (Viewer and up) reads and writes, because anyone may subscribe themselves to a view they can see;
the service refuses the rest: subscribing someone else is Admin only, and someone else's subscription doesn't exist
for a non-Admin (404). Core feature: no capability. The test send runs in the request, outside any transaction,
because a delivery's claim must commit before anything is sent (notify/deliveries.py)."""

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.response import Response

from catalog.services import is_uuid
from core.drf import PROBLEMS, VIEWER, TenantViewSet, filter_errors, policy
from core.input import whole_number
from core.problems import FieldErrors

from . import subscriptions
from .models import DeliveryStatus, SavedViewSubscription
from .subscription_serializers import (
    SavedViewSubscriptionInputSerializer,
    SavedViewSubscriptionSerializer,
    SubscriptionTestSendSerializer,
)

ERRORS = {c: PROBLEMS[c] for c in (400, 403, 404)}


@extend_schema_view(
    list=extend_schema(
        summary="Saved view subscriptions: yours, or everyone's for an Admin",
        parameters=[
            OpenApiParameter("saved_view", str, description="Only this view's subscriptions."),
            OpenApiParameter("user", int, description="Only this member's (Admins; others see only their own)."),
        ],
    ),
    create=extend_schema(
        summary="Subscribe to a saved view's weekly email",
        description="Yourself, to a view you can see; an Admin can also subscribe another member (`user`) to a view "
        "that member can see. One subscription per member and view.",
    ),
    retrieve=extend_schema(summary="Get a saved view subscription"),
    partial_update=extend_schema(
        summary="Change a subscription's weekday or turn it off and on",
        description="The subscriber or an Admin. Takes If-Match.",
    ),
    destroy=extend_schema(summary="Remove a saved view subscription (the subscriber or an Admin)"),
    test_send=extend_schema(
        summary="Send this week's email now (the subscriber or an Admin)",
        description="Rendered as the weekly email would be today, with the subscriber's own access, and sent to the "
        "subscriber. 409 with the reason when it couldn't be sent (view deleted, filter problems, no access, saved "
        "view emails turned off); 429 within a minute of the last test of the subscription. Recorded in the notification "
        "log as kind test.",
        request=None,
        responses={
            200: SubscriptionTestSendSerializer,
            **{c: PROBLEMS[c] for c in (403, 404, 409)},
            429: OpenApiResponse(description="A test was sent within the last minute; Retry-After says when to retry"),
        },
    ),
)
class SavedViewSubscriptionViewSet(TenantViewSet):
    role_policy = policy(VIEWER, VIEWER)
    pagination_class = None
    serializer_class = SavedViewSubscriptionSerializer
    input_serializer_class = SavedViewSubscriptionInputSerializer
    ordering_fields = ()

    def base_queryset(self):
        return SavedViewSubscription.objects.select_related("saved_view", "saved_view__user", "membership__user")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return super().get_queryset()
        return subscriptions.visible(self.organization, self.request.user, self.membership.role)

    def render(self, rows, *, many: bool, serializer_class=None):
        org, user, role = self.organization, self.request.user, self.membership.role
        if many:
            return [subscriptions.subscription_json(org, s, user, role) for s in rows]
        return subscriptions.subscription_json(org, rows, user, role)

    def list(self, request, *args, **kwargs):
        params = request.query_params
        errors = filter_errors(None, params, {"saved_view", "user"})
        qs = self.get_queryset()
        view = params.get("saved_view")
        if view:
            if not is_uuid(view):
                errors["saved_view"] = ["Use a saved view's id."]
            else:
                qs = qs.filter(saved_view_id=view)
        user = params.get("user")
        if user:
            user_number = whole_number(user, 1)
            if user_number is None:
                errors["user"] = ["Use a user id."]
            else:
                qs = qs.filter(membership__user_id=user_number)
        if errors:
            raise FieldErrors(errors)
        return Response(self.render(qs.order_by("weekday", "created_at", "id"), many=True))

    def create_object(self, data):
        return subscriptions.create(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        return subscriptions.update(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        subscriptions.delete(self.organization, self.request.user, obj)

    @action(detail=True, methods=["post"], url_path="test-send")
    def test_send(self, request, pk=None):
        done = subscriptions.test_send(self.organization, request.user, self.get_object())
        ok = done.status == DeliveryStatus.SENT
        detail = "Sent." if ok else (done.last_error or f"Not sent ({done.status}).")
        return Response({"ok": ok, "detail": detail, "status": done.status, "delivery": str(done.id)})
