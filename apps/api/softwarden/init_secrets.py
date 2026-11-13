"""Generate this installation's own secrets on first start.

compose.yaml's one-shot `init` service runs `python -m softwarden.init_secrets` before PostgreSQL and the app start.
Each secret is a file in the secrets volume (SOFTWARDEN_SECRETS_DIR, /data/secrets):

    django_secret_key   Django's SECRET_KEY                       (environment: SOFTWARDEN_SECRET_KEY)
    postgres_password   the database password; PostgreSQL reads   (environment: POSTGRES_PASSWORD)
                        it through POSTGRES_PASSWORD_FILE, from a
                        copy in a volume of its own
                        (SOFTWARDEN_DB_SECRETS_DIR) that holds
                        only this file
    credential_keys     the Fernet key that encrypts UI-entered   (environment: SOFTWARDEN_CREDENTIAL_KEYS or
                        secrets (core/credentials.py)              SOFTWARDEN_CREDENTIAL_KEYS_FILE)
    backup_key          the key the worker's backups are          (environment: SOFTWARDEN_BACKUP_KEY_FILE, a path)
                        encrypted with (core/backup.py, openssl
                        -pass file:); the System page warns until
                        an Admin acknowledges a copy is off the host
    ops_token           the bearer token for /healthz/ops, the    (environment: SOFTWARDEN_OPS_TOKEN)
                        operator health view (softwarden/ops.py)

Rules, so that a re-run or an upgrade never breaks anything:
- A value set in the environment (or `.env`) wins and nothing is generated for it. The database password is the
  exception: PostgreSQL reads only the file, so an environment value is copied into it, but only while there is no
  file yet or no database yet. PostgreSQL ignores the file once the database exists, so replacing it then would lose
  the only copy of the real password; a different environment value stops start-up instead.
- An existing file is never replaced (except the database password before the database exists).
- A database that already exists (PG_VERSION in its data directory) with no password anywhere stops start-up: a new
  password could not open it.

Nothing secret is printed, only what was done. Files are 0600 and belong to the app user (APP_UID). Doesn't import
Django, so it runs before the database exists and without settings.
"""

import base64
import os
import secrets
import sys
from collections.abc import Mapping
from pathlib import Path

# The runtime user of the app image (Dockerfile: `useradd --uid 10001 softwarden`), which reads these files.
APP_UID = 10001

# File names in the secrets volume (not values).
DJANGO_SECRET_KEY = "django_secret_key"  # noqa: S105
POSTGRES_PASSWORD = "postgres_password"  # noqa: S105
CREDENTIAL_KEYS = "credential_keys"
BACKUP_KEY = "backup_key"
OPS_TOKEN = "ops_token"  # noqa: S105
FILES = (DJANGO_SECRET_KEY, POSTGRES_PASSWORD, CREDENTIAL_KEYS, BACKUP_KEY, OPS_TOKEN)


class SetupError(Exception):
    """Start-up must stop; the message says what the operator does next and contains no secret."""


def new_token() -> str:
    return secrets.token_urlsafe(48)


def new_password() -> str:
    return secrets.token_urlsafe(24)


def new_fernet_key() -> str:
    """A Fernet key (32 random bytes, url-safe base64), made without importing cryptography."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _get(environ: Mapping[str, str], name: str) -> str:
    return (environ.get(name) or "").strip()


def _write(path: Path, value: str) -> None:
    """Write atomically, 0600, owned by the app user when running as root (the init container)."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    if sys.platform != "win32" and os.geteuid() == 0:
        os.chown(tmp, APP_UID, -1)
    os.replace(tmp, path)


