"""Required If-Match in the API: `SOFTWARDEN_REQUIRE_IF_MATCH`. On (the installation default since the web app sends If-Match from every
form), a signed-in session's PATCH or DELETE (and every conditional action) without If-Match is 428
/problems/precondition-required and changes nothing; API tokens (scripts, the MCP server) keep the unconditional write.
Off (`0`), nothing changes. The rest of the suite runs with it off (tests/settings.py)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.test import Client

from catalog.models import Vendor
from core.models import AuditEvent, Organization
from tests.test_drf_resources import ETAG, conditional, requests_for, send
from tests.test_tokens import Bearer, expiry, mint  # noqa: F401  (mint is a fixture)

REQUIRED = "/problems/precondition-required"
API = Path(__file__).resolve().parents[1]


@pytest.fixture
def required(settings):
    settings.SOFTWARDEN_REQUIRE_IF_MATCH = True


def session(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def vendor(a):
    return Vendor.objects.create(organization=a["org"], name="Contoso", website="https://old.example")


def installation_value(value: str | None) -> str:
    """SOFTWARDEN_REQUIRE_IF_MATCH as an installation's settings read it (softwarden.settings, not tests.settings, which
    turns it off for the rest of the suite), with the variable unset or set to `value`."""
    environ = {
        k: v for k, v in os.environ.items() if k not in ("SOFTWARDEN_REQUIRE_IF_MATCH", "DJANGO_SETTINGS_MODULE")
    }
    environ["SOFTWARDEN_DEBUG"] = "1"
    if value is not None:
        environ["SOFTWARDEN_REQUIRE_IF_MATCH"] = value
    code = "import softwarden.settings as s; print(s.SOFTWARDEN_REQUIRE_IF_MATCH)"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=API, env=environ, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_the_installation_default_is_on_and_0_turns_it_off():
    assert installation_value(None) == "True"
    assert installation_value("") == "True"
    assert installation_value("0") == "False"
    assert installation_value("1") == "True"


def test_off_a_write_without_if_match_is_unconditional(a, vendor, settings):
    settings.SOFTWARDEN_REQUIRE_IF_MATCH = False
    client = session(a["users"]["admin"])
    response = send(client, "patch", f"/api/v1/vendors/{vendor.id}", {"website": "https://new.example"})
    assert response.status_code == 200
    assert Vendor.objects.get(id=vendor.id).website == "https://new.example"


@pytest.mark.usefixtures("required")
def test_a_session_patch_without_if_match_is_428_and_changes_nothing(a, vendor):
    client = session(a["users"]["admin"])
    path = f"/api/v1/vendors/{vendor.id}"
    response = send(client, "patch", path, {"website": "https://new.example"})
    assert response.status_code == 428, response.content
    body = response.json()
    assert body["type"] == REQUIRED and body["title"] == "Precondition required" and "If-Match" in body["detail"]
    assert Vendor.objects.get(id=vendor.id).website == "https://old.example"
    deleted = send(client, "delete", path)
    assert deleted.status_code == 428 and Vendor.objects.filter(id=vendor.id).exists()
    # With the ETag it applies; a stale one is still 412.
    tag = client.get(path)["ETag"]
    done = send(client, "patch", path, {"website": "https://new.example"}, HTTP_IF_MATCH=tag)
    assert done.status_code == 200 and Vendor.objects.get(id=vendor.id).website == "https://new.example"
    assert send(client, "patch", path, {"website": "https://x.example"}, HTTP_IF_MATCH=tag).status_code == 412


@pytest.mark.usefixtures("required")
def test_an_api_token_write_without_if_match_stays_unconditional(a, vendor, mint):  # noqa: F811
    bearer = Bearer(mint("admin", read_only=False)["secret"])
    response = bearer.patch(f"vendors/{vendor.id}", {"website": "https://script.example"})
    assert response.status_code == 200, response.content
    assert Vendor.objects.get(id=vendor.id).website == "https://script.example"


@pytest.mark.usefixtures("required")
def test_routes_with_their_own_revision_require_it_too(a):
    """Organization settings compare their own revision (require_match), not a viewset's."""
    client = session(a["users"]["admin"])
    response = send(client, "patch", "/api/v1/organization", {"name": "Renamed"})
    assert response.status_code == 428 and response.json()["type"] == REQUIRED
    assert Organization.objects.get(id=a["org"].id).name == "orga"
    tag = client.get("/api/v1/organization")["ETag"]
    assert send(client, "patch", "/api/v1/organization", {"name": "Renamed"}, HTTP_IF_MATCH=tag).status_code == 200


