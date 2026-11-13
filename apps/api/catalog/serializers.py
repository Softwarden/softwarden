"""Serializers for the catalog viewsets (catalog/viewsets.py, catalog/directory_viewsets.py, catalog/curation_viewsets.py).
Output serializers render model instances and are read-only (core/drf.ReadOnlySerializer); input serializers only
describe request bodies in OpenAPI, because the service functions validate every write and own its messages.

Response shapes match what the SPA reads (apps/web/src/api.ts). Composite responses (an assessment with its snapshot,
the dependency explorer) are built by their modules as plain data and described here field by field;
tests/test_drf_resources.py checks that each description has exactly the keys the endpoint returns."""

from datetime import date
from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.drf import BriefSerializer, ProblemSerializer, ReadOnlySerializer, UserBriefSerializer, represent
from core.models import AuditEvent
from discovery.coverage_serializers import SsoCoverageSerializer
from eol.serializers import RecordEndOfLifeSerializer
from tickets.serializers import TicketLinkSerializer

from . import compliance, customfields
from .bulk import ROW_CODES
from .filters import REVIEW_STATES
from .models import (
    CUSTOM_FIELD_KINDS,
    RECORD_KIND_CHOICES,
    TAG_COLORS,
    AgentAuthMode,
    AgentChannel,
    AgentPlatform,
    AgentRisk,
    AssessmentStatus,
    AttachmentCategory,
    AttachmentKind,
    AuthenticationMethod,
    BuiltBy,
    ChangeType,
    ComponentType,
    Criticality,
    CustomFieldType,
    DataCategory,
    DataClassification,
    DeploymentModel,
    Environment,
    Hosting,
    InstanceStatus,
    IntegrationAuthMethod,
    IntegrationDirection,
    Kind,
    LicenseModel,
    Lifecycle,
    ModelModality,
    RiskTier,
    ValidationStatus,
)
from .quality import SEVERITY_ORDER
from .state import agent_gaps, all_gaps, review_state
from .vendor_risk_serializers import VendorRiskSummarySerializer

LINK_STATES = ["unconfirmed", "confirmed", "needs_verification"]


class PersonBriefSerializer(BriefSerializer):
    """A Person reference with the details lists show next to an owner (active, anonymized, department)."""

    email = serializers.CharField(read_only=True)
    title = serializers.CharField(read_only=True)
    active = serializers.BooleanField(read_only=True)
    anonymized = serializers.SerializerMethodField()
    department = BriefSerializer(read_only=True, allow_null=True)

    def get_anonymized(self, person) -> bool:
        return person.anonymized_at is not None

    def to_representation(self, instance):
        """Direct rendering (see core.drf.BriefSerializer); PersonSerializer, which adds fields, takes the generic
        path."""
        if type(self) is not PersonBriefSerializer:
            return super().to_representation(instance)
        department = instance.department
        return {
            "id": str(instance.id),
            "name": str(instance.name),
            "email": str(instance.email),
            "title": str(instance.title),
            "active": bool(instance.active),
            "anonymized": self.get_anonymized(instance),
            "department": None if department is None else {"id": str(department.id), "name": str(department.name)},
        }


class RecordBriefSerializer(BriefSerializer):
    """A reference to a record with its kind, so a client can link to it (/applications/<id>, /components/<id>)."""

    kind = serializers.ChoiceField(Kind.choices, read_only=True)


class InstanceSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    application = serializers.UUIDField(source="application_id", read_only=True)
    name = serializers.CharField(read_only=True)
    environment = serializers.CharField(read_only=True)
    url = serializers.CharField(read_only=True)
    status = serializers.ChoiceField(InstanceStatus.choices, read_only=True)


COMPLIANCE_HELP = "Compliance profile: returned only while the organization has it on; otherwise the key is absent."
CUSTOM_FIELDS_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
    "description": "Custom field values, {name: value}: every custom field that applies to this kind (GET "
    "/custom-fields), in field order, null when not set. Text, long text, URL, choice and date (YYYY-MM-DD) values are "
    "strings; whole and decimal numbers are numbers; yes/no is a boolean; multiple choices a list; an object field "
    "a JSON object.",
}
CUSTOM_FIELDS_WRITE = (
    'Custom field values by name, e.g. {"business_case": "..."}. Only the names sent change (a PATCH merges); null '
    'or "" clears one. A name that isn\'t a custom field of this kind, or a value its type refuses, is a field error '
    "keyed custom_field_data.<name>. On create, fields left out get their default and required ones must have a value."
)


class CustomFieldValuesMixin(serializers.Serializer):
    """`custom_field_data` from the context's `custom_fields` (the fields that apply to the kind, read once per
    request by the viewset)."""

    custom_field_data = serializers.SerializerMethodField()

    @extend_schema_field(CUSTOM_FIELDS_SCHEMA)
    def get_custom_field_data(self, obj) -> dict[str, Any]:
        return customfields.values_for(self.context.get("custom_fields", ()), obj.custom_field_data)


class QualityRuleRefSerializer(serializers.Serializer):
    id = serializers.CharField(help_text="The rule's id (GET /data-quality/rules).")
    label = serializers.CharField()  # type: ignore[assignment]  # the API field is called label
    severity = serializers.ChoiceField(["high", "medium", "low"])


class QualitySerializer(serializers.Serializer):
    """A record's data-quality results (catalog/quality.py): its completeness score and the rules it fails."""

    score = serializers.IntegerField(
        help_text="Completeness, 0 to 100: the share of the data-quality checks that apply to it and pass, rounded "
        "down. Checks marked not applicable are left out; 100 when none apply."
    )
    failing = QualityRuleRefSerializer(many=True, help_text="The checks it fails, most severe first.")
    excepted = QualityRuleRefSerializer(
        many=True, help_text="Checks it would fail that are marked not applicable (an exception that hasn't expired)."
    )


def quality_json(result) -> dict[str, Any]:
    def ref(rule):
        return {"id": rule.id, "label": rule.label, "severity": rule.severity}

    def ordered(rules):
        return sorted(rules, key=lambda r: (SEVERITY_ORDER[r.severity], r.label))

    return {
        "score": result.score,
        "failing": [ref(r) for r in ordered(result.failing)],
        "excepted": [ref(r) for r in ordered(result.excepted)],
    }


class ResourceSerializer(CustomFieldValuesMixin, ReadOnlySerializer):
    """The fields applications and components share. Review state, next review date and tags come from the
    serializer context, computed once per page by the viewset: `today`, `latest` {resource_id: RecordReview}, `tags`
    {resource_id: [names]} and `compliance` (whether the organization's compliance profile is on)."""

    id = serializers.UUIDField(read_only=True)
    kind = serializers.ChoiceField(Kind.choices, read_only=True)
    name = serializers.CharField(read_only=True)
    slug = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    lifecycle = serializers.ChoiceField(Lifecycle.choices, read_only=True)
    criticality = serializers.SerializerMethodField()
    purpose = serializers.CharField(source="extension.purpose", read_only=True)
    business_owner = PersonBriefSerializer(read_only=True, allow_null=True)
    technical_owner = PersonBriefSerializer(read_only=True, allow_null=True)
    support_team = BriefSerializer(read_only=True, allow_null=True)
    it_steward = UserBriefSerializer(read_only=True, allow_null=True)
    archived = serializers.SerializerMethodField()
    gaps = serializers.SerializerMethodField(help_text="For example 'No business owner'.")
    review_state = serializers.SerializerMethodField()
    review_why = serializers.SerializerMethodField()
    next_review_date = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField(help_text="Tag names, sorted.")
    quality = serializers.SerializerMethodField(
        help_text="Data quality: the completeness score and the failing checks. `gaps` keeps the ownership "
        "and agent gaps as before."
    )
    department = BriefSerializer(read_only=True, allow_null=True, help_text="The department the record belongs to.")
    cost_center = serializers.CharField(read_only=True)
    data_classification = serializers.CharField(
        read_only=True, help_text=f"One of {', '.join(DataClassification.values)}, or empty when not recorded."
    )
    # The compliance profile (catalog/compliance.py): present only while the organization has it on, so these are
    # declared optional (not read-only, which OpenAPI marks required). The serializer never writes.
    is_gxp = serializers.BooleanField(required=False, allow_null=True, help_text=COMPLIANCE_HELP)
    validation_status = serializers.ChoiceField(
        ValidationStatus.choices, required=False, allow_null=True, help_text=COMPLIANCE_HELP
    )
    validation_doc_url = serializers.CharField(required=False, allow_null=True, help_text=COMPLIANCE_HELP)
    last_periodic_review = serializers.DateField(required=False, allow_null=True, help_text=COMPLIANCE_HELP)
    next_periodic_review = serializers.DateField(required=False, allow_null=True, help_text=COMPLIANCE_HELP)
    risk_tier = serializers.ChoiceField(RiskTier.choices, required=False, allow_null=True, help_text=COMPLIANCE_HELP)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self.context.get("compliance"):  # omitted, not null, while the profile is off
            for field in compliance.FIELDS:
                data.pop(field, None)
        return data

    @extend_schema_field(serializers.ChoiceField(Criticality.choices, allow_null=True))
    def get_criticality(self, r) -> str | None:
        return r.criticality or None

    def get_archived(self, r) -> bool:
        return r.archived_at is not None

    def get_gaps(self, r) -> list[str]:
        return all_gaps(r)

    def _review(self, r) -> tuple[str, str]:
        cache = self.__dict__.setdefault("_review_state", {})  # per serializer (the list's shared child)
        if r.id not in cache:
            cache[r.id] = review_state(r, self.context["latest"].get(r.id), self.context["today"])
        return cache[r.id]

    @extend_schema_field(serializers.ChoiceField(REVIEW_STATES))
    def get_review_state(self, r) -> str:
        return self._review(r)[0]

    def get_review_why(self, r) -> str:
        return self._review(r)[1]

    def get_next_review_date(self, r) -> date | None:
        latest = self.context["latest"].get(r.id)
        return latest.next_review_date if latest else None

    def get_tags(self, r) -> list[str]:
        return list(self.context["tags"].get(r.id, ()))

    @extend_schema_field(QualitySerializer)
    def get_quality(self, r) -> dict[str, Any]:
        return quality_json(self.context["quality"][r.id])


