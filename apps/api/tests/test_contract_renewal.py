"""Renewal and licences: the contract renew action. Every decision; the terms written to the cycle; the next term linked, driving and carrying the
lines and pools; the next cycle's dates; every refusal; two renews at once; audit and History; the API. The one source
of truth is checked after every test here (tests/conftest.py one_source_of_truth)."""

import threading
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import connection

from catalog import services
from core.models import AuditEvent
from core.problems import Conflict, FieldErrors
from money import contracts, renewal_action, renewals
from money.models import Contract, ContractItem, LicensePool, RenewalCycle
from tests.conftest import source_of_truth_problems

pytestmark = [pytest.mark.django_db, pytest.mark.one_source_of_truth]
D = Decimal
TERM = {
    "start_date": "2026-04-01",
    "end_date": "2027-03-31",
    "notice_value": 1,
    "notice_unit": "months",
    "auto_renew": "yes",
    "amount": "1200",
    "billing_frequency": "annual",
    "currency": "EUR",
}


def app(a, name):
    return services.create_resource(a["org"], a["users"]["editor"], {"name": name, "lifecycle": "active"})


def linked(a, name="Atlassian Cloud", **extra):
    """A contract with its renewal (the renewal's open cycle takes its dates), two lines and a licence pool."""
    org, editor = a["org"], a["users"]["editor"]
    contract = contracts.create_contract(
        org, editor, {"name": name, "create_renewal_obligation": True, "cost_center": "IT-100", **TERM, **extra}
    )
    contracts.add_item(org, editor, contract, {"record": str(a["app"].id), "allocation_percent": "60"})
    contracts.add_item(org, editor, contract, {"record": str(app(a, f"{name} Wiki").id)})
    contracts.create_pool(
        org,
        editor,
        {"application": str(a["app"].id), "contract": str(contract.id), "sku_name": "Standard", "sku_id": f"std-{name}",
         "seats_purchased": 50, "seats_assigned": 40, "seat_sync": True},
    )  # fmt: skip
    contract.refresh_from_db()
    return contract


def renew(a, contract, **data):
    return renewal_action.renew(a["org"], a["users"]["editor"], contract, {"decision": "renew", **data})


def queue_deadline(a, obligation):
    state = next(
        s for s in renewals.queue_states(a["org"], services.org_today(a["org"])) if s.obligation.id == obligation.id
    )
    return state.deadline.date


# ---- every decision ------------------------------------------------------------------------------------------------------


def test_renew_with_the_defaults(a):
    old = linked(a)
    obligation = old.renewal_obligation
    first = RenewalCycle.objects.get(obligation=obligation)
    outcome = renew(a, old, decision_notes="Still needed.")
    new = outcome.successor
    assert new is not None
    new.refresh_from_db()
    # The next term: the day after, as long, the same money, owner, cost centre; linked to the same renewal.
    assert (new.start_date, new.end_date) == (date(2027, 4, 1), date(2028, 3, 31))
    assert (new.amount, new.currency, new.billing_frequency, new.annualized_cost) == (
        D("1200"),
        "EUR",
        "annual",
        D("1200"),
    )
    assert (new.name, new.cost_center, new.auto_renew, new.notice_value, new.notice_unit) == (
        "Atlassian Cloud", "IT-100", "yes", 1, "months"
    )  # fmt: skip
    assert new.predecessor_id == old.id and new.renewal_obligation_id == obligation.id
    assert new.status == "signed"  # starts in the future: a commitment, not current spend
    assert new.notes == "Renewal of Atlassian Cloud."
    assert contracts.driver(obligation).id == new.id  # type: ignore[union-attr]
    old.refresh_from_db()
    assert old.status == "active"  # still what is being paid for until it ends
    # The cycle: decision with the terms before and after, completed; the next cycle takes the next term's dates.
    first.refresh_from_db()
    assert (first.decision, first.decision_notes, first.decided_on) == (
        "renew",
        "Still needed.",
        services.org_today(a["org"]),
    )
    assert first.completed_on == services.org_today(a["org"])
    assert first.completion_evidence == "Renewed as the contract Atlassian Cloud."
    assert (first.old_amount, first.old_currency, first.old_end_date) == (D("1200"), "EUR", date(2027, 3, 31))
    assert (first.new_amount, first.new_annualized_cost, first.new_start_date, first.new_end_date) == (
        D("1200"), D("1200"), date(2027, 4, 1), date(2028, 3, 31)
    )  # fmt: skip
    second = outcome.next_cycle
    assert second is not None and second.sequence == 2 and second.completed_on is None
    assert (second.renewal_date, second.notice_value, second.notice_unit) == (date(2028, 3, 31), 1, "months")
    covered = set(first.coverage.values_list("application_id", flat=True))
    assert set(second.coverage.values_list("application_id", flat=True)) == covered  # the renewal's coverage
    assert queue_deadline(a, obligation) == new.cancellation_deadline == date(2028, 2, 29)
    # Lines copied with their allocations; the pool moved (seats counted once), still syncing.
    lines = {i.resource.name: i.allocation_percent for i in ContractItem.objects.filter(contract=new)}
    assert lines == {"orga App": D("60.00"), "Atlassian Cloud Wiki": None}
    assert ContractItem.objects.filter(contract=old).count() == 2
    pool = LicensePool.objects.get(sku_id="std-Atlassian Cloud")
    assert pool.contract_id == new.id and pool.seat_sync


