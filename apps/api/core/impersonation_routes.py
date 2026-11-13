"""Which routes an impersonation by platform staff may use (enforced by core/impersonation.Middleware).

Closed by default. While impersonating:

- a (method, route) in RESTRICTED is refused, whatever the method (HEAD counts as GET);
- any other GET (and HEAD, OPTIONS) is allowed;
- a write (POST, PUT, PATCH, DELETE) is allowed only when ALLOWED_WRITES lists it: an undeclared write is refused.

Routes are as Django resolves them (ResolverMatch.route). Platform routes (api/v1/platform/) are the platform
permission's (core/platform_access.py) and outside both lists; routes outside api/v1/ (the SPA, SCIM, probes) aren't
session routes and are allowed. tests/test_platform.py checks that every write route under api/v1/ is in exactly one
list and that every entry is a real route answering that method; a new write route fails it until it is placed."""

from softwarden import ee

RESTRICTED: dict[str, list[tuple[str, str]]] = {
    "passwords": [
        ("POST", "api/v1/auth/password"),
        ("POST", "api/v1/users/<int:pk>/reset-password"),
        ("POST", "api/v1/auth/password-reset"),  # asking for a reset link, and using one
        ("POST", "api/v1/auth/password-reset/confirm"),
    ],
    "members": [
        ("PATCH", "api/v1/users/<int:pk>"),  # role and active flag
        ("DELETE", "api/v1/users/<int:pk>"),
        ("POST", "api/v1/people/<uuid:pk>/anonymize"),
    ],
    "identities and sign-in": [
        ("DELETE", "api/v1/users/<int:pk>/identity"),
        ("DELETE", "api/v1/users/<int:pk>/identities/<int:identity_id>"),
        ("POST", "api/v1/users"),  # an invitation: a new way into the organization
        ("POST", "api/v1/invitations"),
        ("POST", "api/v1/invitations/<uuid:pk>/resend"),
        ("POST", "api/v1/pending-access/<uuid:pk>/approve"),  # links a provider identity to an account
        ("POST", "api/v1/auth/login"),
        ("GET", "api/v1/auth/oidc/start"),
        ("GET", "api/v1/auth/oidc/callback"),
        ("POST", "api/v1/auth/invitations/accept"),
        ("POST", "api/v1/auth/signup/verify"),
        ("POST", "api/v1/auth/signup"),
        ("POST", "api/v1/auth/signup/resend"),
        ("POST", "api/v1/setup"),
    ],
    "single sign-on settings": [
        ("PATCH", "api/v1/sso/settings"),
        ("POST", "api/v1/sso/settings/test"),
        ("POST", "api/v1/sso/settings/activate"),
        ("POST", "api/v1/sso/settings/disable"),
        ("POST", "api/v1/sso/settings/domains/verify"),  # A verified domain routes sign-ins
    ],
    "tokens": [
        ("POST", "api/v1/api-tokens"),
        ("POST", "api/v1/calendar-feed"),
        ("POST", "api/v1/calendar-feed/rotate"),
        ("POST", "api/v1/api-tokens/<uuid:pk>/revoke"),
    ],
    "secrets": [
        ("POST", "api/v1/connections/microsoft"),
        ("PATCH", "api/v1/connections/microsoft/<uuid:pk>"),
        ("POST", "api/v1/connections/halo"),
        ("PATCH", "api/v1/connections/halo/<uuid:pk>"),
        ("POST", "api/v1/connections/okta"),
        ("PATCH", "api/v1/connections/okta/<uuid:pk>"),
        ("POST", "api/v1/connections/google_workspace"),
        ("PATCH", "api/v1/connections/google_workspace/<uuid:pk>"),
        ("PATCH", "api/v1/notification-settings/email"),
        ("POST", "api/v1/notification-settings/teams-destinations"),
        ("PATCH", "api/v1/notification-settings/teams-destinations/<uuid:pk>"),
        ("POST", "api/v1/notification-settings/webhook-destinations"),
        ("PATCH", "api/v1/notification-settings/webhook-destinations/<uuid:pk>"),
        ("POST", "api/v1/webhooks"),
        ("PATCH", "api/v1/webhooks/<uuid:pk>"),
        ("POST", "api/v1/webhooks/<uuid:pk>/rotate-secret"),
        ("PATCH", "api/v1/assistant/settings"),
        ("DELETE", "api/v1/connections/microsoft/<uuid:pk>"),
        ("DELETE", "api/v1/connections/halo/<uuid:pk>"),
        ("POST", "api/v1/ticket-connections"),  # a Jira API token or a ServiceNow password
        ("PATCH", "api/v1/ticket-connections/<uuid:pk>"),
        ("DELETE", "api/v1/ticket-connections/<uuid:pk>"),
        ("DELETE", "api/v1/connections/okta/<uuid:pk>"),
        ("DELETE", "api/v1/connections/google_workspace/<uuid:pk>"),
        ("DELETE", "api/v1/notification-settings/email"),
        ("DELETE", "api/v1/notification-settings/teams-destinations/<uuid:pk>"),
        ("DELETE", "api/v1/notification-settings/webhook-destinations/<uuid:pk>"),
        ("DELETE", "api/v1/webhooks/<uuid:pk>"),
        ("POST", "api/v1/system/backup/acknowledge-key"),  # the installation's backup key, not the organization's
    ],
    "data leaving the organization": [
        ("POST", "api/v1/organization/exports"),
        ("GET", "api/v1/organization/exports/<uuid:pk>/download"),
        ("GET", "api/v1/audit-events/export.csv"),
        ("GET", "api/v1/audit-events/export.jsonl"),
        # Bulk exports: support work reads single records, not whole lists.
        ("GET", "api/v1/applications/export.csv"),
        ("GET", "api/v1/applications/export.xlsx"),
        ("GET", "api/v1/components/export.csv"),
        ("GET", "api/v1/components/export.xlsx"),
        ("GET", "api/v1/ai-agents/export.csv"),
        ("GET", "api/v1/ai-agents/export.xlsx"),
        ("GET", "api/v1/contracts/export.csv"),
        ("GET", "api/v1/contracts/export.xlsx"),
        ("GET", "api/v1/license-pools/export.csv"),
        ("GET", "api/v1/reports/license-utilization/export.csv"),
        ("GET", "api/v1/reports/renewals-calendar/export.csv"),
        ("GET", "api/v1/reports/spend/export.csv"),
        ("GET", "api/v1/vendors/risk-overview/export.csv"),  # vendor risk file
        ("GET", "api/v1/vendor-risk-documents/export.csv"),
        ("GET", "api/v1/end-of-life/export.csv"),
        ("GET", "api/v1/catalog-code/export.zip"),  # the whole catalog as YAML files
        ("GET", "api/v1/discovery/shadow-it/export.csv"),  # which third-party apps people use
        ("GET", "api/v1/reports/data-flows/export.csv"),  # data-flow map
        ("GET", "api/v1/people/<uuid:pk>/footprint.csv"),  # offboarding: everything one person appears in
        ("GET", "api/v1/reports/access-review/export.csv"),  # every record's owners
        ("GET", "api/v1/reports/sso-coverage/export.csv"),  # which applications lack SSO or MFA
        # Creates a ticket in the organization's Jira, ServiceNow or Halo (an outward-facing write)
        ("POST", "api/v1/assessments/<uuid:pk>/ticket-links/create-ticket"),
        ("POST", "api/v1/renewals/<uuid:pk>/ticket-links/create-ticket"),
        ("GET", "api/v1/reports/licence-waste/export.csv"),  # licence waste: money per SKU
        ("GET", "api/v1/reports/licence-waste/<uuid:sku_id>"),  # who holds a licence without signing in (personal)
        ("GET", "api/v1/reports/licence-waste/<uuid:sku_id>/export.csv"),
    ],
    "organization deletion": [
        ("POST", "api/v1/organization/deletion"),
        ("DELETE", "api/v1/organization/deletion"),
    ],
}
# The routes of the optional `ee` package (SAML sign-in, SCIM tokens), restricted the same way when it is installed.
if ee.INSTALLED:
    RESTRICTED["identities and sign-in"] += [
        ("GET", "api/v1/auth/saml/start"),
        ("POST", "api/v1/auth/saml/<slug:slug>/acs"),
    ]
    RESTRICTED["tokens"] += [("POST", "api/v1/scim/tokens"), ("POST", "api/v1/scim/tokens/<uuid:pk>/revoke")]