class ApplicationSerializer(ResourceSerializer):
    vendor = BriefSerializer(source="application.vendor", read_only=True, allow_null=True)
    built_by = serializers.CharField(source="application.built_by", read_only=True)
    license_model = serializers.CharField(source="application.license_model", read_only=True)
    documentation_url = serializers.CharField(source="application.documentation_url", read_only=True)
    support_url = serializers.CharField(source="application.support_url", read_only=True)
    category = BriefSerializer(source="application.category", read_only=True, allow_null=True)
    deployment_model = serializers.CharField(
        source="application.deployment_model",
        read_only=True,
        help_text=f"One of {', '.join(DeploymentModel.values)}, or empty.",
    )
    version = serializers.CharField(source="application.version", read_only=True)
    primary_url = serializers.CharField(source="application.primary_url", read_only=True)
    admin_url = serializers.CharField(source="application.admin_url", read_only=True)
    authentication_method = serializers.CharField(
        source="application.authentication_method",
        read_only=True,
        help_text=f"One of {', '.join(AuthenticationMethod.values)}, or empty.",
    )
    sso_enabled = serializers.BooleanField(
        source="application.sso_enabled", read_only=True, allow_null=True, help_text="Null: not recorded."
    )
    scim_provisioning = serializers.BooleanField(
        source="application.scim_provisioning", read_only=True, allow_null=True, help_text="Null: not recorded."
    )
    entra_app_id = serializers.CharField(source="application.entra_app_id", read_only=True)
    service_principal_id = serializers.CharField(source="application.service_principal_id", read_only=True)


class ComponentSerializer(ResourceSerializer):
    component_type = serializers.ChoiceField(ComponentType.choices, source="component.component_type", read_only=True)
    host_runtime = serializers.CharField(source="component.host_runtime", read_only=True)
    repository_url = serializers.CharField(source="component.repository_url", read_only=True)
    documentation_url = serializers.CharField(source="component.documentation_url", read_only=True)
    built_by = serializers.CharField(source="component.built_by", read_only=True)
    license_model = serializers.CharField(source="component.license_model", read_only=True)
    hosting = serializers.CharField(
        source="component.hosting", read_only=True, help_text=f"One of {', '.join(Hosting.values)}, or empty."
    )
    runbook_url = serializers.CharField(source="component.runbook_url", read_only=True)
    schedule_or_trigger = serializers.CharField(source="component.schedule_or_trigger", read_only=True)
    environment = serializers.CharField(
        source="component.environment", read_only=True, help_text=f"One of {', '.join(Environment.values)}, or empty."
    )
    azure_resource_id = serializers.CharField(source="component.azure_resource_id", read_only=True)
    pp_environment_id = serializers.CharField(
        source="component.pp_environment_id", read_only=True, help_text="Power Platform environment ID."
    )
    external_id = serializers.CharField(source="component.external_id", read_only=True)


class AIModelBriefSerializer(BriefSerializer):
    """An AI model reference with its version and provider."""

    version = serializers.CharField(read_only=True)
    provider = BriefSerializer(read_only=True)

    def to_representation(self, instance):
        """Direct rendering (see core.drf.BriefSerializer): an agent list renders every agent's models."""
        if type(self) is not AIModelBriefSerializer:
            return super().to_representation(instance)
        provider = instance.provider
        return {
            "id": str(instance.id),
            "name": str(instance.name),
            "version": str(instance.version),
            "provider": {"id": str(provider.id), "name": str(provider.name)},
        }


class AIAgentSerializer(ResourceSerializer):
    """An AI agent: the shared record fields plus the agent's own. `models_used` is read from the
    queryset's prefetch (catalog/viewsets.AIAgentViewSet), so a page costs one query for every agent's models.
    `gaps` adds the agent's governance gaps (no data access scope, no human-in-loop decision)."""

    platform = serializers.CharField(
        source="ai_agent.platform", read_only=True, help_text=f"One of {', '.join(AgentPlatform.values)}, or empty."
    )
    models_used = serializers.SerializerMethodField(help_text="The AI models the agent uses, by name.")
    environment = serializers.CharField(
        source="ai_agent.environment", read_only=True, help_text=f"One of {', '.join(Environment.values)}, or empty."
    )
    pp_environment_id = serializers.CharField(
        source="ai_agent.pp_environment_id", read_only=True, help_text="Power Platform environment ID."
    )
    channels = serializers.ListField(
        source="ai_agent.channels",
        child=serializers.ChoiceField(AgentChannel.choices),
        read_only=True,
        help_text="Where people reach the agent, in a fixed order.",
    )
    knowledge_sources = serializers.CharField(source="ai_agent.knowledge_sources", read_only=True)
    tools_connectors = serializers.CharField(source="ai_agent.tools_connectors", read_only=True)
    auth_mode = serializers.CharField(
        source="ai_agent.auth_mode", read_only=True, help_text=f"One of {', '.join(AgentAuthMode.values)}, or empty."
    )
    run_as_identity = serializers.CharField(
        source="ai_agent.run_as_identity", read_only=True, help_text="The account or app the agent acts as."
    )
    data_access_scope = serializers.CharField(source="ai_agent.data_access_scope", read_only=True)
    human_in_loop = serializers.BooleanField(
        source="ai_agent.human_in_loop", read_only=True, allow_null=True, help_text="Null: no decision recorded."
    )
    actions_can_write = serializers.BooleanField(
        source="ai_agent.actions_can_write", read_only=True, allow_null=True, help_text="Null: not recorded."
    )
    risk_classification = serializers.CharField(
        source="ai_agent.risk_classification",
        read_only=True,
        help_text=f"One of {', '.join(AgentRisk.values)}, or empty.",
    )
    repo_url = serializers.CharField(source="ai_agent.repo_url", read_only=True)
    external_id = serializers.CharField(
        source="ai_agent.external_id", read_only=True, help_text="Bot ID or schema name in the source platform."
    )

    @extend_schema_field(AIModelBriefSerializer(many=True))
    def get_models_used(self, r) -> list[dict[str, Any]]:
        return [represent(AIModelBriefSerializer, m) for m in r.ai_agent.models_used.all()]

    def get_gaps(self, r) -> list[str]:
        return all_gaps(r) + agent_gaps(r.ai_agent)


def archived_by(resource) -> dict[str, Any] | None:
    """Who archived it, from the latest resource.archived audit event (the record keeps no actor column). None while the
    record isn't archived. The one place this is looked up (records and integrations)."""
    if resource.archived_at is None:
        return None
    event = (
        AuditEvent.objects.filter(
            organization_id=resource.organization_id, target_id=resource.id, action="resource.archived"
        )
        .select_related("actor")
        .order_by("-created_at")
        .first()
    )
    actor = event.actor if event else None
    return {"id": actor.id, "name": actor.get_full_name() or actor.email} if actor else None


class ArchiverSerializer(ReadOnlySerializer):
    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(read_only=True)


class ArchivedMixin(serializers.Serializer):
    """Detail-only: when the record was archived and who did it. Both null while not archived."""

    archived_at = serializers.SerializerMethodField()
    archived_by = serializers.SerializerMethodField()

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_archived_at(self, r) -> Any:
        return r.archived_at  # rendered by the JSON encoder, as before the viewsets

    @extend_schema_field(ArchiverSerializer(allow_null=True))
    def get_archived_by(self, r) -> dict[str, Any] | None:
        return archived_by(r)


class EndOfLifeMixin(serializers.Serializer):
    """Detail-only (capability eol): the record's end-of-life tracking and derived status (eol/reads.py)."""

    end_of_life = serializers.SerializerMethodField(
        help_text="The record's endoflife.date product, cycle and status, with its instances' rows; null when it "
        "isn't tracked or the edition lacks the eol capability (GET /<records>/{id}/end-of-life)."
    )

    @extend_schema_field(RecordEndOfLifeSerializer(allow_null=True))
    def get_end_of_life(self, r) -> dict[str, Any] | None:
        from eol import reads

        return reads.record_block(r.organization, r, self.context["today"])


class SsoCoverageMixin(serializers.Serializer):
    """Detail-only (capability discovery.entra): the application's SSO and MFA coverage from Entra
    (discovery/coverage.py)."""

    sso_coverage = serializers.SerializerMethodField(
        help_text="Single sign-on, SCIM provisioning and conditional access MFA from an SSO and MFA coverage "
        "connector, with gap chips and where Entra differs from the record's own fields (never applied by a run); "
        "null without the discovery.entra capability. GET /reports/sso-coverage lists every application."
    )

    @extend_schema_field(SsoCoverageSerializer(allow_null=True))
    def get_sso_coverage(self, r) -> dict[str, Any] | None:
        from discovery import coverage

        return coverage.record_block(r.organization, r)


class ApplicationDetailSerializer(ArchivedMixin, EndOfLifeMixin, SsoCoverageMixin, ApplicationSerializer):
    instances = InstanceSerializer(source="instances.all", many=True, read_only=True)


class ComponentDetailSerializer(ArchivedMixin, EndOfLifeMixin, ComponentSerializer):
    pass


class AIAgentDetailSerializer(ArchivedMixin, AIAgentSerializer):
    pass


# ---- request bodies (documentation; catalog/services.py validates) -------------------------------------------------------


COMPLIANCE_WRITE = (
    "Compliance profile: accepted only while the organization has it on; otherwise a field error. Null clears it."
)


class RecordInputSerializer(serializers.Serializer):
    """Create needs name and lifecycle; every other field is optional and may be sent alone on PATCH."""

    name = serializers.CharField()
    lifecycle = serializers.ChoiceField(Lifecycle.choices)
    tags = serializers.ListField(
        child=serializers.CharField(), required=False, help_text="Replaces the record's tags; new names are created."
    )
    criticality = serializers.ChoiceField(Criticality.choices, required=False, allow_null=True)
    description = serializers.CharField(required=False)
    purpose = serializers.CharField(required=False)
    built_by = serializers.ChoiceField(BuiltBy.choices, required=False, allow_blank=True)
    license_model = serializers.ChoiceField(LicenseModel.choices, required=False, allow_blank=True)
    documentation_url = serializers.URLField(required=False)
    business_owner = serializers.UUIDField(required=False, allow_null=True, help_text="An active Person.")
    technical_owner = serializers.UUIDField(required=False, allow_null=True, help_text="An active Person.")
    support_team = serializers.UUIDField(required=False, allow_null=True)
    it_steward = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="An Admin or Editor user: a user ID as an integer (a string of digits is accepted too).",
    )
    confirm_duplicate = serializers.BooleanField(
        required=False, help_text="Save even though another record of this kind has the same name."
    )
    expected = serializers.DictField(
        required=False,
        help_text="PATCH only. Values that must still be current, keyed by ownership field; a mismatch is a 409 "
        "with type /problems/stale-value (used by Undo).",
    )
    undo = serializers.BooleanField(required=False, help_text="PATCH only. Marks the audit event as an Undo.")
    retirement_ack = serializers.BooleanField(
        required=False,
        help_text="Set true to acknowledge the active dependents when the request retires the record.",
    )
    retirement_ack_reason = serializers.CharField(
        required=False, help_text="Required with retirement_ack; saved in the audit history."
    )
    department = serializers.UUIDField(required=False, allow_null=True)
    cost_center = serializers.CharField(required=False, allow_blank=True, max_length=50)
    data_classification = serializers.ChoiceField(DataClassification.choices, required=False, allow_blank=True)
    is_gxp = serializers.BooleanField(required=False, allow_null=True, help_text=COMPLIANCE_WRITE)
    validation_status = serializers.ChoiceField(
        ValidationStatus.choices, required=False, allow_null=True, help_text=COMPLIANCE_WRITE
    )
    validation_doc_url = serializers.URLField(
        required=False, allow_null=True, max_length=2000, help_text=COMPLIANCE_WRITE
    )
    last_periodic_review = serializers.DateField(required=False, allow_null=True, help_text=COMPLIANCE_WRITE)
    next_periodic_review = serializers.DateField(
        required=False, allow_null=True, help_text=COMPLIANCE_WRITE + " After the last periodic review."
    )
    risk_tier = serializers.ChoiceField(RiskTier.choices, required=False, allow_null=True, help_text=COMPLIANCE_WRITE)
    custom_field_data = serializers.DictField(required=False, help_text=CUSTOM_FIELDS_WRITE)


