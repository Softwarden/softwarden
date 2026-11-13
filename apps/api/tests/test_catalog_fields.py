"""Catalog fields: the application and component field additions, software categories, the
vendor expansion, team members, tag colours, the compliance profile and its capability gate, and the Excel export."""

import importlib
import io
from datetime import date, datetime

import pytest
from django.apps import apps as django_apps
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import load_workbook

from catalog import categories, compliance, services, snapshots
from catalog.models import (
    Application,
    Component,
    Department,
    Kind,
    Resource,
    SoftwareCategory,
    Tag,
    Team,
    TeamMember,
    Vendor,
)
from core import capabilities as caps
from core.models import AuditEvent, Organization
from imports import csvio, xlsxio
from tests.conftest import revoke_capability

GUID = "6F1D4C2A-3B5E-4A7F-9C8D-1E2F3A4B5C6D"


def latest(action):
    return AuditEvent.objects.filter(action=action).latest("created_at")


def category(org, name="ERP and finance"):
    return SoftwareCategory.objects.get(organization=org, name=name)


def enterprise(org, on=True):
    """Make the organization enterprise with the compliance profile on (or only available). Updates `org` in place,
    so fixtures that pass it to services see the change."""
    fresh, _ = caps.set_edition(org, "enterprise")
    if on:
        fresh.settings = {**fresh.settings, compliance.SETTING: True}
        fresh.save(update_fields=["settings"])
    compliance.forget(org.pk)
    org.edition, org.settings = fresh.edition, fresh.settings
    return org


# ---- applications and components ------------------------------------------------------------------------------------

APP_FIELDS = {
    "deployment_model": "saas",
    "version": "2026R2",
    "primary_url": "https://payroll.example.test",
    "admin_url": "https://payroll.example.test/admin",
    "authentication_method": "saml",
    "sso_enabled": True,
    "scim_provisioning": False,
    "entra_app_id": GUID,
    "service_principal_id": "0f0e0d0c-0b0a-4908-8706-050403020100",
    "cost_center": "CC-2100",
    "data_classification": "confidential",
}


def test_application_fields_round_trip_through_the_api(a, api):
    editor = api(a["users"]["editor"])
    hr = Department.objects.create(organization=a["org"], name="HR")
    erp = category(a["org"])
    body = {"name": "Payroll", "lifecycle": "active", "category": str(erp.id), "department": str(hr.id), **APP_FIELDS}
    created = editor.post("applications", body)
    assert created.status_code == 201, created.json_body
    got = editor.get(f"applications/{created.json_body['id']}").json_body
    assert got["category"] == {"id": str(erp.id), "name": "ERP and finance"}
    assert got["department"] == {"id": str(hr.id), "name": "HR"}
    expected = {**APP_FIELDS, "entra_app_id": GUID.lower()}  # stored lower-case
    assert {k: got[k] for k in APP_FIELDS} == expected
    listed = editor.get(f"applications?category={erp.id}&deployment_model=saas&sso_enabled=true").json_body
    assert [r["name"] for r in listed["results"]] == ["Payroll"]
    assert editor.get("applications?sso_enabled=unknown").json_body["results"][0]["name"] == "orga App"
    assert editor.get(f"applications?department={hr.id}&data_classification=confidential").json_body["results"]
    # Cleared again: yes/no fields back to "not recorded".
    cleared = editor.patch(f"applications/{created.json_body['id']}", {"sso_enabled": None, "category": None})
    assert cleared.status_code == 200 and cleared.json_body["sso_enabled"] is None
    assert cleared.json_body["category"] is None


def test_existing_response_keys_are_kept(a, api):
    """Fields are only added (the SPA reads the old keys)."""
    got = api(a["users"]["viewer"]).get(f"applications/{a['app'].id}").json_body
    old = {"id", "kind", "name", "slug", "description", "lifecycle", "criticality", "purpose", "business_owner"}
    old |= {"technical_owner", "support_team", "it_steward", "archived", "gaps", "review_state", "review_why"}
    old |= {"next_review_date", "tags", "vendor", "built_by", "license_model", "documentation_url", "support_url"}
    old |= {"instances", "archived_at", "archived_by"}
    assert old <= set(got)


@pytest.mark.parametrize(
    "field,value",
    [
        ("primary_url", "javascript:alert(1)"),
        ("admin_url", "https://" + "a" * 2000 + ".test"),
        ("deployment_model", "mainframe"),
        ("authentication_method", "magic"),
        ("sso_enabled", "maybe"),
        ("entra_app_id", "not-a-guid"),
        ("version", "v" * 51),
        ("cost_center", "c" * 51),
        ("data_classification", "secret"),
    ],
)
def test_application_fields_are_validated(a, api, field, value):
    response = api(a["users"]["editor"]).patch(f"applications/{a['app'].id}", {field: value})
    assert response.status_code == 400 and set(response.json_body["errors"]) == {field}, response.json_body


