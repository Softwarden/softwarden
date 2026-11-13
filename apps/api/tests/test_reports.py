"""Reports and dashboard (money/reporting.py, money/report_views.py): spend by vendor, department, category and cost centre with allocation, per currency, reconciling across
dimensions; live-status filtering; action deadlines from the same dates reminders use (one source of truth); expiring
credentials; the dashboard per role and with capabilities off; query counts; CSV exports."""

import csv
import io
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from api import tokens
from catalog import services
from catalog.models import Application, Department, Resource, SoftwareCategory, Vendor
from core import capabilities as caps
from core.models import AuditEvent, Membership
from money import contracts, renewal_rules, renewals, reporting
from money.models import Contract, RenewalCycle

pytestmark = [pytest.mark.django_db, pytest.mark.one_source_of_truth]


def today(a):
    return services.org_today(a["org"])


def iso(a, days):
    return (today(a) + timedelta(days=days)).isoformat()


def contract(a, name, **data):
    return contracts.create_contract(a["org"], a["users"]["editor"], {"name": name, **data})


def line(a, c, record, percent=None):
    data = {"record": str(record.id)}
    if percent is not None:
        data["allocation_percent"] = percent
    return contracts.add_item(a["org"], a["users"]["editor"], c, data)


def record(a, name, kind="application", **fields):
    made = services.create_resource(a["org"], a["users"]["editor"], {"name": name, "kind": kind, "lifecycle": "active"})
    if fields:
        Resource.objects.filter(id=made.id).update(**fields)
        made.refresh_from_db()
    return made


def categorise(app, category):
    Application.objects.filter(resource=app).update(category=category)


@pytest.fixture
def books(a):
    """Two currencies, every dimension's gaps, a contract with no lines, fixed and floating shares."""
    org = a["org"]
    finance = Department.objects.create(organization=org, name="Finance")
    sales = Department.objects.create(organization=org, name="Sales")
    crm_cat = SoftwareCategory.objects.get(organization=org, name="CRM and sales")
    acme = Vendor.objects.create(organization=org, name="Acme")
    globex = Vendor.objects.create(organization=org, name="Globex")
    ledger = record(a, "Ledger", department=finance, cost_center="CC-FIN")
    crm = record(a, "CRM", department=sales)
    categorise(crm, crm_cat)
    job = record(a, "Nightly job", kind="component", department=finance)
    suite = contract(a, "Suite", vendor=str(acme.id), amount="1000.00", billing_frequency="monthly",
                     cost_center="CC-IT")  # fmt: skip  # 12,000 a year
    line(a, suite, ledger, "50")
    line(a, suite, crm)  # floating: 25 %
    line(a, suite, job)  # floating: 25 %
    partial = contract(a, "Partial", vendor=str(globex.id), amount="999.99", currency="EUR")
    line(a, partial, crm, "33.33")  # the rest, 66.67 %, is unallocated
    bare = contract(a, "Bare", amount="100.00", billing_frequency="quarterly", currency="EUR")  # 400, no lines
    return {"ledger": ledger, "crm": crm, "job": job, "suite": suite, "partial": partial, "bare": bare,
            "finance": finance, "sales": sales, "category": crm_cat, "acme": acme, "globex": globex}  # fmt: skip


def per_currency(rows):
    found: dict[str, Decimal] = {}
    for r in rows:
        found[r["currency"]] = found.get(r["currency"], Decimal(0)) + Decimal(r["amount"])
    return found


# ---- spend ------------------------------------------------------------------------------------------------------------


def test_every_dimension_adds_up_to_the_total_per_currency(a, books):
    """Each dimension's groups, per currency, add up to the annualised total and to the
    allocation rows `contracts.allocations` gives, to the cent."""
    totals = {t["currency"]: Decimal(t["annualized"]) for t in reporting.spend_report(a["org"], "vendor")["totals"]}
    assert totals == {"EUR": Decimal("1399.99"), "USD": Decimal("12000.00")}
    live = Contract.objects.filter(organization=a["org"])
    expected: dict[str, Decimal] = {}
    for row in contracts.allocations(live):
        expected[row.contract.currency] = expected.get(row.contract.currency, Decimal(0)) + row.amount
    assert expected == totals
    for by in reporting.DIMENSIONS:
        report = reporting.spend_report(a["org"], by)
        assert per_currency(report["rows"]) == totals, by
        assert per_currency(report["allocations"]) == totals, by


