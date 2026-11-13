"""Serializers for the discovery viewsets (discovery/viewsets.py). Output serializers render
connectors, runs, observations and a record's linked sources, read-only (core/drf.ReadOnlySerializer); input
serializers only describe request bodies, because discovery/engine.py validates every write and owns its messages.

A connector's secret is write-only: no serializer here has a field for it, only `secret_set_on`.

Per-page facts (each connector's last run and candidate counts, an observation's suggested owners and matches, a
record's curated values) are computed once by the viewset and passed in the serializer context."""

from datetime import date
from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from catalog.models import Lifecycle
from catalog.serializers import RecordBriefSerializer
from core.capabilities import SOURCE_CAPABILITY, has_capability
from core.connection_serializers import LastTestSerializer, last_test_of
from core.connections import KINDS as CONNECTION_KINDS
from core.connections import is_simulated
from core.drf import BriefSerializer, ReadOnlySerializer, UserBriefSerializer, represent

from . import engine
from .assistant_serializers import AssistantSuggestionSerializer, suggestion_data
from .models import (
    ConnectorSchedule,
    ConsentType,
    CredentialOrigin,
    CredentialType,
    DecisionKind,
    GoneResolution,
    RunStatus,
    RunTrigger,
    Source,
)

OBSERVATION_STATUSES = list(engine.STATUSES)


class ScopeSerializer(serializers.Serializer):
    """Each source reads the keys it declares (its SCOPE_DEFAULTS); a key another source declares is refused."""

    exclude_microsoft = serializers.BooleanField(
        required=False, help_text="Entra and shadow IT: skip apps Microsoft publishes into every tenant (first-party)."
    )
    exclude_disabled = serializers.BooleanField(required=False, help_text="Entra: skip disabled service principals.")
    name_exclusions = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Entra, shadow IT, Azure, Power Platform, Halo, Okta, Google Workspace: name prefixes to skip.",
    )
    first_party_tenants = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Entra and shadow IT: tenant IDs whose apps count as Microsoft first-party (appOwnerOrganizationId). Default: "
        "f8cdef31-a31e-4b4a-93e4-5f571e91255a (Microsoft Services, on Microsoft Learn) and "
        "72f988bf-86f1-41af-91ab-2d7cd011db47 (Microsoft's corporate tenant, not on Learn).",
    )
    include_agent_identities = serializers.BooleanField(
        required=False,
        help_text="Entra: also list Entra Agent ID agent identities (optional AgentIdentity.Read.All). Default off.",
    )
    sku_exclusions = serializers.ListField(
        child=serializers.CharField(), required=False, help_text="Entra licenses: SKU part number prefixes to skip."
    )
    subscriptions = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Azure: subscription IDs to read. Empty (the default): every subscription the app has Reader on, "
        "so Reader on a management group covers the subscriptions under it.",
    )
    include_logic_apps = serializers.BooleanField(required=False, help_text="Azure: Logic Apps. Default on.")
    include_function_apps = serializers.BooleanField(required=False, help_text="Azure: Function Apps. Default on.")
    include_app_services = serializers.BooleanField(
        required=False, help_text="Azure: App Services (web apps that are not function apps). Default off."
    )
    include_runbooks = serializers.BooleanField(required=False, help_text="Azure: Automation runbooks. Default on.")
    environment_urls = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Power Platform: Dataverse environment URLs (https://<name>.crm[N].dynamics.com) to read; least "
        "privilege, the default way.",
    )
    all_environments = serializers.BooleanField(
        required=False,
        help_text="Power Platform: also list every environment through the Power Platform admin API. Opt-in: the "
        "app's registration for it is admin-equivalent. Default off.",
    )
    include_cloud_flows = serializers.BooleanField(
        required=False, help_text="Power Platform: also read cloud flows (modern flows, category 5). Default off."
    )
    asset_types = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Halo: only these asset types (case-insensitive). Empty (the default): every type.",
    )
    exclude_inactive = serializers.BooleanField(
        required=False, help_text="Halo: skip inactive assets. Okta: skip inactive apps. Default on."
    )
    exclude_okta_apps = serializers.BooleanField(
        required=False,
        help_text="Okta: skip Okta's own apps (Admin Console, Dashboard, Browser Plugin). Default on.",
    )
    exclude_bookmarks = serializers.BooleanField(
        required=False, help_text="Okta: skip bookmark apps (a link, no sign-in). Default off."
    )
    assignment_counts = serializers.BooleanField(
        required=False,
        help_text="Okta: count each kept app's assigned users (GET /api/v1/apps/{id}/users, okta.apps.read). Default "
        "on.",
    )
    days = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=180,
        help_text="Google Workspace: the reporting window in days, 1 to 180 (Google keeps 180). Default 30.",
    )
    min_users = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=100_000,
        help_text="Google Workspace: skip apps fewer people than this authorized in the window. Default 1.",
    )
    sign_ins = serializers.BooleanField(
        required=False,
        help_text="Shadow IT: read the Entra sign-in logs (AuditLog.Read.All; the tenant needs Entra ID P1 or P2) to "
        "count users and sign-ins. Off: consent grants only. Default on.",
    )
    provisioning = serializers.BooleanField(
        required=False,
        help_text="SSO and MFA coverage: read each linked enterprise app's provisioning (SCIM) jobs "
        "(Synchronization.Read.All). Off: provisioning is reported unknown. Default on.",
    )
    conditional_access = serializers.BooleanField(
        required=False,
        help_text="SSO and MFA coverage: read conditional access policies (Policy.Read.All; the tenant needs Entra ID "
        "P1) to tell which apps require MFA. Off: MFA is reported unknown. Default on.",
    )
    sign_in_activity = serializers.BooleanField(
        required=False,
        help_text="License usage: read each licensed user's last successful sign-in (AuditLog.Read.All; the tenant "
        "needs Entra ID P1). Off: licenses are counted and the last sign-in is unknown. Default on.",
    )
    service_account_prefixes = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="License usage: user principal name prefixes (such as svc-) of service and shared accounts, counted "
        "apart and never as waste. Lower-cased.",
    )
    excluded_users = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="License usage: user object IDs to count as service accounts (Entra has no flag for them).",
    )


