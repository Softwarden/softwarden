"""Resource split: slugs, one extension row per kind, archived_at, and the data migration."""

import uuid
from datetime import date

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from catalog import services
from catalog.models import Application, Component, Resource, Vendor
from core.problems import FieldErrors


def make(org, actor, name="Payroll", **extra):
    return services.create_resource(org, actor, {"name": name, "lifecycle": "active", **extra})


def test_slug_suffixes_on_collision(a):
    org, actor = a["org"], a["users"]["editor"]
    slugs = [make(org, actor, "Payroll Hub").slug for _ in range(3)]
    assert slugs == ["payroll-hub", "payroll-hub-2", "payroll-hub-3"]


def test_slug_is_unique_per_kind_and_org(a, b):
    editor = a["users"]["editor"]
    app = make(a["org"], editor, "Nightly Export")
    component = make(a["org"], editor, "Nightly Export", kind="component")
    other_org = make(b["org"], b["users"]["editor"], "Nightly Export")
    assert app.slug == component.slug == other_org.slug == "nightly-export"


def test_slug_survives_rename(a):
    app = make(a["org"], a["users"]["editor"], "Old Name")
    services.update_resource(a["org"], a["users"]["editor"], app, {"name": "New Name"})
    app.refresh_from_db()
    assert (app.name, app.slug) == ("New Name", "old-name")


def test_unsluggable_name_falls_back_to_kind(a):
    assert make(a["org"], a["users"]["editor"], "!!!").slug == "application"


@pytest.mark.parametrize(
    "kind,present,absent", [("application", Application, Component), ("component", Component, Application)]
)
def test_create_makes_exactly_one_matching_extension(a, kind, present, absent):
    resource = make(a["org"], a["users"]["editor"], kind=kind)
    assert present.objects.filter(resource=resource).count() == 1
    assert absent.objects.filter(resource=resource).count() == 0


def test_unknown_kind_is_rejected_without_rows(a):
    before = Resource.objects.count()
    with pytest.raises(FieldErrors):
        make(a["org"], a["users"]["editor"], kind="integration")
    assert Resource.objects.count() == before


def test_failed_create_leaves_no_orphan_rows(a):
    before = (Resource.objects.count(), Application.objects.count())
    with pytest.raises(FieldErrors):
        make(a["org"], a["users"]["editor"], vendor="not-a-vendor")
    assert (Resource.objects.count(), Application.objects.count()) == before


def test_check_one_extension_flags_missing_and_extra(a):
    resource = Resource.objects.get(id=make(a["org"], a["users"]["editor"], kind="component").id)
    services.check_one_extension(resource)
    Application.objects.create(resource=resource, organization=a["org"])
    with pytest.raises(ValueError):
        services.check_one_extension(Resource.objects.get(id=resource.id))
    orphan = Resource.objects.create(organization=a["org"], name="Bare", slug="bare", lifecycle="active")
    with pytest.raises(ValueError):
        services.check_one_extension(orphan)


def test_component_fields_and_type_validation(a):
    org, editor = a["org"], a["users"]["editor"]
    c = make(
        org,
        editor,
        kind="component",
        component_type="runbook",
        host_runtime="cron",
        vendor=str(Vendor.objects.create(organization=org, name="V").id),
    )
    assert (c.component.component_type, c.component.host_runtime) == ("runbook", "cron")
    with pytest.raises(FieldErrors) as exc:
        make(org, editor, "Bad", kind="component", component_type="toaster")
    assert "component_type" in exc.value.errors


def test_application_vendor_in_json_and_blocks_vendor_delete(a, api):
    editor = a["users"]["editor"]
    vendor = Vendor.objects.create(organization=a["org"], name="Contoso Labs")
    app = make(a["org"], editor, "With Vendor", vendor=str(vendor.id))
    client = api(editor)
    body = client.get(f"applications/{app.id}").json_body
    assert body["vendor"]["name"] == "Contoso Labs" and body["archived"] is False
    assert client.delete(f"vendors/{vendor.id}").status_code == 409


