"""Licence waste (discovery/entra_licence_usage.py, discovery/licence_waste.py) on the simulated tenant: every
user shape (enabled or disabled, member or guest, no, one or several licences, a sign-in recent, old, never or failed
only), the tenant without Entra ID P1 or the AuditLog consent, paging, throttling and pacing, the empty-listing guard,
retention, privacy (nothing names a user; the drill-down is Admin-only; the CSVs), linking to people, the money
(prices from contracts, rounding, mixed currencies, every named gap), filters, roles, capability, impersonation,
tenancy, the organization setting, the offboarding footprint, export and query counts."""

import csv
import io
import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from catalog import org_lifecycle, services
from catalog.models import Person, Vendor
from core import connections, graph
from core.models import AuditEvent, CapabilityOverride, UserIdentity
from core.request_context import forget_organization
from discovery import access, entra_simulator, licence_waste
from discovery import engine as discovery
from discovery import entra_licence_usage as usage
from discovery.entra_simulator import E3, E5, FLOW, user_id
from discovery.graph_source import AUDIT_LOG_SIGN_IN_ACTIVITY, USER_READ_ALL, Client
from discovery.models import Connector, ConnectorRun, LicensedUser, RunStatus
from money import contracts
from money.models import LicensePool
from offboarding.footprint import LICENCES_NOT_READ

SECRET = "synthetic-secret"
NOW = datetime.now(UTC)
USERS = "/v1.0/users"
SVC = "svc-backup@contoso.example.test"
DEV = entra_simulator.SKUS[3][0]  # DEVELOPERPACK_E5: reported by subscribedSkus, held by nobody
# Queries a report or a drill-down may take (measured 9 and 6), with a margin; they stay flat as users grow.
REPORT_QUERIES = 12
DRILL_QUERIES = 9

# The source is a preview feature until its report has a screen (core/capabilities.PREVIEW_SOURCES): new
# connectors need preview features on. tests/test_preview_features.py covers it off.
pytestmark = pytest.mark.usefixtures("preview_on")


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """A fake monotonic clock that sleeping advances (no test ever waits)."""
    state: dict[str, Any] = {"mono": 1000.0, "slept": []}

    def sleep(seconds):
        state["slept"].append(seconds)
        state["mono"] += seconds

    monkeypatch.setattr(graph, "sleep", sleep)
    monkeypatch.setattr(graph, "monotonic", lambda: state["mono"])
    return state


def make_usage(org, actor, scope=None):
    data: dict[str, Any] = {
        "source": "entra_licence_usage",
        "tenant_id": "simulated",
        "client_id": "",
        "secret": SECRET,
    }
    if scope is not None:
        data["scope"] = scope
    connector = discovery.create_connector(org, actor, data)
    connections.run_test(org, actor, connector.microsoft_connection)
    return Connector.objects.get(pk=connector.pk)


def make_licences(org, actor):
    """An Entra licences connector on the same simulated tenant: the SKUs' part numbers and seat counts."""
    data = {"source": "entra_licenses", "tenant_id": "simulated", "client_id": "", "secret": SECRET}
    connector = discovery.create_connector(org, actor, data)
    connections.run_test(org, actor, connector.microsoft_connection)
    connector = Connector.objects.get(pk=connector.pk)
    assert discovery.run(connector).status == RunStatus.SUCCEEDED
    return connector


def configure(connector, **config):
    connector.config = {**connector.config, **config}
    connector.save(update_fields=["config"])
    return connector


def run(connector, transport=None):
    connector.refresh_from_db()
    return discovery.run(connector, transport=transport)


@pytest.fixture
def lu(a):
    return make_usage(a["org"], a["users"]["admin"])


def counts_of(row) -> dict[str, int]:
    return {k: row[k] for k in licence_waste.COUNT_FIELDS}


def report(api, user, query="", status=200):
    response = api(user).get(f"reports/licence-waste{'?' + query if query else ''}")
    assert response.status_code == status, response.content
    return response.json_body


def by_sku(body) -> dict[str, dict[str, Any]]:
    return {r["sku_id"]: r for r in body["rows"]}


def make_app(a, name):
    return services.create_resource(a["org"], a["users"]["editor"], {"name": name, "lifecycle": "active"})


def make_contract(a, name, amount, currency="USD", **extra):
    data = {"name": name, "amount": amount, "currency": currency, "billing_frequency": "annual", **extra}
    return contracts.create_contract(a["org"], a["users"]["editor"], data)


def make_pool(a, app, sku, seats, contract=None, name=None):
    data: dict[str, Any] = {
        "application": str(app.id),
        "sku_name": name or f"Pool {sku[:8]}",
        "sku_id": sku,
        "seats_purchased": seats,
    }
    if contract is not None:
        data["contract"] = str(contract.id)
    return contracts.create_pool(a["org"], a["users"]["editor"], data)


# The expected counts with sign-in read and N = 90 (entra_simulator.licensed_users_base): per licence shape, eight
# member shapes (four sign-ins, enabled or disabled) and eight guest ones, plus the named users.
E5_COUNTS = {
    "assigned": 12,  # 8 shapes + Dana, Priya, the new starter, the legacy account (userType null)
    "active": 3,  # recent, Dana, legacy
    "new": 1,  # the new starter: never signed in, created 10 days ago
    "inactive": 4,  # old, never, failed only, Priya
    "never_signed_in": 1,  # never (failed only isn't "never")
    "disabled": 4,
    "unknown": 0,
    "reclaimable": 8,
    "guests": 8,
    "guests_inactive": 7,  # enabled guests old, never, failed; every disabled guest
    "service_accounts": 0,
    "group_reclaimable": 0,
}
E3_COUNTS = {  # 8 member shapes + the backup service account, not excluded by the scope
    "assigned": 9,
    "active": 1,
    "new": 0,
    "inactive": 4,
    "never_signed_in": 2,
    "disabled": 4,
    "unknown": 0,
    "reclaimable": 8,
    "guests": 8,
    "guests_inactive": 7,
    "service_accounts": 0,
    "group_reclaimable": 0,
}
LICENSED = 37  # 32 licensed shapes + 5 named
LISTED = 53  # 48 shapes + 5 named


# ---- the run ------------------------------------------------------------------------------------------------------


def test_a_run_stores_every_licensed_user_shape(a, lu):
    result = run(lu)
    assert result.status == RunStatus.SUCCEEDED, result.error
    c = result.counts
    assert (c["seen"], c["licensed_users"], c["users_removed"]) == (LISTED, LICENSED, 0)
    assert (c["guests"], c["disabled"], c["service_accounts"]) == (16, 16, 0)
    assert "sign_in_unknown" not in c and "notes" not in c
    rows = LicensedUser.objects.filter(connector=lu)
    assert rows.count() == LICENSED and all(r.sign_in_known for r in rows)
    one = rows.get(user_key=usage.user_key(lu.id, user_id("member.one.old.on@contoso.example.test")))
    today = datetime.now(UTC).date()
    assert one.sku_ids == [E5] and one.account_enabled and not one.guest
    assert one.last_sign_in_on == today - timedelta(days=200) and one.attempted
    several = rows.get(user_key=usage.user_key(lu.id, user_id("member.several.failed.off@contoso.example.test")))
    assert several.sku_ids == sorted([E3, FLOW]) and not several.account_enabled
    assert several.last_sign_in_on is None and several.attempted  # only failures: not "never"
    never = rows.get(user_key=usage.user_key(lu.id, user_id("member.one.never.on@contoso.example.test")))
    assert never.last_sign_in_on is None and not never.attempted
    guest = rows.get(
        user_key=usage.user_key(lu.id, user_id("guest.one.recent.on_fabrikam.example.test#EXT#@contoso.example.test"))
    )
    assert guest.guest and guest.last_sign_in_on == today - timedelta(days=5)
    legacy = rows.get(user_key=usage.user_key(lu.id, user_id("legacy@contoso.example.test")))
    assert not legacy.guest  # userType null is a member
    # Users without a licence aren't stored at all.
    assert not rows.filter(user_key=usage.user_key(lu.id, user_id("member.none.recent.on@contoso.example.test")))


