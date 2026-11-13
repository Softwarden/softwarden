"""Shadow IT (discovery/entra_shadow_it.py) on the simulated tenant: third-party apps from the sign-in logs and
OAuth consent grants as discovery candidates with usage counts, the incremental cursor, throttling, the licence and
permission errors, the retention cap, what is (not) stored, the access check, and the overview API with its CSV."""

import csv
import io
import json
import re
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction

from catalog import org_lifecycle, services
from catalog.models import Resource
from core import connections, graph
from core.models import AuditEvent
from core.problems import FieldErrors
from discovery import access, entra_shadow_it, entra_simulator
from discovery import engine as discovery
from discovery.models import (
    Connector,
    ConnectorRun,
    Observation,
    ReconciliationDecision,
    RunStatus,
    ShadowItApp,
    ShadowItState,
    ShadowItUsageDay,
)

SECRET = "synthetic-secret"
NOW = datetime(2026, 11, 9, 0, 15, tzinfo=UTC)  # the first run reads up to midnight (NOW - LAG)
DAY = timedelta(days=1)
LAG = entra_shadow_it.LAG
START = datetime(2026, 10, 11, tzinfo=UTC)  # the first run's rolling window: Nov 9 and the 29 days before
SIGNINS = "/v1.0/auditLogs/signIns"


def app_id(name):
    return entra_simulator._id(f"app/{name}")


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """One clock for the source and the simulated tenant, and a fake monotonic clock that sleeping advances (no test
    ever waits). The sketch key is fixed, so distinct-user estimates are the same on every run of the suite."""
    state: dict[str, Any] = {"now": NOW, "mono": 1000.0, "slept": []}

    def sleep(seconds):
        state["slept"].append(seconds)
        state["mono"] += seconds

    monkeypatch.setattr(entra_shadow_it, "clock", lambda: state["now"])
    monkeypatch.setattr(entra_simulator, "clock", lambda: state["now"])
    monkeypatch.setattr(graph, "sleep", sleep)
    monkeypatch.setattr(graph, "monotonic", lambda: state["mono"])
    monkeypatch.setattr(entra_shadow_it.secrets, "token_bytes", lambda n: bytes(range(n)))
    return state


def make_shadow(org, actor, scope=None):
    data: dict[str, Any] = {"source": "entra_shadow_it", "tenant_id": "simulated", "client_id": "", "secret": SECRET}
    if scope:
        data["scope"] = scope
    connector = discovery.create_connector(org, actor, data)
    connections.run_test(org, actor, connector.microsoft_connection)
    return Connector.objects.get(pk=connector.pk)


def configure(connector, **config):
    connector.config = {**connector.config, **config}
    connector.save(update_fields=["config"])
    return connector


def run(connector, now=None, transport=None):
    connector.refresh_from_db()
    return discovery.run(connector, now=now or NOW, transport=transport)


@pytest.fixture
def shadow(a):
    return make_shadow(a["org"], a["users"]["admin"])


def obs(connector, name):
    return Observation.objects.get(connector=connector, display_name=name)


def summary(connector, name):
    return ShadowItApp.objects.get(connector=connector, display_name=name)


def expected(name, start, end):
    """(sign-ins, distinct users) the simulator holds for [start, end), as read while each was within retention."""
    return entra_simulator.expected_sign_ins(name, start, end, now=end)


def signin_windows(transport):
    """[(ge, le)] of every sign-in log read the transport answered, in order."""
    out = []
    for _method, url in transport.calls:
        parts = urllib.parse.urlsplit(url)
        if parts.path != SIGNINS:
            continue
        text = urllib.parse.parse_qs(parts.query)["$filter"][0]
        ge, le = re.findall(r"createdDateTime (?:ge|le) (\S+)", text)
        out.append((entra_shadow_it.parse_time(ge), entra_shadow_it.parse_time(le)))
    return out


# ---- the run -------------------------------------------------------------------------------------------------------


def test_a_run_surfaces_third_party_apps_with_usage_counts(shadow):
    result = run(shadow)
    assert result.status == RunStatus.SUCCEEDED, result.error
    # Microsoft Teams (sign-ins) and Azure Portal (a grant) are Microsoft's; Expense Hub is the tenant's own; Legacy
    # Portal has no enterprise app. The rest are third-party apps nobody registered.
    assert result.counts["skipped"] == {"microsoft": 2, "own_tenant": 1, "no_service_principal": 1}
    names = set(Observation.objects.filter(connector=shadow).values_list("display_name", flat=True))
    assert names == {"MeetNote AI", "FileBridge Sync", "ChartPilot", "QuickPoll", "Workday", "Zoom"}
    assert result.counts["new"] == 6 and result.counts["sign_in_windows"] == 10
    for name in ("MeetNote AI", "FileBridge Sync", "ChartPilot", "Workday", "Zoom"):
        sign_ins, users = expected(name, START, NOW - LAG)
        data = obs(shadow, name).data
        assert (data["sign_ins_30d"], data["users_30d"]) == (sign_ins, users), name
        row = summary(shadow, name)
        assert (row.sign_ins_30d, row.users_30d) == (sign_ins, users)
    assert obs(shadow, "MeetNote AI").data["users_30d"] == 6 and obs(shadow, "ChartPilot").data["users_30d"] == 9
    # Failed sign-ins (every 4th of MeetNote's) aren't use.
    assert result.counts["sign_ins_failed"] > 0
    # The newest sign-in before the cursor; ChartPilot's carry seven fractional digits.
    last = entra_shadow_it.parse_time(obs(shadow, "ChartPilot").data["last_sign_in_at"])
    assert last is not None and NOW - LAG - timedelta(hours=3) <= last < NOW - LAG
    assert summary(shadow, "ChartPilot").last_sign_in_at == last
    # Candidates in the ordinary queue, with their usage in `details`.
    meet = obs(shadow, "MeetNote AI")
    assert discovery.status_of(meet) == "new" and meet.data["enterprise_app"]
    assert meet.data["publisher"] == "MeetNote Inc. (synthetic)"
    assert ShadowItState.objects.get(connector=shadow).read_until == NOW - LAG