class PermissionSerializer(serializers.Serializer):
    name = serializers.CharField(
        help_text="A Microsoft Graph application permission, an Azure role, a Dataverse privilege, the Power Platform "
        "management app registration, a Halo scope, an Okta scope or admin role, or a Google scope (through "
        "domain-wide delegation) or admin privilege (see kind)."
    )
    kind = serializers.ChoiceField(
        [
            "graph",
            "azure_rbac",
            "dataverse_privilege",
            "power_platform_registration",
            "halo_scope",
            "okta_scope",
            "okta_admin_role",
            "google_scope",
            "google_admin_role",
        ]
    )
    role_id = serializers.CharField(
        help_text="Its app role or role definition ID; empty until confirmed on Microsoft Learn (or not applicable)."
    )
    required = serializers.BooleanField(  # type: ignore[assignment]  # the field, not Field.required
        help_text="False: optional; the check reports it but doesn't fail."
    )
    why = serializers.CharField()
    verified = serializers.BooleanField(
        help_text="The grant was confirmed on Microsoft Learn for these calls (false: unverified, or not Microsoft)."
    )


class AccessStepSerializer(serializers.Serializer):
    group = serializers.ChoiceField(["credentials", "api"])
    name = serializers.CharField()
    permission = serializers.CharField(help_text="The permission this step proves; empty for sign-in steps.")
    required = serializers.BooleanField()  # type: ignore[assignment]  # the field, not Field.required
    state = serializers.ChoiceField(
        ["ok", "failed", "optional_missing", "warning", "not_used", "not_checked", "skipped"],
        help_text="not_used: the scope does not use it; not_checked: past the check's cap of 10 subscriptions or "
        "environments (a run reports each).",
    )
    problem = serializers.CharField(help_text="missing_permission, wrong_credentials, unreachable, ... or empty.")
    problem_label = serializers.CharField()
    code = serializers.CharField(
        help_text="The service's error code (Graph, ARM, Dataverse, Halo) or an AADSTS code; never its message text."
    )
    detail = serializers.CharField()
    next = serializers.CharField(help_text="What to do next.")


class AccessCheckSerializer(serializers.Serializer):
    ok = serializers.BooleanField(help_text="Every required permission works. Optional ones may be missing.")
    missing = serializers.ListField(child=serializers.CharField(), help_text="Required permissions not granted.")
    optional_missing = serializers.ListField(
        child=serializers.CharField(), help_text="Optional permissions not granted (reported, not failing)."
    )
    steps = AccessStepSerializer(many=True)
    checked_at = serializers.DateTimeField()
    checked_by = UserBriefSerializer(allow_null=True)


class RunConnectorSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    source = serializers.ChoiceField(Source.choices, read_only=True)  # type: ignore[assignment]  # the field, not Field.source


class SkippedSerializer(serializers.Serializer):
    filter = serializers.CharField(help_text="The filter's key, as in counts.skipped.")
    label = serializers.CharField(help_text="The filter in words (the connector's `filters`).")  # type: ignore[assignment]  # the API field, not Field.label
    count = serializers.IntegerField()