def test_the_report_counts_every_category(a, api, lu):
    run(lu)
    rows = by_sku(report(api, a["users"]["viewer"]))
    assert set(rows) == {E5, E3, FLOW}
    assert counts_of(rows[E5]) == E5_COUNTS
    assert counts_of(rows[E3]) == E3_COUNTS
    assert counts_of(rows[FLOW]) == {**E3_COUNTS, "group_reclaimable": 8}  # FLOW_FREE comes through a group
    for row in rows.values():  # the categories partition the members
        assert row["assigned"] == row["active"] + row["new"] + row["inactive"] + row["disabled"] + row["unknown"]
        assert row["reclaimable"] == row["inactive"] + row["disabled"]


def test_the_cutoff_follows_days(a, api, lu):
    run(lu)
    rows = by_sku(report(api, a["users"]["viewer"], "days=365"))  # 200 days ago is active now
    assert (rows[E5]["active"], rows[E5]["inactive"], rows[E5]["never_signed_in"]) == (5, 2, 1)
    rows = by_sku(report(api, a["users"]["viewer"], "days=7"))  # the new starter (10 days) is inactive now
    assert (rows[E5]["active"], rows[E5]["new"], rows[E5]["inactive"]) == (3, 0, 5)
    assert (
        report(api, a["users"]["viewer"], "days=7")["cutoff"]
        == (datetime.now(UTC).date() - timedelta(days=7)).isoformat()
    )


def test_service_accounts_named_by_the_scope_are_counted_apart(a, api):
    lu = make_usage(
        a["org"],
        a["users"]["admin"],
        {"service_account_prefixes": [" SVC-"], "excluded_users": [user_id("legacy@contoso.example.test").upper()]},
    )
    assert lu.scope["service_account_prefixes"] == ["svc-"]
    assert lu.scope["excluded_users"] == [user_id("legacy@contoso.example.test")]
    result = run(lu)
    assert result.counts["service_accounts"] == 2
    rows = by_sku(report(api, a["users"]["viewer"]))
    assert rows[E3]["service_accounts"] == 1 and rows[E3]["assigned"] == 8 and rows[E3]["never_signed_in"] == 1
    assert rows[E5]["service_accounts"] == 1 and rows[E5]["active"] == 2 and rows[E5]["assigned"] == 11


@pytest.mark.parametrize(
    "scope, field",
    [
        ({"sign_in_activity": "yes"}, "scope.sign_in_activity"),
        ({"service_account_prefixes": "svc-"}, "scope.service_account_prefixes"),
        ({"service_account_prefixes": [""]}, "scope.service_account_prefixes"),
        ({"excluded_users": ["not-a-guid"]}, "scope.excluded_users"),
    ],
)
def test_the_scope_is_validated(a, scope, field):
    from core.problems import FieldErrors

    with pytest.raises(FieldErrors) as e:
        make_usage(a["org"], a["users"]["admin"], scope)
    assert field in e.value.errors


def test_one_licence_usage_connector_per_tenant(a, lu):
    """Two would count every licensed user twice (each keys its rows with its own hash): refused on create and on
    update, by connection and by tenant ID; other sources may share the connection."""
    from core.problems import FieldErrors

    org, admin = a["org"], a["users"]["admin"]
    source = {"source": "entra_licence_usage", "name": "Second"}
    with pytest.raises(FieldErrors) as e:
        discovery.create_connector(org, admin, {**source, "microsoft_connection": str(lu.microsoft_connection_id)})
    assert "already reads this tenant" in e.value.errors["microsoft_connection"][0] and lu.name in str(e.value.errors)
    with pytest.raises(FieldErrors) as e:  # another connection to the same tenant
        discovery.create_connector(org, admin, {**source, "tenant_id": "simulated", "client_id": "", "secret": SECRET})
    assert "microsoft_connection" in e.value.errors
    other_tenant = "0000000e-0000-4000-8000-00000000000e"
    client = "11111111-1111-4111-8111-111111111111"
    second = discovery.create_connector(
        org, admin, {**source, "tenant_id": other_tenant, "client_id": client, "secret": SECRET}
    )
    with pytest.raises(FieldErrors) as e:  # a third on its tenant, typed in upper case, on a connection of its own
        discovery.create_connector(
            org,
            admin,
            {**source, "name": "Third", "tenant_id": other_tenant.upper(), "client_id": client, "secret": SECRET},
        )
    assert "Second" in e.value.errors["microsoft_connection"][0]
    with pytest.raises(FieldErrors) as e:
        discovery.update_connector(org, admin, second, {"microsoft_connection": str(lu.microsoft_connection_id)})
    assert "microsoft_connection" in e.value.errors
    assert Connector.objects.filter(organization=org, source="entra_licence_usage").count() == 2
    coverage = discovery.create_connector(
        org, admin, {"source": "entra_coverage", "microsoft_connection": str(lu.microsoft_connection_id)}
    )
    assert coverage.microsoft_connection_id == lu.microsoft_connection_id
    discovery.update_connector(org, admin, lu, {"name": "Renamed"})  # the connector itself is never its own duplicate


def test_a_run_refuses_a_tenant_another_connector_reads(a, lu):
    """Two connections can name one tenant differently (its domain and its GUID), or a connection's tenant can be
    changed in Connections: the creation check can't see either. The run compares the tenant its token was issued in
    with the one the other enabled connectors' last runs read."""
    org, admin = a["org"], a["users"]["admin"]
    first = run(lu)
    assert first.counts["tenant_key"] == usage.tenant_key(org.id, "simulated") and len(first.counts["tenant_key"]) == 16
    client = "11111111-1111-4111-8111-111111111111"
    second = discovery.create_connector(
        org,
        admin,
        {
            "source": "entra_licence_usage",
            "name": "Second",
            "tenant_id": "contoso.example.test",
            "client_id": client,
            "secret": SECRET,
        },
    )
    # Its connection is pointed at the same tenant through Connections, which doesn't look at connectors.
    connections.update_connection(org, admin, second.microsoft_connection, {"tenant_id": "simulated"})
    changed = Connector.objects.get(pk=second.pk).microsoft_connection
    assert changed is not None
    connections.run_test(org, admin, changed)
    result = run(second)
    assert result.status == RunStatus.FAILED and f"“{lu.name}” already reads this tenant" in result.error
    assert not LicensedUser.objects.filter(connector=second).exists()
    # Turned off, the first no longer counts: the second may read the tenant (and then the first is refused).
    discovery.update_connector(org, admin, lu, {"enabled": False})
    assert run(second).status == RunStatus.SUCCEEDED
    discovery.update_connector(org, admin, lu, {"enabled": True})
    assert run(lu).status == RunStatus.FAILED
    assert usage.tenant_key(org.id, "simulated") != usage.tenant_key(a["org"].id, "other")


def test_the_token_names_its_tenant():
    import base64

    def jwt(payload):
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"eyJhbGciOiJSUzI1NiJ9.{body}.signature"

    guid = "0000000F-0000-4000-8000-00000000000F"
    assert usage.token_tenant(jwt({"tid": guid, "aud": "https://graph.microsoft.com"})) == guid.lower()
    assert usage.token_tenant(jwt({"tid": "contoso.example.test"})) == ""  # not a GUID
    assert usage.token_tenant(jwt(["not", "an", "object"])) == ""
    assert usage.token_tenant("simulated-token") == "" and usage.token_tenant("a.%%%.c") == ""


