"""Global search (core/search.py, catalog/search.py, GET /api/v1/search).

Matching across kinds (a vendor's name finds its applications, an AI provider's its agents), every word anywhere, the
last word as a prefix, typo matches when nothing else does, snippets as text segments, archived records, tenancy,
capabilities, anonymized people, custom fields (and what deleting or clearing one does), freshness after writes, query
counts, roles, validation, the rate limit and the GIN indexes the matching relies on."""

import json

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from catalog import admin_services, attachments, customfields, services
from catalog.models import Person, Resource, Vendor
from core import capabilities as caps
from core import search
from tests.test_ai_agents import model, provider
from tests.test_attachments import PDF, upload
from tests.test_drf_resources import described
from tests.test_tokens import Bearer, mint  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def fresh_throttle():
    cache.clear()  # the search throttle counts in the process cache
    yield
    cache.clear()


def find(client, q, **params):
    query = "&".join(f"{k}={v}" for k, v in {"q": q, **params}.items())
    response = client.get(f"search?{query}")
    assert response.status_code == 200, response.json_body
    return response.json_body


def names(body, kind):
    return [r["name"] for g in body["groups"] if g["kind"] == kind for r in g["results"]]


def group(body, kind):
    return next(g for g in body["groups"] if g["kind"] == kind)


def vendor(f, name, **data):
    return admin_services.create_named(Vendor, f["org"], f["users"]["editor"], {"name": name, **data})


def record(f, name, kind="application", **data):
    return services.create_resource(
        f["org"], f["users"]["editor"], {"name": name, "kind": kind, "lifecycle": "active", **data}
    )


@pytest.fixture
def catalog(a):
    """Contoso supplies two applications and provides the models one agent uses; other records don't involve it."""
    contoso = vendor(a, "Contoso Software")
    other = vendor(a, "Fabrikam Labs")
    payroll = record(a, "Payroll", vendor=str(contoso.id), description="Pays everyone on the last working day.")
    ledger = record(a, "Ledger", vendor=str(contoso.id))
    record(a, "Timesheets", vendor=str(other.id))
    prov = provider(a, "Contoso")
    large = model(a, prov, "Contoso Large", version="2")
    helper = record(a, "HR helper", kind="ai_agent", models_used=[str(large.id)])
    job = record(a, "Nightly export", kind="component", purpose="Copies payroll totals to the ledger.")
    return {"contoso": contoso, "payroll": payroll, "ledger": ledger, "helper": helper, "job": job, "model": large}


# ---- matching ----------------------------------------------------------------------------------------------------


def test_a_vendor_name_finds_its_applications_its_agents_and_itself(a, api, catalog):
    body = find(api(a["users"]["viewer"]), "contoso")
    assert body["fuzzy"] is False and body["query"] == "contoso"
    assert [g["kind"] for g in body["groups"]] == [
        "application",
        "component",
        "integration",
        "ai_agent",
        "vendor",
        "person",
        "attachment",
        "contract",  # (money/search.py)
    ]
    assert sorted(names(body, "application")) == ["Ledger", "Payroll"]
    assert names(body, "ai_agent") == ["HR helper"]  # through its model's provider (and the model's name)
    assert names(body, "vendor") == ["Contoso Software"]
    assert group(body, "application")["count"] == 2 and group(body, "component")["count"] == 0
    hit = group(body, "application")["results"][0]
    assert hit["snippet"]["field"] == "vendor" and hit["snippet"]["label"] == "Vendor"
    assert {"text": "Contoso", "match": True} in hit["snippet"]["segments"]
    assert hit["path"] == f"applications/{hit['id']}" and hit["lifecycle"] == "active" and hit["archived"] is False
    agent = group(body, "ai_agent")["results"][0]
    assert agent["path"] == f"ai-agents/{catalog['helper'].id}" and agent["snippet"]["field"] == "ai_models"
    assert agent["snippet"]["label"] == "AI models"


