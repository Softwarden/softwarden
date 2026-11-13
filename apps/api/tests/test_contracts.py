"""Contracts, contract lines and licence pools (the service layer and the API): the annualised cost for every billing frequency, the cancellation deadline with month-end clamping,
the one source of truth with a linked renewal, the services' audit, the API's filters,
ETags, exports and roles, and the joins with search, the record map, custom fields, attachments and data quality.
The allocation integrity tests are in tests/test_allocation_integrity.py."""

import csv
import io
import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from catalog import customfields, quality, services
from catalog.models import Attachment, Vendor
from core.models import AuditEvent
from core.problems import Conflict, FieldErrors
from money import contracts, renewals
from money.contracts import annualize, deadline_of, term_months
from money.models import Contract, ContractItem, LicensePool, RenewalCycle, RenewalObligation
from notify.attention import attention_queues

pytestmark = [pytest.mark.django_db, pytest.mark.one_source_of_truth]  # tests/conftest.py
D = Decimal


def new(a, **data):
    return contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Atlassian Cloud", **data})


def component(a, name="Sync job"):
    return services.create_resource(
        a["org"], a["users"]["editor"], {"name": name, "kind": "component", "lifecycle": "active"}
    )


def app(a, name, **extra):
    return services.create_resource(a["org"], a["users"]["editor"], {"name": name, "lifecycle": "active", **extra})


# ---- annualised cost: every billing frequency -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "frequency", "start", "end", "expected"),
    [
        ("100", "monthly", None, None, "1200.00"),
        ("250", "quarterly", None, None, "1000.00"),
        ("600", "semiannual", None, None, "1200.00"),
        ("5000", "annual", None, None, "5000.00"),
        ("30000", "multi_year", date(2025, 1, 1), date(2027, 12, 31), "10000.00"),  # 36 months
        ("1000", "multi_year", date(2025, 1, 1), date(2027, 12, 31), "333.33"),  # half up, to the cent
        ("24000", "multi_year", date(2026, 7, 1), date(2028, 6, 30), "12000.00"),
        ("30000", "multi_year", None, None, "30000.00"),  # no dates: counted in full
        ("30000", "multi_year", date(2026, 1, 1), None, "30000.00"),
        ("6000", "multi_year", date(2026, 1, 1), date(2026, 6, 30), "6000.00"),  # under a year counts as one
        ("999", "one_time", None, None, "999.00"),
        ("700", "usage_based", None, None, "700.00"),
        ("0.01", "monthly", None, None, "0.12"),
        ("33.335", "annual", None, None, "33.34"),
    ],
)
def test_annualize_every_billing_frequency(amount, frequency, start, end, expected):
    assert annualize(D(amount), frequency, start, end) == D(expected)


def test_annualize_without_an_amount_is_none():
    assert annualize(None, "monthly") is None


@pytest.mark.parametrize(
    ("start", "end", "months"),
    [
        (date(2025, 1, 1), date(2027, 12, 31), 36),
        (date(2026, 1, 1), date(2026, 12, 31), 12),
        (date(2026, 1, 1), date(2028, 6, 30), 30),
        (date(2026, 1, 1), date(2026, 3, 31), 12),  # at least a year
        (date(2026, 1, 1), date(2026, 1, 1), None),  # the end isn't after the start
        (None, date(2026, 1, 1), None),
    ],
)
def test_term_months(start, end, months):
    assert term_months(start, end) == months


def test_annualization_notes_say_how():
    assert contracts.annualization_note(D(1), "monthly", None, None) == "Monthly amount × 12"
    assert contracts.annualization_note(D(1), "semiannual", None, None) == "Six-monthly amount × 2"
    assert contracts.annualization_note(D(1), "multi_year", date(2025, 1, 1), date(2027, 12, 31)) == (
        "Term total ÷ 3 years"
    )
    assert "counted in full" in contracts.annualization_note(D(1), "multi_year", None, None)
    assert contracts.annualization_note(None, "monthly", None, None) == ""


def test_the_service_stores_the_derived_values_and_recomputes_them(a):
    contract = new(a, amount="100", billing_frequency="monthly", end_date="2026-12-31", notice_value=60,
                   notice_unit="days", auto_renew="yes")  # fmt: skip
    assert contract.annualized_cost == D("1200.00")
    assert contract.cancellation_deadline == date(2026, 11, 1)
    contract = contracts.update_contract(a["org"], a["users"]["editor"], contract, {"billing_frequency": "quarterly"})
    assert contract.annualized_cost == D("400.00")
    contract = contracts.update_contract(a["org"], a["users"]["editor"], contract, {"end_date": None})
    assert contract.cancellation_deadline is None


# ---- cancellation deadline: notice periods and month-end clamping ----------------------------------------------------


@pytest.mark.parametrize(
    ("end", "value", "unit", "expected"),
    [
        (date(2027, 1, 31), 1, "months", date(2026, 12, 31)),  # 31 January minus a month stays at month end
        (date(2027, 3, 31), 1, "months", date(2027, 2, 28)),  # clamped to February's last day
        (date(2028, 3, 31), 1, "months", date(2028, 2, 29)),  # a leap year
        (date(2027, 3, 31), 2, "months", date(2027, 1, 31)),
        (date(2027, 2, 28), 1, "months", date(2027, 1, 28)),
        (date(2028, 2, 29), 1, "years", date(2027, 2, 28)),  # 29 February a year back is 28 February
        (date(2029, 2, 28), 1, "years", date(2028, 2, 28)),
        (date(2026, 12, 31), 60, "days", date(2026, 11, 1)),
        (date(2028, 3, 1), 1, "days", date(2028, 2, 29)),
        (date(2027, 5, 31), 3, "months", date(2027, 2, 28)),
    ],
)
def test_notice_period_deadlines_clamp_to_month_end(a, end, value, unit, expected):
    for auto_renew in ("yes", "unknown"):
        assert deadline_of(auto_renew, end, value, unit, 90).date == expected
        contract = new(a, end_date=end.isoformat(), notice_value=value, notice_unit=unit, auto_renew=auto_renew)
        assert contract.cancellation_deadline == expected


def test_deadline_labels_follow_the_renewal_rules(a):
    end = date(2027, 6, 30)
    with_notice = deadline_of("yes", end, 3, "months", 90)
    assert (with_notice.date, with_notice.label) == (date(2027, 3, 30), "calculated")
    assumed = deadline_of("unknown", end, None, "", 90)
    assert (assumed.date, assumed.label) == (end - timedelta(days=90), "assumed")
    not_renewing = deadline_of("no", end, 3, "months", 30)
    assert (not_renewing.date, not_renewing.label) == (end - timedelta(days=30), "renewal_action")
    assert deadline_of("yes", None, 3, "months", 90).date is None


