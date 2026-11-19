import os
import tomllib
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from . import ee as _ee
from .env import (
    CLOUD,
    db_conn_max_age,
    db_options,
    deployment,
    env_flag,
    secret,
    secrets_dir,
)

BASE_DIR = Path(__file__).resolve().parent.parent


def _project_version() -> str:
    """This build's version: apps/api/pyproject.toml's, which the image carries. /healthz/ops and the OpenAPI schema
    report it; docs/DEVELOPMENT.md, "Cutting a release", sets it (tests/test_upgrade_ci.py checks the others agree)."""
    try:
        with (BASE_DIR / "pyproject.toml").open("rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "unknown"


APP_VERSION = _project_version()
# The project's home: the contact address in the User-Agent outbound requests to public
# services send (eol/client.py). Not an installation setting; tests/test_eol.py reads it from here too.
PROJECT_URL = "https://github.com/Softwarden/softwarden"

# Secrets come from the environment first, then from the files the compose `init` service generates in
# SOFTWARDEN_SECRETS_DIR (softwarden/env.py, softwarden/init_secrets.py), so the stack starts with no `.env`.
DEV_SECRET_KEY = "dev-only-insecure-key-change-me"  # noqa: S105
SECRET_KEY = secret("SOFTWARDEN_SECRET_KEY", "django_secret_key") or DEV_SECRET_KEY
# Development mode is opt-in: only SOFTWARDEN_DEBUG=1 turns it on, so a process started any other way
# (a systemd unit, another image) runs in production mode and refuses the development secret key below.
DEBUG = os.environ.get("SOFTWARDEN_DEBUG", "0") == "1"
if not DEBUG and SECRET_KEY == DEV_SECRET_KEY:
    raise ImproperlyConfigured("Set SOFTWARDEN_SECRET_KEY to a long random value (see docs/ENV.md).")
ALLOWED_HOSTS = os.environ.get("SOFTWARDEN_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
# The installation mode (softwarden/env.deployment, docs/CLOUD.md): `selfhosted` (default) or `cloud`, the
# hosted multi-organization service. Cloud turns public signup on and private service hosts off by default, keeps
# database connections open, makes first-run setup create platform staff instead of an organization (core/bootstrap.py)
# and adds the hosted requirements to `manage.py doctor`. Editions stay core/capabilities.py's alone.
SOFTWARDEN_DEPLOYMENT = deployment()
SOFTWARDEN_CLOUD = SOFTWARDEN_DEPLOYMENT == CLOUD

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.postgres",
    "rest_framework",
    "drf_spectacular",
    "django_filters",
    "core",
    "catalog",
    "money",
    "notify",
    "discovery",
    "imports",
    "api",
    "eol",  # end-of-life tracking via endoflife.date (eol/models.py)
    "tickets",  # Jira / ServiceNow / Halo ticket links on assessments and renewals (tickets/models.py)
    # The optional `ee` package (SAML and SCIM), not part of this repository: installed only when present
    # (softwarden/ee.py), and capabilities still turn it on per organization.
    *(["ee.saml", "ee.scim"] if _ee.INSTALLED else []),
]

MIDDLEWARE = [
    # First: drops X-Forwarded-* headers unless they come from a trusted proxy (Caddy in compose).
    "core.middleware.TrustedProxyMiddleware",
    # A request id and audit unit of work per request (X-Request-ID from the trusted proxy, else generated).
    "core.request_context.RequestIdMiddleware",
    # Before anything that could answer or read the body (WhiteNoise, CSRF, which parses a multipart body for session
    # clients): an oversize multipart request on any path is refused unread (core/upload_limit.py).
    "core.upload_limit.RequestSizeLimitMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # The app's own frame protection, CSP and Permissions-Policy, so a deployment without Caddy (Helm, another
    # proxy) has them; before WhiteNoise, so the files it serves get them too (softwarden/security_headers.py).
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "softwarden.security_headers.SecurityHeadersMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # Django's, except that a request sent with a session key rotated away (sign-in, organization switch, impersonation
    # start and stop) leaves the newer cookie alone instead of deleting it (core/sessions.py).
    "core.sessions.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Platform staff impersonating a member: swaps request.user, expires, refuses restricted routes.
    "core.impersonation.Middleware",
    # A NUL character in an /api/ path or query parameter is a 400 naming it, before any filter sees it.
    "core.input.QueryGuardMiddleware",
]

ROOT_URLCONF = "softwarden.urls"
WSGI_APPLICATION = "softwarden.wsgi.application"

# Kept 60 s outside development (env.db_conn_max_age): a connection per request cost a TCP and auth handshake each
# and ran Windows short of ephemeral ports under load. Development servers (runserver: a thread per request) keep none,
# except in cloud mode, which always keeps them (a managed server across a network).
_conn_max_age = db_conn_max_age(default=60 if SOFTWARDEN_CLOUD or not DEBUG else 0)
# The database password: the environment, else the secrets volume (the compose `init` service generates it), else, under
# DEBUG only, the local development database's (compose.dev.yaml). Outside DEBUG a missing password stops start-up
# instead of trying the published development one; `doctor` fails on the development password outside DEBUG.
DEV_DB_PASSWORD = "softwarden-dev"  # noqa: S105
_db_password = secret("POSTGRES_PASSWORD", "postgres_password")
if not _db_password and not DEBUG:
    raise ImproperlyConfigured(
        "Set POSTGRES_PASSWORD (or provide postgres_password in SOFTWARDEN_SECRETS_DIR; Docker Compose's init service "
        "generates it). Outside development there is no default database password (docs/ENV.md)."
    )
# A connection pooler in front of PostgreSQL must run in session mode, for the app and the worker alike: the backup lock
# and the Graph mail slots are session advisory locks (tests/test_cloud_deployment.py lists them; docs/CLOUD.md).
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "softwarden"),
        "USER": os.environ.get("POSTGRES_USER", "softwarden"),
        "PASSWORD": _db_password or DEV_DB_PASSWORD,
        "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("POSTGRES_PORT", "5434"),
        # 60 s by default (0 under DEBUG closes the connection after each request); the e2e
        # servers keep them too (softwarden/env.py).
        "CONN_MAX_AGE": _conn_max_age,
        # A kept connection is checked before reuse, so a restarted database doesn't fail the next request.
        "CONN_HEALTH_CHECKS": _conn_max_age > 0,
        # PostgreSQL's JIT compiler is for long analytical queries. Every query here is short, but the data-quality
        # query's correlated subqueries push its cost estimate past jit_above_cost, so each dashboard load spent about
        # 40 ms compiling for a query that runs in 10. Off for this application's sessions only, unless
        # SOFTWARDEN_DB_JIT=on (behind a pooler that refuses the `options` parameter; softwarden/env.py).
        # POSTGRES_SSLMODE and POSTGRES_SSLROOTCERT, when set, go here too (softwarden/env.db_tls_options).
        "OPTIONS": db_options(),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The SPA is served from the same origin (Vite proxy in development), so it reads the CSRF cookie.
