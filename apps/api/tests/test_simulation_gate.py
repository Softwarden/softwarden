"""Simulated tenants and hosts outside development (core/simulation.py).

The suite allows simulation by default (tests/conftest.simulation_allowed: pytest runs with DEBUG off). Here
`simulation_off` makes a production server (DEBUG off, not the Playwright server, SOFTWARDEN_ALLOW_SIMULATED unset)
and each connection kind is refused on save, at the test and at run time; one saved while it was allowed is kept and
refuses to run; the flag, DEBUG or the Playwright server allow it again; the sign-in simulators ignore the flag; and
seed_demo refuses with the flag's name before writing anything."""

from __future__ import annotations

import io
import json
import uuid
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings as django_settings
from django.core.management import CommandError, call_command

from catalog.models import Resource
from core import capabilities as caps
from core import connections, google_workspace, halo, oidc_simulator, okta, simulation
from core.models import GoogleWorkspaceConnection, HaloConnection, MicrosoftConnection, OktaConnection, Organization
from core.problems import Conflict, FieldErrors
from discovery import access, entra, graph_source
from discovery import engine as discovery
from discovery import google_workspace as gw_source
from discovery import okta as okta_source
from discovery.models import Connector, ConnectorRun, Observation, RunStatus
from notify import channel_services, channels
from notify.models import EmailSettings
from tickets import clients, refresh
from tickets import services as tickets
from tickets import simulator as tickets_simulator
from tickets.models import TicketConnection

FLAG = simulation.FLAG
SECRET = "synthetic-connection-secret-b1"  # noqa: S105 (synthetic)
GOOGLE_ADMIN = f"admin@{google_workspace.SIMULATED_DOMAIN}"


# ---- helpers ----------------------------------------------------------------------------------------------------------


def turn_off(settings, monkeypatch) -> None:
    """A production server from here on (the `simulation_off` fixture, mid-test)."""
    settings.DEBUG = False
    settings.SOFTWARDEN_ALLOW_SIMULATED = False
    monkeypatch.delenv("SOFTWARDEN_E2E", raising=False)


@pytest.fixture(scope="module")
def pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def key_file(pem: str) -> str:
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "contoso-prj",
            "private_key_id": "0123456789abcdef0123456789abcdef01234567",
            "private_key": pem,
            "client_email": "softwarden-discovery@contoso-prj.iam.gserviceaccount.com",
            "client_id": "112233445566778899001",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )


def enterprise(a) -> Any:
    a["org"], _ = caps.set_edition(a["org"], "enterprise")
    return a["org"]


# Each kind: (Kind, the field that names the simulator, the simulated data, the discovery source, its connector field).
def kinds(pem: str) -> dict[str, tuple[connections.Kind, str, dict[str, Any], str]]:
    return {
        "microsoft": (connections.MICROSOFT, "tenant_id", {"tenant_id": "simulated", "secret": SECRET}, "entra"),
        "halo": (
            connections.HALO,
            "base_url",
            {"base_url": halo.SIMULATED_URL, "client_id": "softwarden", "secret": SECRET},
            "halo",
        ),
        "okta": (
            connections.OKTA,
            "base_url",
            {"base_url": okta.SIMULATED_URL, "client_id": "0oasoftwarden", "secret": pem},
            "okta",
        ),
        "google": (
            connections.GOOGLE,
            "admin_email",
            {"admin_email": GOOGLE_ADMIN, "secret": key_file(pem)},
            "google_workspace",
        ),
    }


KINDS = ["microsoft", "halo", "okta", "google"]


def simulated_connector(a, pem, which: str) -> tuple[Any, Connector]:
    """A connection of `which` on its simulator, tested, and a connector on it (made while simulation is allowed)."""
    org, admin = enterprise(a), a["users"]["admin"]
    kind, _, data, source = kinds(pem)[which]
    conn = connections.create_connection(org, admin, kind, {"name": f"{which} {uuid.uuid4().hex[:6]}", **data})
    conn = connections.run_test(org, admin, conn)
    assert conn.test_current, conn.test_result
    connector = discovery.create_connector(
        org, admin, {"name": f"{which} connector", "source": source, f"{kind.key}_connection": str(conn.id)}
    )
    return conn, Connector.objects.get(pk=connector.pk)


