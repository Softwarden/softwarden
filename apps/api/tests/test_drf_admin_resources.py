"""The DRF layer's part C: discovery, imports, API tokens, members, organization settings, audit, sign-in
and the System status. The sweeps in tests/test_drf_resources.py cover scoping, read-only tokens and If-Match on every
viewset route; this module covers what is particular to these routes: the audit log is append-only, secrets are
write-only (never a response field or an ETag input), token management is session-only, organization settings
honour If-Match, and the composite responses are described key for key."""

import json

import pytest
from django.test import Client
from django.urls import get_resolver
from django.utils import timezone

from api.models import ApiToken
from catalog import services
from catalog.account_serializers import (
    InvitationCreatedSerializer,
    InvitationPreviewSerializer,
    MemberWithPasswordSerializer,
    MeSerializer,
    OrganizationSerializer,
)
from catalog.audit_viewsets import AuditLogPageSerializer, AuditViewSet, RecordAuditPageSerializer
from core import connections, credentials
from core.models import AuditEvent, Job, MicrosoftConnection, Organization
from core.oidc_views import AuthOptionsSerializer
from core.setup_views import SetupStateSerializer
from core.system_views import SystemStatusSerializer
from discovery import engine as discovery
from discovery.models import Observation
from discovery.serializers import CandidatePageSerializer, DiscoverySourceSerializer
from imports import importer
from imports.serializers import ImportRowPageSerializer, ImportRunDetailSerializer
from notify.models import DigestRun
from tests.conftest import digest_row
from tests.test_drf_resources import STALE, described, send
from tests.test_tokens import Bearer, expiry, mint  # noqa: F401  (mint is a fixture)
from tests.test_url_coverage import walk

NEW = {"tenant_id": "simulated", "client_id": "", "secret": "synthetic-secret"}
TEMPLATE_CSV = b"name,lifecycle,criticality\nPayroll,active,high\nBroken,nope,\n"
OTHER_CSV = b"Application,State\nPayroll,active\n"


@pytest.fixture
def connector(a):
    made = discovery.create_connector(a["org"], a["users"]["admin"], dict(NEW))
    connections.run_test(a["org"], a["users"]["admin"], made.microsoft_connection)  # a run needs a tested connection
    discovery.run(made)
    return made


def admin_client(f):
    client = Client()
    client.force_login(f["users"]["admin"])
    return client


# ---- the audit log is append-only ---------------------------------------------------------------------------------


def test_no_route_changes_or_removes_an_audit_event(a):
    routes = {}
    for route, callback in walk(get_resolver().url_patterns):
        cls = getattr(callback, "cls", None)
        if cls is not None and issubclass(cls, AuditViewSet):
            routes[route] = {m for m in callback.actions if m not in ("head", "options")}
    assert set(routes) >= {
        "api/v1/audit-events",
        "api/v1/audit-events/<uuid:pk>",
        "api/v1/applications/<uuid:pk>/audit",
        "api/v1/components/<uuid:pk>/audit",
        "api/v1/integrations/<uuid:pk>/audit",
        "api/v1/renewals/<uuid:pk>/audit",
    }
    assert all(methods == {"get"} for methods in routes.values()), routes
    event = AuditEvent.objects.create(organization=a["org"], actor=a["users"]["admin"], action="export", changes={})
    client = admin_client(a)
    for method in ("put", "patch", "delete", "post"):
        response = send(client, method, f"/api/v1/audit-events/{event.id}", {"action": "forged"})
        assert response.status_code == 405, method
    event.refresh_from_db()
    assert event.action == "export"
    assert client.get(f"/api/v1/audit-events/{event.id}").json()["label"] == "Exported"


def test_the_audit_log_is_admin_only_and_history_is_any_role(a, api):
    assert api(a["users"]["editor"]).get("audit-events").status_code == 403
    assert api(a["users"]["viewer"]).get(f"applications/{a['app'].id}/audit").status_code == 200


# ---- Secrets are write-only ---------------------------------------------------------------------------------------


def test_a_connectors_etag_ignores_its_ciphertext_but_moves_when_the_secret_is_replaced(a, connector):
    client = admin_client(a)
    url = f"/api/v1/connectors/{connector.id}"
    first = client.get(url)
    tag = first["ETag"]
    stored = MicrosoftConnection.objects.get(id=connector.microsoft_connection_id)
    assert stored.secret_ciphertext not in first.content.decode() and "synthetic-secret" not in first.content.decode()
    # Re-encrypting under another key (what rotate_credential_keys does) changes the slot but not the secret.
    MicrosoftConnection.objects.filter(id=stored.id).update(secret_ciphertext="re-encrypted", secret_key_id="k2")
    assert client.get(url)["ETag"] == tag
    replaced = send(client, "patch", url, {"secret": "replaced-secret"}, HTTP_IF_MATCH=tag)
    assert replaced.status_code == 200 and replaced["ETag"] != tag  # the connection's secret_set_at moved
    assert "replaced-secret" not in replaced.content.decode()
    assert credentials.slot_value(MicrosoftConnection.objects.get(id=stored.id), "secret") == "replaced-secret"


