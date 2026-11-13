"""Cloud deployment (docs/CLOUD.md): the installation mode SOFTWARDEN_DEPLOYMENT and the defaults it changes,
the managed-PostgreSQL settings (POSTGRES_SSLMODE / POSTGRES_SSLROOTCERT) and their reach into the backup's pg_dump, the
session advisory locks that make a pooler need session mode, cloud-mode first-run setup (platform staff, no
organization, no web setup), each worker's own liveness heartbeat, and `doctor`'s cloud checks.

Several workers claiming jobs and firing schedules at once are covered in tests/test_jobs.py
(test_concurrent_workers_run_each_job_exactly_once, test_a_schedule_fires_once_under_two_workers,
test_two_whole_ticks_at_once_fire_each_schedule_once)."""

import io
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.utils import timezone

from catalog import admin_services
from core import backup, bootstrap, credentials, doctor, totp
from core.models import BootstrapState, Membership, Organization, PlatformAuditEvent, PlatformStaff
from core.problems import Conflict, FieldErrors
from softwarden import env

API = Path(__file__).resolve().parents[1]
PASSWORD = "tangerine-orbit-ladder"
CLOUD_NAMES = {name for name, _fn in doctor.CLOUD_CHECKS}


# ---- parsing ----------------------------------------------------------------------------------------------------------


def test_deployment_defaults_to_selfhosted_and_refuses_anything_else(monkeypatch):
    monkeypatch.delenv("SOFTWARDEN_DEPLOYMENT", raising=False)
    assert env.deployment() == "selfhosted"
    monkeypatch.setenv("SOFTWARDEN_DEPLOYMENT", " Cloud ")
    assert env.deployment() == "cloud"
    monkeypatch.setenv("SOFTWARDEN_DEPLOYMENT", "saas")
    with pytest.raises(ValueError, match="SOFTWARDEN_DEPLOYMENT"):
        env.deployment()


def test_tls_options_reach_the_connection(monkeypatch):
    monkeypatch.delenv("POSTGRES_SSLMODE", raising=False)
    monkeypatch.delenv("POSTGRES_SSLROOTCERT", raising=False)
    monkeypatch.delenv("SOFTWARDEN_DB_JIT", raising=False)
    assert env.db_tls_options() == {}
    monkeypatch.setenv("POSTGRES_SSLMODE", "Verify-Full")
    monkeypatch.setenv("POSTGRES_SSLROOTCERT", "/etc/softwarden/pg/root.crt")
    assert env.db_options() == {
        "options": "-c jit=off",
        "sslmode": "verify-full",
        "sslrootcert": "/etc/softwarden/pg/root.crt",
    }
    monkeypatch.setenv("SOFTWARDEN_DB_JIT", "on")
    assert env.db_options() == {"sslmode": "verify-full", "sslrootcert": "/etc/softwarden/pg/root.crt"}
    monkeypatch.setenv("POSTGRES_SSLMODE", "required")
    with pytest.raises(ValueError, match="POSTGRES_SSLMODE"):
        env.db_tls_options()


def test_conn_max_age_default_is_the_callers(monkeypatch):
    monkeypatch.delenv("SOFTWARDEN_DB_CONN_MAX_AGE", raising=False)
    assert env.db_conn_max_age(default=60) == 60
    monkeypatch.setenv("SOFTWARDEN_DB_CONN_MAX_AGE", "0")
    assert env.db_conn_max_age(default=60) == 0


SETTINGS_VARS = (
    "SOFTWARDEN_DEPLOYMENT",
    "SOFTWARDEN_SIGNUP_ENABLED",
    "SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS",
    "SOFTWARDEN_SMTP_ANY_PORT",
    "SOFTWARDEN_DB_CONN_MAX_AGE",
    "POSTGRES_SSLMODE",
    "POSTGRES_SSLROOTCERT",
    "PGSSLMODE",
)


