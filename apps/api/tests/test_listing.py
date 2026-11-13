"""Applications and components API: component CRUD, filters, cursor pagination, search."""

from datetime import timedelta

import pytest
from django.utils import timezone

from catalog import services
from catalog.models import Application, Person, RecordReview, Resource, Vendor
from core.models import AuditEvent


def make(a, name, kind="application", **extra):
    return services.create_resource(
        a["org"], a["users"]["editor"], {"name": name, "lifecycle": "active", "kind": kind, **extra}
    )


def names(response):
    assert response.status_code == 200, response.json_body
    return [r["name"] for r in response.json_body["results"]]


def walk(client, url, size):
    """Follow `next` to the end; returns the names in order and the number of pages."""
    seen, pages, cursor = [], 0, None
    while True:
        sep = "&" if "?" in url else "?"
        body = client.get(f"{url}{sep}page_size={size}" + (f"&cursor={cursor}" if cursor else "")).json_body
        seen += [r["name"] for r in body["results"]]
        pages += 1
        cursor = body["next"]
        if not cursor:
            return seen, pages


# ---- components -------------------------------------------------------------------


def test_component_crud_with_audit(a, api):
    client = api(a["users"]["editor"])
    created = client.post(
        "components",
        {
            "name": "Nightly export",
            "lifecycle": "active",
            "component_type": "scheduled_job",
            "host_runtime": "cron on batch-01",
            "repository_url": "https://git.example.test/export",
            "description": "Exports payroll nightly",
            "purpose": "Feeds the bank file",
        },
    )
    assert created.status_code == 201, created.json_body
    body = created.json_body
    assert body["kind"] == "component" and body["slug"] == "nightly-export"
    assert (body["component_type"], body["host_runtime"], body["repository_url"]) == (
        "scheduled_job",
        "cron on batch-01",
        "https://git.example.test/export",
    )
    assert body["description"] == "Exports payroll nightly" and body["purpose"] == "Feeds the bank file"
    assert "vendor" not in body and "support_url" not in body

    url = f"components/{body['id']}"
    patched = client.patch(url, {"component_type": "runbook", "criticality": "high", "built_by": "internal"})
    assert patched.status_code == 200 and patched.json_body["component_type"] == "runbook"
    assert client.get(url).json_body["criticality"] == "high"
    assert client.patch(url, {"component_type": "toaster"}).status_code == 400
    assert client.post("components", {"name": "", "lifecycle": "active"}).status_code == 400
    assert {e.action for e in AuditEvent.objects.filter(target_id=body["id"])} == {
        "resource.created",
        "resource.updated",
    }
    assert client.get("components").json_body["results"][0]["id"] == body["id"]
    assert names(client.get("applications")) == ["orga App"]  # a component never shows up as an application


def test_component_kind_cannot_be_overridden_by_the_payload(a, api):
    created = api(a["users"]["editor"]).post(
        "components", {"name": "Sneaky", "lifecycle": "active", "kind": "application"}
    )
    assert created.json_body["kind"] == "component"


def test_application_json_has_extension_fields(a, api):
    app = make(
        a,
        "Payroll",
        description="Pays people",
        built_by="vendor",
        license_model="commercial",
        documentation_url="https://docs.example.test/p",
        support_url="https://help.example.test/p",
    )
    body = api(a["users"]["viewer"]).get(f"applications/{app.id}").json_body
    assert (body["slug"], body["description"], body["built_by"], body["license_model"]) == (
        "payroll",
        "Pays people",
        "vendor",
        "commercial",
    )
    assert (
        body["documentation_url"] == "https://docs.example.test/p"
        and body["support_url"] == "https://help.example.test/p"
    )
    for key in ("vendor", "purpose", "gaps", "review_state", "next_review_date"):  # fields the SPA already reads
        assert key in body


# ---- filters ----------------------------------------------------------------------


