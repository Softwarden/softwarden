"""Data quality (catalog/quality.py).

Every built-in rule failing and passing; capability and compliance-profile gating; turning rules off; exceptions
(create, refusals, expiry, audit, tenancy); the completeness score, its filter and ordering; the summary and the rules
list; failures paging; the Needs attention queue without duplicates; the record gaps agreeing with the rules; flat
query counts; and the rule specs as data."""

import json
from datetime import date, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from catalog import attachments, compliance, quality, services
from catalog.models import (
    AIAgent,
    Application,
    Component,
    Integration,
    Person,
    QualityException,
    Resource,
    Vendor,
    VendorRiskProfile,
)
from catalog.serializers import QualityRuleSerializer, QualitySummarySerializer
from catalog.state import agent_gaps, all_gaps
from core import capabilities as caps
from core.dates import add_months
from core.models import AuditEvent
from discovery.models import AppCoverage, Connector
from eol import services as eol_services
from eol.models import EolProduct, EolRelease, TrackedVersion
from money import contracts
from money.models import Contract, ContractItem
from notify import digest
from notify.attention import UNCOUNTED
from notify.models import NotificationSnooze
from notify.serializers import AttentionDataQualitySerializer
from tests.conftest import make_org, revoke_capability, snapshot_problems

pytestmark = pytest.mark.django_db


def enterprise(org, on=True):
    fresh, _ = caps.set_edition(org, "enterprise")
    if on:
        fresh.settings = {**fresh.settings, compliance.SETTING: True}
        fresh.save(update_fields=["settings"])
    compliance.forget(org.pk)
    org.edition, org.settings = fresh.edition, fresh.settings
    return org


def eol_cache():
    """A cached endoflife.date product with one release cycle (eol/models.py)."""
    product, _ = EolProduct.objects.get_or_create(
        name="postgresql", defaults={"label": "PostgreSQL", "releases_fetched_at": timezone.now()}
    )
    EolRelease.objects.get_or_create(product=product, name="16", defaults={"eol_from": date(2028, 11, 9)})
    return product


def coverage(ctx, record):
    """An SSO and MFA coverage row for the application's Entra app (SAML, provisioning on, MFA for all
    users), so the coverage rules apply and pass. One coverage connector per organization."""
    import uuid

    org = ctx["org"]
    app_id = str(uuid.uuid4())
    Application.objects.filter(resource=record).update(entra_app_id=app_id)
    connector = Connector.objects.filter(organization=org, source="entra_coverage").first()
    if connector is None:
        connector = Connector.objects.create(organization=org, source="entra_coverage", name="Coverage")
    now = timezone.now()
    AppCoverage.objects.create(
        organization=org,
        connector=connector,
        app_id=app_id,
        display_name=record.name,
        sso="saml",
        scim="on",
        mfa="all_users",
        first_seen_at=now,
        seen_at=now,
    )


def full(ctx, kind="application", name=None, **over):
    """A record that passes every community rule: owners, team, criticality, classification, a vendor, documentation
    and a line on a live contract (by kind), an agent's scope and human-in-loop decision, and an
    application's or component's end-of-life tracking with a known cycle and an application's SSO and MFA coverage.
    `over` replaces any of those."""
    org, editor = ctx["org"], ctx["users"]["editor"]
    if kind == "integration":  # data-flow rules: a classified flow with its data categories
        ends = [
            services.create_resource(
                org, editor, {"kind": "component", "name": f"End {i} {Resource.objects.count()}", "lifecycle": "active"}
            )
            for i in (1, 2)
        ]
        data = {
            "name": name or f"Flow {Resource.objects.count()}",
            "lifecycle": "active",
            "source": str(ends[0].id),
            "target": str(ends[1].id),
            "data_classification": "confidential",
            "data_categories": ["personal"],
            "it_steward": editor.id,
        } | over
        return services.create_integration(org, editor, data)
    vendor = Vendor.objects.get_or_create(organization=org, name="Fabrikam")[0]
    data = {
        "kind": kind,
        "name": name or f"{kind} {Resource.objects.count()}",
        "lifecycle": "active",
        "business_owner": str(ctx["people"]["dana"].id),
        "technical_owner": str(ctx["people"]["priya"].id),
        "support_team": str(ctx["team"].id),
        "criticality": "high",
        "data_classification": "internal",
        "it_steward": editor.id,
    }
    if kind == "application":
        data |= {"vendor": str(vendor.id), "documentation_url": "https://docs.example.test/app"}
    elif kind == "component":
        data |= {"documentation_url": "https://docs.example.test/job"}
    else:
        data |= {"data_access_scope": "HR policies only", "human_in_loop": False}
    data |= over
    record = services.create_resource(org, editor, data)
    if kind == "application":
        # The vendor is high risk and processes personal data, with a current report and a signed DPA, so
        # the vendor risk rules apply and pass.
        VendorRiskProfile.objects.get_or_create(
            organization=org,
            vendor=vendor,
            defaults={
                "risk_tier": "high",
                "processes_personal_data": True,
                "dpa_status": "yes",
                "assurance_issued_on": services.org_today(org),
                "assurance_validity_months": 120,  # still current in tests that look years ahead
                "assurance_expires_on": add_months(services.org_today(org), 120),
            },
        )
        held = Contract.objects.filter(organization=org, name="Held contract").first()
        held = held or contracts.create_contract(org, editor, {"name": "Held contract"})
        contracts.add_item(org, editor, held, {"record": str(record.id)})
        coverage(ctx, record)
    if kind in ("application", "component"):
        eol_cache()
        eol_services.track(org, editor, record, {"product": "postgresql", "cycle": "16"})
    return record


def failing(ctx, record):
    return sorted(r.id for r in quality.evaluate(ctx["org"], [record.id])[record.id].failing)


def exception(ctx, record, rule="documentation", **over):
    data = {
        "record": str(record.id),
        "rule": rule,
        "reason": "Vendor portal holds it.",
        "owner": str(ctx["people"]["dana"].id),
    }
    return quality.create_exception(ctx["org"], ctx["users"]["editor"], {**data, **over})


