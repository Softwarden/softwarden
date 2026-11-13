"""Problem details everywhere (RFC 9457; core/problems.py).

Every error the API answers is `application/problem+json` with type, title, status and detail (and `errors` where a
field or parameter is at fault): 400, 401, 403, 404, 405, 406, 409, 411, 412, 413, 415, 429 and 500. Generic sweeps
walk the URL list; the specific cases cover what only some routes do, and the errors Django answers itself (a path no
route matches, a CSRF failure on a non-DRF view, an unhandled exception)."""

from __future__ import annotations

import json
import re
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory, override_settings
from django.urls import get_resolver

from catalog.models import Vendor
from core import problems
from core.drf import PUBLIC, declared_policy
from tests.test_drf_resources import requests_for, send
from tests.test_tokens import Bearer, mint  # noqa: F401  (mint is a fixture)
from tests.test_url_coverage import methods_and_actions, walk

PROBLEM = "application/problem+json"
PARAM = re.compile(r"<(?:(\w+):)?(\w+)>")
ALL_METHODS = ("get", "post", "patch", "put", "delete")


def is_problem(response, status: int, problem_type: str | None = None) -> dict:
    assert response.status_code == status, (response.status_code, response.content[:300])
    assert response["Content-Type"] == PROBLEM, (response.request["PATH_INFO"], response["Content-Type"])
    body = json.loads(response.content)
    assert {"type", "title", "status", "detail"} <= set(body), body
    assert body["status"] == status and isinstance(body["title"], str) and isinstance(body["detail"], str), body
    assert body["type"] in problems.PROBLEM_TYPES, body["type"]  # every type is a documented one
    assert body["title"] == problems.PROBLEM_TYPES[body["type"]][1] or body["type"] == "/problems/too-many-events"
    if problem_type is not None:
        assert body["type"] == problem_type, body
    if "errors" in body:
        assert all(isinstance(v, list) and all(isinstance(m, str) for m in v) for v in body["errors"].values()), body
    return body


def dummy(route: str) -> str:
    """The route with placeholder IDs (authentication and the method check come before any lookup)."""

    def value(m):
        converter = m.group(1) or "str"
        return {"uuid": str(uuid.uuid4()), "int": "1"}.get(converter, "x")

    return "/" + PARAM.sub(value, route)


def api_routes():
    """[(path with placeholders, {methods it answers}, policy)] for every DRF route under /api/."""
    found = []
    for route, callback in walk(get_resolver().url_patterns):
        if not route.startswith("api/") or getattr(callback, "cls", None) is None:
            continue
        found.append((dummy(route), {m for m, _ in methods_and_actions(callback)}, declared_policy(callback)))
    return found


