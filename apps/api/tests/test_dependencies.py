"""Dependency traversal: directions, depth, cycles, retired, data flows, the cap."""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from catalog import graph, services
from catalog.models import Relationship, Resource
from tests.conftest import rel_type


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


def explore(ctx, target, direction="dependents", depth=1, **options):
    """graph.traverse for the context's organization; depth "all" is None, and "true"/"false" are booleans."""
    options = {k: v == "true" if isinstance(v, str) else v for k, v in options.items()}
    return graph.traverse(ctx["org"], target, direction, None if depth == "all" else depth, **options)


def names(rows):
    return [r["name"] for r in rows]


def chain(ctx):
    """Core <- Mid <- Top: Mid depends on Core, Top depends on Mid."""
    core, mid, top = record(ctx, "Core"), record(ctx, "Mid", "component"), record(ctx, "Top")
    depends(ctx, mid, core)
    depends(ctx, top, mid)
    return core, mid, top


# ---- directions and depth ---------------------------------------------------------------------------------


def test_dependents_by_depth(a):
    core, mid, top = chain(a)
    one = explore(a, core)  # defaults: dependents, depth 1
    assert names(one["nodes"]) == ["Mid"] and one["nodes"][0]["depth"] == 1 and one["direction"] == "dependents"
    two = explore(a, core, depth=2)
    assert [(n["name"], n["depth"]) for n in two["nodes"]] == [("Mid", 1), ("Top", 2)]
    assert explore(a, core, depth="all")["nodes"] == two["nodes"]
    assert explore(a, top, depth="all")["nodes"] == []  # nothing depends on Top


def test_depends_on_and_both(a):
    core, mid, top = chain(a)
    down = explore(a, top, direction="depends_on", depth="all")
    assert [(n["name"], n["depth"], n["direction"]) for n in down["nodes"]] == [
        ("Mid", 1, "depends_on"),
        ("Core", 2, "depends_on"),
    ]
    both = explore(a, mid, direction="both", depth="all")
    assert sorted((n["name"], n["direction"]) for n in both["nodes"]) == [("Core", "depends_on"), ("Top", "dependents")]
    assert both["target"]["name"] == "Mid" and both["depth"] == "all"


def test_only_depends_on_links_are_followed(a):
    old, new = record(a, "Old"), record(a, "New")
    services.create_relationship(
        a["org"], a["users"]["editor"], {"type": "replaces", "source": str(new.id), "target": str(old.id)}
    )
    feed = flow(a, "Feed", new, record(a, "Elsewhere"))
    implements(a, record(a, "Worker", "component"), feed)
    assert explore(a, old, depth="all")["nodes"] == []
    assert explore(a, new, depth="all")["nodes"] == []


# ---- paths, shortest first, cycles ----------------------------------------------------------------------


def test_paths_run_from_the_target_and_name_each_link(a):
    core, mid, top = chain(a)
    node = explore(a, core, depth=2)["nodes"][1]
    assert [s["name"] for s in node["path"]] == ["Core", "Mid", "Top"]
    assert node["path"][0]["via"] is None and node["path"][2]["via"]["type"] == "depends_on"
    assert node["link"]["relationship"] == node["path"][2]["via"]["relationship"]
    down = explore(a, top, direction="depends_on", depth=2)["nodes"][1]
    assert [s["name"] for s in down["path"]] == ["Top", "Mid", "Core"]


def test_the_shortest_path_wins_and_each_record_appears_once(a):
    core, mid, top = chain(a)
    depends(a, top, core)  # Top is also a direct dependent
    nodes = explore(a, core, depth="all")["nodes"]
    assert [(n["name"], n["depth"]) for n in nodes] == [("Mid", 1), ("Top", 1)]
    assert [s["name"] for s in nodes[1]["path"]] == ["Core", "Top"]


def test_a_diamond_is_not_a_cycle(a):
    core, left, right, base = record(a, "Core"), record(a, "Left"), record(a, "Right"), record(a, "Base")
    for middle in (left, right):
        depends(a, middle, core)
        depends(a, base, middle)
    result = explore(a, core, depth="all")
    assert [(n["name"], n["depth"], n["cycle"]) for n in result["nodes"]] == [
        ("Left", 1, False),
        ("Right", 1, False),
        ("Base", 2, False),
    ]
    assert [s["name"] for s in result["nodes"][2]["path"]] == ["Core", "Left", "Base"]  # the first by name
    assert result["summary"]["cycles"] == 0


