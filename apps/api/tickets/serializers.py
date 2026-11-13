"""Serializers for ticket links and ticket connections. They document the shapes (OpenAPI); tickets/services.py
validates every write and tickets/reads.py renders links. The connection secret is write-only: no output field
holds it, only `secret_set_on`."""

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.connection_serializers import LastTestSerializer, ProbeResultSerializer, last_test_of
from core.drf import ReadOnlySerializer, UserBriefSerializer

from .clients import is_simulated
from .models import CONNECTED_SYSTEMS, StatusSource, TicketStatus, TicketSystem

SYSTEMS = [s.value for s in CONNECTED_SYSTEMS]


class TicketLinkSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    system = serializers.ChoiceField(TicketSystem.choices)
    system_label = serializers.CharField()
    external_key = serializers.CharField(help_text="ITOPS-123, INC0012345 or a Halo ticket number; empty if unknown.")
    url = serializers.CharField(help_text="The ticket's https address. Shown and linked; Softwarden never fetches it.")
    title = serializers.CharField()
    status = serializers.ChoiceField(TicketStatus.choices)
    status_label = serializers.CharField()
    status_source = serializers.ChoiceField(
        StatusSource.choices, help_text="manual: set by an Editor; refreshed: read through the ticket connection."
    )
    external_status = serializers.CharField(help_text="The system's own status name, e.g. In Review; may be empty.")
    status_checked_at = serializers.DateTimeField(allow_null=True)
    status_error = serializers.CharField(help_text="Why the last read failed, in Softwarden's words; empty on success.")
    cycle = serializers.UUIDField(allow_null=True, help_text="A renewal's cycle the ticket is about, or null.")
    created_in_system = serializers.BooleanField(help_text="Softwarden created this ticket.")
    created_by = UserBriefSerializer(allow_null=True)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class TicketLinkInputSerializer(serializers.Serializer):
    """Create needs url. The system and key are parsed from the URL unless given."""

    url = serializers.CharField(required=False)
    title = serializers.CharField(required=False, allow_blank=True)
    system = serializers.ChoiceField(TicketSystem.choices, required=False)
    external_key = serializers.CharField(required=False, allow_blank=True)
    status = serializers.ChoiceField(
        TicketStatus.choices,
        required=False,
        allow_null=True,
        help_text="Set by hand. null: read it from the system again (unknown until the next read).",
    )
    cycle = serializers.UUIDField(required=False, allow_null=True, help_text="Renewals only: one of its cycles.")


class ParsedUrlSerializer(serializers.Serializer):
    system = serializers.ChoiceField(TicketSystem.choices)
    system_label = serializers.CharField()
    external_key = serializers.CharField()


class TicketSystemSerializer(serializers.Serializer):
    system = serializers.ChoiceField(SYSTEMS)
    label = serializers.CharField()  # type: ignore[assignment]  # the API field, not Field.label
    reads = serializers.BooleanField(help_text="Its ticket connection is usable: statuses are read through it.")
    creates = serializers.BooleanField(help_text="Editors and Admins can create tickets in it from here.")


class HaloConnectionRefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class CreateTicketInputSerializer(serializers.Serializer):
    system = serializers.ChoiceField(SYSTEMS)
    title = serializers.CharField(required=False, help_text="Default: the assessment's title or 'Renewal: <name>'.")
    description = serializers.CharField(required=False, help_text="Default: a line linking back to Softwarden.")
    cycle = serializers.UUIDField(required=False, allow_null=True, help_text="Renewals only: one of its cycles.")


KEPT = " Null on a connection that isn't `available`."


