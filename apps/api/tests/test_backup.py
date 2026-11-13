"""The worker's encrypted backups (core/backup.py).

Most tests replace pg_dump, pg_restore and openssl with a small fake (FAKE below) so they run anywhere: the fake
"encrypts" by prefixing `Salted__`, records its argv (one file per call: pg_dump and its openssl run at the same
time, and two processes appending to one file lose lines on Windows), and fails on request. The round trip with
the real openssl, and the full run with the real pg_dump, run when those tools are installed and skip with the reason
otherwise."""

import collections
import io
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client
from django.utils import timezone

from core import backup, health, jobs, worker
from core.models import AuditEvent, BackupState, Job, JobStatus, Organization
from tests.conftest import quiet_schedules

# The fake needs only the standard library. On Windows a virtualenv's python.exe is a launcher that runs the base
# interpreter as a child, so killing it would leave the fake running; the base interpreter is one process.
PYTHON = getattr(sys, "_base_executable", None) or sys.executable
KEY = "k3y-" + "Zq9" * 20  # what the key file holds; must never appear in argv, logs, rows or responses

FAKE = r"""
import json, os, sys, time
ctl, tool, args = sys.argv[1], sys.argv[2], sys.argv[3:]
os.makedirs(os.path.join(ctl, "calls"), exist_ok=True)
with open(os.path.join(ctl, "calls", f"{time.time_ns()}-{os.getpid()}.json"), "x", encoding="utf-8") as log:
    log.write(json.dumps([tool, *args]))
def flag(name):
    return os.path.exists(os.path.join(ctl, name))
def value(option):
    return args[args.index(option) + 1] if option in args else None
def fail():
    path = os.path.join(ctl, "stderr-" + tool)
    sys.stderr.write(open(path, encoding="utf-8").read() if os.path.exists(path) else "fake failure")
    sys.exit(3)
if flag("slow-" + tool):
    time.sleep(60)
if tool == "pg_dump":
    sys.stdout.buffer.write(b"PGDMP" + b"x" * 4096)
    sys.stdout.flush()
    if flag("fail-pg_dump"):
        fail()
elif tool == "pg_restore":
    data = sys.stdin.buffer.read(5)
    if data != b"PGDMP" or flag("fail-pg_restore"):
        fail()
    print(";\n; Archive created at 2026-11-07\n;     TOC Entries: 1")
elif tool == "openssl":
    src, out = value("-in"), value("-out")
    if any(part.endswith("media.tar.enc") for part in (src or "", out or "")) and flag("fail-openssl-media"):
        fail()
    if not any(a.startswith("file:") for a in args):
        fail()
    data = open(src, "rb").read() if src else sys.stdin.buffer.read()
    if "-d" in args:
        if not data.startswith(b"Salted__"):
            sys.stderr.write("bad magic number")
            sys.exit(1)
        data = data[8:]
    else:
        data = b"Salted__" + data
    if out:
        open(out, "wb").write(data)
    else:
        sys.stdout.buffer.write(data)
"""


@pytest.fixture
def ctl(tmp_path):
    path = tmp_path / "ctl"
    path.mkdir()
    return path


