"""The data-flow map (catalog/data_flows.py, catalog/data_flow_views.py): an integration's
data categories through the API (validation, audit, filter), the report's filters and every risk (with the
vendor_risk capability on and off), the graph's shape, the CSV (escaping, audit, impersonation), the data-quality
rules on integrations, the Needs attention queue, roles, the capability and tenancy."""

import csv
import io
from datetime import timedelta

import pytest

from catalog import data_flows, quality, services
from catalog import vendor_risk as risk
from catalog.models import Integration, Resource, Vendor, VendorRiskDocument
from core.impersonation_routes import RESTRICTED_ROUTES
from core.models import AuditEvent, CapabilityOverride
from core.request_context import forget_organization
from notify.attention import UNCOUNTED, attention_queues
from notify.digest import PENDING_SECTIONS, SECTION_NAMES, SECTIONS
from notify.digest import _line as digest_line
from notify.digest import _path as digest_path
from notify.digest import plan as digest_plan
from notify.digest import render as digest_render
from tests.test_platform import make_staff, start
from tests.test_problems import is_problem

pytestmark = pytest.mark.django_db

REPORT = "reports/data-flows"


def app(f, name, vendor=None, classification="", **extra):
    data = {"kind": "application", "name": name, "lifecycle": "active", "data_classification": classification}
    if vendor is not None:
        data["vendor"] = str(vendor.id)
    return services.create_resource(f["org"], f["users"]["editor"], data | extra)


def component(f, name, classification=""):
    data = {"kind": "component", "name": name, "lifecycle": "active", "data_classification": classification}
    return services.create_resource(f["org"], f["users"]["editor"], data)


def flow(f, name, source, target, **data):
    payload = {"name": name, "lifecycle": "active", "source": str(source.id), "target": str(target.id)} | data
    return services.create_integration(f["org"], f["users"]["editor"], payload)


def vendor(f, name="Fabrikam"):
    return Vendor.objects.get_or_create(organization=f["org"], name=name)[0]


def revoke(org, capability):
    CapabilityOverride.objects.create(organization=org, capability=capability, effect="revoke", reason="test")
    forget_organization(org.pk)


def report(f, filters=None):
    return data_flows.report(f["org"], filters or data_flows.Filters())


def rows_by_name(body):
    return {r["name"]: r for r in body["flows"]}


def codes(row):
    return [(r["code"], r["end"]) for r in row["risks"]]


def csv_rows(response):
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


@pytest.fixture
def world(a):
    """HR (internal component) sends restricted payroll data to Payroll (an application of Fabrikam, classified
    confidential); CRM exchanges confidential data with Mailer (Contoso, no auth recorded)."""
    hr = component(a, "HR", "restricted")
    payroll = app(a, "Payroll", vendor(a), "confidential")
    crm = app(a, "CRM", None, "confidential")
    mailer = app(a, "Mailer", vendor(a, "Contoso"), "confidential")
    feed = flow(
        a,
        "Payroll feed",
        hr,
        payroll,
        data_classification="restricted",
        data_categories=["financial", "personal"],
        auth_method="ssh_key",
        direction="one_way",
    )
    sync = flow(a, "Mail sync", crm, mailer, data_classification="confidential", direction="two_way")
    return {**a, "hr": hr, "payroll": payroll, "crm": crm, "mailer": mailer, "feed": feed, "sync": sync}


# ---- data categories on integrations --------------------------------------------------------------------------------


