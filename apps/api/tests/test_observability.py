"""Observability: JSON log lines with the request id,
organization and user ids and never a query string (softwarden/logs.py, core/request_context.py); the operator health
view /healthz/ops behind the ops token or an Admin session (softwarden/ops.py, core/health.ops_detail); OpenTelemetry
off unless configured (softwarden/otel.py)."""

import contextvars
import importlib.util
import io
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client

from core import jobs
from core.request_context import log_context, unit_of_work
from softwarden import logs, otel

API = Path(__file__).resolve().parents[1]
OPS = "/healthz/ops"
TOKEN = "ops-token-for-tests-" + "x" * 40


@pytest.fixture
def captured():
    """Every line the JSON formatter writes meanwhile, parsed. The formatter runs at emit time, inside the request or
    job, as in production."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.JsonFormatter())
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    logs.forget_secrets()
    try:
        yield lambda: [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
        logs.forget_secrets()


def request_lines(lines):
    return [line for line in lines if line["logger"] == "softwarden.request"]


# ---- JSON logs ------------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_request_line_carries_the_request_id_user_and_org(a, api, captured):
    editor = a["users"]["editor"]
    response = api(editor).get("applications")
    assert response.status_code == 200
    lines = request_lines(captured())
    assert len(lines) == 1
    line = lines[0]
    assert line["request_id"] == response["X-Request-ID"]
    assert line["user"] == str(editor.pk)
    assert line["org"] == str(a["org"].id)
    assert line["method"] == "GET"
    assert line["path"] == "/api/v1/applications"
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], int | float) and line["duration_ms"] >= 0
    assert line["level"] == "INFO"
    # ids only: never the organization's name or the user's address
    raw = json.dumps(lines)
    assert a["org"].name not in raw and editor.email not in raw


@pytest.mark.django_db
def test_lines_written_during_a_request_carry_its_fields(a, api, captured, monkeypatch):
    """A line any code writes mid-request (here inside the list view) has the request's id, org and user."""
    from catalog import viewsets

    original = viewsets.ApplicationViewSet.list

    def noisy(self, request, *args, **kwargs):
        logging.getLogger("catalog").warning("listing applications")
        return original(self, request, *args, **kwargs)

    monkeypatch.setattr(viewsets.ApplicationViewSet, "list", noisy)
    response = api(a["users"]["viewer"]).get("applications")
    assert response.status_code == 200
    line = next(x for x in captured() if x["message"] == "listing applications")
    assert line["request_id"] == response["X-Request-ID"]
    assert line["org"] == str(a["org"].id)
    assert line["user"] == str(a["users"]["viewer"].pk)
    assert line["path"] == "/api/v1/applications"


@pytest.mark.django_db
def test_an_anonymous_request_line_has_no_user_and_probes_log_at_debug(captured):
    Client().get("/api/v1/auth/options")
    Client().get("/healthz")
    lines = request_lines(captured())
    options = next(x for x in lines if x["path"] == "/api/v1/auth/options")
    assert "user" not in options and "org" not in options
    assert options["request_id"]
    probe = next(x for x in lines if x["path"] == "/healthz")
    assert probe["level"] == "DEBUG"


@pytest.mark.django_db
def test_a_job_s_lines_carry_its_id_and_organization(a, captured, monkeypatch):
    monkeypatch.setattr(jobs, "JOB_KINDS", dict(jobs.JOB_KINDS))

    def work(job, ctx):
        logging.getLogger("test.job").info("working")

    jobs.register_job("test.logs", work)
    job = jobs.enqueue("test.logs", organization=a["org"])
    claimed = jobs.claim()
    assert claimed is not None and claimed.id == job.id
    assert jobs.run(claimed) == "succeeded"
    lines = captured()
    inside = next(x for x in lines if x["message"] == "working")
    assert inside["request_id"] == str(job.id)
    assert inside["org"] == str(a["org"].id)
    done = next(x for x in lines if x["message"].startswith("job succeeded"))
    assert done["request_id"] == str(job.id)  # the runner's own line, outside the job's unit of work


def test_a_line_outside_any_request_has_no_request_fields(captured):
    # An empty context: earlier tests' audit events left a command-style request id in this one.
    contextvars.Context().run(logging.getLogger("x").info, "plain")
    (line,) = [x for x in captured() if x["message"] == "plain"]
    assert set(line) == {"ts", "level", "logger", "message"}