@pytest.fixture
def configured(settings, tmp_path, ctl, monkeypatch):
    """Backups configured into tmp_path, with the fake tools."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    key = secrets / "backup_key"
    key.write_text(KEY + "\n", encoding="utf-8")
    key.chmod(0o600)
    settings.BACKUP_DIR = str(tmp_path / "backups")
    settings.BACKUP_KEY_FILE = str(key)
    settings.BACKUP_KEY_FILE_CONFIGURED = False
    settings.BACKUP_MIN_FREE_MB = 0  # the free-space floor has its own tests; the test machine's disk is not ours
    settings.BACKUP_REQUIRE_MOUNT = False  # tmp_path is no mount point
    media = Path(settings.MEDIA_ROOT)
    (media / "attachments" / "org").mkdir(parents=True)
    (media / "attachments" / "org" / "a1").write_bytes(b"file one")
    (media / ".incoming").mkdir()
    (media / ".incoming" / "staged").write_bytes(b"not archived")
    (media / "exports" / "org").mkdir(parents=True)  # organization exports and tenant backups
    (media / "exports" / "org" / "e1.zip").write_bytes(b"not archived either")
    fake = tmp_path / "fake_tool.py"
    fake.write_text(FAKE, encoding="utf-8")
    monkeypatch.setattr(
        backup,
        "tools",
        lambda: {t: [PYTHON, str(fake), str(ctl), t] for t in ("pg_dump", "pg_restore", "openssl")},
    )
    return Path(settings.BACKUP_DIR)


def logged_calls(ctl):
    """Every call the fake recorded, as [tool, *args]. Processes that run at the same time log in no set order."""
    calls = ctl / "calls"
    if not calls.is_dir():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(calls.iterdir())]


def daily_dirs(dest):
    daily = dest / "daily"
    return sorted(p.name for p in daily.iterdir()) if daily.is_dir() else []


def make_set(parent, name, *, sums=True, media=True):
    """A set on disk in the published layout (content is irrelevant to completeness)."""
    d = parent / name
    d.mkdir(parents=True)
    parts = ["dump.enc", "manifest.json.enc"] + (["media.tar.enc"] if media else [])
    for part in parts:
        (d / f"{name}.{part}").write_bytes(b"Salted__x")
    if sums:
        (d / f"{name}.sha256").write_text("".join(f"0  {name}.{p}\n" for p in parts), encoding="utf-8")
    return d


# ---- publishing -----------------------------------------------------------------------------------------------


def test_a_backup_publishes_one_complete_set_in_the_ops_format(db, configured, ctl):
    now = datetime(2026, 11, 4, 2, 30, 5, tzinfo=UTC)  # a Wednesday
    result = backup.run_backup(now=now)
    name = "softwarden-20261104T023005Z"
    assert result["set"] == name and result["weekly"] is False and result["size_bytes"] > 0
    assert daily_dirs(configured) == [name]
    set_dir = configured / "daily" / name
    assert sorted(p.name for p in set_dir.iterdir()) == sorted(
        f"{name}.{p}" for p in ("dump.enc", "manifest.json.enc", "media.tar.enc", "sha256", "sha256.hmac")
    )
    assert backup.is_complete(set_dir)
    if os.name == "posix":  # private files in the private folder (Windows keeps no Unix modes)
        assert {stat.S_IMODE(p.stat().st_mode) for p in set_dir.iterdir()} == {0o600}
    # The checksum file's integrity tag under the backup key (ops/restore.sh checks it).
    sums = (set_dir / f"{name}.sha256").read_bytes()
    tag = (set_dir / f"{name}.sha256.hmac").read_bytes().decode()
    assert tag == f"HMAC-SHA256 {backup.sums_tag(sums, KEY)}\n" and KEY not in tag
    # sha256sum's format (ops/restore.sh runs `sha256sum -c` on it): "<hex>  <name>", dump first.
    lines = (set_dir / f"{name}.sha256").read_text(encoding="utf-8").splitlines()
    assert [line.split("  ")[1] for line in lines] == [
        f"{name}.dump.enc",
        f"{name}.manifest.json.enc",
        f"{name}.media.tar.enc",
    ]
    for line in lines:
        digest, file = line.split("  ")
        assert digest == backup._sha256(set_dir / file)
    # The media archive holds media/ without the restore staging folder.
    media = (set_dir / f"{name}.media.tar.enc").read_bytes()[8:]
    with tarfile.open(fileobj=io.BytesIO(media)) as tar:
        names = tar.getnames()
    assert "media" in names and "media/attachments/org/a1" in names and not any(".incoming" in n for n in names)
    assert not any(n.startswith("media/exports") for n in names)  # exports and tenant backups are rebuilt, not kept
    # The manifest is upgrade_check's.
    manifest = json.loads((set_dir / f"{name}.manifest.json.enc").read_bytes()[8:])
    assert {"written_at", "migrations", "reviews", "audit_events"} <= set(manifest)
    state = BackupState.objects.get()
    assert state.last_set == name and state.last_size_bytes == result["size_bytes"] and state.last_success_at


def test_order_is_manifest_then_dump_then_media(db, configured, monkeypatch):
    # The order in which the backup starts its processes, recorded here: pg_dump and its openssl run at the same time,
    # so the order in which the fakes log themselves is the scheduler's.
    calls = []
    start = backup.Runner.start

    def recording(self, argv, label, **kwargs):
        calls.append(argv[3:])  # [PYTHON, fake, ctl, tool, *args] -> [tool, *args]
        return start(self, argv, label, **kwargs)

    monkeypatch.setattr(backup.Runner, "start", recording)
    backup.run_backup()
    steps = []
    for call in calls:
        if call[0] == "pg_dump":
            steps.append("dump")
        elif call[0] == "openssl" and "-d" not in call:
            out = Path(call[call.index("-out") + 1]).name
            steps.append(next(part for part in backup.PARTS if out.endswith(part)))
    assert steps == ["manifest.json.enc", "dump", "dump.enc", "media.tar.enc"]


@pytest.mark.parametrize("failing", ["fail-pg_dump", "fail-pg_restore", "fail-openssl-media"])
def test_a_failure_publishes_nothing_and_is_recorded(db, configured, ctl, failing):
    (ctl / failing).write_text("", encoding="utf-8")
    with pytest.raises(backup.BackupError):
        backup.run_backup()
    assert daily_dirs(configured) == []  # no set, no in-progress directory left
    state = BackupState.objects.get()
    assert state.last_success_at is None and state.last_failure_at and "exited with status" in state.last_failure


def test_a_failure_writing_the_sums_publishes_nothing_and_shows_only_the_stage(db, configured, monkeypatch):
    def broken(set_dir, name):
        raise OSError(28, "No space left on device: /orga/private/path")

    monkeypatch.setattr(backup, "_write_sums", broken)
    with pytest.raises(backup.BackupError):
        backup.run_backup()
    assert daily_dirs(configured) == []
    failure = BackupState.objects.get().last_failure
    assert failure == "The backup failed while writing the checksums (OSError)."


def test_leftovers_of_a_crashed_run_are_cleared(db, configured):
    leftover = configured / "daily" / ".in-progress-deadbeef"
    leftover.mkdir(parents=True)
    (leftover / "half.dump.enc").write_bytes(b"x")
    backup.run_backup()
    assert not leftover.exists() and len(daily_dirs(configured)) == 1


def test_same_second_runs_get_distinct_names(db, configured):
    now = datetime(2026, 11, 4, 2, 30, 0, tzinfo=UTC)
    first = backup.run_backup(now=now)["set"]
    second = backup.run_backup(now=now)["set"]
    third = backup.run_backup(now=now)["set"]
    assert (first, second, third) == (
        "softwarden-20261104T023000Z",
        "softwarden-20261104T023000Z-2",
        "softwarden-20261104T023000Z-3",
    )
    assert [p.name for p in backup.complete_sets(configured / "daily")] == [first, second, third]


def test_a_sunday_set_is_also_kept_as_weekly(db, configured):
    sunday = datetime(2026, 11, 8, 2, 30, tzinfo=UTC)
    result = backup.run_backup(now=sunday)
    assert result["weekly"] is True
    weekly = configured / "weekly" / result["set"]
    assert backup.is_complete(weekly)
    assert sorted(p.name for p in weekly.iterdir()) == sorted(
        p.name for p in (configured / "daily" / result["set"]).iterdir()
    )


def test_a_key_readable_by_others_is_refused(db, configured, settings):
    if os.name != "posix":
        pytest.skip("file modes are POSIX-only; Windows has no group/other bits to check")
    Path(settings.BACKUP_KEY_FILE).chmod(0o644)
    with pytest.raises(backup.BackupError, match="chmod 600"):
        backup.run_backup()


def test_a_missing_key_fails_with_a_readable_message(db, configured, settings):
    Path(settings.BACKUP_KEY_FILE).unlink()
    with pytest.raises(backup.BackupError, match="key file .* is missing"):
        backup.run_backup()
    assert daily_dirs(configured) == []


def test_not_configured_does_nothing(db, settings):
    settings.BACKUP_DIR = ""
    with pytest.raises(backup.NotConfigured):
        backup.run_backup()
    assert not BackupState.objects.exists()
    assert backup.scheduled(timezone.now()) == {"skipped": "not configured"}
    assert not Job.objects.filter(kind=backup.JOB_KIND).exists()


# ---- completeness and retention ---------------------------------------------------------------------------------


def test_a_set_is_complete_only_with_a_sums_file_listing_every_part(tmp_path):
    assert backup.is_complete(make_set(tmp_path, "softwarden-20261101T023000Z"))
    assert not backup.is_complete(make_set(tmp_path, "softwarden-20261102T023000Z", sums=False))
    partial = make_set(tmp_path, "softwarden-20261103T023000Z")
    sums = partial / "softwarden-20261103T023000Z.sha256"
    sums.write_text("0  softwarden-20261103T023000Z.dump.enc\n", encoding="utf-8")  # media and manifest unlisted
    assert not backup.is_complete(partial)
    missing = make_set(tmp_path, "softwarden-20261104T023000Z")
    (missing / "softwarden-20261104T023000Z.media.tar.enc").unlink()  # listed but gone
    assert not backup.is_complete(missing)
    assert not backup.is_complete(make_set(tmp_path, "not-a-set"))
    assert [p.name for p in backup.complete_sets(tmp_path)] == ["softwarden-20261101T023000Z"]


def test_prune_keeps_the_newest(tmp_path):
    names = [f"softwarden-202611{day:02d}T023000Z" for day in range(1, 8)]
    for name in names:
        make_set(tmp_path, name)
    make_set(tmp_path, "softwarden-20261101T023000Z-2")
    newer_incomplete = make_set(tmp_path, "softwarden-20261130T023000Z", sums=False)
    removed = backup.prune(tmp_path, 3)
    assert sorted(removed) == sorted([*names[:4], "softwarden-20261101T023000Z-2"])
    assert [p.name for p in backup.complete_sets(tmp_path)] == names[4:]
    assert newer_incomplete.exists()  # never counted, never deleted
    assert backup.prune(tmp_path, 0) == names[4:6]  # at least the newest complete set is always kept
    assert [p.name for p in backup.complete_sets(tmp_path)] == [names[-1]]


def test_retention_runs_after_each_backup(db, configured, settings):
    settings.BACKUP_KEEP_DAILY = 2
    for name in ("softwarden-20261101T023000Z", "softwarden-20261102T023000Z", "softwarden-20261103T023000Z"):
        make_set(configured / "daily", name)
    result = backup.run_backup(now=datetime(2026, 11, 4, 2, 30, tzinfo=UTC))
    assert result["pruned"] == 2
    assert daily_dirs(configured) == ["softwarden-20261103T023000Z", result["set"]]


# ---- one at a time ------------------------------------------------------------------------------------------------


def other_session():
    s = connection.settings_dict
    return psycopg.connect(
        host=s["HOST"] or None, port=s["PORT"] or None, dbname=s["NAME"], user=s["USER"], password=s["PASSWORD"]
    )


def test_only_one_backup_runs_at_a_time(db, configured):
    with other_session() as other:
        other.execute("SELECT pg_advisory_lock(%s)", [backup.LOCK_KEY])
        with pytest.raises(backup.BackupBusy):
            backup.run_backup()
        job = jobs.enqueue(backup.JOB_KIND, {"trigger": "test"})
        claimed = jobs.claim(lane=jobs.LONG_LANE)
        assert claimed.id == job.id
        assert jobs.run(claimed) == JobStatus.FAILED  # permanent: retrying can't help a run already in progress
        other.execute("SELECT pg_advisory_unlock(%s)", [backup.LOCK_KEY])
    assert daily_dirs(configured) == []
    assert not BackupState.objects.filter(last_failure_at__isnull=False).exists()  # a refusal isn't a failed backup
    assert backup.run_backup()["set"]  # the lock was released


def test_the_schedule_queues_one_job_at_a_time(db, configured):
    assert backup.scheduled(timezone.now()) == {"queued": 1}
    assert backup.scheduled(timezone.now()) == {"skipped": "a backup is already queued or running"}
    job = Job.objects.get(kind=backup.JOB_KIND)
    assert job.organization_id is None and job.payload == {"trigger": "schedule"}


# ---- the job, the command and secrets ------------------------------------------------------------------------------


def test_the_schedule_and_job_are_registered():
    definition = jobs.SCHEDULES[backup.SCHEDULE]
    assert definition.cron == "30 2 * * *" and definition.func is backup.scheduled
    kind = jobs.JOB_KINDS[backup.JOB_KIND]
    assert kind.lane == jobs.LONG_LANE and kind.max_attempts == 1 and kind.lease == backup.LEASE
    order = list(jobs.SCHEDULES)
    assert order.index(backup.SCHEDULE) < order.index("jobs.prune")


def test_the_cron_comes_from_the_environment():
    code = (
        "import django, os; os.environ['SOFTWARDEN_BACKUP_CRON'] = '15 1 * * *'; django.setup(); "
        "from core import jobs; print(jobs.SCHEDULES['backup.nightly'].cron)"
    )
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "softwarden.settings"}
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True
    )
    assert out.stdout.strip() == "15 1 * * *", out.stderr[-500:]


def test_the_job_records_size_and_duration_and_never_the_key(db, configured, ctl, caplog):
    caplog.set_level(logging.DEBUG)
    worker.tick()  # the worker's own tick doesn't run long jobs: claim it on the long lane as the worker's lane does
    backup.scheduled(timezone.now())
    job = jobs.claim(lane=jobs.LONG_LANE)
    assert job.kind == backup.JOB_KIND
    assert jobs.run(job) == JobStatus.SUCCEEDED
    job.refresh_from_db()
    assert set(job.result) == {"set", "size_bytes", "duration_seconds", "media_files", "weekly", "pruned"}
    assert job.result["size_bytes"] > 0 and job.result["media_files"] == 1
    argv = json.dumps(logged_calls(ctl))
    assert "file:" in argv and KEY not in argv  # the key goes by file, never by value
    rows = json.dumps(list(Job.objects.values()), default=str) + json.dumps(
        list(BackupState.objects.values()), default=str
    )
    assert KEY not in rows and KEY not in caplog.text


def test_a_failure_echoing_the_key_is_redacted(db, configured, ctl, caplog):
    caplog.set_level(logging.DEBUG)
    (ctl / "fail-pg_dump").write_text("", encoding="utf-8")
    (ctl / "stderr-pg_dump").write_text(f"connection failed for key {KEY}", encoding="utf-8")
    job = jobs.enqueue(backup.JOB_KIND, {"trigger": "test"})
    assert jobs.run(jobs.claim(lane=jobs.LONG_LANE)) == JobStatus.FAILED
    job.refresh_from_db()
    state = BackupState.objects.get()
    # Stored and shown: the stage and the exit status only. The tool's output goes to the log, redacted.
    assert state.last_failure == "pg_dump exited with status 3." and "pg_dump exited with status 3" in job.last_error
    assert "connection failed for key [redacted]" in caplog.text
    assert KEY not in job.last_error + state.last_failure + caplog.text


def test_pg_dump_gets_the_password_by_file_not_argv_or_environment(db, configured, monkeypatch):
    seen = []
    real = subprocess.Popen

    def spy(argv, *args, **kwargs):
        seen.append((list(argv), dict(kwargs.get("env") or {})))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    backup.run_backup()
    password = connection.settings_dict["PASSWORD"]
    dump_argv, dump_env = next((a, e) for a, e in seen if "pg_dump" in a)
    assert password not in " ".join(dump_argv) and "PGPASSWORD" not in dump_env
    assert set(dump_env) <= {
        "PGPASSFILE",
        "PGCONNECT_TIMEOUT",
        "LC_ALL",
        "PATH",
        "SYSTEMROOT",
        "SystemRoot",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
    }
    assert not Path(dump_env["PGPASSFILE"]).exists()  # removed with the scratch directory
    for argv, env in seen:
        assert KEY not in " ".join(argv) and KEY not in " ".join(env.values())


def test_libpq_tls_settings_reach_pg_dump_and_nothing_else_does(db, configured, monkeypatch):
    """An external PostgreSQL that requires TLS (Helm, PGSSLMODE=require): pg_dump and pg_restore get libpq's TLS
    settings, as the app's own connection does; other inherited variables still don't reach them."""
    monkeypatch.setenv("PGSSLMODE", "verify-full")
    monkeypatch.setenv("PGSSLROOTCERT", "/etc/softwarden/pg-root.crt")
    monkeypatch.setenv("PGSSLCERT", "/etc/softwarden/client.crt")
    monkeypatch.setenv("PGSSLKEY", "/etc/softwarden/client.key")
    monkeypatch.setenv("SOFTWARDEN_SMTP_PASSWORD", "not-for-children")
    seen = []
    real = subprocess.Popen

    def spy(argv, *args, **kwargs):
        seen.append((list(argv), dict(kwargs.get("env") or {})))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    backup.run_backup()
    for tool in ("pg_dump", "pg_restore"):
        env = next(e for a, e in seen if tool in a)
        assert env["PGSSLMODE"] == "verify-full" and env["PGSSLROOTCERT"] == "/etc/softwarden/pg-root.crt", tool
        assert env["PGSSLCERT"] == "/etc/softwarden/client.crt" and env["PGSSLKEY"] == "/etc/softwarden/client.key"
        assert "SOFTWARDEN_SMTP_PASSWORD" not in env


def test_backup_now_inline_and_queued(db, configured, capsys):
    call_command("backup_now", "--inline")
    assert "published" in capsys.readouterr().out
    call_command("backup_now", "--no-wait")
    assert "Queued backup job" in capsys.readouterr().out
    call_command("backup_now", "--no-wait")
    assert "Joined backup job" in capsys.readouterr().out
    job = jobs.claim(lane=jobs.LONG_LANE)
    jobs.run(job)
    call_command("backup_now", "--no-wait")  # a finished job isn't joined
    assert "Queued" in capsys.readouterr().out


def test_backup_now_reports_failure(db, configured, ctl, settings):
    (ctl / "fail-pg_dump").write_text("", encoding="utf-8")
    with pytest.raises(CommandError, match="Backup failed"):
        call_command("backup_now", "--inline")
    settings.BACKUP_DIR = ""
    with pytest.raises(CommandError, match="aren't configured"):
        call_command("backup_now", "--inline")


# ---- status, warnings and the key acknowledgement ----------------------------------------------------------------


def warning_codes(now=None):
    return [w["code"] for w in health.status(now)["warnings"]]


def test_the_stale_warning(db, configured):
    now = timezone.now()
    assert backup.status(now)["state"] == "never" and "backup_stale" in warning_codes(now)
    BackupState.objects.create(last_success_at=now - timedelta(hours=25), last_set="softwarden-x")
    assert backup.status(now)["state"] == "ok" and "backup_stale" not in warning_codes(now)
    BackupState.objects.filter(pk=1).update(last_success_at=now - timedelta(hours=27))
    block = backup.status(now)
    assert block["state"] == "stale" and block["age_seconds"] == 27 * 3600
    stale = next(w for w in health.status(now)["warnings"] if w["code"] == "backup_stale")
    assert "27 hours" in stale["message"]


def test_a_newer_failure_warns(db, configured):
    now = timezone.now()
    BackupState.objects.create(last_success_at=now - timedelta(hours=1), last_failure_at=now - timedelta(hours=2))
    assert "backup_failed" not in warning_codes(now)
    BackupState.objects.filter(pk=1).update(last_failure_at=now, last_failure="BackupError: pg_dump exited")
    assert "backup_failed" in warning_codes(now)


def test_no_warnings_when_not_configured(db, settings):
    settings.BACKUP_DIR = ""
    assert backup.status()["state"] == "not_configured" and warning_codes() == []


@pytest.mark.parametrize("clock", [None, "02:30:00", "02:30:30"], indirect=True)
def test_readyz_ignores_backups(db, configured, clock):
    quiet_schedules(only=["backup.nightly"])  # at 02:30 the tick would start the nightly backup
    worker.tick()
    assert "backup_stale" in warning_codes()
    response = Client().get("/readyz")
    assert response.status_code == 200 and "backup" not in response.json()["checks"]


def test_system_shows_the_backup_block_to_admins(a, api, configured):
    now = timezone.now()
    BackupState.objects.create(
        last_success_at=now - timedelta(hours=2), last_set="softwarden-20261107T023000Z", last_size_bytes=12345
    )
    body = api(a["users"]["admin"]).get("system").json_body
    block = body["backup"]
    assert block["state"] == "ok" and block["last_set"] == "softwarden-20261107T023000Z"
    assert block["last_size_bytes"] == 12345 and 7100 <= block["age_seconds"] <= 7300
    assert block["destination"] == str(configured) and block["cron"] == "30 2 * * *"
    assert block["stale_after_seconds"] == 26 * 3600 and block["running"] is False
    assert block["key_source"] == "generated" and block["key_present"] is True and block["key_acknowledged"] is False
    assert [w["code"] for w in body["warnings"]] == ["backup_key_unacknowledged"]
    assert body["warnings"][0]["message"] == "Backup key is stored on this host: copy it somewhere safe."
    assert KEY not in json.dumps(body) and backup.key_fingerprint() not in json.dumps(body)


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_acknowledging_the_key_is_admin_only(a, api, configured, role):
    assert api(a["users"][role]).post("system/backup/acknowledge-key").status_code == 403
    assert api().post("system/backup/acknowledge-key").status_code in (401, 403)
    assert not BackupState.objects.filter(key_acknowledged_at__isnull=False).exists()


def test_acknowledging_the_key_is_audited_once_per_key(a, b, api, configured, settings):
    admin = a["users"]["admin"]
    response = api(admin).post("system/backup/acknowledge-key")
    assert response.status_code == 200 and response.json_body["key_acknowledged"] is True
    # The key is installation-wide: the event is in every organization, naming none of them. Only the Admin's own
    # organization names them; the others record no actor and `via` (backups nit).
    events = AuditEvent.objects.filter(action="backup.key_acknowledged")
    assert {e.organization_id for e in events} == {a["org"].id, b["org"].id} and events.count() == 2
    ours, theirs = events.get(organization=a["org"]), events.get(organization=b["org"])
    assert ours.actor_id == admin.id and "via" not in ours.changes
    assert theirs.actor_id is None and theirs.changes["via"] == "another_admin"
    for event in events:
        assert event.changes.get("key_source") == "generated"
        stored = json.dumps([event.changes, event.prechange, event.postchange], default=str)
        assert KEY not in stored and backup.key_fingerprint() not in stored
        assert a["org"].slug not in stored and b["org"].slug not in stored
    assert "backup_key_unacknowledged" not in warning_codes()
    # Again for the same key: nothing new.
    assert api(admin).post("system/backup/acknowledge-key").status_code == 200
    assert AuditEvent.objects.filter(action="backup.key_acknowledged").count() == 2
    # A new key shows the warning again.
    Path(settings.BACKUP_KEY_FILE).write_text("another-key-" + "y" * 40 + "\n", encoding="utf-8")
    assert "backup_key_unacknowledged" in warning_codes()
    # b is one organization among others: its Admin can't acknowledge the installation's key; staff can.
    response = api(b["users"]["admin"]).post("system/backup/acknowledge-key")
    assert response.status_code == 403 and "backup_key_unacknowledged" in warning_codes()
    from tests.test_platform import make_staff

    staff_admin = make_staff(org=b["org"])
    assert api(staff_admin).post("system/backup/acknowledge-key").status_code == 200
    assert AuditEvent.objects.filter(action="backup.key_acknowledged", organization=b["org"]).count() == 2


def test_another_organizations_admin_cannot_acknowledge_the_key(a, b, api, configured):
    """With two organizations, the second one's Admin gets 403 and nothing changes."""
    response = api(b["users"]["admin"]).post("system/backup/acknowledge-key")
    assert response.status_code == 403 and response.json_body["type"] == "/problems/forbidden"
    assert not BackupState.objects.filter(key_acknowledged_at__isnull=False).exists()
    assert not AuditEvent.objects.filter(action="backup.key_acknowledged").exists()
    body = api(b["users"]["admin"]).get("system").json_body
    assert "backup" not in body and "backup_key_unacknowledged" not in json.dumps(body)


