"""Link confirmations: the table, the service and the API."""

from datetime import date

import pytest
from django.db import IntegrityError, connection, transaction

from catalog import services
from catalog.models import LinkConfirmation
from core.models import AuditEvent

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


# ---- The table ----------------------------------------------------------------------------------------------


def make(ctx, **kw):
    return LinkConfirmation(
        organization=ctx["org"],
        confirmed_by=ctx["people"]["dana"],
        recorded_by=ctx["users"]["editor"],
        confirmed_on=TODAY,
        evidence="x",
        confirmed_signature={},
        **kw,
    )


def test_exactly_one_of_relationship_or_integration(a):
    rel = link(a, "depends_on", a["app"], record(a, "Job", "component"))
    feed = integration(a).integration
    for kw in ({}, {"relationship": rel, "integration": feed}):
        with pytest.raises(IntegrityError), transaction.atomic():
            make(a, **kw).save()
    make(a, relationship=rel).save()
    make(a, integration=feed).save()
    assert LinkConfirmation.objects.count() == 2


def test_composite_foreign_keys_keep_a_confirmation_in_one_organization(a, b):
    theirs = link(b, "depends_on", b["app"], record(b, "Their job", "component"))
    their_feed = integration(b).integration
    for kw in (
        {"relationship": theirs},
        {"integration": their_feed},
    ):
        with pytest.raises(IntegrityError), transaction.atomic():
            make(a, **kw).save()
            connection.check_constraints()  # the foreign keys are deferred; check inside the test transaction
    ours = link(a, "depends_on", a["app"], record(a, "Job", "component"))
    with pytest.raises(IntegrityError), transaction.atomic():
        LinkConfirmation(
            organization=a["org"],
            relationship=ours,
            confirmed_by=b["people"]["dana"],  # another organization's Person
            recorded_by=a["users"]["editor"],
            confirmed_on=TODAY,
            evidence="x",
            confirmed_signature={},
        ).save()
        connection.check_constraints()


# ---- Service and API ----------------------------------------------------------------------------------------


def test_confirm_a_relationship_stores_who_when_evidence_and_signature(a, api):
    job = record(a, "Job", "component")
    with_owner(a, a["app"])
    rel = link(a, "depends_on", a["app"], job)
    client = api(a["users"]["editor"])
    response = confirm(client, "relationships", rel.id, a, confirmed_on="2026-10-01", evidence=" Email from Dana ")
    assert response.status_code == 201
    body = response.json_body
    assert body["confirmed_by"]["id"] == str(a["people"]["dana"].id) and body["evidence"] == "Email from Dana"
    assert body["recorded_by"]["id"] == a["users"]["editor"].id and body["confirmed_on"] == "2026-10-01"
    assert body["confirmed_signature"] == {
        "type": "depends_on",
        "source": str(a["app"].id),
        "source_instance": None,
        "target": str(job.id),
        "target_instance": None,
        "owner": str(a["people"]["dana"].id),  # the source record's technical owner
    }
    row = LinkConfirmation.objects.get()
    assert row.relationship_id == rel.id and row.integration_id is None and row.organization == a["org"]
    event = AuditEvent.objects.get(action="link.confirmed")
    # The confirmation is the target; its relationship is the related target, so the link's history shows it.
    assert event.target_type == "LinkConfirmation" and str(event.target_id) == body["id"]
    assert event.related_target_type == "Relationship" and event.related_target_id == rel.id
    assert event.actor == a["users"]["editor"]
    assert event.changes == {
        "link": "relationship",
        "type": "depends_on",  # the ends, so the event stays readable if the link is removed
        "source": str(a["app"].id),
        "target": str(job.id),
        "confirmation": body["id"],
        "confirmed_by": str(a["people"]["dana"].id),
        "confirmed_on": "2026-10-01",
    }
    history = client.get(f"relationships/{rel.id}/confirmations").json_body
    assert [c["id"] for c in history] == [body["id"]]


def test_confirm_an_integration_uses_its_own_technical_owner(a, api):
    feed = integration(a)
    with_owner(a, feed, "priya")
    client = api(a["users"]["admin"])
    response = confirm(client, "integrations", feed.id, a)
    assert response.status_code == 201
    signature = response.json_body["confirmed_signature"]
    assert signature["owner"] == str(a["people"]["priya"].id) and signature["type"] == "integration"
    assert signature["source"] == str(feed.integration.source_resource_id)
    row = LinkConfirmation.objects.get()
    assert row.integration_id == feed.id and row.relationship_id is None
    event = AuditEvent.objects.get(action="link.confirmed")
    assert event.target_type == "LinkConfirmation" and str(event.target_id) == response.json_body["id"]
    # The integration's record is the related target, so it shows on the integration's own audit.
    assert event.related_target_type == "Resource" and event.related_target_id == feed.id
    assert client.get(f"integrations/{feed.id}/confirmations").json_body[0]["id"] == response.json_body["id"]