def test_licences_held_through_a_group_are_flagged(a, api, lu):
    run(lu)
    flow = LicensedUser.objects.get(
        connector=lu, user_key=usage.user_key(lu.id, user_id("member.several.old.on@contoso.example.test"))
    )
    assert flow.group_sku_ids == [FLOW] and set(flow.sku_ids) == {E3, FLOW}
    admin = a["users"]["admin"]
    assert {u["group_assigned"] for u in drill(api, admin, sku=FLOW)["users"]} == {True}
    assert {u["group_assigned"] for u in drill(api, admin, sku=E3)["users"]} == {False}


# ---- without Entra ID P1 or the AuditLog consent ------------------------------------------------------------------


def _user_reads(transport):
    return [u for _m, u in transport.calls if "/v1.0/users" in u]


def test_without_p1_licences_are_counted_and_sign_in_is_unknown(a, api, lu):
    configure(lu, simulated_no_premium=True)
    transport = entra_simulator.Transport(lu)
    result = run(lu, transport)
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.counts["sign_in_unknown"] == LICENSED and result.counts["licensed_users"] == LICENSED
    note = result.counts["notes"][0]
    assert "Entra ID P1" in note and "Authentication_RequestFromNonPremiumTenantOrB2CTenant" in note
    assert usage.UNKNOWN in note
    reads = _user_reads(transport)
    assert "signInActivity" in reads[0] and all("signInActivity" not in u for u in reads[1:])
    assert not LicensedUser.objects.filter(connector=lu, sign_in_known=True).exists()
    assert not LicensedUser.objects.filter(connector=lu, last_sign_in_on__isnull=False).exists()
    body = report(api, a["users"]["viewer"])
    e5 = by_sku(body)[E5]
    assert (e5["unknown"], e5["disabled"], e5["active"], e5["inactive"], e5["reclaimable"]) == (8, 4, 0, 0, 4)
    assert body["sign_in_activity"] == "unknown" and any(usage.UNKNOWN in n for n in body["notes"])


def test_without_the_auditlog_consent_the_note_names_it(a, lu):
    granted = [p for p in entra_simulator.GRANTED if p != "AuditLog.Read.All"]
    configure(lu, simulated_permissions=granted)
    result = run(lu)
    assert result.status == RunStatus.SUCCEEDED
    assert (
        "AuditLog.Read.All" in result.counts["notes"][0]
        and "403 Authorization_RequestDenied" in result.counts["notes"][0]
    )
    assert result.counts["sign_in_unknown"] == LICENSED


def test_turned_off_in_the_scope_sign_in_is_never_asked_for(a, api):
    lu = make_usage(a["org"], a["users"]["admin"], {"sign_in_activity": False})
    transport = entra_simulator.Transport(lu)
    result = run(lu, transport)
    assert result.status == RunStatus.SUCCEEDED
    assert all("signInActivity" not in u for u in _user_reads(transport))
    assert "turned off" in result.counts["notes"][0]
    assert by_sku(report(api, a["users"]["viewer"]))[E5]["unknown"] == 8


def test_without_user_read_all_the_run_fails_and_names_it(a, lu):
    configure(lu, simulated_permissions=["AuditLog.Read.All", "User.ReadBasic.All"])
    result = run(lu)
    assert result.status == RunStatus.FAILED
    assert "User.Read.All" in result.error and not LicensedUser.objects.exists()


def test_switching_p1_off_later_forgets_the_dates(a, lu):
    run(lu)
    assert LicensedUser.objects.filter(last_sign_in_on__isnull=False).exists()
    configure(lu, simulated_no_premium=True)
    run(lu)
    assert not LicensedUser.objects.filter(last_sign_in_on__isnull=False).exists()


# ---- paging, throttling, pacing ------------------------------------------------------------------------------------


def test_every_page_is_read(a, lu):
    configure(lu, simulated_user_page=10)
    transport = entra_simulator.Transport(lu)
    result = run(lu, transport)
    assert result.counts["seen"] == LISTED
    reads = _user_reads(transport)
    assert len(reads) == math.ceil(LISTED / 10)
    assert "%24top=500" in reads[0] or "$top=500" in reads[0]  # Graph's page limit with signInActivity


def test_a_throttled_read_waits_retry_after(a, lu, clock):
    configure(lu, simulated_throttle={"path": USERS, "times": 2, "retry_after": "4"})
    result = run(lu)
    assert result.status == RunStatus.SUCCEEDED and result.counts["licensed_users"] == LICENSED
    assert clock["slept"] == [4.0, 4.0]


def test_throttling_past_the_budget_fails_the_run_and_changes_nothing(a, lu):
    run(lu)
    configure(lu, simulated_throttle={"path": USERS, "times": 100, "retry_after": "1"})
    result = run(lu)
    assert result.status == RunStatus.FAILED and "throttl" in result.error
    assert LicensedUser.objects.filter(connector=lu).count() == LICENSED


def test_pages_with_sign_in_activity_are_paced_on_a_real_tenant(a, lu, clock):
    configure(lu, simulated_user_page=20)
    client = Client(entra_simulator.Transport(lu), connections.credential(discovery.connection_of(lu)))
    users = usage.read_users(client, usage.ACTIVITY_FIELDS, usage.ACTIVITY_TOP, AUDIT_LOG_SIGN_IN_ACTIVITY, usage.PACE)
    assert len(users) == LISTED
    assert clock["slept"] == pytest.approx([6.5, 6.5])  # three pages; ten requests a minute per tenant


def test_a_tenant_too_large_stops_with_a_readable_error(a, lu, monkeypatch):
    monkeypatch.setattr(usage, "MAX_USERS", 20)
    configure(lu, simulated_user_page=10)
    result = run(lu)
    assert result.status == RunStatus.FAILED
    assert "more than 20 users (30 read in 3 pages)" in result.error and "nothing was changed" in result.error


def test_the_bound_counts_users_not_pages(a, lu, monkeypatch):
    """Graph may answer fewer users a page than $top with signInActivity: the bound is users read, so small pages
    still read the whole tenant."""
    monkeypatch.setattr(usage, "MAX_USERS", LISTED)
    configure(lu, simulated_user_page=3)
    result = run(lu)
    assert result.status == RunStatus.SUCCEEDED and result.counts["seen"] == LISTED


def test_the_sign_in_read_stops_in_time_and_reads_again_without_it(a, api, lu, clock):
    configure(lu, simulated_user_page=10)
    client = Client(entra_simulator.Transport(lu), connections.credential(discovery.connection_of(lu)))
    with pytest.raises(usage.OutOfTime) as e:
        usage.read_users(client, usage.ACTIVITY_FIELDS, 500, AUDIT_LOG_SIGN_IN_ACTIVITY, usage.PACE, read_for=10.0)
    assert (e.value.users, e.value.pages) == (20, 2)  # a third page would start past the 10 seconds
    # In a run: the read without signInActivity follows; licences are counted, the last sign-in is unknown.
    import discovery.entra_licence_usage as module

    original = module.read_users

    def slow(client, select, top, permission, pace, read_for=None):
        if "signInActivity" in select:
            raise usage.OutOfTime(400, 4)
        return original(client, select, top, permission, pace, read_for)

    module.read_users = slow
    try:
        result = run(lu)
    finally:
        module.read_users = original
    assert result.status == RunStatus.SUCCEEDED and result.counts["sign_in_unknown"] == LICENSED
    assert "longer than 25 minutes (400 users in 4 pages" in result.counts["notes"][0]


# ---- the empty-listing guard and retention ----------------------------------------------------------------------


def test_an_empty_listing_is_refused_while_rows_exist(a, lu):
    configure(lu, simulated_no_users=True)
    assert run(lu).status == RunStatus.SUCCEEDED  # nothing stored yet: nothing to protect
    configure(lu, simulated_no_users=False)
    run(lu)
    configure(lu, simulated_no_users=True)
    result = run(lu)
    assert result.status == RunStatus.FAILED and "listed no users" in result.error
    assert LicensedUser.objects.filter(connector=lu).count() == LICENSED


