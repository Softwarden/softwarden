"""End-of-life reminders (notify 0012; eol/reminders.py, notify/reminders.py): a tracked version's
end-of-life date is a reminder source, announced by the built-in "End of life" rule at 90, 30 and 7 days before, then
every 30 days while the version is past its end of life (notify 0013; a cycle flagged end of life with no date is due
the day the version was tracked), to the record's IT steward, else its technical owner. End of life left the daily digest with them, and the Needs attention queue folds each version's reminder into its row.

Covers: the thresholds and the 30-day repeats, flag-only versions, routing, the `eol` capability, dedupe (reruns, a
regenerated or slightly moved date), snooze, acknowledge, the send time, absence from the digest, regeneration on
writes, folding, tenancy, the migrations' seeding and the upgrade with no burst."""

from __future__ import annotations

import importlib
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.db import connection, transaction

from catalog import services
from catalog.models import Person, Resource
from core import capabilities
from core.models import AuditEvent
from core.problems import FieldErrors
from eol import services as eol_services
from eol.models import EolRelease
from notify import digest, reminders, rules
from notify import services as notify_services
from notify.attention import attention_queues
from notify.models import Delivery, NotificationRule, Reminder, ReminderStatus
from tests.test_eol import app, product

DAYS_TO_EOL = 100