def test_a_lead_time_change_refreshes_stored_deadlines(a, api):
    assumed = new(a, end_date="2027-06-30")
    noticed = new(a, end_date="2027-06-30", notice_value=1, notice_unit="months", auto_renew="yes")
    assert assumed.cancellation_deadline == date(2027, 6, 30) - timedelta(days=90)
    assert api(a["users"]["admin"]).patch("organization", {"renewal_lead_days": 30}).status_code == 200
    assumed.refresh_from_db()
    noticed.refresh_from_db()
    assert assumed.cancellation_deadline == date(2027, 5, 31)
    assert noticed.cancellation_deadline == date(2027, 5, 30)


# ---- one source of truth -------------------------------------------------------------------------


def queue_deadline(org, obligation):
    """The deadline the renewal's reminders run on: Needs attention's renewal action row, which the digest reads
    (notify/digest.plan), and the renewal page's state."""
    queues, _ = attention_queues(org, SimpleNamespace(id=None), services.org_today(org))
    row = next((r for r in queues["renewal_actions"] if r["id"] == str(obligation.id)), None)
    state = renewals.queue_states(org, services.org_today(org))
    found = next(s for s in state if s.obligation.id == obligation.id)
    if row is not None:
        assert row["deadline"] == (found.deadline.date.isoformat() if found.deadline.date else None)
    return found.deadline.date


INPUTS: list[dict[str, Any]] = [
    {"end_date": "2027-03-31", "notice_value": 1, "notice_unit": "months", "auto_renew": "yes"},
    {"end_date": "2028-03-31", "notice_value": 1, "notice_unit": "months", "auto_renew": "unknown"},
    {"end_date": "2027-01-31", "notice_value": 1, "notice_unit": "months", "auto_renew": "yes"},
    {"end_date": "2028-02-29", "notice_value": 1, "notice_unit": "years", "auto_renew": "yes"},
    {"end_date": "2027-06-30", "notice_value": None, "notice_unit": "", "auto_renew": "yes"},  # assumed
    {"end_date": "2027-06-30", "notice_value": 2, "notice_unit": "months", "auto_renew": "no"},  # renewal action
    {"end_date": "2026-12-31", "notice_value": 60, "notice_unit": "days", "auto_renew": "unknown"},
]  # clearing the end date of a driving contract is refused (tests/test_contract_renewal_links.py)


def test_an_obligations_reminders_and_its_contracts_deadline_never_disagree(a, api):
    """The acceptance check: created with the contract, then through every kind of edit, a linked renewal's reminder
    deadline is the contract's cancellation deadline."""
    org, editor = a["org"], a["users"]["editor"]
    contract = new(a, create_renewal_obligation=True, **INPUTS[0])
    obligation = contract.renewal_obligation
    assert obligation is not None and obligation.name == contract.name
    assert queue_deadline(org, obligation) == contract.cancellation_deadline == date(2027, 2, 28)
    for inputs in INPUTS[1:]:
        contract = contracts.update_contract(org, editor, contract, inputs)
        contract.refresh_from_db()
        assert queue_deadline(org, obligation) == contract.cancellation_deadline, inputs
        body = api(a["users"]["viewer"]).get(f"renewals/{obligation.id}").json_body
        mine = api(a["users"]["viewer"]).get(f"contracts/{contract.id}").json_body
        assert body["deadline"]["date"] == mine["deadline"]["date"] == mine["cancellation_deadline"], inputs
        assert body["deadline"]["label"] == mine["deadline"]["label"], inputs
        assert body["contract"] == {"id": str(contract.id), "name": contract.name}
    # The organization's lead time moves Assumed deadlines on both sides together.
    contract = contracts.update_contract(
        org, editor, contract, {"end_date": "2027-06-30", "notice_value": None, "notice_unit": ""}
    )
    assert contract.cancellation_deadline == date(2027, 6, 30) - timedelta(days=90)
    assert api(a["users"]["admin"]).patch("organization", {"renewal_lead_days": 45}).status_code == 200
    contract.refresh_from_db()
    org.refresh_from_db()
    assert queue_deadline(org, obligation) == contract.cancellation_deadline == date(2027, 5, 16)


def test_the_renewal_refuses_to_change_what_its_contract_owns(a, api):
    contract = new(a, create_renewal_obligation=True, **INPUTS[0])
    obligation = contract.renewal_obligation
    cycle = RenewalCycle.objects.get(obligation=obligation)
    editor = api(a["users"]["editor"])
    for change in (
        {"renewal_date": "2027-12-31"},
        {"explicit_deadline": "2027-01-01"},
        {"notice_value": 3, "notice_unit": "months"},
    ):
        refused = editor.patch(f"renewals/{obligation.id}/cycles/{cycle.id}", change)
        assert refused.status_code == 400, change
        assert "contract Atlassian Cloud" in next(iter(refused.json_body["errors"].values()))[0]
    refused = editor.patch(f"renewals/{obligation.id}", {"auto_renew": "no"})
    assert refused.status_code == 400 and "auto_renew" in refused.json_body["errors"]
    # Sending back what it already has is fine (a form sends every field), and so is the rest of the cycle.
    same = editor.patch(
        f"renewals/{obligation.id}/cycles/{cycle.id}",
        {
            "renewal_date": "2027-03-31",
            "notice_value": 1,
            "notice_unit": "months",
            "term_value": 1,
            "term_unit": "years",
        },
    )
    assert same.status_code == 200, same.json_body
    assert editor.patch(f"renewals/{obligation.id}", {"auto_renew": "yes", "name": "Atlassian"}).status_code == 200
    contract.refresh_from_db()
    assert queue_deadline(a["org"], obligation) == contract.cancellation_deadline


def test_unlinking_gives_the_renewal_its_dates_back(a, api):
    contract = new(a, create_renewal_obligation=True, **INPUTS[0])
    obligation = contract.renewal_obligation
    cycle = RenewalCycle.objects.get(obligation=obligation)
    contracts.update_contract(a["org"], a["users"]["editor"], contract, {"renewal_obligation": None})
    changed = api(a["users"]["editor"]).patch(
        f"renewals/{obligation.id}/cycles/{cycle.id}", {"renewal_date": "2027-12-31"}
    )
    assert changed.status_code == 200
    assert changed.json_body["contract"] is None


def test_linking_an_existing_renewal_takes_over_its_dates(a):
    org, editor = a["org"], a["users"]["editor"]
    obligation = renewals.create_renewal(
        org,
        editor,
        {"name": "Workday", "auto_renew": "no", "renewal_date": "2027-01-01", "explicit_deadline": "2026-10-01"},
    )
    contract = new(a, renewal_obligation=str(obligation.id), **INPUTS[0])
    cycle = RenewalCycle.objects.get(obligation=obligation)
    obligation.refresh_from_db()
    assert (cycle.renewal_date, cycle.explicit_deadline, cycle.notice_value) == (date(2027, 3, 31), None, 1)
    assert obligation.auto_renew == "yes"
    assert queue_deadline(org, obligation) == contract.cancellation_deadline
    events = AuditEvent.objects.filter(related_target_id=obligation.id, action="renewal_cycle.updated")
    assert events.get().changes["contract"] == str(contract.id)
    assert AuditEvent.objects.get(action="renewal.updated", target_id=obligation.id).changes["auto_renew"] == [
        "no",
        "yes",
    ]


