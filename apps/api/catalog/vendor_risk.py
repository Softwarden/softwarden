"""The vendor risk file: a vendor's security questionnaire, assurance report (SOC 2 or
an equivalent), DPA, subprocessor list, risk tier and dated risk documents, with reminders when documents age out.

**Status is derived, never stored.** A dated item (a risk document's `expires_on`, the profile's assurance report
expiry) is, on the organization's local `today`:

- `expired` once its expiry date has passed (the expiry day itself still counts as valid);
- `expiring` from `horizon` days before its expiry date (the organization's largest reminder threshold, the window
  Needs attention shows reminders in, so a document turns `expiring` the day its reminder appears there);
- `current` otherwise, and when it has no expiry date at all.

The profile's assurance report is one only with a type and an issue date: until both are set it has no expiry
(`assurance_expires_on` null) and reads `missing`. SOC 2 and ISO 27001 documents always have an expiry (entered, or an
issue date plus a validity); the other kinds may be undated.

**Superseded.** A newer item of the same family replaces an older one, in both directions; supersession is like
for like. The families are SOC 2 (`soc2` documents and the profile's report when it is a SOC 2 Type I or Type II),
ISO 27001 (`iso27001` documents and the profile's report when it is an ISO 27001 certificate), a profile's report of
type `other` on its own, and each other document kind on its own; `other` documents are unrelated to each other and
never supersede. A SOC 2 report and an ISO 27001 certificate held at the same time are both in force (either one
still satisfies the assurance gap). Within one vendor and family,
every dated item but the latest is superseded: the latest expiry wins, ties go to the item created last (then the
higher id). A superseded item keeps its status but raises no reminder, isn't counted, isn't a `next_expiry` and
doesn't match the overview's expiry filters (`superseded_keys`, the one rule every reader uses).

**Gaps** (a vendor's, `gaps_of`; the risk overview, the Needs attention "Vendor risk" queue and two data-quality
rules on the vendor's applications, catalog/quality.py, read the same definitions):

- `no_current_assurance`: the risk tier is high or critical and there is no valid assurance: the profile's report
  isn't expired, or a SOC 2 or ISO 27001 document isn't expired;
- `dpa_missing`: the vendor processes personal data (the profile's flag) and the DPA isn't signed (`dpa_status` yes)
  and there is no unexpired DPA document. For the vendor's own gap the flag is the only signal: data classifications
  say how sensitive data is, not whether it is personal. The data-flow map (catalog/data_flows.py) also reads each
  integration's data categories, and raises `no_dpa` for personal data sent to a vendor without a DPA (a DPA marked
  not applicable doesn't answer for personal data there either).

**Reminders.** Every unsuperseded expiry date is a reminder of source `vendor_risk` (notify/reminders.py), keyed by
the vendor (`source_id`) and `document:<id>` or `assurance` (`source_field`), so the notification rules (the built-in
"Vendor risk documents" rule at 60, 30 and 7 days), the Needs attention Reminders queue and the digest machinery
treat it like any other source. A write here is audited, and the audit hook regenerates the vendor's reminders once
the transaction commits.

Writes: `update_profile` (creates the profile on the first write), `create_document`, `update_document`,
`delete_document` and `remove_for` (a vendor being deleted), each validated, scoped to the organization and audited
in one transaction. Everything needs the `vendor_risk` capability (the views require it; the reminder source, the
queue and the data-quality rules are empty without it)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Exists, OuterRef, Q

from core import dates
from core.capabilities import VENDOR_RISK, has_capability
from core.dates import org_today
from core.problems import FieldErrors

from .models import (
    AssuranceType,
    Attachment,
    DpaStatus,
    QuestionnaireStatus,
    RiskDocumentKind,
    Vendor,
    VendorRiskDocument,
    VendorRiskProfile,
    VendorRiskTier,
)
from .services import audit, is_uuid

CURRENT, EXPIRING, EXPIRED, MISSING = "current", "expiring", "expired", "missing"
DOCUMENT_STATUSES = (CURRENT, EXPIRING, EXPIRED)
ASSURANCE_STATUSES = (MISSING, CURRENT, EXPIRING, EXPIRED)
ASSURANCE_KINDS = (RiskDocumentKind.SOC2, RiskDocumentKind.ISO27001)
HIGH_TIERS = (VendorRiskTier.HIGH, VendorRiskTier.CRITICAL)
TIER_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}  # not assessed: 4
NO_TIER = "none"  # the tier filter's word for "not assessed"

GAP_LABELS = {
    "no_current_assurance": "No current SOC 2 or equivalent",
    "dpa_missing": "No signed DPA",
}
GAP_WHY = {
    "no_current_assurance": "High or critical risk with no current SOC 2 report or equivalent.",
    "dpa_missing": "Processes personal data with no signed data processing agreement.",
}
GAPS = tuple(GAP_LABELS)

VALIDITY_MAX = 120  # months: ten years
TEXT_MAX = 5000
TITLE_MAX = 200
URL_MAX = 2000
NOT_ACCEPTED = "This field isn't accepted here."
DATE_MESSAGE = "Enter a date as YYYY-MM-DD, or null to clear it."
ATTACHMENT_MESSAGE = "Choose one of this vendor's attachments."


# ---- deriving --------------------------------------------------------------------------------------------------------


def horizon(org) -> int:
    """Days before its expiry a dated item turns `expiring`: the organization's largest reminder threshold."""
    return max(org.reminder_thresholds or [0])