def test_component_fields_and_new_types(a, api):
    editor = api(a["users"]["editor"])
    body = {
        "name": "Provisioning workflow",
        "lifecycle": "active",
        "component_type": "infra_automation",
        "hosting": "azure",
        "environment": "prod",
        "runbook_url": "https://wiki.example.test/runbooks/provisioning",
        "schedule_or_trigger": "On new hire",
        "azure_resource_id": "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Logic/workflows/prov",
        "pp_environment_id": "Default-1234",
        "external_id": "HALO-42",
    }
    created = editor.post("components", body)
    assert created.status_code == 201, created.json_body
    got = editor.get(f"components/{created.json_body['id']}").json_body
    assert {k: got[k] for k in body if k not in ("name", "lifecycle")} == {
        k: v for k, v in body.items() if k not in ("name", "lifecycle")
    }
    for new_type in ("api", "service", "workflow", "infra_automation", "scheduled_job", "runbook"):
        assert editor.patch(f"components/{created.json_body['id']}", {"component_type": new_type}).status_code == 200
    assert editor.get("components?hosting=azure&environment=prod&component_type=runbook").json_body["results"]
    bad = editor.patch(f"components/{created.json_body['id']}", {"hosting": "mars", "environment": "staging"})
    assert bad.status_code == 400 and set(bad.json_body["errors"]) == {"hosting", "environment"}
    # Application-only fields sent to a component are ignored, not stored.
    assert editor.patch(f"components/{created.json_body['id']}", {"version": "1"}).status_code == 200


def test_record_edits_are_audited_with_the_new_fields(a, api):
    hr = Department.objects.create(organization=a["org"], name="HR")
    erp = category(a["org"])
    api(a["users"]["editor"]).patch(
        f"applications/{a['app'].id}",
        {"category": str(erp.id), "department": str(hr.id), "sso_enabled": True, "deployment_model": "saas"},
    )
    event = latest("resource.updated")
    assert event.changes["category"] == [None, str(erp.id)] and event.changes["sso_enabled"] == [None, True]
    assert event.postchange["category"] == {"id": str(erp.id), "display": "ERP and finance"}
    assert event.postchange["department"] == {"id": str(hr.id), "display": "HR"}
    assert event.prechange["sso_enabled"] is None and event.postchange["sso_enabled"] is True
    history = api(a["users"]["viewer"]).get(f"applications/{a['app'].id}/audit").json_body["results"][0]
    assert "Category: not set → ERP and finance" in history["summary"]
    assert "Department: not set → HR" in history["summary"]


def test_another_organizations_category_and_department_are_refused(a, b, api):
    theirs = Department.objects.create(organization=b["org"], name="HR")
    response = api(a["users"]["editor"]).patch(
        f"applications/{a['app'].id}", {"category": str(category(b["org"]).id), "department": str(theirs.id)}
    )
    assert response.status_code == 400 and set(response.json_body["errors"]) == {"category", "department"}
    assert api(a["users"]["viewer"]).get(f"applications?department={theirs.id}").json_body["results"] == []


# ---- software categories ----------------------------------------------------------------------------------------


def test_every_new_organization_gets_the_default_categories(db):
    org = Organization.objects.create(name="Fresh (synthetic)")
    names = sorted(SoftwareCategory.objects.filter(organization=org).values_list("name", flat=True))
    assert names == sorted(n for n, _ in categories.DEFAULT_CATEGORIES)
    assert categories.seed(org) == 0  # idempotent
    assert not AuditEvent.objects.filter(organization=org).exists()  # part of creating the organization


def test_the_migration_seeded_existing_organizations(a):
    migration = importlib.import_module("catalog.migrations.0023_software_categories")
    SoftwareCategory.objects.filter(organization=a["org"]).delete()
    migration.seed_existing(django_apps, None)
    assert SoftwareCategory.objects.filter(organization=a["org"]).count() == len(migration.DEFAULT_CATEGORIES)
    assert [n for n, _ in migration.DEFAULT_CATEGORIES] == [n for n, _ in categories.DEFAULT_CATEGORIES]


def test_a_department_used_by_a_record_can_t_be_deleted(a, api):
    editor = api(a["users"]["editor"])
    finance = Department.objects.create(organization=a["org"], name="Finance")
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"department": str(finance.id)})
    refused = editor.delete(f"departments/{finance.id}")
    assert refused.status_code == 409, refused.json_body  # the services' refusal, not PROTECT's 500
    assert refused.json_body["detail"] == "Finance can't be deleted: 1 record uses it (orga App). Reassign them first."
    assert refused.json_body["blocking_references"] == {"records": 1}
    dana = a["people"]["dana"]
    dana.department = finance
    dana.save()
    assert {d["name"]: d["usage"] for d in editor.get("departments").json_body}["Finance"] == 2  # people + records
    both = editor.delete(f"departments/{finance.id}").json_body
    assert both["blocking_references"] == {"people and records": 2} and "2 people and records use it" in both["detail"]
    assert Department.objects.filter(pk=finance.pk).exists()


