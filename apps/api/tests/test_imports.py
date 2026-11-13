"""CSV import: sources, upload, validate, match, preview, commit, cancel, error report,
purge. Every endpoint has a role-matrix and a cross-tenant test."""

from datetime import date, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from catalog import admin_services, services, state
from catalog.models import Application, Kind, Person, RecordReview, Resource, Team, Vendor
from core import worker
from core.models import AuditEvent, Schedule
from imports import csvio
from imports import importer as imports
from imports.models import ExternalIdentifier, ImportRun, ImportSource
from tests.conftest import TICK_MOMENTS, no_digest, rel_type


def sheet(kind, *rows):
    return csvio.write_csv(csvio.TEMPLATE[kind], list(rows))


def upload(who, data, kind="application", source=None, name="import.csv"):
    body = {"file": SimpleUploadedFile(name, data, content_type="text/csv"), "kind": kind}
    if source is not None:
        body["source"] = str(source.id)
    response = who.client.post("/api/v1/imports", body)
    response.json_body = response.json() if response.content else None
    return response


def run_of(who, run_id):
    return who.get(f"imports/{run_id}").json_body


def rows_of(who, run_id, **query):
    qs = "&".join(f"{k}={v}" for k, v in query.items())
    return who.get(f"imports/{run_id}/rows?{qs}").json_body["results"]


def make_app(a, name="Payroll", **extra):
    data = {"name": name, "lifecycle": "active", **extra}
    return services.create_resource(a["org"], a["users"]["admin"], data)


def review(a, resource):
    return services.record_review(
        a["org"], a["users"]["admin"], resource, {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Asked"}
    )


def export(resource):
    from catalog import tags

    return csvio.record_values(resource, tags.names_of(resource))


@pytest.fixture
def admin(a, api):
    return api(a["users"]["admin"])


@pytest.fixture
def editor(a, api):
    return api(a["users"]["editor"])


@pytest.fixture
def viewer(a, api):
    return api(a["users"]["viewer"])


# ---- import sources ---------------------------------------------------------------------------------------------


def test_source_crud_and_audit(a, admin):
    created = admin.post("import-sources", {"name": "IT spreadsheet", "kind": "application"})
    assert created.status_code == 201
    body = created.json_body
    assert body["name"] == "IT spreadsheet" and body["identifiers"] == 0
    assert admin.post("import-sources", {"name": "it SPREADSHEET", "kind": "component"}).status_code == 400
    assert admin.post("import-sources", {"name": "x", "kind": "integration"}).status_code == 400
    renamed = admin.patch(f"import-sources/{body['id']}", {"name": "CMDB export"})
    assert renamed.json_body["name"] == "CMDB export"
    assert admin.patch(f"import-sources/{body['id']}", {"kind": "component"}).status_code == 400
    assert admin.delete(f"import-sources/{body['id']}").status_code == 204
    actions = list(AuditEvent.objects.filter(action__startswith="import_source.").values_list("action", flat=True))
    assert sorted(actions) == ["import_source.created", "import_source.deleted", "import_source.updated"]


def test_source_delete_refused_while_keys_exist(a, admin):
    source = ImportSource.objects.create(organization=a["org"], name="CMDB", kind="application")
    ExternalIdentifier.objects.create(organization=a["org"], source=source, external_key="APP-1", resource=a["app"])
    refused = admin.delete(f"import-sources/{source.id}")
    assert refused.status_code == 409
    assert refused.json_body["blocking_references"] == {"external identifier": 1}
    assert ImportSource.objects.filter(id=source.id).exists()


def test_source_roles(a, admin, editor, viewer, api):
    source = ImportSource.objects.create(organization=a["org"], name="CMDB", kind="application")
    assert viewer.get("import-sources").status_code == 403
    assert api().get("import-sources").status_code in (401, 403)
    assert editor.get("import-sources").status_code == 200
    assert [s["name"] for s in admin.get("import-sources?kind=application").json_body] == ["CMDB"]
    for who in (editor, viewer):
        assert who.post("import-sources", {"name": "n", "kind": "application"}).status_code == 403
        assert who.patch(f"import-sources/{source.id}", {"name": "n"}).status_code == 403
        assert who.delete(f"import-sources/{source.id}").status_code == 403
    assert ImportSource.objects.filter(id=source.id, name="CMDB").exists()


def test_source_cross_tenant(a, b, api):
    other = ImportSource.objects.create(organization=b["org"], name="Theirs", kind="application")
    admin = api(a["users"]["admin"])
    assert admin.patch(f"import-sources/{other.id}", {"name": "mine"}).status_code == 404
    assert admin.delete(f"import-sources/{other.id}").status_code == 404
    assert admin.get("import-sources").json_body == []
    refused = upload(admin, sheet(Kind.APPLICATION, {"name": "X", "lifecycle": "active"}), source=other)
    assert refused.status_code == 400 and "source" in refused.json_body["errors"]


# ---- roles and tenancy on runs ----------------------------------------------------------------------------------


def test_run_endpoints_deny_viewers_and_signed_out(a, admin, viewer, api):
    run = upload(admin, sheet(Kind.APPLICATION, {"name": "Okta", "lifecycle": "active"})).json_body["id"]
    urls = [
        ("get", "imports"),
        ("get", f"imports/{run}"),
        ("patch", f"imports/{run}"),
        ("get", f"imports/{run}/rows"),
        ("post", f"imports/{run}/commit"),
        ("post", f"imports/{run}/cancel"),
        ("post", f"imports/{run}/rerun"),
        ("get", f"imports/{run}/errors.csv"),
        ("get", "imports/template.csv?kind=application"),
    ]
    for method, url in urls:
        assert getattr(viewer, method)(url).status_code == 403, url
        assert getattr(api(), method)(url).status_code in (401, 403), url
    assert upload(viewer, sheet(Kind.APPLICATION, {"name": "X", "lifecycle": "active"})).status_code == 403
    assert Resource.objects.filter(organization=a["org"], name="Okta").count() == 0


def test_run_endpoints_are_tenant_scoped(a, b, admin, api):
    theirs = api(b["users"]["admin"])
    run = upload(theirs, sheet(Kind.APPLICATION, {"name": "Okta", "lifecycle": "active"})).json_body["id"]
    for method, url in [
        ("get", f"imports/{run}"),
        ("patch", f"imports/{run}"),
        ("get", f"imports/{run}/rows"),
        ("post", f"imports/{run}/commit"),
        ("post", f"imports/{run}/cancel"),
        ("post", f"imports/{run}/rerun"),
        ("get", f"imports/{run}/errors.csv"),
    ]:
        assert getattr(admin, method)(url).status_code == 404, url
    assert admin.get("imports").json_body == []
    assert theirs.get("imports").json_body[0]["id"] == run
    assert ImportRun.objects.get(id=run).status == "previewed"


def test_template_download(editor):
    response = editor.client.get("/api/v1/imports/template.csv?kind=component")
    assert response.status_code == 200 and response.content.startswith(b"\xef\xbb\xbf")
    headers, _ = csvio.read_csv(response.content + b"x\n")  # headings only; a row to satisfy the reader
    assert "component_type" in headers and "tags" in headers
    assert editor.get("imports/template.csv?kind=nope").status_code == 400


# ---- upload and file limits --------------------------------------------------------------------------------------


def test_upload_creates_a_previewed_run(a, editor):
    data = sheet(
        Kind.APPLICATION,
        {"name": "Okta", "lifecycle": "active", "criticality": "High"},
        {"name": "Slack", "lifecycle": "evaluating"},
    )
    response = upload(editor, data, name="apps.csv")
    assert response.status_code == 201
    body = response.json_body
    assert body["status"] == "previewed" and body["filename"] == "apps.csv" and body["purged"] is False
    assert body["counts"]["create"] == 2 and body["counts"]["total"] == 2
    assert body["uploaded_by"]["email"] == "editor@orga.test"
    assert body["can_commit"] is True
    rows = rows_of(editor, body["id"])
    assert [r["row_number"] for r in rows] == [2, 3]
    assert rows[0]["changes"]["criticality"] == ["", "high"]
    assert "No business owner" in rows[0]["gaps"]
    assert Resource.objects.filter(organization=a["org"], name="Okta").count() == 0  # nothing saved by a preview
    assert [r["id"] for r in editor.get("imports").json_body] == [body["id"]]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "empty"),
        (b"name,lifecycle\n", "no rows"),
        ("name\nCaf\xe9\n".encode("latin-1"), "isn't UTF-8"),
        (b"name\n" + b"x\n" * 5001, "limit is 5,000"),
    ],
)
def test_upload_rejections(a, editor, data, message):
    response = upload(editor, data)
    assert response.status_code == 400
    assert message in response.json_body["detail"]
    assert ImportRun.objects.count() == 0