class ApplicationInputSerializer(RecordInputSerializer):
    vendor = serializers.UUIDField(required=False, allow_null=True)
    support_url = serializers.URLField(required=False)
    category = serializers.UUIDField(required=False, allow_null=True, help_text="A software category ID.")
    deployment_model = serializers.ChoiceField(DeploymentModel.choices, required=False, allow_blank=True)
    version = serializers.CharField(required=False, allow_blank=True, max_length=50)
    primary_url = serializers.URLField(required=False, allow_blank=True, max_length=2000)
    admin_url = serializers.URLField(required=False, allow_blank=True, max_length=2000)
    authentication_method = serializers.ChoiceField(AuthenticationMethod.choices, required=False, allow_blank=True)
    sso_enabled = serializers.BooleanField(required=False, allow_null=True, help_text="Null: not recorded.")
    scim_provisioning = serializers.BooleanField(required=False, allow_null=True, help_text="Null: not recorded.")
    entra_app_id = serializers.CharField(
        required=False, allow_blank=True, help_text="The Entra app registration's application (client) ID, a GUID."
    )
    service_principal_id = serializers.CharField(
        required=False, allow_blank=True, help_text="The Entra service principal's object ID, a GUID."
    )


class ComponentInputSerializer(RecordInputSerializer):
    component_type = serializers.ChoiceField(ComponentType.choices, required=False)
    host_runtime = serializers.CharField(required=False, max_length=200)
    repository_url = serializers.URLField(required=False)
    hosting = serializers.ChoiceField(Hosting.choices, required=False, allow_blank=True)
    runbook_url = serializers.URLField(required=False, allow_blank=True, max_length=2000)
    schedule_or_trigger = serializers.CharField(required=False, allow_blank=True, max_length=200)
    environment = serializers.ChoiceField(Environment.choices, required=False, allow_blank=True)
    azure_resource_id = serializers.CharField(required=False, allow_blank=True, max_length=500)
    pp_environment_id = serializers.CharField(
        required=False, allow_blank=True, max_length=64, help_text="Power Platform environment ID."
    )
    external_id = serializers.CharField(required=False, allow_blank=True, max_length=200)


class AIAgentInputSerializer(RecordInputSerializer):
    platform = serializers.ChoiceField(AgentPlatform.choices, required=False, allow_blank=True)
    models_used = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        max_length=50,
        help_text="AI model IDs; replaces the agent's models. An empty list removes them all.",
    )
    environment = serializers.ChoiceField(Environment.choices, required=False, allow_blank=True)
    pp_environment_id = serializers.CharField(
        required=False, allow_blank=True, max_length=64, help_text="Power Platform environment ID."
    )
    channels = serializers.ListField(
        child=serializers.ChoiceField(AgentChannel.choices),
        required=False,
        help_text="Replaces the agent's channels; each at most once.",
    )
    knowledge_sources = serializers.CharField(required=False, allow_blank=True, help_text="At most 5,000 characters.")
    tools_connectors = serializers.CharField(required=False, allow_blank=True, help_text="At most 5,000 characters.")
    auth_mode = serializers.ChoiceField(AgentAuthMode.choices, required=False, allow_blank=True)
    run_as_identity = serializers.CharField(required=False, allow_blank=True, max_length=200)
    data_access_scope = serializers.CharField(
        required=False, allow_blank=True, help_text="What data the agent can reach; at most 5,000 characters."
    )
    human_in_loop = serializers.BooleanField(required=False, allow_null=True, help_text="Null: no decision recorded.")
    actions_can_write = serializers.BooleanField(required=False, allow_null=True, help_text="Null: not recorded.")
    risk_classification = serializers.ChoiceField(AgentRisk.choices, required=False, allow_blank=True)
    repo_url = serializers.URLField(required=False, allow_blank=True, max_length=2000)
    external_id = serializers.CharField(required=False, allow_blank=True, max_length=200)


class InstanceInputSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="Unique per application, ignoring case.")
    environment = serializers.CharField(required=False, help_text="For example Production or Sandbox.")
    url = serializers.URLField(required=False)
    status = serializers.ChoiceField(InstanceStatus.choices, required=False)


# ---- people, teams, departments, vendors ------------------------------------------------------------------------------


class PersonSerializer(PersonBriefSerializer):
    """The People & Teams view of a Person: adds the linked user and how many unarchived records they own
    (`responsible`; from the context's `counts`)."""

    user = UserBriefSerializer(read_only=True, allow_null=True)
    responsible = serializers.SerializerMethodField()

    def get_responsible(self, person) -> int:
        return self.context["counts"].get(person.id, 0)


class ResponsibilityRefSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    kind = serializers.ChoiceField(Kind.choices)
    name = serializers.CharField()
    archived = serializers.BooleanField()


class RenewalRefSerializer(ReadOnlySerializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    archived = serializers.BooleanField()


class PersonDetailSerializer(PersonSerializer):
    """Adds what the Person owns or has confirmed, from the context's
    `responsibilities` {person_id: {...}}."""

    business_owner = serializers.SerializerMethodField()
    technical_owner = serializers.SerializerMethodField()
    confirmed = serializers.SerializerMethodField()
    review_count = serializers.SerializerMethodField()
    latest_review = serializers.SerializerMethodField()
    renewals = serializers.SerializerMethodField()

    def _of(self, person):
        return self.context["responsibilities"][person.id]

    @extend_schema_field(ResponsibilityRefSerializer(many=True))
    def get_business_owner(self, person) -> list[dict]:
        return self._of(person)["business_owner"]

    @extend_schema_field(ResponsibilityRefSerializer(many=True))
    def get_technical_owner(self, person) -> list[dict]:
        return self._of(person)["technical_owner"]

    @extend_schema_field(ResponsibilityRefSerializer(many=True))
    def get_confirmed(self, person) -> list[dict]:
        return self._of(person)["confirmed"]

    def get_review_count(self, person) -> int:
        return self._of(person)["review_count"]

    @extend_schema_field(serializers.DateField(allow_null=True))
    def get_latest_review(self, person) -> str | None:
        return self._of(person)["latest_review"]

    @extend_schema_field(RenewalRefSerializer(many=True))
    def get_renewals(self, person) -> list[dict]:
        return self._of(person)["renewals"]


class PersonInputSerializer(serializers.Serializer):
    name = serializers.CharField()
    email = serializers.EmailField(required=False, allow_blank=True)
    title = serializers.CharField(required=False, allow_blank=True)
    department = serializers.UUIDField(required=False, allow_null=True)
    user = serializers.IntegerField(required=False, allow_null=True, help_text="A user of this organization.")
    active = serializers.BooleanField(required=False, help_text="PATCH only.")


class AnonymizeInputSerializer(serializers.Serializer):
    confirm_name = serializers.CharField(help_text="The Person's current name, typed to confirm. Can't be undone.")


class NamedSerializer(ReadOnlySerializer):
    """A team, department or vendor with how many rows use it (`usage`, from the context's `usage` counts)."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    usage = serializers.SerializerMethodField()

    def get_usage(self, obj) -> int:
        return self.context["usage"].get(obj.id, 0)


class TeamSerializer(NamedSerializer):
    """`members` comes from the context's `members` {team_id: [Person]}, read once per page."""

    description = serializers.CharField(read_only=True)
    members = serializers.SerializerMethodField(help_text="The team's people, by name. Optional.")

    @extend_schema_field(PersonBriefSerializer(many=True))
    def get_members(self, team) -> list[dict[str, Any]]:
        return [represent(PersonBriefSerializer, p) for p in self.context["members"].get(team.id, ())]


class DepartmentSerializer(NamedSerializer):
    pass


class VendorSerializer(CustomFieldValuesMixin, NamedSerializer):
    website = serializers.CharField(read_only=True)
    support_url = serializers.CharField(read_only=True)
    support_email = serializers.CharField(read_only=True)
    support_phone = serializers.CharField(read_only=True)
    portal_url = serializers.CharField(read_only=True, help_text="The vendor's customer or admin portal.")
    account_manager = PersonBriefSerializer(read_only=True, allow_null=True, help_text="A Person in the catalog.")
    notes = serializers.CharField(read_only=True)


class VendorDetailSerializer(VendorSerializer):
    """One vendor (GET and PATCH /vendors/{id}): the list's fields and its risk summary."""

    risk = serializers.SerializerMethodField(
        help_text="The vendor risk file at a glance (GET /vendors/{id}/risk for all of it). Null without "
        "the vendor_risk capability."
    )

    @extend_schema_field(VendorRiskSummarySerializer(allow_null=True))
    def get_risk(self, obj) -> dict[str, Any] | None:
        return self.context.get("risk", {}).get(obj.id)


class SoftwareCategorySerializer(NamedSerializer):
    """`usage`: the applications in the category."""

    description = serializers.CharField(read_only=True)


class TeamInputSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="Unique in the organization, ignoring case.")
    description = serializers.CharField(required=False, allow_blank=True)
    members = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        max_length=500,
        help_text="Person IDs; replaces the team's members. An empty list removes them all.",
    )


class DepartmentInputSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="Unique in the organization, ignoring case.")


class VendorInputSerializer(serializers.Serializer):
    name = serializers.CharField(help_text="Unique in the organization, ignoring case.")
    website = serializers.URLField(required=False, allow_blank=True)
    support_url = serializers.URLField(required=False, allow_blank=True, max_length=2000)
    support_email = serializers.EmailField(required=False, allow_blank=True)
    support_phone = serializers.CharField(required=False, allow_blank=True, max_length=50)
    portal_url = serializers.URLField(required=False, allow_blank=True, max_length=2000)
    account_manager = serializers.UUIDField(required=False, allow_null=True, help_text="A Person ID.")
    notes = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    custom_field_data = serializers.DictField(required=False, help_text=CUSTOM_FIELDS_WRITE)


