"""List performance regressions (list performance).

The DRF layer made the renewals list about twice as slow (p95 ~900 ms against 500 ms) by building nested brief
serializers per row, and the integration and relationship lists the same way for link ends. These tests pin:

- each list's query count, which must not grow with the number of rows (same count for 1 row and for 20, every
  relation a row can show filled in, each row with its own people, departments, teams and vendors), and a ceiling;
- the direct rendering of the brief serializers and link ends, which must equal DRF's generic rendering;
- `represent()`, which must equal `serializer_class(obj).data`."""

import json
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework import serializers

from catalog import admin_services, ai, assessments, relationship_types, services
from catalog import tags as tags_service
from catalog.models import ApplicationInstance, Department, Integration, Person, SoftwareCategory, Team, Vendor
from catalog.serializers import (
    EndSerializer,
    PersonBriefSerializer,
    PersonSerializer,
    RecordBriefSerializer,
)
from core.drf import BriefSerializer, UserBriefSerializer, represent
from money import contracts, renewals

pytestmark = pytest.mark.django_db

# The queries one list request makes today, whatever its size: session, user and membership, then the list's own.
# Raise a ceiling only with a reason; a count that grows with the rows is an N+1 (the equality test catches it).
# Custom fields add one query to the record, integration and vendor lists: the organization's field
# definitions, read once per request for the filters and the rows (catalog/customfields.definitions).
# Data quality adds one more to the record lists: every row's data-quality results for the page
# (catalog/quality.evaluate).
# Capability overrides add one query to every list that checks a capability: the organization's live
# overrides, read once per request (core/capabilities.capabilities), so that a platform grant or revoke applies from
# the next request in every worker process.
# The shared rate limit adds one to every list: the API rate limit counts the request in the shared database counters (one upsert,
# core/counters.py), so it holds across every process.
CEILINGS = {
    "applications": 10,
    "components": 10,
    "integrations": 8,
    "renewals": 9,  # one more for the contract each renewal takes its dates from
    "people": 6,
    "relationships": 6,
    "assessments": 8,
    "teams": 7,  # the page, usage counts, members (one query for the page)
    # the page with account managers, usage by applications, by renewals and by contracts, custom fields
    "vendors": 9,
    "categories": 6,
    "ai-agents": 11,  # the record list's queries plus one for every agent's models (prefetch, providers joined)
    "ai-models": 7,  # the page with providers, usage by agents
    "ai-providers": 7,  # the page, usage by models
    "relationship-types": 6,  # the page, usage by relationships
    # the page (vendor, owner, both terms and the renewal joined), line counts, pool counts, the renewals' driving
    # contracts, custom fields
    "contracts": 10,
    "license-pools": 8,  # the page (application, contract joined), custom fields, licence observations (seat_proposals)
}


