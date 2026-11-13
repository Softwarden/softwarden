"""Notification rules (notify/rules.py, notify/rule_services.py, notify/rule_viewsets.py): built-in rules and their seeding, CRUD and validation, leads and
escalation steps with the catch-up rule, dedupe across reruns and moved dates, snooze and acknowledge, audiences, the
fallback and preferences, filters, the preview (no writes), test sends, tenancy, roles, delete guards and query counts.
The acceptance check end to end: a contract whose cancellation deadline is 30 days away sends exactly one email and one Teams
card at the 30-day mark, a rerun sends nothing, a snooze suppresses until its date, and the log shows both. Transports
are mocked (Django's in-memory mail, a fake Workflows POST); nothing contacts Microsoft or Teams."""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.core import mail
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from catalog import services
from catalog.models import Person, Resource, Team, TeamMember, Vendor
from core import jobs
from core.models import AuditEvent, Membership
from money import contracts
from notify import channel_services, reminders, rule_services, rules
from notify import services as notify_services
from notify.models import (
    Delivery,
    NotificationPreference,
    NotificationRule,
    Reminder,
    TeamsDestination,
    WebhookDestination,
)
from tests.conftest import make_org
from tests.test_channels import SIGNING_SECRET, TEAMS_URL, WEBHOOK_URL, posts  # noqa: F401  (posts is a fixture)

pytestmark = pytest.mark.django_db

BUILTIN_KEYS = {
    "renewals_contracts",
    "record_reviews",
    "periodic_reviews",
    "custom_dates",
    "credentials",
    "vendor_risk",  # (notify 0011)
    "end_of_life",  # (notify 0012)
    "daily_digest",
}