def ensure(
    directory: str | Path,
    environ: Mapping[str, str],
    pgdata: str | Path | None = None,
    database_dir: str | Path | None = None,
) -> list[str]:
    """Create the missing secret files in `directory`. Returns one line per thing done (never a value). `pgdata` is
    the PostgreSQL data directory, mounted read-only, to tell a new database from an existing one. `database_dir` is
    the database container's own secrets volume: it gets a copy of postgres_password and nothing else, so the db
    container never mounts the credential, backup and Django keys."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    database_exists = bool(pgdata) and (Path(str(pgdata)) / "PG_VERSION").exists()
    done = []

    path = directory / DJANGO_SECRET_KEY
    if not _get(environ, "SOFTWARDEN_SECRET_KEY") and not path.exists():
        _write(path, new_token())
        done.append("Generated the Django secret key.")

    path = directory / POSTGRES_PASSWORD
    configured = _get(environ, "POSTGRES_PASSWORD")
    if configured:
        current = path.read_text(encoding="utf-8").strip() if path.exists() else None
        if current is not None and current != configured and database_exists:
            raise SetupError(
                "POSTGRES_PASSWORD in the environment or .env differs from the password this database was created "
                "with (kept in the secrets volume as postgres_password). The database keeps its original password, so "
                "the file is left as it is. Remove POSTGRES_PASSWORD (for example run without compose.dev.yaml), or "
                "set it to the database's password, and start again."
            )
        if current != configured:
            _write(path, configured)
            done.append("Using POSTGRES_PASSWORD from the environment for the database.")
    elif not path.exists():
        if database_exists:
            raise SetupError(
                "The database already exists, but its password is missing (no POSTGRES_PASSWORD in the environment "
                "or .env, and no generated password in the secrets volume). A new password couldn't open it. Set "
                "POSTGRES_PASSWORD to the database's current password (an earlier development stack used "
                "softwarden-dev) in .env or the shell, and start again."
            )
        _write(path, new_password())
        done.append("Generated the database password.")

    if database_dir is not None:
        # Re-copied on every run, so it follows the file above (and an install from before the copy gets one).
        password = path.read_text(encoding="utf-8").strip()
        copy = Path(database_dir) / POSTGRES_PASSWORD
        Path(database_dir).mkdir(parents=True, exist_ok=True)
        if not copy.exists() or copy.read_text(encoding="utf-8").strip() != password:
            _write(copy, password)
            done.append("Copied the database password into the database's own secrets volume.")

    path = directory / CREDENTIAL_KEYS
    has_key = _get(environ, "SOFTWARDEN_CREDENTIAL_KEYS") or _get(environ, "SOFTWARDEN_CREDENTIAL_KEYS_FILE")
    if not has_key and not path.exists():
        _write(path, new_fernet_key())
        done.append(
            "Generated the credential encryption key. Back it up off this server: run "
            "`docker compose exec app python manage.py bootstrap_admin` (docs/DEVELOPMENT.md, 'Credential encryption keys')."
        )
        if database_exists:
            done.append(
                "The database already existed: secrets saved there under an earlier key need that key back (add it "
                "after the new one in SOFTWARDEN_CREDENTIAL_KEYS, or keep SOFTWARDEN_SECRETS_KEY set)."
            )

    path = directory / BACKUP_KEY
    if not _get(environ, "SOFTWARDEN_BACKUP_KEY_FILE") and not path.exists():
        _write(path, new_token())
        done.append(
            "Generated the backup encryption key. Copy it off this server (backup runbook, 'The key'); the System "
            "page warns until an Admin confirms."
        )

    path = directory / OPS_TOKEN
    if not _get(environ, "SOFTWARDEN_OPS_TOKEN") and not path.exists():
        _write(path, new_token())
        done.append(
            "Generated the /healthz/ops token (read it with `docker compose exec app cat /data/secrets/ops_token`)."
        )
    return done


def main() -> int:
    directory = os.environ.get("SOFTWARDEN_SECRETS_DIR") or "/data/secrets"
    try:
        done = ensure(
            directory,
            os.environ,
            os.environ.get("SOFTWARDEN_PGDATA") or "/pgdata",
            os.environ.get("SOFTWARDEN_DB_SECRETS_DIR") or None,
        )
    except SetupError as exc:
        print(f"softwarden init: {exc}", file=sys.stderr)
        return 1
    for line in done or ["Secrets are in place; nothing changed."]:
        print(f"softwarden init: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
