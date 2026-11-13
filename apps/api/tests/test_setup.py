"""First-run setup (core/bootstrap.py, core/setup_views.py): the one-time token file, the public setup API, its bad-token limit and CSRF, the CLI, setup
closing after the first account by any path, the credential key shown once, and the trusted-proxy rule."""

import importlib
import io
import json
import os
import threading
import time

import pytest
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import Client, RequestFactory

from catalog import admin_services
from core import bootstrap, credentials
from core.middleware import TrustedProxyMiddleware, client_ip
from core.models import AuditEvent, BootstrapState, Membership, Organization, Role
from core.problems import Conflict
from tests.conftest import TEST_CREDENTIAL_KEY, make_org

PASSWORD = "tangerine-orbit-ladder"
FORM = {
    "organization_name": "Fabrikam (synthetic)",
    "timezone": "America/Chicago",
    "email": "Ana@Fabrikam.test",
    "name": "Ana Silva",
    "password": PASSWORD,
}


def browser(**extra):
    client = Client(enforce_csrf_checks=True, **extra)
    client.get("/api/v1/setup")
    return client


def post(client, data, csrf=True, **extra):
    headers = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value} if csrf else {}
    response = client.post("/api/v1/setup", data=json.dumps(data), content_type="application/json", **headers, **extra)
    response.json_body = response.json() if response.content else None
    return response


# ---- state ------------------------------------------------------------------------------------------------------


def test_fresh_install_is_pending_and_any_first_account_closes_it(fresh):
    assert bootstrap.pending()
    get_user_model().objects.create_user(username="lone@example.test", password="x")  # any path, even no org
    assert not bootstrap.pending()
    assert BootstrapState.objects.get().completed_at is not None


def test_any_first_organization_closes_setup(fresh):
    make_org("seeded")
    state = BootstrapState.objects.get()
    assert state.completed_at is not None
    # Removing every account later never reopens it.
    Membership.objects.all().delete()
    get_user_model().objects.all().delete()
    Organization.objects.update(coordinator=None)
    assert not bootstrap.pending()


def test_a_missing_state_row_fails_closed(fresh):
    BootstrapState.objects.all().delete()
    assert not bootstrap.pending()


def test_migration_marks_an_existing_installation_completed(fresh):
    migration = importlib.import_module("core.migrations.0006_bootstrap_state")
    BootstrapState.objects.all().delete()
    migration.create_state(django_apps, None)
    assert BootstrapState.objects.get().completed_at is None
    BootstrapState.objects.all().delete()
    Organization.objects.create(name="Existing")
    BootstrapState.objects.all().delete()  # the signal created nothing; the row is gone again
    migration.create_state(django_apps, None)
    assert BootstrapState.objects.get().completed_at is not None


# ---- the token file ---------------------------------------------------------------------------------------------


def test_token_is_private_reused_and_replaced_when_expired(fresh):
    token = bootstrap.prepare_token()
    path = bootstrap.token_path()
    assert len(token) == bootstrap.TOKEN_LENGTH and path.read_text(encoding="ascii") == token
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert bootstrap.prepare_token() == token  # reused while valid
    assert bootstrap.matches(token) and not bootstrap.matches(token[:-1] + "x") and not bootstrap.matches(None)
    old = time.time() - bootstrap.TOKEN_TTL - 5
    os.utime(path, (old, old))
    assert bootstrap.read_token() == "" and not bootstrap.matches(token)
    renewed = bootstrap.prepare_token()
    assert renewed and renewed != token


def test_no_token_once_setup_is_closed(fresh):
    bootstrap.prepare_token()
    make_org("seeded")
    assert bootstrap.prepare_token() == ""
    assert not bootstrap.token_path().exists()


# ---- the CLI ------------------------------------------------------------------------------------------------------


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    call_command("bootstrap_admin", *args, stdout=out, stderr=err)
    return out.getvalue()


def test_cli_prints_the_token_and_the_setup_address(fresh, settings):
    settings.SOFTWARDEN_BASE_URL = "https://softwarden.example.test"
    prepared = run("--prepare")
    token = bootstrap.read_token()
    assert token and token not in prepared and "open" in prepared
    out = run()
    assert "https://softwarden.example.test/setup" in out and token in out.splitlines()
    assert "bootstrap_admin --create" in out


def test_cli_says_when_setup_is_closed(fresh):
    make_org("seeded")
    assert "closed" in run("--prepare")
    out = run()
    assert "closed" in out and "reset_admin" in out


def test_cli_create_closes_setup_and_removes_the_token(fresh, monkeypatch):
    bootstrap.prepare_token()
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", PASSWORD)
    call_command(
        "bootstrap_admin",
        "--create",
        "--no-input",
        org_name="CLI org",
        timezone="UTC",
        email="cli@example.test",
        name="Cli Admin",
        stdout=io.StringIO(),
    )
    assert not bootstrap.pending() and not bootstrap.token_path().exists()
    assert post(browser(), {**FORM, "token": "anything"}).status_code == 409


