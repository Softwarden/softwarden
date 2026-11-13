"""Contracts and licence pools on a record's Map (catalog/record_map.py).
Registered from MoneyConfig.ready(), with the `contracts` capability.

- contracts: each contract line of a frontier record is an edge from the record to the contract (a leaf node), with
  the line's allocation and the contract's status, end date and deadline in its detail. An archived contract is shown
  and flagged, never hidden.
- licence pools: each pool of a frontier application is a leaf node, with its seats and utilisation.

Each contributor is one query whatever the frontier's size; every query filters on the organization."""

from collections.abc import Iterable
from typing import Any

from catalog.record_map import Contributor, Link, other_node, register
from core.capabilities import CONTRACTS

from .contracts import utilization
from .models import ContractItem, LicensePool


def _date(value):
    return value.isoformat() if value else None


def contracts(org, frontier: list[str]) -> Iterable[Link]:
    rows = (
        ContractItem.objects.filter(organization=org, resource_id__in=frontier)
        .select_related("contract__vendor")
        .order_by("contract__name", "contract_id", "id")
    )
    for item in rows:
        contract, record, cid = item.contract, str(item.resource_id), str(item.contract_id)
        node = other_node(
            cid,
            "contract",
            contract.name,
            f"contracts/{cid}",
            {
                "vendor": contract.vendor and {"id": str(contract.vendor.id), "name": contract.vendor.name},
                "status": contract.status,
                "end_date": _date(contract.end_date),
                "cancellation_deadline": _date(contract.cancellation_deadline),
                "annualized_cost": None if contract.annualized_cost is None else str(contract.annualized_cost),
                "currency": contract.currency,
            },
        )
        node["archived"] = contract.archived_at is not None
        percent = item.allocation_percent
        edge = {
            "id": f"contract_item:{item.id}",
            "source": record,
            "target": cid,
            "relation": "contract",
            "category": "contract",
            "label": contract.name,
            "reads": "Covered by contract",
            "reads_inverse": "Covers",
            "archived": contract.archived_at is not None,
            "retired": False,
            "detail": {"line": str(item.id), "allocation_percent": None if percent is None else str(percent)},
        }
        yield Link(record, cid, edge, node)


def license_pools(org, frontier: list[str]) -> Iterable[Link]:
    rows = LicensePool.objects.filter(organization=org, application_id__in=list[Any](frontier)).order_by(
        "sku_name", "id"
    )
    for pool in rows:
        app, pid = str(pool.application_id), str(pool.id)
        node = other_node(
            pid,
            "license_pool",
            pool.sku_name,
            f"license-pools/{pid}",
            {
                "sku_id": pool.sku_id,
                "seats_purchased": pool.seats_purchased,
                "seats_assigned": pool.seats_assigned,
                "utilization": utilization(pool),
                "contract": None if pool.contract_id is None else str(pool.contract_id),
            },
        )
        edge = {
            "id": f"license_pool:{pid}",
            "source": app,
            "target": pid,
            "relation": "license_pool",
            "category": "license_pool",
            "label": pool.sku_name,
            "reads": "Has license pool",
            "reads_inverse": "License pool of",
            "archived": False,
            "retired": False,
            "detail": {},
        }
        yield Link(app, pid, edge, node)


def register_contributors() -> None:
    register(Contributor("contracts", 50, (("contract", "Contracts"),), contracts, CONTRACTS))
    register(Contributor("license_pools", 60, (("license_pool", "License pools"),), license_pools, CONTRACTS))