@pytest.fixture(autouse=True)
def empty_cache():
    """Scoped throttles (sign-in, setup, search) count in the process cache across tests; the sweeps start clean."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def test_the_problem_types_have_one_status_and_title_each():
    for problem_type, (status, title) in problems.PROBLEM_TYPES.items():
        assert problem_type.startswith("/problems/") and 400 <= status < 600 and title, problem_type
    assert set(problems.STATUS_TYPES.values()) <= set(problems.PROBLEM_TYPES)


# ---- generic sweeps over the URL list --------------------------------------------------------------------------------


@pytest.mark.django_db
def test_every_route_answers_a_method_it_lacks_with_a_405_problem():
    from core import counters

    client = Client()
    checked = 0
    for path, methods, _ in api_routes():
        # DRF throttles before it answers 405, and sign-in's start and callback share one bucket; the
        # buckets are the shared database counters.
        counters.reset()
        for method in ALL_METHODS:
            if method in methods:
                continue
            response = send(client, method, path)
            body = is_problem(response, 405, problems.METHOD_NOT_ALLOWED)
            assert method.upper() in body["detail"], (path, body)
            assert "Allow" in response, path
            checked += 1
    assert checked > 500


@pytest.mark.django_db
def test_every_signed_in_route_refuses_a_request_without_credentials_with_a_problem():
    client = Client()
    checked = 0
    for path, methods, policy in api_routes():
        for method in methods:
            if policy.role_for(method) == PUBLIC:
                continue
            response = send(client, method, path)
            is_problem(response, 403, problems.NOT_SIGNED_IN)
            checked += 1
    assert checked > 300


@pytest.mark.django_db
def test_every_signed_in_route_refuses_an_unknown_token_with_a_401_problem(settings):
    # The sweep sends 300+ refused tokens from one address; the per-address limit on them (429 past
    # SOFTWARDEN_INVALID_TOKEN_RATE) is tests/test_shared_limits.py's. Here every route's 401 shape is checked.
    settings.INVALID_TOKEN_RATE = ""
    client = Bearer("sw_" + "x" * 40).client
    checked = 0
    for path, methods, policy in api_routes():
        for method in methods:
            if policy.role_for(method) == PUBLIC:
                continue
            response = send(client, method, path)
            body = is_problem(response, 401, problems.INVALID_TOKEN)
            assert body["detail"] == "This API token isn't recognized."
            assert response["WWW-Authenticate"].startswith("Bearer "), path
            checked += 1
    assert checked > 300


# Every route and capability, the preview features (Okta, Google Workspace, ServiceNow, Halo) included.
@pytest.mark.usefixtures("preview_on")
def test_every_list_refuses_an_unknown_parameter_with_a_400_problem_naming_it(a):
    client = Client()
    client.force_login(a["users"]["admin"])
    from core.capabilities import set_edition

    a["org"], _ = set_edition(a["org"], "enterprise")
    lists = [r for r in requests_for(a, a["users"]["admin"]) if r[2] == "list"]
    assert len(lists) > 40
    for _method, path, _action, cls in lists:
        joiner = "&" if "?" in path else "?"
        response = client.get(f"{path}{joiner}colour=red")
        body = is_problem(response, 400, problems.INVALID_REQUEST)
        assert body["errors"]["colour"] == ["This filter isn't available here."], (cls.__name__, body)


# Every route and capability, the preview features (Okta, Google Workspace, ServiceNow, Halo) included.
@pytest.mark.usefixtures("preview_on")
def test_every_viewset_route_answers_another_organizations_id_with_a_404_problem(a, b):
    """test_drf_resources checks the status and media type; here the whole body."""
    client = Client()
    client.force_login(a["users"]["admin"])
    from core.capabilities import set_edition

    set_edition(a["org"], "enterprise")
    for method, path, _action, _cls in requests_for(b, b["users"]["admin"], keyed_only=True):
        body = is_problem(send(client, method, path), 404, problems.NOT_FOUND)
        assert body["detail"] in (problems.NOT_FOUND_DETAIL, "Not found."), (path, body)


# ---- specific cases ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/v1/nothing-here", "/api/v2/applications", "/api/", "/api/v1/applications/x/y"])
@pytest.mark.django_db
def test_a_path_no_route_matches_is_a_404_problem(path):
    body = is_problem(Client().get(path), 404, problems.NOT_FOUND)
    assert body["detail"] == problems.NO_ROUTE_DETAIL


@pytest.mark.parametrize("path", ["/api/v1/applications/not-a-uuid", "/api/v1/contracts/42/archive"])
@pytest.mark.django_db
def test_a_record_route_with_a_malformed_id_says_the_record_does_not_exist(path):
    # The same detail as an unknown UUID, not "no API endpoint at this path".
    body = is_problem(Client().get(path), 404, problems.NOT_FOUND)
    assert body["detail"] == problems.NOT_FOUND_DETAIL


def test_a_path_outside_the_api_still_falls_through_to_the_spa(db):
    response = Client().get("/no-such-page")
    assert response["Content-Type"] != PROBLEM


def test_validation_errors_are_400_problems_with_errors(a, api):
    response = api(a["users"]["editor"]).post("applications", {"name": "", "lifecycle": "sideways"})
    body = is_problem(response, 400, problems.INVALID_REQUEST)
    assert set(body["errors"]) >= {"name", "lifecycle"}
    assert body["detail"] == "Some fields need attention."


def test_a_malformed_json_body_is_a_400_problem(a):
    client = Client()
    client.force_login(a["users"]["editor"])
    response = client.post("/api/v1/applications", "{not json", content_type="application/json")
    is_problem(response, 400, problems.MALFORMED_REQUEST)


def test_a_body_that_isnt_an_object_is_a_400_problem(a, api):
    response = api(a["users"]["editor"]).client.post("/api/v1/vendors", "[1, 2]", content_type="application/json")
    body = is_problem(response, 400, problems.INVALID_REQUEST)
    assert body["errors"] == {"non_field": ["Send a JSON object."]}


def test_role_refusals_read_only_tokens_and_capabilities_are_typed_403_problems(a, api, mint):  # noqa: F811
    viewer = api(a["users"]["viewer"])
    is_problem(viewer.post("vendors", {"name": "Mine"}), 403, problems.FORBIDDEN)
    reader = Bearer(mint("admin", read_only=True)["secret"])
    body = is_problem(reader.post("vendors", {"name": "Mine"}), 403, problems.READ_ONLY_TOKEN)
    assert body["detail"] == "This token is read-only."
    from tests.conftest import revoke_capability

    revoke_capability(a["org"], "discovery.halo")
    body = is_problem(api(a["users"]["admin"]).get("connections/halo"), 403, problems.CAPABILITY_REQUIRED)
    assert body["capability"]


def test_a_session_write_without_the_csrf_token_is_a_csrf_problem(a):
    client = Client(enforce_csrf_checks=True)
    client.force_login(a["users"]["editor"])
    response = client.post("/api/v1/vendors", json.dumps({"name": "No token"}), content_type="application/json")
    body = is_problem(response, 403, problems.CSRF_FAILED)
    assert body["detail"].startswith("CSRF Failed:")
    assert not Vendor.objects.filter(name="No token").exists()


def test_a_csrf_failure_outside_drf_is_a_csrf_problem_under_api_only(a, rf):
    """CsrfViewMiddleware refuses a session POST to a plain Django view before it runs: CSRF_FAILURE_VIEW answers the
    problem under /api/ and Django's page elsewhere (the SPA catch-all)."""
    client = Client(enforce_csrf_checks=True)
    client.force_login(a["users"]["editor"])
    page = client.post("/applications/new", {"x": "1"})
    assert page.status_code == 403 and page["Content-Type"] != PROBLEM
    response = problems.csrf_failure(rf.post("/api/v1/anything"), "CSRF token missing.")
    assert response.status_code == 403 and response["Content-Type"] == PROBLEM
    body = json.loads(response.content)
    assert body["type"] == problems.CSRF_FAILED and body["detail"] == "CSRF Failed: CSRF token missing."


