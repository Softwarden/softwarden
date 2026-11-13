"""The DRF layer's foundation (core/drf.py) as the application and component viewsets use it: role declarations, read-only
tokens, organization scoping, ETag / If-Match, keyset pagination, ordering, the filters' results and refusals (fixed
expectations recorded from catalog/listing.py before the FilterSets replaced it), and the rule that viewsets,
composite views and serializers never write models themselves. tests/test_drf_resources.py runs the scoping,
read-only token and If-Match checks over every viewset."""

import ast
import base64
import inspect
import json
import re
import types
from datetime import timedelta
from pathlib import Path

import pytest
from django.test import Client
from rest_framework import mixins

from catalog import services
from catalog.models import Kind, RecordReview, Resource, Vendor
from core import drf
from core.dates import org_today
from core.models import AuditEvent
from tests.test_listing import make
from tests.test_tokens import Bearer, mint  # noqa: F401  (mint is a fixture)

# ---- role declarations ---------------------------------------------------------------------------------------------


def test_policy_resolution():
    p = drf.policy(drf.VIEWER)
    assert (p.role_for("GET"), p.role_for("post"), p.role_for("DELETE")) == ("viewer", "editor", "editor")
    assert drf.policy(drf.ADMIN).role_for("POST") == "admin"  # write never drops below read
    p = drf.policy(drf.VIEWER, drf.EDITOR, destroy=drf.ADMIN, patch=drf.ADMIN)
    assert p.role_for("DELETE", "destroy") == "admin"
    assert p.role_for("PATCH", "partial_update") == "admin"  # a method override
    assert p.role_for("POST", "archive") == "editor"
    assert p.role_for("OPTIONS") == "viewer"
    assert drf.policy(drf.PUBLIC).role_for("POST") == "public"
    with pytest.raises(ValueError):
        drf.policy("owner")


def test_viewset_roles_and_messages(a, api):
    viewer, editor = api(a["users"]["viewer"]), api(a["users"]["editor"])
    app = a["app"].id
    assert viewer.get("applications").status_code == 200
    refused = viewer.post("applications", {"name": "No", "lifecycle": "active"})
    assert refused.status_code == 403 and refused["Content-Type"] == "application/problem+json"
    assert refused.json_body["detail"] == "Editors and Admins can make changes; your role is Viewer."
    assert viewer.post(f"applications/{app}/archive").status_code == 403
    refused = editor.delete(f"applications/{app}")
    assert refused.status_code == 403 and refused.json_body["detail"] == "Only Admins can do this."
    assert api().get("applications").status_code in (401, 403)


def test_a_method_the_route_lacks_is_405(a, api):
    assert api(a["users"]["admin"]).client.put(f"/api/v1/applications/{a['app'].id}").status_code == 405
    assert api(a["users"]["admin"]).client.delete("/api/v1/applications").status_code == 405


# ---- read-only tokens -------------------------------------------------------------------------------------------------


def test_read_only_token_reads_but_never_writes(a, mint):  # noqa: F811
    bearer = Bearer(mint("admin", read_only=True)["secret"])
    app = a["app"].id
    assert bearer.get("applications").status_code == 200
    assert bearer.get("components?lifecycle=active").status_code == 200
    writes = [
        bearer.post("applications", {"name": "Via token", "lifecycle": "active"}),
        bearer.post("components", {"name": "Via token", "lifecycle": "active"}),
        bearer.patch(f"applications/{app}", {"description": "changed"}),
        bearer.post(f"applications/{app}/archive", {}),
        bearer.client.delete(f"/api/v1/applications/{app}"),
        bearer.post("people", {"name": "Via token"}),  # another viewset
        bearer.patch("organization", {"review_interval_days": 120}),  # a function view: the same rule
    ]
    for response in writes:
        assert response.status_code == 403, response.status_code
        assert response.json()["detail"] == "This token is read-only."
    assert Resource.objects.get(id=app).description == ""
    assert not Resource.objects.filter(name="Via token").exists()