def test_upload_too_large(editor):
    response = upload(editor, b"name\n" + b"x" * (5 * 1024 * 1024))
    assert response.status_code == 400 and "5 MB" in response.json_body["detail"]


def test_upload_needs_file_and_kind(editor):
    assert editor.client.post("/api/v1/imports", {"kind": "application"}).status_code == 400
    bad = upload(editor, sheet(Kind.APPLICATION, {"name": "x", "lifecycle": "active"}), kind="integration")
    assert bad.status_code == 400 and "kind" in bad.json_body["errors"]


def test_bom_and_utf8_names_survive(a, editor):
    run = upload(editor, sheet(Kind.APPLICATION, {"name": "Café Planner", "lifecycle": "active"})).json_body
    editor.post(f"imports/{run['id']}/commit")
    assert Resource.objects.filter(organization=a["org"], name="Café Planner").exists()


def test_formulas_are_stored_as_text(a, editor):
    # A cell the user typed as =SUM(A1) is escaped by a spreadsheet export as '=SUM(A1); here it is sent raw.
    data = b"name,lifecycle,description\n=SUM(A1),active,@cmd\n"
    run = upload(editor, data).json_body
    assert editor.post(f"imports/{run['id']}/commit").status_code == 200
    app = Resource.objects.get(organization=a["org"], name="=SUM(A1)")
    assert app.description == "@cmd"


# ---- Validation -------------------------------------------------------------------------------------------------


def test_row_validation(a, admin, b):
    theirs = make_app_in(b)
    other_kind = services.create_resource(
        a["org"], a["users"]["admin"], {"kind": "component", "name": "Cron", "lifecycle": "active"}
    )
    archived = make_app(a, "Old CRM")
    services.archive_resource(a["org"], a["users"]["admin"], archived)
    data = sheet(
        Kind.APPLICATION,
        {"name": "", "lifecycle": "active"},  # 2 no name
        {"name": "A", "lifecycle": "live"},  # 3 unknown lifecycle
        {"name": "B", "lifecycle": "active", "criticality": "severe"},  # 4
        {"name": "C", "lifecycle": "active", "built_by": "aliens", "license_model": "pirate"},  # 5
        {"name": "D", "lifecycle": ""},  # 6 blank lifecycle on a new record
        {"name": "E", "lifecycle": "active", "documentation_url": "ftp://x"},  # 7
        {"name": "F", "lifecycle": "active", "softwarden_id": str(other_kind.id)},  # 8 other kind
        {"name": "G", "lifecycle": "active", "softwarden_id": str(archived.id)},  # 9 archived
        {"name": "H", "lifecycle": "active", "softwarden_id": str(theirs.id)},  # 10 other organization
        {"name": "I", "lifecycle": "active", "softwarden_id": "not-a-uuid"},  # 11
        {"name": "J", "lifecycle": "active", "external_key": "K1"},  # 12 key without a source
        {"name": "Fine", "lifecycle": "Active", "criticality": "Critical"},  # 13 labels are accepted
    )
    run = upload(admin, data).json_body
    rows = {r["row_number"]: r for r in rows_of(admin, run["id"])}
    cols = {n: [e["column"] for e in r["errors"]] for n, r in rows.items()}
    assert cols[2] == ["name"]
    assert (
        cols[3] == ["lifecycle"]
        and "proposed, evaluating, active, deprecated, retired" in rows[3]["errors"][0]["message"]
    )
    assert cols[4] == ["criticality"]
    assert cols[5] == ["built_by", "license_model"]
    assert cols[6] == ["lifecycle"] and rows[6]["errors"][0]["message"] == "Enter a lifecycle for new records."
    assert cols[7] == ["documentation_url"]
    assert "different kind" in rows[8]["errors"][0]["message"]
    assert "archived" in rows[9]["errors"][0]["message"]
    assert "No record in this organization" in rows[10]["errors"][0]["message"]
    assert cols[11] == ["softwarden_id"]
    assert cols[12] == ["external_key"]
    assert rows[13]["errors"] == [] and rows[13]["outcome"] == "create"
    assert run["counts"]["error"] == 11 and run["can_commit"] is False