@pytest.fixture(autouse=True)
def email_on(settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.SOFTWARDEN_BASE_URL = "https://softwarden.example.test"


def today(f) -> date:
    return services.org_today(f["org"])


def at(f, days: int, minutes_after_send_time: int = 1) -> datetime:
    """`minutes_after_send_time` past the organization's send time (its digest time) on its local date `days` on."""
    zone = ZoneInfo(f["org"].timezone)
    moment = datetime.combine(today(f) + timedelta(days=days), f["org"].digest_time, tzinfo=zone)
    return moment + timedelta(minutes=minutes_after_send_time)


def run(f, days: int = 0) -> int:
    return rules.run_for_org(f["org"], at(f, days))


def only_end_of_life(f) -> NotificationRule:
    """The built-in End of life rule on, every other rule off (the other built-ins would add notices of their own)."""
    rule = NotificationRule.objects.get(organization=f["org"], builtin_key="end_of_life")
    NotificationRule.objects.filter(organization=f["org"]).exclude(pk=rule.pk).update(enabled=False)
    return rule


def notices(f):
    return Delivery.objects.filter(
        organization=f["org"], channel="email", kind__in=["reminder", "escalation"], rule__builtin_key="end_of_life"
    ).order_by("created_at", "id")


def tracked(f, name="Billing DB", days=DAYS_TO_EOL, steward="editor", technical_owner=None, cycle="12"):
    """A record tracked on PostgreSQL `cycle`, whose end of life is `days` from today; a cached release per call."""
    t = today(f)
    found = product_once(f)
    EolRelease.objects.update_or_create(
        product=found, name=cycle, defaults={"position": int(cycle), "eol_from": t + timedelta(days=days)}
    )
    record = app(f, name)
    owner = {"technical_owner": technical_owner} if technical_owner is not None else {}
    Resource.objects.filter(pk=record.pk).update(it_steward=f["users"][steward] if steward else None, **owner)
    eol_services.track(f["org"], f["users"]["editor"], record, {"product": "postgresql", "cycle": cycle})
    return record


def product_once(f):
    from eol.models import EolProduct

    return EolProduct.objects.filter(name="postgresql").first() or product("postgresql", "PostgreSQL")


def generate(f, days: int = 0):
    return reminders.generate(f["org"], today(f) + timedelta(days=days))


def eol_reminders(f):
    return Reminder.objects.filter(organization=f["org"], source_type="end_of_life")


# ---- the rule and its thresholds --------------------------------------------------------------------------------------


def test_every_organization_has_the_end_of_life_built_in(a):
    rule = NotificationRule.objects.get(organization=a["org"], builtin_key="end_of_life")
    assert rule.name == "End of life" and rule.kind == "reminders" and rule.enabled
    assert rule.source_types == ["end_of_life"] and rule.lead_times_days == [90, 30, 7]
    assert rule.escalation_days == [] and rule.notify_routed and rule.send_email
    assert rule.repeat_every_days == 30 and rule.escalate_when_acknowledged is False  # repeats until acknowledged
    assert rule.baselined_at is not None  # a new organization's built-ins start baselined


def test_a_version_is_announced_at_90_30_and_7_days_once_each(a):
    only_end_of_life(a)
    record = tracked(a)
    generate(a)
    [reminder] = eol_reminders(a)
    assert reminder.source_id == record.id and reminder.resource_id == record.id
    assert reminder.due_date == today(a) + timedelta(days=DAYS_TO_EOL) and reminder.title == "Billing DB"
    assert reminder.label == "End of life: PostgreSQL 12" and reminder.status == ReminderStatus.OPEN
    sent_on = {}
    for days in (0, 9, 10, 11, 40, 69, 70, 71, 92, 93, 94, 100, 101, 129, 130, 131, 159, 160):
        count = run(a, days)
        if count:
            sent_on[days] = count
    # 90, 30 and 7 days left; nothing on the date (the 7-day notice covers it); then 30 and 60 days after it.
    assert sent_on == {10: 1, 70: 1, 93: 1, 130: 1, 160: 1}
    assert [(d.lead_time_days, d.recipient, d.due_date) for d in notices(a)][:3] == [
        (lead, a["users"]["editor"].email, reminder.due_date) for lead in (90, 30, 7)
    ]
    assert [(d.kind, d.overdue_step) for d in notices(a)][3:] == [("escalation", 30), ("escalation", 60)]
    first = notices(a).first()
    assert first.subject and "Billing DB" in first.content["text"] and "#h-eol" in first.content["text"]


def test_nothing_goes_out_before_the_send_time(a):
    """Quiet hours: notices go from the organization's send time (its digest time) on, never before it."""
    only_end_of_life(a)
    tracked(a, days=90)
    generate(a)
    assert rules.run_for_org(a["org"], at(a, 0, minutes_after_send_time=-2)) == 0
    assert rules.run_for_org(a["org"], at(a, 0, minutes_after_send_time=1)) == 1


def test_a_version_already_inside_a_lead_when_tracked_gets_that_lead_only(a):
    """The catch-up rule: tracked 20 days before its end of life, it gets the 30-day notice once, never the 90."""
    only_end_of_life(a)
    tracked(a, days=20)
    generate(a)
    assert run(a) == 1 and run(a, 1) == 0
    assert [d.lead_time_days for d in notices(a)] == [30]


# ---- routing ------------------------------------------------------------------------------------------------------------


def test_routed_to_the_it_steward_else_the_technical_owner_else_the_coordinator(a):
    only_end_of_life(a)
    viewer = a["users"]["viewer"]
    owner = Person.objects.create(organization=a["org"], name="Vic Viewer", user=viewer)
    tracked(a, "Stewarded", days=90, technical_owner=owner)  # the steward wins
    tracked(a, "Owned", days=90, steward=None, technical_owner=owner)
    tracked(a, "Nobody's", days=90, steward=None)
    generate(a)
    assert run(a) == 3
    sent = {d.reminder.title: d.recipient for d in notices(a)}
    assert sent == {
        "Stewarded": a["users"]["editor"].email,
        "Owned": viewer.email,
        "Nobody's": a["users"]["admin"].email,  # unassigned: the IT coordinator
    }
    # The same routing the reminders API shows.
    routed = dict(eol_reminders(a).annotate(to=reminders.routed_to()).values_list("title", "to"))
    assert routed == {"Stewarded": a["users"]["editor"].id, "Owned": viewer.id, "Nobody's": None}


def test_other_sources_never_fall_back_to_the_technical_owner(a):
    """The technical owner fallback is end of life's own: a record review reminder on the same record stays
    unassigned without a steward, as Needs attention routes it."""
    viewer = a["users"]["viewer"]
    owner = Person.objects.create(organization=a["org"], name="Vic Viewer", user=viewer)
    record = tracked(a, "Owned", days=90, steward=None, technical_owner=owner)
    review = Reminder.objects.create(
        organization=a["org"],
        source_type="record_review",
        source_id=record.id,
        due_date=today(a),
        title="Owned",
        label="Record review due",
        resource=record,
    )
    assert Reminder.objects.filter(pk=review.pk).annotate(to=reminders.routed_to()).get().to is None


# ---- capability, dedupe, snooze -------------------------------------------------------------------------------------------


def test_without_the_eol_capability_nothing_is_reminded_or_sent(a, monkeypatch):
    only_end_of_life(a)
    tracked(a, days=90)
    generate(a)
    [reminder] = eol_reminders(a)
    monkeypatch.setattr(capabilities, "capabilities", lambda org: capabilities.COMMUNITY - {capabilities.EOL})
    assert generate(a)["obsoleted"] == 1
    reminder.refresh_from_db()
    assert reminder.status == ReminderStatus.OBSOLETE and reminder.resolution_kind == "source_changed"
    assert run(a) == 0 and not notices(a).exists()
    monkeypatch.undo()
    assert generate(a)["reopened"] == 1  # back on: the same row opens again
    assert run(a) == 1


def test_reruns_and_regeneration_never_repeat_a_notice(a):
    only_end_of_life(a)
    tracked(a, days=30)
    generate(a)
    assert run(a) == 1
    assert generate(a) == dict.fromkeys(reminders.KEYS, 0)  # nothing to do, no second row
    assert run(a) == 0
    # endoflife.date moves the date three days earlier: the same reminder moves, and its 30-day notice isn't repeated.
    EolRelease.objects.filter(product_id="postgresql", name="12").update(eol_from=today(a) + timedelta(days=27))
    assert generate(a)["moved"] == 1 and eol_reminders(a).count() == 1
    assert run(a) == 0 and notices(a).count() == 1


def test_a_snoozed_reminder_is_quiet_through_its_date_then_catches_up(a):
    only_end_of_life(a)
    tracked(a)
    generate(a)
    assert run(a, 10) == 1  # 90 days left
    [reminder] = eol_reminders(a)
    until = today(a) + timedelta(days=75)  # over the 30-day mark (day 70)
    notify_services.snooze_reminder(a["org"], a["users"]["editor"], reminder, {"until": until.isoformat()})
    assert [run(a, days) for days in (70, 71, 75)] == [0, 0, 0]
    assert run(a, 76) == 1 and run(a, 77) == 0  # the day after: the 30-day lead it missed, once
    assert run(a, 93) == 1  # then the 7-day lead as usual
    assert [d.lead_time_days for d in notices(a)] == [90, 30, 7]


def test_an_acknowledged_reminder_sends_no_more_leads(a):
    only_end_of_life(a)
    tracked(a)
    generate(a)
    [reminder] = eol_reminders(a)
    notify_services.acknowledge_reminder(a["org"], a["users"]["editor"], reminder)
    assert [run(a, days) for days in (10, 70, 93, 130, 160)] == [0, 0, 0, 0, 0]  # no leads, no repeats


def test_closing_one_by_hand_needs_a_note(a):
    tracked(a)
    generate(a)
    [reminder] = eol_reminders(a)
    with pytest.raises(FieldErrors) as refused:
        notify_services.resolve_reminder(a["org"], a["users"]["editor"], reminder, {"kind": "done"})
    assert "security" in refused.value.errors["note"][0]
    notify_services.resolve_reminder(a["org"], a["users"]["editor"], reminder, {"kind": "done", "note": "Upgraded"})
    reminder.refresh_from_db()
    assert reminder.status == ReminderStatus.DONE


# ---- the digest and Needs attention ---------------------------------------------------------------------------------------


def test_end_of_life_is_emailed_by_the_rule_never_by_the_digest(a):
    a["org"].digest_time = time(0, 0)
    a["org"].save()
    tracked(a, days=30)
    generate(a)
    items, _ = digest.plan(a["org"], today(a))
    assert not [e for e in items.get(a["users"]["editor"].id, []) if e[0] == "end_of_life"]
    queues, _ = attention_queues(a["org"], a["users"]["editor"], today(a))
    assert [r["record"]["name"] for r in queues["end_of_life"]] == ["Billing DB"]  # still on the page
    assert rules.run_for_org(a["org"], at(a, 0)) >= 1
    assert [d.lead_time_days for d in notices(a)] == [30]


def test_needs_attention_folds_the_reminder_into_the_end_of_life_row(a, api):
    record = tracked(a, days=30)
    generate(a)
    [reminder] = eol_reminders(a)
    body = api(a["users"]["admin"]).get("needs-attention?scope=all").json_body
    [row] = body["end_of_life"]
    assert row["record"]["id"] == str(record.id)
    assert row["reminder"] == {
        "id": str(reminder.id),
        "status": "open",
        "snoozed_until": None,
        "due": reminder.due_date.isoformat(),
        "due_text": f"End of life on {reminder.due_date.isoformat()}",
    }
    assert not [r for r in body["reminders"] if r["source_type"] == "end_of_life"]  # listed once, in its row
    detail = api(a["users"]["viewer"]).get(f"reminders/{reminder.id}").json_body
    assert detail["source_type"] == "end_of_life" and detail["path"] == f"/applications/{record.id}#h-eol"


def test_a_version_flagged_end_of_life_with_no_date_is_due_the_day_it_was_tracked(a, api):
    only_end_of_life(a)
    product("legacy", "Legacy", **{"1": {"is_eol": True}})
    row = eol_services.track(a["org"], a["users"]["editor"], app(a, "Old one"), {"product": "legacy", "cycle": "1"})
    generate(a)
    [reminder] = eol_reminders(a)
    assert reminder.due_date == today(a) and reminder.label == "End of life (no date published): Legacy 1"
    assert reminder.source_field == f"tracked_version:{row.id}:undated:1"
    [shown] = api(a["users"]["admin"]).get("needs-attention?scope=all").json_body["end_of_life"]
    since = f"End of life (no date published), tracked since {today(a).isoformat()}"
    assert shown["reminder"]["id"] == str(reminder.id) and shown["reminder"]["due"] == today(a).isoformat()
    assert shown["reminder"]["due_text"] == since
    counts = {days: run(a, days) for days in (0, 1, 29, 30, 31, 59, 60)}
    assert {d: n for d, n in counts.items() if n} == {0: 1, 30: 1, 60: 1}  # due today, then every 30 days
    assert generate(a, 60) == dict.fromkeys(reminders.KEYS, 0)  # the due date never moves
    # Worded as what it is: never "due" on, or "overdue" since, the day tracking began.
    for notice in notices(a):
        assert notice.subject == f"End of life (no date published): Legacy 1: Old one (tracked since {today(a)})"
        assert since in notice.content["text"] and "overdue" not in notice.content["text"].lower()
        assert "due today" not in notice.content["text"]


def test_re_tracking_to_another_flag_only_cycle_is_a_new_occurrence(a):
    """The acknowledgement was for cycle 1: re-tracked to cycle 2 (also flagged, also with no date), it is a new
    reminder that notifies; and a date published later is a new, dated one."""
    only_end_of_life(a)
    product("legacy", "Legacy", **{"1": {"is_eol": True}, "2": {"is_eol": True}})
    record = app(a, "Old one")
    eol_services.track(a["org"], a["users"]["editor"], record, {"product": "legacy", "cycle": "1"})
    generate(a)
    [first] = eol_reminders(a)
    notify_services.acknowledge_reminder(a["org"], a["users"]["editor"], first)
    eol_services.track(a["org"], a["users"]["editor"], record, {"product": "legacy", "cycle": "2"})
    generate(a)
    first.refresh_from_db()
    assert first.status == ReminderStatus.OBSOLETE
    [second] = eol_reminders(a).filter(status=ReminderStatus.OPEN)
    assert second.label == "End of life (no date published): Legacy 2" and second.due_date == first.due_date
    assert run(a) == 1
    EolRelease.objects.filter(product_id="legacy", name="2").update(eol_from=today(a) - timedelta(days=60))
    generate(a, 1)
    second.refresh_from_db()
    assert second.status == ReminderStatus.OBSOLETE
    [dated] = eol_reminders(a).filter(status=ReminderStatus.OPEN)
    assert dated.label == "End of life: Legacy 2" and dated.due_date == today(a) - timedelta(days=60)


def test_a_long_cycle_name_still_fits_the_key():
    from eol.reminders import source_field, tracked_version_of

    field = source_field("0" * 36, "x" * 100)
    assert len(field) <= 100 and tracked_version_of(field) == "0" * 36


def test_a_version_flagged_long_after_it_was_tracked_gets_one_notice_then_the_rhythm(a):
    """Tracked 45 days ago, flagged now: due 45 days ago, so the 30-day repeat goes once (the catch-up rule), and the
    60-day one on its date."""
    only_end_of_life(a)
    product("legacy", "Legacy", **{"1": {"is_eol": True}})
    row = eol_services.track(a["org"], a["users"]["editor"], app(a, "Old one"), {"product": "legacy", "cycle": "1"})
    type(row).objects.filter(pk=row.pk).update(created_at=at(a, -45))
    generate(a)
    [reminder] = eol_reminders(a)
    assert reminder.due_date == today(a) - timedelta(days=45)
    assert [run(a, days) for days in (0, 1, 14, 15, 16)] == [1, 0, 0, 1, 0]
    assert [d.overdue_step for d in notices(a)] == [30, 60]


def test_a_version_neither_dated_nor_flagged_has_no_reminder(a, api):
    product("legacy", "Legacy", **{"1": {"is_eol": False}})
    eol_services.track(a["org"], a["users"]["editor"], app(a, "Current one"), {"product": "legacy", "cycle": "1"})
    generate(a)
    assert not eol_reminders(a).exists()


def test_an_upgraded_version_closes_its_reminder_and_stops_the_repeats(a):
    only_end_of_life(a)
    record = tracked(a, days=-30)
    generate(a)
    assert run(a) == 1
    EolRelease.objects.create(product_id="postgresql", name="16", position=16)
    eol_services.track(a["org"], a["users"]["editor"], record, {"product": "postgresql", "cycle": "16"})
    generate(a, 1)
    assert not eol_reminders(a).filter(status__in=["open", "snoozed", "acknowledged"]).exists()
    assert run(a, 30) == 0


# ---- regenerated on writes --------------------------------------------------------------------------------------------------


def test_tracking_retiring_and_untracking_regenerate_once_committed(a, django_capture_on_commit_callbacks):
    t = today(a)
    product("postgresql", "PostgreSQL", **{"12": {"eol_from": t + timedelta(days=60)}, "16": {}})
    editor = a["users"]["editor"]
    record = app(a, "Billing DB")
    with django_capture_on_commit_callbacks(execute=True):
        row = eol_services.track(a["org"], editor, record, {"product": "postgresql", "cycle": "12"})
    [made] = eol_reminders(a)
    assert made.source_field == f"tracked_version:{row.id}" and made.status == ReminderStatus.OPEN
    instance = services.create_instance(a["org"], editor, record, {"name": "Production"})
    with django_capture_on_commit_callbacks(execute=True):
        eol_services.track_instance(a["org"], editor, instance, {"cycle": "12"})
    assert sorted(eol_reminders(a).values_list("title", flat=True)) == ["Billing DB", "Billing DB (Production)"]
    with django_capture_on_commit_callbacks(execute=True):
        services.update_instance(a["org"], editor, instance, {"name": "Prod"})  # the title follows the instance
    assert eol_reminders(a).filter(title="Billing DB (Prod)", status=ReminderStatus.OPEN).exists()
    with django_capture_on_commit_callbacks(execute=True):
        services.update_resource(a["org"], editor, Resource.objects.get(pk=record.pk), {"lifecycle": "retired"})
    assert not eol_reminders(a).filter(status__in=["open", "snoozed", "acknowledged"]).exists()
    with django_capture_on_commit_callbacks(execute=True):
        services.update_resource(a["org"], editor, Resource.objects.get(pk=record.pk), {"lifecycle": "active"})
    assert eol_reminders(a).filter(status=ReminderStatus.OPEN).count() == 2  # back
    with django_capture_on_commit_callbacks(execute=True):
        eol_services.untrack(a["org"], editor, Resource.objects.get(pk=record.pk))
    assert not eol_reminders(a).filter(status=ReminderStatus.OPEN).exists()


def test_another_organizations_versions_never_reach_this_one(a, b):
    only_end_of_life(a)
    tracked(b, "Their DB", days=90)
    generate(a)
    generate(b)
    assert not eol_reminders(a).exists() and eol_reminders(b).count() == 1
    assert run(a) == 0
    assert rules.run_for_org(b["org"], at(b, 0)) >= 1
    assert all(d.recipient.endswith("@orgb.test") for d in Delivery.objects.filter(organization=b["org"]))


# ---- notify 0012 seeding ------------------------------------------------------------------------------------------------------


class _Editor:
    def execute(self, sql):
        with connection.cursor() as cursor:
            cursor.execute(sql)


def test_the_migration_seeds_the_built_in_once_and_never_stops_on_a_name(a, b):
    from django.apps import apps as django_apps

    from tests.conftest import make_org

    migration = importlib.import_module("notify.migrations.0012_end_of_life_reminders")
    c = make_org("orgc")
    NotificationRule.objects.filter(organization__in=[a["org"], b["org"]], builtin_key="end_of_life").delete()

    def custom(org, name):
        NotificationRule.objects.create(organization=org, name=name, kind="reminders", source_types=["contract"])

    custom(a["org"], "end of life")  # the same name, other case
    custom(b["org"], "End of life")
    custom(b["org"], "End of life (built-in)")
    kept = NotificationRule.objects.get(organization=c["org"], builtin_key="end_of_life")
    with transaction.atomic():
        migration.seed_existing(django_apps, _Editor())
    seeded = {
        r.organization_id: r
        for r in NotificationRule.objects.filter(
            builtin_key="end_of_life", organization__in=[a["org"], b["org"], c["org"]]
        )
    }
    assert seeded[a["org"].id].name == "End of life (built-in)"
    assert seeded[b["org"].id].name == "End of life (built-in 2)"
    assert seeded[c["org"].id].id == kept.id  # already had it: no duplicate
    for org in (a["org"], b["org"]):
        rule = seeded[org.id]
        assert rule.baselined_at is None  # the planner's first run baselines it, so the upgrade sends no burst
        assert rule.source_types == ["end_of_life"] and rule.lead_times_days == [90, 30, 7] and rule.enabled
    with transaction.atomic():
        migration.seed_existing(django_apps, _Editor())  # a rerun adds nothing
    assert NotificationRule.objects.filter(builtin_key="end_of_life").count() == 3


def test_an_upgraded_organization_gets_no_burst_for_versions_already_inside_a_lead(a):
    """The seeded rule isn't baselined: its first run records the stages already running instead of sending them."""
    only_end_of_life(a)
    NotificationRule.objects.filter(organization=a["org"], builtin_key="end_of_life").update(baselined_at=None)
    tracked(a, days=20)
    generate(a)
    assert run(a) == 0
    assert notices(a).count() == 1 and notices(a).get().baseline is True
    assert run(a, 13) == 1  # 7 days left: a stage that begins after the upgrade is sent


# ---- repeating every 30 days past end of life (notify 0013) -------------------------------------------------------------


def test_a_version_past_end_of_life_is_announced_every_30_days(a):
    """Thirty days past its end of life when tracked: one notice now, the next 30 days later, never at 29."""
    only_end_of_life(a)
    tracked(a, days=-30)
    generate(a)
    [reminder] = eol_reminders(a)
    assert reminder.due_date == today(a) - timedelta(days=30)
    assert [run(a, days) for days in (0, 1, 29, 30, 31, 59, 60)] == [1, 0, 0, 1, 0, 0, 1]
    sent = list(notices(a))
    assert [(d.kind, d.overdue_step, d.lead_time_days) for d in sent] == [
        ("escalation", 30, None),
        ("escalation", 60, None),
        ("escalation", 90, None),
    ]
    assert {d.recipient for d in sent} == {a["users"]["editor"].email}  # the routed owner, as for the leads
    assert sent[0].subject.startswith("Overdue: End of life: PostgreSQL 12: Billing DB (30 days overdue)")


def test_a_version_past_end_of_life_by_less_than_30_days_waits_for_the_mark(a):
    only_end_of_life(a)
    tracked(a, days=-10)
    generate(a)
    assert [run(a, days) for days in (0, 19, 20, 21)] == [0, 0, 1, 0]


def test_repeats_are_never_sent_twice_by_reruns_or_regeneration(a):
    only_end_of_life(a)
    tracked(a, days=-30)
    generate(a)
    assert run(a) == 1
    assert rules.run_for_org(a["org"], at(a, 0, minutes_after_send_time=50)) == 0  # the next hourly run
    assert generate(a) == dict.fromkeys(reminders.KEYS, 0)
    assert run(a) == 0
    assert run(a, 30) == 1 and run(a, 30) == 0
    # endoflife.date moves the date three days earlier: the reminder moves, and the 60-day repeat isn't sent again.
    EolRelease.objects.filter(product_id="postgresql", name="12").update(eol_from=today(a) - timedelta(days=33))
    assert generate(a, 30)["moved"] == 1
    assert run(a, 30) == 0 and run(a, 31) == 0
    assert notices(a).count() == 2


def test_a_snooze_pauses_the_repeats_then_the_missed_one_goes_once(a):
    only_end_of_life(a)
    tracked(a, days=-30)
    generate(a)
    assert run(a) == 1
    [reminder] = eol_reminders(a)
    until = today(a) + timedelta(days=40)  # over the next mark (day 30)
    notify_services.snooze_reminder(a["org"], a["users"]["editor"], reminder, {"until": until.isoformat()})
    assert [run(a, days) for days in (30, 35, 40)] == [0, 0, 0]
    assert run(a, 41) == 1 and run(a, 42) == 0  # the day after: the 60-day repeat it missed, once
    assert run(a, 60) == 1  # then the 90-day one on its date
    assert [d.overdue_step for d in notices(a)] == [30, 60, 90]


def test_acknowledging_stops_the_repeats(a):
    only_end_of_life(a)
    tracked(a, days=-30)
    generate(a)
    assert run(a) == 1
    [reminder] = eol_reminders(a)
    notify_services.acknowledge_reminder(a["org"], a["users"]["editor"], reminder)
    assert [run(a, days) for days in (30, 60, 90)] == [0, 0, 0]
    assert notices(a).count() == 1


def test_without_the_eol_capability_nothing_repeats(a, monkeypatch):
    only_end_of_life(a)
    tracked(a, days=-30)
    generate(a)
    assert run(a) == 1
    monkeypatch.setattr(capabilities, "capabilities", lambda org: capabilities.COMMUNITY - {capabilities.EOL})
    assert generate(a, 30)["obsoleted"] == 1
    assert [run(a, days) for days in (30, 60)] == [0, 0]
    assert notices(a).count() == 1


def test_the_rule_previews_its_repeats(a):
    rule = only_end_of_life(a)
    tracked(a, days=-30)
    generate(a)
    events = rules.preview(a["org"], rule, today(a), 61)
    assert [(e["date"], e["overdue_step"]) for e in events] == [
        (today(a).isoformat(), 30),
        ((today(a) + timedelta(days=30)).isoformat(), 60),
        ((today(a) + timedelta(days=60)).isoformat(), 90),
    ]


def test_repeat_steps_merge_with_escalation_steps():
    assert [rules.current_step(n, [1, 7], 30) for n in (0, 1, 6, 7, 29, 30, 59, 60, 61)] == [
        None,
        1,
        1,
        7,
        7,
        30,
        30,
        60,
        60,
    ]
    assert [rules.next_step(n, [1, 7], 30) for n in (0, 1, 7, 29, 30, 45)] == [1, 7, 30, 30, 60, 60]
    assert rules.current_step(400, [1, 7]) == 7 and rules.next_step(400, [1, 7]) is None  # no repeat: as before


def test_repeat_every_days_is_a_validated_audited_setting(a, api):
    admin = api(a["users"]["admin"])
    rule = NotificationRule.objects.get(organization=a["org"], builtin_key="end_of_life")
    for bad in (0, 366, "30", True, 1.5):
        refused = admin.patch(f"notification-rules/{rule.id}", {"repeat_every_days": bad})
        assert refused.status_code == 400 and "repeat_every_days" in refused.json_body["errors"], bad
    changed = admin.patch(f"notification-rules/{rule.id}", {"repeat_every_days": 14})
    assert changed.status_code == 200 and changed.json_body["repeat_every_days"] == 14
    event = AuditEvent.objects.get(action="notification_rule.updated", target_id=rule.id)
    assert event.changes["fields"] == ["repeat_every_days"]
    assert event.prechange is not None and event.postchange is not None
    assert event.prechange["repeat_every_days"] == 30 and event.postchange["repeat_every_days"] == 14
    off = admin.patch(f"notification-rules/{rule.id}", {"repeat_every_days": None})
    assert off.status_code == 200 and off.json_body["repeat_every_days"] is None
    only_repeats = admin.post(
        "notification-rules",
        {"name": "Monthly nag", "source_types": ["end_of_life"], "lead_times_days": [], "repeat_every_days": 30},
    )
    assert only_repeats.status_code == 201, only_repeats.json_body
    nothing = admin.post(
        "notification-rules", {"name": "Silent", "source_types": ["end_of_life"], "lead_times_days": []}
    )
    assert nothing.status_code == 400 and "lead_times_days" in nothing.json_body["errors"]


# ---- notify 0013: the upgrade ---------------------------------------------------------------------------------------------


def _as_before_0013(org) -> NotificationRule:
    """The End of life built-in as notify 0012 left it, already baselined by its first run."""
    rule = only_end_of_life({"org": org})
    NotificationRule.objects.filter(pk=rule.pk).update(
        repeat_every_days=None,
        escalate_when_acknowledged=True,
        baselined_at=datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC")),
    )
    return rule


def test_the_migration_gives_existing_end_of_life_rules_the_repeat(a, b):
    from django.apps import apps as django_apps

    migration = importlib.import_module("notify.migrations.0013_end_of_life_repeats")
    _as_before_0013(a["org"])
    stepped = _as_before_0013(b["org"])
    NotificationRule.objects.filter(pk=stepped.pk).update(escalation_days=[7])  # an Admin's own escalation
    other = NotificationRule.objects.get(organization=a["org"], builtin_key="vendor_risk")
    with transaction.atomic():
        migration.repeat_existing(django_apps, _Editor())
    mine = NotificationRule.objects.get(organization=a["org"], builtin_key="end_of_life")
    assert mine.repeat_every_days == 30 and mine.escalate_when_acknowledged is False and mine.baselined_at is None
    theirs = NotificationRule.objects.get(pk=stepped.pk)
    assert theirs.repeat_every_days == 30 and theirs.escalate_when_acknowledged is True  # the Admin's choice stays
    other.refresh_from_db()
    assert other.repeat_every_days is None and other.baselined_at is not None  # only End of life
    NotificationRule.objects.filter(pk=mine.pk).update(
        repeat_every_days=7, baselined_at=datetime.now(tz=ZoneInfo("UTC"))
    )
    with transaction.atomic():
        migration.repeat_existing(django_apps, _Editor())  # a rerun changes nothing set since
    mine.refresh_from_db()
    assert mine.repeat_every_days == 7 and mine.baselined_at is not None


def test_the_upgrade_baselines_flag_only_versions_whose_reminders_dont_exist_yet(a):
    """Migration, then the planner's first run (baseline), then the hourly generate, then the next run: the planner
    syncs the rule's reminders before baselining, so flag-only versions (which had no reminder before notify 0013) and
    dated ones past their end of life are all baselined, and nothing bursts out."""
    from django.apps import apps as django_apps

    migration = importlib.import_module("notify.migrations.0013_end_of_life_repeats")
    _as_before_0013(a["org"])
    product("legacy", "Legacy", **{"1": {"is_eol": True}})
    for name, ago in (("Old one", 45), ("Older one", 400)):
        row = eol_services.track(a["org"], a["users"]["editor"], app(a, name), {"product": "legacy", "cycle": "1"})
        type(row).objects.filter(pk=row.pk).update(created_at=at(a, -ago))
    tracked(a, "Dated", days=-45)
    Reminder.objects.filter(organization=a["org"], source_type="end_of_life").delete()  # as before 0013's first run
    with transaction.atomic():
        migration.repeat_existing(django_apps, _Editor())
    assert rules.run_for_org(a["org"], at(a, 0, minutes_after_send_time=-30)) == 0  # baselines (before send time)
    generate(a)
    assert run(a) == 0 and rules.run_for_org(a["org"], at(a, 0, minutes_after_send_time=50)) == 0
    assert notices(a).filter(baseline=False).count() == 0
    assert notices(a).filter(baseline=True).count() == 3


def test_overdue_reminders_are_read_only_for_the_types_a_rule_escalates_or_repeats(a):
    """The End of life rule repeats; Record reviews doesn't escalate: an overdue record review isn't read every hour."""
    eol_rule = only_end_of_life(a)
    reviews = NotificationRule.objects.get(organization=a["org"], builtin_key="record_reviews")
    record = tracked(a, days=-45)
    generate(a)
    t = today(a)

    def review(due):
        return Reminder.objects.create(
            organization=a["org"],
            source_type="record_review",
            source_id=record.id,
            source_field=str(due),
            due_date=due,
            title="Billing DB",
            label="Record review due",
            resource=record,
        )

    overdue, due_today = review(t - timedelta(days=10)), review(t)
    loaded = {r.id for r in rules.live_reminders(a["org"], [eol_rule, reviews], t, t)}
    [eol] = eol_reminders(a)
    assert eol.id in loaded and due_today.id in loaded and overdue.id not in loaded
    loaded = {r.id for r in rules.live_reminders(a["org"], [reviews], t, t)}
    assert loaded == {due_today.id}


def test_the_upgrade_sends_no_burst_for_versions_already_past_end_of_life(a):
    """Versions long past end of life (and one inside a lead) on the upgrade: its first run sends nothing (each
    version's current repeat is recorded as a baseline), then each version is announced once at its own next 30-day
    mark, within 30 days, and never more than once in them."""
    from django.apps import apps as django_apps

    migration = importlib.import_module("notify.migrations.0013_end_of_life_repeats")
    _as_before_0013(a["org"])
    for name, days, cycle in (("A", -45, "10"), ("B", -100, "11"), ("C", -400, "12"), ("D", -29, "13")):
        tracked(a, name, days=days, cycle=cycle)
    tracked(a, "Soon", days=20, cycle="14")
    generate(a)
    with transaction.atomic():
        migration.repeat_existing(django_apps, _Editor())
    assert run(a) == 0  # the first run baselines
    assert rules.run_for_org(a["org"], at(a, 0, minutes_after_send_time=50)) == 0  # and the next hour sends nothing
    assert notices(a).filter(baseline=False).count() == 0
    assert sorted(notices(a).filter(baseline=True).values_list("reminder__title", flat=True)) == ["A", "B", "C", "Soon"]
    sent_on = {days: run(a, days) for days in range(1, 31)}
    sent = notices(a).filter(baseline=False)
    assert sorted((d.reminder.title, d.overdue_step or -d.lead_time_days) for d in sent) == [
        ("A", 60),  # each version once in the 30 days, at its own next mark
        ("B", 120),
        ("C", 420),
        ("D", 30),
        ("Soon", -7),  # the 7-day lead, as before
    ]
    assert {d: n for d, n in sent_on.items() if n} == {1: 1, 13: 1, 15: 1, 20: 2}


def test_a_failing_source_on_the_first_run_skips_only_that_rule(a, monkeypatch, caplog):
    """The End of life source raises while its unbaselined rule is synced: the renewals rule still sends, the End of
    life rule stays unbaselined (tried again next run), and the log names the organization and the rule by id."""
    import eol.reminders

    eol_rule = NotificationRule.objects.get(organization=a["org"], builtin_key="end_of_life")
    NotificationRule.objects.filter(organization=a["org"]).exclude(
        builtin_key__in=["end_of_life", "renewals_contracts"]
    ).update(enabled=False)
    NotificationRule.objects.filter(pk=eol_rule.pk).update(baselined_at=None)
    Reminder.objects.create(
        organization=a["org"],
        source_type="contract",
        source_id=a["org"].id,
        due_date=today(a) + timedelta(days=30),
        title="Contoso CRM",
        label="Contract cancellation deadline",
    )

    def broken(*args, **kwargs):
        raise RuntimeError("endoflife.date cache unreadable")

    monkeypatch.setattr(eol.reminders, "facts", broken)
    with caplog.at_level("ERROR", logger="softwarden.notify"):
        assert run(a) == 1
    sent = Delivery.objects.get(organization=a["org"], kind="reminder")
    assert sent.rule is not None and sent.rule.builtin_key == "renewals_contracts" and sent.lead_time_days == 30
    eol_rule.refresh_from_db()
    assert eol_rule.baselined_at is None
    [logged] = [r for r in caplog.records if "baseline failed" in r.getMessage()]
    assert str(a["org"].id) in logged.getMessage() and str(eol_rule.id) in logged.getMessage()
    monkeypatch.undo()
    assert run(a, 1) == 0  # the source is back: baselined now, nothing new owed
    eol_rule.refresh_from_db()
    assert eol_rule.baselined_at is not None