def groups(report):
    return {(r["currency"], r["gap"] or r["label"]): r["amount"] for r in report["rows"]}


def test_spend_groups_per_dimension(a, books):
    org = a["org"]
    assert groups(reporting.spend_report(org, "vendor")) == {
        ("USD", "Acme"): "12000.00",
        ("EUR", "Globex"): "999.99",
        ("EUR", "no_vendor"): "400.00",
    }
    assert groups(reporting.spend_report(org, "department")) == {
        ("USD", "Finance"): "9000.00",  # Ledger 6,000 and the job 3,000
        ("USD", "Sales"): "3000.00",
        ("EUR", "Sales"): "333.30",  # 33.33 % of 999.99, to the cent
        ("EUR", "unallocated"): "1066.69",  # Partial's remainder and Bare
    }
    assert groups(reporting.spend_report(org, "category")) == {
        ("USD", "CRM and sales"): "3000.00",
        ("USD", "no_category"): "6000.00",  # Ledger
        ("USD", "not_application"): "3000.00",  # the component
        ("EUR", "CRM and sales"): "333.30",
        ("EUR", "unallocated"): "1066.69",
    }


def test_cost_centre_is_the_records_else_the_contracts(a, books):
    """The line's record's cost centre wins; a record without one, and cost not on a record, take the
    contract's; with neither the gap is named."""
    found = groups(reporting.spend_report(a["org"], "cost_center"))
    assert found == {
        ("USD", "CC-FIN"): "6000.00",  # Ledger's own
        ("USD", "CC-IT"): "6000.00",  # CRM and the job take the contract's
        ("EUR", "no_cost_center"): "1399.99",
    }
    detail = reporting.spend_report(a["org"], "cost_center")["allocations"]
    unallocated = [r for r in detail if r["record"] is None]
    assert {(r["contract"]["name"], r["amount"], r["percent"]) for r in unallocated} == {
        ("Partial", "666.69", "66.67"),
        ("Bare", "400.00", "100.00"),
    }


def test_only_live_contracts_are_spend_and_signed_terms_are_future(a, books):
    org, editor = a["org"], a["users"]["editor"]
    contract(a, "Future", amount="500.00", status="signed", start_date=iso(a, 40), end_date=iso(a, 400))
    contract(a, "Old", amount="700.00", status="expired", end_date=iso(a, -40))
    contract(a, "Dropped", amount="800.00", status="cancelled")
    contract(a, "Done", amount="900.00", status="renewed", end_date=iso(a, -10))
    contract(a, "Unpriced")  # no amount: no cost
    archived = contract(a, "Shelved", amount="1100.00")
    contracts.archive_contract(org, editor, archived)
    contract(a, "Pending", amount="10.00", status="pending_renewal")
    contract(a, "Setup fee", amount="250.00", billing_frequency="one_time")
    report = reporting.spend_report(org, "vendor")
    names = {r["contract"]["name"] for r in report["allocations"]}
    assert names == {"Suite", "Partial", "Bare", "Pending", "Setup fee"}
    usd = next(t for t in report["totals"] if t["currency"] == "USD")
    assert usd == {
        "currency": "USD",
        "annualized": "12260.00",
        "run_rate": "12010.00",
        "one_time": "250.00",
        "future": "500.00",
        "future_one_time": "0.00",
        "next_start": iso(a, 40),
        "contracts": 3,
    }
    assert per_currency(report["rows"])["USD"] == Decimal(usd["annualized"])


def test_spend_endpoint_roles_parameters_and_capability(a, api, books, monkeypatch):
    viewer = api(a["users"]["viewer"])
    body = viewer.get("reports/spend?by=department").json_body
    assert body["by"] == "department" and set(body) == {"by", "totals", "rows", "allocations"}
    assert viewer.get("reports/spend").json_body["by"] == "vendor"
    refused = viewer.get("reports/spend?by=colour")
    assert refused.status_code == 400 and "by" in refused.json_body["errors"]
    assert api().get("reports/spend").status_code in (401, 403)
    real = caps.capabilities
    monkeypatch.setattr(caps, "capabilities", lambda org: real(org) - {caps.CONTRACTS})
    off = viewer.get("reports/spend")
    assert off.status_code == 403 and off.json_body["capability"] == "contracts"
    assert viewer.get("reports/spend/export.csv").status_code == 403
    assert viewer.get("reports/license-utilization/export.csv").status_code == 403


