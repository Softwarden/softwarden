"""Per-organization limits (cloud edition; docs/CAPABILITIES.md "Limits").

A limit caps how much an organization may create, beside the capabilities that say what it may use at all. Names:

    records           primary catalog records of every kind (applications, components, integrations, AI agents),
                      archived included: catalog.Resource rows
    other_records     every other row people create, together: people, teams, departments, categories, vendors,
                      tags, relationships and their types, custom fields, saved views, AI providers and models,
                      contracts and their lines, license pools, renewals, notification rules and destinations, API
                      tokens and import sources (catalog/usage.OTHER_RECORD_MODELS)
    connectors        discovery connectors
    members           active memberships (the account is active)
    attachment_bytes  the stored size of the organization's attachment files
    api_rate_read     API reads (GET, HEAD, OPTIONS) per minute, for the organization as a whole: every token and user
    api_rate_write    API writes per minute, the same way
    invitation_emails_hour, invitation_emails_day
                      invitation emails sent through the installation's own mail server in the last hour and day
                      (core.models.InstallationEmail rows). Not counted for an organization with its own mail
                      server. Until a second member is active, the edition's values are capped at
                      NEW_ORGANIZATION_BUDGET unless platform staff set an override (`effective`).

None means unlimited. An organization's limits are its edition's defaults (EDITION_LIMITS: community and enterprise
unlimited, cloud finite and generous), with per-organization overrides set by platform staff on top
(core.models.OrganizationLimitOverride, catalog/platform_admin.set_limit; an override past its expiry no longer
applies). `limits(org)` reads the overrides once per unit of work, like core/capabilities.capabilities; every change
calls forget_organization. Organization.has_limit_overrides says whether there are any, so an organization without
overrides costs no query (the API rate throttle asks on every request). Billing tiers set their values here later.

Enforced at create time only, in the service layer (`check_room`): the usage is counted live, not from the hourly
snapshot, under a transaction-scoped advisory lock per organization and limit, so two creates at once can't both take
the last place. Over the limit the create is refused with the 409 problem `/problems/limit-reached`, naming the limit,
its maximum and the current usage. Updates and deletes are never refused, and lowering a limit below the current usage
removes nothing: it only refuses new creates until usage is under it again. The two API rates are enforced by
core/throttling.OrgRateThrottle instead.

Only this module and core/capabilities.py read an organization's edition (tests/test_capabilities.py)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from django.db import connection
from django.db.models import Q
from django.utils import timezone

from .models import Edition, OrganizationLimitOverride
from .problems import LimitReached
from .request_context import organization_cache

RECORDS = "records"
OTHER_RECORDS = "other_records"
CONNECTORS = "connectors"
MEMBERS = "members"
ATTACHMENT_BYTES = "attachment_bytes"
API_RATE_READ = "api_rate_read"
API_RATE_WRITE = "api_rate_write"
INVITATION_EMAILS_HOUR = "invitation_emails_hour"
INVITATION_EMAILS_DAY = "invitation_emails_day"

# {name: (what it counts, unit)}, in display order.
LIMITS: dict[str, tuple[str, str]] = {
    RECORDS: ("Catalog records of every kind, archived included", "records"),
    OTHER_RECORDS: (
        "People, vendors, contracts, relationships, tags and the other rows people create, together",
        "rows",
    ),
    CONNECTORS: ("Discovery connectors", "connectors"),
    MEMBERS: ("Active members", "members"),
    ATTACHMENT_BYTES: ("Stored attachment files", "bytes"),
    API_RATE_READ: ("API reads per minute, across every token and user", "requests per minute"),
    API_RATE_WRITE: ("API writes per minute, across every token and user", "requests per minute"),
    INVITATION_EMAILS_HOUR: ("Invitation emails sent through the service's mail server in the last hour", "emails"),
    INVITATION_EMAILS_DAY: ("Invitation emails sent through the service's mail server in the last 24 hours", "emails"),
}
NAMES = list(LIMITS)  # the API's LimitNameEnum (softwarden/settings.py ENUM_NAME_OVERRIDES)
RATES = frozenset({API_RATE_READ, API_RATE_WRITE})
COUNTED = tuple(name for name in LIMITS if name not in RATES)
# The send budgets: a window each, counted from InstallationEmail rows.
WINDOWS = {INVITATION_EMAILS_HOUR: timedelta(hours=1), INVITATION_EMAILS_DAY: timedelta(days=1)}
# Until a second member is active, unless platform staff set an override: a brand-new signup can't send much.
NEW_ORGANIZATION_BUDGET = {INVITATION_EMAILS_HOUR: 5, INVITATION_EMAILS_DAY: 20}

GB = 1024**3
UNLIMITED: dict[str, int | None] = dict.fromkeys(LIMITS)

# The defaults per edition. Cloud's comfortably fit a new customer's first months (signup, a few colleagues, an Entra
# connector and its discovered records); platform staff raise one organization's with an override.
EDITION_LIMITS: dict[str, dict[str, int | None]] = {
    Edition.COMMUNITY: UNLIMITED,
    Edition.CLOUD: {
        RECORDS: 5000,
        OTHER_RECORDS: 50000,  # ten per catalog record at the records limit
        CONNECTORS: 10,
        MEMBERS: 50,
        ATTACHMENT_BYTES: 5 * GB,
        API_RATE_READ: 1200,
        API_RATE_WRITE: 300,
        INVITATION_EMAILS_HOUR: 20,
        INVITATION_EMAILS_DAY: 100,
    },
    Edition.ENTERPRISE: UNLIMITED,
}

MESSAGES = {
    RECORDS: "This organization has reached its limit of {maximum} catalog records.",
    OTHER_RECORDS: (
        "This organization has reached its limit of {maximum} people, vendors, contracts, relationships, tags and "
        "other rows, counted together."
    ),
    CONNECTORS: "This organization has reached its limit of {maximum} discovery connectors.",
    MEMBERS: "This organization has reached its limit of {maximum} active members.",
    ATTACHMENT_BYTES: "This file would take the organization's attachments past their limit of {maximum} bytes.",
    INVITATION_EMAILS_HOUR: "This organization has sent its {maximum} invitation emails for this hour.",
    INVITATION_EMAILS_DAY: "This organization has sent its {maximum} invitation emails for today.",
}
ADVICE = " Remove some, or ask the service operator to raise the limit."
SEND_ADVICE = (
    " Try again later, set up your organization's own mail server in Email settings, or ask the service operator to "
    "raise the limit."
)


def edition_limits(org) -> dict[str, int | None]:
    """`org`'s edition's limits, before overrides. An unknown edition gets community's, as capabilities does."""
    return dict(EDITION_LIMITS.get(org.edition, EDITION_LIMITS[Edition.COMMUNITY]))


