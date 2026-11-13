"""Dependency traversal. Read-only.

The organization's dependency links (`depends_on`, and any added relationship type with `is_dependency`)
and the records they join are loaded in bulk, then walked in memory, so the
number of queries doesn't grow with the size of the graph. Every query filters on the organization, and a link whose
other end isn't one of the organization's records is dropped, so nothing from another organization can appear as a
node.

Rules:
- Breadth first from the target, so a node is reached by its shortest path and appears once (one path per node).
  Ties go to the node and link that sort first by name, then ID.
- Cycle-safe: an edge that leads back to a record on the current path (the target included) becomes a "cycle" row
  that points at that record and is not entered again. A record reached a second time by a different route is a
  diamond, not a cycle, and is skipped, unless the two records can reach each other: then the edge closes a loop
  between branches (A and B both depend on the target and on each other) and becomes a cycle row too, once per loop.
- Depth 1 to MAX_DEPTH, or None (all). Direction is "dependents" (what depends on the target), "depends_on" (what
  the target depends on) or "both"; the two passes are separate, so a record can appear in each.
- Retired records are left out, and not walked through, unless include_retired. The number left out is reported.
- Integrations are listed one hop out from the target and from each listed record (as source or target), with the
  components that implement them. They are data flows, never walked further.
- At most MAX_NODES rows (nodes and data flows together, nearest first) are returned; `total` is the full count.
- Every cycle row is also listed in `cycles` (the link that closes the loop, its ends and the pass), uncapped up to
  MAX_NODES, so a graph can mark the closing edges even when the row itself is past the cap; its `closes_at` and
  `reached_from` records may then be missing from `nodes` too.
- Record kinds the edition lacks (AI agents without `ai_agents`) are left out of the walk (record_map.hidden_kinds),
  as their routes would refuse; the target is always kept.

Callers: `GET /dependencies?target=` (the explorer) and `GET /<applications|components|ai-agents>/<id>/dependencies`
(catalog/viewsets.py) share `parse_options` and this module, so both answer the same for the same record.
"""

from django.db.models import Q, TextField
from django.db.models.functions import Cast

from core.input import whole_number

from . import record_map
from .links import latest_confirmations
from .models import RECORD_KINDS, ApplicationInstance, Kind, Lifecycle, Relationship, RelationshipKind, Resource
from .services import org_today
from .state import all_gaps, link_signature, link_state

MAX_NODES = 150
MAX_DEPTH = 10  # an explicit depth beyond this is refused; "all" has no limit (the walk is cycle-safe)
EDGE_KEYS = ("id", "source_id", "target_id", "source_instance_id", "target_instance_id", "type", "type_name")


def text(column):
    """The column as text, so an ID arrives as the string we key on without a UUID object being made for it."""
    return Cast(column, output_field=TextField())


DIRECTIONS = ("dependents", "depends_on", "both")


def person_ref(p):
    return p and {"id": str(p.id), "name": p.name, "active": p.active}


