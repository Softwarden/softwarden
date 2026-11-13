"""Custom fields: definitions and their rules, value
validation per type with field errors keyed custom_field_data.<name>, defaults and required, applies_to, the type,
choice and delete policies, cf_ filters, tenancy, audit snapshots and diff labels, CSV and Excel export, CSV import,
reminder sources, roles, the capability and query counts. FACTORIES in test_drf_resources.py covers the new route in
the cross-tenant, read-only token and If-Match sweeps."""

import csv
import datetime
import io
import threading
import time
from typing import Any

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from openpyxl import load_workbook

from catalog import admin_services, customfields, saved_filters, services
from catalog.audit_log import TARGET_TYPES
from catalog.models import CustomField, Resource, SavedFilter, Vendor
from core import connections
from core.models import AuditEvent
from core.problems import FieldErrors
from imports import importer
from imports.models import ImportRun

pytestmark = pytest.mark.django_db

RECORDS = ["application", "component", "integration", "ai_agent", "vendor"]


def field(f, name="business_case", label=None, type="text", applies_to=None, **extra):
    data = {
        "name": name,
        "label": label or name.replace("_", " ").capitalize(),
        "type": type,
        "applies_to": applies_to or ["application"],
        **extra,
    }
    return customfields.create_field(f["org"], f["users"]["admin"], data)


def app(f, name="Payroll", **data):
    return services.create_resource(
        f["org"], f["users"]["editor"], {"name": name, "kind": "application", "lifecycle": "active", **data}
    )


def pre(event) -> dict[str, Any]:
    assert event.prechange is not None
    return event.prechange


def post(event) -> dict[str, Any]:
    assert event.postchange is not None
    return event.postchange


def values(f, record):
    record.refresh_from_db()
    return record.custom_field_data


# ---- definitions --------------------------------------------------------------------------------------------------


def test_an_admin_defines_a_field_and_every_role_reads_the_definitions(a, api):
    admin, editor, viewer = (api(a["users"][r]) for r in ("admin", "editor", "viewer"))
    body = {
        "name": "risk_owner",
        "label": "Risk owner",
        "type": "select",
        "choices": ["Security", "Quality"],
        "default": "Quality",
        "applies_to": ["ai_agent", "application"],
        "weight": 10,
        "description": "Who signs off the risk.",
    }
    assert editor.post("custom-fields", body).status_code == 403
    assert viewer.post("custom-fields", body).status_code == 403
    created = admin.post("custom-fields", body)
    assert created.status_code == 201, created.json_body
    got = created.json_body
    assert got["applies_to"] == ["application", "ai_agent"]  # canonical order
    assert got["choices"] == ["Security", "Quality"] and got["default"] == "Quality" and got["usage"] == 0
    assert got["required"] is False and got["is_reminder_source"] is False and got["type"] == "select"
    assert created["ETag"]
    for client in (viewer, editor):
        listed = client.get("custom-fields")
        assert listed.status_code == 200 and [f["name"] for f in listed.json_body] == ["risk_owner"]
        assert client.get(f"custom-fields/{got['id']}").status_code == 200
    assert editor.patch(f"custom-fields/{got['id']}", {"label": "X"}).status_code == 403
    assert editor.delete(f"custom-fields/{got['id']}").status_code == 403
    event = AuditEvent.objects.get(action="custom_field.created")
    assert event.target_type == "CustomField" and "CustomField" in TARGET_TYPES
    assert post(event)["name"] == "risk_owner" and post(event)["choices"] == ["Security", "Quality"]


def test_definitions_are_ordered_by_weight_and_filtered_by_kind_and_type(a, api):
    field(a, "zeta", weight=1, applies_to=["component"])
    field(a, "alpha", weight=50, type="date")
    field(a, "beta", weight=50, applies_to=["vendor", "application"])
    client = api(a["users"]["viewer"])
    assert [f["name"] for f in client.get("custom-fields").json_body] == ["zeta", "alpha", "beta"]
    assert [f["name"] for f in client.get("custom-fields?applies_to=application").json_body] == ["alpha", "beta"]
    assert [f["name"] for f in client.get("custom-fields?type=date").json_body] == ["alpha"]
    assert [f["name"] for f in client.get("custom-fields?ordering=-name").json_body] == ["zeta", "beta", "alpha"]
    assert client.get("custom-fields?applies_to=person").status_code == 400
    assert client.get("custom-fields?colour=red").status_code == 400


@pytest.mark.parametrize(
    "data,key,message",
    [
        ({"name": ""}, "name", "Enter a name: lowercase letters, digits and underscores."),
        ({"name": "Business Case"}, "name", None),
        ({"name": "bad__name"}, "name", None),
        ({"name": "9lives"}, "name", None),
        ({"name": "api_token"}, "name", None),
        ({"name": "x" * 51}, "name", "Use 50 characters or fewer."),
        ({"label": ""}, "label", "Enter a label."),
        ({"type": "colour"}, "type", None),
        ({"type": "select"}, "choices", "A choice field needs at least one choice."),
        ({"type": "text", "choices": ["A"]}, "choices", "Only choice fields take choices."),
        ({"type": "select", "choices": ["A, B"]}, "choices", None),
        ({"type": "select", "choices": ["A", "a"]}, "choices", '"a" is listed twice.'),
        ({"type": "select", "choices": "A"}, "choices", "Send the choices as a list of text."),
        ({"is_reminder_source": True}, "is_reminder_source", "Only date fields can be reminder sources."),
        ({"applies_to": []}, "applies_to", "Choose at least one kind of record."),
        ({"applies_to": ["person"]}, "applies_to", None),
        ({"weight": -1}, "weight", "Use a whole number from 0 to 32767."),
        ({"required": "yes"}, "required", "Use true or false."),
        ({"type": "integer", "default": "many"}, "default", "Enter a whole number."),
        ({"type": "select", "choices": ["A"], "default": "B"}, "default", "Choose one of: A."),
        ({"description": "x" * 501}, "description", "Use 500 characters or fewer."),
    ],
)
def test_definitions_are_validated(a, api, data, key, message):
    body = {"name": "seats", "label": "Seats", "type": "text", "applies_to": ["application"], **data}
    response = api(a["users"]["admin"]).post("custom-fields", body)
    assert response.status_code == 400, response.json_body
    assert key in response.json_body["errors"], response.json_body
    if message:
        assert response.json_body["errors"][key] == [message]
    assert not CustomField.objects.exists()


def test_names_and_labels_are_unique_per_organization(a, b, api):
    field(a, "seats", label="Seats")
    field(b, "seats", label="Seats")  # another organization may use the same name
    client = api(a["users"]["admin"])
    clash = client.post(
        "custom-fields", {"name": "seats", "label": "Licences", "type": "text", "applies_to": ["vendor"]}
    )
    assert clash.json_body["errors"] == {"name": ["A custom field named seats already exists."]}
    label = client.post(
        "custom-fields", {"name": "licences", "label": "SEATS", "type": "text", "applies_to": ["vendor"]}
    )
    assert label.json_body["errors"] == {"label": ["The field seats already has the label Seats."]}