def test_history_is_newest_first_and_keeps_every_row(a, api):
    rel = link(a, "depends_on", a["app"], record(a, "Job", "component"))
    client = api(a["users"]["editor"])
    for day in ("2026-08-01", "2026-10-01", "2026-09-01"):
        assert confirm(client, "relationships", rel.id, a, confirmed_on=day).status_code == 201
    dates = [c["confirmed_on"] for c in client.get(f"relationships/{rel.id}/confirmations").json_body]
    assert dates == ["2026-10-01", "2026-09-01", "2026-08-01"]


def test_validation(a, api):
    rel = link(a, "depends_on", a["app"], record(a, "Job", "component"))
    client = api(a["users"]["editor"])
    url = f"relationships/{rel.id}/confirmations"
    empty = client.post(url, {})
    assert empty.status_code == 400 and empty.headers["Content-Type"] == "application/problem+json"
    assert empty.json_body["errors"].keys() == {"confirmed_by", "evidence"}
    person = str(a["people"]["dana"].id)
    for body, field in (
        ({"confirmed_by": person, "evidence": "x" * 501}, "evidence"),
        ({"confirmed_by": person, "evidence": "ok", "confirmed_on": "2999-01-01"}, "confirmed_on"),
        ({"confirmed_by": person, "evidence": "ok", "confirmed_on": "last week"}, "confirmed_on"),
        ({"confirmed_by": person, "evidence": "ok", "confirmed_on": 5}, "confirmed_on"),
        ({"confirmed_by": "nonsense", "evidence": "ok"}, "confirmed_by"),
        ({"confirmed_by": person, "evidence": ["x"]}, "evidence"),
    ):
        response = client.post(url, body)
        assert response.status_code == 400 and field in response.json_body["errors"], body
    assert not LinkConfirmation.objects.exists()


def test_an_inactive_person_needs_a_reason(a, api):
    rel = link(a, "depends_on", a["app"], record(a, "Job", "component"))
    client = api(a["users"]["editor"])
    gone = str(a["people"]["gone"].id)
    url = f"relationships/{rel.id}/confirmations"
    refused = client.post(url, {"confirmed_by": gone, "evidence": "Left a note"})
    assert refused.status_code == 400 and "inactive_reason" in refused.json_body["errors"]
    ok = client.post(url, {"confirmed_by": gone, "evidence": "Left a note", "inactive_reason": "Wrote the interface"})
    assert ok.status_code == 201 and ok.json_body["inactive_reason"] == "Wrote the interface"


def test_removing_a_link_removes_its_confirmations_and_audits_the_count(a, api):
    rel = link(a, "depends_on", a["app"], record(a, "Job", "component"))
    client = api(a["users"]["editor"])
    confirm(client, "relationships", rel.id, a)
    assert client.delete(f"relationships/{rel.id}").status_code == 204
    assert not LinkConfirmation.objects.exists()
    assert AuditEvent.objects.get(action="relationship.deleted").changes["confirmations"] == 1
    feed = integration(a, "Doomed")
    confirm(client, "integrations", feed.id, a)
    assert api(a["users"]["admin"]).delete(f"integrations/{feed.id}").status_code == 204
    assert not LinkConfirmation.objects.exists()


def test_a_viewer_reads_but_cannot_confirm(a, api):
    rel = link(a, "depends_on", a["app"], record(a, "Job", "component"))
    feed = integration(a)
    confirm(api(a["users"]["editor"]), "relationships", rel.id, a)
    viewer = api(a["users"]["viewer"])
    assert len(viewer.get(f"relationships/{rel.id}/confirmations").json_body) == 1
    assert viewer.get(f"integrations/{feed.id}/confirmations").json_body == []
    for response in (confirm(viewer, "relationships", rel.id, a), confirm(viewer, "integrations", feed.id, a)):
        assert response.status_code == 403
    assert LinkConfirmation.objects.count() == 1


def test_other_organizations_links_are_404(a, b, api):
    theirs = link(b, "depends_on", b["app"], record(b, "Their job", "component"))
    their_feed = integration(b)
    client = api(a["users"]["admin"])
    before = AuditEvent.objects.count()
    for kind, pk in (("relationships", theirs.id), ("integrations", their_feed.id)):
        assert client.get(f"{kind}/{pk}/confirmations").status_code == 404
        assert confirm(client, kind, pk, a).status_code == 404
    assert not LinkConfirmation.objects.exists() and AuditEvent.objects.count() == before


def test_another_organizations_person_is_not_a_confirmer(a, b, api):
    rel = link(a, "depends_on", a["app"], record(a, "Job", "component"))
    response = api(a["users"]["editor"]).post(
        f"relationships/{rel.id}/confirmations",
        {"confirmed_by": str(b["people"]["dana"].id), "evidence": "x"},
    )
    assert response.status_code == 400 and response.json_body["errors"]["confirmed_by"] == [
        "Choose an existing record."
    ]
