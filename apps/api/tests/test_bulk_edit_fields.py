"""Bulk edit: lifecycle (with the retirement rule), criticality, IT steward, department and custom fields.
The first fields (owners, support team, tags, next review date) are in test_bulk_edit.py."""

import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from catalog import bulk, customfields, services, tags
from catalog.models import Department, Resource
from core import capabilities as caps
from core.models import AuditEvent
from core.problems import Conflict, FieldErrors


def make(a, name, kind="application", **extra):
    return services.create_resource(
        a["org"], a["users"]["editor"], {"name": name, "lifecycle": "active", "kind": kind, **extra}
    )


def depends(a, source, target, rel_type="depends_on"):
    return services.create_relationship(
        a["org"], a["users"]["editor"], {"type": rel_type, "source": str(source.id), "target": str(target.id)}
    )


def review(a, resource):
    return services.record_review(
        a["org"], a["users"]["editor"], resource, {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Email"}
    )


def body(ids, kind="application", **changes):
    return {"kind": kind, "ids": [str(i) for i in ids], "changes": changes}


def field(a, name, type="text", applies_to=("application",), **extra):
    data = {"name": name, "label": name.replace("_", " ").capitalize(), "type": type, "applies_to": list(applies_to)}
    return customfields.create_field(a["org"], a["users"]["admin"], {**data, **extra})


def fresh(resource):
    return Resource.objects.get(id=resource.id)


def bulk_events(resource):
    return AuditEvent.objects.filter(action="resource.updated", target_id=resource.id, changes__bulk=True)


# ---- lifecycle -------------------------------------------------------------------------------------------------------


def test_lifecycle_preview_apply_and_no_op(a, api):
    editor = api(a["users"]["editor"])
    one, two = make(a, "One"), make(a, "Two", lifecycle="deprecated")
    review(a, one)
    preview = editor.post("bulk-edit/preview", body([one.id, two.id], lifecycle="deprecated")).json_body
    rows = {r["name"]: r for r in preview["rows"]}
    assert rows["One"]["changes"] == {"lifecycle": {"before": "active", "after": "deprecated"}}
    assert rows["One"]["will_need_confirmation"] is True  # lifecycle is a confirmed field
    assert rows["Two"]["unchanged"] is True and rows["Two"]["changes"] == {}
    assert fresh(one).lifecycle == "active"
    result = editor.post("bulk-edit/apply", body([one.id, two.id], lifecycle="deprecated")).json_body
    assert (result["updated"], result["unchanged"]) == (1, 1)
    assert fresh(one).lifecycle == "deprecated"
    assert bulk_events(one).get().changes["lifecycle"] == ["active", "deprecated"]
    assert not bulk_events(two).exists()


def test_lifecycle_must_be_a_status(a, api):
    refused = api(a["users"]["editor"]).post("bulk-edit/preview", body([a["app"].id], lifecycle="gone"))
    assert refused.status_code == 400 and "lifecycle" in refused.json_body["errors"]


def test_retiring_a_record_others_depend_on_is_refused_per_row(a, api):
    editor = api(a["users"]["editor"])
    target, free, user = make(a, "Target"), make(a, "Free"), make(a, "User")
    depends(a, user, target)
    request = body([target.id, free.id], lifecycle="retired", add_tags=["Sunset"])
    preview = editor.post("bulk-edit/preview", request).json_body
    rows = {r["name"]: r for r in preview["rows"]}
    assert rows["Free"]["errors"] == []
    assert len(rows["Target"]["errors"]) == 1 and "depended on by 1 active record: User" in rows["Target"]["errors"][0]
    assert preview["summary"]["errors"] == 1
    refused = editor.post("bulk-edit/apply", request)
    assert refused.status_code == 400
    assert [(r["name"], r["field_errors"]) for r in refused.json_body["rows"]] == [("Target", {})]
    assert fresh(free).lifecycle == "active" and fresh(target).lifecycle == "active"  # all or nothing
    assert not AuditEvent.objects.filter(changes__bulk=True).exists()


def test_inactive_dependents_and_non_dependency_links_do_not_block(a, api):
    editor = api(a["users"]["editor"])
    target = make(a, "Target")
    retired, archived, other = make(a, "Old"), make(a, "Shelved"), make(a, "Successor")
    depends(a, retired, target)
    depends(a, archived, target)
    services.update_resource(a["org"], a["users"]["editor"], retired, {"lifecycle": "retired"})
    services.archive_resource(a["org"], a["users"]["editor"], archived)
    depends(a, other, target, "replaces")  # not a dependency type
    response = editor.post("bulk-edit/apply", body([target.id], lifecycle="retired"))
    assert response.status_code == 200, response.json_body
    assert fresh(target).lifecycle == "retired"


def test_dependents_retired_in_the_same_edit_go_first(a, api):
    editor = api(a["users"]["editor"])
    base, middle, top = make(a, "Base"), make(a, "Middle"), make(a, "Top")
    depends(a, middle, base)
    depends(a, top, middle)
    # Selected with the depended-on record first: apply still retires Top, then Middle, then Base.
    request = body([base.id, middle.id, top.id], lifecycle="retired")
    preview = editor.post("bulk-edit/preview", request).json_body
    assert preview["summary"]["errors"] == 0 and preview["summary"]["changed"] == 3
    assert editor.post("bulk-edit/apply", request).status_code == 200
    assert {fresh(r).lifecycle for r in (base, middle, top)} == {"retired"}
    events = AuditEvent.objects.filter(action="resource.updated", changes__bulk=True).order_by("created_at", "id")
    assert [e.target_id for e in events] == [top.id, middle.id, base.id]
    assert all("retirement_ack" not in e.changes for e in events)  # nothing needed acknowledging


def test_records_depending_on_each_other_in_a_loop_are_refused(a, api):
    editor = api(a["users"]["editor"])
    one, two, under = make(a, "One"), make(a, "Two"), make(a, "Under")
    depends(a, one, two)
    depends(a, two, one)
    depends(a, one, under)
    preview = editor.post("bulk-edit/preview", body([one.id, two.id, under.id], lifecycle="retired")).json_body
    rows = {r["name"]: r for r in preview["rows"]}
    assert "in a loop" in rows["One"]["errors"][0] and "Two" in rows["One"]["errors"][0]
    assert "in a loop" in rows["Two"]["errors"][0]
    assert "in a loop" in rows["Under"]["errors"][0]  # waits on One, which can't go first
    assert editor.post("bulk-edit/apply", body([one.id, two.id, under.id], lifecycle="retired")).status_code == 400
    assert {fresh(r).lifecycle for r in (one, two, under)} == {"active"}


def test_a_refusal_found_only_at_apply_is_a_row_error(a, api, monkeypatch):
    """The plan is checked again by update_resource under the row locks: a dependency recorded between preview and
    apply is caught by the record's own retirement check, reported on its row, and nothing is saved."""
    editor = api(a["users"]["editor"])
    first, target = make(a, "First"), make(a, "Target")
    depends(a, make(a, "User"), target)
    monkeypatch.setattr(bulk, "_retirements", lambda org, by_id, retiring: ({}, list(retiring)))
    refused = editor.post("bulk-edit/apply", body([first.id, target.id], lifecycle="retired"))
    assert refused.status_code == 400
    [row] = refused.json_body["rows"]
    assert row["name"] == "Target" and "still depended on by 1 active record" in row["errors"][0]
    assert fresh(first).lifecycle == "active"
    assert not AuditEvent.objects.filter(changes__bulk=True).exists()


def test_field_errors_from_a_record_write_map_to_its_row(a, monkeypatch):
    one, two = make(a, "One"), make(a, "Two")
    real = services.update_resource

    def refuse_two(org, actor, resource, data, audit_extra=None):
        if resource.id == two.id:
            raise FieldErrors({"custom_field_data.headcount": ["Enter a whole number."]})
        return real(org, actor, resource, data, audit_extra=audit_extra)

    monkeypatch.setattr(services, "update_resource", refuse_two)
    with pytest.raises(bulk.BulkRefused) as refused:
        bulk.apply(a["org"], a["users"]["editor"], body([one.id, two.id], criticality="high"))
    assert refused.value.extra["rows"] == [
        {
            "id": str(two.id),
            "name": "Two",
            "errors": [],
            "codes": [],
            "field_errors": {"custom_field_data.headcount": ["Enter a whole number."]},
        }
    ]
    assert fresh(one).criticality == ""  # rolled back with the rest


# ---- row error codes: clients key off these, not the words ---------------------------------------------------


def test_retirement_refusals_carry_codes_in_preview_and_apply(a, api):
    editor = api(a["users"]["editor"])
    target, free, user = make(a, "Target"), make(a, "Free"), make(a, "User")
    depends(a, user, target)
    one, two = make(a, "One"), make(a, "Two")
    depends(a, one, two)
    depends(a, two, one)
    request = body([target.id, free.id, one.id, two.id], lifecycle="retired")
    rows = {r["name"]: r for r in editor.post("bulk-edit/preview", request).json_body["rows"]}
    assert rows["Target"]["codes"] == ["retire_dependents"]
    assert rows["Target"]["errors"][0].startswith("Still depended on by 1 active record: User.")  # words kept
    assert rows["One"]["codes"] == rows["Two"]["codes"] == ["retire_dependency_loop"]
    assert rows["Free"]["codes"] == rows["Free"]["errors"] == []
    refused = editor.post("bulk-edit/apply", request)
    assert refused.status_code == 400
    assert {r["name"]: r["codes"] for r in refused.json_body["rows"]} == {
        "Target": ["retire_dependents"],
        "One": ["retire_dependency_loop"],
        "Two": ["retire_dependency_loop"],
    }
    assert all(len(r["codes"]) == len(r["errors"]) for r in refused.json_body["rows"])


def test_a_retirement_refused_only_at_apply_has_the_same_code(a, api, monkeypatch):
    target = make(a, "Target")
    depends(a, make(a, "User"), target)
    monkeypatch.setattr(bulk, "_retirements", lambda org, by_id, retiring: ({}, list(retiring)))
    refused = api(a["users"]["editor"]).post("bulk-edit/apply", body([target.id], lifecycle="retired"))
    [row] = refused.json_body["rows"]
    assert row["codes"] == ["retire_dependents"] and len(row["errors"]) == 1


def test_another_conflict_at_apply_is_coded_conflict(a, monkeypatch):
    one = make(a, "One")

    def conflict(org, actor, resource, data, audit_extra=None):
        raise Conflict("Someone else changed it.")

    monkeypatch.setattr(services, "update_resource", conflict)
    with pytest.raises(bulk.BulkRefused) as refused:
        bulk.apply(a["org"], a["users"]["editor"], body([one.id], criticality="high"))
    [row] = refused.value.extra["rows"]
    assert (row["errors"], row["codes"]) == (["Someone else changed it."], ["conflict"])


def test_every_row_error_has_its_code(a, api):
    editor = api(a["users"]["editor"])
    shelved, unreviewed, reviewed = make(a, "Shelved"), make(a, "Unreviewed"), make(a, "Reviewed")
    services.archive_resource(a["org"], a["users"]["editor"], shelved)
    last = review(a, reviewed)
    early = last.confirmation_date.isoformat()
    preview = editor.post("bulk-edit/preview", body([shelved.id, unreviewed.id, reviewed.id], next_review_date=early))
    rows = {r["name"]: r["codes"] for r in preview.json_body["rows"]}
    assert rows == {
        "Shelved": ["archived", "no_review"],
        "Unreviewed": ["no_review"],
        "Reviewed": ["review_before_confirmation"],
    }
    services.update_resource(
        a["org"], a["users"]["editor"], unreviewed, {"tags": [f"T{i}" for i in range(tags.PER_RECORD_MAX)]}
    )
    preview = editor.post("bulk-edit/preview", body([unreviewed.id], add_tags=["One more"])).json_body
    assert preview["rows"][0]["codes"] == ["too_many_tags"]
    assert set(bulk.ROW_CODES) == {
        "archived",
        "retire_dependents",
        "retire_dependency_loop",
        "too_many_tags",
        "no_review",
        "review_before_confirmation",
        "conflict",
    }


# ---- criticality, IT steward, department ------------------------------------------------------------------------------


def test_criticality_set_clear_and_validation(a, api):
    editor = api(a["users"]["editor"])
    one, two = make(a, "One", criticality="high"), make(a, "Two")
    preview = editor.post("bulk-edit/preview", body([one.id, two.id], criticality="high")).json_body
    rows = {r["name"]: r for r in preview["rows"]}
    assert rows["One"]["unchanged"] is True
    assert rows["Two"]["changes"] == {"criticality": {"before": None, "after": "high"}}
    assert editor.post("bulk-edit/apply", body([one.id, two.id], criticality=None)).json_body["updated"] == 1
    assert fresh(one).criticality == "" and bulk_events(one).get().changes["criticality"] == ["high", None]
    refused = editor.post("bulk-edit/preview", body([one.id], criticality="extreme"))
    assert refused.status_code == 400 and "criticality" in refused.json_body["errors"]


def test_it_steward(a, b, api):
    editor = api(a["users"]["editor"])
    app = a["app"]  # its steward is the editor
    admin = a["users"]["admin"]
    admin.first_name, admin.last_name = "Ada", "Admin"
    admin.save()
    preview = editor.post("bulk-edit/preview", body([app.id], it_steward=admin.id)).json_body
    assert preview["rows"][0]["changes"]["it_steward"] == {
        "before": {"id": str(a["users"]["editor"].id), "name": "editor@orga.test"},
        "after": {"id": str(admin.id), "name": "Ada Admin"},
    }
    assert editor.post("bulk-edit/apply", body([app.id], it_steward=admin.id)).status_code == 200
    assert fresh(app).it_steward == admin
    same = editor.post("bulk-edit/preview", body([app.id], it_steward=admin.id)).json_body
    assert same["rows"][0]["unchanged"] is True
    # a Viewer, another organization's user, nonsense and an ID past bigint
    for refused_id in (
        a["users"]["viewer"].id,
        b["users"]["admin"].id,
        "abc",
        "²",
        "１２",
        True,
        2**64,
        str(2**64),
        -1,
        0,
        1.5,
    ):
        refused = editor.post("bulk-edit/preview", body([app.id], it_steward=refused_id))
        assert refused.status_code == 400 and "it_steward" in refused.json_body["errors"], refused_id
    assert editor.post("bulk-edit/preview", body([app.id], it_steward=str(admin.id))).status_code == 200
    assert editor.post("bulk-edit/apply", body([app.id], it_steward=None)).status_code == 200
    assert fresh(app).it_steward is None


def test_department(a, b, api):
    editor = api(a["users"]["editor"])
    finance = Department.objects.create(organization=a["org"], name="Finance")
    theirs = Department.objects.create(organization=b["org"], name="Finance")
    preview = editor.post("bulk-edit/preview", body([a["app"].id], department=str(finance.id))).json_body
    assert preview["rows"][0]["changes"] == {
        "department": {"before": None, "after": {"id": str(finance.id), "name": "Finance"}}
    }
    assert preview["rows"][0]["will_need_confirmation"] is False  # not a confirmed field
    assert editor.post("bulk-edit/apply", body([a["app"].id], department=str(finance.id))).status_code == 200
    assert fresh(a["app"]).department == finance
    refused = editor.post("bulk-edit/preview", body([a["app"].id], department=str(theirs.id)))
    assert refused.status_code == 400 and "department" in refused.json_body["errors"]


# ---- custom fields ---------------------------------------------------------------------------------------------------


def test_custom_fields_set_and_clear_with_before_and_after(a, api):
    editor = api(a["users"]["editor"])
    field(a, "business_case")
    field(a, "tier", type="select", choices=["gold", "silver"])
    one = make(a, "One", custom_field_data={"business_case": "Payroll", "tier": "gold"})
    two = make(a, "Two")
    changes = {"custom_field_data": {"tier": "silver", "business_case": None}}
    preview = editor.post("bulk-edit/preview", body([one.id, two.id], **changes)).json_body
    rows = {r["name"]: r for r in preview["rows"]}
    assert rows["One"]["changes"] == {
        "custom_field_data.tier": {"before": "gold", "after": "silver"},
        "custom_field_data.business_case": {"before": "Payroll", "after": None},
    }
    assert rows["Two"]["changes"] == {"custom_field_data.tier": {"before": None, "after": "silver"}}
    assert fresh(one).custom_field_data == {"business_case": "Payroll", "tier": "gold"}
    assert editor.post("bulk-edit/apply", body([one.id, two.id], **changes)).json_body["updated"] == 2
    assert fresh(one).custom_field_data == {"tier": "silver"}
    assert fresh(two).custom_field_data == {"tier": "silver"}
    event = bulk_events(one).get().changes
    assert event["custom_field_data.tier"] == ["gold", "silver"]
    assert event["custom_field_data.business_case"] == ["Payroll", None]
    # the same values again change nothing
    again = editor.post("bulk-edit/apply", body([one.id, two.id], **changes)).json_body
    assert (again["updated"], again["unchanged"]) == (0, 2)
    assert bulk_events(one).count() == 1


def test_custom_fields_are_validated_as_single_edits(a, api):
    editor = api(a["users"]["editor"])
    field(a, "tier", type="select", choices=["gold", "silver"])
    field(a, "headcount", type="integer")
    field(a, "owner_code", required=True)
    field(a, "job_name", applies_to=("component",))
    app = make(a, "App", custom_field_data={"owner_code": "X1"})
    path = "bulk-edit/preview"

    def errors(value):
        response = editor.post(path, body([app.id], custom_field_data=value))
        assert response.status_code == 400, response.json_body
        return response.json_body["errors"]

    assert errors({"tier": "bronze"}) == {"custom_field_data.tier": ["Choose one of: gold, silver."]}
    assert errors({"headcount": "many"}) == {"custom_field_data.headcount": ["Enter a whole number."]}
    assert errors({"owner_code": None}) == {"custom_field_data.owner_code": ["Owner code is required."]}
    assert errors({"owner_code": ""}) == {"custom_field_data.owner_code": ["Owner code is required."]}
    assert "doesn't apply" in errors({"job_name": "x"})["custom_field_data.job_name"][0]
    assert errors({"nope": 1}) == {"custom_field_data.nope": ["There is no custom field with this name."]}
    assert "custom_field_data" in errors({})
    assert "custom_field_data" in errors("tier=gold")
    # setting a required field (rather than clearing it) is fine, and a component field works on components
    assert editor.post(path, body([app.id], custom_field_data={"owner_code": "X2"})).status_code == 200
    job = make(a, "Job", "component")
    applied = editor.post("bulk-edit/apply", body([job.id], kind="component", custom_field_data={"job_name": "n"}))
    assert applied.status_code == 200 and fresh(job).custom_field_data == {"job_name": "n"}
    assert fresh(app).custom_field_data == {"owner_code": "X1"}


def test_custom_field_values_are_normalised_before_comparing(a, api):
    editor = api(a["users"]["editor"])
    field(a, "regions", type="multiselect", choices=["eu", "us", "apac"])
    app = make(a, "App", custom_field_data={"regions": ["eu", "us"]})
    preview = editor.post("bulk-edit/preview", body([app.id], custom_field_data={"regions": ["us", "eu"]})).json_body
    assert preview["rows"][0]["unchanged"] is True


# ---- everything together, tenancy, capability, query counts ------------------------------------------------------------


def test_all_new_fields_in_one_event_per_record(a, api):
    editor = api(a["users"]["editor"])
    field(a, "business_case")
    finance = Department.objects.create(organization=a["org"], name="Finance")
    ids = [make(a, f"R{n}").id for n in range(3)]
    changes = {
        "lifecycle": "deprecated",
        "criticality": "low",
        "department": str(finance.id),
        "it_steward": a["users"]["admin"].id,
        "custom_field_data": {"business_case": "Consolidate"},
        "technical_owner": str(a["people"]["dana"].id),
    }
    assert editor.post("bulk-edit/apply", body(ids, **changes)).json_body["updated"] == 3
    for rid in ids:
        event = AuditEvent.objects.get(action="resource.updated", target_id=rid, changes__bulk=True)
        assert {"lifecycle", "criticality", "department", "it_steward", "custom_field_data.business_case"} <= set(
            event.changes
        )


def test_cross_tenant_ids_are_unknown(a, b, api):
    editor = api(a["users"]["editor"])
    for path in ("bulk-edit/preview", "bulk-edit/apply"):
        response = editor.post(path, body([a["app"].id, b["app"].id], lifecycle="retired"))
        assert response.status_code == 404
        assert response.json_body["detail"] == "Some of the selected records don't exist or you don't have access."
        unknown = editor.post(path, body([a["app"].id, uuid.uuid4()], lifecycle="retired")).json_body
        assert unknown["detail"] == response.json_body["detail"]
    assert fresh(b["app"]).lifecycle == "active" and fresh(a["app"]).lifecycle == "active"


def test_agents_need_the_ai_agents_capability(a, api, monkeypatch):
    agent = make(a, "Helper", "ai_agent")
    editor = api(a["users"]["editor"])
    assert editor.post("bulk-edit/apply", body([agent.id], kind="ai_agent", criticality="high")).status_code == 200
    monkeypatch.setitem(caps.EDITION_CAPABILITIES, "community", caps.COMMUNITY - {caps.AI_AGENTS})
    for path in ("bulk-edit/preview", "bulk-edit/apply"):
        refused = editor.post(path, body([agent.id], kind="ai_agent", criticality="low"))
        assert refused.status_code == 403 and refused.json_body["capability"] == "ai_agents"
    assert editor.post("bulk-edit/apply", body([a["app"].id], criticality="low")).status_code == 200
    assert fresh(agent).criticality == "high"


def test_preview_queries_do_not_grow_with_the_selection(a, api):
    editor = api(a["users"]["editor"])
    field(a, "business_case")
    finance = Department.objects.create(organization=a["org"], name="Finance")

    def selection(n, prefix):
        records = [make(a, f"{prefix}{i}", tags=["T"]) for i in range(n)]
        for r in records:
            review(a, r)
            user = make(a, f"{r.name} user")
            depends(a, user, r)
            services.archive_resource(a["org"], a["users"]["editor"], user)
        return [r.id for r in records]

    changes = {
        "lifecycle": "retired",
        "criticality": "low",
        "department": str(finance.id),
        "it_steward": a["users"]["admin"].id,
        "custom_field_data": {"business_case": "x"},
        "add_tags": ["New"],
        "business_owner": str(a["people"]["dana"].id),
    }

    def count(ids):
        with CaptureQueriesContext(connection) as ctx:
            assert editor.post("bulk-edit/preview", body(ids, **changes)).status_code == 200
        return len(ctx.captured_queries)

    small = selection(2, "S")
    count(small)  # warm the request's one-off lookups
    assert count(small) == count(selection(12, "L"))


def test_two_hundred_records(a, api):
    """The limit: preview and apply 200 records with several fields (apply goes through update_resource for each).
    Timings are printed for the report (pytest -s); the bound is only a guard against something pathological."""
    import time

    editor = api(a["users"]["editor"])
    field(a, "business_case")
    ids = [make(a, f"Bulk {n:03}").id for n in range(200)]
    changes = {
        "lifecycle": "deprecated",
        "criticality": "low",
        "custom_field_data": {"business_case": "Consolidate"},
        "add_tags": ["Cleanup"],
        "technical_owner": str(a["people"]["dana"].id),
    }
    started = time.perf_counter()
    preview = editor.post("bulk-edit/preview", body(ids, **changes)).json_body
    previewed = time.perf_counter() - started
    assert preview["summary"]["changed"] == 200
    started = time.perf_counter()
    result = editor.post("bulk-edit/apply", body(ids, **changes))
    applied = time.perf_counter() - started
    assert result.status_code == 200 and result.json_body["updated"] == 200
    assert AuditEvent.objects.filter(action="resource.updated", changes__bulk=True).count() == 200
    print(f"bulk edit 200: preview {previewed:.2f}s, apply {applied:.2f}s")
    assert applied < 60


def test_single_edit_steward_input_is_validated_like_bulk(a, b, api):
    """Single and bulk edits share services.steward_user: bad input is a field error, never a server error."""
    editor = api(a["users"]["editor"])
    url = f"applications/{a['app'].id}"
    for bad in ("abc", "²", "１２", True, 2**64, -1, a["users"]["viewer"].id, b["users"]["admin"].id):
        refused = editor.patch(url, {"it_steward": bad})
        assert refused.status_code == 400, (bad, refused.status_code)
        assert refused.json_body["errors"]["it_steward"] == ["Choose an Admin or Editor in this organization."], bad
    assert editor.patch(url, {"it_steward": str(a["users"]["admin"].id)}).status_code == 200
    assert fresh(a["app"]).it_steward == a["users"]["admin"]
