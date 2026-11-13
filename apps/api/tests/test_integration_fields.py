"""Integration fields (catalog 0035): direction, auth_method and
monitoring_url on the integration row, and the Resource's data_classification now written for integrations too."""

import json

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from catalog import services
from catalog.models import Integration, IntegrationAuthMethod, IntegrationDirection, Resource
from core.models import AuditEvent
from core.problems import FieldErrors

NEW_FIELDS = {
    "direction": "two_way",
    "auth_method": "managed_identity",
    "monitoring_url": "https://monitoring.example.test/flows/payroll?view=" + "x" * 1500,
    "data_classification": "confidential",
}


def record(ctx, name, kind="application"):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": "active"}
    )


def flow(ctx, name="Nightly feed", **extra):
    source, target = record(ctx, f"{name} source"), record(ctx, f"{name} target", "component")
    return services.create_integration(
        ctx["org"],
        ctx["users"]["editor"],
        {"name": name, "lifecycle": "active", "source": str(source.id), "target": str(target.id), **extra},
    )


def body(ctx, **extra):
    source, target = record(ctx, "Feed source"), record(ctx, "Feed target", "component")
    return {"name": "Payroll feed", "lifecycle": "active", "source": str(source.id), "target": str(target.id), **extra}


# ---- model ----------------------------------------------------------------------------------------------------------


def test_new_fields_default_to_not_recorded(a):
    feed = flow(a)
    ext = Integration.objects.get(pk=feed.pk)
    assert (ext.direction, ext.auth_method, ext.monitoring_url) == ("", "", "")
    assert Resource.objects.get(pk=feed.pk).data_classification == ""
    assert Integration._meta.get_field("monitoring_url").max_length == 2000
    assert IntegrationDirection.values == ["one_way", "two_way"]
    # oauth, none and other are the same values as Application.authentication_method's.
    assert {"oauth", "none", "other"} <= set(IntegrationAuthMethod.values)


# ---- service validation ------------------------------------------------------------------------------------------


def test_service_stores_and_validates_the_new_fields(a):
    feed = flow(a, **NEW_FIELDS)
    ext, resource = Integration.objects.get(pk=feed.pk), Resource.objects.get(pk=feed.pk)
    assert (ext.direction, ext.auth_method, ext.monitoring_url) == (
        "two_way",
        "managed_identity",
        NEW_FIELDS["monitoring_url"],
    )
    assert resource.data_classification == "confidential"

    bad = {
        "direction": "sideways",
        "auth_method": "saml",  # a person's sign-in method, not an integration's
        "data_classification": "secret",
    }
    for field, value in bad.items():
        with pytest.raises(FieldErrors) as raised:
            services.update_integration(a["org"], a["users"]["editor"], resource, {field: value})
        assert field in raised.value.errors
    for url in ("javascript:alert(1)", "ftp://files.example.test/x", "not a url"):
        with pytest.raises(FieldErrors) as raised:
            services.update_integration(a["org"], a["users"]["editor"], resource, {"monitoring_url": url})
        assert raised.value.errors["monitoring_url"] == ["Enter a full web address starting with http:// or https://."]
    with pytest.raises(FieldErrors) as raised:
        services.update_integration(
            a["org"], a["users"]["editor"], resource, {"monitoring_url": "https://x.example.test/" + "a" * 2000}
        )
    assert raised.value.errors["monitoring_url"] == ["Use 2,000 characters or fewer."]
    # Nothing was stored by the refused writes; blank clears each one back to not recorded.
    ext.refresh_from_db()
    assert ext.direction == "two_way" and ext.auth_method == "managed_identity"
    cleared = {k: "" for k in NEW_FIELDS}
    services.update_integration(a["org"], a["users"]["editor"], Resource.objects.get(pk=feed.pk), cleared)
    ext.refresh_from_db()
    assert (ext.direction, ext.auth_method, ext.monitoring_url) == ("", "", "")
    assert Resource.objects.get(pk=feed.pk).data_classification == ""


