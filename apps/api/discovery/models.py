"""Discovery (ADR-007). Moved from the catalog app in the split; tables and constraint
names keep their `catalog_*` names (discovery/migrations/0001_initial.py)."""

from decimal import Decimal

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from catalog.models import ComponentType, Resource
from core.models import (
    CATALOG_ORG_KEY,
    GoogleWorkspaceConnection,
    HaloConnection,
    MicrosoftConnection,
    OktaConnection,
    OrgScoped,
)


class Source(models.TextChoices):
    ENTRA = "entra", "Microsoft Entra ID"
    ENTRA_LICENSES = "entra_licenses", "Microsoft Entra licenses"
    # The Azure, Power Platform and Halo sources: capabilities discovery.azure, discovery.powerplatform and discovery.halo.
    AZURE = "azure", "Azure"
    POWERPLATFORM = "powerplatform", "Power Platform"
    HALO = "halo", "HaloITSM"
    # Third-party apps people sign in to or consented to (sign-in logs, OAuth consent grants); capability
    # discovery.entra. Its own source, so an organization without Entra ID P1 or the consents can leave it off.
    ENTRA_SHADOW_IT = "entra_shadow_it", "Microsoft Entra shadow IT"
    # Single sign-on, provisioning (SCIM) and conditional access of the catalog's Entra apps; capability
    # discovery.entra. Its own source, so an organization without Entra ID P1 or these consents can leave it off.
    ENTRA_COVERAGE = "entra_coverage", "Microsoft Entra SSO and MFA coverage"
    # The applications an organization runs through Okta (capability discovery.okta), and the third-party
    # apps people granted access to their Google Workspace data (OAuth token audit log; capability discovery.google).
    OKTA = "okta", "Okta"
    GOOGLE_WORKSPACE = "google_workspace", "Google Workspace"
    # Each licensed user's licences and last successful sign-in, for licence waste; capability
    # discovery.entra. Its own source: it is the first to read per-user data and needs User.Read.All (and, for the
    # last sign-in, AuditLog.Read.All and Entra ID P1), which an organization that only wants seat counts never grants.
    ENTRA_LICENCE_USAGE = "entra_licence_usage", "Microsoft Entra license usage"


# The connection columns of a connector; at most one is set (discovery_connector_one_connection).
CONNECTION_COLUMNS = ("microsoft_connection", "halo_connection", "okta_connection", "google_workspace_connection")


def _one_connection() -> Q:
    """No two connection columns are set: every pair has a null."""
    pairs = [(a, b) for i, a in enumerate(CONNECTION_COLUMNS) for b in CONNECTION_COLUMNS[i + 1 :]]
    condition = Q()
    for a, b in pairs:
        condition &= Q(**{f"{a}__isnull": True}) | Q(**{f"{b}__isnull": True})
    return condition


class ConnectorSchedule(models.TextChoices):
    MANUAL = "manual", "Only when run"
    DAILY = "daily", "Daily"


class Connector(OrgScoped):
    """A read-only source of observations. It signs in through a connection (core.models): a
    Microsoft source through `microsoft_connection`, Halo through `halo_connection`, never both (the credentials and
    their secret live on the connection, which must be tested at its current revision before a run uses it).
    `config` holds the source's own non-secret settings (the synthetic tenant's `simulated_size` and
    `simulator_changes`). `scope` is the admin-defined filter; each source reads the keys it understands, so M5
    sources need no schema change. A run is requested by setting `run_requested_at`; the worker picks it up."""

    source = models.CharField(max_length=20, choices=Source.choices)
    name = models.CharField(max_length=200)
    enabled = models.BooleanField(default=True)
    config = models.JSONField(default=dict)
    scope = models.JSONField(default=dict)
    # Composite keys to the connection's organization (discovery 0005); PROTECT, so a connection in use can't go.
    microsoft_connection = models.ForeignKey(
        MicrosoftConnection, null=True, blank=True, on_delete=models.PROTECT, related_name="connectors"
    )
    halo_connection = models.ForeignKey(
        HaloConnection, null=True, blank=True, on_delete=models.PROTECT, related_name="connectors"
    )
    # (discovery 0017), composite keys as above.
    okta_connection = models.ForeignKey(
        OktaConnection, null=True, blank=True, on_delete=models.PROTECT, related_name="connectors"
    )
    google_workspace_connection = models.ForeignKey(
        GoogleWorkspaceConnection, null=True, blank=True, on_delete=models.PROTECT, related_name="connectors"
    )
    schedule = models.CharField(max_length=10, choices=ConnectorSchedule.choices, default=ConnectorSchedule.MANUAL)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    run_requested_at = models.DateTimeField(null=True, blank=True)
    run_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # The last access check (discovery/access.py): {ok, steps: [...]}, Softwarden's words and Graph's error
    # codes only, never a token, a secret or Microsoft's message text.
    access_check = models.JSONField(default=dict, blank=True)
    access_checked_at = models.DateTimeField(null=True, blank=True)
    access_checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(OrgScoped.Meta):
        db_table = "catalog_connector"
        ordering = ["name"]
        constraints = [
            CATALOG_ORG_KEY,
            models.UniqueConstraint("organization", Lower("name"), name="catalog_connector_unique_name"),
            models.CheckConstraint(condition=_one_connection(), name="discovery_connector_one_connection"),
        ]


class RunStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


ACTIVE_RUN_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)


class RunTrigger(models.TextChoices):
    MANUAL = "manual", "Run now"
    SCHEDULED = "scheduled", "Daily schedule"


class ConnectorRun(OrgScoped):
    """One pass over a source (each a `discovery.run` job, discovery/engine.py). `counts` is {seen, kept,
    skipped: {filter: n}, new, changed, unchanged, gone, ...}. A failed run changes no observation (the engine writes in
    one transaction); `error` is a redacted, readable message in Softwarden's words.

    Run now and the daily schedule create the run `queued` (`queued_at`, `job_id` of the job that will start it; no
    foreign key, since finished jobs are pruned after 30 days and the history stays); the job moves it to `running`
    (`started_at`) under a lease: `lease_token` until `lease_expires`, renewed as the run makes progress. Only the
    holder of the live token writes the outcome or applies observations, so a run whose lease expired (its worker
    stopped or stalled), marked failed by the stuck sweep, can never overwrite a newer run when it finishes late. At
    most one queued or running run per connector (database constraint). Rows from before runs had leases have no lease
    and count as stuck an hour after they started (engine.STUCK_AFTER)."""

    connector = models.ForeignKey(Connector, on_delete=models.CASCADE, related_name="runs")
    trigger = models.CharField(max_length=10, choices=RunTrigger.choices)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(max_length=10, choices=RunStatus.choices, default=RunStatus.RUNNING)
    queued_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True, default=timezone.now)  # null while queued
    finished_at = models.DateTimeField(null=True, blank=True)
    counts = models.JSONField(default=dict)
    error = models.CharField(max_length=500, blank=True)
    # Why a failed run failed, for clients, when it has a code: `empty_listing` (the
    # empty-listing guard, rest_source.EmptyListing; a run with accept_empty_listing gets through). Empty otherwise.
    failure_code = models.CharField(max_length=40, blank=True, default="")
    job_id = models.UUIDField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires = models.DateTimeField(null=True, blank=True)

    class Meta(OrgScoped.Meta):
        db_table = "catalog_connectorrun"
        ordering = ["-created_at", "-id"]
        constraints = [
            CATALOG_ORG_KEY,
            models.UniqueConstraint(
                fields=["connector"],
                condition=Q(status__in=["queued", "running"]),
                name="discovery_connectorrun_one_active",
            ),
        ]
        indexes = [models.Index(fields=["organization", "-created_at"], name="discovery_run_history")]


class ObservationKind(models.TextChoices):
    """What an observation can become. Entra produces applications and `ai_agent` (a Copilot
    Studio agent's service principal or an Entra Agent ID agent identity: an AI agent candidate). Azure produces components (Logic Apps, Function Apps, App Services, runbooks, with
    `suggested_component_type`), Power Platform `ai_agent` (Copilot Studio agents read from Dataverse) and components
    (cloud flows), and Halo applications (assets). A `license`
    observation (produced by the Entra licences source) is one SKU's seat counts: it never becomes a
    record, so it stays out of the candidates queue and feeds the licence pools' seat review instead
    (money/licences.py; `data`: sku_id, sku_part_number, seats_purchased (prepaidUnits.enabled), seats_assigned
    (consumedUnits), seats_warning, seats_suspended, seats_locked_out, capability_status, applies_to)."""

    APPLICATION = "application", "Application"
    COMPONENT = "component", "Component"
    SUGGESTED_INTEGRATION = "suggested_integration", "Suggested integration"
    SUGGESTED_OWNER = "suggested_owner", "Suggested owner"
    LICENSE = "license", "License seat counts"
    AI_AGENT = "ai_agent", "AI agent"


