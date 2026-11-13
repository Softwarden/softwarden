"""Relationship types: the per-organization model with its locked core types,
seeding (new organizations and migration 0033), the Relationship.type foreign key (migration 0034), the Admin-only
service and API, traversal by `is_dependency`, and tenancy."""

import importlib
import threading
import time

import pytest
from django.apps import apps as django_apps
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test.utils import CaptureQueriesContext

from catalog import assessments, graph, relationship_types, services
from catalog.models import AssessmentSnapshot, Relationship, RelationshipType
from core.models import AuditEvent, Organization
from core.problems import Conflict, FieldErrors
from tests.conftest import rel_type


def record(ctx, name, kind="application"):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": "active"}
    )


def link(ctx, rel_type_slug, source, target, **extra):
    return services.create_relationship(
        ctx["org"],
        ctx["users"]["editor"],
        {"type": rel_type_slug, "source": str(source.id), "target": str(target.id), **extra},
    )


def add_type(ctx, **data):
    return relationship_types.create_type(ctx["org"], ctx["users"]["admin"], {"name": "Runs on", **data})


# ---- model and seeding ----------------------------------------------------------------------------------------------


def test_every_new_organization_gets_the_core_types(db):
    org = Organization.objects.create(name="Fresh (synthetic)")
    rows = {t.slug: t for t in RelationshipType.objects.filter(organization=org)}
    assert set(rows) == {"depends_on", "implements", "replaces"}
    assert all(t.is_core for t in rows.values())
    assert {s for s, t in rows.items() if t.is_dependency} == {"depends_on"}
    assert (rows["depends_on"].name, rows["depends_on"].inverse_name) == ("Depends on", "Is needed by")
    assert all(t.inverse_name for t in rows.values())
    assert relationship_types.seed(org) == 0  # idempotent
    assert not AuditEvent.objects.filter(organization=org).exists()  # part of creating the organization


def test_the_migration_seeded_existing_organizations(a):
    migration = importlib.import_module("catalog.migrations.0033_relationship_types")
    Relationship.objects.filter(organization=a["org"]).delete()
    RelationshipType.objects.filter(organization=a["org"]).delete()
    migration.seed_existing(django_apps, None)
    seeded = RelationshipType.objects.filter(organization=a["org"])
    assert sorted(seeded.values_list("slug", flat=True)) == ["depends_on", "implements", "replaces"]
    # The migration's frozen copy matches what new organizations get today.
    assert [row[:5] for row in migration.CORE_TYPES] == [row[:5] for row in relationship_types.CORE_TYPES]


def test_slug_and_name_are_unique_per_organization(a, b):
    with pytest.raises(IntegrityError), transaction.atomic():
        RelationshipType.objects.create(organization=a["org"], slug="depends_on", name="Something else")
    with pytest.raises(IntegrityError), transaction.atomic():
        RelationshipType.objects.create(organization=a["org"], slug="other", name="DEPENDS ON")
    # Another organization has its own list.
    RelationshipType.objects.create(organization=b["org"], slug="runs_on", name="Runs on")
    RelationshipType.objects.create(organization=a["org"], slug="runs_on", name="Runs on")


def test_a_type_in_use_is_protected_in_the_database(a):
    two = record(a, "Two")
    rel = link(a, "depends_on", a["app"], two)
    with pytest.raises(IntegrityError), transaction.atomic():
        rel.type.delete()


def test_the_database_rejects_another_organizations_type(a, b):
    two = record(a, "Two")
    with pytest.raises(IntegrityError), transaction.atomic():
        Relationship.objects.create(organization=a["org"], type=rel_type(b["org"]), source=a["app"], target=two)
        connection.check_constraints()  # the composite key is deferred; force the check


# ---- services -------------------------------------------------------------------------------------------------------