@pytest.fixture(autouse=True)
def email_on(settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.SOFTWARDEN_BASE_URL = "https://softwarden.example.test"


def today(f) -> date:
    return services.org_today(f["org"])


def at(f, days: int):
    """A minute after the organization's digest time (notices go from then on) on its local date `days` from
    today."""
    zone = ZoneInfo(f["org"].timezone)
    moment = datetime.combine(today(f) + timedelta(days=days), f["org"].digest_time, tzinfo=zone)
    return moment + timedelta(minutes=1)


def run(f, days: int = 0) -> int:
    return rules.run_for_org(f["org"], at(f, days))


def drain() -> None:
    """Run every due job (the worker's loop): each queued delivery gets its attempt."""
    while (job := jobs.claim()) is not None:
        jobs.run(job)


def builtin(f, key) -> NotificationRule:
    return NotificationRule.objects.get(organization=f["org"], builtin_key=key)


def only(f, rule=None) -> None:
    """Turn off every rule but `rule` (the built-ins would also cover the reminders a test makes)."""
    others = NotificationRule.objects.filter(organization=f["org"])
    if rule is not None:
        others = others.exclude(pk=rule.pk)
    others.update(enabled=False)


def rule(f, **data) -> NotificationRule:
    body = {"name": f"Rule {uuid.uuid4().hex[:6]}", "source_types": ["credential_expiry"], **data}
    made = rule_services.create_rule(f["org"], f["users"]["admin"], body)
    only(f, made)
    return made


def reminder(f, days: int, resource=None, **extra) -> Reminder:
    """A credential reminder on a record (routed to its IT steward). Credential reminders are generated only by the
    hourly run (nothing registers a source yet), so nothing in a test closes it."""
    return Reminder.objects.create(
        organization=f["org"],
        source_type="credential_expiry",
        source_id=uuid.uuid4(),
        source_field="key-1",
        due_date=today(f) + timedelta(days=days),
        title="Payroll API secret",
        label="Client secret expires",
        resource=resource or f["app"],
        **extra,
    )


def emails(f=None):
    return Delivery.objects.filter(channel="email", kind__in=["reminder", "escalation"]).order_by("created_at", "id")


def leads(qs) -> list:
    return [(d.lead_time_days, d.overdue_step, d.recipient) for d in qs]


def teams(f, name="Ops"):
    return channel_services.create_destination(
        f["org"], f["users"]["admin"], TeamsDestination, {"name": name, "url": TEAMS_URL}
    )


# ---- built-ins ---------------------------------------------------------------------------------------------------


def test_every_organization_starts_with_the_built_in_rules(a):
    found = {r.builtin_key: r for r in NotificationRule.objects.filter(organization=a["org"])}
    assert set(found) == BUILTIN_KEYS
    assert found["renewals_contracts"].lead_times_days == [90, 60, 30, 7]
    assert set(found["renewals_contracts"].source_types) == {"renewal_cycle", "contract"}
    assert found["record_reviews"].lead_times_days == [0]
    assert found["daily_digest"].kind == "digest" and found["daily_digest"].enabled
    assert all(r.enabled and r.notify_routed and r.send_email and not r.escalation_days for r in found.values())
    assert rule_services.seed(a["org"]) == 0  # seeding again adds nothing
    other = make_org("orgc")
    assert NotificationRule.objects.filter(organization=other["org"]).count() == len(BUILTIN_KEYS)
    assert not AuditEvent.objects.filter(action__startswith="notification_rule.").exists()  # part of creating it


def test_built_ins_are_editable_but_never_deleted(a, api):
    admin = api(a["users"]["admin"])
    renewals = builtin(a, "renewals_contracts")
    changed = admin.patch(f"notification-rules/{renewals.id}", {"lead_times_days": [45, 14, 14, 3], "enabled": False})
    assert changed.status_code == 200
    assert changed.json_body["lead_times_days"] == [45, 14, 3] and changed.json_body["enabled"] is False
    assert changed.json_body["builtin"] is True
    refused = admin.delete(f"notification-rules/{renewals.id}")
    assert refused.status_code == 409 and "Turn it off instead" in refused.json_body["detail"]
    digest_rule = builtin(a, "daily_digest")
    assert admin.delete(f"notification-rules/{digest_rule.id}").status_code == 409
    assert NotificationRule.objects.filter(organization=a["org"]).count() == len(BUILTIN_KEYS)
    event = AuditEvent.objects.get(action="notification_rule.updated")
    assert set(event.changes["fields"]) == {"lead_times_days", "enabled"}
    assert event.prechange is not None and event.postchange is not None
    assert event.prechange["lead_times_days"] == [90, 60, 30, 7] and event.postchange["lead_times_days"] == [45, 14, 3]


# ---- CRUD and validation -----------------------------------------------------------------------------------------


def test_rule_crud_is_audited_and_admin_only(a, api):
    admin = api(a["users"]["admin"])
    ops = teams(a)
    a["people"]["priya"].email = "priya@orga.test"
    a["people"]["priya"].save()
    body = {
        "name": "Big contracts",
        "source_types": ["contract", "renewal_cycle"],
        "lead_times_days": [120, 30],
        "escalation_days": [7, 1],
        "teams_destinations": [str(ops.id)],
        "people": [str(a["people"]["priya"].id)],
        "team": str(a["team"].id),
        "roles": ["admin"],
        "escalation_roles": ["admin"],
        "fallback_person": str(a["people"]["dana"].id),
        "criticalities": ["critical", "high"],
        "min_annual_value": "50000",
    }
    created = admin.post("notification-rules", body)
    assert created.status_code == 201, created.json_body
    rid = created.json_body["id"]
    shown = admin.get(f"notification-rules/{rid}").json_body
    assert shown["lead_times_days"] == [120, 30] and shown["escalation_days"] == [1, 7]
    assert shown["teams_destinations"] == [{"id": str(ops.id), "name": "Ops", "enabled": True}]
    assert shown["people"] == [{"id": str(a["people"]["priya"].id), "name": "Priya Raman"}]
    assert shown["criticalities"] == ["high", "critical"] and shown["min_annual_value"] == "50000.00"
    assert shown["builtin"] is False and shown["digest_time"] is None
    assert len(admin.get("notification-rules").json_body) == len(BUILTIN_KEYS) + 1
    event = AuditEvent.objects.get(action="notification_rule.created")
    assert event.postchange is not None
    assert event.postchange["people"] == [{"id": str(a["people"]["priya"].id), "display": None}]
    assert event.postchange["teams_destinations"] == [{"id": str(ops.id), "display": "Ops"}]
    assert TEAMS_URL not in str(event.postchange)
    changed = admin.patch(f"notification-rules/{rid}", {"people": [], "teams_destinations": []})
    assert changed.status_code == 200 and changed.json_body["people"] == []
    assert set(AuditEvent.objects.get(action="notification_rule.updated").changes["fields"]) == {
        "people",
        "teams_destinations",
    }
    assert admin.delete(f"notification-rules/{rid}").status_code == 204
    assert AuditEvent.objects.filter(action="notification_rule.deleted").count() == 1
    for role in ("editor", "viewer"):
        client = api(a["users"][role])
        assert client.get("notification-rules").status_code == 403
        assert client.post("notification-rules", body).status_code == 403


@pytest.mark.parametrize(
    "body,field",
    [
        ({"source_types": []}, "source_types"),
        ({"source_types": ["nope"]}, "source_types"),
        ({"lead_times_days": [-1]}, "lead_times_days"),
        ({"lead_times_days": [400]}, "lead_times_days"),
        ({"lead_times_days": list(range(11))}, "lead_times_days"),
        ({"lead_times_days": [], "escalation_days": []}, "lead_times_days"),
        ({"escalation_days": [0]}, "escalation_days"),
        ({"roles": ["owner"]}, "roles"),
        ({"criticalities": ["huge"]}, "criticalities"),
        ({"record_kinds": ["integration"]}, "record_kinds"),
        ({"min_annual_value": "-5"}, "min_annual_value"),
        ({"send_email": False}, "send_email"),
        ({"notify_routed": False}, "notify_routed"),
        ({"team": "not-an-id"}, "team"),
        ({"digest_time": "08:00"}, "digest_time"),
        ({"surprise": 1}, "surprise"),
        ({"name": ""}, "name"),
        ({"name": "Renewals and contracts"}, "name"),  # a built-in's name, case-insensitively
    ],
)
def test_rule_validation(a, api, body, field):
    payload = {"name": "Mine", "source_types": ["contract"], "lead_times_days": [30], **body}
    response = api(a["users"]["admin"]).post("notification-rules", payload)
    assert response.status_code == 400 and field in response.json_body["errors"], response.json_body


def test_a_rule_refuses_another_organizations_rows(a, b, api):
    admin = api(a["users"]["admin"])
    theirs = teams(b, "Theirs")
    their_vendor = Vendor.objects.create(organization=b["org"], name="Theirs")
    for field, value in (
        ("team", str(b["team"].id)),
        ("fallback_person", str(b["people"]["dana"].id)),
        ("vendor", str(their_vendor.id)),
        ("people", [str(b["people"]["dana"].id)]),
        ("teams_destinations", [str(theirs.id)]),
    ):
        body = {"name": "X", "source_types": ["contract"], "lead_times_days": [30], field: value}
        response = admin.post("notification-rules", body)
        assert response.status_code == 400 and field in response.json_body["errors"], field
    assert not NotificationRule.objects.filter(organization=a["org"], name="X").exists()


def test_an_anonymized_or_inactive_person_cant_be_chosen(a, api):
    response = api(a["users"]["admin"]).post(
        "notification-rules",
        {"name": "X", "source_types": ["contract"], "lead_times_days": [30], "people": [str(a["people"]["gone"].id)]},
    )
    assert response.status_code == 400 and response.json_body["errors"]["people"] == ["This person is inactive."]


def test_teams_vendors_and_destinations_a_rule_names_cant_be_deleted(a, api):
    admin = api(a["users"]["admin"])
    vendor = Vendor.objects.create(organization=a["org"], name="Contoso")
    ops = teams(a)
    made = rule(
        a,
        lead_times_days=[30],
        fallback_team=str(a["team"].id),
        vendor=str(vendor.id),
        teams_destinations=[str(ops.id)],
    )
    for path in (f"teams/{a['team'].id}", f"vendors/{vendor.id}", f"notification-settings/teams-destinations/{ops.id}"):
        refused = admin.delete(path)
        assert refused.status_code == 409, path
        assert made.name in refused.json_body["detail"]
    assert Team.objects.filter(id=a["team"].id).exists() and TeamsDestination.objects.filter(id=ops.id).exists()
    admin.delete(f"notification-rules/{made.id}")
    assert admin.delete(f"notification-settings/teams-destinations/{ops.id}").status_code == 204


# ---- the acceptance check -----------------------------------------------------------------------------------------------


def _done_when_setup(a):
    editor = a["users"]["editor"]
    owner = a["people"]["priya"]
    owner.user = editor
    owner.save()
    ops = teams(a)
    renewals = builtin(a, "renewals_contracts")
    rule_services.update_rule(a["org"], a["users"]["admin"], renewals, {"teams_destinations": [str(ops.id)]})
    end = today(a) + timedelta(days=a["org"].renewal_lead_days + 30)
    made = contracts.create_contract(
        a["org"],
        editor,
        {"name": "Contoso CRM", "end_date": end.isoformat(), "auto_renew": "yes", "renewal_owner": str(owner.id)},
    )
    made.refresh_from_db()
    assert made.cancellation_deadline == today(a) + timedelta(days=30)
    reminders.generate(a["org"], today(a))
    return made, ops


def test_done_when_one_email_and_one_teams_card_at_30_days_rerun_nothing_log_shows_both(a, api, posts):  # noqa: F811
    contract, ops = _done_when_setup(a)
    assert run(a) == 2
    drain()
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [a["users"]["editor"].email]
    assert (
        mail.outbox[0].subject
        == f"Cancellation deadline: Contoso CRM (due in 30 days, {contract.cancellation_deadline})"
    )
    assert f"https://softwarden.example.test/contracts/{contract.id}" in mail.outbox[0].body
    assert len(posts.calls) == 1
    card = posts.calls[0][1].decode()
    assert "AdaptiveCard" in card and "Contoso CRM" in card
    # Re-running the job (the same hour, the next hour, the next day inside the 30-day lead) sends nothing.
    for days in (0, 0, 1, 5):
        assert run(a, days) == 0
    drain()
    assert len(mail.outbox) == 1 and len(posts.calls) == 1
    # The log shows both deliveries.
    log = api(a["users"]["admin"]).get("notification-log").json_body["results"]
    assert {(r["channel"], r["status"], r["kind"], r["rule_name"], r["lead_time_days"]) for r in log} == {
        ("email", "sent", "reminder", "Renewals and contracts", 30),
        ("teams", "sent", "reminder", "Renewals and contracts", 30),
    }
    assert all(r["reminder"]["title"] == "Contoso CRM" for r in log)
    assert {r["destination"]["name"] for r in log if r["channel"] == "teams"} == {"Ops"}
    assert all(r["rule"] == str(builtin(a, "renewals_contracts").id) for r in log)


def test_done_when_snoozing_suppresses_until_the_date(a, posts):  # noqa: F811
    _done_when_setup(a)
    found = Reminder.objects.get(organization=a["org"], source_type="contract")
    until = today(a) + timedelta(days=3)
    notify_services.snooze_reminder(a["org"], a["users"]["editor"], found, {"until": until.isoformat()})
    for days in (0, 1, 2, 3):  # quiet through the date
        assert run(a, days) == 0
    assert run(a, 4) == 2  # the day after: the 30-day lead it missed, once (26 days left)
    assert run(a, 5) == 0
    drain()
    assert len(mail.outbox) == 1 and len(posts.calls) == 1
    assert set(Delivery.objects.values_list("lead_time_days", flat=True)) == {30}


# ---- when: leads, escalation, catch-up -----------------------------------------------------------------------------


def test_each_lead_sends_once_and_a_missed_lead_catches_up_to_the_nearest_only(a):
    rule(a, lead_times_days=[90, 60, 30, 7])
    reminder(a, 25)  # first seen 25 days out: 90 and 60 were missed
    assert run(a) == 1
    assert leads(emails()) == [(30, None, a["users"]["editor"].email)]
    for days in range(1, 18):  # 24 .. 8 days left: still the 30-day lead
        assert run(a, days) == 0
    # The worker is down from 8 days left until 3 days left: the 7-day lead goes out once, late.
    assert run(a, 22) == 1
    assert run(a, 23) == 0 and run(a, 25) == 0  # due today: nothing more (no lead 0, no escalation)
    assert run(a, 26) == 0
    assert [d.lead_time_days for d in emails()] == [30, 7]
    assert all(d.kind == "reminder" for d in emails())


def test_lead_zero_is_the_due_date(a):
    rule(a, lead_times_days=[0])
    reminder(a, 2)
    assert run(a) == 0 and run(a, 1) == 0
    assert run(a, 2) == 1 and run(a, 2) == 0
    assert "due today" in emails().get().subject


def test_escalation_steps_after_the_due_date_with_catch_up(a):
    a["people"]["priya"].email = "priya@orga.test"
    a["people"]["priya"].save()
    rule(a, lead_times_days=[7], escalation_days=[1, 7, 30], escalation_people=[str(a["people"]["priya"].id)])
    reminder(a, -3)  # 3 days overdue: step 1 was missed and goes out once now
    assert run(a) == 2
    assert sorted(leads(emails())) == [(None, 1, a["users"]["editor"].email), (None, 1, "priya@orga.test")]
    assert all(d.kind == "escalation" for d in emails())
    assert "3 days overdue" in emails().first().subject
    assert run(a, 1) == 0 and run(a, 3) == 0
    assert run(a, 4) == 2  # 7 days overdue: step 7
    assert run(a, 40) == 2  # 43 days overdue: step 30, once
    assert run(a, 80) == 0  # no step after the last


def test_acknowledged_stops_leads_but_not_escalation_unless_the_rule_says_so(a):
    made = rule(a, lead_times_days=[30, 7], escalation_days=[1])
    found = reminder(a, 30)
    notify_services.acknowledge_reminder(a["org"], a["users"]["editor"], found)
    assert run(a) == 0 and run(a, 23) == 0  # no advance notices
    assert run(a, 31) == 1  # escalation still goes out
    assert emails().get().overdue_step == 1 and "acknowledged" in emails().get().content["text"]
    Delivery.objects.all().delete()
    rule_services.update_rule(a["org"], a["users"]["admin"], made, {"escalate_when_acknowledged": False})
    assert run(a, 31) == 0


def test_snoozed_reminders_are_quiet_through_the_date_then_catch_up(a):
    rule(a, lead_times_days=[30], escalation_days=[1])
    found = reminder(a, 1)
    notify_services.snooze_reminder(
        a["org"], a["users"]["editor"], found, {"until": (today(a) + timedelta(days=5)).isoformat()}
    )
    for days in range(0, 6):  # the lead and the first escalation day fall inside the snooze
        assert run(a, days) == 0
    assert run(a, 6) == 1  # the day after: 5 days overdue, step 1 catches up once
    assert emails().get().kind == "escalation"


def test_closed_reminders_send_nothing(a):
    rule(a, lead_times_days=[30])
    reminder(a, 10, status="done")
    reminder(a, 10, status="obsolete")
    assert run(a) == 0


# ---- once: dedupe -------------------------------------------------------------------------------------------------


def test_a_date_moved_later_sends_the_leads_not_yet_sent_and_never_repeats_one(a):
    rule(a, lead_times_days=[60, 30, 7])
    found = reminder(a, 30)
    assert run(a) == 1
    # The source date moves later (the generator moves an open reminder: same row, new due date).
    Reminder.objects.filter(id=found.id).update(due_date=today(a) + timedelta(days=50))
    assert run(a) == 1  # the 60-day lead, not sent yet in this cycle (50 days left)
    assert run(a) == 0
    assert run(a, 20) == 0  # 30 days before the new date: the 30-day lead went out already in this cycle
    assert run(a, 43) == 1  # 7 days before: not sent yet
    keys = list(Delivery.objects.order_by("created_at").values_list("dedupe_key", "lead_time_days"))
    assert [lead for _k, lead in keys] == [30, 60, 7]
    assert len({k for k, _ in keys}) == 3 and all(":email:u" in k for k, _ in keys)
    assert all("@" not in k and "://" not in k for k, _ in keys)
    # The notices for the moved date carry it; the first one, its old date.
    assert keys[0][0].endswith(str(found.due_date)) and keys[1][0].endswith(str(found.due_date + timedelta(days=20)))


@pytest.mark.parametrize("shift", [-1, -10, 1])
def test_a_date_moved_a_little_doesnt_repeat_the_lead_already_sent(a, shift):
    rule(a, lead_times_days=[60, 30, 7])
    found = reminder(a, 30)
    assert run(a) == 1
    Reminder.objects.filter(id=found.id).update(due_date=found.due_date + timedelta(days=shift))
    later = run(a)
    for days in (1, 5):
        assert run(a, days) == 0
    sent = [d.lead_time_days for d in emails()]
    assert len(sent) == len(set(sent))  # no lead twice in a cycle
    if shift < 0:
        assert later == 0 and sent == [30]  # still the 30-day lead: sent already
    else:
        assert later == 1 and sent == [30, 60]  # 31 days left: the 60-day lead, not sent before, goes out


def test_a_changed_default_lead_time_moves_many_deadlines_and_repeats_no_notice(a, api):
    """The flood case: renewal_lead_days changes, every default-notice contract deadline moves in place, and the next
    run would otherwise re-notify each of them inside a lead window."""
    lead = a["org"].renewal_lead_days
    for n in range(4):
        end = today(a) + timedelta(days=lead + 25 + n)
        contracts.create_contract(
            a["org"], a["users"]["editor"], {"name": f"C{n}", "end_date": end.isoformat(), "auto_renew": "yes"}
        )
    reminders.generate(a["org"], today(a))
    assert run(a) == 4  # each in the built-in 30-day lead, to the coordinator
    before = {r.id: r.due_date for r in Reminder.objects.filter(organization=a["org"], source_type="contract")}
    assert api(a["users"]["admin"]).patch("organization", {"renewal_lead_days": lead + 5}).status_code == 200
    reminders.generate(a["org"], today(a))
    after = {r.id: r.due_date for r in Reminder.objects.filter(organization=a["org"], source_type="contract")}
    assert set(after) == set(before) and all(after[i] == before[i] - timedelta(days=5) for i in after)
    assert run(a) == 0 and run(a, 1) == 0
    assert sorted(d.lead_time_days for d in emails()) == [30, 30, 30, 30]


def test_a_reopened_reminder_notifies_again_in_its_new_cycle(a):
    rule(a, lead_times_days=[30])
    found = reminder(a, 20)
    assert run(a) == 1
    Reminder.objects.filter(id=found.id).update(notify_cycle=1)
    assert run(a) == 1 and run(a) == 0


def test_two_rules_covering_one_reminder_each_send(a):
    first = rule(a, lead_times_days=[30])
    second = rule_services.create_rule(
        a["org"],
        a["users"]["admin"],
        {"name": "Second", "source_types": ["credential_expiry"], "lead_times_days": [30]},
    )
    reminder(a, 20)
    assert run(a) == 2 and run(a) == 0
    assert set(Delivery.objects.values_list("rule_id", flat=True)) == {first.id, second.id}


def test_the_schedule_runs_every_organization_on_its_local_date(a, b):
    assert jobs.SCHEDULES["notify.rules"].cron == "25 * * * *"
    for f in (a, b):
        f["org"].digest_time = time(0, 0)
        f["org"].save()
    rule(a, lead_times_days=[30])
    rule(b, lead_times_days=[30])
    reminder(a, 10)
    reminder(b, 10, resource=b["app"])
    assert rules.scheduled_run(timezone.now()) == {"rule_deliveries": 2, "rule_failed": 0}
    assert rules.scheduled_run(timezone.now()) == {"rule_deliveries": 0, "rule_failed": 0}
    assert Delivery.objects.filter(organization=b["org"], recipient=b["users"]["editor"].email).count() == 1


# ---- to whom ------------------------------------------------------------------------------------------------------


def _unrouted(f, name="Orphan"):
    return Resource.objects.create(organization=f["org"], name=name, slug=name.lower(), lifecycle="active")


def test_routing_finds_nobody_the_fallback_then_the_coordinator(a):
    a["people"]["priya"].email = "priya@orga.test"
    a["people"]["priya"].save()
    orphan = _unrouted(a)
    made = rule(a, lead_times_days=[30], fallback_person=str(a["people"]["priya"].id))
    found = reminder(a, 20, resource=orphan)
    assert run(a) == 1 and emails().get().recipient == "priya@orga.test"
    event = rules.preview(a["org"], made, today(a) + timedelta(days=1), 1)
    assert event == []  # sent already
    # No fallback: the IT coordinator.
    rule_services.update_rule(a["org"], a["users"]["admin"], made, {"fallback_person": None})
    Reminder.objects.filter(id=found.id).update(notify_cycle=1)
    assert run(a) == 1 and emails().last().recipient == a["users"]["admin"].email
    # No coordinator either: nobody is emailed (a Teams destination still would be).
    a["org"].coordinator = None
    a["org"].save()
    Reminder.objects.filter(id=found.id).update(notify_cycle=2)
    assert run(a) == 0


def test_a_fallback_team_reaches_its_members_people_by_their_account(a):
    orphan = _unrouted(a)
    a["people"]["priya"].email = "priya@orga.test"
    a["people"]["priya"].save()
    a["people"]["dana"].user = a["users"]["viewer"]  # reached at her account's address, keyed by the user
    a["people"]["dana"].save()
    for person in (a["people"]["priya"], a["people"]["dana"], a["people"]["gone"]):
        TeamMember.objects.create(organization=a["org"], team=a["team"], person=person)
    rule(a, lead_times_days=[30], fallback_team=str(a["team"].id))
    reminder(a, 20, resource=orphan)
    assert run(a) == 2
    assert {d.recipient for d in emails()} == {"priya@orga.test", a["users"]["viewer"].email}
    keys = {d.dedupe_key.split(":")[-2] for d in emails()}  # the member; the due date comes last
    assert keys == {f"p{a['people']['priya'].id.hex}", f"u{a['users']['viewer'].id}"}


def test_people_teams_and_roles_are_added_and_each_recipient_once(a):
    a["people"]["dana"].user = a["users"]["editor"]  # the routed steward too: once
    a["people"]["dana"].save()
    rule(a, lead_times_days=[30], people=[str(a["people"]["dana"].id)], roles=["admin"])
    second_admin = make_org("orgz")["users"]["admin"]
    Membership.objects.create(user=second_admin, organization=a["org"], role="admin")
    reminder(a, 20)
    assert run(a) == 3
    assert {d.recipient for d in emails()} == {
        a["users"]["editor"].email,
        a["users"]["admin"].email,
        second_admin.email,
    }


def test_a_member_who_turned_reminder_emails_off_isnt_emailed_or_replaced(a, api):
    editor = api(a["users"]["editor"])
    changed = editor.patch("auth/me/preferences", {"reminders_enabled": False})
    assert changed.status_code == 200 and changed.json_body["reminders_enabled"] is False
    assert changed.json_body["digest_enabled"] is True
    made = rule(a, lead_times_days=[30])
    reminder(a, 20)
    assert run(a) == 0  # the routed steward opted out: no fallback stands in
    events = rules.preview(a["org"], made, today(a), 1)
    assert events[0]["recipients"] == [] and events[0]["opted_out"] == ["editor@orga.test"]
    assert AuditEvent.objects.filter(action="preference.updated", changes={"reminders_enabled": False}).exists()
    assert editor.patch("auth/me/preferences", {"reminders_enabled": "no"}).status_code == 400


def test_teams_and_webhooks_get_one_message_each_and_a_destination_that_is_off_none(a, posts):  # noqa: F811
    ops = teams(a)
    off = teams(a, "Off")
    channel_services.update_destination(a["org"], a["users"]["admin"], off, {"enabled": False})
    hook = channel_services.create_destination(
        a["org"],
        a["users"]["admin"],
        WebhookDestination,
        {"name": "SIEM", "url": WEBHOOK_URL, "signing_secret": SIGNING_SECRET},
    )
    rule(
        a,
        lead_times_days=[30],
        send_email=False,
        teams_destinations=[str(ops.id), str(off.id)],
        webhook_destinations=[str(hook.id)],
    )
    reminder(a, 20)
    assert run(a) == 2
    drain()
    sent = Delivery.objects.order_by("channel")
    assert [(d.channel, d.destination_name, d.status) for d in sent] == [
        ("teams", "Ops", "sent"),
        ("webhook", "SIEM", "sent"),
    ]
    webhook = Delivery.objects.get(channel="webhook")
    assert webhook.content["event"] == "reminder.due"
    assert webhook.content["data"]["lead_time_days"] == 30 and webhook.content["data"]["reminder"]["title"]
    assert WEBHOOK_URL not in str(webhook.content) and TEAMS_URL not in str(sent[0].content)


# ---- filters ------------------------------------------------------------------------------------------------------


def matched(f, made) -> list:
    made.refresh_from_db()
    return [p.reminder for p in rules.plan(f["org"], today(f), [made])]


def test_filters_by_criticality_kind_vendor_and_annual_value(a):
    vendor = Vendor.objects.create(organization=a["org"], name="Contoso")
    Resource.objects.filter(id=a["app"].id).update(criticality="high")
    made = rule(a, lead_times_days=[30], criticalities=["critical"])
    reminder(a, 20)
    assert run(a) == 0 and matched(a, made) == []
    # Which reminders a rule covers (widening an enabled rule baselines what is already running: see the no-burst
    # tests; matching is checked on the plan).
    rule_services.update_rule(a["org"], a["users"]["admin"], made, {"criticalities": ["high"]})
    assert len(matched(a, made)) == 1
    rule_services.update_rule(a["org"], a["users"]["admin"], made, {"criticalities": [], "record_kinds": ["component"]})
    assert matched(a, made) == []
    rule_services.update_rule(a["org"], a["users"]["admin"], made, {"record_kinds": ["application"]})
    assert len(matched(a, made)) == 1
    Delivery.objects.all().delete()
    # Vendor and value: contracts.
    end = today(a) + timedelta(days=a["org"].renewal_lead_days + 30)
    cheap = contracts.create_contract(
        a["org"],
        a["users"]["editor"],
        {"name": "Cheap", "end_date": end.isoformat(), "auto_renew": "no", "amount": "100", "vendor": str(vendor.id)},
    )
    contracts.create_contract(
        a["org"],
        a["users"]["editor"],
        {"name": "Big", "end_date": end.isoformat(), "auto_renew": "no", "amount": "90000"},
    )
    reminders.generate(a["org"], today(a))
    rule_services.update_rule(
        a["org"],
        a["users"]["admin"],
        made,
        {"source_types": ["contract"], "record_kinds": [], "vendor": str(vendor.id)},
    )
    assert run(a) == 1 and emails().get().reminder.contract_id == cheap.id
    Delivery.objects.all().delete()
    rule_services.update_rule(a["org"], a["users"]["admin"], made, {"vendor": None, "min_annual_value": "50000"})
    assert run(a) == 1 and emails().get().reminder.title == "Big"


# ---- preview and test send ---------------------------------------------------------------------------------------


def test_preview_shows_what_the_planner_would_send_and_writes_nothing(a, api):
    a["people"]["priya"].email = "priya@orga.test"
    a["people"]["priya"].save()
    made = rule(a, lead_times_days=[30, 7], escalation_days=[2], people=[str(a["people"]["priya"].id)])
    reminder(a, 40)
    snoozed = reminder(a, 10)
    notify_services.snooze_reminder(
        a["org"], a["users"]["editor"], snoozed, {"until": (today(a) + timedelta(days=4)).isoformat()}
    )
    admin = api(a["users"]["admin"])
    with CaptureQueriesContext(connection) as queries:
        body = admin.get(f"notification-rules/{made.id}/preview?days=60").json_body
    writes = [
        q["sql"]
        for q in queries
        if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        and "django_session" not in q["sql"]
        and "core_ratecounter" not in q["sql"]  # the request's rate-limit count, not the preview's
    ]
    assert writes == []
    events = [(e["date"], e["lead_time_days"], e["overdue_step"], e["reminder"]["due_date"]) for e in body["events"]]
    t = today(a)

    def day(n):
        return (t + timedelta(days=n)).isoformat()

    assert events == [
        (day(5), 7, None, day(10)),  # the day after the snooze: 5 days left, so the 7-day lead (30 was missed)
        (day(10), 30, None, day(40)),
        (day(12), None, 2, day(10)),
        (day(33), 7, None, day(40)),
        (day(42), None, 2, day(40)),
    ]
    assert {r["email"] for r in body["events"][0]["recipients"]} == {a["users"]["editor"].email, "priya@orga.test"}
    assert not Delivery.objects.exists() and body["days"] == 60 and body["start"] == t.isoformat()
    # The planner agrees on the first day the preview names.
    assert run(a, 5) == 2 and run(a, 4) == 0
    for bad in ("0", "181", "x"):
        assert admin.get(f"notification-rules/{made.id}/preview?days={bad}").status_code == 400
    assert api(a["users"]["editor"]).get(f"notification-rules/{made.id}/preview").status_code == 403


def test_test_send_sends_a_sample_to_you_and_a_chosen_destination(a, api, posts):  # noqa: F811
    ops = teams(a)
    made = rule(a, lead_times_days=[30])
    admin = api(a["users"]["admin"])
    response = admin.post(f"notification-rules/{made.id}/test-send", {"teams_destination": str(ops.id)})
    assert response.status_code == 200 and response.json_body["ok"] is True
    kinds = {
        (r["delivery"]["channel"], r["delivery"]["kind"], r["delivery"]["rule_name"])
        for r in response.json_body["results"]
    }
    assert kinds == {("email", "test", made.name), ("teams", "test", made.name)}
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [a["users"]["admin"].email]
    assert mail.outbox[0].subject.startswith("[Test] ") and "Example: Contoso CRM" in mail.outbox[0].body
    assert len(posts.calls) == 1
    event = AuditEvent.objects.get(action="notification_rule.tested")
    assert event.changes["ok"] is True and len(event.changes["deliveries"]) == 2
    # The real reminder is used when the rule covers one; email off sends to the destination only.
    reminder(a, 20)
    again = admin.post(
        f"notification-rules/{made.id}/test-send", {"email": False, "teams_destination": str(ops.id)}
    ).json_body
    assert len(again["results"]) == 1 and "Payroll API secret" in str(
        Delivery.objects.get(id=again["results"][0]["delivery"]["id"]).content
    )
    assert admin.post(f"notification-rules/{made.id}/test-send", {"email": False}).status_code == 400
    assert api(a["users"]["editor"]).post(f"notification-rules/{made.id}/test-send", {}).status_code == 403


def test_test_send_of_the_daily_digest_sends_your_own_digest(a, api):
    response = api(a["users"]["admin"]).post(f"notification-rules/{builtin(a, 'daily_digest').id}/test-send", {})
    assert response.status_code == 200 and response.json_body["ok"] is True
    assert len(mail.outbox) == 1 and mail.outbox[0].subject.startswith("[Test] Softwarden: ")
    assert "Hi " in mail.outbox[0].body


# ---- tenancy and query counts ------------------------------------------------------------------------------------


def test_another_organizations_reminders_never_reach_a_rule(a, b):
    rule(a, lead_times_days=[30])
    reminder(b, 20, resource=b["app"])
    assert run(a) == 0


def _second_run_queries(f, count: int) -> int:
    for _ in range(count):
        reminder(f, 20)
    run(f)
    with CaptureQueriesContext(connection) as queries:
        assert run(f) == 0
    return len(queries)


def test_planner_queries_dont_grow_with_reminders(a, b):
    a["people"]["priya"].email = "priya@orga.test"
    a["people"]["priya"].save()
    b["people"]["priya"].email = "priya@orgb.test"
    b["people"]["priya"].save()
    for f in (a, b):
        rule(
            f,
            lead_times_days=[30],
            people=[str(f["people"]["priya"].id)],
            team=str(f["team"].id),
            roles=["admin"],
            criticalities=["high", "medium", "low", "critical"],
            teams_destinations=[str(teams(f).id)],
        )
        Resource.objects.filter(id=f["app"].id).update(criticality="high")
    small = _second_run_queries(a, 3)
    large = _second_run_queries(b, 15)
    assert small == large and small <= 15


def test_rule_tables_reject_rows_of_another_organization(a, b):
    from notify.models import (
        NotificationRuleEscalationPerson,
        NotificationRulePerson,
        NotificationRuleTeamsDestination,
        NotificationRuleWebhookDestination,
    )
    from tests.test_tenancy import _rejected

    mine = rule(a, lead_times_days=[30])
    theirs_team = teams(b, "Theirs")
    their_hook = channel_services.create_destination(
        b["org"],
        b["users"]["admin"],
        WebhookDestination,
        {"name": "B", "url": WEBHOOK_URL, "signing_secret": SIGNING_SECRET},
    )
    their_vendor = Vendor.objects.create(organization=b["org"], name="Theirs")
    for field, value in (
        ("team", b["team"]),
        ("escalation_team", b["team"]),
        ("fallback_team", b["team"]),
        ("fallback_person", b["people"]["dana"]),
        ("vendor", their_vendor),
    ):
        _rejected(lambda field=field, value=value: NotificationRule.objects.filter(id=mine.id).update(**{field: value}))
    base = {"organization": a["org"], "rule": mine}
    _rejected(lambda: NotificationRulePerson.objects.create(person=b["people"]["dana"], **base))
    _rejected(lambda: NotificationRuleEscalationPerson.objects.create(person=b["people"]["dana"], **base))
    _rejected(lambda: NotificationRuleTeamsDestination.objects.create(destination=theirs_team, **base))
    _rejected(lambda: NotificationRuleWebhookDestination.objects.create(destination=their_hook, **base))
    their_rule = builtin(b, "renewals_contracts")
    _rejected(
        lambda: NotificationRulePerson.objects.create(
            organization=a["org"], rule=their_rule, person=a["people"]["dana"]
        )
    )
    _rejected(
        lambda: Delivery.objects.create(
            organization=a["org"], channel="email", recipient="x@y.test", dedupe_key="k", subject="s", rule=their_rule
        )
    )
    with pytest.raises(ValueError):
        from notify import deliveries

        deliveries.enqueue(
            a["org"], channel="email", dedupe_key="k2", subject="s", content={}, recipient="x@y.test", rule=their_rule
        )


def test_rule_ids_are_404_across_organizations(a, b, api):
    theirs = builtin(b, "renewals_contracts")
    admin = api(a["users"]["admin"])
    for method, path in (
        ("get", f"notification-rules/{theirs.id}"),
        ("patch", f"notification-rules/{theirs.id}"),
        ("get", f"notification-rules/{theirs.id}/preview"),
        ("post", f"notification-rules/{theirs.id}/test-send"),
    ):
        response = getattr(admin, method)(path, {}) if method != "get" else admin.get(path)
        assert response.status_code == 404, path
    assert {r["id"] for r in admin.get("notification-rules").json_body}.isdisjoint(
        set(NotificationRule.objects.filter(organization=b["org"]).values_list("id", flat=True))
    )


def test_deliveries_keep_the_rules_name_after_it_is_deleted(a):
    made = rule(a, lead_times_days=[30])
    reminder(a, 20)
    run(a)
    rule_services.delete_rule(a["org"], a["users"]["admin"], made)
    delivery = Delivery.objects.get()
    assert delivery.rule_id is None and delivery.rule_name == made.name


def test_preferences_default_on_and_unknown_keys_refused(a, api):
    client = api(a["users"]["viewer"])
    shown = client.get("auth/me/preferences").json_body
    assert shown["reminders_enabled"] is True and shown["digest_enabled"] is True
    assert client.patch("auth/me/preferences", {"nope": True}).status_code == 400
    assert client.patch("auth/me/preferences", {"digest_enabled": False}).json_body["reminders_enabled"] is True
    pref = NotificationPreference.objects.get(membership__user=a["users"]["viewer"])
    assert pref.digest_enabled is False and pref.reminders_enabled is True


def test_value_filter_keeps_cents(a):
    made = rule(a, lead_times_days=[30], min_annual_value="1234.5")
    assert made.min_annual_value == Decimal("1234.50")


# ---- migrations ---------------------------------------------------------------------------------------------------

BEFORE_RULES = [("notify", "0004_channels_and_deliveries")]


@pytest.mark.django_db(transaction=True)
def test_existing_organizations_get_the_built_ins_and_their_digests_become_deliveries():
    """notify 0005 seeds the built-in rules for organizations that already exist; 0006 turns each digest into a
    delivery (pending becomes queued) carrying its message, and the digest keeps who, when and how many."""
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE_RULES)
    old = executor.loader.project_state(BEFORE_RULES).apps
    Org, User = old.get_model("core", "Organization"), old.get_model("auth", "User")
    Run, OldDigest = old.get_model("notify", "DigestRun"), old.get_model("notify", "Digest")
    org = Org.objects.create(name="Upgraded (synthetic)", slug="upgraded-synthetic")
    user = User.objects.create(username="u@upgraded.test", email="u@upgraded.test")
    other = User.objects.create(username="v@upgraded.test", email="v@upgraded.test")
    stale = User.objects.create(username="w@upgraded.test", email="w@upgraded.test")
    run = Run.objects.create(organization=org, for_date=date(2026, 11, 4))
    common = {"organization": org, "run": run, "for_date": run.for_date, "item_count": 3, "html_body": "<p>h</p>"}
    OldDigest.objects.create(user=user, status="sent", subject="Sent one", text_body="t1", attempts=1, **common)
    OldDigest.objects.create(
        user=other, status="pending", subject="Pending one", text_body="t2", attempts=2, last_error="Down", **common
    )
    old_one = OldDigest.objects.create(user=stale, status="pending", subject="Old", text_body="t3", **common)
    OldDigest.objects.filter(id=old_one.id).update(created_at=timezone.now() - timedelta(days=2))
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    try:
        new = executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps
        Rule, NewDigest = new.get_model("notify", "NotificationRule"), new.get_model("notify", "Digest")
        keys = set(Rule.objects.filter(organization_id=org.id).values_list("builtin_key", flat=True))
        assert keys == BUILTIN_KEYS
        # Not baselined yet: the planner's first run records what is in flight instead of sending it.
        assert not Rule.objects.filter(organization_id=org.id, baselined_at__isnull=False).exists()
        digest_rule = Rule.objects.get(organization_id=org.id, builtin_key="daily_digest")
        got = {
            d.user_id: d.delivery for d in NewDigest.objects.filter(organization_id=org.id).select_related("delivery")
        }
        assert got[user.id].status == "sent" and got[user.id].content == {"text": "t1", "html": "<p>h</p>"}
        assert got[other.id].status == "queued" and got[other.id].attempts == 2 and got[other.id].last_error == "Down"
        assert got[user.id].dedupe_key == f"digest:{user.id}:2026-11-04" and got[user.id].kind == "digest"
        assert got[user.id].rule_id == digest_rule.id and got[user.id].recipient == "u@upgraded.test"
        assert NewDigest.objects.get(organization_id=org.id, user_id=user.id).item_count == 3
        assert got[stale.id].status == "failed" and got[stale.id].next_attempt_at is None
        assert got[stale.id].last_error == "Not sent: pending since 2026-11-04 when deliveries were introduced."
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM catalog_digest WHERE organization_id = %s", [org.id])
            cursor.execute("DELETE FROM notify_delivery WHERE organization_id = %s", [org.id])
            cursor.execute("DELETE FROM catalog_digestrun WHERE organization_id = %s", [org.id])
            cursor.execute("DELETE FROM notify_notificationrule WHERE organization_id = %s", [org.id])
            cursor.execute("DELETE FROM catalog_organization WHERE id = %s", [org.id])
            cursor.execute("DELETE FROM auth_user WHERE id IN (%s, %s, %s)", [user.id, other.id, stale.id])


