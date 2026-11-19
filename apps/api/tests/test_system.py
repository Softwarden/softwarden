"""Health (core/health.py), `/readyz` and the System status API: the System page shows each
schedule's last run and state, and /readyz fails when the heartbeat is stale."""

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from core import health, jobs, worker
from core.models import Job, JobStatus, Schedule, WorkerHeartbeat


@pytest.fixture
def only_schedules(monkeypatch):
    registry: dict = {}
    monkeypatch.setattr(jobs, "SCHEDULES", registry)
    return registry


def schedule(name, **fields):
    Schedule.objects.filter(name=name).update(**fields)
    return Schedule.objects.get(name=name)


def state_of(name, now=None):
    return next(c for c in health.schedule_checks(now) if c["name"] == name)["state"]


# ---- health ----------------------------------------------------------------------------------------------------------


def test_worker_check(db):
    now = timezone.now()
    assert health.worker_check(now)["state"] == "never"
    worker.beat(now - timedelta(seconds=60))
    check = health.worker_check(now)
    assert check["state"] == "ok" and check["age_seconds"] == 60 and check["stale_after_seconds"] == 180
    worker.beat(now - timedelta(seconds=181))
    assert health.worker_check(now)["state"] == "stale"


def test_schedule_states(db, only_schedules):
    jobs.register_schedule("test.s", lambda now: None, "*/5 * * * *", "Every five minutes")
    now = timezone.now().replace(minute=2, second=0, microsecond=0)
    assert state_of("test.s", now) == "never_run"  # no row yet
    jobs.sync_schedules(now)
    assert state_of("test.s", now) == "never_run"  # first due at :05
    worker.tick(now.replace(minute=5))
    row = Schedule.objects.get(name="test.s")
    assert row.last_started is not None and state_of("test.s", now.replace(minute=6)) == "ok"
    later = row.next_due + health.LATE_GRACE + timedelta(seconds=1)
    assert state_of("test.s", later) == "overdue"
    assert state_of("test.s", row.next_due + health.LATE_GRACE) != "overdue"  # within the grace
    schedule("test.s", last_started=now, last_finished=now, last_succeeded=None, last_error="SMTPException: down")
    assert state_of("test.s", now) == "failing"
    schedule("test.s", lease_token="1f0b3c2e-0000-4000-8000-000000000000", lease_expires=now + timedelta(minutes=5))
    assert state_of("test.s", now) == "running"
    assert state_of("test.s", now + timedelta(minutes=6)) == "stuck"
    schedule("test.s", enabled=False)
    assert state_of("test.s", later) == "disabled"


def test_a_dead_worker_makes_every_minute_schedules_overdue(db, only_schedules):
    jobs.register_schedule("test.minute", lambda now: None, "* * * * *", "Every minute")
    now = timezone.now()
    worker.tick(now)
    assert state_of("test.minute", now + timedelta(minutes=10)) == "ok"
    assert state_of("test.minute", now + timedelta(minutes=32)) == "overdue"


def test_rows_no_longer_registered_are_retired_and_harmless(db, only_schedules):
    Schedule.objects.create(name="old.thing", cron="* * * * *", next_due=timezone.now() - timedelta(days=3))
    worker.beat()
    checks = health.schedule_checks()
    assert [(c["name"], c["state"]) for c in checks] == [("old.thing", "retired")]
    assert health.status()["status"] == "ok"


def test_overall_status(db, only_schedules):
    jobs.register_schedule("test.minute", lambda now: None, "* * * * *", "Every minute")
    now = timezone.now()
    assert health.status(now)["status"] == "unhealthy"  # no worker yet
    worker.tick(now)
    assert health.status(now)["status"] == "ok"
    schedule("test.minute", last_finished=now, last_succeeded=None, last_error="x")
    assert health.status(now)["status"] == "degraded"
    worker.beat(now - timedelta(minutes=5))
    assert health.status(now)["status"] == "unhealthy"


# ---- /readyz ---------------------------------------------------------------------------------------------------------


