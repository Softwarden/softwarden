"""Cross-tenant suite: another organization's IDs behave as if they don't exist. The database half
checks that every tenant-to-tenant reference has a composite foreign key and refuses a cross-organization row."""

from datetime import date

import pytest
from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, models, transaction

from catalog import services
from catalog.models import (
    AIAgent,
    Application,
    ApplicationInstance,
    Component,
    Department,
    Kind,
    Person,
    RecordReview,
    Relationship,
    RelationshipType,
    Resource,
    SoftwareCategory,
    Team,
    TeamMember,
    Vendor,
)
from core.local_apps import local_models
from core.models import AuditEvent, Membership
from imports import csvio
from imports.models import ImportRow, ImportRun
from notify.models import NotificationPreference


@pytest.mark.parametrize(
    "method,suffix,payload",
    [
        ("get", "", None),
        ("patch", "", {"name": "Hijacked"}),
        ("get", "/reviews", None),
        ("post", "/reviews", {"evidence": "x"}),
    ],
)
def test_other_org_record_is_404(a, b, api, method, suffix, payload):
    client = api(a["users"]["admin"])
    url = f"applications/{b['app'].id}{suffix}"
    response = getattr(client, method)(url, payload) if payload is not None else getattr(client, method)(url)
    assert response.status_code == 404
    b["app"].refresh_from_db()
    assert b["app"].name == "orgb App"


@pytest.mark.parametrize(
    "method,suffix,payload",
    [
        ("get", "", None),
        ("patch", "", {"name": "Hijacked"}),
        ("get", "/reviews", None),
        ("post", "/reviews", {"evidence": "x"}),
    ],
)
def test_other_org_component_is_404(a, b, api, method, suffix, payload):
    component = services.create_resource(
        b["org"], b["users"]["editor"], {"name": "orgb Job", "kind": "component", "lifecycle": "active"}
    )
    client = api(a["users"]["admin"])
    url = f"components/{component.id}{suffix}"
    response = getattr(client, method)(url, payload) if payload is not None else getattr(client, method)(url)
    assert response.status_code == 404
    component.refresh_from_db()
    assert component.name == "orgb Job"


@pytest.mark.parametrize(
    "kind,method,suffix",
    [
        (kind, method, suffix)
        for kind in ("application", "component")
        for method, suffix in (("post", "/archive"), ("post", "/restore"), ("delete", ""))
    ],
)
def test_other_org_archive_restore_delete_is_404(a, b, api, kind, method, suffix):
    if kind == "application":
        theirs = b["app"]
    else:
        theirs = services.create_resource(
            b["org"], b["users"]["editor"], {"name": "orgb Job", "kind": "component", "lifecycle": "active"}
        )
    client = api(a["users"]["admin"])
    before = AuditEvent.objects.count()
    response = getattr(client, method)(f"{kind}s/{theirs.id}{suffix}")
    assert response.status_code == 404
    theirs.refresh_from_db()
    assert theirs.archived_at is None and AuditEvent.objects.count() == before


def test_needs_attention_excludes_other_org_agents(a, b, api):
    services.create_resource(
        b["org"], b["users"]["editor"], {"name": "orgb Bot", "kind": "ai_agent", "lifecycle": "active"}
    )
    attention = api(a["users"]["admin"]).get("needs-attention?scope=all").json_body
    names = {r["name"] for q in ("ownership_gaps", "reviews", "agent_gaps") for r in attention[q]}
    assert names == {"orga App"}
    assert api(a["users"]["viewer"]).get("ai-agents").json_body["results"] == []


def test_kinds_do_not_cross_endpoints(a, api):
    editor = a["users"]["editor"]
    component = services.create_resource(a["org"], editor, {"name": "Job", "kind": "component", "lifecycle": "active"})
    client = api(editor)
    assert client.get(f"applications/{component.id}").status_code == 404
    assert client.get(f"components/{a['app'].id}").status_code == 404
    assert client.get(f"applications/{component.id}/reviews").status_code == 404
    assert client.get(f"components/{a['app'].id}/reviews").status_code == 404
    assert client.post(f"applications/{component.id}/reviews", {"evidence": "x"}).status_code == 404
    assert client.post(f"components/{a['app'].id}/reviews", {"evidence": "x"}).status_code == 404
    assert not RecordReview.objects.exists()