def test_a_writing_token_is_still_capped_by_its_owners_role(a, mint):  # noqa: F811
    editor = Bearer(mint("editor", read_only=False)["secret"])
    created = editor.post("applications", {"name": "Via token", "lifecycle": "active"})
    assert created.status_code == 201
    refused = editor.client.delete(f"/api/v1/applications/{created.json()['id']}")
    assert refused.status_code == 403 and refused.json()["detail"] == "Only Admins can do this."


# ---- organization scoping ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["application", "component"])
def test_another_organizations_record_is_404_everywhere(a, b, api, kind):
    theirs = b["app"] if kind == "application" else make(b, "Their job", kind="component")
    admin = api(a["users"]["admin"])
    url = f"{kind}s/{theirs.id}"
    for response in (
        admin.get(url),
        admin.patch(url, {"description": "mine now"}),
        admin.post(url + "/archive"),
        admin.post(url + "/restore"),
        admin.delete(url),
    ):
        assert response.status_code == 404 and response["Content-Type"] == "application/problem+json"
    theirs.refresh_from_db()
    assert theirs.description == "" and theirs.archived_at is None
    listed = {r["id"] for r in admin.get(f"{kind}s?archived=all&page_size=200").json_body["results"]}
    assert str(theirs.id) not in listed
    owner = b["people"]["dana"].id
    assert admin.get(f"{kind}s?owner={owner}").json_body["results"] == []


# ---- ETag / If-Match ------------------------------------------------------------------------------------------------


class Raw:
    """A signed-in client that can send If-Match."""

    def __init__(self, user):
        self.client = Client()
        self.client.force_login(user)

    def send(self, method, url, data=None, if_match=None):
        extra = {"HTTP_IF_MATCH": if_match} if if_match is not None else {}
        body = json.dumps(data) if data is not None else None
        return getattr(self.client, method)("/api/v1/" + url, body, content_type="application/json", **extra)


ETAG = re.compile(r'^"[0-9a-f]{32}"$')


@pytest.mark.parametrize("kind", ["application", "component"])
def test_etag_on_reads_and_writes(a, kind):
    record = a["app"] if kind == "application" else make(a, "Nightly job", kind="component")
    raw = Raw(a["users"]["editor"])
    url = f"{kind}s/{record.id}"
    first = raw.send("get", url)
    assert ETAG.match(first["ETag"])
    assert raw.send("get", url)["ETag"] == first["ETag"]  # stable while nothing changes
    updated = raw.send("patch", url, {"description": "Changed"}, if_match=first["ETag"])
    assert updated.status_code == 200 and updated["ETag"] != first["ETag"]
    assert raw.send("get", url)["ETag"] == updated["ETag"]
    tagged = raw.send("patch", url, {"tags": ["finance"]})  # no If-Match: still allowed
    assert tagged.status_code == 200 and tagged["ETag"] != updated["ETag"]  # tags are part of the record
    archived = raw.send("post", url + "/archive")
    assert archived.status_code == 200 and archived["ETag"] != tagged["ETag"]
    created = raw.send("post", f"{kind}s", {"name": "Fresh", "lifecycle": "active"})
    assert created.status_code == 201 and ETAG.match(created["ETag"])
    assert raw.send("get", f"{kind}s/{created.json()['id']}")["ETag"] == created["ETag"]


def test_a_stale_if_match_is_412_and_changes_nothing(a):
    raw = Raw(a["users"]["admin"])
    url = f"applications/{a['app'].id}"
    seen = raw.send("get", url)["ETag"]
    services.update_resource(a["org"], a["users"]["editor"], a["app"], {"description": "Someone else's edit"})
    events = AuditEvent.objects.filter(target_id=a["app"].id).count()
    stale = raw.send("patch", url, {"description": "Mine"}, if_match=seen)
    assert stale.status_code == 412 and stale["Content-Type"] == "application/problem+json"
    body = stale.json()
    assert body["type"] == "/problems/precondition-failed" and body["status"] == 412
    current = raw.send("get", url)["ETag"]
    assert stale["ETag"] == current and body["revision"] == current.strip('"')
    assert Resource.objects.get(id=a["app"].id).description == "Someone else's edit"
    assert AuditEvent.objects.filter(target_id=a["app"].id).count() == events
    refused = raw.send("delete", url, if_match=seen)
    assert refused.status_code == 412 and Resource.objects.filter(id=a["app"].id).exists()
    # If-Match compares strongly (RFC 9110 §13.1.1): a weak tag never matches; a list and * do.
    assert raw.send("patch", url, {"description": "A"}, if_match=f"W/{current}").status_code == 412
    assert raw.send("patch", url, {"description": "A"}, if_match=f'"0000", {current}').status_code == 200
    current = raw.send("get", url)["ETag"]
    assert raw.send("patch", url, {"description": "B"}, if_match="*").status_code == 200