def test_platform_staff_acknowledge_the_key_from_the_console(a, b, api, configured):
    from core.models import PlatformAuditEvent
    from tests.test_platform import make_staff

    staff = make_staff()  # in no organization
    assert api(a["users"]["admin"]).post("platform/system/backup/acknowledge-key").status_code == 403
    response = api(staff).post("platform/system/backup/acknowledge-key")
    assert response.status_code == 200 and response.json_body["key_acknowledged"] is True
    events = AuditEvent.objects.filter(action="backup.key_acknowledged")
    assert {e.organization_id for e in events} == {a["org"].id, b["org"].id}
    assert all(e.actor_id is None and e.changes["via"] == "platform" for e in events)
    platform = PlatformAuditEvent.objects.get(action="backup.key_acknowledged")
    assert platform.actor_id == staff.id and KEY not in json.dumps(platform.details)
    assert api(staff).post("platform/system/backup/acknowledge-key").status_code == 200  # again: nothing new
    assert PlatformAuditEvent.objects.filter(action="backup.key_acknowledged").count() == 1


def test_a_configured_key_needs_no_acknowledgement(a, api, configured, settings):
    settings.BACKUP_KEY_FILE_CONFIGURED = True
    assert backup.status()["key_acknowledged"] is True and "backup_key_unacknowledged" not in warning_codes()
    response = api(a["users"]["admin"]).post("system/backup/acknowledge-key")
    assert response.status_code == 409 and not AuditEvent.objects.filter(action="backup.key_acknowledged").exists()