# ---- the credential key, shown once -------------------------------------------------------------------------------


def test_generated_credential_key_is_shown_once(fresh, monkeypatch, tmp_path):
    key = credentials.generate_key()
    (tmp_path / "credential_keys").write_text(key + "\n", encoding="utf-8")
    monkeypatch.setenv("SOFTWARDEN_SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("SOFTWARDEN_CREDENTIAL_KEYS")
    assert key not in run("--prepare")
    first = run()
    assert key in first.splitlines() and "Back it up" in first and "down -v" in first
    second = run()
    assert key not in second
    assert credentials.key_id(key) in second and "docker compose exec app cat" in second
    assert bootstrap.key_already_shown()


def test_a_key_from_the_environment_is_never_printed(fresh):
    out = run()
    assert TEST_CREDENTIAL_KEY not in out and credentials.key_id(TEST_CREDENTIAL_KEY) in out


def test_cli_warns_when_no_key_is_configured(fresh, monkeypatch):
    monkeypatch.delenv("SOFTWARDEN_CREDENTIAL_KEYS")
    assert "No credential encryption key" in run()


# ---- the API ------------------------------------------------------------------------------------------------------


def test_get_is_public_uncached_and_sets_the_csrf_cookie(fresh):
    client = Client()
    response = client.get("/api/v1/setup")
    assert response.status_code == 200 and response.json() == {"pending": True}
    assert "csrftoken" in response.cookies
    assert "no-cache" in response["Cache-Control"] or "max-age=0" in response["Cache-Control"]


def test_sign_in_options_say_whether_setup_is_pending(fresh):
    """The sign-in page reads setup_pending from auth/options rather than making a second request to
    setup."""
    client = Client()
    assert client.get("/api/v1/auth/options").json()["setup_pending"] is True
    post(browser(), {**FORM, "token": bootstrap.prepare_token()})
    assert client.get("/api/v1/auth/options").json()["setup_pending"] is False


def test_setup_creates_the_organization_and_admin_and_signs_in(fresh):
    token = bootstrap.prepare_token()
    client = browser()
    response = post(client, {**FORM, "token": token})
    assert response.status_code == 200, response.json_body
    body = response.json_body
    assert body["role"] == "admin" and body["organization"]["name"] == "Fabrikam (synthetic)"
    assert body["user"]["email"] == "ana@fabrikam.test" and body["must_change_password"] is False
    org = Organization.objects.get()
    membership = Membership.objects.get()
    assert org.timezone == "America/Chicago" and org.coordinator == membership.user
    assert membership.role == Role.ADMIN and membership.user.check_password(PASSWORD)
    created = AuditEvent.objects.get(action="user.created")
    assert created.changes["via"] == "setup" and created.organization == org
    assert AuditEvent.objects.filter(action="user.login", actor=membership.user).exists()
    everything = json.dumps(list(AuditEvent.objects.values_list("changes", flat=True)))
    assert PASSWORD not in everything and token not in everything
    # Signed in, setup closed, token gone.
    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.get("/api/v1/setup").json() == {"pending": False}
    assert not bootstrap.token_path().exists() and BootstrapState.objects.get().completed_at is not None
    assert token not in json.dumps(body)


def test_setup_needs_the_csrf_token(fresh):
    token = bootstrap.prepare_token()
    response = post(browser(), {**FORM, "token": token}, csrf=False)
    assert response.status_code == 403 and "CSRF" in response.json_body["detail"]
    assert not Organization.objects.exists() and bootstrap.pending()


@pytest.mark.parametrize("which", ["wrong", "missing", "expired"])
def test_bad_tokens_are_refused(fresh, which):
    token = bootstrap.prepare_token()
    if which == "expired":
        old = time.time() - bootstrap.TOKEN_TTL - 1
        os.utime(bootstrap.token_path(), (old, old))
    data = {**FORM, "token": {"wrong": "x" * 43, "missing": "", "expired": token}[which]}
    response = post(browser(), data)
    assert response.status_code == 403 and "bootstrap_admin" in response.json_body["detail"]
    assert not Organization.objects.exists()


def test_invalid_fields_are_explained_and_keep_the_token(fresh):
    token = bootstrap.prepare_token()
    client = browser()
    response = post(client, {**FORM, "token": token, "organization_name": "", "password": "short"})
    assert response.status_code == 400
    assert {"organization_name", "password"} <= set(response.json_body["errors"])
    assert not Organization.objects.exists() and bootstrap.matches(token)
    assert post(client, {**FORM, "token": token}).status_code == 200


def test_five_bad_tokens_block_the_address_for_five_minutes(fresh):
    token = bootstrap.prepare_token()
    client = browser()
    for _ in range(bootstrap.FAILURE_LIMIT):
        assert post(client, {**FORM, "token": "y" * 43}).status_code == 403
    blocked = post(client, {**FORM, "token": token})  # even the right token, from this address
    assert blocked.status_code == 429 and 0 < int(blocked["Retry-After"]) <= bootstrap.FAILURE_WINDOW + 1
    assert not Organization.objects.exists()
    # Another address isn't blocked.
    other = browser(REMOTE_ADDR="192.0.2.7")
    assert post(other, {**FORM, "token": token}, REMOTE_ADDR="192.0.2.7").status_code == 200


def test_a_forged_forwarded_address_does_not_dodge_the_limit(fresh, settings):
    settings.TRUSTED_PROXIES = []
    bootstrap.prepare_token()
    client = browser()
    for n in range(bootstrap.FAILURE_LIMIT):
        post(client, {**FORM, "token": "z" * 43}, HTTP_X_FORWARDED_FOR=f"198.51.100.{n}")
    response = post(client, {**FORM, "token": "z" * 43}, HTTP_X_FORWARDED_FOR="198.51.100.99")
    assert response.status_code == 429


def test_behind_the_trusted_proxy_each_client_has_its_own_limit(fresh, settings):
    settings.TRUSTED_PROXIES = ["127.0.0.1"]
    token = bootstrap.prepare_token()
    client = browser()
    for _ in range(bootstrap.FAILURE_LIMIT):
        post(client, {**FORM, "token": "z" * 43}, HTTP_X_FORWARDED_FOR="198.51.100.1")
    assert post(client, {**FORM, "token": token}, HTTP_X_FORWARDED_FOR="198.51.100.1").status_code == 429
    assert post(client, {**FORM, "token": token}, HTTP_X_FORWARDED_FOR="198.51.100.2").status_code == 200


def test_closed_setup_answers_409(fresh):
    token = bootstrap.prepare_token()
    make_org("seeded")
    response = post(browser(), {**FORM, "token": token})
    assert response.status_code == 409 and "reset_admin" in response.json_body["detail"]
    assert Organization.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_setups_create_one_organization():
    """The advisory lock serialises the first Admin: of two racing creations exactly one succeeds."""
    BootstrapState.objects.update_or_create(pk=1, defaults={"completed_at": None})
    barrier = threading.Barrier(2)
    results: list[str] = []

    def create(n):
        try:
            barrier.wait(timeout=10)
            admin_services.bootstrap_admin(
                {
                    "org_name": f"Race {n}",
                    "timezone": "UTC",
                    "email": f"r{n}@example.test",
                    "name": "Racer",
                    "password": PASSWORD,
                },
                via="setup",
            )
            results.append("created")
        except Conflict:
            results.append("conflict")
        finally:
            connection.close()

    threads = [threading.Thread(target=create, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(results) == ["conflict", "created"]
    assert Organization.objects.count() == 1 and get_user_model().objects.count() == 1


# ---- setup_status ----------------------------------------------------------------------------------------------


def status_lines():
    out = io.StringIO()
    call_command("setup_status", stdout=out)
    return out.getvalue().split()


def test_setup_status_reports_facts_without_secrets(fresh):
    assert status_lines() == ["migrated=1", "setup_pending=1", "encrypted_secrets=0"]
    data = make_org("seeded")
    from discovery import engine as discovery

    discovery.create_connector(
        data["org"], data["users"]["admin"], {"tenant_id": "simulated", "client_id": "", "secret": "s3cret-value"}
    )
    lines = status_lines()
    assert lines == ["migrated=1", "setup_pending=0", "encrypted_secrets=1"]


# ---- forwarded headers -----------------------------------------------------------------------------------------


def through_middleware(remote, **headers):
    request = RequestFactory().get("/", REMOTE_ADDR=remote, **headers)
    TrustedProxyMiddleware(lambda r: r)(request)
    return request


def test_forwarded_headers_are_dropped_unless_the_peer_is_a_trusted_proxy(settings):
    settings.TRUSTED_PROXIES = ["10.9.0.0/16", "localhost"]
    headers = {"HTTP_X_FORWARDED_FOR": "203.0.113.5", "HTTP_X_FORWARDED_PROTO": "https"}
    untrusted = through_middleware("192.0.2.10", **headers)
    assert not untrusted.is_secure() and client_ip(untrusted) == "192.0.2.10"
    by_range = through_middleware("10.9.3.4", **headers)
    assert by_range.is_secure() and client_ip(by_range) == "203.0.113.5"
    by_name = through_middleware("127.0.0.1", **headers)  # `localhost` resolves to it
    assert by_name.is_secure() and client_ip(by_name) == "203.0.113.5"
    settings.TRUSTED_PROXIES = []
    assert not through_middleware("10.9.3.4", **headers).is_secure()


def test_client_ip_takes_the_address_the_proxy_appended(settings):
    settings.TRUSTED_PROXIES = ["10.9.0.0/16"]
    request = through_middleware("10.9.3.4", HTTP_X_FORWARDED_FOR="1.1.1.1, 203.0.113.5")
    assert client_ip(request) == "203.0.113.5"
