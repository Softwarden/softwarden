"""Needs attention: links to verify and stale assessments, with the scopes and counts."""

from datetime import timedelta

from django.utils import timezone

from catalog import assessments, services
from catalog.models import LinkConfirmation, Resource
from notify.attention import QUEUES


def record(ctx, name, kind="application", **extra):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": "active", **extra}
    )


def depends(ctx, source, target):
    return services.create_relationship(
        ctx["org"], ctx["users"]["editor"], {"type": "depends_on", "source": str(source.id), "target": str(target.id)}
    )


def confirm_old(ctx, api, kind, pk, days_ago):
    """Confirm through the API, then move the date back."""
    body = {"confirmed_by": str(ctx["people"]["dana"].id), "evidence": "Email from Dana"}
    assert api(ctx["users"]["editor"]).post(f"{kind}/{pk}/confirmations", body).status_code == 201
    on = services.org_today(ctx["org"]) - timedelta(days=days_ago)
    LinkConfirmation.objects.filter(**{f"{kind[:-1]}_id": pk}).update(confirmed_on=on)
    return on


def feed(ctx, source, target, name="Nightly feed"):
    return services.create_integration(
        ctx["org"],
        ctx["users"]["editor"],
        {"name": name, "lifecycle": "active", "source": str(source.id), "target": str(target.id)},
    )


def attention(ctx, api, role="admin", scope="all"):
    return api(ctx["users"][role]).get(f"needs-attention?scope={scope}").json_body


# ---- links to verify -------------------------------------------------------------------------------------------


def test_an_overdue_relationship_is_listed_with_its_why_text(a, api):
    top, core = record(a, "Top"), record(a, "Core")
    rel = depends(a, top, core)
    on = confirm_old(a, api, "relationships", rel.id, 100)
    [row] = attention(a, api)["links_to_verify"]
    assert row["id"] == str(rel.id) and row["link_kind"] == "relationship" and row["type"] == "depends_on"
    assert row["record"]["name"] == "Top" and row["source"]["name"] == "Top" and row["target"]["name"] == "Core"
    assert row["why"] == f"Confirmed {on.isoformat()}; interval 90 days."
    assert row["last_confirmed_on"] == on.isoformat()
    editor = a["users"]["editor"]
    assert row["steward"]["name"] == (editor.get_full_name() or editor.email)


def test_fresh_and_never_confirmed_links_are_not_listed(a, api):
    top, core, other = record(a, "Top"), record(a, "Core"), record(a, "Other")
    fresh = depends(a, top, core)
    depends(a, top, other)  # never confirmed: Unconfirmed, not Needs verification
    confirm_old(a, api, "relationships", fresh.id, 90)  # day 90 is still good
    assert attention(a, api)["links_to_verify"] == []


def test_a_changed_signature_is_listed_with_what_changed(a, api):
    top, core = record(a, "Top"), record(a, "Core")
    rel = depends(a, top, core)
    confirm_old(a, api, "relationships", rel.id, 1)
    services.update_resource(a["org"], a["users"]["editor"], top, {"technical_owner": str(a["people"]["priya"].id)})
    [row] = attention(a, api)["links_to_verify"]
    assert row["why"] == "Owner changed since confirmation."


def test_an_integration_is_routed_to_its_own_steward_and_listed_oldest_first(a, api):
    src, dst = record(a, "Src"), record(a, "Dst")
    flow = feed(a, src, dst)
    services.update_resource(a["org"], a["users"]["admin"], flow, {"it_steward": a["users"]["admin"].id})
    rel = depends(a, src, dst)
    confirm_old(a, api, "integrations", flow.id, 200)
    confirm_old(a, api, "relationships", rel.id, 120)
    rows = attention(a, api)["links_to_verify"]
    assert [r["link_kind"] for r in rows] == ["integration", "relationship"]  # oldest confirmation first
    assert rows[0]["record"] == {"id": str(flow.id), "kind": "integration", "name": "Nightly feed"}
    # The integration's steward is the admin, not the source record's steward (the editor).
    assert [r["link_kind"] for r in attention(a, api, "admin", "mine")["links_to_verify"]] == ["integration"]
    assert [r["link_kind"] for r in attention(a, api, "editor", "mine")["links_to_verify"]] == ["relationship"]