def test_a_cycle_back_to_the_target_is_marked_and_not_entered_again(a):
    core, mid, top = chain(a)
    depends(a, core, top)  # Core depends on Top: Core -> Top -> Mid -> Core
    result = explore(a, core, depth="all")
    assert [(n["name"], n["depth"], n["cycle"]) for n in result["nodes"]] == [
        ("Mid", 1, False),
        ("Top", 2, False),
        ("Core", 3, True),
    ]
    marker = result["nodes"][2]
    assert [s["name"] for s in marker["path"]] == ["Core", "Mid", "Top", "Core"]
    assert result["summary"]["cycles"] == 1 and result["total"] == 3
    # depth 2 stops before the loop closes
    assert not any(n["cycle"] for n in explore(a, core, depth=2)["nodes"])


def test_a_cycle_that_does_not_include_the_target(a):
    core, x, y = record(a, "Core"), record(a, "X"), record(a, "Y")
    depends(a, x, core)
    depends(a, y, x)
    depends(a, x, y)  # X and Y depend on each other
    result = explore(a, core, depth="all")
    assert [(n["name"], n["depth"], n["cycle"]) for n in result["nodes"]] == [
        ("X", 1, False),
        ("Y", 2, False),
        ("X", 3, True),
    ]
    assert [s["name"] for s in result["nodes"][2]["path"]] == ["Core", "X", "Y", "X"]


def test_a_loop_between_branches_is_a_cycle_shown_once(a):
    """A and B both depend on Root and on each other. Each reaches the other by its own branch, so the
    old walk skipped both cross links as diamonds and showed no loop."""
    root, left, right = record(a, "Root"), record(a, "A"), record(a, "B")
    for side in (left, right):
        depends(a, side, root)
    depends(a, left, right)
    depends(a, right, left)
    result = explore(a, root, depth="all")
    assert [(n["name"], n["depth"], n["cycle"]) for n in result["nodes"]] == [
        ("A", 1, False),
        ("B", 1, False),
        ("B", 2, True),
    ]
    assert [s["name"] for s in result["nodes"][2]["path"]] == ["Root", "A", "B"]
    assert result["summary"]["cycles"] == 1
    # The same loop seen from the other direction, and a depth that stops before it.
    up = explore(a, left, direction="depends_on", depth="all")
    assert up["summary"]["cycles"] == 1
    assert explore(a, root, depth=1)["summary"]["cycles"] == 0


def test_a_retired_record_doesnt_close_a_hidden_loop(a):
    root, left, gone, right = record(a, "Root"), record(a, "A"), record(a, "Gone", lifecycle="retired"), record(a, "B")
    for side in (left, right):
        depends(a, side, root)
    depends(a, gone, left)  # A <- Gone <- B <- ... the loop runs through a retired record
    depends(a, right, gone)
    depends(a, left, right)
    assert explore(a, root, depth="all")["summary"]["cycles"] == 0
    assert explore(a, root, depth="all", include_retired=True)["summary"]["cycles"] == 1


def test_both_directions_through_a_loop_with_the_target(a):
    core, other = record(a, "Core"), record(a, "Other")
    depends(a, other, core)
    depends(a, core, other)
    both = explore(a, core, direction="both", depth="all")
    assert sorted((n["name"], n["direction"], n["cycle"]) for n in both["nodes"]) == [
        ("Core", "dependents", True),
        ("Core", "depends_on", True),
        ("Other", "dependents", False),
        ("Other", "depends_on", False),
    ]