def test_every_word_must_match_but_each_may_match_a_different_field(a, api, catalog):
    client = api(a["users"]["viewer"])
    assert names(find(client, "contoso payroll"), "application") == ["Payroll"]  # vendor + name
    assert names(find(client, "contoso%20ledger"), "application") == ["Ledger"]
    # Timesheets is Fabrikam's: no exact match, so only names similar to the whole query come back.
    assert find(client, "contoso timesheets")["fuzzy"] is True
    body = find(client, "payroll ledger")  # both words in the component's purpose
    assert names(body, "component") == ["Nightly export"]
    assert group(body, "component")["results"][0]["snippet"]["field"] == "purpose"


def test_the_last_word_is_a_prefix_and_names_rank_first(a, api, catalog):
    client = api(a["users"]["viewer"])
    assert names(find(client, "payr"), "application") == ["Payroll"]
    assert names(find(client, "nightly ex"), "component") == ["Nightly export"]
    assert names(find(client, "pay"), "application") == ["Payroll"]
    assert names(find(client, "led"), "application") == ["Ledger"]
    assert names(find(client, "ledger"), "component") == ["Nightly export"]  # a whole word in the purpose
    assert names(find(client, "ledg everyone"), "application") == []  # earlier words are whole words
    # "Payroll" is the name of one record and in the purpose of another: the name match scores higher.
    record(a, "Payroll archive", kind="component")
    ranked = group(find(client, "payroll"), "component")["results"]
    assert [r["name"] for r in ranked] == ["Payroll archive", "Nightly export"]
    assert ranked[0]["score"] > ranked[1]["score"]


def test_description_tags_other_text_and_integrations_match(a, api, catalog):
    client = api(a["users"]["viewer"])
    services.update_resource(a["org"], a["users"]["editor"], catalog["ledger"], {"tags": ["finance-core"]})
    body = find(client, "finance-core")
    assert names(body, "application") == ["Ledger"]
    assert group(body, "application")["results"][0]["snippet"]["field"] == "tags"
    hit = group(find(client, "everyone"), "application")["results"][0]
    assert hit["name"] == "Payroll" and hit["snippet"]["field"] == "description"
    services.update_resource(a["org"], a["users"]["editor"], catalog["job"], {"host_runtime": "Kubernetes CronJob"})
    hit = group(find(client, "kubernetes"), "component")["results"][0]
    assert hit["name"] == "Nightly export" and hit["snippet"]["field"] == "details"
    feed = services.create_integration(
        a["org"],
        a["users"]["editor"],
        {
            "name": "Payroll feed",
            "lifecycle": "active",
            "source": str(catalog["payroll"].id),
            "target": str(catalog["job"].id),
            "data_purpose": "Salary totals for reconciliation",
        },
    )
    body = find(client, "reconciliation")
    assert names(body, "integration") == ["Payroll feed"]
    hit = group(body, "integration")["results"][0]
    assert hit["path"] == f"integrations/{feed.id}" and hit["snippet"]["label"] == "Data purpose"


def test_people_match_by_name_only(a, api, catalog):
    client = api(a["users"]["viewer"])
    Person.objects.filter(id=a["people"]["priya"].id).update(title="Payroll lead", email="priya@contoso.example")
    body = find(client, "priya")
    assert names(body, "person") == ["Priya Raman"]
    hit = group(body, "person")["results"][0]
    assert hit["path"] == f"people/{a['people']['priya'].id}" and hit["snippet"] is None
    assert names(find(client, "payroll"), "person") == []  # not by title
    assert names(find(client, "contoso"), "person") == []  # nor by email


def test_inactive_people_are_archived_and_anonymized_people_never_match(a, api):
    client = api(a["users"]["viewer"])
    assert names(find(client, "former"), "person") == []  # inactive: left out by default
    assert names(find(client, "former", include_archived="true"), "person") == ["Former person"]
    assert group(find(client, "former", include_archived="true"), "person")["results"][0]["archived"] is True
    admin_services.anonymize_person(a["org"], a["users"]["admin"], a["people"]["priya"], "Priya Raman")
    for q in ("priya", "raman", "former", "person"):
        found = names(find(client, q, include_archived="true"), "person")
        assert "Priya Raman" not in found and not any(n.startswith("Former person #") for n in found), q
    assert names(find(client, "prya raman", include_archived="true"), "person") == []  # nor by a typo match


