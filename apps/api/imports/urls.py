"""Routes for CSV imports and import sources. Included from api/urls.py under /api/v1/. Each route maps only
the actions its viewset supports, so a method a route lacks is a 405."""

from django.urls import path

from .viewsets import ImportRunViewSet, ImportSourceViewSet

urlpatterns = [
    path("import-sources", ImportSourceViewSet.as_view({"get": "list", "post": "create"})),
    path(
        "import-sources/<uuid:pk>",
        ImportSourceViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
    ),
    path("imports", ImportRunViewSet.as_view({"get": "list", "post": "create"})),
    path("imports/template.csv", ImportRunViewSet.as_view({"get": "template"})),
    path("imports/<uuid:pk>", ImportRunViewSet.as_view({"get": "retrieve", "patch": "partial_update"})),
    path("imports/<uuid:pk>/rows", ImportRunViewSet.as_view({"get": "rows"})),
    path("imports/<uuid:pk>/commit", ImportRunViewSet.as_view({"post": "commit"})),
    path("imports/<uuid:pk>/cancel", ImportRunViewSet.as_view({"post": "cancel"})),
    path("imports/<uuid:pk>/rerun", ImportRunViewSet.as_view({"post": "rerun"})),
    path("imports/<uuid:pk>/errors.csv", ImportRunViewSet.as_view({"get": "error_report"})),
]
