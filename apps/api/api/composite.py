"""Composite read endpoints (re-implemented): compact, stable answers for scripts
and the MCP server's read tools (`GET /api/v1/composite/...`, api/composite_views.py). Read-only: nothing here writes,
so nothing is audited.

One source of truth. Every builder reads the same service function as the screen that shows the same thing, and only
reshapes (and, for owners, enriches) its rows, so a composite can never disagree with the UI:
- search: core/search.run with the /search parameters and throttle (core/search_views.search_options; the command
  palette and the results page);
- deadlines and renewals: money/reporting.deadline_rows (the dashboard, the renewals calendar, the ICS feed and the
  reminders' dates); renewals adds who owns each one, in a fixed number of queries;
- spend: money/reporting.load, groups and top (the spend report and the dashboard's charts);
- dependencies: catalog/graph.traverse (the dependency explorer);
- discovery summary: discovery/engine.status_counts_by (the candidate queue's counts, by the same status rules as
  GET discovery/candidates) and each connector's last finished run and the one queued or running;
- data gaps: catalog/quality.summary_json, failing_records and evaluate (the data-quality page and the dashboard
  panel);
- expiring credentials: money/reporting.expiring_credentials (the dashboard block, API tokens and discovered ones).

Compact: references are `{id, name}` (people add `email` and `active`, user accounts `email`), every record carries
its `path` under /api/v1/ (the SPA uses the same path), and lists are capped by `limit` with `count` (every match)
and `truncated`.
Every query filters on the organization; query counts don't grow with rows (tests/test_composite.py)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from django.db.models.functions import Lower

from catalog import graph, quality, record_map
from catalog.models import KIND_PATHS, Kind, Resource
from catalog.serializers import quality_json
from core import capabilities as caps
from core import search
from core.drf import timestamp
from core.problems import CapabilityRequired
from money import reporting

# ---- limits and shared shapes ----------------------------------------------------------------------------------------

LIMIT_DEFAULT, LIMIT_MAX = 50, 200
DEADLINE_DAYS, DEADLINE_DAYS_MAX = 90, 730
CREDENTIAL_DAYS_MAX = 365
SPEND_GROUPS = 25  # real groups per currency; the rest fold into one "Everything else" row (gaps are never folded)
RECORDS_PER_ROW = 20  # records listed on one renewal row (owners are gathered from all of them)
CONNECTORS_MAX = 100


def require_any(org, *names: str) -> None:
    """403 capability problem naming the first capability unless the organization has at least one of them."""
    have = caps.capabilities(org)
    if not any(n in have for n in names):
        raise CapabilityRequired(names[0])


def record_path(kind: str, rid: Any) -> str:
    segment = KIND_PATHS.get(kind) or ("integrations" if kind == Kind.INTEGRATION else f"{kind}s")
    return f"{segment}/{rid}"


def person(p) -> dict[str, Any] | None:
    """A Person: who owns or answers for something. Not necessarily a user."""
    if p is None:
        return None
    return {"id": str(p.id), "name": p.name, "email": p.email or None, "active": bool(p.active)}


def user(u) -> dict[str, Any] | None:
    """A user account (IT steward, IT follow-up, token owner)."""
    if u is None:
        return None
    return {"id": int(u.id), "name": u.get_full_name() or u.email, "email": u.email or None}


def team(t) -> dict[str, Any] | None:
    return None if t is None else {"id": str(t.id), "name": t.name}


def record_owners(r: Resource) -> dict[str, Any]:
    return {
        "business_owner": person(r.business_owner),
        "technical_owner": person(r.technical_owner),
        "it_steward": user(r.it_steward),
        "support_team": team(r.support_team),
    }


def record_ref(r: Resource) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "kind": r.kind,
        "name": r.name,
        "lifecycle": r.lifecycle,
        "archived": r.archived_at is not None,
        "path": record_path(r.kind, r.id),
    }


OWNER_RELATED = ("business_owner", "technical_owner", "it_steward", "support_team")


def load_records(org, ids: Iterable[Any]) -> dict[str, Resource]:
    """{id: record with its owners} in one query; kinds the edition lacks (AI agents without ai_agents) left out."""
    wanted = {str(i) for i in ids if i}
    if not wanted:
        return {}
    qs = Resource.objects.filter(organization=org, id__in=wanted).select_related(*OWNER_RELATED)
    hidden = record_map.hidden_kinds(org)
    if hidden:
        qs = qs.exclude(kind__in=hidden)
    return {str(r.id): r for r in qs}


def capped(rows: list[Any], limit: int) -> tuple[list[Any], bool]:
    return rows[:limit], len(rows) > limit


# ---- search ----------------------------------------------------------------------------------------------------------


def snippet_text(snippet: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snippet:
        return None
    return {
        "field": snippet["field"],
        "label": snippet["label"],
        "text": "".join(s["text"] for s in snippet["segments"]),
    }


def search_results(org, query: search.Query, kinds: list[str], limit: int, include_archived: bool) -> dict[str, Any]:
    """core/search.run, flattened: one list, grouped kinds in registry order, best first within each, the snippet as
    plain text. `count` is every match; `truncated` says a kind had more than `limit`."""
    found = search.run(org, query, kinds, limit, include_archived)
    results = []
    truncated = False
    for group in found["groups"]:
        truncated |= group["count"] > len(group["results"])
        for hit in group["results"]:
            results.append(
                {
                    "kind": hit["kind"],
                    "id": hit["id"],
                    "name": hit["name"],
                    "path": hit["path"],
                    "lifecycle": hit["lifecycle"],
                    "archived": hit["archived"],
                    "snippet": snippet_text(hit["snippet"]),
                    "parent": hit["parent"],
                }
            )
    return {
        "query": found["query"],
        "ignored": found["ignored"],
        "fuzzy": found["fuzzy"],
        "count": sum(g["count"] for g in found["groups"]),
        "by_kind": {g["kind"]: g["count"] for g in found["groups"]},
        "truncated": truncated,
        "results": results,
    }


# ---- deadlines and renewals -------------------------------------------------------------------------------------------


def _deadline(row: dict[str, Any]) -> dict[str, Any]:
    """A deadline row (money/reporting.deadline_rows) in the compact shape both composites share."""
    path = f"renewals/{row['id']}" if row["source"] == "renewal" else f"contracts/{row['id']}"
    return {
        "source": row["source"],
        "id": row["id"],
        "name": row["name"],
        "path": path,
        "date": row["date"],
        "days": row["timing"]["days"],
        "overdue": row["timing"]["overdue"],
        "action": row["action"],
        "label": row["label_text"],
        "explanation": row["explanation"] or None,
        "term_ends": row["term_ends"],
        "auto_renew": row["auto_renew"],
        "decision": row["decision"],
        "vendor_name": row["vendor"],
        "contract": row["contract"],
        "annualized_cost": row["annualized_cost"],
        "currency": row["currency"],
    }


BASES = ("action_deadline", "term_end")


def _deadline_rows(org, today: date, within_days: int, basis: str = "action_deadline") -> list[dict[str, Any]]:
    """The open action deadlines due within `within_days` (overdue ones included), soonest first; or, with
    basis=term_end, the open actions whose term (a renewal's renewal date, a contract's end date) ends within
    `within_days` or has already ended, whenever their action deadline falls (an entered deadline can be after the
    renewal date), soonest term end first. Rows with no term end are left out of term_end."""
    require_any(org, caps.RENEWALS, caps.CONTRACTS)
    until = today + timedelta(days=within_days)
    if basis != "term_end":
        return reporting.deadline_rows(org, today, until=until)
    last = until.isoformat()
    rows = [r for r in reporting.deadline_rows(org, today) if r["term_ends"] and r["term_ends"] <= last]
    rows.sort(key=lambda r: (r["term_ends"], r["name"].casefold(), r["uid"]))
    return rows


def deadlines(org, today: date, within_days: int, limit: int) -> dict[str, Any]:
    """Every open action deadline up to `within_days` ahead, overdue ones included, soonest first, with the 30/60/90-day
    counts. With within_days=90 the counts and `count` are the dashboard's deadlines block's."""
    rows = _deadline_rows(org, today, within_days)
    shown, truncated = capped(rows, limit)
    counts = reporting.windows(rows, today)
    return {
        "as_of": today.isoformat(),
        "within_days": within_days,
        "overdue": counts["overdue"],
        "windows": counts["windows"],
        "count": len(rows),
        "truncated": truncated,
        "results": [{**_deadline(r), "follow_up": r["owner"]} for r in shown],
    }


def _vendor(v) -> dict[str, Any] | None:
    if v is None:
        return None
    return {
        "id": str(v.id),
        "name": v.name,
        "account_manager": person(v.account_manager),
        "support_email": v.support_email or None,
    }


def _distinct(values: Iterable[dict[str, Any] | None]) -> list[dict[str, Any]]:
    found: dict[Any, dict[str, Any]] = {}
    for v in values:
        if v is not None:
            found.setdefault(v["id"], v)
    return sorted(found.values(), key=lambda v: (v["name"].casefold(), str(v["id"])))


GAPS = (
    "no_renewal_owner",
    "renewal_owner_inactive",
    "no_follow_up",
    "no_vendor",
    "no_vendor_contact",
    "no_records",
    "no_business_owner",
    "no_it_steward",
    "no_support_team",
)


def renewal_gaps(source: str, owner, contract_owner, follow_up, vendor, records: list[Resource]) -> list[str]:
    """Named gaps in who answers for a renewal, so a missing owner is said, never just null. The renewal has an owner
    when either its own (a renewal's responsible person) or its contract's renewal owner is set; it is inactive when
    every one that is set has left. Codes in GAPS order."""
    gaps = []
    owners = [o for o in (owner, contract_owner) if o is not None]
    if not owners:
        gaps.append("no_renewal_owner")
    elif not any(o["active"] for o in owners):
        gaps.append("renewal_owner_inactive")
    if source == "renewal" and follow_up is None:
        gaps.append("no_follow_up")
    if vendor is None:
        gaps.append("no_vendor")
    elif vendor["account_manager"] is None and vendor["support_email"] is None:
        gaps.append("no_vendor_contact")
    if not records:
        gaps.append("no_records")
    else:
        if any(r.business_owner_id is None for r in records):
            gaps.append("no_business_owner")
        if any(r.it_steward_id is None for r in records):
            gaps.append("no_it_steward")
        if any(r.support_team_id is None for r in records):
            gaps.append("no_support_team")
    return gaps


def renewals(org, today: date, within_days: int, limit: int, basis: str = "action_deadline") -> dict[str, Any]:
    """The deadlines of GET composite/deadlines with who owns each one: the renewal owner (a renewal's responsible
    person, a contract's renewal owner), the contract owner (the renewal owner of the contract on the row: the one that
    drives a renewal, or the contract itself; never copied into renewal_owner, so a renewal linked to a contract after
    it was created still shows the contract's owner), the IT follow-up (renewals), the vendor's contacts, and the
    records it covers
    (a renewal's open cycle's applications plus, when a contract drives it, that contract's lines; a contract's lines)
    with their business and technical owners, IT steward and support team, also gathered per role. Six queries on top
    of the deadline rows, however many rows."""
    from money.models import Contract, ContractItem, RenewalCycleApplication, RenewalObligation

    rows = _deadline_rows(org, today, within_days, basis)
    shown, truncated = capped(rows, limit)
    renewal_rows = [r for r in shown if r["source"] == "renewal"]
    contract_ids = {r["contract"]["id"] for r in shown if r["contract"]}
    obligations = {
        str(o.id): o
        for o in RenewalObligation.objects.filter(
            organization=org, id__in=[r["id"] for r in renewal_rows]
        ).select_related("responsible", "followup", "vendor__account_manager")
    }
    terms = {
        str(c.id): c
        for c in Contract.objects.filter(organization=org, id__in=contract_ids).select_related(
            "renewal_owner", "vendor__account_manager"
        )
    }
    covered: dict[str, list[str]] = {}
    for cycle_id, app_id in RenewalCycleApplication.objects.filter(
        organization=org, cycle_id__in=[r["cycle"] for r in renewal_rows if r["cycle"]]
    ).values_list("cycle_id", "application_id"):
        covered.setdefault(str(cycle_id), []).append(str(app_id))
    lines: dict[str, list[str]] = {}
    for contract_id, resource_id in (
        ContractItem.objects.filter(organization=org, contract_id__in=contract_ids)
        .order_by("created_at", "id")
        .values_list("contract_id", "resource_id")
    ):
        lines.setdefault(str(contract_id), []).append(str(resource_id))
    records = load_records(
        org, [i for ids in covered.values() for i in ids] + [i for ids in lines.values() for i in ids]
    )

    out = []
    for row in shown:
        term = terms.get(row["contract"]["id"]) if row["contract"] else None
        if row["source"] == "renewal":
            o = obligations[row["id"]]
            owner, follow_up, vendor = person(o.responsible), user(o.followup), _vendor(o.vendor)
            ids = covered.get(row["cycle"] or "", [])
        else:
            assert term is not None  # a contract row's contract is itself
            owner, follow_up, vendor = person(term.renewal_owner), None, _vendor(term.vendor)
            ids = []
        ids = list(dict.fromkeys([*ids, *(lines.get(str(term.id), []) if term else [])]))
        mine = sorted((records[i] for i in ids if i in records), key=lambda r: (r.name.casefold(), r.kind, str(r.id)))
        contract_owner = person(term.renewal_owner) if term is not None else None
        out.append(
            {
                **_deadline(row),
                "vendor": vendor,
                "owners": {
                    "renewal_owner": owner,
                    "contract_owner": contract_owner,
                    "follow_up": follow_up,
                    "business_owners": _distinct(person(r.business_owner) for r in mine),
                    "technical_owners": _distinct(person(r.technical_owner) for r in mine),
                    "it_stewards": _distinct(user(r.it_steward) for r in mine),
                    "support_teams": _distinct(team(r.support_team) for r in mine),
                },
                "records": [{**record_ref(r), **record_owners(r)} for r in mine[:RECORDS_PER_ROW]],
                "records_total": len(mine),
                "gaps": renewal_gaps(row["source"], owner, contract_owner, follow_up, vendor, mine),
            }
        )
    return {
        "as_of": today.isoformat(),
        "within_days": within_days,
        "basis": basis,
        "count": len(rows),
        "truncated": truncated,
        "results": out,
    }


# ---- spend -----------------------------------------------------------------------------------------------------------


def spend(org, today: date, by: str, groups_per_currency: int) -> dict[str, Any]:
    """Annualised spend of live contracts per currency (never converted), grouped by `by`: the largest
    `groups_per_currency` real groups, every named gap, and one "Everything else" row (gap `other`), so each currency's
    groups still add up to its `annualized` total. With by=vendor and 8 groups this is the dashboard's chart."""
    if not caps.has_capability(org, caps.CONTRACTS):
        raise CapabilityRequired(caps.CONTRACTS)
    loaded = reporting.load(org)
    rows = reporting.top(reporting.groups(by, loaded.allocations), groups_per_currency)
    return {
        "as_of": today.isoformat(),
        "by": by,
        "totals": loaded.commitments,
        "groups": [{**{k: v for k, v in r.items() if k != "groups"}, "folded": r.get("groups")} for r in rows],
        "truncated": any(r["gap"] == "other" for r in rows),
    }


# ---- dependencies ----------------------------------------------------------------------------------------------------


def _node(n: dict[str, Any]) -> dict[str, Any]:
    via = n["path"][-1]["via"] if n["path"] else None
    return {
        "id": n["id"],
        "kind": n["kind"],
        "name": n["name"],
        "path": record_path(n["kind"], n["id"]),
        "lifecycle": n["lifecycle"],
        "archived": n["archived"],
        "hops": n["depth"],
        "via": via["type_name"] if via else None,
        "through": [step["name"] for step in n["path"][1:-1]],
        "cycle": n["cycle"],
        "link_state": n["link"]["state"],
        "technical_owner": n["technical_owner"],
    }


def _flow(f: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f["id"],
        "name": f["name"],
        "path": record_path(Kind.INTEGRATION, f["id"]),
        "lifecycle": f["lifecycle"],
        "archived": f["archived"],
        "source": {k: f["source"][k] for k in ("id", "kind", "name")},
        "target": {k: f["target"][k] for k in ("id", "kind", "name")},
        "protocol": f["protocol"] or None,
        "link_state": f["link"]["state"],
        "technical_owner": f["technical_owner"],
    }


def dependencies(org, target: Resource, direction: str, depth: int | None, include_retired: bool, limit: int):
    """catalog/graph.traverse (the dependency explorer), compact: what the target relies on (`depends_on`), what relies
    on it (`dependents`), nearest first, and the integrations touching them (`data_flows`). `count` is every node and
    flow the walk found; `truncated` when the explorer's cap or `limit` cut any list."""
    if target.kind == Kind.AI_AGENT and not caps.has_capability(org, caps.AI_AGENTS):
        raise CapabilityRequired(caps.AI_AGENTS)
    found = graph.traverse(org, target, direction=direction, depth=depth, include_retired=include_retired, flows=True)
    wanted = ("depends_on", "dependents") if direction == "both" else (direction,)
    lists = {d: [_node(n) for n in found["nodes"] if n["direction"] == d] for d in wanted}
    flows = [_flow(f) for f in found["data_flows"]]
    cut = any(len(v) > limit for v in lists.values()) or len(flows) > limit
    summary = found["summary"]
    return {
        "target": {**record_ref(target), "technical_owner": person(target.technical_owner)},
        "direction": direction,
        "depth": found["depth"],
        "include_retired": include_retired,
        "count": found["total"],
        "truncated": bool(found["capped"]) or cut,
        "summary": {
            "nodes": summary["nodes"],
            "data_flows": summary["data_flows"],
            "cycles": summary["cycles"],
            "hidden_retired": summary["hidden_retired"],
        },
        "depends_on": lists["depends_on"][:limit] if "depends_on" in lists else None,
        "dependents": lists["dependents"][:limit] if "dependents" in lists else None,
        "data_flows": flows[:limit],
    }


# ---- discovery summary -----------------------------------------------------------------------------------------------


def discovery_summary(org, today: date) -> dict[str, Any]:
    """Per connector and for the organization: the candidate queue's counts (the same status rules as GET
    discovery/candidates `counts`, every connector), the last finished run and the one queued or running. Four
    queries."""
    from discovery import engine
    from discovery.models import NOT_CANDIDATES, Connector, ConnectorRun, Observation, RunStatus

    observations = Observation.objects.filter(organization=org).exclude(kind__in=NOT_CANDIDATES)
    counts = engine.status_counts_by(observations, "connector_id")
    connectors = list(
        Connector.objects.filter(organization=org)
        .select_related("reviewer")
        .order_by(Lower("name"), "id")[: CONNECTORS_MAX + 1]
    )
    shown, truncated = capped(connectors, CONNECTORS_MAX)
    runs = ConnectorRun.objects.filter(organization=org, connector__in=[c.id for c in shown])
    active_states = [RunStatus.QUEUED, RunStatus.RUNNING]
    ended = runs.exclude(status__in=active_states).order_by("connector_id", "-created_at").distinct("connector_id")
    last = {r.connector_id: r for r in ended}
    active = {r.connector_id: r for r in runs.filter(status__in=active_states).order_by("connector_id", "created_at")}

    def run(r) -> dict[str, Any] | None:
        if r is None:
            return None
        return {
            "id": str(r.id),
            "status": r.status,
            "trigger": r.trigger,
            "queued_at": timestamp(r.queued_at),
            "started_at": timestamp(r.started_at),
            "finished_at": timestamp(r.finished_at),
            "error": r.error or None,  # Softwarden's words; never a secret (discovery/serializers.RunSerializer)
        }

    # The sources this organization has the capability of; a connector of another (an edition without it, or a preview
    # source while the installation keeps them off) is kept but never runs (GET connectors `available`).
    available = {s for s, cap in caps.SOURCE_CAPABILITY.items() if caps.has_capability(org, cap)}
    empty = dict.fromkeys(engine.COUNTED, 0)
    totals = dict(empty)
    for per_connector in counts.values():  # summed here rather than a second query: the same rows, counted once
        for s in engine.COUNTED:
            totals[s] += per_connector[s]
    return {
        "as_of": today.isoformat(),
        "counts": totals,
        "connectors_total": len(connectors) if not truncated else Connector.objects.filter(organization=org).count(),
        "truncated": truncated,
        "connectors": [
            {
                "id": str(c.id),
                "name": c.name,
                "source": c.source,
                "enabled": c.enabled,
                "available": c.source in available,
                "schedule": c.schedule,
                "path": f"connectors/{c.id}",
                "reviewer": user(c.reviewer),
                "counts": counts.get(c.id, dict(empty)),
                "last_run": run(last.get(c.id)),
                "active_run": run(active.get(c.id)),
            }
            for c in shown
        ],
    }


# ---- data gaps -------------------------------------------------------------------------------------------------------


def data_gaps(org, today: date, rule: quality.Rule | None, kind: str | None, limit: int) -> dict[str, Any]:
    """The data-quality summary (GET data-quality/summary's numbers) and the live records failing `rule` (or any rule
    that is on), by name, each with the checks it fails and its owners. `count` is every such record (the total GET
    data-quality/failures pages through with the same rule and kind)."""
    if not caps.has_capability(org, caps.DATA_QUALITY):
        raise CapabilityRequired(caps.DATA_QUALITY)
    if kind == Kind.AI_AGENT and not caps.has_capability(org, caps.AI_AGENTS):
        raise CapabilityRequired(caps.AI_AGENTS)
    summary = quality.summary_json(org, today)
    qs = Resource.objects.filter(organization=org).filter(quality.failing_records(org, rule, today))
    if kind:
        qs = qs.filter(kind=kind)
    count = qs.count()
    rows = list(qs.select_related(*OWNER_RELATED).order_by(Lower("name"), "id")[:limit])
    results = {rid: quality_json(found) for rid, found in quality.evaluate(org, [r.id for r in rows], today).items()}
    return {
        "as_of": summary["as_of"],
        "score": summary["score"],
        "records": summary["records"],
        "complete": summary["complete"],
        "by_rule": [
            {k: r[k] for k in ("id", "label", "severity", "records", "failing", "excepted")} for r in summary["by_rule"]
        ],
        "rule": rule.id if rule else None,
        "kind": kind,
        "count": count,
        "truncated": count > len(rows),
        "results": [
            {
                **record_ref(r),
                "criticality": r.criticality or None,
                "score": results[r.id]["score"],
                "failing": results[r.id]["failing"],  # most severe first, as on the record and the failures list
                **record_owners(r),
            }
            for r in rows
        ],
    }


# ---- expiring credentials --------------------------------------------------------------------------------------------


def expiring_credentials(org, membership, today: date, within_days: int, kind: str | None, limit: int):
    """money/reporting.expiring_credentials (the dashboard block: API tokens, an Admin's all of the organization's and
    others' their own, and the live discovered client secrets and certificates), ending within `within_days` or ended
    in the last `within_days`, soonest first, with the owners of the record a discovered credential's app is linked
    to. With within_days=30 these are the dashboard's rows. Never key material, a hash or a secret."""
    found = reporting.expiring_credentials(org, membership.user, membership.role, today, within_days)["rows"]
    if kind:
        found = [r for r in found if r["kind"] == kind]
    shown, truncated = capped(found, limit)
    records = load_records(org, [r["record"]["id"] for r in shown if r["record"]])
    out = []
    for r in shown:
        linked = records.get(r["record"]["id"]) if r["record"] else None
        out.append(
            {
                "kind": r["kind"],
                "id": r["id"],
                "name": r["name"],
                "prefix": r["prefix"] or None,
                "credential_type": r["credential_type"],
                "key_id": r["key_id"],
                "expires_on": r["expires_on"],
                "days": r["days"],
                "expired": r["expired"],
                "owner": r["owner"],
                "application": r["application"],
                "record": {**record_ref(linked), **record_owners(linked)} if linked else None,
            }
        )
    return {
        "as_of": today.isoformat(),
        "within_days": within_days,
        "count": len(found),
        "truncated": truncated,
        "results": out,
    }
