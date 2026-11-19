"""Tenancy, accounts, audit, the worker heartbeat, jobs and schedules.

These models moved here from the catalog app in the split. Their tables keep the names they were created
with (`catalog_*`), so the move changed no SQL; see core/migrations/0001_initial.py."""

import uuid
from datetime import time
from typing import Any

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import IntegrityError, models, transaction
from django.db.models.fields.json import KeyTransform
from django.db.models.functions import Lower, Now
from django.utils import timezone
from django.utils.text import slugify


def default_thresholds():
    return [90, 60, 30, 7]


class Edition(models.TextChoices):
    """An edition. Only core/capabilities.py reads an organization's edition; features check capability names instead."""

    COMMUNITY = "community", "Community"
    CLOUD = "cloud", "Cloud"
    ENTERPRISE = "enterprise", "Enterprise"


SLUG_LENGTH = 60
# Slugs no new organization gets: words of the service's own paths and roles, which a
# slug appears beside in URLs (`/login?org=`, `auth/saml/<slug>/...`) and operator commands, so an organization named
# "API" or "Admin" can't pose as the service. Such a name gets `-2`. Existing organizations keep their slug.
RESERVED_SLUGS = frozenset(
    {
        *("about", "account", "accounts", "admin", "administrator", "api", "app", "apps", "assets", "auth"),
        *("billing", "blog", "callback", "cloud", "console", "dashboard", "docs", "e2e", "help", "healthz", "invite"),
        *("invitations", "livez", "login", "logout", "mail", "media", "metadata", "mcp", "oauth", "oidc", "operator"),
        *("org", "orgs", "platform", "readyz", "root", "saml", "schema", "scim", "security", "settings", "setup"),
        *("signin", "signup", "softwarden", "sso", "staff", "static", "status", "support", "system", "v1", "v2"),
        *("v3", "verify", "webhooks", "well-known", "www"),
    }
)


def unique_org_slug(name, taken):
    """A slug for `name` that isn't in `taken` (a container of slugs already used) or RESERVED_SLUGS: the slugified
    name, then `-2`, `-3`, ... Names with nothing slugifiable become `organization`. Shared by Organization.save and
    the backfill in core/migrations/0004, so existing and new organizations get the same shape of slug."""
    base = (slugify(name) or "organization")[: SLUG_LENGTH - 6].strip("-") or "organization"
    candidate, n = base, 1
    while candidate in taken or candidate in RESERVED_SLUGS:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


class Organization(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    # A stable, URL-safe handle for operators (`set_edition <slug> ...`); set from the name on first save, not
    # changed when the name changes.
    slug = models.SlugField(max_length=SLUG_LENGTH, unique=True)
    # The edition (features check capabilities, never this string). Set only by an operator (`manage.py set_edition`) or platform staff (the platform
    # console, catalog/platform_admin.change_edition); no tenant API writes it.
    edition = models.CharField(max_length=20, choices=Edition.choices, default=Edition.COMMUNITY)
    # The licence key as a secret slot: Fernet ciphertext, the id of the key that encrypted
    # it, and when it was set. Not verified yet; never returned, audited, logged or exported. Empty until an operator
    # sets one.
    licence_key_ciphertext = models.TextField(blank=True, default="")
    licence_key_key_id = models.CharField(max_length=20, blank=True, default="")
    licence_key_set_at = models.DateTimeField(null=True, blank=True)
    # Platform admin console (catalog/platform_admin.py): when the organization was created (backfilled from
    # its oldest membership by core 0019), and a suspension set and cleared only by platform staff. While
    # `suspended_at` is set every member request but `auth/me` is refused with /problems/organization-suspended and
    # the worker leaves the organization's jobs queued.
    # Database defaults too, so an older process still running during an upgrade can insert organizations.
    created_at = models.DateTimeField(default=timezone.now, db_default=Now(), editable=False)
    suspended_at = models.DateTimeField(null=True, blank=True)
    suspended_reason = models.CharField(max_length=500, blank=True, default="", db_default="")
    # Limits (core/limits.py): whether any OrganizationLimitOverride row exists for this organization, kept by
    # catalog/platform_admin.set_limit and remove_limit, so the per-request API rate check reads overrides only for an
    # organization that has some (the row comes with every membership anyway).
    has_limit_overrides = models.BooleanField(default=False, db_default=False)
    # (Declared before `timezone`: below it, the name is that field, not django.utils.timezone.)
    timezone = models.CharField(max_length=64, default="UTC")
    review_interval_days = models.PositiveIntegerField(default=90)
    # Renewals and digest: the lead time behind Assumed and Renewal action deadlines,
    # reminder thresholds in days before the action deadline, and the local time of the daily digest.
    renewal_lead_days = models.PositiveIntegerField(default=90)
    reminder_thresholds = ArrayField(models.PositiveIntegerField(), default=default_thresholds)
    digest_time = models.TimeField(default=time(8, 0))
    coordinator = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Self-service deletion (catalog/org_lifecycle.py, capability org.self_delete): while `deletion_due_at`
    # is set the organization is scheduled for deletion; an Admin can cancel until then, and the `org.purge_deleted`
    # schedule removes every row of it afterwards. Written only by the lifecycle services, never by PATCH.
    deletion_requested_at = models.DateTimeField(null=True, blank=True)
    deletion_due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deletion_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # The purge's own bookkeeping (catalog/org_lifecycle.purge_due): when it last tried this
    # organization, and how many tries in a row raised. Due organizations are taken least recently tried first, so
    # ones that keep failing can't hold the queue; PURGE_ALERT_AFTER failures make the schedule fail (System page).
    purge_attempted_at = models.DateTimeField(null=True, blank=True)
    purge_failures = models.PositiveIntegerField(default=0, db_default=0)
    # Per-organization settings that don't warrant a column; the columns above stay columns. Declared last:
    # inside this class body the name shadows django.conf.settings.
    settings = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "catalog_organization"

    def __str__(self):
        return self.name

    SLUG_ATTEMPTS = 5

    def save(self, *args, **kwargs):
        """Chooses the slug on first save. The choice is read-then-write, so two organizations of the same name
        created at once can pick the same slug: the insert runs in a savepoint and, when another organization took
        the slug meanwhile, the next free one is tried."""
        if self.slug:
            super().save(*args, **kwargs)
            return
        taken = set(
            Organization.objects.exclude(pk=self.pk)
            .filter(slug__startswith=(slugify(self.name) or "organization")[:20])
            .values_list("slug", flat=True)
        )
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = {*kwargs["update_fields"], "slug"}
        for attempt in range(1, self.SLUG_ATTEMPTS + 1):
            self.slug = unique_org_slug(self.name, taken)
            try:
                with transaction.atomic():
                    super().save(*args, **kwargs)
                return
            except IntegrityError:
                clash = Organization.objects.exclude(pk=self.pk).filter(slug=self.slug).exists()
                if not clash or attempt == self.SLUG_ATTEMPTS:
                    self.slug = ""
                    raise
                taken.add(self.slug)


def active_organizations():
    """Organizations not suspended by the platform operator: what every per-organization schedule loops
    over, so a suspended organization produces no digests, notices, reminders, term moves, webhook posts or connector
    runs while suspended, and nothing piles up to flood out when it is unsuspended."""
    return Organization.objects.filter(suspended_at__isnull=True)


def suspended_organization_ids():
    """The ids of suspended organizations, as a subquery (core/jobs.claim, the webhook outbox)."""
    return Organization.objects.filter(suspended_at__isnull=False).values("id")


class Role(models.TextChoices):
    ADMIN = "admin", "Admin"
    EDITOR = "editor", "Editor"
    VIEWER = "viewer", "Viewer"


# OrgScoped's (organization, id) key under the name it got in the catalog app. A model that moved out of catalog in
# the split uses this instead of OrgScoped.Meta.constraints, so its constraint keeps its database name. New
# models use OrgScoped.Meta.constraints and get their own app's prefix.
CATALOG_ORG_KEY = models.UniqueConstraint(fields=["organization", "id"], name="catalog_%(class)s_org_id")


class Membership(models.Model):
    """A user's role in one organization. A user may belong to several organizations, one Membership each;
    the request organization is the session's current organization (core/access.py), or an API token's own.

    `must_change_password` is about the account, not the organization: it is set on the membership whose Admin reset
    the password and honoured while any of the user's memberships carries it (core/access.py)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="memberships",
        related_query_name="membership",
    )
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="memberships")
    role = models.CharField(max_length=10, choices=Role.choices)
    must_change_password = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "catalog_membership"
        constraints = [
            CATALOG_ORG_KEY,
            models.UniqueConstraint(fields=["user", "organization"], name="catalog_membership_user_org"),
        ]


class OrgScoped(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="%(app_label)s_%(class)s_org_id"),
        ]


ISSUER_MAX = 500


class UserIdentity(models.Model):
    """An identity provider's account pinned to a User at its first OIDC sign-in. Later sign-ins
    match on (issuer, subject) only, never on email again. Entra pins `https://login.microsoftonline.com/{tid}/v2.0`
    and the `oid` claim; Google `https://accounts.google.com` (the development simulator's
    `https://accounts.google.simulated.invalid`) and `sub`; Okta and generic providers `iss` and `sub`
    (core/oidc_discovery.identity_key). The identity belongs to the account, not to an organization (core 0011 moved
    the Entra rows here from tenant and object ID)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="identities")
    issuer = models.CharField(max_length=ISSUER_MAX)
    subject = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "catalog_useridentity"
        constraints = [
            models.UniqueConstraint(fields=["issuer", "subject"], name="core_useridentity_issuer_subject"),
        ]


