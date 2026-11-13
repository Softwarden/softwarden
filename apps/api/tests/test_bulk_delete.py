"""Bulk delete: Admin only, up to 200 records of one kind, the single delete's reference rules per record,
all or nothing, one resource.deleted event per record marked bulk, attachments and exceptions going with it."""

import time
import uuid
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from catalog import attachments, bulk, quality, services
from catalog.models import Attachment, QualityException, Resource, ResourceTag
from core import capabilities as caps
from core.models import AuditEvent


def make(a, name, kind="application", **extra):
    return services.create_resource(
        a["org"], a["users"]["editor"], {"name": name, "lifecycle": "active", "kind": kind, **extra}
    )


def depends(a, source, target):
    return services.create_relationship(
        a["org"], a["users"]["editor"], {"type": "depends_on", "source": str(source.id), "target": str(target.id)}
    )


def review(a, resource):
    return services.record_review(
        a["org"], a["users"]["editor"], resource, {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Email"}
    )


def body(ids, kind="application"):
    return {"kind": kind, "ids": [str(i) for i in ids]}


def exists(*records):
    return [Resource.objects.filter(id=r.id).exists() for r in records]


@pytest.fixture
def mixed(a):
    """Free (tags, a link attachment, a data-quality exception), Reviewed (a review), Linked and Needed (a
    relationship between them), Fed (an integration's end)."""
    free = make(a, "Free", tags=["Finance", "Old"])
    attachments.add_link(a["org"], a["users"]["editor"], free, {"url": "https://example.test/runbook"})
    quality.create_exception(
        a["org"],
        a["users"]["editor"],
        {
            "record": str(free.id),
            "rule": "documentation",
            "reason": "Vendor portal.",
            "owner": str(a["people"]["dana"].id),
        },
    )
    reviewed = make(a, "Reviewed")
    review(a, reviewed)
    review(a, reviewed)
    linked, needed = make(a, "Linked"), make(a, "Needed")
    depends(a, linked, needed)
    fed = make(a, "Fed")
    services.create_integration(
        a["org"],
        a["users"]["editor"],
        {"name": "Feed", "lifecycle": "active", "source": str(fed.id), "target": str(make(a, "Sink", "component").id)},
    )
    return {"free": free, "reviewed": reviewed, "linked": linked, "needed": needed, "fed": fed}


# ---- roles -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["bulk-delete/preview", "bulk-delete/apply"])
@pytest.mark.parametrize("role,status", [("admin", 200), ("editor", 403), ("viewer", 403)])
def test_role_matrix(a, api, path, role, status):
    record = make(a, "Doomed")
    response = api(a["users"][role]).post(path, body([record.id]))
    assert response.status_code == status, response.json_body
    assert exists(record) == [path.endswith("preview") or status != 200]


def test_the_service_checks_the_role_too(a):
    record = make(a, "Doomed")
    for call in (bulk.delete_preview, bulk.delete_apply):
        with pytest.raises(Exception, match="Only Admins"):
            call(a["org"], a["users"]["editor"], body([record.id]))
    assert exists(record) == [True]


def test_unauthenticated(api):
    assert api().post("bulk-delete/preview", {}).status_code in (401, 403)
    assert api().post("bulk-delete/apply", {}).status_code in (401, 403)


# ---- preview ---------------------------------------------------------------------------------------------------------


def test_preview_lists_what_blocks_each_record_and_writes_nothing(a, api, mixed):
    admin = api(a["users"]["admin"])
    ids = [mixed[k].id for k in ("free", "reviewed", "linked", "needed", "fed")]
    events = AuditEvent.objects.count()
    got = admin.post("bulk-delete/preview", body(ids)).json_body
    assert got["kind"] == "application"
    assert got["summary"] == {"total": 5, "deletable": 1, "blocked": 4}
    rows = {r["name"]: r for r in got["rows"]}
    assert [r["name"] for r in got["rows"]] == ["Free", "Reviewed", "Linked", "Needed", "Fed"]  # selection order
    assert rows["Free"] == {
        "id": str(mixed["free"].id),
        "name": "Free",
        "can_delete": True,
        "blocking_references": {},  # tags and attachments don't block (they go with it)
        "removes": {"attachments": 1, "quality_exceptions": 1},
        "errors": [],
    }
    assert rows["Reviewed"]["blocking_references"] == {"review": 2}
    assert rows["Reviewed"]["errors"] == ["It has 2 reviews, so it can't be deleted. Archive it instead."]
    assert rows["Linked"]["blocking_references"] == {"relationship": 1}
    assert rows["Needed"]["blocking_references"] == {"relationship": 1}
    assert rows["Fed"]["blocking_references"] == {"integration": 1}
    assert all(not rows[n]["can_delete"] for n in ("Reviewed", "Linked", "Needed", "Fed"))
    assert AuditEvent.objects.count() == events
    assert all(exists(*mixed.values()))


def test_preview_matches_the_single_delete_rule(a, mixed):
    records = list(mixed.values())
    together = services.references_to_many(records)
    for record in records:
        assert together[record.id] == services.references_to(record)


def test_preview_queries_do_not_grow_with_the_selection(a, api):
    admin = api(a["users"]["admin"])

    def selection(n, prefix):
        records = [make(a, f"{prefix}{i}", tags=["T"]) for i in range(n)]
        for r in records:
            review(a, r)
            attachments.add_link(a["org"], a["users"]["editor"], r, {"url": "https://example.test/x"})
        return [r.id for r in records]

    def count(ids):
        with CaptureQueriesContext(connection) as ctx:
            assert admin.post("bulk-delete/preview", body(ids)).status_code == 200
        return len(ctx.captured_queries)

    small = selection(2, "S")
    count(small)
    assert count(small) == count(selection(15, "L"))


# ---- apply -----------------------------------------------------------------------------------------------------------


def test_apply_is_all_or_nothing_with_409_rows(a, api, mixed):
    admin = api(a["users"]["admin"])
    ids = [mixed["free"].id, mixed["reviewed"].id, mixed["linked"].id]
    events = AuditEvent.objects.count()
    refused = admin.post("bulk-delete/apply", body(ids))
    assert refused.status_code == 409 and refused["Content-Type"] == "application/problem+json"
    assert refused.json_body["detail"] == (
        "2 of the selected records can't be deleted because other records reference them. Nothing was deleted."
    )
    assert refused.json_body["rows"] == [
        {
            "id": str(mixed["reviewed"].id),
            "name": "Reviewed",
            "blocking_references": {"review": 2},
            "errors": ["It has 2 reviews, so it can't be deleted. Archive it instead."],
        },
        {
            "id": str(mixed["linked"].id),
            "name": "Linked",
            "blocking_references": {"relationship": 1},
            "errors": ["It has 1 relationship, so it can't be deleted. Archive it instead."],
        },
    ]
    assert all(exists(*mixed.values()))
    assert AuditEvent.objects.count() == events
    assert Attachment.objects.filter(resource=mixed["free"]).exists()


def test_apply_deletes_each_record_through_the_single_delete(a, api, mixed):
    admin = api(a["users"]["admin"])
    other = make(a, "Other", tags=["Finance"])
    result = admin.post("bulk-delete/apply", body([mixed["free"].id, other.id]))
    assert result.status_code == 200, result.json_body
    assert result.json_body == {
        "kind": "application",
        "deleted": 2,
        "rows": [{"id": str(mixed["free"].id), "name": "Free"}, {"id": str(other.id), "name": "Other"}],
    }
    assert exists(mixed["free"], other) == [False, False]
    for record in (mixed["free"], other):
        event = AuditEvent.objects.get(action="resource.deleted", target_id=record.id)
        assert event.changes == {"kind": "application", "name": record.name, "bulk": True}
        assert event.actor == a["users"]["admin"]
    # the attachment and exception went with it, each audited; tag links too
    assert AuditEvent.objects.filter(action="attachment.deleted").count() == 1
    assert AuditEvent.objects.filter(action="quality_exception.deleted").count() == 1
    assert not Attachment.objects.filter(organization=a["org"], resource_id=mixed["free"].id).exists()
    assert not QualityException.objects.filter(organization=a["org"], resource_id=mixed["free"].id).exists()
    assert not ResourceTag.objects.filter(resource_id__in=[mixed["free"].id, other.id]).exists()


def test_apply_rolls_back_when_a_delete_fails_midway(a, monkeypatch):
    one, two = make(a, "One"), make(a, "Two")
    real = services.delete_resource
    calls = []

    def flaky(org, actor, resource, audit_extra=None):
        calls.append(resource.id)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(org, actor, resource, audit_extra=audit_extra)

    monkeypatch.setattr(services, "delete_resource", flaky)
    with pytest.raises(RuntimeError):
        bulk.delete_apply(a["org"], a["users"]["admin"], body([one.id, two.id]))
    assert exists(one, two) == [True, True]
    assert not AuditEvent.objects.filter(action="resource.deleted").exists()


def test_archived_records_and_agents_and_components_can_be_deleted(a, api):
    admin = api(a["users"]["admin"])
    shelved = make(a, "Shelved")
    services.archive_resource(a["org"], a["users"]["editor"], shelved)
    assert admin.post("bulk-delete/apply", body([shelved.id])).status_code == 200
    job, agent = make(a, "Job", "component"), make(a, "Helper", "ai_agent")
    assert admin.post("bulk-delete/apply", body([job.id], kind="component")).status_code == 200
    assert admin.post("bulk-delete/apply", body([agent.id], kind="ai_agent")).status_code == 200
    assert exists(shelved, job, agent) == [False, False, False]


def test_a_tagged_record_can_be_deleted_on_its_own_too(a, api):
    """Tag links are the record's own rows: they used to be counted as references ('1 tag and 1 resource tag')."""
    record = make(a, "Tagged", tags=["Finance", "Payroll"])
    assert services.references_to(record) == {}
    assert api(a["users"]["admin"]).delete(f"applications/{record.id}").status_code == 204
    # The cascaded tag links have no events of their own: the record's deleted event keeps them in its prechange.
    event = AuditEvent.objects.get(action="resource.deleted", target_id=record.id)
    assert event.prechange is not None
    assert sorted(t["display"] for t in event.prechange["tags"]) == ["Finance", "Payroll"]
    assert event.postchange is None


def through(rel: Any) -> Any:
    """A many-to-many relation's through model (typed loosely: django-stubs types the reverse side without it)."""
    return rel.through


def test_every_many_to_many_to_a_record_or_instance_has_an_explicit_through_model():
    """references_to skips a many-to-many with an explicit through model (its foreign key is counted, or owned) and
    counts one with an auto-created table. An explicit through model keeps the link rows tenant-scoped and audited."""
    from catalog.models import ApplicationInstance

    found = []
    for model in (Resource, ApplicationInstance):
        for rel in model._meta.related_objects:
            if rel.many_to_many:
                found.append((model.__name__, rel.related_model.__name__, through(rel)._meta.auto_created))
        for m2m in model._meta.many_to_many:
            found.append((model.__name__, m2m.name, through(m2m.remote_field)._meta.auto_created))
    assert found, "expected Tag.resources and RenewalCycle.applications"
    assert [f for f in found if f[2]] == []


def test_an_auto_created_many_to_many_would_be_counted(a, monkeypatch):
    from catalog.models import Tag

    rel = next(r for r in Resource._meta.related_objects if r.related_model is Tag)
    monkeypatch.setattr(through(rel)._meta, "auto_created", Tag)  # as Django marks a table it made itself
    record = make(a, "Tagged", tags=["Finance"])
    assert services.references_to(record) == {"tag": 1}


def test_a_renewal_cycle_covering_a_record_counts_once(a):
    from money import renewals

    renewals.create_renewal(
        a["org"],
        a["users"]["editor"],
        {"name": "Licence", "auto_renew": "yes", "renewal_date": "2030-01-31", "applications": [str(a["app"].id)]},
    )
    assert services.references_to(a["app"]) == {"renewal cycle": 1}


# ---- validation, tenancy, capability ---------------------------------------------------------------------------------


def test_validation(a, api):
    admin = api(a["users"]["admin"])
    rid = str(a["app"].id)
    for bad in (
        {"kind": "application", "ids": []},
        {"kind": "application"},
        {"kind": "integration", "ids": [rid]},
        {"kind": "application", "ids": ["nope"]},
        {"kind": "application", "ids": [str(uuid.uuid4()) for _ in range(201)]},
        {"kind": "application", "ids": [rid], "changes": {}},
    ):
        assert admin.post("bulk-delete/preview", bad).status_code == 400, bad
        assert admin.post("bulk-delete/apply", bad).status_code == 400, bad
    assert exists(a["app"]) == [True]


@pytest.mark.parametrize("path", ["bulk-delete/preview", "bulk-delete/apply"])
def test_cross_tenant_and_unknown_ids_are_not_found(a, b, api, path):
    admin = api(a["users"]["admin"])
    mine = make(a, "Mine")
    theirs = admin.post(path, body([mine.id, b["app"].id]))
    unknown = admin.post(path, body([mine.id, uuid.uuid4()]))
    other_kind = admin.post(path, body([mine.id], kind="component"))
    for response in (theirs, unknown, other_kind):
        assert response.status_code == 404
        assert response.json_body["detail"] == theirs.json_body["detail"]  # no hint that b's record exists
    assert exists(mine, b["app"]) == [True, True]
    assert not AuditEvent.objects.filter(action="resource.deleted").exists()


def test_agents_need_the_ai_agents_capability(a, api, monkeypatch):
    agent = make(a, "Helper", "ai_agent")
    monkeypatch.setitem(caps.EDITION_CAPABILITIES, "community", caps.COMMUNITY - {caps.AI_AGENTS})
    admin = api(a["users"]["admin"])
    for path in ("bulk-delete/preview", "bulk-delete/apply"):
        refused = admin.post(path, body([agent.id], kind="ai_agent"))
        assert refused.status_code == 403 and refused.json_body["capability"] == "ai_agents"
    assert exists(agent) == [True]
    assert admin.post("bulk-delete/preview", body([a["app"].id])).status_code == 200


# ---- scale -----------------------------------------------------------------------------------------------------------


def test_two_hundred_records(a, api):
    """The limit: preview and apply 200 records (apply goes through the single delete for each). Timings are printed
    for the report (pytest -s); the bound here is only a guard against something pathological."""
    admin = api(a["users"]["admin"])
    ids = [make(a, f"Bulk {n:03}", tags=["Cleanup"]).id for n in range(200)]
    started = time.perf_counter()
    preview = admin.post("bulk-delete/preview", body(ids)).json_body
    previewed = time.perf_counter() - started
    assert preview["summary"] == {"total": 200, "deletable": 200, "blocked": 0}
    started = time.perf_counter()
    result = admin.post("bulk-delete/apply", body(ids))
    applied = time.perf_counter() - started
    assert result.status_code == 200 and result.json_body["deleted"] == 200
    assert AuditEvent.objects.filter(action="resource.deleted", changes__bulk=True).count() == 200
    print(f"bulk delete 200: preview {previewed:.2f}s, apply {applied:.2f}s")
    assert applied < 60
