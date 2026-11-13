"""Serializers for connections (core/connection_viewsets.py). Output only: core/connections.py validates every
write. The secret is write-only: no output field holds it, only `secret_set_on`."""

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .connections import PROBLEMS, is_simulated
from .drf import BriefSerializer, ReadOnlySerializer, UserBriefSerializer, represent


class LastTestSerializer(serializers.Serializer):
    at = serializers.DateTimeField()
    ok = serializers.BooleanField()
    by = UserBriefSerializer(allow_null=True)
    problem = serializers.ChoiceField(["", *PROBLEMS], help_text="Empty on success.")
    problem_label = serializers.CharField()
    code = serializers.CharField(help_text="The provider's error code (an AADSTS code, invalid_client, ...), or empty.")
    detail = serializers.CharField(help_text="What the test found, in Softwarden's words; never a secret or a token.")
    next = serializers.CharField(help_text="What to do next.")


def last_test_of(c) -> dict[str, Any] | None:
    """A connection's last test (LastTestSerializer), or None when it was never tested. Also on a connector's
    connection brief (discovery/serializers.py)."""
    if c.tested_at is None:
        return None
    result = c.test_result or {}
    return {
        "at": serializers.DateTimeField().to_representation(c.tested_at),
        "ok": bool(c.test_ok),
        "by": represent(UserBriefSerializer, c.tested_by),
        **{k: str(result.get(k, "")) for k in ("problem", "problem_label", "code", "detail", "next")},
    }


class _ConnectionSerializer(ReadOnlySerializer):
    """Context: `connectors` {connection id: [{id, name}]}."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    enabled = serializers.BooleanField(read_only=True)
    notes = serializers.CharField(read_only=True)
    client_id = serializers.CharField(read_only=True)
    simulated = serializers.SerializerMethodField(help_text="True for the synthetic tenant or Halo (no network).")
    secret_set_on = serializers.SerializerMethodField(
        help_text="When the client secret was last replaced, or null. The secret itself is never returned."
    )
    revision = serializers.IntegerField(read_only=True, help_text="Goes up when a sign-in value or the secret changes.")
    tested_revision = serializers.IntegerField(
        read_only=True, allow_null=True, help_text="The revision the last successful test covered."
    )
    test_current = serializers.BooleanField(
        read_only=True, help_text="Tested successfully at the current revision. Connectors run only while true."
    )
    last_test = serializers.SerializerMethodField()
    connectors = serializers.SerializerMethodField(help_text="Connectors that sign in through this connection.")
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def get_simulated(self, c) -> bool:
        return is_simulated(c)  # core/connections.is_simulated: the one test for every kind

    @extend_schema_field(serializers.DateField(allow_null=True))
    def get_secret_set_on(self, c) -> str | None:
        return c.secret_set_at.date().isoformat() if c.secret_set_at else None

    @extend_schema_field(LastTestSerializer(allow_null=True))
    def get_last_test(self, c) -> dict[str, Any] | None:
        return last_test_of(c)

    @extend_schema_field(BriefSerializer(many=True))
    def get_connectors(self, c) -> list[dict[str, str]]:
        return self.context.get("connectors", {}).get(c.id, [])


class MicrosoftConnectionSerializer(_ConnectionSerializer):
    tenant_id = serializers.CharField(read_only=True, help_text="GUID or domain; simulated for the synthetic tenant.")


class HaloConnectionSerializer(_ConnectionSerializer):
    base_url = serializers.CharField(read_only=True)
    scope = serializers.CharField(read_only=True)


class ProbeResultSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    problem = serializers.ChoiceField(["", *PROBLEMS], help_text="Empty on success.")
    problem_label = serializers.CharField()
    code = serializers.CharField(help_text="The provider's error code (an AADSTS code, invalid_client, ...), or empty.")
    detail = serializers.CharField(help_text="What the test found, in Softwarden's words; never a secret or a token.")
    next = serializers.CharField(help_text="What to do next.")


_RESULT_HELP = (
    "This call's outcome: the same as last_test, except problem `changed` when the connection was edited during the "
    "test, which records nothing."
)


class MicrosoftConnectionTestResponseSerializer(MicrosoftConnectionSerializer):
    """The test action's 200: the connection plus `result`."""

    result = ProbeResultSerializer(read_only=True, help_text=_RESULT_HELP)


class HaloConnectionTestResponseSerializer(HaloConnectionSerializer):
    """The test action's 200: the connection plus `result`."""

    result = ProbeResultSerializer(read_only=True, help_text=_RESULT_HELP)


