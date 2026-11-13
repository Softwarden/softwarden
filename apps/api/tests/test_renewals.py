"""Renewals API."""

import uuid
from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction

from catalog import services
from catalog.models import Vendor
from core.models import AuditEvent
from money import renewals
from money.models import RenewalCycle, RenewalCycleApplication, RenewalObligation


def app(org_fixture, name, lifecycle="active"):
    return services.create_resource(
        org_fixture["org"], org_fixture["users"]["editor"], {"name": name, "lifecycle": lifecycle}
    )


def body(a, **kw):
    return {
        "name": "Workday subscription",
        "vendor": str(Vendor.objects.create(organization=a["org"], name=f"Workday Inc. {uuid.uuid4().hex[:6]}").id),
        "responsible": str(a["people"]["priya"].id),
        "followup": a["users"]["editor"].id,
        "auto_renew": "yes",
        "renewal_date": "2027-03-31",
        "notice_value": 2,
        "notice_unit": "months",
        "term_value": 1,
        "term_unit": "years",
        "applications": [str(a["app"].id)],
        **kw,
    }


def test_create_with_first_cycle_and_derived_deadline(a, api):
    response = api(a["users"]["editor"]).post("renewals", body(a))
    assert response.status_code == 201, response.json_body
    r = response.json_body
    assert r["name"] == "Workday subscription" and r["auto_renew"] == "yes"
    assert r["responsible"]["name"] == "Priya Raman" and r["followup"]["id"] == a["users"]["editor"].id
    assert r["cycle"]["sequence"] == 1 and r["cycle"]["open"] is True
    assert [x["name"] for x in r["cycle"]["applications"]] == ["orga App"]
    assert r["deadline"]["date"] == "2027-01-31" and r["deadline"]["label"] == "calculated"
    assert r["deadline"]["label_text"] == "Calculated"
    assert "minus notice period 2 months" in r["deadline"]["explanation"]
    assert r["gaps"] == [] and r["history"] == []
    event = AuditEvent.objects.get(action="renewal.created")
    assert event.changes["notice"] == [2, "months"] and event.changes["applications"] == [str(a["app"].id)]


def test_missing_values_are_allowed_and_shown_as_gaps(a, api):
    r = api(a["users"]["editor"]).post("renewals", {"name": "Adobe Sign", "auto_renew": "yes"}).json_body
    assert r["deadline"]["date"] is None
    assert r["gaps"] == ["No deadline", "No applications", "No responsible person", "No IT follow-up"]


def test_validation(a, b, api):
    client = api(a["users"]["editor"])
    bad = client.post(
        "renewals",
        {
            "name": "",
            "auto_renew": "maybe",
            "renewal_date": "31/03/2027",
            "notice_value": 2,
            "notice_unit": "weeks",
            "term_value": "x",
            "followup": a["users"]["viewer"].id,
            "responsible": str(a["people"]["gone"].id),
            "applications": [str(b["app"].id)],
        },
    )
    assert bad.status_code == 400
    errors = bad.json_body["errors"]
    assert set(errors) == {
        "name",
        "auto_renew",
        "renewal_date",
        "notice_unit",
        "term_value",
        "followup",
        "responsible",
        "applications",
    }
    assert errors["followup"] == ["Choose an Admin or Editor in this organization."]
    assert errors["responsible"] == ["Choose an active person."]
    component = services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Job", "kind": "component", "lifecycle": "active"}
    )
    only_apps = client.post("renewals", {"name": "X", "applications": [str(component.id)]})
    assert "applications only" in only_apps.json_body["errors"]["applications"][0]
    assert RenewalObligation.objects.count() == 0


def test_one_obligation_covers_many_applications(a, api):
    apps = [str(a["app"].id), str(app(a, "Benefits").id), str(app(a, "Payroll").id)]
    r = api(a["users"]["editor"]).post("renewals", body(a, applications=apps)).json_body
    assert len(r["cycle"]["applications"]) == 3
    assert RenewalCycle.objects.count() == 1  # one cycle, so one reminder per threshold


