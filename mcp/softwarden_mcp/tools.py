"""The tools, as plain functions of a SoftwardenClient: each returns an `Answer`, a compact text for the model to read
and the same facts as structured data. Kept small on purpose (a model's context is the budget): references are names
(with an id where a follow-up call needs one), empty values are left out, and lists are capped with a count.

Data sources (docs/MCP.md "Tools"): the composite namespace (GET /api/v1/composite/..., the API's compact contract
for scripts and this server) wherever it has the answer; record detail routes for get_item; the AI agent list's
`model` and `provider` filters for agents_by_model; reminders and discovery candidates for the write tools' IDs.

Read tools never change anything. Write tools are separate (WRITE_TOOLS) and only registered with MCP_ENABLE_WRITES;
each sends If-Match with the revision the caller read, so nothing changes if someone edited the item since."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from .client import REVISION, SoftwardenClient, SoftwardenError

LIST_MAX = 200  # the composite and list routes' largest `limit` / `page_size`
RENEWAL_DAYS_MAX = 730
MODELS_MATCHED_MAX = 5  # agents_by_model: models a name matches, each listed with its agents

# get_item's kinds -> the detail route under /api/v1/ (the SPA uses the same path).
ITEM_PATHS: dict[str, str] = {
    "application": "applications",
    "component": "components",
    "ai_agent": "ai-agents",
    "integration": "integrations",
    "contract": "contracts",
    "renewal": "renewals",
    "license_pool": "license-pools",
    "vendor": "vendors",
    "person": "people",
    "team": "teams",
    "reminder": "reminders",
    "candidate": "discovery/observations",
}
ItemKind = Literal[
    "application",
    "component",
    "ai_agent",
    "integration",
    "contract",
    "renewal",
    "license_pool",
    "vendor",
    "person",
    "team",
    "reminder",
    "candidate",
]
RecordKind = Literal["application", "component", "ai_agent"]
EditableField = Literal[
    "description",
    "purpose",
    "lifecycle",
    "criticality",
    "business_owner",
    "technical_owner",
    "support_team",
    "it_steward",
    "cost_center",
    "data_classification",
]
EDITABLE_HELP = {
    "description": "free text",
    "purpose": "free text",
    "lifecycle": "proposed, evaluating, active, deprecated or retired",
    "criticality": "low, medium, high or critical; empty clears it",
    "business_owner": "a person's ID (search kinds=person); empty clears it",
    "technical_owner": "a person's ID; empty clears it",
    "support_team": "a team's ID; empty clears it",
    "it_steward": "an Editor or Admin user's numeric ID; empty clears it",
    "cost_center": "free text, up to 50 characters",
    "data_classification": "the organization's classification value; empty clears it",
}
NULLABLE = {"criticality", "business_owner", "technical_owner", "support_team", "it_steward"}


UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass
class Answer:
    text: str
    data: dict[str, Any]


# ---- shared shaping --------------------------------------------------------------------------------------------------


def uuid_id(value: str, what: str = "The ID") -> str:
    """A Softwarden ID (a UUID) as given by search or a list, or a refusal before any request is made: the value
    becomes part of a URL path, so nothing else (`..`, `/`, `?`) may get through."""
    found = (value or "").strip()
    if not UUID.match(found):
        raise SoftwardenError(f"{what} must be a Softwarden ID (a UUID like 3f2a1b0c-9d8e-4f7a-8b6c-5d4e3f2a1b0c).")
    return found.lower()


def item_route(kind: str, item_id: str, *action: str) -> tuple[str, ...]:
    """The detail route of an item (or an action under it) as path segments; the client quotes each one."""
    if kind not in ITEM_PATHS:
        raise SoftwardenError(f"Unknown kind {kind!r}. Use one of: {', '.join(ITEM_PATHS)}.")
    return (*ITEM_PATHS[kind].split("/"), uuid_id(item_id), *action)


def q(value: Any) -> str:
    """Recorded text (a name, a title, a source's value) quoted as a JSON string, so it reads as data, never as part
    of the answer's own wording or as instructions."""
    return json.dumps(str(value), ensure_ascii=False)


def _by(provider: Any) -> str:
    """` ("OpenAI")` after a model's name, or nothing for a model with no provider, never `("None")`."""
    return f" ({q(provider)})" if provider else ""


def quoted(values: Iterable[Any]) -> str:
    return ", ".join(q(v) for v in values)


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def compact(value: Any) -> Any:
    """Drop None, empty strings, lists and objects at any depth (they cost tokens and say nothing)."""
    if isinstance(value, Mapping):
        out = {k: compact(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [compact(v) for v in value if v not in (None, "", [], {})]
    return value


def compact_row(value: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = compact(value)
    return out


def who(person: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A person, user account or team as {name, email}, marked inactive when they have left."""
    if not person:
        return None
    out: dict[str, Any] = {"name": person.get("name")}
    if person.get("email"):
        out["email"] = person["email"]
    if person.get("active") is False:
        out["inactive"] = True
    return out


def who_text(person: Mapping[str, Any] | None) -> str:
    if not person:
        return "nobody"
    text = q(person.get("name") or "unnamed")
    if person.get("email"):
        text += f" <{person['email']}>"
    if person.get("inactive") or person.get("active") is False:
        text += " (inactive)"
    return text


def names(items: Iterable[Mapping[str, Any]] | None) -> list[str]:
    return [str(i.get("name")) for i in items or [] if i.get("name")]


def days_until(day: str | None, as_of: str) -> int | None:
    if not day:
        return None
    return (date.fromisoformat(day) - date.fromisoformat(as_of)).days


def in_days(days: int | None) -> str:
    if days is None:
        return ""
    if days == 0:
        return "today"
    return f"in {days} days" if days > 0 else f"{-days} days ago"


def more(count: int, shown: int, noun: str) -> str:
    return f" Showing {shown} of {count} {noun}; narrow the request to see the rest." if count > shown else ""


def results_page(client: SoftwardenClient, path: str, limit: int, **params: Any) -> list[dict[str, Any]]:
    """A keyset list's rows, following `next` until `limit` (lists answer {results, next, previous})."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(rows) < limit:
        page = client.get(path, page_size=min(limit, LIST_MAX), cursor=cursor, **params).data
        rows.extend(page.get("results", []))
        cursor = page.get("next")
        if not cursor:
            break
    return rows[:limit]


# ---- read tools ------------------------------------------------------------------------------------------------------


def search(
    client: SoftwardenClient,
    query: str,
    kinds: list[str] | None = None,
    limit: int = 10,
    include_archived: bool = False,
) -> Answer:
    data = client.get(
        "composite/search",
        q=query,
        kinds=",".join(kinds) if kinds else None,
        limit=clamp(limit, 1, 100),
        include_archived="true" if include_archived else None,
    ).data
    rows = [
        compact(
            {
                "kind": r["kind"],
                "id": r["id"],
                "name": r["name"],
                "lifecycle": r.get("lifecycle"),
                "archived": r.get("archived") or None,
                "matched": (r.get("snippet") or {}).get("text"),
                "parent": (r.get("parent") or {}).get("name") if isinstance(r.get("parent"), dict) else r.get("parent"),
                "path": r.get("path"),
            }
        )
        for r in data["results"]
    ]
    lines = [f"{data['count']} matches for {query!r}" + (" (fuzzy)" if data.get("fuzzy") else "") + "."]
    for r in rows:
        extra = ", ".join(x for x in (r.get("lifecycle"), "archived" if r.get("archived") else None) if x)
        line = f"- {r['kind']} {q(r['name'])} (id {r['id']}{', ' + extra if extra else ''})"
        if r.get("parent"):
            line += f" on {q(r['parent'])}"
        if r.get("matched"):
            line += f", matched {q(r['matched'])}"
        lines.append(line)
    if data.get("truncated"):
        lines.append("More matched than shown; raise limit or name kinds.")
    return Answer("\n".join(lines), {"query": query, "count": data["count"], "results": rows})


def get_item(client: SoftwardenClient, kind: str, item_id: str) -> Answer:
    page = client.get(item_route(kind, item_id))
    record = compact(page.data)
    title = record.get("name") or record.get("title") or record.get("label") or item_id
    text = f"{kind} {q(title)} (id {record.get('id', item_id)})"
    if page.etag:
        text += f", revision {page.etag}"
    text += "\n" + json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
    return Answer(text, {"kind": kind, "revision": page.etag, "item": record})


def _renewal_row(r: Mapping[str, Any], as_of: str) -> dict[str, Any]:
    owners = r.get("owners") or {}
    vendor = r.get("vendor") or {}
    return compact_row(
        {
            "source": r["source"],
            "id": r["id"],
            "name": r["name"],
            "path": r.get("path"),
            "term_ends": r.get("term_ends"),
            "days_to_term_end": days_until(r.get("term_ends"), as_of),
            "action": r.get("action"),
            "action_deadline": r.get("date"),
            "days_to_deadline": r.get("days"),
            "overdue": r.get("overdue") or None,
            "auto_renew": r.get("auto_renew"),
            "decision": r.get("decision"),
            "vendor": vendor.get("name") or r.get("vendor_name"),
            "vendor_contact": who(vendor.get("account_manager")) or vendor.get("support_email"),
            "contract": (r.get("contract") or {}).get("name"),
            "annualized_cost": r.get("annualized_cost"),
            "currency": r.get("currency"),
            "renewal_owner": who(owners.get("renewal_owner")),
            "contract_owner": who(owners.get("contract_owner")),
            "follow_up": who(owners.get("follow_up")),
            "business_owners": names(owners.get("business_owners")),
            "technical_owners": names(owners.get("technical_owners")),
            "it_stewards": names(owners.get("it_stewards")),
            "support_teams": names(owners.get("support_teams")),
            "covers": names(r.get("records")),
            "covers_total": r.get("records_total"),
            "gaps": r.get("gaps"),
        }
    )


def _renewal_line(row: Mapping[str, Any]) -> str:
    kind = "contract" if row["source"] == "contract" else "renewal"
    parts = [f"- {q(row['name'])} ({kind}" + (f", vendor {q(row['vendor'])}" if row.get("vendor") else "") + ")"]
    if row.get("term_ends"):
        parts.append(f"term ends {row['term_ends']} ({in_days(row.get('days_to_term_end'))})")
    else:
        parts.append("no term end recorded")
    action = "cancel by" if row.get("action") == "cancel_by" else "decide by"
    deadline = f"{action} {row.get('action_deadline')} ({in_days(row.get('days_to_deadline'))})"
    parts.append(deadline + (" OVERDUE" if row.get("overdue") else ""))
    owner = row.get("renewal_owner") or row.get("contract_owner")
    role = "renewal owner" if row.get("renewal_owner") else "contract owner"
    parts.append(f"owner: {who_text(owner)} ({role})" if owner else "owner: none named")
    if row.get("renewal_owner") and row.get("contract_owner") and row["contract_owner"] != row["renewal_owner"]:
        parts.append(f"contract owner: {who_text(row['contract_owner'])}")
    if row.get("follow_up"):
        parts.append(f"IT follow-up: {who_text(row['follow_up'])}")
    if row.get("business_owners"):
        parts.append("business owners: " + quoted(row["business_owners"]))
    if row.get("covers"):
        parts.append("covers: " + quoted(row["covers"]))
    if row.get("annualized_cost"):
        parts.append(f"{row['annualized_cost']} {row.get('currency') or ''}/year".replace(" /", "/"))
    if row.get("gaps"):
        parts.append("gaps: " + ", ".join(row["gaps"]))
    return "; ".join(parts)


def renewals_with_owners(
    client: SoftwardenClient,
    within_days: int = 90,
    date_basis: Literal["term_end", "action_deadline"] = "term_end",
    limit: int = 50,
) -> Answer:
    """GET composite/renewals with `basis`: term_end (the default, "what renews in the next N days") has Softwarden
    keep the open actions whose term (a renewal's renewal date, a contract's end date) ends within N days or has
    already ended, whenever their deadline falls (an entered deadline can be after the renewal date), soonest term
    end first; action_deadline keeps those whose cancel-or-decide day is within N days or overdue. Softwarden filters
    and counts, so `count` is every match even when only the first `limit` rows come back."""
    days = clamp(within_days, 1, RENEWAL_DAYS_MAX)
    limit = clamp(limit, 1, LIST_MAX)
    data = client.get("composite/renewals", within_days=days, limit=limit, basis=date_basis).data
    as_of = data["as_of"]
    rows = [_renewal_row(r, as_of) for r in data["results"]]
    count, truncated = int(data["count"]), bool(data.get("truncated"))
    until = date.fromordinal(date.fromisoformat(as_of).toordinal() + days).isoformat()
    if date_basis == "term_end":
        ended = any((r.get("days_to_term_end") or 0) < 0 for r in rows)
        head = (
            f"{count} renewals with a term ending by {until} (the next {days} days from {as_of})"
            + (" or already ended with the action still open" if ended else "")
            + ", filtered on the term end (a renewal's renewal date or a contract's end date), whenever the cancel "
            "or decide date falls. Renewals with no term end recorded aren't in this view (ask with "
            "date_basis=action_deadline)."
        )
    else:
        head = (
            f"{count} open renewal actions due by {until} (the next {days} days from {as_of}) or overdue, "
            "filtered on the action deadline (the day to cancel or decide by), not the term end."
        )
    if truncated:
        first = "ended terms" if date_basis == "term_end" else "overdue ones"
        head += (
            f" Softwarden counts {count} in all; only the first {len(rows)} are shown ({first} come first and count "
            "toward that limit). Ask for fewer days or a higher limit (at most 200) to see the rest."
        )
    lines = [head, *(_renewal_line(r) for r in rows)]
    if not rows:
        lines.append("Nothing found.")
    return Answer(
        "\n".join(lines),
        {
            "as_of": as_of,
            "within_days": days,
            "until": until,
            "date_basis": date_basis,
            "count": count,
            "shown": len(rows),
            "truncated": truncated,
            "results": rows,
        },
    )


def dependencies(
    client: SoftwardenClient,
    record_id: str,
    direction: Literal["both", "depends_on", "dependents"] = "both",
    depth: int = 2,
    include_retired: bool = False,
    limit: int = 50,
) -> Answer:
    data = client.get(
        "composite/dependencies",
        target=uuid_id(record_id, "record_id"),
        direction=direction,
        depth=clamp(depth, 1, 10),
        include_retired="true" if include_retired else None,
        limit=clamp(limit, 1, LIST_MAX),
    ).data

    def node(n: Mapping[str, Any]) -> dict[str, Any]:
        return compact_row(
            {
                "kind": n["kind"],
                "id": n["id"],
                "name": n["name"],
                "hops": n.get("hops"),
                "via": n.get("via"),
                "through": n.get("through"),
                "lifecycle": n.get("lifecycle"),
                "link_state": n.get("link_state"),
                "technical_owner": (n.get("technical_owner") or {}).get("name"),
            }
        )

    target = data["target"]
    out = {
        "target": compact({"kind": target["kind"], "id": target["id"], "name": target["name"]}),
        "depends_on": [node(n) for n in data.get("depends_on") or []],
        "dependents": [node(n) for n in data.get("dependents") or []],
        "data_flows": [
            compact(
                {
                    "name": f["name"],
                    "source": f["source"]["name"],
                    "target": f["target"]["name"],
                    "protocol": f.get("protocol"),
                }
            )
            for f in data.get("data_flows") or []
        ],
        "count": data.get("count"),
        "truncated": data.get("truncated"),
    }

    def listed(rows: list[dict[str, Any]]) -> str:
        return "; ".join(
            f"{q(r['name'])} ({r['kind']}, {r.get('hops', '?')} hop{'s' if r.get('hops') != 1 else ''}"
            + (f", via {q(r['via'])}" if r.get("via") else "")
            + (f", through {' > '.join(q(t) for t in r['through'])}" if r.get("through") else "")
            + ")"
            for r in rows
        )

    lines = [f"Dependencies of {q(target['name'])} ({target['kind']}), depth {data.get('depth')}:"]
    if direction in ("both", "depends_on"):
        lines.append("Depends on: " + (listed(out["depends_on"]) or "nothing recorded."))
    if direction in ("both", "dependents"):
        lines.append("Depended on by: " + (listed(out["dependents"]) or "nothing recorded."))
    if out["data_flows"]:
        lines.append(
            "Data flows: "
            + "; ".join(f"{q(f['name'])} ({q(f['source'])} -> {q(f['target'])})" for f in out["data_flows"])
        )
    if data.get("truncated"):
        lines.append("Some links were cut by the depth or limit.")
    return Answer("\n".join(lines), out)


def spend(
    client: SoftwardenClient,
    by: Literal["vendor", "department", "category", "cost_center"] = "vendor",
    groups: int = 10,
) -> Answer:
    data = client.get("composite/spend", by=by, groups=clamp(groups, 1, LIST_MAX)).data
    totals = [
        compact({"currency": t["currency"], "annualized": t.get("annualized"), "contracts": t.get("contracts")})
        for t in data.get("totals") or []
    ]
    rows = [
        compact(
            {
                "label": g.get("label") or g.get("gap"),
                "gap": g.get("gap"),
                "currency": g["currency"],
                "amount": g["amount"],
                "contracts": g.get("contracts"),
            }
        )
        for g in data.get("groups") or []
    ]
    lines = [f"Annualized spend of live contracts, by {data['by']} (as of {data['as_of']}; currencies never mixed):"]
    lines += [f"Total {t.get('annualized')} {t['currency']} across {t.get('contracts', 0)} contracts" for t in totals]
    lines += [
        f"- {('gap ' + r['gap']) if r.get('gap') else q(r['label'])}: {r['amount']} {r['currency']} "
        f"({r.get('contracts', 0)} contracts)"
        for r in rows
    ]
    if not totals:
        lines.append("No live contracts with a cost.")
    return Answer("\n".join(lines), {"as_of": data["as_of"], "by": data["by"], "totals": totals, "groups": rows})


def expiring_credentials(
    client: SoftwardenClient,
    within_days: int = 30,
    kind: Literal["api_token", "discovered_credential"] | None = None,
    limit: int = 50,
) -> Answer:
    data = client.get(
        "composite/expiring-credentials",
        within_days=clamp(within_days, 1, 365),
        kind=kind,
        limit=clamp(limit, 1, LIST_MAX),
    ).data
    rows = []
    for c in data["results"]:
        record = c.get("record") or {}
        rows.append(
            compact(
                {
                    "kind": c["kind"],
                    "name": c.get("name"),
                    "credential_type": c.get("credential_type"),
                    "expires_on": c.get("expires_on"),
                    "days": c.get("days"),
                    "expired": c.get("expired") or None,
                    "application": (c.get("application") or {}).get("name"),
                    "token_owner": (c.get("owner") or {}).get("name") if isinstance(c.get("owner"), dict) else None,
                    "record": record.get("name"),
                    "business_owner": who(record.get("business_owner")),
                    "technical_owner": who(record.get("technical_owner")),
                }
            )
        )
    lines = [f"{data['count']} credentials ending within {data['within_days']} days or ended in the last as many:"]
    for r in rows:
        what = "API token" if r["kind"] == "api_token" else (r.get("credential_type") or "credential")
        line = f"- {what} {q(r.get('name', ''))}"
        if r.get("application"):
            line += f" on app {q(r['application'])}"
        line += f" {'expired' if r.get('expired') else 'expires'} {r.get('expires_on')} ({in_days(r.get('days'))})"
        if r.get("record"):
            line += f"; record {q(r['record'])}, technical owner {who_text(r.get('technical_owner'))}"
        if r.get("token_owner"):
            line += f"; owner {q(r['token_owner'])}"
        lines.append(line)
    lines[0] += more(data["count"], len(rows), "credentials")
    return Answer("\n".join(lines), {"as_of": data["as_of"], "count": data["count"], "results": rows})


def data_gaps(
    client: SoftwardenClient,
    rule: str | None = None,
    kind: Literal["application", "component", "ai_agent", "integration"] | None = None,
    limit: int = 25,
) -> Answer:
    data = client.get("composite/data-gaps", rule=rule, kind=kind, limit=clamp(limit, 1, LIST_MAX)).data
    rules = [compact({k: r.get(k) for k in ("id", "label", "severity", "failing")}) for r in data.get("by_rule") or []]
    rows = [
        compact(
            {
                "kind": r["kind"],
                "id": r["id"],
                "name": r["name"],
                "score": r.get("score"),
                "failing": [f.get("label") for f in r.get("failing") or []],
                "business_owner": (r.get("business_owner") or {}).get("name"),
                "it_steward": (r.get("it_steward") or {}).get("name"),
            }
        )
        for r in data["results"]
    ]
    lines = [
        f"Data quality score {data.get('score')} ({data.get('complete')} of {data.get('records')} records complete,"
        f" as of {data['as_of']}).",
        "Failing per check: "
        + ("; ".join(f"{r.get('label')}: {r.get('failing', 0)}" for r in rules if r.get("failing")) or "none."),
        f"{data['count']} records fail " + (f"the check {data['rule']}" if data.get("rule") else "a check") + ":",
    ]
    lines += [f"- {r['kind']} {q(r['name'])} (id {r['id']}): {', '.join(r.get('failing', []))}" for r in rows]
    lines[2] += more(data["count"], len(rows), "records")
    return Answer(
        "\n".join(lines),
        {"as_of": data["as_of"], "score": data.get("score"), "by_rule": rules, "count": data["count"], "results": rows},
    )


def _agent(a: Mapping[str, Any]) -> dict[str, Any]:
    return compact_row(
        {
            "id": a["id"],
            "name": a["name"],
            "lifecycle": a.get("lifecycle"),
            "platform": a.get("platform"),
            "models": [
                f"{m.get('name')}{' ' + m['version'] if m.get('version') else ''} ({(m.get('provider') or {}).get('name')})"
                for m in a.get("models_used") or []
            ],
            "risk": a.get("risk_classification"),
            "business_owner": (a.get("business_owner") or {}).get("name"),
            "technical_owner": (a.get("technical_owner") or {}).get("name"),
            "gaps": a.get("gaps"),
        }
    )


def agents_by_model(
    client: SoftwardenClient, model: str | None = None, provider: str | None = None, limit: int = 50
) -> Answer:
    """No arguments: every model in use with its agent count (GET ai-models/in-use). `model`: models whose name or
    version contains the text (GET ai-models?q=), each with the agents using it (GET ai-agents?model=). `provider`:
    providers whose name contains the text, with the agents using any of their models (GET ai-agents?provider=)."""
    limit = clamp(limit, 1, LIST_MAX)
    model = (model or "").strip()
    provider = (provider or "").strip()
    if not model and not provider:
        used = client.get("ai-models/in-use").data
        rows = [
            compact(
                {
                    "id": m["id"],
                    "model": m["name"],
                    "version": m.get("version"),
                    "provider": (m.get("provider") or {}).get("name"),
                    "agents": m.get("agents"),
                    "live_agents": m.get("live_agents"),
                }
            )
            for m in used
        ]
        lines = [f"{len(rows)} AI models in use by agents (most used first):"]
        lines += [
            f"- {q(r['model'] + (' ' + r['version'] if r.get('version') else ''))}{_by(r.get('provider'))}: "
            f"{r.get('agents', 0)} agents, {r.get('live_agents', 0)} live"
            for r in rows
        ]
        lines.append("Ask with model or provider to list the agents.")
        return Answer("\n".join(lines), {"models": rows})

    groups: list[dict[str, Any]] = []
    if model:
        found = client.get("ai-models", q=model).data
        if provider:
            found = [m for m in found if provider.casefold() in str((m.get("provider") or {}).get("name")).casefold()]
        for m in found[:MODELS_MATCHED_MAX]:
            agents = results_page(client, "ai-agents", limit, model=m["id"])
            label = f"{m['name']}{' ' + m['version'] if m.get('version') else ''}"
            groups.append(
                {
                    "model": label,
                    "provider": (m.get("provider") or {}).get("name"),
                    "id": m["id"],
                    "agents": [_agent(a) for a in agents],
                }
            )
        matched = len(found)
    else:
        found = [p for p in client.get("ai-providers").data if provider.casefold() in str(p.get("name")).casefold()]
        for p in found[:MODELS_MATCHED_MAX]:
            agents = results_page(client, "ai-agents", limit, provider=p["id"])
            groups.append({"provider": p["name"], "id": p["id"], "agents": [_agent(a) for a in agents]})
        matched = len(found)
    noun = "models" if model else "providers"
    if not groups:
        return Answer(
            f"No AI {noun} match {model or provider!r}. Call agents_by_model with no arguments to see the models in use.",
            {"matched": 0, "groups": []},
        )
    lines = [
        f"{matched} {noun} match {model or provider!r}"
        + (f"; the first {MODELS_MATCHED_MAX}:" if matched > MODELS_MATCHED_MAX else ":")
    ]
    for g in groups:
        title = q(g.get("model") or g["provider"])
        if g.get("model"):
            title += _by(g.get("provider"))
        lines.append(f"{title}: {len(g['agents'])} agents" + (" (limit reached)" if len(g["agents"]) >= limit else ""))
        for a in g["agents"]:
            owner = a.get("business_owner") or a.get("technical_owner")
            lines.append(
                f"- {q(a['name'])} (id {a['id']}, {a.get('lifecycle', '?')}"
                + (f", {a['platform']}" if a.get("platform") else "")
                + (f", owner {q(owner)})" if owner else ", no owner)")
            )
    return Answer("\n".join(lines), {"matched": matched, "groups": groups})


def discovery_summary(client: SoftwardenClient) -> Answer:
    data = client.get("composite/discovery-summary").data
    counts = data.get("counts") or {}
    connectors = [
        compact(
            {
                "name": c["name"],
                "source": c.get("source"),
                "enabled": c.get("enabled"),
                "candidates": (c.get("counts") or {}).get("candidates"),
                "last_run": compact({k: (c.get("last_run") or {}).get(k) for k in ("status", "finished_at", "error")}),
                "running": bool(c.get("active_run")) or None,
            }
        )
        for c in data.get("connectors") or []
    ]
    lines = [
        f"Discovery (as of {data['as_of']}): {counts.get('candidates', 0)} candidates to decide "
        f"({counts.get('new', 0)} new, {counts.get('changed', 0)} changed), {counts.get('missing', 0)} linked records "
        f"no longer seen, {counts.get('differs', 0)} with a source difference."
    ]
    for c in connectors:
        last = c.get("last_run") or {}
        lines.append(
            f"- {q(c['name'])} ({c.get('source')}{'' if c.get('enabled') else ', off'}): {c.get('candidates', 0)} "
            f"candidates; last run {last.get('status', 'never')}" + (" ; running now" if c.get("running") else "")
        )
    return Answer("\n".join(lines), {"as_of": data["as_of"], "counts": counts, "connectors": connectors})


# The reminder statuses Softwarden's reminder list filters on (notify.models.ReminderStatus); list_reminders describes
# these and apps/api/tests/test_mcp_e2e.py calls each against the real API.
REMINDER_STATUSES = ("open", "snoozed", "acknowledged", "done", "obsolete")


def list_reminders(client: SoftwardenClient, status: str = "open,snoozed", limit: int = 25) -> Answer:
    rows = [
        compact(
            {
                "id": r["id"],
                "title": r.get("title"),
                "label": r.get("label"),
                "due_date": r.get("due_date"),
                "status": r.get("status"),
                "snoozed_until": r.get("snoozed_until"),
                "source_type": r.get("source_type"),
                "steward": (r.get("steward") or {}).get("name"),
            }
        )
        for r in results_page(client, "reminders", clamp(limit, 1, LIST_MAX), status=status, ordering="due_date")
    ]
    lines = [f"{len(rows)} reminders ({status}), soonest first:"]
    lines += [
        f"- {q(r.get('label'))}: {q(r.get('title'))} due {r.get('due_date')} [{r.get('status')}] (id {r['id']})"
        for r in rows
    ]
    return Answer("\n".join(lines), {"status": status, "results": rows})


def list_candidates(
    client: SoftwardenClient, status: str = "candidates", q_: str | None = None, limit: int = 25
) -> Answer:
    """GET discovery/candidates, following its keyset `next_after` (50 a page, stable while candidates are
    decided, where the offset `next` skips a row) until `limit`; says when there are more."""
    limit = clamp(limit, 1, LIST_MAX)
    found: list[dict[str, Any]] = []
    cursor: str | None = None
    counts: dict[str, Any] = {}
    while True:
        data = client.get("discovery/candidates", status=status, q=q_, after=cursor).data
        counts = data.get("counts") or counts
        found.extend(data.get("results") or [])
        cursor = data.get("next_after")
        if not cursor or len(found) >= limit:
            break
    more_left = bool(cursor) or len(found) > limit
    rows = [
        compact_row(
            {
                "id": o["id"],
                "name": o.get("name"),
                "kind": o.get("kind"),
                "source": o.get("source"),
                "connector": (o.get("connector") or {}).get("name"),
                "status": o.get("status"),
                "ai_agent": o.get("ai_agent") or None,
                "first_seen_at": o.get("first_seen_at"),
                "suggested_match": ((o.get("suggested_matches") or [{}])[0] or {}).get("name"),
            }
        )
        for o in found[:limit]
    ]
    lines = [f"Discovery candidates ({status}); counts: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v)]
    lines += [
        f"- {q(r.get('name'))} ({r.get('kind')}, from {q(r.get('connector') or r.get('source'))}) [{r.get('status')}] "
        f"(id {r['id']})" + (f"; looks like {q(r['suggested_match'])}" if r.get("suggested_match") else "")
        for r in rows
    ]
    if more_left:
        lines.append(f"More candidates than the {len(rows)} shown; raise limit or narrow with q.")
    return Answer("\n".join(lines), {"status": status, "counts": counts, "results": rows, "more": more_left})


# ---- write tools (MCP_ENABLE_WRITES only) ----------------------------------------------------------------------------


def _need_revision(revision: str) -> str:
    """The one revision get_item returned: letters, digits, - and _ only, so never `*` (any revision), a list of
    tags or a weak tag, any of which would let the write through regardless of edits since."""
    value = (revision or "").strip().strip('"')
    if not REVISION.match(value):
        raise SoftwardenError(
            "Pass the revision get_item returned, exactly as returned, so the change applies only if nobody edited "
            "it since."
        )
    return value


def update_record(
    client: SoftwardenClient, kind: RecordKind, record_id: str, field: EditableField, value: str | None, revision: str
) -> Answer:
    if field not in EDITABLE_HELP:
        raise SoftwardenError(f"{field!r} can't be changed here. Fields: {', '.join(EDITABLE_HELP)}.")
    if kind not in ("application", "component", "ai_agent"):
        raise SoftwardenError("update_record changes applications, components and AI agents.")
    new: Any = value
    if field in NULLABLE and (value is None or not str(value).strip()):
        new = None
    elif value is None:
        new = ""
    elif field == "it_steward":
        if not str(value).strip().isdigit():
            raise SoftwardenError("it_steward takes a user's numeric ID.")
        new = int(str(value).strip())
    route = item_route(kind, record_id)
    page = client.write("PATCH", route, {field: new}, revision=_need_revision(revision))
    record = page.data or {}
    shown = record.get(field)
    if isinstance(shown, dict):
        shown = shown.get("name")
    text = (
        f"Changed {field} of {kind} {q(record.get('name'))} to {json.dumps(shown, ensure_ascii=False)}. "
        f"New revision: {page.etag}. Audited in its History."
    )
    return Answer(text, {"kind": kind, "id": record.get("id"), "field": field, "value": shown, "revision": page.etag})


def snooze_reminder(client: SoftwardenClient, reminder_id: str, until: str, revision: str) -> Answer:
    try:
        date.fromisoformat(until)
    except ValueError:
        raise SoftwardenError("until is a date as YYYY-MM-DD, after today and at most 90 days ahead.") from None
    route = item_route("reminder", reminder_id, "snooze")
    page = client.write("POST", route, {"until": until}, revision=_need_revision(revision))
    r = page.data or {}
    text = (
        f"Snoozed {q(r.get('label'))} for {q(r.get('title'))} through {r.get('snoozed_until')}: no notifications until "
        f"then, for everyone it reaches; it stays in Needs attention. New revision: {page.etag}."
    )
    return Answer(
        text,
        {"id": r.get("id"), "status": r.get("status"), "snoozed_until": r.get("snoozed_until"), "revision": page.etag},
    )


def acknowledge_reminder(client: SoftwardenClient, reminder_id: str, revision: str) -> Answer:
    route = item_route("reminder", reminder_id, "acknowledge")
    page = client.write("POST", route, {}, revision=_need_revision(revision))
    r = page.data or {}
    text = f"Acknowledged {q(r.get('label'))} for {q(r.get('title'))}: its advance notices stop. New revision: {page.etag}."
    return Answer(text, {"id": r.get("id"), "status": r.get("status"), "revision": page.etag})


def dismiss_candidate(client: SoftwardenClient, candidate_id: str, reason: str, revision: str) -> Answer:
    if not (reason or "").strip():
        raise SoftwardenError("Say why the candidate is dismissed (for example: test registration).")
    route = item_route("candidate", candidate_id, "dismiss")
    page = client.write("POST", route, {"reason": reason.strip()}, revision=_need_revision(revision))
    o = page.data or {}
    text = (
        f"Dismissed the discovery candidate {q(o.get('name'))} ({o.get('kind')}) with the reason {q(reason.strip())}. "
        "It stays dismissed until the source changes it; Reopen in the queue undoes this."
    )
    return Answer(text, {"id": o.get("id"), "status": o.get("status"), "revision": page.etag})
