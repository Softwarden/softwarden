"""The dependency explorer endpoint: parameters, errors, shape, and organization scope."""

from catalog import graph, services
from catalog.models import Resource


def record(ctx, name, kind="application", lifecycle="active"):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": lifecycle}
    )


def depends(ctx, source, target, **extra):
    """source depends on target."""
    return services.create_relationship(
        ctx["org"],
        ctx["users"]["editor"],
        {"type": "depends_on", "source": str(source.id), "target": str(target.id), **extra},
    )


def flow(ctx, name, source, target, **extra):
    return services.create_integration(
        ctx["org"],
        ctx["users"]["editor"],
        {"name": name, "lifecycle": "active", "source": str(source.id), "target": str(target.id), **extra},
    )


def implements(ctx, component, integration):
    return services.create_relationship(
        ctx["org"],
        ctx["users"]["editor"],
        {"type": "implements", "source": str(component.id), "target": str(integration.id)},
    )


def explore(client, target, **params):
    query = "&".join([f"target={target.id}", *(f"{k}={v}" for k, v in params.items())])
    response = client.get(f"dependencies?{query}")
    assert response.status_code == 200, response.json_body
    return response.json_body


def names(rows):
    return [r["name"] for r in rows]


def chain(ctx):
    """Core <- Mid <- Top: Mid depends on Core, Top depends on Mid."""
    core, mid, top = record(ctx, "Core"), record(ctx, "Mid", "component"), record(ctx, "Top")
    depends(ctx, mid, core)
    depends(ctx, top, mid)
    return core, mid, top


def test_parameters_map_to_the_traversal(a, api):
    client = api(a["users"]["viewer"])
    core, mid, top = chain(a)
    flow(a, "Core feed", core, record(a, "Far"))
    assert names(explore(client, core)["nodes"]) == ["Mid"]  # defaults: dependents, depth 1
    assert explore(client, core)["direction"] == "dependents" and explore(client, core)["depth"] == "1"
    assert names(explore(client, core, depth=2)["nodes"]) == ["Mid", "Top"]
    assert names(explore(client, top, direction="depends_on", depth="all")["nodes"]) == ["Mid", "Core"]
    assert sorted(names(explore(client, mid, direction="both")["nodes"])) == ["Core", "Top"]
    assert explore(client, core, data_flows="false")["data_flows"] == []
    assert names(explore(client, core)["data_flows"]) == ["Core feed"]
    archived = Resource.objects.get(id=mid.id)
    services.update_resource(
        a["org"],
        a["users"]["editor"],
        archived,
        {"lifecycle": "retired", "retirement_ack": True, "retirement_ack_reason": "Test setup"},
    )
    assert explore(client, core)["nodes"] == []
    assert names(explore(client, core, include_retired="true")["nodes"]) == ["Mid"]
    assert explore(client, core, depth=2, show_instances="true")["show_instances"] is True


def test_the_response_is_what_the_traversal_returns(a, api):
    core, mid, top = chain(a)
    flow(a, "Core feed", core, record(a, "Far"))
    body = explore(api(a["users"]["viewer"]), core, direction="both", depth="all")
    assert body == graph.traverse(a["org"], core, "both", None)
    assert set(body) == {
        "target",
        "direction",
        "depth",
        "include_retired",
        "show_instances",
        "nodes",
        "data_flows",
        "total",
        "shown",
        "capped",
        "summary",
        "cycles",
    }


# ---- validation ---------------------------------------------------------------------------------------


def test_bad_parameters_are_field_errors(a, api):
    client = api(a["users"]["viewer"])
    app = a["app"].id
    assert client.get("dependencies").json_body["errors"] == {
        "target": ["Choose an application, component or AI agent."]
    }
    assert "target" in client.get("dependencies?target=nonsense").json_body["errors"]
    response = client.get(f"dependencies?target={app}&direction=sideways&depth=11&include_retired=maybe")
    assert response.status_code == 400 and response["Content-Type"] == "application/problem+json"
    assert set(response.json_body["errors"]) == {"direction", "depth", "include_retired"}
    feed = flow(a, "Feed", a["app"], record(a, "Job", "component"))
    integration_target = client.get(f"dependencies?target={feed.id}")
    assert integration_target.status_code == 400 and "target" in integration_target.json_body["errors"]


# ---- cross-tenant -------------------------------------------------------------------------------------


def mirror(ctx):
    """The same shape in each organization: records with the same names, links, a cycle, a flow and an implementer."""
    core, mid, top = chain(ctx)
    depends(ctx, core, top)
    feed = flow(ctx, "Feed", mid, record(ctx, "Far"))
    implements(ctx, record(ctx, "Worker", "component"), feed)
    return core


def test_another_organizations_records_never_appear_as_nodes(a, b, api):
    core_a, core_b = mirror(a), mirror(b)
    mine = explore(api(a["users"]["viewer"]), core_a, direction="both", depth="all")
    theirs = explore(api(b["users"]["viewer"]), core_b, direction="both", depth="all")
    assert names(mine["nodes"]) == names(theirs["nodes"])  # the same shape, so a leak would add nodes
    assert mine["summary"] == theirs["summary"] and len(mine["data_flows"]) == len(theirs["data_flows"]) == 1
    ours = {str(i) for i in Resource.objects.filter(organization=a["org"]).values_list("id", flat=True)}
    seen = (
        {mine["target"]["id"]} | {n["id"] for n in mine["nodes"]} | {s["id"] for n in mine["nodes"] for s in n["path"]}
    )
    seen |= {f["id"] for f in mine["data_flows"]} | {i["id"] for f in mine["data_flows"] for i in f["implemented_by"]}
    seen |= {end["id"] for f in mine["data_flows"] for end in (f["source"], f["target"], f["attached_to"])}
    assert seen <= ours and not seen & {n["id"] for n in theirs["nodes"]}


def test_another_organizations_target_is_404(a, b, api):
    core_b = mirror(b)
    client = api(a["users"]["admin"])
    assert client.get(f"dependencies?target={core_b.id}").status_code == 404
    assert client.get(f"dependencies?target={core_b.id}&direction=both&depth=all").status_code == 404
