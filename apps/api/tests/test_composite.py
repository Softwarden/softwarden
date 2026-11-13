"""Composite read endpoints (api/composite.py, api/composite_views.py): shapes and filters, roles with a session and a read-only token, tenancy, capabilities, the same numbers as the
dashboard and reports for the same data, flat query counts, and the OpenAPI description."""

import json
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from api import composite_serializers as cs
from api import tokens
from catalog import services
from catalog.models import Resource, Team, Vendor
from core import capabilities as caps
from core.models import Membership
from discovery import engine as discovery
from discovery.models import Connector, Observation
from money import contracts, renewals
from tests.test_discovery_sources import make_connector
from tests.test_reports import contract, iso, keys_match, line, record, today
from tests.test_tokens import Bearer

pytestmark = [pytest.mark.django_db, pytest.mark.one_source_of_truth]

PATHS = {
    "search": ("composite/search?q=App", cs.CompositeSearchSerializer),
    "deadlines": ("composite/deadlines", cs.CompositeDeadlinesSerializer),
    "renewals": ("composite/renewals", cs.CompositeRenewalsSerializer),
    "spend": ("composite/spend", cs.CompositeSpendSerializer),
    "discovery": ("composite/discovery-summary", cs.CompositeDiscoverySerializer),
    "data_gaps": ("composite/data-gaps", cs.CompositeDataGapsSerializer),
    "credentials": ("composite/expiring-credentials", cs.CompositeCredentialsSerializer),
}


def depends(a, source, target):
    """source depends on target."""
    return services.create_relationship(
        a["org"], a["users"]["editor"], {"type": "depends_on", "source": str(source.id), "target": str(target.id)}
    )


@pytest.fixture
def owned(a):
    """A renewal a contract drives (lines on two records), a renewal of its own covering the fixture's application,
    a contract that drives no renewal and has no owner, records with and without owners, and a dependency."""
    org, editor, people = a["org"], a["users"]["editor"], a["people"]
    acme = Vendor.objects.create(organization=org, name="Acme", account_manager=people["priya"])
    bare = Vendor.objects.create(organization=org, name="Bare vendor")
    people["dana"].email = "dana@orga.test"
    people["dana"].save()
    crm = record(a, "CRM", business_owner=people["dana"], technical_owner=people["priya"], support_team=a["team"])
    ledger = record(a, "Ledger", kind="component")
    driven = contract(a, "Driven", vendor=str(acme.id), end_date=iso(a, 100), notice_value=60, notice_unit="days",
                      auto_renew="yes", amount="1200.00", renewal_owner=str(people["dana"].id),
                      create_renewal_obligation=True)  # fmt: skip  # deadline in 40 days
    line(a, driven, crm, "60")
    line(a, driven, ledger)
    alone = contract(a, "Alone", vendor=str(bare.id), end_date=iso(a, org.renewal_lead_days + 45), auto_renew="no")
    domain = renewals.create_renewal(org, editor, {"name": "Domain", "renewal_date": iso(a, 20), "auto_renew": "yes",
                                                   "notice_value": 10, "notice_unit": "days", "followup": editor.id,
                                                   "applications": [str(a["app"].id)]})  # fmt: skip  # 10 days
    contract(a, "Far", end_date=iso(a, 500))
    depends(a, a["app"], crm)
    depends(a, crm, ledger)
    driven.refresh_from_db()
    return {"crm": crm, "ledger": ledger, "driven": driven, "alone": alone, "domain": domain, "acme": acme}


def read_only_token(a, role="viewer"):
    owner = Membership.objects.get(organization=a["org"], user=a["users"][role])
    _, secret = tokens.create_token(a["org"], a["users"]["admin"], owner, "MCP", True, today(a) + timedelta(days=30))
    return Bearer(secret)


def body(response):
    assert response.status_code == 200, (response.status_code, response.content[:500])
    return json.loads(response.content)


# ---- roles, tokens and shapes ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
def test_every_composite_answers_every_role_and_matches_its_description(a, api, owned, role):
    client = api(a["users"][role])
    for path, serializer in PATHS.values():
        keys_match(serializer(), body(client.get(path)), path)
    dependencies = f"composite/dependencies?target={owned['crm'].id}"
    keys_match(cs.CompositeDependenciesSerializer(), body(client.get(dependencies)), dependencies)