def test_attachments_match_by_title_and_file_name_with_their_parent(a, api, catalog):
    editor, viewer = api(a["users"]["editor"]), api(a["users"]["viewer"])
    link = attachments.add_link(
        a["org"], a["users"]["editor"], catalog["payroll"], {"url": "https://contoso.example/sop", "title": "Run SOP"}
    )
    made = upload(editor, f"vendors/{catalog['contoso'].id}/attachments", "master-agreement.pdf", PDF, title="MSA")
    assert made.status_code == 201, made.json_body
    body = find(viewer, "sop")
    hit = group(body, "attachment")["results"][0]
    assert hit["name"] == "Run SOP" and hit["path"] == f"attachments/{link.id}" and hit["snippet"] is None
    assert hit["parent"] == {
        "kind": "application",
        "id": str(catalog["payroll"].id),
        "name": "Payroll",
        "path": f"applications/{catalog['payroll'].id}",
    }
    hit = group(find(viewer, "master"), "attachment")["results"][0]
    assert hit["name"] == "MSA" and hit["parent"]["kind"] == "vendor" and hit["parent"]["path"].startswith("vendors/")
    assert hit["snippet"]["field"] == "filename" and hit["snippet"]["label"] == "File name"
    services.archive_resource(a["org"], a["users"]["editor"], catalog["payroll"])
    assert names(find(viewer, "sop"), "attachment") == []
    archived = group(find(viewer, "sop", include_archived="true"), "attachment")["results"]
    assert [h["name"] for h in archived] == ["Run SOP"] and archived[0]["archived"] is True


def test_a_typo_finds_similar_names_when_nothing_matches(a, api, catalog):
    client = api(a["users"]["viewer"])
    body = find(client, "contso")
    assert body["fuzzy"] is True
    assert names(body, "vendor") == ["Contoso Software"]
    assert all(r["snippet"] is None for g in body["groups"] for r in g["results"])
    body = find(client, "payrol")  # a prefix still matches exactly: no fuzzy results
    assert body["fuzzy"] is False and names(body, "application") == ["Payroll"]
    assert find(client, "payrll")["fuzzy"] is True and "Payroll" in names(find(client, "payrll"), "application")
    nothing = find(client, "zzqqxx")
    assert nothing["fuzzy"] is False and all(g["count"] == 0 and g["results"] == [] for g in nothing["groups"])
    record(a, "Old payroll", lifecycle="active")
    services.archive_resource(
        a["org"], a["users"]["editor"], Resource.objects.get(organization=a["org"], name="Old payroll")
    )
    assert "Old payroll" not in names(find(client, "old payrll"), "application")  # typo matches skip archived too
    archived = find(client, "old payrll", include_archived="true")
    assert archived["fuzzy"] is True and "Old payroll" in names(archived, "application")


# ---- snippets are text -------------------------------------------------------------------------------------------


def test_snippets_are_text_segments_and_never_markup(a, api):
    hostile = 'Payroll <script>alert("x")</script> <img src=x onerror=alert(1)> marker & <b>bold</b> end'
    record(a, "Hostile <b>name</b>", description=hostile)
    body = find(api(a["users"]["viewer"]), "marker")
    hit = group(body, "application")["results"][0]
    assert hit["name"] == "Hostile <b>name</b>"  # the name is data, returned as it is; the SPA renders it as text
    segments = hit["snippet"]["segments"]
    assert all(set(s) == {"text", "match"} and isinstance(s["text"], str) for s in segments)
    text = "".join(s["text"] for s in segments)
    assert "" not in text and "" not in text  # our markers can't be forged from the record's text
    # ts_headline drops what its parser reads as a tag (<script>, <b>); the rest, markup-like or not, stays as inert text
    # in a segment, which the SPA renders as text. Nothing is ever turned into markup.
    assert "<script>" not in text and "<b>" not in text
    assert "<img src=x onerror=alert(1)>" in text
    assert [s["text"] for s in segments if s["match"]] == ["marker"]
    assert search.segments("a b c") == [
        {"text": "a ", "match": False},
        {"text": "b", "match": True},
        {"text": " c", "match": False},
    ]
    assert search.segments("") == [] and search.segments(None) == []