def test_a_matching_if_match_lets_an_admin_delete(a):
    doomed = make(a, "Doomed")
    raw = Raw(a["users"]["admin"])
    tag = raw.send("get", f"applications/{doomed.id}")["ETag"]
    assert raw.send("delete", f"applications/{doomed.id}", if_match=tag).status_code == 204
    assert not Resource.objects.filter(id=doomed.id).exists()


def test_undo_still_uses_the_expected_value_check(a, api):
    """The SPA's Undo sends `expected` (the stale-value 409), which keeps working beside If-Match."""
    client = api(a["users"]["editor"])
    url = f"applications/{a['app'].id}"
    dana, priya = str(a["people"]["dana"].id), str(a["people"]["priya"].id)
    client.patch(url, {"business_owner": dana})
    client.patch(url, {"business_owner": priya})
    undo = client.patch(url, {"business_owner": None, "expected": {"business_owner": dana}, "undo": True})
    assert undo.status_code == 409 and undo.json_body["type"] == "/problems/stale-value"
    assert AuditEvent.objects.filter(target_id=a["app"].id, action="resource.undo_refused").count() == 1


# ---- pagination and ordering -----------------------------------------------------------------------------------------


@pytest.fixture
def ten(a):
    return [make(a, f"Item {i:02d}", criticality=("high" if i % 2 else "low")) for i in range(10)]


def follow(client, url, size):
    rows, cursor = [], None
    while True:
        sep = "&" if "?" in url else "?"
        page = client.get(f"{url}{sep}page_size={size}" + (f"&cursor={cursor}" if cursor else ""))
        assert page.status_code == 200, page.json_body
        assert set(page.json_body) == {"results", "next", "previous"}
        rows += page.json_body["results"]
        cursor = page.json_body["next"]
        if not cursor:
            return rows


def test_ordering_from_the_whitelist(a, api, ten):
    client = api(a["users"]["viewer"])
    names = [r["name"] for r in follow(client, "applications?ordering=-name", 3)]
    assert names == sorted(names, reverse=True) and len(names) == 11
    by_created = [r["name"] for r in follow(client, "applications?ordering=created_at", 4)]
    assert by_created == ["orga App"] + [f"Item {i:02d}" for i in range(10)]
    mixed = [(r["criticality"] or "", r["name"]) for r in follow(client, "applications?ordering=criticality,-name", 2)]
    assert [c for c, _ in mixed] == sorted((c for c, _ in mixed), key=CRITICALITY_RANK.index)  # by rank, not A to Z
    for level in {c for c, _ in mixed}:
        group = [n for c, n in mixed if c == level]
        assert group == sorted(group, reverse=True)
    refused = client.get("applications?ordering=description")
    assert refused.status_code == 400 and "ordering" in refused.json_body["errors"]


CRITICALITY_RANK = ["critical", "high", "medium", "low", ""]
LIFECYCLE_RANK = ["proposed", "evaluating", "active", "deprecated", "retired"]


@pytest.fixture
def ranks(a):
    """Records spread over every criticality (and blank) and every lifecycle, out of order by name."""
    mix = [
        ("low", "retired"),
        ("critical", "active"),
        ("", "proposed"),
        ("high", "deprecated"),
        ("medium", "evaluating"),
        ("critical", "proposed"),
        ("", "active"),
        ("low", "evaluating"),
    ]
    return [make(a, f"Rank {i}", criticality=c, lifecycle=life) for i, (c, life) in enumerate(mix)]