# ---- the day's hour, no burst, audit of webhooks, preview cost, addresses -------------------------------------------


def test_no_notice_before_the_organizations_digest_time(a):
    a["org"].timezone = "Pacific/Auckland"
    a["org"].digest_time = time(8, 0)
    a["org"].save()
    rule(a, lead_times_days=[30])
    reminder(a, 20)
    local = datetime.combine(today(a), time(7, 59), tzinfo=ZoneInfo("Pacific/Auckland"))
    assert rules.run_for_org(a["org"], local) == 0  # 07:59 in Auckland
    assert rules.run_for_org(a["org"], local + timedelta(minutes=2)) == 1  # 08:01
    # 19:30 UTC is 08:30 the next day in Auckland (UTC+13 in November): that local day's notices may go.
    utc = datetime.combine(today(a), time(19, 30), tzinfo=UTC)
    assert utc.astimezone(ZoneInfo("Pacific/Auckland")).time() >= time(8, 0)
    assert rules.run_for_org(a["org"], utc - timedelta(hours=2)) == 0  # 06:30 local on that day: nothing yet


def test_a_new_rule_baselines_reminders_already_inside_a_stage_and_sends_only_later_stages(a):
    reminder(a, 25)  # already inside the 30-day lead when the rule is made
    made = rule(a, lead_times_days=[30, 7])
    assert made.baselined_at is not None
    baselined = Delivery.objects.get()
    assert baselined.status == "skipped" and baselined.last_error == rules.BASELINE_NOTE
    assert baselined.lead_time_days == 30 and baselined.rule == made
    assert run(a) == 0
    reminder(a, 20)  # enters a stage after the rule exists: sends
    assert run(a) == 1
    assert run(a, 18) == 2  # both reach the 7-day lead (7 and 2 days left): future stages send
    assert run(a, 19) == 0


