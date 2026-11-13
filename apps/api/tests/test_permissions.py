"""Table-driven role checks. ROLE_LEDGER names the minimum role of every method of every organization route, and
the tests below hold the declarations and the refusals to it; endpoints() proves each listed route works end to end
for the roles allowed. A new route gets a ROLE_LEDGER line, and an endpoints() entry where a working request is cheap
to build."""

from datetime import timedelta

import pytest

from catalog import assessments, services
from catalog.models import ChangeAssessment, Department, Team, Vendor
from core.models import AuditEvent, Membership
from money import renewals
from tests.conftest import digest_row, make_org


@pytest.fixture(autouse=True)
def email_on(settings):
    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


ALL = {"admin", "editor", "viewer"}
WRITERS = {"admin", "editor"}
ADMIN = {"admin"}


def endpoints(a):
    app, dana, team = a["app"].id, a["people"]["dana"].id, a["team"].id
    spare = Department.objects.create(organization=a["org"], name="Spare").id
    vendor = Vendor.objects.create(organization=a["org"], name="Zylker").id
    doomed_vendor = Vendor.objects.create(organization=a["org"], name="Doomed vendor").id
    doomed_team = Team.objects.create(organization=a["org"], name="Doomed team").id
    priya = a["people"]["priya"].id
    viewer = a["users"]["viewer"].id
    # Someone in orga and another organization, whom an Admin can remove from orga (removal keeps one membership).
    shared = make_org("shared")["users"]["viewer"]
    Membership.objects.create(user=shared, organization=a["org"], role="viewer")
    # Access and provisioning: an invitation, a pending access request and a pinned identity to act on.
    from django.utils import timezone

    from catalog import invitations
    from core.models import PendingAccess, UserIdentity

    held_invitation, _ = invitations.create_invitation(
        a["org"], a["users"]["admin"], {"email": "held@x.test", "role": "viewer"}
    )
    now = timezone.now()
    waiting = PendingAccess.objects.create(
        organization=a["org"], issuer="https://idp.example.test", subject="p1", email="wait@x.test", first_seen=now,
        last_seen=now,
    ).id  # fmt: skip
    declined = PendingAccess.objects.create(
        organization=a["org"], issuer="https://idp.example.test", subject="p2", email="no@x.test", first_seen=now,
        last_seen=now,
    ).id  # fmt: skip
    identity = UserIdentity.objects.create(user=a["users"]["viewer"], issuer="https://idp.example.test", subject="v").id
    component = services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Existing job", "kind": "component", "lifecycle": "active"}
    ).id
    instance = services.create_instance(a["org"], a["users"]["editor"], a["app"], {"name": "Existing instance"}).id
    review = {"confirmed_by": str(dana), "evidence": "Email from Dana"}
    confirmation = {"confirmed_by": str(dana), "evidence": "Confirmed by email"}
    doomed_app = services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Doomed app", "lifecycle": "active"}
    ).id
    doomed_job = services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Doomed job", "kind": "component", "lifecycle": "active"}
    ).id
    job_link = services.create_relationship(
        a["org"], a["users"]["editor"], {"type": "depends_on", "source": str(app), "target": str(component)}
    ).id
    doomed_link = services.create_relationship(
        a["org"], a["users"]["editor"], {"type": "depends_on", "source": str(component), "target": str(app)}
    ).id
    linked_app = services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Linked app", "lifecycle": "active"}
    ).id

    def feed(name):
        return services.create_integration(
            a["org"],
            a["users"]["editor"],
            {"name": name, "lifecycle": "active", "source": str(linked_app), "target": str(component)},
        ).id

    held = ChangeAssessment.objects.create(
        organization=a["org"], target_id=component, change_type="change", title="Existing", owner=a["users"]["editor"]
    )
    snapshot = assessments.write_snapshot(a["org"], a["users"]["editor"], held)
    feed_id, archived_feed, doomed_feed = feed("Existing feed"), feed("Archivable feed"), feed("Doomed feed")

    def renewal(name, done=False):
        obligation = renewals.create_renewal(a["org"], a["users"]["editor"], {"name": name, "auto_renew": "yes"})
        cycle = obligation.cycles.get()
        if done:
            renewals.record_decision(a["org"], a["users"]["editor"], obligation, cycle.id, {"decision": "renew"})
            renewals.record_completion(
                a["org"],
                a["users"]["editor"],
                obligation,
                cycle.id,
                {"completed_on": services.org_today(a["org"]).isoformat(), "completion_evidence": "PO 1"},
            )
        return obligation.id, cycle.id

    held_renewal, held_cycle = renewal("Held renewal")
    decided_renewal, decided_cycle = renewal("Decided renewal")
    completed_renewal, _ = renewal("Completed renewal", done=True)
    doomed_renewal, _ = renewal("Doomed renewal")
    completion = {"completed_on": services.org_today(a["org"]).isoformat(), "completion_evidence": "PO 2"}
    snooze = {
        "subject_type": "ownership_gaps",
        "subject_id": str(app),
        "kind": "snooze",
        "until": (services.org_today(a["org"]) + timedelta(days=3)).isoformat(),
    }
    failed_digest = digest_row(a["org"], a["users"]["editor"], "failed", attempts=4).id
    from money import contracts

    held_contract = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Held contract"}).id
    held_event = AuditEvent.objects.get(action="contract.created", target_id=held_contract).id
    doomed_contract = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Doomed contract"}).id
    lined = contracts.create_contract(a["org"], a["users"]["editor"], {"name": "Lined contract"})
    line = contracts.add_item(a["org"], a["users"]["editor"], lined, {"record": str(app)}).id
    doomed_line = contracts.add_item(a["org"], a["users"]["editor"], lined, {"record": str(component)}).id
    pool = contracts.create_pool(a["org"], a["users"]["editor"], {"application": str(app), "sku_name": "E5"}).id
    doomed_pool = contracts.create_pool(a["org"], a["users"]["editor"], {"application": str(app), "sku_name": "E3"}).id
    renewable = contracts.create_contract(
        a["org"], a["users"]["editor"], {"name": "Renewable", "end_date": "2027-03-31"}
    ).id
    reviewed = {}
    for sku in ("sku-apply", "sku-dismiss"):  # a pool and a synthetic licence observation that disagrees with it
        found = contracts.create_pool(
            a["org"], a["users"]["editor"], {"application": str(app), "sku_name": sku, "sku_id": sku}
        ).id
        reviewed[sku] = (found, _licence(a["org"], sku).id)
    from core import connections

    def connection(name):
        data = {"name": name, "tenant_id": "simulated", "secret": "synthetic-secret"}
        return connections.create_connection(a["org"], a["users"]["admin"], connections.MICROSOFT, data).id

    held_connection, doomed_connection = connection("Held connection"), connection("Doomed connection")
    return [
        # (method, url, payload, roles allowed)
        # Connections (Admin only; Halo is enterprise and covered in tests/test_connections.py)
        ("get", "connections/microsoft", None, ADMIN),
        ("post", "connections/microsoft", {"name": "New", "tenant_id": "simulated", "secret": "s"}, ADMIN),
        ("get", f"connections/microsoft/{held_connection}", None, ADMIN),
        ("patch", f"connections/microsoft/{held_connection}", {"notes": "Checked"}, ADMIN),
        ("post", f"connections/microsoft/{held_connection}/test", None, ADMIN),
        ("delete", f"connections/microsoft/{doomed_connection}", None, ADMIN),
        # Single sign-on settings (Admin only; test and activate in tests/test_sso.py)
        ("get", "sso/settings", None, ADMIN),
        ("patch", "sso/settings", {"provider": "okta"}, ADMIN),
        ("post", "sso/settings/disable", None, ADMIN),
        # Renewal and licences
        ("get", f"contracts/{renewable}/renew", None, ALL),
        ("post", f"contracts/{renewable}/renew", {"decision": "renew"}, WRITERS),
        ("get", "license-pools/totals", None, ALL),
        ("get", "license-pools/seat-review", None, ALL),
        ("post", "license-pools/{}/seat-review/{}/apply".format(*reviewed["sku-apply"]), {}, WRITERS),
        ("post", "license-pools/{}/seat-review/{}/dismiss".format(*reviewed["sku-dismiss"]), {"reason": "x"}, WRITERS),
        # Contracts and licence pools
        ("get", "contracts", None, ALL),
        ("get", "contracts?status=active&end_before=2030-01-01&archived=all", None, ALL),
        ("get", "contracts/preview?amount=100&billing_frequency=monthly", None, ALL),
        ("post", "contracts", {"name": "New contract", "amount": "10"}, WRITERS),
        ("get", f"contracts/{held_contract}", None, ALL),
        ("patch", f"contracts/{held_contract}", {"notes": "Checked"}, WRITERS),
        ("post", f"contracts/{held_contract}/archive", {}, WRITERS),
        ("post", f"contracts/{held_contract}/restore", {}, WRITERS),
        ("delete", f"contracts/{doomed_contract}", None, ADMIN),
        ("get", f"contracts/{lined.id}/lines", None, ALL),
        ("post", f"contracts/{held_contract}/lines", {"record": str(app)}, WRITERS),
        ("get", f"contracts/{lined.id}/lines/{line}", None, ALL),
        ("patch", f"contracts/{lined.id}/lines/{line}", {"allocation_percent": "10"}, WRITERS),
        ("delete", f"contracts/{lined.id}/lines/{doomed_line}", None, WRITERS),
        ("get", f"contracts/{held_contract}/attachments", None, ALL),
        ("post", f"contracts/{held_contract}/attachments", {"url": "https://example.com/msa"}, WRITERS),
        ("get", f"contracts/{held_contract}/audit", None, ALL),
        ("get", f"contracts/{held_contract}/audit/{held_event}", None, ALL),
        ("get", "contracts/export.csv", None, ALL),
        ("get", "contracts/export.xlsx?status=active", None, ALL),
        ("get", "license-pools", None, ALL),
        ("post", "license-pools", {"application": str(app), "sku_name": "New SKU"}, WRITERS),
        ("get", f"license-pools/{pool}", None, ALL),
        ("patch", f"license-pools/{pool}", {"seats_purchased": 5}, WRITERS),
        ("delete", f"license-pools/{doomed_pool}", None, ADMIN),
        ("get", f"license-pools/{pool}/audit", None, ALL),
        ("get", "auth/me", None, ALL),
        ("post", "auth/me/organization", {"organization_id": str(a["org"].id)}, ALL),
        ("get", "people", None, ALL),
        ("post", "people", {"name": "New Person"}, WRITERS),
        ("patch", f"people/{priya}", {"email": "priya@example.test"}, WRITERS),
        ("get", f"people/{priya}", None, ALL),
        ("post", f"people/{priya}/anonymize", {"confirm_name": "Priya Raman"}, ADMIN),
        ("get", "teams", None, ALL),
        ("post", "teams", {"name": "Network"}, WRITERS),
        ("patch", f"teams/{team}", {"name": "IT Apps"}, WRITERS),
        ("get", "departments", None, ALL),
        ("post", "departments", {"name": "Legal"}, WRITERS),
        ("patch", f"departments/{spare}", {"name": "Spare desks"}, WRITERS),
        ("delete", f"departments/{spare}", None, WRITERS),
        ("get", "vendors", None, ALL),
        ("post", "vendors", {"name": "Contoso Labs"}, WRITERS),
        ("patch", f"vendors/{vendor}", {"website": "https://zylker.example"}, WRITERS),
        ("delete", f"vendors/{doomed_vendor}", None, WRITERS),
        ("delete", f"teams/{doomed_team}", None, WRITERS),
        ("get", "stewards", None, ALL),
        ("get", "organization", None, ALL),
        ("get", "organization/limits", None, ALL),  # limits and usage (core/limits.py)
        ("patch", "organization", {"review_interval_days": 120}, ADMIN),
        ("get", "users", None, ADMIN),
        ("post", "users", {"email": "new@orga.test", "role": "viewer"}, ADMIN),
        ("patch", f"users/{viewer}", {"role": "viewer"}, ADMIN),
        ("post", f"users/{viewer}/reset-password", {}, ADMIN),
        ("delete", f"users/{shared.id}", None, ADMIN),
        ("delete", f"users/{viewer}/identities/{identity}", None, ADMIN),
        # Invitations and pending access (Admin only; the link's own page is public, see PUBLIC)
        ("get", "invitations", None, ADMIN),
        ("get", "invitations?status=pending", None, ADMIN),
        ("post", "invitations", {"email": "invited@x.test", "role": "editor"}, ADMIN),
        ("get", f"invitations/{held_invitation.id}", None, ADMIN),
        ("post", f"invitations/{held_invitation.id}/resend", {}, ADMIN),
        ("post", f"invitations/{held_invitation.id}/revoke", {}, ADMIN),
        # The organization's data export and its deletion state (Admin only)
        ("get", "organization/exports", None, ADMIN),
        ("post", "organization/exports", {}, ADMIN),
        ("get", "organization/deletion", None, ADMIN),
        ("get", "pending-access", None, ADMIN),
        ("get", f"pending-access/{waiting}", None, ADMIN),
        ("post", f"pending-access/{waiting}/approve", {"role": "viewer"}, ADMIN),
        ("delete", f"pending-access/{declined}", None, ADMIN),
        ("get", "applications", None, ALL),
        ("post", "applications", {"name": "X", "lifecycle": "active"}, WRITERS),
        ("get", "applications?lifecycle=active&q=App&gap=true", None, ALL),
        ("get", f"applications/{app}", None, ALL),
        ("patch", f"applications/{app}", {"support_team": str(team)}, WRITERS),
        ("get", f"applications/{app}/reviews", None, ALL),
        ("post", f"applications/{app}/reviews", review, WRITERS),
        ("get", f"applications/{app}/instances", None, ALL),
        ("post", f"applications/{app}/instances", {"name": "Sandbox", "environment": "Sandbox"}, WRITERS),
        ("get", f"applications/{app}/instances/{instance}", None, ALL),
        ("patch", f"applications/{app}/instances/{instance}", {"status": "degraded"}, WRITERS),
        ("delete", f"applications/{app}/instances/{instance}", None, WRITERS),
        ("get", "components", None, ALL),
        ("post", "components", {"name": "Nightly job", "lifecycle": "active", "component_type": "script"}, WRITERS),
        ("get", f"components/{component}", None, ALL),
        ("patch", f"components/{component}", {"host_runtime": "cron"}, WRITERS),
        ("get", f"components/{component}/reviews", None, ALL),
        ("post", f"components/{component}/reviews", review, WRITERS),
        ("post", f"components/{component}/archive", {}, WRITERS),
        ("post", f"components/{component}/restore", {}, WRITERS),
        ("delete", f"components/{doomed_job}", None, ADMIN),
        ("post", f"applications/{app}/archive", {}, WRITERS),
        ("post", f"applications/{app}/restore", {}, WRITERS),
        ("delete", f"applications/{doomed_app}", None, ADMIN),
        ("get", "relationships", None, ALL),
        ("get", f"relationships?resource={app}&type=depends_on", None, ALL),
        ("post", "relationships", {"type": "depends_on", "source": str(linked_app), "target": str(app)}, WRITERS),
        ("get", f"relationships/{job_link}", None, ALL),
        ("patch", f"relationships/{job_link}", {"purpose": "Runs nightly"}, WRITERS),
        ("delete", f"relationships/{doomed_link}", None, WRITERS),
        ("get", f"relationships/{job_link}/confirmations", None, ALL),
        ("post", f"relationships/{job_link}/confirmations", confirmation, WRITERS),
        ("get", f"dependencies?target={app}", None, ALL),
        ("get", f"dependencies?target={component}&direction=both&depth=all&show_instances=true", None, ALL),
        ("get", f"applications/{app}/dependencies?depth=3&direction=both", None, ALL),
        ("get", f"components/{component}/dependencies?depth=all", None, ALL),
        ("get", f"applications/{app}/map", None, ALL),
        ("get", f"components/{component}/map?depth=2", None, ALL),
        ("get", "integrations", None, ALL),
        ("get", f"integrations?source={linked_app}&implemented=false&protocol=SFTP&q=feed", None, ALL),
        (
            "post",
            "integrations",
            {"name": "New feed", "lifecycle": "active", "source": str(app), "target": str(component)},
            WRITERS,
        ),
        ("get", f"integrations/{feed_id}", None, ALL),
        ("patch", f"integrations/{feed_id}", {"protocol": "REST"}, WRITERS),
        ("post", f"integrations/{archived_feed}/archive", {}, WRITERS),
        ("post", f"integrations/{archived_feed}/restore", {}, WRITERS),
        ("delete", f"integrations/{doomed_feed}", None, ADMIN),
        ("get", f"integrations/{feed_id}/confirmations", None, ALL),
        ("post", f"integrations/{feed_id}/confirmations", confirmation, WRITERS),
        ("get", "assessments", None, ALL),
        ("get", "assessments?status=draft,in_review&stale=false&q=exist", None, ALL),
        ("post", "assessments", {"target": str(app), "change_type": "change", "title": "New one"}, WRITERS),
        ("get", f"assessments/{held.id}", None, ALL),
        ("patch", f"assessments/{held.id}", {"findings": "Checked"}, WRITERS),
        ("post", f"assessments/{held.id}/status", {"status": "in_review"}, WRITERS),
        ("post", f"assessments/{held.id}/refresh", None, WRITERS),
        ("get", f"assessments/{held.id}/snapshots", None, ALL),
        ("get", f"assessments/{held.id}/snapshots/{snapshot.id}", None, ALL),
        ("get", "renewals", None, ALL),
        ("get", "renewals?due_within=30&has_gaps=true&mine=true&q=held", None, ALL),
        ("get", "renewals/preview?renewal_date=2027-03-31&notice_value=2&notice_unit=months", None, ALL),
        ("post", "renewals", {"name": "New renewal", "auto_renew": "no"}, WRITERS),
        ("get", f"renewals/{held_renewal}", None, ALL),
        ("patch", f"renewals/{held_renewal}", {"auto_renew": "no"}, WRITERS),
        ("patch", f"renewals/{held_renewal}/cycles/{held_cycle}", {"renewal_date": "2027-03-31"}, WRITERS),
        ("post", f"renewals/{held_renewal}/cycles/{held_cycle}/decision", {"decision": "renew"}, WRITERS),
        ("post", f"renewals/{decided_renewal}/cycles/{decided_cycle}/decision", {"decision": "cancel"}, WRITERS),
        ("post", f"renewals/{held_renewal}/archive", {}, WRITERS),
        ("post", f"renewals/{held_renewal}/restore", {}, WRITERS),
        ("delete", f"renewals/{doomed_renewal}", None, ADMIN),
        ("post", f"renewals/{decided_renewal}/cycles/{decided_cycle}/completion", completion, WRITERS),
        ("post", f"renewals/{completed_renewal}/cycles", {}, WRITERS),
        ("get", f"renewals/{held_renewal}/audit", None, ALL),
        ("get", "auth/me/preferences", None, ALL),
        ("patch", "auth/me/preferences", {"digest_enabled": False}, ALL),
        ("post", "needs-attention/snoozes", snooze, WRITERS),
        ("get", "admin/digests", None, ADMIN),
        ("post", f"admin/digests/{failed_digest}/retry", None, ADMIN),
        ("get", "needs-attention?scope=all", None, ALL),
        ("get", "search?q=App", None, ALL),
        ("get", "search?q=App&kinds=application,vendor&limit=5&include_archived=true", None, ALL),
        # Composite read endpoints (api/composite_views.py)
        ("get", "composite/search?q=App", None, ALL),
        ("get", "composite/deadlines?within_days=90", None, ALL),
        ("get", "composite/renewals?within_days=90", None, ALL),
        ("get", "composite/spend?by=department", None, ALL),
        ("get", f"composite/dependencies?target={app}", None, ALL),
        ("get", "composite/discovery-summary", None, ALL),
        ("get", "composite/data-gaps?rule=business_owner", None, ALL),
        ("get", "composite/expiring-credentials?within_days=30", None, ALL),
        ("get", f"applications/{app}/audit", None, ALL),
        ("get", f"components/{component}/audit", None, ALL),
        ("get", "audit-events", None, ADMIN),
        ("get", "audit-events/export.csv?action=resource", None, ADMIN),
        ("get", "system", None, ADMIN),
    ]


