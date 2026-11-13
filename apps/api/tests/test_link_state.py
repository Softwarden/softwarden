"""The derived link state."""

from datetime import UTC, date, datetime

import pytest

from catalog import services
from catalog.models import Integration, LinkConfirmation, Relationship, Resource
from catalog.state import link_state
from core import dates

TODAY = date(2026, 11, 3)


def record(ctx, name, kind="application"):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": "active"}
    )


def link(ctx, rel_type, source, target, **extra):
    return services.create_relationship(
        ctx["org"],
        ctx["users"]["editor"],
        {"type": rel_type, "source": str(source.id), "target": str(target.id), **extra},
    )


def integration(ctx, name="Nightly feed"):
    ends = [record(ctx, f"{name} source"), record(ctx, f"{name} target", "component")]
    return services.create_integration(
        ctx["org"],
        ctx["users"]["editor"],
        {"name": name, "lifecycle": "active", "source": str(ends[0].id), "target": str(ends[1].id)},
    )


def confirm(client, kind, pk, ctx, **extra):
    body = {"confirmed_by": str(ctx["people"]["dana"].id), "evidence": "Email from Dana", **extra}
    return client.post(f"{kind}/{pk}/confirmations", body)


def with_owner(ctx, resource, person="dana"):
    services.update_resource(
        ctx["org"], ctx["users"]["editor"], resource, {"technical_owner": str(ctx["people"][person].id)}
    )


# ---- The derived state --------------------------------------------------------------------------------------


class Fake:
    """Just the members link_state reads from a LinkConfirmation."""

    def __init__(self, confirmed_on, signature):
        self.confirmed_on, self.confirmed_signature = confirmed_on, signature


SIG = {
    "type": "depends_on",
    "source": "s",
    "source_instance": None,
    "target": "t",
    "target_instance": None,
    "owner": "o",
}


def test_state_at_the_interval_boundary():
    confirmed = Fake(date(2026, 6, 1), SIG)
    assert link_state(SIG, None, TODAY, 90) == ("unconfirmed", "Never confirmed.")
    assert link_state(SIG, confirmed, date(2026, 8, 30), 90) == (
        "confirmed",
        "Confirmed 2026-06-01.",
    )  # day 90: still good
    assert link_state(SIG, confirmed, date(2026, 8, 31), 90) == (
        "needs_verification",
        "Confirmed 2026-06-01; interval 90 days.",  # day 91
    )
    assert link_state(SIG, confirmed, date(2026, 6, 1), 90)[0] == "confirmed"
    assert link_state(SIG, confirmed, date(2026, 6, 2), 1)[0] == "confirmed"
    assert link_state(SIG, confirmed, date(2026, 6, 3), 1)[0] == "needs_verification"


@pytest.mark.parametrize(
    "key,label",
    [
        ("type", "Type"),
        ("source", "Source"),
        ("source_instance", "Source instance"),
        ("target", "Target"),
        ("target_instance", "Target instance"),
        ("owner", "Owner"),
    ],
)
def test_each_signature_member_makes_a_link_need_verification(key, label):
    confirmed = Fake(TODAY, SIG)
    assert link_state({**SIG, key: "changed"}, confirmed, TODAY, 90) == (
        "needs_verification",
        f"{label} changed since confirmation.",
    )


def test_why_names_several_changes_and_an_old_confirmation():
    confirmed = Fake(date(2026, 1, 1), SIG)
    state, why = link_state({**SIG, "target": "x", "owner": None}, confirmed, TODAY, 90)
    assert state == "needs_verification"
    assert why == "Target and owner changed since confirmation. Confirmed 2026-01-01; interval 90 days."


def test_the_date_is_judged_in_the_organization_timezone(a, api, monkeypatch):
    rel = link(a, "depends_on", a["app"], record(a, "Job", "component"))
    client = api(a["users"]["viewer"])
    LinkConfirmation.objects.create(
        organization=a["org"],
        relationship=rel,
        confirmed_by=a["people"]["dana"],
        recorded_by=a["users"]["editor"],
        confirmed_on=date(2026, 6, 1),
        evidence="x",
        confirmed_signature={
            "type": "depends_on",
            "source": str(a["app"].id),
            "source_instance": None,
            "target": str(rel.target_id),
            "target_instance": None,
            "owner": None,
        },
    )

    class At(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 30, 20, 0, tzinfo=UTC).astimezone(tz)  # 20:00 UTC on day 90

    monkeypatch.setattr(dates, "datetime", At)
    states = {}
    for zone in ("UTC", "America/Los_Angeles", "Pacific/Auckland"):  # Auckland is already on 31 August
        a["org"].timezone = zone
        a["org"].save()
        states[zone] = client.get(f"relationships/{rel.id}").json_body["link_state"]
    assert states == {"UTC": "confirmed", "America/Los_Angeles": "confirmed", "Pacific/Auckland": "needs_verification"}


