"""CSV export and template."""

import pytest

from catalog import services
from catalog.models import Resource
from core.models import AuditEvent
from imports import csvio

ROLES = ["admin", "editor", "viewer"]


def make(a, name, kind="application", **extra):
    data = {"name": name, "lifecycle": "active", "kind": kind, **extra}
    return services.create_resource(a["org"], a["users"]["editor"], data)


def read(response):
    assert response.content.startswith(b"\xef\xbb\xbf")
    text = response.content.decode("utf-8-sig")
    if len(text.splitlines()) == 1:  # headings only: an empty export
        return text.strip().split(","), []
    return csvio.read_csv(response.content)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("kind", ["application", "component"])
def test_every_role_can_export(a, api, role, kind):
    make(a, "Exported one", kind)
    response = api(a["users"][role]).client.get(f"/api/v1/{kind}s/export.csv")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert response["Content-Disposition"].startswith("attachment; filename=")
    headers, rows = read(response)
    assert headers == csvio.TEMPLATE[kind] and "softwarden_id" in headers
    assert [r["name"] for r in rows if r["name"] == "Exported one"] == ["Exported one"]


def test_export_is_signed_in_only_and_tenant_scoped(a, b, api):
    assert api().client.get("/api/v1/applications/export.csv").status_code in (401, 403)
    _, rows = read(api(a["users"]["viewer"]).client.get("/api/v1/applications/export.csv"))
    assert [r["name"] for r in rows] == ["orga App"]


def test_export_round_trips_record_values(a, api):
    a["app"].business_owner = a["people"]["dana"]
    a["app"].support_team = a["team"]
    a["app"].save()
    make(a, "=SUM(A1)", description="@cmd", purpose="-Internal", tags=["Finance", "Core"])
    make(a, "Quote", purpose="'-Internal", description="+1\tx")
    response = api(a["users"]["viewer"]).client.get("/api/v1/applications/export.csv")
    headers, rows = read(response)
    by_id = {r["softwarden_id"]: r for r in rows}
    resources = Resource.objects.filter(organization=a["org"], kind="application")
    assert len(by_id) == resources.count() == 3
    for r in resources.select_related("business_owner", "technical_owner", "support_team", "it_steward"):
        names = [t.name for t in r.tags.all()]
        assert by_id[str(r.id)] == csvio.record_values(r, names)
    purposes = {r["name"]: r["purpose"] for r in rows}
    assert purposes["=SUM(A1)"] == "-Internal" and purposes["Quote"] == "'-Internal"
    assert b"'-Internal" in response.content and b"''-Internal" in response.content  # escaped on the wire
    assert b"'=SUM(A1)" in response.content


def test_export_uses_list_filters_and_archived_option(a, api):
    old = make(a, "Old thing", lifecycle="retired")
    make(a, "Tagged", tags=["Finance"])
    services.archive_resource(a["org"], a["users"]["editor"], old)
    client = api(a["users"]["viewer"])

    def names(query):
        return sorted(r["name"] for r in read(client.client.get("/api/v1/applications/export.csv" + query))[1])

    assert names("") == ["Tagged", "orga App"]
    assert names("?tag=finance") == ["Tagged"]
    assert names("?lifecycle=retired") == []
    assert names("?include_archived=true") == ["Old thing", "Tagged", "orga App"]
    assert names("?archived=true") == ["Old thing"]
    assert names("?q=Tagged&page_size=1") == ["Tagged"]
    assert client.get("applications/export.csv?bogus=1").status_code == 400
    assert client.get("applications/export.csv?include_archived=maybe").status_code == 400
    assert client.client.get("/api/v1/applications/export.csv?gap=true").status_code == 200


def test_export_is_audited_without_contents(a, api):
    make(a, "Secret-ish name", description="private words")
    api(a["users"]["admin"]).client.get("/api/v1/applications/export.csv?lifecycle=active&include_archived=true")
    event = AuditEvent.objects.get(action="export")
    assert event.organization == a["org"] and event.actor == a["users"]["admin"]
    assert event.changes == {
        "kind": "application",
        "rows": 2,
        "filters": {"lifecycle": "active", "archived": "all"},
    }
    assert "private words" not in str(event.changes) and "Secret-ish" not in str(event.changes)


@pytest.mark.parametrize("kind", ["application", "component"])
@pytest.mark.parametrize("role,status", [("admin", 200), ("editor", 200), ("viewer", 403)])
def test_template_for_editors_and_admins(a, api, kind, role, status):
    response = api(a["users"][role]).client.get(f"/api/v1/{kind}s/template.csv")
    assert response.status_code == status
    if status == 200:
        assert response.content.startswith(b"\xef\xbb\xbf")
        assert response.content.decode("utf-8-sig").strip().split(",") == csvio.TEMPLATE[kind]
        assert "attachment" in response["Content-Disposition"]
