import uuid

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.models import F, Q, Value
from django.db.models.functions import Coalesce, Lower
from django.utils import timezone

from core.models import Organization, OrgScoped
from core.models import default_thresholds as default_thresholds  # referenced by the frozen migration 0015


def _named(class_name, plural=None, indexes=()):
    """Reference data with a case-insensitive unique name per organization. `plural`: a verbose name pair
    (singular, plural) when the class name doesn't read well; `indexes`: the model's own indexes."""

    class Meta(OrgScoped.Meta):
        ordering = ["name"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint("organization", Lower("name"), name=f"catalog_{class_name.lower()}_unique_name"),
        ]
        if plural is not None:
            verbose_name, verbose_name_plural = plural

    if indexes:
        Meta.indexes = list(indexes)
    return Meta


class Department(OrgScoped):
    name = models.CharField(max_length=200)

    Meta = _named("Department")


class Vendor(OrgScoped):
    """A software supplier. Migration 0025 adds the support contacts, portal, notes and the account manager, a Person
    in the same organization (composite foreign key)."""

    name = models.CharField(max_length=200)
    website = models.URLField(blank=True)
    support_url = models.URLField(max_length=2000, blank=True)
    support_email = models.EmailField(blank=True)
    support_phone = models.CharField(max_length=50, blank=True)
    portal_url = models.URLField(max_length=2000, blank=True)
    account_manager = models.ForeignKey("Person", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    notes = models.TextField(blank=True)
    custom_field_data = models.JSONField(default=dict, blank=True)  # migration 0028 (see Resource)

    Meta = _named(
        "Vendor",
        indexes=[GinIndex(fields=["custom_field_data"], name="catalog_vendor_cf_gin", opclasses=["jsonb_path_ops"])],
    )


class Person(OrgScoped):
    """Someone who can own or confirm a record; not necessarily a User. Names never merge or auto-match;
    email and the linked User are unique per organization when set. Migration 0021 adds a composite
    foreign key so the department is in the Person's organization."""

    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    title = models.CharField(max_length=200, blank=True)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    active = models.BooleanField(default=True)
    anonymized_at = models.DateTimeField(null=True, blank=True)  # never cleared

    class Meta(OrgScoped.Meta):
        ordering = ["name"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(
                "organization", Lower("email"), condition=~Q(email=""), name="catalog_person_unique_email"
            ),
            models.UniqueConstraint(
                fields=["organization", "user"], condition=Q(user__isnull=False), name="catalog_person_unique_user"
            ),
        ]


class Team(OrgScoped):
    """A support team; never an owner. Members are optional People, through TeamMember."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    members = models.ManyToManyField(Person, through="TeamMember", related_name="teams", blank=True)

    Meta = _named("Team")


class TeamMember(models.Model):
    """A Person on a Team. Migration 0025 adds composite keys so the team and the person share the row's
    organization."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="member_links")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="team_links")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="catalog_teammember_org_id"),
            models.UniqueConstraint(fields=["team", "person"], name="catalog_teammember_unique"),
        ]
        indexes = [models.Index(fields=["organization", "person"], name="catalog_teammember_person")]


class SoftwareCategory(OrgScoped):
    """A category for applications: per organization, seeded with the default list
    when the organization is created (catalog/categories.py) and by migration 0023 for existing ones."""

    name = models.CharField(max_length=100)
    description = models.CharField(max_length=200, blank=True)

    Meta = _named("SoftwareCategory", ("software category", "software categories"))


class Kind(models.TextChoices):
    APPLICATION = "application", "Application"
    COMPONENT = "component", "Component"
    # Added ahead of the integrations table so kind handling is three-way first. Integrations
    # have no criticality gap and no record review, so the record queues filter on RECORD_KINDS.
    INTEGRATION = "integration", "Integration"
    # An AI agent is a record like an application or a component (owners, criticality, reviews).
    AI_AGENT = "ai_agent", "AI agent"


# The kinds that carry ownership, criticality and record reviews; every record list, count and queue is limited to these.
RECORD_KINDS = [Kind.APPLICATION, Kind.COMPONENT, Kind.AI_AGENT]
# Their values as plain strings, for API choice fields (OpenAPI RecordKindEnum).
RECORD_KIND_CHOICES = ["application", "component", "ai_agent"]
# The kinds CSV import takes (imports/importer.py); AI agents are exported but not imported yet.
IMPORT_KINDS = [Kind.APPLICATION, Kind.COMPONENT]
# The URL segment of each record kind's list (/api/v1/<plural>, and the SPA's pages).
KIND_PATHS: dict[str, str] = {
    Kind.APPLICATION: "applications",
    Kind.COMPONENT: "components",
    Kind.AI_AGENT: "ai-agents",
}


class Lifecycle(models.TextChoices):
    PROPOSED = "proposed", "Proposed"
    EVALUATING = "evaluating", "Evaluating"
    ACTIVE = "active", "Active"
    DEPRECATED = "deprecated", "Deprecated"
    RETIRED = "retired", "Retired"


