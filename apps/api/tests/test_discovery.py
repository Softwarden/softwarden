"""Discovery engine and reconciliation on the simulated Entra tenant."""

import io
import os

import pytest
from django.core.management import call_command

from catalog.models import Resource
from core import connections, credentials, graph
from core.models import AuditEvent, MicrosoftConnection
from core.problems import Conflict, FieldErrors
from discovery import engine as discovery
from discovery import entra, entra_simulator
from discovery.models import ConnectorRun, Observation, ReconciliationDecision, RunStatus
from tests.conftest import drain_jobs


def make_connector(org, actor, tested=True, **extra):
    """An Entra connector on the simulated tenant through its own Microsoft connection, tested unless asked not to
    (a run needs a connection tested at its current revision)."""
    data = {"tenant_id": "simulated", "client_id": "", "secret": "synthetic-secret", **extra}
    connector = discovery.create_connector(org, actor, data)
    if tested:
        connections.run_test(org, actor, connector.microsoft_connection)
    return connector


def obs_named(connector, name):
    return Observation.objects.get(connector=connector, display_name=name)


def test_slice_exit_check(a):
    """The backlog's slice exit check: configure, run, link, dismiss, rerun; nothing comes back, nothing is changed."""
    org, admin, editor = a["org"], a["users"]["admin"], a["users"]["editor"]
    connector = make_connector(org, admin)
    run = discovery.run(connector)
    assert run.status == RunStatus.SUCCEEDED
    # 15 objects: 4 Microsoft apps, one managed identity and one disabled app are skipped and counted.
    assert run.counts["seen"] == 15
    assert run.counts["skipped"] == {"not_application": 1, "microsoft": 4, "disabled": 1}
    assert run.counts["kept"] == run.counts["new"] == 9
    assert not Observation.objects.filter(connector=connector, display_name="Microsoft Teams").exists()

    app = a["app"]
    before = Resource.objects.values().get(id=app.id)
    discovery.link(org, editor, obs_named(connector, "Workday"), {"resource": str(app.id)})
    discovery.dismiss(org, editor, obs_named(connector, "Graph Explorer Test"), {"reason": "A developer's test"})

    second = discovery.run(connector)
    assert second.counts["new"] == 0 and second.counts["changed"] == 0 and second.counts["unchanged"] == 9
    statuses = {o.display_name: discovery.status_of(o) for o in Observation.objects.filter(connector=connector)}
    assert statuses["Workday"] == "linked" and statuses["Graph Explorer Test"] == "dismissed"
    assert sum(s in ("new", "changed") for s in statuses.values()) == 7
    after = Resource.objects.values().get(id=app.id)
    assert {k: v for k, v in after.items() if k != "updated_at"} == {
        k: v for k, v in before.items() if k != "updated_at"
    }


def test_client_only_reads(a):
    connector = make_connector(a["org"], a["users"]["admin"])
    transport = entra_simulator.Transport(connector)
    discovery.run(connector, transport=transport)
    methods = {(m, url.split("?")[0]) for m, url in transport.calls}
    assert methods == {
        ("POST", "https://login.microsoftonline.com/simulated/oauth2/v2.0/token"),
        ("GET", "https://graph.microsoft.com/v1.0/servicePrincipals"),
        ("GET", "https://graph.microsoft.com/v1.0/applications"),
    }


def test_paging_and_scale(a):
    connector = make_connector(a["org"], a["users"]["admin"])
    connector.config = {**connector.config, "simulated_size": 250}
    connector.save()
    run = discovery.run(connector)
    assert run.counts["seen"] == 265
    assert run.counts["skipped"]["disabled"] == 1 + 25  # every tenth generated app is disabled
    assert Observation.objects.filter(connector=connector).count() == run.counts["kept"] == 9 + 225


def test_scope_filters_are_counted_not_stored(a):
    connector = make_connector(
        a["org"],
        a["users"]["admin"],
        scope={"exclude_microsoft": False, "exclude_disabled": False, "name_exclusions": ["graph explorer", "Old "]},
    )
    run = discovery.run(connector)
    assert run.counts["skipped"] == {"not_application": 1, "name_exclusions": 2}
    assert Observation.objects.filter(connector=connector, display_name="Microsoft Teams").exists()


