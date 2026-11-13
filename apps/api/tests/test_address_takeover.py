"""An account an organization made through single sign-on or SCIM doesn't hold its email address
(catalog/invitations.managed, holder_for, release_address).

Pending-access approval and SCIM create the global account for whatever address the organization's own identity
provider or SCIM client sends, with no proof that its owner controls the mailbox. Such an account has no password, so
it is the organization's, not the address owner's: a later signup or invitation for the address, after its own email
proof, takes the address over (the old account is retired in its organization, its identities unlinked), and no
Admin can give it a password."""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client

from catalog import invitations
from core.models import AuditEvent, MailboxProof, Membership, PlatformStaff, Role, UserIdentity
from tests.sso_helpers import activate, sign_in
from tests.test_invitations import PASSWORD, invite, page, pending, problem_type

VICTIM = "x@victim.example"


@pytest.fixture(autouse=True)
def fresh_throttles():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def signup_on(settings):
    settings.SOFTWARDEN_SIGNUP_ENABLED = True
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.SOFTWARDEN_BASE_URL = "https://app.softwarden.example.test"
    return settings


def post(client: Client, path: str, body: dict | None = None):
    response = client.post(f"/api/v1/{path}", json.dumps(body or {}), content_type="application/json")
    response.json_body = response.json() if response.content else None  # type: ignore[attr-defined]
    return response


def approved(f, api, email=VICTIM):
    """Org f approves a pending single sign-on identity for `email`: the account it makes."""
    row = pending(f, email)
    response = api(f["users"]["admin"]).post(f"pending-access/{row.id}/approve", {"role": "admin"})
    assert response.status_code == 200, response.json_body
    return get_user_model().objects.get(username=email)


def link_in(message, path: str) -> str:
    return next(p for p in message.body.split() if f"{path}#" in p).rsplit("#", 1)[1]


def signs_up(email=VICTIM) -> Client:
    response = post(Client(), "auth/signup", {"email": email, "name": "Real Owner", "organization_name": "Victim Co",
                                               "password": PASSWORD})  # fmt: skip
    assert response.status_code == 202
    message = mail.outbox[-1]
    assert message.to == [email] and "/signup/verify#" in message.body, message.subject  # a link, not the note
    client = Client()
    verified = post(client, "auth/signup/verify", {"token": link_in(message, "/signup/verify"), "password": PASSWORD})
    assert verified.status_code == 200, verified.json_body
    return client


# ---- the acceptance -----------------------------------------------------------------------------------------------


def test_a_signup_takes_over_an_address_an_organization_approved_and_its_admin_cant_reach_it(
    a, b, api, simulator, signup_on
):
    """Org B approves a pending identity for x@victim.example; a signup for that address then
    receives a confirmation link, confirms, and ends with an account org B's Admin can neither reset nor sign in as."""
    activate(b["org"], "okta")
    old = approved(b, api)
    assert not old.has_usable_password()
    b_admin = api(b["users"]["admin"])
    # The members list says so up front (the SPA offers no reset for it); a password account says it has one.
    members = {m["email"]: m for m in b_admin.get("users").json_body}
    assert members[VICTIM]["has_password"] is False
    assert members[b["users"]["admin"].email]["has_password"] is True
    # The attack's next step was an Admin reset: an account with no password has none to reset.
    reset = b_admin.post(f"users/{old.id}/reset-password")
    assert reset.status_code == 409 and "single sign-on" in reset.json_body["detail"]
    old.refresh_from_db()
    assert not old.has_usable_password()

    owner = signs_up()
    new = get_user_model().objects.get(username=VICTIM)
    assert new.pk != old.pk and new.check_password(PASSWORD) and new.is_active
    assert [m.organization.name for m in Membership.objects.filter(user=new)] == ["Victim Co"]
    assert owner.get("/api/v1/auth/me").json()["organization"]["name"] == "Victim Co"
    # The old account is retired in org B: no address, no identity, no password, deactivated; its membership stays
    # as the history B's records name.
    old.refresh_from_db()
    assert old.username == old.email == f"retired+{old.pk}@invalid"
    assert not old.is_active and not old.has_usable_password()
    assert not UserIdentity.objects.filter(user=old).exists()
    assert Membership.objects.filter(user=old, organization=b["org"]).exists()
    reclaimed = AuditEvent.objects.get(action="user.address_reclaimed")
    assert reclaimed.organization_id == b["org"].id and reclaimed.changes["user"] == old.pk
    assert new.pk not in reclaimed.changes.values()  # org B learns the address was reclaimed, not by whom
    assert AuditEvent.objects.get(action="user.address_released").changes["retired_user"] == old.pk

    # Org B's Admin can't reset the new account (not B's) ...
    assert b_admin.post(f"users/{new.id}/reset-password").status_code == 404
    # ... nor sign in as it through B's identity provider: the old identity signs nobody in, and matching by email
    # finds no member of B.
    attempt = Client()
    location = sign_in(attempt, VICTIM, query={"org": b["org"].slug})["Location"]
    assert location.startswith("/login?error="), location
    assert attempt.get("/api/v1/auth/me").status_code in (401, 403)
    new.refresh_from_db()
    assert new.check_password(PASSWORD) and not UserIdentity.objects.filter(user=new).exists()