CSRF_COOKIE_HTTPONLY = False
SESSION_COOKIE_HTTPONLY = True
# Django's database sessions, keeping a rotated-away key briefly as a tombstone (core/sessions.py).
SESSION_ENGINE = "core.sessions"
# Cookies are Secure unless DEBUG is on (plain-HTTP local runs) or SOFTWARDEN_SECURE_COOKIES=0 opts out
# (an install run outside Compose and Helm used to send them without Secure).
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = (
    os.environ.get("SOFTWARDEN_SECURE_COOKIES", "0" if DEBUG else "1").strip() == "1"
)
# The Playwright server's switch (core/simulation.e2e) turns on test-only routes and the sign-in simulators. A
# hosted installation or one behind TLS is never that server, so the combination refuses to start.
if os.environ.get("SOFTWARDEN_E2E") == "1" and (SOFTWARDEN_CLOUD or SESSION_COOKIE_SECURE):
    raise ImproperlyConfigured(
        "SOFTWARDEN_E2E=1 is for the Playwright test server only; unset it (it can't be combined with "
        "SOFTWARDEN_DEPLOYMENT=cloud or with Secure cookies, which are on unless SOFTWARDEN_DEBUG=1)."
    )
# Development mode turns on the sign-in simulators, which sign anyone in as any email. A hosted installation,
# or one that asks for Secure cookies (it is behind TLS, so people reach it), is never a development server: DEBUG
# with cloud mode or an explicit SOFTWARDEN_SECURE_COOKIES=1 (Compose and Helm set it) refuses to start.
if DEBUG and (SOFTWARDEN_CLOUD or os.environ.get("SOFTWARDEN_SECURE_COOKIES", "").strip() == "1"):
    raise ImproperlyConfigured(
        "SOFTWARDEN_DEBUG=1 is for local development only; unset it (it can't be combined with "
        "SOFTWARDEN_DEPLOYMENT=cloud or with SOFTWARDEN_SECURE_COOKIES=1, which Docker Compose and Helm set)."
    )
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
# A CSRF failure on a non-DRF view is a problem (DRF views raise the same type through core/authentication.py).
CSRF_FAILURE_VIEW = "core.problems.csrf_failure"
CSRF_TRUSTED_ORIGINS = os.environ.get(
    "SOFTWARDEN_CSRF_TRUSTED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
).split(",")