def test_conflicts_are_409_problems(a, api):
    editor = api(a["users"]["editor"])
    team = editor.post("teams", {"name": "Held team"}).json_body
    editor.patch(f"applications/{a['app'].id}", {"support_team": team["id"]})
    in_use = is_problem(api(a["users"]["admin"]).delete(f"teams/{team['id']}"), 409, problems.CONFLICT)
    assert in_use["blocking_references"]
    job = editor.post("components", {"name": "Twice job", "lifecycle": "active"}).json_body
    link = {"type": "depends_on", "source": job["id"], "target": str(a["app"].id)}
    assert editor.post("relationships", link).status_code == 201
    is_problem(editor.post("relationships", link), 409, "/problems/duplicate-relationship")


def test_a_stale_if_match_is_a_412_problem_with_the_current_revision(a, api):
    client = api(a["users"]["editor"]).client
    path = f"/api/v1/applications/{a['app'].id}"
    response = client.patch(
        path, json.dumps({"description": "x"}), content_type="application/json", HTTP_IF_MATCH='"' + "0" * 32 + '"'
    )
    body = is_problem(response, 412, problems.PRECONDITION_FAILED)
    assert response["ETag"] == f'"{body["revision"]}"'


def test_an_unsupported_body_type_is_a_415_problem(a):
    client = Client()
    client.force_login(a["users"]["editor"])
    response = client.post("/api/v1/vendors", "name=Plain", content_type="text/plain")
    body = is_problem(response, 415, problems.UNSUPPORTED_MEDIA_TYPE)
    assert "text/plain" in body["detail"]


