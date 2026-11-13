"""A record's History with its children in words (attachments, relationships, instances,
integrations, reviews, confirmations, data-quality exceptions), the own-only filter, paging and tenancy."""

from django.db import connection
from django.test.utils import CaptureQueriesContext

from catalog import attachments, quality, services
from core.models import AuditEvent
from tests.conftest import rel_type
from tests.test_integrations import flow, record


def history(api, ctx, path, query=""):
    response = api(ctx["users"]["viewer"]).get(f"{path}/audit{query}")
    assert response.status_code == 200, response.json_body
    return response.json_body


def by_kind(body, kind):
    return [e for e in body["results"] if e["child"] and e["child"]["kind"] == kind]


def link(ctx, name="Contract", url="https://contoso.sharepoint.com/c.pdf"):
    return attachments.add_link(
        ctx["org"], ctx["users"]["editor"], ctx["app"], {"url": url, "title": name, "category": "contract"}
    )


def depends(ctx, source, target, slug="depends_on"):
    return services.create_relationship(
        ctx["org"], ctx["users"]["editor"], {"type": slug, "source": str(source.id), "target": str(target.id)}
    )


def test_attachments_are_children_named_in_words(a, api):
    editor = a["users"]["editor"]
    contract = link(a)
    contract_id = str(contract.id)
    attachments.update_attachment(a["org"], editor, contract, {"title": "Master contract", "category": "invoice"})
    attachments.delete_attachment(a["org"], editor, contract)

    events = by_kind(history(api, a, f"applications/{a['app'].id}"), "attachment")
    assert [e["child"]["sentence"] for e in events] == [
        "Removed attachment 'Master contract'",
        "Changed attachment 'Master contract': Title: Contract → Master contract; Category: Contract → Invoice",
        "Added attachment 'Contract' (link)",
    ]
    assert {e["child"]["id"] for e in events} == {contract_id}
    assert events[0]["child"]["kind_label"] == "Attachment" and events[0]["child"]["name"] == "Master contract"


def test_instances_are_children_with_their_changes(a, api):
    editor = a["users"]["editor"]
    prod = services.create_instance(a["org"], editor, a["app"], {"name": "prod"})
    services.update_instance(a["org"], editor, prod, {"status": "offline"})
    services.delete_instance(a["org"], editor, prod)

    events = by_kind(history(api, a, f"applications/{a['app'].id}"), "instance")
    assert [e["child"]["sentence"] for e in events] == [
        "Deleted instance 'prod'",
        "Instance 'prod' changed: Status: unknown → offline",
        "Added instance 'prod'",
    ]


def test_relationships_read_from_either_end(a, api):
    payroll = record(a, "Payroll API", "component")
    rel = depends(a, a["app"], payroll)
    services.update_relationship(a["org"], a["users"]["editor"], rel, {"purpose": "Pays people"})
    services.delete_relationship(a["org"], a["users"]["editor"], rel)

    source_side = [
        e["child"]["sentence"] for e in by_kind(history(api, a, f"applications/{a['app'].id}"), "relationship")
    ]
    assert source_side == [
        "Unlinked from Payroll API: depends on",
        "Link to Payroll API (depends on) changed: Purpose changed",
        "Linked to Payroll API: depends on",
    ]
    target_side = [e["child"]["sentence"] for e in by_kind(history(api, a, f"components/{payroll.id}"), "relationship")]
    assert target_side == [
        "Unlinked from orga App: is needed by",
        "Link to orga App (is needed by) changed: Purpose changed",
        "Linked to orga App: is needed by",
    ]


def test_a_type_without_an_inverse_name_reads_the_whole_link_from_the_target(a, api):
    payroll = record(a, "Payroll API", "component")
    rtype = rel_type(a["org"])
    rtype.inverse_name = ""
    rtype.save()
    depends(a, a["app"], payroll)
    [event] = by_kind(history(api, a, f"components/{payroll.id}"), "relationship")
    assert event["child"]["sentence"] == "Linked: orga App depends on Payroll API"


