"""Installation checks for `manage.py doctor`. Each check is `ok`, `warn` or `fail` with one short message and, unless ok, a fix hint.

It reuses what already decides each fact rather than re-deriving it: the readiness probe's database and migration check
(softwarden/ops.py), the worker and schedule states and the backup block (core/health.py, core/backup.py), first-run
setup (core/bootstrap.py), the credential keyring (core/credentials.py) and each organization's single sign-on state
(core/sso.py). `setup_status` reads the same `setup_facts`.

Nothing secret is ever part of a result: no key, password, token or client secret, nor anything derived from one
(not even a key id). Values shown are names, counts, host names, paths and states. tests/test_doctor.py seeds every
secret the installation can hold and checks the output for each.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.db import connection
from django.http.request import validate_host
from django.utils import timezone

OK, WARN, FAIL = "ok", "warn", "fail"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
# Fix hints name the generic step first, then the Docker Compose command (the primary install, docs/INSTALL.md).
COMPOSE_APP = "docker compose exec app python manage.py"
COMPOSE_WORKER = "docker compose exec worker python manage.py"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    message: str
    fix: str = ""
    organization: str = ""  # the organization's slug, for per-organization checks

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def overall(checks: list[Check]) -> str:
    states = {c.status for c in checks}
    return FAIL if FAIL in states else WARN if WARN in states else OK


# ---- setup facts (shared with `setup_status`) -------------------------------------------------------------------------


def migrated() -> bool:
    """The app's tables exist (the first migration ran)."""
    from .models import BootstrapState

    return BootstrapState._meta.db_table in connection.introspection.table_names()


def setup_facts() -> dict[str, int]:
    """{migrated, setup_pending, encrypted_secrets}: the setup scripts' facts. Reads no secret value."""
    from . import bootstrap, credentials

    if not migrated():
        return {"migrated": 0, "setup_pending": 1, "encrypted_secrets": 0}
    return {"migrated": 1, "setup_pending": int(bootstrap.pending()), "encrypted_secrets": credentials.stored_count()}


# ---- installation-wide checks -----------------------------------------------------------------------------------------


def check_database() -> list[Check]:
    # The readiness probe's own check (softwarden/ops.py), so doctor and /readyz can't disagree. It is private there;
    # a change to its return shape ({"database": ..., "migrations": ...}, ok) must update this function.
    from softwarden.ops import _database_and_migrations

    db = settings.DATABASES["default"]
    where = f"{db.get('NAME')} on {db.get('HOST') or 'local socket'}:{db.get('PORT') or '5432'}"
    checks, _ok = _database_and_migrations()
    if checks.get("database") != "ok":
        return [
            Check(
                "database",
                FAIL,
                f"PostgreSQL doesn't answer ({where}).",
                "Check that PostgreSQL runs, and POSTGRES_HOST, POSTGRES_PORT and the password "
                "(Compose: docker compose up -d db; docker compose logs db app).",
            )
        ]
    found = [Check("database", OK, f"PostgreSQL answers ({where}).")]
    if checks.get("migrations") != "ok":
        found.append(
            Check(
                "migrations",
                FAIL,
                "Migrations are pending.",
                "manage.py migrate (Compose: the app applies them as it starts, docker compose up -d app; Helm: the "
                "migration job runs on every upgrade).",
            )
        )
    else:
        found.append(Check("migrations", OK, "Every migration is applied."))
    return found


def _platform_staff_exists() -> bool:
    from .models import PlatformStaff

    return PlatformStaff.objects.exists()


def check_setup() -> Check:
    from .models import Organization

    facts = setup_facts()
    if settings.SOFTWARDEN_CLOUD and not _platform_staff_exists():
        return Check(
            "setup",
            WARN,
            "First-run setup is still open: no platform staff account exists yet (hosted installation, no web setup).",
            "manage.py bootstrap_admin --create makes the operator's platform staff account (Kubernetes: kubectl exec "
            "-it deploy/softwarden-app -- python manage.py bootstrap_admin --create; docs/CLOUD.md).",
        )
    if facts["setup_pending"]:
        return Check(
            "setup",
            WARN,
            "First-run setup is still open: no organization or account exists yet.",
            "manage.py bootstrap_admin prints the setup address and its one-time token "
            f"(Compose: {COMPOSE_APP} bootstrap_admin; docs/INSTALL.md).",
        )
    orgs = Organization.objects.count()
    return Check(
        "setup",
        OK,
        f"Done: {orgs} organization{'s' if orgs != 1 else ''}, {facts['encrypted_secrets']} stored secret"
        f"{'s' if facts['encrypted_secrets'] != 1 else ''} (encrypted).",
    )


