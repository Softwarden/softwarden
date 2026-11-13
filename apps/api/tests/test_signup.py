"""Public signup with email confirmation, and the cloud invitation email (catalog/signup.py,
catalog/signup_views.py, catalog/invitations.email_link)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from catalog import signup
from catalog.models import RelationshipType
from core import capabilities as caps
from core.models import AuditEvent, Edition, Membership, Organization, PendingSignup, Role

PASSWORD = "a-much-longer-passphrase"  # noqa: S105  (a synthetic test password)


@pytest.fixture(autouse=True)
def fresh_throttles():
    """Signup, resend and confirm count in the process cache (the signup and sign-in rates)."""
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


def post(client: Client, path: str, body: dict, **extra):
    response = client.post(f"/api/v1/{path}", json.dumps(body), content_type="application/json", **extra)
    response.json_body = response.json() if response.content else None  # type: ignore[attr-defined]
    return response


def form(email="new.person@example.test", **overrides):
    return {
        "email": email,
        "name": "New Person",
        "organization_name": "Fabrikam",
        "password": PASSWORD,
        "timezone": "Europe/London",
        **overrides,
    }


def token_in(message) -> str:
    line = next(part for part in message.body.split() if "/signup/verify#" in part)
    return line.rsplit("#", 1)[1]


def sign_up(email="new.person@example.test", client=None, **overrides) -> str:
    """Sign up and return the token from the confirmation email."""
    before = len(mail.outbox)
    response = post(client or Client(), "auth/signup", form(email, **overrides))
    assert response.status_code == 202, response.json_body
    assert len(mail.outbox) == before + 1
    return token_in(mail.outbox[-1])


def verify(client: Client, token: str, password=PASSWORD):
    return post(client, "auth/signup/verify", {"token": token, "password": password})


# ---- on, off, email -------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_signup_is_off_by_default(settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    assert settings.SOFTWARDEN_SIGNUP_ENABLED is False
    client = Client()
    for path, body in (
        ("auth/signup", form()),
        ("auth/signup/resend", {"email": "x@example.test"}),
        ("auth/signup/verify", {"token": "x" * 43, "password": PASSWORD}),
    ):
        response = post(client, path, body)
        assert response.status_code == 404, path
        assert response.json_body["type"] == "/problems/signup-disabled"
    assert client.get("/api/v1/auth/options").json()["signup_enabled"] is False
    assert not PendingSignup.objects.exists() and mail.outbox == []


@pytest.mark.django_db
def test_signup_refuses_clearly_without_email(settings):
    settings.SOFTWARDEN_SIGNUP_ENABLED = True
    settings.SOFTWARDEN_EMAIL_CONFIGURED = False
    response = post(Client(), "auth/signup", form())
    assert response.status_code == 503
    assert response.json_body["type"] == "/problems/signup-unavailable"
    assert "SOFTWARDEN_SMTP_HOST" in response.json_body["detail"]
    assert Client().get("/api/v1/auth/options").json()["signup_enabled"] is False
    assert not PendingSignup.objects.exists()


@pytest.mark.django_db
def test_options_report_signup_when_on(signup_on):
    assert Client().get("/api/v1/auth/options").json()["signup_enabled"] is True


@pytest.mark.django_db
def test_signup_needs_the_csrf_token(signup_on):
    client = Client(enforce_csrf_checks=True)
    response = post(client, "auth/signup", form())
    assert response.status_code == 403 and response.json_body["type"] == "/problems/csrf-failed"
    client.get("/api/v1/auth/csrf")
    response = post(client, "auth/signup", form(), HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)
    assert response.status_code == 202


# ---- signing up -----------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_signing_up_emails_a_link_and_creates_nothing_yet(signup_on):
    response = post(Client(), "auth/signup", form("New.Person@Example.test"))
    assert response.status_code == 202
    assert set(response.json_body) == {"detail"}
    [message] = mail.outbox
    assert message.to == ["new.person@example.test"]
    assert "https://app.softwarden.example.test/signup/verify#" in message.body
    token = token_in(message)
    row = PendingSignup.objects.get()
    assert row.email == "new.person@example.test" and row.organization_name == "Fabrikam"
    assert row.token_hash == signup.hash_token(token) and token not in json.dumps(
        {f.attname: str(getattr(row, f.attname)) for f in row._meta.concrete_fields}
    )
    assert PASSWORD not in row.password_hash and row.password_hash
    assert timedelta(hours=23) < row.expires_at - timezone.now() <= timedelta(hours=24)
    assert not get_user_model().objects.filter(username="new.person@example.test").exists()
    assert not Organization.objects.exists() and not AuditEvent.objects.exists()


@pytest.mark.django_db
def test_signup_validates_its_fields(signup_on):
    response = post(Client(), "auth/signup", form("not-an-email", name="", organization_name="", password="short"))
    assert response.status_code == 400
    assert set(response.json_body["errors"]) == {"email", "name", "organization_name", "password"}
    response = post(Client(), "auth/signup", form(timezone="Mars/Olympus"))
    assert response.status_code == 400 and set(response.json_body["errors"]) == {"timezone"}
    assert mail.outbox == [] and not PendingSignup.objects.exists()


# ---- confirming -----------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_confirming_creates_the_account_a_cloud_organization_and_its_admin(signup_on):
    token = sign_up()
    client = Client()
    response = verify(client, token)
    assert response.status_code == 200, response.json_body
    me = response.json_body
    assert me["role"] == "admin" and me["edition"] == "cloud"
    assert caps.ORG_SELF_DELETE in me["capabilities"] and caps.BILLING in me["capabilities"]
    assert me["organization"]["name"] == "Fabrikam" and me["organization"]["slug"] == "fabrikam"
    assert me["organization_deletion"] is None
    user = get_user_model().objects.get(username="new.person@example.test")
    assert user.first_name == "New" and user.last_name == "Person" and user.check_password(PASSWORD)
    org = Organization.objects.get()
    assert org.edition == Edition.CLOUD and org.timezone == "Europe/London" and org.coordinator == user
    assert Membership.objects.get(user=user).role == Role.ADMIN
    assert RelationshipType.objects.filter(organization=org).exists()  # seeded like any new organization
    assert not PendingSignup.objects.exists()
    created = AuditEvent.objects.get(action="user.created")
    assert created.organization == org and created.changes["via"] == "signup" and created.actor == user
    assert AuditEvent.objects.filter(organization=org, action="user.login").exists()
    assert client.get("/api/v1/auth/me").status_code == 200  # this browser is signed in
    # the account signs in with its password like any other
    assert post(Client(), "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 200


@pytest.mark.django_db
def test_a_second_organization_of_the_same_name_gets_its_own_slug(signup_on):
    verify(Client(), sign_up("one@example.test"))
    verify(Client(), sign_up("two@example.test"))
    assert sorted(Organization.objects.values_list("slug", flat=True)) == ["fabrikam", "fabrikam-2"]


@pytest.mark.django_db
def test_confirming_needs_the_password_chosen_at_signup(signup_on):
    token = sign_up()
    response = verify(Client(), token, password="another-long-passphrase")
    assert response.status_code == 400 and set(response.json_body["errors"]) == {"password"}
    assert not get_user_model().objects.exists() and PendingSignup.objects.exists()
    assert verify(Client(), token).status_code == 200  # the link still works with the right password


@pytest.mark.django_db
def test_an_expired_link_is_refused(signup_on):
    token = sign_up()
    PendingSignup.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    response = verify(Client(), token)
    assert response.status_code == 404 and response.json_body["type"] == "/problems/signup-link-unavailable"
    assert not get_user_model().objects.exists()


@pytest.mark.django_db
def test_a_used_link_is_refused(signup_on):
    token = sign_up()
    assert verify(Client(), token).status_code == 200
    response = verify(Client(), token)
    assert response.status_code == 404 and response.json_body["type"] == "/problems/signup-link-unavailable"
    assert Organization.objects.count() == 1


@pytest.mark.django_db
def test_an_unknown_or_malformed_token_is_refused(signup_on):
    for token in ("x" * 43, "short", None, 7):
        response = post(Client(), "auth/signup/verify", {"token": token, "password": PASSWORD})
        assert response.status_code == 404, token


@pytest.mark.django_db
def test_resending_sends_a_new_link_and_the_old_one_stops_working(signup_on):
    old = sign_up()
    PendingSignup.objects.update(last_sent_at=timezone.now() - timedelta(minutes=2))
    response = post(Client(), "auth/signup/resend", {"email": "new.person@example.test"})
    assert response.status_code == 202 and len(mail.outbox) == 2
    new = token_in(mail.outbox[-1])
    assert new != old
    assert verify(Client(), old).status_code == 404
    assert verify(Client(), new).status_code == 200


@pytest.mark.django_db
def test_signing_up_again_replaces_the_link(signup_on):
    old = sign_up()
    PendingSignup.objects.update(last_sent_at=timezone.now() - timedelta(minutes=2))
    new = sign_up(organization_name="Fabrikam Two")
    assert verify(Client(), old).status_code == 404
    assert verify(Client(), new).status_code == 200
    assert Organization.objects.get().name == "Fabrikam Two"


@pytest.mark.django_db
def test_one_message_per_address_per_minute(signup_on):
    sign_up()
    again = post(Client(), "auth/signup", form())
    resent = post(Client(), "auth/signup/resend", {"email": "new.person@example.test"})
    assert again.status_code == resent.status_code == 202
    assert len(mail.outbox) == 1  # both answered as usual; nothing more was sent


@pytest.mark.django_db
def test_an_account_created_meanwhile_ends_the_signup(signup_on):
    token = sign_up()
    # An account with a password (an invitation's): one made by single sign-on with none wouldn't hold the address.
    get_user_model().objects.create_user(
        username="new.person@example.test", email="new.person@example.test", password="an-invitees-passphrase"
    )
    response = verify(Client(), token)
    assert response.status_code == 409
    assert not PendingSignup.objects.exists() and not Organization.objects.exists()


@pytest.mark.django_db
def test_an_email_that_could_not_be_sent_is_a_503(signup_on, monkeypatch):
    from notify import transactional

    monkeypatch.setattr(transactional, "send", lambda *args, **kwargs: False)
    response = post(Client(), "auth/signup", form())
    assert response.status_code == 503 and response.json_body["type"] == "/problems/email-failed"


@pytest.mark.django_db
def test_the_smtp_failure_is_caught_and_logged_without_the_address(signup_on, caplog, monkeypatch):
    from django.core.mail.backends.locmem import EmailBackend

    def broken(self, messages):
        raise ConnectionRefusedError("smtp down")

    monkeypatch.setattr(EmailBackend, "send_messages", broken)
    response = post(Client(), "auth/signup", form())
    assert response.status_code == 503
    assert "new.person@example.test" not in caplog.text and "ConnectionRefusedError" in caplog.text


# ---- no account enumeration -------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_existing_account_gets_the_same_answer_and_a_note_instead_of_a_link(a, signup_on):
    new = post(Client(), "auth/signup", form("someone.new@example.test"))
    existing = post(Client(), "auth/signup", form("Admin@orga.test"))
    assert new.status_code == existing.status_code == 202
    assert new.json_body == existing.json_body
    note = mail.outbox[-1]
    assert note.to == ["admin@orga.test"] and "already have an account" in note.body
    assert "/signup/verify#" not in note.body
    assert not PendingSignup.objects.filter(email="admin@orga.test").exists()
    # A repeat within the minute answers the same and sends nothing more.
    assert post(Client(), "auth/signup", form("admin@orga.test")).json_body == new.json_body
    assert len(mail.outbox) == 2


@pytest.mark.django_db
def test_resend_answers_the_same_for_any_email(a, signup_on):
    bodies = {
        email: post(Client(), "auth/signup/resend", {"email": email}).json_body
        for email in ("nobody@example.test", "admin@orga.test")
    }
    assert len({json.dumps(b) for b in bodies.values()}) == 1
    assert mail.outbox == []


# ---- throttles ------------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_signup_and_resend_are_throttled_per_client_address(signup_on):
    client = Client()
    for n in range(10):
        path = "auth/signup" if n % 2 else "auth/signup/resend"
        assert post(client, path, {"email": "bad"}, REMOTE_ADDR="198.51.100.7").status_code == 400
    refused = post(client, "auth/signup", form(), REMOTE_ADDR="198.51.100.7")
    assert refused.status_code == 429 and refused.json_body["type"] == "/problems/rate-limited"
    assert "Retry-After" in refused
    assert post(client, "auth/signup", form(), REMOTE_ADDR="198.51.100.8").status_code == 202  # another address


@pytest.mark.django_db
def test_confirming_is_throttled_at_the_sign_in_rate(signup_on):
    client = Client()
    for _ in range(10):
        assert verify(client, "x" * 43).status_code == 404
    assert verify(client, "x" * 43).status_code == 429


@pytest.mark.django_db
def test_a_forwarded_address_counts_only_through_a_trusted_proxy(signup_on):
    """The same peer and trusted-proxy rules as sign-in: an untrusted X-Forwarded-For is ignored."""
    client = Client()
    for n in range(10):
        spoofed = f"203.0.113.{n}"
        response = post(client, "auth/signup/resend", {"email": "bad"}, HTTP_X_FORWARDED_FOR=spoofed)
        assert response.status_code == 400
    assert post(client, "auth/signup/resend", {"email": "bad"}, HTTP_X_FORWARDED_FOR="203.0.113.99").status_code == 429


# ---- the purge schedule ---------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_schedule_purges_expired_signups_only(signup_on):
    from core import jobs

    sign_up("old@example.test")
    sign_up("fresh@example.test")
    PendingSignup.objects.filter(email="old@example.test").update(expires_at=timezone.now() - timedelta(minutes=1))
    assert jobs.SCHEDULES["signup.purge"].func is signup.purge_expired
    assert signup.purge_expired(timezone.now()) == {"deleted": 1}
    assert list(PendingSignup.objects.values_list("email", flat=True)) == ["fresh@example.test"]


# ---- inviting a colleague -------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_invitation_is_emailed_when_email_is_configured_and_still_returns_its_link(a, api, signup_on):
    response = api(a["users"]["admin"]).post("invitations", {"email": "colleague@orga.test", "role": "editor"})
    assert response.status_code == 201
    body = response.json_body
    assert body["emailed"] is True and body["link"].startswith("https://app.softwarden.example.test/invite#")
    [message] = mail.outbox
    assert message.to == ["colleague@orga.test"] and body["link"] in message.body
    # Through the installation's mail server the Subject names no organization; the body does.
    assert message.subject == "Your invitation to Softwarden" and "orga" in message.body and "Editor" in message.body
    # Resending emails the new link.
    resent = api(a["users"]["admin"]).post(f"invitations/{body['id']}/resend").json_body
    assert resent["emailed"] is True and resent["link"] in mail.outbox[-1].body and resent["link"] != body["link"]


@pytest.mark.django_db
def test_without_email_an_invitation_is_only_the_copy_once_link(a, api, settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = False
    body = api(a["users"]["admin"]).post("invitations", {"email": "colleague@orga.test", "role": "viewer"}).json_body
    assert body["emailed"] is False and "/invite#" in body["link"] and mail.outbox == []


@pytest.mark.django_db
def test_a_new_customer_signs_up_and_invites_a_colleague_with_no_operator(signup_on):
    """The signup acceptance check: signup, confirm, invite by email, the colleague accepts from the email."""
    owner = Client()
    assert verify(owner, sign_up("owner@fabrikam.test")).status_code == 200
    invited = post(owner, "invitations", {"email": "colleague@fabrikam.test", "role": "editor"})
    assert invited.status_code == 201 and invited.json_body["emailed"] is True
    token = mail.outbox[-1].body.split("/invite#", 1)[1].split()[0]
    colleague = Client()
    accepted = post(colleague, "auth/invitations/accept", {"token": token, "name": "Col League", "password": PASSWORD})
    assert accepted.status_code == 200, accepted.json_body
    assert accepted.json_body["role"] == "editor" and accepted.json_body["edition"] == "cloud"
    org = Organization.objects.get()
    assert set(Membership.objects.filter(organization=org).values_list("role", flat=True)) == {"admin", "editor"}
    # Limits: the cloud defaults leave room for the rest of the journey (connect Entra, run discovery, add
    # what it found) with no operator raising anything.
    connection = post(
        owner, "connections/microsoft", {"name": "Entra", "tenant_id": "simulated", "secret": "synthetic"}
    )
    assert connection.status_code == 201, connection.json_body
    assert post(owner, f"connections/microsoft/{connection.json_body['id']}/test", {}).status_code == 200
    connector = post(
        owner, "connectors", {"source": "entra", "name": "Entra", "microsoft_connection": connection.json_body["id"]}
    )
    assert connector.status_code == 201, connector.json_body
    assert post(owner, f"connectors/{connector.json_body['id']}/run", {}).status_code in (201, 202)
    from tests.conftest import drain_jobs

    drain_jobs("discovery.run")
    from discovery.models import Observation

    candidate = Observation.objects.filter(organization=org, kind="application").order_by("display_name").first()
    assert candidate is not None
    created = post(colleague, f"discovery/observations/{candidate.id}/create", {})
    assert created.status_code in (200, 201), created.json_body
    meters = {r["name"]: r for r in owner.get("/api/v1/organization/limits").json()["limits"]}
    assert meters["members"]["usage"] == 2 and meters["members"]["limit"] == 50
    assert meters["connectors"]["usage"] == 1 and meters["records"]["usage"] == 1
    assert all(r["usage"] is None or r["usage"] < r["limit"] for r in meters.values())
    # ... and reaches the first renewal reminder: a renewal with its notice deadline ten days out, then the hourly
    # reminder schedule, as the worker runs it.
    from datetime import date

    from catalog import services
    from core import jobs
    from notify.models import Reminder

    renewal_date = (services.org_today(org) + timedelta(days=20)).isoformat()
    renewal = post(
        colleague,
        "renewals",
        {
            "name": "Fabrikam domain",
            "renewal_date": renewal_date,
            "auto_renew": "yes",
            "notice_value": 10,
            "notice_unit": "days",
        },
    )
    assert renewal.status_code == 201, renewal.json_body
    jobs.SCHEDULES["reminders.generate"].func(timezone.now())
    deadline = date.fromisoformat(renewal_date) - timedelta(days=10)
    assert Reminder.objects.filter(organization=org, due_date=deadline, status="open").exists()


# ---- review follow-ups ------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_refused_email_keeps_no_link_and_no_cooldown(signup_on, monkeypatch):
    from notify import transactional

    real_send = transactional.send
    monkeypatch.setattr(transactional, "send", lambda *args, **kwargs: False)
    assert post(Client(), "auth/signup", form()).status_code == 503
    assert not PendingSignup.objects.exists()
    monkeypatch.setattr(transactional, "send", real_send)
    old = sign_up()  # straight away: the failure used no cooldown
    row = PendingSignup.objects.get()
    PendingSignup.objects.update(last_sent_at=timezone.now() - timedelta(minutes=2))
    monkeypatch.setattr(transactional, "send", lambda *args, **kwargs: False)
    assert post(Client(), "auth/signup/resend", {"email": row.email}).status_code == 503
    after = PendingSignup.objects.get()
    assert after.token_hash == row.token_hash and after.send_count == 1  # rolled back
    monkeypatch.setattr(transactional, "send", real_send)
    assert verify(Client(), old).status_code == 200  # the earlier link still works


@pytest.mark.django_db
def test_the_send_limit_is_a_rolling_window(signup_on):
    sign_up()
    for _ in range(signup.SEND_LIMIT - 1):
        PendingSignup.objects.update(last_sent_at=timezone.now() - timedelta(minutes=2))
        post(Client(), "auth/signup/resend", {"email": "new.person@example.test"})
    assert len(mail.outbox) == signup.SEND_LIMIT
    PendingSignup.objects.update(last_sent_at=timezone.now() - timedelta(minutes=2))
    assert post(Client(), "auth/signup/resend", {"email": "new.person@example.test"}).status_code == 202
    assert len(mail.outbox) == signup.SEND_LIMIT  # at the limit: same answer, nothing sent
    PendingSignup.objects.update(window_started_at=timezone.now() - signup.SEND_WINDOW - timedelta(seconds=1))
    post(Client(), "auth/signup/resend", {"email": "new.person@example.test"})
    assert len(mail.outbox) == signup.SEND_LIMIT + 1  # a new window
    assert PendingSignup.objects.get().send_count == 1


@pytest.mark.django_db
def test_an_expired_signup_starts_again_even_at_its_limit(signup_on):
    sign_up()
    PendingSignup.objects.update(send_count=signup.SEND_LIMIT, last_sent_at=timezone.now() - timedelta(hours=25))
    PendingSignup.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
    token = sign_up()
    assert PendingSignup.objects.get().send_count == 1
    assert verify(Client(), token).status_code == 200


@pytest.mark.django_db
def test_names_with_line_breaks_are_refused(signup_on):
    response = post(Client(), "auth/signup", form(organization_name="Fabrikam\nBcc: x@evil.test", name="New\tPerson"))
    assert response.status_code == 400 and set(response.json_body["errors"]) == {"organization_name", "name"}


@pytest.mark.django_db
def test_email_subjects_lose_control_characters(a, api, signup_on):
    from notify import transactional

    assert transactional.clean_subject("You're invited to A\r\nBcc: x") == "You're invited to A Bcc: x"
    Organization.objects.filter(pk=a["org"].pk).update(name="Orga\nBcc: evil@example.test")
    body = api(a["users"]["admin"]).post("invitations", {"email": "c@orga.test", "role": "viewer"}).json_body
    assert body["emailed"] is True
    # Through the installation's mail server the name stays out of the Subject altogether.
    assert "\n" not in mail.outbox[-1].subject and "Bcc" not in mail.outbox[-1].subject


def _orphan(email="former@example.test"):
    """A deactivated account in no organization, with an old sign-in identity: what a purge leaves."""
    from core.models import UserIdentity

    user = get_user_model().objects.create_user(
        username=email, email=email, password="old-password-old", is_active=False
    )
    UserIdentity.objects.create(user=user, issuer="https://idp.old.test", subject="s-old")
    return user


def _assert_retired(old, address):
    from core.models import UserIdentity

    old.refresh_from_db()
    assert old.username == old.email == f"retired+{old.pk}@invalid"
    assert old.is_active is False and not old.has_usable_password()
    assert not UserIdentity.objects.filter(user=old).exists()
    fresh = get_user_model().objects.get(username=address)
    assert fresh.pk != old.pk and fresh.is_active and fresh.check_password(PASSWORD)
    assert not UserIdentity.objects.filter(user=fresh).exists()
    return fresh


@pytest.mark.django_db
def test_signing_up_with_a_left_over_account_retires_it_and_makes_a_fresh_one(signup_on):
    orphan = _orphan()
    token = sign_up("former@example.test")
    assert "/signup/verify#" in mail.outbox[-1].body  # a link, not "you already have an account"
    response = verify(Client(), token)
    assert response.status_code == 200, response.json_body
    fresh = _assert_retired(orphan, "former@example.test")
    assert response.json_body["user"]["id"] == fresh.pk
    org = Organization.objects.get()
    released = AuditEvent.objects.get(organization=org, action="user.address_released")
    assert released.changes["retired_user"] == orphan.pk and released.changes["via"] == "signup"
    assert AuditEvent.objects.get(organization=org, action="user.created").changes["user"] == fresh.pk


@pytest.mark.django_db
def test_a_deactivated_account_that_still_belongs_somewhere_keeps_its_address(a, signup_on):
    get_user_model().objects.filter(username="viewer@orga.test").update(is_active=False)
    assert post(Client(), "auth/signup", form("viewer@orga.test")).status_code == 202
    assert "already have an account" in mail.outbox[-1].body
    assert not PendingSignup.objects.exists()


@pytest.mark.django_db
def test_accepting_an_invitation_for_a_left_over_address_retires_the_old_account(a, api, signup_on):
    orphan = _orphan("former@orga.test")
    body = api(a["users"]["admin"]).post("invitations", {"email": "former@orga.test", "role": "editor"}).json_body
    token = body["link"].rsplit("#", 1)[1]
    preview = post(Client(), "auth/invitations/preview", {"token": token})
    assert preview.status_code == 200 and preview.json_body["account_exists"] is False
    accepted = post(Client(), "auth/invitations/accept", {"token": token, "name": "Fresh Start", "password": PASSWORD})
    assert accepted.status_code == 200, accepted.json_body
    fresh = _assert_retired(orphan, "former@orga.test")
    assert fresh.first_name == "Fresh"
    assert Membership.objects.get(user=fresh, organization=a["org"]).role == "editor"
    assert not Membership.objects.filter(user=orphan).exists()
    released = AuditEvent.objects.get(organization=a["org"], action="user.address_released")
    assert released.changes["retired_user"] == orphan.pk and released.changes["via"] == "invitation"


@pytest.mark.django_db
def test_a_live_organization_keeps_showing_the_old_person(b, api, signup_on):
    """The old row stays what other organizations' history points at: their audit log and records keep its name."""
    from catalog import services
    from catalog.models import Resource

    old = b["users"]["editor"]
    shown_before = old.get_full_name() or old.email  # "editor@orgb.test": the fixture's accounts have no name
    services.audit(b["org"], old, "resource.updated", None, {"name": ["x", "y"]})
    Membership.objects.filter(user=old).delete()  # left orgb, and was then deactivated
    get_user_model().objects.filter(pk=old.pk).update(is_active=False)
    assert verify(Client(), sign_up("editor@orgb.test")).status_code == 200

    reader = api(b["users"]["admin"])
    events = reader.get("audit-events?action=resource.updated").json_body["results"]
    assert [e["actor"]["name"] for e in events] == [shown_before]
    app = Resource.objects.get(organization=b["org"])
    assert app.it_steward_id == old.pk
    record = reader.get(f"applications/{app.id}").json_body
    assert record["it_steward"]["name"] == shown_before and record["it_steward"]["id"] == old.pk


