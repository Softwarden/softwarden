"""Contracts and their renewal: the one source of truth when the newest term leaves its renewal (unlinked,
deleted, re-pointed, archived, cancelled or expired) and when two contracts link at once; the next cycle refused while
the contract's end date is stale; clearing a driving contract's end date; bad numbers as field errors; and the
`contracts` capability turned off."""

import threading
from datetime import date
from types import SimpleNamespace

import pytest
from django.db import connection

from catalog import attachments, customfields, quality, services
from core import capabilities as caps
from core.problems import Conflict, FieldErrors
from money import contracts, renewals
from money.models import Contract, RenewalCycle
from notify.attention import attention_queues

# After every scenario here (links, unlinks, re-points, archive, restore, status changes, races): at most one driver
# per renewal, and its open cycle and auto-renewal match it (tests/conftest.py one_source_of_truth).
pytestmark = [pytest.mark.django_db, pytest.mark.one_source_of_truth]
OLD = {"end_date": "2027-03-31", "notice_value": 1, "notice_unit": "months", "auto_renew": "yes"}


def renewal_deadline(org, obligation):
    """The deadline the renewal's reminders run on (Needs attention's renewal action row, which the digest reads)."""
    state = next(s for s in renewals.queue_states(org, services.org_today(org)) if s.obligation.id == obligation.id)
    queues, _ = attention_queues(org, SimpleNamespace(id=None), services.org_today(org))
    row = next((r for r in queues["renewal_actions"] if r["id"] == str(obligation.id)), None)
    if row is not None:
        assert row["deadline"] == (state.deadline.date.isoformat() if state.deadline.date else None)
    return state.deadline.date


def terms(a):
    """An old term with its renewal, and its successor linked to the same renewal (driving it)."""
    org, editor = a["org"], a["users"]["editor"]
    old = contracts.create_contract(org, editor, {"name": "Old", "create_renewal_obligation": True, **OLD})
    obligation = old.renewal_obligation
    assert obligation is not None
    successor = Contract.objects.create(organization=org, name="New", predecessor=old)
    successor = contracts.update_contract(
        org,
        editor,
        successor,
        {"end_date": "2028-03-31", "notice_value": 3, "notice_unit": "months", "auto_renew": "yes",
         "renewal_obligation": str(obligation.id)},
    )  # fmt: skip
    assert contracts.driver(obligation).id == successor.id  # type: ignore[union-attr]
    assert renewal_deadline(org, obligation) == successor.cancellation_deadline == date(2027, 12, 31)
    old.refresh_from_db()
    return old, successor, obligation


def falls_back_to_old(a, old, obligation):
    old.refresh_from_db()
    driving = contracts.driver(obligation)
    assert driving is not None and driving.id == old.id
    assert RenewalCycle.objects.get(obligation=obligation).renewal_date == date(2027, 3, 31)
    assert renewal_deadline(a["org"], obligation) == old.cancellation_deadline == date(2027, 2, 28)


def test_unlinking_the_successor_hands_the_renewal_back(a):
    old, successor, obligation = terms(a)
    contracts.update_contract(a["org"], a["users"]["editor"], successor, {"renewal_obligation": None})
    falls_back_to_old(a, old, obligation)


def test_deleting_the_successor_hands_the_renewal_back(a):
    old, successor, obligation = terms(a)
    contracts.delete_contract(a["org"], a["users"]["admin"], successor)
    falls_back_to_old(a, old, obligation)


def test_repointing_the_successor_syncs_both_renewals(a):
    org, editor = a["org"], a["users"]["editor"]
    old, successor, obligation = terms(a)
    other = renewals.create_renewal(org, editor, {"name": "Other", "renewal_date": "2026-01-01"})
    successor = contracts.update_contract(org, editor, successor, {"renewal_obligation": str(other.id)})
    falls_back_to_old(a, old, obligation)
    assert RenewalCycle.objects.get(obligation=other).renewal_date == date(2028, 3, 31)
    assert renewal_deadline(org, other) == successor.cancellation_deadline


@pytest.mark.parametrize("how", ["archived", "cancelled", "expired"])
def test_a_successor_that_stops_counting_hands_the_renewal_back(a, how):
    org, editor = a["org"], a["users"]["editor"]
    old, successor, obligation = terms(a)
    if how == "archived":
        contracts.archive_contract(org, editor, successor)
    else:
        contracts.update_contract(org, editor, successor, {"status": how})
    falls_back_to_old(a, old, obligation)
    successor.refresh_from_db()
    assert successor.renewal_obligation_id == obligation.id  # the link stays, for history
    if how == "archived":
        contracts.restore_contract(org, editor, successor)
    else:
        contracts.update_contract(org, editor, successor, {"status": "active"})
    assert contracts.driver(obligation).id == successor.id  # type: ignore[union-attr]
    assert renewal_deadline(org, obligation) == date(2027, 12, 31)


