"""Audit snapshots: the target of an audit event as it was before and after the write.

Format. A snapshot is a JSON object of the target's audited fields, named as the API names them:
- plain values as JSON (dates and times in ISO 8601, UUIDs and decimals as strings);
- a reference (foreign key) in an identity format, `{"id": ..., "display": "<name when written>"}`, so a diff stays
  readable after the referenced row is renamed or deleted. References to a Person or a User keep `display` null: audit
  data stores people as IDs only, so anonymizing a Person also anonymizes old snapshots, and the API resolves the current
  name when read;
- a many-to-many set (a record's tags, a cycle's covered applications) as a list of identity entries sorted by id, so
  order never counts as a change and two entries with the same name still differ;
- a record's or vendor's custom field values as one key per field that applies, `custom_field_data.<name>`,
  by the organization's current definitions only (catalog/customfields.audit_values).

What is in a snapshot is a per-model whitelist (SPECS, and resource_snapshot for records): never a secret, a
ciphertext, a token hash, a password or a licence key, never a Person's name, email or title, never a
file's contents. A JSON column with an open shape is copied only by its declared keys (Spec.json_keys). Every key is
also checked against redaction.SNAPSHOT_DENYLIST, and JSON values are redacted at any depth, as a second line of
defence.

Capture. The "before" snapshot is the database state when the unit of work (core/request_context.py: one request,
job or command) first touches the target: a pre_save or pre_delete signal on the target or one of its child rows (a
record's extension row or tag links, a cycle's coverage rows) reads it once. catalog/services.audit then takes the
"after" snapshot from the saved instance and drops the stored "before", so a later write in the unit reads it from the
database again. An event whose target was not written in the unit (a sign-in, "run
requested", an observation linked to an existing record) has no snapshots. A `.deleted` event keeps the row as it
was when removed as `prechange` and null as `postchange`. Writes that bypass signals (bulk_create, QuerySet.update)
call touch() first when they change audited fields."""

import datetime
import decimal
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from django.contrib.auth import get_user_model
from django.db import models
from django.db.models import Value
from django.db.models.functions import Concat, Trim
from django.db.models.signals import pre_delete, pre_save

from core.request_context import snapshot_store

from .redaction import SNAPSHOT_DENYLIST, redact, secret_like


@dataclass(frozen=True)
class Spec:
    fields: tuple[str, ...]  # model field names; foreign keys become identity entries, many-to-many identity lists
    # JSON columns are copied only by an explicit key list: {field: instance -> the keys to keep}. A key nobody
    # declared (a future setting, a stray value) never reaches the permanent log, whatever it is called.
    json_keys: dict[str, Callable[[Any], tuple[str, ...]]] = field(default_factory=dict)


@dataclass
class Pending:
    pre: dict[str, Any] | None  # the target as the unit first saw it; None when the unit created it


# Organization.settings keys that are audited: a key is added here when a feature starts writing it, and only if it is
# not a secret (secrets have their own slots). Any other key never reaches a snapshot.
ORG_SETTINGS_AUDITED: tuple[str, ...] = (
    "compliance_profile_enabled",  # catalog/compliance.py
    "attachment_max_size_mb",  # catalog/attachments.py ORG_KEYS
    "attachment_extensions",
    "attachment_quota_mb",
    "data_quality_disabled_rules",  # catalog/quality.py SETTING
    "sso_auto_provision",  # core/sso.py AUTO_PROVISION
    "scim_default_role",  # the optional ee package
    "scim_role_groups",
    "inactive_seat_days",  # discovery/licence_waste.py SETTING
)


def _connector_config_keys(connector) -> tuple[str, ...]:
    from discovery import engine

    source = engine.SOURCES.get(connector.source)
    return tuple(source.CONFIG_FIELDS) if source is not None else ()  # unknown source: fail closed


