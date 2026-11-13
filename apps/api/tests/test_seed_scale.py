import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from catalog.models import (
    AgentModel,
    AIAgent,
    AIModel,
    Application,
    ApplicationInstance,
    Component,
    Integration,
    LinkConfirmation,
    Person,
    RecordReview,
    Relationship,
    Resource,
)
from catalog.services import org_today
from catalog.state import review_state
from core.models import AuditEvent, Membership, Organization
from discovery.engine import status_q
from discovery.models import Connector, Observation

pytestmark = pytest.mark.django_db
OLD_KINDS = ["application", "component"]  # the 1,000 resources at scale 1.0; AI agents come on top


def seed(**kw):
    call_command("seed_scale", scale=0.05, stdout=io.StringIO(), **kw)
    return Organization.objects.get(name="Scale Test (synthetic)")


def test_counts_proportions_and_extensions():
    org = seed()
    resources = Resource.objects.filter(organization=org, kind__in=OLD_KINDS)
    assert resources.count() == 50
    assert resources.filter(kind="application").count() == 30
    assert resources.filter(kind="component").count() == 20
    assert ApplicationInstance.objects.filter(organization=org).count() == 100
    assert RecordReview.objects.filter(organization=org).count() == 250
    assert Membership.objects.filter(organization=org).count() == 3
    assert Person.objects.filter(organization=org).count() == 3
    # exactly one extension per resource, matching its kind
    assert Application.objects.count() == 30 and Component.objects.count() == 20
    for r in resources.select_related("application", "component"):
        assert (r.extension is not None) and (hasattr(r, "component") != hasattr(r, "application"))
    # variety for Needs attention: archived, ownership gaps, and several review states
    assert resources.filter(archived_at__isnull=False).count() == 4
    assert resources.filter(business_owner__isnull=True).exists()
    today = org_today(org)
    latest: dict = {}
    for review in RecordReview.objects.filter(organization=org):
        if review.resource_id not in latest or review.confirmation_date > latest[review.resource_id].confirmation_date:
            latest[review.resource_id] = review
    states = {
        review_state(r, latest.get(r.id), today)[0]
        for r in resources.select_related("business_owner", "technical_owner")
    }
    assert {"unreviewed", "overdue", "needs_confirmation", "confirmed"} <= states


def test_second_run_refuses_without_reset_and_reset_rebuilds():
    org = seed()
    with pytest.raises(CommandError, match="--reset"):
        seed()
    assert Resource.objects.filter(organization=org, kind__in=OLD_KINDS).count() == 50
    seed(reset=True)
    assert Resource.objects.filter(organization=org, kind__in=OLD_KINDS).count() == 50
    assert RecordReview.objects.filter(organization=org).count() == 250
    assert Relationship.objects.filter(organization=org).count() == 100
    assert Integration.objects.filter(organization=org).count() == 15
    assert (
        AIAgent.objects.filter(organization=org).count() == 3 and AIModel.objects.filter(organization=org).count() == 12
    )
    # The audit log is rebuilt too: the old synthetic events are purged (append-only's one removal path), not added to.
    events = AuditEvent.objects.filter(organization=org)
    assert events.filter(action="user.login").exists() and events.count() < 2500 + 100


def test_audit_events_at_scale_shape():
    org = seed()
    events = AuditEvent.objects.filter(organization=org)
    assert events.filter(request_id__isnull=False).count() >= 2500  # 50,000 at full scale, plus the seed's own
    assert events.filter(action="resource.updated", prechange__isnull=False).exists()
    assert events.filter(action="instance.updated", related_target_type="Resource").exists()
    assert events.dates("created_at", "month").count() > 12  # spread over two years, not stamped "now"


def test_links_integrations_and_confirmations_at_scale_shape():
    org = seed()
    assert Integration.objects.filter(organization=org).count() == 15  # 300 * 0.05
    assert Resource.objects.filter(organization=org, kind="integration").count() == 15
    assert Relationship.objects.filter(organization=org).count() == 100  # 2,000 * 0.05
    types = set(Relationship.objects.filter(organization=org).values_list("type__slug", flat=True))
    assert types == {"depends_on", "implements"}
    # Every integration has exactly its own extension row; none is a gap or carries criticality.
    for r in Resource.objects.filter(organization=org, kind="integration").select_related("integration"):
        assert r.integration.source_resource_id != r.integration.target_resource_id and r.criticality == ""
    # A mix of states for the explorer: confirmed (current), overdue and changed, plus never confirmed.
    assert LinkConfirmation.objects.filter(organization=org).exists()
    states = {state_of(org, r) for r in Relationship.objects.filter(organization=org).select_related("source")}
    assert {"unconfirmed", "confirmed", "needs_verification"} <= states


def test_discovery_connector_yields_scaled_observations_and_reset_rebuilds_it():
    org = seed()
    connector = Connector.objects.get(organization=org)
    assert connector.runs.get().status == "succeeded"
    # 2,000 * 0.05 generated apps plus the simulated tenant's own; all are candidates
    total = Observation.objects.filter(connector=connector).count()
    assert 90 <= total < 130  # some generated apps fall outside the default scope
    assert Observation.objects.filter(connector=connector).filter(status_q("candidates")).count() == total
    seed(reset=True)
    assert Connector.objects.filter(organization=org).count() == 1
    assert Observation.objects.filter(organization=org).count() == total


def state_of(org, rel):
    from catalog.links import latest_confirmations, relationship_state_fields

    latest = latest_confirmations(org, relationship_ids=[rel.id]).get(("relationship", rel.id))
    return relationship_state_fields(org, rel, latest)["link_state"]


def test_ai_agents_at_scale():
    """Agents on top of the scaled resources, each with an extension row and one to three models, and gaps for
    the AI agent gaps queue at full scale."""
    org = seed()
    agents = Resource.objects.filter(organization=org, kind="ai_agent")
    assert agents.count() == 3 == AIAgent.objects.filter(organization=org).count()
    for agent in agents:
        assert 1 <= AgentModel.objects.filter(agent_id=agent.id).count() <= 3
        assert agent.ai_agent.channels and agent.ai_agent.platform
    assert AIModel.objects.filter(organization=org).count() == 12


def test_seeding_refreshes_the_planner_statistics():
    """QA (second perf fix): seed_scale ANALYZEs what it bulk-inserted, so a timing run started right after it
    isn't planned from statistics that predate the organization (557 ms instead of 121 for the dashboard)."""
    from django.db import connection

    org = seed()
    with connection.cursor() as cursor:
        # to_regclass resolves the name through the search path, so a same-named table in another schema can't answer.
        cursor.execute("SELECT reltuples FROM pg_class WHERE oid = to_regclass(%s)", [Resource._meta.db_table])
        (estimated,) = cursor.fetchone()
    assert estimated >= Resource.objects.filter(organization=org).count()