def test_change_brings_a_dismissed_candidate_back_but_not_a_linked_one(a):
    org, admin, editor = a["org"], a["users"]["admin"], a["users"]["editor"]
    connector = make_connector(org, admin)
    discovery.run(connector)
    discovery.dismiss(org, editor, obs_named(connector, "Contract Vault"), {"reason": "Legal owns it"})
    discovery.link(org, editor, obs_named(connector, "Zoom"), {"resource": str(a["app"].id)})
    # A description change isn't relevant; a rename and an owner change are.
    call_command("simulate_entra_change", "owners", "Contract Vault", "lee.chen@example.test")
    call_command("simulate_entra_change", "rename", "Zoom", "Zoom Workplace")
    connector.refresh_from_db()
    run = discovery.run(connector)
    assert run.counts["changed"] == 2
    assert discovery.status_of(obs_named(connector, "Contract Vault")) == "changed"
    zoom = obs_named(connector, "Zoom Workplace")
    assert discovery.status_of(zoom) == "linked"
    assert a["app"].name == Resource.objects.get(id=a["app"].id).name  # curated name untouched
    # Deciding again replaces the stale dismissal.
    discovery.dismiss(org, editor, obs_named(connector, "Contract Vault"), {"reason": "Still Legal's"})
    assert discovery.status_of(obs_named(connector, "Contract Vault")) == "dismissed"


def test_gone_and_back(a):
    org, admin = a["org"], a["users"]["admin"]
    connector = make_connector(org, admin)
    discovery.run(connector)
    call_command("simulate_entra_change", "remove", "Expense Hub")
    connector.refresh_from_db()
    run = discovery.run(connector)
    assert run.counts["gone"] == 1
    hub = obs_named(connector, "Expense Hub")
    assert discovery.status_of(hub) == "gone"
    call_command("simulate_entra_change", "reset")
    connector.refresh_from_db()
    discovery.run(connector)
    assert discovery.status_of(obs_named(connector, "Expense Hub")) == "new"


def test_create_record_from_candidate(a):
    org, editor = a["org"], a["users"]["editor"]
    connector = make_connector(org, a["users"]["admin"])
    discovery.run(connector)
    obs, resource = discovery.create_record(org, editor, obs_named(connector, "ServiceNow"), {})
    assert resource.name == "ServiceNow" and resource.lifecycle == "active"
    assert resource.application.documentation_url == "https://example.test/servicenow"
    assert resource.business_owner is None and resource.technical_owner is None  # owners are suggestions only
    assert ReconciliationDecision.objects.get(observation=obs).resource == resource
    assert AuditEvent.objects.filter(action="observation.created", target_id=resource.id).exists()
    # Undo removes the link; the record stays.
    discovery.reopen(org, editor, obs)
    assert Resource.objects.filter(id=resource.id).exists()
    assert discovery.status_of(Observation.objects.get(id=obs.id)) == "new"


def test_several_observations_may_link_to_one_record(a):
    org, editor = a["org"], a["users"]["editor"]
    connector = make_connector(org, a["users"]["admin"])
    discovery.run(connector)
    for name in ("Workday", "Payroll Sync Daemon"):
        discovery.link(org, editor, obs_named(connector, name), {"resource": str(a["app"].id)})
    assert ReconciliationDecision.objects.filter(resource=a["app"]).count() == 2


def test_decisions_are_checked(a, b):
    org, editor = a["org"], a["users"]["editor"]
    connector = make_connector(org, a["users"]["admin"])
    discovery.run(connector)
    obs = obs_named(connector, "Zoom")
    with pytest.raises(FieldErrors):  # another organization's record is unknown
        discovery.link(org, editor, obs, {"resource": str(b["app"].id)})
    with pytest.raises(FieldErrors):
        discovery.dismiss(org, editor, obs, {"reason": "  "})
    discovery.dismiss(org, editor, obs, {"reason": "Duplicate"})
    with pytest.raises(Conflict):
        discovery.link(org, editor, obs, {"resource": str(a["app"].id)})
    a["app"].archived_at = a["app"].created_at
    a["app"].save()
    discovery.reopen(org, editor, obs)
    with pytest.raises(FieldErrors):
        discovery.link(org, editor, obs, {"resource": str(a["app"].id)})