def test_turning_a_rule_on_baselines_too(a):
    made = rule(a, lead_times_days=[30])
    rule_services.update_rule(a["org"], a["users"]["admin"], made, {"enabled": False})
    reminder(a, 20)
    rule_services.update_rule(a["org"], a["users"]["admin"], made, {"enabled": True})
    assert run(a) == 0 and Delivery.objects.get().status == "skipped"


def test_built_ins_seeded_by_the_migration_are_baselined_by_their_first_run(a):
    """notify 0005 leaves `baselined_at` null for organizations that existed: the first run records, never sends."""
    renewals = builtin(a, "renewals_contracts")
    end = today(a) + timedelta(days=a["org"].renewal_lead_days + 20)
    contracts.create_contract(
        a["org"], a["users"]["editor"], {"name": "Old", "end_date": end.isoformat(), "auto_renew": "yes"}
    )
    reminders.generate(a["org"], today(a))
    NotificationRule.objects.filter(organization=a["org"]).update(baselined_at=None)  # as after the migration
    assert run(a) == 0
    renewals.refresh_from_db()
    assert renewals.baselined_at is not None
    assert {d.status for d in Delivery.objects.filter(rule=renewals)} == {"skipped"}
    assert run(a) == 0
    assert run(a, 13) == 1  # 7 days before: a later stage sends normally