def test_categories_crud_by_role(a, b, api):
    viewer, editor = api(a["users"]["viewer"]), api(a["users"]["editor"])
    listed = viewer.get("categories").json_body
    assert len(listed) == len(categories.DEFAULT_CATEGORIES) and {"usage", "description"} <= set(listed[0])
    assert viewer.post("categories", {"name": "Robotics"}).status_code == 403
    made = editor.post("categories", {"name": "Robotics", "description": "Factory robots."})
    assert made.status_code == 201 and made.json_body["description"] == "Factory robots."
    assert latest("category.created").target_type == "SoftwareCategory"
    assert editor.post("categories", {"name": "robotics"}).status_code == 400  # unique ignoring case
    assert editor.post("categories", {"name": "Long", "description": "x" * 201}).status_code == 400
    renamed = editor.patch(f"categories/{made.json_body['id']}", {"name": "Robotics and automation"})
    assert renamed.status_code == 200 and latest("category.updated").postchange["name"] == "Robotics and automation"
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"category": made.json_body["id"]})
    assert {c["name"]: c["usage"] for c in viewer.get("categories").json_body}["Robotics and automation"] == 1
    refused = editor.delete(f"categories/{made.json_body['id']}")
    assert refused.status_code == 409 and "orga App" in refused.json_body["detail"]
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"category": None})
    assert editor.delete(f"categories/{made.json_body['id']}").status_code == 204
    assert latest("category.deleted").changes == {"name": "Robotics and automation"}
    assert editor.get(f"categories/{category(b['org']).id}").status_code == 404


# ---- vendors, teams and tags ----------------------------------------------------------------------------------------


def test_vendor_details_and_account_manager(a, b, api):
    editor = api(a["users"]["editor"])
    dana = a["people"]["dana"]
    body = {
        "name": "Contoso Payroll",
        "website": "https://contoso.example.test",
        "support_url": "https://contoso.example.test/support",
        "support_email": "help@contoso.example.test",
        "support_phone": "+1 555 0100",
        "portal_url": "https://portal.contoso.example.test",
        "account_manager": str(dana.id),
        "notes": "Renews in March.",
    }
    made = editor.post("vendors", body)
    assert made.status_code == 201, made.json_body
    got = editor.get(f"vendors/{made.json_body['id']}").json_body
    assert {k: got[k] for k in body if k != "account_manager"} == {
        k: v for k, v in body.items() if k != "account_manager"
    }
    assert got["account_manager"]["id"] == str(dana.id) and got["account_manager"]["name"] == "Dana Ortiz"
    event = latest("vendor.created")
    assert event.postchange["account_manager"] == {"id": str(dana.id), "display": None}  # a Person by ID
    for field, value in (
        ("support_email", "not-an-email"),
        ("portal_url", "ftp://portal"),
        ("support_phone", "1" * 51),
        ("account_manager", str(b["people"]["dana"].id)),
    ):
        refused = editor.patch(f"vendors/{made.json_body['id']}", {field: value})
        assert refused.status_code == 400 and set(refused.json_body["errors"]) == {field}, field
    editor.patch(f"vendors/{made.json_body['id']}", {"account_manager": None, "support_phone": ""})
    changes = latest("vendor.updated").changes
    assert changes["account_manager"] == [str(dana.id), None] and changes["support_phone"] == ["+1 555 0100", ""]


