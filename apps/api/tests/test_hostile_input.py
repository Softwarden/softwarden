"""Hostile input is a 400 problem, never a 500 (pre-release review 1.0).

A NUL character, an unpaired surrogate, a number with thousands of digits or a non-ASCII digit ("²"), a value of the
wrong JSON type, an edge date or a deeply nested body each used to reach PostgreSQL or `int()` and answer 500. They
are refused centrally (core/parsers.py, core/input.py, core/problems.UNUSABLE_INPUT); these tests send each class to
a list, an export, a composite route, /people and /organization, and sweep every documented query parameter and the
request-body fields of the main write endpoints for any 5xx."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from catalog.models import Person
from core.input import MAX_DEPTH, body_errors, text_problem, whole_number

NUL = "a\x00b"
SURROGATE = "\ud800x"
HUGE = "9" * 5000
SUPERSCRIPT_TWO = "²"


# ---- the helpers ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,low,high,expected",
    [
        ("12", 0, 100, 12),
        (12, 0, 100, 12),
        ("0", 0, 100, 0),
        ("", 0, 100, None),
        (SUPERSCRIPT_TWO, 0, 100, None),
        ("٣", 0, 100, None),  # Arabic-Indic three: isdecimal() but not ASCII
        (HUGE, 0, 2**63 - 1, None),
        ("9" * 19, 0, 2**63 - 1, None),
        ("9" * 18, 0, 2**63 - 1, 10**18 - 1),
        (True, 0, 100, None),
        (1.0, 0, 100, None),
        ("-1", 0, 100, None),
        ("101", 0, 100, None),
        (None, 0, 100, None),
    ],
)
def test_whole_number(value, low, high, expected):
    assert whole_number(value, low, high) == expected


def test_text_problem_and_body_errors():
    assert text_problem("fine \U0001f600") is None
    assert text_problem(NUL) and text_problem(SURROGATE)
    errors = body_errors({"name": NUL, "lines": [{"notes": "ok"}, {"notes": SURROGATE}], "n": 1, "ok": "x"})
    assert set(errors) == {"name", "lines[1].notes"}
    assert body_errors([NUL]) == {"[0]": [text_problem(NUL)]}
    deep: object = "x"
    for _ in range(MAX_DEPTH + 1):
        deep = {"a": deep}
    assert list(body_errors(deep)) == [".".join(["a"] * MAX_DEPTH)]  # named where the depth ran out


# ---- the acceptance: each class against one list, one export, one composite route, /people and /organization ---------


def _raw(client, method, path, body: str):
    response = getattr(client, method)("/api/v1/" + path, data=body, content_type="application/json")
    response.json_body = response.json() if "json" in response.get("Content-Type", "") else None
    return response


def _assert_problem(response, field=None):
    assert response.status_code == 400, (response.status_code, response.content[:300])
    assert response["Content-Type"].startswith("application/problem+json")
    if field is not None:
        assert field in response.json_body["errors"], response.json_body


QUERY_CASES = [
    # (path, parameter that must be named)
    ("applications?q=a%00b", "q"),
    ("applications?tag=a%00b", "tag"),
    (f"applications?page_size={HUGE}", "page_size"),
    ("applications?page_size=%C2%B2", "page_size"),
    (f"applications?completeness__gte={HUGE}", "completeness__gte"),
    ("applications?completeness__lt=%C2%B2", "completeness__lt"),
    (f"applications?it_steward={HUGE}", "it_steward"),
    ("applications/export.csv?q=a%00b", "q"),
    (f"applications/export.csv?completeness__gte={HUGE}", "completeness__gte"),
    ("applications/export.xlsx?tag=a%00b", "tag"),
    (f"composite/renewals?within_days={HUGE}", "within_days"),
    (f"composite/renewals?limit={HUGE}", "limit"),
    ("composite/search?q=a%00b", "q"),
    (f"dependencies?depth={HUGE}", "depth"),
    ("search?q=a%00b", "q"),
    (f"search?q=zoom&limit={HUGE}", "limit"),
    ("people?q=a%00b", "q"),
    ("audit-events?action=a%00b", "action"),
    (f"audit-events?actor={HUGE}", "actor"),
    ("contracts?cost_center=a%00b", "cost_center"),
    (f"reports/renewals-calendar?months={HUGE}", "months"),
]


@pytest.mark.parametrize("path,field", QUERY_CASES)
def test_hostile_query_parameters_are_400_problems(a, api, path, field):
    response = api(a["users"]["admin"]).client.get("/api/v1/" + path)
    response.json_body = response.json()
    _assert_problem(response, field)


@pytest.mark.parametrize(
    "path", ["audit-events?to=9999-12-31", "audit-events?from=0001-01-01", "discovery/runs?to=9999-12-31"]
)
def test_edge_dates_in_filters_are_clamped_not_500(a, api, path):
    """A day outside 1900..9998 is clamped: every stored timestamp is inside that window, so the rows are the same."""
    response = api(a["users"]["admin"]).client.get("/api/v1/" + path)
    assert response.status_code == 200, response.content[:300]


BODY_VALUES = [
    ("nul", NUL),
    ("surrogate", SURROGATE),
    ("int", 123),
    ("bool", True),
    ("list", ["x"]),
    ("dict", {"x": 1}),
]


@pytest.mark.parametrize("label,value", BODY_VALUES)
@pytest.mark.parametrize("field", ["name", "email", "title"])
def test_people_refuse_hostile_text(a, api, field, label, value):
    editor = api(a["users"]["editor"])
    person = a["people"]["dana"]
    before = Person.objects.values().get(id=person.id)
    _assert_problem(editor.patch(f"people/{person.id}", {field: value}), field)
    assert Person.objects.values().get(id=person.id) == before
    count = Person.objects.count()
    body = {"name": "New person", "email": "new@example.test", field: value}
    _assert_problem(editor.post("people", body), field)
    assert Person.objects.count() == count


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", NUL),
        ("name", SURROGATE),
        ("name", 123),
        ("name", ["x"]),
        ("timezone", ["Europe/Paris"]),
        ("timezone", {"x": 1}),
        ("timezone", NUL),
        ("coordinator", "not-a-number"),
        ("coordinator", HUGE),
        ("coordinator", [1]),
        ("coordinator", NUL),
    ],
)
def test_organization_refuses_hostile_values(a, api, field, value):
    org = a["org"]
    before = (org.name, org.timezone, org.coordinator_id)
    _assert_problem(api(a["users"]["admin"]).patch("organization", {field: value}), field)
    org.refresh_from_db()
    assert (org.name, org.timezone, org.coordinator_id) == before


def test_a_nul_or_surrogate_anywhere_in_a_body_names_the_field(a, api):
    editor = api(a["users"]["editor"])
    r = editor.post("applications", {"name": "ok", "lifecycle": "active", "description": NUL})
    _assert_problem(r, "description")
    assert "NUL" in r.json_body["errors"]["description"][0]
    assert NUL not in json.dumps(r.json_body)  # the value is never echoed (it may be a secret)
    r = editor.patch(f"applications/{a['app'].id}", {"custom_fields": {"x": SURROGATE}})
    _assert_problem(r, "custom_fields.x")
    r = editor.post("applications", {"name": "ok", "lifecycle": "active", NUL: 1})
    _assert_problem(r, "non_field")


def test_deep_nesting_is_a_400(a, api):
    editor = api(a["users"]["editor"]).client
    nested = "[" * 40 + "]" * 40
    body = '{"name": "x", "lifecycle": "active", "tags": ' + nested + "}"
    _assert_problem(_raw(editor, "post", "applications", body))
    # Past Python's recursion limit the JSON decoder itself gives up: a malformed request, not a RecursionError.
    very = "[" * 100_000 + "]" * 100_000
    response = _raw(editor, "post", "applications", very)
    assert response.status_code == 400
    assert response.json_body["type"] == "/problems/malformed-request"


def test_the_net_turns_unusable_input_into_400(a, api, monkeypatch, caplog):
    """An exception only input can cause, raised past every parser, is a 400 and is logged without its message."""
    from django.db import DataError

    from catalog import services

    def explode(*args, **kwargs):
        raise DataError("PostgreSQL text fields cannot contain NUL (0x00) bytes: secret-value")

    monkeypatch.setattr(services, "warn_duplicate_name", explode)
    response = api(a["users"]["editor"]).post("applications", {"name": "ok", "lifecycle": "active"})
    assert response.status_code == 400
    assert response.json_body["type"] == "/problems/invalid-request"
    assert "secret-value" not in caplog.text and "DataError" in caplog.text


# ---- the sweep: every documented query parameter, and the body fields of the main writes -----------------------------

NET_LOG = "Refused unusable input"
SWEEP_QUERY_VALUES = [NUL, HUGE, SUPERSCRIPT_TWO, "9999-12-31", "0001-01-01", str(2**63)]
# Routes the sweep leaves out: sign-in and setup flows, the operator console, the schema itself, and the two reads
# that would call a public service.
SKIP_PREFIXES = ("/api/v1/auth/", "/api/v1/platform", "/api/v1/setup", "/api/v1/schema", "/api/v1/signup")
SKIP_PATHS = ("end-of-life/products", "ticket-links/parse")


@pytest.fixture
def schema(a, api, settings):
    """The served OpenAPI document, with the rate limits off: a sweep sends thousands of requests, and a 429 would
    hide the answer it is looking for."""
    settings.API_READ_RATE = settings.API_WRITE_RATE = settings.SEARCH_RATE = ""
    return json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)


def _fill(template: str, a) -> str | None:
    """A concrete path for a template whose only parameter is an application's id, else None."""
    if "{" not in template:
        return template
    if template.startswith("/api/v1/applications/{id}") and template.count("{") == 1:
        return template.replace("{id}", str(a["app"].id))
    return None