def test_the_name_never_changes_and_the_label_does(a, api):
    seats = field(a, "seats", label="Seats", type="integer")
    client = api(a["users"]["admin"])
    renamed = client.patch(f"custom-fields/{seats.id}", {"name": "licences"})
    assert renamed.status_code == 400
    assert renamed.json_body["errors"]["name"] == [
        "A field's name can't change after it is created; change its label instead."
    ]
    relabelled = client.patch(f"custom-fields/{seats.id}", {"name": "seats", "label": "Licensed seats"})
    assert relabelled.status_code == 200 and relabelled.json_body["label"] == "Licensed seats"
    event = AuditEvent.objects.get(action="custom_field.updated")
    assert event.changes == {"name": "seats", "label": ["Seats", "Licensed seats"]}
    assert pre(event)["label"] == "Seats" and post(event)["label"] == "Licensed seats"
    log = api(a["users"]["admin"]).get("audit-events?target_type=CustomField").json_body["results"]
    assert log[0]["label"] == "Custom field changed" and "Label: Seats → Licensed seats" in log[0]["summary"]


# ---- values ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "type_,extra,good,stored,bad,message",
    [
        ("text", {}, "  Hello  ", "Hello", 5, "Enter text."),
        ("text", {}, "Hello", "Hello", "two\nlines", "Use one line; a long text field takes more."),
        ("text", {}, "x" * 500, "x" * 500, "x" * 501, "Use 500 characters or fewer."),
        ("longtext", {}, "two\nlines", "two\nlines", "x" * 5001, "Use 5,000 characters or fewer."),
        ("integer", {}, 250, 250, "lots", "Enter a whole number."),
        ("integer", {}, "42", 42, True, "Enter a whole number."),
        ("integer", {}, -7, -7, 2**53, "Use a whole number from -9,007,199,254,740,991 to 9,007,199,254,740,991."),
        ("decimal", {}, "12.50", 12.5, "abc", "Enter a number, such as 12.5."),
        ("decimal", {}, 3, 3, "1.1234567", "Use at most 6 decimal places."),
        ("decimal", {}, 0.000001, 0.000001, "1234567890123456", "Use at most 15 digits."),
        ("boolean", {}, False, False, "yes", "Use true or false, or leave it empty."),
        ("date", {}, "2026-01-31", "2026-01-31", "31/01/2026", "Enter a date as YYYY-MM-DD."),
        ("date", {}, "2028-02-29", "2028-02-29", "2026-02-30", "Enter a date as YYYY-MM-DD."),
        ("url", {}, "https://example.test/x", "https://example.test/x", "ftp://x.test", customfields.URL_MESSAGE),
        ("select", {"choices": ["Low", "High"]}, "High", "High", "Medium", "Choose one of: Low, High."),
        (
            "multiselect",
            {"choices": ["US", "EU", "APAC"]},
            ["APAC", "US", "US"],
            ["US", "APAC"],
            ["US", "Mars"],
            "Choose any of: US, EU, APAC.",
        ),
        ("multiselect", {"choices": ["US"]}, ["US"], ["US"], "US", "Send the choices as a list."),
        ("object", {}, {"tier": {"level": 2}}, {"tier": {"level": 2}}, ["not", "object"], "Send a JSON object."),
        ("object", {}, {"a": 1}, {"a": 1}, {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}, "Nest at most 5 levels."),
        ("object", {}, {"a": 1}, {"a": 1}, {"a": "x" * 5000}, "Keep the object under 5,000 characters of JSON."),
        ("object", {}, {"a": 1}, {"a": 1}, {"vendor": {"api_key": "x"}}, customfields.DENIED_KEYS_MESSAGE),
    ],
)
def test_each_type_validates_and_stores_its_values(a, api, type_, extra, good, stored, bad, message):
    field(a, "f", label="F", type=type_, **extra)
    client = api(a["users"]["editor"])
    url = f"applications/{a['app'].id}"
    ok = client.patch(url, {"custom_field_data": {"f": good}})
    assert ok.status_code == 200, ok.json_body
    assert ok.json_body["custom_field_data"] == {"f": stored}
    assert values(a, a["app"]) == {"f": stored}
    refused = client.patch(url, {"custom_field_data": {"f": bad}})
    assert refused.status_code == 400
    assert refused.json_body["errors"] == {"custom_field_data.f": [message]}
    assert values(a, a["app"]) == {"f": stored}  # nothing changed


def test_errors_are_field_errors_keyed_by_field_beside_the_records_own(a, api):
    """The FieldErrors problem shape every endpoint uses, so the SPA can show each one inline."""
    field(a, "seats", type="integer")
    field(a, "tier", type="select", choices=["Gold"])
    response = api(a["users"]["editor"]).post(
        "applications",
        {"name": "", "lifecycle": "active", "custom_field_data": {"seats": "x", "tier": "Lead", "nope": 1}},
    )
    assert response.status_code == 400 and response["Content-Type"] == "application/problem+json"
    body = response.json_body
    assert body["title"] == "Invalid request" and body["status"] == 400
    assert body["errors"] == {"name": ["Enter a name."]}  # name and lifecycle are checked first
    response = api(a["users"]["editor"]).post(
        "applications",
        {"name": "Box", "lifecycle": "active", "custom_field_data": {"seats": "x", "tier": "Lead", "nope": 1}},
    )
    assert response.json_body["errors"] == {
        "custom_field_data.seats": ["Enter a whole number."],
        "custom_field_data.tier": ["Choose one of: Gold."],
        "custom_field_data.nope": ["There is no custom field with this name."],
    }
    not_object = api(a["users"]["editor"]).patch(f"applications/{a['app'].id}", {"custom_field_data": ["seats"]})
    assert not_object.json_body["errors"] == {
        "custom_field_data": ["Send custom fields as an object of field names and values."]
    }
    assert not Resource.objects.filter(name="Box").exists()


def test_a_field_applies_only_to_its_kinds(a, api):
    field(a, "seats", label="Seats", type="integer", applies_to=["component", "vendor"])
    client = api(a["users"]["editor"])
    refused = client.patch(f"applications/{a['app'].id}", {"custom_field_data": {"seats": 3}})
    assert refused.json_body["errors"] == {"custom_field_data.seats": ["Seats doesn't apply to applications."]}
    assert client.get(f"applications/{a['app'].id}").json_body["custom_field_data"] == {}
    job = client.post("components", {"name": "Job", "lifecycle": "active", "custom_field_data": {"seats": 3}})
    assert job.status_code == 201 and job.json_body["custom_field_data"] == {"seats": 3}


def test_defaults_fill_new_records_and_required_fields_need_a_value(a, api):
    field(a, "tier", type="select", choices=["Gold", "Silver"], default="Silver")
    field(a, "ticket", label="Ticket", required=True)
    client = api(a["users"]["editor"])
    missing = client.post("applications", {"name": "Box", "lifecycle": "active"})
    assert missing.status_code == 400
    assert missing.json_body["errors"] == {"custom_field_data.ticket": ["Ticket is required."]}
    created = client.post("applications", {"name": "Box", "lifecycle": "active", "custom_field_data": {"ticket": "T1"}})
    assert created.status_code == 201
    assert created.json_body["custom_field_data"] == {"tier": "Silver", "ticket": "T1"}
    chosen = client.post(
        "applications",
        {"name": "Crate", "lifecycle": "active", "custom_field_data": {"ticket": "T2", "tier": None}},
    )
    assert chosen.json_body["custom_field_data"] == {
        "tier": None,
        "ticket": "T2",
    }  # an explicit empty beats the default
    url = f"applications/{created.json_body['id']}"
    cleared = client.patch(url, {"custom_field_data": {"ticket": ""}})
    assert cleared.json_body["errors"] == {"custom_field_data.ticket": ["Ticket is required."]}
    # The fixture's application predates the requirement: other edits still go through.
    assert client.patch(f"applications/{a['app'].id}", {"description": "Still editable"}).status_code == 200


