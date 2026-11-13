"""The app sends its own browser security headers, so a deployment without Caddy (the Helm chart, another
proxy) still gets clickjacking protection, the SPA's Content-Security-Policy and, where configured, HSTS. A response
that sets its own policy (attachment downloads, the SAML pages) keeps exactly that one."""

import io
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.test import Client

from softwarden import security_headers

pytestmark = pytest.mark.django_db

REPO = Path(__file__).resolve().parents[3]
API = REPO / "apps" / "api"


def assert_spa_headers(response):
    assert response["X-Frame-Options"] == "DENY"
    assert response["Content-Security-Policy"] == security_headers.SPA_CSP
    assert response["Permissions-Policy"] == security_headers.PERMISSIONS_POLICY
    assert response["X-Content-Type-Options"] == "nosniff"


def test_the_spa_index_carries_the_headers(settings, tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>Softwarden</title>", encoding="utf-8")
    settings.SPA_DIR = tmp_path
    response = Client().get("/applications/some-deep-link")
    assert response.status_code == 200 and response["Content-Type"].startswith("text/html")
    assert_spa_headers(response)


def test_a_json_response_and_a_problem_carry_the_headers(a, api):
    ok = api(a["users"]["viewer"]).get("applications")
    assert ok.status_code == 200 and ok["Content-Type"].startswith("application/json")
    assert_spa_headers(ok)
    problem = api(a["users"]["viewer"]).get("applications/00000000-0000-0000-0000-000000000000")
    assert problem.status_code == 404 and problem["Content-Type"] == "application/problem+json"
    assert_spa_headers(problem)
    anonymous = Client().get("/api/v1/applications")
    assert anonymous.status_code in (401, 403)
    assert_spa_headers(anonymous)


def test_a_csv_export_carries_the_headers(a, api):
    response = api(a["users"]["admin"]).client.get("/api/v1/audit-events/export.csv")
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    assert_spa_headers(response)


def test_a_view_that_sets_its_own_policy_keeps_it():
    """The middleware fills in only what the response lacks (as Caddy's `?` headers do)."""
    from django.http import HttpResponse

    def view(request):
        response = HttpResponse("x")
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
        response["Permissions-Policy"] = "camera=()"
        return response

    response = security_headers.SecurityHeadersMiddleware(view)(None)
    assert response["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert response["Permissions-Policy"] == "camera=()"


def test_the_app_and_caddy_send_the_same_policies():
    """Compose keeps Caddy's conditional headers as a second layer (they now apply to Caddy's own responses only);
    the values must not drift apart."""
    caddy = (REPO / "docker" / "Caddyfile").read_text(encoding="utf-8")
    csp = re.search(r'header \?Content-Security-Policy "([^"]+)"', caddy)
    permissions = re.search(r'header \?Permissions-Policy "([^"]+)"', caddy)
    assert csp and csp.group(1) == security_headers.SPA_CSP
    assert permissions and permissions.group(1) == security_headers.PERMISSIONS_POLICY


def test_hsts_is_sent_on_https_only_when_configured(settings, a, api):
    settings.SECURE_HSTS_SECONDS = 31536000
    secure = api(a["users"]["viewer"]).client.get("/api/v1/applications", secure=True)
    assert secure["Strict-Transport-Security"] == "max-age=31536000"
    plain = api(a["users"]["viewer"]).client.get("/api/v1/applications")
    assert "Strict-Transport-Security" not in plain


def test_deploy_check_no_longer_reports_missing_clickjacking_protection(settings):
    settings.DEBUG = False
    out = io.StringIO()
    try:
        call_command("check", "--deploy", stdout=out, stderr=out)
    except SystemCheckError as e:
        out.write(str(e))
    assert "security.W002" not in out.getvalue()


def test_api_responses_are_not_stored_by_browsers_or_intermediaries(a, api):
    """A JSON list, a detail, a CSV and an xlsx export, a problem and an anonymous refusal
    all say Cache-Control: private, no-store; a view's own Cache-Control (an attachment, the audit export) is kept."""
    viewer = api(a["users"]["viewer"]).client
    responses = {
        "list": viewer.get("/api/v1/applications"),
        "detail": viewer.get(f"/api/v1/applications/{a['app'].id}"),
        "me": viewer.get("/api/v1/auth/me"),
        "csv": viewer.get("/api/v1/applications/export.csv"),
        "xlsx": viewer.get("/api/v1/applications/export.xlsx"),
        "problem": viewer.get("/api/v1/applications/00000000-0000-0000-0000-000000000000"),
        "anonymous": Client().get("/api/v1/applications"),
    }
    for name, response in responses.items():
        assert response.status_code in (200, 401, 403, 404), name
        assert response.get("Cache-Control") == "private, no-store", (name, response.get("Cache-Control"))
    assert responses["xlsx"]["Content-Type"].startswith("application/vnd.openxmlformats")
    audit = api(a["users"]["admin"]).client.get("/api/v1/audit-events/export.csv")
    assert audit["Cache-Control"] == "no-store"  # its own, unchanged


def _cookie_settings(tmp_path, **env: str) -> list[str]:
    """SESSION_COOKIE_SECURE and CSRF_COOKIE_SECURE of a fresh settings import with `env`."""
    code = (
        "import django, sys; django.setup(); from django.conf import settings as s; "
        "sys.stdout.write(f'{s.SESSION_COOKIE_SECURE} {s.CSRF_COOKIE_SECURE}')"
    )
    environ = {k: v for k, v in os.environ.items() if k not in ("SOFTWARDEN_SECURE_COOKIES", "SOFTWARDEN_DEBUG")}
    environ.update(
        SOFTWARDEN_SECRETS_DIR=str(tmp_path),
        SOFTWARDEN_SECRET_KEY="a-test-secret-key-that-is-long-enough-for-production-settings-0123456789",
        DJANGO_SETTINGS_MODULE="softwarden.settings",
        **env,
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=API, env=environ, capture_output=True, text=True, check=True
    )
    return result.stdout.split()


def test_cookies_are_secure_by_default_when_debug_is_off(tmp_path):
    """DEBUG off and SOFTWARDEN_SECURE_COOKIES unset gives Secure cookies; 0 opts out;
    development (DEBUG on) keeps plain-HTTP cookies unless asked."""
    assert _cookie_settings(tmp_path, SOFTWARDEN_DEBUG="0") == ["True", "True"]
    assert _cookie_settings(tmp_path, SOFTWARDEN_DEBUG="0", SOFTWARDEN_SECURE_COOKIES="0") == ["False", "False"]
    assert _cookie_settings(tmp_path, SOFTWARDEN_DEBUG="1") == ["False", "False"]
    assert _cookie_settings(tmp_path, SOFTWARDEN_DEBUG="1", SOFTWARDEN_SECURE_COOKIES="1") == ["True", "True"]
