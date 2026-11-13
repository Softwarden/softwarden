"""Reminders (notify/reminders.py; notify/services.py; notify/reminder_viewsets.py): one
reminder per source occurrence from renewal cycles, contracts, periodic reviews, record reviews, custom dates and
credentials; the same dates the rest of the app shows (one source of truth); idempotent reruns; moved and vanished
sources; regeneration on save; snooze, acknowledge, reopen and resolve; capabilities, timezones, tenancy, roles, the
list's query count, the schedule, and the Needs attention rows."""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from django.core import mail
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from catalog import admin_services, compliance, customfields, services
from catalog.models import RecordReview, Resource, Team, Vendor
from core import capabilities as caps
from core import jobs
from core.models import AuditEvent
from money import contracts, renewals, reporting
from money.models import RenewalCycle
from notify import reminders
from notify.attention import UNCOUNTED, attention_queues
from notify.models import Reminder
from notify.reminders import Fact
from notify.serializers import ReminderSerializer
from tests.test_drf_resources import described

pytestmark = [pytest.mark.django_db, pytest.mark.one_source_of_truth]

ZERO = dict.fromkeys(reminders.KEYS, 0)


def today(a):
    return services.org_today(a["org"])


def iso(a, days):
    return (today(a) + timedelta(days=days)).isoformat()


def on(a, days):
    return today(a) + timedelta(days=days)


def contract(a, name, **data):
    return contracts.create_contract(a["org"], a["users"]["editor"], {"name": name, **data})


def generate(a):
    return reminders.generate(a["org"], today(a))


def live(org, **filters):
    return Reminder.objects.filter(organization=org, status__in=["open", "snoozed", "acknowledged"], **filters)


def one(org, **filters):
    found = list(Reminder.objects.filter(organization=org, **filters))
    assert len(found) == 1, found
    return found[0]


def enterprise(org):
    """The compliance profile on (enterprise edition plus the organization setting)."""
    fresh, _ = caps.set_edition(org, "enterprise")
    fresh.settings = {**fresh.settings, compliance.SETTING: True}
    fresh.save(update_fields=["settings"])
    compliance.forget(org.pk)
    org.edition, org.settings = fresh.edition, fresh.settings
    return org


def date_field(a, name="cert_expiry", applies_to=("application", "vendor", "contract", "license_pool")):
    return customfields.create_field(
        a["org"],
        a["users"]["admin"],
        {"name": name, "label": "Certificate expiry", "type": "date", "is_reminder_source": True,
         "applies_to": list(applies_to)},
    )  # fmt: skip