def test_a_patch_merges_by_name_and_null_clears(a, api):
    field(a, "seats", type="integer")
    field(a, "region", type="text")
    client = api(a["users"]["editor"])
    url = f"applications/{a['app'].id}"
    client.patch(url, {"custom_field_data": {"seats": 5, "region": "EU"}})
    assert client.patch(url, {"custom_field_data": {"seats": 6}}).json_body["custom_field_data"] == {
        "seats": 6,
        "region": "EU",
    }
    assert client.patch(url, {"custom_field_data": {"region": None}}).json_body["custom_field_data"] == {
        "seats": 6,
        "region": None,
    }
    assert values(a, a["app"]) == {"seats": 6}  # an unset value has no key
    assert client.patch(url, {"name": "Renamed"}).json_body["custom_field_data"]["seats"] == 6


@pytest.mark.parametrize("kind,path", [("component", "components"), ("ai_agent", "ai-agents")])
def test_records_show_the_fields_of_their_kind_in_weight_order(a, api, kind, path):
    field(a, "zulu", weight=1, applies_to=[kind])
    field(a, "alpha", weight=2, applies_to=[kind, "application"])
    field(a, "apps_only", weight=0)
    client = api(a["users"]["editor"])
    created = client.post(path, {"name": "Thing", "lifecycle": "active", "custom_field_data": {"alpha": "A"}})
    assert created.status_code == 201, created.json_body
    assert list(created.json_body["custom_field_data"].items()) == [("zulu", None), ("alpha", "A")]
    row = client.get(path).json_body["results"][0]
    assert row["custom_field_data"] == {"zulu": None, "alpha": "A"}
    assert list(client.get("applications").json_body["results"][0]["custom_field_data"]) == ["apps_only", "alpha"]


def test_integrations_and_vendors_carry_custom_fields(a, api):
    field(a, "sla", label="SLA", type="text", applies_to=["integration", "vendor"])
    client = api(a["users"]["editor"])
    job = client.post("components", {"name": "Job", "lifecycle": "active"}).json_body
    feed = client.post(
        "integrations",
        {
            "name": "Feed",
            "lifecycle": "active",
            "source": str(a["app"].id),
            "target": job["id"],
            "custom_field_data": {"sla": "4 hours"},
        },
    )
    assert feed.status_code == 201 and feed.json_body["custom_field_data"] == {"sla": "4 hours"}
    assert client.get("integrations?cf_sla=hour").json_body["results"][0]["name"] == "Feed"
    vendor = client.post("vendors", {"name": "Fabrikam", "custom_field_data": {"sla": "Next day"}})
    assert vendor.status_code == 201 and vendor.json_body["custom_field_data"] == {"sla": "Next day"}
    changed = client.patch(f"vendors/{vendor.json_body['id']}", {"custom_field_data": {"sla": "Same day"}})
    assert changed.json_body["custom_field_data"] == {"sla": "Same day"}
    assert [v["name"] for v in client.get("vendors?cf_sla=same").json_body] == ["Fabrikam"]
    assert client.get("vendors?cf_nope=x").status_code == 400
    event = AuditEvent.objects.filter(action="vendor.updated").latest("created_at")
    assert event.changes["custom_field_data.sla"] == ["Next day", "Same day"]
    assert pre(event)["custom_field_data.sla"] == "Next day"
    assert post(event)["custom_field_data.sla"] == "Same day"
    bad = client.post("vendors", {"name": "Contoso", "custom_field_data": {"sla": 5}})
    assert bad.json_body["errors"] == {"custom_field_data.sla": ["Enter text."]}


def test_a_value_change_moves_the_etag(a, api):
    field(a, "seats", type="integer")
    client = api(a["users"]["editor"])
    url = f"applications/{a['app'].id}"
    seen = client.get(url)["ETag"]
    client.patch(url, {"custom_field_data": {"seats": 3}})
    stale = client.client.patch(
        "/api/v1/" + url,
        '{"custom_field_data": {"seats": 4}}',
        content_type="application/json",
        HTTP_IF_MATCH=seen,
    )
    assert stale.status_code == 412 and values(a, a["app"]) == {"seats": 3}


# ---- changing and deleting a field ---------------------------------------------------------------------------------


def test_the_type_changes_only_while_no_record_holds_a_value(a, api):
    seats = field(a, "seats", label="Seats", type="text", applies_to=["application", "component"])
    client = api(a["users"]["admin"])
    url = f"custom-fields/{seats.id}"
    assert client.patch(url, {"type": "integer"}).status_code == 200  # unused: allowed
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"custom_field_data": {"seats": 4}})
    refused = client.patch(url, {"type": "text"})
    assert refused.status_code == 400
    assert refused.json_body["errors"]["type"] == [
        "1 record hold a value for Seats, so its type can't change. Clear the values first, or add a new field."
    ]
    assert CustomField.objects.get(id=seats.id).type == "integer"
    removed = client.patch(url, {"applies_to": ["component"]})
    assert removed.json_body["errors"]["applies_to"] == [
        "Values exist for 1 of the applications. Clear them before removing the kind."
    ]
    assert client.patch(url, {"applies_to": ["application"]}).status_code == 200  # no component holds one


def test_a_choice_in_use_cant_be_removed_but_choices_can_be_added(a, api):
    tier = field(a, "tier", label="Tier", type="multiselect", choices=["Gold", "Silver"])
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"custom_field_data": {"tier": ["Gold"]}})
    client = api(a["users"]["admin"])
    url = f"custom-fields/{tier.id}"
    refused = client.patch(url, {"choices": ["Silver", "Bronze"]})
    assert refused.json_body["errors"]["choices"] == [
        "Still in use: Gold (1 record). Change those records before removing the choice."
    ]
    added = client.patch(url, {"choices": ["Gold", "Silver", "Bronze"]})
    assert added.status_code == 200 and added.json_body["choices"] == ["Gold", "Silver", "Bronze"]
    assert client.patch(url, {"choices": ["Gold", "Bronze"]}).status_code == 200  # Silver was unused


def test_a_default_that_no_longer_fits_is_refused(a, api):
    tier = field(a, "tier", type="select", choices=["Gold", "Silver"], default="Silver")
    refused = api(a["users"]["admin"]).patch(f"custom-fields/{tier.id}", {"choices": ["Gold"]})
    assert refused.json_body["errors"]["default"] == ["The default no longer fits: Choose one of: Gold."]