# ---- archived, tenancy, capabilities -------------------------------------------------------------------------------


def test_archived_records_are_left_out_unless_asked_for(a, api, catalog):
    client = api(a["users"]["viewer"])
    services.archive_resource(a["org"], a["users"]["editor"], catalog["ledger"])
    assert names(find(client, "contoso"), "application") == ["Payroll"]
    body = find(client, "contoso", include_archived="true")
    assert sorted(names(body, "application")) == ["Ledger", "Payroll"]
    ledger = next(r for r in group(body, "application")["results"] if r["name"] == "Ledger")
    assert ledger["archived"] is True


def test_another_organizations_records_never_appear(a, b, api, catalog):
    vendor(b, "Contoso Software")
    record(b, "Payroll", description="Pays everyone")
    record(b, "Contoso tool", kind="ai_agent")
    Person.objects.create(organization=b["org"], name="Contoso Person")
    services.create_resource(
        b["org"], b["users"]["editor"], {"name": "Secret", "kind": "application", "lifecycle": "active"}
    )
    for client, own in ((api(a["users"]["viewer"]), a), (api(b["users"]["viewer"]), b)):
        for q in ("contoso", "payroll", "everyone", "secret", "contso"):
            body = find(client, q, include_archived="true")
            ids = {r["id"] for g in body["groups"] for r in g["results"]}
            mine = {str(i) for i in Resource.objects.filter(organization=own["org"]).values_list("id", flat=True)}
            mine |= {str(i) for i in Vendor.objects.filter(organization=own["org"]).values_list("id", flat=True)}
            mine |= {str(i) for i in Person.objects.filter(organization=own["org"]).values_list("id", flat=True)}
            assert ids <= mine, (own["org"].name, q)
    assert names(find(api(a["users"]["viewer"]), "secret"), "application") == []


def test_join_paths_never_cross_organizations(a, b, api):
    """Every path a record matches through (tag, AI provider, AI model, custom value) and attachments, in b only, all
    sharing one word that appears in none of b's record names; a has a custom field of the same name."""
    word = "quokka"
    editor = b["users"]["editor"]
    motto = field(b, "motto", "text", applies_to=("application", "vendor"))
    field(a, "motto", "text", applies_to=("application", "vendor"))
    tagged = record(b, "Alpha", tags=[word])
    prov = provider(b, "Quokka Labs")
    agent = record(b, "Beta", kind="ai_agent", models_used=[str(model(b, prov, "Quokka One").id)])
    valued = record(b, "Gamma", custom_field_data={motto.name: f"Run like a {word}"})
    vendor(b, "Delta", custom_field_data={motto.name: word})
    attachments.add_link(b["org"], editor, tagged, {"url": "https://b.example/n", "title": "Quokka notes"})
    seen = find(api(b["users"]["viewer"]), word, include_archived="true")
    assert sorted(names(seen, "application")) == ["Alpha", "Gamma"]
    assert names(seen, "ai_agent") == ["Beta"] and names(seen, "vendor") == ["Delta"]
    assert names(seen, "attachment") == ["Quokka notes"]
    assert agent.id and valued.id
    for q in (word, "quokk", "quokka labs", "quokka one"):
        body = find(api(a["users"]["viewer"]), q, include_archived="true")
        assert [(g["kind"], g["count"], g["results"]) for g in body["groups"] if g["count"] or g["results"]] == [], q


