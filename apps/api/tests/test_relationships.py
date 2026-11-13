"""Relationships: the table's constraints, the service rules, the API, and the delete guards."""

from catalog import services
from catalog.models import Relationship
from core.models import AuditEvent


def record(ctx, name, kind="application"):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": "active"}
    )


def integration(ctx, name="Nightly feed"):
    ends = [record(ctx, f"{name} source"), record(ctx, f"{name} target", "component")]
    return services.create_integration(
        ctx["org"],
        ctx["users"]["editor"],
        {"name": name, "lifecycle": "active", "source": str(ends[0].id), "target": str(ends[1].id)},
    )


def link(ctx, rel_type, source, target, **extra):
    return services.create_relationship(
        ctx["org"],
        ctx["users"]["editor"],
        {"type": rel_type, "source": str(source.id), "target": str(target.id), **extra},
    )


def instance(ctx, application, name="Production"):
    return services.create_instance(ctx["org"], ctx["users"]["editor"], application, {"name": name})


def post(client, body):
    return client.post("relationships", body)


# ---- Service rules ------------------------------------------------------------------------------------------


def test_create_edit_delete_through_the_api_are_audited(a, api):
    client = api(a["users"]["editor"])
    other = record(a, "Billing")
    prod = instance(a, a["app"])
    created = post(
        client,
        {
            "type": "depends_on",
            "source": str(a["app"].id),
            "source_instance": str(prod.id),
            "target": str(other.id),
            "purpose": " Posts invoices ",
        },
    )
    assert created.status_code == 201
    body = created.json_body
    assert body["type"] == "depends_on" and body["purpose"] == "Posts invoices"
    assert body["source"] == {
        "id": str(a["app"].id),
        "kind": "application",
        "name": "orga App",
        "archived": False,
        "instance": {"id": str(prod.id), "name": "Production"},
    }
    assert body["target"]["instance"] is None and body["created_by"]["id"] == a["users"]["editor"].id
    event = AuditEvent.objects.get(action="relationship.created")
    assert str(event.target_id) == body["id"] and event.target_type == "Relationship"
    assert event.organization == a["org"] and event.actor == a["users"]["editor"]
    assert event.changes["source_instance"] == str(prod.id) and event.changes["target"] == str(other.id)

    edited = client.patch(f"relationships/{body['id']}", {"purpose": "Posts invoices nightly", "source_instance": None})
    assert edited.status_code == 200 and edited.json_body["source"]["instance"] is None
    changes = AuditEvent.objects.get(action="relationship.updated").changes
    assert changes == {
        "purpose": ["Posts invoices", "Posts invoices nightly"],
        "source_instance": [str(prod.id), None],
    }

    assert client.delete(f"relationships/{body['id']}").status_code == 204
    assert not Relationship.objects.exists()
    deleted = AuditEvent.objects.get(action="relationship.deleted")
    assert str(deleted.target_id) == body["id"] and deleted.changes["type"] == "depends_on"


def test_list_and_filters(a, api):
    client = api(a["users"]["viewer"])
    billing, job = record(a, "Billing"), record(a, "Job", "component")
    link(a, "depends_on", a["app"], billing)
    link(a, "depends_on", job, a["app"])
    link(a, "replaces", billing, record(a, "Billing 2"))
    assert len(client.get("relationships").json_body) == 3
    mine = client.get(f"relationships?resource={a['app'].id}").json_body
    assert {(r["source"]["name"], r["target"]["name"]) for r in mine} == {("orga App", "Billing"), ("Job", "orga App")}
    only = client.get(f"relationships?type=replaces&resource={billing.id}").json_body
    assert [r["type"] for r in only] == ["replaces"]
    assert client.get("relationships?resource=nonsense").status_code == 400


def test_depends_on_accepts_applications_and_components_at_both_ends(a):
    job, other_job = record(a, "Job", "component"), record(a, "Other job", "component")
    link(a, "depends_on", a["app"], job)
    link(a, "depends_on", job, a["app"])
    link(a, "depends_on", job, other_job)
    assert Relationship.objects.count() == 3


def test_an_integration_is_never_a_depends_on_endpoint(a, api):
    client = api(a["users"]["editor"])
    feed = integration(a)
    for source, target, field in ((a["app"], feed, "target"), (feed, a["app"], "source")):
        response = post(client, {"type": "depends_on", "source": str(source.id), "target": str(target.id)})
        assert response.status_code == 400 and field in response.json_body["errors"]
    assert not Relationship.objects.exists()