def test_data_categories_are_validated_kept_in_order_and_audited(world, api):
    editor = api(world["users"]["editor"])
    url = f"integrations/{world['sync'].id}"
    response = editor.patch(url, {"data_categories": ["health", "personal", "health"]})
    assert response.status_code == 200, response.json_body
    body = editor.get(url).json_body
    assert body["data_categories"] == ["personal", "health"] and body["contains_personal_data"] is True
    event = AuditEvent.objects.filter(action="resource.updated", target_id=world["sync"].id).latest("created_at")
    assert event.changes["data_categories"] == [[], ["personal", "health"]]
    for bad in (["gossip"], "personal", [1], {"personal": True}):
        response = editor.patch(url, {"data_categories": bad})
        assert response.status_code == 400 and "data_categories" in response.json_body["errors"], bad
    assert Integration.objects.get(resource=world["sync"]).data_categories == ["personal", "health"]
    # Health data is special-category personal data (GDPR Art. 9): on its own it is personal data
    assert editor.patch(url, {"data_categories": ["health"]}).json_body["contains_personal_data"] is True
    assert editor.patch(url, {"data_categories": ["financial"]}).json_body["contains_personal_data"] is False
    assert editor.patch(url, {"data_categories": None}).json_body["data_categories"] == []
    created = editor.post(
        "integrations",
        {
            "name": "New flow",
            "lifecycle": "active",
            "source": str(world["crm"].id),
            "target": str(world["payroll"].id),
            "data_classification": "restricted",
            "data_categories": ["special_category", "credentials"],
        },
    )
    assert created.status_code == 201, created.json_body
    assert created.json_body["data_categories"] == ["special_category", "credentials"]
    assert created.json_body["contains_personal_data"] is True


def test_viewers_cannot_edit_classification_or_categories(world, api):
    viewer = api(world["users"]["viewer"])
    url = f"integrations/{world['sync'].id}"
    assert viewer.patch(url, {"data_categories": ["personal"]}).status_code == 403
    assert viewer.patch(url, {"data_classification": "public"}).status_code == 403
    assert Integration.objects.get(resource=world["sync"]).data_categories == []


def test_the_integration_list_filters_by_data_category(world, api):
    viewer = api(world["users"]["viewer"])
    names = [r["name"] for r in viewer.get("integrations?data_category=personal,health").json_body["results"]]
    assert names == ["Payroll feed"]
    assert viewer.get("integrations?data_category=gossip").status_code == 400


# ---- the report -----------------------------------------------------------------------------------------------------


def test_the_report_lists_live_flows_most_sensitive_first_with_their_ends(world, api):
    services.archive_resource(world["org"], world["users"]["editor"], flow(world, "Old", world["crm"], world["hr"]))
    flow(world, "Retired", world["crm"], world["hr"], lifecycle="retired")
    flow(world, "Unclassified", world["hr"], world["crm"])
    body = api(world["users"]["viewer"]).get(REPORT).json_body
    assert [r["name"] for r in body["flows"]] == ["Payroll feed", "Mail sync", "Unclassified"]
    feed = body["flows"][0]
    assert feed["classification"] == "restricted" and feed["categories"] == ["personal", "financial"]
    assert feed["contains_personal_data"] is True and feed["auth_method"] == "ssh_key"
    assert feed["source"]["vendor"] is None and feed["source"]["classification"] == "restricted"
    assert feed["target"]["vendor"] == {"id": str(vendor(world).id), "name": "Fabrikam"}
    assert feed["target"]["path"] == f"applications/{world['payroll'].id}" and feed["path"].startswith("integrations/")
    assert "_steward_id" not in feed
    assert body["summary"] == {
        "flows": 3,
        "risky": 2,
        "personal_data": 1,
        "by_classification": {"restricted": 1, "confidential": 1, "internal": 0, "public": 0, "none": 1},
        "by_risk": {"no_dpa": 2, "no_auth_method": 1, "down_classification": 1},
    }
    assert body["vendor_risk"] is True and body["as_of"] == services.org_today(world["org"]).isoformat()


def test_the_report_filters(world, api):
    viewer = api(world["users"]["viewer"])

    def names(query):
        response = viewer.get(f"{REPORT}?{query}")
        assert response.status_code == 200, (query, response.json_body)
        return [r["name"] for r in response.json_body["flows"]]

    flow(world, "Public", world["crm"], world["hr"], data_classification="public")
    assert names("min_classification=restricted") == ["Payroll feed"]
    assert names("min_classification=confidential") == ["Payroll feed", "Mail sync"]
    assert names("min_classification=public") == ["Payroll feed", "Mail sync", "Public"]
    assert names("category=personal") == ["Payroll feed"] and names("category=health,other") == []
    assert names(f"source={world['crm'].id}") == ["Mail sync", "Public"]
    assert names(f"target={world['payroll'].id}") == ["Payroll feed"]
    assert names(f"record={world['hr'].id}") == ["Payroll feed", "Public"]
    assert names("risk=no_auth_method") == ["Mail sync"]
    assert names("risk=down_classification,no_auth_method") == ["Payroll feed", "Mail sync"]
    body = viewer.get(f"{REPORT}?risk=no_auth_method").json_body
    assert body["summary"]["flows"] == 1  # over the flows returned
    for bad in ("min_classification=secret", "category=gossip", "source=nope", "risk=bad", "colour=red"):
        assert viewer.get(f"{REPORT}?{bad}").status_code == 400, bad