def test_consent_types_scopes_and_risky_scopes(shadow):
    run(shadow)
    meet = obs(shadow, "MeetNote AI").data
    assert (meet["consent"], meet["consenting_users"]) == ("user", 3)
    assert meet["scopes"] == ["Calendars.Read", "Mail.ReadWrite", "offline_access", "openid", "profile"]
    assert meet["risky_scopes"] == ["Mail.ReadWrite", "offline_access"]
    bridge = obs(shadow, "FileBridge Sync").data
    assert (bridge["consent"], bridge["consenting_users"]) == ("admin", 0)
    assert bridge["risky_scopes"] == ["Files.ReadWrite.All", "offline_access", "Sites.Read.All"]
    poll = obs(shadow, "QuickPoll").data  # consented, never signed in to in the window
    assert (poll["consent"], poll["consenting_users"], poll["risky_scopes"]) == ("user", 1, ["Directory.Read.All"])
    assert (poll["users_30d"], poll["sign_ins_30d"], poll["last_sign_in_at"]) == (0, 0, "")
    chart = obs(shadow, "ChartPilot").data  # SSO only: people sign in, nobody granted scopes
    assert (chart["consent"], chart["scopes"], chart["risky_scopes"]) == ("none", [], [])
    zoom = obs(shadow, "Zoom").data
    assert zoom["consent"] == "admin" and zoom["risky_scopes"] == []
    assert summary(shadow, "FileBridge Sync").risky and not summary(shadow, "ChartPilot").risky


@pytest.mark.parametrize("risky", ["Directory.ReadWrite.All", "RoleManagement.Read.Directory", "MAIL.READWRITE"])
def test_high_risk_scope_rules(risky):
    assert entra_shadow_it.is_risky(risky)
    assert not entra_shadow_it.is_risky("User.Read") and not entra_shadow_it.is_risky("openid")


def test_apps_already_in_the_catalog_are_not_candidates(a, shadow):
    org, editor = a["org"], a["users"]["editor"]
    # Workday is a record with its application ID (any case); Zoom is an Entra connector's observation.
    services.create_resource(
        org,
        editor,
        {"name": "Workday HR", "kind": "application", "lifecycle": "active", "entra_app_id": app_id("Workday").upper()},
    )
    entra_connector = discovery.create_connector(
        org, a["users"]["admin"], {"name": "Entra", "tenant_id": "simulated", "client_id": "", "secret": SECRET}
    )
    connections.run_test(org, a["users"]["admin"], entra_connector.microsoft_connection)
    assert discovery.run(Connector.objects.get(pk=entra_connector.pk)).status == RunStatus.SUCCEEDED
    result = run(shadow)
    assert result.counts["skipped"]["in_catalog"] == 2 and result.counts["in_catalog"] == 2
    assert not Observation.objects.filter(connector=shadow, display_name__in=["Workday", "Zoom"]).exists()
    # Kept in the overview, with their usage, as linked or as the Entra source's candidate.
    workday = summary(shadow, "Workday")
    assert workday.in_catalog and workday.sign_ins_30d == expected("Workday", START, NOW - LAG)[0]
    rows = {r["name"]: r for r in api_list(a, "discovery/shadow-it")}
    assert rows["Workday"]["status"] == "linked" and rows["Workday"]["record"]["name"] == "Workday HR"
    assert rows["Zoom"]["status"] == "new" and rows["Zoom"]["candidate"]["source"] == "entra"
    assert rows["Zoom"]["candidate"]["id"] == str(
        Observation.objects.get(connector=entra_connector, display_name="Zoom").id
    )
    assert rows["MeetNote AI"]["candidate"]["source"] == "entra_shadow_it"


def test_a_candidate_created_here_stays_this_connectors_and_keeps_updating(a, shadow, clock):
    run(shadow)
    meet = obs(shadow, "MeetNote AI")
    _obs, record = discovery.create_record(a["org"], a["users"]["editor"], meet, {})
    assert record.application.entra_app_id == app_id("MeetNote AI")  # prefilled from the candidate
    clock["now"] = NOW + DAY
    result = run(shadow, now=NOW + DAY)
    assert "in_catalog" not in result.counts["skipped"]
    meet.refresh_from_db()
    assert meet.out_of_scope == "" and meet.last_seen_at == NOW + DAY and discovery.status_of(meet) == "linked"
    row = next(r for r in api_list(a, "discovery/shadow-it") if r["name"] == "MeetNote AI")
    assert row["status"] == "linked" and row["record"]["id"] == str(record.id)


def test_first_party_and_own_apps_leave_nothing_behind(shadow):
    run(shadow)
    for name in ("Microsoft Teams", "Azure Portal", "Expense Hub", "Legacy Portal"):
        assert not Observation.objects.filter(display_name=name).exists()
        assert not ShadowItApp.objects.filter(app_id=app_id(name)).exists()
        assert not ShadowItUsageDay.objects.filter(app_id=app_id(name)).exists()


def test_the_first_party_list_is_the_scope(a, shadow):
    discovery.update_connector(
        a["org"], a["users"]["admin"], shadow, {"scope": {"first_party_tenants": [entra_simulator.VENDOR_TENANT]}}
    )
    result = run(shadow)
    # Workday and Zoom's publisher is now first party; Microsoft's own apps are third party.
    assert not Observation.objects.filter(connector=shadow, display_name__in=["Workday", "Zoom"]).exists()
    assert Observation.objects.filter(connector=shadow, display_name="Microsoft Teams").exists()
    assert result.counts["skipped"]["microsoft"] == 2


