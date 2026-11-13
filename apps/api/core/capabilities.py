"""Editions and capabilities (docs/CAPABILITIES.md).

An organization's edition (community, cloud, enterprise) maps to a set of capability names. Every gated feature asks
for a capability name, through `has_capability` or `require_capability`; nothing outside this module, core/limits.py
(the per-edition limit defaults) and the Organization model reads or compares the edition. tests/test_capabilities.py enforces that by scanning the source.

Platform staff can grant or revoke a capability for one organization (core.models.CapabilityOverride, the
platform admin console): `capabilities(org)` is the edition's set, plus live grants, minus live revokes. The overrides
are read once per unit of work (one request or job, core/request_context.organization_cache) and every change calls
forget_organization, so a change applies from the next request.

Community has every feature in this repository; a release that ships a feature adds its name to COMMUNITY.
ENTERPRISE_ONLY names SAML single sign-on and SCIM provisioning, which are not part of this repository (an optional
`ee` package provides them); the cloud edition's extras go in CLOUD_ONLY, and what both have but community in
CLOUD_AND_ENTERPRISE. GATED is everything community lacks; tests/test_capabilities.py requires one API test per gated
name.

Preview features: `capabilities(org)` is also where the
installation setting SOFTWARDEN_PREVIEW_FEATURES (off by default) applies, and the only place. Every edition has the
`preview` capability; while the setting is off it is taken away from every organization, with PREVIEW_ONLY (the
capabilities that exist only as preview features), whatever the edition or overrides say. Features gate on those names
as on any other: Okta and Google Workspace discovery on discovery.okta and discovery.google, ServiceNow and Halo ticket
connections on `preview` (tickets/services.SYSTEM_CAPABILITY), and the SPA hides them from `auth/me`'s list. Nothing
is deleted: turning the setting on gives the names back and the stored data is used again.
"""

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from softwarden import ee

from .models import CapabilityOverride, Edition, Organization, OverrideEffect
from .problems import CapabilityRequired, FieldErrors
from .request_context import forget_organization, organization_cache

# ---- community: the features that exist today --------------------------------------------------------------------
CATALOG = "catalog"  # applications, components, integrations, people, teams, vendors
RELATIONSHIPS = "relationships"  # relationships, dependencies and change assessments
REVIEWS = "reviews"  # record reviews and staleness
RENEWALS = "renewals"
DIGEST = "notifications.digest"  # the daily digest and Needs attention snoozes
DISCOVERY_ENTRA = "discovery.entra"
# Discovery from Azure (Logic Apps, Function Apps, App Services, Automation runbooks), Power Platform (Copilot Studio
# agents and cloud flows) and HaloITSM assets. Community, as every discovery source is; Okta and Google Workspace are
# preview features below.
DISCOVERY_AZURE = "discovery.azure"
DISCOVERY_POWERPLATFORM = "discovery.powerplatform"
DISCOVERY_HALO = "discovery.halo"
IMPORTS = "imports"  # CSV import and export
API_TOKENS = "api.tokens"
AUDIT_LOG = "audit.log"  # the in-app audit log and its CSV export; JSON lines for a SIEM is audit.export
SSO_OIDC = "sso.oidc"  # OpenID Connect single sign-on per organization (Entra, Okta, Google, generic)
AI_AGENTS = "ai_agents"  # AI agents, AI providers and models
CUSTOM_FIELDS = "custom_fields"  # admin-defined fields on records and vendors
ATTACHMENTS = "attachments"  # files and links on records and vendors
DATA_QUALITY = "data_quality"  # data-quality rules, exceptions and the completeness score
CONTRACTS = "contracts"  # contracts, contract lines and licence pools
WEBHOOKS = "webhooks"  # signed event webhooks with subscriptions and a delivery log
ORG_EXPORT = "org.export"  # an Admin exports all of the organization's data as a zip
# The vendor risk file: questionnaire, assurance report, DPA, subprocessors, risk documents and their expiry reminders.
# Community: the plan lists it as a differentiator without an (E) mark.
VENDOR_RISK = "vendor_risk"
EOL = "eol"  # end-of-life tracking of on-prem software versions via endoflife.date
CATALOG_CODE = "catalog_code"  # the catalog as YAML files in git: export, plan, apply (catalog_code/)
# The data-flow map: where classified data goes, risky flows, its graph and CSV, and the integration data-quality checks
# (catalog/data_flows.py). Community: it reads data every edition records, and the plan has no (E) mark.
DATA_FLOWS = "data_flows"
# Jira, ServiceNow and Halo ticket links on assessments and renewals, their status refresh and (opt-in per connection)
# ticket creation (tickets/). Community: the plan gives it no (E) mark. A Halo ticket connection also needs
# discovery.halo, because it signs in through the Halo connection.
TICKET_LINKS = "ticket_links"
# The installation's preview features are on (SOFTWARDEN_PREVIEW_FEATURES; see the module docstring). In every edition,
# so that one name says "on" to the API and the SPA; taken away from everyone while the setting is off. ServiceNow and
# Halo ticket connections need it (with ticket_links); Okta and Google Workspace discovery are PREVIEW_ONLY.
PREVIEW = "preview"
# Claude suggests link, create or dismiss for discovery candidates (off until an Admin turns it on).
ASSISTANT = "assistant"
# Okta and Google Workspace discovery: preview features (PREVIEW_ONLY below). Each discovery source is its own
# capability, so Google Workspace has its own name rather than riding on discovery.okta.
DISCOVERY_OKTA = "discovery.okta"
DISCOVERY_GOOGLE = "discovery.google"
AUDIT_EXPORT = "audit.export"  # the audit log as JSON lines for a SIEM
COMPLIANCE_PROFILE = "compliance_profile"  # the compliance fields; off per organization until an Admin turns them on

