"""Offboarding and access review (offboarding/).

The reference guard (every field pointing at a Person, an account or a membership is classified), a footprint with
every section, reassignment (success, audit `via`, refusals, all or nothing, roles), the access review flags and
filters, both CSVs (formula escaping, audit, impersonation) and tenancy."""

import threading
import time
from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.utils import timezone

from api.models import ApiToken
from catalog import services
from catalog.models import (
    AIAgent,
    Application,
    ChangeAssessment,
    Component,
    Integration,
    LinkConfirmation,
    Person,
    QualityException,
    RecordReview,
    Relationship,
    Resource,
    SavedFilter,
    TeamMember,
    Vendor,
)
from core.impersonation_routes import ALLOWED_WRITES, RESTRICTED_ROUTES
from core.models import AuditEvent, Membership, UserIdentity
from core.problems import FieldErrors
from discovery.models import Connector, Observation, ReconciliationDecision
from money import contracts as money_contracts
from money.models import CalendarFeed, Contract, LicensePool, RenewalObligation
from notify.models import (
    NotificationRule,
    NotificationRuleEscalationPerson,
    NotificationRulePerson,
    SavedViewSubscription,
)
from offboarding import reassign as reassign_module
from offboarding import references
from offboarding.footprint import ADMINS_ONLY, LICENCES_NOT_READ, NO_ACCOUNT
from offboarding.serializers import AccessReviewSerializer, FootprintSerializer, ReassignResultSerializer
from tests.conftest import make_org, rel_type
from tests.test_drf_resources import described
from tests.test_platform import make_staff, start
from tests.test_problems import is_problem

ENTRA = "https://login.microsoftonline.com/11111111-2222-3333-4444-555555555555/v2.0"
PRIYA_OID = "0f0e0d0c-aaaa-bbbb-cccc-000000000001"


# ---- fixtures -------------------------------------------------------------------------------------------------------


def record(org, name, kind="application", **fields):
    r = Resource.objects.create(organization=org, name=name, slug=name.lower().replace(" ", "-"), kind=kind, **fields)
    if kind == "application":
        Application.objects.create(resource=r, organization=org)
    elif kind == "component":
        Component.objects.create(resource=r, organization=org)
    return r


def integration(org, name, source, target, **fields):
    r = Resource.objects.create(
        organization=org,
        name=name,
        slug=name.lower().replace(" ", "-"),
        kind="integration",
        lifecycle="active",
        **fields,
    )
    Integration.objects.create(resource=r, organization=org, source_resource=source, target_resource=target)
    return r


def agent(org, name, run_as="", **fields):
    r = record(org, name, kind="ai_agent", lifecycle="active", **fields)
    AIAgent.objects.create(resource=r, organization=org, run_as_identity=run_as)
    return r


def observation(connector, name, owners, resource=None):
    now = timezone.now()
    obs = Observation.objects.create(
        organization=connector.organization,
        connector=connector,
        kind="application",
        external_id=name.lower(),
        display_name=name,
        data={"name": name, "owners": owners},
        fingerprint="f" * 64,
        first_seen_at=now,
        last_seen_at=now,
        last_changed_at=now,
    )
    if resource is not None:
        ReconciliationDecision.objects.create(
            organization=connector.organization, observation=obs, decision="linked", resource=resource, fingerprint="f"
        )
    return obs


def scene(a):
    """Priya, who is leaving, appears in every section; her account is the Editor's. Dana takes over: her account is
    the Admin's."""
    org, users = a["org"], a["users"]
    priya, dana = a["people"]["priya"], a["people"]["dana"]
    editor, admin = users["editor"], users["admin"]
    Person.objects.filter(id=priya.id).update(user=editor, email="priya@orga.test")
    Person.objects.filter(id=dana.id).update(user=admin, email="dana@orga.test")
    priya.refresh_from_db()
    dana.refresh_from_db()
    app = a["app"]  # its IT steward is the Editor: Priya's account
    Resource.objects.filter(id=app.id).update(business_owner=priya)
    app.refresh_from_db()
    job = record(org, "Payroll job", kind="component", lifecycle="active", technical_owner=priya)
    old = record(org, "Old CRM", lifecycle="retired", business_owner=priya, archived_at=timezone.now())
    bot = agent(org, "HR Bot", run_as=" Priya@Orga.test ")  # her email, as typed
    ops = agent(org, "Ops Agent", technical_owner=priya)
    feed = integration(org, "HR feed", app, job, technical_owner=priya)
    TeamMember.objects.create(organization=org, team=a["team"], person=priya)
    vendor = Vendor.objects.create(organization=org, name="Contoso", account_manager=priya)
    contract = Contract.objects.create(organization=org, name="Contoso MSA", vendor=vendor, renewal_owner=priya)
    LicensePool.objects.create(organization=org, application=app, contract=contract, sku_name="E3")
    renewal = RenewalObligation.objects.create(
        organization=org, name="Contoso renewal", responsible=priya, followup=editor
    )
    today = date.today()
    RecordReview.objects.create(
        organization=org,
        resource=job,
        confirmed_by=priya,
        recorded_by=admin,
        confirmation_date=today,
        next_review_date=today + timedelta(days=180),
        evidence="Call",
        confirmed_values={},
    )
    LinkConfirmation.objects.create(
        organization=org,
        integration=feed.integration,
        confirmed_by=priya,
        recorded_by=admin,
        confirmed_on=today,
        evidence="Call",
        confirmed_signature={},
    )
    assessment = ChangeAssessment.objects.create(
        organization=org, target=app, change_type="change", title="Upgrade", owner=editor
    )
    exception = QualityException.objects.create(
        organization=org, resource=app, rule="vendor", reason="Built by a friend", owner=priya, expires_on=today
    )
    rule = NotificationRule.objects.create(
        organization=org,
        name="Renewals to Priya",
        fallback_person=priya,
        source_types=["renewal_cycle"],
        lead_times_days=[30],
    )
    NotificationRulePerson.objects.create(organization=org, rule=rule, person=priya)
    connector = Connector.objects.create(organization=org, source="entra", name="Entra", reviewer=editor)
    UserIdentity.objects.create(user=editor, issuer=ENTRA, subject=PRIYA_OID.upper())
    zoom = observation(connector, "Zoom", [{"external_id": PRIYA_OID, "name": "", "email": "", "type": "user"}])
    org.coordinator = editor
    org.save()
    ApiToken.objects.create(
        organization=org, user=editor, name="Sync script", token_hash="a" * 64, prefix="sw_abc", expires_on=today
    )
    CalendarFeed.objects.create(organization=org, user=editor, token_hash="b" * 64, prefix="cal_abc")
    view = SavedFilter.objects.create(organization=org, user=editor, kind="application", name="My apps")
    membership = Membership.objects.get(organization=org, user=editor)
    SavedViewSubscription.objects.create(organization=org, saved_view=view, membership=membership, weekday=0)
    return {
        "priya": priya,
        "dana": dana,
        "app": app,
        "job": job,
        "old": old,
        "bot": bot,
        "ops": ops,
        "feed": feed,
        "vendor": vendor,
        "contract": contract,
        "renewal": renewal,
        "assessment": assessment,
        "exception": exception,
        "rule": rule,
        "connector": connector,
        "zoom": zoom,
    }