def test_component_list_and_filters_stay_in_own_org(a, b, api):
    services.create_resource(
        b["org"], b["users"]["editor"], {"name": "orgb Job", "kind": "component", "lifecycle": "active"}
    )
    client = api(a["users"]["viewer"])
    assert client.get("components").json_body["results"] == []
    # Another organization's person, team and vendor IDs filter to nothing rather than leaking records.
    assert (
        client.get(f"applications?owner={b['people']['dana'].id}&support_team={b['team'].id}").json_body["results"]
        == []
    )


def test_lists_only_show_own_org(a, b, api):
    client = api(a["users"]["viewer"])
    names = {r["name"] for r in client.get("applications").json_body["results"]}
    assert names == {"orga App"}
    people = {p["name"] for p in client.get("people").json_body}
    assert "Former person" in people and len(people) == 3
    attention = client.get("needs-attention?scope=all").json_body
    assert {r["name"] for r in attention["ownership_gaps"] + attention["reviews"]} == {"orga App"}


def test_needs_attention_excludes_other_org_components(a, b, api):
    services.create_resource(
        b["org"], b["users"]["editor"], {"name": "orgb Job", "kind": "component", "lifecycle": "active"}
    )
    attention = api(a["users"]["admin"]).get("needs-attention?scope=all").json_body
    assert {r["name"] for r in attention["ownership_gaps"] + attention["reviews"]} == {"orga App"}


def test_cannot_reference_other_org_people_or_teams(a, b, api):
    client = api(a["users"]["editor"])
    url = f"applications/{a['app'].id}"
    response = client.patch(url, {"business_owner": str(b["people"]["dana"].id), "support_team": str(b["team"].id)})
    assert response.status_code == 400
    assert set(response.json_body["errors"]) == {"business_owner", "support_team"}
    review = client.post(url + "/reviews", {"confirmed_by": str(b["people"]["dana"].id), "evidence": "x"})
    assert review.status_code == 400 and not RecordReview.objects.exists()


def test_writes_are_audited_in_own_org(a, api):
    client = api(a["users"]["editor"])
    client.post("applications", {"name": "Audited", "lifecycle": "active"})
    event = AuditEvent.objects.get(action="resource.created")
    assert event.organization == a["org"] and event.actor == a["users"]["editor"]


@pytest.mark.parametrize(
    "method,payload",
    [
        ("get", None),
        ("patch", {"name": "Hijacked"}),
        ("delete", None),
    ],
)
def test_other_org_instance_is_404(a, b, api, method, payload):
    theirs = services.create_instance(b["org"], b["users"]["editor"], b["app"], {"name": "Production"})
    client = api(a["users"]["admin"])
    before = AuditEvent.objects.count()
    for app_id in (b["app"].id, a["app"].id):  # their application, then our own application with their instance ID
        url = f"applications/{app_id}/instances/{theirs.id}"
        response = getattr(client, method)(url, payload) if payload is not None else getattr(client, method)(url)
        assert response.status_code == 404
    theirs.refresh_from_db()
    assert theirs.name == "Production" and AuditEvent.objects.count() == before


def test_other_org_application_instances_are_404(a, b, api):
    client = api(a["users"]["admin"])
    url = f"applications/{b['app'].id}/instances"
    assert client.get(url).status_code == 404
    assert client.post(url, {"name": "Production"}).status_code == 404
    assert not ApplicationInstance.objects.exists()


def test_instances_do_not_exist_on_components(a, api):
    editor = a["users"]["editor"]
    component = services.create_resource(a["org"], editor, {"name": "Job", "kind": "component", "lifecycle": "active"})
    instance = services.create_instance(a["org"], editor, a["app"], {"name": "Production"})
    client = api(editor)
    assert client.get(f"applications/{component.id}/instances").status_code == 404
    assert client.post(f"applications/{component.id}/instances", {"name": "Production"}).status_code == 404
    assert client.get(f"applications/{component.id}/instances/{instance.id}").status_code == 404
    assert "instances" not in client.get(f"components/{component.id}").json_body
    assert ApplicationInstance.objects.count() == 1


def test_database_rejects_instance_pointing_at_other_org_application(a, b):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ApplicationInstance.objects.create(organization=a["org"], application=b["app"], name="Smuggled")
            connection.check_constraints()  # the composite FK is deferred; force the check


# ---- Database: composite foreign keys -----------------------------------------------------------------------------