def test_q_is_nfc_normalised_and_unsearchable_words_are_reported(a, api, catalog):
    from urllib.parse import quote

    client = api(a["users"]["viewer"])
    record(a, "Café Ops")
    response = client.get("search?q=" + quote("café ops"))  # e + combining acute accent (NFD)
    assert response.status_code == 200
    assert response.json_body["query"] == "café ops" and names(response.json_body, "application") == ["Café Ops"]
    assert response.json_body["ignored"] == []
    body = find(client, quote("payroll ²"))
    assert names(body, "application") == ["Payroll"] and body["ignored"] == ["²"]
    body = find(client, quote("² payr"))
    assert names(body, "application") == ["Payroll"] and body["ignored"] == ["²"]
    response = client.get("search?q=" + quote("² ³"))
    assert response.status_code == 400 and "q" in response.json_body["errors"]


def test_kinds_and_capabilities(a, api, catalog, monkeypatch):
    client = api(a["users"]["viewer"])
    body = find(client, "contoso", kinds="vendor,application")
    assert [g["kind"] for g in body["groups"]] == ["application", "vendor"]
    monkeypatch.setitem(
        caps.EDITION_CAPABILITIES, "community", caps.COMMUNITY - {caps.AI_AGENTS, caps.ATTACHMENTS, caps.CUSTOM_FIELDS}
    )
    attachments.add_link(
        a["org"], a["users"]["editor"], catalog["helper"], {"url": "https://x.example/c", "title": "Contoso"}
    )
    body = find(client, "contoso")
    assert "ai_agent" not in [g["kind"] for g in body["groups"]]
    assert "attachment" not in [g["kind"] for g in body["groups"]]
    for kinds, capability in (("ai_agent", caps.AI_AGENTS), ("application,attachment", caps.ATTACHMENTS)):
        response = client.get(f"search?q=contoso&kinds={kinds}")
        assert response.status_code == 403, kinds
        assert response.json_body["capability"] == capability
    monkeypatch.setitem(caps.EDITION_CAPABILITIES, "community", caps.COMMUNITY - {caps.AI_AGENTS})
    # Attachments on, agents off: an agent's attachment is left out with the agent.
    assert names(find(client, "contoso", kinds="attachment"), "attachment") == []


# ---- custom fields ---------------------------------------------------------------------------------------------------


def field(f, name, type_, applies_to=("application", "vendor"), **extra):
    return customfields.create_field(
        f["org"],
        f["users"]["admin"],
        {"name": name, "label": name.replace("_", " ").title(), "type": type_, "applies_to": list(applies_to), **extra},
    )


def test_text_like_custom_fields_match_and_stop_matching_when_cleared_or_deleted(a, api, catalog):
    client = api(a["users"]["viewer"])
    case = field(a, "business_case", "longtext")
    tier = field(a, "tier", "select", choices=["Platinum", "Gold"])
    field(a, "regions", "multiselect", choices=["Nordics", "Benelux"])
    field(a, "wiki", "url")
    field(a, "seats", "integer")
    field(a, "renewal", "date")
    field(a, "settings", "object")
    editor = a["users"]["editor"]
    services.update_resource(
        a["org"],
        editor,
        catalog["payroll"],
        {
            "custom_field_data": {
                "business_case": "Replaces the spreadsheet process",
                "tier": "Platinum",
                "regions": ["Nordics"],
                "wiki": "https://wiki.example/payroll",
                "seats": 4242,
                "renewal": "2027-03-31",
                "settings": {"colour": "Ultramarine"},
            }
        },
    )
    admin_services.update_named(Vendor, a["org"], editor, catalog["contoso"], {"custom_field_data": {"tier": "Gold"}})
    body = find(client, "spreadsheet")
    hit = group(body, "application")["results"][0]
    assert hit["name"] == "Payroll"
    assert hit["snippet"]["field"] == "custom_field" and hit["snippet"]["label"] == "Business Case"
    assert names(find(client, "platinum"), "application") == ["Payroll"]
    assert names(find(client, "nordics"), "application") == ["Payroll"]
    assert names(find(client, "wiki.example"), "application") == ["Payroll"]
    assert names(find(client, "gold"), "vendor") == ["Contoso Software"]
    assert names(find(client, "spreadsheet contoso"), "application") == ["Payroll"]  # custom value + vendor
    for q in ("4242", "ultramarine", "2027"):  # integers, objects and dates are not text
        assert names(find(client, q), "application") == [], q
    # Cleared: the value no longer matches.
    services.update_resource(a["org"], editor, catalog["payroll"], {"custom_field_data": {"tier": None}})
    assert names(find(client, "platinum"), "application") == []
    # Deleted: the field's values are gone, so they no longer match.
    customfields.delete_field(a["org"], a["users"]["admin"], case)
    assert names(find(client, "spreadsheet"), "application") == []
    customfields.delete_field(a["org"], a["users"]["admin"], tier)
    assert names(find(client, "gold"), "vendor") == []


