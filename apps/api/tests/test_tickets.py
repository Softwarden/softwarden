"""Ticket links (tickets/): URL parsing, link CRUD on assessments and renewals, roles, tenancy, ticket
connections and their secrets, the status refresh against the simulators (every mapping, errors, timeouts and the
SSRF rules), the hourly job and the guarded create-ticket action. No real Jira, ServiceNow or Halo is contacted: the
simulated hosts (tickets/simulator.py) answer, and any other address fails the test (`no_ticket_network`)."""

import json
import logging
from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from catalog import assessments as assessments_service
from catalog import org_lifecycle
from core import capabilities as caps
from core import connections as core_connections
from core import halo, jobs, outbound
from core.graph import Unreachable
from core.models import AuditEvent, CapabilityOverride, Job, Organization
from money import renewals
from money.models import RenewalCycle
from tickets import clients, parsing, refresh, services, simulator
from tickets.models import TicketConnection, TicketLink

# Okta, Google Workspace, ServiceNow and Halo are preview features (launch scope, off by default): this module
# covers them with SOFTWARDEN_PREVIEW_FEATURES on. tests/test_preview_features.py covers them off.
pytestmark = pytest.mark.usefixtures("preview_on")

JIRA = simulator.JIRA_URL
SNOW = simulator.SERVICENOW_URL
TOKEN = "jira-api-token-very-secret-1"  # noqa: S105 (synthetic)
PASSWORD = "snow-password-very-secret-2"  # noqa: S105 (synthetic)
HALO_SECRET = "halo-client-secret-very-secret-3"  # noqa: S105 (synthetic)
REAL_REQUEST = outbound.request  # the guard below replaces it per test; SSRF tests put it back


@pytest.fixture(autouse=True)
def no_ticket_network(monkeypatch):
    """Only the simulated hosts answer; a request to any other address through the real transport fails the test."""

    def refuse(method, url, body, headers, *args, **kwargs):
        raise AssertionError(f"a test reached for a real ticket system ({method} {url.split('?')[0]})")

    monkeypatch.setattr(outbound, "request", refuse)


def make_assessment(ctx, title="Upgrade payroll"):
    return assessments_service.create_assessment(
        ctx["org"], ctx["users"]["editor"], {"target": str(ctx["app"].id), "change_type": "change", "title": title}
    )


def make_renewal(ctx, name="Adobe Creative Cloud"):
    return renewals.create_renewal(ctx["org"], ctx["users"]["editor"], {"name": name})


def jira_connection(ctx, test=True, **extra):
    data = {"system": "jira", "base_url": JIRA, "username": "it@example.test", "secret": TOKEN, **extra}
    conn = services.create_connection(ctx["org"], ctx["users"]["admin"], data)
    if test:
        conn = services.run_test(ctx["org"], ctx["users"]["admin"], conn)
    return conn


def snow_connection(ctx, test=True, **extra):
    data = {"system": "servicenow", "base_url": SNOW, "username": "softwarden", "secret": PASSWORD, **extra}
    conn = services.create_connection(ctx["org"], ctx["users"]["admin"], data)
    if test:
        conn = services.run_test(ctx["org"], ctx["users"]["admin"], conn)
    return conn


def halo_connection(ctx, scope="read:assets read:tickets", test=True, **extra):
    org, admin = ctx["org"], ctx["users"]["admin"]
    caps.set_edition(org, "enterprise")
    org.refresh_from_db()
    data = {"name": "Halo", "base_url": halo.SIMULATED_URL, "client_id": "sw", "scope": scope, "secret": HALO_SECRET}
    base = core_connections.create_connection(org, admin, core_connections.HALO, data)
    core_connections.run_test(org, admin, base)
    conn = services.create_connection(org, admin, {"system": "halo", "halo_connection": str(base.id), **extra})
    if test:
        conn = services.run_test(org, admin, conn)
    return conn


def link_on(ctx, target, url, **data):
    return services.create_link(ctx["org"], ctx["users"]["editor"], target, {"url": url, **data})


# ---- URL parsing --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "system", "key"),
    [
        ("https://contoso.atlassian.net/browse/ITOPS-123", "jira", "ITOPS-123"),
        ("https://contoso.atlassian.net/browse/ITOPS-123?focusedCommentId=1", "jira", "ITOPS-123"),
        (
            "https://contoso.atlassian.net/jira/software/projects/ITOPS/boards/1?selectedIssue=ITOPS-9",
            "jira",
            "ITOPS-9",
        ),
        ("https://contoso.atlassian.net/jira/servicedesk/projects/HELP/queues/custom/1/HELP-42", "jira", "HELP-42"),
        ("https://jira.contoso.internal/browse/OPS_2-7", "jira", "OPS_2-7"),  # Data Center on its own host
        ("https://contoso.atlassian.net/jira/your-work", "jira", ""),
        (
            "https://contoso.service-now.com/nav_to.do?uri=incident.do?sysparm_query=number=INC0012345",
            "servicenow",
            "INC0012345",
        ),
        (
            "https://contoso.service-now.com/now/nav/ui/classic/params/target/change_request.do%3Fsysparm_query%3D"
            "number%253DCHG0030001",
            "servicenow",
            "CHG0030001",
        ),
        ("https://contoso.service-now.com/incident.do?sys_id=a1b2c3d4e5f60718293a4b5c6d7e8f90", "servicenow", ""),
        (
            "https://contoso.servicenowservices.com/sp?id=ticket&table=sc_req_item&number=RITM0010001",
            "servicenow",
            "RITM0010001",
        ),
        ("https://contoso.haloitsm.com/tickets?id=12345", "halo", "12345"),
        ("https://contoso.halopsa.com/ticket?id=77&showall=true", "halo", "77"),
        ("https://contoso.haloitsm.com/portal/ticket?id=8", "halo", "8"),
        ("https://contoso.haloservicedesk.com/tickets/991", "halo", "991"),
        ("https://contoso.haloitsm.com/dashboard", "halo", ""),
        ("https://github.com/contoso/ops/issues/12", "other", ""),
    ],
)
def test_known_url_shapes_parse_to_their_system_and_key(url, system, key):
    parsed = parsing.parse(url)
    assert (parsed.system, parsed.key) == (system, key)


def test_ip_literals_and_escapes_are_accepted():
    assert parsing.parse("https://[2001:db8::1]/browse/ITOPS-1").key == "ITOPS-1"
    assert parsing.parse("https://192.0.2.10/browse/ITOPS-1").key == "ITOPS-1"
    assert parsing.parse("https://x.atlassian.net/issues/?jql=project%20%3D%20OPS&selectedIssue=OPS-1").key == "OPS-1"


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://contoso.service-now.com/incident_list.do?sysparm_query=active=true^number=INC0012345", "INC0012345"),
        ("https://contoso.service-now.com/x.do?sysparm_query=number=INC0012345^ORnumber=INC0000002|x", "INC0012345"),
        ("https://contoso.service-now.com/now/nav/ui#{INC0012345}", "INC0012345"),
    ],
)
def test_servicenow_encoded_queries_are_accepted(url, key):
    found = parsing.parse(url)
    assert (found.system, found.key) == ("servicenow", key)


def test_intranet_hosts_with_underscores_are_accepted():
    found = parsing.parse("https://jira_prod.corp.example/browse/ITOPS-1")
    assert (found.system, found.key) == ("jira", "ITOPS-1")