# Every local app at any depth (core.local_apps reads the app registry: ee/saml and ee/scim as well as catalog), so a
# new app's models are held to the checks below the moment it is installed.
# Models with no organization at all: the tenant itself and per-user or per-process rows. BootstrapState is the
# installation's one first-run setup flag (core/bootstrap.py), which exists before any organization does; BackupState
# is the installation's backup status (core/backup.py), like the backups themselves not one organization's.
NOT_TENANT = {
    "core.Organization",
    "core.UserIdentity",
    "core.WorkerHeartbeat",
    "core.Schedule",
    "core.BootstrapState",
    "core.BackupState",
    # The shared rate-limit counters are the installation's; an organization's API bucket is a hashed key.
    "core.RateCounter",
    # Cloud: a signup waiting for its email confirmation exists before its organization does, and a
    # tombstone outlives the organization it records (catalog/signup.py, catalog/org_lifecycle.py).
    "core.PendingSignup",
    # A password reset link resets an account, which is no one organization's (catalog/password_reset.py).
    "core.PasswordReset",
    # That an account's owner proved its mailbox is about the account, not one organization.
    "core.MailboxProof",
    "core.OrganizationTombstone",
    # Platform admin console: platform staff are installation operators, and the platform audit stream
    # outlives the organizations it mentions (a plain organization_id, catalog/platform_admin.py).
    "core.PlatformStaff",
    "core.PlatformAuditEvent",
    # The endoflife.date cache is public data shared by the installation (eol/models.py), not tenant data.
    "eol.EolProduct",
    "eol.EolRelease",
    "eol.EolSyncState",
}
# Tenant rows whose organization may be null, and why. Every other tenant table's organization is NOT NULL.
NULLABLE_ORG = {
    "core.Job": "installation-wide work (backups, media sweeps, pruning jobs) belongs to no organization",
}
# Tenant rows without their own (organization, id) key, and why.
NO_ORG_KEY = {
    "catalog.SnapshotNode": "lean rows written a thousand at a time (migration catalog 0014)",
    "catalog.SnapshotLink": "lean rows written a thousand at a time (migration catalog 0014)",
}


def _label(model):
    return f"{model._meta.app_label}.{model.__name__}"


def _tenant(model):
    return any(f.name == "organization" for f in model._meta.concrete_fields)


def _local_models():
    return local_models()


def _composite_keys():
    """(table, column, referenced table) for every column of a database foreign key that includes organization_id."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT cl.relname, con.confrelid::regclass::text, array_agg(a.attname::text)
            FROM pg_constraint con
            JOIN pg_class cl ON cl.oid = con.conrelid
            JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = ANY (con.conkey)
            WHERE con.contype = 'f' AND array_length(con.conkey, 1) >= 2
            GROUP BY con.oid, cl.relname, con.confrelid
            """
        )
        rows = cursor.fetchall()
    return {
        (table, column, target) for table, target, columns in rows if "organization_id" in columns for column in columns
    }


def test_no_model_has_an_implicit_many_to_many_table():
    """A plain ManyToManyField makes a through table Django writes itself: no organization, no UUID key, no composite
    keys, and none of the checks here see it. Every many-to-many goes through a tenant model of its own."""
    implicit = [_label(m) for m in local_models(include_auto_created=True) if m._meta.auto_created]
    assert implicit == []
    assert len(local_models(include_auto_created=True)) == len(_local_models())


def test_every_local_model_is_a_tenant_table_or_named():
    assert {_label(m) for m in _local_models() if not _tenant(m)} == NOT_TENANT


def test_every_tenant_table_has_a_uuid_key_and_an_organization_key():
    for model in _local_models():
        if not _tenant(model) or _label(model) in NO_ORG_KEY:
            continue
        pk = model._meta.pk
        if isinstance(pk, models.OneToOneField):
            # An extension row: its key is its parent's, which must itself be a tenant row with a UUID key.
            parent = pk.related_model
            assert _tenant(parent) and isinstance(parent._meta.pk, models.UUIDField), _label(model)
        else:
            assert isinstance(pk, models.UUIDField), _label(model)
        keys = [tuple(c.fields) for c in model._meta.constraints if isinstance(c, models.UniqueConstraint)]
        assert ("organization", pk.name) in keys, f"{_label(model)} lacks UNIQUE (organization, {pk.name})"


def test_only_named_tenant_tables_allow_a_null_organization():
    nullable = {_label(m) for m in _local_models() if _tenant(m) and m._meta.get_field("organization").null}
    assert nullable == set(NULLABLE_ORG)