def test_a_second_run_adds_only_new_sign_ins(a, shadow, clock):
    run(shadow)
    clock["now"] = NOW + DAY
    sim = entra_simulator.Transport(shadow, shadow=True)
    result = run(shadow, now=NOW + DAY, transport=sim)
    assert result.status == RunStatus.SUCCEEDED, result.error
    # Read from the cursor (midnight) to the new cursor, one window: nothing is read twice.
    assert signin_windows(sim) == [(NOW - LAG, NOW + DAY - LAG)]
    # Only the usage changed: no candidate counts as changed (VOLATILE), so the assistant isn't queued for nothing.
    assert result.counts["sign_in_windows"] == 1
    assert (result.counts["unchanged"], result.counts["changed"]) == (6, 0)
    # The rolling window is exactly 30 UTC days: from Oct 12 (today, Nov 10, and the 29 days before) to the cursor.
    start = entra_shadow_it.window_start(NOW + DAY)
    assert start == datetime(2026, 10, 12, tzinfo=UTC)
    for name in ("MeetNote AI", "FileBridge Sync", "ChartPilot", "Workday", "Zoom"):
        sign_ins, users = expected(name, start, NOW + DAY - LAG)
        row = summary(shadow, name)
        assert (row.sign_ins_30d, row.users_30d) == (sign_ins, users), name
        assert obs(shadow, name).data["sign_ins_30d"] == sign_ins
    # Zoom signs in at midnight, the first run's end: counted by the second run only, once.
    days = ShadowItUsageDay.objects.filter(connector=shadow, app_id=app_id("Zoom"))
    assert days.get(day=NOW.date()).sign_ins == 1
    assert sum(d.sign_ins for d in days) == expected("Zoom", start, NOW + DAY - LAG)[0]
    assert result.counts["usage_days_pruned"] > 0 and not days.filter(day__lt=start.date()).exists()
    # Run again at once: nothing new to read, no count changes.
    sim = entra_simulator.Transport(shadow, shadow=True)
    again = run(shadow, now=NOW + DAY, transport=sim)
    assert signin_windows(sim) == [] and again.counts["sign_ins_counted"] == 0
    assert again.counts["changed"] == 0 and again.counts["unchanged"] == 6
    assert summary(shadow, "Zoom").sign_ins_30d == expected("Zoom", start, NOW + DAY - LAG)[0]
    assert ShadowItState.objects.get(connector=shadow).read_until == NOW + DAY - LAG
    # Five minutes later, the five minutes since the cursor: Zoom's next midnight sign-in, once.
    clock["now"] = NOW + DAY + timedelta(minutes=5)
    sim = entra_simulator.Transport(shadow, shadow=True)
    later = run(shadow, now=NOW + DAY + timedelta(minutes=5), transport=sim)
    assert signin_windows(sim) == [(NOW + DAY - LAG, NOW + DAY - LAG + timedelta(minutes=5))]
    assert summary(shadow, "Zoom").sign_ins_30d == expected("Zoom", start, NOW + DAY - LAG)[0] + 1
    assert later.counts["sign_ins_counted"] == 2  # Zoom's, and ChartPilot's at 00:01:00.123456


def test_a_failed_run_stores_nothing(a, shadow, clock):
    run(shadow)
    before = list(ShadowItUsageDay.objects.values_list("app_id", "day", "sign_ins").order_by("app_id", "day"))
    configure(shadow, simulated_errors={"/v1.0/oauth2PermissionGrants": 500})
    clock["now"] = NOW + DAY
    result = run(shadow, now=NOW + DAY)
    assert result.status == RunStatus.FAILED
    assert list(ShadowItUsageDay.objects.values_list("app_id", "day", "sign_ins").order_by("app_id", "day")) == before
    assert ShadowItState.objects.get(connector=shadow).read_until == NOW - LAG
    configure(shadow, simulated_errors={})
    assert run(shadow, now=NOW + DAY).status == RunStatus.SUCCEEDED
    assert (
        summary(shadow, "Zoom").sign_ins_30d
        == expected("Zoom", entra_shadow_it.window_start(NOW + DAY), NOW + DAY - LAG)[0]
    )


def test_a_throttled_window_is_waited_out_at_the_same_size(shadow, clock):
    configure(shadow, simulated_throttle={"path": SIGNINS, "times": 2, "retry_after": "3"})
    sim = entra_simulator.Transport(shadow, shadow=True)
    result = run(shadow, transport=sim)
    assert result.status == RunStatus.SUCCEEDED, result.error
    windows = signin_windows(sim)
    # Two throttled reads of the first window, each waited out (Retry-After), then the same window again.
    assert windows[:3] == [(START, START + timedelta(days=3))] * 3
    assert all(le - ge == timedelta(days=3) for ge, le in windows[:-1])
    assert clock["slept"] == [3.0, 3.0]
    assert state_of(shadow).window_minutes == 3 * 24 * 60  # nothing smaller stored
    assert not any("windows of" in n for n in result.counts.get("notes", []))
    # The same counts as an unthrottled read: nothing lost or counted twice.
    sign_ins, users = expected("ChartPilot", START, NOW - LAG)
    assert (summary(shadow, "ChartPilot").sign_ins_30d, summary(shadow, "ChartPilot").users_30d) == (sign_ins, users)


def test_a_throttled_read_that_cant_wait_fails_readably(shadow):
    configure(shadow, simulated_throttle={"path": SIGNINS, "times": 5, "retry_after": "600"})
    result = run(shadow)
    assert result.status == RunStatus.FAILED
    assert "asked to wait 600 seconds" in result.error and "sign-in logs" in result.error
    assert not Observation.objects.filter(connector=shadow).exists()
    assert not ShadowItState.objects.filter(connector=shadow).exists()


def test_a_tenant_without_p1_fails_with_the_licence_named(a, shadow):
    configure(shadow, simulated_no_premium=True)
    result = run(shadow)
    assert result.status == RunStatus.FAILED
    assert entra_shadow_it.LICENCE_CODE in result.error and "P1 or P2" in result.error
    assert "Read sign-in logs" in result.error and "application permission" not in result.error
    assert not Observation.objects.filter(connector=shadow).exists()
    # Consent grants only, with the sign-in logs off.
    discovery.update_connector(a["org"], a["users"]["admin"], shadow, {"scope": {"sign_ins": False}})
    result = run(shadow)
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert set(Observation.objects.filter(connector=shadow).values_list("display_name", flat=True)) == {
        "MeetNote AI",
        "FileBridge Sync",
        "QuickPoll",
        "Zoom",
    }
    assert obs(shadow, "MeetNote AI").data["sign_ins_read"] is False
    assert any("weren't read" in n for n in result.counts["notes"])
    assert ShadowItState.objects.get(connector=shadow).read_until is None


@pytest.mark.parametrize(
    "granted, missing, what",
    [
        (["Directory.Read.All"], "AuditLog.Read.All", "read the sign-in logs"),
        (["AuditLog.Read.All"], "Directory.Read.All", "read the tenant's ID"),
        (["AuditLog.Read.All", "Application.Read.All"], "Directory.Read.All", "read the tenant's ID"),
    ],
)
def test_a_missing_permission_is_named(shadow, granted, missing, what):
    configure(shadow, simulated_permissions=granted)
    result = run(shadow)
    assert result.status == RunStatus.FAILED
    assert f"application permission {missing} with admin consent" in result.error and what in result.error
    assert "Authorization_RequestDenied" in result.error