def test_renegotiate_with_new_terms(a):
    old = linked(a)
    obligation = old.renewal_obligation
    outcome = renewal_action.renew(
        a["org"],
        a["users"]["editor"],
        old,
        {"decision": "renegotiate", "decision_notes": "Moved to monthly.", "amount": "150", "currency": "usd",
         "billing_frequency": "monthly", "end_date": "2028-06-30", "notice_value": 60, "notice_unit": "days",
         "auto_renew": "no", "name": "Atlassian Cloud 2027", "po_number": "PO-77"},
    )  # fmt: skip
    new = outcome.successor
    assert new is not None
    assert (new.name, new.amount, new.currency, new.annualized_cost, new.po_number) == (
        "Atlassian Cloud 2027", D("150"), "USD", D("1800"), "PO-77"
    )  # fmt: skip
    cycle = RenewalCycle.objects.get(obligation=obligation, sequence=1)
    change = renewals.TERM_COLUMNS
    assert {k: getattr(cycle, k) for k in change} == {
        "old_amount": D("1200"), "old_currency": "EUR", "old_billing_frequency": "annual",
        "old_annualized_cost": D("1200"), "old_start_date": date(2026, 4, 1), "old_end_date": date(2027, 3, 31),
        "new_amount": D("150"), "new_currency": "USD", "new_billing_frequency": "monthly",
        "new_annualized_cost": D("1800"), "new_start_date": date(2027, 4, 1), "new_end_date": date(2028, 6, 30),
    }  # fmt: skip
    assert cycle.decision == "renegotiate"
    obligation.refresh_from_db()
    assert obligation.auto_renew == "no"  # the renewal follows its new driver
    assert outcome.next_cycle is not None and outcome.next_cycle.renewal_date == date(2028, 6, 30)
    assert queue_deadline(a, obligation) == new.cancellation_deadline == date(2028, 6, 30) - timedelta(days=90)