# Kinds that never become records: left out of the candidates queue and its counts.
NOT_CANDIDATES = (ObservationKind.LICENSE,)


class Observation(OrgScoped):
    """What a source last said about one of its objects (ADR-007): kept apart from curated records, never written
    back. `data` is the source's mapped values (name, description, homepage, owners as suggested owners …);
    `fingerprint` hashes the fields that make a dismissed candidate come back. `gone_at` is set when a run
    no longer sees the object; it is cleared if the object returns."""

    connector = models.ForeignKey(Connector, on_delete=models.CASCADE, related_name="observations")
    kind = models.CharField(max_length=30, choices=ObservationKind.choices)
    external_id = models.CharField(max_length=200)
    display_name = models.CharField(max_length=400)
    data = models.JSONField(default=dict)
    fingerprint = models.CharField(max_length=64)
    suggested_component_type = models.CharField(max_length=30, choices=ComponentType.choices, blank=True)
    ai_agent = models.BooleanField(default=False)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    last_changed_at = models.DateTimeField()
    gone_at = models.DateTimeField(null=True, blank=True)
    # Reconciliation: the source still has the object but the connector's scope leaves it out (a filter
    # skipped it, or the scope no longer reads its kind, subscription or environment): the filter's key and since when.
    # Such an observation keeps its state and is never "no longer seen"; both clear when it comes back into scope.
    out_of_scope = models.CharField(max_length=40, blank=True)
    out_of_scope_at = models.DateTimeField(null=True, blank=True)

    class Meta(OrgScoped.Meta):
        db_table = "catalog_observation"
        ordering = ["display_name", "id"]
        constraints = [
            CATALOG_ORG_KEY,
            models.UniqueConstraint(
                fields=["connector", "kind", "external_id"], name="catalog_observation_unique_external"
            ),
        ]
        indexes = [GinIndex(fields=["display_name"], name="catalog_observation_name_trgm", opclasses=["gin_trgm_ops"])]


class DecisionKind(models.TextChoices):
    LINKED = "linked", "Linked"
    CREATED = "created", "Created"
    DISMISSED = "dismissed", "Dismissed"


class GoneResolution(models.TextChoices):
    KEPT = "kept", "Kept the record"
    ARCHIVED = "archived", "Archived the record"


class ReconciliationDecision(OrgScoped):
    """The current decision on an observation. Undoing deletes the row; the audit log keeps the history.
    `fingerprint` is the observation's at decision time, so a dismissed candidate returns only after a relevant change.
    Several observations may point at one record; migration catalog 0017 adds composite keys so the
    observation and the record share the row's organization.

    Reconciliation (discovery 0012): a linked or created observation the source no longer lists (`gone_at`) is
    "no longer seen in source" in Needs attention until someone resolves it (`gone_acknowledged_at`, `_by`,
    `gone_resolution` kept or archived, `gone_note`). The engine clears these when the object comes back, so a later
    disappearance asks again."""

    observation = models.OneToOneField(Observation, on_delete=models.CASCADE, related_name="decision")
    decision = models.CharField(max_length=10, choices=DecisionKind.choices)
    resource = models.ForeignKey(
        Resource, null=True, blank=True, on_delete=models.PROTECT, related_name="discovery_links"
    )
    reason = models.CharField(max_length=500, blank=True)
    fingerprint = models.CharField(max_length=64)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    gone_acknowledged_at = models.DateTimeField(null=True, blank=True)
    gone_acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    gone_resolution = models.CharField(max_length=10, choices=GoneResolution.choices, blank=True)
    gone_note = models.CharField(max_length=500, blank=True)

    class Meta(OrgScoped.Meta):
        db_table = "catalog_reconciliationdecision"
        constraints = [
            CATALOG_ORG_KEY,
            models.CheckConstraint(
                condition=(Q(decision="dismissed") & Q(resource__isnull=True))
                | (~Q(decision="dismissed") & Q(resource__isnull=False)),
                name="catalog_decision_resource_matches",
            ),
        ]
        indexes = [models.Index(fields=["organization", "resource"], name="catalog_decision_resource")]