def test_an_organizations_own_hosts_parse_as_their_system():
    hosts = {"help.contoso.test": "halo", "itsm.contoso.test": "servicenow", "jira.contoso.test": "jira"}
    assert parsing.parse("https://help.contoso.test/tickets?id=5", hosts).key == "5"
    assert parsing.parse("https://itsm.contoso.test/x?number=INC0000002", hosts).system == "servicenow"
    found = parsing.parse("https://jira.contoso.test/issues/?selectedIssue=ABC-1", hosts)
    assert (found.system, found.key) == ("jira", "ABC-1")
    assert parsing.parse("https://help.contoso.test/tickets?id=5").system == "other"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        None,
        42,
        "http://contoso.atlassian.net/browse/ITOPS-1",
        "javascript:alert(1)",
        "https://",
        "https:///browse/ITOPS-1",
        "https://user:pass@contoso.atlassian.net/browse/ITOPS-1",
        "https://contoso.atlassian.net/browse/ITOPS 1",
        "https://contoso.atlassian.net:99999/browse/ITOPS-1",
        "https://contoso.atlassian.net/browse/ÄÖ-1",
        "https://contoso.atlassian.net/" + "a" * 2050,
        "ftp://contoso.atlassian.net/browse/ITOPS-1",
        "https://evil.example\\.atlassian.net/browse/ITOPS-1",  # browsers open evil.example
        'https://x.atlassian.net/browse/ITOPS-1"><script>',
        "https://x.atlassian.net/browse/ITOPS-1<x>",
        "https://x.atlassian.net/browse/`ITOPS-1`",
        "https://x.atlassian.net/browse/{ITOPS-1}",
        "https://x.atlassian.net/browse/ITOPS-1^2",  # ^ | { } only after ? or #
        "https://x.atlassian.net/a|b/ITOPS-1",
        "https://x.service-now.com/incident_list.do?sysparm_query=number=INC0012345\\",
        'https://x.service-now.com/incident_list.do?q="INC0012345"',
        "https://-x.atlassian.net/browse/ITOPS-1",
        "https://x..atlassian.net/browse/ITOPS-1",
        "https://[not-an-ip]/browse/ITOPS-1",
    ],
)
def test_bad_urls_are_refused(url):
    with pytest.raises(parsing.InvalidUrl):
        parsing.parse(url)


@pytest.mark.parametrize(
    ("system", "key", "ok"),
    [
        ("jira", "ITOPS-1", True),
        ("jira", "itops-1", False),
        ("jira", "ITOPS", False),
        ("servicenow", "INC0012345", True),
        ("servicenow", "INC12", False),
        ("halo", "123", True),
        ("halo", "12a", False),
        ("other", "#42", True),
        ("other", "x\n", False),
        ("jira", "", True),
    ],
)
def test_keys_are_checked_per_system(system, key, ok):
    assert (parsing.key_problem(system, key) == "") is ok


def test_servicenow_numbers_are_read_from_their_own_table():
    assert parsing.servicenow_table("INC0000001") == "incident"
    assert parsing.servicenow_table("CHG0000001") == "change_request"
    assert parsing.servicenow_table("SCTASK0000001") == "sc_task"
    assert parsing.servicenow_table("WEIRD0000001") == "task"


# ---- links: CRUD, roles, audit --------------------------------------------------------------------------------------


def test_an_editor_links_a_pasted_jira_url_to_an_assessment(a, api):
    assessment = make_assessment(a)
    editor = api(a["users"]["editor"])
    url = "https://contoso.atlassian.net/browse/ITOPS-123"
    response = editor.post(f"assessments/{assessment.id}/ticket-links", {"url": url, "title": "  Upgrade   ticket "})
    assert response.status_code == 201, response.json_body
    body = response.json_body
    assert (body["system"], body["external_key"], body["status"], body["title"]) == (
        "jira",
        "ITOPS-123",
        "unknown",
        "Upgrade ticket",
    )
    assert body["system_label"] == "Jira" and body["status_source"] == "manual" and body["cycle"] is None
    assert body["created_by"]["id"] == a["users"]["editor"].id
    event = AuditEvent.objects.get(action="ticket_link.created")
    assert event.target_type == "TicketLink" and event.related_target_type == "ChangeAssessment"
    assert event.related_target_id == assessment.id and event.changes["external_key"] == "ITOPS-123"
    assert (event.postchange or {})["url"] == url
    # the assessment's detail lists it; the list shape doesn't carry it
    detail = editor.get(f"assessments/{assessment.id}").json_body
    assert [link["id"] for link in detail["ticket_links"]] == [body["id"]]
    assert "ticket_links" not in editor.get("assessments").json_body[0]
    # the same address twice is refused
    again = editor.post(f"assessments/{assessment.id}/ticket-links", {"url": url})
    assert again.status_code == 400 and "url" in again.json_body["errors"]


def test_viewers_read_editors_write(a, api):
    assessment = make_assessment(a)
    link = link_on(a, services.assessment_target(a["org"], assessment.id), "https://x.atlassian.net/browse/A-1")
    viewer = api(a["users"]["viewer"])
    base = f"assessments/{assessment.id}/ticket-links"
    assert viewer.get(base).status_code == 200
    assert viewer.get(f"{base}/{link.id}").json_body["external_key"] == "A-1"
    assert viewer.post(base, {"url": "https://x.atlassian.net/browse/A-2"}).status_code == 403
    assert viewer.patch(f"{base}/{link.id}", {"status": "closed"}).status_code == 403
    assert viewer.delete(f"{base}/{link.id}").status_code == 403
    assert viewer.post(f"{base}/{link.id}/refresh").status_code == 403
    assert viewer.post(f"{base}/create-ticket", {"system": "jira"}).status_code == 403
    assert viewer.get("ticket-connections").status_code == 403
    assert api(a["users"]["editor"]).get("ticket-connections").status_code == 403
    assert api().get(base).status_code in (401, 403)


def test_editing_and_removing_a_link(a, api):
    assessment = make_assessment(a)
    editor = api(a["users"]["editor"])
    base = f"assessments/{assessment.id}/ticket-links"
    made = editor.post(base, {"url": "https://contoso.atlassian.net/browse/ITOPS-1"}).json_body
    changed = editor.patch(f"{base}/{made['id']}", {"status": "in_progress", "title": "Work"})
    assert changed.status_code == 200
    assert (changed.json_body["status"], changed.json_body["status_source"]) == ("in_progress", "manual")
    event = AuditEvent.objects.get(action="ticket_link.updated")
    assert event.changes["status"] == ["unknown", "in_progress"] and event.changes["title"] == ["", "Work"]
    # a new URL is parsed again
    moved = editor.patch(f"{base}/{made['id']}", {"url": "https://contoso.service-now.com/x?number=INC0000002"})
    assert (moved.json_body["system"], moved.json_body["external_key"]) == ("servicenow", "INC0000002")
    # an explicit system and key win over the URL
    custom = editor.patch(
        f"{base}/{made['id']}", {"url": "https://tickets.contoso.test/42", "system": "other", "external_key": "#42"}
    )
    assert (custom.json_body["system"], custom.json_body["external_key"]) == ("other", "#42")
    bad = editor.patch(f"{base}/{made['id']}", {"system": "jira", "external_key": "nope", "colour": "red"})
    assert bad.status_code == 400 and set(bad.json_body["errors"]) == {"external_key", "colour"}
    assert editor.patch(f"{base}/{made['id']}", {"status": "done"}).status_code == 400
    assert editor.delete(f"{base}/{made['id']}").status_code == 204
    assert not TicketLink.objects.exists()
    assert AuditEvent.objects.filter(action="ticket_link.deleted").count() == 1


def test_renewal_links_name_one_of_the_renewals_own_cycles(a, api):
    renewal = make_renewal(a)
    other = make_renewal(a, "Other")
    cycle = RenewalCycle.objects.get(obligation=renewal)
    other_cycle = RenewalCycle.objects.get(obligation=other)
    editor = api(a["users"]["editor"])
    base = f"renewals/{renewal.id}/ticket-links"
    url = "https://contoso.haloitsm.com/tickets?id=4"
    bad = editor.post(base, {"url": url, "cycle": str(other_cycle.id)})
    assert bad.status_code == 400 and "cycle" in bad.json_body["errors"]
    made = editor.post(base, {"url": url, "cycle": str(cycle.id), "status": "open"})
    assert made.status_code == 201 and made.json_body["cycle"] == str(cycle.id)
    assert made.json_body["status"] == "open"
    detail = editor.get(f"renewals/{renewal.id}").json_body
    assert [link["external_key"] for link in detail["ticket_links"]] == ["4"]
    assert editor.get(f"renewals/{other.id}").json_body["ticket_links"] == []
    event = AuditEvent.objects.get(action="ticket_link.created")
    assert (event.related_target_type, event.related_target_id) == ("RenewalObligation", renewal.id)
    # the renewal's history shows it
    history = editor.get(f"renewals/{renewal.id}/audit").json_body
    assert any(row.get("action") == "ticket_link.created" for row in history["results"])
    # an assessment's link can't name a cycle
    assessment = make_assessment(a)
    refused = editor.post(f"assessments/{assessment.id}/ticket-links", {"url": url, "cycle": str(cycle.id)})
    assert refused.status_code == 400 and "cycle" in refused.json_body["errors"]