def make_app_in(org_fixture, name="Their App"):
    return services.create_resource(
        org_fixture["org"], org_fixture["users"]["admin"], {"name": name, "lifecycle": "active"}
    )


def test_duplicate_ids_and_keys_error_both_rows(a, admin):
    app = make_app(a)
    source = ImportSource.objects.create(organization=a["org"], name="S", kind="application")
    data = sheet(
        Kind.APPLICATION,
        {"name": "One", "lifecycle": "active", "softwarden_id": str(app.id)},
        {"name": "Two", "lifecycle": "active", "softwarden_id": str(app.id).upper()},
        {"name": "Three", "lifecycle": "active", "external_key": "K"},
        {"name": "Four", "lifecycle": "active", "external_key": "K"},
    )
    run = upload(admin, data, source=source).json_body
    rows = rows_of(admin, run["id"])
    assert [r["outcome"] for r in rows] == ["error"] * 4
    assert "row 3" in rows[0]["errors"][0]["message"] and "row 2" in rows[1]["errors"][0]["message"]


def test_long_values_use_the_record_forms_limits(a, admin):
    data = sheet(
        Kind.APPLICATION,
        {"name": "x" * 201, "lifecycle": "active"},
        {"name": "ok", "lifecycle": "active", "documentation_url": "https://e.test/" + "a" * 200},
        {"name": "ok2", "lifecycle": "active", "description": "d" * 5001},
    )
    rows = rows_of(admin, upload(admin, data).json_body["id"])
    assert [r["errors"][0]["column"] for r in rows] == ["name", "documentation_url", "description"]


# ---- commit, create and update ----------------------------------------------------------------------------------


def test_commit_creates_through_services_and_marks_the_import(a, editor):
    data = sheet(
        Kind.APPLICATION,
        {
            "name": "Okta",
            "lifecycle": "active",
            "criticality": "high",
            "business_owner_email": "DANA@example.test",
            "purpose": "Single sign-on",
            "tags": "Identity; Security",
        },
    )
    Person.objects.filter(id=a["people"]["dana"].id).update(email="dana@example.test")
    run = upload(editor, data).json_body
    assert run["counts"]["create"] == 1
    done = editor.post(f"imports/{run['id']}/commit")
    assert done.status_code == 200 and done.json_body["status"] == "committed"
    assert done.json_body["counts"]["created"] == 1 and done.json_body["can_commit"] is False
    app = Resource.objects.get(organization=a["org"], name="Okta")
    assert app.created_by_import_id is not None and str(app.created_by_import_id) == run["id"]
    assert app.business_owner_id == a["people"]["dana"].id
    assert app.it_steward_id == a["users"]["editor"].id  # the uploader, as the form defaults
    assert app.application.purpose == "Single sign-on"
    assert export(app)["tags"] == "Identity; Security"
    created = AuditEvent.objects.get(action="resource.created", target_id=app.id)
    assert created.changes["import_run"] == run["id"]
    committed = AuditEvent.objects.get(action="import.committed")
    assert committed.changes["run"] == run["id"] and committed.changes["created"] == 1
    assert committed.target_type == "ImportRun" and str(committed.target_id) == run["id"]
    result = rows_of(editor, run["id"])[0]["result"]
    assert result["id"] == str(app.id)
    assert editor.post(f"imports/{run['id']}/commit").status_code == 409  # not twice


def test_commit_updates_only_changed_fields(a, admin):
    app = make_app(a, "Payroll", criticality="low", purpose="Pay people")
    data = sheet(
        Kind.APPLICATION,
        {"softwarden_id": str(app.id), "name": "Payroll", "criticality": "high", "description": ""},
    )
    run = upload(admin, data).json_body
    row = rows_of(admin, run["id"])[0]
    assert row["outcome"] == "update" and row["changes"] == {"criticality": ["low", "high"]}
    admin.post(f"imports/{run['id']}/commit")
    app.refresh_from_db()
    assert app.criticality == "high" and app.application.purpose == "Pay people"  # blank cells leave values
    event = AuditEvent.objects.filter(action="resource.updated", target_id=app.id).first()
    assert event is not None
    assert event.changes["import_run"] == run["id"] and event.changes["criticality"] == ["low", "high"]


def test_export_shaped_file_reimports_as_all_unchanged(a, admin):
    ops = Team.objects.get(organization=a["org"])
    vendor = Vendor.objects.create(organization=a["org"], name="Acme")
    services.update_resource(a["org"], a["users"]["admin"], a["app"], {"business_owner": str(a["people"]["dana"].id)})
    one = make_app(
        a,
        "Payroll, Inc.",
        description="Line one\nLine two, with a comma",
        criticality="high",
        technical_owner=str(a["people"]["priya"].id),
        support_team=str(ops.id),
        vendor=str(vendor.id),
        purpose="-Internal",
        built_by="vendor",
        license_model="commercial",
        documentation_url="https://docs.example.test/payroll",
        tags=["Finance", "HR"],
    )
    review(a, one)
    review(a, a["app"])
    before = {r.id: r.updated_at for r in Resource.objects.filter(organization=a["org"])}
    events = AuditEvent.objects.count()
    records = Resource.objects.filter(organization=a["org"], kind="application")
    data = csvio.write_csv(csvio.TEMPLATE[Kind.APPLICATION], [export(r) for r in records])
    run = upload(admin, data).json_body
    assert run["counts"]["unchanged"] == 2 and run["counts"]["create"] == 0 and run["counts"]["update"] == 0
    assert run["counts"]["needs_confirmation"] == 0 and run["can_commit"] is False  # nothing to commit
    assert all(r["outcome"] == "unchanged" for r in rows_of(admin, run["id"]))
    assert admin.post(f"imports/{run['id']}/commit").status_code == 200  # a no-op: nothing is written
    assert {r.id: r.updated_at for r in Resource.objects.filter(organization=a["org"])} == before
    assert AuditEvent.objects.count() == events + 2  # the upload and the commit, nothing on the records