def test_preview_skips_the_baseline_of_a_rule_thats_off(a):
    made = rule(a, lead_times_days=[30, 7])
    rule_services.update_rule(a["org"], a["users"]["admin"], made, {"enabled": False})
    made.refresh_from_db()
    reminder(a, 20)
    events = rules.preview(a["org"], made, today(a), 30)
    assert [e["lead_time_days"] for e in events] == [7]  # turning it on today would baseline the 30-day lead


def test_webhook_destinations_are_named_in_the_rule_audit_without_an_address(a):
    hook = channel_services.create_destination(
        a["org"],
        a["users"]["admin"],
        WebhookDestination,
        {"name": "SIEM", "url": WEBHOOK_URL, "signing_secret": SIGNING_SECRET},
    )
    made = rule(a, lead_times_days=[30], webhook_destinations=[str(hook.id)])
    created = AuditEvent.objects.get(action="notification_rule.created")
    assert created.changes["webhook_destinations"] == [{"id": str(hook.id), "name": "SIEM"}]
    rule_services.update_rule(a["org"], a["users"]["admin"], made, {"webhook_destinations": []})
    updated = AuditEvent.objects.get(action="notification_rule.updated")
    assert updated.changes["webhook_destinations"] == [[{"id": str(hook.id), "name": "SIEM"}], []]
    for event in (created, updated):
        stored = str([event.changes, event.prechange, event.postchange])
        assert WEBHOOK_URL not in stored and "WEBHOOK-BEARER" not in stored and SIGNING_SECRET not in stored


