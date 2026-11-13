"""A value of the wrong JSON type in a text field is a 400 naming the field, never a silent clear.

Every optional text field of records, vendors, people, contracts and renewals is sent 123, true, [] and {}: each
answers 400 with the field under `errors`, and the stored row is unchanged. null still clears an optional field."""

from __future__ import annotations

import pytest

from catalog.admin_services import VENDOR_FIELDS
from catalog.models import Application, Kind, Person, Resource, Vendor
from catalog.services import EXTENSION_TEXT
from money.models import Contract, RenewalCycle

WRONG = [("int", 123), ("bool", True), ("list", []), ("dict", {})]


def _stored(model, pk):
    return model.objects.values().get(pk=pk)


def _assert_refused(response, field):
    assert response.status_code == 400, (field, response.status_code, response.content[:300])
    assert field in response.json_body["errors"], response.json_body


RECORD_FIELDS = sorted(
    {("application", f) for f in ("description", "cost_center", *EXTENSION_TEXT[Kind.APPLICATION])}
    | {("component", f) for f in ("description", "cost_center", *EXTENSION_TEXT[Kind.COMPONENT])}
    | {("ai_agent", f) for f in ("description", "cost_center", *EXTENSION_TEXT[Kind.AI_AGENT])}
    | {("integration", f) for f in ("description", *EXTENSION_TEXT[Kind.INTEGRATION])}
)
ROUTES = {
    "application": "applications",
    "component": "components",
    "ai_agent": "ai-agents",
    "integration": "integrations",
}


def _record(a, api, kind):
    editor = api(a["users"]["editor"])
    if kind == "application":
        return a["app"].id
    if kind == "integration":
        other = editor.post("applications", {"name": "Other end", "lifecycle": "active"}).json_body["id"]
        body = {"name": "Feed", "lifecycle": "active", "source": str(a["app"].id), "target": other}
        return editor.post("integrations", body).json_body["id"]
    return editor.post(ROUTES[kind], {"name": f"A {kind}", "lifecycle": "active"}).json_body["id"]


@pytest.mark.parametrize("label,value", WRONG)
@pytest.mark.parametrize("kind,field", RECORD_FIELDS)
def test_record_text_fields_refuse_other_types(a, api, kind, field, label, value):
    editor = api(a["users"]["editor"])
    record = _record(a, api, kind)
    resource = Resource.objects.select_related(*Kind.values).get(id=record)
    extension = getattr(resource, kind)
    before = (_stored(Resource, record), _stored(type(extension), extension.pk))
    _assert_refused(editor.patch(f"{ROUTES[kind]}/{record}", {field: value}), field)
    assert (_stored(Resource, record), _stored(type(extension), extension.pk)) == before
    if kind != "integration":
        count = Resource.objects.count()
        _assert_refused(editor.post(ROUTES[kind], {"name": "New", "lifecycle": "active", field: value}), field)
        assert Resource.objects.count() == count


@pytest.mark.parametrize("label,value", WRONG)
@pytest.mark.parametrize("field", sorted(VENDOR_FIELDS))
def test_vendor_text_fields_refuse_other_types(a, api, field, label, value):
    editor = api(a["users"]["editor"])
    vendor = Vendor.objects.create(organization=a["org"], name="Acme", notes="Keep me")
    before = _stored(Vendor, vendor.id)
    _assert_refused(editor.patch(f"vendors/{vendor.id}", {field: value}), field)
    assert _stored(Vendor, vendor.id) == before


@pytest.mark.parametrize("label,value", WRONG)
@pytest.mark.parametrize("field", ["name", "email", "title"])
def test_person_text_fields_refuse_other_types(a, api, field, label, value):
    person = Person.objects.create(organization=a["org"], name="Sam", email="sam@example.test", title="Lead")
    before = _stored(Person, person.id)
    _assert_refused(api(a["users"]["editor"]).patch(f"people/{person.id}", {field: value}), field)
    assert _stored(Person, person.id) == before