def sections(body):
    return {s["key"]: s for s in body["sections"]}


def ids(section):
    return {i["id"] for i in section["items"]}


# ---- the reference guard --------------------------------------------------------------------------------------------


def test_every_reference_to_a_person_is_classified():
    """A new foreign key or many-to-many field to Person fails here until it is placed in PERSON_REFERENCES: in a
    footprint section, or left out with a reason."""
    found = references.person_references()
    assert found == set(references.PERSON_REFERENCES), (
        f"unclassified: {sorted(found - set(references.PERSON_REFERENCES))}; "
        f"stale: {sorted(set(references.PERSON_REFERENCES) - found)}"
    )
    assert "catalog.Resource.business_owner" in found and "notify.NotificationRule.people" in found


def test_every_reference_to_an_account_or_membership_is_classified():
    found = references.account_references()
    assert found == set(references.ACCOUNT_REFERENCES), (
        f"unclassified: {sorted(found - set(references.ACCOUNT_REFERENCES))}; "
        f"stale: {sorted(set(references.ACCOUNT_REFERENCES) - found)}"
    )
    assert references.ACCOUNT_REFERENCES["catalog.Resource.it_steward"].sections == ("it_steward",)
    assert references.ACCOUNT_REFERENCES["core.AuditEvent.actor"].reason == references.HISTORY


def test_every_section_is_fed_by_a_reference_or_a_named_text_match():
    assert references.sections_fed() | set(references.TEXT_SECTIONS) == set(references.SECTIONS)
    with pytest.raises(AssertionError):
        references.Ref()  # neither in the footprint nor left out with a reason
    with pytest.raises(AssertionError):
        references.included("nowhere")


# ---- the footprint --------------------------------------------------------------------------------------------------


def test_the_footprint_covers_every_section(a, api):
    s = scene(a)
    body = api(a["users"]["admin"]).get(f"people/{s['priya'].id}/footprint").json_body
    assert body["person"]["name"] == "Priya Raman" and body["person"]["account"]["role"] == "editor"
    found = sections(body)
    assert list(found) == list(references.SECTIONS)
    for key, section in found.items():
        if key == "licences":
            assert section["available"] is False and section["reason"] == LICENCES_NOT_READ
            continue
        assert section["available"] and section["count"] >= 1, key
    assert body["total"] == sum(s_["count"] for s_ in body["sections"])

    records = {i["name"]: i for i in found["records"]["items"]}
    assert set(records) == {"orga App", "Payroll job", "Old CRM"}
    assert records["Old CRM"]["archived"] is True and records["orga App"]["roles"] == ["business_owner"]
    assert records["Payroll job"]["path"] == f"/components/{s['job'].id}"
    agents = {i["name"]: i for i in found["ai_agents"]["items"]}
    assert agents["HR Bot"]["roles"] == ["run_as"] and agents["Ops Agent"]["roles"] == ["technical_owner"]
    assert found["integrations"]["items"][0]["path"] == f"/integrations/{s['feed'].id}"
    assert ids(found["it_steward"]) == {str(s["app"].id)}
    assert found["teams"]["items"][0]["name"] == "IT Applications"
    assert ids(found["vendors"]) == {str(s["vendor"].id)}
    assert found["contracts"]["items"][0]["detail"] == "Active; 1 license pool"
    assert found["renewals"]["items"][0]["roles"] == ["responsible", "followup"]
    assert {i["type"] for i in found["reviews"]["items"]} == {"component", "integration"}
    assert ids(found["assessments"]) == {str(s["assessment"].id)}
    assert found["quality_exceptions"]["items"][0]["name"] == "orga App: No vendor"
    assert found["notification_rules"]["items"][0]["roles"] == ["audience", "fallback"]
    assert ids(found["discovery"]) == {str(s["connector"].id)}
    zoom = found["entra_owners"]["items"][0]
    assert zoom["name"] == "Zoom" and "matched by Entra object ID" in zoom["detail"] and zoom["path"] == "/discovery"
    assert {i["type"] for i in found["account"]["items"]} == {
        "membership",
        "coordinator",
        "api_token",
        "calendar_feed",
        "saved_view",
        "subscription",
    }


def test_viewers_read_it_without_the_admin_sections(a, api):
    s = scene(a)
    viewer = api(a["users"]["viewer"])
    body = viewer.get(f"people/{s['priya'].id}/footprint").json_body
    assert body["person"]["account"] is None and body["person"]["email"] == "priya@orga.test"
    found = sections(body)
    for key in ("account", "notification_rules"):
        assert found[key]["available"] is False and found[key]["reason"] == ADMINS_ONLY and found[key]["items"] == []
    assert found["records"]["count"] == 3