def test_preview_jumps_between_stage_changes(a, monkeypatch):
    """Cost bound: decide runs a handful of times per reminder, not once per day of the window."""
    made = rule(a, lead_times_days=[90, 60, 30, 7], escalation_days=[1, 7])
    Reminder.objects.bulk_create(
        [
            Reminder(
                organization=a["org"],
                source_type="credential_expiry",
                source_id=uuid.uuid4(),
                source_field="k",
                due_date=today(a) + timedelta(days=n % 200 - 10),
                title=f"Secret {n}",
                label="Client secret expires",
                resource=a["app"],
            )
            for n in range(400)
        ]
    )
    calls = 0
    real = rules.decide

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(rules, "decide", counting)
    events = rules.preview(a["org"], made, today(a), rules.PREVIEW_MAX_DAYS)
    assert events and calls <= 400 * 8, calls


def test_one_email_per_address_and_an_opted_out_members_address_stays_opted_out(a, api):
    editor, admin = a["users"]["editor"], a["users"]["admin"]
    api(editor).patch("auth/me/preferences", {"reminders_enabled": False})
    # People with no account whose addresses are members' (any case): the editor's opt-out holds, the admin is
    # emailed once although reached both by role and as a person.
    twin = Person.objects.create(organization=a["org"], name="Editor entry", email=editor.email.upper())
    admin_entry = Person.objects.create(organization=a["org"], name="Admin entry", email=admin.email.upper())
    made = rule(a, lead_times_days=[30], people=[str(twin.id), str(admin_entry.id)], roles=["admin"])
    reminder(a, 20)  # routed to the editor, who opted out
    assert run(a) == 1 and emails().get().recipient.lower() == admin.email
    planned = rules.plan(a["org"], today(a), [made])[0]
    assert set(planned.audience.opted_out.values()) == {"Editor entry", editor.email}
    assert len(planned.audience.recipients) == 1