def test_no_dpa_follows_the_vendor_risk_file(world):
    org, editor = world["org"], world["users"]["editor"]
    feed = rows_by_name(report(world))["Payroll feed"]
    assert ("no_dpa", "target") in codes(feed)
    assert feed["target"]["vendor_risk"] == {
        "has_profile": False,
        "risk_tier": None,
        "processes_personal_data": False,
        "dpa_status": None,
        "dpa": False,
        "assurance": False,
    }
    risk.update_profile(org, editor, vendor(world), {"dpa_status": "yes", "risk_tier": "high"})
    feed = rows_by_name(report(world))["Payroll feed"]
    assert "no_dpa" not in [c for c, _ in codes(feed)] and feed["target"]["vendor_risk"]["dpa"] is True
    # A DPA document counts while it hasn't expired.
    risk.update_profile(org, editor, vendor(world), {"dpa_status": "no"})
    today = services.org_today(org)
    doc = risk.create_document(org, editor, vendor(world), {"kind": "dpa", "expires_on": today.isoformat()})
    assert "no_dpa" not in [c for c, _ in codes(rows_by_name(report(world))["Payroll feed"])]
    VendorRiskDocument.objects.filter(id=doc.id).update(expires_on=today - timedelta(days=1))
    assert ("no_dpa", "target") in codes(rows_by_name(report(world))["Payroll feed"])
    # Internal personal data still needs a DPA (an HR sync); internal data that isn't personal doesn't.
    services.update_integration(org, editor, world["feed"], {"data_classification": "internal"})
    [no_dpa] = [r for r in rows_by_name(report(world))["Payroll feed"]["risks"] if r["code"] == "no_dpa"]
    assert no_dpa["why"] == "Personal data goes to Payroll, and its vendor Fabrikam has no DPA."
    services.update_integration(org, editor, world["feed"], {"data_categories": ["financial"]})
    assert "no_dpa" not in [c for c, _ in codes(rows_by_name(report(world))["Payroll feed"])]
    services.update_integration(
        org, editor, world["feed"], {"data_classification": "", "data_categories": ["health", "special_category"]}
    )
    assert ("no_dpa", "target") in codes(rows_by_name(report(world))["Payroll feed"])  # unclassified but personal
    services.update_integration(org, editor, world["feed"], {"data_categories": ["health"]})
    assert ("no_dpa", "target") in codes(rows_by_name(report(world))["Payroll feed"])  # health alone is personal
    # A component (no vendor) never raises it.
    hr_flow = flow(world, "To HR", world["payroll"], world["hr"], data_classification="restricted")
    assert codes(rows_by_name(report(world))[hr_flow.name]) == []


def test_a_dpa_marked_not_applicable_answers_only_for_data_that_isnt_personal(world):
    org, editor = world["org"], world["users"]["editor"]
    risk.update_profile(org, editor, vendor(world), {"dpa_status": "not_applicable"})
    feed = rows_by_name(report(world))["Payroll feed"]  # restricted and personal
    assert ("no_dpa", "target") in codes(feed) and feed["target"]["vendor_risk"]["dpa_status"] == "not_applicable"
    services.update_integration(org, editor, world["feed"], {"data_categories": ["financial", "credentials"]})
    assert "no_dpa" not in [c for c, _ in codes(rows_by_name(report(world))["Payroll feed"])]
    risk.update_profile(org, editor, vendor(world), {"dpa_status": "no"})
    assert ("no_dpa", "target") in codes(rows_by_name(report(world))["Payroll feed"])


def test_without_vendor_risk_the_dpa_is_unknown_and_no_dpa_is_not_raised(world, api):
    revoke(world["org"], "vendor_risk")
    body = api(world["users"]["viewer"]).get(REPORT).json_body
    assert body["vendor_risk"] is False and body["summary"]["by_risk"]["no_dpa"] == 0
    feed = rows_by_name(body)["Payroll feed"]
    assert feed["target"]["vendor"]["name"] == "Fabrikam" and feed["target"]["vendor_risk"] is None
    assert codes(feed) == [("down_classification", "target")]
    sync = rows_by_name(body)["Mail sync"]
    assert codes(sync) == [("no_auth_method", "target")]  # judged without the risk file