def test_cancel(a):
    old = linked(a)
    obligation = old.renewal_obligation
    with pytest.raises(FieldErrors) as refused:
        renewal_action.renew(a["org"], a["users"]["editor"], old, {"decision": "cancel"})
    assert refused.value.errors == {"decision_notes": ["Say why it isn't being renewed."]}
    with pytest.raises(FieldErrors) as refused:
        renewal_action.renew(
            a["org"], a["users"]["editor"], old, {"decision": "cancel", "decision_notes": "x", "end_date": "2028-01-01"}
        )
    assert refused.value.errors == {"end_date": ["Only renewing or renegotiating creates a next term."]}
    outcome = renewal_action.renew(
        a["org"], a["users"]["editor"], old, {"decision": "cancel", "decision_notes": "Moving to Confluence DC."}
    )
    assert outcome.successor is None and outcome.next_cycle is None
    assert outcome.contract.status == "cancelled"
    assert contracts.driver(obligation) is None
    assert not Contract.objects.filter(predecessor=old).exists()
    cycle = RenewalCycle.objects.get(obligation=obligation)
    assert (cycle.decision, cycle.completion_evidence) == ("cancel", "Contract Atlassian Cloud cancelled.")
    assert cycle.completed_on is not None
    assert (cycle.old_amount, cycle.new_amount, cycle.new_currency) == (D("1200"), None, "")
    assert (
        LicensePool.objects.get(sku_id="std-Atlassian Cloud").contract_id == old.id
    )  # pools stay with a cancelled term
    events = list(AuditEvent.objects.filter(target_id=old.id).order_by("created_at").values_list("action", flat=True))
    assert events[-2:] == ["contract.decided", "contract.updated"]


def test_a_contract_without_a_renewal_is_renewed_without_cycle_work(a):
    old = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Slack", **TERM})
    cycles = RenewalCycle.objects.count()
    outcome = renew(a, old, end_date="2028-03-31")
    assert outcome.successor is not None and outcome.successor.renewal_obligation_id is None
    assert outcome.cycle is None and outcome.next_cycle is None
    assert RenewalCycle.objects.count() == cycles


def test_an_ended_term_is_marked_renewed_and_the_next_starts_now(a):
    today = services.org_today(a["org"])
    old = linked(
        a, start_date=(today - timedelta(days=400)).isoformat(), end_date=(today - timedelta(days=10)).isoformat()
    )
    outcome = renew(a, old)
    assert outcome.contract.status == "renewed"
    assert outcome.successor is not None and outcome.successor.status == "active"
    assert outcome.successor.start_date == today - timedelta(days=9)


def test_a_completed_cycle_is_not_rewritten(a):
    """The renewal's cycle was completed by hand: renewing establishes the next cycle and leaves history alone."""
    old = linked(a)
    obligation = old.renewal_obligation
    first = RenewalCycle.objects.get(obligation=obligation)
    renewals.record_decision(a["org"], a["users"]["editor"], obligation, first.id, {"decision": "renew"})
    today = services.org_today(a["org"]).isoformat()
    renewals.record_completion(
        a["org"], a["users"]["editor"], obligation, first.id, {"completed_on": today, "completion_evidence": "PO 9"}
    )
    outcome = renew(a, old)
    first.refresh_from_db()
    assert outcome.cycle is None and first.completion_evidence == "PO 9" and first.new_amount is None
    assert outcome.next_cycle is not None and outcome.next_cycle.renewal_date == date(2028, 3, 31)


def test_chosen_lines_and_copied_pools(a):
    old = linked(a)
    wiki = ContractItem.objects.get(contract=old, resource__name="Atlassian Cloud Wiki")
    pool = LicensePool.objects.get(contract=old)
    outcome = renew(a, old, lines=[str(wiki.id)], pools=[str(pool.id)], pool_action="copy")
    new = outcome.successor
    assert new is not None
    assert [(i.resource.name, i.allocation_percent) for i in ContractItem.objects.filter(contract=new)] == [
        ("Atlassian Cloud Wiki", None)
    ]
    copy = LicensePool.objects.get(contract=new)
    pool.refresh_from_db()
    assert pool.contract_id == old.id and pool.seat_sync  # the original keeps syncing
    assert (copy.sku_id, copy.seats_purchased, copy.seats_assigned, copy.seat_sync, copy.last_synced) == (
        "std-Atlassian Cloud", 50, 40, False, None
    )  # fmt: skip
    assert AuditEvent.objects.get(action="license_pool.created", target_id=copy.id).changes["copied_from"] == str(
        pool.id
    )
    nothing = renew(a, linked(a, name="Jira"), lines=[], pools=[])
    assert nothing.successor is not None and not ContractItem.objects.filter(contract=nothing.successor).exists()