class SourceDifference(OrgScoped):
    """ "Source differs" (reconciliation): one curated field of a linked or created observation's record
    whose source value differs (discovery/drift.py). Kept current by every run, every decision and every record edit;
    never applied on its own: applying one is an ordinary audited record edit. `since` is when this source value
    started to differ (a new source value restarts it); `ignored_at` hides it until the source value changes again.
    Composite key (discovery 0012) keeps the observation in the row's organization."""

    observation = models.ForeignKey(Observation, on_delete=models.CASCADE, related_name="differences")
    field = models.CharField(max_length=60)
    source_value = models.TextField()
    since = models.DateTimeField()
    ignored_at = models.DateTimeField(null=True, blank=True)
    ignored_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(OrgScoped.Meta):
        verbose_name = "source difference"
        ordering = ["observation", "field"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["observation", "field"], name="discovery_difference_unique_field"),
        ]
        indexes = [
            models.Index(
                fields=["organization", "observation"],
                condition=Q(ignored_at__isnull=True),
                name="discovery_difference_open",
            )
        ]


class CredentialType(models.TextChoices):
    SECRET = "secret", "Client secret"
    CERTIFICATE = "certificate", "Certificate"


class CredentialOrigin(models.TextChoices):
    APP_REGISTRATION = "app_registration", "App registration"
    ENTERPRISE_APP = "enterprise_app", "Enterprise application"


class DiscoveredCredential(OrgScoped):
    """A client secret or certificate a source saw on an observed app: its key ID,
    type, display name and end date, never key material, a secret or its hint. Upserted by every run of the observation's
    connector (discovery/credentials.py); one seen no more is marked `removed_at` (and comes back if seen again). Live
    credentials of live, undismissed observations are the `credential_expiry` reminder source and the dashboard's
    expiring discovered credentials (one source of truth: discovery/credentials.live). Composite keys (discovery 0008)
    keep the observation in the row's organization."""

    observation = models.ForeignKey(Observation, on_delete=models.CASCADE, related_name="credentials")
    key_id = models.CharField(max_length=100)
    credential_type = models.CharField(max_length=20, choices=CredentialType.choices)
    display_name = models.CharField(max_length=300, blank=True)
    origin = models.CharField(max_length=20, choices=CredentialOrigin.choices)
    object_id = models.CharField(max_length=64, blank=True)  # the app registration's or service principal's object ID
    expires_on = models.DateField(null=True, blank=True)  # endDateTime's UTC date
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta(OrgScoped.Meta):
        verbose_name = "discovered credential"
        ordering = ["expires_on", "id"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["observation", "key_id"], name="discovery_credential_unique_key"),
        ]
        indexes = [
            models.Index(
                fields=["organization", "expires_on"],
                condition=Q(removed_at__isnull=True),
                name="discovery_credential_live",
            )
        ]


# ---- shadow IT (source entra_shadow_it, discovery/entra_shadow_it.py) -------------------------------------
#
# What is stored, and nothing more (privacy): per connector a cursor and a random key;
# per third-party app per UTC day the sign-in count, the distinct users that day and a HyperLogLog sketch of keyed
# hashes of the signing-in users' object IDs (`users_sketch`, so distinct users over several days can be counted
# without keeping who they were); per app its current summary. Never a sign-in row, a user ID, a name, an address, a
# location or a device, and never who consented: only how many.


class ShadowItState(OrgScoped):
    """The shadow IT source's state per connector. `read_until` is the cursor: every sign-in created before it has
    been counted (null until a run read sign-ins); a run reads from it to now. `sketch_key` is the random key user
    object IDs are hashed with before they enter a sketch; it never leaves the database (no response, log, audit event
    or organization export: catalog/org_lifecycle.SECRET_COLUMNS). Written only by a run, in the run's transaction.
    Composite key (discovery 0015) keeps the connector in the row's organization."""

    connector = models.OneToOneField(Connector, on_delete=models.CASCADE, related_name="shadow_it_state")
    read_until = models.DateTimeField(null=True, blank=True)
    # The sign-in window the next run starts with, in minutes (3 days down to an hour): halved when a window was too
    # big for a read or a run, doubled back after a complete one, so a large tenant's runs keep advancing.
    window_minutes = models.PositiveIntegerField(default=3 * 24 * 60)
    sketch_key = models.BinaryField(max_length=32)

    class Meta(OrgScoped.Meta):
        verbose_name = "shadow IT state"