def test_the_database_refuses_a_cycle_of_another_renewal(a):
    """tickets 0002: the composite key (organization, obligation, cycle) -> the cycle's (organization, obligation, id)
    holds even for a write that skips the services. A link with no cycle, or an assessment's, isn't checked."""
    renewal, other = make_renewal(a), make_renewal(a, "Other")
    cycle, other_cycle = RenewalCycle.objects.get(obligation=renewal), RenewalCycle.objects.get(obligation=other)
    url = "https://contoso.atlassian.net/browse/IT-{}"
    common = {"organization": a["org"], "system": "jira"}
    with transaction.atomic():
        TicketLink.objects.create(obligation=renewal, cycle=cycle, url=url.format(1), **common)
        TicketLink.objects.create(obligation=renewal, url=url.format(2), **common)
        TicketLink.objects.create(assessment=make_assessment(a), url=url.format(3), **common)
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")  # the keys are deferred: check them now
    with pytest.raises(IntegrityError, match="tickets_ticketlink_org_obligation_cycle_fk"):
        with transaction.atomic():
            TicketLink.objects.create(obligation=renewal, cycle=other_cycle, url=url.format(4), **common)
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_links_go_with_their_renewal(a, api):
    renewal = make_renewal(a)
    link_on(a, services.renewal_target(a["org"], renewal.id), "https://x.atlassian.net/browse/A-1")
    assert api(a["users"]["admin"]).delete(f"renewals/{renewal.id}").status_code == 204
    assert not TicketLink.objects.exists()


def test_parse_preview(a, api):
    viewer = api(a["users"]["viewer"])
    found = viewer.get("ticket-links/parse?url=https://contoso.atlassian.net/browse/ITOPS-5").json_body
    assert found == {"system": "jira", "system_label": "Jira", "external_key": "ITOPS-5"}
    bad = viewer.get("ticket-links/parse?url=http://contoso.atlassian.net/browse/ITOPS-5")
    assert bad.status_code == 400 and "url" in bad.json_body["errors"]


def test_a_null_status_goes_back_to_reading_it_from_the_system(a, api):
    """PATCH status: null undoes a status set by hand: unknown and unread, so the next read (by hand or hourly) sets
    it; audited as a status change."""
    jira_connection(a)
    assessment = make_assessment(a)
    editor = api(a["users"]["editor"])
    base = f"assessments/{assessment.id}/ticket-links"
    made = editor.post(base, {"url": f"{JIRA}/browse/ITOPS-2", "status": "resolved"}).json_body
    assert (made["status"], made["status_source"]) == ("resolved", "manual")
    cleared = editor.patch(f"{base}/{made['id']}", {"status": None})
    assert cleared.status_code == 200
    body = cleared.json_body
    assert (body["status"], body["status_checked_at"], body["external_status"]) == ("unknown", None, "")
    event = AuditEvent.objects.filter(action="ticket_link.updated").latest("created_at")
    assert event.changes["status"] == ["resolved", "unknown"]
    read = editor.post(f"{base}/{made['id']}/refresh").json_body
    assert (read["status"], read["status_source"]) == ("in_progress", "refreshed")


def test_a_reset_of_an_unknown_status_set_by_hand_is_saved_and_audited(a, api):
    """Scenario B: read, then Unknown set by hand (the read's time stays), then null: the status is unchanged, but the
    read state isn't, so the reset is saved and audited rather than dropped."""
    jira_connection(a)
    assessment = make_assessment(a)
    editor = api(a["users"]["editor"])
    base = f"assessments/{assessment.id}/ticket-links"
    made = editor.post(base, {"url": f"{JIRA}/browse/ITOPS-2"}).json_body
    read = editor.post(f"{base}/{made['id']}/refresh").json_body
    assert (read["status"], read["status_source"]) == ("in_progress", "refreshed")
    by_hand = editor.patch(f"{base}/{made['id']}", {"status": "unknown"}).json_body
    assert (by_hand["status"], by_hand["status_source"]) == ("unknown", "manual")
    assert by_hand["status_checked_at"] is not None
    events = AuditEvent.objects.filter(action="ticket_link.updated").count()
    reset = editor.patch(f"{base}/{made['id']}", {"status": None})
    assert reset.status_code == 200
    assert reset.json_body["status_checked_at"] is None
    assert TicketLink.objects.get(pk=made["id"]).status_checked_at is None  # persisted, not only answered
    assert AuditEvent.objects.filter(action="ticket_link.updated").count() == events + 1
    event = AuditEvent.objects.filter(action="ticket_link.updated").latest("created_at")
    assert event.changes["status_checked_at"][1] is None and "status" not in event.changes
    # Nothing left to reset: no write, no event.
    assert editor.patch(f"{base}/{made['id']}", {"status": None}).status_code == 200
    assert AuditEvent.objects.filter(action="ticket_link.updated").count() == events + 1


def test_a_failed_read_of_a_never_set_link_keeps_it_unknown_with_its_error(a, api):
    """Scenario A: a link whose status nobody set (unknown, manual) and whose read failed is still unknown: the SPA
    shows the error, not "set by hand". A reset clears the error and is audited."""
    jira_connection(a)
    assessment = make_assessment(a)
    editor = api(a["users"]["editor"])
    base = f"assessments/{assessment.id}/ticket-links"
    made = editor.post(base, {"url": f"{JIRA}/browse/ITOPS-404"}).json_body
    assert (made["status"], made["status_source"], made["status_checked_at"]) == ("unknown", "manual", None)
    failed = editor.post(f"{base}/{made['id']}/refresh").json_body
    assert failed["status"] == "unknown" and failed["status_error"]
    reset = editor.patch(f"{base}/{made['id']}", {"status": None}).json_body
    assert reset["status_error"] == "" and reset["status_checked_at"] is None
    event = AuditEvent.objects.filter(action="ticket_link.updated").latest("created_at")
    assert event.changes["status_error"][1] == ""


def test_every_member_sees_what_each_system_does_never_its_settings(a, api):
    viewer = api(a["users"]["viewer"])
    nothing = viewer.get("ticket-links/systems")
    assert nothing.status_code == 200
    assert nothing.json_body == [
        {"system": "jira", "label": "Jira", "reads": False, "creates": False},
        {"system": "servicenow", "label": "ServiceNow", "reads": False, "creates": False},
        {"system": "halo", "label": "HaloITSM", "reads": False, "creates": False},
    ]
    conn = jira_connection(a)  # tested, creating off
    snow_connection(a, test=False)  # not tested: reads nothing
    rows = {r["system"]: r for r in viewer.get("ticket-links/systems").json_body}
    assert (rows["jira"]["reads"], rows["jira"]["creates"]) == (True, False)
    assert (rows["servicenow"]["reads"], rows["servicenow"]["creates"]) == (False, False)
    admin = a["users"]["admin"]
    conn = services.update_connection(a["org"], admin, conn, {"create_enabled": True})
    rows = {r["system"]: r for r in viewer.get("ticket-links/systems").json_body}
    assert rows["jira"]["creates"] is False  # no project key yet
    conn = services.update_connection(a["org"], admin, conn, {"create_target": "ITOPS"})
    rows = {r["system"]: r for r in viewer.get("ticket-links/systems").json_body}
    assert rows["jira"]["creates"] is True
    assert set(rows["jira"]) == {"system", "label", "reads", "creates"}
    # turned off: neither
    services.update_connection(a["org"], admin, conn, {"enabled": False})
    rows = {r["system"]: r for r in viewer.get("ticket-links/systems").json_body}
    assert (rows["jira"]["reads"], rows["jira"]["creates"]) == (False, False)
    # without the capability: refused
    CapabilityOverride.objects.create(organization=a["org"], capability="ticket_links", effect="revoke", reason="t")
    caps.forget_organization(a["org"].pk)
    assert viewer.get("ticket-links/systems").status_code == 403


