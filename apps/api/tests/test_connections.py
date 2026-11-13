"""Connections: MicrosoftConnection and HaloConnection, their revision and test, the Halo capability, the
role matrix, cross-tenant isolation, run refusal for an untested connection, and the move of connector credentials
(discovery 0005, 0006). Nothing here reaches the network: the simulated tenant and Halo, or scripted transports."""

import json
from typing import Any

import pytest
from django.db import connection as db
from django.db.models import F
from django.test.utils import CaptureQueriesContext

from core import connections, credentials, graph, halo, outbound
from core.capabilities import set_edition
from core.models import AuditEvent, HaloConnection, MicrosoftConnection
from core.problems import Conflict, FieldErrors
from discovery import engine as discovery
from discovery.models import Connector, ConnectorRun, RunStatus
from tests.conftest import drain_jobs
from tests.test_tenancy import _rejected

SECRET = "synthetic-connection-secret-7c1"
TENANT = "8c3dcbf0-7a3b-4f43-9a3e-1b2c3d4e5f60"
CLIENT = "1d2c3b4a-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
HALO_URL = halo.SIMULATED_URL


def ms(org, actor, **extra):
    data = {"name": "Contoso Entra", "tenant_id": "simulated", "secret": SECRET, **extra}
    return connections.create_connection(org, actor, connections.MICROSOFT, data)


def hal(org, actor, **extra):
    data = {"name": "Halo", "base_url": HALO_URL, "client_id": "softwarden", "secret": SECRET, **extra}
    return connections.create_connection(org, actor, connections.HALO, data)


def connector_on(org, actor, conn, name="Entra"):
    return discovery.create_connector(org, actor, {"name": name, "microsoft_connection": str(conn.id)})


@pytest.fixture
def enterprise(a):
    a["org"], _ = set_edition(a["org"], "enterprise")
    return a


class Scripted:
    """A token endpoint that answers with one scripted reply (or raises), recording the calls."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def send(self, method, url, headers, body=None):
        self.calls.append((method, url))
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


def token_error(status, error, code):
    body = {"error": error, "error_description": f"{code}: Something Microsoft says. Trace ID: x", "error_codes": []}
    return graph.Reply(status, {}, json.dumps(body).encode())


# ---- the model: revision and test state ---------------------------------------------------------------------------


def test_revision_moves_on_sign_in_values_and_the_secret_only(a):
    org, admin = a["org"], a["users"]["admin"]
    conn = ms(org, admin)
    assert conn.revision == 1 and conn.tested_revision is None and not conn.test_current
    for change in ({"name": "Renamed"}, {"notes": "Owned by IT"}, {"enabled": False}, {"enabled": True}):
        conn = connections.update_connection(org, admin, conn, change)
    assert conn.revision == 1
    conn = connections.update_connection(org, admin, conn, {"tenant_id": TENANT, "client_id": CLIENT})
    assert conn.revision == 2
    conn = connections.update_connection(org, admin, conn, {"secret": "another-secret"})
    assert conn.revision == 3
    # Unchanged values are no change at all: no revision, no event.
    events = AuditEvent.objects.filter(action="microsoft_connection.updated").count()
    conn = connections.update_connection(org, admin, conn, {"tenant_id": TENANT})
    assert conn.revision == 3 and AuditEvent.objects.filter(action="microsoft_connection.updated").count() == events


def test_revision_moves_on_any_save_path_but_not_on_key_rotation(a, monkeypatch):
    conn = ms(a["org"], a["users"]["admin"])
    conn.client_id = CLIENT
    conn.save()
    assert MicrosoftConnection.objects.get(pk=conn.pk).revision == 2
    new = credentials.generate_key()
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", f"{new},{credentials.configured_key_values()[0]}")
    credentials.rotate_all()
    stored = MicrosoftConnection.objects.get(pk=conn.pk)
    assert stored.secret_key_id == credentials.key_id(new) and stored.revision == 2


def test_a_successful_test_records_the_revision_and_a_change_needs_another(a):
    org, admin = a["org"], a["users"]["admin"]
    conn = connections.run_test(org, admin, ms(org, admin))
    assert conn.test_ok and conn.tested_revision == conn.revision == 1 and conn.test_current
    assert conn.tested_by == admin and conn.test_result["problem"] == "" and "token" in conn.test_result["detail"]
    conn = connections.update_connection(org, admin, conn, {"secret": "replaced"})
    assert conn.revision == 2 and conn.tested_revision == 1 and not conn.test_current
    conn = connections.run_test(org, admin, conn)
    assert conn.test_current and conn.tested_revision == 2


def test_a_failed_test_clears_the_tested_revision(a):
    org, admin = a["org"], a["users"]["admin"]
    conn = connections.run_test(org, admin, ms(org, admin))
    conn = connections.update_connection(org, admin, conn, {"secret": "wrong-secret"})  # the simulator refuses it
    conn = connections.run_test(org, admin, conn)
    assert conn.test_ok is False and conn.tested_revision is None and not conn.test_current
    assert conn.test_result["code"] == "AADSTS7000215" and conn.test_result["problem"] == "wrong_credentials"


def test_a_connection_changed_during_its_test_is_not_marked_tested(a, monkeypatch):
    org, admin = a["org"], a["users"]["admin"]
    conn = ms(org, admin)
    real = connections.microsoft_transport

    def racing(tenant):
        MicrosoftConnection.objects.filter(pk=conn.pk).update(revision=F("revision") + 1)  # someone saved meanwhile
        return real(tenant)

    monkeypatch.setattr(connections, "microsoft_transport", racing)
    tested = connections.run_test(org, admin, conn)
    assert tested.probe.problem == "changed" and tested.tested_at is None and tested.test_result == {}
    event = AuditEvent.objects.filter(action="microsoft_connection.tested").latest("created_at")
    assert event.changes["problem"] == "changed" and event.changes["ok"] is False


def test_a_stale_test_never_overwrites_a_newer_success(a, monkeypatch, api):
    """A slow test of revision 1 finishes after an edit (revision 2) that was itself tested successfully: the newer
    success stays recorded; the slow call's response says `changed`."""
    org, admin = a["org"], a["users"]["admin"]
    conn = ms(org, admin)
    real = connections.microsoft_transport

    def slow(tenant):
        MicrosoftConnection.objects.filter(pk=conn.pk).update(revision=2, tested_revision=2, test_ok=True)
        return real(tenant)

    monkeypatch.setattr(connections, "microsoft_transport", slow)
    response = api(admin).post(f"connections/microsoft/{conn.id}/test")
    assert response.status_code == 200 and response.json_body["result"]["problem"] == "changed"
    assert response.json_body["test_current"] is True and response.json_body["tested_revision"] == 2
    stored = MicrosoftConnection.objects.get(pk=conn.pk)
    assert stored.tested_revision == 2 and stored.test_ok is True