def test_the_next_cycle_takes_the_contracts_dates(a):
    org, editor = a["org"], a["users"]["editor"]
    contract = new(a, create_renewal_obligation=True, **INPUTS[0])
    obligation = contract.renewal_obligation
    cycle = RenewalCycle.objects.get(obligation=obligation)
    renewals.record_decision(org, editor, obligation, cycle.id, {"decision": "renew"})
    renewals.record_completion(
        org,
        editor,
        obligation,
        cycle.id,
        {"completed_on": services.org_today(org).isoformat(), "completion_evidence": "PO 1"},
    )
    contract = contracts.update_contract(org, editor, contract, {"end_date": "2028-03-31"})  # no open cycle: kept
    with pytest.raises(FieldErrors) as refused:
        renewals.establish_next_cycle(org, editor, obligation, {"renewal_date": "2029-01-01"})
    assert "renewal_date" in refused.value.errors
    following = renewals.establish_next_cycle(org, editor, obligation, {})
    assert following.renewal_date == date(2028, 3, 31)
    assert queue_deadline(org, obligation) == contract.cancellation_deadline == date(2028, 2, 29)


def test_links_that_would_make_two_sources_are_refused(a):
    org, editor = a["org"], a["users"]["editor"]
    first = new(a, create_renewal_obligation=True, **INPUTS[0])
    obligation = first.renewal_obligation
    cases = [
        ({"name": "Second", "end_date": "2027-03-31", "renewal_obligation": str(obligation.id)}, "already takes"),
        ({"name": "No end", "renewal_obligation": str(obligation.id)}, "end date"),
        ({"name": "No end", "create_renewal_obligation": True}, "end date"),
        (
            {
                "name": "Both",
                "end_date": "2027-03-31",
                "create_renewal_obligation": True,
                "renewal_obligation": str(obligation.id),
            },
            "not both",
        ),  # fmt: skip
        ({"name": "Bad", "end_date": "2027-03-31", "create_renewal_obligation": "yes"}, "true or false"),
    ]
    for data, words in cases:
        with pytest.raises(FieldErrors) as refused:
            contracts.create_contract(org, editor, data)
        assert words in str(refused.value.errors), data
    renewals.archive_renewal(org, editor, (other := renewals.create_renewal(org, editor, {"name": "Old"})))
    with pytest.raises(FieldErrors) as refused:
        contracts.create_contract(
            org, editor, {"name": "X", "end_date": "2027-01-01", "renewal_obligation": str(other.id)}
        )
    assert "archived" in str(refused.value.errors)
    with pytest.raises(Conflict) as blocked:
        renewals.delete_renewal(org, a["users"]["admin"], obligation)
    assert blocked.value.extra["blocking_references"] == {"linked contract": 1}


def test_a_successor_term_takes_over_the_renewal(a):
    """The renew action (next package) links the next term to the same renewal: the newest term drives it."""
    org, editor = a["org"], a["users"]["editor"]
    old = new(a, create_renewal_obligation=True, **INPUTS[0])
    obligation = old.renewal_obligation
    successor = Contract.objects.create(
        organization=org, name="Atlassian Cloud 2028", predecessor=old, end_date=date(2028, 3, 31)
    )
    successor = contracts.update_contract(org, editor, successor, {"renewal_obligation": str(obligation.id)})
    driving = contracts.driver(obligation)
    assert driving is not None and driving.id == successor.id
    cycle = RenewalCycle.objects.get(obligation=obligation)
    assert cycle.renewal_date == date(2028, 3, 31)
    contracts.update_contract(org, editor, old, {"end_date": "2027-04-30"})  # the old term no longer drives it
    cycle.refresh_from_db()
    assert cycle.renewal_date == date(2028, 3, 31)


def test_creating_the_renewal_covers_the_contracts_applications(a):
    org, editor = a["org"], a["users"]["editor"]
    contract = new(a, end_date="2027-03-31", renewal_owner=str(a["people"]["dana"].id))
    contracts.add_item(org, editor, contract, {"record": str(a["app"].id)})
    contracts.add_item(org, editor, contract, {"record": str(component(a).id)})  # not an application: not covered
    contract = contracts.update_contract(org, editor, contract, {"create_renewal_obligation": True})
    cycle = RenewalCycle.objects.get(obligation=contract.renewal_obligation)
    assert list(cycle.applications.values_list("id", flat=True)) == [a["app"].id]
    assert contract.renewal_obligation is not None
    assert contract.renewal_obligation.responsible_id == a["people"]["dana"].id


# ---- services: validation and audit -------------------------------------------------------------------------------------


def test_validation_messages(a):
    org, editor = a["org"], a["users"]["editor"]
    with pytest.raises(FieldErrors) as e:
        contracts.create_contract(
            org,
            editor,
            {
                "name": "",
                "amount": "12.345",
                "currency": "us dollars",
                "billing_frequency": "weekly",
                "start_date": "2026-02-30",
                "notice_value": 2,
                "notice_unit": "weeks",
                "annualized_cost": "1",
                "renewal_owner": str(a["people"]["gone"].id),
                "status": "draft",
            },
        )
    errors = e.value.errors
    assert errors["name"] == ["Enter a name."]
    assert errors["amount"] == ["Use at most two decimal places for the amount."]
    assert errors["currency"] == ["Enter a three-letter currency code, such as USD or EUR."]
    assert "billing_frequency" in errors and "status" in errors and "notice_unit" in errors
    assert errors["start_date"] == ["Enter a date as YYYY-MM-DD."]
    assert errors["annualized_cost"][0].startswith("This is worked out")
    assert errors["renewal_owner"] == ["Choose an active person."]
    with pytest.raises(FieldErrors) as e:
        contracts.create_contract(
            org, editor, {"name": "X", "amount": -1, "start_date": "2027-01-01", "end_date": "2026-01-01"}
        )
    assert e.value.errors == {
        "amount": ["The amount can't be negative."],
        "end_date": ["The end date is before the start date."],
    }
    for huge in ("1e30", "9" * 31, "NaN", "Infinity"):
        with pytest.raises(FieldErrors) as e:
            contracts.create_contract(org, editor, {"name": "X", "amount": huge})
        assert "amount" in e.value.errors, huge
    made = contracts.create_contract(org, editor, {"name": "Y", "amount": 1200.5, "currency": "eur"})
    assert (made.amount, made.currency, made.billing_frequency, made.status, made.auto_renew) == (
        D("1200.50"),
        "EUR",
        "annual",
        "active",
        "unknown",
    )


