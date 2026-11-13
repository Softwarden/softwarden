"""The vendor risk file (catalog/vendor_risk.py, catalog/vendor_risk_viewsets.py): the
risk profile and documents, derived status at its boundaries, superseded documents, gaps, the `vendor_risk` reminder
source (created, moved, closed, never duplicated), the Needs attention queue, the data-quality rules, the API's roles,
filters, ETags and CSV exports, the vendor detail's summary, search, the capability and tenancy."""

import csv
import io
from datetime import timedelta

import pytest
from django.db import connection, transaction

from catalog import admin_services, quality, services
from catalog import vendor_risk as risk
from catalog.models import Attachment, Vendor, VendorRiskDocument, VendorRiskProfile
from catalog.vendor_risk_viewsets import absent_revision
from core.models import AuditEvent, CapabilityOverride
from core.problems import FieldErrors
from core.request_context import forget_organization
from notify import digest, reminders, rule_services, rules
from notify.attention import UNCOUNTED, attention_queues
from notify.models import NotificationRule, Reminder

pytestmark = pytest.mark.django_db


def today(f):
    return services.org_today(f["org"])


def iso(f, days):
    return (today(f) + timedelta(days=days)).isoformat()


def vendor(f, name="Fabrikam"):
    return Vendor.objects.get_or_create(organization=f["org"], name=name)[0]


def link(f, v, title="SOC 2 report.pdf"):
    return Attachment.objects.create(
        organization=f["org"], vendor=v, kind="link", title=title, url="https://trust.example.test/soc2"
    )


def profile(f, v, **data):
    return risk.update_profile(f["org"], f["users"]["editor"], v, data)


def document(f, v, **data):
    return risk.create_document(f["org"], f["users"]["editor"], v, {"kind": "soc2", **data})


def live(org, **filters):
    return Reminder.objects.filter(organization=org, status__in=["open", "snoozed", "acknowledged"], **filters)


def revoke(org):
    CapabilityOverride.objects.create(organization=org, capability="vendor_risk", effect="revoke", reason="test")
    forget_organization(org.pk)


def patch_if_match(f, role, url, data, tag):
    import json

    from django.test import Client

    client = Client()
    client.force_login(f["users"][role])
    return client.patch("/api/v1/" + url, data=json.dumps(data), content_type="application/json", HTTP_IF_MATCH=tag)


def rows_of(response):
    text = response.content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


# ---- derived status --------------------------------------------------------------------------------------------------


def test_status_boundaries_follow_the_largest_reminder_threshold(a):
    day = today(a)
    days = risk.horizon(a["org"])
    assert days == 90  # the default thresholds 90, 60, 30, 7
    assert risk.status_of(None, day, days) == "current"  # undated
    assert risk.status_of(day - timedelta(days=1), day, days) == "expired"
    assert risk.status_of(day, day, days) == "expiring"  # the expiry day itself is still valid
    assert risk.status_of(day + timedelta(days=90), day, days) == "expiring"
    assert risk.status_of(day + timedelta(days=91), day, days) == "current"
    a["org"].reminder_thresholds = [30, 7]
    assert risk.horizon(a["org"]) == 30
    assert risk.status_of(day + timedelta(days=31), day, 30) == "current"


def test_a_document_reads_its_status_and_days_left(a, api):
    v = vendor(a)
    expired = document(a, v, expires_on=iso(a, -1))
    expiring = document(a, v, kind="pen_test", expires_on=iso(a, 0))
    current = document(a, v, kind="insurance", expires_on=iso(a, 91))
    undated = document(a, v, kind="other", title="Architecture letter")
    body = api(a["users"]["viewer"]).get(f"vendors/{v.id}/risk-documents").json_body
    found = {r["id"]: r for r in body}
    assert found[str(expired.id)]["status"] == "expired" and found[str(expired.id)]["days_left"] == -1
    assert found[str(expiring.id)]["status"] == "expiring" and found[str(expiring.id)]["days_left"] == 0
    assert found[str(current.id)]["status"] == "current" and found[str(current.id)]["days_left"] == 91
    assert found[str(undated.id)]["status"] == "current" and found[str(undated.id)]["days_left"] is None
    assert [r["id"] for r in body][-1] == str(undated.id)  # the soonest expiry first, undated last


def test_the_assurance_expiry_is_worked_out_and_never_sent(a):
    v = vendor(a)
    p = profile(a, v, assurance_type="soc2_type2", assurance_issued_on="2026-01-31", assurance_validity_months=1)
    assert p.assurance_expires_on.isoformat() == "2026-02-28"  # month-end clamping (core/dates.add_months)
    p = profile(a, v, assurance_validity_months=12)
    assert p.assurance_expires_on.isoformat() == "2027-01-31"
    with pytest.raises(FieldErrors) as refused:
        profile(a, v, assurance_expires_on="2030-01-01")
    assert "assurance_expires_on" in refused.value.errors
    p = profile(a, v, assurance_issued_on=None)
    assert p.assurance_expires_on is None
    assert risk.assurance_status(p, today(a), 90) == "missing"


def test_a_document_expiry_comes_from_a_date_or_a_validity_never_both(a):
    v = vendor(a)
    doc = document(a, v, issued_on="2026-03-31", validity_months=11)
    assert doc.expires_on.isoformat() == "2027-02-28"
    with pytest.raises(FieldErrors) as both:
        document(a, v, issued_on="2026-01-01", validity_months=12, expires_on="2027-06-01")
    assert "expires_on" in both.value.errors
    with pytest.raises(FieldErrors) as no_issue:
        document(a, v, validity_months=12)
    assert "issued_on" in no_issue.value.errors
    with pytest.raises(FieldErrors) as backwards:
        document(a, v, issued_on="2026-06-01", expires_on="2026-05-01")
    assert "expires_on" in backwards.value.errors
    # Clearing the validity keeps the date it gave, now entered; a new issue date with a validity recomputes it.
    doc = risk.update_document(a["org"], a["users"]["editor"], doc, {"validity_months": None})
    assert str(doc.expires_on) == "2027-02-28" and doc.validity_months is None
    doc = risk.update_document(a["org"], a["users"]["editor"], doc, {"validity_months": 6, "issued_on": "2026-05-01"})
    assert str(doc.expires_on) == "2026-11-01"
    with pytest.raises(FieldErrors) as kind:
        risk.create_document(a["org"], a["users"]["editor"], v, {"title": "No kind"})
    assert "kind" in kind.value.errors


def test_profile_validation_names_each_field(a, b):
    v = vendor(a)
    theirs = link(b, vendor(b))
    another = link(a, vendor(a, "Contoso"))
    with pytest.raises(FieldErrors) as refused:
        profile(
            a,
            v,
            risk_tier="extreme",
            processes_personal_data="yes",
            questionnaire_reviewed_on="31/12/2026",
            dpa_attachment=str(theirs.id),
            assurance_attachment=str(another.id),
            subprocessor_url="ftp://example.test/list",
            assurance_validity_months=0,
        )
    assert set(refused.value.errors) == {
        "risk_tier",
        "processes_personal_data",
        "questionnaire_reviewed_on",
        "dpa_attachment",
        "assurance_attachment",
        "subprocessor_url",
        "assurance_validity_months",
    }
    assert not VendorRiskProfile.objects.exists()  # nothing was written


