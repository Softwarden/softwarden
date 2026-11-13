"""Dependencies and map (API): each record's `/dependencies` (the explorer with the record as the target,
depth up to 10 or all, cycle marking) and its `/map` (everything it is connected to, from a registry of
contributors), with tenancy, roles, query counts and OpenAPI."""

import json

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from catalog import graph, record_map, relationship_types, services
from catalog.models import Resource
from core import connections
from discovery import engine as discovery
from discovery.models import Observation

# ---- helpers ------------------------------------------------------------------------------------------------------


def record(ctx, name, kind="application", lifecycle="active", **extra):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": lifecycle, **extra}
    )


def link(ctx, source, target, slug="depends_on", **extra):
    """source <slug> target (source depends on target for depends_on)."""
    return services.create_relationship(
        ctx["org"],
        ctx["users"]["editor"],
        {"type": slug, "source": str(source.id), "target": str(target.id), **extra},
    )


def flow(ctx, name, source, target, **extra):
    return services.create_integration(
        ctx["org"],
        ctx["users"]["editor"],
        {"name": name, "lifecycle": "active", "source": str(source.id), "target": str(target.id), **extra},
    )


def retire(ctx, resource):
    services.update_resource(
        ctx["org"],
        ctx["users"]["editor"],
        Resource.objects.get(pk=resource.pk),
        {"lifecycle": "retired", "retirement_ack": True, "retirement_ack_reason": "Test setup"},
    )


def custom_type(ctx, name, is_dependency):
    return relationship_types.create_type(
        ctx["org"],
        ctx["users"]["admin"],
        {"name": name, "inverse_name": f"{name} (inverse)", "is_dependency": is_dependency},
    )


def get(client, url):
    response = client.get(url)
    assert response.status_code == 200, (url, response.status_code, response.json_body)
    return response.json_body