def read_csv(response):
    text = b"".join(response.streaming_content if response.streaming else [response.content]).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def test_spend_csv_is_escaped_audited_and_reconciles(a, api, books):
    contracts.update_contract(a["org"], a["users"]["editor"], books["bare"], {"name": "=HYPERLINK(1)"})
    response = api(a["users"]["viewer"]).get("reports/spend/export.csv?by=cost_center")
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    assert "spend-by-cost-center" in response["Content-Disposition"]
    rows = read_csv(response)
    assert set(rows[0]) == set(reporting_columns())
    assert "'=HYPERLINK(1)" in {r["contract"] for r in rows}  # formula-escaped
    total = sum((Decimal(r["amount"]) for r in rows if r["currency"] == "USD"), Decimal(0))
    assert total == Decimal("12000.00")
    event = AuditEvent.objects.filter(organization=a["org"], action="export").latest("created_at")
    assert event.changes == {"kind": "spend_allocation", "rows": len(rows), "filters": {"by": "cost_center"}}
    assert event.actor_id == a["users"]["viewer"].id


def reporting_columns():
    from money.report_views import SPEND_COLUMNS

    return SPEND_COLUMNS


def test_licence_utilisation_csv(a, api):
    org = a["org"]
    contracts.create_pool(org, a["users"]["editor"], {"application": str(a["app"].id), "sku_name": "+E5",
                                                      "seats_purchased": 10, "seats_assigned": 12})  # fmt: skip
    response = api(a["users"]["viewer"]).get("reports/license-utilization/export.csv")
    assert response.status_code == 200
    rows = read_csv(response)
    assert [r["sku_name"] for r in rows] == ["'+E5", ""]  # escaped; then the organization's total row
    assert (
        rows[0]["overused"] == "yes"
        and rows[0]["utilization"] == "120.0"
        and rows[1]["application"] == "All applications"
    )
    event = AuditEvent.objects.filter(organization=org, action="export").latest("created_at")
    assert event.changes["kind"] == "license_total" and event.changes["rows"] == 1


# ---- deadlines: one source of truth -------------------------------------------------------------------------------------


@pytest.fixture
def deadlines(a):
    """A renewal driven by a contract, a contract with no renewal, a renewal with no contract, and ones that have no
    open action."""
    org, editor = a["org"], a["users"]["editor"]
    driven = contract(a, "Driven", end_date=iso(a, 100), notice_value=60, notice_unit="days", auto_renew="yes",
                      amount="1200.00", create_renewal_obligation=True)  # fmt: skip  # deadline in 40 days
    alone = contract(a, "Alone", end_date=iso(a, org.renewal_lead_days + 45), auto_renew="no")  # 45 days
    renewal = renewals.create_renewal(org, editor, {"name": "Domain", "renewal_date": iso(a, 20),
                                                    "auto_renew": "yes", "notice_value": 10,
                                                    "notice_unit": "days"})  # fmt: skip  # 10 days
    overdue = renewals.create_renewal(org, editor, {"name": "Late", "explicit_deadline": iso(a, -3)})
    done = renewals.create_renewal(org, editor, {"name": "Decided", "renewal_date": iso(a, 30)})
    cycle = RenewalCycle.objects.get(obligation=done)
    renewals.record_decision(org, editor, done, cycle.id, {"decision": "renew"})
    renewals.record_completion(org, editor, done, cycle.id, {"completed_on": iso(a, 0), "completion_evidence": "Paid"})
    contract(a, "Over", end_date=iso(a, 50), status="expired")
    shelved = contract(a, "Shelved", end_date=iso(a, 50))
    contracts.archive_contract(org, editor, shelved)
    far = contract(a, "Far", end_date=iso(a, 500))
    driven.refresh_from_db()
    return {"driven": driven, "alone": alone, "renewal": renewal, "overdue": overdue, "far": far}