class ShadowItUsageDay(OrgScoped):
    """One third-party app's interactive sign-ins on one UTC day, as far as runs have read them: the count of
    successful sign-ins, the distinct users that day (`users`, the sketch's estimate) and `users_sketch`, a zlib-packed
    HyperLogLog (4,096 registers) of HMAC-SHA256(sketch_key, user object ID). A later run that reads more of the day
    adds to the count and merges into the sketch. Rows older than the rolling window (30 days) are deleted by the next
    run. Composite key (discovery 0015) keeps the connector in the row's organization."""

    connector = models.ForeignKey(Connector, on_delete=models.CASCADE, related_name="+")
    app_id = models.CharField(max_length=36)
    day = models.DateField()
    sign_ins = models.PositiveIntegerField(default=0)
    users = models.PositiveIntegerField(default=0)
    users_sketch = models.BinaryField()

    class Meta(OrgScoped.Meta):
        verbose_name = "shadow IT usage day"
        ordering = ["connector", "app_id", "day"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["connector", "app_id", "day"], name="discovery_shadowday_unique"),
        ]
        indexes = [models.Index(fields=["connector", "day"], name="discovery_shadowday_window")]


class ConsentType(models.TextChoices):
    ADMIN = "admin", "Admin consent"  # an AllPrincipals grant: for everyone in the tenant
    USER = "user", "User consent"  # Principal grants only: some people consented for themselves
    NONE = "none", "No consent grant"  # signed in to (SAML or OIDC without delegated scopes), nobody consented


class ShadowItApp(OrgScoped):
    """One third-party app (by application ID) the last run saw people sign in to in the rolling window or found an
    OAuth consent grant for, with its summary: users and sign-ins over the window (recomputed from the usage days),
    the last sign-in, the consent type, how many people consented (never who), the scopes granted and the high-risk
    ones. Apps the catalog already has are kept too (`in_catalog`), so the overview shows them linked with their usage;
    the others are discovery candidates (observations of the same connector). Rewritten by every run; an app the run no
    longer sees is deleted. Composite key (discovery 0015) keeps the connector in the row's organization."""

    connector = models.ForeignKey(Connector, on_delete=models.CASCADE, related_name="shadow_it_apps")
    app_id = models.CharField(max_length=36)
    service_principal_id = models.CharField(max_length=36, blank=True)
    display_name = models.CharField(max_length=400)
    publisher = models.CharField(max_length=300, blank=True)
    verified_publisher = models.CharField(max_length=300, blank=True)
    users_30d = models.PositiveIntegerField(default=0)
    sign_ins_30d = models.PositiveIntegerField(default=0)
    last_sign_in_at = models.DateTimeField(null=True, blank=True)
    consent = models.CharField(max_length=10, choices=ConsentType.choices, default=ConsentType.NONE)
    consenting_users = models.PositiveIntegerField(default=0)
    scopes = models.JSONField(default=list)
    risky_scopes = models.JSONField(default=list)
    risky = models.BooleanField(default=False)
    in_catalog = models.BooleanField(default=False)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()

    class Meta(OrgScoped.Meta):
        verbose_name = "shadow IT app"
        ordering = ["-users_30d", "display_name", "id"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["connector", "app_id"], name="discovery_shadowapp_unique"),
        ]
        indexes = [models.Index(fields=["organization", "app_id"], name="discovery_shadowapp_app")]


# ---- SSO and MFA coverage (source `entra_coverage`, discovery/entra_coverage.py) --------------------------


class SsoMode(models.TextChoices):
    """How people sign in to an app, from its service principal's `preferredSingleSignOnMode` (null for older SAML and
    OIDC apps: unknown, never "no SSO", unless a SAML signing certificate says SAML)."""

    SAML = "saml", "SAML"
    OIDC = "oidc", "OpenID Connect"
    PASSWORD = "password", "Password-based SSO"
    NONE = "none", "No SSO"
    UNKNOWN = "unknown", "Unknown"


class ProvisioningState(models.TextChoices):
    ON = "on", "On"
    OFF = "off", "Off"
    UNKNOWN = "unknown", "Unknown"


class MfaCoverage(models.TextChoices):
    """What the tenant's enabled conditional access policies require of people signing in to an app."""

    ALL_USERS = "all_users", "MFA required for all users"
    SOME_USERS = "some_users", "MFA required for some users"
    NONE = "none", "No MFA policy"
    UNKNOWN = "unknown", "Unknown"
    # An enabled policy blocks sign-in for all users: nobody signs in, so no MFA gap either.
    BLOCKED = "blocked", "Sign-in blocked for all users"