def test_a_questionnaire_status_dates_itself_when_no_date_is_sent(a):
    v = vendor(a)
    p = profile(a, v, questionnaire_status="requested")
    assert p.questionnaire_requested_on == today(a)
    p = profile(a, v, questionnaire_status="reviewed")
    assert p.questionnaire_reviewed_on == today(a)
    with pytest.raises(FieldErrors):
        profile(a, v, questionnaire_reviewed_on=iso(a, -10))  # before the request


# ---- audit -----------------------------------------------------------------------------------------------------------


def test_writes_are_audited_with_snapshots_under_the_vendor(a, api):
    v = vendor(a)
    attachment = link(a, v)
    profile(a, v, risk_tier="high", notes="Board asked for it")
    created = AuditEvent.objects.get(action="vendor_risk.created")
    assert created.changes["risk_tier"] == "high" and created.related_target_type == "Vendor"
    assert created.related_target_id == v.id and created.prechange is None
    assert (created.postchange or {})["vendor"]["display"] == "Fabrikam"
    profile(a, v, risk_tier="critical", assurance_attachment=str(attachment.id))
    updated = AuditEvent.objects.get(action="vendor_risk.updated")
    assert updated.changes["risk_tier"] == ["high", "critical"]
    pre, post = updated.prechange or {}, updated.postchange or {}
    assert pre["risk_tier"] == "high" and post["risk_tier"] == "critical"
    assert post["assurance_attachment"] == {"id": str(attachment.id), "display": "SOC 2 report.pdf"}
    doc = document(a, v, title="FY26", expires_on=iso(a, 100))
    risk.update_document(a["org"], a["users"]["editor"], doc, {"title": "FY26 bridge letter"})
    risk.delete_document(a["org"], a["users"]["editor"], doc)
    actions = list(
        AuditEvent.objects.filter(action__startswith="vendor_risk_document.")
        .order_by("created_at")
        .values_list("action", flat=True)
    )
    assert actions == ["vendor_risk_document.created", "vendor_risk_document.updated", "vendor_risk_document.deleted"]
    log = api(a["users"]["admin"]).get("audit-events?action=vendor_risk_document.updated").json_body["results"]
    assert log[0]["target"]["kind"] == "vendor" and "Title: FY26 → FY26 bridge letter" in log[0]["summary"]


def test_deleting_a_vendor_removes_its_risk_file_audited_and_its_reminders(a, django_capture_on_commit_callbacks):
    v = vendor(a)
    attachment = link(a, v)
    with django_capture_on_commit_callbacks(execute=True):
        profile(a, v, risk_tier="high", dpa_attachment=str(attachment.id))
        document(a, v, attachment=str(attachment.id), expires_on=iso(a, 30))
    assert live(a["org"], source_type="vendor_risk").count() == 1
    with django_capture_on_commit_callbacks(execute=True):
        admin_services.delete_named(Vendor, a["org"], a["users"]["admin"], v)
    assert not VendorRiskProfile.objects.exists() and not VendorRiskDocument.objects.exists()
    assert not Attachment.objects.exists()
    assert {"vendor_risk.deleted", "vendor_risk_document.deleted", "attachment.deleted", "vendor.deleted"} <= set(
        AuditEvent.objects.values_list("action", flat=True)
    )
    assert not live(a["org"], source_type="vendor_risk").exists()


def test_removing_a_linked_attachment_clears_the_link(a):
    from catalog import attachments

    v = vendor(a)
    attachment = link(a, v)
    p = profile(a, v, dpa_attachment=str(attachment.id))
    doc = document(a, v, attachment=str(attachment.id), expires_on=iso(a, 100))
    attachments.delete_attachment(a["org"], a["users"]["editor"], attachment)
    p.refresh_from_db()
    doc.refresh_from_db()
    assert p.dpa_attachment_id is None and doc.attachment_id is None


# ---- superseded documents and gaps -------------------------------------------------------------------------------


def test_a_later_item_of_the_same_family_supersedes_the_older(a):
    v = vendor(a)
    old = document(a, v, expires_on=iso(a, -5))
    iso_cert = document(a, v, kind="iso27001", expires_on=iso(a, 100))  # its own family: held beside the SOC 2
    new = document(a, v, expires_on=iso(a, 360))
    other_1 = document(a, v, kind="other", expires_on=iso(a, -5))
    other_2 = document(a, v, kind="other", expires_on=iso(a, 360))
    pen = document(a, v, kind="pen_test", expires_on=iso(a, -5))
    insurance_old = document(a, v, kind="insurance", expires_on=iso(a, 10))
    insurance_new = document(a, v, kind="insurance", expires_on=iso(a, 200))
    found = risk.superseded_for(a["org"])
    # Like for like: `other` documents are unrelated; a lone pen test isn't replaced; an ISO 27001 certificate and a
    # SOC 2 report coexist.
    assert found == {old.id, insurance_old.id}
    assert not {new.id, iso_cert.id, other_1.id, other_2.id, pen.id, insurance_new.id} & found
    summary = risk.summary(risk.files(a["org"], [v])[v.id], today(a), 90)
    assert summary["documents"] == {"current": 4, "expiring": 0, "expired": 2}  # the replaced two aren't counted


def test_gaps_follow_tier_assurance_personal_data_and_the_dpa(a):
    v = vendor(a)
    day = today(a)

    def gaps():
        f = risk.files(a["org"], [v])[v.id]
        return risk.gaps_of(f.profile, f.documents, day)

    assert gaps() == []  # no profile
    profile(a, v, risk_tier="medium", processes_personal_data=False)
    assert gaps() == []
    profile(a, v, risk_tier="critical")
    assert gaps() == ["no_current_assurance"]
    profile(a, v, assurance_type="iso27001", assurance_issued_on=iso(a, -400), assurance_validity_months=12)
    assert gaps() == ["no_current_assurance"]  # expired
    profile(a, v, assurance_issued_on=iso(a, -300))
    assert gaps() == []  # current
    profile(a, v, assurance_type="")
    assert gaps() == ["no_current_assurance"]  # a report without a type is no report (it has no expiry)
    profile(a, v, assurance_type="iso27001", assurance_issued_on=None)
    doc = document(a, v, expires_on=iso(a, -1))
    assert gaps() == ["no_current_assurance"]  # an expired SOC 2 document doesn't count
    risk.update_document(a["org"], a["users"]["editor"], doc, {"expires_on": iso(a, 0)})
    assert gaps() == []  # one expiring today does
    with pytest.raises(FieldErrors) as undated:
        risk.update_document(a["org"], a["users"]["editor"], doc, {"expires_on": None})
    assert "expires_on" in undated.value.errors  # SOC 2 and ISO 27001 documents are always dated
    profile(a, v, processes_personal_data=True)
    assert gaps() == ["dpa_missing"]
    profile(a, v, dpa_status="no")
    assert gaps() == ["dpa_missing"]
    document(a, v, kind="dpa", expires_on=iso(a, 10))
    assert gaps() == []  # a DPA document in force
    VendorRiskDocument.objects.filter(kind="dpa").update(expires_on=None)
    assert gaps() == []  # an undated DPA is in force too (only assurance documents must be dated)
    VendorRiskDocument.objects.filter(kind="dpa").delete()
    profile(a, v, dpa_status="yes")
    assert gaps() == []


