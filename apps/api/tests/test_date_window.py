"""Stored dates stay inside the accepted window, and one bad row never fails a list.

`0001-01-01` is what .NET and Go clients serialize for an unset date. It used to be stored by a request that answered
500, and then every screen that works out renewal deadlines failed for the whole organization.
"""

from datetime import date, time, timedelta

import pytest
from django.utils import timezone

from catalog import services
from core import dates, doctor
from core.models import AuditEvent
from money import renewal_rules, renewals
from money.models import RenewalCycle, RenewalObligation
from notify import digest, reminders

EDGE = ["0001-01-01", "0001-03-01", "1899-12-31", "9999-12-31"]


def body(a, **kw):
    return {"name": "Edge renewal", "auto_renew": "yes", "applications": [str(a["app"].id)], **kw}


# ---- core.dates ------------------------------------------------------------------------------------------------------


def test_window_bounds():
    assert dates.parse_stored("1900-01-01") == date(1900, 1, 1)
    assert dates.parse_stored("9998-12-31") == date(9998, 12, 31)
    for raw in EDGE:
        with pytest.raises(ValueError, match="between 1900-01-01 and 9998-12-31"):
            dates.parse_stored(raw)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        dates.parse_stored("31/03/2027")


def test_shift_is_total():
    assert dates.shift(date(1, 1, 1), -90, "days") is None
    assert dates.shift(date(1, 3, 1), -3, "months") is None
    assert dates.shift(date(9999, 12, 31), 1, "years") is None
    assert dates.shift(date(2027, 3, 31), -1, "months") == date(2027, 2, 28)


@pytest.mark.parametrize(
    "args",
    [
        ("yes", date(1, 1, 1), None, "", None, 90),  # assumed: minus the lead time
        ("yes", date(1, 1, 1), 2, "months", None, 90),  # calculated: minus the notice period
        ("no", date(1, 1, 1), None, "", None, 90),  # renewal action
    ],
)
def test_action_deadline_out_of_range_has_no_date_and_a_reason(args):
    deadline = renewal_rules.action_deadline(*args)
    assert deadline.date is None and deadline.label is None
    assert "out of range" in deadline.explanation and "0001-01-01" in deadline.explanation


# ---- the API refuses edge dates and stores nothing -------------------------------------------------------------------


@pytest.mark.parametrize("field", ["renewal_date", "explicit_deadline"])
@pytest.mark.parametrize("raw", EDGE)
def test_create_refuses_dates_outside_the_window(a, api, field, raw):
    before = (RenewalObligation.objects.count(), AuditEvent.objects.count())
    response = api(a["users"]["editor"]).post("renewals", body(a, **{field: raw}))
    assert response.status_code == 400, response.json_body
    assert list(response.json_body["errors"]) == [field]
    assert "between 1900-01-01 and 9998-12-31" in response.json_body["errors"][field][0]
    assert (RenewalObligation.objects.count(), AuditEvent.objects.count()) == before


@pytest.mark.parametrize("field", ["renewal_date", "explicit_deadline"])
def test_cycle_patch_refuses_dates_outside_the_window(a, api, field):
    client = api(a["users"]["editor"])
    created = client.post("renewals", body(a, renewal_date="2027-03-31")).json_body
    cycle = RenewalCycle.objects.get(obligation_id=created["id"])
    events = AuditEvent.objects.count()
    response = client.patch(f"renewals/{created['id']}/cycles/{cycle.id}", {field: "0001-01-01"})
    assert response.status_code == 400 and list(response.json_body["errors"]) == [field]
    cycle.refresh_from_db()
    assert cycle.renewal_date == date(2027, 3, 31) and cycle.explicit_deadline is None
    assert AuditEvent.objects.count() == events


def test_preview_refuses_dates_outside_the_window(a, api):
    response = api(a["users"]["viewer"]).get("renewals/preview?renewal_date=0001-01-01")
    assert response.status_code == 400 and list(response.json_body["errors"]) == ["renewal_date"]


def test_a_create_whose_response_fails_writes_nothing(a, api, monkeypatch):
    """The create response is built inside the write's transaction: a request that answers 500 has stored nothing."""
    from money.viewsets import RenewalViewSet

    def boom(self, *args, **kwargs):
        raise RuntimeError("rendering failed")

    monkeypatch.setattr(RenewalViewSet, "detail_response", boom)
    before = (RenewalObligation.objects.count(), AuditEvent.objects.count())
    with pytest.raises(RuntimeError):
        api(a["users"]["editor"]).post("renewals", body(a, renewal_date="2027-03-31"))
    assert (RenewalObligation.objects.count(), AuditEvent.objects.count()) == before


