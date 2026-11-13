"""The preview features (Okta and Google Workspace discovery, ServiceNow and Halo ticket
connections) are hidden unless the installation sets SOFTWARDEN_PREVIEW_FEATURES (off by default; core/capabilities.py
is the one place that reads it). With it off: the capabilities are gone for every organization whatever its edition
or overrides, every surface refuses or leaves them out, the schedules skip them, manual ServiceNow and Halo links still
work, and stored data is kept and used again once the setting is back on. The features themselves are covered with
the setting on in tests/test_okta_google.py and tests/test_tickets.py."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.utils import timezone

from core import capabilities as caps
from core.models import AuditEvent, CapabilityOverride, GoogleWorkspaceConnection, Job, OktaConnection
from core.problems import CapabilityRequired
from discovery import engine as discovery
from discovery.models import Connector, ConnectorRun, Observation, RunStatus
from tests.conftest import drain_jobs
from tests.test_okta_google import _pem, google_connection, make, okta_connection
from tests.test_tickets import (
    HALO_SECRET,
    JIRA,
    SNOW,
    halo_connection,
    jira_connection,
    link_on,
    make_assessment,
    make_renewal,
    no_ticket_network,  # noqa: F401 (the autouse guard: no real ticket system is contacted)
    snow_connection,
)
from tickets import refresh, services
from tickets.models import TicketConnection, TicketLink

# Code that reads the setting (an attribute of settings, or the name quoted for getattr or the environment); comments
# and docstrings that mention it don't count.
READS = re.compile(r"""settings\.SOFTWARDEN_PREVIEW_FEATURES|["']SOFTWARDEN_PREVIEW_FEATURES["']""")
PREVIEW_NAMES = {"preview", "discovery.okta", "discovery.google"}
HALO_URL = "https://contoso.haloitsm.com/tickets?id=12345"
SNOW_URL = "https://contoso.service-now.com/nav_to.do?uri=incident.do?sysparm_query=number=INC0012345"


@pytest.fixture(scope="module")
def pem() -> str:
    return _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))


def revoke_or_grant(org, name, effect):
    CapabilityOverride.objects.create(organization=org, capability=name, effect=effect, reason="test")
    caps.forget_organization(org.pk)


def problem(response) -> tuple[int, str | None]:
    return response.status_code, (response.json_body or {}).get("capability")


@pytest.fixture
def stored(a, pem, settings):
    """An enterprise organization that set up every preview feature while the setting was on: an Okta and a Google
    Workspace connector that ran (with their observations), a Jira, a ServiceNow and a Halo ticket connection, and
    links to all three systems. Then the setting goes off (the default)."""
    settings.SOFTWARDEN_PREVIEW_FEATURES = True
    org, admin = a["org"], a["users"]["admin"]
    org, _ = caps.set_edition(org, "enterprise")
    a["org"] = org
    okta_conn = okta_connection(org, admin, pem)
    google_conn = google_connection(org, admin, pem)
    okta_connector = make(org, admin, "okta", okta_conn)
    google_connector = make(org, admin, "google_workspace", google_conn)
    for connector in (okta_connector, google_connector):
        assert discovery.run(connector).status == RunStatus.SUCCEEDED
    jira = jira_connection(a)
    snow = snow_connection(a)
    halo_ticket = halo_connection(a)
    target = services.assessment_target(org, make_assessment(a).id)
    links = {
        "jira": link_on(a, target, f"{JIRA}/browse/ITOPS-1"),
        "servicenow": link_on(a, target, f"{SNOW}/x?number=INC0000006"),
        "halo": link_on(a, target, "https://halo.simulated.invalid/tickets?id=1"),
    }
    observations = Observation.objects.filter(organization=org).count()
    assert observations > 0
    settings.SOFTWARDEN_PREVIEW_FEATURES = False
    return {
        **a,
        "okta_conn": okta_conn,
        "google_conn": google_conn,
        "okta": okta_connector,
        "google": google_connector,
        "jira": jira,
        "snow": snow,
        "halo_ticket": halo_ticket,
        "target": target,
        "links": links,
        "observations": observations,
    }


