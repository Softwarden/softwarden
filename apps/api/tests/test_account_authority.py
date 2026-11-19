"""Account authority.

- The routes that issue or change sign-in authority need a signed-in session; any API token is refused there
  (core/impersonation_routes.SESSION_ONLY_ROUTES, enforced by core.drf.RolePermission). The walk below sends an Admin's
  read-write token to every one of them, and to every other session-only route, so a new one can't slip past.
- Every current-password check counts with the account's sign-in attempts (admin_services.check_current_password)
  and a failure is audited; an Admin can't reset their own password.
- An Admin's open invitations stop when they stop being an Admin.
- A password reset or change revokes the account's calendar feeds; no feed is served while a change is forced."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from api import tokens
from catalog import admin_services, services
from core import capabilities, counters, signin_limits
from core.drf import PUBLIC
from core.impersonation_routes import (
    RESTRICTED,
    SESSION_ONLY_CATEGORIES,
    SESSION_ONLY_ROUTES,
    TOKEN_MAY_USE,
)
from core.models import AuditEvent, Invitation, Membership
from money import calendar_feeds
from money.models import CalendarFeed
from softwarden import ee
from tests.test_problems import dummy
from tests.test_url_coverage import declarations

PASSWORD = "correct-horse-battery"
START = 1_800_000_000 - (1_800_000_000 % 3600)
FEED = "/api/v1/calendar/renewals.ics"


@pytest.fixture
def clock(monkeypatch):
    """The shared counters' clock, held still; move it with now[0] += seconds."""
    now = [float(START)]
    monkeypatch.setattr(counters, "clock", lambda: now[0])
    return now


class TokenClient:
    def __init__(self, org, owner, creator, read_only=False):
        membership = Membership.objects.get(organization=org, user=owner)
        expires = services.org_today(org) + timedelta(days=30)
        self.token, secret = tokens.create_token(org, creator, membership, "Script", read_only, expires)
        self.client = Client(enforce_csrf_checks=True, HTTP_AUTHORIZATION=f"Bearer {secret}")

    def send(self, method: str, path: str, body=None):
        if method == "GET":
            return self.client.get(path)
        return getattr(self.client, method.lower())(path, json.dumps(body or {}), content_type="application/json")


@pytest.fixture
def admin_token(a):
    org, _ = capabilities.set_edition(a["org"], "enterprise")  # no capability refusal hides the session refusal
    a["org"] = org
    return TokenClient(org, a["users"]["admin"], a["users"]["admin"])


@pytest.fixture
def admin2(a):
    user = get_user_model().objects.create_user(
        username="admin2@orga.test", email="admin2@orga.test", password=PASSWORD
    )
    Membership.objects.create(user=user, organization=a["org"], role="admin")
    return user


# ---- Sign-in authority needs a signed-in session --------------------------------------------------------------

# The routes the review named; SESSION_ONLY_ROUTES must hold each of them (and holds more: the categories they're in).
NAMED = {
    ("POST", "api/v1/users"),
    ("POST", "api/v1/invitations"),
    ("POST", "api/v1/invitations/<uuid:pk>/resend"),
    ("POST", "api/v1/users/<int:pk>/reset-password"),
    ("PATCH", "api/v1/users/<int:pk>"),
    ("POST", "api/v1/pending-access/<uuid:pk>/approve"),
    ("PATCH", "api/v1/sso/settings"),
    ("POST", "api/v1/sso/settings/test"),
    ("POST", "api/v1/sso/settings/activate"),
    ("POST", "api/v1/sso/settings/disable"),
    ("POST", "api/v1/sso/settings/domains/verify"),
    ("POST", "api/v1/auth/password"),
}
# Routes outside SESSION_ONLY_CATEGORIES whose views refuse tokens themselves (core.drf.SessionOnly, require_session).
OTHER_SESSION_ONLY = {
    *RESTRICTED["tokens"],
    *RESTRICTED["organization deletion"],
    ("POST", "api/v1/organization/exports"),
    ("GET", "api/v1/organization/exports/<uuid:pk>/download"),
    ("GET", "api/v1/api-tokens"),
    *([("GET", "api/v1/scim/tokens")] if ee.INSTALLED else []),  # the optional package's SCIM tokens
    ("GET", "api/v1/calendar-feed"),
    ("DELETE", "api/v1/calendar-feed"),
}