def status_of(expires_on: date | None, today: date, days: int) -> str:
    if expires_on is None:
        return CURRENT
    if expires_on < today:
        return EXPIRED
    if expires_on <= today + timedelta(days=days):
        return EXPIRING
    return CURRENT


def assurance_status(profile: VendorRiskProfile | None, today: date, days: int) -> str:
    if profile is None or profile.assurance_expires_on is None:
        return MISSING
    return status_of(profile.assurance_expires_on, today, days)


def valid_on(expires_on: date | None, today: date) -> bool:
    return expires_on is None or expires_on >= today


# The profile's report joins the family of the documents of its kind (like for like); `other` is its own.
REPORT_FAMILY = {
    AssuranceType.SOC2_TYPE1: RiskDocumentKind.SOC2,
    AssuranceType.SOC2_TYPE2: RiskDocumentKind.SOC2,
    AssuranceType.ISO27001: RiskDocumentKind.ISO27001,
}
OTHER_REPORT_FAMILY = "report:other"


def family_of(kind: str) -> str | None:
    """The supersession family of a document kind: the kind itself (`soc2` and `iso27001` each with the profile's
    report of that kind), or None for `other` (never superseded)."""
    return None if kind == RiskDocumentKind.OTHER else kind


def report_family(profile) -> str:
    """The supersession family of a profile's assurance report, by its type."""
    return REPORT_FAMILY.get(profile.assurance_type, OTHER_REPORT_FAMILY)


def superseded_keys(documents: Iterable[Any], profiles: Iterable[Any] = ()) -> set[uuid.UUID]:
    """The ids of the items a later one of the same vendor and family replaces (module docstring): documents (each
    needs `id`, `vendor_id`, `kind`, `expires_on`, `created_at`) and profiles' assurance reports (a profile's `id`
    stands for its report; it needs `vendor_id`, `assurance_type`, `assurance_expires_on`, `created_at`). An undated
    item takes no part."""
    groups: dict[tuple[Any, str], list[tuple[date, Any, str, uuid.UUID]]] = {}
    for d in documents:
        family = family_of(d.kind)
        if family is not None and d.expires_on is not None:
            groups.setdefault((d.vendor_id, family), []).append((d.expires_on, d.created_at, str(d.id), d.id))
    for p in profiles:
        if p is not None and p.assurance_expires_on is not None:
            key = (p.vendor_id, report_family(p))
            groups.setdefault(key, []).append((p.assurance_expires_on, p.created_at, str(p.id), p.id))
    found: set[uuid.UUID] = set()
    for items in groups.values():
        latest = max(items, key=lambda i: i[:3])
        found |= {i[3] for i in items if i is not latest}
    return found


def assurance_superseded(profile: VendorRiskProfile | None, documents: Iterable[Any]) -> bool:
    """Whether a document of the report's kind replaces the profile's assurance report (superseded_keys)."""
    if profile is None or profile.assurance_expires_on is None:
        return False
    return profile.id in superseded_keys(documents, [profile])


