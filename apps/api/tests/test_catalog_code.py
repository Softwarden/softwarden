"""Catalog-as-code (catalog_code/, docs/CATALOG_AS_CODE.md): the catalog as YAML files in git.

Export gives one stable file per record; plan diffs a tree against the organization without writing; apply writes it
through the service functions in one transaction, refusing a stale plan; prune deletes what the tree no longer has.
Also: line-numbered errors, YAML and zip safety, limits, capabilities, roles, impersonation, tenancy and the CLI."""

from __future__ import annotations

import io
import json
import threading
import time
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection

from catalog import admin_services, ai, attachments, bulk, customfields, platform_admin, relationship_types, services
from catalog.models import (
    Department,
    Person,
    Relationship,
    Resource,
    SoftwareCategory,
    Tag,
    Team,
    Vendor,
)
from catalog_code import bundle, engine, fmt
from catalog_code.kinds import PersonKind, ResourceKind
from core.models import AuditEvent, Organization
from offboarding import reassign as reassign_module
from tests.conftest import make_org
from tests.test_platform import make_staff, start
from tests.test_problems import is_problem

# ---- fixtures -------------------------------------------------------------------------------------------------------


def build(env):
    """Every kind catalog-as-code holds, with references between them, custom fields, tags, instances and a link."""
    org, admin = env["org"], env["users"]["admin"]
    dana, priya, gone = env["people"]["dana"], env["people"]["priya"], env["people"]["gone"]
    finance = admin_services.create_named(Department, org, admin, {"name": "Finance"})
    admin_services.update_person(org, admin, dana, {"email": "dana@example.test", "title": "Head of apps"})
    admin_services.update_person(org, admin, priya, {"email": "priya@example.test", "department": str(finance.id)})
    admin_services.update_person(org, admin, gone, {"email": "gone@example.test"})
    customfields.create_field(
        org,
        admin,
        {"name": "contract_ref", "label": "Contract ref", "type": "text", "applies_to": ["application", "vendor"]},
    )
    customfields.create_field(
        org, admin, {"name": "go_live", "label": "Go-live", "type": "date", "applies_to": ["component"]}
    )
    customfields.create_field(
        org,
        admin,
        {
            "name": "tier",
            "label": "Tier",
            "type": "select",
            "choices": ["gold", "silver"],
            "applies_to": ["application", "ai_agent"],
            "weight": 10,
        },
    )
    relationship_types.create_type(
        org, admin, {"slug": "feeds_reports", "name": "Feeds reports", "inverse_name": "Reports from"}
    )
    team = env["team"]
    admin_services.update_named(
        Team, org, admin, team, {"description": "First line\nSecond line", "members": [str(dana.id), str(priya.id)]}
    )
    vendor = admin_services.create_named(
        Vendor,
        org,
        admin,
        {
            "name": "Contoso",
            "website": "https://contoso.example",
            "account_manager": str(priya.id),
            "custom_field_data": {"contract_ref": "C-1"},
        },
    )
    provider = ai.create_provider(org, admin, {"name": "Anthropic", "website": "https://anthropic.example"})
    model = ai.create_model(
        org, admin, {"provider": str(provider.id), "name": "Claude", "version": "5.5", "modality": "multimodal"}
    )
    category = SoftwareCategory.objects.get(organization=org, name="CRM and sales")
    crm = services.create_resource(
        org,
        admin,
        {
            "kind": "application",
            "name": "CRM",
            "lifecycle": "active",
            "criticality": "high",
            "description": "Customers.\nAll of them.",
            "business_owner": str(dana.id),
            "technical_owner": str(priya.id),
            "support_team": str(team.id),
            "department": str(finance.id),
            "vendor": str(vendor.id),
            "category": str(category.id),
            "tags": ["Customer", "tier-1"],
            "sso_enabled": True,
            "version": "10.2",
            "entra_app_id": "AAAAAAAA-0000-0000-0000-000000000001",
            "custom_field_data": {"contract_ref": "C-42", "tier": "gold"},
        },
    )
    prod = services.create_instance(
        org,
        admin,
        crm,
        {"name": "Production", "environment": "prod", "url": "https://crm.example", "status": "operational"},
    )
    services.create_instance(org, admin, crm, {"name": "Sandbox"})
    payroll = services.create_resource(
        org, admin, {"kind": "application", "name": "Payroll", "lifecycle": "deprecated"}
    )
    sync = services.create_resource(
        org,
        admin,
        {
            "kind": "component",
            "name": "Nightly sync",
            "lifecycle": "active",
            "component_type": "script",
            "technical_owner": str(priya.id),
            "custom_field_data": {"go_live": "2026-01-31"},
            "tags": ["tier-1"],
        },
    )
    bot = services.create_resource(
        org,
        admin,
        {
            "kind": "ai_agent",
            "name": "Helpdesk bot",
            "lifecycle": "evaluating",
            "platform": "claude",
            "models_used": [str(model.id)],
            "channels": ["web", "teams"],
            "human_in_loop": False,
            "custom_field_data": {"tier": "silver"},
        },
    )
    flow = services.create_integration(
        org,
        admin,
        {
            "name": "CRM to Payroll",
            "lifecycle": "active",
            "source": str(crm.id),
            "source_instance": str(prod.id),
            "target": str(payroll.id),
            "protocol": "SFTP",
            "direction": "one_way",
            "data_classification": "confidential",
            "data_categories": ["financial", "personal"],  # data-flow map; stored in choice order
        },
    )
    services.archive_resource(org, admin, payroll)
    services.create_relationship(
        org,
        admin,
        {
            "type": "depends_on",
            "source": str(sync.id),
            "target": str(crm.id),
            "target_instance": str(prod.id),
            "purpose": "Reads accounts",
        },
    )
    services.create_relationship(org, admin, {"type": "implements", "source": str(sync.id), "target": str(flow.id)})
    services.create_relationship(org, admin, {"type": "feeds_reports", "source": str(bot.id), "target": str(crm.id)})
    tag = Tag.objects.get(organization=org, name="Customer")
    admin_services.update_tag(org, admin, tag, {"color": "blue", "description": "Customer-facing"})
    attachments.add_link(
        org,
        admin,
        crm,
        {"kind": "link", "url": "https://wiki.example/crm", "title": "Runbook", "category": "sop_runbook"},
    )
    return {"crm": crm, "prod": prod, "payroll": payroll, "sync": sync, "bot": bot, "flow": flow, "vendor": vendor}


@pytest.fixture
def rich(a):
    return {**a, **build(a)}


def fresh_org(name="Fresh"):
    return Organization.objects.create(name=name)


def upload(files: dict[str, bytes], name="catalog.zip"):
    return SimpleUploadedFile(name, bundle.write_zip(files), content_type="application/zip")


def post_tree(client, route, files, **fields):
    body = {"file": upload(files) if isinstance(files, dict) else files, **fields}
    response = client.client.post(f"/api/v1/catalog-code/{route}", body)
    response.json_body = response.json() if "json" in response.get("Content-Type", "") else None
    return response


def doc(files, path):
    value, _ = fmt.load(files[path])
    return value


def put(files, path, value):
    return {**files, path: fmt.dump(value)}


# ---- the format -----------------------------------------------------------------------------------------------------

GOLDEN_CRM = """\
apiVersion: softwarden/v1
kind: Application
metadata:
  slug: crm
  name: CRM
  description: |-
    Customers.
    All of them.
  tags:
    - Customer
    - tier-1
spec:
  lifecycle: active
  criticality: high
  businessOwner: dana@example.test
  technicalOwner: priya@example.test
  supportTeam: IT Applications
  department: Finance
  vendor: Contoso
  category: CRM and sales
  version: '10.2'
  ssoEnabled: true
  entraAppId: aaaaaaaa-0000-0000-0000-000000000001
  instances:
    - name: Production
      environment: prod
      url: https://crm.example
      status: operational
    - name: Sandbox
  customFields:
    contract_ref: C-42
    tier: gold
status:
  attachments:
    - title: Runbook
      category: sop_runbook
      kind: link
      url: https://wiki.example/crm
"""

GOLDEN_RELATIONSHIP = """\
apiVersion: softwarden/v1
kind: Relationship
spec:
  type: depends_on
  source: component:nightly-sync
  target: application:crm
  targetInstance: Production
  purpose: Reads accounts
"""


GOLDEN_FLOW = """\
apiVersion: softwarden/v1
kind: Integration
metadata:
  slug: crm-to-payroll
  name: CRM to Payroll
spec:
  lifecycle: active
  source: application:crm
  sourceInstance: Production
  target: application:payroll
  dataClassification: confidential
  dataCategories:
    - personal
    - financial
  protocol: SFTP
  direction: one_way
"""