def test_a_link_without_a_steward_goes_to_the_coordinator_and_the_unassigned_scope(a, api):
    top, core = record(a, "Top"), record(a, "Core")
    Resource.objects.filter(id=top.id).update(it_steward=None)
    rel = depends(a, top, core)
    confirm_old(a, api, "relationships", rel.id, 100)
    [row] = attention(a, api, "admin", "mine")["links_to_verify"]
    assert row["steward"]["coordinator"] is True
    assert attention(a, api, "editor", "mine")["links_to_verify"] == []
    assert len(attention(a, api, "editor", "unassigned")["links_to_verify"]) == 1
    assert attention(a, api, "admin", "mine")["counts"]["unassigned"] >= 1


def test_retired_and_archived_ends_hide_the_link(a, api):
    top, core, old = record(a, "Top"), record(a, "Core"), record(a, "Old")
    one, two = depends(a, top, core), depends(a, top, old)
    for rel in (one, two):
        confirm_old(a, api, "relationships", rel.id, 100)
    assert len(attention(a, api)["links_to_verify"]) == 2
    Resource.objects.filter(id=old.id).update(lifecycle="retired")
    assert [r["id"] for r in attention(a, api)["links_to_verify"]] == [str(one.id)]
    Resource.objects.filter(id=core.id).update(archived_at=timezone.now())
    assert attention(a, api)["links_to_verify"] == []


def test_without_a_coordinator_an_unrouted_link_shows_an_unassigned_steward(a, api):
    top, core = record(a, "Top"), record(a, "Core")
    Resource.objects.filter(id=top.id).update(it_steward=None)
    a["org"].coordinator = None
    a["org"].save()
    confirm_old(a, api, "relationships", depends(a, top, core).id, 100)
    [row] = attention(a, api, "admin", "unassigned")["links_to_verify"]
    assert row["steward"] == {"name": "Unassigned"}
    assert attention(a, api, "admin", "mine")["links_to_verify"] == []


# ---- stale assessments --------------------------------------------------------------------------------------------


def assess(ctx, target, **extra):
    return assessments.create_assessment(
        ctx["org"],
        ctx["users"]["editor"],
        {"target": str(target.id), "change_type": "retirement", "title": "Retire it", **extra},
    )


def test_a_stale_open_assessment_is_listed_for_its_owner(a, api):
    core, top = record(a, "Core"), record(a, "Top")
    saved = assess(a, core, target_date="2026-12-01")
    assert attention(a, api)["stale_assessments"] == []  # fresh
    depends(a, top, core)  # a new dependent flags the snapshot
    [row] = attention(a, api)["stale_assessments"]
    assert row["id"] == str(saved.id) and row["title"] == "Retire it" and row["target"]["name"] == "Core"
    assert row["target_date"] == "2026-12-01" and row["status"] == "draft"
    assert row["why"].startswith(f"Since {row['stale_since']}: A depends_on link was added")
    assert len(attention(a, api, "editor", "mine")["stale_assessments"]) == 1  # the owner defaults to the creator
    assert attention(a, api, "admin", "mine")["stale_assessments"] == []
    assert len(attention(a, api, "viewer", "all")["stale_assessments"]) == 1


def test_closed_assessments_are_not_nagged_and_an_unowned_one_goes_to_the_coordinator(a, api):
    core, top = record(a, "Core"), record(a, "Top")
    open_one, closed = assess(a, core, title="Open one"), assess(a, core, title="Closed one")
    depends(a, top, core)
    assert len(attention(a, api)["stale_assessments"]) == 2
    assessments.change_status(a["org"], a["users"]["editor"], closed, {"status": "closed"})
    [row] = attention(a, api)["stale_assessments"]
    assert row["title"] == "Open one"
    type(open_one).objects.filter(id=open_one.id).update(owner=None)
    assert len(attention(a, api, "admin", "mine")["stale_assessments"]) == 1
    assert attention(a, api, "editor", "mine")["stale_assessments"] == []


