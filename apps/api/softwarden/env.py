"""Secrets for settings: the environment first, then the file the `init` service generated (softwarden/init_secrets.py).

Each secret has an environment variable and a file name in the secrets volume (SOFTWARDEN_SECRETS_DIR, default
/data/secrets in the container). A value in the environment (or `.env`) always wins; otherwise the generated file is
read; otherwise the caller's default applies. Imports nothing from Django, so settings.py can use it."""

import os
from pathlib import Path

DEFAULT_SECRETS_DIR = "/data/secrets"


def secrets_dir() -> Path:
    return Path(os.environ.get("SOFTWARDEN_SECRETS_DIR") or DEFAULT_SECRETS_DIR)


def read_secret_file(filename: str) -> str:
    """The stripped content of a generated secret file, or "" when it is missing or unreadable."""
    try:
        return (secrets_dir() / filename).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def secret(name: str, filename: str, default: str = "") -> str:
    """os.environ[name] when set and non-empty, else the generated file, else `default`."""
    value = (os.environ.get(name) or "").strip()
    return value or read_secret_file(filename) or default


SELFHOSTED, CLOUD = "selfhosted", "cloud"
DEPLOYMENTS = (SELFHOSTED, CLOUD)


def deployment() -> str:
    """The installation mode, from SOFTWARDEN_DEPLOYMENT (docs/CLOUD.md): `selfhosted` (the default: one team's
    own server) or `cloud` (the hosted, multi-organization service). It changes installation defaults and what
    `doctor` and first-run setup expect; it never sets an organization's edition (core/capabilities.py does)."""
    raw = (os.environ.get("SOFTWARDEN_DEPLOYMENT") or "").strip().lower()
    if not raw:
        return SELFHOSTED
    if raw not in DEPLOYMENTS:
        raise ValueError(f"SOFTWARDEN_DEPLOYMENT must be selfhosted or cloud, not {raw!r}")
    return raw


def db_conn_max_age(default: int = 0) -> int:
    """Seconds a database connection is kept for reuse (Django's CONN_MAX_AGE), from SOFTWARDEN_DB_CONN_MAX_AGE.

    Unset: `default`, which settings.py makes 60: a new connection per request costs a TCP and
    authentication handshake (TLS too for a managed server) and leaves a socket in TIME_WAIT, which ran Windows short
    of ephemeral ports under load. Under DEBUG it is 0 (Django's development server starts a thread per request, so a
    kept connection would outlive its thread), except in cloud mode. The Playwright API servers set it so a page's requests reuse one connection per server thread instead of
    opening hundreds per minute, which ran Windows short of ephemeral ports and made
    the e2e runs flaky. A value that isn't a whole number of seconds >= 0 is refused."""
    raw = (os.environ.get("SOFTWARDEN_DB_CONN_MAX_AGE") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"SOFTWARDEN_DB_CONN_MAX_AGE must be a whole number of seconds, not {raw!r}") from None
    if value < 0:
        raise ValueError("SOFTWARDEN_DB_CONN_MAX_AGE must be 0 or more")
    return value


def db_options() -> dict[str, str]:
    """Connection OPTIONS: `-c jit=off` for the application's sessions (JIT compilation cost the dashboard's
    data-quality query about 40 ms a load, and every query here is short). SOFTWARDEN_DB_JIT=on leaves the server's
    setting alone and sends no `options` startup parameter, which PgBouncer and other poolers (Azure Database for
    PostgreSQL's built-in one included) refuse; turn JIT off for the role there instead (install guide)."""
    raw = (os.environ.get("SOFTWARDEN_DB_JIT") or "").strip().lower()
    if raw in ("", "off"):
        options = {"options": "-c jit=off"}
    elif raw == "on":
        options = {}
    else:
        raise ValueError(f"SOFTWARDEN_DB_JIT must be on or off, not {raw!r}")
    return {**options, **db_tls_options()}


# libpq's sslmode values, weakest first. `require` and stronger refuse an unencrypted connection.
SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
TLS_REQUIRED = frozenset({"require", "verify-ca", "verify-full"})


def db_tls_options() -> dict[str, str]:
    """libpq TLS parameters for the app's connection: POSTGRES_SSLMODE (one of SSLMODES) and POSTGRES_SSLROOTCERT (the
    path of the server's CA bundle, for verify-ca and verify-full). Unset, nothing is sent and libpq's own PGSSLMODE /
    PGSSLROOTCERT (or its default, `prefer`) apply, as before. core/backup.py passes the same values to pg_dump and
    pg_restore."""
    options = {}
    mode = (os.environ.get("POSTGRES_SSLMODE") or "").strip().lower()
    if mode:
        if mode not in SSLMODES:
            raise ValueError(f"POSTGRES_SSLMODE must be one of {', '.join(SSLMODES)}, not {mode!r}")
        options["sslmode"] = mode
    root = (os.environ.get("POSTGRES_SSLROOTCERT") or "").strip()
    if root:
        options["sslrootcert"] = root
    return options


def env_flag(name: str, default: bool) -> bool:
    """A yes/no environment setting: 0, false, no or off (any case) mean no, any other value yes; unset or empty means
    `default`."""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")
