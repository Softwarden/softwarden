"""`manage.py doctor` (core/doctor.py): ok / warn / fail per check with a fix hint, `--json`,
exit status 1 on a failure, and never a secret in the output, whatever the installation holds."""

import io
import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone

from core import backup, credentials, doctor
from core.models import BackupState, BootstrapState, MicrosoftConnection, Schedule, WorkerHeartbeat
from discovery import engine as discovery
from discovery.models import ConnectorRun, RunStatus, RunTrigger
from tests.conftest import TEST_CREDENTIAL_KEY, make_org

BACKUP_KEY = "doctor-backup-key-" + "k" * 40
CLIENT_SECRET = "doctor-client-secret-Zq81"
SMTP_PASSWORD = "doctor-smtp-password-Lm22"
OIDC_SECRET = "doctor-oidc-secret-Pp93"
ANTHROPIC_KEY = "sk-ant-doctor-Xy77"
DJANGO_KEY = "doctor-django-secret-key-" + "d" * 30
OPS_TOKEN = "doctor-ops-token-Rt45"


def run_doctor(*args):
    out = io.StringIO()
    code: int | str | None = 0
    try:
        call_command("doctor", *args, stdout=out)
    except SystemExit as exc:
        code = exc.code
    return out.getvalue(), code


def as_json():
    text, code = run_doctor("--json")
    return json.loads(text), code


def by_name(document, name, org=""):
    return [c for c in document["checks"] if c["name"] == name and c["organization"] == org]


def one(document, name, org=""):
    found = by_name(document, name, org)
    assert len(found) == 1, (name, document["checks"])
    return found[0]


@pytest.fixture
def server(settings, tmp_path, monkeypatch):
    """A production-shaped installation: debug off, HTTPS address on an accepted host, secure cookies, SMTP set, the
    worker ticking and backups configured with an acknowledged key and a recent set. The test database's password is
    the development one (compose.dev.yaml, CI), so the check that refuses it compares with another value."""
    monkeypatch.setattr("softwarden.settings.DEV_DB_PASSWORD", "not-the-test-database-password")
    settings.DEBUG = False
    settings.SECRET_KEY = DJANGO_KEY
    settings.SOFTWARDEN_BASE_URL = "https://softwarden.example.test"
    settings.SOFTWARDEN_DNS_RESOLVER = "10.0.0.2"  # a real resolver named; doctor never queries it
    settings.ALLOWED_HOSTS = ["softwarden.example.test", "127.0.0.1"]
    settings.CSRF_TRUSTED_ORIGINS = ["https://softwarden.example.test"]
    settings.SESSION_COOKIE_SECURE = True
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.SMTP_HOST = "smtp.example.test"
    settings.EMAIL_PORT = 587
    settings.EMAIL_HOST_PASSWORD = SMTP_PASSWORD
    key = tmp_path / "backup_key"
    key.write_text(BACKUP_KEY + "\n", encoding="utf-8")
    key.chmod(0o600)
    settings.BACKUP_DIR = str(tmp_path / "backups")
    settings.BACKUP_KEY_FILE = str(key)
    settings.BACKUP_KEY_FILE_CONFIGURED = False
    now = timezone.now()
    WorkerHeartbeat.objects.update_or_create(name="worker", defaults={"last_tick": now})
    BackupState.objects.update_or_create(
        pk=1,
        defaults={
            "last_success_at": now - timedelta(hours=3),
            "last_set": "softwarden-20261107T023000Z",
            "key_fingerprint": backup.key_fingerprint(),
            "key_acknowledged_at": now,
        },
    )
    return key


def connector_with_run(org, admin, secret=CLIENT_SECRET, run_status=RunStatus.SUCCEEDED):
    connector = discovery.create_connector(org, admin, {"tenant_id": "simulated", "client_id": "", "secret": secret})
    MicrosoftConnection.objects.filter(pk=connector.microsoft_connection_id).update(tested_revision=1, test_ok=True)
    if run_status is not None:
        ConnectorRun.objects.create(
            organization=org,
            connector=connector,
            trigger=RunTrigger.MANUAL,
            status=run_status,
            finished_at=timezone.now(),
            counts={"seen": 12},
            error="Microsoft Graph refused to list app registrations." if run_status == RunStatus.FAILED else "",
        )
    return connector


# ---- shape and exit status --------------------------------------------------------------------------------------------