def signed_in_routes(pairs) -> list[tuple[str, str]]:
    """The pairs whose route answers that method for a signed-in account (public routes give a token nothing)."""
    found, _ = declarations()
    return sorted(
        (method, route)
        for method, route in pairs
        if found.get((method.lower(), route), found.get(("*", route))) not in (None, PUBLIC)
    )


def test_the_named_routes_are_session_only_and_the_exemptions_are_placed():
    assert NAMED <= SESSION_ONLY_ROUTES, NAMED - SESSION_ONLY_ROUTES
    in_categories = {pair for category in SESSION_ONLY_CATEGORIES for pair in RESTRICTED[category]}
    assert set(TOKEN_MAY_USE) <= in_categories  # an exemption names a route the categories hold
    assert all(reason.strip() for reason in TOKEN_MAY_USE.values())
    assert not set(TOKEN_MAY_USE) & SESSION_ONLY_ROUTES
    # Every route a signed-in account uses in those categories is either session-only or exempted with a reason.
    for pair in signed_in_routes(in_categories):
        assert pair in SESSION_ONLY_ROUTES or pair in TOKEN_MAY_USE, pair


@pytest.mark.parametrize("method,route", signed_in_routes(SESSION_ONLY_ROUTES | OTHER_SESSION_ONLY))
def test_an_admin_read_write_token_is_refused_on_every_session_only_route(method, route, a, admin_token):
    before = AuditEvent.objects.count()
    response = admin_token.send(method, dummy(route))
    assert response.status_code == 403, (method, route, response.status_code, response.content[:300])
    assert "session" in response.json()["detail"].lower(), response.json()
    assert AuditEvent.objects.count() == before  # nothing ran


def test_a_read_only_token_is_refused_too(a):
    token = TokenClient(a["org"], a["users"]["admin"], a["users"]["admin"], read_only=True)
    assert token.send("POST", "/api/v1/invitations", {"email": "x@orga.test", "role": "admin"}).status_code == 403
    assert token.send("GET", "/api/v1/invitations").status_code == 200  # reads stay open to tokens


def test_an_admin_token_cant_invite_reset_or_promote(a, admin_token):
    viewer = a["users"]["viewer"]
    invited = admin_token.send("POST", "/api/v1/invitations", {"email": "new@orga.test", "role": "admin"})
    assert invited.status_code == 403 and "link" not in invited.json()
    assert admin_token.send("POST", "/api/v1/users", {"email": "new@orga.test", "role": "admin"}).status_code == 403
    reset = admin_token.send("POST", f"/api/v1/users/{viewer.id}/reset-password")
    assert reset.status_code == 403 and "temporary_password" not in reset.json()
    assert admin_token.send("PATCH", f"/api/v1/users/{viewer.id}", {"role": "admin"}).status_code == 403
    changed = admin_token.send(
        "POST", "/api/v1/auth/password", {"current_password": PASSWORD, "new_password": "x" * 20}
    )
    assert changed.status_code == 403
    assert not Invitation.objects.exists()
    assert Membership.objects.get(user=viewer).role == "viewer"
    assert not Membership.objects.filter(user=viewer, must_change_password=True).exists()
    # The route's reads and an exempted write still work with the token.
    assert admin_token.send("GET", "/api/v1/users").status_code == 200


def test_a_session_still_does_all_of_it(a, api):
    admin, viewer = api(a["users"]["admin"]), a["users"]["viewer"]
    assert admin.post("invitations", {"email": "new@orga.test", "role": "viewer"}).status_code == 201
    assert admin.patch(f"users/{viewer.id}", {"role": "editor"}).status_code == 200
    assert admin.post(f"users/{viewer.id}/reset-password").status_code == 200


# ---- Counted current-password checks --------------------------------------------------------------------------


def change(client, current, new="a-brand-new-passphrase-2026"):
    return client.post("auth/password", {"current_password": current, "new_password": new})