def test_integrations_still_take_no_department_cost_center_or_criticality(a):
    feed = flow(a, cost_center="CC-1", criticality="high", department=None)
    stored = Resource.objects.get(pk=feed.pk)
    assert stored.cost_center == "" and stored.criticality == ""


def test_record_fields_are_not_stored_on_applications(a):
    """direction and auth_method belong to integrations only: an application ignores them (no field error, even
    for values no integration would accept), stores neither and never audits them."""
    app = services.create_resource(
        a["org"],
        a["users"]["editor"],
        {"name": "App", "kind": "application", "lifecycle": "active", "direction": "bogus", "auth_method": "x"},
    )
    stored = Resource.objects.select_related("application").get(pk=app.pk)
    for name in ("direction", "auth_method"):
        assert not hasattr(stored, name) and not hasattr(stored.application, name)
    event = AuditEvent.objects.get(action="resource.created", target_id=app.id)
    assert event.postchange is not None
    assert "direction" not in event.postchange and "auth_method" not in event.postchange
    assert "direction" not in event.changes and "auth_method" not in event.changes


def test_the_audit_labels_tell_the_two_authentication_fields_apart():
    from catalog.audit_log import FIELD_LABELS

    assert FIELD_LABELS["auth_method"] == "Integration authentication"
    assert FIELD_LABELS["authentication_method"] == "Authentication method"


# ---- API ----------------------------------------------------------------------------------------------------------


def test_api_round_trip(a, api):
    client = api(a["users"]["editor"])
    created = client.post("integrations", body(a, **NEW_FIELDS))
    assert created.status_code == 201, created.json_body
    made = created.json_body
    assert {k: made[k] for k in NEW_FIELDS} == NEW_FIELDS
    shown = api(a["users"]["viewer"]).get(f"integrations/{made['id']}").json_body
    assert {k: shown[k] for k in NEW_FIELDS} == NEW_FIELDS
    listed = api(a["users"]["viewer"]).get("integrations").json_body["results"][0]
    assert {k: listed[k] for k in NEW_FIELDS} == NEW_FIELDS

    edited = client.patch(f"integrations/{made['id']}", {"direction": "one_way", "monitoring_url": ""})
    assert edited.status_code == 200, edited.json_body
    assert edited.json_body["direction"] == "one_way" and edited.json_body["monitoring_url"] == ""
    assert edited.json_body["auth_method"] == "managed_identity"  # untouched by the PATCH

    blank = client.post("integrations", {**body(a), "name": "Plain feed"}).json_body
    assert {k: blank[k] for k in NEW_FIELDS} == dict.fromkeys(NEW_FIELDS, "")


def test_api_validation_errors(a, api):
    client = api(a["users"]["editor"])
    cases = [
        ({"direction": "sideways"}, "direction"),
        ({"auth_method": "ldap"}, "auth_method"),
        ({"data_classification": "top_secret"}, "data_classification"),
        ({"monitoring_url": "javascript:alert(1)"}, "monitoring_url"),
        ({"monitoring_url": "https://x.example.test/" + "a" * 2000}, "monitoring_url"),
    ]
    for extra, field in cases:
        response = client.post("integrations", body(a, **extra))
        assert response.status_code == 400 and field in response.json_body["errors"], (extra, response.json_body)
    assert not Resource.objects.filter(kind="integration").exists()


def test_filters(a, api):
    flow(a, "Two-way OAuth", direction="two_way", auth_method="oauth", data_classification="restricted")
    flow(a, "One-way key", direction="one_way", auth_method="api_key", data_classification="internal")
    flow(a, "Unrecorded")
    client = api(a["users"]["viewer"])

    def names(query):
        response = client.get(f"integrations?{query}")
        assert response.status_code == 200, response.json_body
        return sorted(r["name"] for r in response.json_body["results"])

    assert names("direction=two_way") == ["Two-way OAuth"]
    assert names("direction=one_way,two_way") == ["One-way key", "Two-way OAuth"]
    assert names("auth_method=api_key") == ["One-way key"]
    assert names("data_classification=restricted,internal") == ["One-way key", "Two-way OAuth"]
    assert names("direction=two_way&auth_method=api_key") == []
    for bad in ("direction=sideways", "auth_method=saml", "data_classification=secret"):
        response = client.get(f"integrations?{bad}")
        assert response.status_code == 400 and bad.split("=")[0] in response.json_body["errors"]