def test_export_matches_the_golden_files_and_is_byte_identical(rich):
    files = engine.render_tree(rich["org"], include_people=True)
    assert files["applications/crm.yaml"].decode() == GOLDEN_CRM
    assert files["integrations/crm-to-payroll.yaml"].decode() == GOLDEN_FLOW
    assert files["relationships/component-nightly-sync--depends-on--application-crm--production.yaml"].decode() == (
        GOLDEN_RELATIONSHIP
    )
    assert engine.render_tree(rich["org"], include_people=True) == files
    assert bundle.write_zip(files) == bundle.write_zip(engine.render_tree(rich["org"], include_people=True))
    manifest = doc(files, "catalog.yaml")
    assert manifest["kind"] == "Catalog" and manifest["spec"]["includesPeople"] is True
    assert "Person" in manifest["spec"]["kinds"] and "Application" in manifest["spec"]["kinds"]
    folders = {path.split("/")[0] for path in files if "/" in path}
    assert folders == {
        "applications",
        "components",
        "ai-agents",
        "integrations",
        "relationships",
        "relationship-types",
        "vendors",
        "teams",
        "people",
        "departments",
        "categories",
        "tags",
        "custom-fields",
        "ai-providers",
        "ai-models",
    }


def test_people_are_exported_only_when_asked(rich):
    files = engine.render_tree(rich["org"])
    assert not any(path.startswith("people/") for path in files)
    crm = doc(files, "applications/crm.yaml")
    assert crm["spec"]["businessOwner"] == str(rich["people"]["dana"].id)  # a Softwarden ID, not an email
    assert b"@example.test" not in b"".join(files.values())
    assert doc(files, "catalog.yaml")["spec"]["includesPeople"] is False
    people = engine.render_tree(rich["org"], include_people=True)
    assert doc(people, "people/dana-example-test.yaml") == {
        "apiVersion": "softwarden/v1",
        "kind": "Person",
        "metadata": {"email": "dana@example.test", "name": "Dana Ortiz"},
        "spec": {"title": "Head of apps"},
    }
    assert doc(people, "people/gone-example-test.yaml")["spec"] == {"active": False}


def test_no_secret_ever_leaves(rich):
    """Connector credentials, tokens and attachment files are never in a tree; attachments are metadata only."""
    from core import connections

    connections.create_connection(
        rich["org"],
        rich["users"]["admin"],
        connections.MICROSOFT,
        {
            "name": "Tenant",
            "tenant_id": "8c3dcbf0-7a3b-4f43-9a3e-1b2c3d4e5f60",
            "client_id": "1d2c3b4a-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
            "secret": "s3cret-value",
        },
    )
    files = engine.render_tree(rich["org"], include_people=True)
    blob = b"".join(files.values()).lower()
    for word in (b"s3cret", b"password", b"token", b"ciphertext", b"storage_name", b"attachments/"):
        assert word not in blob, word


# ---- round trip -----------------------------------------------------------------------------------------------------


def test_export_then_apply_on_a_fresh_organization_reproduces_it(rich):
    files = engine.render_tree(rich["org"], include_people=True)
    fresh = fresh_org()
    result = engine.execute(fresh, None, files, prune=True, apply=True)
    assert result["errors"] == [] and result["applied"] is True, result["errors"]
    again = engine.render_tree(fresh, include_people=True)
    # Byte for byte, except the read-only attachment metadata: apply never makes attachments.
    crm = doc(files, "applications/crm.yaml")
    assert crm.pop("status") == {
        "attachments": [
            {"title": "Runbook", "category": "sop_runbook", "kind": "link", "url": "https://wiki.example/crm"}
        ]
    }
    assert again == {**files, "applications/crm.yaml": fmt.dump(crm)}
    # The records and links are there, scoped to the fresh organization.
    assert (
        Resource.objects.filter(organization=fresh).count() == Resource.objects.filter(organization=rich["org"]).count()
    )
    assert Relationship.objects.filter(organization=fresh).count() == 3
    owner = Resource.objects.get(organization=fresh, kind="application", slug="crm").business_owner
    assert owner is not None and owner.organization_id == fresh.id and owner.email == "dana@example.test"
    assert Resource.objects.get(organization=fresh, slug="payroll").archived_at is not None
    # A second apply changes nothing.
    second = engine.execute(fresh, None, files, prune=True, apply=True)
    assert second["summary"]["create"] == second["summary"]["update"] == second["summary"]["delete"] == 0


def test_an_integrations_data_categories_plan_and_apply(rich):
    """Data-flow map: data categories are carried by export and apply, kept in choice order, and an unknown
    category is the file's error."""
    org = rich["org"]
    path = "integrations/crm-to-payroll.yaml"
    files = engine.render_tree(org)
    flow = doc(files, path)
    flow["spec"]["dataCategories"] = ["credentials", "personal", "credentials"]
    result = engine.execute(org, None, put(files, path, flow), apply=True)
    assert result["errors"] == [] and result["applied"] is True, result["errors"]
    [update] = [c for c in result["changes"] if c["file"] == path]
    fields = {f["path"]: (f["before"], f["after"]) for f in update["fields"]}
    assert fields == {"spec.dataCategories": (["personal", "financial"], ["personal", "credentials"])}
    stored = Resource.objects.get(id=rich["flow"].id).integration
    assert stored.data_categories == ["personal", "credentials"]
    event = AuditEvent.objects.filter(action="resource.updated", target_id=rich["flow"].id).latest("created_at")
    assert event.changes["data_categories"] == [["personal", "financial"], ["personal", "credentials"]]
    del flow["spec"]["dataCategories"]  # left out: none
    assert engine.execute(org, None, put(files, path, flow), apply=True)["applied"]
    assert Resource.objects.get(id=rich["flow"].id).integration.data_categories == []
    flow["spec"]["dataCategories"] = ["gossip"]
    bad = engine.execute(org, None, put(files, path, flow))
    assert [(e["file"], e["path"]) for e in bad["errors"]] == [(path, "spec.dataCategories")]
    assert "Use any of: personal, special_category" in bad["errors"][0]["message"]


def test_a_tree_applies_to_its_own_organization_as_unchanged(rich):
    files = engine.render_tree(rich["org"])
    result = engine.execute(rich["org"], None, files, prune=True)
    assert result["errors"] == [] and result["changes"] == [], result["changes"]
    assert result["summary"]["unchanged"] == result["summary"]["files"] - 1  # every file but the manifest


def test_people_without_an_email_resolve_within_one_apply(a):
    """A Person file keyed by Softwarden ID (no email) is created in another organization, and the records naming
    that ID point at the new person."""
    org, admin = a["org"], a["users"]["admin"]
    services.update_resource(org, admin, a["app"], {"business_owner": str(a["people"]["dana"].id)})
    files = engine.render_tree(org, include_people=True)
    dana = f"people/{a['people']['dana'].id}.yaml"
    assert doc(files, dana)["metadata"] == {"id": str(a["people"]["dana"].id), "name": "Dana Ortiz"}
    fresh = fresh_org()
    result = engine.execute(fresh, None, files, apply=True)
    assert result["applied"], result["errors"]
    owner = Resource.objects.get(organization=fresh, slug=a["app"].slug).business_owner
    assert owner is not None and owner.name == "Dana Ortiz" and owner.organization_id == fresh.id


# ---- plan -----------------------------------------------------------------------------------------------------------


def edited_tree(org):
    """The organization's tree with CRM changed, a new application, and the nightly sync's files removed."""
    files = engine.render_tree(org)
    crm = doc(files, "applications/crm.yaml")
    crm["spec"]["lifecycle"] = "deprecated"
    crm["metadata"]["tags"] = ["Customer"]
    crm["spec"]["customFields"]["tier"] = "silver"
    crm["spec"]["instances"] = [crm["spec"]["instances"][0], {"name": "Staging", "environment": "test"}]
    files = put(files, "applications/crm.yaml", crm)
    new = {
        "apiVersion": "softwarden/v1",
        "kind": "Application",
        "metadata": {"slug": "new-app", "name": "New app"},
        "spec": {"lifecycle": "proposed", "vendor": "Contoso"},
    }
    files = put(files, "applications/new-app.yaml", new)
    return {p: v for p, v in files.items() if "nightly-sync" not in p}