class Walk:
    """One pass in one direction. `adjacent[x]` lists (neighbor id, edge) in walking order."""

    def __init__(self, target_id, adjacent, lower_names, max_depth, include_retired, retired):
        self.target_id = target_id
        self.adjacent = adjacent
        self.lower_names = lower_names  # {id: lower-case name}, for a stable walking order
        self.max_depth = max_depth
        self.include_retired = include_retired
        self.retired = retired
        self.hidden_retired: set = set()
        self.rows: list[dict] = []  # {"id", "depth", "parent", "edge", "cycle"}
        self._parent: dict = {target_id: None}
        self._via: dict = {target_id: None}  # the edge a record was reached by
        self._on_path: dict = {target_id: {target_id}}  # the records on the path to each record, itself included
        self._component: dict | None = None  # strongly connected components, computed on the first cross edge
        self._looped: set = set()  # components that already have a cycle row

    def run(self):
        level = [self.target_id]
        depth = 0
        while level and (self.max_depth is None or depth < self.max_depth):
            depth += 1
            following = []
            for node in level:
                on_path = self._on_path[node]
                for neighbor, edge in self.adjacent.get(node, []):
                    if neighbor in on_path:
                        self._looped.add(self._component_of(neighbor))
                        self.rows.append({"id": neighbor, "depth": depth, "parent": node, "edge": edge, "cycle": True})
                    elif neighbor in self._parent:
                        # Reached already by another branch. A diamond is skipped; a loop between branches is shown.
                        component = self._component_of(node)
                        if component == self._component_of(neighbor) and component not in self._looped:
                            self._looped.add(component)
                            self.rows.append(
                                {"id": neighbor, "depth": depth, "parent": node, "edge": edge, "cycle": True}
                            )
                    elif neighbor in self.retired and not self.include_retired:
                        self.hidden_retired.add(neighbor)
                    else:
                        self._parent[neighbor] = node
                        self._via[neighbor] = edge
                        self._on_path[neighbor] = on_path | {neighbor}
                        self.rows.append({"id": neighbor, "depth": depth, "parent": node, "edge": edge, "cycle": False})
                        following.append(neighbor)
            level = sorted(following, key=lambda n: self.lower_names[n])
        return self

    def _component_of(self, node):
        if self._component is None:
            self._component = self._components()
        return self._component.get(node, node)

    def _components(self):
        """Tarjan's strongly connected components over the records this walk may enter (retired ones only when
        they're included), iteratively. Two records share a component when each can reach the other."""
        allowed = (lambda n: True) if self.include_retired else (lambda n: n == self.target_id or n not in self.retired)
        index: dict = {}
        low: dict = {}
        on_stack: set = set()
        stack: list = []
        component: dict = {}
        counter = 0
        for root in self.adjacent:
            if root in index or not allowed(root):
                continue
            work = [(root, iter(self.adjacent.get(root, [])))]
            index[root] = low[root] = counter
            counter += 1
            stack.append(root)
            on_stack.add(root)
            while work:
                node, neighbors = work[-1]
                advanced = False
                for neighbor, _edge in neighbors:
                    if not allowed(neighbor):
                        continue
                    if neighbor not in index:
                        index[neighbor] = low[neighbor] = counter
                        counter += 1
                        stack.append(neighbor)
                        on_stack.add(neighbor)
                        work.append((neighbor, iter(self.adjacent.get(neighbor, []))))
                        advanced = True
                        break
                    if neighbor in on_stack:
                        low[node] = min(low[node], index[neighbor])
                if advanced:
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component[member] = node
                        if member == node:
                            break
        return component

    def path(self, row):
        """(record ID, edge reached by) from the target to the row's record; a cycle row ends at the repeated record."""
        steps = []
        node = row["parent"]
        while node is not None:
            steps.append((node, self._via[node]))
            node = self._parent[node]
        steps.reverse()
        steps.append((row["id"], row["edge"]))
        return steps


