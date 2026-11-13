"""People, Teams, Departments and Vendors (E3; -07; -08; -17)."""

from catalog import admin_services, services
from catalog.models import Application, Department, Person, RecordReview, Resource, Team, Vendor
from core.models import AuditEvent


def test_person_optional_fields_and_case_insensitive_email(a, api):
    editor = api(a["users"]["editor"])
    dept = Department.objects.create(organization=a["org"], name="HR")
    pat = editor.post(
        "people",
        {"name": "Pat Lee", "email": "pat@example.org", "title": "Payroll lead", "department": str(dept.id)},
    )
    assert pat.status_code == 201 and pat.json_body["title"] == "Payroll lead"
    clash = editor.post("people", {"name": "Pat Lee", "email": "PAT@example.org"})
    assert clash.status_code == 400
    assert clash.json_body["errors"]["email"] == ["A person with this email already exists: Pat Lee."]
    # Same name, different or no email: both exist, nothing merges.
    assert editor.post("people", {"name": "Pat Lee"}).status_code == 201
    assert editor.post("people", {"name": "Pat Lee", "email": "pat.lee@example.org"}).status_code == 201
    assert Person.objects.filter(organization=a["org"], name="Pat Lee").count() == 3
    assert editor.post("people", {"name": "No mail", "email": "not-an-email"}).status_code == 400
    # Editing someone else onto an existing email is refused too.
    assert editor.patch(f"people/{a['people']['dana'].id}", {"email": "Pat@Example.org"}).status_code == 400


def test_person_linked_user_is_optional_unique_and_in_org(a, b, api):
    admin = api(a["users"]["admin"])
    dana, priya = a["people"]["dana"], a["people"]["priya"]
    linked = admin.patch(f"people/{dana.id}", {"user": a["users"]["editor"].id})
    assert linked.status_code == 200 and linked.json_body["user"]["email"] == "editor@orga.test"
    again = admin.patch(f"people/{priya.id}", {"user": a["users"]["editor"].id})
    assert again.status_code == 400 and "Dana Ortiz" in again.json_body["errors"]["user"][0]
    other_org = admin.patch(f"people/{priya.id}", {"user": b["users"]["editor"].id})
    assert other_org.status_code == 400 and "user" in other_org.json_body["errors"]
    assert admin.patch(f"people/{dana.id}", {"user": None}).json_body["user"] is None


def test_person_detail_lists_owned_and_confirmed_records(a, api):
    editor = api(a["users"]["editor"])
    dana = a["people"]["dana"]
    editor.patch(f"applications/{a['app'].id}", {"technical_owner": str(dana.id)})
    job = services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Nightly job", "kind": "component", "lifecycle": "active"}
    )
    editor.patch(f"components/{job.id}", {"business_owner": str(dana.id), "technical_owner": str(dana.id)})
    editor.post(f"applications/{a['app'].id}/reviews", {"confirmed_by": str(dana.id), "evidence": "Email"})
    body = api(a["users"]["viewer"]).get(f"people/{dana.id}").json_body
    assert [r["name"] for r in body["technical_owner"]] == ["Nightly job", "orga App"]
    assert [r["name"] for r in body["business_owner"]] == ["Nightly job"]
    assert [r["name"] for r in body["confirmed"]] == ["orga App"] and body["review_count"] == 1
    assert body["responsible"] == 2  # two records, though she holds three roles on them
    listed = {p["name"]: p for p in editor.get("people").json_body}
    assert listed["Dana Ortiz"]["responsible"] == 2 and listed["Priya Raman"]["responsible"] == 0


def test_owner_pickers_can_ask_for_active_people_only(a, api):
    names = [p["name"] for p in api(a["users"]["editor"]).get("people?active=true").json_body]
    assert "Former person" not in names and "Dana Ortiz" in names


def test_marking_an_owner_inactive_makes_their_records_need_confirmation(a, api):
    editor = api(a["users"]["editor"])
    dana = a["people"]["dana"]
    editor.patch(
        f"applications/{a['app'].id}",
        {
            "business_owner": str(dana.id),
            "technical_owner": str(a["people"]["priya"].id),
            "support_team": str(a["team"].id),
            "criticality": "high",
        },
    )
    editor.post(f"applications/{a['app'].id}/reviews", {"confirmed_by": str(dana.id), "evidence": "Email"})
    assert editor.get(f"applications/{a['app'].id}").json_body["review_state"] == "confirmed"
    assert editor.patch(f"people/{dana.id}", {"active": False}).status_code == 200
    record = editor.get(f"applications/{a['app'].id}").json_body
    assert record["review_state"] == "needs_confirmation"
    assert any("nactive" in gap for gap in record["gaps"]), record["gaps"]
    event = AuditEvent.objects.filter(action="person.updated").latest("created_at")
    assert event.target_id == dana.id and event.changes == {"fields": ["active"]}


