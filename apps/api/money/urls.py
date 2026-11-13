"""Routes for renewals, contracts and licence pools. Included from api/urls.py under /api/v1/. Viewset routes map
only the actions each viewset supports, so a method a route lacks is a 405."""

from typing import Any

from django.urls import path

from catalog.attachment_viewsets import ContractAttachmentViewSet
from catalog.audit_viewsets import ContractAuditViewSet, LicensePoolAuditViewSet

from .contract_viewsets import ContractLineViewSet, ContractViewSet, LicensePoolViewSet
from .report_views import (
    CalendarExportView,
    CalendarFeedIcsView,
    CalendarFeedRotateView,
    CalendarFeedView,
    CalendarReportView,
    DashboardView,
    LicenseExportView,
    SpendExportView,
    SpendReportView,
)
from .viewsets import RenewalViewSet

LIST: dict[str, Any] = {"get": "list", "post": "create"}
DETAIL: dict[str, Any] = {"get": "retrieve", "patch": "partial_update", "delete": "destroy"}

urlpatterns = [
    path("renewals", RenewalViewSet.as_view({"get": "list", "post": "create"})),
    path("renewals/preview", RenewalViewSet.as_view({"get": "preview"})),
    path(
        "renewals/<uuid:pk>",
        RenewalViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
    ),
    path("renewals/<uuid:pk>/archive", RenewalViewSet.as_view({"post": "archive"})),
    path("renewals/<uuid:pk>/restore", RenewalViewSet.as_view({"post": "restore"})),
    path("renewals/<uuid:pk>/cycles", RenewalViewSet.as_view({"post": "cycles"})),
    path("renewals/<uuid:pk>/cycles/<uuid:cycle_pk>", RenewalViewSet.as_view({"patch": "cycle"})),
    path("renewals/<uuid:pk>/cycles/<uuid:cycle_pk>/decision", RenewalViewSet.as_view({"post": "decision"})),
    path("renewals/<uuid:pk>/cycles/<uuid:cycle_pk>/completion", RenewalViewSet.as_view({"post": "completion"})),
    # Contracts; the fixed paths before the ID routes
    path("contracts", ContractViewSet.as_view(LIST)),
    path("contracts/preview", ContractViewSet.as_view({"get": "preview"})),
    path("contracts/export.csv", ContractViewSet.as_view({"get": "export"})),
    path("contracts/export.xlsx", ContractViewSet.as_view({"get": "export_xlsx"})),
    path("contracts/<uuid:pk>", ContractViewSet.as_view(DETAIL)),
    path("contracts/<uuid:pk>/archive", ContractViewSet.as_view({"post": "archive"})),
    path("contracts/<uuid:pk>/restore", ContractViewSet.as_view({"post": "restore"})),
    path("contracts/<uuid:pk>/renew", ContractViewSet.as_view({"get": "renewal_terms", "post": "renew"})),
    path("contracts/<uuid:pk>/lines", ContractLineViewSet.as_view(LIST)),
    path("contracts/<uuid:pk>/lines/<uuid:item_pk>", ContractLineViewSet.as_view(DETAIL)),
    path("contracts/<uuid:pk>/attachments", ContractAttachmentViewSet.as_view(LIST)),
    path("contracts/<uuid:pk>/audit", ContractAuditViewSet.as_view({"get": "list"})),
    path("contracts/<uuid:pk>/audit/<uuid:event_pk>", ContractAuditViewSet.as_view({"get": "retrieve"})),
    # Licence pools
    path("license-pools", LicensePoolViewSet.as_view(LIST)),
    path("license-pools/export.csv", LicensePoolViewSet.as_view({"get": "export"})),
    path("license-pools/totals", LicensePoolViewSet.as_view({"get": "totals"})),
    path("license-pools/seat-review", LicensePoolViewSet.as_view({"get": "seat_review"})),
    path(
        "license-pools/<uuid:pk>/seat-review/<uuid:observation_pk>/apply",
        LicensePoolViewSet.as_view({"post": "apply_seat_count"}),
    ),
    path(
        "license-pools/<uuid:pk>/seat-review/<uuid:observation_pk>/dismiss",
        LicensePoolViewSet.as_view({"post": "dismiss_seat_count"}),
    ),
    path(
        "license-pools/unbound/<uuid:observation_pk>/create",
        LicensePoolViewSet.as_view({"post": "create_from_unbound"}),
    ),
    path("license-pools/<uuid:pk>/bind", LicensePoolViewSet.as_view({"post": "bind"})),
    path("license-pools/<uuid:pk>", LicensePoolViewSet.as_view(DETAIL)),
    path("license-pools/<uuid:pk>/audit", LicensePoolAuditViewSet.as_view({"get": "list"})),
    path("license-pools/<uuid:pk>/audit/<uuid:event_pk>", LicensePoolAuditViewSet.as_view({"get": "retrieve"})),
    # Dashboard and reports
    path("dashboard", DashboardView.as_view()),
    path("reports/spend", SpendReportView.as_view()),
    path("reports/spend/export.csv", SpendExportView.as_view()),
    path("reports/renewals-calendar", CalendarReportView.as_view()),
    path("reports/renewals-calendar/export.csv", CalendarExportView.as_view()),
    path("reports/license-utilization/export.csv", LicenseExportView.as_view()),
    path("calendar-feed", CalendarFeedView.as_view()),
    path("calendar-feed/rotate", CalendarFeedRotateView.as_view()),
    # Public: authenticated only by the feed's secret in the query string (never the path, which is logged)
    path("calendar/renewals.ics", CalendarFeedIcsView.as_view()),
]