class Criticality(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    CRITICAL = "critical", "Critical"


class BuiltBy(models.TextChoices):
    VENDOR = "vendor", "Vendor"
    INTERNAL = "internal", "Internal"
    CONTRACTOR = "contractor", "Contractor"
    COMMUNITY = "community", "Community"


class LicenseModel(models.TextChoices):
    COMMERCIAL = "commercial", "Commercial"
    OPEN_SOURCE = "open_source", "Open source"
    FREE = "free", "Free"
    INTERNAL = "internal", "Internal"
    UNKNOWN = "unknown", "Unknown"


class ComponentType(models.TextChoices):
    SCRIPT = "script", "Script"
    SCHEDULED_JOB = "scheduled_job", "Scheduled job"
    RUNBOOK = "runbook", "Runbook"
    FUNCTION = "function", "Function"
    LOGIC_APP = "logic_app", "Logic App"
    AUTOMATION_FLOW = "automation_flow", "Automation flow"
    REPORT = "report", "Report"
    AI_SERVICE = "ai_service", "AI service"
    MIDDLEWARE = "middleware", "Middleware"
    INTERNAL_API = "internal_api", "Internal API"
    # Values added next to the earlier ones (no remap).
    API = "api", "API"
    SERVICE = "service", "Service"
    WORKFLOW = "workflow", "Workflow"
    INFRA_AUTOMATION = "infra_automation", "Infrastructure automation"
    OTHER = "other", "Other"


class DataClassification(models.TextChoices):
    PUBLIC = "public", "Public"
    INTERNAL = "internal", "Internal"
    CONFIDENTIAL = "confidential", "Confidential"
    RESTRICTED = "restricted", "Restricted"


class ValidationStatus(models.TextChoices):
    """Compliance profile."""

    NOT_ASSESSED = "not_assessed", "Not assessed"
    NOT_REQUIRED = "not_required", "Not required"
    IN_VALIDATION = "in_validation", "In validation"
    VALIDATED = "validated", "Validated"
    RETIRED = "retired", "Retired"


class RiskTier(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"


class DeploymentModel(models.TextChoices):
    SAAS = "saas", "SaaS"
    ON_PREM = "on_prem", "On-premises"
    HYBRID = "hybrid", "Hybrid"
    DESKTOP = "desktop", "Desktop"


class AuthenticationMethod(models.TextChoices):
    SAML = "saml", "SAML"
    OIDC = "oidc", "OpenID Connect"
    OAUTH = "oauth", "OAuth"
    LDAP = "ldap", "LDAP or Kerberos"
    LOCAL = "local", "Local accounts"
    NONE = "none", "No sign-in"
    OTHER = "other", "Other"


class IntegrationDirection(models.TextChoices):
    """Which way an integration's data moves. Source and target name the ends; two-way
    means data also comes back from the target. Blank means not recorded."""

    ONE_WAY = "one_way", "One way (source to target)"
    TWO_WAY = "two_way", "Both ways"


class DataCategory(models.TextChoices):
    """The kinds of data an integration carries (data-flow map, migration 0041). A multi-select alongside the
    data classification, which says how sensitive the data is; these say what it is. Personal, special-category
    personal and health data (GDPR Art. 9 special category) make `contains_personal_data` true (derived, never stored)."""

    PERSONAL = "personal", "Personal data"
    SPECIAL_CATEGORY = "special_category", "Special-category personal data"
    FINANCIAL = "financial", "Financial"
    CREDENTIALS = "credentials", "Credentials or secrets"
    HEALTH = "health", "Health"
    CUSTOMER_CONTENT = "customer_content", "Customer content"
    OTHER = "other", "Other"


# Health data is special-category personal data (GDPR Art. 9), so it counts as personal data too.
PERSONAL_CATEGORIES = (DataCategory.PERSONAL, DataCategory.SPECIAL_CATEGORY, DataCategory.HEALTH)


class IntegrationAuthMethod(models.TextChoices):
    """How an integration authenticates to the other end. oauth, none and other share
    their values with AuthenticationMethod; the rest of that list (SAML, LDAP, local accounts) is about people signing
    in, so the machine-to-machine methods are their own values. Blank means not recorded."""

    OAUTH = "oauth", "OAuth 2.0"
    API_KEY = "api_key", "API key"
    BASIC = "basic", "Username and password"
    CERTIFICATE = "certificate", "Certificate"
    MANAGED_IDENTITY = "managed_identity", "Managed identity"
    SSH_KEY = "ssh_key", "SSH key"
    NONE = "none", "No authentication"
    OTHER = "other", "Other"


class Hosting(models.TextChoices):
    AZURE = "azure", "Azure"
    ON_PREM = "on_prem", "On-premises"
    POWER_PLATFORM = "power_platform", "Power Platform"
    OTHER = "other", "Other"


class Environment(models.TextChoices):
    """A component's environment. Blank means not recorded, so there is no "unknown" value."""

    DEV = "dev", "Development"
    TEST = "test", "Test"
    PROD = "prod", "Production"


class AgentPlatform(models.TextChoices):
    """Where an AI agent is built or hosted."""

    COPILOT_STUDIO = "copilot_studio", "Copilot Studio"
    CLAUDE = "claude", "Claude"
    AZURE_OPENAI = "azure_openai", "Azure OpenAI"
    OPENAI = "openai", "OpenAI"
    M365_COPILOT_EXTENSION = "m365_copilot_extension", "Microsoft 365 Copilot extension"
    CUSTOM = "custom", "Custom"
    OTHER = "other", "Other"


class AgentChannel(models.TextChoices):
    """Where people reach an AI agent."""

    TEAMS = "teams", "Teams"
    M365_COPILOT = "m365_copilot", "Microsoft 365 Copilot"
    WEB = "web", "Web"
    API = "api", "API"
    EMAIL = "email", "Email"
    OTHER = "other", "Other"


class AgentAuthMode(models.TextChoices):
    """Whose identity an AI agent acts with."""

    USER = "user", "Signed-in user"
    SERVICE_PRINCIPAL = "service_principal", "Service principal"
    API_KEY = "api_key", "API key"
    MANAGED_IDENTITY = "managed_identity", "Managed identity"


class AgentRisk(models.TextChoices):
    """The risk class of an AI agent (the EU AI Act's four tiers)."""

    MINIMAL = "minimal", "Minimal"
    LIMITED = "limited", "Limited"
    HIGH = "high", "High"
    UNACCEPTABLE = "unacceptable", "Unacceptable"


class ModelModality(models.TextChoices):
    """What an AI model takes and produces (the values are this project's)."""

    TEXT = "text", "Text"
    MULTIMODAL = "multimodal", "Multimodal"
    IMAGE = "image", "Image"
    AUDIO = "audio", "Audio"
    EMBEDDING = "embedding", "Embedding"
    OTHER = "other", "Other"


class Resource(OrgScoped):
    """The shared row for an application or component. Kind-specific fields live in the
    one-to-one Application or Component row; the service layer keeps exactly one, matching kind. Migration 0021 adds
    composite foreign keys so the owners, the support team and the creating import run are in its organization."""

    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.APPLICATION)
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220)
    description = models.TextField(blank=True)
    lifecycle = models.CharField(max_length=20, choices=Lifecycle.choices)
    criticality = models.CharField(max_length=20, choices=Criticality.choices, blank=True)
    business_owner = models.ForeignKey(Person, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    technical_owner = models.ForeignKey(Person, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    support_team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    it_steward = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    archived_at = models.DateTimeField(null=True, blank=True)
    # The import run that created this record, for "Unreviewed imports" and the record's source.
    created_by_import = models.ForeignKey(
        "imports.ImportRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="created_resources"
    )
    # Who pays and how sensitive the data is. Migration 0024 adds the department's composite key.
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    cost_center = models.CharField(max_length=50, blank=True)
    data_classification = models.CharField(max_length=20, choices=DataClassification.choices, blank=True)
    # The compliance profile (migration 0026): read and written only while the organization has it on
    # (catalog/compliance.py). Null means not recorded; the plan makes all six nullable columns, text included,
    # so one rule covers them (hence the DJ001 exceptions).
    is_gxp = models.BooleanField(null=True, blank=True)
    validation_status = models.CharField(  # noqa: DJ001
        max_length=20, choices=ValidationStatus.choices, null=True, blank=True
    )
    validation_doc_url = models.URLField(max_length=2000, null=True, blank=True)  # noqa: DJ001
    last_periodic_review = models.DateField(null=True, blank=True)
    next_periodic_review = models.DateField(null=True, blank=True)
    risk_tier = models.CharField(max_length=10, choices=RiskTier.choices, null=True, blank=True)  # noqa: DJ001
    # Custom field values (migration 0028): {field name: value}, only for the organization's CustomFields that
    # apply to this kind, validated in catalog/customfields.py. A value that isn't set has no key.
    custom_field_data = models.JSONField(default=dict, blank=True)

    class Meta(OrgScoped.Meta):
        ordering = ["name"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["organization", "kind", "slug"], name="catalog_resource_unique_slug"),
        ]
        indexes = [
            GinIndex(fields=["name"], name="catalog_resource_name_trgm", opclasses=["gin_trgm_ops"]),
            GinIndex(fields=["custom_field_data"], name="catalog_resource_cf_gin", opclasses=["jsonb_path_ops"]),
        ]

    @property
    def archived(self):
        return self.archived_at is not None

    @property
    def extension(self):
        """The Application, Component, Integration or AIAgent row for this resource's kind, or None if it is
        missing."""
        return getattr(self, self.kind, None) if self.kind in Kind.values else None


class Application(models.Model):
    """Application fields of a Resource. It carries its own organization so migrations 0020 and 0021
    can add composite foreign keys: the resource and the vendor belong to that organization."""

    resource = models.OneToOneField(Resource, primary_key=True, on_delete=models.CASCADE, related_name="application")
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    purpose = models.TextField(blank=True)
    vendor = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    built_by = models.CharField(max_length=20, choices=BuiltBy.choices, blank=True)
    license_model = models.CharField(max_length=20, choices=LicenseModel.choices, blank=True)
    documentation_url = models.URLField(blank=True)
    support_url = models.URLField(blank=True)
    # Migration 0024 adds the category's composite key.
    category = models.ForeignKey(SoftwareCategory, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    deployment_model = models.CharField(max_length=20, choices=DeploymentModel.choices, blank=True)
    version = models.CharField(max_length=50, blank=True)
    primary_url = models.URLField(max_length=2000, blank=True)
    admin_url = models.URLField(max_length=2000, blank=True)
    authentication_method = models.CharField(max_length=20, choices=AuthenticationMethod.choices, blank=True)
    sso_enabled = models.BooleanField(null=True, blank=True)  # null: not recorded
    scim_provisioning = models.BooleanField(null=True, blank=True)
    entra_app_id = models.CharField(max_length=36, blank=True)  # the app registration's client ID (a GUID)
    service_principal_id = models.CharField(max_length=36, blank=True)  # the enterprise application's object ID

    class Meta:
        constraints = [
            # The (organization_id, id) key of an extension row, whose id is its resource's.
            models.UniqueConstraint(fields=["organization", "resource"], name="catalog_application_org_resource_uq"),
        ]


class InstanceStatus(models.TextChoices):
    OPERATIONAL = "operational", "Operational"
    DEGRADED = "degraded", "Degraded"
    OFFLINE = "offline", "Offline"
    UNKNOWN = "unknown", "Unknown"


class ApplicationInstance(OrgScoped):
    """One deployment of an application, such as Production or Sandbox. `application` points at
    the shared Resource row; migration 0007 adds the database composite FK (organization_id, application_id) so
    it cannot reference another organization's row, and the service layer keeps it to kind=application."""

    application = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="instances")
    name = models.CharField(max_length=200)
    environment = models.CharField(max_length=100, blank=True)
    url = models.URLField(blank=True)
    status = models.CharField(max_length=20, choices=InstanceStatus.choices, default=InstanceStatus.UNKNOWN)

    class Meta(OrgScoped.Meta):
        ordering = ["name"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(
                "organization", "application", Lower("name"), name="catalog_applicationinstance_unique_name"
            ),
            # The target of the relationship composite FKs (migration 0010): an instance can only be named
            # together with the application it belongs to, in the same organization.
            models.UniqueConstraint(
                fields=["organization", "application", "id"], name="catalog_applicationinstance_org_app_id"
            ),
        ]


class RelationshipKind(models.TextChoices):
    """The slugs of the three core relationship types every organization has (it was the TextChoices called
    RelationshipType before the model of that name). Code that gives a core type its own rules (the endpoint matrix,
    `implements` for data flows, the `replaces` cycle rule) compares a type's slug with these."""

    DEPENDS_ON = "depends_on", "Depends on"
    IMPLEMENTS = "implements", "Implements"
    REPLACES = "replaces", "Replaces"


class RelationshipType(OrgScoped):
    """A kind of relationship, read as "<source> <name> <target>" and from the target as "<target> <inverse_name>
    <source>". Every organization has the three core types (`is_core`), seeded
    when it is created (catalog/relationship_types.py) and by migration 0033 for existing ones; Admins add more.

    `slug` is the wire value of a relationship's `type` and never changes once created (link confirmations, filters
    and imports key on it). A core type's slug and `is_dependency` are locked and it can't be deleted; its labels and
    description can be edited. `is_dependency`: dependency traversal (the explorer, affected sets, retirement checks
    and staleness) follows links of this type. A type in use can't be deleted (catalog/relationship_types.py)."""

    slug = models.SlugField(max_length=50)
    name = models.CharField(max_length=100)
    inverse_name = models.CharField(max_length=100, blank=True)
    description = models.CharField(max_length=200, blank=True)
    is_dependency = models.BooleanField(default=False)
    is_core = models.BooleanField(default=False)

    class Meta(OrgScoped.Meta):
        ordering = ["name"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["organization", "slug"], name="catalog_relationshiptype_unique_slug"),
            models.UniqueConstraint("organization", Lower("name"), name="catalog_relationshiptype_unique_name"),
        ]

    def __str__(self):
        return self.name