class AppCoverage(OrgScoped):
    """Derived data, never curated: one Entra app (by application ID) that a catalog application is linked to, as the
    last run of an `entra_coverage` connector saw it. Single sign-on (`sso`, with the raw `sso_mode` and why), SCIM
    provisioning (`scim`, why, and when its jobs were last read: at most a capped number of apps a run, least recently
    checked first, the rest keep their last answer), and conditional access (`mfa`, why, when its policies were read (`mfa_checked_at`), and `policies`: the policies
    that cover or may cover the app, each {id, name, state, users, mfa, effect, exclusions}). Rewritten by every run in
    the run's transaction; an app the run no longer finds (no longer linked, or gone from the tenant) is deleted.
    Nothing here writes to a catalog record: the record's own sso_enabled and scim_provisioning stay as people set
    them, and a difference is shown, never applied. Composite key (discovery 0016) keeps the connector in the row's
    organization."""

    connector = models.ForeignKey(Connector, on_delete=models.CASCADE, related_name="coverage")
    app_id = models.CharField(max_length=36)
    service_principal_id = models.CharField(max_length=36, blank=True)
    display_name = models.CharField(max_length=400)
    sso = models.CharField(max_length=10, choices=SsoMode.choices, default=SsoMode.UNKNOWN)
    sso_mode = models.CharField(max_length=40, blank=True)  # preferredSingleSignOnMode as Graph wrote it, or ""
    sso_detail = models.CharField(max_length=300, blank=True)
    scim = models.CharField(max_length=10, choices=ProvisioningState.choices, default=ProvisioningState.UNKNOWN)
    scim_detail = models.CharField(max_length=300, blank=True)
    scim_checked_at = models.DateTimeField(null=True, blank=True)
    mfa = models.CharField(max_length=12, choices=MfaCoverage.choices, default=MfaCoverage.UNKNOWN)
    mfa_detail = models.CharField(max_length=300, blank=True)
    mfa_checked_at = models.DateTimeField(null=True, blank=True)  # when the policies behind `mfa` were read
    policies = models.JSONField(default=list)
    first_seen_at = models.DateTimeField()
    seen_at = models.DateTimeField()

    class Meta(OrgScoped.Meta):
        verbose_name = "app coverage"
        verbose_name_plural = "app coverage"
        ordering = ["display_name", "id"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["connector", "app_id"], name="discovery_coverage_unique"),
        ]
        indexes = [models.Index(fields=["organization", "app_id"], name="discovery_coverage_app")]


# ---- licence usage (source `entra_licence_usage`, discovery/entra_licence_usage.py) -----------------------
#
# What is stored, and nothing more (privacy): per licensed user, a keyed hash of the
# user's object ID, the SKU IDs assigned, whether the account is enabled, a guest or excluded as a service account, the
# date (not the time) of the last successful sign-in and of creation, and the Person here it is linked to when there
# is one. Never a name, an email address, a user principal name, the object ID itself, an IP address or a device. Users
# with no licence aren't stored. Every run rewrites the connector's rows, so a user no longer licensed (or deleted)
# goes with the next run.


class LicensedUser(OrgScoped):
    """Derived data, never curated: one licensed user of the tenant as the last run of an `entra_licence_usage`
    connector saw them. `user_key` is HMAC-SHA256 of the user's object ID keyed by the installation's secret key and the
    connector (discovery/entra_licence_usage.user_key), so rows can be told apart and shown as "Unlinked user" plus a
    short hash, but the object ID can't be read back or tested for without that key. `sku_ids` are the assigned
    licences' skuId (direct or group-based, as `assignedLicenses` lists them). `sign_in_known` is false when the run
    couldn't read signInActivity (no Entra ID P1, no AuditLog.Read.All, or turned off in the scope): the last sign-in
    is then unknown, never "never". `last_sign_in_on` is the UTC date of lastSuccessfulSignInDateTime; `attempted`
    says a sign-in was recorded at all (interactive or not, successful or not), so a user with only failures isn't
    "never signed in". `person` is the Person here whose Entra sign-in identity has the object ID, or whose email is
    the user's mail or user principal name, matched at the run and never stored otherwise. Rewritten by every run in
    the run's transaction. Composite keys (discovery 0017) keep the connector and the person in the row's
    organization."""

    connector = models.ForeignKey(Connector, on_delete=models.CASCADE, related_name="licensed_users")
    user_key = models.CharField(max_length=64)
    sku_ids = ArrayField(models.CharField(max_length=36), default=list)
    account_enabled = models.BooleanField(default=True)
    guest = models.BooleanField(default=False)
    service_account = models.BooleanField(default=False)  # excluded by the connector's scope
    sign_in_known = models.BooleanField(default=False)
    last_sign_in_on = models.DateField(null=True, blank=True)
    attempted = models.BooleanField(default=False)
    created_on = models.DateField(null=True, blank=True)
    # The SKUs of sku_ids held only through a group (licenseAssignmentStates): reclaimed by group membership.
    group_sku_ids = ArrayField(models.CharField(max_length=36), default=list)
    person = models.ForeignKey(
        "catalog.Person", null=True, blank=True, on_delete=models.SET_NULL, related_name="licensed_users"
    )
    seen_at = models.DateTimeField()

    class Meta(OrgScoped.Meta):
        verbose_name = "licensed user"
        ordering = ["connector", "user_key"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["connector", "user_key"], name="discovery_licenseduser_unique"),
        ]
        indexes = [
            GinIndex(fields=["sku_ids"], name="discovery_licenseduser_skus"),
            models.Index(fields=["organization", "person"], name="discovery_licenseduser_person"),
        ]