# ---- the one rule ------------------------------------------------------------------------------------------------------


def test_allowed_under_debug_the_playwright_server_or_the_flag_only(settings, monkeypatch, db):
    turn_off(settings, monkeypatch)
    assert not simulation.allowed() and not simulation.development()
    settings.SOFTWARDEN_ALLOW_SIMULATED = True
    assert simulation.allowed() and not simulation.development()
    settings.SOFTWARDEN_ALLOW_SIMULATED = False
    settings.DEBUG = True
    assert simulation.allowed() and simulation.development()
    settings.DEBUG = False
    monkeypatch.setenv("SOFTWARDEN_E2E", "1")
    assert not simulation.allowed()  # the e2e flag alone: the database isn't a throwaway _e2e one
    monkeypatch.setitem(django_settings.DATABASES["default"], "NAME", "softwarden_e2e")
    assert simulation.allowed() and simulation.development()


def test_the_flag_never_turns_on_the_sign_in_simulators(settings, monkeypatch, db):
    """SOFTWARDEN_ALLOW_SIMULATED allows simulated data, never a simulator that signs anyone in as any email."""
    turn_off(settings, monkeypatch)
    settings.SOFTWARDEN_ALLOW_SIMULATED = True
    assert simulation.allowed() and not oidc_simulator.allowed()
    settings.DEBUG = True
    assert oidc_simulator.allowed()


# ---- save -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("which", KINDS)
def test_a_simulated_value_is_refused_on_save_naming_the_flag(a, pem, preview_on, simulation_off, which):
    org, admin = enterprise(a), a["users"]["admin"]
    kind, field, data, _ = kinds(pem)[which]
    with pytest.raises(FieldErrors) as refused:
        connections.create_connection(org, admin, kind, {"name": "Synthetic", **data})
    message = refused.value.errors[field][0]
    assert FLAG in message and "demos and tests" in message
    assert not kind.model.objects.filter(organization=org).exists()


def test_an_update_to_a_simulated_value_is_refused(a, simulation_off):
    org, admin = a["org"], a["users"]["admin"]
    real = {
        "name": "Contoso",
        "tenant_id": "8c3dcbf0-7a3b-4f43-9a3e-1b2c3d4e5f60",
        "client_id": "1d2c3b4a-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
        "secret": SECRET,
    }
    conn = connections.create_connection(org, admin, connections.MICROSOFT, real)
    with pytest.raises(FieldErrors) as refused:
        connections.update_connection(org, admin, conn, {"tenant_id": "simulated"})
    assert FLAG in refused.value.errors["tenant_id"][0]
    assert MicrosoftConnection.objects.get(pk=conn.pk).tenant_id == real["tenant_id"]


def test_the_api_answers_400_with_the_field_error(a, api, simulation_off):
    response = api(a["users"]["admin"]).post(
        "connections/microsoft", {"name": "Synthetic", "tenant_id": "simulated", "secret": SECRET}
    )
    assert response.status_code == 400
    assert FLAG in json.dumps(response.json_body)


def test_the_legacy_connector_form_is_refused_on_its_tenant_field(a, simulation_off):
    with pytest.raises(FieldErrors) as refused:
        discovery.create_connector(
            a["org"], a["users"]["admin"], {"name": "Entra", "tenant_id": "simulated", "secret": SECRET}
        )
    assert FLAG in refused.value.errors["tenant_id"][0]
    assert not Connector.objects.filter(organization=a["org"]).exists()


