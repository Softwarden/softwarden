"""Derived review state, gaps, and Why text. Pure functions over loaded records."""

from datetime import date, timedelta

CONFIRMED_FIELDS = [
    ("business_owner", "Business owner"),
    ("technical_owner", "Technical owner"),
    ("support_team", "Support team"),
    ("lifecycle", "Lifecycle"),
    ("criticality", "Criticality"),
]
ACTIVE_LIFECYCLES = {"active", "deprecated"}


def confirmed_snapshot(resource):
    """The values a review confirms, as stored in RecordReview.confirmed_values."""
    return {
        "business_owner": str(resource.business_owner_id) if resource.business_owner_id else None,
        "technical_owner": str(resource.technical_owner_id) if resource.technical_owner_id else None,
        "support_team": str(resource.support_team_id) if resource.support_team_id else None,
        "lifecycle": resource.lifecycle,
        "criticality": resource.criticality or None,
    }


def _join(labels):
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " and " + labels[-1].lower()


# The gap labels, shared with the data-quality rules that check the same things (catalog/quality.py), so a chip and
# a rule never word one gap two ways.
OWNERSHIP_LABELS = {
    "business_owner": "No business owner",
    "technical_owner": "No technical owner",
    "support_team": "No support team",
    "inactive_owner": "Inactive owner",
    "criticality": "No criticality",
}


def ownership_gaps(resource):
    gaps = []
    # An integration has no business owner, so its absence is not a gap.
    if resource.business_owner_id is None and resource.kind != "integration":
        gaps.append(OWNERSHIP_LABELS["business_owner"])
    if resource.technical_owner_id is None:
        gaps.append(OWNERSHIP_LABELS["technical_owner"])
    if resource.support_team_id is None:
        gaps.append(OWNERSHIP_LABELS["support_team"])
    inactive = OWNERSHIP_LABELS["inactive_owner"]
    for owner in (resource.business_owner, resource.technical_owner):
        if owner is not None and not owner.active and inactive not in gaps:
            gaps.append(inactive)
    return gaps


def all_gaps(resource):
    gaps = ownership_gaps(resource)
    if not resource.criticality and resource.kind != "integration":  # integrations have none
        gaps.append(OWNERSHIP_LABELS["criticality"])
    return gaps


AGENT_GAP_LABELS = {"no_owner": "No owner", "scope": "No data access scope", "hitl": "No human-in-loop decision"}

# SSO and MFA coverage gaps (discovery/coverage.py): the chips on an application's coverage and the coverage
# report, and the labels of the data-quality rules that check the same things (catalog/quality.py).
COVERAGE_LABELS = {
    "no_sso": "No SSO",
    "no_scim": "No SCIM",
    "no_mfa": "No MFA policy",
    "mfa_partial": "MFA for some users only",
    "unknown": "Coverage unknown",
}


def agent_gaps(agent):
    """An AI agent's governance gaps: no data access scope recorded, and no human-in-loop decision
    (null; false is a decision). `agent` is the AIAgent row or anything with those two attributes."""
    gaps = []
    if not (agent.data_access_scope or "").strip():
        gaps.append(AGENT_GAP_LABELS["scope"])
    if agent.human_in_loop is None:
        gaps.append(AGENT_GAP_LABELS["hitl"])
    return gaps


def ownership_why(resource):
    empty = [label for field, label in CONFIRMED_FIELDS[:3] if getattr(resource, field + "_id") is None]
    parts = []
    if empty:
        parts.append(f"{_join(empty)} {'is' if len(empty) == 1 else 'are'} empty.")
    for field, label in CONFIRMED_FIELDS[:2]:
        owner = getattr(resource, field)
        if owner is not None and not owner.active:
            parts.append(f"{label} {owner.name} is inactive.")
    return " ".join(parts)


def review_state(resource, latest_review, today: date):
    """Return (state, why). Needs confirmation wins over Overdue; Due still shows lateness."""
    if latest_review is None:
        created = resource.created_at.date().isoformat() if resource.created_at else ""
        return "unreviewed", f"Created {created}; never reviewed."
    reasons = []
    current = confirmed_snapshot(resource)
    changed = [
        label for field, label in CONFIRMED_FIELDS if latest_review.confirmed_values.get(field) != current[field]
    ]
    if changed:
        reasons.append(f"{_join(changed)} changed since the last review.")
    for field, label in CONFIRMED_FIELDS[:2]:
        owner = getattr(resource, field)
        if owner is not None and not owner.active:
            reasons.append(f"{label} {owner.name} is inactive.")
    overdue = today > latest_review.next_review_date
    if overdue:
        reasons.append(f"Next review was {latest_review.next_review_date.isoformat()}.")
    if changed or any("inactive" in r for r in reasons):
        return "needs_confirmation", " ".join(reasons)
    if overdue:
        return "overdue", " ".join(reasons)
    return "confirmed", f"Confirmed {latest_review.confirmation_date.isoformat()}."


# ---- Link state -----------------------------------------------------------------

SIGNATURE_LABELS = {
    "type": "Type",
    "source": "Source",
    "source_instance": "Source instance",
    "target": "Target",
    "target_instance": "Target instance",
    "owner": "Owner",
}


def _id(value):
    return str(value) if value else None


def link_signature(link_type, source, source_instance, target, target_instance, owner):
    """The members a confirmation covers, from IDs (UUIDs or their strings). IDs only, so a rename changes nothing."""
    return {
        "type": link_type,
        "source": _id(source),
        "source_instance": _id(source_instance),
        "target": _id(target),
        "target_instance": _id(target_instance),
        "owner": _id(owner),
    }


def relationship_signature(rel, owner_id):
    """What a confirmation covers for a relationship: type, endpoints, instances and the owner, which is the source
    record's technical owner."""
    return link_signature(
        rel.type.slug, rel.source_id, rel.source_instance_id, rel.target_id, rel.target_instance_id, owner_id
    )


def integration_signature(integration, owner_id):
    """The same for an integration: its two ends and instances, and its own technical owner. An integration has no
    type of its own, so the key is fixed."""
    return link_signature(
        "integration",
        integration.source_resource_id,
        integration.source_instance_id,
        integration.target_resource_id,
        integration.target_instance_id,
        owner_id,
    )


def link_state(signature, latest, today: date, interval_days: int):
    """Return (state, why) for a relationship or integration. `latest` is its newest LinkConfirmation or None;
    today is the date in the organization's timezone. Needs verification when the confirmation is older than the
    interval (the last good day is confirmed_on + interval) or the current signature differs from the confirmed one.
    A record review never enters into this."""
    if latest is None:
        return "unconfirmed", "Never confirmed."
    reasons = []
    confirmed = latest.confirmed_signature
    changed = (
        []
        if confirmed == signature
        else [label for key, label in SIGNATURE_LABELS.items() if confirmed.get(key) != signature[key]]
    )
    if changed:
        reasons.append(f"{_join(changed)} changed since confirmation.")
    if today > latest.confirmed_on + timedelta(days=interval_days):
        reasons.append(f"Confirmed {latest.confirmed_on.isoformat()}; interval {interval_days} days.")
    if reasons:
        return "needs_verification", " ".join(reasons)
    return "confirmed", f"Confirmed {latest.confirmed_on.isoformat()}."