def has_assurance(profile: VendorRiskProfile | None, documents: Iterable[Any], today: date) -> bool:
    """Whether the vendor has valid assurance today: the profile's report isn't expired, or a SOC 2 or ISO 27001
    document isn't. Every SOC 2 and ISO 27001 document is dated (_apply_document), and the report has an expiry only
    with a type and an issue date (_apply_profile)."""
    report = profile is not None and profile.assurance_expires_on is not None and profile.assurance_expires_on >= today
    return report or any(
        d.kind in ASSURANCE_KINDS and d.expires_on is not None and d.expires_on >= today for d in documents
    )


def has_dpa(profile: VendorRiskProfile | None, documents: Iterable[Any], today: date) -> bool:
    """Whether the vendor has a DPA today: signed on the profile (`dpa_status` yes), or an unexpired DPA document."""
    signed = profile is not None and profile.dpa_status == DpaStatus.YES
    return signed or any(d.kind == RiskDocumentKind.DPA and valid_on(d.expires_on, today) for d in documents)


def gaps_of(profile: VendorRiskProfile | None, documents: Iterable[Any], today: date) -> list[str]:
    """The vendor's gap codes (module docstring), in GAPS order."""
    if profile is None:
        return []
    documents = list(documents)
    found = []
    if profile.risk_tier in HIGH_TIERS and not has_assurance(profile, documents, today):
        found.append("no_current_assurance")
    if profile.processes_personal_data and not has_dpa(profile, documents, today):
        found.append("dpa_missing")
    return found


def gap_json(codes: Iterable[str]) -> list[dict[str, str]]:
    return [{"code": c, "label": GAP_LABELS[c]} for c in codes]


# The same gaps as conditions over another table's vendor column (catalog/quality.py's rules on applications): SQL
# that agrees with gaps_of, so a record's data-quality result and the vendor's gaps can't disagree.


def assured_q(vendor_ref: str, today: date) -> Q:
    """Rows whose vendor (the column `vendor_ref` of the outer query) has valid assurance."""
    profile = VendorRiskProfile.objects.filter(vendor_id=OuterRef(vendor_ref), assurance_expires_on__gte=today)
    docs = VendorRiskDocument.objects.filter(
        vendor_id=OuterRef(vendor_ref), kind__in=ASSURANCE_KINDS, expires_on__gte=today
    )
    return Q(Exists(profile)) | Q(Exists(docs))


def dpa_q(vendor_ref: str, today: date) -> Q:
    """Rows whose vendor has a signed DPA or an unexpired DPA document."""
    profile = VendorRiskProfile.objects.filter(vendor_id=OuterRef(vendor_ref), dpa_status=DpaStatus.YES)
    docs = VendorRiskDocument.objects.filter(vendor_id=OuterRef(vendor_ref), kind=RiskDocumentKind.DPA).filter(
        Q(expires_on__isnull=True) | Q(expires_on__gte=today)
    )
    return Q(Exists(profile)) | Q(Exists(docs))


# ---- one vendor's file, read ---------------------------------------------------------------------------------------


@dataclass
class VendorFile:
    """Everything the summaries need about one vendor."""

    vendor: Vendor
    profile: VendorRiskProfile | None = None
    documents: list[VendorRiskDocument] = field(default_factory=list)


def files(org, vendors: Iterable[Vendor]) -> dict[uuid.UUID, VendorFile]:
    """{vendor id: VendorFile} for these vendors of the organization, in two queries."""
    found = {v.id: VendorFile(v) for v in vendors}
    if not found:
        return found
    for profile in VendorRiskProfile.objects.filter(organization=org, vendor_id__in=list(found)).select_related(
        "assurance_attachment", "dpa_attachment", "subprocessor_attachment"
    ):
        found[profile.vendor_id].profile = profile
    for doc in (
        VendorRiskDocument.objects.filter(organization=org, vendor_id__in=list(found))
        .select_related("attachment")
        .order_by("expires_on", "kind", "created_at", "id")
    ):
        found[doc.vendor_id].documents.append(doc)
    return found


def counts(documents: Iterable[VendorRiskDocument], superseded: set, today: date, days: int) -> dict[str, int]:
    """Unsuperseded documents per status."""
    out = dict.fromkeys(DOCUMENT_STATUSES, 0)
    for d in documents:
        if d.id not in superseded:
            out[status_of(d.expires_on, today, days)] += 1
    return out