def test_nullable_organization_tables_reference_no_tenant_rows():
    """A row with no organization can't carry a composite key, so such a table must not point at tenant rows: a job
    refers to its records by id in its payload and its callable re-reads them inside the job's organization."""
    for label in NULLABLE_ORG:
        model = apps.get_model(label)
        refs = [
            f.name
            for f in model._meta.concrete_fields
            if f.is_relation and f.name != "organization" and _tenant(f.related_model)
        ]
        assert refs == [], label


@pytest.mark.django_db
def test_every_tenant_reference_has_a_composite_foreign_key():
    covered = _composite_keys()
    missing = [
        f"{_label(model)}.{field.name}"
        for model in _local_models()
        if _tenant(model)
        for field in model._meta.concrete_fields
        if field.is_relation and field.name != "organization" and _tenant(field.related_model)
        if (model._meta.db_table, field.column, field.related_model._meta.db_table) not in covered
    ]
    assert missing == []


def _rejected(write):
    with pytest.raises(IntegrityError), transaction.atomic():
        write()
        connection.check_constraints()  # the composite keys are deferred; force the check


def _run(org):
    return ImportRun.objects.create(
        organization=org, kind="application", filename="x.csv", purge_after=date(2030, 1, 1)
    )


def test_database_rejects_person_in_other_org_department(a, b):
    theirs = Department.objects.create(organization=b["org"], name="HR")
    _rejected(lambda: Person.objects.create(organization=a["org"], name="Smuggled", department=theirs))


@pytest.mark.parametrize("field", ["business_owner", "technical_owner", "support_team", "created_by_import"])
def test_database_rejects_resource_pointing_at_other_org(a, b, field):
    theirs = {
        "business_owner": b["people"]["dana"],
        "technical_owner": b["people"]["dana"],
        "support_team": b["team"],
        "created_by_import": _run(b["org"]),
    }[field]
    _rejected(lambda: Resource.objects.filter(id=a["app"].id).update(**{field: theirs}))


@pytest.mark.parametrize("model", [Application, Component, AIAgent])
def test_database_rejects_extension_row_in_another_org(a, b, model):
    bare = Resource.objects.create(organization=a["org"], name="Bare", slug="bare", lifecycle="active")
    _rejected(lambda: model.objects.create(resource=bare, organization=b["org"]))


def test_database_rejects_application_with_other_org_vendor(a, b):
    theirs = Vendor.objects.create(organization=b["org"], name="Zylker")
    _rejected(lambda: Application.objects.filter(resource=a["app"]).update(vendor=theirs))


# Catalog fields (catalog 0024, 0025).


def test_database_rejects_resource_in_other_org_department(a, b):
    theirs = Department.objects.create(organization=b["org"], name="HR")
    _rejected(lambda: Resource.objects.filter(id=a["app"].id).update(department=theirs))


def test_database_rejects_application_with_other_org_category(a, b):
    theirs = SoftwareCategory.objects.filter(organization=b["org"]).first()
    _rejected(lambda: Application.objects.filter(resource=a["app"]).update(category=theirs))


def test_database_rejects_relationship_with_other_org_type(a, b):
    """A link's type is a row of its own organization (catalog 0034)."""
    other = services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Other", "kind": "application", "lifecycle": "active"}
    )
    rel = services.create_relationship(
        a["org"], a["users"]["editor"], {"type": "depends_on", "source": str(a["app"].id), "target": str(other.id)}
    )
    theirs = RelationshipType.objects.get(organization=b["org"], slug="depends_on")
    _rejected(lambda: Relationship.objects.filter(id=rel.id).update(type=theirs))


def test_cannot_see_or_use_other_org_relationship_types_through_the_api(a, b, api):
    theirs = RelationshipType.objects.create(organization=b["org"], slug="runs_on", name="Runs on")
    client = api(a["users"]["admin"])
    assert client.get(f"relationship-types/{theirs.id}").status_code == 404
    assert "runs_on" not in {t["slug"] for t in client.get("relationship-types").json_body}
    other = services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Other", "kind": "application", "lifecycle": "active"}
    )
    created = client.post("relationships", {"type": "runs_on", "source": str(a["app"].id), "target": str(other.id)})
    assert created.status_code == 400 and "type" in created.json_body["errors"]


def test_database_rejects_vendor_with_other_org_account_manager(a, b):
    ours = Vendor.objects.create(organization=a["org"], name="Contoso")
    _rejected(lambda: Vendor.objects.filter(id=ours.id).update(account_manager=b["people"]["dana"]))


