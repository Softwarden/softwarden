"""Platform admin console: platform staff, the platform audit stream, organizations and usage, edition,
deletion and suspension from the platform, capability overrides, and impersonation (catalog/platform_admin.py,
catalog/platform_views.py, core/platform_access.py, core/impersonation.py)."""

from __future__ import annotations

import ast
import io
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, transaction
from django.test import Client
from django.utils import timezone

from catalog import platform_admin, usage
from core import access, capabilities, impersonation, jobs
from core.models import (
    AuditAppendOnly,
    AuditEvent,
    CapabilityOverride,
    Job,
    JobStatus,
    Membership,
    Organization,
    OrganizationUsage,
    PlatformAuditEvent,
    PlatformStaff,
)
from tests.test_drf_resources import offset_timestamps
from tests.test_permissions import PLATFORM_ROUTES
from tests.test_problems import dummy, is_problem
from tests.test_url_coverage import declarations

PASSWORD = "correct-horse-battery"
RESTRICTED_TYPE = "/problems/impersonation-restricted"
SUSPENDED_TYPE = "/problems/organization-suspended"


@pytest.fixture(autouse=True)
def empty_cache():
    """Starting an impersonation shares the sign-in throttle, which counts in the process cache across tests."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def make_staff(email="ops@platform.test", org=None, role="admin"):
    user = get_user_model().objects.create_user(username=email, email=email, password=PASSWORD)
    platform_admin.grant_staff(user)
    if org is not None:
        Membership.objects.create(user=user, organization=org, role=role)
    return user


@pytest.fixture
def staff(db):
    return make_staff()


def start(client, org, member, reason="Support ticket 42", password=PASSWORD):
    body = {"organization_id": str(org.id), "user_id": member.id, "reason": reason}
    if password is not None:
        body["password"] = password
    return client.post("platform/impersonation", body)


def send(client, method: str, url: str):
    """One request with an empty JSON body (none for GET)."""
    method = method.lower()
    if method == "get":
        return client.get(url)
    return getattr(client, method)(url, json.dumps({}), content_type="application/json")


def session_values(client) -> dict:
    return dict(client.client.session.items())


# ---- the management command and platform staff ---------------------------------------------------------------------


def test_platform_staff_command_grants_lists_and_revokes(a):
    user = a["users"]["viewer"]
    out = io.StringIO()
    call_command("platform_staff", "grant", user.email.upper(), stdout=out)
    assert "is now platform staff" in out.getvalue()
    assert PlatformStaff.objects.filter(user=user).exists()
    out = io.StringIO()
    call_command("platform_staff", "grant", user.email, stdout=out)
    assert "already" in out.getvalue()
    out = io.StringIO()
    call_command("platform_staff", "list", stdout=out)
    assert f"{user.email}\tactive" in out.getvalue()
    out = io.StringIO()
    call_command("platform_staff", "revoke", user.email, stdout=out)
    assert "no longer platform staff" in out.getvalue()
    assert not PlatformStaff.objects.filter(user=user).exists()
    events = list(PlatformAuditEvent.objects.order_by("created_at").values_list("action", "details"))
    assert events == [
        ("staff.granted", {"user": user.id, "email": user.email, "via": "cli"}),
        ("staff.revoked", {"user": user.id, "email": user.email, "via": "cli"}),
    ]
    # Staff status confers no role: the account's membership and role are unchanged, and no tenant event was written.
    assert Membership.objects.get(user=user).role == "viewer"
    assert not AuditEvent.objects.filter(action__startswith="staff.").exists()
    with pytest.raises(CommandError, match="No account"):
        call_command("platform_staff", "grant", "nobody@x.test")
    with pytest.raises(CommandError, match="email"):
        call_command("platform_staff", "grant")
    user.is_active = False
    user.save()
    with pytest.raises(CommandError, match="deactivated"):
        call_command("platform_staff", "grant", user.email)
    out = io.StringIO()
    call_command("platform_staff", "list", stdout=out)
    assert "No platform staff" in out.getvalue()


def test_staff_without_a_membership_signs_in_to_the_console_only(staff, a, api):
    client = Client()
    response = client.post(
        "/api/v1/auth/login", json.dumps({"email": staff.email, "password": PASSWORD}), content_type="application/json"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["platform_staff"] is True and body["user"]["email"] == staff.email and body["impersonation"] is None
    assert client.get("/api/v1/platform/organizations").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 403  # no organization
    assert client.get("/api/v1/applications").status_code == 403  # staff status gives no role anywhere
    # An ordinary account with no membership is still refused at sign-in.
    loner = get_user_model().objects.create_user(username="lone@x.test", email="lone@x.test", password=PASSWORD)
    response = Client().post(
        "/api/v1/auth/login", json.dumps({"email": loner.email, "password": PASSWORD}), content_type="application/json"
    )
    assert response.status_code == 403


def test_me_says_whether_you_are_platform_staff(a, api):
    member = a["users"]["editor"]
    assert api(member).get("auth/me").json_body["platform_staff"] is False
    platform_admin.grant_staff(member)
    me = api(member).get("auth/me").json_body
    assert me["platform_staff"] is True and me["impersonation"] is None and me["organization_suspended"] is None


def test_me_says_the_deployment(a, api, settings):
    """`auth/me.deployment` follows SOFTWARDEN_DEPLOYMENT, so the SPA can say who can act."""
    member = a["users"]["admin"]
    settings.SOFTWARDEN_CLOUD = False
    assert api(member).get("auth/me").json_body["deployment"] == "selfhosted"
    settings.SOFTWARDEN_CLOUD = True
    assert api(member).get("auth/me").json_body["deployment"] == "cloud"


def test_a_purge_leaves_a_staff_account_active(staff, a):
    from catalog import org_lifecycle

    org_lifecycle._release_accounts(a["org"].pk, [staff.pk, a["users"]["viewer"].pk])
    staff.refresh_from_db()
    assert staff.is_active  # no membership, but staff: kept for the console


# ---- access: every platform route ------------------------------------------------------------------------------------


def platform_paths(org, override):
    return [(method, path.format(org=org.id, override=override.id)) for method, path in PLATFORM_ROUTES]


@pytest.fixture
def override(a, staff):
    return platform_admin.set_override(
        staff, a["org"], {"capability": "org.self_delete", "effect": "grant", "reason": "Trial"}
    )


def test_every_platform_route_needs_a_staff_session(a, staff, override, api):
    from api.tokens import create_token

    # A staff account that is also a member can own an API token; the token still can't reach the console.
    member_staff = make_staff("ops2@platform.test", org=a["org"])
    _token, secret = create_token(
        a["org"],
        member_staff,
        Membership.objects.get(user=member_staff),
        "Ops",
        False,
        timezone.localdate() + timedelta(days=5),
    )
    bearer = Client(HTTP_AUTHORIZATION=f"Bearer {secret}")
    for method, path in platform_paths(a["org"], override):
        url = "/api/v1/" + path
        for client in (Client(), api(a["users"]["admin"]).client, bearer):
            response = send(client, method, url)
            is_problem(response, 403)
        response = send(bearer, method, url)
        assert "API tokens" in response.json()["detail"], (method, path)
        response = send(api(staff).client, method, url)
        assert response.status_code not in (401, 403), (method, path, response.status_code)


# ---- organizations and usage ----------------------------------------------------------------------------------------


def test_the_usage_job_writes_every_organizations_counts(a, b):
    from catalog import services
    from catalog.models import Attachment

    services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Job", "kind": "component", "lifecycle": "active"}
    )
    Attachment.objects.create(
        organization=a["org"],
        resource=a["app"],
        kind="file",
        title="a.pdf",
        storage_name="x",
        size=1234,
        sha256="0" * 64,
    )
    Job.objects.create(organization=a["org"], kind="x", status=JobStatus.SUCCEEDED, finished_at=timezone.now())
    Job.objects.create(
        organization=a["org"], kind="x", status=JobStatus.SUCCEEDED, finished_at=timezone.now() - timedelta(days=40)
    )
    assert "platform.usage" in jobs.SCHEDULES
    result = usage.refresh_all(timezone.now())
    assert result == {"organizations": 2}
    snap = OrganizationUsage.objects.get(organization=a["org"])
    counts = snap.counts
    assert counts["records"] == {"application": 1, "component": 1, "integration": 0, "ai_agent": 0}
    assert counts["records_total"] == 2
    assert (counts["members"], counts["active_members"]) == (3, 3)
    assert (counts["attachments"], counts["attachment_bytes"]) == (1, 1234)
    assert counts["jobs_30d"] == 1 and counts["connectors"] == 0 and counts["exports"] == 0
    assert snap.last_activity_at is not None
    assert OrganizationUsage.objects.get(organization=b["org"]).counts["records_total"] == 1
    usage.refresh_all(timezone.now())  # refreshed in place, one row per organization
    assert OrganizationUsage.objects.filter(organization=a["org"]).count() == 1
    assert usage.COUNTERS["members"](a["org"].pk) == 3  # reusable one by one (the limits package)


def test_organization_list_filters_and_pages(a, b, staff, api):
    from catalog import services

    services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Job", "kind": "component", "lifecycle": "active"}
    )
    usage.refresh_all(timezone.now())
    capabilities.set_edition(b["org"], "enterprise")
    client = api(staff)
    rows = client.get("platform/organizations").json_body["results"]
    assert [r["slug"] for r in rows] == ["orga", "orgb"]
    row = rows[0]
    assert row["member_count"] == 3 and row["edition"] == "community" and row["deletion"] is None
    assert row["usage"]["counts"]["records_total"] == 2 and row["last_activity_at"] is not None
    assert row["suspended_at"] is None and row["created_at"]
    assert [r["slug"] for r in client.get("platform/organizations?edition=enterprise").json_body["results"]] == ["orgb"]
    assert [r["slug"] for r in client.get("platform/organizations?q=RGA").json_body["results"]] == ["orga"]
    Organization.objects.filter(pk=a["org"].pk).update(deletion_due_at=timezone.now() + timedelta(days=3))
    assert [r["slug"] for r in client.get("platform/organizations?deletion=scheduled").json_body["results"]] == ["orga"]
    assert [r["slug"] for r in client.get("platform/organizations?deletion=none").json_body["results"]] == ["orgb"]
    assert client.get("platform/organizations?suspended=true").json_body["results"] == []
    page = client.get("platform/organizations?page_size=1&ordering=-created_at").json_body
    assert len(page["results"]) == 1 and page["next"]
    second = client.get(f"platform/organizations?page_size=1&ordering=-created_at&cursor={page['next']}").json_body
    assert {page["results"][0]["slug"], second["results"][0]["slug"]} == {"orga", "orgb"}
    for bad in ("nope=1", "edition=platinum", "deletion=maybe", "suspended=yes", "ordering=edition"):
        is_problem(client.get(f"platform/organizations?{bad}"), 400)


def test_organization_detail_shows_members_capabilities_and_usage(a, staff, override, api):
    other_staff = make_staff("ops3@platform.test", org=a["org"], role="viewer")
    body = api(staff).get(f"platform/organizations/{a['org'].id}").json_body
    assert body["slug"] == "orga" and body["timezone"] == "UTC" and body["member_count"] == 4
    members = {m["email"]: m for m in body["members"]}
    assert members["admin@orga.test"]["role"] == "admin" and members["admin@orga.test"]["platform_staff"] is False
    assert members[other_staff.email]["platform_staff"] is True
    assert body["live_usage"]["counts"]["members"] == 4
    assert body["usage"] is None  # no snapshot yet
    caps = body["capabilities"]
    assert "org.self_delete" not in caps["edition"] and "org.self_delete" in caps["effective"]
    assert caps["overrides"][0]["capability"] == "org.self_delete" and caps["overrides"][0]["live"] is True
    assert body["recent_events"][0]["action"] == "capability.granted"
    is_problem(api(staff).get("platform/organizations/00000000-0000-0000-0000-000000000000"), 404)


def test_platform_audit_stream_filters(a, b, staff, api):
    platform_admin.suspend(staff, a["org"], {"reason": "Unpaid"})
    platform_admin.suspend(staff, b["org"], {"reason": "Abuse"})
    client = api(staff)
    rows = client.get(f"platform/audit-events?organization={a['org'].id}").json_body["results"]
    assert [(r["action"], r["details"]["reason"]) for r in rows] == [("organization.suspended", "Unpaid")]
    assert rows[0]["actor"]["email"] == staff.email and rows[0]["label"] == "Organization suspended"
    assert len(client.get("platform/audit-events?action=organization.suspended").json_body["results"]) == 2
    assert client.get(f"platform/audit-events?actor={staff.id}").json_body["results"]
    for bad in ("organization=x", "actor=me", "nope=1"):
        is_problem(client.get(f"platform/audit-events?{bad}"), 400)


# ---- edition, deletion and suspension from the platform -----------------------------------------------------------


def test_edition_change_is_audited_in_both_streams(a, staff, api):
    client = api(staff)
    url = f"platform/organizations/{a['org'].id}/edition"
    is_problem(client.post(url, {"edition": "enterprise"}), 400)  # a reason is required
    is_problem(client.post(url, {"edition": "platinum", "reason": "x"}), 400)
    response = client.post(url, {"edition": "enterprise", "reason": "Signed contract #7"})
    assert response.status_code == 200 and response.json_body["edition"] == "enterprise"
    assert "org.tenant_backups" in response.json_body["capabilities"]["effective"]
    event = AuditEvent.objects.get(organization=a["org"], action="organization.edition_changed")
    assert event.actor == staff
    assert event.changes == {"edition": ["community", "enterprise"], "via": "platform", "reason": "Signed contract #7"}
    platform = PlatformAuditEvent.objects.get(action="organization.edition_changed")
    assert platform.organization_id == a["org"].id and platform.actor == staff
    assert platform.details == {"edition": ["community", "enterprise"], "reason": "Signed contract #7"}
    client.post(url, {"edition": "enterprise", "reason": "Again"})  # unchanged: nothing written
    assert PlatformAuditEvent.objects.filter(action="organization.edition_changed").count() == 1
    # The tenant's Admins see who acted and why.
    logged = api(a["users"]["admin"]).get("audit-events?action=organization.edition_changed").json_body["results"]
    assert logged[0]["actor"]["name"] == staff.email and "Signed contract #7" in logged[0]["summary"]


def test_cancelling_a_scheduled_deletion_from_the_platform(a, staff, api):
    client = api(staff)
    url = f"platform/organizations/{a['org'].id}/cancel-deletion"
    is_problem(client.post(url, {"reason": "Mistake"}), 409)
    due = timezone.now() + timedelta(days=10)
    Organization.objects.filter(pk=a["org"].pk).update(deletion_due_at=due, deletion_requested_at=timezone.now())
    assert client.get(f"platform/organizations/{a['org'].id}").json_body["deletion"]["due_at"]
    response = client.post(url, {"reason": "Customer asked by phone"})
    assert response.status_code == 200 and response.json_body["deletion"] is None
    assert Organization.objects.get(pk=a["org"].pk).deletion_due_at is None
    event = AuditEvent.objects.get(organization=a["org"], action="organization.deletion_cancelled")
    assert event.actor == staff and event.changes["via"] == "platform"
    assert event.changes["reason"] == "Customer asked by phone"
    platform = PlatformAuditEvent.objects.get(action="organization.deletion_cancelled")
    assert platform.organization_id == a["org"].id and platform.details["reason"] == "Customer asked by phone"


def test_suspension_refuses_members_and_holds_jobs(a, b, staff, api):
    from api.tokens import create_token

    client = api(staff)
    admin = a["users"]["admin"]
    _token, secret = create_token(
        a["org"], admin, Membership.objects.get(user=admin), "Script", True, timezone.localdate() + timedelta(days=5)
    )
    held = Job.objects.create(organization=a["org"], kind="org.export")
    response = client.post(f"platform/organizations/{a['org'].id}/suspend", {"reason": "Unpaid invoice"})
    assert response.status_code == 200 and response.json_body["suspended_at"]
    assert response.json_body["suspended_reason"] == "Unpaid invoice"
    is_problem(client.post(f"platform/organizations/{a['org'].id}/suspend", {"reason": "Again"}), 409)
    member = api(a["users"]["editor"])
    for url in ("applications", "needs-attention", "organization"):
        is_problem(member.get(url), 403, SUSPENDED_TYPE)
    is_problem(Client(HTTP_AUTHORIZATION=f"Bearer {secret}").get("/api/v1/applications"), 403, SUSPENDED_TYPE)
    me = member.get("auth/me").json_body  # the SPA can still say why
    assert me["organization_suspended"]["suspended_at"]
    assert api(b["users"]["editor"]).get("applications").status_code == 200  # other organizations are untouched
    installation = Job.objects.create(kind="jobs.prune")
    assert jobs.claim(ids=[held.id]) is None  # the suspended organization's job stays queued
    assert jobs.claim(ids=[installation.id]).id == installation.id  # installation-wide work still runs
    assert set(AuditEvent.objects.filter(organization=a["org"]).values_list("action", flat=True)) >= {
        "organization.suspended"
    }
    response = client.post(f"platform/organizations/{a['org'].id}/unsuspend", {"reason": "Paid"})
    assert response.status_code == 200 and response.json_body["suspended_at"] is None
    assert member.get("applications").status_code == 200
    assert jobs.claim(ids=[held.id]).id == held.id
    is_problem(client.post(f"platform/organizations/{a['org'].id}/unsuspend", {"reason": "Again"}), 409)
    events = PlatformAuditEvent.objects.filter(organization_id=a["org"].id).order_by("created_at")
    assert list(events.values_list("action", flat=True)) == ["organization.suspended", "organization.unsuspended"]
    tenant = AuditEvent.objects.get(organization=a["org"], action="organization.unsuspended")
    assert tenant.actor == staff and tenant.changes["reason"] == "Paid"


def test_a_suspended_members_other_organization_still_works(a, b, staff, api):
    user = a["users"]["editor"]
    Membership.objects.create(user=user, organization=b["org"], role="viewer")
    platform_admin.suspend(staff, a["org"], {"reason": "Unpaid"})
    client = api(user)
    response = client.post("auth/me/organization", {"organization_id": str(b["org"].id)})
    assert response.status_code == 200 and response.json_body["organization"]["slug"] == "orgb"
    assert client.get("applications").status_code == 200


# ---- capability overrides ------------------------------------------------------------------------------------------


def test_a_grant_applies_from_the_next_request_and_is_audited(a, staff, api):
    admin = api(a["users"]["admin"])
    is_problem(admin.post("organization/deletion", {}), 403, "/problems/capability-required")  # a cloud capability
    url = f"platform/organizations/{a['org'].id}/capability-overrides"
    response = api(staff).post(url, {"capability": "org.self_delete", "effect": "grant", "reason": "Pilot"})
    assert response.status_code == 201 and response.json_body["effect"] == "grant"
    past_the_gate = admin.post("organization/deletion", {})
    assert past_the_gate.status_code == 400 and past_the_gate.json_body["errors"]
    assert "org.self_delete" in admin.get("auth/me").json_body["capabilities"]
    event = AuditEvent.objects.get(organization=a["org"], action="organization.capability_granted")
    assert event.actor == staff
    assert event.changes == {"capability": "org.self_delete", "expires_at": None, "via": "platform", "reason": "Pilot"}
    assert PlatformAuditEvent.objects.get(action="capability.granted").details["capability"] == "org.self_delete"


def test_a_revoke_takes_a_capability_away_and_removing_it_restores_the_edition(a, staff, api):
    admin = api(a["users"]["admin"])
    url = f"platform/organizations/{a['org'].id}/capability-overrides"
    body = api(staff).post(url, {"capability": "imports", "effect": "revoke", "reason": "Abuse"}).json_body
    assert "imports" not in admin.get("auth/me").json_body["capabilities"]
    assert not capabilities.has_capability(Organization.objects.get(pk=a["org"].pk), "imports")
    assert AuditEvent.objects.filter(organization=a["org"], action="organization.capability_revoked").exists()
    assert PlatformAuditEvent.objects.filter(action="capability.revoked").exists()
    is_problem(api(staff).post(f"{url}/{body['id']}/remove", {}), 400)  # reason required
    assert api(staff).post(f"{url}/{body['id']}/remove", {"reason": "Resolved"}).status_code == 204
    assert "imports" in admin.get("auth/me").json_body["capabilities"]
    assert not CapabilityOverride.objects.exists()
    assert AuditEvent.objects.filter(organization=a["org"], action="organization.capability_override_removed").exists()
    assert PlatformAuditEvent.objects.filter(action="capability.override_removed").exists()


def test_an_override_expires(a, staff, api):
    admin = api(a["users"]["admin"])
    url = f"platform/organizations/{a['org'].id}/capability-overrides"
    soon = (timezone.now() + timedelta(hours=1)).isoformat()
    response = api(staff).post(
        url, {"capability": "org.self_delete", "effect": "grant", "reason": "Trial", "expires_at": soon}
    )
    assert response.status_code == 201 and response.json_body["expires_at"]
    assert "org.self_delete" in admin.get("auth/me").json_body["capabilities"]
    CapabilityOverride.objects.update(expires_at=timezone.now() - timedelta(seconds=1))  # time passes
    assert "org.self_delete" not in admin.get("auth/me").json_body["capabilities"]
    listed = api(staff).get(url).json_body["overrides"]
    assert listed[0]["live"] is False


def test_override_input_is_checked(a, staff, api):
    url = f"platform/organizations/{a['org'].id}/capability-overrides"
    client = api(staff)
    errors = client.post(url, {"capability": "audit.exprot", "effect": "maybe"}).json_body["errors"]
    assert set(errors) == {"capability", "effect", "reason"}
    past = (timezone.now() - timedelta(hours=1)).isoformat()
    errors = client.post(url, {"capability": "org.self_delete", "effect": "grant", "reason": "x", "expires_at": past})
    assert "expires_at" in errors.json_body["errors"]
    naive = client.post(
        url, {"capability": "org.self_delete", "effect": "grant", "reason": "x", "expires_at": "2099-01-01T00:00"}
    )
    assert "expires_at" in naive.json_body["errors"]
    assert not CapabilityOverride.objects.exists()
    assert not PlatformAuditEvent.objects.filter(action__startswith="capability.").exists()


# Every route and capability, the preview features (Okta, Google Workspace, ServiceNow, Halo) included.
@pytest.mark.usefixtures("preview_on")
def test_capabilities_are_the_edition_plus_grants_minus_revokes(a, staff):
    org = a["org"]
    platform_admin.set_override(staff, org, {"capability": "org.self_delete", "effect": "grant", "reason": "r"})
    platform_admin.set_override(staff, org, {"capability": "imports", "effect": "revoke", "reason": "r"})
    assert capabilities.capabilities(org) == (capabilities.COMMUNITY | {"org.self_delete"}) - {"imports"}
    # Replacing an override of the same name: one row, the new effect.
    platform_admin.set_override(staff, org, {"capability": "org.self_delete", "effect": "revoke", "reason": "r"})
    assert CapabilityOverride.objects.filter(organization=org, capability="org.self_delete").get().effect == "revoke"
    assert "org.self_delete" not in capabilities.capabilities(org)


# ---- impersonation ------------------------------------------------------------------------------------------------


def test_starting_needs_reauthentication_and_a_reason(a, staff, api):
    client = api(staff)
    member = a["users"]["editor"]
    is_problem(start(client, a["org"], member, password="wrong-password-1"), 403, "/problems/reauthentication-required")
    is_problem(start(client, a["org"], member, password=None), 403, "/problems/reauthentication-required")
    errors = start(client, a["org"], member, reason="  ").json_body["errors"]
    assert "reason" in errors
    errors = client.post("platform/impersonation", {"reason": "x", "password": PASSWORD}).json_body["errors"]
    assert set(errors) == {"organization_id", "user_id"}
    stranger = make_staff("x@x.test").id  # not a member of orga
    is_problem(client.post("platform/impersonation", {
        "organization_id": str(a["org"].id), "user_id": stranger, "reason": "x", "password": PASSWORD,
    }), 400)  # fmt: skip
    assert not PlatformAuditEvent.objects.filter(action__startswith="impersonation.").exists()
    response = start(client, a["org"], member)
    assert response.status_code == 201
    body = response.json_body
    assert body["active"] is True and body["minutes"] == 30
    assert body["impersonation"]["user"]["id"] == member.id and body["impersonation"]["by"]["id"] == staff.id
    assert body["impersonation"]["reason"] == "Support ticket 42"


def test_me_shows_the_impersonation(a, staff, api):
    client = api(staff)
    member = a["users"]["editor"]
    start(client, a["org"], member)
    me = client.get("auth/me").json_body
    assert me["user"]["id"] == member.id and me["role"] == "editor" and me["organization"]["slug"] == "orga"
    banner = me["impersonation"]
    assert banner["by"]["id"] == staff.id and banner["reason"] == "Support ticket 42"
    ends = datetime.fromisoformat(banner["expires_at"])
    assert timedelta(minutes=29) < ends - timezone.now() <= timedelta(minutes=30)
    assert me["platform_staff"] is False  # the member's own flag
    session = client.get("platform/session").json_body  # the console still knows who is really signed in
    assert session["user"]["id"] == staff.id and session["impersonation"]["user"]["id"] == member.id


def test_the_time_limit_comes_from_the_environment(a, staff, api, settings):
    settings.SOFTWARDEN_IMPERSONATION_MINUTES = 5
    body = start(api(staff), a["org"], a["users"]["viewer"]).json_body
    ends = datetime.fromisoformat(body["impersonation"]["expires_at"])
    assert ends - timezone.now() <= timedelta(minutes=5)


def test_writes_while_impersonating_record_impersonated_by(a, staff, api):
    client = api(staff)
    member = a["users"]["editor"]
    start(client, a["org"], member)
    response = client.post("applications", {"name": "Made in support", "lifecycle": "active"})
    assert response.status_code == 201
    event = AuditEvent.objects.get(organization=a["org"], action="resource.created")
    assert event.actor == member and event.impersonated_by == staff
    started = AuditEvent.objects.get(organization=a["org"], action="impersonation.started")
    assert started.actor == staff and started.impersonated_by is None
    assert started.changes["user"] == member.id and started.changes["reason"] == "Support ticket 42"
    platform = PlatformAuditEvent.objects.get(action="impersonation.started")
    assert platform.organization_id == a["org"].id and platform.details["user"] == member.id
    # The append-only platform stream outlives a purge, so it names the member by id, never by address.
    assert "email" not in platform.details and member.email not in json.dumps(platform.details)
    # The organization's Admins see both people on the event.
    rows = api(a["users"]["admin"]).get("audit-events?action=resource.created").json_body["results"]
    assert rows[0]["actor"]["id"] == member.id and rows[0]["impersonated_by"]["id"] == staff.id
    plain = api(a["users"]["editor"])
    plain.post("applications", {"name": "Made by the member", "lifecycle": "active"})
    own = AuditEvent.objects.filter(action="resource.created").exclude(pk=event.pk).get()
    assert own.impersonated_by is None


def test_every_export_or_download_read_is_placed():
    """A GET route that looks like data leaving the organization is either refused while impersonating (bulk
    exports) or explicitly allowed with a reason (core/impersonation_routes.ALLOWED_READS)."""
    import re

    from core.impersonation_routes import ALLOWED_READS, RESTRICTED_ROUTES

    found, _ = declarations()
    looks = re.compile(r"export|download|reveal")
    reads = {
        ("GET", route)
        for method, route in found
        if method == "get" and route.startswith("api/v1/") and looks.search(route)
    }
    assert reads, "the sweep saw no export routes"
    unplaced = reads - RESTRICTED_ROUTES - ALLOWED_READS
    assert unplaced == set(), f"restrict these or list them in ALLOWED_READS with a reason: {sorted(unplaced)}"
    assert ALLOWED_READS <= reads and not ALLOWED_READS & RESTRICTED_ROUTES
    assert ("GET", "api/v1/applications/export.csv") in RESTRICTED_ROUTES


def test_a_single_attachment_download_stays_allowed(a, staff, api):
    from catalog.models import Attachment

    attachment = Attachment.objects.create(
        organization=a["org"], resource=a["app"], kind="link", title="Runbook", url="https://example.com/runbook"
    )
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    # A link answers 404 (open its url instead): past the impersonation check, which would answer 403.
    assert client.client.get(f"/api/v1/attachments/{attachment.id}/download").status_code == 404
    is_problem(client.client.get("/api/v1/applications/export.csv"), 403, RESTRICTED_TYPE)


def test_every_restricted_route_is_a_real_route():
    found, _ = declarations()
    for method, route in impersonation.RESTRICTED_ROUTES:
        assert (method.lower(), route) in found or ("*", route) in found, (method, route)


@pytest.mark.parametrize("method,route", sorted(impersonation.RESTRICTED_ROUTES))
def test_restricted_routes_are_refused_while_impersonating(method, route, a, staff, api):
    client = api(staff)
    assert start(client, a["org"], a["users"]["admin"]).status_code == 201
    before = AuditEvent.objects.count()
    response = send(client.client, method, dummy(route))
    body = is_problem(response, 403, RESTRICTED_TYPE)
    assert "impersonating" in body["detail"]
    assert AuditEvent.objects.count() == before  # nothing ran
    assert client.get("platform/impersonation").json_body["active"] is True  # still impersonating


def test_the_restricted_routes_still_work_without_impersonation(a, api):
    """The refusal is the impersonation's: the member themselves can still change their password."""
    response = api(a["users"]["editor"]).post(
        "auth/password", {"current_password": PASSWORD, "new_password": "another-long-passphrase-9"}
    )
    assert response.status_code in (200, 204)