def check_mode() -> Check:
    from softwarden.settings import DEV_SECRET_KEY

    if settings.DEBUG:
        weak = " with the development secret key" if settings.SECRET_KEY == DEV_SECRET_KEY else ""
        return Check(
            "mode",
            WARN,
            f"Development mode (SOFTWARDEN_DEBUG on){weak}.",
            "Never on a server: the image sets SOFTWARDEN_DEBUG=0; remove it from .env.",
        )
    return Check("mode", OK, "Production mode (SOFTWARDEN_DEBUG off).")


def check_dns_resolver() -> Check:
    """The resolver sign-in domain verification uses (core/dns_txt.py): `simulated` outside development verifies
    nothing real (only *.dns-verified.test), so cloud organizations could never verify their domains."""
    from . import dns_txt

    value = dns_txt.resolver_setting()
    if value == "simulated":
        if settings.DEBUG:
            return Check("dns_resolver", OK, "Simulated (development): no DNS lookups are made.")
        return Check(
            "dns_resolver",
            WARN,
            "SOFTWARDEN_DNS_RESOLVER is `simulated` with SOFTWARDEN_DEBUG off: sign-in domains can't be verified.",
            "Remove SOFTWARDEN_DNS_RESOLVER (the host's /etc/resolv.conf is used) or set it to a resolver, such as "
            "10.0.0.2 or 10.0.0.2:53.",
        )
    return Check("dns_resolver", OK, f"TXT lookups go to {value or 'the first nameserver in /etc/resolv.conf'}.")


def check_base_url() -> Check:
    """SOFTWARDEN_BASE_URL (links in email, the SSO redirect address) against the host the app accepts and the TLS
    name. PUBLIC_HOSTNAME itself is read by compose, which derives these settings from it."""
    url = settings.SOFTWARDEN_BASE_URL
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    fix_host = (
        "Set PUBLIC_HOSTNAME to the name people open (Compose: in .env, then docker compose up -d; Helm: "
        "publicHostname; docs/ENV.md)."
    )
    if parts.scheme not in ("http", "https") or not host:
        return Check("base_url", FAIL, f"SOFTWARDEN_BASE_URL isn't an http(s) address ({url!r}).", fix_host)
    if not validate_host(host, settings.ALLOWED_HOSTS):
        return Check(
            "base_url",
            FAIL,
            f"{url}: the app refuses requests for {host} (not in SOFTWARDEN_ALLOWED_HOSTS).",
            fix_host,
        )
    local = host in LOCAL_HOSTS
    if settings.DEBUG:
        return Check("base_url", OK, f"{url} (development).")
    if parts.scheme != "https":
        return Check(
            "base_url",
            WARN,
            f"{url} isn't HTTPS: sign-in needs secure cookies, and links in email would be plain HTTP.",
            "Leave SOFTWARDEN_BASE_URL unset (it follows PUBLIC_HOSTNAME as https://), or give its https:// address.",
        )
    if not settings.SESSION_COOKIE_SECURE:
        return Check(
            "base_url",
            WARN,
            f"{url}: cookies aren't marked Secure (SOFTWARDEN_SECURE_COOKIES is off).",
            "Remove SOFTWARDEN_SECURE_COOKIES=0 from the environment (it defaults to 1 when DEBUG is off).",
        )
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in settings.CSRF_TRUSTED_ORIGINS:
        return Check(
            "base_url",
            WARN,
            f"{origin} isn't in SOFTWARDEN_CSRF_TRUSTED_ORIGINS.",
            "Leave SOFTWARDEN_CSRF_TRUSTED_ORIGINS unset (it follows PUBLIC_HOSTNAME), or add this origin to it.",
        )
    if local:
        return Check(
            "base_url",
            WARN,
            f"{url}: the TLS certificate and every link are for localhost, which only this machine can open.",
            fix_host,
        )
    return Check("base_url", OK, f"{url} (HTTPS, host accepted, secure cookies).")