# ---- reminders ---------------------------------------------------------------------------------------------------


def test_documents_and_the_assurance_report_are_reminder_sources(a, django_capture_on_commit_callbacks):
    v = vendor(a)
    with django_capture_on_commit_callbacks(execute=True):
        doc = document(a, v, title="FY26", expires_on=iso(a, 100))
        # The report expires in 65 days, before the document: superseded.
        profile(a, v, assurance_type="soc2_type1", assurance_issued_on=iso(a, -300), assurance_validity_months=12)
    found = {r.source_field: r for r in live(a["org"], source_type="vendor_risk")}
    assert set(found) == {f"document:{doc.id}"}  # the later SOC 2 document supersedes the profile's report
    reminder = found[f"document:{doc.id}"]
    assert reminder.source_id == v.id and reminder.title == "Fabrikam"
    assert reminder.label == "SOC 2 report (FY26) expires" and reminder.due_date.isoformat() == iso(a, 100)
    with django_capture_on_commit_callbacks(execute=True):
        risk.update_document(a["org"], a["users"]["editor"], doc, {"kind": "pen_test"})
    labels = {r.source_field: r.label for r in live(a["org"], source_type="vendor_risk")}
    assert labels == {
        f"document:{doc.id}": "Penetration test report (FY26) expires",
        "assurance": "SOC 2 Type I expires",
    }


def test_a_moved_date_moves_the_same_reminder_and_reruns_add_nothing(a, django_capture_on_commit_callbacks):
    v = vendor(a)
    with django_capture_on_commit_callbacks(execute=True):
        doc = document(a, v, expires_on=iso(a, 40))
    first = live(a["org"], source_type="vendor_risk").get()
    with django_capture_on_commit_callbacks(execute=True):
        risk.update_document(a["org"], a["users"]["editor"], doc, {"expires_on": iso(a, 50)})
    moved = live(a["org"], source_type="vendor_risk").get()
    assert moved.id == first.id and moved.due_date.isoformat() == iso(a, 50)
    assert reminders.generate(a["org"], today(a)) == dict.fromkeys(reminders.KEYS, 0)  # nothing to do, no duplicate
    assert Reminder.objects.filter(source_type="vendor_risk").count() == 1
    with django_capture_on_commit_callbacks(execute=True):
        Vendor.objects.filter(pk=v.pk).update(name="Fabrikam Ltd")
        admin_services.update_named(Vendor, a["org"], a["users"]["editor"], Vendor.objects.get(pk=v.pk), {})
    assert live(a["org"], source_type="vendor_risk").get().title == "Fabrikam Ltd"  # the vendor's name refreshed


def test_a_superseded_or_deleted_document_closes_its_reminder(a, django_capture_on_commit_callbacks):
    v = vendor(a)
    with django_capture_on_commit_callbacks(execute=True):
        old = document(a, v, expires_on=iso(a, 10))
    with django_capture_on_commit_callbacks(execute=True):
        new = document(a, v, expires_on=iso(a, 370))
    assert set(live(a["org"], source_type="vendor_risk").values_list("source_field", flat=True)) == {
        f"document:{new.id}"
    }
    assert Reminder.objects.get(source_field=f"document:{old.id}").status == "obsolete"
    with django_capture_on_commit_callbacks(execute=True):
        risk.delete_document(a["org"], a["users"]["editor"], new)
    # The old one is no longer superseded: its reminder comes back (same row, a new notify cycle).
    back = live(a["org"], source_type="vendor_risk").get()
    assert back.source_field == f"document:{old.id}" and back.notify_cycle == 1


def test_without_the_capability_no_reminder_row_or_check(a, django_capture_on_commit_callbacks):
    v = vendor(a)
    with django_capture_on_commit_callbacks(execute=True):
        profile(a, v, risk_tier="critical", processes_personal_data=True)
        document(a, v, kind="pen_test", expires_on=iso(a, 20))
    assert live(a["org"], source_type="vendor_risk").count() == 1
    assert len(risk.attention_rows(a["org"], today(a))) == 1
    revoke(a["org"])
    reminders.generate(a["org"], today(a))
    assert not live(a["org"], source_type="vendor_risk").exists()
    assert risk.attention_rows(a["org"], today(a)) == []
    assert not {"vendor_assurance", "vendor_dpa"} & {r.id for r in quality.active_rules(a["org"])}


def test_the_built_in_rule_covers_vendor_risk_and_the_vendor_filter_matches(a):
    rule = NotificationRule.objects.get(organization=a["org"], builtin_key="vendor_risk")
    assert rule.source_types == ["vendor_risk"] and rule.lead_times_days == [60, 30, 7] and rule.enabled
    assert rule_services.seed(a["org"]) == 0
    v = vendor(a)
    document(a, v, expires_on=iso(a, 25))
    reminders.generate(a["org"], today(a))
    reminder = live(a["org"], source_type="vendor_risk").get()
    decision = rules.decide(rule, reminder, today(a))
    assert decision.stage is not None and decision.stage.days == 30  # 25 days out: the 30-day lead
    facts = rules.filter_facts(a["org"], [reminder])
    assert facts[reminder.id].vendors == {v.id}
    rule.vendor = v
    assert rules.matches(rule, facts[reminder.id])
    rule.vendor = vendor(a, "Someone else")
    assert not rules.matches(rule, facts[reminder.id])


def test_a_vendor_risk_reminder_points_at_the_vendor(a, api):
    v = vendor(a)
    document(a, v, expires_on=iso(a, 15))
    reminders.generate(a["org"], today(a))
    reminder = live(a["org"], source_type="vendor_risk").get()
    shown = api(a["users"]["viewer"]).get(f"reminders/{reminder.id}").json_body
    assert shown["path"] == f"/vendors/{v.id}" and shown["source_type"] == "vendor_risk"
    body = api(a["users"]["admin"]).get("needs-attention?scope=all").json_body
    row = next(r for r in body["reminders"] if r["id"] == str(reminder.id))
    assert row["path"] == f"/vendors/{v.id}" and row["label"] == "SOC 2 report expires"
    # Closing one by hand needs a reason, like a credential's expiry.
    editor = api(a["users"]["editor"])
    refused = editor.post(f"reminders/{reminder.id}/resolve", {"kind": "done"})
    assert refused.status_code == 400 and "note" in refused.json_body["errors"]


# ---- Needs attention and data quality ------------------------------------------------------------------------------


