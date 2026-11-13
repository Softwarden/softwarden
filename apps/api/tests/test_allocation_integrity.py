"""Contract cost allocation (the acceptance check): fixed allocations can't exceed 100% on any write path, concurrent edits included, and allocation
rows always sum exactly to the annualised cost.

Model-level paths (full_clean and a bare save()) have no counterpart here: every write goes through
money/contracts.py, so the service's add, change and replace paths and the API stand in for them. The CSV
import swap is money/contracts.set_items, the replace the contracts import will call (the import itself is a later
Package)."""

import threading
from decimal import Decimal

import pytest
from django.db import connection

from catalog import services
from core.problems import FieldErrors
from money import contracts
from money.models import Contract, ContractItem

pytestmark = pytest.mark.django_db
D = Decimal


def make_contract(a, amount="100.00", **kw):
    return contracts.create_contract(
        a["org"], a["users"]["editor"], {"name": "Box", "amount": amount, "billing_frequency": "annual", **kw}
    )


def record(a, name=None):
    count = ContractItem.objects.count() + Contract.objects.count()
    return services.create_resource(
        a["org"], a["users"]["editor"], {"name": name or f"Software {count}-{id(a)}", "lifecycle": "active"}
    )


def line(a, contract, pct=None, sw=None):
    sw = sw or record(a, f"Software {ContractItem.objects.count()}")
    data = {"record": str(sw.id)}
    if pct is not None:
        data["allocation_percent"] = pct
    return contracts.add_item(a["org"], a["users"]["editor"], contract, data)


def rows(contract):
    contract.refresh_from_db()
    return list(contracts.allocations([contract]))


# --- Over-allocation --------------------------------------------------------------------------------------------------


def test_service_rejects_fixed_total_over_100(a):
    contract = make_contract(a)
    line(a, contract, "80")
    with pytest.raises(FieldErrors) as exc:
        line(a, contract, "80")
    assert exc.value.errors["allocation_percent"] == [
        "Fixed allocations would total 160% (max 100%); 80% is already allocated to other lines."
    ]
    assert ContractItem.objects.count() == 1


def test_changing_a_line_is_also_rejected(a):
    contract = make_contract(a)
    first = line(a, contract, "80")
    second = line(a, contract, None)
    with pytest.raises(FieldErrors) as exc:
        contracts.update_item(a["org"], a["users"]["editor"], second, {"allocation_percent": "30"})
    assert "allocation_percent" in exc.value.errors
    second.refresh_from_db()
    assert second.allocation_percent is None
    # The line's own old value doesn't count against it: 80 → 100 is fine.
    contracts.update_item(a["org"], a["users"]["editor"], first, {"allocation_percent": "100"})
    first.refresh_from_db()
    assert first.allocation_percent == D("100")


def test_api_add_and_update_reject_over_allocation_with_400(a, api):
    contract = make_contract(a)
    first = line(a, contract, "80")
    editor = api(a["users"]["editor"])
    added = editor.post(
        f"contracts/{contract.id}/lines", {"record": str(record(a, "Extra").id), "allocation_percent": "30"}
    )
    assert added.status_code == 400, added.json_body
    assert "max 100%" in added.json_body["errors"]["allocation_percent"][0]
    assert contract.items.count() == 1
    second = line(a, contract, None)
    changed = editor.patch(f"contracts/{contract.id}/lines/{second.id}", {"allocation_percent": "25"})
    assert changed.status_code == 400 and "max 100%" in changed.json_body["errors"]["allocation_percent"][0]
    second.refresh_from_db()
    first.refresh_from_db()
    assert second.allocation_percent is None and first.allocation_percent == D("80")


@pytest.mark.parametrize("value", ["100.01", "-1", "12.345", "lots", "1e30", "9" * 31, "NaN", "Infinity", "-Infinity"])
def test_a_single_line_outside_0_to_100_is_refused(a, value):
    with pytest.raises(FieldErrors) as exc:
        line(a, make_contract(a), value)
    assert "allocation_percent" in exc.value.errors


def test_replacing_the_lines_checks_the_final_total_and_can_reshuffle(a):
    org, editor = a["org"], a["users"]["editor"]
    contract = make_contract(a)
    jira, confluence = record(a, "Jira"), record(a, "Confluence")
    with pytest.raises(FieldErrors) as exc:
        contracts.set_items(org, editor, contract, [(jira, D("80")), (confluence, D("80"))])
    assert "100%" in exc.value.errors["items"][0]
    assert not ContractItem.objects.exists()
    contracts.set_items(org, editor, contract, [(jira, D("80")), (confluence, D("20"))])
    # Swapping the split in one change must not trip over its own intermediate state.
    contracts.set_items(org, editor, contract, [(jira, D("20")), (confluence, D("80"))])
    split = {i.resource.name: i.allocation_percent for i in contract.items.select_related("resource")}
    assert split == {"Jira": D("20"), "Confluence": D("80")}