def test_unchanged_commit_writes_nothing(a, admin):
    one = make_app(a, "Payroll")
    review(a, one)
    before = Resource.objects.get(id=one.id).updated_at
    run = upload(admin, sheet(Kind.APPLICATION, export(one))).json_body
    assert run["counts"]["unchanged"] == 1 and run["can_commit"] is False
    assert (
        admin.post(f"imports/{run['id']}/commit").status_code == 200
    )  # an all-unchanged run still commits, as a no-op
    assert Resource.objects.get(id=one.id).updated_at == before
    assert not AuditEvent.objects.filter(action="resource.updated").exists()
    latest = RecordReview.objects.filter(resource=one).first()
    assert state.review_state(Resource.objects.get(id=one.id), latest, date.today())[0] == "confirmed"


def test_internal_purposes_round_trip_with_zero_updates(a, admin):
    dash = make_app(a, "Dash", purpose="-Internal")
    quote = make_app(a, "Quote", purpose="'-Internal")
    data = csvio.write_csv(csvio.TEMPLATE[Kind.APPLICATION], [export(dash), export(quote)])
    assert b"'-Internal" in data and b"''-Internal" in data
    run = upload(admin, data).json_body
    assert run["counts"]["unchanged"] == 2 and run["counts"]["update"] == 0
    admin.post(f"imports/{run['id']}/commit")
    assert Application.objects.get(resource=dash).purpose == "-Internal"
    assert Application.objects.get(resource=quote).purpose == "'-Internal"
    assert not AuditEvent.objects.filter(action="resource.updated").exists()


def test_changing_one_confirmed_field_marks_only_that_record(a, admin):
    one, two = make_app(a, "One", criticality="low"), make_app(a, "Two", criticality="low")
    unreviewed = make_app(a, "Three", criticality="low")
    for r in (one, two):
        review(a, r)
    values = [dict(export(r)) for r in (one, two, unreviewed)]
    values[0]["criticality"] = "critical"  # confirmed field, reviewed record
    values[2]["criticality"] = "critical"  # confirmed field, but never reviewed
    values[1]["description"] = "Not a confirmed field"
    run = upload(admin, csvio.write_csv(csvio.TEMPLATE[Kind.APPLICATION], values)).json_body
    flags = {r["name"]: r["needs_confirmation"] for r in rows_of(admin, run["id"])}
    assert flags == {"One": True, "Two": False, "Three": False}
    assert run["counts"]["needs_confirmation"] == 1 and run["counts"]["update"] == 3
    admin.post(f"imports/{run['id']}/commit")
    today = date.today()
    got = {}
    for r in (one, two, unreviewed):
        latest = RecordReview.objects.filter(resource=r).first()
        got[r.name] = state.review_state(Resource.objects.get(id=r.id), latest, today)[0]
    assert got == {"One": "needs_confirmation", "Two": "confirmed", "Three": "unreviewed"}


def test_external_keys_reimport_without_duplicates(a, admin):
    source = ImportSource.objects.create(organization=a["org"], name="CMDB", kind="application")
    data = sheet(
        Kind.APPLICATION,
        {"external_key": "APP-1", "name": "Okta", "lifecycle": "active"},
        {"external_key": "APP-2", "name": "Slack", "lifecycle": "active"},
    )
    first = upload(admin, data, source=source).json_body
    admin.post(f"imports/{first['id']}/commit")
    assert ExternalIdentifier.objects.filter(source=source).count() == 2
    assert Resource.objects.filter(organization=a["org"], name__in=["Okta", "Slack"]).count() == 2
    again = upload(admin, data, source=source).json_body
    assert again["counts"]["unchanged"] == 2 and again["counts"]["create"] == 0
    changed = data.replace(b"Okta", b"Okta SSO")
    third = upload(admin, changed, source=source).json_body
    row = rows_of(admin, third["id"])[0]
    assert row["outcome"] == "update" and row["changes"]["name"] == ["Okta", "Okta SSO"] and row["matched"]
    admin.post(f"imports/{third['id']}/commit")
    assert Resource.objects.filter(organization=a["org"], name="Okta SSO").count() == 1
    assert Resource.objects.filter(organization=a["org"], name="Okta").count() == 0
    assert Resource.objects.filter(organization=a["org"], kind="application").count() == 3  # + the fixture app


def test_softwarden_id_row_gets_a_key_stored(a, admin):
    app = make_app(a)
    source = ImportSource.objects.create(organization=a["org"], name="CMDB", kind="application")
    row = {**export(app), "external_key": "K-9"}
    run = upload(admin, sheet(Kind.APPLICATION, row), source=source).json_body
    assert run["counts"]["unchanged"] == 1 and run["counts"]["new_keys"] == 1 and run["can_commit"] is True
    admin.post(f"imports/{run['id']}/commit")
    assert ExternalIdentifier.objects.get(source=source).resource_id == app.id
    clash = {**export(make_app(a, "Other")), "external_key": "K-9"}
    run = upload(admin, sheet(Kind.APPLICATION, clash), source=source).json_body
    assert "belongs to another record" in rows_of(admin, run["id"])[0]["errors"][0]["message"]


# ---- Possible matches --------------------------------------------------------------------------------------------


