"""AuditEvent extensions: prechange / postchange snapshots in the identity format, the
related target of child rows, field-level diffs, and the API's filters and detail routes."""

import json
from datetime import timedelta

import pytest
from django.db import transaction

from api import tokens
from catalog import services, snapshots
from catalog.audit_log import TARGET_TYPES, snapshot_diff
from catalog.models import ApplicationInstance, LinkConfirmation, RecordReview, Resource, Tag, Team, Vendor
from core.models import AuditEvent, Membership
from core.request_context import unit_of_work
from discovery import engine as discovery
from discovery import entra
from tests.conftest import SNAPSHOT_SECRET_WORDS, named_like_a_secret, snapshot_problems


def latest(action):
    return AuditEvent.objects.filter(action=action).latest("created_at")


def rows_by_field(detail):
    return {row["field"]: row for row in detail["diff"]}


# ---- snapshots ----------------------------------------------------------------------------------------------------


def test_record_edit_keeps_before_and_after_in_the_identity_format(a, api):
    editor, admin = api(a["users"]["editor"]), api(a["users"]["admin"])
    app, dana, team = a["app"], a["people"]["dana"], a["team"]
    vendor = Vendor.objects.create(organization=a["org"], name="Contoso")
    editor.patch(f"applications/{app.id}", {"tags": ["Finance"]})
    body = {
        "name": "Payroll",
        "technical_owner": str(dana.id),
        "support_team": str(team.id),
        "vendor": str(vendor.id),
        "tags": ["Finance", "HR"],
    }
    assert editor.patch(f"applications/{app.id}", body).status_code == 200

    event = latest("resource.updated")
    pre, post = event.prechange, event.postchange
    assert pre["name"] == "orga App" and post["name"] == "Payroll"
    assert pre["technical_owner"] is None
    assert post["technical_owner"] == {"id": str(dana.id), "display": None}  # a Person is an ID only
    assert post["support_team"] == {"id": str(team.id), "display": "IT Applications"}
    assert post["vendor"] == {"id": str(vendor.id), "display": "Contoso"}
    assert post["it_steward"] == {"id": a["users"]["editor"].id, "display": None}  # a User too
    finance, hr = Tag.objects.get(name="Finance"), Tag.objects.get(name="HR")
    assert pre["tags"] == [{"id": str(finance.id), "display": "Finance"}]
    assert post["tags"] == sorted(
        [{"id": str(finance.id), "display": "Finance"}, {"id": str(hr.id), "display": "HR"}], key=lambda e: e["id"]
    )
    assert event.changes["name"] == ["orga App", "Payroll"]  # the payload the History tab reads is unchanged

    # Renamed afterwards: the snapshot still says what it was called; the diff names the Person as they are now.
    team.name, vendor.name = "Renamed team", "Renamed vendor"
    team.save()
    vendor.save()
    admin.post(f"people/{dana.id}/anonymize", {"confirm_name": "Dana Ortiz"})
    detail = admin.get(f"audit-events/{event.id}").json_body
    assert detail["diff_source"] == "snapshots" and detail["prechange"] == pre and detail["postchange"] == post
    diff = rows_by_field(detail)
    assert set(diff) == {"name", "technical_owner", "support_team", "vendor", "tags"}
    assert diff["name"] == {
        "field": "name",
        "label": "Name",
        "change": "changed",
        "before": "orga App",
        "after": "Payroll",
    }
    assert diff["support_team"]["after"]["display"] == "IT Applications"
    assert diff["vendor"]["after"]["display"] == "Contoso"
    assert diff["technical_owner"]["after"] == {"id": str(dana.id), "display": "Former person #1"}
    assert diff["tags"]["added"] == [{"id": str(hr.id), "display": "HR"}] and diff["tags"]["removed"] == []