def test_no_auth_method_is_for_external_vendors_with_none_or_nothing_recorded(world):
    org, editor = world["org"], world["users"]["editor"]
    sync = rows_by_name(report(world))["Mail sync"]
    [no_auth] = [r for r in sync["risks"] if r["code"] == "no_auth_method"]
    assert no_auth["end"] == "target" and "isn't recorded" in no_auth["why"] and "Contoso" in no_auth["why"]
    assert no_auth["label"] == "No authentication to an external vendor"
    services.update_integration(org, editor, world["sync"], {"auth_method": "none"})
    [no_auth] = [r for r in rows_by_name(report(world))["Mail sync"]["risks"] if r["code"] == "no_auth_method"]
    assert "with no authentication" in no_auth["why"]
    services.update_integration(org, editor, world["sync"], {"auth_method": "oauth"})
    assert "no_auth_method" not in [c for c, _ in codes(rows_by_name(report(world))["Mail sync"])]
    # An internal end (a component, or an application with no vendor) is not an external vendor.
    internal = flow(world, "Internal", world["mailer"], world["crm"], data_classification="internal")
    assert codes(rows_by_name(report(world))[internal.name]) == []


def test_down_classification_compares_with_each_receiving_record(world):
    org, editor = world["org"], world["users"]["editor"]
    feed = rows_by_name(report(world))["Payroll feed"]
    [down] = [r for r in feed["risks"] if r["code"] == "down_classification"]
    assert down["end"] == "target" and down["why"] == (
        "Restricted data goes to Payroll, which is classified confidential."
    )
    # A receiving record with no classification isn't compared.
    services.update_resource(org, editor, world["payroll"], {"data_classification": ""})
    assert "down_classification" not in [c for c, _ in codes(rows_by_name(report(world))["Payroll feed"])]
    # One-way: only the target receives; two-way: the source too.
    services.update_resource(org, editor, world["crm"], {"data_classification": "internal"})
    assert ("down_classification", "source") in codes(rows_by_name(report(world))["Mail sync"])
    services.update_integration(org, editor, world["sync"], {"direction": "one_way"})
    assert "down_classification" not in [c for c, _ in codes(rows_by_name(report(world))["Mail sync"])]


def test_a_two_way_flow_judges_the_source_vendor_too(world):
    org, editor = world["org"], world["users"]["editor"]
    back = flow(
        world,
        "Back",
        world["payroll"],
        world["crm"],
        data_classification="confidential",
        direction="two_way",
        auth_method="oauth",
    )
    assert codes(rows_by_name(report(world))["Back"]) == [("no_dpa", "source")]
    services.update_integration(org, editor, back, {"direction": "one_way"})
    assert codes(rows_by_name(report(world))["Back"]) == []


# ---- the graph -------------------------------------------------------------------------------------------------------