def test_writes_are_audited_with_snapshots(a):
    org, editor = a["org"], a["users"]["editor"]
    vendor = Vendor.objects.create(organization=org, name="Atlassian")
    contract = new(a, vendor=str(vendor.id), amount="100", billing_frequency="monthly", end_date="2027-03-31")
    created = AuditEvent.objects.get(action="contract.created")
    assert created.target_type == "Contract" and created.prechange is None and created.postchange is not None
    assert created.postchange["vendor"] == {"id": str(vendor.id), "display": "Atlassian"}
    assert created.postchange["annualized_cost"] == "1200.00" and "cancellation_deadline" not in created.postchange
    contracts.update_contract(org, editor, contract, {"amount": "200", "renewal_owner": str(a["people"]["dana"].id)})
    updated = AuditEvent.objects.get(action="contract.updated")
    assert updated.prechange is not None and updated.postchange is not None
    assert updated.changes["amount"] == ["100.00", "200.00"]
    assert updated.changes["annualized_cost"] == ["1200.00", "2400.00"]
    assert updated.changes["renewal_owner"] == [None, str(a["people"]["dana"].id)]
    assert updated.prechange["amount"] == "100.00" and updated.postchange["amount"] == "200.00"
    assert updated.postchange["renewal_owner"] == {"id": str(a["people"]["dana"].id), "display": None}
    item = contracts.add_item(org, editor, contract, {"record": str(a["app"].id), "allocation_percent": "40"})
    line = AuditEvent.objects.get(action="contract_item.created")
    assert (line.target_type, line.related_target_type, line.related_target_id) == (
        "ContractItem",
        "Contract",
        contract.id,
    )
    assert line.postchange == {"contract": {"id": str(contract.id), "display": contract.name},
                               "resource": {"id": str(a["app"].id), "display": "orga App"},
                               "allocation_percent": "40.00"}  # fmt: skip
    contracts.update_item(org, editor, item, {"allocation_percent": None})
    assert AuditEvent.objects.get(action="contract_item.updated").changes["allocation_percent"] == ["40.00", None]
    contracts.archive_contract(org, editor, contract)
    contracts.archive_contract(org, editor, contract)  # idempotent: one event
    contracts.restore_contract(org, editor, contract)
    assert AuditEvent.objects.filter(action="contract.archived").count() == 1
    contracts.delete_contract(org, a["users"]["admin"], contract)
    deleted = AuditEvent.objects.get(action="contract.deleted")
    assert deleted.changes == {"name": "Atlassian Cloud", "lines": 1} and deleted.postchange is None
    assert not ContractItem.objects.exists()


def test_an_update_that_changes_nothing_writes_no_event(a):
    contract = new(a, amount="10")
    contracts.update_contract(a["org"], a["users"]["editor"], contract, {"amount": "10.00", "name": "Atlassian Cloud"})
    assert not AuditEvent.objects.filter(action="contract.updated").exists()


def test_delete_is_refused_while_a_pool_or_next_term_points_at_it(a):
    org = a["org"]
    contract = new(a)
    contracts.create_pool(
        org, a["users"]["editor"], {"application": str(a["app"].id), "sku_name": "E5", "contract": str(contract.id)}
    )
    with pytest.raises(Conflict) as refused:
        contracts.delete_contract(org, a["users"]["admin"], contract)
    assert refused.value.extra["blocking_references"] == {"license pool": 1}
    LicensePool.objects.all().delete()
    Contract.objects.create(organization=org, name="Next", predecessor=contract)
    with pytest.raises(Conflict) as refused:
        contracts.delete_contract(org, a["users"]["admin"], contract)
    assert refused.value.extra["blocking_references"] == {"next term": 1}


def test_records_and_vendors_on_contracts_are_not_deleted(a):
    org = a["org"]
    vendor = Vendor.objects.create(organization=org, name="Atlassian")
    contract = new(a, vendor=str(vendor.id))
    contracts.add_item(org, a["users"]["editor"], contract, {"record": str(a["app"].id)})
    with pytest.raises(Conflict) as refused:
        services.delete_resource(org, a["users"]["admin"], a["app"])
    assert refused.value.extra["blocking_references"] == {"contract line": 1}
    from catalog import admin_services

    with pytest.raises(Conflict) as refused:
        admin_services.delete_named(Vendor, org, a["users"]["admin"], vendor)
    assert "Atlassian Cloud" in refused.value.extra["blocking_names"]


# ---- licence pools ------------------------------------------------------------------------------------------------------


def test_licence_pools(a):
    org, editor = a["org"], a["users"]["editor"]
    pool = contracts.create_pool(
        org, editor, {"application": str(a["app"].id), "sku_name": "E5", "seats_purchased": 200, "seats_assigned": 190}
    )
    assert contracts.utilization(pool) == 95.0
    over = contracts.update_pool(org, editor, pool, {"seats_assigned": 230})  # overuse is a real state
    assert contracts.utilization(over) == 115.0
    assert contracts.utilization(LicensePool(sku_name="x", seats_purchased=0)) is None
    with pytest.raises(FieldErrors) as refused:
        contracts.update_pool(org, editor, pool, {"seat_sync": True})
    assert refused.value.errors == {"seat_sync": ["Set the SKU ID to sync seat counts from Entra."]}
    contracts.update_pool(org, editor, pool, {"seat_sync": True, "sku_id": "06ebc4ee-1bb5-47dd-8120-11324bc54e06"})
    other = app(a, "Office")
    with pytest.raises(FieldErrors) as refused:
        contracts.create_pool(
            org,
            editor,
            {"application": str(other.id), "sku_name": "E5", "sku_id": "06EBC4EE-1bb5-47dd-8120-11324bc54e06",
             "seat_sync": True},
        )  # fmt: skip
    assert "already syncs this SKU" in refused.value.errors["seat_sync"][0]
    with pytest.raises(FieldErrors) as refused:
        contracts.create_pool(
            org,
            editor,
            {"application": str(component(a).id), "sku_name": "", "seats_purchased": -1, "last_synced": "x"},
        )
    assert set(refused.value.errors) == {"application", "sku_name", "seats_purchased", "last_synced"}
    assert AuditEvent.objects.filter(action="license_pool.updated").count() == 2