def test_plan_shows_each_field_change_and_writes_nothing(rich):
    org = rich["org"]
    files = edited_tree(org)
    events = AuditEvent.objects.count()
    result = engine.execute(org, None, files)
    assert result["errors"] == [] and result["applied"] is False
    assert AuditEvent.objects.count() == events  # rehearsed, then rolled back
    crm = Resource.objects.get(id=rich["crm"].id)
    assert crm.lifecycle == "active" and not Resource.objects.filter(organization=org, slug="new-app").exists()
    changes = {(c["kind"], c["key"]): c for c in result["changes"]}
    update = changes[("Application", "application:crm")]
    assert update["action"] == "update" and update["file"] == "applications/crm.yaml"
    fields = {f["path"]: (f["before"], f["after"]) for f in update["fields"]}
    assert fields["spec.lifecycle"] == ("active", "deprecated")
    assert fields["metadata.tags"] == (["Customer", "tier-1"], ["Customer"])
    assert fields["spec.customFields.tier"] == ("gold", "silver")
    assert fields["spec.instances[Staging]"] == (None, {"name": "Staging", "environment": "test"})
    assert "spec.instances[Sandbox]" not in fields  # kept without prune, with a warning
    assert [w["message"] for w in result["warnings"]] == [
        "The instance Sandbox isn't in the file. It is kept; apply with prune to delete it."
    ]
    create = changes[("Application", "application:new-app")]
    assert create["action"] == "create"
    assert {f["path"]: f["after"] for f in create["fields"]} == {
        "metadata.slug": "new-app",
        "metadata.name": "New app",
        "spec.lifecycle": "proposed",
        "spec.vendor": "Contoso",
    }
    assert result["summary"]["delete"] == 0 and result["summary"]["update"] == 1 and result["summary"]["create"] == 1


def test_prune_plans_and_applies_deletes_for_the_kinds_in_the_tree(rich):
    org = rich["org"]
    files = edited_tree(org)
    # The nightly sync is the catalog's only component: deleting it empties the kind, which the prune cap refuses
    # without allow_mass_delete.
    capped = engine.execute(org, None, files, prune=True)
    assert [e["mass_delete"] for e in capped["errors"]] == [3] and "Component 1 of 1" in capped["errors"][0]["message"]
    plan = engine.execute(org, None, files, prune=True, allow_mass_delete=True)
    assert plan["errors"] == [], plan["errors"]
    deletes = [(c["kind"], c["key"]) for c in plan["changes"] if c["action"] == "delete"]
    assert ("Component", "component:nightly-sync") in deletes
    assert sum(1 for kind, _ in deletes if kind == "Relationship") == 2  # its two links go first
    crm = next(c for c in plan["changes"] if c["key"] == "application:crm")
    assert {"path": "spec.instances[Sandbox]", "before": {"name": "Sandbox"}, "after": None} in crm["fields"]
    result = engine.execute(
        org, None, files, prune=True, apply=True, expected_hash=plan["plan_hash"], allow_mass_delete=True
    )
    assert result["applied"], result["errors"]
    assert not Resource.objects.filter(id=rich["sync"].id).exists()
    assert not rich["crm"].instances.filter(name="Sandbox").exists()
    assert AuditEvent.objects.filter(organization=org, action="resource.deleted", target_id=rich["sync"].id).exists()
    # People and tags are never pruned, and a tree without catalog.yaml prunes only the kinds it has files for.
    assert Person.objects.filter(organization=org).count() == 3
    only_apps = {p: v for p, v in engine.render_tree(org).items() if p.startswith("applications/")}
    narrow = engine.execute(org, None, only_apps, prune=True)
    assert {c["kind"] for c in narrow["changes"] if c["action"] == "delete"} <= {"Application"}


def test_prune_refusals_are_errors_and_stop_the_apply(rich):
    """A record something else still references (a review) can't be deleted; the apply writes nothing."""
    org, admin = rich["org"], rich["users"]["admin"]
    services.record_review(
        org, admin, rich["sync"], {"confirmed_by": str(rich["people"]["dana"].id), "evidence": "Asked in Teams"}
    )
    files = edited_tree(org)
    plan = engine.execute(org, None, files, prune=True)
    assert any("nightly-sync" in (e.get("key") or "") and "review" in e["message"] for e in plan["errors"])
    result = engine.execute(org, None, files, prune=True, apply=True, expected_hash=plan["plan_hash"])
    assert result["applied"] is False
    assert Resource.objects.get(id=rich["crm"].id).lifecycle == "active"  # nothing applied


def many_apps(org, admin, count=12):
    for i in range(count):
        services.create_resource(org, admin, {"kind": "application", "name": f"Bulk {i:02d}", "lifecycle": "active"})


def test_a_manifest_only_tree_with_prune_is_refused_with_the_count(a, api):
    """A bad checkout, a sparse clone or a truncated zip (only catalog.yaml) would plan to delete every record of
    the manifest's kinds. A prune past the cap is an error that names the count, so nothing is deleted, unless the
    caller says allow_mass_delete."""
    org, admin = a["org"], a["users"]["admin"]
    many_apps(org, admin)
    apps = Resource.objects.filter(organization=org, kind="application").count()
    records = Resource.objects.filter(organization=org).count()
    only = {"catalog.yaml": engine.render_tree(org)["catalog.yaml"]}
    plan = engine.execute(org, None, only, prune=True)
    deletes = plan["summary"]["delete"]
    assert deletes >= apps > engine.MASS_DELETE_MAX
    [refusal] = [e for e in plan["errors"] if e.get("mass_delete")]
    assert f"deletes {deletes} records" in refusal["message"] and f"Application {apps} of {apps}" in refusal["message"]
    assert "allow_mass_delete" in refusal["message"]
    client = api(admin)
    body = is_problem(
        post_tree(client, "apply", only, prune="true", plan_hash=plan["plan_hash"]),
        400,
        "/problems/catalog-code-invalid",
    )
    assert any(f"deletes {deletes} records" in e["message"] for e in body["file_errors"])
    assert Resource.objects.filter(organization=org).count() == records  # nothing deleted
    # The plan says the same over the API, and with the flag the refusal is gone and the apply deletes.
    planned = post_tree(client, "plan", only, prune="true").json_body
    assert any(e.get("mass_delete") for e in planned["errors"])
    allowed = post_tree(client, "plan", only, prune="true", allow_mass_delete="true").json_body
    assert not any(e.get("mass_delete") for e in allowed["errors"]), allowed["errors"]
    response = post_tree(client, "apply", only, prune="true", allow_mass_delete="true", plan_hash=allowed["plan_hash"])
    assert response.status_code == 200, response.json_body
    assert response.json_body["applied"] is True
    assert not Resource.objects.filter(organization=org, kind="application").exists()
    applied = AuditEvent.objects.get(organization=org, action="catalog_code.applied")
    assert applied.changes["allow_mass_delete"] is True and applied.changes["delete"] == deletes


def test_a_prune_that_takes_most_of_a_small_kind_is_refused(a):
    """Past the share cap (20% of a kind) with only a few records: three of four applications without a file."""
    org, admin = a["org"], a["users"]["admin"]
    Resource.objects.filter(organization=org).delete()
    many_apps(org, admin, 4)
    files = engine.render_tree(org)
    kept = [p for p in files if p.startswith("applications/")][0]
    tree = {p: v for p, v in files.items() if not p.startswith("applications/") or p == kept}
    plan = engine.execute(org, None, tree, prune=True)
    assert plan["summary"]["delete"] == 3
    assert any(e.get("mass_delete") and "Application 3 of 4" in e["message"] for e in plan["errors"])
    assert engine.execute(org, None, tree, prune=True, allow_mass_delete=True)["errors"] == []


def test_a_prune_that_empties_a_tiny_kind_is_refused(a):
    """Below the share floor, but the kind would be left with nothing: both of two applications without a file."""
    org, admin = a["org"], a["users"]["admin"]
    Resource.objects.filter(organization=org).delete()
    many_apps(org, admin, 2)
    # catalog.yaml names the kinds, so Application is pruned though no application file is left.
    tree = {p: v for p, v in engine.render_tree(org).items() if not p.startswith("applications/")}
    plan = engine.execute(org, None, tree, prune=True)
    assert plan["summary"]["delete"] == 2
    assert any(e.get("mass_delete") and "Application 2 of 2" in e["message"] for e in plan["errors"]), plan["errors"]
    assert engine.execute(org, None, tree, prune=True, allow_mass_delete=True)["errors"] == []
    # One of two leaves the kind a record: below every threshold, no refusal.
    files = engine.render_tree(org)
    first = sorted(p for p in files if p.startswith("applications/"))[0]
    one_left = {p: v for p, v in files.items() if not p.startswith("applications/") or p == first}
    assert not any(e.get("mass_delete") for e in engine.execute(org, None, one_left, prune=True)["errors"])


def test_the_cli_refuses_a_mass_delete_without_its_flag(a, tmp_path):
    org, admin = a["org"], a["users"]["admin"]
    many_apps(org, admin)
    (tmp_path / "catalog.yaml").write_bytes(engine.render_tree(org)["catalog.yaml"])
    before = Resource.objects.filter(organization=org).count()
    err = io.StringIO()
    with pytest.raises(CommandError, match="nothing was applied"):
        call_command(
            "catalog_apply", "--org", org.slug, "--dir", str(tmp_path), "--prune", "--no-plan-check",
            stdout=io.StringIO(), stderr=err,
        )  # fmt: skip
    assert "--allow-mass-delete" in err.getvalue()
    assert Resource.objects.filter(organization=org).count() == before
    out = io.StringIO()
    call_command(
        "catalog_apply", "--org", org.slug, "--dir", str(tmp_path), "--prune", "--no-plan-check",
        "--allow-mass-delete", stdout=out,
    )  # fmt: skip
    assert "Applied." in out.getvalue()
    assert not Resource.objects.filter(organization=org, kind="application").exists()