def test_a_person_without_an_account_and_entra_matched_by_email(a, api):
    org, dana = a["org"], a["people"]["dana"]
    Person.objects.filter(id=dana.id).update(email="Dana.Ortiz@example.test")
    connector = Connector.objects.create(organization=org, source="entra", name="Entra")
    linked = record(org, "Zoom", lifecycle="active")
    observation(
        connector,
        "Zoom",
        [{"external_id": "x1", "name": "Dana Ortiz", "email": "dana.ortiz@EXAMPLE.test", "type": "user"}],
        resource=linked,
    )
    found = sections(api(a["users"]["admin"]).get(f"people/{dana.id}/footprint").json_body)
    for key in ("it_steward", "assessments", "discovery", "account"):
        assert found[key]["available"] is False and found[key]["reason"] == NO_ACCOUNT
    [zoom] = found["entra_owners"]["items"]
    assert zoom["path"] == f"/applications/{linked.id}" and "matched by email; linked to Zoom" in zoom["detail"]


def test_capabilities_turn_sections_off(a, api, monkeypatch):
    from core import capabilities

    s = scene(a)
    org = a["org"]
    Resource.objects.filter(id=s["ops"].id).update(it_steward=a["users"]["editor"])
    # A confirmed relationship with an AI agent at one end, and a discovered app linked to an AI agent record.
    link = Relationship.objects.create(organization=org, type=rel_type(org), source=s["ops"], target=s["app"])
    LinkConfirmation.objects.create(
        organization=org,
        relationship=link,
        confirmed_by=s["priya"],
        recorded_by=a["users"]["admin"],
        confirmed_on=date.today(),
        evidence="Call",
        confirmed_signature={},
    )
    observation(
        s["connector"], "Agent app", [{"external_id": PRIYA_OID, "name": "", "email": "", "type": "user"}], s["bot"]
    )
    off = {capabilities.CONTRACTS, capabilities.AI_AGENTS}
    monkeypatch.setattr(capabilities, "capabilities", lambda org: capabilities.COMMUNITY - off)
    response = api(a["users"]["admin"]).get(f"people/{s['priya'].id}/footprint")
    found = sections(response.json_body)
    assert (
        found["contracts"]["available"] is False and found["contracts"]["reason"] == "Needs the contracts capability."
    )
    assert found["ai_agents"]["available"] is False
    assert ids(found["it_steward"]) == {str(s["app"].id)}  # the agent it stewards stays out too
    assert {i["type"] for i in found["reviews"]["items"]} == {"component", "integration"}  # not the agent's link
    agent_app = next(i for i in found["entra_owners"]["items"] if i["name"] == "Agent app")
    assert agent_app["path"] == "/discovery" and "HR Bot" not in agent_app["detail"]
    assert "Ops Agent" not in response.content.decode() and "HR Bot" not in response.content.decode()
    # Reassign neither lists nor changes the agents it can't show.
    moved = api(a["users"]["admin"]).post(
        f"people/{s['priya'].id}/reassign", {"to": str(s["dana"].id), "roles": ["technical_owner"]}
    )
    assert moved.status_code == 200 and "Ops Agent" not in {r["name"] for r in moved.json_body["rows"]}
    s["ops"].refresh_from_db()
    assert s["ops"].technical_owner_id == s["priya"].id


def test_the_footprint_csv_is_escaped_audited_and_restricted_while_impersonating(a, api):
    s = scene(a)
    Resource.objects.filter(id=s["job"].id).update(name="=HYPERLINK(evil)")
    response = api(a["users"]["viewer"]).client.get(f"/api/v1/people/{s['priya'].id}/footprint.csv")
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    lines = response.content.decode("utf-8-sig").splitlines()
    assert lines[0] == "section,section_label,type,id,name,roles,archived,detail,path"
    assert any(",'=HYPERLINK(evil)," in line for line in lines)
    assert any(line.startswith("licences,") and "not_available" in line and LICENCES_NOT_READ in line for line in lines)
    assert any(line.startswith("account,") and ADMINS_ONLY in line for line in lines)  # a Viewer's export
    event = AuditEvent.objects.get(action="export")
    assert event.changes["kind"] == "footprint_row" and event.changes["filters"] == {"person": str(s["priya"].id)}
    assert event.actor == a["users"]["viewer"] and event.changes["rows"] == len(lines) - 1  # the rows written
    assert ("GET", "api/v1/people/<uuid:pk>/footprint.csv") in RESTRICTED_ROUTES
    staff = make_staff()
    client = api(staff)
    assert start(client, a["org"], a["users"]["admin"]).status_code == 201
    url = f"/api/v1/people/{s['priya'].id}/footprint.csv"
    is_problem(client.client.get(url), 403, "/problems/impersonation-restricted")
    assert client.get(f"people/{s['priya'].id}/footprint").status_code == 200  # reading it is support work


def test_footprint_tenancy(a, b, api):
    s = scene(a)
    other = api(b["users"]["admin"])
    for url in (f"people/{s['priya'].id}/footprint", f"people/{s['priya'].id}/footprint.csv"):
        is_problem(other.get(url), 404)
    found = sections(other.get(f"people/{b['people']['priya'].id}/footprint").json_body)
    assert found["records"]["count"] == 0 and found["entra_owners"]["count"] == 0  # a's rows never show
    assert api().get(f"people/{s['priya'].id}/footprint").status_code in (401, 403)


# ---- reassign -------------------------------------------------------------------------------------------------------

ALL_ROLES = [
    "business_owner",
    "technical_owner",
    "it_steward",
    "vendor_account_manager",
    "renewal_responsible",
    "renewal_followup",
    "contract_renewal_owner",
    "quality_exception_owner",
    "notification_rules",
]


def test_a_dry_run_says_what_would_change_and_writes_nothing(a, api):
    s = scene(a)
    before = AuditEvent.objects.count()
    response = api(a["users"]["editor"]).post(
        f"people/{s['priya'].id}/reassign",
        {"to": str(s["dana"].id), "roles": ["business_owner", "technical_owner"], "dry_run": True},
    )
    assert response.status_code == 200, response.json_body
    body = response.json_body
    assert body["dry_run"] is True and body["person"]["name"] == "Priya Raman" and body["to"]["name"] == "Dana Ortiz"
    rows = {r["name"]: r for r in body["rows"]}
    assert rows["orga App"]["status"] == "planned" and rows["Old CRM"]["status"] == "skipped"
    assert "Archived" in rows["Old CRM"]["reason"] and rows["HR feed"]["roles"] == ["technical_owner"]
    assert body["changed"] == 4 and body["skipped"] == 1  # app, job, ops agent, feed; the archived CRM
    assert body["refused"] == 0
    assert AuditEvent.objects.count() == before
    s["app"].refresh_from_db()
    assert s["app"].business_owner_id == s["priya"].id
    assert described(ReassignResultSerializer(), body) == []