def superseded_for(org, vendor_ids: Iterable[Any] | None = None) -> set[uuid.UUID]:
    """The superseded items of these vendors of the organization (every vendor when None), in two queries."""
    docs = VendorRiskDocument.objects.filter(organization=org).only(
        "id", "vendor_id", "kind", "expires_on", "created_at"
    )
    profiles = VendorRiskProfile.objects.filter(organization=org, assurance_expires_on__isnull=False).only(
        "id", "vendor_id", "assurance_type", "assurance_expires_on", "created_at"
    )
    if vendor_ids is not None:
        wanted = list(vendor_ids)
        docs, profiles = docs.filter(vendor_id__in=wanted), profiles.filter(vendor_id__in=wanted)
    return superseded_keys(docs, profiles)


def superseded_in(f: VendorFile) -> set[uuid.UUID]:
    """The vendor's superseded items: document ids, and the profile's id when its report is superseded."""
    return superseded_keys(f.documents, [f.profile])


def dated_items(f: VendorFile, superseded: set | None = None) -> list[date]:
    """The expiry dates of the vendor's unsuperseded items: its documents' and its assurance report's."""
    superseded = superseded_in(f) if superseded is None else superseded
    items = [d.expires_on for d in f.documents if d.id not in superseded and d.expires_on is not None]
    p = f.profile
    if p is not None and p.assurance_expires_on is not None and p.id not in superseded:
        items.append(p.assurance_expires_on)
    return items


def summary(f: VendorFile, today: date, days: int) -> dict[str, Any]:
    """A vendor's risk at a glance: the vendor detail's `risk` and the overview's row (without its documents)."""
    p = f.profile
    superseded = superseded_in(f)
    dated = dated_items(f, superseded)
    return {
        "has_profile": p is not None,
        "risk_tier": (p.risk_tier if p else "") or None,
        "processes_personal_data": bool(p and p.processes_personal_data),
        "questionnaire_status": p.questionnaire_status if p else QuestionnaireStatus.NOT_REQUESTED,
        "dpa_status": (p.dpa_status if p else "") or None,
        "assurance": {
            "type": (p.assurance_type if p else "") or None,
            "expires_on": p.assurance_expires_on.isoformat() if p and p.assurance_expires_on else None,
            "status": assurance_status(p, today, days),
        },
        "documents": counts(f.documents, superseded, today, days),
        "next_expiry": min(dated).isoformat() if dated else None,
        "gaps": gap_json(gaps_of(p, f.documents, today)),
    }


def document_brief(d: VendorRiskDocument, superseded: set, today: date, days: int) -> dict[str, Any]:
    return {
        "id": str(d.id),
        "kind": d.kind,
        "title": d.title,
        "expires_on": d.expires_on.isoformat() if d.expires_on else None,
        "status": status_of(d.expires_on, today, days),
        "superseded": d.id in superseded,
    }


# ---- the overview ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OverviewFilters:
    tiers: tuple[str, ...] = ()  # VendorRiskTier values and NO_TIER
    gaps: tuple[str, ...] = ()
    expiring_within: int | None = None  # days: an unsuperseded item expiring from today through today + N
    expired: bool | None = None  # an unsuperseded expired item (true), or none (false)


def matches(f: VendorFile, row: dict[str, Any], filters: OverviewFilters, today: date) -> bool:
    if filters.tiers and (row["risk_tier"] or NO_TIER) not in filters.tiers:
        return False
    if filters.gaps and not {g["code"] for g in row["gaps"]} & set(filters.gaps):
        return False
    dated = dated_items(f)
    if filters.expiring_within is not None:
        end = today + timedelta(days=filters.expiring_within)
        if not any(today <= d <= end for d in dated):
            return False
    if filters.expired is not None and any(d < today for d in dated) != filters.expired:
        return False
    return True


