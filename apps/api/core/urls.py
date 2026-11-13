"""Routes for sign-in options, OIDC sign-in and the single sign-on settings, the System status, first-run setup, global search and connections.
Included from api/urls.py under /api/v1/."""

from django.urls import path

from . import connection_viewsets, oidc_simulator, oidc_views, search_views, setup_views, sso_views, system_views

urlpatterns = [
    path("auth/options", oidc_views.options),
    path("auth/oidc/start", oidc_views.start),
    path("auth/oidc/callback", oidc_views.callback),
    path("auth/oidc/simulator", oidc_simulator.authorize),
    path("sso/settings", sso_views.SsoSettingsView.as_view()),
    path("sso/settings/test", sso_views.SsoTestView.as_view()),
    path("sso/settings/activate", sso_views.SsoActivateView.as_view()),
    path("sso/settings/disable", sso_views.SsoDisableView.as_view()),
    path("sso/settings/domains/verify", sso_views.SsoDomainVerifyView.as_view()),
    path("system", system_views.SystemView.as_view()),
    path("system/backup/acknowledge-key", system_views.BackupKeyAcknowledgeView.as_view()),
    path("setup", setup_views.setup),
    path("search", search_views.SearchView.as_view()),
]

for _kind, _view in (
    ("microsoft", connection_viewsets.MicrosoftConnectionViewSet),
    ("halo", connection_viewsets.HaloConnectionViewSet),
    ("okta", connection_viewsets.OktaConnectionViewSet),
    ("google_workspace", connection_viewsets.GoogleWorkspaceConnectionViewSet),
):
    urlpatterns += [
        path(f"connections/{_kind}", _view.as_view({"get": "list", "post": "create"})),
        path(
            f"connections/{_kind}/<uuid:pk>",
            _view.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
        ),
        path(f"connections/{_kind}/<uuid:pk>/test", _view.as_view({"post": "test"})),
    ]
