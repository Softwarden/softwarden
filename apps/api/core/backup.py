"""Nightly encrypted backups taken by the worker (the in-app counterpart of
ops/backup.sh). docs/04-development/backup-runbook.md is the operator's guide.

A backup is a *set*: one directory under `<BACKUP_DIR>/daily/` named `softwarden-<UTC YYYYMMDDTHHMMSSZ>[-n]` holding

    <set>.manifest.json.enc   the `upgrade_check --write-manifest` manifest, written just before the dump
    <set>.dump.enc            pg_dump -Fc
    <set>.media.tar.enc       a tar of MEDIA_ROOT (top-level `media/`, without `.incoming`), taken right after the dump
    <set>.sha256              `sha256sum` lines for the three files above
    <set>.sha256.hmac         "HMAC-SHA256 <hex>": the sums file's integrity tag under the backup key (sums_tag)

each `.enc` made with `openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass file:<key file>`: the format of
ops/backup.sh, so `ops/restore.sh` restores a set from either. A set taken on a Sunday (UTC) is also hard-linked into
`weekly/`. Retention keeps the newest BACKUP_KEEP_DAILY (14) daily and BACKUP_KEEP_WEEKLY (8) weekly sets; the newest
complete set is never pruned.

Rules:
- Streaming. pg_dump writes into openssl's stdin and openssl writes the file; the media tar is written by Python's
  tarfile straight into openssl's stdin. Nothing readable is written except the manifest, in a private temporary
  directory removed at once. Every stage's exit status is checked.
- No secret leaves the process. The key reaches openssl as `-pass file:<path>`, never as a value; the database password
  reaches pg_dump through a 0600 PGPASSFILE in a private temporary directory, not the command line or the environment.
  Errors are redacted (core/jobs.redact_configured, which knows this key) before they are stored or logged.
- Atomic publish. A set is built in `daily/.in-progress-<uuid>/`, the sums file is written last, and the directory
  is renamed into place. A failed run publishes nothing; leftovers are removed by the next run. A set counts as complete
  only when its sums file lists every part present (`is_complete`).
- One run at a time: a PostgreSQL session advisory lock, so the worker's job and `backup_now --inline` exclude each other
  across processes, and a crash releases it with the connection.
- Media consistency. The dump comes first, then the media: a file whose attachment is deleted after the dump is kept on
  the volume for SOFTWARDEN_ATTACHMENT_RETENTION_DAYS (7, at least 1, and every run is far shorter), so every file the
  dump names is in the archive. Files added after the dump are extra and harmless.

Disk safety. Before writing, a run checks the destination's free space and refuses (a recorded failure) when free
space minus an estimate (the last set's size x 1.2; on the first run the database size plus the media size) would fall
below a floor: SOFTWARDEN_BACKUP_MIN_FREE_MB, or by default the larger of 2 GB and 10 % of the disk. The default volume
shares the database's disk, so a full disk would stop PostgreSQL too. It also refuses a destination that isn't a mount
point (SOFTWARDEN_BACKUP_REQUIRE_MOUNT, on by default): a run in a container without the volume, such as
`backup_now --inline` in the app container, would otherwise write into the container's own layer and report success.

Status lives in the installation-wide BackupState row (core/models.py), so the app container (which doesn't mount the
backup volume) can show it; the Job row holds the run's set name, size and duration."""

import hashlib
import hmac
import io
import logging
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management import call_command
from django.db import connection, transaction
from django.utils import timezone

from . import jobs
from .models import BackupState, Job, JobStatus

log = logging.getLogger("softwarden.backup")

JOB_KIND = "backup.run"
SCHEDULE = "backup.nightly"
LEASE = timedelta(minutes=15)
RENEW_EVERY = 60.0  # seconds between lease renewals while a stage runs
# pg_try_advisory_lock key: "SWBACKUP" as a 64-bit integer, distinct from the other advisory locks in this codebase.
LOCK_KEY = 0x5357_4241_434B_5550 - (1 << 63)
REQUEST_LOCK_KEY = LOCK_KEY + 1  # serializes request_backup's check-and-queue
SCRATCH_PREFIX = "softwarden-backup-"


def scratch_prefix() -> str:
    """The private temporary folders' prefix, named after the database: runs against different
    databases on one host (parallel test runs) never clear each other's folders. `--` ends the name, which has no
    double dash once slugified, so `softwarden` never matches `softwarden_carry`'s folders."""
    name = re.sub(r"[^a-z0-9]+", "-", str(connection.settings_dict.get("NAME") or "db").lower()).strip("-") or "db"
    return f"{SCRATCH_PREFIX}{name}--"


