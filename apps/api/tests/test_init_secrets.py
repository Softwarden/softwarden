"""Generated secrets (softwarden/init_secrets.py; softwarden/env.py): created once,
never replaced, the environment wins, never a new password for an existing database, and settings read the
environment first, then the generated file."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from softwarden import env, init_secrets

REPO = Path(__file__).resolve().parents[3]
API = REPO / "apps" / "api"
FILES = init_secrets.FILES
POSIX = os.name == "posix"


def _read(directory, name):
    return (directory / name).read_text(encoding="utf-8").strip()


def test_a_fresh_install_gets_all_five(tmp_path):
    secrets_dir = tmp_path / "secrets"
    done = init_secrets.ensure(secrets_dir, {}, pgdata=tmp_path / "pgdata")
    assert len(done) == len(FILES) == 5
    for name in FILES:
        assert len(_read(secrets_dir, name)) >= 24
        if POSIX:
            assert (secrets_dir / name).stat().st_mode & 0o777 == 0o600
    Fernet(_read(secrets_dir, "credential_keys").encode())  # a usable key
    printed = " ".join(done)
    assert not any(_read(secrets_dir, n) in printed for n in FILES)
    assert not list(secrets_dir.glob(".*.tmp"))


def test_a_rerun_changes_nothing(tmp_path):
    init_secrets.ensure(tmp_path, {})
    before = {name: _read(tmp_path, name) for name in FILES}
    assert init_secrets.ensure(tmp_path, {}) == []
    assert {name: _read(tmp_path, name) for name in FILES} == before


def test_values_in_the_environment_win_and_are_not_generated(tmp_path):
    environ = {
        "SOFTWARDEN_SECRET_KEY": "from-env",
        "POSTGRES_PASSWORD": "pg-from-env",
        "SOFTWARDEN_CREDENTIAL_KEYS": Fernet.generate_key().decode(),
    }
    init_secrets.ensure(tmp_path, environ)
    assert not (tmp_path / "django_secret_key").exists()
    assert not (tmp_path / "credential_keys").exists()
    # PostgreSQL reads only the file, so an environment password is copied there; before the database exists a
    # changed value still replaces it.
    assert _read(tmp_path, "postgres_password") == "pg-from-env"
    init_secrets.ensure(tmp_path, {**environ, "POSTGRES_PASSWORD": "changed"}, pgdata=tmp_path / "no-database")
    assert _read(tmp_path, "postgres_password") == "changed"


def test_the_ops_token_is_generated_unless_configured(tmp_path):
    """Observability: /healthz/ops's bearer token. SOFTWARDEN_OPS_TOKEN set means no file; a file is kept."""
    init_secrets.ensure(tmp_path / "a", {"SOFTWARDEN_OPS_TOKEN": "configured-ops-token"})
    assert not (tmp_path / "a" / "ops_token").exists()
    init_secrets.ensure(tmp_path / "b", {})
    token = _read(tmp_path / "b", "ops_token")
    assert len(token) >= 48
    if POSIX:
        assert (tmp_path / "b" / "ops_token").stat().st_mode & 0o777 == 0o600
    assert init_secrets.ensure(tmp_path / "b", {}) == []
    assert _read(tmp_path / "b", "ops_token") == token


def test_the_database_gets_only_its_password_in_a_volume_of_its_own(tmp_path):
    """The db container mounts a copy of postgres_password alone, never the credential, backup
    or Django keys. The copy follows the file, and an install from before the copy gets one on its next start."""
    secrets_dir, db_dir = tmp_path / "secrets", tmp_path / "db-secrets"
    done = init_secrets.ensure(secrets_dir, {}, database_dir=db_dir)
    assert sorted(p.name for p in db_dir.iterdir()) == ["postgres_password"]
    assert _read(db_dir, "postgres_password") == _read(secrets_dir, "postgres_password")
    if POSIX:
        assert (db_dir / "postgres_password").stat().st_mode & 0o777 == 0o600
    assert not any(_read(secrets_dir, n) in " ".join(done) for n in FILES)
    assert init_secrets.ensure(secrets_dir, {}, database_dir=db_dir) == []
    # Before the database exists a new environment password replaces the file, and the copy with it.
    init_secrets.ensure(secrets_dir, {"POSTGRES_PASSWORD": "pg-from-env"}, database_dir=db_dir)
    assert _read(db_dir, "postgres_password") == "pg-from-env"
    # An install from before the copy: the secrets volume has the password, the new volume is empty.
    (db_dir / "postgres_password").unlink()
    assert init_secrets.ensure(secrets_dir, {}, database_dir=db_dir) == [
        "Copied the database password into the database's own secrets volume."
    ]
    assert _read(db_dir, "postgres_password") == "pg-from-env"