class RunSerializer(ReadOnlySerializer):
    """One connector run. Reads the run's connector (selected with it), never a secret: `error` and
    `notes` are Softwarden's words, with any secret removed before they were stored."""

    id = serializers.UUIDField(read_only=True)
    connector = RunConnectorSerializer(read_only=True)
    trigger = serializers.ChoiceField(
        RunTrigger.choices, read_only=True, help_text="manual (Run now) or scheduled (the daily schedule)."
    )
    status = serializers.ChoiceField(
        RunStatus.choices,
        read_only=True,
        help_text="queued (waiting for the worker), running, succeeded or failed (a run that lost its lease is failed "
        "with the reason in error).",
    )
    queued_at = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="When Run now or the schedule queued it."
    )
    started_at = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="Null while queued, or if it never started."
    )
    finished_at = serializers.DateTimeField(read_only=True, allow_null=True)
    duration_seconds = serializers.SerializerMethodField(help_text="From start to finish; null until it finishes.")
    requested_by = UserBriefSerializer(read_only=True, allow_null=True)
    counts = serializers.DictField(
        read_only=True,
        help_text="seen, kept, skipped {filter: n}, new, changed, unchanged, gone; when present out_of_scope, not_read, "
        "returned, differs (open source differences), credentials, notes; Entra licenses: pools_synced, pools_held, "
        "pools_to_review, unbound; license usage: licensed_users, users_removed, linked, guests, disabled, "
        "service_accounts, sign_in_unknown, tenant_key (a hash naming the tenant read, not its ID)",
    )
    skipped = serializers.SerializerMethodField(help_text="counts.skipped with each filter's label, largest first.")
    notes = serializers.SerializerMethodField(help_text="What the run said without failing (counts.notes).")
    error = serializers.CharField(
        read_only=True, help_text="A readable reason the run failed; never contains a secret."
    )
    failure_code = serializers.ChoiceField(
        ["", "empty_listing"],
        read_only=True,
        help_text="Why a failed run failed, when it has a code; empty otherwise. `empty_listing`: the empty-listing "
        "guard refused it (a listing came back empty while the connector has live observations); an Admin's run "
        "with accept_empty_listing gets through.",
    )
    job_id = serializers.UUIDField(
        read_only=True, allow_null=True, help_text="The discovery.run job that started it; null for in-process runs."
    )

    def get_duration_seconds(self, r) -> int | None:
        if r.started_at is None or r.finished_at is None:
            return None
        return max(int((r.finished_at - r.started_at).total_seconds()), 0)

    @extend_schema_field(SkippedSerializer(many=True))
    def get_skipped(self, r) -> list[dict[str, Any]]:
        skipped = (r.counts or {}).get("skipped") or {}
        if not isinstance(skipped, dict):
            return []
        labels = getattr(engine.SOURCES.get(r.connector.source), "FILTERS", {})
        rows = [{"filter": k, "label": labels.get(k, k), "count": n} for k, n in skipped.items() if isinstance(n, int)]
        return sorted(rows, key=lambda row: (-row["count"], row["filter"]))

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_notes(self, r) -> list[str]:
        notes = (r.counts or {}).get("notes") or []
        return [str(n) for n in notes] if isinstance(notes, list) else []


def connection_of(c):
    """The connector's connection, from the row loaded with it (the viewset selects it)."""
    for key in CONNECTION_KINDS:
        if getattr(c, f"{key}_connection_id"):
            return getattr(c, f"{key}_connection")
    return None


class ConnectionBriefSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    kind = serializers.SerializerMethodField()
    name = serializers.CharField(read_only=True)
    enabled = serializers.BooleanField(read_only=True)
    revision = serializers.IntegerField(read_only=True)
    tested_revision = serializers.IntegerField(read_only=True, allow_null=True)
    test_current = serializers.BooleanField(
        read_only=True, help_text="Tested successfully at its current revision; runs are refused otherwise."
    )
    last_test = serializers.SerializerMethodField(
        help_text="The connection's last test (when, passed or not, who, what it found), or null when it was never "
        "tested. With test_current false: null is “not tested”, a passed one is “changed since its last test”."
    )

    @extend_schema_field(serializers.ChoiceField(list(CONNECTION_KINDS)))
    def get_kind(self, c) -> str:
        from core.connections import kind_of

        return kind_of(c).key

    @extend_schema_field(LastTestSerializer(allow_null=True))
    def get_last_test(self, c) -> dict[str, Any] | None:
        return last_test_of(c)


class CandidateCountsSerializer(serializers.Serializer):
    new = serializers.IntegerField()
    changed = serializers.IntegerField()