@pytest.mark.parametrize(
    "body",
    [
        {"sso_auto_provision": True},
        {"scim_role_groups": {"admin": "Everyone"}},
        {"scim_default_role": "admin"},
        {"timezone": "Europe/London", "sso_auto_provision": False},  # one sign-in key refuses the whole change
    ],
)
def test_sign_in_settings_of_the_organization_are_refused_while_impersonating(body, a, staff, api):
    """Organization settings' sign-in keys (auto-provisioning, SCIM roles) stay with the members: a support
    session that set them would outlive its 30 minutes. Refused before any capability check, nothing written."""
    from core.capabilities import set_edition

    set_edition(a["org"], "enterprise")  # so the SCIM keys would otherwise be accepted
    client = api(staff)
    start(client, a["org"], a["users"]["admin"])
    before = dict(Organization.objects.get(pk=a["org"].pk).settings or {})
    events = AuditEvent.objects.count()
    body_ = is_problem(client.patch("organization", body), 403, RESTRICTED_TYPE)
    assert "impersonating" in body_["detail"]
    org = Organization.objects.get(pk=a["org"].pk)
    assert (org.settings or {}) == before and org.timezone != "Europe/London"
    assert AuditEvent.objects.count() == events
    # Ordinary settings stay allowed, and the member's Admin can still change the sign-in ones.
    assert client.patch("organization", {"timezone": "Europe/London"}).status_code == 200
    assert api(a["users"]["admin"]).patch("organization", {"sso_auto_provision": True}).status_code == 200