# ---- capabilities ---------------------------------------------------------------------------------------------------


def test_the_setting_is_off_by_default_and_read_in_one_place():
    root = Path(__file__).resolve().parent.parent
    assert 'SOFTWARDEN_PREVIEW_FEATURES = env_flag("SOFTWARDEN_PREVIEW_FEATURES", False)' in (
        root / "softwarden" / "settings.py"
    ).read_text(encoding="utf-8")
    readers = sorted(
        str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*.py")
        if ".venv" not in path.parts and "tests" not in path.parts and READS.search(path.read_text(encoding="utf-8"))
    )
    assert readers == ["core/capabilities.py", "softwarden/settings.py"]
    assert caps.preview_features_on() is False
    assert caps.HIDDEN_WITHOUT_PREVIEW == PREVIEW_NAMES
    assert caps.PREVIEW in caps.COMMUNITY  # every edition has it while the installation turns it on


@pytest.mark.parametrize("edition", ["community", "cloud", "enterprise"])
def test_without_the_setting_no_edition_has_the_preview_features(a, edition):
    org, _ = caps.set_edition(a["org"], edition)
    have = caps.capabilities(org)
    assert not have & PREVIEW_NAMES
    absent = frozenset() if caps.ee_installed() else caps.EE_ONLY  # SAML and SCIM need the optional ee package
    assert have == caps.EDITION_CAPABILITIES[edition] - PREVIEW_NAMES - absent  # nothing else moves
    for name in PREVIEW_NAMES:
        assert caps.has_capability(org, name) is False
        with pytest.raises(CapabilityRequired) as refused:
            caps.require_capability(org, name)
        assert "a preview feature this installation hasn't turned on" in str(refused.value.detail)


def test_overrides_cant_bring_them_back_while_the_setting_is_off(a):
    org = a["org"]
    for name in PREVIEW_NAMES:
        revoke_or_grant(org, name, "grant")
    assert not caps.capabilities(org) & PREVIEW_NAMES


def test_turning_the_setting_on_gives_them_back_in_every_edition(a, settings):
    settings.SOFTWARDEN_PREVIEW_FEATURES = True
    org = a["org"]
    assert caps.capabilities(org) & PREVIEW_NAMES == PREVIEW_NAMES  # community has Okta and Google too
    org, _ = caps.set_edition(org, "enterprise")
    assert caps.capabilities(org) & PREVIEW_NAMES == PREVIEW_NAMES
    # A platform revoke of `preview` hides them for that organization alone.
    revoke_or_grant(org, "preview", "revoke")
    assert not caps.capabilities(org) & PREVIEW_NAMES
    assert "discovery.halo" in caps.capabilities(org)  # Halo discovery isn't a preview feature


def test_auth_me_reports_them_only_with_the_setting_on(a, api, settings):
    org, _ = caps.set_edition(a["org"], "enterprise")
    me = api(a["users"]["viewer"]).get("auth/me").json_body
    assert not set(me["capabilities"]) & PREVIEW_NAMES
    assert "discovery.halo" in me["capabilities"] and "ticket_links" in me["capabilities"]
    settings.SOFTWARDEN_PREVIEW_FEATURES = True
    me = api(a["users"]["viewer"]).get("auth/me").json_body
    assert set(me["capabilities"]) >= PREVIEW_NAMES
    assert me["capabilities"] == sorted(me["capabilities"])


# ---- Okta and Google Workspace discovery ----------------------------------------------------------------------------


def test_okta_and_google_connections_are_refused(stored, api, pem):
    admin = api(stored["users"]["admin"])
    for kind, conn in (("okta", stored["okta_conn"]), ("google_workspace", stored["google_conn"])):
        cap = "discovery.okta" if kind == "okta" else "discovery.google"
        assert problem(admin.get(f"connections/{kind}")) == (403, cap)
        assert problem(admin.get(f"connections/{kind}/{conn.id}")) == (403, cap)
        assert problem(admin.patch(f"connections/{kind}/{conn.id}", {"name": "Renamed"})) == (403, cap)
        assert problem(admin.post(f"connections/{kind}/{conn.id}/test")) == (403, cap)
        assert problem(admin.delete(f"connections/{kind}/{conn.id}")) == (403, cap)
        refused = admin.post(f"connections/{kind}", {"name": "New"})
        assert problem(refused) == (403, cap)
        assert "preview feature" in refused.json_body["detail"]
    assert OktaConnection.objects.filter(organization=stored["org"]).count() == 1
    assert GoogleWorkspaceConnection.objects.filter(organization=stored["org"]).count() == 1