@pytest.mark.parametrize("descending", [False, True])
def test_criticality_sorts_by_rank_with_blank_last_both_ways(a, api, ranks, descending):
    """critical, high, medium, low (or the reverse), never alphabetically; blank last in both directions. Two rows a
    page, so the cursors cross every rank boundary."""
    client = api(a["users"]["viewer"])
    rows = follow(client, f"applications?ordering={'-' if descending else ''}criticality", 2)
    values = [r["criticality"] or "" for r in rows]
    ranked = CRITICALITY_RANK[:4][::-1] + [""] if descending else CRITICALITY_RANK
    assert values == sorted(values, key=ranked.index)
    assert values[-1] == "" and len(rows) == len({r["id"] for r in rows}) == 9  # the fixture's own app is blank too


@pytest.mark.parametrize("descending", [False, True])
def test_lifecycle_sorts_in_lifecycle_order_both_ways(a, api, ranks, descending):
    client = api(a["users"]["viewer"])
    rows = follow(client, f"applications?ordering={'-' if descending else ''}lifecycle", 3)
    values = [r["lifecycle"] for r in rows]
    order = LIFECYCLE_RANK[::-1] if descending else LIFECYCLE_RANK
    assert values == sorted(values, key=order.index)
    assert len(rows) == len({r["id"] for r in rows}) == 9


def test_previous_pages_across_a_rank(a, api, ranks):
    client = api(a["users"]["viewer"])
    p1 = client.get("applications?page_size=3&ordering=-criticality").json_body
    p2 = client.get(f"applications?page_size=3&ordering=-criticality&cursor={p1['next']}").json_body
    back = client.get(f"applications?page_size=3&ordering=-criticality&cursor={p2['previous']}").json_body
    assert back["results"] == p1["results"] and back["previous"] is None
    assert not {r["id"] for r in p1["results"]} & {r["id"] for r in p2["results"]}


def test_a_cursor_belongs_to_its_ordering(a, api, ten):
    client = api(a["users"]["viewer"])
    cursor = client.get("applications?page_size=2").json_body["next"]
    refused = client.get(f"applications?page_size=2&ordering=-name&cursor={cursor}")
    assert refused.status_code == 400 and "cursor" in refused.json_body["errors"]


def test_previous_pages_with_a_descending_ordering(a, api, ten):
    client = api(a["users"]["viewer"])
    p1 = client.get("applications?page_size=4&ordering=-name").json_body
    p2 = client.get(f"applications?page_size=4&ordering=-name&cursor={p1['next']}").json_body
    back = client.get(f"applications?page_size=4&ordering=-name&cursor={p2['previous']}").json_body
    assert back["results"] == p1["results"] and back["previous"] is None


# ---- filters: the results and refusals catalog/listing.py gave, as fixed expectations -------------------------


@pytest.fixture
def varied(a):
    """Records that differ on every filter the SPA sends."""
    editor, org = a["users"]["editor"], a["org"]
    dana, gone = a["people"]["dana"], a["people"]["gone"]
    vendor = Vendor.objects.create(organization=org, name="Contoso")
    rows = [
        make(
            a, "Payroll", criticality="critical", business_owner=str(dana.id), tags=["finance"], vendor=str(vendor.id)
        ),
        make(a, "Payroll export", lifecycle="deprecated", technical_owner=str(dana.id), support_team=str(a["team"].id)),
        make(a, "Old CRM", lifecycle="retired", criticality="low", description="customer records"),
        make(a, "Night batch", kind="component", component_type="scheduled_job", tags=["Finance"]),
        make(a, "Sync script", kind="component", component_type="script", business_owner=str(dana.id)),
        make(a, "Proposal tool", lifecycle="proposed", it_steward=a["users"]["admin"].id),
        make(a, "Old payroll job", kind="component", lifecycle="retired", description="customer feed"),
        make(a, "Team job", kind="component", criticality="critical", support_team=str(a["team"].id)),
    ]
    services.archive_resource(org, editor, rows[2])
    services.archive_resource(org, editor, rows[6])
    Resource.objects.filter(id=rows[5].id).update(technical_owner=gone)  # an inactive owner: a gap
    today = org_today(org)
    RecordReview.objects.create(
        organization=org,
        resource=rows[0],
        confirmed_by=dana,
        recorded_by=editor,
        confirmation_date=today - timedelta(days=200),
        next_review_date=today - timedelta(days=10),
        evidence="Email",
        confirmed_values={},
    )
    return {"vendor": vendor, "dana": dana}