def test_a_connection_deleted_during_its_test_is_a_404(a, monkeypatch, api):
    org, admin = a["org"], a["users"]["admin"]
    conn = ms(org, admin)
    real = connections.microsoft_transport

    def deleting(tenant):
        MicrosoftConnection.objects.filter(pk=conn.pk).delete()
        return real(tenant)

    monkeypatch.setattr(connections, "microsoft_transport", deleting)
    response = api(admin).post(f"connections/microsoft/{conn.id}/test")
    assert response.status_code == 404 and response["Content-Type"] == "application/problem+json"


@pytest.mark.parametrize(
    "error,code",
    [
        ("invalid_client", "invalid_client"),
        ("<script>alert(1)</script>", ""),
        ("x" * 61, ""),
        ("has space", ""),
    ],
)
def test_stored_codes_are_plain_and_short(enterprise, monkeypatch, error, code):
    a = enterprise
    reply = graph.Reply(400, {}, json.dumps({"error": error, "error_description": "no code here"}).encode())
    conn = ms(a["org"], a["users"]["admin"], tenant_id=TENANT, client_id=CLIENT)
    monkeypatch.setattr(connections, "microsoft_transport", lambda tenant: Scripted(reply))
    assert connections.run_test(a["org"], a["users"]["admin"], conn).test_result["code"] == code
    h = hal(a["org"], a["users"]["admin"], base_url="https://contoso.haloitsm.com")

    class Refusing:
        def post_form(self, url, form):
            return reply

    monkeypatch.setattr(connections, "halo_transport", lambda base_url: Refusing())
    assert connections.run_test(a["org"], a["users"]["admin"], h).test_result["code"] == code


# ---- validation ---------------------------------------------------------------------------------------------------


def test_microsoft_validation(a):
    org, admin = a["org"], a["users"]["admin"]
    with pytest.raises(FieldErrors) as e:
        connections.create_connection(org, admin, connections.MICROSOFT, {})
    assert set(e.value.errors) == {"name", "tenant_id", "client_id", "secret"}
    with pytest.raises(FieldErrors) as e:
        ms(org, admin, tenant_id="evil.example/../x", client_id="nope", secret="  ", client_secret="x")
    # An unknown field (a typo for the secret, say) is refused, not silently ignored.
    assert set(e.value.errors) == {"tenant_id", "client_id", "secret", "client_secret"}
    with pytest.raises(FieldErrors) as e:
        ms(org, admin, tenant_id=TENANT, client_id="nope")
    assert set(e.value.errors) == {"client_id"}
    ms(org, admin, tenant_id=TENANT, client_id=CLIENT)
    with pytest.raises(FieldErrors) as e:
        ms(org, admin, name="CONTOSO entra")
    assert "name" in e.value.errors
    ms(org, admin, name="Other")  # another name is fine
    assert MicrosoftConnection.objects.filter(organization=org).count() == 2


