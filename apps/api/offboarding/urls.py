"""Offboarding and access review routes, included from api/urls.py under /api/v1/."""

from django.urls import path

from .views import AccessReviewExportView, AccessReviewView, FootprintExportView, FootprintView, ReassignView

urlpatterns = [
    path("people/<uuid:pk>/footprint", FootprintView.as_view()),
    path("people/<uuid:pk>/footprint.csv", FootprintExportView.as_view()),
    path("people/<uuid:pk>/reassign", ReassignView.as_view()),
    path("reports/access-review", AccessReviewView.as_view()),
    path("reports/access-review/export.csv", AccessReviewExportView.as_view()),
]