def test_a_missing_key_warns_and_cannot_be_acknowledged(a, api, configured, settings):
    Path(settings.BACKUP_KEY_FILE).unlink()
    assert "backup_key_missing" in warning_codes()
    assert api(a["users"]["admin"]).post("system/backup/acknowledge-key").status_code == 409


def test_the_backup_block_is_documented(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    status = doc["components"]["schemas"]["SystemStatus"]["properties"]
    assert {"backup", "warnings"} <= set(status)
    block = doc["components"]["schemas"]["BackupStatus"]["properties"]
    assert {"state", "last_success_at", "age_seconds", "last_size_bytes", "last_set", "last_failure"} <= set(block)
    assert {"key_acknowledged", "destination"} <= set(block)
    post = doc["paths"]["/api/v1/system/backup/acknowledge-key"]["post"]
    assert {"200", "403", "409"} <= set(post["responses"])


# ---- with the real tools, when installed ---------------------------------------------------------------------------


def find_openssl():
    found = shutil.which("openssl")
    if found:
        return found
    for candidate in (r"C:\Program Files\Git\usr\bin\openssl.exe", r"C:\Program Files\Git\mingw64\bin\openssl.exe"):
        if Path(candidate).is_file():
            return candidate
    return None


def test_encrypt_decrypt_round_trip_with_real_openssl(db, configured, settings, monkeypatch, tmp_path):
    openssl = find_openssl()
    if openssl is None:
        pytest.skip("openssl isn't installed here; the image installs it (Dockerfile)")
    fake_tools = backup.tools()
    monkeypatch.setattr(backup, "tools", lambda: {**fake_tools, "openssl": [openssl]})
    result = backup.run_backup()
    set_dir = configured / "daily" / result["set"]
    key = Path(settings.BACKUP_KEY_FILE)
    # Decrypt exactly as ops/common.sh does.
    for part, check in (("dump.enc", lambda b: b.startswith(b"PGDMP")), ("media.tar.enc", None)):
        out = tmp_path / f"plain-{part}"
        subprocess.run(  # noqa: S603
            [openssl, "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass", f"file:{key}"]
            + ["-in", str(set_dir / f"{result['set']}.{part}"), "-out", str(out)],
            check=True,
        )
        data = out.read_bytes()
        if check:
            assert check(data)
        else:
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                assert "media/attachments/org/a1" in tar.getnames()
    assert (set_dir / f"{result['set']}.dump.enc").read_bytes().startswith(b"Salted__")
    # The wrong key doesn't decrypt.
    wrong = tmp_path / "wrong.key"
    wrong.write_text("not-the-key\n", encoding="utf-8")
    refused = subprocess.run(  # noqa: S603
        [openssl, "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass", f"file:{wrong}"]
        + ["-in", str(set_dir / f"{result['set']}.media.tar.enc"), "-out", str(tmp_path / "x")],
        capture_output=True,
    )
    assert refused.returncode != 0


def cannot_run_here(reason):
    """Skip locally; fail in CI (the CI variable is set), whose api job installs postgresql-client-17, so the real-tool
    path can't go untested there."""
    if os.environ.get("CI"):
        pytest.fail(f"The real pg_dump backup can't run in CI: {reason}.")
    pytest.skip(reason)


def test_a_full_backup_with_real_pg_dump(db, configured, settings, monkeypatch, tmp_path):
    """core/backup.py with the real pg_dump, pg_restore and openssl: the set publishes, and the dump in it decrypts
    exactly as ops/common.sh does and lists back through pg_restore with this database's tables."""
    openssl, pg_dump, pg_restore = find_openssl(), shutil.which("pg_dump"), shutil.which("pg_restore")
    if not (openssl and pg_dump and pg_restore):
        cannot_run_here("pg_dump, pg_restore or openssl isn't installed here; the image installs postgresql-client-17")
    assert openssl and pg_dump and pg_restore
    version = subprocess.run([pg_dump, "--version"], capture_output=True, text=True, check=False).stdout  # noqa: S603
    found = re.search(r"(\d+)\.", version)
    client = int(found.group(1)) if found else 0
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version_num")
        (number,) = cursor.fetchone()
    server = int(number) // 10000
    if client < server:
        cannot_run_here(f"pg_dump {client} can't dump a PostgreSQL {server} server; the image installs the 17 client")
    monkeypatch.setattr(
        backup, "tools", lambda: {"pg_dump": [pg_dump], "pg_restore": [pg_restore], "openssl": [openssl]}
    )
    result = backup.run_backup()
    set_dir = configured / "daily" / result["set"]
    assert backup.is_complete(set_dir) and result["size_bytes"] > 0
    plain = tmp_path / "plain.dump"
    subprocess.run(  # noqa: S603
        [
            openssl,
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "200000",
            "-pass",
            f"file:{settings.BACKUP_KEY_FILE}",
        ]
        + ["-in", str(set_dir / f"{result['set']}.dump.enc"), "-out", str(plain)],
        check=True,
    )
    listing = subprocess.run(  # noqa: S603
        [pg_restore, "--list", str(plain)], capture_output=True, text=True, check=True
    ).stdout
    assert "TABLE public core_organization" in listing and "TABLE DATA public catalog_resource" in listing


# ---- disk safety, mounts, aborts, weekly and pruning ----------------------------------------------------------------


Usage = collections.namedtuple("Usage", "total used free")


def test_a_backup_refuses_when_it_would_fill_the_disk(db, configured, settings, monkeypatch):
    settings.BACKUP_MIN_FREE_MB = None  # default floor: the larger of 2 GB and 10 % of the disk
    monkeypatch.setattr(backup, "estimate_bytes", lambda: 3 * backup.GB)
    monkeypatch.setattr(backup, "disk_usage", lambda path: Usage(100 * backup.GB, 88 * backup.GB, 12 * backup.GB))
    with pytest.raises(backup.BackupError, match="Not enough free space"):
        backup.run_backup()  # 12 - 3 = 9 GB left, under the 10 GB floor
    assert daily_dirs(configured) == [] and logged_calls(configured.parent / "ctl") == []  # no tool ran
    assert "Not enough free space" in BackupState.objects.get().last_failure
    half = backup.GB // 2
    monkeypatch.setattr(backup, "disk_usage", lambda path: Usage(10 * backup.GB, 5 * backup.GB, 4 * backup.GB + half))
    with pytest.raises(backup.BackupError):
        backup.run_backup()  # 4.5 - 3 = 1.5 GB left, under the 2 GB minimum floor
    monkeypatch.setattr(backup, "disk_usage", lambda path: Usage(10 * backup.GB, 4 * backup.GB, 6 * backup.GB))
    assert backup.run_backup()["set"]  # 6 - 3 = 3 GB left, above max(2 GB, 1 GB)
    settings.BACKUP_MIN_FREE_MB = 4096
    with pytest.raises(backup.BackupError, match="SOFTWARDEN_BACKUP_MIN_FREE_MB"):
        backup.run_backup()


def test_the_space_estimate(db, configured):
    def database_size():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_database_size(current_database())")
            return cursor.fetchone()[0]

    # The database's size can move between two reads in a long full run (e.g. the stats collector), so the estimate
    # is checked against sizes read just before and just after it.
    before = database_size()
    first = backup.estimate_bytes()  # no set yet: the database plus the media files the archive takes
    after = database_size()
    media = len(b"file one")  # not .incoming (restore staging) nor exports/ (rebuilt): neither is archived
    assert min(before, after) + media <= first <= max(before, after) + media
    BackupState.objects.create(last_size_bytes=1000)
    assert backup.estimate_bytes() == 1200


def test_a_destination_that_is_not_a_mount_is_refused(db, configured, settings):
    settings.BACKUP_REQUIRE_MOUNT = True
    with pytest.raises(backup.BackupError, match="isn't a mounted volume"):
        backup.run_backup()
    assert daily_dirs(configured) == [] and "isn't a mounted volume" in BackupState.objects.get().last_failure
    BackupState.objects.update(last_failure="", last_failure_at=None)
    with pytest.raises(CommandError, match="isn't a mounted volume"):
        call_command("backup_now", "--inline")  # e.g. in the app container, which has no backup volume
    # Refused before a run: not a failed backup (backups nit), so the System page shows no failure for it.
    assert BackupState.objects.get().last_failure_at is None


def test_backup_now_inline_redacts_any_error(db, configured, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError(f"unexpected with {KEY}")

    monkeypatch.setattr(backup, "run_backup", boom)
    with pytest.raises(CommandError) as raised:
        call_command("backup_now", "--inline")
    assert KEY not in str(raised.value) and "[redacted]" in str(raised.value)


def test_a_weekly_set_survives_daily_pruning(db, configured, settings):
    settings.BACKUP_KEEP_DAILY = 1
    sunday = backup.run_backup(now=datetime(2026, 11, 8, 2, 30, tzinfo=UTC))["set"]
    backup.run_backup(now=datetime(2026, 11, 9, 2, 30, tzinfo=UTC))
    backup.run_backup(now=datetime(2026, 11, 10, 2, 30, tzinfo=UTC))
    assert daily_dirs(configured) == ["softwarden-20261110T023000Z"]
    weekly = configured / "weekly" / sunday
    assert backup.is_complete(weekly)
    for line in (weekly / f"{sunday}.sha256").read_text(encoding="utf-8").splitlines():
        digest, file = line.split("  ")
        assert backup._sha256(weekly / file) == digest  # the hard links still hold the data
    assert (weekly / f"{sunday}.dump.enc").read_bytes()[8:].startswith(b"PGDMP")


def test_a_failed_weekly_copy_keeps_the_daily_success(db, configured, monkeypatch):
    def broken(source, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backup, "_link_or_copy", broken)
    result = backup.run_backup(now=datetime(2026, 11, 8, 2, 30, tzinfo=UTC))
    assert result["weekly"] is False and backup.is_complete(configured / "daily" / result["set"])
    assert list((configured / "weekly").iterdir()) == []
    state = BackupState.objects.get()
    assert state.last_set == result["set"] and state.last_failure_at is None


def test_prune_never_removes_the_set_just_written(tmp_path):
    for name in ("softwarden-20261101T023000Z", "softwarden-20261102T023000Z", "softwarden-20261103T023000Z"):
        make_set(tmp_path, name)
    # A clock stepped back: the new set sorts oldest.
    assert backup.prune(tmp_path, 1, protect="softwarden-20261101T023000Z") == ["softwarden-20261102T023000Z"]
    assert [p.name for p in backup.complete_sets(tmp_path)] == [
        "softwarden-20261101T023000Z",
        "softwarden-20261103T023000Z",
    ]


@pytest.fixture
def private_tmp(tmp_path, monkeypatch):
    path = tmp_path / "tmp"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


@pytest.fixture
def popen_spy(monkeypatch):
    started = []
    real = subprocess.Popen

    def spy(*args, **kwargs):
        proc = real(*args, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", spy)
    return started


@pytest.mark.parametrize("how", ["stopping", "lease_lost"])
def test_an_abort_mid_stage_publishes_nothing_and_leaves_nothing_running(
    db, configured, ctl, private_tmp, popen_spy, monkeypatch, how
):
    (ctl / "slow-pg_dump").write_text("", encoding="utf-8")  # pg_dump hangs for a minute
    calls = {"n": 0}

    def stopping():
        calls["n"] += 1
        return how == "stopping" and calls["n"] >= 2

    def renew():
        raise jobs.LeaseLost

    if how == "lease_lost":
        monkeypatch.setattr(backup, "RENEW_EVERY", 0.0)
    expected = jobs.LeaseLost if how == "lease_lost" else backup.BackupError
    clock = time.monotonic()
    with pytest.raises(expected):
        backup.run_backup(stopping=stopping, renew=renew)
    assert time.monotonic() - clock < 30  # stopped, not waited out
    assert daily_dirs(configured) == []  # no set and no .in-progress folder
    assert list(private_tmp.iterdir()) == []  # no scratch folder (pgpass, manifest) left
    assert popen_spy and all(proc.poll() is not None for proc in popen_spy)  # every child is gone
    assert BackupState.objects.get().last_failure_at is not None


def test_stale_scratch_folders_are_cleared(db, configured, private_tmp):
    stale = private_tmp / f"{backup.scratch_prefix()}crashed"
    stale.mkdir()
    (stale / "pgpass").write_text("x", encoding="utf-8")
    # Another database's run on the same host (a parallel test run) keeps its folder (backups nit).
    others = private_tmp / f"{backup.SCRATCH_PREFIX}some-other-db--running"
    others.mkdir()
    longer = private_tmp / f"{backup.scratch_prefix()[:-2]}-carry--running"  # a name that only starts like ours
    longer.mkdir()
    backup.run_backup()
    assert not stale.exists() and sorted(p.name for p in private_tmp.iterdir()) == sorted([others.name, longer.name])


def test_request_backup_takes_a_transaction_lock(db, configured, monkeypatch):
    from django.db.backends.utils import CursorWrapper

    seen = []

    original = CursorWrapper.execute

    def spy(self, sql, params=None):
        seen.append(sql)
        return original(self, sql, params)

    monkeypatch.setattr(CursorWrapper, "execute", spy)
    backup.request_backup("test")
    assert any("pg_advisory_xact_lock" in sql for sql in seen)
    assert Organization.objects.count() == 0  # no organization needed for an installation-wide job
