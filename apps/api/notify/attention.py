"""Needs attention: every queue for the organization, routed to a steward and cut by scope. The page
(notify/viewsets.py), the navigation badge and the daily digest all read `attention_queues`, so they can't disagree.

The queue functions below return rows with the steward's user ID (or None) still attached; `attention_queues` routes
them (steward, else coordinator). Retired and archived records never appear."""

import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import connection

from catalog import links
from catalog.filters import latest_reviews
from catalog.models import (
    RECORD_KINDS,
    AIAgent,
    ApplicationInstance,
    AssessmentSnapshot,
    Integration,
    LinkConfirmation,
    Person,
    Relationship,
    RelationshipType,
    Resource,
)
from catalog.staleness import OPEN_STATUSES
from catalog.state import (
    ACTIVE_LIFECYCLES,
    AGENT_GAP_LABELS,
    agent_gaps,
    link_signature,
    link_state,
    ownership_gaps,
    ownership_why,
    review_state,
)
from core.drf import UserBriefSerializer, represent
from money import renewal_rules


def _end(rid, kind, name, instance):
    return {"id": str(rid), "kind": kind, "name": name, "instance": instance}


# The database narrows 2,300 links to the ones whose newest confirmation is past the interval or whose signature
# differs (a superset is harmless: Python's link_state decides and writes the Why text). Never-confirmed links are
# left out. The signature is built here exactly as state.link_signature builds it: IDs as text, nulls as null.
_NARROW = """
SELECT {columns}, latest.confirmed_on, latest.confirmed_signature FROM {table} AS link
{joins}
LEFT JOIN {instances} AS si ON si.id = link.source_instance_id
LEFT JOIN {instances} AS ti ON ti.id = link.target_instance_id
JOIN LATERAL (
    SELECT confirmed_on, confirmed_signature FROM {confirmations} AS c
    WHERE c.{column} = {key} AND c.organization_id = %(org)s
    ORDER BY c.confirmed_on DESC, c.created_at DESC LIMIT 1
) AS latest ON TRUE
WHERE {where}
  AND (latest.confirmed_on < %(cutoff)s OR latest.confirmed_signature IS DISTINCT FROM jsonb_build_object(
      'type', {type}, 'source', {source}::text, 'source_instance', {source_instance}::text,
      'target', {target}::text, 'target_instance', {target_instance}::text, 'owner', {owner}::text))
"""
# What each candidate row carries, in this order, for both kinds: the link and record it belongs to, its type, both
# ends (ID, instance, kind, name, instance name), the owner in its signature and the steward it is routed to.
_COLUMNS = {
    True: "link.id, src.kind, src.name, rt.slug, rt.name, link.source_id, link.source_instance_id, src.kind, src.name, "
    "si.name, link.target_id, link.target_instance_id, tgt.kind, tgt.name, ti.name, src.technical_owner_id, "
    "src.it_steward_id",
    False: "link.resource_id, own.kind, own.name, 'integration', 'Integration', link.source_resource_id, link.source_instance_id, "
    "src.kind, src.name, si.name, link.target_resource_id, link.target_instance_id, tgt.kind, tgt.name, ti.name, "
    "own.technical_owner_id, own.it_steward_id",
}


