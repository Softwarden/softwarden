"""The platform admin console's routes (catalog/platform_views.py), included from api/urls.py under
/api/v1/platform/. Every route here is `platform` (tests/test_url_coverage.py checks both ways)."""

from django.urls import path

from . import platform_views as views

urlpatterns = [
    path("session", views.PlatformSessionView.as_view()),
    # The second factor, reachable before it is verified (catalog/staff_second_factor.py).
    path("second-factor", views.PlatformSecondFactorView.as_view()),
    path("second-factor/enrollment", views.PlatformSecondFactorEnrollmentView.as_view()),
    path("second-factor/verify", views.PlatformSecondFactorVerifyView.as_view()),
    path("organizations", views.PlatformOrganizationListView.as_view()),
    path("organizations/<uuid:pk>", views.PlatformOrganizationDetailView.as_view()),
    path("organizations/<uuid:pk>/edition", views.PlatformEditionView.as_view()),
    path("organizations/<uuid:pk>/cancel-deletion", views.PlatformCancelDeletionView.as_view()),
    path("organizations/<uuid:pk>/suspend", views.PlatformSuspendView.as_view()),
    path("organizations/<uuid:pk>/unsuspend", views.PlatformUnsuspendView.as_view()),
    path("organizations/<uuid:pk>/capability-overrides", views.PlatformOverridesView.as_view()),
    path(
        "organizations/<uuid:pk>/capability-overrides/<uuid:override_pk>/remove",
        views.PlatformOverrideRemoveView.as_view(),
    ),
    path("organizations/<uuid:pk>/limits", views.PlatformLimitsView.as_view()),
    path("organizations/<uuid:pk>/limits/<str:name>/remove", views.PlatformLimitRemoveView.as_view()),
    path("organizations/<uuid:pk>/exports", views.PlatformExportView.as_view()),
    path("sign-in-domains", views.PlatformDomainClaimsView.as_view()),
    path("sign-in-domains/move", views.PlatformDomainMoveView.as_view()),
    path("sign-in-domains/release", views.PlatformDomainReleaseView.as_view()),
    path("audit-events", views.PlatformAuditView.as_view()),
    path("capabilities", views.PlatformCapabilitiesView.as_view()),
    path("system", views.PlatformSystemView.as_view()),
    path("system/backup/acknowledge-key", views.PlatformBackupKeyAcknowledgeView.as_view()),
    path("impersonation", views.PlatformImpersonationView.as_view()),
]