NO_INSTANCE = uuid.UUID(int=0)  # stands in for "no instance" inside the relationship unique index


class Relationship(OrgScoped):
    """A typed, directed link: source depends on / implements / replaces target. Migration
    0010 adds composite foreign keys so every endpoint and instance belongs to the same organization, and an
    instance to the application named beside it. The endpoint matrix and the `replaces` cycle rule are in
    services.py. Archiving an endpoint keeps the link; a link is removed only on purpose."""

    # Migration 0034 made this a foreign key (it was a choice column) with a composite key to the organization's type.
    type = models.ForeignKey(RelationshipType, on_delete=models.PROTECT, related_name="relationships")
    source = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="relationships_from")
    source_instance = models.ForeignKey(
        ApplicationInstance, null=True, blank=True, on_delete=models.PROTECT, related_name="relationships_from"
    )
    target = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="relationships_to")
    target_instance = models.ForeignKey(
        ApplicationInstance, null=True, blank=True, on_delete=models.PROTECT, related_name="relationships_to"
    )
    purpose = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(OrgScoped.Meta):
        ordering = ["created_at"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(
                "organization",
                "type",
                "source",
                Coalesce("source_instance", Value(NO_INSTANCE, output_field=models.UUIDField())),
                "target",
                Coalesce("target_instance", Value(NO_INSTANCE, output_field=models.UUIDField())),
                name="catalog_relationship_unique_link",
            ),
            # Not the same record on both ends, unless the ends are different instances.
            models.CheckConstraint(
                condition=~Q(source=F("target"))
                | (Q(source_instance__isnull=True) & Q(target_instance__isnull=False))
                | (Q(source_instance__isnull=False) & Q(target_instance__isnull=True))
                | (
                    Q(source_instance__isnull=False)
                    & Q(target_instance__isnull=False)
                    & ~Q(source_instance=F("target_instance"))
                ),
                name="catalog_relationship_not_self",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "source"], name="catalog_relationship_source"),
            models.Index(fields=["organization", "target"], name="catalog_relationship_target"),
        ]