def test_wrong_current_passwords_are_counted_and_audited(a, api, clock):
    editor = a["users"]["editor"]
    client = api(editor)
    for _ in range(signin_limits.ACCOUNT_FREE):
        response = change(client, "not-my-password")
        assert response.status_code == 400 and set(response.json_body["errors"]) == {"current_password"}
    limited = change(client, PASSWORD)  # the right one too, inside the wait
    assert limited.status_code == 429 and int(limited["Retry-After"]) == 1
    editor.refresh_from_db()
    assert editor.check_password(PASSWORD)
    failed = AuditEvent.objects.filter(organization=a["org"], action="user.password_check_failed")
    assert failed.count() == signin_limits.ACCOUNT_FREE
    event = failed.first()
    assert event is not None
    assert event.actor_id == editor.id and event.changes["purpose"] == "password_change"
    assert "not-my-password" not in json.dumps(event.changes)
    clock[0] += 1
    assert change(client, PASSWORD).status_code == 204
    # A match forgets the count: the next mistake is free again.
    editor.refresh_from_db()
    client = api(editor)
    assert change(client, "a-brand-new-passphrase-2026", "x").status_code == 400  # current right, new refused
    assert change(client, "not-my-password").status_code == 400


def test_password_change_and_sign_in_share_the_count(a, api, clock):
    editor = a["users"]["editor"]
    for _ in range(signin_limits.ACCOUNT_FREE):
        assert change(api(editor), "not-my-password").status_code == 400
    response = Client().post(
        "/api/v1/auth/login",
        json.dumps({"email": editor.email, "password": PASSWORD}),
        content_type="application/json",
    )
    assert response.status_code == 429


def test_organization_deletion_reauthentication_is_counted(a, api, clock):
    capabilities.set_edition(a["org"], "cloud")
    client = api(a["users"]["admin"])
    for _ in range(signin_limits.ACCOUNT_FREE):
        response = client.post("organization/deletion", {"confirm": "orga", "password": "nope-nope-nope"})
        assert response.status_code == 403
        assert response.json_body["type"] == "/problems/reauthentication-required"
    assert client.post("organization/deletion", {"confirm": "orga", "password": PASSWORD}).status_code == 429
    a["org"].refresh_from_db()
    assert a["org"].deletion_due_at is None
    events = AuditEvent.objects.filter(organization=a["org"], action="user.password_check_failed")
    assert events.count() == signin_limits.ACCOUNT_FREE
    assert {e.changes["purpose"] for e in events} == {"reauthentication"}
    clock[0] += 1
    assert client.post("organization/deletion", {"confirm": "orga", "password": PASSWORD}).status_code == 200


def test_a_wrong_slug_costs_no_password_attempt(a, api, clock):
    capabilities.set_edition(a["org"], "cloud")
    client = api(a["users"]["admin"])
    for _ in range(signin_limits.ACCOUNT_FREE + 2):
        assert client.post("organization/deletion", {"confirm": "nope", "password": "x"}).status_code == 400
    assert not AuditEvent.objects.filter(action="user.password_check_failed").exists()


def test_the_check_refuses_to_run_inside_a_transaction(a):
    """A count inside the caller's transaction would roll back with the refusal: the check fails loudly instead."""
    from django.db import transaction

    with pytest.raises(RuntimeError), transaction.atomic():
        admin_services.check_current_password(a["users"]["editor"], PASSWORD, "test")


def test_an_admin_cant_reset_their_own_password(a, api):
    admin = a["users"]["admin"]
    response = api(admin).post(f"users/{admin.id}/reset-password")
    assert response.status_code == 409 and response.json_body["detail"] == admin_services.OWN_RESET
    admin.refresh_from_db()
    assert admin.check_password(PASSWORD)
    assert not Membership.objects.filter(user=admin, must_change_password=True).exists()


# ---- Authority outliving a demoted creator --------------------------------------------------------------------


def accept(token: str):
    return Client().post(
        "/api/v1/auth/invitations/accept",
        json.dumps({"token": token, "name": "New Person", "password": "a-long-new-passphrase-1"}),
        content_type="application/json",
    )


