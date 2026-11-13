"""Routes for connectors, their runs, discovery candidates and a record's discovery sources. Included from api/urls.py
under /api/v1/. Each route maps only the actions its viewset supports, so a method a route lacks is a 405."""

from django.urls import path

from catalog.models import Kind
from core.capabilities import AI_AGENTS

from .assistant_viewsets import AssistantBatchesView, AssistantBatchView, AssistantSettingsView, AssistantUsageView
from .coverage_viewsets import SsoCoverageViewSet
from .licence_waste_views import (
    ContractWasteView,
    LicencePoolWasteView,
    LicenceWasteExportView,
    LicenceWasteReportView,
    LicenceWasteUsersExportView,
    LicenceWasteUsersView,
)
from .shadow_it_viewsets import ShadowItViewSet
from .viewsets import (
    ConnectorRunViewSet,
    ConnectorViewSet,
    DiscoveredCredentialViewSet,
    DiscoverySourcesView,
    ObservationViewSet,
    RecordSourceViewSet,
    RunHistoryViewSet,
)

urlpatterns = [
    path("connectors", ConnectorViewSet.as_view({"get": "list", "post": "create"})),
    path(
        "connectors/<uuid:pk>",
        ConnectorViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
    ),
    path("connectors/<uuid:pk>/run", ConnectorViewSet.as_view({"post": "run"})),
    path("connectors/<uuid:pk>/access-check", ConnectorViewSet.as_view({"post": "access_check"})),
    path("connectors/<uuid:pk>/runs", ConnectorRunViewSet.as_view({"get": "list"})),
    path("connectors/<uuid:pk>/runs/<uuid:run_pk>", ConnectorRunViewSet.as_view({"get": "retrieve"})),
    path("discovery/runs", RunHistoryViewSet.as_view({"get": "list"})),
    path("discovery/runs/<uuid:pk>", RunHistoryViewSet.as_view({"get": "retrieve"})),
    path("discovery/sources", DiscoverySourcesView.as_view()),
    path("discovery/candidates", ObservationViewSet.as_view({"get": "list"})),
    path("discovery/credentials", DiscoveredCredentialViewSet.as_view({"get": "list"})),
    # Shadow IT overview (discovery/shadow_it_viewsets.py).
    path("discovery/shadow-it", ShadowItViewSet.as_view({"get": "list"})),
    path("discovery/shadow-it/export.csv", ShadowItViewSet.as_view({"get": "export"})),
    # SSO and MFA coverage report (discovery/coverage_viewsets.py).
    path("reports/sso-coverage", SsoCoverageViewSet.as_view({"get": "list"})),
    path("reports/sso-coverage/export.csv", SsoCoverageViewSet.as_view({"get": "export"})),
    # Licence waste (discovery/licence_waste_views.py); the drill-down is Admin-only.
    path("reports/licence-waste", LicenceWasteReportView.as_view()),
    path("reports/licence-waste/export.csv", LicenceWasteExportView.as_view()),
    path("reports/licence-waste/<uuid:sku_id>", LicenceWasteUsersView.as_view()),
    path("reports/licence-waste/<uuid:sku_id>/export.csv", LicenceWasteUsersExportView.as_view()),
    path("license-pools/<uuid:pk>/licence-waste", LicencePoolWasteView.as_view()),
    path("contracts/<uuid:pk>/licence-waste", ContractWasteView.as_view()),
    path("discovery/observations/<uuid:pk>", ObservationViewSet.as_view({"get": "retrieve"})),
    path("discovery/observations/<uuid:pk>/link", ObservationViewSet.as_view({"post": "link"})),
    path("discovery/observations/<uuid:pk>/create", ObservationViewSet.as_view({"post": "create_record"})),
    path("discovery/observations/<uuid:pk>/dismiss", ObservationViewSet.as_view({"post": "dismiss"})),
    path("discovery/observations/<uuid:pk>/reopen", ObservationViewSet.as_view({"post": "reopen"})),
    path("discovery/observations/<uuid:pk>/suggestions", ObservationViewSet.as_view({"get": "suggestions"})),
    path("discovery/observations/<uuid:pk>/assistant/accept", ObservationViewSet.as_view({"post": "assistant_accept"})),
    path("discovery/observations/<uuid:pk>/assistant/reject", ObservationViewSet.as_view({"post": "assistant_reject"})),
    path("discovery/observations/<uuid:pk>/drift", ObservationViewSet.as_view({"get": "drift"})),
    path("discovery/observations/<uuid:pk>/drift/apply", ObservationViewSet.as_view({"post": "drift_apply"})),
    path("discovery/observations/<uuid:pk>/drift/ignore", ObservationViewSet.as_view({"post": "drift_ignore"})),
    path(
        "discovery/observations/<uuid:pk>/missing/acknowledge",
        ObservationViewSet.as_view({"post": "missing_acknowledge"}),
    ),
    path("discovery/observations/<uuid:pk>/missing/archive", ObservationViewSet.as_view({"post": "missing_archive"})),
    # The assistant (capability assistant; discovery/assistant_viewsets.py).
    path("assistant/settings", AssistantSettingsView.as_view()),
    path("assistant/usage", AssistantUsageView.as_view()),
    path("assistant/batches", AssistantBatchesView.as_view()),
    path("assistant/batches/<uuid:pk>", AssistantBatchView.as_view()),
    path(
        "applications/<uuid:pk>/discovery-sources",
        RecordSourceViewSet.as_view({"get": "list"}, kind=Kind.APPLICATION),
    ),
    path(
        "components/<uuid:pk>/discovery-sources",
        RecordSourceViewSet.as_view({"get": "list"}, kind=Kind.COMPONENT),
    ),
    # Like every other AI agent route, refused without ai_agents (the view checks the record's kind too).
    path(
        "ai-agents/<uuid:pk>/discovery-sources",
        RecordSourceViewSet.as_view({"get": "list"}, kind=Kind.AI_AGENT, required_capability=AI_AGENTS),
    ),
]
