"""Renewal queues in Needs attention: actions within the largest threshold or overdue, gaps,
routing to the IT follow-up assignee or the coordinator, counts, archived renewals, and the other organization."""

from datetime import timedelta

from catalog import services
from money import renewals
from notify.attention import UNCOUNTED


def renewal(f, name, days=None, **kw):
    """A renewal whose action deadline is `days` from today (entered), covering the fixture app."""
    today = services.org_today(f["org"])
    data = {
        "name": name,
        "auto_renew": "yes",
        "renewal_date": (today + timedelta(days=400)).isoformat(),
        "notice_value": 1,
        "notice_unit": "months",
        "responsible": str(f["people"]["dana"].id),
        "followup": f["users"]["editor"].id,
        "applications": [str(f["app"].id)],
        **kw,
    }
    if days is not None:
        data["explicit_deadline"] = (today + timedelta(days=days)).isoformat()
    return renewals.create_renewal(f["org"], f["users"]["editor"], data)


def names(rows):
    return [r["name"] for r in rows]


def test_actions_within_the_largest_threshold_or_overdue(a, api):
    renewal(a, "Overdue", -3)
    renewal(a, "Due soon", 10)
    renewal(a, "At the horizon", 90)
    renewal(a, "Beyond", 91)
    body = api(a["users"]["editor"]).get("needs-attention?scope=all").json_body
    assert names(body["renewal_actions"]) == ["Overdue", "Due soon", "At the horizon"]
    row = body["renewal_actions"][0]
    assert row["label"] == "entered" and row["label_text"] == "Entered"
    assert row["timing"]["text"] == "Overdue by 3 days" and row["steward"]["id"] == a["users"]["editor"].id
    assert row["why"].endswith("(Entered), overdue by 3 days.")
    assert body["renewal_gaps"] == []


def test_horizon_follows_the_organization_thresholds(a, api):
    a["org"].reminder_thresholds = [30, 7]
    a["org"].save()
    renewal(a, "In 40 days", 40)
    renewal(a, "In 20 days", 20)
    body = api(a["users"]["editor"]).get("needs-attention?scope=all").json_body
    assert names(body["renewal_actions"]) == ["In 20 days"]


def test_decision_without_completion_says_so(a, api):
    r = renewal(a, "Workday", -1)
    renewals.record_decision(a["org"], a["users"]["editor"], r, r.cycles.get().id, {"decision": "renew"})
    body = api(a["users"]["editor"]).get("needs-attention?scope=all").json_body
    assert body["renewal_actions"][0]["why"].endswith("Decision: Renew, not completed.")
    assert body["renewal_gaps"][0]["gaps"] == ["Decision without completion"]


def test_completed_cycle_leaves_the_actions_queue(a, api):
    r = renewal(a, "Done", 5)
    cycle = r.cycles.get()
    renewals.record_decision(a["org"], a["users"]["editor"], r, cycle.id, {"decision": "cancel"})
    renewals.record_completion(
        a["org"],
        a["users"]["editor"],
        r,
        cycle.id,
        {"completed_on": services.org_today(a["org"]).isoformat(), "completion_evidence": "Notice sent"},
    )
    body = api(a["users"]["editor"]).get("needs-attention?scope=all").json_body
    assert body["renewal_actions"] == []
    assert body["renewal_gaps"][0]["gaps"] == ["Next cycle not established"]


def test_gaps_and_entered_deadline_without_renewal_date_is_in_both(a, api):
    renewal(a, "Entered only", 20, renewal_date=None)
    renewal(a, "Nothing yet", None, renewal_date=None, auto_renew="unknown", applications=[], responsible=None)
    body = api(a["users"]["editor"]).get("needs-attention?scope=all").json_body
    assert names(body["renewal_actions"]) == ["Entered only"]
    gaps = {r["name"]: r["gaps"] for r in body["renewal_gaps"]}
    assert gaps["Entered only"] == ["No renewal date"]
    assert gaps["Nothing yet"] == ["No deadline", "Auto-renewal unknown", "No applications", "No responsible person"]
    assert body["renewal_gaps"][-1]["name"] == "Nothing yet"  # no deadline sorts last


def test_routing_to_followup_or_coordinator(a, api):
    renewal(a, "Assigned", 5)
    renewal(a, "Unassigned", 6, followup=None)
    editor = api(a["users"]["editor"]).get("needs-attention").json_body
    assert names(editor["renewal_actions"]) == ["Assigned"]
    admin = api(a["users"]["admin"]).get("needs-attention").json_body  # the coordinator catches unrouted items
    assert names(admin["renewal_actions"]) == ["Unassigned"]
    assert admin["renewal_actions"][0]["steward"]["coordinator"] is True
    unassigned = api(a["users"]["viewer"]).get("needs-attention?scope=unassigned").json_body
    assert names(unassigned["renewal_actions"]) == ["Unassigned"]
    assert "Unassigned" in names(unassigned["renewal_gaps"])  # No IT follow-up is itself a gap


def test_counts_include_renewals(a, api):
    renewal(a, "One", 5)
    body = api(a["users"]["editor"]).get("needs-attention?scope=all").json_body
    # Every queue counts: Data quality, Seat counts, Reminders
    total = sum(len(body[q]) for q in body if isinstance(body[q], list) and q not in UNCOUNTED)
    assert body["counts"]["all"] == total


def test_archived_renewals_never_appear(a, api):
    r = renewal(a, "Archived", -5, applications=[])
    renewals.archive_renewal(a["org"], a["users"]["editor"], r)
    body = api(a["users"]["editor"]).get("needs-attention?scope=all").json_body
    assert body["renewal_actions"] == [] and body["renewal_gaps"] == []


def test_other_organization_renewals_never_appear(a, b, api):
    renewal(b, "Theirs", -1, applications=[])
    body = api(a["users"]["admin"]).get("needs-attention?scope=all").json_body
    assert body["renewal_actions"] == [] and body["renewal_gaps"] == []
