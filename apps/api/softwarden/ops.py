"""Liveness and readiness probes, the operator health view, and the built SPA served from the API's origin."""

import hashlib
import hmac
import time
from typing import Any

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.migrations.executor import MigrationExecutor
from django.http import FileResponse, Http404, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from core.drf import public


@public
@require_safe
@never_cache
def healthz(request):
    """The process is up. No database call, so a slow database doesn't get the container restarted."""
    return JsonResponse({"status": "ok"})


# The migration check loads every migration file and reads django_migrations: far more work than `SELECT 1`, on public,
# unthrottled probes. Its answer is kept per process for MIGRATION_CHECK_SECONDS, per
# database name, so a flood of probes costs one check per window; a finished migrate shows within that window.
MIGRATION_CHECK_SECONDS = 30.0
_migration_checks: dict[str, tuple[float, int]] = {}


def _pending_migrations(connection) -> int:
    name = str(connection.settings_dict.get("NAME"))
    now = time.monotonic()
    kept = _migration_checks.get(name)
    if kept is not None and now - kept[0] < MIGRATION_CHECK_SECONDS:
        return kept[1]
    executor = MigrationExecutor(connection)
    pending = len(executor.migration_plan(executor.loader.graph.leaf_nodes()))
    _migration_checks[name] = (now, pending)
    return pending


def _probe_database():
    """(milliseconds `SELECT 1` took, number of migrations not applied). Raises when the database doesn't answer."""
    connection = connections[DEFAULT_DB_ALIAS]
    started = time.monotonic()
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    latency_ms = round((time.monotonic() - started) * 1000, 1)
    return latency_ms, _pending_migrations(connection)


def _database_and_migrations():
    """(checks, ok): the database answers and every migration has been applied."""
    try:
        _latency, pending = _probe_database()
    except Exception:
        return {"database": "unreachable"}, False
    if pending:
        return {"database": "ok", "migrations": "pending"}, False
    return {"database": "ok", "migrations": "ok"}, True


def _unavailable(checks):
    return JsonResponse({"status": "unavailable", "checks": checks}, status=503)


@public
@require_safe
@never_cache
def readyz_web(request):
    """This web container can serve: database and migrations only. The container healthcheck uses it, and the worker
    waits for it before starting, so it must not depend on the worker (that would be a start-up deadlock)."""
    checks, ok = _database_and_migrations()
    if not ok:
        return _unavailable(checks)
    return JsonResponse({"status": "ok", "checks": checks})


@public
@require_safe
@never_cache
def readyz(request):
    """The installation is ready: the database answers, every migration is applied, the
    worker's heartbeat is fresh and no schedule is overdue or stuck (core/health.py). A failing schedule is reported
    but doesn't fail readiness. Point an outside monitor here; nothing in the stack depends on it.

    Public, so it says how things stand and nothing more: no schedule names and no heartbeat
    age, only the worst state. Which schedule is late or failing is on the System page and at /healthz/ops."""
    checks, ok = _database_and_migrations()
    if not ok:
        return _unavailable(checks)
    from core import health

    now = timezone.now()
    worker = health.worker_check(now)
    schedules = health.schedule_checks(now)
    checks["worker"] = {"ok": "ok", "never": "not running", "stale": "stale"}[worker["state"]]
    states = {c["state"] for c in schedules}
    checks["schedules"] = next((state for state in ("stuck", "overdue", "failing") if state in states), "ok")
    if health.overall(worker, schedules) == "unhealthy":
        return _unavailable(checks)
    return JsonResponse({"status": "ok", "checks": checks})


def version() -> str:
    """This build's version: apps/api/pyproject.toml's, which the image carries (settings.APP_VERSION)."""
    return str(settings.APP_VERSION)


def _bearer(request) -> str:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def _token_matches(presented: str, configured: str) -> bool:
    """Constant-time, and over digests of equal length, so the comparison doesn't leak the token's length."""
    if not presented or not configured:
        return False
    return hmac.compare_digest(
        hashlib.sha256(presented.encode()).digest(), hashlib.sha256(configured.encode()).digest()
    )


def _single_organization() -> bool:
    """The installation has exactly one organization."""
    from core.models import Organization

    return Organization.objects.all()[:2].count() == 1


def _ops_allowed(request) -> bool:
    """The ops token (no database needed), or an Admin session while the installation has exactly one organization
    that isn't on the cloud edition: the body carries installation-wide job
    figures, which an Admin of one tenant among several must not see. The membership comes from
    core.access.current_membership, so an SSO session's confinement and the must-change-password rule (across every
    membership) hold."""
    if _token_matches(_bearer(request), settings.OPS_TOKEN):
        return True
    from rest_framework.exceptions import PermissionDenied

    from core import access
    from core.capabilities import BILLING, has_capability
    from core.models import Role

    try:
        if not request.user.is_authenticated or not _single_organization():
            return False
        request.auth = None  # a session, never an API token: what core.access reads for a token's organization
        membership = access.current_membership(request)
    except PermissionDenied:
        return False
    except Exception:  # the database is down: only the token opens the view then
        return False
    return membership.role == Role.ADMIN and not has_capability(membership.organization, BILLING)


@public  # to the role machinery: it authenticates by its own token or an Admin session (tests/test_permissions.py)
@require_safe
@never_cache
def healthz_ops(request):
    """The operator health view: database latency, migrations pending, worker heartbeat age, the job queue
    per lane, jobs failed in the last 24 h, the backup's age and state, disk space and the version. Installation-wide,
    no organization's data and no secrets. A 404 to anyone without the SOFTWARDEN_OPS_TOKEN bearer or (on a
    single-organization installation) an Admin session, so it doesn't advertise itself. 200 whenever the database
    answers, with `status` ok, degraded or unhealthy (as the System page) or `migrations_pending`; 503 only when the
    database doesn't answer. /readyz stays the probe to alert on."""
    if not _ops_allowed(request):
        raise Http404()
    body: dict[str, Any] = {"version": version(), "checked_at": timezone.now()}
    try:
        latency_ms, pending = _probe_database()
    except Exception:
        body.update(status="unavailable", database={"state": "unreachable", "latency_ms": None})
        return JsonResponse(body, status=503)
    body["database"] = {"state": "ok", "latency_ms": latency_ms}
    body["migrations"] = {"pending": pending}
    from core import health

    detail = health.ops_detail(body["checked_at"])
    body.update(detail)
    if pending:
        body["status"] = "migrations_pending"
    return JsonResponse(body)


@public
@require_safe
@never_cache
def spa(request, path=""):
    """Deep links like /applications/<id> get index.html; the SPA router takes it from there.
    Hashed assets under /assets are served by WhiteNoise before this view is reached."""
    index = settings.SPA_DIR / "index.html"
    if not index.is_file():
        raise Http404("The web app isn't built. Run `npm run build` in apps/web.")
    return FileResponse(index.open("rb"), content_type="text/html; charset=utf-8")