# ---- refusals ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["cancelled", "expired", "renewed"])
def test_a_contract_that_does_not_count_is_refused(a, status):
    contract = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Old", **TERM, "status": status})
    with pytest.raises(Conflict) as refused:
        renew(a, contract)
    assert "can't be renewed or canceled" in str(refused.value.detail)


def test_refusals(a):
    org, editor = a["org"], a["users"]["editor"]
    archived = contracts.archive_contract(org, editor, contracts.create_contract(org, editor, {"name": "A", **TERM}))
    with pytest.raises(Conflict) as refused:
        renew(a, archived)
    assert "archived" in str(refused.value.detail)
    open_ended = contracts.create_contract(org, editor, {"name": "Open", "start_date": "2026-01-01"})
    with pytest.raises(Conflict) as refused:
        renew(a, open_ended)
    assert "end date first" in str(refused.value.detail)
    old = linked(a)
    renew(a, old)
    with pytest.raises(Conflict) as refused:
        renew(a, old)  # a double submit
    assert "already has a next term" in str(refused.value.detail)
    with pytest.raises(Conflict):
        renewal_action.renew(org, editor, old, {"decision": "cancel", "decision_notes": "x"})
    shelved = linked(a, name="Shelved")
    renewals.archive_renewal(org, editor, shelved.renewal_obligation)
    with pytest.raises(Conflict) as refused:
        renew(a, shelved)
    assert "is archived. Restore the renewal first." in str(refused.value.detail)


def test_a_renewal_driven_by_an_unrelated_contract_is_refused(a):
    """Older data only (every write path refuses two drivers): the renewal is driven by First, so Second can't decide
    it."""
    first = linked(a, name="First")
    obligation = first.renewal_obligation
    second = Contract.objects.create(
        organization=a["org"], name="Second", end_date=date(2027, 9, 30), renewal_obligation=obligation
    )
    with pytest.raises(Conflict) as refused:
        renew(a, second)
    assert str(refused.value.detail) == (
        "Its renewal takes its dates from the contract First. Unlink one of them first."
    )
    second.delete()  # leave the database as every write path would


def test_next_term_validation(a):
    old = linked(a)
    for data, field, message in (
        ({"start_date": "2027-01-01", "end_date": "2027-03-31"}, "end_date", "has to end after this one (2027-03-31)"),
        ({"start_date": "2027-01-01", "end_date": "2027-01-31"}, "end_date", "has to end after this one"),
        ({"start_date": "2027-03-01"}, "start_date", "Starts before this term ends (2027-03-31)"),
        ({"end_date": None}, "end_date", "Needed for the next term."),
        ({"amount": "-1"}, "amount", "can't be negative"),
        ({"lines": ["not-an-id"]}, "lines", "Send a list of this contract's line IDs."),
        ({"pools": [str(old.id)]}, "pools", "Choose license pool of this contract only."),
        ({"pool_action": "split"}, "pool_action", "Choose move or copy."),
        ({"decision": "maybe"}, "decision", "Choose renew, renegotiate or cancel."),
        ({"decided_on": "2999-01-01"}, "decided_on", "The date can't be in the future."),
        ({"predecessor": str(old.id)}, "predecessor", "The renew action doesn't take this field."),
    ):
        with pytest.raises(FieldErrors) as refused:
            renew(a, old, **data)
        assert message in refused.value.errors[field][0], (data, refused.value.errors)
    assert not Contract.objects.filter(predecessor=old).exists()
    overlap = renew(a, old, start_date="2027-03-01", overlap_confirmed=True)  # confirmed: both count while they overlap
    assert overlap.successor is not None and overlap.successor.start_date == date(2027, 3, 1)


def test_another_organizations_lines_and_pools_are_refused(a, b):
    old = linked(a)
    theirs = linked(b, name="Theirs")
    line = ContractItem.objects.filter(contract=theirs).first()
    pool = LicensePool.objects.get(contract=theirs)
    with pytest.raises(FieldErrors) as refused:
        renew(a, old, lines=[str(line.id)], pools=[str(pool.id)])  # type: ignore[union-attr]
    assert set(refused.value.errors) == {"lines", "pools"}


