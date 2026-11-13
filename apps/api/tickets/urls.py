"""Ticket link routes (tickets/views.py), included from api/urls.py under /api/v1/."""

from typing import Any

from django.urls import path

from . import views

READ_EDIT: dict[str, Any] = {"get": "retrieve", "patch": "partial_update", "delete": "destroy"}

urlpatterns = [
    path("ticket-links/parse", views.ParseUrlView.as_view()),
    path("ticket-links/systems", views.SystemsView.as_view()),
    path("ticket-connections", views.TicketConnectionViewSet.as_view({"get": "list", "post": "create"})),
    path("ticket-connections/<uuid:pk>", views.TicketConnectionViewSet.as_view(READ_EDIT)),
    path("ticket-connections/<uuid:pk>/test", views.TicketConnectionViewSet.as_view({"post": "test"})),
]

for _prefix, _kind in (("assessments", "assessment"), ("renewals", "renewal")):
    urlpatterns += [
        path(f"{_prefix}/<uuid:pk>/ticket-links", views.TicketLinksView.as_view(kind=_kind)),
        path(f"{_prefix}/<uuid:pk>/ticket-links/create-ticket", views.CreateTicketView.as_view(kind=_kind)),
        path(f"{_prefix}/<uuid:pk>/ticket-links/<uuid:link_pk>", views.TicketLinkView.as_view(kind=_kind)),
        path(
            f"{_prefix}/<uuid:pk>/ticket-links/<uuid:link_pk>/refresh",
            views.TicketLinkRefreshView.as_view(kind=_kind),
        ),
    ]