class ConnectorSerializer(ReadOnlySerializer):
    """Context: `last_runs` and `active_runs` {connector id: ConnectorRun} and `counts` {connector id: (new,
    changed)}."""

    id = serializers.UUIDField(read_only=True)
    source = serializers.ChoiceField(Source.choices, read_only=True)  # type: ignore[assignment]  # the field, not Field.source
    source_label = serializers.CharField(source="get_source_display", read_only=True)
    name = serializers.CharField(read_only=True)
    enabled = serializers.BooleanField(read_only=True)
    available = serializers.SerializerMethodField(
        help_text="The organization has the source's capability. When false (an edition without it, or Okta and "
        "Google Workspace while the installation's preview features are off) the connector is kept as it is but "
        "never runs: no run, schedule or access check, and no change other than turning it off."
    )
    connection = serializers.SerializerMethodField(
        help_text="The connection the connector signs in through, or null. A run needs test_current."
    )
    tenant_id = serializers.SerializerMethodField(
        help_text="The connection's tenant: GUID or domain; simulated for the synthetic tenant."
    )
    client_id = serializers.SerializerMethodField(help_text="The connection's application (client) ID.")
    simulated = serializers.SerializerMethodField(
        help_text="True on the synthetic tenant (or the synthetic Halo, Okta or Workspace) used before a real one is connected."
    )
    scope = serializers.SerializerMethodField()
    filters = serializers.SerializerMethodField(help_text="Filter key to label, for run counts.")
    schedule = serializers.ChoiceField(ConnectorSchedule.choices, read_only=True)
    reviewer = UserBriefSerializer(read_only=True, allow_null=True)
    secret_set_on = serializers.SerializerMethodField(
        help_text="When the connection's secret was last replaced. The secret itself is never returned."
    )
    run_requested = serializers.SerializerMethodField(
        help_text="A run is queued for the worker (Run now or the schedule)."
    )
    last_run = serializers.SerializerMethodField(
        help_text="The newest run that is neither queued nor running (active_run has those), started or not, or null."
    )
    active_run = serializers.SerializerMethodField(
        help_text="The queued or running run, or null: poll connectors/{id}/runs/{run id} for its progress."
    )
    candidates = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField(
        help_text="The grants this source uses: Graph permissions, Azure roles, Dataverse privileges, Halo scopes."
    )
    access_check = serializers.SerializerMethodField(help_text="The last access check, or null.")

    def get_available(self, c) -> bool:
        available = self.context.get("available_sources")  # the connector viewset's page context
        if available is None:
            capability = SOURCE_CAPABILITY.get(c.source)
            return capability is not None and has_capability(c.organization, capability)
        return c.source in available

    @extend_schema_field(ConnectionBriefSerializer(allow_null=True))
    def get_connection(self, c) -> dict[str, Any] | None:
        return represent(ConnectionBriefSerializer, connection_of(c))

    def get_tenant_id(self, c) -> str:
        return getattr(connection_of(c), "tenant_id", "")

    def get_client_id(self, c) -> str:
        connection = connection_of(c)  # a Google Workspace connection shows its service account's client ID
        return getattr(connection, "client_id", "") or getattr(connection, "service_account_client_id", "")

    def get_simulated(self, c) -> bool:
        return is_simulated(connection_of(c))

    @extend_schema_field(ScopeSerializer)
    def get_scope(self, c) -> dict[str, Any]:
        return dict(engine.module(c).SCOPE_DEFAULTS) | dict(c.scope)

    @extend_schema_field(serializers.DictField(child=serializers.CharField()))
    def get_filters(self, c) -> dict[str, str]:
        return engine.module(c).FILTERS

    @extend_schema_field(serializers.DateField(allow_null=True))
    def get_secret_set_on(self, c) -> str | None:
        connection = connection_of(c)
        return connection.secret_set_at.date().isoformat() if connection and connection.secret_set_at else None

    def get_run_requested(self, c) -> bool:
        active = self.context["active_runs"].get(c.id)
        return c.run_requested_at is not None or (active is not None and active.status == RunStatus.QUEUED)

    @extend_schema_field(RunSerializer(allow_null=True))
    def get_last_run(self, c) -> dict[str, Any] | None:
        run = self.context["last_runs"].get(c.id)
        return represent(RunSerializer, run)

    @extend_schema_field(RunSerializer(allow_null=True))
    def get_active_run(self, c) -> dict[str, Any] | None:
        return represent(RunSerializer, self.context["active_runs"].get(c.id))

    @extend_schema_field(CandidateCountsSerializer)
    def get_candidates(self, c) -> dict[str, int]:
        new, changed = self.context["counts"].get(c.id, (0, 0))
        return {"new": new, "changed": changed}

    @extend_schema_field(PermissionSerializer(many=True))
    def get_permissions(self, c) -> list[dict[str, Any]]:
        return [p.as_dict() for p in getattr(engine.module(c), "PERMISSIONS", [])]

    @extend_schema_field(AccessCheckSerializer(allow_null=True))
    def get_access_check(self, c) -> dict[str, Any] | None:
        if c.access_checked_at is None or not c.access_check:
            return None
        return {
            **{k: c.access_check.get(k) for k in ("ok", "missing", "optional_missing", "steps")},
            "checked_at": serializers.DateTimeField().to_representation(c.access_checked_at),
            "checked_by": represent(UserBriefSerializer, c.access_checked_by),
        }


class ConnectorInputSerializer(serializers.Serializer):
    source = serializers.ChoiceField(Source.choices, required=False)  # type: ignore[assignment]
    name = serializers.CharField(required=False)
    enabled = serializers.BooleanField(required=False)
    microsoft_connection = serializers.UUIDField(
        required=False,
        help_text="The Microsoft connection a Microsoft source (entra, entra_licenses, azure, powerplatform) signs in "
        "through.",
    )
    halo_connection = serializers.UUIDField(
        required=False, help_text="The Halo connection the halo source signs in through."
    )
    okta_connection = serializers.UUIDField(
        required=False, help_text="The Okta connection the okta source signs in through."
    )
    google_workspace_connection = serializers.UUIDField(
        required=False,
        help_text="The Google Workspace connection the google_workspace source signs in through.",
    )
    tenant_id = serializers.CharField(
        required=False,
        help_text="Legacy: creates or changes the connector's own Microsoft connection. GUID or domain; simulated for "
        "the synthetic tenant.",
    )
    client_id = serializers.CharField(required=False, help_text="Legacy, as tenant_id.")
    secret = serializers.CharField(
        required=False,
        write_only=True,
        help_text="Legacy, as tenant_id. Write-only; stored encrypted on the connection, never returned.",
    )
    scope = ScopeSerializer(required=False)
    schedule = serializers.ChoiceField(ConnectorSchedule.choices, required=False)
    reviewer = serializers.IntegerField(required=False, allow_null=True, help_text="An active Admin or Editor.")


# ---- observations ---------------------------------------------------------------------------------------------------


class PersonMatchSerializer(BriefSerializer):
    active = serializers.BooleanField(read_only=True)


class SuggestedOwnerSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="Empty when the source couldn't name the owner (resolved false).")
    email = serializers.CharField()
    external_id = serializers.CharField(help_text="The owner's Entra object ID.")
    resolved = serializers.BooleanField(
        help_text="False: the named gap \u201cowner not resolved\u201d (the app lacks the optional User.ReadBasic.All)."
    )
    person = PersonMatchSerializer(
        allow_null=True,
        help_text="The Person whose user signs in with this Entra object ID, else the Person with this email, if any. "
        "Never applied.",
    )