def _licence(org, sku):
    from django.utils import timezone

    from discovery.models import Connector, Observation

    connector = Connector.objects.get_or_create(organization=org, name="Licences", defaults={"source": "entra"})[0]
    now = timezone.now()
    return Observation.objects.create(
        organization=org,
        connector=connector,
        kind="license",
        external_id=sku,
        display_name=sku,
        data={"sku_id": sku, "seats_purchased": 5, "seats_assigned": 4},
        fingerprint=sku,
        first_seen_at=now,
        last_seen_at=now,
        last_changed_at=now,
    )


def call(client, method, url, payload):
    return getattr(client, method)(url, payload) if payload is not None else getattr(client, method)(url)


@pytest.mark.parametrize("role", ["admin", "editor", "viewer"])
def test_role_matrix(a, api, role):
    client = api(a["users"][role])
    for method, url, payload, allowed in endpoints(a):
        response = call(client, method, url, payload)
        if role in allowed:
            assert response.status_code in (200, 201, 204), (role, method, url, response.json_body)
        else:
            assert response.status_code == 403, (role, method, url, response.status_code)
            assert response["Content-Type"] == "application/problem+json"


def test_anonymous_is_refused_everywhere(a, api):
    client = api()
    for method, url, payload, _ in endpoints(a):
        assert call(client, method, url, payload).status_code in (401, 403), (method, url)