def test_reading_stops_at_the_retention(a, shadow, clock):
    run(shadow)
    later = NOW + timedelta(days=45)
    clock["now"] = later
    sim = entra_simulator.Transport(shadow, shadow=True)
    result = run(shadow, now=later, transport=sim)
    assert result.status == RunStatus.SUCCEEDED, result.error
    windows = signin_windows(sim)
    assert windows[0][0] == entra_shadow_it.window_start(later)  # the rolling window, though the cursor is older
    assert windows[-1][1] == later - LAG
    assert any("past Entra's 30-day retention" in n for n in result.counts["notes"])
    # Only whole days of the new window remain; the old run's days are gone.
    assert not ShadowItUsageDay.objects.filter(day__lt=entra_shadow_it.window_start(later).date()).exists()
    sign_ins, users = expected("ChartPilot", entra_shadow_it.window_start(later), later - LAG)
    assert (summary(shadow, "ChartPilot").sign_ins_30d, summary(shadow, "ChartPilot").users_30d) == (sign_ins, users)


def test_no_user_identifier_is_stored(a, shadow):
    run(shadow)
    # The usage table holds counts and a sketch, nothing else.
    columns = {f.column for f in ShadowItUsageDay._meta.concrete_fields}
    assert columns == {
        "id",
        "organization_id",
        "created_at",
        "updated_at",
        "connector_id",
        "app_id",
        "day",
        "sign_ins",
        "users",
        "users_sketch",
    }
    users = [entra_simulator.sign_in_user(k) for k in range(10)]
    sketches = [bytes(d.users_sketch) for d in ShadowItUsageDay.objects.all()]
    stored = json.dumps(
        [
            list(ShadowItUsageDay.objects.values("app_id", "day", "sign_ins", "users")),
            list(ShadowItApp.objects.values()),
            list(Observation.objects.filter(connector=shadow).values("display_name", "data")),
            list(ConnectorRun.objects.filter(connector=shadow).values("counts", "error")),
            list(AuditEvent.objects.values("prechange", "postchange")),
            [s.hex() for s in sketches],
        ],
        default=str,
    ).lower()
    sample = entra_simulator.sign_ins(NOW - DAY, NOW)
    for value in [
        *users,
        *(u.replace("-", "") for u in users),
        "contoso.example.test",
        "synthetic user",
        "203.0.113.",
        "synthetic city",
        sample[0]["id"],
        sample[0]["correlationId"],
    ]:
        assert value.lower() not in stored, value
    for sketch in sketches:  # a sketch is registers, not hashes: no user's keyed hash appears in it
        registers = entra_shadow_it.Sketch.load(sketch).registers
        assert len(registers) == entra_shadow_it.SKETCH_M and max(registers) < 64


def test_the_sketch_counts_distinct_users_and_merges():
    key = b"k" * 32
    sketch, other = entra_shadow_it.Sketch(), entra_shadow_it.Sketch()
    for n in range(40):
        sketch.add(entra_shadow_it.user_hash(key, f"user-{n}"))
    for n in range(20, 70):
        other.add(entra_shadow_it.user_hash(key, f"USER-{n}"))  # the same users, another case
    assert abs(sketch.estimate() - 40) <= 1 and abs(other.estimate() - 50) <= 1
    sketch.merge(other)
    assert abs(sketch.estimate() - 70) <= 1
    assert entra_shadow_it.Sketch.load(sketch.dump()).registers == sketch.registers
    assert entra_shadow_it.Sketch.load(b"not a sketch").estimate() == 0


def test_the_sketch_key_never_leaves_the_database(shadow):
    run(shadow)
    assert "sketch_key" not in org_lifecycle.exported_columns(ShadowItState)
    assert "users_sketch" in org_lifecycle.exported_columns(ShadowItUsageDay)
    assert ShadowItState in org_lifecycle.tenant_models() and ShadowItUsageDay in org_lifecycle.tenant_models()


def test_dismissed_candidates_return_on_new_risk_not_on_usage(a, shadow, clock, monkeypatch):
    run(shadow)
    chart = obs(shadow, "ChartPilot")
    discovery.dismiss(a["org"], a["users"]["editor"], chart, {"reason": "approved informally"})
    clock["now"] = NOW + DAY
    run(shadow, now=NOW + DAY)
    chart.refresh_from_db()
    assert discovery.status_of(chart) == "dismissed"  # more sign-ins don't bring it back
    rows = {r["name"]: r for r in api_list(a, "discovery/shadow-it")}
    assert rows["ChartPilot"]["status"] == "dismissed"
    # A new high-risk scope does.
    monkeypatch.setattr(
        entra_simulator, "GRANTS", [*entra_simulator.GRANTS, ("ChartPilot", "Principal", 5, "Files.ReadWrite.All")]
    )
    run(shadow, now=NOW + DAY)
    chart.refresh_from_db()
    assert discovery.status_of(chart) == "changed"
    assert next(r for r in api_list(a, "discovery/shadow-it") if r["name"] == "ChartPilot")["status"] == "new"


def test_losing_a_scope_or_a_consent_never_revives_a_dismissal(a, shadow, monkeypatch):
    run(shadow)
    bridge = obs(shadow, "FileBridge Sync")  # admin consent, Files.ReadWrite.All, Sites.Read.All, offline_access
    discovery.dismiss(a["org"], a["users"]["editor"], bridge, {"reason": "approved by security"})
    grants = [g for g in entra_simulator.GRANTS if g[0] != "FileBridge Sync"]
    # Down to one user's consent with fewer scopes: less risk, still dismissed.
    monkeypatch.setattr(entra_simulator, "GRANTS", [*grants, ("FileBridge Sync", "Principal", 1, "offline_access")])
    result = run(shadow)
    bridge.refresh_from_db()
    assert discovery.status_of(bridge) == "dismissed" and result.counts["changed"] == 1
    assert bridge.data["consent"] == "user" and bridge.data["risky_scopes"] == ["offline_access"]
    assert bridge.data["risk_seen"]["consent"] == "admin"  # the high-water mark stays
    # Back to what it had when dismissed: nothing new, still dismissed.
    monkeypatch.setattr(
        entra_simulator, "GRANTS", [*grants, ("FileBridge Sync", "AllPrincipals", None, "Files.ReadWrite.All")]
    )
    run(shadow)
    bridge.refresh_from_db()
    assert discovery.status_of(bridge) == "dismissed"
    # A high-risk scope it never had: back in the queue.
    monkeypatch.setattr(entra_simulator, "GRANTS", [*grants, ("FileBridge Sync", "Principal", 1, "Mail.Send")])
    run(shadow)
    bridge.refresh_from_db()
    assert discovery.status_of(bridge) == "changed"