def _narrow(org, today, relationships):
    """Candidate rows for Needs verification, each with everything the queue row needs and its newest confirmation,
    in one query per kind (reading the links a second time by ID cost a quarter of the Needs attention time)."""
    live = "{r}.archived_at IS NULL AND {r}.lifecycle <> 'retired'"
    if relationships:
        sql = _NARROW.format(
            columns=_COLUMNS[True],
            key="link.id",
            table=Relationship._meta.db_table,
            confirmations=LinkConfirmation._meta.db_table,
            instances=ApplicationInstance._meta.db_table,
            column="relationship_id",
            joins=f"JOIN {RelationshipType._meta.db_table} AS rt ON rt.id = link.type_id AND rt.organization_id = link.organization_id "
            f"JOIN {Resource._meta.db_table} AS src ON src.id = link.source_id "
            f"JOIN {Resource._meta.db_table} AS tgt ON tgt.id = link.target_id",
            where="link.organization_id = %(org)s AND " + live.format(r="src") + " AND " + live.format(r="tgt"),
            type="rt.slug",
            source="link.source_id",
            source_instance="link.source_instance_id",
            target="link.target_id",
            target_instance="link.target_instance_id",
            owner="src.technical_owner_id",
        )
    else:
        sql = _NARROW.format(
            columns=_COLUMNS[False],
            key="link.resource_id",
            table=Integration._meta.db_table,
            confirmations=LinkConfirmation._meta.db_table,
            instances=ApplicationInstance._meta.db_table,
            column="integration_id",
            joins=f"JOIN {Resource._meta.db_table} AS own ON own.id = link.resource_id "
            f"JOIN {Resource._meta.db_table} AS src ON src.id = link.source_resource_id "
            f"JOIN {Resource._meta.db_table} AS tgt ON tgt.id = link.target_resource_id",
            where="link.organization_id = %(org)s AND "
            + " AND ".join(live.format(r=alias) for alias in ("own", "src", "tgt")),
            type="'integration'",
            source="link.source_resource_id",
            source_instance="link.source_instance_id",
            target="link.target_resource_id",
            target_instance="link.target_instance_id",
            owner="own.technical_owner_id",
        )
    cutoff = today - timedelta(days=org.review_interval_days)
    with connection.cursor() as cursor:
        cursor.execute(sql, {"org": org.id, "cutoff": cutoff})
        return cursor.fetchall()


def links_to_verify(org, today):
    """Relationships and integrations whose derived state is Needs verification (never the unconfirmed ones: those have
    no date or change to point at). A relationship is routed by its source record's IT steward, an integration by its
    own. Oldest confirmation first. This runs on every Needs attention request and the badge, so the database
    narrows about 2,300 links (the seed_scale size) to the few that may need verifying, with their names, in two queries."""
    interval = timedelta(days=org.review_interval_days)
    candidates: list[dict[str, Any]] = []
    for relationships, link_kind in ((True, "relationship"), (False, "integration")):
        for row in _narrow(org, today, relationships):
            (
                rid,
                rkind,
                rname,
                ltype,
                tname_of_type,
                sid,
                sinst,
                skind,
                sname,
                sin,
                tid,
                tinst,
                tkind,
                tname,
                tin,
                owner,
                steward,
                on,
                signature,
            ) = row
            known = links.Latest(on, signature if isinstance(signature, dict) else json.loads(signature))
            current = link_signature(ltype, sid, sinst, tid, tinst, owner)
            if known.confirmed_signature == current and today <= known.confirmed_on + interval:
                continue  # the common case: confirmed and unchanged
            state, why = link_state(current, known, today, org.review_interval_days)
            if state != "needs_verification":
                continue
            candidates.append(
                {
                    "id": str(rid),
                    "link_kind": link_kind,
                    "type": ltype,
                    "type_name": tname_of_type,
                    "record": {"id": str(rid if not relationships else sid), "kind": rkind, "name": rname},
                    "source": _end(sid, skind, sname, sin),
                    "target": _end(tid, tkind, tname, tin),
                    "why": why,
                    "last_confirmed_on": known.confirmed_on.isoformat(),
                    "steward_id": steward,
                }
            )
    candidates.sort(key=lambda r: (r["last_confirmed_on"], r["record"]["name"].lower(), r["id"]))
    return candidates