def test_anonymize_requires_the_typed_name_and_cannot_be_undone(a, api):
    admin = api(a["users"]["admin"])
    priya = a["people"]["priya"]
    priya.email, priya.title, priya.user = "priya@example.test", "HR director", a["users"]["viewer"]
    priya.save()
    RecordReview.objects.create(
        organization=a["org"],
        resource=a["app"],
        confirmed_by=priya,
        recorded_by=a["users"]["editor"],
        confirmation_date="2026-10-01",
        next_review_date="2027-01-01",
        evidence="Email",
        confirmed_values={},
    )
    wrong = admin.post(f"people/{priya.id}/anonymize", {"confirm_name": "priya raman"})
    assert wrong.status_code == 400 and "confirm_name" in wrong.json_body["errors"]
    done = admin.post(f"people/{priya.id}/anonymize", {"confirm_name": "Priya Raman"}).json_body
    assert done["name"] == "Former person #1" and done["anonymized"] and not done["active"]
    priya.refresh_from_db()
    assert (priya.email, priya.title, priya.user, priya.department) == ("", "", None, None)
    # The review stays and now shows the anonymized name.
    reviews = admin.get(f"applications/{a['app'].id}/reviews").json_body
    assert reviews[0]["confirmed_by"]["name"] == "Former person #1"
    assert admin.post(f"people/{priya.id}/anonymize", {"confirm_name": "Former person #1"}).status_code == 409
    assert admin.patch(f"people/{priya.id}", {"active": True}).status_code == 409
    event = AuditEvent.objects.get(action="person.anonymized")
    assert event.target_id == priya.id and "priya" not in str(event.changes).lower()


def test_anonymize_numbers_never_repeat(a, api):
    Person.objects.create(organization=a["org"], name="Former person #3", active=False)
    admin = api(a["users"]["admin"])
    first = admin.post(f"people/{a['people']['dana'].id}/anonymize", {"confirm_name": "Dana Ortiz"}).json_body
    second = admin.post(f"people/{a['people']['priya'].id}/anonymize", {"confirm_name": "Priya Raman"}).json_body
    assert (first["name"], second["name"]) == ("Former person #4", "Former person #5")


def test_anonymize_runs_the_purge_hooks(a, api, monkeypatch):
    calls = []

    def purge(org, person):
        calls.append((org.id, person.id))
        return {"import_files": 2}

    monkeypatch.setattr(admin_services, "ANONYMIZE_HOOKS", [purge])
    dana = a["people"]["dana"]
    api(a["users"]["admin"]).post(f"people/{dana.id}/anonymize", {"confirm_name": "Dana Ortiz"})
    assert calls == [(a["org"].id, dana.id)]
    assert AuditEvent.objects.get(action="person.anonymized").changes == {"purged": {"import_files": 2}}


def test_team_description_and_vendor_website(a, api):
    editor = api(a["users"]["editor"])
    team = editor.post("teams", {"name": "Service Desk", "description": "First-line support"})
    assert team.json_body["description"] == "First-line support" and team.json_body["usage"] == 0
    clash = editor.post("teams", {"name": "service desk"})
    assert clash.json_body["errors"]["name"] == ["A team named Service Desk already exists."]
    assert editor.post("vendors", {"name": "Zylker", "website": "zylker"}).status_code == 400
    vendor = editor.post("vendors", {"name": "Zylker", "website": "https://zylker.example"})
    assert vendor.status_code == 201 and vendor.json_body["website"] == "https://zylker.example"


def test_delete_refused_names_the_blocking_references(a, api):
    editor = api(a["users"]["editor"])
    vendor = Vendor.objects.create(organization=a["org"], name="Zylker Software")
    for name in ("ServiceDesk Plus", "ServiceDesk Plus support"):
        resource = services.create_resource(a["org"], a["users"]["editor"], {"name": name, "lifecycle": "active"})
        Application.objects.filter(resource=resource).update(vendor=vendor)
    refused = editor.delete(f"vendors/{vendor.id}")
    assert refused.status_code == 409
    assert refused.json_body["detail"] == (
        "Zylker Software can't be deleted: 2 applications, renewals and contracts use it (ServiceDesk Plus, "
        "ServiceDesk Plus support). "
        "Reassign them first."
    )
    assert refused.json_body["blocking_references"] == {"applications, renewals and contracts": 2}
    assert [v["usage"] for v in editor.get("vendors").json_body if v["name"] == "Zylker Software"] == [2]

    dept = Department.objects.create(organization=a["org"], name="Legal")
    Person.objects.filter(id=a["people"]["dana"].id).update(department=dept)
    assert editor.delete(f"departments/{dept.id}").json_body["detail"] == (
        "Legal can't be deleted: 1 person uses it (Dana Ortiz). Reassign them first."
    )

    team = a["team"]
    for i in range(5):
        app = services.create_resource(a["org"], a["users"]["editor"], {"name": f"App {i}", "lifecycle": "active"})
        Resource.objects.filter(id=app.id).update(support_team=team)
    assert editor.delete(f"teams/{team.id}").json_body["detail"] == (
        "IT Applications can't be deleted: 5 records use it (App 0, App 1, App 2 and 2 more). Reassign them first."
    )
    assert Team.objects.filter(id=team.id).exists() and not AuditEvent.objects.filter(action="team.deleted").exists()


def test_a_person_link_to_a_user_refuses_ids_that_are_not_plain_numbers(a, api):
    """ "²".isdigit() is true but int("²") fails, and a number past the key range overflows the query: both are the
    field error, never a 500 (services.user_id)."""
    admin, dana = api(a["users"]["admin"]), a["people"]["dana"]
    for bad in ("²", "١", "9" * 30, -1, True, 1.5, "1 "):
        response = admin.patch(f"people/{dana.id}", {"user": bad})
        assert response.status_code == 400, bad
        assert response.json_body["errors"]["user"] == ["Choose a user in this organization."]
    assert admin.patch(f"people/{dana.id}", {"user": str(a["users"]["viewer"].id)}).status_code == 200