def test_simple_filters(a, api):
    people, team, editor = a["people"], a["team"], a["users"]["editor"]
    vendor = Vendor.objects.create(organization=a["org"], name="Contoso Labs")
    make(
        a,
        "Alpha",
        criticality="high",
        business_owner=str(people["dana"].id),
        support_team=str(team.id),
        vendor=str(vendor.id),
        it_steward=editor.id,
    )
    make(a, "Beta", lifecycle="retired", criticality="low", technical_owner=str(people["dana"].id))
    make(a, "Gamma", criticality="high", business_owner=str(people["priya"].id))
    Resource.objects.filter(name="orga App").update(lifecycle="proposed", it_steward=None)
    client = api(a["users"]["viewer"])

    assert names(client.get("applications?lifecycle=retired")) == ["Beta"]
    assert names(client.get("applications?criticality=high")) == ["Alpha", "Gamma"]
    assert names(client.get(f"applications?owner={people['dana'].id}")) == ["Alpha", "Beta"]  # business or technical
    assert names(client.get(f"applications?support_team={team.id}")) == ["Alpha"]
    assert names(client.get(f"applications?it_steward={editor.id}")) == ["Alpha", "Beta", "Gamma"]
    assert names(client.get(f"applications?vendor={vendor.id}")) == ["Alpha"]
    assert names(client.get("applications?lifecycle=active&criticality=high&owner=" + str(people["priya"].id))) == [
        "Gamma"
    ]
    assert names(client.get("applications?lifecycle=retired&criticality=high")) == []


def test_lifecycle_accepts_comma_separated_list(a, api):
    make(a, "Alpha")
    make(a, "Beta", lifecycle="deprecated")
    make(a, "Gamma", lifecycle="retired")
    client = api(a["users"]["viewer"])
    assert names(client.get("applications?lifecycle=active,deprecated")) == ["Alpha", "Beta", "orga App"]
    assert names(client.get("applications?lifecycle=deprecated,retired")) == ["Beta", "Gamma"]
    assert names(client.get("applications?lifecycle=retired")) == ["Gamma"]  # single value still works
    assert names(client.get("applications?lifecycle=active,deprecated&archived=false")) == ["Alpha", "Beta", "orga App"]
    bad = client.get("applications?lifecycle=active,sleeping")
    assert bad.status_code == 400 and "lifecycle" in bad.json_body["errors"]
    assert client.get("applications?lifecycle=active,").status_code == 400


def test_component_type_filter(a, api):
    make(a, "Cron one", "component", component_type="script")
    make(a, "Flow one", "component", component_type="automation_flow")
    client = api(a["users"]["viewer"])
    assert names(client.get("components?component_type=script")) == ["Cron one"]
    assert names(client.get("components")) == ["Cron one", "Flow one"]


def test_archived_filter(a, api):
    make(a, "Live")
    old = make(a, "Old")
    Resource.objects.filter(id=old.id).update(archived_at=timezone.now())
    client = api(a["users"]["viewer"])
    assert names(client.get("applications")) == ["Live", "orga App"]
    assert names(client.get("applications?archived=false")) == ["Live", "orga App"]
    assert names(client.get("applications?archived=true")) == ["Old"]
    assert names(client.get("applications?archived=all")) == ["Live", "Old", "orga App"]


def test_gap_filter(a, api):
    people, team = a["people"], a["team"]
    make(
        a,
        "Complete",
        business_owner=str(people["dana"].id),
        technical_owner=str(people["priya"].id),
        support_team=str(team.id),
    )
    make(a, "No team", business_owner=str(people["dana"].id), technical_owner=str(people["priya"].id))
    sam = Person.objects.create(organization=a["org"], name="Sam Okafor")
    make(
        a,
        "Inactive owner",
        business_owner=str(people["dana"].id),
        support_team=str(team.id),
        technical_owner=str(sam.id),
    )
    Person.objects.filter(id=sam.id).update(active=False)  # owner left after being assigned
    client = api(a["users"]["viewer"])
    assert names(client.get("applications?gap=false")) == ["Complete"]
    assert names(client.get("applications?gap=true")) == ["Inactive owner", "No team", "orga App"]