def test_patch_obligation_and_cycle_separately(a, api):
    client = api(a["users"]["editor"])
    r = client.post("renewals", body(a)).json_body
    refused = client.patch(f"renewals/{r['id']}", {"renewal_date": "2027-06-30"})
    assert refused.status_code == 400 and "renewal_date" in refused.json_body["errors"]
    assert client.patch(f"renewals/{r['id']}", {"auto_renew": "no"}).json_body["deadline"]["label"] == (
        "renewal_action"
    )
    changed = client.patch(
        f"renewals/{r['id']}/cycles/{r['cycle']['id']}",
        {"explicit_deadline": "2026-12-15", "applications": []},
    ).json_body
    assert changed["deadline"]["date"] == "2026-12-15" and changed["deadline"]["label"] == "entered"
    assert "No applications" in changed["gaps"]
    assert AuditEvent.objects.filter(action="renewal_cycle.updated").count() == 1
    assert AuditEvent.objects.get(action="renewal.updated").changes == {"auto_renew": ["yes", "no"]}


def test_decision_is_not_completion(a, api):
    client = api(a["users"]["editor"])
    r = client.post("renewals", body(a)).json_body
    url = f"renewals/{r['id']}/cycles/{r['cycle']['id']}"
    decided = client.post(f"{url}/decision", {"decision": "cancel", "decision_notes": "Moving to Zoho"}).json_body
    assert decided["cycle"]["decision"] == "cancel" and decided["cycle"]["open"] is True
    assert decided["cycle"]["decided_on"] == services.org_today(a["org"]).isoformat()
    assert decided["deadline"]["timing"] is not None  # still an action
    future = (services.org_today(a["org"]) + timedelta(days=2)).isoformat()
    assert client.post(f"{url}/decision", {"decision": "renew", "decided_on": future}).status_code == 400
    assert client.post(f"{url}/decision", {"decision": "maybe"}).status_code == 400


def test_completion_needs_decision_date_and_evidence(a, api):
    client = api(a["users"]["editor"])
    r = client.post("renewals", body(a)).json_body
    url = f"renewals/{r['id']}/cycles/{r['cycle']['id']}"
    refused = client.post(f"{url}/completion", {})
    assert set(refused.json_body["errors"]) == {"decision", "completed_on", "completion_evidence"}
    client.post(f"{url}/decision", {"decision": "renew"})
    today = services.org_today(a["org"]).isoformat()
    done = client.post(f"{url}/completion", {"completed_on": today, "completion_evidence": "PO 44120"}).json_body
    assert done["cycle"]["open"] is False and done["cycle"]["completion_evidence"] == "PO 44120"
    assert done["deadline"]["timing"] is None
    assert done["gaps"] == ["Next cycle not established"]
    # A completed cycle is history.
    assert client.post(f"{url}/decision", {"decision": "cancel"}).status_code == 409
    assert client.patch(url, {"renewal_date": "2030-01-01"}).status_code == 409