class Invitation(OrgScoped):
    """An Admin's invitation to join the organization (access and provisioning (a); catalog/invitations.py).

    The link `{SOFTWARDEN_BASE_URL}/invite#<token>` is shown once, when the invitation is made or resent; the token
    travels only in the fragment and in request bodies, never in a logged path, and only its SHA-256 is stored. The
    membership is created when the invitee accepts: a new account sets its name and
    password, an existing account accepts while signed in as itself. Single use (`accepted_at`), revocable
    (`revoked_at`), and good for INVITATION_DAYS (`expires_at`). One live invitation per email per organization: a new
    one revokes the previous (the partial unique constraint holds that under concurrency)."""

    email = models.CharField(max_length=254)  # lower case
    name = models.CharField(max_length=200, blank=True, default="")  # a suggestion for a new account's name
    role = models.CharField(max_length=10, choices=Role.choices)
    token_hash = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    # The current token's link that proves the mailbox went out through the organization's own mail server, as it may
    # while the installation has one organization (catalog/invitations.proof_via): its Admin
    # could have read it there, so once proof is required that link no longer creates an account. Reset by a resend.
    proof_via_organization = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            *OrgScoped.Meta.constraints,
            models.UniqueConstraint(
                fields=["organization", "email"],
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="core_invitation_one_live",
            ),
        ]


class PendingAccess(OrgScoped):
    """An unknown single sign-on identity waiting for an Admin ("access
    pending"). Written only while the organization's `sso_auto_provision` setting is on, by a sign-in at its active
    settings that matched no member (core/oidc.request_access); no User exists and nobody is signed in. An Admin
    approves with a role (an account, its membership and the pinned identity) or declines (the row goes). At most
    PENDING_ACCESS_MAX rows per organization. `subject` identifies the person at the provider and is never in a
    response."""

    issuer = models.CharField(max_length=ISSUER_MAX)
    subject = models.CharField(max_length=255)
    email = models.CharField(max_length=254, blank=True, default="")  # lower case; as the provider sent it
    name = models.CharField(max_length=200, blank=True, default="")
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()

    class Meta:
        ordering = ["-last_seen"]
        constraints = [
            *OrgScoped.Meta.constraints,
            models.UniqueConstraint(fields=["organization", "issuer", "subject"], name="core_pendingaccess_identity"),
        ]


class SsoProvider(models.TextChoices):
    ENTRA = "entra", "Microsoft Entra ID"
    OKTA = "okta", "Okta"
    GOOGLE = "google", "Google Workspace"
    OIDC = "oidc", "OpenID Connect"
    SAML = "saml", "SAML single sign-on"  # capability sso.saml, from the optional ee package