GB = 1024**3
MB = 1024**2
CIPHER = ("enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000")
SET_PREFIX = "softwarden-"
SET_NAME = re.compile(r"^softwarden-(\d{8}T\d{6}Z)(?:-(\d+))?$")
IN_PROGRESS = ".in-progress-"
PARTS = ("dump.enc", "manifest.json.enc", "media.tar.enc")  # sums order, as ops/backup.sh writes them
STDERR_TAIL = 300
# Top-level MEDIA_ROOT folders left out of the media archive: ops/restore.sh's staging directory, and the organization
# data exports and tenant backups (catalog/org_lifecycle.EXPORT_ROOT). Those zips are rebuilt from the
# database and the attachments, are short-lived, and would otherwise multiply every set by the retained tenant backups.
# After a restore their rows find no file and the download answers 404 (catalog/org_lifecycle.open_export).
MEDIA_EXCLUDE = frozenset({".incoming", "exports"})


class BackupError(Exception):
    """A backup failed. The message names the stage and, for a tool, its exit status: it is stored and shown to every
    organization's Admins, so it never holds a tool's output, which could
    carry one organization's data. `detail` (a tool's stderr tail) goes to the worker log only, redacted."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


class BackupBusy(BackupError):
    """Another backup holds the lock."""


class NotConfigured(BackupError):
    """SOFTWARDEN_BACKUP_DIR isn't set."""


# ---- configuration ----------------------------------------------------------------------------------------------


def destination() -> Path | None:
    return Path(settings.BACKUP_DIR) if settings.BACKUP_DIR else None


def key_path() -> Path:
    return Path(settings.BACKUP_KEY_FILE)


def key_source() -> str:
    """`configured` when SOFTWARDEN_BACKUP_KEY_FILE names the key, `generated` when it is the secrets volume's."""
    return "configured" if settings.BACKUP_KEY_FILE_CONFIGURED else "generated"


def key_value() -> str:
    """The key as openssl reads it (the first line), for redaction and the fingerprint only; "" when unreadable.
    Never logged, returned or stored."""
    try:
        with key_path().open(encoding="utf-8") as handle:
            return handle.readline().rstrip("\r\n")
    except (OSError, UnicodeError):
        return ""


def key_fingerprint() -> str:
    """A short one-way digest of the key, to tell whether an acknowledgement was for this key. "" without a key."""
    value = key_value()
    if not value:
        return ""
    return hashlib.sha256(b"softwarden-backup-key-acknowledgement\0" + value.encode()).hexdigest()[:16]


def check_key() -> Path:
    """The key file, or BackupError: missing, empty, or (on POSIX) readable by group or others (as ops/common.sh)."""
    path = key_path()
    if not path.is_file():
        raise BackupError(
            f"The backup key file {path} is missing. The init service generates it (docker compose up -d), or set "
            "SOFTWARDEN_BACKUP_KEY_FILE."
        )
    if not key_value():
        raise BackupError(f"The backup key file {path} is empty or unreadable.")
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise BackupError(f"The backup key file {path} is readable by group or others; chmod 600 it.")
    return path


def tools() -> dict[str, list[str]]:
    """The commands, as argv prefixes (tests replace them)."""
    return {
        "pg_dump": [settings.BACKUP_PG_DUMP],
        "pg_restore": [settings.BACKUP_PG_RESTORE],
        "openssl": [settings.BACKUP_OPENSSL],
    }


# ---- sets on disk -----------------------------------------------------------------------------------------------


def set_order(name: str) -> tuple[str, int]:
    match = SET_NAME.match(name)
    if match is None:
        return ("", 0)
    return (match.group(1), int(match.group(2) or 1))


def is_complete(set_dir: Path) -> bool:
    """A published set: named like one, with a sums file that lists the dump and every part present in it."""
    name = set_dir.name
    if SET_NAME.match(name) is None or not set_dir.is_dir():
        return False
    sums = set_dir / f"{name}.sha256"
    if not sums.is_file():
        return False
    try:
        listed = {
            line.split()[-1].lstrip("*") for line in sums.read_text(encoding="utf-8").splitlines() if line.strip()
        }
    except (OSError, UnicodeError, IndexError):
        return False
    present = {f"{name}.{part}" for part in PARTS if (set_dir / f"{name}.{part}").is_file()}
    return f"{name}.dump.enc" in present and present <= listed and all((set_dir / f).is_file() for f in listed)


def complete_sets(parent: Path) -> list[Path]:
    """Complete sets in `parent`, oldest first."""
    if not parent.is_dir():
        return []
    found = [p for p in parent.iterdir() if is_complete(p)]
    return sorted(found, key=lambda p: set_order(p.name))