def overview(org, filters: OverviewFilters, today: date | None = None) -> list[dict[str, Any]]:
    """Every vendor with its tier, assurance, document statuses and gaps (GET vendors/risk-overview and its CSV), the
    riskiest first: critical, high, medium, low, not assessed; then by name. Three queries."""
    today = today or org_today(org)
    days = horizon(org)
    vendors = list(Vendor.objects.filter(organization=org).order_by("name", "id"))
    rows = []
    for f in files(org, vendors).values():
        superseded = superseded_in(f)
        row = {
            "vendor": {"id": str(f.vendor.id), "name": f.vendor.name},
            **summary(f, today, days),
            "document_list": [document_brief(d, superseded, today, days) for d in f.documents],
            "_profile": f.profile,
        }
        if matches(f, row, filters, today):
            rows.append(row)
    rows.sort(key=lambda r: (TIER_ORDER.get(r["risk_tier"] or "", 4), r["vendor"]["name"].lower(), r["vendor"]["id"]))
    return rows


# ---- Needs attention -------------------------------------------------------------------------------------------------


def attention_rows(org, today: date) -> list[dict[str, Any]]:
    """The "Vendor risk" queue: one row per vendor with a gap, routed to nobody (a vendor has no IT steward), so the
    coordinator catches it. Empty without the `vendor_risk` capability. Two queries."""
    if not has_capability(org, VENDOR_RISK):
        return []
    profiles = list(
        VendorRiskProfile.objects.filter(organization=org)
        .filter(Q(risk_tier__in=HIGH_TIERS) | Q(processes_personal_data=True))
        .select_related("vendor")
    )
    if not profiles:
        return []
    docs: dict[Any, list[VendorRiskDocument]] = {}
    for d in VendorRiskDocument.objects.filter(organization=org, vendor_id__in=[p.vendor_id for p in profiles]):
        docs.setdefault(d.vendor_id, []).append(d)
    rows = []
    for p in profiles:
        codes = gaps_of(p, docs.get(p.vendor_id, []), today)
        if not codes:
            continue
        rows.append(
            {
                "id": str(p.vendor_id),
                "kind": "vendor_risk",
                "name": p.vendor.name,
                "risk_tier": p.risk_tier or None,
                "gaps": gap_json(codes),
                "why": " ".join(GAP_WHY[c] for c in codes),
                "steward_id": None,
            }
        )
    rows.sort(key=lambda r: (TIER_ORDER.get(str(r["risk_tier"] or ""), 4), str(r["name"]).lower(), str(r["id"])))
    return rows


# ---- reminders (notify/reminders.py, source `vendor_risk`) -------------------------------------------------------


def document_label(d: VendorRiskDocument) -> str:
    kind = RiskDocumentKind(d.kind).label
    return f"{kind} ({d.title}) expires" if d.title else f"{kind} expires"


def assurance_label(profile: VendorRiskProfile) -> str:
    kind = AssuranceType(profile.assurance_type).label if profile.assurance_type else "Assurance report"
    return f"{kind} expires"


def reminder_facts(org, today: date, ids=None) -> list:
    """One reminder per unsuperseded expiry date of the vendors `ids` (every vendor when None): source id the vendor,
    field `document:<id>` or `assurance`. Empty without the capability."""
    from notify.models import ReminderSource
    from notify.reminders import Fact

    if not has_capability(org, VENDOR_RISK):
        return []
    docs = VendorRiskDocument.objects.filter(organization=org)
    profiles = VendorRiskProfile.objects.filter(organization=org, assurance_expires_on__isnull=False)
    vendors = Vendor.objects.filter(organization=org)
    if ids is not None:
        docs, profiles, vendors = (
            docs.filter(vendor_id__in=list(ids)),
            profiles.filter(vendor_id__in=list(ids)),
            vendors.filter(id__in=list(ids)),
        )
    names = dict(vendors.values_list("id", "name"))
    by_vendor: dict[Any, list[VendorRiskDocument]] = {}
    for d in docs:
        by_vendor.setdefault(d.vendor_id, []).append(d)
    profile_rows = list(profiles)
    superseded = superseded_keys((d for rows in by_vendor.values() for d in rows), profile_rows)
    found: list = []
    for vendor_id, rows in by_vendor.items():
        for d in rows:
            if d.expires_on is None or d.id in superseded:
                continue
            found.append(
                Fact(
                    ReminderSource.VENDOR_RISK,
                    vendor_id,
                    d.expires_on,
                    title=names.get(vendor_id, ""),
                    label=document_label(d),
                    source_field=f"document:{d.id}",
                )
            )
    for p in profile_rows:
        if p.id in superseded:
            continue
        assert p.assurance_expires_on is not None
        found.append(
            Fact(
                ReminderSource.VENDOR_RISK,
                p.vendor_id,
                p.assurance_expires_on,
                title=names.get(p.vendor_id, ""),
                label=assurance_label(p),
                source_field="assurance",
            )
        )
    return found