class SsoStatus(models.TextChoices):
    """`legacy`: nothing activated here; the installation's SOFTWARDEN_OIDC_* applies when the instance has one
    organization. `active`: the activated configuration signs people in. `disabled`: no SSO for this organization,
    the environment's included (an Admin's Disable or `sso_recovery --disable`)."""

    LEGACY = "legacy", "Not set up here"
    ACTIVE = "active", "On"
    DISABLED = "disabled", "Off"


class SsoSettings(OrgScoped):
    """An organization's OpenID Connect sign-in (core/sso.py; a draft / test / activate flow).

    The active fields are what sign-in uses; an Admin edits the draft fields, which bumps `draft_revision`, runs a test
    sign-in of their own account against the draft (`draft_tested_revision`, `draft_test_ok`, when, who, a detail) and
    activates a draft only while its passed test is of the current revision; activation copies it to the active side
    and bumps `active_revision`. Every sign-in pins (mode, organization, revision) in the session, so a change in
    between refuses the callback. Secret slots `secret` and `draft_secret` (Fernet, core/credentials.py).

    The `saml` preset (enterprise, ee/saml) uses the `saml_*` fields instead of the OpenID Connect ones: the IdP's
    entity ID (copied to `issuer`, which identities are pinned on), its HTTP-Redirect sign-in URL and its signing
    certificates (base64 DER, no PEM armour); `label` names its button. It needs no client ID or secret."""

    status = models.CharField(max_length=10, choices=SsoStatus.choices, default=SsoStatus.LEGACY)
    # active: what sign-in uses
    provider = models.CharField(max_length=10, choices=SsoProvider.choices, blank=True, default="")
    tenant = models.CharField(max_length=64, blank=True, default="")
    okta_domain = models.CharField(max_length=253, blank=True, default="")
    okta_server = models.CharField(max_length=100, blank=True, default="")
    discovery_url = models.CharField(max_length=2000, blank=True, default="")
    issuer = models.CharField(max_length=ISSUER_MAX, blank=True, default="")
    hosted_domains = ArrayField(models.CharField(max_length=253), default=list, blank=True)
    client_id = models.CharField(max_length=255, blank=True, default="")
    secret_ciphertext = models.TextField(blank=True, default="")
    secret_key_id = models.CharField(max_length=20, blank=True, default="")
    secret_set_at = models.DateTimeField(null=True, blank=True)
    saml_entity_id = models.CharField(max_length=ISSUER_MAX, blank=True, default="")
    saml_sso_url = models.CharField(max_length=2000, blank=True, default="")
    saml_certs = ArrayField(models.TextField(), default=list, blank=True)
    label = models.CharField(max_length=60, blank=True, default="")
    active_revision = models.PositiveIntegerField(default=0)
    activated_at = models.DateTimeField(null=True, blank=True)
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # draft: what an Admin edits and tests
    draft_provider = models.CharField(max_length=10, choices=SsoProvider.choices, blank=True, default="")
    draft_tenant = models.CharField(max_length=64, blank=True, default="")
    draft_okta_domain = models.CharField(max_length=253, blank=True, default="")
    draft_okta_server = models.CharField(max_length=100, blank=True, default="")
    draft_discovery_url = models.CharField(max_length=2000, blank=True, default="")
    draft_issuer = models.CharField(max_length=ISSUER_MAX, blank=True, default="")
    draft_hosted_domains = ArrayField(models.CharField(max_length=253), default=list, blank=True)
    draft_client_id = models.CharField(max_length=255, blank=True, default="")
    draft_secret_ciphertext = models.TextField(blank=True, default="")
    draft_secret_key_id = models.CharField(max_length=20, blank=True, default="")
    draft_secret_set_at = models.DateTimeField(null=True, blank=True)
    draft_saml_entity_id = models.CharField(max_length=ISSUER_MAX, blank=True, default="")
    draft_saml_sso_url = models.CharField(max_length=2000, blank=True, default="")
    draft_saml_certs = ArrayField(models.TextField(), default=list, blank=True)
    draft_label = models.CharField(max_length=60, blank=True, default="")
    draft_revision = models.PositiveIntegerField(default=0)
    draft_tested_revision = models.PositiveIntegerField(null=True, blank=True)
    draft_test_ok = models.BooleanField(null=True, blank=True)
    draft_tested_at = models.DateTimeField(null=True, blank=True)
    draft_tested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    draft_test_code = models.CharField(max_length=60, blank=True, default="")
    draft_test_detail = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        constraints = [
            *OrgScoped.Meta.constraints,
            models.UniqueConstraint(fields=["organization"], name="core_ssosettings_one_per_org"),
        ]

    @property
    def draft_is_tested(self) -> bool:
        return (
            bool(self.draft_revision)
            and self.draft_test_ok is True
            and self.draft_tested_revision == self.draft_revision
        )


class SsoDomain(OrgScoped):
    """A sign-in domain: a work email at this domain finds this organization on the sign-in page
    (core/sso.org_for_sign_in) once it is verified. One organization holds a verified domain. With
    SOFTWARDEN_SSO_DOMAIN_VERIFICATION (cloud) a claim starts pending and is verified by a DNS TXT record carrying
    `challenge` (core/sso.verify_domain); several organizations may claim a domain while
    pending. Without it a claim is verified at once and the first organization keeps it."""

    domain = models.CharField(max_length=253)  # lower case, IDNA
    verified_at = models.DateTimeField(null=True, blank=True)  # null: pending (routes nobody)
    # The value the TXT record `_softwarden-challenge.<domain>` must carry (`softwarden-domain-verification=<value>`).
    # Published in DNS by design, so not a secret.
    challenge = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["domain"]
        constraints = [
            *OrgScoped.Meta.constraints,
            models.UniqueConstraint(fields=["organization", "domain"], name="core_ssodomain_org_domain"),
            models.UniqueConstraint(
                fields=["domain"], condition=models.Q(verified_at__isnull=False), name="core_ssodomain_verified"
            ),
        ]


class SamlAssertion(OrgScoped):
    """A SAML assertion that signed someone in (or tested a draft) at this organization's service provider: the replay
    cache (ee/saml/sp.py). A second use of the same assertion ID from the same IdP is refused (`replay`). Kept until
    `expires_at`, the assertion's latest NotOnOrAfter plus the clock skew, after which the assertion is refused as
    expired anyway; the `saml.replay_purge` schedule deletes it then."""

    issuer = models.CharField(max_length=ISSUER_MAX)
    assertion_id = models.CharField(max_length=255)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [
            *OrgScoped.Meta.constraints,
            models.UniqueConstraint(fields=["organization", "issuer", "assertion_id"], name="core_samlassertion_once"),
        ]