def test_a_dry_run_shows_what_the_real_run_would_refuse(a, api, monkeypatch):
    """The dry run runs the services in a savepoint that is rolled back, so their refusals show."""
    s = scene(a)

    def refuse(org, actor, contract, data, note=None):
        raise FieldErrors({"renewal_owner": ["Simulated refusal."]})

    monkeypatch.setattr(money_contracts, "update_contract", refuse)
    before = AuditEvent.objects.count()
    response = api(a["users"]["admin"]).post(
        f"people/{s['priya'].id}/reassign", {"to": str(s["dana"].id), "roles": ALL_ROLES, "dry_run": True}
    )
    assert response.status_code == 200, response.json_body
    body = response.json_body
    rows = {r["name"]: r for r in body["rows"]}
    assert rows["Contoso MSA"]["status"] == "refused"
    assert rows["Contoso MSA"]["field_errors"] == {"renewal_owner": ["Simulated refusal."]}
    assert rows["orga App"]["status"] == "planned" and "field_errors" not in rows["orga App"]
    assert body["refused"] == 1 and body["changed"] == 8
    assert described(ReassignResultSerializer(), body) == []
    assert AuditEvent.objects.count() == before  # the services' events were rolled back with the savepoint
    s["app"].refresh_from_db()
    assert s["app"].business_owner_id == s["priya"].id and s["app"].it_steward_id == a["users"]["editor"].id
    assert Vendor.objects.get(id=s["vendor"].id).account_manager_id == s["priya"].id


def test_an_upper_case_or_braced_id_names_the_same_person(a, api):
    s = scene(a)
    url = f"people/{s['priya'].id}/reassign"
    for to in (str(s["dana"].id).upper(), "{" + str(s["dana"].id) + "}"):
        response = api(a["users"]["editor"]).post(url, {"to": to, "roles": ["business_owner"], "dry_run": True})
        assert response.status_code == 200, (to, response.json_body)
        assert response.json_body["to"]["id"] == str(s["dana"].id)


def test_nothing_to_change_writes_no_event(a, api):
    """Only archived rows (skipped): 200, nothing changed, no person.reassigned event."""
    s = scene(a)
    Resource.objects.filter(id=s["app"].id).update(business_owner=None)
    response = api(a["users"]["editor"]).post(
        f"people/{s['priya'].id}/reassign", {"to": str(s["dana"].id), "roles": ["business_owner"]}
    )
    assert response.status_code == 200, response.json_body
    assert response.json_body["changed"] == 0 and response.json_body["skipped"] == 1  # the archived CRM
    assert not AuditEvent.objects.filter(action="person.reassigned").exists()


def test_rule_audiences_are_swapped_in_place_and_deduplicated(a, api):
    s = scene(a)
    org, priya, dana = a["org"], s["priya"], s["dana"]
    escalating = NotificationRule.objects.create(
        organization=org, name="Escalations", source_types=["contract"], lead_times_days=[30], escalation_days=[7]
    )
    NotificationRulePerson.objects.create(organization=org, rule=escalating, person=a["people"]["gone"])
    NotificationRuleEscalationPerson.objects.create(organization=org, rule=escalating, person=priya)
    NotificationRuleEscalationPerson.objects.create(organization=org, rule=escalating, person=dana)  # already there
    response = api(a["users"]["admin"]).post(
        f"people/{priya.id}/reassign", {"to": str(dana.id), "roles": ["notification_rules"]}
    )
    assert response.status_code == 200, response.json_body
    escalating.refresh_from_db()
    assert list(escalating.escalation_people.all()) == [dana]  # once, though both were on it
    assert list(escalating.people.all()) == [a["people"]["gone"]]  # an inactive person already named is kept
    first = NotificationRule.objects.get(id=s["rule"].id)
    assert list(first.people.all()) == [dana] and first.fallback_person_id == dana.id
    assert reassign_module._swap([priya.id, dana.id, a["people"]["gone"].id], priya, dana) == [
        str(dana.id),
        str(a["people"]["gone"].id),
    ]


def test_responses_match_their_serializers(a, api):
    s = scene(a)
    ReconciliationDecision.objects.create(
        organization=a["org"], observation=s["zoom"], decision="linked", resource=s["job"], fingerprint="f"
    )
    admin = api(a["users"]["admin"])
    footprint = admin.get(f"people/{s['priya'].id}/footprint").json_body
    assert described(FootprintSerializer(), footprint) == []
    review = admin.get("reports/access-review?include_retired=true").json_body
    assert described(AccessReviewSerializer(), review) == []
    assert review["results"] and any(r["entra_owners"] for r in review["results"])
    moved = admin.post(f"people/{s['priya'].id}/reassign", {"to": str(s["dana"].id), "roles": ALL_ROLES})
    assert described(ReassignResultSerializer(), moved.json_body) == []