@pytest.mark.parametrize("field", ["team", "person"])
def test_database_rejects_team_member_from_other_org(a, b, field):
    values = {"team": a["team"], "person": a["people"]["dana"]}
    values[field] = b["team"] if field == "team" else b["people"]["dana"]
    _rejected(lambda: TeamMember.objects.create(organization=a["org"], **values))


def test_cannot_use_other_org_reference_data_through_the_api(a, b, api):
    client = api(a["users"]["editor"])
    vendor = client.post("vendors", {"name": "Contoso", "account_manager": str(b["people"]["dana"].id)})
    assert vendor.status_code == 400 and "account_manager" in vendor.json_body["errors"]
    team = client.patch(f"teams/{a['team'].id}", {"members": [str(b["people"]["priya"].id)]})
    assert team.status_code == 400 and not Team.objects.get(pk=a["team"].pk).members.exists()
    category = SoftwareCategory.objects.filter(organization=b["org"]).first()
    assert category is not None  # seeded with the organization
    app = client.patch(f"applications/{a['app'].id}", {"category": str(category.id)})
    assert app.status_code == 400 and "category" in app.json_body["errors"]
    assert client.get(f"categories/{category.id}").status_code == 404


@pytest.mark.parametrize("field", ["resource", "confirmed_by"])
def test_database_rejects_review_pointing_at_other_org(a, b, field):
    values = {"resource": a["app"], "confirmed_by": a["people"]["dana"]}
    values[field] = b["app"] if field == "resource" else b["people"]["dana"]
    _rejected(
        lambda: RecordReview.objects.create(
            organization=a["org"],
            recorded_by=a["users"]["editor"],
            confirmation_date=date(2026, 10, 1),
            next_review_date=date(2027, 1, 1),
            evidence="x",
            confirmed_values={},
            **values,
        )
    )


@pytest.mark.parametrize("field", ["run", "matched_resource", "possible_match", "result_resource"])
def test_database_rejects_import_row_pointing_at_other_org(a, b, field):
    values = {"run": _run(a["org"])}
    values[field] = _run(b["org"]) if field == "run" else b["app"]
    _rejected(lambda: ImportRow.objects.create(organization=a["org"], row_number=2, outcome="create", **values))


def _their_contract_and_pool(b):
    from money import contracts

    contract = contracts.create_contract(b["org"], b["users"]["editor"], {"name": "Theirs"})
    pool = contracts.create_pool(b["org"], b["users"]["editor"], {"application": str(b["app"].id), "sku_name": "S"})
    return contract, pool


@pytest.mark.parametrize(
    "field",
    ["matched_contract", "possible_contract", "result_contract", "matched_license_pool", "result_license_pool"],
)
def test_database_rejects_import_row_pointing_at_other_org_contract_or_pool(a, b, field):
    """Imports (imports 0003): contract and licence pool rows keep their targets in the row's organization."""
    contract, pool = _their_contract_and_pool(b)
    values = {"run": _run(a["org"]), field: pool if "pool" in field else contract}
    _rejected(lambda: ImportRow.objects.create(organization=a["org"], row_number=2, outcome="create", **values))


@pytest.mark.parametrize("field", ["contract", "license_pool"])
def test_database_rejects_external_key_for_other_org_contract_or_pool(a, b, field):
    from imports.models import ExternalIdentifier, ImportSource

    contract, pool = _their_contract_and_pool(b)
    source = ImportSource.objects.create(organization=a["org"], name="Sheet", kind=field)
    target = pool if field == "license_pool" else contract
    _rejected(
        lambda: ExternalIdentifier.objects.create(
            organization=a["org"], source=source, external_key="K", **{field: target}
        )
    )


def test_an_external_key_names_exactly_one_target(a):
    from imports.models import ExternalIdentifier, ImportSource
    from money import contracts

    source = ImportSource.objects.create(organization=a["org"], name="Sheet", kind="contract")
    contract = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Mine"})
    _rejected(lambda: ExternalIdentifier.objects.create(organization=a["org"], source=source, external_key="K"))
    _rejected(
        lambda: ExternalIdentifier.objects.create(
            organization=a["org"], source=source, external_key="K", contract=contract, resource=a["app"]
        )
    )