def test_deadlines_come_from_the_renewal_states_and_the_contracts_own_deadline(a, deadlines):
    org = a["org"]
    rows = reporting.deadline_rows(org, today(a))
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"Driven", "Alone", "Domain", "Late", "Far"}  # Driven once, as its renewal
    driven = deadlines["driven"]
    row = by_name["Driven"]
    assert row["source"] == "renewal" and row["id"] == str(driven.renewal_obligation_id)
    assert row["contract"] == {"id": str(driven.id), "name": "Driven"} and row["annualized_cost"] == "1200.00"
    # One source of truth: the reminders' state, the contract's stored deadline and the row agree.
    state = next(s for s in renewals.queue_states(org, today(a)) if s.obligation.id == driven.renewal_obligation_id)
    assert row["date"] == state.deadline.date.isoformat() == driven.cancellation_deadline.isoformat() == iso(a, 40)
    alone = by_name["Alone"]
    assert alone["source"] == "contract" and alone["date"] == deadlines["alone"].cancellation_deadline.isoformat()
    assert alone["label"] == renewal_rules.RENEWAL_ACTION and alone["action"] == "renew_by"
    assert by_name["Domain"]["date"] == iso(a, 10) and by_name["Domain"]["action"] == "cancel_by"
    assert by_name["Late"]["timing"]["overdue"] and by_name["Late"]["label"] == renewal_rules.ENTERED
    assert [r["name"] for r in rows] == ["Late", "Domain", "Driven", "Alone", "Far"]
    assert len({r["uid"] for r in rows}) == len(rows)


def test_moving_the_contracts_end_date_moves_the_row(a, deadlines):
    driven = deadlines["driven"]
    contracts.update_contract(a["org"], a["users"]["editor"], driven, {"end_date": iso(a, 130)})
    row = next(r for r in reporting.deadline_rows(a["org"], today(a)) if r["name"] == "Driven")
    driven.refresh_from_db()
    assert row["date"] == driven.cancellation_deadline.isoformat() == iso(a, 70)


def test_windows_and_the_dashboard_rows(a, api, deadlines):
    body = api(a["users"]["viewer"]).get("dashboard").json_body["deadlines"]
    assert body["overdue"] == 1
    assert body["windows"] == [{"days": 30, "count": 1}, {"days": 60, "count": 3}, {"days": 90, "count": 3}]
    assert body["total"] == 4 and [r["name"] for r in body["rows"]] == ["Late", "Domain", "Driven", "Alone"]


def test_renewals_calendar_report(a, api, deadlines):
    viewer = api(a["users"]["viewer"])
    body = viewer.get("reports/renewals-calendar?months=3").json_body
    assert body["months"] == 3 and len(body["by_month"]) == 3
    assert body["from"] == today(a).replace(day=1).isoformat()
    assert body["to"] == reporting.month_end(today(a), 3).isoformat()
    assert "Far" not in {r["name"] for r in body["rows"]} and body["overdue"] == 1
    assert sum(m["count"] for m in body["by_month"]) == len([r for r in body["rows"] if r["date"] >= body["from"]])
    wide = viewer.get("reports/renewals-calendar?months=24").json_body
    assert "Far" in {r["name"] for r in wide["rows"]}
    assert viewer.get("reports/renewals-calendar").json_body["months"] == 12
    for bad in ("0", "25", "x", "1.5"):
        refused = viewer.get(f"reports/renewals-calendar?months={bad}")
        assert refused.status_code == 400 and "months" in refused.json_body["errors"], bad


def test_month_end():
    from datetime import date

    assert reporting.month_end(date(2026, 11, 5), 1) == date(2026, 11, 30)
    assert reporting.month_end(date(2026, 11, 5), 2) == date(2026, 12, 31)
    assert reporting.month_end(date(2026, 11, 5), 4) == date(2027, 2, 28)
    assert reporting.month_end(date(2027, 12, 31), 13) == date(2028, 12, 31)


def test_renewals_calendar_csv(a, api, deadlines):
    renewals.update_renewal(a["org"], a["users"]["editor"], deadlines["renewal"], {"name": "@Domain"})
    response = api(a["users"]["viewer"]).get("reports/renewals-calendar/export.csv?months=6")
    assert response.status_code == 200
    rows = read_csv(response)
    assert [r["name"] for r in rows] == ["Late", "'@Domain", "Driven", "Alone"]
    assert rows[0]["overdue"] == "yes" and rows[2]["contract"] == "Driven" and rows[2]["annualized_cost"] == "1200.00"
    event = AuditEvent.objects.filter(organization=a["org"], action="export").latest("created_at")
    assert event.changes == {"kind": "renewal_deadline", "rows": 4, "filters": {"months": 6}}