def test_create_derives_the_slug_and_audits(a):
    rtype = add_type(a, inverse_name="Hosts", is_dependency=True, description="Where it runs.")
    assert (rtype.slug, rtype.is_core, rtype.is_dependency) == ("runs_on", False, True)
    event = AuditEvent.objects.get(action="relationship_type.created", target_id=rtype.id)
    assert event.changes["slug"] == "runs_on" and event.changes["is_dependency"] is True
    assert event.postchange is not None and event.prechange is None
    assert event.postchange["inverse_name"] == "Hosts"


@pytest.mark.parametrize(
    "data,field",
    [
        ({"name": ""}, "name"),
        ({"name": "depends ON"}, "name"),
        ({"slug": "Bad Slug"}, "slug"),
        ({"slug": "1abc"}, "slug"),
        ({"slug": "depends_on"}, "slug"),
        ({"name": "Replaces!"}, "name"),  # the derived slug, replaces, is taken: the error is on what was sent
        ({"slug": "x" * 51}, "slug"),
        ({"name": "x" * 101}, "name"),
        ({"inverse_name": "x" * 101}, "inverse_name"),
        ({"description": "x" * 201}, "description"),
        ({"is_dependency": "yes"}, "is_dependency"),
        ({"is_core": True}, "is_core"),
    ],
)
def test_create_validates(a, data, field):
    with pytest.raises(FieldErrors) as e:
        add_type(a, **data)
    assert field in e.value.errors


def test_a_core_type_keeps_its_slug_and_dependency_flag_but_labels_can_change(a):
    depends = rel_type(a["org"])
    for data, field in (({"slug": "needs"}, "slug"), ({"is_dependency": False}, "is_dependency")):
        with pytest.raises(FieldErrors) as e:
            relationship_types.update_type(a["org"], a["users"]["admin"], depends, data)
        assert field in e.value.errors
    changed = relationship_types.update_type(
        a["org"],
        a["users"]["admin"],
        depends,
        {"name": "Needs", "inverse_name": "Is used by", "description": "Changed.", "slug": "depends_on"},
    )
    assert (changed.slug, changed.name, changed.inverse_name, changed.is_dependency) == (
        "depends_on",
        "Needs",
        "Is used by",
        True,
    )
    event = AuditEvent.objects.filter(action="relationship_type.updated").latest("created_at")
    assert event.changes["name"] == ["Depends on", "Needs"] and event.changes["inverse_name"] == [
        "Is needed by",
        "Is used by",
    ]


def test_an_added_types_slug_never_changes_but_its_dependency_flag_can(a):
    rtype = add_type(a)
    with pytest.raises(FieldErrors):
        relationship_types.update_type(a["org"], a["users"]["admin"], rtype, {"slug": "hosted_on"})
    rtype = relationship_types.update_type(a["org"], a["users"]["admin"], rtype, {"is_dependency": True})
    assert rtype.is_dependency is True


def test_a_core_type_can_t_be_deleted(a):
    with pytest.raises(Conflict) as e:
        relationship_types.delete_type(a["org"], a["users"]["admin"], rel_type(a["org"], "replaces"))
    assert "core" in str(e.value.detail)
    assert RelationshipType.objects.filter(organization=a["org"]).count() == 3


def test_deleting_a_type_in_use_is_refused_with_the_count(a):
    rtype = add_type(a)
    two, three = record(a, "Two"), record(a, "Three")
    link(a, "runs_on", a["app"], two)
    link(a, "runs_on", a["app"], three)
    with pytest.raises(Conflict) as e:
        relationship_types.delete_type(a["org"], a["users"]["admin"], rtype)
    assert "2 relationships use it" in str(e.value.detail)
    assert e.value.extra["blocking_references"] == {"relationships": 2}
    Relationship.objects.filter(type=rtype).delete()
    relationship_types.delete_type(a["org"], a["users"]["admin"], rtype)
    assert not RelationshipType.objects.filter(id=rtype.id).exists()
    event = AuditEvent.objects.get(action="relationship_type.deleted")
    assert event.changes == {"slug": "runs_on", "name": "Runs on"} and event.postchange is None


