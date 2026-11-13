"""Relationship types: `/relationship-types`, a plain array by name.

Roles: every role reads them (the SPA labels links and fills the type picker from them); Admins add, change and
delete them. A core type can't be deleted and its slug and `is_dependency` are fixed; a type in use can't be deleted
(409 with the count). Core feature: no capability. Every write goes through catalog/relationship_types.py."""

from typing import Any

from drf_spectacular.utils import extend_schema, extend_schema_view

from core.drf import ADMIN, VIEWER, TenantViewSet, policy

from . import relationship_types
from .models import RelationshipType
from .serializers import RelationshipTypeInputSerializer, RelationshipTypeSerializer


@extend_schema_view(
    list=extend_schema(summary="List relationship types", description="By name. Any role."),
    create=extend_schema(
        summary="Add a relationship type (Admin)",
        description="An added type follows the depends_on endpoint rules. The slug is derived from the name when "
        "left out and never changes.",
    ),
    retrieve=extend_schema(summary="Get a relationship type"),
    partial_update=extend_schema(
        summary="Change a relationship type (Admin)",
        description="Name, inverse name and description of any type; is_dependency of an added type only. The slug "
        "never changes. Turning is_dependency on or off flags the open assessments it could change as stale.",
    ),
    destroy=extend_schema(
        summary="Delete a relationship type (Admin)",
        description="Refused (409) for a core type, and for a type in use: `blocking_references.relationships` "
        "says how many relationships use it.",
    ),
)
class RelationshipTypeViewSet(TenantViewSet):
    """The organization's relationship types. `usage`: the relationships of each type."""

    role_policy = policy(VIEWER, ADMIN)
    pagination_class = None
    serializer_class = RelationshipTypeSerializer
    input_serializer_class = RelationshipTypeInputSerializer
    ordering_fields = ("name", "slug", "created_at")
    default_ordering = ("name",)

    def base_queryset(self):
        return RelationshipType.objects.all()

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        return {"usage": relationship_types.usage(self.organization)}

    def create_object(self, data):
        return relationship_types.create_type(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        return relationship_types.update_type(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        relationship_types.delete_type(self.organization, self.request.user, obj)