def test_no_documented_query_parameter_answers_500_to_hostile_values(a, api, schema, caplog):
    client = api(a["users"]["admin"]).client
    five: list[str] = []
    statuses: Counter[int] = Counter()
    tried = 0
    for template, item in sorted(schema["paths"].items()):
        op = item.get("get")
        if not op or template.startswith(SKIP_PREFIXES) or any(s in template for s in SKIP_PATHS):
            continue
        path = _fill(template, a)
        if path is None:
            continue
        for param in [p for p in op.get("parameters", []) if p.get("in") == "query"]:
            for value in SWEEP_QUERY_VALUES:
                tried += 1
                response = client.get(path, {param["name"]: value})
                statuses[response.status_code] += 1
                if response.status_code >= 500:
                    five.append(f"{template}?{param['name']}={value[:12]!r}")
    assert tried > 1000
    assert five == []
    assert 429 not in statuses and statuses[400] > 1000, statuses
    assert NET_LOG not in caplog.text  # every refusal came from a guard, none from the net in core/problems


SWEEP_BODY_VALUES = [NUL, SURROGATE, 123, True, [], {}, HUGE, [[["x"]]]]


def _props(schema, op) -> list[str]:
    content = (op.get("requestBody") or {}).get("content", {})
    ref = (content.get("application/json") or {}).get("schema", {})
    if "$ref" in ref:
        ref = schema["components"]["schemas"][ref["$ref"].split("/")[-1]]
    return list(ref.get("properties", {}))