def test_with_no_contract_left_driving_the_renewal_is_editable_again(a, api):
    org, editor = a["org"], a["users"]["editor"]
    contract = contracts.create_contract(org, editor, {"name": "Only", "create_renewal_obligation": True, **OLD})
    obligation = contract.renewal_obligation
    assert obligation is not None
    contracts.archive_contract(org, editor, contract)
    assert contracts.driver(obligation) is None
    cycle = RenewalCycle.objects.get(obligation=obligation)
    response = api(editor).patch(f"renewals/{obligation.id}/cycles/{cycle.id}", {"renewal_date": "2027-12-31"})
    assert response.status_code == 200 and response.json_body["contract"] is None
    body = api(editor).get(f"contracts/{contract.id}").json_body
    assert body["drives_renewal"] is False


concurrent = pytest.mark.django_db(transaction=True)  # as the other concurrency tests in this suite


@concurrent
def test_two_contracts_linking_one_renewal_at_once_are_checked_one_after_the_other(a):
    org, editor = a["org"], a["users"]["editor"]
    obligation = renewals.create_renewal(org, editor, {"name": "Shared", "renewal_date": "2027-01-01"})
    barrier = threading.Barrier(2)
    outcomes = []

    def link(name):
        try:
            barrier.wait()
            contracts.create_contract(
                org, editor, {"name": name, "end_date": "2027-06-30", "renewal_obligation": str(obligation.id)}
            )
            outcomes.append("ok")
        except FieldErrors:
            outcomes.append("refused")
        finally:
            connection.close()

    threads = [threading.Thread(target=link, args=(n,)) for n in ("First", "Second")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes) == ["ok", "refused"]
    assert Contract.objects.filter(renewal_obligation=obligation).count() == 1


def test_the_next_cycle_waits_for_the_contracts_new_end_date(a):
    org, editor = a["org"], a["users"]["editor"]
    contract = contracts.create_contract(org, editor, {"name": "C", "create_renewal_obligation": True, **OLD})
    obligation = contract.renewal_obligation
    cycle = RenewalCycle.objects.get(obligation=obligation)
    renewals.record_decision(org, editor, obligation, cycle.id, {"decision": "renew"})
    today = services.org_today(org).isoformat()
    renewals.record_completion(org, editor, obligation, cycle.id, {"completed_on": today, "completion_evidence": "PO"})
    with pytest.raises(Conflict) as refused:
        renewals.establish_next_cycle(org, editor, obligation, {})
    assert "Update the contract's end date first" in str(refused.value.detail)
    assert RenewalCycle.objects.filter(obligation=obligation).count() == 1
    contracts.update_contract(org, editor, contract, {"end_date": "2027-03-31"})  # not after: still refused
    with pytest.raises(Conflict):
        renewals.establish_next_cycle(org, editor, obligation, {})
    contracts.update_contract(org, editor, contract, {"end_date": "2028-03-31"})
    assert renewals.establish_next_cycle(org, editor, obligation, {}).renewal_date == date(2028, 3, 31)


def test_a_linked_contracts_end_date_cannot_be_cleared(a, api):
    """Driving or not: a renewal that falls back to a linked contract always finds an end date. Only
    the PATCH that unlinks it may clear it."""
    editor = a["users"]["editor"]
    old, successor, obligation = terms(a)
    for contract in (successor, old):  # the driver, and the older term it renews
        refused = api(editor).patch(f"contracts/{contract.id}", {"end_date": None})
        assert refused.status_code == 400
        assert refused.json_body["errors"]["end_date"] == [contracts.NEEDS_END]
    unlinked = api(editor).patch(f"contracts/{successor.id}", {"end_date": None, "renewal_obligation": None})
    assert unlinked.status_code == 200
    falls_back_to_old(a, old, obligation)