def test_no_credential_key_means_no_secret_can_be_stored(a, monkeypatch):
    monkeypatch.delenv("SOFTWARDEN_CREDENTIAL_KEYS")
    with pytest.raises(FieldErrors) as e:
        ms(a["org"], a["users"]["admin"])
    assert "isn't configured" in e.value.errors["secret"][0]


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://contoso.haloitsm.com", True),
        ("https://halo.internal.example/halo/", True),  # a path is kept; the trailing slash is dropped
        ("https://10.0.0.5", True),  # on-premises Halo: private networks are allowed
        ("http://contoso.haloitsm.com", False),
        ("https://127.0.0.1", False),
        ("https://169.254.169.254", False),  # cloud metadata
        ("https://[::1]", False),
        ("https://localhost", False),
        ("https://user:pass@contoso.haloitsm.com", False),
        ("https://contoso.haloitsm.com/?tenant=x", False),
        ("ftp://contoso.haloitsm.com", False),
    ],
)
def test_halo_base_url_goes_through_the_address_guard(enterprise, url, ok):
    a = enterprise
    if ok:
        conn = hal(a["org"], a["users"]["admin"], base_url=url)
        assert conn.base_url == url.rstrip("/")
    else:
        with pytest.raises(FieldErrors) as e:
            hal(a["org"], a["users"]["admin"], base_url=url)
        assert set(e.value.errors) == {"base_url"}


def test_halo_scope_and_client_are_checked(enterprise):
    a = enterprise
    with pytest.raises(FieldErrors) as e:
        hal(a["org"], a["users"]["admin"], client_id="", scope="read:assets; drop")
    assert set(e.value.errors) == {"client_id", "scope"}
    assert hal(a["org"], a["users"]["admin"]).scope == halo.DEFAULT_SCOPE