class AIProviderSerializer(NamedSerializer):
    """`usage`: the provider's models."""

    website = serializers.CharField(read_only=True)


class AIProviderInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, help_text="Unique in the organization, ignoring case.")
    website = serializers.URLField(required=False, allow_blank=True, max_length=2000)


class AIModelSerializer(ReadOnlySerializer):
    """`usage`: the agents using the model, archived ones included (from the context's `usage` counts)."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    version = serializers.CharField(read_only=True)
    provider = BriefSerializer(read_only=True)
    modality = serializers.CharField(read_only=True, help_text=f"One of {', '.join(ModelModality.values)}, or empty.")
    hosting_region = serializers.CharField(read_only=True, help_text="For example 'Sweden Central' or 'EU'.")
    usage = serializers.SerializerMethodField()

    def get_usage(self, obj) -> int:
        return self.context["usage"].get(obj.id, 0)


class AIModelInputSerializer(serializers.Serializer):
    provider = serializers.UUIDField(help_text="An AI provider ID.")
    name = serializers.CharField(max_length=200, help_text="Unique per provider with the version, ignoring case.")
    version = serializers.CharField(required=False, allow_blank=True, max_length=100)
    modality = serializers.ChoiceField(ModelModality.choices, required=False, allow_blank=True)
    hosting_region = serializers.CharField(required=False, allow_blank=True, max_length=100)


class ModelInUseSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    version = serializers.CharField()
    modality = serializers.CharField()
    hosting_region = serializers.CharField()
    provider = BriefSerializer()
    agents = serializers.IntegerField(help_text="Unarchived agents using it.")
    live_agents = serializers.IntegerField(help_text="Of those, the Active or Deprecated ones.")


class RelationshipTypeSerializer(ReadOnlySerializer):
    """`usage`: the relationships of this type. A core type (`is_core`) can't be deleted and its slug and
    `is_dependency` are fixed; its labels and description can be edited."""

    id = serializers.UUIDField(read_only=True)
    slug = serializers.CharField(read_only=True, help_text="A relationship's `type` on the wire; never changes.")
    name = serializers.CharField(read_only=True, help_text='Reads from the source: "Depends on".')
    inverse_name = serializers.CharField(read_only=True, help_text='Reads from the target: "Is needed by".')
    description = serializers.CharField(read_only=True)
    is_dependency = serializers.BooleanField(
        read_only=True, help_text="Dependency traversal, retirement checks and affected sets follow it."
    )
    is_core = serializers.BooleanField(read_only=True)
    usage = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def get_usage(self, obj) -> int:
        return self.context["usage"].get(obj.id, 0)


class RelationshipTypeInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, help_text="Unique in the organization, ignoring case.")
    slug = serializers.SlugField(
        required=False,
        max_length=50,
        help_text="Create only: lower-case letters, digits and underscores; derived from the name when left out. It "
        "never changes.",
    )
    inverse_name = serializers.CharField(required=False, allow_blank=True, max_length=100)
    description = serializers.CharField(required=False, allow_blank=True, max_length=200)
    is_dependency = serializers.BooleanField(required=False, help_text="Default false. Fixed on the core types.")


class SoftwareCategoryInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, help_text="Unique in the organization, ignoring case.")
    description = serializers.CharField(required=False, allow_blank=True, max_length=200)


# ---- reviews and link confirmations ---------------------------------------------------------------------------------


class ReviewSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    confirmed_by = PersonBriefSerializer(read_only=True)
    recorded_by = UserBriefSerializer(read_only=True)
    confirmation_date = serializers.DateField(read_only=True)
    next_review_date = serializers.DateField(read_only=True)
    evidence = serializers.CharField(read_only=True)
    inactive_reason = serializers.CharField(read_only=True)
    confirmed_values = serializers.DictField(
        read_only=True, help_text="The five confirmed fields as they were when recorded."
    )


class ReviewInputSerializer(serializers.Serializer):
    confirmed_by = serializers.UUIDField(help_text="The Person who confirmed the values.")
    evidence = serializers.CharField(help_text="How the confirmation was obtained.")
    confirmation_date = serializers.DateField(required=False, help_text="Defaults to today; not in the future.")
    next_review_date = serializers.DateField(
        required=False, help_text="Defaults to the confirmation date plus the organization's review interval."
    )
    inactive_reason = serializers.CharField(required=False, help_text="Required when the confirmer is inactive.")


class ConfirmationSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    confirmed_by = PersonBriefSerializer(read_only=True)
    recorded_by = UserBriefSerializer(read_only=True)
    confirmed_on = serializers.DateField(read_only=True)
    evidence = serializers.CharField(read_only=True)
    inactive_reason = serializers.CharField(read_only=True)
    confirmed_signature = serializers.DictField(
        read_only=True,
        help_text="What was confirmed: type, source, source_instance, target, target_instance and owner (IDs). For "
        "a relationship the owner is the source record's technical owner.",
    )
    created_at = serializers.DateTimeField(read_only=True)


class ConfirmationInputSerializer(serializers.Serializer):
    confirmed_by = serializers.UUIDField(help_text="The Person who confirmed the link.")
    evidence = serializers.CharField(help_text="How the confirmation was obtained; at most 500 characters.")
    confirmed_on = serializers.DateField(required=False, help_text="Defaults to today; not in the future.")
    inactive_reason = serializers.CharField(required=False, help_text="Required when the confirmer is inactive.")


# ---- relationships and integrations ---------------------------------------------------------------------------------


class LinkStateMixin(serializers.Serializer):
    """The derived confirmation state of a relationship or integration, from the context's `states`
    {link_id: {link_state, link_why, last_confirmed_on}}, computed once per page from one confirmations query."""

    link_state = serializers.SerializerMethodField(
        help_text="Unconfirmed: never confirmed. Needs verification: the newest confirmation is older than the "
        "organization's review interval, or an endpoint, instance, type or owner differs from what was confirmed. "
        "A record review never confirms links."
    )
    link_why = serializers.SerializerMethodField(help_text="For example 'Confirmed 2026-06-01; interval 90 days.'")
    last_confirmed_on = serializers.SerializerMethodField()

    def _state(self, obj) -> dict[str, Any]:
        return self.context["states"][obj.pk]

    @extend_schema_field(serializers.ChoiceField(LINK_STATES))
    def get_link_state(self, obj) -> str:
        return self._state(obj)["link_state"]

    def get_link_why(self, obj) -> str:
        return self._state(obj)["link_why"]

    @extend_schema_field(serializers.DateField(allow_null=True))
    def get_last_confirmed_on(self, obj) -> str | None:
        return self._state(obj)["last_confirmed_on"]


class EndSerializer(ReadOnlySerializer):
    """One end of a link: the record (an application or component) and, optionally, one of its instances. Rendered
    from a (resource, instance) pair."""

    id = serializers.SerializerMethodField()
    kind = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    archived = serializers.SerializerMethodField(help_text="Archiving a record keeps its links.")
    instance = serializers.SerializerMethodField(help_text="The application instance this end is about, if any.")

    def get_id(self, end) -> str:
        return str(end[0].id)

    @extend_schema_field(serializers.ChoiceField(Kind.choices))
    def get_kind(self, end) -> str:
        return end[0].kind

    def get_name(self, end) -> str:
        return end[0].name

    def get_archived(self, end) -> bool:
        return end[0].archived_at is not None

    @extend_schema_field(BriefSerializer(allow_null=True))
    def get_instance(self, end) -> dict[str, str] | None:
        instance = end[1]
        return {"id": str(instance.id), "name": instance.name} if instance else None

    def to_representation(self, instance):
        """The method fields called directly: a link list renders two ends per row (see core.drf.BriefSerializer)."""
        if type(self) is not EndSerializer:
            return super().to_representation(instance)
        return {
            "id": self.get_id(instance),
            "kind": self.get_kind(instance),
            "name": self.get_name(instance),
            "archived": self.get_archived(instance),
            "instance": self.get_instance(instance),
        }


class RelationshipTypeBriefSerializer(ReadOnlySerializer):
    """A relationship's type with its labels: the link reads "<source> <name> <target>" and, from the target,
    "<target> <inverse_name> <source>"."""

    id = serializers.UUIDField(read_only=True)
    slug = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    inverse_name = serializers.CharField(read_only=True)
    is_dependency = serializers.BooleanField(read_only=True)
    is_core = serializers.BooleanField(read_only=True)


def relationship_type_brief(rtype) -> dict[str, Any]:
    return {
        "id": str(rtype.id),
        "slug": rtype.slug,
        "name": rtype.name,
        "inverse_name": rtype.inverse_name,
        "is_dependency": rtype.is_dependency,
        "is_core": rtype.is_core,
    }


class RelationshipSerializer(LinkStateMixin, ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    type = serializers.CharField(
        source="type.slug",
        read_only=True,
        help_text="The relationship type's slug: depends_on, implements, replaces, or a type an Admin added.",
    )
    relationship_type = serializers.SerializerMethodField(help_text="The type with its labels.")
    source = serializers.SerializerMethodField(  # type: ignore[assignment]  # the field, not Field.source
        help_text="The record that depends on, implements or replaces the target."
    )
    target = serializers.SerializerMethodField()
    purpose = serializers.CharField(read_only=True)
    created_by = UserBriefSerializer(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)

    @extend_schema_field(RelationshipTypeBriefSerializer)
    def get_relationship_type(self, r) -> dict[str, Any]:
        return relationship_type_brief(r.type)

    @extend_schema_field(EndSerializer)
    def get_source(self, r) -> dict[str, Any]:
        return represent(EndSerializer, (r.source, r.source_instance))

    @extend_schema_field(EndSerializer)
    def get_target(self, r) -> dict[str, Any]:
        return represent(EndSerializer, (r.target, r.target_instance))


class RelationshipLimitedSerializer(serializers.Serializer):
    """GET relationships with `?limit=`: the first `limit` links in the list's order, and how many match in all."""

    results = RelationshipSerializer(many=True)
    count = serializers.IntegerField(help_text="Every link the filters match, not only those returned.")
    has_more = serializers.BooleanField(help_text="count is more than the links returned.")