def _connector_scope_keys(connector) -> tuple[str, ...]:
    from discovery import engine

    return tuple(engine.module(connector).SCOPE_DEFAULTS)  # the scope's declared shape; other keys are refused


@cache
def specs() -> dict[type[models.Model], Spec]:
    """The audited fields of every snapshot-able target other than Resource (resource_snapshot)."""
    from api.models import ApiToken
    from core.models import (
        GoogleWorkspaceConnection,
        HaloConnection,
        MicrosoftConnection,
        OktaConnection,
        Organization,
        SsoSettings,
    )
    from discovery.models import AssistantSettings, Connector, Observation
    from eol.models import TrackedVersion
    from imports.models import ImportRun, ImportSource
    from money.models import CalendarFeed, Contract, ContractItem, LicensePool, RenewalCycle, RenewalObligation
    from notify.models import (
        EmailSettings,
        NotificationRule,
        Reminder,
        SavedViewSubscription,
        TeamsDestination,
        WebhookDestination,
    )
    from tickets.models import TicketConnection, TicketLink

    from .models import (
        AIModel,
        AIProvider,
        ApplicationInstance,
        Attachment,
        ChangeAssessment,
        CustomField,
        Department,
        LinkConfirmation,
        Person,
        QualityException,
        RecordReview,
        Relationship,
        RelationshipType,
        SavedFilter,
        SoftwareCategory,
        Tag,
        Team,
        Vendor,
        VendorRiskDocument,
        VendorRiskProfile,
    )

    return {
        Person: Spec(("department", "user", "active", "anonymized_at")),  # name, email and title never
        Team: Spec(("name", "description", "members")),  # members as Person IDs only
        Department: Spec(("name",)),
        Vendor: Spec(
            (
                "name",
                "website",
                "support_url",
                "support_email",
                "support_phone",
                "portal_url",
                "account_manager",
                "notes",
            )
        ),
        # The vendor risk file (catalog/vendor_risk.py); attachments as identity entries.
        VendorRiskProfile: Spec(
            (
                "vendor",
                "risk_tier",
                "processes_personal_data",
                "questionnaire_status",
                "questionnaire_requested_on",
                "questionnaire_reviewed_on",
                "assurance_type",
                "assurance_issued_on",
                "assurance_validity_months",
                "assurance_expires_on",
                "assurance_attachment",
                "dpa_status",
                "dpa_signed_on",
                "dpa_attachment",
                "subprocessor_url",
                "subprocessor_attachment",
                "subprocessors_reviewed_on",
                "notes",
            )
        ),
        VendorRiskDocument: Spec(
            ("vendor", "kind", "title", "issued_on", "validity_months", "expires_on", "attachment", "notes")
        ),
        SoftwareCategory: Spec(("name", "description")),
        CustomField: Spec(
            (
                "name",
                "label",
                "type",
                "description",
                "choices",
                "required",
                "default",
                "applies_to",
                "weight",
                "is_reminder_source",
            )
        ),
        Tag: Spec(("name", "color", "description")),
        AIProvider: Spec(("name", "website")),
        AIModel: Spec(("provider", "name", "version", "modality", "hosting_region")),
        ApplicationInstance: Spec(("application", "name", "environment", "url", "status")),
        # End-of-life tracking (eol/services.py): the product, version and cycle of a record or an instance.
        TrackedVersion: Spec(("resource", "instance", "product", "version", "cycle", "cycle_explicit")),
        # Never the stored file's name or its contents: what it is, its size and SHA-256 (catalog/attachments.py).
        Attachment: Spec(
            (
                "resource",
                "vendor",
                "contract",
                "kind",
                "title",
                "category",
                "url",
                "filename",
                "content_type",
                "size",
                "sha256",
                "uploaded_by",
            )
        ),
        # The type as an identity entry: {"id", "display": its name when written} (the slug before).
        Relationship: Spec(("type", "source", "source_instance", "target", "target_instance", "purpose")),
        RelationshipType: Spec(("slug", "name", "inverse_name", "description", "is_dependency", "is_core")),
        QualityException: Spec(("resource", "rule", "reason", "owner", "expires_on", "created_by")),
        RecordReview: Spec(
            (
                "resource",
                "confirmed_by",
                "inactive_reason",
                "recorded_by",
                "confirmation_date",
                "next_review_date",
                "evidence",
                "confirmed_values",
            )
        ),
        LinkConfirmation: Spec(
            (
                "relationship",
                "integration",
                "confirmed_by",
                "inactive_reason",
                "recorded_by",
                "confirmed_on",
                "evidence",
                "confirmed_signature",
            )
        ),
        ChangeAssessment: Spec(
            (
                "target",
                "change_type",
                "title",
                "description",
                "owner",
                "target_date",
                "ticket_url",
                "findings",
                "status",
                "retirement_ack_reason",
            )
        ),
        SavedFilter: Spec(("user", "kind", "name", "query", "shared", "columns")),
        RenewalObligation: Spec(("name", "vendor", "responsible", "followup", "auto_renew", "archived_at")),
        RenewalCycle: Spec(
            (
                "obligation",
                "sequence",
                "renewal_date",
                "notice_value",
                "notice_unit",
                "explicit_deadline",
                "term_value",
                "term_unit",
                "applications",
                "decision",
                "decision_notes",
                "decided_on",
                "completed_on",
                "completion_evidence",
                # What a decision on a contract's term changed (money 0005)
                "old_amount",
                "old_currency",
                "old_billing_frequency",
                "old_annualized_cost",
                "old_start_date",
                "old_end_date",
                "new_amount",
                "new_currency",
                "new_billing_frequency",
                "new_annualized_cost",
                "new_start_date",
                "new_end_date",
            )
        ),
        # Contracts. The stored cancellation deadline is left out: it is derived, and also moves when the
        # organization's lead time changes, which is that setting's event, not the contract's.
        Contract: Spec(
            (
                "name",
                "vendor",
                "contract_number",
                "po_number",
                "amount",
                "currency",
                "billing_frequency",
                "annualized_cost",
                "start_date",
                "end_date",
                "auto_renew",
                "notice_value",
                "notice_unit",
                "renewal_owner",
                "status",
                "cost_center",
                "notes",
                "predecessor",
                "renewal_obligation",
                "archived_at",
            )
        ),
        ContractItem: Spec(("contract", "resource", "allocation_percent")),
        LicensePool: Spec(
            (
                "application",
                "contract",
                "sku_name",
                "sku_id",
                "seats_purchased",
                "seats_assigned",
                "seat_sync",
                "last_synced",
                "notes",
            )
        ),
        # The organization's settings columns; the licence key slot never; the settings JSON only by declared key.
        Organization: Spec(
            (
                "name",
                "timezone",
                "review_interval_days",
                "renewal_lead_days",
                "reminder_thresholds",
                "digest_time",
                "coordinator",
                "edition",
                *(("settings",) if ORG_SETTINGS_AUDITED else ()),
            ),
            json_keys={"settings": lambda org: ORG_SETTINGS_AUDITED},
        ),
        # The source's own settings, by its declared keys; credentials are the connection's.
        Connector: Spec(
            (
                "source",
                "name",
                "enabled",
                "config",
                "scope",
                "schedule",
                "reviewer",
                "microsoft_connection",
                "halo_connection",
                "okta_connection",
                "google_workspace_connection",
            ),
            json_keys={"config": _connector_config_keys, "scope": _connector_scope_keys},
        ),
        # Connections: the sign-in values in clear and the test state; never the secret slot, the notes
        # (free text) or the stored test wording.
        MicrosoftConnection: Spec(
            ("name", "enabled", "tenant_id", "client_id", "revision", "tested_revision", "test_ok")
        ),
        HaloConnection: Spec(
            ("name", "enabled", "base_url", "client_id", "scope", "revision", "tested_revision", "test_ok")
        ),
        # Ticket links (tickets/services.py): never the secret slot, the notes or the stored test wording.
        TicketConnection: Spec(
            (
                "system",
                "name",
                "enabled",
                "base_url",
                "username",
                "halo_connection",
                "create_enabled",
                "create_target",
                "create_issue_type",
                "revision",
                "tested_revision",
                "test_ok",
            )
        ),
        TicketLink: Spec(
            (
                "assessment",
                "obligation",
                "cycle",
                "system",
                "external_key",
                "url",
                "title",
                "status",
                "status_source",
                "external_status",
                "created_in_system",
            )
        ),
        OktaConnection: Spec(
            ("name", "enabled", "base_url", "client_id", "scope", "revision", "tested_revision", "test_ok")
        ),
        GoogleWorkspaceConnection: Spec(
            (
                "name",
                "enabled",
                "admin_email",
                "service_account_email",
                "service_account_client_id",
                "revision",
                "tested_revision",
                "test_ok",
            )
        ),
        # Not the source's data: it can carry suggested owners' names and emails.
        Observation: Spec(("connector", "kind", "external_id", "display_name", "gone_at")),
        ImportSource: Spec(("name", "kind", "column_mapping")),
        # Not the file, its rows, decisions or filename: those are purged.
        ImportRun: Spec(("kind", "source", "status", "previewed_at", "committed_at", "purged_at")),
        # Never the hash; the prefix is what the UI shows to tell tokens apart.
        ApiToken: Spec(("user", "name", "prefix", "read_only", "expires_on", "revoked_at")),
        # A renewals calendar feed: never the hash; the prefix tells feeds apart.
        CalendarFeed: Spec(("user", "prefix", "revoked_at")),
        # A reminder: what it is about and what people did with it; the generator's own writes aren't audited.
        Reminder: Spec(
            (
                "source_type",
                "source_id",
                "source_field",
                "due_date",
                "status",
                "snoozed_until",
                "acknowledged_by",
                "resolution_kind",
                "resolution_note",
                "resolution_evidence",
                "resolved_by",
            )
        ),
        # Notification channels: the settings in clear only; secret slots and webhook addresses never,
        # a destination's address by its host.
        EmailSettings: Spec(
            (
                "transport",
                "graph_tenant_id",
                "graph_client_id",
                "sender_mailbox",
                "smtp_host",
                "smtp_port",
                "smtp_security",
                "smtp_username",
                "from_address",
                "last_test_ok",
            )
        ),
        TeamsDestination: Spec(("name", "enabled", "url_host", "last_test_ok")),
        # A webhook: its subscriptions and why it was turned off; never a secret slot or the address.
        WebhookDestination: Spec(
            ("name", "description", "enabled", "url_host", "events", "disabled_reason", "last_test_ok")
        ),
        # A notification rule: every setting; people as IDs only, Teams destinations by name (never a
        # URL). Webhook destinations are left out: "webhook" is on the snapshot denylist; the event's `fields` still
        # says when they changed.
        NotificationRule: Spec(
            (
                "name",
                "kind",
                "builtin_key",
                "enabled",
                "source_types",
                "lead_times_days",
                "escalation_days",
                "repeat_every_days",
                "escalate_when_acknowledged",
                "send_email",
                "teams_destinations",
                "notify_routed",
                "people",
                "team",
                "roles",
                "escalation_people",
                "escalation_team",
                "escalation_roles",
                "fallback_team",
                "fallback_person",
                "criticalities",
                "record_kinds",
                "vendor",
                "min_annual_value",
            )
        ),
        # A saved view subscription: the view, the member (by membership id) and when.
        SavedViewSubscription: Spec(("saved_view", "membership", "weekday", "active")),
        # The assistant's settings: on or off and the cap; never the key slot.
        AssistantSettings: Spec(("enabled", "monthly_cap_usd")),
        # Single sign-on settings: the provider values in clear, the revisions and the test state; never the
        # secret slots or the test's detail (it can name the account that signed in). SAML certificates are
        # public but long: the update event names them by fingerprint instead.
        SsoSettings: Spec(
            (
                "status",
                "provider",
                "tenant",
                "okta_domain",
                "okta_server",
                "issuer",
                "hosted_domains",
                "client_id",
                "saml_entity_id",
                "saml_sso_url",
                "label",
                "active_revision",
                "draft_provider",
                "draft_tenant",
                "draft_okta_domain",
                "draft_okta_server",
                "draft_issuer",
                "draft_hosted_domains",
                "draft_client_id",
                "draft_saml_entity_id",
                "draft_saml_sso_url",
                "draft_label",
                "draft_revision",
                "draft_tested_revision",
                "draft_test_ok",
            )
        ),
    }