def test_the_vendor_risk_queue_lists_vendors_with_gaps_for_the_coordinator(a, api):
    v = vendor(a)
    fine = vendor(a, "Contoso")
    profile(a, v, risk_tier="critical", processes_personal_data=True)
    profile(a, fine, risk_tier="high", dpa_status="yes", processes_personal_data=True)
    document(a, fine, kind="iso27001", expires_on=iso(a, 200))
    body = api(a["users"]["admin"]).get("needs-attention?scope=all").json_body
    assert [r["name"] for r in body["vendor_risk"]] == ["Fabrikam"]
    row = body["vendor_risk"][0]
    assert row["kind"] == "vendor_risk" and row["risk_tier"] == "critical"
    assert [g["code"] for g in row["gaps"]] == ["no_current_assurance", "dpa_missing"]
    assert row["steward"]["coordinator"] is True
    # Counted, and a digest section after Seat counts, since the vendor risk file's SPA package shows the queue.
    assert "vendor_risk" not in UNCOUNTED and "vendor_risk" not in digest.PENDING_SECTIONS
    queues = [q for q in body if isinstance(body[q], list) and q not in UNCOUNTED]
    assert "vendor_risk" in queues and body["counts"]["all"] == sum(len(body[q]) for q in queues)
    assert digest.SECTION_NAMES["vendor_risk"] == "Vendor risk"
    names = [q for q, _ in digest.SECTIONS]
    assert names.index("vendor_risk") == names.index("seat_counts") + 1
    assert digest._path("vendor_risk", row) == f"/vendors/{v.id}?tab=risk"
    assert digest._line("vendor_risk", row) == "Fabrikam — No current SOC 2 or equivalent, No signed DPA"
    mine = api(a["users"]["admin"]).get("needs-attention?scope=mine").json_body["vendor_risk"]
    assert [r["id"] for r in mine] == [str(v.id)]  # the coordinator's
    assert api(a["users"]["editor"]).get("needs-attention?scope=mine").json_body["vendor_risk"] == []
    # A row can be snoozed like any other.
    made = api(a["users"]["admin"]).post(
        "needs-attention/snoozes",
        {"subject_type": "vendor_risk", "subject_id": str(v.id), "kind": "snooze", "until": iso(a, 7)},
    )
    assert made.status_code == 201, made.json_body


def test_the_data_quality_rules_judge_the_vendors_applications(a, api):
    v = vendor(a)
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"vendor": str(v.id)})
    ids = lambda: [r.id for r in quality.evaluate(a["org"], [a["app"].id])[a["app"].id].failing]  # noqa: E731
    assert not {"vendor_assurance", "vendor_dpa"} & set(ids())  # no profile: the rules don't apply
    profile(a, v, risk_tier="high", processes_personal_data=True)
    assert {"vendor_assurance", "vendor_dpa"} <= set(ids())
    profile(a, v, dpa_status="yes", assurance_type="soc2_type2", assurance_issued_on=iso(a, -10))
    assert not {"vendor_assurance", "vendor_dpa"} & set(ids())
    profile(a, v, assurance_issued_on=None, dpa_status="no")
    document(a, v, kind="iso27001", expires_on=iso(a, 10))  # in force: counts
    document(a, v, kind="dpa", expires_on=iso(a, -1))  # expired: doesn't
    assert set(ids()) & {"vendor_assurance", "vendor_dpa"} == {"vendor_dpa"}
    # The Vendor risk queue lists the vendor; the Data quality queue doesn't repeat it.
    body = api(a["users"]["admin"]).get("needs-attention?scope=all").json_body
    assert {r["rule"] for r in body["data_quality"]}.isdisjoint({"vendor_assurance", "vendor_dpa"})
    assert [r["id"] for r in body["vendor_risk"]] == [str(v.id)]
    rules_body = {r["id"]: r for r in api(a["users"]["viewer"]).get("data-quality/rules").json_body}
    assert rules_body["vendor_dpa"]["covered_by"] == "vendor_risk"


def test_the_gaps_and_the_quality_rules_agree(a):
    """catalog/vendor_risk.gaps_of (Python) and the rules' SQL (assured_q, dpa_q) on a mixed set of vendors: the
    profile half and the documents half, at the expiry boundaries."""
    services_editor = a["users"]["editor"]
    high, personal = {"risk_tier": "high"}, {"processes_personal_data": True}
    report = {"assurance_type": "soc2_type2"}
    # name: (profile data, documents, the report's expiry set directly: offset in days from today, or None)
    cases = {
        "none": ({}, [], None),
        "high, no report": (high, [], None),
        "high, report": ({**high, **report, "assurance_issued_on": iso(a, -30)}, [], None),
        "critical, old report": ({"risk_tier": "critical", **report, "assurance_issued_on": iso(a, -800)}, [], None),
        "high, untyped report": ({**high, "assurance_issued_on": iso(a, -30)}, [], None),
        "high, report expires today": ({**high, **report, "assurance_issued_on": iso(a, -30)}, [], 0),
        "high, report expired yesterday": ({**high, **report, "assurance_issued_on": iso(a, -30)}, [], -1),
        "high, soc2 expires today": (high, [{"kind": "soc2", "expires_on": iso(a, 0)}], None),
        "high, soc2 expired yesterday": (high, [{"kind": "soc2", "expires_on": iso(a, -1)}], None),
        "high, iso by validity": (
            high,
            [{"kind": "iso27001", "issued_on": iso(a, -200), "validity_months": 12}],
            None,
        ),
        "high, old iso by validity": (
            high,
            [{"kind": "iso27001", "issued_on": iso(a, -400), "validity_months": 12}],
            None,
        ),
        "high, pen test only": (high, [{"kind": "pen_test", "expires_on": iso(a, 100)}], None),
        "personal, no dpa": (personal, [], None),
        "personal, n/a": ({**personal, "dpa_status": "not_applicable"}, [], None),
        "personal, signed": ({**personal, "dpa_status": "yes"}, [], None),
        "personal, dpa doc today": (personal, [{"kind": "dpa", "expires_on": iso(a, 0)}], None),
        "personal, dpa doc yesterday": (personal, [{"kind": "dpa", "expires_on": iso(a, -1)}], None),
        "personal, undated dpa doc": (personal, [{"kind": "dpa"}], None),
    }
    apps = {}
    for name, (data, docs, report_offset) in cases.items():
        v = vendor(a, name)
        if data:
            profile(a, v, **data)
        for doc in docs:
            risk.create_document(a["org"], services_editor, v, doc)
        if report_offset is not None:
            day = today(a) + timedelta(days=report_offset)
            VendorRiskProfile.objects.filter(vendor=v).update(assurance_expires_on=day)
        app = services.create_resource(
            a["org"],
            services_editor,
            {"kind": "application", "name": f"App {name}", "lifecycle": "active", "vendor": str(v.id)},
        )
        apps[name] = (v, app)
    results = quality.evaluate(a["org"], [app.id for _, app in apps.values()])
    for name, (v, app) in apps.items():
        f = risk.files(a["org"], [v])[v.id]
        expected = {
            {"no_current_assurance": "vendor_assurance", "dpa_missing": "vendor_dpa"}[g]
            for g in risk.gaps_of(f.profile, f.documents, today(a))
        }
        failing = {r.id for r in results[app.id].failing} & {"vendor_assurance", "vendor_dpa"}
        assert failing == expected, name
    # The cases cover both outcomes of both rules.
    gaps = {name: _gaps(a, v) for name, (v, _) in apps.items()}
    assert gaps["high, report expires today"] == set() and gaps["high, report expired yesterday"] == {
        "no_current_assurance"
    }
    assert gaps["high, soc2 expires today"] == set() and gaps["high, soc2 expired yesterday"] == {
        "no_current_assurance"
    }
    assert gaps["high, iso by validity"] == set() and gaps["high, old iso by validity"] == {"no_current_assurance"}
    assert gaps["high, pen test only"] == {"no_current_assurance"} and gaps["high, untyped report"] == {
        "no_current_assurance"
    }
    assert gaps["personal, dpa doc today"] == set() and gaps["personal, dpa doc yesterday"] == {"dpa_missing"}
    assert gaps["personal, undated dpa doc"] == set()