# ---- apply ----------------------------------------------------------------------------------------------------------


def test_apply_writes_through_the_services_and_audits_via_catalog_code(rich, api):
    org, admin = rich["org"], rich["users"]["admin"]
    files = edited_tree(org)
    client = api(admin)
    plan = post_tree(client, "plan", files)
    assert plan.status_code == 200 and plan.json_body["applied"] is False
    response = post_tree(client, "apply", files, plan_hash=plan.json_body["plan_hash"])
    assert response.status_code == 200, response.json_body
    assert response.json_body["applied"] is True and response.json_body["summary"]["create"] == 1
    crm = Resource.objects.get(id=rich["crm"].id)
    assert crm.lifecycle == "deprecated" and crm.custom_field_data["tier"] == "silver"
    assert sorted(crm.instances.values_list("name", flat=True)) == ["Production", "Sandbox", "Staging"]
    event = AuditEvent.objects.filter(organization=org, action="resource.updated", target_id=crm.id).latest(
        "created_at"
    )
    assert event.actor == admin and event.changes["via"] == "catalog_code"
    assert event.changes["lifecycle"] == ["active", "deprecated"]
    new = Resource.objects.get(organization=org, kind="application", slug="new-app")
    assert new.it_steward == admin and new.application.vendor_id == rich["vendor"].id
    created = AuditEvent.objects.get(organization=org, action="resource.created", target_id=new.id)
    assert created.changes["via"] == "catalog_code"
    instance = AuditEvent.objects.get(organization=org, action="instance.created", changes__name="Staging")
    assert instance.changes["via"] == "catalog_code"
    summary = AuditEvent.objects.get(organization=org, action="catalog_code.applied")
    assert summary.actor == admin and summary.changes["create"] == 1 and summary.changes["interface"] == "api"


def test_apply_never_writes_discovery_observations(rich):
    """Discovery's observations of a record stay exactly as the source left them when a tree edits the record."""
    from discovery import engine as discovery
    from discovery.models import Observation, ReconciliationDecision
    from tests.test_discovery import make_connector

    org, admin = rich["org"], rich["users"]["admin"]
    connector = make_connector(org, admin)
    discovery.run(connector)
    discovery.link(
        org,
        admin,
        Observation.objects.get(connector=connector, display_name="Workday"),
        {"resource": str(rich["crm"].id)},
    )
    observations = list(Observation.objects.order_by("id").values())
    decisions = ReconciliationDecision.objects.count()
    result = engine.execute(org, admin, edited_tree(org), apply=True)
    assert result["applied"], result["errors"]
    assert list(Observation.objects.order_by("id").values()) == observations
    assert ReconciliationDecision.objects.count() == decisions


def test_a_stale_plan_is_refused(rich, api):
    org, admin = rich["org"], rich["users"]["admin"]
    files = edited_tree(org)
    plan = engine.execute(org, None, files)
    services.update_resource(org, admin, rich["crm"], {"criticality": "low"})  # someone edits meanwhile
    with pytest.raises(engine.Stale):
        engine.execute(org, None, files, apply=True, expected_hash=plan["plan_hash"])
    response = post_tree(api(admin), "apply", files, plan_hash=plan["plan_hash"])
    body = is_problem(response, 409, "/problems/catalog-code-stale")
    assert "Plan again" in body["detail"]
    assert Resource.objects.get(id=rich["crm"].id).lifecycle == "active"
    fresh = engine.execute(org, None, files)
    # Other files, or another prune flag, with the same hash are stale too.
    other = {**doc(files, "applications/new-app.yaml"), "metadata": {"slug": "other", "name": "Other"}}
    changed = put(files, "applications/other.yaml", other)
    for tree, prune in ((changed, False), (files, True)):
        with pytest.raises(engine.Stale):
            engine.execute(org, None, tree, prune=prune, apply=True, expected_hash=fresh["plan_hash"])
    assert engine.execute(org, None, files, apply=True, expected_hash=fresh["plan_hash"])["applied"]
    assert post_tree(api(admin), "apply", files).status_code == 400  # plan_hash is required


# ---- errors with their file and line --------------------------------------------------------------------------------

HEAD = "apiVersion: softwarden/v1\nkind: Application\nmetadata:\n  slug: {slug}\n  name: {name}\nspec:\n"


def app_file(slug, *spec_lines, name=None):
    text = HEAD.format(slug=slug, name=name or slug.title()) + "".join(f"  {line}\n" for line in spec_lines)
    return text.encode()


def test_errors_name_the_file_line_and_field(rich, api):
    org = rich["org"]
    files = {
        "a.yaml": b"apiVersion: softwarden/v1\nkind: Widget\n",
        "b.yaml": app_file("bee", "lifecycle: active", "colour: red"),
        "c.yaml": app_file("cee", "lifecycle: sometimes"),
        "d.yaml": app_file("dee", "lifecycle: active", "vendor: Nobody"),
        "e.yaml": b"apiVersion: softwarden/v1\nkind: Application\nspec: [\n",
        "f.yaml": app_file("eff", "lifecycle: active", "lifecycle: retired"),
        "g.yaml": b"apiVersion: softwarden/v1\nkind: Application\nmetadata:\n  name: No slug\nspec:\n  lifecycle: active\n",
        "h.yaml": b"apiVersion: softwarden/v2\nkind: Application\n",
        "i.yaml": app_file("Not-A-Slug", "lifecycle: active"),
        "j.yaml": app_file("jay", "lifecycle: active", "version: 10.2"),
        "k.yaml": app_file("crm", "lifecycle: active", name="CRM"),
        "l.yaml": app_file("crm", "lifecycle: active", name="CRM again"),
        "m.yaml": app_file("em", "lifecycle: active", "businessOwner: nobody@example.test"),
        "n.yaml": app_file("en", "lifecycle: active", "documentationUrl: not a url"),
        "o.yaml": app_file("oh", "lifecycle: active", "customFields:", "  tier: platinum", "  nope: 1"),
    }
    result = engine.execute(org, None, files)
    found = {(e["file"], e["line"], e["path"]): e["message"] for e in result["errors"]}
    assert found[("a.yaml", 2, "kind")].startswith("Unknown kind 'Widget'")
    assert found[("b.yaml", 8, "spec.colour")].startswith("Unknown field colour for Application")
    assert found[("c.yaml", 7, "spec.lifecycle")] == "Choose a lifecycle status."  # the service's own check
    assert found[("d.yaml", 8, "spec.vendor")] == 'No vendor named "Nobody" is in this organization or these files.'
    assert any(f == "e.yaml" and line == 4 for f, line, _ in found)  # a YAML syntax error, at its line
    assert found[("f.yaml", 8, None)] == '"lifecycle" appears twice.'
    assert found[("g.yaml", 3, "metadata.slug")] == "Add metadata.slug."
    assert found[("h.yaml", 1, "apiVersion")] == "Set apiVersion: softwarden/v1."
    assert "lower case" in found[("i.yaml", 4, "metadata.slug")]
    assert found[("j.yaml", 8, "spec.version")] == "Put 10.2 in quotes: this field is text."
    assert found[("l.yaml", 2, "kind")] == "This is the same Application as k.yaml."
    assert found[("m.yaml", 8, "spec.businessOwner")].startswith('No person "nobody@example.test"')
    assert found[("n.yaml", 8, "spec.documentationUrl")].startswith("Enter a full web address")
    assert found[("o.yaml", 9, "spec.customFields.tier")] == "Tier: Choose one of: gold, silver."
    assert found[("o.yaml", 10, "spec.customFields.nope")].startswith("There is no custom field nope")
    # Apply refuses the whole tree with the same list.
    response = post_tree(api(rich["users"]["admin"]), "apply", files, plan_hash=result["plan_hash"])
    body = is_problem(response, 400, "/problems/catalog-code-invalid")
    assert len(body["file_errors"]) == len(result["errors"])
    assert not Resource.objects.filter(organization=org, slug__in=["dee", "jay", "em"]).exists()


# ---- YAML and zip safety --------------------------------------------------------------------------------------------