def test_contract_deadlines_leave_with_the_contracts_capability(a, deadlines, monkeypatch):
    real = caps.capabilities
    monkeypatch.setattr(caps, "capabilities", lambda org: real(org) - {caps.CONTRACTS})
    rows = reporting.deadline_rows(a["org"], today(a))
    assert {r["name"] for r in rows} == {"Driven", "Domain", "Late"}  # renewals only; Driven as its renewal
    assert all(r["contract"] is None and r["source"] == "renewal" for r in rows)


@pytest.mark.parametrize("decision", ["renew", "renegotiate"])
@pytest.mark.parametrize("renewals_on", [True, False])
def test_a_renewed_unlinked_term_has_no_deadline_left(a, monkeypatch, decision, renewals_on):
    """The renew action leaves the old term Active until its end date; its action is taken, so only the next term has
    a row (review blocker), with the renewals capability on or off."""
    from money import renewal_action

    if not renewals_on:
        real = caps.capabilities
        monkeypatch.setattr(caps, "capabilities", lambda org: real(org) - {caps.RENEWALS})
    old = contract(a, "Old", end_date=iso(a, 60), notice_value=90, notice_unit="days", auto_renew="yes")
    assert [r["name"] for r in reporting.deadline_rows(a["org"], today(a))] == ["Old"]
    new = renewal_action.renew(a["org"], a["users"]["editor"], old, {"decision": decision, "name": "New"}).successor
    assert new is not None
    old.refresh_from_db()
    assert old.status == "active"  # until its end date
    rows = reporting.deadline_rows(a["org"], today(a))
    assert [(r["name"], r["source"]) for r in rows] == [("New", "contract")]
    contracts.archive_contract(a["org"], a["users"]["editor"], new)  # the next term no longer counts: Old is back
    assert {r["name"] for r in reporting.deadline_rows(a["org"], today(a))} == {"Old"}


def test_contract_rows_date_is_the_stored_deadline_and_agrees_with_the_computation(a, deadlines):
    for row in reporting.deadline_rows(a["org"], today(a)):
        if row["source"] != "contract":
            continue
        c = Contract.objects.get(id=row["id"])
        computed = contracts.contract_deadline(c, a["org"].renewal_lead_days)
        assert c.cancellation_deadline is not None and computed.date == c.cancellation_deadline
        assert row["date"] == c.cancellation_deadline.isoformat()
        assert row["label"] == computed.label and row["explanation"] == computed.explanation


def test_a_stale_stored_deadline_keeps_its_date_with_a_neutral_label(a, api, deadlines, caplog):
    """Review blocker: a stored deadline its computation doesn't explain never fails the dashboard, the calendar or
    the feed; the stored date stays (it is what filters read), the label is neutral, and a warning names the
    contract by id only."""
    import icalendar

    alone = deadlines["alone"]
    stale = today(a) + timedelta(days=12)
    Contract.objects.filter(id=alone.id).update(cancellation_deadline=stale)
    client = api(a["users"]["viewer"])
    with caplog.at_level("WARNING", logger="softwarden.reports"):
        dashboard = client.get("dashboard")
        report = client.get("reports/renewals-calendar")
        csv_export = client.get("reports/renewals-calendar/export.csv")
        secret = client.post("calendar-feed").json_body["url"].split("token=", 1)[1]
        feed = api().client.get(f"/api/v1/calendar/renewals.ics?token={secret}")
    assert [r.status_code for r in (dashboard, report, csv_export, feed)] == [200, 200, 200, 200]
    for rows in (dashboard.json_body["deadlines"]["rows"], report.json_body["rows"]):
        row = next(r for r in rows if r["id"] == str(alone.id))
        assert row["date"] == stale.isoformat()
        # Alone doesn't auto-renew: the neutral words follow its action (renew_by), as the event title does.
        assert (row["action"], row["label"], row["label_text"]) == ("renew_by", None, "Renewal action due")
        assert row["explanation"] == ""
    events = {str(e["SUMMARY"]): e for e in icalendar.Calendar.from_ical(feed.content).walk("VEVENT")}
    assert events["Renewal action due: Alone"]["DTSTART"].dt == stale  # type: ignore[union-attr]
    warnings = [r.getMessage() for r in caplog.records if r.name == "softwarden.reports"]
    assert warnings and all(
        m == f"contract deadline differs from its computation contract={alone.id}" for m in warnings
    )
    assert "Alone" not in caplog.text