@pytest.mark.django_db
def test_the_calendar_feed_secret_never_reaches_a_log_line(a, api, captured):
    """The feed's secret travels in the query string: no line, request or Django's own "Not Found", carries any of
    the query, valid secret or not."""
    cache.clear()  # the feed throttles count in the process cache
    created = api(a["users"]["viewer"]).post("calendar-feed").json_body
    secret = created["url"].split("token=", 1)[1]
    feed = "/api/v1/calendar/renewals.ics"
    ok = Client().get(f"{feed}?token={secret}&months=6")
    bad = Client().get(f"{feed}?token=swc_{'q' * 40}")
    assert ok.status_code == 200 and bad.status_code == 404
    cache.clear()
    lines = captured()
    raw = json.dumps(lines)
    assert secret not in raw and secret[12:] not in raw
    assert "token=" not in raw and "months=" not in raw and "q" * 40 not in raw
    paths = {x["path"] for x in request_lines(lines)}
    assert feed in paths


def test_scrub_removes_query_strings_and_configured_secrets(settings):
    settings.OPS_TOKEN = TOKEN
    logs.forget_secrets()
    try:
        text = 'GET /api/v1/calendar/renewals.ics?token=swc_abc&x=1 HTTP/1.1" 200'
        assert logs.scrub(text) == 'GET /api/v1/calendar/renewals.ics HTTP/1.1" 200'
        # An absolute URL keeps its scheme and host only (a webhook URL's secret is its path).
        assert logs.scrub("https://graph.example/v1.0/users?$select=id done") == "https://graph.example/[redacted] done"
        assert logs.scrub("Listening at: http://0.0.0.0:8000 (1)") == "Listening at: http://0.0.0.0:8000 (1)"
        assert logs.scrub("Is it ready?") == "Is it ready?"
        assert logs.scrub(f"bearer {TOKEN} refused") == "bearer [redacted] refused"
        assert logs.scrub(f"key {settings.SECRET_KEY}") == "key [redacted]"
        assert logs.scrub("GET /api/v1/applications/0b6f3f2e-1c1d-4c1e-9d4e-2f1f0e1d2c3b 200 returncode=0") == (
            "GET /api/v1/applications/0b6f3f2e-1c1d-4c1e-9d4e-2f1f0e1d2c3b 200 returncode=0"
        )
        # Ordinary log text isn't mistaken for a credential.
        for prose in ("oidc token rejected", "Basic settings saved", "job 5 state=failed", "exit code=1"):
            assert logs.scrub(prose) == prose
    finally:
        logs.forget_secrets()


def test_scrub_catches_encoded_secrets_and_secret_shapes(settings, monkeypatch):
    """Every case of an encoded-secret probe comes out redacted, with none
    of the configured values left in it, and short values are caught by their shape."""
    import base64
    import urllib.parse

    password = 'db-pass/with:odd"chars'  # noqa: S105  (a test value)
    key = "aBc-_" + "k" * 38 + "="
    monkeypatch.setenv("POSTGRES_PASSWORD", password)
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", key)
    logs.forget_secrets()
    cases = {
        "plain db password": f"connection failed password={password}",
        "url-encoded db password in DSN": "postgresql://softwarden:"
        + urllib.parse.quote(password, safe="")
        + "@db:5432/softwarden",
        "base64 basic auth": "Authorization: Basic " + base64.b64encode(f"softwarden:{password}".encode()).decode(),
        "repr-escaped": repr({"password": password}),
        "json-escaped": json.dumps({"password": password}),
        "credential key": f"key {key} used",
        "credential key urlsafe->std b64": "key " + key.replace("-", "+").replace("_", "/"),
        "secret key": settings.SECRET_KEY,
        "calendar token query": "GET /api/v1/calendar/renewals.ics?token=swc_abcdefghijklmnop&months=3",
        "token in form body": "token=swc_abcdefghijklmnop&months=3",
        "bearer header": "Authorization: Bearer sw_abcdefghijklmnopqrstuvwxyz0123456789",
        "webhook path secret": "POST https://hooks.example.test/services/T000/B000/XXXXSECRETPATHXXXX failed",
        "oidc callback": "GET /api/v1/auth/oidc/callback?code=0.AAAA-secretcode&state=xyz",
        "oidc callback without a path": "code=0.AAAA-secretcode&state=xyz",
        "invite fragment": "http://localhost/invite#tok_abcdefghijklmnopqrstuvwxyz",
        "userinfo url": "https://user:hunter2hunter2@smtp.example.test/x",
        "short password": "login failed password=abc",
        "anthropic key": "refused sk-ant-api03-abcdefgh",
        "scim token": "token scim_abcdefghijklmnop refused",
    }
    leaks = {
        "plain": password,
        "encoded": urllib.parse.quote(password, safe=""),
        "key": key,
        "std key": key.replace("-", "+").replace("_", "/"),
        "secret key": settings.SECRET_KEY,
    }
    secrets = ("swc_abc", "sw_abc", "XXXXSECRETPATH", "secretcode", "xyz", "tok_", "hunter2", "abc", "sk-ant-", "scim_")
    try:
        for name, text in cases.items():
            out = logs.scrub(text)
            leaked = [n for n, v in leaks.items() if v in out] + [s for s in secrets if s in out]
            assert not leaked, (name, out, leaked)
            assert out != text, name
    finally:
        logs.forget_secrets()