# ---- a stored bad row never fails a list -----------------------------------------------------------------------------


@pytest.fixture
def stored_bad_row(a):
    """Rows as an older release (or a restore, or a direct database edit) could have stored them: year 1."""
    org, editor = a["org"], a["users"]["editor"]
    today = services.org_today(org)
    good = renewals.create_renewal(
        org,
        editor,
        body(
            a,
            name="Good renewal",
            explicit_deadline=(today + timedelta(days=5)).isoformat(),
            responsible=str(a["people"]["priya"].id),
            followup=editor.id,
        ),
    )
    bad = renewals.create_renewal(
        org, editor, body(a, name="Bad renewal", responsible=str(a["people"]["priya"].id), followup=editor.id)
    )
    RenewalCycle.objects.filter(obligation=bad).update(renewal_date=date(1, 1, 1), term_value=1, term_unit="years")
    worse = renewals.create_renewal(org, editor, body(a, name="Worse renewal", auto_renew="no"))
    RenewalCycle.objects.filter(obligation=worse).update(
        renewal_date=date(1, 3, 1), notice_value=3, notice_unit="months"
    )
    return {"good": good, "bad": bad, "worse": worse}


def test_every_renewal_read_still_answers(a, api, stored_bad_row):
    client = api(a["users"]["viewer"])
    for url in (
        "dashboard",
        "renewals",
        f"renewals/{stored_bad_row['bad'].id}",
        f"renewals/{stored_bad_row['worse'].id}",
        "needs-attention",
        "composite/renewals",
        "composite/deadlines",
        "reports/renewals-calendar",
        "reports/renewals-calendar/export.csv",
    ):
        response = client.get(url)
        assert response.status_code == 200, (url, response.status_code)
    rows = {r["name"]: r for r in client.get("renewals").json_body}
    assert rows["Good renewal"]["deadline"]["date"] is not None
    bad = rows["Bad renewal"]["deadline"]
    assert bad["date"] is None and "out of range" in bad["explanation"]
    assert "No deadline" in rows["Bad renewal"]["gaps"]
    detail = client.get(f"renewals/{stored_bad_row['bad'].id}").json_body
    assert detail["cycle"]["next_renewal_date"] is None


def test_queue_states_still_answer(a, stored_bad_row):
    states = renewals.queue_states(a["org"], services.org_today(a["org"]))
    names = {s.obligation.name: s for s in states}
    assert names["Good renewal"].deadline.date is not None