# ---- kept, but not tested or run ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("which", KINDS)
def test_an_existing_simulated_connection_is_kept_but_refuses_to_test_and_run(
    a, api, pem, settings, monkeypatch, preview_on, which
):
    conn, connector = simulated_connector(a, pem, which)
    org, admin = a["org"], a["users"]["admin"]
    turn_off(settings, monkeypatch)
    model = type(conn)

    # Kept: still listed, still marked simulated, its connector still there.
    assert model.objects.filter(pk=conn.pk).exists() and Connector.objects.filter(pk=connector.pk).exists()
    kind = connections.kind_of(conn)
    listed = api(admin).get(f"connections/{kind.key}").json_body
    rows = listed["results"] if isinstance(listed, dict) else listed
    assert [r["simulated"] for r in rows if r["id"] == str(conn.id)] == [True]

    # Run now: 409 naming the flag; nothing is queued.
    response = api(admin).post(f"connectors/{connector.id}/run")
    assert response.status_code == 409 and FLAG in response.json_body["detail"]
    with pytest.raises(Conflict):
        discovery.request_run(org, admin, connector)

    # A run started anyway (the schedule, a queued job) fails with the reason and reads nothing.
    run = discovery.run(connector)
    assert run.status == RunStatus.FAILED and FLAG in run.error and conn.name in run.error
    assert not Observation.objects.filter(connector=connector).exists()

    # The access check says why without signing in.
    result = access.evaluate(connector)
    assert not result["ok"] and result["steps"][0]["problem"] == "simulation_off"
    assert FLAG in result["steps"][0]["next"]

    # The test: refused with its own problem, recorded as failed.
    tested = connections.run_test(org, admin, conn)
    assert tested.test_result["problem"] == "simulation_off" and FLAG in tested.test_result["detail"]
    assert tested.test_result["problem_label"] == "Simulation not allowed"
    assert not tested.test_current
    response = api(admin).post(f"connections/{kind.key}/{conn.id}/test")
    assert response.status_code == 200 and response.json_body["result"]["problem"] == "simulation_off"

    # Renaming it is still allowed (only setting a simulated value is refused).
    renamed = connections.update_connection(org, admin, conn, {"name": "Kept for later"})
    assert renamed.name == "Kept for later"


@pytest.mark.parametrize("which", KINDS)
def test_a_new_connector_on_a_stored_simulated_connection_is_refused(
    a, api, pem, settings, monkeypatch, preview_on, which
):
    """With simulation off, a connector naming a kept simulated connection is refused on create (API and
    service) and on update, naming the flag; nothing is written."""
    conn, connector = simulated_connector(a, pem, which)
    org, admin = a["org"], a["users"]["admin"]
    kind, _, _, source = kinds(pem)[which]
    turn_off(settings, monkeypatch)
    field = f"{kind.key}_connection"
    before = Connector.objects.count()

    response = api(admin).post("connectors", {"name": "new on simulated", "source": source, field: str(conn.id)})
    assert response.status_code == 400, response.content
    assert FLAG in json.dumps(response.json_body)
    with pytest.raises(FieldErrors) as refused:
        discovery.create_connector(org, admin, {"name": "service", "source": source, field: str(conn.id)})
    assert FLAG in refused.value.errors[field][0]
    assert Connector.objects.count() == before

    # Pointing another connector at it is refused too; renaming the kept one is not.
    real = connections.create_connection(org, admin, kind, {"name": "real", **real_data(pem, which)})
    other = discovery.create_connector(org, admin, {"name": "other", "source": source, field: str(real.id)})
    with pytest.raises(FieldErrors) as refused:
        discovery.update_connector(org, admin, other, {field: str(conn.id)})
    assert FLAG in refused.value.errors[field][0]
    assert discovery.update_connector(org, admin, connector, {"name": "kept, renamed"}).name == "kept, renamed"


def real_data(pem: str, which: str) -> dict[str, Any]:
    """A non-simulated connection of `which` (never contacted here)."""
    return {
        "microsoft": {
            "tenant_id": "11111111-2222-4333-8444-555555555555",
            "client_id": "22222222-3333-4444-8555-666666666666",
            "secret": SECRET,
        },
        "halo": {"base_url": "https://halo.example.com", "client_id": "softwarden", "secret": SECRET},
        "okta": {"base_url": "https://example.okta.com", "client_id": "0oasoftwarden", "secret": pem},
        "google": {"admin_email": "admin@example.com", "secret": key_file(pem)},
    }[which]


@pytest.mark.parametrize("which", KINDS)
def test_the_flag_allows_them_again(a, pem, settings, monkeypatch, preview_on, which):
    turn_off(settings, monkeypatch)
    settings.SOFTWARDEN_ALLOW_SIMULATED = True
    _, connector = simulated_connector(a, pem, which)
    run = discovery.run(connector)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert Observation.objects.filter(connector=connector).exists()