def test_custom_fields_are_not_searched_without_the_capability(a, api, catalog, monkeypatch):
    field(a, "business_case", "text")
    services.update_resource(
        a["org"], a["users"]["editor"], catalog["payroll"], {"custom_field_data": {"business_case": "Spreadsheet"}}
    )
    client = api(a["users"]["viewer"])
    assert names(find(client, "spreadsheet"), "application") == ["Payroll"]
    monkeypatch.setitem(caps.EDITION_CAPABILITIES, "community", caps.COMMUNITY - {caps.CUSTOM_FIELDS})
    assert names(find(client, "spreadsheet"), "application") == []


# ---- freshness: the index is the row ------------------------------------------------------------------------------


def test_the_index_follows_creates_updates_and_deletes(a, api, catalog):
    client, editor, admin = api(a["users"]["viewer"]), a["users"]["editor"], a["users"]["admin"]
    created = api(editor).post("applications", {"name": "Quarry", "lifecycle": "active"}).json_body
    assert names(find(client, "quarry"), "application") == ["Quarry"]
    api(editor).patch(f"applications/{created['id']}", {"name": "Granite", "purpose": "Stores quarry samples"})
    assert names(find(client, "granite"), "application") == ["Granite"]
    hit = group(find(client, "quarry"), "application")["results"][0]  # now through the purpose only
    assert hit["name"] == "Granite" and hit["snippet"]["field"] == "purpose"
    # The vendor is matched through its own row: renaming it moves its applications with it.
    admin_services.update_named(Vendor, a["org"], editor, catalog["contoso"], {"name": "Northwind Traders"})
    assert names(find(client, "contoso"), "application") == []
    assert sorted(names(find(client, "northwind"), "application")) == ["Ledger", "Payroll"]
    # Agent models: removing the model takes the agent out of the provider's results.
    services.update_resource(a["org"], editor, catalog["helper"], {"models_used": []})
    assert names(find(client, "contoso"), "ai_agent") == []
    assert api(admin).delete(f"applications/{created['id']}").status_code == 204
    assert names(find(client, "granite", include_archived="true"), "application") == []


# ---- the endpoint ----------------------------------------------------------------------------------------------------


def test_every_member_may_search_and_anyone_else_may_not(a, api, catalog, mint):  # noqa: F811
    for role in ("viewer", "editor", "admin"):
        assert names(find(api(a["users"][role]), "payroll"), "application") == ["Payroll"], role
    response = api().get("search?q=payroll")
    assert response.status_code in (401, 403)
    bearer = Bearer(mint("viewer", read_only=True)["secret"])
    response = bearer.client.get("/api/v1/search?q=payroll")
    assert response.status_code == 200 and names(response.json(), "application") == ["Payroll"]
    response = bearer.client.post("/api/v1/search?q=payroll", "{}", content_type="application/json")
    assert response.status_code in (403, 405)