def readyz():
    response = Client().get("/readyz")
    return response.status_code, response.json()


def test_readyz_is_ready_once_the_worker_ticks(db):
    assert readyz()[0] == 503
    worker.tick()
    code, body = readyz()
    assert code == 200 and body == {
        "status": "ok",
        "checks": {"database": "ok", "migrations": "ok", "worker": "ok", "schedules": "ok"},
    }


def test_readyz_fails_when_the_heartbeat_is_stale(db):
    worker.tick()
    WorkerHeartbeat.objects.filter(name="worker").update(last_tick=timezone.now() - timedelta(minutes=4))
    code, body = readyz()
    assert code == 503 and body["status"] == "unavailable"
    # The public probe gives the state only; the heartbeat's age is on the System page and /healthz/ops.
    assert body["checks"]["worker"] == "stale" and " ago" not in str(body)


def test_readyz_fails_when_a_schedule_is_overdue_or_stuck(db):
    worker.tick()
    Schedule.objects.filter(name="notify.deliveries").update(next_due=timezone.now() - timedelta(hours=1))
    code, body = readyz()
    # The worst state only, no schedule name (names are on the System page and /healthz/ops).
    assert code == 503 and body["checks"]["schedules"] == "overdue" and "notify.deliveries" not in str(body)
    Schedule.objects.filter(name="notify.deliveries").update(
        next_due=timezone.now() + timedelta(minutes=1),
        lease_token="1f0b3c2e-0000-4000-8000-000000000000",
        lease_expires=timezone.now() - timedelta(minutes=1),
        last_started=timezone.now() - timedelta(hours=1),
    )
    code, body = readyz()
    assert code == 503 and body["checks"]["schedules"] == "stuck" and "notify.deliveries" not in str(body)


def test_readyz_reports_a_failing_schedule_without_failing(db):
    worker.tick()
    now = timezone.now()
    Schedule.objects.filter(name="imports.purge").update(last_finished=now, last_error="RuntimeError: x")
    code, body = readyz()
    assert code == 200 and body["checks"]["schedules"] == "failing" and "imports.purge" not in str(body)


def test_readyz_web_ignores_the_worker(db):
    response = Client().get("/readyz/web")
    assert response.status_code == 200 and "worker" not in response.json()["checks"]


# ---- System status API ------------------------------------------------------------------------------------------------


def test_system_status_for_admins(a, b, api):
    from tests.conftest import no_digest

    no_digest()  # the job counts below are the test's own, whatever the time of day
    worker.tick()
    now = timezone.now()
    Schedule.objects.filter(name="imports.purge").update(last_finished=now, last_error="RuntimeError: db hiccup")
    Job.objects.create(kind="test.a", organization=a["org"], status=JobStatus.QUEUED)
    Job.objects.create(
        kind="test.a",
        organization=a["org"],
        status=JobStatus.FAILED,
        attempts=3,
        last_error="KeyError: x",
        finished_at=now,
    )
    Job.objects.create(kind="test.sys", status=JobStatus.ABANDONED, attempts=1, last_error="Abandoned", finished_at=now)
    Job.objects.create(
        kind="test.theirs", organization=b["org"], status=JobStatus.FAILED, last_error="orgb detail", finished_at=now
    )
    response = api(a["users"]["admin"]).get("system")
    assert response.status_code == 200
    body = response.json_body
    assert body["status"] == "degraded"
    assert body["worker"]["state"] == "ok" and body["worker"]["age_seconds"] >= 0
    by_name = {s["name"]: s for s in body["schedules"]}
    assert {"digest.build", "notify.deliveries", "discovery.connectors", "imports.purge", "jobs.prune"} <= set(by_name)
    send = by_name["notify.deliveries"]
    assert send["state"] == "ok" and send["cron"] == "* * * * *" and send["enabled"] is True
    assert send["last_started"] and send["last_finished"] and send["last_succeeded"] and send["next_due"]
    assert send["last_result"] == {"deliveries_expired": 0, "deliveries_requeued": 0}
    assert send["label"] == "Fail deliveries whose worker stopped mid-send; requeue deliveries whose job is gone"
    purge = by_name["imports.purge"]
    assert purge["state"] == "failing" and purge["last_error"] == "RuntimeError: db hiccup"
    assert by_name["jobs.prune"]["state"] == "never_run"  # nightly; not due yet
    assert body["jobs"]["organization"] == {"queued": 1, "running": 0, "succeeded": 0, "failed": 1, "abandoned": 0}
    assert body["jobs"]["system"] == {"queued": 0, "running": 0, "succeeded": 0, "failed": 0, "abandoned": 1}
    problems = body["jobs"]["recent_problems"]
    assert {(p["kind"], p["scope"]) for p in problems} == {("test.a", "organization"), ("test.sys", "system")}
    assert "orgb detail" not in json.dumps(body)  # another organization's jobs never show


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_system_status_is_admin_only(a, api, role):
    assert api(a["users"][role]).get("system").status_code == 403
    assert api().get("system").status_code in (401, 403)