def prune(parent: Path, keep: int, protect: str = "") -> list[str]:
    """Delete all but the newest `keep` complete sets (at least one is always kept). Directories that aren't complete
    sets are left alone, and so is `protect` (the set just written, even if a clock step made it sort as old). Returns
    the names removed."""
    keep = max(1, int(keep))
    sets = complete_sets(parent)
    removed = []
    for old in sets[:-keep]:
        if old.name == protect:
            continue
        shutil.rmtree(old)
        removed.append(old.name)
    return removed


def _clear_leftovers(parent: Path) -> int:
    """Remove in-progress directories of earlier runs (only called under the lock, so none is live)."""
    cleared = 0
    if parent.is_dir():
        for path in parent.iterdir():
            if path.name.startswith(IN_PROGRESS) and path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                cleared += 1
    return cleared


def _clear_stale_scratch() -> int:
    """Remove private temporary folders of earlier runs a crash left behind (only under the lock)."""
    cleared = 0
    for path in Path(tempfile.gettempdir()).glob(f"{scratch_prefix()}*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            cleared += 1
    return cleared


def disk_usage(path: Path):
    return shutil.disk_usage(path)


def _tree_size(root: Path) -> int:
    """The size of the files the media archive takes: MEDIA_ROOT without MEDIA_EXCLUDE."""
    total = 0
    for current, dirs, files in os.walk(root):
        if Path(current) == root:
            dirs[:] = [d for d in dirs if d not in MEDIA_EXCLUDE]
        for name in files:
            try:
                total += (Path(current) / name).stat().st_size
            except OSError:
                continue
    return total


def estimate_bytes() -> int:
    """How much the next set needs: the last set's size x 1.2, or on the first run the database plus the media."""
    row = BackupState.objects.filter(pk=1).first()
    if row is not None and row.last_size_bytes:
        return int(row.last_size_bytes * 1.2)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_database_size(current_database())")
        (database,) = cursor.fetchone()
    return int(database) + _tree_size(Path(settings.MEDIA_ROOT))


def floor_bytes(total: int) -> int:
    if settings.BACKUP_MIN_FREE_MB is not None:
        return int(settings.BACKUP_MIN_FREE_MB) * MB
    return max(2 * GB, total // 10)


def _size(n: int) -> str:
    return f"{n / GB:.1f} GB" if n >= GB else f"{n / MB:.0f} MB"


def check_space(dest: Path) -> None:
    usage = disk_usage(dest)
    estimate, floor = estimate_bytes(), floor_bytes(usage.total)
    if usage.free - estimate < floor:
        raise BackupError(
            f"Not enough free space for a backup: {_size(usage.free)} free, the set needs about {_size(estimate)} and "
            f"{_size(floor)} must stay free (SOFTWARDEN_BACKUP_MIN_FREE_MB). Free space or move the backups to "
            "another disk (backup runbook)."
        )


def check_mounted(dest: Path) -> None:
    if settings.BACKUP_REQUIRE_MOUNT and not os.path.ismount(dest):
        raise BackupError(
            f"The backup destination {dest} isn't a mounted volume in this container, so a set would be lost with "
            "it. Run backups in the worker container (docker compose exec worker python manage.py backup_now), or "
            "set SOFTWARDEN_BACKUP_REQUIRE_MOUNT=0 for development."
        )


def _set_name(daily: Path, started: datetime) -> str:
    """`softwarden-<UTC stamp>`, or with `-2`, `-3`, ... when a set of that second exists already."""
    base = f"{SET_PREFIX}{started.astimezone(UTC):%Y%m%dT%H%M%SZ}"
    name, n = base, 1
    while (daily / name).exists():
        n += 1
        name = f"{base}-{n}"
    return name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


TAG_LABEL = "HMAC-SHA256"


def sums_tag(sums: bytes, key: str) -> str:
    """The integrity tag of a set's .sha256 file: HMAC-SHA256 of its bytes under
    SHA-256("softwarden-backup-sums-hmac" NUL key line). openssl enc's CBC has no authentication and the sums file
    beside a set can be rewritten by anyone who can write the share; only the backup key's holder can write a tag that
    matches. ops/common.sh's sums_tag computes the same value and ops/restore.sh checks it."""
    mac_key = hashlib.sha256(b"softwarden-backup-sums-hmac\0" + key.encode()).digest()
    return hmac.new(mac_key, sums, hashlib.sha256).hexdigest()


def _write_sums(set_dir: Path, name: str) -> None:
    lines = [
        f"{_sha256(set_dir / f'{name}.{part}')}  {name}.{part}\n"
        for part in PARTS
        if (set_dir / f"{name}.{part}").is_file()
    ]
    body = "".join(lines).encode("utf-8")
    key = key_value()
    if not key:
        raise BackupError("The backup key file became unreadable during the backup; the set wasn't published.")
    tag = set_dir / f".{name}.sha256.hmac.part"
    tag.write_text(f"{TAG_LABEL} {sums_tag(body, key)}\n", encoding="utf-8", newline="\n")
    os.replace(tag, set_dir / f"{name}.sha256.hmac")  # before the sums file, which marks the set complete
    tmp = set_dir / f".{name}.sha256.part"
    tmp.write_bytes(body)
    os.replace(tmp, set_dir / f"{name}.sha256")


def _link_or_copy(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


# ---- processes --------------------------------------------------------------------------------------------------


@dataclass
class Runner:
    """Starts the pipeline's processes, renews the job's lease while they run and stops them all on any failure.
    `tick` is called about once a second; it renews the lease every RENEW_EVERY seconds and raises when the worker is
    stopping."""

    renew: Callable[[], None] = lambda: None
    stopping: Callable[[], bool] = lambda: False
    scratch: Path = field(default_factory=Path)
    env: dict[str, str] = field(default_factory=dict)
    procs: list[subprocess.Popen] = field(default_factory=list)
    labels: dict[int, str] = field(default_factory=dict)
    last_renew: float = field(default_factory=time.monotonic)

    def tick(self) -> None:
        if self.stopping():
            raise BackupError("The worker is stopping; the backup was abandoned and nothing was published.")
        if time.monotonic() - self.last_renew >= RENEW_EVERY:
            self.renew()
            self.last_renew = time.monotonic()

    def _stderr_path(self, label: str) -> Path:
        return self.scratch / (re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") + ".stderr")

    def start(self, argv: list[str], label: str, **kwargs: Any) -> subprocess.Popen:
        errors = self._stderr_path(label).open("wb")
        try:
            proc = subprocess.Popen(argv, stderr=errors, env=self.env, **kwargs)  # noqa: S603 (fixed argv, no shell)
        except OSError as error:
            errors.close()
            raise BackupError(f"Can't start {label} ({argv[0]}): {error.strerror or error}.") from None
        errors.close()
        self.labels[id(proc)] = label
        self.procs.append(proc)
        return proc

    def stderr_of(self, label: str) -> str:
        try:
            text = self._stderr_path(label).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return " ".join(text.split())[-STDERR_TAIL:]

    def wait(self, proc: subprocess.Popen) -> int:
        while True:
            try:
                return proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.tick()

    def check(self, proc: subprocess.Popen, allowed: tuple[int, ...] = (0,)) -> None:
        code = self.wait(proc)
        if code not in allowed:
            label = self.labels.get(id(proc), "a backup process")
            raise BackupError(f"{label} exited with status {code}.", detail=self.stderr_of(label))

    def kill_all(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass


def _openssl_encrypt(key: Path, out: Path) -> list[str]:
    return [*tools()["openssl"], *CIPHER, "-salt", "-pass", f"file:{key}", "-out", str(out)]


def _openssl_decrypt(key: Path, source: Path) -> list[str]:
    return [*tools()["openssl"], *CIPHER, "-d", "-pass", f"file:{key}", "-in", str(source)]


# libpq's TLS settings, which the app's own connection (psycopg, also libpq) already follows: an external PostgreSQL
# that requires TLS needs pg_dump and pg_restore to follow them too. Paths and a
# mode, never a secret (a client key file is passed by path).
PG_TLS_ENV = ("PGSSLMODE", "PGSSLROOTCERT", "PGSSLCERT", "PGSSLKEY")


def _pgpass_line(db: dict[str, Any]) -> str:
    def esc(value: Any) -> str:
        return str(value).replace("\\", "\\\\").replace(":", "\\:")

    return f"*:*:*:*:{esc(db.get('PASSWORD') or '')}\n"


def _process_env(scratch: Path, db: dict[str, Any]) -> dict[str, str]:
    """A minimal environment: no inherited secret reaches a child. The password goes through a private PGPASSFILE."""
    pgpass = scratch / "pgpass"
    fd = os.open(pgpass, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(_pgpass_line(db))
    env = {"PGPASSFILE": str(pgpass), "PGCONNECT_TIMEOUT": "30", "LC_ALL": "C.UTF-8"}
    for name in ("PATH", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "TMPDIR", "HOME", *PG_TLS_ENV):
        if os.environ.get(name):
            env[name] = os.environ[name]
    # POSTGRES_SSLMODE / POSTGRES_SSLROOTCERT (softwarden/env.db_tls_options) are in the app's connection options, which
    # libpq ranks above its environment: the tools get them the same way round.
    options = db.get("OPTIONS") or {}
    for option, name in (("sslmode", "PGSSLMODE"), ("sslrootcert", "PGSSLROOTCERT")):
        if options.get(option):
            env[name] = str(options[option])
    return env


def _pg_dump_argv(db: dict[str, Any]) -> list[str]:
    argv = [*tools()["pg_dump"], "--format=custom", "--no-owner", "--no-password"]
    if db.get("HOST"):
        argv += ["--host", str(db["HOST"])]
    if db.get("PORT"):
        argv += ["--port", str(db["PORT"])]
    if db.get("USER"):
        argv += ["--username", str(db["USER"])]
    return [*argv, "--dbname", str(db["NAME"])]


def _dump(run: Runner, key: Path, out: Path) -> None:
    """pg_dump | openssl -> out."""
    db = connection.settings_dict
    dump = run.start(_pg_dump_argv(db), "pg_dump", stdout=subprocess.PIPE)
    assert dump.stdout is not None
    enc = run.start(_openssl_encrypt(key, out), "openssl (dump)", stdin=dump.stdout)
    dump.stdout.close()  # openssl holds the only read end, so pg_dump sees a closed pipe if openssl dies
    run.check(dump)
    run.check(enc)
    if not out.is_file() or out.stat().st_size == 0:
        raise BackupError("The encrypted dump is empty.")


def _verify_dump(run: Runner, key: Path, source: Path) -> None:
    """A full decrypt (openssl checks the padding of the last block, so a cut-off file fails), then a pg_restore listing
    of the decrypted stream (only the right key gives one), as ops/common.sh check_archive."""
    run.check(run.start(_openssl_decrypt(key, source), "openssl (dump check)", stdout=subprocess.DEVNULL))
    dec = run.start(_openssl_decrypt(key, source), "openssl (dump listing)", stdout=subprocess.PIPE)
    assert dec.stdout is not None
    listing = run.scratch / "toc.txt"
    with listing.open("wb") as toc:
        lister = run.start([*tools()["pg_restore"], "--list"], "pg_restore --list", stdin=dec.stdout, stdout=toc)
        dec.stdout.close()
        run.check(lister)
    run.wait(dec)  # pg_restore reads only the table of contents, so openssl may stop on a closed pipe: not checked
    if b"TOC Entries" not in listing.read_bytes():
        raise BackupError("The encrypted dump doesn't decrypt to a pg_dump archive.")


def _media_entries(root: Path) -> Iterator[tuple[Path, str]]:
    """(path, name in the archive) for MEDIA_ROOT and everything under it except MEDIA_EXCLUDE (the restore staging
    directory and the organization exports)."""
    yield root, "media"
    for current, dirs, files in os.walk(root):
        here = Path(current)
        rel = here.relative_to(root)
        if rel == Path("."):
            dirs[:] = [d for d in dirs if d not in MEDIA_EXCLUDE]
        dirs.sort()
        for name in [*sorted(dirs), *sorted(files)]:
            yield here / name, (Path("media") / rel / name).as_posix()


def _media(run: Runner, key: Path, out: Path) -> int:
    """tar of MEDIA_ROOT | openssl -> out. A file removed while the tar is written (the sweep) is skipped. Returns the
    number of files archived."""
    root = Path(settings.MEDIA_ROOT)
    enc = run.start(_openssl_encrypt(key, out), "openssl (media)", stdin=subprocess.PIPE)
    assert enc.stdin is not None
    files = 0
    try:
        with tarfile.open(fileobj=enc.stdin, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            if not root.is_dir():
                info = tarfile.TarInfo("media")
                info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o700, int(time.time())
                tar.addfile(info)
            else:
                for count, (path, arcname) in enumerate(_media_entries(root)):
                    if count % 200 == 0:
                        run.tick()
                    try:
                        tar.add(path, arcname=arcname, recursive=False)
                    except FileNotFoundError:
                        continue
                    if path.is_file():
                        files += 1
    except OSError as error:  # a broken pipe when openssl has died, or a file that can't be read
        try:
            code = enc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            code = None
        if code in (None, 0):
            raise BackupError("Writing the media archive failed.", detail=str(error)) from None
        # openssl failed first: run.check below reports its status and message
    finally:
        try:
            enc.stdin.close()
        except OSError:
            pass
    run.check(enc)
    return files


def _verify_media(run: Runner, key: Path, source: Path) -> int:
    """Decrypt in full and read the archive entry by entry; the archive must hold `media/`. Returns the entry count."""
    dec = run.start(_openssl_decrypt(key, source), "openssl (media check)", stdout=subprocess.PIPE)
    assert dec.stdout is not None
    entries = 0
    top = False
    try:
        with tarfile.open(fileobj=dec.stdout, mode="r|") as tar:
            for member in tar:
                entries += 1
                top = top or member.name == "media"
                if entries % 200 == 0:
                    run.tick()
    except tarfile.TarError as error:
        dec.kill()
        raise BackupError("The encrypted media archive doesn't decrypt to a readable tar.", detail=str(error)) from None
    finally:
        dec.stdout.close()
    run.check(dec)
    if not top:
        raise BackupError("The media archive has no media/ directory.")
    return entries


def _manifest(run: Runner, key: Path, out: Path) -> None:
    """upgrade_check --write-manifest into the private scratch directory, encrypted, the plain copy removed."""
    plain = run.scratch / "manifest.json"
    try:
        call_command("upgrade_check", write_manifest=str(plain), stdout=io.StringIO())
        if not plain.is_file() or plain.stat().st_size == 0:
            raise BackupError("upgrade_check wrote an empty manifest.")
        run.check(
            run.start([*_openssl_encrypt(key, out), "-in", str(plain)], "openssl (manifest)", stdin=subprocess.DEVNULL)
        )
    finally:
        plain.unlink(missing_ok=True)


# ---- the run ----------------------------------------------------------------------------------------------------


@contextmanager
def _lock() -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_KEY])
        (taken,) = cursor.fetchone()
    if not taken:
        raise BackupBusy("Another backup is running; only one runs at a time.")
    try:
        yield
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])
        except Exception:  # a dead connection has released the lock already
            log.warning("backup lock release failed; the connection's end releases it")


def _state_row() -> BackupState:
    row, _ = BackupState.objects.select_for_update().get_or_create(pk=1)
    return row


def _record_success(finished: datetime, name: str, size: int, duration: float) -> None:
    with transaction.atomic():
        row = _state_row()
        row.last_success_at, row.last_set, row.last_size_bytes = finished, name, size
        row.last_duration_seconds = round(duration, 1)
        row.save()


def public_error(error: BaseException, stage: str) -> BackupError:
    """The BackupError to store and show: a BackupError as it is, anything else as its stage and type only."""
    if isinstance(error, BackupError):
        return error
    return BackupError(f"The backup failed while {stage} ({type(error).__name__}).", detail=str(error))


def _record_failure(error: BackupError) -> str:
    text = jobs.redact_configured(str(error))[: jobs.ERROR_MAX]
    with transaction.atomic():
        row = _state_row()
        row.last_failure_at, row.last_failure = timezone.now(), text
        row.save(update_fields=["last_failure_at", "last_failure", "updated_at"])
    return text


def run_backup(*, renew: Callable[[], None] | None = None, stopping: Callable[[], bool] | None = None, now=None):
    """Take one backup set and prune. Returns {"set", "size_bytes", "duration_seconds", "media_files", "weekly",
    "pruned"}. Raises BackupError (recorded on BackupState first); NotConfigured and BackupBusy aren't recorded as
    failures: nothing was attempted."""
    dest = destination()
    if dest is None:
        raise NotConfigured("Backups aren't configured: set SOFTWARDEN_BACKUP_DIR (the image sets /data/backups).")
    with _lock():
        progress = {"stage": "checking the settings"}
        try:
            return _run_locked(dest, renew or (lambda: None), stopping or (lambda: False), now, progress)
        except Exception as error:
            public = public_error(error, progress["stage"])
            text = _record_failure(public)
            detail = jobs.redact_configured(public.detail)[-STDERR_TAIL:]
            log.warning("backup failed: %s%s", text, f" detail: {detail}" if detail else "")
            if isinstance(error, (BackupError, jobs.LeaseLost)):
                raise
            raise public from None


def _run_locked(dest: Path, renew, stopping, now, progress: dict[str, str]) -> dict[str, Any]:
    clock = time.monotonic()
    started = now or timezone.now()
    key = check_key()
    progress["stage"] = "checking the destination"
    check_mounted(dest)
    daily, weekly = dest / "daily", dest / "weekly"
    try:
        daily.mkdir(parents=True, exist_ok=True)
        weekly.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise BackupError(f"Can't create the backup folders in {dest}: {error.strerror or error}.") from None
    if not os.access(daily, os.W_OK) or not os.access(weekly, os.W_OK):
        raise BackupError(f"The backup destination {dest} isn't writable by this user.")
    _clear_leftovers(daily)
    _clear_leftovers(weekly)
    _clear_stale_scratch()
    progress["stage"] = "checking free space"
    check_space(dest)
    name = _set_name(daily, started)
    work = daily / f"{IN_PROGRESS}{uuid.uuid4().hex}"
    scratch = Path(tempfile.mkdtemp(prefix=scratch_prefix()))
    run = Runner(renew=renew, stopping=stopping, scratch=scratch)
    try:
        work.mkdir(mode=0o700)
        run.env = _process_env(scratch, connection.settings_dict)
        log.info("backup started set=%s destination=%s", name, dest)
        progress["stage"] = "writing the manifest"
        _manifest(run, key, work / f"{name}.manifest.json.enc")
        progress["stage"] = "dumping the database"
        _dump(run, key, work / f"{name}.dump.enc")
        progress["stage"] = "checking the dump"
        _verify_dump(run, key, work / f"{name}.dump.enc")
        progress["stage"] = "archiving the attachment files"
        media_files = _media(run, key, work / f"{name}.media.tar.enc")
        progress["stage"] = "checking the media archive"
        _verify_media(run, key, work / f"{name}.media.tar.enc")
        progress["stage"] = "writing the checksums"
        _write_sums(work, name)  # last: until it exists the set is not complete
        run.tick()
        progress["stage"] = "publishing the set"
        # openssl creates the parts under the process umask (0644 in the image); the set's files are the app user's
        # alone, like the 0700 folders they are in. Hard-linked weekly copies share these modes.
        for part in work.iterdir():
            os.chmod(part, 0o600)
        os.rename(work, daily / name)
    except BaseException:
        run.kill_all()
        shutil.rmtree(work, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    published = daily / name
    size = sum(p.stat().st_size for p in published.iterdir() if p.is_file())
    on_sunday = started.astimezone(UTC).isoweekday() == 7
    weekly_kept = False
    if on_sunday and not (weekly / name).exists():
        # The daily set is published and good whatever happens here: a failure is a warning, not a failed backup.
        staging = weekly / f"{IN_PROGRESS}{uuid.uuid4().hex}"
        try:
            staging.mkdir(mode=0o700)
            for part in sorted(published.iterdir()):
                _link_or_copy(part, staging / part.name)
            os.rename(staging, weekly / name)
            weekly_kept = True
        except OSError as error:
            shutil.rmtree(staging, ignore_errors=True)
            log.warning("backup set=%s published, but keeping it as this week's failed: %s", name, type(error).__name__)
    pruned = prune(daily, settings.BACKUP_KEEP_DAILY, protect=name) + prune(
        weekly, settings.BACKUP_KEEP_WEEKLY, protect=name
    )
    duration = time.monotonic() - clock
    _record_success(timezone.now(), name, size, duration)
    log.info(
        "backup published set=%s size=%s media_files=%s weekly=%s pruned=%s took=%.1fs",
        name,
        size,
        media_files,
        weekly_kept,
        len(pruned),
        duration,
    )
    return {
        "set": name,
        "size_bytes": size,
        "duration_seconds": round(duration, 1),
        "media_files": media_files,
        "weekly": weekly_kept,
        "pruned": len(pruned),
    }


# ---- the job, the schedule and the command ------------------------------------------------------------------------


def run_job(job: Job, ctx: jobs.JobContext) -> dict[str, Any]:
    try:
        return run_backup(renew=ctx.renew, stopping=ctx.stopping)
    except (NotConfigured, BackupBusy) as error:
        raise jobs.PermanentError(str(error)) from None


def active_job() -> Job | None:
    return (
        Job.objects.filter(kind=JOB_KIND, status__in=[JobStatus.QUEUED, JobStatus.RUNNING])
        .order_by("created_at")
        .first()
    )


def request_backup(trigger: str) -> tuple[Job, bool]:
    """Queue a backup job unless one is queued or running already. (job, created)."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [REQUEST_LOCK_KEY])
        existing = active_job()
        if existing is not None:
            return existing, False
        return jobs.enqueue(JOB_KIND, {"trigger": trigger}), True


def scheduled(now) -> dict[str, Any]:
    """The nightly schedule: queue the job (the long lane runs it, so the tick never waits on a backup)."""
    if destination() is None:
        return {"skipped": "not configured"}
    _job, created = request_backup("schedule")
    return {"queued": 1} if created else {"skipped": "a backup is already queued or running"}


def register() -> None:
    jobs.register_job(JOB_KIND, run_job, lease=LEASE, max_attempts=1, lane=jobs.LONG_LANE)
    jobs.register_schedule(
        SCHEDULE,
        scheduled,
        settings.BACKUP_CRON,
        "Back up the database and attachment files (encrypted), then prune old sets",
    )


# ---- status and the key acknowledgement ---------------------------------------------------------------------------


def status(now=None) -> dict[str, Any]:
    """The System page's backup block (core/system_views.BackupStatusSerializer). Nothing secret."""
    now = now or timezone.now()
    row = BackupState.objects.filter(pk=1).first()
    dest = destination()
    stale_after = timedelta(hours=settings.BACKUP_STALE_HOURS)
    last = row.last_success_at if row else None
    if dest is None:
        state = "not_configured"
    elif last is None:
        state = "never"
    else:
        state = "ok" if now - last <= stale_after else "stale"
    present = bool(key_value())
    source = key_source()
    acknowledged = source == "configured" or bool(
        row and row.key_fingerprint and row.key_fingerprint == key_fingerprint()
    )
    job = active_job()
    return {
        "state": state,
        "destination": str(dest) if dest else "",
        "cron": settings.BACKUP_CRON,
        "last_success_at": last,
        "age_seconds": max(0, int((now - last).total_seconds())) if last else None,
        "stale_after_seconds": int(stale_after.total_seconds()),
        "last_set": row.last_set if row else "",
        "last_size_bytes": row.last_size_bytes if row else None,
        "last_duration_seconds": row.last_duration_seconds if row else None,
        "last_failure_at": row.last_failure_at if row else None,
        "last_failure": (row.last_failure if row else "")[: jobs.ERROR_MAX],
        "running": job is not None and job.status == JobStatus.RUNNING,
        "key_source": source,
        "key_present": present,
        "key_acknowledged": acknowledged,
        "key_acknowledged_at": row.key_acknowledged_at if row else None,
    }


def warnings(block: dict[str, Any]) -> list[dict[str, str]]:
    """System warnings from the backup block. Warnings never change the overall status or `/readyz`."""
    found: list[dict[str, str]] = []
    if block["state"] == "not_configured":
        return found
    if block["state"] == "stale":
        hours = block["age_seconds"] // 3600
        found.append({"code": "backup_stale", "message": f"The last good backup is {hours} hours old."})
    elif block["state"] == "never":
        found.append({"code": "backup_stale", "message": "No backup has completed yet."})
    failed = block["last_failure_at"]
    if failed and (block["last_success_at"] is None or failed > block["last_success_at"]):
        found.append({"code": "backup_failed", "message": "The last backup failed: " + block["last_failure"]})
    if not block["key_present"]:
        found.append({"code": "backup_key_missing", "message": "The backup key file is missing, so backups fail."})
    elif not block["key_acknowledged"]:
        found.append(
            {
                "code": "backup_key_unacknowledged",
                "message": "Backup key is stored on this host: copy it somewhere safe.",
            }
        )
    return found


def acknowledge_key(org, actor) -> bool:
    """An Admin confirms the generated key has been copied off the host. The key is installation-wide, so the event
    is audited in every organization: in `org` (the one the request came
    from) with the acting Admin, in the others with no actor and `via: another_admin`, so no organization's log
    names a person from another. `org` None: platform staff acknowledged it from the platform
    console (catalog/platform_admin.acknowledge_backup_key), recorded in every organization with no actor and
    `via: platform`. Idempotent (a repeat for the same key writes nothing). Returns whether anything changed."""
    from catalog.services import audit

    from .models import Organization
    from .problems import Conflict

    if key_source() == "configured":
        raise Conflict(
            "The backup key is set with SOFTWARDEN_BACKUP_KEY_FILE; there is no generated key to acknowledge."
        )
    fingerprint = key_fingerprint()
    if not fingerprint:
        raise Conflict("There is no backup key yet. Start the stack (the init service generates it) and try again.")
    with transaction.atomic():
        row = _state_row()
        if row.key_fingerprint == fingerprint:
            return False
        row.key_fingerprint, row.key_acknowledged_at, row.key_acknowledged_by = fingerprint, timezone.now(), actor
        row.save()
        for each in Organization.objects.order_by("id"):
            if org is None:
                audit(each, None, "backup.key_acknowledged", None, {"key_source": "generated", "via": "platform"})
            elif each.pk == org.pk:
                audit(each, actor, "backup.key_acknowledged", None, {"key_source": "generated"})
            else:  # the actor is another organization's Admin: named only in their own organization's log
                audit(
                    each,
                    None,
                    "backup.key_acknowledged",
                    None,
                    {"key_source": "generated", "via": "another_admin"},
                )
    return True


def wait_for(job: Job, timeout: float, poll: float = 2.0) -> Job:
    """Poll a job until it finishes or `timeout` seconds pass (backup_now)."""
    deadline = time.monotonic() + timeout
    while True:
        job.refresh_from_db()
        if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.ABANDONED):
            return job
        if time.monotonic() >= deadline:
            return job
        time.sleep(poll)