REST_FRAMEWORK = {
    # Sessions first and unchanged for the SPA; API tokens (Authorization: Bearer sw_...) after.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "core.authentication.SessionAuthentication",
        "api.tokens.TokenAuthentication",
    ],
    # Every view declares its minimum role (core/drf.py: min_role, public, TenantViewSet.role_policy), which also
    # refuses read-only API tokens on writes. A view that declares nothing is refused (tests/test_url_coverage.py).
    "DEFAULT_PERMISSION_CLASSES": ["core.permissions.DenyUndeclared"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["core.parsers.JSONParser"],
    "EXCEPTION_HANDLER": "core.problems.problem_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # Every signed-in request: per API token or per session user, reads and writes apart (core/throttling.py).
    "DEFAULT_THROTTLE_CLASSES": ["core.throttling.ApiRateThrottle", "core.throttling.OrgRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        "login": os.environ.get("LOGIN_THROTTLE_RATE", "10/min"),
        # Public signup and its resend (catalog/signup_views.py), per client address. Confirming a link uses
        # the sign-in rate.
        "signup": os.environ.get("SOFTWARDEN_SIGNUP_THROTTLE_RATE") or "10/hour",
        # Single sign-on starts and callbacks (OpenID Connect, SAML), and invitation preview and accept, have
        # scopes of their own, so ten wrong passwords from an office address no longer block its SSO sign-ins.
        "sso": os.environ.get("SOFTWARDEN_SSO_THROTTLE_RATE") or "120/min",
        "invitation": os.environ.get("SOFTWARDEN_INVITATION_THROTTLE_RATE") or "30/min",
        # Asking for a password reset link (catalog/password_reset_views.py), per client address. Using a link
        # uses the sign-in rate.
        "password_reset": os.environ.get("SOFTWARDEN_PASSWORD_RESET_THROTTLE_RATE") or "10/hour",
    },
    # Throttles key on the last X-Forwarded-For address, which TrustedProxyMiddleware leaves only when the request
    # came through a trusted proxy; otherwise on the peer address.
    "NUM_PROXIES": 1,
}

# The reverse proxy (Caddy in compose.yaml). Host names, IP addresses or CIDR ranges,
# comma-separated; host names are resolved (compose's service name `proxy`). Only requests from these peers may set
# X-Forwarded-For (client address for rate limits) and X-Forwarded-Proto (HTTPS, secure cookies). Empty: trust none.
# Names are resolved in the background and cached (30 s; 15 s while one fails), never per request (core/middleware.py).
TRUSTED_PROXIES = [p.strip() for p in os.environ.get("SOFTWARDEN_TRUSTED_PROXIES", "").split(",") if p.strip()]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Security headers (softwarden/security_headers.py): no page is ever framed. HSTS from the app only when SOFTWARDEN_HSTS_SECONDS is
# set (the Helm chart sets it with ingress TLS); Compose leaves it to Caddy, which skips it for localhost.
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = int(os.environ.get("SOFTWARDEN_HSTS_SECONDS") or "0")

# First-run setup (core/bootstrap.py): the one-time token file, private to the app container's `bootstrap` volume.
SOFTWARDEN_BOOTSTRAP_TOKEN_FILE = os.environ.get(
    "SOFTWARDEN_BOOTSTRAP_TOKEN_FILE", str(BASE_DIR / ".bootstrap" / "setup-token")
)