def test_the_graph_has_records_as_nodes_and_flows_as_weighted_labelled_edges(world, api, monkeypatch):
    body = api(world["users"]["viewer"]).get(f"{REPORT}/graph").json_body
    assert [n["name"] for n in body["nodes"]] == ["CRM", "HR", "Mailer", "Payroll"]
    nodes = {n["name"]: n for n in body["nodes"]}
    assert nodes["Payroll"]["risky"] is True and nodes["HR"]["risky"] is False
    assert nodes["Payroll"]["vendor"]["name"] == "Fabrikam" and nodes["HR"]["kind"] == "component"
    assert nodes["HR"]["classification"] == "restricted" and nodes["HR"]["weight"] == 4
    assert nodes["Payroll"]["path"] == f"applications/{world['payroll'].id}"
    assert nodes["Payroll"]["map_path"] == f"applications/{world['payroll'].id}/map"
    assert nodes["Payroll"]["detail"]["vendor_risk"]["dpa"] is False
    feed, sync = body["edges"]
    assert feed["id"] == f"integration:{world['feed'].id}"
    assert feed["source"] == str(world["hr"].id) and feed["target"] == str(world["payroll"].id)
    assert (
        feed["label"] == "Personal data, Financial" and feed["weight"] == 4 and feed["classification"] == "restricted"
    )
    assert feed["risks"] == ["no_dpa", "down_classification"] and feed["reads"] == "Sends data to"
    assert feed["detail"]["integration"]["name"] == "Payroll feed" and feed["detail"]["auth_method"] == "ssh_key"
    assert sync["label"] == "Mail sync" and sync["weight"] == 3 and sync["reads"] == "Exchanges data with"
    assert body["truncated"] is False
    assert [x["classification"] for x in body["legend"]] == ["restricted", "confidential", "internal", "public", None]
    assert [x["weight"] for x in body["legend"]] == [4, 3, 2, 1, 0]
    monkeypatch.setattr(data_flows, "MAX_GRAPH_EDGES", 1)
    body = api(world["users"]["viewer"]).get(f"{REPORT}/graph?min_classification=confidential").json_body
    assert body["truncated"] is True and len(body["edges"]) == 1 and len(body["nodes"]) == 2
    filtered = api(world["users"]["viewer"]).get(f"{REPORT}/graph?category=personal").json_body
    assert [e["id"] for e in filtered["edges"]] == [f"integration:{world['feed'].id}"]


# ---- the CSV ---------------------------------------------------------------------------------------------------------


def test_the_csv_is_escaped_audited_and_restricted_while_impersonating(world, api):
    services.update_integration(world["org"], world["users"]["editor"], world["sync"], {"name": "=HYPERLINK(1)"})
    viewer = api(world["users"]["viewer"])
    response = viewer.client.get(f"/api/v1/{REPORT}/export.csv?min_classification=confidential")
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    assert "softwarden-data-flows-" in response["Content-Disposition"]
    rows = csv_rows(response)
    assert [r["integration"] for r in rows] == ["Payroll feed", "'=HYPERLINK(1)"]
    feed = rows[0]
    assert feed["categories"] == "personal; financial" and feed["contains_personal_data"] == "yes"
    assert feed["target_vendor"] == "Fabrikam" and feed["target_vendor_dpa"] == "no"
    assert feed["target_vendor_assurance"] == "no" and feed["source_vendor_dpa"] == ""
    assert feed["source_vendor_assurance"] == ""
    assert feed["risks"] == (
        "Confidential or personal data to a vendor with no DPA (target); Classified above the receiving record (target)"
    )
    event = AuditEvent.objects.get(organization=world["org"], action="export")
    assert event.actor == world["users"]["viewer"]
    assert event.changes == {"kind": "data_flows", "rows": 2, "filters": {"min_classification": "confidential"}}
    assert ("GET", "api/v1/reports/data-flows/export.csv") in RESTRICTED_ROUTES
    staff = api(make_staff())
    assert start(staff, world["org"], world["users"]["viewer"]).status_code == 201
    is_problem(staff.client.get(f"/api/v1/{REPORT}/export.csv"), 403, "/problems/impersonation-restricted")
    assert staff.get(REPORT).status_code == 200  # reading the report is support work
    assert AuditEvent.objects.filter(organization=world["org"], action="export").count() == 1


def test_without_vendor_risk_the_csv_leaves_the_dpa_cells_empty(world, api):
    revoke(world["org"], "vendor_risk")
    rows = csv_rows(api(world["users"]["viewer"]).client.get(f"/api/v1/{REPORT}/export.csv"))
    assert {r["target_vendor_dpa"] for r in rows} == {""}


# ---- data quality ----------------------------------------------------------------------------------------------------


def failing(f, record):
    return sorted(r.id for r in quality.evaluate(f["org"], [record.id])[record.id].failing)