@cache
def roots() -> dict[type[models.Model], tuple[type[models.Model], str]]:
    """Child rows whose writes change their parent's snapshot: {child model: (parent model, attname on the child)}."""
    from money.models import RenewalCycle, RenewalCycleApplication
    from notify.models import (
        NotificationRule,
        NotificationRuleEscalationPerson,
        NotificationRulePerson,
        NotificationRuleTeamsDestination,
        NotificationRuleWebhookDestination,
    )

    from .models import (
        AgentModel,
        AIAgent,
        Application,
        Component,
        Integration,
        Resource,
        ResourceTag,
        Team,
        TeamMember,
    )

    return {
        TeamMember: (Team, "team_id"),
        Application: (Resource, "resource_id"),
        Component: (Resource, "resource_id"),
        Integration: (Resource, "resource_id"),
        AIAgent: (Resource, "resource_id"),
        AgentModel: (Resource, "agent_id"),  # an agent's key is its record's
        ResourceTag: (Resource, "resource_id"),
        RenewalCycleApplication: (RenewalCycle, "cycle_id"),
        NotificationRulePerson: (NotificationRule, "rule_id"),
        NotificationRuleEscalationPerson: (NotificationRule, "rule_id"),
        NotificationRuleTeamsDestination: (NotificationRule, "rule_id"),
        NotificationRuleWebhookDestination: (NotificationRule, "rule_id"),
    }