# Attachments (catalog/attachments.py). Files live under MEDIA_ROOT, the `softwarden-media` volume at
# /data/media in compose; there is no MEDIA_URL route: a file is read only through the role-checked download endpoint.
# The limits are the installation's; an organization's Admin may only tighten them (organization settings).
MEDIA_ROOT = Path(os.environ.get("SOFTWARDEN_MEDIA_ROOT", str(BASE_DIR / "media")))
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700
ATTACHMENT_MAX_SIZE_MB = int(os.environ.get("SOFTWARDEN_ATTACHMENT_MAX_MB", "25"))
ATTACHMENT_EXTENSIONS = [
    e.strip().lower().lstrip(".")
    for e in os.environ.get(
        "SOFTWARDEN_ATTACHMENT_EXTENSIONS",
        "pdf,docx,xlsx,pptx,vsdx,doc,xls,png,jpg,jpeg,gif,txt,csv,md,json,zip",
    ).split(",")
    if e.strip()
]
# Total stored per organization; 0 means no quota.
ATTACHMENT_QUOTA_MB = int(os.environ.get("SOFTWARDEN_ATTACHMENT_QUOTA_MB", "10240"))
# The largest multipart request read at all (core/upload_limit.py), in MB: the attachment limit plus room for the
# multipart framing, and at least the 5 MB CSV import. Compose gives Caddy's request_body limit the same value.
MAX_REQUEST_MB = int(os.environ.get("SOFTWARDEN_MAX_REQUEST_MB") or max(ATTACHMENT_MAX_SIZE_MB + 1, 6))
# Days a deleted attachment's file is kept before the nightly sweep removes it; longer than the longest backup run.
ATTACHMENT_RETENTION_DAYS = int(os.environ.get("SOFTWARDEN_ATTACHMENT_RETENTION_DAYS", "7"))
# Backups (core/backup.py, docs/04-development/backup-runbook.md): the worker's nightly encrypted sets, in the
# same format as ops/backup.sh. BACKUP_DIR empty (outside the image) means backups aren't configured: the schedule
# does nothing and the System page says so. The image sets /data/backups (the `softwarden-backups` volume). The key
# file defaults to `backup_key` in the secrets volume, which the init service generates when it is absent.
BACKUP_DIR = os.environ.get("SOFTWARDEN_BACKUP_DIR", "").strip()
BACKUP_KEY_FILE_CONFIGURED = bool((os.environ.get("SOFTWARDEN_BACKUP_KEY_FILE") or "").strip())
BACKUP_KEY_FILE = (os.environ.get("SOFTWARDEN_BACKUP_KEY_FILE") or "").strip() or str(secrets_dir() / "backup_key")
BACKUP_CRON = os.environ.get("SOFTWARDEN_BACKUP_CRON", "").strip() or "30 2 * * *"
BACKUP_KEEP_DAILY = int(os.environ.get("SOFTWARDEN_BACKUP_KEEP_DAILY", "14"))
BACKUP_KEEP_WEEKLY = int(os.environ.get("SOFTWARDEN_BACKUP_KEEP_WEEKLY", "8"))
# The last good backup is "stale" (a System warning, never a readiness failure) once older than this.
BACKUP_STALE_HOURS = int(os.environ.get("SOFTWARDEN_BACKUP_STALE_HOURS", "26"))
# Free space that must remain on the backup disk after a set, in MB. Unset: the larger of 2 GB and 10 % of the disk.
# The default volume shares the database's disk, so this also keeps PostgreSQL from running out.
BACKUP_MIN_FREE_MB = (
    int(os.environ["SOFTWARDEN_BACKUP_MIN_FREE_MB"])
    if (os.environ.get("SOFTWARDEN_BACKUP_MIN_FREE_MB") or "").strip()
    else None
)
# Refuse a destination that isn't a mount point (a container without the backup volume). 0 for development and tests.
BACKUP_REQUIRE_MOUNT = env_flag("SOFTWARDEN_BACKUP_REQUIRE_MOUNT", True)
# The tools, by name on PATH or a full path (the image installs postgresql-client-17 and openssl).
BACKUP_PG_DUMP = os.environ.get("SOFTWARDEN_BACKUP_PG_DUMP", "pg_dump")
BACKUP_PG_RESTORE = os.environ.get("SOFTWARDEN_BACKUP_PG_RESTORE", "pg_restore")
BACKUP_OPENSSL = os.environ.get("SOFTWARDEN_BACKUP_OPENSSL", "openssl")
# /healthz/ops (softwarden/ops.py): the operator health view answers to this bearer token or an Admin session, and is
# a 404 to anyone else. Unset, the init service's generated `ops_token` in the secrets volume; empty everywhere (a
# local run without the volume), only an Admin session opens it.
OPS_TOKEN = secret("SOFTWARDEN_OPS_TOKEN", "ops_token")
# Every rate limit below and in DEFAULT_THROTTLE_RATES counts in the shared PostgreSQL counters (core/counters.py):
# one limit for the installation, whichever worker or pod serves the request.
# File uploads per user (DRF rate syntax).
ATTACHMENT_UPLOAD_RATE = os.environ.get("SOFTWARDEN_ATTACHMENT_UPLOAD_RATE", "60/hour")
# Global search requests per user (core/search_views.py; DRF rate syntax). Empty: no limit.
SEARCH_RATE = os.environ.get("SOFTWARDEN_SEARCH_RATE", "600/min")
# Refused bearer tokens (API and SCIM: unknown, revoked, expired, owner gone) per client address (api/tokens.py).
# Past it a refused token is answered 429 (Retry-After) instead of 401 until the window ends; a valid token is never
# counted or refused. Empty: no limit.
INVALID_TOKEN_RATE = os.environ.get("SOFTWARDEN_INVALID_TOKEN_RATE", "30/min")
# API rate limits for every signed-in request (core/throttling.py, docs/API.md): one bucket per API token and one per
# session user, reads and writes apart, writes stricter. DRF rate syntax (`n/sec`, `/min`, `/hour`, `/day`). Empty: no
# limit.
API_READ_RATE = os.environ.get("SOFTWARDEN_API_READ_RATE", "1200/min")
API_WRITE_RATE = os.environ.get("SOFTWARDEN_API_WRITE_RATE", "240/min")
# The renewals calendar feed (money/report_views.py), public and authenticated by its secret: per feed, and per client
# address over every feed (Outlook.com and Google fetch from shared addresses);
# empty: no limit.
CALENDAR_FEED_RATE = os.environ.get("SOFTWARDEN_CALENDAR_FEED_RATE", "60/hour")
CALENDAR_FEED_ADDRESS_RATE = os.environ.get("SOFTWARDEN_CALENDAR_FEED_ADDRESS_RATE", "1200/hour")