def test_the_data_flow_rules_check_integrations(world, api):
    org, editor = world["org"], world["users"]["editor"]
    assert failing(world, world["feed"]) == []
    assert failing(world, world["sync"]) == ["flow_categories"]  # confidential with no categories
    bare = flow(world, "Bare", world["crm"], world["hr"])
    assert failing(world, bare) == ["flow_classification"]  # no classification; categories don't apply
    services.update_integration(org, editor, bare, {"data_classification": "internal"})
    assert failing(world, bare) == []  # categories are asked for from confidential up
    body = api(world["users"]["viewer"]).get("data-quality/failures?kind=integration").json_body
    assert [r["name"] for r in body["results"]] == ["Mail sync"]
    summary = api(world["users"]["viewer"]).get("data-quality/summary").json_body
    kinds = {k["kind"]: k for k in summary["by_kind"]}
    assert kinds["integration"]["records"] == 3 and kinds["integration"]["complete"] == 2
    rules = {r["id"]: r for r in api(world["users"]["viewer"]).get("data-quality/rules").json_body}
    assert rules["flow_classification"]["kinds"] == ["integration"] and rules["flow_classification"]["status"] == "on"
    # Archived integrations aren't counted; the rules need data_flows.
    services.archive_resource(org, editor, world["sync"])
    assert api(world["users"]["viewer"]).get("data-quality/failures?kind=integration").json_body["results"] == []
    revoke(org, "data_flows")
    assert quality.status(quality.BY_ID["flow_classification"], org)[0] == quality.UNAVAILABLE
    assert "integration" not in quality.checked_kinds(org)
    summary = api(world["users"]["viewer"]).get("data-quality/summary").json_body
    assert "integration" not in [k["kind"] for k in summary["by_kind"]]


def test_the_data_flow_rules_appear_in_the_data_quality_queue(world, api):
    flow(world, "Bare", world["crm"], world["hr"])
    rows = api(world["users"]["admin"]).get("needs-attention?scope=all").json_body["data_quality"]
    assert {r["rule"]: r["count"] for r in rows}.get("flow_classification") == 1
    assert {r["rule"]: r["count"] for r in rows}.get("flow_categories") == 1


# ---- Needs attention ---------------------------------------------------------------------------------------------------


def test_risky_flows_are_a_counted_needs_attention_queue_routed_to_the_steward_and_a_digest_section(world, api):
    body = api(world["users"]["editor"]).get("needs-attention?scope=mine").json_body
    rows = {r["name"]: r for r in body["data_flows"]}
    assert list(rows) == ["Payroll feed", "Mail sync"]
    feed = rows["Payroll feed"]
    assert feed["kind"] == "data_flow" and feed["id"] == str(world["feed"].id)
    assert [r["code"] for r in feed["risks"]] == ["no_dpa", "down_classification"]
    assert feed["source"] == {"id": str(world["hr"].id), "kind": "component", "name": "HR"}
    assert feed["steward"]["id"] == world["users"]["editor"].id and "Fabrikam" in feed["why"]
    # Counted, and a digest section after Vendor risk (End of life, between them, left the digest with its reminders),
    # since the data-flow map's SPA package shows the queue.
    assert "data_flows" not in UNCOUNTED and "data_flows" not in PENDING_SECTIONS
    assert SECTION_NAMES["data_flows"] == "Data flows"
    names = [q for q, _ in SECTIONS]
    assert names.index("data_flows") == names.index("vendor_risk") + 1
    assert digest_path("data_flows", feed) == f"/integrations/{world['feed'].id}"
    assert digest_line("data_flows", feed) == (
        "Payroll feed — Confidential or personal data to a vendor with no DPA, Classified above the receiving record"
    )
    queues, counts = attention_queues(world["org"], world["users"]["editor"], services.org_today(world["org"]))
    assert "data_flows" in queues and queues["data_flows"]
    assert counts["all"] == sum(len(q) for q in queues.values())
    revoke(world["org"], "data_flows")
    assert api(world["users"]["editor"]).get("needs-attention?scope=all").json_body["data_flows"] == []


def test_the_digest_line_names_each_risk_once_on_a_two_way_flow(world):
    both = flow(
        world,
        "Both vendors",
        world["payroll"],
        world["mailer"],
        data_classification="confidential",
        direction="two_way",
    )
    rows = attention_queues(world["org"], world["users"]["editor"], services.org_today(world["org"]))[0]["data_flows"]
    row = next(r for r in rows if r["id"] == str(both.id))
    labels = [r["label"] for r in row["risks"]]
    assert len(labels) > len(set(labels))  # a risk per receiving end
    assert digest_line("data_flows", row) == f"Both vendors — {', '.join(dict.fromkeys(labels))}"


