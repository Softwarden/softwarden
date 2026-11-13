"""Multi-organization membership: a user in two organizations sees only the current
one's data, switching is audited, roles are per membership, API tokens keep to their own organization, and the
account-level actions one organization's Admin could use against another are refused."""

import json
import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from api import tokens
from api.models import ApiToken
from catalog.models import RelationshipType, Resource, SoftwareCategory
from core.access import CURRENT_ORGANIZATION
from core.dates import org_today
from core.models import AuditEvent, Membership, Organization, Role, UserIdentity
from notify import digest
from notify.models import NotificationPreference, NotificationRule

SWITCH = "auth/me/organization"


def join(user, org, role, **kw):
    """A membership for an existing user in another organization (newer than the user's first)."""
    membership = Membership.objects.create(user=user, organization=org, role=role, **kw)
    Membership.objects.filter(pk=membership.pk).update(created_at=timezone.now() + timedelta(seconds=1))
    return membership


@pytest.fixture
def both(a, b):
    """orga's Admin is also a Viewer in orgb (orga is the older membership)."""
    user = a["users"]["admin"]
    join(user, b["org"], Role.VIEWER)
    return user


def names(response):
    return {r["name"] for r in response.json_body["results"]}


def switch(client, org):
    return client.post(SWITCH, {"organization_id": str(org.id)})


# ---- what the user sees ------------------------------------------------------------------------------------------


def test_me_lists_every_organization_and_defaults_to_the_oldest(a, b, both, api):
    me = api(both).get("auth/me").json_body
    assert me["organization"]["id"] == str(a["org"].id) == me["current_organization"]
    assert me["role"] == "admin"
    assert me["organizations"] == [
        {"id": str(a["org"].id), "name": "orga", "role": "admin"},
        {"id": str(b["org"].id), "name": "orgb", "role": "viewer"},
    ]
    # A single-organization user's `me` keeps the fields the SPA reads, plus a one-item list.
    single = api(a["users"]["viewer"]).get("auth/me").json_body
    assert {"user", "role", "must_change_password", "organization", "digest_problem"} <= set(single)
    assert [o["id"] for o in single["organizations"]] == [str(a["org"].id)]


def test_lists_and_details_follow_the_current_organization(a, b, both, api):
    client = api(both)
    assert names(client.get("applications")) == {"orga App"}
    assert client.get(f"applications/{b['app'].id}").status_code == 404
    people = {p["name"] for p in client.get("people").json_body}
    assert people == {"Dana Ortiz", "Priya Raman", "Former person"}

    response = switch(client, b["org"])
    assert response.status_code == 200
    assert response.json_body["organization"]["id"] == str(b["org"].id) and response.json_body["role"] == "viewer"
    assert names(client.get("applications")) == {"orgb App"}
    assert client.get(f"applications/{b['app'].id}").status_code == 200
    assert client.get(f"applications/{a['app'].id}").status_code == 404
    assert client.get("auth/me").json_body["current_organization"] == str(b["org"].id)

    switch(client, a["org"])
    assert names(client.get("applications")) == {"orga App"}


def test_role_is_per_membership(a, b, both, api):
    client = api(both)
    assert client.post("applications", {"name": "Made in A", "lifecycle": "active"}).status_code == 201
    switch(client, b["org"])
    refused = client.post("applications", {"name": "Made in B", "lifecycle": "active"})
    assert refused.status_code == 403
    assert client.get("users").status_code == 403  # Admin in orga, Viewer here
    assert not Resource.objects.filter(name="Made in B").exists()


# ---- switching ---------------------------------------------------------------------------------------------------


def test_switch_is_audited_in_both_organizations_without_naming_the_other(a, b, both, api):
    switch(api(both), b["org"])
    out = AuditEvent.objects.get(action="user.organization_switched", organization=a["org"])
    into = AuditEvent.objects.get(action="user.organization_switched", organization=b["org"])
    assert out.actor == into.actor == both
    assert out.changes == {"user": both.id, "direction": "out"}
    assert into.changes == {"user": both.id, "direction": "in"}
    assert str(b["org"].id) not in json.dumps(out.changes) and str(a["org"].id) not in json.dumps(into.changes)


def test_switch_to_the_current_organization_changes_nothing(a, both, api):
    response = switch(api(both), a["org"])
    assert response.status_code == 200 and response.json_body["current_organization"] == str(a["org"].id)
    assert not AuditEvent.objects.filter(action="user.organization_switched").exists()


