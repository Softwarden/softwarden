"""Routes for the catalog: sign-in, accounts and reference data, records, links, assessments and audit activity.
Included from api/urls.py under /api/v1/. Viewset routes map only the actions each viewset supports, so a method a
route lacks is a 405."""

from typing import Any

from django.urls import include, path

from core.capabilities import AI_AGENTS

from . import access_viewsets, password_reset_views, signup_views, views
from .access_viewsets import InvitationViewSet, PendingAccessViewSet
from .account_viewsets import OrganizationLimitsView, OrganizationView, PasswordView, UserViewSet
from .ai_viewsets import AIModelViewSet, AIProviderViewSet
from .attachment_viewsets import (
    AIAgentAttachmentViewSet,
    ApplicationAttachmentViewSet,
    AttachmentViewSet,
    ComponentAttachmentViewSet,
    IntegrationAttachmentViewSet,
    VendorAttachmentViewSet,
)
from .audit_viewsets import (
    AIAgentAuditViewSet,
    ApplicationAuditViewSet,
    AuditCsvExportViewSet,
    AuditEventViewSet,
    AuditJsonlExportViewSet,
    ComponentAuditViewSet,
    IntegrationAuditViewSet,
    RenewalAuditViewSet,
)
from .customfield_viewsets import CustomFieldViewSet
from .data_flow_views import DataFlowExportView, DataFlowGraphView, DataFlowReportView
from .directory_viewsets import (
    DepartmentViewSet,
    PersonViewSet,
    SoftwareCategoryViewSet,
    StewardsView,
    TeamViewSet,
    VendorViewSet,
)
from .lifecycle_views import OrganizationDeletionView, OrgExportViewSet
from .models import Kind
from .quality_viewsets import FailureViewSet, QualityExceptionViewSet, RulesView, SummaryView
from .relationship_type_viewsets import RelationshipTypeViewSet
from .vendor_risk_viewsets import (
    VendorRiskDocumentsOfVendorViewSet,
    VendorRiskDocumentViewSet,
    VendorRiskOverviewExportView,
    VendorRiskOverviewView,
    VendorRiskProfileViewSet,
)
from .viewsets import (
    AIAgentReviewViewSet,
    AIAgentViewSet,
    ApplicationReviewViewSet,
    ApplicationViewSet,
    AssessmentViewSet,
    ComponentReviewViewSet,
    ComponentViewSet,
    DependenciesView,
    InstanceViewSet,
    IntegrationConfirmationViewSet,
    IntegrationViewSet,
    RecordDependenciesView,
    RecordMapView,
    RelationshipConfirmationViewSet,
    RelationshipViewSet,
)

LIST: dict[str, Any] = {"get": "list", "post": "create"}
DETAIL: dict[str, Any] = {"get": "retrieve", "patch": "partial_update", "delete": "destroy"}
READ_EDIT: dict[str, Any] = {"get": "retrieve", "patch": "partial_update"}
POST = {"archive": {"post": "archive"}, "restore": {"post": "restore"}}


def graph_routes(prefix: str, kind: str, capability: str | None = None) -> list:
    """A record's dependencies (the explorer with it as the target) and its Map."""
    kwargs = {"kind": kind, "required_capability": capability}
    return [
        path(f"{prefix}/<uuid:pk>/dependencies", RecordDependenciesView.as_view(**kwargs)),
        path(f"{prefix}/<uuid:pk>/map", RecordMapView.as_view(**kwargs)),
    ]


def records(prefix: str, viewset) -> list:
    """List, detail, archive and restore for one Resource kind."""
    return [
        path(prefix, viewset.as_view(LIST)),
        path(f"{prefix}/<uuid:pk>", viewset.as_view(DETAIL)),
        path(f"{prefix}/<uuid:pk>/archive", viewset.as_view(POST["archive"])),
        path(f"{prefix}/<uuid:pk>/restore", viewset.as_view(POST["restore"])),
    ]


