"""The compliance profile.

Six nullable columns on Resource: is_gxp, validation_status, validation_doc_url, last_periodic_review,
next_periodic_review and risk_tier. They are part of the API only while the organization's profile is on: the
`compliance_profile` capability (core/capabilities.py) and the organization setting
`compliance_profile_enabled`, which an Admin turns on in Organization settings. While it is off:

- the fields are left out of every record response (omitted, not null), export and audit snapshot;
- a write that names one is refused with a field error, and so is a list filter;
- the CSV template has no columns for them.

An organization that loses the capability (an operator moved it back to community) has the profile off whatever the
setting says; the stored values are kept and come back if the profile is turned on again."""

from datetime import date
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

from core import dates
from core.capabilities import COMPLIANCE_PROFILE, has_capability
from core.request_context import forget_organization, organization_cache

from .models import RiskTier, ValidationStatus

SETTING = "compliance_profile_enabled"  # Organization.settings key (audited: snapshots.ORG_SETTINGS_AUDITED)
FIELDS = (
    "is_gxp",
    "validation_status",
    "validation_doc_url",
    "last_periodic_review",
    "next_periodic_review",
    "risk_tier",
)
OFF = "The compliance profile is off for this organization. An Admin can turn it on in Organization settings."
URL_MAX = 2000


def enabled(org) -> bool:
    """Whether the organization's records carry the compliance fields now."""
    return has_capability(org, COMPLIANCE_PROFILE) and (org.settings or {}).get(SETTING) is True


def enabled_for(org_id) -> bool:
    """`enabled` by organization ID, read once per unit of work (audit snapshots ask for every write). A change to the
    setting or the edition calls forget() so the rest of the unit sees it."""
    from core.models import Organization

    cache = organization_cache(org_id)
    if "compliance" not in cache:
        org = Organization.objects.only("edition", "settings").filter(pk=org_id).first()
        cache["compliance"] = org is not None and enabled(org)
    return cache["compliance"]


def forget(org_id) -> None:
    forget_organization(org_id)


def _date(value: Any, field: str, errors: dict[str, list[str]]):
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return dates.check_window(value, field, errors)
    try:
        return dates.parse_stored(value)
    except ValueError as exc:
        errors[field] = [str(exc)]
    return None


def yes_no(value: Any, field: str, errors: dict[str, list[str]]):
    """True, False or None (not recorded) from JSON, or from CSV text (true/false/yes/no, blank for none). Used for
    every yes/no field (catalog/services.py too)."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "false", "no"):
        return value.strip().lower() in ("true", "yes")
    errors[field] = ["Use true or false, or leave it empty."]
    return None


def clean(org, data: dict[str, Any], errors: dict[str, list[str]], current=None) -> dict[str, Any]:
    """The compliance values present in data, validated: {field: value} (None clears). Refused field by field while
    the profile is off. `current` is the record being changed, if any: a PATCH of one review date is checked against
    the other as stored."""
    present = [f for f in FIELDS if f in data]
    if not present:
        return {}
    if not enabled(org):
        for f in present:
            errors[f] = [OFF]
        return {}
    values: dict[str, Any] = {}
    if "is_gxp" in data:
        values["is_gxp"] = yes_no(data["is_gxp"], "is_gxp", errors)
    for field, choices in (("validation_status", ValidationStatus), ("risk_tier", RiskTier)):
        if field in data:
            value = data[field] or None
            if value is not None and value not in choices.values:
                errors[field] = [f"Use one of: {', '.join(choices.values)}, or leave it empty."]
            values[field] = value
    if "validation_doc_url" in data:
        url = data["validation_doc_url"].strip() if isinstance(data["validation_doc_url"], str) else ""
        if data["validation_doc_url"] not in (None, "") and not url:
            errors["validation_doc_url"] = ["Enter a full web address starting with http:// or https://."]
        elif len(url) > URL_MAX:
            errors["validation_doc_url"] = [f"Use {URL_MAX:,} characters or fewer."]
        elif url:
            try:
                URLValidator(schemes=["http", "https"])(url)
            except ValidationError:
                errors["validation_doc_url"] = ["Enter a full web address starting with http:// or https://."]
        values["validation_doc_url"] = url or None
    for field in ("last_periodic_review", "next_periodic_review"):
        if field in data:
            values[field] = _date(data[field], field, errors)
    last = values.get("last_periodic_review", getattr(current, "last_periodic_review", None))
    nxt = values.get("next_periodic_review", getattr(current, "next_periodic_review", None))
    if last and nxt and nxt <= last and "next_periodic_review" not in errors:
        errors["next_periodic_review"] = ["The next periodic review must be after the last one."]
    return values


def hide(event) -> None:
    """Drop the compliance fields from an audit event read back while the profile is off: its payload and both
    snapshots, so the summary and the diff, computed from them, leave them out too. The stored event is unchanged;
    the instance is never saved (events are append-only)."""
    event.changes = {k: v for k, v in (event.changes or {}).items() if k not in FIELDS}
    for attr in ("prechange", "postchange"):
        snap = getattr(event, attr)
        if isinstance(snap, dict):
            setattr(event, attr, {k: v for k, v in snap.items() if k not in FIELDS})


def as_text(value) -> str:
    """A compliance value in a CSV cell: dates ISO, booleans true/false, empty for none."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