def test_parameters_are_validated(a, api):
    client = api(a["users"]["viewer"])
    cases = [
        ("search", "q"),
        ("search?q=", "q"),
        ("search?q=%20%20", "q"),
        ("search?q=" + "x" * 201, "q"),
        ("search?q=---", "q"),
        ("search?q=a&limit=0", "limit"),
        ("search?q=a&limit=101", "limit"),
        ("search?q=a&limit=ten", "limit"),
        ("search?q=a&kinds=invoice", "kinds"),
        ("search?q=a&include_archived=maybe", "include_archived"),
        ("search?q=a&sort=name", "sort"),
    ]
    for url, key in cases:
        response = client.get(url)
        assert response.status_code == 400, url
        assert response["Content-Type"] == "application/problem+json"
        assert key in response.json_body["errors"], (url, response.json_body)
    assert client.get("search?q=" + "x" * 200).status_code == 200
    assert client.get("search?q=a&limit=100&kinds=").status_code == 200


def test_limit_caps_results_per_kind_and_count_holds_every_match(a, api):
    for i in range(7):
        record(a, f"Widget {i}")
    body = find(api(a["users"]["viewer"]), "widget", limit=3)
    assert group(body, "application")["count"] == 7
    assert names(body, "application") == ["Widget 0", "Widget 1", "Widget 2"]


def test_query_parsing():
    query = search.parse("  Power-BI   reports  reports ")
    assert query.terms == ("power-bi", "reports") and query.text == "Power-BI   reports  reports"
    assert query.term(0) == "'power-bi'" and query.term(1) == "'reports':*"
    assert query.term(0, "A") == "'power-bi':A" and query.term(1, "A") == "'reports':*A"
    assert query.all == "'power-bi' & 'reports':*" and query.any == "'power-bi' | 'reports':*"
    assert search.parse("it's o'brien \\x").terms == ("it", "s", "o", "brien", "x")  # quotes never reach to_tsquery
    assert len(search.parse(" ".join(f"w{i}" for i in range(20))).terms) == search.TERMS_MAX


def test_the_response_matches_its_serializer(a, api, catalog):
    from core.search_views import SearchResponseSerializer

    attachments.add_link(
        a["org"], a["users"]["editor"], catalog["payroll"], {"url": "https://x.example/c", "title": "Contoso SOP"}
    )
    for q in ("contoso", "contso"):
        body = find(api(a["users"]["viewer"]), q, include_archived="true")
        assert described(SearchResponseSerializer(), body) == [], q
        assert any(g["results"] for g in body["groups"])


def test_query_count_does_not_grow_with_results(a, api, catalog):
    client = api(a["users"]["viewer"])
    find(client, "zzz")  # warm the session

    def count(q):
        with CaptureQueriesContext(connection) as queries:
            find(client, q)
        return len(queries)

    one = count("payroll")
    for i in range(25):
        record(a, f"Payroll copy {i}", description="Payroll", tags=["payroll"])
    assert count("payroll") == one
    # One more searcher (contracts); the capability overrides; the shared counters: the API and search rate limits
    # count in the database (one upsert each).
    assert one <= 13, one


def test_searches_are_rate_limited_per_user(a, api, settings):
    settings.SEARCH_RATE = "3/min"
    client = api(a["users"]["viewer"])
    for _ in range(3):
        assert client.get("search?q=payroll").status_code == 200
    response = client.get("search?q=payroll")
    assert response.status_code == 429 and int(response["Retry-After"]) > 0
    assert api(a["users"]["editor"]).get("search?q=payroll").status_code == 200  # per user
    settings.SEARCH_RATE = ""
    cache.clear()
    for _ in range(5):
        assert client.get("search?q=payroll").status_code == 200


def test_registering_kinds_twice_is_refused():
    class Again(search.Searcher):
        kinds = ("vendor",)

    with pytest.raises(ValueError, match="vendor"):
        search.register(Again())
    assert "vendor" in search.all_kinds()


# ---- the indexes -----------------------------------------------------------------------------------------------------