def test_implements_goes_from_a_component_to_an_integration(a, api):
    client = api(a["users"]["editor"])
    feed, job = integration(a), record(a, "Job", "component")
    assert post(client, {"type": "implements", "source": str(job.id), "target": str(feed.id)}).status_code == 201
    for source, target, field in (
        (a["app"], feed, "source"),  # an application doesn't implement
        (job, a["app"], "target"),  # nothing implements an application
        (feed, job, "source"),
    ):
        response = post(client, {"type": "implements", "source": str(source.id), "target": str(target.id)})
        assert response.status_code == 400 and field in response.json_body["errors"], (source.name, target.name)


def test_replaces_needs_the_same_kind(a, api):
    client = api(a["users"]["editor"])
    job, new_app = record(a, "Job", "component"), record(a, "New app")
    mixed = post(client, {"type": "replaces", "source": str(job.id), "target": str(new_app.id)})
    assert mixed.status_code == 400
    assert "can only replace another component" in mixed.json_body["errors"]["target"][0]
    assert post(client, {"type": "replaces", "source": str(new_app.id), "target": str(a["app"].id)}).status_code == 201
    assert (
        post(
            client, {"type": "replaces", "source": str(job.id), "target": str(record(a, "Job 2", "component").id)}
        ).status_code
        == 201
    )


def test_replaces_cycles_are_rejected_but_depends_on_cycles_are_allowed(a, api):
    client = api(a["users"]["editor"])
    two, three = record(a, "Two"), record(a, "Three")
    one = a["app"]
    link(a, "replaces", one, two)
    link(a, "replaces", two, three)
    for source, target in ((two, one), (three, one), (one, one)):
        response = post(client, {"type": "replaces", "source": str(source.id), "target": str(target.id)})
        assert response.status_code == 400 and "replace itself" in response.json_body["errors"]["target"][0]
    assert post(client, {"type": "replaces", "source": str(one.id), "target": str(three.id)}).status_code == 201
    # depends_on may loop, through others or directly back.
    link(a, "depends_on", one, two)
    link(a, "depends_on", two, three)
    assert post(client, {"type": "depends_on", "source": str(three.id), "target": str(one.id)}).status_code == 201


def test_editing_into_a_replaces_cycle_is_rejected(a, api):
    client = api(a["users"]["editor"])
    two = record(a, "Two")
    link(a, "replaces", a["app"], two)
    other = link(a, "depends_on", two, a["app"])
    response = client.patch(f"relationships/{other.id}", {"type": "replaces"})
    assert response.status_code == 400 and "replace itself" in response.json_body["errors"]["target"][0]
    other.refresh_from_db()
    assert other.type.slug == "depends_on"


def test_a_record_may_link_to_itself_only_through_two_different_instances(a, api):
    client = api(a["users"]["editor"])
    prod, sandbox = instance(a, a["app"]), instance(a, a["app"], "Sandbox")
    me = str(a["app"].id)
    for ends in (
        {},
        {"source_instance": str(prod.id)},
        {"source_instance": str(prod.id), "target_instance": str(prod.id)},
    ):
        response = post(client, {"type": "depends_on", "source": me, "target": me, **ends})
        assert response.status_code == 400 and "target" in response.json_body["errors"], ends
    ok = post(
        client,
        {
            "type": "depends_on",
            "source": me,
            "source_instance": str(sandbox.id),
            "target": me,
            "target_instance": str(prod.id),
        },
    )
    assert ok.status_code == 201


def test_an_instance_must_belong_to_its_stated_application(a, b, api):
    client = api(a["users"]["editor"])
    other, job = record(a, "Other"), record(a, "Job", "component")
    elsewhere = instance(a, other)
    ours = instance(a, a["app"])
    theirs = instance(b, b["app"])
    base = {"type": "depends_on", "source": str(a["app"].id), "target": str(job.id)}
    wrong = post(client, {**base, "source_instance": str(elsewhere.id)})
    assert wrong.status_code == 400 and "doesn't belong to orga App" in wrong.json_body["errors"]["source_instance"][0]
    assert post(client, {**base, "target_instance": str(ours.id)}).status_code == 400  # a component has no instances
    foreign = post(client, {**base, "source_instance": str(theirs.id)})
    assert foreign.status_code == 400 and foreign.json_body["errors"]["source_instance"] == [
        "Choose an existing instance."
    ]
    assert post(client, {**base, "source_instance": "nonsense"}).status_code == 400
    assert not Relationship.objects.exists()


def test_changing_the_record_without_its_instance_is_refused(a, api):
    client = api(a["users"]["editor"])
    other, job = record(a, "Other"), record(a, "Job", "component")
    rel = link(a, "depends_on", a["app"], job, source_instance=str(instance(a, a["app"]).id))
    response = client.patch(f"relationships/{rel.id}", {"source": str(other.id)})
    assert response.status_code == 400 and "source_instance" in response.json_body["errors"]
    assert (
        client.patch(f"relationships/{rel.id}", {"source": str(other.id), "source_instance": None}).status_code == 200
    )