RESTRICTED_ROUTES = frozenset(pair for pairs in RESTRICTED.values() for pair in pairs)

# Keys of an ALLOWED_WRITES route that are sign-in settings all the same: Organization settings carries
# auto-provisioning for unknown single sign-on identities and SCIM's default role and group-to-role mapping, which
# would outlive the impersonation. catalog/admin_services.update_organization refuses a body with any of them while
# impersonating (/problems/impersonation-restricted, nothing written); its other keys stay ordinary support work.
RESTRICTED_SETTINGS = frozenset({"sso_auto_provision", "scim_default_role", "scim_role_groups"})
RESTRICTED_SETTINGS_DETAIL = (
    "Sign-in settings (auto-provisioning and SCIM roles) aren't changed while impersonating someone: they stay with "
    "the organization's Admins. Stop impersonating first."
)

# GET routes whose path looks like data leaving (export, download, reveal) that stay allowed while impersonating, each
# with its reason. tests/test_platform.py fails on such a route in neither this list nor RESTRICTED.
ALLOWED_READS = frozenset(
    {
        ("GET", "api/v1/attachments/<uuid:pk>/download"),  # one file of one record: support work (reads aren't audited)
        ("GET", "api/v1/organization/exports"),  # the list of exports, no data; downloading one is restricted
        ("GET", "api/v1/organization/exports/<uuid:pk>"),  # one export's status, no data
    }
)