def test_reassign_moves_every_role_with_one_audited_change_per_row(a, api):
    s = scene(a)
    priya, dana = s["priya"], s["dana"]
    response = api(a["users"]["admin"]).post(f"people/{priya.id}/reassign", {"to": str(dana.id), "roles": ALL_ROLES})
    assert response.status_code == 200, response.json_body
    body = response.json_body
    assert body["dry_run"] is False and body["skipped"] == 1
    s["app"].refresh_from_db()
    assert s["app"].business_owner_id == dana.id and s["app"].it_steward_id == a["users"]["admin"].id
    for name in ("job", "ops", "feed"):
        s[name].refresh_from_db()
        assert s[name].technical_owner_id == dana.id, name
    s["old"].refresh_from_db()
    assert s["old"].business_owner_id == priya.id  # archived: kept
    assert Vendor.objects.get(id=s["vendor"].id).account_manager_id == dana.id
    assert Contract.objects.get(id=s["contract"].id).renewal_owner_id == dana.id
    renewal = RenewalObligation.objects.get(id=s["renewal"].id)
    assert renewal.responsible_id == dana.id and renewal.followup_id == a["users"]["admin"].id
    assert QualityException.objects.get(id=s["exception"].id).owner_id == dana.id
    rule = NotificationRule.objects.get(id=s["rule"].id)
    assert list(rule.people.all()) == [dana] and rule.fallback_person_id == dana.id
    assert TeamMember.objects.filter(person=priya).exists()  # memberships aren't moved; nothing is deleted
    marked = AuditEvent.objects.filter(changes__via="offboarding")
    assert {e.action for e in marked} == {
        "resource.updated",
        "vendor.updated",
        "contract.updated",
        "renewal.updated",
        "quality_exception.updated",
        "notification_rule.updated",
        "person.reassigned",
    }
    assert all(e.changes["offboarding_person"] == str(priya.id) for e in marked)
    assert marked.filter(action="resource.updated").count() == 4  # one per record: app, job, ops agent, feed
    app_event = marked.get(action="resource.updated", target_id=s["app"].id)
    assert set(app_event.changes) >= {"business_owner", "it_steward"}  # both roles, one event
    summary = AuditEvent.objects.get(action="person.reassigned")
    assert summary.target_id == priya.id and summary.changes["reassigned_to"] == str(dana.id)
    assert summary.changes["changed"] == body["changed"] == 9  # 4 records, vendor, contract, renewal, exception, rule
    log = api(a["users"]["admin"]).get("audit-events?action=person.reassigned").json_body["results"][0]
    assert log["summary"].startswith("9 rows to Dana Ortiz (business owner, technical owner")


def test_reassign_refuses_inactive_same_unknown_and_other_org_targets(a, b, api):
    s = scene(a)
    editor = api(a["users"]["editor"])
    url = f"people/{s['priya'].id}/reassign"
    cases = {
        str(a["people"]["gone"].id): "Choose an active person; inactive people can't be owners.",
        str(s["priya"].id): "Choose someone other than the person leaving.",
        str(b["people"]["dana"].id): "Choose an existing person.",
        "not-a-uuid": "Choose the person who takes over.",
    }
    for to, message in cases.items():
        body = is_problem(editor.post(url, {"to": to, "roles": ["business_owner"]}), 400)
        assert body["errors"]["to"] == [message], to
    body = is_problem(editor.post(url, {"to": str(s["dana"].id), "roles": ["owner_of_everything"]}), 400)
    assert "Unknown role owner_of_everything" in body["errors"]["roles"][0]
    body = is_problem(editor.post(url, {"to": str(s["dana"].id), "roles": [], "extra": 1}), 400)
    assert set(body["errors"]) == {"roles", "extra"}
    anonymized = Person.objects.create(organization=a["org"], name="Gone too", anonymized_at=timezone.now())
    body = is_problem(editor.post(url, {"to": str(anonymized.id), "roles": ["business_owner"]}), 400)
    assert "active person" in body["errors"]["to"][0]
    s["app"].refresh_from_db()
    assert s["app"].business_owner_id == s["priya"].id
    assert not AuditEvent.objects.filter(changes__via="offboarding").exists()


def test_account_roles_need_the_targets_account_to_be_an_admin_or_editor(a, api):
    s = scene(a)
    priya = s["priya"]
    no_account = Person.objects.create(organization=a["org"], name="New hire")
    url = f"people/{priya.id}/reassign"
    body = is_problem(api(a["users"]["editor"]).post(url, {"to": str(no_account.id), "roles": ["it_steward"]}), 400)
    assert "no active Admin or Editor account" in body["errors"]["to"][0]
    viewer_person = Person.objects.create(organization=a["org"], name="Vic", user=a["users"]["viewer"])
    body = is_problem(
        api(a["users"]["editor"]).post(url, {"to": str(viewer_person.id), "roles": ["renewal_followup"]}), 400
    )
    assert "no active Admin or Editor account" in body["errors"]["to"][0]
    ok = api(a["users"]["editor"]).post(url, {"to": str(no_account.id), "roles": ["business_owner"]})
    assert ok.status_code == 200  # roles held on the Person need no account


def test_reassign_is_all_or_nothing(a, api, monkeypatch):
    s = scene(a)

    def refuse(org, actor, contract, data, note=None):
        raise FieldErrors({"renewal_owner": ["Simulated refusal."]})

    monkeypatch.setattr(money_contracts, "update_contract", refuse)
    before = AuditEvent.objects.count()
    response = api(a["users"]["admin"]).post(
        f"people/{s['priya'].id}/reassign", {"to": str(s["dana"].id), "roles": ALL_ROLES}
    )
    body = is_problem(response, 400)
    assert body["errors"]["roles"] == ["Some rows can't take this change. Nothing was saved."]
    [row] = body["rows"]
    assert row["name"] == "Contoso MSA" and row["field_errors"] == {"renewal_owner": ["Simulated refusal."]}
    for name in ("app", "job", "feed"):
        s[name].refresh_from_db()
    assert s["app"].business_owner_id == s["priya"].id and s["job"].technical_owner_id == s["priya"].id
    assert Vendor.objects.get(id=s["vendor"].id).account_manager_id == s["priya"].id
    assert AuditEvent.objects.count() == before


def test_reassign_roles(a, api):
    s = scene(a)
    url = f"people/{s['priya'].id}/reassign"
    body = {"to": str(s["dana"].id), "roles": ["business_owner"]}
    is_problem(api(a["users"]["viewer"]).post(url, body), 403)
    rules = {"to": str(s["dana"].id), "roles": ["notification_rules"]}
    response = api(a["users"]["editor"]).post(url, rules)
    is_problem(response, 403)
    assert "Only Admins" in response.json_body["detail"]
    assert api(a["users"]["admin"]).post(url, rules).status_code == 200
    assert ("POST", "api/v1/people/<uuid:pk>/reassign") in ALLOWED_WRITES