def test_a_listing_without_licences_is_refused(a, lu):
    configure(lu, simulated_users_without_licences=True)
    result = run(lu)
    assert result.status == RunStatus.FAILED and "User.Read.All" in result.error


def test_a_listing_with_no_licensed_user_is_refused_while_rows_exist(a, lu):
    run(lu)
    every = [u["upn"] for u in entra_simulator.licensed_users_base() if u["skus"]]
    configure(lu, simulated_remove_users=every)
    result = run(lu)
    assert result.status == RunStatus.FAILED and "no user with a license" in result.error
    assert LicensedUser.objects.filter(connector=lu).count() == LICENSED


def test_users_no_longer_seen_are_deleted(a, lu):
    run(lu)
    gone = user_id("member.one.old.on@contoso.example.test")
    configure(lu, simulated_remove_users=["member.one.old.on@contoso.example.test"])
    result = run(lu)
    assert result.counts["users_removed"] == 1
    assert LicensedUser.objects.filter(connector=lu).count() == LICENSED - 1
    assert not LicensedUser.objects.filter(user_key=usage.user_key(lu.id, gone)).exists()


def test_deleting_the_connector_deletes_its_users(a, lu):
    run(lu)
    discovery.delete_connector(a["org"], a["users"]["admin"], lu)
    assert not LicensedUser.objects.exists() and not ConnectorRun.objects.filter(connector_id=lu.id).exists()


def test_a_disabled_connectors_users_arent_counted(a, api, lu):
    run(lu)
    Connector.objects.filter(pk=lu.pk).update(enabled=False)
    body = report(api, a["users"]["viewer"])
    assert body["rows"] == [] and body["connectors"] == [] and licence_waste.NO_CONNECTOR in body["notes"]


# ---- privacy -------------------------------------------------------------------------------------------------------


def _identifying(org_users) -> set[str]:
    """Every value that would identify a simulated user: object IDs, names, mails, user principal names."""
    found = set()
    for u in org_users:
        found |= {u["id"], u["displayName"], u["userPrincipalName"].lower()}
        if u["mail"]:
            found.add(u["mail"].lower())
    return found


def test_no_name_email_or_object_id_is_stored(a, lu):
    result = run(lu)
    secret = _identifying(entra_simulator.licensed_users({}))
    with connection.cursor() as cursor:
        cursor.execute("SELECT row_to_json(u)::text FROM discovery_licenseduser u")
        stored = " ".join(r[0] for r in cursor.fetchall()).lower()
    assert stored and not [s for s in secret if s.lower() in stored]
    assert "@" not in stored
    run_text = json.dumps(result.counts).lower()
    assert not [s for s in secret if s.lower() in run_text]
    for event in AuditEvent.objects.filter(organization=a["org"]):
        text = json.dumps([event.changes, event.prechange, event.postchange], default=str).lower()
        assert not [s for s in secret if s.lower() in text]
    # The key is a keyed hash: not the object ID's plain SHA-256, and another connector's differs.
    import hashlib

    oid = user_id("member.one.old.on@contoso.example.test")
    key = usage.user_key(lu.id, oid)
    assert len(key) == 64 and key != hashlib.sha256(oid.encode()).hexdigest()
    assert usage.user_key(a["org"].id, oid) != key


def test_licensed_users_stay_out_of_the_organization_export(a, lu):
    assert "discovery.LicensedUser" in org_lifecycle.EXCLUDED_MODELS
    assert any(m._meta.label == "discovery.LicensedUser" for m in org_lifecycle.tenant_models())  # purged with it


def test_the_purge_removes_them(a, lu):
    run(lu)
    org = a["org"]
    org.deletion_requested_at = datetime.now(UTC) - timedelta(days=40)
    org.deletion_due_at = datetime.now(UTC) - timedelta(days=1)
    org.save()
    from core.models import ExportPurpose, ExportStatus, OrgExport

    OrgExport.objects.create(
        organization=org,
        purpose=ExportPurpose.DELETION,
        status=ExportStatus.READY,
        finished_at=datetime.now(UTC) - timedelta(days=30),
    )
    assert org_lifecycle.purge(org.pk) is not None
    assert not LicensedUser.objects.exists()


# ---- linking to people ------------------------------------------------------------------------------------------------


def test_people_are_linked_by_email_and_by_their_entra_sign_in(a, lu):
    org = a["org"]
    dana = a["people"]["dana"]
    Person.objects.filter(pk=dana.pk).update(email="Dana.Ortiz@Example.test")
    account = get_user_model().objects.create_user(username="old@orga.test", email="old@orga.test", password="x")
    old = Person.objects.create(organization=org, name="Morgan Old", user=account)
    UserIdentity.objects.create(
        user=account,
        issuer="https://login.microsoftonline.com/simulated/v2.0",
        subject=user_id("member.one.old.on@contoso.example.test").upper(),
    )
    # An anonymized person is never linked, even with a matching email.
    Person.objects.create(
        organization=org, name="Gone", email="member.one.never.on@contoso.example.test", anonymized_at=NOW
    )
    result = run(lu)
    assert result.counts["linked"] == 2
    linked = {r.person_id: r for r in LicensedUser.objects.filter(connector=lu, person__isnull=False)}
    assert set(linked) == {dana.id, old.id}
    assert linked[dana.id].user_key == usage.user_key(lu.id, user_id(entra_simulator.DANA["mail"]))


TENANT = "0000000c-0000-4000-8000-00000000000c"


def _user(key, oid, upn="", mail="", guest=False):
    """A user as map_user hands it to link_people."""
    return {"user_key": key, "object_id": oid, "upn": "" if guest else upn, "mail": "" if guest else mail}


def _account(email, person_org, name, tenant=TENANT, oid=None):
    account = get_user_model().objects.create_user(username=email, email=email, password="x")
    person = Person.objects.create(organization=person_org, name=name, user=account)
    if oid:
        UserIdentity.objects.create(
            user=account, issuer=f"https://login.microsoftonline.com/{tenant}/v2.0", subject=oid
        )
    return person


def test_identity_linking_uses_the_tenant_guid_and_any_entra_issuer_for_a_domain(a):
    oid = "1111aaaa-0000-4000-8000-000000000001"
    person = _account("morgan@orga.test", a["org"], "Morgan", oid=oid)
    users = [_user("k1", oid)]
    assert usage.link_people(a["org"].id, TENANT.upper(), users) == {"k1": person.id}  # upper-case GUID
    assert usage.link_people(a["org"].id, "Contoso.onmicrosoft.com", users) == {"k1": person.id}  # a domain name
    assert usage.link_people(a["org"].id, "0000000d-0000-4000-8000-00000000000d", users) == {}  # another tenant


def test_guests_are_linked_by_identity_only(a, lu):
    Person.objects.create(
        organization=a["org"], name="Partner", email="guest.one.old.on@fabrikam.example.test"
    )  # the guest's mail
    run(lu)
    assert not LicensedUser.objects.filter(person__isnull=False).exists()
    oid = "1111aaaa-0000-4000-8000-000000000002"
    person = _account("partner@fabrikam.test", a["org"], "Partner account", oid=oid)
    guest = _user("g", oid, mail="partner@fabrikam.test", guest=True)
    assert usage.link_people(a["org"].id, TENANT, [guest]) == {"g": person.id}