# Ordinary work an Admin, Editor or Viewer does, which staff may do for them while impersonating, audited with
# `impersonated_by`.
ALLOWED_WRITES = frozenset(
    {
        ("POST", "api/v1/admin/digests/<uuid:pk>/retry"),
        ("POST", "api/v1/ai-agents"),
        ("DELETE", "api/v1/ai-agents/<uuid:pk>"),
        ("PATCH", "api/v1/ai-agents/<uuid:pk>"),
        ("POST", "api/v1/ai-agents/<uuid:pk>/archive"),
        ("POST", "api/v1/ai-agents/<uuid:pk>/attachments"),
        ("POST", "api/v1/ai-agents/<uuid:pk>/restore"),
        ("POST", "api/v1/ai-agents/<uuid:pk>/reviews"),
        ("POST", "api/v1/ai-models"),
        ("DELETE", "api/v1/ai-models/<uuid:pk>"),
        ("PATCH", "api/v1/ai-models/<uuid:pk>"),
        ("POST", "api/v1/ai-providers"),
        ("DELETE", "api/v1/ai-providers/<uuid:pk>"),
        ("PATCH", "api/v1/ai-providers/<uuid:pk>"),
        ("POST", "api/v1/applications"),
        ("DELETE", "api/v1/applications/<uuid:pk>"),
        ("PATCH", "api/v1/applications/<uuid:pk>"),
        ("POST", "api/v1/applications/<uuid:pk>/archive"),
        ("POST", "api/v1/applications/<uuid:pk>/attachments"),
        ("DELETE", "api/v1/applications/<uuid:pk>/end-of-life"),
        ("PATCH", "api/v1/applications/<uuid:pk>/end-of-life"),
        ("POST", "api/v1/applications/<uuid:pk>/instances"),
        ("DELETE", "api/v1/applications/<uuid:pk>/instances/<uuid:instance_pk>"),
        ("PATCH", "api/v1/applications/<uuid:pk>/instances/<uuid:instance_pk>"),
        ("DELETE", "api/v1/applications/<uuid:pk>/instances/<uuid:instance_pk>/end-of-life"),
        ("PATCH", "api/v1/applications/<uuid:pk>/instances/<uuid:instance_pk>/end-of-life"),
        ("POST", "api/v1/applications/<uuid:pk>/restore"),
        ("POST", "api/v1/applications/<uuid:pk>/reviews"),
        ("POST", "api/v1/assessments"),
        ("PATCH", "api/v1/assessments/<uuid:pk>"),
        ("POST", "api/v1/assessments/<uuid:pk>/refresh"),
        ("POST", "api/v1/assessments/<uuid:pk>/status"),
        ("POST", "api/v1/assessments/<uuid:pk>/ticket-links"),  # ticket links
        ("DELETE", "api/v1/assessments/<uuid:pk>/ticket-links/<uuid:link_pk>"),
        ("PATCH", "api/v1/assessments/<uuid:pk>/ticket-links/<uuid:link_pk>"),
        ("POST", "api/v1/assessments/<uuid:pk>/ticket-links/<uuid:link_pk>/refresh"),
        ("POST", "api/v1/renewals/<uuid:pk>/ticket-links"),
        ("DELETE", "api/v1/renewals/<uuid:pk>/ticket-links/<uuid:link_pk>"),
        ("PATCH", "api/v1/renewals/<uuid:pk>/ticket-links/<uuid:link_pk>"),
        ("POST", "api/v1/renewals/<uuid:pk>/ticket-links/<uuid:link_pk>/refresh"),
        ("POST", "api/v1/ticket-connections/<uuid:pk>/test"),
        ("POST", "api/v1/assistant/batches"),
        ("DELETE", "api/v1/attachments/<uuid:pk>"),
        ("PATCH", "api/v1/attachments/<uuid:pk>"),
        ("POST", "api/v1/auth/invitations/preview"),
        ("POST", "api/v1/auth/logout"),
        ("POST", "api/v1/auth/me/organization"),
        ("PATCH", "api/v1/auth/me/preferences"),
        ("POST", "api/v1/bulk-delete/apply"),
        ("POST", "api/v1/bulk-delete/preview"),
        ("POST", "api/v1/bulk-edit/apply"),
        ("POST", "api/v1/bulk-edit/preview"),
        ("DELETE", "api/v1/calendar-feed"),
        # Catalog-as-code: planning and applying a tree is catalog editing, like an import or a bulk edit
        # (each record's event carries impersonated_by); the export is restricted above.
        ("POST", "api/v1/catalog-code/apply"),
        ("POST", "api/v1/catalog-code/plan"),
        ("POST", "api/v1/categories"),
        ("DELETE", "api/v1/categories/<uuid:pk>"),
        ("PATCH", "api/v1/categories/<uuid:pk>"),
        ("POST", "api/v1/components"),
        ("DELETE", "api/v1/components/<uuid:pk>"),
        ("PATCH", "api/v1/components/<uuid:pk>"),
        ("POST", "api/v1/components/<uuid:pk>/archive"),
        ("POST", "api/v1/components/<uuid:pk>/attachments"),
        ("DELETE", "api/v1/components/<uuid:pk>/end-of-life"),
        ("PATCH", "api/v1/components/<uuid:pk>/end-of-life"),
        ("POST", "api/v1/components/<uuid:pk>/restore"),
        ("POST", "api/v1/components/<uuid:pk>/reviews"),
        ("POST", "api/v1/connections/halo/<uuid:pk>/test"),
        ("POST", "api/v1/connections/okta/<uuid:pk>/test"),
        ("POST", "api/v1/connections/google_workspace/<uuid:pk>/test"),
        ("POST", "api/v1/connections/microsoft/<uuid:pk>/test"),
        ("POST", "api/v1/connectors"),
        ("DELETE", "api/v1/connectors/<uuid:pk>"),
        ("PATCH", "api/v1/connectors/<uuid:pk>"),
        ("POST", "api/v1/connectors/<uuid:pk>/access-check"),
        ("POST", "api/v1/connectors/<uuid:pk>/run"),
        ("POST", "api/v1/contracts"),
        ("DELETE", "api/v1/contracts/<uuid:pk>"),
        ("PATCH", "api/v1/contracts/<uuid:pk>"),
        ("POST", "api/v1/contracts/<uuid:pk>/archive"),
        ("POST", "api/v1/contracts/<uuid:pk>/attachments"),
        ("POST", "api/v1/contracts/<uuid:pk>/lines"),
        ("DELETE", "api/v1/contracts/<uuid:pk>/lines/<uuid:item_pk>"),
        ("PATCH", "api/v1/contracts/<uuid:pk>/lines/<uuid:item_pk>"),
        ("POST", "api/v1/contracts/<uuid:pk>/renew"),
        ("POST", "api/v1/contracts/<uuid:pk>/restore"),
        ("POST", "api/v1/custom-fields"),
        ("DELETE", "api/v1/custom-fields/<uuid:pk>"),
        ("PATCH", "api/v1/custom-fields/<uuid:pk>"),
        ("POST", "api/v1/data-quality/exceptions"),
        ("DELETE", "api/v1/data-quality/exceptions/<uuid:pk>"),
        ("PATCH", "api/v1/data-quality/exceptions/<uuid:pk>"),
        ("POST", "api/v1/departments"),
        ("DELETE", "api/v1/departments/<uuid:pk>"),
        ("PATCH", "api/v1/departments/<uuid:pk>"),
        ("POST", "api/v1/discovery/observations/<uuid:pk>/assistant/accept"),
        ("POST", "api/v1/discovery/observations/<uuid:pk>/assistant/reject"),
        ("POST", "api/v1/discovery/observations/<uuid:pk>/create"),
        ("POST", "api/v1/discovery/observations/<uuid:pk>/dismiss"),
        ("POST", "api/v1/discovery/observations/<uuid:pk>/drift/apply"),
        ("POST", "api/v1/discovery/observations/<uuid:pk>/drift/ignore"),
        ("POST", "api/v1/discovery/observations/<uuid:pk>/link"),
        ("POST", "api/v1/discovery/observations/<uuid:pk>/missing/acknowledge"),
        ("POST", "api/v1/discovery/observations/<uuid:pk>/missing/archive"),
        ("POST", "api/v1/discovery/observations/<uuid:pk>/reopen"),
        ("POST", "api/v1/import-sources"),
        ("DELETE", "api/v1/import-sources/<uuid:pk>"),
        ("PATCH", "api/v1/import-sources/<uuid:pk>"),
        ("POST", "api/v1/imports"),
        ("PATCH", "api/v1/imports/<uuid:pk>"),
        ("POST", "api/v1/imports/<uuid:pk>/cancel"),
        ("POST", "api/v1/imports/<uuid:pk>/commit"),
        ("POST", "api/v1/imports/<uuid:pk>/rerun"),
        ("POST", "api/v1/integrations"),
        ("DELETE", "api/v1/integrations/<uuid:pk>"),
        ("PATCH", "api/v1/integrations/<uuid:pk>"),
        ("POST", "api/v1/integrations/<uuid:pk>/archive"),
        ("POST", "api/v1/integrations/<uuid:pk>/attachments"),
        ("POST", "api/v1/integrations/<uuid:pk>/confirmations"),
        ("POST", "api/v1/integrations/<uuid:pk>/restore"),
        ("POST", "api/v1/invitations/<uuid:pk>/revoke"),
        ("POST", "api/v1/license-pools"),
        ("DELETE", "api/v1/license-pools/<uuid:pk>"),
        ("PATCH", "api/v1/license-pools/<uuid:pk>"),
        ("POST", "api/v1/license-pools/<uuid:pk>/bind"),
        ("POST", "api/v1/license-pools/<uuid:pk>/seat-review/<uuid:observation_pk>/apply"),
        ("POST", "api/v1/license-pools/<uuid:pk>/seat-review/<uuid:observation_pk>/dismiss"),
        ("POST", "api/v1/license-pools/unbound/<uuid:observation_pk>/create"),
        ("POST", "api/v1/needs-attention/snoozes"),
        ("DELETE", "api/v1/needs-attention/snoozes/<uuid:pk>"),
        ("POST", "api/v1/notification-log/<uuid:pk>/resend"),
        ("POST", "api/v1/notification-rules"),
        ("DELETE", "api/v1/notification-rules/<uuid:pk>"),
        ("PATCH", "api/v1/notification-rules/<uuid:pk>"),
        ("POST", "api/v1/notification-rules/<uuid:pk>/test-send"),
        ("POST", "api/v1/notification-settings/email/test-connection"),
        ("POST", "api/v1/notification-settings/email/test-send"),
        ("POST", "api/v1/notification-settings/teams-destinations/<uuid:pk>/test-send"),
        ("POST", "api/v1/notification-settings/webhook-destinations/<uuid:pk>/test-send"),
        ("PATCH", "api/v1/organization"),
        ("DELETE", "api/v1/pending-access/<uuid:pk>"),
        ("POST", "api/v1/people"),
        ("PATCH", "api/v1/people/<uuid:pk>"),
        ("POST", "api/v1/people/<uuid:pk>/reassign"),  # offboarding: ordinary owner edits, audited per row
        ("POST", "api/v1/relationship-types"),
        ("DELETE", "api/v1/relationship-types/<uuid:pk>"),
        ("PATCH", "api/v1/relationship-types/<uuid:pk>"),
        ("POST", "api/v1/relationships"),
        ("DELETE", "api/v1/relationships/<uuid:pk>"),
        ("PATCH", "api/v1/relationships/<uuid:pk>"),
        ("POST", "api/v1/relationships/<uuid:pk>/confirmations"),
        ("POST", "api/v1/reminders/<uuid:pk>/acknowledge"),
        ("POST", "api/v1/reminders/<uuid:pk>/reopen"),
        ("POST", "api/v1/reminders/<uuid:pk>/resolve"),
        ("POST", "api/v1/reminders/<uuid:pk>/snooze"),
        ("POST", "api/v1/renewals"),
        ("DELETE", "api/v1/renewals/<uuid:pk>"),
        ("PATCH", "api/v1/renewals/<uuid:pk>"),
        ("POST", "api/v1/renewals/<uuid:pk>/archive"),
        ("POST", "api/v1/renewals/<uuid:pk>/cycles"),
        ("PATCH", "api/v1/renewals/<uuid:pk>/cycles/<uuid:cycle_pk>"),
        ("POST", "api/v1/renewals/<uuid:pk>/cycles/<uuid:cycle_pk>/completion"),
        ("POST", "api/v1/renewals/<uuid:pk>/cycles/<uuid:cycle_pk>/decision"),
        ("POST", "api/v1/renewals/<uuid:pk>/restore"),
        ("POST", "api/v1/saved-filters"),
        ("DELETE", "api/v1/saved-filters/<str:pk>"),
        ("PATCH", "api/v1/saved-filters/<str:pk>"),
        ("POST", "api/v1/saved-view-subscriptions"),
        ("DELETE", "api/v1/saved-view-subscriptions/<uuid:pk>"),
        ("PATCH", "api/v1/saved-view-subscriptions/<uuid:pk>"),
        ("POST", "api/v1/saved-view-subscriptions/<uuid:pk>/test-send"),
        ("PATCH", "api/v1/tags/<uuid:pk>"),
        ("POST", "api/v1/teams"),
        ("DELETE", "api/v1/teams/<uuid:pk>"),
        ("PATCH", "api/v1/teams/<uuid:pk>"),
        ("POST", "api/v1/vendors"),
        ("DELETE", "api/v1/vendors/<uuid:pk>"),
        ("PATCH", "api/v1/vendors/<uuid:pk>"),
        ("POST", "api/v1/vendors/<uuid:pk>/attachments"),
        ("PATCH", "api/v1/vendors/<uuid:pk>/risk"),
        ("POST", "api/v1/vendors/<uuid:pk>/risk-documents"),
        ("DELETE", "api/v1/vendor-risk-documents/<uuid:pk>"),
        ("PATCH", "api/v1/vendor-risk-documents/<uuid:pk>"),
        ("POST", "api/v1/webhook-deliveries/<uuid:pk>/resend"),
        ("POST", "api/v1/webhooks/<uuid:pk>/ping"),
    }
)

# Playwright-only routes (api/e2e_views.py, routed only by api/urls.e2e_enabled(): SOFTWARDEN_E2E=1 with an `_e2e`
# database) that an impersonated session may write to. Outside both lists above because the real URLconf never has
# them, so tests/test_platform.py's walk can't see them. Every other e2e route is an undeclared write and refused.
E2E_ALLOWED = frozenset(
    {
        ("POST", "api/v1/e2e/expire-impersonation"),  # ends this session's impersonation now (the test's clock)
    }
)