def check_limit_name(name) -> str:
    if not isinstance(name, str) or name not in LIMITS:
        from .problems import FieldErrors

        raise FieldErrors({"limit": [f"Choose one of {', '.join(LIMITS)}."]})
    return name


def live_overrides(org_id, now=None) -> dict[str, int | None]:
    """{limit: value} of the organization's overrides that haven't expired. A name no longer in LIMITS is ignored."""
    now = now or timezone.now()
    rows = OrganizationLimitOverride.objects.filter(organization_id=org_id).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )
    return {name: value for name, value in rows.values_list("limit", "value") if name in LIMITS}


def limits(org) -> dict[str, int | None]:
    """The effective limits of `org`: its edition's, with live overrides on top. Read once per unit of work."""
    base = edition_limits(org)
    if org.pk is None or not org.has_limit_overrides:  # most organizations: no query
        return base
    cache = organization_cache(org.pk)
    overrides = cache.get("limit_overrides")
    if overrides is None:
        overrides = cache["limit_overrides"] = live_overrides(org.pk)
    return {**base, **overrides}


def emails_sent(org_id, name: str) -> int:
    """InstallationEmail rows of the organization within the budget's window."""
    from .models import InstallationEmail

    since = timezone.now() - WINDOWS[name]
    return InstallationEmail.objects.filter(organization_id=org_id, created_at__gt=since).count()