def test_state_on_relationship_and_integration_responses(a, api):
    editor = api(a["users"]["editor"])
    job = record(a, "Job", "component")
    with_owner(a, a["app"])
    rel = link(a, "depends_on", a["app"], job)
    feed = integration(a)
    assert editor.get(f"relationships/{rel.id}").json_body["link_state"] == "unconfirmed"
    assert editor.get(f"integrations/{feed.id}").json_body["link_state"] == "unconfirmed"
    confirm(editor, "relationships", rel.id, a)
    confirm(editor, "integrations", feed.id, a)
    shown = editor.get(f"relationships/{rel.id}").json_body
    assert shown["link_state"] == "confirmed" and shown["last_confirmed_on"] == services.org_today(a["org"]).isoformat()
    assert shown["link_why"].startswith("Confirmed ")
    assert editor.get("relationships").json_body[0]["link_state"] == "confirmed"
    assert editor.get("integrations").json_body["results"][0]["link_state"] == "confirmed"
    assert editor.get(f"integrations/{feed.id}").json_body["link_state"] == "confirmed"


def test_a_change_to_the_link_or_its_owner_needs_verification_and_a_new_confirmation_clears_it(a, api):
    editor = api(a["users"]["editor"])
    job, other = record(a, "Job", "component"), record(a, "Other")
    rel = link(a, "depends_on", a["app"], job)
    confirm(editor, "relationships", rel.id, a)
    state = lambda: editor.get(f"relationships/{rel.id}").json_body  # noqa: E731
    assert state()["link_state"] == "confirmed"
    # a rename or a purpose edit doesn't count
    editor.patch(f"relationships/{rel.id}", {"purpose": "Posts invoices"})
    services.update_resource(a["org"], a["users"]["editor"], job, {"name": "Job renamed"})
    assert state()["link_state"] == "confirmed"
    # the target changes
    editor.patch(f"relationships/{rel.id}", {"target": str(other.id)})
    assert state()["link_state"] == "needs_verification" and state()["link_why"] == "Target changed since confirmation."
    confirm(editor, "relationships", rel.id, a)
    assert state()["link_state"] == "confirmed"
    # the source record's technical owner changes
    with_owner(a, a["app"], "priya")
    assert state()["link_state"] == "needs_verification" and state()["link_why"] == "Owner changed since confirmation."
    # the type changes
    confirm(editor, "relationships", rel.id, a)
    editor.patch(f"relationships/{rel.id}", {"type": "replaces"})
    assert state()["link_why"] == "Type changed since confirmation."


def test_an_integration_needs_verification_when_an_end_or_its_owner_changes(a, api):
    editor = api(a["users"]["editor"])
    feed = integration(a)
    confirm(editor, "integrations", feed.id, a)
    url = f"integrations/{feed.id}"
    assert editor.get(url).json_body["link_state"] == "confirmed"
    editor.patch(url, {"target": str(record(a, "New target").id)})
    assert editor.get(url).json_body["link_why"] == "Target changed since confirmation."
    confirm(editor, "integrations", feed.id, a)
    with_owner(a, Resource.objects.get(id=feed.id))  # a fresh object: `feed` still holds the old target
    assert editor.get(url).json_body["link_why"] == "Owner changed since confirmation."


def test_a_record_review_never_confirms_a_link(a, api):
    editor = api(a["users"]["editor"])
    job = record(a, "Job", "component")
    rel = link(a, "depends_on", a["app"], job)
    feed = integration(a)
    review = {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Email from Dana"}
    for resource, kind in ((a["app"], "applications"), (job, "components")):
        assert editor.post(f"{kind}/{resource.id}/reviews", review).status_code == 201
    assert not LinkConfirmation.objects.exists()
    assert editor.get(f"relationships/{rel.id}").json_body["link_state"] == "unconfirmed"
    assert editor.get(f"integrations/{feed.id}").json_body["link_state"] == "unconfirmed"
    # and a link confirmation doesn't review a record
    confirm(editor, "relationships", rel.id, a)
    assert editor.get(f"applications/{a['app'].id}").json_body["review_state"] == "confirmed"  # from the review above
    assert (
        editor.get(f"components/{job.id}").json_body["review_state"] == "confirmed"
    )  # unchanged by the link confirmation


def test_confirming_does_not_touch_a_record_that_was_never_reviewed(a, api):
    editor = api(a["users"]["editor"])
    job = record(a, "Job", "component")
    rel = link(a, "depends_on", a["app"], job)
    confirm(editor, "relationships", rel.id, a)
    assert editor.get(f"applications/{a['app'].id}").json_body["review_state"] == "unreviewed"
    assert editor.get(f"components/{job.id}").json_body["review_state"] == "unreviewed"
    assert Relationship.objects.count() == 1 and Integration.objects.count() == 0