def test_create_and_delete_snapshot_one_side_only(a, api):
    editor, admin = api(a["users"]["editor"]), api(a["users"]["admin"])
    made = editor.post(f"applications/{a['app'].id}/instances", {"name": "Sandbox", "environment": "test"}).json_body
    created = latest("instance.created")
    assert created.prechange is None
    assert created.postchange["name"] == "Sandbox"
    assert created.postchange["application"] == {"id": str(a["app"].id), "display": "orga App"}
    diff = admin.get(f"audit-events/{created.id}").json_body["diff"]
    assert {r["field"]: r["change"] for r in diff} == {
        "application": "added",
        "name": "added",
        "environment": "added",
        "status": "added",
    }  # a creation lists what it set; the empty URL is left out

    assert admin.delete(f"applications/{a['app'].id}/instances/{made['id']}").status_code == 204
    deleted = latest("instance.deleted")
    assert deleted.prechange["name"] == "Sandbox" and deleted.postchange is None
    diff = admin.get(f"audit-events/{deleted.id}").json_body["diff"]
    assert {r["change"] for r in diff} == {"removed"} and "name" in {r["field"] for r in diff}


def test_events_that_write_nothing_have_no_snapshots(a, api):
    api(a["users"]["admin"]).post(f"applications/{a['app'].id}/archive")
    archived = latest("resource.archived")
    assert archived.prechange["archived_at"] is None and archived.postchange["archived_at"] is not None
    client = api()
    client.post("auth/login", {"email": "editor@orga.test", "password": "correct-horse-battery"})
    login = latest("user.login")
    assert login.prechange is None and login.postchange is None
    detail = api(a["users"]["admin"]).get(f"audit-events/{login.id}").json_body
    assert detail["diff_source"] == "none" and detail["diff"] == []


def test_two_events_on_one_target_in_one_unit_chain_their_snapshots(a):
    org, editor = a["org"], a["users"]["editor"]
    with unit_of_work():
        record = services.create_resource(org, editor, {"name": "Chained", "lifecycle": "active"})
        services.update_resource(org, editor, record, {"lifecycle": "deprecated"})
    created, updated = latest("resource.created"), latest("resource.updated")
    assert created.prechange is None and created.postchange["lifecycle"] == "active"
    assert updated.prechange == created.postchange and updated.postchange["lifecycle"] == "deprecated"


def test_child_rows_name_their_parent_as_related_target(a, api):
    editor = api(a["users"]["editor"])
    app, dana = a["app"], a["people"]["dana"]
    instance = editor.post(f"applications/{app.id}/instances", {"name": "Production"}).json_body
    editor.patch(f"applications/{app.id}/instances/{instance['id']}", {"environment": "prod"})
    editor.post(f"applications/{app.id}/reviews", {"confirmed_by": str(dana.id), "evidence": "Email"})

    updated = latest("instance.updated")
    assert updated.target_type == "ApplicationInstance" and str(updated.target_id) == instance["id"]
    assert (updated.related_target_type, updated.related_target_id) == ("Resource", app.id)
    assert updated.prechange["environment"] == "" and updated.postchange["environment"] == "prod"
    review = latest("review.recorded")
    assert review.target_type == "RecordReview" and review.target_id == RecordReview.objects.get().id
    assert (review.related_target_type, review.related_target_id) == ("Resource", app.id)
    assert review.postchange["evidence"] == "Email"
    assert review.postchange["confirmed_by"] == {"id": str(dana.id), "display": None}

    # The application's History lists them, with the target named as before (the record).
    history = api(a["users"]["viewer"]).get(f"applications/{app.id}/audit").json_body["results"]
    assert [e["label"] for e in history] == ["Review recorded", "Instance changed", "Instance added"]
    assert all(e["target"] == {"kind": "application", "id": str(app.id), "name": "orga App"} for e in history)
    assert history[1]["related_target_type"] == "Resource" and history[1]["related_target_id"] == str(app.id)


def test_renewal_cycle_events_target_the_cycle(a, api):
    from tests.test_renewals import body

    client = api(a["users"]["editor"])
    r = client.post("renewals", body(a)).json_body
    client.patch(
        f"renewals/{r['id']}/cycles/{r['cycle']['id']}", {"explicit_deadline": "2026-12-15", "applications": []}
    )
    event = latest("renewal_cycle.updated")
    assert event.target_type == "RenewalCycle" and str(event.target_id) == r["cycle"]["id"]
    assert event.related_target_type == "RenewalObligation" and str(event.related_target_id) == r["id"]
    assert event.prechange["applications"] == [{"id": str(a["app"].id), "display": "orga App"}]
    assert event.postchange["applications"] == [] and event.postchange["explicit_deadline"] == "2026-12-15"
    history = client.get(f"renewals/{r['id']}/audit").json_body["results"]
    assert [e["label"] for e in history] == ["Cycle terms changed", "Renewal recorded"]
    assert history[0]["target"]["kind"] == "renewal" and history[0]["target"]["id"] == r["id"]
    one = client.get(f"renewals/{r['id']}/audit/{event.id}").json_body
    assert rows_by_field(one)["applications"]["removed"] == [{"id": str(a["app"].id), "display": "orga App"}]