def test_next_cycle_is_explicit_and_keeps_history(a, api):
    client = api(a["users"]["editor"])
    benefits = app(a, "Benefits Portal")
    r = client.post("renewals", body(a, applications=[str(a["app"].id), str(benefits.id)])).json_body
    url = f"renewals/{r['id']}/cycles"
    assert client.post(url, {}).status_code == 409  # the current cycle isn't complete
    first = r["cycle"]["id"]
    client.post(f"{url}/{first}/decision", {"decision": "renew", "decision_notes": "HR confirmed"})
    today = services.org_today(a["org"]).isoformat()
    client.post(f"{url}/{first}/completion", {"completed_on": today, "completion_evidence": "PO 39012"})
    nxt = client.post(url, {})
    assert nxt.status_code == 201
    n = nxt.json_body
    assert n["cycle"]["sequence"] == 2 and n["cycle"]["renewal_date"] == "2028-03-31"
    assert n["cycle"]["notice"] == {"value": 2, "unit": "months"} and n["cycle"]["term"] == {
        "value": 1,
        "unit": "years",
    }
    assert {x["name"] for x in n["cycle"]["applications"]} == {"orga App", "Benefits Portal"}
    assert n["cycle"]["decision"] is None and n["deadline"]["date"] == "2028-01-31"
    # Editing the new cycle leaves the old one as it was.
    client.patch(f"{url}/{n['cycle']['id']}", {"applications": [str(benefits.id)], "renewal_date": "2028-04-30"})
    detail = client.get(f"renewals/{r['id']}").json_body
    old = detail["history"][0]
    assert old["sequence"] == 1 and old["renewal_date"] == "2027-03-31" and old["decision"] == "renew"
    assert old["completion_evidence"] == "PO 39012" and len(old["applications"]) == 2
    assert [x["name"] for x in detail["cycle"]["applications"]] == ["Benefits Portal"]
    assert client.post(url, {}).status_code == 409


def test_next_cycle_values_can_be_overridden(a, api):
    client = api(a["users"]["editor"])
    r = client.post("renewals", body(a, term_value=None, term_unit="")).json_body
    url = f"renewals/{r['id']}/cycles"
    first = r["cycle"]["id"]
    client.post(f"{url}/{first}/decision", {"decision": "renegotiate"})
    client.post(
        f"{url}/{first}/completion",
        {"completed_on": services.org_today(a["org"]).isoformat(), "completion_evidence": "New terms signed"},
    )
    n = client.post(url, {"renewal_date": "2028-06-30", "notice_value": 90, "notice_unit": "days"}).json_body
    assert n["cycle"]["renewal_date"] == "2028-06-30" and n["deadline"]["date"] == "2028-04-01"


def test_all_covered_applications_retired(a, api):
    retired = app(a, "Old portal", "retired")
    r = api(a["users"]["editor"]).post("renewals", body(a, applications=[str(retired.id)])).json_body
    assert r["gaps"] == ["All covered applications retired: confirm cancellation"]


def test_retiring_an_application_never_rewrites_cycles(a, api):
    client = api(a["users"]["editor"])
    r = client.post("renewals", body(a)).json_body
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"lifecycle": "retired"})
    after = client.get(f"renewals/{r['id']}").json_body
    assert [x["lifecycle"] for x in after["cycle"]["applications"]] == ["retired"]
    assert after["gaps"] == ["All covered applications retired: confirm cancellation"]


def test_covered_application_delete_is_refused(a, api):
    doomed = app(a, "Doomed")
    api(a["users"]["editor"]).post("renewals", body(a, applications=[str(doomed.id)]))
    refused = api(a["users"]["admin"]).delete(f"applications/{doomed.id}")
    assert refused.status_code == 409
    assert "1 renewal cycle" in refused.json_body["detail"]


def test_vendor_is_protected(a, api):
    """People are never deleted, only anonymized; a vendor in use can't be deleted."""
    r = api(a["users"]["editor"]).post("renewals", body(a)).json_body
    refused = api(a["users"]["admin"]).delete(f"vendors/{r['vendor']['id']}")
    assert refused.status_code == 409
    assert "1 application, renewal or contract uses it (Workday subscription)" in refused.json_body["detail"]