@pytest.mark.parametrize("label,value", WRONG)
@pytest.mark.parametrize("field", ["name", "contract_number", "po_number", "cost_center", "notes"])
def test_contract_text_fields_refuse_other_types(a, api, field, label, value):
    editor = api(a["users"]["editor"])
    body = {"name": "Support", "contract_number": "C-1", "po_number": "PO-1", "cost_center": "CC-1", "notes": "n"}
    contract = editor.post("contracts", body).json_body["id"]
    before = _stored(Contract, contract)
    _assert_refused(editor.patch(f"contracts/{contract}", {field: value}), field)
    assert _stored(Contract, contract) == before


@pytest.mark.parametrize("label,value", WRONG)
def test_renewal_text_fields_refuse_other_types(a, api, label, value):
    editor = api(a["users"]["editor"])
    renewal = editor.post("renewals", {"name": "Zoom renewal"}).json_body
    _assert_refused(editor.patch(f"renewals/{renewal['id']}", {"name": value}), "name")
    cycle_url = f"renewals/{renewal['id']}/cycles/{renewal['cycle']['id']}"
    before = _stored(RenewalCycle, renewal["cycle"]["id"])
    _assert_refused(
        editor.post(f"{cycle_url}/decision", {"decision": "renew", "decision_notes": value}), "decision_notes"
    )
    assert _stored(RenewalCycle, renewal["cycle"]["id"]) == before
    editor.post(f"{cycle_url}/decision", {"decision": "renew"})
    before = _stored(RenewalCycle, renewal["cycle"]["id"])
    body = {"completed_on": "2026-01-02", "completion_evidence": value}
    _assert_refused(editor.post(f"{cycle_url}/completion", body), "completion_evidence")
    assert _stored(RenewalCycle, renewal["cycle"]["id"]) == before


@pytest.mark.parametrize("field", ["review_interval_days", "renewal_lead_days"])
@pytest.mark.parametrize("value", [True, False, 12.5, "30.0", [30], {"days": 30}])
def test_organization_integer_settings_refuse_booleans_and_fractions(a, api, field, value):
    org = a["org"]
    before = (org.review_interval_days, org.renewal_lead_days)
    _assert_refused(api(a["users"]["admin"]).patch("organization", {field: value}), field)
    org.refresh_from_db()
    assert (org.review_interval_days, org.renewal_lead_days) == before


def test_null_still_clears_an_optional_text_field(a, api):
    editor = api(a["users"]["editor"])
    a["app"].description = "Old"
    a["app"].save()
    response = editor.patch(f"applications/{a['app'].id}", {"description": None, "purpose": None})
    assert response.status_code == 200
    a["app"].refresh_from_db()
    assert a["app"].description == ""
    assert api(a["users"]["admin"]).patch("organization", {"renewal_lead_days": "45"}).status_code == 200
    assert editor.patch(f"applications/{a['app'].id}", {"criticality": None}).status_code == 200


@pytest.mark.parametrize("value", [[], {}, 0, False])
@pytest.mark.parametrize("field", ["criticality", "data_classification", "built_by", "license_model"])
def test_record_choice_fields_refuse_empty_containers(a, api, field, value):
    """An empty list or object (or 0, false) for a choice is refused, not read as "clear it"; null still clears."""
    record = a["app"].id
    before = (_stored(Resource, record), _stored(Application, record))
    _assert_refused(api(a["users"]["editor"]).patch(f"applications/{record}", {field: value}), field)
    assert (_stored(Resource, record), _stored(Application, record)) == before


@pytest.mark.parametrize("label,value", WRONG)
@pytest.mark.parametrize("field", ["title", "description", "findings", "ticket_url"])
def test_assessment_text_fields_refuse_other_types(a, api, field, label, value):
    from catalog.models import ChangeAssessment as Assessment

    editor = api(a["users"]["editor"])
    body = {"target": str(a["app"].id), "change_type": "change", "title": "Move", "description": "Keep me"}
    assessment = editor.post("assessments", body).json_body["id"]
    before = _stored(Assessment, assessment)
    _assert_refused(editor.patch(f"assessments/{assessment}", {field: value}), field)
    assert _stored(Assessment, assessment) == before