class ObservedCredentialSerializer(ReadOnlySerializer):
    """A client secret or certificate seen on the observed app. Never key material or a secret."""

    id = serializers.UUIDField(read_only=True)
    key_id = serializers.CharField(read_only=True)
    credential_type = serializers.ChoiceField(CredentialType.choices, read_only=True)
    display_name = serializers.CharField(read_only=True)
    origin = serializers.ChoiceField(CredentialOrigin.choices, read_only=True)
    expires_on = serializers.DateField(read_only=True, allow_null=True)
    removed_at = serializers.DateTimeField(
        read_only=True, allow_null=True, help_text="Set once a run no longer sees it."
    )


class DecisionSerializer(ReadOnlySerializer):
    decision = serializers.ChoiceField(DecisionKind.choices, read_only=True)
    reason = serializers.CharField(read_only=True)
    decided_by = UserBriefSerializer(read_only=True, allow_null=True)
    decided_at = serializers.DateTimeField(source="created_at", read_only=True)
    record = RecordBriefSerializer(source="resource", read_only=True, allow_null=True)
    gone_resolution = serializers.ChoiceField(
        [*GoneResolution.values, ""],
        read_only=True,
        help_text="No longer seen in source: kept or archived once someone resolved it; empty otherwise. "
        "Cleared when the object comes back.",
    )
    gone_acknowledged_at = serializers.DateTimeField(read_only=True, allow_null=True)
    gone_acknowledged_by = UserBriefSerializer(read_only=True, allow_null=True)
    gone_note = serializers.CharField(read_only=True)


class MatchSerializer(RecordBriefSerializer):
    score = serializers.FloatField(
        read_only=True,
        help_text="0 to 1: name similarity (pg_trgm similarity or word similarity); 1 for the same name.",
    )


class DifferenceSerializer(serializers.Serializer):
    field = serializers.CharField(help_text="The record field: name, description, documentation_url, entra_app_id, ...")
    label = serializers.CharField()  # type: ignore[assignment]  # the API field is called label
    curated = serializers.CharField(help_text="The record's value now (empty when not set).")
    source = serializers.CharField(help_text="What the source says.")  # type: ignore[assignment]
    since = serializers.DateTimeField(help_text="When the source started saying this (a new source value restarts it).")
    ignored = serializers.BooleanField(help_text="Someone chose to keep the record's value until the source changes.")
    ignored_at = serializers.DateTimeField(allow_null=True)
    ignored_by = UserBriefSerializer(allow_null=True)


def difference_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(DifferenceSerializer(e).data) for e in entries]


