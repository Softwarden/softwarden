"""Renewal settings, the digest preference, snooze and acknowledge, and the delivery log
with Retry now and the Admin banner."""

from datetime import time, timedelta

import pytest
from django.core import mail
from django.utils import timezone

from catalog import services
from core.models import AuditEvent
from money import renewals
from money.models import RenewalCycle, RenewalObligation
from notify import digest
from notify.models import Delivery, DeliveryStatus, Digest, NotificationSnooze


@pytest.fixture(autouse=True)
def email_on(settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


def renewal(f, name, days):
    today = services.org_today(f["org"])
    return renewals.create_renewal(
        f["org"],
        f["users"]["editor"],
        {
            "name": name,
            "auto_renew": "yes",
            "explicit_deadline": (today + timedelta(days=days)).isoformat(),
            "followup": f["users"]["editor"].id,
        },
    )


# ---- organization settings -------------------------------------------------------------------


def test_renewal_settings(a, api):
    admin = api(a["users"]["admin"])
    body = admin.get("organization").json_body
    assert body["renewal_lead_days"] == 90 and body["reminder_thresholds"] == [90, 60, 30, 7]
    assert body["digest_time"] == "08:00"
    assert body["email"] == {"configured": True, "last_run": None}
    changed = admin.patch(
        "organization", {"renewal_lead_days": 60, "reminder_thresholds": [60, 14, 3], "digest_time": "07:30"}
    ).json_body
    assert changed["renewal_lead_days"] == 60 and changed["reminder_thresholds"] == [60, 14, 3]
    assert changed["digest_time"] == "07:30"


@pytest.mark.parametrize(
    "field,value",
    [
        ("renewal_lead_days", 0),
        ("renewal_lead_days", 400),
        ("reminder_thresholds", [7, 30]),
        ("reminder_thresholds", [30, 30]),
        ("reminder_thresholds", []),
        ("reminder_thresholds", [500]),
        ("reminder_thresholds", ["30"]),
        ("reminder_thresholds", "90,60"),
        ("digest_time", "8am"),
    ],
)
def test_renewal_settings_validation(a, api, field, value):
    response = api(a["users"]["admin"]).patch("organization", {field: value})
    assert response.status_code == 400 and field in response.json_body["errors"]


def test_lead_time_changes_assumed_deadlines(a, api):
    r = renewals.create_renewal(
        a["org"], a["users"]["editor"], {"name": "X", "auto_renew": "yes", "renewal_date": "2027-03-31"}
    )
    api(a["users"]["admin"]).patch("organization", {"renewal_lead_days": 30})
    assert api(a["users"]["editor"]).get(f"renewals/{r.id}").json_body["deadline"]["date"] == "2027-03-01"


def test_email_status_reports_the_last_run(a, api):
    a["org"].digest_time = time(0, 0)
    a["org"].save()
    digest.run_for_org(a["org"], timezone.now())
    digest.send_now(a["org"], services.org_today(a["org"]))
    last = api(a["users"]["admin"]).get("organization").json_body["email"]["last_run"]
    assert last["sent"] == 1 and last["failed"] == 0 and last["for_date"] == services.org_today(a["org"]).isoformat()


# ---- preference ------------------------------------------------------------------------------


def test_digest_preference_is_your_own(a, api):
    client = api(a["users"]["viewer"])
    shown = client.get("auth/me/preferences").json_body
    assert shown["digest_enabled"] is True and shown["digest_time"] == "08:00"
    assert client.patch("auth/me/preferences", {"digest_enabled": False}).json_body["digest_enabled"] is False
    assert api(a["users"]["editor"]).get("auth/me/preferences").json_body["digest_enabled"] is True
    assert client.patch("auth/me/preferences", {"digest_enabled": "no"}).status_code == 400
    assert AuditEvent.objects.filter(action="preference.updated").count() == 1


# ---- snooze and acknowledge ------------------------------------------------------------------


def test_snooze_marks_the_row_and_keeps_it_visible(a, api):
    r = renewal(a, "Zoom Pro", -3)
    client = api(a["users"]["editor"])
    until = (services.org_today(a["org"]) + timedelta(days=4)).isoformat()
    made = client.post(
        "needs-attention/snoozes",
        {"subject_type": "renewal_actions", "subject_id": str(r.id), "kind": "snooze", "until": until},
    )
    assert made.status_code == 201, made.json_body
    body = client.get("needs-attention").json_body
    row = body["renewal_actions"][0]
    assert row["name"] == "Zoom Pro" and row["snooze"]["until"] == until  # still visible, marked
    # Someone else doesn't see your snooze.
    assert api(a["users"]["admin"]).get("needs-attention?scope=all").json_body["renewal_actions"][0]["snooze"] is None
    # The obligation is unchanged.
    assert RenewalCycle.objects.get(obligation=r).completed_on is None
    assert RenewalObligation.objects.get(id=r.id).archived_at is None
    assert client.delete(f"needs-attention/snoozes/{made.json_body['id']}").status_code == 204
    assert client.get("needs-attention").json_body["renewal_actions"][0]["snooze"] is None


def test_a_members_snoozes_are_theirs_alone(a, api):
    """The snooze list and reads are filtered to the signed-in member (notify/viewsets.SnoozeViewSet); another
    member of the same organization, an Admin included, can't list, read or undo them."""
    r = renewal(a, "Zoom Pro", -3)
    editor, admin = api(a["users"]["editor"]), api(a["users"]["admin"])
    until = (services.org_today(a["org"]) + timedelta(days=4)).isoformat()
    made = editor.post(
        "needs-attention/snoozes",
        {"subject_type": "renewal_actions", "subject_id": str(r.id), "kind": "snooze", "until": until},
    )
    assert made.status_code == 201, made.json_body
    assert [s["id"] for s in editor.get("needs-attention/snoozes").json_body] == [made.json_body["id"]]
    assert admin.get("needs-attention/snoozes").json_body == []
    assert admin.get(f"needs-attention/snoozes/{made.json_body['id']}").status_code == 404
    assert admin.delete(f"needs-attention/snoozes/{made.json_body['id']}").status_code == 404
    assert NotificationSnooze.objects.filter(pk=made.json_body["id"]).exists()


def test_snooze_any_item_and_link_subjects(a, api):
    client = api(a["users"]["editor"])
    until = (services.org_today(a["org"]) + timedelta(days=1)).isoformat()
    made = client.post(
        "needs-attention/snoozes",
        {"subject_type": "ownership_gaps", "subject_id": str(a["app"].id), "kind": "snooze", "until": until},
    )
    assert made.status_code == 201
    assert client.get("needs-attention").json_body["ownership_gaps"][0]["snooze"]["kind"] == "snooze"


def test_snooze_validation(a, api):
    r = renewal(a, "Zoom Pro", 10)
    client = api(a["users"]["editor"])
    today = services.org_today(a["org"])

    def post(**kw):
        return client.post(
            "needs-attention/snoozes",
            {"subject_type": "renewal_actions", "subject_id": str(r.id), "kind": "snooze", **kw},
        )

    assert "until" in post(until=today.isoformat()).json_body["errors"]
    assert "until" in post(until=(today + timedelta(days=91)).isoformat()).json_body["errors"]
    assert "until" in post(until="soon").json_body["errors"]
    assert "kind" in post(kind="mute").json_body["errors"]
    gone = client.post(
        "needs-attention/snoozes",
        {"subject_type": "renewal_actions", "subject_id": str(a["app"].id), "kind": "snooze", "until": "2030-01-01"},
    )
    assert gone.status_code == 400
    refused = client.post(
        "needs-attention/snoozes",
        {"subject_type": "ownership_gaps", "subject_id": str(a["app"].id), "kind": "acknowledge"},
    )
    assert refused.json_body["errors"]["kind"] == ["Only renewal actions can be acknowledged; snooze other items."]
    assert api(a["users"]["viewer"]).post("needs-attention/snoozes", {}).status_code == 403


def test_acknowledge_records_the_current_threshold(a, api):
    r = renewal(a, "Workday", 25)
    client = api(a["users"]["editor"])
    made = client.post(
        "needs-attention/snoozes", {"subject_type": "renewal_actions", "subject_id": str(r.id), "kind": "acknowledge"}
    ).json_body
    assert made["kind"] == "acknowledge" and made["threshold"] == 30 and made["until"] is None
    event = AuditEvent.objects.get(organization=a["org"], action="notification.acknowledged")
    assert event.actor == a["users"]["editor"] and event.changes["subject_id"] == str(r.id)
    overdue = renewal(a, "Late", -1)
    late = client.post(
        "needs-attention/snoozes",
        {"subject_type": "renewal_actions", "subject_id": str(overdue.id), "kind": "acknowledge"},
    ).json_body
    assert late["threshold"] == -1 and late["until"] == services.org_today(a["org"]).isoformat()


def test_you_can_only_undo_your_own(a, api):
    r = renewal(a, "Zoom Pro", 10)
    made = api(a["users"]["editor"]).post(
        "needs-attention/snoozes",
        {
            "subject_type": "renewal_actions",
            "subject_id": str(r.id),
            "kind": "snooze",
            "until": (services.org_today(a["org"]) + timedelta(days=2)).isoformat(),
        },
    )
    assert api(a["users"]["admin"]).delete(f"needs-attention/snoozes/{made.json_body['id']}").status_code == 404
    assert NotificationSnooze.objects.count() == 1


def test_other_organizations_items_cant_be_snoozed(a, b, api):
    theirs = renewal(b, "Theirs", 10)
    response = api(a["users"]["editor"]).post(
        "needs-attention/snoozes",
        {"subject_type": "renewal_actions", "subject_id": str(theirs.id), "kind": "snooze", "until": "2030-01-01"},
    )
    assert response.status_code == 400


# ---- delivery log ----------------------------------------------------------------------------


def failed_digest(f, settings):
    settings.EMAIL_BACKEND = "tests.test_digest.FailingBackend"
    f["org"].digest_time = time(0, 0)
    f["org"].save()
    digest.run_for_org(f["org"], timezone.now())
    d = Digest.objects.select_related("delivery").get(organization=f["org"])
    assert d.delivery is not None
    Delivery.objects.filter(id=d.delivery.id).update(
        status=DeliveryStatus.FAILED, attempts=4, next_attempt_at=None, last_error="Connection refused"
    )
    return d


def test_delivery_log_banner_and_retry(a, b, api, settings):
    d = failed_digest(a, settings)
    admin = api(a["users"]["admin"])
    log = admin.get("admin/digests").json_body
    assert len(log) == 1 and log[0]["status"] == "failed" and log[0]["last_error"] == "Connection refused"
    assert admin.get("admin/digests?status=sent").json_body == []
    assert admin.get("admin/digests?status=nope").status_code == 400
    assert admin.get("auth/me").json_body["digest_problem"] == {"for_date": d.for_date.isoformat(), "failed": 1}
    assert api(a["users"]["editor"]).get("auth/me").json_body["digest_problem"] is None  # Admins only
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    retried = admin.post(f"admin/digests/{d.id}/retry").json_body
    assert retried["status"] == "sent" and len(mail.outbox) == 1
    # The resend is the digest's delivery now, and the failed one stays in the notification log.
    resent = Delivery.objects.get(id=retried["delivery"])
    assert resent.resend_of_id == d.delivery_id and resent.kind == "digest" and resent.requested_by_id is not None
    assert admin.get("auth/me").json_body["digest_problem"] is None
    assert AuditEvent.objects.filter(action="digest.retried").count() == 1
    # Another organization's digest is a 404, and the log never lists it.
    theirs = failed_digest(b, settings)
    assert admin.post(f"admin/digests/{theirs.id}/retry").status_code == 404
    assert {x["id"] for x in admin.get("admin/digests").json_body} == {str(d.id)}


def test_retry_when_email_is_not_configured(a, api, settings):
    d = failed_digest(a, settings)
    settings.SOFTWARDEN_EMAIL_CONFIGURED = False
    response = api(a["users"]["admin"]).post(f"admin/digests/{d.id}/retry")
    assert response.status_code == 409 and "Email isn't configured" in response.json_body["detail"]


def test_smtp_down_leaves_every_action_in_needs_attention(a, api, settings):
    renewal(a, "Zoom Pro", -1)
    failed_digest(a, settings)
    body = api(a["users"]["editor"]).get("needs-attention").json_body
    assert [r["name"] for r in body["renewal_actions"]] == ["Zoom Pro"]


def test_e2e_endpoint_does_not_exist_outside_the_e2e_suite(a, api):
    response = api(a["users"]["admin"]).client.post("/api/v1/e2e/run-digest")
    assert response.status_code == 404
