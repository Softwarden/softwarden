"""The relationships table: composite foreign keys, the unique index and the not-self check."""

import pytest
from django.db import IntegrityError, connection, transaction

from catalog import services
from catalog.models import Relationship
from tests.conftest import rel_type


def record(ctx, name, kind="application"):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": "active"}
    )


def instance(ctx, application, name="Production"):
    return services.create_instance(ctx["org"], ctx["users"]["editor"], application, {"name": name})


# ---- The table ----------------------------------------------------------------------------------------------


def test_database_rejects_a_link_to_another_organizations_record(a, b):
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Relationship.objects.create(
                organization=a["org"], type=rel_type(a["org"], "depends_on"), source=a["app"], target=b["app"]
            )
            connection.check_constraints()  # the composite FK is deferred; force the check


def test_database_rejects_an_instance_of_another_application(a):
    other = record(a, "Other")
    theirs = instance(a, other)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Relationship.objects.create(
                organization=a["org"],
                type=rel_type(a["org"], "depends_on"),
                source=a["app"],
                source_instance=theirs,
                target=other,
            )
            connection.check_constraints()


def test_database_rejects_a_duplicate_link_with_and_without_instances(a):
    other = record(a, "Other")
    prod = instance(a, a["app"])
    for source_instance in (None, prod):
        Relationship.objects.create(
            organization=a["org"],
            type=rel_type(a["org"], "depends_on"),
            source=a["app"],
            source_instance=source_instance,
            target=other,
        )
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Relationship.objects.create(
                    organization=a["org"],
                    type=rel_type(a["org"], "depends_on"),
                    source=a["app"],
                    source_instance=source_instance,
                    target=other,
                )
    assert Relationship.objects.count() == 2  # an instance makes a different link


def test_database_rejects_a_record_linked_to_itself_unless_instances_differ(a):
    prod, sandbox = instance(a, a["app"]), instance(a, a["app"], "Sandbox")
    for ends in ({}, {"source_instance": prod, "target_instance": prod}):
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Relationship.objects.create(
                    organization=a["org"],
                    type=rel_type(a["org"], "depends_on"),
                    source=a["app"],
                    target=a["app"],
                    **ends,
                )
    Relationship.objects.create(
        organization=a["org"],
        type=rel_type(a["org"], "depends_on"),
        source=a["app"],
        source_instance=prod,
        target=a["app"],
        target_instance=sandbox,
    )