def test_the_redaction_covers_every_new_field_and_the_traceback(settings, captured):
    """Message, request fields (path, method, ids) and the traceback are all scrubbed, whichever way they arrive."""
    settings.OPS_TOKEN = TOKEN
    logs.forget_secrets()
    logger = logging.getLogger("test.redaction")
    with log_context(path=f"/x?token={TOKEN}", method="GET", user=TOKEN, org="o1"):
        logger.warning("saw %s at /y?secret=1", TOKEN, extra={"request_id": TOKEN})
        try:
            raise RuntimeError(f"failed with {TOKEN} at /z?token=abc")
        except RuntimeError:
            logger.exception("boom")
    lines = [x for x in captured() if x["logger"] == "test.redaction"]
    assert len(lines) == 2
    raw = json.dumps(lines)
    assert TOKEN not in raw and "token=" not in raw and "secret=1" not in raw
    first = lines[0]
    assert first["path"] == "/x" and first["user"] == "[redacted]" and first["request_id"] == "[redacted]"
    assert first["message"] == "saw [redacted] at /y"
    assert "exc" in lines[1] and "[redacted]" in lines[1]["exc"]


def test_the_text_format_carries_the_same_fields_scrubbed():
    record = logging.LogRecord("catalog", logging.INFO, __file__, 1, "GET /a?token=s", (), None)
    with unit_of_work() as request_id, log_context(path="/a", method="GET", org="o1"):
        line = logs.TextFormatter().format(record)
    assert "token" not in line
    assert f"request_id={request_id}" in line and "org=o1" in line and "path=/a" in line


def test_the_log_format_setting_is_checked():
    code = "import django; django.setup()"
    env = {**_environ(), "SOFTWARDEN_LOG_FORMAT": "yaml"}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=API, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0 and "SOFTWARDEN_LOG_FORMAT must be json or text" in result.stderr


def test_the_log_fields_lists_agree():
    from core import request_context

    assert logs.FIELDS == request_context.LOG_FIELDS


def test_runserver_and_django_lines_go_through_the_scrubbing_formatter(settings):
    """Django's DEFAULT_LOGGING gives django.server its own formatter and `django` its own DEBUG console handler; the
    settings route both through the console handler with this project's formatter."""
    settings.OPS_TOKEN = TOKEN
    logs.forget_secrets()
    server = logging.getLogger("django.server")
    assert not server.propagate
    # Only this project's formatters (pytest may add its own capture handlers beside them); never Django's own.
    ours = [h for h in server.handlers if isinstance(h.formatter, logs.JsonFormatter | logs.TextFormatter)]
    assert ours and not [h for h in server.handlers if type(h.formatter).__name__ == "ServerFormatter"]
    handler = ours[0]
    assert logging.getLogger("django").handlers == []
    record = server.makeRecord(
        "django.server",
        logging.INFO,
        __file__,
        1,
        '"%s" %s %s',
        (f"GET /api/v1/calendar/renewals.ics?token=swc_secret&x={TOKEN} HTTP/1.1", "200", "12"),
        None,
    )
    line = handler.format(record)
    assert "token=" not in line and "swc_secret" not in line and TOKEN not in line
    assert "/api/v1/calendar/renewals.ics" in line
    logs.forget_secrets()


