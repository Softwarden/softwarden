"""Discovery's contributor to a record's Map (catalog/record_map.py): the source objects a record is linked to or was
created from (a linked or created reconciliation decision), as leaf nodes. Dismissed observations aren't linked to a
record and never appear. Registered from DiscoveryConfig.ready()."""

from collections.abc import Iterable

from catalog.record_map import Contributor, Link, other_node, register
from core.drf import timestamp

from .models import DecisionKind, ReconciliationDecision


def sources(org, frontier: list[str]) -> Iterable[Link]:
    rows = (
        ReconciliationDecision.objects.filter(organization=org, resource_id__in=frontier)
        .exclude(decision=DecisionKind.DISMISSED)
        .select_related("observation__connector")
        .order_by("created_at", "id")
    )
    for decision in rows:
        observation, connector = decision.observation, decision.observation.connector
        record, oid = str(decision.resource_id), str(observation.id)
        node = other_node(
            oid,
            "discovery_source",
            observation.display_name,
            f"discovery/observations/{oid}",
            {
                "connector": {"id": str(connector.id), "name": connector.name, "source": connector.source},
                "external_id": observation.external_id,
                "decision": decision.decision,
                "last_seen_at": timestamp(observation.last_seen_at),
                "gone_at": timestamp(observation.gone_at),
            },
        )
        edge = {
            "id": f"discovery:{decision.id}",
            "source": record,
            "target": oid,
            "relation": "discovery",
            "category": "discovery",
            "label": connector.name,
            "reads": "Discovered in",
            "reads_inverse": "Source of",
            "archived": False,
            "retired": False,
            "detail": {"decision": decision.decision, "gone": observation.gone_at is not None},
        }
        yield Link(record, oid, edge, node)


def register_contributor() -> None:
    register(Contributor("discovery", 40, (("discovery", "Discovery sources"),), sources))