# ---- the takeover paths -------------------------------------------------------------------------------------------


def test_an_emailed_invitation_takes_over_an_address_another_organization_approved(a, b, api, signup_on):
    old = approved(b, api)
    body = invite(api, a["users"]["admin"], VICTIM, "editor")
    preview = page(Client(), invitations.mailbox_token(body["link"].rsplit("#", 1)[1])).json()
    assert preview["account_exists"] is False  # the address is free for its owner
    client = Client()
    accepted = page(client, link_in(mail.outbox[-1], "/invite"), "post", {"name": "Real Owner", "password": PASSWORD})
    assert accepted.status_code == 200, accepted.json()
    new = get_user_model().objects.get(username=VICTIM)
    assert new.pk != old.pk and Membership.objects.get(user=new).organization_id == a["org"].id
    old.refresh_from_db()
    assert old.username == f"retired+{old.pk}@invalid" and not old.is_active
    released = AuditEvent.objects.get(action="user.address_released")
    assert released.organization_id == a["org"].id and released.changes["via"] == "invitation"
    assert AuditEvent.objects.get(action="user.address_reclaimed").organization_id == b["org"].id


def test_a_scim_account_doesnt_hold_its_address_either(a, b, signup_on):
    """SCIM creates accounts the same way (no password): modelled here as such an account in one organization."""
    old = get_user_model().objects.create_user(username=VICTIM, email=VICTIM, password=None)
    Membership.objects.create(user=old, organization=b["org"], role=Role.VIEWER)
    assert invitations.holder_for(VICTIM) is None
    signs_up()
    assert get_user_model().objects.get(username=VICTIM).pk != old.pk


def test_accounts_that_keep_their_address(a, b, signup_on):
    """A password account, platform staff, and an account with no password whose owner proved its mailbox hold their
    address: a signup for it gets the "you already have an account" note. (Membership in two organizations no longer
    counts: that was the review's escape, test_joining_a_second_organization_doesnt_make_it_hold_the_address.)"""
    with_password = a["users"]["viewer"]
    staff = get_user_model().objects.create_user(username="staff@victim.example", email="staff@victim.example")
    PlatformStaff.objects.create(user=staff)
    shared = get_user_model().objects.create_user(username="two@victim.example", email="two@victim.example")
    Membership.objects.create(user=shared, organization=a["org"], role=Role.VIEWER)
    Membership.objects.create(user=shared, organization=b["org"], role=Role.VIEWER)
    assert invitations.holder_for(shared.email) is None
    MailboxProof.objects.create(user=shared, via="invitation")
    for account in (with_password, staff, shared):
        assert invitations.holder_for(account.email) == account
        before = len(mail.outbox)
        response = post(Client(), "auth/signup", {"email": account.email, "name": "N P", "organization_name": "Z",
                                                  "password": PASSWORD})  # fmt: skip
        assert response.status_code == 202
        assert mail.outbox[before].subject == "You already have a Softwarden account"


def test_a_member_made_by_single_sign_on_is_still_a_member_when_invited_again(a, api):
    """Inside its own organization the account is still the member: an invitation to it is refused as one."""
    old = approved(a, api, "member@orga.test")
    response = api(a["users"]["admin"]).post("invitations", {"email": old.email, "role": "viewer"})
    assert response.status_code == 400 and "already a member" in response.json_body["errors"]["email"][0]


def test_an_organization_made_account_joins_another_only_through_the_emailed_link(a, b, api, simulator):
    """Signed in as itself, an organization-made account accepts only with the link that proves its mailbox, which
    then counts as its proof. (This test used to accept with the copy-once link: that was the escape the review
    found, test_joining_a_second_organization_doesnt_make_it_hold_the_address.)"""
    old = approved(b, api, "mover@orgb.test")
    body = invite(api, a["users"]["admin"], old.email, "viewer")
    client = Client()
    client.force_login(old)
    copied = page(client, body["link"].rsplit("#", 1)[1], "post")
    assert copied.status_code == 403 and problem_type(copied) == "/problems/invitation-emailed-link-required"
    assert not Membership.objects.filter(user=old, organization=a["org"]).exists()
    response = page(client, invitations.mailbox_token(body["link"].rsplit("#", 1)[1]), "post")
    assert response.status_code == 200, response.json()
    assert Membership.objects.filter(user=old, organization=a["org"]).exists()
    assert MailboxProof.objects.get(user=old).via == "invitation"
    assert invitations.holder_for(old.email) == old  # proven now: it holds its address