def test_a_ticket_connection_names_its_halo_connection(a, api):
    conn = halo_connection(a)
    body = api(a["users"]["admin"]).get(f"ticket-connections/{conn.id}").json_body
    assert body["halo_connection"] == {"id": str(conn.halo_connection_id), "name": "Halo"}
    jira = jira_connection(a)
    assert api(a["users"]["admin"]).get(f"ticket-connections/{jira.id}").json_body["halo_connection"] is None


def test_without_the_capability_links_are_refused_and_hidden(a, api):
    assessment = make_assessment(a)
    link_on(a, services.assessment_target(a["org"], assessment.id), "https://x.atlassian.net/browse/A-1")
    CapabilityOverride.objects.create(organization=a["org"], capability="ticket_links", effect="revoke", reason="t")
    caps.forget_organization(a["org"].pk)
    editor = api(a["users"]["editor"])
    response = editor.get(f"assessments/{assessment.id}/ticket-links")
    assert response.status_code == 403 and response.json_body["capability"] == "ticket_links"
    assert editor.get(f"assessments/{assessment.id}").json_body["ticket_links"] == []
    assert api(a["users"]["admin"]).get("ticket-connections").status_code == 403


# ---- tenancy --------------------------------------------------------------------------------------------------------


def test_another_organizations_links_are_404(a, b, api):
    theirs = make_assessment(b)
    their_link = link_on(b, services.assessment_target(b["org"], theirs.id), "https://x.atlassian.net/browse/B-1")
    their_renewal = make_renewal(b)
    ours = make_assessment(a)
    client = api(a["users"]["admin"])
    before_b = AuditEvent.objects.filter(organization=b["org"]).count()
    for url in (f"assessments/{theirs.id}/ticket-links", f"renewals/{their_renewal.id}/ticket-links"):
        assert client.get(url).status_code == 404
        assert client.post(url, {"url": "https://x.atlassian.net/browse/A-9"}).status_code == 404
        assert client.post(f"{url}/create-ticket", {"system": "jira"}).status_code == 404
    # their link under our assessment, or under theirs
    for parent in (ours.id, theirs.id):
        url = f"assessments/{parent}/ticket-links/{their_link.id}"
        assert client.get(url).status_code == 404
        assert client.patch(url, {"title": "x"}).status_code == 404
        assert client.delete(url).status_code == 404
        assert client.post(f"{url}/refresh").status_code == 404
    # their cycle on our renewal
    our_renewal = make_renewal(a)
    their_cycle = RenewalCycle.objects.get(obligation=their_renewal)
    refused = client.post(
        f"renewals/{our_renewal.id}/ticket-links",
        {"url": "https://x.atlassian.net/browse/A-1", "cycle": str(their_cycle.id)},
    )
    assert refused.status_code == 400
    their_link.refresh_from_db()
    assert their_link.title == "" and TicketLink.objects.filter(organization=a["org"]).count() == 0
    assert AuditEvent.objects.filter(organization=b["org"]).count() == before_b


def test_another_organizations_connections_are_404(a, b, api):
    theirs = jira_connection(b, test=False)
    client = api(a["users"]["admin"])
    assert client.get("ticket-connections").json_body == []
    assert client.get(f"ticket-connections/{theirs.id}").status_code == 404
    assert client.patch(f"ticket-connections/{theirs.id}", {"name": "x"}).status_code == 404
    assert client.delete(f"ticket-connections/{theirs.id}").status_code == 404
    assert client.post(f"ticket-connections/{theirs.id}/test").status_code == 404


def test_the_database_refuses_cross_organization_references(a, b):
    theirs = make_assessment(b)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            TicketLink.objects.create(organization=a["org"], assessment=theirs, system="other", url="https://x.test/1")
            connection.check_constraints()
    their_renewal = make_renewal(b)
    our_renewal = make_renewal(a)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            TicketLink.objects.create(
                organization=a["org"],
                obligation=our_renewal,
                cycle=RenewalCycle.objects.get(obligation=their_renewal),
                system="other",
                url="https://x.test/2",
            )
            connection.check_constraints()
    ours = make_assessment(a)
    with pytest.raises(IntegrityError):  # both targets at once
        with transaction.atomic():
            TicketLink.objects.create(
                organization=a["org"], assessment=ours, obligation=our_renewal, system="other", url="https://x.test/3"
            )


def test_a_halo_ticket_connection_cant_use_another_organizations_halo_connection(a, b):
    caps.set_edition(b["org"], "enterprise")
    caps.set_edition(a["org"], "enterprise")
    a["org"].refresh_from_db()
    b["org"].refresh_from_db()
    data = {"name": "Halo", "base_url": halo.SIMULATED_URL, "client_id": "c", "secret": "s"}
    theirs = core_connections.create_connection(b["org"], b["users"]["admin"], core_connections.HALO, data)
    with pytest.raises(services.FieldErrors) as refused:
        services.create_connection(a["org"], a["users"]["admin"], {"system": "halo", "halo_connection": str(theirs.id)})
    assert "halo_connection" in refused.value.errors


# ---- connections and their secrets ----------------------------------------------------------------------------------


def test_an_admin_adds_a_jira_connection_and_the_token_is_write_only(a, api, caplog):
    caplog.set_level(logging.DEBUG)
    admin = api(a["users"]["admin"])
    made = admin.post(
        "ticket-connections",
        {"system": "jira", "base_url": JIRA + "/", "username": "it@example.test", "secret": TOKEN},
    )
    assert made.status_code == 201, made.json_body
    body = made.json_body
    assert body["name"] == "Jira" and body["base_url"] == JIRA and body["simulated"] is True
    assert body["secret_set_on"] and body["test_current"] is False and body["create_enabled"] is False
    tested = admin.post(f"ticket-connections/{body['id']}/test")
    assert tested.status_code == 200 and tested.json_body["result"]["ok"] is True
    assert tested.json_body["test_current"] is True
    replaced = admin.patch(f"ticket-connections/{body['id']}", {"secret": TOKEN + "-new"})
    assert replaced.status_code == 200 and replaced.json_body["test_current"] is False  # the revision moved
    responses = json.dumps(
        [made.json_body, tested.json_body, replaced.json_body, admin.get("ticket-connections").json_body]
    )
    stored = TicketConnection.objects.get()
    events = list(AuditEvent.objects.filter(organization=a["org"]))
    assert {e.action for e in events} >= {
        "ticket_connection.created",
        "ticket_connection.tested",
        "ticket_connection.updated",
    }
    assert AuditEvent.objects.get(action="ticket_connection.updated").changes["replaced"] == ["secret"]
    audit_blob = json.dumps([[e.changes, e.prechange, e.postchange] for e in events])
    etag = replaced["ETag"]
    for value in (TOKEN, TOKEN + "-new", stored.secret_ciphertext):
        assert value not in responses and value not in audit_blob and value not in caplog.text and value not in etag
    assert "secret_ciphertext" not in audit_blob


