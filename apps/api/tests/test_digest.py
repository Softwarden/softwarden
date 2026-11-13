"""Reminders and the daily digest, and the worker tick. Each digest is a
Delivery (kind digest, key digest:{user_id}:{date}) sent by the delivery engine through the organization's email
settings; these tests read the message from the delivery and its status through digest.status_of."""

from datetime import UTC, datetime, time, timedelta

import pytest
from django.core import mail
from django.core.mail.backends.base import BaseEmailBackend
from django.utils import timezone

from catalog import services
from core import worker
from core.models import AuditEvent, Membership, WorkerHeartbeat
from money import renewals
from notify import deliveries, digest, rule_services
from notify.models import (
    Delivery,
    DeliveryStatus,
    Digest,
    DigestRun,
    DigestStatus,
    NotificationPreference,
    NotificationSnooze,
    ReminderDispatch,
)


class FailingBackend(BaseEmailBackend):
    """The mail server refuses the connection (nothing sent: retried). The text names a secret, which must never
    reach the log."""

    def open(self):
        raise ConnectionRefusedError("SMTP login failed for hunter2-secret at mail.example.test")

    def send_messages(self, messages):
        raise ConnectionRefusedError("SMTP login failed for hunter2-secret at mail.example.test")


@pytest.fixture(autouse=True)
def email_on(settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.SOFTWARDEN_BASE_URL = "https://softwarden.example.test"


@pytest.fixture
def org(a):
    """Digest time midnight, so a run is always due; the fixture app's ownership gaps go to the editor (steward)."""
    a["org"].digest_time = time(0, 0)
    a["org"].save()
    a["users"]["editor"].first_name = "Sam"
    a["users"]["editor"].save()
    return a


def renewal(f, name, days, **kw):
    today = services.org_today(f["org"])
    data = {
        "name": name,
        "auto_renew": "yes",
        "explicit_deadline": (today + timedelta(days=days)).isoformat(),
        "renewal_date": (today + timedelta(days=days + 60)).isoformat(),
        "responsible": str(f["people"]["dana"].id),
        "followup": f["users"]["editor"].id,
        "applications": [str(f["app"].id)],
        **kw,
    }
    return renewals.create_renewal(f["org"], f["users"]["editor"], data)


def run(f, days_later=0):
    """Build the digest as at `days_later` and send its deliveries (the worker's jobs, done here in process)."""
    now = timezone.now() + timedelta(days=days_later)
    built = digest.run_for_org(f["org"], now)
    if built is not None:
        digest.send_now(f["org"], built.for_date, now + timedelta(seconds=1))
    return built


def dv(d) -> Delivery:
    """The digest's delivery (always set for a built digest)."""
    assert d.delivery is not None
    return d.delivery


def text(d):
    return dv(d).content["text"]


def mail_for(user):
    return [m for m in mail.outbox if m.to == [user.email]]


def test_one_digest_per_user_with_items(org):
    renewal(org, "Workday subscription", 30)  # the 30-day threshold fires today
    built = run(org)
    assert built is not None
    sent = mail_for(org["users"]["editor"])
    assert len(sent) == 1
    message = sent[0]
    # the app is also unreviewed, and fails five data-quality checks (one Data quality row per rule,
    # "Not on a contract" included)
    assert message.subject.startswith("Softwarden: 8 items need attention (")
    assert "Hi Sam," in message.body
    assert "RENEWAL ACTIONS (1)" in message.body and "OWNERSHIP GAPS (1)" in message.body
    assert "REVIEWS (1)" in message.body and "orga App — Unreviewed: Created" in message.body
    assert "DATA QUALITY (5)" in message.body and "No criticality — 1 record" in message.body
    assert "https://softwarden.example.test/data-quality?rule=criticality&it_steward=" in message.body
    assert "Workday subscription — action deadline" in message.body and "(Entered) — in 30 days" in message.body
    assert "https://softwarden.example.test/renewals/" in message.body
    html, kind = message.alternatives[0]
    assert kind == "text/html" and "<h2" in html and "Renewal actions (1)</h2>" in html
    d = Digest.objects.select_related("delivery").get(user=org["users"]["editor"])
    assert digest.status_of(d) == DigestStatus.SENT and dv(d).attempts == 1 and d.item_count == 8
    # The digest is a delivery: kind digest, one key per user and day, the Daily digest rule, in the log.
    assert dv(d).kind == "digest" and dv(d).channel == "email"
    assert dv(d).dedupe_key == f"digest:{org['users']['editor'].id}:{d.for_date.isoformat()}"
    assert dv(d).rule == rule_services.digest_rule(org["org"]) and dv(d).rule_name == "Daily digest"
    assert dv(d).subject == message.subject and dv(d).recipient == org["users"]["editor"].email
    assert mail_for(org["users"]["viewer"]) == []  # nothing routed to the viewer: no email at all


def test_not_before_the_digest_time_and_once_a_day(org):
    org["org"].digest_time = time(23, 59, 59)
    org["org"].timezone = "UTC"
    org["org"].save()
    early = datetime.combine(services.org_today(org["org"]), time(8, 0), tzinfo=UTC)
    assert digest.run_for_org(org["org"], early) is None
    late = early.replace(hour=23, minute=59, second=59, microsecond=1)
    assert digest.run_for_org(org["org"], late) is not None
    assert digest.run_for_org(org["org"], late) is None
    assert DigestRun.objects.count() == 1 and Digest.objects.count() == 1


def test_digest_date_is_organization_local(org):
    org["org"].timezone = "Pacific/Auckland"
    org["org"].digest_time = time(8, 0)
    org["org"].save()
    now = datetime(2026, 11, 3, 20, 30, tzinfo=UTC)  # 09:30 on 4 November in Auckland
    built = digest.run_for_org(org["org"], now)
    assert built is not None and built.for_date.isoformat() == "2026-11-04"


def test_reminder_fires_once_per_threshold_for_a_multi_application_obligation(org):
    second = services.create_resource(org["org"], org["users"]["editor"], {"name": "Benefits", "lifecycle": "active"})
    renewal(org, "Workday subscription", 30, applications=[str(org["app"].id), str(second.id)])
    run(org)
    assert ReminderDispatch.objects.count() == 1
    dispatch = ReminderDispatch.objects.get()
    assert dispatch.threshold_days == 30 and dispatch.digest is not None
    run(org, days_later=1)  # 29 days left: the 30-day threshold has fired, 7 hasn't come
    tomorrow = Digest.objects.get(for_date=services.org_today(org["org"]) + timedelta(days=1))
    assert "RENEWAL ACTIONS" not in text(tomorrow)
    assert ReminderDispatch.objects.count() == 1


def test_late_created_obligation_skips_passed_thresholds(org):
    renewal(org, "Late", 40)  # 90 and 60 passed before it existed
    run(org)
    assert ReminderDispatch.objects.count() == 0
    assert "RENEWAL ACTIONS" not in text(Digest.objects.get())


def test_overdue_actions_appear_every_day(org):
    renewal(org, "Zoom Pro", -2)
    run(org)
    run(org, days_later=1)
    bodies = [text(d) for d in Digest.objects.order_by("for_date")]
    assert len(bodies) == 2 and all("Zoom Pro — action deadline" in b for b in bodies)
    assert "Overdue by 3 days" in bodies[1]
    assert ReminderDispatch.objects.count() == 0  # overdue is not a threshold


def test_unassigned_items_go_to_the_coordinator(org):
    renewal(org, "Nobody's", 30, followup=None)
    run(org)
    coordinator = mail_for(org["users"]["admin"])
    assert len(coordinator) == 1
    body = coordinator[0].body
    assert "UNASSIGNED (" in body and "Renewal actions: Nobody's — action deadline" in body
    assert "Renewal gaps: Nobody's — No IT follow-up" in body


def test_no_coordinator_means_nobody_gets_unrouted_items(org):
    org["org"].coordinator = None
    org["org"].save()
    renewal(org, "Nobody's", 30, followup=None)
    run(org)
    assert mail_for(org["users"]["admin"]) == []


def test_people_without_accounts_are_never_emailed(org):
    org["people"]["dana"].email = "dana@example.test"
    org["people"]["dana"].save()
    renewal(org, "Workday subscription", 30)
    run(org)
    assert all(m.to != ["dana@example.test"] for m in mail.outbox)
    users = {u.email for u in org["users"].values()}
    assert {m.to[0] for m in mail.outbox} <= users


def test_digest_off_and_inactive_users_get_nothing(org):
    editor = Membership.objects.get(user=org["users"]["editor"], organization=org["org"])
    NotificationPreference.objects.create(membership=editor, organization=org["org"], digest_enabled=False)
    renewal(org, "Workday subscription", 30)
    run(org)
    assert mail_for(org["users"]["editor"]) == []
    assert ReminderDispatch.objects.count() == 0  # nobody was told, so the threshold hasn't fired


def test_email_not_configured_records_digests_as_not_sent(org, settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = False
    run(org)
    d = Digest.objects.select_related("delivery").get()
    assert digest.status_of(d) == DigestStatus.SKIPPED and dv(d).last_error.startswith("Email isn't configured")
    assert mail.outbox == []


def test_one_delivery_per_user_per_day_and_a_rerun_is_a_no_op(org):
    """The run is once a day, and the delivery's key is too: building the day again (a second worker that missed the
    run's row, a replaced run) never queues a second email."""
    renewal(org, "Workday subscription", 30)
    run(org)
    first = Delivery.objects.get(kind="digest")
    assert digest.run_for_org(org["org"], timezone.now()) is None  # the run's row: nothing to do
    DigestRun.objects.all().delete()  # even with the run's row gone, the delivery key holds
    run(org)
    assert Delivery.objects.filter(kind="digest").count() == 1 and len(mail_for(org["users"]["editor"])) == 1
    assert dv(Digest.objects.select_related("delivery").get()) == first


def test_the_daily_digest_rule_turns_digests_off(org, api):
    rule = rule_services.digest_rule(org["org"])
    assert rule is not None and rule.is_builtin and rule.enabled
    admin = api(org["users"]["admin"])
    assert admin.patch(f"notification-rules/{rule.id}", {"enabled": False}).status_code == 200
    assert run(org) is None and not Digest.objects.exists() and mail.outbox == []
    admin.patch(f"notification-rules/{rule.id}", {"enabled": True})
    assert run(org) is not None and mail.outbox


def test_the_daily_digest_rule_sets_the_digest_time(org, api):
    """The rule's time is the organization's digest time (one column, also set in organization settings)."""
    rule = rule_services.digest_rule(org["org"])
    assert rule is not None
    admin = api(org["users"]["admin"])
    changed = admin.patch(f"notification-rules/{rule.id}", {"digest_time": "23:59"})
    assert changed.status_code == 200 and changed.json_body["digest_time"] == "23:59"
    org["org"].refresh_from_db()
    assert org["org"].digest_time == time(23, 59)
    assert admin.get("organization").json_body["digest_time"] == "23:59"
    assert AuditEvent.objects.filter(action="organization.updated", changes__digest_time=["00:00", "23:59"]).exists()
    refused = admin.patch(f"notification-rules/{rule.id}", {"lead_times_days": [7]})
    assert refused.status_code == 400 and "lead_times_days" in refused.json_body["errors"]


def test_failures_are_retried_then_left_failed_with_a_redacted_error(org, settings):
    """Through the delivery engine: retried after about 1, 4 and 16 minutes, then Failed; the error never carries a
    secret from the server's words."""
    settings.EMAIL_BACKEND = "tests.test_digest.FailingBackend"
    settings.EMAIL_HOST_PASSWORD = "hunter2-secret"
    now = timezone.now()
    built = digest.run_for_org(org["org"], now)
    assert built is not None
    expected_gaps = [1, 4, 16]
    for attempt, wait in enumerate(expected_gaps, start=1):
        before = timezone.now()
        digest.send_now(org["org"], built.for_date)
        d = Digest.objects.select_related("delivery").get()
        assert digest.status_of(d) == DigestStatus.PENDING and dv(d).attempts == attempt
        assert "hunter2-secret" not in dv(d).last_error
        due = dv(d).next_attempt_at
        assert due is not None
        assert timedelta(minutes=wait) - timedelta(seconds=5) < due - before <= timedelta(minutes=wait, seconds=5)
        Delivery.objects.filter(id=dv(d).id).update(next_attempt_at=now)
    digest.send_now(org["org"], built.for_date)
    d = Digest.objects.select_related("delivery").get()
    assert digest.status_of(d) == DigestStatus.FAILED and dv(d).attempts == 4
    assert dv(d).next_attempt_at is None and "Gave up after 4 attempts" in dv(d).last_error
    assert "hunter2-secret" not in dv(d).last_error
    assert digest.digest_problem(org["org"]) == {"for_date": built.for_date.isoformat(), "failed": 1}


def test_admin_retry_sends_a_failed_digest(org, settings):
    settings.EMAIL_BACKEND = "tests.test_digest.FailingBackend"
    built = digest.run_for_org(org["org"], timezone.now())
    assert built is not None
    digest.send_now(org["org"], built.for_date, timezone.now() + timedelta(days=1), rounds=deliveries.MAX_ATTEMPTS)
    d = Digest.objects.select_related("delivery").get()
    assert digest.status_of(d) == DigestStatus.FAILED
    failed = dv(d)
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    retried = digest.retry(org["org"], org["users"]["admin"], d)
    assert digest.status_of(retried) == DigestStatus.SENT and len(mail.outbox) == 1
    assert dv(retried).resend_of == failed and dv(retried).content == failed.content
    assert mail.outbox[0].body == failed.content["text"]  # the same email as built
    event = AuditEvent.objects.get(action="digest.retried")
    assert event.changes["sent"] is True and event.changes["delivery"] == str(dv(retried).id)


def test_admin_retry_of_a_pending_digest_sends_it_now(org):
    built = digest.run_for_org(org["org"], timezone.now())
    assert built is not None
    d = Digest.objects.select_related("delivery").get()
    Delivery.objects.filter(id=dv(d).id).update(next_attempt_at=timezone.now() + timedelta(hours=1))
    retried = digest.retry(org["org"], org["users"]["admin"], d)
    assert digest.status_of(retried) == DigestStatus.SENT and dv(retried) == dv(d)
    assert len(mail.outbox) == 1


def test_snoozed_items_are_left_out_of_that_users_digest(org):
    r = renewal(org, "Workday subscription", -1)
    today = services.org_today(org["org"])
    NotificationSnooze.objects.create(
        organization=org["org"],
        user=org["users"]["editor"],
        subject_type="renewal_actions",
        subject_id=str(r.id),
        kind="snooze",
        until=today + timedelta(days=2),
    )
    run(org)
    assert "Workday subscription — action deadline" not in text(Digest.objects.get())
    run(org, days_later=3)  # the snooze has ended
    latest = Digest.objects.order_by("-for_date").first()
    assert latest is not None
    assert "Workday subscription — action deadline" in text(latest)


def test_acknowledgment_holds_until_the_next_threshold(org):
    r = renewal(org, "Workday subscription", 30)
    NotificationSnooze.objects.create(
        organization=org["org"],
        user=org["users"]["editor"],
        subject_type="renewal_actions",
        subject_id=str(r.id),
        kind="acknowledge",
        threshold=30,
    )
    run(org)
    assert "RENEWAL ACTIONS" not in text(Digest.objects.get())
    assert ReminderDispatch.objects.count() == 0
    run(org, days_later=23)  # 7 days left: the next threshold, so the acknowledgment no longer holds
    latest = Digest.objects.order_by("-for_date").first()
    assert latest is not None
    assert "Workday subscription — action deadline" in text(latest)


def test_other_organizations_items_never_appear(org, b):
    renewal(b, "Their renewal", 30)
    b["org"].digest_time = time(0, 0)
    b["org"].save()
    run(org)
    run(b)
    for m in mail_for(org["users"]["editor"]):
        assert "Their renewal" not in m.body and "orgb" not in m.body
    theirs = mail_for(b["users"]["editor"])
    assert len(theirs) == 1 and "orga" not in theirs[0].body


# Pinned, never the real clock: from 23:59 UTC the second tick, a minute later, is the next local
# day, whose digest is rightly due, so the test checked "today's run is done" only before then.
@pytest.mark.parametrize(
    "clock", ["00:00:00", "02:30:00", "04:45:00", "07:59:59", "12:00:00", "23:58:59"], indirect=True
)
def test_worker_tick_records_a_heartbeat_and_runs_digests(org, clock):
    summary = worker.tick()
    # The digest is built by the schedule and sent by its notify.deliver job in the same tick.
    assert summary is not None and summary["runs"] >= 1 and summary["jobs"].get("succeeded", 0) >= 1
    assert Delivery.objects.filter(kind="digest", status=DeliveryStatus.SENT).exists() and mail.outbox
    assert WorkerHeartbeat.objects.get(name="worker").last_tick is not None
    # The next tick (a minute later: every-minute schedules fire once per minute) finds today's run done.
    assert worker.tick(timezone.now() + timedelta(minutes=1))["runs"] == 0
    assert worker.heartbeat_age() is not None and worker.heartbeat_age() < 60


def test_readiness_reports_the_worker(org, client):
    assert client.get("/readyz").json()["checks"]["worker"] == "not running"
    worker.tick()
    assert client.get("/readyz").json()["checks"]["worker"] == "ok"


def test_renewal_page_shows_which_reminders_fired(org, api):
    r = renewal(org, "Workday subscription", 30)
    run(org)
    body = api(org["users"]["editor"]).get(f"renewals/{r.id}").json_body
    reminders = {x["days"]: x for x in body["cycle"]["reminders"]}
    today = services.org_today(org["org"])
    assert reminders[30] == {"days": 30, "on": today.isoformat(), "status": "sent", "sent_on": today.isoformat()}
    assert reminders[7]["status"] == "not_sent" and reminders[7]["on"] == (today + timedelta(days=23)).isoformat()
    DigestRun.objects.all().delete()  # the digest row gone: the reminder still says when it fired
    again = api(org["users"]["editor"]).get(f"renewals/{r.id}").json_body
    assert {x["days"]: x for x in again["cycle"]["reminders"]}[30]["sent_on"] == today.isoformat()


def test_the_renewal_page_reads_a_digests_status_as_the_digest_log_does(org, api):
    """One reading (digest.status_word): a digest whose delivery is gone reads failed on the renewal page too; a
    threshold whose digest is gone altogether still reads sent on the day it fired."""
    r = renewal(org, "Workday subscription", 30)
    run(org)
    Digest.objects.update(delivery=None)
    reminders_shown = api(org["users"]["editor"]).get(f"renewals/{r.id}").json_body["cycle"]["reminders"]
    assert {x["days"]: x for x in reminders_shown}[30]["status"] == "failed"
    assert digest.status_of(Digest.objects.get()) == DigestStatus.FAILED