@pytest.mark.parametrize("field", ["cycle", "contract", "resource", "license_pool"])
def test_database_rejects_reminder_pointing_at_other_org_source(a, b, field):
    """Reminders (notify 0003): a reminder's source row is in the reminder's organization."""
    from money import renewals
    from notify.models import Reminder

    contract, pool = _their_contract_and_pool(b)
    obligation = renewals.create_renewal(
        b["org"], b["users"]["editor"], {"name": "Theirs", "renewal_date": "2027-03-31"}
    )
    theirs = {"cycle": obligation.cycles.get(), "contract": contract, "resource": b["app"], "license_pool": pool}
    _rejected(
        lambda: Reminder.objects.create(
            organization=a["org"],
            source_type="custom_date",
            source_id=theirs[field].pk,
            due_date=date(2027, 1, 1),
            title="x",
            label="y",
            **{field: theirs[field]},
        )
    )


def test_reminders_never_cross_organizations(a, b, api):
    from notify import reminders
    from notify.models import Reminder

    Reminder.objects.create(
        organization=b["org"], source_type="record_review", source_id=b["app"].id, resource=b["app"],
        due_date=date(2027, 1, 1), title="Theirs", label="Record review due",
    )  # fmt: skip
    reminders.generate(a["org"], date(2026, 11, 5))  # a's generation never touches b's rows
    assert Reminder.objects.get(organization=b["org"]).status == "open"
    assert api(a["users"]["admin"]).get("reminders").json_body["results"] == []
    theirs = Reminder.objects.get(organization=b["org"])
    assert api(a["users"]["admin"]).get(f"reminders/{theirs.id}").status_code == 404


def test_database_rejects_preference_for_other_org_membership(a, b):
    theirs = Membership.objects.get(user=b["users"]["editor"], organization=b["org"])
    _rejected(lambda: NotificationPreference.objects.create(organization=a["org"], membership=theirs))


@pytest.mark.parametrize("field", ["saved_view", "membership"])
def test_database_rejects_saved_view_subscription_pointing_at_other_org(a, b, field):
    """A subscription's view and member are its organization's (composite keys)."""
    from catalog.models import SavedFilter
    from notify.models import SavedViewSubscription

    def parts(f):
        view = SavedFilter.objects.create(
            organization=f["org"], user=f["users"]["editor"], shared=True, kind="application", name="V", query={}
        )
        return {
            "saved_view": view,
            "membership": Membership.objects.get(user=f["users"]["viewer"], organization=f["org"]),
        }

    ours, theirs = parts(a), parts(b)
    row = {**ours, field: theirs[field]}
    _rejected(lambda: SavedViewSubscription.objects.create(organization=a["org"], weekday=2, **row))
    SavedViewSubscription.objects.create(organization=a["org"], weekday=2, **ours)  # the same row in-org is fine


def test_database_rejects_connector_on_other_org_connection(a, b):
    """A connector's connection is its organization's (composite keys, discovery 0005)."""
    from core import connections
    from discovery.models import Connector

    def conn(f):
        data = {"name": "Entra", "tenant_id": "simulated", "secret": "synthetic-secret"}
        return connections.create_connection(f["org"], f["users"]["admin"], connections.MICROSOFT, data)

    ours, theirs = conn(a), conn(b)
    _rejected(
        lambda: Connector.objects.create(organization=a["org"], source="entra", name="X", microsoft_connection=theirs)
    )
    Connector.objects.create(organization=a["org"], source="entra", name="X", microsoft_connection=ours)


def test_database_rejects_connector_on_other_org_halo_connection(a, b):
    """The same for Halo connections (enterprise; the database key doesn't care about editions)."""
    from core.models import HaloConnection
    from discovery.models import Connector

    def conn(f):
        return HaloConnection.objects.create(
            organization=f["org"], name="Halo", base_url="https://halo.simulated.invalid", client_id="c"
        )

    ours, theirs = conn(a), conn(b)
    _rejected(lambda: Connector.objects.create(organization=a["org"], source="halo", name="H", halo_connection=theirs))
    Connector.objects.create(organization=a["org"], source="halo", name="H", halo_connection=ours)


def test_database_rejects_connector_on_other_org_okta_or_google_connection(a, b):
    """The same for Okta and Google Workspace connections (composite keys, discovery 0017), and a connector
    has one connection at most (discovery_connector_one_connection)."""
    from core.models import GoogleWorkspaceConnection, OktaConnection
    from discovery.models import Connector

    def okta(f):
        return OktaConnection.objects.create(
            organization=f["org"], name="Okta", base_url="https://okta.simulated.invalid", client_id="c"
        )

    def google(f):
        return GoogleWorkspaceConnection.objects.create(
            organization=f["org"], name="Workspace", admin_email="admin@google.simulated.invalid"
        )

    ours, theirs = okta(a), okta(b)
    _rejected(lambda: Connector.objects.create(organization=a["org"], source="okta", name="O", okta_connection=theirs))
    Connector.objects.create(organization=a["org"], source="okta", name="O", okta_connection=ours)
    mine, other = google(a), google(b)
    _rejected(
        lambda: Connector.objects.create(
            organization=a["org"], source="google_workspace", name="G", google_workspace_connection=other
        )
    )
    Connector.objects.create(
        organization=a["org"], source="google_workspace", name="G", google_workspace_connection=mine
    )
    _rejected(
        lambda: Connector.objects.create(
            organization=a["org"], source="okta", name="Both", okta_connection=ours, google_workspace_connection=mine
        )
    )