def test_a_user_consent_rising_to_admin_revives_a_dismissal(a, shadow, monkeypatch):
    run(shadow)
    meet = obs(shadow, "MeetNote AI")
    discovery.dismiss(a["org"], a["users"]["editor"], meet, {"reason": "pilot"})
    monkeypatch.setattr(
        entra_simulator, "GRANTS", [*entra_simulator.GRANTS, ("MeetNote AI", "AllPrincipals", None, "openid")]
    )
    run(shadow)
    meet.refresh_from_db()
    assert discovery.status_of(meet) == "changed"


def test_the_summary_fields_are_one_list():
    values = entra_shadow_it.map_object({"app_id": app_id("ChartPilot"), "servicePrincipal": {"id": "x"}})
    assert tuple(entra_shadow_it._app_fields(values, None, NOW)) == entra_shadow_it.APP_FIELDS


# ---- windows: too big, out of time, failures --------------------------------------------------------------------


class Slow:
    """The simulated tenant, with every sign-in log read taking `seconds` of the (fake) monotonic clock."""

    def __init__(self, connector, clock, seconds):
        self.sim = entra_simulator.Transport(connector, shadow=True)
        self.clock, self.seconds = clock, seconds

    @property
    def calls(self):
        return self.sim.calls

    def send(self, method, url, headers, body=None):
        if urllib.parse.urlsplit(url).path == SIGNINS:
            self.clock["mono"] += self.seconds
        return self.sim.send(method, url, headers, body)


def state_of(connector):
    return ShadowItState.objects.get(connector=connector)


def test_a_window_with_too_many_pages_is_halved(shadow, monkeypatch):
    monkeypatch.setattr(entra_shadow_it, "WINDOW_PAGES", 2)
    configure(shadow, simulated_signin_page=10)  # a 3-day window is about 10 pages
    sim = entra_simulator.Transport(shadow, shadow=True)
    result = run(shadow, transport=sim)
    assert result.status == RunStatus.SUCCEEDED, result.error
    sizes = [le - ge for ge, le in signin_windows(sim)]
    assert sizes[0] == timedelta(days=3) and min(sizes) < timedelta(hours=24)
    assert any("too many sign-ins for one read" in n for n in result.counts["notes"])
    assert state_of(shadow).read_until == NOW - LAG  # every window was read in the end
    for name in ("MeetNote AI", "ChartPilot", "Zoom"):
        assert summary(shadow, name).sign_ins_30d == expected(name, START, NOW - LAG)[0], name


def test_too_many_sign_ins_in_an_hour_fail_readably(shadow, monkeypatch):
    monkeypatch.setattr(entra_shadow_it, "WINDOW_PAGES", 1)
    configure(shadow, simulated_signin_page=1)
    result = run(shadow)
    assert result.status == RunStatus.FAILED and "sign-ins in one hour" in result.error
    assert not ShadowItState.objects.filter(connector=shadow).exists()


def test_reading_stops_at_the_runs_time_and_keeps_complete_windows(shadow, clock):
    configure(shadow, simulated_signin_page=40)  # three pages a 3-day window
    slow = Slow(shadow, clock, 300.0)  # 6 pages fit in READ_FOR (30 minutes)
    result = run(shadow, transport=slow)
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.counts["sign_in_windows"] == 2
    state = state_of(shadow)
    assert state.read_until == START + timedelta(days=6)
    assert state.window_minutes == 36 * 60  # halved for the next run
    assert any("counted up to" in n and "18 hours" not in n for n in result.counts["notes"])
    assert summary(shadow, "Zoom").sign_ins_30d == expected("Zoom", START, START + timedelta(days=6))[0]


def test_the_runs_hard_deadline_stops_a_window_like_its_time(shadow, clock, monkeypatch):
    monkeypatch.setattr(entra_shadow_it, "READ_FOR", 10_000.0)  # past the 40-minute run deadline
    configure(shadow, simulated_signin_page=40)
    result = run(shadow, transport=Slow(shadow, clock, 300.0))  # 8 pages reach the deadline
    assert result.status == RunStatus.SUCCEEDED, result.error
    # Nine pages ran before the deadline was passed: three windows; the fourth stopped at the deadline.
    assert result.counts["sign_in_windows"] == 3 and state_of(shadow).read_until == START + timedelta(days=9)
    assert state_of(shadow).window_minutes == 36 * 60


def test_a_large_tenant_eventually_advances(shadow, clock):
    configure(shadow, simulated_signin_page=10)  # about 10 pages a 3-day window
    # Six pages fit a run: no 3-day window ever completes, so the first run reads none and halves the window.
    first = run(shadow, transport=Slow(shadow, clock, 300.0))
    assert first.status == RunStatus.SUCCEEDED, first.error
    assert first.counts["sign_in_windows"] == 0 and state_of(shadow).read_until is None
    assert state_of(shadow).window_minutes == 36 * 60
    assert any("No complete window" in n for n in first.counts["notes"])
    cursors = []
    for _ in range(80):
        result = run(shadow, transport=Slow(shadow, clock, 300.0))
        assert result.status == RunStatus.SUCCEEDED, result.error
        cursors.append(state_of(shadow).read_until)
        if cursors[-1] == NOW - LAG:
            break
    assert cursors[-1] == NOW - LAG and cursors == sorted(cursors) and len(set(cursors)) == len(cursors)
    for name in ("MeetNote AI", "ChartPilot", "Zoom"):
        sign_ins, users = expected(name, START, NOW - LAG)
        assert (summary(shadow, name).sign_ins_30d, summary(shadow, name).users_30d) == (sign_ins, users), name


def test_a_later_window_failing_keeps_the_complete_ones(shadow):
    configure(shadow, simulated_signin_page=1000, simulated_fail_after={"path": SIGNINS, "after": 3, "status": 500})
    result = run(shadow)
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.counts["sign_in_windows"] == 3 and state_of(shadow).read_until == START + timedelta(days=9)
    assert any("500" in n and "counted up to" in n for n in result.counts["notes"])
    # The next run continues from there, and counts every sign-in once.
    configure(shadow, simulated_fail_after={})
    run(shadow)
    assert state_of(shadow).read_until == NOW - LAG
    assert summary(shadow, "Zoom").sign_ins_30d == expected("Zoom", START, NOW - LAG)[0]