def test_nested_impersonation_and_the_console_are_refused(a, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    is_problem(start(client, a["org"], a["users"]["viewer"]), 403, RESTRICTED_TYPE)
    is_problem(client.get("platform/organizations"), 403, RESTRICTED_TYPE)
    is_problem(client.post(f"platform/organizations/{a['org'].id}/suspend", {"reason": "x"}), 403, RESTRICTED_TYPE)
    assert client.get("platform/impersonation").status_code == 200
    assert client.get("platform/session").status_code == 200
    assert PlatformAuditEvent.objects.filter(action="impersonation.started").count() == 1


def test_staff_and_inactive_accounts_cannot_be_impersonated(a, staff, api):
    client = api(staff)
    other = make_staff("other@platform.test", org=a["org"])
    is_problem(start(client, a["org"], other), 403, RESTRICTED_TYPE)
    member = a["users"]["viewer"]
    member.is_active = False
    member.save()
    is_problem(start(client, a["org"], member), 403, RESTRICTED_TYPE)
    assert impersonation.SESSION_KEY not in session_values(client)


def test_a_suspended_organization_cannot_be_impersonated_into(a, staff, api):
    platform_admin.suspend(staff, a["org"], {"reason": "Unpaid"})
    is_problem(start(api(staff), a["org"], a["users"]["editor"]), 409)


def test_stopping_restores_exactly_the_staff_session(a, b, staff, api):
    Membership.objects.create(user=staff, organization=b["org"], role="viewer")
    client = api(staff)
    assert client.get("auth/me").json_body["organization"]["slug"] == "orgb"  # the staff's own current organization
    before = session_values(client)
    assert before[access.CURRENT_ORGANIZATION] == str(b["org"].id)
    start(client, a["org"], a["users"]["editor"])
    during = session_values(client)
    assert during[access.CURRENT_ORGANIZATION] == str(a["org"].id)
    assert access.AUTHENTICATED_AT not in during  # the member's session never counts as a fresh sign-in
    response = client.delete("platform/impersonation")
    assert response.status_code == 200 and response.json_body["active"] is False
    assert session_values(client) == before
    me = client.get("auth/me").json_body
    assert me["user"]["id"] == staff.id and me["organization"]["slug"] == "orgb" and me["impersonation"] is None
    stopped = AuditEvent.objects.get(organization=a["org"], action="impersonation.stopped")
    assert stopped.actor == staff and stopped.impersonated_by is None and stopped.changes["reason"] == "stopped"
    assert PlatformAuditEvent.objects.filter(action="impersonation.stopped").exists()
    is_problem(client.delete("platform/impersonation"), 409)


def test_expiry_ends_the_impersonation_on_the_next_request(a, staff, api):
    client = api(staff)
    member = a["users"]["editor"]
    start(client, a["org"], member)
    session = client.client.session
    state = session[impersonation.SESSION_KEY]
    state["expires_at"] = (timezone.now() - timedelta(seconds=1)).isoformat()
    session[impersonation.SESSION_KEY] = state
    session.save()
    body = client.get("platform/session").json_body
    assert body["user"]["id"] == staff.id and body["impersonation"] is None
    assert impersonation.SESSION_KEY not in session_values(client)
    assert client.get("platform/organizations").status_code == 200  # back to the console
    expired = AuditEvent.objects.get(organization=a["org"], action="impersonation.expired")
    assert expired.actor == staff and expired.changes["reason"] == "expired"
    assert PlatformAuditEvent.objects.filter(action="impersonation.expired").count() == 1


def test_losing_staff_or_the_member_ends_the_impersonation(a, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    platform_admin.revoke_staff(staff)
    client.get("auth/csrf")
    assert impersonation.SESSION_KEY not in session_values(client)
    assert PlatformAuditEvent.objects.get(action="impersonation.stopped").details["reason"] == "staff_unavailable"
    platform_admin.grant_staff(staff)
    start(client, a["org"], a["users"]["viewer"])
    Membership.objects.filter(user=a["users"]["viewer"], organization=a["org"]).delete()
    client.get("auth/csrf")
    assert impersonation.SESSION_KEY not in session_values(client)
    reasons = PlatformAuditEvent.objects.filter(action="impersonation.stopped").values_list(
        "details__reason", flat=True
    )
    assert set(reasons) == {"staff_unavailable", "member_unavailable"}


def test_signing_out_while_impersonating_audits_the_end(a, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    assert client.post("auth/logout").status_code == 204
    event = AuditEvent.objects.get(organization=a["org"], action="impersonation.stopped")
    assert event.changes["reason"] == "signed_out"
    assert client.get("platform/session").status_code == 403  # signed out altogether


# ---- no secrets -----------------------------------------------------------------------------------------------------


def test_platform_responses_carry_no_secrets(a, staff, override, api):
    from api.tokens import create_token
    from core import connections

    connections.create_connection(
        a["org"], a["users"]["admin"], connections.MICROSOFT,
        {"name": "Tenant", "tenant_id": "simulated", "secret": "synthetic-secret-value-123"},
    )  # fmt: skip
    admin = a["users"]["admin"]
    _token, token_secret = create_token(
        a["org"], admin, Membership.objects.get(user=admin), "Script", True, timezone.localdate() + timedelta(days=5)
    )
    Organization.objects.filter(pk=a["org"].pk).update(licence_key_ciphertext="gAAAA-not-a-real-ciphertext")
    usage.refresh_all(timezone.now())
    client = api(staff)
    member = a["users"]["editor"]
    start_body = start(client, a["org"], member).content.decode()
    client.delete("platform/impersonation")
    bodies = [start_body] + [
        client.client.get("/api/v1/" + url).content.decode()
        for url in (
            "platform/session",
            "platform/organizations",
            f"platform/organizations/{a['org'].id}",
            f"platform/organizations/{a['org'].id}/capability-overrides",
            "platform/audit-events",
            "platform/impersonation",
        )
    ]
    me = client.client.get("/api/v1/auth/me")
    assert me.status_code == 403  # no membership: the console only
    for body in bodies:
        assert offset_timestamps(json.loads(body)) == [], body  # timestamps in UTC with Z, as everywhere
        for secret in (
            "synthetic-secret-value-123",
            token_secret,
            "gAAAA",
            "ciphertext",
            "token_hash",
            "pbkdf2",
            "md5$",
        ):
            assert secret not in body, secret
        assert PASSWORD not in body


# ---- the platform audit stream is append-only ------------------------------------------------------------------------


def test_platform_audit_events_are_append_only(staff, a):
    event = platform_admin.platform_audit(staff, "organization.suspended", a["org"].pk, {"reason": "x"})
    with pytest.raises(AuditAppendOnly):
        event.save()
    with pytest.raises(AuditAppendOnly):
        PlatformAuditEvent.objects.all().update(action="x")
    with pytest.raises(AuditAppendOnly):
        PlatformAuditEvent.objects.all().delete()
    from django.db import connection

    for sql in (
        "UPDATE core_platformauditevent SET action = 'x'",
        "DELETE FROM core_platformauditevent",
    ):
        with pytest.raises(DatabaseError, match="append-only"), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql)
    with pytest.raises(ValueError, match="Unknown platform action"):
        platform_admin.platform_audit(staff, "organization.deleted", a["org"].pk)


def test_removing_a_staff_account_keeps_the_events_it_appears_on(a, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    client.post("applications", {"name": "Made in support", "lifecycle": "active"})
    client.delete("platform/impersonation")
    staff.delete()  # nulls actor and impersonated_by, which the triggers allow
    event = AuditEvent.objects.get(action="resource.created")
    assert event.impersonated_by_id is None and event.actor == a["users"]["editor"]
    assert PlatformAuditEvent.objects.filter(action="impersonation.started", actor__isnull=True).exists()
    assert PlatformAuditEvent.objects.get(action="impersonation.started").actor_email == "ops@platform.test"


def test_every_platform_action_written_is_labelled():
    root = Path(__file__).resolve().parent.parent
    written = set()
    for app in ("core", "catalog"):
        for path in (root / app).rglob("*.py"):
            if "migrations" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", None)) == "platform_audit"
                ):
                    arg = node.args[1]
                    assert isinstance(arg, ast.Constant), f"{path.name}:{node.lineno}: use a literal action"
                    written.add(arg.value)
    assert written and written <= set(platform_admin.PLATFORM_ACTIONS)


# ---- staff accounts are no organization's to control -------------------------------------------------------------


def test_a_tenant_admin_cannot_take_over_a_staff_member(a, api):
    from core.models import UserIdentity

    member = make_staff("ops@orga.test", org=a["org"], role="viewer")
    admin = api(a["users"]["admin"])
    for response in (
        admin.post(f"users/{member.id}/reset-password"),
        admin.patch(f"users/{member.id}", {"active": False}),
        admin.client.delete(f"/api/v1/users/{member.id}/identity"),
    ):
        body = is_problem(response, 409)
        assert "service operator" in body["detail"]
    identity = UserIdentity.objects.create(user=member, issuer="https://idp.example.test", subject="s1")
    is_problem(admin.client.delete(f"/api/v1/users/{member.id}/identities/{identity.id}"), 409)
    member.refresh_from_db()
    assert member.is_active and member.check_password(PASSWORD) and UserIdentity.objects.filter(user=member).exists()
    # The role in this organization is still the Admin's to change.
    assert admin.patch(f"users/{member.id}", {"role": "editor"}).status_code == 200
    rows = {m["id"]: m for m in admin.get("users").json_body}
    assert rows[member.id]["last_login"] is None  # shown as an account that isn't only this organization's


@pytest.fixture
def simulated_sso(settings):
    settings.DEBUG = True
    settings.SOFTWARDEN_BASE_URL = "http://testserver"


def test_single_sign_on_never_signs_in_staff(a, simulated_sso):
    from core.models import UserIdentity
    from tests.sso_helpers import activate, sign_in

    activate(a["org"], "entra")
    # By email: an IdP the organization's Admin controls asserts the staff account's address.
    member = make_staff("ops@orga.test", org=a["org"])
    client = Client()
    assert "error=platform_staff" in sign_in(client, member.email, query={"org": "orga"})["Location"]
    assert not UserIdentity.objects.filter(user=member).exists()
    assert client.get("/api/v1/platform/session").status_code == 403
    # By an identity pinned before the account became staff.
    editor = a["users"]["editor"]
    assert sign_in(Client(), editor.email, query={"org": "orga"})["Location"] == "/renewals"
    platform_admin.grant_staff(editor)
    assert "error=platform_staff" in sign_in(Client(), editor.email, query={"org": "orga"})["Location"]


def test_the_console_refuses_sso_sessions_and_temporary_passwords(a, api):
    member = make_staff("ops@orga.test", org=a["org"])
    client = api(member)
    assert client.get("platform/organizations").status_code == 200
    session = client.client.session
    session[access.SSO_ORGANIZATION] = str(a["org"].id)
    session.save()
    assert "single sign-on" in is_problem(client.get("platform/organizations"), 403)["detail"]
    session = client.client.session
    del session[access.SSO_ORGANIZATION]
    session.save()
    Membership.objects.filter(user=member).update(must_change_password=True)
    assert "password" in is_problem(client.get("platform/organizations"), 403)["detail"]


def test_staff_sign_ins_are_audited(a):
    member = make_staff("ops@orga.test", org=a["org"])
    loner = make_staff("solo@platform.test")
    for user, password in ((member, PASSWORD), (loner, PASSWORD), (loner, "wrong-password-1")):
        Client().post(
            "/api/v1/auth/login",
            json.dumps({"email": user.email, "password": password}),
            content_type="application/json",
        )
    events = PlatformAuditEvent.objects.filter(action__startswith="staff.sign")
    assert sorted((e.action, e.actor_id) for e in events) == sorted(
        [("staff.signed_in", member.id), ("staff.signed_in", loner.id), ("staff.sign_in_failed", None)]
    )
    failed = events.get(action="staff.sign_in_failed")  # nobody proved who they are: no actor, the account in details
    assert failed.details == {"user": loner.id} and failed.actor_email == ""
    Client().post(
        "/api/v1/auth/login",
        json.dumps({"email": a["users"]["editor"].email, "password": PASSWORD}),
        content_type="application/json",
    )
    assert events.count() == 3  # ordinary members aren't in the platform stream


# ---- impersonation is confined to its organization ------------------------------------------------------------------


def test_impersonation_is_confined_to_its_organization(a, b, staff, api):
    from catalog.models import Resource

    member = a["users"]["editor"]
    Membership.objects.create(user=member, organization=b["org"], role="admin")
    client = api(staff)
    start(client, a["org"], member)
    me = client.get("auth/me").json_body
    assert [o["id"] for o in me["organizations"]] == [str(a["org"].id)]
    is_problem(client.post("auth/me/organization", {"organization_id": str(b["org"].id)}), 404)
    # Even with the session pointed at the other organization, the write lands in the impersonated one.
    session = client.client.session
    session[access.CURRENT_ORGANIZATION] = str(b["org"].id)
    session.save()
    assert client.post("applications", {"name": "Support fix", "lifecycle": "active"}).status_code == 201
    assert Resource.objects.get(name="Support fix").organization_id == a["org"].id
    assert not AuditEvent.objects.filter(organization=b["org"], impersonated_by=staff).exists()


def test_a_write_on_the_request_that_notices_the_end_is_not_run(a, b, staff, api):
    from catalog.models import Resource

    Membership.objects.create(user=staff, organization=b["org"], role="admin")  # the staff account's own organization
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    session = client.client.session
    state = session[impersonation.SESSION_KEY]
    state["expires_at"] = (timezone.now() - timedelta(seconds=1)).isoformat()
    session[impersonation.SESSION_KEY] = state
    session.save()
    response = client.post("applications", {"name": "Meant for the member", "lifecycle": "active"})
    is_problem(response, 409, "/problems/impersonation-ended")
    assert not Resource.objects.filter(name="Meant for the member").exists()
    assert AuditEvent.objects.filter(organization=a["org"], action="impersonation.expired").exists()
    # The next request is the staff account's own, as usual.
    assert client.post("applications", {"name": "The staff account's own", "lifecycle": "active"}).status_code == 201
    assert Resource.objects.get(name="The staff account's own").organization_id == b["org"].id


# ---- the route policy is closed by default ----------------------------------------------------------------------------


def write_routes():
    found, _ = declarations()
    return {
        (method.upper(), route)
        for method, route in found
        if method not in ("get", "*") and route.startswith("api/v1/") and not route.startswith("api/v1/platform/")
    }


def test_every_write_route_is_allowed_or_restricted_exactly_once():
    from core.impersonation_routes import ALLOWED_WRITES, RESTRICTED_ROUTES

    writes = write_routes()
    restricted_writes = {pair for pair in RESTRICTED_ROUTES if pair[0] != "GET"}
    assert not ALLOWED_WRITES & RESTRICTED_ROUTES
    unplaced = writes - ALLOWED_WRITES - restricted_writes
    assert unplaced == set(), f"place these in core/impersonation_routes.py: {sorted(unplaced)}"
    assert ALLOWED_WRITES <= writes, sorted(ALLOWED_WRITES - writes)  # no stale entry


def test_an_undeclared_write_is_refused(a, staff, api, monkeypatch):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    monkeypatch.setattr(impersonation, "ALLOWED_WRITES", frozenset())
    is_problem(client.post("applications", {"name": "Undeclared", "lifecycle": "active"}), 403, RESTRICTED_TYPE)
    assert client.get("applications").status_code == 200  # reads stay open
    assert impersonation.is_restricted("POST", "api/v1/applications")
    assert not impersonation.is_restricted("POST", "api/v1/platform/impersonation")  # the platform permission's
    assert not impersonation.is_restricted("POST", "scim/v2/<slug:slug>/Users")  # not a session route


def test_head_counts_as_get_for_restricted_routes(a, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["admin"])
    for path in ("/api/v1/auth/oidc/callback", "/api/v1/audit-events/export.csv"):
        assert client.client.head(path).status_code == 403, path
    assert impersonation.is_restricted("HEAD", "api/v1/auth/oidc/callback")
    assert client.get("platform/impersonation").json_body["active"] is True


# ---- exports, schedules, jobs, the trigger ----------------------------------------------------------------------------


def test_audit_exports_carry_impersonated_by(a, staff, api):
    import csv

    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    client.post("applications", {"name": "Made in support", "lifecycle": "active"})
    client.delete("platform/impersonation")
    capabilities.set_edition(a["org"], "enterprise")  # the JSON lines export is audit.export
    admin = api(a["users"]["admin"]).client
    text = b"".join(admin.get("/api/v1/audit-events/export.csv").streaming_content).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    made = next(r for r in rows if r["action"] == "resource.created")
    assert made["impersonated_by_id"] == str(staff.id) and made["impersonated_by"] == staff.email
    assert made["actor"] == a["users"]["editor"].email
    started = next(r for r in rows if r["action"] == "impersonation.started")
    assert started["impersonated_by_id"] == "" and started["impersonated_by"] == ""
    body = b"".join(admin.get("/api/v1/audit-events/export.jsonl").streaming_content).decode()
    lines = [json.loads(line) for line in body.splitlines()]
    made = next(r for r in lines if r["action"] == "resource.created")
    assert made["impersonated_by"] == {"id": staff.id, "name": staff.email}
    assert next(r for r in lines if r["action"] == "impersonation.started")["impersonated_by"] is None


def test_per_organization_schedules_skip_suspended_organizations(a, b, staff, monkeypatch):
    """Every per-organization schedule loop, run for real with its per-organization step recorded: the suspended
    organization is skipped, the other one isn't."""
    from core.models import active_organizations
    from money import contracts, terms
    from notify import digest, reminders, rules, subscriptions

    platform_admin.suspend(staff, a["org"], {"reason": "Unpaid"})
    assert list(active_organizations()) == [Organization.objects.get(pk=b["org"].pk)]
    seen: dict[str, set] = {}

    def recorder(name, result):
        def record(org, *args, **kwargs):
            seen.setdefault(name, set()).add(org.pk)
            return result() if callable(result) else result

        return record

    monkeypatch.setattr(reminders, "generate", recorder("reminders", dict))
    monkeypatch.setattr(rules, "run_for_org", recorder("rules", 0))
    monkeypatch.setattr(digest, "run_for_org", recorder("digest", None))
    monkeypatch.setattr(subscriptions, "run_for_org", recorder("subscriptions", dict))
    monkeypatch.setattr(terms, "advance_terms", recorder("terms", dict))
    monkeypatch.setattr(contracts, "refresh_deadlines", recorder("deadlines", 0))
    now = timezone.now()
    reminders.scheduled_generate(now)
    rules.scheduled_run(now)
    digest.scheduled_build(now)
    subscriptions.scheduled_run(now)
    terms.scheduled_advance(now)
    assert seen == {
        name: {b["org"].pk} for name in ("reminders", "rules", "digest", "subscriptions", "terms", "deadlines")
    }


def test_the_webhook_outbox_holds_a_suspended_organizations_events(a, b, staff, monkeypatch):
    from notify import webhooks
    from notify.models import WebhookEvent

    WebhookEvent.objects.create(organization=a["org"], event_type="record.created", content={})
    WebhookEvent.objects.create(organization=b["org"], event_type="record.created", content={})
    platform_admin.suspend(staff, a["org"], {"reason": "Unpaid"})
    fanned: list = []
    monkeypatch.setattr(webhooks, "deliver", lambda org_id, events: fanned.append(org_id))
    webhooks.drain_once()
    assert fanned == [b["org"].pk]
    assert list(WebhookEvent.objects.values_list("organization_id", flat=True)) == [a["org"].pk]


def test_connector_runs_are_not_queued_for_a_suspended_organization(a, b, staff, monkeypatch):
    from discovery import engine
    from discovery.models import Connector

    now = timezone.now()
    for org in (a["org"], b["org"]):
        Connector.objects.create(organization=org, name="Entra", source="entra", run_requested_at=now)
    platform_admin.suspend(staff, a["org"], {"reason": "Unpaid"})
    queued: list = []

    def queue(connector, *args):
        queued.append(connector.organization_id)
        return 1

    monkeypatch.setattr(engine, "queue_run", queue)
    assert engine.due_runs(now) == 1
    assert queued == [b["org"].pk]
    # The suspended organization's request is kept for when it is unsuspended.
    assert Connector.objects.get(organization=a["org"]).run_requested_at is not None


def test_jobs_queued_while_impersonating_keep_impersonated_by(a, staff):
    from catalog import services
    from core.request_context import impersonating

    def write(job, ctx):
        services.audit(a["org"], None, "organization.updated", a["org"], {"name": "x"})
        return {}

    jobs.register_job("platform.test_job", write)
    try:
        with impersonating(staff.pk):
            job = jobs.enqueue("platform.test_job", {}, organization=a["org"])
        assert job.payload == {jobs.IMPERSONATED_BY: staff.pk}
        plain = jobs.enqueue("platform.test_job", {}, organization=a["org"])
        assert plain.payload == {}
        for queued in (job, plain):
            jobs.run(jobs.claim(ids=[queued.id]))
        events = AuditEvent.objects.filter(organization=a["org"], action="organization.updated").order_by("created_at")
        assert [e.impersonated_by_id for e in events] == [staff.pk, None]
    finally:
        jobs.JOB_KINDS.pop("platform.test_job", None)


def test_the_audit_trigger_refuses_changing_impersonated_by(a, staff, api):
    from django.db import connection

    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    client.post("applications", {"name": "Made in support", "lifecycle": "active"})
    event = AuditEvent.objects.get(action="resource.created")
    for sql, params in (
        ("UPDATE catalog_auditevent SET impersonated_by_id = %s WHERE id = %s", [a["users"]["admin"].pk, event.id]),
        ("UPDATE catalog_auditevent SET impersonated_by_id = NULL, action = 'x' WHERE id = %s", [event.id]),
        ("UPDATE catalog_auditevent SET impersonated_by_id = %s WHERE id = %s", [staff.pk, event.id]),  # unchanged
    ):
        with pytest.raises(DatabaseError, match="append-only"), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, params)
    with transaction.atomic(), connection.cursor() as cursor:  # nulling it alone is what removing the account does
        cursor.execute("UPDATE catalog_auditevent SET impersonated_by_id = NULL WHERE id = %s", [event.id])
    assert AuditEvent.objects.get(pk=event.pk).impersonated_by_id is None