COMMUNITY = frozenset(
    {
        CATALOG,
        RELATIONSHIPS,
        REVIEWS,
        RENEWALS,
        DIGEST,
        DISCOVERY_ENTRA,
        DISCOVERY_AZURE,
        DISCOVERY_POWERPLATFORM,
        DISCOVERY_HALO,
        IMPORTS,
        API_TOKENS,
        AUDIT_LOG,
        SSO_OIDC,
        AI_AGENTS,
        CUSTOM_FIELDS,
        ATTACHMENTS,
        DATA_QUALITY,
        CONTRACTS,
        WEBHOOKS,
        ORG_EXPORT,
        VENDOR_RISK,
        EOL,
        CATALOG_CODE,
        DATA_FLOWS,
        TICKET_LINKS,
        PREVIEW,
        ASSISTANT,
        DISCOVERY_OKTA,
        DISCOVERY_GOOGLE,
        AUDIT_EXPORT,
        COMPLIANCE_PROFILE,
    }
)

# ---- enterprise: SAML and SCIM, from the optional `ee` package ---------------------------------------------------
# Configurable audit retention isn't built, so no edition offers it (docs/CAPABILITIES.md):
# AuditEventQuerySet.purge() is the deletion it will use.
SSO_SAML = "sso.saml"
SCIM = "scim"

ENTERPRISE_ONLY = frozenset({SSO_SAML, SCIM})
# The capabilities the optional `ee` package provides (softwarden/ee.py). Without the package no organization has
# them, whatever its edition or an override says, and the platform console doesn't offer them.
EE_ONLY = frozenset({SSO_SAML, SCIM})

# ---- cloud: community plus billing -------------------------------------------------------------------------------
BILLING = "billing"
# An Admin deletes their own organization (export first, a grace period, then every row removed). Cloud only:
# on a self-hosted install the operator owns the database and removes an organization themselves.
ORG_SELF_DELETE = "org.self_delete"
CLOUD_ONLY = frozenset({BILLING, ORG_SELF_DELETE})

# ---- cloud and enterprise, not community -----------------------------------------------------------------------------
# A nightly export of the organization's data kept for SOFTWARDEN_TENANT_BACKUP_RETENTION nights and downloadable by
# its Admins (catalog/org_lifecycle.queue_backups). A self-hosted community install has the installation's
# own nightly backup (core/backup.py).
TENANT_BACKUPS = "org.tenant_backups"
CLOUD_AND_ENTERPRISE = frozenset({TENANT_BACKUPS})