def test_connection_validation(a, api):
    admin = api(a["users"]["admin"])
    bad = admin.post("ticket-connections", {"system": "jira", "base_url": "http://jira.example.test"})
    assert bad.status_code == 400 and set(bad.json_body["errors"]) >= {"base_url", "username", "secret"}
    for address in ("https://127.0.0.1", "https://169.254.169.254", "https://localhost", "https://u:p@jira.test"):
        refused = admin.post(
            "ticket-connections", {"system": "jira", "base_url": address, "username": "u", "secret": "s"}
        )
        assert refused.status_code == 400 and "base_url" in refused.json_body["errors"], address
    assert admin.post("ticket-connections", {"system": "trello"}).status_code == 400
    jira_connection(a, test=False)
    again = admin.post("ticket-connections", {"system": "jira", "base_url": JIRA, "username": "u", "secret": "s"})
    assert again.status_code == 400 and "system" in again.json_body["errors"]
    conn = TicketConnection.objects.get()
    assert admin.patch(f"ticket-connections/{conn.id}", {"system": "servicenow"}).status_code == 400
    assert admin.patch(f"ticket-connections/{conn.id}", {"create_target": "lower"}).status_code == 400
    assert admin.patch(f"ticket-connections/{conn.id}", {"secret": ""}).status_code == 400
    assert admin.patch(f"ticket-connections/{conn.id}", {"halo_connection": str(conn.id)}).status_code == 400
    # A Halo ticket connection needs discovery.halo (every edition has it; a platform revoke takes it away)
    CapabilityOverride.objects.create(organization=a["org"], capability="discovery.halo", effect="revoke", reason="t")
    caps.forget_organization(a["org"].pk)
    halo_refused = admin.post("ticket-connections", {"system": "halo", "halo_connection": str(conn.id)})
    assert halo_refused.status_code == 403 and halo_refused.json_body["capability"] == "discovery.halo"
    assert admin.delete(f"ticket-connections/{conn.id}").status_code == 204
    assert AuditEvent.objects.filter(action="ticket_connection.deleted").count() == 1


@pytest.mark.parametrize(
    ("secret", "ok", "problem"),
    [(TOKEN, True, ""), (simulator.WRONG_SECRET, False, "wrong_credentials")],
)
def test_the_jira_and_servicenow_connection_tests(a, secret, ok, problem):
    for make in (jira_connection, snow_connection):
        conn = make(a, secret=secret)
        assert (conn.probe.ok, conn.probe.problem) == (ok, problem)
        assert conn.test_current is ok
        conn.delete()


def test_the_halo_ticket_connection_needs_a_ticket_scope(a):
    conn = halo_connection(a, scope="read:assets")
    assert conn.probe.problem == "missing_permission" and not conn.test_current
    TicketConnection.objects.all().delete()
    core_connections.HaloConnection.objects.all().delete()
    conn = halo_connection(a)
    assert conn.probe.ok and conn.test_current
    assert conn.halo_connection is not None


def test_a_test_against_a_refused_address_records_why(a, monkeypatch):
    conn = jira_connection(a, test=False)
    TicketConnection.objects.filter(pk=conn.pk).update(base_url="https://jira.contoso.test")
    monkeypatch.setattr(clients, "transport_for", lambda base_url: clients.HttpTransport())
    monkeypatch.setattr(outbound, "request", REAL_REQUEST)
    monkeypatch.setattr(outbound, "_resolve", lambda host, port: ["127.0.0.1"])  # DNS rebinding to loopback
    tested = services.run_test(a["org"], a["users"]["admin"], TicketConnection.objects.get())
    assert tested.probe.problem == "refused" and not tested.test_current


# ---- status refresh -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "status", "label"),
    [
        ("ITOPS-1", "open", "To Do"),
        ("ITOPS-2", "in_progress", "In Progress"),
        ("ITOPS-3", "closed", "Done"),
        ("ITOPS-4", "resolved", "Resolved"),
        ("ITOPS-5", "unknown", "Parked"),
    ],
)
def test_jira_statuses_map_to_the_normalised_set(a, key, status, label):
    jira_connection(a)
    target = services.assessment_target(a["org"], make_assessment(a).id)
    link = link_on(a, target, f"{JIRA}/browse/{key}")
    link = services.refresh_link(a["org"], a["users"]["editor"], link)
    assert (link.status, link.external_status, link.status_source, link.status_error) == (
        status,
        label,
        "refreshed",
        "",
    )
    assert link.status_checked_at is not None and link.title  # the title fills in when the link had none


@pytest.mark.parametrize(
    ("number", "status", "label"),
    [
        ("INC0000001", "open", "New"),
        ("INC0000002", "in_progress", "In Progress"),
        ("INC0000003", "in_progress", "On Hold"),
        ("INC0000006", "resolved", "Resolved"),
        ("INC0000007", "closed", "Closed"),
        ("INC0000008", "closed", "Canceled"),
        ("INC0000062", "resolved", "6"),
        ("CHG0000001", "in_progress", "Scheduled"),
        ("RITM0000001", "closed", "Closed Complete"),
        ("INC0000099", "unknown", "Waiting for Godot"),
    ],
)
def test_servicenow_states_map_to_the_normalised_set(a, number, status, label):
    snow_connection(a)
    target = services.renewal_target(a["org"], make_renewal(a).id)
    link = link_on(a, target, f"{SNOW}/x.do?sysparm_query=number={number}")
    assert link.system == "servicenow" and link.external_key == number  # the connection's host is known
    link = services.refresh_link(a["org"], a["users"]["editor"], link)
    assert (link.status, link.external_status, link.status_error) == (status, label, "")


@pytest.mark.parametrize(
    ("ticket", "status", "label"),
    [("1", "open", "New"), ("2", "in_progress", "Status 2"), ("9", "closed", "Closed"), ("5", "resolved", "Resolved")],
)
def test_halo_statuses_map_to_the_normalised_set(a, ticket, status, label):
    halo_connection(a)
    target = services.assessment_target(a["org"], make_assessment(a).id)
    link = link_on(a, target, f"{halo.SIMULATED_URL}/tickets?id={ticket}")
    assert link.system == "halo"
    link = services.refresh_link(a["org"], a["users"]["editor"], link)
    assert (link.status, link.external_status, link.status_error) == (status, label, "")


@pytest.mark.parametrize(
    ("number", "words"),
    [
        (404, "has no"),
        (403, "refused access"),
        (500, "busy or unavailable (500)"),
        (429, "busy or unavailable (429)"),
        (408, "didn't answer in time"),
        (422, "not with the expected JSON"),
    ],
)
def test_failed_reads_keep_the_status_and_record_why_in_our_words(a, caplog, number, words):
    caplog.set_level(logging.DEBUG)
    jira_connection(a)
    target = services.assessment_target(a["org"], make_assessment(a).id)
    link = link_on(a, target, f"{JIRA}/browse/ITOPS-{number}", status="in_progress")
    link = services.refresh_link(a["org"], a["users"]["editor"], link)
    assert link.status == "in_progress" and words in link.status_error
    assert "Basic " not in link.status_error and "seen_authorization" not in link.status_error
    assert TOKEN not in caplog.text and not AuditEvent.objects.filter(action="ticket_link.status_changed").exists()


def test_a_status_change_is_audited_once(a, api):
    jira_connection(a)
    assessment = make_assessment(a)
    link = link_on(a, services.assessment_target(a["org"], assessment.id), f"{JIRA}/browse/ITOPS-2")
    editor = api(a["users"]["editor"])
    url = f"assessments/{assessment.id}/ticket-links/{link.id}/refresh"
    first = editor.post(url)
    assert first.status_code == 200 and first.json_body["status"] == "in_progress"
    editor.post(url)
    events = AuditEvent.objects.filter(action="ticket_link.status_changed")
    assert events.count() == 1 and events.get().changes["status"] == ["unknown", "in_progress"]
    assert events.get().actor == a["users"]["editor"]


def test_refresh_needs_a_usable_connection_a_key_and_the_same_site(a, api):
    assessment = make_assessment(a)
    target = services.assessment_target(a["org"], assessment.id)
    editor = api(a["users"]["editor"])
    other = link_on(a, target, "https://github.com/x/y/issues/1")
    refused = editor.post(f"assessments/{assessment.id}/ticket-links/{other.id}/refresh")
    assert refused.status_code == 409
    link = link_on(a, target, f"{JIRA}/browse/ITOPS-1")
    no_conn = editor.post(f"assessments/{assessment.id}/ticket-links/{link.id}/refresh")
    assert no_conn.status_code == 409 and "No Jira ticket connection" in no_conn.json_body["detail"]
    conn = jira_connection(a, test=False)
    untested = editor.post(f"assessments/{assessment.id}/ticket-links/{link.id}/refresh")
    assert untested.status_code == 409 and "test" in untested.json_body["detail"]
    services.run_test(a["org"], a["users"]["admin"], conn)
    elsewhere = link_on(a, target, "https://contoso.atlassian.net/browse/ITOPS-1")
    found = services.refresh_link(a["org"], a["users"]["editor"], elsewhere)
    assert "isn't on the Jira site" in found.status_error and found.status == "unknown"
    keyless = link_on(a, target, f"{JIRA}/projects")
    assert services.refresh_link(a["org"], a["users"]["editor"], keyless).status_error.startswith("This link has no")