def _gaps(f, v) -> set[str]:
    found = risk.files(f["org"], [v])[v.id]
    return set(risk.gaps_of(found.profile, found.documents, today(f)))


# ---- the API: roles, ETags, filters, exports ---------------------------------------------------------------------


def test_viewers_read_and_editors_write(a, api):
    v = vendor(a)
    viewer, editor = api(a["users"]["viewer"]), api(a["users"]["editor"])
    empty = viewer.get(f"vendors/{v.id}/risk")
    assert empty.status_code == 200 and empty.json_body["id"] is None
    assert empty["ETag"] == f'"{absent_revision(v.id)}"'
    assert empty.json_body["questionnaire_status"] == "not_requested"
    assert empty.json_body["assurance_validity_months"] == 12
    assert viewer.patch(f"vendors/{v.id}/risk", {"risk_tier": "low"}).status_code == 403
    assert viewer.post(f"vendors/{v.id}/risk-documents", {"kind": "soc2"}).status_code == 403
    made = editor.patch(f"vendors/{v.id}/risk", {"risk_tier": "low"})
    assert made.status_code == 200 and made.json_body["risk_tier"] == "low" and made.json_body["id"]
    doc = editor.post(f"vendors/{v.id}/risk-documents", {"kind": "soc2", "expires_on": iso(a, 10)})
    assert doc.status_code == 201 and doc.json_body["status"] == "expiring"
    url = f"vendor-risk-documents/{doc.json_body['id']}"
    assert viewer.get(url).status_code == 200
    assert viewer.patch(url, {"title": "x"}).status_code == 403
    assert viewer.delete(url).status_code == 403
    assert editor.patch(url, {"title": "Bridge letter"}).json_body["title"] == "Bridge letter"
    assert editor.delete(url).status_code == 204
    assert api(a["users"]["admin"]).patch(f"vendors/{v.id}/risk", {"notes": "ok"}).status_code == 200


def test_the_profile_honours_if_match(a, api):
    v = vendor(a)
    absent = f'"{absent_revision(v.id)}"'
    first = patch_if_match(a, "editor", f"vendors/{v.id}/risk", {"risk_tier": "low"}, absent)
    assert first.status_code == 200
    tag = first["ETag"]
    stale = patch_if_match(a, "editor", f"vendors/{v.id}/risk", {"risk_tier": "high"}, absent)
    assert stale.status_code == 412
    fresh = patch_if_match(a, "editor", f"vendors/{v.id}/risk", {"risk_tier": "high"}, tag)
    assert fresh.status_code == 200 and fresh.json().get("risk_tier") == "high"
    assert VendorRiskProfile.objects.get().risk_tier == "high"


def test_the_document_list_filters(a, api):
    v, w = vendor(a), vendor(a, "Contoso")
    profile(a, v, risk_tier="critical")
    soon = document(a, v, expires_on=iso(a, 10))
    later = document(a, v, kind="pen_test", expires_on=iso(a, 200))
    gone = document(a, w, kind="insurance", expires_on=iso(a, -3))
    viewer = api(a["users"]["viewer"])

    def ids(query):
        response = viewer.get(f"vendor-risk-documents?{query}")
        assert response.status_code == 200, response.json_body
        return {r["id"] for r in response.json_body["results"]}

    assert ids("") == {str(soon.id), str(later.id), str(gone.id)}
    assert ids("tier=critical") == {str(soon.id), str(later.id)}
    assert ids("tier=none") == {str(gone.id)}
    assert ids("expiring_within=30") == {str(soon.id)}
    assert ids("status=expired") == {str(gone.id)}
    assert ids("status=current,expiring") == {str(soon.id), str(later.id)}
    assert ids(f"vendor={w.id}") == {str(gone.id)}
    assert ids("kind=pen_test,insurance") == {str(later.id), str(gone.id)}
    assert viewer.get("vendor-risk-documents?expiring_within=soon").status_code == 400
    assert viewer.get("vendor-risk-documents?tier=extreme").status_code == 400
    assert viewer.get("vendor-risk-documents?colour=red").status_code == 400
    ordered = [r["id"] for r in viewer.get("vendor-risk-documents?ordering=-expiry").json_body["results"]]
    assert ordered == [str(later.id), str(soon.id), str(gone.id)]


def test_the_overview_lists_every_vendor_riskiest_first_with_filters(a, api):
    v, w, x = vendor(a, "Fabrikam"), vendor(a, "Contoso"), vendor(a, "Adatum")
    profile(a, v, risk_tier="high", processes_personal_data=True)
    document(a, v, kind="pen_test", expires_on=iso(a, 20))
    profile(a, w, risk_tier="critical", assurance_type="iso27001", assurance_issued_on=iso(a, -100))
    document(a, w, kind="insurance", expires_on=iso(a, -2))
    viewer = api(a["users"]["viewer"])
    body = viewer.get("vendors/risk-overview").json_body
    assert [r["vendor"]["name"] for r in body] == ["Contoso", "Fabrikam", "Adatum"]
    fab = body[1]
    assert fab["risk_tier"] == "high" and fab["has_profile"] is True
    assert [g["code"] for g in fab["gaps"]] == ["no_current_assurance", "dpa_missing"]
    assert fab["documents"] == {"current": 0, "expiring": 1, "expired": 0}
    assert fab["next_expiry"] == iso(a, 20) and fab["document_list"][0]["kind"] == "pen_test"
    assert body[2]["has_profile"] is False and body[2]["risk_tier"] is None and body[2]["gaps"] == []
    assert body[0]["assurance"]["status"] == "current"

    def names(query):
        response = viewer.get(f"vendors/risk-overview?{query}")
        assert response.status_code == 200, response.json_body
        return [r["vendor"]["name"] for r in response.json_body]

    assert names("tier=critical,high") == ["Contoso", "Fabrikam"]
    assert names("tier=none") == ["Adatum"]
    assert names("gap=dpa_missing") == ["Fabrikam"]
    assert names("expiring_within=30") == ["Fabrikam"]
    assert names("expired=true") == ["Contoso"]
    assert names("expired=false") == ["Fabrikam", "Adatum"]
    for bad in ("tier=extreme", "gap=nope", "expiring_within=-1", "expired=maybe", "colour=red"):
        assert viewer.get(f"vendors/risk-overview?{bad}").status_code == 400, bad
    assert x.id  # Adatum stays listed with no risk file