def test_a_record_linked_to_itself_through_two_instances_is_a_cycle_marker(a):
    prod = services.create_instance(a["org"], a["users"]["editor"], a["app"], {"name": "Production"})
    sandbox = services.create_instance(a["org"], a["users"]["editor"], a["app"], {"name": "Sandbox"})
    depends(a, a["app"], a["app"], source_instance=str(sandbox.id), target_instance=str(prod.id))
    result = explore(a, a["app"], depth="all", show_instances="true")
    assert [(n["name"], n["cycle"], n["depth"]) for n in result["nodes"]] == [("orga App", True, 1)]
    step = result["nodes"][0]["path"][1]
    assert step["instance"] == "Sandbox" and step["previous_instance"] == "Production"


# ---- retired, archived, instances -----------------------------------------------------------------------


def test_retired_records_are_left_out_and_not_walked_through_unless_included(a):
    core = record(a, "Core")
    old = record(a, "Old", lifecycle="retired")
    beyond = record(a, "Beyond")
    depends(a, old, core)
    depends(a, beyond, old)
    hidden = explore(a, core, depth="all")
    assert hidden["nodes"] == [] and hidden["summary"]["hidden_retired"] == 1
    shown = explore(a, core, depth="all", include_retired="true")
    assert [(n["name"], n["lifecycle"]) for n in shown["nodes"]] == [("Old", "retired"), ("Beyond", "active")]
    assert shown["include_retired"] is True and shown["summary"]["hidden_retired"] == 0


def test_archived_records_stay_and_are_flagged(a, api):
    client = api(a["users"]["editor"])
    core, mid, top = chain(a)
    client.post(f"components/{mid.id}/archive")
    nodes = explore(a, core, depth=2)["nodes"]
    assert [(n["name"], n["archived"]) for n in nodes] == [("Mid", True), ("Top", False)]


def test_instances_are_shown_only_when_asked(a):
    prod = services.create_instance(a["org"], a["users"]["editor"], a["app"], {"name": "Production"})
    job = record(a, "Job", "component")
    depends(a, job, a["app"], target_instance=str(prod.id))
    plain = explore(a, a["app"])["nodes"][0]["path"]
    assert all("instance" not in step for step in plain)
    detailed = explore(a, a["app"], show_instances="true")["nodes"][0]["path"]
    assert detailed[1]["instance"] is None and detailed[1]["previous_instance"] == "Production"


def test_owner_gaps_and_the_technical_owner_are_on_each_node(a):
    core, mid, top = chain(a)
    services.update_resource(a["org"], a["users"]["editor"], mid, {"technical_owner": str(a["people"]["dana"].id)})
    nodes = explore(a, core, depth=2)["nodes"]
    assert nodes[0]["technical_owner"] == {"id": str(a["people"]["dana"].id), "name": "Dana Ortiz", "active": True}
    assert nodes[1]["technical_owner"] is None and "No technical owner" in nodes[1]["gaps"]


# ---- link state on the rows -----------------------------------------------------------------------------