def first_then_second(a, how):
    """First links a renewal, stops counting (archived, cancelled or expired), and Second links the same renewal."""
    org, editor = a["org"], a["users"]["editor"]
    first = contracts.create_contract(org, editor, {"name": "First", "create_renewal_obligation": True, **OLD})
    obligation = first.renewal_obligation
    assert obligation is not None
    if how == "archived":
        contracts.archive_contract(org, editor, first)
    else:
        contracts.update_contract(org, editor, first, {"status": how})
    second = contracts.create_contract(
        org,
        editor,
        {"name": "Second", "end_date": "2028-06-30", "notice_value": 3, "notice_unit": "months", "auto_renew": "yes",
         "renewal_obligation": str(obligation.id)},
    )  # fmt: skip
    assert contracts.driver(obligation).id == second.id  # type: ignore[union-attr]
    return first, second, obligation


def second_still_drives(a, second, obligation):
    second.refresh_from_db()
    assert contracts.driver(obligation).id == second.id  # type: ignore[union-attr]
    assert RenewalCycle.objects.get(obligation=obligation).renewal_date == date(2028, 6, 30)
    assert renewal_deadline(a["org"], obligation) == second.cancellation_deadline == date(2028, 3, 30)


def test_restoring_an_older_contract_cannot_take_the_renewal_back(a, api):
    first, second, obligation = first_then_second(a, "archived")
    with pytest.raises(Conflict) as refused:
        contracts.restore_contract(a["org"], a["users"]["editor"], first)
    assert str(refused.value.detail) == (
        "This renewal now takes its dates from the contract Second. Unlink one of them first."
    )
    response = api(a["users"]["editor"]).post(f"contracts/{first.id}/restore")
    assert response.status_code == 409
    first.refresh_from_db()
    assert first.archived_at is not None
    second_still_drives(a, second, obligation)
    # Unlinking one of them lets it come back.
    contracts.update_contract(a["org"], a["users"]["editor"], first, {"renewal_obligation": None})
    assert api(a["users"]["editor"]).post(f"contracts/{first.id}/restore").status_code == 200
    second_still_drives(a, second, obligation)


@pytest.mark.parametrize("how", ["cancelled", "expired"])
def test_reactivating_an_older_contract_cannot_take_the_renewal_back(a, api, how):
    first, second, obligation = first_then_second(a, how)
    for status in ("active", "pending_renewal", "signed"):
        refused = api(a["users"]["editor"]).patch(f"contracts/{first.id}", {"status": status})
        assert refused.status_code == 400
        assert refused.json_body["errors"]["status"] == [
            "This renewal now takes its dates from the contract Second. Unlink one of them first."
        ]
    # Resending the link it already has is not a re-link: still refused.
    same = api(a["users"]["editor"]).patch(
        f"contracts/{first.id}", {"status": "active", "renewal_obligation": str(obligation.id)}
    )
    assert same.status_code == 400 and "status" in same.json_body["errors"]
    first.refresh_from_db()
    assert first.status == how
    second_still_drives(a, second, obligation)
    # Moving between statuses that don't count is allowed, and unlinking it in the same edit lets it count again.
    assert api(a["users"]["editor"]).patch(f"contracts/{first.id}", {"status": "expired"}).status_code == 200
    unlinked = api(a["users"]["editor"]).patch(
        f"contracts/{first.id}", {"status": "active", "renewal_obligation": None}
    )
    assert unlinked.status_code == 200


def test_a_term_may_count_again_under_its_own_successor(a):
    """Reviving a contract whose renewal is driven by the term that renews it changes nothing: the successor drives.
    Its term hasn't ended, so the "renewed by" guard (ended terms, or straight from Renewed) doesn't apply."""
    old, successor, obligation = terms(a)
    contracts.archive_contract(a["org"], a["users"]["editor"], old)
    contracts.restore_contract(a["org"], a["users"]["editor"], old)
    contracts.update_contract(a["org"], a["users"]["editor"], old, {"status": "cancelled"})
    contracts.update_contract(a["org"], a["users"]["editor"], old, {"status": "active"})
    assert contracts.driver(obligation).id == successor.id  # type: ignore[union-attr]
    assert renewal_deadline(a["org"], obligation) == date(2027, 12, 31)


def test_set_items_refuses_records_out_of_scope(a, b):
    contract = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Scoped"})
    with pytest.raises(ValueError):
        contracts.set_items(a["org"], a["users"]["editor"], contract, [(b["app"], None)])


@pytest.mark.parametrize("amount", ["1e30", "9" * 31, "NaN", "Infinity", "-Infinity", 1e30])
def test_huge_and_non_finite_amounts_are_field_errors(a, api, amount):
    editor = api(a["users"]["editor"])
    made = editor.post("contracts", {"name": "Big", "amount": amount})
    assert made.status_code == 400 and "amount" in made.json_body["errors"], made.json_body
    preview = editor.get(f"contracts/preview?amount={amount}")
    assert preview.status_code == 400 and "amount" in preview.json_body["errors"]


