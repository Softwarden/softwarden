"""Test-only routes for capabilities that have no real endpoint yet (tests/test_capabilities.py). Loaded only by tests
that set ROOT_URLCONF to this module; nothing in the shipped URLconf includes it."""

from django.urls import include, path
from rest_framework.decorators import api_view
from rest_framework.response import Response

from core.access import org_role
from core.capabilities import require_capability
from core.drf import VIEWER, min_role


@api_view(["GET"])
@min_role(VIEWER)
def gated(request, name):
    """Stands in for the feature behind `name`: role first (any member), then the capability."""
    org, _ = org_role(request)
    require_capability(org, name)
    return Response({"capability": name})


urlpatterns = [
    path("api/v1/_test/capability/<str:name>", gated),
    path("", include("softwarden.urls")),
]