def test_archive_restore_and_delete(a, api):
    client = api(a["users"]["editor"])
    r = client.post("renewals", body(a)).json_body
    assert client.post(f"renewals/{r['id']}/archive").json_body["archived"] is True
    assert client.get("renewals").json_body == []
    assert [x["id"] for x in client.get("renewals?archived=true").json_body] == [r["id"]]
    assert client.post(f"renewals/{r['id']}/restore").json_body["archived"] is False
    admin = api(a["users"]["admin"])
    client.post(f"renewals/{r['id']}/cycles/{r['cycle']['id']}/decision", {"decision": "renew"})
    refused = admin.delete(f"renewals/{r['id']}")
    assert refused.status_code == 409 and "1 recorded decision" in refused.json_body["detail"]
    fresh = client.post("renewals", body(a, name="Spare")).json_body
    assert admin.delete(f"renewals/{fresh['id']}").status_code == 204
    assert not RenewalObligation.objects.filter(id=fresh["id"]).exists()
    assert not RenewalCycleApplication.objects.filter(cycle_id=fresh["cycle"]["id"]).exists()


def test_list_filters_and_order(a, api):
    client = api(a["users"]["editor"])
    today = services.org_today(a["org"])

    def make(name, deadline, **kw):
        extra = {"explicit_deadline": deadline.isoformat()} if deadline else {}
        return client.post("renewals", body(a, name=name, renewal_date=None, **extra, **kw)).json_body

    overdue = make("Zoom Pro", today - timedelta(days=3))
    soon = make("Workday", today + timedelta(days=20), applications=[str(app(a, "Workday HR").id)])
    later = make("Miro", today + timedelta(days=100), auto_renew="no", followup=None)
    none = make("Adobe Sign", None, auto_renew="unknown")
    listed = client.get("renewals").json_body
    assert [x["name"] for x in listed] == ["Zoom Pro", "Workday", "Miro", "Adobe Sign"]
    assert listed[0]["deadline"]["timing"]["text"] == "Overdue by 3 days"

    def names(query):
        response = client.get(f"renewals?{query}")
        assert response.status_code == 200, response.json_body
        return {x["name"] for x in response.json_body}

    assert names("due_within=30") == {"Zoom Pro", "Workday"}
    assert names("overdue=true") == {"Zoom Pro"}
    assert names("auto_renew=no") == {"Miro"}
    assert names("followup=none") == {"Miro"}
    assert names(f"followup={a['users']['editor'].id}") == {"Zoom Pro", "Workday", "Adobe Sign"}
    assert names("q=workday hr") == {"Workday"}
    assert names(f"application={a['app'].id}") == {"Zoom Pro", "Miro", "Adobe Sign"}
    assert names("has_gaps=false") == set()  # every one lacks a renewal date or more
    assert names("decision=none") == {"Zoom Pro", "Workday", "Miro", "Adobe Sign"}
    client.post(f"renewals/{soon['id']}/cycles/{soon['cycle']['id']}/decision", {"decision": "renew"})
    assert names("decision=renew") == {"Workday"}
    # Mine: assigned to me, plus unassigned ones for the coordinator (the admin in fixtures).
    assert names("mine=true") == {"Zoom Pro", "Workday", "Adobe Sign"}
    assert {x["name"] for x in api(a["users"]["admin"]).get("renewals?mine=true").json_body} == {"Miro"}
    bad = client.get("renewals?overdue=yes&due_within=x&auto_renew=maybe&decision=x&followup=x")
    assert set(bad.json_body["errors"]) == {"overdue", "due_within", "auto_renew", "decision", "followup"}
    assert overdue and later and none


def test_preview(a, api):
    client = api(a["users"]["viewer"])
    p = client.get("renewals/preview?auto_renew=yes&renewal_date=2027-03-31&notice_value=1&notice_unit=months")
    assert p.json_body["date"] == "2027-02-28" and p.json_body["label"] == "calculated"
    assert client.get("renewals/preview?renewal_date=nope").status_code == 400


