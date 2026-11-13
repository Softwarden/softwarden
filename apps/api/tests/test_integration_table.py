"""The integrations table: composite foreign keys and the not-self check."""

import pytest
from django.db import IntegrityError, connection, transaction

from catalog import services
from catalog.models import Integration, Resource


def record(ctx, name, kind="application"):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": "active"}
    )


def instance(ctx, application, name="Production"):
    return services.create_instance(ctx["org"], ctx["users"]["editor"], application, {"name": name})


def flow(ctx, name="Nightly feed"):
    source, target = record(ctx, f"{name} source"), record(ctx, f"{name} target", "component")
    return services.create_integration(
        ctx["org"],
        ctx["users"]["editor"],
        {"name": name, "lifecycle": "active", "source": str(source.id), "target": str(target.id)},
    )


# ---- The table ----------------------------------------------------------------------------------------------


def test_database_rejects_an_end_in_another_organization(a, b):
    mine = flow(a)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Integration.objects.filter(pk=mine.pk).update(target_resource=b["app"])
            connection.check_constraints()  # the composite FK is deferred; force the check


def test_database_rejects_an_instance_of_another_application(a):
    other = record(a, "Other")
    theirs = instance(a, other)
    mine = flow(a)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Integration.objects.filter(pk=mine.pk).update(source_instance=theirs)
            connection.check_constraints()


def test_database_rejects_the_same_record_at_both_ends_without_two_different_instances(a):
    app = a["app"]
    prod, sandbox = instance(a, app), instance(a, app, "Sandbox")
    mine = flow(a)
    for ends in (
        {},
        {"source_instance": prod},
        {"source_instance": prod, "target_instance": prod},
    ):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Integration.objects.filter(pk=mine.pk).update(source_resource=app, target_resource=app, **ends)
    Integration.objects.filter(pk=mine.pk).update(
        source_resource=app, target_resource=app, source_instance=prod, target_instance=sandbox
    )


def test_database_rejects_an_extension_in_another_organization_than_its_record(a, b):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            resource = Resource.objects.create(
                organization=a["org"], kind="integration", name="Odd", slug="odd", lifecycle="active"
            )
            Integration.objects.create(
                resource=resource, organization=b["org"], source_resource=b["app"], target_resource=b["app"]
            )
            connection.check_constraints()