def test_never_overwrites_the_password_of_an_existing_database(tmp_path):
    """compose.dev.yaml sets POSTGRES_PASSWORD=softwarden-dev; run after a plain `up` generated a password, it must not
    replace the only copy of the real one (PostgreSQL ignores the file for an existing database)."""
    secrets_dir, pgdata = tmp_path / "secrets", tmp_path / "pgdata"
    init_secrets.ensure(secrets_dir, {}, pgdata=pgdata)  # plain `up`: generated
    generated = _read(secrets_dir, "postgres_password")
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("17\n", encoding="utf-8")  # PostgreSQL created the database with it
    with pytest.raises(init_secrets.SetupError, match="differs"):
        init_secrets.ensure(secrets_dir, {"POSTGRES_PASSWORD": "softwarden-dev"}, pgdata=pgdata)
    assert _read(secrets_dir, "postgres_password") == generated
    # The same value is fine, and without the variable the file is used.
    assert init_secrets.ensure(secrets_dir, {"POSTGRES_PASSWORD": generated}, pgdata=pgdata) == []
    assert init_secrets.ensure(secrets_dir, {}, pgdata=pgdata) == []


def test_dev_password_on_a_fresh_volume_and_on_a_volume_from_an_early_version(tmp_path):
    """The documented dev flow: a new volume, and the existing softwarden-db volume (made with softwarden-dev, no file)."""
    dev = {"POSTGRES_PASSWORD": "softwarden-dev"}
    fresh = tmp_path / "fresh"
    init_secrets.ensure(fresh, dev, pgdata=tmp_path / "fresh-pg")
    assert _read(fresh, "postgres_password") == "softwarden-dev"
    old_pg = tmp_path / "old-pg"
    old_pg.mkdir()
    (old_pg / "PG_VERSION").write_text("17\n", encoding="utf-8")
    existing = tmp_path / "existing"
    init_secrets.ensure(existing, dev, pgdata=old_pg)
    assert _read(existing, "postgres_password") == "softwarden-dev"
    assert init_secrets.ensure(existing, dev, pgdata=old_pg) == []


def test_a_key_file_setting_counts_as_a_key(tmp_path):
    init_secrets.ensure(tmp_path, {"SOFTWARDEN_CREDENTIAL_KEYS_FILE": "/run/secrets/keys"})
    assert not (tmp_path / "credential_keys").exists()


def test_a_backup_key_file_setting_means_no_generated_backup_key(tmp_path):
    """SOFTWARDEN_BACKUP_KEY_FILE names the operator's own key (core/backup.py): nothing is generated for it."""
    init_secrets.ensure(tmp_path, {"SOFTWARDEN_BACKUP_KEY_FILE": "/run/keys/backup.key"})
    assert not (tmp_path / "backup_key").exists()
    init_secrets.ensure(tmp_path, {})
    key = _read(tmp_path, "backup_key")
    assert len(key) >= 48 and key.isprintable()  # openssl -pass file: reads the first line
    assert init_secrets.ensure(tmp_path, {}) == [] and _read(tmp_path, "backup_key") == key  # never replaced


def test_the_legacy_secrets_key_does_not_replace_a_credential_key(tmp_path):
    """SOFTWARDEN_SECRETS_KEY only decrypts (core/credentials.py), so a credential key is still generated."""
    init_secrets.ensure(tmp_path, {"SOFTWARDEN_SECRETS_KEY": Fernet.generate_key().decode()})
    assert (tmp_path / "credential_keys").exists()


def test_never_invents_a_password_for_an_existing_database(tmp_path):
    pgdata = tmp_path / "pgdata"
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("17\n", encoding="utf-8")
    secrets_dir = tmp_path / "secrets"
    with pytest.raises(init_secrets.SetupError, match="POSTGRES_PASSWORD"):
        init_secrets.ensure(secrets_dir, {}, pgdata=pgdata)
    assert not (secrets_dir / "postgres_password").exists()
    # With the password in the environment it starts, and warns that the new credential key is new.
    done = init_secrets.ensure(secrets_dir, {"POSTGRES_PASSWORD": "softwarden-dev"}, pgdata=pgdata)
    assert _read(secrets_dir, "postgres_password") == "softwarden-dev"
    assert any("already existed" in line for line in done)
    # From then on the file is enough.
    assert init_secrets.ensure(secrets_dir, {}, pgdata=pgdata) == []