# ---- the Microsoft test: AADSTS classification --------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply,problem,code,advice",
    [
        (token_error(401, "invalid_client", "AADSTS7000215"), "wrong_credentials", "AADSTS7000215", "Secret ID"),
        (
            token_error(401, "invalid_client", "AADSTS7000222"),
            "wrong_credentials",
            "AADSTS7000222",
            "new client secret",
        ),
        (token_error(400, "invalid_request", "AADSTS90002"), "wrong_credentials", "AADSTS90002", "tenant"),
        (token_error(400, "unauthorized_client", "AADSTS700016"), "wrong_credentials", "AADSTS700016", "client) ID"),
        (token_error(400, "invalid_grant", "AADSTS65001"), "missing_permission", "AADSTS65001", "admin consent"),
        (token_error(400, "invalid_client", "AADSTS7000229"), "missing_permission", "AADSTS7000229", "admin consent"),
        (token_error(400, "unauthorized_client", "AADSTS7000112"), "disabled", "AADSTS7000112", "Enterprise"),
        (token_error(401, "invalid_client", "AADSTS1234567"), "wrong_credentials", "AADSTS1234567", "client secret"),
        (token_error(503, "temporarily_unavailable", "AADSTS50000"), "unreachable", "AADSTS50000", "few minutes"),
        (graph.Reply(418, {}, b"<html>teapot</html>"), "error", "", "client secret"),
        (graph.Unreachable("TimeoutError", ambiguous=True), "unreachable", "", "outbound internet access"),
    ],
)
def test_token_errors_are_classified(a, monkeypatch, reply, problem, code, advice):
    org, admin = a["org"], a["users"]["admin"]
    conn = ms(org, admin, tenant_id=TENANT, client_id=CLIENT)
    transport = Scripted(reply)
    monkeypatch.setattr(connections, "microsoft_transport", lambda tenant: transport)
    tested = connections.run_test(org, admin, conn)
    result = tested.test_result
    assert not tested.test_ok and tested.tested_revision is None
    assert (result["problem"], result["code"]) == (problem, code)
    assert advice in result["next"] and result["problem_label"]
    assert "Something Microsoft says" not in json.dumps(result)  # Softwarden's words, never the server's
    assert transport.calls == [("POST", f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token")]


def test_the_microsoft_test_sends_the_client_credentials_request(a, monkeypatch):
    org, admin = a["org"], a["users"]["admin"]
    conn = ms(org, admin, tenant_id=TENANT, client_id=CLIENT)
    seen = []

    class Accepting:
        def send(self, method, url, headers, body=None):
            seen.append(body.decode())
            return graph.Reply(200, {}, json.dumps({"access_token": "t0ken", "expires_in": 3599}).encode())

    monkeypatch.setattr(connections, "microsoft_transport", lambda tenant: Accepting())
    assert connections.run_test(org, admin, conn).test_current
    form = seen[0]
    assert "grant_type=client_credentials" in form and f"client_id={CLIENT}" in form
    assert "scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" in form
    assert "t0ken" not in json.dumps(MicrosoftConnection.objects.get(pk=conn.pk).test_result)


def test_a_connection_without_a_readable_secret_fails_without_calling_out(a, monkeypatch):
    org, admin = a["org"], a["users"]["admin"]
    conn = ms(org, admin)
    MicrosoftConnection.objects.filter(pk=conn.pk).update(secret_ciphertext="not-a-fernet-token")
    monkeypatch.setattr(connections, "microsoft_transport", lambda tenant: pytest.fail("no call expected"))
    tested = connections.run_test(org, admin, conn)
    assert tested.test_result["problem"] == "not_configured" and not tested.test_current


# ---- Halo: the capability, the test --------------------------------------------------------------------------------


def test_halo_needs_its_capability_in_services_and_api(a, api):
    """Community has discovery.halo; without it (a platform revoke) the services and the API refuse, naming it."""
    from core.capabilities import forget_organization
    from core.models import CapabilityOverride

    CapabilityOverride.objects.create(organization=a["org"], capability="discovery.halo", effect="revoke", reason="t")
    forget_organization(a["org"].pk)
    with pytest.raises(Exception) as e:
        hal(a["org"], a["users"]["admin"])
    assert getattr(e.value, "extra", {}).get("capability") == "discovery.halo"
    admin = api(a["users"]["admin"])
    for method, url, body in (
        ("get", "connections/halo", None),
        ("post", "connections/halo", {"name": "Halo"}),
    ):
        response = getattr(admin, method)(url, body) if body else getattr(admin, method)(url)
        assert response.status_code == 403
        assert response.json_body["type"] == "/problems/capability-required"
        assert response.json_body["capability"] == "discovery.halo"
    # A Viewer is refused for role first, without the capability being named.
    viewer = api(a["users"]["viewer"]).get("connections/halo")
    assert viewer.status_code == 403 and "capability" not in viewer.json_body


@pytest.mark.parametrize(
    "secret,scope,ok,problem,code",
    [
        (SECRET, halo.DEFAULT_SCOPE, True, "", ""),
        ("wrong-secret", halo.DEFAULT_SCOPE, False, "wrong_credentials", "invalid_client"),
        (SECRET, "read:bad", False, "missing_permission", "invalid_scope"),
    ],
)
def test_the_halo_test_against_the_simulator(enterprise, secret, scope, ok, problem, code):
    a = enterprise
    conn = hal(a["org"], a["users"]["admin"], secret=secret, scope=scope)
    tested = connections.run_test(a["org"], a["users"]["admin"], conn)
    assert tested.test_ok is ok and tested.test_current is ok
    assert (tested.test_result["problem"], tested.test_result["code"]) == (problem, code)


def test_the_halo_test_refuses_a_name_that_resolves_to_loopback(enterprise, monkeypatch):
    """The guard runs again when connecting: a name that resolves to loopback or metadata is never contacted."""
    a = enterprise
    conn = hal(a["org"], a["users"]["admin"], base_url="https://halo.example.com")
    monkeypatch.setattr(outbound, "_resolve", lambda host, port: ["169.254.169.254"])
    monkeypatch.setattr(outbound._PinnedHTTPSConnection, "connect", lambda self: pytest.fail("must not connect"))
    tested = connections.run_test(a["org"], a["users"]["admin"], conn)
    assert tested.test_result["problem"] == "refused" and not tested.test_current


def test_the_halo_http_transport_posts_the_token_form_through_the_guard(monkeypatch):
    sent: dict[str, Any] = {}

    def fake_post(url, body, headers, timeout=None, deadline=None, *, private_ok=False):
        sent.update(url=url, body=body.decode(), private_ok=private_ok)
        return graph.Reply(401, {}, json.dumps({"error": "invalid_client"}).encode())

    monkeypatch.setattr(outbound, "post", fake_post)
    with pytest.raises(halo.HaloError) as e:
        halo.request_token(halo.HttpTransport(), "https://contoso.haloitsm.com", "client", SECRET, "all")
    assert sent["url"] == "https://contoso.haloitsm.com/auth/token" and sent["private_ok"] is True
    assert "grant_type=client_credentials" in sent["body"] and "scope=all" in sent["body"]
    assert (e.value.status, e.value.code) == (401, "invalid_client") and SECRET not in str(e.value)


# ---- runs refuse an untested or changed connection -----------------------------------------------------------------


def test_a_connector_runs_only_on_a_connection_tested_at_its_revision(a, api):
    org, admin = a["org"], a["users"]["admin"]
    conn = ms(org, admin)
    connector = connector_on(org, admin, conn)

    run = discovery.run(connector)  # the engine refuses, whatever started the run
    assert run.status == RunStatus.FAILED and "hasn't been tested" in run.error
    response = api(admin).post(f"connectors/{connector.id}/run")
    assert response.status_code == 409 and "hasn't been tested" in response.json_body["detail"]

    connections.run_test(org, admin, conn)
    assert discovery.run(connector).status == RunStatus.SUCCEEDED

    connections.update_connection(org, admin, conn, {"secret": "rotated-at-microsoft"})
    run = discovery.run(connector)
    assert run.status == RunStatus.FAILED and "changed after its last successful test" in run.error
    assert "revision 2" in run.error and "rotated-at-microsoft" not in run.error
    with pytest.raises(Conflict):
        discovery.request_run(org, admin, Connector.objects.get(pk=connector.pk))

    connections.update_connection(org, admin, conn, {"secret": "wrong-secret"})
    connections.run_test(org, admin, conn)
    run = discovery.run(connector)
    assert run.status == RunStatus.FAILED and "failed its last test" in run.error

    connections.update_connection(org, admin, conn, {"secret": SECRET, "enabled": False})
    connections.run_test(org, admin, conn)  # a disabled connection can be tested ...
    run = discovery.run(connector)  # ... but not used
    assert run.status == RunStatus.FAILED and "turned off" in run.error
    assert ConnectorRun.objects.filter(connector=connector, status=RunStatus.SUCCEEDED).count() == 1


def test_scheduled_runs_are_refused_too(a):
    org, admin = a["org"], a["users"]["admin"]
    connector = connector_on(org, admin, ms(org, admin))
    Connector.objects.filter(pk=connector.pk).update(run_requested_at="2026-11-06T10:00:00Z")  # before Runs
    assert discovery.due_runs() == 1
    drain_jobs()
    run = ConnectorRun.objects.get(connector=connector)
    assert run.status == RunStatus.FAILED and "hasn't been tested" in run.error


def test_legacy_connector_credentials_create_the_connectors_own_connection(a):
    org, admin = a["org"], a["users"]["admin"]
    connector = discovery.create_connector(org, admin, {"name": "Entra", "tenant_id": "simulated", "secret": SECRET})
    conn = connector.microsoft_connection
    assert conn.name == "Entra" and conn.tenant_id == "simulated" and credentials.slot_value(conn, "secret") == SECRET
    assert AuditEvent.objects.filter(action="microsoft_connection.created", target_id=conn.id).exists()
    discovery.update_connector(org, admin, connector, {"secret": "replaced"})
    conn.refresh_from_db()
    assert conn.revision == 2
    with pytest.raises(FieldErrors) as e:
        discovery.create_connector(
            org, admin, {"name": "Both", "microsoft_connection": str(conn.id), "tenant_id": "simulated"}
        )
    assert "microsoft_connection" in e.value.errors
    with pytest.raises(FieldErrors) as e:
        discovery.create_connector(org, admin, {"name": "Neither"})
    assert "microsoft_connection" in e.value.errors
    # A connection other connectors share is never changed through one of them.
    shared = connector_on(org, admin, conn, "Entra shared")
    for data in ({"secret": "sneaky"}, {"tenant_id": TENANT, "client_id": CLIENT}):
        with pytest.raises(FieldErrors) as e:
            discovery.update_connector(org, admin, connector, data)
        assert set(e.value.errors) == set(data)
        assert "Entra shared" in next(iter(e.value.errors.values()))[0]
    conn.refresh_from_db()
    assert conn.revision == 2 and conn.tenant_id == "simulated"
    discovery.delete_connector(org, admin, shared)
    # A second connector with the same name pattern gets a connection name of its own.
    other = discovery.create_connector(org, admin, {"name": "entra 2", "tenant_id": "simulated", "secret": SECRET})
    assert other.microsoft_connection_id != conn.id


# ---- deletion ------------------------------------------------------------------------------------------------------


def test_a_connection_in_use_cannot_be_deleted(a, api):
    org, admin = a["org"], a["users"]["admin"]
    conn = ms(org, admin)
    first, second = connector_on(org, admin, conn, "Entra A"), connector_on(org, admin, conn, "Entra B")
    response = api(admin).delete(f"connections/microsoft/{conn.id}")
    assert response.status_code == 409
    body = response.json_body
    assert "Entra A" in body["detail"] and "Entra B" in body["detail"]
    assert body["connectors"] == [{"id": str(first.id), "name": "Entra A"}, {"id": str(second.id), "name": "Entra B"}]
    for c in (first, second):
        discovery.delete_connector(org, admin, c)
    assert api(admin).delete(f"connections/microsoft/{conn.id}").status_code == 204
    assert not MicrosoftConnection.objects.filter(pk=conn.pk).exists()
    assert AuditEvent.objects.filter(action="microsoft_connection.deleted").get().changes == {"name": conn.name}


# ---- the API ---------------------------------------------------------------------------------------------------------


def test_crud_with_etags_and_a_write_only_secret(a, api):
    admin = api(a["users"]["admin"])
    created = admin.post(
        "connections/microsoft", {"name": "Contoso", "tenant_id": TENANT, "client_id": CLIENT, "secret": SECRET}
    )
    assert created.status_code == 201, created.json_body
    body = created.json_body
    assert body["revision"] == 1 and body["tested_revision"] is None and body["test_current"] is False
    assert body["last_test"] is None and body["secret_set_on"] and body["connectors"] == []
    assert "secret" not in body and SECRET not in created.content.decode()
    url = f"connections/microsoft/{body['id']}"
    tag = admin.get(url)["ETag"]
    stale = admin.client.patch(
        f"/api/v1/{url}", json.dumps({"name": "X"}), content_type="application/json", HTTP_IF_MATCH='"stale"'
    )
    assert stale.status_code == 412
    changed = admin.client.patch(
        f"/api/v1/{url}", json.dumps({"secret": "new-value"}), content_type="application/json", HTTP_IF_MATCH=tag
    )
    assert changed.status_code == 200 and changed["ETag"] != tag and changed.json()["revision"] == 2
    assert "new-value" not in changed.content.decode()
    listed = admin.get("connections/microsoft").json_body
    assert [c["name"] for c in listed] == ["Contoso"]
    deleted = admin.client.delete(f"/api/v1/{url}", HTTP_IF_MATCH='"stale"')
    assert deleted.status_code == 412
    assert admin.delete(url).status_code == 204


def test_the_test_action_returns_the_connection_with_its_result(a, api):
    admin = api(a["users"]["admin"])
    conn = ms(a["org"], a["users"]["admin"])
    response = admin.post(f"connections/microsoft/{conn.id}/test")
    assert response.status_code == 200
    body = response.json_body
    assert body["test_current"] and body["tested_revision"] == body["revision"] == 1 and body["simulated"]
    assert body["last_test"]["ok"] and body["last_test"]["by"]["id"] == a["users"]["admin"].id
    assert response["ETag"]


def test_the_connector_shows_its_connection(a, api):
    org, admin = a["org"], a["users"]["admin"]
    conn = connections.run_test(org, admin, ms(org, admin))
    connector = connector_on(org, admin, conn)
    body = api(a["users"]["viewer"]).get(f"connectors/{connector.id}").json_body
    last_test = body["connection"].pop("last_test")
    assert body["connection"] == {
        "id": str(conn.id),
        "kind": "microsoft",
        "name": conn.name,
        "enabled": True,
        "revision": 1,
        "tested_revision": 1,
        "test_current": True,
    }
    assert last_test["ok"] is True and last_test["by"]["id"] == admin.id and last_test["problem"] == ""
    assert set(last_test) == {"at", "ok", "by", "problem", "problem_label", "code", "detail", "next"}
    assert body["tenant_id"] == "simulated" and body["simulated"] and body["secret_set_on"]
    listed = api(admin).get(f"connections/microsoft/{conn.id}").json_body
    assert listed["connectors"] == [{"id": str(connector.id), "name": "Entra"}]


def test_the_connection_brief_tells_not_tested_from_changed_since_its_test(a, api):
    """The brief's last_test is null until a test, and stays the last (passed) test once the
    connection changed, so the SPA says "not tested" or "changed since last test". The list reads it without a
    query per connector."""
    org, admin = a["org"], a["users"]["admin"]
    conn = ms(org, admin)
    connector = connector_on(org, admin, conn)
    viewer = api(a["users"]["viewer"])
    brief = viewer.get(f"connectors/{connector.id}").json_body["connection"]
    assert brief["test_current"] is False and brief["last_test"] is None  # never tested
    connections.run_test(org, admin, conn)
    conn.refresh_from_db()
    connections.update_connection(org, admin, conn, {"secret": "another-synthetic-secret"})
    brief = viewer.get(f"connectors/{connector.id}").json_body["connection"]
    assert brief["test_current"] is False and brief["last_test"]["ok"] is True  # changed since its last test
    assert "another-synthetic-secret" not in json.dumps(brief)
    with CaptureQueriesContext(db) as one:
        assert len(viewer.get("connectors").json_body) == 1
    for k in range(3):
        connector_on(org, admin, connections.run_test(org, admin, ms(org, admin, name=f"Spare {k}")), name=f"C{k}")
    with CaptureQueriesContext(db) as four:
        assert len(viewer.get("connectors").json_body) == 4
    assert len(four.captured_queries) == len(one.captured_queries)  # no query per connector (its tester included)


ROUTES = [
    ("get", "connections/microsoft", None),
    ("post", "connections/microsoft", {"name": "New", "tenant_id": "simulated", "secret": "x"}),
    ("get", "connections/microsoft/{id}", None),
    ("patch", "connections/microsoft/{id}", {"notes": "x"}),
    ("post", "connections/microsoft/{id}/test", None),
    ("delete", "connections/microsoft/{doomed}", None),
]


@pytest.mark.parametrize("role", ["admin", "editor", "viewer"])
def test_role_matrix_admin_only(a, api, role):
    conn, doomed = ms(a["org"], a["users"]["admin"]), ms(a["org"], a["users"]["admin"], name="Doomed")
    client = api(a["users"][role])
    for method, url, body in ROUTES:
        url = url.format(id=conn.id, doomed=doomed.id)
        response = getattr(client, method)(url, body) if body is not None else getattr(client, method)(url)
        if role == "admin":
            assert response.status_code in (200, 201, 204), (method, url, response.json_body)
        else:
            assert response.status_code == 403, (role, method, url)


def test_halo_role_matrix_in_enterprise(enterprise, api):
    a = enterprise
    conn, doomed = hal(a["org"], a["users"]["admin"]), hal(a["org"], a["users"]["admin"], name="Doomed")
    routes = [
        ("get", "connections/halo", None),
        ("post", "connections/halo", {"name": "N", "base_url": HALO_URL, "client_id": "c", "secret": "s"}),
        ("get", f"connections/halo/{conn.id}", None),
        ("patch", f"connections/halo/{conn.id}", {"scope": "all"}),
        ("post", f"connections/halo/{conn.id}/test", None),
        ("delete", f"connections/halo/{doomed.id}", None),
    ]
    for role in ("editor", "viewer"):
        for method, url, body in routes:
            response = getattr(api(a["users"][role]), method)(url, *([body] if body else []))
            assert response.status_code == 403 and "capability" not in response.json_body, (role, url)
    admin = api(a["users"]["admin"])
    for method, url, body in routes:
        response = getattr(admin, method)(url, *([body] if body else []))
        assert response.status_code in (200, 201, 204), (url, response.json_body)


# ---- cross-tenant --------------------------------------------------------------------------------------------------


def test_another_organizations_connection_is_unknown(a, b, api):
    theirs = ms(b["org"], b["users"]["admin"], name="Theirs")
    admin = api(a["users"]["admin"])
    assert admin.get("connections/microsoft").json_body == []
    for method, suffix, body in (
        ("get", "", None),
        ("patch", "", {"notes": "x"}),
        ("delete", "", None),
        ("post", "/test", None),
    ):
        url = f"connections/microsoft/{theirs.id}{suffix}"
        response = getattr(admin, method)(url, body) if body else getattr(admin, method)(url)
        assert response.status_code == 404, (method, url)
    response = admin.post("connectors", {"name": "Smuggled", "microsoft_connection": str(theirs.id)})
    assert response.status_code == 400 and "microsoft_connection" in response.json_body["errors"]
    theirs.refresh_from_db()
    assert theirs.notes == "" and theirs.tested_at is None


def test_database_allows_only_one_connection_per_connector(enterprise):
    a = enterprise
    m, h = ms(a["org"], a["users"]["admin"]), hal(a["org"], a["users"]["admin"])
    _rejected(
        lambda: Connector.objects.create(
            organization=a["org"], source="entra", name="X", microsoft_connection=m, halo_connection=h
        )
    )


def test_halo_connections_have_their_own_kind(enterprise):
    a = enterprise
    h = hal(a["org"], a["users"]["admin"], name="Contoso Entra")  # the same name as a Microsoft one is fine
    ms(a["org"], a["users"]["admin"])
    assert HaloConnection.objects.get(pk=h.pk).name == "Contoso Entra"
    with pytest.raises(FieldErrors) as e:
        discovery.create_connector(a["org"], a["users"]["admin"], {"name": "X", "microsoft_connection": str(h.id)})
    assert "microsoft_connection" in e.value.errors


# ---- the move of connector credentials (discovery 0005, 0006) -------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_connector_credentials_move_to_connections_still_encrypted():
    from datetime import timedelta

    from django.db.migrations.executor import MigrationExecutor
    from django.utils import timezone

    before_state = [("discovery", "0004_observation_license_kind"), ("core", "0008_auditevent_append_only")]
    executor = MigrationExecutor(db)
    executor.migrate(before_state)
    old = executor.loader.project_state(before_state).apps
    Org, OldConnector, OldRun = (
        old.get_model("core", "Organization"),
        old.get_model("discovery", "Connector"),
        old.get_model("discovery", "ConnectorRun"),
    )
    org = Org.objects.create(name="Upgraded connectors (synthetic)", slug="upgraded-connectors-synthetic")
    token, kid = credentials.encrypt(SECRET)
    now = timezone.now()
    working = OldConnector.objects.create(
        organization=org,
        source="entra",
        name="Working",
        config={"tenant_id": TENANT, "client_id": CLIENT, "simulated_size": 3},
        scope={},
        secret_ciphertext=token,
        secret_key_id=kid,
        secret_set_at=now,
    )
    OldRun.objects.create(organization=org, connector=working, trigger="manual", status="succeeded", finished_at=now)
    broken = OldConnector.objects.create(
        organization=org, source="entra", name="Broken", config={"tenant_id": "simulated"}, scope={}
    )
    OldRun.objects.create(organization=org, connector=broken, trigger="manual", status="failed")
    earlier = now - timedelta(hours=2)
    # The last run succeeded, then the secret was replaced through the old form and nothing ran since.
    rotated = OldConnector.objects.create(
        organization=org,
        source="entra",
        name="Rotated",
        config={"tenant_id": TENANT, "client_id": CLIENT},
        scope={},
        secret_ciphertext=token,
        secret_key_id=kid,
        secret_set_at=now,
    )
    OldRun.objects.create(
        organization=org,
        connector=rotated,
        trigger="manual",
        status="succeeded",
        started_at=earlier,
        finished_at=earlier,
    )
    OldConnector.objects.filter(pk=rotated.pk).update(updated_at=earlier - timedelta(hours=1))  # only the secret moved
    # The last run succeeded, then the connector (its tenant and client) was edited and nothing ran since.
    edited = OldConnector.objects.create(
        organization=org,
        source="entra",
        name="Edited",
        config={"tenant_id": TENANT, "client_id": CLIENT},
        scope={},
        secret_ciphertext=token,
        secret_key_id=kid,
        secret_set_at=earlier - timedelta(hours=1),
    )
    OldRun.objects.create(
        organization=org,
        connector=edited,
        trigger="manual",
        status="succeeded",
        started_at=earlier,
        finished_at=earlier,
    )
    executor = MigrationExecutor(db)
    executor.migrate(executor.loader.graph.leaf_nodes())
    try:
        w = Connector.objects.select_related("microsoft_connection").get(pk=working.pk)
        conn = w.microsoft_connection
        assert conn is not None
        assert (conn.name, conn.tenant_id, conn.client_id) == ("Working", TENANT, CLIENT)
        assert conn.secret_ciphertext == token and conn.secret_key_id == kid and conn.secret_set_at == now
        assert credentials.slot_value(conn, "secret") == SECRET
        assert w.config == {"simulated_size": 3}  # the credentials left the connector's config
        assert conn.test_current and conn.test_result["carried_over"]  # its last run worked
        bc = Connector.objects.select_related("microsoft_connection").get(pk=broken.pk).microsoft_connection
        assert bc is not None
        assert bc.name == "Broken" and bc.secret_ciphertext == "" and not bc.test_current  # must be tested first
        for changed in (rotated, edited):  # changed after their last successful run: never carried over as tested
            c = Connector.objects.select_related("microsoft_connection").get(pk=changed.pk).microsoft_connection
            assert c is not None and c.secret_ciphertext == token
            assert not c.test_current and c.tested_at is None and c.test_result == {}, changed.name
        assert {f.name for f in Connector._meta.concrete_fields}.isdisjoint({"secret_ciphertext", "secret_key_id"})
    finally:
        with db.cursor() as cursor:
            cursor.execute("DELETE FROM catalog_connectorrun WHERE organization_id = %s", [org.id])
            cursor.execute("DELETE FROM catalog_connector WHERE organization_id = %s", [org.id])
            cursor.execute("DELETE FROM core_microsoftconnection WHERE organization_id = %s", [org.id])
            cursor.execute("DELETE FROM catalog_relationshiptype WHERE organization_id = %s", [org.id])
            cursor.execute("DELETE FROM catalog_softwarecategory WHERE organization_id = %s", [org.id])
            cursor.execute("DELETE FROM notify_notificationrule WHERE organization_id = %s", [org.id])
            cursor.execute("DELETE FROM catalog_organization WHERE id = %s", [org.id])


# ---- private service hosts: the installation setting ---------------------------------------------------------------

PRIVATE = (
    "https://10.0.0.5",
    "https://192.168.1.20",
    "https://100.64.1.1",
    "https://[fd12:3456::1]",
    "https://[fec0::1]",
)
METADATA = (
    "https://169.254.169.254",
    "https://[fd00:ec2::254]",
    "https://100.100.100.200",
    "https://[::ffff:100.100.100.200]",
)


@pytest.mark.parametrize("allowed", [True, False])
def test_private_service_hosts_follow_the_installation_setting(settings, allowed):
    settings.SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS = allowed
    for url in PRIVATE:
        host = url.removeprefix("https://").strip("[]")
        if allowed:
            outbound.check_service_url(url)
            outbound.check_smtp_host(host)
        else:
            with pytest.raises(outbound.UnsafeDestination):
                outbound.check_service_url(url)
            with pytest.raises(outbound.UnsafeDestination):
                outbound.check_smtp_host(host)
    for url in METADATA:  # cloud metadata is refused in both modes
        with pytest.raises(outbound.UnsafeDestination):
            outbound.check_service_url(url)
        with pytest.raises(outbound.UnsafeDestination):
            outbound.check_smtp_host(url.removeprefix("https://").strip("[]"))
    outbound.check_service_url("https://8.8.8.8")  # public is fine either way


def test_a_name_resolving_to_a_private_address_is_refused_when_private_hosts_are_off(enterprise, settings, monkeypatch):
    settings.SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS = False
    a = enterprise
    conn = hal(a["org"], a["users"]["admin"], base_url="https://halo.example.com")
    monkeypatch.setattr(outbound, "_resolve", lambda host, port: ["10.1.2.3"])
    monkeypatch.setattr(outbound._PinnedHTTPSConnection, "connect", lambda self: pytest.fail("must not connect"))
    assert connections.run_test(a["org"], a["users"]["admin"], conn).test_result["problem"] == "refused"
    with pytest.raises(FieldErrors):
        hal(a["org"], a["users"]["admin"], name="Private", base_url="https://10.1.2.3")


def test_the_private_hosts_setting_reads_the_environment(monkeypatch):
    from softwarden.env import env_flag

    for value, expected in (("false", False), ("0", False), ("OFF", False), ("no", False), ("true", True), ("1", True)):
        monkeypatch.setenv("SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS", value)
        assert env_flag("SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS", True) is expected, value
    monkeypatch.delenv("SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS")
    assert env_flag("SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS", True) is True