def stale_assessments(org):
    """Open assessments whose current snapshot is stale, routed to the assessment owner. Target date order, no
    date last. The why text is the first reason and a count of the rest, from the reasons the snapshot saved.

    One query from the snapshot side, so the assessment and its snapshot are read together: reading the assessments
    and then their snapshots in a second query let a concurrent refresh clear a snapshot in between, and the lookup
    raised KeyError (under parallel e2e)."""
    found = AssessmentSnapshot.objects.filter(
        organization=org,
        is_current=True,  # at most one current snapshot per assessment (partial unique index)
        stale_since__isnull=False,
        assessment__organization=org,
        assessment__status__in=OPEN_STATUSES,
        assessment__target__archived_at__isnull=True,
    ).values_list(
        "assessment_id",
        "assessment__title",
        "assessment__status",
        "assessment__change_type",
        "assessment__target_date",
        "assessment__owner_id",
        "assessment__target_id",
        "assessment__target__kind",
        "assessment__target__name",
        "stale_since",
        "stale_reasons",
    )
    zone = ZoneInfo(org.timezone)
    rows: list[dict[str, Any]] = []
    for aid, title, status, change_type, target_date, owner_id, tid, tkind, tname, stale_since, reasons in found:
        assert stale_since is not None  # filtered above
        reasons = reasons or []
        since = stale_since.astimezone(zone).date().isoformat()
        extra = f" (and {len(reasons) - 1} more)" if len(reasons) > 1 else ""
        rows.append(
            {
                "id": str(aid),
                "title": title,
                "status": status,
                "change_type": change_type,
                "target": {"id": str(tid), "kind": tkind, "name": tname},
                "target_date": target_date.isoformat() if target_date else None,
                "stale_since": since,
                "why": f"Since {since}: {reasons[0]}{extra}" if reasons else f"Snapshot stale since {since}.",
                "steward_id": owner_id,
            }
        )
    rows.sort(key=lambda r: (r["target_date"] or "9999", r["title"].lower(), r["id"]))
    return rows


def renewal_queues(org, today, *, states=None):
    """Renewal actions (an open cycle whose action deadline is within the largest reminder threshold, or overdue) and
    renewal gaps (an obligation with any gap), routed to the IT follow-up assignee. Archived renewals never
    appear. A cycle with an entered deadline and no renewal date is in both: it has an action and a gap. `states` is
    renewals.queue_states(org, today) when the caller already has it (the dashboard shares it with its deadlines)."""
    from money import renewals

    horizon = max(org.reminder_thresholds or [0])
    actions: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    for s in renewals.queue_states(org, today) if states is None else states:
        o = s.obligation
        base = {
            "id": str(o.id),
            "name": o.name,
            "vendor": o.vendor_name,
            "cycle": str(s.cycle.id),
            "deadline": s.deadline.date.isoformat() if s.deadline.date else None,
            "label": s.deadline.label,
            "label_text": renewal_rules.LABELS.get(s.deadline.label),
            "steward_id": o.followup_id,
        }
        if s.timing and s.timing["days"] <= horizon:
            decision = s.cycle.decision
            why = f"Action deadline {base['deadline']} ({base['label_text']}), {s.timing['text'].lower()}."
            if decision:
                why += f" Decision: {decision.capitalize()}, not completed."
            actions.append(
                {**base, "timing": s.timing, "decision": decision or None, "why": why, "kind": "renewal_action"}
            )
        if s.gaps:
            gaps.append({**base, "gaps": s.gaps, "why": "; ".join(s.gaps) + ".", "kind": "renewal_gap"})
    actions.sort(key=lambda r: (r["deadline"], r["name"].lower(), r["id"]))
    gaps.sort(key=lambda r: (r["deadline"] or "9999", r["name"].lower(), r["id"]))
    return actions, gaps


