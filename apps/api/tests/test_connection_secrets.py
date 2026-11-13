"""Secret hygiene for connections (acceptance check: "No secret or token appears in any log line, audit snapshot, API
response or export"). The part of the exposure tests that applies to connections: create a
Microsoft and a Halo connection with known secret values, test them, run a connector, replace the secrets, delete
them, and look for the values (and their ciphertexts, and the tokens the simulators issue) everywhere they could leak.

The full suite, every source's run as a job, access checks, run history, run errors, reconciliation,
reminders, the notification log and a sweep of every GET route for every role, is tests/test_integration_exposure.py;
this file keeps the connection-specific cases."""

import json
import logging

import pytest

from core import connections, graph_simulator, halo
from core.capabilities import set_edition
from core.models import AuditEvent, HaloConnection, Job, MicrosoftConnection
from discovery import engine as discovery
from discovery.models import Connector, ConnectorRun, RunStatus
from tests.conftest import TEST_CREDENTIAL_KEY, drain_jobs

MS_SECRET = "ms-exposure-secret-Zq81~Kx"
MS_REPLACED = "ms-exposure-replaced-4Lp0~Yw"
HALO_SECRET = "halo-exposure-secret-77Tx"
HALO_REPLACED = "halo-exposure-replaced-90Qa"
SECRETS = (MS_SECRET, MS_REPLACED, HALO_SECRET, HALO_REPLACED)
TOKENS = (graph_simulator.TOKEN, halo.TOKEN)


@pytest.fixture
def org(a):
    a["org"], _ = set_edition(a["org"], "enterprise")  # Halo is enterprise; audit export as JSON lines too
    return a


def test_connection_secrets_never_leave_the_server(org, api, caplog):
    caplog.set_level(logging.DEBUG)
    a = org
    admin = api(a["users"]["admin"])
    seen: list[str] = []
    ciphertexts: set[str] = set()

    def keep(response):
        seen.append(
            response.content.decode() if not response.streaming else b"".join(response.streaming_content).decode()
        )
        seen.append(json.dumps(dict(response.items())))  # headers, the ETag included
        return response

    ms = keep(admin.post("connections/microsoft", {"name": "Entra", "tenant_id": "simulated", "secret": MS_SECRET}))
    assert ms.status_code == 201, ms.json_body
    hl = keep(
        admin.post(
            "connections/halo",
            {"name": "Halo", "base_url": halo.SIMULATED_URL, "client_id": "softwarden", "secret": HALO_SECRET},
        )
    )
    assert hl.status_code == 201, hl.json_body
    ms_url, hl_url = f"connections/microsoft/{ms.json_body['id']}", f"connections/halo/{hl.json_body['id']}"
    ciphertexts |= {
        MicrosoftConnection.objects.get(pk=ms.json_body["id"]).secret_ciphertext,
        HaloConnection.objects.get(pk=hl.json_body["id"]).secret_ciphertext,
    }

    # Test both (tokens are issued and must not be stored or returned), use one in a run, read everything back.
    assert keep(admin.post(f"{ms_url}/test")).json_body["test_current"]
    assert keep(admin.post(f"{hl_url}/test")).json_body["test_current"]
    connector = keep(admin.post("connectors", {"name": "Entra", "microsoft_connection": ms.json_body["id"]}))
    assert connector.status_code == 201, connector.json_body
    keep(admin.post(f"connectors/{connector.json_body['id']}/run"))
    drain_jobs()
    assert ConnectorRun.objects.get(connector_id=connector.json_body["id"]).status == RunStatus.SUCCEEDED
    for url in ("connections/microsoft", "connections/halo", ms_url, hl_url, "connectors", "discovery/candidates"):
        keep(admin.get(url))

    # Replace both secrets (a failing one for Halo too), test again, then delete.
    keep(admin.patch(ms_url, {"secret": MS_REPLACED}))
    keep(admin.patch(hl_url, {"secret": HALO_REPLACED, "scope": "read:assets"}))
    keep(admin.post(f"{ms_url}/test"))
    keep(admin.post(f"{hl_url}/test"))
    keep(admin.patch(hl_url, {"secret": "wrong-secret"}))
    keep(admin.post(f"{hl_url}/test"))
    ciphertexts |= {
        MicrosoftConnection.objects.get(pk=ms.json_body["id"]).secret_ciphertext,
        HaloConnection.objects.get(pk=hl.json_body["id"]).secret_ciphertext,
    }
    keep(admin.post("connectors", {"name": "Bad", "microsoft_connection": ms.json_body["id"], "secret": MS_SECRET}))
    discovery.delete_connector(a["org"], a["users"]["admin"], Connector.objects.get(pk=connector.json_body["id"]))
    assert keep(admin.delete(ms_url)).status_code == 204
    assert keep(admin.delete(hl_url)).status_code == 204

    # Exports: the audit log as CSV and as JSON lines.
    keep(admin.client.get("/api/v1/audit-events/export.csv"))
    keep(admin.client.get("/api/v1/audit-events/export.jsonl"))
    keep(admin.get("audit-events"))

    events = list(AuditEvent.objects.filter(organization=a["org"]))
    assert {e.action for e in events} >= {
        "microsoft_connection.created",
        "microsoft_connection.updated",
        "microsoft_connection.tested",
        "microsoft_connection.deleted",
        "halo_connection.created",
        "halo_connection.updated",
        "halo_connection.tested",
        "halo_connection.deleted",
    }
    stored = {
        "audit": json.dumps([[e.changes, e.prechange, e.postchange] for e in events], default=str),
        "jobs": json.dumps(list(Job.objects.values("payload", "last_error", "result")), default=str),
        "runs": json.dumps(list(ConnectorRun.objects.values("error", "counts")), default=str),
        "connections": json.dumps(
            list(MicrosoftConnection.objects.values("test_result"))
            + list(HaloConnection.objects.values("test_result")),
            default=str,
        ),
        "logs": caplog.text,
        "responses": "\n".join(seen),
    }
    for where, text in stored.items():
        for value in (*SECRETS, *TOKENS, *ciphertexts, TEST_CREDENTIAL_KEY):
            assert value not in text, f"{value[:12]}... found in {where}"
    # The audit trail still says that a secret was replaced, by name only.
    updated = next(e for e in events if e.action == "microsoft_connection.updated")
    assert updated.changes["replaced"] == ["secret"]