def test_a_person_already_on_the_rule_can_stay_after_being_deactivated(a):
    person = a["people"]["priya"]
    made = rule(a, lead_times_days=[30], people=[str(person.id)], fallback_person=str(person.id))
    Person.objects.filter(id=person.id).update(active=False)
    kept = rule_services.update_rule(
        a["org"],
        a["users"]["admin"],
        made,
        {"people": [str(person.id)], "fallback_person": str(person.id), "lead_times_days": [14]},
    )
    assert kept.lead_times_days == [14]
    with pytest.raises(rule_services.FieldErrors):
        rule_services.update_rule(a["org"], a["users"]["admin"], made, {"people": [str(a["people"]["gone"].id)]})


# ---- dates moved far, widening rules, late digest times, same-day stages, the log's baselines -----------------------


def test_a_deadline_moved_a_year_later_gets_every_lead_again(a):
    """An extended contract: the leads sent for the old date don't cover a date a year later."""
    rule(a, lead_times_days=[90, 60, 30, 7])
    found = reminder(a, 100)
    for days in (10, 40, 70, 93):  # 90, 60, 30 and 7 days before the old date
        assert run(a, days) == 1
    Reminder.objects.filter(id=found.id).update(due_date=found.due_date + timedelta(days=365))
    assert run(a, 94) == 0  # the new date is far off: nothing until its own marks
    for days in (375, 405, 435, 458):  # 90, 60, 30 and 7 days before the new date
        assert run(a, days) == 1, days
        assert run(a, days) == 0
    sent = list(emails().values_list("lead_time_days", "due_date"))
    assert [lead for lead, _due in sent] == [90, 60, 30, 7, 90, 60, 30, 7]
    assert {due for _lead, due in sent[4:]} == {found.due_date + timedelta(days=365)}