def discovery_candidates(org):
    """One row per connector with undecided or changed candidates, routed to the connector's reviewer. One row
    per connector, not per candidate: a tenant has hundreds of service principals and this page has a 500 ms budget."""
    from discovery import engine as discovery
    from discovery.models import Connector

    counts = discovery.candidate_counts(org)
    rows: list[dict[str, Any]] = []
    for c in Connector.objects.filter(organization=org, id__in=counts).only("id", "name", "reviewer_id"):
        new, changed = counts[c.id]
        parts = [f"{new} new" if new else "", f"{changed} changed since dismissed" if changed else ""]
        rows.append(
            {
                "id": str(c.id),
                "name": c.name,
                "new": new,
                "changed": changed,
                "why": f"{' and '.join(p for p in parts if p)} {'candidate' if new + changed == 1 else 'candidates'} to link, create or dismiss.",
                "steward_id": c.reviewer_id,
                "kind": "discovery",
            }
        )
    rows.sort(key=lambda r: (r["name"].lower(), r["id"]))
    return rows


def discovery_missing(org):
    """One row per linked record whose source no longer lists it ("no longer seen in source", reconciliation;
    discovery/missing.py), routed to the record's IT steward, else the connector's reviewer. A row
    leaves when someone keeps or archives the record, or when the object comes back."""
    from discovery import missing

    return missing.attention_rows(org)


def data_quality(org, today):
    """One row per data-quality rule and IT steward with live records failing it (catalog/quality.attention_rows),
    routed to that steward. Rules another queue already covers (ownership, AI agent and review gaps) are left out, so a
    record is never listed twice for the same reason. Empty without the data_quality capability."""
    from catalog import quality
    from core.capabilities import DATA_QUALITY, has_capability

    if not has_capability(org, DATA_QUALITY):
        return []
    return quality.attention_rows(org, today)


def vendor_risk(org, today):
    """One row per vendor whose risk file has a gap (catalog/vendor_risk.attention_rows): high or critical risk with no
    current SOC 2 or equivalent, or personal data with no signed DPA. A vendor has no IT steward, so the coordinator
    catches every row. Empty without the vendor_risk capability."""
    from catalog import vendor_risk as risk

    return risk.attention_rows(org, today)


def end_of_life(org, today):
    """One row per tracked version of a live record that is end of life or near it (eol/attention.py), routed
    to the record's IT steward. Empty without the eol capability."""
    from eol import attention

    return attention.attention_rows(org, today)


def data_flows(org, today):
    """One row per live data flow with a risk (catalog/data_flows.attention_rows): confidential data to a
    vendor with no DPA, no authentication to an external vendor, or a flow classified above the record receiving it.
    Routed to the integration's IT steward. Empty without the data_flows capability."""
    from catalog import data_flows as flows

    return flows.attention_rows(org, today)


def seat_counts(org):
    """One row per licence pool whose seat counts a source disagrees with (open proposals, money/licences.py), routed
    to its application's IT steward. Empty without the contracts capability, and one query when nothing is observed."""
    from money import licences

    return licences.attention_rows(org)


# Reminder sources whose deadline already has a row of its own in another queue: the reminder is folded into that row
# (`reminder` on a Renewal actions row, matched by cycle; on a Reviews row, matched by record; on an End of life row,
# matched by tracked version) instead of being listed twice. The Reminders queue lists the others.
FOLDED_REMINDERS = {"renewal_cycle": "renewal_actions", "record_review": "reviews", "end_of_life": "end_of_life"}


def _folded_row_id(queue, reminder):
    """The id of the row a folded reminder belongs to, or None (its source is gone)."""
    if queue == "renewal_actions":
        return reminder.cycle_id
    if queue == "end_of_life":
        from eol.reminders import tracked_version_of

        return tracked_version_of(reminder.source_field)
    return reminder.resource_id


def _end_of_life_due_text(reminder) -> str:
    """What an End of life row's reminder date means: the end-of-life date, or for a cycle flagged end of life with no
    date the day tracking began ("End of life (no date published), tracked since ...", eol/reminders.py)."""
    from eol.reminders import is_undated, undated_text

    if is_undated(reminder.source_type, reminder.source_field):
        return undated_text(reminder.due_date)
    return f"End of life on {reminder.due_date.isoformat()}"