def test_debug_allows_them_without_the_flag(a, pem, settings, monkeypatch):
    turn_off(settings, monkeypatch)
    settings.DEBUG = True
    _, connector = simulated_connector(a, pem, "microsoft")
    assert discovery.run(connector).status == RunStatus.SUCCEEDED


def test_a_queued_run_fails_with_the_reason_when_simulation_is_turned_off(a, pem, settings, monkeypatch):
    """A run queued while it was allowed (Run now, the daily schedule) fails in the worker once it isn't."""
    _, connector = simulated_connector(a, pem, "microsoft")
    run_ = discovery.request_run(a["org"], a["users"]["admin"], connector)
    turn_off(settings, monkeypatch)
    from tests.conftest import drain_jobs

    drain_jobs()
    run_ = ConnectorRun.objects.get(pk=run_.pk)
    assert run_.status == RunStatus.FAILED and FLAG in run_.error


# ---- the transport selections (a backstop behind the checks above) -----------------------------------------------------


def test_every_transport_selection_refuses_a_simulator(a, pem, simulation_off):
    with pytest.raises(simulation.NotAllowed):
        connections.microsoft_transport("simulated")
    for select, error in (
        (lambda: halo.transport_for(halo.SIMULATED_URL), halo.HaloError),
        (lambda: okta.transport_for(okta.SIMULATED_URL), okta.OktaError),
        (lambda: google_workspace.transport_for(GOOGLE_ADMIN), google_workspace.GoogleError),
    ):
        with pytest.raises(error) as refused:
            select()
        found: Any = refused.value
        assert found.refused and FLAG in found.reason
    with pytest.raises(clients.TicketError) as ticket:
        clients.transport_for(tickets_simulator.JIRA_URL)
    assert ticket.value.kind == "refused" and FLAG in str(ticket.value)
    with pytest.raises(simulation.NotAllowed):
        channels.graph_transport("simulated")
    assert not entra.valid_tenant("simulated") and entra.valid_tenant("contoso.onmicrosoft.com")

    ms = connections.MicrosoftCredential("simulated", "", SECRET)
    hal = connections.HaloCredential(halo.SIMULATED_URL, "softwarden", SECRET)
    for credential in (ms, hal):
        with pytest.raises(graph_source.SourceError, match=FLAG):
            graph_source.transports(None, credential, graph_source.entra_simulator)
    with pytest.raises(graph_source.SourceError, match=FLAG):
        graph_source.Client(object(), ms)
    with pytest.raises(graph_source.SourceError, match=FLAG):
        okta_source.transport_for(None, connections.OktaCredential(okta.SIMULATED_URL, "c", "", "s", pem))
    with pytest.raises(graph_source.SourceError, match=FLAG):
        gw_source.transport_for(None, connections.GoogleCredential(GOOGLE_ADMIN, "sa@x", "1", "s"))
    # A real address is untouched: the selection returns the real transport (nothing is sent here).
    assert not isinstance(halo.transport_for("https://contoso.haloitsm.com"), halo.SimulatedTransport)


# ---- ticket connections ------------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("preview_on")
@pytest.mark.parametrize("system", ["jira", "servicenow"])
def test_a_simulated_ticket_system_is_refused_on_save(a, simulation_off, system):
    url = tickets_simulator.JIRA_URL if system == "jira" else tickets_simulator.SERVICENOW_URL
    with pytest.raises(FieldErrors) as refused:
        tickets.create_connection(
            a["org"], a["users"]["admin"], {"system": system, "base_url": url, "username": "it", "secret": SECRET}
        )
    assert FLAG in refused.value.errors["base_url"][0]
    assert not TicketConnection.objects.filter(organization=a["org"]).exists()


def _ticket_link(a):
    from catalog import assessments

    app = Resource.objects.create(organization=a["org"], kind="application", name="Payroll")
    assessment = assessments.create_assessment(
        a["org"], a["users"]["editor"], {"target": str(app.id), "change_type": "change", "title": "Upgrade"}
    )
    target = tickets.assessment_target(a["org"], assessment.id)
    link = tickets.create_link(
        a["org"], a["users"]["editor"], target, {"url": f"{tickets_simulator.JIRA_URL}/browse/ITOPS-1"}
    )
    return assessment, link