def test_matching_uses_the_search_indexes(a, catalog):
    """With sequential scans off, every source's match is an index scan on migration 0031's GIN indexes, so the
    expressions in catalog/search.py still match the indexed ones."""
    checks = {
        "catalog_resource_search": "SELECT id FROM catalog_resource WHERE search_vector @@ to_tsquery('simple', 'x')",
        "catalog_application_search": "SELECT 1 FROM catalog_application WHERE search_vector @@ to_tsquery('simple', 'x')",
        "catalog_component_search": "SELECT 1 FROM catalog_component WHERE search_vector @@ to_tsquery('simple', 'x')",
        "catalog_aiagent_search": "SELECT 1 FROM catalog_aiagent WHERE search_vector @@ to_tsquery('simple', 'x')",
        "catalog_integration_search": "SELECT 1 FROM catalog_integration WHERE search_vector @@ to_tsquery('simple', 'x')",
        "catalog_vendor_search": "SELECT 1 FROM catalog_vendor WHERE search_vector @@ to_tsquery('simple', 'x')",
        "catalog_tag_search": "SELECT 1 FROM catalog_tag WHERE search_vector @@ to_tsquery('simple', 'x')",
        "catalog_aiprovider_search": "SELECT 1 FROM catalog_aiprovider WHERE search_vector @@ to_tsquery('simple', 'x')",
        "catalog_aimodel_search": "SELECT 1 FROM catalog_aimodel WHERE search_vector @@ to_tsquery('simple', 'x')",
        "catalog_person_search": "SELECT 1 FROM catalog_person WHERE search_vector @@ to_tsquery('simple', 'x')",
        "catalog_attachment_search": "SELECT 1 FROM catalog_attachment WHERE search_vector @@ to_tsquery('simple', 'x')",
        "catalog_resource_cf_search": "SELECT 1 FROM catalog_resource "
        "WHERE to_tsvector('simple'::regconfig, custom_field_data) @@ to_tsquery('simple', 'x')",
        "catalog_vendor_cf_search": "SELECT 1 FROM catalog_vendor "
        "WHERE to_tsvector('simple'::regconfig, custom_field_data) @@ to_tsquery('simple', 'x')",
        "catalog_resource_name_trgm": "SELECT 1 FROM catalog_resource WHERE 'x' <% name",
        "catalog_vendor_name_trgm": "SELECT 1 FROM catalog_vendor WHERE 'x' <% name",
        "catalog_person_name_trgm": "SELECT 1 FROM catalog_person WHERE 'x' <% name",
    }
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        for index, sql in checks.items():
            cursor.execute("EXPLAIN " + sql)
            plan = "\n".join(row[0] for row in cursor.fetchall())
            assert index in plan, (index, plan)


def test_the_vectors_follow_the_row(a, catalog):
    """Generated columns: written by Postgres on every insert and update, whatever path wrote the row."""
    Resource.objects.filter(id=catalog["ledger"].id).update(name="Abacus", description="Counting frames")
    with connection.cursor() as cursor:
        cursor.execute("SELECT search_vector::text FROM catalog_resource WHERE id = %s", [catalog["ledger"].id])
        vector = cursor.fetchone()[0]
    assert "'abacus':1A" in vector and "'counting':2B" in vector and "ledger" not in vector
    Person.objects.filter(id=a["people"]["dana"].id).update(anonymized_at="2026-01-01T00:00:00Z")
    with connection.cursor() as cursor:
        cursor.execute("SELECT search_vector::text FROM catalog_person WHERE id = %s", [a["people"]["dana"].id])
        assert cursor.fetchone()[0] == ""


def test_snippet_json_holds_no_html_from_any_field(a, api, catalog):
    """Every field a snippet can come from, filled with markup: the response holds only text segments."""
    field(a, "notes_cf", "text")
    evil = "<svg onload=alert(1)>evil</svg>"
    services.update_resource(
        a["org"],
        a["users"]["editor"],
        catalog["payroll"],
        {"purpose": evil, "tags": ["evil"], "custom_field_data": {"notes_cf": evil}},
    )
    admin_services.update_named(Vendor, a["org"], a["users"]["editor"], catalog["contoso"], {"notes": evil})
    body = find(api(a["users"]["viewer"]), "evil")
    assert names(body, "application") == ["Payroll"] and names(body, "vendor") == ["Contoso Software"]
    assert "<svg" not in json.dumps([r["snippet"] for g in body["groups"] for r in g["results"]])