class Component(models.Model):
    """Component fields of a Resource; carries its own organization like Application (migration 0020)."""

    resource = models.OneToOneField(Resource, primary_key=True, on_delete=models.CASCADE, related_name="component")
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    component_type = models.CharField(max_length=30, choices=ComponentType.choices, default=ComponentType.OTHER)
    purpose = models.TextField(blank=True)
    host_runtime = models.CharField(max_length=200, blank=True)
    repository_url = models.URLField(blank=True)
    documentation_url = models.URLField(blank=True)
    built_by = models.CharField(max_length=20, choices=BuiltBy.choices, blank=True)
    license_model = models.CharField(max_length=20, choices=LicenseModel.choices, blank=True)
    hosting = models.CharField(max_length=20, choices=Hosting.choices, blank=True)
    runbook_url = models.URLField(max_length=2000, blank=True)
    schedule_or_trigger = models.CharField(max_length=200, blank=True)
    environment = models.CharField(max_length=10, choices=Environment.choices, blank=True)
    azure_resource_id = models.CharField(max_length=500, blank=True)
    pp_environment_id = models.CharField(max_length=64, blank=True)  # Power Platform environment ID
    external_id = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "resource"], name="catalog_component_org_resource_uq"),
        ]


class Integration(models.Model):
    """A data flow between two records. Source and target are typed columns, not relationship
    rows; only `implements` links touch an integration. It carries its own organization so migration 0011 can add
    composite foreign keys: both ends belong to that organization, and an instance to the application named beside
    it. That an end is an application or a component, never an integration, is checked in services.py."""

    resource = models.OneToOneField(Resource, primary_key=True, on_delete=models.CASCADE, related_name="integration")
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    source_resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="integrations_from")
    source_instance = models.ForeignKey(
        ApplicationInstance, null=True, blank=True, on_delete=models.PROTECT, related_name="integrations_from"
    )
    target_resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="integrations_to")
    target_instance = models.ForeignKey(
        ApplicationInstance, null=True, blank=True, on_delete=models.PROTECT, related_name="integrations_to"
    )
    data_purpose = models.TextField(blank=True)
    protocol = models.CharField(max_length=100, blank=True)
    schedule = models.CharField(max_length=200, blank=True)
    documentation_url = models.URLField(blank=True)
    # Integration fields (migration 0035). Blank: not recorded. The data classification is the Resource's.
    direction = models.CharField(max_length=20, choices=IntegrationDirection.choices, blank=True)
    auth_method = models.CharField(max_length=20, choices=IntegrationAuthMethod.choices, blank=True)
    monitoring_url = models.URLField(max_length=2000, blank=True)
    # Data-flow map (migration 0041): what kinds of data it carries, in DataCategory order, once each.
    data_categories = ArrayField(
        models.CharField(max_length=20, choices=DataCategory.choices), blank=True, default=list
    )

    class Meta:
        constraints = [
            # The same record at both ends only through two different instances (a prod to sandbox refresh).
            models.CheckConstraint(
                condition=~Q(source_resource=F("target_resource"))
                | (
                    Q(source_instance__isnull=False)
                    & Q(target_instance__isnull=False)
                    & ~Q(source_instance=F("target_instance"))
                ),
                name="catalog_integration_not_self",
            ),
            # The target of link_confirmations' composite foreign key (migration 0012).
            models.UniqueConstraint(fields=["organization", "resource"], name="catalog_integration_org_resource_uq"),
        ]
        indexes = [
            models.Index(fields=["organization", "source_resource"], name="catalog_integration_source"),
            models.Index(fields=["organization", "target_resource"], name="catalog_integration_target"),
        ]

    @property
    def contains_personal_data(self) -> bool:
        return any(c in PERSONAL_CATEGORIES for c in self.data_categories)


class AIProvider(OrgScoped):
    """Who makes or hosts AI models (a reference entity): per organization, a unique name."""

    name = models.CharField(max_length=200)
    website = models.URLField(max_length=2000, blank=True)

    Meta = _named("AIProvider", ("AI provider", "AI providers"))


class AIModel(OrgScoped):
    """One model of a provider: name and version unique per provider, ignoring case. Migration 0027
    adds the composite foreign key so the provider is in the model's organization; a provider with models can't be
    deleted (PROTECT, and a 409 from the service)."""

    provider = models.ForeignKey(AIProvider, on_delete=models.PROTECT, related_name="models")
    name = models.CharField(max_length=200)
    version = models.CharField(max_length=100, blank=True)
    modality = models.CharField(max_length=20, choices=ModelModality.choices, blank=True)
    hosting_region = models.CharField(max_length=100, blank=True)

    class Meta(OrgScoped.Meta):
        ordering = ["name", "version"]
        verbose_name = "AI model"
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(
                "organization", "provider", Lower("name"), Lower("version"), name="catalog_aimodel_unique_name"
            ),
        ]
        indexes = [models.Index(fields=["organization", "provider"], name="catalog_aimodel_provider")]


