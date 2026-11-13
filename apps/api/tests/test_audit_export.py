"""The audit log as CSV (community `audit.log`) and JSON lines for a SIEM (enterprise `audit.export`): filters,
formula escaping, streaming, roles, the capability gate, snapshots without secrets, and the export's own event."""

import csv
import io
import json
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from catalog import audit_export, services
from core import capabilities as caps
from core.models import AuditEvent
from tests.conftest import revoke_capability, snapshot_problems
from tests.test_integrations import flow, record

CSV_URL = "/api/v1/audit-events/export.csv"
JSONL_URL = "/api/v1/audit-events/export.jsonl"


def get(api, user, url):
    return api(user).client.get(url)


def body(response) -> bytes:
    assert response.streaming, "exports are streamed"
    return b"".join(response.streaming_content)


def read_csv(response):
    data = body(response)
    assert data.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    return rows


def read_jsonl(response):
    return [json.loads(line) for line in body(response).decode("utf-8").splitlines()]


def enterprise(org):
    caps.set_edition(org, "enterprise")


# ---- CSV --------------------------------------------------------------------------------------------------------------


def test_csv_holds_the_log_newest_first_with_words_and_references(a, api):
    editor = a["users"]["editor"]
    services.update_resource(a["org"], editor, a["app"], {"purpose": "Pays people", "name": "Payroll"})
    services.create_instance(a["org"], editor, a["app"], {"name": "prod"})
    response = get(api, a["users"]["admin"], CSV_URL)
    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"].startswith('attachment; filename="softwarden-audit-log-')
    assert response["Cache-Control"] == "no-store"
    rows = read_csv(response)
    assert list(rows[0]) == audit_export.CSV_COLUMNS
    assert [r["action"] for r in rows] == ["instance.created", "resource.updated"]
    instance, updated = rows
    assert instance["label"] == "Instance added" and instance["target_type"] == "ApplicationInstance"
    assert instance["related_target_type"] == "Resource" and instance["related_target_id"] == str(a["app"].id)
    assert instance["target_kind"] == "application" and instance["target_name"] == "Payroll"
    assert updated["summary"].startswith("Name: orga App → Payroll") and "Purpose changed" in updated["summary"]
    assert updated["actor"] == "editor@orga.test" and updated["actor_id"] == str(editor.id)
    assert updated["time_utc"].endswith("Z") and updated["time_local"][-6] in "+-"
    event = AuditEvent.objects.get(id=updated["id"])
    assert updated["request_id"] == (str(event.request_id) if event.request_id else "")


def test_csv_takes_the_logs_filters(a, api):
    admin, editor = a["users"]["admin"], a["users"]["editor"]
    services.update_resource(a["org"], editor, a["app"], {"purpose": "Pays people"})
    services.create_instance(a["org"], admin, a["app"], {"name": "prod"})
    by_actor = read_csv(get(api, admin, f"{CSV_URL}?actor={editor.id}"))
    assert [r["action"] for r in by_actor] == ["resource.updated"]
    assert [r["action"] for r in read_csv(get(api, admin, f"{CSV_URL}?action=instance"))] == ["instance.created"]
    assert len(read_csv(get(api, admin, f"{CSV_URL}?related_target={a['app'].id}"))) == 1
    assert read_csv(get(api, admin, f"{CSV_URL}?from=2000-01-01&to=2000-01-02")) == []
    today = timezone.now().date().isoformat()
    assert len(read_csv(get(api, admin, f"{CSV_URL}?target_type=Resource&from=2000-01-01&to={today}"))) >= 1
    for bad in (
        "from=yesterday",
        "actor=me",
        "actor=²",
        "actor=" + "9" * 30,
        "cursor=x",
        "ordering=action",
        "nonsense=1",
    ):
        response = api(admin).get(f"audit-events/export.csv?{bad}")
        assert response.status_code == 400, bad
        assert bad.split("=")[0] in response.json_body["errors"]


def test_csv_cells_are_formula_escaped(a, api):
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"name": '=HYPERLINK("x")'})
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"name": "+cmd"})
    rows = read_csv(get(api, a["users"]["admin"], CSV_URL))
    assert rows[0]["target_name"] == "'+cmd"
    assert rows[0]["summary"].startswith("Name: =HYPERLINK") and rows[1]["target_name"] == "'+cmd"


def test_csv_streams_in_batches_with_flat_query_counts(a, api, monkeypatch):
    monkeypatch.setattr(audit_export, "BATCH", 10)
    AuditEvent.objects.bulk_create(
        AuditEvent(organization=a["org"], actor=a["users"]["editor"], action="team.created", changes={"name": f"T{i}"})
        for i in range(25)
    )
    response = get(api, a["users"]["admin"], CSV_URL)
    chunks = list(response.streaming_content)
    assert len(chunks) == 1 + 3  # the header, then one chunk per batch
    rows = list(csv.DictReader(io.StringIO(b"".join(chunks).decode("utf-8-sig"))))
    assert len(rows) == 25 and len({r["id"] for r in rows}) == 25

    def queries(n):
        AuditEvent.objects.bulk_create(
            AuditEvent(organization=a["org"], action="team.created", changes={"name": f"U{i}"}) for i in range(n)
        )
        with CaptureQueriesContext(connection) as ctx:
            b"".join(get(api, a["users"]["admin"], CSV_URL).streaming_content)
        return len(ctx)

    few, many = queries(0), queries(10)  # one more batch
    assert many - few <= 3  # a page query plus a couple of name lookups, not one per row