def test_a_fresh_install_says_what_to_do_next_and_fails_without_a_worker(db):
    BootstrapState.objects.update_or_create(pk=1, defaults={"completed_at": None})
    cache.clear()
    document, code = as_json()
    assert code == 1 and document["status"] == "fail"
    assert one(document, "database")["status"] == "ok" and one(document, "migrations")["status"] == "ok"
    setup = one(document, "setup")
    assert setup["status"] == "warn" and "bootstrap_admin" in setup["fix"]
    worker = one(document, "worker")
    assert worker["status"] == "fail" and worker["fix"]
    assert document["counts"]["fail"] >= 1
    # Every check that isn't ok carries a fix; every status is one of three.
    for check in document["checks"]:
        assert check["status"] in ("ok", "warn", "fail")
        assert bool(check["fix"]) == (check["status"] != "ok"), check


def test_a_healthy_server_exits_zero(db, server):
    data = make_org("doctorok")
    connector_with_run(data["org"], data["users"]["admin"])
    document, code = as_json()
    assert code == 0 and document["status"] != "fail", document
    names = (
        "database",
        "migrations",
        "mode",
        "base_url",
        "worker",
        "schedules",
        "credential_keys",
        "email",
        "dns_resolver",
        "database_password",
        "demo_accounts",
    )
    for name in names:
        assert one(document, name)["status"] == "ok", one(document, name)
    assert one(document, "backup")["status"] == "ok" and one(document, "backup_key")["status"] == "ok"
    assert one(document, "setup")["message"].startswith("Done: 1 organization, 1 stored secret")
    assert one(document, "sso", "doctorok")["status"] == "ok"
    run = one(document, "discovery: Microsoft Entra ID", "doctorok")
    assert run["status"] == "ok" and "12 seen" in run["message"] and "simulated tenant" in run["message"]


def test_text_output_has_one_line_per_check_with_its_fix(db, server):
    make_org("doctortext")
    text, code = run_doctor()
    assert code == 0
    lines = text.splitlines()
    assert lines[0].startswith("Softwarden doctor, ")
    assert any(line.split()[:2] == ["OK", "database"] for line in lines)
    discovery_line = next(line for line in lines if "[doctortext] discovery" in line)
    assert discovery_line.split()[0] == "WARN"
    fix = lines[lines.index(discovery_line) + 1]
    assert "fix:" in fix and "FIRST_RUN.md" in fix
    assert lines[-1].endswith("failure(s).")


# ---- individual checks ------------------------------------------------------------------------------------------------


def test_an_unreachable_database_fails_and_skips_what_needs_it(db, monkeypatch):
    import softwarden.ops

    monkeypatch.setattr(softwarden.ops, "_database_and_migrations", lambda: ({"database": "unreachable"}, False))
    document, code = as_json()
    assert code == 1 and one(document, "database")["status"] == "fail"
    names = {c["name"] for c in document["checks"]}
    assert names == {"database", "mode", "database_password", "base_url", "credential_keys"}


def test_a_check_that_raises_is_a_failure_naming_only_the_error_type(db, server, monkeypatch):
    def broken():
        raise ValueError(f"leaked {SMTP_PASSWORD}")

    monkeypatch.setattr(doctor, "check_email", broken)
    text, code = run_doctor()
    assert code == 1 and "Couldn't check (ValueError)" in text and SMTP_PASSWORD not in text


def test_base_url_problems(db, server, settings):
    settings.SOFTWARDEN_BASE_URL = "https://other.example.test"
    assert one(as_json()[0], "base_url")["status"] == "fail"  # the app would refuse that host
    settings.SOFTWARDEN_BASE_URL = "http://softwarden.example.test"
    assert one(as_json()[0], "base_url")["status"] == "warn"
    settings.SOFTWARDEN_BASE_URL = "https://localhost"
    settings.ALLOWED_HOSTS = ["localhost"]
    settings.CSRF_TRUSTED_ORIGINS = ["https://localhost"]
    check = one(as_json()[0], "base_url")
    assert check["status"] == "warn" and "PUBLIC_HOSTNAME" in check["fix"]
    settings.SOFTWARDEN_BASE_URL = "https://softwarden.example.test"
    settings.ALLOWED_HOSTS = ["softwarden.example.test"]
    settings.CSRF_TRUSTED_ORIGINS = ["https://softwarden.example.test"]
    settings.SESSION_COOKIE_SECURE = False
    assert one(as_json()[0], "base_url")["status"] == "warn"


def test_a_simulated_dns_resolver_outside_development_is_a_warning(db, server, settings):
    """`simulated` verifies no real sign-in domain, so with DEBUG off doctor says so."""
    settings.SOFTWARDEN_DNS_RESOLVER = "simulated"
    check = one(as_json()[0], "dns_resolver")
    assert check["status"] == "warn" and "SOFTWARDEN_DNS_RESOLVER" in check["message"] and check["fix"]
    settings.DEBUG = True
    assert one(as_json()[0], "dns_resolver")["status"] == "ok"