def test_filters_stay_in_the_organization(a, b, api):
    flow(b, "Theirs", direction="two_way")
    assert api(a["users"]["viewer"]).get("integrations?direction=two_way").json_body["results"] == []


def test_audit_diff_of_each_new_field(a, api):
    feed = flow(a)
    client = api(a["users"]["editor"])
    for field, value in NEW_FIELDS.items():
        assert client.patch(f"integrations/{feed.id}", {field: value}).status_code == 200
        event = AuditEvent.objects.filter(action="resource.updated", target_id=feed.id).order_by("-created_at")[0]
        assert event.changes == {field: ["", value]}, field
        assert event.prechange is not None and event.postchange is not None
        assert event.prechange[field] == "" and event.postchange[field] == value
    history = api(a["users"]["viewer"]).get(f"integrations/{feed.id}/audit").json_body["results"]
    summaries = [e["summary"] for e in history]
    assert any(s.startswith("Direction: not set → Both ways") for s in summaries)
    assert any(s.startswith("Integration authentication: not set → Managed identity") for s in summaries)
    assert any(s.startswith("Data classification: not set → confidential") for s in summaries)
    assert any(s.startswith("Monitoring URL: ") for s in summaries)


def test_role_checks_unchanged(a, api):
    feed = flow(a)
    assert api(a["users"]["viewer"]).patch(f"integrations/{feed.id}", {"direction": "one_way"}).status_code == 403
    assert api(a["users"]["viewer"]).post("integrations", body(a, direction="one_way")).status_code == 403
    assert api(a["users"]["editor"]).patch(f"integrations/{feed.id}", {"direction": "one_way"}).status_code == 200
    assert Integration.objects.get(pk=feed.pk).direction == "one_way"


def test_openapi_documents_the_fields(a, api):
    schema = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    components = schema["components"]["schemas"]
    read, write = components["Integration"]["properties"], components["IntegrationInputRequest"]["properties"]
    for field in NEW_FIELDS:
        assert field in read and field in write, field
    params = {p["name"] for p in schema["paths"]["/api/v1/integrations"]["get"]["parameters"]}
    assert {"direction", "auth_method", "data_classification"} <= params


# ---- migration 0035 ------------------------------------------------------------------------------------------------

BEFORE_FIELDS = [("catalog", "0034_relationship_type_fk")]


@pytest.mark.django_db(transaction=True)
def test_existing_integrations_read_as_not_recorded_after_the_migration():
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE_FIELDS)
    old = executor.loader.project_state(BEFORE_FIELDS).apps
    Org, Res, Int = (
        old.get_model("core", "Organization"),
        *(old.get_model("catalog", m) for m in ("Resource", "Integration")),
    )
    org = Org.objects.create(name="Fields (synthetic)")
    try:
        src, dst, own = (
            Res.objects.create(organization=org, name=n, slug=n.lower(), lifecycle="active", kind=k)
            for n, k in (("Src", "application"), ("Dst", "component"), ("Feed", "integration"))
        )
        Int.objects.create(resource=own, organization=org, source_resource=src, target_resource=dst, protocol="SFTP")
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        new = executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps
        row = new.get_model("catalog", "Integration").objects.get(resource_id=own.id)
        assert (row.protocol, row.direction, row.auth_method, row.monitoring_url) == ("SFTP", "", "", "")
    finally:
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        with connection.cursor() as cursor:
            for table in (
                "catalog_integration",
                "catalog_resource",
                "catalog_relationshiptype",
                "catalog_softwarecategory",
                "notify_notificationrule",  # seeded by notify 0005
            ):
                cursor.execute(f"DELETE FROM {table} WHERE organization_id = %s", [org.id])  # noqa: S608
            cursor.execute("DELETE FROM catalog_organization WHERE id = %s", [org.id])
