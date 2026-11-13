import json
import logging

import pytest
from django.test import Client, override_settings

from softwarden.logs import JsonFormatter


def test_healthz_needs_no_sign_in():
    response = Client().get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_readyz_checks_database_migrations_and_the_worker():
    """/readyz needs a worker heartbeat. Before any worker has ticked it is not ready; /readyz/web (the
    container healthcheck, which the worker waits on) is."""
    response = Client().get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == {
        "database": "ok",
        "migrations": "ok",
        "worker": "not running",
        "schedules": "ok",
    }
    web = Client().get("/readyz/web")
    assert web.status_code == 200
    assert web.json() == {"status": "ok", "checks": {"database": "ok", "migrations": "ok"}}


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["/readyz", "/readyz/web"])
def test_readyz_reports_pending_migrations(monkeypatch, url):
    monkeypatch.setattr("django.db.migrations.executor.MigrationExecutor.migration_plan", lambda self, targets: [1])
    response = Client().get(url)
    assert response.status_code == 503
    assert response.json()["checks"]["migrations"] == "pending"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("states", "worker_state", "status", "summary"),
    [
        (["failing"], "ok", 200, "failing"),
        (["overdue", "failing"], "ok", 503, "overdue"),
        (["stuck", "overdue"], "stale", 503, "stuck"),
    ],
)
def test_readyz_names_no_schedule(monkeypatch, states, worker_state, status, summary):
    """The public probe says the worst state only; names and the heartbeat age are for the System page and
    /healthz/ops."""
    from core import health

    names = [f"secret.schedule_{i}" for i in range(len(states))]
    checks = [{"name": n, "label": f"Label {n}", "state": s} for n, s in zip(names, states, strict=True)]
    monkeypatch.setattr(health, "schedule_checks", lambda now=None: checks)
    worker = {"state": worker_state, "last_tick": None, "age_seconds": 4321, "stale_after_seconds": 180}
    monkeypatch.setattr(health, "worker_check", lambda now=None: worker)
    response = Client().get("/readyz")
    assert response.status_code == status
    body = response.content.decode()
    assert response.json()["checks"]["schedules"] == summary
    assert response.json()["checks"]["worker"] == ("ok" if worker_state == "ok" else "stale")
    assert not any(n in body or f"Label {n}" in body for n in names) and "4321" not in body


@pytest.mark.django_db
def test_the_migration_check_is_kept_for_30_seconds(monkeypatch):
    """A flood of probes costs one migration check per process per window."""
    from django.db.migrations.executor import MigrationExecutor

    from softwarden import ops

    calls = []
    real = MigrationExecutor.migration_plan

    def counted(self, targets, *args, **kwargs):
        calls.append(1)
        return real(self, targets, *args, **kwargs)

    monkeypatch.setattr(MigrationExecutor, "migration_plan", counted)
    clock = [1000.0]
    monkeypatch.setattr(ops.time, "monotonic", lambda: clock[0])
    for _ in range(5):
        assert Client().get("/readyz/web").status_code == 200
    assert len(calls) == 1
    clock[0] += ops.MIGRATION_CHECK_SECONDS + 1
    Client().get("/readyz/web")
    assert len(calls) == 2


def test_spa_routes_get_index_html(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>Softwarden</title>")
    with override_settings(SPA_DIR=tmp_path):
        response = Client().get("/applications/0b6a4f0e-4f8e-4a55-9a59-000000000000")
    assert response.status_code == 200
    assert b"<title>Softwarden</title>" in response.getvalue()


def test_root_files_of_the_build_are_served(tmp_path):
    """public/ files land at the root of the build: the pre-paint theme script (a file, so a `script-src 'self'` CSP
    allows it) and the Inter font licence. WhiteNoise serves them from SPA_DIR (WHITENOISE_ROOT), not the SPA view."""
    (tmp_path / "index.html").write_text("<!doctype html>")
    (tmp_path / "theme-init.js").write_text("document.documentElement.dataset.theme = 'light';")
    (tmp_path / "licenses").mkdir()
    (tmp_path / "licenses" / "inter-OFL.txt").write_text("SIL Open Font License")
    with override_settings(SPA_DIR=tmp_path, WHITENOISE_ROOT=tmp_path):
        client = Client()
        script = client.get("/theme-init.js")
        licence = client.get("/licenses/inter-OFL.txt")
    assert script.status_code == 200
    assert script["Content-Type"].startswith("text/javascript")
    assert b"dataset.theme" in script.getvalue()
    assert licence.status_code == 200
    assert b"SIL Open Font License" in licence.getvalue()
    # The SPA's files are same-origin only; WhiteNoise's default `Access-Control-Allow-Origin: *` is off.
    assert "Access-Control-Allow-Origin" not in script and "Access-Control-Allow-Origin" not in licence


def test_unknown_api_path_is_not_the_spa(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html>")
    with override_settings(SPA_DIR=tmp_path):
        assert Client().get("/api/v1/no-such-thing").status_code == 404


def test_logs_are_one_json_object_per_line():
    record = logging.LogRecord("catalog", logging.WARNING, __file__, 1, "Refused %s", ("delete",), None)
    line = JsonFormatter().format(record)
    assert "\n" not in line
    entry = json.loads(line)
    assert entry["level"] == "WARNING"
    assert entry["logger"] == "catalog"
    assert entry["message"] == "Refused delete"
    assert entry["ts"].endswith("+00:00")