class RelationshipInputSerializer(serializers.Serializer):
    """Create needs type, source and target. On PATCH send only what changes; the result is checked as a whole.
    Endpoint matrix: depends_on takes applications, components and AI agents at both ends; implements goes from
    a component to an integration; replaces needs the same kind at both ends and is refused if it would form a cycle.
    A type an Admin added follows the depends_on rules."""

    type = serializers.CharField(
        help_text="A relationship type slug of the organization: depends_on, implements, replaces, or an added one."
    )
    source = serializers.UUIDField()  # type: ignore[assignment]
    target = serializers.UUIDField()
    source_instance = serializers.UUIDField(
        required=False, allow_null=True, help_text="An instance of the source application."
    )
    target_instance = serializers.UUIDField(
        required=False, allow_null=True, help_text="An instance of the target application."
    )
    purpose = serializers.CharField(required=False, help_text="Why the link exists; at most 5,000 characters.")


class ImplementerSerializer(BriefSerializer):
    archived = serializers.BooleanField()


class IntegrationSerializer(CustomFieldValuesMixin, LinkStateMixin, ReadOnlySerializer):
    """An integration (a Resource with its integration row). `implemented_by` comes from the context's
    `implementers` {integration_id: [components]}; vendor-native is the absence of any."""

    id = serializers.UUIDField(read_only=True)
    kind = serializers.ChoiceField(Kind.choices, read_only=True)
    name = serializers.CharField(read_only=True)
    slug = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    lifecycle = serializers.ChoiceField(Lifecycle.choices, read_only=True)
    data_purpose = serializers.CharField(source="integration.data_purpose", read_only=True)
    protocol = serializers.CharField(source="integration.protocol", read_only=True)
    schedule = serializers.CharField(source="integration.schedule", read_only=True)
    documentation_url = serializers.CharField(source="integration.documentation_url", read_only=True)
    # Integration fields. Empty: not recorded.
    direction = serializers.CharField(
        source="integration.direction",
        read_only=True,
        help_text=f"One of {', '.join(IntegrationDirection.values)}, or empty when not recorded.",
    )
    auth_method = serializers.CharField(
        source="integration.auth_method",
        read_only=True,
        help_text=f"One of {', '.join(IntegrationAuthMethod.values)}, or empty when not recorded.",
    )
    monitoring_url = serializers.CharField(source="integration.monitoring_url", read_only=True)
    data_classification = serializers.CharField(
        read_only=True, help_text=f"One of {', '.join(DataClassification.values)}, or empty when not recorded."
    )
    # Data-flow map: what kinds of data it carries, and whether that includes personal data (derived).
    data_categories = serializers.ListField(
        source="integration.data_categories",
        child=serializers.ChoiceField(DataCategory.choices),
        read_only=True,
        help_text="What kinds of data it carries, in a fixed order; empty when not recorded.",
    )
    contains_personal_data = serializers.BooleanField(
        source="integration.contains_personal_data",
        read_only=True,
        help_text="True when its data categories include personal, special-category personal or health data (derived).",
    )
    source = serializers.SerializerMethodField(  # type: ignore[assignment]  # the field, not Field.source
        help_text="The application or component the data comes from."
    )
    target = serializers.SerializerMethodField(help_text="The application or component the data goes to.")
    technical_owner = PersonBriefSerializer(read_only=True, allow_null=True)
    support_team = BriefSerializer(read_only=True, allow_null=True)
    it_steward = UserBriefSerializer(read_only=True, allow_null=True)
    archived = serializers.SerializerMethodField()
    vendor_native = serializers.SerializerMethodField(help_text="True when no component implements it (derived).")
    implemented_by = serializers.SerializerMethodField(help_text="Components with an implements link to it.")

    @extend_schema_field(EndSerializer)
    def get_source(self, r) -> dict[str, Any]:
        ext = r.integration
        return represent(EndSerializer, (ext.source_resource, ext.source_instance))

    @extend_schema_field(EndSerializer)
    def get_target(self, r) -> dict[str, Any]:
        ext = r.integration
        return represent(EndSerializer, (ext.target_resource, ext.target_instance))

    def get_archived(self, r) -> bool:
        return r.archived_at is not None

    def get_vendor_native(self, r) -> bool:
        return not self.context["implementers"].get(r.id)

    @extend_schema_field(ImplementerSerializer(many=True))
    def get_implemented_by(self, r) -> list[dict[str, Any]]:
        return self.context["implementers"].get(r.id, [])


class IntegrationDetailSerializer(ArchivedMixin, IntegrationSerializer):
    pass


class IntegrationInputSerializer(serializers.Serializer):
    """Create needs name, lifecycle, source and target; every other field is optional and may be sent alone on
    PATCH, which is checked as a whole. Source and target are applications or components, never integrations; the
    same record at both ends needs two different instances. Integrations have a technical owner, a support team and
    an IT steward only and no criticality."""

    name = serializers.CharField()
    lifecycle = serializers.ChoiceField(Lifecycle.choices)
    source = serializers.UUIDField()  # type: ignore[assignment]
    target = serializers.UUIDField()
    source_instance = serializers.UUIDField(
        required=False, allow_null=True, help_text="An instance of the source application."
    )
    target_instance = serializers.UUIDField(
        required=False, allow_null=True, help_text="An instance of the target application."
    )
    description = serializers.CharField(required=False)
    data_purpose = serializers.CharField(required=False, help_text="What data moves and why; at most 5,000 characters.")
    protocol = serializers.CharField(required=False, help_text="For example SFTP or REST; at most 100 characters.")
    schedule = serializers.CharField(required=False, help_text="For example nightly at 02:00; at most 200 characters.")
    documentation_url = serializers.URLField(required=False)
    direction = serializers.ChoiceField(IntegrationDirection.choices, required=False, allow_blank=True)
    auth_method = serializers.ChoiceField(
        IntegrationAuthMethod.choices,
        required=False,
        allow_blank=True,
        help_text="How it authenticates to the other end.",
    )
    monitoring_url = serializers.URLField(
        required=False, allow_blank=True, max_length=2000, help_text="Where its runs or health are watched."
    )
    data_classification = serializers.ChoiceField(DataClassification.choices, required=False, allow_blank=True)
    data_categories = serializers.ListField(
        child=serializers.ChoiceField(DataCategory.choices),
        required=False,
        allow_null=True,
        help_text="Replaces its data categories; each at most once, kept in a fixed order. Null or [] clears them.",
    )
    technical_owner = serializers.UUIDField(required=False, allow_null=True, help_text="An active Person.")
    support_team = serializers.UUIDField(required=False, allow_null=True)
    it_steward = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="An Admin or Editor user: a user ID as an integer (a string of digits is accepted too).",
    )
    confirm_duplicate = serializers.BooleanField(
        required=False, help_text="Save even though another integration has the same name."
    )
    custom_field_data = serializers.DictField(required=False, help_text=CUSTOM_FIELDS_WRITE)


# ---- dependency explorer (catalog/graph.py builds the response) ---------------------------------------------------


class DependencyStepSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    kind = serializers.ChoiceField(Kind.choices)
    via = serializers.DictField(
        allow_null=True,
        help_text="The link this step was reached by: relationship ID, type (slug) and type_name. Null for the target.",
    )
    instance = serializers.CharField(required=False, allow_null=True, help_text="With show_instances: its instance.")
    previous_instance = serializers.CharField(
        required=False, allow_null=True, help_text="With show_instances: the instance of the step before."
    )


class DependencyLinkSerializer(serializers.Serializer):
    relationship = serializers.UUIDField(required=False, help_text="The link this row was reached by; nodes only.")
    state = serializers.ChoiceField(LINK_STATES)
    why = serializers.CharField()
    last_confirmed_on = serializers.DateField(allow_null=True)


class DependencyOwnerSerializer(BriefSerializer):
    active = serializers.BooleanField()


class DependencyNodeSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    kind = serializers.ChoiceField(Kind.choices)
    name = serializers.CharField()
    lifecycle = serializers.ChoiceField(Lifecycle.choices)
    archived = serializers.BooleanField()
    direction = serializers.ChoiceField(["dependents", "depends_on"], help_text="Which pass reached it.")
    depth = serializers.IntegerField(help_text="Links from the target.")
    cycle = serializers.BooleanField(
        help_text="True when the link leads back to a record already on this path; the path ends there and goes no "
        "further. The record's own row has cycle false."
    )
    path = DependencyStepSerializer(many=True, help_text="From the target to this record, one path per node.")
    link = DependencyLinkSerializer()
    technical_owner = DependencyOwnerSerializer(allow_null=True)
    gaps = serializers.ListField(child=serializers.CharField())


class DependencyRefSerializer(BriefSerializer):
    kind = serializers.ChoiceField(Kind.choices)


class DependencyImplementerSerializer(ImplementerSerializer):
    lifecycle = serializers.ChoiceField(Lifecycle.choices)


class DependencyFlowSerializer(serializers.Serializer):
    id = serializers.UUIDField(help_text="The integration's ID.")
    name = serializers.CharField()
    lifecycle = serializers.ChoiceField(Lifecycle.choices)
    archived = serializers.BooleanField()
    hop = serializers.IntegerField(help_text="Always 1: integrations are listed one hop out and not followed.")
    depth = serializers.IntegerField(help_text="Depth of the record it hangs from, plus one.")
    attached_to = DependencyRefSerializer(help_text="The nearest listed record at one of its ends.")
    source = serializers.DictField()  # type: ignore[assignment]  # the field, not Field.source
    target = serializers.DictField()
    protocol = serializers.CharField()
    schedule = serializers.CharField()
    direction = serializers.CharField(help_text="one_way, two_way, or blank when not recorded.")
    auth_method = serializers.CharField(help_text="How the integration authenticates; blank when not recorded.")
    implemented_by = DependencyImplementerSerializer(many=True)
    vendor_native = serializers.BooleanField()
    link = DependencyLinkSerializer()
    technical_owner = DependencyOwnerSerializer(allow_null=True)


class DependencySummarySerializer(serializers.Serializer):
    nodes = serializers.IntegerField()
    data_flows = serializers.IntegerField()
    needs_verification = serializers.IntegerField()
    unconfirmed = serializers.IntegerField()
    cycles = serializers.IntegerField()
    hidden_retired = serializers.IntegerField(help_text="Retired records left out; pass include_retired to see them.")


class DependencyTargetSerializer(DependencyRefSerializer):
    lifecycle = serializers.ChoiceField(Lifecycle.choices)
    archived = serializers.BooleanField()


class DependencyCycleSerializer(serializers.Serializer):
    relationship = serializers.UUIDField(help_text="The link that closes the loop.")
    type = serializers.CharField(help_text="Its relationship type's slug.")
    source = serializers.UUIDField(help_text="The link's source (it depends on the target).")  # type: ignore[assignment]
    target = serializers.UUIDField()
    closes_at = serializers.UUIDField(help_text="The record already on the path that the link leads back to.")
    reached_from = serializers.UUIDField(help_text="The record the walk was at when it found the link.")
    direction = serializers.ChoiceField(["dependents", "depends_on"], help_text="Which pass found it.")
    depth = serializers.IntegerField()