@pytest.mark.parametrize("change", [{"role": "editor"}, {"active": False}])
def test_an_invitation_dies_when_its_creator_loses_admin(a, api, admin2, change, settings):
    settings.SOFTWARDEN_BASE_URL = "https://softwarden.example.test"
    created = api(admin2).post("invitations", {"email": "late@orga.test", "role": "admin"}).json_body
    other = api(a["users"]["admin"]).post("invitations", {"email": "kept@orga.test", "role": "viewer"}).json_body
    assert api(a["users"]["admin"]).patch(f"users/{admin2.id}", change).status_code == 200
    token = created["link"].rsplit("/invite#", 1)[1]
    response = accept(token)
    assert response.status_code == 404 and response.json()["type"] == "/problems/invitation-unavailable"
    assert not get_user_model().objects.filter(username="late@orga.test").exists()
    event = AuditEvent.objects.get(action="invitation.revoked", target_id=created["id"])
    assert event.actor_id == a["users"]["admin"].id and event.changes["reason"] == "creator_no_longer_admin"
    updated = AuditEvent.objects.filter(action="user.updated", changes__user=admin2.id).latest("created_at")
    assert updated.changes["invitations_revoked"] == 1
    # Another Admin's invitation stands.
    assert Invitation.objects.get(id=other["id"]).revoked_at is None


def test_an_invitation_dies_when_its_creator_is_removed(a, b, api, admin2):
    Membership.objects.create(user=admin2, organization=b["org"], role="viewer")
    created = api(admin2).post("invitations", {"email": "late@orga.test", "role": "admin"}).json_body
    assert api(a["users"]["admin"]).delete(f"users/{admin2.id}").status_code == 204
    assert Invitation.objects.get(id=created["id"]).revoked_at is not None
    event = AuditEvent.objects.get(action="invitation.revoked", target_id=created["id"])
    assert event.changes["reason"] == "creator_removed"


# ---- Calendar feeds and passwords -----------------------------------------------------------------------------


def subscribe(org, user) -> str:
    _, secret = calendar_feeds.create_feed(org, user)
    return secret


def fetch(secret):
    return Client().get(f"{FEED}?token={secret}", HTTP_ACCEPT="text/calendar")


def test_an_admin_reset_revokes_the_members_feeds(a, b, api):
    viewer = a["users"]["viewer"]
    Membership.objects.filter(user=viewer).update(role="viewer")
    secret = subscribe(a["org"], viewer)
    assert fetch(secret).status_code == 200
    assert api(a["users"]["admin"]).post(f"users/{viewer.id}/reset-password").status_code == 200
    assert fetch(secret).status_code == 404
    assert CalendarFeed.objects.get(user=viewer).revoked_at is not None
    event = AuditEvent.objects.get(organization=a["org"], action="calendar_feed.revoked")
    assert event.actor_id == a["users"]["admin"].id and event.changes["reason"] == "password_reset"


def test_your_own_password_change_revokes_your_feeds(a, api):
    editor = a["users"]["editor"]
    secret = subscribe(a["org"], editor)
    assert change(api(editor), PASSWORD).status_code == 204
    assert fetch(secret).status_code == 404
    event = AuditEvent.objects.get(organization=a["org"], action="calendar_feed.revoked")
    assert event.changes["reason"] == "password_changed"


def test_a_reset_link_revokes_the_feeds(a, settings):
    from catalog import password_reset
    from core import email_tokens
    from core.models import PasswordReset

    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    editor = a["users"]["editor"]
    secret = subscribe(a["org"], editor)
    token, digest = email_tokens.new_token()
    PasswordReset.objects.create(user=editor, token_hash=digest, expires_at=timezone.now() + timedelta(hours=1))
    password_reset.confirm_reset({"token": token, "password": "a-long-new-passphrase-1"})
    assert fetch(secret).status_code == 404


def test_no_feed_while_a_password_change_is_forced(a, b):
    editor = a["users"]["editor"]
    secret = subscribe(a["org"], editor)
    Membership.objects.create(user=editor, organization=b["org"], role="viewer", must_change_password=True)
    assert fetch(secret).status_code == 404  # the flag is the account's, set in any organization
    Membership.objects.filter(user=editor).update(must_change_password=False)
    assert fetch(secret).status_code == 200