def test_a_refused_address_at_send_time_never_connects(a, monkeypatch):
    """The connection's host now resolves to a loopback or metadata address (DNS rebinding): refused before any
    socket is opened, and recorded on the link."""
    jira_connection(a)
    TicketConnection.objects.update(base_url="https://jira.contoso.test")
    target = services.assessment_target(a["org"], make_assessment(a).id)
    link = link_on(a, target, "https://jira.contoso.test/browse/ITOPS-1")
    monkeypatch.setattr(clients, "transport_for", lambda base_url: clients.HttpTransport())
    monkeypatch.setattr(outbound, "request", REAL_REQUEST)

    def no_socket(*args, **kwargs):
        raise AssertionError("a socket was opened")

    monkeypatch.setattr(outbound.socket, "create_connection", no_socket)
    for address in ("127.0.0.1", "169.254.169.254", "::1"):
        monkeypatch.setattr(outbound, "_resolve", lambda host, port, address=address: [address])
        found = services.refresh_link(a["org"], a["users"]["editor"], link)
        assert found.status_error == outbound.SERVICE_REFUSED, address


def test_private_hosts_are_refused_on_a_hosted_install(a, settings, monkeypatch):
    settings.SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS = False
    refused = services.FieldErrors
    with pytest.raises(refused):
        services.create_connection(
            a["org"],
            a["users"]["admin"],
            {"system": "jira", "base_url": "https://10.0.0.5", "username": "u", "secret": "s"},
        )


def test_a_timeout_through_the_real_transport_is_a_timeout(a, monkeypatch):
    jira_connection(a)
    target = services.assessment_target(a["org"], make_assessment(a).id)
    link = link_on(a, target, f"{JIRA}/browse/ITOPS-1")

    class Slow:
        def send(self, method, url, headers, body=None):
            raise Unreachable("no complete answer within 20 s", ambiguous=True)

    monkeypatch.setattr(clients, "transport_for", lambda base_url: Slow())
    found = services.refresh_link(a["org"], a["users"]["editor"], link)
    assert found.status_error == "Jira didn't answer in time."


# ---- the hourly job -------------------------------------------------------------------------------------------------


def test_the_schedule_queues_one_job_per_organization_with_due_links(a, b):
    assert jobs.SCHEDULES[refresh.SCHEDULE].func is refresh.scheduled
    now = timezone.now()
    assert refresh.scheduled(now) == {"queued": 0}
    jira_connection(a)
    snow_connection(a)
    target = services.assessment_target(a["org"], make_assessment(a).id)
    link_on(a, target, f"{JIRA}/browse/ITOPS-1")
    jira_connection(b, test=False)  # untested: nothing for b
    link_on(b, services.assessment_target(b["org"], make_assessment(b).id), f"{JIRA}/browse/ITOPS-1")
    assert refresh.scheduled(now) == {"queued": 1}
    job = Job.objects.get(kind=refresh.JOB)
    assert job.organization_id == a["org"].pk and job.payload == {}
    assert refresh.scheduled(now) == {"queued": 0}  # one queued already
    Organization.objects.filter(pk=a["org"].pk).update(suspended_at=now)
    Job.objects.all().delete()
    assert refresh.scheduled(now) == {"queued": 0}


def test_the_job_reads_due_links_and_leaves_finished_ones(a):
    jira_connection(a)
    snow_connection(a)
    target = services.assessment_target(a["org"], make_assessment(a).id)
    links = [link_on(a, target, f"{JIRA}/browse/ITOPS-{n}") for n in (1, 2, 3)]
    snow = link_on(a, target, f"{SNOW}/x?number=INC0000006")
    done = link_on(a, target, f"{JIRA}/browse/ITOPS-4", status="closed")
    fresh = link_on(a, target, f"{JIRA}/browse/ITOPS-5")
    TicketLink.objects.filter(pk=fresh.pk).update(status_checked_at=timezone.now())
    counts = refresh.refresh_org(a["org"])
    assert counts == {
        **{"read": 0, "changed": 0, "failed": 0, "skipped_systems": 0, "stopped_systems": 0, "out_of_time": 0},
        "read": 4,
        "changed": 4,
    }
    statuses = dict(TicketLink.objects.values_list("external_key", "status"))
    assert statuses == {
        "ITOPS-1": "open",
        "ITOPS-2": "in_progress",
        "ITOPS-3": "closed",
        "INC0000006": "resolved",
        "ITOPS-4": "closed",
        "ITOPS-5": "unknown",
    }
    assert TicketLink.objects.get(pk=done.pk).status_checked_at is None
    events = AuditEvent.objects.filter(action="ticket_link.status_changed")
    assert events.count() == 4 and all(e.actor is None for e in events)
    # read again within the hour: nothing is due
    assert refresh.refresh_org(a["org"])["read"] == 0
    later = timezone.now() + timedelta(hours=2)
    # open, in progress and unknown are read again; closed and resolved stay as they are
    assert refresh.refresh_org(a["org"], now=later)["read"] == 3
    assert links and snow


def test_refused_credentials_stop_that_systems_reads(a):
    conn = jira_connection(a)
    target = services.assessment_target(a["org"], make_assessment(a).id)
    for n in (1, 2, 3):
        link_on(a, target, f"{JIRA}/browse/ITOPS-{n}")
    # the token is replaced behind the connection's back (as if revoked in Jira): reads answer 401
    from core import credentials

    row = TicketConnection.objects.get(pk=conn.pk)
    credentials.slot_set(row, "secret", simulator.WRONG_SECRET)
    TicketConnection.objects.filter(pk=conn.pk).update(
        secret_ciphertext=row.secret_ciphertext, secret_key_id=row.secret_key_id
    )
    counts = refresh.refresh_org(a["org"])
    assert counts["failed"] == 1 and counts["read"] == 0
    assert TicketLink.objects.filter(status_checked_at__isnull=True).count() == 2
    assert "refused the connection's credentials" in TicketLink.objects.exclude(status_error="").get().status_error


def test_the_job_skips_unusable_connections_and_the_capability(a):
    conn = jira_connection(a)
    link_on(a, services.assessment_target(a["org"], make_assessment(a).id), f"{JIRA}/browse/ITOPS-1")
    TicketConnection.objects.filter(pk=conn.pk).update(enabled=False)
    assert refresh.refresh_org(a["org"])["skipped_systems"] == 1
    TicketConnection.objects.filter(pk=conn.pk).update(enabled=True)
    CapabilityOverride.objects.create(organization=a["org"], capability="ticket_links", effect="revoke", reason="t")
    caps.forget_organization(a["org"].pk)
    assert refresh.refresh_org(a["org"]) == {
        "read": 0,
        "changed": 0,
        "failed": 0,
        "skipped_systems": 0,
        "stopped_systems": 0,
        "out_of_time": 0,
    }


def test_the_job_runs_through_the_worker(a):
    jira_connection(a)
    link_on(a, services.assessment_target(a["org"], make_assessment(a).id), f"{JIRA}/browse/ITOPS-3")
    refresh.scheduled(timezone.now())
    job = jobs.claim()
    assert job is not None and job.kind == refresh.JOB
    jobs.run(job)
    job.refresh_from_db()
    assert job.status == "succeeded" and job.result["read"] == 1
    assert TicketLink.objects.get().status == "closed"


# ---- create a ticket ------------------------------------------------------------------------------------------------