def test_minus_zero_is_zero(a):
    made = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Free", "amount": "-0"})
    assert str(made.amount) == "0.00"


def test_non_ascii_digits_are_field_errors(a, api):
    editor = api(a["users"]["editor"])
    pool = editor.post("license-pools", {"application": str(a["app"].id), "sku_name": "E5", "seats_purchased": "²"})
    assert pool.status_code == 400 and "seats_purchased" in pool.json_body["errors"]
    contract = editor.post("contracts", {"name": "N", "notice_value": "²", "notice_unit": "days"})
    assert contract.status_code == 400 and "notice_value" in contract.json_body["errors"]
    renewal = editor.post("renewals", {"name": "R", "notice_value": "²", "notice_unit": "days"})
    assert renewal.status_code == 400 and "notice_value" in renewal.json_body["errors"]


# ---- the contracts capability turned off -------------------------------------------------------------------------------


@pytest.fixture
def without_contracts(monkeypatch):
    real = caps.capabilities
    monkeypatch.setattr(caps, "capabilities", lambda org: real(org) - {caps.CONTRACTS})


@pytest.fixture
def held(a):
    org, editor = a["org"], a["users"]["editor"]
    contract = contracts.create_contract(org, editor, {"name": "Hidden deal", "end_date": "2027-03-31"})
    line = contracts.add_item(org, editor, contract, {"record": str(a["app"].id)})
    pool = contracts.create_pool(org, editor, {"application": str(a["app"].id), "sku_name": "Hidden SKU"})
    link = attachments.add_link(org, editor, contract, {"url": "https://example.com/hidden", "title": "Hiddenmsa"})
    return SimpleNamespace(contract=contract, line=line, pool=pool, link=link)


ROUTES = [
    ("get", "contracts"),
    ("post", "contracts"),
    ("get", "contracts/preview"),
    ("get", "contracts/export.csv"),
    ("get", "contracts/{c}"),
    ("patch", "contracts/{c}"),
    ("post", "contracts/{c}/archive"),
    ("get", "contracts/{c}/lines"),
    ("patch", "contracts/{c}/lines/{l}"),
    ("get", "contracts/{c}/attachments"),
    ("get", "contracts/{c}/audit"),
    ("get", "attachments/{a}"),
    ("get", "license-pools"),
    ("get", "license-pools/{p}"),
    ("get", "license-pools/{p}/audit"),
    # Renewal and licences
    ("get", "contracts/{c}/renew"),
    ("post", "contracts/{c}/renew"),
    ("get", "license-pools/totals"),
    ("get", "license-pools/seat-review"),
    ("post", "license-pools/{p}/seat-review/{p}/apply"),
    ("post", "license-pools/{p}/seat-review/{p}/dismiss"),
]


@pytest.mark.parametrize("method,route", ROUTES)
def test_every_contract_route_needs_the_capability(a, api, held, without_contracts, method, route):
    url = route.format(c=held.contract.id, l=held.line.id, p=held.pool.id, a=held.link.id)
    client = api(a["users"]["admin"])
    response = client.get(url) if method == "get" else getattr(client, method)(url, {"name": "x"})
    assert response.status_code == 403, url
    assert response.json_body["type"] == "/problems/capability-required"
    assert response.json_body["capability"] == "contracts"


def test_without_the_capability_contracts_are_out_of_every_other_view(a, api, held, without_contracts):
    org = a["org"]
    viewer = api(a["users"]["viewer"])
    body = viewer.get("search?q=hidden").json_body
    assert "contract" not in [g["kind"] for g in body["groups"]]
    assert viewer.get("search?q=hidden&kinds=contract").status_code == 403
    assert viewer.get("search?q=hiddenmsa&kinds=attachment").json_body["groups"][0]["results"] == []
    graph = viewer.get(f"applications/{a['app'].id}/map").json_body
    assert {e["category"] for e in graph["edges"]} & {"contract", "license_pool"} == set()
    assert "contract" not in [c["key"] for c in graph["categories"]]
    assert quality.status(quality.BY_ID["contract"], org)[0] == quality.UNAVAILABLE
    history = viewer.get(f"applications/{a['app'].id}/audit").json_body["results"]
    assert not [e for e in history if e["action"].startswith("contract_item.")]
    refused = api(a["users"]["admin"]).post(
        "custom-fields", {"name": "budget", "label": "Budget", "type": "text", "applies_to": ["contract"]}
    )
    assert refused.status_code == 400 and "capability" in refused.json_body["errors"]["applies_to"][0]


