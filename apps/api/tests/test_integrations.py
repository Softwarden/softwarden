"""Integrations: the table, the service rules, the API, and how they stay out of the record queues."""

from catalog import services
from catalog.models import Integration, Resource
from core.models import AuditEvent


def record(ctx, name, kind="application"):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": kind, "lifecycle": "active"}
    )


def instance(ctx, application, name="Production"):
    return services.create_instance(ctx["org"], ctx["users"]["editor"], application, {"name": name})


def flow(ctx, name="Nightly feed", source=None, target=None, **extra):
    """An integration from source to target (two fresh records unless given), made through the service."""
    source = source or record(ctx, f"{name} source")
    target = target or record(ctx, f"{name} target", "component")
    return services.create_integration(
        ctx["org"],
        ctx["users"]["editor"],
        {"name": name, "lifecycle": "active", "source": str(source.id), "target": str(target.id), **extra},
    )


def body(source, target, **extra):
    return {"name": "Payroll feed", "lifecycle": "active", "source": str(source.id), "target": str(target.id), **extra}


def implement(ctx, component, integration):
    return services.create_relationship(
        ctx["org"],
        ctx["users"]["editor"],
        {"type": "implements", "source": str(component.id), "target": str(integration.id)},
    )


# ---- Create and edit ----------------------------------------------------------------------------------------


def test_create_edit_through_the_api_are_audited(a, api):
    client = api(a["users"]["editor"])
    app, job = a["app"], record(a, "Job", "component")
    prod, sandbox = instance(a, app), instance(a, app, "Sandbox")
    created = client.post(
        "integrations",
        body(
            app,
            job,
            source_instance=str(prod.id),
            data_purpose=" Payroll totals ",
            protocol="SFTP",
            schedule="Nightly 02:00",
            documentation_url="https://docs.example.test/payroll",
            technical_owner=str(a["people"]["dana"].id),
            support_team=str(a["team"].id),
            criticality="critical",  # integrations have none
            business_owner=str(a["people"]["priya"].id),  # nor a business owner
        ),
    )
    assert created.status_code == 201, created.json_body
    made = created.json_body
    assert made["kind"] == "integration" and made["slug"] == "payroll-feed" and made["data_purpose"] == "Payroll totals"
    assert made["source"]["instance"] == {"id": str(prod.id), "name": "Production"}
    assert made["target"]["instance"] is None and made["target"]["name"] == "Job"
    assert made["technical_owner"]["name"] == "Dana Ortiz" and made["support_team"]["name"] == "IT Applications"
    assert made["it_steward"]["id"] == a["users"]["editor"].id  # the creator, as for records
    assert made["vendor_native"] is True and made["implemented_by"] == [] and "criticality" not in made
    stored = Resource.objects.get(id=made["id"])
    assert stored.criticality == "" and stored.business_owner is None
    services.check_one_extension(stored)
    event = AuditEvent.objects.get(action="resource.created", target_id=made["id"])
    assert event.organization == a["org"] and event.actor == a["users"]["editor"]

    edited = client.patch(
        f"integrations/{made['id']}",
        {"protocol": "REST", "target": str(app.id), "target_instance": str(sandbox.id)},
    )
    assert edited.status_code == 200, edited.json_body
    assert edited.json_body["target"]["instance"]["id"] == str(sandbox.id)
    changes = AuditEvent.objects.get(action="resource.updated", target_id=made["id"]).changes
    assert changes == {
        "protocol": ["SFTP", "REST"],
        "target": [str(job.id), str(app.id)],
        "target_instance": [None, str(sandbox.id)],
    }
    assert client.get(f"integrations/{made['id']}").json_body["protocol"] == "REST"