def test_team_members(a, b, api):
    editor, viewer = api(a["users"]["editor"]), api(a["users"]["viewer"])
    team = a["team"]
    dana, priya = a["people"]["dana"], a["people"]["priya"]
    before = viewer.client.get(f"/api/v1/teams/{team.id}")["ETag"]
    response = editor.patch(f"teams/{team.id}", {"members": [str(priya.id), str(dana.id), str(dana.id)]})
    assert response.status_code == 200, response.json_body
    assert [m["name"] for m in response.json_body["members"]] == ["Dana Ortiz", "Priya Raman"]
    assert viewer.client.get(f"/api/v1/teams/{team.id}")["ETag"] != before  # members are part of the revision
    event = latest("team.updated")
    assert sorted(event.changes["members"][1]) == sorted([str(dana.id), str(priya.id)])
    assert event.prechange["members"] == []
    assert {e["display"] for e in event.postchange["members"]} == {None}  # people by ID only
    summary = api(a["users"]["admin"]).get(f"audit-events/{event.id}").json_body["summary"]
    assert summary.startswith("IT Applications → IT Applications; Members: not set → ") and "Dana Ortiz" in summary
    listed = {t["name"]: t for t in viewer.get("teams").json_body}
    assert len(listed["IT Applications"]["members"]) == 2
    refused = editor.patch(f"teams/{team.id}", {"members": [str(b["people"]["dana"].id)]})
    assert refused.status_code == 400 and "members" in refused.json_body["errors"]
    assert editor.patch(f"teams/{team.id}", {"members": "everyone"}).status_code == 400
    assert editor.patch(f"teams/{team.id}", {"members": []}).status_code == 200
    assert not TeamMember.objects.filter(team=team).exists()
    made = editor.post("teams", {"name": "Night shift", "members": [str(dana.id)]})
    assert made.status_code == 201 and [m["name"] for m in made.json_body["members"]] == ["Dana Ortiz"]
    assert viewer.patch(f"teams/{team.id}", {"members": []}).status_code == 403


def test_tag_colour_and_description(a, api):
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"tags": ["Finance"]})
    tag = Tag.objects.get(organization=a["org"], name="Finance")
    editor, viewer = api(a["users"]["editor"]), api(a["users"]["viewer"])
    assert viewer.patch(f"tags/{tag.id}", {"color": "green"}).status_code == 403
    done = editor.patch(f"tags/{tag.id}", {"color": "green", "description": "Month-end close."})
    assert done.status_code == 200 and (done.json_body["color"], done.json_body["count"]) == ("green", 1)
    assert viewer.get("tags").json_body[0]["description"] == "Month-end close."
    for bad in ("#00ff00", "chartreuse", 3):
        refused = editor.patch(f"tags/{tag.id}", {"color": bad})
        assert refused.status_code == 400 and "color" in refused.json_body["errors"], bad
    event = latest("tag.updated")
    assert event.target_type == "Tag" and event.changes["color"] == ["", "green"]
    assert event.postchange == {"name": "Finance", "color": "green", "description": "Month-end close."}
    assert editor.patch(f"tags/{tag.id}", {"color": ""}).json_body["color"] == ""


# ---- the compliance profile ------------------------------------------------------------------------------------------

COMPLIANCE_VALUES = {
    "is_gxp": True,
    "validation_status": "validated",
    "validation_doc_url": "https://contoso.sharepoint.example.test/sites/qa/validation/payroll.pdf",
    "last_periodic_review": "2026-01-15",
    "next_periodic_review": "2027-01-15",
    "risk_tier": "high",
}


def test_off_by_default_fields_are_omitted_and_refused(a, api):
    admin_view = api(a["users"]["admin"]).get("organization").json_body
    assert (admin_view["compliance_profile_available"], admin_view["compliance_profile_enabled"]) == (True, False)
    editor = api(a["users"]["editor"])
    got = editor.get(f"applications/{a['app'].id}").json_body
    assert not set(compliance.FIELDS) & set(got)
    assert not set(compliance.FIELDS) & set(editor.get("applications").json_body["results"][0])
    refused = editor.patch(f"applications/{a['app'].id}", {"is_gxp": True, "risk_tier": "high"})
    assert refused.status_code == 400 and set(refused.json_body["errors"]) == {"is_gxp", "risk_tier"}
    assert refused.json_body["errors"]["is_gxp"] == [compliance.OFF]
    assert editor.post("applications", {"name": "X", "lifecycle": "active", "is_gxp": False}).status_code == 400
    assert editor.get("applications?is_gxp=true").status_code == 400
    # Turning it on needs the capability (every edition has it; here a platform revoke took it away): 403 problem
    # naming it, for the Admin too.
    revoke_capability(a["org"], "compliance_profile")
    admin = api(a["users"]["admin"])
    gated = admin.patch("organization", {compliance.SETTING: True})
    assert gated.status_code == 403 and gated.json_body["capability"] == "compliance_profile"
    org = admin.get("organization").json_body
    assert (org["compliance_profile_available"], org["compliance_profile_enabled"]) == (False, False)
    assert Organization.objects.get(pk=a["org"].pk).settings.get(compliance.SETTING) is None


