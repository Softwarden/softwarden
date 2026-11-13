"""Retirement with active dependents: acknowledgment and reason, links kept."""

from catalog import services
from catalog.models import ChangeAssessment, Relationship
from core.models import AuditEvent

PROBLEM = "/problems/retirement-needs-acknowledgment"
ACK = {"retirement_ack": True, "retirement_ack_reason": "Kiosk moves to the new vendor on 2026-12-01"}


def record(ctx, name, kind="application", lifecycle="active"):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": lifecycle}
    )


def depends(ctx, source, target):
    """source depends on target."""
    return services.create_relationship(
        ctx["org"], ctx["users"]["editor"], {"type": "depends_on", "source": str(source.id), "target": str(target.id)}
    )


def retire(client, resource, kind="applications", **extra):
    return client.patch(f"{kind}/{resource.id}", {"lifecycle": "retired", **extra})


def scenario(ctx):
    """Legacy has an active app, a deprecated component, and a retired, an archived and a proposed dependent."""
    legacy = record(ctx, "Legacy")
    active, deprecated = record(ctx, "Kiosk"), record(ctx, "Badge job", "component", "deprecated")
    retired, archived, proposed = (
        record(ctx, "Old reports", lifecycle="retired"),
        record(ctx, "Shelved"),
        record(ctx, "Next", lifecycle="proposed"),
    )
    services.archive_resource(ctx["org"], ctx["users"]["editor"], archived)
    for dependent in (active, deprecated, retired, archived, proposed):
        depends(ctx, dependent, legacy)
    return legacy, active, deprecated


# ---- the record rule -----------------------------------------------------------------------------------------------


def test_retiring_with_active_dependents_is_refused_and_lists_them(a, api):
    legacy, active, deprecated = scenario(a)
    response = retire(api(a["users"]["editor"]), legacy)
    assert response.status_code == 409
    assert response["Content-Type"] == "application/problem+json"
    body = response.json_body
    assert body["type"] == PROBLEM and body["status"] == 409
    assert body["detail"] == (
        "Legacy is still depended on by 2 active records: Badge job, Kiosk. "
        "Acknowledge them and give a reason to continue."
    )
    assert [(d["name"], d["kind"], d["lifecycle"]) for d in body["dependents"]] == [
        ("Badge job", "component", "deprecated"),
        ("Kiosk", "application", "active"),
    ]
    assert {(d["name"], d["reason"]) for d in body["not_required"]} == {
        ("Next", "proposed"),
        ("Old reports", "retired"),
        ("Shelved", "archived"),
    }
    legacy.refresh_from_db()
    assert legacy.lifecycle == "active"  # nothing saved
    assert not AuditEvent.objects.filter(action="resource.updated", target_id=legacy.id).exists()


def test_the_acknowledgment_needs_both_the_flag_and_a_reason(a, api):
    legacy, *_ = scenario(a)
    client = api(a["users"]["editor"])
    only_reason = retire(client, legacy, retirement_ack_reason="Because")
    assert only_reason.status_code == 409 and only_reason.json_body["type"] == PROBLEM
    assert retire(client, legacy, retirement_ack=False, retirement_ack_reason="Because").status_code == 409
    no_reason = retire(client, legacy, retirement_ack=True)
    assert no_reason.status_code == 400
    assert no_reason.json_body["errors"]["retirement_ack_reason"] == [
        "Give a reason for retiring it while records depend on it."
    ]
    blank = retire(client, legacy, retirement_ack=True, retirement_ack_reason="   ")
    assert blank.status_code == 400
    too_long = retire(client, legacy, retirement_ack=True, retirement_ack_reason="x" * 5001)
    assert too_long.status_code == 400
    legacy.refresh_from_db()
    assert legacy.lifecycle == "active"


def test_acknowledged_retirement_saves_and_puts_the_reason_and_dependents_in_the_audit(a, api):
    legacy, active, deprecated = scenario(a)
    links_before = Relationship.objects.count()
    response = retire(api(a["users"]["editor"]), legacy, **ACK)
    assert response.status_code == 200 and response.json_body["lifecycle"] == "retired"
    event = AuditEvent.objects.get(action="resource.updated", target_id=legacy.id)
    assert event.changes["lifecycle"] == ["active", "retired"]
    assert event.changes["retirement_ack"] == {
        "reason": ACK["retirement_ack_reason"],
        "dependents": [str(deprecated.id), str(active.id)],
    }
    assert Relationship.objects.count() == links_before  # links are never removed
    explored = api(a["users"]["viewer"]).get(f"dependencies?target={legacy.id}&include_retired=true")
    assert {n["name"] for n in explored.json_body["nodes"]} == {"Kiosk", "Badge job", "Old reports", "Shelved", "Next"}