def test_review_state_filter(a, api):
    people, team, editor = a["people"], a["team"], a["users"]["editor"]
    owners = {
        "business_owner": str(people["dana"].id),
        "technical_owner": str(people["priya"].id),
        "support_team": str(team.id),
    }
    confirmed, changed, overdue = (
        make(a, "Confirmed", **owners),
        make(a, "Changed", **owners),
        make(a, "Overdue", **owners),
    )
    review = {"confirmed_by": str(people["dana"].id), "evidence": "Email"}
    for r in (confirmed, changed, overdue):
        services.record_review(a["org"], editor, r, review)
    services.update_resource(a["org"], editor, changed, {"support_team": None})
    RecordReview.objects.filter(resource=overdue).update(next_review_date=timezone.now().date() - timedelta(days=1))
    client = api(a["users"]["viewer"])
    assert names(client.get("applications?review_state=confirmed")) == ["Confirmed"]
    assert names(client.get("applications?review_state=needs_confirmation")) == ["Changed"]
    assert names(client.get("applications?review_state=overdue")) == ["Overdue"]
    assert names(client.get("applications?review_state=unreviewed")) == ["orga App"]
    assert names(client.get("applications?review_state=overdue&gap=false")) == ["Overdue"]
    assert names(client.get("applications?review_state=overdue&gap=true")) == []


@pytest.mark.parametrize(
    "query,field",
    [
        ("lifecycle=sleeping", "lifecycle"),
        ("criticality=extreme", "criticality"),
        ("gap=maybe", "gap"),
        ("review_state=stale", "review_state"),
        ("archived=yes", "archived"),
        ("owner=not-a-uuid", "owner"),
        ("support_team=1", "support_team"),
        ("vendor=nope", "vendor"),
        ("it_steward=abc", "it_steward"),
        ("component_type=script", "component_type"),  # applications have no component types
        ("page_size=0", "page_size"),
        ("page_size=lots", "page_size"),
        ("cursor=garbage", "cursor"),
        ("colour=red", "colour"),
    ],
)
def test_invalid_filters_are_problem_400s(a, api, query, field):
    response = api(a["users"]["viewer"]).get("applications?" + query)
    assert response.status_code == 400
    assert response["Content-Type"] == "application/problem+json"
    assert response.json_body["status"] == 400 and field in response.json_body["errors"]


def test_vendor_filter_is_not_offered_on_components(a, api):
    response = api(a["users"]["viewer"]).get(f"components?vendor={a['team'].id}")
    assert response.status_code == 400 and "vendor" in response.json_body["errors"]


# ---- pagination -------------------------------------------------------------------


@pytest.fixture
def many(a):
    for i in range(1, 13):
        make(a, f"Item {i:02d}", criticality="high" if i % 3 == 0 else "")
    # same name, different ids: ordering must fall back to id
    make(a, "Item 05")
    return sorted(["orga App"] + [f"Item {i:02d}" for i in range(1, 13)] + ["Item 05"])


def test_cursor_pages_cover_everything_once(a, api, many):
    client = api(a["users"]["viewer"])
    seen, pages = walk(client, "applications", 5)
    assert seen == many and pages == 3
    first = client.get("applications?page_size=5").json_body
    assert first["previous"] is None and first["next"]
    assert set(first) == {"results", "next", "previous"}


def test_previous_cursor_returns_the_prior_page(a, api, many):
    client = api(a["users"]["viewer"])
    p1 = client.get("applications?page_size=5").json_body
    p2 = client.get(f"applications?page_size=5&cursor={p1['next']}").json_body
    p3 = client.get(f"applications?page_size=5&cursor={p2['next']}").json_body
    assert p3["next"] is None and p3["previous"]
    back2 = client.get(f"applications?page_size=5&cursor={p3['previous']}").json_body
    assert back2["results"] == p2["results"]
    back1 = client.get(f"applications?page_size=5&cursor={back2['previous']}").json_body
    assert back1["results"] == p1["results"] and back1["previous"] is None
    assert back1["next"] == p1["next"]


