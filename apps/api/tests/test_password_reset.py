"""Self-service password reset by an emailed, single-use, one-hour link
(catalog/password_reset.py, catalog/password_reset_views.py, core/email_tokens.py)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from catalog import admin_services, password_reset
from core.models import AuditEvent, Job, Membership, PasswordReset, PlatformStaff, Role
from tests.conftest import drain_jobs

NEW_PASSWORD = "a-brand-new-passphrase-2026"  # noqa: S105  (a synthetic test password)


@pytest.fixture(autouse=True)
def fresh_throttles():
    """Asking for a link and using one count in the process cache (the password_reset and sign-in rates)."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def email_on(settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.SOFTWARDEN_BASE_URL = "https://app.softwarden.example.test"
    return settings


def post(client: Client, path: str, body: dict):
    response = client.post(f"/api/v1/{path}", json.dumps(body), content_type="application/json")
    response.json_body = response.json() if response.content else None  # type: ignore[attr-defined]
    return response


def ask(email: str, client: Client | None = None):
    """Ask for a link, then run the queued send as the worker's next tick would."""
    response = post(client or Client(), "auth/password-reset", {"email": email})
    drain_jobs(password_reset.SEND_JOB)
    return response


def token_in(message) -> str:
    line = next(part for part in message.body.split() if "/reset-password#" in part)
    return line.rsplit("#", 1)[1]


def confirm(token: str, password: str = NEW_PASSWORD, client: Client | None = None):
    return post(client or Client(), "auth/password-reset/confirm", {"token": token, "password": password})


def signed_in(email: str, password: str = "correct-horse-battery") -> Client:  # noqa: S107  (the fixture's)
    client = Client()
    response = post(client, "auth/login", {"email": email, "password": password})
    assert response.status_code == 200, response.json_body
    return client


def share(user, org) -> None:
    Membership.objects.create(user=user, organization=org, role=Role.VIEWER)


# ---- the acceptance -----------------------------------------------------------------------------------------------


def test_a_shared_account_resets_its_password_by_link_and_its_old_sessions_end(a, b, api, email_on):
    """Request a reset for a shared account (an Admin can't reset it), follow the link, set a
    password; the old sessions end. The answer for an unknown address is identical."""
    user = a["users"]["editor"]
    share(user, b["org"])
    refused = api(a["users"]["admin"]).post(f"users/{user.id}/reset-password")
    assert refused.status_code == 409  # shared: no organization's Admin may reset it
    old = signed_in(user.email)
    assert old.get("/api/v1/auth/me").status_code == 200

    known, unknown = ask(user.email), ask("nobody.here@example.test")
    assert known.status_code == unknown.status_code == 202
    assert known.json_body == unknown.json_body
    assert known.content == unknown.content
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [user.email]
    assert mail.outbox[0].subject == "Reset your Softwarden password"
    token = token_in(mail.outbox[0])
    assert "https://app.softwarden.example.test/reset-password#" in mail.outbox[0].body

    done = confirm(token)
    assert done.status_code == 204, done.json_body
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)
    assert old.get("/api/v1/auth/me").status_code in (401, 403)  # the old session ended
    assert signed_in(user.email, NEW_PASSWORD).get("/api/v1/auth/me").status_code == 200
    # Single use.
    again = confirm(token, "yet-another-passphrase-2026")
    assert again.status_code == 404 and again.json_body["type"] == "/problems/password-reset-link-unavailable"
    # Audited in each of the account's organizations, never with the token or the password.
    for action in ("user.password_reset_requested", "user.password_reset_completed"):
        events = AuditEvent.objects.filter(action=action)
        assert {e.organization_id for e in events} == {a["org"].id, b["org"].id}, action
    dumped = json.dumps(list(AuditEvent.objects.values_list("changes", flat=True)))
    assert token not in dumped and NEW_PASSWORD not in dumped
    assert PasswordReset.objects.get().token_hash != token