def test_the_database_allows_one_synced_pool_per_sku(a):
    LicensePool.objects.create(organization=a["org"], application=a["app"], sku_name="A", sku_id="sku", seat_sync=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        LicensePool.objects.create(
            organization=a["org"], application=a["app"], sku_name="B", sku_id="SKU", seat_sync=True
        )


# ---- the API ---------------------------------------------------------------------------------------------------------------


def test_create_read_update_through_the_api(a, api):
    editor = api(a["users"]["editor"])
    vendor = Vendor.objects.create(organization=a["org"], name="Atlassian")
    made = editor.post(
        "contracts",
        {
            "name": "Atlassian Cloud",
            "vendor": str(vendor.id),
            "amount": "1000",
            "currency": "usd",
            "billing_frequency": "quarterly",
            "start_date": "2026-04-01",
            "end_date": "2027-03-31",
            "notice_value": 1,
            "notice_unit": "months",
            "auto_renew": "yes",
            "create_renewal_obligation": True,
        },
    )
    assert made.status_code == 201, made.json_body
    body = made.json_body
    assert body["annualized_cost"] == "4000.00" and body["currency"] == "USD"
    assert body["cancellation_deadline"] == "2027-02-28" == body["deadline"]["date"]
    assert body["deadline"]["label"] == "calculated" and body["annualization_note"] == "Quarterly amount × 4"
    assert body["notice"] == {"value": 1, "unit": "months"} and body["drives_renewal"] is True
    assert body["renewal_obligation"]["name"] == "Atlassian Cloud" and body["term_months"] == 12
    cid = body["id"]
    line = editor.post(f"contracts/{cid}/lines", {"record": str(a["app"].id), "allocation_percent": "60"})
    assert line.status_code == 201, line.json_body
    assert line.json_body["share_percent"] == "60.0000" and line.json_body["annual_amount"] == "2400.00"
    detail = editor.get(f"contracts/{cid}")
    assert detail.json_body["allocated_percent"] == "60.00"
    assert detail.json_body["unallocated"] == {"share_percent": "40.0000", "annual_amount": "1600.00"}
    assert [x["record"]["name"] for x in detail.json_body["lines"]] == ["orga App"]
    assert detail.json_body["line_count"] == 1
    etag = detail["ETag"]
    stale = editor.client.patch(
        f"/api/v1/contracts/{cid}",
        '{"notes": "x"}',
        content_type="application/json",
        HTTP_IF_MATCH='"' + "0" * 32 + '"',
    )
    assert stale.status_code == 412
    editor.patch(f"contracts/{cid}/lines/{line.json_body['id']}", {"allocation_percent": "50"})
    assert editor.get(f"contracts/{cid}")["ETag"] != etag  # a line change moves the contract's ETag
    over = editor.post(f"contracts/{cid}/lines", {"record": str(component(a).id), "allocation_percent": "60"})
    assert over.status_code == 400 and "allocation_percent" in over.json_body["errors"]
    preview = editor.get(
        "contracts/preview?amount=100&billing_frequency=monthly&end_date=2028-03-31&notice_value=1&notice_unit=months&auto_renew=yes"
    )
    assert preview.json_body["annualized_cost"] == "1200.00" and preview.json_body["deadline"]["date"] == "2028-02-29"
    assert editor.get("contracts/preview?amount=x&billing_frequency=weekly").status_code == 400


def test_list_filters(a, api):
    org = a["org"]
    vendor = Vendor.objects.create(organization=org, name="Contoso")
    first = new(a, vendor=str(vendor.id), end_date="2027-01-31", cost_center="CC-100", auto_renew="yes",
                notice_value=1, notice_unit="months", contract_number="AT-1", create_renewal_obligation=True)  # fmt: skip
    second = new(a, name="Slack", end_date="2027-09-30", status="pending_renewal", po_number="PO-7")
    third = new(a, name="Old", status="expired")
    contracts.add_item(org, a["users"]["editor"], second, {"record": str(a["app"].id)})
    contracts.archive_contract(org, a["users"]["editor"], third)
    viewer = api(a["users"]["viewer"])

    def names(query=""):
        response = viewer.get(f"contracts{query}")
        assert response.status_code == 200, response.json_body
        return [r["name"] for r in response.json_body["results"]]

    assert names() == ["Atlassian Cloud", "Slack"]
    assert names("?archived=all") == ["Atlassian Cloud", "Old", "Slack"]
    assert names(f"?vendor={vendor.id}") == ["Atlassian Cloud"]
    assert names("?status=pending_renewal,expired&archived=all") == ["Old", "Slack"]
    assert names("?end_before=2027-06-30") == ["Atlassian Cloud"]
    assert names("?end_after=2027-06-30") == ["Slack"]
    assert names("?deadline_before=2026-12-31") == ["Atlassian Cloud"]
    assert names("?auto_renew=yes") == ["Atlassian Cloud"]
    assert names("?cost_center=cc-100") == ["Atlassian Cloud"]
    assert names("?renewal_obligation=none") == ["Slack"]
    assert names(f"?renewal_obligation={first.renewal_obligation_id}") == ["Atlassian Cloud"]
    assert names(f"?record={a['app'].id}") == ["Slack"]
    assert names("?q=po-7") == ["Slack"] and names("?q=contoso") == ["Atlassian Cloud"]
    assert names("?ordering=-ends_on") == ["Slack", "Atlassian Cloud"]
    assert names("?ordering=deadline_on") == ["Atlassian Cloud", "Slack"]
    bad = viewer.get("contracts?end_before=soon&status=draft&colour=red")
    assert bad.status_code == 400 and set(bad.json_body["errors"]) == {"end_before", "status", "colour"}
    page = viewer.get("contracts?page_size=1")
    assert len(page.json_body["results"]) == 1 and page.json_body["next"]
    rest = viewer.get(f"contracts?page_size=1&cursor={page.json_body['next']}")
    assert [r["name"] for r in rest.json_body["results"]] == ["Slack"]


def test_licence_pool_api(a, api):
    editor = api(a["users"]["editor"])
    contract = new(a)
    made = editor.post(
        "license-pools",
        {"application": str(a["app"].id), "contract": str(contract.id), "sku_name": "E5", "seats_purchased": 10,
         "seats_assigned": 12},
    )  # fmt: skip
    assert made.status_code == 201, made.json_body
    assert made.json_body["overused"] is True and made.json_body["seats_available"] == -2
    assert made.json_body["utilization"] == 120.0 and made.json_body["contract"]["name"] == "Atlassian Cloud"
    viewer = api(a["users"]["viewer"])
    assert [p["sku_name"] for p in viewer.get("license-pools?overused=true").json_body["results"]] == ["E5"]
    assert viewer.get("license-pools?overused=false").json_body["results"] == []
    assert viewer.get(f"license-pools?contract={contract.id}").json_body["results"][0]["id"] == made.json_body["id"]
    assert editor.delete(f"license-pools/{made.json_body['id']}").status_code == 403
    assert api(a["users"]["admin"]).delete(f"license-pools/{made.json_body['id']}").status_code == 204
    assert api(a["users"]["viewer"]).get(f"contracts/{contract.id}").json_body["license_pool_count"] == 0


def test_roles(a, api):
    contract = new(a)
    viewer, editor, admin = (api(a["users"][r]) for r in ("viewer", "editor", "admin"))
    assert viewer.get("contracts").status_code == 200
    assert viewer.get(f"contracts/{contract.id}").status_code == 200
    assert viewer.post("contracts", {"name": "No"}).status_code == 403
    assert viewer.patch(f"contracts/{contract.id}", {"name": "No"}).status_code == 403
    assert viewer.post(f"contracts/{contract.id}/lines", {"record": str(a["app"].id)}).status_code == 403
    assert viewer.post(f"contracts/{contract.id}/archive").status_code == 403
    assert editor.post(f"contracts/{contract.id}/archive").status_code == 200
    assert editor.delete(f"contracts/{contract.id}").status_code == 403
    assert admin.delete(f"contracts/{contract.id}").status_code == 204


def test_another_organizations_contracts_are_404(a, b, api):
    theirs = contracts.create_contract(b["org"], b["users"]["editor"], {"name": "Theirs"})
    line = contracts.add_item(b["org"], b["users"]["editor"], theirs, {"record": str(b["app"].id)})
    pool = contracts.create_pool(b["org"], b["users"]["editor"], {"application": str(b["app"].id), "sku_name": "E3"})
    admin = api(a["users"]["admin"])
    for method, url in (
        ("get", f"contracts/{theirs.id}"),
        ("patch", f"contracts/{theirs.id}"),
        ("delete", f"contracts/{theirs.id}"),
        ("get", f"contracts/{theirs.id}/lines"),
        ("patch", f"contracts/{theirs.id}/lines/{line.id}"),
        ("get", f"contracts/{theirs.id}/audit"),
        ("get", f"contracts/{theirs.id}/attachments"),
        ("get", f"license-pools/{pool.id}"),
        ("delete", f"license-pools/{pool.id}"),
    ):
        assert getattr(admin, method)(url).status_code == 404, url
    assert admin.get("contracts").json_body["results"] == []
    assert admin.get("license-pools").json_body["results"] == []
    mine = new(a)
    refused = api(a["users"]["editor"]).post(f"contracts/{mine.id}/lines", {"record": str(b["app"].id)})
    assert refused.status_code == 400 and "record" in refused.json_body["errors"]
    refused = api(a["users"]["editor"]).post(
        "contracts", {"name": "X", "vendor": str(Vendor.objects.create(organization=b["org"], name="V").id)}
    )
    assert refused.status_code == 400 and "vendor" in refused.json_body["errors"]
    obligation = renewals.create_renewal(b["org"], b["users"]["editor"], {"name": "Theirs"})
    refused = api(a["users"]["editor"]).post(
        "contracts", {"name": "X", "end_date": "2027-01-01", "renewal_obligation": str(obligation.id)}
    )
    assert refused.status_code == 400 and "renewal_obligation" in refused.json_body["errors"]
    refused = api(a["users"]["editor"]).post("license-pools", {"application": str(b["app"].id), "sku_name": "X"})
    assert refused.status_code == 400 and "application" in refused.json_body["errors"]


def _rejected(write):
    with pytest.raises(IntegrityError), transaction.atomic():
        write()
        connection.check_constraints()  # the composite keys are deferred; force the check


def test_the_database_rejects_cross_organization_references(a, b):
    theirs_vendor = Vendor.objects.create(organization=b["org"], name="Theirs")
    mine = Contract.objects.create(organization=a["org"], name="Mine")
    theirs = Contract.objects.create(organization=b["org"], name="Theirs")
    _rejected(lambda: Contract.objects.create(organization=a["org"], name="X", vendor=theirs_vendor))
    _rejected(lambda: Contract.objects.create(organization=a["org"], name="X", renewal_owner=b["people"]["dana"]))
    _rejected(lambda: Contract.objects.create(organization=a["org"], name="X", predecessor=theirs))
    _rejected(
        lambda: Contract.objects.create(
            organization=a["org"],
            name="X",
            renewal_obligation=RenewalObligation.objects.create(organization=b["org"], name="R"),
        )
    )
    _rejected(lambda: ContractItem.objects.create(organization=a["org"], contract=mine, resource=b["app"]))
    _rejected(lambda: ContractItem.objects.create(organization=a["org"], contract=theirs, resource=a["app"]))
    _rejected(lambda: LicensePool.objects.create(organization=a["org"], application=b["app"], sku_name="X"))
    _rejected(
        lambda: LicensePool.objects.create(organization=a["org"], application=a["app"], contract=theirs, sku_name="X")
    )
    _rejected(
        lambda: Attachment.objects.create(
            organization=a["org"], contract=theirs, kind="link", title="T", url="https://example.com"
        )
    )


def test_exports(a, api):
    org = a["org"]
    customfields.create_field(
        org,
        a["users"]["admin"],
        {"name": "budget_line", "label": "Budget line", "type": "text", "applies_to": ["contract"]},
    )
    vendor = Vendor.objects.create(organization=org, name="Atlassian")
    contract = new(a, vendor=str(vendor.id), amount="1000", billing_frequency="annual", end_date="2027-03-31",
                   renewal_owner=str(a["people"]["dana"].id), custom_field_data={"budget_line": "=SUM(A1)"})  # fmt: skip
    contracts.add_item(org, a["users"]["editor"], contract, {"record": str(a["app"].id), "allocation_percent": "40"})
    contracts.add_item(org, a["users"]["editor"], contract, {"record": str(component(a).id)})
    viewer = api(a["users"]["viewer"])
    response = viewer.client.get("/api/v1/contracts/export.csv")
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0]["name"] == "Atlassian Cloud" and rows[0]["vendor"] == "Atlassian"
    assert rows[0]["annualized_cost"] == "1000.00" and rows[0]["items"] == "orga App=40; Sync job"
    assert rows[0]["renewal_owner_name"] == "Dana Ortiz" and rows[0]["cf_budget_line"] == "'=SUM(A1)"
    event = AuditEvent.objects.get(action="export")
    assert event.changes == {"kind": "contract", "rows": 1, "filters": {}}
    xlsx = viewer.client.get("/api/v1/contracts/export.xlsx?status=active")
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
    assert AuditEvent.objects.filter(action="export", changes__format="xlsx").exists()
    assert viewer.client.get("/api/v1/contracts/export.csv?status=nope").status_code == 400