def test_without_the_capability_contract_dates_remind_nobody(a, monkeypatch):
    org = a["org"]
    customfields.create_field(
        org,
        a["users"]["admin"],
        {
            "name": "review_on",
            "label": "Review on",
            "type": "date",
            "applies_to": ["contract"],
            "is_reminder_source": True,
        },
    )
    contracts.create_contract(
        org, a["users"]["editor"], {"name": "C", "custom_field_data": {"review_on": "2027-01-15"}}
    )
    assert len(customfields.reminder_sources(org)) == 1
    real = caps.capabilities
    monkeypatch.setattr(caps, "capabilities", lambda o: real(o) - {caps.CONTRACTS})
    assert customfields.reminder_sources(org) == []


@pytest.mark.parametrize("how", ["cancelled", "expired"])
def test_a_form_that_sends_every_field_back_cannot_revive_an_older_contract(a, api, how):
    """The SPA's edit form PATCHes every field it shows, the link included."""
    first, second, obligation = first_then_second(a, how)
    editor = api(a["users"]["editor"])
    shown = editor.get(f"contracts/{first.id}").json_body
    form = {
        "name": shown["name"],
        "vendor": shown["vendor"] and shown["vendor"]["id"],
        "contract_number": shown["contract_number"],
        "po_number": shown["po_number"],
        "amount": shown["amount"],
        "currency": shown["currency"],
        "billing_frequency": shown["billing_frequency"],
        "start_date": shown["start_date"],
        "end_date": shown["end_date"],
        "auto_renew": shown["auto_renew"],
        "notice_value": shown["notice"] and shown["notice"]["value"],
        "notice_unit": shown["notice"]["unit"] if shown["notice"] else "",
        "renewal_owner": shown["renewal_owner"] and shown["renewal_owner"]["id"],
        "status": "active",
        "cost_center": shown["cost_center"],
        "notes": shown["notes"],
        "renewal_obligation": shown["renewal_obligation"]["id"],
        "custom_field_data": shown["custom_field_data"],
    }
    refused = editor.patch(f"contracts/{first.id}", form)
    assert refused.status_code == 400, refused.json_body
    assert set(refused.json_body["errors"]) == {"status"}
    assert editor.patch(f"contracts/{first.id}", {**form, "status": how}).status_code == 200  # unchanged: fine
    second_still_drives(a, second, obligation)


@pytest.mark.parametrize(
    "write",
    [
        lambda org, user, o, c: renewals.update_renewal(org, user, o, {"name": "Renamed", "auto_renew": "no"}),
        lambda org, user, o, c: renewals.update_cycle(org, user, o, c.id, {"renewal_date": "2030-01-01"}),
    ],
    ids=["update_renewal", "update_cycle"],
)
def test_renewal_writes_roll_back_whole_when_their_audit_fails(a, monkeypatch, write):
    """Called directly (not inside a request's transaction), a renewal write is one unit: the save and its event go
    together, so a failure writing the event leaves the renewal as it was."""
    org, editor = a["org"], a["users"]["editor"]
    obligation = renewals.create_renewal(org, editor, {"name": "Atomic", "renewal_date": "2027-01-01"})
    cycle = RenewalCycle.objects.get(obligation=obligation)

    def broken(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(renewals, "audit", broken)
    with pytest.raises(RuntimeError):
        write(org, editor, obligation, cycle)
    obligation.refresh_from_db()
    cycle.refresh_from_db()
    assert (obligation.name, obligation.auto_renew) == ("Atomic", "unknown")
    assert cycle.renewal_date == date(2027, 1, 1)


def test_establishing_the_next_cycle_rolls_back_whole(a, monkeypatch):
    org, editor = a["org"], a["users"]["editor"]
    obligation = renewals.create_renewal(
        org, editor, {"name": "Atomic", "renewal_date": "2027-01-01", "term_value": 1, "term_unit": "years"}
    )
    cycle = RenewalCycle.objects.get(obligation=obligation)
    renewals.record_decision(org, editor, obligation, cycle.id, {"decision": "renew"})
    today = services.org_today(org).isoformat()
    renewals.record_completion(org, editor, obligation, cycle.id, {"completed_on": today, "completion_evidence": "PO"})

    def broken(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(renewals, "audit", broken)
    with pytest.raises(RuntimeError):
        renewals.establish_next_cycle(org, editor, obligation, {})
    assert RenewalCycle.objects.filter(obligation=obligation).count() == 1