# Endpoints that answer without signing in, on purpose (minimum role: public). Everything else is in endpoints().
# tests/test_url_coverage.py checks this list against the routes that declare `public`, so adding a public route
# means adding it here. A leading "/" is a path outside /api/v1/ (probes and the SPA).
PUBLIC = [
    ("get", "health"),
    ("get", "auth/csrf"),
    ("get", "auth/options"),
    ("post", "auth/login"),
    ("get", "auth/oidc/start"),
    ("get", "auth/oidc/callback"),
    ("get", "auth/oidc/simulator"),  # 404 unless the OIDC simulator is configured (local testing only)
    ("get", "setup"),
    ("post", "setup"),
    ("post", "auth/invitations/preview"),  # the invitation page: 404 unless the body's token is a live invitation
    ("post", "auth/invitations/accept"),
    (
        "post",
        "auth/signup",
    ),  # public signup: 404 /problems/signup-disabled unless the installation turns it on
    ("post", "auth/signup/resend"),
    ("post", "auth/signup/verify"),
    ("post", "auth/password-reset"),  # self-service reset: 503 /problems/password-reset-unavailable without email
    ("post", "auth/password-reset/confirm"),
    ("get", "calendar/renewals.ics"),  # the renewals calendar feed: authenticated by its secret only
    ("get", "/healthz"),
    ("get", "/readyz"),
    ("get", "/readyz/web"),
    ("get", "/"),  # the SPA's index.html for client-side routes
]


