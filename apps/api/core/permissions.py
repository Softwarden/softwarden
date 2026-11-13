"""The project's default DRF permission (settings.REST_FRAMEWORK). Kept apart from core/drf.py because DRF imports it
while loading APIView, before core/drf.py's own DRF imports can finish."""

from rest_framework import permissions


class DenyUndeclared(permissions.BasePermission):
    """A DRF view that declares no minimum role (core/drf.py: min_role, public, TenantViewSet.role_policy) is refused
    for everyone, so a forgotten declaration fails closed. tests/test_url_coverage.py finds such routes first."""

    message = "This endpoint doesn't declare who may use it."

    def has_permission(self, request, view):
        return False