def parity_queries(a, varied):
    dana, team, vendor = varied["dana"].id, a["team"].id, varied["vendor"].id
    steward = a["users"]["admin"].id
    common = [
        {},
        {"lifecycle": "active,deprecated", "archived": "false"},
        {"lifecycle": "active,deprecated", "archived": "false", "gap": "true"},
        {"lifecycle": "active,deprecated", "archived": "false", "review_state": "overdue"},
        {"archived": "true"},
        {"archived": "all"},
        {"review_state": "unreviewed"},
        {"gap": "false"},
        {"criticality": "critical"},
        {"owner": str(dana)},
        {"support_team": str(team)},
        {"it_steward": str(steward)},
        {"tag": "FINANCE"},
        {"tag": "  finance  "},
        {"imported": "true"},
        {"imported": "false"},
        {"q": "payroll"},
        {"q": "Payroll export"},
        {"q": "customer", "archived": "all"},
        {"q": "   "},
        # refused
        {"lifecycle": "sleeping"},
        {"lifecycle": ""},
        {"criticality": "extreme"},
        {"criticality": ""},
        {"gap": "maybe"},
        {"review_state": "stale"},
        {"archived": "yes"},
        {"owner": "not-a-uuid"},
        {"support_team": "1"},
        {"it_steward": "abc"},
        {"imported": "1"},
        {"colour": "red"},
        {"criticality": "extreme", "owner": "x", "colour": "red"},
    ]
    return {
        Kind.APPLICATION: common + [{"vendor": str(vendor)}, {"vendor": "nope"}, {"component_type": "script"}],
        Kind.COMPONENT: common
        + [{"component_type": "scheduled_job"}, {"component_type": "nope"}, {"vendor": str(vendor)}],
    }


LIFECYCLES = ["Use one or more of: proposed, evaluating, active, deprecated, retired (comma-separated)."]
CRITICALITIES = ["Use one of: low, medium, high, critical."]
UNKNOWN = ["This filter isn't available here."]
AN_ID = ["Use an ID from the list."]
COMPONENT_TYPES = [
    "Use one of: script, scheduled_job, runbook, function, logic_app, automation_flow, report, ai_service, "
    "middleware, internal_api, api, service, workflow, infra_automation, other."
]
APPS = ["Payroll", "Payroll export", "Proposal tool", "orga App"]
JOBS = ["Night batch", "Sync script", "Team job"]
# Line for line with parity_queries: the record names a query returns, or the errors it is refused with. Recorded
# from catalog/listing.py (the function views' filter code) on this fixture before the FilterSets replaced it.
REFUSED = [
    {"lifecycle": LIFECYCLES},
    {"lifecycle": LIFECYCLES},
    {"criticality": CRITICALITIES},
    {"criticality": CRITICALITIES},
    {"gap": ["Use one of: true, false."]},
    {"review_state": ["Use one of: unreviewed, needs_confirmation, overdue, confirmed."]},
    {"archived": ["Use one of: true, false, all."]},
    {"owner": AN_ID},
    {"support_team": AN_ID},
    {"it_steward": ["Use a user ID from the list."]},
    {"imported": ["Use one of: true, false."]},
    {"colour": UNKNOWN},
    {"colour": UNKNOWN, "criticality": CRITICALITIES, "owner": AN_ID},
]
EXPECTED = {
    Kind.APPLICATION: [
        APPS,
        ["Payroll", "Payroll export", "orga App"],
        ["Payroll", "Payroll export", "orga App"],
        [],
        ["Old CRM"],
        ["Old CRM", *APPS],
        ["Payroll export", "Proposal tool", "orga App"],
        [],
        ["Payroll"],
        ["Payroll", "Payroll export"],
        ["Payroll export"],
        ["Proposal tool"],
        ["Payroll"],
        ["Payroll"],
        [],
        APPS,
        ["Payroll", "Payroll export"],
        ["Payroll", "Payroll export"],
        ["Old CRM"],
        APPS,
        *REFUSED,
        ["Payroll"],
        {"vendor": AN_ID},
        {"component_type": UNKNOWN},
    ],
    Kind.COMPONENT: [
        JOBS,
        JOBS,
        JOBS,
        [],
        ["Old payroll job"],
        ["Night batch", "Old payroll job", "Sync script", "Team job"],
        JOBS,
        [],
        ["Team job"],
        ["Sync script"],
        ["Team job"],
        [],
        ["Night batch"],
        ["Night batch"],
        [],
        JOBS,
        [],
        [],
        ["Old payroll job"],
        JOBS,
        *REFUSED,
        ["Night batch"],
        {"component_type": COMPONENT_TYPES},
        {"vendor": UNKNOWN},
    ],
}