def test_debug_mode_is_a_warning(db, server, settings):
    settings.DEBUG = True
    check = one(as_json()[0], "mode")
    assert check["status"] == "warn" and "SOFTWARDEN_DEBUG" in check["message"]


def test_the_development_database_password_fails_outside_debug(db, server, settings, monkeypatch):
    """Settings.py uses softwarden-dev only under DEBUG; set by hand on a server, doctor fails, without
    printing it."""
    password = str(settings.DATABASES["default"]["PASSWORD"])
    assert one(as_json()[0], "database_password")["status"] == "ok"
    monkeypatch.setattr("softwarden.settings.DEV_DB_PASSWORD", password)
    document, code = as_json()
    check = one(document, "database_password")
    assert check["status"] == "fail" and code != 0 and "POSTGRES_PASSWORD" in check["fix"]
    assert password not in json.dumps(document)
    settings.DEBUG = True
    assert one(as_json()[0], "database_password")["status"] == "ok"


def test_demo_accounts_with_the_published_password_warn_outside_debug(db, server, settings):
    """An active @example.test account that still has seed_demo's password is a warning (never in DEBUG);
    one whose password changed, or that is deactivated, isn't."""
    from django.contrib.auth import get_user_model

    from core.demo import DEMO_PASSWORD

    User = get_user_model()
    assert one(as_json()[0], "demo_accounts")["status"] == "ok"
    demo = User.objects.create_user("admin@example.test", "admin@example.test", DEMO_PASSWORD)
    changed = User.objects.create_user("editor@example.test", "editor@example.test", "a-new-long-password-1")
    User.objects.create_user("viewer@example.test", "viewer@example.test", DEMO_PASSWORD, is_active=False)
    User.objects.create_user("someone@contoso.example", "someone@contoso.example", DEMO_PASSWORD)
    check = one(as_json()[0], "demo_accounts")
    assert check["status"] == "warn" and "1 demo account(s)" in check["message"] and demo.username in check["message"]
    assert changed.username not in check["message"] and "viewer@" not in check["message"]
    assert DEMO_PASSWORD not in check["message"] + check["fix"]
    demo.set_password("another-long-password-2")
    demo.save()
    assert one(as_json()[0], "demo_accounts")["status"] == "ok"
    settings.DEBUG = True
    User.objects.create_user("platform@example.test", "platform@example.test", DEMO_PASSWORD)
    assert one(as_json()[0], "demo_accounts")["status"] == "ok"


def test_a_stale_worker_and_an_overdue_schedule_fail(db, server):
    WorkerHeartbeat.objects.filter(name="worker").update(last_tick=timezone.now() - timedelta(minutes=10))
    from core import jobs

    name = next(iter(jobs.SCHEDULES))
    Schedule.objects.create(name=name, cron="* * * * *", next_due=timezone.now() - timedelta(hours=2))
    document, code = as_json()
    assert code == 1
    assert one(document, "worker")["status"] == "fail"
    schedules = one(document, "schedules")
    assert schedules["status"] == "fail" and f"overdue: {name}" in schedules["message"]


def test_backup_states(db, server, settings):
    BackupState.objects.filter(pk=1).update(last_success_at=None, last_set="")
    document, _ = as_json()
    assert one(document, "backup")["status"] == "warn" and "backup_now" in one(document, "backup")["fix"]
    BackupState.objects.filter(pk=1).update(key_fingerprint="")
    assert one(as_json()[0], "backup_key")["status"] == "warn"  # generated key not confirmed off the host
    Path(settings.BACKUP_KEY_FILE).unlink()
    document, code = as_json()
    assert code == 1 and one(document, "backup_key")["status"] == "fail"
    settings.BACKUP_DIR = ""
    document, _ = as_json()
    assert [c["status"] for c in by_name(document, "backup")] == ["warn"] and not by_name(document, "backup_key")


def test_a_failed_backup_after_the_last_good_one_warns(db, server):
    BackupState.objects.filter(pk=1).update(
        last_failure_at=timezone.now(),
        last_failure="The backup failed while dumping the database (CalledProcessError).",
    )
    check = one(as_json()[0], "backup_last_run")
    assert check["status"] == "warn" and "dumping the database" in check["message"]


def test_email_off_warns_for_organizations_relying_on_the_installation_default(db, server, settings):
    make_org("doctormail")
    settings.SOFTWARDEN_EMAIL_CONFIGURED = False
    settings.SMTP_HOST = ""
    check = one(as_json()[0], "email")
    assert check["status"] == "warn" and "1 organization" in check["message"]