def test_email_linking_prefers_the_upn_and_never_guesses(a):
    org = a["org"]
    jane = Person.objects.create(organization=org, name="Jane", email="jane@contoso.test")
    sam = Person.objects.create(organization=org, name="Sam", email="sam@contoso.test")
    users = [
        _user("jane", "2222aaaa-0000-4000-8000-000000000001", upn="jane@contoso.test", mail="jane@contoso.test"),
        # Her admin account: its mail is hers, but her own account's UPN claims her first.
        _user("adm", "2222aaaa-0000-4000-8000-000000000002", upn="adm-jane@contoso.test", mail="jane@contoso.test"),
        # UPN and mail name two different people: unlinked.
        _user("both", "2222aaaa-0000-4000-8000-000000000003", upn="sam@contoso.test", mail="jane@contoso.test"),
    ]
    assert usage.link_people(org.id, TENANT, users) == {"jane": jane.id}
    # Two accounts whose mail alone names one person: neither is linked.
    shared = [
        _user("s1", "2222aaaa-0000-4000-8000-000000000004", upn="svc1@contoso.test", mail="sam@contoso.test"),
        _user("s2", "2222aaaa-0000-4000-8000-000000000005", upn="svc2@contoso.test", mail="sam@contoso.test"),
    ]
    assert usage.link_people(org.id, TENANT, shared) == {}
    # One of them alone links by mail; an identity elsewhere takes precedence over it.
    assert usage.link_people(org.id, TENANT, shared[:1]) == {"s1": sam.id}
    oid = "2222aaaa-0000-4000-8000-000000000006"
    account = get_user_model().objects.create_user(username="sam-login", email="sam@contoso.test", password="x")
    Person.objects.filter(pk=sam.pk).update(user=account)
    UserIdentity.objects.create(user=account, issuer=f"https://login.microsoftonline.com/{TENANT}/v2.0", subject=oid)
    assert usage.link_people(org.id, TENANT, [*shared[:1], _user("sam", oid)]) == {"sam": sam.id}


def test_anonymizing_a_person_unlinks_their_licences(a, lu):
    from catalog import admin_services

    dana = a["people"]["dana"]
    Person.objects.filter(pk=dana.pk).update(email=entra_simulator.DANA["mail"])
    run(lu)
    assert LicensedUser.objects.filter(person=dana).count() == 1
    admin_services.anonymize_person(a["org"], a["users"]["admin"], dana, "Dana Ortiz")
    assert not LicensedUser.objects.filter(person=dana).exists()
    event = AuditEvent.objects.get(organization=a["org"], action="person.anonymized")
    assert event.changes["purged"]["licensed_users"] == 1
    run(lu)  # and the next run doesn't link them again
    assert not LicensedUser.objects.filter(person=dana).exists()


def test_people_of_another_organization_are_never_linked(a, b, lu):
    Person.objects.filter(pk=b["people"]["dana"].pk).update(email=entra_simulator.DANA["mail"])
    assert run(lu).counts["linked"] == 0


def test_deleting_a_person_unlinks_the_row(a, lu):
    dana = a["people"]["dana"]
    Person.objects.filter(pk=dana.pk).update(email=entra_simulator.DANA["mail"])
    run(lu)
    row = LicensedUser.objects.get(person=dana)
    Person.objects.filter(pk=dana.pk).delete()
    row.refresh_from_db()
    assert row.person_id is None


# ---- the money --------------------------------------------------------------------------------------------------------


@pytest.fixture
def priced(a, lu):
    """E5 on a USD contract (100 seats), E3 on a EUR contract (100 seats), FLOW_FREE unbound."""
    run(lu)
    m365 = make_app(a, "Microsoft 365")
    e5 = make_contract(a, "M365 E5", "57000.00")
    e3 = make_contract(a, "M365 E3", "36000.00", "EUR")
    return {
        "app": m365,
        "e5": e5,
        "e3": e3,
        "e5_pool": make_pool(a, m365, E5.upper(), 100, e5, "Microsoft 365 E5"),
        "e3_pool": make_pool(a, m365, E3, 100, e3, "Microsoft 365 E3"),
    }


def test_the_money_is_the_contract_price_times_the_reclaimable_seats(a, api, priced):
    body = report(api, a["users"]["viewer"])
    rows = by_sku(body)
    e5 = rows[E5]
    assert e5["price"]["currency"] == "USD" and e5["price"]["annual_unit_price"] == "570.00"
    assert e5["price"]["seats"] == 100 and e5["price"]["contracts"] == [{"id": str(priced["e5"].id), "name": "M365 E5"}]
    assert e5["annual_waste"] == "4560.00" and e5["gap"] is None  # 8 reclaimable × 570
    assert e5["applications"] == [{"id": str(priced["app"].id), "name": "Microsoft 365"}]
    assert rows[E3]["price"]["currency"] == "EUR" and rows[E3]["annual_waste"] == "2880.00"
    flow = rows[FLOW]
    assert flow["price"] is None and flow["annual_waste"] is None
    assert flow["gap"] == {"code": "no_pool", "label": licence_waste.GAPS["no_pool"]}
    summary = body["summary"]
    assert summary["totals"] == [
        {"currency": "EUR", "skus": 1, "reclaimable": 8, "annual_waste": "2880.00", "annual_unassigned": "0.00"},
        {"currency": "USD", "skus": 1, "reclaimable": 8, "annual_waste": "4560.00", "annual_unassigned": "0.00"},
    ]
    assert summary["unpriced"] == {"skus": 1, "reclaimable": 8}  # shown as a gap, never priced at 0
    assert summary["reclaimable"] == 24 and summary["skus"] == 3


def test_money_is_rounded_once_to_the_cent(a, api, priced):
    contracts.update_contract(a["org"], a["users"]["editor"], priced["e5"], {"amount": "1000.00"})
    contracts.update_pool(a["org"], a["users"]["editor"], priced["e5_pool"], {"seats_purchased": 3})
    e5 = by_sku(report(api, a["users"]["viewer"]))[E5]
    assert e5["price"]["annual_unit_price"] == "333.33"
    assert e5["annual_waste"] == "2666.67"  # 1000 × 8 ÷ 3, not 333.33 × 8 = 2666.64


def test_several_contracts_of_one_currency_share_one_price(a, api, priced):
    extra = make_contract(a, "M365 E5 top-up", "20000.00")
    make_pool(a, priced["app"], E5, 50, extra, "E5 top-up")
    e5 = by_sku(report(api, a["users"]["viewer"]))[E5]
    assert e5["price"]["seats"] == 150 and e5["price"]["annual_cost"] == "77000.00"
    assert e5["price"]["annual_unit_price"] == "513.33" and e5["annual_waste"] == "4106.67"  # 77000 × 8 ÷ 150
    assert [c["name"] for c in e5["price"]["contracts"]] == ["M365 E5", "M365 E5 top-up"]


def _gap(api, a, sku=E5):
    row = by_sku(report(api, a["users"]["viewer"]))[sku]
    assert row["price"] is None and row["annual_waste"] is None and row["annual_unassigned"] is None
    return row["gap"]["code"]


def test_mixed_currencies_for_one_sku_are_a_gap(a, api, priced):
    euro = make_contract(a, "M365 E5 Europe", "20000.00", "EUR")
    make_pool(a, priced["app"], E5, 50, euro)
    assert _gap(api, a) == "mixed_currencies"


def test_a_pool_without_a_contract_is_a_gap(a, api, priced):
    contracts.update_pool(a["org"], a["users"]["editor"], priced["e5_pool"], {"contract": None})
    assert _gap(api, a) == "no_contract"


def test_a_contract_without_an_amount_is_a_gap(a, api, priced):
    contracts.update_contract(a["org"], a["users"]["editor"], priced["e5"], {"amount": None})
    assert _gap(api, a) == "no_cost"


def test_a_contract_that_isnt_live_is_a_gap(a, api, priced):
    contracts.archive_contract(a["org"], a["users"]["editor"], priced["e5"])
    assert _gap(api, a) == "contract_not_live"


def test_a_contract_covering_other_licences_is_a_gap(a, api, priced):
    contracts.update_pool(a["org"], a["users"]["editor"], priced["e3_pool"], {"contract": str(priced["e5"].id)})
    assert _gap(api, a) == "contract_shared" and _gap(api, a, E3) == "contract_shared"