def check_worker(now) -> list[Check]:
    from . import health

    worker = health.worker_check(now)
    found = []
    if worker["state"] == "ok":
        found.append(Check("worker", OK, f"Heartbeat {worker['age_seconds']} s ago."))
    else:
        message = (
            "No worker has ever ticked against this database."
            if worker["state"] == "never"
            else f"The last heartbeat was {worker['age_seconds']} s ago (stale after {worker['stale_after_seconds']} s)."
        )
        found.append(
            Check(
                "worker",
                FAIL,
                message + " Digests, connector runs and backups wait for it.",
                "Start the worker and read its log (Compose: docker compose up -d worker; docker compose logs worker).",
            )
        )
    checks = health.schedule_checks(now)
    by_state: dict[str, list[str]] = {}
    for c in checks:
        by_state.setdefault(c["state"], []).append(c["name"])
    bad = {s: by_state[s] for s in ("stuck", "overdue") if s in by_state}
    if bad:
        found.append(
            Check(
                "schedules",
                FAIL,
                "; ".join(f"{state}: {', '.join(names)}" for state, names in bad.items()) + ".",
                "Admin > System status shows each schedule's last start and error; the worker log has the message.",
            )
        )
    elif "failing" in by_state:
        found.append(
            Check(
                "schedules",
                WARN,
                f"Last run failed: {', '.join(by_state['failing'])}.",
                "Admin > System status shows the error type; the worker log has the message.",
            )
        )
    else:
        found.append(Check("schedules", OK, f"{len(checks)} schedules, none failing, overdue or stuck."))
    return found


# How to move off the public development credential key.
DEV_KEY_ROTATION = (
    "Put a new key first in SOFTWARDEN_CREDENTIAL_KEYS (manage.py generate_credential_key), keep the development key "
    "after it, and run manage.py rotate_credential_keys."
)


def check_credential_keys(with_db: bool = True) -> list[Check]:
    from . import credentials

    state = credentials.status()
    if not state["available"]:
        hint = (
            DEV_KEY_ROTATION
            if state["error"] == credentials.DEV_KEY_REFUSED  # the development key is configured alone
            else "Set SOFTWARDEN_CREDENTIAL_KEYS or SOFTWARDEN_CREDENTIAL_KEYS_FILE (docs/ENV.md); on Compose the init "
            "service generates one (docker compose up -d)."
        )
        return [Check("credential_keys", FAIL, state["error"] or "No credential encryption key is configured.", hint)]
    source = {"generated": "generated, in the secrets volume", "development": "the development key"}.get(
        state["source"], state["source"]
    )
    found = []
    if state["source"] == "development":
        found.append(
            Check(
                "credential_keys",
                WARN,
                "Secrets are encrypted with the fixed development key.",
                "Development only. On a server the init service generates a key.",
            )
        )
    else:
        found.append(Check("credential_keys", OK, f"{state['count']} key(s) ({source})."))
    on_dev_key = credentials.count_under_key(credentials.DEV_KEY_ID) if with_db and not settings.DEBUG else 0
    if on_dev_key:  # A database dump of these is effectively plaintext
        found.append(
            Check(
                "stored_secrets_dev_key",
                FAIL,
                f"{on_dev_key} stored secret(s) are encrypted under the public development credential key.",
                DEV_KEY_ROTATION,
            )
        )
    unreadable = credentials.unreadable_count() if with_db else 0
    if unreadable:
        found.append(
            Check(
                "stored_secrets",
                WARN,
                f"{unreadable} stored secret(s) were encrypted under a key that isn't configured; they can't be read.",
                "After a restore, bring the old installation's credential_keys from its off-host copy and put them "
                "after the current key in SOFTWARDEN_CREDENTIAL_KEYS (backup runbook), or enter those secrets again.",
            )
        )
    return found


def check_backup(now) -> list[Check]:
    from . import backup

    block = backup.status(now)
    if block["state"] == "not_configured":
        return [
            Check(
                "backup",
                WARN,
                "Backups aren't configured (SOFTWARDEN_BACKUP_DIR is empty; the image sets /data/backups).",
                "Set SOFTWARDEN_BACKUP_DIR to a mounted volume (the image sets /data/backups; Helm: "
                "persistence.backups.enabled), or take backups with ops/backup.sh (backup runbook).",
            )
        ]
    found = []
    if not block["key_present"]:
        found.append(
            Check(
                "backup_key",
                FAIL,
                "The backup key file is missing, so every backup fails.",
                "Check SOFTWARDEN_BACKUP_KEY_FILE, or the backup_key in the secrets (Compose: docker compose up -d, the "
                "init service generates it; Helm: the keys Secret).",
            )
        )
    elif not block["key_acknowledged"]:
        found.append(
            Check(
                "backup_key",
                WARN,
                "The generated backup key is stored only on this host.",
                "Copy it off the host (docs/INSTALL.md, step 6), then confirm on Admin > System status, or "
                "POST /api/v1/system/backup/acknowledge-key with an Admin's API token.",
            )
        )
    else:
        source = "SOFTWARDEN_BACKUP_KEY_FILE" if block["key_source"] == "configured" else "copy confirmed off the host"
        found.append(Check("backup_key", OK, f"Present ({source})."))
    take_one = f"manage.py backup_now --inline in the worker (Compose: {COMPOSE_WORKER} backup_now --inline)"
    if block["state"] == "never":
        found.append(Check("backup", WARN, "No backup has completed yet.", take_one))
    elif block["state"] == "stale":
        hours = (block["age_seconds"] or 0) // 3600
        found.append(
            Check(
                "backup",
                WARN,
                f"The last good backup ({block['last_set']}) is {hours} hours old.",
                "Check Admin > System status and the worker log, then take one: " + take_one,
            )
        )
    else:
        hours = (block["age_seconds"] or 0) // 3600
        found.append(Check("backup", OK, f"Last good backup {block['last_set']}, {hours} h ago."))
    failed = block["last_failure_at"]
    if failed and (block["last_success_at"] is None or failed > block["last_success_at"]):
        found.append(
            Check(
                "backup_last_run",
                WARN,
                "The last backup failed: " + block["last_failure"],
                "The worker log has the details (Compose: docker compose logs worker | grep backup; backup runbook).",
            )
        )
    return found