def tracked(model) -> bool:
    from .models import Resource

    return model is Resource or model in specs()


# ---- values -------------------------------------------------------------------------------------------------------


def plain(value):
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, uuid.UUID | decimal.Decimal):
        return str(value)
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return redact({k: plain(v) for k, v in value.items()}, SNAPSHOT_DENYLIST)
    return value


def ident(pk):
    """A primary key as JSON: users keep their integer id, everything else is a UUID string."""
    return pk if isinstance(pk, int) else str(pk)


def _people() -> tuple[type, ...]:
    from .models import Person

    return (Person, get_user_model())


def _display_path(model) -> Any:
    """How a referenced row is named in an identity entry (a field path, or an expression), or None for a person
    or a row without a name. An AI model is "name version", so two versions of one model differ in a diff."""
    from .models import AIModel, Attachment, ChangeAssessment, Integration

    if model in _people():
        return None
    if model is AIModel:
        return Trim(Concat("name", Value(" "), "version"))
    if model is Integration:
        return "resource__name"
    if model in (ChangeAssessment, Attachment):  # an attachment: a vendor risk file's link to it
        return "title"
    names = {f.name for f in model._meta.get_fields()}
    return "name" if "name" in names else None


def display(model, pk, cached=None) -> str | None:
    """The referenced row's name, from the instance's cached relation when present, else one query (remembered for the
    rest of the unit)."""
    path = _display_path(model)
    if path is None:
        return None
    if cached is not None and isinstance(path, str) and "__" not in path:
        return getattr(cached, path)
    store = snapshot_store()
    key = ("display", model._meta.label, pk)
    if key not in store:
        rows = model._base_manager.filter(pk=pk).annotate(_display=path) if not isinstance(path, str) else None
        found = rows if rows is not None else model._base_manager.filter(pk=pk)
        store[key] = found.values_list("_display" if rows is not None else path, flat=True).first()
    return store[key]