def test_reassign_while_impersonating_is_audited_with_the_staff_member(a, api):
    s = scene(a)
    staff = make_staff()
    client = api(staff)
    assert start(client, a["org"], a["users"]["editor"]).status_code == 201
    response = client.post(f"people/{s['priya'].id}/reassign", {"to": str(s["dana"].id), "roles": ["business_owner"]})
    assert response.status_code == 200, response.json_body
    event = AuditEvent.objects.get(action="resource.updated", target_id=s["app"].id)
    assert event.impersonated_by == staff and event.actor == a["users"]["editor"]
    assert event.changes["via"] == "offboarding"


def test_reassign_with_a_token(a):
    """An Editor's API token reassigns (CSRF doesn't apply to a bearer token); a read-only token can't."""
    from tests.test_api_lifecycle import TokenClient, problem

    s = scene(a)
    url = f"people/{s['priya'].id}/reassign"
    body = {"to": str(s["dana"].id), "roles": ["technical_owner"]}
    problem(TokenClient(a, "editor", read_only=True).call("post", url, body), 403, "/problems/read-only-token")
    response = TokenClient(a, "editor").call("post", url, body)
    assert response.status_code == 200, response.body
    s["job"].refresh_from_db()
    assert s["job"].technical_owner_id == s["dana"].id


def test_reassign_tenancy(a, b, api):
    s = scene(a)
    url = f"people/{s['priya'].id}/reassign"
    is_problem(
        api(b["users"]["admin"]).post(url, {"to": str(b["people"]["dana"].id), "roles": ["business_owner"]}), 404
    )
    s["app"].refresh_from_db()
    assert s["app"].business_owner_id == s["priya"].id


# ---- concurrency: reassign against ordinary writes -----------------------------------------------