class ObservationSerializer(ReadOnlySerializer):
    """Context: `owners` {observation id: [suggested owners]} and `matches` {observation id: [records]}
    (discovery/engine.py), computed once per page."""

    id = serializers.UUIDField(read_only=True)
    connector = BriefSerializer(read_only=True)
    kind = serializers.CharField(read_only=True)
    external_id = serializers.CharField(read_only=True)
    name = serializers.CharField(source="display_name", read_only=True)
    app_id = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    homepage = serializers.SerializerMethodField()
    publisher = serializers.SerializerMethodField()
    enabled = serializers.SerializerMethodField()
    ai_agent = serializers.BooleanField(read_only=True)
    source = serializers.CharField(source="connector.source", read_only=True)  # type: ignore[assignment]
    component_type = serializers.CharField(
        source="suggested_component_type",
        read_only=True,
        help_text="For a component candidate: the component type it suggests (logic_app, function, service, runbook, "
        "automation_flow); empty otherwise.",
    )
    details = serializers.SerializerMethodField(
        help_text="Source-specific values (Azure: resource_id, subscription, location, state, site kind, tags; Power "
        "Platform: environment, state, authentication; Halo: halo_asset_id, asset_type, ...; Okta: okta_app_id, "
        "sign_on_mode, assigned_users, ...; Google Workspace: client_id, users, grants, scopes, risky_scopes, ...). "
        "Never a secret, and never a person's email or ID."
    )
    agent = serializers.SerializerMethodField(
        help_text="copilot_studio (Copilot Studio tags), agent_identity (Entra Agent ID) or empty."
    )
    registration = serializers.SerializerMethodField(help_text="An app registration in this tenant was seen.")
    enterprise_app = serializers.SerializerMethodField(
        help_text="An enterprise application (service principal) was seen."
    )
    credentials = serializers.SerializerMethodField(
        help_text="Client secrets and certificates seen on it, live ones first by end date."
    )
    first_seen_at = serializers.DateTimeField(read_only=True)
    last_seen_at = serializers.DateTimeField(read_only=True)
    last_changed_at = serializers.DateTimeField(read_only=True)
    gone_at = serializers.DateTimeField(read_only=True, allow_null=True)
    out_of_scope = serializers.CharField(
        read_only=True,
        help_text="The connector's filter that leaves this object out (a key of the connector's `filters`), or empty: "
        "the source still has it, so it keeps its state and is never “no longer seen”.",
    )
    out_of_scope_label = serializers.SerializerMethodField()
    out_of_scope_at = serializers.DateTimeField(read_only=True, allow_null=True)
    status = serializers.SerializerMethodField()
    decision = serializers.SerializerMethodField()
    suggested_owners = serializers.SerializerMethodField()
    suggested_matches = serializers.SerializerMethodField(
        help_text="Up to 3 live records of compatible kinds with a similar name, best first, each with a score; only "
        "while the observation is new or changed (GET .../suggestions for more)."
    )
    differences = serializers.SerializerMethodField(
        help_text="Linked or created: the record fields the source differs on, ignored ones included (marked), never "
        "applied on their own. Empty otherwise, and while the observation is gone."
    )
    missing = serializers.SerializerMethodField(
        help_text="Linked or created, no longer seen in source and not yet resolved (kept or archived)."
    )
    assistant_suggestion = serializers.SerializerMethodField(
        help_text="The assistant's newest suggestion for this candidate (capability assistant): link, "
        "create or dismiss with confidence and reason, or why there is none, and what was decided on it. Null without "
        "the capability or before any suggestion. Never applied on its own: Accept or decide as usual."
    )

    def get_app_id(self, o) -> str:
        return o.data.get("app_id", "")

    def get_description(self, o) -> str:
        return o.data.get("description", "")

    def get_homepage(self, o) -> str:
        return o.data.get("homepage", "")

    def get_publisher(self, o) -> str:
        return o.data.get("publisher", "")

    def get_enabled(self, o) -> bool:
        return o.data.get("enabled", True)

    @extend_schema_field(serializers.ChoiceField(["", "copilot_studio", "agent_identity"]))
    def get_agent(self, o) -> str:
        return o.data.get("agent", "")

    def get_registration(self, o) -> bool:
        return bool(o.data.get("registration", False))

    def get_enterprise_app(self, o) -> bool:
        return bool(o.data.get("enterprise_app", o.kind != "license" and o.connector.source == "entra"))

    @extend_schema_field(serializers.DictField())
    def get_details(self, o) -> dict[str, Any]:
        keys = getattr(engine.SOURCES.get(o.connector.source), "DETAILS", ())
        return {k: o.data[k] for k in keys if k in o.data}

    @extend_schema_field(ObservedCredentialSerializer(many=True))
    def get_credentials(self, o) -> list[dict[str, Any]]:
        rows = sorted(
            o.credentials.all(),
            key=lambda c: (c.removed_at is not None, c.expires_on is None, c.expires_on or date.max, c.key_id),
        )
        return list(ObservedCredentialSerializer(rows, many=True).data)

    def get_out_of_scope_label(self, o) -> str:
        if not o.out_of_scope:
            return ""
        filters = getattr(engine.SOURCES.get(o.connector.source), "FILTERS", {})
        return str(filters.get(o.out_of_scope, o.out_of_scope))

    @extend_schema_field(serializers.ChoiceField(OBSERVATION_STATUSES))
    def get_status(self, o) -> str:
        return engine.status_of(o)

    @extend_schema_field(DecisionSerializer(allow_null=True))
    def get_decision(self, o) -> dict[str, Any] | None:
        d = getattr(o, "decision", None)
        return represent(DecisionSerializer, d)

    @extend_schema_field(SuggestedOwnerSerializer(many=True))
    def get_suggested_owners(self, o) -> list[dict[str, Any]]:
        return self.context["owners"].get(o.id, [])

    @extend_schema_field(MatchSerializer(many=True))
    def get_suggested_matches(self, o) -> list[dict[str, Any]]:
        return self.context["matches"].get(o.id, []) if engine.status_of(o) in ("new", "changed") else []

    @extend_schema_field(DifferenceSerializer(many=True))
    def get_differences(self, o) -> list[dict[str, Any]]:
        return difference_rows(self.context.get("differences", {}).get(o.id, []))

    @extend_schema_field(AssistantSuggestionSerializer(allow_null=True))
    def get_assistant_suggestion(self, o) -> dict[str, Any] | None:
        return suggestion_data(self.context.get("assistant", {}).get(o.id), o)

    def get_missing(self, o) -> bool:
        d = getattr(o, "decision", None)
        return (
            d is not None
            and d.decision != DecisionKind.DISMISSED
            and o.gone_at is not None
            and d.gone_acknowledged_at is None
        )


class CandidatePageSerializer(serializers.Serializer):
    status = serializers.ChoiceField([*OBSERVATION_STATUSES, *engine.LINKED_FILTERS, "candidates", "all"])
    counts = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="Observations per status (new, changed, linked, dismissed, gone, out_of_scope, candidates) and per linked filter "
        "(missing: linked, no longer seen, unresolved; differs: linked with a source difference nobody ignored) under "
        "the connector and search filters.",
    )
    results = ObservationSerializer(many=True, help_text="By source name, 50 at a time.")
    next = serializers.CharField(
        allow_null=True,
        help_text="An offset: pass back as ?cursor= for the next 50. A row that leaves the list meanwhile (decided) "
        "shifts the rest, so one is skipped; walk with next_after instead. Null on a page asked for with after.",
    )
    next_after = serializers.CharField(
        allow_null=True,
        help_text="Pass back as ?after= for the next 50 after this page's last row (keyset, by source name and id): "
        "stable while rows are decided or added. Null on the last page.",
    )


class LinkInputSerializer(serializers.Serializer):
    resource = serializers.UUIDField(help_text="An application, component or AI agent.")


class CreateFromInputSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(
        ["application", "component", "ai_agent"],
        required=False,
        help_text="Default: ai_agent for an AI agent candidate, else application.",
    )
    name = serializers.CharField(required=False)
    lifecycle = serializers.ChoiceField(Lifecycle.choices, required=False)
    description = serializers.CharField(required=False)
    custom_field_data = serializers.DictField(
        required=False, help_text="Custom field values by name, as on the record's own create (defaults fill the rest)."
    )