def test_enterprise_admin_turns_it_on_and_off(a, api):
    enterprise(a["org"], on=False)
    admin, editor = api(a["users"]["admin"]), api(a["users"]["editor"])
    assert not set(compliance.FIELDS) & set(editor.get(f"applications/{a['app'].id}").json_body)
    assert editor.patch("organization", {compliance.SETTING: True}).status_code == 403  # Admin only
    assert admin.patch("organization", {compliance.SETTING: "yes"}).status_code == 400
    turned = admin.patch("organization", {compliance.SETTING: True})
    assert turned.status_code == 200 and turned.json_body["compliance_profile_enabled"] is True
    event = latest("organization.updated")
    assert event.changes == {compliance.SETTING: [False, True]}
    assert event.postchange["settings"] == {compliance.SETTING: True}  # an audited settings key
    got = editor.get(f"applications/{a['app'].id}").json_body
    assert {f: got[f] for f in compliance.FIELDS} == dict.fromkeys(compliance.FIELDS)  # present, not recorded
    saved = editor.patch(f"applications/{a['app'].id}", COMPLIANCE_VALUES)
    assert saved.status_code == 200, saved.json_body
    assert {f: saved.json_body[f] for f in compliance.FIELDS} == COMPLIANCE_VALUES
    update = latest("resource.updated")
    assert update.postchange["risk_tier"] == "high" and update.postchange["last_periodic_review"] == "2026-01-15"
    assert update.changes["is_gxp"] == [None, "true"]
    assert [r["name"] for r in editor.get("applications?is_gxp=true&risk_tier=high").json_body["results"]] == [
        "orga App"
    ]
    assert editor.get("applications?validation_status=in_validation").json_body["results"] == []
    # Off again: hidden, refused, kept.
    assert admin.patch("organization", {compliance.SETTING: False}).status_code == 200
    assert not set(compliance.FIELDS) & set(editor.get(f"applications/{a['app'].id}").json_body)
    assert editor.patch(f"applications/{a['app'].id}", {"risk_tier": "low"}).status_code == 400
    assert Resource.objects.get(pk=a["app"].pk).risk_tier == "high"
    editor.patch(f"applications/{a['app'].id}", {"description": "Edited while off"})
    assert "risk_tier" not in latest("resource.updated").postchange  # snapshots leave them out too


@pytest.mark.parametrize(
    "values,field",
    [
        ({"validation_status": "approved"}, "validation_status"),
        ({"risk_tier": "extreme"}, "risk_tier"),
        ({"is_gxp": "perhaps"}, "is_gxp"),
        ({"validation_doc_url": "notaurl"}, "validation_doc_url"),
        ({"last_periodic_review": "15/01/2026"}, "last_periodic_review"),
        ({"last_periodic_review": "2026-06-01", "next_periodic_review": "2026-01-01"}, "next_periodic_review"),
    ],
)
def test_compliance_values_are_validated(a, api, values, field):
    enterprise(a["org"])
    response = api(a["users"]["editor"]).patch(f"applications/{a['app'].id}", values)
    assert response.status_code == 400 and set(response.json_body["errors"]) == {field}, response.json_body


def test_a_patch_of_one_review_date_is_checked_against_the_stored_other(a, api):
    enterprise(a["org"])
    editor = api(a["users"]["editor"])
    editor.patch(f"applications/{a['app'].id}", {"last_periodic_review": "2026-06-01"})
    refused = editor.patch(f"applications/{a['app'].id}", {"next_periodic_review": "2026-05-01"})
    assert refused.status_code == 400 and "next_periodic_review" in refused.json_body["errors"]


def test_losing_the_capability_turns_the_profile_off(a, api):
    org = enterprise(a["org"])
    editor = api(a["users"]["editor"])
    assert "is_gxp" in editor.get(f"applications/{a['app'].id}").json_body
    revoke_capability(org, "compliance_profile")
    assert "is_gxp" not in editor.get(f"applications/{a['app'].id}").json_body
    assert api(a["users"]["admin"]).get("organization").json_body["compliance_profile_enabled"] is False


def test_audit_history_hides_compliance_values_while_the_profile_is_off(a, api):
    enterprise(a["org"])
    admin, editor = api(a["users"]["admin"]), api(a["users"]["editor"])
    url = f"applications/{a['app'].id}"
    editor.patch(url, {"risk_tier": "high", "description": "Reviewed"})
    event = latest("resource.updated")

    def shown():
        """Everything the History tab, the log and the event detail return for the edit, as text."""
        history = editor.get(f"{url}/audit").json_body["results"]
        row = next(r for r in history if r["id"] == str(event.id))
        logged = next(r for r in admin.get("audit-events").json_body["results"] if r["id"] == str(event.id))
        detail = admin.get(f"audit-events/{event.id}").json_body
        one = editor.get(f"{url}/audit/{event.id}").json_body
        return row, logged, detail, one

    row, logged, detail, one = shown()
    assert "Risk tier: not set → high" in row["summary"] and "Risk tier" in logged["summary"]
    assert detail["postchange"]["risk_tier"] == "high" and "risk_tier" in {d["field"] for d in detail["diff"]}
    assert admin.patch("organization", {compliance.SETTING: False}).status_code == 200
    row, logged, detail, one = shown()
    for item in (row, logged, detail, one):
        assert "risk_tier" not in str(item) and "Risk tier" not in item["summary"], item
    assert "Description changed" in row["summary"]  # the rest of the event is still there
    assert {d["field"] for d in detail["diff"]} == {"description"}
    assert not set(compliance.FIELDS) & (set(detail["prechange"]) | set(detail["postchange"]))
    assert AuditEvent.objects.get(pk=event.pk).changes["risk_tier"] == [None, "high"]  # stored as it was
    assert admin.patch("organization", {compliance.SETTING: True}).status_code == 200
    row, logged, detail, one = shown()
    assert "Risk tier: not set → high" in row["summary"] and detail["postchange"]["risk_tier"] == "high"