def test_failed_run_changes_nothing_and_is_readable(a, monkeypatch):
    org, admin = a["org"], a["users"]["admin"]
    connector = make_connector(org, admin)
    discovery.run(connector)
    count = Observation.objects.count()
    # The secret stops working at Microsoft after the test passed (revoked there): the run fails readably.
    conn = connector.microsoft_connection
    credentials.slot_set(conn, "secret", "wrong-secret")
    MicrosoftConnection.objects.filter(pk=conn.pk).update(secret_ciphertext=conn.secret_ciphertext)
    run = discovery.run(connector)
    assert run.status == RunStatus.FAILED and "client secret is wrong" in run.error and "wrong-secret" not in run.error
    assert Observation.objects.count() == count
    # A run that can't read its secret says so.
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", credentials.generate_key())
    run = discovery.run(connector)
    assert run.status == RunStatus.FAILED and "can't be read" in run.error


def test_one_run_at_a_time(a):
    connector = make_connector(a["org"], a["users"]["admin"])
    ConnectorRun.objects.create(organization=a["org"], connector=connector, trigger="manual")
    assert discovery.run(connector) is None


def test_worker_runs_requested_and_daily(a):
    from datetime import UTC, datetime

    org, admin = a["org"], a["users"]["admin"]
    connector = make_connector(org, admin, schedule="daily")
    early = datetime(2026, 11, 3, 1, 0, tzinfo=UTC)
    assert discovery.due_runs(early) == 0
    queued = discovery.request_run(org, admin, connector)  # Run now queues at once
    assert queued.status == RunStatus.QUEUED and discovery.due_runs(early) == 0
    drain_jobs()
    run = ConnectorRun.objects.get()
    assert (run.trigger, run.status, run.requested_by) == ("manual", RunStatus.SUCCEEDED, admin)
    later = datetime(2026, 11, 3, 3, 0, tzinfo=UTC)
    assert discovery.due_runs(later) == 1
    assert discovery.due_runs(later) == 0  # queued, then once a day
    drain_jobs()
    assert discovery.due_runs(later) == 0
    assert ConnectorRun.objects.filter(trigger="scheduled", status=RunStatus.SUCCEEDED).count() == 1


def test_secret_storage(a, monkeypatch):
    connector = make_connector(a["org"], a["users"]["admin"])
    stored = MicrosoftConnection.objects.get(id=connector.microsoft_connection_id).secret_ciphertext
    assert "synthetic-secret" not in stored and credentials.decrypt(stored) == "synthetic-secret"
    # Rotation: a new key first, the old one after it.
    old, new = os.environ["SOFTWARDEN_CREDENTIAL_KEYS"], credentials.generate_key()
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", f"{new},{old}")
    call_command("rotate_credential_keys", stdout=io.StringIO())
    rotated = MicrosoftConnection.objects.get(id=connector.microsoft_connection_id)
    assert rotated.secret_key_id == credentials.key_id(new)
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", new)
    assert credentials.decrypt(rotated.secret_ciphertext) == "synthetic-secret"
    # No key: secrets can't be saved.
    monkeypatch.delenv("SOFTWARDEN_CREDENTIAL_KEYS")
    with pytest.raises(FieldErrors) as e:
        discovery.update_connector(a["org"], a["users"]["admin"], connector, {"secret": "x"})
    assert "isn't configured" in e.value.errors["secret"][0]


def test_secret_never_in_audit(a):
    make_connector(a["org"], a["users"]["admin"])
    # Every event the creation wrote, the inline connection's included (that is where the secret was entered).
    # The list must not be empty, or the loop below checks nothing.
    events = list(AuditEvent.objects.filter(organization=a["org"]))
    actions = {e.action for e in events}
    assert "connector.created" in actions and any(x.endswith("connection.created") for x in actions), actions
    for event in events:
        assert "synthetic-secret" not in str(event.changes)


