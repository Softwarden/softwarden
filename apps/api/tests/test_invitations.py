"""Invitations, pending access and unlinking one identity (catalog/invitations.py, catalog/access_viewsets.py, core/oidc.request_access)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from core import oidc_discovery, oidc_simulator
from core.models import AuditEvent, Invitation, Membership, PendingAccess, Role, UserIdentity
from tests.sso_helpers import activate, sign_in

PASSWORD = "a-much-longer-passphrase"  # noqa: S105  (a synthetic test password)
OKTA_ISSUER = f"https://{oidc_simulator.OKTA_DOMAIN}/oauth2/default"


@pytest.fixture(autouse=True)
def fresh_throttles():
    """The invitation routes share the sign-in throttle, which counts in the process cache."""
    cache.clear()
    yield
    cache.clear()


def invite(api, admin, email, role="viewer", **extra):
    response = api(admin).post("invitations", {"email": email, "role": role, **extra})
    assert response.status_code == 201, response.json_body
    return response.json_body


def token_of(body) -> str:
    return body["link"].rsplit("/invite#", 1)[1]


def emailed(body) -> str:
    """The token of the link emailed to the invitee: it proves the mailbox, so it can create an account where more
    than one organization shares the installation (catalog/invitations.mailbox_token)."""
    from catalog import invitations

    return invitations.mailbox_token(token_of(body))


def page(client: Client, token: str, method="get", data=None, **headers):
    """The invitation page's two calls: "get" previews, "post" accepts. The token is always in the body."""
    path = "/api/v1/auth/invitations/" + ("preview" if method == "get" else "accept")
    body = {"token": token, **(data or {})}
    return client.post(path, json.dumps(body), content_type="application/json", **headers)


def accept_new(token: str, name="New Person", password=PASSWORD) -> Client:
    client = Client()
    response = page(client, token, "post", {"name": name, "password": password})
    assert response.status_code == 200, response.json()
    return client