# The operator health view (softwarden/ops.py): a plain Django view, so `public` to the role machinery, but it
# answers only the SOFTWARDEN_OPS_TOKEN bearer or an Admin session and is a 404 to everyone else
# (tests/test_observability.py). tests/test_url_coverage.py accepts it beside PUBLIC.
OPS_TOKEN_ROUTES = [("get", "/healthz/ops")]

# The platform admin console (catalog/platform_views.py): every route under /api/v1/platform/ declares
# `platform` (platform staff with a session, never an API token). tests/test_url_coverage.py checks this list against
# the routes, and tests/test_platform.py sends each one as nobody, a tenant Admin, a staff API token and staff.
# {org} and {override} are filled in by the test.
PLATFORM_ROUTES = [
    ("get", "platform/session"),
    ("get", "platform/second-factor"),
    ("post", "platform/second-factor/enrollment"),
    ("post", "platform/second-factor/verify"),
    ("get", "platform/organizations"),
    ("get", "platform/organizations/{org}"),
    ("post", "platform/organizations/{org}/edition"),
    ("post", "platform/organizations/{org}/cancel-deletion"),
    ("post", "platform/organizations/{org}/suspend"),
    ("post", "platform/organizations/{org}/unsuspend"),
    ("get", "platform/organizations/{org}/capability-overrides"),
    ("post", "platform/organizations/{org}/capability-overrides"),
    ("post", "platform/organizations/{org}/capability-overrides/{override}/remove"),
    ("get", "platform/organizations/{org}/limits"),
    ("post", "platform/organizations/{org}/limits"),
    ("post", "platform/organizations/{org}/limits/records/remove"),
    ("post", "platform/organizations/{org}/exports"),
    ("get", "platform/sign-in-domains"),
    ("post", "platform/sign-in-domains/move"),
    ("post", "platform/sign-in-domains/release"),
    ("get", "platform/audit-events"),
    ("get", "platform/capabilities"),
    ("get", "platform/system"),
    ("post", "platform/system/backup/acknowledge-key"),
    ("get", "platform/impersonation"),
    ("post", "platform/impersonation"),
    ("delete", "platform/impersonation"),
]


def test_ops_routes_are_hidden_without_their_token(a, api):
    for user in (None, a["users"]["editor"], a["users"]["viewer"]):
        client = api(user)
        for method, path in OPS_TOKEN_ROUTES:
            assert getattr(client.client, method)(path).status_code == 404, (user, path)


def test_public_endpoints_answer_anonymous_requests(a, api):
    """Public routes never answer 401/403 for lack of a session: sign-in refuses bad credentials and setup is closed
    (409) once an organization exists, which the fixture's does."""
    client = api()
    for method, url in PUBLIC:
        path = url if url.startswith("/") else "/api/v1/" + url
        response = getattr(client.client, method)(path, content_type="application/json")  # some answer HTML
        assert response.status_code not in (401, 403) or url == "auth/login", (method, url, response.status_code)
    assert client.get("setup").json_body == {"pending": False}
    assert client.post("setup", {"token": "x"}).status_code == 409


def test_session_writes_require_csrf(a, api):
    client = api(a["users"]["editor"], enforce_csrf=True)
    response = client.post("applications", {"name": "No token", "lifecycle": "active"})
    assert response.status_code == 403


def test_login_and_wrong_password(a, api):
    from django.core.cache import cache

    cache.clear()  # the public sweep above spends the sign-in throttle
    client = api()
    bad = client.post("auth/login", {"email": "editor@orga.test", "password": "wrong-password-here"})
    assert bad.status_code in (401, 403)
    good = client.post("auth/login", {"email": "editor@orga.test", "password": "correct-horse-battery"})
    assert good.status_code == 200 and good.json_body["role"] == "editor"
    assert client.get("auth/me").json_body["organization"]["name"] == "orga"


# ---- the role ledger: every route, every method ---------------------------------------------------------------