def test_okta_and_google_connectors_cant_be_created_changed_run_or_checked(stored, api):
    admin = api(stored["users"]["admin"])
    for source, connector, conn in (
        ("okta", stored["okta"], stored["okta_conn"]),
        ("google_workspace", stored["google"], stored["google_conn"]),
    ):
        cap = caps.SOURCE_CAPABILITY[source]
        created = admin.post("connectors", {"source": source, "name": "Another", f"{source}_connection": str(conn.id)})
        assert problem(created) == (403, cap)
        assert problem(admin.patch(f"connectors/{connector.id}", {"name": "Renamed"})) == (403, cap)
        assert problem(admin.patch(f"connectors/{connector.id}", {"schedule": "daily"})) == (403, cap)
        assert problem(admin.post(f"connectors/{connector.id}/run")) == (403, cap)
        assert problem(admin.post(f"connectors/{connector.id}/access-check")) == (403, cap)
        # Whatever starts a run, it doesn't read the source.
        run = discovery.run(Connector.objects.get(pk=connector.pk))
        assert run.status == RunStatus.FAILED and cap in run.error
        assert "a preview feature this installation hasn't turned on" in run.error  # not "edition doesn't include"
    # Turning one off is still allowed, so an Admin can stop it.
    off = admin.patch(f"connectors/{stored['okta'].id}", {"enabled": False})
    assert off.status_code == 200 and off.json_body["enabled"] is False


def test_the_connector_list_keeps_them_marked_unavailable(stored, api, settings):
    viewer = api(stored["users"]["viewer"])
    entra = discovery.create_connector(
        stored["org"], stored["users"]["admin"], {"name": "Entra", "tenant_id": "simulated", "secret": "s3cret-value"}
    )
    rows = {row["source"]: row for row in viewer.get("connectors").json_body}
    assert set(rows) == {"okta", "google_workspace", "entra"}
    assert (rows["okta"]["available"], rows["google_workspace"]["available"], rows["entra"]["available"]) == (
        False,
        False,
        True,
    )
    assert viewer.get(f"connectors/{stored['okta'].id}").json_body["available"] is False
    assert viewer.get(f"connectors/{entra.id}").json_body["available"] is True
    summary = viewer.get("composite/discovery-summary").json_body
    assert {c["source"]: c["available"] for c in summary["connectors"]} == {
        "okta": False,
        "google_workspace": False,
        "entra": True,
    }
    settings.SOFTWARDEN_PREVIEW_FEATURES = True
    rows = {row["source"]: row for row in viewer.get("connectors").json_body}
    assert rows["okta"]["available"] is True and rows["google_workspace"]["available"] is True
    summary = viewer.get("composite/discovery-summary").json_body
    assert all(c["available"] for c in summary["connectors"])


def test_the_sources_leave_the_preview_ones_out(a, api, settings):
    org, _ = caps.set_edition(a["org"], "enterprise")
    viewer = api(a["users"]["viewer"])
    listed = [row["id"] for row in viewer.get("discovery/sources").json_body]
    assert "okta" not in listed and "google_workspace" not in listed
    assert {"entra", "azure", "halo"} <= set(listed)
    settings.SOFTWARDEN_PREVIEW_FEATURES = True
    sources = {row["id"]: row for row in viewer.get("discovery/sources").json_body}
    assert sources["okta"]["available"] is True and sources["google_workspace"]["available"] is True
    revoke_or_grant(org, "discovery.okta", "revoke")
    sources = {row["id"]: row for row in viewer.get("discovery/sources").json_body}
    assert sources["okta"]["available"] is False  # on, but turned off for this organization: listed as unavailable