def test_a_tokens_hash_is_in_no_response_or_etag_and_tokens_need_a_session(a, mint):  # noqa: F811
    created = mint("editor", read_only=False)
    token = ApiToken.objects.get(id=created["id"])
    client = admin_client(a)
    url = f"/api/v1/api-tokens/{token.id}"
    first = client.get(url)
    body = first.json()
    assert "secret" not in body and "token_hash" not in body and token.token_hash not in first.content.decode()
    assert {k: v for k, v in created.items() if k != "secret"} == body
    ApiToken.objects.filter(id=token.id).update(token_hash="0" * 64)
    assert client.get(url)["ETag"] == first["ETag"]
    # A token manages no tokens, not even its own: refused for reads and writes.
    bearer = Bearer(mint("admin", read_only=False)["secret"])
    for response in (bearer.get(f"api-tokens/{token.id}"), bearer.post(f"api-tokens/{token.id}/revoke", {})):
        assert response.status_code == 403
        assert response.json()["detail"] == "Manage API tokens from a signed-in session, not with a token."
    assert ApiToken.objects.get(id=token.id).revoked_at is None


def test_the_licence_key_is_not_an_etag_input(a):
    client = admin_client(a)
    tag = client.get("/api/v1/organization")["ETag"]
    Organization.objects.filter(id=a["org"].id).update(licence_key_ciphertext="sealed", licence_key_key_id="k1")
    response = client.get("/api/v1/organization")
    assert response["ETag"] == tag and "sealed" not in response.content.decode()


# ---- organization settings honour If-Match ------------------------------------------------------------------------


def test_organization_settings_honour_if_match(a):
    client = admin_client(a)
    tag = client.get("/api/v1/organization")["ETag"]
    stale = send(client, "patch", "/api/v1/organization", {"name": "Renamed"}, HTTP_IF_MATCH=STALE)
    assert stale.status_code == 412 and stale.json()["type"] == "/problems/precondition-failed"
    assert stale["ETag"] == tag and Organization.objects.get(id=a["org"].id).name == "orga"
    done = send(client, "patch", "/api/v1/organization", {"name": "Renamed"}, HTTP_IF_MATCH=tag)
    assert done.status_code == 200 and done.json()["name"] == "Renamed" and done["ETag"] != tag
    assert send(client, "patch", "/api/v1/organization", {"name": "Again"}, HTTP_IF_MATCH=tag).status_code == 412
    viewer = Client()
    viewer.force_login(a["users"]["viewer"])
    refused = send(viewer, "patch", "/api/v1/organization", {"name": "Viewer"}, HTTP_IF_MATCH=STALE)
    assert refused.status_code == 403  # the role refusal comes before the revision check


def test_a_member_patch_honours_if_match(a):
    client = admin_client(a)
    url = f"/api/v1/users/{a['users']['viewer'].id}"
    tag = client.get(url)["ETag"]
    assert send(client, "patch", url, {"role": "editor"}, HTTP_IF_MATCH=STALE).status_code == 412
    done = send(client, "patch", url, {"role": "editor"}, HTTP_IF_MATCH=tag)
    assert done.status_code == 200 and done.json()["role"] == "editor" and done["ETag"] != tag
    assert send(client, "patch", url, {"role": "viewer"}, HTTP_IF_MATCH=tag).status_code == 412


# ---- parameters ---------------------------------------------------------------------------------------------------


def test_unknown_parameters_are_named_and_the_spas_own_are_accepted(a, b, api, connector):
    client = api(a["users"]["admin"])
    run = importer.create_run(a["org"], a["users"]["editor"], "application", "x.csv", TEMPLATE_CSV)
    for url in (
        "audit-events",
        f"applications/{a['app'].id}/audit",
        "discovery/candidates",
        f"imports/{run.id}/rows",
        "users",
        "connectors",
        "import-sources",
        "api-tokens",
    ):
        response = client.get(f"{url}?colour=red")
        assert response.status_code == 400 and response.json_body["errors"] == {
            "colour": ["This filter isn't available here."]
        }, url
    assert client.get(f"imports/{run.id}/rows?outcome=&q=&offset=0&limit=100&rev=3").status_code == 200
    assert client.get("discovery/candidates?status=candidates&connector=&q=").status_code == 200
    assert client.get("audit-events?actor=&action=&from=&to=").status_code == 200
    errors = client.get("audit-events?actor=me&from=yesterday").json_body["errors"]
    assert errors == {"actor": ["Choose a user."], "from": ["Enter a date as YYYY-MM-DD."]}
    assert client.get("audit-events?cursor=nope").json_body["errors"] == {
        "cursor": ["This page link has expired. Start from the first page."]
    }
    theirs = discovery.create_connector(b["org"], b["users"]["admin"], dict(NEW))
    assert client.get(f"discovery/candidates?status=all&connector={theirs.id}").status_code == 404
    assert client.get("discovery/candidates?connector=nope").status_code == 400
    assert client.get("discovery/candidates?status=").status_code == 400  # as before: a status must be named


