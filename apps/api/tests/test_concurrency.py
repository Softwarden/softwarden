"""Overlapping writes: a stale copy of a Person can't undo an anonymization, and concurrent
role changes can't remove the last active Admin. The threaded tests use real separate database connections."""

import threading

import pytest
from django.db import connection

from catalog import admin_services
from catalog.models import Person
from core.models import AuditEvent, Membership, Role
from core.problems import Conflict


def test_anonymize_wins_then_a_stale_edit_is_refused(a):
    stale = Person.objects.get(id=a["people"]["dana"].id)  # the PATCH view read this before anonymization
    admin_services.anonymize_person(a["org"], a["users"]["admin"], Person.objects.get(id=stale.id), "Dana Ortiz")
    with pytest.raises(Conflict):
        admin_services.update_person(a["org"], a["users"]["editor"], stale, {"title": "Payroll lead"})
    row = Person.objects.get(id=stale.id)
    assert row.anonymized_at is not None and row.name.startswith("Former person #") and row.title == ""
    assert not AuditEvent.objects.filter(action="person.updated", target_id=stale.id).exists()


def test_edit_wins_then_anonymize_clears_the_edited_details(a):
    stale = Person.objects.get(id=a["people"]["dana"].id)  # the anonymize view read this before the edit
    admin_services.update_person(
        a["org"], a["users"]["editor"], Person.objects.get(id=stale.id), {"title": "Payroll lead", "email": "d@x.test"}
    )
    admin_services.anonymize_person(a["org"], a["users"]["admin"], stale, "Dana Ortiz")
    row = Person.objects.get(id=stale.id)
    assert row.anonymized_at is not None and row.title == "" and row.email == ""
    actions = list(
        AuditEvent.objects.filter(target_id=stale.id).order_by("created_at").values_list("action", flat=True)
    )
    assert actions == ["person.updated", "person.anonymized"]


def run_together(*jobs):
    """Run each job in its own thread and connection, released at the same moment. Returns the exceptions raised."""
    barrier = threading.Barrier(len(jobs))
    errors: list = [None] * len(jobs)

    def runner(i, job):
        try:
            barrier.wait()
            job()
        except Exception as e:  # collected for the assertions
            errors[i] = e
        finally:
            connection.close()

    threads = [threading.Thread(target=runner, args=(i, j)) for i, j in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


@pytest.fixture
def two_admins(transactional_db):
    from tests.conftest import make_org

    f = make_org("race")
    second = f["users"]["editor"]
    Membership.objects.filter(user=second).update(role=Role.ADMIN)
    f["org"].coordinator = f["users"]["viewer"]  # coordinator checks out of the way
    Membership.objects.filter(user=f["users"]["viewer"]).update(role=Role.EDITOR)
    f["org"].save()
    return f, f["users"]["admin"], second


@pytest.mark.parametrize(
    "first,second",
    [
        ({"role": "viewer"}, {"role": "viewer"}),
        ({"active": False}, {"active": False}),
        ({"role": "editor"}, {"active": False}),
    ],
)
def test_concurrent_changes_keep_one_active_admin(two_admins, first, second):
    f, one, two = two_admins
    org = f["org"]
    for _ in range(5):
        Membership.objects.filter(user__in=[one, two]).update(role=Role.ADMIN)
        type(one).objects.filter(id__in=[one.id, two.id]).update(is_active=True)
        m1, m2 = Membership.objects.get(user=one), Membership.objects.get(user=two)
        errors = run_together(
            lambda m1=m1: admin_services.update_member(org, one, m1, first),
            lambda m2=m2: admin_services.update_member(org, two, m2, second),
        )
        assert Membership.objects.filter(organization=org, role=Role.ADMIN, user__is_active=True).count() == 1
        refused = [e for e in errors if e is not None]
        assert len(refused) == 1 and isinstance(refused[0], Conflict)
        assert "last active Admin" in str(refused[0].detail)


def test_another_organization_is_not_blocked(two_admins):
    from tests.conftest import make_org

    f, one, _ = two_admins
    other = make_org("other")
    other_editor = Membership.objects.get(user=other["users"]["viewer"])
    errors = run_together(
        lambda: admin_services.update_member(f["org"], one, Membership.objects.get(user=one), {"role": "viewer"}),
        lambda: admin_services.update_member(other["org"], other["users"]["admin"], other_editor, {"role": "editor"}),
    )
    assert errors == [None, None]


def test_concurrent_removals_from_two_organizations_leave_one_membership(transactional_db):
    """Two Admins remove the same person from their two organizations at once: the memberships are locked in one
    order, so the second sees the first's removal and is refused instead of leaving the account with none."""
    from tests.conftest import make_org

    one, two = make_org("raceone"), make_org("racetwo")
    person = one["users"]["editor"]
    for _ in range(5):
        Membership.objects.filter(user=person).exclude(organization=one["org"]).delete()
        Membership.objects.get_or_create(user=person, organization=one["org"], defaults={"role": Role.EDITOR})
        Membership.objects.create(user=person, organization=two["org"], role=Role.EDITOR)
        m1 = Membership.objects.get(user=person, organization=one["org"])
        m2 = Membership.objects.get(user=person, organization=two["org"])
        errors = run_together(
            lambda m1=m1: admin_services.remove_member(one["org"], one["users"]["admin"], m1),
            lambda m2=m2: admin_services.remove_member(two["org"], two["users"]["admin"], m2),
        )
        assert Membership.objects.filter(user=person).count() == 1
        refused = [e for e in errors if e is not None]
        assert len(refused) == 1 and isinstance(refused[0], Conflict)