def test_a_patch_without_the_ends_keeps_them(a, api):
    feed = flow(a)
    before = feed.integration
    response = api(a["users"]["editor"]).patch(f"integrations/{feed.id}", {"schedule": "Hourly", "name": "Hourly feed"})
    assert response.status_code == 200
    after = Integration.objects.get(pk=feed.pk)
    assert (after.source_resource_id, after.target_resource_id) == (
        before.source_resource_id,
        before.target_resource_id,
    )
    assert after.schedule == "Hourly"
    changes = AuditEvent.objects.get(action="resource.updated").changes
    assert changes == {"name": ["Nightly feed", "Hourly feed"], "schedule": ["", "Hourly"]}


def test_ends_must_be_applications_or_components(a, api):
    client = api(a["users"]["editor"])
    feed, job = flow(a), record(a, "Job", "component")
    for field in ("source", "target"):
        sent = {"source": str(a["app"].id), "target": str(job.id), field: str(feed.id)}
        response = client.post("integrations", {"name": "Chain", "lifecycle": "active", **sent})
        assert response.status_code == 400
        assert response.json_body["errors"][field] == [
            f"The {field} of an integration must be an application or a component."
        ]
    assert client.patch(f"integrations/{feed.id}", {"target": str(feed.id)}).status_code == 400
    assert Resource.objects.filter(kind="integration").count() == 1


def test_the_same_record_at_both_ends_needs_two_different_instances(a, api):
    client = api(a["users"]["editor"])
    app = a["app"]
    prod, sandbox = instance(a, app), instance(a, app, "Sandbox")
    for ends in (
        {},
        {"source_instance": str(prod.id)},
        {"source_instance": str(prod.id), "target_instance": str(prod.id)},
    ):
        response = client.post("integrations", body(app, app, **ends))
        assert response.status_code == 400
        assert response.json_body["errors"]["target"] == [
            "A record can only link to itself through two different instances."
        ]
    ok = client.post("integrations", body(app, app, source_instance=str(prod.id), target_instance=str(sandbox.id)))
    assert ok.status_code == 201
    # Editing into the same record without instances is refused too, and nothing is stored.
    feed = flow(a, "Other feed")
    refused = client.patch(f"integrations/{feed.id}", {"source": str(app.id), "target": str(app.id)})
    assert refused.status_code == 400 and Integration.objects.get(pk=feed.pk).source_resource_id != app.id


def test_an_instance_must_belong_to_its_stated_application(a, api):
    client = api(a["users"]["editor"])
    job = record(a, "Job", "component")
    other = record(a, "Other app")
    theirs = instance(a, other)
    refused = client.post("integrations", body(a["app"], job, source_instance=str(theirs.id)))
    assert refused.status_code == 400
    assert refused.json_body["errors"]["source_instance"] == ["This instance doesn't belong to orga App."]
    component_instance = client.post("integrations", body(a["app"], job, target_instance=str(theirs.id)))
    assert component_instance.status_code == 400 and "target_instance" in component_instance.json_body["errors"]
    # Changing the record without naming a new instance would leave the old instance behind.
    mine = instance(a, a["app"])
    feed = flow(a, "Own feed", source=a["app"], source_instance=str(mine.id))
    moved = client.patch(f"integrations/{feed.id}", {"source": str(other.id)})
    assert moved.status_code == 400 and "source_instance" in moved.json_body["errors"]


def test_validation_errors(a, api):
    client = api(a["users"]["editor"])
    job = record(a, "Job", "component")
    cases: list[tuple[dict, str | None]] = [
        ({"name": " "}, "name"),
        ({"lifecycle": "bogus"}, "lifecycle"),
        ({"documentation_url": "javascript:alert(1)"}, "documentation_url"),
        ({"documentation_url": "https://x.example.test/" + "a" * 200}, "documentation_url"),
        ({"protocol": "p" * 101}, "protocol"),
        ({"schedule": "s" * 201}, "schedule"),
        ({"data_purpose": "d" * 5001}, "data_purpose"),
        ({"protocol": 5}, "protocol"),  # not text is refused, never stored as empty
        ({"source": ""}, "source"),
        ({"target": "not-a-uuid"}, "target"),
        ({"technical_owner": str(a["people"]["gone"].id)}, "technical_owner"),
        ({"support_team": "nonsense"}, "support_team"),
    ]
    for extra, field in cases:
        response = client.post("integrations", {**body(a["app"], job), **extra})
        if field is None:
            assert response.status_code == 201
        else:
            assert response.status_code == 400 and field in response.json_body["errors"], (extra, response.json_body)
            assert response["Content-Type"] == "application/problem+json"
    assert Resource.objects.filter(kind="integration").count() == 0  # nothing was stored
    missing = client.post("integrations", {"name": "No ends", "lifecycle": "active"})
    assert set(missing.json_body["errors"]) == {"source", "target"}