# The edition an organization created by public signup gets (catalog/signup.py).
SIGNUP_EDITION = Edition.CLOUD

EDITION_CAPABILITIES: dict[str, frozenset[str]] = {
    Edition.COMMUNITY: COMMUNITY,
    Edition.CLOUD: COMMUNITY | CLOUD_ONLY | CLOUD_AND_ENTERPRISE,
    Edition.ENTERPRISE: COMMUNITY | ENTERPRISE_ONLY | CLOUD_AND_ENTERPRISE,
}

ALL = frozenset().union(*EDITION_CAPABILITIES.values())
GATED = ALL - COMMUNITY  # what a community organization is refused

# Capabilities that exist only as preview features: hidden with `preview` while SOFTWARDEN_PREVIEW_FEATURES is off. To
# release one, take it out of this set (and, for the ticket systems, out of tickets/services.SYSTEM_CAPABILITY).
PREVIEW_ONLY = frozenset({DISCOVERY_OKTA, DISCOVERY_GOOGLE})
HIDDEN_WITHOUT_PREVIEW = PREVIEW_ONLY | {PREVIEW}

# The capability each discovery source needs; a source not listed here can't be added (discovery/engine.py).
SOURCE_CAPABILITY = {
    "entra": DISCOVERY_ENTRA,
    "entra_licenses": DISCOVERY_ENTRA,  # the tenant's licences are part of Entra discovery
    "azure": DISCOVERY_AZURE,
    "powerplatform": DISCOVERY_POWERPLATFORM,
    "halo": DISCOVERY_HALO,
    "entra_shadow_it": DISCOVERY_ENTRA,  # sign-in logs and consent grants of the same Entra tenant
    "entra_coverage": DISCOVERY_ENTRA,  # SSO, SCIM and conditional access of the same Entra tenant
    "okta": DISCOVERY_OKTA,  # Okta applications (discovery/okta.py)
    "google_workspace": DISCOVERY_GOOGLE,  # third-party apps granted Google Workspace access
    "entra_licence_usage": DISCOVERY_ENTRA,  # licensed users and their last sign-in, for licence waste
}

# Sources whose own capability is released but which are offered only as a preview: licence
# usage until the licence waste report has a screen. Without `preview`, creating a connector of one is refused (403
# naming `preview`) and GET discovery/sources leaves it out; a connector made earlier keeps its capability, so it keeps
# running. To release one, take it out of this set.
PREVIEW_SOURCES = frozenset({"entra_licence_usage"})


def source_offered(org, source) -> bool:
    """Whether new connectors of `source` are offered to `org`: a preview source only while it has `preview`."""
    return source not in PREVIEW_SOURCES or has_capability(org, PREVIEW)


def catalogue() -> list[dict]:
    """Every capability name with the editions that include it (in Edition order) and whether community lacks it, by
    name: the platform console's override dialog (catalog/platform_views.PlatformCapabilitiesView)."""
    return [
        {
            "name": name,
            "editions": [edition for edition in Edition.values if name in EDITION_CAPABILITIES[edition]],
            "gated": name in GATED,
        }
        for name in sorted(ALL)
        if ee_installed() or name not in EE_ONLY
    ]


def edition_capabilities(org) -> frozenset[str]:
    """The capability names `org`'s edition has, before overrides. An unknown edition gets community's (fail closed)."""
    return EDITION_CAPABILITIES.get(org.edition, COMMUNITY)


def live_overrides(org_id, now=None) -> tuple[frozenset[str], frozenset[str]]:
    """(granted, revoked) capability names of the organization's overrides that haven't expired. A name no longer in
    ALL (a capability removed from the code) is ignored."""
    now = now or timezone.now()
    rows = CapabilityOverride.objects.filter(organization_id=org_id).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )
    granted: set[str] = set()
    revoked: set[str] = set()
    for name, effect in rows.values_list("capability", "effect"):
        if name in ALL:
            (granted if effect == OverrideEffect.GRANT else revoked).add(name)
    return frozenset(granted), frozenset(revoked)