def test_deleting_a_field_removes_its_values_and_is_audited_once(a, api):
    seats = field(a, "seats", label="Seats", type="integer", applies_to=["application", "vendor"])
    keep = field(a, "region")
    other = app(a, "Other", custom_field_data={"seats": 9, "region": "EU"})
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"custom_field_data": {"seats": 4}})
    vendor = admin_services.create_named(
        Vendor, a["org"], a["users"]["editor"], {"name": "Fabrikam", "custom_field_data": {"seats": 1}}
    )
    client = api(a["users"]["admin"])
    assert {f["name"]: f["usage"] for f in client.get("custom-fields").json_body} == {"seats": 3, "region": 1}
    assert client.delete(f"custom-fields/{seats.id}").status_code == 204
    assert values(a, a["app"]) == {} and values(a, other) == {"region": "EU"}
    vendor.refresh_from_db()
    assert vendor.custom_field_data == {}
    event = AuditEvent.objects.get(action="custom_field.deleted")
    assert event.changes == {"name": "seats", "label": "Seats", "cleared": 3, "saved_filters_changed": 0}
    assert pre(event)["name"] == "seats" and event.postchange is None
    log = client.get("audit-events?target_type=CustomField").json_body["results"]
    assert log[0]["summary"] == "Seats; values removed from 3 records"
    assert CustomField.objects.filter(id=keep.id).exists()
    # The name is free again, for any type.
    assert field(a, "seats", label="Seats", type="date").type == "date"


# ---- filters ----------------------------------------------------------------------------------------------------------


@pytest.fixture
def stocked(a):
    field(a, "notes_cf", label="Notes", type="longtext")
    field(a, "site", label="Site", type="url")
    field(a, "seats", type="integer")
    field(a, "cost", type="decimal")
    field(a, "sox", label="SOX scope", type="boolean")
    field(a, "renewal", type="date")
    field(a, "tier", type="select", choices=["Gold", "Silver", "Bronze"])
    field(a, "regions", type="multiselect", choices=["US", "EU", "APAC"])
    field(a, "extra", type="object")
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"name": "Bare"})
    app(
        a,
        "Alpha",
        custom_field_data={
            "notes_cf": "Runs the Payroll export",
            "site": "https://alpha.example.test",
            "seats": 250,
            "cost": 12.5,
            "sox": True,
            "renewal": "2026-03-31",
            "tier": "Gold",
            "regions": ["US", "EU"],
            "extra": {"k": 1},
        },
    )
    app(
        a,
        "Beta",
        custom_field_data={
            "notes_cf": "Nothing to see",
            "seats": 5,
            "cost": 99.95,
            "sox": False,
            "renewal": "2027-01-15",
            "tier": "Silver",
            "regions": ["APAC"],
        },
    )
    return a


@pytest.mark.parametrize(
    "query,expected",
    [
        ("cf_notes_cf=payroll", {"Alpha"}),
        ("cf_site=ALPHA.example", {"Alpha"}),
        ("cf_seats=250", {"Alpha"}),
        ("cf_seats__gte=6", {"Alpha"}),
        ("cf_seats__lte=250&cf_seats__gte=5", {"Alpha", "Beta"}),
        ("cf_cost=12.50", {"Alpha"}),
        ("cf_cost__gte=50", {"Beta"}),
        ("cf_sox=true", {"Alpha"}),
        ("cf_sox=false", {"Beta"}),
        ("cf_renewal=2026-03-31", {"Alpha"}),
        ("cf_renewal__gte=2026-12-31", {"Beta"}),
        ("cf_renewal__lte=2026-12-31", {"Alpha"}),
        ("cf_tier=Gold", {"Alpha"}),
        ("cf_tier=Gold,Silver", {"Alpha", "Beta"}),
        ("cf_regions=EU", {"Alpha"}),
        ("cf_regions=APAC,EU", {"Alpha", "Beta"}),
        ("cf_tier__set=false", {"Bare"}),
        ("cf_extra__set=true", {"Alpha"}),
        ("cf_seats__gte=6&tag=none", set()),
    ],
)
def test_filters_per_type(stocked, api, query, expected):
    response = api(stocked["users"]["viewer"]).get(f"applications?{query}")
    assert response.status_code == 200, response.json_body
    assert {r["name"] for r in response.json_body["results"]} == expected


@pytest.mark.parametrize(
    "query,param,message",
    [
        ("cf_nope=x", "cf_nope", "This filter isn't available here."),
        ("cf_extra=x", "cf_extra", "This filter isn't available here."),  # objects aren't filtered
        ("cf_sox__gte=1", "cf_sox__gte", "This filter isn't available here."),
        ("cf_seats=many", "cf_seats", "Enter a whole number."),
        ("cf_renewal__gte=31/12/2026", "cf_renewal__gte", "Enter a date as YYYY-MM-DD."),
        ("cf_tier=Lead", "cf_tier", "Use one or more of: Gold, Silver, Bronze (comma-separated)."),
        ("cf_sox=maybe", "cf_sox", "Use one of: true, false."),
    ],
)
def test_bad_filters_are_refused_like_any_parameter(stocked, api, query, param, message):
    response = api(stocked["users"]["viewer"]).get(f"applications?{query}")
    assert response.status_code == 400
    assert response.json_body["errors"] == {param: [message]}


def test_a_field_of_another_kind_is_no_filter_here(stocked, api):
    field(stocked, "build", applies_to=["component"])
    client = api(stocked["users"]["viewer"])
    assert client.get("applications?cf_build=x").status_code == 400
    assert client.get("components?cf_build=x").status_code == 200


def test_export_and_saved_filters_take_the_custom_filters(stocked, api):
    client = api(stocked["users"]["editor"])
    response = client.client.get("/api/v1/applications/export.csv?cf_tier=Gold")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert [r["name"] for r in rows] == ["Alpha"]
    saved = client.post("saved-filters", {"name": "Gold", "kind": "application", "query": {"cf_tier": "Gold"}})
    assert saved.status_code == 201, saved.json_body
    refused = client.post("saved-filters", {"name": "Bad", "kind": "application", "query": {"cf_tier": "Lead"}})
    assert refused.status_code == 400
    assert refused.json_body["errors"]["query"] == [
        "cf_tier: Use one or more of: Gold, Silver, Bronze (comma-separated)."
    ]
    with pytest.raises(FieldErrors):
        saved_filters.clean_query({"cf_tier": "Gold"}, "application")  # no organization: no custom filters


# ---- tenancy --------------------------------------------------------------------------------------------------------


def test_another_organizations_fields_never_apply(a, b, api):
    theirs = field(b, "sauce_level", label="Sauce", type="integer")
    mine = api(a["users"]["admin"])
    assert mine.get("custom-fields").json_body == []
    for method, payload in (("get", None), ("patch", {"label": "Mine now"}), ("delete", None)):
        call = getattr(mine, method)
        response = call(f"custom-fields/{theirs.id}", payload) if payload else call(f"custom-fields/{theirs.id}")
        assert response.status_code == 404
    theirs.refresh_from_db()
    assert theirs.label == "Sauce"
    refused = mine.patch(f"applications/{a['app'].id}", {"custom_field_data": {"sauce_level": 3}})
    assert refused.json_body["errors"] == {
        "custom_field_data.sauce_level": ["There is no custom field with this name."]
    }
    assert mine.get("applications?cf_sauce_level=3").status_code == 400
    assert mine.get(f"applications/{a['app'].id}").json_body["custom_field_data"] == {}
    # Same name in both organizations, different types: each validates against its own.
    field(a, "sauce_level", label="Sauce", type="text")
    assert mine.patch(f"applications/{a['app'].id}", {"custom_field_data": {"sauce_level": "hot"}}).status_code == 200
    services.update_resource(b["org"], b["users"]["editor"], b["app"], {"custom_field_data": {"sauce_level": 7}})
    # Filters on one organization's values never read the other's (text in a, a number in b).
    assert [r["name"] for r in mine.get("applications?cf_sauce_level=hot").json_body["results"]] == ["orga App"]
    other = api(b["users"]["viewer"]).get("applications?cf_sauce_level__gte=5").json_body["results"]
    assert [r["name"] for r in other] == ["orgb App"]


