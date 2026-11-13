from datetime import date, timedelta

from catalog import services
from catalog.state import review_state


def owned(a):
    app = a["app"]
    app.business_owner, app.technical_owner, app.support_team = a["people"]["priya"], a["people"]["dana"], a["team"]
    app.save()
    return app


def test_unreviewed_then_confirmed(a):
    app = owned(a)
    assert review_state(app, None, date.today())[0] == "unreviewed"
    review = services.record_review(
        a["org"], a["users"]["editor"], app, {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Email"}
    )
    assert review_state(app, review, date.today())[0] == "confirmed"


def test_changed_field_needs_confirmation(a):
    app = owned(a)
    review = services.record_review(
        a["org"], a["users"]["editor"], app, {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Email"}
    )
    app.technical_owner = a["people"]["priya"]
    state, why = review_state(app, review, date.today())
    assert state == "needs_confirmation" and "Technical owner changed" in why


def test_overdue_and_precedence(a):
    app = owned(a)
    review = services.record_review(
        a["org"], a["users"]["editor"], app, {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Email"}
    )
    later = review.next_review_date + timedelta(days=1)
    assert review_state(app, review, later)[0] == "overdue"
    app.lifecycle = "deprecated"
    state, why = review_state(app, review, later)
    assert state == "needs_confirmation" and "Next review was" in why


def test_inactive_owner_needs_confirmation(a):
    app = owned(a)
    review = services.record_review(
        a["org"], a["users"]["editor"], app, {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Email"}
    )
    a["people"]["dana"].active = False
    a["people"]["dana"].save()
    app.refresh_from_db()
    assert review_state(app, review, date.today())[0] == "needs_confirmation"


def test_review_validation(a, api):
    client = api(a["users"]["editor"])
    url = f"applications/{a['app'].id}/reviews"
    future = (services.org_today(a["org"]) + timedelta(days=1)).isoformat()
    bad = client.post(url, {"confirmed_by": str(a["people"]["gone"].id), "evidence": "", "confirmation_date": future})
    assert bad.status_code == 400
    assert {"inactive_reason", "evidence", "confirmation_date"} <= set(bad.json_body["errors"])
    ok = client.post(
        url,
        {
            "confirmed_by": str(a["people"]["gone"].id),
            "inactive_reason": "Confirmed before leaving",
            "evidence": "Email",
        },
    )
    assert ok.status_code == 201


def test_inactive_people_cannot_be_owners(a, api):
    client = api(a["users"]["editor"])
    response = client.patch(f"applications/{a['app'].id}", {"technical_owner": str(a["people"]["gone"].id)})
    assert response.status_code == 400 and "technical_owner" in response.json_body["errors"]


def test_needs_attention_reports_an_empty_catalog(a, api):
    from catalog.models import Application, Resource

    Application.objects.filter(resource__organization=a["org"]).delete()
    Resource.objects.filter(organization=a["org"]).delete()
    body = api(a["users"]["viewer"]).get("needs-attention?scope=all").json_body
    assert body["records"] == 0 and body["ownership_gaps"] == [] and body["reviews"] == []