@pytest.mark.parametrize("kind", [Kind.APPLICATION, Kind.COMPONENT])
def test_filters_give_the_recorded_results(a, b, api, varied, kind):
    from urllib.parse import urlencode

    client = api(a["users"]["viewer"])
    make(b, "Payroll", criticality="critical")  # another organization's namesake never shows
    queries = parity_queries(a, varied)[kind]
    assert len(queries) == len(EXPECTED[kind])
    outcomes = set()
    for params, expected in zip(queries, EXPECTED[kind], strict=True):
        response = client.get(f"{kind}s?{urlencode({**params, 'page_size': 200})}")
        if isinstance(expected, dict):
            assert response.status_code == 400, (params, response.json_body)
            assert response.json_body["errors"] == expected, params
        else:
            assert response.status_code == 200, (params, response.json_body)
            assert sorted(r["name"] for r in response.json_body["results"]) == expected, params
            outcomes.add(tuple(expected))
    assert len(outcomes) >= 6  # the fixture makes the filters disagree, so equal results mean something


@pytest.mark.parametrize("kind", [Kind.APPLICATION, Kind.COMPONENT])
def test_export_and_saved_filters_refuse_what_the_list_refuses(a, api, varied, kind):
    """CSV export and saved filters validate through the list's FilterSet: the same refusals and messages."""
    from urllib.parse import urlencode

    client = api(a["users"]["editor"])
    for params, expected in zip(parity_queries(a, varied)[kind], EXPECTED[kind], strict=True):
        if not isinstance(expected, dict):
            continue
        exported = client.client.get(f"/api/v1/{kind}s/export.csv?{urlencode(params)}")
        assert exported.status_code == 400 and exported.json()["errors"] == expected, params
        kept = {k: v for k, v in params.items() if v != ""}  # a saved query drops empty values, as before
        if not kept:
            continue
        saved = client.post("saved-filters", {"name": "Refused", "kind": kind, "query": kept})
        assert saved.status_code == 400, params
        wanted = [f"{name}: {' '.join(messages)}" for name, messages in expected.items()]
        assert saved.json_body["errors"]["query"] == wanted, params


def test_paging_errors_join_filter_errors(a, api):
    response = api(a["users"]["viewer"]).get("applications?criticality=extreme&page_size=0&ordering=colour")
    assert response.status_code == 400
    assert set(response.json_body["errors"]) == {"criticality", "page_size", "ordering"}


def b64(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "ordering,values",
    [
        ("", ["x", "orga App"]),  # a text rank bucket
        ("", [0, 5]),  # a number for a name
        ("", [True, "orga App"]),  # a boolean
        ("", [0, "orga App", "not-a-uuid"]),  # a bad id (and one value too many is caught first)
        ("created_at", ["yesterday"]),  # a garbage date
        ("created_at", ["2026-13-45 99:00:00+00:00"]),
        ("created_at", [1]),
    ],
)
def test_a_tampered_cursor_is_the_cursor_problem(a, api, ordering, values):
    client = api(a["users"]["viewer"])
    keys = "rank_bucket,name,id" if not ordering else f"{ordering},id"
    good = client.get("applications" + (f"?ordering={ordering}" if ordering else "")).json_body["results"][0]["id"]
    cursor = b64(["a", keys, *values, good] if len(values) < 3 else ["a", keys, *values])
    query = f"applications?cursor={cursor}" + (f"&ordering={ordering}" if ordering else "")
    response = client.get(query)
    assert response.status_code == 400, response.json_body
    assert response.json_body["errors"] == {"cursor": [drf.BAD_CURSOR]}