concurrent = pytest.mark.django_db(transaction=True)  # as the other concurrency tests in this suite


@concurrent
def test_two_renews_at_once_one_wins(a):
    old = linked(a)
    barrier = threading.Barrier(2)
    outcomes = []

    def go():
        try:
            barrier.wait()
            renew(a, Contract.objects.get(id=old.id))
            outcomes.append("ok")
        except Conflict:
            outcomes.append("refused")
        finally:
            connection.close()

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes) == ["ok", "refused"]
    assert Contract.objects.filter(predecessor=old).count() == 1
    assert RenewalCycle.objects.filter(obligation=old.renewal_obligation).count() == 2


# ---- audit and History ---------------------------------------------------------------------------------------------------


def test_every_write_is_audited_and_the_history_says_renewed_from(a, api):
    old = linked(a)
    obligation = old.renewal_obligation
    before = AuditEvent.objects.count()
    new = renew(a, old).successor
    assert new is not None
    actions = list(AuditEvent.objects.order_by("created_at", "id").values_list("action", flat=True)[before:])
    assert actions == [
        "contract.created",
        "contract_item.created",
        "contract_item.created",
        "license_pool.updated",
        "contract.decided",
        "renewal_cycle.decided",
        "renewal_cycle.completed",
        "renewal_cycle.established",
    ]
    created = AuditEvent.objects.get(action="contract.created", target_id=new.id)
    assert (created.changes["predecessor"], created.changes["renewed_from"]) == (str(old.id), "Atlassian Cloud")
    decided = AuditEvent.objects.get(action="renewal_cycle.decided", related_target_id=obligation.id)
    assert decided.changes["terms_change"]["old_amount"] == "1200.00"
    assert decided.changes["terms_change"]["new_end_date"] == "2028-03-31"
    viewer = api(a["users"]["viewer"])
    history = viewer.get(f"contracts/{new.id}/audit").json_body["results"]
    assert any(e["summary"] == "Atlassian Cloud, renewed from Atlassian Cloud" for e in history), history
    old_history = viewer.get(f"contracts/{old.id}/audit").json_body["results"]
    assert any(e["summary"] == "Renew: next term Atlassian Cloud" for e in old_history), old_history
    renewal_history = viewer.get(f"renewals/{obligation.id}/audit").json_body["results"]
    assert any(
        "1200.00 EUR a year → 1200.00 EUR a year; ends 2027-03-31 → 2028-03-31" in str(e) for e in renewal_history
    )


# ---- the API -------------------------------------------------------------------------------------------------------------