def reminder_brief(status, snoozed_until, reminder_id, due) -> dict[str, Any]:
    return {
        "id": str(reminder_id),
        "status": status,
        "snoozed_until": snoozed_until.isoformat() if snoozed_until else None,
        "due": due.isoformat(),
    }


def reminders(org, today):
    """(rows, folded): live reminders (open, snoozed, acknowledged) due within the largest reminder threshold or
    overdue, as Reminders queue rows routed like their source (notify/reminders.routed_to); and {(queue, row id):
    brief} for the reminders folded into a Renewal actions or Reviews row. One query. A snoozed or acknowledged reminder
    stays listed with its status (snoozing quiets notifications, it never hides the item)."""
    from .models import LIVE_REMINDER_STATUSES, Reminder
    from .reminders import path_of, routed_to
    from .services import status_now

    horizon = today + timedelta(days=max(org.reminder_thresholds or [0]))
    found = (
        Reminder.objects.filter(organization=org, status__in=LIVE_REMINDER_STATUSES)
        .annotate(routed=routed_to())
        .values_list(
            "id",
            "source_type",
            "source_id",
            "source_field",
            "due_date",
            "title",
            "label",
            "status",
            "snoozed_until",
            "cycle_id",
            "cycle__obligation_id",
            "contract_id",
            "resource_id",
            "resource__kind",
            "license_pool_id",
            "routed",
            named=True,
        )
    )
    rows: list[dict[str, Any]] = []
    folded: dict[tuple[str, str], dict[str, Any]] = {}
    for r in found:
        status = status_now(r, today)
        snoozed_until = r.snoozed_until if status == "snoozed" else None
        queue = FOLDED_REMINDERS.get(r.source_type)
        if queue is not None:
            row_id = _folded_row_id(queue, r)
            if row_id is not None:
                brief = reminder_brief(status, snoozed_until, r.id, r.due_date)
                if queue == "end_of_life":
                    brief["due_text"] = _end_of_life_due_text(r)
                folded[queue, str(row_id)] = brief
            continue
        if r.due_date > horizon:
            continue
        timing = renewal_rules.timing(r.due_date, today)
        assert timing is not None
        rows.append(
            {
                "id": str(r.id),
                "kind": "reminder",
                "source_type": r.source_type,
                "source_id": str(r.source_id),
                "name": r.title,
                "label": r.label,
                "due": r.due_date.isoformat(),
                "timing": timing,
                "status": status,
                "snoozed_until": snoozed_until.isoformat() if snoozed_until else None,
                "path": path_of(
                    r.source_type,
                    r.source_id,
                    resource_id=r.resource_id,
                    resource_kind=r.resource__kind,
                    obligation_id=r.cycle__obligation_id,
                    contract_id=r.contract_id,
                    license_pool_id=r.license_pool_id,
                ),
                "why": f"{r.label} {r.due_date.isoformat()}, {timing['text'].lower()}.",
                "steward_id": r.routed,
            }
        )
    rows.sort(key=lambda x: (x["due"], x["name"].lower(), x["id"]))
    return rows, folded


def agent_governance(record, agent):
    """(gaps, why) for a live AI agent missing an owner (neither a business nor a technical owner), a data access
    scope or a human-in-loop decision, or None. A missing owner also shows under Ownership gaps
    with the rest of the record's ownership; here it sits with the agent's other governance gaps."""
    gaps = []
    why = []
    if record.business_owner_id is None and record.technical_owner_id is None:
        gaps.append(AGENT_GAP_LABELS["no_owner"])
        why.append("Nobody owns it: no business or technical owner.")
    found = agent_gaps(agent)
    if AGENT_GAP_LABELS["scope"] in found:
        why.append("What data it can reach isn't recorded.")
    if AGENT_GAP_LABELS["hitl"] in found:
        why.append("Whether a person approves its actions isn't decided.")
    gaps += found
    return (gaps, " ".join(why)) if gaps else None


# ---- every queue, routed (the page, the badge and the digest) ------------------------------------------------