class TicketConnectionSerializer(ReadOnlySerializer):
    """A connection that isn't `available` (kept from when the installation's preview features were on) shows only
    its id, system, name, `available` and timestamps: every setting is null (WITHHELD), as its detail route refuses
    them (403)."""

    id = serializers.UUIDField(read_only=True)
    system = serializers.ChoiceField(SYSTEMS, read_only=True)
    name = serializers.CharField(read_only=True)
    available = serializers.SerializerMethodField(
        help_text="The organization can use this system now. false: kept from when the installation's preview "
        "features were on (ServiceNow, Halo); never read or written to, it can only be deleted, and its settings "
        "are null."
    )
    enabled = serializers.BooleanField(read_only=True, allow_null=True, help_text=KEPT.strip())
    notes = serializers.CharField(read_only=True, allow_null=True, help_text=KEPT.strip())
    base_url = serializers.CharField(
        read_only=True, allow_null=True, help_text="Jira and ServiceNow; empty for Halo." + KEPT
    )
    username = serializers.CharField(
        read_only=True, allow_null=True, help_text="Jira: the account email; ServiceNow: the user name." + KEPT
    )
    halo_connection = serializers.SerializerMethodField(
        help_text="Halo: the Halo connection it signs in through, else null." + KEPT
    )
    create_enabled = serializers.BooleanField(
        read_only=True, allow_null=True, help_text="Editors may create tickets through it." + KEPT
    )
    create_target = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="Jira project key, ServiceNow table or Halo ticket type ID for new tickets." + KEPT,
    )
    create_issue_type = serializers.CharField(
        read_only=True, allow_null=True, help_text="Jira issue type for new tickets." + KEPT
    )
    simulated = serializers.SerializerMethodField(help_text="True for a synthetic system (no network)." + KEPT)
    secret_set_on = serializers.SerializerMethodField(
        help_text="When the API token or password was last replaced, or null. It is never returned."
    )
    revision = serializers.IntegerField(read_only=True, allow_null=True, help_text=KEPT.strip())
    tested_revision = serializers.IntegerField(read_only=True, allow_null=True)
    test_current = serializers.BooleanField(
        read_only=True,
        allow_null=True,
        help_text="Tested successfully at the current revision. Statuses are read only while true." + KEPT,
    )
    last_test = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    # Everything but id, system, name, available and the timestamps.
    WITHHELD = (
        "enabled",
        "notes",
        "base_url",
        "username",
        "halo_connection",
        "create_enabled",
        "create_target",
        "create_issue_type",
        "simulated",
        "secret_set_on",
        "revision",
        "tested_revision",
        "test_current",
        "last_test",
    )

    def to_representation(self, instance) -> dict[str, Any]:
        data = super().to_representation(instance)
        if not data["available"]:
            data.update(dict.fromkeys(self.WITHHELD))
        return data

    def get_available(self, c) -> bool:
        systems = self.context.get("available_systems")  # the viewset's page context
        if systems is None:
            from .services import system_available

            return system_available(c.organization, c.system)
        return c.system in systems

    @extend_schema_field(HaloConnectionRefSerializer(allow_null=True))
    def get_halo_connection(self, c) -> dict[str, str] | None:
        h = c.halo_connection
        return {"id": str(h.id), "name": h.name} if h is not None else None

    @extend_schema_field(serializers.BooleanField(allow_null=True))
    def get_simulated(self, c) -> bool:
        return is_simulated(c.halo_connection.base_url if c.halo_connection else c.base_url)

    @extend_schema_field(serializers.DateField(allow_null=True))
    def get_secret_set_on(self, c) -> str | None:
        return c.secret_set_at.date().isoformat() if c.secret_set_at else None

    @extend_schema_field(LastTestSerializer(allow_null=True))
    def get_last_test(self, c) -> dict[str, Any] | None:
        return last_test_of(c)


class TicketConnectionTestResponseSerializer(TicketConnectionSerializer):
    result = ProbeResultSerializer(read_only=True)


class TicketConnectionInputSerializer(serializers.Serializer):
    system = serializers.ChoiceField(SYSTEMS, required=False, help_text="Required to create; can't be changed.")
    name = serializers.CharField(required=False)
    enabled = serializers.BooleanField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True)
    base_url = serializers.CharField(required=False, help_text="Jira and ServiceNow: the site's https address.")
    username = serializers.CharField(required=False)
    secret = serializers.CharField(
        required=False,
        write_only=True,
        help_text="Jira API token or ServiceNow password. Required to create either; send it only to replace it. "
        "Stored encrypted, never returned. Not used for Halo.",
    )
    halo_connection = serializers.UUIDField(required=False, help_text="Halo: the Halo connection's ID.")
    create_enabled = serializers.BooleanField(required=False)
    create_target = serializers.CharField(required=False, allow_blank=True)
    create_issue_type = serializers.CharField(required=False, allow_blank=True)
