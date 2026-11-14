"""Per-organization limits (core/limits.py, the create services that enforce them, core/throttling
.OrgRateThrottle, the platform's limit routes and `GET organization/limits`)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from catalog import invitations, platform_admin, services, usage
from catalog.models import Attachment, Kind, Person, Resource
from core import capabilities, limits
from core.models import (
    AuditEvent,
    Edition,
    Invitation,
    Membership,
    Organization,
    OrganizationLimitOverride,
    PendingAccess,
    PlatformAuditEvent,
)
from core.problems import LimitReached
from discovery import engine as discovery
from discovery.models import Connector
from imports import csvio
from tests.test_concurrency import run_together

LIMIT_TYPE = "/problems/limit-reached"
PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def empty_cache():
    cache.clear()  # the throttles count in the process cache
    yield
    cache.clear()


@pytest.fixture
def staff(db):
    from tests.test_platform import make_staff

    return make_staff()


def set_limit(org, name, value, staff=None, **extra):
    if staff is None:
        from tests.test_platform import make_staff

        staff = get_user_model().objects.filter(platform_staff__isnull=False).first() or make_staff()
    return platform_admin.set_limit(staff, org, {"limit": name, "value": value, "reason": "Test", **extra})


def cloud(org):
    org, _ = capabilities.set_edition(org, "cloud")
    return org


def assert_limit_reached(response, name, maximum, usage):
    assert response.status_code == 409, (response.status_code, getattr(response, "json_body", None))
    assert response["Content-Type"] == "application/problem+json"
    body = response.json() if not hasattr(response, "json_body") or response.json_body is None else response.json_body
    assert body["type"] == LIMIT_TYPE and body["title"] == "Limit reached" and body["status"] == 409
    assert (body["limit"], body["maximum"], body["usage"]) == (name, maximum, usage)
    return body


def records(org) -> int:
    return Resource.objects.filter(organization=org).count()


# ---- the defaults and the overrides -----------------------------------------------------------------------------------


def test_defaults_per_edition(a):
    org = a["org"]
    assert limits.limits(org) == dict.fromkeys(limits.LIMITS)  # community: unlimited
    org = capabilities.set_edition(org, "enterprise")[0]
    assert limits.limits(org) == dict.fromkeys(limits.LIMITS)
    org = cloud(org)
    assert limits.limits(org) == {
        "records": 5000,
        "other_records": 50000,
        "connectors": 10,
        "members": 50,
        "attachment_bytes": 5 * 1024**3,
        "api_rate_read": 1200,
        "api_rate_write": 300,
        "invitation_emails_hour": 20,  # the send budget (lower until a second member, limits.effective)
        "invitation_emails_day": 100,
    }
    assert set(limits.EDITION_LIMITS) == set(Edition.values)
    org.edition = "platinum"  # not reachable through the service: fails to community's, as capabilities does
    assert limits.edition_limits(org) == dict.fromkeys(limits.LIMITS)


def test_an_override_is_set_audited_in_both_streams_and_applies_from_the_next_request(a, staff, api):
    org = cloud(a["org"])
    response = api(staff).post(
        f"platform/organizations/{org.id}/limits", {"limit": "records", "value": 20000, "reason": "Ticket 7"}
    )
    assert response.status_code == 201, response.json_body
    assert response.json_body["limit"] == "records" and response.json_body["value"] == 20000
    assert response.json_body["live"] is True
    assert limits.limits(Organization.objects.get(pk=org.pk))["records"] == 20000
    event = AuditEvent.objects.get(organization=org, action="organization.limit_set")
    assert event.changes["limit"] == "records" and event.changes["value"] == [5000, 20000]
    assert event.changes["via"] == "platform" and event.changes["reason"] == "Ticket 7"
    assert event.actor == staff
    platform = PlatformAuditEvent.objects.get(action="limit.set")
    assert platform.organization_id == org.pk and platform.details["reason"] == "Ticket 7"
    # Unlimited is null, and replaces the earlier override.
    set_limit(org, "records", None, staff)
    assert OrganizationLimitOverride.objects.get(organization=org, limit="records").value is None
    assert limits.limits(Organization.objects.get(pk=org.pk))["records"] is None
    shown = api(staff).get(f"platform/organizations/{org.id}/limits").json_body
    row = next(r for r in shown["limits"] if r["name"] == "records")
    assert row["limit"] is None and row["default"] == 5000 and row["overridden"] is True and row["usage"] == 1
    assert [o["limit"] for o in shown["overrides"]] == ["records"]


def test_an_override_expires(a, staff):
    org = cloud(a["org"])
    later = (timezone.now() + timedelta(hours=1)).isoformat()
    set_limit(org, "connectors", 50, staff, expires_at=later)
    assert limits.limits(org)["connectors"] == 50
    OrganizationLimitOverride.objects.update(expires_at=timezone.now() - timedelta(seconds=1))  # time passes
    from core.request_context import forget_organization

    forget_organization(org.pk)
    assert limits.limits(org)["connectors"] == 10


def test_removing_an_override_restores_the_default_and_is_audited(a, staff, api):
    org = cloud(a["org"])
    set_limit(org, "members", 5, staff)
    response = api(staff).post(f"platform/organizations/{org.id}/limits/members/remove", {"reason": "Upgrade done"})
    assert response.status_code == 204
    assert not OrganizationLimitOverride.objects.exists()
    assert limits.limits(Organization.objects.get(pk=org.pk))["members"] == 50
    event = AuditEvent.objects.get(organization=org, action="organization.limit_override_removed")
    assert event.changes["value"] == [5, 50] and event.changes["reason"] == "Upgrade done"
    assert PlatformAuditEvent.objects.filter(action="limit.override_removed", organization_id=org.pk).exists()
    again = api(staff).post(f"platform/organizations/{org.id}/limits/members/remove", {"reason": "Again"})
    assert again.status_code == 404
    unknown = api(staff).post(f"platform/organizations/{org.id}/limits/seats/remove", {"reason": "x"})
    assert unknown.status_code == 404


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"limit": "seats", "value": 1, "reason": "x"}, "limit"),
        ({"limit": "records", "value": -1, "reason": "x"}, "value"),
        ({"limit": "records", "value": True, "reason": "x"}, "value"),
        ({"limit": "records", "value": "10", "reason": "x"}, "value"),
        ({"limit": "records", "reason": "x"}, "value"),
        ({"limit": "records", "value": 1}, "reason"),
        ({"limit": "records", "value": 1, "reason": "x", "expires_at": "2000-01-01T00:00:00Z"}, "expires_at"),
    ],
)
def test_override_input_is_checked(a, staff, api, body, field):
    response = api(staff).post(f"platform/organizations/{a['org'].id}/limits", body)
    assert response.status_code == 400 and field in response.json_body["errors"], response.json_body
    assert not OrganizationLimitOverride.objects.exists()


def test_an_override_is_one_organizations_only(a, b, staff):
    set_limit(a["org"], "records", 0, staff)
    assert limits.limits(a["org"])["records"] == 0
    assert limits.limits(b["org"])["records"] is None
    services.create_resource(b["org"], b["users"]["editor"], {"name": "Theirs", "lifecycle": "active"})
    with pytest.raises(LimitReached):
        services.create_resource(a["org"], a["users"]["editor"], {"name": "Ours", "lifecycle": "active"})


def test_the_limit_routes_are_platform_staff_only(a, api):
    for user in (a["users"]["admin"], None):
        assert api(user).get(f"platform/organizations/{a['org'].id}/limits").status_code == 403
        body = {"limit": "records", "value": 1, "reason": "x"}
        assert api(user).post(f"platform/organizations/{a['org'].id}/limits", body).status_code == 403
    assert not OrganizationLimitOverride.objects.exists()


# ---- exposure ---------------------------------------------------------------------------------------------------------


def test_every_member_reads_the_limits_and_usage(a, b, api, staff):
    org = cloud(a["org"])
    set_limit(org, "connectors", 3, staff)
    services.create_resource(b["org"], b["users"]["editor"], {"name": "Not ours", "lifecycle": "active"})
    for role in ("admin", "editor", "viewer"):
        response = api(a["users"][role]).get("organization/limits")
        assert response.status_code == 200
        rows = {r["name"]: r for r in response.json_body["limits"]}
        assert list(rows) == list(limits.LIMITS)
        assert rows["records"] == {
            "name": "records",
            "label": limits.LIMITS["records"][0],
            "unit": "records",
            "limit": 5000,
            "default": 5000,
            "overridden": False,
            "usage": 1,
        }
        assert rows["members"]["usage"] == 3 and rows["connectors"]["limit"] == 3 and rows["connectors"]["overridden"]
        assert rows["api_rate_read"]["usage"] is None and rows["api_rate_read"]["limit"] == 1200
    assert api().get("organization/limits").status_code in (401, 403)
    community = api(b["users"]["viewer"]).get("organization/limits").json_body["limits"]
    assert all(r["limit"] is None for r in community)


# ---- other records: the catch-all for secondary tables ----------------------------------------


def other_rows(org) -> int:
    return usage.other_record_total(org.pk)


def test_a_cloud_organization_at_its_cap_gets_409_on_post_people(a, b, api):
    org = cloud(a["org"])
    used = other_rows(org)  # the fixture's people and team, the built-in relationship types and notification rules
    assert used >= 4
    set_limit(org, "other_records", used + 1)
    editor = api(a["users"]["editor"])
    assert editor.post("people", {"name": "Last place"}).status_code == 201
    body = assert_limit_reached(editor.post("people", {"name": "One too many"}), "other_records", used + 1, used + 1)
    assert "counted together" in body["detail"]
    assert not Person.objects.filter(organization=org, name="One too many").exists()
    # The same cap refuses the other tables it counts, and only this organization's.
    assert_limit_reached(editor.post("vendors", {"name": "Acme"}), "other_records", used + 1, used + 1)
    assert_limit_reached(editor.post("teams", {"name": "Ops"}), "other_records", used + 1, used + 1)
    assert api(b["users"]["editor"]).post("people", {"name": "Elsewhere"}).status_code == 201
    # Updates still work over it.
    person = Person.objects.get(organization=org, name="Last place")
    assert editor.patch(f"people/{person.id}", {"name": "Renamed"}).status_code == 200


def test_every_counted_table_is_a_tenant_table_and_counted(a):
    from django.apps import apps

    for label in usage.OTHER_RECORD_MODELS:
        model = apps.get_model(label)
        assert model._meta.get_field("organization").related_model is Organization, label
    before = other_rows(a["org"])
    from catalog.models import Vendor

    Vendor.objects.create(organization=a["org"], name="Counted")
    assert other_rows(a["org"]) == before + 1


def test_the_create_services_of_the_counted_tables_check_the_cap(a):
    """Each service that creates a row of a counted table refuses at the cap (one line each, so a new one is noticed)."""
    from api import tokens
    from catalog import admin_services, ai, customfields, relationship_types, saved_filters
    from catalog.models import Department, SoftwareCategory, Team, Vendor
    from imports import importer
    from money import contracts, renewals
    from notify import channel_services, rule_services
    from notify.models import TeamsDestination, WebhookDestination

    org = cloud(a["org"])
    admin, editor = a["users"]["admin"], a["users"]["editor"]
    # Made before the cap: what the AI model, the contract lines and the relationship below hang on.
    other = services.create_resource(org, editor, {"name": "Other", "kind": "component", "lifecycle": "active"})
    provider = ai.create_provider(org, editor, {"name": "Existing provider"})
    contract = contracts.create_contract(org, editor, {"name": "Existing", "contract_number": "E-1"})
    set_limit(org, "other_records", other_rows(org))
    membership = Membership.objects.get(organization=org, user=admin)
    attempts = {
        "AI model": lambda: ai.create_model(org, editor, {"provider": str(provider.id), "name": "Model"}),
        "contract line": lambda: contracts.add_item(org, editor, contract, {"record": str(a["app"].id)}),
        "contract lines, set at once": lambda: contracts.set_items(org, editor, contract, [(a["app"], None)]),
        "relationship": lambda: services.create_relationship(
            org, editor, {"type": "depends_on", "source": str(a["app"].id), "target": str(other.id)}
        ),
        "person": lambda: services.create_person(org, editor, {"name": "P"}),
        "team": lambda: admin_services.create_named(Team, org, editor, {"name": "T"}),
        "department": lambda: admin_services.create_named(Department, org, editor, {"name": "D"}),
        "category": lambda: admin_services.create_named(SoftwareCategory, org, editor, {"name": "C"}),
        "vendor": lambda: admin_services.create_named(Vendor, org, editor, {"name": "V"}),
        "tag": lambda: admin_services.create_tag(org, editor, {"name": "fresh"}),
        "relationship type": lambda: relationship_types.create_type(
            org, admin, {"name": "Feeds", "slug": "feeds", "inverse_name": "Fed by"}
        ),
        "custom field": lambda: customfields.create_field(
            org, admin, {"name": "tier", "label": "Tier", "type": "text", "applies_to": ["application"]}
        ),
        "saved view": lambda: saved_filters.create(org, editor, {"kind": "application", "name": "Mine", "query": {}}),
        "AI provider": lambda: ai.create_provider(org, editor, {"name": "Prov"}),
        "contract": lambda: contracts.create_contract(org, editor, {"name": "K", "contract_number": "K-1"}),
        "license pool": lambda: contracts.create_pool(org, editor, {"application": str(a["app"].id), "sku_name": "E5"}),
        "renewal": lambda: renewals.create_renewal(org, editor, {"name": "R"}),
        "notification rule": lambda: rule_services.create_rule(
            org, admin, {"name": "Rule", "source_types": ["contract"], "lead_times_days": [30]}
        ),
        "Teams destination": lambda: channel_services.create_destination(
            org, admin, TeamsDestination, {"name": "Ch", "url": "https://example.test/flows/1?sig=x"}
        ),
        "webhook": lambda: channel_services.create_destination(
            org,
            admin,
            WebhookDestination,
            {"name": "Hook", "url": "https://example.test/hook", "signing_secret": "s" * 32},
        ),
        "API token": lambda: tokens.create_token(org, admin, membership, "T", False, None),
        "import source": lambda: importer.create_source(org, admin, {"name": "S", "kind": "application"}),
    }
    for what, attempt in attempts.items():
        with pytest.raises(LimitReached) as refused:
            attempt()
        assert refused.value.limit == "other_records", what


# ---- records -----------------------------------------------------------------------------------------------------------


def test_a_single_create_at_the_limit_is_refused_with_the_problem(a, api):
    org = a["org"]
    set_limit(org, "records", 2)
    assert api(a["users"]["editor"]).post("applications", {"name": "Second", "lifecycle": "active"}).status_code == 201
    response = api(a["users"]["editor"]).post("components", {"name": "Third", "lifecycle": "active"})
    body = assert_limit_reached(response, "records", 2, 2)
    assert "2 catalog records" in body["detail"] and "Archived records count too" in body["detail"]
    agent = api(a["users"]["editor"]).post("ai-agents", {"name": "Bot", "lifecycle": "active"})
    assert_limit_reached(agent, "records", 2, 2)
    assert records(org) == 2
    assert not AuditEvent.objects.filter(organization=org, action="resource.created", changes__name="Third").exists()


def test_archived_records_count(a, api):
    services.archive_resource(a["org"], a["users"]["editor"], a["app"])
    set_limit(a["org"], "records", 1)
    response = api(a["users"]["editor"]).post("applications", {"name": "New", "lifecycle": "active"})
    assert_limit_reached(response, "records", 1, 1)


def test_updates_and_deletes_still_work_over_the_limit_and_lowering_it_keeps_the_data(a, api):
    org = a["org"]
    second = services.create_resource(org, a["users"]["editor"], {"name": "Second", "lifecycle": "active"})
    set_limit(org, "records", 0)  # below the usage: nothing is removed
    assert records(org) == 2
    editor = api(a["users"]["editor"])
    response = editor.patch(f"applications/{a['app'].id}", {"description": "Still editable"})
    assert response.status_code == 200, response.json_body
    assert editor.post(f"applications/{second.id}/archive").status_code == 200
    assert editor.post(f"applications/{second.id}/restore").status_code == 200
    assert api(a["users"]["admin"]).delete(f"applications/{second.id}").status_code == 204
    assert records(org) == 1
    assert_limit_reached(editor.post("applications", {"name": "No", "lifecycle": "active"}), "records", 0, 1)


def test_a_concurrent_pair_at_one_below_the_limit_lets_only_one_through(transactional_db):
    from tests.conftest import make_org

    f = make_org("race")
    org, editor = f["org"], f["users"]["editor"]
    OrganizationLimitOverride.objects.create(organization=org, limit="records", value=2, reason="race")
    Organization.objects.filter(pk=org.pk).update(has_limit_overrides=True)

    def create(name):
        return lambda: services.create_resource(
            Organization.objects.get(pk=org.pk), editor, {"name": name, "lifecycle": "active"}
        )

    errors = run_together(create("One"), create("Two"))
    assert sorted(type(e).__name__ for e in errors if e is not None) == ["LimitReached"]
    assert records(org) == 2


def import_csv(client, *names):
    data = csvio.write_csv(csvio.TEMPLATE[Kind.APPLICATION], [{"name": n, "lifecycle": "active"} for n in names])
    response = client.client.post(
        "/api/v1/imports",
        {"file": SimpleUploadedFile("apps.csv", data, content_type="text/csv"), "kind": "application"},
    )
    assert response.status_code == 201, response.content
    return response.json()


def test_an_import_that_would_pass_the_limit_is_refused_whole(a, api):
    org = a["org"]
    editor = api(a["users"]["editor"])
    run = import_csv(editor, "Okta", "Slack", "Zoom")
    set_limit(org, "records", 3)  # one in the organization, room for two
    response = editor.post(f"imports/{run['id']}/commit")
    body = assert_limit_reached(response, "records", 3, 1)
    assert body["adding"] == 3 and "2 records are left" in body["detail"]
    assert records(org) == 1  # nothing was written
    assert editor.get(f"imports/{run['id']}").json_body["status"] == "previewed"  # still committable later
    set_limit(org, "records", 4)
    assert editor.post(f"imports/{run['id']}/commit").status_code == 200
    assert records(org) == 4


def test_creating_a_record_from_a_discovery_candidate_is_limited(a, api):
    from tests.test_discovery import make_connector

    org = a["org"]
    connector = make_connector(org, a["users"]["admin"])
    discovery.run(connector)
    from discovery.models import Observation

    candidate = Observation.objects.filter(connector=connector, kind="application").order_by("display_name").first()
    assert candidate is not None
    set_limit(org, "records", records(org))
    response = api(a["users"]["editor"]).post(f"discovery/observations/{candidate.id}/create", {})
    assert_limit_reached(response, "records", records(org), records(org))
    # Discovery's review queue isn't limited: the run above kept every observation.
    assert Observation.objects.filter(connector=connector).count() > 1


# ---- connectors -------------------------------------------------------------------------------------------------------


def test_a_connector_over_the_limit_is_refused_before_its_connection_is_made(a, api):
    from core.models import MicrosoftConnection

    org = a["org"]
    from tests.test_discovery import make_connector

    make_connector(org, a["users"]["admin"])
    set_limit(org, "connectors", 1)
    connections = MicrosoftConnection.objects.filter(organization=org).count()
    body = {"source": "entra", "name": "Second", "tenant_id": "simulated", "secret": "synthetic-secret"}
    response = api(a["users"]["admin"]).post("connectors", body)
    assert_limit_reached(response, "connectors", 1, 1)
    assert Connector.objects.filter(organization=org).count() == 1
    assert MicrosoftConnection.objects.filter(organization=org).count() == connections


# ---- members ----------------------------------------------------------------------------------------------------------


def test_an_invitation_over_the_members_limit_is_refused(a, api):
    set_limit(a["org"], "members", 3)  # the fixture's three
    response = api(a["users"]["admin"]).post("invitations", {"email": "new@orga.test", "role": "viewer"})
    assert_limit_reached(response, "members", 3, 3)
    response = api(a["users"]["admin"]).post("users", {"email": "new@orga.test", "role": "viewer"})
    assert_limit_reached(response, "members", 3, 3)
    assert not Invitation.objects.exists()


def test_a_deactivated_member_frees_a_place(a, api):
    set_limit(a["org"], "members", 3)
    viewer = a["users"]["viewer"]
    viewer.is_active = False
    viewer.save()
    response = api(a["users"]["admin"]).post("invitations", {"email": "new@orga.test", "role": "viewer"})
    assert response.status_code == 201, response.json_body


def test_accepting_an_invitation_checks_the_limit_again(a, api):
    org = a["org"]
    _, token = invitations.create_invitation(org, a["users"]["admin"], {"email": "late@orga.test", "role": "viewer"})
    set_limit(org, "members", 3)  # lowered after the invitation was sent
    response = Client().post(
        "/api/v1/auth/invitations/accept",
        json.dumps({"token": token, "name": "Late Comer", "password": "a-much-longer-passphrase"}),
        content_type="application/json",
    )
    assert_limit_reached(response, "members", 3, 3)
    assert not get_user_model().objects.filter(email="late@orga.test").exists()
    assert Membership.objects.filter(organization=org).count() == 3


def test_an_existing_account_accepting_is_limited_too(a, b, api):
    org = a["org"]
    outsider = b["users"]["viewer"]
    _, token = invitations.create_invitation(org, a["users"]["admin"], {"email": outsider.email, "role": "viewer"})
    set_limit(org, "members", 3)
    response = api(outsider).post("auth/invitations/accept", {"token": token})
    assert_limit_reached(response, "members", 3, 3)
    assert not Membership.objects.filter(organization=org, user=outsider).exists()


def test_approving_pending_access_is_limited(a, api):
    org = a["org"]
    now = timezone.now()
    row = PendingAccess.objects.create(
        organization=org,
        issuer="https://idp.example.test",
        subject="pat",
        email="pat@orga.test",
        name="Pat Pending",
        first_seen=now,
        last_seen=now,
    )
    set_limit(org, "members", 3)
    response = api(a["users"]["admin"]).post(f"pending-access/{row.id}/approve", {"role": "viewer"})
    assert_limit_reached(response, "members", 3, 3)
    assert PendingAccess.objects.filter(pk=row.pk).exists()
    assert not get_user_model().objects.filter(email="pat@orga.test").exists()


def test_signup_checks_the_new_organizations_members_limit(settings, db, monkeypatch):
    from tests.test_signup import sign_up, verify

    settings.SOFTWARDEN_SIGNUP_ENABLED = True
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    monkeypatch.setitem(limits.EDITION_LIMITS, Edition.CLOUD, {**limits.EDITION_LIMITS[Edition.CLOUD], "members": 0})
    response = verify(Client(), sign_up("owner@fabrikam.test"))
    assert_limit_reached(response, "members", 0, 0)
    assert not Organization.objects.filter(name="Fabrikam").exists()


# ---- attachment bytes -------------------------------------------------------------------------------------------------


def test_an_upload_past_the_attachment_bytes_limit_is_refused(a, api):
    set_limit(a["org"], "attachment_bytes", 30)
    client = api(a["users"]["editor"]).client

    def upload(content):
        response = client.post(
            f"/api/v1/applications/{a['app'].id}/attachments",
            {"file": SimpleUploadedFile("notes.txt", content), "title": "Notes"},
        )
        return response

    assert upload(b"twenty bytes of text").status_code == 201
    refused = upload(b"another twenty bytes")
    body = assert_limit_reached(refused, "attachment_bytes", 30, 20)
    assert body["adding"] == 20
    assert Attachment.objects.filter(organization=a["org"]).count() == 1
    # A link has no size and isn't limited.
    link = api(a["users"]["editor"]).post(
        f"applications/{a['app'].id}/attachments", {"kind": "link", "url": "https://example.test/doc", "title": "Doc"}
    )
    assert link.status_code == 201, link.json_body


# ---- the organization's API rate -------------------------------------------------------------------------------------


def session(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def one_window(monkeypatch):
    """The organization's API rate counts per clock minute (core/counters.py), so a test that straddled a minute
    boundary would start a fresh window halfway. Pin the clock mid-minute so each test stays in one window."""
    from core import counters

    monkeypatch.setattr(counters, "clock", lambda: 1_800_000_030.0)


def test_the_org_rate_counts_every_user_of_one_organization_together(a, b, settings, one_window):
    settings.API_READ_RATE = "1000/min"  # the per-user bucket out of the way
    set_limit(a["org"], "api_rate_read", 3)
    set_limit(b["org"], "api_rate_read", 3)
    admin, editor = session(a["users"]["admin"]), session(a["users"]["editor"])
    assert admin.get("/api/v1/vendors").status_code == 200
    assert admin.get("/api/v1/vendors").status_code == 200
    assert editor.get("/api/v1/vendors").status_code == 200
    refused = editor.get("/api/v1/vendors")  # another user, the same organization's bucket
    assert refused.status_code == 429 and refused.json()["type"] == "/problems/rate-limited"
    assert int(refused["Retry-After"]) >= 1
    assert admin.get(f"/api/v1/applications/{a['app'].id}").status_code == 429
    # Another organization has its own bucket.
    other = session(b["users"]["viewer"])
    for _ in range(3):
        assert other.get("/api/v1/vendors").status_code == 200
    assert other.get("/api/v1/vendors").status_code == 429


def test_the_org_write_rate_is_its_own_bucket(a, settings, one_window):
    settings.API_WRITE_RATE = "1000/min"
    set_limit(a["org"], "api_rate_write", 1)
    admin, editor = session(a["users"]["admin"]), session(a["users"]["editor"])

    def post(client, name):
        return client.post("/api/v1/vendors", json.dumps({"name": name}), content_type="application/json")

    assert post(admin, "One").status_code == 201
    assert post(editor, "Two").status_code == 429
    assert editor.get("/api/v1/vendors").status_code == 200  # reads are unlimited here


def test_the_org_rate_is_skipped_when_unlimited(a, settings):
    settings.API_READ_RATE = "1000/min"
    assert limits.limits(a["org"])["api_rate_read"] is None
    client = session(a["users"]["viewer"])
    for _ in range(25):
        assert client.get("/api/v1/vendors").status_code == 200


def test_an_api_token_shares_its_organizations_bucket(a, settings, one_window):
    from tests.test_rate_limits import token_client

    settings.API_READ_RATE = "1000/min"
    set_limit(a["org"], "api_rate_read", 2)
    script, browser = token_client(a), session(a["users"]["admin"])
    assert script.get("/api/v1/vendors").status_code == 200
    assert browser.get("/api/v1/vendors").status_code == 200
    assert script.get("/api/v1/vendors").status_code == 429


# ---- a fresh cloud organization's first steps fit its defaults ------------------------------------------------------


def test_cloud_defaults_fit_the_first_steps_comfortably():
    floors = {
        "members": 10,
        "connectors": 3,
        "records": 1000,
        "other_records": 10000,
        "api_rate_read": 600,
        "api_rate_write": 120,
    }
    cloud_limits = limits.EDITION_LIMITS[Edition.CLOUD]
    for name, floor in floors.items():
        value = cloud_limits[name]
        assert value is not None and value >= floor, name


def test_an_organization_without_overrides_reads_its_limits_without_a_query(a, django_assert_num_queries):
    org = cloud(a["org"])
    with django_assert_num_queries(0):
        assert limits.limits(org)["api_rate_read"] == 1200
    set_limit(org, "api_rate_read", 10)
    assert Organization.objects.get(pk=org.pk).has_limit_overrides is True
    platform_admin.remove_limit(
        get_user_model().objects.get(platform_staff__isnull=False), org, "api_rate_read", {"reason": "Done"}
    )
    assert Organization.objects.get(pk=org.pk).has_limit_overrides is False


# ---- review follow-ups --------------------------------------------------------------------------------------------------


def test_reactivating_a_member_counts_against_the_members_limit(a, api):
    org, admin = a["org"], api(a["users"]["admin"])
    viewer = a["users"]["viewer"]
    assert admin.patch(f"users/{viewer.id}", {"active": False}).status_code == 200
    set_limit(org, "members", 3)
    # The freed place is taken by someone new.
    _, token = invitations.create_invitation(org, a["users"]["admin"], {"email": "new@orga.test", "role": "viewer"})
    accepted = Client().post(
        "/api/v1/auth/invitations/accept",
        json.dumps({"token": token, "name": "New Person", "password": "a-much-longer-passphrase"}),
        content_type="application/json",
    )
    assert accepted.status_code == 200, accepted.content
    response = admin.patch(f"users/{viewer.id}", {"active": True})
    assert_limit_reached(response, "members", 3, 3)
    viewer.refresh_from_db()
    assert viewer.is_active is False
    assert Membership.objects.filter(organization=org, user__is_active=True).count() == 3
    # A role change of an active member is an update and is never refused.
    assert admin.patch(f"users/{a['users']['editor'].id}", {"role": "viewer"}).status_code == 200


@pytest.mark.parametrize("name", ["api_rate_read", "api_rate_write"])
def test_an_api_rate_is_at_least_one(a, staff, api, name):
    response = api(staff).post(
        f"platform/organizations/{a['org'].id}/limits", {"limit": name, "value": 0, "reason": "Lock out"}
    )
    assert response.status_code == 400 and "value" in response.json_body["errors"]
    assert "Suspend" in response.json_body["errors"]["value"][0]
    assert not OrganizationLimitOverride.objects.exists()
    assert set_limit(a["org"], name, 1, staff).value == 1
    assert set_limit(a["org"], "records", 0, staff).value == 0  # a counted limit may be 0


def test_an_import_commit_racing_a_single_create_at_one_below_the_limit(transactional_db):
    from imports import importer
    from tests.conftest import make_org

    f = make_org("race")
    org, editor = f["org"], f["users"]["editor"]
    data = csvio.write_csv(csvio.TEMPLATE[Kind.APPLICATION], [{"name": "Imported", "lifecycle": "active"}])
    run = importer.create_run(org, editor, Kind.APPLICATION, "apps.csv", data)
    OrganizationLimitOverride.objects.create(organization=org, limit="records", value=2, reason="race")
    Organization.objects.filter(pk=org.pk).update(has_limit_overrides=True)

    def commit():
        importer.commit_run(Organization.objects.get(pk=org.pk), editor, run)

    def single():
        services.create_resource(Organization.objects.get(pk=org.pk), editor, {"name": "Typed", "lifecycle": "active"})

    errors = run_together(commit, single)
    assert sorted(type(e).__name__ for e in errors if e is not None) == ["LimitReached"]
    assert records(org) == 2
