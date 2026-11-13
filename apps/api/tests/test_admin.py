"""Accounts, people and reference-data rules (E2, E3), plus their cross-tenant behaviour."""

import pytest
from django.test import Client

from catalog.models import Department, Vendor
from core.models import AuditEvent, Membership


def accept_invitation(link, name="New Person", password="a-much-longer-passphrase"):  # noqa: S107
    """Accept an invitation link as a new account (catalog/access_viewsets.invitation); returns its signed-in client."""
    client = Client()
    token = link.rsplit("/invite#", 1)[1]
    response = client.post(
        "/api/v1/auth/invitations/accept",
        {"token": token, "name": name, "password": password},
        content_type="application/json",
    )
    assert response.status_code == 200, response.json()
    return client


def test_an_invited_person_sets_their_own_password_through_the_link(a, api):
    """Inviting no longer hands out a temporary password: the person chooses one, under the password
    validators, when they accept, and is signed in without a forced change."""
    admin = api(a["users"]["admin"])
    invited = admin.post("users", {"email": "New.Person@orga.test", "name": "New Person", "role": "editor"})
    assert invited.status_code == 201 and "temporary_password" not in invited.json_body
    token = invited.json_body["link"].rsplit("/invite#", 1)[1]
    short = Client().post(
        "/api/v1/auth/invitations/accept",
        {"token": token, "name": "New Person", "password": "short"},
        content_type="application/json",
    )
    assert short.status_code == 400 and "password" in short.json()["errors"]
    newcomer = accept_invitation(invited.json_body["link"])
    assert newcomer.get("/api/v1/auth/me").json()["must_change_password"] is False
    assert newcomer.get("/api/v1/applications").status_code == 200
    login = api().post("auth/login", {"email": "new.person@orga.test", "password": "a-much-longer-passphrase"})
    assert login.status_code == 200


def test_changing_a_password_to_the_same_one_is_refused(a, api):
    """The same-password check (catalog/admin_services.change_password), which a forced change relies on: a
    temporary password can't be kept by "changing" it to itself."""
    user = a["users"]["editor"]
    Membership.objects.filter(user=user).update(must_change_password=True)
    same = api(user).post(
        "auth/password", {"current_password": "correct-horse-battery", "new_password": "correct-horse-battery"}
    )
    assert same.status_code == 400 and "new_password" in same.json_body["errors"]
    assert Membership.objects.get(user=user, organization=a["org"]).must_change_password is True
    assert not AuditEvent.objects.filter(action="user.password_changed").exists()


def test_last_admin_and_coordinator_guards(a, api):
    admin = api(a["users"]["admin"])
    me = a["users"]["admin"].id
    assert admin.patch(f"users/{me}", {"role": "editor"}).status_code == 409  # last active Admin
    accept_invitation(admin.post("users", {"email": "second@orga.test", "role": "admin"}).json_body["link"])
    assert admin.patch(f"users/{me}", {"active": False}).status_code == 409  # still the coordinator
    editor = a["users"]["editor"].id
    assert admin.patch("organization", {"coordinator": editor}).status_code == 200
    assert admin.patch(f"users/{me}", {"role": "editor"}).status_code == 200


@pytest.mark.parametrize(
    "content_type,body",
    [
        ("text/plain", '{"email": "viewer@orga.test", "password": "correct-horse-battery"}'),
        ("application/x-www-form-urlencoded", "email=viewer%40orga.test&password=correct-horse-battery"),
        ("multipart/form-data; boundary=x", '--x\r\nContent-Disposition: form-data; name="email"\r\n\r\nv\r\n--x--'),
    ],
)
def test_sign_in_takes_json_only_which_is_what_keeps_it_safe_from_login_csrf(a, content_type, body):
    """Documented, not enforced: anonymous POST auth/login isn't
    CSRF-checked. A cross-site page can only send it as a simple request (a form: text/plain, urlencoded or multipart);
    application/json needs a CORS preflight, which the API never answers. So the JSON-only parser is the guard: any
    other body is 415 and signs nobody in, even with the right password and CSRF checks on."""
    client = Client(enforce_csrf_checks=True)
    response = client.post("/api/v1/auth/login", body, content_type=content_type, HTTP_ORIGIN="https://evil.example")
    assert response.status_code == 415
    assert client.get("/api/v1/auth/me").status_code == 403
    assert "Access-Control-Allow-Origin" not in response
    preflight = client.options(
        "/api/v1/auth/login", HTTP_ORIGIN="https://evil.example", HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST"
    )
    assert "Access-Control-Allow-Origin" not in preflight


def test_deactivated_user_cannot_sign_in(a, api):
    admin = api(a["users"]["admin"])
    assert admin.patch(f"users/{a['users']['viewer'].id}", {"active": False}).status_code == 200
    login = api().post("auth/login", {"email": "viewer@orga.test", "password": "correct-horse-battery"})
    assert login.status_code in (401, 403)


