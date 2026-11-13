"""ops/restore.sh run for real against a stand-in `docker`: what it runs, in what
order, and how it ends. The stand-in logs every docker command and answers the few whose output the script reads; the
backup set is a real one encrypted with ops/common.sh's openssl parameters. Needs bash and openssl."""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core import backup

REPO = Path(__file__).resolve().parents[3]
KEY = "a-test-backup-key-not-a-real-one"

FAKE_DOCKER = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$*" in
  *"pg_restore -l"*) cat > /dev/null; echo "; TOC Entries: 1"; exit 0 ;;
  *"ps --status running --services"*) echo app; exit 0 ;;
  *"run --rm --no-deps -T migrate"*) exit "${FAKE_MIGRATE_RC:-0}" ;;
  *"manage.py shell"*) echo "unreadable=${FAKE_UNREADABLE:-0}"; exit 0 ;;
  *"pg_restore"*) cat > /dev/null; exit 0 ;;
esac
exit 0
"""

# The full path: on Windows, CreateProcess looks in System32 (WSL's bash.exe) before PATH.
BASH = shutil.which("bash") or "bash"
pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("openssl") is None, reason="needs bash and openssl"
)


def posix(path: Path) -> str:
    return path.as_posix()


@pytest.fixture
def restore(tmp_path):
    """Returns run(*args, **env) -> (exit code, output, the docker commands it ran)."""
    bin_dir, project, keys, sets = (tmp_path / n for n in ("bin", "project", "keys", "sets"))
    for d in (bin_dir, project, keys, sets):
        d.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    (project / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    key = keys / "backup.key"
    key.write_text(f"{KEY}\r\n", encoding="utf-8", newline="")  # CRLF: the tag uses the line without it, as openssl
    key.chmod(0o600)
    (sets / "plain.dump").write_bytes(b"PGDMP stand-in")
    env = {
        **os.environ,
        "BACKUP_KEY_FILE": posix(key),
        "SOFTWARDEN_DIR": posix(project),
        "SOFTWARDEN_ALLOW_NOACL_KEY": "1",  # Git Bash on Windows keeps no file modes
        "FAKE_LOG": posix(tmp_path / "docker.log"),
    }
    env.pop("COMPOSE_PROJECT_NAME", None)
    dump = sets / "softwarden-2026-11-11.dump.enc"
    subprocess.run(  # noqa: S603
        [
            BASH,
            "-c",
            '. "$1/ops/common.sh"; aes_encrypt "$2" "$3"',
            "-",
            posix(REPO),
            posix(sets / "plain.dump"),
            posix(dump),
        ],
        env=env,
        check=True,
    )
    # The checksum file and its integrity tag, as core/backup.py writes them; restore.sh checks the tag in bash.
    sums = sets / "softwarden-2026-11-11.sha256"
    sums.write_bytes(f"{hashlib.sha256(dump.read_bytes()).hexdigest()}  {dump.name}\n".encode())
    tag = Path(f"{sums}.hmac")
    tag.write_text(f"HMAC-SHA256 {backup.sums_tag(sums.read_bytes(), KEY)}\n", encoding="utf-8", newline="\n")
    # The stand-in comes first on PATH. On Windows, bash.exe turns this list into its own PATH.
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]

    def run(*args, **extra):
        result = subprocess.run(  # noqa: S603
            [BASH, posix(REPO / "ops" / "restore.sh"), *args, posix(dump)],
            env={**env, **{k: str(v) for k, v in extra.items()}},
            capture_output=True,
            text=True,
            timeout=120,
        )
        log = tmp_path / "docker.log"
        commands = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        log.unlink(missing_ok=True)
        return result.returncode, result.stdout + result.stderr, commands

    run.dump, run.sums, run.tag, run.env = dump, sums, tag, env  # type: ignore[attr-defined]
    return run


def started(commands):
    return any(c.startswith("compose start app worker") for c in commands)


def test_a_restore_migrates_before_it_starts_the_app(restore):
    code, out, commands = restore("--yes", "--no-manifest", "--no-media")
    assert code == 0, out
    migrate = commands.index("compose run --rm --no-deps -T migrate")
    assert migrate < next(i for i, c in enumerate(commands) if c.startswith("compose start app worker"))


def test_a_failed_migration_leaves_the_app_and_worker_stopped(restore):
    """The app no longer migrates as it starts, so starting it after a failed migration would serve this
    image's code on the old schema."""
    code, out, commands = restore("--yes", "--no-manifest", "--no-media", FAKE_MIGRATE_RC=3)
    assert code == 3, out
    assert "compose stop app worker" in commands
    assert not started(commands), commands
    assert "app and worker left STOPPED" in out
    assert "FAILED during: migrating the restored database" in out