class _ConnectionInputSerializer(serializers.Serializer):
    name = serializers.CharField(required=False, help_text="Unique per organization and kind, 100 characters at most.")
    enabled = serializers.BooleanField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True)
    secret = serializers.CharField(
        required=False,
        write_only=True,
        help_text="The client secret. Required to create; send it only to replace it. Stored encrypted, never returned.",
    )


class MicrosoftConnectionInputSerializer(_ConnectionInputSerializer):
    tenant_id = serializers.CharField(required=False, help_text="Directory (tenant) ID: a GUID or the tenant's domain.")
    client_id = serializers.CharField(required=False, help_text="Application (client) ID, a GUID.")


class HaloConnectionInputSerializer(_ConnectionInputSerializer):
    base_url = serializers.CharField(required=False, help_text="https://yourcompany.haloitsm.com")
    client_id = serializers.CharField(required=False)
    scope = serializers.CharField(required=False, help_text="Default: read:assets.")


# ---- Okta and Google Workspace --------------------------------------------------------------------------------------


class OktaConnectionSerializer(_ConnectionSerializer):
    base_url = serializers.CharField(read_only=True, help_text="The Okta org address, https://{org}.okta.com.")
    key_id = serializers.CharField(read_only=True, help_text="The key's kid in the service app's JWKS, or empty.")
    scope = serializers.CharField(read_only=True, help_text="Read-only Okta scopes, space-separated.")
    secret_set_on = serializers.SerializerMethodField(
        help_text="When the private key was last replaced, or null. The key itself is never returned."
    )

    @extend_schema_field(serializers.DateField(allow_null=True))
    def get_secret_set_on(self, c) -> str | None:
        return c.secret_set_at.date().isoformat() if c.secret_set_at else None


class GoogleWorkspaceConnectionSerializer(_ConnectionSerializer):
    client_id = serializers.CharField(
        source="service_account_client_id",
        read_only=True,
        help_text="The service account's numeric client ID, read from the key file: the ID a super administrator "
        "authorizes under domain-wide delegation.",
    )
    admin_email = serializers.CharField(read_only=True, help_text="The Workspace administrator the account acts as.")
    service_account_email = serializers.CharField(read_only=True, help_text="Read from the key file.")
    secret_set_on = serializers.SerializerMethodField(
        help_text="When the key file was last replaced, or null. The key itself is never returned."
    )

    @extend_schema_field(serializers.DateField(allow_null=True))
    def get_secret_set_on(self, c) -> str | None:
        return c.secret_set_at.date().isoformat() if c.secret_set_at else None


class OktaConnectionTestResponseSerializer(OktaConnectionSerializer):
    """The test action's 200: the connection plus `result`."""

    result = ProbeResultSerializer(read_only=True, help_text=_RESULT_HELP)


class GoogleWorkspaceConnectionTestResponseSerializer(GoogleWorkspaceConnectionSerializer):
    """The test action's 200: the connection plus `result`."""

    result = ProbeResultSerializer(read_only=True, help_text=_RESULT_HELP)


class OktaConnectionInputSerializer(_ConnectionInputSerializer):
    base_url = serializers.CharField(
        required=False,
        help_text="https://yourcompany.okta.com (also .oktapreview.com, .okta-emea.com or a custom domain; not the "
        "-admin address). https://okta.simulated.invalid selects the synthetic Okta.",
    )
    client_id = serializers.CharField(required=False, help_text="The API service app's client ID.")
    key_id = serializers.CharField(
        required=False, allow_blank=True, help_text="The key's kid, when the app has several keys. Optional."
    )
    scope = serializers.CharField(
        required=False, help_text="Read-only Okta scopes. Default: okta.apps.read (okta.users.read also accepted)."
    )
    secret = serializers.CharField(
        required=False,
        write_only=True,
        help_text="The service app's private key, PEM or JWK (RSA 2048+ or EC P-256). Required to create; send it only "
        "to replace it. Stored encrypted, never returned.",
    )


class GoogleWorkspaceConnectionInputSerializer(_ConnectionInputSerializer):
    admin_email = serializers.CharField(
        required=False,
        help_text="The Workspace administrator the service account acts as (domain-wide delegation). An address in "
        "google.simulated.invalid selects the synthetic Workspace.",
    )
    secret = serializers.CharField(
        required=False,
        write_only=True,
        help_text="The service account key file (JSON) as Google Cloud downloads it. Only its private key and key ID "
        "are kept, encrypted, never returned; its client_email and client_id are shown. Required to create.",
    )