def test_discovery_states(db, server):
    data = make_org("doctordisc")
    org, admin = data["org"], data["users"]["admin"]
    assert one(as_json()[0], "discovery", "doctordisc")["status"] == "warn"  # none set up
    connector = connector_with_run(org, admin, run_status=RunStatus.FAILED)
    check = one(as_json()[0], f"discovery: {connector.name}", "doctordisc")
    assert check["status"] == "warn" and "refused to list" in check["message"]
    MicrosoftConnection.objects.filter(pk=connector.microsoft_connection_id).update(revision=2)
    check = one(as_json()[0], f"discovery: {connector.name}", "doctordisc")
    assert check["status"] == "warn" and "Test the connection" in check["fix"]


def test_secrets_under_a_key_that_is_gone_are_counted(db, server, monkeypatch):
    data = make_org("doctorkeys")
    connector_with_run(data["org"], data["users"]["admin"])
    assert not by_name(as_json()[0], "stored_secrets")
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", credentials.generate_key())
    check = one(as_json()[0], "stored_secrets")
    assert check["status"] == "warn" and check["message"].startswith("1 stored secret")


def test_missing_credential_keys_fail(db, server, monkeypatch):
    monkeypatch.delenv("SOFTWARDEN_CREDENTIAL_KEYS")
    document, code = as_json()
    assert code == 1 and one(document, "credential_keys")["status"] == "fail"


def test_sso_break_glass_and_no_password_admin_warn(db, server, settings):
    data = make_org("doctorsso")
    settings.SOFTWARDEN_FORCE_LOCAL_LOGIN = True
    check = one(as_json()[0], "sso", "doctorsso")
    assert check["status"] == "warn" and "FORCE_LOCAL_LOGIN" in check["fix"]
    settings.SOFTWARDEN_FORCE_LOCAL_LOGIN = False
    admin = data["users"]["admin"]
    admin.set_unusable_password()
    admin.save()
    check = one(as_json()[0], "sso", "doctorsso")
    assert check["status"] == "warn" and "reset_admin" in check["fix"]


# ---- no secrets -------------------------------------------------------------------------------------------------------


def test_no_secret_is_ever_printed(db, server, settings, monkeypatch):
    data = make_org("doctorsecrets")
    connector_with_run(data["org"], data["users"]["admin"])
    monkeypatch.setenv("SOFTWARDEN_OIDC_TENANT", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("SOFTWARDEN_OIDC_CLIENT_ID", "doctor-oidc-client")
    monkeypatch.setenv("SOFTWARDEN_OIDC_CLIENT_SECRET", OIDC_SECRET)
    settings.SOFTWARDEN_ASSISTANT_INSTALLATION_KEY = ANTHROPIC_KEY
    settings.OPS_TOKEN = OPS_TOKEN
    BackupState.objects.filter(pk=1).update(
        last_failure_at=timezone.now(), last_failure="The backup failed while encrypting (CalledProcessError)."
    )
    secrets = [
        TEST_CREDENTIAL_KEY,
        credentials.key_id(TEST_CREDENTIAL_KEY),
        BACKUP_KEY,
        backup.key_fingerprint(),
        CLIENT_SECRET,
        SMTP_PASSWORD,
        OIDC_SECRET,
        ANTHROPIC_KEY,
        DJANGO_KEY,
        OPS_TOKEN,
        str(settings.DATABASES["default"]["PASSWORD"]),
    ]
    text, _ = run_doctor()
    raw, _ = run_doctor("--json")
    assert "[doctorsecrets] sso" in text  # the per-organization checks ran
    for value in secrets:
        assert value and value not in text and value not in raw, value


def test_the_database_probe_gets_a_short_connect_timeout_only_for_its_run(db, server, monkeypatch):
    import os

    seen = []
    real = doctor.check_database

    def spy():
        seen.append(os.environ.get("PGCONNECT_TIMEOUT"))
        return real()

    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    monkeypatch.setattr(doctor, "check_database", spy)
    run_doctor()
    assert seen == ["5"] and "PGCONNECT_TIMEOUT" not in os.environ
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "30")  # an operator's own value wins and stays
    run_doctor()
    assert seen[-1] == "30" and os.environ["PGCONNECT_TIMEOUT"] == "30"


def test_a_failure_listing_organizations_is_reported_not_raised(db, server, monkeypatch):
    make_org("doctororgs")
    monkeypatch.setattr(doctor, "_per_organization", lambda: 1 / 0)
    document, code = as_json()
    assert code == 1 and one(document, "organizations")["message"] == "Couldn't check (ZeroDivisionError)."


def test_the_backup_key_hint_names_the_system_page_and_the_api(db, server):
    BackupState.objects.filter(pk=1).update(key_fingerprint="")
    fix = one(as_json()[0], "backup_key")["fix"]
    assert "System status" in fix and "POST /api/v1/system/backup/acknowledge-key" in fix