def preview_features_on() -> bool:
    """The installation turned its preview features on (SOFTWARDEN_PREVIEW_FEATURES). Read here only."""
    return bool(getattr(settings, "SOFTWARDEN_PREVIEW_FEATURES", False))


def ee_installed() -> bool:
    """The optional `ee` package is installed (softwarden/ee.py)."""
    return ee.INSTALLED


def _without_hidden_previews(names: frozenset[str]) -> frozenset[str]:
    """`names` without the preview features when the installation has them off, or the organization lacks `preview`
    (a platform revoke hides them for that organization alone), and without what the `ee` package provides when it
    isn't installed."""
    if not ee_installed():
        names = names - EE_ONLY
    if PREVIEW in names and preview_features_on():
        return names
    return names - HIDDEN_WITHOUT_PREVIEW


def capabilities(org) -> frozenset[str]:
    """The capability names enabled for `org`: its edition's set, plus live grants, minus live revokes, minus the
    preview features unless the installation turned them on. The overrides are read once per unit of work; changing
    one forgets them (forget_organization)."""
    base = edition_capabilities(org)
    if org.pk is None:
        return _without_hidden_previews(base)
    cache = organization_cache(org.pk)
    overrides = cache.get("capability_overrides")
    if overrides is None:
        overrides = cache["capability_overrides"] = live_overrides(org.pk)
    granted, revoked = overrides
    return _without_hidden_previews((base | granted) - revoked)


def check_capability_name(name) -> str:
    """`name` if it is a known capability, else a field error listing none (the list is in docs/CAPABILITIES.md)."""
    if not isinstance(name, str) or name not in ALL or (name in EE_ONLY and not ee_installed()):
        raise FieldErrors({"capability": ["Choose a capability name from docs/CAPABILITIES.md."]})
    return name


def in_edition(queryset, edition):
    """Organizations in `edition` (the platform console's filter). Here because only this module reads editions."""
    if edition not in Edition.values:
        raise FieldErrors({"edition": [f"Choose one of {', '.join(Edition.values)}."]})
    return queryset.filter(edition=edition)


def has_capability(org, name) -> bool:
    if name not in ALL:
        raise ValueError(f"Unknown capability {name!r}; add it to core/capabilities.py.")
    return name in capabilities(org)


def require_capability(org, name):
    """403 problem `/problems/capability-required` naming the capability when `org` lacks it. Call it after the role
    check, so a Viewer refused for role still gets the role message."""
    if not has_capability(org, name):
        raise CapabilityRequired(name, preview=hidden_preview(name))


def hidden_preview(name) -> bool:
    """`name` is a preview feature this installation keeps off (SOFTWARDEN_PREVIEW_FEATURES), whatever the edition:
    the words a refusal, a failed run or doctor use then."""
    return name in HIDDEN_WITHOUT_PREVIEW and not preview_features_on()


def create_signup_organization(**fields) -> Organization:
    """A new organization from public signup, in SIGNUP_EDITION. Here because only this module sets editions."""
    return Organization.objects.create(edition=SIGNUP_EDITION, **fields)


@transaction.atomic
def set_edition(org, edition, actor=None, via="cli"):
    """Change an organization's edition. Called by the CLI (`manage.py set_edition`), the platform console
    (catalog/platform_admin.change_edition, platform staff only, which also writes the platform audit event) and the
    e2e-only fixture (api/e2e_views.make_enterprise_org, routed only for Playwright's _e2e database); no tenant route
    does. Audited in that organization with the before and after values. Returns (organization, changed)."""
    from catalog.services import audit  # catalog depends on core; import here to keep core importable on its own

    if edition not in Edition.values:
        raise FieldErrors({"edition": [f"Choose one of {', '.join(Edition.values)}."]})
    org = Organization.objects.select_for_update().get(pk=org.pk)
    before = org.edition
    if before == edition:
        return org, False
    org.edition = edition
    org.save(update_fields=["edition"])
    forget_organization(org.pk)  # facts cached for this unit (core/request_context.py) are stale now
    audit(org, actor, "organization.edition_changed", org, {"edition": [before, edition], "via": via})
    return org, True