# ---- the assistant (capability `assistant`, discovery/assistant.py) ------------------------------


class AssistantSettings(OrgScoped):
    """One organization's assistant settings (Admin). Off until an Admin turns it on. The Anthropic API key is a secret
    slot (`api_key_*`, core/credentials.SLOTS): write-only, never returned, audited or logged; without one, the
    installation's key (ANTHROPIC_API_KEY, settings.SOFTWARDEN_ASSISTANT_INSTALLATION_KEY) is used if it has one. The
    model is installation-wide (SOFTWARDEN_ASSISTANT_MODEL). `monthly_cap_usd` bounds what the organization's calls may
    cost in a calendar month of its timezone. A key the API refuses turns the assistant off and is noted in
    `key_refused_at` / `key_refused_detail` for the Admin, until the key is replaced or the assistant turned on again."""

    enabled = models.BooleanField(default=False)
    api_key_ciphertext = models.TextField(blank=True, default="")
    api_key_key_id = models.CharField(max_length=20, blank=True, default="")
    api_key_set_at = models.DateTimeField(null=True, blank=True)
    monthly_cap_usd = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("10.00"))
    key_refused_at = models.DateTimeField(null=True, blank=True)
    key_refused_detail = models.CharField(max_length=300, blank=True)

    class Meta(OrgScoped.Meta):
        verbose_name = "assistant settings"
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["organization"], name="discovery_assistantsettings_one_per_org"),
        ]


class AssistantTrigger(models.TextChoices):
    AFTER_RUN = "after_run", "After a discovery run"
    MANUAL = "manual", "Requested by an Editor"
    FOLLOW_UP = "follow_up", "Follow-up of a batch with candidates left over"


class AssistantBatchStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    STOPPED = "stopped", "Stopped"  # cap reached, worker stopped: what was suggested so far stays
    FAILED = "failed", "Failed"


ACTIVE_BATCH_STATUSES = (AssistantBatchStatus.QUEUED, AssistantBatchStatus.RUNNING)


class AssistantBatch(OrgScoped):
    """One pass of the assistant over undecided candidates (one connector's, or all), as a `discovery.assistant` job
    whose payload is this row's ID only. `counts`: {candidates, unchanged, suggested, refused, failed, skipped
    (decided while the call was out), calls, remaining}. `stop_reason` says why it ended early (cap_reached, key_refused, ...; assistant.STOP_LABELS). At most
    one queued or running batch per organization."""

    trigger = models.CharField(max_length=10, choices=AssistantTrigger.choices)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    connector = models.ForeignKey(Connector, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    connector_run_id = models.UUIDField(null=True, blank=True)  # the run that queued it (no key: old runs are pruned)
    status = models.CharField(max_length=10, choices=AssistantBatchStatus.choices, default=AssistantBatchStatus.QUEUED)
    job_id = models.UUIDField(null=True, blank=True)  # jobs are pruned after 30 days; the batch stays
    queued_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    counts = models.JSONField(default=dict)
    stop_reason = models.CharField(max_length=30, blank=True)
    error = models.CharField(max_length=500, blank=True)

    class Meta(OrgScoped.Meta):
        verbose_name = "assistant batch"
        ordering = ["-queued_at", "-id"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(
                fields=["organization"],
                condition=Q(status__in=["queued", "running"]),
                name="discovery_assistantbatch_one_active",
            ),
        ]


class AssistantOutcome(models.TextChoices):
    """What one API call came to (the usage log)."""

    PENDING = "pending", "In progress"  # its estimate is reserved against the cap until it finishes
    OK = "ok", "Answered"
    REFUSAL = "refusal", "Declined by the model"
    MAX_TOKENS = "max_tokens", "Answer cut off"
    INVALID = "invalid", "Answer not usable"
    RATE_LIMITED = "rate_limited", "Rate limited"
    KEY_REFUSED = "key_refused", "API key refused"
    BAD_REQUEST = "bad_request", "Request refused"
    API_ERROR = "api_error", "API error"
    CONNECTION_ERROR = "connection_error", "Not reached"
    ABANDONED = "abandoned", "Never finished"  # still pending after the job's lease: its reserved cost stays


class AssistantUsage(OrgScoped):
    """One Anthropic API call (the usage log): the model, the four token counts the API reports, the cost in USD
    computed from the price table (discovery/assistant.PRICES), what it came to and how many candidates it carried.
    A call's worst-case estimate is reserved here (`pending`, `cost_usd` the estimate) before it is sent, so the cap
    holds even when a worker dies mid-call; the answer replaces it with the actual cost."""

    batch = models.ForeignKey(AssistantBatch, null=True, blank=True, on_delete=models.SET_NULL, related_name="calls")
    model = models.CharField(max_length=100)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cache_read_input_tokens = models.PositiveIntegerField(default=0)
    cache_creation_input_tokens = models.PositiveIntegerField(default=0)
    estimated_cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0"))
    outcome = models.CharField(max_length=20, choices=AssistantOutcome.choices, default=AssistantOutcome.PENDING)
    candidate_count = models.PositiveSmallIntegerField(default=0)

    class Meta(OrgScoped.Meta):
        verbose_name = "assistant usage"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["organization", "created_at"], name="discovery_usage_month")]