def test_possible_match_needs_a_decision(a, admin):
    twin = make_app(a, "Service Desk Plus")
    source = ImportSource.objects.create(organization=a["org"], name="CMDB", kind="application")
    data = sheet(
        Kind.APPLICATION,
        {"external_key": "SD-1", "name": "Service Desk Plus", "lifecycle": "active", "criticality": "high"},
    )
    run = upload(admin, data, source=source).json_body
    assert run["panels"]["matches"] == [
        {
            "row_number": 2,
            "name": "Service Desk Plus",
            "candidate": {"id": str(twin.id), "name": "Service Desk Plus", "known_to_other_source": False},
            "decision": None,
        }
    ]
    assert run["counts"]["pending_decisions"] == 1 and run["can_commit"] is False
    assert rows_of(admin, run["id"])[0]["possible_match"]["id"] == str(twin.id)
    refused = admin.post(f"imports/{run['id']}/commit")
    assert refused.status_code == 409 and refused.json_body["type"] == imports.REFUSED
    assert ImportRun.objects.get(id=run["id"]).status == "refused"
    assert Resource.objects.filter(organization=a["org"], name__iexact="service desk plus").count() == 1

    again = upload(admin, data, source=source).json_body
    patched = admin.patch(f"imports/{again['id']}", {"decisions": {"matches": {"2": "match"}}})
    assert patched.status_code == 200
    row = rows_of(admin, again["id"])[0]
    assert row["outcome"] == "update" and row["matched"]["id"] == str(twin.id)
    assert admin.post(f"imports/{again['id']}/commit").status_code == 200
    assert ExternalIdentifier.objects.get(source=source).resource_id == twin.id  # the approval stores the key
    twin.refresh_from_db()
    assert twin.criticality == "high"

    third = upload(admin, sheet(Kind.APPLICATION, {"name": "Service Desk Plus", "lifecycle": "active"})).json_body
    admin.patch(f"imports/{third['id']}", {"decisions": {"matches": {"2": "create"}}})
    assert admin.post(f"imports/{third['id']}/commit").status_code == 200
    assert Resource.objects.filter(organization=a["org"], name="Service Desk Plus").count() == 2


def test_decision_validation(a, admin):
    make_app(a, "Twin")
    run = upload(admin, sheet(Kind.APPLICATION, {"name": "Twin", "lifecycle": "active"})).json_body
    assert admin.patch(f"imports/{run['id']}", {"decisions": {"matches": {"9": "create"}}}).status_code == 400
    assert admin.patch(f"imports/{run['id']}", {"decisions": {"matches": {"2": "maybe"}}}).status_code == 400
    assert admin.patch(f"imports/{run['id']}", {"decisions": {"people": {"x@y.test": "create"}}}).status_code == 400
    assert admin.patch(f"imports/{run['id']}", {"exclude": [2]}).status_code == 400  # no error to exclude
    assert admin.patch(f"imports/{run['id']}", {"exclude": [99]}).status_code == 400


# ---- errors block the commit until excluded ------------------------------------------------------------------------


def test_error_rows_block_commit_until_excluded(a, admin):
    data = sheet(
        Kind.APPLICATION,
        {"name": "Good", "lifecycle": "active"},
        {"name": "Bad", "lifecycle": "live"},
    )
    run = upload(admin, data).json_body
    assert run["can_commit"] is False and run["counts"]["blocking_errors"] == 1
    refused = admin.post(f"imports/{run['id']}/commit")
    assert refused.status_code == 409 and refused.json_body["error_rows"] == [3]
    detail = run_of(admin, run["id"])
    assert detail["status"] == "refused" and "errors" in detail["refused_reason"]
    assert Resource.objects.filter(organization=a["org"], name="Good").count() == 0
    assert admin.patch(f"imports/{run['id']}", {"exclude": [3]}).status_code == 409  # a refused run is closed

    again = admin.post(f"imports/{run['id']}/rerun")
    assert again.status_code == 201 and again.json_body["id"] != run["id"]
    new = again.json_body["id"]
    patched = admin.patch(f"imports/{new}", {"exclude": [3]}).json_body
    assert patched["can_commit"] is True
    assert patched["counts"]["excluded"] == 1 and patched["counts"]["error"] == 0 and patched["counts"]["create"] == 1
    assert [r["excluded"] for r in rows_of(admin, new, outcome="excluded")] == [True]
    assert admin.post(f"imports/{new}/commit").status_code == 200
    assert Resource.objects.filter(organization=a["org"], name="Good").count() == 1
    assert Resource.objects.filter(organization=a["org"], name="Bad").count() == 0
    assert run_of(admin, new)["counts"]["excluded"] == 1


def test_staging_edits_are_audited_with_counts_only(a, admin):
    # A PATCH of decisions or exclusions is one import.staging_updated event with counts, never row contents.
    data = sheet(Kind.APPLICATION, {"name": "Good", "lifecycle": "active"}, {"name": "Secret Bad", "lifecycle": "live"})
    run = upload(admin, data).json_body
    assert admin.patch(f"imports/{run['id']}", {"exclude": [3]}).status_code == 200
    assert admin.patch(f"imports/{run['id']}", {"include": [3]}).status_code == 200
    events = list(AuditEvent.objects.filter(action="import.staging_updated").order_by("created_at"))
    assert [(e.changes["rows_excluded"], e.changes["rows_included"]) for e in events] == [(1, 0), (0, 1)]
    assert all(e.changes["decisions_changed"] == 0 and e.target_id == ImportRun.objects.get().id for e in events)
    assert "Secret Bad" not in str([e.changes for e in events])
    assert admin.patch(f"imports/{run['id']}", {"exclude": [99]}).status_code == 400  # refused: no event
    assert AuditEvent.objects.filter(action="import.staging_updated").count() == 2