def test_deadlines_are_the_organizations_own(a, b, deadlines):
    contracts.create_contract(b["org"], b["users"]["editor"], {"name": "Theirs", "end_date": iso(a, 20)})
    assert "Theirs" not in {r["name"] for r in reporting.deadline_rows(a["org"], today(a))}
    assert {r["name"] for r in reporting.deadline_rows(b["org"], today(a))} == {"Theirs"}


# ---- expiring credentials ---------------------------------------------------------------------------------------------


def make_token(a, role, days, name):
    owner = Membership.objects.get(organization=a["org"], user=a["users"][role])
    token, _ = tokens.create_token(a["org"], a["users"]["admin"], owner, name, True, today(a) + timedelta(days=days))
    return token


def test_expiring_api_tokens(a, b, api):
    soon = make_token(a, "editor", 5, "Soon")
    make_token(a, "viewer", 25, "Viewer's")
    make_token(a, "editor", 90, "Later")
    revoked = make_token(a, "editor", 3, "Revoked")
    tokens.revoke_token(a["org"], a["users"]["admin"], revoked)
    lapsed = make_token(a, "editor", 10, "Lapsed")
    from api.models import ApiToken

    ApiToken.objects.filter(id=lapsed.id).update(expires_on=today(a) - timedelta(days=2))
    owner_b = Membership.objects.get(organization=b["org"], user=b["users"]["editor"])
    tokens.create_token(b["org"], b["users"]["admin"], owner_b, "Theirs", True, today(a) + timedelta(days=3))
    admin = api(a["users"]["admin"]).get("dashboard").json_body["expiring_credentials"]
    assert [r["name"] for r in admin["rows"]] == ["Lapsed", "Soon", "Viewer's"]
    assert admin["rows"][0]["expired"] and admin["rows"][0]["days"] == -2
    assert admin["rows"][1] == {
        "kind": "api_token",
        "id": str(soon.id),
        "name": "Soon",
        "prefix": soon.prefix,
        "owner": {"id": a["users"]["editor"].id, "name": "editor@orga.test", "email": "editor@orga.test"},
        "expires_on": iso(a, 5),
        "days": 5,
        "expired": False,
        "credential_type": None,
        "key_id": None,
        "application": None,
        "record": None,
    }
    editor = api(a["users"]["editor"]).get("dashboard").json_body["expiring_credentials"]
    assert [r["name"] for r in editor["rows"]] == ["Lapsed", "Soon"]  # their own only
    viewer = api(a["users"]["viewer"]).get("dashboard").json_body["expiring_credentials"]
    assert [r["name"] for r in viewer["rows"]] == ["Viewer's"] and viewer["count"] == 1


# ---- the dashboard ----------------------------------------------------------------------------------------------------

BLOCKS = {
    "as_of",
    "needs_attention",
    "deadlines",
    "expiring_credentials",
    "spend",
    "license_utilization",
    "data_quality",
}


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
def test_dashboard_blocks_for_every_role(a, api, books, role):
    response = api(a["users"][role]).get("dashboard")
    assert response.status_code == 200
    body = response.json_body
    assert set(body) == BLOCKS and all(body[k] is not None for k in BLOCKS)
    assert set(body["spend"]) == {"totals", "by_vendor", "by_department", "by_category"}
    for key in ("by_vendor", "by_department", "by_category"):
        assert per_currency(body["spend"][key]) == {"USD": Decimal("12000.00"), "EUR": Decimal("1399.99")}
    queues = {q["queue"]: q for q in body["needs_attention"]["queues"]}
    from notify.attention import QUEUES

    assert set(queues) == set(QUEUES) and set(body["needs_attention"]["counts"]) == {"all", "mine", "unassigned"}
    assert body["data_quality"]["as_of"] == today(a).isoformat()
    assert body["license_utilization"]["total"]["pools"] == 0