# ---- every built-in rule ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["application", "component", "ai_agent"])
def test_a_complete_record_fails_nothing_and_scores_100(a, kind):
    record = full(a, kind)
    result = quality.evaluate(a["org"], [record.id])[record.id]
    assert result.failing == [] and result.excepted == [] and result.score == 100
    # every rule that is on and checks this kind applies: an active record, not built in-house
    assert result.applicable == len([r for r in quality.active_rules(a["org"]) if kind in r.kinds])


def _break(ctx, record, rule_id):
    """Make `record` fail `rule_id` with the smallest change."""
    r = Resource.objects.get(pk=record.pk)
    if rule_id == "business_owner":
        Resource.objects.filter(pk=r.pk).update(business_owner=None)
    elif rule_id == "technical_owner":
        Resource.objects.filter(pk=r.pk).update(technical_owner=None)
    elif rule_id == "support_team":
        Resource.objects.filter(pk=r.pk).update(support_team=None)
    elif rule_id == "inactive_owner":
        Resource.objects.filter(pk=r.pk).update(technical_owner=ctx["people"]["gone"])
    elif rule_id == "criticality":
        Resource.objects.filter(pk=r.pk).update(criticality="")
    elif rule_id == "data_classification":
        Resource.objects.filter(pk=r.pk).update(data_classification="")
    elif rule_id == "documentation":
        model = Application if r.kind == "application" else Component
        model.objects.filter(resource=r).update(documentation_url="")
    elif rule_id == "vendor":
        Application.objects.filter(resource=r).update(vendor=None)
    elif rule_id == "contract":
        ContractItem.objects.filter(resource=r).delete()
    elif rule_id == "review_overdue":
        Resource.objects.filter(pk=r.pk).update(created_at=timezone.now() - timedelta(days=200))
    elif rule_id == "agent_data_access_scope":
        AIAgent.objects.filter(resource=r).update(data_access_scope="   ")
    elif rule_id == "agent_human_in_loop":
        AIAgent.objects.filter(resource=r).update(human_in_loop=None)
    elif rule_id == "vendor_assurance":
        VendorRiskProfile.objects.filter(vendor=r.application.vendor).update(assurance_expires_on=None)
    elif rule_id == "vendor_dpa":
        VendorRiskProfile.objects.filter(vendor=r.application.vendor).update(dpa_status="no")
    elif rule_id == "eol_cycle_unknown":
        TrackedVersion.objects.filter(resource=r).update(cycle="", cycle_explicit=False)
    elif rule_id == "flow_classification":
        Resource.objects.filter(pk=r.pk).update(data_classification="")
    elif rule_id == "flow_categories":
        Integration.objects.filter(resource=r).update(data_categories=[])
    elif rule_id in ("sso_missing", "mfa_missing", "scim_missing"):
        field, value = {
            "sso_missing": ("sso", "none"),
            "mfa_missing": ("mfa", "none"),
            "scim_missing": ("scim", "off"),
        }[rule_id]
        AppCoverage.objects.filter(app_id=r.application.entra_app_id).update(**{field: value})
    else:
        raise AssertionError(rule_id)


COMMUNITY_RULES = [
    (rule.id, kind) for rule in quality.RULES if not rule.compliance and not rule.planned for kind in rule.kinds
]


@pytest.mark.parametrize("rule_id,kind", COMMUNITY_RULES)
def test_each_community_rule_fails_on_exactly_its_gap(a, rule_id, kind):
    record = full(a, kind)
    assert failing(a, record) == []
    _break(a, record, rule_id)
    assert failing(a, record) == [rule_id]


def test_documentation_is_a_url_or_an_attachment_of_a_documentation_category(a):
    app = full(a, "application", documentation_url="")
    job = full(a, "component", documentation_url="")
    assert failing(a, app) == ["documentation"] and failing(a, job) == ["documentation"]
    Component.objects.filter(resource=job).update(runbook_url="https://runbooks.example.test/job")
    assert failing(a, job) == []  # a component's runbook counts
    upload = SimpleUploadedFile("invoice.txt", b"Invoice 42.")
    attachments.upload_file(a["org"], a["users"]["editor"], app, upload, {"category": "invoice"})
    assert failing(a, app) == ["documentation"]  # an invoice isn't documentation
    attachments.add_link(
        a["org"], a["users"]["editor"], app, {"url": "https://wiki.example.test", "category": "sop_runbook"}
    )
    assert failing(a, app) == []


def test_vendor_applies_only_to_applications_not_built_in_house(a):
    internal = full(a, "application", vendor=None, built_by="internal")
    bought = full(a, "application", vendor=None, built_by="vendor")
    unknown = full(a, "application", vendor=None)
    assert failing(a, internal) == [] and failing(a, bought) == ["vendor"] and failing(a, unknown) == ["vendor"]
    vendor = [quality.BY_ID["vendor"]]
    result = quality.evaluate(a["org"], [internal.id], rules=vendor)[internal.id]
    assert result.applicable == quality.evaluate(a["org"], [bought.id], rules=vendor)[bought.id].applicable - 1