def test_licence_usage_is_a_preview_source_but_a_connector_made_earlier_keeps_running(a, api, settings):
    """Licence usage's capability (discovery.entra) is released, but the source is offered only as a preview
    until its report has a screen: with the setting off a new connector is the preview 403 and GET discovery/sources
    leaves it out. One made while the setting was on stays available and runs."""
    org, admin = a["org"], a["users"]["admin"]
    client = api(admin)
    data = {"source": "entra_licence_usage", "name": "Usage", "tenant_id": "simulated", "secret": "s3cret-value"}
    refused = client.post("connectors", data)
    assert problem(refused) == (403, caps.PREVIEW)
    assert "a preview feature this installation hasn't turned on" in refused.json_body["detail"]
    assert not Connector.objects.filter(organization=org).exists()
    listed = [row["id"] for row in client.get("discovery/sources").json_body]
    assert "entra_licence_usage" not in listed and "entra_shadow_it" in listed  # the other Entra sources stay

    settings.SOFTWARDEN_PREVIEW_FEATURES = True
    assert "entra_licence_usage" in [row["id"] for row in client.get("discovery/sources").json_body]
    made = client.post("connectors", data)
    assert made.status_code == 201, made.json_body
    connector = Connector.objects.get(pk=made.json_body["id"])
    from core import connections

    assert connector.microsoft_connection is not None
    connections.run_test(org, admin, connector.microsoft_connection)

    settings.SOFTWARDEN_PREVIEW_FEATURES = False
    assert client.get(f"connectors/{connector.id}").json_body["available"] is True
    assert discovery.run(Connector.objects.get(pk=connector.pk)).status == RunStatus.SUCCEEDED
    assert client.post(f"connectors/{connector.id}/run").status_code == 202


def test_doctor_reports_kept_preview_connectors_as_unavailable(stored, settings):
    from core import doctor

    checks = {c.name: c for c in doctor.check_discovery(stored["org"])}
    for key in ("okta", "google"):
        check = checks[f"discovery: {stored[key].name}"]
        assert check.status == doctor.OK and check.message.startswith("Unavailable (preview feature off)")
    settings.SOFTWARDEN_PREVIEW_FEATURES = True
    checks = {c.name: c for c in doctor.check_discovery(stored["org"])}
    assert not checks[f"discovery: {stored['okta'].name}"].message.startswith("Unavailable")


def test_the_schedule_skips_okta_and_google_connectors(stored, settings):
    org, admin = stored["org"], stored["users"]["admin"]
    settings.SOFTWARDEN_PREVIEW_FEATURES = True
    for key in ("okta", "google"):
        discovery.update_connector(org, admin, Connector.objects.get(pk=stored[key].pk), {"schedule": "daily"})
    settings.SOFTWARDEN_PREVIEW_FEATURES = False
    later = datetime(2026, 11, 3, 3, 0, tzinfo=UTC)
    before = ConnectorRun.objects.count()
    assert discovery.due_runs(later) == 0
    # A run asked for the pre-Runs way isn't queued either.
    Connector.objects.filter(pk=stored["okta"].pk).update(run_requested_at=timezone.now())
    assert discovery.due_runs(later) == 0
    assert ConnectorRun.objects.count() == before
    assert not Job.objects.filter(kind="discovery.run", status="queued").exists()
    # Back on: both are due again.
    settings.SOFTWARDEN_PREVIEW_FEATURES = True
    assert discovery.due_runs(later) == 2


def test_discovery_data_is_kept_and_used_again_when_the_setting_is_back_on(stored, api, settings):
    org = stored["org"]
    admin = api(stored["users"]["admin"])
    assert problem(admin.get("connections/okta")) == (403, "discovery.okta")
    assert Connector.objects.filter(organization=org).count() == 2
    assert Observation.objects.filter(organization=org).count() == stored["observations"]
    settings.SOFTWARDEN_PREVIEW_FEATURES = True
    listed = admin.get("connections/okta")
    assert listed.status_code == 200 and [c["id"] for c in listed.json_body] == [str(stored["okta_conn"].id)]
    assert admin.get("connections/google_workspace").status_code == 200
    queued = admin.post(f"connectors/{stored['okta'].id}/run")
    assert queued.status_code == 202
    drain_jobs()
    latest = ConnectorRun.objects.filter(connector_id=stored["okta"].pk).latest("created_at")
    assert latest.status == RunStatus.SUCCEEDED