# ---- SCIM provisioning (the optional ee package) -----------------------------------------------------------------
# The rows live in core, as SAML's do, so the migration, tenancy and upgrade guards cover them unchanged; ee/scim holds
# the protocol and the logic only.


class ScimToken(OrgScoped):
    """A bearer token an identity provider's provisioning service (Entra) calls `/scim/v2/<slug>/` with (ee/scim/
    tokens.py). The secret is `scim_` plus 43 URL-safe characters, shown once; only its SHA-256 is stored, and `prefix`
    (its first 13 characters) finds the candidates that are then compared in constant time. It can't call /api/v1.
    Good until `expires_at` (at most two years), revocable, never deleted, so the audit log keeps naming it."""

    name = models.CharField(max_length=100)
    token_hash = models.CharField(max_length=64, unique=True)
    prefix = models.CharField(max_length=16, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    expires_at = models.DateTimeField()
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [*OrgScoped.Meta.constraints]


class ScimUser(OrgScoped):
    """A user as the identity provider provisioned it (ee/scim/provisioning.py).
    `id` is the SCIM id. `user` is the account it maps to and `membership` that account's membership here; both null
    while the account belongs to other organizations and hasn't accepted the `invitation` made for it (an account is
    never attached to an organization without its owner's consent), or when no email was sent. `version` is the ETag
    (W/"<version>"), raised on every change."""

    external_id = models.CharField(max_length=255, blank=True, default="")
    user_name = models.CharField(max_length=255)
    given_name = models.CharField(max_length=150, blank=True, default="")
    family_name = models.CharField(max_length=150, blank=True, default="")
    formatted_name = models.CharField(max_length=300, blank=True, default="")
    display_name = models.CharField(max_length=300, blank=True, default="")
    emails = models.JSONField(default=list, blank=True)  # [{"value", "type", "primary"}] as the provider sent them
    active = models.BooleanField(default=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="scim_users"
    )
    membership = models.ForeignKey(
        Membership, null=True, blank=True, on_delete=models.SET_NULL, related_name="scim_users"
    )
    invitation = models.ForeignKey(
        Invitation, null=True, blank=True, on_delete=models.SET_NULL, related_name="scim_users"
    )
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            *OrgScoped.Meta.constraints,
            models.UniqueConstraint("organization", Lower("user_name"), name="core_scimuser_user_name"),
            # One provisioned user per membership (ee/scim refuses a second with 409 uniqueness first).
            models.UniqueConstraint(
                fields=["organization", "membership"],
                condition=models.Q(membership__isnull=False),
                name="core_scimuser_one_per_membership",
            ),
        ]
        indexes = [models.Index(fields=["organization", "external_id"], name="core_scimuser_external_id")]


class ScimGroup(OrgScoped):
    """A group as the identity provider provisioned it. With the organization's
    `scim_role_groups` naming it, its members get that role; otherwise it is stored and nothing else changes."""

    display_name = models.CharField(max_length=300)
    external_id = models.CharField(max_length=255, blank=True, default="")
    members = models.ManyToManyField(ScimUser, through="ScimGroupMember", related_name="scim_groups")
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            *OrgScoped.Meta.constraints,
            models.UniqueConstraint("organization", Lower("display_name"), name="core_scimgroup_display_name"),
        ]


class ScimGroupMember(models.Model):
    """A provisioned user in a provisioned group (the through table). Composite keys keep both in the row's
    organization (core 0015)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    group = models.ForeignKey(ScimGroup, on_delete=models.CASCADE, related_name="member_links")
    user = models.ForeignKey(ScimUser, on_delete=models.CASCADE, related_name="group_links")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "id"], name="core_scimgroupmember_org_id"),
            models.UniqueConstraint(fields=["group", "user"], name="core_scimgroupmember_unique"),
        ]
        indexes = [models.Index(fields=["organization", "user"], name="core_scimgroupmember_user")]


class WorkerHeartbeat(models.Model):
    """The background worker's last tick, for the readiness check."""

    name = models.CharField(max_length=40, primary_key=True)
    last_tick = models.DateTimeField()

    class Meta:
        db_table = "catalog_workerheartbeat"


class AuditAppendOnly(TypeError):
    """Raised by any attempt to change or remove an audit event through the ORM."""

    def __init__(self, what="changed or removed"):
        super().__init__(f"Audit events are append-only and can't be {what}.")


AUDIT_PURGE_SETTING = "softwarden.audit_purge"


class AuditEventQuerySet(models.QuerySet):
    """Append-only: inserts, single or bulk, work; update and delete are refused here, by
    AuditEvent.save/delete, and by the database trigger of core/migrations/0008. The one removal path is purge().

    This is the default manager only. `_base_manager` deliberately stays Django's plain one: deleting a user nulls
    `actor` through it (the SET_NULL cascade), so don't set Meta.base_manager_name. The trigger is the real guard; these
    methods catch mistakes early with a clear error."""

    def update(self, **kwargs):
        raise AuditAppendOnly("updated")

    def delete(self):
        raise AuditAppendOnly("deleted")

    def bulk_update(self, objs, fields, batch_size=None):
        raise AuditAppendOnly("updated")

    def update_or_create(self, *args, **kwargs):
        raise AuditAppendOnly("updated")

    def purge(self) -> int:
        """Remove these events: the only removal path. Today only seed_scale --reset uses it, to rebuild the synthetic
        scale organization; configurable audit retention, a future feature (not built, so no capability names it;
        docs/CAPABILITIES.md), will use it and must audit the purge itself. Sets the transaction-local setting the
        trigger honours, so it must run inside transaction.atomic."""
        from django.db import connections, transaction

        if not transaction.get_connection(self.db).in_atomic_block:
            raise RuntimeError("AuditEvent purge needs a transaction.")
        sql, params = self.values("id").query.sql_with_params()
        with connections[self.db].cursor() as cursor:
            cursor.execute("SELECT set_config(%s, 'on', true)", [AUDIT_PURGE_SETTING])
            cursor.execute(f"DELETE FROM catalog_auditevent WHERE id IN ({sql})", params)  # noqa: S608 (ORM SQL)
            removed = cursor.rowcount
            cursor.execute("SELECT set_config(%s, 'off', true)", [AUDIT_PURGE_SETTING])
        return removed