def review(a, record, next_days):
    return services.record_review(
        a["org"],
        a["users"]["editor"],
        record,
        {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Asked", "next_review_date": iso(a, next_days)},
    )


@pytest.fixture
def sources(a):
    """One of every source: a renewal, a contract alone, a contract that drives a renewal, a periodic review, a record
    review, custom dates on a record, a vendor, a contract and a licence pool."""
    org, editor = a["org"], a["users"]["editor"]
    enterprise(org)
    domain = renewals.create_renewal(org, editor, {"name": "Domain", "renewal_date": iso(a, 20), "auto_renew": "yes",
                                                   "notice_value": 10, "notice_unit": "days",
                                                   "followup": editor.id})  # fmt: skip  # deadline in 10 days
    alone = contract(a, "Alone", end_date=iso(a, org.renewal_lead_days + 30), auto_renew="no")  # in 30 days
    driven = contract(a, "Driven", end_date=iso(a, 100), notice_value=60, notice_unit="days", auto_renew="yes",
                      create_renewal_obligation=True)  # fmt: skip  # in 40 days, through its renewal
    review(a, a["app"], 60)
    date_field(a)
    services.update_resource(
        org, editor, a["app"], {"next_periodic_review": iso(a, 45), "custom_field_data": {"cert_expiry": iso(a, 15)}}
    )
    vendor = admin_services.create_named(
        Vendor, org, a["users"]["admin"], {"name": "Acme", "custom_field_data": {"cert_expiry": iso(a, 25)}}
    )
    pool = contracts.create_pool(org, editor, {"application": str(a["app"].id), "sku_name": "E5",
                                               "custom_field_data": {"cert_expiry": iso(a, 35)}})  # fmt: skip
    contracts.update_contract(org, editor, alone, {"custom_field_data": {"cert_expiry": iso(a, 50)}})
    alone.refresh_from_db()
    driven.refresh_from_db()
    return {"domain": domain, "alone": alone, "driven": driven, "vendor": vendor, "pool": pool}


# ---- generation from every source ---------------------------------------------------------------------------------


def test_every_source_produces_its_reminder(a, sources):
    counts = generate(a)
    org = a["org"]
    assert counts == {**ZERO, "created": 9}
    domain_cycle = RenewalCycle.objects.get(obligation=sources["domain"])
    driven_cycle = RenewalCycle.objects.get(obligation_id=sources["driven"].renewal_obligation_id)
    found = {(r.source_type, r.source_id, r.source_field): r for r in Reminder.objects.filter(organization=org)}
    expected = {
        ("renewal_cycle", domain_cycle.id, ""): (on(a, 10), "Domain", "Cancellation deadline"),
        ("renewal_cycle", driven_cycle.id, ""): (on(a, 40), "Driven", "Cancellation deadline"),
        ("contract", sources["alone"].id, ""): (on(a, 30), "Alone", "Renewal action due"),
        ("periodic_review", a["app"].id, ""): (on(a, 45), a["app"].name, "Periodic review due"),
        ("record_review", a["app"].id, ""): (on(a, 60), a["app"].name, "Record review due"),
        ("custom_date", a["app"].id, "cert_expiry"): (on(a, 15), a["app"].name, "Certificate expiry"),
        ("custom_date", sources["vendor"].id, "cert_expiry"): (on(a, 25), "Acme", "Certificate expiry"),
        ("custom_date", sources["pool"].id, "cert_expiry"): (on(a, 35), "E5", "Certificate expiry"),
        ("custom_date", sources["alone"].id, "cert_expiry"): (on(a, 50), "Alone", "Certificate expiry"),
    }
    assert {k: (r.due_date, r.title, r.label) for k, r in found.items()} == expected
    assert all(r.status == "open" and r.notify_cycle == 0 for r in found.values())
    # The key columns name the source's row, so routing and the composite keys hold.
    assert found["renewal_cycle", domain_cycle.id, ""].cycle_id == domain_cycle.id
    assert found["contract", sources["alone"].id, ""].contract_id == sources["alone"].id
    assert found["custom_date", sources["pool"].id, "cert_expiry"].license_pool_id == sources["pool"].id
    vendor_row = found["custom_date", sources["vendor"].id, "cert_expiry"]
    assert (vendor_row.resource_id, vendor_row.contract_id, vendor_row.license_pool_id, vendor_row.cycle_id) == (
        None,
        None,
        None,
        None,
    )
    assert found["renewal_cycle", domain_cycle.id, ""].deadline_label == "calculated"


def test_the_generator_sends_nothing_and_audits_nothing(a, sources):
    before = AuditEvent.objects.filter(organization=a["org"]).count()
    generate(a)
    assert mail.outbox == []
    assert AuditEvent.objects.filter(organization=a["org"]).count() == before


def test_a_rerun_changes_nothing(a, sources):
    generate(a)
    rows = {r.id: (r.due_date, r.status, r.updated_at) for r in Reminder.objects.filter(organization=a["org"])}
    assert generate(a) == ZERO
    assert generate(a) == ZERO
    assert {r.id: (r.due_date, r.status, r.updated_at) for r in Reminder.objects.filter(organization=a["org"])} == rows


def test_a_contract_deadline_in_30_days_makes_exactly_one_reminder(a):
    """The generator's half: one reminder at the deadline, whatever the reruns."""
    alone = contract(a, "Thirty", end_date=iso(a, a["org"].renewal_lead_days + 30), auto_renew="no")
    alone.refresh_from_db()
    for _ in range(3):
        generate(a)
    reminder = one(a["org"], source_type="contract")
    assert reminder.due_date == alone.cancellation_deadline == on(a, 30)


# ---- one source of truth --------------------------------------------------------------------------------------------


def test_reminders_are_exactly_the_deadline_rows(a, sources):
    """The renewals and contracts reminders are the dashboard's and calendar's rows, date for date; a contract that
    drives a renewal is represented only by the renewal, on the contract's own cancellation deadline."""
    generate(a)
    org = a["org"]
    rows = reporting.deadline_rows(org, today(a))
    money_reminders = live(org, source_type__in=["renewal_cycle", "contract"])
    assert sorted((r["date"], r["name"]) for r in rows) == sorted(
        (m.due_date.isoformat(), m.title) for m in money_reminders
    )
    driven = sources["driven"]
    assert not Reminder.objects.filter(source_type="contract", source_id=driven.id).exists()
    renewal_reminder = one(org, source_type="renewal_cycle", title="Driven")
    state = next(s for s in renewals.queue_states(org, today(a)) if s.obligation.id == driven.renewal_obligation_id)
    assert renewal_reminder.due_date == driven.cancellation_deadline == state.deadline.date
    assert one(org, source_type="contract").due_date == sources["alone"].cancellation_deadline


def test_moving_the_contracts_end_date_moves_the_renewal_reminder(a, sources):
    generate(a)
    driven = sources["driven"]
    before = one(a["org"], source_type="renewal_cycle", title="Driven")
    contracts.update_contract(a["org"], a["users"]["editor"], driven, {"end_date": iso(a, 130)})
    assert generate(a) == {**ZERO, "moved": 1}
    driven.refresh_from_db()
    after = one(a["org"], source_type="renewal_cycle", title="Driven")
    assert after.id == before.id and after.due_date == driven.cancellation_deadline == on(a, 70)
    row = next(r for r in reporting.deadline_rows(a["org"], today(a)) if r["name"] == "Driven")
    assert row["date"] == after.due_date.isoformat() and row["cycle"] == str(after.cycle_id)


def test_linking_a_contract_to_a_renewal_hands_its_reminder_to_the_renewal(a):
    org, editor = a["org"], a["users"]["editor"]
    alone = contract(a, "Later linked", end_date=iso(a, 100), notice_value=1, notice_unit="months", auto_renew="yes")
    generate(a)
    assert one(org, source_type="contract").status == "open"
    domain = renewals.create_renewal(org, editor, {"name": "Later linked", "renewal_date": iso(a, 300)})
    contracts.update_contract(org, editor, alone, {"renewal_obligation": str(domain.id)})
    generate(a)
    alone.refresh_from_db()
    assert one(org, source_type="contract").status == "obsolete"  # the renewal speaks for it now
    assert one(org, source_type="renewal_cycle", status="open").due_date == alone.cancellation_deadline


# ---- moved, closed and returning sources ------------------------------------------------------------------------------


def test_a_moved_date_moves_an_open_or_snoozed_reminder_in_place(a):
    org = a["org"]
    date_field(a, applies_to=("application",))
    services.update_resource(org, a["users"]["editor"], a["app"], {"custom_field_data": {"cert_expiry": iso(a, 15)}})
    generate(a)
    reminder = one(org, source_type="custom_date")
    Reminder.objects.filter(id=reminder.id).update(status="snoozed", snoozed_until=on(a, 5))
    services.update_resource(org, a["users"]["editor"], a["app"], {"custom_field_data": {"cert_expiry": iso(a, 40)}})
    assert generate(a) == {**ZERO, "moved": 1}
    moved = one(org, source_type="custom_date")
    assert (moved.id, moved.due_date, moved.status, moved.snoozed_until) == (
        reminder.id,
        on(a, 40),
        "snoozed",
        on(a, 5),
    )


def test_an_acknowledged_reminder_closes_and_a_new_one_opens_for_a_new_date(a, api):
    org = a["org"]
    date_field(a, applies_to=("application",))
    services.update_resource(org, a["users"]["editor"], a["app"], {"custom_field_data": {"cert_expiry": iso(a, 15)}})
    generate(a)
    first = one(org, source_type="custom_date")
    assert api(a["users"]["editor"]).post(f"reminders/{first.id}/acknowledge").status_code == 200
    assert generate(a) == ZERO  # acknowledged stays acknowledged
    services.update_resource(org, a["users"]["editor"], a["app"], {"custom_field_data": {"cert_expiry": iso(a, 40)}})
    assert generate(a) == {**ZERO, "created": 1, "obsoleted": 1}
    first.refresh_from_db()
    assert first.status == "obsolete" and first.resolution_kind == "source_changed"
    assert one(org, source_type="custom_date", status="open").due_date == on(a, 40)
    # Back to the first date: the occurrence comes back as it was, acknowledged, in the same notification cycle.
    services.update_resource(org, a["users"]["editor"], a["app"], {"custom_field_data": {"cert_expiry": iso(a, 15)}})
    assert generate(a) == {**ZERO, "reopened": 1, "obsoleted": 1}
    first.refresh_from_db()
    assert (first.status, first.notify_cycle, first.resolution_kind, first.resolved_at) == ("acknowledged", 0, "", None)
    assert first.acknowledged_by_id == a["users"]["editor"].id


def test_an_acknowledged_reminder_survives_its_source_briefly_going_away(a, api, sources):
    """Archive and restore, or a capability or profile flip: the same date comes back acknowledged, not open."""
    org, editor = a["org"], a["users"]["editor"]
    generate(a)
    deadline = one(org, source_type="contract")
    api(editor).post(f"reminders/{deadline.id}/acknowledge")
    contracts.archive_contract(org, editor, sources["alone"])
    generate(a)
    deadline.refresh_from_db()
    assert deadline.status == "obsolete" and deadline.acknowledged_at is not None  # kept for a return
    contracts.restore_contract(org, editor, sources["alone"])
    assert generate(a)["reopened"] == 2  # the deadline (acknowledged) and the contract's custom date (open)
    deadline.refresh_from_db()
    assert (deadline.status, deadline.notify_cycle) == ("acknowledged", 0)
    custom = one(org, source_type="custom_date", source_id=sources["alone"].id)
    assert (custom.status, custom.notify_cycle, custom.acknowledged_at) == ("open", 1, None)


def test_leaving_acknowledged_forgets_who_acknowledged(a, api, due):
    editor = api(a["users"]["editor"])
    for verb, body in (("snooze", {"until": iso(a, 3)}), ("reopen", {}), ("resolve", {"kind": "done", "note": "x"})):
        assert editor.post(f"reminders/{due.id}/acknowledge").status_code == 200
        after = editor.post(f"reminders/{due.id}/{verb}", body).json_body
        assert after["status"] != "acknowledged" and (after["acknowledged_at"], after["acknowledged_by"]) == (
            None,
            None,
        )
        due.refresh_from_db()
        assert (due.acknowledged_at, due.acknowledged_by_id) == (None, None), verb
        if verb == "snooze":
            editor.post(f"reminders/{due.id}/reopen")


def test_reopening_a_snooze_that_ran_out_changes_nothing(a, api, due):
    Reminder.objects.filter(id=due.id).update(status="snoozed", snoozed_until=on(a, -1))
    reopened = api(a["users"]["editor"]).post(f"reminders/{due.id}/reopen")
    assert reopened.status_code == 200 and reopened.json_body["status"] == "open"
    assert not AuditEvent.objects.filter(action="reminder.reopened").exists()


def test_closed_history_for_other_dates_is_never_read(a, sources):
    generate(a)
    org = a["org"]
    vendor = sources["vendor"]
    Reminder.objects.bulk_create(
        Reminder(
            organization=org,
            source_type="custom_date",
            source_id=vendor.id,
            source_field="cert_expiry",
            due_date=on(a, -400 - i),
            title="Acme",
            label="Old",
            status=status,
        )  # fmt: skip
        for i, status in enumerate(["done", "obsolete"] * 50)
    )
    wanted = reminders.desired(org, today(a), reminders.ALL_TYPES)
    read = list(reminders.existing_rows(org, reminders.ALL_TYPES, None, wanted))
    assert len(read) == 9 and all(r.label != "Old" for r in read)  # the live ones only
    assert generate(a) == ZERO
    assert Reminder.objects.filter(organization=org, label="Old").count() == 100  # left as history


def test_a_done_reminder_is_never_resurrected_for_its_date(a, api):
    org = a["org"]
    date_field(a, applies_to=("application",))
    services.update_resource(org, a["users"]["editor"], a["app"], {"custom_field_data": {"cert_expiry": iso(a, 15)}})
    generate(a)
    first = one(org, source_type="custom_date")
    resolved = api(a["users"]["editor"]).post(f"reminders/{first.id}/resolve", {"kind": "done", "evidence": "CHG-1"})
    assert resolved.status_code == 200 and resolved.json_body["status"] == "done"
    assert generate(a) == ZERO
    services.update_resource(org, a["users"]["editor"], a["app"], {"custom_field_data": {"cert_expiry": iso(a, 400)}})
    assert generate(a) == {**ZERO, "created": 1}  # the next date is a new obligation
    services.update_resource(org, a["users"]["editor"], a["app"], {"custom_field_data": {"cert_expiry": iso(a, 15)}})
    generate(a)
    first.refresh_from_db()
    assert first.status == "done"  # a person said so: the same date never comes back
    assert one(org, source_type="custom_date", due_date=on(a, 400)).status == "obsolete"


def test_archiving_or_deleting_the_source_makes_its_reminders_obsolete(a, sources):
    org, editor = a["org"], a["users"]["editor"]
    generate(a)
    alone = sources["alone"]
    contracts.archive_contract(org, editor, alone)
    assert generate(a)["obsoleted"] == 2  # its cancellation deadline and its custom date
    assert {r.status for r in Reminder.objects.filter(source_id=alone.id)} == {"obsolete"}
    contracts.restore_contract(org, editor, alone)
    assert generate(a)["reopened"] == 2
    assert {(r.status, r.notify_cycle) for r in Reminder.objects.filter(source_id=alone.id)} == {("open", 1)}
    # Deleting: the reminder outlives the vendor as history, closed.
    vendor_id = sources["vendor"].id
    admin_services.delete_named(Vendor, org, a["users"]["admin"], sources["vendor"])
    generate(a)
    assert one(org, source_id=vendor_id).status == "obsolete"


def test_a_deleted_record_leaves_its_reminders_closed_with_the_key_cleared(a):
    org = a["org"]
    date_field(a, applies_to=("component",))
    job = services.create_resource(org, a["users"]["editor"], {"name": "Job", "kind": "component", "lifecycle": "active",
                                                               "custom_field_data": {"cert_expiry": iso(a, 9)}})  # fmt: skip
    generate(a)
    job_id = job.id
    assert one(org, source_id=job_id).resource_id == job_id
    services.delete_resource(org, a["users"]["admin"], job)  # a reminder never blocks a delete
    assert not Resource.objects.filter(id=job_id).exists()
    generate(a)
    gone = one(org, source_id=job_id)
    assert (gone.status, gone.resource_id) == ("obsolete", None)


def test_a_completed_cycle_closes_its_reminder_and_the_next_cycle_gets_one(a):
    org, editor = a["org"], a["users"]["editor"]
    domain = renewals.create_renewal(org, editor, {"name": "Domain", "renewal_date": iso(a, 20), "auto_renew": "yes",
                                                   "notice_value": 10, "notice_unit": "days"})  # fmt: skip
    generate(a)
    cycle = RenewalCycle.objects.get(obligation=domain)
    renewals.record_decision(org, editor, domain, cycle.id, {"decision": "renew"})
    assert generate(a) == ZERO  # decided but not completed: the action is still open (as Needs attention says)
    renewals.record_completion(
        org, editor, domain, cycle.id, {"completed_on": iso(a, 0), "completion_evidence": "Paid"}
    )
    assert generate(a)["obsoleted"] == 1
    renewals.establish_next_cycle(org, editor, domain, {"renewal_date": iso(a, 385)})
    generate(a)
    nxt = RenewalCycle.objects.get(obligation=domain, completed_on__isnull=True)
    assert one(org, source_type="renewal_cycle", status="open").source_id == nxt.id


# ---- on save -----------------------------------------------------------------------------------------------------------


def test_writes_regenerate_their_source_after_commit(a, django_capture_on_commit_callbacks):
    org, editor = a["org"], a["users"]["editor"]
    date_field(a, applies_to=("application", "vendor"))
    with django_capture_on_commit_callbacks(execute=True):
        thirty = contract(a, "Thirty", end_date=iso(a, org.renewal_lead_days + 30), auto_renew="no")
    assert one(org, source_type="contract").due_date == on(a, 30)
    with django_capture_on_commit_callbacks(execute=True):
        contracts.update_contract(org, editor, thirty, {"end_date": iso(a, org.renewal_lead_days + 50)})
    assert one(org, source_type="contract").due_date == on(a, 50)
    with django_capture_on_commit_callbacks(execute=True):
        services.update_resource(org, editor, a["app"], {"custom_field_data": {"cert_expiry": iso(a, 12)}})
        review(a, a["app"], 70)
    assert one(org, source_type="custom_date").due_date == on(a, 12)
    assert one(org, source_type="record_review").due_date == on(a, 70)
    with django_capture_on_commit_callbacks(execute=True):
        renewals.create_renewal(org, editor, {"name": "Domain", "explicit_deadline": iso(a, 5)})
    assert one(org, source_type="renewal_cycle").due_date == on(a, 5)
    with django_capture_on_commit_callbacks(execute=True):
        contracts.archive_contract(org, editor, thirty)
    assert one(org, source_type="contract").status == "obsolete"


def test_turning_the_compliance_profile_on_regenerates_periodic_reviews(a, django_capture_on_commit_callbacks):
    org = a["org"]
    caps.set_edition(org, "enterprise")
    org.refresh_from_db()
    Resource.objects.filter(id=a["app"].id).update(next_periodic_review=on(a, 45))
    with django_capture_on_commit_callbacks(execute=True):
        admin_services.update_organization(org, a["users"]["admin"], {compliance.SETTING: True})
    assert one(org, source_type="periodic_review").due_date == on(a, 45)
    org.refresh_from_db()
    with django_capture_on_commit_callbacks(execute=True):
        admin_services.update_organization(org, a["users"]["admin"], {compliance.SETTING: False})
    assert one(org, source_type="periodic_review").status == "obsolete"


def test_a_write_that_rolls_back_regenerates_nothing(a, django_capture_on_commit_callbacks):
    org = a["org"]
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        try:
            with transaction.atomic():
                contract(a, "Never", end_date=iso(a, 120), auto_renew="no")
                raise RuntimeError("rolled back")
        except RuntimeError:
            pass
    assert callbacks == [] and not Reminder.objects.filter(organization=org).exists()


def test_a_failing_regeneration_never_breaks_the_write(a, django_capture_on_commit_callbacks, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(reminders, "sync", broken)
    with django_capture_on_commit_callbacks(execute=True):
        made = contract(a, "Kept", end_date=iso(a, 120), auto_renew="no")
    assert made.pk and not Reminder.objects.exists()
    assert "reminder regeneration failed" in caplog.text and "Kept" not in caplog.text


def test_writes_that_cant_move_a_date_schedule_nothing(a, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks() as callbacks:
        admin_services.create_named(Team, a["org"], a["users"]["admin"], {"name": "Night shift"})
    assert callbacks == []


# ---- snooze, acknowledge, reopen, resolve ---------------------------------------------------------------------------


@pytest.fixture
def due(a):
    """An open contract reminder due in 30 days."""
    contract(a, "Thirty", end_date=iso(a, a["org"].renewal_lead_days + 30), auto_renew="no")
    generate(a)
    return one(a["org"], source_type="contract")


def test_viewers_read_and_editors_act(a, api, due):
    viewer, editor = api(a["users"]["viewer"]), api(a["users"]["editor"])
    assert viewer.get("reminders").status_code == 200
    assert viewer.get(f"reminders/{due.id}").status_code == 200
    for verb, body in (("snooze", {"until": iso(a, 3)}), ("acknowledge", {}), ("reopen", {}), ("resolve", {})):
        assert viewer.post(f"reminders/{due.id}/{verb}", body).status_code == 403, verb
    assert editor.post(f"reminders/{due.id}/acknowledge").status_code == 200
    assert api(a["users"]["admin"]).post(f"reminders/{due.id}/reopen").status_code == 200
    assert editor.get("reminders").status_code == 200 and api().get("reminders").status_code in (401, 403)


def test_snoozing_quiets_the_reminder_through_the_date_then_it_returns(a, api, due):
    editor = api(a["users"]["editor"])
    for bad, message in (
        ({}, "Enter a date as YYYY-MM-DD."),
        ({"until": iso(a, 0)}, "Choose a date after today."),
        ({"until": iso(a, 91)}, "Snooze for 90 days at most."),
    ):
        refused = editor.post(f"reminders/{due.id}/snooze", bad)
        assert refused.status_code == 400 and refused.json_body["errors"]["until"] == [message]
    snoozed = editor.post(f"reminders/{due.id}/snooze", {"until": iso(a, 3)})
    assert snoozed.status_code == 200
    assert (snoozed.json_body["status"], snoozed.json_body["snoozed_until"]) == ("snoozed", iso(a, 3))
    event = AuditEvent.objects.get(action="reminder.snoozed")
    assert event.target_type == "Reminder" and event.changes["until"] == iso(a, 3)
    assert event.prechange is not None and event.postchange is not None
    assert (event.prechange["status"], event.postchange["status"]) == ("open", "snoozed")
    assert generate(a) == ZERO  # the generator leaves a running snooze alone
    # Still snoozed on its last day; open again the day after, before and after the generator runs.
    Reminder.objects.filter(id=due.id).update(snoozed_until=on(a, 0))
    assert editor.get(f"reminders/{due.id}").json_body["status"] == "snoozed"
    Reminder.objects.filter(id=due.id).update(snoozed_until=on(a, -1))
    back = editor.get(f"reminders/{due.id}").json_body
    assert (back["status"], back["snoozed_until"]) == ("open", None)
    assert [r["id"] for r in editor.get("reminders?status=open").json_body["results"]] == [str(due.id)]
    assert editor.get("reminders?status=snoozed").json_body["results"] == []
    assert generate(a) == {**ZERO, "unsnoozed": 1}
    due.refresh_from_db()
    assert (due.status, due.snoozed_until) == ("open", None)


def test_acknowledging_is_recorded_once(a, api, due):
    editor = api(a["users"]["editor"])
    body = editor.post(f"reminders/{due.id}/acknowledge").json_body
    assert body["status"] == "acknowledged" and body["acknowledged_by"]["id"] == a["users"]["editor"].id
    assert editor.post(f"reminders/{due.id}/acknowledge").status_code == 200
    assert AuditEvent.objects.filter(action="reminder.acknowledged").count() == 1
    refused = editor.post(f"reminders/{due.id}/acknowledge", {"note": "x"})
    assert refused.status_code == 400 and "note" in refused.json_body["errors"]


def test_reopening(a, api, due):
    editor = api(a["users"]["editor"])
    editor.post(f"reminders/{due.id}/resolve", {"kind": "risk_accepted", "note": "Renewing anyway"})
    reopened = editor.post(f"reminders/{due.id}/reopen").json_body
    assert (reopened["status"], reopened["notify_cycle"], reopened["resolution_kind"]) == ("open", 1, None)
    assert AuditEvent.objects.get(action="reminder.reopened").changes["from"] == "done"
    Reminder.objects.filter(id=due.id).update(status="obsolete", resolution_kind="source_changed")
    refused = editor.post(f"reminders/{due.id}/reopen")
    assert refused.status_code == 409 and "no longer applies" in refused.json_body["detail"]


def test_resolving_needs_a_reason_for_money_compliance_and_access(a, api, due):
    editor = api(a["users"]["editor"])
    refused = editor.post(f"reminders/{due.id}/resolve", {"kind": "done"})
    assert refused.status_code == 400 and list(refused.json_body["errors"]) == ["note"]
    assert editor.post(f"reminders/{due.id}/resolve", {"kind": "finished"}).json_body["errors"]["kind"]
    assert editor.post(f"reminders/{due.id}/resolve", {"kind": "done", "colour": "red"}).json_body["errors"]["colour"]
    done = editor.post(f"reminders/{due.id}/resolve", {"kind": "done", "note": "Cancelled with the vendor"})
    assert done.status_code == 200 and done.json_body["resolution_note"] == "Cancelled with the vendor"
    assert done.json_body["resolved_by"]["id"] == a["users"]["editor"].id
    for verb, body in (("snooze", {"until": iso(a, 3)}), ("acknowledge", {}), ("resolve", {"kind": "done"})):
        again = editor.post(f"reminders/{due.id}/{verb}", body)
        assert again.status_code == 409 and "Reopen it first" in again.json_body["detail"], verb
    assert generate(a) == ZERO  # a resolved reminder stays resolved


def test_a_custom_date_can_be_resolved_without_a_reason(a, api):
    date_field(a, applies_to=("application",))
    services.update_resource(
        a["org"], a["users"]["editor"], a["app"], {"custom_field_data": {"cert_expiry": iso(a, 9)}}
    )
    generate(a)
    reminder = one(a["org"], source_type="custom_date")
    resolved = api(a["users"]["editor"]).post(f"reminders/{reminder.id}/resolve", {"kind": "done"})
    assert resolved.status_code == 200
    accepted = api(a["users"]["editor"]).post(f"reminders/{reminder.id}/reopen")
    assert accepted.status_code == 200
    risk = api(a["users"]["editor"]).post(f"reminders/{reminder.id}/resolve", {"kind": "risk_accepted"})
    assert risk.status_code == 400 and "note" in risk.json_body["errors"]


# ---- the API ---------------------------------------------------------------------------------------------------------


def test_the_list_and_its_filters(a, api, sources):
    generate(a)
    client = api(a["users"]["viewer"])
    body = client.get("reminders").json_body
    rows = body["results"]
    assert [r["due_date"] for r in rows] == sorted(r["due_date"] for r in rows) and len(rows) == 9
    assert described(ReminderSerializer(many=True), rows) == []
    by_type = {r["source_type"] for r in client.get("reminders?source_type=custom_date,contract").json_body["results"]}
    assert by_type == {"custom_date", "contract"}
    window = client.get(f"reminders?due_after={iso(a, 20)}&due_before={iso(a, 40)}").json_body["results"]
    assert {r["title"] for r in window} == {"Acme", "Alone", "E5", "Driven"}
    mine = client.get(f"reminders?source_id={a['app'].id}").json_body["results"]
    assert {r["source_type"] for r in mine} == {"periodic_review", "record_review", "custom_date"}
    refused = client.get("reminders?status=gone&scope=everyone&due_after=soon&colour=red")
    assert refused.status_code == 400
    assert set(refused.json_body["errors"]) == {"status", "scope", "due_after", "colour"}
    row = next(r for r in rows if r["source_type"] == "renewal_cycle" and r["title"] == "Domain")
    assert row["path"] == f"/renewals/{sources['domain'].id}" and row["deadline_label_text"] == "Calculated"
    assert row["timing"] == {"days": 10, "overdue": False, "text": "in 10 days"}
    paths = {(r["source_type"], r["title"]): r["path"] for r in rows}
    assert paths["contract", "Alone"] == f"/contracts/{sources['alone'].id}"
    assert paths["custom_date", "E5"] == f"/license-pools/{sources['pool'].id}"
    assert paths["custom_date", "Acme"] == f"/vendors/{sources['vendor'].id}"
    assert paths["periodic_review", a["app"].name] == f"/applications/{a['app'].id}"


def test_scopes_route_like_needs_attention(a, api, sources):
    """A renewal goes to its IT follow-up, a record's dates to its IT steward, a contract to its renewal owner's user;
    a vendor's dates are unassigned and the IT coordinator catches them."""
    org = a["org"]
    dana = a["people"]["dana"]
    dana.user = a["users"]["viewer"]
    dana.save()
    contracts.update_contract(org, a["users"]["editor"], sources["alone"], {"renewal_owner": str(dana.id)})
    generate(a)

    def titles(user, scope):
        rows = api(user).get(f"reminders?scope={scope}&source_type=contract,custom_date,renewal_cycle").json_body
        return sorted((r["source_type"], r["title"]) for r in rows["results"])

    assert titles(a["users"]["viewer"], "mine") == [("contract", "Alone"), ("custom_date", "Alone")]
    assert ("renewal_cycle", "Domain") in titles(a["users"]["editor"], "mine")
    assert ("custom_date", "E5") in titles(a["users"]["editor"], "mine")  # the pool's application's steward
    # A renewal made from a contract has no IT follow-up of its own.
    assert titles(a["users"]["editor"], "unassigned") == [("custom_date", "Acme"), ("renewal_cycle", "Driven")]
    assert ("custom_date", "Acme") in titles(a["users"]["admin"], "mine")  # the coordinator catches it
    unassigned = api(a["users"]["admin"]).get("reminders?scope=unassigned").json_body["results"]
    assert unassigned and all(r["unassigned"] and r["steward"]["coordinator"] for r in unassigned)
    assigned = api(a["users"]["admin"]).get(f"reminders?scope=all&source_id={sources['domain'].cycles.get().id}")
    assert assigned.json_body["results"][0]["steward"]["id"] == a["users"]["editor"].id


def test_the_list_query_count_is_flat(a, api):
    client = api(a["users"]["viewer"])
    date_field(a, applies_to=("application", "vendor"))

    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert client.get("reminders").status_code == 200
        return len(ctx.captured_queries)

    for i in range(3):
        admin_services.create_named(Vendor, a["org"], a["users"]["admin"],
                                    {"name": f"V{i}", "custom_field_data": {"cert_expiry": iso(a, i + 1)}})  # fmt: skip
    generate(a)
    client.get("reminders")  # the first request also saves the session
    small = count()
    for i in range(3, 30):
        admin_services.create_named(Vendor, a["org"], a["users"]["admin"],
                                    {"name": f"V{i}", "custom_field_data": {"cert_expiry": iso(a, i + 1)}})  # fmt: skip
        contract(a, f"C{i}", end_date=iso(a, a["org"].renewal_lead_days + i), auto_renew="no",
                 renewal_owner=str(a["people"]["dana"].id))  # fmt: skip
    generate(a)
    assert count() == small


def test_another_organizations_reminders_dont_exist(a, b, api, due):
    theirs = contract(b, "Theirs", end_date=iso(b, b["org"].renewal_lead_days + 30), auto_renew="no")
    reminders.generate(b["org"], today(b))
    their_reminder = one(b["org"], source_type="contract")
    client = api(a["users"]["admin"])
    assert [r["id"] for r in client.get("reminders").json_body["results"]] == [str(due.id)]
    assert client.get(f"reminders/{their_reminder.id}").status_code == 404
    assert client.post(f"reminders/{their_reminder.id}/acknowledge").status_code == 404
    assert client.get(f"reminders?source_id={theirs.id}").json_body["results"] == []
    their_reminder.refresh_from_db()
    assert their_reminder.status == "open"
    queues, _ = attention_queues(a["org"], a["users"]["admin"], today(a))
    assert str(their_reminder.id) not in {r["id"] for r in queues["reminders"]}


def test_the_schema_documents_the_reminder_endpoints(a, api, monkeypatch):
    import json

    from tests import test_openapi

    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    found = {
        (method, path): op
        for path, operations in doc["paths"].items()
        if path.startswith("/api/v1/reminders")
        for method, op in operations.items()
        if method in ("get", "post", "patch", "put", "delete")
    }
    assert set(found) == {
        ("get", "/api/v1/reminders"),
        ("get", "/api/v1/reminders/{id}"),
        ("post", "/api/v1/reminders/{id}/snooze"),
        ("post", "/api/v1/reminders/{id}/acknowledge"),
        ("post", "/api/v1/reminders/{id}/reopen"),
        ("post", "/api/v1/reminders/{id}/resolve"),
    }
    no_body = {("post", "/api/v1/reminders/{id}/acknowledge"), ("post", "/api/v1/reminders/{id}/reopen")}
    monkeypatch.setattr(test_openapi, "NO_BODY", test_openapi.NO_BODY | no_body)
    assert test_openapi.body_problems(found) == []
    params = {p["name"] for p in found["get", "/api/v1/reminders"]["parameters"]}
    assert {"status", "source_type", "source_id", "due_after", "due_before", "scope", "cursor"} <= params
    assert "412" in found["post", "/api/v1/reminders/{id}/snooze"]["responses"]


# ---- capabilities and timezones -------------------------------------------------------------------------------------


def test_capabilities_and_the_compliance_profile_gate_their_sources(a, sources, monkeypatch):
    org = a["org"]
    generate(a)
    real = caps.capabilities
    monkeypatch.setattr(caps, "capabilities", lambda o: real(o) - {caps.CONTRACTS})
    generate(a)
    closed = {(r.source_type, r.title) for r in Reminder.objects.filter(organization=org, status="obsolete")}
    # Contracts off: the contract's deadline and the contract and pool custom dates go; the driven renewal stays.
    assert closed == {("contract", "Alone"), ("custom_date", "Alone"), ("custom_date", "E5")}
    assert live(org, source_type="renewal_cycle").count() == 2
    monkeypatch.setattr(caps, "capabilities", real)
    org.settings = {**org.settings, compliance.SETTING: False}
    org.save(update_fields=["settings"])
    compliance.forget(org.pk)
    generate(a)
    assert one(org, source_type="periodic_review").status == "obsolete"
    assert live(org, source_type="contract").count() == 1  # back with the capability, in a new cycle
    monkeypatch.setattr(caps, "capabilities", lambda o: real(o) - {caps.RENEWALS, caps.REVIEWS, caps.CUSTOM_FIELDS})
    generate(a)
    assert {r.source_type for r in live(org)} == {"contract"}


def test_each_organization_is_judged_on_its_own_date(a, b):
    """At 2027-03-31 23:30 UTC it is already 1 April in Auckland and still 31 March in Los Angeles: a snooze through
    31 March has run out in Auckland only."""
    rows = []
    for f, zone in ((a, "Pacific/Auckland"), (b, "America/Los_Angeles")):
        f["org"].timezone = zone
        f["org"].save()
        date_field(f, applies_to=("application",))
        services.update_resource(
            f["org"], f["users"]["editor"], f["app"], {"custom_field_data": {"cert_expiry": "2027-05-01"}}
        )
        reminders.generate(f["org"], date(2027, 3, 1))
        row = one(f["org"], source_type="custom_date")
        Reminder.objects.filter(id=row.id).update(status="snoozed", snoozed_until=date(2027, 3, 31))
        rows.append(row)
    now = datetime(2027, 3, 31, 23, 30, tzinfo=UTC)
    assert reminders.scheduled_generate(now) == {**ZERO, "unsnoozed": 1, "failed": 0}
    assert [Reminder.objects.get(id=r.id).status for r in rows] == ["open", "snoozed"]
    assert reminders.scheduled_generate(now + timedelta(hours=8))["unsnoozed"] == 1  # Los Angeles reaches 1 April
    assert Reminder.objects.get(id=rows[1].id).status == "open"


def test_the_term_job_regenerates_renewals_and_contracts_once_per_organization(
    a, b, monkeypatch, django_capture_on_commit_callbacks
):
    """Each contract the term job moves commits on its own; the reminders follow once per organization, not once per
    contract."""
    from money import terms

    for f in (a, b):
        for i in range(3):
            contracts.create_contract(
                f["org"], f["users"]["editor"],
                {"name": f"Signed {i}", "status": "signed", "start_date": "2027-04-01", "end_date": "2028-03-31"},
            )  # fmt: skip
        contract(f, "Ended", start_date="2026-01-01", end_date="2026-12-31", auto_renew="no")
    calls: list[tuple[Any, tuple[str, ...]]] = []
    real = reminders.sync

    def counted(org, day, types=reminders.ALL_TYPES, ids=None):
        calls.append((org.pk, tuple(types)))
        return real(org, day, types, ids)

    monkeypatch.setattr(reminders, "sync", counted)
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        moved = terms.scheduled_advance(datetime(2027, 4, 1, 12, tzinfo=UTC))
    assert moved["started"] == 6 and moved["expired"] == 2
    money = [org for org, types in calls if set(types) == set(reminders.MONEY)]
    assert sorted(money) == sorted([a["org"].pk, b["org"].pk])  # one each
    # The reminder flushes, one per organization (webhooks register no commit hooks: notify 0010 outbox).
    assert len(callbacks) == 2
    assert one(a["org"], source_type="contract", title="Signed 0").status == "open"


def test_a_failing_organization_doesnt_stop_the_others(a, b, monkeypatch, caplog):
    contract(b, "Theirs", end_date=iso(b, b["org"].renewal_lead_days + 30), auto_renew="no")
    real = reminders.generate

    def flaky(org, day):
        if org.pk == a["org"].pk:
            raise RuntimeError("secret detail")
        return real(org, day)

    monkeypatch.setattr(reminders, "generate", flaky)
    counts = reminders.scheduled_generate(datetime.now(UTC))
    assert counts["failed"] == 1 and counts["created"] == 1
    assert f"organization={a['org'].id}" in caplog.text


# ---- Credential expiry (feeds it) -------------------------------------------------------------------------------------


def test_a_registered_credential_source_produces_and_closes_its_reminders(a, monkeypatch):
    org = a["org"]
    credential = uuid.uuid4()
    produced = [
        Fact(
            "credential_expiry",
            credential,
            on(a, 14),
            title="Payroll client secret",
            label="Client secret expires",
            source_field="key-1",
            resource_id=a["app"].id,
        )  # fmt: skip
    ]
    monkeypatch.setattr(reminders, "CREDENTIAL_SOURCES", [])
    reminders.register_credential_source(lambda o, day, ids: [f for f in produced if o.pk == org.pk])
    generate(a)
    reminder = one(org, source_type="credential_expiry")
    assert (reminder.source_id, reminder.source_field, reminder.due_date) == (credential, "key-1", on(a, 14))
    body = next(r for r in reminders_api_rows(a) if r["source_type"] == "credential_expiry")
    assert body["steward"]["id"] == a["users"]["editor"].id  # routed by the record it belongs to
    assert body["path"] == f"/applications/{a['app'].id}"
    produced.clear()  # removed from the tenant
    generate(a)
    assert one(org, source_type="credential_expiry").status == "obsolete"
    reminders.register_credential_source(lambda o, day, ids: [Fact("contract", credential, on(a, 1), "x", "y")])
    with pytest.raises(ValueError):
        generate(a)


def reminders_api_rows(a):
    from tests.conftest import Api

    return Api(a["users"]["viewer"]).get("reminders").json_body["results"]


# ---- the schedule ----------------------------------------------------------------------------------------------------


def test_the_schedule_is_registered_and_listed(a, api):
    assert jobs.SCHEDULES["reminders.generate"].func is reminders.scheduled_generate
    names = list(jobs.SCHEDULES)
    assert names.index("contracts.advance_terms") < names.index("reminders.generate")  # statuses first in a tick
    jobs.sync_schedules()
    body = api(a["users"]["admin"]).get("system").json_body
    row = next(s for s in body["schedules"] if s["name"] == "reminders.generate")
    assert row["cron"] == "20 * * * *" and row["label"].startswith("Generate reminders")


# ---- Needs attention ---------------------------------------------------------------------------------------------------


def test_needs_attention_lists_reminders_once_per_deadline(a, api, sources):
    org = a["org"]
    RecordReview.objects.filter(resource=a["app"]).update(next_review_date=on(a, -2))  # overdue: a Reviews row
    generate(a)
    body = api(a["users"]["editor"]).get("needs-attention?scope=all").json_body
    queue = {(r["source_type"], r["name"]): r for r in body["reminders"]}
    # Renewal cycles and record reviews are folded into their own rows, never listed twice.
    assert {t for t, _ in queue} <= {"contract", "periodic_review", "custom_date", "credential_expiry"}
    assert set(queue) == {
        ("contract", "Alone"),
        ("periodic_review", a["app"].name),
        ("custom_date", a["app"].name),
        ("custom_date", "Acme"),
        ("custom_date", "E5"),
        ("custom_date", "Alone"),
    }
    row = queue["contract", "Alone"]
    assert row["due"] == iso(a, 30) and row["why"] == f"Renewal action due {iso(a, 30)}, in 30 days."
    assert row["status"] == "open" and row["snooze"] is None and row["path"] == f"/contracts/{sources['alone'].id}"
    assert queue["custom_date", "Acme"]["steward"]["coordinator"] is True
    actions = {r["name"]: r for r in body["renewal_actions"]}
    domain = one(org, source_type="renewal_cycle", title="Domain")
    assert actions["Domain"]["reminder"] == {"id": str(domain.id), "status": "open", "snoozed_until": None,
                                             "due": iso(a, 10)}  # fmt: skip
    record_review = one(org, source_type="record_review", status="open")
    reviews = {r["id"]: r for r in body["reviews"]}
    assert reviews[str(a["app"].id)]["reminder"]["id"] == str(record_review.id)
    # Counted renders it: the badge and the page add up the same rows, reminders included.
    assert "reminders" not in UNCOUNTED and body["reminders"]
    counted = sum(len(body[q]) for q in body if isinstance(body[q], list))
    assert body["counts"]["all"] == counted
    mine = api(a["users"]["editor"]).get("needs-attention?scope=mine").json_body
    assert mine["counts"]["mine"] == sum(len(mine[q]) for q in mine if isinstance(mine[q], list))


def test_a_snoozed_reminder_stays_in_needs_attention_with_its_status(a, api, due):
    editor = api(a["users"]["editor"])
    editor.post(f"reminders/{due.id}/snooze", {"until": iso(a, 3)})
    row = next(r for r in editor.get("needs-attention?scope=all").json_body["reminders"] if r["id"] == str(due.id))
    assert (row["status"], row["snoozed_until"]) == ("snoozed", iso(a, 3))
    editor.post(f"reminders/{due.id}/resolve", {"kind": "done", "note": "Cancelled"})
    assert editor.get("needs-attention?scope=all").json_body["reminders"] == []
    refused = editor.post("needs-attention/snoozes", {"subject_type": "reminders", "subject_id": str(due.id),
                                                      "kind": "snooze", "until": iso(a, 3)})  # fmt: skip
    assert refused.status_code == 400 and "subject_type" in refused.json_body["errors"]


def test_reminders_beyond_the_horizon_wait(a, api):
    a["org"].reminder_thresholds = [30, 7]
    a["org"].save()
    contract(a, "Soon", end_date=iso(a, a["org"].renewal_lead_days + 20), auto_renew="no")
    contract(a, "Later", end_date=iso(a, a["org"].renewal_lead_days + 45), auto_renew="no")
    generate(a)
    body = api(a["users"]["editor"]).get("needs-attention?scope=all").json_body
    assert [r["name"] for r in body["reminders"]] == ["Soon"]