def test_a_read_only_token_reads_every_composite(a, owned):
    bearer = read_only_token(a)
    for path, serializer in PATHS.values():
        keys_match(serializer(), body(bearer.get(path)), path)
    assert body(bearer.get(f"composite/dependencies?target={owned['crm'].id}"))["target"]["name"] == "CRM"
    # Every composite route is GET only: a write method is a 405 whoever asks (core.drf.RolePermission answers 405
    # before the read-only refusal for a method the route doesn't have).
    assert bearer.post("composite/renewals", {}).status_code == 405


def test_signed_out_and_bad_tokens_are_refused(a, api, owned):
    for path, _ in PATHS.values():
        assert api().get(path).status_code in (401, 403), path
        assert Bearer("sw_" + "x" * 40).get(path).status_code == 401, path


def test_unknown_parameters_and_bad_values_are_problems(a, api, owned):
    client = api(a["users"]["viewer"])
    cases = {
        "composite/renewals?within_days=-1": "within_days",
        "composite/renewals?within_days=731": "within_days",
        "composite/deadlines?limit=0": "limit",
        "composite/deadlines?colour=red": "colour",
        "composite/spend?by=colour": "by",
        "composite/spend?groups=999": "groups",
        "composite/dependencies": "target",
        "composite/dependencies?target=nope": "target",
        f"composite/dependencies?target={owned['crm'].id}&depth=99": "depth",
        f"composite/dependencies?target={owned['crm'].id}&direction=up": "direction",
        f"composite/dependencies?target={owned['crm'].id}&show_instances=true": "show_instances",
        "composite/data-gaps?rule=nope": "rule",
        "composite/data-gaps?kind=vendor": "kind",
        "composite/expiring-credentials?within_days=0": "within_days",
        "composite/expiring-credentials?kind=password": "kind",
        "composite/discovery-summary?status=new": "status",
        "composite/search": "q",
        "composite/search?q=App&limit=500": "limit",
    }
    for path, param in cases.items():
        response = client.get(path)
        assert response.status_code == 400, (path, response.status_code)
        assert response["Content-Type"] == "application/problem+json"
        assert param in response.json_body["errors"], (path, response.json_body)


# ---- renewals with owners --------------------------------------------------------------------------------------------


def test_renewals_say_who_owns_each_one(a, api, owned):
    found = api(a["users"]["viewer"]).get("composite/renewals?within_days=90").json_body
    assert found["count"] == 3 and not found["truncated"]
    rows = {r["name"]: r for r in found["results"]}
    assert list(rows) == ["Domain", "Driven", "Alone"]  # soonest first; Far is beyond 90 days

    driven = rows["Driven"]
    assert driven["source"] == "renewal" and driven["path"] == f"renewals/{owned['driven'].renewal_obligation_id}"
    assert driven["contract"] == {"id": str(owned["driven"].id), "name": "Driven"}
    assert driven["date"] == iso(a, 40) and driven["days"] == 40 and driven["annualized_cost"] == "1200.00"
    dana = {"id": str(a["people"]["dana"].id), "name": "Dana Ortiz", "email": "dana@orga.test", "active": True}
    priya = {"id": str(a["people"]["priya"].id), "name": "Priya Raman", "email": None, "active": True}
    assert driven["owners"]["renewal_owner"] == dana  # the contract's renewal owner became the renewal's responsible
    assert driven["owners"]["contract_owner"] == dana and driven["vendor_name"] == "Acme"
    assert driven["vendor"] == {"id": str(owned["acme"].id), "name": "Acme", "account_manager": priya,
                                "support_email": None}  # fmt: skip
    # The driving contract's lines, by name, with their owners; gathered per role in owners.
    assert [r["name"] for r in driven["records"]] == ["CRM", "Ledger"] and driven["records_total"] == 2
    crm = driven["records"][0]
    assert crm["business_owner"] == dana and crm["technical_owner"] == priya
    assert crm["support_team"] == {"id": str(a["team"].id), "name": "IT Applications"}
    assert crm["path"] == f"applications/{owned['crm'].id}" and driven["records"][1]["kind"] == "component"
    assert driven["owners"]["business_owners"] == [dana] and driven["owners"]["technical_owners"] == [priya]
    # Ledger has no owners or team, and the renewal no follow-up: said, never just null.
    assert driven["owners"]["follow_up"] is None
    assert driven["gaps"] == ["no_follow_up", "no_business_owner", "no_support_team"]

    domain = rows["Domain"]
    editor = a["users"]["editor"]
    assert domain["owners"]["follow_up"] == {"id": editor.id, "name": editor.email, "email": editor.email}
    assert [r["id"] for r in domain["records"]] == [str(a["app"].id)]
    assert domain["owners"]["it_stewards"] == [domain["owners"]["follow_up"]]  # the fixture app's steward
    assert domain["vendor"] is None and domain["owners"]["renewal_owner"] is None and domain["contract"] is None
    assert domain["owners"]["contract_owner"] is None and domain["vendor_name"] is None
    assert domain["gaps"] == ["no_renewal_owner", "no_vendor", "no_business_owner", "no_support_team"]

    alone = rows["Alone"]
    assert alone["source"] == "contract" and alone["path"] == f"contracts/{owned['alone'].id}"
    assert alone["owners"]["follow_up"] is None and alone["records"] == []
    assert alone["owners"]["contract_owner"] is None and alone["vendor"]["name"] == "Bare vendor"
    assert alone["gaps"] == ["no_renewal_owner", "no_vendor_contact", "no_records"]  # no follow-up gap on contracts