def test_main_prints_only_what_it_did(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SOFTWARDEN_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("SOFTWARDEN_PGDATA", str(tmp_path / "none"))
    for name in (
        "SOFTWARDEN_SECRET_KEY",
        "POSTGRES_PASSWORD",
        "SOFTWARDEN_CREDENTIAL_KEYS",
        "SOFTWARDEN_BACKUP_KEY_FILE",
        "SOFTWARDEN_OPS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    assert init_secrets.main() == 0
    out = capsys.readouterr().out
    assert out.count("softwarden init:") == 5
    assert not any(_read(tmp_path, n) in out for n in FILES)
    assert init_secrets.main() == 0
    assert "nothing changed" in capsys.readouterr().out


def test_main_fails_with_a_readable_message(tmp_path, capsys, monkeypatch):
    (tmp_path / "pg").mkdir()
    (tmp_path / "pg" / "PG_VERSION").write_text("17\n", encoding="utf-8")
    monkeypatch.setenv("SOFTWARDEN_SECRETS_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SOFTWARDEN_PGDATA", str(tmp_path / "pg"))
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    assert init_secrets.main() == 1
    err = capsys.readouterr().err
    assert "Set POSTGRES_PASSWORD" in err and "softwarden-dev" in err


def test_runs_without_django(tmp_path):
    """The init container runs it before the database exists: no Django import, no settings."""
    code = "import sys, softwarden.init_secrets; sys.exit('django' in sys.modules)"
    environ = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    result = subprocess.run([sys.executable, "-c", code], cwd=API, env=environ, check=False)  # noqa: S603
    assert result.returncode == 0


def test_app_uid_matches_the_image_user():
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    uids = re.findall(r"useradd[^\n]*--uid (\d+)", dockerfile)
    assert uids == [str(init_secrets.APP_UID)]


# ---- settings: environment first, then the generated file ---------------------------------------------------------


def test_settings_read_the_environment_first_then_the_generated_file(tmp_path, monkeypatch):
    (tmp_path / "django_secret_key").write_text("generated\n", encoding="utf-8")
    monkeypatch.setenv("SOFTWARDEN_SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("SOFTWARDEN_SECRET_KEY", raising=False)
    assert env.secret("SOFTWARDEN_SECRET_KEY", "django_secret_key") == "generated"
    monkeypatch.setenv("SOFTWARDEN_SECRET_KEY", "from-env")
    assert env.secret("SOFTWARDEN_SECRET_KEY", "django_secret_key") == "from-env"
    monkeypatch.setenv("SOFTWARDEN_SECRET_KEY", "  ")  # blank counts as unset (compose passes `${X:-}`)
    assert env.secret("SOFTWARDEN_SECRET_KEY", "django_secret_key") == "generated"
    assert env.secret("X_MISSING", "missing", "fallback") == "fallback"


def test_settings_module_uses_generated_secrets(tmp_path):
    """A fresh process with no secret in its environment picks up the generated Django key and database password."""
    init_secrets.ensure(tmp_path, {})
    code = (
        "import django, sys; django.setup(); from django.conf import settings as s; "
        "sys.stdout.write(s.SECRET_KEY + '\\n' + s.DATABASES['default']['PASSWORD'])"
    )
    environ = {
        k: v
        for k, v in os.environ.items()
        if k not in ("SOFTWARDEN_SECRET_KEY", "POSTGRES_PASSWORD", "SOFTWARDEN_DEBUG")
    }
    environ.update(
        SOFTWARDEN_SECRETS_DIR=str(tmp_path), SOFTWARDEN_DEBUG="0", DJANGO_SETTINGS_MODULE="softwarden.settings"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=API, env=environ, capture_output=True, text=True, check=True
    )
    assert result.stdout.splitlines() == [_read(tmp_path, "django_secret_key"), _read(tmp_path, "postgres_password")]


def test_settings_refuse_to_start_without_a_key_when_debug_is_off(tmp_path):
    code = "import django; django.setup()"
    environ = {k: v for k, v in os.environ.items() if k not in ("SOFTWARDEN_SECRET_KEY",)}
    environ.update(
        SOFTWARDEN_SECRETS_DIR=str(tmp_path), SOFTWARDEN_DEBUG="0", DJANGO_SETTINGS_MODULE="softwarden.settings"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=API, env=environ, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0 and "SOFTWARDEN_SECRET_KEY" in result.stderr


def test_debug_is_off_unless_set(tmp_path):
    """With SOFTWARDEN_DEBUG unset (and no key anywhere) the settings are production settings, so
    they refuse the development secret key instead of starting in development mode."""
    code = "import django; django.setup()"
    environ = {k: v for k, v in os.environ.items() if k not in ("SOFTWARDEN_SECRET_KEY", "SOFTWARDEN_DEBUG")}
    environ.update(SOFTWARDEN_SECRETS_DIR=str(tmp_path), DJANGO_SETTINGS_MODULE="softwarden.settings")
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=API, env=environ, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0 and "Set SOFTWARDEN_SECRET_KEY" in result.stderr
    environ["SOFTWARDEN_DEBUG"] = "1"
    started = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=API, env=environ, capture_output=True, text=True, check=False
    )
    assert started.returncode == 0, started.stderr