class AuditEvent(models.Model):
    """One write, sign-in or account event. Append-only; see AuditEventQuerySet.

    `target` is the row the event is about (type name and id). `related_target` is the record a child row belongs to
    (an instance's application, a review's record, a confirmation's link, a cycle's renewal), so the parent's History
    shows the event. `changes` is the action's own payload, which the labels and summaries read
    (catalog/audit_log.py); `prechange` and `postchange` are full snapshots of the target before and after the write
    (catalog/snapshots.py), null when the event wrote no snapshot-able row or predates them. `request_id` groups the
    events of one HTTP request, job or command."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=60)
    target_type = models.CharField(max_length=40, blank=True)
    target_id = models.UUIDField(null=True)
    # A database default too, so an older process still running during an upgrade can insert events.
    related_target_type = models.CharField(max_length=40, blank=True, default="", db_default="")
    related_target_id = models.UUIDField(null=True, blank=True)
    request_id = models.UUIDField(null=True, blank=True)
    changes = models.JSONField(default=dict)
    prechange = models.JSONField(null=True, blank=True)
    postchange = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # The platform staff account that made this write while impersonating `actor` (core/impersonation.py);
    # null otherwise. Set by catalog/services.audit and audit_account from the request's impersonation.
    impersonated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = AuditEventQuerySet.as_manager()

    class Meta:
        db_table = "catalog_auditevent"
        ordering = ["-created_at"]
        # The (organization, id) key; nothing references audit events yet, but every tenant table carries it.
        constraints = [models.UniqueConstraint(fields=["organization", "id"], name="catalog_auditevent_org_id")]
        indexes = [
            # The log and every filtered page: newest first within the organization (the cursor breaks ties on id).
            models.Index(fields=["organization", "created_at", "id"], name="catalog_audit_org_time"),
            # A record's History and the target / related_target / request_id filters.
            models.Index(fields=["organization", "target_id"], name="catalog_audit_org_target"),
            models.Index(
                fields=["organization", "related_target_id"],
                name="catalog_audit_org_related",
                condition=models.Q(related_target_id__isnull=False),
            ),
            models.Index(
                fields=["organization", "request_id"],
                name="catalog_audit_org_request",
                condition=models.Q(request_id__isnull=False),
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AuditAppendOnly("changed")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditAppendOnly("deleted")


class JobStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    # Its lease expired without an outcome: the worker stopped or stalled mid-run. Final;
    # whoever enqueued the work decides whether to enqueue it again.
    ABANDONED = "abandoned", "Abandoned"


class Job(models.Model):
    """One unit of background work, claimed by a worker with `FOR UPDATE SKIP LOCKED`
    (core/jobs.py). `kind` names a callable registered in core.jobs; `payload` holds ids and options, never secrets
    (enqueue refuses secret-looking keys).

    `organization` is null for installation-wide work (backups, media sweeps, pruning old jobs); the only nullable
    organization among tenant tables (tests/test_tenancy.py NULLABLE_ORG). A job for an organization keeps the
    (organization, id) key like every tenant table; nothing references jobs yet.

    A claimed job holds `lease_token` until `lease_expires`; only the holder of a live token records the outcome. A
    worker that dies mid-job leaves the lease to expire, and the next tick marks the job abandoned."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # CASCADE (core 0017): an organization's jobs are worker bookkeeping about it and mean nothing once it
    # is gone, so they never block deleting it. SET_NULL would turn them into installation-wide jobs run without one.
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=80)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=JobStatus.choices, default=JobStatus.QUEUED)
    run_after = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    last_error = models.TextField(blank=True, default="")  # exception class and message, secrets removed (jobs.py)
    result = models.JSONField(default=dict, blank=True)  # a small summary of the callable's return value
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "id"], name="core_job_org_id")]
        indexes = [
            # The claim query: queued jobs whose time has come, oldest first.
            models.Index(fields=["run_after"], name="core_job_queued", condition=models.Q(status=JobStatus.QUEUED)),
            # The abandonment sweep.
            models.Index(
                fields=["lease_expires"], name="core_job_running", condition=models.Q(status=JobStatus.RUNNING)
            ),
            models.Index(fields=["organization", "status"], name="core_job_org_status"),
            # The `notify.deliveries` sweep's orphan check: a live job naming a delivery (core 0017).
            models.Index(
                KeyTransform("delivery_id", "payload"),  # the sweep compares `payload -> 'delivery_id'`
                name="core_job_delivery",
                condition=models.Q(status__in=[JobStatus.QUEUED, JobStatus.RUNNING]),
            ),
        ]


class Schedule(models.Model):
    """The run state of one recurring task. The definition (callable,
    cron, label) is code registered in core.jobs; the worker creates and updates this row. Installation-wide: one row
    per name, no organization.

    `next_due` is the cron time the schedule fires next. A worker fires it by moving `next_due` forward and taking
    the lease in one conditional UPDATE, so two workers never run the same fire. `enabled` belongs to the operator:
    the worker never changes it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=80, unique=True)
    cron = models.CharField(max_length=100)
    enabled = models.BooleanField(default=True)
    next_due = models.DateTimeField()
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires = models.DateTimeField(null=True, blank=True)
    last_started = models.DateTimeField(null=True, blank=True)
    last_finished = models.DateTimeField(null=True, blank=True)
    last_succeeded = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    last_result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]


class BootstrapState(models.Model):
    """Whether first-run setup is still open. One row,
    pk 1, installation-wide. Holds no credential: the one-time token is a private file (core/bootstrap.py).

    `completed_at` is set by the first Organization or User created through any path (the setup API, the CLI, a
    seed, Entra sign-in; signals in core/apps.py) and never cleared. Setup is open only while this row exists with no
    `completed_at` and no organization or user exists, so a missing row fails closed. Migration core 0006 marked an
    existing installation completed."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="core_bootstrapstate_singleton")]