def test_other_organization_is_404(a, b, api):
    theirs = renewals.create_renewal(b["org"], b["users"]["editor"], {"name": "Theirs", "auto_renew": "yes"})
    cycle = theirs.cycles.get()
    client = api(a["users"]["admin"])
    for method, url, payload in [
        ("get", f"renewals/{theirs.id}", None),
        ("patch", f"renewals/{theirs.id}", {"name": "Hijacked"}),
        ("delete", f"renewals/{theirs.id}", None),
        ("post", f"renewals/{theirs.id}/archive", {}),
        ("post", f"renewals/{theirs.id}/cycles", {}),
        ("patch", f"renewals/{theirs.id}/cycles/{cycle.id}", {"renewal_date": "2030-01-01"}),
        ("post", f"renewals/{theirs.id}/cycles/{cycle.id}/decision", {"decision": "renew"}),
        ("post", f"renewals/{theirs.id}/cycles/{cycle.id}/completion", {}),
    ]:
        response = getattr(client, method)(url, payload) if payload is not None else getattr(client, method)(url)
        assert response.status_code == 404, (method, url)
    assert client.get("renewals").json_body == []
    theirs.refresh_from_db()
    assert theirs.name == "Theirs" and theirs.archived_at is None
    # A cycle of theirs under one of our obligations is also a 404.
    ours = client.post("renewals", body(a)).json_body
    assert client.post(f"renewals/{ours['id']}/cycles/{cycle.id}/decision", {"decision": "renew"}).status_code == 404


def test_database_keeps_cycles_and_coverage_in_one_organization(a, b):
    ours = renewals.create_renewal(a["org"], a["users"]["editor"], {"name": "Ours"})
    with pytest.raises(IntegrityError), transaction.atomic():
        RenewalCycleApplication.objects.create(organization=a["org"], cycle=ours.cycles.get(), application=b["app"])
        connection.check_constraints()  # the composite keys are deferred; check inside the test transaction
    with pytest.raises(IntegrityError), transaction.atomic():
        RenewalCycle.objects.create(organization=b["org"], obligation=ours, sequence=2, decision="renew")
        connection.check_constraints()


def test_only_one_open_cycle(a):
    ours = renewals.create_renewal(a["org"], a["users"]["editor"], {"name": "Ours"})
    with pytest.raises(IntegrityError), transaction.atomic():
        RenewalCycle.objects.create(organization=a["org"], obligation=ours, sequence=2)


def test_audit_activity_names_every_cycle_event(a, b, api):
    client = api(a["users"]["editor"])
    r = client.post("renewals", body(a)).json_body
    cycle = f"renewals/{r['id']}/cycles/{r['cycle']['id']}"
    client.patch(f"renewals/{r['id']}", {"followup": a["users"]["admin"].id})
    client.patch(cycle, {"notice_value": 3, "notice_unit": "months"})
    client.post(f"{cycle}/decision", {"decision": "renew"})
    today = services.org_today(a["org"]).isoformat()
    client.post(f"{cycle}/completion", {"completed_on": today, "completion_evidence": "PO 1"})
    client.post(f"renewals/{r['id']}/cycles", {})
    events = client.get(f"renewals/{r['id']}/audit").json_body["results"]
    rows = [(e["label"], e["summary"]) for e in events]
    assert rows[0] == (
        "Next cycle established",
        "Cycle 2; Renewal date 2028-03-31; Notice period 3 months; Covers orga App",
    )
    assert ("Cycle completed", f"Renew, completed {today}") in rows
    assert ("Decision recorded", "Decision: none → renew") in rows
    assert ("Cycle terms changed", "Notice period: 2 months → 3 months") in rows
    assert ("Renewal changed", "IT follow-up: editor@orga.test → admin@orga.test") in rows
    assert rows[-1] == ("Renewal recorded", "Renewal date 2027-03-31; Notice period 2 months; Covers orga App")
    assert all(e["target"] == {"kind": "renewal", "id": r["id"], "name": "Workday subscription"} for e in events)
    log = api(a["users"]["admin"]).get("audit-events?action=renewal_cycle").json_body["results"]
    assert len(log) == 4
    theirs = renewals.create_renewal(b["org"], b["users"]["editor"], {"name": "Theirs"})
    assert client.get(f"renewals/{theirs.id}/audit").status_code == 404