def test_link_confirmations_reviews_and_quality_exceptions_are_children(a, api):
    editor, dana = api(a["users"]["editor"]), a["people"]["dana"]
    payroll = record(a, "Payroll API", "component")
    rel = depends(a, a["app"], payroll)
    editor.post(f"relationships/{rel.id}/confirmations", {"confirmed_by": str(dana.id), "evidence": "Email"})
    editor.post(f"applications/{a['app'].id}/reviews", {"confirmed_by": str(dana.id), "evidence": "Email"})
    exc = quality.create_exception(
        a["org"],
        a["users"]["editor"],
        {"record": str(a["app"].id), "rule": "documentation", "reason": "Vendor portal", "owner": str(dana.id)},
    )
    quality.delete_exception(a["org"], a["users"]["editor"], exc)

    body = history(api, a, f"applications/{a['app'].id}")
    [confirmation] = by_kind(body, "link_confirmation")
    assert confirmation["child"]["sentence"].startswith("Link to Payroll API (depends on) confirmed by Dana Ortiz on ")
    [review] = by_kind(body, "review")
    assert review["child"]["sentence"] == "Review recorded: confirmed by Dana Ortiz"
    created, removed = reversed(by_kind(body, "quality_exception"))
    assert (
        created["child"]["sentence"].startswith("Marked check 'No documentation' not applicable")
        and " until " in created["child"]["sentence"]
    )
    assert removed["child"]["sentence"].endswith("applies again: the not-applicable mark was removed")


def test_integrations_the_record_is_an_end_of_are_children(a, api):
    hr = record(a, "HR", "component")
    feed = flow(a, "Payroll feed", source=a["app"], target=hr)
    services.update_integration(a["org"], a["users"]["editor"], feed, {"protocol": "SFTP"})
    services.archive_resource(a["org"], a["users"]["editor"], feed)

    source_side = by_kind(history(api, a, f"applications/{a['app'].id}"), "integration")
    assert [e["child"]["sentence"] for e in source_side] == [
        "Archived integration 'Payroll feed' to HR",
        "Integration 'Payroll feed' to HR changed: Protocol: not set → SFTP",
        "Added integration 'Payroll feed' to HR",
    ]
    assert {e["child"]["id"] for e in source_side} == {str(feed.id)}
    target_side = by_kind(history(api, a, f"components/{hr.id}"), "integration")
    assert target_side[-1]["child"]["sentence"] == "Added integration 'Payroll feed' from orga App"


def test_a_deleted_or_repointed_integration_stays_in_the_old_ends_history(a, api):
    hr, crm = record(a, "HR", "component"), record(a, "CRM", "component")
    feed = flow(a, "Payroll feed", source=a["app"], target=hr)
    services.update_integration(a["org"], a["users"]["editor"], feed, {"target": str(crm.id)})
    old_end = [e["child"]["sentence"] for e in by_kind(history(api, a, f"components/{hr.id}"), "integration")]
    assert old_end[0].startswith("Integration 'Payroll feed' changed: ")
    assert old_end[-1] == "Added integration 'Payroll feed' from orga App"
    services.delete_resource(a["org"], a["users"]["admin"], feed)
    newest = by_kind(history(api, a, f"applications/{a['app'].id}"), "integration")[0]
    assert newest["child"]["sentence"] == "Deleted integration 'Payroll feed' to CRM"