# ---- ServiceNow and Halo ticket connections -----------------------------------------------------------------------


def test_kept_ticket_connections_are_listed_unavailable_and_can_only_be_deleted(stored, api):
    admin = api(stored["users"]["admin"])
    listed = admin.get("ticket-connections")
    assert listed.status_code == 200
    assert {row["system"]: row["available"] for row in listed.json_body} == {
        "jira": True,
        "servicenow": False,
        "halo": False,
    }
    for conn in (stored["snow"], stored["halo_ticket"]):
        assert problem(admin.get(f"ticket-connections/{conn.id}")) == (403, "preview")
        assert problem(admin.patch(f"ticket-connections/{conn.id}", {"name": "Renamed"})) == (403, "preview")
        assert problem(admin.post(f"ticket-connections/{conn.id}/test")) == (403, "preview")
    assert admin.get(f"ticket-connections/{stored['jira'].id}").json_body["available"] is True
    # Deleting a kept one is allowed, and audited as kept.
    assert admin.delete(f"ticket-connections/{stored['snow'].id}").status_code == 204
    event = AuditEvent.objects.get(action="ticket_connection.deleted", target_id=stored["snow"].id)
    assert event.changes == {"name": stored["snow"].name, "system": "servicenow", "kept": True}
    assert TicketLink.objects.filter(pk=stored["links"]["servicenow"].pk).exists()  # its links stay
    TicketConnection.objects.filter(organization=stored["org"], system__in=["servicenow", "halo"]).delete()
    for body in (
        {"system": "servicenow", "base_url": SNOW, "username": "softwarden", "secret": "a-password"},
        {"system": "halo", "halo_connection": str(stored["halo_ticket"].halo_connection_id)},
    ):
        refused = admin.post("ticket-connections", body)
        assert problem(refused) == (403, "preview") and "preview feature" in refused.json_body["detail"]
    unknown = admin.post("ticket-connections", {"system": "zendesk"})
    assert unknown.status_code == 400 and unknown.json_body["errors"]["system"] == ["Choose one of jira."]


def test_the_services_refuse_them_too(stored):
    org, admin = stored["org"], stored["users"]["admin"]
    for call in (
        lambda: services.update_connection(org, admin, stored["snow"], {"name": "x"}),
        lambda: services.update_connection(org, admin, stored["halo_ticket"], {"enabled": False}),
        lambda: services.run_test(org, admin, stored["snow"]),
        lambda: services.create_connection(org, admin, {"system": "servicenow"}),
    ):
        with pytest.raises(CapabilityRequired):
            call()
    kept = services.connection_for(org, "servicenow")
    assert kept is not None
    with pytest.raises(services.NotUsable, match="preview feature"):
        services.credential_of(kept)


def test_a_kept_ticket_connection_is_listed_without_its_settings(stored, api, settings):
    from tickets.serializers import TicketConnectionSerializer

    admin = api(stored["users"]["admin"])
    shown = {"id", "system", "name", "available", "created_at", "updated_at"}
    rows = {row["system"]: row for row in admin.get("ticket-connections").json_body}
    for system, conn in (("servicenow", stored["snow"]), ("halo", stored["halo_ticket"])):
        row = rows[system]
        assert set(row) == shown | set(TicketConnectionSerializer.WITHHELD)
        assert (row["id"], row["name"], row["available"]) == (str(conn.id), conn.name, False)
        assert all(row[key] is None for key in TicketConnectionSerializer.WITHHELD), row
        assert row["created_at"] and row["updated_at"]
    jira = rows["jira"]  # a usable connection keeps every setting
    assert jira["base_url"] and jira["username"] and isinstance(jira["revision"], int) and jira["test_current"] is True
    assert jira["enabled"] is True and jira["last_test"] is not None
    # Back on: the kept ones show their settings again.
    settings.SOFTWARDEN_PREVIEW_FEATURES = True
    rows = {row["system"]: row for row in admin.get("ticket-connections").json_body}
    assert rows["servicenow"]["base_url"] == SNOW and rows["servicenow"]["username"] == "softwarden"
    assert rows["halo"]["halo_connection"]["id"] == str(stored["halo_ticket"].halo_connection_id)