def test_archived_at_hides_from_list(a, api):
    app = a["app"]
    from django.utils import timezone

    app.archived_at = timezone.now()
    app.save()
    client = api(a["users"]["viewer"])
    assert client.get("applications").json_body["results"] == []
    assert client.get(f"applications/{app.id}").json_body["archived"] is True


@pytest.mark.django_db(transaction=True)
def test_data_migration_moves_existing_rows():
    executor = MigrationExecutor(connection)
    executor.migrate([("catalog", "0002_accounts_reference_data")])
    old = executor.loader.project_state([("catalog", "0002_accounts_reference_data")]).apps
    Org, Vend, Res = (
        old.get_model("catalog", "Organization"),
        old.get_model("catalog", "Vendor"),
        old.get_model("catalog", "Resource"),
    )
    org = Org.objects.create(name="Migrated (synthetic)")
    vendor = Vend.objects.create(organization=org, name="Zylker")
    first = Res.objects.create(
        organization=org, name="Same Name", lifecycle="active", purpose="Pay people", vendor=vendor
    )
    second = Res.objects.create(organization=org, name="Same Name", lifecycle="active", archived=True)
    comp = Res.objects.create(organization=org, name="Same Name", kind="component", lifecycle="active", purpose="Cron")

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    try:
        new = executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps
        R, A, C = (new.get_model("catalog", m) for m in ("Resource", "Application", "Component"))
        r1, r2, r3 = R.objects.get(id=first.id), R.objects.get(id=second.id), R.objects.get(id=comp.id)
        assert [r1.slug, r2.slug, r3.slug] == ["same-name", "same-name-2", "same-name"]
        assert r1.archived_at is None and r2.archived_at is not None
        assert (A.objects.get(resource_id=r1.id).purpose, A.objects.get(resource_id=r1.id).vendor_id) == (
            "Pay people",
            vendor.id,
        )
        assert A.objects.filter(resource_id=r2.id).exists() and not C.objects.filter(resource_id=r2.id).exists()
        assert C.objects.get(resource_id=r3.id).purpose == "Cron" and not A.objects.filter(resource_id=r3.id).exists()
        # catalog 0020 filled the extension rows' organization from their resource.
        assert {e.organization_id for e in A.objects.filter(resource__in=[r1, r2])} == {org.id}
        assert C.objects.get(resource_id=r3.id).organization_id == org.id
    finally:
        Res = executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps.get_model("catalog", "Resource")
        for model in ("Application", "Component"):
            executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps.get_model(
                "catalog", model
            ).objects.all().delete()
        Res.objects.all().delete()
        executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps.get_model(
            "catalog", "Vendor"
        ).objects.all().delete()
        # catalog 0023 seeded the organization's software categories.
        executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps.get_model(
            "catalog", "SoftwareCategory"
        ).objects.filter(organization_id=org.id).delete()
        # catalog 0033 seeded its relationship types.
        executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps.get_model(
            "catalog", "RelationshipType"
        ).objects.filter(organization_id=org.id).delete()
        # notify 0005 seeded its notification rules.
        executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps.get_model(
            "notify", "NotificationRule"
        ).objects.filter(organization_id=org.id).delete()
        executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps.get_model(
            "core", "Organization"
        ).objects.filter(name="Migrated (synthetic)").delete()


# core 0003 keeps Organization as these rows' historical model knew it (core 0004 added NOT NULL slug).
BEFORE_KEY_SWAPS = [
    ("imports", "0001_initial"),
    ("money", "0001_initial"),
    ("catalog", "0021_composite_keys"),
    ("core", "0003_membership_per_organization"),
]