def test_only_retired_archived_or_not_yet_active_dependents_need_no_acknowledgment(a, api):
    legacy = record(a, "Legacy")
    for name, lifecycle in (("Old", "retired"), ("Idea", "proposed"), ("Trial", "evaluating"), ("Shelved", "active")):
        dependent = record(a, name, lifecycle=lifecycle)
        depends(a, dependent, legacy)
        if name == "Shelved":
            services.archive_resource(a["org"], a["users"]["editor"], dependent)
    response = retire(api(a["users"]["editor"]), legacy)
    assert response.status_code == 200
    assert "retirement_ack" not in AuditEvent.objects.get(action="resource.updated", target_id=legacy.id).changes


def test_no_dependents_means_no_rule_and_acknowledgment_fields_are_not_stored(a, api):
    lonely = record(a, "Lonely")
    depends(a, lonely, record(a, "Backend"))  # it depends on something; nothing depends on it
    response = retire(api(a["users"]["editor"]), lonely, **ACK)
    assert response.status_code == 200
    assert "retirement_ack" not in AuditEvent.objects.get(action="resource.updated", target_id=lonely.id).changes


def test_the_rule_applies_to_a_component_target_and_ignores_other_link_types_and_self_links(a, api):
    job = record(a, "Nightly job", "component")
    feed = services.create_integration(
        a["org"],
        a["users"]["editor"],
        {"name": "Feed", "lifecycle": "active", "source": str(a["app"].id), "target": str(job.id)},
    )
    services.create_relationship(
        a["org"], a["users"]["editor"], {"type": "implements", "source": str(job.id), "target": str(feed.id)}
    )
    other = record(a, "Other job", "component")
    services.create_relationship(
        a["org"], a["users"]["editor"], {"type": "replaces", "source": str(other.id), "target": str(job.id)}
    )
    prod = services.create_instance(a["org"], a["users"]["editor"], a["app"], {"name": "Production"})
    sandbox = services.create_instance(a["org"], a["users"]["editor"], a["app"], {"name": "Sandbox"})
    services.create_relationship(
        a["org"],
        a["users"]["editor"],
        {
            "type": "depends_on",
            "source": str(a["app"].id),
            "source_instance": str(prod.id),
            "target": str(a["app"].id),
            "target_instance": str(sandbox.id),
        },
    )
    client = api(a["users"]["editor"])
    assert retire(client, a["app"]).status_code == 200  # only its own instances depend on it
    depends(a, record(a, "Dependent app"), job)
    refused = retire(client, job, kind="components")
    assert refused.status_code == 409 and [d["name"] for d in refused.json_body["dependents"]] == ["Dependent app"]
    assert retire(client, job, kind="components", **ACK).status_code == 200


def test_rule_only_applies_when_lifecycle_becomes_retired(a, api):
    legacy, *_ = scenario(a)
    client = api(a["users"]["editor"])
    assert client.patch(f"applications/{legacy.id}", {"lifecycle": "deprecated"}).status_code == 200
    assert client.patch(f"applications/{legacy.id}", {"description": "Edited"}).status_code == 200
    assert retire(client, legacy, **ACK).status_code == 200
    assert (
        client.patch(f"applications/{legacy.id}", {"lifecycle": "retired", "description": "Again"}).status_code == 200
    )
    assert client.patch(f"applications/{legacy.id}", {"lifecycle": "active"}).status_code == 200  # un-retiring is free


def test_viewers_cannot_retire_and_other_organizations_dependents_never_appear(a, b, api):
    for ctx in (a, b):
        legacy, *_ = scenario(ctx)
        ctx["legacy"] = legacy
    assert retire(api(a["users"]["viewer"]), a["legacy"]).status_code == 403
    names = {d["name"] for d in retire(api(a["users"]["editor"]), a["legacy"]).json_body["dependents"]}
    assert names == {"Kiosk", "Badge job"}
    assert retire(api(a["users"]["editor"]), b["legacy"]).status_code == 404


# ---- the assessment rule -------------------------------------------------------------------------------------------------


def start(client, target, change_type="retirement", **extra):
    body = {"target": str(target.id), "change_type": change_type, "title": "Retire Legacy", **extra}
    return client.post("assessments", body)