# ---- joins: search, the record map, custom fields, attachments, data quality ---------------------------------------------


def test_contracts_join_global_search(a, api):
    vendor = Vendor.objects.create(organization=a["org"], name="Contoso")
    new(a, vendor=str(vendor.id), contract_number="AGR-2291", po_number="PO-55120")
    archived = new(a, name="Retired deal")
    contracts.archive_contract(a["org"], a["users"]["editor"], archived)
    viewer = api(a["users"]["viewer"])

    def found(q, extra=""):
        body = viewer.get(f"search?q={q}&kinds=contract{extra}").json_body
        return [(r["name"], r["path"]) for r in body["groups"][0]["results"]]

    expected = [("Atlassian Cloud", f"contracts/{Contract.objects.get(name='Atlassian Cloud').id}")]
    assert found("atlass") == expected
    assert found("AGR-2291") == expected
    assert found("PO-55120") == expected
    assert found("contoso") == expected  # by the vendor's name
    assert found("retired") == []
    assert [n for n, _ in found("retired", "&include_archived=true")] == ["Retired deal"]
    assert found("atlasian") == expected  # typo matching
    assert "contract" in [g["kind"] for g in viewer.get("search?q=atlassian").json_body["groups"]]


def test_contracts_and_pools_on_the_record_map(a, api):
    contract = new(a, end_date="2027-03-31")
    contracts.add_item(
        a["org"], a["users"]["editor"], contract, {"record": str(a["app"].id), "allocation_percent": "25"}
    )
    pool = contracts.create_pool(a["org"], a["users"]["editor"], {"application": str(a["app"].id), "sku_name": "E5"})
    body = api(a["users"]["viewer"]).get(f"applications/{a['app'].id}/map").json_body
    kinds = {n["id"]: n["kind"] for n in body["nodes"]}
    assert kinds[str(contract.id)] == "contract" and kinds[str(pool.id)] == "license_pool"
    assert {"key": "contract", "label": "Contracts"} in body["categories"]
    assert body["counts"]["contract"] == 1 and body["counts"]["license_pool"] == 1
    edge = next(e for e in body["edges"] if e["category"] == "contract")
    assert edge["detail"]["allocation_percent"] == "25.00" and edge["reads"] == "Covered by contract"