class RateCounter(models.Model):
    """One fixed-window counter of a rate limit (core/counters.py): every process and pod
    of the installation counts in this one table, so a limit is the installation's, not each worker's. Keyed by
    (scope, key, window): the scope names the limit (`throttle`, `signin-account`, `setup-token`, `bad-token`, `scim`,
    `second-factor`), the key is a keyed hash (HMAC-SHA256 under SECRET_KEY) of what is counted (an address, an
    account's email, a token id), so no address or email is stored, and the window is the window's number since the
    epoch (time // period). `id` is the three joined, so one `INSERT ... ON CONFLICT (id) DO UPDATE ... RETURNING`
    counts a request. The counts that must not reset on the clock (`signin-account`, `second-factor`) have no window
    in `id` (window_index 0) and lapse an idle time after their last attempt (core/counters.attempt). Rows are pruned
    once `expires_at` has passed (the `counters.prune` schedule). Not tenant data: an organization's API bucket is a
    key like any other."""

    id = models.CharField(primary_key=True, max_length=100)
    scope = models.CharField(max_length=40)
    key = models.CharField(max_length=32)
    window_index = models.BigIntegerField()
    count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        indexes = [models.Index(fields=["scope", "key"], name="core_ratecounter_scope_key")]


class BackupState(models.Model):
    """The installation's backup status (core/backup.py): the last good set, the last failure, and whether
    an Admin has acknowledged the generated backup key. One row, pk 1, installation-wide, like BootstrapState. Holds no
    secret: `key_fingerprint` is a short one-way digest of the key the acknowledgement was for, so a regenerated key
    shows the warning again; it never appears in an API response or an audit event."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_set = models.CharField(max_length=80, blank=True, default="")
    last_size_bytes = models.BigIntegerField(null=True, blank=True)
    last_duration_seconds = models.FloatField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_failure = models.TextField(blank=True, default="")  # the error with configured secrets removed (jobs.py)
    key_acknowledged_at = models.DateTimeField(null=True, blank=True)
    key_acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    key_fingerprint = models.CharField(max_length=16, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="core_backupstate_singleton")]


# ---- connections ------------------------------------------------------------------------------------------


class Connection(OrgScoped):
    """Credentials an organization's background work signs in with: discovery connectors point at one (discovery.Connector). Services are in
    core/connections.py; the routes are Admin only.

    `revision` goes up whenever a value that decides how the connection signs in changes (`CONNECTION_FIELDS` and the
    secret, counted through `secret_set_at` so re-encrypting under a new key changes nothing); a rename, the notes or
    turning it off and on don't. A successful test records `tested_revision = revision`; a failed one clears it. A
    connection is usable only while `tested_revision == revision`, so a changed connection must be tested again before
    any run uses it (discovery/engine.py refuses otherwise). `test_result` is Softwarden's own sanitised words and the
    provider's error code, never a server's text, a token or a secret."""

    CONNECTION_FIELDS: tuple[str, ...] = ()
    # Not a column: the outcome of the test call that returned this row (core/connections.run_test), for its response.
    probe: Any = None

    name = models.CharField(max_length=100)
    enabled = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    secret_ciphertext = models.TextField(blank=True, default="")
    secret_key_id = models.CharField(max_length=20, blank=True, default="")
    secret_set_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)
    tested_revision = models.PositiveIntegerField(null=True, blank=True)
    tested_at = models.DateTimeField(null=True, blank=True)
    test_ok = models.BooleanField(null=True, blank=True)
    test_result = models.JSONField(default=dict, blank=True)
    tested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    objects = models.Manager()

    class Meta(OrgScoped.Meta):
        abstract = True
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        """Bump `revision` when a connection value differs from the stored row, whichever path saves it."""
        if not self._state.adding:
            stored = type(self)._base_manager.filter(pk=self.pk).first()
            if stored is not None and stored.connection_state() != self.connection_state():
                self.revision = stored.revision + 1
                if kwargs.get("update_fields") is not None:
                    kwargs["update_fields"] = {*kwargs["update_fields"], "revision"}
        super().save(*args, **kwargs)

    @property
    def test_current(self) -> bool:
        """Tested successfully at the current revision: the only state in which a run may use it."""
        return self.tested_revision is not None and self.tested_revision == self.revision

    def connection_state(self) -> dict:
        return {f: getattr(self, f) for f in (*self.CONNECTION_FIELDS, "secret_set_at")}


class MicrosoftConnection(Connection):
    """An Entra app registration signing in with client credentials (a client secret) against the global Microsoft
    identity platform (`login.microsoftonline.com`). `tenant_id` is a GUID, a tenant domain, or `simulated` (the
    synthetic tenant in core/graph_simulator.py and discovery/entra_simulator.py). Client secrets only and
    the global cloud only; certificates and national clouds are not offered."""

    CONNECTION_FIELDS = ("tenant_id", "client_id")

    tenant_id = models.CharField(max_length=253)
    client_id = models.CharField(max_length=36, blank=True)

    class Meta(Connection.Meta):
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint("organization", Lower("name"), name="core_microsoftconn_name"),
        ]


class HaloConnection(Connection):
    """A HaloITSM API application (client credentials, read-only use; capability `discovery.halo`).
    `base_url` is the instance's https address (core/outbound.check_service_url: private networks allowed for
    on-premises Halo; loopback, link-local and metadata addresses refused, on save and again when connecting). The
    host `halo.simulated.invalid` selects the synthetic Halo in core/halo.py."""

    CONNECTION_FIELDS = ("base_url", "client_id", "scope")

    base_url = models.CharField(max_length=300)
    client_id = models.CharField(max_length=200)
    scope = models.CharField(max_length=200, default="read:assets")

    class Meta(Connection.Meta):
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint("organization", Lower("name"), name="core_haloconn_name"),
        ]


class OktaConnection(Connection):
    """An Okta API service app (capability `discovery.okta`): OAuth 2.0 client credentials with a
    signed client assertion (`private_key_jwt`), read-only Okta scopes (`scope`, default `okta.apps.read`). The secret
    slot holds the app's private key (PEM or JWK), write-only. `base_url` is the org's Okta address
    (`https://{org}.okta.com`, `.oktapreview.com`, `.okta-emea.com` or a custom domain; core/okta.check_domain, public
    addresses only, on save and on every resolved address when connecting). `key_id` is the key's `kid` in the app's
    JWKS (blank: the JWK's own, else none is sent). The host `okta.simulated.invalid` selects the synthetic Okta."""

    CONNECTION_FIELDS = ("base_url", "client_id", "key_id", "scope")

    base_url = models.CharField(max_length=300)
    client_id = models.CharField(max_length=100)
    key_id = models.CharField(max_length=200, blank=True)
    scope = models.CharField(max_length=200, default="okta.apps.read")

    class Meta(Connection.Meta):
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint("organization", Lower("name"), name="core_oktaconn_name"),
        ]