def test_system_status_before_the_worker_has_run(a, api):
    body = api(a["users"]["admin"]).get("system").json_body
    assert body["status"] == "unhealthy" and body["worker"] == {
        "state": "never",
        "last_tick": None,
        "age_seconds": None,
        "stale_after_seconds": 180,
    }
    assert {s["state"] for s in body["schedules"]} == {"never_run"}


def test_system_status_is_documented(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    get = doc["paths"]["/api/v1/system"]["get"]
    assert {"200", "403"} <= set(get["responses"])
    status = doc["components"]["schemas"]["SystemStatus"]["properties"]
    assert {"status", "worker", "schedules", "jobs", "checked_at"} <= set(status)
    assert {"state", "last_started", "last_succeeded", "last_error", "next_due"} <= set(
        doc["components"]["schemas"]["ScheduleStatus"]["properties"]
    )


def test_a_run_past_the_ceiling_is_stuck_even_with_a_live_lease(db, only_schedules):
    jobs.register_schedule("test.s", lambda now: None, "0 0 1 1 *", "Yearly")
    now = timezone.now()
    jobs.sync_schedules(now)
    schedule(
        "test.s",
        lease_token="1f0b3c2e-0000-4000-8000-000000000000",
        lease_expires=now + timedelta(minutes=3),
        last_started=now - jobs.SCHEDULE_CEILING - timedelta(minutes=1),
    )
    assert state_of("test.s", now) == "stuck"


def test_readyz_survives_a_lease_without_a_start_time(db):
    worker.tick()
    Schedule.objects.filter(name="notify.deliveries").update(
        lease_token="1f0b3c2e-0000-4000-8000-000000000000",
        lease_expires=timezone.now() - timedelta(minutes=1),
        last_started=None,
    )
    response = Client().get("/readyz")
    assert response.status_code == 503 and response.json()["checks"]["schedules"] == "stuck"
    assert "notify.deliveries" not in response.content.decode()


# ---- the Playwright-only fixture for the System page (api/e2e_views.system_states) -------------------------------------


def test_the_e2e_system_states_route_does_not_exist_outside_the_e2e_suite(a, api):
    """Routed only for Playwright's _e2e database (api/urls.py); here it is a 404 and changes nothing."""
    worker.beat()
    response = api(a["users"]["admin"]).client.post(
        "/api/v1/e2e/system-states", data=json.dumps({"worker": "never"}), content_type="application/json"
    )
    assert response.status_code == 404
    assert WorkerHeartbeat.objects.exists()


def test_the_e2e_system_states_fixture_produces_each_state(a):
    from api.e2e_views import E2E_JOB_KIND, set_system_states

    names = list(jobs.SCHEDULES)
    wanted = dict(zip(names, ["ok", "failing", "stuck", "overdue", "disabled"], strict=False))
    assert (
        set_system_states(a["org"], {"worker": "stale", "schedules": wanted, "retired": ["old.e2e"], "jobs": True})
        == {}
    )
    status = health.status()
    assert status["worker"]["state"] == "stale" and status["status"] == "unhealthy"
    got = {c["name"]: c["state"] for c in status["schedules"]}
    assert {n: got[n] for n in wanted} == wanted and got["old.e2e"] == "retired"
    assert Job.objects.filter(kind=E2E_JOB_KIND, organization=a["org"], status=JobStatus.FAILED).count() == 1
    assert Job.objects.filter(kind=E2E_JOB_KIND, organization=None, status=JobStatus.ABANDONED).count() == 1

    # Running and never run, with a live worker; every earlier row is replaced.
    assert (
        set_system_states(a["org"], {"worker": "ok", "schedules": {names[0]: "running", names[1]: "never_run"}}) == {}
    )
    got = {c["name"]: c["state"] for c in health.schedule_checks()}
    assert got[names[0]] == "running" and got[names[1]] == "never_run" and "old.e2e" not in got
    assert health.worker_check()["state"] == "ok"


@pytest.mark.parametrize(
    "spec",
    [
        {"worker": "asleep"},
        {"schedules": {"no.such": "ok"}},
        {"schedules": {"jobs.prune": "late"}},
        {"retired": ["jobs.prune"]},
    ],
)
def test_the_e2e_system_states_fixture_refuses_bad_input(a, spec):
    from api.e2e_views import set_system_states

    worker.beat()
    assert set_system_states(a["org"], spec) != {}
    assert WorkerHeartbeat.objects.exists()  # nothing was replaced


@pytest.mark.parametrize("body", [[], "ok", None])
def test_the_e2e_system_states_fixture_wants_an_object(a, body):
    from api.e2e_views import set_system_states

    worker.beat()
    assert set_system_states(a["org"], body) == {"spec": ["Send an object."]}
    assert WorkerHeartbeat.objects.exists()


# ---- the Playwright-only enterprise organization (api/e2e_views.enterprise_org) ---------------------


def test_the_e2e_enterprise_org_route_does_not_exist_outside_the_e2e_suite(a, api):
    """Routed only for Playwright's _e2e database (api/urls.py); here it is a 404 and makes no organization or
    account, and the caller's edition is unchanged."""
    from django.contrib.auth import get_user_model

    from api.e2e_views import ENTERPRISE_ADMIN, ENTERPRISE_ORG
    from core.models import Organization

    response = api(a["users"]["admin"]).client.post("/api/v1/e2e/enterprise-org")
    assert response.status_code == 404
    assert not Organization.objects.filter(name=ENTERPRISE_ORG).exists()
    assert not get_user_model().objects.filter(username=ENTERPRISE_ADMIN).exists()
    a["org"].refresh_from_db()
    assert a["org"].edition == "community"


def test_the_e2e_enterprise_org_fixture_makes_an_enterprise_org_with_the_profile_off(db):
    """The fixture's own logic: an enterprise organization with an Admin in it only and the compliance profile off,
    and a rerun turns the profile off again without making a second organization or account."""
    from api.e2e_views import ENTERPRISE_ORG, make_enterprise_org
    from catalog import compliance
    from core.capabilities import COMPLIANCE_PROFILE, has_capability
    from core.models import Membership, Organization, Role

    org, user = make_enterprise_org()
    assert org.edition == "enterprise" and has_capability(org, COMPLIANCE_PROFILE)
    assert not compliance.enabled(org)
    assert list(Membership.objects.filter(user=user).values_list("organization_id", "role")) == [(org.id, Role.ADMIN)]
    assert org.coordinator_id == user.id

    Organization.objects.filter(pk=org.pk).update(settings={compliance.SETTING: True, "other": 1})
    org2, user2 = make_enterprise_org()
    assert (org2.id, user2.id) == (org.id, user.id)
    assert not compliance.enabled(org2) and org2.settings == {"other": 1}
    assert Organization.objects.filter(name=ENTERPRISE_ORG).count() == 1


def test_the_e2e_enterprise_orgs_are_separate_so_one_spec_group_never_resets_another(db):
    """`which` picks an enterprise organization per group of specs: making the one leaves the compliance spec's
    organization, and its profile, alone. An unknown name is refused by the route (checked here on the mapping)."""
    from api.e2e_views import ENTERPRISE_ADMIN, ENTERPRISE_ORGS, make_enterprise_org
    from catalog import compliance
    from core.models import Membership, Organization

    northwind, erin = make_enterprise_org()
    assert erin.email == ENTERPRISE_ADMIN
    Organization.objects.filter(pk=northwind.pk).update(settings={compliance.SETTING: True})
    compliance.forget(northwind.pk)
    litware, lee = make_enterprise_org("secondary")
    assert litware.id != northwind.id and lee.id != erin.id
    assert litware.name == ENTERPRISE_ORGS["secondary"][0] and litware.edition == "enterprise"
    assert list(Membership.objects.filter(user=lee).values_list("organization_id", flat=True)) == [litware.id]
    northwind.refresh_from_db()
    assert compliance.enabled(northwind)  # untouched by the call
    assert set(ENTERPRISE_ORGS) == {"compliance", "secondary"}


# ---- the Playwright-only custom fields organization (api/e2e_views.custom_fields_org) ----------------


def test_the_e2e_custom_fields_org_route_does_not_exist_outside_the_e2e_suite(a, api):
    """Routed only for Playwright's _e2e database (api/urls.py); here it is a 404 and makes no organization."""
    from api.e2e_views import CUSTOM_FIELDS_ORG
    from core.models import Organization

    response = api(a["users"]["admin"]).client.post("/api/v1/e2e/custom-fields-org")
    assert response.status_code == 404
    assert not Organization.objects.filter(name=CUSTOM_FIELDS_ORG).exists()


def test_the_e2e_custom_fields_org_fixture_makes_its_own_org_with_three_roles(db):
    """An organization of its own (custom fields are organization-wide), with an Admin, an Editor and a Viewer in it
    only, people, a team and a vendor; a rerun makes nothing twice."""
    from api.e2e_views import CUSTOM_FIELDS_ORG, CUSTOM_FIELDS_USERS, make_custom_fields_org
    from catalog.models import Person, Team, Vendor
    from core.models import Membership, Organization

    org = make_custom_fields_org()
    roles = dict(Membership.objects.filter(organization=org).values_list("user__username", "role"))
    assert roles == {email: role for email, _, _, role in CUSTOM_FIELDS_USERS}
    assert Membership.objects.filter(user__username__in=roles).exclude(organization=org).count() == 0
    assert org.coordinator.username == "cf-admin@example.test"
    assert Person.objects.filter(organization=org).count() == 2
    assert Team.objects.filter(organization=org).count() == 1 and Vendor.objects.filter(organization=org).count() == 1

    again = make_custom_fields_org()
    assert again.id == org.id and Organization.objects.filter(name=CUSTOM_FIELDS_ORG).count() == 1
    assert Person.objects.filter(organization=org).count() == 2


# ---- the Playwright-only spec organizations (api/e2e_views.spec_org) ------------------------------------


def test_the_e2e_spec_org_route_does_not_exist_outside_the_e2e_suite(a, api):
    from api.e2e_views import JOURNEY_ORG, QUALITY_ORG
    from core.models import Organization

    response = api(a["users"]["admin"]).client.post("/api/v1/e2e/spec-org")
    assert response.status_code == 404
    assert not Organization.objects.filter(name__in=[QUALITY_ORG, JOURNEY_ORG]).exists()


def test_the_e2e_spec_orgs_are_their_own_and_a_rerun_resets_their_settings(db):
    """Each its own organization with three roles; a rerun makes nothing twice and puts the rule switches and
    attachment limits back to their defaults (other settings stay)."""
    from api.e2e_views import SPEC_ORGS, make_spec_org
    from catalog.models import Person
    from core.models import Membership, Organization

    for which, (name, users, _) in SPEC_ORGS.items():
        org = make_spec_org(which)
        roles = dict(Membership.objects.filter(organization=org).values_list("user__username", "role"))
        assert roles == {email: role for email, _, _, role in users}
        assert Membership.objects.filter(user__username__in=roles).exclude(organization=org).count() == 0
        assert org.coordinator.username == users[0][0]
        assert Person.objects.filter(organization=org, active=True).count() == 2

        org.settings = {"data_quality_disabled_rules": ["vendor"], "attachment_max_size_mb": 1, "other": 1}
        org.save()
        again = make_spec_org(which)
        assert again.id == org.id and Organization.objects.filter(name=name).count() == 1
        assert again.settings == {"other": 1}
        assert Person.objects.filter(organization=org).count() == 2


def test_system_status_warns_when_stored_secrets_have_no_key(a, api, monkeypatch):
    """A restore into a stack with a new credential key leaves every stored secret
    unreadable; the System API says so (doctor's stored_secrets), with no count and no value."""
    from core import credentials
    from notify.models import TeamsDestination
    from tests.conftest import TEST_CREDENTIAL_KEY

    teams = TeamsDestination(organization=a["org"], name="Restored", url_host="restore.e2e.test")
    credentials.slot_set(teams, "url", "https://restore.e2e.test/hook/SECRET-PATH")
    teams.save()

    def warnings():
        body = api(a["users"]["admin"]).get("system").json_body
        assert "SECRET-PATH" not in json.dumps(body)
        return {w["code"]: w["message"] for w in body["warnings"]}

    assert "stored_secrets_unreadable" not in warnings()
    new_key = credentials.generate_key()
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", new_key)  # the restored stack's own, new key
    assert "credential key" in warnings()["stored_secrets_unreadable"]
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", f"{new_key},{TEST_CREDENTIAL_KEY}")  # the old key brought back
    assert "stored_secrets_unreadable" not in warnings()


# ---- the installation view and the organization view ---------------------------------


def make_staff_member(org=None):
    from tests.test_platform import make_staff

    return make_staff(org=org)


INSTALLATION_ONLY = {"schedules", "backup"}


def test_with_two_organizations_the_second_ones_admin_gets_the_organization_view(a, b, api):
    worker.tick()
    now = timezone.now()
    Job.objects.create(kind="test.sys", status=JobStatus.ABANDONED, last_error="Abandoned", finished_at=now)
    Job.objects.create(kind="test.b", organization=b["org"], status=JobStatus.FAILED, last_error="x", finished_at=now)
    Job.objects.create(kind="test.a", organization=a["org"], status=JobStatus.FAILED, last_error="y", finished_at=now)
    response = api(b["users"]["admin"]).get("system")
    assert response.status_code == 200
    body = response.json_body
    assert body["scope"] == "organization"
    assert not INSTALLATION_ONLY & set(body), body.keys()  # no schedules, no backup detail
    assert set(body["jobs"]) == {"organization", "recent_problems"}  # no installation-wide counts
    assert body["jobs"]["organization"]["failed"] == 1
    assert [p["kind"] for p in body["jobs"]["recent_problems"]] == ["test.b"]  # neither the system's nor a's
    assert body["worker"]["state"] == "ok" and body["status"] in {"ok", "degraded", "unhealthy"}
    for name in ("org.purge_deleted", "platform.usage", "backup.nightly"):
        assert name not in json.dumps(body)
    # The installation's first organization (self-hosted: bootstrap_admin's) is the operator's: the full view.
    full = api(a["users"]["admin"]).get("system").json_body
    assert full["scope"] == "installation" and INSTALLATION_ONLY <= set(full) and "system" in full["jobs"]


def test_a_cloud_organization_gets_the_organization_view_even_when_first(a, api):
    from core import capabilities

    capabilities.set_edition(a["org"], "cloud")
    body = api(a["users"]["admin"]).get("system").json_body
    assert body["scope"] == "organization" and not INSTALLATION_ONLY & set(body)


@pytest.mark.parametrize("edition", ["enterprise", "community"])
def test_on_a_cloud_installation_the_first_organization_never_gets_the_installation_view(a, b, api, settings, edition):
    """On cloud the first organization is the first signup. Staff moving it to another edition (no billing)
    must not hand it the operator's view or the backup key acknowledgement."""
    from core import capabilities

    settings.SOFTWARDEN_CLOUD = True
    capabilities.set_edition(a["org"], edition)
    body = api(a["users"]["admin"]).get("system").json_body
    assert body["scope"] == "organization" and not INSTALLATION_ONLY & set(body)
    response = api(a["users"]["admin"]).post("system/backup/acknowledge-key")
    assert response.status_code == 403 and response.json_body["type"] == "/problems/forbidden"
    staff = make_staff_member(a["org"])  # staff still get it
    assert api(staff).get("system").json_body["scope"] == "installation"


def test_platform_staff_get_the_installation_view_in_any_organization(a, b, api):
    staff = make_staff_member(b["org"])
    body = api(staff).get("system").json_body
    assert body["scope"] == "installation" and INSTALLATION_ONLY <= set(body)
    assert {"organization", "system", "recent_problems"} == set(body["jobs"])


def test_the_platform_system_route_is_the_installation_view_for_staff_only(a, b, api):
    staff = make_staff_member()  # in no organization
    body = api(staff).get("platform/system").json_body
    assert body["scope"] == "installation" and INSTALLATION_ONLY <= set(body)
    assert set(body["jobs"]) == {"system", "recent_problems"}  # no organization's jobs
    for user in (a["users"]["admin"], b["users"]["admin"]):
        assert api(user).get("platform/system").status_code == 403


def test_the_organization_view_warns_only_about_its_own_unreadable_secrets(a, b, api, monkeypatch):
    """Another organization's secrets under a missing key don't show on this organization's System page."""
    from core import credentials
    from notify.models import TeamsDestination
    from tests.conftest import TEST_CREDENTIAL_KEY

    theirs = TeamsDestination(organization=a["org"], name="Restored", url_host="restore.e2e.test")
    credentials.slot_set(theirs, "url", "https://restore.e2e.test/hook/SECRET-PATH")
    theirs.save()
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", credentials.generate_key())

    def codes(user):
        return [w["code"] for w in api(user).get("system").json_body["warnings"]]

    assert codes(b["users"]["admin"]) == []  # a's secret is unreadable, not b's
    assert "stored_secrets_unreadable" in codes(a["users"]["admin"])  # the installation view
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", TEST_CREDENTIAL_KEY)
    mine = TeamsDestination(organization=b["org"], name="Mine", url_host="restore.e2e.test")
    credentials.slot_set(mine, "url", "https://restore.e2e.test/hook/MINE")
    mine.save()
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", credentials.generate_key())
    assert codes(b["users"]["admin"]) == ["stored_secrets_unreadable"]


def test_demo_accounts_with_the_published_password_are_a_warning_outside_debug(a, b, api, settings):
    """The installation view names `@example.test` accounts still on seed_demo's published password, outside
    DEBUG only; an organization's Admin on a shared installation (the organization view) doesn't get it."""
    from django.contrib.auth import get_user_model

    from core.demo import DEMO_PASSWORD

    get_user_model().objects.create_user("admin@example.test", "admin@example.test", DEMO_PASSWORD)
    codes = lambda user: [w["code"] for w in api(user).get("system").json_body["warnings"]]  # noqa: E731
    settings.DEBUG = True
    assert "demo_password" not in codes(a["users"]["admin"])  # expected in development
    settings.DEBUG = False
    body = api(a["users"]["admin"]).get("system").json_body
    assert body["scope"] == "installation"
    warning = next(w for w in body["warnings"] if w["code"] == "demo_password")
    assert "admin@example.test" in warning["message"] and DEMO_PASSWORD not in warning["message"]
    other = api(b["users"]["admin"]).get("system").json_body
    assert other["scope"] == "organization" and "demo_password" not in [w["code"] for w in other["warnings"]]