def test_dashboard_needs_attention_matches_the_page(a, api):
    client = api(a["users"]["editor"])
    page = client.get("needs-attention?scope=all").json_body
    summary = client.get("dashboard").json_body["needs_attention"]
    assert summary["counts"] == page["counts"]
    for q in summary["queues"]:
        assert q["all"] == len(page[q["queue"]]), q["queue"]


def test_dashboard_spend_folds_small_groups(a):
    org = a["org"]
    for i in range(reporting.DASHBOARD_TOP + 3):
        vendor = Vendor.objects.create(organization=org, name=f"Vendor {i:02d}")
        contract(a, f"C{i}", vendor=str(vendor.id), amount=str(100 + i))
    rows = reporting.dashboard(org, Membership.objects.get(organization=org, user=a["users"]["viewer"]),
                               today(a))["spend"]["by_vendor"]  # fmt: skip
    assert len(rows) == reporting.DASHBOARD_TOP + 1
    assert [r["gap"] for r in rows[:-1]] == [None] * reporting.DASHBOARD_TOP
    other = rows[-1]
    assert other["gap"] == "other" and other["groups"] == 3 and other["amount"] == "303.00"  # 100, 101, 102
    assert per_currency(rows)["USD"] == sum(Decimal(100 + i) for i in range(reporting.DASHBOARD_TOP + 3))


def test_dashboard_with_capabilities_off(a, api, books, deadlines, monkeypatch):
    real = caps.capabilities
    monkeypatch.setattr(caps, "capabilities", lambda org: real(org) - {caps.CONTRACTS, caps.DATA_QUALITY})
    body = api(a["users"]["viewer"]).get("dashboard").json_body
    assert body["spend"] is None and body["license_utilization"] is None and body["data_quality"] is None
    assert body["deadlines"] is not None and all(r["source"] == "renewal" for r in body["deadlines"]["rows"])
    monkeypatch.setattr(caps, "capabilities", lambda org: real(org) - {caps.CONTRACTS, caps.RENEWALS, caps.API_TOKENS})
    body = api(a["users"]["viewer"]).get("dashboard").json_body
    assert body["deadlines"] is None and body["expiring_credentials"]["rows"] == []


def test_dashboard_is_the_organizations_own(a, b, api, books):
    contracts.create_contract(b["org"], b["users"]["editor"], {"name": "Theirs", "amount": "5.00", "currency": "GBP"})
    body = api(a["users"]["viewer"]).get("dashboard").json_body
    assert "GBP" not in {t["currency"] for t in body["spend"]["totals"]}


def test_dashboard_query_count_does_not_grow_with_contracts(a, api):
    client = api(a["users"]["admin"])
    org = a["org"]

    def add(n):
        for i in range(n):
            vendor = Vendor.objects.create(organization=org, name=f"V{Vendor.objects.count()}-{i}")
            app = record(a, f"App {Resource.objects.count()}", department=Department.objects.create(
                organization=org, name=f"D{Department.objects.count()}"), cost_center=f"CC{i}")  # fmt: skip
            c = contract(a, f"C {Contract.objects.count()}", vendor=str(vendor.id), amount="10.00",
                         end_date=iso(a, 20 + i), create_renewal_obligation=True)  # fmt: skip
            line(a, c, app, "40")
            contracts.create_pool(org, a["users"]["editor"], {"application": str(app.id), "sku_name": f"S{i}",
                                                              "seats_purchased": 5})  # fmt: skip
            make_token(a, "editor", 5 + i, f"T{i}")

    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert client.get("dashboard").status_code == 200
        return len(ctx.captured_queries)

    add(1)
    count()  # the first request also saves the new session (three queries)
    one = count()
    add(6)
    assert count() == one
    assert one <= 40, one  # session and membership, Needs attention's queues, spend, deadlines, pools, data quality