# ---- audit ------------------------------------------------------------------------------------------------------------


def test_snapshots_and_diffs_name_fields_by_their_label(a, api):
    field(a, "seats", label="Licensed seats", type="integer")
    field(a, "brief", label="Brief", type="longtext")
    field(a, "regions", label="Regions", type="multiselect", choices=["US", "EU"])
    client = api(a["users"]["editor"])
    url = f"applications/{a['app'].id}"
    client.patch(url, {"custom_field_data": {"seats": 5, "brief": "Short", "regions": ["US"]}})
    client.patch(url, {"custom_field_data": {"seats": 10, "brief": "Longer", "regions": ["US", "EU"]}})
    event = AuditEvent.objects.filter(action="resource.updated", target_id=a["app"].id).latest("created_at")
    assert event.changes["custom_field_data.seats"] == [5, 10]
    assert pre(event)["custom_field_data.brief"] == "Short"
    assert post(event)["custom_field_data.regions"] == ["US", "EU"]
    summary = client.get(f"{url}/audit").json_body["results"][0]["summary"]
    assert "Licensed seats: 5 → 10" in summary and "Brief changed" in summary and "Regions: US → US, EU" in summary
    diff = {r["field"]: r for r in client.get(f"{url}/audit/{event.id}").json_body["diff"]}
    assert diff["custom_field_data.seats"]["label"] == "Licensed seats"
    assert diff["custom_field_data.brief"]["before"] == "Short" and diff["custom_field_data.brief"]["after"] == "Longer"
    assert diff["custom_field_data.regions"]["added"] == ["EU"]
    # A relabel shows on old events; a deleted field falls back to its name in words.
    seats = CustomField.objects.get(name="seats")
    customfields.update_field(a["org"], a["users"]["admin"], seats, {"label": "Seats"})
    diff = {r["field"]: r for r in client.get(f"{url}/audit/{event.id}").json_body["diff"]}
    assert diff["custom_field_data.seats"]["label"] == "Seats"
    customfields.delete_field(a["org"], a["users"]["admin"], seats)
    diff = {r["field"]: r for r in client.get(f"{url}/audit/{event.id}").json_body["diff"]}
    assert diff["custom_field_data.seats"]["label"] == "Seats"  # "seats" in words


def test_snapshots_hold_only_declared_fields_and_no_secret_looking_object(a):
    field(a, "extra", type="object")
    record = app(a, "Fresh")
    # A stray key (not a field) and an object holding a secret-looking key, written behind the service's back.
    Resource.objects.filter(id=record.id).update(
        custom_field_data={"stray": "x", "extra": {"nested": {"password": "hunter2"}}}
    )
    record.refresh_from_db()
    services.update_resource(a["org"], a["users"]["editor"], record, {"description": "Touched"})
    event = AuditEvent.objects.filter(action="resource.updated").latest("created_at")
    assert "custom_field_data.stray" not in pre(event) and "custom_field_data.extra" not in pre(event)
    assert not any("hunter2" in str(v) for v in (event.prechange, event.postchange, event.changes))


def test_object_values_with_secret_like_keys_are_refused(a, api):
    field(a, "extra", type="object")
    response = api(a["users"]["editor"]).patch(
        f"applications/{a['app'].id}", {"custom_field_data": {"extra": {"Client_Secret": "x"}}}
    )
    assert response.json_body["errors"] == {"custom_field_data.extra": [customfields.DENIED_KEYS_MESSAGE]}


# ---- CSV and Excel -----------------------------------------------------------------------------------------------------


def export_rows(client, query=""):
    response = client.client.get(f"/api/v1/applications/export.csv{query}")
    assert response.status_code == 200
    return response.content, list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


def test_csv_export_and_template_have_a_column_per_field(stocked, api):
    client = api(stocked["users"]["editor"])
    _, rows = export_rows(client)
    alpha = next(r for r in rows if r["name"] == "Alpha")
    assert alpha["cf_seats"] == "250" and alpha["cf_cost"] == "12.5" and alpha["cf_sox"] == "true"
    assert alpha["cf_regions"] == "US; EU" and alpha["cf_renewal"] == "2026-03-31" and alpha["cf_extra"] == '{"k":1}'
    assert next(r for r in rows if r["name"] == "Bare")["cf_tier"] == ""
    template = client.client.get("/api/v1/applications/template.csv").content.decode("utf-8-sig")
    assert template.strip().split(",")[-9:] == [  # weight, then name (all 100 here)
        "cf_cost",
        "cf_extra",
        "cf_notes_cf",
        "cf_regions",
        "cf_renewal",
        "cf_seats",
        "cf_site",
        "cf_sox",
        "cf_tier",
    ]
    components = client.client.get("/api/v1/components/template.csv").content.decode("utf-8-sig")
    assert "cf_" not in components


def test_excel_export_types_custom_columns_and_adds_the_custom_fields_sheet(stocked, api):
    response = api(stocked["users"]["viewer"]).client.get("/api/v1/applications/export.xlsx")
    assert response.status_code == 200
    wb = load_workbook(io.BytesIO(response.content))
    assert wb.sheetnames == ["Applications", "Custom fields"]
    sheet = wb["Applications"]
    header = [c.value for c in sheet[1]]
    rows = {r[header.index("name")].value: r for r in sheet.iter_rows(min_row=2)}
    alpha = rows["Alpha"]
    assert alpha[header.index("cf_seats")].value == 250 and alpha[header.index("cf_cost")].value == 12.5
    assert alpha[header.index("cf_sox")].value is True
    renewal = alpha[header.index("cf_renewal")].value
    assert isinstance(renewal, datetime.datetime) and renewal.date().isoformat() == "2026-03-31"
    assert alpha[header.index("cf_regions")].value == "US; EU"
    legend = wb["Custom fields"]
    assert [c.value for c in legend[1]] == [
        "column",
        "label",
        "type",
        "required",
        "choices",
        "default",
        "applies_to",
        "description",
    ]
    first = [c.value for c in legend[2]]
    assert first[:3] == ["cf_cost", "Cost", "Decimal number"] and first[6] == "applications"
    plain = api(stocked["users"]["viewer"]).client.get("/api/v1/components/export.xlsx")
    assert load_workbook(io.BytesIO(plain.content)).sheetnames == ["Components"]  # no fields, no second sheet