class GoogleWorkspaceConnection(Connection):
    """A Google Cloud service account with domain-wide delegation (capability `discovery.google`)
    that signs in as `admin_email`, a Workspace administrator it impersonates, with the read-only Reports scope
    (core/google_workspace.py). The secret slot holds the service account key's private key and key ID, write-only;
    `service_account_email` and `service_account_client_id` are read from the key JSON when it is entered (the client
    ID is what an administrator authorizes under domain-wide delegation) and are not secret. An `admin_email` in the
    domain `google.simulated.invalid` selects the synthetic Workspace."""

    CONNECTION_FIELDS = ("admin_email", "service_account_email", "service_account_client_id")

    admin_email = models.CharField(max_length=254)
    service_account_email = models.CharField(max_length=254, blank=True)
    service_account_client_id = models.CharField(max_length=40, blank=True)

    class Meta(Connection.Meta):
        constraints = OrgScoped.Meta.constraints + [
            models.UniqueConstraint("organization", Lower("name"), name="core_googleconn_name"),
        ]


# ---- cloud: signup and the organization lifecycle ---------------------------------------------------------


class PendingSignup(models.Model):
    """A public signup waiting for its email to be confirmed (catalog/signup.py). Installation-wide: no organization
    exists yet, and no User either, so an unconfirmed address never claims an account (an invitation or a later
    signup of the real owner isn't blocked by it). The link `{SOFTWARDEN_BASE_URL}/signup/verify#<token>` is emailed;
    only the token's SHA-256 is stored, it is single use and good for SIGNUP_HOURS, and a new signup or a resend for
    the same email rotates it, so earlier links stop working. `password_hash` is the chosen password, hashed by
    Django's hasher as an account's would be; confirming asks for the password again and copies the hash to the new
    account. Expired rows are deleted by the `signup.purge` schedule. Never in a response, a log or an audit event."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.CharField(max_length=254, unique=True)  # lower case
    name = models.CharField(max_length=200)
    organization_name = models.CharField(max_length=200)
    timezone = models.CharField(max_length=64, default="UTC")
    password_hash = models.CharField(max_length=128)
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField(db_index=True)
    last_sent_at = models.DateTimeField()
    # Messages sent in the current window (SEND_LIMIT per SEND_WINDOW from window_started_at); a new window, or a new
    # signup once the row has expired, starts the count again, so nobody can lock an address out for good.
    send_count = models.PositiveSmallIntegerField(default=1)
    window_started_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class MailboxProof(models.Model):
    """The account's owner showed they read its address's mail, or the operator vouched for it
    (catalog/invitations.proven). Written when an account is created by confirming a signup or by an emailed
    invitation link, when an emailed invitation link is accepted while signed in, when a reset link is used, and for
    accounts the operator's CLI makes. Installation-wide: the account is no one organization's. Migration core 0028
    wrote one for every account that had a usable password or was platform staff. An account with neither and no
    proof (made by pending-access approval or SCIM) doesn't hold its address."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, primary_key=True, related_name="+")
    via = models.CharField(max_length=40)
    proven_at = models.DateTimeField(auto_now_add=True)


class PasswordReset(models.Model):
    """A self-service password reset link (catalog/password_reset.py). Installation-wide:
    the account it resets is no one organization's. The link `{SOFTWARDEN_BASE_URL}/reset-password#<token>` is emailed
    to the account's address; only the token's SHA-256 is stored (core/email_tokens.py). Single use (`used_at`; using
    one also ends every other open link of the account) and good for an hour (`expires_at`). The rows of the last hour
    are also the per-account send count. Rows older than a day are deleted when the next reset is asked for. Never in
    a response, a log or an audit event."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class InstallationEmail(OrgScoped):
    """One message an organization had sent through the installation's own mail server (an invitation, a resend;
    catalog/invitations.reserve_email): when, and what kind. No address, subject or body.
    The rows of the last hour and day are the organization's usage of its `invitation_emails_hour` and
    `invitation_emails_day` limits (core/limits.py), counted in the database so every app process sees one count.
    Written before the message goes out, under the limit's lock, so a refused send still counts. Rows older than two
    days are deleted when the organization sends the next one. Platform bookkeeping: left out of the data export,
    removed with the organization."""

    kind = models.CharField(max_length=40)

    class Meta:
        constraints = [*OrgScoped.Meta.constraints]
        indexes = [models.Index(fields=["organization", "created_at"], name="core_installemail_org_created")]


class ExportStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"
    EXPIRED = "expired", "Expired"


class ExportPurpose(models.TextChoices):
    EXPORT = "export", "Export on request"
    DELETION = "deletion", "Before deletion"
    # Limits, tenant backups and export: the nightly per-organization backup (capability org.tenant_backups),
    # and an export the service operator made because the organization asked through support.
    BACKUP = "backup", "Nightly backup"
    OPERATOR = "operator", "Requested by the operator"


class OrgExport(OrgScoped):
    """A full export of one organization's data (catalog/org_lifecycle.py): a zip of JSON lines per model plus the
    attachment files, built by the `org.export` job and downloadable by Admins until `expires_at` (EXPORT_DAYS after
    it is ready). Secrets are never in it: credential ciphertexts, token hashes and password hashes are left out.
    The file is `exports/<organization id>/<id>.zip` under MEDIA_ROOT; `org.exports_expire` removes it when it
    expires, and the organization's purge removes them all."""

    purpose = models.CharField(max_length=10, choices=ExportPurpose.choices, default=ExportPurpose.EXPORT)
    status = models.CharField(max_length=10, choices=ExportStatus.choices, default=ExportStatus.QUEUED)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    storage_name = models.CharField(max_length=200, blank=True, default="")
    size = models.PositiveBigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    counts = models.JSONField(default=dict, blank=True)  # rows per model and attachment files, as in the manifest
    error = models.CharField(max_length=300, blank=True, default="")
    # The `org.export` Job building it, by id only (jobs are worker bookkeeping; no foreign key to them), so an export
    # whose job died is found and marked failed (org.exports_expire).
    job_id = models.UUIDField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [*OrgScoped.Meta.constraints]