def test_duplicates_are_a_conflict(a, api):
    client = api(a["users"]["editor"])
    job = record(a, "Job", "component")
    first = link(a, "depends_on", a["app"], job)
    response = post(client, {"type": "depends_on", "source": str(a["app"].id), "target": str(job.id)})
    assert response.status_code == 409 and response["Content-Type"] == "application/problem+json"
    assert response.json_body["type"] == "/problems/duplicate-relationship"
    assert response.json_body["duplicate_of"] == {"id": str(first.id)}
    # The same pair with another type is a different link, and editing a link into a duplicate is refused too.
    second = link(a, "replaces", a["app"], record(a, "New"))
    assert client.patch(f"relationships/{second.id}", {"type": "depends_on", "target": str(job.id)}).status_code == 409
    assert client.patch(f"relationships/{first.id}", {"purpose": "same link, new text"}).status_code == 200


def test_validation_errors(a, api):
    client = api(a["users"]["editor"])
    empty = post(client, {})
    assert empty.status_code == 400 and empty.headers["Content-Type"] == "application/problem+json"
    assert set(empty.json_body["errors"]) == {"type", "source", "target"}
    bad = post(
        client,
        {"type": "owns", "source": str(a["app"].id), "target": str(a["app"].id), "purpose": "x" * 5001},
    )
    assert bad.status_code == 400 and {"type", "purpose"} <= set(bad.json_body["errors"])
    assert post(client, {"type": "depends_on", "source": 7, "target": [1]}).status_code == 400
    assert not Relationship.objects.exists()


def test_archiving_an_endpoint_keeps_the_link(a, api):
    client = api(a["users"]["editor"])
    job = record(a, "Job", "component")
    rel = link(a, "depends_on", a["app"], job)
    client.post(f"components/{job.id}/archive")
    shown = client.get(f"relationships/{rel.id}").json_body
    assert shown["target"]["archived"] is True and shown["target"]["name"] == "Job"
    assert client.patch(f"relationships/{rel.id}", {"purpose": "still editable"}).status_code == 200
    assert Relationship.objects.count() == 1


def test_a_viewer_reads_but_cannot_write(a, api):
    rel = link(a, "depends_on", a["app"], record(a, "Job", "component"))
    viewer = api(a["users"]["viewer"])
    assert viewer.get(f"relationships/{rel.id}").status_code == 200
    for response in (
        post(viewer, {"type": "depends_on"}),
        viewer.patch(f"relationships/{rel.id}", {"purpose": "x"}),
        viewer.delete(f"relationships/{rel.id}"),
    ):
        assert response.status_code == 403
    assert Relationship.objects.count() == 1


# ---- cross-tenant -------------------------------------------------------------------------------------------


def test_other_org_relationship_is_404(a, b, api):
    theirs = link(b, "depends_on", b["app"], record(b, "Their job", "component"))
    client = api(a["users"]["admin"])
    before = AuditEvent.objects.count()
    assert client.get(f"relationships/{theirs.id}").status_code == 404
    assert client.patch(f"relationships/{theirs.id}", {"purpose": "Hijacked"}).status_code == 404
    assert client.delete(f"relationships/{theirs.id}").status_code == 404
    theirs.refresh_from_db()
    assert theirs.purpose == "" and AuditEvent.objects.count() == before


def test_lists_and_filters_stay_in_own_org(a, b, api):
    # Both organizations link their own records; neither list shows the other's links or records.
    mine = link(a, "depends_on", a["app"], record(a, "Our job", "component"))
    link(b, "depends_on", b["app"], record(b, "Their job", "component"))
    client = api(a["users"]["viewer"])
    assert [r["id"] for r in client.get("relationships").json_body] == [str(mine.id)]
    assert client.get(f"relationships?resource={b['app'].id}").status_code == 404
    assert [r["id"] for r in client.get(f"relationships?resource={a['app'].id}").json_body] == [str(mine.id)]


def test_cannot_link_or_pick_instances_across_organizations(a, b, api):
    client = api(a["users"]["editor"])
    theirs = record(b, "Their job", "component")
    ours = record(a, "Our job", "component")
    response = post(client, {"type": "depends_on", "source": str(a["app"].id), "target": str(theirs.id)})
    assert response.status_code == 400 and response.json_body["errors"]["target"] == ["Choose an existing record."]
    their_instance = instance(b, b["app"])
    response = post(
        client,
        {
            "type": "depends_on",
            "source": str(b["app"].id),
            "source_instance": str(their_instance.id),
            "target": str(ours.id),
        },
    )
    assert response.status_code == 400 and "source" in response.json_body["errors"]
    assert not Relationship.objects.exists()