@pytest.mark.parametrize("value", [[], {}, 0, False, 123])
def test_instance_status_refuses_other_types(a, api, value):
    from catalog.models import ApplicationInstance

    editor = api(a["users"]["editor"])
    base = f"applications/{a['app'].id}/instances"
    instance = editor.post(base, {"name": "Production", "status": "operational"}).json_body["id"]
    before = _stored(ApplicationInstance, instance)
    _assert_refused(editor.patch(f"{base}/{instance}", {"status": value}), "status")
    assert _stored(ApplicationInstance, instance) == before


@pytest.mark.parametrize("label,value", WRONG)
def test_contract_renew_action_refuses_non_text_evidence(a, label, value):
    from core.problems import FieldErrors
    from money import contracts, renewal_action

    org, editor = a["org"], a["users"]["editor"]
    term = {"start_date": "2026-04-01", "end_date": "2027-03-31", "auto_renew": "yes", "amount": "1200"}
    contract = contracts.create_contract(org, editor, {"name": "Support", "create_renewal_obligation": True, **term})
    before = _stored(Contract, contract.id)
    with pytest.raises(FieldErrors) as refused:
        renewal_action.renew(org, editor, contract, {"decision": "renew", "completion_evidence": value})
    assert "completion_evidence" in refused.value.errors
    assert _stored(Contract, contract.id) == before


# ---- The remaining fields that read a wrong JSON type as something else ---------------------------------------


@pytest.mark.parametrize("value", [0, False, [], {}, 5])
def test_tag_color_and_ai_model_modality_refuse_other_types(a, api, value):
    from catalog.models import AIModel, AIProvider, Tag

    editor = api(a["users"]["editor"])
    editor.patch(f"applications/{a['app'].id}", {"tags": ["finance"]})  # the tags list shows tags in use only
    tag = Tag.objects.get(organization=a["org"], name="finance")
    Tag.objects.filter(id=tag.id).update(color="blue")
    _assert_refused(editor.patch(f"tags/{tag.id}", {"color": value}), "color")
    assert Tag.objects.get(id=tag.id).color == "blue"
    provider = AIProvider.objects.create(organization=a["org"], name="Fabrikam")
    model = AIModel.objects.create(organization=a["org"], provider=provider, name="Large", modality="text")
    _assert_refused(editor.patch(f"ai-models/{model.id}", {"modality": value}), "modality")
    assert AIModel.objects.get(id=model.id).modality == "text"
    assert editor.patch(f"tags/{tag.id}", {"color": None}).status_code == 200  # null still clears


@pytest.mark.parametrize("value", ["false", 0, 1, None, []])
def test_person_and_member_active_take_a_boolean_only(a, api, value):
    person = Person.objects.create(organization=a["org"], name="Sam", active=True)
    _assert_refused(api(a["users"]["editor"]).patch(f"people/{person.id}", {"active": value}), "active")
    assert Person.objects.get(id=person.id).active is True
    viewer = a["users"]["viewer"]
    _assert_refused(api(a["users"]["admin"]).patch(f"users/{viewer.id}", {"active": value}), "active")
    viewer.refresh_from_db()
    assert viewer.is_active is True


@pytest.mark.parametrize("value", [5, False, [], {}])
def test_notice_unit_refuses_other_types(a, api, value):
    editor = api(a["users"]["editor"])
    contract = editor.post("contracts", {"name": "Support", "notice_value": 30, "notice_unit": "days"}).json_body["id"]
    before = _stored(Contract, contract)
    _assert_refused(editor.patch(f"contracts/{contract}", {"notice_value": None, "notice_unit": value}), "notice_unit")
    _assert_refused(editor.patch(f"contracts/{contract}", {"notice_unit": value}), "notice_unit")
    assert _stored(Contract, contract) == before


@pytest.mark.parametrize("label,value", WRONG)
def test_retirement_acknowledgment_types_are_checked_without_a_lifecycle_change(a, api, label, value):
    editor = api(a["users"]["editor"])
    record = a["app"].id
    before = _stored(Resource, record)
    _assert_refused(editor.patch(f"applications/{record}", {"retirement_ack_reason": value}), "retirement_ack_reason")
    if not isinstance(value, bool):
        _assert_refused(editor.patch(f"applications/{record}", {"retirement_ack": value}), "retirement_ack")
    assert _stored(Resource, record) == before