def test_creating_a_ticket_is_off_until_an_admin_turns_it_on(a, api):
    assessment = make_assessment(a)
    editor = api(a["users"]["editor"])
    url = f"assessments/{assessment.id}/ticket-links/create-ticket"
    assert editor.post(url, {"system": "jira"}).status_code == 409  # no connection
    conn = jira_connection(a)
    off = editor.post(url, {"system": "jira"})
    assert off.status_code == 409 and "turned off" in off.json_body["detail"]
    services.update_connection(a["org"], a["users"]["admin"], conn, {"create_enabled": True})
    no_project = editor.post(url, {"system": "jira"})
    assert no_project.status_code == 409 and "project key" in no_project.json_body["detail"]
    # where new tickets go isn't a sign-in value: the connection stays tested
    conn = services.update_connection(a["org"], a["users"]["admin"], conn, {"create_target": "ITOPS"})
    assert conn.test_current
    # a new token is: test again first
    conn = services.update_connection(a["org"], a["users"]["admin"], conn, {"secret": TOKEN + "-2"})
    untested = editor.post(url, {"system": "jira"})
    assert untested.status_code == 409 and "test" in untested.json_body["detail"]
    assert not TicketLink.objects.exists() and not AuditEvent.objects.filter(action__startswith="ticket_link.").exists()
    bad = editor.post(url, {"system": "trello", "extra": 1})
    assert bad.status_code == 400 and set(bad.json_body["errors"]) == {"system", "extra"}
    services.run_test(a["org"], a["users"]["admin"], conn)
    assert editor.post(url, {"system": "jira"}).status_code == 201


def test_an_editor_creates_a_jira_ticket_for_an_assessment(a, api):
    assessment = make_assessment(a)
    jira_connection(a, create_enabled=True, create_target="ITOPS", create_issue_type="Story")
    editor = api(a["users"]["editor"])
    transport = simulator.Transport()
    from tickets import clients as ticket_clients

    original = ticket_clients.transport_for
    ticket_clients.transport_for = lambda base_url: transport
    try:
        made = editor.post(f"assessments/{assessment.id}/ticket-links/create-ticket", {"system": "jira"})
    finally:
        ticket_clients.transport_for = original
    assert made.status_code == 201, made.json_body
    body = made.json_body
    assert (body["external_key"], body["url"], body["created_in_system"], body["status"]) == (
        "ITOPS-100",
        f"{JIRA}/browse/ITOPS-100",
        True,
        "open",
    )
    method, _url, payload = transport.calls[-1]
    fields = payload["fields"]
    assert method == "POST" and fields["project"] == {"key": "ITOPS"} and fields["issuetype"] == {"name": "Story"}
    assert fields["summary"] == "Upgrade payroll" and f"/assessments/{assessment.id}" in json.dumps(
        fields["description"]
    )
    event = AuditEvent.objects.get(action="ticket_link.ticket_created")
    assert event.actor == a["users"]["editor"] and event.changes["external_key"] == "ITOPS-100"


@pytest.mark.parametrize("system", ["servicenow", "halo"])
def test_servicenow_and_halo_tickets_are_created_for_a_renewal(a, api, system):
    if system == "servicenow":
        snow_connection(a, create_enabled=True)
    else:
        halo_connection(a, create_enabled=True, scope="read:assets read:tickets edit:tickets")
    renewal = make_renewal(a)
    cycle = RenewalCycle.objects.get(obligation=renewal)
    made = api(a["users"]["editor"]).post(
        f"renewals/{renewal.id}/ticket-links/create-ticket",
        {"system": system, "title": "Renew Adobe", "description": "Quote due", "cycle": str(cycle.id)},
    )
    assert made.status_code == 201, made.json_body
    expected = {"servicenow": ("INC0001001", "New"), "halo": ("5001", "New")}[system]
    assert (made.json_body["external_key"], made.json_body["external_status"]) == expected
    assert made.json_body["cycle"] == str(cycle.id) and made.json_body["title"] == "Renew Adobe"


def test_a_failed_create_is_audited_and_says_so(a, api):
    assessment = make_assessment(a)
    jira_connection(a, create_enabled=True, create_target="NOPE")
    made = api(a["users"]["editor"]).post(f"assessments/{assessment.id}/ticket-links/create-ticket", {"system": "jira"})
    assert made.status_code == 409 and made.json_body["problem"] == "not_found"
    assert not TicketLink.objects.exists()
    event = AuditEvent.objects.get(action="ticket_link.create_failed")
    assert event.target_type == "ChangeAssessment" and event.changes["ambiguous"] is False


def test_a_create_whose_answer_was_lost_warns_it_may_exist(a, api, monkeypatch):
    assessment = make_assessment(a)
    jira_connection(a, create_enabled=True, create_target="ITOPS")

    class Lost:
        def send(self, method, url, headers, body=None):
            raise Unreachable("no complete answer within 20 s", ambiguous=True)

    monkeypatch.setattr(clients, "transport_for", lambda base_url: Lost())
    made = api(a["users"]["editor"]).post(f"assessments/{assessment.id}/ticket-links/create-ticket", {"system": "jira"})
    assert made.status_code == 409 and "may have been created anyway" in made.json_body["detail"]
    assert AuditEvent.objects.get(action="ticket_link.create_failed").changes["ambiguous"] is True


# ---- export, purge, jobs: no secret leaves ------------------------------------------------------------------------


def test_the_export_leaves_the_secret_out_and_the_purge_removes_every_row(a, b):
    jira_connection(a)
    link_on(a, services.assessment_target(a["org"], make_assessment(a).id), f"{JIRA}/browse/ITOPS-1")
    link_on(b, services.assessment_target(b["org"], make_assessment(b).id), f"{JIRA}/browse/ITOPS-1")
    columns = org_lifecycle.exported_columns(TicketConnection)
    assert "secret_ciphertext" not in columns and "secret_key_id" not in columns and "username" in columns
    assert {TicketLink, TicketConnection} <= set(org_lifecycle.tenant_models())
    Organization.objects.filter(pk=a["org"].pk).update(
        deletion_requested_at=timezone.now() - timedelta(days=31), deletion_due_at=timezone.now() - timedelta(minutes=1)
    )
    assert org_lifecycle.purge(a["org"].pk) is not None
    assert not TicketLink.objects.filter(organization_id=a["org"].pk).exists()
    assert not TicketConnection.objects.filter(organization_id=a["org"].pk).exists()
    assert TicketLink.objects.filter(organization=b["org"]).count() == 1


def test_the_credential_never_appears_in_its_repr():
    credential = clients.Credential("jira", JIRA, "u", TOKEN)
    assert TOKEN not in repr(credential)


# ---- review follow-ups ----------------------------------------------------------------------------------------------


def test_the_refresh_job_runs_in_the_long_lane():
    assert jobs.JOB_KINDS[refresh.JOB].lane == jobs.LONG_LANE


class SlowTransport:
    """Every read takes `seconds` of a fake clock and answers like the simulator."""

    def __init__(self, clock, seconds, fail=None):
        self.clock, self.seconds, self.fail = clock, seconds, fail
        self.inner = simulator.Transport()
        self.reads = 0

    def post_form(self, url, form):
        self.inner.calls.append(("POST", url, None))
        return self.inner.post_form(url, form)

    def send(self, method, url, headers, body=None):
        self.clock[0] += self.seconds
        self.reads += 1
        if self.fail:
            raise Unreachable(self.fail, ambiguous=True)
        return self.inner.send(method, url, headers, body)


def test_a_slow_system_stops_the_run_within_its_budget(a, monkeypatch):
    jira_connection(a)
    target = services.assessment_target(a["org"], make_assessment(a).id)
    for n in range(1, 41):
        link_on(a, target, f"{JIRA}/browse/ITOPS-{n}")
    clock = [0.0]
    slow = SlowTransport(clock, 19.0)  # each read answers, just under the request deadline
    monkeypatch.setattr(clients, "transport_for", lambda base_url: slow)
    counts = refresh.refresh_org(a["org"], budget=180.0, clock=lambda: clock[0])
    assert counts["out_of_time"] == 1 and slow.reads == 10  # 10 x 19 s: the eleventh never starts
    assert clock[0] <= 180.0 + clients.DEADLINE
    assert TicketLink.objects.filter(status_checked_at__isnull=True).count() == 30  # read by a later run