def ref(instance, field) -> dict[str, Any] | None:
    pk = getattr(instance, field.attname)
    if pk is None:
        return None
    cached = field.get_cached_value(instance, None) if field.is_cached(instance) else None
    return {"id": ident(pk), "display": display(field.related_model, pk, cached)}


def ref_to(model, pk) -> dict[str, Any] | None:
    return None if pk is None else {"id": ident(pk), "display": display(model, pk)}


def identities(rows) -> list[dict[str, Any]]:
    """(pk, display) pairs as a sorted identity list."""
    return sorted(({"id": ident(pk), "display": name} for pk, name in rows), key=lambda e: str(e["id"]))


def _m2m(instance, field) -> list[dict[str, Any]]:
    if instance.pk is None:
        return []
    path = _display_path(field.related_model)
    related = getattr(instance, field.name)
    if path is None:  # people and rows without a name: IDs only
        return identities((pk, None) for pk in related.values_list("pk", flat=True))
    if not isinstance(path, str):
        return identities(related.annotate(_display=path).values_list("pk", "_display"))
    return identities(related.values_list("pk", path))


# ---- snapshots ----------------------------------------------------------------------------------------------------

RESOURCE_EXTENSION = {
    "application": (
        "purpose",
        "vendor",
        "built_by",
        "license_model",
        "documentation_url",
        "support_url",
        "category",
        "deployment_model",
        "version",
        "primary_url",
        "admin_url",
        "authentication_method",
        "sso_enabled",
        "scim_provisioning",
        "entra_app_id",
        "service_principal_id",
    ),
    "component": (
        "component_type",
        "purpose",
        "host_runtime",
        "repository_url",
        "documentation_url",
        "built_by",
        "license_model",
        "hosting",
        "runbook_url",
        "schedule_or_trigger",
        "environment",
        "azure_resource_id",
        "pp_environment_id",
        "external_id",
    ),
    "integration": (
        "data_purpose",
        "protocol",
        "schedule",
        "documentation_url",
        "direction",
        "auth_method",
        "monitoring_url",
        "data_categories",  # data-flow map: a list in choice order
    ),
    # Models_used is a set of identity entries (sorted by id), channels a list in choice order.
    "ai_agent": (
        "purpose",
        "platform",
        "models_used",
        "environment",
        "pp_environment_id",
        "channels",
        "knowledge_sources",
        "tools_connectors",
        "auth_mode",
        "run_as_identity",
        "data_access_scope",
        "human_in_loop",
        "actions_can_write",
        "risk_classification",
        "repo_url",
        "external_id",
    ),
}
INTEGRATION_ENDS = {
    "source": "source_resource",
    "source_instance": "source_instance",
    "target": "target_resource",
    "target_instance": "target_instance",
}