@pytest.mark.parametrize("charset", ["utf-16", "UTF-16LE", "latin-1", '"iso-8859-1"'])
def test_json_in_a_charset_other_than_utf8_is_a_415_problem(a, charset):
    # JSON between systems is UTF-8 (RFC 8259); another declared charset isn't decoded in it.
    from catalog.models import Vendor

    client = Client()
    client.force_login(a["users"]["editor"])
    body = json.dumps({"name": "Wide"})  # the test client encodes text in the declared charset
    response = client.post("/api/v1/vendors", body, content_type=f"application/json; charset={charset}")
    assert is_problem(response, 415, problems.UNSUPPORTED_MEDIA_TYPE)["detail"].startswith("Send JSON as UTF-8")
    assert not Vendor.objects.filter(name="Wide").exists()
    ok = client.post("/api/v1/vendors", b'{"name": "Narrow"}', content_type="application/json; charset=UTF-8")
    assert ok.status_code == 201
    ascii_ok = client.post("/api/v1/vendors", b'{"name": "Plain"}', content_type="application/json; charset=US-ASCII")
    assert ascii_ok.status_code == 201  # ASCII is a subset of UTF-8


def test_an_unacceptable_accept_header_is_a_406_problem(a):
    client = Client()
    client.force_login(a["users"]["viewer"])
    response = client.get("/api/v1/vendors", HTTP_ACCEPT="text/html")
    is_problem(response, 406, problems.NOT_ACCEPTABLE)


@override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=1024)
def test_an_oversize_json_body_is_a_413_problem_before_anything_reads_it(a):
    client = Client()
    client.force_login(a["users"]["editor"])
    big = json.dumps({"name": "Large", "notes": "x" * 4096})
    response = client.post("/api/v1/vendors", big, content_type="application/json")
    body = is_problem(response, 413, problems.CONTENT_TOO_LARGE)
    assert "1 KB" in body["detail"]
    assert not Vendor.objects.filter(name="Large").exists()
    small = client.post("/api/v1/vendors", json.dumps({"name": "Small"}), content_type="application/json")
    assert small.status_code == 201


def test_an_oversize_and_a_lengthless_upload_are_413_and_411_problems(a, settings):
    settings.MAX_REQUEST_MB = 1
    client = Client()
    client.force_login(a["users"]["editor"])
    big = SimpleUploadedFile("big.txt", b"a" * (2 * 1024 * 1024))
    path = f"/api/v1/applications/{a['app'].id}/attachments"
    is_problem(client.post(path, {"file": big}), 413, problems.CONTENT_TOO_LARGE)
    from core.upload_limit import RequestSizeLimitMiddleware

    request = RequestFactory().post(path, {"file": SimpleUploadedFile("n.txt", b"x")})
    request.META["CONTENT_LENGTH"] = ""
    response = RequestSizeLimitMiddleware(lambda r: None)(request)
    assert response.status_code == 411 and json.loads(response.content)["type"] == problems.LENGTH_REQUIRED


def test_an_unhandled_exception_is_a_500_problem_without_the_stack_or_its_message(a, monkeypatch, caplog):
    from catalog import directory_viewsets

    leak = "boom sw_secretvalue1234567890 password=hunter2"

    def explode(self, request, *args, **kwargs):
        raise RuntimeError(leak)

    monkeypatch.setattr(directory_viewsets.VendorViewSet, "list", explode)
    client = Client(raise_request_exception=False)
    client.force_login(a["users"]["viewer"])
    response = client.get("/api/v1/vendors")
    body = is_problem(response, 500, problems.SERVER_ERROR)
    text = response.content.decode()
    assert "sw_secret" not in text and "hunter2" not in text and "Traceback" not in text and "RuntimeError" not in text
    assert body["detail"] == problems.SERVER_ERROR_DETAIL
    assert any(r.levelname == "ERROR" for r in caplog.records)  # Django logged it for the operator