def collect(org, target, direction, depth, include_retired, flows, hidden_kinds=()):
    """The walk itself, from light columns only: no cap and no detail. Returns a dict with `target_id`, `records`
    ({id: (name, lifecycle, technical owner ID)}), `passes` ({direction: Walk}), `nodes` (one per row of every pass,
    {"walk", "row", "key"}), `flow_rows` (data flows as light dicts, nearest first) and `hidden_flows`. traverse()
    serves it capped and in full; the assessment snapshot (assessments.py) takes all of it. Records of `hidden_kinds`
    (kinds the edition lacks; traverse passes them) are left out of the walk, except the target itself."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {DIRECTIONS}")
    if target.organization_id != org.id or target.kind not in RECORD_KINDS:
        raise ValueError("The target must be a record (application, component or AI agent) of the organization.")

    # IDs are strings from here on: a UUID object hashes in Python code, a string's hash is cached, and the
    # walk does hundreds of thousands of lookups.
    target_id = str(target.id)
    records = {  # id -> (name, lifecycle, technical owner ID)
        rid: (name, lifecycle, owner_id)
        for rid, name, lifecycle, owner_id in Resource.objects.filter(organization=org, kind__in=RECORD_KINDS)
        .exclude(Q(kind__in=hidden_kinds) & ~Q(id=target.id))
        .values_list(text("id"), "name", "lifecycle", text("technical_owner_id"))
    }
    forward: dict = {}  # source -> [(target, edge)]: what a record depends on
    backward: dict = {}  # target -> [(source, edge)]: what depends on a record
    links = (
        Relationship.objects.filter(organization=org, type__is_dependency=True)
        .order_by("created_at", "id")
        .values_list(
            text("id"),
            text("source_id"),
            text("target_id"),
            text("source_instance_id"),
            text("target_instance_id"),
            "type__slug",
            "type__name",
        )
    )
    for row in links:
        link = dict(zip(EDGE_KEYS, row, strict=True))
        if link["source_id"] not in records or link["target_id"] not in records:
            continue  # not one of this organization's records; the database forbids it, and so do we
        forward.setdefault(link["source_id"], []).append((link["target_id"], link))
        backward.setdefault(link["target_id"], []).append((link["source_id"], link))
    lower = {rid: record[0].lower() for rid, record in records.items()}
    for adjacent in (forward, backward):
        for edges in adjacent.values():
            edges.sort(key=lambda e: lower[e[0]])  # stable: links with the same neighbor keep creation order
    retired = {rid for rid, (_, lifecycle, _) in records.items() if lifecycle == Lifecycle.RETIRED}

    passes = {}
    for name, adjacent in (("dependents", backward), ("depends_on", forward)):
        if direction in (name, "both"):
            passes[name] = Walk(target_id, adjacent, lower, depth, include_retired, retired).run()

    nodes = [  # one per row of every pass: {"walk", "row", "key"}; key sorts nearest first, then by name
        {"walk": name, "row": row, "key": (row["depth"], 0, lower[row["id"]], name != "dependents")}
        for name, walk in passes.items()
        for row in walk.rows
    ]
    # Integrations one hop from the target and from every record listed (not from a cycle row's repeat).
    listed = {target_id: 0}
    for n in nodes:
        if not n["row"]["cycle"]:
            listed.setdefault(n["row"]["id"], n["row"]["depth"])
    flow_rows, hidden_flows = _data_flows(org, listed, include_retired) if flows else ([], 0)
    return {
        "target_id": target_id,
        "records": records,
        "passes": passes,
        "nodes": nodes,
        "flow_rows": flow_rows,
        "hidden_flows": hidden_flows,
    }


def traverse(org, target, direction="dependents", depth=None, include_retired=False, show_instances=False, flows=True):
    """Everything within `depth` hops of target through links whose type has `is_dependency` (None = no limit), plus
    its data flows. target must be an application, component or AI agent of org. Returns a dict that the views serve as
    it is.

    Everything is decided from light columns first (names, lifecycles, owner IDs, link IDs), so counting, link state
    and the cap cost nothing per row beyond arithmetic; only the rows that are returned (at most MAX_NODES) are
    loaded in full."""
    found = collect(org, target, direction, depth, include_retired, flows, record_map.hidden_kinds(org))
    target_id, records, passes = found["target_id"], found["records"], found["passes"]
    nodes, flow_rows, hidden_flows = found["nodes"], found["flow_rows"], found["hidden_flows"]

    # Nearest first; a record's data flow comes after the records at the depth it hangs from. Then the cap.
    ordered = sorted(
        [(n["key"], i, "node") for i, n in enumerate(nodes)]
        + [((f["depth"], 1, f["name"].lower(), False), i, "flow") for i, f in enumerate(flow_rows)]
    )
    kept = ordered[:MAX_NODES]
    kept_nodes = [nodes[i] for _, i, kind in kept if kind == "node"]
    kept_flows = [flow_rows[i] for _, i, kind in kept if kind == "flow"]

    # Link state for the rows that are returned, from one confirmations query.
    today = org_today(org)
    confirmations = {
        (kind, str(link_id)): latest
        for (kind, link_id), latest in latest_confirmations(
            org, [n["row"]["edge"]["id"] for n in kept_nodes], [f["id"] for f in kept_flows]
        ).items()
    }

    def link_fields(signature, latest):
        state, why = link_state(signature, latest, today, org.review_interval_days)
        return {
            "state": state,
            "why": why,
            "last_confirmed_on": latest.confirmed_on.isoformat() if latest else None,
        }

    for n in kept_nodes:
        edge = n["row"]["edge"]
        signature = link_signature(
            edge["type"],
            edge["source_id"],
            edge["source_instance_id"],
            edge["target_id"],
            edge["target_instance_id"],
            records[edge["source_id"]][2],
        )
        n["link"] = link_fields(signature, confirmations.get(("relationship", edge["id"])))
    for f in kept_flows:
        signature = link_signature(
            "integration",
            f["source_resource_id"],
            f["source_instance_id"],
            f["target_resource_id"],
            f["target_instance_id"],
            f["owner_id"],
        )
        f["link"] = link_fields(signature, confirmations.get(("integration", f["id"])))

    def count(state):
        return sum(1 for x in (*kept_nodes, *kept_flows) if x["link"]["state"] == state)

    summary = {  # counts before the cap, except the two link counts, which are of the rows returned
        "nodes": len(nodes),
        "data_flows": len(flow_rows),
        "needs_verification": count("needs_verification"),
        "unconfirmed": count("unconfirmed"),
        "cycles": sum(1 for n in nodes if n["row"]["cycle"]),
        "hidden_retired": len(set().union(*(w.hidden_retired for w in passes.values()))) + hidden_flows,
    }

    # Full detail for the rows that are returned, in a few queries.
    wanted = {target_id} | {n["row"]["id"] for n in kept_nodes} | {f["attached_to"] for f in kept_flows}
    wanted |= {step for n in kept_nodes for step, _ in passes[n["walk"]].path(n["row"])}
    resources = {
        str(r.id): r
        for r in Resource.objects.filter(organization=org, id__in=wanted).select_related(
            "business_owner", "technical_owner", "support_team"
        )
    }
    instance_names = _instance_names(org, kept_nodes, kept_flows) if show_instances else {}

    def end(resource_id):
        r = resources[resource_id]
        return {"id": str(r.id), "name": r.name, "kind": r.kind}

    out_nodes = []
    for n in kept_nodes:
        row, edge = n["row"], n["row"]["edge"]
        far, near = ("source", "target") if n["walk"] == "dependents" else ("target", "source")
        steps = []
        for node_id, via in passes[n["walk"]].path(row):
            step = end(node_id)
            step["via"] = via and {"relationship": str(via["id"]), "type": via["type"], "type_name": via["type_name"]}
            if show_instances and via:
                step["instance"] = instance_names.get(via[f"{far}_instance_id"])
                step["previous_instance"] = instance_names.get(via[f"{near}_instance_id"])
            steps.append(step)
        r = resources[row["id"]]
        out_nodes.append(
            {
                "id": str(r.id),
                "kind": r.kind,
                "name": r.name,
                "lifecycle": r.lifecycle,
                "archived": r.archived_at is not None,
                "direction": n["walk"],
                "depth": row["depth"],
                "cycle": row["cycle"],
                "path": steps,
                "link": {"relationship": str(edge["id"]), **n["link"]},
                "technical_owner": person_ref(r.technical_owner),
                "gaps": all_gaps(r),
            }
        )
    return {
        "target": {**end(target_id), "lifecycle": target.lifecycle, "archived": target.archived_at is not None},
        "direction": direction,
        "depth": "all" if depth is None else str(depth),
        "include_retired": include_retired,
        "show_instances": show_instances,
        "nodes": out_nodes,
        "data_flows": _flow_json(org, kept_flows, end, show_instances, instance_names),
        "total": len(ordered),
        "shown": len(kept),
        "capped": len(ordered) > MAX_NODES,
        "summary": summary,
        "cycles": _cycles(nodes),
    }


def _cycles(nodes):
    """The links that close a loop, one per cycle row, nearest first (the order of `nodes`' keys), at most MAX_NODES.
    `source` and `target` are the link's own ends (source depends on target), `closes_at` the record already on the
    path that the link leads back to."""
    found = sorted((n for n in nodes if n["row"]["cycle"]), key=lambda n: n["key"])[:MAX_NODES]
    return [
        {
            "relationship": n["row"]["edge"]["id"],
            "type": n["row"]["edge"]["type"],
            "source": n["row"]["edge"]["source_id"],
            "target": n["row"]["edge"]["target_id"],
            "closes_at": n["row"]["id"],
            "reached_from": n["row"]["parent"],
            "direction": n["walk"],
            "depth": n["row"]["depth"],
        }
        for n in found
    ]


def parse_options(params):
    """The explorer's query parameters other than `target`, validated: (options for traverse(), errors). Shared by
    `/dependencies` and each record's `/dependencies` so both take exactly the same values."""
    errors: dict[str, list[str]] = {}
    direction = params.get("direction", "dependents")
    if direction not in DIRECTIONS:
        errors["direction"] = ["Choose dependents, depends_on or both."]
    raw = params.get("depth", "1")
    depth = None
    if raw != "all":
        depth = whole_number(raw, 1, MAX_DEPTH)
        if depth is None:
            errors["depth"] = [f"Choose a number from 1 to {MAX_DEPTH}, or all."]
    flags = {}
    for name, default in (("include_retired", False), ("show_instances", False), ("data_flows", True)):
        value = params.get(name)
        if value is None:
            flags[name] = default
        elif value.lower() in ("true", "false"):
            flags[name] = value.lower() == "true"
        else:
            errors[name] = ["Use true or false."]
    options = {
        "direction": direction,
        "depth": depth,
        "include_retired": flags.get("include_retired", False),
        "show_instances": flags.get("show_instances", False),
        "flows": flags.get("data_flows", True),
    }
    return options, errors


def _instance_names(org, nodes, flows):
    ids = {
        i for n in nodes for i in (n["row"]["edge"]["source_instance_id"], n["row"]["edge"]["target_instance_id"]) if i
    }
    for f in flows:
        ids |= {f["source_instance_id"], f["target_instance_id"]} - {None}
    found = ApplicationInstance.objects.filter(organization=org, id__in=ids).values_list("id", "name")
    return {str(i): name for i, name in found}


def _data_flows(org, listed, include_retired):
    """Integrations with one end at a listed record, once each, attached to the nearest such record, as light dicts
    sorted nearest first. Returns (list, number of retired integrations left out)."""
    # All of the organization's integrations in one light query, filtered here: a parameter per listed record in
    # SQL costs more than reading a few hundred rows.
    touching = Resource.objects.filter(organization=org, kind=Kind.INTEGRATION).values_list(
        text("id"),
        "name",
        "lifecycle",
        text("technical_owner_id"),
        text("integration__source_resource_id"),
        text("integration__source_instance_id"),
        text("integration__target_resource_id"),
        text("integration__target_instance_id"),
    )
    found, hidden = [], 0
    for rid, name, lifecycle, owner_id, source, source_instance, target, target_instance in touching:
        if source not in listed and target not in listed:
            continue
        if lifecycle == Lifecycle.RETIRED and not include_retired:
            hidden += 1
            continue
        nearest = min((e for e in (source, target) if e in listed), key=lambda e: (listed[e], str(e)))
        found.append(
            {
                "id": rid,
                "name": name,
                "lifecycle": lifecycle,
                "owner_id": owner_id,
                "source_resource_id": source,
                "source_instance_id": source_instance,
                "target_resource_id": target,
                "target_instance_id": target_instance,
                "attached_to": nearest,
                "depth": listed[nearest] + 1,
            }
        )
    found.sort(key=lambda f: (f["depth"], f["name"].lower(), f["id"]))
    return found, hidden


def _flow_json(org, flows, end, show_instances, instance_names):
    """The returned data flows in full: the integration's own columns, its ends and its implementing components."""
    if not flows:
        return []
    ids = [f["id"] for f in flows]
    details = {
        str(r.id): r
        for r in Resource.objects.filter(organization=org, id__in=ids).select_related(
            "technical_owner", "integration__source_resource", "integration__target_resource"
        )
    }
    implementers: dict = {}
    links = Relationship.objects.filter(organization=org, type__slug=RelationshipKind.IMPLEMENTS, target_id__in=ids)
    for link in links.select_related("source").order_by("source__name", "id"):
        implementers.setdefault(str(link.target_id), []).append(
            {
                "id": str(link.source_id),
                "name": link.source.name,
                "lifecycle": link.source.lifecycle,
                "archived": link.source.archived_at is not None,
            }
        )

    def flow_end(resource, instance_id):
        body = {"id": str(resource.id), "name": resource.name, "kind": resource.kind}
        if show_instances:
            body["instance"] = instance_id and {"id": str(instance_id), "name": instance_names.get(instance_id)}
        return body

    out = []
    for f in flows:
        resource = details[f["id"]]
        ext = resource.integration
        out.append(
            {
                "id": str(resource.id),
                "name": resource.name,
                "lifecycle": resource.lifecycle,
                "archived": resource.archived_at is not None,
                "hop": 1,
                "depth": f["depth"],
                "attached_to": end(f["attached_to"]),
                "source": flow_end(ext.source_resource, f["source_instance_id"]),
                "target": flow_end(ext.target_resource, f["target_instance_id"]),
                "protocol": ext.protocol,
                "schedule": ext.schedule,
                "direction": ext.direction,
                "auth_method": ext.auth_method,
                "implemented_by": implementers.get(f["id"], []),
                "vendor_native": f["id"] not in implementers,
                "link": f["link"],
                "technical_owner": person_ref(resource.technical_owner),
            }
        )
    return out