def test_the_first_window_failing_fails_the_run(shadow):
    configure(shadow, simulated_fail_after={"path": SIGNINS, "after": 0, "status": 502})
    result = run(shadow)
    assert result.status == RunStatus.FAILED and "502" in result.error
    assert not ShadowItState.objects.filter(connector=shadow).exists()


@pytest.mark.parametrize("after", [0, 1])  # the first page of a window, or a later one
def test_a_504_halves_the_window(shadow, clock, after):
    configure(shadow, simulated_signin_page=40, simulated_fail_after={"path": SIGNINS, "after": after, "status": 504})
    sim = entra_simulator.Transport(shadow, shadow=True)
    result = run(shadow, transport=sim)
    assert result.status == RunStatus.SUCCEEDED, result.error
    windows = signin_windows(sim)
    retried = [le - ge for ge, le in windows if ge == START]
    assert retried[0] == timedelta(days=3) and retried[-1] == timedelta(days=1.5)
    assert clock["slept"] == [2.0]  # backoff: a 504 carries no Retry-After
    assert summary(shadow, "Zoom").sign_ins_30d == expected("Zoom", START, NOW - LAG)[0]


def test_another_run_meanwhile_applies_nothing(shadow, monkeypatch):
    run(shadow)
    original = entra_shadow_it.fetch

    def fetch_then_race(connector, credential, transport=None):
        found = original(connector, credential, transport)
        ShadowItState.objects.filter(connector=connector).update(read_until=NOW - DAY)  # another run moved it
        return found

    monkeypatch.setattr(entra_shadow_it, "fetch", fetch_then_race)
    before = list(ShadowItUsageDay.objects.values_list("app_id", "day", "sign_ins").order_by("app_id", "day"))
    result = run(shadow, now=NOW)
    assert result.status == RunStatus.FAILED and "Another run" in result.error
    assert list(ShadowItUsageDay.objects.values_list("app_id", "day", "sign_ins").order_by("app_id", "day")) == before


def test_an_app_no_longer_used_or_consented_is_inactive_not_gone(a, shadow, monkeypatch):
    run(shadow)
    poll = obs(shadow, "QuickPoll")
    _obs, record = discovery.create_record(a["org"], a["users"]["editor"], poll, {})
    # QuickPoll is there for its consent grant only; the grant is revoked. Its enterprise app is still in the tenant.
    granted = list(entra_simulator.GRANTS)
    monkeypatch.setattr(entra_simulator, "GRANTS", [g for g in entra_simulator.GRANTS if g[0] != "QuickPoll"])
    result = run(shadow)
    assert result.counts["gone"] == 0 and result.counts["skipped"]["inactive"] == 1
    poll.refresh_from_db()
    assert poll.gone_at is None and poll.out_of_scope == "inactive" and discovery.status_of(poll) == "linked"
    # So its record is never "no longer seen in source".
    assert not Observation.objects.filter(discovery.missing_q()).exists()
    assert not ShadowItApp.objects.filter(connector=shadow, display_name="QuickPoll").exists()
    # Consented again: back in scope.
    monkeypatch.setattr(entra_simulator, "GRANTS", granted)
    run(shadow)
    poll.refresh_from_db()
    assert poll.out_of_scope == "" and Resource.objects.filter(id=record.id, archived_at__isnull=True).exists()


def test_an_app_removed_from_the_tenant_goes(shadow, monkeypatch):
    run(shadow)
    monkeypatch.setattr(entra_simulator, "SHADOW_ROWS", [r for r in entra_simulator.SHADOW_ROWS if r[0] != "QuickPoll"])
    monkeypatch.setattr(entra_simulator, "GRANTS", [g for g in entra_simulator.GRANTS if g[0] != "QuickPoll"])
    result = run(shadow)
    assert result.counts["gone"] == 1 and obs(shadow, "QuickPoll").gone_at is not None


def without(monkeypatch, name):
    """The simulated tenant without the app's enterprise app (deleted); its sign-ins stay in the logs."""
    monkeypatch.setattr(entra_simulator, "SHADOW_ROWS", [r for r in entra_simulator.SHADOW_ROWS if r[0] != name])


@pytest.mark.parametrize("later", [DAY, timedelta(days=31)])
def test_a_deleted_app_goes_with_the_sign_in_logs_off(a, shadow, clock, monkeypatch, later):
    run(shadow)
    chart = obs(shadow, "ChartPilot")  # sign-ins only: usage days stored, no consent grant
    discovery.link(a["org"], a["users"]["editor"], chart, {"resource": str(a["app"].id)})
    discovery.update_connector(a["org"], a["users"]["admin"], shadow, {"scope": {"sign_ins": False}})
    without(monkeypatch, "ChartPilot")
    clock["now"] = NOW + later
    result = run(shadow, now=NOW + later)
    assert result.status == RunStatus.SUCCEEDED, result.error
    chart.refresh_from_db()
    assert chart.gone_at == NOW + later and chart.out_of_scope == "" and result.counts["gone"] == 1
    # Its linked record is "no longer seen in source".
    assert list(Observation.objects.filter(discovery.missing_q()).values_list("id", flat=True)) == [chart.id]
    assert not ShadowItApp.objects.filter(connector=shadow, app_id=app_id("ChartPilot")).exists()


def test_a_deleted_app_with_recent_use_goes(a, shadow, clock, monkeypatch):
    run(shadow)
    meet = obs(shadow, "MeetNote AI")  # sign-ins and consent grants, both still in the logs and the directory
    _obs, record = discovery.create_record(a["org"], a["users"]["editor"], meet, {})
    without(monkeypatch, "MeetNote AI")
    clock["now"] = NOW + DAY
    result = run(shadow, now=NOW + DAY)
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.counts["sign_ins_counted"] > 0  # it was still signed in to
    meet.refresh_from_db()
    assert meet.gone_at == NOW + DAY and result.counts["gone"] == 1
    assert list(Observation.objects.filter(discovery.missing_q()).values_list("id", flat=True)) == [meet.id]
    assert Resource.objects.filter(id=record.id, archived_at__isnull=True).exists()  # never touched
    # Unobserved apps without an enterprise app are still only counted (Legacy Portal).
    assert result.counts["skipped"]["no_service_principal"] == 1