@pytest.fixture
def reclaim_race(transactional_db, settings):
    """A left-over account, a pending signup for its address and an invitation to it, ready to be claimed at once."""
    from django.contrib.auth.hashers import make_password

    from catalog import invitations
    from tests.conftest import make_org

    settings.SOFTWARDEN_SIGNUP_ENABLED = True
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    orphan = _orphan("contested@example.test")
    f = make_org("racer")
    now = timezone.now()
    signup_token = "s" * 43
    PendingSignup.objects.create(
        email="contested@example.test",
        name="Signup Person",
        organization_name="Signup Co",
        password_hash=make_password(PASSWORD),
        token_hash=signup.hash_token(signup_token),
        expires_at=now + timedelta(hours=1),
        last_sent_at=now,
        window_started_at=now,
    )
    _, invite_token = invitations.create_invitation(
        f["org"], f["users"]["admin"], {"email": "contested@example.test", "role": "viewer"}
    )
    # The emailed link's token: the signup's organization makes this a two-organization installation.
    return orphan, signup_token, invitations.mailbox_token(invite_token)


def test_a_signup_and_an_invitation_claiming_one_address_at_once_make_one_account(reclaim_race):
    from catalog import invitations
    from tests.test_concurrency import run_together

    orphan, signup_token, invite_token = reclaim_race
    errors = run_together(
        lambda: signup.verify({"token": signup_token, "password": PASSWORD}),
        lambda: invitations.accept_as_new_account(invite_token, {"name": "Invited Person", "password": PASSWORD}),
    )
    assert errors.count(None) == 1, errors
    assert {type(e).__name__ for e in errors if e is not None} <= {"Conflict", "SignInFirst"}
    live = get_user_model().objects.filter(username="contested@example.test")
    assert live.count() == 1 and live.get().pk != orphan.pk
    orphan.refresh_from_db()
    assert orphan.username == f"retired+{orphan.pk}@invalid"
    assert AuditEvent.objects.filter(action="user.address_released").count() == 1
    winner = live.get()
    [membership] = Membership.objects.filter(user=winner)  # exactly one organization for the new account
    signup_won = errors[0] is None
    from core.models import Invitation

    invitation = Invitation.objects.get(email="contested@example.test")
    if signup_won:
        assert type(errors[1]).__name__ == "SignInFirst"
        assert membership.organization.name == "Signup Co" and membership.role == "admin"
        assert not PendingSignup.objects.exists()
        # the losing invitation is untouched, and trying again asks the invitee to sign in
        assert invitation.accepted_at is None and invitation.revoked_at is None
        with pytest.raises(invitations.SignInFirst):
            invitations.accept_as_new_account(invite_token, {"name": "Invited Person", "password": PASSWORD})
    else:
        assert type(errors[0]).__name__ == "Conflict"
        assert membership.organization.name == "racer" and invitation.accepted_by_id == winner.pk
        # The losing signup made no organization. Which way it lost depends on the timing: it rolled back with its
        # link still there, unconsumed, or (the invitation's account committed before it looked) it removed its
        # pending row and answered Conflict. Either way confirming again is refused.
        assert not Organization.objects.filter(name="Signup Co").exists()
        from core.problems import Conflict

        if PendingSignup.objects.filter(email="contested@example.test").exists():
            with pytest.raises(Conflict):
                signup.verify({"token": signup_token, "password": PASSWORD})
        else:  # the link went with the pending row
            with pytest.raises(signup.LinkUnavailable):
                signup.verify({"token": signup_token, "password": PASSWORD})
    assert set(Organization.objects.values_list("name", flat=True)) == {"racer"} | (
        {"Signup Co"} if signup_won else set()
    )