def test_a_kept_halo_ticket_connection_never_blocks_its_halo_connection_for_good(stored, api):
    admin = api(stored["users"]["admin"])
    halo_id = stored["halo_ticket"].halo_connection_id
    refused = admin.delete(f"connections/halo/{halo_id}")
    assert refused.status_code == 409
    assert refused.json_body["ticket_connections"] == [
        {"id": str(stored["halo_ticket"].id), "name": stored["halo_ticket"].name}
    ]
    assert admin.delete(f"ticket-connections/{stored['halo_ticket'].id}").status_code == 204
    assert admin.delete(f"connections/halo/{halo_id}").status_code == 204


def test_another_organizations_kept_connection_is_404(stored, b, api):
    other = api(b["users"]["admin"])
    snow = stored["snow"].id
    for response in (
        other.get(f"ticket-connections/{snow}"),
        other.patch(f"ticket-connections/{snow}", {"name": "Mine"}),
        other.delete(f"ticket-connections/{snow}"),
        other.post(f"ticket-connections/{snow}/test"),
    ):
        assert response.status_code == 404
    assert TicketConnection.objects.filter(pk=snow).exists()
    assert other.get("ticket-connections").json_body == []


def test_systems_lists_jira_only(stored, api):
    rows = api(stored["users"]["viewer"]).get("ticket-links/systems").json_body
    assert rows == [{"system": "jira", "label": "Jira", "reads": True, "creates": False}]


def test_refresh_and_create_ticket_are_refused_for_servicenow_and_halo(stored, api):
    editor = api(stored["users"]["editor"])
    base = f"assessments/{stored['target'].obj.id}/ticket-links"
    for system in ("servicenow", "halo"):
        link = stored["links"][system]
        assert problem(editor.post(f"{base}/{link.id}/refresh")) == (403, "preview")
        assert problem(editor.post(f"{base}/create-ticket", {"system": system})) == (403, "preview")
    jira_link = stored["links"]["jira"]
    read = editor.post(f"{base}/{jira_link.id}/refresh")
    assert read.status_code == 200 and read.json_body["status"] == "open"
    untouched = TicketLink.objects.get(pk=stored["links"]["servicenow"].pk)
    assert untouched.status_checked_at is None and untouched.status_error == ""


def test_the_hourly_refresh_skips_servicenow_and_halo(stored, b):
    org = stored["org"]
    now = timezone.now()
    TicketLink.objects.filter(pk=stored["links"]["jira"].pk).delete()  # only ServiceNow and Halo links are due
    assert refresh.scheduled(now) == {"queued": 0}
    assert refresh.refresh_org(org)["read"] == 0
    for system in ("servicenow", "halo"):
        assert TicketLink.objects.get(pk=stored["links"][system].pk).status_checked_at is None
    # With a due Jira link the job is queued, and reads that link only.
    link_on(stored, stored["target"], f"{JIRA}/browse/ITOPS-2")
    assert refresh.scheduled(now) == {"queued": 1}
    counts = refresh.refresh_org(org)
    assert counts["read"] == 1 and counts["skipped_systems"] == 0
    for system in ("servicenow", "halo"):
        assert TicketLink.objects.get(pk=stored["links"][system].pk).status_checked_at is None