def resource_snapshot(resource) -> dict[str, Any]:
    """An application, component, AI agent or integration record: its own fields, its kind's extension fields (an
    agent's models as identity entries; integration ends named source / target as the API names them) and its tags.
    Integrations have no business owner or criticality."""
    from . import compliance
    from .models import Kind, Resource, ResourceTag

    meta = Resource._meta
    integration = resource.kind == Kind.INTEGRATION
    data: dict[str, Any] = {"kind": resource.kind, "name": resource.name, "description": resource.description}
    data["lifecycle"] = resource.lifecycle
    if not integration:
        data["criticality"] = resource.criticality
    owners = ("technical_owner", "support_team", "it_steward") if integration else None
    for name in owners or ("business_owner", "technical_owner", "support_team", "it_steward"):
        data[name] = ref(resource, meta.get_field(name))
    data["archived_at"] = plain(resource.archived_at)
    data["data_classification"] = resource.data_classification  # integrations too
    if not integration:
        data["department"] = ref(resource, meta.get_field("department"))
        data["cost_center"] = resource.cost_center
        if compliance.enabled_for(resource.organization_id):  # left out while the profile is off
            for name in compliance.FIELDS:
                data[name] = plain(getattr(resource, name))
    ext = resource.extension
    if ext is not None:
        ext_meta = type(ext)._meta
        if integration:
            for name, attr in INTEGRATION_ENDS.items():
                data[name] = ref(ext, ext_meta.get_field(attr))
        for name in RESOURCE_EXTENSION.get(resource.kind, ()):
            field = ext_meta.get_field(name)
            if field.many_to_many:
                data[name] = _m2m(ext, field)
            else:
                data[name] = ref(ext, field) if field.is_relation else plain(getattr(ext, name))
    if not integration:
        data["tags"] = identities(
            ResourceTag.objects.filter(resource_id=resource.pk).values_list("tag_id", "tag__name")
        )
    data.update(_custom(resource, resource.kind))
    return data