class DependenciesSerializer(serializers.Serializer):
    target = DependencyTargetSerializer()
    direction = serializers.ChoiceField(["dependents", "depends_on", "both"])
    depth = serializers.CharField(help_text="1 to 10, or all.")
    include_retired = serializers.BooleanField()
    show_instances = serializers.BooleanField()
    nodes = DependencyNodeSerializer(many=True, help_text="Nearest first, then by name.")
    data_flows = DependencyFlowSerializer(many=True, help_text="Data flow, not operational. Nearest first.")
    total = serializers.IntegerField(help_text="Rows before the cap: nodes (cycle rows included) and data flows.")
    shown = serializers.IntegerField(help_text="Rows returned; at most 150.")
    capped = serializers.BooleanField()
    summary = DependencySummarySerializer(
        help_text="Counted before the cap, except needs_verification and unconfirmed, which count the rows returned."
    )
    cycles = DependencyCycleSerializer(
        many=True,
        help_text="Every link that closes a loop (the cycle rows), nearest first, at most 150; not capped with the "
        "rows, so a graph can mark them. When the rows are capped, a cycle's row and its closes_at and reached_from "
        "records may be missing from nodes.",
    )


# ---- a record's Map (catalog/record_map.py builds the response) ------------------------------------------------------


class MapNodeSerializer(serializers.Serializer):
    id = serializers.CharField(help_text="The record's, integration's, instance's or source observation's UUID.")
    kind = serializers.CharField(
        help_text="application, component, ai_agent, integration, instance, discovery_source, contract or license_pool."
    )
    name = serializers.CharField()
    lifecycle = serializers.ChoiceField(Lifecycle.choices, allow_null=True, help_text="Null for non-records.")
    archived = serializers.BooleanField()
    retired = serializers.BooleanField()
    path = serializers.CharField(allow_null=True, help_text="Its route under /api/v1/ (the SPA's route is the same).")
    map_path = serializers.CharField(allow_null=True, help_text="Its own map, for records; null otherwise.")
    expandable = serializers.BooleanField(help_text="A record: depth 2 expands it.")
    detail = serializers.DictField(
        help_text="Kind-specific: an instance's environment and status, a source's "
        "connector, external_id, decision, last_seen_at and gone_at; a contract's vendor, status, end_date, "
        "cancellation_deadline, annualized_cost and currency; a license pool's sku_id, seats and utilization."
    )
    hop = serializers.IntegerField(help_text="Steps from the record (0 for the record itself).")
    root = serializers.BooleanField()  # type: ignore[assignment]  # the field, not Field.root


class MapEdgeSerializer(serializers.Serializer):
    id = serializers.CharField(help_text="<relation kind>:<uuid>, unique across nodes and edges.")
    source = serializers.CharField()  # type: ignore[assignment]  # the field, not Field.source
    target = serializers.CharField()
    relation = serializers.CharField(
        help_text="The relationship type's slug, or integration, instance, discovery, contract, license_pool."
    )
    category = serializers.CharField(help_text="One of the response's categories.")
    label = serializers.CharField(  # type: ignore[assignment]  # the field, not Field.label
        help_text="Drawn on the edge: the type's name, or the integration's name."
    )
    reads = serializers.CharField(help_text="Read from the source: 'Depends on', 'Sends data to'.")
    reads_inverse = serializers.CharField(help_text="Read from the target: 'Is needed by', 'Receives data from'.")
    archived = serializers.BooleanField(help_text="An archived integration.")
    retired = serializers.BooleanField(help_text="A retired integration.")
    detail = serializers.DictField(
        help_text="relationship: relationship ID, type {id, slug, name, inverse_name, is_dependency}, instances, "
        "purpose. integration: integration {id, name, lifecycle, path}, protocol, direction, schedule, auth_method, "
        "instances. discovery: decision, gone."
    )


class MapRowSerializer(serializers.Serializer):
    edge = serializers.CharField()
    category = serializers.CharField()
    near = serializers.CharField(help_text="The node the connection was found from (the record, at step 1).")
    other = serializers.CharField(help_text="The node at the other end.")
    reads = serializers.CharField(help_text="The connection read from `near`.")
    hop = serializers.IntegerField(help_text="The step it was found at: 1 for the record's own connections.")


class MapCategorySerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # the field, not Field.label


class RecordMapSerializer(serializers.Serializer):
    root = serializers.UUIDField()  # type: ignore[assignment]  # the field, not Field.root
    depth = serializers.IntegerField()
    nodes = MapNodeSerializer(many=True, help_text="The record first, then nearest first.")
    edges = MapEdgeSerializer(many=True)
    rows = MapRowSerializer(many=True, help_text="One per edge: the table beside the graph, in display order.")
    categories = MapCategorySerializer(many=True, help_text="What the registered contributors produce, in order.")
    counts = serializers.DictField(
        child=serializers.IntegerField(), help_text="The record's own connections by category."
    )
    truncated = serializers.BooleanField()
    max_nodes = serializers.IntegerField()


# ---- change assessments (catalog/assessment_render.py builds the response) ------------------------------------------


class AssessmentTargetSerializer(BriefSerializer):
    kind = serializers.ChoiceField(Kind.choices)
    lifecycle = serializers.ChoiceField(Lifecycle.choices)
    archived = serializers.BooleanField()


class SnapshotCountsSerializer(serializers.Serializer):
    dependents = serializers.IntegerField()
    cycles = serializers.IntegerField(
        help_text="Paths that led back to a record already on them (the target included)."
    )
    data_flows = serializers.IntegerField(help_text="Data flow, not operational.")
    implementers = serializers.IntegerField()
    links = serializers.IntegerField()


class SnapshotSummarySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    taken_at = serializers.DateTimeField()
    taken_by = UserBriefSerializer(allow_null=True)
    is_current = serializers.BooleanField()
    stale = serializers.BooleanField(help_text="True once a relevant change was made after it was taken.")
    stale_since = serializers.DateTimeField(allow_null=True)
    stale_reasons = serializers.ListField(child=serializers.CharField(), help_text="Readable sentences, oldest first.")
    counts = SnapshotCountsSerializer()


class SnapshotOwnersSerializer(serializers.Serializer):
    business_owner = DependencyOwnerSerializer(allow_null=True)
    technical_owner = DependencyOwnerSerializer(allow_null=True)


class SnapshotNodeResourceSerializer(BriefSerializer):
    kind = serializers.ChoiceField(Kind.choices)
    archived = serializers.BooleanField()


class SnapshotNodeSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    resource = SnapshotNodeResourceSerializer()
    role = serializers.ChoiceField(
        ["dependent", "data_flow", "implementer"], help_text="Data flows are not operational."
    )
    depth = serializers.IntegerField()
    path = serializers.ListField(
        child=serializers.DictField(), help_text="From the target to this record: id, name and kind of each step."
    )
    in_cycle = serializers.BooleanField(
        help_text="A path that led back to a record already on it; not a second member."
    )
    lifecycle_then = serializers.ChoiceField(Lifecycle.choices)
    lifecycle_now = serializers.ChoiceField(Lifecycle.choices)
    owners_then = SnapshotOwnersSerializer(help_text="Names are the Person's current name (anonymized if they were).")
    owners_now = SnapshotOwnersSerializer()
    changed = serializers.DictField(child=serializers.BooleanField(), help_text="lifecycle and owners: differs now.")


class SnapshotLinkEndSerializer(BriefSerializer):
    kind = serializers.ChoiceField(Kind.choices)


class SnapshotLinkSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(["relationship", "integration"])
    id = serializers.UUIDField(allow_null=True, help_text="Null once the link was removed.")
    removed = serializers.BooleanField()
    changed = serializers.BooleanField(help_text="Removed, or its signature differs from the one saved.")
    type = serializers.CharField(help_text="A relationship type's slug, or integration.")
    type_name = serializers.CharField(
        allow_null=True,
        help_text="A relationship's type name: the type's current name, or for a type deleted since, its name when "
        "the snapshot was taken (the slug if that wasn't saved). Null for an integration.",
    )
    inverse_name = serializers.CharField(
        allow_null=True,
        help_text="The type's inverse name, chosen as type_name is; may be blank. Null for an integration.",
    )
    source = SnapshotLinkEndSerializer()  # type: ignore[assignment]  # the field, not Field.source
    target = SnapshotLinkEndSerializer()
    signature_then = serializers.DictField()
    link_state = serializers.ChoiceField(LINK_STATES, allow_null=True, help_text="Null once the link was removed.")
    link_why = serializers.CharField(allow_null=True)
    last_confirmed_on = serializers.DateField(allow_null=True)


class SnapshotSerializer(SnapshotSummarySerializer):
    nodes = SnapshotNodeSerializer(many=True)
    links = SnapshotLinkSerializer(many=True)


STATUSES = AssessmentStatus.choices


class AssessmentListSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    target = AssessmentTargetSerializer()
    change_type = serializers.ChoiceField(ChangeType.choices)
    title = serializers.CharField()
    description = serializers.CharField()
    owner = UserBriefSerializer(allow_null=True)
    target_date = serializers.DateField(allow_null=True)
    ticket_url = serializers.CharField()
    findings = serializers.CharField()
    status = serializers.ChoiceField(STATUSES)
    retirement_ack_reason = serializers.CharField()
    stale = serializers.BooleanField(help_text="The current snapshot is stale. A flag beside the status, not a status.")
    snapshot = SnapshotSummarySerializer(allow_null=True, help_text="The current snapshot, without its rows.")
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class AssessmentSerializer(AssessmentListSerializer):
    snapshot = SnapshotSerializer(allow_null=True, help_text="The current snapshot with its nodes and links.")
    history = SnapshotSummarySerializer(
        many=True, help_text="Every snapshot, newest first; earlier ones are read-only."
    )
    ticket_links = TicketLinkSerializer(
        many=True,
        help_text="Linked Jira, ServiceNow, Halo and other tickets (GET /assessments/{id}/ticket-links). "
        "Empty without the ticket_links capability.",
    )