@pytest.mark.parametrize("failure", ["no complete answer within 20 s", "ConnectionRefusedError"])
def test_a_timeout_or_no_answer_stops_that_system_after_the_first_read(a, monkeypatch, failure):
    jira_connection(a)
    snow_connection(a)
    target = services.assessment_target(a["org"], make_assessment(a).id)
    for n in (1, 2, 3):
        link_on(a, target, f"{JIRA}/browse/ITOPS-{n}")
    link_on(a, target, f"{SNOW}/x?number=INC0000002")
    clock = [0.0]
    dead = SlowTransport(clock, 20.0, fail=failure)
    live = simulator.Transport()
    monkeypatch.setattr(clients, "transport_for", lambda base_url: dead if "jira" in base_url else live)
    counts = refresh.refresh_org(a["org"], clock=lambda: clock[0])
    assert dead.reads == 1 and counts["stopped_systems"] == 1
    assert counts["read"] == 1  # ServiceNow is still read
    assert TicketLink.objects.get(external_key="INC0000002").status == "in_progress"


@pytest.mark.parametrize("status", [403, 429, 500])
def test_forbidden_and_busy_stop_that_system(a, status):
    jira_connection(a)
    target = services.assessment_target(a["org"], make_assessment(a).id)
    link_on(a, target, f"{JIRA}/browse/ITOPS-{status}")
    link_on(a, target, f"{JIRA}/browse/ITOPS-1")
    TicketLink.objects.filter(external_key="ITOPS-1").update(status_checked_at=timezone.now() - timedelta(days=1))
    counts = refresh.refresh_org(a["org"])
    assert counts["stopped_systems"] == 1 and counts["failed"] == 1 and counts["read"] == 0


def test_the_worker_stop_flag_ends_the_run(a):
    jira_connection(a)
    target = services.assessment_target(a["org"], make_assessment(a).id)
    for n in (1, 2, 3):
        link_on(a, target, f"{JIRA}/browse/ITOPS-{n}")
    calls = []

    def stopping():
        calls.append(1)
        return len(calls) > 2  # stop after the first read

    counts = refresh.refresh_org(a["org"], stopping=stopping)
    assert counts["read"] == 1 and counts["out_of_time"] == 1


def test_a_halo_run_asks_for_one_token(a, monkeypatch):
    halo_connection(a)
    target = services.assessment_target(a["org"], make_assessment(a).id)
    for ticket in (1, 2, 9):
        link_on(a, target, f"{halo.SIMULATED_URL}/tickets?id={ticket}")
    transport = simulator.Transport()
    monkeypatch.setattr(clients, "transport_for", lambda base_url: transport)
    assert refresh.refresh_org(a["org"])["read"] == 3
    assert [c[0] for c in transport.calls].count("POST") == 1  # the token, once
    assert [c[0] for c in transport.calls].count("GET") == 3


def test_deleting_a_halo_connection_a_ticket_connection_uses_is_refused(a, api):
    conn = halo_connection(a)
    assert conn.halo_connection is not None
    response = api(a["users"]["admin"]).delete(f"connections/halo/{conn.halo_connection_id}")
    assert response.status_code == 409
    assert response.json_body["ticket_connections"] == [{"id": str(conn.id), "name": conn.name}]
    assert core_connections.HaloConnection.objects.filter(pk=conn.halo_connection_id).exists()


def test_another_ticket_resets_the_status_and_is_read_again(a):
    jira_connection(a)
    assessment = make_assessment(a)
    target = services.assessment_target(a["org"], assessment.id)
    link = link_on(a, target, f"{JIRA}/browse/ITOPS-3")
    link = services.refresh_link(a["org"], a["users"]["editor"], link)
    assert link.status == "closed" and link.external_status == "Done"
    editor = a["users"]["editor"]
    link = services.update_link(a["org"], editor, target, link, {"url": f"{JIRA}/browse/ITOPS-2"})
    link.refresh_from_db()
    assert (link.external_key, link.status, link.status_source, link.external_status) == (
        "ITOPS-2",
        "unknown",
        "manual",
        "",
    )
    assert link.status_checked_at is None and link.status_error == ""
    assert refresh.refresh_org(a["org"])["read"] == 1
    link.refresh_from_db()
    assert link.status == "in_progress"
    # a new key alone, or a new system, resets it too; a sent status wins; a title change keeps it
    link = services.update_link(a["org"], editor, target, link, {"title": "Renamed"})
    assert link.status == "in_progress"
    link = services.update_link(a["org"], editor, target, link, {"external_key": "ITOPS-4"})
    assert link.status == "unknown"
    link = services.update_link(a["org"], editor, target, link, {"external_key": "ITOPS-5", "status": "open"})
    assert (link.status, link.status_source) == ("open", "manual")
    link = services.update_link(a["org"], editor, target, link, {"system": "other"})
    assert link.status == "unknown"
    # the same key on another site is another ticket
    link = services.update_link(
        a["org"], editor, target, link, {"url": f"{JIRA}/browse/ITOPS-2", "status": "in_progress"}
    )
    assert link.status == "in_progress"
    link = services.update_link(a["org"], editor, target, link, {"url": "https://other.atlassian.net/browse/ITOPS-2"})
    assert (link.external_key, link.status) == ("ITOPS-2", "unknown")


def _created(key, url=None):
    return clients.Created(key, url or f"{JIRA}/browse/{key}", clients.TicketState("open", "", "x"))


@pytest.mark.parametrize("key", ["ITOPS-" + "1" * 120, "not a key", ""])
def test_a_created_ticket_with_a_key_we_cant_store_is_audited_and_named(a, api, monkeypatch, key):
    assessment = make_assessment(a)
    jira_connection(a, create_enabled=True, create_target="ITOPS")
    monkeypatch.setattr(clients, "create", lambda credential, **kw: _created(key))
    made = api(a["users"]["editor"]).post(f"assessments/{assessment.id}/ticket-links/create-ticket", {"system": "jira"})
    assert made.status_code == 409 and made.json_body["problem"] == "not_linked"
    assert made.json_body["external_key"] == key[:100] and "Link it by hand" in made.json_body["detail"]
    event = AuditEvent.objects.get(action="ticket_link.create_failed")
    assert event.changes["created"] is True and event.changes["external_key"] == key[:100]
    assert not TicketLink.objects.exists()


def test_a_created_ticket_whose_target_or_cycle_went_away_is_audited(a, monkeypatch):
    jira_connection(a, create_enabled=True, create_target="ITOPS")
    renewal = make_renewal(a)
    cycle = RenewalCycle.objects.get(obligation=renewal)
    target = services.renewal_target(a["org"], renewal.id)

    def create_then_delete_cycle(credential, **kw):
        RenewalCycle.objects.filter(pk=cycle.pk).delete()  # deleted while the system was creating the ticket
        return _created("ITOPS-100")

    monkeypatch.setattr(clients, "create", create_then_delete_cycle)
    with pytest.raises(services.Conflict) as refused:
        services.create_ticket(a["org"], a["users"]["editor"], target, {"system": "jira", "cycle": str(cycle.id)})
    assert refused.value.extra["external_key"] == "ITOPS-100"
    assessment = make_assessment(a)
    gone = services.assessment_target(a["org"], assessment.id)

    def create_then_delete_target(credential, **kw):
        from catalog.models import AssessmentSnapshot, ChangeAssessment

        AssessmentSnapshot.objects.filter(assessment_id=assessment.id).delete()
        ChangeAssessment.objects.filter(pk=assessment.id).delete()
        return _created("ITOPS-101")

    monkeypatch.setattr(clients, "create", create_then_delete_target)
    with pytest.raises(services.Conflict):
        services.create_ticket(a["org"], a["users"]["editor"], gone, {"system": "jira"})
    events = AuditEvent.objects.filter(action="ticket_link.create_failed").order_by("created_at")
    assert [e.changes["external_key"] for e in events] == ["ITOPS-100", "ITOPS-101"]
    assert all(e.changes["created"] is True for e in events) and not TicketLink.objects.exists()


def test_a_halo_ticket_type_must_be_ascii_digits(a):
    with pytest.raises(services.FieldErrors) as refused:
        halo_connection(a, create_target="²")
    assert "create_target" in refused.value.errors