# ---- composite responses are described key for key ----------------------------------------------------------------


@pytest.fixture
def busy(a, connector, mint):  # noqa: F811
    org, editor = a["org"], a["users"]["editor"]
    observations = list(Observation.objects.filter(connector=connector).order_by("display_name"))
    discovery.link(org, editor, observations[0], {"resource": str(a["app"].id)})
    discovery.dismiss(org, editor, observations[1], {"reason": "Test registration"})
    job = services.create_resource(org, editor, {"name": "Job", "kind": "component", "lifecycle": "active"})
    services.create_relationship(org, editor, {"type": "depends_on", "source": str(a["app"].id), "target": str(job.id)})
    run = importer.create_run(org, editor, "application", "x.csv", TEMPLATE_CSV)
    unmapped = importer.create_run(org, editor, "application", "y.csv", OTHER_CSV)
    digests = DigestRun.objects.create(organization=org, for_date=services.org_today(org), finished_at=timezone.now())
    digest_row(org, editor, "sent", run=digests)
    Job.objects.create(organization=org, kind="x.y", status="failed", attempts=3, last_error="Boom")
    return {"run": run, "unmapped": unmapped}


def test_part_c_composite_responses_match_their_serializers(a, api, busy, mint):  # noqa: F811
    client = api(a["users"]["admin"])
    run, unmapped, app = busy["run"].id, busy["unmapped"].id, a["app"].id
    cases = [
        (CandidatePageSerializer(), "discovery/candidates?status=all"),
        (DiscoverySourceSerializer(many=True), f"applications/{app}/discovery-sources"),
        (AuditLogPageSerializer(), "audit-events"),
        (RecordAuditPageSerializer(), f"applications/{app}/audit"),
        (ImportRowPageSerializer(), f"imports/{run}/rows"),
        (ImportRunDetailSerializer(), f"imports/{run}"),
        (ImportRunDetailSerializer(), f"imports/{unmapped}"),
        (MeSerializer(), "auth/me"),
        (OrganizationSerializer(), "organization"),
        (SystemStatusSerializer(), "system"),
        (AuthOptionsSerializer(), "auth/options"),
        (SetupStateSerializer(), "setup"),
    ]
    for serializer, url in cases:
        response = client.get(url)
        assert response.status_code == 200, (url, response.json_body)
        assert described(serializer, response.json_body) == [], url
    page = client.get("discovery/candidates?status=all").json_body
    assert page["results"] and {r["status"] for r in page["results"]} >= {"linked", "dismissed", "new"}
    assert client.get(f"imports/{unmapped}").json_body["needs_mapping"] is True
    assert client.get("organization").json_body["email"]["last_run"]["sent"] == 1
    invited = client.post("users", {"email": "new@orga.test", "name": "New Person", "role": "viewer"})
    assert invited.status_code == 201 and "/invite#" in invited.json_body["link"]
    assert described(InvitationCreatedSerializer(), invited.json_body) == []
    resent = client.post(f"invitations/{invited.json_body['id']}/resend")
    assert resent.status_code == 200 and described(InvitationCreatedSerializer(), resent.json_body) == []
    preview = client.post("auth/invitations/preview", {"token": resent.json_body["link"].rsplit("#", 1)[1]})
    assert preview.status_code == 200 and described(InvitationPreviewSerializer(), preview.json_body) == []
    reset = client.post(f"users/{a['users']['viewer'].id}/reset-password")
    assert reset.status_code == 200 and described(MemberWithPasswordSerializer(), reset.json_body) == []
    created = client.post(
        "api-tokens",
        {"user": a["users"]["viewer"].id, "name": "Read", "read_only": True, "expires_on": expiry(a["org"])},
    )
    assert created.status_code == 201 and set(created.json_body) == {
        "id", "name", "user", "prefix", "read_only", "expires_on", "last_used_at", "revoked_at", "created_at",
        "created_by", "secret",
    }  # fmt: skip
    assert json.dumps(created.json_body).count(created.json_body["secret"]) == 1


def test_every_discovery_source_config_field_is_a_connector_field():
    """ConnectorSerializer names tenant_id and client_id; a source with other non-secret config fields must
    add them there, or they would silently vanish from the response."""
    from discovery.serializers import ConnectorInputSerializer, ConnectorSerializer

    for source, module in discovery.SOURCES.items():
        assert set(module.CONFIG_FIELDS) <= set(ConnectorSerializer().fields), source
        assert set(module.CONFIG_FIELDS) <= set(ConnectorInputSerializer().fields), source


def test_tokens_are_refused_only_on_methods_the_route_has(a, mint):  # noqa: F811
    bearer = Bearer(mint("admin", read_only=False)["secret"])
    assert bearer.client.delete("/api/v1/api-tokens").status_code == 405  # no DELETE here: 405 before the refusal
    assert bearer.get("api-tokens").status_code == 403
