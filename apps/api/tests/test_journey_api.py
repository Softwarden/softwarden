"""The first-run journey through the API: create → assign owners → record review → Needs attention clears."""


def names(rows):
    return {r["name"] for r in rows}


def test_editor_journey(a, api):
    client = api(a["users"]["editor"])
    created = client.post("applications", {"name": "Expense Hub", "lifecycle": "active"})
    assert created.status_code == 201
    app = created.json_body
    assert {"No business owner", "No technical owner", "No support team"} <= set(app["gaps"])
    assert app["review_state"] == "unreviewed" and app["it_steward"]["email"] == "editor@orga.test"

    attention = client.get("needs-attention").json_body
    assert "Expense Hub" in names(attention["ownership_gaps"]) and "Expense Hub" in names(attention["reviews"])

    owners = {
        "business_owner": str(a["people"]["priya"].id),
        "technical_owner": str(a["people"]["dana"].id),
        "support_team": str(a["team"].id),
        "criticality": "high",
    }
    assert client.patch(f"applications/{app['id']}", owners).status_code == 200
    attention = client.get("needs-attention").json_body
    assert "Expense Hub" not in names(attention["ownership_gaps"]) and "Expense Hub" in names(attention["reviews"])

    review = client.post(
        f"applications/{app['id']}/reviews",
        {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Email from Dana"},
    )
    assert review.status_code == 201
    detail = client.get(f"applications/{app['id']}").json_body
    assert detail["review_state"] == "confirmed" and detail["gaps"] == []
    attention = client.get("needs-attention").json_body
    assert "Expense Hub" not in names(attention["ownership_gaps"] + attention["reviews"])


def test_viewer_sees_but_cannot_change(a, api):
    client = api(a["users"]["viewer"])
    assert client.get(f"applications/{a['app'].id}").status_code == 200
    assert client.patch(f"applications/{a['app'].id}", {"name": "Nope"}).status_code == 403
