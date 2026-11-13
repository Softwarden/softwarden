"""The organization-scoped querysets the catalog's viewsets, audit views and exports start from, with the joins their
serializers read. Each takes the organization explicitly, so a caller can't forget to scope."""

from .models import RECORD_KINDS, Kind, Resource


def resources_for(org, kind="application"):
    """Resources of one record kind, or of every record kind when kind is None. Never integrations."""
    related = {"application": ["application__vendor"], "component": ["component"], "ai_agent": ["ai_agent"]}.get(
        kind, ["application", "component", "ai_agent"]
    )
    qs = Resource.objects.filter(organization=org, kind__in=RECORD_KINDS)
    if kind is not None:
        qs = qs.filter(kind=kind)
    return qs.select_related(
        "business_owner__department", "technical_owner__department", "support_team", "it_steward", *related
    )


INTEGRATION_RELATED = (
    "technical_owner__department",
    "support_team",
    "it_steward",
    "integration__source_resource",
    "integration__target_resource",
    "integration__source_instance",
    "integration__target_instance",
)


def integrations_for(org):
    """Integrations only; the record lists and queues never include them (resources_for filters RECORD_KINDS)."""
    return Resource.objects.filter(organization=org, kind=Kind.INTEGRATION).select_related(*INTEGRATION_RELATED)


RELATIONSHIP_RELATED = ("type", "source", "target", "source_instance", "target_instance", "created_by")