def test_database_allows_one_membership_per_user_and_organization(a, b):
    user = a["users"]["viewer"]
    Membership.objects.create(user=user, organization=b["org"], role="viewer")  # a second organization is fine
    with pytest.raises(IntegrityError), transaction.atomic():
        Membership.objects.create(user=user, organization=a["org"], role="editor")


def test_services_give_extension_rows_their_resources_organization(a):
    editor = a["users"]["editor"]
    component = services.create_resource(a["org"], editor, {"name": "Job", "kind": "component", "lifecycle": "active"})
    app = services.create_resource(a["org"], editor, {"name": "Payroll", "lifecycle": "active"})
    assert Component.objects.get(resource=component).organization_id == a["org"].id
    assert Application.objects.get(resource=app).organization_id == a["org"].id
    bot = services.create_resource(a["org"], editor, {"name": "Bot", "kind": "ai_agent", "lifecycle": "active"})
    assert AIAgent.objects.get(resource=bot).organization_id == a["org"].id


# ---- API: other organizations' references are refused -------------------------------------------------------------


def test_cannot_set_other_org_technical_owner_or_vendor(a, b, api):
    theirs = Vendor.objects.create(organization=b["org"], name="Zylker")
    response = api(a["users"]["editor"]).patch(
        f"applications/{a['app'].id}", {"technical_owner": str(b["people"]["dana"].id), "vendor": str(theirs.id)}
    )
    assert response.status_code == 400
    assert set(response.json_body["errors"]) == {"technical_owner", "vendor"}
    assert Application.objects.get(resource=a["app"]).vendor_id is None


def test_cannot_put_a_person_in_other_org_department(a, b, api):
    theirs = Department.objects.create(organization=b["org"], name="HR")
    client = api(a["users"]["editor"])
    created = client.post("people", {"name": "Pat Lee", "department": str(theirs.id)})
    assert created.status_code == 400 and "department" in created.json_body["errors"]
    edited = client.patch(f"people/{a['people']['dana'].id}", {"department": str(theirs.id)})
    assert edited.status_code == 400 and "department" in edited.json_body["errors"]
    assert not Person.objects.filter(department=theirs).exists()


def test_import_cannot_match_other_org_record(a, b, api):
    row = {"softwarden_id": str(b["app"].id), "name": "orgb App", "lifecycle": "active"}
    body = {
        "file": SimpleUploadedFile("import.csv", csvio.write_csv(csvio.TEMPLATE[Kind.APPLICATION], [row])),
        "kind": "application",
    }
    response = api(a["users"]["admin"]).client.post("/api/v1/imports", body)
    assert response.status_code == 201
    imported = ImportRow.objects.get(run_id=response.json()["id"])
    assert imported.organization_id == a["org"].id
    assert imported.outcome == "error" and imported.matched_resource_id is None


def test_database_rejects_deliveries_pointing_at_other_org(a, b):
    """A delivery's destinations, reminder and resent delivery are its organization's (composite keys)."""
    from django.utils import timezone

    from notify import channel_services, deliveries
    from notify.models import Delivery, Reminder, TeamsDestination, WebhookDestination
    from tests.test_channels import SIGNING_SECRET, TEAMS_URL, WEBHOOK_URL, _email

    theirs = channel_services.create_destination(
        b["org"], b["users"]["admin"], TeamsDestination, {"name": "B", "url": TEAMS_URL}
    )
    hook = channel_services.create_destination(
        b["org"],
        b["users"]["admin"],
        WebhookDestination,
        {"name": "B", "url": WEBHOOK_URL, "signing_secret": SIGNING_SECRET},
    )
    reminder = Reminder.objects.create(
        organization=b["org"],
        source_type="periodic_review",
        source_id=b["app"].id,
        due_date=timezone.now().date(),
        title="x",
        label="y",
    )
    their_delivery = _email(b["org"], "b")
    base = {"organization": a["org"], "channel": "teams", "subject": "x", "content": {}}
    _rejected(lambda: Delivery.objects.create(dedupe_key="1", teams_destination=theirs, **base))
    _rejected(lambda: Delivery.objects.create(dedupe_key="2", webhook_destination=hook, **base))
    _rejected(lambda: Delivery.objects.create(dedupe_key="3", reminder=reminder, **base))
    _rejected(lambda: Delivery.objects.create(dedupe_key="4", resend_of=their_delivery, **base))
    with pytest.raises(ValueError):
        deliveries.enqueue(a["org"], channel="teams", dedupe_key="5", subject="x", content={}, teams_destination=theirs)


