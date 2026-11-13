"""Serializers for the API token viewset (api/viewsets.py). The output serializer renders a token
read-only (core/drf.ReadOnlySerializer); the input serializer only describes the request body, because the
viewset's parser and api/tokens.py validate it.

The secret appears only in the create response (`ApiTokenCreatedSerializer`). Only its SHA-256 is stored, and no
serializer has a field for the hash."""

from rest_framework import serializers

from core.drf import ReadOnlySerializer, UserBriefSerializer


class ApiTokenSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    user = UserBriefSerializer(
        read_only=True, help_text="The owner. The token acts with this account's role, never higher."
    )
    prefix = serializers.CharField(
        read_only=True, help_text="The first characters of the secret, to tell tokens apart."
    )
    read_only = serializers.BooleanField(read_only=True)  # type: ignore[assignment]  # the field, not Field.read_only
    expires_on = serializers.DateField(read_only=True)
    last_used_at = serializers.DateTimeField(read_only=True, allow_null=True)
    revoked_at = serializers.DateTimeField(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)
    created_by = UserBriefSerializer(read_only=True, allow_null=True)


class ApiTokenCreatedSerializer(ApiTokenSerializer):
    secret = serializers.CharField(read_only=True, help_text="Shown once. Only its SHA-256 hash is stored.")


class ApiTokenInputSerializer(serializers.Serializer):
    user = serializers.IntegerField(help_text="The owner: an active member of the organization.")
    name = serializers.CharField(max_length=100)
    read_only = serializers.BooleanField(default=True)  # type: ignore[assignment]  # the field, not Field.read_only
    expires_on = serializers.DateField(help_text="Required: after today, at most 365 days ahead (organization date).")