def check_email() -> Check:
    from notify.models import EmailSettings, EmailTransport

    from .models import Organization

    if settings.SOFTWARDEN_EMAIL_CONFIGURED:
        if settings.SMTP_HOST:
            return Check("email", OK, f"Installation default: SMTP {settings.SMTP_HOST}:{settings.EMAIL_PORT}.")
        return Check("email", OK, f"Installation default: {settings.EMAIL_BACKEND}.")
    own = EmailSettings.objects.exclude(transport=EmailTransport.INSTALLATION_DEFAULT).values("organization_id")
    relying = Organization.objects.exclude(pk__in=own).count()
    if not relying:
        return Check("email", OK, "No installation default; every organization sends through its own settings.")
    return Check(
        "email",
        WARN,
        f"Email is off for {relying} organization(s): digests and reminders are recorded as not sent (actions stay "
        "in Needs attention).",
        "Set SOFTWARDEN_SMTP_HOST and the other SMTP settings (docs/ENV.md), or choose Graph or SMTP in Admin > "
        "Email settings.",
    )


# ---- hosted (cloud) installation -------------------------------------------------------------------------------------
# Only with SOFTWARDEN_DEPLOYMENT=cloud (docs/CLOUD.md). The worker's heartbeat is the `worker` check above, which
# every installation runs.

CLOUD_GUIDE = "docs/CLOUD.md"
LIBPQ_SSLMODE = "PGSSLMODE"  # libpq's own variables, documented in ENV.md's prose with the other PG* ones
LIBPQ_SSLROOTCERT = "PGSSLROOTCERT"