@pytest.mark.django_db(transaction=True)
def test_parallel_commits_of_a_refused_run_write_one_event(a, admin):
    # Eight parallel commits of a run with an error: one refuses it (and audits), the rest find it closed.
    import threading

    from django.db import connection

    from core.problems import Conflict

    data = sheet(Kind.APPLICATION, {"name": "Good", "lifecycle": "active"}, {"name": "Bad", "lifecycle": "live"})
    run_id = upload(admin, data).json_body["id"]
    barrier = threading.Barrier(8)
    outcomes = []

    def commit():
        try:
            barrier.wait()
            imports.commit_run(a["org"], a["users"]["editor"], ImportRun.objects.get(id=run_id))
            outcomes.append("committed")
        except Conflict as e:
            outcomes.append(getattr(e, "extra", {}).get("type", "closed"))
        finally:
            connection.close()

    threads = [threading.Thread(target=commit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(outcomes) == 8 and "committed" not in outcomes
    assert AuditEvent.objects.filter(action="import.refused").count() == 1
    assert ImportRun.objects.get(id=run_id).status == "refused"


def test_error_report(a, admin):
    data = sheet(
        Kind.APPLICATION,
        {"name": "=Bad", "lifecycle": "live", "purpose": "-x"},
        {"name": "Good", "lifecycle": "active"},
        {"name": "Also bad", "lifecycle": "nope"},
    )
    run = upload(admin, data).json_body
    admin.patch(f"imports/{run['id']}", {"exclude": [4]})
    response = admin.client.get(f"/api/v1/imports/{run['id']}/errors.csv")
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    assert response.content.startswith(b"\xef\xbb\xbf")
    headers, rows = csvio.read_csv(response.content)
    assert headers[:3] == ["import_row", "import_outcome", "import_errors"]
    assert [r["import_row"] for r in rows] == ["2", "4"]
    assert [r["import_outcome"] for r in rows] == ["error", "excluded"]
    assert rows[0]["name"] == "=Bad" and rows[0]["purpose"] == "-x"  # escaped on write, restored on read
    assert b"'=Bad" in response.content
    assert "lifecycle:" in rows[0]["import_errors"]
    # The report goes back in as a file: its report columns are ignored.
    fixed = response.content.replace(b"live", b"active")
    assert upload(admin, fixed).status_code == 201


# ---- Owners, teams, vendors, steward -------------------------------------------------------------------------------


def test_owner_email_matches_case_insensitively(a, admin):
    Person.objects.filter(id=a["people"]["dana"].id).update(email="Dana.Ortiz@Example.test")
    data = sheet(
        Kind.APPLICATION,
        {"name": "Okta", "lifecycle": "active", "business_owner_email": "dana.ortiz@EXAMPLE.TEST"},
    )
    run = upload(admin, data).json_body
    assert run["panels"]["people"] == [] and run["panels"]["matched"]["people"] == 1
    assert run["counts"]["pending_decisions"] == 0 and rows_of(admin, run["id"])[0]["gaps"][0] == "No technical owner"
    admin.post(f"imports/{run['id']}/commit")
    assert Resource.objects.get(organization=a["org"], name="Okta").business_owner_id == a["people"]["dana"].id
    assert Person.objects.filter(organization=a["org"]).count() == 3


def test_inactive_owner_is_flagged_not_set(a, admin):
    Person.objects.filter(id=a["people"]["gone"].id).update(email="gone@example.test")
    data = sheet(
        Kind.APPLICATION,
        {"name": "Okta", "lifecycle": "active", "technical_owner_email": "gone@example.test"},
    )
    run = upload(admin, data).json_body
    row = rows_of(admin, run["id"])[0]
    assert row["outcome"] == "create" and row["warnings"][0]["column"] == "technical_owner_email"
    assert "Inactive owner" in row["gaps"] and "No technical owner" not in row["gaps"][:0]
    admin.post(f"imports/{run['id']}/commit")
    assert Resource.objects.get(organization=a["org"], name="Okta").technical_owner_id is None


def test_unmatched_owners_team_and_vendor_need_choices(a, admin):
    data = sheet(
        Kind.APPLICATION,
        {
            "name": "Okta",
            "lifecycle": "active",
            "business_owner_email": "new.owner@example.test",
            "business_owner_name": "New Owner",
            "technical_owner_name": "Name Only",
            "support_team": "Facilities IT",
            "vendor": "okta inc",
        },
        {
            "name": "Slack",
            "lifecycle": "active",
            "business_owner_email": "NEW.owner@example.test",
            "support_team": "it applications",  # matches the fixture team, case-insensitively
        },
    )
    run = upload(admin, data).json_body
    panels = run["panels"]
    assert [(e["key"], e["rows"], e["name"]) for e in panels["people"]] == [
        ("new.owner@example.test", [2, 3], "New Owner"),
        ("name only", [2], "Name Only"),
    ]
    assert [e["key"] for e in panels["teams"]] == ["facilities it"] and panels["matched"]["teams"] == 1
    assert [e["key"] for e in panels["vendors"]] == ["okta inc"]
    assert run["counts"]["pending_decisions"] == 4 and run["can_commit"] is False
    assert admin.post(f"imports/{run['id']}/commit").status_code == 409

    run = upload(admin, data).json_body
    choice = {
        "people": {"new.owner@example.test": "create", "name only": "gap"},
        "teams": {"facilities it": "create"},
        "vendors": {"okta inc": "gap"},
    }
    patched = admin.patch(f"imports/{run['id']}", {"decisions": choice}).json_body
    assert patched["can_commit"] is True and patched["counts"]["people_to_create"] == 1
    assert patched["decisions"]["people"]["name only"] == "gap"
    okta = rows_of(admin, run["id"])[0]
    assert okta["changes"]["business_owner"] == ["", "New Owner (new)"] and "No technical owner" in okta["gaps"]
    assert admin.post(f"imports/{run['id']}/commit").status_code == 200
    owner = Person.objects.get(organization=a["org"], email="new.owner@example.test")
    assert owner.name == "New Owner"
    assert Resource.objects.get(organization=a["org"], name="Okta").business_owner_id == owner.id
    assert Resource.objects.get(organization=a["org"], name="Slack").business_owner_id == owner.id
    support_team = Resource.objects.get(organization=a["org"], name="Slack").support_team
    assert support_team is not None and support_team.name == "IT Applications"
    assert Team.objects.filter(organization=a["org"], name="Facilities IT").exists()
    assert not Vendor.objects.filter(organization=a["org"]).exists()
    assert Person.objects.filter(organization=a["org"], name="Name Only").count() == 0  # names never auto-create
    assert run_of(admin, run["id"])["counts"]["people_created"] == 1
    assert AuditEvent.objects.filter(action="person.created").count() == 1


def test_owner_named_by_email_only_is_created_with_the_email_as_name(a, admin):
    data = sheet(Kind.APPLICATION, {"name": "Okta", "lifecycle": "active", "technical_owner_email": "t@example.test"})
    run = upload(admin, data).json_body
    admin.patch(f"imports/{run['id']}", {"decisions": {"people": {"t@example.test": "create"}}})
    admin.post(f"imports/{run['id']}/commit")
    assert Person.objects.get(organization=a["org"], email="t@example.test").name == "t@example.test"


def test_it_steward_must_be_an_active_admin_or_editor(a, admin):
    data = sheet(
        Kind.APPLICATION,
        {"name": "A", "lifecycle": "active", "it_steward_email": "ADMIN@orga.test"},
        {"name": "B", "lifecycle": "active", "it_steward_email": "viewer@orga.test"},
        {"name": "C", "lifecycle": "active", "it_steward_email": "nobody@example.test"},
    )
    run = upload(admin, data).json_body
    rows = rows_of(admin, run["id"])
    assert [r["outcome"] for r in rows] == ["create", "error", "error"]
    assert rows[1]["errors"][0]["column"] == "it_steward_email"
    admin.patch(f"imports/{run['id']}", {"exclude": [3, 4]})
    admin.post(f"imports/{run['id']}/commit")
    assert Resource.objects.get(organization=a["org"], name="A").it_steward_id == a["users"]["admin"].id


def test_unchanged_owner_with_no_email_round_trips(a, admin):
    nameless = Person.objects.create(organization=a["org"], name="Contractor Pat")
    app = make_app(a, "Legacy", business_owner=str(nameless.id))
    run = upload(admin, sheet(Kind.APPLICATION, export(app))).json_body
    assert run["counts"]["unchanged"] == 1 and run["counts"]["pending_decisions"] == 0


# ---- refused when a record changed after the preview -------------------------------------------------------------------


def test_commit_refused_when_a_record_changed_after_preview(a, admin):
    app = make_app(a, "Workday", criticality="low")
    other = make_app(a, "Zoom")
    data = sheet(
        Kind.APPLICATION,
        {**export(app), "criticality": "high"},
        {"name": "Brand new", "lifecycle": "active"},
        {**export(other), "description": "changed in the file"},
    )
    run = upload(admin, data).json_body
    services.update_resource(a["org"], a["users"]["editor"], app, {"description": "Edited meanwhile"})
    refused = admin.post(f"imports/{run['id']}/commit")
    assert refused.status_code == 409
    body = refused.json_body
    assert body["type"] == imports.REFUSED and "changed after this preview" in body["detail"]
    assert body["changed"] == [{"row": 2, "name": "Workday"}]
    detail = run_of(admin, run["id"])
    assert detail["status"] == "refused" and "Workday" in detail["refused_reason"]
    assert Resource.objects.filter(organization=a["org"], name="Brand new").count() == 0  # nothing saved
    app.refresh_from_db()
    other.refresh_from_db()
    assert app.criticality == "low" and other.description == ""
    assert AuditEvent.objects.filter(action="import.refused").count() == 1
    assert not AuditEvent.objects.filter(action="import.committed").exists()
    # Rerun the preview and it goes through.
    new = admin.post(f"imports/{run['id']}/rerun").json_body
    assert admin.post(f"imports/{new['id']}/commit").status_code == 200
    app.refresh_from_db()
    assert app.criticality == "high" and app.description == "Edited meanwhile"


def test_a_failure_during_commit_saves_nothing(a, admin, monkeypatch):
    app = make_app(a, "Workday")
    data = sheet(
        Kind.APPLICATION,
        {"name": "First new", "lifecycle": "active"},
        {**export(app), "criticality": "high"},
    )
    run = upload(admin, data).json_body
    real = services.update_resource

    def boom(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("disk full")

    monkeypatch.setattr(services, "update_resource", boom)
    with pytest.raises(RuntimeError):
        admin.client.post(f"/api/v1/imports/{run['id']}/commit")
    assert Resource.objects.filter(organization=a["org"], name="First new").count() == 0
    app.refresh_from_db()
    assert app.criticality == ""
    assert ImportRun.objects.get(id=run["id"]).status == "previewed"


def test_retiring_a_record_with_dependents_is_a_row_error(a, admin):
    from catalog.models import Relationship

    app = make_app(a, "Core")
    dependent = make_app(a, "Needs core")
    Relationship.objects.create(
        organization=a["org"],
        type=rel_type(a["org"], "depends_on"),
        source=dependent,
        target=app,
        created_by=a["users"]["admin"],
    )
    run = upload(admin, sheet(Kind.APPLICATION, {**export(app), "lifecycle": "retired"})).json_body
    row = rows_of(admin, run["id"])[0]
    assert row["outcome"] == "error" and row["errors"][0]["column"] == "lifecycle"


# ---- cancel, history ---------------------------------------------------------------------------------------------------


def test_cancel_and_history(a, admin, editor):
    run = upload(editor, sheet(Kind.APPLICATION, {"name": "Okta", "lifecycle": "active"})).json_body
    cancelled = admin.post(f"imports/{run['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json_body["status"] == "cancelled"
    assert admin.post(f"imports/{run['id']}/cancel").status_code == 409
    assert admin.post(f"imports/{run['id']}/commit").status_code == 409
    assert admin.patch(f"imports/{run['id']}", {}).status_code == 409
    assert Resource.objects.filter(organization=a["org"], name="Okta").count() == 0
    history = admin.get("imports").json_body
    assert history[0]["status"] == "cancelled" and history[0]["uploaded_by"]["email"] == "editor@orga.test"
    assert AuditEvent.objects.filter(action="import.cancelled").count() == 1


def test_rows_endpoint_filters_and_pages(a, admin):
    data = sheet(
        Kind.APPLICATION,
        *[{"name": f"App {i}", "lifecycle": "active"} for i in range(5)],
        {"name": "Odd", "lifecycle": "nope"},
    )
    run = upload(admin, data).json_body
    page = admin.get(f"imports/{run['id']}/rows?limit=2&offset=1").json_body
    assert page["count"] == 6 and [r["row_number"] for r in page["results"]] == [3, 4]
    assert admin.get(f"imports/{run['id']}/rows?outcome=error").json_body["count"] == 1
    assert admin.get(f"imports/{run['id']}/rows?q=odd").json_body["count"] == 1
    assert admin.get(f"imports/{run['id']}/rows?outcome=nope").status_code == 400
    assert admin.get(f"imports/{run['id']}/rows?limit=5000").status_code == 400


# ---- Purge ---------------------------------------------------------------------------------------------------------------


def test_purge_after_thirty_days_keeps_the_counts(a, admin):
    run = upload(admin, sheet(Kind.APPLICATION, {"name": "Okta", "lifecycle": "bad"})).json_body
    stored = ImportRun.objects.get(id=run["id"])
    assert stored.purge_after == services.org_today(a["org"]) + timedelta(days=30)
    assert imports.purge_expired() == 0
    assert imports.purge_expired(today=stored.purge_after) == 1
    purged = run_of(admin, run["id"])
    assert purged["purged"] is True and purged["counts"]["error"] == 1 and purged["can_commit"] is False
    stored.refresh_from_db()
    assert stored.file_bytes is None and stored.decisions == {}
    row = stored.rows.get()
    assert row.raw == {} and row.errors == [] and row.changes == {}
    gone = admin.get(f"imports/{run['id']}/errors.csv")
    assert gone.status_code == 410 and gone.json()["detail"] == "File purged after 30 days"
    assert admin.post(f"imports/{run['id']}/commit").status_code == 409
    assert admin.post(f"imports/{run['id']}/rerun").status_code == 409
    assert imports.purge_expired(today=stored.purge_after) == 0  # once
    assert AuditEvent.objects.filter(action="import.purged").count() == 1


@pytest.mark.parametrize("clock", TICK_MOMENTS, indirect=True)
def test_worker_tick_purges_expired_runs(a, admin, monkeypatch, clock):
    # Every schedule runs, so the failing purge is shown not to stop the tick; but no digest, which the second tick
    # (a minute later) would otherwise build when the pair spans the digest time, 08:00.
    no_digest()
    run = upload(admin, sheet(Kind.APPLICATION, {"name": "Okta", "lifecycle": "active"})).json_body
    ImportRun.objects.filter(id=run["id"]).update(purge_after=services.org_today(a["org"]) - timedelta(days=1))
    summary = worker.tick()
    assert summary["imports_purged"] == 1
    assert ImportRun.objects.get(id=run["id"]).purged_at is not None

    def boom(*args, **kwargs):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(imports, "purge_expired", boom)
    later = worker.tick(timezone.now() + timedelta(minutes=1))
    assert later["runs"] == 0 and later["schedules_failed"] == [
        "imports.purge"
    ]  # a failing purge doesn't stop the tick
    assert Schedule.objects.get(name="imports.purge").last_error == "RuntimeError"  # type only; detail in logs


def test_anonymizing_a_person_purges_runs_that_mention_them(a, admin):
    Person.objects.filter(id=a["people"]["dana"].id).update(email="dana@example.test")
    mention = upload(
        admin,
        sheet(Kind.APPLICATION, {"name": "Okta", "lifecycle": "active", "business_owner_email": "dana@example.test"}),
    ).json_body
    other = upload(admin, sheet(Kind.APPLICATION, {"name": "Slack", "lifecycle": "active"})).json_body
    assert imports.purge_for_person in admin_services.ANONYMIZE_HOOKS
    response = admin.post(f"people/{a['people']['dana'].id}/anonymize", {"confirm_name": "Dana Ortiz"})
    assert response.status_code == 200
    assert run_of(admin, mention["id"])["purged"] is True
    assert run_of(admin, other["id"])["purged"] is False
    event = AuditEvent.objects.get(action="person.anonymized")
    assert event.changes["purged"] == {"import_runs": 1}
    assert not ImportRun.objects.get(id=mention["id"]).rows.exclude(raw={}).exists()


def test_a_person_created_by_import_is_remembered_for_the_purge(a, admin):
    data = sheet(Kind.APPLICATION, {"name": "Okta", "lifecycle": "active", "business_owner_email": "new@example.test"})
    run = upload(admin, data).json_body
    admin.patch(f"imports/{run['id']}", {"decisions": {"people": {"new@example.test": "create"}}})
    admin.post(f"imports/{run['id']}/commit")
    person = Person.objects.get(organization=a["org"], email="new@example.test")
    admin.post(f"people/{person.id}/anonymize", {"confirm_name": "new@example.test"})
    assert run_of(admin, run["id"])["purged"] is True and run_of(admin, run["id"])["status"] == "committed"


# ---- OpenAPI ---------------------------------------------------------------------------------------------------------------


def test_schema_documents_the_import_endpoints(a, api):
    import json

    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    paths = doc["paths"]
    for url in (
        "/api/v1/import-sources",
        "/api/v1/import-sources/{id}",
        "/api/v1/imports",
        "/api/v1/imports/{id}",
        "/api/v1/imports/{id}/rows",
        "/api/v1/imports/{id}/commit",
        "/api/v1/imports/{id}/cancel",
        "/api/v1/imports/{id}/rerun",
        "/api/v1/imports/{id}/errors.csv",
        "/api/v1/imports/template.csv",
    ):
        assert url in paths, url
    assert "multipart/form-data" in paths["/api/v1/imports"]["post"]["requestBody"]["content"]
    assert {"409", "410"} <= set(paths["/api/v1/imports/{id}/commit"]["post"]["responses"])