def test_default_and_maximum_page_size(a, api):
    for i in range(205):
        Resource.objects.create(organization=a["org"], name=f"Bulk {i:03d}", slug=f"bulk-{i}", lifecycle="active")
    Application.objects.bulk_create(
        [Application(resource=r, organization=a["org"]) for r in Resource.objects.filter(application__isnull=True)]
    )
    client = api(a["users"]["viewer"])
    assert len(client.get("applications").json_body["results"]) == 50
    assert len(client.get("applications?page_size=200").json_body["results"]) == 200
    # Above the maximum is a 400 naming page_size, not a silent clamp to 200 a client would take for everything.
    for size in ("201", "500"):
        refused = client.get(f"applications?page_size={size}")
        assert refused.status_code == 400 and "page_size" in refused.json_body["errors"]
    assert len(client.get("applications?page_size=7").json_body["results"]) == 7


def test_derived_filters_are_correct_across_pages(a, api):
    """gap/review_state are computed in Python; pages must still be full and complete."""
    people, team = a["people"], a["team"]
    for i in range(1, 13):
        extra = (
            {
                "business_owner": str(people["dana"].id),
                "technical_owner": str(people["priya"].id),
                "support_team": str(team.id),
            }
            if i % 2
            else {}
        )
        make(a, f"Rec {i:02d}", **extra)
    client = api(a["users"]["viewer"])
    seen, pages = walk(client, "applications?gap=true", 4)
    assert seen == ["orga App", "Rec 02", "Rec 04", "Rec 06", "Rec 08", "Rec 10", "Rec 12"] and pages == 2
    body = client.get("applications?gap=true&page_size=4").json_body
    assert len(body["results"]) == 4 and body["next"]  # full page even though most rows are filtered out
    seen, _ = walk(client, "applications?gap=false", 4)
    assert seen == [f"Rec {i:02d}" for i in range(1, 13, 2)]
    seen, _ = walk(client, "applications?review_state=unreviewed", 5)
    assert len(seen) == 13


# ---- search -----------------------------------------------------------------------


def test_search_full_text_over_name_description_and_purpose(a, api):
    make(a, "Payroll", description="Runs the monthly salary cycle")
    make(a, "Ledger", purpose="General accounting for finance")
    make(a, "Wiki")
    client = api(a["users"]["viewer"])
    assert names(client.get("applications?q=salary")) == ["Payroll"]
    assert names(client.get("applications?q=accounting")) == ["Ledger"]
    assert names(client.get("applications?q=salaries")) == ["Payroll"]  # stemmed
    assert names(client.get("applications?q=zebra")) == []
    assert names(client.get("applications?q=%20%20")) == names(client.get("applications"))  # blank q is no filter


def test_search_tolerates_typos(a, api):
    make(a, "Payroll")
    make(a, "Intranet Wiki")
    client = api(a["users"]["viewer"])
    assert names(client.get("applications?q=Payrol")) == ["Payroll"]
    assert names(client.get("applications?q=Pyroll")) == ["Payroll"]
    assert names(client.get("applications?q=intrnet")) == ["Intranet Wiki"]


def test_search_ranks_exact_then_prefix_then_other(a, api):
    make(a, "Billing Archive", description="old billing data")
    make(a, "Billing")
    make(a, "Aardvark", description="billing helper")
    make(a, "Billing Portal")
    client = api(a["users"]["viewer"])
    assert names(client.get("applications?q=billing")) == ["Billing", "Billing Archive", "Billing Portal", "Aardvark"]


def test_search_pages_keep_rank_order_and_combine_with_filters(a, api):
    for i in range(6):
        make(a, f"Billing {i}")
    make(a, "Billing", criticality="low")
    make(a, "Aardvark", description="billing helper")
    client = api(a["users"]["viewer"])
    seen, pages = walk(client, "applications?q=billing", 3)
    assert seen == ["Billing"] + [f"Billing {i}" for i in range(6)] + ["Aardvark"] and pages == 3
    assert names(client.get("applications?q=billing&criticality=low")) == ["Billing"]
    assert names(client.get("components?q=billing")) == []