def test_an_export_re_imports_unchanged_and_edited_cells_update(stocked, api):
    client = api(stocked["users"]["editor"])
    content, rows = export_rows(client)
    run = importer.create_run(stocked["org"], stocked["users"]["editor"], "application", "x.csv", content)
    assert run.status == "previewed" and run.counts["unchanged"] == 3 and run.counts["error"] == 0, run.counts
    edited = io.StringIO()
    writer = csv.DictWriter(edited, fieldnames=list(rows[0]))
    writer.writeheader()
    for row in rows:
        if row["name"] == "Alpha":
            row.update({"cf_seats": "300", "cf_regions": "EU;APAC", "cf_sox": "no", "cf_tier": ""})
        writer.writerow(row)
    run = importer.create_run(
        stocked["org"], stocked["users"]["editor"], "application", "y.csv", edited.getvalue().encode()
    )
    assert run.counts["update"] == 1 and run.counts["unchanged"] == 2, run.counts
    row = run.rows.get(outcome="update")
    assert row.changes == {
        "cf_seats": ["250", "300"],
        "cf_sox": ["true", "false"],
        "cf_regions": ["US; EU", "EU; APAC"],
    }  # a blank cell leaves the value
    importer.commit_run(stocked["org"], stocked["users"]["editor"], run)
    alpha = Resource.objects.get(name="Alpha")
    assert alpha.custom_field_data["seats"] == 300 and alpha.custom_field_data["regions"] == ["EU", "APAC"]
    assert alpha.custom_field_data["tier"] == "Gold" and alpha.custom_field_data["sox"] is False
    event = AuditEvent.objects.filter(action="resource.updated", target_id=alpha.id).latest("created_at")
    assert event.changes["custom_field_data.seats"] == [250, 300] and event.changes["import_run"] == str(run.id)


def test_import_rows_with_bad_custom_cells_are_row_errors(a):
    field(a, "seats", label="Seats", type="integer")
    field(a, "ticket", label="Ticket", required=True)
    field(a, "tier", label="Tier", type="select", choices=["Gold"], default="Gold")
    data = (
        b"name,lifecycle,cf_seats,cf_ticket,cf_tier\n"
        b"Good,active,12,T-1,\n"
        b"Bad seats,active,lots,T-2,\n"
        b"No ticket,active,3,,\n"
        b"Bad tier,active,3,T-3,Lead\n"
    )
    run = importer.create_run(a["org"], a["users"]["editor"], "application", "x.csv", data)
    assert run.status == "previewed"  # a template file: cf_ columns are template columns
    errors = {r.raw["name"]: r.errors for r in run.rows.all()}
    assert errors["Bad seats"][0] == {"level": "error", "column": "cf_seats", "message": "Seats: Enter a whole number."}
    assert errors["No ticket"][0] == {"level": "error", "column": "cf_ticket", "message": "Ticket is required."}
    assert errors["Bad tier"][0]["message"] == "Tier: Choose one of: Gold."
    good = run.rows.get(row_number=2)
    assert good.outcome == "create" and good.changes["cf_seats"] == ["", "12"]
    importer.update_run(a["org"], a["users"]["editor"], run, {"exclude": [3, 4, 5]})
    importer.commit_run(a["org"], a["users"]["editor"], ImportRun.objects.get(id=run.id))
    assert Resource.objects.get(name="Good").custom_field_data == {"seats": 12, "ticket": "T-1", "tier": "Gold"}


def test_mapping_offers_custom_fields_by_label(a, api):
    field(a, "business_case", label="Business case", type="longtext")
    data = b"App name,Business case,Stage\nPortal,Saves time,active\n"
    run = importer.create_run(a["org"], a["users"]["editor"], "application", "x.csv", data)
    body = api(a["users"]["editor"]).get(f"imports/{run.id}").json_body
    assert body["needs_mapping"] is True
    assert body["suggested_mapping"]["Business case"] == "cf_business_case"
    assert {"value": "cf_business_case", "label": "Business case", "required": False} in body["fields"]
    mapped = importer.map_run(
        a["org"],
        a["users"]["editor"],
        run,
        {"mapping": {"App name": "name", "Business case": "cf_business_case", "Stage": "lifecycle"}},
    )
    assert mapped.counts["create"] == 1
    importer.commit_run(a["org"], a["users"]["editor"], mapped)
    assert Resource.objects.get(name="Portal").custom_field_data == {"business_case": "Saves time"}


def test_import_creates_vendors_by_name_even_when_a_vendor_field_is_required(a):
    field(a, "vendor_tier", label="Vendor tier", required=True, applies_to=["vendor"])
    data = b"name,lifecycle,vendor\nPortal,active,Brand new vendor\n"
    run = importer.create_run(a["org"], a["users"]["editor"], "application", "x.csv", data)
    importer.update_run(a["org"], a["users"]["editor"], run, {"decisions": {"vendors": {"brand new vendor": "create"}}})
    importer.commit_run(a["org"], a["users"]["editor"], ImportRun.objects.get(id=run.id))
    assert Vendor.objects.get(name="Brand new vendor").custom_field_data == {}


# ---- discovery, reminders, capability, queries ------------------------------------------------------------------------


def test_a_record_created_from_discovery_takes_values_and_defaults(a, api):
    from discovery import engine as discovery
    from discovery.models import Observation

    field(a, "tier", type="select", choices=["Gold", "Silver"], default="Silver")
    field(a, "ticket", label="Ticket", required=True)
    connector = discovery.create_connector(
        a["org"], a["users"]["admin"], {"tenant_id": "simulated", "client_id": "", "secret": "synthetic-secret"}
    )
    connections.run_test(
        a["org"], a["users"]["admin"], connector.microsoft_connection
    )  # a run needs a tested connection
    discovery.run(connector)
    obs = Observation.objects.filter(connector=connector).earliest("display_name")
    client = api(a["users"]["editor"])
    refused = client.post(f"discovery/observations/{obs.id}/create", {})
    assert refused.status_code == 400 and refused.json_body["errors"] == {
        "custom_field_data.ticket": ["Ticket is required."]
    }
    made = client.post(f"discovery/observations/{obs.id}/create", {"custom_field_data": {"ticket": "CHG-1"}})
    assert made.status_code in (200, 201), made.json_body
    created = Resource.objects.get(organization=a["org"], name=obs.display_name[:200])
    assert created.custom_field_data == {"tier": "Silver", "ticket": "CHG-1"}


def test_reminder_sources_list_flagged_dates_soonest_first(a):
    flagged = field(
        a, "cert_expiry", label="Certificate expiry", type="date", is_reminder_source=True, applies_to=RECORDS
    )
    field(a, "launch", type="date")  # not a reminder source
    later = app(a, "Later", custom_field_data={"cert_expiry": "2027-06-01", "launch": "2026-01-01"})
    sooner = app(a, "Sooner", custom_field_data={"cert_expiry": "2026-12-01"})
    gone = app(a, "Gone", custom_field_data={"cert_expiry": "2026-01-01"})
    services.archive_resource(a["org"], a["users"]["editor"], gone)
    vendor = admin_services.create_named(
        Vendor, a["org"], a["users"]["editor"], {"name": "Fabrikam", "custom_field_data": {"cert_expiry": "2026-12-15"}}
    )
    found = customfields.reminder_sources(a["org"])
    assert [(s.record.pk, s.field.id, s.due.isoformat()) for s in found] == [
        (sooner.id, flagged.id, "2026-12-01"),
        (vendor.id, flagged.id, "2026-12-15"),
        (later.id, flagged.id, "2027-06-01"),
    ]


