"""OpenAPI shapes of saved view subscriptions (notify/subscriptions.py builds the responses; the key-for-key test in
tests/test_saved_views.py keeps the two aligned)."""

from rest_framework import serializers

from catalog.models import RECORD_KIND_CHOICES


class SubscriptionViewSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField(allow_null=True, help_text="Null when the view is private to someone else.")
    kind = serializers.ChoiceField(choices=RECORD_KIND_CHOICES)
    shared = serializers.BooleanField()


class SubscriberSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class SavedViewSubscriptionSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    saved_view = SubscriptionViewSerializer(
        allow_null=True, help_text="Null once the view was deleted (each send day then records a skipped email)."
    )
    user = SubscriberSerializer(help_text="The member who gets the email.")
    mine = serializers.BooleanField(help_text="The email is yours.")
    weekday = serializers.IntegerField(help_text="0 Monday to 6 Sunday, in the organization's timezone.")
    weekday_name = serializers.CharField()
    active = serializers.BooleanField()
    last_sent = serializers.DateField(allow_null=True, help_text="The local date of the last weekly email queued.")
    send_time = serializers.CharField(help_text="HH:MM local: the notification send time (digest time, by 23:00).")
    timezone = serializers.CharField()
    can_edit = serializers.BooleanField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class SavedViewSubscriptionInputSerializer(serializers.Serializer):
    saved_view = serializers.CharField(help_text="A saved view's id (not a built-in). On create only.")
    user = serializers.IntegerField(
        required=False, help_text="The member to subscribe (Admins only; default yourself). On create only."
    )
    weekday = serializers.IntegerField(min_value=0, max_value=6, help_text="0 Monday to 6 Sunday.")
    active = serializers.BooleanField(required=False, default=True)


class SubscriptionTestSendSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    detail = serializers.CharField()
    status = serializers.CharField(help_text="The test delivery's status (sent, failed, skipped).")
    delivery = serializers.UUIDField(help_text="The test delivery, in the notification log (kind test).")