# ---- Writes --------------------------------------------------------------------------------------------------------


def _date(data, name, errors) -> date | None:
    value = data.get(name)
    if value in (None, ""):
        return None
    try:
        return dates.parse_stored(value)
    except ValueError as exc:
        errors[name] = [DATE_MESSAGE if str(exc) == dates.FORMAT_MESSAGE else str(exc)]
    return None


def _expiry(issued: date, months: int, field: str) -> date:
    """Issued on plus the validity, clamped (core/dates.add_months), refused when it would fall after the last date the
    API stores, naming the issue date."""
    day = dates.shift(issued, months, "months")
    if day is None or not dates.in_window(day):
        raise FieldErrors({field: [f"With this validity the expiry would be after {dates.LATEST.isoformat()}."]})
    return day


def _choice(data, name, choices, errors, *, blank: bool) -> str | None:
    value = data.get(name)
    if value is None and blank:
        return ""
    if value == "" and blank:
        return ""
    if isinstance(value, str) and value in choices:
        return value
    words = ", ".join(choices) + (", or empty" if blank else "")
    errors[name] = [f"Choose one of: {words}."]
    return None


def _text(data, name, errors, limit: int) -> str | None:
    value = data.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        errors[name] = ["Send text."]
        return None
    value = value.strip()
    if len(value) > limit:
        errors[name] = [f"Use {limit:,} characters or fewer."]
        return None
    return value


def _months(data, name, errors, *, nullable: bool) -> int | None:
    value = data.get(name)
    if value is None and nullable:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= VALIDITY_MAX:
        return value
    errors[name] = [f"Enter a whole number of months from 1 to {VALIDITY_MAX}" + (", or null." if nullable else ".")]
    return None


def _attachment(org, vendor, data, name, errors) -> Attachment | None:
    value = data.get(name)
    if value in (None, ""):
        return None
    found = (
        Attachment.objects.filter(organization=org, vendor=vendor, id=value).first()
        if isinstance(value, str) and is_uuid(value)
        else None
    )
    if found is None:  # another vendor's or another organization's attachment is unknown here
        errors[name] = [ATTACHMENT_MESSAGE]
    return found


def _url(data, name, errors) -> str | None:
    value = _text(data, name, errors, URL_MAX)
    if value:
        try:
            URLValidator(schemes=["http", "https"])(value)
        except ValidationError:
            errors[name] = ["Enter a full web address starting with http:// or https://."]
            return None
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


PROFILE_FIELDS = (
    "risk_tier",
    "processes_personal_data",
    "questionnaire_status",
    "questionnaire_requested_on",
    "questionnaire_reviewed_on",
    "assurance_type",
    "assurance_issued_on",
    "assurance_validity_months",
    "assurance_attachment",
    "dpa_status",
    "dpa_signed_on",
    "dpa_attachment",
    "subprocessor_url",
    "subprocessor_attachment",
    "subprocessors_reviewed_on",
    "notes",
)
PROFILE_DERIVED = ("assurance_expires_on",)
PROFILE_DATES = (
    "questionnaire_requested_on",
    "questionnaire_reviewed_on",
    "assurance_issued_on",
    "dpa_signed_on",
    "subprocessors_reviewed_on",
)
PROFILE_ATTACHMENTS = ("assurance_attachment", "dpa_attachment", "subprocessor_attachment")


def profile_values(p: VendorRiskProfile) -> dict[str, Any]:
    """What an event records as before and after (references as IDs)."""
    values: dict[str, Any] = {}
    for name in (*PROFILE_FIELDS, *PROFILE_DERIVED):
        attr = f"{name}_id" if name in PROFILE_ATTACHMENTS else name
        values[name] = _plain(getattr(p, attr))
    return values