def test_duplicate_names_ask_first(a, api):
    client = api(a["users"]["editor"])
    flow(a, "Payroll feed")
    again = client.post("integrations", body(a["app"], record(a, "Job", "component")))
    assert again.status_code == 409 and again.json_body["type"] == "/problems/duplicate-name"
    assert client.post("integrations", {**body(a["app"], a["app"]), "confirm_duplicate": True}).status_code == 400
    ok = client.post("integrations", {**body(a["app"], record(a, "Job 2", "component")), "confirm_duplicate": True})
    assert ok.status_code == 201


# ---- Derived Vendor-native, list and filters ----------------------------------------------------------------


def test_vendor_native_is_derived_from_implements_links(a, api):
    client = api(a["users"]["viewer"])
    feed = flow(a)
    assert client.get(f"integrations/{feed.id}").json_body["vendor_native"] is True
    job = record(a, "Sync job", "component")
    link = implement(a, job, feed)
    shown = client.get(f"integrations/{feed.id}").json_body
    assert shown["vendor_native"] is False
    assert shown["implemented_by"] == [{"id": str(job.id), "name": "Sync job", "archived": False}]
    services.archive_resource(a["org"], a["users"]["editor"], job)  # archiving keeps the link, so it still counts
    shown = client.get(f"integrations/{feed.id}").json_body
    assert shown["vendor_native"] is False and shown["implemented_by"][0]["archived"] is True
    link.delete()
    assert client.get(f"integrations/{feed.id}").json_body["vendor_native"] is True


def test_list_search_and_filters(a, api):
    client = api(a["users"]["viewer"])
    billing, crm = record(a, "Billing"), record(a, "CRM")
    sftp = flow(
        a,
        "Invoice export",
        source=billing,
        target=crm,
        protocol="SFTP",
        data_purpose="Invoices to the CRM",
        technical_owner=str(a["people"]["dana"].id),
    )
    rest = flow(a, "Customer sync", source=crm, target=billing, protocol="REST", lifecycle="deprecated")
    implement(a, record(a, "Export job", "component"), sftp)

    def names(query):
        response = client.get(f"integrations{query}")
        assert response.status_code == 200, response.json_body
        return [r["name"] for r in response.json_body["results"]]

    assert names("") == ["Customer sync", "Invoice export"]
    assert names(f"?source={billing.id}") == ["Invoice export"]
    assert names(f"?target={billing.id}") == ["Customer sync"]
    assert names("?protocol=sftp") == ["Invoice export"]
    assert names("?implemented=true") == ["Invoice export"]
    assert names("?implemented=false") == ["Customer sync"]
    assert names(f"?owner={a['people']['dana'].id}") == ["Invoice export"]
    assert names(f"?owner={a['people']['priya'].id}") == []
    assert names("?q=invoices") == ["Invoice export"]  # data purpose is searched
    assert names("?q=customer") == ["Customer sync"]
    assert names("?lifecycle=deprecated") == ["Customer sync"]
    assert names(f"?support_team={a['team'].id}") == []
    services.archive_resource(a["org"], a["users"]["editor"], rest)
    assert names("") == ["Invoice export"]
    assert names("?archived=true") == ["Customer sync"]
    assert names("?archived=all") == ["Customer sync", "Invoice export"]
    page = client.get("integrations?archived=all&page_size=1").json_body
    assert len(page["results"]) == 1 and page["next"]
    assert [
        r["name"]
        for r in client.get(f"integrations?archived=all&page_size=1&cursor={page['next']}").json_body["results"]
    ] == ["Invoice export"]