def test_a_window_grows_back_only_when_a_doubled_one_fits(shadow, clock):
    configure(shadow, simulated_signin_page=40)  # an 18-hour window is one page, a 36-hour one two
    ShadowItState.objects.create(
        organization=shadow.organization, connector=shadow, sketch_key=bytes(range(32)), window_minutes=18 * 60
    )
    slow = Slow(shadow, clock, 300.0)  # six pages of 300 s fit in a run
    result = run(shadow, transport=slow)
    assert result.status == RunStatus.SUCCEEDED, result.error
    sizes = [le - ge for ge, le in dict.fromkeys(signin_windows(slow))]  # one entry per window, not per page
    # 18 hours (1 page; 5 left, a doubled 2-page window fits) grows to 36 hours. That took 2 pages, leaving 3: a
    # doubled 4-page window wouldn't fit, so the next window stays at 36 hours.
    assert sizes[:3] == [timedelta(hours=18), timedelta(hours=36), timedelta(hours=36)]


def test_with_sign_ins_off_apps_without_consent_are_not_read(a, shadow, clock):
    run(shadow)
    discovery.update_connector(a["org"], a["users"]["admin"], shadow, {"scope": {"sign_ins": False}})
    later = NOW + timedelta(days=31)  # the usage days earlier runs counted have aged out
    clock["now"] = later
    result = run(shadow, now=later)
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.counts["gone"] == 0 and result.counts["skipped"]["not_read"] == 2
    for name in ("ChartPilot", "Workday"):  # sign-ins only, no consent grant
        found = obs(shadow, name)
        assert found.gone_at is None and found.out_of_scope == "not_read"
    assert obs(shadow, "MeetNote AI").out_of_scope == ""  # its consent grant is still read
    assert not hasattr(entra_shadow_it, "scope_reason")  # an unlisted observation is gone: its app was deleted


def test_scope_validation(a):
    with pytest.raises(FieldErrors) as refused:
        make_shadow(a["org"], a["users"]["admin"], scope={"sign_ins": "yes", "first_party_tenants": ["nope"]})
    assert set(refused.value.errors) == {"scope.sign_ins", "scope.first_party_tenants"}
    with pytest.raises(FieldErrors) as refused:
        make_shadow(a["org"], a["users"]["admin"], scope={"include_agent_identities": True})
    assert "scope" in refused.value.errors


# ---- the access check ----------------------------------------------------------------------------------------------


def steps(result):
    return {s["name"]: s for s in result["steps"]}


def test_access_check_passes_and_names_each_permission(a, shadow):
    result = access.check_access(a["org"], a["users"]["admin"], shadow).access_check
    assert result["ok"] and result["missing"] == []
    by = steps(result)
    assert by["Read sign-in logs"]["permission"] == "AuditLog.Read.All" and by["Read sign-in logs"]["state"] == "ok"
    assert by["Read OAuth consent grants"]["permission"] == "Directory.Read.All"
    configure(shadow, simulated_permissions=["Directory.Read.All"])
    result = access.check_access(a["org"], a["users"]["admin"], shadow).access_check
    assert not result["ok"] and result["missing"] == ["AuditLog.Read.All"]
    configure(shadow, simulated_permissions=["AuditLog.Read.All"])
    result = access.check_access(a["org"], a["users"]["admin"], shadow).access_check
    assert result["missing"] == ["Directory.Read.All"]


def test_access_check_names_the_missing_licence(a, shadow):
    configure(shadow, simulated_no_premium=True)
    result = access.check_access(a["org"], a["users"]["admin"], shadow).access_check
    step = steps(result)["Read sign-in logs"]
    assert not result["ok"] and result["missing"] == []
    assert (step["state"], step["problem"], step["problem_label"]) == ("failed", "missing_licence", "Missing license")
    assert "P1 or P2" in step["detail"] and "Read sign-in logs" in step["next"]
    discovery.update_connector(a["org"], a["users"]["admin"], shadow, {"scope": {"sign_ins": False}})
    shadow.refresh_from_db()
    result = access.check_access(a["org"], a["users"]["admin"], shadow).access_check
    assert result["ok"] and steps(result)["Read sign-in logs"]["state"] == "not_used"


def test_the_source_lists_its_permissions(a, api):
    sources = {s["id"]: s for s in api(a["users"]["viewer"]).get("discovery/sources").json_body}
    shadow = sources["entra_shadow_it"]
    assert shadow["capability"] == "discovery.entra" and shadow["available"]
    assert [p["name"] for p in shadow["permissions"]] == ["AuditLog.Read.All", "Directory.Read.All"]
    assert shadow["scope_defaults"]["sign_ins"] is True and "in_catalog" in shadow["filters"]


# ---- the overview API --------------------------------------------------------------------------------------------


def api_list(a, url, role="viewer"):
    from tests.conftest import Api

    response = Api(a["users"][role]).get(url)
    assert response.status_code == 200, response.content
    return response.json_body["results"]


def test_overview_lists_apps_with_usage_consent_and_status(a, api, shadow):
    run(shadow)
    body = api(a["users"]["viewer"]).get("discovery/shadow-it").json_body
    rows = body["results"]
    assert [r["users_30d"] for r in rows] == sorted((r["users_30d"] for r in rows), reverse=True)
    meet = next(r for r in rows if r["name"] == "MeetNote AI")
    assert meet["app_id"] == app_id("MeetNote AI") and meet["status"] == "new" and meet["record"] is None
    assert (meet["consent"], meet["consenting_users"], meet["risky"]) == ("user", 3, True)
    assert meet["risky_scopes"] == ["Mail.ReadWrite", "offline_access"]
    assert meet["candidate"]["id"] == str(obs(shadow, "MeetNote AI").id) and meet["candidate"]["status"] == "new"
    assert meet["connector"] == {"id": str(shadow.id), "name": shadow.name}
    assert meet["last_sign_in_at"].endswith("Z")
    # The candidates queue shows the usage too.
    queue = api(a["users"]["viewer"]).get(f"discovery/candidates?connector={shadow.id}").json_body
    detail = next(r for r in queue["results"] if r["name"] == "MeetNote AI")["details"]
    assert detail["users_30d"] == 6 and detail["consent"] == "user"