def test_function_views_look_up_the_membership_once(a, api, monkeypatch):
    from core import access

    calls = []
    real = access.memberships_of

    def counted(user):
        calls.append(user)
        return real(user)

    monkeypatch.setattr(access, "memberships_of", counted)
    assert api(a["users"]["editor"]).get("organization").status_code == 200  # RolePermission, then org_role
    assert len(calls) == 1
    calls.clear()
    assert api(a["users"]["editor"]).get("applications").status_code == 200
    assert len(calls) == 1


def test_unknown_parameters_are_named(a, api):
    response = api(a["users"]["viewer"]).get("applications?colour=red&size=xl")
    assert response.status_code == 400
    assert response.json_body["errors"] == {
        "colour": ["This filter isn't available here."],
        "size": ["This filter isn't available here."],
    }
    assert response.json_body["detail"] == "Some fields need attention."


# ---- Viewsets and serializers never write -------------------------------------------------------------------------

WRITE_CALLS = {"save", "delete", "bulk_create", "bulk_update", "get_or_create", "update_or_create"}
# A manager or queryset write; any receiver except a dict literal or a name that holds request data or plain values.
QUERYSET_WRITES = {"create", "update"}
PLAIN_NAMES = {"data", "payload", "body", "params", "kwargs", "errors", "context", "headers"}
# Related-manager writes (record.tags.add(...)); a bare local name is a set or list, not a manager.
RELATED_WRITES = {"add", "set", "remove", "clear"}
BUILTINS = {"set", "dict", "list", "frozenset"}


def subclasses(base):
    found, todo = set(), [base]
    while todo:
        cls = todo.pop()
        for sub in cls.__subclasses__():
            found.add(sub)
            todo.append(sub)
    return found


def viewset_classes():
    import api.urls  # noqa: F401  (importing the routes imports every viewset)
    import catalog.urls  # noqa: F401
    import core.urls  # noqa: F401
    import discovery.urls  # noqa: F401
    import imports.urls  # noqa: F401
    import money.urls  # noqa: F401
    import notify.urls  # noqa: F401

    return subclasses(drf.TenantViewSet)


def composite_view_classes():
    viewset_classes()
    return subclasses(drf.RoleAPIView)


def serializer_modules():
    from api import serializers as api_serializers
    from catalog import account_serializers, assessment_render, audit_log
    from catalog import serializers as catalog_serializers
    from discovery import serializers as discovery_serializers
    from imports import serializers as import_serializers
    from money import serializers as money_serializers
    from notify import serializers as notify_serializers

    return {
        catalog_serializers,
        money_serializers,
        notify_serializers,
        assessment_render,
        drf,
        api_serializers,
        account_serializers,
        audit_log,
        discovery_serializers,
        import_serializers,
    }


def is_write(name, receiver):
    if name in WRITE_CALLS:
        return True
    if name in QUERYSET_WRITES:
        return not isinstance(receiver, ast.Dict) and not (
            isinstance(receiver, ast.Name) and receiver.id in PLAIN_NAMES
        )
    if name in RELATED_WRITES:
        builtin = isinstance(receiver, ast.Call) and getattr(receiver.func, "id", None) in BUILTINS
        return isinstance(receiver, ast.Attribute | ast.Call) and not builtin
    return False


def calls_a_module(receiver, module):
    """`saved_filters.create(...)` or `renewals.delete_renewal(...)`: a service module's function, not a model write."""
    if not isinstance(receiver, ast.Name):
        return False
    # A module-level dict, set or list (core/oidc_simulator.py IDPS.update(...)) isn't a model either.
    return isinstance(getattr(module, receiver.id, None), types.ModuleType | dict | set | list)