def test_list_rejects_filters_that_do_not_apply(a, api):
    client = api(a["users"]["viewer"])
    for query in (
        "criticality=high",
        "gap=true",
        "review_state=overdue",
        "vendor=x",
        "implemented=maybe",
        "source=nope",
    ):
        assert client.get(f"integrations?{query}").status_code == 400, query


# ---- Archive, restore, delete -------------------------------------------------------------------------------


def test_archive_restore_and_delete(a, api):
    editor, admin = api(a["users"]["editor"]), api(a["users"]["admin"])
    feed = flow(a)
    url = f"integrations/{feed.id}"
    archived = editor.post(f"{url}/archive")
    assert archived.status_code == 200 and archived.json_body["archived"] is True
    assert archived.json_body["archived_by"]["id"] == a["users"]["editor"].id
    assert editor.post(f"{url}/archive").status_code == 200  # idempotent
    assert AuditEvent.objects.filter(action="resource.archived", target_id=feed.id).count() == 1
    assert editor.post(f"{url}/restore").json_body["archived"] is False
    assert AuditEvent.objects.filter(action="resource.restored", target_id=feed.id).count() == 1
    assert editor.delete(url).status_code == 403  # hard delete is Admin only
    assert admin.delete(url).status_code == 204
    assert not Resource.objects.filter(id=feed.id).exists() and not Integration.objects.exists()
    deleted = AuditEvent.objects.get(action="resource.deleted")
    assert deleted.changes == {"kind": "integration", "name": "Nightly feed"}
    assert admin.get(url).status_code == 404


def test_deleting_an_implemented_integration_is_refused_until_the_link_is_removed(a, api):
    admin = api(a["users"]["admin"])
    feed = flow(a)
    link = implement(a, record(a, "Job", "component"), feed)
    refused = admin.delete(f"integrations/{feed.id}")
    assert refused.status_code == 409
    assert (
        refused.json_body["detail"]
        == "Nightly feed can't be deleted because it has 1 relationship. Archive it instead."
    )
    link.delete()
    assert admin.delete(f"integrations/{feed.id}").status_code == 204


def test_records_and_instances_used_by_an_integration_cannot_be_deleted(a, api):
    admin, editor = api(a["users"]["admin"]), api(a["users"]["editor"])
    prod, sandbox = instance(a, a["app"]), instance(a, a["app"], "Sandbox")
    feed = flow(a, source=a["app"], target=a["app"], source_instance=str(prod.id), target_instance=str(sandbox.id))
    refused = admin.delete(f"applications/{a['app'].id}")
    assert refused.status_code == 409
    assert refused.json_body["blocking_references"] == {"instance": 2, "integration": 1}  # one row, two ends
    assert refused.json_body["detail"].startswith(
        "orga App can't be deleted because it has 2 instances and 1 integration."
    )
    blocked = editor.delete(f"applications/{a['app'].id}/instances/{prod.id}")
    assert blocked.status_code == 409
    assert blocked.json_body["detail"] == (
        "Production can't be deleted because it has 1 integration. Remove or change it first."
    )
    other_feed = flow(a, "Second feed", source=a["app"], source_instance=str(prod.id))
    plural = editor.delete(f"applications/{a['app'].id}/instances/{prod.id}")
    assert plural.json_body["detail"] == (
        "Production can't be deleted because it has 2 integrations. Remove or change them first."
    )
    target = Resource.objects.get(id=other_feed.integration.target_resource_id)
    assert services.references_to(target) == {"integration": 1}
    for gone in (feed, other_feed):
        admin.delete(f"integrations/{gone.id}")
    assert editor.delete(f"applications/{a['app'].id}/instances/{prod.id}").status_code == 204