def _apply_profile(org, profile: VendorRiskProfile, data: dict[str, Any], today: date) -> None:
    errors: dict[str, list[str]] = {
        k: [NOT_ACCEPTED] for k in sorted(set(data) - set(PROFILE_FIELDS) - set(PROFILE_DERIVED))
    }
    vendor = profile.vendor
    for name in PROFILE_DERIVED:
        if name in data:
            errors[name] = ["Worked out from the issue date and the validity; send those instead."]
    if "risk_tier" in data:
        value = _choice(data, "risk_tier", VendorRiskTier.values, errors, blank=True)
        if value is not None:
            profile.risk_tier = value
    if "processes_personal_data" in data:
        value = data["processes_personal_data"]
        if isinstance(value, bool):
            profile.processes_personal_data = value
        else:
            errors["processes_personal_data"] = ["Send true or false."]
    if "questionnaire_status" in data:
        value = _choice(data, "questionnaire_status", QuestionnaireStatus.values, errors, blank=False)
        if value is not None:
            profile.questionnaire_status = value
    for name in PROFILE_DATES:
        if name in data:
            setattr(profile, name, _date(data, name, errors))
    # A status moved to requested or reviewed with no date given for it takes today's.
    if "questionnaire_status" in data and not errors.get("questionnaire_status"):
        if profile.questionnaire_status == QuestionnaireStatus.REQUESTED and "questionnaire_requested_on" not in data:
            profile.questionnaire_requested_on = profile.questionnaire_requested_on or today
        if profile.questionnaire_status == QuestionnaireStatus.REVIEWED and "questionnaire_reviewed_on" not in data:
            profile.questionnaire_reviewed_on = profile.questionnaire_reviewed_on or today
    if (
        profile.questionnaire_requested_on
        and profile.questionnaire_reviewed_on
        and profile.questionnaire_reviewed_on < profile.questionnaire_requested_on
        and not errors
    ):
        errors["questionnaire_reviewed_on"] = ["The review can't be before the request."]
    if "assurance_type" in data:
        value = _choice(data, "assurance_type", AssuranceType.values, errors, blank=True)
        if value is not None:
            profile.assurance_type = value
    if "assurance_validity_months" in data:
        months = _months(data, "assurance_validity_months", errors, nullable=False)
        if months is not None:
            profile.assurance_validity_months = months
    if "dpa_status" in data:
        value = _choice(data, "dpa_status", DpaStatus.values, errors, blank=True)
        if value is not None:
            profile.dpa_status = value
    if "subprocessor_url" in data:
        value = _url(data, "subprocessor_url", errors)
        if value is not None:
            profile.subprocessor_url = value
    if "notes" in data:
        value = _text(data, "notes", errors, TEXT_MAX)
        if value is not None:
            profile.notes = value
    for name in PROFILE_ATTACHMENTS:
        if name in data:
            setattr(profile, name, _attachment(org, vendor, data, name, errors))
    if errors:
        raise FieldErrors(errors)
    # A report is one with a type and an issue date; without both it has no expiry, so it counts for nothing.
    issued = profile.assurance_issued_on
    dated = issued is not None and bool(profile.assurance_type)
    profile.assurance_expires_on = (
        _expiry(issued, profile.assurance_validity_months, "assurance_issued_on")
        if issued is not None and dated
        else None
    )


@transaction.atomic
def update_profile(org, actor, vendor: Vendor, data: dict[str, Any]) -> VendorRiskProfile:
    """Create or change the vendor's risk profile with the fields present in `data` (the first write creates it,
    audited `vendor_risk.created`; later ones `vendor_risk.updated` with each changed field's before and after)."""
    Vendor.objects.select_for_update().filter(organization=org, id=vendor.id).values_list("id").first()
    profile = VendorRiskProfile.objects.select_for_update().filter(organization=org, vendor=vendor).first()
    creating = profile is None
    if profile is None:
        profile = VendorRiskProfile(organization=org, vendor=vendor)
    before = profile_values(profile)
    _apply_profile(org, profile, data, org_today(org))
    after = profile_values(profile)
    if not creating and after == before:
        return profile  # nothing changed: no save, no event
    profile.save()
    if creating:
        # What the new profile holds besides the defaults.
        defaults = profile_values(VendorRiskProfile(organization=org, vendor=vendor))
        changes = {"vendor": str(vendor.id), **{k: v for k, v in after.items() if v != defaults[k]}}
        audit(org, actor, "vendor_risk.created", profile, changes)
    else:
        changed = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
        audit(org, actor, "vendor_risk.updated", profile, {"vendor": str(vendor.id), **changed})
    return profile


DOCUMENT_FIELDS = ("kind", "title", "issued_on", "validity_months", "expires_on", "attachment", "notes")


