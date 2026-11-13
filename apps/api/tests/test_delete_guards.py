"""Delete guards: an instance or a record that a relationship names can't be deleted."""

from catalog import services
from catalog.models import ApplicationInstance, Relationship
from core.models import AuditEvent


def record(ctx, name, kind="application"):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": "active"}
    )


def instance(ctx, application, name="Production"):
    return services.create_instance(ctx["org"], ctx["users"]["editor"], application, {"name": name})


def link(ctx, rel_type, source, target, **extra):
    return services.create_relationship(
        ctx["org"],
        ctx["users"]["editor"],
        {"type": rel_type, "source": str(source.id), "target": str(target.id), **extra},
    )


# ---- Delete guards ------------------------------------------------------------------------------------------


def test_instance_delete_is_refused_while_relationships_use_it(a, api):
    client = api(a["users"]["editor"])
    prod, sandbox = instance(a, a["app"]), instance(a, a["app"], "Sandbox")
    job = record(a, "Job", "component")
    link(a, "depends_on", a["app"], job, source_instance=str(prod.id))
    link(a, "depends_on", a["app"], a["app"], source_instance=str(prod.id), target_instance=str(sandbox.id))
    url = f"applications/{a['app'].id}/instances/{prod.id}"
    refused = client.delete(url)
    assert refused.status_code == 409 and refused["Content-Type"] == "application/problem+json"
    assert refused.json_body["detail"] == (
        "Production can't be deleted because it has 2 relationships. Remove or change them first."
    )
    assert refused.json_body["blocking_references"] == {"relationship": 2}
    assert ApplicationInstance.objects.filter(id=prod.id).exists()
    assert not AuditEvent.objects.filter(action="instance.deleted").exists()
    # The target end blocks too; an unused instance still deletes.
    refused_target = client.delete(f"applications/{a['app'].id}/instances/{sandbox.id}")
    assert refused_target.status_code == 409 and "1 relationship." in refused_target.json_body["detail"]
    Relationship.objects.all().delete()
    assert client.delete(url).status_code == 204


def test_resource_delete_is_refused_while_relationships_use_it(a, api):
    admin = api(a["users"]["admin"])
    job = record(a, "Job", "component")
    link(a, "depends_on", a["app"], job)
    link(a, "depends_on", job, a["app"])
    refused = admin.delete(f"components/{job.id}")
    assert refused.status_code == 409
    # Two links name the record, once each side; the count is of links, and the instance and review nouns still work.
    assert refused.json_body["blocking_references"] == {"relationship": 2}
    assert refused.json_body["detail"] == "Job can't be deleted because it has 2 relationships. Archive it instead."
    prod = instance(a, a["app"])
    link(
        a,
        "depends_on",
        a["app"],
        a["app"],
        source_instance=str(prod.id),
        target_instance=str(instance(a, a["app"], "Sandbox").id),
    )
    mixed = admin.delete(f"applications/{a['app'].id}")
    assert mixed.json_body["blocking_references"] == {"instance": 2, "relationship": 3}  # a self-link counts once
    assert mixed.json_body["detail"].startswith(
        "orga App can't be deleted because it has 2 instances and 3 relationships"
    )
    Relationship.objects.all().delete()
    assert admin.delete(f"components/{job.id}").status_code == 204


def test_a_single_relationship_is_singular(a, api):
    job = record(a, "Job", "component")
    link(a, "depends_on", a["app"], job)
    refused = api(a["users"]["admin"]).delete(f"components/{job.id}")
    assert refused.json_body["detail"] == "Job can't be deleted because it has 1 relationship. Archive it instead."


def test_references_to_names_any_model_that_points_at_the_record(a):
    # The hook for E9: a new model that points at a resource or an instance is counted by its verbose name.
    job = record(a, "Job", "component")
    link(a, "depends_on", a["app"], job)
    assert services.references_to(job) == {"relationship": 1}
    assert services.references_to(a["app"]) == {"relationship": 1}