@pytest.mark.parametrize(
    "query, names",
    [
        ("status=new", {"MeetNote AI", "FileBridge Sync", "ChartPilot", "QuickPoll", "Zoom"}),
        ("status=linked", {"Workday"}),
        ("status=linked,new&consent=admin", {"FileBridge Sync", "Zoom"}),
        ("consent=none", {"ChartPilot", "Workday"}),
        ("risky=true", {"MeetNote AI", "FileBridge Sync", "QuickPoll"}),
        ("risky=false&min_users=6", {"ChartPilot"}),
        ("q=meet", {"MeetNote AI"}),
        ("q=unverified", {"FileBridge Sync", "QuickPoll"}),
        (f"q={app_id('Zoom').upper()}", {"Zoom"}),
    ],
)
def test_overview_filters(a, api, shadow, query, names):
    services.create_resource(
        a["org"],
        a["users"]["editor"],
        {"name": "Workday HR", "kind": "application", "lifecycle": "active", "entra_app_id": app_id("Workday")},
    )
    run(shadow)
    rows = api(a["users"]["viewer"]).get(f"discovery/shadow-it?{query}").json_body["results"]
    assert {r["name"] for r in rows} == names


@pytest.mark.parametrize(
    "query, field",
    [
        ("status=gone", "status"),
        ("consent=maybe", "consent"),
        ("risky=yes", "risky"),
        ("min_users=-1", "min_users"),
        ("connector=nope", "connector"),
        ("owner=1", "owner"),
        ("ordering=publisher", "ordering"),
    ],
)
def test_overview_refuses_bad_filters(a, api, query, field):
    response = api(a["users"]["viewer"]).get(f"discovery/shadow-it?{query}")
    assert response.status_code == 400 and field in response.json_body["errors"]


def test_overview_orders_and_pages(a, api, shadow):
    run(shadow)
    viewer = api(a["users"]["viewer"])
    first = viewer.get("discovery/shadow-it?ordering=display_name&page_size=4").json_body
    second = viewer.get(f"discovery/shadow-it?ordering=display_name&page_size=4&cursor={first['next']}").json_body
    names = [r["name"] for r in first["results"] + second["results"]]
    assert names == sorted(names, key=str.lower) and len(names) == 6


def test_overview_csv_is_audited(a, api, shadow):
    run(shadow)
    response = api(a["users"]["viewer"]).get("discovery/shadow-it/export.csv?risky=true&page_size=2")
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    assert 'filename="softwarden-shadow-it-' in response["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert [r["name"] for r in rows] == ["MeetNote AI", "FileBridge Sync", "QuickPoll"]
    assert rows[0]["risky_scopes"] == "Mail.ReadWrite offline_access" and rows[0]["status"] == "new"
    assert rows[0]["users_30d"] == "6" and rows[0]["connector"] == shadow.name
    event = AuditEvent.objects.get(organization=a["org"], action="export")
    assert event.actor == a["users"]["viewer"]
    assert event.changes == {"kind": "shadow_it", "rows": 3, "filters": {"risky": "true"}}
    bad = api(a["users"]["viewer"]).get("discovery/shadow-it/export.csv?risky=maybe")
    assert bad.status_code == 400 and AuditEvent.objects.filter(action="export").count() == 1


def test_overview_roles_and_methods(a, api, shadow):
    run(shadow)
    assert api().get("discovery/shadow-it").status_code in (401, 403)
    assert api().get("discovery/shadow-it/export.csv").status_code in (401, 403)
    for role in ("viewer", "editor", "admin"):
        assert api(a["users"][role]).get("discovery/shadow-it").status_code == 200
    admin = api(a["users"]["admin"])
    assert admin.post("discovery/shadow-it", {}).status_code == 405
    assert admin.delete("discovery/shadow-it/export.csv").status_code == 405


def test_overview_is_refused_without_the_capability(a, api, shadow):
    from core.models import CapabilityOverride

    CapabilityOverride.objects.create(
        organization=a["org"], capability="discovery.entra", effect="revoke", reason="test"
    )
    response = api(a["users"]["viewer"]).get("discovery/shadow-it")
    assert response.status_code == 403 and "discovery.entra" in json.dumps(response.json_body)


# ---- tenancy -------------------------------------------------------------------------------------------------------


def test_another_organization_sees_nothing(a, b, api, shadow):
    run(shadow)
    other = api(b["users"]["admin"])
    assert other.get("discovery/shadow-it").json_body["results"] == []
    assert other.get(f"discovery/shadow-it?connector={shadow.id}").status_code == 404
    rows = list(csv.reader(io.StringIO(other.get("discovery/shadow-it/export.csv").content.decode("utf-8-sig"))))
    assert len(rows) == 1  # the header only
    # Their own connector on the same simulated tenant has its own rows, state and key.
    theirs = make_shadow(b["org"], b["users"]["admin"])
    run(theirs)
    assert len(other.get("discovery/shadow-it").json_body["results"]) == 6
    assert len(api(a["users"]["viewer"]).get("discovery/shadow-it").json_body["results"]) == 6
    assert ShadowItApp.objects.filter(organization=b["org"], connector=shadow).count() == 0


def test_another_organizations_record_doesnt_link_an_app(a, b, api, shadow):
    services.create_resource(
        b["org"],
        b["users"]["editor"],
        {"name": "Workday HR", "kind": "application", "lifecycle": "active", "entra_app_id": app_id("Workday")},
    )
    run(shadow)
    assert Observation.objects.filter(connector=shadow, display_name="Workday").exists()
    rows = {r["name"]: r for r in api(a["users"]["viewer"]).get("discovery/shadow-it").json_body["results"]}
    assert rows["Workday"]["status"] == "new" and rows["Workday"]["record"] is None


def _rejected(write):
    with pytest.raises(IntegrityError), transaction.atomic():
        write()
        connection.check_constraints()


def test_database_rejects_shadow_rows_on_another_organizations_connector(a, b, shadow):
    org = b["org"]
    _rejected(lambda: ShadowItState.objects.create(organization=org, connector=shadow, sketch_key=b"x"))
    _rejected(
        lambda: ShadowItUsageDay.objects.create(
            organization=org, connector=shadow, app_id="x", day=NOW.date(), users_sketch=b""
        )
    )
    _rejected(
        lambda: ShadowItApp.objects.create(
            organization=org, connector=shadow, app_id="x", display_name="x", first_seen_at=NOW, last_seen_at=NOW
        )
    )


def test_deleting_the_connector_removes_its_shadow_rows_and_keeps_records(a, shadow):
    run(shadow)
    meet = obs(shadow, "MeetNote AI")
    _obs, record = discovery.create_record(a["org"], a["users"]["editor"], meet, {})
    discovery.delete_connector(a["org"], a["users"]["admin"], shadow)
    assert not ShadowItApp.objects.exists() and not ShadowItUsageDay.objects.exists()
    assert not ShadowItState.objects.exists() and not ReconciliationDecision.objects.exists()
    assert Resource.objects.filter(id=record.id).exists()
