"""Routes for curation. Included from catalog/urls.py under /api/v1/."""

from django.urls import path

from .curation_viewsets import (
    BulkApplyView,
    BulkDeleteApplyView,
    BulkDeletePreviewView,
    BulkPreviewView,
    SavedFilterViewSet,
    TagViewSet,
)
from .viewsets import AIAgentViewSet, ApplicationViewSet, ComponentViewSet

urlpatterns = [
    path("applications/export.csv", ApplicationViewSet.as_view({"get": "export"})),
    path("components/export.csv", ComponentViewSet.as_view({"get": "export"})),
    path("applications/export.xlsx", ApplicationViewSet.as_view({"get": "export_xlsx"})),
    path("components/export.xlsx", ComponentViewSet.as_view({"get": "export_xlsx"})),
    # AI agents are exported, not imported yet: no template route
    path("ai-agents/export.csv", AIAgentViewSet.as_view({"get": "export"})),
    path("ai-agents/export.xlsx", AIAgentViewSet.as_view({"get": "export_xlsx"})),
    path("applications/template.csv", ApplicationViewSet.as_view({"get": "template"})),
    path("components/template.csv", ComponentViewSet.as_view({"get": "template"})),
    path("tags", TagViewSet.as_view({"get": "list"})),
    path("tags/<uuid:pk>", TagViewSet.as_view({"get": "retrieve", "patch": "partial_update"})),
    path("saved-filters", SavedFilterViewSet.as_view({"get": "list", "post": "create"})),
    path(
        "saved-filters/<str:pk>",
        SavedFilterViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
    ),
    path("bulk-edit/preview", BulkPreviewView.as_view()),
    path("bulk-edit/apply", BulkApplyView.as_view()),
    path("bulk-delete/preview", BulkDeletePreviewView.as_view()),
    path("bulk-delete/apply", BulkDeleteApplyView.as_view()),
]
