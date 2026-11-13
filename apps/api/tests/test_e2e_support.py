"""E2E support and schema fixes: the Playwright-only routes the cloud journey needs (api/e2e_views.py outbox,
expire-impersonation, run-export-jobs) and their gating, the impersonation-ended backstop after a read
(core/impersonation.py), platform/capabilities, the organization list's time zone and keyset pages in the schema, and
the limit-reached problem's own schema (core/openapi.py)."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core import mail
from django.utils import timezone

from api import urls as api_urls
from catalog import org_lifecycle, platform_admin
from catalog.models import Resource
from core import capabilities, impersonation, simulation
from core.models import Job, JobStatus, Membership, OrgExport
from core.openapi import LIMIT_REACHED_OPERATIONS
from tests.test_platform import make_staff, start
from tests.test_problems import is_problem

ENDED = "/problems/impersonation-ended"
RESTRICTED = "/problems/impersonation-restricted"
CLOUD_E2E_ROUTES = {"e2e/outbox", "e2e/expire-impersonation", "e2e/run-export-jobs"}


def _urlpatterns():
    """The test-only routes as the e2e server routes them (api/urls.e2e_urlpatterns), beside the real API."""
    from django.urls import include, path

    return [path("api/v1/", include(api_urls.e2e_urlpatterns())), path("api/v1/", include("api.urls"))]


urlpatterns = _urlpatterns()
e2e = pytest.mark.urls("tests.test_e2e_support")


@pytest.fixture(autouse=True)
def empty_cache():
    """Starting an impersonation shares the sign-in throttle, which counts in the process cache across tests."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def staff(db):
    return make_staff()


def expire(client) -> None:
    """Move the session's impersonation past its time limit, as the clock would."""
    session = client.client.session
    state = session[impersonation.SESSION_KEY]
    state["expires_at"] = (timezone.now() - timedelta(seconds=1)).isoformat()
    session[impersonation.SESSION_KEY] = state
    session.save()


# ---- gating ---------------------------------------------------------------------------------------------------------


def test_the_e2e_routes_are_gated_by_the_flag_and_the_database_name(monkeypatch):
    routes = {str(p.pattern) for p in api_urls.e2e_urlpatterns()}
    assert routes >= CLOUD_E2E_ROUTES | {"e2e/run-digest", "e2e/assistant-org"}
    # Every test-only view in the module is routed there (and so behind the same gate), and nothing else is.
    from api import e2e_views

    views = {v for v in vars(e2e_views).values() if callable(v) and hasattr(v, "cls")}
    assert {p.callback for p in api_urls.e2e_urlpatterns()} == views
    assert not api_urls.e2e_enabled()  # the test database's name doesn't end in _e2e
    named = SimpleNamespace(DATABASES={"default": {"NAME": "softwarden_cloud_e2e"}})
    monkeypatch.setattr(simulation, "settings", named)
    monkeypatch.delenv("SOFTWARDEN_E2E", raising=False)
    assert not api_urls.e2e_enabled()  # the database alone isn't enough
    monkeypatch.setenv("SOFTWARDEN_E2E", "1")
    assert api_urls.e2e_enabled() and simulation.e2e()
    # An image leaves api/e2e_views.py out (.dockerignore), and then neither the flag nor the database name
    # makes it the Playwright server: no test-only routes, no sign-in simulators.
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "api.e2e_views" else real_find_spec(name, *a)
    )
    assert not api_urls.e2e_enabled() and not simulation.e2e()
    monkeypatch.setattr(importlib.util, "find_spec", real_find_spec)
    monkeypatch.setattr(simulation, "settings", SimpleNamespace(DATABASES={"default": {"NAME": "softwarden"}}))
    assert not api_urls.e2e_enabled()  # nor the flag alone


SETUP = "import os, django; os.environ['DJANGO_SETTINGS_MODULE'] = 'softwarden.settings'; django.setup()"


