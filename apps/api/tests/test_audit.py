import pytest

from catalog import services
from core.models import AuditEvent


@pytest.mark.django_db
def test_person_audit_references_the_id_not_the_name(a):
    person = services.create_person(a["org"], a["users"]["editor"], {"name": "Avery Quinn", "email": "aq@example.test"})
    event = AuditEvent.objects.get(action="person.created", target_id=person.id)
    assert event.target_type == "Person"
    assert "Avery" not in str(event.changes) and "aq@example.test" not in str(event.changes)


@pytest.mark.django_db
def test_secrets_are_redacted_at_any_depth(a):
    services.audit(
        a["org"],
        None,
        "test.event",
        None,
        {
            "new_password": "hunter2-hunter2",
            "nested": {"api_token": "abc", "role": "editor"},
            "rows": [{"sessionid": "x"}],
        },
    )
    changes = AuditEvent.objects.get(action="test.event").changes
    assert changes == {
        "new_password": "[redacted]",
        "nested": {"api_token": "[redacted]", "role": "editor"},
        "rows": [{"sessionid": "[redacted]"}],
    }


@pytest.mark.django_db
def test_person_fields_are_dropped_even_if_passed(a):
    person = a["people"]["dana"]
    services.audit(a["org"], None, "person.updated", person, {"name": "Someone", "fields": ["name"]})
    assert AuditEvent.objects.get(action="person.updated").changes == {"fields": ["name"]}