@pytest.mark.usefixtures("preview_on", "required")
def test_every_conditional_write_without_if_match_is_428_for_a_session(a):
    client = session(a["users"]["admin"])
    requests = [r for r in requests_for(a, a["users"]["admin"]) if conditional(r[2], r[3])]
    assert len(requests) >= 30
    before = AuditEvent.objects.filter(organization=a["org"]).count()
    for method, path, action, cls in requests:
        response = send(client, method, path, {"name": "Changed"})
        assert response.status_code == 428, (cls.__name__, action, method, path, response.status_code)
        assert response.json()["type"] == REQUIRED, path
        assert "ETag" not in response or ETAG.match(response["ETag"])
    assert AuditEvent.objects.filter(organization=a["org"]).count() == before  # nothing was written


def test_the_schema_documents_428_wherever_it_documents_412(api, a):
    schema = json.loads(api(a["users"]["admin"]).client.get("/api/v1/schema?format=json").content)
    with_412 = [
        (path, method)
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if isinstance(operation, dict) and "412" in operation.get("responses", {})
    ]
    assert len(with_412) >= 30
    for path, method in with_412:
        assert "428" in schema["paths"][path][method]["responses"], (path, method)


# ---- composite routes with their own revision (review follow-up): end-of-life tracking and ticket links ----------


def eol_routes(a):
    """A tracked application and a tracked instance of it: [(path, body for a PATCH)]."""
    from catalog import services as catalog_services
    from tests.test_eol import app, product

    product("postgresql", "PostgreSQL", **{"16": {}, "13": {}})
    record = app(a, version="16.1")
    instance = catalog_services.create_instance(a["org"], a["users"]["editor"], record, {"name": "Production"})
    editor = session(a["users"]["editor"])
    record_url = f"/api/v1/applications/{record.id}/end-of-life"
    instance_url = f"/api/v1/applications/{record.id}/instances/{instance.id}/end-of-life"
    # Starting the tracking is a create: no If-Match needed.
    assert send(editor, "patch", record_url, {"product": "postgresql"}).status_code == 200
    assert send(editor, "patch", instance_url, {"version": "13.9"}).status_code == 200
    # The instance first: deleting the record's tracking removes its instances' too.
    return [(instance_url, {"version": "13.2"}), (record_url, {"cycle": "13"})]


def ticket_link_route(a):
    from tests.test_tickets import link_on, make_assessment
    from tickets import services as ticket_services

    assessment = make_assessment(a)
    target = ticket_services.assessment_target(a["org"], assessment.id)
    link = link_on(a, target, "https://contoso.atlassian.net/browse/ITOPS-1")
    return f"/api/v1/assessments/{assessment.id}/ticket-links/{link.id}", {"title": "Work"}


@pytest.mark.usefixtures("required")
def test_end_of_life_and_ticket_link_writes_are_conditional(a):
    editor = session(a["users"]["editor"])
    for path, body in [*eol_routes(a), ticket_link_route(a)]:
        read = editor.get(path)
        assert read.status_code == 200 and ETAG.match(read["ETag"]), path
        tag = read["ETag"]
        before = AuditEvent.objects.filter(organization=a["org"]).count()
        for method in ("patch", "delete"):
            refused = send(editor, method, path, body)
            assert refused.status_code == 428 and refused.json()["type"] == REQUIRED, (method, path)
            stale = send(editor, method, path, body, HTTP_IF_MATCH='"0000"')
            assert stale.status_code == 412, (method, path, stale.content)
        assert AuditEvent.objects.filter(organization=a["org"]).count() == before  # nothing was written
        done = send(editor, "patch", path, body, HTTP_IF_MATCH=tag)
        assert done.status_code == 200 and done["ETag"] != tag, (path, done.content)
        assert send(editor, "patch", path, body, HTTP_IF_MATCH=tag).status_code == 412  # the old ETag is stale now
        assert send(editor, "delete", path, HTTP_IF_MATCH=done["ETag"]).status_code == 204, path


@pytest.mark.usefixtures("required")
def test_an_if_match_on_tracking_that_is_gone_is_412(a):
    editor = session(a["users"]["editor"])
    _, (record_url, _) = eol_routes(a)
    tag = editor.get(record_url)["ETag"]
    assert send(editor, "delete", record_url, HTTP_IF_MATCH=tag).status_code == 204
    # Someone else stopped tracking it: a save from the form that read it isn't a silent new tracking.
    assert send(editor, "patch", record_url, {"product": "postgresql"}, HTTP_IF_MATCH=tag).status_code == 412
    assert send(editor, "patch", record_url, {"product": "postgresql"}).status_code == 200  # a create, as before