def database_tls() -> tuple[str, bool]:
    """(the sslmode the app's connection asked for, whether this connection is encrypted). The mode is
    POSTGRES_SSLMODE (the connection's options), else libpq's PGSSLMODE; with neither, libpq 16+ defaults to
    verify-full when the root certificate is `system` (POSTGRES_SSLROOTCERT or PGSSLROOTCERT), and otherwise to
    `prefer`, which accepts an unencrypted connection. Encryption is the server's own answer for this session
    (pg_stat_ssl). Behind a session-mode pooler that is the pooler-to-server leg, not the app-to-pooler one."""
    db: Any = settings.DATABASES["default"]
    options = db.get("OPTIONS") or {}
    mode = str(options.get("sslmode") or "").strip().lower() or (os.environ.get(LIBPQ_SSLMODE) or "").strip().lower()
    if not mode:
        root = str(options.get("sslrootcert") or os.environ.get(LIBPQ_SSLROOTCERT) or "").strip()
        mode = "verify-full" if root == "system" else "prefer"
    with connection.cursor() as cursor:
        cursor.execute("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
        row = cursor.fetchone()
    return mode, bool(row and row[0])


def check_cloud_database() -> Check:
    from softwarden.env import TLS_REQUIRED

    mode, encrypted = database_tls()
    fix = (
        "Set POSTGRES_SSLMODE=require (or verify-full with POSTGRES_SSLROOTCERT, the server's CA bundle, or `system`) for "
        f"app, worker and the migration job (Helm: database.sslmode, database.sslrootcert; {CLOUD_GUIDE}). Behind a "
        "session-mode pooler this check sees the pooler-to-server connection: turn on TLS between the app and the pooler "
        "on the pooler too."
    )
    if mode not in TLS_REQUIRED:
        state = "encrypted now" if encrypted else "not encrypted"
        return Check(
            "cloud: database TLS",
            FAIL,
            f"sslmode is {mode}, which would accept an unencrypted connection ({state}).",
            fix,
        )
    if not encrypted:
        return Check("cloud: database TLS", FAIL, f"sslmode is {mode} but this connection isn't encrypted.", fix)
    return Check("cloud: database TLS", OK, f"Encrypted (sslmode {mode}).")


def check_cloud_email() -> Check:
    from notify import transactional

    if not transactional.configured():
        return Check(
            "cloud: email",
            FAIL,
            "The installation can't send email, so signup refuses (it confirms every address) and invitations fall back "
            "to copy-once links.",
            "Set SOFTWARDEN_SMTP_HOST and the other SMTP settings (Helm: smtp.*, the password in smtp.existingSecret; "
            f"{CLOUD_GUIDE}).",
        )
    return Check("cloud: email", OK, "The installation can send email (signup confirmations, invitations).")


def check_cloud_signup() -> Check:
    if not settings.SOFTWARDEN_SIGNUP_ENABLED:
        return Check(
            "cloud: signup",
            WARN,
            "Public signup is off (SOFTWARDEN_SIGNUP_ENABLED): only invitations add people and organizations.",
            "Leave SOFTWARDEN_SIGNUP_ENABLED unset in cloud mode (it defaults on), unless signup is closed on purpose.",
        )
    return Check("cloud: signup", OK, "Public signup is on.")


def check_cloud_credential_keys() -> Check:
    from . import credentials

    state = credentials.status()
    if not state["available"]:
        return Check(
            "cloud: credential keys",
            FAIL,
            "No credential encryption key is configured.",
            f"Provide SOFTWARDEN_CREDENTIAL_KEYS_FILE from a Secret (Helm: secrets.existingSecret; {CLOUD_GUIDE}).",
        )
    if state["source"] not in (credentials.KEYS_ENV, credentials.KEYS_FILE_ENV):
        where = "the fixed development key" if state["source"] == "development" else "a key generated into a volume"
        return Check(
            "cloud: credential keys",
            FAIL,
            f"Secrets are encrypted with {where}. A hosted installation's key comes from your secret store, so that "
            "losing a volume can't make every organization's stored secrets unreadable.",
            "Create the key yourself and provide it as SOFTWARDEN_CREDENTIAL_KEYS_FILE or SOFTWARDEN_CREDENTIAL_KEYS "
            f"(Helm: secrets.existingSecret, which sets the file; {CLOUD_GUIDE}). Keep the old key after the new one "
            "while anything is still encrypted under it.",
        )
    return Check("cloud: credential keys", OK, f"From {state['source']}.")


def check_cloud_base_url() -> Check:
    parts = urlsplit(settings.SOFTWARDEN_BASE_URL)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or host in LOCAL_HOSTS:
        return Check(
            "cloud: base URL",
            FAIL,
            f"SOFTWARDEN_BASE_URL is {settings.SOFTWARDEN_BASE_URL!r}: signup, invitation and reminder links need the "
            "public https:// address.",
            f"Set SOFTWARDEN_BASE_URL to the public https:// address (Helm: publicHostname; {CLOUD_GUIDE}).",
        )
    return Check("cloud: base URL", OK, f"{settings.SOFTWARDEN_BASE_URL} (HTTPS).")


def check_cloud_platform_staff() -> Check:
    from .models import PlatformStaff

    staff = PlatformStaff.objects.filter(user__is_active=True).count()
    if not staff:
        return Check(
            "cloud: platform staff",
            FAIL,
            "No active platform staff account: nobody can use the platform console.",
            "manage.py bootstrap_admin --create on a new installation, or manage.py platform_staff grant <email> for an "
            f"existing account ({CLOUD_GUIDE}).",
        )
    plural = "s" if staff != 1 else ""
    return Check("cloud: platform staff", OK, f"{staff} active platform staff account{plural}.")


def check_cloud_private_hosts() -> Check:
    if settings.SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS:
        return Check(
            "cloud: private service hosts",
            WARN,
            "Organizations may point their SMTP relay or Halo URL at private network addresses, which on a shared "
            "service reach the operator's own network.",
            "Leave SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS unset in cloud mode (it defaults off).",
        )
    return Check("cloud: private service hosts", OK, "Organizations' own servers must be on public addresses.")


def check_cloud_second_factor() -> Check:
    """Platform staff need a TOTP code for the console (catalog/staff_second_factor.py)."""
    from catalog import staff_second_factor

    from .models import PlatformStaff

    name = "cloud: platform second factor"
    if not settings.SOFTWARDEN_PLATFORM_TOTP:
        return Check(
            name,
            FAIL,
            "SOFTWARDEN_PLATFORM_TOTP is off: one staff password opens every organization and impersonation.",
            f"Unset SOFTWARDEN_PLATFORM_TOTP (it defaults on in cloud mode) ({CLOUD_GUIDE}).",
        )
    rows = list(PlatformStaff.objects.filter(user__is_active=True))
    waiting = sum(1 for row in rows if not staff_second_factor.enrolled(row))
    if waiting:
        return Check(
            name,
            WARN,
            f"{waiting} active staff account(s) haven't set up their second factor yet; the console asks them to at "
            "their next sign-in, so whoever signs in first with the password sets it up.",
            "Have each staff member sign in to the console and set it up now (platform admin runbook, Second factor).",
        )
    return Check(name, OK, "Required, and every active staff account has set it up.")


ONE_ADDRESS_MINIMUM = 20  # sign-in attempts still counted before "they all came from one address" means anything


def check_cloud_rate_limits() -> Check:
    """Rate limits count in the shared database counters (core/counters.py), so they hold across pods; what can
    still defeat a per-address limit is every request arriving from one address: no trusted proxy listed (the app then
    sees only the proxy's or the ingress's address), or a load balancer in front of the proxy that isn't trusted. The
    second is read from the sign-in counters still kept (the last ten minutes or so: `counters.prune`)."""
    from .models import RateCounter

    name = "cloud: rate limits"
    if not settings.TRUSTED_PROXIES:
        return Check(
            name,
            WARN,
            "No trusted proxy is listed, so the app sees every request as coming from the ingress or proxy: per-address "
            "limits (sign-in, signup, setup, refused tokens) are shared by every client. Per-account sign-in limits "
            "still apply.",
            "Set SOFTWARDEN_TRUSTED_PROXIES to the ingress controller's or proxy's addresses (Helm: trustedProxies; "
            f"{CLOUD_GUIDE}).",
        )
    rows = list(RateCounter.objects.filter(scope="throttle:login").values_list("key", "count"))
    attempts = sum(count for _, count in rows)
    if attempts >= ONE_ADDRESS_MINIMUM and len({key for key, _ in rows}) == 1:
        return Check(
            name,
            WARN,
            f"The last {attempts} sign-in attempts all came from one client address: a load balancer or proxy in front "
            "of the trusted one is probably being counted as the client, so one person's failures slow everyone's "
            "sign-in.",
            "List that load balancer in SOFTWARDEN_TRUSTED_PROXIES (or in Caddy's `trusted_proxies`, with "
            f"`trusted_proxies_strict`) so the client's own address reaches the app ({CLOUD_GUIDE}).",
        )
    return Check(name, OK, "Counted in the database, shared by every process; client addresses come through the proxy.")


CLOUD_CHECKS = (
    ("cloud: database TLS", check_cloud_database),
    ("cloud: email", check_cloud_email),
    ("cloud: signup", check_cloud_signup),
    ("cloud: credential keys", check_cloud_credential_keys),
    ("cloud: base URL", check_cloud_base_url),
    ("cloud: platform staff", check_cloud_platform_staff),
    ("cloud: private service hosts", check_cloud_private_hosts),
    ("cloud: rate limits", check_cloud_rate_limits),
    ("cloud: platform second factor", check_cloud_second_factor),
)


def check_cloud() -> list[Check]:
    found: list[Check] = []
    for name, fn in CLOUD_CHECKS:
        found += _guarded(name, fn)
    return found


# ---- per organization -------------------------------------------------------------------------------------------------


def check_sso(org) -> Check:
    from . import sso

    state = sso.state(org)
    slug = org.slug
    if sso.local_admins(org) == 0:
        return Check(
            "sso",
            WARN,
            f"{state['detail']} No Admin can sign in with a password.",
            f"manage.py reset_admin <email> (Compose: {COMPOSE_APP} reset_admin <email>; SSO recovery runbook).",
            slug,
        )
    source = state["source"]
    if source == "force_local":
        return Check(
            "sso",
            WARN,
            state["detail"],
            "Break-glass: remove SOFTWARDEN_FORCE_LOCAL_LOGIN once SSO works (SSO recovery runbook).",
            slug,
        )
    if source == "legacy":
        return Check(
            "sso",
            WARN,
            f"Signs in with {state['provider']} through SOFTWARDEN_OIDC_* (deprecated).",
            "Set up sign-in in Admin > Sign-in and activate it; then remove SOFTWARDEN_OIDC_*.",
            slug,
        )
    if source == "off" and state["detail"] != "Not set up.":
        return Check("sso", WARN, state["detail"], "Admin > Sign-in shows the settings and their last test.", slug)
    if source == "active":
        return Check("sso", OK, f"Single sign-on with {state['provider']}.", organization=slug)
    message = "Password sign-in only." if source == "off" else state["detail"]
    return Check("sso", OK, message, organization=slug)


def check_scim_tokens(org) -> list[Check]:
    """Live SCIM tokens refused because the Admin who created them is no longer an active Admin here (they
    answer 401, so the identity provider's provisioning has stopped). A warning while any exists; nothing otherwise."""
    from django.utils import timezone

    from .access import is_active_admin
    from .models import ScimToken

    live = ScimToken.objects.filter(organization=org, revoked_at=None, expires_at__gt=timezone.now())
    stale = [t for t in live.order_by("created_at", "id") if not is_active_admin(t.created_by_id, org.pk)]
    if not stale:
        return []
    names = ", ".join(t.name for t in stale[:5])
    return [
        Check(
            "scim_tokens",
            WARN,
            f"{len(stale)} SCIM token(s) refused because their creator is no longer an active Admin: {names}.",
            "Admin > Provisioning: create a new SCIM token, give it to the identity provider, revoke the old one.",
            org.slug,
        )
    ]


def check_discovery(org) -> list[Check]:
    from discovery.models import Connector, RunStatus

    from . import connections, simulation
    from .capabilities import SOURCE_CAPABILITY, hidden_preview

    slug = org.slug
    connectors = list(
        Connector.objects.filter(organization=org).select_related(
            "microsoft_connection", "halo_connection", "okta_connection", "google_workspace_connection"
        )
    )
    if not connectors:
        return [
            Check(
                "discovery",
                WARN,
                "No discovery source is set up.",
                "Discovery > Add a source, as Admin (docs/FIRST_RUN.md).",
                slug,
            )
        ]
    found = []
    for c in connectors:
        name = f"discovery: {c.name}"
        if not c.enabled:
            found.append(Check(name, OK, "Turned off.", organization=slug))
            continue
        capability = SOURCE_CAPABILITY.get(c.source)
        if capability is not None and hidden_preview(capability):
            # Kept from when preview features were on (launch scope): it never runs, nothing to fix.
            found.append(Check(name, OK, "Unavailable (preview feature off); kept, never run.", organization=slug))
            continue
        conn = c.microsoft_connection or c.halo_connection or c.okta_connection or c.google_workspace_connection
        if connections.is_simulated(conn) and not simulation.allowed():
            # Demo data on a server that refuses simulators; kept, never run.
            found.append(
                Check(
                    name,
                    WARN,
                    "Uses a simulated tenant or host (synthetic data), which this server doesn't allow; kept, never "
                    "run.",
                    f"Delete the connector if it is left from a demo, or set {simulation.FLAG}=1 on a demo server.",
                    slug,
                )
            )
            continue
        if conn is None or not conn.test_current:
            found.append(
                Check(
                    name,
                    WARN,
                    "Its connection hasn't passed a test since it last changed; no run will use it.",
                    "Admin > Connections: Test the connection.",
                    slug,
                )
            )
            continue
        run = c.runs.order_by("-created_at", "-id").first()
        simulated = " (simulated tenant)" if connections.is_simulated(conn) else ""
        if run is None:
            found.append(Check(name, WARN, f"Never run{simulated}.", "Discovery: Run now.", slug))
        elif run.status == RunStatus.FAILED:
            when = run.finished_at or run.created_at
            found.append(
                Check(
                    name,
                    WARN,
                    f"The last run failed ({when:%Y-%m-%d %H:%M} UTC){simulated}: {run.error or 'no detail'}",
                    "Discovery > the connector's page shows the run; fix it, then Run now.",
                    slug,
                )
            )
        elif run.status in (RunStatus.QUEUED, RunStatus.RUNNING):
            found.append(Check(name, OK, f"A run is {run.status}{simulated}.", organization=slug))
        else:
            when = run.finished_at or run.created_at
            seen = (run.counts or {}).get("seen")
            counted = f", {seen} seen" if seen is not None else ""
            found.append(
                Check(name, OK, f"Last run succeeded {when:%Y-%m-%d %H:%M} UTC{counted}{simulated}.", organization=slug)
            )
    return found


# ---- all of it --------------------------------------------------------------------------------------------------------


def _guarded(name: str, fn) -> list[Check]:
    """A check that raises (a table a pending migration adds, say) is a fail naming the error type only."""
    try:
        result = fn()
    except Exception as exc:
        return [
            Check(
                name,
                FAIL,
                f"Couldn't check ({type(exc).__name__}).",
                "Apply pending migrations (manage.py migrate), then read the app log.",
            )
        ]
    return result if isinstance(result, list) else [result]


def _tables_exist() -> bool:
    try:
        return migrated()
    except Exception:
        return False


def run(now=None) -> list[Check]:
    """Every check, in order. Without a database (or its tables) only the checks that need none follow."""
    now = now or timezone.now()
    checks = check_database()
    checks += [check_mode(), check_base_url()]
    if checks[0].status == FAIL or not _tables_exist():
        return checks + check_credential_keys(with_db=False)
    checks += _guarded("setup", check_setup)
    checks += _guarded("worker", lambda: check_worker(now))
    checks += _guarded("credential_keys", check_credential_keys)
    checks += _guarded("backup", lambda: check_backup(now))
    checks += _guarded("email", check_email)
    checks += _guarded("dns_resolver", lambda: [check_dns_resolver()])
    if settings.SOFTWARDEN_CLOUD:
        checks += check_cloud()
    checks += _guarded("organizations", _per_organization)
    return checks


def _per_organization() -> list[Check]:
    from .models import Organization

    found: list[Check] = []
    for org in Organization.objects.order_by("name"):
        found += _guarded("sso", lambda org=org: check_sso(org))
        found += _guarded("discovery", lambda org=org: check_discovery(org))
        found += _guarded("scim_tokens", lambda org=org: check_scim_tokens(org))
        found += _guarded("stored_dates", lambda org=org: check_stored_dates(org))
    return found


# The date columns people enter through the API (core/dates.parse_stored holds them to the window), by model label.
# Only these: dates a connector or a job records (a discovered credential's expiry, often 9999-12-31 for a secret that
# never expires; sign-in days; digest and reminder dates; token and import expiries) are observations or derived, are
# not held to the window, and would make this check warn about correct data.
ENTERED_DATE_COLUMNS: dict[str, tuple[str, ...]] = {
    "catalog.Resource": ("last_periodic_review", "next_periodic_review"),
    "catalog.RecordReview": ("confirmation_date", "next_review_date"),
    "catalog.LinkConfirmation": ("confirmed_on",),
    "catalog.ChangeAssessment": ("target_date",),
    "catalog.VendorRiskProfile": (
        "questionnaire_requested_on",
        "questionnaire_reviewed_on",
        "assurance_issued_on",
        "assurance_expires_on",
        "dpa_signed_on",
        "subprocessors_reviewed_on",
    ),
    "catalog.VendorRiskDocument": ("issued_on", "expires_on"),
    "money.RenewalCycle": ("renewal_date", "explicit_deadline", "decided_on", "completed_on"),
    "money.Contract": ("start_date", "end_date"),
}


def _dated_tenant_fields() -> list[tuple[Any, tuple[str, ...]]]:
    from django.apps import apps

    return [(apps.get_model(label), fields) for label, fields in ENTERED_DATE_COLUMNS.items()]


def check_stored_dates(org) -> Check:
    """Stored dates outside the window the API accepts (core/dates.EARLIEST to LATEST). The API refuses them,
    so they come from an older release, a restore or a direct database edit. They no longer break a list, but a
    renewal with one has no deadline and no reminder."""
    from django.db.models import Count, Q

    from .dates import EARLIEST, LATEST

    counts: list[tuple[str, int]] = []
    for model, fields in _dated_tenant_fields():
        outside = {
            name: Count("pk", filter=Q(**{f"{name}__lt": EARLIEST}) | Q(**{f"{name}__gt": LATEST})) for name in fields
        }
        totals = model._base_manager.filter(organization=org).aggregate(**outside)
        for name in fields:
            if totals[name]:
                label = f"{model._meta.verbose_name} {model._meta.get_field(name).verbose_name}"
                counts.append((label, totals[name]))
    window = f"{EARLIEST.isoformat()} to {LATEST.isoformat()}"
    if not counts:
        return Check("stored_dates", OK, f"Every stored date is within {window}.", organization=org.slug)
    total = sum(n for _label, n in counts)
    return Check(
        "stored_dates",
        WARN,
        f"{total} stored date(s) are outside {window}: " + ", ".join(f"{label} ({n})" for label, n in counts) + ".",
        "Correct each date on its record (a renewal shows no deadline and sends no reminder until then); the audit "
        "log has the record. The API refuses such dates, so they came from a restore or a direct database edit.",
        organization=org.slug,
    )
