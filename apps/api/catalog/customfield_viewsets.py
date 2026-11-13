"""Custom field definitions: `/custom-fields`, a plain array by weight then name.

Roles: every role reads the definitions (the SPA renders forms, detail, columns and filters from them); Admins add,
change and delete them. Every route needs the `custom_fields` capability (community, so on in every edition today).
Values are written through each record's own POST and PATCH (`custom_field_data`), never here. Every write goes
through catalog/customfields.py."""

from typing import Any

from drf_spectacular.utils import extend_schema, extend_schema_view

from core.capabilities import CUSTOM_FIELDS
from core.drf import ADMIN, VIEWER, TenantViewSet, policy

from . import customfields
from .filters import CustomFieldDefinitionFilterSet
from .models import CustomField
from .serializers import CustomFieldInputSerializer, CustomFieldSerializer


@extend_schema_view(
    list=extend_schema(summary="List custom fields", description="By weight, then name. Any role."),
    create=extend_schema(summary="Add a custom field (Admin)"),
    retrieve=extend_schema(summary="Get a custom field"),
    partial_update=extend_schema(
        summary="Change a custom field (Admin)",
        description="The name never changes. The type can't change while any record holds a value; a choice in use "
        "can't be removed; a kind can't be removed from applies_to while records of that kind hold a value. Each "
        "refusal is a field error saying how many records.",
    ),
    destroy=extend_schema(
        summary="Delete a custom field (Admin)",
        description="Removes the field's values from every record and vendor (`usage` says how many) and the field; "
        "audited once as custom_field.deleted with the number cleared.",
    ),
)
class CustomFieldViewSet(TenantViewSet):
    """Admin-defined fields on applications, components, integrations, AI agents and vendors. `usage`: the records
    and vendors holding a value."""

    role_policy = policy(VIEWER, ADMIN)
    required_capability = CUSTOM_FIELDS
    pagination_class = None
    serializer_class = CustomFieldSerializer
    input_serializer_class = CustomFieldInputSerializer
    filterset_class = CustomFieldDefinitionFilterSet
    ordering_fields = ("weight", "name", "label", "created_at")
    default_ordering = ("weight", "name")

    def base_queryset(self):
        return CustomField.objects.all()

    def page_context(self, rows: list[Any]) -> dict[str, Any]:
        return {"usage": customfields.usage(self.organization)}

    def create_object(self, data):
        return customfields.create_field(self.organization, self.request.user, data)

    def update_object(self, obj, data):
        return customfields.update_field(self.organization, self.request.user, obj, data)

    def destroy_object(self, obj):
        customfields.delete_field(self.organization, self.request.user, obj)
