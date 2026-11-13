"""Ticket links as plain data (TicketLinkSerializer's shape): the nested routes and the assessment and renewal detail
bodies. Empty when the organization lacks the `ticket_links` capability."""

from __future__ import annotations

from typing import Any

from core.capabilities import TICKET_LINKS, has_capability
from core.drf import UserBriefSerializer, iso, represent

from .models import TicketLink, TicketStatus, TicketSystem

SYSTEM_LABELS = dict(TicketSystem.choices)
STATUS_LABELS = dict(TicketStatus.choices)


def link_json(link: TicketLink) -> dict[str, Any]:
    return {
        "id": str(link.id),
        "system": link.system,
        "system_label": SYSTEM_LABELS.get(link.system, link.system),
        "external_key": link.external_key,
        "url": link.url,
        "title": link.title,
        "status": link.status,
        "status_label": STATUS_LABELS.get(link.status, link.status),
        "status_source": link.status_source,
        "external_status": link.external_status,
        "status_checked_at": iso(link.status_checked_at),
        "status_error": link.status_error,
        "cycle": str(link.cycle_id) if link.cycle_id else None,
        "created_in_system": link.created_in_system,
        "created_by": represent(UserBriefSerializer, link.created_by),
        "created_at": iso(link.created_at),
        "updated_at": iso(link.updated_at),
    }


def _rows(org, **target) -> list[dict[str, Any]]:
    if not has_capability(org, TICKET_LINKS):
        return []
    rows = TicketLink.objects.filter(organization=org, **target).select_related("created_by").order_by("created_at")
    return [link_json(link) for link in rows]


def for_assessment(org, assessment) -> list[dict[str, Any]]:
    return _rows(org, assessment=assessment)


def for_renewal(org, obligation) -> list[dict[str, Any]]:
    return _rows(org, obligation=obligation)