def writes_in(module):
    tree = ast.parse(inspect.getsource(module))
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if is_write(node.func.attr, node.func.value) and not calls_a_module(node.func.value, module):
                problems.append(f"{module.__name__}:{node.lineno} {ast.unparse(node.func)}(...)")
    return problems


def test_the_write_scan_catches_writes():
    def scan(source):
        node = ast.parse(source).body[0].value
        return is_write(node.func.attr, node.func.value)

    for source in (
        "record.save()",
        "Resource.objects.create(name='x')",
        "self.get_queryset().update(name='x')",
        "qs.update(name='x')",
        "Resource.objects.filter(id=1).delete()",
        "record.tags.add(tag)",
        "self.get_object().tags.set([])",
        "obj.instances.clear()",
    ):
        assert scan(source), source
    for source in ("{}.update(x)", "data.update(x)", "seen.add(x)", "set().add(x)", "errors.update(x)"):
        assert not scan(source), source


def test_viewsets_and_serializers_never_write_models():
    classes = viewset_classes()
    composites = composite_view_classes()
    assert {c.__name__ for c in classes} >= {
        "ApplicationViewSet",
        "ComponentViewSet",
        "IntegrationViewSet",
        "RelationshipViewSet",
        "PersonViewSet",
        "RenewalViewSet",
        "SnoozeViewSet",
        "DigestViewSet",
        "ConnectorViewSet",
        "ObservationViewSet",
        "ImportRunViewSet",
        "ImportSourceViewSet",
        "ApiTokenViewSet",
        "UserViewSet",
        "AuditEventViewSet",
    }
    assert {c.__name__ for c in composites} >= {
        "NeedsAttentionView",
        "DependenciesView",
        "BulkApplyView",
        "OrganizationView",
        "PasswordView",
        "SystemView",
    }
    for cls in classes:  # DRF's model mixins call serializer.save(); the base must not bring them in
        assert not issubclass(cls, mixins.CreateModelMixin | mixins.UpdateModelMixin | mixins.DestroyModelMixin)
    modules = {inspect.getmodule(c) for c in classes | composites} | serializer_modules()
    assert {m.__name__ for m in modules} >= {
        "catalog.viewsets",
        "catalog.directory_viewsets",
        "catalog.curation_viewsets",
        "money.viewsets",
        "notify.viewsets",
        "money.serializers",
        "notify.serializers",
        "discovery.viewsets",
        "imports.viewsets",
        "api.viewsets",
        "catalog.account_viewsets",
        "catalog.audit_viewsets",
        "core.system_views",
        "discovery.serializers",
        "imports.serializers",
        "api.serializers",
        "catalog.account_serializers",
    }
    problems = [p for m in modules for p in writes_in(m)]
    assert problems == []


def routed_modules():
    """The module of every routed view (function views and plain Django views included) and of every class of ours it
    inherits from: what the resolver can reach, not what subclasses a base."""
    from django.conf import settings
    from django.urls import get_resolver

    from tests.test_url_coverage import walk

    base = Path(settings.BASE_DIR).resolve()
    found = set()
    for _route, callback in walk(get_resolver().url_patterns):
        cls = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
        for target in cls.__mro__ if cls is not None else (callback,):
            module = inspect.getmodule(target)
            source = getattr(module, "__file__", None)  # builtins (object) has none
            if source and Path(source).resolve().is_relative_to(base) and ".venv" not in Path(source).parts:
                found.add(module)
    return found


def test_no_routed_module_writes_models():
    """Views never save domain models: every module a route's view comes from, function views too, has no model
    write (the scan above, which found its modules through the viewset and composite-view bases)."""
    modules = routed_modules()
    names = {m.__name__ for m in modules}
    assert {"core.setup_views", "softwarden.ops", "catalog.viewsets", "core.drf"} <= names
    problems = [p for m in sorted(modules, key=lambda m: m.__name__) for p in writes_in(m)]
    assert problems == []


def test_read_only_serializers_refuse_to_write(a):
    from catalog.serializers import ApplicationSerializer

    serializer = ApplicationSerializer()
    with pytest.raises(NotImplementedError):
        serializer.create({})
    with pytest.raises(NotImplementedError):
        serializer.update(a["app"], {})