@pytest.mark.usefixtures("preview_on")
def test_an_existing_simulated_ticket_connection_is_kept_but_refuses_to_test_and_refresh(a, api, settings, monkeypatch):
    org, admin, editor = a["org"], a["users"]["admin"], a["users"]["editor"]
    data = {"system": "jira", "base_url": tickets_simulator.JIRA_URL, "username": "it@example.test", "secret": SECRET}
    conn = tickets.run_test(org, admin, tickets.create_connection(org, admin, data))
    assert conn.test_current
    assessment, link = _ticket_link(a)
    turn_off(settings, monkeypatch)

    assert TicketConnection.objects.filter(pk=conn.pk).exists()
    # The refresh an Editor asks for: 409 naming the flag; the hourly job skips the system.
    with pytest.raises(Conflict, match=FLAG):
        tickets.refresh_link(org, editor, link)
    response = api(editor).post(f"assessments/{assessment.id}/ticket-links/{link.id}/refresh")
    assert response.status_code == 409 and FLAG in response.json_body["detail"]
    counts = refresh.refresh_org(org)
    assert counts["read"] == 0 and counts["skipped_systems"] == 1
    assert [s["reads"] for s in tickets.systems(org) if s["system"] == "jira"] == [False]
    # The test: refused with its own problem.
    tested = tickets.run_test(org, admin, conn)
    assert tested.test_result["problem"] == "simulation_off" and not tested.test_current

    settings.SOFTWARDEN_ALLOW_SIMULATED = True  # the flag brings it back
    conn = tickets.run_test(org, admin, conn)
    assert conn.test_current
    assert tickets.refresh_link(org, editor, link).status_error == ""


@pytest.mark.usefixtures("preview_on")
def test_a_halo_ticket_connection_on_the_simulated_halo_refuses_to_run(a, settings, monkeypatch):
    org, admin = enterprise(a), a["users"]["admin"]
    data = {
        "name": "Halo",
        "base_url": halo.SIMULATED_URL,
        "client_id": "sw",
        "scope": "read:assets read:tickets",
        "secret": SECRET,
    }
    base = connections.run_test(org, admin, connections.create_connection(org, admin, connections.HALO, data))
    conn = tickets.run_test(
        org, admin, tickets.create_connection(org, admin, {"system": "halo", "halo_connection": str(base.id)})
    )
    assert conn.test_current
    turn_off(settings, monkeypatch)
    with pytest.raises(tickets.NotUsable, match=FLAG):
        tickets.credential_of(TicketConnection.objects.get(pk=conn.pk))
    tested = tickets.run_test(org, admin, conn)
    assert tested.test_result["problem"] == "simulation_off"
    assert HaloConnection.objects.filter(pk=base.pk).exists()


# ---- Graph email -------------------------------------------------------------------------------------------------------


GRAPH = {
    "transport": "graph",
    "graph_tenant_id": "simulated",
    "graph_client_id": "11111111-2222-4333-8444-555555555555",
    "graph_secret": "graph-client-secret-b1",
    "sender_mailbox": "softwarden@contoso.example",
}


def test_graph_email_on_the_simulated_tenant_is_refused_on_save(a, simulation_off):
    with pytest.raises(FieldErrors) as refused:
        channel_services.update_email_settings(a["org"], a["users"]["admin"], GRAPH)
    assert FLAG in refused.value.errors["graph_tenant_id"][0]


def test_stored_graph_email_on_the_simulated_tenant_refuses_to_test_and_send(a, settings, monkeypatch):
    channel_services.update_email_settings(a["org"], a["users"]["admin"], GRAPH)
    turn_off(settings, monkeypatch)
    row = EmailSettings.objects.get(organization=a["org"])
    result = channels.test_email_connection(a["org"])
    assert not result.ok and FLAG in result.detail
    with pytest.raises(channels.DeliveryFailure, match=FLAG) as failure:
        channels._send_graph(row, None)  # type: ignore[arg-type]  # refused before the delivery is read
    assert failure.value.outcome == "permanent"


# ---- the demo ----------------------------------------------------------------------------------------------------------