def test_one_organizations_profile_is_its_own(a, b, api):
    enterprise(a["org"])
    assert "is_gxp" in api(a["users"]["viewer"]).get(f"applications/{a['app'].id}").json_body
    assert "is_gxp" not in api(b["users"]["viewer"]).get(f"applications/{b['app'].id}").json_body


def test_compliance_columns_in_export_template_and_import(a, api):
    editor = api(a["users"]["editor"])
    community = editor.client.get("/api/v1/applications/template.csv").content.decode("utf-8-sig")
    assert "is_gxp" not in community
    enterprise(a["org"])
    template = editor.client.get("/api/v1/applications/template.csv").content.decode("utf-8-sig")
    assert template.strip().split(",")[-6:] == list(compliance.FIELDS)
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"is_gxp": True, "risk_tier": "low"})
    _, rows = csvio.read_csv(editor.client.get("/api/v1/applications/export.csv").content)
    assert (rows[0]["is_gxp"], rows[0]["risk_tier"], rows[0]["validation_status"]) == ("true", "low", "")
    # The export re-imports unchanged; a changed cell is an update through the same validation.
    rows[0]["risk_tier"] = "medium"
    data = csvio.write_csv(csvio.columns(Kind.APPLICATION, True), rows)
    body = {"file": SimpleUploadedFile("x.csv", data, content_type="text/csv"), "kind": "application"}
    run = editor.client.post("/api/v1/imports", body).json()
    assert run["counts"]["update"] == 1, run
    assert editor.post(f"imports/{run['id']}/commit").status_code == 200
    assert Resource.objects.get(pk=a["app"].pk).risk_tier == "medium"


# ---- CSV import of the new fields -------------------------------------------------------------------------------------


def test_import_resolves_category_and_department_by_name(a, api):
    Department.objects.create(organization=a["org"], name="HR")
    admin = api(a["users"]["admin"])
    rows = [
        {"name": "Payroll", "lifecycle": "active", "category": "erp and finance", "department": "HR"}
        | {"sso_enabled": "Yes", "deployment_model": "SaaS", "entra_app_id": GUID, "data_classification": "internal"},
        {"name": "Mystery", "lifecycle": "active", "category": "Astrology"},
        {"name": "Odd", "lifecycle": "active", "scim_provisioning": "sometimes"},
    ]
    data = csvio.write_csv(csvio.TEMPLATE[Kind.APPLICATION], rows)
    run = admin.client.post(
        "/api/v1/imports", {"file": SimpleUploadedFile("x.csv", data, content_type="text/csv"), "kind": "application"}
    ).json()
    found = {r["name"]: r for r in admin.get(f"imports/{run['id']}/rows").json_body["results"]}
    assert found["Payroll"]["outcome"] == "create", found["Payroll"]
    assert any("category" == e.get("column") for e in found["Mystery"]["errors"])
    assert any("scim_provisioning" == e.get("column") for e in found["Odd"]["errors"])
    admin.patch(f"imports/{run['id']}", {"exclude": [found["Mystery"]["row_number"], found["Odd"]["row_number"]]})
    assert admin.post(f"imports/{run['id']}/commit").status_code == 200
    made = Resource.objects.select_related("application__category", "department").get(name="Payroll")
    ext = made.application
    assert ext.category is not None and made.department is not None
    assert (ext.category.name, made.department.name, ext.sso_enabled, ext.deployment_model) == (
        "ERP and finance",
        "HR",
        True,
        "saas",
    )
    assert ext.entra_app_id == GUID.lower() and made.data_classification == "internal"
    # The export of what was imported re-imports with nothing changed.
    again = csvio.write_csv(
        csvio.TEMPLATE[Kind.APPLICATION], [csvio.record_values(Resource.objects.get(pk=made.pk), [])]
    )
    rerun = admin.client.post(
        "/api/v1/imports", {"file": SimpleUploadedFile("y.csv", again, content_type="text/csv"), "kind": "application"}
    ).json()
    assert rerun["counts"]["unchanged"] == 1 and rerun["counts"]["update"] == 0, rerun["counts"]