class OrganizationTombstone(models.Model):
    """The platform's trace of an organization that deleted itself (catalog/org_lifecycle.purge): written in
    the purge's transaction, outside any organization, since every row of the organization (its audit log included)
    is gone. Minimal on purpose: the former id, a SHA-256 of its name (to answer "was X a customer?" without keeping
    the name), who asked and when, and when it was removed."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    former_organization_id = models.UUIDField(unique=True)
    name_sha256 = models.CharField(max_length=64)
    requested_by_id = models.IntegerField(null=True, blank=True)  # the User's id; the account itself is kept
    requested_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField()
    rows_deleted = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-deleted_at"]


# ---- cloud: the platform admin console -------------------------------------------------------------------


class PlatformStaff(models.Model):
    """A platform operator: an account that may use `/api/v1/platform/` (catalog/platform_views.py). Granted and
    revoked only by `manage.py platform_staff`, audited in PlatformAuditEvent. A dedicated table rather than Django's
    `is_staff`, so that `createsuperuser` or an imported user row can't make someone platform staff without the
    audited command. Being staff confers no role in any organization: tenant routes still need a membership."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="platform_staff")
    granted_at = models.DateTimeField(default=timezone.now)
    # The second factor (catalog/staff_second_factor.py). The TOTP secret is the credentials slot `totp`
    # (Fernet, write-only: shown once at enrollment); confirmed once a code from it was accepted; the last accepted
    # step, so a code is never accepted twice.
    totp_ciphertext = models.TextField(blank=True, default="")
    totp_key_id = models.CharField(max_length=20, blank=True, default="")
    totp_set_at = models.DateTimeField(null=True, blank=True)
    totp_confirmed_at = models.DateTimeField(null=True, blank=True)
    totp_last_step = models.BigIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["granted_at"]


class PlatformAuditEventQuerySet(models.QuerySet):
    """Append-only, like AuditEventQuerySet; the trigger of core/migrations/0019 is the real guard. No purge path."""

    def update(self, **kwargs):
        raise AuditAppendOnly("updated")

    def delete(self):
        raise AuditAppendOnly("deleted")

    def bulk_update(self, objs, fields, batch_size=None):
        raise AuditAppendOnly("updated")

    def update_or_create(self, *args, **kwargs):
        raise AuditAppendOnly("updated")


class PlatformAuditEvent(models.Model):
    """One platform action (catalog/platform_admin.platform_audit): staff granted or revoked, an organization's edition,
    suspension, deletion or capability overrides changed by staff, an impersonation started, stopped or expired.
    Installation-wide, not tenant data: `organization_id` is a plain UUID (no foreign key), so the event outlives an
    organization's purge. `actor_email` keeps who acted when the account is later removed. `details` is redacted like
    an AuditEvent's payload (catalog/redaction.py). Append-only in the database (UPDATE and DELETE refused by a
    trigger, except nulling `actor_id`, which deleting the account does)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    actor_email = models.CharField(max_length=254, blank=True, default="")
    action = models.CharField(max_length=60)
    organization_id = models.UUIDField(null=True, blank=True)
    details = models.JSONField(default=dict)
    request_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = PlatformAuditEventQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at", "id"], name="core_platformaudit_time"),
            models.Index(
                fields=["organization_id", "created_at"],
                name="core_platformaudit_org",
                condition=models.Q(organization_id__isnull=False),
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AuditAppendOnly("changed")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AuditAppendOnly("deleted")


class OrganizationUsage(OrgScoped):
    """One organization's usage counts (catalog/usage.py), refreshed hourly by the `platform.usage` schedule so the
    platform's organization list stays fast. A snapshot: `counts` is catalog/usage.counts()'s dict as of
    `computed_at`. Platform bookkeeping: left out of the organization's data export, removed with it by the purge."""

    computed_at = models.DateTimeField()
    counts = models.JSONField(default=dict)
    last_activity_at = models.DateTimeField(null=True, blank=True)  # the newest audit event
    last_sign_in_at = models.DateTimeField(null=True, blank=True)  # the newest member sign-in

    class Meta:
        constraints = [
            *OrgScoped.Meta.constraints,
            models.UniqueConstraint(fields=["organization"], name="core_organizationusage_one_per_org"),
        ]


class OverrideEffect(models.TextChoices):
    GRANT = "grant", "Grant"
    REVOKE = "revoke", "Revoke"


class CapabilityOverride(OrgScoped):
    """A feature flag for one organization, set by platform staff (catalog/platform_admin.py): grant or revoke one
    capability name on top of the edition's set, optionally until `expires_at`. core/capabilities.capabilities applies
    the live ones: edition set, plus grants, minus revokes. One row per (organization, capability). Platform
    bookkeeping: left out of the data export; every change is audited in both streams."""

    capability = models.CharField(max_length=60)
    effect = models.CharField(max_length=10, choices=OverrideEffect.choices)
    reason = models.CharField(max_length=500)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["capability"]
        constraints = [
            *OrgScoped.Meta.constraints,
            models.UniqueConstraint(fields=["organization", "capability"], name="core_capabilityoverride_one"),
        ]


class OrganizationLimitOverride(OrgScoped):
    """One organization's value for one limit (core/limits.py), set by platform staff (catalog/platform_admin
    .set_limit) on top of its edition's default, optionally until `expires_at`. `value` None means unlimited. One row
    per (organization, limit). Lowering a limit below the current usage removes nothing; it only refuses new creates.
    Platform bookkeeping: left out of the data export; every change is audited in both streams."""

    limit = models.CharField(max_length=40)
    value = models.PositiveBigIntegerField(null=True, blank=True)
    reason = models.CharField(max_length=500)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["limit"]
        constraints = [
            *OrgScoped.Meta.constraints,
            models.UniqueConstraint(fields=["organization", "limit"], name="core_limitoverride_one"),
        ]
