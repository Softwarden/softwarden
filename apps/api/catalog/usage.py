"""Per-organization usage counts (platform admin console).

Each counter takes an organization id and answers from the database now; `counts(org_id)` runs them all. The
`platform.usage` schedule (hourly, :05) writes every organization's counts into its core.models.OrganizationUsage
snapshot, which the platform's organization list reads, so the list stays one query however many organizations
there are. The next package (per-organization limits) checks its limits against the same counters.

Counted:

    records          {kind: rows} of applications, components, integrations and AI agents (archived included)
    records_total    their sum
    members          memberships; active_members those whose account is active
    connectors       discovery connectors
    api_tokens       API tokens not revoked and not expired
    attachments      attachment rows (files and links); attachment_bytes the stored files' sizes
    exports          organization data exports, any state
    jobs_30d         background jobs of the organization that finished in the last 30 days

Plus, beside the counts: the newest audit event (last activity) and the newest member sign-in."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Sum
from django.utils import timezone

from core.models import AuditEvent, Job, Membership, Organization, OrganizationUsage, OrgExport

JOB_WINDOW = timedelta(days=30)


def record_counts(org_id) -> dict[str, int]:
    from .models import Kind, Resource

    found = dict(
        Resource.objects.filter(organization_id=org_id).values("kind").annotate(n=Count("id")).values_list("kind", "n")
    )
    return {kind: int(found.get(kind, 0)) for kind in Kind.values}


def record_total(org_id) -> int:
    """Every record of every kind, archived included (the `records` limit, core/limits.py)."""
    from .models import Resource

    return Resource.objects.filter(organization_id=org_id).count()


def member_count(org_id) -> int:
    return Membership.objects.filter(organization_id=org_id).count()


def active_member_count(org_id) -> int:
    return Membership.objects.filter(organization_id=org_id, user__is_active=True).count()


def connector_count(org_id) -> int:
    from discovery.models import Connector

    return Connector.objects.filter(organization_id=org_id).count()


def api_token_count(org_id, now=None) -> int:
    from api.models import ApiToken

    today = timezone.localdate(now or timezone.now())
    return ApiToken.objects.filter(organization_id=org_id, revoked_at__isnull=True, expires_on__gte=today).count()


def attachment_totals(org_id) -> tuple[int, int]:
    from .models import Attachment

    row = Attachment.objects.filter(organization_id=org_id).aggregate(n=Count("id"), size=Sum("size"))
    return int(row["n"] or 0), int(row["size"] or 0)


def attachment_bytes(org_id) -> int:
    """The stored files' sizes (the `attachment_bytes` limit); links have no size."""
    return attachment_totals(org_id)[1]


# The secondary tables the `other_records` limit counts together (core/limits.py): the rows
# people and integrations create beside the catalog records, which no other limit bounds. Rows that only follow from
# another (a record's tags, a team's members, history, observations, deliveries) aren't listed: what makes them is.
OTHER_RECORD_MODELS = (
    "catalog.Person",
    "catalog.Team",
    "catalog.Department",
    "catalog.SoftwareCategory",
    "catalog.Vendor",
    "catalog.Tag",
    "catalog.Relationship",
    "catalog.RelationshipType",
    "catalog.CustomField",
    "catalog.SavedFilter",
    "catalog.AIProvider",
    "catalog.AIModel",
    "money.Contract",
    "money.ContractItem",
    "money.LicensePool",
    "money.RenewalObligation",
    "notify.NotificationRule",
    "notify.TeamsDestination",
    "notify.WebhookDestination",
    "api.ApiToken",
    "imports.ImportSource",
)


def other_record_total(org_id) -> int:
    """Rows of OTHER_RECORD_MODELS in the organization, in one query (the `other_records` limit)."""
    from django.apps import apps
    from django.db import connection

    quote = connection.ops.quote_name
    parts = []
    for label in OTHER_RECORD_MODELS:
        table = quote(apps.get_model(label)._meta.db_table)
        parts.append(f"(SELECT count(*) FROM {table} WHERE organization_id = %s)")  # noqa: S608  (names from models)
    with connection.cursor() as cursor:
        cursor.execute("SELECT " + " + ".join(parts), [org_id] * len(parts))
        return int(cursor.fetchone()[0])


def export_count(org_id) -> int:
    return OrgExport.objects.filter(organization_id=org_id).count()


def recent_job_count(org_id, now=None) -> int:
    since = (now or timezone.now()) - JOB_WINDOW
    return Job.objects.filter(organization_id=org_id, finished_at__gte=since).count()


def last_activity(org_id) -> datetime | None:
    return AuditEvent.objects.filter(organization_id=org_id).aggregate(at=Max("created_at"))["at"]


def last_sign_in(org_id) -> datetime | None:
    users = Membership.objects.filter(organization_id=org_id).values("user_id")
    return get_user_model().objects.filter(pk__in=users).aggregate(at=Max("last_login"))["at"]


# The scalar counters by name, for callers that want one (core/limits.py reads records_total, connectors,
# active_members and attachment_bytes).
COUNTERS: dict[str, Callable[..., int]] = {
    "records_total": record_total,
    "other_records": other_record_total,
    "attachment_bytes": attachment_bytes,
    "members": member_count,
    "active_members": active_member_count,
    "connectors": connector_count,
    "api_tokens": api_token_count,
    "exports": export_count,
    "jobs_30d": recent_job_count,
}


def counts(org_id, now=None) -> dict[str, Any]:
    """Every count for one organization now (see the module docstring)."""
    now = now or timezone.now()
    records = record_counts(org_id)
    attachments, attachment_bytes = attachment_totals(org_id)
    return {
        "records": records,
        "records_total": sum(records.values()),
        "members": member_count(org_id),
        "active_members": active_member_count(org_id),
        "connectors": connector_count(org_id),
        "api_tokens": api_token_count(org_id, now),
        "attachments": attachments,
        "attachment_bytes": attachment_bytes,
        "exports": export_count(org_id),
        "jobs_30d": recent_job_count(org_id, now),
    }


def refresh(org_id, now=None) -> OrganizationUsage:
    """Write one organization's snapshot. Usage is bookkeeping, not a change anyone made: not audited."""
    now = now or timezone.now()
    usage, _ = OrganizationUsage.objects.update_or_create(
        organization_id=org_id,
        defaults={
            "computed_at": now,
            "counts": counts(org_id, now),
            "last_activity_at": last_activity(org_id),
            "last_sign_in_at": last_sign_in(org_id),
        },
    )
    return usage


def refresh_all(now) -> dict[str, int]:
    """The `platform.usage` schedule: every organization's snapshot. An organization purged meanwhile is skipped."""
    refreshed = 0
    for org_id in Organization.objects.order_by("id").values_list("id", flat=True):
        try:
            with transaction.atomic():
                refresh(org_id, now)
            refreshed += 1
        except IntegrityError:  # the organization was purged after the list was read
            continue
    return {"organizations": refreshed}


def register() -> None:
    from core import jobs

    jobs.register_schedule("platform.usage", refresh_all, "5 * * * *", "Refresh every organization's usage counts")