def test_a_contract_linked_later_brings_its_owner(a, api):
    """A renewal made on its own, with nobody responsible, then linked by a contract that has a renewal owner: the
    renewal's own owner stays null (no fallback), the contract's is contract_owner, and the renewal has an owner."""
    org, editor, priya = a["org"], a["users"]["editor"], a["people"]["priya"]
    obligation = renewals.create_renewal(org, editor, {"name": "Payroll", "renewal_date": iso(a, 80),
                                                       "followup": editor.id})  # fmt: skip
    unstewarded = record(a, "Payroll app", it_steward=None)
    linked = contract(a, "Payroll 2026", end_date=iso(a, 80), renewal_owner=str(priya.id),
                      renewal_obligation=str(obligation.id))  # fmt: skip
    line(a, linked, unstewarded)
    client = api(a["users"]["viewer"])

    def row():
        return next(r for r in client.get("composite/renewals").json_body["results"] if r["name"] == "Payroll")

    found = row()
    assert found["source"] == "renewal" and found["contract"]["id"] == str(linked.id)
    assert found["owners"]["renewal_owner"] is None
    assert found["owners"]["contract_owner"]["id"] == str(priya.id)
    assert "no_renewal_owner" not in found["gaps"] and "renewal_owner_inactive" not in found["gaps"]
    assert "no_it_steward" in found["gaps"] and found["records"][0]["it_steward"] is None
    # Every owner that is set has left: said as renewal_owner_inactive, not as no owner.
    type(priya).objects.filter(id=priya.id).update(active=False)
    assert "renewal_owner_inactive" in row()["gaps"] and "no_renewal_owner" not in row()["gaps"]


def test_a_row_lists_twenty_records_and_counts_them_all(a, api, owned):
    for i in range(22 - 2):
        line(a, owned["driven"], record(a, f"Extra {i:02d}", business_owner=a["people"]["priya"]))
    row = next(r for r in api(a["users"]["viewer"]).get("composite/renewals").json_body["results"]
               if r["name"] == "Driven")  # fmt: skip
    assert len(row["records"]) == 20 and row["records_total"] == 22
    # Owners are gathered from every record, not only the twenty listed.
    assert {p["name"] for p in row["owners"]["business_owners"]} == {"Dana Ortiz", "Priya Raman"}


def test_renewals_window_limit_and_overdue(a, api, owned):
    client = api(a["users"]["viewer"])
    renewals.create_renewal(a["org"], a["users"]["editor"], {"name": "Late", "explicit_deadline": iso(a, -3)})
    names = [r["name"] for r in client.get("composite/renewals?within_days=15").json_body["results"]]
    assert names == ["Late", "Domain"]  # overdue ones are always in
    assert client.get("composite/renewals?within_days=15").json_body["results"][0]["overdue"] is True
    capped = client.get("composite/renewals?within_days=730&limit=2").json_body
    assert capped["count"] == 5 and capped["truncated"] and len(capped["results"]) == 2
    assert client.get("composite/renewals").json_body["within_days"] == 90