def test_the_csv_exports_follow_the_filters_and_are_audited(a, api):
    v, w = vendor(a), vendor(a, "Contoso")
    profile(a, v, risk_tier="high", processes_personal_data=True, notes="=cmd|' /C calc'!A0")
    document(a, v, kind="pen_test", title='=HYPERLINK("x")', expires_on=iso(a, 20))
    profile(a, w, risk_tier="low")
    viewer = api(a["users"]["viewer"])
    response = viewer.get("vendors/risk-overview/export.csv?tier=high")
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    assert "attachment;" in response["Content-Disposition"]
    rows = rows_of(response)
    assert [r["vendor"] for r in rows] == ["Fabrikam"]
    assert rows[0]["gaps"] == "No current SOC 2 or equivalent; No signed DPA"
    assert rows[0]["documents_expiring"] == "1" and rows[0]["next_expiry"] == iso(a, 20)
    event = AuditEvent.objects.get(action="export", changes__kind="vendor_risk_file")
    assert event.changes == {"kind": "vendor_risk_file", "rows": 1, "filters": {"tier": "high"}}
    docs = rows_of(viewer.get("vendor-risk-documents/export.csv?status=expiring"))
    assert [d["vendor"] for d in docs] == ["Fabrikam"] and docs[0]["status"] == "expiring"
    assert docs[0]["title"].startswith("'=")  # formulas are escaped
    assert AuditEvent.objects.filter(action="export", changes__kind="vendor_risk_document").exists()
    assert viewer.get("vendor-risk-documents/export.csv?tier=nope").status_code == 400
    summary = api(a["users"]["admin"]).get("audit-events?action=export").json_body["results"]
    assert {e["summary"] for e in summary} >= {"CSV, 1 vendor risk file", "CSV, 1 vendor risk document"}


def test_the_vendor_detail_carries_the_risk_summary(a, api):
    v = vendor(a)
    viewer = api(a["users"]["viewer"])
    shown = viewer.get(f"vendors/{v.id}").json_body
    assert shown["risk"]["has_profile"] is False and shown["risk"]["assurance"]["status"] == "missing"
    profile(a, v, risk_tier="critical")
    document(a, v, kind="pen_test", expires_on=iso(a, 30))
    shown = viewer.get(f"vendors/{v.id}").json_body
    assert shown["risk"]["risk_tier"] == "critical" and shown["risk"]["next_expiry"] == iso(a, 30)
    assert [g["code"] for g in shown["risk"]["gaps"]] == ["no_current_assurance"]
    assert "risk" not in viewer.get("vendors").json_body[0]  # the list stays lean
    revoke(a["org"])
    assert viewer.get(f"vendors/{v.id}").json_body["risk"] is None


def test_every_route_needs_the_capability(a, api):
    v = vendor(a)
    doc = document(a, v, expires_on=iso(a, 10))
    revoke(a["org"])
    editor = api(a["users"]["editor"])
    for method, url, data in (
        ("get", f"vendors/{v.id}/risk", None),
        ("patch", f"vendors/{v.id}/risk", {"risk_tier": "low"}),
        ("get", f"vendors/{v.id}/risk-documents", None),
        ("post", f"vendors/{v.id}/risk-documents", {"kind": "soc2"}),
        ("get", "vendor-risk-documents", None),
        ("get", "vendor-risk-documents/export.csv", None),
        ("get", f"vendor-risk-documents/{doc.id}", None),
        ("delete", f"vendor-risk-documents/{doc.id}", None),
        ("get", "vendors/risk-overview", None),
        ("get", "vendors/risk-overview/export.csv", None),
    ):
        response = getattr(editor, method)(url, data) if data is not None else getattr(editor, method)(url)
        assert response.status_code == 403, (method, url)
        assert response.json()["capability"] == "vendor_risk"


def test_search_finds_a_vendor_by_its_risk_documents(a, api):
    v = vendor(a)
    document(a, v, title="Kestrelridge bridge letter", expires_on=iso(a, 10))
    profile(a, v, notes="Escalate through Ombrellino")
    viewer = api(a["users"]["viewer"])
    for word in ("kestrelridge", "ombrellino"):
        body = viewer.get(f"search?q={word}").json_body
        vendors = next(g for g in body["groups"] if g["kind"] == "vendor")
        assert [h["id"] for h in vendors["results"]] == [str(v.id)], word


# ---- tenancy -------------------------------------------------------------------------------------------------------


def test_another_organizations_risk_file_is_404(a, b, api):
    theirs = vendor(b, "Theirs")
    their_doc = document(b, theirs, expires_on=iso(b, 10))
    profile(b, theirs, risk_tier="high")
    client = api(a["users"]["admin"])
    assert client.get(f"vendors/{theirs.id}/risk").status_code == 404
    assert client.patch(f"vendors/{theirs.id}/risk", {"risk_tier": "low"}).status_code == 404
    assert client.get(f"vendors/{theirs.id}/risk-documents").status_code == 404
    assert client.post(f"vendors/{theirs.id}/risk-documents", {"kind": "soc2"}).status_code == 404
    for method in ("get", "patch", "delete"):
        url = f"vendor-risk-documents/{their_doc.id}"
        response = getattr(client, method)(url, {"title": "x"}) if method == "patch" else getattr(client, method)(url)
        assert response.status_code == 404
    assert client.get("vendor-risk-documents").json_body["results"] == []
    assert client.get("vendors/risk-overview").json_body == []
    assert client.get(f"vendor-risk-documents?vendor={theirs.id}").json_body["results"] == []
    their_doc.refresh_from_db()
    assert their_doc.title == "" and VendorRiskProfile.objects.get(vendor=theirs).risk_tier == "high"
    # Their attachment is unknown here.
    mine = vendor(a)
    refused = api(a["users"]["editor"]).post(
        f"vendors/{mine.id}/risk-documents", {"kind": "dpa", "attachment": str(link(b, theirs).id)}
    )
    assert refused.status_code == 400 and "attachment" in refused.json_body["errors"]


def test_the_organization_export_and_purge_cover_the_risk_file():
    from catalog import org_lifecycle

    labels = {m._meta.label for m in org_lifecycle.tenant_models()}
    assert {"catalog.VendorRiskProfile", "catalog.VendorRiskDocument"} <= labels
    assert "notes" in org_lifecycle.exported_columns(VendorRiskProfile)