@pytest.mark.django_db
def test_an_invitation_goes_through_the_organizations_own_email_channel(a, api, settings, monkeypatch):
    from notify import channels
    from notify.models import EmailSettings, EmailTransport

    settings.SOFTWARDEN_EMAIL_CONFIGURED = False  # no installation email: only the organization's own
    EmailSettings.objects.update_or_create(
        organization=a["org"],
        defaults={"transport": EmailTransport.SMTP, "smtp_host": "smtp.orga.test", "from_address": "it@orga.test"},
    )
    sent = []
    monkeypatch.setattr(channels, "_send_smtp", lambda row, delivery: sent.append((row, delivery)))
    body = api(a["users"]["admin"]).post("invitations", {"email": "c@orga.test", "role": "viewer"}).json_body
    assert body["emailed"] is True
    [(row, delivery)] = sent
    assert row.organization_id == a["org"].pk and delivery.recipient == "c@orga.test"
    assert body["link"] in delivery.content["text"] and delivery.pk is not None
    from notify.models import Delivery

    assert not Delivery.objects.filter(pk=delivery.pk).exists()  # never stored: it carries the link


@pytest.fixture
def race_signup(transactional_db, settings):
    from django.contrib.auth.hashers import make_password

    settings.SOFTWARDEN_SIGNUP_ENABLED = True
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    now = timezone.now()
    token = "t" * 43
    PendingSignup.objects.create(
        email="race@example.test",
        name="Race Person",
        organization_name="Race Co",
        password_hash=make_password(PASSWORD),
        token_hash=signup.hash_token(token),
        expires_at=now + timedelta(hours=1),
        last_sent_at=now,
        window_started_at=now,
    )
    return token