def test_too_large_an_export_is_refused_with_the_count(a, api, monkeypatch):
    monkeypatch.setattr(audit_export, "MAX_ROWS", 3)
    AuditEvent.objects.bulk_create(
        AuditEvent(organization=a["org"], action="team.created", changes={"name": f"T{i}"}) for i in range(4)
    )
    response = api(a["users"]["admin"]).get("audit-events/export.csv")
    assert response.status_code == 400
    message = "4 events match; one export holds at most 3. Narrow the date range (from, to) or the filters."
    assert response.json_body["errors"]["non_field"] == [message]  # as before
    # A problem type with the numbers, so a client needn't read the words.
    assert response.json_body["type"] == "/problems/too-many-events"
    assert (response.json_body["count"], response.json_body["limit"]) == (4, 3)
    assert response.json_body["detail"] == message and response.json_body["status"] == 400
    assert not AuditEvent.objects.filter(action="export").exists()
    assert get(api, a["users"]["admin"], f"{CSV_URL}?action=user").status_code == 200


def test_the_jsonl_export_refuses_with_the_same_problem(a, api, monkeypatch):
    enterprise(a["org"])
    monkeypatch.setattr(audit_export, "MAX_ROWS", 1)
    AuditEvent.objects.bulk_create(
        AuditEvent(organization=a["org"], action="team.created", changes={"name": f"T{i}"}) for i in range(1200)
    )
    matching = AuditEvent.objects.filter(organization=a["org"]).count()  # with the edition change's own event
    assert matching > 1000
    response = get(api, a["users"]["admin"], JSONL_URL)
    problem = response.json()
    assert response.status_code == 400 and response["Content-Type"] == "application/problem+json"
    assert (problem["type"], problem["count"], problem["limit"]) == ("/problems/too-many-events", matching, 1)
    assert problem["detail"].startswith(f"{matching:,} events match; one export holds at most 1.")


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_exports_are_admin_only(a, api, role):
    enterprise(a["org"])
    for url in (CSV_URL, JSONL_URL):
        response = get(api, a["users"][role], url)
        assert response.status_code == 403
        assert "capability" not in response.json()
    assert get(api, None, CSV_URL).status_code in (401, 403)


def test_an_export_is_audited_with_its_filters_and_count_and_not_in_itself(a, api):
    admin = a["users"]["admin"]
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"purpose": "Pays people"})
    rows = read_csv(get(api, admin, f"{CSV_URL}?action=resource"))
    assert len(rows) == 1
    event = AuditEvent.objects.get(action="export")
    assert event.actor == admin and event.organization == a["org"]
    assert event.changes == {"kind": "audit_event", "format": "csv", "rows": 1, "filters": {"action": "resource"}}
    log = api(admin).get("audit-events?action=export").json_body["results"]
    assert log[0]["summary"] == "CSV, 1 audit event"
    again = read_csv(get(api, admin, CSV_URL))
    assert [r["action"] for r in again] == ["export", "resource.updated"]  # the earlier export, not this one
    assert AuditEvent.objects.filter(action="export").latest("created_at").changes["rows"] == 2


def test_another_organizations_events_are_never_exported(a, b, api):
    services.update_resource(b["org"], b["users"]["editor"], b["app"], {"purpose": "Secret plans"})
    assert read_csv(get(api, a["users"]["admin"], CSV_URL)) == []
    assert read_csv(get(api, a["users"]["admin"], f"{CSV_URL}?target={b['app'].id}")) == []
    enterprise(a["org"])
    lines = read_jsonl(get(api, a["users"]["admin"], JSONL_URL))
    assert {x["organization"]["id"] for x in lines} == {str(a["org"].id)}
    assert "Secret plans" not in json.dumps(lines)


# ---- JSON lines (enterprise) ------------------------------------------------------------------------------------------


def test_jsonl_needs_the_audit_export_capability(a, api):
    revoke_capability(a["org"], "audit.export")
    response = get(api, a["users"]["admin"], JSONL_URL)
    assert response.status_code == 403
    problem = response.json()
    assert problem["type"] == "/problems/capability-required" and problem["capability"] == "audit.export"
    assert not AuditEvent.objects.filter(action="export").exists()