urlpatterns = [
    path("health", views.health),
    path("auth/csrf", views.csrf),
    path("auth/login", views.login_view),
    path("auth/logout", views.logout_view),
    path("auth/me", views.me),
    path("auth/me/organization", views.me_organization),
    path("auth/password", PasswordView.as_view()),
    # Self-service password reset by an emailed link (catalog/password_reset.py); public.
    path("auth/password-reset", password_reset_views.password_reset_request),
    path("auth/password-reset/confirm", password_reset_views.password_reset_confirm),
    path("users", UserViewSet.as_view(LIST)),
    path("users/<int:pk>", UserViewSet.as_view(DETAIL)),
    path("users/<int:pk>/reset-password", UserViewSet.as_view({"post": "reset_password"})),
    path("users/<int:pk>/identity", UserViewSet.as_view({"delete": "identity"})),
    path("users/<int:pk>/identities/<int:identity_id>", UserViewSet.as_view({"delete": "unlink_identity"})),
    path("invitations", InvitationViewSet.as_view(LIST)),
    path("invitations/<uuid:pk>", InvitationViewSet.as_view({"get": "retrieve"})),
    path("invitations/<uuid:pk>/revoke", InvitationViewSet.as_view({"post": "revoke"})),
    path("invitations/<uuid:pk>/resend", InvitationViewSet.as_view({"post": "resend"})),
    path("auth/invitations/preview", access_viewsets.invitation_preview),
    path("auth/invitations/accept", access_viewsets.invitation_accept),
    path("pending-access", PendingAccessViewSet.as_view({"get": "list"})),
    path("pending-access/<uuid:pk>", PendingAccessViewSet.as_view({"get": "retrieve", "delete": "destroy"})),
    path("pending-access/<uuid:pk>/approve", PendingAccessViewSet.as_view({"post": "approve"})),
    path("organization", OrganizationView.as_view()),
    path("organization/limits", OrganizationLimitsView.as_view()),  # limits and usage (core/limits.py)
    # Export everything, and self-service deletion (catalog/lifecycle_views.py); public signup.
    path("organization/exports", OrgExportViewSet.as_view({"get": "list", "post": "create"})),
    path("organization/exports/<uuid:pk>", OrgExportViewSet.as_view({"get": "retrieve"})),
    path("organization/exports/<uuid:pk>/download", OrgExportViewSet.as_view({"get": "download"})),
    path("organization/deletion", OrganizationDeletionView.as_view()),
    path("auth/signup", signup_views.signup_view),
    path("auth/signup/resend", signup_views.signup_resend),
    path("auth/signup/verify", signup_views.signup_verify),
    path("audit-events", AuditEventViewSet.as_view({"get": "list"})),
    path("audit-events/export.csv", AuditCsvExportViewSet.as_view({"get": "list"})),
    path("audit-events/export.jsonl", AuditJsonlExportViewSet.as_view({"get": "list"})),
    path("audit-events/<uuid:pk>", AuditEventViewSet.as_view({"get": "retrieve"})),
    # People & Teams
    path("people", PersonViewSet.as_view(LIST)),
    path("people/<uuid:pk>", PersonViewSet.as_view(READ_EDIT)),
    path("people/<uuid:pk>/anonymize", PersonViewSet.as_view({"post": "anonymize"})),
    path("teams", TeamViewSet.as_view(LIST)),
    path("teams/<uuid:pk>", TeamViewSet.as_view(DETAIL)),
    path("departments", DepartmentViewSet.as_view(LIST)),
    path("departments/<uuid:pk>", DepartmentViewSet.as_view(DETAIL)),
    path("vendors", VendorViewSet.as_view(LIST)),
    path("vendors/<uuid:pk>", VendorViewSet.as_view(DETAIL)),
    path("vendors/<uuid:pk>/attachments", VendorAttachmentViewSet.as_view(LIST)),
    # The vendor risk file (catalog/vendor_risk_viewsets.py)
    path("vendors/risk-overview", VendorRiskOverviewView.as_view()),
    path("vendors/risk-overview/export.csv", VendorRiskOverviewExportView.as_view()),
    path("vendors/<uuid:pk>/risk", VendorRiskProfileViewSet.as_view(READ_EDIT)),
    path("vendors/<uuid:pk>/risk-documents", VendorRiskDocumentsOfVendorViewSet.as_view(LIST)),
    path("vendor-risk-documents", VendorRiskDocumentViewSet.as_view({"get": "list"})),
    path("vendor-risk-documents/export.csv", VendorRiskDocumentViewSet.as_view({"get": "export"})),
    path("vendor-risk-documents/<uuid:pk>", VendorRiskDocumentViewSet.as_view(DETAIL)),
    path("categories", SoftwareCategoryViewSet.as_view(LIST)),
    path("categories/<uuid:pk>", SoftwareCategoryViewSet.as_view(DETAIL)),
    path("stewards", StewardsView.as_view()),
    # AI providers and models; in-use before the ID route
    path("ai-providers", AIProviderViewSet.as_view(LIST)),
    path("ai-providers/<uuid:pk>", AIProviderViewSet.as_view(DETAIL)),
    path("ai-models", AIModelViewSet.as_view(LIST)),
    path("ai-models/in-use", AIModelViewSet.as_view({"get": "in_use"})),
    path("ai-models/<uuid:pk>", AIModelViewSet.as_view(DETAIL)),
    # Custom fields: definitions; values go through each record's own routes
    path("custom-fields", CustomFieldViewSet.as_view(LIST)),
    path("custom-fields/<uuid:pk>", CustomFieldViewSet.as_view(DETAIL)),
    # Relationship types: core types seeded per organization; Admins add more
    path("relationship-types", RelationshipTypeViewSet.as_view(LIST)),
    path("relationship-types/<uuid:pk>", RelationshipTypeViewSet.as_view(DETAIL)),
    # Curation routes first: export.csv and template.csv would otherwise read as record IDs (they aren't UUIDs, so
    # the order is belt and braces).
    path("", include("catalog.urls_curation")),
    # Applications and components
    *records("applications", ApplicationViewSet),
    path("applications/<uuid:pk>/reviews", ApplicationReviewViewSet.as_view(LIST)),
    path("applications/<uuid:pk>/reviews/<uuid:review_pk>", ApplicationReviewViewSet.as_view({"get": "retrieve"})),
    path("applications/<uuid:pk>/audit", ApplicationAuditViewSet.as_view({"get": "list"})),
    path("applications/<uuid:pk>/audit/<uuid:event_pk>", ApplicationAuditViewSet.as_view({"get": "retrieve"})),
    path("applications/<uuid:pk>/instances", InstanceViewSet.as_view(LIST)),
    path("applications/<uuid:pk>/attachments", ApplicationAttachmentViewSet.as_view(LIST)),
    path("applications/<uuid:pk>/instances/<uuid:instance_pk>", InstanceViewSet.as_view(DETAIL)),
    *graph_routes("applications", Kind.APPLICATION),
    *records("components", ComponentViewSet),
    path("components/<uuid:pk>/reviews", ComponentReviewViewSet.as_view(LIST)),
    path("components/<uuid:pk>/reviews/<uuid:review_pk>", ComponentReviewViewSet.as_view({"get": "retrieve"})),
    path("components/<uuid:pk>/audit", ComponentAuditViewSet.as_view({"get": "list"})),
    path("components/<uuid:pk>/audit/<uuid:event_pk>", ComponentAuditViewSet.as_view({"get": "retrieve"})),
    path("components/<uuid:pk>/attachments", ComponentAttachmentViewSet.as_view(LIST)),
    *graph_routes("components", Kind.COMPONENT),
    *records("ai-agents", AIAgentViewSet),
    path("ai-agents/<uuid:pk>/reviews", AIAgentReviewViewSet.as_view(LIST)),
    path("ai-agents/<uuid:pk>/reviews/<uuid:review_pk>", AIAgentReviewViewSet.as_view({"get": "retrieve"})),
    path("ai-agents/<uuid:pk>/audit", AIAgentAuditViewSet.as_view({"get": "list"})),
    path("ai-agents/<uuid:pk>/audit/<uuid:event_pk>", AIAgentAuditViewSet.as_view({"get": "retrieve"})),
    path("ai-agents/<uuid:pk>/attachments", AIAgentAttachmentViewSet.as_view(LIST)),
    *graph_routes("ai-agents", Kind.AI_AGENT, AI_AGENTS),
    # Integrations and relationships
    *records("integrations", IntegrationViewSet),
    path("integrations/<uuid:pk>/audit", IntegrationAuditViewSet.as_view({"get": "list"})),
    path("integrations/<uuid:pk>/audit/<uuid:event_pk>", IntegrationAuditViewSet.as_view({"get": "retrieve"})),
    path("integrations/<uuid:pk>/attachments", IntegrationAttachmentViewSet.as_view(LIST)),
    path(
        "integrations/<uuid:pk>/confirmations",
        IntegrationViewSet.as_view({"get": "confirmations", "post": "confirmations"}),
    ),
    path(
        "integrations/<uuid:pk>/confirmations/<uuid:confirmation_pk>",
        IntegrationConfirmationViewSet.as_view({"get": "retrieve"}),
    ),
    path("relationships", RelationshipViewSet.as_view(LIST)),
    path("relationships/<uuid:pk>", RelationshipViewSet.as_view(DETAIL)),
    path(
        "relationships/<uuid:pk>/confirmations",
        RelationshipViewSet.as_view({"get": "confirmations", "post": "confirmations"}),
    ),
    path(
        "relationships/<uuid:pk>/confirmations/<uuid:confirmation_pk>",
        RelationshipConfirmationViewSet.as_view({"get": "retrieve"}),
    ),
    path("dependencies", DependenciesView.as_view()),
    # The data-flow map (catalog/data_flow_views.py): where classified data goes
    path("reports/data-flows", DataFlowReportView.as_view()),
    path("reports/data-flows/graph", DataFlowGraphView.as_view()),
    path("reports/data-flows/export.csv", DataFlowExportView.as_view()),
    # Attachments: listed and added under their record or vendor (above); one by ID here
    path("attachments/<uuid:pk>", AttachmentViewSet.as_view(DETAIL)),
    path("attachments/<uuid:pk>/download", AttachmentViewSet.as_view({"get": "download"})),
    # Data quality: rules, the summary, failing records and exceptions
    path("data-quality/rules", RulesView.as_view()),
    path("data-quality/summary", SummaryView.as_view()),
    path("data-quality/failures", FailureViewSet.as_view({"get": "list"})),
    path("data-quality/exceptions", QualityExceptionViewSet.as_view(LIST)),
    path("data-quality/exceptions/<uuid:pk>", QualityExceptionViewSet.as_view(DETAIL)),
    # Change assessments
    path("assessments", AssessmentViewSet.as_view(LIST)),
    path("assessments/<uuid:pk>", AssessmentViewSet.as_view(READ_EDIT)),
    path("assessments/<uuid:pk>/status", AssessmentViewSet.as_view({"post": "status"})),
    path("assessments/<uuid:pk>/refresh", AssessmentViewSet.as_view({"post": "refresh"})),
    path("assessments/<uuid:pk>/snapshots", AssessmentViewSet.as_view({"get": "snapshots"})),
    path("assessments/<uuid:pk>/snapshots/<uuid:snapshot_pk>", AssessmentViewSet.as_view({"get": "snapshot"})),
    path("renewals/<uuid:pk>/audit", RenewalAuditViewSet.as_view({"get": "list"})),
    path("renewals/<uuid:pk>/audit/<uuid:event_pk>", RenewalAuditViewSet.as_view({"get": "retrieve"})),
]