def add_rows(ctx, start, count):
    """`count` of every listed kind, each with every relation its list row shows, and its own related rows."""
    org, admin, editor = ctx["org"], ctx["users"]["admin"], ctx["users"]["editor"]
    today = services.org_today(org)
    for i in range(start, start + count):
        stewards = (admin, editor)
        department = Department.objects.create(organization=org, name=f"Department {i}")
        owner = Person.objects.create(
            organization=org, name=f"Owner {i}", email=f"owner{i}@orga.test", title="Lead", department=department
        )
        tech = Person.objects.create(organization=org, name=f"Tech {i}", department=department)
        team = admin_services.create_named(
            Team, org, editor, {"name": f"Team {i}", "members": [str(owner.id), str(tech.id)]}
        )
        vendor = admin_services.create_named(
            Vendor, org, editor, {"name": f"Vendor {i}", "account_manager": str(owner.id)}
        )
        category = SoftwareCategory.objects.create(organization=org, name=f"Category {i}")
        common = {
            "lifecycle": "active",
            "business_owner": str(owner.id),
            "technical_owner": str(tech.id),
            "support_team": str(team.id),
            "it_steward": stewards[i % 2].id,
            "department": str(department.id),
        }
        app = services.create_resource(
            org,
            editor,
            {
                "name": f"App {i}",
                "kind": "application",
                "vendor": str(vendor.id),
                "category": str(category.id),
                **common,
            },
        )
        comp = services.create_resource(org, editor, {"name": f"Comp {i}", "kind": "component", **common})
        provider = ai.create_provider(org, editor, {"name": f"Provider {i}"})
        models = [
            ai.create_model(org, editor, {"provider": str(provider.id), "name": f"Model {i}.{n}"}) for n in (1, 2)
        ]
        agent = services.create_resource(
            org,
            editor,
            {
                "name": f"Agent {i}",
                "kind": "ai_agent",
                "models_used": [str(m.id) for m in models],
                "channels": ["teams", "web"],
                **common,
            },
        )
        for record in (app, comp, agent):
            tags_service.set_tags(org, record, [f"tag-{i}", "shared"])
            services.record_review(
                org, editor, record, {"confirmed_by": str(owner.id), "evidence": "Email", "confirmation_date": None}
            )
        instance = services.create_instance(org, editor, app, {"name": f"Production {i}"})
        rel = services.create_relationship(
            org,
            editor,
            {
                "type": "depends_on",
                "source": str(app.id),
                "target": str(comp.id),
                "source_instance": str(instance.id),
                "purpose": "Reads",
            },
        )
        services.confirm_link(org, editor, rel, {"confirmed_by": str(owner.id), "evidence": "Call"})
        feed = services.create_integration(
            org,
            editor,
            {
                "name": f"Feed {i}",
                "lifecycle": "active",
                "source": str(app.id),
                "source_instance": str(instance.id),
                "target": str(comp.id),
                "technical_owner": str(tech.id),
                "support_team": str(team.id),
                "it_steward": stewards[i % 2].id,
            },
        )
        services.create_relationship(
            org, editor, {"type": "implements", "source": str(comp.id), "target": str(feed.id)}
        )
        added = relationship_types.create_type(org, admin, {"name": f"Runs on {i}", "is_dependency": True})
        services.create_relationship(org, editor, {"type": added.slug, "source": str(comp.id), "target": str(app.id)})
        services.confirm_link(
            org, editor, Integration.objects.get(resource=feed), {"confirmed_by": str(owner.id), "evidence": "Call"}
        )
        obligation = renewals.create_renewal(
            org,
            editor,
            {
                "name": f"Renewal {i}",
                "vendor": str(vendor.id),
                "responsible": str(owner.id),
                "followup": stewards[i % 2].id,
                "auto_renew": "yes",
                "renewal_date": (today + timedelta(days=30 + i)).isoformat(),
                "notice_value": 30,
                "notice_unit": "days",
                "term_value": 1,
                "term_unit": "years",
                "applications": [str(app.id)],
            },
        )
        first = renewals.latest_cycle(obligation)
        renewals.record_decision(org, editor, obligation, first.id, {"decision": "renew"})
        renewals.record_completion(
            org, editor, obligation, first.id, {"completed_on": today.isoformat(), "completion_evidence": f"PO {i}"}
        )
        renewals.establish_next_cycle(org, editor, obligation, {})
        # A contract with a vendor, an owner, lines, a linked renewal and a pool; a pool on the application.
        contract = contracts.create_contract(
            org,
            editor,
            {
                "name": f"Contract {i}",
                "vendor": str(vendor.id),
                "renewal_owner": str(owner.id),
                "amount": "1200",
                "end_date": (today + timedelta(days=200 + i)).isoformat(),
                "create_renewal_obligation": True,
            },
        )
        contracts.add_item(org, editor, contract, {"record": str(app.id), "allocation_percent": "50"})
        contracts.add_item(org, editor, contract, {"record": str(comp.id)})
        contracts.create_pool(
            org, editor, {"application": str(app.id), "contract": str(contract.id), "sku_name": f"SKU {i}"}
        )
        assessments.create_assessment(
            org, editor, {"target": str(app.id), "change_type": "change", "title": f"Move {i}", "owner": admin.id}
        )
    assert ApplicationInstance.objects.filter(organization=org).count() >= start + count


def list_request(client, path):
    with CaptureQueriesContext(connection) as queries:
        response = client.get(path)
    assert response.status_code == 200, response.json_body
    body = response.json_body
    rows = body["results"] if isinstance(body, dict) else body
    return len(queries), len(rows)


@pytest.mark.parametrize("path", sorted(CEILINGS))
def test_list_query_count_does_not_grow_with_rows(a, api, path):
    add_rows(a, 0, 1)
    client = api(a["users"]["viewer"])
    client.get(path)  # warm per-process caches (content types and the like) before counting
    one_queries, one_rows = list_request(client, path)
    add_rows(a, 1, 19)
    many_queries, many_rows = list_request(client, path)
    assert one_rows >= 1 and many_rows >= 20, (one_rows, many_rows)
    assert many_queries == one_queries, (
        f"{path}: {one_queries} queries for {one_rows} rows, {many_queries} for {many_rows}"
    )
    assert many_queries <= CEILINGS[path], f"{path}: {many_queries} queries, ceiling {CEILINGS[path]}"