def test_seed_demo_refuses_with_the_flag_before_writing_anything(simulation_off, db):
    with pytest.raises(CommandError) as refused:
        call_command("seed_demo", stdout=io.StringIO())
    assert f"{FLAG}=1" in str(refused.value) and "Nothing was changed" in str(refused.value)
    assert not Organization.objects.filter(name="Contoso (synthetic)").exists()


def test_seed_demo_accounts_only_refuses_too_unless_the_database_is_a_playwright_one(simulation_off, db, monkeypatch):
    """--accounts-only makes accounts with the published demo password, so it refuses on a server that
    allows no simulators, before writing anything; the Playwright servers' `_e2e` databases are the exception."""
    from catalog.management.commands import seed_demo

    with pytest.raises(CommandError) as refused:
        call_command("seed_demo", accounts_only=True, stdout=io.StringIO())
    assert f"{FLAG}=1" in str(refused.value) and "Nothing was changed" in str(refused.value)
    assert not Organization.objects.filter(name="Contoso (synthetic)").exists()
    monkeypatch.setattr(seed_demo, "e2e_database", lambda: True)
    call_command("seed_demo", accounts_only=True, stdout=io.StringIO())
    assert Organization.objects.filter(name="Contoso (synthetic)").exists()


def test_seed_demo_with_the_flag_seeds_the_discovery_scenario(settings, monkeypatch, db):
    turn_off(settings, monkeypatch)
    settings.SOFTWARDEN_ALLOW_SIMULATED = True
    call_command("seed_demo", stdout=io.StringIO())
    org = Organization.objects.get(name="Contoso (synthetic)")
    connector = Connector.objects.get(organization=org)
    assert connector.runs.filter(status=RunStatus.SUCCEEDED).exists()


@pytest.mark.parametrize("command,args", [("simulate_entra_change", ["reset"]), ("seed_scale", []), ("nfr_timing", [])])
def test_the_other_demo_commands_refuse_too(simulation_off, db, command, args):
    with pytest.raises(CommandError, match=FLAG):
        call_command(command, *args, stdout=io.StringIO())


def test_the_connection_models_are_the_four_kinds():
    """Every connection kind is covered by is_simulated (a new kind needs its own gate)."""
    assert {k.model for k in connections.KINDS.values()} == {
        MicrosoftConnection,
        HaloConnection,
        OktaConnection,
        GoogleWorkspaceConnection,
    }


def test_doctor_warns_about_a_kept_simulated_connector(a, pem, settings, monkeypatch):
    from core import doctor

    _, connector = simulated_connector(a, pem, "microsoft")
    turn_off(settings, monkeypatch)
    found = [c for c in doctor.check_discovery(a["org"]) if c.name == f"discovery: {connector.name}"]
    assert len(found) == 1 and found[0].status == doctor.WARN and FLAG in found[0].fix


# ---- review follow-ups -------------------------------------------------------------------------------------------------


def test_auth_me_says_whether_simulation_is_allowed(a, api, settings, monkeypatch):
    """The SPA shows its simulated-tenant hints only where they would be accepted."""
    admin = api(a["users"]["admin"])
    assert admin.get("auth/me").json_body["simulation_allowed"] is True
    turn_off(settings, monkeypatch)
    assert admin.get("auth/me").json_body["simulation_allowed"] is False
    assert api(a["users"]["viewer"]).get("auth/me").json_body["simulation_allowed"] is False


def test_the_schedule_skips_a_kept_simulated_connector(a, pem, settings, monkeypatch):
    """A demo install promoted to production: nothing is queued, so no run fails every day."""
    from django.utils import timezone

    _, connector = simulated_connector(a, pem, "microsoft")
    now = timezone.now()
    Connector.objects.filter(pk=connector.pk).update(run_requested_at=now, run_requested_by=a["users"]["admin"])
    turn_off(settings, monkeypatch)
    assert discovery.due_runs(now) == 0
    assert not ConnectorRun.objects.filter(connector=connector).exists()
    settings.SOFTWARDEN_ALLOW_SIMULATED = True
    assert discovery.due_runs(now) == 1


