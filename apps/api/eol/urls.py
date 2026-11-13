"""End-of-life routes (eol/views.py), included from api/urls.py under /api/v1/."""

from django.urls import path

from catalog.models import Kind

from . import views

urlpatterns = [
    path("applications/<uuid:pk>/end-of-life", views.RecordEndOfLifeView.as_view(kind=Kind.APPLICATION)),
    path("applications/<uuid:pk>/instances/<uuid:instance_pk>/end-of-life", views.InstanceEndOfLifeView.as_view()),
    path("components/<uuid:pk>/end-of-life", views.RecordEndOfLifeView.as_view(kind=Kind.COMPONENT)),
    path("end-of-life", views.OverviewView.as_view()),
    path("end-of-life/export.csv", views.OverviewExportView.as_view()),
    path("end-of-life/products", views.ProductListView.as_view()),
    path("end-of-life/products/<str:name>", views.ProductDetailView.as_view()),
]