def test_manual_servicenow_and_halo_links_still_work(a, api):
    editor = api(a["users"]["editor"])
    parsed = editor.get("ticket-links/parse?url=" + quote(SNOW_URL, safe=""))
    assert parsed.status_code == 200 and parsed.json_body["system"] == "servicenow"
    assessment = make_assessment(a)
    renewal = make_renewal(a)
    made = editor.post(f"assessments/{assessment.id}/ticket-links", {"url": SNOW_URL, "status": "in_progress"})
    assert made.status_code == 201
    body = made.json_body
    assert (body["system"], body["external_key"], body["status"], body["status_source"]) == (
        "servicenow",
        "INC0012345",
        "in_progress",
        "manual",
    )
    halo_link = editor.post(f"renewals/{renewal.id}/ticket-links", {"url": HALO_URL})
    assert halo_link.status_code == 201
    assert (halo_link.json_body["system"], halo_link.json_body["external_key"]) == ("halo", "12345")
    changed = editor.patch(f"renewals/{renewal.id}/ticket-links/{halo_link.json_body['id']}", {"status": "resolved"})
    assert changed.status_code == 200 and changed.json_body["status"] == "resolved"
    assert changed.json_body["status_source"] == "manual"
    assert len(editor.get(f"assessments/{assessment.id}/ticket-links").json_body) == 1
    removed = editor.delete(f"renewals/{renewal.id}/ticket-links/{halo_link.json_body['id']}")
    assert removed.status_code == 204


def test_ticket_data_is_kept_and_used_again_when_the_setting_is_back_on(stored, api, settings):
    org = stored["org"]
    admin = api(stored["users"]["admin"])
    assert TicketConnection.objects.filter(organization=org).count() == 3
    assert TicketLink.objects.filter(organization=org).count() == 3
    settings.SOFTWARDEN_PREVIEW_FEATURES = True
    listed = admin.get("ticket-connections").json_body
    assert [(row["system"], row["available"]) for row in listed] == [
        ("halo", True),
        ("jira", True),
        ("servicenow", True),
    ]
    systems = {row["system"]: row["reads"] for row in admin.get("ticket-links/systems").json_body}
    assert systems == {"jira": True, "servicenow": True, "halo": True}
    base = f"assessments/{stored['target'].obj.id}/ticket-links"
    read = admin.post(f"{base}/{stored['links']['servicenow'].id}/refresh")
    assert read.status_code == 200 and read.json_body["status"] == "resolved"
    assert admin.get(f"ticket-connections/{stored['snow'].id}").status_code == 200


@pytest.mark.urls("tests.e2e_urls")
def test_the_e2e_route_writes_a_kept_halo_ticket_connection(a, b, api):
    from core import connections as core_connections
    from core import halo

    org, _ = caps.set_edition(a["org"], "enterprise")
    data = {
        "name": "Halo",
        "base_url": halo.SIMULATED_URL,
        "client_id": "sw",
        "scope": "read:tickets",
        "secret": HALO_SECRET,
    }
    base = core_connections.create_connection(org, a["users"]["admin"], core_connections.HALO, data)
    admin = api(a["users"]["admin"])
    assert admin.post("ticket-connections", {"system": "halo", "halo_connection": str(base.id)}).status_code == 403
    assert api(a["users"]["editor"]).post("e2e/kept-ticket-connection", {}).status_code == 403
    assert admin.post("e2e/kept-ticket-connection", {"halo_connection": str(base.id)}).status_code == 400
    other = api(b["users"]["admin"]).post("e2e/kept-ticket-connection", {"halo_connection": str(base.id), "name": "x"})
    assert other.status_code == 400  # another organization's Halo connection
    made = admin.post("e2e/kept-ticket-connection", {"halo_connection": str(base.id), "name": "Halo tickets"})
    assert made.status_code == 201
    kept = TicketConnection.objects.get(pk=made.json_body["id"])
    assert (kept.organization_id, kept.system, kept.halo_connection_id) == (org.pk, "halo", base.pk)
    listed = admin.get("ticket-connections").json_body  # kept: listed, unavailable, while preview features are off
    assert [(row["id"], row["available"]) for row in listed] == [(str(kept.id), False)]
    assert admin.post("e2e/kept-ticket-connection", {"delete": str(kept.id)}).status_code == 204
    assert not TicketConnection.objects.filter(pk=kept.pk).exists()