def test_the_demo_has_vendor_risk_files_with_gaps_and_expiring_documents(db):
    from django.core.management import call_command

    from core.models import Membership, Organization, Role

    call_command("seed_demo", stdout=io.StringIO())
    org = Organization.objects.get(name="Contoso (synthetic)")
    admin = Membership.objects.filter(organization=org, role=Role.ADMIN).order_by("id")[0].user
    queues, _ = attention_queues(org, admin, services.org_today(org))
    assert [(r["name"], [g["code"] for g in r["gaps"]]) for r in queues["vendor_risk"]] == [
        ("Zoom", ["no_current_assurance", "dpa_missing"])
    ]
    rows = {r["vendor"]["name"]: r for r in risk.overview(org, risk.OverviewFilters())}
    assert rows["Workday Inc."]["assurance"]["status"] == "expiring"
    assert rows["Workday Inc."]["documents"] == {"current": 1, "expiring": 1, "expired": 0}
    call_command("seed_demo", stdout=io.StringIO())  # a rerun leaves the scenario alone
    assert VendorRiskDocument.objects.filter(organization=org).count() == 3


# ---- review follow-ups -----------------------------------------------------------------------------------------------


def test_a_newer_report_supersedes_an_older_document_and_the_other_way(a, django_capture_on_commit_callbacks):
    """A SOC 2 report on the profile and SOC 2 documents are one family: the latest replaces the rest, in both
    directions, for reminders, counts, `next_expiry` and the overview's expiry filters."""
    v = vendor(a)
    with django_capture_on_commit_callbacks(execute=True):
        old = document(a, v, title="FY25", expires_on=iso(a, 20))
    assert live(a["org"], source_type="vendor_risk").get().source_field == f"document:{old.id}"
    with django_capture_on_commit_callbacks(execute=True):
        p = profile(a, v, risk_tier="high", assurance_type="soc2_type2", assurance_issued_on=iso(a, 0))
    report_day = p.assurance_expires_on
    assert set(live(a["org"], source_type="vendor_risk").values_list("source_field", flat=True)) == {"assurance"}
    assert Reminder.objects.get(source_field=f"document:{old.id}").status == "obsolete"
    f = risk.files(a["org"], [v])[v.id]
    summary = risk.summary(f, today(a), 90)
    assert summary["documents"] == {"current": 0, "expiring": 0, "expired": 0}  # the old one isn't counted
    assert summary["next_expiry"] == report_day.isoformat() and summary["assurance"]["status"] == "current"
    later = today(a) + timedelta(days=30)  # the old document has expired by then: the vendor still isn't "expired"
    expired = risk.overview(a["org"], risk.OverviewFilters(expired=True), today=later)
    assert expired == []
    soon = risk.overview(a["org"], risk.OverviewFilters(expiring_within=25))
    assert soon == []  # the old document's date isn't an upcoming expiry any more
    shown = api_get(a, f"vendors/{v.id}/risk-documents")
    assert shown[0]["superseded"] is True
    assert api_get(a, f"vendors/{v.id}/risk")["assurance_superseded"] is False
    # The other way: a later SOC 2 document replaces the report.
    with django_capture_on_commit_callbacks(execute=True):
        newer = document(a, v, title="FY27", expires_on=(report_day + timedelta(days=30)).isoformat())
    assert set(live(a["org"], source_type="vendor_risk").values_list("source_field", flat=True)) == {
        f"document:{newer.id}"
    }
    assert api_get(a, f"vendors/{v.id}/risk")["assurance_superseded"] is True


def api_get(f, url):
    from django.test import Client

    client = Client()
    client.force_login(f["users"]["viewer"])
    response = client.get("/api/v1/" + url)
    assert response.status_code == 200, response.content
    return response.json()


def test_a_tie_on_the_expiry_goes_to_the_item_created_last(a):
    v = vendor(a)
    first = document(a, v, expires_on=iso(a, 50))
    second = document(a, v, expires_on=iso(a, 50))
    assert risk.superseded_for(a["org"]) == {first.id}
    VendorRiskDocument.objects.filter(pk=second.pk).update(created_at=first.created_at)  # same time: the higher id
    assert risk.superseded_for(a["org"]) == {min(first.id, second.id, key=str)}
    # A SOC 2 document added after the SOC 2 report with the same expiry replaces the report.
    w = vendor(a, "Contoso")
    p = profile(a, w, assurance_type="soc2_type1", assurance_issued_on=iso(a, -300))
    assert p.assurance_expires_on is not None
    same_day = document(a, w, expires_on=p.assurance_expires_on.isoformat())
    replaced = risk.superseded_for(a["org"], [w.id])
    assert replaced == {p.id} and same_day.id not in replaced
    assert risk.assurance_superseded(p, [same_day])


def test_a_soc2_report_and_an_iso_certificate_are_both_in_force(a, django_capture_on_commit_callbacks):
    v = vendor(a)
    with django_capture_on_commit_callbacks(execute=True):
        p = profile(a, v, risk_tier="high", assurance_type="soc2_type2", assurance_issued_on=iso(a, -300))
        cert = document(a, v, kind="iso27001", expires_on=iso(a, 600))  # expires long after the SOC 2 report
    assert risk.superseded_for(a["org"]) == set()
    assert set(live(a["org"], source_type="vendor_risk").values_list("source_field", flat=True)) == {
        "assurance",
        f"document:{cert.id}",
    }
    summary = risk.summary(risk.files(a["org"], [v])[v.id], today(a), 90)
    assert summary["assurance"]["status"] == "expiring" and summary["next_expiry"] == p.assurance_expires_on.isoformat()
    assert summary["documents"] == {"current": 1, "expiring": 0, "expired": 0}
    # Once the SOC 2 report lapses it shows as expired; the certificate still satisfies the gap.
    later = p.assurance_expires_on + timedelta(days=1)
    assert [r["vendor"]["name"] for r in risk.overview(a["org"], risk.OverviewFilters(expired=True), today=later)] == [
        "Fabrikam"
    ]
    f = risk.files(a["org"], [v])[v.id]
    assert risk.gaps_of(f.profile, f.documents, later) == []
    assert api_get(a, f"vendors/{v.id}/risk")["assurance_superseded"] is False
    # Like for like: a later SOC 2 document replaces the SOC 2 profile report; the ISO certificate stays in force.
    soc2 = document(a, v, expires_on=iso(a, 700))
    assert risk.superseded_for(a["org"]) == {p.id} and soc2.id not in risk.superseded_for(a["org"])


def test_an_other_report_is_never_replaced_by_a_soc2_document(a, django_capture_on_commit_callbacks):
    v = vendor(a)
    with django_capture_on_commit_callbacks(execute=True):
        p = profile(a, v, assurance_type="other", assurance_issued_on=iso(a, -300))
        soc2 = document(a, v, expires_on=iso(a, 600))
    assert risk.superseded_for(a["org"]) == set()
    assert set(live(a["org"], source_type="vendor_risk").values_list("source_field", flat=True)) == {
        "assurance",
        f"document:{soc2.id}",
    }
    assert risk.assurance_superseded(p, [soc2]) is False