@pytest.mark.parametrize("value", ["other-org", "random", "not-a-uuid"])
def test_cannot_switch_to_an_organization_you_are_not_in(a, b, both, api, value):
    other = Organization.objects.create(name="orgc")
    target = {"other-org": str(other.id), "random": str(uuid.uuid4()), "not-a-uuid": "not-a-uuid"}[value]
    client = api(a["users"]["editor"])  # in orga only
    response = client.post(SWITCH, {"organization_id": target})
    assert response.status_code == 404
    # orgb is real, but not theirs: as if it didn't exist.
    assert switch(client, b["org"]).status_code == 404
    assert client.get("auth/me").json_body["current_organization"] == str(a["org"].id)
    assert not AuditEvent.objects.filter(action="user.organization_switched").exists()


def test_switch_needs_an_organization_id(both, api):
    response = api(both).post(SWITCH, {})
    assert response.status_code == 400 and "organization_id" in response.json_body["errors"]


def test_switch_rotates_the_session_key_and_csrf_token(a, b, both):
    client = Client()
    client.force_login(both)
    client.get("/api/v1/auth/csrf")
    before_key, before_csrf = client.session.session_key, client.cookies["csrftoken"].value
    response = client.post(
        "/api/v1/" + SWITCH,
        json.dumps({"organization_id": str(b["org"].id)}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=before_csrf,
    )
    assert response.status_code == 200
    assert client.session.session_key != before_key
    assert client.cookies["csrftoken"].value != before_csrf
    assert client.session[CURRENT_ORGANIZATION] == str(b["org"].id)


def test_switch_requires_csrf(b, both, api):
    response = switch(api(both, enforce_csrf=True), b["org"])
    assert response.status_code == 403
    assert not AuditEvent.objects.filter(action="user.organization_switched").exists()


def test_stale_session_value_falls_back_and_never_reaches_the_old_organization(a, b, both, api):
    client = api(both)
    switch(client, b["org"])
    Membership.objects.filter(user=both, organization=b["org"]).delete()  # removed from orgb while signed in
    assert names(client.get("applications")) == {"orga App"}
    assert client.get(f"applications/{b['app'].id}").status_code == 404
    me = client.get("auth/me").json_body
    assert me["current_organization"] == str(a["org"].id)
    assert [o["id"] for o in me["organizations"]] == [str(a["org"].id)]
    assert client.client.session[CURRENT_ORGANIZATION] == str(a["org"].id)


def test_session_value_naming_a_foreign_organization_is_ignored(a, b, api):
    client = api(a["users"]["viewer"])
    session = client.client.session
    session[CURRENT_ORGANIZATION] = str(b["org"].id)  # as if tampered with or left from another account
    session.save()
    assert names(client.get("applications")) == {"orga App"}
    assert client.get("auth/me").json_body["current_organization"] == str(a["org"].id)


def test_password_change_required_in_one_organization_holds_in_all(a, b, both, api):
    Membership.objects.filter(user=both, organization=b["org"]).update(must_change_password=True)
    client = api(both)
    assert client.get("applications").status_code == 403
    assert switch(client, b["org"]).status_code == 403
    assert client.get("auth/me").json_body["must_change_password"] is True
    done = client.post("auth/password", {"current_password": "correct-horse-battery", "new_password": "a-new-pass-123"})
    assert done.status_code == 204
    assert not Membership.objects.filter(user=both, must_change_password=True).exists()
    assert client.get("applications").status_code == 200
    # Account events are written in every organization the user belongs to.
    assert set(AuditEvent.objects.filter(action="user.password_changed").values_list("organization_id", flat=True)) == {
        a["org"].id,
        b["org"].id,
    }


def test_sign_in_with_two_organizations(a, b, both, api):
    from django.core.cache import cache

    cache.clear()  # sign-in is throttled (10 a minute) across the test process
    client = api()
    bad = client.post("auth/login", {"email": both.username, "password": "wrong-password-here"})
    assert bad.status_code in (401, 403)
    assert set(AuditEvent.objects.filter(action="user.login_failed").values_list("organization_id", flat=True)) == {
        a["org"].id,
        b["org"].id,
    }
    good = client.post("auth/login", {"email": both.username, "password": "correct-horse-battery"})
    assert good.status_code == 200 and len(good.json_body["organizations"]) == 2
    assert good.json_body["current_organization"] == str(a["org"].id)
    assert AuditEvent.objects.get(action="user.login").organization_id == a["org"].id


def test_account_with_no_membership_cannot_sign_in(db, api):
    get_user_model().objects.create_user(
        username="loner@x.test", email="loner@x.test", password="correct-horse-battery"
    )
    response = api().post("auth/login", {"email": "loner@x.test", "password": "correct-horse-battery"})
    assert response.status_code == 403


# ---- API tokens keep to their own organization --------------------------------------------------------------------


@pytest.fixture
def b_token(a, b):
    """A token in orgb for orgb's Admin, who is also an Editor in orga. orgb is their oldest membership, so it is
    also what the session fallback would pick; the token must ignore that and anything in the session."""
    user = b["users"]["admin"]
    join(user, a["org"], Role.EDITOR)
    owner = Membership.objects.get(user=user, organization=b["org"])
    token, secret = tokens.create_token(
        b["org"], user, owner, "Script", read_only=True, expires_on=org_today(b["org"]) + timedelta(days=30)
    )
    return user, token, secret


def bearer(secret):
    return Client(HTTP_AUTHORIZATION=f"Bearer {secret}")


def test_token_reads_only_its_own_organization(a, b, b_token, api):
    user, _, secret = b_token
    # The same user's session, switched to orga, changes nothing for the token.
    session = api(user)
    switch(session, a["org"])
    assert names(session.get("applications")) == {"orga App"}

    client = bearer(secret)
    listed = client.get("/api/v1/applications").json()
    assert {r["name"] for r in listed["results"]} == {"orgb App"}
    assert client.get(f"/api/v1/applications/{a['app'].id}").status_code == 404
    me = client.get("/api/v1/auth/me").json()
    assert me["current_organization"] == str(b["org"].id)
    assert [o["id"] for o in me["organizations"]] == [str(b["org"].id)]  # a token sees only its own organization


def test_token_whose_oldest_membership_is_elsewhere_still_uses_its_own(a, b, api):
    """The reverse of the fixture: the token is in the newer membership's organization."""
    user = a["users"]["editor"]
    newer = join(user, b["org"], Role.EDITOR)
    _, secret = tokens.create_token(
        b["org"],
        b["users"]["admin"],
        newer,
        "Script",
        read_only=True,
        expires_on=org_today(b["org"]) + timedelta(days=5),
    )
    listed = bearer(secret).get("/api/v1/applications").json()
    assert {r["name"] for r in listed["results"]} == {"orgb App"}


def test_token_cannot_switch_organization(a, b_token):
    _, _, secret = b_token
    response = bearer(secret).post(
        "/api/v1/" + SWITCH, json.dumps({"organization_id": str(a["org"].id)}), content_type="application/json"
    )
    assert response.status_code == 403
    assert not AuditEvent.objects.filter(action="user.organization_switched").exists()


def test_token_stops_when_its_owner_leaves_its_organization_but_stays_elsewhere(a, b, b_token):
    user, token, secret = b_token
    Membership.objects.filter(user=user, organization=b["org"]).delete()
    assert Membership.objects.filter(user=user, organization=a["org"]).exists()
    assert bearer(secret).get("/api/v1/applications").status_code == 401


# ---- Admin user management ---------------------------------------------------------------------------------------


def accept_signed_in(user, link):
    """The invited account accepts its invitation while signed in as itself."""
    client = Client()
    client.force_login(user)
    token = link.rsplit("/invite#", 1)[1]
    response = client.post("/api/v1/auth/invitations/accept", {"token": token}, content_type="application/json")
    assert response.status_code == 200, response.json()
    return client


def test_inviting_an_existing_account_adds_a_membership_when_it_accepts(a, b, api):
    existing = b["users"]["editor"]
    response = api(a["users"]["admin"]).post("users", {"email": existing.email, "role": "viewer"})
    assert response.status_code == 201, response.json_body
    assert "temporary_password" not in response.json_body and response.json_body["email"] == existing.email
    assert not Membership.objects.filter(user=existing, organization=a["org"]).exists()  # not without consent
    accept_signed_in(existing, response.json_body["link"])
    assert Membership.objects.get(user=existing, organization=a["org"]).role == Role.VIEWER
    assert Membership.objects.get(user=existing, organization=b["org"]).role == Role.EDITOR  # untouched
    existing.refresh_from_db()
    assert existing.check_password("correct-horse-battery")  # they keep their own password
    event = AuditEvent.objects.get(action="user.invited", organization=a["org"])
    assert "user" not in event.changes and "existing_account" not in event.changes  # nothing before consent
    accepted = AuditEvent.objects.get(action="invitation.accepted", organization=a["org"])
    assert accepted.changes["user"] == existing.id and accepted.changes["new_account"] is False


def test_inviting_someone_already_in_the_organization_is_refused(a, api):
    response = api(a["users"]["admin"]).post("users", {"email": a["users"]["viewer"].email, "role": "editor"})
    assert response.status_code == 400 and "email" in response.json_body["errors"]


def test_account_level_changes_to_a_shared_account_are_refused(a, b, api):
    shared = b["users"]["admin"]
    join(shared, a["org"], Role.VIEWER)
    UserIdentity.objects.create(user=shared, issuer="https://idp.example.test", subject="o")
    client = api(a["users"]["admin"])
    assert client.post(f"users/{shared.id}/reset-password").status_code == 409
    assert client.patch(f"users/{shared.id}", {"active": False}).status_code == 409
    assert client.delete(f"users/{shared.id}/identity").status_code == 409
    shared.refresh_from_db()
    assert shared.is_active and shared.check_password("correct-horse-battery")
    assert UserIdentity.objects.filter(user=shared).exists()
    # The role in this organization is this organization's to change.
    assert client.patch(f"users/{shared.id}", {"role": "editor"}).status_code == 200
    assert Membership.objects.get(user=shared, organization=b["org"]).role == Role.ADMIN


def test_removing_a_member_removes_only_that_membership(a, b, api):
    shared = b["users"]["editor"]
    here = join(shared, a["org"], Role.EDITOR)
    NotificationPreference.objects.create(organization=a["org"], membership=here, digest_enabled=False)
    token, _ = tokens.create_token(
        a["org"],
        a["users"]["admin"],
        here,
        "Script",
        read_only=True,
        expires_on=org_today(a["org"]) + timedelta(days=5),
    )
    other_token, _ = tokens.create_token(
        b["org"],
        b["users"]["admin"],
        Membership.objects.get(user=shared, organization=b["org"]),
        "Script",
        read_only=True,
        expires_on=org_today(b["org"]) + timedelta(days=5),
    )
    assert api(a["users"]["editor"]).delete(f"users/{shared.id}").status_code == 403
    assert api(a["users"]["admin"]).delete(f"users/{shared.id}").status_code == 204
    assert not Membership.objects.filter(user=shared, organization=a["org"]).exists()
    assert Membership.objects.filter(user=shared, organization=b["org"]).exists()
    assert not NotificationPreference.objects.filter(membership_id=here.id).exists()
    assert ApiToken.objects.get(pk=token.pk).revoked_at is not None
    assert ApiToken.objects.get(pk=other_token.pk).revoked_at is None
    shared.refresh_from_db()
    assert shared.is_active
    event = AuditEvent.objects.get(action="user.removed")
    assert event.organization_id == a["org"].id and event.changes["api_access_revoked"] == 1
    # orgb's Admin can't remove someone from orga: their ID is not found there.
    assert api(b["users"]["admin"]).delete(f"users/{a['users']['viewer'].id}").status_code == 404


def test_removal_guards(a, b, api):
    client = api(a["users"]["admin"])
    assert client.delete(f"users/{a['users']['viewer'].id}").status_code == 409  # their only organization
    # orga's only Admin, also in orgb: still the last Admin here.
    join(a["users"]["admin"], b["org"], Role.VIEWER)
    assert client.delete(f"users/{a['users']['admin'].id}").status_code == 409
    # The coordinator (orga's editor) must be replaced first.
    a["org"].coordinator = a["users"]["editor"]
    a["org"].save()
    join(a["users"]["editor"], b["org"], Role.VIEWER)
    assert client.delete(f"users/{a['users']['editor'].id}").status_code == 409
    assert Membership.objects.filter(organization=a["org"]).count() == 3


# ---- notifications are per membership ----------------------------------------------------------------------------


def test_digest_preference_is_per_membership(a, b, both, api):
    client = api(both)
    off = client.patch("auth/me/preferences", {"digest_enabled": False})
    assert off.status_code == 200 and off.json_body["digest_enabled"] is False
    pref = NotificationPreference.objects.get()
    assert pref.organization_id == a["org"].id and pref.membership.organization_id == a["org"].id
    assert AuditEvent.objects.get(action="preference.updated").organization_id == a["org"].id
    switch(client, b["org"])
    assert client.get("auth/me/preferences").json_body["digest_enabled"] is True
    assert both.id not in digest.recipients(a["org"])
    assert both.id in digest.recipients(b["org"])


def test_digest_preference_rejects_a_non_boolean(both, api):
    response = api(both).patch("auth/me/preferences", {"digest_enabled": "no"})
    assert response.status_code == 400
    assert not NotificationPreference.objects.exists()


# ---- review follow-ups: invite leakage, member list, recovery, audit names -----------------------------------------


def test_inviting_an_existing_account_hides_its_activity_elsewhere(a, b, api):
    existing = b["users"]["editor"]
    get_user_model().objects.filter(pk=existing.pk).update(last_login=timezone.now())
    UserIdentity.objects.create(user=existing, issuer="https://idp.example.test", subject="o")
    admin = api(a["users"]["admin"])
    body = admin.post("users", {"email": existing.email, "role": "viewer"}).json_body
    assert not {"last_login", "entra_linked", "identities"} & set(body)  # an invitation describes no account
    accept_signed_in(existing, body["link"])
    row = {u["id"]: u for u in admin.get("users").json_body}[existing.id]
    assert row["last_login"] is None and row["entra_linked"] is None and row["identities"] is None


def test_inviting_a_deactivated_account_answers_as_any_other_invitation(a, b, api):
    """The answer never tells an Admin that an address has an account elsewhere, deactivated or not (it was a
    generic 400, still different from a new address's 201). The emailed link answers as unavailable."""
    from django.test import Client

    from catalog import invitations

    gone = b["users"]["viewer"]
    get_user_model().objects.filter(pk=gone.pk).update(is_active=False)
    response = api(a["users"]["admin"]).post("users", {"email": gone.email, "role": "viewer"})
    assert response.status_code == 201 and "deactivated" not in json.dumps(response.json_body)
    emailed = invitations.mailbox_token(response.json_body["link"].rsplit("#", 1)[1])
    preview = Client().post("/api/v1/auth/invitations/preview", {"token": emailed}, content_type="application/json")
    assert preview.status_code == 404
    assert not Membership.objects.filter(user=gone, organization=a["org"]).exists()


def test_member_list_reports_account_level_facts(a, b, api):
    shared = b["users"]["editor"]
    get_user_model().objects.filter(pk=shared.pk).update(last_login=timezone.now())
    join(shared, a["org"], Role.VIEWER)
    Membership.objects.filter(user=shared, organization=b["org"]).update(must_change_password=True)
    local = a["users"]["editor"]
    get_user_model().objects.filter(pk=local.pk).update(last_login=timezone.now())
    rows = {u["id"]: u for u in api(a["users"]["admin"]).get("users").json_body}
    # The flag set in orgb holds here too; activity outside orga is not shown.
    assert rows[shared.id]["must_change_password"] is True
    assert rows[shared.id]["last_login"] is None and rows[shared.id]["entra_linked"] is None
    # Someone only in orga keeps both.
    assert rows[local.id]["last_login"] is not None and rows[local.id]["entra_linked"] is False


def test_reset_admin_any_member_recovers_a_shared_non_admin(a, b):
    from io import StringIO

    from django.core.management import CommandError, call_command

    shared = b["users"]["viewer"]
    join(shared, a["org"], Role.EDITOR)
    with pytest.raises(CommandError, match="--any-member"):
        call_command("reset_admin", shared.email, stdout=StringIO())  # Admins only by default, as before
    out = StringIO()
    call_command("reset_admin", shared.email, "--any-member", stdout=out)
    temp = out.getvalue().split(": ", 1)[1].splitlines()[0]
    shared.refresh_from_db()
    assert shared.check_password(temp)
    assert Membership.objects.filter(user=shared, must_change_password=True).count() == 1
    assert set(AuditEvent.objects.filter(action="user.password_reset").values_list("organization_id", flat=True)) == {
        a["org"].id,
        b["org"].id,
    }
    assert temp not in json.dumps(list(AuditEvent.objects.values_list("changes", flat=True)))


def test_audit_log_still_names_a_removed_member(a, b, api):
    shared = b["users"]["editor"]
    admin = api(a["users"]["admin"])
    invited = admin.post("users", {"email": shared.email, "role": "viewer"})
    assert invited.status_code == 201
    accept_signed_in(shared, invited.json_body["link"])
    assert admin.delete(f"users/{shared.id}").status_code == 204
    events = {e["action"]: e for e in admin.get("audit-events").json_body["results"]}
    name = shared.get_full_name() or shared.email
    assert events["invitation.accepted"]["summary"].startswith(name)  # user.invited names nobody before consent
    assert events["user.removed"]["summary"].startswith(name)
    assert events["user.removed"]["label"] == "Removed from organization"
    assert "a deleted record" not in json.dumps(events)


# ---- migration over existing rows (core 0003, notify 0002) --------------------------------------------------------

BEFORE_MULTI_ORG = [("core", "0002_auditevent_org_id"), ("notify", "0001_initial")]


@pytest.mark.django_db(transaction=True)
def test_migration_keeps_memberships_and_moves_preferences_to_them():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE_MULTI_ORG)
    old = executor.loader.project_state(BEFORE_MULTI_ORG).apps
    Org, OldMembership = old.get_model("core", "Organization"), old.get_model("core", "Membership")
    OldPreference = old.get_model("notify", "NotificationPreference")
    User = old.get_model("auth", "User")
    org = Org.objects.create(name="Multi-org migration (synthetic)")
    other = Org.objects.create(name="Multi-org migration, other (synthetic)")
    ann = User.objects.create(username="ann@migrate.test", email="ann@migrate.test")
    bob = User.objects.create(username="bob@migrate.test", email="bob@migrate.test")
    cat = User.objects.create(username="cat@migrate.test", email="cat@migrate.test")
    OldMembership.objects.create(user=ann, organization=org, role="admin", must_change_password=False)
    OldMembership.objects.create(user=bob, organization=org, role="viewer", must_change_password=True)
    OldMembership.objects.create(user=cat, organization=org, role="editor", must_change_password=False)
    OldPreference.objects.create(user=ann, organization=org, digest_enabled=False)
    OldPreference.objects.create(user=bob, organization=org, digest_enabled=True)
    # Saved in an organization cat isn't a member of: no membership to move to, so it is dropped.
    OldPreference.objects.create(user=cat, organization=other, digest_enabled=False)

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    try:
        rows = {m.user.username: m for m in Membership.objects.select_related("user").filter(organization_id=org.id)}
        assert {k: (m.role, m.must_change_password) for k, m in rows.items()} == {
            "ann@migrate.test": ("admin", False),
            "bob@migrate.test": ("viewer", True),
            "cat@migrate.test": ("editor", False),
        }
        assert all(isinstance(m.id, uuid.UUID) for m in rows.values()) and len({m.id for m in rows.values()}) == 3
        prefs = {
            p.membership.user.username: p for p in NotificationPreference.objects.select_related("membership__user")
        }
        assert {k: p.digest_enabled for k, p in prefs.items()} == {"ann@migrate.test": False, "bob@migrate.test": True}
        assert {p.organization_id for p in prefs.values()} == {org.id}
        assert all(isinstance(p.id, uuid.UUID) for p in prefs.values())
        # The new shape works: a second organization for ann, with its own preference.
        second = Membership.objects.create(
            user_id=rows["ann@migrate.test"].user_id, organization_id=other.id, role="viewer"
        )
        NotificationPreference.objects.create(organization_id=other.id, membership=second, digest_enabled=True)
    finally:
        NotificationPreference.objects.all().delete()
        Membership.objects.filter(organization_id__in=[org.id, other.id]).delete()
        get_user_model().objects.filter(username__endswith="@migrate.test").delete()
        SoftwareCategory.objects.filter(organization_id__in=[org.id, other.id]).delete()  # seeded by catalog 0023
        RelationshipType.objects.filter(organization_id__in=[org.id, other.id]).delete()  # seeded by catalog 0033
        NotificationRule.objects.filter(organization_id__in=[org.id, other.id]).delete()  # seeded by notify 0005
        Organization.objects.filter(id__in=[org.id, other.id]).delete()


def test_the_e2e_multi_org_fixture_route_does_not_exist_outside_the_e2e_suite(a, api):
    """api/e2e_views.multi_org_user (SPA-2's organization switcher spec) is routed only for Playwright's _e2e
    database; here it is a 404 and makes no organization or account."""
    from django.contrib.auth import get_user_model

    from core.models import Organization

    response = api(a["users"]["admin"]).client.post("/api/v1/e2e/multi-org-user")
    assert response.status_code == 404
    assert not Organization.objects.filter(name="Fabrikam (synthetic)").exists()
    assert not get_user_model().objects.filter(username="multi@example.test").exists()