def _custom(instance, kind) -> dict[str, Any]:
    """custom_field_data.<name> for each of the organization's fields that applies to the kind (declared keys only)."""
    from . import customfields

    return {
        k: plain(v)
        for k, v in customfields.audit_values(instance.organization_id, kind, instance.custom_field_data).items()
    }


def snapshot(instance) -> dict[str, Any] | None:
    """The instance's audited fields in the snapshot format, or None for a model that has no snapshot."""
    from .models import Resource

    if isinstance(instance, Resource):
        data = resource_snapshot(instance)
    else:
        spec = specs().get(type(instance))
        if spec is None:
            return None
        meta = type(instance)._meta
        data = {}
        for name in spec.fields:
            column = meta.get_field(name)
            if column.many_to_many:
                data[name] = _m2m(instance, column)
            elif column.is_relation:
                data[name] = ref(instance, column)
            elif name in spec.json_keys:
                value = column.value_from_object(instance) or {}
                data[name] = plain({k: value[k] for k in spec.json_keys[name](instance) if k in value})
            else:
                data[name] = plain(column.value_from_object(instance))
        if type(instance) in custom_field_models():
            data.update(_custom(instance, custom_field_models()[type(instance)]))
    return {k: v for k, v in data.items() if not secret_like(k, SNAPSHOT_DENYLIST)}


@cache
def custom_field_models() -> dict[type[models.Model], str]:
    """Models other than Resource whose snapshots carry custom fields, with the applies_to kind they answer to."""
    from money.models import Contract, LicensePool

    from .models import Vendor

    return {Vendor: "vendor", Contract: "contract", LicensePool: "license_pool"}


def load(model, pk):
    """The row as stored now, with what its snapshot reads joined in, or None if it doesn't exist."""
    from .models import Kind, Resource

    if model is Resource:
        return (
            Resource._base_manager.select_related(
                *Kind.values,
                "support_team",
                "department",
                "application__vendor",
                "application__category",
                "integration__source_resource",
                "integration__target_resource",
                "integration__source_instance",
                "integration__target_instance",
            )
            .filter(pk=pk)
            .first()
        )
    spec = specs()[model]
    meta = model._meta
    columns = [meta.get_field(n).attname for n in spec.fields if not meta.get_field(n).many_to_many]
    if model in custom_field_models():
        columns += ["organization_id", "custom_field_data"]
    return model._base_manager.filter(pk=pk).only(*columns).first()


# ---- capture ------------------------------------------------------------------------------------------------------


def _root_key(model, instance) -> tuple[type, Any] | None:
    parent = roots().get(model)
    if parent is not None:
        return parent[0], getattr(instance, parent[1])
    if tracked(model):
        return model, instance.pk
    return None


def _key(model, pk) -> tuple[str, str]:
    return (model._meta.label, str(pk))


