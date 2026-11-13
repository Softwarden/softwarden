"""An invitation creates an account only from the link emailed to the invited address
(catalog/invitations.py `proof_required`, `mailbox_token`, catalog/access_viewsets.py).

With more than one organization on the installation (or in cloud mode), the copy of the link an Admin is shown can
accept for an account that exists (signed in as it) but can't create one: an Admin could otherwise create the account
for any address with a password of their choosing. The link in the invitation email carries a signature only the
server can make, so it proves the mailbox. Previews and create responses don't depend on whether the address has an
account elsewhere."""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client

from catalog import invitations
from core.models import Invitation, Membership
from tests.test_invitations import PASSWORD, invite, page, problem_type, signed_in, token_of

EMAILED_LINK_REQUIRED = "/problems/invitation-emailed-link-required"


@pytest.fixture(autouse=True)
def fresh_throttles():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def email_on(settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.SOFTWARDEN_BASE_URL = "https://app.softwarden.example.test"
    return settings


def emailed_token(email: str) -> str:
    message = [m for m in mail.outbox if m.to == [email]][-1]
    part = next(p for p in message.body.split() if "/invite#" in p)
    return part.rsplit("#", 1)[1]


def post(client: Client, path: str, body: dict):
    response = client.post(f"/api/v1/{path}", json.dumps(body), content_type="application/json")
    response.json_body = response.json() if response.content else None  # type: ignore[attr-defined]
    return response


# ---- the acceptance -----------------------------------------------------------------------------------------------


def test_the_link_an_admin_is_shown_cant_create_an_account_and_the_owner_can_still_sign_up(a, b, api, email_on):
    """With two organizations, the `link` from POST /invitations alone can't create
    an account for an address that has none; the real owner can still sign up afterwards."""
    email_on.SOFTWARDEN_SIGNUP_ENABLED = True
    body = invite(api, b["users"]["admin"], "ceo@victim.test", "admin")
    squatter = Client()
    accepted = page(squatter, token_of(body), "post", {"name": "Chief Exec", "password": "attacker-chosen-pass-2026"})
    assert accepted.status_code == 403 and problem_type(accepted) == EMAILED_LINK_REQUIRED
    assert not get_user_model().objects.filter(username="ceo@victim.test").exists()
    assert Invitation.objects.get().accepted_at is None
    assert squatter.get("/api/v1/auth/me").status_code in (401, 403)

    signup = post(Client(), "auth/signup", {"email": "ceo@victim.test", "name": "Real CEO",
                                            "organization_name": "Victim Corp", "password": PASSWORD})  # fmt: skip
    assert signup.status_code == 202
    link = next(p for p in mail.outbox[-1].body.split() if "/signup/verify#" in p)
    owner = Client()
    verified = post(owner, "auth/signup/verify", {"token": link.rsplit("#", 1)[1], "password": PASSWORD})
    assert verified.status_code == 200, verified.json_body
    user = get_user_model().objects.get(username="ceo@victim.test")
    assert user.check_password(PASSWORD)
    assert list(Membership.objects.filter(user=user).values_list("organization__name", flat=True)) == ["Victim Corp"]


def test_previews_and_create_responses_are_the_same_with_or_without_an_account_elsewhere(a, b, api):
    """Org A invites an address with an account in B, one with a deactivated account in
    B, platform staff, and one with no account; each create and each preview of the returned link answer alike."""
    from core.models import PlatformStaff

    gone = b["users"]["viewer"]
    gone.is_active = False
    gone.save()
    PlatformStaff.objects.create(user=b["users"]["editor"])
    addresses = [b["users"]["admin"].email, gone.email, b["users"]["editor"].email, "nobody@nowhere.test"]
    admin = api(a["users"]["admin"])
    creates, previews = [], []
    for email in addresses:
        response = admin.post("invitations", {"email": email, "role": "viewer"})
        creates.append(response)
        previews.append(page(Client(), token_of(response.json_body), "get"))
    varying = {"id", "email", "created_at", "expires_at", "link"}
    assert {r.status_code for r in creates} == {201}
    assert len({json.dumps({k: v for k, v in r.json_body.items() if k not in varying}) for r in creates}) == 1
    assert {r.status_code for r in previews} == {200}
    shapes = {json.dumps({k: v for k, v in r.json().items() if k not in ("email", "expires_at")}) for r in previews}
    assert len(shapes) == 1, shapes
    assert previews[0].json()["account_exists"] is None and previews[0].json()["can_create_account"] is False
    # Accepting with that link and nobody signed in: one answer too.
    accepted = [page(Client(), token_of(r.json_body), "post", {"name": "X Y", "password": PASSWORD}) for r in creates]
    assert {(r.status_code, problem_type(r)) for r in accepted} == {(403, EMAILED_LINK_REQUIRED)}


# ---- the emailed link ---------------------------------------------------------------------------------------------


def test_the_emailed_link_creates_the_account(a, b, api, email_on):
    body = invite(api, a["users"]["admin"], "colleague@example.test", "editor")
    assert body["emailed"] is True
    emailed = emailed_token("colleague@example.test")
    assert emailed != token_of(body) and emailed.startswith(token_of(body))
    assert token_of(body) in mail.outbox[-1].body  # the emailed link carries the token and its signature
    preview = page(Client(), emailed, "get").json()
    assert preview["account_exists"] is False and preview["can_create_account"] is True
    client = Client()
    response = page(client, emailed, "post", {"name": "Col League", "password": PASSWORD})
    assert response.status_code == 200, response.json()
    user = get_user_model().objects.get(username="colleague@example.test")
    assert Membership.objects.get(user=user).role == "editor"
    assert client.get("/api/v1/auth/me").status_code == 200
    assert page(Client(), token_of(body), "get").status_code == 404  # used: both forms of the link


def test_a_signature_for_another_token_or_a_tampered_one_is_unknown(a, b, api, email_on):
    one = invite(api, a["users"]["admin"], "one@example.test")
    two = invite(api, a["users"]["admin"], "two@example.test")
    signed_one = invitations.mailbox_token(token_of(one))
    forged = token_of(two) + signed_one[len(token_of(one)) :]
    assert page(Client(), forged, "get").status_code == 404
    tampered = signed_one[:-1] + ("A" if signed_one[-1] != "A" else "B")
    assert page(Client(), tampered, "get").status_code == 404


def test_an_old_secret_key_still_verifies_an_emailed_link(a, b, api, settings):
    body = invite(api, a["users"]["admin"], "rotated@example.test")
    signed = invitations.mailbox_token(token_of(body))
    settings.SECRET_KEY_FALLBACKS = [settings.SECRET_KEY]
    settings.SECRET_KEY = "a-new-secret-key-" + "x" * 50
    assert page(Client(), signed, "get").json()["can_create_account"] is True


def test_the_shown_link_still_joins_an_existing_account_signed_in_as_itself(a, b, api):
    body = invite(api, a["users"]["admin"], b["users"]["viewer"].email, "editor")
    client = signed_in(b["users"]["viewer"])
    preview = page(client, token_of(body), "get").json()
    # Signed in as the invited account, which knows it exists: the page offers Join.
    assert preview["session"] == "invitee" and preview["account_exists"] is True
    other = page(signed_in(b["users"]["editor"]), token_of(body), "get").json()
    assert other["session"] == "other_account" and other["account_exists"] is None
    response = page(client, token_of(body), "post")
    assert response.status_code == 200, response.json()
    assert Membership.objects.get(user=b["users"]["viewer"], organization=a["org"]).role == "editor"


def test_a_resend_makes_a_new_emailed_link_and_ends_the_old_one(a, b, api, email_on):
    body = invite(api, a["users"]["admin"], "resent@example.test")
    first = emailed_token("resent@example.test")
    api(a["users"]["admin"]).post(f"invitations/{body['id']}/resend")
    second = emailed_token("resent@example.test")
    assert first != second
    assert page(Client(), first, "get").status_code == 404
    assert page(Client(), second, "get").json()["can_create_account"] is True


def test_one_organization_needs_no_proof(a, api, settings):
    """A single-organization self-hosted installation keeps the copy-once link: its only Admins run the installation."""
    settings.SOFTWARDEN_CLOUD = False
    body = invite(api, a["users"]["admin"], "solo@example.test")
    preview = page(Client(), token_of(body), "get").json()
    assert preview["account_exists"] is False and preview["can_create_account"] is True
    response = page(Client(), token_of(body), "post", {"name": "Solo Person", "password": PASSWORD})
    assert response.status_code == 200, response.json()


def test_cloud_mode_needs_proof_with_one_organization(a, api, settings):
    settings.SOFTWARDEN_CLOUD = True
    body = invite(api, a["users"]["admin"], "solo@example.test")
    response = page(Client(), token_of(body), "post", {"name": "Solo Person", "password": PASSWORD})
    assert response.status_code == 403 and problem_type(response) == EMAILED_LINK_REQUIRED


def test_the_service_refuses_the_shown_link_too(a, b, api):
    body = invite(api, a["users"]["admin"], "service@example.test")
    with pytest.raises(invitations.EmailedLinkRequired):
        invitations.accept_as_new_account(token_of(body), {"name": "S P", "password": PASSWORD})
    user = invitations.accept_as_new_account(
        invitations.mailbox_token(token_of(body)), {"name": "S P", "password": PASSWORD}
    )
    assert user.username == "service@example.test"


# ---- review follow-ups: the proof link never goes through the organization's own mail server ------------------------


def own_mail_server(f, monkeypatch) -> list:
    """Organization f sends email through SMTP settings of its own, which its Admin controls; returns what it sent."""
    from notify import channels
    from notify.models import EmailSettings, EmailTransport

    EmailSettings.objects.update_or_create(
        organization=f["org"],
        defaults={"transport": EmailTransport.SMTP, "smtp_host": "smtp.attacker.test", "from_address": "it@b.test"},
    )
    sent: list = []
    monkeypatch.setattr(channels, "_send_smtp", lambda row, delivery: sent.append(delivery))
    return sent


def test_the_proof_link_goes_through_the_installations_mail_server_only(a, b, api, email_on, monkeypatch):
    """A bypass of the mailbox proof: org B's Admin points its own SMTP settings at a server they read and invites
    ceo@victim.test. The link that creates an account must not reach that server."""
    sent = own_mail_server(b, monkeypatch)
    body = invite(api, b["users"]["admin"], "ceo@victim.test", "admin")
    assert body["emailed"] is True
    assert sent == []  # nothing through the Admin's own server
    assert emailed_token("ceo@victim.test") == invitations.mailbox_token(token_of(body))
    assert mail.outbox[-1].subject == invitations.INSTALLATION_SUBJECT


def test_without_installation_email_the_organizations_server_carries_only_the_copy_once_link(
    a, b, api, settings, monkeypatch
):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = False
    sent = own_mail_server(b, monkeypatch)
    body = invite(api, b["users"]["admin"], "ceo@victim.test", "admin")
    assert body["emailed"] is True
    [delivery] = sent
    part = next(p for p in delivery.content["text"].split() if "/invite#" in p)
    token = part.rsplit("#", 1)[1]
    assert token == token_of(body)  # the copy-once link, which can't create an account
    assert "can't create a new account" in delivery.content["text"]
    accepted = page(Client(), token, "post", {"name": "Squatter", "password": "attacker-chosen-pass-2026"})
    assert accepted.status_code == 403 and problem_type(accepted) == EMAILED_LINK_REQUIRED
    assert not get_user_model().objects.filter(username="ceo@victim.test").exists()


def test_one_organization_may_still_use_its_own_mail_server_for_the_whole_link(a, api, email_on, monkeypatch):
    sent = own_mail_server(a, monkeypatch)
    body = invite(api, a["users"]["admin"], "solo@example.test")
    [delivery] = sent
    assert invitations.mailbox_token(token_of(body)) in delivery.content["text"]
    assert delivery.subject == "You're invited to orga on Softwarden"


def test_a_proof_link_an_organizations_server_carried_stops_proving_once_a_second_organization_exists(
    a, api, email_on, monkeypatch
):
    """Review follow-up: with one organization its own server may carry the whole link; once a second organization
    appears (within the link's 7 days), a link that went through that server no longer creates an account."""
    from tests.conftest import make_org

    sent = own_mail_server(a, monkeypatch)
    body = invite(api, a["users"]["admin"], "early@example.test")
    [delivery] = sent
    token = next(p for p in delivery.content["text"].split() if "/invite#" in p).rsplit("#", 1)[1]
    assert token == invitations.mailbox_token(token_of(body))
    assert page(Client(), token).json()["can_create_account"] is True
    make_org("second")  # signup, or the operator, adds an organization
    preview = page(Client(), token).json()
    assert preview["can_create_account"] is False and preview["account_exists"] is None
    accepted = page(Client(), token, "post", {"name": "Early Bird", "password": PASSWORD})
    assert accepted.status_code == 403 and problem_type(accepted) == EMAILED_LINK_REQUIRED
    assert not get_user_model().objects.filter(username="early@example.test").exists()
    # A resend now goes through the installation's server, whose link proves the mailbox again.
    api(a["users"]["admin"]).post(f"invitations/{body['id']}/resend")
    assert page(Client(), emailed_token("early@example.test")).json()["can_create_account"] is True


def test_a_proof_link_the_installation_sent_keeps_proving_when_a_second_organization_appears(a, api, email_on):
    from tests.conftest import make_org

    invite(api, a["users"]["admin"], "steady@example.test")
    token = emailed_token("steady@example.test")
    make_org("second")
    assert page(Client(), token).json()["can_create_account"] is True
