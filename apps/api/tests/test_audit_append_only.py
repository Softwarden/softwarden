"""Audit events are append-only: the model, its queryset and a database trigger refuse to
change or remove one; inserts, single and bulk, still work; purge() is the one removal path."""

import pytest
from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection, transaction

from catalog import services
from core.models import AuditAppendOnly, AuditEvent


def event(org, **kw):
    return AuditEvent.objects.create(organization=org, action="export", changes={"rows": 1}, **kw)


def test_model_refuses_save_of_an_existing_event_and_delete(a):
    held = event(a["org"])
    held.changes = {"rows": 2}
    with pytest.raises(AuditAppendOnly):
        held.save()
    with pytest.raises(AuditAppendOnly):
        held.delete()
    assert AuditEvent.objects.get(id=held.id).changes == {"rows": 1}


def test_queryset_refuses_update_and_delete_but_inserts_work(a):
    held = event(a["org"])
    events = AuditEvent.objects.filter(id=held.id)
    for attempt in (
        lambda: events.update(action="tampered"),
        lambda: events.delete(),
        lambda: AuditEvent.objects.bulk_update([held], ["action"]),
        lambda: AuditEvent.objects.update_or_create(id=held.id),
    ):
        with pytest.raises(AuditAppendOnly):
            attempt()
    made = AuditEvent.objects.bulk_create([AuditEvent(organization=a["org"], action="export") for _ in range(3)])
    assert len(made) == 3 and AuditEvent.objects.filter(action="export").count() == 4


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE catalog_auditevent SET action = 'tampered' WHERE id = %s",
        "UPDATE catalog_auditevent SET prechange = '{}'::jsonb WHERE id = %s",
        "DELETE FROM catalog_auditevent WHERE id = %s",
    ],
)
def test_database_refuses_update_and_delete(a, sql):
    held = event(a["org"])
    with pytest.raises(DatabaseError, match="append-only"), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, [held.id])
    assert AuditEvent.objects.get(id=held.id).action == "export"


def test_deleting_a_user_only_clears_the_actor(a):
    """ON DELETE SET NULL is the one update the trigger allows: actor_id to null and nothing else."""
    user = get_user_model().objects.create_user(username="leaver@orga.test", email="leaver@orga.test")
    held = event(a["org"], actor=user)
    user.delete()
    held = AuditEvent.objects.get(id=held.id)
    assert held.actor_id is None and held.action == "export"
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE catalog_auditevent SET actor_id = NULL, action = 'x' WHERE id = %s", [held.id])


def test_purge_is_the_one_removal_path(a, b):
    kept = event(b["org"])
    for _ in range(3):
        event(a["org"])
    with transaction.atomic():
        assert AuditEvent.objects.filter(organization=a["org"]).purge() == 3
    assert not AuditEvent.objects.filter(organization=a["org"]).exists()
    assert AuditEvent.objects.filter(id=kept.id).exists()
    # The setting is off again after a purge, so the trigger refuses as before in the same transaction.
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("DELETE FROM catalog_auditevent WHERE id = %s", [kept.id])


def test_services_still_write_events_in_bulk_flows(a):
    services.create_resource(a["org"], a["users"]["editor"], {"name": "Fresh", "lifecycle": "active"})
    assert AuditEvent.objects.filter(action="resource.created").exists()