def test_link_confirmation_targets_the_confirmation(a, api):
    from tests.test_audit_views import link_fixture

    portal, _, _, link = link_fixture(a)
    api(a["users"]["editor"]).post(
        f"relationships/{link.id}/confirmations", {"confirmed_by": str(a["people"]["dana"].id), "evidence": "Email"}
    )
    event = latest("link.confirmed")
    assert event.target_type == "LinkConfirmation" and event.target_id == LinkConfirmation.objects.get().id
    assert (event.related_target_type, event.related_target_id) == ("Relationship", link.id)
    assert event.postchange["relationship"] == {"id": str(link.id), "display": None}
    history = api(a["users"]["viewer"]).get(f"applications/{portal.id}/audit").json_body["results"]
    assert history[0]["label"] == "Link confirmed" and history[0]["target"]["kind"] == "relationship"


# ---- diffs --------------------------------------------------------------------------------------------------------


def test_snapshot_diff_rows():
    tag = {"id": "1", "display": "HR"}
    twin = {"id": "2", "display": "HR"}  # same name, different row: still a change
    assert snapshot_diff({"tags": [tag], "name": "A", "url": ""}, {"tags": [twin], "name": "A", "url": ""}) == [
        {
            "field": "tags",
            "label": "Tags",
            "change": "changed",
            "before": [tag],
            "after": [twin],
            "added": [twin],
            "removed": [tag],
        }
    ]
    assert snapshot_diff(None, {"name": "A", "url": ""}) == [
        {"field": "name", "label": "Name", "change": "added", "before": None, "after": "A"}
    ]
    assert snapshot_diff({"name": "A"}, None)[0]["change"] == "removed"
    assert snapshot_diff({"name": "A"}, {"name": "A", "kind": "application"})[0]["change"] == "added"
    assert snapshot_diff({"name": "A"}, {"name": "A"}) == []


def test_events_before_snapshots_diff_from_their_payload(a, api):
    dana = a["people"]["dana"]
    old = AuditEvent.objects.create(
        organization=a["org"],
        action="resource.updated",
        target_type="Resource",
        target_id=a["app"].id,
        changes={"technical_owner": [None, str(dana.id)], "lifecycle": ["active", "retired"], "undo": True},
    )
    detail = api(a["users"]["admin"]).get(f"audit-events/{old.id}").json_body
    assert detail["diff_source"] == "changes" and detail["prechange"] is None
    diff = rows_by_field(detail)
    assert set(diff) == {"technical_owner", "lifecycle"}
    assert diff["technical_owner"]["after"] == {"id": str(dana.id), "display": "Dana Ortiz"}
    assert diff["lifecycle"]["before"] == "active" and diff["lifecycle"]["after"] == "retired"


# ---- API: filters, fields, routes ---------------------------------------------------------------------------------