# The minimum role of every method of every route that acts in an organization (under /api/v1/; the public,
# operator and platform routes are the lists above). endpoints() proves a role works end to end for the routes it
# lists; this proves every route declares the role intended for it, and that the role below it is refused. The roles
# were read route by route against what each does when the ledger was written: a route's role changes only by
# changing its line here, and a new route fails test_every_route_has_its_ledger_role until it has one.
# The second column is the route's capability decision, made explicitly for every route: the capabilities it refuses an
# organization without, before it reads anything ("-": none; "a+b": either missing is refused). A capability checked
# only once the record is read (an AI agent's attachments need ai_agents after the agent is found) isn't in this column;
# its own tests cover it. tests/test_capabilities.py holds every route to this column both ways: a declared capability
# the column lacks fails, and every capability in it is asked for as an organization without it.
# Columns: minimum role, capabilities, method, route under /api/v1/ (as the resolver writes it).
ROLE_LEDGER = """
admin   -                             GET    admin/digests
admin   -                             GET    admin/digests/<uuid:pk>
admin   -                             POST   admin/digests/<uuid:pk>/retry
viewer  ai_agents                     GET    ai-agents
editor  ai_agents                     POST   ai-agents
admin   ai_agents                     DELETE ai-agents/<uuid:pk>
viewer  ai_agents                     GET    ai-agents/<uuid:pk>
editor  ai_agents                     PATCH  ai-agents/<uuid:pk>
editor  ai_agents                     POST   ai-agents/<uuid:pk>/archive
viewer  attachments                   GET    ai-agents/<uuid:pk>/attachments
editor  attachments                   POST   ai-agents/<uuid:pk>/attachments
viewer  ai_agents                     GET    ai-agents/<uuid:pk>/audit
viewer  ai_agents                     GET    ai-agents/<uuid:pk>/audit/<uuid:event_pk>
viewer  ai_agents                     GET    ai-agents/<uuid:pk>/dependencies
viewer  ai_agents                     GET    ai-agents/<uuid:pk>/discovery-sources
viewer  ai_agents                     GET    ai-agents/<uuid:pk>/map
editor  ai_agents                     POST   ai-agents/<uuid:pk>/restore
viewer  ai_agents                     GET    ai-agents/<uuid:pk>/reviews
editor  ai_agents                     POST   ai-agents/<uuid:pk>/reviews
viewer  ai_agents                     GET    ai-agents/<uuid:pk>/reviews/<uuid:review_pk>
viewer  ai_agents                     GET    ai-agents/export.csv
viewer  ai_agents                     GET    ai-agents/export.xlsx
viewer  ai_agents                     GET    ai-models
editor  ai_agents                     POST   ai-models
editor  ai_agents                     DELETE ai-models/<uuid:pk>
viewer  ai_agents                     GET    ai-models/<uuid:pk>
editor  ai_agents                     PATCH  ai-models/<uuid:pk>
viewer  ai_agents                     GET    ai-models/in-use
viewer  ai_agents                     GET    ai-providers
editor  ai_agents                     POST   ai-providers
editor  ai_agents                     DELETE ai-providers/<uuid:pk>
viewer  ai_agents                     GET    ai-providers/<uuid:pk>
editor  ai_agents                     PATCH  ai-providers/<uuid:pk>
admin   -                             GET    api-tokens
admin   -                             POST   api-tokens
admin   -                             GET    api-tokens/<uuid:pk>
admin   -                             POST   api-tokens/<uuid:pk>/revoke
viewer  -                             GET    applications
editor  -                             POST   applications
admin   -                             DELETE applications/<uuid:pk>
viewer  -                             GET    applications/<uuid:pk>
editor  -                             PATCH  applications/<uuid:pk>
editor  -                             POST   applications/<uuid:pk>/archive
viewer  attachments                   GET    applications/<uuid:pk>/attachments
editor  attachments                   POST   applications/<uuid:pk>/attachments
viewer  -                             GET    applications/<uuid:pk>/audit
viewer  -                             GET    applications/<uuid:pk>/audit/<uuid:event_pk>
viewer  -                             GET    applications/<uuid:pk>/dependencies
viewer  -                             GET    applications/<uuid:pk>/discovery-sources
editor  eol                           DELETE applications/<uuid:pk>/end-of-life
viewer  eol                           GET    applications/<uuid:pk>/end-of-life
editor  eol                           PATCH  applications/<uuid:pk>/end-of-life
viewer  -                             GET    applications/<uuid:pk>/instances
editor  -                             POST   applications/<uuid:pk>/instances
editor  -                             DELETE applications/<uuid:pk>/instances/<uuid:instance_pk>
viewer  -                             GET    applications/<uuid:pk>/instances/<uuid:instance_pk>
editor  -                             PATCH  applications/<uuid:pk>/instances/<uuid:instance_pk>
editor  eol                           DELETE applications/<uuid:pk>/instances/<uuid:instance_pk>/end-of-life
viewer  eol                           GET    applications/<uuid:pk>/instances/<uuid:instance_pk>/end-of-life
editor  eol                           PATCH  applications/<uuid:pk>/instances/<uuid:instance_pk>/end-of-life
viewer  -                             GET    applications/<uuid:pk>/map
editor  -                             POST   applications/<uuid:pk>/restore
viewer  -                             GET    applications/<uuid:pk>/reviews
editor  -                             POST   applications/<uuid:pk>/reviews
viewer  -                             GET    applications/<uuid:pk>/reviews/<uuid:review_pk>
viewer  -                             GET    applications/export.csv
viewer  -                             GET    applications/export.xlsx
editor  -                             GET    applications/template.csv
viewer  -                             GET    assessments
editor  -                             POST   assessments
viewer  -                             GET    assessments/<uuid:pk>
editor  -                             PATCH  assessments/<uuid:pk>
editor  -                             POST   assessments/<uuid:pk>/refresh
viewer  -                             GET    assessments/<uuid:pk>/snapshots
viewer  -                             GET    assessments/<uuid:pk>/snapshots/<uuid:snapshot_pk>
editor  -                             POST   assessments/<uuid:pk>/status
viewer  ticket_links                  GET    assessments/<uuid:pk>/ticket-links
editor  ticket_links                  POST   assessments/<uuid:pk>/ticket-links
editor  ticket_links                  DELETE assessments/<uuid:pk>/ticket-links/<uuid:link_pk>
viewer  ticket_links                  GET    assessments/<uuid:pk>/ticket-links/<uuid:link_pk>
editor  ticket_links                  PATCH  assessments/<uuid:pk>/ticket-links/<uuid:link_pk>
editor  ticket_links                  POST   assessments/<uuid:pk>/ticket-links/<uuid:link_pk>/refresh
editor  ticket_links                  POST   assessments/<uuid:pk>/ticket-links/create-ticket
viewer  assistant                     GET    assistant/batches
editor  assistant                     POST   assistant/batches
viewer  assistant                     GET    assistant/batches/<uuid:pk>
admin   assistant                     GET    assistant/settings
admin   assistant                     PATCH  assistant/settings
admin   assistant                     GET    assistant/usage
editor  attachments                   DELETE attachments/<uuid:pk>
viewer  attachments                   GET    attachments/<uuid:pk>
editor  attachments                   PATCH  attachments/<uuid:pk>
viewer  attachments                   GET    attachments/<uuid:pk>/download
admin   -                             GET    audit-events
admin   -                             GET    audit-events/<uuid:pk>
admin   audit.log                     GET    audit-events/export.csv
admin   audit.export                  GET    audit-events/export.jsonl
account -                             POST   auth/logout
account -                             GET    auth/me
viewer  -                             POST   auth/me/organization
viewer  -                             GET    auth/me/preferences
viewer  -                             PATCH  auth/me/preferences
account -                             POST   auth/password
admin   -                             POST   bulk-delete/apply
admin   -                             POST   bulk-delete/preview
editor  -                             POST   bulk-edit/apply
editor  -                             POST   bulk-edit/preview
viewer  -                             DELETE calendar-feed
viewer  -                             GET    calendar-feed
viewer  -                             POST   calendar-feed
viewer  -                             POST   calendar-feed/rotate
admin   catalog_code                  POST   catalog-code/apply
admin   catalog_code                  GET    catalog-code/export.zip
admin   catalog_code                  POST   catalog-code/plan
viewer  -                             GET    categories
editor  -                             POST   categories
editor  -                             DELETE categories/<uuid:pk>
viewer  -                             GET    categories/<uuid:pk>
editor  -                             PATCH  categories/<uuid:pk>
viewer  -                             GET    components
editor  -                             POST   components
admin   -                             DELETE components/<uuid:pk>
viewer  -                             GET    components/<uuid:pk>
editor  -                             PATCH  components/<uuid:pk>
editor  -                             POST   components/<uuid:pk>/archive
viewer  attachments                   GET    components/<uuid:pk>/attachments
editor  attachments                   POST   components/<uuid:pk>/attachments
viewer  -                             GET    components/<uuid:pk>/audit
viewer  -                             GET    components/<uuid:pk>/audit/<uuid:event_pk>
viewer  -                             GET    components/<uuid:pk>/dependencies
viewer  -                             GET    components/<uuid:pk>/discovery-sources
editor  eol                           DELETE components/<uuid:pk>/end-of-life
viewer  eol                           GET    components/<uuid:pk>/end-of-life
editor  eol                           PATCH  components/<uuid:pk>/end-of-life
viewer  -                             GET    components/<uuid:pk>/map
editor  -                             POST   components/<uuid:pk>/restore
viewer  -                             GET    components/<uuid:pk>/reviews
editor  -                             POST   components/<uuid:pk>/reviews
viewer  -                             GET    components/<uuid:pk>/reviews/<uuid:review_pk>
viewer  -                             GET    components/export.csv
viewer  -                             GET    components/export.xlsx
editor  -                             GET    components/template.csv
viewer  data_quality                  GET    composite/data-gaps
viewer  -                             GET    composite/deadlines
viewer  -                             GET    composite/dependencies
viewer  -                             GET    composite/discovery-summary
viewer  -                             GET    composite/expiring-credentials
viewer  -                             GET    composite/renewals
viewer  -                             GET    composite/search
viewer  contracts                     GET    composite/spend
admin   discovery.google              GET    connections/google_workspace
admin   discovery.google              POST   connections/google_workspace
admin   discovery.google              DELETE connections/google_workspace/<uuid:pk>
admin   discovery.google              GET    connections/google_workspace/<uuid:pk>
admin   discovery.google              PATCH  connections/google_workspace/<uuid:pk>
admin   discovery.google              POST   connections/google_workspace/<uuid:pk>/test
admin   discovery.halo                GET    connections/halo
admin   discovery.halo                POST   connections/halo
admin   discovery.halo                DELETE connections/halo/<uuid:pk>
admin   discovery.halo                GET    connections/halo/<uuid:pk>
admin   discovery.halo                PATCH  connections/halo/<uuid:pk>
admin   discovery.halo                POST   connections/halo/<uuid:pk>/test
admin   -                             GET    connections/microsoft
admin   -                             POST   connections/microsoft
admin   -                             DELETE connections/microsoft/<uuid:pk>
admin   -                             GET    connections/microsoft/<uuid:pk>
admin   -                             PATCH  connections/microsoft/<uuid:pk>
admin   -                             POST   connections/microsoft/<uuid:pk>/test
admin   discovery.okta                GET    connections/okta
admin   discovery.okta                POST   connections/okta
admin   discovery.okta                DELETE connections/okta/<uuid:pk>
admin   discovery.okta                GET    connections/okta/<uuid:pk>
admin   discovery.okta                PATCH  connections/okta/<uuid:pk>
admin   discovery.okta                POST   connections/okta/<uuid:pk>/test
viewer  -                             GET    connectors
admin   -                             POST   connectors
admin   -                             DELETE connectors/<uuid:pk>
viewer  -                             GET    connectors/<uuid:pk>
admin   -                             PATCH  connectors/<uuid:pk>
admin   -                             POST   connectors/<uuid:pk>/access-check
admin   -                             POST   connectors/<uuid:pk>/run
viewer  -                             GET    connectors/<uuid:pk>/runs
viewer  -                             GET    connectors/<uuid:pk>/runs/<uuid:run_pk>
viewer  contracts                     GET    contracts
editor  contracts                     POST   contracts
admin   contracts                     DELETE contracts/<uuid:pk>
viewer  contracts                     GET    contracts/<uuid:pk>
editor  contracts                     PATCH  contracts/<uuid:pk>
editor  contracts                     POST   contracts/<uuid:pk>/archive
viewer  attachments+contracts         GET    contracts/<uuid:pk>/attachments
editor  attachments+contracts         POST   contracts/<uuid:pk>/attachments
viewer  contracts                     GET    contracts/<uuid:pk>/audit
viewer  contracts                     GET    contracts/<uuid:pk>/audit/<uuid:event_pk>
viewer  contracts+discovery.entra     GET    contracts/<uuid:pk>/licence-waste
viewer  contracts                     GET    contracts/<uuid:pk>/lines
editor  contracts                     POST   contracts/<uuid:pk>/lines
editor  contracts                     DELETE contracts/<uuid:pk>/lines/<uuid:item_pk>
viewer  contracts                     GET    contracts/<uuid:pk>/lines/<uuid:item_pk>
editor  contracts                     PATCH  contracts/<uuid:pk>/lines/<uuid:item_pk>
viewer  contracts                     GET    contracts/<uuid:pk>/renew
editor  contracts                     POST   contracts/<uuid:pk>/renew
editor  contracts                     POST   contracts/<uuid:pk>/restore
viewer  contracts                     GET    contracts/export.csv
viewer  contracts                     GET    contracts/export.xlsx
viewer  contracts                     GET    contracts/preview
viewer  custom_fields                 GET    custom-fields
admin   custom_fields                 POST   custom-fields
admin   custom_fields                 DELETE custom-fields/<uuid:pk>
viewer  custom_fields                 GET    custom-fields/<uuid:pk>
admin   custom_fields                 PATCH  custom-fields/<uuid:pk>
viewer  -                             GET    dashboard
viewer  data_quality                  GET    data-quality/exceptions
editor  data_quality                  POST   data-quality/exceptions
editor  data_quality                  DELETE data-quality/exceptions/<uuid:pk>
viewer  data_quality                  GET    data-quality/exceptions/<uuid:pk>
editor  data_quality                  PATCH  data-quality/exceptions/<uuid:pk>
viewer  data_quality                  GET    data-quality/failures
viewer  data_quality                  GET    data-quality/rules
viewer  data_quality                  GET    data-quality/summary
viewer  -                             GET    departments
editor  -                             POST   departments
editor  -                             DELETE departments/<uuid:pk>
viewer  -                             GET    departments/<uuid:pk>
editor  -                             PATCH  departments/<uuid:pk>
viewer  -                             GET    dependencies
viewer  -                             GET    discovery/candidates
viewer  -                             GET    discovery/credentials
viewer  -                             GET    discovery/observations/<uuid:pk>
editor  -                             POST   discovery/observations/<uuid:pk>/assistant/accept
editor  -                             POST   discovery/observations/<uuid:pk>/assistant/reject
editor  -                             POST   discovery/observations/<uuid:pk>/create
editor  -                             POST   discovery/observations/<uuid:pk>/dismiss
viewer  -                             GET    discovery/observations/<uuid:pk>/drift
editor  -                             POST   discovery/observations/<uuid:pk>/drift/apply
editor  -                             POST   discovery/observations/<uuid:pk>/drift/ignore
editor  -                             POST   discovery/observations/<uuid:pk>/link
editor  -                             POST   discovery/observations/<uuid:pk>/missing/acknowledge
editor  -                             POST   discovery/observations/<uuid:pk>/missing/archive
editor  -                             POST   discovery/observations/<uuid:pk>/reopen
viewer  -                             GET    discovery/observations/<uuid:pk>/suggestions
viewer  -                             GET    discovery/runs
viewer  -                             GET    discovery/runs/<uuid:pk>
viewer  discovery.entra               GET    discovery/shadow-it
viewer  discovery.entra               GET    discovery/shadow-it/export.csv
viewer  -                             GET    discovery/sources
viewer  eol                           GET    end-of-life
viewer  eol                           GET    end-of-life/export.csv
viewer  eol                           GET    end-of-life/products
viewer  eol                           GET    end-of-life/products/<str:name>
editor  -                             GET    import-sources
admin   -                             POST   import-sources
admin   -                             DELETE import-sources/<uuid:pk>
editor  -                             GET    import-sources/<uuid:pk>
admin   -                             PATCH  import-sources/<uuid:pk>
editor  -                             GET    imports
editor  -                             POST   imports
editor  -                             GET    imports/<uuid:pk>
editor  -                             PATCH  imports/<uuid:pk>
editor  -                             POST   imports/<uuid:pk>/cancel
editor  -                             POST   imports/<uuid:pk>/commit
editor  -                             GET    imports/<uuid:pk>/errors.csv
editor  -                             POST   imports/<uuid:pk>/rerun
editor  -                             GET    imports/<uuid:pk>/rows
editor  -                             GET    imports/template.csv
viewer  -                             GET    integrations
editor  -                             POST   integrations
admin   -                             DELETE integrations/<uuid:pk>
viewer  -                             GET    integrations/<uuid:pk>
editor  -                             PATCH  integrations/<uuid:pk>
editor  -                             POST   integrations/<uuid:pk>/archive
viewer  attachments                   GET    integrations/<uuid:pk>/attachments
editor  attachments                   POST   integrations/<uuid:pk>/attachments
viewer  -                             GET    integrations/<uuid:pk>/audit
viewer  -                             GET    integrations/<uuid:pk>/audit/<uuid:event_pk>
viewer  -                             GET    integrations/<uuid:pk>/confirmations
editor  -                             POST   integrations/<uuid:pk>/confirmations
viewer  -                             GET    integrations/<uuid:pk>/confirmations/<uuid:confirmation_pk>
editor  -                             POST   integrations/<uuid:pk>/restore
admin   -                             GET    invitations
admin   -                             POST   invitations
admin   -                             GET    invitations/<uuid:pk>
admin   -                             POST   invitations/<uuid:pk>/resend
admin   -                             POST   invitations/<uuid:pk>/revoke
viewer  contracts                     GET    license-pools
editor  contracts                     POST   license-pools
admin   contracts                     DELETE license-pools/<uuid:pk>
viewer  contracts                     GET    license-pools/<uuid:pk>
editor  contracts                     PATCH  license-pools/<uuid:pk>
viewer  contracts                     GET    license-pools/<uuid:pk>/audit
viewer  contracts                     GET    license-pools/<uuid:pk>/audit/<uuid:event_pk>
editor  contracts                     POST   license-pools/<uuid:pk>/bind
viewer  contracts+discovery.entra     GET    license-pools/<uuid:pk>/licence-waste
editor  contracts                     POST   license-pools/<uuid:pk>/seat-review/<uuid:observation_pk>/apply
editor  contracts                     POST   license-pools/<uuid:pk>/seat-review/<uuid:observation_pk>/dismiss
viewer  contracts                     GET    license-pools/export.csv
viewer  contracts                     GET    license-pools/seat-review
viewer  contracts                     GET    license-pools/totals
editor  contracts                     POST   license-pools/unbound/<uuid:observation_pk>/create
viewer  -                             GET    needs-attention
viewer  -                             GET    needs-attention/snoozes
editor  -                             POST   needs-attention/snoozes
viewer  -                             DELETE needs-attention/snoozes/<uuid:pk>
viewer  -                             GET    needs-attention/snoozes/<uuid:pk>
admin   -                             GET    notification-log
admin   -                             GET    notification-log/<uuid:pk>
admin   -                             POST   notification-log/<uuid:pk>/resend
admin   -                             GET    notification-rules
admin   -                             POST   notification-rules
admin   -                             DELETE notification-rules/<uuid:pk>
admin   -                             GET    notification-rules/<uuid:pk>
admin   -                             PATCH  notification-rules/<uuid:pk>
admin   -                             GET    notification-rules/<uuid:pk>/preview
admin   -                             POST   notification-rules/<uuid:pk>/test-send
admin   -                             DELETE notification-settings/email
admin   -                             GET    notification-settings/email
admin   -                             PATCH  notification-settings/email
admin   -                             POST   notification-settings/email/test-connection
admin   -                             POST   notification-settings/email/test-send
admin   -                             GET    notification-settings/teams-destinations
admin   -                             POST   notification-settings/teams-destinations
admin   -                             DELETE notification-settings/teams-destinations/<uuid:pk>
admin   -                             GET    notification-settings/teams-destinations/<uuid:pk>
admin   -                             PATCH  notification-settings/teams-destinations/<uuid:pk>
admin   -                             POST   notification-settings/teams-destinations/<uuid:pk>/test-send
admin   -                             GET    notification-settings/webhook-destinations
admin   -                             POST   notification-settings/webhook-destinations
admin   -                             DELETE notification-settings/webhook-destinations/<uuid:pk>
admin   -                             GET    notification-settings/webhook-destinations/<uuid:pk>
admin   -                             PATCH  notification-settings/webhook-destinations/<uuid:pk>
admin   -                             POST   notification-settings/webhook-destinations/<uuid:pk>/test-send
viewer  -                             GET    organization
admin   -                             PATCH  organization
admin   -                             DELETE organization/deletion
admin   -                             GET    organization/deletion
admin   org.self_delete               POST   organization/deletion
admin   -                             GET    organization/exports
admin   org.export                    POST   organization/exports
admin   -                             GET    organization/exports/<uuid:pk>
admin   -                             GET    organization/exports/<uuid:pk>/download
viewer  -                             GET    organization/limits
admin   sso.oidc                      GET    pending-access
admin   sso.oidc                      DELETE pending-access/<uuid:pk>
admin   sso.oidc                      GET    pending-access/<uuid:pk>
admin   sso.oidc                      POST   pending-access/<uuid:pk>/approve
viewer  -                             GET    people
editor  -                             POST   people
viewer  -                             GET    people/<uuid:pk>
editor  -                             PATCH  people/<uuid:pk>
admin   -                             POST   people/<uuid:pk>/anonymize
viewer  -                             GET    people/<uuid:pk>/footprint
viewer  -                             GET    people/<uuid:pk>/footprint.csv
editor  -                             POST   people/<uuid:pk>/reassign
viewer  -                             GET    relationship-types
admin   -                             POST   relationship-types
admin   -                             DELETE relationship-types/<uuid:pk>
viewer  -                             GET    relationship-types/<uuid:pk>
admin   -                             PATCH  relationship-types/<uuid:pk>
viewer  -                             GET    relationships
editor  -                             POST   relationships
editor  -                             DELETE relationships/<uuid:pk>
viewer  -                             GET    relationships/<uuid:pk>
editor  -                             PATCH  relationships/<uuid:pk>
viewer  -                             GET    relationships/<uuid:pk>/confirmations
editor  -                             POST   relationships/<uuid:pk>/confirmations
viewer  -                             GET    relationships/<uuid:pk>/confirmations/<uuid:confirmation_pk>
viewer  -                             GET    reminders
viewer  -                             GET    reminders/<uuid:pk>
editor  -                             POST   reminders/<uuid:pk>/acknowledge
editor  -                             POST   reminders/<uuid:pk>/reopen
editor  -                             POST   reminders/<uuid:pk>/resolve
editor  -                             POST   reminders/<uuid:pk>/snooze
viewer  -                             GET    renewals
editor  -                             POST   renewals
admin   -                             DELETE renewals/<uuid:pk>
viewer  -                             GET    renewals/<uuid:pk>
editor  -                             PATCH  renewals/<uuid:pk>
editor  -                             POST   renewals/<uuid:pk>/archive
viewer  -                             GET    renewals/<uuid:pk>/audit
viewer  -                             GET    renewals/<uuid:pk>/audit/<uuid:event_pk>
editor  -                             POST   renewals/<uuid:pk>/cycles
editor  -                             PATCH  renewals/<uuid:pk>/cycles/<uuid:cycle_pk>
editor  -                             POST   renewals/<uuid:pk>/cycles/<uuid:cycle_pk>/completion
editor  -                             POST   renewals/<uuid:pk>/cycles/<uuid:cycle_pk>/decision
editor  -                             POST   renewals/<uuid:pk>/restore
viewer  ticket_links                  GET    renewals/<uuid:pk>/ticket-links
editor  ticket_links                  POST   renewals/<uuid:pk>/ticket-links
editor  ticket_links                  DELETE renewals/<uuid:pk>/ticket-links/<uuid:link_pk>
viewer  ticket_links                  GET    renewals/<uuid:pk>/ticket-links/<uuid:link_pk>
editor  ticket_links                  PATCH  renewals/<uuid:pk>/ticket-links/<uuid:link_pk>
editor  ticket_links                  POST   renewals/<uuid:pk>/ticket-links/<uuid:link_pk>/refresh
editor  ticket_links                  POST   renewals/<uuid:pk>/ticket-links/create-ticket
viewer  -                             GET    renewals/preview
viewer  -                             GET    reports/access-review
viewer  -                             GET    reports/access-review/export.csv
viewer  data_flows                    GET    reports/data-flows
viewer  data_flows                    GET    reports/data-flows/export.csv
viewer  data_flows                    GET    reports/data-flows/graph
viewer  discovery.entra               GET    reports/licence-waste
admin   discovery.entra               GET    reports/licence-waste/<uuid:sku_id>
admin   discovery.entra               GET    reports/licence-waste/<uuid:sku_id>/export.csv
viewer  discovery.entra               GET    reports/licence-waste/export.csv
viewer  contracts                     GET    reports/license-utilization/export.csv
viewer  -                             GET    reports/renewals-calendar
viewer  -                             GET    reports/renewals-calendar/export.csv
viewer  contracts                     GET    reports/spend
viewer  contracts                     GET    reports/spend/export.csv
viewer  discovery.entra               GET    reports/sso-coverage
viewer  discovery.entra               GET    reports/sso-coverage/export.csv
viewer  -                             GET    saved-filters
viewer  -                             POST   saved-filters
viewer  -                             DELETE saved-filters/<str:pk>
viewer  -                             GET    saved-filters/<str:pk>
viewer  -                             PATCH  saved-filters/<str:pk>
viewer  -                             GET    saved-view-subscriptions
viewer  -                             POST   saved-view-subscriptions
viewer  -                             DELETE saved-view-subscriptions/<uuid:pk>
viewer  -                             GET    saved-view-subscriptions/<uuid:pk>
viewer  -                             PATCH  saved-view-subscriptions/<uuid:pk>
viewer  -                             POST   saved-view-subscriptions/<uuid:pk>/test-send
account -                             GET    schema
viewer  -                             GET    search
admin   sso.oidc                      GET    sso/settings
admin   sso.oidc                      PATCH  sso/settings
admin   sso.oidc                      POST   sso/settings/activate
admin   sso.oidc                      POST   sso/settings/disable
admin   sso.oidc                      POST   sso/settings/domains/verify
admin   sso.oidc                      POST   sso/settings/test
viewer  -                             GET    stewards
admin   -                             GET    system
admin   -                             POST   system/backup/acknowledge-key
viewer  -                             GET    tags
viewer  -                             GET    tags/<uuid:pk>
editor  -                             PATCH  tags/<uuid:pk>
viewer  -                             GET    teams
editor  -                             POST   teams
editor  -                             DELETE teams/<uuid:pk>
viewer  -                             GET    teams/<uuid:pk>
editor  -                             PATCH  teams/<uuid:pk>
admin   ticket_links                  GET    ticket-connections
admin   ticket_links                  POST   ticket-connections
admin   ticket_links                  DELETE ticket-connections/<uuid:pk>
admin   ticket_links                  GET    ticket-connections/<uuid:pk>
admin   ticket_links                  PATCH  ticket-connections/<uuid:pk>
admin   ticket_links                  POST   ticket-connections/<uuid:pk>/test
viewer  ticket_links                  GET    ticket-links/parse
viewer  ticket_links                  GET    ticket-links/systems
admin   -                             GET    users
admin   -                             POST   users
admin   -                             DELETE users/<int:pk>
admin   -                             GET    users/<int:pk>
admin   -                             PATCH  users/<int:pk>
admin   -                             DELETE users/<int:pk>/identities/<int:identity_id>
admin   -                             DELETE users/<int:pk>/identity
admin   -                             POST   users/<int:pk>/reset-password
viewer  vendor_risk                   GET    vendor-risk-documents
editor  vendor_risk                   DELETE vendor-risk-documents/<uuid:pk>
viewer  vendor_risk                   GET    vendor-risk-documents/<uuid:pk>
editor  vendor_risk                   PATCH  vendor-risk-documents/<uuid:pk>
viewer  vendor_risk                   GET    vendor-risk-documents/export.csv
viewer  -                             GET    vendors
editor  -                             POST   vendors
editor  -                             DELETE vendors/<uuid:pk>
viewer  -                             GET    vendors/<uuid:pk>
editor  -                             PATCH  vendors/<uuid:pk>
viewer  attachments                   GET    vendors/<uuid:pk>/attachments
editor  attachments                   POST   vendors/<uuid:pk>/attachments
viewer  vendor_risk                   GET    vendors/<uuid:pk>/risk
editor  vendor_risk                   PATCH  vendors/<uuid:pk>/risk
viewer  vendor_risk                   GET    vendors/<uuid:pk>/risk-documents
editor  vendor_risk                   POST   vendors/<uuid:pk>/risk-documents
viewer  vendor_risk                   GET    vendors/risk-overview
viewer  vendor_risk                   GET    vendors/risk-overview/export.csv
admin   webhooks                      GET    webhook-deliveries
admin   webhooks                      GET    webhook-deliveries/<uuid:pk>
admin   webhooks                      POST   webhook-deliveries/<uuid:pk>/resend
admin   webhooks                      GET    webhooks
admin   webhooks                      POST   webhooks
admin   webhooks                      DELETE webhooks/<uuid:pk>
admin   webhooks                      GET    webhooks/<uuid:pk>
admin   webhooks                      PATCH  webhooks/<uuid:pk>
admin   webhooks                      POST   webhooks/<uuid:pk>/ping
admin   webhooks                      POST   webhooks/<uuid:pk>/rotate-secret
admin   webhooks                      GET    webhooks/event-types
"""