def test_assurance_documents_must_be_dated_other_kinds_needn_t(a, api):
    v = vendor(a)
    for kind in ("soc2", "iso27001"):
        with pytest.raises(FieldErrors) as refused:
            risk.create_document(a["org"], a["users"]["editor"], v, {"kind": kind, "issued_on": iso(a, -10)})
        assert "expires_on" in refused.value.errors
    for kind in ("pen_test", "questionnaire", "dpa", "insurance", "other"):
        assert risk.create_document(a["org"], a["users"]["editor"], v, {"kind": kind}).expires_on is None
    pen = VendorRiskDocument.objects.get(kind="pen_test")
    with pytest.raises(FieldErrors):  # becoming a SOC 2 report needs a date
        risk.update_document(a["org"], a["users"]["editor"], pen, {"kind": "soc2"})
    response = api(a["users"]["editor"]).post(f"vendors/{v.id}/risk-documents", {"kind": "soc2"})
    assert response.status_code == 400 and "expires_on" in response.json_body["errors"]


def test_a_report_without_a_type_has_no_expiry(a):
    v = vendor(a)
    p = profile(a, v, risk_tier="high", assurance_issued_on=iso(a, -10))
    assert p.assurance_expires_on is None and risk.assurance_status(p, today(a), 90) == "missing"
    p = profile(a, v, assurance_type="other")
    assert p.assurance_expires_on is not None
    p = profile(a, v, assurance_type=None)
    assert p.assurance_expires_on is None
    f = risk.files(a["org"], [v])[v.id]
    assert risk.gaps_of(f.profile, f.documents, today(a)) == ["no_current_assurance"]


def test_unknown_fields_are_refused(a, api):
    v = vendor(a)
    with pytest.raises(FieldErrors) as refused:
        profile(a, v, risk_tier="low", colour="red", id="x")
    assert refused.value.errors == {
        "colour": ["This field isn't accepted here."],
        "id": ["This field isn't accepted here."],
    }
    assert not VendorRiskProfile.objects.exists()
    editor = api(a["users"]["editor"])
    response = editor.patch(f"vendors/{v.id}/risk", {"risk_tier": "low", "gaps": []})
    assert response.status_code == 400 and response.json_body["errors"] == {"gaps": ["This field isn't accepted here."]}
    response = editor.post(f"vendors/{v.id}/risk-documents", {"kind": "pen_test", "status": "current"})
    assert response.status_code == 400 and response.json_body["errors"] == {
        "status": ["This field isn't accepted here."]
    }
    doc = document(a, v, kind="pen_test")
    response = editor.patch(f"vendor-risk-documents/{doc.id}", {"vendor": str(vendor(a, "Other").id)})
    assert response.status_code == 400 and "vendor" in response.json_body["errors"]


def test_a_patch_that_changes_nothing_writes_nothing(a, api):
    v = vendor(a)
    profile(a, v, risk_tier="high", notes="Same")
    doc = document(a, v, kind="pen_test", title="Same", expires_on=iso(a, 10))
    stamps = (VendorRiskProfile.objects.get().updated_at, VendorRiskDocument.objects.get().updated_at)
    events = AuditEvent.objects.count()
    editor = api(a["users"]["editor"])
    first = editor.get(f"vendors/{v.id}/risk")["ETag"]
    same = editor.patch(f"vendors/{v.id}/risk", {"risk_tier": "high", "notes": "Same"})
    assert same.status_code == 200 and same["ETag"] == first
    same_doc = editor.patch(f"vendor-risk-documents/{doc.id}", {"title": "Same", "expires_on": iso(a, 10)})
    assert same_doc.status_code == 200
    assert AuditEvent.objects.count() == events
    assert (VendorRiskProfile.objects.get().updated_at, VendorRiskDocument.objects.get().updated_at) == stamps


def test_the_superseded_filter(a, api):
    v = vendor(a)
    old = document(a, v, expires_on=iso(a, 20))
    new = document(a, v, expires_on=iso(a, 300))
    viewer = api(a["users"]["viewer"])

    def ids(query):
        response = viewer.get(f"vendor-risk-documents?{query}")
        assert response.status_code == 200, response.json_body
        return {r["id"] for r in response.json_body["results"]}

    assert ids("status=expiring") == {str(old.id)}  # absent: replaced documents match too (documented)
    assert ids("status=expiring&superseded=false") == set()
    assert ids("expiring_within=30&superseded=false") == set()
    assert ids("superseded=true") == {str(old.id)}
    assert ids("superseded=false") == {str(new.id)}
    assert viewer.get("vendor-risk-documents?superseded=maybe").status_code == 400
    exported = rows_of(viewer.get("vendor-risk-documents/export.csv?superseded=false"))
    assert [r["superseded"] for r in exported] == ["no"]


# ---- notify 0011 seeding -------------------------------------------------------------------------------------------


def _seed_migration():
    import importlib

    return importlib.import_module("notify.migrations.0011_vendor_risk_reminders")


class _Editor:
    def execute(self, sql):
        with connection.cursor() as cursor:
            cursor.execute(sql)


def test_the_migration_seeds_the_built_in_once_and_never_stops_on_a_name(a, b):
    from django.apps import apps as django_apps

    from tests.conftest import make_org

    c = make_org("orgc")
    NotificationRule.objects.filter(organization__in=[a["org"], b["org"]], builtin_key="vendor_risk").delete()

    def custom(org, name):
        NotificationRule.objects.create(organization=org, name=name, kind="reminders", source_types=["contract"])

    custom(a["org"], "vendor risk documents")  # the same name, other case
    custom(b["org"], "Vendor risk documents")
    custom(b["org"], "Vendor risk documents (built-in)")
    kept = NotificationRule.objects.get(organization=c["org"], builtin_key="vendor_risk")
    with transaction.atomic():
        _seed_migration().seed_existing(django_apps, _Editor())
    seeded = {
        r.organization_id: r
        for r in NotificationRule.objects.filter(
            builtin_key="vendor_risk", organization__in=[a["org"], b["org"], c["org"]]
        )
    }
    assert seeded[a["org"].id].name == "Vendor risk documents (built-in)"
    assert seeded[b["org"].id].name == "Vendor risk documents (built-in 2)"
    assert seeded[c["org"].id].id == kept.id  # already had it: no duplicate
    assert NotificationRule.objects.filter(organization=c["org"], builtin_key="vendor_risk").count() == 1
    for org in (a["org"], b["org"]):
        rule = seeded[org.id]
        assert rule.baselined_at is None  # the planner's first run baselines it, so the upgrade sends no burst
        assert rule.source_types == ["vendor_risk"] and rule.lead_times_days == [60, 30, 7] and rule.enabled
    with transaction.atomic():
        _seed_migration().seed_existing(django_apps, _Editor())  # a rerun adds nothing
    assert NotificationRule.objects.filter(builtin_key="vendor_risk").count() == 3