def test_component_template_has_the_new_columns():
    assert {"hosting", "environment", "runbook_url", "external_id", "department"} <= set(csvio.TEMPLATE[Kind.COMPONENT])
    assert {"category", "sso_enabled", "entra_app_id"} <= set(csvio.TEMPLATE[Kind.APPLICATION])
    assert not set(compliance.FIELDS) & set(csvio.TEMPLATE[Kind.APPLICATION])


# ---- Excel export ------------------------------------------------------------------------------------------------------


def workbook(response):
    assert response.status_code == 200, response.content[:300]
    assert response["Content-Type"] == xlsxio.XLSX_TYPE
    assert response["Content-Disposition"].endswith('.xlsx"')
    return load_workbook(io.BytesIO(response.content))


@pytest.mark.parametrize("role", ["admin", "editor", "viewer"])
@pytest.mark.parametrize("kind", ["application", "component"])
def test_every_role_exports_a_workbook_with_a_frozen_typed_header(a, api, role, kind):
    services.create_resource(a["org"], a["users"]["editor"], {"name": "Exported", "lifecycle": "active", "kind": kind})
    wb = workbook(api(a["users"][role]).client.get(f"/api/v1/{kind}s/export.xlsx"))
    ws = wb.active
    assert ws.title == f"{kind}s".capitalize() and ws.freeze_panes == "A2"
    header = [c.value for c in ws[1]]
    assert header == [*csvio.TEMPLATE[kind], "review_state", "next_review_date", "updated_at"]
    assert all(c.font.bold for c in ws[1]) and ws.auto_filter.ref.startswith("A1:")
    names = [row[header.index("name")] for row in ws.iter_rows(min_row=2, values_only=True)]
    assert "Exported" in names