def load_settings(**extra: str) -> dict:
    """settings.py read in a fresh interpreter with exactly these variables (the rest of the environment unchanged)."""
    code = (
        "import django, json, sys; django.setup(); from django.conf import settings as s; "
        "d = s.DATABASES['default']; sys.stdout.write(json.dumps({"
        "'deployment': s.SOFTWARDEN_DEPLOYMENT, 'cloud': s.SOFTWARDEN_CLOUD, 'signup': s.SOFTWARDEN_SIGNUP_ENABLED, "
        "'private': s.SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS, 'any_port': s.SOFTWARDEN_SMTP_ANY_PORT, "
        "'age': d['CONN_MAX_AGE'], "
        "'checks': d['CONN_HEALTH_CHECKS'], 'options': d['OPTIONS']}))"
    )
    environ = {k: v for k, v in os.environ.items() if k not in SETTINGS_VARS}
    environ.update(DJANGO_SETTINGS_MODULE="softwarden.settings", **extra)
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=API, env=environ, capture_output=True, text=True
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def test_selfhosted_development_defaults_are_unchanged():
    """Under DEBUG (as here) no connection is kept: runserver starts a thread per request."""
    got = load_settings()
    assert got["deployment"] == "selfhosted" and got["cloud"] is False
    assert got["signup"] is False and got["private"] is True and got["any_port"] is True
    assert got["age"] == 0 and got["checks"] is False
    assert got["options"] == {"options": "-c jit=off"}


def test_production_keeps_connections_for_60_seconds():
    """Outside development a connection is kept 60 s and checked before reuse, self-hosted as in cloud mode."""
    got = load_settings(SOFTWARDEN_DEBUG="0", SOFTWARDEN_SECRET_KEY="d7-" + "x" * 50)
    assert got["cloud"] is False and got["age"] == 60 and got["checks"] is True
    got = load_settings(SOFTWARDEN_DEBUG="0", SOFTWARDEN_SECRET_KEY="d7-" + "x" * 50, SOFTWARDEN_DB_CONN_MAX_AGE="0")
    assert got["age"] == 0 and got["checks"] is False


def test_cloud_mode_changes_the_installation_defaults():
    got = load_settings(SOFTWARDEN_DEPLOYMENT="cloud", POSTGRES_SSLMODE="require")
    assert got["deployment"] == "cloud" and got["cloud"] is True
    assert got["signup"] is True  # signup defaults on
    assert got["private"] is False  # an organization's own servers must be public
    assert got["any_port"] is False  # mail ports only
    assert got["age"] == 60 and got["checks"] is True  # connections kept, checked before reuse
    assert got["options"] == {"options": "-c jit=off", "sslmode": "require"}


def test_explicit_settings_win_over_the_cloud_defaults():
    got = load_settings(
        SOFTWARDEN_DEPLOYMENT="cloud",
        SOFTWARDEN_SIGNUP_ENABLED="0",
        SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS="1",
        SOFTWARDEN_DB_CONN_MAX_AGE="0",
    )
    assert got["signup"] is False and got["private"] is True and got["age"] == 0


def test_a_bad_deployment_stops_start_up():
    with pytest.raises(RuntimeError, match="SOFTWARDEN_DEPLOYMENT must be selfhosted or cloud"):
        load_settings(SOFTWARDEN_DEPLOYMENT="hosted")


def test_the_backup_tools_follow_the_connections_tls_options(tmp_path, monkeypatch):
    monkeypatch.setenv("PGSSLMODE", "prefer")  # libpq ranks the connection's own options above its environment
    monkeypatch.delenv("PGSSLROOTCERT", raising=False)
    db = {"PASSWORD": "pw", "OPTIONS": {"options": "-c jit=off", "sslmode": "verify-full", "sslrootcert": "/ca.crt"}}
    child = backup._process_env(tmp_path, db)
    assert child["PGSSLMODE"] == "verify-full" and child["PGSSLROOTCERT"] == "/ca.crt"
    assert "options" not in child and "PGPASSWORD" not in child
    plain = backup._process_env(tmp_path, {"PASSWORD": "pw", "OPTIONS": {"options": "-c jit=off"}})
    assert plain["PGSSLMODE"] == "prefer" and "PGSSLROOTCERT" not in plain