def test_every_custom_field_route_needs_the_capability(a, api, monkeypatch):
    from core import capabilities as caps

    seats = field(a, "seats")
    monkeypatch.setitem(caps.EDITION_CAPABILITIES, "community", caps.COMMUNITY - {caps.CUSTOM_FIELDS})
    client = api(a["users"]["admin"])
    for response in (
        client.get("custom-fields"),
        client.get(f"custom-fields/{seats.id}"),
        client.post("custom-fields"),
    ):
        assert response.status_code == 403 and response.json_body["capability"] == "custom_fields"
    assert api(a["users"]["viewer"]).post("custom-fields").json_body.get("capability") is None  # role first


@pytest.mark.parametrize("path", ["applications", "components", "ai-agents", "integrations", "vendors"])
def test_custom_fields_add_no_query_per_row(a, api, path):
    kind = {"applications": "application", "components": "component", "ai-agents": "ai_agent"}.get(path)
    kind = kind or path.rstrip("s")
    for i in range(3):
        field(a, f"f{i}", type="integer", applies_to=[kind])
    client = api(a["users"]["viewer"])

    def make(i):
        data = {"custom_field_data": {"f0": i, "f1": i}}
        if path == "vendors":
            admin_services.create_named(Vendor, a["org"], a["users"]["editor"], {"name": f"Vendor {i}", **data})
        elif path == "integrations":
            source = app(a, f"Source {i}")
            target = app(a, f"Target {i}")
            services.create_integration(
                a["org"],
                a["users"]["editor"],
                {
                    "name": f"Feed {i}",
                    "lifecycle": "active",
                    "source": str(source.id),
                    "target": str(target.id),
                    **data,
                },
            )
        else:
            services.create_resource(
                a["org"], a["users"]["editor"], {"name": f"Thing {i}", "kind": kind, "lifecycle": "active", **data}
            )

    def count(query=""):
        with CaptureQueriesContext(connection) as queries:
            response = client.get(f"{path}{query}")
        assert response.status_code == 200
        return len(queries)

    make(0)
    client.get(path)
    one, one_filtered = count(), count("?cf_f0__gte=0")
    for i in range(1, 15):
        make(i)
    assert count() == one and count("?cf_f0__gte=0") == one_filtered


# ---- the acceptance check: a Copilot Studio agent with a custom "Business case" field ----------------------------------


def test_an_editor_creates_a_copilot_studio_agent_with_a_business_case_and_the_edit_is_in_its_history(a, api):
    admin, editor = api(a["users"]["admin"]), api(a["users"]["editor"])
    created_field = admin.post(
        "custom-fields",
        {"name": "business_case", "label": "Business case", "type": "longtext", "applies_to": ["ai_agent"]},
    )
    assert created_field.status_code == 201
    agent = editor.post(
        "ai-agents",
        {
            "name": "HR Policy Assistant",
            "lifecycle": "active",
            "platform": "copilot_studio",
            "channels": ["teams"],
            "business_owner": str(a["people"]["priya"].id),
            "custom_field_data": {"business_case": "Cuts HR policy tickets by a third."},
        },
    )
    assert agent.status_code == 201, agent.json_body
    assert agent.json_body["custom_field_data"] == {"business_case": "Cuts HR policy tickets by a third."}
    url = f"ai-agents/{agent.json_body['id']}"
    edited = editor.patch(url, {"custom_field_data": {"business_case": "Cuts HR policy tickets by half."}})
    assert edited.status_code == 200
    history = editor.get(f"{url}/audit").json_body["results"]
    assert [e["action"] for e in history] == ["resource.updated", "resource.created"]
    assert history[0]["summary"] == "Business case changed"
    detail = editor.get(f"{url}/audit/{history[0]['id']}").json_body
    rows = {r["field"]: r for r in detail["diff"]}
    assert rows["custom_field_data.business_case"] == {
        "field": "custom_field_data.business_case",
        "label": "Business case",
        "change": "changed",
        "before": "Cuts HR policy tickets by a third.",
        "after": "Cuts HR policy tickets by half.",
    }
    created = editor.get(f"{url}/audit/{history[1]['id']}").json_body
    assert {r["field"]: r["label"] for r in created["diff"]}["custom_field_data.business_case"] == "Business case"


# ---- review fixes: saved filters, recreated fields, locking, secret-looking objects ----------------------------------


def saved(f, name, query, kind="application", shared=True):
    return SavedFilter.objects.create(
        organization=f["org"],
        user=f["users"]["editor"],
        shared=shared,
        kind=kind,
        name=name,
        query=query,
        created_by=f["users"]["editor"],
    )


def test_deleting_a_field_strips_it_from_saved_filters(a, api):
    tier = field(a, "tier", type="select", choices=["Gold", "Silver"])
    field(a, "seats", type="integer")
    gold = saved(a, "Gold", {"cf_tier": "Gold", "lifecycle": "active", "cf_seats__gte": "3"})
    mine = saved(a, "Mine", {"cf_tier__set": "true"}, shared=False)
    other = saved(a, "Other", {"cf_seats": "4"})
    customfields.delete_field(a["org"], a["users"]["admin"], tier)
    for row in (gold, mine, other):
        row.refresh_from_db()
    assert gold.query == {"lifecycle": "active", "cf_seats__gte": "3"} and mine.query == {}
    assert other.query == {"cf_seats": "4"}
    event = AuditEvent.objects.get(action="custom_field.deleted")
    assert event.changes["saved_filters_changed"] == 2
    listed = {f["name"]: f for f in api(a["users"]["editor"]).get("saved-filters?kind=application").json_body}
    assert listed["Gold"]["problems"] == [] and listed["Gold"]["query"]["cf_seats__gte"] == "3"
    assert api(a["users"]["editor"]).get("applications?lifecycle=active&cf_seats__gte=3").status_code == 200


def test_removing_a_choice_prunes_it_and_removing_a_kind_drops_the_parameters(a):
    tier = field(
        a, "tier", type="multiselect", choices=["Gold", "Silver", "Bronze"], applies_to=["application", "component"]
    )
    both = saved(a, "Both", {"cf_tier": "Gold,Silver", "cf_tier__set": "true"})
    only = saved(a, "Only", {"cf_tier": "Silver"})
    comp = saved(a, "Components", {"cf_tier": "Gold", "lifecycle": "active"}, kind="component")
    customfields.update_field(a["org"], a["users"]["admin"], tier, {"choices": ["Gold", "Bronze"]})
    for row in (both, only, comp):
        row.refresh_from_db()
    assert both.query == {"cf_tier": "Gold", "cf_tier__set": "true"} and only.query == {}
    assert comp.query == {"cf_tier": "Gold", "lifecycle": "active"}
    first = AuditEvent.objects.filter(action="custom_field.updated").latest("created_at")
    assert first.changes["saved_filters_changed"] == 2
    customfields.update_field(a["org"], a["users"]["admin"], tier, {"applies_to": ["application"]})
    comp.refresh_from_db()
    both.refresh_from_db()
    assert comp.query == {"lifecycle": "active"} and both.query == {"cf_tier": "Gold", "cf_tier__set": "true"}
    customfields.update_field(a["org"], a["users"]["admin"], tier, {"type": "text", "choices": []})
    both.refresh_from_db()
    assert both.query == {}  # a type change drops every parameter of the field