def test_reference_data_names_are_unique_and_delete_is_refused_when_used(a, api):
    editor = api(a["users"]["editor"])
    assert editor.post("teams", {"name": "it applications"}).status_code == 400  # case-insensitive clash
    editor.patch(f"applications/{a['app'].id}", {"support_team": str(a["team"].id)})
    refused = editor.delete(f"teams/{a['team'].id}")
    assert refused.status_code == 409 and "orga App" in refused.json_body["detail"]
    dept = Department.objects.create(organization=a["org"], name="Legal")
    editor.patch(f"people/{a['people']['dana'].id}", {"department": str(dept.id)})
    assert editor.delete(f"departments/{dept.id}").status_code == 409
    vendor = Vendor.objects.create(organization=a["org"], name="Unused")
    assert editor.delete(f"vendors/{vendor.id}").status_code == 204


def test_anonymize_keeps_history_and_removes_personal_data(a, api):
    admin = api(a["users"]["admin"])
    priya = a["people"]["priya"]
    priya.email = "priya@example.test"
    priya.save()
    result = admin.post(f"people/{priya.id}/anonymize", {"confirm_name": "Priya Raman"}).json_body
    assert result["name"].startswith("Former person #") and result["email"] == "" and result["active"] is False
    event = AuditEvent.objects.get(action="person.anonymized")
    assert "priya" not in str(event.changes).lower()


def test_organization_settings_validation(a, api):
    admin = api(a["users"]["admin"])
    bad = admin.patch(
        "organization", {"timezone": "Mars/Base", "review_interval_days": 2, "coordinator": a["users"]["viewer"].id}
    )
    assert bad.status_code == 400 and set(bad.json_body["errors"]) == {
        "timezone",
        "review_interval_days",
        "coordinator",
    }
    ok = admin.patch("organization", {"timezone": "America/New_York", "review_interval_days": 120})
    assert ok.json_body["timezone"] == "America/New_York"


@pytest.mark.parametrize("key", ["licence_key", "license_key"])
def test_a_license_key_in_organization_settings_is_refused_not_ignored(a, api, key):
    """PATCH organization with a license key answered 200 and changed nothing; now a 400 that says how the
    edition is set, and nothing in the request is saved."""
    refused = api(a["users"]["admin"]).patch("organization", {key: "SW-1234", "timezone": "Europe/Paris"})
    assert refused.status_code == 400 and list(refused.json_body["errors"]) == [key]
    assert "manage.py set_edition" in refused.json_body["errors"][key][0]
    a["org"].refresh_from_db()
    assert a["org"].timezone != "Europe/Paris" and a["org"].licence_key_ciphertext == ""


@pytest.mark.parametrize(
    "make_url",
    [
        lambda b: ("patch", f"people/{b['people']['dana'].id}", {"name": "x"}),
        lambda b: ("post", f"people/{b['people']['dana'].id}/anonymize", {"confirm_name": "Dana Ortiz"}),
        lambda b: ("get", f"people/{b['people']['dana'].id}", None),
        lambda b: ("patch", f"departments/{b['dept'].id}", {"name": "x"}),
        lambda b: ("delete", f"vendors/{b['vendor'].id}", None),
        lambda b: ("patch", f"teams/{b['team'].id}", {"name": "x"}),
        lambda b: ("delete", f"teams/{b['team'].id}", None),
        lambda b: ("patch", f"users/{b['users']['viewer'].id}", {"role": "admin"}),
        lambda b: ("post", f"users/{b['users']['viewer'].id}/reset-password", {}),
    ],
)
def test_other_org_admin_records_are_404(a, b, api, make_url):
    b["dept"] = Department.objects.create(organization=b["org"], name="Legal")
    b["vendor"] = Vendor.objects.create(organization=b["org"], name="Zylker")
    method, url, payload = make_url(b)
    client = api(a["users"]["admin"])
    response = getattr(client, method)(url, payload) if payload is not None else getattr(client, method)(url)
    assert response.status_code == 404
    assert Membership.objects.get(user=b["users"]["viewer"]).role == "viewer"
    assert Vendor.objects.filter(id=b["vendor"].id).exists()
    b["people"]["dana"].refresh_from_db()
    assert b["people"]["dana"].name == "Dana Ortiz"


def test_deactivating_a_member_ends_their_sessions_so_reactivating_doesnt_bring_them_back(a, api):
    """A session cookie issued before deactivation is refused after reactivation."""
    editor = a["users"]["editor"]
    old = api(editor)
    assert old.get("applications").status_code == 200
    admin = api(a["users"]["admin"])
    assert admin.patch(f"users/{editor.id}", {"active": False}).status_code == 200
    assert old.get("applications").status_code in (401, 403)
    assert admin.patch(f"users/{editor.id}", {"active": True}).status_code == 200
    assert old.get("applications").status_code == 403
    assert old.get("auth/me").status_code in (401, 403)
    fresh = api().post("auth/login", {"email": editor.username, "password": "correct-horse-battery"})
    assert fresh.status_code == 200  # signing in again works