def test_jsonl_has_stable_fields_and_full_snapshots_oldest_first(a, api):
    enterprise(a["org"])
    editor = a["users"]["editor"]
    hr = record(a, "HR", "component")
    feed = flow(a, "Payroll feed", source=a["app"], target=hr)
    services.update_integration(a["org"], editor, feed, {"protocol": "SFTP"})
    response = get(api, a["users"]["admin"], JSONL_URL)
    assert response.status_code == 200
    assert response["Content-Type"] == "application/x-ndjson; charset=utf-8"
    lines = read_jsonl(response)
    assert lines[0]["action"] == "organization.edition_changed"  # oldest first
    updated = lines[-1]
    assert list(updated) == [
        "schema_version",
        "id",
        "organization",
        "time",
        "action",
        "label",
        "actor",
        "impersonated_by",
        "target",
        "related_target",
        "request_id",
        "summary",
        "changes",
        "prechange",
        "postchange",
    ]
    assert updated["schema_version"] == 1
    assert updated["organization"] == {"id": str(a["org"].id), "slug": a["org"].slug}
    assert updated["action"] == "resource.updated" and updated["label"] == "Fields changed"
    assert updated["actor"] == {"id": editor.id, "name": "editor@orga.test"}
    assert updated["target"] == {"type": "Resource", "id": str(feed.id), "kind": "integration", "name": "Payroll feed"}
    assert updated["related_target"] is None
    assert updated["time"].endswith("Z") and "." in updated["time"]
    assert updated["changes"]["protocol"] == ["", "SFTP"]
    assert updated["prechange"]["protocol"] == "" and updated["postchange"]["protocol"] == "SFTP"
    assert updated["postchange"]["source"] == {"id": str(a["app"].id), "display": "orga App"}
    event = AuditEvent.objects.get(id=updated["id"])
    assert updated["postchange"] == event.postchange and updated["request_id"] == (
        str(event.request_id) if event.request_id else None
    )
    assert AuditEvent.objects.filter(action="export").get().changes["format"] == "jsonl"


def test_jsonl_never_carries_a_secret(a, api):
    """Payloads and snapshots are redacted when written; the export redacts again, so even a stray key is masked."""
    enterprise(a["org"])
    api(a["users"]["admin"]).post(
        "connectors",
        {"source": "entra", "name": "Entra", "config": {"tenant_id": "t", "client_id": "c"}, "client_secret": "s3cr3t"},
    )
    # A row no service could write (bulk_create skips the conftest sweep, which would refuse it): the export masks it.
    AuditEvent.objects.bulk_create(
        [
            AuditEvent(
                organization=a["org"],
                action="connector.updated",
                changes={"name": "x", "api_token": "abc"},
                postchange={"name": "x", "nested": {"client_secret": "abc", "key_id": "k1"}},
            )
        ]
    )
    data = body(get(api, a["users"]["admin"], JSONL_URL)).decode()
    assert "s3cr3t" not in data and '"abc"' not in data and '"k1"' not in data
    for line in map(json.loads, data.splitlines()):
        fake = AuditEvent(prechange=line["prechange"], postchange=line["postchange"])
        assert not snapshot_problems(fake), line["action"]


def test_jsonl_hides_compliance_fields_while_the_profile_is_off(a, api):
    enterprise(a["org"])
    AuditEvent.objects.create(
        organization=a["org"],
        action="resource.updated",
        target_type="Resource",
        target_id=a["app"].id,
        changes={"is_gxp": [False, True]},
        prechange={"name": "orga App", "is_gxp": False},
        postchange={"name": "orga App", "is_gxp": True},
    )
    [line] = [x for x in read_jsonl(get(api, a["users"]["admin"], JSONL_URL)) if x["action"] == "resource.updated"]
    assert "is_gxp" not in line["changes"] and "is_gxp" not in line["postchange"]


def test_jsonl_takes_the_same_filters(a, api):
    enterprise(a["org"])
    services.create_instance(a["org"], a["users"]["editor"], a["app"], {"name": "prod"})
    tomorrow = (timezone.now() + timedelta(days=2)).date().isoformat()
    lines = read_jsonl(get(api, a["users"]["admin"], f"{JSONL_URL}?action=instance.created&to={tomorrow}"))
    assert [x["action"] for x in lines] == ["instance.created"]
    assert lines[0]["related_target"] == {"type": "Resource", "id": str(a["app"].id)}
    assert api(a["users"]["admin"]).get("audit-events/export.jsonl?to=soon").status_code == 400


@pytest.mark.parametrize("bad", ["²", "١", "9" * 30, "0", "-1"])
def test_user_id_filters_refuse_non_ascii_digits_and_overflow(a, api, bad):
    """core/drf.user_id (the it_steward filter) and the audit log's actor: a 400 field error, never a 500."""
    client = api(a["users"]["admin"])
    for url, name in ((f"applications?it_steward={bad}", "it_steward"), (f"audit-events?actor={bad}", "actor")):
        response = client.get(url)
        assert response.status_code == 400, (url, response.status_code)
        assert name in response.json_body["errors"]