def test_renewals_by_term_end_keep_a_deadline_entered_after_the_renewal_date(a, api, owned):
    """basis=term_end (the MCP's "what renews in the next N days"): `within_days` filters term_ends, so a
    renewal whose entered deadline falls after its renewal date (renewal validation allows it) is still in, and one
    with no renewal date is left out; soonest term end first."""
    client = api(a["users"]["viewer"])
    editor = a["users"]["editor"]
    renewals.create_renewal(a["org"], editor, {"name": "Entered late", "renewal_date": iso(a, 30),
                                               "explicit_deadline": iso(a, 200), "auto_renew": "yes"})  # fmt: skip
    renewals.create_renewal(a["org"], editor, {"name": "No date", "explicit_deadline": iso(a, 5)})
    by_deadline = client.get("composite/renewals?within_days=90").json_body
    assert by_deadline["basis"] == "action_deadline"
    assert "Entered late" not in {r["name"] for r in by_deadline["results"]}  # its deadline is 200 days out
    assert "No date" in {r["name"] for r in by_deadline["results"]}
    by_term = client.get("composite/renewals?within_days=90&basis=term_end").json_body
    names = [r["name"] for r in by_term["results"]]
    assert by_term["basis"] == "term_end" and "Entered late" in names and "No date" not in names
    assert all(r["term_ends"] and r["term_ends"] <= iso(a, 90) for r in by_term["results"])
    assert [r["term_ends"] for r in by_term["results"]] == sorted(r["term_ends"] for r in by_term["results"])
    late = next(r for r in by_term["results"] if r["name"] == "Entered late")
    assert late["date"] == iso(a, 200) and late["term_ends"] == iso(a, 30) and late["owners"] is not None
    assert by_term["count"] == len(names) and not by_term["truncated"]
    refused = client.get("composite/renewals?basis=renewal_date")
    assert refused.status_code == 400 and "basis" in refused.json_body["errors"]


# ---- the same numbers as the dashboard and the reports -----------------------------------------------------------------


def test_deadlines_are_the_dashboards(a, api, owned):
    client = api(a["users"]["viewer"])
    dashboard = client.get("dashboard").json_body["deadlines"]
    found = client.get("composite/deadlines?within_days=90").json_body
    assert found["windows"] == dashboard["windows"] and found["overdue"] == dashboard["overdue"]
    assert found["count"] == dashboard["total"]
    assert [(r["id"], r["date"]) for r in found["results"][:10]] == [(r["id"], r["date"]) for r in dashboard["rows"]]
    assert found["results"][0]["follow_up"]["id"] == a["users"]["editor"].id
    renewals_ = client.get("composite/renewals?within_days=90").json_body["results"]
    assert [(r["id"], r["date"]) for r in renewals_] == [(r["id"], r["date"]) for r in found["results"]]
    calendar = client.get("reports/renewals-calendar?months=24").json_body["rows"]
    everything = client.get("composite/deadlines?within_days=730&limit=200").json_body["results"]
    assert {r["id"] for r in calendar} <= {r["id"] for r in everything}


def test_spend_is_the_reports_and_the_dashboards(a, api, owned):
    client = api(a["users"]["viewer"])
    for i in range(10):  # more vendors than the dashboard shows, so groups fold
        vendor = Vendor.objects.create(organization=a["org"], name=f"Vendor {i:02d}")
        contract(a, f"Small {i}", vendor=str(vendor.id), amount=f"{10 + i}.00")
    report = client.get("reports/spend?by=vendor").json_body
    dashboard = client.get("dashboard").json_body["spend"]
    found = client.get("composite/spend?by=vendor&groups=8").json_body
    assert found["totals"] == report["totals"] == dashboard["totals"]
    strip = [{k: v for k, v in r.items() if k not in ("groups", "folded")} for r in dashboard["by_vendor"]]
    assert [{k: v for k, v in r.items() if k != "folded"} for r in found["groups"]] == strip
    assert found["truncated"] and next(r for r in found["groups"] if r["gap"] == "other")["folded"] == 3
    everything = client.get("composite/spend?by=vendor&groups=200").json_body
    assert not everything["truncated"]
    assert [{k: v for k, v in r.items() if k != "folded"} for r in everything["groups"]] == report["rows"]
    for by in ("department", "category", "cost_center"):
        assert client.get(f"composite/spend?by={by}&groups=200").json_body["groups"] == [
            {**r, "folded": None} for r in client.get(f"reports/spend?by={by}").json_body["rows"]
        ]