@pytest.mark.parametrize("field", ["membership", "invitation"])
def test_database_rejects_provisioned_user_pointing_at_other_org(a, b, field):
    """SCIM: a provisioned user's membership and invitation are its organization's (composite keys, core 0015)."""
    from catalog import invitations
    from core.models import ScimUser

    def parts(f):
        invitation, _ = invitations.create_invitation(
            f["org"], f["users"]["admin"], {"email": f"new@{f['org'].slug}.test", "role": "viewer"}
        )
        return {
            "membership": Membership.objects.get(user=f["users"]["viewer"], organization=f["org"]),
            "invitation": invitation,
        }

    ours, theirs = parts(a), parts(b)
    _rejected(lambda: ScimUser.objects.create(organization=a["org"], user_name="x", **{field: theirs[field]}))
    ScimUser.objects.create(organization=a["org"], user_name="x", **{field: ours[field]})  # in-org is fine


@pytest.mark.parametrize("field", ["group", "user"])
def test_database_rejects_group_member_from_other_org(a, b, field):
    """SCIM: a group member row's group and user are its organization's (composite keys, core 0015)."""
    from core.models import ScimGroup, ScimGroupMember, ScimUser

    def parts(f):
        return {
            "group": ScimGroup.objects.create(organization=f["org"], display_name="G"),
            "user": ScimUser.objects.create(organization=f["org"], user_name="u"),
        }

    ours, theirs = parts(a), parts(b)
    _rejected(lambda: ScimGroupMember.objects.create(organization=a["org"], **{**ours, field: theirs[field]}))
    ScimGroupMember.objects.create(organization=a["org"], **ours)


# ---- Vendor risk file ------------------------------------------------------------------------------------------------


def _risk_link(f, v):
    from catalog.models import Attachment

    return Attachment.objects.create(organization=f["org"], vendor=v, kind="link", title="T", url="https://x.test/t")


@pytest.mark.parametrize("field", ["vendor", "assurance_attachment", "dpa_attachment", "subprocessor_attachment"])
def test_database_rejects_a_vendor_risk_profile_pointing_at_other_org(a, b, field):
    """A risk profile's vendor and attachments are its organization's (composite keys, catalog 0040)."""
    from catalog.models import VendorRiskProfile

    theirs_vendor = Vendor.objects.create(organization=b["org"], name="Theirs")
    ours = {"vendor": Vendor.objects.create(organization=a["org"], name="Ours")}
    theirs = (
        {"vendor": theirs_vendor, field: _risk_link(b, theirs_vendor)}
        if field != "vendor"
        else {"vendor": theirs_vendor}
    )
    row = {**ours, field: theirs[field]}
    _rejected(lambda: VendorRiskProfile.objects.create(organization=a["org"], **row))
    mine = {**ours, **({field: _risk_link(a, ours["vendor"])} if field != "vendor" else {})}
    VendorRiskProfile.objects.create(organization=a["org"], **mine)  # the same row in-org is fine


@pytest.mark.parametrize("field", ["vendor", "attachment"])
def test_database_rejects_a_vendor_risk_document_pointing_at_other_org(a, b, field):
    """A risk document's vendor and attachment are its organization's (composite keys, catalog 0040)."""
    from catalog.models import VendorRiskDocument

    theirs_vendor = Vendor.objects.create(organization=b["org"], name="Theirs")
    theirs = {"vendor": theirs_vendor, "attachment": _risk_link(b, theirs_vendor)}
    ours = Vendor.objects.create(organization=a["org"], name="Ours")
    row = {"vendor": ours, field: theirs[field]}
    _rejected(lambda: VendorRiskDocument.objects.create(organization=a["org"], kind="soc2", **row))
    VendorRiskDocument.objects.create(organization=a["org"], kind="soc2", vendor=ours, attachment=_risk_link(a, ours))