# OpenAPI at /api/v1/schema. No Swagger UI: it would load assets from a CDN, and an installation may run offline.
SPECTACULAR_SETTINGS = {
    "TITLE": "Softwarden API",
    "DESCRIPTION": "The Softwarden API (docs/API.md). Sign in with a session (cookies plus the X-CSRFToken header on "
    "writes) or send an API token as `Authorization: Bearer sw_...`; a read-only token is refused on every write. "
    "Organization and role come from the account or the token, never from the request. Errors are RFC 9457 problem "
    "details (`application/problem+json`, Problem schema) whose `type` names what went wrong. Requests are rate "
    "limited per API token and per user (429 with Retry-After). Lists are keyset-paginated; records carry an ETag that "
    "PATCH and DELETE accept as If-Match.",
    "VERSION": APP_VERSION,
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    # Problem responses, 401/429 on signed-in operations and a default problem everywhere (core/openapi.py).
    "POSTPROCESSING_HOOKS": ["drf_spectacular.hooks.postprocess_schema_enums", "core.openapi.problem_responses"],
    # Stable names for choice sets that share a field name (kind, status, role, ...); without them drf-spectacular
    # names them by hash (Kind77bEnum), which changes as fields come and go.
    "ENUM_NAME_OVERRIDES": {
        "ResourceKindEnum": "catalog.models.Kind",
        # The sign-in settings' preset and the sign-in page's button (saml needs the optional ee package).
        "ProviderEnum": ["entra", "okta", "google", "oidc", "saml"],
        "InvitationStatusEnum": "catalog.account_serializers.INVITATION_STATUSES",
        "CoveragePolicyEffectEnum": "discovery.entra_coverage.EFFECT_ORDER",  # SSO and MFA coverage
        "LicenceWasteUserStatusEnum": "discovery.licence_waste.STATUSES",  # licence waste
        "RunFailureCodeEnum": ["empty_listing"],  # ConnectorRun.failure_code
        "EffectEnum": "core.models.OverrideEffect",  # a capability override's grant or revoke (kept its name)
        "ExportStatusEnum": "core.models.ExportStatus",  # organization exports
        "ExportPurposeEnum": "core.models.ExportPurpose",
        "LimitNameEnum": "core.limits.NAMES",  # per-organization limits
        "EditionEnum": "core.models.Edition",  # an organization's edition and platform/capabilities' `editions`
        "RecordKindEnum": ["application", "component", "ai_agent"],
        "ImportKindEnum": ["application", "component", "contract", "license_pool"],
        "LinkRecordKindEnum": ["application", "component", "ai_agent", "integration"],
        "AgentPlatformEnum": "catalog.models.AgentPlatform",
        "AgentChannelEnum": "catalog.models.AgentChannel",
        "AgentAuthModeEnum": "catalog.models.AgentAuthMode",
        "AgentRiskEnum": "catalog.models.AgentRisk",
        "ModelModalityEnum": "catalog.models.ModelModality",
        "LinkKindEnum": ["relationship", "integration"],
        # The explorer's pass on a node and on a cycle row (the name it had when only nodes carried it).
        "DependencyNodeDirectionEnum": ["dependents", "depends_on"],
        "RenewalRowKindEnum": ["renewal_action", "renewal_gap"],
        "SeatCountFieldEnum": ["seats_purchased", "seats_assigned"],
        "SnoozeKindEnum": "notify.models.SnoozeKind",
        "LifecycleEnum": "catalog.models.Lifecycle",
        "LinkStateEnum": "catalog.serializers.LINK_STATES",
        "BulkRowCodeEnum": "catalog.bulk.ROW_CODES",
        "AssessmentStatusEnum": "catalog.models.AssessmentStatus",
        # Ticket links (tickets/models.py): a link's system and status, and the systems a connection reads.
        "TicketSystemEnum": "tickets.models.TicketSystem",
        "TicketStatusEnum": "tickets.models.TicketStatus",
        "TicketConnectionSystemEnum": "tickets.serializers.SYSTEMS",
        "InstanceStatusEnum": "catalog.models.InstanceStatus",
        "ImportStatusEnum": "imports.models.ImportStatus",
        "DigestStatusEnum": "notify.models.DigestStatus",
        "ReminderStatusEnum": "notify.models.ReminderStatus",
        "DeliveryStatusEnum": "notify.models.DeliveryStatus",
        "DeliveryKindEnum": "notify.models.DeliveryKind",
        "DeliveryChannelEnum": "notify.models.Channel",
        "EmailTransportEnum": "notify.models.EmailTransport",
        "SmtpSecurityEnum": "notify.models.SmtpSecurity",
        "LiveReminderStatusEnum": ["open", "snoozed", "acknowledged"],
        "ReminderSourceEnum": "notify.models.ReminderSource",
        "AttentionReminderSourceEnum": [
            "contract",
            "periodic_review",
            "custom_date",
            "credential_expiry",
            "vendor_risk",
        ],
        "ReminderResolutionKindEnum": "notify.models.ResolutionKind",
        "ManualResolutionKindEnum": ["done", "risk_accepted", "no_longer_applies"],
        "RenewalDecisionEnum": "money.models.Decision",
        "DiscoveryDecisionEnum": "discovery.models.DecisionKind",
        "ObservationStatusEnum": ["new", "changed", "linked", "dismissed", "gone", "out_of_scope"],
        "DiscoveredCredentialTypeEnum": "discovery.models.CredentialType",
        "PeriodUnitEnum": "money.models.PeriodUnit",
        "RoleEnum": "core.models.Role",
        "ReassignRoleEnum": "offboarding.reassign.ROLE_NAMES",  # offboarding
        "ReassignRowStatusEnum": "offboarding.reassign.ROW_STATUSES",
        "CriticalityEnum": "catalog.models.Criticality",
        "NotificationRuleKindEnum": "notify.models.RuleKind",
        "NodeRoleEnum": "catalog.models.NodeRole",
        "CustomFieldTypeEnum": "catalog.models.CustomFieldType",
        "CustomFieldKindEnum": [
            "application",
            "component",
            "integration",
            "ai_agent",
            "vendor",
            "contract",
            "license_pool",
        ],
        "ContractStatusEnum": "money.models.ContractStatus",
        "BillingFrequencyEnum": "money.models.BillingFrequency",
        "AttachmentKindEnum": "catalog.models.AttachmentKind",
        "AttachmentCategoryEnum": "catalog.models.AttachmentCategory",
        # `discovery/sources` names a source and its connection kind as connectors and runs do.
        "DiscoverySourceEnum": "discovery.models.Source",
        "ConnectionKindEnum": ["microsoft", "halo", "okta", "google_workspace"],  # core/connections.KINDS
        # Composite endpoints (api/composite_serializers.py).
        "SearchKindEnum": "api.composite_serializers.SEARCH_KINDS",
        "SearchParentKindEnum": "api.composite_serializers.SEARCH_PARENT_KINDS",
        "DeadlineSourceEnum": "api.composite_serializers.DEADLINE_SOURCES",
        "DeadlineActionEnum": "api.composite_serializers.DEADLINE_ACTIONS",
        "CredentialRowKindEnum": "api.composite_serializers.CREDENTIAL_KINDS",
        "RenewalGapEnum": "api.composite.GAPS",
        # Vendor risk file (catalog/vendor_risk_serializers.py).
        # The vendor risk tier has the criticality values (low, medium, high, critical): it is CriticalityEnum. The
        # compliance profile's risk tier keeps its name beside it.
        "RiskTierEnum": "catalog.models.RiskTier",
        "QuestionnaireStatusEnum": "catalog.models.QuestionnaireStatus",
        "AssuranceTypeEnum": "catalog.models.AssuranceType",
        "DpaStatusEnum": "catalog.models.DpaStatus",
        "RiskDocumentKindEnum": "catalog.models.RiskDocumentKind",
        "RiskDocumentStatusEnum": ["current", "expiring", "expired"],
        "AssuranceStatusEnum": ["missing", "current", "expiring", "expired"],
        "VendorRiskGapEnum": ["no_current_assurance", "dpa_missing"],
        # End-of-life tracking (eol/serializers.py, the Needs attention End of life queue).
        "EolStatusEnum": ["supported", "security_only", "end_of_life", "unknown"],
        "EolKnownStatusEnum": ["supported", "security_only", "end_of_life"],
        "TrackedRecordKindEnum": ["application", "component"],
        # Data-flow map (catalog/data_flow_serializers.py). The first two keep the names they already had.
        "DataClassificationEnum": "catalog.models.DataClassification",
        "IntegrationInputDirectionEnum": "catalog.models.IntegrationDirection",
        "DataCategoryEnum": "catalog.models.DataCategory",
        "DataFlowRiskEnum": "catalog.data_flows.RISKS",
        "DataFlowEndEnum": ["source", "target"],
    },
}