def deps(client, resource, prefix="applications", **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return get(client, f"{prefix}/{resource.id}/dependencies" + (f"?{query}" if query else ""))


def names(rows):
    return [r["name"] for r in rows]


def line(ctx, n):
    """R0 <- R1 <- ... <- R(n-1): each record depends on the one before it."""
    rows = [record(ctx, f"R{i}") for i in range(n)]
    for before, after in zip(rows, rows[1:], strict=False):
        link(ctx, after, before)
    return rows


# ---- a record's dependencies --------------------------------------------------------------------------------------


def test_depth_one_two_n_and_all(a, api):
    client = api(a["users"]["viewer"])
    rows = line(a, 7)
    assert names(deps(client, rows[0])["nodes"]) == ["R1"]  # default depth 1, dependents
    assert names(deps(client, rows[0], depth=2)["nodes"]) == ["R1", "R2"]
    three = deps(client, rows[0], depth=3)
    assert names(three["nodes"]) == ["R1", "R2", "R3"] and three["depth"] == "3"
    assert names(deps(client, rows[0], depth=5)["nodes"]) == ["R1", "R2", "R3", "R4", "R5"]
    every = deps(client, rows[0], depth="all")
    assert names(every["nodes"]) == [f"R{i}" for i in range(1, 7)] and every["depth"] == "all"
    assert [n["depth"] for n in every["nodes"]] == [1, 2, 3, 4, 5, 6]
    assert names(deps(client, rows[0], depth=10)["nodes"]) == names(every["nodes"])


@pytest.mark.parametrize("depth", ["0", "11", "-1", "x", "2.5", "１"])
def test_depth_outside_the_range_is_a_field_error(a, api, depth):
    client = api(a["users"]["viewer"])
    for url in (
        f"applications/{a['app'].id}/dependencies?depth={depth}",
        f"dependencies?target={a['app'].id}&depth={depth}",
    ):
        response = client.get(url)
        assert response.status_code == 400 and response["Content-Type"] == "application/problem+json"
        assert response.json_body["errors"] == {"depth": ["Choose a number from 1 to 10, or all."]}


def test_other_parameters_are_validated_like_the_explorer(a, api):
    response = api(a["users"]["viewer"]).get(
        f"applications/{a['app'].id}/dependencies?direction=up&include_retired=maybe&data_flows=1"
    )
    assert response.status_code == 400
    assert set(response.json_body["errors"]) == {"direction", "include_retired", "data_flows"}


def test_direction_and_include_retired(a, api):
    client = api(a["users"]["viewer"])
    core, mid, top = line(a, 3)
    assert deps(client, mid)["direction"] == "dependents" and names(deps(client, mid)["nodes"]) == ["R2"]
    assert names(deps(client, mid, direction="depends_on")["nodes"]) == ["R0"]
    assert sorted(names(deps(client, mid, direction="both")["nodes"])) == ["R0", "R2"]
    retire(a, top)
    hidden = deps(client, mid)
    assert hidden["nodes"] == [] and hidden["summary"]["hidden_retired"] == 1
    assert names(deps(client, mid, include_retired="true")["nodes"]) == ["R2"]


def test_the_record_route_answers_what_the_explorer_answers(a, api):
    client = api(a["users"]["viewer"])
    rows = line(a, 4)
    flow(a, "Feed", rows[1], record(a, "Far", "component"), protocol="SFTP")
    query = "direction=both&depth=all&show_instances=true&include_retired=true"
    mine = get(client, f"applications/{rows[1].id}/dependencies?{query}")
    assert mine == get(client, f"dependencies?target={rows[1].id}&{query}")
    assert mine == graph.traverse(a["org"], rows[1], "both", None, True, True, True)


def test_each_record_kind_has_the_route_and_a_kind_mismatch_is_404(a, api):
    client = api(a["users"]["viewer"])
    component, agent = record(a, "Job", "component"), record(a, "Helper", "ai_agent")
    link(a, agent, component)
    assert names(deps(client, component, "components")["nodes"]) == ["Helper"]
    assert deps(client, agent, "ai-agents", direction="depends_on")["nodes"][0]["name"] == "Job"
    assert client.get(f"applications/{component.id}/dependencies").status_code == 404
    assert client.get(f"components/{agent.id}/map").status_code == 404
    feed = flow(a, "Feed", a["app"], component)
    assert client.get(f"applications/{feed.id}/dependencies").status_code == 404


def test_cycles_are_marked_with_the_closing_link(a, api):
    """A -> B -> C -> A (each depends on the next): from A, dependents walk C, B and then the link B -> ... back."""
    client = api(a["users"]["viewer"])
    x, y, z = record(a, "A"), record(a, "B"), record(a, "C")
    link(a, x, y)
    link(a, y, z)
    closing = link(a, z, x)
    body = deps(client, x, depth="all")
    assert body["summary"]["cycles"] == 1
    assert [(n["name"], n["cycle"]) for n in body["nodes"]] == [("C", False), ("B", False), ("A", True)]
    [cycle] = body["cycles"]
    cycle_row = next(n for n in body["nodes"] if n["cycle"])
    assert cycle["relationship"] == cycle_row["link"]["relationship"]
    assert cycle["closes_at"] == str(x.id) and cycle["reached_from"] == str(y.id)
    assert cycle["direction"] == "dependents" and cycle["depth"] == 3 and cycle["type"] == "depends_on"
    assert (cycle["source"], cycle["target"]) == (str(x.id), str(y.id))
    assert closing.id != cycle["relationship"]  # from A, the walk enters through Z -> A and closes on A -> B
    both = deps(client, x, depth="all", direction="both")
    assert {c["direction"] for c in both["cycles"]} == {"dependents", "depends_on"}
    assert deps(client, x, depth=1)["cycles"] == []  # not reached within one link


def test_a_custom_dependency_type_is_followed_and_a_plain_one_is_not(a, api):
    client = api(a["users"]["viewer"])
    trust = custom_type(a, "SAML trust for", is_dependency=True)
    mention = custom_type(a, "Mentions", is_dependency=False)
    idp, portal, wiki = record(a, "Identity provider"), record(a, "Portal"), record(a, "Wiki")
    link(a, portal, idp, trust.slug)
    link(a, wiki, idp, mention.slug)
    body = deps(client, idp, depth="all")
    assert names(body["nodes"]) == ["Portal"]
    assert body["nodes"][0]["path"][-1]["via"] == {
        "relationship": body["nodes"][0]["link"]["relationship"],
        "type": trust.slug,
        "type_name": "SAML trust for",
    }


def test_what_depends_on_this_app_registration(a, api):
    """The acceptance check: an application depends on the app registration; the registration's dependents (all depths)
    return the application, from the component route or, modelled as an application, from the application route.
    The application's Map lists the registration one step out, with the link to ask its own dependencies."""
    client = api(a["users"]["viewer"])
    registration = record(a, "HR portal app registration", "component", component_type="service")
    portal = record(a, "HR portal")
    link(a, portal, registration)
    mobile = record(a, "HR mobile")
    link(a, mobile, portal)
    body = deps(client, registration, "components", depth="all")
    assert [(n["name"], n["depth"]) for n in body["nodes"]] == [("HR portal", 1), ("HR mobile", 2)]
    as_app = record(a, "Payroll app registration", entra_app_id="00000000-0000-0000-0000-00000000abcd")
    link(a, portal, as_app)
    assert names(deps(client, as_app, depth="all")["nodes"]) == ["HR portal", "HR mobile"]
    the_map = get(client, f"applications/{portal.id}/map")
    reg_node = next(n for n in the_map["nodes"] if n["id"] == str(registration.id))
    assert reg_node["hop"] == 1 and reg_node["path"] == f"components/{registration.id}"
    row = next(r for r in the_map["rows"] if r["other"] == str(registration.id))
    assert row["reads"] == "Depends on" and row["category"] == "dependency"


def test_data_flows_carry_direction_and_auth_method(a):
    core, job = record(a, "Core"), record(a, "Job", "component")
    flow(a, "Two way", core, job, direction="two_way", auth_method="managed_identity", protocol="REST")
    [found] = graph.traverse(a["org"], core)["data_flows"]
    assert (found["direction"], found["auth_method"], found["protocol"]) == ("two_way", "managed_identity", "REST")
    flow(a, "Unrecorded", job, core)
    blank = next(f for f in graph.traverse(a["org"], core)["data_flows"] if f["name"] == "Unrecorded")
    assert blank["direction"] == "" and blank["auth_method"] == ""


def test_another_organizations_record_is_404_on_both_routes(a, b, api):
    theirs = record(b, "Theirs")
    client = api(a["users"]["admin"])
    for suffix in ("dependencies", "dependencies?depth=all&direction=both", "map", "map?depth=2"):
        assert client.get(f"applications/{theirs.id}/{suffix}").status_code == 404


def mesh(ctx, n):
    """A hub with n dependents in a ring of dependencies among themselves, each with a flow, plus instances."""
    hub = record(ctx, "Hub")
    services.create_instance(ctx["org"], ctx["users"]["editor"], hub, {"name": "Production"})
    rows = [record(ctx, f"N{i:03}", "component" if i % 2 else "application") for i in range(n)]
    for i, r in enumerate(rows):
        link(ctx, r, hub)
        link(ctx, rows[(i + 1) % n], r)
        flow(ctx, f"F{i:03}", r, hub)
    return hub


def count_queries(client, url):
    with CaptureQueriesContext(connection) as queries:
        assert client.get(url).status_code == 200
    return len(queries)


@pytest.mark.parametrize("suffix", ["dependencies?depth=all&direction=both&show_instances=true", "map?depth=2"])
def test_queries_do_not_grow_with_the_graph(a, b, api, suffix):
    small, large = mesh(a, 4), mesh(b, 40)
    few = count_queries(api(a["users"]["viewer"]), f"applications/{small.id}/{suffix}")
    many = count_queries(api(b["users"]["viewer"]), f"applications/{large.id}/{suffix}")
    assert few == many, (few, many)
    assert many <= 21  # one more for the capability overrides, read once per request


# ---- a record's map -----------------------------------------------------------------------------------------------


def node(body, resource_or_id):
    nid = str(getattr(resource_or_id, "id", resource_or_id))
    return next(n for n in body["nodes"] if n["id"] == nid)


def row_to(body, resource_or_id):
    nid = str(getattr(resource_or_id, "id", resource_or_id))
    return next(r for r in body["rows"] if r["other"] == nid)


def test_relationships_in_both_directions_read_from_the_record(a, api):
    client = api(a["users"]["viewer"])
    app, needs, needed_by, newer = (
        record(a, "Payroll"),
        record(a, "Database", "component"),
        record(a, "Portal"),
        record(a, "Payroll 2"),
    )
    out = link(a, app, needs)
    inbound = link(a, needed_by, app)
    link(a, newer, app, "replaces")
    body = get(client, f"applications/{app.id}/map")
    assert body["root"] == str(app.id) and body["depth"] == 1 and body["truncated"] is False
    assert node(body, app)["root"] is True and node(body, app)["hop"] == 0
    assert row_to(body, needs)["reads"] == "Depends on"
    assert row_to(body, needed_by)["reads"] == "Is needed by"
    assert row_to(body, newer)["reads"] == "Is replaced by" and row_to(body, newer)["category"] == "relationship"
    edge = next(e for e in body["edges"] if e["id"] == f"relationship:{out.id}")
    assert (edge["source"], edge["target"], edge["relation"], edge["category"]) == (
        str(app.id),
        str(needs.id),
        "depends_on",
        "dependency",
    )
    assert edge["label"] == "Depends on" and edge["reads_inverse"] == "Is needed by"
    assert edge["detail"]["type"]["slug"] == "depends_on" and edge["detail"]["type"]["inverse_name"] == "Is needed by"
    assert edge["detail"]["relationship"] == str(out.id)
    assert next(e for e in body["edges"] if e["id"] == f"relationship:{inbound.id}")["target"] == str(app.id)
    assert body["counts"]["dependency"] == 2 and body["counts"]["relationship"] == 1
    assert [c["key"] for c in body["categories"]] == [
        "dependency",
        "relationship",
        "integration",
        "instance",
        "discovery",
        "contract",  # (money/record_map.py)
        "license_pool",
    ]
    assert (
        node(body, needs)["path"] == f"components/{needs.id}"
        and node(body, needs)["map_path"] == f"components/{needs.id}/map"
    )


def test_a_custom_type_reads_by_its_names(a, api):
    trust = custom_type(a, "SAML trust for", is_dependency=False)
    app, idp = record(a, "Payroll"), record(a, "IdP")
    link(a, app, idp, trust.slug)
    body = get(api(a["users"]["viewer"]), f"applications/{idp.id}/map")
    assert row_to(body, app)["reads"] == "SAML trust for (inverse)"
    assert body["edges"][0]["relation"] == trust.slug and body["edges"][0]["category"] == "relationship"


def test_integrations_are_flow_edges_between_their_ends(a, api):
    client = api(a["users"]["viewer"])
    app, job = record(a, "Payroll"), record(a, "Export job", "component")
    feed = flow(
        a, "Payroll export", app, job, protocol="SFTP", schedule="Nightly", direction="one_way", auth_method="ssh_key"
    )
    back = flow(a, "Sync back", job, app, direction="two_way")
    body = get(client, f"applications/{app.id}/map")
    edge = next(e for e in body["edges"] if e["id"] == f"integration:{feed.id}")
    assert (edge["source"], edge["target"], edge["relation"], edge["label"]) == (
        str(app.id),
        str(job.id),
        "integration",
        "Payroll export",
    )
    assert {k: edge["detail"][k] for k in ("protocol", "schedule", "direction", "auth_method")} == {
        "protocol": "SFTP",
        "schedule": "Nightly",
        "direction": "one_way",
        "auth_method": "ssh_key",
    }
    assert edge["detail"]["integration"]["path"] == f"integrations/{feed.id}"
    rows = {r["edge"]: r for r in body["rows"]}
    assert rows[f"integration:{feed.id}"]["reads"] == "Sends data to"
    assert rows[f"integration:{back.id}"]["reads"] == "Exchanges data with"
    assert len([n for n in body["nodes"] if n["id"] == str(job.id)]) == 1  # one node for both flows
    from_job = get(client, f"components/{job.id}/map")
    assert next(r for r in from_job["rows"] if r["edge"] == f"integration:{feed.id}")["reads"] == "Receives data from"


def test_instances_and_an_implemented_integration_are_leaves(a, api):
    client = api(a["users"]["viewer"])
    app, worker, far = record(a, "Payroll"), record(a, "Worker", "component"), record(a, "Far")
    prod = services.create_instance(a["org"], a["users"]["editor"], app, {"name": "Production", "environment": "prod"})
    feed = flow(a, "Feed", app, far)
    link(a, worker, feed, "implements")
    body = get(client, f"applications/{app.id}/map")
    inst = node(body, prod)
    assert inst["kind"] == "instance" and inst["lifecycle"] is None and inst["expandable"] is False
    assert inst["path"] == f"applications/{app.id}/instances/{prod.id}" and inst["detail"]["environment"] == "prod"
    assert row_to(body, prod)["reads"] == "Has instance"
    worker_map = get(client, f"components/{worker.id}/map?depth=2")
    integration_node = node(worker_map, feed)
    assert integration_node["kind"] == "integration" and integration_node["expandable"] is False
    assert row_to(worker_map, feed)["reads"] == "Implements"
    assert str(far.id) not in {n["id"] for n in worker_map["nodes"]}  # an integration node isn't expanded


def test_discovery_sources_are_on_the_map(a, api):
    connector = discovery.create_connector(
        a["org"], a["users"]["admin"], {"tenant_id": "simulated", "client_id": "", "secret": "synthetic-secret"}
    )
    connections.run_test(
        a["org"], a["users"]["admin"], connector.microsoft_connection
    )  # a run needs a tested connection
    discovery.run(connector)
    editor = api(a["users"]["editor"])
    zoom = Observation.objects.get(connector=connector, display_name="Zoom")
    assert editor.post(f"discovery/observations/{zoom.id}/link", {"resource": str(a["app"].id)}).status_code == 200
    vault = Observation.objects.get(connector=connector, display_name="Contract Vault")
    editor.post(f"discovery/observations/{vault.id}/dismiss", {"reason": "Not ours"})
    body = get(api(a["users"]["viewer"]), f"applications/{a['app'].id}/map")
    source = node(body, zoom)
    assert source["kind"] == "discovery_source" and source["name"] == "Zoom"
    assert source["path"] == f"discovery/observations/{zoom.id}"
    assert source["detail"]["connector"]["name"] == connector.name and source["detail"]["decision"] == "linked"
    assert source["detail"]["external_id"] == zoom.external_id and source["detail"]["gone_at"] is None
    assert row_to(body, zoom)["reads"] == "Discovered in" and row_to(body, zoom)["category"] == "discovery"
    assert str(vault.id) not in {n["id"] for n in body["nodes"]}
    assert "secret" not in json.dumps(body).lower()


def test_archived_and_retired_are_flagged_not_hidden(a, api):
    client = api(a["users"]["viewer"])
    app, old, gone = record(a, "Payroll"), record(a, "Old"), record(a, "Gone", "component")
    link(a, old, app)
    link(a, app, gone)
    feed = flow(a, "Old feed", app, gone)
    services.archive_resource(a["org"], a["users"]["editor"], Resource.objects.get(pk=old.pk))
    retire(a, gone)
    services.archive_resource(a["org"], a["users"]["editor"], Resource.objects.get(pk=feed.pk))
    body = get(client, f"applications/{app.id}/map")
    assert node(body, old)["archived"] is True and node(body, old)["retired"] is False
    assert node(body, gone)["retired"] is True and node(body, gone)["lifecycle"] == "retired"
    edge = next(e for e in body["edges"] if e["id"] == f"integration:{feed.id}")
    assert edge["archived"] is True
    archived_root = get(client, f"applications/{old.id}/map")
    assert node(archived_root, old)["archived"] is True and node(archived_root, app)["hop"] == 1


def test_depth_two_expands_records_and_keeps_edges_between_neighbours(a, api):
    client = api(a["users"]["viewer"])
    app, left, right, far = record(a, "Payroll"), record(a, "Left"), record(a, "Right"), record(a, "Far")
    link(a, left, app)
    link(a, right, app)
    between = link(a, left, right)
    link(a, far, left)
    one = get(client, f"applications/{app.id}/map")
    assert {n["name"] for n in one["nodes"]} == {"Payroll", "Left", "Right"}
    assert f"relationship:{between.id}" not in {e["id"] for e in one["edges"]}  # neighbours aren't expanded at 1
    two = get(client, f"applications/{app.id}/map?depth=2")
    assert {n["name"]: n["hop"] for n in two["nodes"]} == {"Payroll": 0, "Left": 1, "Right": 1, "Far": 2}
    assert f"relationship:{between.id}" in {e["id"] for e in two["edges"]}
    assert len(two["edges"]) == len({e["id"] for e in two["edges"]}) == len(two["rows"]) == 4
    far_row = row_to(two, far)
    assert far_row["near"] == str(left.id) and far_row["hop"] == 2 and far_row["reads"] == "Is needed by"
    assert two["counts"]["dependency"] == 2  # the record's own connections only


@pytest.mark.parametrize("depth", ["0", "3", "all", "x"])
def test_map_depth_is_one_or_two(a, api, depth):
    response = api(a["users"]["viewer"]).get(f"applications/{a['app'].id}/map?depth={depth}")
    assert response.status_code == 400 and response.json_body["errors"] == {"depth": ["Choose 1 or 2."]}


def test_the_map_stops_at_max_nodes(a, api, monkeypatch):
    monkeypatch.setattr(record_map, "MAX_NODES", 5)
    app = record(a, "Hub")
    for i in range(8):
        link(a, record(a, f"Leaf {i}"), app)
    body = get(api(a["users"]["viewer"]), f"applications/{app.id}/map")
    assert len(body["nodes"]) == 5 and body["truncated"] is True and body["max_nodes"] == 5
    assert names(body["nodes"]) == ["Hub", "Leaf 0", "Leaf 1", "Leaf 2", "Leaf 3"]  # by name
    ids = {n["id"] for n in body["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in body["edges"])


def test_no_node_from_another_organization(a, b, api):
    def shape(ctx):
        app, other = record(ctx, "Payroll"), record(ctx, "Database", "component")
        link(ctx, app, other)
        flow(ctx, "Feed", app, other)
        services.create_instance(ctx["org"], ctx["users"]["editor"], app, {"name": "Production"})
        return app

    mine, theirs = shape(a), shape(b)
    body = get(api(a["users"]["viewer"]), f"applications/{mine.id}/map?depth=2")
    other_body = get(api(b["users"]["viewer"]), f"applications/{theirs.id}/map?depth=2")
    assert len(body["nodes"]) == len(other_body["nodes"]) == 3  # same shape: a leak would add nodes
    ours = {str(i) for i in Resource.objects.filter(organization=a["org"]).values_list("id", flat=True)}
    ours |= {str(i) for i in mine.instances.values_list("id", flat=True)}
    assert {n["id"] for n in body["nodes"]} <= ours
    assert not {n["id"] for n in body["nodes"]} & {n["id"] for n in other_body["nodes"]}


def test_a_registered_contributor_joins_the_map(a, api):
    """Adds contracts and licence pools by registering a contributor; nothing else changes."""

    def contracts(org, frontier):
        for rid in frontier:
            node = record_map.other_node(f"c-{rid}", "contract", "Office EA", None, {})
            edge = {
                "id": f"contract:{rid}",
                "source": rid,
                "target": f"c-{rid}",
                "relation": "contract",
                "category": "contract",
                "label": "Covered by",
                "reads": "Covered by",
                "reads_inverse": "Covers",
                "archived": False,
                "retired": False,
                "detail": {},
            }
            yield record_map.Link(rid, f"c-{rid}", edge, node)

    record_map.register(record_map.Contributor("test-contracts", 25, (("contract", "Contracts"),), contracts))
    try:
        body = get(api(a["users"]["viewer"]), f"applications/{a['app'].id}/map")
    finally:
        record_map.CONTRIBUTORS[:] = [c for c in record_map.CONTRIBUTORS if c.name != "test-contracts"]
    assert [c["key"] for c in body["categories"]][2:4] == ["integration", "contract"]
    assert row_to(body, f"c-{a['app'].id}")["reads"] == "Covered by" and body["counts"]["contract"] == 1


def test_roles_any_member_reads_anonymous_is_refused(a, api):
    url = f"applications/{a['app'].id}/map"
    for role in ("viewer", "editor", "admin"):
        assert api(a["users"][role]).get(url).status_code == 200
        assert api(a["users"][role]).get(f"applications/{a['app'].id}/dependencies").status_code == 200
    assert api().get(url).status_code in (401, 403)
    assert api(a["users"]["admin"]).post(url).status_code == 405


def test_openapi_documents_both_routes_for_every_record_kind(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    for prefix in ("applications", "components", "ai-agents"):
        dependencies = doc["paths"][f"/api/v1/{prefix}/{{id}}/dependencies"]["get"]
        assert {"direction", "depth", "include_retired", "show_instances", "data_flows"} <= {
            p["name"] for p in dependencies["parameters"]
        }
        assert "Dependencies" in json.dumps(dependencies["responses"]["200"])
        the_map = doc["paths"][f"/api/v1/{prefix}/{{id}}/map"]["get"]
        assert "RecordMap" in json.dumps(the_map["responses"]["200"])
        assert {"400", "403", "404"} <= set(the_map["responses"])
    schemas = doc["components"]["schemas"]
    assert {"cycles"} <= set(schemas["Dependencies"]["properties"])
    assert {"direction", "auth_method"} <= set(schemas["DependencyFlow"]["properties"])
    assert {"nodes", "edges", "rows", "categories", "truncated"} <= set(schemas["RecordMap"]["properties"])
    assert {"id", "kind", "name", "lifecycle", "archived", "retired", "path"} <= set(schemas["MapNode"]["properties"])
    assert {"id", "source", "target", "relation", "label"} <= set(schemas["MapEdge"]["properties"])


# ---- review follow-ups: counts before the cap, editions without AI agents, cycles past the cap, gated contributors


def test_counts_are_taken_before_the_node_cap(a, api, monkeypatch):
    """Categories rank dependencies first, so with the cap reached the instances and sources don't fit on the map;
    `counts` still reports every connection of the record."""
    monkeypatch.setattr(record_map, "MAX_NODES", 5)
    app = record(a, "Hub")
    for i in range(8):
        link(a, record(a, f"Leaf {i}"), app)
    for name in ("Production", "Sandbox"):
        services.create_instance(a["org"], a["users"]["editor"], app, {"name": name})
    body = get(api(a["users"]["viewer"]), f"applications/{app.id}/map")
    assert body["truncated"] is True and len(body["nodes"]) == 5
    assert not [n for n in body["nodes"] if n["kind"] == "instance"]
    assert body["counts"]["dependency"] == 8 and body["counts"]["instance"] == 2


def test_without_ai_agents_agent_nodes_are_left_out(a, api, monkeypatch):
    from core import capabilities as caps

    app, job, agent = record(a, "Payroll"), record(a, "Job", "component"), record(a, "Helper", "ai_agent")
    link(a, agent, app)
    link(a, job, app)
    link(a, app, agent)
    client = api(a["users"]["viewer"])
    on = get(client, f"applications/{app.id}/map")
    assert str(agent.id) in {n["id"] for n in on["nodes"]}
    monkeypatch.setitem(caps.EDITION_CAPABILITIES, "community", caps.COMMUNITY - {caps.AI_AGENTS})
    off = get(client, f"applications/{app.id}/map?depth=2")
    assert {n["name"] for n in off["nodes"]} == {"Payroll", "Job"}
    assert all(str(agent.id) not in (e["source"], e["target"]) for e in off["edges"])
    assert off["counts"]["dependency"] == 1
    for url in (f"applications/{app.id}/dependencies", f"dependencies?target={app.id}"):
        explored = get(client, f"{url}{'&' if '?' in url else '?'}direction=both&depth=all")
        assert names(explored["nodes"]) == ["Job"] and explored["cycles"] == []
    assert client.get(f"ai-agents/{agent.id}/map").status_code == 403


def test_cycles_are_not_cut_by_the_row_cap(a, api, monkeypatch):
    """R0 <- R1 <- ... <- R5 and R0 depends on R5: the loop closes six links out, past a cap of three rows."""
    monkeypatch.setattr(graph, "MAX_NODES", 3)
    rows = line(a, 6)
    closing = link(a, rows[0], rows[5])
    body = deps(api(a["users"]["viewer"]), rows[0], depth="all")
    assert body["capped"] is True and names(body["nodes"]) == ["R1", "R2", "R3"]
    assert not [n for n in body["nodes"] if n["cycle"]]
    [cycle] = body["cycles"]
    assert cycle["relationship"] == str(closing.id) and cycle["depth"] == 6
    assert cycle["closes_at"] == str(rows[0].id) and cycle["reached_from"] == str(rows[5].id)
    assert cycle["reached_from"] not in {n["id"] for n in body["nodes"]}  # documented: may be past the cap


def test_a_contributor_the_edition_lacks_is_skipped(a, api):
    def never(org, frontier):
        raise AssertionError("a contributor the organization can't use was asked")

    gated = record_map.Contributor(
        "test-gated", 25, (("gated", "Gated"),), never, capability="billing"
    )  # one community lacks
    record_map.register(gated)
    try:
        body = get(api(a["users"]["viewer"]), f"applications/{a['app'].id}/map")
    finally:
        record_map.CONTRIBUTORS[:] = [c for c in record_map.CONTRIBUTORS if c.name != "test-gated"]
    assert "gated" not in [c["key"] for c in body["categories"]] and "gated" not in body["counts"]