def test_a_refreshed_assessment_leaves_the_queue(a, api):
    core, top = record(a, "Core"), record(a, "Top")
    saved = assess(a, core)
    depends(a, top, core)
    assert len(attention(a, api)["stale_assessments"]) == 1
    assessments.refresh_snapshot(a["org"], a["users"]["editor"], saved)
    assert attention(a, api)["stale_assessments"] == []


def test_a_refresh_landing_while_the_queue_is_read_never_breaks_it(a, api, monkeypatch, django_assert_num_queries):
    """QA: the queue read the assessments, then their stale snapshots in a second query; an assessment refreshed
    in between had no snapshot left and the lookup raised KeyError (Needs attention and the digest returned 500). The
    refresh here lands as the snapshots are read, which is exactly where the old second query sat."""
    from catalog.models import AssessmentSnapshot
    from notify.attention import stale_assessments

    core, top = record(a, "Core"), record(a, "Top")
    kept, refreshed = assess(a, core, title="Kept stale"), assess(a, core, title="Refreshed meanwhile")
    depends(a, top, core)
    assert len(stale_assessments(a["org"])) == 2
    with django_assert_num_queries(1):  # one read: nothing can change between the assessment and its snapshot
        stale_assessments(a["org"])

    manager = AssessmentSnapshot.objects
    original = manager.filter
    landed: list[bool] = []

    def filter_after_a_concurrent_refresh(*args, **kwargs):
        if not landed:
            landed.append(True)
            monkeypatch.setattr(manager, "filter", original)  # the refresh reads snapshots itself
            assessments.refresh_snapshot(a["org"], a["users"]["editor"], refreshed)
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "filter", filter_after_a_concurrent_refresh)
    [row] = stale_assessments(a["org"])
    assert landed and row["id"] == str(kept.id) and row["title"] == "Kept stale"
    assert [r["title"] for r in attention(a, api)["stale_assessments"]] == ["Kept stale"]


# ---- counts, scope, roles, tenancy ----------------------------------------------------------------------------


def test_counts_cover_every_queue_and_match_each_scope(a, api):
    core, top = record(a, "Core"), record(a, "Top")  # both have owner gaps
    confirm_old(a, api, "relationships", depends(a, top, core).id, 100)
    assess(a, core)
    body = attention(a, api, "editor", "mine")
    mine = sum(len(body[k]) for k in QUEUES)
    assert body["counts"]["mine"] == mine and mine > 0
    everything = attention(a, api, "editor", "all")
    assert everything["counts"] == body["counts"]
    assert everything["counts"]["all"] == sum(len(everything[k]) for k in QUEUES)


def test_every_role_can_read_the_new_queues_and_a_bad_scope_is_a_field_error(a, api):
    top, core = record(a, "Top"), record(a, "Core")
    confirm_old(a, api, "relationships", depends(a, top, core).id, 100)
    for role in ("admin", "editor", "viewer"):
        assert len(attention(a, api, role)["links_to_verify"]) == 1
    response = api(a["users"]["viewer"]).get("needs-attention?scope=everyone")
    assert response.status_code == 400 and "scope" in response.json_body["errors"]


def test_other_organizations_links_and_assessments_never_appear(a, b, api):
    for ctx in (a, b):
        top, core = record(ctx, "Top"), record(ctx, "Core")
        confirm_old(ctx, api, "relationships", depends(ctx, top, core).id, 100)
        assess(ctx, core)
        depends(ctx, record(ctx, "New"), core)
    mine, theirs = attention(a, api), attention(b, api)
    ids_a = {r["id"] for r in mine["links_to_verify"] + mine["stale_assessments"]}
    ids_b = {r["id"] for r in theirs["links_to_verify"] + theirs["stale_assessments"]}
    assert len(ids_a) == 2 and len(ids_b) == 2 and ids_a.isdisjoint(ids_b)