def test_log_filters_by_target_related_target_type_and_request(a, b, api):
    editor, admin = api(a["users"]["editor"]), api(a["users"]["admin"])
    app = a["app"]
    response = editor.post(f"applications/{app.id}/instances", {"name": "Production"})
    request_id = response["X-Request-ID"]
    instance = ApplicationInstance.objects.get(name="Production")
    editor.patch(f"applications/{app.id}", {"criticality": "high"})

    def actions(query):
        got = admin.get(f"audit-events?{query}")
        assert got.status_code == 200, got.json_body
        return [e["action"] for e in got.json_body["results"]]

    assert actions(f"target={app.id}") == ["resource.updated"]
    assert actions(f"target={instance.id}") == ["instance.created"]
    assert actions(f"related_target={app.id}") == ["instance.created"]
    assert actions(f"request_id={request_id}") == ["instance.created"]
    assert actions("target_type=ApplicationInstance") == ["instance.created"]
    assert actions(f"target_type=Resource&target={app.id}&action=resource.updated") == ["resource.updated"]
    page = admin.get("audit-events").json_body
    assert {"value": "RenewalCycle", "label": "Renewal cycle"} in page["target_types"]
    row = next(e for e in page["results"] if e["action"] == "instance.created")
    assert row["request_id"] == request_id and row["target_type"] == "ApplicationInstance"
    assert row["target_id"] == str(instance.id) and row["related_target_id"] == str(app.id)
    assert "prechange" not in row and "diff" not in row  # the list stays light; one event carries them
    # Bad values are a 400 naming the parameter; another organization's IDs simply match nothing.
    for bad in ("target=x", "related_target=1", "request_id=nope", "target_type=Secret"):
        refused = admin.get(f"audit-events?{bad}")
        assert refused.status_code == 400 and bad.split("=")[0] in refused.json_body["errors"]
    assert actions(f"target={b['app'].id}") == []


def test_record_history_retrieve_is_scoped_to_that_record(a, b, api):
    editor, viewer = api(a["users"]["editor"]), api(a["users"]["viewer"])
    app = a["app"]
    editor.patch(f"applications/{app.id}", {"criticality": "high"})
    mine = latest("resource.updated")
    other = services.create_resource(a["org"], a["users"]["editor"], {"name": "Other", "lifecycle": "active"})
    theirs = AuditEvent.objects.get(action="resource.created", target_id=other.id)

    detail = viewer.get(f"applications/{app.id}/audit/{mine.id}")  # any role
    assert detail.status_code == 200
    assert rows_by_field(detail.json_body)["criticality"]["after"] == "high"
    assert detail.json_body["label"] == "Fields changed" and detail.json_body["summary"].startswith("Criticality")
    # Another record's event, through this record's route, doesn't exist; nor does another organization's.
    assert viewer.get(f"applications/{app.id}/audit/{theirs.id}").status_code == 404
    services.update_resource(b["org"], b["users"]["editor"], b["app"], {"criticality": "low"})
    b_event = AuditEvent.objects.filter(organization=b["org"], action="resource.updated").get()
    assert viewer.get(f"applications/{app.id}/audit/{b_event.id}").status_code == 404
    assert api(a["users"]["admin"]).get(f"audit-events/{b_event.id}").status_code == 404
    assert editor.get(f"audit-events/{mine.id}").status_code == 403  # the log stays Admin only


# ---- Secrets never enter a snapshot --------------------------------------------------------------------------------

SECRET = "s3cret-client-value-0123456789"


def test_secrets_never_reach_a_snapshot(a, api):
    org, admin_user = a["org"], a["users"]["admin"]
    connector = discovery.create_connector(
        org, admin_user, {"name": "Entra", "tenant_id": "simulated", "secret": SECRET}
    )
    discovery.update_connector(org, admin_user, connector, {"secret": SECRET + "-2", "name": "Entra 2"})
    owner = Membership.objects.get(organization=org, user=a["users"]["editor"])
    token, raw = tokens.create_token(
        org, admin_user, owner, "Script", True, services.org_today(org) + timedelta(days=9)
    )
    tokens.revoke_token(org, admin_user, token)
    admin = api(admin_user)
    admin.patch("organization", {"review_interval_days": 120})
    api(a["users"]["editor"]).post(
        "auth/password", {"current_password": "correct-horse-battery", "new_password": "a-much-longer-passphrase"}
    )

    events = list(AuditEvent.objects.filter(organization=org))
    by_action = {e.action: e for e in events}
    post = {action: e.postchange or {} for action, e in by_action.items()}
    assert post["connector.updated"]["name"] == "Entra 2"
    # The secret lives on the connector's connection; its events carry the settings, never the slot.
    assert post["microsoft_connection.updated"]["tenant_id"] == "simulated"
    assert by_action["microsoft_connection.updated"].changes["replaced"] == ["secret"]
    ciphertext = connector.microsoft_connection.secret_ciphertext
    assert post["token.created"]["prefix"] == token.prefix
    assert post["organization.updated"]["review_interval_days"] == 120
    for event in events:
        assert snapshot_problems(event) == []
        stored = json.dumps([event.prechange, event.postchange])
        for value in (SECRET, SECRET + "-2", raw, token.token_hash, ciphertext, "correct-horse-battery"):
            assert value not in stored, event.action
    for action in ("connector.created", "microsoft_connection.created", "token.created", "organization.updated"):
        assert post[action] and not any(named_like_a_secret(k, SNAPSHOT_SECRET_WORDS) for k in post[action])