def test_the_answer_is_the_same_for_every_address_that_gets_no_link(a, b, email_on):
    """Unknown, deactivated, single sign-on only, no organization, platform staff, cooling down: one answer."""
    sso_only = get_user_model().objects.create_user(username="sso@orga.test", email="sso@orga.test", password=None)
    Membership.objects.create(user=sso_only, organization=a["org"], role=Role.VIEWER)
    deactivated = a["users"]["viewer"]
    deactivated.is_active = False
    deactivated.save()
    get_user_model().objects.create_user(username="loose@example.test", email="loose@example.test", password="x" * 20)
    staff = b["users"]["viewer"]
    PlatformStaff.objects.create(user=staff)
    sent = ask(a["users"]["editor"].email)
    answers = [
        ask(email)
        for email in (
            "nobody@example.test",
            deactivated.email,
            sso_only.email,
            "loose@example.test",
            staff.email,
            a["users"]["editor"].email,  # a link a moment ago: cooling down
        )
    ]
    assert {(r.status_code, r.content) for r in answers} == {(sent.status_code, sent.content)}
    assert [m.to for m in mail.outbox] == [[a["users"]["editor"].email]]


def test_every_address_takes_the_same_path_and_the_worker_sends(a, email_on):
    """The request never sends mail, so a known address isn't slower than an unknown one. Each
    request queues one job whose payload is the account's id or null: never an address or a token."""
    user = a["users"]["editor"]
    known = post(Client(), "auth/password-reset", {"email": user.email})
    unknown = post(Client(), "auth/password-reset", {"email": "nobody.here@example.test"})
    assert known.content == unknown.content and known.status_code == unknown.status_code == 202
    assert mail.outbox == [] and not PasswordReset.objects.exists()  # nothing in the request
    payloads = list(
        Job.objects.filter(kind=password_reset.SEND_JOB).order_by("created_at").values_list("payload", flat=True)
    )
    assert payloads == [{"user": user.id}, {"user": None}]
    done = drain_jobs(password_reset.SEND_JOB)
    assert sorted(str(Job.objects.get(pk=job.pk).result) for job in done) == [str({"sent": False}), str({"sent": True})]
    assert [m.to for m in mail.outbox] == [[user.email]]


def test_links_per_account_are_limited_and_counted_in_the_database(a, email_on):
    user = a["users"]["admin"]
    for _ in range(password_reset.SEND_LIMIT):
        assert ask(user.email).status_code == 202
        PasswordReset.objects.update(created_at=timezone.now() - password_reset.COOLDOWN - timedelta(seconds=1))
    assert ask(user.email).status_code == 202
    assert len(mail.outbox) == password_reset.SEND_LIMIT
    # An hour later the count starts again.
    PasswordReset.objects.update(created_at=timezone.now() - password_reset.SEND_WINDOW - timedelta(seconds=1))
    ask(user.email)
    assert len(mail.outbox) == password_reset.SEND_LIMIT + 1


def test_a_link_expires_after_an_hour_and_an_old_row_is_cleared(a, email_on):
    user = a["users"]["admin"]
    ask(user.email)
    token = token_in(mail.outbox[-1])
    PasswordReset.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert confirm(token).json_body["type"] == "/problems/password-reset-link-unavailable"
    PasswordReset.objects.update(created_at=timezone.now() - password_reset.KEEP - timedelta(minutes=1))
    ask(a["users"]["viewer"].email)
    assert PasswordReset.objects.filter(user=user).count() == 0


def test_a_refused_password_keeps_the_link_usable(a, email_on):
    user = a["users"]["admin"]
    ask(user.email)
    token = token_in(mail.outbox[-1])
    weak = confirm(token, "short")
    assert weak.status_code == 400 and "password" in weak.json_body["errors"]
    assert confirm(token).status_code == 204