def test_a_derived_slug_clash_is_reported_on_the_name(a):
    with pytest.raises(FieldErrors) as e:
        add_type(a, name="Replaces!")
    assert "slug" not in e.value.errors and "replaces" in e.value.errors["name"][0]


def test_an_update_that_changes_nothing_writes_no_event(a):
    rtype = add_type(a, inverse_name="Hosts")
    relationship_types.update_type(a["org"], a["users"]["admin"], rtype, {"name": "Runs on", "inverse_name": "Hosts"})
    assert not AuditEvent.objects.filter(action="relationship_type.updated").exists()


@pytest.mark.parametrize(
    "name,expected",
    [("Depends on", "depends on"), ("SAML trust for", "SAML trust for"), ("A", "a"), ("", "runs on")],
)
def test_a_link_reads_from_the_types_name(name, expected):
    assert relationship_types.phrase_of(name, "runs_on") == expected


def test_a_renamed_core_type_reads_the_same_in_the_audit_log_and_the_digest(a):
    from catalog.audit_log import Names
    from notify.digest import _line

    depends = rel_type(a["org"])
    relationship_types.update_type(a["org"], a["users"]["admin"], depends, {"name": "Needs"})
    rel = link(a, "depends_on", a["app"], record(a, "Two"))
    event = AuditEvent.objects.get(action="relationship.created", target_id=rel.id)
    assert Names(a["org"], [event]).link_phrase("depends_on") == "needs"
    row = {
        "link_kind": "relationship",
        "type": "depends_on",
        "type_name": "Needs",
        "source": {"name": "A"},
        "target": {"name": "B"},
        "why": "Never confirmed.",
    }
    assert _line("links_to_verify", row).startswith("A needs B")


def test_a_link_locks_its_type_until_it_commits(a):
    """The link's foreign key is checked at commit; the type it uses is locked FOR KEY SHARE so a delete can't slip
    in between (the threaded race is in test_a_delete_that_commits_first_makes_the_link_a_field_error)."""
    with CaptureQueriesContext(connection) as queries:
        link(a, "depends_on", a["app"], record(a, "Two"))
    assert any("FOR KEY SHARE" in q["sql"] and "catalog_relationshiptype" in q["sql"] for q in queries.captured_queries)


@pytest.fixture
def race_org(transactional_db):
    from tests.conftest import make_org

    return make_org("typerace")


