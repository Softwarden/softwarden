"""Catalog-as-code routes, included from api/urls.py under /api/v1/."""

from django.urls import path

from .views import CatalogCodeApplyView, CatalogCodeExportView, CatalogCodePlanView

urlpatterns = [
    path("catalog-code/export.zip", CatalogCodeExportView.as_view()),
    path("catalog-code/plan", CatalogCodePlanView.as_view()),
    path("catalog-code/apply", CatalogCodeApplyView.as_view()),
]