def test_workbook_cells_are_typed_and_formula_safe(a, api):
    editor = a["users"]["editor"]
    tricky = services.create_resource(
        a["org"],
        editor,
        {"name": '=HYPERLINK("http://x")', "lifecycle": "active", "description": "@cmd", "sso_enabled": True},
    )
    services.record_review(a["org"], editor, tricky, {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Mail"})
    services.create_resource(a["org"], editor, {"name": "+1 plus", "lifecycle": "active", "purpose": "-Internal\x07"})
    ws = workbook(api(a["users"]["viewer"]).client.get("/api/v1/applications/export.xlsx")).active
    header = [c.value for c in ws[1]]
    rows = {r[header.index("softwarden_id")].value: r for r in ws.iter_rows(min_row=2)}
    row = rows[str(tricky.id)]

    def cell(name):
        return row[header.index(name)]

    assert cell("name").value == '\'=HYPERLINK("http://x")' and cell("name").data_type == "s"
    assert cell("description").value == "'@cmd"
    assert cell("sso_enabled").value is True and cell("sso_enabled").data_type == "b"
    assert isinstance(cell("next_review_date").value, datetime) and cell("next_review_date").is_date
    assert cell("review_state").value == "confirmed"
    assert isinstance(cell("updated_at").value, datetime)
    assert cell("scim_provisioning").value is None  # not recorded stays empty
    plus = next(r for r in rows.values() if r[header.index("name")].value == "'+1 plus")
    assert plus[header.index("purpose")].value == "'-Internal"  # control character dropped, escape kept
    for r in ws.iter_rows(min_row=2):
        assert all(c.data_type != "f" for c in r)  # never a formula


def test_workbook_honours_the_list_filters_and_is_audited(a, api):
    services.create_resource(a["org"], a["users"]["editor"], {"name": "Retired one", "lifecycle": "retired"})
    client = api(a["users"]["viewer"])

    def names(query):
        ws = workbook(client.client.get("/api/v1/applications/export.xlsx" + query)).active
        return sorted(r[2] for r in ws.iter_rows(min_row=2, values_only=True))

    assert names("?lifecycle=retired") == ["Retired one"]
    assert names("") == ["Retired one", "orga App"]
    assert client.get("applications/export.xlsx?bogus=1").status_code == 400
    event = latest("export")
    assert event.changes == {"kind": "application", "rows": 2, "filters": {}, "format": "xlsx"}
    summary = api(a["users"]["admin"]).get(f"audit-events/{event.id}").json_body["summary"]
    assert summary == "Excel, 2 applications"


def test_workbook_compliance_columns_and_extra_sheets(a, api, monkeypatch):
    enterprise(a["org"])
    services.update_resource(
        a["org"], a["users"]["editor"], a["app"], {"is_gxp": False, "next_periodic_review": "2027-02-01"}
    )
    seen = []

    def custom_fields(org, kind, rows):
        seen.append((org.pk, kind, [r.pk for r in rows]))
        return xlsxio.Sheet(
            "Custom fields",
            [xlsxio.Column("softwarden_id"), xlsxio.Column("seats", xlsxio.NUMBER)],
            [{"softwarden_id": str(r.pk), "seats": "12"} for r in rows],
        )

    monkeypatch.setattr(xlsxio, "EXTRA_SHEETS", [custom_fields])
    wb = workbook(api(a["users"]["viewer"]).client.get("/api/v1/applications/export.xlsx"))
    ws = wb.active
    header = [c.value for c in ws[1]]
    assert header[-9:-3] == list(compliance.FIELDS)
    row = next(ws.iter_rows(min_row=2))
    assert row[header.index("is_gxp")].value is False
    assert row[header.index("next_periodic_review")].value == datetime(2027, 2, 1)
    assert wb.sheetnames == ["Applications", "Custom fields"]
    extra = wb["Custom fields"]
    assert extra.freeze_panes == "A2" and [c.value for c in extra[2]] == [str(a["app"].pk), 12]
    assert seen == [(a["org"].pk, "application", [a["app"].pk])]


def test_typed_values():
    text, flag, day, number = (xlsxio.Column("x", t) for t in (xlsxio.TEXT, xlsxio.BOOLEAN, xlsxio.DATE, xlsxio.NUMBER))
    assert xlsxio.typed(text, "=1+1") == "'=1+1" and xlsxio.typed(text, "") is None
    assert xlsxio.typed(flag, "false") is False and xlsxio.typed(flag, "maybe") == "maybe"
    assert xlsxio.typed(day, "2026-03-01") == date(2026, 3, 1) and xlsxio.typed(day, "soon") == "soon"
    assert xlsxio.typed(number, "-3") == -3 and xlsxio.typed(number, "2.5") == 2.5
    assert xlsxio.typed(number, "-x") == "'-x"
    for not_finite in ("nan", "inf", "-Infinity", float("nan"), float("inf")):
        assert isinstance(xlsxio.typed(number, not_finite), str), not_finite  # kept as text, never NaN or Inf


# ---- snapshots and the database ---------------------------------------------------------------------------------------


def test_one_yes_no_parser():
    from catalog import services as catalog_services

    assert catalog_services.yes_no is compliance.yes_no
    errors: dict = {}
    assert [compliance.yes_no(v, "f", errors) for v in (True, "Yes", "no", "", None)] == [True, True, False, None, None]
    assert compliance.yes_no("maybe", "f", errors) is None and errors == {
        "f": ["Use true or false, or leave it empty."]
    }


def test_unknown_is_not_an_environment(a, api):
    created = services.create_resource(
        a["org"], a["users"]["editor"], {"name": "J", "kind": "component", "lifecycle": "active"}
    )
    refused = api(a["users"]["editor"]).patch(f"components/{created.id}", {"environment": "unknown"})
    assert refused.status_code == 400 and "environment" in refused.json_body["errors"]


def test_set_edition_clears_cached_organization_facts(a):
    from core.request_context import organization_cache

    pk = a["org"].pk
    assert compliance.enabled_for(pk) is False
    assert organization_cache(pk)["compliance"] is False  # beside the capability overrides
    Organization.objects.filter(pk=pk).update(settings={compliance.SETTING: True})
    assert compliance.enabled_for(pk) is False  # read once per unit
    caps.set_edition(a["org"], "enterprise")  # core clears the organization's cached facts, whoever cached them
    assert compliance.enabled_for(pk) is True


def test_snapshot_specs_cover_the_new_models():
    specs = snapshots.specs()
    assert {SoftwareCategory, Tag, Team, Vendor} <= set(specs)
    assert "members" in specs[Team].fields and "account_manager" in specs[Vendor].fields
    assert snapshots.roots()[TeamMember] == (Team, "team_id")


def test_extension_rows_keep_their_defaults(a):
    app = Application.objects.get(resource=a["app"])
    assert (app.sso_enabled, app.category_id, app.deployment_model) == (None, None, "")
    comp = services.create_resource(
        a["org"], a["users"]["editor"], {"name": "J", "kind": "component", "lifecycle": "active"}
    )
    assert Component.objects.get(resource=comp).hosting == ""
    assert Resource.objects.get(pk=a["app"].pk).is_gxp is None


def test_team_members_are_unique(a):
    from django.db import IntegrityError, transaction

    TeamMember.objects.create(organization=a["org"], team=a["team"], person=a["people"]["dana"])
    with pytest.raises(IntegrityError), transaction.atomic():
        TeamMember.objects.create(organization=a["org"], team=a["team"], person=a["people"]["dana"])
    assert Team.objects.get(pk=a["team"].pk).members.count() == 1
    assert Vendor._meta.get_field("account_manager").null