def test_a_create_answers_the_new_records_location(a, api):
    # A 201 from a create carries Location, the URL its ETag belongs to.
    editor = api(a["users"]["editor"])
    for path, body in (
        ("applications", {"name": "Located", "lifecycle": "active"}),
        ("vendors", {"name": "Located Vendor"}),
        ("contracts", {"name": "Located contract"}),
        (f"applications/{a['app'].id}/instances", {"name": "Production", "status": "operational"}),
    ):
        created = editor.post(path, body)
        assert created.status_code == 201, (path, created.json_body)
        location = created["Location"]
        assert location == f"http://testserver/api/v1/{path}/{created.json_body['id']}", location
        assert editor.get(location.removeprefix("http://testserver/api/v1/")).status_code == 200


HERE = "http://testserver/api/v1/"


@pytest.mark.usefixtures("preview_on")  # ticket links are a preview feature in tests
def test_the_other_record_creating_201s_answer_location(a, api):
    # Every 201 that creates a record with a URL of its own says where it is.
    from django.core.files.uploadedfile import SimpleUploadedFile

    from catalog import assessments as assessments_service

    admin, editor, viewer = (api(a["users"][r]) for r in ("admin", "editor", "viewer"))
    saved = editor.post("saved-filters", {"name": "Mine", "kind": "application", "query": {"lifecycle": "active"}})
    assert saved["Location"] == f"{HERE}saved-filters/{saved.json_body['id']}"
    for path in ("invitations", "users"):  # a member is added through an invitation
        invited = admin.post(path, {"email": f"{path}@orga.test", "role": "viewer"})
        assert invited.status_code == 201, invited.json_body
        assert invited["Location"] == f"{HERE}invitations/{invited.json_body['id']}"
    feed = viewer.post("calendar-feed")
    assert feed.status_code == 201 and feed["Location"] == f"{HERE}calendar-feed"
    upload = SimpleUploadedFile("apps.csv", b"name,lifecycle\nZoom,live\n", content_type="text/csv")
    run = editor.client.post("/api/v1/imports", {"file": upload, "kind": "application"}).json()
    assert editor.post(f"imports/{run['id']}/commit").status_code == 409  # refused: the row has an error
    rerun = editor.post(f"imports/{run['id']}/rerun")
    assert rerun.status_code == 201 and rerun["Location"] == f"{HERE}imports/{rerun.json_body['id']}"
    assessment = assessments_service.create_assessment(
        a["org"], a["users"]["editor"], {"target": str(a["app"].id), "change_type": "change", "title": "Upgrade"}
    )
    base = f"assessments/{assessment.id}/ticket-links"
    link = editor.post(base, {"url": "https://contoso.atlassian.net/browse/ITOPS-1"})
    assert link.status_code == 201, link.json_body
    assert link["Location"] == f"{HERE}{base}/{link.json_body['id']}"
    assert editor.get(link["Location"].removeprefix(HERE)).status_code == 200


def test_location_keeps_the_installations_sub_path(a, api):
    # Under a script prefix (SCRIPT_NAME /softwarden) the route is resolved on path_info and the URL
    # keeps the prefix.
    from django.test import RequestFactory
    from rest_framework.response import Response

    from core.drf import set_location

    request = RequestFactory().post("/api/v1/vendors", SCRIPT_NAME="/softwarden")
    request.path = "/softwarden/api/v1/vendors"
    vendor = Vendor.objects.create(organization=a["org"], name="Prefixed")
    response = Response(status=201)
    set_location(request, response, f"/api/v1/vendors/{vendor.id}")
    assert response["Location"] == f"http://testserver/softwarden/api/v1/vendors/{vendor.id}"
    nowhere = Response(status=201)
    set_location(request, nowhere, f"/api/v1/no-such-thing/{vendor.id}")
    assert "Location" not in nowhere