class AssessmentInputSerializer(serializers.Serializer):
    """Create needs target, change_type and title. PATCH takes title, description, owner, target_date, ticket_url and
    findings; the target and type are fixed and the status has its own action."""

    target = serializers.UUIDField(help_text="Create only. An application or component.")
    change_type = serializers.ChoiceField(ChangeType.choices, help_text="Create only.")
    title = serializers.CharField()
    description = serializers.CharField(required=False)
    owner = serializers.IntegerField(
        required=False, allow_null=True, help_text="An Admin or Editor user. Defaults to you on create."
    )
    target_date = serializers.DateField(required=False, allow_null=True)
    ticket_url = serializers.URLField(required=False, help_text="An external ticket; http(s) only.")
    findings = serializers.CharField(required=False)
    status = serializers.ChoiceField(STATUSES, required=False, help_text="Create only. Default draft.")
    retirement_ack = serializers.BooleanField(
        required=False,
        help_text="Set true to acknowledge the active dependents when a retirement is created as Ready.",
    )
    retirement_ack_reason = serializers.CharField(
        required=False, help_text="Required with retirement_ack; saved in the audit history."
    )


class AssessmentStatusInputSerializer(serializers.Serializer):
    status = serializers.ChoiceField(STATUSES)
    retirement_ack = serializers.BooleanField(
        required=False,
        help_text="Set true to acknowledge the active dependents when a retirement assessment moves to Ready for "
        "external execution; the reason is saved on the assessment.",
    )
    retirement_ack_reason = serializers.CharField(
        required=False, help_text="Required with retirement_ack; saved in the audit history."
    )


# ---- curation: tags, saved filters, bulk edit -------------------------------------------------------------------------


class TagSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    count = serializers.IntegerField(read_only=True, help_text="Records carrying the tag.")
    color = serializers.CharField(
        read_only=True,
        help_text=f"A palette name the interface maps to its color tokens: one of {', '.join(TAG_COLORS)}, or empty "
        "for the neutral default.",
    )
    description = serializers.CharField(read_only=True)


class TagInputSerializer(serializers.Serializer):
    color = serializers.ChoiceField(
        [(c, c) for c in TAG_COLORS], required=False, allow_blank=True, help_text="Empty for the neutral default."
    )
    description = serializers.CharField(required=False, allow_blank=True, max_length=500)


class SavedViewOwnerSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class SavedViewSubscriptionBriefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    weekday = serializers.IntegerField(help_text="0 Monday to 6 Sunday.")
    active = serializers.BooleanField()


class SavedFilterSerializer(serializers.Serializer):
    id = serializers.CharField(help_text='A UUID, or "builtin:<slug>" for a built-in.')
    name = serializers.CharField()
    kind = serializers.ChoiceField(choices=RECORD_KIND_CHOICES)
    query = serializers.DictField(child=serializers.CharField(), help_text="The list's query parameters.")
    columns = serializers.ListField(
        child=serializers.CharField(),
        help_text="The list's column ids to show, in order (cf_<name> for a custom field); empty: the list's defaults.",
    )
    shared = serializers.BooleanField(help_text="Seen by everyone in the organization; otherwise by its owner only.")
    builtin = serializers.BooleanField()
    mine = serializers.BooleanField(help_text="You own it.")
    owner = SavedViewOwnerSerializer(allow_null=True, help_text="Null for built-ins and ownerless shared views.")
    can_edit = serializers.BooleanField(help_text="You may change it (PATCH).")
    can_delete = serializers.BooleanField()
    problems = serializers.ListField(
        child=serializers.CharField(),
        help_text="What the list would refuse in the query now, as 'parameter: message' (for example a custom field "
        "deleted since); empty when it runs as saved.",
    )
    subscription = SavedViewSubscriptionBriefSerializer(
        allow_null=True, help_text="Your own weekly email of this view, if any (saved-view-subscriptions)."
    )


class SavedFilterInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    kind = serializers.ChoiceField(choices=RECORD_KIND_CHOICES, help_text="On create only.")
    query = serializers.DictField(
        child=serializers.CharField(allow_blank=True),
        help_text="The list's filter parameters; validated as the list validates them.",
    )
    columns = serializers.ListField(
        child=serializers.CharField(), required=False, help_text="Column ids in order; empty for the defaults."
    )
    shared = serializers.BooleanField(required=False, default=False, help_text="Admins and Editors only.")


class BulkChangesSerializer(serializers.Serializer):
    business_owner = serializers.UUIDField(required=False, allow_null=True)
    technical_owner = serializers.UUIDField(required=False, allow_null=True)
    support_team = serializers.UUIDField(required=False, allow_null=True)
    add_tags = serializers.ListField(child=serializers.CharField(), required=False)
    remove_tags = serializers.ListField(child=serializers.CharField(), required=False)
    next_review_date = serializers.DateField(required=False, help_text="Sets the latest review's next review date.")
    it_steward = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="An Admin or Editor user: a user ID as an integer (a string of digits is accepted too); null clears "
        "it.",
    )
    department = serializers.UUIDField(required=False, allow_null=True)
    lifecycle = serializers.ChoiceField(
        Lifecycle.choices,
        required=False,
        help_text="Retiring a record that active records outside the selection depend on is refused for that record "
        "(retire it on its own page to acknowledge them); dependents in the selection are retired first.",
    )
    criticality = serializers.ChoiceField(
        Criticality.choices, required=False, allow_null=True, allow_blank=True, help_text="Null or empty clears it."
    )
    custom_field_data = serializers.DictField(
        required=False,
        help_text="Custom field values by field name, validated as a single edit validates them; null, an empty "
        "string, [] or {} clears one (refused for a required field). Errors are keyed custom_field_data.<name>.",
    )


class BulkInputSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=RECORD_KIND_CHOICES)
    ids = serializers.ListField(child=serializers.UUIDField(), max_length=200)
    changes = BulkChangesSerializer()


class BulkRowSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    changes = serializers.DictField(help_text="Per changed field: {before, after}.")
    unchanged = serializers.BooleanField()
    will_need_confirmation = serializers.BooleanField()
    errors = serializers.ListField(child=serializers.CharField())  # type: ignore[assignment]
    codes = serializers.ListField(
        child=serializers.ChoiceField(ROW_CODES),
        help_text="A stable code for each message in errors, in the same order: key off these, not the wording.",
    )


class BulkSummarySerializer(serializers.Serializer):
    total = serializers.IntegerField()
    changed = serializers.IntegerField()
    unchanged = serializers.IntegerField()
    errors = serializers.IntegerField()  # type: ignore[assignment]
    will_need_confirmation = serializers.IntegerField()


class BulkPreviewSerializer(serializers.Serializer):
    kind = serializers.CharField()
    summary = BulkSummarySerializer()
    rows = BulkRowSerializer(many=True)


class BulkResultSerializer(BulkPreviewSerializer):
    updated = serializers.IntegerField()
    unchanged = serializers.IntegerField()


class BulkRefusedRowSerializer(serializers.Serializer):
    """A row of a refused bulk edit's 400 problem (`rows`)."""

    id = serializers.UUIDField()
    name = serializers.CharField()
    errors = serializers.ListField(child=serializers.CharField())  # type: ignore[assignment]
    codes = serializers.ListField(
        child=serializers.ChoiceField(ROW_CODES),
        help_text="A stable code for each message in errors, in the same order (conflict: the record's own write was "
        "refused with a 409).",
    )
    field_errors = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()),
        help_text="The single-edit field errors this record's write was refused with (for example "
        "custom_field_data.<name>); empty when the refusal is in `errors`.",
    )


class BulkRefusedSerializer(ProblemSerializer):
    """The 400 problem of a refused bulk edit apply: the usual problem members and the refused `rows`."""

    rows = BulkRefusedRowSerializer(many=True, required=False, help_text="With a refused apply: each refused record.")


class BulkDeleteInputSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=RECORD_KIND_CHOICES)
    ids = serializers.ListField(child=serializers.UUIDField(), max_length=200)


class BulkDeleteRemovesSerializer(serializers.Serializer):
    attachments = serializers.IntegerField(help_text="Attachments deleted with the record (each audited).")
    quality_exceptions = serializers.IntegerField(help_text="Data-quality exceptions deleted with the record.")


class BulkDeleteRowSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    can_delete = serializers.BooleanField()
    blocking_references = serializers.DictField(
        child=serializers.IntegerField(),
        help_text="What references the record, by noun with a count (as a single delete's 409); empty when it can be "
        "deleted.",
    )
    removes = BulkDeleteRemovesSerializer()
    errors = serializers.ListField(child=serializers.CharField())  # type: ignore[assignment]


class BulkDeleteSummarySerializer(serializers.Serializer):
    total = serializers.IntegerField()
    deletable = serializers.IntegerField()
    blocked = serializers.IntegerField()


class BulkDeletePreviewSerializer(serializers.Serializer):
    kind = serializers.CharField()
    summary = BulkDeleteSummarySerializer()
    rows = BulkDeleteRowSerializer(many=True)


class BulkDeletedRowSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()


class BulkDeleteResultSerializer(serializers.Serializer):
    kind = serializers.CharField()
    deleted = serializers.IntegerField()
    rows = BulkDeletedRowSerializer(many=True)


class BulkDeleteRefusedRowSerializer(serializers.Serializer):
    """A row of a refused bulk delete's 409 problem (`rows`)."""

    id = serializers.UUIDField()
    name = serializers.CharField()
    blocking_references = serializers.DictField(child=serializers.IntegerField())
    errors = serializers.ListField(child=serializers.CharField())  # type: ignore[assignment]


# ---- custom fields ------------------------------------------------------------------------------------------


class CustomFieldSerializer(ReadOnlySerializer):
    """A custom field definition. `usage`: the records and vendors holding a value (from the context's `usage`)."""

    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(
        read_only=True,
        help_text="The key in custom_field_data, the cf_<name> CSV column and list filter. Never changes.",
    )
    label = serializers.CharField(read_only=True)  # type: ignore[assignment]  # the API field, not Field.label
    type = serializers.ChoiceField(CustomFieldType.choices, read_only=True)
    description = serializers.CharField(read_only=True)
    choices = serializers.ListField(child=serializers.CharField(), read_only=True, help_text="Choice fields only.")
    required = serializers.BooleanField(read_only=True)  # type: ignore[assignment]  # the API field
    default = serializers.JSONField(read_only=True, allow_null=True, help_text="Applied when a record is created.")
    applies_to = serializers.ListField(child=serializers.ChoiceField(list(CUSTOM_FIELD_KINDS)), read_only=True)
    weight = serializers.IntegerField(read_only=True, help_text="Lower first.")
    is_reminder_source = serializers.BooleanField(
        read_only=True, help_text="Date fields only: reminders from notification rules."
    )
    usage = serializers.SerializerMethodField()

    def get_usage(self, obj) -> int:
        return self.context["usage"].get(obj.name, 0)