# ---- the integrity tag -------------------------------------------------------------------------------------


def test_a_tagged_set_restores(restore):
    code, out, _ = restore("--yes", "--no-manifest", "--no-media")
    assert code == 0, out
    assert "integrity tag ok" in out and "sha256 ok" in out


def test_a_changed_set_with_a_rewritten_checksum_fails_the_rehearsal(restore):
    """The review's acceptance: flip a byte in the set and regenerate the .sha256; restore.sh --rehearse fails."""
    data = bytearray(restore.dump.read_bytes())
    data[-1] ^= 0x01
    restore.dump.write_bytes(bytes(data))
    restore.sums.write_bytes(f"{hashlib.sha256(bytes(data)).hexdigest()}  {restore.dump.name}\n".encode())
    code, out, commands = restore("--rehearse")
    assert code != 0
    assert ".sha256.hmac doesn't match" in out and "FAILED during: checking the integrity tag" in out
    assert not any("pg_restore" in c or "createdb" in c or "CREATE DATABASE" in c for c in commands), commands
    # A plain restore refuses the same way, before anything is stopped.
    code, out, commands = restore("--yes", "--no-manifest", "--no-media")
    assert code != 0 and "doesn't match" in out and "compose stop app worker" not in commands


def test_a_tag_under_another_key_fails(restore, tmp_path):
    other = tmp_path / "keys" / "other.key"
    other.write_text("another-key\n", encoding="utf-8")
    other.chmod(0o600)
    code, out, _ = restore("--yes", "--no-manifest", "--no-media", BACKUP_KEY_FILE=posix(other))
    assert code != 0 and "doesn't match" in out


def test_an_untagged_set_needs_accept_untagged(restore):
    """Sets written before tags (and a set whose tag was deleted) are refused unless the operator says the copy is
    trusted; then the checksum is still checked."""
    restore.tag.unlink()
    code, out, commands = restore("--yes", "--no-manifest", "--no-media")
    assert code != 0 and "--accept-untagged" in out and "compose stop app worker" not in commands
    # The refusal says a 1.0+ set without a tag was altered or is incomplete, and the flag is for pre-1.0 sets only.
    assert "Every set written by Softwarden 1.0 or later has one" in out and "altered or is incomplete" in out
    code, out, _ = restore("--accept-untagged", "--yes", "--no-manifest", "--no-media")
    assert code == 0, out
    assert "WARNING: no softwarden-2026-11-11.sha256.hmac" in out and "sha256 ok" in out
    assert "(taken 2026-11-11)" in out and "before Softwarden 1.0" in out  # the set's time beside the warning
    # Without the checksum file too: refused, then accepted with the flag (a set from before checksums).
    restore.sums.unlink()
    code, out, _ = restore("--yes", "--no-manifest", "--no-media")
    assert code != 0 and "--accept-untagged" in out
    code, out, _ = restore("--accept-untagged", "--yes", "--no-manifest", "--no-media")
    assert code == 0, out


def test_an_empty_key_line_is_refused(restore, tmp_path):
    """A key file whose first line is empty is refused before anything is checked, as core/backup.check_key refuses
    it, and ops/common.sh's sums_tag makes no tag from it."""
    empty = tmp_path / "keys" / "empty.key"
    empty.write_text("\nthe-key-on-line-two\n", encoding="utf-8", newline="\n")
    empty.chmod(0o600)
    code, out, commands = restore("--yes", "--no-manifest", "--no-media", BACKUP_KEY_FILE=posix(empty))
    assert code != 0 and "first line is empty" in out and not commands
    result = subprocess.run(  # noqa: S603
        [BASH, "-c", '. "$1/ops/common.sh"; sums_tag "$2"', "-", posix(REPO), posix(restore.sums)],
        env={**restore.env, "BACKUP_KEY_FILE": posix(empty)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and not result.stdout.strip()


def test_ops_backup_writes_the_same_tag_as_the_worker(restore, tmp_path):
    """ops/common.sh's write_sums_tag (ops/backup.sh) and core/backup.sums_tag agree, byte for byte."""
    restore.tag.unlink()
    subprocess.run(  # noqa: S603
        [BASH, "-c", '. "$1/ops/common.sh"; write_sums_tag "$2"', "-", posix(REPO), posix(restore.sums)],
        env=restore.env,
        check=True,
    )
    want = f"HMAC-SHA256 {backup.sums_tag(restore.sums.read_bytes(), KEY)}\n"
    assert restore.tag.read_bytes().decode() == want