@pytest.mark.parametrize("where", ["view", "middleware"])
def test_an_unreachable_database_is_a_503_with_retry_after(a, monkeypatch, where):
    # With the database down, an API call answers 503 database-unavailable, not a 500 that reads as a bug.
    from django.db import OperationalError

    from catalog import directory_viewsets

    def down(*args, **kwargs):
        raise OperationalError('connection to server at "db" (172.18.0.2), port 5432 failed: Connection refused')

    client = Client(raise_request_exception=False)
    client.force_login(a["users"]["viewer"])
    if where == "view":
        monkeypatch.setattr(directory_viewsets.VendorViewSet, "list", down)
    else:  # the session is read before any view runs
        monkeypatch.setattr("django.contrib.sessions.backends.db.SessionStore.load", down)
    response = client.get("/api/v1/vendors")
    body = is_problem(response, 503, problems.DATABASE_UNAVAILABLE)
    assert response["Retry-After"] == "30" and "172.18" not in response.content.decode()
    assert body["detail"] == problems.DATABASE_UNAVAILABLE_DETAIL


@pytest.mark.parametrize(
    ("cause", "status"),
    [
        ("DeadlockDetected", 500),  # 40P01
        ("SerializationFailure", 500),  # 40001
        ("DiskFull", 500),  # 53100
        ("OutOfMemory", 500),  # 53200
        ("AdminShutdown", 503),  # 57P01
        ("CannotConnectNow", 503),  # 57P03
        ("TooManyConnections", 503),  # 53300
        ("ConnectionFailure", 503),  # 08006
    ],
)
def test_only_connection_failures_are_503(a, monkeypatch, cause, status):
    # A deadlock or a full disk is the server's problem to report (500), not "try again" (503).
    import psycopg.errors
    from django.db import OperationalError

    from catalog import directory_viewsets

    def fail(*args, **kwargs):
        raise OperationalError("refused") from getattr(psycopg.errors, cause)("refused")

    monkeypatch.setattr(directory_viewsets.VendorViewSet, "list", fail)
    client = Client(raise_request_exception=False)
    client.force_login(a["users"]["viewer"])
    is_problem(client.get("/api/v1/vendors"), status, problems.DATABASE_UNAVAILABLE if status == 503 else None)


def test_the_django_level_handlers_answer_problems_under_api_only(rf):
    for handler, status in (
        (problems.bad_request, 400),
        (problems.permission_denied, 403),
        (problems.page_not_found, 404),
        (problems.server_error, 500),
    ):
        api = handler(rf.get("/api/v1/anything"))
        assert api.status_code == status and api["Content-Type"] == PROBLEM, handler
        page = handler(rf.get("/somewhere"))
        assert page.status_code == status and page["Content-Type"] != PROBLEM, handler


def test_the_handlers_are_installed():
    from softwarden import urls

    assert urls.handler404 == "core.problems.page_not_found"
    assert urls.handler500 == "core.problems.server_error"
    assert urls.handler400 == "core.problems.bad_request"
    assert urls.handler403 == "core.problems.permission_denied"
    from django.conf import settings

    assert settings.CSRF_FAILURE_VIEW == "core.problems.csrf_failure"


def test_a_wrong_password_is_the_invalid_credentials_problem(a):
    from django.core.cache import cache

    cache.clear()
    response = Client().post(
        "/api/v1/auth/login",
        json.dumps({"email": a["users"]["editor"].email, "password": "wrong-password"}),
        content_type="application/json",
    )
    body = is_problem(response, 403, problems.INVALID_CREDENTIALS)
    assert body["detail"] == "That email and password don't match an active account."  # the sign-in page shows it