def test_using_a_link_ends_the_other_links_and_the_forced_change(a, email_on):
    user = a["users"]["viewer"]
    Membership.objects.filter(user=user).update(must_change_password=True)
    ask(user.email)
    first = token_in(mail.outbox[-1])
    PasswordReset.objects.update(created_at=timezone.now() - password_reset.COOLDOWN - timedelta(seconds=1))
    ask(user.email)
    second = token_in(mail.outbox[-1])
    assert confirm(second).status_code == 204
    assert confirm(first).status_code == 404
    assert not Membership.objects.filter(user=user, must_change_password=True).exists()


def test_a_password_set_another_way_ends_open_links(a, api, email_on):
    user = a["users"]["viewer"]
    ask(user.email)
    token = token_in(mail.outbox[-1])
    admin_services.change_password(user, "correct-horse-battery", "a-changed-passphrase-2026")
    assert confirm(token).status_code == 404
    PasswordReset.objects.update(created_at=timezone.now() - password_reset.COOLDOWN - timedelta(seconds=1))
    ask(user.email)
    token = token_in(mail.outbox[-1])
    assert api(a["users"]["admin"]).post(f"users/{user.id}/reset-password").status_code == 200
    assert confirm(token).status_code == 404


def test_an_account_that_can_no_longer_reset_cant_use_its_link(a, email_on):
    user = a["users"]["viewer"]
    ask(user.email)
    token = token_in(mail.outbox[-1])
    user.is_active = False
    user.save()
    assert confirm(token).status_code == 404


def test_reset_needs_email_and_says_so(a, settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = False
    for path, body in (
        ("auth/password-reset", {"email": a["users"]["admin"].email}),
        ("auth/password-reset/confirm", {"token": "x" * 43, "password": NEW_PASSWORD}),
    ):
        response = post(Client(), path, body)
        assert response.status_code == 503, path
        assert response.json_body["type"] == "/problems/password-reset-unavailable"
    assert Client().get("/api/v1/auth/options").json()["password_reset_enabled"] is False
    assert not PasswordReset.objects.exists()


def test_options_offer_reset_when_email_works(db, email_on):
    assert Client().get("/api/v1/auth/options").json()["password_reset_enabled"] is True


def test_a_refused_message_keeps_no_link_and_answers_the_same(a, email_on, monkeypatch):
    from notify import transactional

    monkeypatch.setattr(transactional, "send", lambda *args, **kwargs: False)
    response = ask(a["users"]["admin"].email)
    assert response.status_code == 202 and response.content == ask("nobody@example.test").content
    assert not PasswordReset.objects.exists()
    assert not AuditEvent.objects.filter(action="user.password_reset_requested").exists()


def test_bad_input(a, email_on):
    assert ask("not-an-email").status_code == 400
    for token in (None, 5, "short", "x" * 43):
        response = post(Client(), "auth/password-reset/confirm", {"token": token, "password": NEW_PASSWORD})
        assert response.status_code == 404, token


def test_both_routes_need_the_csrf_token(a, email_on):
    client = Client(enforce_csrf_checks=True)
    for path, body in (
        ("auth/password-reset", {"email": a["users"]["admin"].email}),
        ("auth/password-reset/confirm", {"token": "x" * 43, "password": NEW_PASSWORD}),
    ):
        assert post(client, path, body).status_code == 403, path
    assert mail.outbox == []


def test_asking_is_throttled_per_client_address(a, email_on, settings):
    from rest_framework.throttling import ScopedRateThrottle

    rates = {**ScopedRateThrottle.THROTTLE_RATES, "password_reset": "2/hour"}
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": rates}
    original = ScopedRateThrottle.THROTTLE_RATES
    ScopedRateThrottle.THROTTLE_RATES = rates
    try:
        assert [ask(f"n{i}@example.test").status_code for i in range(3)] == [202, 202, 429]
    finally:
        ScopedRateThrottle.THROTTLE_RATES = original