# ---- Integrations stay out of the record screens ------------------------------------------------------------


def test_integrations_are_not_in_the_record_lists_or_queues(a, api):
    feed = flow(a)
    admin = api(a["users"]["admin"])
    assert str(feed.id) not in {r["id"] for r in admin.get("applications").json_body["results"]}
    assert str(feed.id) not in {r["id"] for r in admin.get("components").json_body["results"]}
    attention = admin.get("needs-attention?scope=all").json_body
    assert str(feed.id) not in {row["id"] for row in attention["ownership_gaps"] + attention["reviews"]}
    for path in ("applications", "components"):
        assert admin.get(f"{path}/{feed.id}").status_code == 404
    # And the integration API serves nothing but integrations.
    assert admin.get(f"integrations/{a['app'].id}").status_code == 404
    assert all(r["kind"] == "integration" for r in admin.get("integrations").json_body["results"])


def test_an_integration_is_never_a_gap(a, api):
    feed = flow(a)
    assert "criticality" not in api(a["users"]["admin"]).get(f"integrations/{feed.id}").json_body
    assert Resource.objects.get(id=feed.id).criticality == ""


# ---- Other organizations ------------------------------------------------------------------------------------


def test_other_organizations_integration_is_404(a, b, api):
    theirs = flow(b)
    admin = api(a["users"]["admin"])
    url = f"integrations/{theirs.id}"
    assert admin.get(url).status_code == 404
    assert admin.patch(url, {"name": "Mine now"}).status_code == 404
    assert admin.post(f"{url}/archive").status_code == 404
    assert admin.post(f"{url}/restore").status_code == 404
    assert admin.delete(url).status_code == 404
    assert Resource.objects.get(id=theirs.id).name == "Nightly feed"


def test_lists_and_filters_stay_in_own_org(a, b, api):
    # The same names on both sides, each linked to its own neighbors, so a leak would show up as an extra row.
    mine, theirs = flow(a, "Payroll feed"), flow(b, "Payroll feed")
    implement(b, record(b, "Their job", "component"), theirs)
    client = api(a["users"]["viewer"])
    results = client.get("integrations?archived=all").json_body["results"]
    assert [r["id"] for r in results] == [str(mine.id)]
    assert results[0]["implemented_by"] == [] and results[0]["vendor_native"] is True
    assert client.get("integrations?implemented=true").json_body["results"] == []
    assert client.get(f"integrations?source={theirs.integration.source_resource_id}").status_code == 404
    assert client.get(f"integrations?target={theirs.integration.target_resource_id}").status_code == 404
    assert client.get(f"integrations?owner={b['people']['dana'].id}").json_body["results"] == []


def test_cannot_name_other_organizations_records_or_instances(a, b, api):
    client = api(a["users"]["editor"])
    job = record(a, "Job", "component")
    theirs = instance(b, b["app"])
    for extra, field in (
        ({"source": str(b["app"].id), "target": str(job.id)}, "source"),
        ({"target": str(b["app"].id)}, "target"),
        ({"source_instance": str(theirs.id)}, "source_instance"),
        ({"technical_owner": str(b["people"]["dana"].id)}, "technical_owner"),
        ({"support_team": str(b["team"].id)}, "support_team"),
    ):
        response = client.post("integrations", {**body(a["app"], job), **extra})
        assert response.status_code == 400 and field in response.json_body["errors"], extra
    assert not Integration.objects.exists()
    feed = flow(a)
    assert client.patch(f"integrations/{feed.id}", {"target": str(b["app"].id)}).status_code == 400
    # Another organization's integration can't be a target of an implements link either.
    their_feed = flow(b)
    link = client.post("relationships", {"type": "implements", "source": str(job.id), "target": str(their_feed.id)})
    assert link.status_code == 400