def touch(instance, creating: bool = False) -> None:
    """Note that the unit is about to write `instance` (or a child row of its snapshot root): the first time, read the
    root's "before" snapshot from the database. Signals call this for saves and deletes; call it yourself before a
    bulk_create or QuerySet.update that changes audited fields."""
    found = _root_key(type(instance), instance)
    if found is None or found[1] is None:
        return
    model, pk = found
    store = snapshot_store()
    key = _key(model, pk)
    pending = store.get(key)
    if pending is None:
        if creating and model is type(instance):
            pending = Pending(pre=None)
        else:
            row = load(model, pk)
            pending = Pending(pre=snapshot(row) if row is not None else None)
        store[key] = pending


def take(target, action: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(prechange, postchange) for an event on `target`, written as the event is: see the module docstring."""
    if target is None or not tracked(type(target)) or target.pk is None:
        return None, None
    store = snapshot_store()
    key = _key(type(target), target.pk)
    if action.endswith(".deleted"):
        store.pop(key, None)
        row = load(type(target), target.pk)  # services audit a delete just before it happens
        return snapshot(row if row is not None else target), None
    # Taken off the store: a later write in the unit reads "before" from the database again, which is right whether
    # this event committed or its savepoint rolled back.
    pending = store.pop(key, None)
    if pending is None:
        return None, None
    return pending.pre, snapshot(target)


def _on_pre_save(sender, instance, raw=False, **kwargs):
    if not raw:
        touch(instance, creating=instance._state.adding)


def _on_pre_delete(sender, instance, **kwargs):
    touch(instance)


def connect() -> None:
    """Called once from CatalogConfig.ready: signals for the snapshot-able models and their child rows only."""
    from .models import Resource

    senders: list[type[models.Model]] = [Resource, *specs(), *roots()]
    for model in senders:
        uid = f"audit-snapshot-{model._meta.label}"
        pre_save.connect(_on_pre_save, sender=model, dispatch_uid=uid + "-save")
        pre_delete.connect(_on_pre_delete, sender=model, dispatch_uid=uid + "-delete")


# ---- related target -----------------------------------------------------------------------------------------------


def related_of(target) -> tuple[str, Any] | None:
    """The record a child row belongs to, as (type name, id): an instance's application, a review's record, a link
    confirmation's relationship (or the integration's record), a renewal cycle's renewal, an attachment's record or
    vendor or contract, a vendor risk profile's or document's vendor, a data-quality exception's record, a contract
    line's contract. None for anything else."""
    from money.models import ContractItem, RenewalCycle

    from .models import (
        ApplicationInstance,
        Attachment,
        LinkConfirmation,
        QualityException,
        RecordReview,
        VendorRiskDocument,
        VendorRiskProfile,
    )

    if isinstance(target, VendorRiskProfile | VendorRiskDocument):
        return "Vendor", target.vendor_id
    if isinstance(target, QualityException):
        return "Resource", target.resource_id
    if isinstance(target, Attachment):
        if target.resource_id is not None:
            return "Resource", target.resource_id
        if target.contract_id is not None:
            return "Contract", target.contract_id
        return "Vendor", target.vendor_id
    if isinstance(target, ContractItem):
        return "Contract", target.contract_id
    if isinstance(target, ApplicationInstance):
        return "Resource", target.application_id
    from eol.models import TrackedVersion

    if isinstance(target, TrackedVersion):
        return "Resource", target.resource_id
    from tickets.models import TicketLink

    if isinstance(target, TicketLink):
        if target.assessment_id is not None:
            return "ChangeAssessment", target.assessment_id
        return "RenewalObligation", target.obligation_id
    if isinstance(target, RecordReview):
        return "Resource", target.resource_id
    if isinstance(target, LinkConfirmation):
        if target.relationship_id:
            return "Relationship", target.relationship_id
        return "Resource", target.integration_id  # an Integration row's key is its record's
    if isinstance(target, RenewalCycle):
        return "RenewalObligation", target.obligation_id
    return None