def test_no_write_answers_500_to_hostile_body_values(a, api, schema, caplog):
    admin = api(a["users"]["admin"])
    app = a["app"].id
    vendor = admin.post("vendors", {"name": "Sweep vendor"}).json_body["id"]
    contract = admin.post("contracts", {"name": "Sweep contract"}).json_body["id"]
    renewal = admin.post("renewals", {"name": "Sweep renewal"}).json_body["id"]
    instance = admin.post(f"applications/{app}/instances", {"name": "Production"}).json_body["id"]
    category = admin.post("categories", {"name": "Sweep category"}).json_body["id"]
    department = admin.post("departments", {"name": "Sweep department"}).json_body["id"]
    targets = {
        "/api/v1/applications/{id}": f"applications/{app}",
        "/api/v1/applications/{id}/instances/{instance_pk}": f"applications/{app}/instances/{instance}",
        "/api/v1/people/{id}": f"people/{a['people']['dana'].id}",
        "/api/v1/teams/{id}": f"teams/{a['team'].id}",
        "/api/v1/vendors/{id}": f"vendors/{vendor}",
        "/api/v1/categories/{id}": f"categories/{category}",
        "/api/v1/departments/{id}": f"departments/{department}",
        "/api/v1/contracts/{id}": f"contracts/{contract}",
        "/api/v1/renewals/{id}": f"renewals/{renewal}",
        "/api/v1/organization": "organization",
    }
    dana = str(a["people"]["dana"].id)
    creates = {
        "/api/v1/applications": ("applications", {"name": "fz", "lifecycle": "active"}),
        "/api/v1/components": ("components", {"name": "fz", "lifecycle": "active"}),
        "/api/v1/applications/{id}/instances": (f"applications/{app}/instances", {"name": "fz"}),
        "/api/v1/applications/{id}/reviews": (f"applications/{app}/reviews", {"confirmed_by": dana, "evidence": "x"}),
        "/api/v1/vendors": ("vendors", {"name": "fz"}),
        "/api/v1/people": ("people", {"name": "fz"}),
        "/api/v1/teams": ("teams", {"name": "fz"}),
        "/api/v1/categories": ("categories", {"name": "fz"}),
        "/api/v1/departments": ("departments", {"name": "fz"}),
        "/api/v1/contracts": ("contracts", {"name": "fz"}),
        "/api/v1/renewals": ("renewals", {"name": "fz"}),
    }
    five: list[str] = []
    statuses: Counter[int] = Counter()
    n = 0
    for template, path in targets.items():
        for field in _props(schema, schema["paths"][template]["patch"]):
            for value in SWEEP_BODY_VALUES:
                response = admin.patch(path, {field: value})
                statuses[response.status_code] += 1
                if response.status_code >= 500:
                    five.append(f"PATCH {template} {field}={str(value)[:12]!r}")
    for template, (path, base) in creates.items():
        fields = _props(schema, schema["paths"][template]["post"])
        assert fields, template
        for field in fields:
            for value in SWEEP_BODY_VALUES:
                n += 1
                body = {**base, field: value}
                if "name" in base and field != "name":
                    body["name"] = f"fz-{n}"
                response = admin.post(path, body)
                statuses[response.status_code] += 1
                if response.status_code >= 500:
                    five.append(f"POST {template} {field}={str(value)[:12]!r}")
    assert five == []
    assert 429 not in statuses and statuses[400] > 500, statuses
    assert NET_LOG not in caplog.text  # every refusal came from a guard, none from the net in core/problems