def test_custom_fields_on_contracts_and_pools(a, api):
    org, admin = a["org"], a["users"]["admin"]
    field = customfields.create_field(
        org,
        admin,
        {"name": "budget_line", "label": "Budget line", "type": "text", "applies_to": ["contract", "license_pool"]},
    )
    editor = api(a["users"]["editor"])
    made = editor.post("contracts", {"name": "With field", "custom_field_data": {"budget_line": "IT-42"}})
    assert made.status_code == 201 and made.json_body["custom_field_data"] == {"budget_line": "IT-42"}
    refused = editor.post("contracts", {"name": "Bad", "custom_field_data": {"nope": 1}})
    assert "custom_field_data.nope" in refused.json_body["errors"]
    pool = editor.post(
        "license-pools",
        {"application": str(a["app"].id), "sku_name": "E5", "custom_field_data": {"budget_line": "IT-7"}},
    )
    assert pool.json_body["custom_field_data"] == {"budget_line": "IT-7"}
    viewer = api(a["users"]["viewer"])
    assert [r["name"] for r in viewer.get("contracts?cf_budget_line=it-4").json_body["results"]] == ["With field"]
    assert len(viewer.get("license-pools?cf_budget_line__set=true").json_body["results"]) == 1
    assert customfields.value_counts(org, "budget_line") == {"contract": 1, "license_pool": 1}
    assert customfields.usage(org) == {"budget_line": 2}
    event = AuditEvent.objects.get(action="contract.created")
    assert (event.postchange or {})["custom_field_data.budget_line"] == "IT-42"
    found = viewer.get("search?q=IT-42&kinds=contract").json_body["groups"][0]["results"]
    assert found[0]["snippet"]["label"] == "Budget line"
    refused = api(admin).patch(f"custom-fields/{field.id}", {"applies_to": ["application"]})
    assert refused.status_code == 400 and "contracts" in refused.json_body["errors"]["applies_to"][0]
    customfields.delete_field(org, admin, field)
    assert Contract.objects.get(name="With field").custom_field_data == {}
    assert LicensePool.objects.get().custom_field_data == {}


def test_contract_reminder_source_dates(a):
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
    new(a, custom_field_data={"review_on": "2027-01-15"})
    archived = new(a, name="Archived", custom_field_data={"review_on": "2027-01-10"})
    contracts.archive_contract(org, a["users"]["editor"], archived)
    sources = customfields.reminder_sources(org)
    assert [(s.record.name, s.due) for s in sources] == [("Atlassian Cloud", date(2027, 1, 15))]


def test_attachments_on_contracts_and_the_contract_history(a, api):
    contract = new(a)
    editor = api(a["users"]["editor"])
    link = editor.post(f"contracts/{contract.id}/attachments", {"url": "https://example.com/msa.pdf", "title": "MSA"})
    assert link.status_code == 201, link.json_body
    assert link.json_body["attached_to"] == {"type": "contract", "id": str(contract.id), "name": "Atlassian Cloud"}
    assert [x["title"] for x in editor.get(f"contracts/{contract.id}/attachments").json_body] == ["MSA"]
    assert editor.get(f"attachments/{link.json_body['id']}").status_code == 200
    item = contracts.add_item(
        a["org"], a["users"]["editor"], contract, {"record": str(a["app"].id), "allocation_percent": "40"}
    )
    contracts.update_item(a["org"], a["users"]["editor"], item, {"allocation_percent": "50"})
    history = editor.get(f"contracts/{contract.id}/audit").json_body["results"]
    sentences = [e["child"]["sentence"] for e in history if e["child"]]
    assert sentences == [
        "Allocation on line 'orga App': 40% → 50%",
        "Added line 'orga App' (40%)",
        "Added attachment 'MSA' (link)",
    ]
    assert history[-1]["action"] == "contract.created" and history[-1]["child"] is None
    assert history[-1]["target"] == {
        "kind": "contract",
        "id": str(contract.id),
        "name": "Atlassian Cloud",
    }
    own = editor.get(f"contracts/{contract.id}/audit?children=false").json_body["results"]
    assert [e["action"] for e in own] == ["contract.created"]
    one = editor.get(f"contracts/{contract.id}/audit/{history[0]['id']}").json_body
    assert one["diff"][0]["field"] == "allocation_percent"
    record = editor.get(f"applications/{a['app'].id}/audit").json_body["results"]
    assert record[0]["child"]["sentence"] == "Allocation on contract 'Atlassian Cloud': 40% → 50%"
    found = api(a["users"]["viewer"]).get("search?q=msa&kinds=attachment").json_body["groups"][0]["results"]
    assert found[0]["parent"] == {
        "kind": "contract",
        "id": str(contract.id),
        "name": "Atlassian Cloud",
        "path": f"contracts/{contract.id}",
    }
    contracts.delete_contract(a["org"], a["users"]["admin"], contract)
    assert not Attachment.objects.exists()
    assert AuditEvent.objects.filter(action="attachment.deleted", related_target_id=contract.id).exists()


