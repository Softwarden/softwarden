"""Database connection reuse (softwarden/env.py db_conn_max_age): 60 s by default, 0 under DEBUG, or what
SOFTWARDEN_DB_CONN_MAX_AGE says (the Playwright API servers set it to stop exhausting ephemeral ports on Windows)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from softwarden import env

API = Path(__file__).resolve().parents[1]


def test_the_default_is_the_callers(monkeypatch):
    monkeypatch.delenv("SOFTWARDEN_DB_CONN_MAX_AGE", raising=False)
    assert env.db_conn_max_age() == 0
    monkeypatch.setenv("SOFTWARDEN_DB_CONN_MAX_AGE", "  ")
    assert env.db_conn_max_age() == 0


def test_the_environment_sets_the_age(monkeypatch):
    monkeypatch.setenv("SOFTWARDEN_DB_CONN_MAX_AGE", "60")
    assert env.db_conn_max_age() == 60


@pytest.mark.parametrize("raw", ["sixty", "1.5", "-1"])
def test_a_bad_value_is_refused(monkeypatch, raw):
    monkeypatch.setenv("SOFTWARDEN_DB_CONN_MAX_AGE", raw)
    with pytest.raises(ValueError, match="SOFTWARDEN_DB_CONN_MAX_AGE"):
        env.db_conn_max_age()


def _settings(**extra: str) -> list[str]:
    code = (
        "import django, sys; django.setup(); from django.conf import settings as s; "
        "d = s.DATABASES['default']; sys.stdout.write(f\"{d['CONN_MAX_AGE']} {d['CONN_HEALTH_CHECKS']}\")"
    )
    environ = {k: v for k, v in os.environ.items() if k != "SOFTWARDEN_DB_CONN_MAX_AGE"}
    environ.update(DJANGO_SETTINGS_MODULE="softwarden.settings", **extra)
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=API, env=environ, capture_output=True, text=True, check=True
    )
    return result.stdout.split()


def test_settings_read_the_variable():
    assert _settings() == ["0", "False"]
    assert _settings(SOFTWARDEN_DB_CONN_MAX_AGE="60") == ["60", "True"]


@pytest.mark.django_db
def test_sessions_run_without_the_jit_compiler():
    """JIT compilation cost the dashboard's data-quality query about 40 ms per load; every query here is
    short, so the application's sessions turn it off."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SHOW jit")
        assert cursor.fetchone() == ("off",)


def test_jit_switch(monkeypatch):
    """SOFTWARDEN_DB_JIT=on sends no `options` startup parameter, which PgBouncer and other poolers refuse."""
    monkeypatch.delenv("SOFTWARDEN_DB_JIT", raising=False)
    assert env.db_options() == {"options": "-c jit=off"}
    monkeypatch.setenv("SOFTWARDEN_DB_JIT", "off")
    assert env.db_options() == {"options": "-c jit=off"}
    monkeypatch.setenv("SOFTWARDEN_DB_JIT", " ON ")
    assert env.db_options() == {}
    monkeypatch.setenv("SOFTWARDEN_DB_JIT", "maybe")
    with pytest.raises(ValueError, match="SOFTWARDEN_DB_JIT"):
        env.db_options()