def test_no_purchased_seats_is_a_gap(a, api, priced):
    contracts.update_pool(a["org"], a["users"]["editor"], priced["e5_pool"], {"seats_purchased": 0})
    assert _gap(api, a) == "no_seats"


def test_one_unpriced_pool_beside_a_priced_one_says_so(a, api, priced):
    make_pool(a, priced["app"], E5, 10, None, "E5 trial")
    e5 = by_sku(report(api, a["users"]["viewer"]))[E5]
    assert e5["price"]["some_pools_unpriced"] is True and e5["annual_waste"] == "4560.00"


def test_without_the_contracts_capability_the_money_is_a_gap(a, api, priced):
    CapabilityOverride.objects.create(organization=a["org"], capability="contracts", effect="revoke", reason="t")
    forget_organization(a["org"].pk)
    assert _gap(api, a) == "contracts_off"


def test_seat_counts_and_unassigned_money_come_from_the_licences_source(a, api, priced):
    make_licences(a["org"], a["users"]["admin"])
    rows = by_sku(report(api, a["users"]["viewer"]))
    e5 = rows[E5]
    assert (e5["sku_part_number"], e5["seats_purchased"], e5["seats_assigned"], e5["seats_unassigned"]) == (
        "SPE_E5",
        250,
        231,
        19,
    )
    assert e5["annual_unassigned"] == "10830.00"  # 19 × 570
    dev = rows[DEV]  # reported by subscribedSkus, held by nobody here
    assert dev["assigned"] == 0 and dev["seats_purchased"] == 0 and dev["gap"]["code"] == "no_pool"


def test_curated_pools_and_contracts_are_never_written(a, api, priced):
    before = list(LicensePool.objects.values())
    report(api, a["users"]["viewer"])
    run(Connector.objects.get(source="entra_licence_usage"))
    assert list(LicensePool.objects.values()) == before


# ---- filters ---------------------------------------------------------------------------------------------------------


def test_filters(a, api, priced):
    viewer = a["users"]["viewer"]
    assert set(by_sku(report(api, viewer, f"sku={E5.upper()},Microsoft%20365%20E3"))) == {E5, E3}
    assert set(by_sku(report(api, viewer, f"sku={FLOW}"))) == {FLOW}
    assert set(by_sku(report(api, viewer, f"application={priced['app'].id}"))) == {E5, E3}
    vendor = Vendor.objects.create(organization=a["org"], name="Microsoft")
    contracts.update_contract(a["org"], a["users"]["editor"], priced["e3"], {"vendor": str(vendor.id)})
    assert set(by_sku(report(api, viewer, f"vendor={vendor.id}"))) == {E3}
    assert report(api, viewer, "min_inactive=5")["rows"] == []
    body = report(api, viewer, "min_inactive=4")
    assert len(body["rows"]) == 3 and body["summary"]["skus"] == 3


@pytest.mark.parametrize(
    "query, field",
    [
        ("days=0", "days"),
        ("days=6", "days"),  # the organization setting's range, 7 to 730
        ("days=731", "days"),
        ("days=x", "days"),
        ("min_inactive=-1", "min_inactive"),
        ("application=nope", "application"),
        ("colour=red", "colour"),
    ],
)
def test_bad_filters_are_field_errors(a, api, lu, query, field):
    body = report(api, a["users"]["viewer"], query, status=400)
    assert field in json.dumps(body)


def test_another_organizations_record_in_a_filter_is_404(a, b, api, lu):
    other = make_app(b, "Theirs")
    assert api(a["users"]["viewer"]).get(f"reports/licence-waste?application={other.id}").status_code == 404
    vendor = Vendor.objects.create(organization=b["org"], name="Theirs")
    assert api(a["users"]["viewer"]).get(f"reports/licence-waste?vendor={vendor.id}").status_code == 404


# ---- the drill-down -------------------------------------------------------------------------------------------------


def drill(api, user, sku=E5, query="", status=200):
    response = api(user).get(f"reports/licence-waste/{sku}{'?' + query if query else ''}")
    assert response.status_code == status, response.content
    return response.json_body


def test_the_drill_down_lists_the_reclaimable_members(a, api, lu):
    Person.objects.filter(pk=a["people"]["priya"].pk).update(email=entra_simulator.PRIYA["mail"])
    run(lu)
    body = drill(api, a["users"]["admin"])
    assert body["statuses"] == ["inactive", "disabled"] and body["user_type"] == "member"
    assert body["total"] == 8 and not body["truncated"]
    users = body["users"]
    assert users[0]["never_signed_in"] and users[0]["last_sign_in"] == "never signed in"
    assert {u["status"] for u in users} == {"inactive", "disabled"}
    priya = next(u for u in users if u["person"])
    assert priya["name"] == "Priya Raman" and priya["days_since"] == 200 and priya["status"] == "inactive"
    unlinked = [u for u in users if not u["person"]]
    assert {u["name"] for u in unlinked} == {"Unlinked user"} and all(len(u["ref"]) == 8 for u in unlinked)
    assert len({u["ref"] for u in users}) == 8
    assert "no successful sign-in recorded" in {u["last_sign_in"] for u in users}
    text = json.dumps(body).lower()
    assert "@" not in text and user_id("member.one.old.on@contoso.example.test") not in text


def test_the_drill_down_filters(a, api, lu):
    run(lu)
    admin = a["users"]["admin"]
    assert drill(api, admin, query="status=active")["total"] == 3
    assert drill(api, admin, query="status=new")["total"] == 1
    assert drill(api, admin, query="user_type=guest&status=inactive,disabled")["total"] == 7
    assert drill(api, admin, query="user_type=all&status=active")["total"] == 4
    assert drill(api, admin, query="days=365&status=inactive")["total"] == 2
    assert drill(api, admin, sku=FLOW, query="status=inactive")["total"] == 4
    assert drill(api, admin, query="status=maybe", status=400)
    # A SKU no source, pool or user knows is a 404 (it was a 200 with an empty report), and its CSV writes no
    # export event.
    unknown = "00000000-0000-4000-8000-000000000001"
    assert drill(api, admin, sku=unknown, status=404)["type"] == "/problems/not-found"
    exports = AuditEvent.objects.filter(action="export").count()
    assert api(admin).get(f"reports/licence-waste/{unknown}/export.csv").status_code == 404
    assert AuditEvent.objects.filter(action="export").count() == exports
    assert drill(api, admin, query="status=active")["total"] == 3  # a known SKU still answers


def test_unknown_sign_ins_show_as_unknown_in_the_drill_down(a, api, lu):
    configure(lu, simulated_no_premium=True)
    run(lu)
    users = drill(api, a["users"]["admin"], query="status=unknown")["users"]
    assert len(users) == 8 and {u["last_sign_in"] for u in users} == {usage.UNKNOWN}


def test_the_drill_down_is_admin_only(a, api, lu):
    run(lu)
    for role in ("viewer", "editor"):
        assert api(a["users"][role]).get(f"reports/licence-waste/{E5}").status_code == 403
        assert api(a["users"][role]).get(f"reports/licence-waste/{E5}/export.csv").status_code == 403
    assert api().get(f"reports/licence-waste/{E5}").status_code in (401, 403)
    assert api(a["users"]["admin"]).post(f"reports/licence-waste/{E5}", {}).status_code == 405