def test_replaces_cycle_check_ignores_other_organizations(a, b, api):
    # B's replaces chain doesn't influence A: even with the same names, no link crosses organizations.
    b_old = record(b, "Old")
    link(b, "replaces", b["app"], b_old)
    new, old = record(a, "New"), record(a, "Old")
    link(a, "replaces", new, old)
    assert (
        post(
            api(a["users"]["editor"]), {"type": "replaces", "source": str(a["app"].id), "target": str(new.id)}
        ).status_code
        == 201
    )


# ---- ?limit= (the relationship-type in-use dialog shows 20 of what may be thousands) ----------------------------------


def test_limit_returns_the_first_links_with_the_count(a, api):
    client = api(a["users"]["viewer"])
    made = [link(a, "depends_on", record(a, f"User {i}"), a["app"]) for i in range(5)]
    everything = client.get("relationships").json_body
    assert isinstance(everything, list) and [r["id"] for r in everything] == [str(r.id) for r in made]  # unchanged
    page = client.get("relationships?limit=2").json_body
    assert set(page) == {"results", "count", "has_more"}
    assert [r["id"] for r in page["results"]] == [str(r.id) for r in made[:2]]
    assert (page["count"], page["has_more"]) == (5, True)
    assert page["results"][0] == everything[0]  # the same rows as the array
    whole = client.get("relationships?limit=5").json_body
    assert (len(whole["results"]), whole["count"], whole["has_more"]) == (5, 5, False)
    assert client.get("relationships?limit=500").json_body["count"] == 5
    newest = client.get("relationships?limit=2&ordering=-created_at").json_body
    assert [r["id"] for r in newest["results"]] == [str(made[4].id), str(made[3].id)]


def test_limit_order_is_stable_when_created_at_ties(a, api):
    client = api(a["users"]["viewer"])
    made = [link(a, "depends_on", record(a, f"User {i}"), a["app"]) for i in range(4)]
    Relationship.objects.filter(id__in=[r.id for r in made]).update(created_at=made[0].created_at)
    by_id = sorted(str(r.id) for r in made)
    assert [r["id"] for r in client.get("relationships?limit=3").json_body["results"]] == by_id[:3]
    assert [r["id"] for r in client.get("relationships?limit=3").json_body["results"]] == by_id[:3]
    assert [r["id"] for r in client.get("relationships").json_body] == by_id


def test_limit_counts_only_the_filtered_links(a, api):
    client = api(a["users"]["viewer"])
    billing = record(a, "Billing")
    link(a, "depends_on", a["app"], billing)
    link(a, "depends_on", record(a, "Other"), billing)
    replaced = link(a, "replaces", billing, record(a, "Billing 2"))
    got = client.get("relationships?type=replaces&limit=1").json_body
    assert ([r["id"] for r in got["results"]], got["count"], got["has_more"]) == ([str(replaced.id)], 1, False)
    got = client.get(f"relationships?resource={billing.id}&limit=1").json_body
    assert (len(got["results"]), got["count"], got["has_more"]) == (1, 3, True)


def test_limit_is_validated(a, api):
    client = api(a["users"]["viewer"])
    for bad in ("0", "501", "abc", "", "-1", "1.5", "²"):
        response = client.get(f"relationships?limit={bad}")
        assert response.status_code == 400, bad
        assert response.json_body["errors"] == {"limit": ["Use a whole number from 1 to 500."]}, bad
    response = client.get("relationships?limit=0&resource=nonsense")
    assert set(response.json_body["errors"]) == {"limit", "resource"}  # one 400 naming both
    assert client.get("relationships?limit=5&cursor=x").json_body["errors"] == {
        "cursor": ["This filter isn't available here."]
    }


def test_limit_stays_in_own_org(a, b, api):
    mine = link(a, "depends_on", record(a, "Our job", "component"), a["app"])
    for i in range(3):
        link(b, "depends_on", record(b, f"Their job {i}", "component"), b["app"])
    got = api(a["users"]["viewer"]).get("relationships?limit=10").json_body
    assert ([r["id"] for r in got["results"]], got["count"], got["has_more"]) == ([str(mine.id)], 1, False)
    assert api(a["users"]["viewer"]).get(f"relationships?resource={b['app'].id}&limit=1").status_code == 404


def test_limit_needs_a_signed_in_member(a, b, api):
    link(a, "depends_on", record(a, "Job", "component"), a["app"])
    assert api().get("relationships?limit=1").status_code in (401, 403)
    for role in ("viewer", "editor", "admin"):
        assert api(a["users"][role]).get("relationships?limit=1").json_body["count"] == 1
    assert api(b["users"]["admin"]).get("relationships?limit=1").json_body["count"] == 0
