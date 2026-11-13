"""CSV import column mapping: non-template headings leave the run uploaded until PATCHed
with a mapping; the mapping is validated, applied, previewed and optionally saved on the source."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from catalog.models import Kind, Resource
from core.models import AuditEvent
from imports import csvio
from imports.models import ImportSource


def sheet(kind, *rows):
    return csvio.write_csv(csvio.TEMPLATE[kind], list(rows))


def upload(who, data, kind="application", source=None, name="import.csv"):
    body = {"file": SimpleUploadedFile(name, data, content_type="text/csv"), "kind": kind}
    if source is not None:
        body["source"] = str(source.id)
    response = who.client.post("/api/v1/imports", body)
    response.json_body = response.json() if response.content else None
    return response


def rows_of(who, run_id):
    return who.get(f"imports/{run_id}/rows").json_body["results"]


@pytest.fixture
def admin(a, api):
    return api(a["users"]["admin"])


@pytest.fixture
def editor(a, api):
    return api(a["users"]["editor"])


@pytest.fixture
def viewer(a, api):
    return api(a["users"]["viewer"])


FILE = (
    b"App Name,Business owner email,Status,Notes,External-Key\r\n"
    b"Okta,dana@example.test,active,sso,K1\r\n"
    b"Slack,,active,chat,K2\r\n"
)

GOOD = {
    "App Name": "name",
    "Business owner email": "business_owner_email",
    "Status": "lifecycle",
    "Notes": "description",
    "External-Key": "ignore",
}


@pytest.fixture
def source(a):
    return ImportSource.objects.create(organization=a["org"], name="CMDB", kind=Kind.APPLICATION)


def patch(who, run, mapping, **extra):
    return who.patch(f"imports/{run}", {"mapping": mapping, **extra})


def test_template_upload_is_unchanged(editor):
    body = upload(editor, sheet(Kind.APPLICATION, {"name": "Okta", "lifecycle": "active"})).json_body
    assert body["status"] == "previewed" and body["needs_mapping"] is False and body["mapping"] == {}


def test_other_headings_leave_the_run_uploaded_with_suggestions(editor):
    response = upload(editor, FILE)
    assert response.status_code == 201
    body = response.json_body
    assert body["status"] == "uploaded" and body["needs_mapping"] is True and body["can_commit"] is False
    assert body["headers"] == ["App Name", "Business owner email", "Status", "Notes", "External-Key"]
    # no source: external_key can't be used, so it isn't suggested
    assert body["suggested_mapping"] == {
        "App Name": "ignore",
        "Business owner email": "business_owner_email",
        "Status": "ignore",
        "Notes": "ignore",
        "External-Key": "ignore",
    }
    fields = {f["value"]: f for f in body["fields"]}
    assert fields["business_owner_email"]["label"] == "Business owner email"
    assert fields["name"]["required"] is True and fields["lifecycle"]["required"] is False
    assert "softwarden_id" in fields and "external_key" in fields and "component_type" not in fields


def test_name_suggested_and_external_key_with_a_source(editor, source):
    body = upload(editor, b"Name,External key,Business Owner Name\r\nOkta,K1,Dana\r\n", source=source).json_body
    assert body["suggested_mapping"] == {
        "Name": "name",
        "External key": "external_key",
        "Business Owner Name": "business_owner_name",
    }


def test_suggestion_by_name_with_a_source(editor, source):
    body = upload(editor, b"Application,External key,Business Owner Name\r\nOkta,K1,Dana\r\n", source=source).json_body
    assert body["suggested_mapping"] == {
        "Application": "ignore",
        "External key": "external_key",
        "Business Owner Name": "business_owner_name",
    }


def test_suggestion_prefilled_from_the_saved_source_mapping(editor, source):
    source.column_mapping = {"App Name": "name", "Status": "lifecycle", "Gone": "tags", "Notes": "bogus"}
    source.save()
    body = upload(editor, FILE, source=source).json_body
    assert body["suggested_mapping"]["App Name"] == "name"
    assert body["suggested_mapping"]["Status"] == "lifecycle"
    assert body["suggested_mapping"]["Notes"] == "ignore"  # an invalid saved target is dropped
    assert "Gone" not in body["suggested_mapping"]
    assert body["suggested_mapping"]["External-Key"] == "external_key"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"App Name": "ignore"}, "Map a column to Name to continue."),
        ({"Status": "name"}, '"App Name" and "Status" are both mapped to Name.'),
        ({"External-Key": "external_key"}, "Choose an import source to use external keys."),
        ({"Notes": "owner"}, "isn't a column you can map"),
        ({"Notes": "component_type"}, "isn't a column you can map"),
        ({"Notes": None}, 'Choose a column for "Notes"'),
    ],
)
def test_mapping_validation(editor, change, message):
    run = upload(editor, FILE).json_body["id"]
    mapping = {**GOOD, **change}
    if change.get("Notes", 1) is None:
        del mapping["Notes"]
    response = patch(editor, run, mapping)
    assert response.status_code == 400
    assert any(message in m for m in response.json_body["errors"]["mapping"])
    assert editor.get(f"imports/{run}").json_body["status"] == "uploaded"


def test_bad_shape_and_unmapped_runs_cannot_be_decided_or_committed(editor):
    run = upload(editor, FILE).json_body["id"]
    assert patch(editor, run, {"App Name": "name"}).status_code == 400
    assert editor.patch(f"imports/{run}", {"mapping": ["name"]}).status_code == 400
    assert editor.patch(f"imports/{run}", {"decisions": {}}).status_code == 409  # map first
    assert editor.post(f"imports/{run}/commit").status_code == 409


def test_mapped_preview_and_commit(a, editor):
    run = upload(editor, FILE).json_body["id"]
    done = patch(editor, run, GOOD)
    assert done.status_code == 200
    body = done.json_body
    assert body["status"] == "previewed" and body["needs_mapping"] is False and body["mapping"] == GOOD
    assert body["counts"]["create"] == 2 and body["can_commit"] is False  # dana isn't a person yet: a choice is needed
    rows = rows_of(editor, run)
    assert rows[0]["name"] == "Okta" and rows[0]["changes"]["description"] == ["", "sso"]
    again = patch(editor, run, GOOD)
    assert again.status_code == 409 and "Upload the file again" in again.json_body["detail"]
    editor.patch(f"imports/{run}", {"decisions": {"people": {"dana@example.test": "gap"}}})
    committed = editor.post(f"imports/{run}/commit").json_body
    assert committed["status"] == "committed" and committed["counts"]["created"] == 2
    assert Resource.objects.filter(organization=a["org"], name__in=["Okta", "Slack"]).count() == 2


def test_mapped_external_keys_with_a_source(editor, source):
    run = upload(editor, FILE, source=source).json_body["id"]
    mapping = {**GOOD, "External-Key": "external_key", "Business owner email": "ignore"}
    assert patch(editor, run, mapping).json_body["counts"]["new_keys"] == 2
    assert editor.post(f"imports/{run}/commit").json_body["counts"]["keys_stored"] == 2


def test_save_mapping_stores_audits_and_prefills(editor, source):
    source.column_mapping = {"Old": "tags", "Status": "ignore"}
    source.save()
    run = upload(editor, FILE, source=source).json_body["id"]
    assert patch(editor, run, GOOD, save_mapping=True).status_code == 200
    source.refresh_from_db()
    assert source.column_mapping == {"Old": "tags", **GOOD}  # merged, new entries win
    event = AuditEvent.objects.get(action="import_source.mapping_saved")
    assert event.changes == {"name": "CMDB", "mapping": GOOD}
    again = upload(editor, FILE, source=source).json_body
    assert again["suggested_mapping"] == GOOD


def test_save_mapping_needs_a_source_and_is_not_saved_on_error(editor, source):
    run = upload(editor, FILE).json_body["id"]
    response = patch(editor, run, GOOD, save_mapping=True)
    assert response.status_code == 400 and "save_mapping" in response.json_body["errors"]
    run = upload(editor, FILE, source=source).json_body["id"]
    assert patch(editor, run, {**GOOD, "App Name": "ignore"}, save_mapping=True).status_code == 400
    source.refresh_from_db()
    assert source.column_mapping == {}


def test_admin_may_save_too_and_viewer_is_denied(admin, viewer, source):
    run = upload(admin, FILE, source=source).json_body["id"]
    assert patch(viewer, run, GOOD, save_mapping=True).status_code == 403
    assert patch(admin, run, GOOD, save_mapping=True).status_code == 200


def test_mapping_is_tenant_scoped(b, api, admin):
    run = upload(api(b["users"]["admin"]), FILE).json_body["id"]
    assert patch(admin, run, GOOD).status_code == 404


def test_rerun_copies_the_mapping_and_the_error_report_uses_original_headings(editor):
    data = b"App Name,Status,Notes\r\nOkta,active,a\r\nBad,nonsense,b\r\n"
    run = upload(editor, data).json_body["id"]
    mapping = {"App Name": "name", "Status": "lifecycle", "Notes": "ignore"}
    assert patch(editor, run, mapping).json_body["counts"]["error"] == 1
    report = editor.client.get(f"/api/v1/imports/{run}/errors.csv").content
    headers, rows = csvio.read_csv(report)
    assert headers == ["import_row", "import_outcome", "import_errors", "App Name", "Status", "Notes"]
    assert rows[0]["App Name"] == "Bad" and rows[0]["Notes"] == "b" and rows[0]["import_row"] == "3"
    again = editor.post(f"imports/{run}/rerun").json_body
    assert again["mapping"] == mapping and again["status"] == "previewed" and again["counts"]["error"] == 1


def test_rerun_of_an_unmapped_run_stays_unmapped(editor):
    run = upload(editor, FILE).json_body["id"]
    again = editor.post(f"imports/{run}/rerun").json_body
    assert again["status"] == "uploaded" and again["needs_mapping"] is True
    assert patch(editor, again["id"], GOOD).json_body["status"] == "previewed"