def test_data_gaps_are_the_data_quality_pages(a, api, owned):
    client = api(a["users"]["viewer"])
    summary = client.get("data-quality/summary").json_body
    found = client.get("composite/data-gaps").json_body
    for key in ("score", "records", "complete"):
        assert found[key] == summary[key], key
    assert [(r["id"], r["failing"], r["excepted"]) for r in found["by_rule"]] == [
        (r["id"], r["failing"], r["excepted"]) for r in summary["by_rule"]
    ]
    failures = client.get("data-quality/failures?page_size=200").json_body["results"]
    assert found["count"] == len(failures) and {r["id"] for r in found["results"]} == {r["id"] for r in failures}
    by_id = {r["id"]: r for r in failures}
    for row in found["results"]:
        assert [x["id"] for x in row["failing"]] == [x["id"] for x in by_id[row["id"]]["quality"]["failing"]]
    only = client.get("composite/data-gaps?rule=business_owner&kind=component").json_body
    assert only["rule"] == "business_owner" and only["kind"] == "component"
    assert [r["name"] for r in only["results"]] == ["Ledger"] and only["results"][0]["business_owner"] is None
    crm = next(r for r in found["results"] if r["name"] == "CRM")  # fails documentation and criticality
    assert crm["business_owner"]["name"] == "Dana Ortiz" and crm["it_steward"]["id"] == a["users"]["editor"].id
    capped = client.get("composite/data-gaps?limit=1").json_body
    assert capped["count"] == found["count"] and len(capped["results"]) == 1 and capped["truncated"]


def test_expiring_credentials_are_the_dashboards(a, b, api, owned):
    connector = make_connector(a["org"], a["users"]["admin"])
    discovery.run(connector)
    ledger_sso = Observation.objects.get(connector=connector, display_name="Finance Ledger SSO")
    discovery.link(a["org"], a["users"]["editor"], ledger_sso, {"resource": str(owned["crm"].id)})
    make_connector(b["org"], b["users"]["admin"])
    discovery.run(Connector.objects.get(organization=b["org"]))
    owner = Membership.objects.get(organization=a["org"], user=a["users"]["editor"])
    tokens.create_token(a["org"], a["users"]["admin"], owner, "Soon", True, today(a) + timedelta(days=5))
    for role in ("admin", "viewer"):
        client = api(a["users"][role])
        dashboard = client.get("dashboard").json_body["expiring_credentials"]["rows"]
        found = client.get("composite/expiring-credentials").json_body
        assert [r["id"] for r in found["results"]] == [r["id"] for r in dashboard], role
        assert found["count"] == len(dashboard) and found["within_days"] == 30
    admin = api(a["users"]["admin"])
    rows = admin.get("composite/expiring-credentials").json_body["results"]
    token = next(r for r in rows if r["kind"] == "api_token")
    assert token["owner"]["id"] == a["users"]["editor"].id and token["prefix"].startswith("sw_")
    linked = next(r for r in rows if r["application"] and r["application"]["name"] == "Finance Ledger SSO")
    assert linked["record"]["name"] == "CRM" and linked["record"]["business_owner"]["name"] == "Dana Ortiz"
    only = admin.get("composite/expiring-credentials?kind=api_token").json_body
    assert {r["kind"] for r in only["results"]} == {"api_token"}
    # Viewers see their own tokens only, as on the dashboard.
    assert not [r for r in api(a["users"]["viewer"]).get("composite/expiring-credentials").json_body["results"]
                if r["kind"] == "api_token"]  # fmt: skip
    text = json.dumps(rows)
    assert "token_hash" not in text and "secret_ciphertext" not in text