class AIAgent(models.Model):
    """AI agent fields of a Resource: one-to-one like Application and Component, carrying
    its own organization so migration 0027's composite foreign key keeps the resource in it. Channels are a list of
    AgentChannel values; knowledge sources and tools are free text. `human_in_loop` and `actions_can_write` are
    null until someone records the decision; a missing human-in-loop decision is a Needs attention row."""

    resource = models.OneToOneField(Resource, primary_key=True, on_delete=models.CASCADE, related_name="ai_agent")
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    purpose = models.TextField(blank=True)
    platform = models.CharField(max_length=30, choices=AgentPlatform.choices, blank=True)
    models_used = models.ManyToManyField(AIModel, through="AgentModel", related_name="agents", blank=True)
    environment = models.CharField(max_length=10, choices=Environment.choices, blank=True)
    pp_environment_id = models.CharField(max_length=64, blank=True)  # Power Platform environment ID
    channels = ArrayField(models.CharField(max_length=20, choices=AgentChannel.choices), blank=True, default=list)
    knowledge_sources = models.TextField(blank=True)
    tools_connectors = models.TextField(blank=True)
    auth_mode = models.CharField(max_length=20, choices=AgentAuthMode.choices, blank=True)
    run_as_identity = models.CharField(max_length=200, blank=True)  # the account or app the agent acts as
    data_access_scope = models.TextField(blank=True)
    human_in_loop = models.BooleanField(null=True, blank=True)  # null: no decision recorded
    actions_can_write = models.BooleanField(null=True, blank=True)  # null: not recorded
    risk_classification = models.CharField(max_length=20, choices=AgentRisk.choices, blank=True)
    repo_url = models.URLField(max_length=2000, blank=True)
    external_id = models.CharField(max_length=200, blank=True)  # bot ID or schema name

    class Meta:
        verbose_name = "AI agent"
        constraints = [
            models.UniqueConstraint(fields=["organization", "resource"], name="catalog_aiagent_org_resource_uq"),
        ]


class AgentModel(models.Model):
    """An AI model an agent uses (the through table of AIAgent.models_used). Migration 0027 adds composite keys so the
    agent and the model share the row's organization."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    agent = models.ForeignKey(AIAgent, on_delete=models.CASCADE, related_name="model_links")
    model = models.ForeignKey(AIModel, on_delete=models.PROTECT, related_name="agent_links")

    class Meta:
        verbose_name = "AI agent model"
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="catalog_agentmodel_org_id"),
            models.UniqueConstraint(fields=["agent", "model"], name="catalog_agentmodel_unique"),
        ]
        indexes = [models.Index(fields=["organization", "model"], name="catalog_agentmodel_model")]


class RecordReview(OrgScoped):
    """A Person confirmed the five confirmed fields; IT records it. Migration 0021 adds composite foreign
    keys so the record and the Person are in the review's organization."""

    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="reviews")
    confirmed_by = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="+")
    inactive_reason = models.CharField(max_length=500, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    confirmation_date = models.DateField()
    next_review_date = models.DateField()
    evidence = models.CharField(max_length=500)
    confirmed_values = models.JSONField()

    class Meta(OrgScoped.Meta):
        ordering = ["-confirmation_date", "-created_at"]


class LinkConfirmation(OrgScoped):
    """A Person confirmed a relationship or an integration is still accurate; IT records it.
    One table with two nullable foreign keys and an exactly-one check. `confirmed_signature` holds what was
    confirmed (endpoints, instances, type, owner), so a later difference means Needs verification. Migration 0012
    adds composite foreign keys so the link and the Person belong to the same organization. Removing a link
    removes its confirmations; the audit log keeps the history."""

    relationship = models.ForeignKey(
        Relationship, null=True, blank=True, on_delete=models.CASCADE, related_name="confirmations"
    )
    integration = models.ForeignKey(
        Integration, null=True, blank=True, on_delete=models.CASCADE, related_name="confirmations"
    )
    confirmed_by = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="+")
    inactive_reason = models.CharField(max_length=500, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    confirmed_on = models.DateField()
    evidence = models.CharField(max_length=500)
    confirmed_signature = models.JSONField()

    class Meta(OrgScoped.Meta):
        ordering = ["-confirmed_on", "-created_at"]
        constraints = OrgScoped.Meta.constraints + [
            models.CheckConstraint(
                condition=(Q(relationship__isnull=False) & Q(integration__isnull=True))
                | (Q(relationship__isnull=True) & Q(integration__isnull=False)),
                name="catalog_linkconfirmation_exactly_one_link",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "relationship"], name="catalog_linkconf_relationship"),
            models.Index(fields=["organization", "integration"], name="catalog_linkconf_integration"),
        ]


class ChangeType(models.TextChoices):
    CHANGE = "change", "Change"
    RETIREMENT = "retirement", "Retirement"


class AssessmentStatus(models.TextChoices):
    """An assessment's status. Staleness is never a status; it is `stale_since` on the current snapshot."""

    DRAFT = "draft", "Draft"
    IN_REVIEW = "in_review", "In review"
    READY = "ready", "Ready for external execution"
    CLOSED = "closed", "Closed"
    CANCELLED = "cancelled", "Canceled"


class ChangeAssessment(OrgScoped):
    """A saved assessment of a planned change or retirement of one application or component. The
    target is chosen when the assessment is made and does not change. Documentation, not approval. Migration 0013
    adds a composite foreign key so the target belongs to the same organization."""

    target = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="assessments")
    change_type = models.CharField(max_length=20, choices=ChangeType.choices)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    target_date = models.DateField(null=True, blank=True)
    ticket_url = models.URLField(blank=True)
    findings = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=AssessmentStatus.choices, default=AssessmentStatus.DRAFT)
    retirement_ack_reason = models.TextField(blank=True)

    class Meta(OrgScoped.Meta):
        ordering = ["-updated_at", "id"]
        verbose_name = "change assessment"
        indexes = [models.Index(fields=["organization", "status"], name="catalog_assessment_status")]