WSGI_ORDER_CODE = """
import sys, wsgiref.util
from softwarden import otel
seen = []
class Marker:
    def __init__(self, get_response):
        self.get_response = get_response
    def __call__(self, request):
        seen.append(request.path)
        return self.get_response(request)
def configure(service):
    from django.conf import settings
    settings.MIDDLEWARE = ["__main__.Marker", *settings.MIDDLEWARE]
    return True
otel.configure = configure
from softwarden.wsgi import application
environ = {}
wsgiref.util.setup_testing_defaults(environ)
environ["PATH_INFO"] = "/healthz"
application(environ, lambda status, headers: None)
sys.exit(0 if seen == ["/healthz"] else 1)
"""


def test_the_wsgi_application_loads_middleware_after_tracing_is_set_up():
    """softwarden/wsgi.py builds the handler after otel.configure, so middleware the Django instrumentation adds to
    settings.MIDDLEWARE is in the chain gunicorn serves. Checked with a stand-in configure that adds a marker."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", WSGI_ORDER_CODE], cwd=API, env=_environ(), capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_caddy_s_error_log_drops_the_query_string():
    """Caddy's http.log.error lines carry the request's full URI (an upstream 502 while the app restarts): the global
    log filter drops the whole query (it used to delete only `token`, leaving an OIDC code and the setup token;
    checked by hand against caddy 2.11.9, docs/04-development/operations.md)."""
    text = (API.parents[1] / "docker" / "Caddyfile").read_text(encoding="utf-8")
    globals_block = text[: text.index("http://{$PUBLIC_HOSTNAME")]
    compact = " ".join(line.split("#")[0].strip() for line in globals_block.splitlines()).split()
    assert 'log { format filter { wrap json fields { request>uri regexp "\\?.*$" "" } } }' in " ".join(compact)


def _environ():
    import os

    return {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}


# ---- /healthz/ops ---------------------------------------------------------------------------------------------------


@pytest.fixture
def ops_token(settings):
    settings.OPS_TOKEN = TOKEN
    return TOKEN


def bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.mark.django_db
def test_ops_is_a_404_without_credentials_or_with_a_wrong_token(ops_token):
    assert Client().get(OPS).status_code == 404
    assert Client().get(OPS, **bearer("wrong")).status_code == 404
    assert Client().get(OPS, **bearer(ops_token + "x")).status_code == 404
    assert Client().get(OPS, HTTP_AUTHORIZATION=f"Basic {ops_token}").status_code == 404
    assert Client().get(OPS, **bearer("")).status_code == 404


@pytest.mark.django_db
def test_ops_with_no_token_configured_never_matches_an_empty_bearer(settings):
    settings.OPS_TOKEN = ""
    assert Client().get(OPS, HTTP_AUTHORIZATION="Bearer ").status_code == 404
    assert Client().get(OPS, HTTP_AUTHORIZATION="Bearer").status_code == 404


@pytest.mark.django_db
def test_ops_is_a_404_for_editors_and_viewers(a, api, ops_token):
    for role in ("editor", "viewer"):
        assert api(a["users"][role]).client.get(OPS).status_code == 404


@pytest.mark.django_db
def test_ops_answers_the_token_and_an_admin_session(a, api, ops_token):
    by_token = Client().get(OPS, **bearer(ops_token))
    assert by_token.status_code == 200
    assert by_token["Cache-Control"].startswith("max-age=0")
    by_admin = api(a["users"]["admin"]).client.get(OPS)
    assert by_admin.status_code == 200
    assert set(by_token.json()) == set(by_admin.json())


@pytest.mark.django_db
def test_ops_admin_session_only_on_a_single_organization_installation(a, b, api, ops_token):
    """With two organizations only the token opens it."""
    assert api(a["users"]["admin"]).client.get(OPS).status_code == 404
    assert Client().get(OPS, **bearer(ops_token)).status_code == 200


@pytest.mark.django_db
def test_ops_admin_session_not_on_the_cloud_edition(a, api, ops_token):
    from core.models import Edition

    type(a["org"]).objects.filter(pk=a["org"].pk).update(edition=Edition.CLOUD)
    assert api(a["users"]["admin"]).client.get(OPS).status_code == 404
    assert Client().get(OPS, **bearer(ops_token)).status_code == 200


@pytest.mark.django_db
def test_ops_admin_who_must_change_their_password_is_refused(a, api, ops_token):
    from core.models import Membership

    Membership.objects.filter(user=a["users"]["admin"]).update(must_change_password=True)
    assert api(a["users"]["admin"]).client.get(OPS).status_code == 404


@pytest.mark.django_db
def test_ops_session_goes_through_current_membership(a, b, api, ops_token, monkeypatch):
    """With the single-organization rule set aside, the membership is core.access.current_membership's: an Admin of A
    signed in by B's SSO acts as B's Viewer, and must_change_password on any membership refuses."""
    from core import access
    from core.models import Membership, Role

    monkeypatch.setattr("softwarden.ops._single_organization", lambda: True)
    admin = a["users"]["admin"]
    Membership.objects.create(user=admin, organization=b["org"], role=Role.VIEWER)
    client = api(admin).client
    assert client.get(OPS).status_code == 200  # the oldest membership: Admin of A
    session = client.session
    session[access.SSO_ORGANIZATION] = str(b["org"].id)
    session.save()
    assert client.get(OPS).status_code == 404  # confined to B, where they are a Viewer

    other = api(admin).client  # a password session again, Admin of A
    Membership.objects.filter(user=admin, organization=b["org"]).update(must_change_password=True)
    assert other.get(OPS).status_code == 404


def test_the_token_compare_is_over_digests():
    from softwarden import ops

    assert ops._token_matches(TOKEN, TOKEN)
    assert not ops._token_matches(TOKEN[:-1], TOKEN)
    assert not ops._token_matches("", "")
    assert not ops._token_matches(TOKEN, "")


@pytest.mark.django_db
def test_ops_content(a, b, ops_token):
    from datetime import timedelta

    from django.utils import timezone

    from core.models import Job, JobStatus
    from discovery import engine

    old = timezone.now() - timedelta(minutes=10)
    Job.objects.create(kind=engine.RUN_JOB, organization=a["org"], run_after=old)
    Job.objects.create(kind="test.unknown", organization=b["org"], run_after=old)
    Job.objects.create(kind="x", organization=a["org"], status=JobStatus.FAILED, finished_at=timezone.now())
    body = Client().get(OPS, **bearer(ops_token)).json()
    assert set(body) == {
        "version",
        "checked_at",
        "status",
        "database",
        "migrations",
        "worker",
        "schedules",
        "jobs",
        "backup",
        "disk",
    }
    assert body["version"] and body["version"] != "unknown"
    assert body["database"]["state"] == "ok" and body["database"]["latency_ms"] >= 0
    assert body["migrations"] == {"pending": 0}
    assert body["worker"]["state"] == "never" and body["worker"]["age_seconds"] is None
    assert body["status"] == "unhealthy"  # no worker has ticked, as /readyz says
    assert set(body["jobs"]["lanes"]) == {"default", "long"}
    lanes = body["jobs"]["lanes"]
    assert lanes["long"]["queued"] == 1 and lanes["default"]["queued"] == 1
    assert lanes["long"]["oldest_age_seconds"] >= 590
    assert body["jobs"]["failed_24h"] == 1 and body["jobs"]["abandoned_24h"] == 0
    assert body["backup"]["state"] in ("ok", "stale", "never", "not_configured")
    assert "key_present" in body["backup"] and "destination" not in body["backup"]
    assert "media" in body["disk"] and body["disk"]["media"]["free_bytes"] > 0
    assert set(body["schedules"]) == {"count", "stuck", "overdue", "failing"}


@pytest.mark.django_db
def test_ops_body_has_no_org_data_or_secrets(a, b, ops_token, settings):
    raw = Client().get(OPS, **bearer(ops_token)).content.decode()
    for value in (a["org"].name, b["org"].name, str(a["org"].id), a["users"]["admin"].email, ops_token):
        assert value not in raw
    assert settings.SECRET_KEY not in raw
    from core import backup

    key = backup.key_value()
    assert not key or key not in raw


@pytest.mark.django_db
def test_ops_reports_pending_migrations(ops_token, monkeypatch):
    monkeypatch.setattr("django.db.migrations.executor.MigrationExecutor.migration_plan", lambda self, t: [1, 2])
    body = Client().get(OPS, **bearer(ops_token)).json()
    assert body["migrations"] == {"pending": 2} and body["status"] == "migrations_pending"


@pytest.mark.django_db
def test_ops_answers_503_with_the_token_when_the_database_is_down(ops_token, monkeypatch):
    def down():
        raise RuntimeError("no database")

    monkeypatch.setattr("softwarden.ops._probe_database", down)
    response = Client().get(OPS, **bearer(ops_token))
    assert response.status_code == 503
    assert response.json()["database"] == {"state": "unreachable", "latency_ms": None}


@pytest.mark.django_db
def test_ops_token_is_never_logged(ops_token, captured):
    Client().get(OPS, **bearer(ops_token))
    Client().get(OPS, **bearer("wrong-token-value"))
    raw = json.dumps(captured())
    assert ops_token not in raw and "wrong-token-value" not in raw
    assert OPS in raw


# ---- OpenTelemetry --------------------------------------------------------------------------------------------------


def test_otel_is_off_and_never_imported_without_the_endpoint():
    """A fresh process that serves a request with OTEL_EXPORTER_OTLP_ENDPOINT unset imports nothing from
    opentelemetry, even when the extra is installed."""
    code = (
        "import sys, django; django.setup(); "
        "from softwarden import otel, wsgi; "
        "assert otel.configure('worker') is False; "
        "sys.exit(any(m == 'opentelemetry' or m.startswith('opentelemetry.') for m in sys.modules))"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=API, env=_environ(), capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_otel_without_the_extra_warns_and_stays_off():
    if importlib.util.find_spec("opentelemetry.sdk") is not None:
        pytest.skip("the otel extra is installed here; this checks the default image")
    code = (
        "import sys, logging, django; django.setup(); from softwarden import otel; "
        "sys.exit(0 if otel.configure('web') is False and otel.job_span(None) is not None else 1)"
    )
    env = {**_environ(), "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9"}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=API, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "otel extra isn't installed" in result.stdout + result.stderr


def test_otel_span_scrubbing_removes_query_strings():
    class Span:
        def __init__(self, attributes):
            self.attributes = dict(attributes)

        def is_recording(self):
            return True

        def set_attribute(self, name, value):
            self.attributes[name] = value

    class Request:
        request_id = "r-1"

    span = Span({"http.target": "/f.ics?token=s", "url.full": "https://h/f.ics?token=s", "url.query": "token=s"})
    otel._response_hook(span, Request(), None)
    assert span.attributes == {
        "http.target": "/f.ics",
        "url.full": "https://h/f.ics",
        "url.query": "",
        otel.REQUEST_ID: "r-1",
    }


def test_otel_scrub_attributes_covers_exception_events(settings):
    """ScrubbingExporter's rule, which needs no SDK: every string attribute (an exception event's message and stack
    trace included) through softwarden.logs.scrub, and the query attributes emptied."""
    settings.OPS_TOKEN = TOKEN
    logs.forget_secrets()
    try:
        cleaned = otel.scrub_attributes(
            {
                "exception.message": f"refused {TOKEN} for /f.ics?token=swc_x",
                "exception.stacktrace": f'File "x.py"\n  GET /a?token=swc_y {TOKEN}',
                "url.query": "token=swc_z",
                "http.status_code": 500,
                "list": [f"/b?token={TOKEN}", 3],
            }
        )
    finally:
        logs.forget_secrets()
    raw = json.dumps(cleaned)
    assert TOKEN not in raw and "token=" not in raw and "swc_" not in raw
    assert cleaned["exception.message"] == "refused [redacted] for /f.ics"
    assert cleaned["url.query"] == "" and cleaned["http.status_code"] == 500 and cleaned["list"] == ["/b", 3]


ON_TOKEN = "ops-token-for-on-tests-" + "y" * 30
PROBE = Path(__file__).resolve().parent / "otel_probe.py"


def _run_on(service):
    """tests/otel_probe.py in its own process with tracing switched on; CI's WITH_OTEL=1 image leg runs the same file
    inside the built image."""
    env = {
        **_environ(),
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
        "OTEL_BSP_SCHEDULE_DELAY": "60000",
        "SOFTWARDEN_OPS_TOKEN": ON_TOKEN,
        "PYTHONPATH": str(API),
    }
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(PROBE), service], cwd=API, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr
    (line,) = [x for x in result.stdout.splitlines() if x.startswith("RESULT ")]  # the log lines are on stdout too
    return json.loads(line.removeprefix("RESULT "))


# The two tests below need the optional `otel` extra. Locally they skip without it; CI deselects them in the api job
# (-m "not otel": no extra there, like the default image) and runs them in the image job's WITH_OTEL=1 leg, where a
# skip fails the job (ops/ci/test-report-check.py), and runs tests/otel_probe.py inside that image as well.
OTEL_MISSING = pytest.mark.skipif(
    importlib.util.find_spec("opentelemetry.sdk") is None,
    reason="the otel extra isn't installed in this environment (uv sync --extra otel; the image's WITH_OTEL=1)",
)


@pytest.mark.otel
@OTEL_MISSING
def test_otel_on_traces_requests_through_the_wsgi_application():  # pragma: no cover - needs the extra
    """Driven through softwarden.wsgi.application, as gunicorn does: the Django instrumentation's middleware is in the
    chain, the span carries the request id, and the query string is gone."""
    from tests import otel_probe

    otel_probe.check("web", _run_on("web"), ON_TOKEN)


@pytest.mark.otel
@OTEL_MISSING
def test_otel_on_traces_jobs_scrubbed_without_orphan_queries():  # pragma: no cover - needs the extra
    """A job's span carries the job id as the request id, its exception event is scrubbed, and the worker's polling
    outside a job (an orphan query) is dropped."""
    from tests import otel_probe

    otel_probe.check("worker", _run_on("worker"), ON_TOKEN)


def test_the_worker_sampler_warns_when_it_falls_back(monkeypatch, caplog):
    """Observability nit: the sampler reads OTEL_TRACES_SAMPLER through a private SDK helper; when that fails
    it samples everything, and says so once in the log. Stub modules stand in for the optional `otel` extra."""
    import sys
    import types

    sampling: Any = types.ModuleType("opentelemetry.sdk.trace.sampling")

    class Sampler:
        pass

    def broken():
        raise AttributeError("moved in a newer SDK")

    sampling.Sampler = Sampler
    sampling.DEFAULT_ON = types.SimpleNamespace(get_description=lambda: "AlwaysOnSampler")
    sampling._get_from_env_or_default = broken
    trace_api = types.ModuleType("opentelemetry.trace")
    root: Any = types.ModuleType("opentelemetry")
    root.trace = trace_api
    sdk = types.ModuleType("opentelemetry.sdk")
    sdk_trace: Any = types.ModuleType("opentelemetry.sdk.trace")
    sdk_trace.sampling = sampling
    for name, module in {
        "opentelemetry": root,
        "opentelemetry.trace": trace_api,
        "opentelemetry.sdk": sdk,
        "opentelemetry.sdk.trace": sdk_trace,
        "opentelemetry.sdk.trace.sampling": sampling,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    caplog.set_level("WARNING", logger="softwarden.otel")
    made = otel._worker_sampler()
    assert made.get_description() == "DropOrphanQueries(AlwaysOnSampler)"
    assert "OTEL_TRACES_SAMPLER not read (AttributeError)" in caplog.text and "moved" not in caplog.text


def test_worker_tick_lines_never_carry_a_stale_request_id(db, monkeypatch):
    """Observability nit: something outside a unit (a command's first audit event) leaves an id for the rest
    of the context; each tick's own lines carry the tick's fresh id instead, a different one every tick."""
    from core import request_context, worker

    stale = request_context.current_request_id()
    seen: list[str | None] = []
    real = worker.log.info

    def noted(message, *args, **kwargs):
        if message.startswith("worker tick"):
            seen.append(request_context.log_fields().get("request_id"))
        return real(message, *args, **kwargs)

    monkeypatch.setattr(worker.log, "info", noted)
    worker.tick()
    worker.tick()
    assert len(seen) == 2 and None not in seen and str(stale) not in seen and seen[0] != seen[1]
    assert request_context.current_request_id() == stale  # the context's own id is back after the tick