def test_discovery_summary_is_the_candidate_queues(a, b, api):
    connector = make_connector(
        a["org"], a["users"]["admin"], name="Entra (synthetic)", reviewer=a["users"]["editor"].id
    )
    discovery.run(connector)
    zoom = Observation.objects.get(connector=connector, display_name="Zoom")
    discovery.link(a["org"], a["users"]["editor"], zoom, {"resource": str(a["app"].id)})
    make_connector(b["org"], b["users"]["admin"])
    discovery.run(Connector.objects.get(organization=b["org"]))
    client = api(a["users"]["viewer"])
    queue = client.get("discovery/candidates").json_body["counts"]
    found = client.get("composite/discovery-summary").json_body
    assert found["counts"] == queue and found["counts"]["linked"] == 1
    assert found["connectors_total"] == 1 and not found["truncated"]
    row = found["connectors"][0]
    assert row["id"] == str(connector.id) and row["counts"] == queue and row["source"] == "entra"
    assert row["reviewer"]["id"] == a["users"]["editor"].id and row["path"] == f"connectors/{connector.id}"
    assert row["last_run"]["status"] == "succeeded" and row["active_run"] is None
    text = json.dumps(found)
    assert "secret" not in text.lower() and "tenant_id" not in text


def test_search_is_the_search_endpoints(a, api, owned):
    client = api(a["users"]["viewer"])
    grouped = client.get("search?q=crm").json_body
    found = client.get("composite/search?q=crm").json_body
    assert found["count"] == sum(g["count"] for g in grouped["groups"])
    assert [r["id"] for r in found["results"]] == [h["id"] for g in grouped["groups"] for h in g["results"]]
    assert found["by_kind"] == {g["kind"]: g["count"] for g in grouped["groups"]}
    hit = next(r for r in found["results"] if r["name"] == "CRM")
    assert hit["path"] == f"applications/{owned['crm'].id}" and hit["kind"] == "application"
    record(a, "CRM archive")
    capped = client.get("composite/search?q=crm&limit=1&kinds=application").json_body
    assert capped["truncated"] and len(capped["results"]) == 1


def test_dependencies_are_the_explorers(a, api, owned):
    client = api(a["users"]["viewer"])
    crm = owned["crm"]
    found = client.get(f"composite/dependencies?target={crm.id}").json_body
    assert found["direction"] == "both" and found["depth"] == "2"
    assert [n["name"] for n in found["depends_on"]] == ["Ledger"]
    assert [n["name"] for n in found["dependents"]] == ["orga App"]
    assert found["depends_on"][0]["via"] == "Depends on" and found["depends_on"][0]["hops"] == 1
    assert found["target"]["technical_owner"]["name"] == "Priya Raman"
    explorer = client.get(f"dependencies?target={crm.id}&direction=both&depth=2").json_body
    assert {n["id"] for n in explorer["nodes"]} == {n["id"] for n in found["depends_on"] + found["dependents"]}
    assert found["count"] == explorer["total"]
    two = client.get(f"composite/dependencies?target={a['app'].id}&direction=depends_on").json_body
    assert two["dependents"] is None and [(n["name"], n["hops"], n["through"]) for n in two["depends_on"]] == [
        ("CRM", 1, []),
        ("Ledger", 2, ["CRM"]),
    ]
    one = client.get(f"composite/dependencies?target={a['app'].id}&direction=depends_on&limit=1").json_body
    assert len(one["depends_on"]) == 1 and one["truncated"] and one["count"] == 2
    integration = services.create_integration(a["org"], a["users"]["editor"], {
        "name": "Feed", "lifecycle": "active", "source": str(crm.id), "target": str(owned["ledger"].id)})  # fmt: skip
    assert [f["name"] for f in client.get(f"composite/dependencies?target={crm.id}").json_body["data_flows"]] == [
        "Feed"
    ]
    refused = client.get(f"composite/dependencies?target={integration.id}")
    assert refused.status_code == 400 and "target" in refused.json_body["errors"]


# ---- tenancy and capabilities ----------------------------------------------------------------------------------------


def test_another_organizations_data_never_appears(a, b, api, owned):
    org_b, editor_b = b["org"], b["users"]["editor"]
    theirs = Vendor.objects.create(organization=org_b, name="Their vendor")
    other = contracts.create_contract(org_b, editor_b, {"name": "Their contract", "vendor": str(theirs.id),
                                                        "amount": "5.00", "currency": "GBP", "end_date": iso(a, 50),
                                                        "create_renewal_obligation": True})  # fmt: skip
    client = api(a["users"]["viewer"])
    for path, _ in PATHS.values():
        text = json.dumps(body(client.get(path)))
        for name in ("Their contract", "Their vendor", "orgb App", "GBP"):
            assert name not in text, (path, name)
    assert client.get(f"composite/dependencies?target={b['app'].id}").status_code == 404
    assert "Their contract" not in json.dumps(client.get("composite/search?q=their").json_body)
    # And the other way round: their token reads only theirs.
    theirs_token = read_only_token(b)
    rows = body(theirs_token.get("composite/renewals"))["results"]
    assert [r["name"] for r in rows] == ["Their contract"] and rows[0]["contract"]["id"] == str(other.id)