# ---- the direct renderings equal DRF's generic one -----------------------------------------------------------------


def ordered(data):
    """Keys and values in order, so a rendering that reorders fields doesn't compare equal."""
    return json.dumps(data, default=str)


def generic(serializer_class, obj):
    """What DRF's own Serializer.to_representation makes of `obj` with this class's declared fields."""
    return serializers.Serializer.to_representation(serializer_class(), obj)


def test_brief_renderings_match_the_generic_path(a):
    org = a["org"]
    department = Department.objects.create(organization=org, name="Finance")
    with_department = Person.objects.create(
        organization=org, name="Ana", email="ana@orga.test", title="Controller", department=department
    )
    anonymized = Person.objects.create(organization=org, name="Former", active=False)
    anonymized.anonymized_at = anonymized.created_at
    for person in (with_department, anonymized, a["people"]["gone"]):
        assert ordered(PersonBriefSerializer(person).data) == ordered(generic(PersonBriefSerializer, person))
    for obj in (department, a["team"], Vendor.objects.create(organization=org, name="Contoso")):
        assert ordered(BriefSerializer(obj).data) == ordered(generic(BriefSerializer, obj))
    named = a["users"]["editor"]
    named.first_name, named.last_name = "Eddie", "Editor"
    for user in (named, a["users"]["viewer"]):  # with a full name, and without (email)
        assert ordered(UserBriefSerializer(user).data) == ordered(generic(UserBriefSerializer, user))
    assert UserBriefSerializer(a["users"]["viewer"]).data["name"] == a["users"]["viewer"].email


def test_subclasses_that_add_fields_still_render_them(a):
    person = a["people"]["dana"]
    assert set(PersonSerializer(person, context={"counts": {}}).data) >= set(PersonSerializer().fields)
    assert ordered(RecordBriefSerializer(a["app"]).data) == ordered(generic(RecordBriefSerializer, a["app"]))
    assert set(RecordBriefSerializer(a["app"]).data) == {"id", "name", "kind"}


def test_link_end_rendering_matches_the_generic_path(a):
    instance = services.create_instance(a["org"], a["users"]["editor"], a["app"], {"name": "Production"})
    for end in ((a["app"], None), (a["app"], instance)):
        assert ordered(EndSerializer(end).data) == ordered(generic(EndSerializer, end))


def test_represent_equals_data_and_keeps_none(a):
    user = a["users"]["admin"]
    assert ordered(represent(UserBriefSerializer, user)) == ordered(UserBriefSerializer(user).data)
    assert ordered(represent(RecordBriefSerializer, a["app"])) == ordered(RecordBriefSerializer(a["app"]).data)
    assert represent(BriefSerializer, None) is None


def serializers_built(monkeypatch, client, path):
    """How many serializer instances one request builds (a per-row `Serializer(obj).data` builds one per row)."""
    built = []
    original = serializers.BaseSerializer.__init__

    def counting(self, *args, **kwargs):
        built.append(type(self).__name__)
        original(self, *args, **kwargs)

    monkeypatch.setattr(serializers.BaseSerializer, "__init__", counting)
    try:
        assert client.get(path).status_code == 200
    finally:
        monkeypatch.setattr(serializers.BaseSerializer, "__init__", original)
    return built


@pytest.mark.parametrize("path", ["renewals", "integrations", "relationships", "assessments", "ai-agents"])
def test_rows_are_rendered_without_a_serializer_per_row(a, api, monkeypatch, path):
    """The renewals regression: three brief serializers built per renewal. Composite rows render through
    `represent()` and the direct brief renderings, so the serializers a request builds don't grow with its rows."""
    add_rows(a, 0, 1)
    client = api(a["users"]["viewer"])
    client.get(path)  # the shared instances are built once per process, on first use
    one = serializers_built(monkeypatch, client, path)
    add_rows(a, 1, 19)
    many = serializers_built(monkeypatch, client, path)
    assert len(many) == len(one), f"{path}: {len(one)} serializers for 1 row, {len(many)} for 20: {sorted(many)}"