def test_each_row_carries_the_state_of_the_link_it_was_reached_by(a, api):
    editor = api(a["users"]["editor"])
    core, mid, top = chain(a)
    body = {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Email"}
    rel = [r for r in editor.get(f"relationships?resource={mid.id}").json_body if r["source"]["name"] == "Mid"][0]
    editor.post(f"relationships/{rel['id']}/confirmations", body)
    nodes = explore(a, core, depth=2)["nodes"]
    assert [(n["name"], n["link"]["state"]) for n in nodes] == [("Mid", "confirmed"), ("Top", "unconfirmed")]
    assert nodes[0]["link"]["last_confirmed_on"] == services.org_today(a["org"]).isoformat()
    # moving the source's owner makes the confirmed link need verification, and the summary counts it
    services.update_resource(a["org"], a["users"]["editor"], mid, {"technical_owner": str(a["people"]["dana"].id)})
    again = explore(a, core, depth=2)
    assert again["nodes"][0]["link"] == {
        "relationship": rel["id"],
        "state": "needs_verification",
        "why": "Owner changed since confirmation.",
        "last_confirmed_on": nodes[0]["link"]["last_confirmed_on"],
    }
    assert again["summary"]["needs_verification"] == 1 and again["summary"]["unconfirmed"] == 1


# ---- integrations: one hop, with implementers ---------------------------------------------------------


def test_integrations_are_listed_one_hop_out_with_their_implementers(a):
    core, mid, top = chain(a)
    far = record(a, "Far away")
    in_feed = flow(a, "Core to Far", core, far, protocol="SFTP", schedule="Nightly")
    flow(a, "Top to Far", top, far)
    worker = record(a, "Worker", "component")
    implements(a, worker, in_feed)
    unrelated = flow(a, "Far to Far2", far, record(a, "Far2"))
    result = explore(a, core, depth=2)
    assert names(result["data_flows"]) == ["Core to Far", "Top to Far"]  # none for Far: it isn't listed
    first, second = result["data_flows"]
    assert first["hop"] == 1 and first["attached_to"]["name"] == "Core" and first["depth"] == 1
    assert first["implemented_by"] == [
        {"id": str(worker.id), "name": "Worker", "lifecycle": "active", "archived": False}
    ]
    assert first["vendor_native"] is False and first["protocol"] == "SFTP" and first["link"]["state"] == "unconfirmed"
    assert second["vendor_native"] is True and second["attached_to"]["name"] == "Top" and second["depth"] == 3
    assert (first["source"]["name"], first["target"]["name"]) == ("Core", "Far away")
    # one hop only: Far's own integration isn't followed, and the integrations are not nodes
    assert str(unrelated.id) not in {f["id"] for f in result["data_flows"]}
    assert names(result["nodes"]) == ["Mid", "Top"]
    # depth 1 lists only the flows of the records within reach
    assert names(explore(a, core)["data_flows"]) == ["Core to Far"]
    assert explore(a, core, flows=False)["data_flows"] == []
    assert result["summary"]["data_flows"] == 2


def test_an_integration_between_two_listed_records_is_listed_once_at_the_nearest(a):
    core, mid, top = chain(a)
    flow(a, "Top to Core", top, core)
    flows = explore(a, core, depth="all")["data_flows"]
    assert names(flows) == ["Top to Core"] and flows[0]["attached_to"]["name"] == "Core"


def test_retired_integrations_follow_the_include_retired_toggle(a):
    core, far = record(a, "Core"), record(a, "Far")
    feed = flow(a, "Old feed", core, far)
    services.update_resource(a["org"], a["users"]["editor"], feed, {"lifecycle": "retired"})
    hidden = explore(a, core)
    assert hidden["data_flows"] == [] and hidden["summary"]["hidden_retired"] == 1
    assert names(explore(a, core, include_retired="true")["data_flows"]) == ["Old feed"]


def test_target_with_no_links_is_empty(a):
    result = explore(a, a["app"], direction="both", depth="all")
    assert result["nodes"] == [] and result["data_flows"] == [] and result["total"] == 0 and not result["capped"]


# ---- the 150-node cap ---------------------------------------------------------------------------------


def test_the_cap_keeps_the_nearest_150_and_reports_the_full_count(a):
    core = record(a, "Core")
    for i in range(160):
        depends(a, record(a, f"Fan {i:03d}"), core)
    result = explore(a, core)
    assert result["capped"] is True and result["total"] == 160 and result["shown"] == 150
    assert len(result["nodes"]) == 150 and result["summary"]["nodes"] == 160
    assert names(result["nodes"])[:2] == ["Fan 000", "Fan 001"]
    # flows share the cap and come after the records at the same depth
    flow(a, "Core feed", core, record(a, "Elsewhere"))
    result = explore(a, core)
    assert result["total"] == 161 and result["shown"] == 150 and result["data_flows"] == []


def test_below_the_cap_nothing_is_dropped(a):
    core = record(a, "Core")
    for i in range(10):
        depends(a, record(a, f"Fan {i}"), core)
    result = explore(a, core)
    assert result["capped"] is False and result["total"] == result["shown"] == 10


# ---- queries do not grow with the graph (no N+1) -------------------------------------------------------


def count_queries(org, target, **kw):
    with CaptureQueriesContext(connection) as queries:
        graph.traverse(org, target, **kw)
    return len(queries)


def test_the_number_of_queries_does_not_depend_on_the_size_of_the_graph(a):
    core = record(a, "Core")
    previous = core
    for i in range(3):
        node = record(a, f"Small {i}")
        depends(a, node, previous)
        previous = node
    flow(a, "Small feed", core, previous)
    small = count_queries(a["org"], core, direction="both", depth=None, show_instances=True)
    for i in range(40):
        node = record(a, f"Big {i}")
        depends(a, node, previous)
        depends(a, core, node)  # loops back, so there are cycle rows too
        flow(a, f"Big feed {i}", node, core)
        implements(a, record(a, f"Impl {i}", "component"), flow(a, f"Impl feed {i}", node, core))
        previous = node
    assert count_queries(a["org"], core, direction="both", depth=None, show_instances=True) == small
    assert small <= 8


def test_a_long_chain_is_walked_to_the_end(a):
    core = record(a, "Core")
    previous = core
    for i in range(120):
        node = record(a, f"Link {i:03d}")
        depends(a, node, previous)
        previous = node
    result = graph.traverse(a["org"], core, "dependents", None)
    assert result["total"] == 120 and not result["capped"] and result["nodes"][-1]["depth"] == 120
    assert len(result["nodes"][-1]["path"]) == 121


# ---- organization scope at every hop -------------------------------------------------------------------------


def mirror(ctx):
    """The same shape in each organization: records with the same names, links, a cycle, a flow and an implementer."""
    core, mid, top = chain(ctx)
    depends(ctx, core, top)
    feed = flow(ctx, "Feed", mid, record(ctx, "Far"))
    implements(ctx, record(ctx, "Worker", "component"), feed)
    return core


def test_another_organizations_records_never_appear_as_nodes(a, b):
    core_a, core_b = mirror(a), mirror(b)
    mine = explore(a, core_a, direction="both", depth="all")
    theirs = explore(b, core_b, direction="both", depth="all")
    assert names(mine["nodes"]) == names(theirs["nodes"])  # the same shape, so a leak would add nodes
    assert mine["summary"] == theirs["summary"] and len(mine["data_flows"]) == len(theirs["data_flows"]) == 1
    ours = {str(i) for i in Resource.objects.filter(organization=a["org"]).values_list("id", flat=True)}
    seen = (
        {mine["target"]["id"]} | {n["id"] for n in mine["nodes"]} | {s["id"] for n in mine["nodes"] for s in n["path"]}
    )
    seen |= {f["id"] for f in mine["data_flows"]} | {i["id"] for f in mine["data_flows"] for i in f["implemented_by"]}
    seen |= {end["id"] for f in mine["data_flows"] for end in (f["source"], f["target"], f["attached_to"])}
    assert seen <= ours and not seen & {n["id"] for n in theirs["nodes"]}


def test_the_walk_refuses_a_target_from_another_organization(a, b):
    core_b = record(b, "Core")
    with pytest.raises(ValueError, match="of the organization"):
        explore(a, core_b)
    with pytest.raises(ValueError, match="direction"):
        explore(a, a["app"], direction="sideways")
    feed = flow(a, "Feed", a["app"], record(a, "Job", "component"))
    with pytest.raises(ValueError, match="application, component or AI agent"):
        explore(a, feed)


def test_the_walk_drops_links_that_cross_organizations(a, b):
    # The database forbids these rows (composite foreign keys, checked when a transaction commits, which a test
    # never does); the walk must also ignore one it is handed, in both directions and whoever's row it is.
    core_a, core_b = record(a, "Core"), record(b, "Core")
    crossing = [
        Relationship(organization=a["org"], type=rel_type(a["org"], "depends_on"), source=core_b, target=core_a),
        Relationship(organization=a["org"], type=rel_type(a["org"], "depends_on"), source=core_a, target=core_b),
        Relationship(organization=b["org"], type=rel_type(b["org"], "depends_on"), source=core_a, target=core_b),
        Relationship(organization=b["org"], type=rel_type(b["org"], "depends_on"), source=core_b, target=core_a),
    ]
    Relationship.objects.bulk_create(crossing)
    try:
        for ctx, target in ((a, core_a), (b, core_b)):
            found = explore(ctx, target, direction="both", depth="all")
            assert found["nodes"] == [] and found["total"] == 0
    finally:  # the deferred foreign keys are checked when the test's transaction ends
        Relationship.objects.filter(id__in=[r.id for r in crossing]).delete()