def test_capabilities_gate_the_composites(a, api, owned, monkeypatch):
    client = api(a["users"]["viewer"])
    real = caps.capabilities

    def without(*names):
        monkeypatch.setattr(caps, "capabilities", lambda org: real(org) - set(names))

    without(caps.CONTRACTS)
    off = client.get("composite/spend")
    assert off.status_code == 403 and off.json_body["capability"] == "contracts"
    assert all(r["source"] == "renewal" for r in client.get("composite/renewals").json_body["results"])
    without(caps.DATA_QUALITY)
    assert client.get("composite/data-gaps").json_body["capability"] == "data_quality"
    without(caps.RENEWALS)  # contracts on, renewals off: only contracts' own deadlines, from the same rows
    for path in ("composite/deadlines", "composite/renewals"):
        rows = client.get(path).json_body["results"]
        assert rows and all(r["source"] == "contract" for r in rows), path
    without(caps.RENEWALS, caps.CONTRACTS)
    for path in ("composite/deadlines", "composite/renewals"):
        refused = client.get(path)
        assert refused.status_code == 403 and refused.json_body["capability"] == "renewals", path
    without(caps.AI_AGENTS)
    agent = record(a, "Helper", kind="ai_agent")
    refused = client.get(f"composite/dependencies?target={agent.id}")
    assert refused.status_code == 403 and refused.json_body["capability"] == "ai_agents"
    assert client.get("composite/data-gaps?kind=ai_agent").json_body["capability"] == "ai_agents"
    assert client.get("composite/search?q=x&kinds=ai_agent").json_body["capability"] == "ai_agents"


def test_records_of_a_kind_the_edition_lacks_are_left_off_renewals(a, api, owned, monkeypatch):
    agent = record(a, "Contract bot", kind="ai_agent")
    line(a, owned["driven"], agent)
    client = api(a["users"]["viewer"])
    names = lambda: next(r for r in client.get("composite/renewals").json_body["results"] if r["name"] == "Driven")  # noqa: E731
    assert "Contract bot" in [r["name"] for r in names()["records"]]
    real = caps.capabilities
    monkeypatch.setattr(caps, "capabilities", lambda org: real(org) - {caps.AI_AGENTS})
    assert "Contract bot" not in [r["name"] for r in names()["records"]]


# ---- query counts ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "composite/renewals?within_days=730&limit=200",
        "composite/deadlines?within_days=730&limit=200",
        "composite/spend?by=vendor",
        "composite/data-gaps?limit=200",
        "composite/expiring-credentials?limit=200",
        "composite/discovery-summary",
        "composite/search?q=app",
        "DEPENDENCIES",
    ],
)
def test_query_counts_do_not_grow_with_rows(a, api, path):
    client = api(a["users"]["admin"])
    org, people = a["org"], a["people"]
    hub = record(a, "Hub app")

    def add(n):
        for i in range(n):
            k = Resource.objects.count()
            team = Team.objects.create(organization=org, name=f"Team {k}")
            vendor = Vendor.objects.create(organization=org, name=f"Vendor {k}", account_manager=people["priya"])
            app = record(
                a, f"App {k}", business_owner=people["dana"], support_team=team, it_steward=a["users"]["admin"]
            )
            c = contract(a, f"C {k}", vendor=str(vendor.id), amount="10.00", end_date=iso(a, 20 + i),
                         renewal_owner=str(people["dana"].id), create_renewal_obligation=bool(i % 2))  # fmt: skip
            line(a, c, app)
            line(a, c, a["app"], "10")
            renewals.create_renewal(org, a["users"]["editor"], {"name": f"R {k}", "renewal_date": iso(a, 30 + i),
                                                                "applications": [str(app.id)],
                                                                "followup": a["users"]["editor"].id})  # fmt: skip
            depends(a, app, hub)
            owner = Membership.objects.get(organization=org, user=a["users"]["editor"])
            tokens.create_token(org, a["users"]["admin"], owner, f"T{k}", True, today(a) + timedelta(days=5 + i))
            connector = make_connector(org, a["users"]["admin"], name=f"Entra {k}")
            discovery.run(connector)

    url = f"composite/dependencies?target={hub.id}&depth=all" if path == "DEPENDENCIES" else path

    def count():
        with CaptureQueriesContext(connection) as ctx:
            assert client.get(url).status_code == 200
        return len(ctx.captured_queries)

    add(2)  # every kind of row exists from the start (a driven renewal and one of its own), so only counts grow
    count()  # the first request also saves the new session
    first = count()
    add(4)
    assert count() == first, path
    assert first <= 40, (path, first)