def test_secrets_are_never_job_arguments():
    """The connection test runs in the request; nothing about a connection is queued with its secret. A job
    payload with a secret-looking key is refused in any case (core/jobs.check_payload, which `enqueue` calls)."""
    from core import jobs

    for key in ("secret", "client_secret", "clientSecret"):
        with pytest.raises(ValueError):
            jobs.check_payload({key: MS_SECRET, "connection_id": "x"})


def test_a_secret_in_a_failed_test_never_reaches_the_stored_result(a, monkeypatch):
    """Even if a provider echoed the secret back in its error, only Softwarden's words and the code are stored."""
    from core import graph

    org, admin = a["org"], a["users"]["admin"]
    conn = connections.create_connection(
        org,
        admin,
        connections.MICROSOFT,
        {
            "name": "Echo",
            "tenant_id": "8c3dcbf0-7a3b-4f43-9a3e-1b2c3d4e5f60",
            "client_id": "1d2c3b4a-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
            "secret": MS_SECRET,
        },
    )

    class Echo:
        def send(self, method, url, headers, body=None):
            text = f"AADSTS7000215: Invalid client secret provided: {MS_SECRET}"
            return graph.Reply(401, {}, json.dumps({"error": "invalid_client", "error_description": text}).encode())

    monkeypatch.setattr(connections, "microsoft_transport", lambda tenant: Echo())
    tested = connections.run_test(org, admin, conn)
    assert tested.test_result["code"] == "AADSTS7000215"
    assert MS_SECRET not in json.dumps(tested.test_result)
    assert all(MS_SECRET not in json.dumps([e.changes, e.postchange]) for e in AuditEvent.objects.all())