def ledger_rows() -> dict[tuple[str, str], tuple[str, frozenset[str]]]:
    """{(method, route): (minimum role, capabilities)} from ROLE_LEDGER."""
    from core import capabilities

    rows = {}
    for line in ROLE_LEDGER.strip().splitlines():
        role, needs, method, route = line.split()
        names = frozenset() if needs == "-" else frozenset(needs.split("+"))
        assert names <= capabilities.ALL, line
        assert (method.lower(), "api/v1/" + route) not in rows, line
        rows[method.lower(), "api/v1/" + route] = (role, names)
    return rows


def role_ledger() -> dict[tuple[str, str], str]:
    return {key: role for key, (role, _names) in ledger_rows().items()}


def capability_ledger() -> dict[tuple[str, str], frozenset[str]]:
    return {key: names for key, (_role, names) in ledger_rows().items()}


def test_every_route_has_its_ledger_role():
    from core.drf import PLATFORM
    from core.drf import PUBLIC as PUBLIC_ROLE
    from tests.test_url_coverage import declarations

    found, problems = declarations()
    assert problems == []
    declared = {key: role for key, role in found.items() if role not in (PUBLIC_ROLE, PLATFORM)}
    expected = role_ledger()
    assert sorted(set(declared) - set(expected)) == [], "add the new route to ROLE_LEDGER with its minimum role"
    assert sorted(set(expected) - set(declared)) == [], "remove the route that's gone from ROLE_LEDGER"
    wrong = [
        f"{method.upper()} {route}: declares {declared[method, route]}, the ledger says {role}"
        for (method, route), role in expected.items()
        if declared[method, route] != role
    ]
    assert wrong == [], "\n".join(wrong)