def test_saved_filters_list_their_problems_as_a_backstop(a, api):
    field(a, "tier", type="select", choices=["Gold"])
    saved(a, "Stale", {"cf_tier": "Lead", "cf_gone": "x"})  # written behind the service's back
    listed = {f["name"]: f for f in api(a["users"]["viewer"]).get("saved-filters?kind=application").json_body}
    assert sorted(listed["Stale"]["problems"]) == [
        "cf_gone: This filter isn't available here.",
        "cf_tier: Use one or more of: Gold (comma-separated).",
    ]
    assert all(f["problems"] == [] for name, f in listed.items() if name != "Stale")  # the built-ins too


def test_a_recreated_field_starts_without_the_old_values(a, api):
    seats = field(a, "seats", type="integer")
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"custom_field_data": {"seats": 4}})
    customfields.delete_field(a["org"], a["users"]["admin"], seats)
    assert values(a, a["app"]) == {}
    # A key of the same name written behind the service's back after the delete is cleared too.
    other = app(a, "Other")
    Resource.objects.filter(id=other.id).update(custom_field_data={"seats": "not a number"})
    again = field(a, "seats", type="text")
    assert values(a, a["app"]) == {} and values(a, other) == {}
    assert api(a["users"]["admin"]).get(f"custom-fields/{again.id}").json_body["usage"] == 0


def test_record_writes_read_the_definitions_again_inside_their_transaction(a):
    """A stale cached definition (another request changed the field) never validates a write."""
    seats = field(a, "seats", type="text")
    customfields.definitions(a["org"])  # cached as text
    CustomField.objects.filter(id=seats.id).update(type="integer")  # another request's change
    with pytest.raises(FieldErrors) as refused:
        services.update_resource(a["org"], a["users"]["editor"], a["app"], {"custom_field_data": {"seats": "many"}})
    assert refused.value.errors == {"custom_field_data.seats": ["Enter a whole number."]}


@pytest.mark.django_db(transaction=True)
def test_a_type_change_waits_for_a_write_in_flight_and_then_sees_its_value(transactional_db):
    from tests.conftest import make_org

    f = make_org("race")
    seats = field(f, "seats", type="text")
    holding, release, finished = threading.Event(), threading.Event(), threading.Event()
    outcome: dict[str, Any] = {}

    def writer():
        try:
            with transaction.atomic():
                record = Resource.objects.get(id=f["app"].id)
                services.update_resource(f["org"], f["users"]["editor"], record, {"custom_field_data": {"seats": "4"}})
                holding.set()
                release.wait(10)
        finally:
            connection.close()

    def changer():
        holding.wait(10)
        try:
            customfields.update_field(f["org"], f["users"]["admin"], seats, {"type": "integer"})
            outcome["result"] = "changed"
        except FieldErrors as e:
            outcome["result"] = e.errors
        finally:
            finished.set()
            connection.close()

    threads = [threading.Thread(target=writer), threading.Thread(target=changer)]
    for t in threads:
        t.start()
    assert holding.wait(10)
    time.sleep(0.5)
    blocked = not finished.is_set()  # waiting on the write's FOR SHARE
    release.set()
    for t in threads:
        t.join(15)
    assert blocked
    assert "type" in outcome["result"]  # it saw the committed value and refused
    assert CustomField.objects.get(id=seats.id).type == "text"


def test_concurrent_patches_of_two_fields_both_stay(a):
    field(a, "seats", type="integer")
    field(a, "region")
    stale = Resource.objects.get(id=a["app"].id)  # read before the other write
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"custom_field_data": {"seats": 3}})
    services.update_resource(a["org"], a["users"]["editor"], stale, {"custom_field_data": {"region": "EU"}})
    assert values(a, a["app"]) == {"seats": 3, "region": "EU"}
    field(a, "tier", applies_to=["vendor"])
    field(a, "sla", applies_to=["vendor"])
    vendor = admin_services.create_named(Vendor, a["org"], a["users"]["editor"], {"name": "Fabrikam"})
    stale_vendor = Vendor.objects.get(id=vendor.id)
    admin_services.update_named(Vendor, a["org"], a["users"]["editor"], vendor, {"custom_field_data": {"tier": "A"}})
    admin_services.update_named(
        Vendor, a["org"], a["users"]["editor"], stale_vendor, {"custom_field_data": {"sla": "1d"}}
    )
    vendor.refresh_from_db()
    assert vendor.custom_field_data == {"tier": "A", "sla": "1d"}


def test_secret_looking_objects_are_never_shown_or_exported(a, api):
    field(a, "extra", type="object")
    Resource.objects.filter(id=a["app"].id).update(custom_field_data={"extra": {"db": {"password": "hunter2"}}})
    client = api(a["users"]["viewer"])
    assert client.get(f"applications/{a['app'].id}").json_body["custom_field_data"] == {"extra": None}
    content = client.client.get("/api/v1/applications/export.csv").content.decode("utf-8-sig")
    xlsx = client.client.get("/api/v1/applications/export.xlsx").content
    assert "hunter2" not in content and b"hunter2" not in xlsx


def test_has_key_queries_use_the_indexed_jsonpath_operator(a, api):
    field(a, "seats", type="integer")
    with CaptureQueriesContext(connection) as queries:
        assert api(a["users"]["viewer"]).get("applications?cf_seats__set=true").status_code == 200
    sql = " ".join(q["sql"] for q in queries.captured_queries)
    assert "@?" in sql and "::jsonpath" in sql and " ? " not in sql


def test_a_label_change_scans_no_records(a):
    seats = field(a, "seats", type="integer")
    with CaptureQueriesContext(connection) as queries:
        customfields.update_field(
            a["org"], a["users"]["admin"], seats, {"label": "Seats", "weight": 5, "required": True}
        )
    assert not any("@?" in q["sql"] for q in queries.captured_queries)


def test_the_database_keeps_names_and_labels_unique(a):
    field(a, "seats", label="Seats")
    clashes = [
        (
            CustomField(organization=a["org"], name="licences", label="SEATS", type="text", applies_to=["vendor"]),
            "label",
        ),
        (CustomField(organization=a["org"], name="seats", label="Other", type="text", applies_to=["vendor"]), "name"),
    ]
    for clash, key in clashes:
        with pytest.raises(FieldErrors) as refused:
            customfields._save(clash)
        assert list(refused.value.errors) == [key]


def test_a_deleted_fields_long_values_read_as_changed(a, api):
    brief = field(a, "brief", label="Brief", type="longtext")
    client = api(a["users"]["editor"])
    url = f"applications/{a['app'].id}"
    client.patch(url, {"custom_field_data": {"brief": "x" * 150}})
    client.patch(url, {"custom_field_data": {"brief": "y" * 150}})
    customfields.delete_field(a["org"], a["users"]["admin"], brief)
    summary = client.get(f"{url}/audit").json_body["results"][0]["summary"]
    assert summary == "Brief changed"