def signed_in(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def problem_type(response) -> str:
    assert response["Content-Type"] == "application/problem+json", response.content
    return response.json()["type"]


# ---- invitations: create ------------------------------------------------------------------------------------------


def test_inviting_returns_a_copy_once_link_and_adds_nobody(a, api, settings):
    settings.SOFTWARDEN_BASE_URL = "https://softwarden.example.test"
    body = invite(api, a["users"]["admin"], "New.Person@Orga.test", "editor", name="New Person")
    assert body["link"].startswith("https://softwarden.example.test/invite#")
    assert set(body) == {
        "id", "email", "name", "role", "status", "invited_by", "created_at", "expires_at", "accepted_at",
        "accepted_by", "revoked_at", "link", "emailed",
    }  # fmt: skip
    assert body["email"] == "new.person@orga.test" and body["status"] == "pending" and body["role"] == "editor"
    assert body["invited_by"]["id"] == a["users"]["admin"].id
    token = token_of(body)
    row = Invitation.objects.get()
    assert row.token_hash != token and token not in json.dumps(api(a["users"]["admin"]).get("invitations").json_body)
    assert not get_user_model().objects.filter(username="new.person@orga.test").exists()
    event = AuditEvent.objects.get(action="user.invited")
    assert event.changes == {"role": "editor", "email": "n***@orga.test", "replaced": False}
    assert token not in json.dumps(list(AuditEvent.objects.values_list("changes", flat=True)))
    detail = api(a["users"]["admin"]).get(f"invitations/{row.id}")
    assert "link" not in detail.json_body and detail["ETag"]


def test_post_users_is_an_alias_that_invites(a, api):
    response = api(a["users"]["admin"]).post("users", {"email": "alias@orga.test", "role": "viewer"})
    assert response.status_code == 201 and "/invite#" in response.json_body["link"]
    assert "temporary_password" not in response.json_body
    assert Invitation.objects.filter(email="alias@orga.test").exists()


def test_a_new_invitation_replaces_the_live_one_for_the_same_email(a, api):
    first = invite(api, a["users"]["admin"], "twice@orga.test")
    second = invite(api, a["users"]["admin"], "twice@orga.test", "editor")
    assert page(Client(), token_of(first)).status_code == 404
    assert page(Client(), token_of(second)).json()["role"] == "editor"
    assert Invitation.objects.filter(email="twice@orga.test", revoked_at__isnull=True).count() == 1
    assert AuditEvent.objects.filter(action="user.invited").latest("created_at").changes["replaced"] is True


def test_invite_validation(a, b, api):
    admin = api(a["users"]["admin"])
    bad = admin.post("invitations", {"email": "not-an-email", "role": "boss"})
    assert bad.status_code == 400 and set(bad.json_body["errors"]) == {"email", "role"}
    member = admin.post("invitations", {"email": a["users"]["viewer"].email, "role": "viewer"})
    assert member.status_code == 400 and "already a member" in member.json_body["errors"]["email"][0]


def test_invitation_list_filters_and_statuses(a, api):
    admin = api(a["users"]["admin"])
    pending = invite(api, a["users"]["admin"], "p@orga.test")
    revoked = invite(api, a["users"]["admin"], "r@orga.test")
    expired = invite(api, a["users"]["admin"], "e@orga.test")
    admin.post(f"invitations/{revoked['id']}/revoke")
    Invitation.objects.filter(pk=expired["id"]).update(expires_at=timezone.now() - timedelta(minutes=1))
    rows = {r["email"]: r["status"] for r in admin.get("invitations").json_body["results"]}
    assert rows == {"p@orga.test": "pending", "r@orga.test": "revoked", "e@orga.test": "expired"}
    assert [r["id"] for r in admin.get("invitations?status=pending").json_body["results"]] == [pending["id"]]
    assert [r["email"] for r in admin.get("invitations?q=E@ORGA").json_body["results"]] == ["e@orga.test"]
    assert admin.get("invitations?status=lost").status_code == 400


# ---- invitations: accept ------------------------------------------------------------------------------------------


def test_accepting_as_a_new_account_creates_it_and_signs_in(a, api):
    token = token_of(invite(api, a["users"]["admin"], "newbie@orga.test", "editor", name="Suggested Name"))
    anonymous = Client()
    preview = page(anonymous, token).json()
    assert preview == {
        "organization": {"name": "orga", "slug": a["org"].slug},
        "email": "n***@orga.test",
        "role": "editor",
        "name": "Suggested Name",
        "expires_at": preview["expires_at"],
        "account_exists": False,
        "can_create_account": True,  # one organization: the link an Admin was shown creates accounts
        "organization_account": None,  # no organization made an account for this address
        "session": "none",
        "sso": None,
    }
    weak = page(anonymous, token, "post", {"name": "", "password": "short"})
    assert weak.status_code == 400 and set(weak.json()["errors"]) == {"name", "password"}
    accepted = page(anonymous, token, "post", {"name": "Nia Newbie", "password": PASSWORD})
    assert accepted.status_code == 200
    me = accepted.json()
    assert me["organization"]["name"] == "orga" and me["role"] == "editor" and me["must_change_password"] is False
    user = get_user_model().objects.get(username="newbie@orga.test")
    assert user.check_password(PASSWORD) and user.get_full_name() == "Nia Newbie"
    assert Membership.objects.get(user=user, organization=a["org"]).role == Role.EDITOR
    assert anonymous.get("/api/v1/applications").status_code == 200  # signed in, not confined
    row = Invitation.objects.get()
    assert row.accepted_by == user and row.accepted_at is not None
    event = AuditEvent.objects.get(action="invitation.accepted")
    assert event.changes == {"user": user.id, "role": "editor", "new_account": True}
    assert PASSWORD not in json.dumps(list(AuditEvent.objects.values_list("changes", flat=True)))
    # Used: the link is gone, the same way as an unknown one.
    used = page(Client(), token)
    assert used.status_code == 404 and problem_type(used) == "/problems/invitation-unavailable"
    assert page(Client(), token, "post", {"name": "Again", "password": PASSWORD}).status_code == 404


def test_accepting_as_an_existing_account_needs_its_own_session(a, b, api):
    from catalog import invitations

    person = b["users"]["editor"]
    token = token_of(invite(api, a["users"]["admin"], person.email))
    assert not Membership.objects.filter(user=person, organization=a["org"]).exists()  # nothing at invite time
    invited = AuditEvent.objects.get(action="user.invited")
    assert invited.changes == {"role": "viewer", "email": "e***@orgb.test", "replaced": False}  # no account facts

    anonymous = Client()
    # The link the Admin was shown says nothing about the address; the emailed one says it has an account.
    assert page(anonymous, token).json()["account_exists"] is None
    refused = page(anonymous, token, "post")
    assert refused.status_code == 403 and problem_type(refused) == "/problems/invitation-emailed-link-required"
    mailbox = invitations.mailbox_token(token)
    assert page(anonymous, mailbox).json()["account_exists"] is True
    refused = page(anonymous, mailbox, "post")
    assert refused.status_code == 403 and problem_type(refused) == "/problems/not-signed-in"

    other = signed_in(b["users"]["viewer"])
    assert page(other, token).json()["session"] == "other_account"
    wrong = page(other, token, "post")
    assert wrong.status_code == 409 and problem_type(wrong) == "/problems/invitation-wrong-account"

    client = signed_in(person)
    assert page(client, token).json()["session"] == "invitee"
    done = page(client, token, "post")
    assert done.status_code == 200
    assert done.json()["organization"]["name"] == "orga"  # the session moves to the new organization
    assert Membership.objects.get(user=person, organization=a["org"]).role == Role.VIEWER
    assert Membership.objects.get(user=person, organization=b["org"]).role == Role.EDITOR  # untouched
    person.refresh_from_db()
    assert person.check_password("correct-horse-battery")
    event = AuditEvent.objects.get(action="invitation.accepted")
    assert event.organization_id == a["org"].id and event.changes["new_account"] is False
    assert page(client, token, "post").status_code == 404


def test_a_signed_in_account_cant_create_another_through_a_link(a, b, api):
    token = token_of(invite(api, a["users"]["admin"], "someone.new@orga.test"))
    response = page(signed_in(b["users"]["viewer"]), token, "post", {"name": "X", "password": PASSWORD})
    assert response.status_code == 409 and problem_type(response) == "/problems/invitation-wrong-account"
    assert not get_user_model().objects.filter(username="someone.new@orga.test").exists()


@pytest.mark.parametrize("how", ["expired", "revoked", "unknown", "deactivated"])
def test_a_link_that_cant_be_used_gives_one_answer(a, b, api, how):
    person = b["users"]["viewer"]
    body = invite(api, a["users"]["admin"], person.email)
    token = token_of(body)
    if how == "expired":
        Invitation.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    elif how == "revoked":
        assert api(a["users"]["admin"]).post(f"invitations/{body['id']}/revoke").status_code == 200
    elif how == "unknown":
        token = "x" * 43
    else:  # deactivated after the invitation was made: its pending invitations stop working
        assert api(b["users"]["admin"]).patch(f"users/{person.id}", {"active": False}).status_code == 200
        token = emailed(body)  # the link the Admin was shown says nothing about the address
    client = signed_in(person) if how != "deactivated" else Client()
    for response in (page(client, token), page(client, token, "post")):
        assert response.status_code == 404 and problem_type(response) == "/problems/invitation-unavailable"
        assert response.json()["detail"].startswith("This invitation link isn't valid any more.")
    assert not Membership.objects.filter(user=person, organization=a["org"]).exists()


def test_resend_rotates_the_token_and_revoke_ends_it(a, api):
    admin = api(a["users"]["admin"])
    body = invite(api, a["users"]["admin"], "resend@orga.test")
    Invitation.objects.update(expires_at=timezone.now() - timedelta(days=1))  # expired: resend renews it
    resent = admin.post(f"invitations/{body['id']}/resend")
    assert resent.status_code == 200 and resent.json_body["status"] == "pending"
    assert resent.json_body["link"] != body["link"]
    assert page(Client(), token_of(body)).status_code == 404
    assert page(Client(), token_of(resent.json_body)).status_code == 200
    assert admin.post(f"invitations/{body['id']}/revoke").json_body["status"] == "revoked"
    assert page(Client(), token_of(resent.json_body)).status_code == 404
    assert admin.post(f"invitations/{body['id']}/revoke").status_code == 409
    assert admin.post(f"invitations/{body['id']}/resend").status_code == 409
    actions = list(AuditEvent.objects.filter(organization=a["org"]).values_list("action", flat=True))
    assert {"user.invited", "invitation.resent", "invitation.revoked"} <= set(actions)


def test_invitations_are_admin_only_and_stay_in_their_organization(a, b, api):
    body = invite(api, b["users"]["admin"], "b-only@orgb.test")
    assert api(a["users"]["editor"]).get("invitations").status_code == 403
    other = api(a["users"]["admin"])
    assert other.get("invitations").json_body["results"] == []
    for method, path in (
        ("get", f"invitations/{body['id']}"),
        ("post", f"invitations/{body['id']}/revoke"),
        ("post", f"invitations/{body['id']}/resend"),
    ):
        assert getattr(other, method)(path).status_code == 404, path
    assert Invitation.objects.get().revoked_at is None


def test_an_api_token_cant_accept(a, b, api):
    from api import tokens
    from core.dates import org_today

    person = b["users"]["editor"]
    token = token_of(invite(api, a["users"]["admin"], person.email))
    owner = Membership.objects.get(user=person, organization=b["org"])
    _, secret = tokens.create_token(
        b["org"], b["users"]["admin"], owner, "Script", read_only=False, expires_on=org_today(b["org"]) + timedelta(5)
    )
    client = Client(HTTP_AUTHORIZATION=f"Bearer {secret}")
    assert page(client, token, "post").status_code == 403
    assert not Membership.objects.filter(user=person, organization=a["org"]).exists()


def test_accepting_through_the_inviting_organizations_sso_needs_an_already_pinned_identity(a, b, api, simulator):
    """An existing account accepts after signing in with the inviting organization's single sign-on only when that
    identity already signs in to the invited account; an unpinned identity with the same email gets nothing."""
    person = b["users"]["editor"]
    activate(a["org"], "okta")
    token = token_of(invite(api, a["users"]["admin"], person.email, "editor"))
    stranger = Client()
    preview = page(stranger, token).json()
    assert preview["sso"]["provider"] == "okta" and preview["sso"]["start_url"].endswith("next=%2Finvite")
    assert token not in json.dumps(preview)
    done = sign_in(stranger, person.email, next_path="/invite", query={"org": a["org"].slug})
    assert done["Location"].startswith("/login?error=no_account")
    assert not UserIdentity.objects.filter(user=person).exists()

    subject = oidc_simulator.subject("okta", person.email)
    UserIdentity.objects.create(user=person, issuer=OKTA_ISSUER, subject=subject)  # pinned earlier, elsewhere
    # Without the preview in this browser, the callback has no invitation to sign in for.
    assert sign_in(Client(), person.email, next_path="/invite", query={"org": a["org"].slug})["Location"].startswith(
        "/login?error=no_account"
    )
    client = Client()
    page(client, token)
    done = sign_in(client, person.email, next_path="/invite", query={"org": a["org"].slug})
    assert done["Location"] == "/invite"
    login_event = AuditEvent.objects.filter(action="user.login", organization=a["org"]).latest("created_at")
    assert login_event.actor is None and login_event.changes == {
        "method": "okta", "invitation": True, "email_domain": "orgb.test"
    }  # fmt: skip
    assert client.get("/api/v1/applications").status_code == 403  # confined to orga, no membership there yet
    assert page(client, token).json()["session"] == "invitee"
    accepted = page(client, token, "post")
    assert accepted.status_code == 200 and accepted.json()["organization"]["name"] == "orga"
    assert [m["name"] for m in accepted.json()["organizations"]] == ["orga"]  # still confined to orga
    assert client.get(f"/api/v1/applications/{b['app'].id}").status_code == 404


def test_invitation_routes_have_their_own_throttle(a, monkeypatch):
    """`invitation`, apart from sign-in's `login`, counted in the shared counters."""
    from rest_framework.throttling import ScopedRateThrottle

    monkeypatch.setattr(
        ScopedRateThrottle, "THROTTLE_RATES", {**ScopedRateThrottle.THROTTLE_RATES, "invitation": "2/min"}
    )
    client = Client()
    assert [page(client, "y" * 43).status_code for _ in range(3)] == [404, 404, 429]


# ---- pending access -----------------------------------------------------------------------------------------------


def auto_provision(api, f, on=True):
    response = api(f["users"]["admin"]).patch("organization", {"sso_auto_provision": on})
    assert response.status_code == 200 and response.json_body["sso_auto_provision"] is on


def test_auto_provision_is_an_audited_org_setting_off_by_default(a, api):
    admin = api(a["users"]["admin"])
    assert admin.get("organization").json_body["sso_auto_provision"] is False
    assert admin.patch("organization", {"sso_auto_provision": "yes"}).status_code == 400
    assert api(a["users"]["editor"]).patch("organization", {"sso_auto_provision": True}).status_code == 403
    auto_provision(api, a)
    event = AuditEvent.objects.get(action="organization.updated")
    assert event.changes["sso_auto_provision"] == [False, True]
    assert (event.postchange or {})["settings"]["sso_auto_provision"] is True


def test_unknown_identity_is_refused_while_auto_provision_is_off(a, simulator):
    activate(a["org"], "okta")
    done = sign_in(Client(), "stranger@orga.test", query={"org": a["org"].slug})
    assert done["Location"].startswith("/login?error=no_account")
    assert not PendingAccess.objects.exists()


def test_unknown_identity_waits_for_an_admin_when_auto_provision_is_on(a, api, simulator):
    activate(a["org"], "okta")
    auto_provision(api, a)
    client = Client()
    done = sign_in(client, "Stranger@Orga.test", query={"org": a["org"].slug})
    assert done["Location"] == f"/login?error=access_pending&org={a['org'].slug}"
    assert client.get("/api/v1/auth/me").status_code == 403  # nobody signed in
    assert not get_user_model().objects.filter(username="stranger@orga.test").exists()
    row = PendingAccess.objects.get()
    assert (row.email, row.issuer, row.organization_id) == ("stranger@orga.test", OKTA_ISSUER, a["org"].id)
    first_seen = row.first_seen
    sign_in(Client(), "stranger@orga.test", query={"org": a["org"].slug})  # again: the same row, seen later
    row.refresh_from_db()
    assert PendingAccess.objects.count() == 1 and row.first_seen == first_seen and row.last_seen > first_seen
    listed = api(a["users"]["admin"]).get("pending-access").json_body["results"]
    assert listed == [
        {
            "id": str(row.id),
            "email": "stranger@orga.test",
            "name": "stranger@orga.test",  # the latest sign-in's name claim
            "issuer": OKTA_ISSUER,
            "provider": "Okta",
            "first_seen": listed[0]["first_seen"],
            "last_seen": listed[0]["last_seen"],
        }
    ]
    assert row.subject not in json.dumps(listed)
    assert AuditEvent.objects.filter(action="pending_access.created", organization=a["org"]).count() == 1


ENTRA_ISSUER = f"https://login.microsoftonline.com/{oidc_simulator.TENANT_ID}/v2.0"
OTHER_TENANT = "0000aaaa-0000-4000-8000-00000000beef"


@pytest.mark.parametrize(
    "overrides",
    [
        {"email": None},  # no email claim: preferred_username (mutable, Learn) never links
        {"idp": f"https://sts.windows.net/{OTHER_TENANT}/"},  # a guest: another tenant authenticated them
        {"idp": "live.com"},  # a personal Microsoft account invited as a guest
        {"acct": 1},  # the optional `acct` claim says guest
    ],
    ids=["preferred_username-only", "guest-idp", "guest-msa", "guest-acct"],
)
def test_entra_first_sign_in_links_a_member_only_by_email_of_a_member_of_the_tenant(a, api, simulator, overrides):
    """A first Entra sign-in links an existing member only through the `email`
    claim of a non-guest of the pinned tenant; anything else goes to pending access, where an Admin's approval links
    it. Learn, ID token claims reference: `preferred_username` and `email` are mutable, `idp` differs from `iss` for
    guests; optional claims reference: `acct` 1 is a guest."""
    from tests.sso_helpers import sign_in_with

    editor = a["users"]["editor"]
    activate(a["org"], "entra")
    auto_provision(api, a)
    done = sign_in_with(Client(), editor.username, overrides, query={"org": a["org"].slug})
    assert done["Location"] == f"/login?error=access_pending&org={a['org'].slug}"
    assert not UserIdentity.objects.filter(user=editor).exists()
    row = PendingAccess.objects.get()
    assert (row.email, row.issuer) == (editor.username, ENTRA_ISSUER)  # what the Admin sees and approves
    approved = api(a["users"]["admin"]).post(f"pending-access/{row.id}/approve", {"role": "viewer"})
    assert approved.status_code == 200, approved.json_body
    assert UserIdentity.objects.get(user=editor).subject == oidc_simulator.object_id(editor.username)
    assert Membership.objects.get(user=editor).role == "editor"  # a member's role is left as it is
    assert "/login" not in sign_in_with(Client(), editor.username, overrides, query={"org": a["org"].slug})["Location"]


def test_entra_member_of_the_tenant_links_by_email_and_returns_by_object_id(a, simulator):
    """A member's `email` (idp equal to iss, or acct 0) links on the first sign-in; later sign-ins match the
    pinned tid and oid only, whatever the email claim says then."""
    from tests.sso_helpers import sign_in_with

    editor = a["users"]["editor"]
    activate(a["org"], "entra")
    member = {"idp": ENTRA_ISSUER, "acct": 0}
    assert "/login" not in sign_in_with(Client(), editor.username, member, query={"org": a["org"].slug})["Location"]
    assert UserIdentity.objects.get(user=editor).issuer == ENTRA_ISSUER
    renamed = {"email": "someone.else@orga.test", "preferred_username": "x@orga.test"}
    assert "/login" not in sign_in_with(Client(), editor.username, renamed, query={"org": a["org"].slug})["Location"]


def test_pending_access_is_capped_per_organization(a, api, simulator, monkeypatch):
    from core import oidc

    monkeypatch.setattr(oidc, "PENDING_ACCESS_MAX", 2)
    activate(a["org"], "okta")
    auto_provision(api, a)
    for email in ("one@orga.test", "two@orga.test"):
        assert "access_pending&" in sign_in(Client(), email, query={"org": a["org"].slug})["Location"]
    done = sign_in(Client(), "three@orga.test", query={"org": a["org"].slug})
    assert done["Location"] == f"/login?error=access_pending_full&org={a['org'].slug}"
    assert PendingAccess.objects.count() == 2
    # Someone already waiting is still recognised.
    assert "access_pending&" in sign_in(Client(), "one@orga.test", query={"org": a["org"].slug})["Location"]


def pending(f, email, issuer=OKTA_ISSUER, subject=None, name="Pat Pending"):
    now = timezone.now()
    return PendingAccess.objects.create(
        organization=f["org"],
        issuer=issuer,
        subject=subject or oidc_simulator.subject("okta", email),
        email=email,
        name=name,
        first_seen=now,
        last_seen=now,
    )


def test_approving_creates_the_account_and_the_identity_signs_in_confined(a, b, api, simulator):
    activate(a["org"], "okta")
    row = pending(a, "pat@orga.test")
    response = api(a["users"]["admin"]).post(f"pending-access/{row.id}/approve", {"role": "editor"})
    assert response.status_code == 200, response.json_body
    user = get_user_model().objects.get(username="pat@orga.test")
    assert response.json_body["id"] == user.id and response.json_body["role"] == "editor"
    assert response.json_body["identities"][0]["provider"] == "Okta"
    assert not user.has_usable_password() and user.get_full_name() == "Pat Pending"
    assert UserIdentity.objects.filter(user=user, issuer=OKTA_ISSUER, subject=row.subject).exists()
    assert not PendingAccess.objects.exists()
    event = AuditEvent.objects.get(action="pending_access.approved")
    assert event.changes == {"user": user.id, "role": "editor", "provider": "Okta", "new_account": True}
    # The pinned identity signs in, confined to orga, even after the account later joins orgb.
    Membership.objects.create(user=user, organization=b["org"], role="admin")
    client = Client()
    assert sign_in(client, "pat@orga.test", query={"org": a["org"].slug})["Location"] == "/renewals"
    me = client.get("/api/v1/auth/me").json()
    assert me["role"] == "editor" and [m["name"] for m in me["organizations"]] == ["orga"]
    assert client.get(f"/api/v1/applications/{b['app'].id}").status_code == 404


def test_approving_an_active_account_with_no_memberships_asks_for_an_invitation(a, api):
    """An active account that belongs to no organization has a password (or another way in) of its own; this
    organization's provider never takes it over. It is invited, and accepts while signed in as itself."""
    loner = get_user_model().objects.create_user(username="loner@orga.test", email="loner@orga.test", password="x")
    row = pending(a, "loner@orga.test")
    response = api(a["users"]["admin"]).post(f"pending-access/{row.id}/approve", {"role": "viewer"})
    assert response.status_code == 409 and problem_type(response) == "/problems/invitation-required"
    assert not Membership.objects.filter(user=loner).exists()
    assert not UserIdentity.objects.filter(user=loner).exists() and PendingAccess.objects.filter(pk=row.pk).exists()
    assert not AuditEvent.objects.filter(action="pending_access.approved").exists()


def test_approving_an_account_in_another_organization_asks_for_an_invitation(a, b, api):
    row = pending(a, b["users"]["editor"].email)
    response = api(a["users"]["admin"]).post(f"pending-access/{row.id}/approve", {"role": "viewer"})
    assert response.status_code == 409 and problem_type(response) == "/problems/invitation-required"
    assert not Membership.objects.filter(user=b["users"]["editor"], organization=a["org"]).exists()
    assert not UserIdentity.objects.exists() and PendingAccess.objects.filter(pk=row.pk).exists()
    assert not AuditEvent.objects.filter(action="pending_access.approved").exists()


def test_approving_a_member_pins_the_identity_and_keeps_the_role(a, api):
    member = a["users"]["viewer"]
    row = pending(a, member.email)
    response = api(a["users"]["admin"]).post(f"pending-access/{row.id}/approve", {"role": "admin"})
    assert response.status_code == 200 and response.json_body["role"] == "viewer"
    assert UserIdentity.objects.filter(user=member, subject=row.subject).exists()
    assert not PendingAccess.objects.exists()


def test_approve_validation(a, api):
    row = pending(a, "who@orga.test")
    admin = api(a["users"]["admin"])
    assert admin.post(f"pending-access/{row.id}/approve", {"role": "owner"}).status_code == 400
    nameless = pending(a, "", subject="no-email")
    assert admin.post(f"pending-access/{nameless.id}/approve", {"role": "viewer"}).status_code == 409
    assert PendingAccess.objects.count() == 2


def test_declining_deletes_the_request(a, api):
    row = pending(a, "nope@orga.test")
    assert api(a["users"]["admin"]).delete(f"pending-access/{row.id}").status_code == 204
    assert not PendingAccess.objects.exists() and not get_user_model().objects.filter(username="nope@orga.test")
    event = AuditEvent.objects.get(action="pending_access.declined")
    assert event.changes == {"email": "n***@orga.test", "provider": "Okta"}


def test_pending_access_is_admin_only_and_stays_in_its_organization(a, b, api):
    row = pending(b, "b-pending@orgb.test")
    assert api(a["users"]["editor"]).get("pending-access").status_code == 403
    other = api(a["users"]["admin"])
    assert other.get("pending-access").json_body["results"] == []
    assert other.get(f"pending-access/{row.id}").status_code == 404
    assert other.post(f"pending-access/{row.id}/approve", {"role": "admin"}).status_code == 404
    assert other.delete(f"pending-access/{row.id}").status_code == 404
    assert PendingAccess.objects.filter(pk=row.pk).exists()
    assert not get_user_model().objects.filter(username="b-pending@orgb.test").exists()


def test_pending_access_filters(a, api):
    pending(a, "okta@orga.test")
    pending(a, "entra@orga.test", issuer=oidc_discovery.entra_issuer(oidc_simulator.TENANT_ID), subject="e")
    pending(a, "other@orga.test", issuer="https://idp.example.test", subject="o")
    pending(a, "saml@orga.test", issuer=f"{oidc_discovery.SAML_PREFIX}https://idp.example.test/saml", subject="s")
    admin = api(a["users"]["admin"])

    def emails(query):
        return sorted(r["email"] for r in admin.get(f"pending-access?{query}").json_body["results"])

    assert emails("provider=okta") == ["okta@orga.test"]
    assert emails("provider=entra") == ["entra@orga.test"]
    assert emails("provider=other") == ["other@orga.test"]  # SAML has its own value
    assert emails("provider=saml") == ["saml@orga.test"]
    assert emails("q=ENTRA") == ["entra@orga.test"]
    assert admin.get("pending-access?provider=github").status_code == 400


# ---- identities ---------------------------------------------------------------------------------------------------


def test_users_list_shows_identities_without_subjects_and_one_can_be_unlinked(a, b, api):
    viewer = a["users"]["viewer"]
    okta = UserIdentity.objects.create(user=viewer, issuer=OKTA_ISSUER, subject="secret-subject-1")
    google = UserIdentity.objects.create(user=viewer, issuer=oidc_discovery.GOOGLE_ISSUER, subject="secret-subject-2")
    admin = api(a["users"]["admin"])
    rows = {u["id"]: u for u in admin.get("users").json_body}
    assert {i["provider"] for i in rows[viewer.id]["identities"]} == {"Okta", "Google"}
    assert set(rows[viewer.id]["identities"][0]) == {"id", "provider", "issuer", "last_login_at"}
    assert rows[viewer.id]["entra_linked"] is True and rows[a["users"]["editor"].id]["identities"] == []
    assert "secret-subject" not in json.dumps(admin.get("users").json_body)

    assert api(a["users"]["editor"]).delete(f"users/{viewer.id}/identities/{okta.id}").status_code == 403
    assert admin.delete(f"users/{viewer.id}/identities/{okta.id}").status_code == 204
    assert list(UserIdentity.objects.filter(user=viewer)) == [google]
    event = AuditEvent.objects.get(action="user.identity_unlinked")
    assert event.changes == {"user": viewer.id, "method": "sso", "issuers": [OKTA_ISSUER]}
    # Another member's identity, a missing one, and another organization's member are 404.
    assert admin.delete(f"users/{a['users']['editor'].id}/identities/{google.id}").status_code == 404
    assert admin.delete(f"users/{viewer.id}/identities/{okta.id}").status_code == 404
    other = UserIdentity.objects.create(user=b["users"]["viewer"], issuer=OKTA_ISSUER, subject="b")
    assert admin.delete(f"users/{b['users']['viewer'].id}/identities/{other.id}").status_code == 404
    assert UserIdentity.objects.filter(pk=other.pk).exists()


def test_unlinking_one_identity_of_a_shared_account_is_refused(a, b, api):
    shared = b["users"]["viewer"]
    Membership.objects.create(user=shared, organization=a["org"], role="viewer")
    identity = UserIdentity.objects.create(user=shared, issuer=OKTA_ISSUER, subject="s")
    admin = api(a["users"]["admin"])
    assert (
        admin.get("users").json_body
        and {u["id"]: u for u in admin.get("users").json_body}[shared.id]["identities"] is None
    )
    assert admin.delete(f"users/{shared.id}/identities/{identity.id}").status_code == 409
    assert UserIdentity.objects.filter(pk=identity.pk).exists()


@pytest.mark.parametrize(
    "issuer,label",
    [
        ("https://login.microsoftonline.com/0000/v2.0", "Entra"),
        ("https://accounts.google.com", "Google"),
        ("accounts.google.com", "Google"),
        ("https://acme.okta.com/oauth2/default", "Okta"),
        ("https://acme.oktapreview.com", "Okta"),
        ("https://sso.example.test/realms/it", "sso.example.test"),
    ],
)
def test_provider_label(issuer, label):
    assert oidc_discovery.provider_label(issuer) == label


# ---- the migration -----------------------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_the_migration_runs_forward_over_existing_rows():
    """core 0013 adds the two tables to a database that already has organizations and users."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before = [("core", "0012_sso_settings")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    org = old.get_model("core", "Organization").objects.create(name="Before", slug="before-0013")
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        from core.models import Organization

        moved = Organization.objects.get(pk=org.pk)
        assert not Invitation.objects.filter(organization=moved).exists()
        assert not PendingAccess.objects.filter(organization=moved).exists()
    finally:
        from core.models import Organization
        from notify.models import NotificationRule

        # notify 0011 seeds the vendor risk built-in rule for every organization it finds, this one too.
        NotificationRule.objects.filter(organization_id=org.pk).delete()
        Organization.objects.filter(pk=org.pk).delete()


# ---- review follow-ups -----------------------------------------------------------------------------------------------


def test_the_token_never_reaches_a_logged_path(a, api, caplog):
    """Paths are logged (the app's request line on every request, softwarden/logs.py), so the token
    travels in the link's fragment and request bodies only."""
    import logging

    from django.urls import Resolver404, resolve

    body = invite(api, a["users"]["admin"], "logged@orga.test")
    token = token_of(body)
    assert "#" in body["link"] and f"/{token}" not in body["link"]
    with caplog.at_level(logging.INFO):
        assert page(Client(), "z" * 43, "post").status_code == 404  # a 4xx: django.request logs its path
        assert page(Client(), token).status_code == 200
        unavailable = page(Client(), token, "post", {"name": "x", "password": "short"})
    assert unavailable.status_code == 400
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "/api/v1/auth/invitations/accept" in logged and token not in logged
    # Gunicorn writes no access log; the app's own request line (softwarden.request, one per request above,
    # path only) is part of `logged`. The SPA page is requested as /invite: browsers never send the fragment.
    from pathlib import Path

    assert sum(r.name == "softwarden.request" for r in caplog.records) >= 3
    conf: dict = {}
    exec((Path(__file__).resolve().parent.parent / "gunicorn.conf.py").read_text(encoding="utf-8"), conf)  # noqa: S102
    assert conf["accesslog"] is None and "access_log_format" not in conf
    with pytest.raises(Resolver404):
        resolve(f"/api/v1/auth/invitations/{token}")


def test_preview_and_accept_need_the_csrf_token(a, api):
    token = token_of(invite(api, a["users"]["admin"], "csrf@orga.test"))
    client = Client(enforce_csrf_checks=True)
    for method in ("get", "post"):
        refused = page(client, token, method, {"name": "C S", "password": PASSWORD})
        assert refused.status_code == 403 and problem_type(refused) == "/problems/csrf-failed", method
    assert not get_user_model().objects.filter(username="csrf@orga.test").exists()
    client.get("/api/v1/auth/csrf")
    csrf = client.cookies["csrftoken"].value
    assert page(client, token, HTTP_X_CSRFTOKEN=csrf).status_code == 200
    accepted = page(client, token, "post", {"name": "C S", "password": PASSWORD}, HTTP_X_CSRFTOKEN=csrf)
    assert accepted.status_code == 200
    assert get_user_model().objects.filter(username="csrf@orga.test").exists()


def test_a_session_confined_to_another_organization_cant_accept(a, b, api, simulator):
    person = b["users"]["editor"]
    activate(b["org"], "okta")
    client = Client()
    assert sign_in(client, person.email, query={"org": b["org"].slug})["Location"] == "/renewals"  # confined to orgb
    token = token_of(invite(api, a["users"]["admin"], person.email))
    assert page(client, token).json()["session"] == "other_sign_in"  # the page says why before accepting does
    refused = page(client, token, "post")
    assert refused.status_code == 409 and problem_type(refused) == "/problems/invitation-other-sign-in"
    assert not Membership.objects.filter(user=person, organization=a["org"]).exists()
    assert Invitation.objects.get().accepted_at is None
    # A password sign-in in the same browser lifts the confinement and accepts.
    client.post("/api/v1/auth/login", {"email": person.email, "password": "correct-horse-battery"}, "application/json")
    assert page(client, token).json()["session"] == "invitee"
    assert page(client, token, "post").status_code == 200


def test_approving_a_member_who_is_also_elsewhere_is_refused(a, b, api):
    both = b["users"]["editor"]
    Membership.objects.create(user=both, organization=a["org"], role="viewer")
    row = pending(a, both.email)
    response = api(a["users"]["admin"]).post(f"pending-access/{row.id}/approve", {"role": "viewer"})
    assert response.status_code == 409 and problem_type(response) == "/problems/invitation-required"
    assert not UserIdentity.objects.filter(user=both).exists()
    assert PendingAccess.objects.filter(pk=row.pk).exists()


def test_decline_and_approve_of_a_gone_request_are_404(a, api):
    admin = api(a["users"]["admin"])
    row = pending(a, "gone@orga.test")
    assert admin.delete(f"pending-access/{row.id}").status_code == 204
    from catalog import invitations

    with pytest.raises(Exception) as declined:
        invitations.decline_pending(a["org"], a["users"]["admin"], row)
    assert getattr(declined.value, "status_code", None) == 404
    with pytest.raises(Exception) as approved:
        invitations.approve_pending(a["org"], a["users"]["admin"], row, {"role": "viewer"})
    assert getattr(approved.value, "status_code", None) == 404
    assert AuditEvent.objects.filter(action="pending_access.declined").count() == 1
    event = AuditEvent.objects.get(action="pending_access.declined")
    assert (event.target_type, event.target_id) == ("PendingAccess", row.id)


def test_approval_is_in_the_members_history(a, api):
    row = pending(a, "history@orga.test")
    api(a["users"]["admin"]).post(f"pending-access/{row.id}/approve", {"role": "viewer"})
    event = AuditEvent.objects.get(action="pending_access.approved")
    member = Membership.objects.get(user__username="history@orga.test")
    assert (event.target_type, event.target_id) == ("Membership", member.id)


def test_a_changed_email_on_a_pending_request_is_audited(a, api, simulator):
    activate(a["org"], "okta")
    auto_provision(api, a)
    sign_in(Client(), "first@orga.test", query={"org": a["org"].slug})
    row = PendingAccess.objects.get()
    from tests.sso_helpers import sign_in_with

    subject = row.subject
    done = sign_in_with(Client(), "first@orga.test", {"email": "second@orga.test"}, query={"org": a["org"].slug})
    assert "access_pending" in done["Location"]
    row.refresh_from_db()
    assert row.subject == subject and row.email == "second@orga.test"
    event = AuditEvent.objects.get(action="pending_access.updated")
    assert event.changes == {"email": ["f***@orga.test", "s***@orga.test"]} and event.actor is None


def test_pending_access_needs_the_sso_capability(a, api, monkeypatch):
    from core import capabilities

    real = capabilities.capabilities
    monkeypatch.setattr(capabilities, "capabilities", lambda org: real(org) - {capabilities.SSO_OIDC})
    admin = api(a["users"]["admin"])
    refused = admin.get("pending-access")
    assert refused.status_code == 403 and problem_type(refused) == "/problems/capability-required"
    assert admin.patch("organization", {"sso_auto_provision": True}).status_code == 403


def test_leaving_an_organization_unlinks_its_providers_identities(a, b, api, monkeypatch):
    """(f): removing someone from an organization unlinks their identities at issuers that no organization they still
    belong to configures, and no legacy setting; identities another of their organizations configures stay."""
    from tests.sso_helpers import activate as set_up

    person = b["users"]["editor"]
    Membership.objects.create(user=person, organization=a["org"], role="viewer")
    set_up(b["org"], "okta")  # orgb configures Okta
    okta = UserIdentity.objects.create(user=person, issuer=OKTA_ISSUER, subject="kept")
    orga_idp = UserIdentity.objects.create(user=person, issuer="https://idp.orga.test", subject="gone")
    entra = oidc_discovery.entra_issuer(oidc_simulator.TENANT_ID)
    legacy = UserIdentity.objects.create(user=person, issuer=entra, subject="legacy")
    monkeypatch.setenv("SOFTWARDEN_OIDC_TENANT", oidc_simulator.TENANT_ID)
    monkeypatch.setenv("SOFTWARDEN_OIDC_CLIENT_ID", "11111111-2222-4333-8444-555555555555")
    monkeypatch.setenv("SOFTWARDEN_OIDC_CLIENT_SECRET", "s")
    assert api(a["users"]["admin"]).delete(f"users/{person.id}").status_code == 204
    assert set(UserIdentity.objects.filter(user=person)) == {okta, legacy}
    assert not UserIdentity.objects.filter(pk=orga_idp.pk).exists()
    event = AuditEvent.objects.get(action="user.identity_unlinked")
    assert event.organization_id == a["org"].id
    assert event.changes == {
        "user": person.id, "method": "sso", "issuers": ["https://idp.orga.test"], "reason": "membership_removed"
    }  # fmt: skip


# ---- review follow-ups -----------------------------------------------------------------------------------------------


def test_another_account_confined_elsewhere_is_other_account_and_the_inviting_orgs_sso_is_invitee(a, b, api, simulator):
    """`other_sign_in` is only for the invited account: another account is `other_account` (accepting checks that
    first), and a session confined to the inviting organization itself is `invitee`."""
    activate(b["org"], "okta")
    someone_else = Client()
    sign_in(someone_else, b["users"]["viewer"].email, query={"org": b["org"].slug})
    token = token_of(invite(api, a["users"]["admin"], b["users"]["editor"].email))
    assert page(someone_else, token).json()["session"] == "other_account"
    refused = page(someone_else, token, "post")
    assert refused.status_code == 409 and problem_type(refused) == "/problems/invitation-wrong-account"
    # The invited account, in a session confined to the inviting organization (as core/oidc.invitee signs one in).
    loner = get_user_model().objects.create_user(username="loner@orgb.test", email="loner@orgb.test", password="x")
    own = token_of(invite(api, b["users"]["admin"], loner.email))
    client = signed_in(loner)
    session = client.session
    session["softwarden_sso_org"] = str(b["org"].id)
    session.save()
    assert page(client, own).json()["session"] == "invitee"


def test_a_resend_ends_the_old_previews_sso_path(a, b, api, simulator):
    """The session remembers the previewed invitation with a prefix of its token hash: after a resend, an SSO sign-in
    started from the old preview no longer signs in the invited account."""
    from catalog import invitations

    person = b["users"]["editor"]
    activate(a["org"], "okta")
    body = invite(api, a["users"]["admin"], person.email, "editor")
    UserIdentity.objects.create(user=person, issuer=OKTA_ISSUER, subject=oidc_simulator.subject("okta", person.email))
    client = Client()
    page(client, token_of(body))
    remembered = client.session[invitations.SESSION_INVITATION]
    row = Invitation.objects.get()
    assert remembered == {"id": str(row.id), "hash": row.token_hash[:16]}
    assert token_of(body) not in json.dumps(remembered)
    assert api(a["users"]["admin"]).post(f"invitations/{body['id']}/resend").status_code == 200
    done = sign_in(client, person.email, next_path="/invite", query={"org": a["org"].slug})
    assert done["Location"].startswith("/login?error=no_account")
    assert not Membership.objects.filter(user=person, organization=a["org"]).exists()
    assert invitations.live_by_id(remembered) is None
    assert invitations.live_by_id(invitations.remember(Invitation.objects.get())) is not None
    for junk in (str(row.id), {"id": "not-a-uuid", "hash": "0" * 16}, {"id": str(row.id)}, None):
        assert invitations.live_by_id(junk) is None


def test_a_preview_with_no_live_invitation_or_no_sso_button_clears_the_session_entry(a, b, api, simulator):
    from catalog import invitations

    person = b["users"]["editor"]
    activate(a["org"], "okta")
    body = invite(api, a["users"]["admin"], person.email)
    client = Client()
    page(client, token_of(body))
    assert invitations.SESSION_INVITATION in client.session
    assert api(a["users"]["admin"]).post(f"invitations/{body['id']}/resend").status_code == 200
    assert page(client, token_of(body)).status_code == 404  # the old link
    assert invitations.SESSION_INVITATION not in client.session
    # A preview of an invitation with no SSO button (a new account's) clears it too.
    resent = Invitation.objects.get()
    session = client.session
    session[invitations.SESSION_INVITATION] = invitations.remember(resent)
    session.save()
    fresh = emailed(invite(api, a["users"]["admin"], "brand.new@orga.test"))
    assert page(client, fresh).json()["sso"] is None
    assert invitations.SESSION_INVITATION not in client.session
    # So does an existing account's invitation from an organization with no single sign-on (the
    # "account exists, no SSO" branch of _sso_option).
    session = client.session
    session[invitations.SESSION_INVITATION] = invitations.remember(resent)
    session.save()
    existing = emailed(invite(api, b["users"]["admin"], a["users"]["viewer"].email))
    shown = page(client, existing).json()
    assert shown["account_exists"] is True and shown["sso"] is None
    assert invitations.SESSION_INVITATION not in client.session