class AssessmentSnapshot(OrgScoped):
    """The affected set as it was when saved. Refreshing adds a snapshot and clears
    `is_current` on the old one; nothing is deleted. `stale_since` and `stale_reasons` are set by catalog/staleness.py
    and cleared only by refreshing."""

    assessment = models.ForeignKey(ChangeAssessment, on_delete=models.CASCADE, related_name="snapshots")
    taken_at = models.DateTimeField(default=timezone.now)
    taken_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    is_current = models.BooleanField(default=True)
    stale_since = models.DateTimeField(null=True, blank=True)
    stale_reasons = models.JSONField(default=list)  # readable sentences, oldest first

    class Meta(OrgScoped.Meta):
        ordering = ["-taken_at", "-id"]
        verbose_name = "assessment snapshot"
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(
                fields=["assessment"], condition=Q(is_current=True), name="catalog_snapshot_one_current"
            ),
        ]
        indexes = [models.Index(fields=["organization", "is_current"], name="catalog_snapshot_current")]


class NodeRole(models.TextChoices):
    DEPENDENT = "dependent", "Dependent"
    DATA_FLOW = "data_flow", "Data flow"
    IMPLEMENTER = "implementer", "Implementer"


class SnapshotNode(OrgScoped):
    """One record in a snapshot, with what it was then. Owners are kept as Person IDs, not names, so anonymizing a
    Person also anonymizes old snapshots. The record is PROTECTed: archived, never deleted. An `in_cycle` row
    marks a path that led back to a record already on it (the target included); it is not a second member.

    A hub's snapshot is over a thousand rows written in one request, so these rows carry no more indexes
    and foreign key checks than they need: the composite keys of migration 0013 enforce the organization, so the
    single-column constraints, the organization index and the (organization, id) key are left off (migration 0014)."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+", db_index=False)
    snapshot = models.ForeignKey(
        AssessmentSnapshot, on_delete=models.CASCADE, related_name="nodes", db_constraint=False
    )
    resource = models.ForeignKey(
        Resource, on_delete=models.PROTECT, related_name="snapshot_nodes", db_constraint=False, db_index=False
    )
    role = models.CharField(max_length=20, choices=NodeRole.choices)
    depth = models.PositiveIntegerField()
    path = models.JSONField()  # resource IDs from the target to this record
    lifecycle_then = models.CharField(max_length=20, choices=Lifecycle.choices)
    owners_then = models.JSONField()  # {"business_owner": id or None, "technical_owner": id or None}
    in_cycle = models.BooleanField(default=False)

    class Meta:
        ordering = ["depth", "id"]
        verbose_name = "snapshot node"
        indexes = [models.Index(fields=["organization", "resource"], name="catalog_snapnode_resource")]


class SnapshotLink(OrgScoped):
    """A relationship or integration on a snapshot's paths, with its signature then. If the link is later removed
    the reference becomes null (`link_kind` and the signature still say what it was). Lean like SnapshotNode."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+", db_index=False)
    snapshot = models.ForeignKey(
        AssessmentSnapshot, on_delete=models.CASCADE, related_name="links", db_constraint=False
    )
    link_kind = models.CharField(max_length=20)  # "relationship" or "integration"
    relationship = models.ForeignKey(
        Relationship,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="snapshot_links",
        db_constraint=False,
        db_index=False,
    )
    integration = models.ForeignKey(
        Integration,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="snapshot_links",
        db_constraint=False,
        db_index=False,
    )
    signature_then = models.JSONField()
    # A relationship's type labels when the snapshot was taken, so a type deleted since still reads in words. Blank
    # for integrations and for older snapshots (migration 0038).
    type_name_then = models.CharField(max_length=100, blank=True, default="")
    inverse_name_then = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        verbose_name = "snapshot link"
        constraints = [
            models.CheckConstraint(
                condition=Q(relationship__isnull=True) | Q(integration__isnull=True),
                name="catalog_snapshotlink_not_both",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "relationship"], name="catalog_snaplink_relationship"),
            models.Index(fields=["organization", "integration"], name="catalog_snaplink_integration"),
        ]


# ---- Tags and saved filters --------------------------------------------------------------------------------


# The colours a tag may take: names of the design system's hue families (apps/web/src/design/tokens.json), which the
# SPA maps to its tokens. Never a hex value; blank is the neutral default.
TAG_COLORS = ("gray", "blue", "teal", "green", "amber", "orange", "red", "violet", "magenta")


class Tag(OrgScoped):
    """A plain label on records: unique per organization ignoring case, created on first use, with a
    colour (one of TAG_COLORS) and a description."""

    name = models.CharField(max_length=60)
    color = models.CharField(max_length=20, blank=True, choices=[(c, c.capitalize()) for c in TAG_COLORS])
    description = models.CharField(max_length=500, blank=True)
    resources = models.ManyToManyField(Resource, through="ResourceTag", related_name="tags")

    Meta = _named("Tag")