def test_tenant_and_hosts_are_validated(a):
    with pytest.raises(FieldErrors) as e:
        make_connector(a["org"], a["users"]["admin"], tenant_id="evil.example/../x", client_id="nope")
    assert set(e.value.errors) == {"tenant_id", "client_id"}
    assert entra.valid_tenant("contoso.onmicrosoft.com") and entra.valid_tenant("8c3dcbf0-7a3b-4f43-9a3e-1b2c3d4e5f60")
    assert not entra.valid_tenant("169.254.169.254") and not entra.valid_tenant("localhost")


def test_next_link_to_another_host_stops_the_run(a):
    connector = make_connector(a["org"], a["users"]["admin"])

    class Evil(entra_simulator.Transport):
        def send(self, method, url, headers, body=None):
            if method == "GET":
                return graph.Reply(
                    200, {}, b'{"value": [], "@odata.nextLink": "https://169.254.169.254/latest/meta-data"}'
                )
            return super().send(method, url, headers, body)

    run = discovery.run(connector, transport=Evil(connector))
    assert run.status == RunStatus.FAILED and "unexpected host" in run.error


def test_http_transport_refuses_other_hosts():
    with pytest.raises(entra.SourceError):
        entra.HttpTransport().get("https://example.test/v1.0/servicePrincipals", {})
    with pytest.raises(entra.SourceError):
        entra.HttpTransport().get("http://graph.microsoft.com/v1.0/servicePrincipals", {})


def test_suggestions(a):
    org = a["org"]
    a["people"]["dana"].email = "Dana.Ortiz@example.test"
    a["people"]["dana"].save()
    connector = make_connector(org, a["users"]["admin"])
    discovery.run(connector)
    zoom = obs_named(connector, "Zoom")
    owners = discovery.suggested_owners(org, [zoom])[zoom.id]
    assert owners == [
        {
            "name": "Dana Ortiz",
            "email": "dana.ortiz@example.test",
            "external_id": entra_simulator.DANA["id"],
            "resolved": True,
            "person": {"id": str(a["people"]["dana"].id), "name": "Dana Ortiz", "active": True},
        }
    ]
    agent = obs_named(connector, "HR Helpdesk Agent")
    assert agent.ai_agent
    a["app"].name = "Zoom Meetings"
    a["app"].save()
    assert discovery.suggested_matches(org, [zoom])[zoom.id][0]["id"] == str(a["app"].id)


def test_cross_tenant_observations_stay_apart(a, b):
    ca = make_connector(a["org"], a["users"]["admin"])
    discovery.run(ca)
    obs = obs_named(ca, "Zoom")
    with pytest.raises(Observation.DoesNotExist):
        discovery.dismiss(b["org"], b["users"]["editor"], obs, {"reason": "not ours"})


def test_needs_attention_and_digest(a, api):
    from datetime import date

    from notify import digest

    org, admin, editor = a["org"], a["users"]["admin"], a["users"]["editor"]
    connector = make_connector(org, admin, reviewer=editor.id)
    discovery.run(connector)
    body = api(editor).get("needs-attention?scope=mine").json_body
    [row] = body["discovery_candidates"]
    assert row["new"] == 9 and row["changed"] == 0 and row["steward"]["id"] == editor.id
    assert row["why"] == "9 new candidates to link, create or dismiss."
    assert body["counts"]["mine"] >= 1
    # Without a reviewer the coordinator gets it.
    discovery.update_connector(org, admin, connector, {"reviewer": None})
    row = api(admin).get("needs-attention?scope=mine").json_body["discovery_candidates"][0]
    assert row["steward"].get("coordinator")
    items, _ = digest.plan(org, date(2026, 11, 3))
    lines = [digest._line(q, r) for q, r, *_ in items[admin.id] if q == "discovery_candidates"]
    assert lines == ["Microsoft Entra ID — 9 new candidates to link, create or dismiss."]
    # Every candidate decided: the row goes.
    for obs in Observation.objects.filter(connector=connector):
        discovery.dismiss(org, editor, obs, {"reason": "Demo"})
    assert api(admin).get("needs-attention?scope=all").json_body["discovery_candidates"] == []