@pytest.mark.usefixtures("preview_on")
def test_a_halo_ticket_connection_cannot_point_at_a_kept_simulated_halo(a, settings, monkeypatch):
    org, admin = enterprise(a), a["users"]["admin"]
    data = {
        "name": "Halo",
        "base_url": halo.SIMULATED_URL,
        "client_id": "sw",
        "scope": "read:tickets",
        "secret": SECRET,
    }
    base = connections.create_connection(org, admin, connections.HALO, data)
    turn_off(settings, monkeypatch)
    with pytest.raises(FieldErrors) as refused:
        tickets.create_connection(org, admin, {"system": "halo", "halo_connection": str(base.id)})
    assert FLAG in refused.value.errors["halo_connection"][0]


def test_kept_graph_email_on_the_simulated_tenant_is_refused_on_any_graph_save(a, settings, monkeypatch):
    org, admin = a["org"], a["users"]["admin"]
    channel_services.update_email_settings(org, admin, GRAPH)
    turn_off(settings, monkeypatch)
    with pytest.raises(FieldErrors) as refused:
        channel_services.update_email_settings(org, admin, {"sender_mailbox": "it@contoso.example"})
    assert FLAG in refused.value.errors["graph_tenant_id"][0]
    with pytest.raises(FieldErrors) as again:
        channel_services.update_email_settings(org, admin, {"transport": "graph"})
    assert FLAG in again.value.errors["graph_tenant_id"][0]
    # Moving to another transport is allowed; the kept Graph values aren't used.
    row = channel_services.update_email_settings(
        org, admin, {"transport": "smtp", "smtp_host": "mail.example.test", "from_address": "it@contoso.example"}
    )
    assert row.transport == "smtp"


# ---- tripwires: a new simulator or ticket system needs its own gate -----------------------------------------------------

# Every simulator module and where it is refused unless core/simulation.allowed(). A new one fails the test below until
# it is gated and listed here.
SIMULATOR_GATES = {
    "core/graph_simulator.py": "connections.microsoft_transport, notify.channels.graph_transport",
    "core/oidc_simulator.py": "sign-in only: simulation.development() (never the flag)",
    "discovery/simulator_base.py": "shared helpers of the discovery simulators below",
    "discovery/entra_simulator.py": "graph_source.transports and graph_source.Client",
    "discovery/azure_simulator.py": "graph_source.transports (Microsoft connection)",
    "discovery/powerplatform_simulator.py": "graph_source.transports (Microsoft connection)",
    "discovery/halo_simulator.py": "graph_source.transports (Halo connection), halo.transport_for",
    "discovery/okta_simulator.py": "discovery.okta.transport_for, okta.transport_for",
    "discovery/google_workspace_simulator.py": "discovery.google_workspace.transport_for, google_workspace.transport_for",
    "tickets/simulator.py": "tickets.clients.transport_for",
}
SKIPPED_DIRS = {".venv", "tests", "node_modules", "__pycache__"}


def test_every_simulator_module_is_listed_with_its_gate():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    found = set()
    for pattern in ("*_simulator.py", "simulator.py", "simulator_*.py"):
        for path in root.rglob(pattern):
            relative = path.relative_to(root)
            if not SKIPPED_DIRS & set(relative.parts):
                found.add(relative.as_posix())
    assert found == set(SIMULATOR_GATES), "a simulator was added or removed: gate it and update SIMULATOR_GATES"


# Every ticket system and its simulated address (None: never read, so nothing to gate).
TICKET_SIMULATORS = {
    "jira": tickets_simulator.JIRA_URL,
    "servicenow": tickets_simulator.SERVICENOW_URL,
    "halo": halo.SIMULATED_URL,
    "other": None,
}


def test_every_ticket_system_is_gated(simulation_off, db):
    from tickets.models import CONNECTED_SYSTEMS, TicketSystem

    assert set(TicketSystem.values) == set(TICKET_SIMULATORS), "a ticket system was added: gate it and list it"
    assert {s for s, url in TICKET_SIMULATORS.items() if url} == set(CONNECTED_SYSTEMS)
    for url in TICKET_SIMULATORS.values():
        if url is None:
            continue
        assert clients.is_simulated(url)
        with pytest.raises(clients.TicketError, match=FLAG):
            clients.transport_for(url)