class ResourceTag(models.Model):
    """Migration 0018 adds composite keys so the tag and the record share the row's organization; migration 0022
    gives the row a UUID key and its (organization, id) key."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    resource = models.ForeignKey(Resource, on_delete=models.CASCADE, related_name="tag_links")
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="links")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="catalog_resourcetag_org_id"),
            models.UniqueConstraint(fields=["resource", "tag"], name="catalog_resourcetag_unique"),
        ]
        indexes = [models.Index(fields=["organization", "tag"], name="catalog_resourcetag_tag")]


# ---- Custom fields ---------------------------------------------------------------------------------


class CustomFieldType(models.TextChoices):
    TEXT = "text", "Text"
    LONGTEXT = "longtext", "Long text"
    INTEGER = "integer", "Whole number"
    DECIMAL = "decimal", "Decimal number"
    BOOLEAN = "boolean", "Yes or no"
    DATE = "date", "Date"
    URL = "url", "Web address"
    SELECT = "select", "Choice"
    MULTISELECT = "multiselect", "Multiple choices"
    OBJECT = "object", "JSON object"


# What a custom field can apply to: every Resource kind and vendors (catalog/customfields.py; the other primary
# records get theirs with the phases that build them).
CUSTOM_FIELD_KINDS = ("application", "component", "integration", "ai_agent", "vendor", "contract", "license_pool")


class CustomField(OrgScoped):
    """An admin-defined field on records. `name` is the key in each record's `custom_field_data`, the
    `cf_<name>` CSV column and list filter; it never changes after creation (the label does). Validation, the rules for
    changing a field that holds values and the API live in catalog/customfields.py."""

    name = models.CharField(max_length=50)
    label = models.CharField(max_length=100)
    type = models.CharField(max_length=20, choices=CustomFieldType.choices)
    description = models.CharField(max_length=500, blank=True)
    choices = ArrayField(models.CharField(max_length=100), blank=True, default=list)
    required = models.BooleanField(default=False)
    default = models.JSONField(null=True, blank=True)  # a value in storage format, applied when a record is created
    applies_to = ArrayField(models.CharField(max_length=20, choices=[(k, k) for k in CUSTOM_FIELD_KINDS]))
    weight = models.PositiveSmallIntegerField(default=100)  # lower first
    is_reminder_source = models.BooleanField(default=False)  # date fields only; reminders read it

    class Meta(OrgScoped.Meta):
        ordering = ["weight", "name"]
        verbose_name = "custom field"
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["organization", "name"], name="catalog_customfield_unique_name"),
            models.UniqueConstraint("organization", Lower("label"), name="catalog_customfield_unique_label"),
            models.CheckConstraint(
                condition=Q(is_reminder_source=False) | Q(type="date"), name="catalog_customfield_reminder_is_date"
            ),
        ]


class SavedFilter(OrgScoped):
    """A saved view (catalog 0039): a name, the list's query
    parameters (`query`; results are never stored) and the columns to show (`columns`, ordered column ids of the
    list, catalog/saved_views.columns_for; empty means the list's defaults). `user` is the owner; `shared` makes it
    visible to the whole organization (made shared by Admins and Editors), otherwise only the owner sees it. A shared
    view from before 0039 whose creator is unknown has no owner (only Admins manage it). Built-ins live in code."""

    # SET_NULL: a deleted user's shared views stay (ownerless, Admins manage them); their private views are deleted
    # with them (catalog/saved_filters.delete_private_views_of, pre_delete on User), so no private view is ownerless.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    kind = models.CharField(max_length=20, choices=[(k, k.label) for k in RECORD_KINDS])
    name = models.CharField(max_length=100)
    query = models.JSONField(default=dict)
    shared = models.BooleanField(default=False)
    columns = ArrayField(models.CharField(max_length=60), default=list, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(OrgScoped.Meta):
        ordering = ["name"]
        constraints = OrgScoped.Meta.constraints + [
            # A private view always has its owner.
            models.CheckConstraint(
                condition=models.Q(shared=True) | models.Q(user__isnull=False), name="catalog_savedfilter_owner"
            ),
        ]


class AttachmentKind(models.TextChoices):
    FILE = "file", "File"
    LINK = "link", "Link"


class AttachmentCategory(models.TextChoices):
    """What an attachment is. A fixed list, like the record
    choices; a per-organization table can replace it if organizations need their own."""

    CONTRACT = "contract", "Contract"
    INVOICE = "invoice", "Invoice"
    SOP_RUNBOOK = "sop_runbook", "SOP or runbook"
    ARCHITECTURE = "architecture", "Architecture"
    VALIDATION = "validation", "Validation"
    VENDOR_DOCS = "vendor_docs", "Vendor documentation"
    SECURITY_REVIEW = "security_review", "Security review"
    OTHER = "other", "Other"


class Attachment(OrgScoped):
    """A file or a link on a record (application, component, integration, AI agent), a vendor
    or a contract. The parent is one of the explicit nullable foreign keys, exactly
    one set (check constraint), each with a composite key to its organization; a later parent adds its own
    column. Writes, limits, content checks and the sweep are in catalog/attachments.py.

    A file is stored under `storage_name`, `attachments/<organization id>/<random hex>`: no part of the original name,
    never under a static or media URL, and read back only by the role-checked download route. `content_type` is what
    the content check found, not what the browser claimed; `size` and `sha256` are measured while the upload is read
    and checked again by `manage.py verify_attachments`. A link has only `url` (http or https)."""

    resource = models.ForeignKey(Resource, null=True, blank=True, on_delete=models.PROTECT, related_name="attachments")
    vendor = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.PROTECT, related_name="attachments")
    # A contract's documents. A string reference: money depends on catalog, not the reverse.
    contract = models.ForeignKey(
        "money.Contract", null=True, blank=True, on_delete=models.PROTECT, related_name="attachments"
    )
    kind = models.CharField(max_length=10, choices=AttachmentKind.choices)
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=30, choices=AttachmentCategory.choices, default=AttachmentCategory.OTHER)
    url = models.URLField(max_length=2000, blank=True)
    storage_name = models.CharField(max_length=200, blank=True)
    filename = models.CharField(max_length=200, blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    size = models.PositiveBigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(OrgScoped.Meta):
        ordering = ["category", "title"]
        constraints = OrgScoped.Meta.constraints + [
            models.CheckConstraint(
                condition=(Q(resource__isnull=False) & Q(vendor__isnull=True) & Q(contract__isnull=True))
                | (Q(resource__isnull=True) & Q(vendor__isnull=False) & Q(contract__isnull=True))
                | (Q(resource__isnull=True) & Q(vendor__isnull=True) & Q(contract__isnull=False)),
                name="catalog_attachment_one_parent",
            ),
            models.CheckConstraint(
                condition=(
                    Q(kind="file")
                    & ~Q(storage_name="")
                    & Q(size__isnull=False)
                    & Q(sha256__regex=r"^[0-9a-f]{64}$")
                    & Q(url="")
                )
                | (Q(kind="link") & ~Q(url="") & Q(storage_name="") & Q(size__isnull=True) & Q(sha256="")),
                name="catalog_attachment_kind_fields",
            ),
            models.UniqueConstraint(
                fields=["storage_name"], condition=~Q(storage_name=""), name="catalog_attachment_unique_storage"
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "resource"], name="catalog_attachment_resource"),
            models.Index(fields=["organization", "vendor"], name="catalog_attachment_vendor"),
            models.Index(fields=["organization", "contract"], name="catalog_attachment_contract"),
        ]

    @property
    def parent(self):
        if self.resource_id is not None:
            return self.resource
        return self.vendor if self.vendor_id is not None else self.contract


class RetiredFile(OrgScoped):
    """A stored attachment file nothing references any more: its attachment was deleted (or its parent was). Kept for
    the retention window (SOFTWARDEN_ATTACHMENT_RETENTION_DAYS), so a backup taken meanwhile still finds every file its
    database dump names; then the nightly `attachments.sweep` deletes the file and this row.
    Operational state: the attachment's own audit events keep its name, size and SHA-256."""

    name = models.CharField(max_length=200, unique=True)
    sha256 = models.CharField(max_length=64, blank=True)
    size = models.PositiveBigIntegerField(null=True, blank=True)
    retired_at = models.DateTimeField(default=timezone.now)

    class Meta(OrgScoped.Meta):
        ordering = ["retired_at"]


class QualityException(OrgScoped):
    """A reasoned "not applicable" for one data-quality rule on one record, such
    as free software that will never be on a contract. It names a rule of catalog/quality.py by id, an accountable
    Person and the last day it applies (`expires_on`, at most a year ahead): from the next day the rule counts again.
    Only rules marked exceptable take one. Migration 0032 adds composite foreign keys so the record and the owner are
    in its organization. Writes and their audit events are in catalog/quality.py; deleting the record removes its
    exceptions, each audited."""

    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="quality_exceptions")
    rule = models.CharField(max_length=60)
    reason = models.TextField()
    owner = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="+")
    expires_on = models.DateField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(OrgScoped.Meta):
        ordering = ["expires_on", "id"]
        verbose_name = "data quality exception"
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["resource", "rule"], name="catalog_qualityexception_unique"),
        ]
        indexes = [models.Index(fields=["organization", "rule"], name="catalog_qualityexc_rule")]