def _start(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 (fixed arguments)
        [sys.executable, "-c", SETUP],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize("extra", [{"SOFTWARDEN_DEPLOYMENT": "cloud"}, {"SOFTWARDEN_SECURE_COOKIES": "1"}])
def test_the_e2e_flag_refuses_to_start_in_cloud_mode_or_behind_tls(extra):
    """SOFTWARDEN_E2E=1 is never a production setting; with cloud mode or secure cookies, start-up fails."""
    env = {**os.environ, "SOFTWARDEN_E2E": "1", "SOFTWARDEN_DEBUG": "1", "POSTGRES_DB": "x_e2e"}
    for k in ("DJANGO_SETTINGS_MODULE", "SOFTWARDEN_DEPLOYMENT", "SOFTWARDEN_SECURE_COOKIES"):
        env.pop(k, None)
    done = _start({**env, **extra})
    assert done.returncode != 0 and "SOFTWARDEN_E2E=1 is for the Playwright test server only" in done.stderr
    done = _start(env)
    assert done.returncode == 0, done.stderr


def test_no_e2e_route_is_routed_outside_the_e2e_server(a, api):
    admin = api(a["users"]["admin"])
    for pattern in api_urls.e2e_urlpatterns():
        assert admin.post(str(pattern.pattern)).status_code == 404, pattern.pattern
    from django.urls import get_resolver

    from tests.test_url_coverage import walk

    assert not [route for route, _ in walk(get_resolver().url_patterns) if "e2e/" in route]


# ---- e2e/outbox and e2e/run-digest ----------------------------------------------------------------------------------


@e2e
def test_the_outbox_lists_filters_and_clears(a, api):
    mail.send_mail("Confirm your signup", "Link: /signup/verify#abc", "noreply@test", ["New.Customer@example.test"])
    mail.send_mail("You're invited", "Join", "noreply@test", ['"Colleague" <colleague@example.test>'])
    admin = api(a["users"]["admin"])
    everything = admin.post("e2e/outbox").json_body
    assert [m["subject"] for m in everything] == ["Confirm your signup", "You're invited"]
    assert everything[0] == {
        "to": ["New.Customer@example.test"],
        "subject": "Confirm your signup",
        "body": "Link: /signup/verify#abc",
    }
    mine = admin.post("e2e/outbox", {"to": "new.customer@EXAMPLE.test", "clear": True}).json_body
    assert [m["subject"] for m in mine] == ["Confirm your signup"]
    assert [m.subject for m in mail.outbox] == ["You're invited"]  # only the ones returned were taken
    assert [m["subject"] for m in admin.post("e2e/outbox", {"to": "colleague@example.test"}).json_body] == [
        "You're invited"
    ]
    assert admin.post("e2e/outbox", {"clear": True}).status_code == 200 and mail.outbox == []
    for bad in ({"to": ""}, {"to": 5}, {"clear": "yes"}):
        assert admin.post("e2e/outbox", bad).status_code == 400, bad
    assert api(a["users"]["editor"]).post("e2e/outbox").status_code == 403


@e2e
def test_run_digest_takes_only_its_own_mail_out_of_the_outbox(a, api, monkeypatch):
    from notify import digest

    mail.send_mail("Confirm your signup", "Link", "noreply@test", ["someone@example.test"])
    monkeypatch.setattr(digest, "run_for_org", lambda org, now: None)

    def send_now(org, today, now, rounds=1):
        mail.send_mail("Your Softwarden digest", "Renewals", "noreply@test", ["admin@orga.test"])

    monkeypatch.setattr(digest, "send_now", send_now)
    sent = api(a["users"]["admin"]).post("e2e/run-digest").json_body
    assert [m["subject"] for m in sent] == ["Your Softwarden digest"]
    assert [m.subject for m in mail.outbox] == ["Confirm your signup"]  # the other mail stays for e2e/outbox


@e2e
def test_run_subscriptions_sends_todays_email_when_the_send_time_has_passed(a, api, settings, monkeypatch):
    """On the organization's weekday after its send time (Auckland on a Wednesday morning, while
    it was Tuesday evening in UTC), the run's time was earlier than the delivery's queue time and the email was never
    attempted."""
    from datetime import datetime, time
    from zoneinfo import ZoneInfo

    from notify import rules
    from notify.models import Delivery
    from tests.test_saved_views import subscribe, view

    settings.SOFTWARDEN_EMAIL_CONFIGURED = True
    settings.SOFTWARDEN_BASE_URL = "https://softwarden.example.test"
    monkeypatch.setattr(rules, "send_time", lambda org: time(0, 0))  # always passed: 00:01 today is before now
    today = datetime.now(ZoneInfo(a["org"].timezone)).date()
    subscribe(a, view(a), weekday=today.weekday())
    body = api(a["users"]["admin"]).post("e2e/run-digest", {"subscriptions": {"weekday": today.weekday()}}).json_body
    assert body["date"] == today.isoformat() and body["counts"] == {"sent": 1}
    assert [e["to"] for e in body["emails"]] == [[a["users"]["viewer"].email]]
    assert Delivery.objects.get(kind="subscription").status == "sent"


def _from_another_thread(subject: str) -> list:
    """Send one email from another thread, as a parallel spec's request would, and answer what that send raised."""
    import threading

    raised: list = []

    def send():
        try:
            mail.send_mail(subject, "Body", "noreply@test", ["other@example.test"])
        except Exception as e:  # noqa: BLE001 (the test reads what was raised)
            raised.append(e)

    worker = threading.Thread(target=send)
    worker.start()
    worker.join()
    return raised


@e2e
def test_run_digest_never_takes_or_fails_mail_another_request_sends_meanwhile(a, api, monkeypatch):
    """A weekly saved view email sent by a parallel spec while a digest ran was taken out of the
    outbox as the digest's, and a `fail` run made it fail. Only the run's own thread is recorded and refused."""
    from notify import digest

    monkeypatch.setattr(digest, "run_for_org", lambda org, now: None)
    raised: list = []

    def send_now(org, today, now, rounds=1):
        raised.extend(_from_another_thread("Saved view: Gold jobs"))
        mail.send_mail("Your Softwarden digest", "Renewals", "noreply@test", ["admin@orga.test"])

    monkeypatch.setattr(digest, "send_now", send_now)
    sent = api(a["users"]["admin"]).post("e2e/run-digest").json_body
    assert [m["subject"] for m in sent] == ["Your Softwarden digest"]
    assert [m.subject for m in mail.outbox] == ["Saved view: Gold jobs"] and raised == []

    mail.outbox.clear()
    refused: list = []

    def send_now_failing(org, today, now, rounds=1):
        raised.extend(_from_another_thread("Saved view: Gold jobs"))
        try:
            mail.send_mail("Your Softwarden digest", "Renewals", "noreply@test", ["admin@orga.test"])
        except ConnectionRefusedError as e:
            refused.append(e)

    monkeypatch.setattr(digest, "send_now", send_now_failing)
    assert api(a["users"]["admin"]).post("e2e/run-digest", {"fail": True}).json_body == []
    assert len(refused) == 1  # the run's own mail server is down
    assert [m.subject for m in mail.outbox] == ["Saved view: Gold jobs"] and raised == []  # the other thread's isn't
    from api import e2e_views

    assert getattr(e2e_views._mail_run, "sent", None) is None and not getattr(e2e_views._mail_run, "failing", False)


# ---- e2e/expire-impersonation ---------------------------------------------------------------------------------------


@e2e
@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_expire_impersonation_ends_it_on_the_next_request(a, b, staff, api, role):
    Membership.objects.create(user=staff, organization=b["org"], role="admin")  # the staff account's own organization
    client = api(staff)
    assert client.post("e2e/expire-impersonation").status_code == 409  # not impersonating
    start(client, a["org"], a["users"][role])
    answer = client.post("e2e/expire-impersonation")
    assert answer.status_code == 200 and answer.json_body["expires_at"]
    # Still impersonating until the next request notices; that read ends it and marks the session.
    body = client.get("platform/session").json_body
    assert body["user"]["id"] == staff.id and body["impersonation"] is None
    assert impersonation.SESSION_KEY not in dict(client.client.session.items())
    is_problem(client.post("applications", {"name": "Meant for the member", "lifecycle": "active"}), 409, ENDED)
    assert not Resource.objects.filter(name="Meant for the member").exists()


@e2e
def test_other_e2e_routes_stay_undeclared_writes_while_impersonating(a, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["admin"])
    is_problem(client.post("e2e/outbox"), 403, RESTRICTED)
    is_problem(client.post("e2e/run-export-jobs"), 403, RESTRICTED)
    assert not impersonation.is_restricted("POST", "api/v1/e2e/expire-impersonation")
    assert impersonation.is_restricted("POST", "api/v1/e2e/run-digest")


# ---- the impersonation-ended backstop after a read -------------------------------------------------------------------


def test_a_read_that_ends_it_refuses_the_next_write_once(a, b, staff, api):
    Membership.objects.create(user=staff, organization=b["org"], role="admin")
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    expire(client)
    assert client.get("platform/session").json_body["impersonation"] is None  # a read noticed it
    assert impersonation.ENDED_NOTICE in dict(client.client.session.items())
    assert client.get("applications").status_code == 200  # further reads are the staff account's own, as usual
    assert impersonation.ENDED_NOTICE in dict(client.client.session.items())  # reads leave the mark
    response = client.post("applications", {"name": "Meant for the member", "lifecycle": "active"})
    body = is_problem(response, 409, ENDED)
    assert "nothing was done" in body["detail"]
    assert not Resource.objects.filter(name="Meant for the member").exists()
    # The mark is gone: the next write is the staff account's own, in its own organization.
    assert impersonation.ENDED_NOTICE not in dict(client.client.session.items())
    assert client.post("applications", {"name": "The staff account's own", "lifecycle": "active"}).status_code == 201
    assert Resource.objects.get(name="The staff account's own").organization_id == b["org"].id


def test_the_write_that_notices_the_end_leaves_no_mark(a, b, staff, api):
    Membership.objects.create(user=staff, organization=b["org"], role="admin")
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    expire(client)
    is_problem(client.post("applications", {"name": "x", "lifecycle": "active"}), 409, ENDED)
    assert impersonation.ENDED_NOTICE not in dict(client.client.session.items())  # refused once, not twice


def test_console_writes_and_signing_out_are_not_refused_and_keep_the_mark(a, b, staff, api):
    """A platform write (the banner's Stop included) is never refused and never uses the mark up: a stale member tab
    still open must still be stopped from saving as the staff account."""
    from catalog.models import Resource

    Membership.objects.create(user=staff, organization=b["org"], role="admin")
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    expire(client)
    client.get("platform/session")
    # The banner's Stop after the end: a 409 of its own (nothing to stop), and the mark stays.
    is_problem(client.delete("platform/impersonation"), 409)
    assert impersonation.ENDED_NOTICE in dict(client.client.session.items())
    # A console write is the staff account's own work: it runs, and the mark stays too.
    response = client.post(f"platform/organizations/{b['org'].id}/suspend", {"reason": "Unpaid"})
    assert response.status_code == 200
    assert impersonation.ENDED_NOTICE in dict(client.client.session.items())
    platform_admin.unsuspend(staff, b["org"], {"reason": "Paid"})
    # The stale member tab's save is still refused.
    is_problem(client.post("applications", {"name": "Meant for the member", "lifecycle": "active"}), 409, ENDED)
    assert not Resource.objects.filter(name="Meant for the member").exists()
    assert impersonation.ENDED_NOTICE not in dict(client.client.session.items())
    # Signing out is never refused, and flushes a mark with the session.
    other = api(staff)
    start(other, a["org"], a["users"]["editor"])
    expire(other)
    other.get("platform/session")
    assert other.post("auth/logout").status_code == 204
    assert impersonation.ENDED_NOTICE not in dict(other.client.session.items())


def test_the_mark_has_no_time_limit(a, b, staff, api):
    """The case it is for: a member form saved long after the impersonation expired."""
    Membership.objects.create(user=staff, organization=b["org"], role="admin")
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    expire(client)
    client.get("platform/session")
    session = client.client.session
    session[impersonation.ENDED_NOTICE] = (timezone.now() - timedelta(days=1)).isoformat()
    session.save()
    is_problem(client.post("applications", {"name": "A day later", "lifecycle": "active"}), 409, ENDED)
    assert not Resource.objects.filter(name="A day later").exists()
    assert impersonation.ENDED_NOTICE not in dict(client.client.session.items())
    assert client.post("applications", {"name": "Then my own", "lifecycle": "active"}).status_code == 201


def test_starting_again_drops_the_mark(a, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    expire(client)
    client.get("platform/session")
    assert impersonation.ENDED_NOTICE in client.client.session
    # A new impersonation is begun on purpose: the old end's mark goes, and isn't kept aside for afterwards.
    assert start(client, a["org"], a["users"]["viewer"]).status_code == 201
    assert impersonation.ENDED_NOTICE not in client.client.session
    assert impersonation.ENDED_NOTICE not in client.client.session[impersonation.SESSION_KEY]["saved"]
    assert client.delete("platform/impersonation").status_code == 200  # stopped on purpose: no mark either
    assert impersonation.ENDED_NOTICE not in client.client.session


# ---- e2e/run-export-jobs --------------------------------------------------------------------------------------------


@e2e
def test_run_export_jobs_runs_the_callers_or_every_organizations(a, b, api):
    mine = api(a["users"]["admin"]).post("organization/exports").json_body
    theirs = api(b["users"]["admin"]).post("organization/exports").json_body
    later = org_lifecycle._queue(b["org"], None, "backup")  # a nightly backup, queued to run later
    Job.objects.filter(pk=str(later.job_id)).update(run_after=timezone.now() + timedelta(hours=3))
    admin = api(a["users"]["admin"])
    ran = admin.post("e2e/run-export-jobs").json_body
    assert [(r["export"], r["status"], r["job_status"], r["purpose"]) for r in ran] == [
        (mine["id"], "ready", JobStatus.SUCCEEDED, "export")
    ]
    assert ran[0]["organization"] == str(a["org"].id)
    assert OrgExport.objects.get(pk=theirs["id"]).status == "queued"  # never another organization's unasked
    every = admin.post("e2e/run-export-jobs", {"all": True}).json_body
    assert {(r["export"], r["status"]) for r in every} == {(theirs["id"], "ready"), (str(later.id), "ready")}
    assert admin.post("e2e/run-export-jobs").json_body == []
    assert admin.post("e2e/run-export-jobs", {"all": "yes"}).status_code == 400
    assert api(a["users"]["editor"]).post("e2e/run-export-jobs").status_code == 403


# ---- platform/capabilities and the organization list ----------------------------------------------------------------


def test_platform_capabilities_lists_every_name_with_its_editions(staff, api):
    rows = api(staff).get("platform/capabilities").json_body
    offered = capabilities.ALL if capabilities.ee_installed() else capabilities.ALL - capabilities.EE_ONLY
    assert [r["name"] for r in rows] == sorted(offered)  # SAML and SCIM only with the optional `ee` package
    by_name = {r["name"]: r for r in rows}
    assert by_name["catalog"] == {"name": "catalog", "editions": ["community", "cloud", "enterprise"], "gated": False}
    assert by_name["org.self_delete"]["editions"] == ["cloud"]
    assert by_name["org.tenant_backups"]["editions"] == ["cloud", "enterprise"]


def test_the_organization_list_carries_each_time_zone(a, staff, api):
    from core.models import Organization

    Organization.objects.filter(pk=a["org"].pk).update(timezone="Pacific/Auckland")
    rows = api(staff).get("platform/organizations").json_body["results"]
    assert {r["slug"]: r["timezone"] for r in rows} == {"orga": "Pacific/Auckland"}


# ---- the schema --------------------------------------------------------------------------------------------------------


def schema(api, user):
    return json.loads(api(user).client.get("/api/v1/schema?format=json").content)


def test_the_platform_lists_are_declared_as_keyset_pages(a, api):
    doc = schema(api, a["users"]["viewer"])
    schemas = doc["components"]["schemas"]
    for path, row in (
        ("/api/v1/platform/organizations", "PlatformOrganization"),
        ("/api/v1/platform/audit-events", "PlatformAuditEvent"),
    ):
        op = doc["paths"][path]["get"]
        ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        page = schemas[ref.rsplit("/", 1)[1]]
        assert ref.endswith(f"/Paginated{row}List")
        assert set(page["required"]) == {"results", "next", "previous"}
        assert page["properties"]["results"]["items"] == {"$ref": f"#/components/schemas/{row}"}
        assert {"cursor", "page_size", "ordering"} <= {p["name"] for p in op["parameters"]}
    assert "timezone" in schemas["PlatformOrganization"]["properties"]
    caps = doc["paths"]["/api/v1/platform/capabilities"]["get"]["responses"]["200"]["content"]["application/json"]
    assert caps["schema"] == {"type": "array", "items": {"$ref": "#/components/schemas/PlatformCapability"}}
    assert schemas["PlatformCapability"]["properties"]["editions"]["items"] == {
        "$ref": "#/components/schemas/EditionEnum"
    }


def test_limit_reached_has_its_own_problem_schema_on_the_routes_that_can_answer_it(a, api):
    doc = schema(api, a["users"]["viewer"])
    schemas, paths = doc["components"]["schemas"], doc["paths"]
    limit = schemas["LimitReachedProblem"]
    assert set(limit["required"]) == {"type", "title", "status", "detail", "limit", "maximum", "usage", "adding"}
    assert limit["properties"]["limit"] == {"$ref": "#/components/schemas/LimitNameEnum"}
    assert {limit["properties"][k]["type"] for k in ("maximum", "usage", "adding")} == {"integer"}
    assert schemas["Problem"]["properties"]["limit"]["type"] == "integer"  # too-many-events keeps its number
    assert schemas["ProblemOrLimitReached"]["oneOf"] == [
        {"$ref": "#/components/schemas/Problem"},
        {"$ref": "#/components/schemas/LimitReachedProblem"},
    ]
    union = {"$ref": "#/components/schemas/ProblemOrLimitReached"}
    for method, path in sorted(LIMIT_REACHED_OPERATIONS):
        assert method in paths.get(path, {}), f"stale entry {method} {path}"
        conflict = paths[path][method]["responses"]["409"]
        assert conflict["content"]["application/problem+json"]["schema"] == union, (method, path)
        assert "limit-reached" in conflict["description"]
    # Elsewhere a 409 stays the plain Problem.
    other = paths["/api/v1/applications/{id}"]["patch"]["responses"]["409"]
    assert other["content"]["application/problem+json"]["schema"] == {"$ref": "#/components/schemas/Problem"}


def test_a_real_limit_reached_answer_matches_its_schema(a, staff, api):
    from core import limits

    platform_admin.set_limit(staff, a["org"], {"limit": limits.RECORDS, "value": 1, "reason": "Trial"})
    response = api(a["users"]["editor"]).post("applications", {"name": "One too many", "lifecycle": "active"})
    body = is_problem(response, 409, "/problems/limit-reached")
    assert set(body) == {"type", "title", "status", "detail", "limit", "maximum", "usage", "adding"}
    assert body["limit"] == "records" and body["limit"] in limits.NAMES
    assert (body["maximum"], body["usage"], body["adding"]) == (1, 1, 1)


# Every core/limits.check_room call site (module:qualname of the enclosing function) and the API operations that reach it,
# so a new call site, or a route that starts reaching one, fails here until LIMIT_REACHED_OPERATIONS names it too.
RECORDS = {("post", f"/api/v1/{kind}") for kind in ("applications", "components", "integrations", "ai-agents")}
ATTACHMENT_PARENTS = ("applications", "components", "integrations", "ai-agents", "vendors", "contracts")
CHECK_ROOM_SITES: dict[str, set[tuple[str, str]]] = {
    "catalog.services:_create": RECORDS
    | {
        ("post", "/api/v1/discovery/observations/{id}/create"),
        ("post", "/api/v1/discovery/observations/{id}/assistant/accept"),
        ("post", "/api/v1/catalog-code/apply"),
    },
    "catalog_code.engine:execute": {("post", "/api/v1/catalog-code/apply")},
    "catalog.attachments:upload_file": {("post", f"/api/v1/{kind}/{{id}}/attachments") for kind in ATTACHMENT_PARENTS},
    "catalog.admin_services:update_member": {("patch", "/api/v1/users/{id}")},
    "catalog.invitations:create_invitation": {("post", "/api/v1/invitations"), ("post", "/api/v1/users")},
    # The invitation email send budget, on create and resend.
    "catalog.invitations:reserve_email": {
        ("post", "/api/v1/invitations"),
        ("post", "/api/v1/users"),
        ("post", "/api/v1/invitations/{id}/resend"),
    },
    "catalog.invitations:accept_as_new_account": {("post", "/api/v1/auth/invitations/accept")},
    "catalog.invitations:accept_as_existing_account": {("post", "/api/v1/auth/invitations/accept")},
    "catalog.invitations:approve_pending": {("post", "/api/v1/pending-access/{id}/approve")},
    "catalog.signup:_create": {("post", "/api/v1/auth/signup/verify")},
    "discovery.engine:create_connector": {("post", "/api/v1/connectors")},
    "imports.importer:_write": {("post", "/api/v1/imports/{id}/commit")},
    # The `other_records` limit: each secondary table's create service.
    "catalog.services:create_person": {
        ("post", "/api/v1/people"),
        ("post", "/api/v1/imports/{id}/commit"),  # people the uploader chose to create
        ("post", "/api/v1/catalog-code/apply"),
    },
    "catalog.admin_services:create_named": {
        ("post", "/api/v1/teams"),
        ("post", "/api/v1/departments"),
        ("post", "/api/v1/vendors"),
        ("post", "/api/v1/categories"),
        ("post", "/api/v1/imports/{id}/commit"),  # teams and vendors the uploader chose to create
        ("post", "/api/v1/catalog-code/apply"),
    },
    "catalog.admin_services:create_tag": {("post", "/api/v1/catalog-code/apply")},
    "catalog.services:create_relationship": {("post", "/api/v1/relationships"), ("post", "/api/v1/catalog-code/apply")},
    "catalog.relationship_types:create_type": {
        ("post", "/api/v1/relationship-types"),
        ("post", "/api/v1/catalog-code/apply"),
    },
    "catalog.customfields:create_field": {("post", "/api/v1/custom-fields"), ("post", "/api/v1/catalog-code/apply")},
    "catalog.saved_filters:create": {("post", "/api/v1/saved-filters")},
    "catalog.ai:create_provider": {("post", "/api/v1/ai-providers"), ("post", "/api/v1/catalog-code/apply")},
    "catalog.ai:create_model": {("post", "/api/v1/ai-models"), ("post", "/api/v1/catalog-code/apply")},
    "money.contracts:create_contract": {("post", "/api/v1/contracts"), ("post", "/api/v1/imports/{id}/commit")},
    "money.contracts:add_item": {("post", "/api/v1/contracts/{id}/lines")},
    "money.contracts:set_items": {
        ("post", "/api/v1/imports/{id}/commit"),  # the contract import's lines column
        ("post", "/api/v1/contracts/{id}/renew"),  # the successor term's lines
    },
    "money.contracts:create_pool": {
        ("post", "/api/v1/license-pools"),
        ("post", "/api/v1/license-pools/unbound/{observation_pk}/create"),
        ("post", "/api/v1/contracts/{id}/renew"),  # pools copied to the successor
        ("post", "/api/v1/imports/{id}/commit"),
    },
    # A contract's "new renewal" link (contracts._create_obligation) creates one too, on create and on edit.
    "money.renewals:create_renewal": {
        ("post", "/api/v1/renewals"),
        ("post", "/api/v1/contracts"),
        ("patch", "/api/v1/contracts/{id}"),
        ("post", "/api/v1/imports/{id}/commit"),
    },
    "notify.rule_services:create_rule": {("post", "/api/v1/notification-rules")},
    "notify.channel_services:create_destination": {
        ("post", "/api/v1/notification-settings/teams-destinations"),
        ("post", "/api/v1/notification-settings/webhook-destinations"),
        ("post", "/api/v1/webhooks"),
    },
    "api.tokens:create_token": {("post", "/api/v1/api-tokens")},
    "imports.importer:create_source": {("post", "/api/v1/import-sources")},
}


def check_room_sites() -> set[str]:
    """module:qualname of every function outside tests that calls check_room."""
    root = Path(__file__).resolve().parent.parent
    found: set[str] = set()

    def visit(node, module: str, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                visit(child, module, [*stack, child.name])
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name == "check_room":
                    found.add(f"{module}:{'.'.join(stack)}")
            visit(child, module, stack)

    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if relative.parts[0] in (".venv", "tests") or "migrations" in relative.parts:
            continue
        visit(ast.parse(path.read_text(encoding="utf-8")), ".".join(relative.with_suffix("").parts), [])
    return found


def test_every_check_room_call_site_is_declared_as_limit_reached():
    assert check_room_sites() == set(CHECK_ROOM_SITES)
    assert set().union(*CHECK_ROOM_SITES.values()) == LIMIT_REACHED_OPERATIONS


@pytest.mark.django_db(transaction=True)
def test_the_vendor_risk_spec_organization_starts_from_the_scenario_on_every_call():
    """e2e/spec-org `vendor_risk` (the vendor risk pages): seed_demo's risk files, and what a spec did to them, to the queue's
    snoozes and to the reminders, are put back on every call."""
    from api.e2e_views import make_spec_org
    from catalog.models import Vendor, VendorRiskDocument, VendorRiskProfile
    from notify.models import NotificationSnooze, Reminder, ReminderStatus

    org = make_spec_org("vendor_risk")
    zoom = Vendor.objects.get(organization=org, name="Zoom")
    names = set(VendorRiskProfile.objects.filter(organization=org).values_list("vendor__name", flat=True))
    assert names == {"Workday Inc.", "Zoom", "Zylker Software"}
    VendorRiskDocument.objects.filter(organization=org, vendor=zoom).delete()
    admin = Membership.objects.get(organization=org, role="admin").user
    NotificationSnooze.objects.create(
        organization=org, user=admin, subject_type="vendor_risk", subject_id=str(zoom.id), kind="snooze"
    )
    live = Reminder.objects.filter(organization=org, source_type="vendor_risk", status=ReminderStatus.OPEN)
    assert live.exists()
    live.update(status=ReminderStatus.ACKNOWLEDGED, acknowledged_at=timezone.now())

    assert make_spec_org("vendor_risk") == org
    assert VendorRiskDocument.objects.filter(organization=org, vendor=zoom, kind="soc2").count() == 1
    assert not NotificationSnooze.objects.filter(organization=org, subject_type="vendor_risk").exists()
    assert not Reminder.objects.filter(organization=org, status=ReminderStatus.ACKNOWLEDGED).exists()
    assert Reminder.objects.filter(organization=org, source_type="vendor_risk", status=ReminderStatus.OPEN).exists()


@pytest.mark.django_db(transaction=True)
def test_the_lifecycle_spec_organization_starts_from_the_scenario_on_every_call():
    """e2e/spec-org `lifecycle` (the EOL calendar and data-flow map pages): the endoflife.date products, the tracked
    versions, the flows' categories and the two queues' snoozes are put back on every call."""
    from api.e2e_views import make_spec_org
    from catalog.models import Integration
    from core.dates import org_today
    from eol import reads
    from eol import services as eol_services
    from notify.attention import attention_queues
    from notify.models import NotificationSnooze

    org = make_spec_org("lifecycle")
    today = org_today(org)
    tracked = {"Ledger DB", "Payments API", "Intranet", "Mystery App", "Build Server"}
    rows = {r["record"]["name"]: r for r in reads.rows(org, today)}
    assert set(rows) == tracked
    assert rows["Ledger DB"]["status"] == "end_of_life" and rows["Ledger DB"]["days_remaining"] == -200
    assert rows["Payments API"]["days_remaining"] == 60 and rows["Intranet"]["status"] == "supported"
    assert rows["Mystery App"]["status"] == "unknown" and rows["Build Server"]["status"] == "security_only"
    admin = Membership.objects.get(organization=org, role="admin").user
    editor = Membership.objects.get(organization=org, role="editor").user
    queues, _ = attention_queues(org, editor, today)
    assert [r["record"]["name"] for r in queues["end_of_life"]] == ["Ledger DB", "Payments API"]
    assert {r["name"] for r in queues["data_flows"]} == {"Payroll export", "Bare link"}
    # What a spec does: link the untracked app, unlink one, add personal data to a flow, snooze a row.
    eol_services.track(
        org, editor, Resource.objects.get(organization=org, name="Untracked App"), {"product": "postgresql"}
    )
    eol_services.untrack(org, editor, Resource.objects.get(organization=org, name="Intranet"))
    feed = Integration.objects.get(organization=org, resource__name="Marketing feed")
    Integration.objects.filter(pk=feed.pk).update(data_categories=["personal"])
    NotificationSnooze.objects.create(
        organization=org, user=admin, subject_type="data_flows", subject_id=str(feed.resource_id), kind="snooze"
    )

    assert make_spec_org("lifecycle") == org
    assert {r["record"]["name"] for r in reads.rows(org, today)} == tracked
    feed.refresh_from_db()
    assert feed.data_categories == []
    assert not NotificationSnooze.objects.filter(organization=org, subject_type="data_flows").exists()


@pytest.mark.django_db(transaction=True)
def test_the_sso_coverage_spec_organization_puts_its_applications_back_on_every_call():
    """e2e/spec-org `sso_coverage` (the SSO coverage page): seven live applications, six matched to the simulated
    tenant's apps by entra_app_id, with the record values that make Workday and Zoom differ; a spec's edit is undone."""
    from api.e2e_views import make_spec_org
    from catalog import services
    from catalog.models import Application
    from discovery.entra_simulator import _id

    org = make_spec_org("sso_coverage")
    apps = {a.resource.name: a for a in Application.objects.filter(organization=org).select_related("resource")}
    assert set(apps) == {
        "Workday",
        "Zoom",
        "ServiceNow",
        "Expense Hub",
        "Finance Ledger",
        "Contract Vault",
        "Legacy Intranet",
    }
    assert apps["Workday"].entra_app_id == _id("app/Workday")
    assert apps["Workday"].sso_enabled is False
    assert apps["Finance Ledger"].entra_app_id == _id("app/Finance Ledger SSO")
    assert apps["Legacy Intranet"].entra_app_id == ""
    editor = Membership.objects.get(organization=org, role="editor").user
    services.update_resource(org, editor, apps["Workday"].resource, {"sso_enabled": True})

    assert make_spec_org("sso_coverage") == org
    assert Application.objects.get(organization=org, resource__name="Workday").sso_enabled is False
    assert Resource.objects.filter(organization=org, kind="application").count() == 7


@pytest.mark.django_db(transaction=True)
def test_the_tickets_spec_organization_starts_with_no_connection_or_link():
    """e2e/spec-org `tickets` (the ticket links pages): connections are one per system, so every call removes them
    and the links."""
    from api.e2e_views import make_spec_org
    from tickets.models import TicketConnection

    org = make_spec_org("tickets")
    TicketConnection.objects.create(
        organization=org, system="jira", name="Jira", base_url="https://jira.simulated.invalid", username="a@b.test"
    )
    assert make_spec_org("tickets") == org
    assert not TicketConnection.objects.filter(organization=org).exists()