def test_two_confirmations_at_once_make_one_account_and_organization(race_signup):
    from tests.test_concurrency import run_together

    data = {"token": race_signup, "password": PASSWORD}
    errors = run_together(lambda: signup.verify(dict(data)), lambda: signup.verify(dict(data)))
    assert sorted(type(e).__name__ for e in errors if e is not None) == ["LinkUnavailable"]
    assert errors.count(None) == 1
    assert Organization.objects.count() == 1 and get_user_model().objects.count() == 1
    assert Membership.objects.count() == 1 and not PendingSignup.objects.exists()


# ---- Reserved slugs, no web addresses in names, a daily ceiling ------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["API", "admin", "SCIM", "Auth", "Platform", "v2", "Login"])
def test_signing_up_as_a_reserved_word_never_gets_that_slug(signup_on, name):
    """A new organization's slug is never a word the service's own paths use."""
    from core.models import RESERVED_SLUGS

    assert verify(Client(), sign_up(organization_name=name)).status_code == 200
    org = Organization.objects.get(name=name)
    assert org.slug not in RESERVED_SLUGS and org.slug == f"{name.lower()}-2"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value",
    [
        ("organization_name", "Claim your prize at https://evil.example"),
        ("organization_name", "evil.example"),
        ("organization_name", "Visit www.evil.example now"),
        ("organization_name", "EVIL.COM/claim"),
        ("name", "Pat http://x.test"),
        ("name", "Pat evil.co"),
    ],
)
def test_a_web_address_in_a_name_is_refused(signup_on, field, value):
    """Both names go into a mail to an address the sender doesn't own; a link there is phishing."""
    response = post(Client(), "auth/signup", form(**{field: value}))
    assert response.status_code == 400 and list(response.json_body["errors"]) == [field]
    assert not PendingSignup.objects.exists() and not mail.outbox


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["Contoso Ltd.", "Dr. Smith & Co", "A.B. Holdings", "Fabrikam 2.0", "St. Mary's"])
def test_ordinary_names_with_dots_are_accepted(signup_on, name):
    assert post(Client(), "auth/signup", form(organization_name=name)).status_code == 202