def test_a_delete_that_commits_first_makes_the_link_a_field_error(race_org):
    f = race_org
    rtype = add_type(f)
    two = record(f, "Two")
    locked, done = threading.Event(), threading.Event()
    outcome: dict = {}

    def delete():
        try:
            with transaction.atomic():
                relationship_types.delete_type(f["org"], f["users"]["admin"], rtype)
                locked.set()
                time.sleep(1.0)  # the link's request arrives while the delete holds the row
        finally:
            done.set()
            connection.close()

    def create():
        try:
            locked.wait(10)
            with transaction.atomic():
                link(f, "runs_on", f["app"], two)
        except Exception as e:  # collected for the assertions
            outcome["error"] = e
        finally:
            connection.close()

    threads = [threading.Thread(target=delete), threading.Thread(target=create)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert done.is_set()
    assert isinstance(outcome.get("error"), FieldErrors) and "type" in outcome["error"].errors
    assert not Relationship.objects.filter(organization=f["org"]).exists()


def test_a_link_that_commits_first_makes_the_delete_a_409(race_org):
    f = race_org
    rtype = add_type(f)
    two = record(f, "Two")
    linked = threading.Event()
    outcome: dict = {}

    def create():
        try:
            with transaction.atomic():
                link(f, "runs_on", f["app"], two)
                linked.set()
                time.sleep(1.0)  # the delete arrives while the link is uncommitted
        finally:
            connection.close()

    def delete():
        try:
            linked.wait(10)
            relationship_types.delete_type(f["org"], f["users"]["admin"], rtype)
        except Exception as e:  # collected for the assertions
            outcome["error"] = e
        finally:
            connection.close()

    threads = [threading.Thread(target=create), threading.Thread(target=delete)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert isinstance(outcome.get("error"), Conflict) and "1 relationship uses it" in str(outcome["error"].detail)
    assert RelationshipType.objects.filter(id=rtype.id).exists()


# ---- relationships with types ---------------------------------------------------------------------------------------


def test_an_added_type_follows_the_depends_on_endpoint_rules(a):
    add_type(a)
    feed = services.create_integration(
        a["org"],
        a["users"]["editor"],
        {"name": "Feed", "lifecycle": "active", "source": str(a["app"].id), "target": str(record(a, "Two").id)},
    )
    with pytest.raises(FieldErrors) as e:
        link(a, "runs_on", a["app"], feed)
    assert "target" in e.value.errors
    with pytest.raises(FieldErrors) as e:
        link(a, "runs_on", a["app"], a["app"])
    assert "A record can only link to itself" in e.value.errors["target"][0]
    agent = record(a, "Helper", "ai_agent")
    rel = link(a, "runs_on", agent, a["app"])
    assert rel.type.slug == "runs_on"


def test_an_unknown_or_other_organizations_type_is_refused(a, b):
    RelationshipType.objects.create(organization=b["org"], slug="theirs", name="Theirs")
    for slug in ("theirs", "nope", None, 3):
        with pytest.raises(FieldErrors) as e:
            link(a, slug, a["app"], record(a, f"Two {slug}"))
        assert "type" in e.value.errors


def test_relationship_audit_keeps_the_slug_and_the_snapshot_names_the_type(a):
    rel = link(a, "depends_on", a["app"], record(a, "Two"))
    event = AuditEvent.objects.get(action="relationship.created", target_id=rel.id)
    assert event.changes["type"] == "depends_on" and event.postchange is not None
    assert event.postchange["type"] == {"id": str(rel.type_id), "display": "Depends on"}


# ---- traversal follows is_dependency --------------------------------------------------------------------------------


def _explore(ctx, target):
    return graph.traverse(ctx["org"], target, direction="dependents", depth=None)


def test_traversal_follows_an_added_type_only_while_it_counts_as_a_dependency(a):
    runs_on = add_type(a)
    host = a["app"]
    service = record(a, "Service")
    link(a, "runs_on", service, host)
    assert [n["name"] for n in _explore(a, host)["nodes"]] == []
    assert services.dependents_of(a["org"], host) == []

    relationship_types.update_type(a["org"], a["users"]["admin"], runs_on, {"is_dependency": True})
    found = _explore(a, host)
    assert [n["name"] for n in found["nodes"]] == ["Service"]
    assert found["nodes"][0]["path"][1]["via"]["type"] == "runs_on"
    assert [r.name for r, _ in services.dependents_of(a["org"], host)] == ["Service"]
    affected = assessments.affected_set(a["org"], host)
    assert {n["id"] for n in affected["nodes"]} == {str(service.id)}


def test_core_types_traverse_as_before(a):
    host, user, successor = a["app"], record(a, "User"), record(a, "Successor")
    link(a, "depends_on", user, host)
    link(a, "replaces", successor, host)
    assert [n["name"] for n in _explore(a, host)["nodes"]] == ["User"]


def test_turning_on_is_dependency_flags_open_assessments(a):
    runs_on = add_type(a)
    host, service = a["app"], record(a, "Service")
    link(a, "runs_on", service, host)
    assessment = assessments.create_assessment(
        a["org"], a["users"]["editor"], {"target": str(host.id), "change_type": "change", "title": "Upgrade"}
    )
    snapshot = AssessmentSnapshot.objects.get(assessment=assessment, is_current=True)
    assert snapshot.stale_since is None
    relationship_types.update_type(a["org"], a["users"]["admin"], runs_on, {"is_dependency": True})
    snapshot.refresh_from_db()
    assert snapshot.stale_since is not None
    assert "Runs on now counts as a dependency" in snapshot.stale_reasons[0]


# ---- API ------------------------------------------------------------------------------------------------------------


def test_every_role_reads_the_types_with_labels_and_usage(a, api):
    link(a, "depends_on", a["app"], record(a, "Two"))
    body = api(a["users"]["viewer"]).get("relationship-types").json_body
    assert [t["slug"] for t in body] == ["depends_on", "implements", "replaces"]  # by name
    depends = body[0]
    assert depends["inverse_name"] == "Is needed by" and depends["is_core"] is True and depends["usage"] == 1
    assert depends["is_dependency"] is True


@pytest.mark.parametrize("role,status", [("viewer", 403), ("editor", 403), ("admin", 201)])
def test_only_admins_add_types(a, api, role, status):
    response = api(a["users"][role]).post("relationship-types", {"name": "Hosted on", "inverse_name": "Hosts"})
    assert response.status_code == status
    if status == 201:
        assert response.json_body["slug"] == "hosted_on" and response.json_body["usage"] == 0


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_only_admins_change_or_delete_types(a, api, role):
    rtype = add_type(a)
    client = api(a["users"][role])
    assert client.patch(f"relationship-types/{rtype.id}", {"name": "Lives on"}).status_code == 403
    assert client.delete(f"relationship-types/{rtype.id}").status_code == 403
    assert RelationshipType.objects.get(id=rtype.id).name == "Runs on"


def test_api_delete_in_use_is_a_409_with_the_count(a, api):
    rtype = add_type(a)
    link(a, "runs_on", a["app"], record(a, "Two"))
    admin = api(a["users"]["admin"])
    refused = admin.delete(f"relationship-types/{rtype.id}")
    assert refused.status_code == 409
    assert refused.json_body["blocking_references"] == {"relationships": 1}
    assert "1 relationship uses it" in refused.json_body["detail"]
    core = admin.delete(f"relationship-types/{rel_type(a['org']).id}")
    assert core.status_code == 409 and core.json_body["type"] == "/problems/core-relationship-type"


def test_api_patch_core_type_labels(a, api):
    admin = api(a["users"]["admin"])
    depends = rel_type(a["org"])
    ok = admin.patch(f"relationship-types/{depends.id}", {"inverse_name": "Is used by"})
    assert ok.status_code == 200 and ok.json_body["inverse_name"] == "Is used by"
    refused = admin.patch(f"relationship-types/{depends.id}", {"is_dependency": False})
    assert refused.status_code == 400 and "is_dependency" in refused.json_body["errors"]


def test_relationships_keep_the_slug_on_the_wire_and_carry_the_labels(a, api):
    add_type(a, inverse_name="Hosts", is_dependency=True)
    editor = api(a["users"]["editor"])
    two = record(a, "Two")
    created = editor.post("relationships", {"type": "runs_on", "source": str(a["app"].id), "target": str(two.id)})
    assert created.status_code == 201, created.json_body
    body = created.json_body
    assert body["type"] == "runs_on"
    assert body["relationship_type"]["name"] == "Runs on" and body["relationship_type"]["inverse_name"] == "Hosts"
    assert body["relationship_type"]["is_dependency"] is True and body["relationship_type"]["is_core"] is False
    core = editor.post("relationships", {"type": "depends_on", "source": str(two.id), "target": str(a["app"].id)})
    assert core.json_body["type"] == "depends_on" and core.json_body["relationship_type"]["slug"] == "depends_on"
    listed = editor.get("relationships?type=runs_on").json_body
    assert [r["type"] for r in listed] == ["runs_on"]
    assert {r["type"] for r in editor.get("relationships?type=runs_on,depends_on").json_body} == {
        "runs_on",
        "depends_on",
    }
    unknown = editor.get("relationships?type=nope")
    assert unknown.status_code == 400 and "type" in unknown.json_body["errors"]


def test_another_organizations_types_are_invisible(a, b, api):
    theirs = RelationshipType.objects.create(organization=b["org"], slug="theirs", name="Theirs")
    client = api(a["users"]["admin"])
    assert "theirs" not in {t["slug"] for t in client.get("relationship-types").json_body}
    assert client.get(f"relationship-types/{theirs.id}").status_code == 404
    assert client.patch(f"relationship-types/{theirs.id}", {"name": "Mine"}).status_code == 404
    assert client.delete(f"relationship-types/{theirs.id}").status_code == 404
    refused = client.post(
        "relationships", {"type": "theirs", "source": str(a["app"].id), "target": str(record(a, "Two").id)}
    )
    assert refused.status_code == 400 and "type" in refused.json_body["errors"]
    filtered = client.get("relationships?type=theirs")
    assert filtered.status_code == 400
    assert RelationshipType.objects.get(id=theirs.id).name == "Theirs"


# ---- migration 0034: the backfill -----------------------------------------------------------------------------------

BEFORE_TYPES = [("catalog", "0032_quality_exceptions")]


@pytest.mark.django_db(transaction=True)
def test_the_type_column_is_filled_from_the_old_values():
    """Links made before 0033/0034 point at their organization's type of the same slug afterwards; a value no type has
    gets an added (not core, not dependency) type, so no link is lost."""
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE_TYPES)
    old = executor.loader.project_state(BEFORE_TYPES).apps
    Org, Res, Rel = (
        old.get_model("core", "Organization"),
        *(old.get_model("catalog", m) for m in ("Resource", "Relationship")),
    )
    org = Org.objects.create(name="Typed (synthetic)")
    ends = [
        Res.objects.create(organization=org, name=f"R{i}", slug=f"r{i}", lifecycle="active", kind="application")
        for i in range(3)
    ]
    Rel.objects.create(organization=org, type="depends_on", source=ends[0], target=ends[1])
    Rel.objects.create(organization=org, type="replaces", source=ends[2], target=ends[1])
    Rel.objects.create(organization=org, type="uses", source=ends[0], target=ends[2])  # behind the services' back
    # Named like the core "Depends on", ignoring case: the made type's name gets a suffix instead of failing.
    Rel.objects.create(organization=org, type="DEPENDS_ON", source=ends[1], target=ends[2])

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    try:
        new = executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps
        NewRel, NewType = new.get_model("catalog", "Relationship"), new.get_model("catalog", "RelationshipType")
        got = {
            (r.source.name, r.target.name): (r.type.slug, r.type.is_core, r.type.organization_id)
            for r in NewRel.objects.filter(organization_id=org.id).select_related("type", "source", "target")
        }
        assert got == {
            ("R0", "R1"): ("depends_on", True, org.id),
            ("R2", "R1"): ("replaces", True, org.id),
            ("R0", "R2"): ("uses", False, org.id),
            ("R1", "R2"): ("DEPENDS_ON", False, org.id),
        }
        types = NewType.objects.filter(organization_id=org.id)
        assert sorted(types.values_list("slug", flat=True)) == [
            "DEPENDS_ON",
            "depends_on",
            "implements",
            "replaces",
            "uses",
        ]
        assert types.get(slug="DEPENDS_ON").name == "Depends on (2)"
        assert types.get(slug="depends_on").is_dependency and not types.get(slug="uses").is_dependency
    finally:
        with connection.cursor() as cursor:
            for table in (
                "catalog_relationship",
                "catalog_resource",
                "catalog_relationshiptype",
                "catalog_softwarecategory",
                "notify_notificationrule",  # seeded by notify 0005
            ):
                cursor.execute(f"DELETE FROM {table} WHERE organization_id = %s", [org.id])  # noqa: S608
            cursor.execute("DELETE FROM catalog_organization WHERE id = %s", [org.id])