def contract_check(org, record) -> str:
    """pass, fail or n/a: the contract rule alone on one record."""
    result = quality.evaluate(org, [record.id], rules=[quality.BY_ID["contract"]])[record.id]
    return "fail" if result.failing else ("pass" if result.passing else "n/a")


def test_the_contract_data_quality_rule_is_on(a, api):
    org = a["org"]
    assert quality.status(quality.BY_ID["contract"], org) == ("on", "")
    internal = app(a, "Built here", built_by="internal")
    free = app(a, "Free tool", license_model="free")
    assert contract_check(org, a["app"]) == "fail"
    assert contract_check(org, internal) == contract_check(org, free) == "n/a"
    contract = new(a)
    contracts.add_item(org, a["users"]["editor"], contract, {"record": str(a["app"].id)})
    assert contract_check(org, a["app"]) == "pass"
    contracts.update_contract(org, a["users"]["editor"], contract, {"status": "expired"})
    assert contract_check(org, a["app"]) == "fail"
    contracts.update_contract(org, a["users"]["editor"], contract, {"status": "signed"})
    assert contract_check(org, a["app"]) == "pass"
    contracts.archive_contract(org, a["users"]["editor"], contract)
    assert contract_check(org, a["app"]) == "fail"
    rules = api(a["users"]["viewer"]).get("data-quality/rules").json_body
    rows = rules if isinstance(rules, list) else rules["rules"]
    assert next(r for r in rows if r["id"] == "contract")["status"] == "on"


def test_contract_list_queries_do_not_grow_with_rows(a, api):
    org, editor = a["org"], a["users"]["editor"]
    viewer = api(a["users"]["viewer"])

    def add(start, count):
        for i in range(start, start + count):
            vendor = Vendor.objects.create(organization=org, name=f"Vendor {i}")
            contract = new(a, name=f"Contract {i:02}", vendor=str(vendor.id), end_date="2027-03-31",
                           renewal_owner=str(a["people"]["dana"].id), create_renewal_obligation=True)  # fmt: skip
            contracts.add_item(org, editor, contract, {"record": str(a["app"].id)})
            contracts.create_pool(org, editor, {"application": str(a["app"].id), "sku_name": f"SKU {i}",
                                                "contract": str(contract.id)})  # fmt: skip

    def count(path):
        viewer.get(path)
        with CaptureQueriesContext(connection) as queries:
            response = viewer.get(path)
        assert response.status_code == 200
        return len(queries)

    add(0, 1)
    one = {p: count(p) for p in ("contracts", "license-pools")}
    add(1, 15)
    many = {p: count(p) for p in ("contracts", "license-pools")}
    assert one == many
    # One more each for the capability overrides (core/capabilities.capabilities, once per request);
    # One more for the API rate limit's counter upsert (core/counters.py)
    assert many["contracts"] <= 10 and many["license-pools"] <= 8, many


def test_the_schema_documents_contracts_and_pools(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    paths, schemas = doc["paths"], doc["components"]["schemas"]
    expected = {
        ("get", "/api/v1/contracts"),
        ("post", "/api/v1/contracts"),
        ("get", "/api/v1/contracts/preview"),
        ("get", "/api/v1/contracts/export.csv"),
        ("get", "/api/v1/contracts/export.xlsx"),
        ("get", "/api/v1/contracts/{id}"),
        ("patch", "/api/v1/contracts/{id}"),
        ("delete", "/api/v1/contracts/{id}"),
        ("post", "/api/v1/contracts/{id}/archive"),
        ("post", "/api/v1/contracts/{id}/restore"),
        ("get", "/api/v1/contracts/{id}/lines"),
        ("post", "/api/v1/contracts/{id}/lines"),
        ("get", "/api/v1/contracts/{id}/lines/{item_pk}"),
        ("patch", "/api/v1/contracts/{id}/lines/{item_pk}"),
        ("delete", "/api/v1/contracts/{id}/lines/{item_pk}"),
        ("get", "/api/v1/contracts/{id}/attachments"),
        ("post", "/api/v1/contracts/{id}/attachments"),
        ("get", "/api/v1/contracts/{id}/audit"),
        ("get", "/api/v1/contracts/{id}/audit/{event_pk}"),
        ("get", "/api/v1/license-pools"),
        ("post", "/api/v1/license-pools"),
        ("get", "/api/v1/license-pools/{id}"),
        ("patch", "/api/v1/license-pools/{id}"),
        ("delete", "/api/v1/license-pools/{id}"),
        ("get", "/api/v1/license-pools/{id}/audit"),
        ("get", "/api/v1/license-pools/{id}/audit/{event_pk}"),
        # Renewal and licences
        ("get", "/api/v1/contracts/{id}/renew"),
        ("post", "/api/v1/contracts/{id}/renew"),
        ("get", "/api/v1/license-pools/export.csv"),
        ("get", "/api/v1/license-pools/totals"),
        ("get", "/api/v1/license-pools/seat-review"),
        ("post", "/api/v1/license-pools/{id}/seat-review/{observation_pk}/apply"),
        ("post", "/api/v1/license-pools/{id}/seat-review/{observation_pk}/dismiss"),
        ("post", "/api/v1/license-pools/unbound/{observation_pk}/create"),  # reconciliation
        ("post", "/api/v1/license-pools/{id}/bind"),
        # Licence waste on a contract's and a pool's page (discovery/licence_waste_views.py)
        ("get", "/api/v1/contracts/{id}/licence-waste"),
        ("get", "/api/v1/license-pools/{id}/licence-waste"),
    }
    found = {
        (m, p) for p, ops in paths.items() for m in ops if p.startswith(("/api/v1/contracts", "/api/v1/license-pools"))
    }
    assert found == expected
    create = paths["/api/v1/contracts"]["post"]
    assert create["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("ContractInputRequest")
    assert {"201", "400", "403", "409"} <= set(create["responses"])
    assert {"If-Match"} <= {p["name"] for p in paths["/api/v1/contracts/{id}"]["patch"]["parameters"]}
    assert "412" in paths["/api/v1/contracts/{id}"]["patch"]["responses"]
    assert {"cursor", "vendor", "end_before", "deadline_after", "record"} <= {
        p["name"] for p in paths["/api/v1/contracts"]["get"]["parameters"]
    }
    assert schemas["BillingFrequencyEnum"]["enum"] == [
        "monthly", "quarterly", "semiannual", "annual", "multi_year", "one_time", "usage_based"
    ]  # fmt: skip
    assert {"lines", "unallocated", "license_pools", "deadline"} <= set(schemas["ContractDetail"]["properties"])
    assert "contract" in schemas["RenewalList"]["properties"]
    assert "terms_change" in schemas["RenewalCycle"]["properties"]