BOMBS = {
    "alias": b"a: &x [1, 2]\nb: *x\n",
    "laughs": b"a: [lol]\nb: &b [lol, lol]\nc: [*b, *b]\n",
    "anchor only": b"apiVersion: &v softwarden/v1\n",
    "merge": b"base: {a: 1}\nother:\n  <<: {b: 2}\n",
    "python": b"x: !!python/object/apply:os.system ['echo hi']\n",
    "binary": b"x: !!binary aGVsbG8=\n",
    "custom tag": b"x: !secret value\n",
    "deep": b"x: " + b"[" * 40 + b"]" * 40 + b"\n",
    "two documents": b"a: 1\n---\nb: 2\n",
    # Malformed scalars: each is a file error, never a server error.
    "huge int": b"x: " + b"9" * 5000 + b"\n",
    "bad int": b"x: !!int abc\n",
    "bad hex": b"x: !!int 0x_\n",
    "empty int": b"x: !!int\n",
    "bad float": b"x: !!float abc\n",
    "bad bool": b"x: !!bool maybe\n",
    "nan": b"x: .nan\n",
    "inf": b"x: .inf\n",
    "minus inf": b"x: -.inf\n",
    "overflowing float": b"x: 1.0e+999\n",  # YAML 1.1 needs the dot and the sign to read it as a float
}


@pytest.mark.parametrize("name", sorted(BOMBS))
def test_unsafe_yaml_is_refused(name):
    with pytest.raises(fmt.YamlError):
        fmt.load(BOMBS[name])


def test_unsafe_yaml_is_a_file_error_with_its_line(rich):
    result = engine.execute(rich["org"], None, {"x.yaml": BOMBS["laughs"], "big.yaml": b"#" * (fmt.MAX_FILE_BYTES + 1)})
    messages = {e["file"]: (e["line"], e["message"]) for e in result["errors"]}
    assert messages["x.yaml"] == (2, "Anchors and aliases (& and *) aren't allowed; write each value out.")
    assert messages["big.yaml"][1] == "The file is larger than 512 KB."