def test_dashboard_loads_the_renewal_states_once_and_shares_them(a, books, deadlines, monkeypatch):
    """QA (second perf fix): Needs attention and the deadlines read the same renewals.queue_states; the
    dashboard loads them once and hands them to both, and the blocks are what each gives on its own."""
    org, today_ = a["org"], today(a)
    membership = Membership.objects.get(organization=org, user=a["users"]["editor"])
    attention_alone = reporting.needs_attention(org, membership.user, today_)
    deadlines_alone = reporting.deadline_rows(org, today_, until=today_ + timedelta(days=max(reporting.WINDOWS)))
    real, calls = renewals.queue_states, []

    def counted(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(renewals, "queue_states", counted)
    body = reporting.dashboard(org, membership, today_)
    assert len(calls) == 1
    assert deadlines_alone, "the fixture has deadlines to compare"
    assert body["needs_attention"] == attention_alone
    assert body["deadlines"]["rows"] == deadlines_alone[: reporting.DASHBOARD_DEADLINES]
    assert body["deadlines"]["total"] == len(deadlines_alone)


@pytest.mark.parametrize(
    "path", ["reports/spend?by=cost_center", "reports/renewals-calendar?months=24", "reports/spend/export.csv"]
)
def test_report_query_counts_do_not_grow(a, api, path):
    client = api(a["users"]["viewer"])

    def add(n):
        for i in range(n):
            app = record(a, f"App {Resource.objects.count()}")
            c = contract(a, f"C {Contract.objects.count()}", amount="10.00", end_date=iso(a, 20 + i),
                         create_renewal_obligation=bool(i % 2))  # fmt: skip
            line(a, c, app)
            line(a, c, a["app"], "10")

    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert client.get(path).status_code == 200
        return len(ctx.captured_queries)

    add(2)
    count()  # the first request also saves the new session (three queries)
    first = count()
    add(5)
    assert count() == first


# ---- response descriptions ----------------------------------------------------------------------------------------------


def keys_match(serializer, body, where="body"):
    """The OpenAPI description has exactly the keys the endpoint returns (nested ones too)."""
    from rest_framework import serializers as s

    fields = serializer.get_fields() if isinstance(serializer, s.Serializer) else serializer.child.get_fields()
    optional = {name for name, f in fields.items() if not f.required}
    assert set(body) <= set(fields) and set(fields) - optional <= set(body), (where, set(body) ^ set(fields))
    for name, field in fields.items():
        value = body.get(name)
        if value is None:
            continue
        if isinstance(field, s.ListSerializer) and value and isinstance(field.child, s.Serializer):
            for item in value:
                keys_match(field.child, item, f"{where}.{name}[]")
        elif isinstance(field, s.Serializer):
            keys_match(field, value, f"{where}.{name}")


def test_descriptions_match_the_bodies(a, api, books, deadlines):
    from money import report_serializers as rs

    make_token(a, "editor", 5, "Soon")
    contracts.create_pool(a["org"], a["users"]["editor"], {"application": str(a["app"].id), "sku_name": "E5"})
    client = api(a["users"]["admin"])
    keys_match(rs.DashboardSerializer(), client.get("dashboard").json_body)
    keys_match(rs.SpendReportSerializer(), client.get("reports/spend?by=category").json_body)
    keys_match(rs.CalendarReportSerializer(), client.get("reports/renewals-calendar").json_body)
    feed = client.post("calendar-feed").json_body
    keys_match(rs.CalendarFeedCreatedSerializer(), feed)
    keys_match(rs.CalendarFeedSerializer(), client.get("calendar-feed").json_body)


def test_named_gaps_are_never_folded(a):
    """Unallocated, no department and the other named gaps are always listed, however small; only real groups past
    the largest 8 fold into "Everything else" (review nit)."""
    org = a["org"]
    for i in range(reporting.DASHBOARD_TOP + 2):
        dept = Department.objects.create(organization=org, name=f"Dept {i:02d}")
        c = contract(a, f"C{i}", amount=str(1000 + i))
        line(a, c, record(a, f"R{i}", department=dept))
    contract(a, "Tiny unallocated", amount="1.00")  # no lines
    tiny = contract(a, "Tiny no department", amount="2.00")
    line(a, tiny, record(a, "Nobody's"))
    member = Membership.objects.get(organization=org, user=a["users"]["viewer"])
    rows = reporting.dashboard(org, member, today(a))["spend"]["by_department"]
    gaps = [r["gap"] for r in rows]
    assert gaps.count(None) == reporting.DASHBOARD_TOP and {"unallocated", "no_department", "other"} <= set(gaps)
    other = next(r for r in rows if r["gap"] == "other")
    assert other["groups"] == 2 and other["amount"] == "2001.00"  # Dept 00 and 01: 1000 + 1001
    assert per_currency(rows)["USD"] == Decimal(sum(1000 + i for i in range(reporting.DASHBOARD_TOP + 2)) + 3)