def test_the_renew_form_and_action_through_the_api(a, api):
    old = linked(a)
    viewer, editor = api(a["users"]["viewer"]), api(a["users"]["editor"])
    form = viewer.get(f"contracts/{old.id}/renew")
    assert form.status_code == 200
    body = form.json_body
    assert body["can_renew"] is True and body["refusal"] is None
    assert body["renewal"] == {"id": str(old.renewal_obligation_id), "name": "Atlassian Cloud"}
    assert body["open_cycle"]["sequence"] == 1 and body["open_cycle"]["renewal_date"] == "2027-03-31"
    assert body["defaults"]["start_date"] == "2027-04-01" and body["defaults"]["end_date"] == "2028-03-31"
    assert len(body["defaults"]["lines"]) == 2 and len(body["defaults"]["pools"]) == 1
    assert viewer.post(f"contracts/{old.id}/renew", {"decision": "renew"}).status_code == 403
    etag = editor.get(f"contracts/{old.id}")["ETag"]
    contracts.update_contract(a["org"], a["users"]["editor"], old, {"notes": "Changed since."})
    stale = editor.client.post(
        f"/api/v1/contracts/{old.id}/renew", {"decision": "renew"}, content_type="application/json", HTTP_IF_MATCH=etag
    )
    assert stale.status_code == 412
    refused = editor.post(f"contracts/{old.id}/renew", {"decision": "renew", "end_date": "2027-01-01"})
    assert refused.status_code == 400 and "end_date" in refused.json_body["errors"]
    made = editor.post(f"contracts/{old.id}/renew", {"decision": "renew", "amount": "1500"})
    assert made.status_code == 201, made.json_body
    new = made.json_body
    assert made["Location"] == f"http://testserver/api/v1/contracts/{new['id']}"  # the successor's own URL
    assert new["predecessor"] == {"id": str(old.id), "name": "Atlassian Cloud"} and new["drives_renewal"] is True
    assert new["renewal"]["cycle"]["sequence"] == 2 and new["renewal"]["cycle"]["renewal_date"] == "2028-03-31"
    assert new["renewal"]["history"][0]["terms_change"]["new_amount"] == "1500.00"
    assert new["renewal"]["history"][0]["decision"] == "renew"
    assert [p["sku_name"] for p in new["license_pools"]] == ["Standard"]
    assert {"seats_available", "overused", "seat_sync", "last_synced"} <= set(new["license_pools"][0])
    assert len(new["lines"]) == 2 and new["lines"][0]["annual_amount"] is not None
    detail = viewer.get(f"contracts/{old.id}").json_body
    assert detail["successor"] == {"id": new["id"], "name": "Atlassian Cloud"} and detail["drives_renewal"] is False
    again = viewer.get(f"contracts/{old.id}/renew").json_body
    assert again["can_renew"] is False and "already has a next term" in again["refusal"]
    assert editor.post(f"contracts/{old.id}/renew", {"decision": "renew"}).status_code == 409
    cancelled = editor.post(f"contracts/{new['id']}/renew", {"decision": "cancel", "decision_notes": "Dropped."})
    assert cancelled.status_code == 200 and cancelled.json_body["status"] == "cancelled"
    assert cancelled.json_body["drives_renewal"] is False


def test_the_detail_shows_the_renewal_and_its_cycles(a, api):
    old = linked(a)
    body = api(a["users"]["viewer"]).get(f"contracts/{old.id}").json_body
    renewal = body["renewal"]
    assert renewal["id"] == str(old.renewal_obligation_id) and renewal["contract"]["id"] == str(old.id)
    assert renewal["cycle"]["renewal_date"] == "2027-03-31" and renewal["cycle"]["terms_change"] is None
    assert renewal["history"] == [] and renewal["deadline"]["date"] == body["cancellation_deadline"]
    unlinked = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Solo"})
    assert api(a["users"]["viewer"]).get(f"contracts/{unlinked.id}").json_body["renewal"] is None
    form = api(a["users"]["viewer"]).get(f"contracts/{unlinked.id}/renew").json_body
    assert form["can_renew"] is False and "end date first" in form["refusal"] and form["open_cycle"] is None


def test_another_organizations_contract_is_404(a, b, api):
    theirs = linked(b, name="Theirs")
    editor = api(a["users"]["editor"])
    assert editor.get(f"contracts/{theirs.id}/renew").status_code == 404
    assert editor.post(f"contracts/{theirs.id}/renew", {"decision": "renew"}).status_code == 404
    assert not Contract.objects.filter(predecessor=theirs).exists()


def test_the_invariant_check_finds_two_drivers_and_a_stale_cycle(a):
    """tests/conftest.py's check is not vacuous."""
    first = linked(a, name="First")
    obligation = first.renewal_obligation
    second = Contract.objects.create(
        organization=a["org"], name="Second", end_date=date(2027, 9, 30), renewal_obligation=obligation
    )
    assert source_of_truth_problems() == ["First: 2 drivers (First, Second)"]
    second.delete()
    RenewalCycle.objects.filter(obligation=obligation).update(renewal_date=date(2030, 1, 1))
    assert source_of_truth_problems() == [
        "First: cycle renewal_date datetime.date(2030, 1, 1) != datetime.date(2027, 3, 31)"
    ]
    RenewalCycle.objects.filter(obligation=obligation).update(renewal_date=date(2027, 3, 31))