@pytest.mark.parametrize("value", [123, True, ["x"], {"x": 1}, NUL])
def test_sign_in_with_a_hostile_email_is_refused_not_500(db, api, value):
    response = api().post("auth/login", {"email": value, "password": value})
    assert response.status_code in (400, 403), response.content[:300]


def test_a_nul_in_next_still_signs_in_to_the_dashboard(a, legacy_entra):
    """The sign-in navigations are outside the query guard: their handlers sanitize `next`, and a browser must end on
    a page, not a JSON problem."""
    from django.test import Client

    from tests.sso_helpers import sign_in

    assert sign_in(Client(), "admin@orga.test", next_path="/\x00/evil.example")["Location"] == "/dashboard"
    started = Client().get("/api/v1/auth/oidc/start?next=/%00/evil.example")
    assert started.status_code == 302


def test_over_long_names_are_field_errors_not_the_net(a, api, caplog):
    admin = api(a["users"]["admin"])
    _assert_problem(admin.patch("organization", {"name": "x" * 100_000}), "name")
    instance = admin.post(f"applications/{a['app'].id}/instances", {"name": "Production"}).json_body["id"]
    path = f"applications/{a['app'].id}/instances/{instance}"
    _assert_problem(admin.patch(path, {"name": "x" * 100_000}), "name")
    _assert_problem(admin.patch(path, {"url": "https://example.test/" + "x" * 300}), "url")
    assert NET_LOG not in caplog.text


def test_the_net_logs_the_application_frame(a, api, monkeypatch, caplog):
    """The net's warning names the line in this application that let the input through, not the database driver."""
    from catalog import services

    def explode(*args, **kwargs):
        # The exception is raised inside the standard library's JSON decoder, below this application's frame.
        json.loads("[" * 100_000 + "]" * 100_000)

    monkeypatch.setattr(services, "warn_duplicate_name", explode)
    response = api(a["users"]["editor"]).post("applications", {"name": "ok", "lifecycle": "active"})
    assert response.status_code == 400
    line = next(r.getMessage() for r in caplog.records if NET_LOG in r.getMessage())
    assert "RecursionError" in line and "test_hostile_input.py" in line, line


@pytest.mark.parametrize("password", [123, None, ["x"], {"x": 1}])
def test_bootstrap_refuses_a_password_that_is_not_text(db, password):
    from catalog import admin_services
    from core.models import BootstrapState
    from core.problems import FieldErrors

    BootstrapState.objects.update_or_create(pk=1, defaults={"completed_at": None})
    form = {"org_name": "Fabrikam", "timezone": "UTC", "email": "ana@fabrikam.test", "name": "Ana Silva"}
    with pytest.raises(FieldErrors) as refused:
        admin_services.bootstrap_admin({**form, "password": password})
    assert "password" in refused.value.errors
    with pytest.raises(FieldErrors) as refused:
        admin_services.bootstrap_platform_staff({"email": "ops@fabrikam.test", "name": "Ops", "password": password})
    assert "password" in refused.value.errors