class DismissInputSerializer(serializers.Serializer):
    reason = serializers.CharField()


class DriftInputSerializer(serializers.Serializer):
    fields = serializers.ListField(  # type: ignore[assignment]  # the API field is called fields
        child=serializers.CharField(),
        required=False,
        help_text="Which differing fields; default every difference nobody ignored.",
    )
    seen = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        help_text="Required: the source value you reviewed for every chosen field (400 if one is missing); if a run "
        "changed one since, nothing is written (409).",
    )


class RunRequestSerializer(serializers.Serializer):
    """POST connectors/{id}/run's optional body."""

    accept_empty_listing = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Admin: this one run skips the empty-listing guard. Use it when a listing really is empty (the last "
        "resource of a type, or every application, was deleted): its observations go no longer seen and their "
        "discovered credentials are removed. Recorded on the run and the audit event; later runs are guarded again.",
    )


class ReopenInputSerializer(serializers.Serializer):
    archive = serializers.BooleanField(
        required=False,
        help_text="Also archive the record created from this candidate (only a create; refused with 409 if it was "
        "changed since or anything references it). Default false: the record stays.",
    )


class MissingInputSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, help_text="Why, at most 500 characters.")


class DriftSerializer(serializers.Serializer):
    observation = serializers.UUIDField()
    name = serializers.CharField(help_text="The source's name for it.")
    connector = BriefSerializer()
    record = RecordBriefSerializer()
    record_etag = serializers.CharField(
        help_text="The record's ETag now (as GET /applications/{id} gives it), for If-Match on drift/apply."
    )
    differences = DifferenceSerializer(many=True)


class SuggestionsSerializer(serializers.Serializer):
    results = MatchSerializer(many=True)


# ---- A record's discovery sources -------------------------------------------------------------------------------------


class SourceConnectorSerializer(BriefSerializer):
    source_label = serializers.CharField(source="get_source_display", read_only=True)


class SourceValuesSerializer(serializers.Serializer):
    name = serializers.CharField()
    description = serializers.CharField()
    homepage = serializers.CharField()


def source_values(o) -> dict[str, str]:
    return {
        "name": o.display_name,
        "description": o.data.get("description", ""),
        "homepage": o.data.get("homepage", ""),
    }


class DiscoverySourceSerializer(ReadOnlySerializer):
    """One linked or created decision on a record, with the source's values and the curated fields they differ on.
    Context: `differences` {observation id: drift entries} (discovery/drift.py)."""

    observation = serializers.UUIDField(source="observation_id", read_only=True)
    connector = SourceConnectorSerializer(source="observation.connector", read_only=True)
    external_id = serializers.CharField(source="observation.external_id", read_only=True)
    app_id = serializers.SerializerMethodField()
    source_values = serializers.SerializerMethodField()
    differs = serializers.SerializerMethodField(
        help_text="Record fields the source differs on that nobody ignored (the field names of `differences`)."
    )
    differences = serializers.SerializerMethodField(help_text="Every difference, ignored ones marked.")
    enabled = serializers.SerializerMethodField()
    last_seen_at = serializers.DateTimeField(source="observation.last_seen_at", read_only=True)
    gone_at = serializers.DateTimeField(source="observation.gone_at", read_only=True, allow_null=True)
    decision = serializers.ChoiceField([DecisionKind.LINKED, DecisionKind.CREATED], read_only=True)
    decided_by = UserBriefSerializer(read_only=True, allow_null=True)
    decided_at = serializers.DateTimeField(source="created_at", read_only=True)

    def get_app_id(self, d) -> str:
        return d.observation.data.get("app_id", "")

    @extend_schema_field(SourceValuesSerializer)
    def get_source_values(self, d) -> dict[str, str]:
        return source_values(d.observation)

    def get_differs(self, d) -> list[str]:
        return [e["field"] for e in self.context["differences"].get(d.observation_id, []) if not e["ignored"]]

    @extend_schema_field(DifferenceSerializer(many=True))
    def get_differences(self, d) -> list[dict[str, Any]]:
        return difference_rows(self.context["differences"].get(d.observation_id, []))

    def get_enabled(self, d) -> bool:
        return d.observation.data.get("enabled", True)


# ---- discovered credentials ------------------------------------------------------------------------------


class ObservationBriefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    kind = serializers.CharField()
    status = serializers.ChoiceField(OBSERVATION_STATUSES)
    connector = BriefSerializer()


class DiscoveredCredentialSerializer(ObservedCredentialSerializer):
    """A credential with the observed app it belongs to and that app's linked record, if any. Context: `today`."""

    object_id = serializers.CharField(read_only=True, help_text="The app registration's or service principal's ID.")
    days = serializers.SerializerMethodField(help_text="Days until the end date (negative once expired), or null.")
    first_seen_at = serializers.DateTimeField(read_only=True)
    last_seen_at = serializers.DateTimeField(read_only=True)
    observation = serializers.SerializerMethodField()
    record = serializers.SerializerMethodField(help_text="The record the app is linked to or was created as.")

    def get_days(self, c) -> int | None:
        return (c.expires_on - self.context["today"]).days if c.expires_on else None

    @extend_schema_field(ObservationBriefSerializer)
    def get_observation(self, c) -> dict[str, Any]:
        o = c.observation
        return {
            "id": str(o.id),
            "name": o.display_name,
            "kind": o.kind,
            "status": engine.status_of(o),
            "connector": {"id": str(o.connector_id), "name": o.connector.name},
        }

    @extend_schema_field(RecordBriefSerializer(allow_null=True))
    def get_record(self, c) -> dict[str, Any] | None:
        d = getattr(c.observation, "decision", None)
        if d is None or d.resource_id is None:
            return None
        return represent(RecordBriefSerializer, d.resource)


