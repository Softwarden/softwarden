"""Routes for Needs attention, notification preferences, snoozes, digests, reminders, channels, notification
rules, saved view subscriptions and webhooks. Included from api/urls.py under /api/v1/."""

from django.urls import path

from .channel_viewsets import (
    EmailSettingsView,
    EmailTestConnectionView,
    EmailTestSendView,
    NotificationLogViewSet,
    TeamsDestinationViewSet,
    WebhookDestinationViewSet,
)
from .reminder_viewsets import ReminderViewSet
from .rule_viewsets import NotificationRuleViewSet
from .subscription_viewsets import SavedViewSubscriptionViewSet
from .viewsets import DigestViewSet, NeedsAttentionView, PreferencesView, SnoozeViewSet
from .webhook_viewsets import WebhookDeliveryViewSet, WebhookEventTypesView, WebhookViewSet

urlpatterns = [
    path("needs-attention", NeedsAttentionView.as_view()),
    path("auth/me/preferences", PreferencesView.as_view()),
    path("needs-attention/snoozes", SnoozeViewSet.as_view({"get": "list", "post": "create"})),
    path("needs-attention/snoozes/<uuid:pk>", SnoozeViewSet.as_view({"get": "retrieve", "delete": "destroy"})),
    path("admin/digests", DigestViewSet.as_view({"get": "list"})),
    path("admin/digests/<uuid:pk>", DigestViewSet.as_view({"get": "retrieve"})),
    path("admin/digests/<uuid:pk>/retry", DigestViewSet.as_view({"post": "retry"})),
    path("reminders", ReminderViewSet.as_view({"get": "list"})),
    path("reminders/<uuid:pk>", ReminderViewSet.as_view({"get": "retrieve"})),
    *(
        path(f"reminders/<uuid:pk>/{name}", ReminderViewSet.as_view({"post": name}))
        for name in ("snooze", "acknowledge", "reopen", "resolve")
    ),
    # Channels and the notification log (Admin)
    path("notification-settings/email", EmailSettingsView.as_view()),
    path("notification-settings/email/test-connection", EmailTestConnectionView.as_view()),
    path("notification-settings/email/test-send", EmailTestSendView.as_view()),
    path(
        "notification-settings/teams-destinations", TeamsDestinationViewSet.as_view({"get": "list", "post": "create"})
    ),
    path(
        "notification-settings/teams-destinations/<uuid:pk>",
        TeamsDestinationViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
    ),
    path(
        "notification-settings/teams-destinations/<uuid:pk>/test-send",
        TeamsDestinationViewSet.as_view({"post": "test_send"}),
    ),
    path(
        "notification-settings/webhook-destinations",
        WebhookDestinationViewSet.as_view({"get": "list", "post": "create"}),
    ),
    path(
        "notification-settings/webhook-destinations/<uuid:pk>",
        WebhookDestinationViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
    ),
    path(
        "notification-settings/webhook-destinations/<uuid:pk>/test-send",
        WebhookDestinationViewSet.as_view({"post": "test_send"}),
    ),
    path("notification-log", NotificationLogViewSet.as_view({"get": "list"})),
    path("notification-log/<uuid:pk>", NotificationLogViewSet.as_view({"get": "retrieve"})),
    path("notification-log/<uuid:pk>/resend", NotificationLogViewSet.as_view({"post": "resend"})),
    # Webhooks (Admin, capability webhooks): endpoints, their events, the delivery log
    path("webhooks", WebhookViewSet.as_view({"get": "list", "post": "create"})),
    path("webhooks/event-types", WebhookEventTypesView.as_view()),
    path(
        "webhooks/<uuid:pk>",
        WebhookViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
    ),
    path("webhooks/<uuid:pk>/ping", WebhookViewSet.as_view({"post": "ping"})),
    path("webhooks/<uuid:pk>/rotate-secret", WebhookViewSet.as_view({"post": "rotate_secret"})),
    path("webhook-deliveries", WebhookDeliveryViewSet.as_view({"get": "list"})),
    path("webhook-deliveries/<uuid:pk>", WebhookDeliveryViewSet.as_view({"get": "retrieve"})),
    path("webhook-deliveries/<uuid:pk>/resend", WebhookDeliveryViewSet.as_view({"post": "resend"})),
    # Notification rules (Admin)
    path("notification-rules", NotificationRuleViewSet.as_view({"get": "list", "post": "create"})),
    path(
        "notification-rules/<uuid:pk>",
        NotificationRuleViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
    ),
    path("notification-rules/<uuid:pk>/preview", NotificationRuleViewSet.as_view({"get": "preview"})),
    path("notification-rules/<uuid:pk>/test-send", NotificationRuleViewSet.as_view({"post": "test_send"})),
    # Saved view subscriptions (every member; their own, or everyone's for an Admin)
    path("saved-view-subscriptions", SavedViewSubscriptionViewSet.as_view({"get": "list", "post": "create"})),
    path(
        "saved-view-subscriptions/<uuid:pk>",
        SavedViewSubscriptionViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
    ),
    path("saved-view-subscriptions/<uuid:pk>/test-send", SavedViewSubscriptionViewSet.as_view({"post": "test_send"})),
]