def test_the_role_below_each_minimum_is_refused(a, settings, monkeypatch):
    """Every ledger route, every method, with ids that name nothing and an empty body, in an enterprise organization:
    the role just below the minimum gets the role refusal (403 /problems/forbidden) before the route reads anything,
    and the minimum role doesn't. The ACCOUNT routes need only a signed-in account (tests/test_auth.py)."""
    import json

    from rest_framework.views import APIView

    from core import capabilities
    from tests.test_tenancy_walk import PARAM, json_client, send

    settings.API_READ_RATE = settings.API_WRITE_RATE = ""
    monkeypatch.setattr(APIView, "check_throttles", lambda self, request: None)
    capabilities.set_edition(a["org"], "enterprise")
    clients = {role: json_client(a["users"][role], raise_request_exception=False) for role in ALL}
    below = {"editor": "viewer", "admin": "editor"}
    nobody = {"int": "987654321", "uuid": "00000000-0000-4000-8000-0000000f0066"}

    def problem_type(response):
        if "problem+json" not in response.get("Content-Type", ""):
            return None
        return json.loads(response.content).get("type")

    wrong, checked = [], 0
    for (method, route), role in sorted(role_ledger().items()):
        if role == "account":
            continue
        path = "/" + PARAM.sub(lambda m: nobody.get(m.group(1) or "", nobody["uuid"]), route)
        if role in below:
            refused = send(clients[below[role]], method, path)
            if (refused.status_code, problem_type(refused)) != (403, "/problems/forbidden"):
                wrong.append(f"{below[role]} {method.upper()} /{route}: {refused.status_code} {problem_type(refused)}")
        allowed = send(clients[role], method, path)
        if allowed.status_code == 401 or problem_type(allowed) == "/problems/forbidden":
            wrong.append(f"{role} {method.upper()} /{route}: {allowed.status_code} {problem_type(allowed)}")
        checked += 1
    assert checked > 450
    assert wrong == [], "\n".join(wrong)
    for role, client in clients.items():  # nothing the walk sent signed anyone out
        assert client.get("/api/v1/auth/me").status_code == 200, role


def test_the_matrix_agrees_with_the_ledger(a):
    """endpoints() above and the ledger name the same role for every route the matrix lists."""
    from django.urls import resolve

    expected = role_ledger()
    allowed_for = {"viewer": ALL, "editor": WRITERS, "admin": ADMIN, "account": ALL}
    wrong = []
    for method, url, _payload, allowed in endpoints(a):
        route = resolve("/api/v1/" + url.split("?")[0]).route
        if allowed_for[expected[method, route]] != allowed:
            wrong.append(f"{method.upper()} {url}: the matrix allows {sorted(allowed)}, the ledger says {route}")
    assert wrong == [], "\n".join(wrong)
