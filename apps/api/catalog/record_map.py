"""A record's Map. Read-only.

`GET /<applications|components|ai-agents>/<id>/map?depth=1|2` returns everything the record is connected to, one or
two steps out, as a graph (`nodes`, `edges`) and as the table shown beside it (`rows`).

Contributors. What counts as a connection comes from a registry, like global search's searchers (core/search.py):
each `Contributor` takes the records on the current frontier and returns `Link`s, in a constant number of queries
whatever the frontier's size. Catalog registers relationships (dependency and other types, both directions),
integrations (as flow edges between their ends) and instances; discovery registers its linked sources from its
own `ready()`. Contracts and licence pools are added the same way (`register` from money/), with no change here,
in the view or in the response's shape: the response's `categories` lists what the registered contributors produce,
in order, so the SPA groups the table without knowing the categories in advance.

Rules:
- Breadth first from the record. Only records (applications, components, AI agents) are expanded; integrations
  reached through an `implements` link, instances and discovery sources are leaves.
- An edge appears once (a relationship between two expanded records is found from both ends). An edge between two
  nodes already on the map is kept, so a triangle shows as one.
- Archived and retired nodes and integrations are shown and flagged, never hidden.
- At most MAX_NODES nodes; nearest first, then by category order, then by name. `truncated` says when it stopped,
  and edges to the nodes left out are left out with them. `counts` (the record's own connections per category) is
  counted before the cap, so it stays true for a record with more connections than the map shows.
- Record kinds the edition lacks (AI agents without `ai_agents`) are left out, as their routes would refuse.
- Every query filters on the organization and a Resource at the far end is loaded with the organization in the
  filter, so a node from another organization can't appear even if a row pointed at one (composite foreign keys
  forbid that already).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from django.db.models import Q

from core.capabilities import AI_AGENTS, has_capability

from .models import KIND_PATHS, RECORD_KINDS, ApplicationInstance, Integration, Kind, Lifecycle, Relationship, Resource

MAX_NODES = 150
DEPTHS = (1, 2)
API_PATHS: dict[str, str] = {**KIND_PATHS, Kind.INTEGRATION: "integrations"}


@dataclass(frozen=True)
class Link:
    """One connection found from `near` (a record on the frontier) to `other`. `edge` is the response's edge:
    `id` (unique across nodes and edges: "<relation>:<uuid>"), `source`, `target`, `relation`, `category`, `label`
    (drawn on the graph), `reads` (from the source), `reads_inverse` (from the target), `archived`, `retired` and
    `detail`. `node` is the far end when it isn't a Resource (an instance, a discovery source); a Resource far end is
    loaded by build(), with the organization in the filter."""

    near: str
    other: str
    edge: dict[str, Any]
    node: dict[str, Any] | None = None


@dataclass(frozen=True)
class Contributor:
    """A source of connections. `collect(org, frontier)` returns the links touching any frontier record (IDs as
    strings) in a constant number of queries. `categories` are the (key, label) pairs its edges use, in display
    order; `rank` orders contributors (lower first). `capability`: the contributor is skipped for an organization
    without it."""

    name: str
    rank: int
    categories: tuple[tuple[str, str], ...]
    collect: Callable[[Any, list[str]], Iterable[Link]]
    capability: str | None = None


CONTRIBUTORS: list[Contributor] = []


def register(contributor: Contributor) -> None:
    """Add a contributor, or replace the one with the same name (an app's `ready()` may run twice in tests)."""
    CONTRIBUTORS[:] = [c for c in CONTRIBUTORS if c.name != contributor.name]
    CONTRIBUTORS.append(contributor)
    CONTRIBUTORS.sort(key=lambda c: (c.rank, c.name))


def active(org) -> list[Contributor]:
    return [c for c in CONTRIBUTORS if c.capability is None or has_capability(org, c.capability)]


# ---- nodes ----------------------------------------------------------------------------------------------------------


def resource_node(rid: str, kind: str, name: str, lifecycle: str, archived: bool) -> dict[str, Any]:
    path = API_PATHS.get(kind)
    expandable = kind in RECORD_KINDS
    return {
        "id": rid,
        "kind": kind,
        "name": name,
        "lifecycle": lifecycle,
        "archived": archived,
        "retired": lifecycle == Lifecycle.RETIRED,
        "path": f"{path}/{rid}" if path else None,
        "map_path": f"{path}/{rid}/map" if expandable else None,
        "expandable": expandable,
        "detail": {},
    }


def other_node(nid: str, kind: str, name: str, path: str | None, detail: dict[str, Any]) -> dict[str, Any]:
    """A node that isn't a Resource: no lifecycle, never archived or retired, never expanded."""
    return {
        "id": nid,
        "kind": kind,
        "name": name,
        "lifecycle": None,
        "archived": False,
        "retired": False,
        "path": path,
        "map_path": None,
        "expandable": False,
        "detail": detail,
    }


def hidden_kinds(org) -> list[str]:
    """Record kinds the organization's edition doesn't have: their rows are left off a map and out of the dependency
    explorer, since their routes would refuse (AI agents without `ai_agents`)."""
    return [] if has_capability(org, AI_AGENTS) else [Kind.AI_AGENT]


def _resources(org, ids: set[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    rows = (
        Resource.objects.filter(organization=org, id__in=ids)
        .exclude(kind__in=hidden_kinds(org))
        .values_list("id", "kind", "name", "lifecycle", "archived_at")
    )
    return {str(i): resource_node(str(i), kind, name, lc, at is not None) for i, kind, name, lc, at in rows}


def _instance_ref(instance) -> dict[str, str] | None:
    return instance and {"id": str(instance.id), "name": instance.name}


# ---- catalog's contributors -----------------------------------------------------------------------------------------


def relationships(org, frontier: list[str]) -> Iterable[Link]:
    """Relationships of every type, both directions: category `dependency` for a type with `is_dependency`, else
    `relationship`. Read from the source as the type's name, from the target as its inverse name."""
    rows = (
        Relationship.objects.filter(organization=org)
        .filter(Q(source_id__in=frontier) | Q(target_id__in=frontier))
        .select_related("type", "source_instance", "target_instance")
        .order_by("created_at", "id")
    )
    wanted = set(frontier)
    for rel in rows:
        rtype = rel.type
        source, target = str(rel.source_id), str(rel.target_id)
        edge = {
            "id": f"relationship:{rel.id}",
            "source": source,
            "target": target,
            "relation": rtype.slug,
            "category": "dependency" if rtype.is_dependency else "relationship",
            "label": rtype.name,
            "reads": rtype.name,
            "reads_inverse": rtype.inverse_name or f"{rtype.name} (from)",
            "archived": False,
            "retired": False,
            "detail": {
                "relationship": str(rel.id),
                "type": {
                    "id": str(rtype.id),
                    "slug": rtype.slug,
                    "name": rtype.name,
                    "inverse_name": rtype.inverse_name,
                    "is_dependency": rtype.is_dependency,
                },
                "source_instance": _instance_ref(rel.source_instance),
                "target_instance": _instance_ref(rel.target_instance),
                "purpose": rel.purpose,
            },
        }
        if source in wanted:
            yield Link(source, target, edge)
        if target in wanted and target != source:
            yield Link(target, source, edge)


def integrations(org, frontier: list[str]) -> Iterable[Link]:
    """Integrations with an end at a frontier record, as an edge from source to target labelled with the
    integration's name. Its own lifecycle and archived state are the edge's."""
    rows = (
        Integration.objects.filter(organization=org)
        .filter(Q(source_resource_id__in=frontier) | Q(target_resource_id__in=frontier))
        .select_related("resource", "source_instance", "target_instance")
        .order_by("resource__name", "resource_id")
    )
    wanted = set(frontier)
    for ext in rows:
        resource = ext.resource
        source, target = str(ext.source_resource_id), str(ext.target_resource_id)
        two_way = ext.direction == "two_way"
        edge = {
            "id": f"integration:{resource.id}",
            "source": source,
            "target": target,
            "relation": "integration",
            "category": "integration",
            "label": resource.name,
            "reads": "Exchanges data with" if two_way else "Sends data to",
            "reads_inverse": "Exchanges data with" if two_way else "Receives data from",
            "archived": resource.archived_at is not None,
            "retired": resource.lifecycle == Lifecycle.RETIRED,
            "detail": {
                "integration": {
                    "id": str(resource.id),
                    "name": resource.name,
                    "lifecycle": resource.lifecycle,
                    "path": f"integrations/{resource.id}",
                },
                "protocol": ext.protocol,
                "direction": ext.direction,
                "schedule": ext.schedule,
                "auth_method": ext.auth_method,
                "source_instance": _instance_ref(ext.source_instance),
                "target_instance": _instance_ref(ext.target_instance),
            },
        }
        if source in wanted:
            yield Link(source, target, edge)
        if target in wanted and target != source:
            yield Link(target, source, edge)


def instances(org, frontier: list[str]) -> Iterable[Link]:
    """An application's instances (Production, Sandbox), as leaf nodes."""
    rows = ApplicationInstance.objects.filter(Q(application_id__in=frontier), organization=org).order_by("name", "id")
    for inst in rows:
        app, iid = str(inst.application_id), str(inst.id)
        node = other_node(
            iid,
            "instance",
            inst.name,
            f"applications/{app}/instances/{iid}",
            {"application": app, "environment": inst.environment, "status": inst.status, "url": inst.url},
        )
        edge = {
            "id": f"instance:{iid}",
            "source": app,
            "target": iid,
            "relation": "instance",
            "category": "instance",
            "label": "Instance",
            "reads": "Has instance",
            "reads_inverse": "Instance of",
            "archived": False,
            "retired": False,
            "detail": {},
        }
        yield Link(app, iid, edge, node)


def register_catalog() -> None:
    """Catalog's contributors; called from CatalogConfig.ready()."""
    register(
        Contributor(
            "relationships",
            10,
            (("dependency", "Dependencies"), ("relationship", "Other relationships")),
            relationships,
        )
    )
    register(Contributor("integrations", 20, (("integration", "Integrations"),), integrations))
    register(Contributor("instances", 30, (("instance", "Instances"),), instances))


# ---- the map ----------------------------------------------------------------------------------------------------------


@dataclass
class _Map:
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[str, dict[str, Any]] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False


def build(org, root: Resource, depth: int = 1) -> dict[str, Any]:
    """The map of `root` (a record of `org`) within `depth` (1 or 2) steps. Returns the response body."""
    if root.organization_id != org.id or root.kind not in RECORD_KINDS:
        raise ValueError("The map's record must be an application, component or AI agent of the organization.")
    if depth not in DEPTHS:
        raise ValueError(f"depth must be one of {DEPTHS}")
    contributors = active(org)
    categories = [c for contributor in contributors for c in contributor.categories]
    rank = {key: i for i, (key, _) in enumerate(categories)}

    root_id = str(root.id)
    found = _Map()
    found.nodes[root_id] = {
        **resource_node(root_id, root.kind, root.name, root.lifecycle, root.archived_at is not None),
        "hop": 0,
        "root": True,
    }
    frontier = [root_id]
    counts = {key: 0 for key, _ in categories}
    for hop in range(1, depth + 1):
        if not frontier:
            break
        links = [link for c in contributors for link in c.collect(org, frontier) if link.edge["id"] not in found.edges]
        far = _resources(org, {lk.other for lk in links if lk.node is None and lk.other not in found.nodes})
        candidates = []
        for link in links:
            other = found.nodes.get(link.other) or link.node or far.get(link.other)
            if other is not None:  # else not one of this organization's rows
                candidates.append((rank.get(link.edge["category"], len(rank)), other["name"].lower(), link, other))
        candidates.sort(key=lambda c: (c[0], c[1], c[2].edge["id"]))
        if hop == 1:  # the record's own connections, counted before the node cap cuts any
            for edge in {c[2].edge["id"]: c[2].edge for c in candidates}.values():
                counts[edge["category"]] = counts.get(edge["category"], 0) + 1

        following = []
        for _, _, link, other in candidates:
            if link.edge["id"] in found.edges:
                continue  # the same edge, found from its other end
            if link.other not in found.nodes:
                if len(found.nodes) >= MAX_NODES:
                    found.truncated = True
                    continue
                found.nodes[link.other] = {**other, "hop": hop, "root": False}
                if other["expandable"]:
                    following.append(link.other)
            edge = link.edge
            found.edges[edge["id"]] = edge
            found.rows.append(
                {
                    "edge": edge["id"],
                    "category": edge["category"],
                    "near": link.near,
                    "other": link.other,
                    "reads": edge["reads"] if link.near == edge["source"] else edge["reads_inverse"],
                    "hop": hop,
                }
            )
        frontier = following

    return {
        "root": root_id,
        "depth": depth,
        "nodes": list(found.nodes.values()),
        "edges": list(found.edges.values()),
        "rows": found.rows,
        "categories": [{"key": key, "label": label} for key, label in categories],
        "counts": counts,
        "truncated": found.truncated,
        "max_nodes": MAX_NODES,
    }