def test_the_drill_down_csv_names_no_one_but_linked_people_and_is_audited(a, api, lu):
    Person.objects.filter(pk=a["people"]["priya"].pk).update(email=entra_simulator.PRIYA["mail"])
    run(lu)
    response = api(a["users"]["admin"]).get(f"reports/licence-waste/{E5}/export.csv?status=inactive")
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    assert f'filename="softwarden-licence-waste-{E5}-' in response["Content-Disposition"]
    text = response.content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0]) == [
        "user",
        "ref",
        "status",
        "last_sign_in",
        "days_since",
        "account_enabled",
        "guest",
        "group_assigned",
        "connector",
    ]
    assert len(rows) == 4 and {r["user"] for r in rows} == {"Priya Raman", "Unlinked user"}
    assert "@" not in text and not [s for s in _identifying(entra_simulator.licensed_users({})) if s in text.lower()]
    event = AuditEvent.objects.get(organization=a["org"], action="export")
    assert event.actor == a["users"]["admin"]
    assert event.changes == {"kind": "licence_waste_users", "sku": E5, "rows": 4, "filters": {"status": "inactive"}}


# ---- the report CSV, roles, capability, impersonation ---------------------------------------------------------------


def test_the_report_csv_is_audited(a, api, priced):
    response = api(a["users"]["viewer"]).get("reports/licence-waste/export.csv?min_inactive=1")
    assert response.status_code == 200 and 'filename="softwarden-licence-waste-' in response["Content-Disposition"]
    rows = {r["sku_id"]: r for r in csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))}
    assert rows[E5]["annual_waste"] == "4560.00" and rows[E5]["currency"] == "USD" and rows[E5]["gap"] == ""
    assert rows[FLOW]["annual_waste"] == "" and rows[FLOW]["gap"] == licence_waste.GAPS["no_pool"]
    assert rows[E5]["applications"] == "Microsoft 365" and rows[E5]["reclaimable"] == "8"
    event = AuditEvent.objects.get(organization=a["org"], action="export")
    assert event.changes == {"kind": "licence_waste", "rows": 3, "filters": {"min_inactive": "1"}}
    assert api(a["users"]["viewer"]).get("reports/licence-waste/export.csv?days=0").status_code == 400
    assert AuditEvent.objects.filter(action="export").count() == 1


def test_report_roles_and_methods(a, api, lu):
    assert api().get("reports/licence-waste").status_code in (401, 403)
    for role in ("viewer", "editor", "admin"):
        assert api(a["users"][role]).get("reports/licence-waste").status_code == 200
    assert api(a["users"]["admin"]).post("reports/licence-waste", {}).status_code == 405


def test_everything_needs_the_capability(a, api, priced):
    CapabilityOverride.objects.create(organization=a["org"], capability="discovery.entra", effect="revoke", reason="t")
    forget_organization(a["org"].pk)
    for path in (
        "reports/licence-waste",
        "reports/licence-waste/export.csv",
        f"reports/licence-waste/{E5}",
        f"license-pools/{priced['e5_pool'].id}/licence-waste",
        f"contracts/{priced['e5'].id}/licence-waste",
    ):
        response = api(a["users"]["admin"]).get(path)
        assert response.status_code == 403 and "discovery.entra" in json.dumps(response.json_body), path
    with pytest.raises(Exception, match="discovery.entra"):
        make_usage(a["org"], a["users"]["admin"])


def test_the_drill_down_and_csvs_are_restricted_while_impersonating():
    from core.impersonation_routes import RESTRICTED

    routes = {route for entries in RESTRICTED.values() for _method, route in entries}
    assert {
        "api/v1/reports/licence-waste/export.csv",
        "api/v1/reports/licence-waste/<uuid:sku_id>",
        "api/v1/reports/licence-waste/<uuid:sku_id>/export.csv",
    } <= routes
    assert "api/v1/reports/licence-waste" not in routes  # counts and money only


# ---- on a pool's and a contract's page ------------------------------------------------------------------------------


def test_waste_on_the_pool_and_the_contract(a, api, priced):
    viewer = api(a["users"]["viewer"])
    body = viewer.get(f"license-pools/{priced['e5_pool'].id}/licence-waste").json_body
    assert body["sku_id"] == E5 and body["gap"] is None and body["days"] == 90
    assert body["row"]["annual_waste"] == "4560.00" and body["row"]["reclaimable"] == 8
    body = viewer.get(f"contracts/{priced['e3'].id}/licence-waste?days=365").json_body
    assert body["contract"]["currency"] == "EUR" and [r["sku_id"] for r in body["rows"]] == [E3]
    assert body["summary"]["totals"][0]["annual_waste"] == "2520.00"  # 7 reclaimable at 365 days × 360
    unbound = make_pool(a, priced["app"], "", 5, None, "Unbound")
    body = viewer.get(f"license-pools/{unbound.id}/licence-waste").json_body
    assert body["row"] is None and body["gap"]["code"] == "no_sku_id"
    empty = make_contract(a, "Nothing bound", "10.00")
    assert viewer.get(f"contracts/{empty.id}/licence-waste").json_body["rows"] == []


def test_the_contract_block_shows_its_share_of_a_shared_sku(a, api, priced):
    """Two contracts price E5 together (100 seats for 57000, 50 for 20000): the SKU's waste is 77000 × 8 ÷ 150 =
    4106.67. Each contract's part is weighted by its own cost, so the cheap top-up carries less than its seats' third:
    20000 × 8 ÷ 150 = 1066.67, and the main contract 57000 × 8 ÷ 150 = 3040.00; together the row."""
    viewer = api(a["users"]["viewer"])
    top_up = make_contract(a, "M365 E5 top-up", "20000.00")
    make_pool(a, priced["app"], E5, 50, top_up, "E5 top-up")
    body = viewer.get(f"contracts/{top_up.id}/licence-waste").json_body
    (row,) = body["rows"]
    assert row["annual_waste"] == "4106.67"  # the SKU's whole row
    assert row["contract_share"] == {
        "seats": 50,
        "of_seats": 150,
        "annual_cost": "20000.00",
        "annual_waste": "1066.67",
        "annual_unassigned": None,  # no Entra licences connector: no seat counts
    }
    assert body["contract_totals"] == {
        "currency": "USD",
        "skus": 1,
        "annual_waste": "1066.67",
        "annual_unassigned": "0.00",
    }
    main = viewer.get(f"contracts/{priced['e5'].id}/licence-waste").json_body
    assert main["rows"][0]["contract_share"]["annual_waste"] == "3040.00"
    assert Decimal("1066.67") + Decimal("3040.00") == Decimal(row["annual_waste"])  # the parts make the row
    # With seat counts, the unassigned money is weighted the same way: 19 unassigned × 20000 ÷ 150 = 2533.33.
    make_licences(a["org"], a["users"]["admin"])
    share = viewer.get(f"contracts/{top_up.id}/licence-waste").json_body["rows"][0]["contract_share"]
    assert share["annual_unassigned"] == "2533.33"
    # A contract that doesn't price its SKU (no amount) has no share.
    contracts.update_contract(a["org"], a["users"]["editor"], top_up, {"amount": None})
    body = viewer.get(f"contracts/{top_up.id}/licence-waste").json_body
    assert body["rows"][0]["contract_share"] is None and body["contract_totals"]["skus"] == 0


def test_another_organizations_pool_or_contract_is_404(a, b, api, priced):
    other = api(b["users"]["admin"])
    assert other.get(f"license-pools/{priced['e5_pool'].id}/licence-waste").status_code == 404
    assert other.get(f"contracts/{priced['e5'].id}/licence-waste").status_code == 404


# ---- tenancy -----------------------------------------------------------------------------------------------------------


def test_another_organization_sees_nothing(a, b, api, priced):
    body = report(api, b["users"]["admin"])
    assert body["rows"] == [] and body["connectors"] == []
    drill(api, b["users"]["admin"], status=404)  # another organization's SKU is unknown here (404, not empty)
    lines = api(b["users"]["admin"]).get("reports/licence-waste/export.csv").content.decode("utf-8-sig").splitlines()
    assert len(lines) == 1  # the header only