def test_joining_a_second_organization_doesnt_make_it_hold_the_address(a, b, api, simulator, signup_on):
    """A bypass of the organization-made account rule: org B approves x@victim.example; the attacker's second organization (A, same identity
    provider) invites it, signs in as it through that provider (the pinned identity, core/oidc.invitee) and accepts
    with the copy-once link, so the account would be in two organizations and hold the address again."""
    activate(b["org"], "okta")
    old = approved(b, api)
    activate(a["org"], "okta")
    body = invite(api, a["users"]["admin"], VICTIM, "viewer")
    client = Client()
    page(client, body["link"].rsplit("#", 1)[1])  # the preview remembers the invitation for single sign-on
    done = sign_in(client, VICTIM, next_path="/invite", query={"org": a["org"].slug})
    assert done["Location"] == "/invite", done["Location"]
    refused = page(client, body["link"].rsplit("#", 1)[1], "post")
    assert refused.status_code == 403 and problem_type(refused) == "/problems/invitation-emailed-link-required"
    assert Membership.objects.filter(user=old).count() == 1
    assert invitations.holder_for(VICTIM) is None
    # Even in two organizations (an older path, or joined before this fix) it doesn't hold the address.
    Membership.objects.create(user=old, organization=a["org"], role=Role.VIEWER)
    assert invitations.holder_for(VICTIM) is None
    signs_up()  # a confirmation link, not the "you already have an account" note
    assert get_user_model().objects.get(username=VICTIM).pk != old.pk


def test_the_emailed_link_names_the_organization_that_made_an_account_for_the_address(a, b, api, signup_on):
    old = approved(b, api)
    body = invite(api, a["users"]["admin"], VICTIM, "viewer")
    copied = page(Client(), body["link"].rsplit("#", 1)[1]).json()
    assert copied["organization_account"] is None  # the inviting Admin learns nothing
    emailed = page(Client(), invitations.mailbox_token(body["link"].rsplit("#", 1)[1])).json()
    assert emailed["organization_account"] == {"organization": "orgb", "sign_in_url": f"/login?org={b['org'].slug}"}
    assert emailed["account_exists"] is False and emailed["can_create_account"] is True
    assert old.is_active


def test_every_way_to_prove_the_mailbox_is_recorded(a, b, api, signup_on):
    signs_up("owner@proof.test")
    assert MailboxProof.objects.get(user__username="owner@proof.test").via == "signup"
    body = invite(api, a["users"]["admin"], "invitee@proof.test", "viewer")
    token = invitations.mailbox_token(body["link"].rsplit("#", 1)[1])
    assert page(Client(), token, "post", {"name": "In Vitee", "password": PASSWORD}).status_code == 200
    assert MailboxProof.objects.get(user__username="invitee@proof.test").via == "invitation"


def test_the_backfill_proves_password_accounts_and_staff_only(a, db):
    import importlib

    from django.apps import apps

    migration = importlib.import_module("core.migrations.0028_mailbox_proof")
    sso_only = get_user_model().objects.create_user(username="sso@proof.test", email="sso@proof.test")
    staff = get_user_model().objects.create_user(username="staff@proof.test", email="staff@proof.test")
    PlatformStaff.objects.create(user=staff)
    MailboxProof.objects.all().delete()
    migration.backfill(apps, None)
    proven = set(MailboxProof.objects.values_list("user_id", flat=True))
    assert staff.pk in proven and a["users"]["viewer"].pk in proven and sso_only.pk not in proven


def test_approving_pending_access_for_platform_staff_is_refused(a, api):
    staff = get_user_model().objects.create_user(username="ops@platform.test", email="ops@platform.test", password="x")
    PlatformStaff.objects.create(user=staff)
    row = pending(a, staff.email)
    response = api(a["users"]["admin"]).post(f"pending-access/{row.id}/approve", {"role": "admin"})
    assert response.status_code == 409 and problem_type(response) == "/problems/invitation-required"
    assert not Membership.objects.filter(user=staff).exists()
    assert not UserIdentity.objects.filter(user=staff).exists()