# Session advisory locks: taken in one statement and released in a later one, on the same server session. A
# transaction-pooling PgBouncer could run the unlock on another server connection and leak the lock, so a pooler in front
# of Softwarden must run in SESSION mode, for the web app and the worker alike (docs/CLOUD.md, Connection poolers;
# Cloud deployment review). A new call site fails this test: add it here and check that section still holds.
# pg_advisory_xact_lock (transaction-scoped) is fine under any pooling and isn't listed.
SESSION_ADVISORY_LOCKS = {
    ("core/backup.py", "pg_try_advisory_lock"),  # the installation backup's lock (worker)
    ("core/backup.py", "pg_advisory_unlock"),
    ("notify/channels.py", "pg_try_advisory_lock"),  # graph_slot: Graph mail slots (worker and web requests)
    ("notify/channels.py", "pg_advisory_unlock"),
}
SESSION_LOCK_CALL = re.compile(r"\b(pg_(?:try_)?advisory_(?:lock|lock_shared|unlock|unlock_all)(?:_shared)?)\s*\(")


def test_session_advisory_locks_are_the_known_ones():
    found = set()
    for path in API.rglob("*.py"):
        parts = set(path.relative_to(API).parts)
        if parts & {".venv", "tests", "migrations", "__pycache__"}:
            continue
        for name in SESSION_LOCK_CALL.findall(path.read_text(encoding="utf-8")):
            found.add((path.relative_to(API).as_posix(), name))
    assert found == SESSION_ADVISORY_LOCKS


def test_transaction_pooling_isnt_offered():
    """Dropped in review: the web app takes Graph mail slots too, so no component can run under transaction pooling."""
    assert not hasattr(env, "db_pool_mode")
    chart = (API.parent.parent / "deploy" / "helm" / "softwarden").rglob("*")
    for path in chart:
        if path.is_file() and path.suffix in (".yaml", ".tpl"):
            text = path.read_text(encoding="utf-8")
            assert "poolMode" not in text and "SOFTWARDEN_DB_POOL_MODE" not in text, path


# ---- first-run setup in cloud mode ------------------------------------------------------------------------------------


@pytest.fixture
def cloud_fresh(db, settings, tmp_path):
    """A fresh cloud installation: setup open, nothing created, the token file in a private temporary folder."""
    settings.SOFTWARDEN_CLOUD = True
    settings.SOFTWARDEN_DEPLOYMENT = "cloud"
    settings.SOFTWARDEN_BOOTSTRAP_TOKEN_FILE = str(tmp_path / "bootstrap" / "setup-token")
    BootstrapState.objects.update_or_create(pk=1, defaults={"completed_at": None})
    cache.clear()
    yield
    cache.clear()