def test_digest_and_reminder_jobs_still_answer(a, stored_bad_row, settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    a["org"].digest_time = time(0, 0)
    a["org"].save()
    built = digest.run_for_org(a["org"], timezone.now())
    assert built is not None
    reminders.generate(a["org"], services.org_today(a["org"]))


def test_doctor_names_stored_dates_outside_the_window(a, stored_bad_row):
    found = doctor.check_stored_dates(a["org"])
    assert found.status == doctor.WARN
    assert "2 stored date(s)" in found.message and "renewal cycle renewal date" in found.message
    RenewalCycle.objects.filter(obligation__in=[stored_bad_row["bad"], stored_bad_row["worse"]]).update(
        renewal_date=date(2027, 1, 1)
    )
    assert doctor.check_stored_dates(a["org"]).status == doctor.OK


def test_doctor_ignores_discovered_dates_such_as_a_never_expiring_secret(a):
    """Entra reports a secret that never expires with endDateTime 9999-12-31, stored as is: correct data, no warning."""
    from discovery import engine as discovery
    from discovery.models import DiscoveredCredential
    from tests.test_discovery_sources import make_connector

    discovery.run(make_connector(a["org"], a["users"]["admin"]))
    assert DiscoveredCredential.objects.filter(organization=a["org"]).exists()
    DiscoveredCredential.objects.filter(organization=a["org"]).update(expires_on=date(9999, 12, 31))
    found = doctor.check_stored_dates(a["org"])
    assert found.status == doctor.OK, found.message


def test_next_term_defaults_stay_inside_the_window(a):
    """A term ending on the last accepted day has no default next start (9999-01-01 would be refused): left empty."""
    from money import contracts, renewal_action

    last = contracts.create_contract(
        a["org"], a["users"]["editor"], {"name": "Last day", "start_date": "9998-01-01", "end_date": "9998-12-31"}
    )
    proposed = renewal_action.defaults(a["org"], last)
    assert proposed["start_date"] is None and proposed["end_date"] is None
    near = contracts.create_contract(
        a["org"], a["users"]["editor"], {"name": "Near the end", "start_date": "9998-01-01", "end_date": "9998-09-30"}
    )
    proposed = renewal_action.defaults(a["org"], near)
    assert proposed["start_date"] == "9998-10-01" and proposed["end_date"] is None  # nine months on is 9999-06-30


# ---- every other stored date uses the same window --------------------------------------------------------------------


def field_errors(call):
    from core.problems import FieldErrors

    with pytest.raises(FieldErrors) as caught:
        call()
    return caught.value.errors


def test_contract_dates(a):
    from money import contracts

    errors = field_errors(
        lambda: contracts.create_contract(
            a["org"], a["users"]["editor"], {"name": "Edge", "start_date": "0001-01-01", "end_date": "9999-12-31"}
        )
    )
    assert errors["start_date"] == [dates.WINDOW_MESSAGE] and errors["end_date"] == [dates.WINDOW_MESSAGE]


def test_review_dates(a):
    org, editor, app = a["org"], a["users"]["editor"], a["app"]
    data = {"confirmed_by": str(a["people"]["priya"].id), "evidence": "Asked"}
    errors = field_errors(lambda: services.record_review(org, editor, app, {**data, "confirmation_date": "0001-01-01"}))
    assert errors["confirmation_date"] == [dates.WINDOW_MESSAGE]
    errors = field_errors(lambda: services.record_review(org, editor, app, {**data, "next_review_date": "9999-12-31"}))
    assert errors["next_review_date"] == [dates.WINDOW_MESSAGE]


def test_bulk_next_review_date(a, api):
    response = api(a["users"]["editor"]).post(
        "bulk-edit/preview",
        {"kind": "application", "ids": [str(a["app"].id)], "changes": {"next_review_date": "0001-01-01"}},
    )
    assert response.status_code == 400, response.json_body
    assert response.json_body["errors"]["next_review_date"] == [dates.WINDOW_MESSAGE]


def test_assessment_target_date(a):
    from catalog import assessments

    errors = field_errors(
        lambda: assessments.create_assessment(
            a["org"],
            a["users"]["editor"],
            {"target": str(a["app"].id), "change_type": "retirement", "title": "Retire", "target_date": "0001-01-01"},
        )
    )
    assert errors["target_date"] == [dates.WINDOW_MESSAGE]


def test_vendor_risk_dates_and_derived_expiry(a):
    from catalog import vendor_risk
    from catalog.models import Vendor

    vendor = Vendor.objects.create(organization=a["org"], name="Edge vendor")
    editor = a["users"]["editor"]
    errors = field_errors(
        lambda: vendor_risk.update_profile(a["org"], editor, vendor, {"questionnaire_requested_on": "0001-01-01"})
    )
    assert errors["questionnaire_requested_on"] == [dates.WINDOW_MESSAGE]
    # Inside the window, but the derived expiry (issued on plus 24 months) would not be.
    errors = field_errors(
        lambda: vendor_risk.create_document(
            a["org"], editor, vendor, {"kind": "soc2", "issued_on": "9998-06-01", "validity_months": 24}
        )
    )
    assert "after 9998-12-31" in errors["issued_on"][0]


def test_custom_date_fields_and_compliance_dates():
    from catalog import compliance, customfields
    from catalog.models import CustomField

    with pytest.raises(customfields.Invalid, match="between 1900-01-01"):
        customfields.clean_value(CustomField(type="date"), "0001-01-01")
    errors: dict[str, list[str]] = {}
    assert compliance._date("0001-01-01", "last_periodic_review", errors) is None
    assert compliance._date(date(1, 1, 1), "next_periodic_review", errors) is None
    assert errors == {"last_periodic_review": [dates.WINDOW_MESSAGE], "next_periodic_review": [dates.WINDOW_MESSAGE]}


def test_csv_contract_import_names_the_date_column(a, api):
    from tests.test_contract_imports import contract_csv
    from tests.test_imports import rows_of, upload

    editor = api(a["users"]["editor"])
    run = upload(editor, contract_csv({"name": "Edge", "start_date": "0001-01-01"}), kind="contract").json_body
    errors = rows_of(editor, run["id"])[0]["errors"]
    assert {e["column"] for e in errors} == {"start_date"}
    assert "between 1900-01-01 and 9998-12-31" in errors[0]["message"]