def test_snapshot_whitelists_hold_no_secret_or_personal_fields():
    from catalog.models import Person

    for model, spec in snapshots.specs().items():
        assert not [f for f in spec.fields if named_like_a_secret(f, SNAPSHOT_SECRET_WORDS)], model
    assert not {"name", "email", "title"} & set(snapshots.specs()[Person].fields)


UNDECLARED = {"smtp": {"pass": "x-undeclared"}, "bot_url": "https://hooks.example.test/x-undeclared"}


def test_undeclared_json_keys_never_reach_a_snapshot(a):
    """Open-shaped JSON columns are copied by declared keys only, so a key the denylist wouldn't catch (pass, bot_url)
    still stays out of the permanent log."""
    org, admin = a["org"], a["users"]["admin"]
    connector = discovery.create_connector(org, admin, {"name": "Entra", "tenant_id": "simulated", "secret": SECRET})
    with unit_of_work():
        org.settings = dict(UNDECLARED)
        org.save()
        services.audit(org, None, "organization.updated", org, {})
        connector.config = {**connector.config, **UNDECLARED}
        connector.scope = {**connector.scope, **UNDECLARED, "exclude_disabled": False}
        connector.save()
        services.audit(org, admin, "connector.updated", connector, {})
    settings_event, connector_event = latest("organization.updated"), latest("connector.updated")
    assert settings_event.postchange["settings"] == {}  # only snapshots.ORG_SETTINGS_AUDITED keys
    post = connector_event.postchange
    assert post["config"] == {}  # the source declares no config keys; credentials are the connection's
    assert post["microsoft_connection"]["id"] == str(connector.microsoft_connection_id)
    assert set(post["scope"]) <= set(entra.SCOPE_DEFAULTS) and post["scope"]["exclude_disabled"] is False
    for event in (settings_event, connector_event):
        stored = json.dumps([event.prechange, event.postchange])
        assert "x-undeclared" not in stored and "bot_url" not in stored and "smtp" not in stored
        assert snapshot_problems(event) == []


def test_a_rolled_back_event_leaves_no_stale_before(a):
    """The store's "before" is dropped when an event takes it: if that event's savepoint rolls back, the next write
    reads "before" from the database (the rolled-back value never shows as the previous state)."""
    org, editor = a["org"], a["users"]["editor"]
    with unit_of_work():
        try:
            with transaction.atomic():
                services.update_resource(org, editor, a["app"], {"lifecycle": "deprecated"})
                raise RuntimeError("roll back")
        except RuntimeError:
            pass
        app = Resource.objects.select_related("application").get(id=a["app"].id)
        assert app.lifecycle == "active"
        services.update_resource(org, editor, app, {"lifecycle": "evaluating"})
    event = AuditEvent.objects.get(action="resource.updated", target_id=app.id)
    pre, post = event.prechange or {}, event.postchange or {}
    assert pre["lifecycle"] == "active" and post["lifecycle"] == "evaluating"


def test_every_target_type_written_is_filterable():
    assert set(TARGET_TYPES) >= {m.__name__ for m in snapshots.specs()} | {"Resource"}


@pytest.mark.parametrize("model", [Team, Vendor])
def test_reference_data_snapshots(a, api, model):
    admin = api(a["users"]["admin"])
    path = {Team: "teams", Vendor: "vendors"}[model]
    made = admin.post(path, {"name": "Original"}).json_body
    admin.patch(f"{path}/{made['id']}", {"name": "Renamed"})
    event = latest(f"{model.__name__.lower()}.updated")
    assert event.prechange["name"] == "Original" and event.postchange["name"] == "Renamed"