def test_another_organizations_connector_on_the_same_tenant_has_its_own_rows(a, b, api, lu):
    run(lu)
    theirs = make_usage(b["org"], b["users"]["admin"])
    run(theirs)
    assert LicensedUser.objects.filter(organization=b["org"]).count() == LICENSED
    mine = set(LicensedUser.objects.filter(organization=a["org"]).values_list("user_key", flat=True))
    assert mine.isdisjoint(LicensedUser.objects.filter(organization=b["org"]).values_list("user_key", flat=True))
    assert counts_of(by_sku(report(api, b["users"]["viewer"]))[E5]) == E5_COUNTS


def _rejected(write):
    with pytest.raises(IntegrityError), transaction.atomic():
        write()
        connection.check_constraints()


def test_database_rejects_rows_crossing_organizations(a, b, lu):
    _rejected(lambda: LicensedUser.objects.create(organization=b["org"], connector=lu, user_key="k", seen_at=NOW))
    mine = make_usage(b["org"], b["users"]["admin"])
    _rejected(
        lambda: LicensedUser.objects.create(
            organization=b["org"], connector=mine, user_key="k", seen_at=NOW, person=a["people"]["dana"]
        )
    )


# ---- the organization's setting --------------------------------------------------------------------------------------


def test_the_inactive_days_are_an_audited_organization_setting(a, api, lu):
    run(lu)
    admin = api(a["users"]["admin"])
    assert admin.get("organization").json_body["inactive_seat_days"] == 90
    assert admin.patch("organization", {"inactive_seat_days": 365}).status_code == 200
    assert admin.get("organization").json_body["inactive_seat_days"] == 365
    body = report(api, a["users"]["viewer"])
    assert body["days"] == 365 and by_sku(body)[E5]["inactive"] == 2
    event = AuditEvent.objects.filter(organization=a["org"], action="organization.updated").latest("created_at")
    assert event.changes["inactive_seat_days"] == [90, 365]
    for bad in (3, 731, "90", True):
        assert admin.patch("organization", {"inactive_seat_days": bad}).status_code == 400
    assert api(a["users"]["editor"]).patch("organization", {"inactive_seat_days": 30}).status_code == 403
    assert drill(api, a["users"]["admin"])["days"] == 365


# ---- the offboarding footprint ---------------------------------------------------------------------------------------


def test_the_footprint_lists_a_persons_licences_once_they_are_read(a, api, lu):
    dana = a["people"]["dana"]
    Person.objects.filter(pk=dana.pk).update(email=entra_simulator.DANA["mail"])

    def section():
        body = api(a["users"]["viewer"]).get(f"people/{dana.id}/footprint").json_body
        return next(s for s in body["sections"] if s["key"] == "licences")

    assert section()["available"] is False and section()["reason"] == LICENCES_NOT_READ
    run(lu)
    make_licences(a["org"], a["users"]["admin"])
    found = section()
    assert found["available"] and found["count"] == 1
    item = found["items"][0]
    assert (item["type"], item["id"], item["name"], item["roles"]) == ("licence", E5, "SPE_E5", ["assigned"])
    assert "last" not in item["detail"].lower()  # no sign-in activity outside the Admin drill-down


# ---- the access check ----------------------------------------------------------------------------------------------


def steps(result):
    return {s["name"]: s for s in result["steps"]}


def test_access_check_passes_and_names_each_permission(a, lu):
    result = access.check_access(a["org"], a["users"]["admin"], lu).access_check
    assert result["ok"] and result["missing"] == []
    by = steps(result)
    assert by["Read users and their licenses"]["permission"] == "User.Read.All"
    assert by["Read last sign-in"]["permission"] == "AuditLog.Read.All"


def test_access_check_warns_of_the_missing_licence_without_failing(a, lu):
    configure(lu, simulated_no_premium=True)
    result = access.check_access(a["org"], a["users"]["admin"], lu).access_check
    step = steps(result)["Read last sign-in"]
    assert result["ok"] and (step["state"], step["problem"]) == ("warning", "missing_licence")
    assert "P1" in step["detail"]


def test_access_check_without_the_consents(a, lu):
    configure(lu, simulated_permissions=["User.Read.All"])
    result = access.check_access(a["org"], a["users"]["admin"], lu).access_check
    assert result["ok"] and result["optional_missing"] == ["AuditLog.Read.All"]
    configure(lu, simulated_permissions=["User.ReadBasic.All", "AuditLog.Read.All"])
    result = access.check_access(a["org"], a["users"]["admin"], lu).access_check
    assert not result["ok"] and result["missing"] == ["User.Read.All"]
    assert steps(result)["Read last sign-in"]["state"] == "skipped"


def test_the_source_lists_its_permissions(a, api):
    sources = {s["id"]: s for s in api(a["users"]["viewer"]).get("discovery/sources").json_body}
    source = sources["entra_licence_usage"]
    assert source["capability"] == "discovery.entra" and source["available"]
    assert [(p["name"], p["required"], p["role_id"]) for p in source["permissions"]] == [
        ("User.Read.All", True, "df021288-bdef-4463-88db-98f22de89214"),
        ("AuditLog.Read.All", False, "b0afded3-3588-46d8-8b3d-9842eff778da"),
    ]
    assert source["scope_defaults"] == {"sign_in_activity": True, "service_account_prefixes": [], "excluded_users": []}
    assert USER_READ_ALL.verified and AUDIT_LOG_SIGN_IN_ACTIVITY.verified


# ---- query counts -----------------------------------------------------------------------------------------------------


def test_report_and_drill_down_queries_stay_flat(a, api, priced, django_assert_max_num_queries):
    viewer, admin = api(a["users"]["viewer"]), api(a["users"]["admin"])
    viewer.get("reports/licence-waste")  # the session's first request saves it once
    admin.get(f"reports/licence-waste/{E5}")
    with CaptureQueriesContext(connection) as before:
        assert viewer.get("reports/licence-waste").status_code == 200
    with CaptureQueriesContext(connection) as drill_before:
        assert admin.get(f"reports/licence-waste/{E5}").status_code == 200
    lu = Connector.objects.get(source="entra_licence_usage")
    configure(lu, simulated_licensed_users=120)
    run(lu)
    make_pool(a, priced["app"], FLOW, 10, make_contract(a, "Flow", "100.00"))
    with CaptureQueriesContext(connection) as after:
        assert viewer.get("reports/licence-waste").status_code == 200
    with CaptureQueriesContext(connection) as drill_after:
        assert admin.get(f"reports/licence-waste/{E5}?user_type=all").json_body["total"] == 8 + 60 + 7
    assert len(after) == len(before) and len(drill_after) == len(drill_before)
    with django_assert_max_num_queries(REPORT_QUERIES):
        viewer.get("reports/licence-waste")
    with django_assert_max_num_queries(DRILL_QUERIES):
        admin.get(f"reports/licence-waste/{E5}")


def test_the_permissions_carry_their_role_ids():
    assert USER_READ_ALL.role_id == "df021288-bdef-4463-88db-98f22de89214"
    assert AUDIT_LOG_SIGN_IN_ACTIVITY.role_id == "b0afded3-3588-46d8-8b3d-9842eff778da"
    assert not AUDIT_LOG_SIGN_IN_ACTIVITY.required and USER_READ_ALL.required


def test_decimal_maths_never_floats(a, priced):
    found, _gap = licence_waste.price(E5, [], {}, True)
    assert found is None
    assert licence_waste.money({"annual_cost": Decimal("1000"), "seats": 3}, 8) == Decimal("2666.67")
    assert licence_waste.money(None, 8) is None
    row = next(r for r in licence_waste.report(a["org"], licence_waste.Filters(days=90))["rows"] if r["sku_id"] == E5)
    for value in (row["annual_waste"], row["price"]["annual_cost"], row["price"]["annual_unit_price"]):
        assert type(value) is Decimal
    totals = licence_waste.summary([row])["totals"][0]
    assert type(totals["annual_waste"]) is Decimal and type(totals["annual_unassigned"]) is Decimal