# ---- OpenAPI ---------------------------------------------------------------------------------------------------------


def test_the_composites_are_described_in_openapi(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    names = ("search", "deadlines", "renewals", "spend", "dependencies", "discovery-summary", "data-gaps",
             "expiring-credentials")  # fmt: skip
    for name in names:
        operation = doc["paths"][f"/api/v1/composite/{name}"]["get"]
        assert {"200", "400", "403"} <= set(operation["responses"]), name
        content = operation["responses"]["200"]["content"]["application/json"]
        assert content["schema"]["$ref"].startswith("#/components/schemas/Composite"), name
        assert content.get("examples"), name
        assert set(doc["paths"][f"/api/v1/composite/{name}"]) == {"get"}, name
    renewals_params = {p["name"] for p in doc["paths"]["/api/v1/composite/renewals"]["get"]["parameters"]}
    assert renewals_params == {"within_days", "limit", "basis"}
    assert "404" in doc["paths"]["/api/v1/composite/dependencies"]["get"]["responses"]


def test_the_search_kind_enum_is_the_search_registry():
    from api.composite_serializers import SEARCH_KINDS
    from core import search

    assert SEARCH_KINDS == search.all_kinds()


def test_the_composite_enums_are_their_sources():
    """Each choice set comes from the code that produces its values."""
    from api import composite_serializers as cs
    from catalog import serializers as catalog_serializers
    from catalog.models import Kind
    from money import reporting

    assert cs.LINK_STATES is catalog_serializers.LINK_STATES
    assert cs.CREDENTIAL_KINDS is reporting.CREDENTIAL_KINDS
    from api import composite_views

    assert not hasattr(composite_views, "CREDENTIAL_KINDS")  # one list, in money/reporting
    assert set(cs.LINK_RECORD_KINDS) == set(Kind.values)
    assert cs.DEADLINE_ACTIONS == list(reporting.NEUTRAL_LABELS)
    assert set(cs.SEARCH_PARENT_KINDS) == set(Kind.values) | {"vendor", "contract"}


def test_the_enums_cover_what_the_builders_return(a, api):
    """Values seen in real responses are in their enums: attachment parents, deadline actions, credential kinds."""
    from datetime import timedelta

    from api import composite_serializers as cs
    from catalog import attachments
    from catalog.models import Vendor
    from core.capabilities import set_edition
    from money import contracts, renewals

    a["org"], _ = set_edition(a["org"], "enterprise")
    org, editor = a["org"], a["users"]["editor"]
    vendor = Vendor.objects.create(organization=org, name="Parent vendor")
    contract = contracts.create_contract(org, editor, {"name": "Parent contract", "end_date": "2027-03-31"})
    for parent in (a["app"], vendor, contract):
        attachments.add_link(org, editor, parent, {"url": "https://example.test/x", "title": "Zebracorn notes"})
    soon = (renewals.org_today(org) + timedelta(days=20)).isoformat()
    renewals.create_renewal(org, editor, {"name": "Cancel me", "auto_renew": "yes", "renewal_date": soon})
    renewals.create_renewal(org, editor, {"name": "Renew me", "auto_renew": "no", "renewal_date": soon})
    client = api(a["users"]["admin"])
    found = client.get("composite/search?q=zebracorn&kinds=attachment").json_body["results"]
    assert {r["parent"]["kind"] for r in found} == {"application", "vendor", "contract"}
    assert {r["parent"]["kind"] for r in found} <= set(cs.SEARCH_PARENT_KINDS)
    actions = {r["action"] for r in client.get("composite/deadlines").json_body["results"]}
    assert actions == set(cs.DEADLINE_ACTIONS)