class SuggestionState(models.TextChoices):
    READY = "ready", "Suggested"
    REFUSED = "refused", "Unavailable: declined by the model"
    FAILED = "failed", "Unavailable"


class SuggestionAction(models.TextChoices):
    LINK = "link", "Link to an existing record"
    CREATE = "create", "Create a record"
    DISMISS = "dismiss", "Dismiss"


class SuggestionOutcome(models.TextChoices):
    """What the people deciding did with a suggestion. Nothing is ever applied on its own."""

    PENDING = "pending", "Not decided yet"
    ACCEPTED = "accepted", "Accepted"  # the decision taken is the suggested one (through Accept, or by hand)
    OVERRIDDEN = "overridden", "Overridden"  # another decision was taken
    IGNORED = "ignored", "Ignored"  # decided after the candidate had changed since the suggestion
    REJECTED = "rejected", "Rejected"  # an Editor said no to it (the candidate stays undecided)
    SUPERSEDED = "superseded", "Superseded"  # a newer suggestion replaced it (the candidate changed)


class AssistantSuggestion(OrgScoped):
    """The assistant's suggestion for one candidate at one fingerprint: link (to `target`, one of the records offered
    for it, never anything else), create or dismiss, with a confidence from 0 to 1 and a short reason; or why there is
    none (`state` refused or failed, `detail`). A candidate whose fingerprint is unchanged since its last ready or
    refused suggestion isn't sent again. The decision taken on the candidate is recorded here (`outcome`, `outcome_by`,
    `decided_action`, `decided_resource`); the audit log keeps every change. Composite keys (discovery 0014) keep the
    observation, batch, usage and records in the row's organization."""

    observation = models.ForeignKey(Observation, on_delete=models.CASCADE, related_name="assistant_suggestions")
    fingerprint = models.CharField(max_length=64)
    batch = models.ForeignKey(AssistantBatch, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    usage = models.ForeignKey(AssistantUsage, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    model = models.CharField(max_length=100)
    state = models.CharField(max_length=10, choices=SuggestionState.choices)
    action = models.CharField(max_length=10, choices=SuggestionAction.choices, blank=True)
    target = models.ForeignKey(Resource, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    detail = models.CharField(max_length=300, blank=True)
    outcome = models.CharField(max_length=12, choices=SuggestionOutcome.choices, default=SuggestionOutcome.PENDING)
    outcome_at = models.DateTimeField(null=True, blank=True)
    outcome_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    via_accept = models.BooleanField(default=False)
    decided_action = models.CharField(max_length=10, choices=SuggestionAction.choices, blank=True)
    decided_resource = models.ForeignKey(Resource, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    outcome_note = models.CharField(max_length=500, blank=True)

    class Meta(OrgScoped.Meta):
        verbose_name = "assistant suggestion"
        ordering = ["-created_at", "-id"]
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint(fields=["observation", "fingerprint"], name="discovery_suggestion_unique_fp"),
            models.CheckConstraint(
                condition=(Q(state="ready") & ~Q(action="")) | (~Q(state="ready") & Q(action="")),
                name="discovery_suggestion_action_when_ready",
            ),
        ]
