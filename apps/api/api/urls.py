"""The /api/v1/ root (included from softwarden/urls.py). Each app owns its routes; this file includes them and adds
the OpenAPI schema, the composite read routes, the API token routes and the Playwright-only routes."""

from django.urls import include, path

from core.drf import ACCOUNT, RolePermission, policy
from softwarden import ee

from . import composite_views as composite
from .schema import SchemaView
from .viewsets import ApiTokenViewSet

urlpatterns = [
    # Compact read endpoints for scripts and the MCP server (api/composite_views.py)
    path("composite/search", composite.CompositeSearchView.as_view()),
    path("composite/deadlines", composite.CompositeDeadlinesView.as_view()),
    path("composite/renewals", composite.CompositeRenewalsView.as_view()),
    path("composite/spend", composite.CompositeSpendView.as_view()),
    path("composite/dependencies", composite.CompositeDependenciesView.as_view()),
    path("composite/discovery-summary", composite.CompositeDiscoveryView.as_view()),
    path("composite/data-gaps", composite.CompositeDataGapsView.as_view()),
    path("composite/expiring-credentials", composite.CompositeCredentialsView.as_view()),
    # Any signed-in account may read the API description; it holds no organization data.
    path("schema", SchemaView.as_view(permission_classes=[RolePermission.bound(policy(ACCOUNT))]), name="schema"),
    path("", include("catalog.urls")),
    path("", include("core.urls")),
    path("", include("money.urls")),
    path("", include("notify.urls")),
    path("", include("discovery.urls")),
    path("", include("imports.urls")),
    path("", include("eol.urls")),  # end-of-life tracking (capability eol)
    path("", include("catalog_code.urls")),  # catalog-as-code (capability catalog_code)
    path("", include("offboarding.urls")),  # offboarding (person footprint, reassign) and access review
    path("", include("tickets.urls")),  # ticket links (capability ticket_links)
    # SAML sign-in and the SCIM admin routes, from the optional `ee` package when it is installed (softwarden/ee.py).
    *([path("", include("ee.saml.urls")), path("", include("ee.scim.admin_urls"))] if ee.INSTALLED else []),
    # Platform admin console: platform staff sessions only (catalog/platform_views.py)
    path("platform/", include("catalog.platform_urls")),
    path("api-tokens", ApiTokenViewSet.as_view({"get": "list", "post": "create"})),
    path("api-tokens/<uuid:pk>", ApiTokenViewSet.as_view({"get": "retrieve"})),
    path("api-tokens/<uuid:pk>/revoke", ApiTokenViewSet.as_view({"post": "revoke"})),
]


def e2e_enabled() -> bool:
    """Playwright only: the test-only routes exist when SOFTWARDEN_E2E=1 and the database is the throwaway _e2e one,
    never in development, on a demo server or in production."""
    from core import simulation

    return simulation.e2e()  # the one test (also needs this file's e2e_views, which the image leaves out)


def e2e_urlpatterns() -> list:
    """The test-only routes (api/e2e_views.py), registered below only when e2e_enabled()."""
    from . import e2e_views

    return [
        path("e2e/run-digest", e2e_views.run_digest),
        path("e2e/run-connectors", e2e_views.run_connectors),
        path("e2e/multi-org-user", e2e_views.multi_org_user),
        path("e2e/sso-seed", e2e_views.sso_seed),
        path("e2e/system-states", e2e_views.system_states),
        path("e2e/enterprise-org", e2e_views.enterprise_org),
        path("e2e/custom-fields-org", e2e_views.custom_fields_org),
        path("e2e/spec-org", e2e_views.spec_org),
        path("e2e/license-observation", e2e_views.license_observation),
        path("e2e/assistant-org", e2e_views.assistant_org),
        # The in-memory outbox, ending this session's impersonation now, and running queued exports.
        path("e2e/outbox", e2e_views.outbox),
        path("e2e/expire-impersonation", e2e_views.expire_impersonation),
        path("e2e/run-export-jobs", e2e_views.run_export_jobs),
        # The worker sends password reset links; run its queued jobs now.
        path("e2e/run-password-reset-jobs", e2e_views.run_password_reset_jobs),
        # Launch scope: a Halo ticket connection kept from when preview features were on.
        path("e2e/kept-ticket-connection", e2e_views.kept_ticket_connection),
        # Turn one capability off (or on) for the caller's organization, as a platform revoke would.
        path("e2e/capability-override", e2e_views.capability_override),
    ]


if e2e_enabled():
    from . import e2e_views

    e2e_views.install_hooks()
    urlpatterns += e2e_urlpatterns()