def document_values(d: VendorRiskDocument) -> dict[str, Any]:
    return {name: _plain(getattr(d, "attachment_id" if name == "attachment" else name)) for name in DOCUMENT_FIELDS}


def _apply_document(org, doc: VendorRiskDocument, data: dict[str, Any]) -> None:
    errors: dict[str, list[str]] = {k: [NOT_ACCEPTED] for k in sorted(set(data) - set(DOCUMENT_FIELDS))}
    if "kind" in data or doc._state.adding:
        value = _choice(data, "kind", RiskDocumentKind.values, errors, blank=False)
        if value is not None:
            doc.kind = value
    if "title" in data:
        value = _text(data, "title", errors, TITLE_MAX)
        if value is not None:
            doc.title = value
    if "notes" in data:
        value = _text(data, "notes", errors, TEXT_MAX)
        if value is not None:
            doc.notes = value
    if "issued_on" in data:
        doc.issued_on = _date(data, "issued_on", errors)
    if "validity_months" in data:
        doc.validity_months = _months(data, "validity_months", errors, nullable=True)
    if "attachment" in data:
        doc.attachment = _attachment(org, doc.vendor, data, "attachment", errors)
    if doc.validity_months is not None:
        # Derived: an expiry sent beside a validity is refused rather than silently replaced.
        if data.get("expires_on") not in (None, ""):
            errors["expires_on"] = ["Send either an expiry date or a validity in months, not both."]
        elif doc.issued_on is None and not errors.get("issued_on"):
            errors["issued_on"] = ["Enter the issue date the validity counts from."]
        elif doc.issued_on is not None:
            doc.expires_on = _expiry(doc.issued_on, doc.validity_months, "issued_on")
    elif "expires_on" in data:  # entered; a validity cleared without a new date leaves the one it gave
        doc.expires_on = _date(data, "expires_on", errors)
    if not errors and doc.issued_on and doc.expires_on and doc.expires_on < doc.issued_on:
        errors["expires_on"] = ["The expiry date can't be before the issue date."]
    if not errors and doc.kind in ASSURANCE_KINDS and doc.expires_on is None:
        # An undated report would count as current assurance for ever.
        errors["expires_on"] = [
            "A SOC 2 report or ISO 27001 certificate needs an expiry date, or an issue date and a validity in months."
        ]
    if errors:
        raise FieldErrors(errors)


@transaction.atomic
def create_document(org, actor, vendor: Vendor, data: dict[str, Any]) -> VendorRiskDocument:
    doc = VendorRiskDocument(organization=org, vendor=vendor)
    _apply_document(org, doc, data)
    doc.save()
    values = {k: v for k, v in document_values(doc).items() if v not in (None, "")}
    audit(org, actor, "vendor_risk_document.created", doc, {"vendor": str(vendor.id), **values})
    return doc


@transaction.atomic
def update_document(org, actor, doc: VendorRiskDocument, data: dict[str, Any]) -> VendorRiskDocument:
    before = document_values(doc)
    _apply_document(org, doc, data)
    after = document_values(doc)
    if after == before:
        return doc  # nothing changed: no save, no event
    doc.save()
    changed = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    audit(org, actor, "vendor_risk_document.updated", doc, {"vendor": str(doc.vendor_id), "kind": doc.kind, **changed})
    return doc


@transaction.atomic
def delete_document(org, actor, doc: VendorRiskDocument) -> None:
    values = {k: v for k, v in document_values(doc).items() if v not in (None, "")}
    audit(org, actor, "vendor_risk_document.deleted", doc, {"vendor": str(doc.vendor_id), **values})
    doc.delete()


def remove_for(org, actor, vendor: Vendor) -> int:
    """Remove a vendor's risk file (inside its delete's transaction, before its attachments go): every document and
    the profile, each audited. Returns how many rows."""
    removed = 0
    for doc in VendorRiskDocument.objects.filter(organization=org, vendor=vendor):
        delete_document(org, actor, doc)
        removed += 1
    profile = VendorRiskProfile.objects.filter(organization=org, vendor=vendor).first()
    if profile is not None:
        audit(org, actor, "vendor_risk.deleted", profile, {"vendor": str(vendor.id)})
        profile.delete()
        removed += 1
    return removed