@pytest.mark.django_db(transaction=True)
def test_tenancy_migrations_fill_organization_and_swap_keys_on_existing_rows():
    """imports 0002, money 0002 and catalog 0022 on rows made before them: ImportRow gets its run's organization and
    every row of the three tables a distinct UUID key."""
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE_KEY_SWAPS)
    old = executor.loader.project_state(BEFORE_KEY_SWAPS).apps
    org = old.get_model("core", "Organization").objects.create(name="Key swap (synthetic)")
    resource = old.get_model("catalog", "Resource").objects.create(
        organization=org, name="Swapped", slug="swapped", lifecycle="active"
    )
    Tag, ResourceTag = old.get_model("catalog", "Tag"), old.get_model("catalog", "ResourceTag")
    for name in ("one", "two"):
        ResourceTag.objects.create(
            organization=org, resource=resource, tag=Tag.objects.create(organization=org, name=name)
        )
    run = old.get_model("imports", "ImportRun").objects.create(
        organization=org, kind="application", filename="x.csv", purge_after=date(2030, 1, 1)
    )
    ImportRow = old.get_model("imports", "ImportRow")
    for n in range(2, 6):
        ImportRow.objects.create(run=run, row_number=n, raw={}, outcome="update", matched_resource=resource)
    obligation = old.get_model("money", "RenewalObligation").objects.create(organization=org, name="Swapped")
    cycle = old.get_model("money", "RenewalCycle").objects.create(organization=org, obligation=obligation, sequence=1)
    old.get_model("money", "RenewalCycleApplication").objects.create(
        organization=org, cycle=cycle, application=resource
    )

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, organization_id FROM catalog_importrow")
            rows = cursor.fetchall()
            ids = {}
            for table in ("catalog_resourcetag", "catalog_renewalcycleapplication"):
                cursor.execute(f"SELECT id FROM {table}")  # noqa: S608 - fixed table names
                ids[table] = [i for (i,) in cursor.fetchall()]
        assert len(rows) == 4 and {o for _, o in rows} == {org.id}
        assert len({i for i, _ in rows}) == 4 and all(isinstance(i, uuid.UUID) for i, _ in rows)
        assert len(set(ids["catalog_resourcetag"])) == 2 and len(ids["catalog_renewalcycleapplication"]) == 1
        assert all(isinstance(i, uuid.UUID) for values in ids.values() for i in values)
    finally:
        with connection.cursor() as cursor:
            for table in (
                "catalog_importrow",
                "catalog_importrun",
                "catalog_resourcetag",
                "catalog_tag",
                "catalog_renewalcycleapplication",
                "catalog_renewalcycle",
                "catalog_renewalobligation",
                "catalog_resource",
                "catalog_softwarecategory",  # seeded by catalog 0023
                "catalog_relationshiptype",  # seeded by catalog 0033
                "notify_notificationrule",  # seeded by notify 0005
            ):
                cursor.execute(f"DELETE FROM {table} WHERE organization_id = %s", [org.id])  # noqa: S608
            cursor.execute("DELETE FROM catalog_organization WHERE id = %s", [org.id])


def test_duplicate_name_is_a_warning_that_can_be_confirmed(a, api):
    editor = api(a["users"]["editor"])
    first = editor.post("applications", {"name": "Expense Hub", "lifecycle": "active"})
    assert first.status_code == 201
    warned = editor.post("applications", {"name": "expense hub", "lifecycle": "active"})
    assert warned.status_code == 409 and warned.json_body["type"] == "/problems/duplicate-name"
    assert warned.json_body["detail"] == "Another application is named Expense Hub. Save anyway?"
    assert warned.json_body["duplicate_of"]["id"] == first.json_body["id"]
    second = editor.post("applications", {"name": "Expense Hub", "lifecycle": "active", "confirm_duplicate": True})
    assert second.status_code == 201 and second.json_body["slug"] == "expense-hub-2"  # nothing merges
    # A component may share an application's name; renaming onto a clash warns; keeping your own name doesn't.
    assert editor.post("components", {"name": "Expense Hub", "lifecycle": "active"}).status_code == 201
    other = editor.post("applications", {"name": "Travel", "lifecycle": "active"}).json_body
    assert editor.patch(f"applications/{other['id']}", {"name": "EXPENSE HUB"}).status_code == 409
    assert editor.patch(f"applications/{first.json_body['id']}", {"name": "Expense hub"}).status_code == 200