def test_an_integrations_own_history_has_its_children(a, api):
    feed = flow(a, "Payroll feed", source=a["app"], target=record(a, "HR", "component"))
    api(a["users"]["editor"]).post(
        f"integrations/{feed.id}/confirmations", {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Email"}
    )
    body = history(api, a, f"integrations/{feed.id}")
    [confirmation] = by_kind(body, "link_confirmation")
    assert confirmation["child"]["sentence"].startswith("Integration confirmed by Dana Ortiz on ")
    own = history(api, a, f"integrations/{feed.id}", "?children=false")
    assert [e["label"] for e in own["results"]] == ["Record created"] and own["results"][0]["child"] is None


def test_own_events_only_filter(a, api):
    editor = a["users"]["editor"]
    services.update_resource(a["org"], editor, a["app"], {"purpose": "Pays people"})
    link(a)
    services.create_instance(a["org"], editor, a["app"], {"name": "prod"})
    depends(a, a["app"], record(a, "Payroll API", "component"))
    api(editor).post(
        f"applications/{a['app'].id}/reviews", {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Email"}
    )
    every = history(api, a, f"applications/{a['app'].id}")
    assert len(every["results"]) == 5
    assert history(api, a, f"applications/{a['app'].id}", "?children=true") == every
    own = history(api, a, f"applications/{a['app'].id}", "?children=false")
    assert [e["label"] for e in own["results"]] == ["Fields changed"]
    assert own["results"][0]["child"] is None
    bad = api(a["users"]["viewer"]).get(f"applications/{a['app'].id}/audit?children=maybe")
    assert bad.status_code == 400 and "children" in bad.json_body["errors"]
    assert api(a["users"]["viewer"]).get(f"applications/{a['app'].id}/audit?kids=1").status_code == 400


def test_older_child_events_that_targeted_the_record_are_children(a, api):
    """Before related_target a review targeted the record itself; the log can't be rewritten (append-only
    trigger), so the History tells it apart by its action."""
    AuditEvent.objects.create(
        organization=a["org"],
        action="review.recorded",
        target_type="Resource",
        target_id=a["app"].id,
        changes={"confirmed_by": str(a["people"]["dana"].id)},
    )
    body = history(api, a, f"applications/{a['app'].id}")
    assert body["results"][0]["child"] == {
        "kind": "review",
        "kind_label": "Review",
        "id": None,
        "name": "Review",
        "sentence": "Review recorded: confirmed by Dana Ortiz",
    }
    assert history(api, a, f"applications/{a['app'].id}", "?children=false")["results"] == []


def test_a_renewals_cycles_are_children(a, api):
    from tests.test_renewals import body as renewal_body

    client = api(a["users"]["editor"])
    renewal = client.post("renewals", renewal_body(a)).json_body
    client.post(f"renewals/{renewal['id']}/cycles/{renewal['cycle']['id']}/decision", {"decision": "renew"})
    body = history(api, a, f"renewals/{renewal['id']}")
    [decided] = by_kind(body, "renewal_cycle")
    assert decided["child"]["sentence"].startswith("Cycle 1: Decision recorded. Decision: ")
    own = history(api, a, f"renewals/{renewal['id']}", "?children=false")["results"]
    assert own and all(e["child"] is None for e in own)


def test_paging_runs_through_children_and_query_counts_stay_flat(a, api):
    editor = a["users"]["editor"]
    for i in range(30):
        link(a, f"Doc {i}")
        services.create_instance(a["org"], editor, a["app"], {"name": f"i{i}"})
    client = api(a["users"]["viewer"])
    path = f"applications/{a['app'].id}/audit"
    with CaptureQueriesContext(connection) as first_page:
        first = client.get(path).json_body
    assert len(first["results"]) == 50 and first["next"]
    second = client.get(f"{path}?cursor={first['next']}").json_body
    assert len(second["results"]) == 10 and second["next"] is None
    ids = [e["id"] for e in first["results"] + second["results"]]
    assert len(set(ids)) == 60
    assert all(e["child"] for e in first["results"] + second["results"])

    for i in range(30, 60):
        link(a, f"Doc {i}")
        services.create_instance(a["org"], editor, a["app"], {"name": f"i{i}"})
    with CaptureQueriesContext(connection) as bigger:
        client.get(path)
    assert len(bigger) == len(first_page)  # the page's names load in a fixed number of queries
    assert len(first_page) <= 30


def test_another_organizations_children_never_appear(a, b, api):
    shared = "Shared"
    b_hr = record(b, shared, "component")
    flow(b, "B feed", source=b["app"], target=b_hr)
    attachments.add_link(
        b["org"],
        b["users"]["editor"],
        b["app"],
        {"url": "https://b.example/x", "title": "B doc", "category": "contract"},
    )
    # An event in b that names a's record id in its payload (as a forged or stray event would) stays in b.
    AuditEvent.objects.create(
        organization=b["org"],
        action="relationship.created",
        target_type="Relationship",
        changes={"type": "depends_on", "source": str(a["app"].id), "target": str(b_hr.id)},
    )
    AuditEvent.objects.create(
        organization=b["org"],
        action="attachment.created",
        target_type="Attachment",
        related_target_type="Resource",
        related_target_id=a["app"].id,
        changes={"title": "Leak", "kind": "link"},
    )
    body = history(api, a, f"applications/{a['app'].id}")
    assert body["results"] == []
    assert api(a["users"]["viewer"]).get(f"applications/{b['app'].id}/audit").status_code == 404


def test_a_link_moved_off_the_record_and_edited_later_leaves_its_history(a, api):
    """The move shows in both old and new end's History; an edit made after the move only in the current ends'."""
    b_rec, c_rec = record(a, "B", "component"), record(a, "C", "component")
    rel = depends(a, a["app"], b_rec)
    services.update_relationship(a["org"], a["users"]["editor"], rel, {"source": str(c_rec.id)})
    services.update_relationship(a["org"], a["users"]["editor"], rel, {"purpose": "Nightly sync"})
    app_side = [e["child"]["sentence"] for e in by_kind(history(api, a, f"applications/{a['app'].id}"), "relationship")]
    assert len(app_side) == 2  # the move and the creation, not the later edit
    assert app_side[1] == "Linked to B: depends on"
    assert not any("Purpose changed" in s for s in app_side)
    c_side = [e["child"]["sentence"] for e in by_kind(history(api, a, f"components/{c_rec.id}"), "relationship")]
    assert c_side[0] == "Link to B (depends on) changed: Purpose changed"
    services.delete_relationship(a["org"], a["users"]["editor"], rel)  # removed: still named from its snapshots
    c_side = [e["child"]["sentence"] for e in by_kind(history(api, a, f"components/{c_rec.id}"), "relationship")]
    assert c_side[:2] == ["Unlinked from B: depends on", "Link to B (depends on) changed: Purpose changed"]


def test_older_link_events_without_snapshots_still_follow_a_removed_link(a, api):
    """Before snapshots an edit's payload held only the changed fields; the link is found by the ends its creation
    saved."""
    import uuid

    link_id = uuid.uuid4()
    b_rec = record(a, "B", "component")
    for action, changes in (
        ("relationship.created", {"type": "depends_on", "source": str(a["app"].id), "target": str(b_rec.id)}),
        ("relationship.updated", {"purpose": ["", "Sync"]}),
    ):
        AuditEvent.objects.create(
            organization=a["org"], action=action, target_type="Relationship", target_id=link_id, changes=changes
        )
    events = by_kind(history(api, a, f"applications/{a['app'].id}"), "relationship")
    assert [e["action"] for e in events] == ["relationship.updated", "relationship.created"]


def test_an_unknown_child_action_reads_as_a_generic_child(a, api):
    # bulk_create: the conftest sweep refuses target types the log can't filter by, which is the point here.
    AuditEvent.objects.bulk_create(
        [
            AuditEvent(
                organization=a["org"],
                action="widget.added",
                target_type="Widget",
                related_target_type="Resource",
                related_target_id=a["app"].id,
                changes={"name": "Gizmo"},
            )
        ]
    )
    [event] = history(api, a, f"applications/{a['app'].id}")["results"]
    assert event["child"] == {
        "kind": "other",
        "kind_label": "Related",
        "id": None,
        "name": "Widget",
        "sentence": "widget.added: Gizmo",
    }