STATE_ORDER = {"overdue": 0, "needs_confirmation": 1, "unreviewed": 2}
CRITICALITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "": 4}


SCOPES = ("mine", "all", "unassigned")


QUEUES = (
    "ownership_gaps",
    "agent_gaps",
    "reviews",
    "links_to_verify",
    "stale_assessments",
    "renewal_actions",
    "renewal_gaps",
    "discovery_candidates",
    "discovery_missing",
    "data_quality",
    "seat_counts",
    "reminders",
    "vendor_risk",
    "end_of_life",
    "data_flows",
)


# Queues listed in the response but not yet in `counts` (the navigation badge), because the SPA doesn't render them
# yet: the page adds up the queues it shows, so counting one it doesn't would make the badge and the page disagree. A
# queue added before its page exists goes here until then. Every queue is counted now.
#
# Reminders count in the badge but are not a digest section (digest.SECTIONS): their notification rules already email
# them (the built-ins cover contracts, periodic reviews, custom dates, credentials and vendor risk documents), so the
# digest would repeat them. End of life is counted but not a digest section either since notify 0012: the built-in End
# of life rule emails each version's reminder (digest.NOT_SECTIONS).
UNCOUNTED: frozenset[str] = frozenset()


def attention_queues(org, user, today, *, renewal_states=None):
    """Every Needs attention queue for the organization, each row carrying `_flags` (mine, unassigned) as seen by
    `user`, and the counts per scope. The page and the daily digest both read this, so they can't disagree.
    `renewal_states` is renewals.queue_states(org, today) when the caller has already loaded it (the dashboard)."""
    is_coordinator = org.coordinator_id == user.id
    # Plain tuples and small namespaces instead of model instances: at 1,000 resources, hydrating Resource, two Person
    # and a User per row dominated the time, and the badge asks for this on every page.
    fields = (
        "id",
        "kind",
        "name",
        "criticality",
        "lifecycle",
        "created_at",
        "business_owner_id",
        "technical_owner_id",
        "support_team_id",
        "it_steward_id",
    )
    people = {
        pid: SimpleNamespace(name=name, active=active)
        for pid, name, active in Person.objects.filter(organization=org).values_list("id", "name", "active")
    }
    items = [
        SimpleNamespace(
            **dict(zip(fields, row, strict=True)),
            business_owner=people.get(row[6]),
            technical_owner=people.get(row[7]),
        )
        for row in Resource.objects.filter(
            organization=org, kind__in=RECORD_KINDS, archived_at__isnull=True, lifecycle__in=ACTIVE_LIFECYCLES
        ).values_list(*fields)
    ]
    # A subquery, not hundreds of ID parameters: the database plans it better (as in the dependency timing work).
    live = Resource.objects.filter(
        organization=org, kind__in=RECORD_KINDS, archived_at__isnull=True, lifecycle__in=ACTIVE_LIFECYCLES
    ).values("id")
    latest = latest_reviews(org, live)
    agents = {
        rid: SimpleNamespace(data_access_scope=scope, human_in_loop=hitl)
        for rid, scope, hitl in AIAgent.objects.filter(organization=org, resource_id__in=live).values_list(
            "resource_id", "data_access_scope", "human_in_loop"
        )
    }
    link_rows = links_to_verify(org, today)
    stale_rows = stale_assessments(org)
    action_rows, renewal_gap_rows = renewal_queues(org, today, states=renewal_states)
    discovery_rows = discovery_candidates(org)
    missing_rows = discovery_missing(org)
    quality_rows = data_quality(org, today)
    seat_rows = seat_counts(org)
    reminder_rows, folded = reminders(org, today)
    vendor_risk_rows = vendor_risk(org, today)
    eol_rows = end_of_life(org, today)
    flow_rows = data_flows(org, today)
    for row in action_rows:
        row["reminder"] = folded.get(("renewal_actions", row["cycle"]))
    for row in eol_rows:
        row["reminder"] = folded.get(("end_of_life", row["id"]))
    routed_rows = (
        link_rows
        + stale_rows
        + action_rows
        + renewal_gap_rows
        + discovery_rows
        + missing_rows
        + quality_rows
        + seat_rows
        + reminder_rows
        + vendor_risk_rows
        + eol_rows
        + flow_rows
    )
    wanted = {r.it_steward_id for r in items} | {r["steward_id"] for r in routed_rows}
    users = get_user_model().objects.in_bulk(wanted - {None})
    stewards: dict = {}  # steward id -> (JSON, mine, unassigned), built once per person

    def routed(user_id):
        """(steward JSON, mine, unassigned) for an item whose own steward or owner is `user_id` (or None)."""
        if user_id not in stewards:
            if user_id in users:
                stewards[user_id] = (represent(UserBriefSerializer, users[user_id]), user_id == user.id, False)
            elif org.coordinator is None:
                stewards[user_id] = ({"name": "Unassigned"}, False, True)
            else:
                stewards[user_id] = (
                    dict(represent(UserBriefSerializer, org.coordinator) or {}, coordinator=True),
                    is_coordinator,
                    True,
                )
        return stewards[user_id]

    owner_rows, agent_rows, review_rows = [], [], []
    for r in items:
        steward, mine, unassigned = routed(r.it_steward_id)
        flags = {"mine": mine, "unassigned": unassigned}
        if r.id in agents:
            found = agent_governance(r, agents[r.id])
            if found:
                agent_rows.append(
                    {
                        "id": str(r.id),
                        "kind": r.kind,
                        "name": r.name,
                        "criticality": r.criticality or None,
                        "gaps": found[0],
                        "why": found[1],
                        "steward": steward,
                        "_flags": flags,
                    }
                )
        gaps = ownership_gaps(r)
        if gaps:
            owner_rows.append(
                {
                    "id": str(r.id),
                    "kind": r.kind,
                    "name": r.name,
                    "criticality": r.criticality or None,
                    "gaps": gaps,
                    "why": ownership_why(r),
                    "steward": steward,
                    "_flags": flags,
                }
            )
        state, why = review_state(r, latest.get(r.id), today)
        if state in STATE_ORDER:
            review = latest.get(r.id)
            review_rows.append(
                {
                    "id": str(r.id),
                    "kind": r.kind,
                    "name": r.name,
                    "state": state,
                    "why": why,
                    "steward": steward,
                    "due": review.next_review_date.isoformat() if review else None,
                    "reminder": folded.get(("reviews", str(r.id))),
                    "_flags": flags,
                }
            )
    owner_rows.sort(key=lambda x: (CRITICALITY_ORDER[x["criticality"] or ""], x["name"].lower()))
    agent_rows.sort(key=lambda x: (CRITICALITY_ORDER[x["criticality"] or ""], x["name"].lower(), x["id"]))
    review_rows.sort(key=lambda x: (STATE_ORDER[x["state"]], x["due"] or "9999", x["name"].lower()))
    for row in routed_rows:
        row["steward"], mine, unassigned = routed(row.pop("steward_id"))
        row["_flags"] = {"mine": mine, "unassigned": unassigned}
    queues = dict(
        zip(
            QUEUES,
            [
                owner_rows,
                agent_rows,
                review_rows,
                link_rows,
                stale_rows,
                action_rows,
                renewal_gap_rows,
                discovery_rows,
                missing_rows,
                quality_rows,
                seat_rows,
                reminder_rows,
                vendor_risk_rows,
                eol_rows,
                flow_rows,
            ],
            strict=True,
        )
    )
    counted = [q for name, q in queues.items() if name not in UNCOUNTED]
    counts = {
        "all": sum(len(q) for q in counted),
        "mine": sum(r["_flags"]["mine"] for q in counted for r in q),
        "unassigned": sum(r["_flags"]["unassigned"] for q in counted for r in q),
    }
    return queues, counts