class SourceInfoSerializer(ReadOnlySerializer):
    """A discovery source as `GET discovery/sources` describes it: what it needs before any connector of it
    exists. Nothing here is the organization's data, only whether its edition includes the source."""

    id = serializers.ChoiceField(Source.choices)
    label = serializers.CharField()  # type: ignore[assignment]  # the API field, not Field.label
    capability = serializers.CharField(help_text="The capability the source needs (discovery.entra, ...).")
    available = serializers.BooleanField(help_text="This organization's edition includes the capability.")
    connection_kind = serializers.ChoiceField(list(CONNECTION_KINDS), help_text="The connection its connectors use.")
    permissions = PermissionSerializer(many=True, help_text="The grants it uses, as on a connector's `permissions`.")
    scope_defaults = ScopeSerializer(help_text="The scope a new connector starts with (only the keys it reads).")
    filters = serializers.DictField(
        child=serializers.CharField(), help_text="Filter key to label, as in run counts' skipped."
    )


# ---- shadow IT overview ----------------------------------------------------------------------------------


class ShadowItCandidateSerializer(serializers.Serializer):
    """The observation to decide on for the app: this connector's own candidate, else the Entra source's."""

    id = serializers.UUIDField()
    status = serializers.ChoiceField(OBSERVATION_STATUSES)
    connector = BriefSerializer()
    source = serializers.ChoiceField(Source.choices)  # type: ignore[assignment]  # the API field, not Field.source


class ShadowItAppSerializer(ReadOnlySerializer):
    """One third-party app of the shadow IT overview (discovery/entra_shadow_it.py). Context: `records` {app ID:
    record} and `candidates` {app ID: [observations]}, computed once per page (ShadowItViewSet.page_context). Counts
    only: never who signed in or who consented."""

    id = serializers.UUIDField(read_only=True)
    connector = BriefSerializer(read_only=True)
    app_id = serializers.CharField(read_only=True, help_text="The app's Entra application (client) ID.")
    name = serializers.CharField(source="display_name", read_only=True)
    publisher = serializers.CharField(read_only=True)
    verified_publisher = serializers.CharField(read_only=True, help_text="Entra's verified publisher, or empty.")
    service_principal_id = serializers.CharField(read_only=True)
    users_30d = serializers.IntegerField(
        read_only=True, help_text="Distinct people who signed in successfully over the last 30 days (estimated)."
    )
    sign_ins_30d = serializers.IntegerField(read_only=True, help_text="Successful interactive sign-ins, last 30 days.")
    last_sign_in_at = serializers.DateTimeField(read_only=True, allow_null=True)
    consent = serializers.ChoiceField(
        ConsentType.choices,
        read_only=True,
        help_text="admin (an administrator consented for everyone), user (some people consented for themselves) or "
        "none (no OAuth consent grant).",
    )
    consenting_users = serializers.IntegerField(read_only=True, help_text="How many people consented (never who).")
    scopes = serializers.ListField(child=serializers.CharField(), read_only=True)
    risky_scopes = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        help_text="The high-risk scopes granted: mail, files, sites, the directory, offline_access, ...",
    )
    risky = serializers.BooleanField(read_only=True)
    in_catalog = serializers.BooleanField(
        read_only=True, help_text="The last run found it in the catalog (a record, or the Entra source's candidate)."
    )
    status = serializers.ChoiceField(
        ["new", "linked", "dismissed"],
        read_only=True,
        help_text="linked: a live record has its application ID, or a candidate for it was linked or created; "
        "dismissed: its candidate was dismissed and hasn't changed since; new: neither.",
    )
    record = serializers.SerializerMethodField(help_text="The record it is linked to, if any.")
    candidate = serializers.SerializerMethodField(
        help_text="The discovery candidate to link, create or dismiss it with (discovery/observations/{id}), if any."
    )
    first_seen_at = serializers.DateTimeField(read_only=True)
    last_seen_at = serializers.DateTimeField(read_only=True)

    @extend_schema_field(RecordBriefSerializer(allow_null=True))
    def get_record(self, row) -> dict[str, Any] | None:
        return self.context.get("records", {}).get(row.app_id)

    @extend_schema_field(ShadowItCandidateSerializer(allow_null=True))
    def get_candidate(self, row) -> dict[str, Any] | None:
        return candidate_of(row, self.context.get("candidates", {}).get(row.app_id, []))


def candidate_of(row, found) -> dict[str, Any] | None:
    """This connector's own candidate for the app (unless out of scope), else an Entra connector's observation."""
    own = [o for o in found if o.connector_id == row.connector_id and not o.out_of_scope]
    others = [o for o in found if o.connector.source == Source.ENTRA]
    chosen = (own or others or [None])[0]
    if chosen is None:
        return None
    return {
        "id": str(chosen.id),
        "status": engine.status_of(chosen),
        "connector": {"id": str(chosen.connector_id), "name": chosen.connector.name},
        "source": chosen.connector.source,
    }