def _counters() -> dict[str, Callable[[Any], int]]:
    from catalog import usage  # catalog depends on core; imported here so core stays importable on its own

    return {
        RECORDS: usage.record_total,
        OTHER_RECORDS: usage.other_record_total,
        CONNECTORS: usage.connector_count,
        MEMBERS: usage.active_member_count,
        ATTACHMENT_BYTES: usage.attachment_bytes,
        INVITATION_EMAILS_HOUR: lambda org_id: emails_sent(org_id, INVITATION_EMAILS_HOUR),
        INVITATION_EMAILS_DAY: lambda org_id: emails_sent(org_id, INVITATION_EMAILS_DAY),
    }


def effective(org, name: str) -> int | None:
    """The limit `org` is held to now: limits(org)[name], except that a send budget (NEW_ORGANIZATION_BUDGET) is
    capped until a second member is active, unless platform staff set an override of that limit."""
    value = limits(org)[name]
    cap = NEW_ORGANIZATION_BUDGET.get(name)
    if value is None or cap is None or value <= cap:
        return value
    if org.pk is not None and org.has_limit_overrides and name in live_overrides(org.pk):
        return value
    if org.pk is not None and int(_counters()[MEMBERS](org.pk)) > 1:
        return value
    return cap


def usage_of(org_id, name: str) -> int | None:
    """The organization's live usage of a counted limit; None for the rates (nothing to count)."""
    if name in RATES:
        return None
    return int(_counters()[name](org_id))


def lock(org_id, name: str) -> None:
    """Serialise this organization's creates for one limit until the transaction ends. An advisory lock, so ordinary
    writes elsewhere in the organization (which hold a key-share lock on its row) never wait on it."""
    if not connection.in_atomic_block:
        raise RuntimeError("check_room needs a transaction: the lock is held until it ends.")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"limit:{name}:{org_id}"])


def check_room(org, name: str, adding: int = 1) -> None:
    """Refuse (409 /problems/limit-reached) when adding `adding` to the organization's live usage of `name` would pass
    its limit. Unlimited: returns at once, without counting. Call it inside the create's transaction, before writing:
    it holds the limit's lock until that transaction ends, so the create is counted before the next one checks."""
    if name in RATES or name not in LIMITS:
        raise ValueError(f"{name!r} isn't a counted limit.")
    maximum = effective(org, name)
    if maximum is None or adding <= 0:
        return
    lock(org.pk, name)
    used = usage_of(org.pk, name) or 0
    if used + adding > maximum:
        detail = MESSAGES[name].format(maximum=maximum) + (" Archived records count too." if name == RECORDS else "")
        detail += SEND_ADVICE if name in WINDOWS else ADVICE
        if adding > 1:
            unit = LIMITS[name][1]
            detail += f" This would add {adding} {unit}; {max(maximum - used, 0)} {unit} are left."
        raise LimitReached(name, maximum, used, adding, detail)


def report(org) -> list[dict[str, Any]]:
    """Each limit with its effective value, the edition's default, the current usage (None for the rates) and whether
    an override applies, for the SPA's meters and the platform console."""
    base = edition_limits(org)
    overrides = live_overrides(org.pk) if org.pk is not None and org.has_limit_overrides else {}
    counters = _counters()
    rows = []
    for name, (label, unit) in LIMITS.items():
        rows.append(
            {
                "name": name,
                "label": label,
                "unit": unit,
                "limit": effective(org, name),
                "default": base[name],
                "overridden": name in overrides,
                "usage": None if name in RATES else int(counters[name](org.pk)),
            }
        )
    return rows