class CustomFieldInputSerializer(serializers.Serializer):
    """Create needs name, label, type and applies_to (and choices for choice fields); a PATCH sends what changes."""

    name = serializers.CharField(
        max_length=50,
        help_text="Lowercase letters, digits and single underscores, starting with a letter; unique in the "
        "organization. Can't change after creation.",
    )
    label = serializers.CharField(  # type: ignore[assignment]  # the API field, not Field.label
        max_length=100, help_text="Unique in the organization, ignoring case."
    )
    type = serializers.ChoiceField(
        CustomFieldType.choices,
        help_text="Can't change while a record holds a value for the field.",
    )
    description = serializers.CharField(required=False, allow_blank=True, max_length=500)
    choices = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False,
        max_length=100,
        help_text="select and multiselect only: at least one, no commas or semicolons. A choice in use can't be removed.",
    )
    required = serializers.BooleanField(required=False)  # type: ignore[assignment]  # the API field
    default = serializers.JSONField(required=False, allow_null=True, help_text="A valid value for the type, or null.")
    applies_to = serializers.ListField(
        child=serializers.ChoiceField(list(CUSTOM_FIELD_KINDS)),
        help_text="At least one. A kind can't be removed while records of that kind hold a value.",
    )
    weight = serializers.IntegerField(required=False, min_value=0, max_value=32767, help_text="Default 100.")
    is_reminder_source = serializers.BooleanField(required=False, help_text="Date fields only.")


# ---- attachments ---------------------------------------------------------------------------------------------


class AttachmentParentSerializer(serializers.Serializer):
    type = serializers.CharField(help_text="application, component, integration, ai_agent, vendor or contract.")
    id = serializers.UUIDField()
    name = serializers.CharField()


class AttachmentSerializer(ReadOnlySerializer):
    """A file or link on a record or vendor. A file is read only through `download_url` (signed in, any role of the
    organization); the stored file's own name is never shown."""

    id = serializers.UUIDField(read_only=True)
    kind = serializers.ChoiceField(AttachmentKind.choices, read_only=True)
    title = serializers.CharField(read_only=True)
    category = serializers.ChoiceField(AttachmentCategory.choices, read_only=True)
    url = serializers.CharField(read_only=True, help_text="A link's web address; empty for a file.")
    filename = serializers.CharField(read_only=True, help_text="A file's name as uploaded (cleaned); empty for a link.")
    content_type = serializers.CharField(
        read_only=True, help_text="A file's type as its content check found it (not the browser's claim)."
    )
    size = serializers.IntegerField(read_only=True, allow_null=True, help_text="Bytes; null for a link.")
    sha256 = serializers.CharField(read_only=True, help_text="A file's SHA-256 (hex); empty for a link.")
    download_url = serializers.SerializerMethodField(help_text="A file's download path; null for a link.")
    attached_to = serializers.SerializerMethodField()
    uploaded_by = UserBriefSerializer(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def get_download_url(self, attachment) -> str | None:
        if attachment.kind != AttachmentKind.FILE:
            return None
        return f"/api/v1/attachments/{attachment.id}/download"

    @extend_schema_field(AttachmentParentSerializer)
    def get_attached_to(self, attachment) -> dict[str, Any]:
        if attachment.resource_id is not None:
            r = attachment.resource
            return {"type": r.kind, "id": str(r.id), "name": r.name}
        if attachment.contract_id is not None:
            c = attachment.contract
            return {"type": "contract", "id": str(c.id), "name": c.name}
        v = attachment.vendor
        return {"type": "vendor", "id": str(v.id), "name": v.name}


ATTACHMENT_TITLE = "At most 200 characters. Default: the file's name, or the link's address."


class AttachmentUploadInputSerializer(serializers.Serializer):
    """multipart/form-data: a file and, optionally, its title and category."""

    file = serializers.FileField(
        help_text="The file. Its extension must be one the organization allows and its content must match it; at most "
        "the organization's size limit (GET /organization `attachments`)."
    )
    title = serializers.CharField(required=False, max_length=200, help_text=ATTACHMENT_TITLE)
    category = serializers.ChoiceField(AttachmentCategory.choices, required=False, help_text="Default other.")


class AttachmentLinkInputSerializer(serializers.Serializer):
    """application/json: a link."""

    kind = serializers.ChoiceField(AttachmentKind.choices, required=False, help_text="link (the default here).")
    url = serializers.URLField(max_length=2000, help_text="http:// or https:// only.")
    title = serializers.CharField(required=False, max_length=200, help_text=ATTACHMENT_TITLE)
    category = serializers.ChoiceField(AttachmentCategory.choices, required=False, help_text="Default other.")


class AttachmentUpdateInputSerializer(serializers.Serializer):
    title = serializers.CharField(required=False, max_length=200)
    category = serializers.ChoiceField(AttachmentCategory.choices, required=False)
    url = serializers.URLField(required=False, max_length=2000, help_text="Links only; a file's content never changes.")


# ---- data quality (catalog/quality.py) -------------------------------------------------------------------

SEVERITY_CHOICES = ["high", "medium", "low"]


class QualityCountsSerializer(serializers.Serializer):
    records = serializers.IntegerField(help_text="Live records the rule checks (passing, failing or excepted).")
    passing = serializers.IntegerField()
    failing = serializers.IntegerField()
    excepted = serializers.IntegerField(help_text="Failing records with an exception that hasn't expired.")
    score = serializers.IntegerField(allow_null=True, help_text="passing * 100 // (passing + failing); null for none.")


class QualityRuleSerializer(serializers.Serializer):
    """A data-quality rule as the organization sees it."""

    id = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # the API field is called label
    description = serializers.CharField()
    kinds = serializers.ListField(child=serializers.ChoiceField([*RECORD_KIND_CHOICES, Kind.INTEGRATION.value]))
    severity = serializers.ChoiceField(SEVERITY_CHOICES)
    exceptable = serializers.BooleanField(help_text="Whether a record can have it marked not applicable.")
    covered_by = serializers.CharField(
        allow_null=True,
        help_text="The Needs attention queue that already lists its records (ownership_gaps, agent_gaps, reviews); "
        "such rules can't be turned off or excepted and are left out of the Data quality queue.",
    )
    status = serializers.ChoiceField(
        ["on", "off", "unavailable"],
        help_text="on; off (turned off in Organization settings); unavailable (a capability or the compliance profile "
        "is missing, or the check isn't built yet).",
    )
    status_reason = serializers.CharField(allow_blank=True, help_text="Why it is off or unavailable; empty when on.")
    spec = serializers.DictField(
        help_text="The rule as data: kinds, applies and fails conditions ({all|any: [...]} or {field, test, value}). "
        "The shape custom rules and rule packs will use."
    )
    counts = QualityCountsSerializer(allow_null=True, help_text="Live records, while the rule is on; else null.")


class QualityKindSummarySerializer(serializers.Serializer):
    kind = serializers.ChoiceField([*RECORD_KIND_CHOICES, Kind.INTEGRATION.value])
    records = serializers.IntegerField(help_text="Live records of this kind (not archived, not retired).")
    complete = serializers.IntegerField(help_text="Of those, records failing no check.")
    passing = serializers.IntegerField()
    failing = serializers.IntegerField()
    excepted = serializers.IntegerField()
    score = serializers.IntegerField(allow_null=True)


class QualityRuleSummarySerializer(QualityCountsSerializer):
    id = serializers.CharField()
    label = serializers.CharField()  # type: ignore[assignment]  # the API field is called label
    severity = serializers.ChoiceField(SEVERITY_CHOICES)


class QualityRuleStatusCountsSerializer(serializers.Serializer):
    on = serializers.IntegerField()
    off = serializers.IntegerField()
    unavailable = serializers.IntegerField()


class QualitySummarySerializer(serializers.Serializer):
    as_of = serializers.DateField(help_text="Today in the organization's timezone (exceptions and due dates).")
    score = serializers.IntegerField(
        allow_null=True,
        help_text="Compliance percentage: passing checks * 100 // (passing + failing) over every live record and "
        "every rule that is on; excepted checks left out. Null when nothing is checked.",
    )
    records = serializers.IntegerField(help_text="Live records checked.")
    complete = serializers.IntegerField(help_text="Live records failing no check.")
    passing = serializers.IntegerField()
    failing = serializers.IntegerField()
    excepted = serializers.IntegerField()
    by_kind = QualityKindSummarySerializer(many=True)
    by_rule = QualityRuleSummarySerializer(many=True, help_text="Rules that are on, most severe first.")
    rules = QualityRuleStatusCountsSerializer(help_text="How many rules are on, off and unavailable.")


class QualityFailureSerializer(ReadOnlySerializer):
    """A live record failing a data-quality check, with its results; `quality` comes from the page context."""

    id = serializers.UUIDField(read_only=True)
    kind = serializers.ChoiceField(Kind.choices, read_only=True)
    name = serializers.CharField(read_only=True)
    lifecycle = serializers.ChoiceField(Lifecycle.choices, read_only=True)
    criticality = serializers.SerializerMethodField()
    it_steward = UserBriefSerializer(read_only=True, allow_null=True)
    quality = serializers.SerializerMethodField()

    @extend_schema_field(serializers.ChoiceField(Criticality.choices, allow_null=True))
    def get_criticality(self, r) -> str | None:
        return r.criticality or None

    @extend_schema_field(QualitySerializer)
    def get_quality(self, r) -> dict[str, Any]:
        return quality_json(self.context["quality"][r.id])


class QualityExceptionSerializer(ReadOnlySerializer):
    id = serializers.UUIDField(read_only=True)
    record = RecordBriefSerializer(source="resource", read_only=True)
    rule = serializers.CharField(read_only=True)
    rule_label = serializers.SerializerMethodField()
    reason = serializers.CharField(read_only=True)
    owner = PersonBriefSerializer(read_only=True, help_text="Who answers for this decision.")
    expires_on = serializers.DateField(read_only=True, help_text="The last day it applies.")
    active = serializers.SerializerMethodField(help_text="False once expires_on has passed: the check counts again.")
    created_by = UserBriefSerializer(read_only=True, allow_null=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def get_rule_label(self, e) -> str:
        from .quality import BY_ID

        rule = BY_ID.get(e.rule)
        return rule.label if rule else e.rule

    def get_active(self, e) -> bool:
        return e.expires_on >= self.context["today"]


class QualityExceptionInputSerializer(serializers.Serializer):
    record = serializers.UUIDField(help_text="An application, component or AI agent. Not changeable.")
    rule = serializers.CharField(help_text="An exceptable rule that is on (GET /data-quality/rules). Not changeable.")
    reason = serializers.CharField(help_text="Why the check doesn't apply; up to 1,000 characters.")
    owner = serializers.UUIDField(help_text="An active Person who answers for the decision.")
    expires_on = serializers.DateField(
        required=False, help_text="The last day it applies: today to a year ahead. Default: a year from today."
    )