def test_review_overdue_follows_the_newest_review_and_only_active_records(a):
    app = full(a)
    Resource.objects.filter(pk=app.pk).update(created_at=timezone.now() - timedelta(days=400))
    assert failing(a, app) == ["review_overdue"]  # never reviewed, added more than an interval ago
    today = services.org_today(a["org"])
    old = (today - timedelta(days=200)).isoformat()
    services.record_review(
        a["org"],
        a["users"]["editor"],
        app,
        {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Call", "confirmation_date": old},
    )
    assert failing(a, app) == ["review_overdue"]  # its next review date has passed
    services.record_review(
        a["org"], a["users"]["editor"], app, {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Call"}
    )
    assert failing(a, app) == []
    proposed = full(a, lifecycle="proposed")
    Resource.objects.filter(pk=proposed.pk).update(created_at=timezone.now() - timedelta(days=400))
    assert failing(a, proposed) == []  # reviews are for active and deprecated records


def test_compiled_rules_follow_the_organizations_settings(a):
    """The rule SQL is cached per organization, day and set of rules; settings the rules read are part of the key."""
    app = full(a)
    Resource.objects.filter(pk=app.pk).update(created_at=timezone.now() - timedelta(days=60))
    assert failing(a, app) == []  # 60 days old, never reviewed: within the 90-day interval
    a["org"].review_interval_days = 30
    a["org"].save()
    assert failing(a, app) == ["review_overdue"]
    later = services.org_today(a["org"]) + timedelta(days=400)
    assert [r.id for r in quality.evaluate(a["org"], [app.id], today=later)[app.id].failing] == ["review_overdue"]


def test_human_in_loop_false_is_a_decision(a):
    agent = full(a, "ai_agent", human_in_loop=False)
    assert failing(a, agent) == []
    AIAgent.objects.filter(resource=agent).update(human_in_loop=True)
    assert failing(a, agent) == []


def test_the_contract_rule_is_on_since_contracts_exist(a):
    """Registered before contracts existed; contracts switched it on, gated by
    the contracts capability. tests/test_contracts.py checks what passes and fails it."""
    rule = quality.BY_ID["contract"]
    assert not rule.planned and rule.capability == caps.CONTRACTS
    assert quality.status(rule, a["org"]) == (quality.ON, "")
    assert rule in quality.active_rules(a["org"])
    assert not [r for r in quality.RULES if r.planned]


# ---- gating -------------------------------------------------------------------------------------------------------------


COMPLIANCE_RULES = ["validation_status", "risk_tier", "periodic_review_date", "periodic_review_overdue"]


def test_compliance_rules_need_the_capability_and_the_profile(a, b):
    # Off by default in every edition: the profile is off.
    org = a["org"]
    assert all(
        quality.status(quality.BY_ID[i], org)[1].startswith("The compliance profile is off") for i in COMPLIANCE_RULES
    )
    enterprise(org)
    assert all(quality.status(quality.BY_ID[i], org)[0] == quality.ON for i in COMPLIANCE_RULES)
    # Without the capability (a platform revoke), the rules are unavailable.
    other = b["org"]
    revoke_capability(other, "compliance_profile")
    statuses = {r.id: quality.status(r, other) for r in quality.RULES}
    assert all(
        statuses[i][0] == quality.UNAVAILABLE and "compliance_profile" in statuses[i][1] for i in COMPLIANCE_RULES
    )


def test_compliance_rules_check_the_profile_fields(a):
    org = enterprise(a["org"])
    app = full(a)
    assert failing(a, app) == ["risk_tier", "validation_status"]
    Resource.objects.filter(pk=app.pk).update(risk_tier="low", validation_status="not_required")
    assert failing(a, app) == []  # no periodic review date needed: not GxP, not high risk
    Resource.objects.filter(pk=app.pk).update(is_gxp=True)
    assert failing(a, app) == ["periodic_review_date"]
    today = services.org_today(org)
    Resource.objects.filter(pk=app.pk).update(next_periodic_review=today - timedelta(days=1))
    assert failing(a, app) == ["periodic_review_overdue"]
    Resource.objects.filter(pk=app.pk).update(next_periodic_review=today)
    assert failing(a, app) == []


def test_agent_rules_and_agents_need_the_ai_agents_capability(a, monkeypatch, api):
    agent = full(a, "ai_agent", human_in_loop=None)
    full(a, "application", criticality="")
    monkeypatch.setitem(caps.EDITION_CAPABILITIES, "community", caps.COMMUNITY - {caps.AI_AGENTS})
    org = a["org"]
    assert quality.status(quality.BY_ID["agent_human_in_loop"], org)[0] == quality.UNAVAILABLE
    assert "ai_agent" not in quality.checked_kinds(org)
    body = api(a["users"]["viewer"]).get("data-quality/summary").json_body
    assert [k["kind"] for k in body["by_kind"]] == ["application", "component", "integration"]
    assert agent.id not in [
        r["id"] for r in api(a["users"]["viewer"]).get("data-quality/failures").json_body["results"]
    ]


def test_every_route_needs_the_data_quality_capability(a, api, monkeypatch):
    monkeypatch.setitem(caps.EDITION_CAPABILITIES, "community", caps.COMMUNITY - {caps.DATA_QUALITY})
    client = api(a["users"]["admin"])
    for path in ("data-quality/rules", "data-quality/summary", "data-quality/failures", "data-quality/exceptions"):
        response = client.get(path)
        assert response.status_code == 403 and response.json_body["capability"] == "data_quality", path
    assert client.get("needs-attention?scope=all").json_body["data_quality"] == []
    assert all(r["quality"]["failing"] == [] for r in client.get("applications").json_body["results"])


# ---- turning rules off ----------------------------------------------------------------------------------------------


def test_an_admin_turns_a_rule_off_and_on_in_organization_settings(a, api):
    full(a, documentation_url="")
    admin = api(a["users"]["admin"])
    assert api(a["users"]["editor"]).patch("organization", {quality.SETTING: ["documentation"]}).status_code == 403
    response = admin.patch("organization", {quality.SETTING: ["documentation", "documentation"]})
    assert response.status_code == 200 and response.json_body[quality.SETTING] == ["documentation"]
    rules = {r["id"]: r for r in admin.get("data-quality/rules").json_body}
    assert rules["documentation"]["status"] == "off" and rules["documentation"]["counts"] is None
    assert "documentation" not in [r["id"] for r in admin.get("data-quality/summary").json_body["by_rule"]]
    event = AuditEvent.objects.filter(organization=a["org"], action="organization.updated").latest("created_at")
    assert event.changes[quality.SETTING] == [[], ["documentation"]]
    assert (event.postchange or {})["settings"][quality.SETTING] == ["documentation"]
    assert admin.patch("organization", {quality.SETTING: []}).json_body[quality.SETTING] == []
    assert {r["id"]: r for r in admin.get("data-quality/rules").json_body}["documentation"]["status"] == "on"


@pytest.mark.parametrize(
    "value,message",
    [
        (["nonsense"], "Unknown rule: nonsense."),
        (["business_owner"], "can't be turned off"),
        ("documentation", "Send a list of rule ids."),
    ],
)
def test_turning_off_unknown_or_gap_rules_is_refused(a, api, value, message):
    response = api(a["users"]["admin"]).patch("organization", {quality.SETTING: value})
    assert response.status_code == 400 and message in response.json_body["errors"][quality.SETTING][0]


# ---- exceptions ---------------------------------------------------------------------------------------------------


def test_an_editor_marks_a_check_not_applicable_and_it_is_audited_on_the_record(a, api):
    app = full(a, documentation_url="")
    editor = api(a["users"]["editor"])
    body = {
        "record": str(app.id),
        "rule": "documentation",
        "reason": "  Bundled with the vendor's suite; their portal documents it.  ",
        "owner": str(a["people"]["dana"].id),
    }
    response = editor.post("data-quality/exceptions", body)
    assert response.status_code == 201, response.json_body
    made = response.json_body
    today = services.org_today(a["org"])
    assert made["record"] == {"id": str(app.id), "name": app.name, "kind": "application"}
    assert made["rule_label"] == "No documentation" and made["active"] is True
    assert made["reason"] == "Bundled with the vendor's suite; their portal documents it."
    assert made["expires_on"] == (today + timedelta(days=365)).isoformat()
    assert made["owner"]["id"] == str(a["people"]["dana"].id) and made["created_by"]["id"] == a["users"]["editor"].id
    result = quality.evaluate(a["org"], [app.id])[app.id]
    assert result.failing == [] and [r.id for r in result.excepted] == ["documentation"] and result.score == 100
    record = editor.get(f"applications/{app.id}").json_body["quality"]
    assert record["failing"] == [] and record["excepted"] == [
        {"id": "documentation", "label": "No documentation", "severity": "low"}
    ]
    event = AuditEvent.objects.get(organization=a["org"], action="quality_exception.created")
    assert (event.related_target_type, event.related_target_id) == ("Resource", app.id)
    assert (event.postchange or {})["rule"] == "documentation" and snapshot_problems(event) == []
    history = editor.get(f"applications/{app.id}/audit").json_body["results"]
    assert any(e["action"] == "quality_exception.created" and "No documentation until" in e["summary"] for e in history)
    assert api(a["users"]["viewer"]).post("data-quality/exceptions", body).status_code == 403


@pytest.mark.parametrize(
    "over,field,message",
    [
        ({"rule": "business_owner"}, "rule", "can't be marked not applicable"),
        ({"rule": "nonsense"}, "rule", "Choose a data-quality rule."),
        ({"rule": "periodic_review_date"}, "rule", "isn't on"),
        ({"reason": "  "}, "reason", "Say why"),
        ({"reason": "x" * 1001}, "reason", "1,000 characters"),
        ({"owner": None}, "owner", "Choose who answers"),
        ({"expires_on": "2000-01-01"}, "expires_on", "today or a later date"),
        ({"expires_on": "not a date"}, "expires_on", "YYYY-MM-DD"),
        ({"record": "4c3f0d5e-1111-4222-8333-444455556666"}, "record", "existing record"),
    ],
)
def test_refused_exceptions(a, api, over, field, message):
    app = full(a, documentation_url="")
    body = {
        "record": str(app.id),
        "rule": "documentation",
        "reason": "Why",
        "owner": str(a["people"]["dana"].id),
        **over,
    }
    response = api(a["users"]["editor"]).post("data-quality/exceptions", body)
    assert response.status_code == 400 and message in response.json_body["errors"][field][0], response.json_body
    assert not QualityException.objects.exists()


def test_exception_limits_kind_owner_and_duplicates(a, api):
    agent = full(a, "ai_agent")
    app = full(a, documentation_url="")
    editor = api(a["users"]["editor"])
    base = {"reason": "Why", "owner": str(a["people"]["dana"].id)}
    wrong_kind = editor.post("data-quality/exceptions", {**base, "record": str(agent.id), "rule": "documentation"})
    assert "doesn't apply to this kind" in wrong_kind.json_body["errors"]["rule"][0]
    gone = editor.post(
        "data-quality/exceptions",
        {**base, "record": str(app.id), "rule": "documentation", "owner": str(a["people"]["gone"].id)},
    )
    assert gone.json_body["errors"]["owner"] == ["Choose an active person."]
    late = (services.org_today(a["org"]) + timedelta(days=366)).isoformat()
    too_late = editor.post(
        "data-quality/exceptions", {**base, "record": str(app.id), "rule": "documentation", "expires_on": late}
    )
    assert "at most 365 days ahead" in too_late.json_body["errors"]["expires_on"][0]
    first = editor.post("data-quality/exceptions", {**base, "record": str(app.id), "rule": "documentation"})
    again = editor.post("data-quality/exceptions", {**base, "record": str(app.id), "rule": "documentation"})
    assert (
        first.status_code == 201 and again.status_code == 409 and again.json_body["existing"] == first.json_body["id"]
    )


def test_an_expired_exception_stops_applying_and_extending_it_brings_it_back(a, api):
    app = full(a, documentation_url="")
    made = exception(a, app)
    yesterday = services.org_today(a["org"]) - timedelta(days=1)
    QualityException.objects.filter(pk=made.pk).update(expires_on=yesterday)
    assert failing(a, app) == ["documentation"]
    editor = api(a["users"]["editor"])
    assert editor.get(f"data-quality/exceptions/{made.id}").json_body["active"] is False
    assert [e["id"] for e in editor.get("data-quality/exceptions?active=false").json_body["results"]] == [str(made.id)]
    assert editor.get("data-quality/exceptions?active=true").json_body["results"] == []
    seen = editor.client.get(f"/api/v1/data-quality/exceptions/{made.id}")["ETag"]
    today = services.org_today(a["org"]).isoformat()
    response = editor.client.patch(
        f"/api/v1/data-quality/exceptions/{made.id}",
        json.dumps({"expires_on": today, "owner": str(a["people"]["priya"].id)}),
        content_type="application/json",
        HTTP_IF_MATCH=seen,
    )
    assert response.status_code == 200 and response.json()["active"] is True
    assert failing(a, app) == []  # the expiry day itself still counts
    event = AuditEvent.objects.get(organization=a["org"], action="quality_exception.updated")
    assert event.changes["expires_on"] == [yesterday.isoformat(), today]
    assert event.changes["accountable"] == [str(a["people"]["dana"].id), str(a["people"]["priya"].id)]
    summary = editor.get(f"applications/{app.id}/audit").json_body["results"][0]["summary"]
    assert "Accountable: Dana Ortiz → Priya Raman" in summary


def test_an_exception_cannot_move_and_removing_it_brings_the_gap_back(a, api):
    app, other = full(a, documentation_url=""), full(a, documentation_url="")
    made = exception(a, app)
    editor = api(a["users"]["editor"])
    moved = editor.patch(f"data-quality/exceptions/{made.id}", {"record": str(other.id), "rule": "vendor"})
    assert set(moved.json_body["errors"]) == {"record", "rule"}
    assert api(a["users"]["viewer"]).delete(f"data-quality/exceptions/{made.id}").status_code == 403
    assert editor.delete(f"data-quality/exceptions/{made.id}").status_code == 204
    assert failing(a, app) == ["documentation"]
    event = AuditEvent.objects.get(organization=a["org"], action="quality_exception.deleted")
    assert (event.prechange or {})["rule"] == "documentation" and event.postchange is None


def test_deleting_a_record_removes_its_exceptions_each_audited(a, api):
    app = full(a, documentation_url="")
    exception(a, app)
    ContractItem.objects.filter(resource=app).delete()  # a record on a contract isn't deleted
    response = api(a["users"]["admin"]).delete(f"applications/{app.id}")
    assert response.status_code == 204
    assert not QualityException.objects.exists()
    assert AuditEvent.objects.filter(action="quality_exception.deleted", related_target_id=app.id).count() == 1


def test_an_exception_on_a_passing_record_changes_nothing(a):
    app = full(a, documentation_url="")
    exception(a, app)
    Application.objects.filter(resource=app).update(documentation_url="https://docs.example.test")
    result = quality.evaluate(a["org"], [app.id])[app.id]
    assert result.excepted == [] and result.failing == [] and result.score == 100


def test_exceptions_stay_in_their_organization(a, b, api):
    theirs = full(b, documentation_url="")
    mine = full(a, documentation_url="")
    editor = api(a["users"]["editor"])
    base = {"rule": "documentation", "reason": "Why", "owner": str(a["people"]["dana"].id)}
    assert editor.post("data-quality/exceptions", {**base, "record": str(theirs.id)}).json_body["errors"]["record"]
    foreign_owner = editor.post(
        "data-quality/exceptions", {**base, "record": str(mine.id), "owner": str(b["people"]["dana"].id)}
    )
    assert foreign_owner.json_body["errors"]["owner"] == ["Choose an existing person."]
    held = exception(b, theirs, owner=str(b["people"]["dana"].id))
    assert editor.get(f"data-quality/exceptions/{held.id}").status_code == 404
    assert editor.get("data-quality/exceptions").json_body["results"] == []
    assert editor.get(f"data-quality/exceptions?record={theirs.id}").json_body["results"] == []
    # b's exception doesn't excuse a's record, nor is b's record in a's counts
    assert failing(a, mine) == ["documentation"]
    assert str(theirs.id) not in [r["id"] for r in editor.get("data-quality/failures").json_body["results"]]


@pytest.mark.parametrize("field", ["resource", "owner"])
def test_the_database_refuses_an_exception_pointing_at_another_organization(a, b, field):
    mine, theirs = full(a), full(b)
    values = {"resource": mine, "owner": a["people"]["dana"]}
    values[field] = theirs if field == "resource" else b["people"]["dana"]
    with pytest.raises(IntegrityError), transaction.atomic():
        QualityException.objects.create(
            organization=a["org"], rule="documentation", reason="x", expires_on=services.org_today(a["org"]), **values
        )
        connection.check_constraints()  # the composite keys are deferred; force the check


def test_blank_text_means_whitespace_by_pythons_definition():
    import sys

    assert all(not chr(c).isspace() for c in range(0x3001, sys.maxunicode + 1))
    assert set(quality.WHITESPACE) == {chr(c) for c in range(0x3001) if chr(c).isspace()}


def test_an_exception_is_only_extended(a, api):
    app = full(a, documentation_url="")
    today = services.org_today(a["org"])
    made = exception(a, app, expires_on=(today + timedelta(days=30)).isoformat())
    editor = api(a["users"]["editor"])
    earlier = editor.patch(
        f"data-quality/exceptions/{made.id}", {"expires_on": (today + timedelta(days=10)).isoformat()}
    )
    assert earlier.status_code == 400
    assert earlier.json_body["errors"]["expires_on"][0].startswith("Choose a date on or after the current expiry")
    later = editor.patch(f"data-quality/exceptions/{made.id}", {"expires_on": (today + timedelta(days=60)).isoformat()})
    assert later.status_code == 200 and later.json_body["expires_on"] == (today + timedelta(days=60)).isoformat()


def test_an_anonymized_owner_must_be_replaced_to_extend(a, api):
    from catalog import admin_services

    app = full(a, documentation_url="")
    owner = Person.objects.create(organization=a["org"], name="Leaver")
    made = exception(a, app, owner=str(owner.id))
    admin_services.anonymize_person(a["org"], a["users"]["admin"], owner, "Leaver")
    editor = api(a["users"]["editor"])
    shown = editor.get(f"data-quality/exceptions/{made.id}").json_body["owner"]
    assert shown["active"] is False and shown["anonymized"] is True
    assert [e["id"] for e in editor.get("data-quality/exceptions?owner_active=false").json_body["results"]] == [
        str(made.id)
    ]
    assert editor.get("data-quality/exceptions?owner_active=true").json_body["results"] == []
    later = (services.org_today(a["org"]) + timedelta(days=366 - 1)).isoformat()
    QualityException.objects.filter(pk=made.pk).update(expires_on=services.org_today(a["org"]))
    refused = editor.patch(f"data-quality/exceptions/{made.id}", {"expires_on": later})
    assert refused.status_code == 400 and refused.json_body["errors"]["owner"] == [
        "Choose an active person to extend it."
    ]
    assert (
        editor.patch(f"data-quality/exceptions/{made.id}", {"reason": "Still true."}).status_code == 200
    )  # no extension
    moved = editor.patch(
        f"data-quality/exceptions/{made.id}", {"expires_on": later, "owner": str(a["people"]["priya"].id)}
    )
    assert moved.status_code == 200 and moved.json_body["owner"]["id"] == str(a["people"]["priya"].id)


def test_a_concurrent_duplicate_is_the_same_conflict(a, monkeypatch):
    """Two requests pass the existence check at once; the unique index refuses the second inside a savepoint, and it
    gets the 409 the check would have given, with the transaction still usable."""
    from core.problems import Conflict

    app = full(a, documentation_url="")
    first = exception(a, app)
    real = QualityException.objects.filter

    def blind(*args, **kwargs):  # the second request's check ran before the first committed
        qs = real(*args, **kwargs)
        return qs.none() if kwargs.get("rule") == "documentation" and "resource" in kwargs else qs

    monkeypatch.setattr(QualityException.objects, "filter", blind)
    with pytest.raises(Conflict) as refused:
        exception(a, app)
    monkeypatch.undo()
    assert refused.value.extra["existing"] == str(first.id)
    assert QualityException.objects.count() == 1  # and the connection still works after the savepoint rolled back


# ---- the score, its filter and ordering ----------------------------------------------------------------------------


def test_the_score_is_passing_over_applicable_rounded_down(a, api):
    app = full(a, criticality="", data_classification="", documentation_url="")
    result = quality.evaluate(a["org"], [app.id])[app.id]
    applicable = result.applicable
    assert len(result.failing) == 3 and result.score == (applicable - 3) * 100 // applicable
    exception(a, app)
    after = quality.evaluate(a["org"], [app.id])[app.id]
    assert after.applicable == applicable - 1 and after.score == (applicable - 3) * 100 // (applicable - 1)
    annotated = quality.annotate_score(Resource.objects.filter(pk=app.pk), a["org"]).get()
    assert annotated.completeness == after.score  # SQL and Python agree
    body = api(a["users"]["viewer"]).get("applications").json_body["results"]
    row = next(r for r in body if r["id"] == str(app.id))
    assert row["quality"]["score"] == after.score
    assert [f["id"] for f in row["quality"]["failing"]] == ["criticality", "data_classification"]  # medium, by label


def test_completeness_filters_and_ordering(a, api):
    good = full(a, name="Good")
    poor = full(a, name="Poor", criticality="", data_classification="")
    middling = full(a, name="Middling", criticality="")
    client = api(a["users"]["viewer"])
    scores = {r: quality.evaluate(a["org"], [r.id])[r.id].score for r in (good, poor, middling)}
    assert scores[good] == 100 and scores[poor] < scores[middling] < 100
    below = client.get("applications?completeness__lt=100").json_body["results"]
    assert {r["name"] for r in below} == {"Poor", "Middling", "orga App"}  # the fixture app has no owner either
    at_least = client.get(f"applications?completeness__gte={scores[middling]}").json_body["results"]
    assert {"Good", "Middling"} <= {r["name"] for r in at_least} and "Poor" not in {r["name"] for r in at_least}
    ordered = client.get("applications?ordering=completeness&page_size=2")
    first = [r["name"] for r in ordered.json_body["results"]]
    rest = [
        r["name"]
        for r in client.get(
            f"applications?ordering=completeness&page_size=2&cursor={ordered.json_body['next']}"
        ).json_body["results"]
    ]
    names = first + rest
    assert names.index("Poor") < names.index("Middling") < names.index("Good")
    assert client.get("applications?ordering=-completeness").json_body["results"][0]["name"] == "Good"
    bad = client.get("applications?completeness__lt=101&completeness__gte=x")
    assert bad.status_code == 400 and set(bad.json_body["errors"]) == {"completeness__lt", "completeness__gte"}
    by_rule = client.get("applications?quality_rule=data_classification").json_body["results"]
    assert {r["name"] for r in by_rule} >= {"Poor"} and "Middling" not in {r["name"] for r in by_rule}
    assert client.get("applications?quality_rule=validation_status").status_code == 400
    exported = client.client.get("/api/v1/applications/export.csv?completeness__lt=100")
    assert exported.status_code == 200 and b"Poor" in exported.content and b"Good" not in exported.content


# ---- summary, rules, failures ----------------------------------------------------------------------------------------


def test_the_summary_counts_live_records_by_kind_and_rule(a, api):
    full(a, name="Complete")
    full(a, name="No class", data_classification="")
    full(a, "component", name="No docs", documentation_url="")
    excused = full(a, "component", name="Excused", documentation_url="")
    exception(a, excused)
    retired = full(a, name="Retired", criticality="")
    services.update_resource(a["org"], a["users"]["editor"], retired, {"lifecycle": "retired"})
    archived = full(a, name="Archived", criticality="")
    services.archive_resource(a["org"], a["users"]["editor"], archived)
    body = api(a["users"]["viewer"]).get("data-quality/summary").json_body
    assert QualitySummarySerializer(data=body).is_valid(), body
    kinds = {k["kind"]: k for k in body["by_kind"]}
    assert kinds["application"]["records"] == 3  # the fixture app, Complete, No class
    assert kinds["component"]["records"] == 2 and kinds["component"]["excepted"] == 1
    assert kinds["component"]["complete"] == 1 and kinds["ai_agent"]["records"] == 0
    assert body["records"] == 5 and body["complete"] == 2
    rules = {r["id"]: r for r in body["by_rule"]}
    assert rules["data_classification"]["failing"] == 2  # No class and the fixture app
    assert rules["documentation"] == {
        "id": "documentation",
        "label": "No documentation",
        "severity": "low",
        "records": 5,
        "passing": 2,
        "failing": 2,
        "excepted": 1,
        "score": 50,
    }
    assert body["score"] == body["passing"] * 100 // (body["passing"] + body["failing"])
    # Switched the contract rule on; then the two vendor risk rules, eol_cycle_unknown, the two
    # data-flow rules and the three SSO and MFA coverage rules
    assert body["rules"] == {"on": 20, "off": 0, "unavailable": 4}
    assert [r["severity"] for r in body["by_rule"]] == sorted(
        (r["severity"] for r in body["by_rule"]), key=lambda s: quality.SEVERITY_ORDER[s]
    )


def test_the_rules_list_shows_status_counts_and_specs(a, api):
    full(a, data_classification="")
    body = api(a["users"]["viewer"]).get("data-quality/rules").json_body
    assert [r["id"] for r in body] == [r.id for r in quality.RULES]
    for row in body:
        serializer = QualityRuleSerializer(data=row)
        assert serializer.is_valid(), (row["id"], serializer.errors)
        assert set(row) == set(QualityRuleSerializer().fields)
    rules = {r["id"]: r for r in body}
    assert rules["data_classification"]["counts"]["failing"] == 2 and rules["data_classification"]["status"] == "on"
    assert rules["validation_status"]["status"] == "unavailable" and rules["validation_status"]["counts"] is None
    assert rules["contract"]["status"] == "on"
    assert rules["business_owner"]["covered_by"] == "ownership_gaps" and rules["documentation"]["covered_by"] is None
    assert rules["vendor"]["spec"] == {
        "id": "vendor",
        "label": "No vendor",
        "description": "An application not built in-house with no vendor recorded.",
        "kinds": ["application"],
        "severity": "medium",
        "applies": {"field": "built_by", "test": "not_in", "value": ["internal"]},
        "fails": {"field": "vendor", "test": "missing"},
        "capability": None,
        "compliance_profile": False,
        "exceptable": True,
    }


def test_rule_specs_are_plain_data_over_known_fields_and_tests():
    def walk(node):
        if "all" in node or "any" in node:
            for child in node.get("all", node.get("any")):
                walk(child)
            return
        assert node["field"] in quality.FIELDS and node["test"] in quality.TESTS, node

    ids = [r.id for r in quality.RULES]
    assert len(ids) == len(set(ids)) and all(len(i) <= 60 for i in ids)
    for rule in quality.RULES:
        spec = json.loads(json.dumps(rule.spec()))  # JSON (and so YAML) as it stands
        walk(spec["fails"])
        if spec["applies"]:
            walk(spec["applies"])
        assert rule.severity in quality.SEVERITIES and set(rule.kinds) <= {
            "application",
            "component",
            "ai_agent",
            "integration",
        }
        assert not (rule.covered_by and rule.exceptable), rule.id  # a covered gap must not vanish here only


def test_failures_page_through_records_failing_a_rule(a, api):
    for i in range(5):
        full(a, name=f"Unclassified {i}", data_classification="")
    full(a, name="Classified")
    other = full(a, "component", name="Unclassified job", data_classification="", it_steward=a["users"]["admin"].id)
    client = api(a["users"]["viewer"])
    first = client.get("data-quality/failures?rule=data_classification&kind=application&page_size=2").json_body
    names = [r["name"] for r in first["results"]]
    cursor = first["next"]
    while cursor:
        page = client.get(
            f"data-quality/failures?rule=data_classification&kind=application&page_size=2&cursor={cursor}"
        ).json_body
        names += [r["name"] for r in page["results"]]
        cursor = page["next"]
    assert sorted(names) == sorted([f"Unclassified {i}" for i in range(5)] + ["orga App"]) and len(set(names)) == 6
    row = first["results"][0]
    assert set(row) == {"id", "kind", "name", "lifecycle", "criticality", "it_steward", "quality"}
    assert "data_classification" in [f["id"] for f in row["quality"]["failing"]]
    mine = client.get(f"data-quality/failures?rule=data_classification&it_steward={a['users']['admin'].id}")
    assert [r["id"] for r in mine.json_body["results"]] == [str(other.id)]
    assert client.get("data-quality/failures?rule=data_classification&it_steward=none").json_body["results"] == []
    assert "Classified" not in [
        r["name"] for r in client.get("data-quality/failures?page_size=200").json_body["results"]
    ]
    for bad in ("rule=nonsense", "rule=validation_status", "kind=vendor", "it_steward=x", "colour=red"):
        assert client.get(f"data-quality/failures?{bad}").status_code == 400, bad


# ---- Needs attention ------------------------------------------------------------------------------------------------


def test_the_data_quality_queue_has_one_row_per_rule_and_steward_and_no_gap_twice(a, api):
    full(a, name="No class 1", data_classification="")
    full(a, name="No class 2", data_classification="")
    full(a, name="Admin's", data_classification="", it_steward=a["users"]["admin"].id)
    full(a, name="Nobody's", criticality="", it_steward=None)
    full(a, name="Ownerless", business_owner=None)  # an ownership gap: not repeated here
    body = api(a["users"]["admin"]).get("needs-attention?scope=all").json_body
    rows = {r["id"]: r for r in body["data_quality"]}
    editor_id, admin_id = a["users"]["editor"].id, a["users"]["admin"].id
    assert rows[f"data_classification:{editor_id}"]["count"] == 3  # two plus the fixture app
    assert rows[f"data_classification:{admin_id}"]["count"] == 1
    assert rows["criticality:none"]["steward"]["coordinator"] is True  # no steward: the coordinator catches it
    assert {r["rule"] for r in body["data_quality"]} <= {
        r.id for r in quality.RULES if not r.covered_by
    }  # ownership, agent and review gaps stay in their own queues
    assert "Ownerless" in [r["name"] for r in body["ownership_gaps"]]
    for row in body["data_quality"]:
        assert set(row) == set(AttentionDataQualitySerializer().fields), row
    # Counted with every other queue since the page renders it: the badge and the page add up the same rows
    # every queue is counted now
    assert "data_quality" not in UNCOUNTED and UNCOUNTED == frozenset()
    queues = [q for q in body if isinstance(body[q], list) and q not in UNCOUNTED]
    assert body["data_quality"] and body["counts"]["all"] == sum(len(body[q]) for q in queues)
    mine = api(a["users"]["editor"]).get("needs-attention?scope=mine").json_body["data_quality"]
    assert {r["id"] for r in mine} == {k for k in rows if k.endswith(f":{editor_id}")}


def test_a_data_quality_row_can_be_snoozed_and_joins_the_digest_when_not(a, api):
    full(a, name="No class", data_classification="")
    editor = api(a["users"]["editor"])
    row = next(
        r
        for r in editor.get("needs-attention?scope=mine").json_body["data_quality"]
        if r["rule"] == "data_classification"
    )
    today = services.org_today(a["org"])
    # Not snoozed: in the steward's digest, under its own section
    items, _ = digest.plan(a["org"], today)
    assert row["id"] in [r["id"] for q, r, *_ in items[a["users"]["editor"].id] if q == "data_quality"]
    until = (today + timedelta(days=7)).isoformat()
    made = editor.post(
        "needs-attention/snoozes",
        {"kind": "snooze", "subject_type": "data_quality", "subject_id": row["id"], "until": until},
    )
    assert made.status_code == 201, made.json_body
    again = next(r for r in editor.get("needs-attention?scope=mine").json_body["data_quality"] if r["id"] == row["id"])
    assert again["snooze"]["until"] == until  # shown on the row, never hidden
    assert NotificationSnooze.objects.get(subject_type="data_quality").subject_id == row["id"]
    # Snoozed: kept out of the editor's digest until the snooze ends
    items, _ = digest.plan(a["org"], today)
    assert row["id"] not in [r["id"] for q, r, *_ in items.get(a["users"]["editor"].id, []) if q == "data_quality"]
    assert digest.SECTION_NAMES["data_quality"] == "Data quality"
    assert digest._line("data_quality", row) == "No data classification — 2 records"
    assert digest._path("data_quality", row) == (
        f"/data-quality?rule=data_classification&it_steward={a['users']['editor'].id}"
    )


# ---- gaps and rules agree ----------------------------------------------------------------------------------------------


GAP_RULES = {
    r.label: r.id for r in quality.RULES if r.covered_by in ("ownership_gaps", "agent_gaps") or r.id == "criticality"
}


def test_record_gaps_and_the_matching_rules_agree(a):
    records = [
        full(a, "application", name="Complete"),
        full(
            a,
            "application",
            name="Nothing",
            business_owner=None,
            technical_owner=None,
            support_team=None,
            criticality="",
        ),
        full(a, "component", name="Gone owner"),
        full(a, "component", name="Both gone", business_owner=None),
        full(a, "ai_agent", name="Bare agent", data_access_scope="", human_in_loop=None, criticality=""),
        full(a, "ai_agent", name="No-break spaces"),
        full(a, "ai_agent", name="Spaced scope"),
        full(a, "ai_agent", name="Blank scope", data_access_scope="  "),
    ]
    Resource.objects.filter(name="Both gone").update(
        technical_owner=a["people"]["gone"], business_owner=a["people"]["gone"]
    )
    Resource.objects.filter(name="Gone owner").update(technical_owner=a["people"]["gone"])
    # Stored as written (the service strips; an import or an old row may not): blank by Python's definition either way
    AIAgent.objects.filter(resource__name="No-break spaces").update(data_access_scope=" 　	 ")
    AIAgent.objects.filter(resource__name="Spaced scope").update(data_access_scope=" HR policies ")
    rows = Resource.objects.filter(id__in=[r.id for r in records]).select_related(
        "business_owner", "technical_owner", "ai_agent"
    )
    results = quality.evaluate(a["org"], [r.id for r in rows])
    for r in rows:
        gaps = all_gaps(r) + (agent_gaps(r.ai_agent) if r.kind == "ai_agent" else [])
        from_rules = sorted(rule.label for rule in results[r.id].failing if rule.label in GAP_RULES)
        assert sorted(gaps) == from_rules, r.name


# ---- query counts -----------------------------------------------------------------------------------------------------


def _count(client, path):
    with CaptureQueriesContext(connection) as queries:
        response = client.get(path)
    assert response.status_code == 200, response.json_body
    return len(queries)


@pytest.mark.parametrize(
    "path",
    [
        "data-quality/failures",
        "data-quality/exceptions",
        "applications",
        "applications?completeness__lt=100&ordering=completeness",
        "data-quality/summary",
        "data-quality/rules",
        "needs-attention?scope=all",
    ],
)
def test_query_counts_do_not_grow_with_rows(a, api, path):
    client = api(a["users"]["viewer"])

    def add(n, start):
        for i in range(start, start + n):
            app = full(a, name=f"Row {i}", documentation_url="", data_classification="")
            exception(a, app)

    add(1, 0)
    client.get(path)  # warm per-process caches before counting
    few = _count(client, path)
    add(15, 1)
    assert _count(client, path) == few, path


def test_seeded_and_fixture_organizations_list_rules_without_failing(db, api):
    ctx = make_org("orgq")
    for role in ("admin", "editor", "viewer"):
        client = api(ctx["users"][role])
        assert client.get("data-quality/rules").status_code == 200
        assert client.get("data-quality/summary").status_code == 200