def make_zip(entries, symlink=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16
            archive.writestr(info, "/etc/passwd")
    buffer.seek(0)
    return buffer


@pytest.mark.parametrize(
    "entries",
    [
        {"../evil.yaml": b"a: 1"},
        {"applications/../../evil.yaml": b"a: 1"},
        {"/abs.yaml": b"a: 1"},
        {"..\\evil.yaml": b"a: 1"},  # zipfile reads a backslash as a separator on Windows; refused either way
        {"C:/x.yaml": b"a: 1"},
        {"A.yaml": b"a: 1", "a.yaml": b"a: 1"},
        {"/".join(["d"] * 9) + "/x.yaml": b"a: 1"},
    ],
)
def test_unsafe_zip_paths_are_refused(entries):
    with pytest.raises(bundle.BundleRejected):
        bundle.read_zip(make_zip(entries))


def test_zip_links_bombs_and_limits_are_refused(monkeypatch):
    with pytest.raises(bundle.BundleRejected, match="symbolic link"):
        bundle.read_zip(make_zip({}, symlink="applications/link.yaml"))
    with pytest.raises(bundle.BundleRejected, match="zip archive"):
        bundle.read_zip(io.BytesIO(b"not a zip at all"))
    # A file that inflates past the per-file limit is that file's error, read no further than the limit.
    bomb = bundle.read_zip(make_zip({"bomb.yaml": b"0" * (20 * fmt.MAX_FILE_BYTES)}))
    assert bomb == {"bomb.yaml": bundle.TOO_LARGE}
    monkeypatch.setattr(bundle, "MAX_ENTRIES", 3)
    with pytest.raises(bundle.BundleRejected, match="too many entries"):
        bundle.read_zip(make_zip({f"{i}.yaml": b"a: 1" for i in range(5)}))
    monkeypatch.setattr(bundle, "MAX_ENTRIES", 25_000)
    monkeypatch.setattr(bundle, "MAX_TOTAL_BYTES", 10)
    with pytest.raises(bundle.BundleRejected, match="add up to more"):
        bundle.read_zip(make_zip({"a.yaml": b"a: 123456", "b.yaml": b"b: 123456"}))
    # An oversize file's inflated bytes count toward the total too.
    monkeypatch.setattr(bundle, "MAX_TOTAL_BYTES", fmt.MAX_FILE_BYTES + 10)
    with pytest.raises(bundle.BundleRejected, match="add up to more"):
        bundle.read_zip(make_zip({"big.yaml": b"0" * (2 * fmt.MAX_FILE_BYTES), "small.yaml": b"a: 123456789"}))
    monkeypatch.setattr(bundle, "MAX_TOTAL_BYTES", 16 * bundle.MB)
    monkeypatch.setattr(bundle, "MAX_FILES", 1)
    with pytest.raises(bundle.BundleRejected, match="more than 1 YAML"):
        bundle.read_zip(make_zip({"a.yaml": b"a: 1", "b.yaml": b"b: 1"}))


def test_other_files_and_hidden_folders_are_ignored():
    files = bundle.read_zip(
        make_zip({"README.md": b"# hi", ".git/config": b"x", ".github/w.yaml": b"x", "repo-main/apps/a.yml": b"a: 1"})
    )
    assert files == {"repo-main/apps/a.yml": b"a: 1"}


def test_the_api_refuses_an_unsafe_zip(rich, api):
    client = api(rich["users"]["admin"])
    upload_file = SimpleUploadedFile("t.zip", make_zip({"../evil.yaml": b"a: 1"}).getvalue(), "application/zip")
    body = is_problem(post_tree(client, "plan", upload_file), 400, "/problems/invalid-request")
    assert "unsafe path" in body["errors"]["file"][0]
    response = client.client.post("/api/v1/catalog-code/plan", {"prune": "true"})
    assert is_problem(response, 400, "/problems/invalid-request")["errors"]["file"]


# ---- limits, capabilities, roles, impersonation ---------------------------------------------------------------------


def test_the_records_limit_is_checked_for_the_whole_tree(rich, api):
    org = rich["org"]
    staff = make_staff()
    used = Resource.objects.filter(organization=org).count()
    platform_admin.set_limit(staff, org, {"limit": "records", "value": used + 1, "reason": "Trial"})
    files = engine.render_tree(org)
    for slug in ("one", "two"):
        files = put(files, f"applications/{slug}.yaml", fmt.load(app_file(slug, "lifecycle: active"))[0])
    plan = engine.execute(org, None, files)
    limit = [e for e in plan["errors"] if e["file"] is None]
    assert len(limit) == 1 and "This would add 2 records" in limit[0]["message"]
    assert plan["summary"]["create"] == 2  # still listed, not rehearsed
    response = post_tree(api(rich["users"]["admin"]), "apply", files, plan_hash=plan["plan_hash"])
    body = is_problem(response, 409, "/problems/limit-reached")
    assert body["limit"] == "records" and body["adding"] == 2
    assert Resource.objects.filter(organization=org).count() == used


def test_capabilities_gate_the_feature_and_each_kind(rich, api):
    org, admin = rich["org"], rich["users"]["admin"]
    staff = make_staff()
    files = engine.render_tree(org)
    platform_admin.set_override(staff, org, {"capability": "ai_agents", "effect": "revoke", "reason": "Test"})
    without_ai = engine.render_tree(org)
    assert not any(p.startswith(("ai-agents/", "ai-models/", "ai-providers/")) for p in without_ai)
    assert not any("helpdesk-bot" in p for p in without_ai)  # nor a relationship to an agent
    plan = engine.execute(org, None, files)
    gated = {e["file"] for e in plan["errors"] if "ai_agents capability" in e["message"]}
    assert "ai-agents/helpdesk-bot.yaml" in gated and "ai-models/anthropic-claude-5-5.yaml" in gated
    platform_admin.set_override(staff, org, {"capability": "custom_fields", "effect": "revoke", "reason": "Test"})
    plan = engine.execute(org, None, without_ai)
    assert any(e["path"] == "spec.customFields" and "custom_fields capability" in e["message"] for e in plan["errors"])
    platform_admin.set_override(staff, org, {"capability": "catalog_code", "effect": "revoke", "reason": "Test"})
    client = api(admin)
    is_problem(client.client.get("/api/v1/catalog-code/export.zip"), 403, "/problems/capability-required")
    for route in ("plan", "apply"):
        is_problem(post_tree(client, route, files, plan_hash="x"), 403, "/problems/capability-required")
    with pytest.raises(CommandError, match="catalog-as-code"):
        call_command("catalog_export", "--org", org.slug, "--dir", "unused")


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_only_admins_use_catalog_as_code(rich, api, role):
    client = api(rich["users"][role])
    files = engine.render_tree(rich["org"])
    is_problem(client.client.get("/api/v1/catalog-code/export.zip"), 403, "/problems/forbidden")
    for route in ("plan", "apply"):
        is_problem(post_tree(client, route, files, plan_hash="x"), 403, "/problems/forbidden")
    assert not AuditEvent.objects.filter(action__startswith="catalog_code.").exists()


def test_the_api_export_is_the_tree_and_is_audited(rich, api):
    client = api(rich["users"]["admin"])
    response = client.client.get("/api/v1/catalog-code/export.zip?include_people=true")
    assert response.status_code == 200 and response["Content-Type"] == "application/zip"
    assert response["Content-Disposition"] == 'attachment; filename="softwarden-catalog-orga.zip"'
    assert response.content == bundle.write_zip(engine.render_tree(rich["org"], include_people=True))
    event = AuditEvent.objects.get(organization=rich["org"], action="catalog_code.exported")
    assert event.actor == rich["users"]["admin"] and event.changes["include_people"] is True
    assert event.changes["via"] == "catalog_code"
    is_problem(
        client.client.get("/api/v1/catalog-code/export.zip?include_people=maybe"), 400, "/problems/invalid-request"
    )


def test_impersonation_refuses_the_export_but_allows_plan_and_apply(rich, api):
    org = rich["org"]
    staff = make_staff()
    client = api(staff)
    assert start(client, org, rich["users"]["admin"]).status_code == 201
    is_problem(client.client.get("/api/v1/catalog-code/export.zip"), 403, "/problems/impersonation-restricted")
    files = edited_tree(org)
    plan = post_tree(client, "plan", files)
    assert plan.status_code == 200, plan.json_body
    applied = post_tree(client, "apply", files, plan_hash=plan.json_body["plan_hash"])
    assert applied.status_code == 200, applied.json_body
    event = AuditEvent.objects.filter(organization=org, action="resource.updated", target_id=rich["crm"].id).latest(
        "created_at"
    )
    assert event.impersonated_by == staff and event.actor == rich["users"]["admin"]


def test_plan_and_apply_with_a_token(rich):
    """An Admin's API token plans and applies (CSRF doesn't apply to a bearer token); a read-only token can't."""
    from tests.test_api_lifecycle import TokenClient

    files = edited_tree(rich["org"])
    admin = TokenClient(rich, "admin")
    reader = TokenClient(rich, "admin", read_only=True)
    plan = admin.call("post", "catalog-code/plan", {"file": upload(files)}, multipart=True)
    assert plan.status_code == 200, plan.body
    refused = reader.call(
        "post", "catalog-code/apply", {"file": upload(files), "plan_hash": plan.body["plan_hash"]}, multipart=True
    )
    assert refused.status_code == 403 and refused.body["type"] == "/problems/read-only-token"
    applied = admin.call(
        "post", "catalog-code/apply", {"file": upload(files), "plan_hash": plan.body["plan_hash"]}, multipart=True
    )
    assert applied.status_code == 200 and applied.body["applied"] is True
    export = reader.call("get", "catalog-code/export.zip")
    assert export.status_code == 200 and export.content == bundle.write_zip(engine.render_tree(rich["org"]))


# ---- tenancy --------------------------------------------------------------------------------------------------------


def test_references_resolve_only_within_the_organization(rich, b):
    """Another organization's slugs, names and Softwarden IDs are unknown; applying never touches it."""
    org = rich["org"]
    other_person = b["people"]["dana"]
    files = {
        "applications/x.yaml": app_file("x", "lifecycle: active", f"businessOwner: {other_person.id}"),
        "relationships/r.yaml": (
            "apiVersion: softwarden/v1\nkind: Relationship\nspec:\n  type: depends_on\n"
            f"  source: application:crm\n  target: application:{b['app'].slug}\n"
        ).encode(),
    }
    result = engine.execute(org, None, files)
    messages = {(e["file"], e["path"]): e["message"] for e in result["errors"]}
    assert messages[("applications/x.yaml", "spec.businessOwner")].startswith(f'No person "{other_person.id}"')
    assert messages[("relationships/r.yaml", "spec.target")].startswith("No application with the slug")
    # The same slug in two organizations is two records: b's tree creates its own in a, and b is untouched.
    b_files = engine.render_tree(b["org"])  # b's people stay b's: its owners would be b's Softwarden IDs
    applied = engine.execute(org, None, b_files, apply=True)
    assert applied["applied"], applied["errors"]
    copy = Resource.objects.get(organization=org, slug=b["app"].slug)
    assert copy.id != b["app"].id
    assert engine.render_tree(b["org"]) == b_files


def test_a_tree_with_people_never_merges_into_another_organizations_namesakes(rich, b):
    """b's People files (no emails) name people a has with emails: each is an error, nothing is applied, b unchanged."""
    org = rich["org"]
    b_files = engine.render_tree(b["org"], include_people=True)
    result = engine.execute(org, None, b_files, apply=True)
    assert result["applied"] is False
    namesakes = {e["message"] for e in result["errors"] if e["file"].startswith("people/")}
    assert {
        f"The person named {name} here has an email; add it as metadata.email to match them."
        for name in ("Dana Ortiz", "Priya Raman", "Former person")
    } <= namesakes
    assert not Resource.objects.filter(organization=org, slug=b["app"].slug).exists()
    assert engine.render_tree(b["org"], include_people=True) == b_files


# ---- people matched by name ------------------------------------------------------------------------


def unkeyed_person(name, **spec):
    import uuid

    body: dict = {"apiVersion": "softwarden/v1", "kind": "Person", "metadata": {"id": str(uuid.uuid4()), "name": name}}
    if spec:
        body["spec"] = spec
    return fmt.dump(body)


def owned_app(slug, business, technical):
    return app_file(slug, "lifecycle: active", f"businessOwner: {business}", f"technicalOwner: {technical}")


def test_two_unkeyed_files_with_one_name_are_both_errors(a):
    """Two different people called Alex Kim, neither with an email: which is which can't be told, so neither is
    created or matched, and the record naming them can't collapse its two owners into one person."""
    files = {"people/alex-1.yaml": unkeyed_person("Alex Kim"), "people/alex-2.yaml": unkeyed_person("Alex Kim")}
    ids = [doc(files, p)["metadata"]["id"] for p in sorted(files)]
    files["applications/app.yaml"] = owned_app("app", *ids)
    result = engine.execute(a["org"], None, files, apply=True)
    assert result["applied"] is False
    people_errors = [e for e in result["errors"] if e["file"].startswith("people/")]
    assert len(people_errors) == 2 and all("names Alex Kim too" in e["message"] for e in people_errors)
    assert not Person.objects.filter(organization=a["org"], name="Alex Kim").exists()


def test_name_matching_ignores_people_the_same_run_created(a):
    """A person an earlier file creates (by email) is never the name match of a later unkeyed file: two people."""
    files = {
        "people/alex.yaml": fmt.dump(
            {
                "apiVersion": "softwarden/v1",
                "kind": "Person",
                "metadata": {"email": "alex@example.test", "name": "Alex Kim"},
            }
        ),
        "people/other-alex.yaml": unkeyed_person("Alex Kim"),
    }
    other = doc(files, "people/other-alex.yaml")["metadata"]["id"]
    files["applications/app.yaml"] = owned_app("app", "alex@example.test", other)
    result = engine.execute(a["org"], None, files, apply=True)
    assert result["applied"], result["errors"]
    app = Resource.objects.get(organization=a["org"], slug="app")
    assert Person.objects.filter(organization=a["org"], name="Alex Kim").count() == 2
    assert app.business_owner_id != app.technical_owner_id


def test_two_files_that_resolve_to_one_person_are_refused(a):
    """Different keys, one row: Dana by her Softwarden ID, and an unknown ID that the name would match to her."""
    dana = a["people"]["dana"]
    files = {
        "people/a.yaml": fmt.dump(
            {"apiVersion": "softwarden/v1", "kind": "Person", "metadata": {"id": str(dana.id), "name": "Dana Ortiz"}}
        ),
        "people/b.yaml": unkeyed_person("Dana Ortiz"),
    }
    result = engine.execute(a["org"], None, files)
    assert [(e["file"], e["message"]) for e in result["errors"]] == [
        ("people/b.yaml", "This is the same Person as people/a.yaml.")
    ]


def test_a_name_only_match_links_references_but_never_edits_the_person(a):
    org, admin = a["org"], a["users"]["admin"]
    sam = services.create_person(org, admin, {"name": "Sam Lee", "title": "CFO"})
    files = {"people/sam.yaml": unkeyed_person("Sam Lee", title="Engineer", active=False)}
    files["applications/app.yaml"] = owned_app("app", doc(files, "people/sam.yaml")["metadata"]["id"], str(sam.id))
    result = engine.execute(org, None, files, apply=True)
    assert result["applied"], result["errors"]
    sam.refresh_from_db()
    assert (sam.title, sam.active) == ("CFO", True)  # unrelated local Sam Lee keeps his fields
    assert Resource.objects.get(organization=org, slug="app").business_owner_id == sam.id
    warnings = {(w["path"], w["message"]) for w in result["warnings"]}
    assert ("spec.title", "Not changed (matched by name only): 'CFO' here, 'Engineer' in the file.") in warnings
    assert ("spec.active", "Not changed (matched by name only): True here, False in the file.") in warnings
    assert not [c for c in result["changes"] if c["kind"] == "Person"]
    # A match by a known ID still updates the person.
    by_id = {
        "people/sam.yaml": fmt.dump(
            {
                "apiVersion": "softwarden/v1",
                "kind": "Person",
                "metadata": {"id": str(sam.id), "name": "Sam Lee"},
                "spec": {"title": "Engineer"},
            }
        )
    }
    assert engine.execute(org, None, by_id, apply=True)["applied"]
    sam.refresh_from_db()
    assert sam.title == "Engineer"


# ---- the CLI --------------------------------------------------------------------------------------------------------


def test_the_cli_exports_and_applies_a_directory(rich, tmp_path):
    org = rich["org"]
    (tmp_path / "applications").mkdir()
    (tmp_path / "applications" / "deleted-since.yaml").write_text("old")
    (tmp_path / "README.md").write_text("keep me")
    out = io.StringIO()
    call_command("catalog_export", "--org", org.slug, "--dir", str(tmp_path), stdout=out)
    assert "Wrote" in out.getvalue()
    files = engine.render_tree(org)
    assert (tmp_path / "applications" / "crm.yaml").read_bytes() == files["applications/crm.yaml"]
    assert not (tmp_path / "applications" / "deleted-since.yaml").exists()  # an earlier export's leftover
    assert (tmp_path / "README.md").read_text() == "keep me"
    assert AuditEvent.objects.get(organization=org, action="catalog_code.exported").changes["interface"] == "cli"

    crm = tmp_path / "applications" / "crm.yaml"
    crm.write_bytes(crm.read_bytes().replace(b"lifecycle: active", b"lifecycle: deprecated"))
    out = io.StringIO()
    call_command("catalog_apply", "--org", org.slug, "--dir", str(tmp_path), "--dry-run", stdout=out)
    text = out.getvalue()
    assert "update  Application application:crm  (applications/crm.yaml)" in text
    assert 'spec.lifecycle: "active" -> "deprecated"' in text and "Plan hash:" in text
    assert Resource.objects.get(id=rich["crm"].id).lifecycle == "active"
    out = io.StringIO()
    call_command("catalog_apply", "--org", org.slug, "--dir", str(tmp_path), "--dry-run", "--json", stdout=out)
    plan = json.loads(out.getvalue())
    with pytest.raises(CommandError, match="changed since this plan"):
        call_command("catalog_apply", "--org", org.slug, "--dir", str(tmp_path), "--expect-hash", "0" * 64)
    out = io.StringIO()
    call_command(
        "catalog_apply", "--org", org.slug, "--dir", str(tmp_path), "--expect-hash", plan["plan_hash"], stdout=out
    )
    assert "Applied." in out.getvalue()
    assert Resource.objects.get(id=rich["crm"].id).lifecycle == "deprecated"
    applied = AuditEvent.objects.get(organization=org, action="catalog_code.applied")
    assert applied.actor is None and applied.changes["interface"] == "cli" and applied.changes["via"] == "catalog_code"

    # Applying needs the reviewed plan's hash, or saying there is none.
    with pytest.raises(CommandError, match="--expect-hash"):
        call_command("catalog_apply", "--org", org.slug, "--dir", str(tmp_path))
    crm.write_bytes(crm.read_bytes().replace(b"lifecycle: deprecated", b"lifecycle: sometimes"))
    err = io.StringIO()
    with pytest.raises(CommandError, match="nothing was applied"):
        call_command(
            "catalog_apply",
            "--org",
            org.slug,
            "--dir",
            str(tmp_path),
            "--no-plan-check",
            stdout=io.StringIO(),
            stderr=err,
        )
    assert "applications/crm.yaml:" in err.getvalue()
    assert "spec.lifecycle: Choose a lifecycle status." in err.getvalue()
    with pytest.raises(CommandError, match="No organization"):
        call_command("catalog_apply", "--org", "nope", "--dir", str(tmp_path), "--dry-run")


def symlink_or_skip(target, link, directory=False):
    import os

    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("this machine can't create symbolic links")


def test_the_cli_refuses_symbolic_links(rich, tmp_path):
    target = tmp_path / "outside.yaml"
    target.write_text("a: 1")
    (tmp_path / "tree").mkdir()
    symlink_or_skip(target, tmp_path / "tree" / "link.yaml")
    with pytest.raises(CommandError, match="symbolic link"):
        call_command("catalog_apply", "--org", rich["org"].slug, "--dir", str(tmp_path / "tree"), "--dry-run")


def test_the_cli_export_never_writes_through_a_link(rich, tmp_path):
    """A file of the tree left as a link (to anywhere) and a linked folder are refused before anything is written."""
    outside = tmp_path / "outside.yaml"
    outside.write_text("keep me")
    tree = tmp_path / "tree"
    (tree / "applications").mkdir(parents=True)
    symlink_or_skip(outside, tree / "applications" / "crm.yaml")
    with pytest.raises(CommandError, match="symbolic link"):
        call_command("catalog_export", "--org", rich["org"].slug, "--dir", str(tree))
    assert outside.read_text() == "keep me"
    assert not (tree / "catalog.yaml").exists()  # refused before anything was written
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    symlink_or_skip(elsewhere, second / "components", directory=True)
    with pytest.raises(CommandError, match="symbolic link"):
        call_command("catalog_export", "--org", rich["org"].slug, "--dir", str(second))
    assert list(elsewhere.iterdir()) == []


# ---- review follow-ups ----------------------------------------------------------------------------------------------


def test_people_without_an_email_converge_over_two_applies(a):
    """A Person file keyed by another organization's ID is matched by exact name, so a second apply creates no one."""
    org, admin = a["org"], a["users"]["admin"]
    services.update_resource(org, admin, a["app"], {"business_owner": str(a["people"]["dana"].id)})
    files = engine.render_tree(org, include_people=True)
    fresh = fresh_org()
    first = engine.execute(fresh, None, files, apply=True)
    assert first["applied"], first["errors"]
    assert {w["message"] for w in first["warnings"]} == {
        "No person has this Softwarden ID or name here; applying creates them."
    }
    count = Person.objects.filter(organization=fresh).count()
    assert count == 3
    second = engine.execute(fresh, None, files, apply=True)
    assert second["applied"], second["errors"]
    assert Person.objects.filter(organization=fresh).count() == count
    assert second["summary"]["create"] == 0 and second["summary"]["update"] == 0
    assert any(
        w["message"].startswith("No person has this Softwarden ID here; matched by name to Dana Ortiz")
        for w in second["warnings"]
    )
    owner = Resource.objects.get(organization=fresh, slug=a["app"].slug).business_owner
    assert owner is not None and owner.name == "Dana Ortiz"
    # Two people of that name: the file has to say which.
    services.create_person(fresh, None, {"name": "Dana Ortiz"})
    third = engine.execute(fresh, None, files)
    assert any("2 people are named Dana Ortiz" in e["message"] for e in third["errors"])


def person_file(**metadata):
    return fmt.dump({"apiVersion": "softwarden/v1", "kind": "Person", "metadata": {"name": "Dana Ortiz", **metadata}})


def test_a_person_file_needs_a_real_id_and_cannot_rewrite_an_email(rich):
    org, dana = rich["org"], rich["people"]["dana"]
    result = engine.execute(org, None, {"people/x.yaml": person_file(id="not-a-uuid")})
    assert result["errors"][0]["path"] == "metadata.id" and "UUID" in result["errors"][0]["message"]
    for metadata in ({"id": str(dana.id)}, {"id": str(dana.id), "email": "other@example.test"}):
        result = engine.execute(org, None, {"people/x.yaml": person_file(**metadata)})
        assert result["errors"][0]["path"] == "metadata.email", metadata
    ok = engine.execute(org, None, {"people/x.yaml": person_file(id=str(dana.id), email="DANA@example.test")})
    assert ok["errors"] == []


def test_a_tag_file_matches_the_tag_as_records_do(rich):
    org = rich["org"]
    admin_services.create_tag(org, rich["users"]["admin"], {"name": "Foo Bar"})
    events = AuditEvent.objects.filter(action="tag.created").count()
    assert admin_services.create_tag(org, None, {"name": "  foo   BAR "}).name == "Foo Bar"
    assert AuditEvent.objects.filter(action="tag.created").count() == events  # only a real create is audited
    tree = {
        "tags/foo-bar.yaml": fmt.dump(
            {"apiVersion": "softwarden/v1", "kind": "Tag", "metadata": {"name": "Foo  Bar"}, "spec": {"color": "teal"}}
        )
    }
    result = engine.execute(org, None, tree, apply=True)
    assert result["applied"], result["errors"]
    assert [(c["action"], c["key"]) for c in result["changes"]] == [("update", "Foo  Bar")]
    assert Tag.objects.filter(organization=org, name__iexact="foo bar").get().color == "teal"
    new = {"tags/new.yaml": fmt.dump({"apiVersion": "softwarden/v1", "kind": "Tag", "metadata": {"name": "New  one"}})}
    assert engine.execute(org, None, new, apply=True)["applied"]
    made = Tag.objects.get(organization=org, name="New one")
    assert AuditEvent.objects.get(action="tag.created", target_id=made.id).changes["via"] == "catalog_code"


def test_values_the_database_refuses_are_file_errors(rich, monkeypatch):
    """Names past their column are field errors in the services; anything else the database refuses (DataError) is
    still that file's error, never a 500."""
    from django.db import DataError

    from catalog_code import kinds

    org = rich["org"]
    long_name = "x" * 150  # categories hold 100 characters
    category = {"apiVersion": "softwarden/v1", "kind": "Category", "metadata": {"name": long_name}}
    person = {
        "apiVersion": "softwarden/v1",
        "kind": "Person",
        "metadata": {"email": "p@example.test", "name": "y" * 300},
    }
    result = engine.execute(org, None, {"categories/c.yaml": fmt.dump(category), "people/p.yaml": fmt.dump(person)})
    found = {(e["file"], e["path"]): e["message"] for e in result["errors"]}
    assert found[("categories/c.yaml", "metadata.name")] == "Use 100 characters or fewer."
    assert found[("people/p.yaml", "metadata.name")] == "Use 200 characters or fewer."

    def refuse(self, run, values):
        raise DataError("value too long")

    monkeypatch.setattr(kinds.DepartmentKind, "create", refuse)
    department = {"apiVersion": "softwarden/v1", "kind": "Department", "metadata": {"name": "Legal"}}
    result = engine.execute(org, None, {"departments/legal.yaml": fmt.dump(department)})
    assert result["errors"][0]["file"] == "departments/legal.yaml"
    assert result["errors"][0]["message"] == engine.DATA_ERROR


def test_an_api_request_creates_at_most_so_many_rows(rich, api, monkeypatch):
    monkeypatch.setattr(engine, "API_MAX_CREATES", 1)
    files = {f"applications/{s}.yaml": app_file(s, "lifecycle: active") for s in ("one", "two")}
    response = post_tree(api(rich["users"]["admin"]), "plan", files)
    body = is_problem(response, 400, "/problems/invalid-request")
    assert "catalog_apply" in body["errors"]["file"][0]
    assert not Resource.objects.filter(organization=rich["org"], slug__in=["one", "two"]).exists()
    assert engine.execute(rich["org"], None, files)["summary"]["create"] == 2  # the CLI has no cap


def test_the_whole_tree_has_a_value_cap(rich, monkeypatch):
    monkeypatch.setattr(engine, "MAX_TREE_NODES", 12)
    files = {f"applications/{s}.yaml": app_file(s, "lifecycle: active") for s in ("one", "two")}
    result = engine.execute(rich["org"], None, files)
    assert any("values in all" in e["message"] for e in result["errors"])


def test_pruning_a_custom_field_says_how_many_values_go(rich):
    # The records keep their values in the files: the field goes at the end, taking them with it.
    files = {p: v for p, v in engine.render_tree(rich["org"]).items() if p != "custom-fields/tier.yaml"}
    plan = engine.execute(rich["org"], None, files, prune=True)
    delete = next(c for c in plan["changes"] if c["kind"] == "CustomField")
    assert (delete["action"], delete["key"]) == ("delete", "tier")
    assert delete["fields"] == [{"path": "values", "before": 2, "after": 0}]


def test_the_plan_hash_names_the_organization(rich):
    """The same files planned for two organizations never share a hash, even were their catalogs alike."""
    files = engine.render_tree(rich["org"])
    from catalog_code.state import State

    a_state, other = State(rich["org"]), fresh_org()
    b_state = State(other)
    assert engine.plan_hash(a_state, files, False) != engine.plan_hash(b_state, files, False)
    empty_a, empty_b = fresh_org("One"), fresh_org("Two")
    assert engine.plan_hash(State(empty_a), {}, False) != engine.plan_hash(State(empty_b), {}, False)


# ---- Lock order: apply against bulk edit and reassign ---------------------------------------------------------------


def race(first, second):
    """Run `first` (which sets an event once it has written its first row) and `second`, each in its own thread and
    connection, as tests/test_offboarding.race does. Returns (the order they finished in, the errors raised)."""
    done, errors = [], []

    def wrap(name, fn):
        def inner():
            try:
                fn()
                done.append(name)
            except Exception as e:  # a deadlock surfaces here as OperationalError
                errors.append((name, type(e).__name__, str(e).splitlines()[0]))
            finally:
                connection.close()

        return inner

    threads = [threading.Thread(target=wrap("apply", first)), threading.Thread(target=wrap("other", second))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    return done, errors


def hold_after_first(monkeypatch, kind_class, holding):
    """Make the apply stop for a moment after the first row of `kind_class` it updates, with that row written (and
    locked) and the next one still to come, so the other thread starts meanwhile. Returns the updated rows' ids."""
    original = kind_class.update
    calls = []

    def update(self, run, obj, current, values, changed):
        written = original(self, run, obj, current, values, changed)
        calls.append(obj.pk)
        if len(calls) == 1:
            holding.set()
            time.sleep(0.8)
        return written

    monkeypatch.setattr(kind_class, "update", update)
    return calls


def test_apply_locks_records_in_id_order_so_a_bulk_edit_waits(transactional_db, monkeypatch):
    """The tree's files are applied in path order; bulk edit locks its records in ID order. The file that sorts first
    names the record with the higher ID, so without the apply's up-front locks it would hold that record and then wait
    for the other, which the bulk edit already holds while it waits for the first: a deadlock. The apply now locks
    both in ID order before writing either, so the bulk edit waits for it."""
    f = make_org("ccrace")
    org, admin, editor = f["org"], f["users"]["admin"], f["users"]["editor"]
    made = [
        services.create_resource(org, admin, {"kind": "application", "name": name, "lifecycle": "active"})
        for name in ("One", "Two")
    ]
    low, high = sorted(made, key=lambda r: r.id)
    Resource.objects.filter(id=high.id).update(slug="a-first")  # its file sorts first
    Resource.objects.filter(id=low.id).update(slug="z-last")
    files = engine.render_tree(org)
    for path in ("applications/a-first.yaml", "applications/z-last.yaml"):
        value = doc(files, path)
        value["metadata"]["description"] = "Written by the tree"
        files = put(files, path, value)
    holding = threading.Event()
    calls = hold_after_first(monkeypatch, ResourceKind, holding)

    def apply():
        result = engine.execute(org, admin, files, apply=True)
        assert result["applied"], result["errors"]

    def edit():
        holding.wait(5)
        ids = [str(low.id), str(high.id)]
        bulk.apply(org, editor, {"kind": "application", "ids": ids, "changes": {"criticality": "high"}})

    done, errors = race(apply, edit)
    assert errors == [] and done == ["apply", "other"]
    assert calls == [high.id, low.id]  # still written in file order: only the locks come first, in ID order
    for record in Resource.objects.filter(id__in=[low.id, high.id]):
        assert record.description == "Written by the tree" and record.criticality == "high"


def test_apply_locks_people_in_id_order_so_a_reassign_waits(transactional_db, monkeypatch):
    """Reassign locks both People in ID order before anything else. A tree whose first Person file names the one with
    the higher ID used to update them in file order, holding that one while reassign held the other: a deadlock. The
    apply now takes people first and in ID order, as reassign does."""
    f = make_org("ccrace2")
    org, admin = f["org"], f["users"]["admin"]
    made = [
        Person.objects.create(organization=org, name=name, email=f"{name.lower()}@ccrace2.test")
        for name in ("Ines", "Omar")
    ]
    low, high = sorted(made, key=lambda p: p.id)

    def person_file(person):
        meta = {"email": person.email, "name": person.name}
        return fmt.dump({"apiVersion": "softwarden/v1", "kind": "Person", "metadata": meta, "spec": {"title": "T"}})

    files = {"people/a-first.yaml": person_file(high), "people/z-last.yaml": person_file(low)}
    holding = threading.Event()
    calls = hold_after_first(monkeypatch, PersonKind, holding)

    def apply():
        result = engine.execute(org, admin, files, apply=True)
        assert result["applied"], result["errors"]

    def offboard():
        holding.wait(5)
        reassign_module.reassign(org, admin, "admin", low, {"to": str(high.id), "roles": ["business_owner"]})

    done, errors = race(apply, offboard)
    assert errors == [] and done == ["apply", "other"]
    assert calls == [high.id, low.id]
    assert set(Person.objects.filter(id__in=[low.id, high.id]).values_list("title", flat=True)) == {"T"}