def race(first, second):
    """Run `first` (which sets `holding` once it holds its lock) and then `second`, each in its own thread and
    connection. Returns (the order they finished in, the errors raised)."""
    done, errors = [], []

    def wrap(name, fn):
        def inner():
            try:
                fn()
                done.append(name)
            except Exception as e:  # a deadlock surfaces here as OperationalError
                errors.append((name, type(e).__name__, str(e).splitlines()[0]))
            finally:
                connection.close()

        return inner

    threads = [threading.Thread(target=wrap("edit", first)), threading.Thread(target=wrap("reassign", second))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    return done, errors


def test_reassign_waits_for_an_if_match_edit_that_sets_an_owner(transactional_db):
    """An If-Match PATCH holds the record's row (core/drf.check_if_match) and then sets Dana as technical owner, which
    takes FOR KEY SHARE on Dana's row to check the foreign key. Reassign locks both People FOR NO KEY UPDATE, which
    doesn't block that check, so it waits for the edit instead of deadlocking (it took FOR UPDATE before)."""
    f = make_org("race1")
    org, editor, admin = f["org"], f["users"]["editor"], f["users"]["admin"]
    priya, dana, app = f["people"]["priya"], f["people"]["dana"], f["app"]
    Resource.objects.filter(id=app.id).update(business_owner=priya)
    holding = threading.Event()

    def edit():
        with transaction.atomic():
            list(Resource.objects.select_for_update().filter(pk=app.pk).values_list("pk", flat=True))
            holding.set()
            time.sleep(0.8)  # reassign locks the People meanwhile and waits for the record
            services.update_resource(org, editor, Resource.objects.get(pk=app.pk), {"technical_owner": str(dana.id)})

    def go():
        holding.wait(5)
        time.sleep(0.2)
        reassign_module.reassign(org, admin, "admin", priya, {"to": str(dana.id), "roles": ["business_owner"]})

    done, errors = race(edit, go)
    assert errors == [] and done == ["edit", "reassign"]
    app.refresh_from_db()
    assert app.business_owner_id == dana.id and app.technical_owner_id == dana.id


def test_reassign_locks_contracts_before_renewals_as_contract_edits_do(transactional_db):
    """A contract edit locks the contract, then its renewal (money/contracts.update_contract, and a contract's If-Match).
    Reassign takes contracts before renewals, the same order, so it waits instead of deadlocking."""
    f = make_org("race2")
    org, editor, admin = f["org"], f["users"]["editor"], f["users"]["admin"]
    priya, dana = f["people"]["priya"], f["people"]["dana"]
    renewal = RenewalObligation.objects.create(organization=org, name="R", responsible=priya)
    contract = Contract.objects.create(
        organization=org, name="C", renewal_owner=priya, renewal_obligation=renewal, end_date=date(2027, 6, 30)
    )
    holding = threading.Event()

    def edit():
        with transaction.atomic():
            locked = money_contracts.lock(contract)
            holding.set()
            time.sleep(0.8)  # reassign starts meanwhile
            money_contracts.update_contract(org, editor, locked, {"name": "C edited"})

    def go():
        holding.wait(5)
        time.sleep(0.2)
        roles = ["renewal_responsible", "contract_renewal_owner"]
        reassign_module.reassign(org, admin, "admin", priya, {"to": str(dana.id), "roles": roles})

    done, errors = race(edit, go)
    assert errors == [] and done == ["edit", "reassign"]
    contract.refresh_from_db()
    renewal.refresh_from_db()
    assert contract.name == "C edited" and contract.renewal_owner_id == dana.id and renewal.responsible_id == dana.id


def test_two_offboardings_with_cross_linked_contracts_dont_deadlock(transactional_db, monkeypatch):
    """Each offboarding's contract is linked to the other's renewal, which update_contract locks when it applies. Both
    plan (and lock their rows) before either applies; the linked renewals are locked in the plan with the matched
    ones, in ID order, so one waits for the other instead of deadlocking."""
    f = make_org("race3")
    org, admin = f["org"], f["users"]["admin"]
    priya, dana = f["people"]["priya"], f["people"]["dana"]
    bea = Person.objects.create(organization=org, name="Bea")
    eve = Person.objects.create(organization=org, name="Eve")
    r_priya = RenewalObligation.objects.create(organization=org, name="RA", responsible=priya)
    r_dana = RenewalObligation.objects.create(organization=org, name="RD", responsible=dana)
    end = date(2027, 6, 30)
    c1 = Contract.objects.create(
        organization=org, name="C1", renewal_owner=priya, renewal_obligation=r_dana, end_date=end
    )
    c2 = Contract.objects.create(
        organization=org, name="C2", renewal_owner=dana, renewal_obligation=r_priya, end_date=end
    )
    barrier = threading.Barrier(2, timeout=3)
    original = reassign_module._apply

    def gated(rows):
        try:
            barrier.wait()  # both have planned before either applies (with the fix one waits in its plan: timeout)
        except threading.BrokenBarrierError:
            pass
        return original(rows)

    monkeypatch.setattr(reassign_module, "_apply", gated)
    roles = ["renewal_responsible", "contract_renewal_owner"]
    done, errors = [], []

    def go(name, source, target):
        def inner():
            try:
                reassign_module.reassign(org, admin, "admin", source, {"to": str(target.id), "roles": roles})
                done.append(name)
            except Exception as e:
                errors.append((name, type(e).__name__, str(e).splitlines()[0]))
            finally:
                connection.close()

        return inner

    threads = [threading.Thread(target=go("one", priya, bea)), threading.Thread(target=go("two", dana, eve))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == [] and sorted(done) == ["one", "two"]
    for row in (c1, c2, r_priya, r_dana):
        row.refresh_from_db()
    assert c1.renewal_owner_id == bea.id and r_priya.responsible_id == bea.id
    assert c2.renewal_owner_id == eve.id and r_dana.responsible_id == eve.id


# ---- access review --------------------------------------------------------------------------------------------------


def review_scene(a):
    org, people = a["org"], a["people"]
    dana, priya, gone = people["dana"], people["priya"], people["gone"]
    leaver = get_user_model().objects.create_user(username="leaver@orga.test", email="leaver@orga.test", password="x")
    Membership.objects.create(user=leaver, organization=org, role="editor")
    Person.objects.filter(id=priya.id).update(user=leaver)
    leaver.is_active = False
    leaver.save()
    Person.objects.filter(id=dana.id).update(email="dana@orga.test")
    alpha = record(org, "Alpha", lifecycle="active", business_owner=gone, technical_owner=dana)
    beta = record(org, "Beta", kind="component", lifecycle="active", technical_owner=priya)
    delta = record(org, "Delta", lifecycle="active", business_owner=dana)
    feed = integration(org, "Feed", alpha, beta)
    record(org, "Retired one", lifecycle="retired", business_owner=dana)
    record(org, "Archived one", lifecycle="active", archived_at=timezone.now())
    connector = Connector.objects.create(organization=org, source="entra", name="Entra")
    observation(
        connector,
        "Delta app",
        [
            {"external_id": "o1", "name": "Dana Ortiz", "email": "DANA@orga.test", "type": "user"},
            {"external_id": "o2", "name": "Stranger", "email": "stranger@elsewhere.test", "type": "user"},
            {"external_id": "o3", "name": "Deploy bot", "email": "", "type": "service_principal"},
        ],
        resource=delta,
    )
    observation(
        connector,
        "Alpha app",
        [{"external_id": "o1", "name": "Dana", "email": "dana@orga.test", "type": "user"}],
        alpha,
    )
    # Other sources' owners aren't compared: Power Platform gives a name only.
    power = Connector.objects.create(organization=org, source="powerplatform", name="Power Platform")
    observation(power, "Alpha flow", [{"external_id": "", "name": "Someone", "email": ""}], alpha)
    return {"alpha": alpha, "beta": beta, "delta": delta, "feed": feed}


def test_the_access_review_flags(a, api):
    review_scene(a)
    body = api(a["users"]["viewer"]).get("reports/access-review").json_body
    rows = {r["record"]["name"]: r for r in body["results"]}
    assert set(rows) == {"Alpha", "Beta", "Delta", "Feed", "orga App"}  # not archived, not retired
    assert (
        rows["Alpha"]["flags"] == ["inactive_owner"]
        and "Business owner Former person is inactive." in rows["Alpha"]["why"]
    )
    assert (
        rows["Alpha"]["business_owner"]["status"] == "inactive"
        and rows["Alpha"]["technical_owner"]["status"] == "active"
    )
    assert rows["Beta"]["flags"] == ["owner_left"] and rows["Beta"]["technical_owner"]["status"] == "left"
    assert rows["orga App"]["flags"] == ["no_owner"]
    assert rows["Feed"]["flags"] == ["no_owner"] and rows["Feed"]["why"] == "No technical owner."
    assert rows["Feed"]["record"]["path"] == f"/integrations/{rows['Feed']['record']['id']}"
    assert rows["Delta"]["flags"] == ["entra_owner_mismatch"]
    assert [o["name"] for o in rows["Delta"]["entra_owners"]] == ["Dana Ortiz", "Stranger"]  # no service principal
    assert rows["Delta"]["entra_owners"][0]["person"]["name"] == "Dana Ortiz"
    assert "Entra also lists Stranger" in rows["Delta"]["why"]
    assert rows["Alpha"]["entra_owners"][0]["person"] is not None  # Dana is a catalog owner there: no mismatch
    assert body["counts"] == {
        "total": 5,
        "flagged": 5,
        "inactive_owner": 1,
        "owner_left": 1,
        "no_owner": 2,
        "entra_owner_mismatch": 1,
    }


def test_removed_from_the_organisation_counts_as_left(a, api):
    scene_ = review_scene(a)
    priya = a["people"]["priya"]
    priya.refresh_from_db()
    priya.user.is_active = True
    priya.user.save()
    rows = {r["record"]["name"]: r for r in api(a["users"]["viewer"]).get("reports/access-review").json_body["results"]}
    assert rows["Beta"]["flags"] == []
    Membership.objects.filter(user=priya.user, organization=a["org"]).delete()
    rows = {r["record"]["name"]: r for r in api(a["users"]["viewer"]).get("reports/access-review").json_body["results"]}
    assert rows["Beta"]["flags"] == ["owner_left"]
    assert scene_["beta"].id


def test_the_access_review_filters(a, api):
    s = review_scene(a)
    viewer = api(a["users"]["viewer"])

    def names(query):
        response = viewer.get(f"reports/access-review?{query}")
        assert response.status_code == 200, response.json_body
        return {r["record"]["name"] for r in response.json_body["results"]}

    assert names("flag=no_owner") == {"Feed", "orga App"}
    assert names("flag=no_owner,owner_left") == {"Feed", "orga App", "Beta"}
    assert names("kind=integration") == {"Feed"}
    assert names("kind=application,component&flag=no_owner") == {"orga App"}
    assert names(f"owner={a['people']['dana'].id}") == {"Alpha", "Delta"}
    assert names(f"owner={a['people']['dana'].id}&include_retired=true") == {"Alpha", "Delta", "Retired one"}
    counts = viewer.get("reports/access-review?flag=no_owner").json_body["counts"]
    assert counts["total"] == 5 and counts["no_owner"] == 2  # counts are before the flag filter
    bad = viewer.get("reports/access-review?flag=bad&kind=vendor&owner=x&include_retired=maybe&colour=red")
    assert bad.status_code == 400
    assert set(bad.json_body["errors"]) == {"flag", "kind", "owner", "include_retired", "colour"}
    assert s["feed"].id


def test_the_access_review_leaves_ai_agents_out_without_the_capability(a, api, monkeypatch):
    from core import capabilities

    agent(a["org"], "Helper")
    viewer = api(a["users"]["viewer"])
    assert "Helper" in {r["record"]["name"] for r in viewer.get("reports/access-review").json_body["results"]}
    monkeypatch.setattr(capabilities, "capabilities", lambda org: capabilities.COMMUNITY - {capabilities.AI_AGENTS})
    assert "Helper" not in {r["record"]["name"] for r in viewer.get("reports/access-review").json_body["results"]}


def test_the_access_review_csv_is_escaped_audited_and_restricted_while_impersonating(a, api):
    s = review_scene(a)
    Resource.objects.filter(id=s["beta"].id).update(name="+SUM(1)")
    response = api(a["users"]["viewer"]).client.get("/api/v1/reports/access-review/export.csv?flag=owner_left")
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    lines = response.content.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("record,record_kind,record_id,lifecycle,business_owner,business_owner_id")
    assert len(lines) == 2 and lines[1].startswith("'+SUM(1),component,") and ",left,owner_left," in lines[1]
    event = AuditEvent.objects.get(action="export")
    assert event.changes == {"kind": "access_review_record", "rows": 1, "filters": {"flag": ["owner_left"]}}
    assert ("GET", "api/v1/reports/access-review/export.csv") in RESTRICTED_ROUTES
    staff = make_staff()
    client = api(staff)
    assert start(client, a["org"], a["users"]["admin"]).status_code == 201
    is_problem(client.client.get("/api/v1/reports/access-review/export.csv"), 403, "/problems/impersonation-restricted")
    assert client.get("reports/access-review").status_code == 200


def test_the_access_review_stays_in_its_organisation(a, b, api):
    review_scene(a)
    rows = api(b["users"]["viewer"]).get("reports/access-review").json_body["results"]
    assert [r["record"]["name"] for r in rows] == ["orgb App"]
    assert (
        api(b["users"]["viewer"]).get(f"reports/access-review?owner={a['people']['dana'].id}").json_body["results"]
        == []
    )
    assert api().get("reports/access-review").status_code in (401, 403)


def test_a_real_run_with_the_dry_runs_plan_hash_applies_and_refuses_when_the_rows_changed(a, api):
    """The dry run's plan_hash covers the rows that would change: a real run that sends it applies when nothing moved
    since, and is refused (409 reassign-stale, nothing saved) when another record came to hold the role meanwhile."""
    s = scene(a)
    editor = api(a["users"]["editor"])
    url = f"people/{s['priya'].id}/reassign"
    body = {"to": str(s["dana"].id), "roles": ["business_owner", "technical_owner"]}
    preview = editor.post(url, {**body, "dry_run": True}).json_body
    assert len(preview["plan_hash"]) == 64
    again = editor.post(url, {**body, "roles": ["technical_owner", "business_owner"], "dry_run": True}).json_body
    assert again["plan_hash"] == preview["plan_hash"]  # the roles' order doesn't matter
    other = editor.post(url, {**body, "roles": ["business_owner"], "dry_run": True}).json_body
    assert other["plan_hash"] != preview["plan_hash"]

    # A record that starts to hold the role after the preview makes the plan stale: nothing is saved.
    late = record(a["org"], "Late app", lifecycle="active", business_owner=s["priya"])
    before = AuditEvent.objects.count()
    problem = is_problem(editor.post(url, {**body, "plan_hash": preview["plan_hash"]}), 409)
    assert problem["type"] == "/problems/reassign-stale"
    assert AuditEvent.objects.count() == before
    s["app"].refresh_from_db()
    assert s["app"].business_owner_id == s["priya"].id

    # A fresh preview's hash applies; a real run without a hash still works as before.
    fresh = editor.post(url, {**body, "dry_run": True}).json_body
    assert fresh["plan_hash"] != preview["plan_hash"]
    done = editor.post(url, {**body, "plan_hash": fresh["plan_hash"]})
    assert done.status_code == 200, done.json_body
    assert done.json_body["plan_hash"] == fresh["plan_hash"] and done.json_body["dry_run"] is False
    late.refresh_from_db()
    assert late.business_owner_id == s["dana"].id


def test_plan_hash_must_be_text_and_only_on_a_real_run(a, api):
    s = scene(a)
    editor = api(a["users"]["editor"])
    url = f"people/{s['priya'].id}/reassign"
    base = {"to": str(s["dana"].id), "roles": ["business_owner"]}
    assert "plan_hash" in is_problem(editor.post(url, {**base, "plan_hash": 5}), 400)["errors"]
    assert "plan_hash" in is_problem(editor.post(url, {**base, "plan_hash": "x", "dry_run": True}), 400)["errors"]