# ---- The staff-only System view needs the second factor too ---------------------------------------------------


def test_staff_get_the_installation_view_only_after_their_second_factor(a, b, settings, monkeypatch):
    from core import totp
    from tests.test_platform import make_staff
    from tests.test_staff_second_factor import enroll, sign_in

    now = float(START)
    monkeypatch.setattr(totp, "clock", lambda: now)
    monkeypatch.setattr(counters, "clock", lambda: now)
    settings.SOFTWARDEN_PLATFORM_TOTP = True
    make_staff(org=b["org"])  # b is not the installation's first organization
    client, response = sign_in()
    assert response.status_code == 200
    assert client.get("/api/v1/system").json()["scope"] == "organization"
    enroll(client, now)
    assert client.get("/api/v1/system").json()["scope"] == "installation"


# ---- The organization's sign-in settings need a signed-in session ------------------------------------

SIGN_IN_BODIES: list[dict[str, object]] = [
    {"sso_auto_provision": True},
    {"scim_default_role": "editor"},
    {"scim_role_groups": None},
    {"timezone": "Europe/London", "sso_auto_provision": False},  # one sign-in key refuses the whole body
]


def test_every_sign_in_setting_is_covered():
    from core.impersonation_routes import RESTRICTED_SETTINGS

    assert {key for body in SIGN_IN_BODIES for key in body} >= RESTRICTED_SETTINGS


@pytest.mark.parametrize("body", SIGN_IN_BODIES)
def test_a_token_cant_change_the_organizations_sign_in_settings(body, a, admin_token, api):
    from core.models import Organization

    before = Organization.objects.get(pk=a["org"].pk)
    events = AuditEvent.objects.count()
    response = admin_token.send("PATCH", "/api/v1/organization", body)
    assert response.status_code == 403
    assert response.json()["detail"].startswith("This changes who can sign in"), response.json()
    after = Organization.objects.get(pk=a["org"].pk)
    assert (after.settings or {}) == (before.settings or {}) and after.timezone == before.timezone
    assert AuditEvent.objects.count() == events
    # A signed-in Admin makes the same change (the SCIM settings need the optional ee package's `scim` capability).
    scim = any(key.startswith("scim_") for key in body)
    expected = 403 if scim and not ee.INSTALLED else 200
    assert api(a["users"]["admin"]).patch("organization", body).status_code == expected


def test_a_token_still_changes_the_other_settings(a, admin_token):
    response = admin_token.send("PATCH", "/api/v1/organization", {"timezone": "Europe/London"})
    assert response.status_code == 200 and response.json()["timezone"] == "Europe/London"


# ---- follow-ups -----------------------------------------------------------------------------------------------------


def test_an_exempted_write_still_works_with_a_token(a, b, admin_token):
    """TOKEN_MAY_USE: removing a member takes authority away, so an offboarding script's token may do it."""
    viewer = a["users"]["viewer"]
    Membership.objects.create(user=viewer, organization=b["org"], role="viewer")  # else it's a deactivation
    assert admin_token.send("DELETE", f"/api/v1/users/{viewer.id}").status_code == 204
    assert not Membership.objects.filter(user=viewer, organization=a["org"]).exists()


def test_a_wrong_password_starting_an_impersonation_is_audited(a, api, clock):
    from core.models import PlatformAuditEvent
    from tests.test_platform import make_staff, start

    staff = make_staff(org=a["org"], role="viewer")
    client = api(staff)
    response = start(client, a["org"], a["users"]["editor"], password="not-my-password")
    assert response.status_code == 403
    event = PlatformAuditEvent.objects.get(action="staff.password_check_failed")
    assert event.actor_id == staff.id and event.details["purpose"] == "impersonation"
    mine = AuditEvent.objects.get(organization=a["org"], action="user.password_check_failed")
    assert mine.changes["purpose"] == "impersonation" and "not-my-password" not in json.dumps(mine.changes)
    assert start(client, a["org"], a["users"]["editor"]).status_code == 201  # the right password still works