@pytest.mark.django_db
def test_a_daily_ceiling_stops_new_organizations(signup_on):
    """At most SOFTWARDEN_SIGNUP_DAILY_LIMIT organizations are created in 24 hours, counted from the database (so
    every app process agrees); signing up and confirming answer 503 /problems/signup-paused meanwhile."""
    signup_on.SOFTWARDEN_SIGNUP_DAILY_LIMIT = 2
    early = sign_up("early@example.test")  # sent before the ceiling was reached; confirmed after
    Organization.objects.create(name="Earlier today")
    old = Organization.objects.create(name="Yesterday")
    Organization.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=25))
    assert verify(Client(), sign_up("first@example.test")).status_code == 200  # the second of today
    refused = post(Client(), "auth/signup", form("late@example.test"))
    assert refused.status_code == 503 and refused.json_body["type"] == "/problems/signup-paused"
    assert not PendingSignup.objects.filter(email="late@example.test").exists()
    held = verify(Client(), early)
    assert held.status_code == 503 and held.json_body["type"] == "/problems/signup-paused"
    assert PendingSignup.objects.filter(email="early@example.test").exists()  # kept: it can confirm tomorrow
    signup_on.SOFTWARDEN_SIGNUP_DAILY_LIMIT = 0  # 0: no ceiling
    assert verify(Client(), early).status_code == 200