concurrent = pytest.mark.django_db(transaction=True)  # as the other concurrency tests in this suite


@concurrent
def test_concurrent_edits_cannot_overallocate_together(a):
    contract = make_contract(a)
    items = [record(a, f"Concurrent {n}") for n in range(2)]
    barrier = threading.Barrier(2)
    outcomes = []

    def add(sw):
        try:
            barrier.wait()
            line(a, contract, "60", sw=sw)
            outcomes.append("ok")
        except FieldErrors:
            outcomes.append("rejected")
        finally:
            connection.close()

    threads = [threading.Thread(target=add, args=(sw,)) for sw in items]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes) == ["ok", "rejected"]
    assert ContractItem.objects.filter(contract=contract).count() == 1


@concurrent
def test_concurrent_line_changes_cannot_overallocate_together(a):
    contract = make_contract(a)
    first, second = line(a, contract, "40"), line(a, contract, "40")
    barrier = threading.Barrier(2)
    outcomes = []

    def change(item):
        try:
            barrier.wait()
            contracts.update_item(a["org"], a["users"]["editor"], item, {"allocation_percent": "60"})
            outcomes.append("ok")
        except FieldErrors:
            outcomes.append("rejected")
        finally:
            connection.close()

    threads = [threading.Thread(target=change, args=(item,)) for item in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes) == ["ok", "rejected"]
    assert sum(i.allocation_percent or D(0) for i in ContractItem.objects.filter(contract=contract)) == D("100")


# --- Exact totals -----------------------------------------------------------------------------------------------------


def test_equal_thirds_sum_exactly_and_deterministically(a):
    contract = make_contract(a, "100.00")
    for _ in range(3):
        line(a, contract, None)
    amounts = [r.amount for r in rows(contract)]
    assert sum(amounts) == D("100.00")
    assert amounts == [D("33.34"), D("33.33"), D("33.33")]
    assert [r.amount for r in rows(contract)] == amounts  # same answer every time


def test_unallocated_remainder_row_and_cents_reconcile(a):
    contract = make_contract(a, "1000.01")
    line(a, contract, "33.33")
    line(a, contract, "33.33")
    allocation = rows(contract)
    assert allocation[-1].item is None  # documented unallocated remainder
    assert sum(r.amount for r in allocation) == D("1000.01")


def test_allocations_split_by_percent_and_evenly(a):
    contract = make_contract(a, "1000")
    fixed, even, other = line(a, contract, "50"), line(a, contract), line(a, contract)
    assert {r.item.id: r.amount for r in rows(contract) if r.item} == {
        fixed.id: D("500.00"),
        even.id: D("250.00"),
        other.id: D("250.00"),
    }


def test_fully_fixed_allocations_leave_remainder_unallocated(a):
    contract = make_contract(a, "1000")
    fixed = line(a, contract, "60")
    assert [(r.item and r.item.id, r.amount) for r in rows(contract)] == [(fixed.id, D("600.00")), (None, D("400.00"))]


def test_a_contract_without_lines_is_one_unallocated_row(a):
    contract = make_contract(a, "120", billing_frequency="monthly")
    assert [(r.item, r.share, r.amount) for r in rows(contract)] == [(None, D("100"), D("1440.00"))]


def test_totals_over_many_contracts_reconcile(a):
    """The vendor totals check, without the reports (a later package): the rows of every contract add up to the
    sum of their annualised costs."""
    made = []
    for amount in ("100.00", "200.00", "0.10"):
        contract = make_contract(a, amount)
        for _ in range(3):
            line(a, contract, None)
        made.append(contract)
    for contract in made:
        contract.refresh_from_db()
    assert sum(r.amount for r in contracts.allocations(made)) == D("300.10")


def test_legacy_overallocated_data_is_scaled_not_inflated(a):
    contract = make_contract(a, "100.00")
    first, second = line(a, contract, "50"), line(a, contract, "50")
    ContractItem.objects.filter(pk__in=[first.pk, second.pk]).update(allocation_percent=D("80"))  # pre-fix data
    allocation = rows(contract)
    assert sum(r.amount for r in allocation) == D("100.00")
    assert [r.amount for r in allocation] == [D("50.00"), D("50.00")]
