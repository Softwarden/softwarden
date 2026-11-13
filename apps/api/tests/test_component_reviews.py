"""Components share review rules and the Needs attention queues with applications."""

from catalog import services


def make_component(a, name="Nightly job", **extra):
    data = {"name": name, "kind": "component", "lifecycle": "active", **extra}
    return services.create_resource(a["org"], a["users"]["editor"], data)


def owned_component(a, name="Nightly job"):
    people = a["people"]
    return make_component(
        a,
        name,
        business_owner=str(people["priya"].id),
        technical_owner=str(people["dana"].id),
        support_team=str(a["team"].id),
    )


def review_payload(a, **extra):
    return {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Email from Dana", **extra}


def items(client, scope="all"):
    body = client.get(f"needs-attention?scope={scope}").json_body
    return body["ownership_gaps"], body["reviews"]


def test_record_and_list_component_review(a, api):
    component = owned_component(a)
    client = api(a["users"]["editor"])
    response = client.post(f"components/{component.id}/reviews", review_payload(a))
    assert response.status_code == 201
    history = client.get(f"components/{component.id}/reviews").json_body
    assert [h["id"] for h in history] == [response.json_body["id"]]
    assert history[0]["confirmed_by"]["name"] == "Dana Ortiz"
    detail = client.get(f"components/{component.id}").json_body
    assert detail["review_state"] == "confirmed" and detail["next_review_date"]


def test_component_review_validation_matches_applications(a, api):
    component = owned_component(a)
    client = api(a["users"]["editor"])
    bad = client.post(f"components/{component.id}/reviews", {"confirmed_by": str(a["people"]["dana"].id)})
    assert bad.status_code == 400 and "evidence" in bad.json_body["errors"]
    future = client.post(f"components/{component.id}/reviews", review_payload(a, confirmation_date="2999-01-01"))
    assert future.status_code == 400 and "confirmation_date" in future.json_body["errors"]


def test_inactive_confirmer_needs_reason(a, api):
    component = owned_component(a)
    client = api(a["users"]["editor"])
    url = f"components/{component.id}/reviews"
    gone = str(a["people"]["gone"].id)
    refused = client.post(url, review_payload(a, confirmed_by=gone))
    assert refused.status_code == 400 and "inactive_reason" in refused.json_body["errors"]
    accepted = client.post(
        url, review_payload(a, confirmed_by=gone, inactive_reason="Left; manager confirmed by phone")
    )
    assert accepted.status_code == 201 and accepted.json_body["inactive_reason"]


def test_component_appears_with_kind_and_clears(a, api):
    component = owned_component(a)
    client = api(
        a["users"]["editor"],
    )
    gaps, reviews = items(client, "mine")
    assert gaps == [] or all(g["kind"] == "application" for g in gaps)
    row = next(r for r in reviews if r["id"] == str(component.id))
    assert row["kind"] == "component" and row["state"] == "unreviewed"
    client.post(f"components/{component.id}/reviews", review_payload(a))
    assert str(component.id) not in {r["id"] for r in items(client, "mine")[1]}


def test_component_ownership_gap_and_routing(a, api):
    component = make_component(a, "Bare job")
    gaps, _ = items(api(a["users"]["editor"]), "mine")
    row = next(g for g in gaps if g["id"] == str(component.id))
    assert row["kind"] == "component" and "No business owner" in row["gaps"]
    # Without a steward the item goes to the coordinator (admin), not the editor.
    services.update_resource(a["org"], a["users"]["editor"], component, {"it_steward": None})
    admin, editor = api(a["users"]["admin"]), api(a["users"]["editor"])
    assert str(component.id) in {g["id"] for g in items(admin, "mine")[0]}
    assert str(component.id) not in {g["id"] for g in items(editor, "mine")[0]}
    unassigned = next(g for g in items(admin, "all")[0] if g["id"] == str(component.id))
    assert unassigned["steward"]["coordinator"] is True


def test_component_needs_confirmation_after_change(a, api):
    component = owned_component(a)
    client = api(a["users"]["editor"])
    client.post(f"components/{component.id}/reviews", review_payload(a))
    client.patch(f"components/{component.id}", {"criticality": "high"})
    row = next(r for r in items(client)[1] if r["id"] == str(component.id))
    assert row["state"] == "needs_confirmation"


def test_archived_and_retired_components_are_excluded(a, api):
    from django.utils import timezone

    archived, retired, live = make_component(a, "Archived"), make_component(a, "Retired"), make_component(a, "Live")
    archived.archived_at = timezone.now()
    archived.save()
    client = api(a["users"]["editor"])
    client.patch(f"components/{retired.id}", {"lifecycle": "retired"})
    gaps, reviews = items(client)
    ids = {r["id"] for r in gaps + reviews}
    assert str(live.id) in ids
    assert str(archived.id) not in ids and str(retired.id) not in ids
