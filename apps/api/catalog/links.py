"""Reading link confirmations in bulk: the newest confirmation per link, and the derived link state."""

from collections import namedtuple

from django.db.models import QuerySet

from .models import LinkConfirmation
from .services import org_today
from .state import integration_signature, link_state, relationship_signature

Latest = namedtuple("Latest", "confirmed_on confirmed_signature")  # all link_state reads, without a model each


def latest_confirmations(org, relationship_ids=(), integration_ids=()):
    """{("relationship" | "integration", id): Latest, the newest confirmation}; DISTINCT ON keeps one row per link instead
    of the whole history. Both lookups are organization-scoped. Each ID argument is a collection of IDs or a queryset
    of them (a subquery)."""
    found: dict = {}
    for kind, column, ids in (
        ("relationship", "relationship_id", relationship_ids),
        ("integration", "integration_id", integration_ids),
    ):
        if not isinstance(ids, QuerySet) and not ids:
            continue
        rows = (
            LinkConfirmation.objects.filter(organization=org, **{f"{column}__in": ids})
            .order_by(column, "-confirmed_on", "-created_at")
            .distinct(column)
            .values_list(column, "confirmed_on", "confirmed_signature")
        )
        found.update({(kind, link_id): Latest(on, signature) for link_id, on, signature in rows})
    return found


def state_fields(org, signature, latest, today=None):
    """The three members every relationship and integration response carries."""
    today = today or org_today(org)
    state, why = link_state(signature, latest, today, org.review_interval_days)
    return {
        "link_state": state,
        "link_why": why,
        "last_confirmed_on": latest.confirmed_on.isoformat() if latest else None,
    }


def relationship_state_fields(org, rel, latest, today=None):
    """rel needs its source loaded (select_related) for the owner."""
    return state_fields(org, relationship_signature(rel, rel.source.technical_owner_id), latest, today)


def integration_state_fields(org, resource, latest, today=None):
    """resource is the integration's Resource, with its `integration` row loaded."""
    return state_fields(org, integration_signature(resource.integration, resource.technical_owner_id), latest, today)