# ---- vendor risk file (catalog 0040; catalog/vendor_risk.py) ------------------------------------------------


class VendorRiskTier(models.TextChoices):
    """How much risk a vendor carries for the organization: entered by a person, never derived."""

    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    CRITICAL = "critical", "Critical"


class QuestionnaireStatus(models.TextChoices):
    NOT_REQUESTED = "not_requested", "Not requested"
    REQUESTED = "requested", "Requested"
    RECEIVED = "received", "Received"
    REVIEWED = "reviewed", "Reviewed"
    NOT_REQUIRED = "not_required", "Not required"


class AssuranceType(models.TextChoices):
    """The vendor's assurance report: a SOC 2 report or an equivalent."""

    SOC2_TYPE1 = "soc2_type1", "SOC 2 Type I"
    SOC2_TYPE2 = "soc2_type2", "SOC 2 Type II"
    ISO27001 = "iso27001", "ISO 27001 certificate"
    OTHER = "other", "Other assurance report"


class DpaStatus(models.TextChoices):
    """Whether a data processing agreement is signed. Blank: not recorded."""

    YES = "yes", "Signed"
    NO = "no", "Not signed"
    NOT_APPLICABLE = "not_applicable", "Not applicable"


class RiskDocumentKind(models.TextChoices):
    SOC2 = "soc2", "SOC 2 report"
    ISO27001 = "iso27001", "ISO 27001 certificate"
    PEN_TEST = "pen_test", "Penetration test report"
    QUESTIONNAIRE = "questionnaire", "Security questionnaire"
    DPA = "dpa", "Data processing agreement"
    INSURANCE = "insurance", "Insurance certificate"
    OTHER = "other", "Other"


class VendorRiskProfile(OrgScoped):
    """A vendor's risk file: the security questionnaire, the assurance report (SOC 2
    or equivalent), the DPA, the subprocessor list, the overall risk tier and notes. One per vendor, created by the
    first write (catalog/vendor_risk.update_profile); a vendor without one reads as the defaults. Documents link to the
    vendor's own attachments by id (SET_NULL: removing the attachment clears the link). `assurance_expires_on` is
    derived, never sent: the issue date plus the validity in months. Composite keys to the vendor and every attachment
    (catalog 0040)."""

    vendor = models.OneToOneField(Vendor, on_delete=models.CASCADE, related_name="risk_profile")
    risk_tier = models.CharField(max_length=10, choices=VendorRiskTier.choices, blank=True)  # blank: not assessed
    # The DPA gap's switch: whether the vendor processes personal data for the organization.
    processes_personal_data = models.BooleanField(default=False)
    questionnaire_status = models.CharField(
        max_length=20, choices=QuestionnaireStatus.choices, default=QuestionnaireStatus.NOT_REQUESTED
    )
    questionnaire_requested_on = models.DateField(null=True, blank=True)
    questionnaire_reviewed_on = models.DateField(null=True, blank=True)
    assurance_type = models.CharField(max_length=20, choices=AssuranceType.choices, blank=True)
    assurance_issued_on = models.DateField(null=True, blank=True)  # the report period's end or the issue date
    assurance_validity_months = models.PositiveSmallIntegerField(default=12)
    assurance_expires_on = models.DateField(null=True, blank=True)  # derived: issued on + validity
    assurance_attachment = models.ForeignKey(
        Attachment, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    dpa_status = models.CharField(max_length=20, choices=DpaStatus.choices, blank=True)
    dpa_signed_on = models.DateField(null=True, blank=True)
    dpa_attachment = models.ForeignKey(Attachment, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    subprocessor_url = models.URLField(max_length=2000, blank=True)
    subprocessor_attachment = models.ForeignKey(
        Attachment, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    subprocessors_reviewed_on = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta(OrgScoped.Meta):
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["organization", "risk_tier"], name="catalog_vendorrisk_tier")]


class VendorRiskDocument(OrgScoped):
    """A dated document in a vendor's risk file: a SOC 2 report, an ISO 27001 certificate, a penetration
    test, a questionnaire, a DPA, an insurance certificate. `expires_on` is entered, or derived from `issued_on` plus
    `validity_months` when those are sent (then it is never sent). Its status is derived when read, never stored:
    current, expiring (within the organization's largest reminder threshold) or expired (catalog/vendor_risk.py).
    An expiry date is a reminder source (`vendor_risk`, notify/reminders.py). Composite keys to the vendor and the
    attachment (catalog 0040)."""

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="risk_documents")
    kind = models.CharField(max_length=20, choices=RiskDocumentKind.choices)
    title = models.CharField(max_length=200, blank=True)
    issued_on = models.DateField(null=True, blank=True)
    validity_months = models.PositiveSmallIntegerField(null=True, blank=True)  # set: expires_on is derived
    expires_on = models.DateField(null=True, blank=True)
    attachment = models.ForeignKey(Attachment, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    notes = models.TextField(blank=True)

    class Meta(OrgScoped.Meta):
        ordering = ["expires_on", "kind", "id"]
        indexes = [
            models.Index(fields=["organization", "vendor"], name="catalog_vendorriskdoc_vendor"),
            models.Index(fields=["organization", "expires_on"], name="catalog_vendorriskdoc_expiry"),
        ]