# The built SPA (apps/web/dist, or /app/web in the container). WhiteNoise serves its files from the site root;
# softwarden.ops.spa answers client-side routes with index.html.
SPA_DIR = Path(os.environ.get("SOFTWARDEN_SPA_DIR", BASE_DIR.parent / "web" / "dist"))
WHITENOISE_ROOT = SPA_DIR if SPA_DIR.is_dir() else None
WHITENOISE_INDEX_FILE = True
WHITENOISE_IMMUTABLE_FILE_TEST = r"^/assets/"  # Vite content-hashes these names
# The SPA's files are for this origin only: no `Access-Control-Allow-Origin: *` (WhiteNoise's default).
WHITENOISE_ALLOW_ALL_ORIGINS = False

# JSON lines on stdout (observability, softwarden/logs.py) with the request id, organization and user ids, and
# never a query string; Docker rotates them. SOFTWARDEN_LOG_FORMAT=text is easier to read locally.
LOG_FORMAT = (os.environ.get("SOFTWARDEN_LOG_FORMAT") or "json").strip().lower()
if LOG_FORMAT not in ("json", "text"):
    raise ImproperlyConfigured(f"SOFTWARDEN_LOG_FORMAT must be json or text, not {LOG_FORMAT!r}.")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "softwarden.logs.JsonFormatter"},
        "text": {"()": "softwarden.logs.TextFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": LOG_FORMAT,
        }
    },
    "root": {"handlers": ["console"], "level": os.environ.get("SOFTWARDEN_LOG_LEVEL", "INFO")},
    # The Anthropic SDK and its HTTP clients log whole requests (the assistant's prompt) at DEBUG: never below WARNING,
    # whatever SOFTWARDEN_LOG_LEVEL says (discovery/assistant.py; the 1.x SDK uses httpx2 and httpcore2).
    "loggers": {
        **{name: {"level": "WARNING"} for name in ("anthropic", "httpx", "httpcore", "httpx2", "httpcore2")},
        # Django's DEFAULT_LOGGING gives `django` its own console handler in DEBUG and `django.server` (runserver's
        # request lines, query strings included) its own formatter. Both go through the scrubbing console handler
        # instead (softwarden/logs.py), so no query string or secret reaches the console in development either.
        "django": {"handlers": [], "level": "INFO", "propagate": True},
        "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# Email for the daily digest. Nothing is sent unless SOFTWARDEN_SMTP_HOST is set: digests are then recorded
# as not sent and every action stays in Needs attention. The password comes from the environment and is never logged.
SMTP_HOST = os.environ.get("SOFTWARDEN_SMTP_HOST", "")
# Whether addresses an organization's Admin enters for its own servers (a Halo URL, an SMTP relay) may be on private
# networks (RFC 1918, ULA, carrier-grade NAT). On by default for a self-hosted install; turn it off for a hosted,
# multi-organization one (core/outbound.py). Loopback, link-local and cloud metadata addresses are refused either way.
# Cloud mode defaults it off (docs/CLOUD.md).
SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS = env_flag("SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS", not SOFTWARDEN_CLOUD)
# Whether an organization's SMTP settings may name any port, not only 25, 465, 587 and 2525. Cloud: off.
SOFTWARDEN_SMTP_ANY_PORT = env_flag("SOFTWARDEN_SMTP_ANY_PORT", not SOFTWARDEN_CLOUD)
EMAIL_BACKEND = os.environ.get("SOFTWARDEN_EMAIL_BACKEND") or (
    "django.core.mail.backends.smtp.EmailBackend" if SMTP_HOST else "django.core.mail.backends.dummy.EmailBackend"
)
SOFTWARDEN_EMAIL_CONFIGURED = bool(SMTP_HOST or os.environ.get("SOFTWARDEN_EMAIL_BACKEND"))
EMAIL_HOST = SMTP_HOST
EMAIL_PORT = int(os.environ.get("SOFTWARDEN_SMTP_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("SOFTWARDEN_SMTP_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("SOFTWARDEN_SMTP_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("SOFTWARDEN_SMTP_TLS", "1") == "1"
EMAIL_TIMEOUT = 20
DEFAULT_FROM_EMAIL = os.environ.get("SOFTWARDEN_EMAIL_FROM", "Softwarden <softwarden@localhost>")
# The address people open Softwarden at; digest links start with it.
SOFTWARDEN_BASE_URL = os.environ.get("SOFTWARDEN_BASE_URL", "http://localhost:8080").rstrip("/")
# Break-glass (core/sso.py): hides and refuses every single sign-on route for every organization, whatever
# their settings say. Local password sign-in is never removed. `manage.py sso_recovery` turns SSO off for good.
SOFTWARDEN_FORCE_LOCAL_LOGIN = env_flag("SOFTWARDEN_FORCE_LOCAL_LOGIN", False)
# Sign-in domain verification (core/sso.py): a sign-in domain an organization claims stays pending, routing no sign-in,
# until a DNS TXT record proves the organization controls it. On by default in cloud mode; off on a self-hosted install,
# where a claimed domain applies at once and the first organization keeps it.
SOFTWARDEN_SSO_DOMAIN_VERIFICATION = env_flag("SOFTWARDEN_SSO_DOMAIN_VERIFICATION", SOFTWARDEN_CLOUD)
# The resolver those TXT lookups use (core/dns_txt.py): `host` or `host:port`; empty, the first nameserver in
# /etc/resolv.conf; `simulated` (the default with DEBUG on), no network, answers from a table and *.dns-verified.test.
SOFTWARDEN_DNS_RESOLVER = os.environ.get("SOFTWARDEN_DNS_RESOLVER", "simulated" if DEBUG else "").strip()
# Cloud (catalog/signup.py): public signup, off by default. When on, anyone can create an account and a new
# organization (edition cloud) at /signup after confirming their email, so it also needs email (SOFTWARDEN_SMTP_HOST):
# without it signup refuses. Off by default on a self-hosted install, on by default in cloud mode
# (SOFTWARDEN_DEPLOYMENT=cloud); the variable wins either way.
SOFTWARDEN_SIGNUP_ENABLED = env_flag("SOFTWARDEN_SIGNUP_ENABLED", SOFTWARDEN_CLOUD)
# The signup ceiling (catalog/signup.require_room_today): at most this many organizations created in 24 hours
# while signup is on (counted from the database); past it signing up and confirming answer 503 /problems/signup-paused.
# 0 turns the ceiling off.
SOFTWARDEN_SIGNUP_DAILY_LIMIT = max(0, int(os.environ.get("SOFTWARDEN_SIGNUP_DAILY_LIMIT") or "200"))
# Launch scope: the preview features (Okta and Google Workspace discovery,
# ServiceNow and Halo ticket connections) are hidden from every organization unless this is on. Off by default. Data
# already stored stays; turning it on brings the features back. The one place it is read is core/capabilities.py
# (the `preview` capability, docs/CAPABILITIES.md).
SOFTWARDEN_PREVIEW_FEATURES = env_flag("SOFTWARDEN_PREVIEW_FEATURES", False)
# Simulators outside development (core/simulation.py): the simulated tenants and hosts (Microsoft tenant `simulated`,
# `*.simulated.invalid`) fill the catalog with synthetic data, so outside DEBUG and the Playwright server they are
# refused on save, at the test and at run time unless this is on. The Docker demo sets it; a production install
# leaves it off. It never turns on the sign-in simulators.
SOFTWARDEN_ALLOW_SIMULATED = env_flag("SOFTWARDEN_ALLOW_SIMULATED", False)
# Required If-Match (core/drf.if_match_required): on (the default), a signed-in session's PATCH and DELETE (and every action that
# takes If-Match) without If-Match is a 428, so a form that never read the record's ETag can't overwrite someone else's
# change. The web app sends the ETag from every edit form. API tokens keep the unconditional write. 0 turns it off.
SOFTWARDEN_REQUIRE_IF_MATCH = env_flag("SOFTWARDEN_REQUIRE_IF_MATCH", True)
# Days between an Admin asking to delete their organization (capability org.self_delete) and its removal; an Admin can
# cancel until then (catalog/org_lifecycle.py).
SOFTWARDEN_ORG_DELETION_GRACE_DAYS = int(os.environ.get("SOFTWARDEN_ORG_DELETION_GRACE_DAYS") or "30")
# Tenant backups (catalog/org_lifecycle.queue_backups; capability org.tenant_backups): how many nightly
# backups of each organization are kept, 1 to 90; older ones are removed.
SOFTWARDEN_TENANT_BACKUP_RETENTION = min(max(int(os.environ.get("SOFTWARDEN_TENANT_BACKUP_RETENTION") or "7"), 1), 90)
# Platform admin console (core/impersonation.py): how long one impersonation lasts, 1 to 240 minutes.
SOFTWARDEN_IMPERSONATION_MINUTES = min(max(int(os.environ.get("SOFTWARDEN_IMPERSONATION_MINUTES") or "30"), 1), 240)
# The platform staff second factor (catalog/staff_second_factor.py): platform staff need a TOTP code as well as their password before the platform
# console opens, and again to start an impersonation. On by default in cloud mode; off self-hosted, where an operator
# can turn it on (an installation shared by organizations that don't trust each other should).
SOFTWARDEN_PLATFORM_TOTP = env_flag("SOFTWARDEN_PLATFORM_TOTP", SOFTWARDEN_CLOUD)
# Webhooks (notify/webhooks.py): attempts per event delivery (the first and its retries after 1 min, 5 min,
# 30 min, 2 h and 6 h; more attempts wait 6 h each; 1 to 20), and how many event deliveries failing in a row turn an
# endpoint off (0: never).
SOFTWARDEN_WEBHOOK_MAX_ATTEMPTS = int(os.environ.get("SOFTWARDEN_WEBHOOK_MAX_ATTEMPTS") or "6")
SOFTWARDEN_WEBHOOK_DISABLE_AFTER = int(os.environ.get("SOFTWARDEN_WEBHOOK_DISABLE_AFTER") or "5")
# How long finished notification deliveries (every channel, with their attempt log) are kept, in days, by when they were
# queued: sent ones, and failed or skipped ones (notify/deliveries.purge, daily schedule `notify.retention`).
SOFTWARDEN_DELIVERY_RETENTION_SENT_DAYS = int(os.environ.get("SOFTWARDEN_DELIVERY_RETENTION_SENT_DAYS") or "30")
SOFTWARDEN_DELIVERY_RETENTION_FAILED_DAYS = int(os.environ.get("SOFTWARDEN_DELIVERY_RETENTION_FAILED_DAYS") or "90")
# End-of-life data (eol/cache.py): the daily `eol.refresh` job reads endoflife.date's public API. Off (0) on an
# air-gapped install, which loads a downloaded snapshot with `manage.py eol_import` instead.
SOFTWARDEN_EOL_ENABLED = env_flag("SOFTWARDEN_EOL_ENABLED", True)

# The assistant (capability `assistant`, discovery/assistant.py): the Claude model every
# organization's suggestions use, the price table's overrides (JSON: {"model": {"input": "2.00", "output": "10.00",
# "cache_read": "0.20", "cache_write": "2.50"}}, USD per million tokens; a model with no price is refused, since its
# cost and the monthly cap couldn't be computed), and the installation's Anthropic API key, used by an organization
# that has entered none of its own. The key comes from the environment only and is never logged (core/jobs.py redacts it).
SOFTWARDEN_ASSISTANT_MODEL = os.environ.get("SOFTWARDEN_ASSISTANT_MODEL", "claude-sonnet-5-5").strip()
SOFTWARDEN_ASSISTANT_PRICES = os.environ.get("SOFTWARDEN_ASSISTANT_PRICES", "")
SOFTWARDEN_ASSISTANT_INSTALLATION_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# The installation's key is offered to organizations only with this ceiling set (USD per organization per month): an
# organization on it is held to the lower of its own cap and this. Unset, ANTHROPIC_API_KEY is never used.
SOFTWARDEN_ASSISTANT_INSTALLATION_CAP_USD = os.environ.get("SOFTWARDEN_ASSISTANT_INSTALLATION_CAP_USD", "").strip()