def test_a_deadline_moved_ten_days_later_doesnt_repeat_the_30_day_notice(a):
    rule(a, lead_times_days=[30, 7])
    found = reminder(a, 30)
    assert run(a) == 1
    Reminder.objects.filter(id=found.id).update(due_date=found.due_date + timedelta(days=10))
    assert run(a) == 0  # 40 days left: no lead yet
    assert run(a, 10) == 0  # 30 days before the new date: 10 days later is inside the 30-day lead, covered
    assert run(a, 33) == 1  # the 7-day lead, never sent
    assert [d.lead_time_days for d in emails()] == [30, 7]


def test_adding_a_teams_channel_to_an_enabled_rule_posts_no_burst(a, posts):  # noqa: F811
    made = rule(a, lead_times_days=[30, 7])
    for days in (20, 21, 22):
        reminder(a, days)
    assert run(a) == 3  # the emails, before Teams is added
    ops = teams(a)
    rule_services.update_rule(a["org"], a["users"]["admin"], made, {"teams_destinations": [str(ops.id)]})
    assert run(a) == 0 and run(a, 1) == 0
    baselined = Delivery.objects.filter(channel="teams")
    assert baselined.count() == 3 and all(d.baseline and d.status == "skipped" for d in baselined)
    assert run(a, 15) == 6  # the 7-day lead (catch-up for all three): an email and a card each
    drain()
    assert len(posts.calls) == 3


def test_loosening_a_filter_or_adding_a_lead_causes_no_burst(a):
    Resource.objects.filter(id=a["app"].id).update(criticality="high")
    made = rule(a, lead_times_days=[7], criticalities=["critical"])
    reminder(a, 20)
    rule_services.update_rule(a["org"], a["users"]["admin"], made, {"criticalities": []})
    assert run(a) == 0  # covered now, but no stage is running yet (lead 7)
    rule_services.update_rule(a["org"], a["users"]["admin"], made, {"lead_times_days": [30, 7]})
    assert run(a) == 0  # the new 30-day lead was already running: baselined
    assert Delivery.objects.get().baseline and Delivery.objects.get().lead_time_days == 30
    assert run(a, 13) == 1  # the 7-day lead sends


def test_a_stage_already_owed_today_still_sends_after_an_unrelated_change(a):
    """Only messages a change adds are baselined: a notice owed before the change (not yet sent, before the digest
    time) still goes."""
    made = rule(a, lead_times_days=[30])
    reminder(a, 20)  # made after the rule: owed, not baselined
    rule_services.update_rule(a["org"], a["users"]["admin"], made, {"name": "Renamed"})
    assert run(a) == 1 and not Delivery.objects.filter(baseline=True).exists()


def test_a_stage_beginning_today_isnt_baselined(a):
    reminder(a, 30)  # the 30-day lead begins today
    reminder(a, 29)  # began yesterday
    rule(a, lead_times_days=[30])
    assert Delivery.objects.filter(baseline=True).count() == 1
    assert run(a) == 1 and emails().filter(status="queued").get().reminder.due_date == today(a) + timedelta(days=30)


def test_a_digest_time_after_the_last_hourly_run_still_sends_that_day(a):
    a["org"].digest_time = time(23, 50)
    a["org"].save()
    assert rules.send_time(a["org"]) == time(23, 0)
    rule(a, lead_times_days=[0])  # a one-day stage
    reminder(a, 0)
    zone = ZoneInfo(a["org"].timezone)
    assert rules.run_for_org(a["org"], datetime.combine(today(a), time(22, 25), tzinfo=zone)) == 0
    assert rules.run_for_org(a["org"], datetime.combine(today(a), time(23, 25, 30), tzinfo=zone)) == 1


def last_run_of_the_day(zone: str, day: date, minute: int) -> datetime:
    """The last hourly run (UTC minute `minute`) that falls on local `day` in `zone`."""
    tz = ZoneInfo(zone)
    start = datetime.combine(day, time(0, 0), tzinfo=tz).astimezone(UTC).replace(minute=minute, second=30)
    found = None
    for hours in range(-2, 28):
        moment = start + timedelta(hours=hours)
        if moment.astimezone(tz).date() == day:
            found = moment
    assert found is not None
    return found


@pytest.mark.parametrize("zone", ["Asia/Kathmandu", "Australia/Eucla", "Pacific/Chatham", "Asia/Kolkata", "UTC"])
def test_a_late_digest_time_is_reached_in_every_offset(a, zone):
    """Offsets are multiples of 15 minutes, so the run at :25 lands in the local 23:00 hour (at 23:10 in Kathmandu,
    +5:45): with the send time capped at 23:00 a digest time of 23:50 still sends that day."""
    a["org"].timezone = zone
    a["org"].digest_time = time(23, 50)
    a["org"].save()
    rule(a, lead_times_days=[0])
    reminder(a, 0)
    last = last_run_of_the_day(zone, today(a), 25)
    assert last.astimezone(ZoneInfo(zone)).hour == 23
    assert rules.run_for_org(a["org"], last - timedelta(hours=1)) == 0  # 22:xx local: before the send time
    assert rules.run_for_org(a["org"], last) == 1


def test_baselines_are_in_the_log_behind_a_filter_and_never_counted_as_digest_problems(a, api):
    reminder(a, 20)
    rule(a, lead_times_days=[30])
    admin = api(a["users"]["admin"])
    rows = admin.get("notification-log").json_body["results"]
    assert len(rows) == 1 and rows[0]["baseline"] is True and rows[0]["status"] == "skipped"
    assert rows[0]["due_date"] == (today(a) + timedelta(days=20)).isoformat()
    assert len(admin.get("notification-log?baseline=true").json_body["results"]) == 1
    assert admin.get("notification-log?baseline=false").json_body["results"] == []
    assert admin.get("notification-log?baseline=maybe").status_code == 400
    assert admin.get("auth/me").json_body["digest_problem"] is None


def test_a_baseline_cant_be_resent_and_a_resend_keeps_the_due_date(a, api):
    admin = api(a["users"]["admin"])
    reminder(a, 20)
    rule(a, lead_times_days=[30, 7])
    baselined = Delivery.objects.get(baseline=True)
    refused = admin.post(f"notification-log/{baselined.id}/resend")
    assert refused.status_code == 409
    assert refused.json_body["detail"] == (
        "This notice was recorded when the rule started and was never written; nothing to resend."
    )
    assert not Delivery.objects.filter(resend_of=baselined).exists()
    assert run(a, 13) == 1  # the 7-day lead
    drain()
    sent = emails().get(baseline=False)
    copy = admin.post(f"notification-log/{sent.id}/resend")
    assert copy.status_code == 202
    resent = Delivery.objects.get(id=copy.json_body["id"])
    assert resent.resend_of == sent and resent.due_date == sent.due_date == today(a) + timedelta(days=20)


def test_the_ledger_reads_only_the_current_notify_cycle(a):
    """A reminder's earlier cycles aren't read on every run."""
    from notify import deliveries

    made = rule(a, lead_times_days=[7], escalation_days=[1, 7, 30])
    one = reminder(a, -40, notify_cycle=3)
    keys = [f"r:{one.id.hex}:{cycle}:{made.id.hex}:o1:email:x:2026-01-01" for cycle in (1, 2, 3)]
    for key in keys:
        deliveries.enqueue(
            a["org"],
            channel="email",
            dedupe_key=key,
            subject="s",
            content={"text": "t"},
            recipient="x@orga.test",
            reminder=one,
            rule=made,
            due_date=one.due_date,
            queue_job=False,
        )
    with CaptureQueriesContext(connection) as seen:
        ledger = rules.Ledger.load(a["org"], [made], [one])
    assert ledger.keys == {keys[2]} and len(seen.captured_queries) == 1