def test_the_digest_renders_a_data_flows_section_until_the_capability_is_revoked(world):
    org, editor = world["org"], world["users"]["editor"]
    today = services.org_today(org)
    items, _ = digest_plan(org, today)
    _, text, html = digest_render(org, editor, items[editor.id], today)
    assert "DATA FLOWS (2)" in text and "Payroll feed — " in text
    assert f"/integrations/{world['feed'].id}" in text and "Data flows" in html
    revoke(org, "data_flows")
    items, _ = digest_plan(org, today)
    entries = items.get(editor.id, [])
    assert not [e for e in entries if e[0] == "data_flows"]
    if entries:
        assert "DATA FLOWS" not in digest_render(org, editor, entries, today)[1]


def test_a_risky_flow_can_be_snoozed(world, api):
    editor = api(world["users"]["editor"])
    response = editor.post(
        "needs-attention/snoozes",
        {
            "subject_type": "data_flows",
            "subject_id": str(world["feed"].id),
            "kind": "snooze",
            "until": (services.org_today(world["org"]) + timedelta(days=7)).isoformat(),
        },
    )
    assert response.status_code in (200, 201), response.json_body
    rows = editor.get("needs-attention?scope=all").json_body["data_flows"]
    assert [r["name"] for r in rows if r["snooze"] is None] == ["Mail sync"]


def test_the_report_takes_a_fixed_number_of_queries(world):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    data_flows.report(world["org"], data_flows.Filters())  # warm the capability cache
    with CaptureQueriesContext(connection) as before:
        data_flows.report(world["org"], data_flows.Filters())
    for i in range(5):
        target = app(world, f"Vendor app {i}", vendor(world, f"Vendor {i}"), "internal")
        flow(world, f"More {i}", world["hr"], target, data_classification="restricted")
    with CaptureQueriesContext(connection) as after:
        assert len(data_flows.report(world["org"], data_flows.Filters())["flows"]) == 7
    assert len(after.captured_queries) == len(before.captured_queries)


# ---- roles, capability, tenancy --------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
def test_every_role_reads_the_report_graph_and_csv(world, api, role):
    client = api(world["users"][role])
    for path in (REPORT, f"{REPORT}/graph", f"{REPORT}/export.csv"):
        assert client.client.get(f"/api/v1/{path}").status_code == 200, path


def test_anonymous_and_no_capability_are_refused(world, api):
    for path in (REPORT, f"{REPORT}/graph", f"{REPORT}/export.csv"):
        assert api().client.get(f"/api/v1/{path}").status_code in (401, 403)
    revoke(world["org"], "data_flows")
    for path in (REPORT, f"{REPORT}/graph", f"{REPORT}/export.csv"):
        is_problem(api(world["users"]["viewer"]).client.get(f"/api/v1/{path}"), 403, "/problems/capability-required")


def test_flows_never_cross_organizations(world, b, api):
    other_a = component(b, "B source", "restricted")
    other_t = app(b, "B target", vendor(b, "Fabrikam"), "public")
    flow(b, "B flow", other_a, other_t, data_classification="restricted")
    viewer = api(world["users"]["viewer"])
    names = [r["name"] for r in viewer.get(REPORT).json_body["flows"]]
    assert names == ["Payroll feed", "Mail sync"]
    graph = viewer.get(f"{REPORT}/graph").json_body
    assert str(other_a.id) not in {n["id"] for n in graph["nodes"]}
    for name in ("source", "target", "record"):
        assert viewer.get(f"{REPORT}?{name}={other_a.id}").status_code == 404
        assert viewer.get(f"{REPORT}/export.csv?{name}={other_t.id}").status_code == 404
    rows = csv_rows(viewer.client.get(f"/api/v1/{REPORT}/export.csv"))
    assert "B flow" not in [r["integration"] for r in rows]
    # Another organization's vendor risk file never answers for this one's vendor of the same name.
    risk.update_profile(b["org"], b["users"]["editor"], vendor(b, "Fabrikam"), {"dpa_status": "yes"})
    assert rows_by_name(report(world))["Payroll feed"]["target"]["vendor_risk"]["dpa"] is False
    assert api(b["users"]["viewer"]).get(f"{REPORT}?record={world['hr'].id}").status_code == 404
    assert Resource.objects.filter(organization=b["org"], kind="integration").count() == 1