def test_cloud_mode_has_no_web_setup(cloud_fresh):
    assert bootstrap.pending() and not bootstrap.web_setup_open()
    assert bootstrap.prepare_token() == "" and not bootstrap.token_path().exists()
    client = Client(enforce_csrf_checks=True)
    assert client.get("/api/v1/setup").json() == {"pending": False}
    assert client.get("/api/v1/auth/options").json()["setup_pending"] is False
    response = client.post(
        "/api/v1/setup",
        data=json.dumps({"token": "x" * 43, "organization_name": "Fabrikam", "timezone": "UTC"}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    )
    assert response.status_code == 409 and "bootstrap_admin --create" in response.json()["detail"]
    assert not Organization.objects.exists() and not get_user_model().objects.exists()


def test_prepare_and_the_token_command_point_at_create(cloud_fresh):
    out = io.StringIO()
    call_command("bootstrap_admin", "--prepare", stdout=out)
    assert "bootstrap_admin --create" in out.getvalue() and not bootstrap.token_path().exists()
    out = io.StringIO()
    call_command("bootstrap_admin", stdout=out)
    assert "no web setup" in out.getvalue() and "/signup" in out.getvalue()


def test_create_makes_platform_staff_in_no_organization(cloud_fresh, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", PASSWORD)
    out = io.StringIO()
    call_command(
        "bootstrap_admin", "--create", "--no-input", "--email", "Ops@Softwarden.test", "--name", "Op Erator", stdout=out
    )
    user = get_user_model().objects.get()
    assert user.email == "ops@softwarden.test" and user.check_password(PASSWORD)
    assert PlatformStaff.objects.filter(user=user).exists()
    assert not Organization.objects.exists() and not Membership.objects.exists()
    assert not bootstrap.pending() and BootstrapState.objects.get().completed_at is not None
    event = PlatformAuditEvent.objects.get()
    assert event.action == "staff.granted" and event.details["via"] == "bootstrap"
    assert PASSWORD not in json.dumps(event.details) and PASSWORD not in out.getvalue()
    # The operator signs in for the platform console (a staff account with no membership).
    response = Client().post(
        "/api/v1/auth/login",
        json.dumps({"email": user.email, "password": PASSWORD}),
        content_type="application/json",
    )
    assert response.status_code == 200
    # Staff exists: a second run creates nothing.
    with pytest.raises(CommandError, match="already has platform staff"):
        call_command("bootstrap_admin", "--create", "--no-input", "--email", "two@x.test", "--name", "Two")
    assert get_user_model().objects.count() == 1


def test_unattended_create_reads_email_and_name_from_the_environment(cloud_fresh, monkeypatch):
    """`--create --no-input` with only environment variables, as a Helm Job runs it."""
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "Ops@Softwarden.test")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_NAME", "Op Erator")
    call_command("bootstrap_admin", "--create", "--no-input", stdout=io.StringIO())
    user = get_user_model().objects.get()
    assert (user.email, user.first_name, user.last_name) == ("ops@softwarden.test", "Op", "Erator")
    assert PlatformStaff.objects.filter(user=user).exists() and not Organization.objects.exists()


def test_unattended_create_with_only_the_password_names_what_is_missing(cloud_fresh, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_NAME", raising=False)
    with pytest.raises(CommandError, match="Nothing was created") as raised:
        call_command("bootstrap_admin", "--create", "--no-input")
    message = str(raised.value)
    assert "--email or BOOTSTRAP_ADMIN_EMAIL" in message and "--name or BOOTSTRAP_ADMIN_NAME" in message
    assert "password" not in message and PASSWORD not in message
    assert not get_user_model().objects.exists() and bootstrap.pending()


def test_create_refuses_organization_options_and_bad_input(cloud_fresh, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", PASSWORD)
    with pytest.raises(CommandError, match="--org-name and --timezone don't apply"):
        call_command(
            "bootstrap_admin", "--create", "--no-input", "--org-name", "Acme", "--email", "a@x.test", "--name", "A"
        )
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "short")
    with pytest.raises(CommandError, match="Nothing was created") as raised:
        call_command("bootstrap_admin", "--create", "--no-input", "--email", "not-an-email", "--name", "A")
    assert "email:" in str(raised.value) and "password:" in str(raised.value)
    assert not get_user_model().objects.exists() and bootstrap.pending()


def test_a_signup_before_the_first_run_doesnt_block_it(cloud_fresh, monkeypatch):
    """Signup is on by default in cloud mode: a customer who signs up first closes setup, but the operator's staff
    account can still be created while no staff exists; once it does, --create is refused."""
    from core.capabilities import create_signup_organization

    customer = get_user_model().objects.create_user(username="early@x.test", email="early@x.test", password=PASSWORD)
    org = create_signup_organization(name="Early bird (synthetic)", timezone="UTC", coordinator=customer)
    Membership.objects.create(user=customer, organization=org, role="admin")
    assert not bootstrap.pending()
    out = io.StringIO()
    call_command("bootstrap_admin", "--prepare", stdout=out)
    assert "bootstrap_admin --create" in out.getvalue()
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", PASSWORD)
    # The customer's address can't be taken over: grant is the way for an existing account.
    with pytest.raises(CommandError, match="already has this email"):
        call_command("bootstrap_admin", "--create", "--no-input", "--email", "EARLY@x.test", "--name", "Op")
    call_command(
        "bootstrap_admin", "--create", "--no-input", "--email", "ops@x.test", "--name", "Op", stdout=io.StringIO()
    )
    staff = PlatformStaff.objects.get()
    assert staff.user.email == "ops@x.test" and not Membership.objects.filter(user=staff.user).exists()
    assert not PlatformStaff.objects.filter(user=customer).exists()
    with pytest.raises(CommandError, match="already has platform staff"):
        call_command("bootstrap_admin", "--create", "--no-input", "--email", "two@x.test", "--name", "Two")
    assert PlatformStaff.objects.count() == 1
    out = io.StringIO()
    call_command("bootstrap_admin", "--prepare", stdout=out)
    assert "closed (platform staff exists)" in out.getvalue()


def test_two_bootstraps_at_once_make_one_staff_account(transactional_db, settings, tmp_path):
    settings.SOFTWARDEN_CLOUD = True
    settings.SOFTWARDEN_BOOTSTRAP_TOKEN_FILE = str(tmp_path / "setup-token")
    BootstrapState.objects.update_or_create(pk=1, defaults={"completed_at": None})
    barrier = threading.Barrier(2)
    outcomes: list = []

    def create(n):
        from django.db import connection

        try:
            barrier.wait(timeout=10)
            admin_services.bootstrap_platform_staff(
                {"email": f"ops{n}@x.test", "name": f"Ops {n}", "password": PASSWORD}
            )
            outcomes.append("created")
        except Conflict:
            outcomes.append("refused")
        except FieldErrors as e:  # pragma: no cover - would be a bug
            outcomes.append(f"field errors {e.errors}")
        finally:
            connection.close()

    threads = [threading.Thread(target=create, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes) == ["created", "refused"]
    assert PlatformStaff.objects.count() == 1 and get_user_model().objects.count() == 1


def test_selfhosted_setup_is_unchanged(db, settings, tmp_path):
    settings.SOFTWARDEN_CLOUD = False
    settings.SOFTWARDEN_BOOTSTRAP_TOKEN_FILE = str(tmp_path / "bootstrap" / "setup-token")
    BootstrapState.objects.update_or_create(pk=1, defaults={"completed_at": None})
    assert bootstrap.web_setup_open() and len(bootstrap.prepare_token()) == bootstrap.TOKEN_LENGTH


# ---- doctor -----------------------------------------------------------------------------------------------------------


def run_doctor():
    out = io.StringIO()
    code: int | str | None = 0
    try:
        call_command("doctor", "--json", stdout=out)
    except SystemExit as exc:
        code = exc.code
    return json.loads(out.getvalue()), code


def check(document, name):
    found = [c for c in document["checks"] if c["name"] == name]
    assert len(found) == 1, (name, document["checks"])
    return found[0]


@pytest.fixture
def cloud(db, settings):
    settings.SOFTWARDEN_CLOUD = True
    settings.SOFTWARDEN_DEPLOYMENT = "cloud"
    settings.SOFTWARDEN_SIGNUP_ENABLED = True
    settings.SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS = False
    settings.SOFTWARDEN_BASE_URL = "https://app.softwarden.example"
    settings.ALLOWED_HOSTS = ["app.softwarden.example"]
    settings.CSRF_TRUSTED_ORIGINS = ["https://app.softwarden.example"]
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.SMTP_HOST = "smtp.example.test"
    settings.TRUSTED_PROXIES = ["10.0.0.0/8"]  # the Helm chart's default (doctor checks one is listed)
    settings.SOFTWARDEN_PLATFORM_TOTP = True  # on by default in cloud mode


def test_selfhosted_doctor_runs_no_cloud_check(db, settings):
    settings.SOFTWARDEN_CLOUD = False
    document, _code = run_doctor()
    assert not CLOUD_NAMES & {c["name"] for c in document["checks"]}


def test_a_ready_cloud_installation_passes_every_cloud_check(cloud, monkeypatch):
    monkeypatch.setattr(doctor, "database_tls", lambda: ("verify-full", True))
    user = get_user_model().objects.create_user(username="ops@x.test", email="ops@x.test", password=PASSWORD)
    staff = PlatformStaff(user=user, totp_confirmed_at=timezone.now())  # its second factor set up
    credentials.slot_set(staff, "totp", totp.new_secret())
    staff.save()
    document, _code = run_doctor()
    for name in CLOUD_NAMES:
        assert check(document, name)["status"] == "ok", check(document, name)
    assert check(document, "cloud: credential keys")["message"] == "From SOFTWARDEN_CREDENTIAL_KEYS."


def test_cloud_requirements_that_are_missing_fail(cloud, settings, monkeypatch, tmp_path):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = False
    settings.SMTP_HOST = ""
    settings.SOFTWARDEN_BASE_URL = "http://app.softwarden.example"
    settings.SOFTWARDEN_SIGNUP_ENABLED = False
    settings.SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS = True
    settings.TRUSTED_PROXIES = []
    settings.SOFTWARDEN_PLATFORM_TOTP = False
    # The key generated into the secrets volume, as Compose's init service leaves it.
    monkeypatch.delenv("SOFTWARDEN_CREDENTIAL_KEYS")
    (tmp_path / "credential_keys").write_text(_fernet_key(), encoding="utf-8")
    monkeypatch.setenv("SOFTWARDEN_SECRETS_DIR", str(tmp_path))
    document, code = run_doctor()
    assert code == 1
    for name in (
        "cloud: email",
        "cloud: credential keys",
        "cloud: base URL",
        "cloud: platform staff",
        "cloud: platform second factor",
    ):
        assert check(document, name)["status"] == "fail", check(document, name)
        assert check(document, name)["fix"]
    for name in ("cloud: signup", "cloud: private service hosts", "cloud: rate limits"):
        assert check(document, name)["status"] == "warn", check(document, name)
    assert "generated into a volume" in check(document, "cloud: credential keys")["message"]
    # The test database has no TLS: `prefer` (libpq's default) and an unencrypted session both fail.
    tls = check(document, "cloud: database TLS")
    assert tls["status"] == "fail" and "POSTGRES_SSLMODE" in tls["fix"]


def _fernet_key() -> str:
    import base64

    return base64.urlsafe_b64encode(b"k" * 32).decode()


def test_database_tls_reads_the_mode_and_the_sessions_encryption(cloud, settings, monkeypatch):
    monkeypatch.delenv("PGSSLMODE", raising=False)
    assert doctor.database_tls() == ("prefer", False)  # the local test server runs without TLS
    monkeypatch.setenv("PGSSLMODE", "require")
    assert doctor.database_tls()[0] == "require"
    options = {**settings.DATABASES["default"]["OPTIONS"], "sslmode": "verify-ca"}
    monkeypatch.setitem(settings.DATABASES["default"], "OPTIONS", options)
    assert doctor.database_tls()[0] == "verify-ca"
    # With no mode set, a `system` root certificate makes libpq 16+ default to verify-full.
    monkeypatch.delenv("PGSSLMODE")
    monkeypatch.setitem(settings.DATABASES["default"], "OPTIONS", {"sslrootcert": "system"})
    assert doctor.database_tls()[0] == "verify-full"
    monkeypatch.setitem(settings.DATABASES["default"], "OPTIONS", {})
    monkeypatch.setenv("PGSSLROOTCERT", "system")
    assert doctor.database_tls()[0] == "verify-full"
    monkeypatch.setenv("PGSSLMODE", "prefer")  # an explicit mode still wins
    assert doctor.database_tls()[0] == "prefer"
    monkeypatch.setattr(doctor, "database_tls", lambda: ("require", False))
    assert doctor.check_cloud_database().status == doctor.FAIL
    monkeypatch.setattr(doctor, "database_tls", lambda: ("prefer", True))
    assert doctor.check_cloud_database().status == doctor.FAIL


def test_a_deactivated_staff_account_doesnt_count(cloud):
    user = get_user_model().objects.create_user(username="gone@x.test", email="gone@x.test", is_active=False)
    PlatformStaff.objects.create(user=user)
    assert doctor.check_cloud_platform_staff().status == doctor.FAIL


def test_cloud_setup_pending_says_create(cloud_fresh):
    found = doctor.check_setup()
    assert found.status == doctor.WARN and "bootstrap_admin --create" in found.fix
    get_user_model().objects.create_user(username="early@x.test", email="early@x.test")  # a signup closed setup
    assert doctor.check_setup().status == doctor.WARN  # still no staff


# ---- the worker's own liveness with several replicas ------------------------------------------------------------------


@pytest.fixture
def liveness(db, tmp_path, monkeypatch):
    """The local heartbeat on, in a temporary file, with a clock the test moves and no schedule but the test's own."""
    from core import jobs, worker

    registry: dict = {}
    monkeypatch.setattr(jobs, "SCHEDULES", registry)
    # keep_once's own connection housekeeping would close the test's transaction-wrapped connection.
    monkeypatch.setattr(worker, "close_old_connections", lambda: None)
    clock = [1000.0]
    monkeypatch.setattr(worker, "_clock", lambda: clock[0])
    path = tmp_path / "heartbeat"
    worker.keep_local_heartbeat(path)
    yield {"path": path, "clock": clock, "registry": registry}
    worker.keep_local_heartbeat(on=False)
    worker._mark_idle()


def test_nothing_touches_the_local_file_outside_the_long_running_command(db, tmp_path, monkeypatch):
    from core import jobs, worker

    monkeypatch.setattr(jobs, "SCHEDULES", {})
    monkeypatch.setattr(worker, "close_old_connections", lambda: None)
    path = tmp_path / "heartbeat"
    assert worker.local_heartbeat_age(path) is None
    worker.tick()
    worker.HeartbeatKeeper().keep_once()
    assert not path.exists() and worker.local_heartbeat_age(path) is None


def test_the_tick_and_an_idle_keeper_touch_the_local_file(liveness):
    from core import worker

    path = liveness["path"]
    worker.beat()
    assert not path.exists()  # a bare database beat doesn't
    worker.tick()
    assert path.exists()
    path.unlink()
    worker.HeartbeatKeeper().keep_once()  # between ticks: the loop is responsive
    assert path.exists()
    age = worker.local_heartbeat_age(path)
    assert age is not None and age < 5


def test_a_long_schedule_stays_live_until_the_bound_then_goes_stale(liveness):
    """A schedule may run for up to an hour (its lease renewed by the keeper), far past the probe's 180 s: the keeper
    keeps this pod's file fresh while the step is within MAIN_LOOP_BOUND, and stops past it, so a hung loop is restarted.
    The clock is moved, not waited for."""
    from core import health, jobs, worker

    path, clock = liveness["path"], liveness["clock"]
    keeper = worker.HeartbeatKeeper()
    seen: dict = {}

    def long_run(now):
        started = clock[0]
        clock[0] = started + health.STALE_AFTER.total_seconds() + 60  # past what the probe allows without the keeper
        path.unlink(missing_ok=True)
        keeper.keep_once()
        seen["within"] = path.exists()
        clock[0] = started + worker.MAIN_LOOP_BOUND.total_seconds() - 1
        path.unlink(missing_ok=True)
        keeper.keep_once()
        seen["at_bound"] = path.exists()
        clock[0] = started + worker.MAIN_LOOP_BOUND.total_seconds() + 1
        path.unlink(missing_ok=True)
        keeper.keep_once()
        seen["past"] = path.exists()
        seen["responsive"] = worker.main_loop_responsive()

    jobs.register_schedule("test.long", long_run, "* * * * *", "A long run")
    worker.tick()
    assert seen == {"within": True, "at_bound": True, "past": False, "responsive": False}
    # The bound: a schedule's ceiling plus one lease, above a default-lane job's lease.
    assert worker.MAIN_LOOP_BOUND == jobs.SCHEDULE_CEILING + jobs.SCHEDULE_LEASE > jobs.DEFAULT_LEASE
    # The tick is over: idle again, and the keeper touches the file.
    assert worker.main_loop_responsive()
    path.unlink(missing_ok=True)
    keeper.keep_once()
    assert path.exists()


def test_an_idle_spell_past_the_interval_goes_stale(liveness, monkeypatch):
    """Between ticks the long-running loop only sleeps for its interval, so a hang there (in run_worker's own loop,
    outside tick()) is unresponsive once the idle spell passes the interval plus IDLE_MARGIN; tick() alone and
    `--once` leave idleness unbounded. The clock is moved, not waited for."""
    from core import worker

    path, clock = liveness["path"], liveness["clock"]
    monkeypatch.setattr(worker, "_idle_bound", None)  # put back after the test, whatever start_main_loop sets
    keeper = worker.HeartbeatKeeper()
    worker.tick()
    clock[0] += 3600  # no loop started: an idle hour is still responsive
    assert worker.main_loop_responsive()

    worker.start_main_loop(60)
    bound = 60 + worker.IDLE_MARGIN.total_seconds()
    clock[0] += bound - 1
    path.unlink(missing_ok=True)
    keeper.keep_once()
    assert worker.main_loop_responsive() and path.exists()
    clock[0] += 2  # past the interval plus the margin: stuck between ticks
    path.unlink(missing_ok=True)
    keeper.keep_once()
    assert not worker.main_loop_responsive() and not path.exists()

    worker.tick()  # the loop ticks again: idle starts over from the tick's end
    path.unlink(missing_ok=True)
    keeper.keep_once()
    assert worker.main_loop_responsive() and path.exists()
    clock[0] += bound + 1
    assert not worker.main_loop_responsive()
    worker.stop_main_loop()
    assert worker.main_loop_responsive()  # the command stopped: unbounded again


def test_the_command_bounds_idleness_by_its_interval(db, monkeypatch):
    """run_worker starts the bound with its own --interval and lifts it when it stops."""
    import signal

    from core import worker
    from core.management.commands import run_worker

    seen: dict = {}
    monkeypatch.setattr(worker, "_idle_bound", None)
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    monkeypatch.setattr(run_worker, "close_old_connections", lambda: None)
    monkeypatch.setattr(worker, "keep_local_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker.HeartbeatKeeper, "start", lambda self: self)
    monkeypatch.setattr(worker.HeartbeatKeeper, "stop", lambda self: None)

    def tick(**kwargs):
        seen["bound"] = worker._idle_bound
        raise KeyboardInterrupt  # end the loop after one tick

    monkeypatch.setattr(worker, "tick", tick)
    with pytest.raises(KeyboardInterrupt):
        call_command("run_worker", "--interval", "15", "--long-jobs", "1", stdout=io.StringIO())
    assert seen["bound"] == 15 + worker.IDLE_MARGIN.total_seconds()
    assert worker._idle_bound is None


def test_each_step_restarts_the_bound(liveness):
    """Two schedules in one tick that each take most of the bound are each within it: the bound is per step."""
    from core import jobs, worker

    path, clock = liveness["path"], liveness["clock"]
    keeper = worker.HeartbeatKeeper()
    seen: list = []

    def step(now):
        clock[0] += worker.MAIN_LOOP_BOUND.total_seconds() - 10
        path.unlink(missing_ok=True)
        keeper.keep_once()
        seen.append(path.exists())

    jobs.register_schedule("test.first", step, "* * * * *", "First")
    jobs.register_schedule("test.second", step, "* * * * *", "Second")
    worker.tick()
    assert seen == [True, True]


def test_the_local_heartbeat_path_is_resolved_when_asked(monkeypatch, tmp_path):
    import tempfile

    from core import worker

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert worker.local_heartbeat_path() == tmp_path / worker.LOCAL_HEARTBEAT_NAME


def test_the_local_check(tmp_path, monkeypatch):
    from core import worker

    monkeypatch.setattr(worker, "local_heartbeat_age", lambda p=None: None)
    with pytest.raises(CommandError, match="never"):
        call_command("run_worker", "--check", "--local", stdout=io.StringIO())
    monkeypatch.setattr(worker, "local_heartbeat_age", lambda p=None: 400.0)
    with pytest.raises(CommandError, match="stale"):
        call_command("run_worker", "--check", "--local", stdout=io.StringIO())
    monkeypatch.setattr(worker, "local_heartbeat_age", lambda p=None: 12.0)
    out = io.StringIO()
    call_command("run_worker", "--check", "--local", stdout=out)  # no database needed
    assert "ok (12 s ago)" in out.getvalue()
    with pytest.raises(CommandError, match="--local goes with --check"):
        call_command("run_worker", "--once", "--local", stdout=io.StringIO())