def test_a_retirement_assessment_ready_for_external_execution_needs_the_acknowledgment(a, api):
    legacy, active, deprecated = scenario(a)
    client = api(a["users"]["editor"])
    made = start(client, legacy).json_body
    assert client.post(f"assessments/{made['id']}/status", {"status": "in_review"}).status_code == 200  # no rule yet
    refused = client.post(f"assessments/{made['id']}/status", {"status": "ready"})
    assert refused.status_code == 409 and refused.json_body["type"] == PROBLEM
    assert [d["name"] for d in refused.json_body["dependents"]] == ["Badge job", "Kiosk"]
    assert ChangeAssessment.objects.get().status == "in_review"
    no_reason = client.post(f"assessments/{made['id']}/status", {"status": "ready", "retirement_ack": True})
    assert no_reason.status_code == 400 and "retirement_ack_reason" in no_reason.json_body["errors"]
    done = client.post(f"assessments/{made['id']}/status", {"status": "ready", **ACK})
    assert done.status_code == 200
    assert (
        done.json_body["status"] == "ready" and done.json_body["retirement_ack_reason"] == ACK["retirement_ack_reason"]
    )
    event = AuditEvent.objects.get(
        action="assessment.status_changed", target_id=made["id"], changes__status=["in_review", "ready"]
    )
    assert event.changes["retirement_ack"]["reason"] == ACK["retirement_ack_reason"]
    assert len(event.changes["retirement_ack"]["dependents"]) == 2
    legacy.refresh_from_db()
    assert legacy.lifecycle == "active"  # the assessment never retires the record itself


def test_the_assessment_rule_is_only_for_retirements_becoming_ready_with_active_dependents(a, api):
    legacy, *_ = scenario(a)
    client = api(a["users"]["editor"])
    change = start(client, legacy, "change").json_body
    assert client.post(f"assessments/{change['id']}/status", {"status": "ready"}).status_code == 200
    lonely = record(a, "Lonely")
    quiet = start(client, lonely).json_body
    assert client.post(f"assessments/{quiet['id']}/status", {"status": "ready"}).status_code == 200
    assert ChangeAssessment.objects.get(id=quiet["id"]).retirement_ack_reason == ""
    for status in ("closed", "cancelled", "draft"):  # other statuses never need it
        made = start(client, legacy).json_body
        assert client.post(f"assessments/{made['id']}/status", {"status": status}).status_code in (200,)


def test_ready_again_and_an_already_retired_target_need_no_second_acknowledgment(a, api):
    legacy, *_ = scenario(a)
    client = api(a["users"]["editor"])
    made = start(client, legacy).json_body
    assert client.post(f"assessments/{made['id']}/status", {"status": "ready", **ACK}).status_code == 200
    assert client.post(f"assessments/{made['id']}/status", {"status": "ready"}).status_code == 200
    assert client.post(f"assessments/{made['id']}/status", {"status": "in_review"}).status_code == 200
    assert retire(client, legacy, **ACK).status_code == 200  # now the record is retired
    assert (
        client.post(f"assessments/{made['id']}/status", {"status": "ready"}).status_code == 200
    )  # nothing left to retire


def test_creating_a_retirement_assessment_as_ready_takes_the_same_rule(a, api):
    legacy, *_ = scenario(a)
    client = api(a["users"]["editor"])
    refused = start(client, legacy, status="ready")
    assert refused.status_code == 409 and refused.json_body["type"] == PROBLEM
    assert ChangeAssessment.objects.count() == 0
    made = start(client, legacy, status="ready", **ACK)
    assert made.status_code == 201 and made.json_body["retirement_ack_reason"] == ACK["retirement_ack_reason"]
    assert AuditEvent.objects.get(action="assessment.created").changes["retirement_ack"]["reason"]


def test_viewers_cannot_change_assessment_status_and_other_organizations_get_404(a, b, api):
    legacy, *_ = scenario(a)
    theirs = scenario(b)[0]
    made = start(api(a["users"]["editor"]), legacy).json_body
    their_made = start(api(b["users"]["editor"]), theirs).json_body
    assert api(a["users"]["viewer"]).post(f"assessments/{made['id']}/status", {"status": "ready"}).status_code == 403
    assert (
        api(a["users"]["editor"]).post(f"assessments/{their_made['id']}/status", {"status": "ready"}).status_code == 404
    )
