"""SSO and MFA coverage (discovery/entra_coverage.py, discovery/coverage.py) on the simulated tenant: every
single sign-on mode (null is unknown), every provisioning state, every conditional access shape (all users, some
users, report-only, OR, authentication strength, exclusions, application filters, Microsoft app groups), the tenant
without P1, conditional access throttling without Retry-After, the provisioning cap, the empty-listing guard, curated
fields never written, the data-quality rules, the report with filters, CSV, roles, capability and tenancy, and the
access check."""

import csv
import io
import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from catalog import quality, services
from catalog.models import Application, Resource
from core import connections, graph
from core.models import AuditEvent
from core.problems import FieldErrors
from core.request_context import forget_organization
from discovery import access, coverage, entra_coverage, entra_simulator
from discovery import engine as discovery
from discovery.entra_simulator import EVERYONE, PHISHING_RESISTANT, ca_policy, grant
from discovery.graph_source import GRAPH_AUDIENCE, Client
from discovery.models import AppCoverage, Connector, ConnectorRun, Observation, RunStatus

SECRET = "synthetic-secret"
NOW = datetime(2026, 11, 9, 2, 0, tzinfo=UTC)
CA_PATH = entra_simulator.CA_PATH
APP = "11111111-1111-4111-8111-111111111111"
ALL = {"includeUsers": ["All"]}  # all users, nobody excluded
# Queries a report page (6 measured) and an application detail (15 measured, the whole detail) may take, with a
# small margin; the report stays flat whatever the number of rows.
REPORT_QUERIES = 8
DETAIL_QUERIES = 18
OTHER = "22222222-2222-4222-8222-222222222222"


def app_id(name):
    return entra_simulator._id(f"app/{name}")


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """One clock for the source, and a fake monotonic clock that sleeping advances (no test ever waits)."""
    state: dict[str, Any] = {"now": NOW, "mono": 1000.0, "slept": []}

    def sleep(seconds):
        state["slept"].append(seconds)
        state["mono"] += seconds

    monkeypatch.setattr(entra_coverage, "clock", lambda: state["now"])
    monkeypatch.setattr(graph, "sleep", sleep)
    monkeypatch.setattr(graph, "monotonic", lambda: state["mono"])
    return state


def make_coverage(org, actor, scope=None):
    data: dict[str, Any] = {"source": "entra_coverage", "tenant_id": "simulated", "client_id": "", "secret": SECRET}
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


LINKED = {
    # record name: (simulated app, criticality)
    "Workday HR": ("Workday", "critical"),
    "Zoom Meetings": ("Zoom", "high"),
    "ServiceNow ITSM": ("ServiceNow", "high"),
    "Expense Hub": ("Expense Hub", "medium"),
    "Finance Ledger": ("Finance Ledger SSO", "critical"),
    "Contract Vault": ("Contract Vault", "low"),
    "Payroll Sync": ("Payroll Sync Daemon", "medium"),
    "Microsoft Teams": ("Microsoft Teams", "low"),
}


def make_record(org, actor, name, **fields):
    return services.create_resource(org, actor, {"name": name, "kind": "application", "lifecycle": "active", **fields})


@pytest.fixture
def records(a):
    org, editor = a["org"], a["users"]["editor"]
    return {
        name: make_record(org, editor, name, entra_app_id=app_id(app), criticality=crit)
        for name, (app, crit) in LINKED.items()
    }


@pytest.fixture
def cov(a, records):
    return make_coverage(a["org"], a["users"]["admin"])


def block_of(org, resource) -> dict[str, Any]:
    block = coverage.record_block(org, resource)
    assert block is not None
    return block


def row(connector, name):
    return AppCoverage.objects.get(connector=connector, app_id=app_id(name))


# ---- single sign-on ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sp, sso",
    [
        ({"preferredSingleSignOnMode": "saml"}, "saml"),
        ({"preferredSingleSignOnMode": "oidc"}, "oidc"),
        ({"preferredSingleSignOnMode": "password"}, "password"),
        ({"preferredSingleSignOnMode": "notSupported"}, "none"),
        ({"preferredSingleSignOnMode": None}, "unknown"),  # older SAML and OIDC apps: never "no SSO"
        ({}, "unknown"),
        ({"preferredSingleSignOnMode": None, "preferredTokenSigningKeyThumbprint": "AB12"}, "saml"),
        # SAML settings count only when they say something (a relay state); a dict of nulls doesn't.
        ({"preferredSingleSignOnMode": None, "samlSingleSignOnSettings": {"relayState": None}}, "unknown"),
        (
            {
                "preferredSingleSignOnMode": None,
                "samlSingleSignOnSettings": {"relayState": "https://app.example.test/"},
            },
            "saml",
        ),
        ({"preferredSingleSignOnMode": None, "samlSingleSignOnSettings": {"relayState": "  "}}, "unknown"),
        ({"preferredSingleSignOnMode": "somethingNew"}, "unknown"),
        ({"preferredSingleSignOnMode": 3}, "unknown"),
    ],
)
def test_every_sso_mode(sp, sso):
    found, mode, why = entra_coverage.sso_of(sp)
    assert found == sso and why
    if sp.get("preferredSingleSignOnMode") is None and sso == "unknown":
        assert "isn't known" in why and mode == ""


# ---- provisioning ----------------------------------------------------------------------------------------------------


def jobs(*pairs):
    return [{"schedule": {"state": s}, "status": {"code": c}} for s, c in pairs]


@pytest.mark.parametrize(
    "found, scim, words",
    [
        ([], "off", "No provisioning job"),
        (None, "off", "No provisioning job"),
        (jobs(("Active", "Active")), "on", "on"),
        (jobs(("Active", "NotRun")), "on", "on"),
        (jobs(("Active", "NotConfigured")), "on", "on"),
        (jobs(("Active", "Paused")), "off", "paused"),
        (jobs(("Active", "Quarantine")), "off", "quarantine"),
        (jobs(("Paused", "Active")), "off", "paused"),
        (jobs(("Disabled", "NotRun")), "off", "isn't active"),
        (jobs(("Disabled", "NotRun"), ("Active", "Active")), "on", "1 job"),
        (jobs(("active", "quarantine")), "off", "quarantine"),  # case doesn't matter
    ],
)
def test_every_provisioning_state(found, scim, words):
    state, why = entra_coverage.provisioning_of(found)
    assert state == scim and words in why


# ---- conditional access ----------------------------------------------------------------------------------------------


def policy(name="P", state="enabled", apps=None, users=None, controls=None, conditions=None):
    return ca_policy(
        name,
        state,
        apps if apps is not None else {"includeApplications": [APP]},
        users if users is not None else EVERYONE,
        controls if controls is not None else grant("mfa"),
        conditions,
    )


@pytest.mark.parametrize(
    "policies, mfa, effects",
    [
        # MFA for everyone, by app ID or All; a single control with Graph's default OR still requires it.
        ([policy()], "all_users", ["mfa_all_users"]),
        ([policy(apps={"includeApplications": ["All"]})], "all_users", ["mfa_all_users"]),
        ([policy(controls=grant("mfa", "compliantDevice", operator="AND"))], "all_users", ["mfa_all_users"]),
        ([policy(controls=grant(strength=PHISHING_RESISTANT))], "all_users", ["mfa_all_users"]),
        # Exclusions: the app excluded from All, or another app's policy.
        ([policy(apps={"includeApplications": ["All"], "excludeApplications": [APP]})], "none", []),
        ([policy(apps={"includeApplications": [OTHER]})], "none", []),
        # Only some users: admins (roles), a group, guests, listed people; or only some sign-ins.
        ([policy(users={"includeRoles": [entra_simulator.GLOBAL_ADMIN_ROLE]})], "some_users", ["mfa_some_users"]),
        ([policy(users={"includeGroups": ["g1"]})], "some_users", ["mfa_some_users"]),
        (
            [policy(users={"includeGuestsOrExternalUsers": {"guestOrExternalUserTypes": "b2bCollaborationGuest"}})],
            "some_users",
            ["mfa_some_users"],
        ),
        ([policy(users={"includeUsers": ["u1", "u2"]})], "some_users", ["mfa_some_users"]),
        (
            [policy(conditions={"locations": {"includeLocations": ["All"], "excludeLocations": ["AllTrusted"]}})],
            "some_users",
            ["mfa_some_users"],
        ),
        ([policy(conditions={"signInRiskLevels": ["high"]})], "some_users", ["mfa_some_users"]),
        ([policy(conditions={"clientAppTypes": ["browser"]})], "some_users", ["mfa_some_users"]),
        # Nobody: users None, or a workload identity policy.
        ([policy(users={"includeUsers": ["None"]})], "none", []),
        # Not enforced: report-only is listed, disabled isn't.
        ([policy(state="enabledForReportingButNotEnforced")], "none", ["report_only"]),
        ([policy(state="disabled")], "none", []),
        # OR with another control: MFA is optional.
        ([policy(controls=grant("mfa", "compliantDevice"))], "none", ["mfa_optional"]),
        ([policy(controls={**grant("mfa"), "termsOfUse": ["tou"]})], "none", ["mfa_optional"]),
        # A block for everyone isn't a missing MFA policy; a block for some users changes nothing.
        ([policy(controls=grant("block"), users=ALL)], "blocked", ["blocked"]),
        # A block with any exclusion lets someone in (break-glass, or "everyone except Sales"): it decides nothing.
        ([policy(controls=grant("block"))], "none", ["blocked"]),
        (
            [policy(controls=grant("block"), users={"includeUsers": ["All"], "excludeGroups": ["sales"]})],
            "none",
            ["blocked"],
        ),
        (
            [
                policy(
                    "Block all but Sales",
                    controls=grant("block"),
                    users={"includeUsers": ["All"], "excludeGroups": ["sales"]},
                ),
                policy("MFA for Sales", users={"includeGroups": ["sales"]}),
            ],
            "some_users",
            ["blocked", "mfa_some_users"],
        ),
        (
            [
                policy(
                    "Block all but Sales",
                    controls=grant("block"),
                    users={"includeUsers": ["All"], "excludeGroups": ["sales"]},
                ),
                policy(
                    "Compliant device for Sales", users={"includeGroups": ["sales"]}, controls=grant("compliantDevice")
                ),
            ],
            "none",
            ["blocked", "no_mfa"],
        ),
        ([policy(controls=grant("block"), users={"includeRoles": ["r"]})], "none", ["blocked"]),
        (
            [policy(controls=grant("block"), conditions={"clientAppTypes": ["exchangeActiveSync", "other"]})],
            "none",
            ["blocked"],
        ),
        ([policy("A", controls=grant("block"), users=ALL), policy("B")], "blocked", ["blocked", "mfa_all_users"]),
        # Every condition that narrows a policy to some sign-ins, including ones Softwarden doesn't know.
        (
            [policy(conditions={"authenticationFlows": {"transferMethods": "deviceCodeFlow"}})],
            "some_users",
            ["mfa_some_users"],
        ),
        ([policy(conditions={"insiderRiskLevels": "elevated"})], "some_users", ["mfa_some_users"]),
        (
            [policy(conditions={"devices": {"includeDevices": ["All"], "excludeDevices": ["Compliant"]}})],
            "some_users",
            ["mfa_some_users"],
        ),
        ([policy(conditions={"devices": {"includeDevices": ["Compliant"]}})], "some_users", ["mfa_some_users"]),
        (
            [policy(conditions={"devices": {"deviceFilter": {"mode": "exclude", "rule": "x"}}})],
            "some_users",
            ["mfa_some_users"],
        ),
        ([policy(conditions={"servicePrincipalRiskLevels": ["high"]})], "some_users", ["mfa_some_users"]),
        (
            [policy(conditions={"clientApplications": {"includeServicePrincipals": ["sp"]}})],
            "some_users",
            ["mfa_some_users"],
        ),
        ([policy(conditions={"someFutureCondition": {"include": ["x"]}})], "some_users", ["mfa_some_users"]),
        # Empty or null conditions narrow nothing.
        (
            [
                policy(
                    conditions={
                        "authenticationFlows": None,
                        "insiderRiskLevels": None,
                        "devices": {"includeDevices": ["All"], "excludeDevices": []},
                        "servicePrincipalRiskLevels": [],
                        "clientApplications": {"includeServicePrincipals": [], "excludeServicePrincipals": []},
                        "someFutureCondition": {"include": []},
                        "@odata.type": "#microsoft.graph.conditionalAccessConditionSet",
                    }
                )
            ],
            "all_users",
            ["mfa_all_users"],
        ),
        ([policy(controls=None) | {"grantControls": None}], "none", ["no_mfa"]),
        # Application filters can't be evaluated.
        (
            [policy(apps={"includeApplications": [APP], "applicationFilter": {"mode": "exclude", "rule": "x"}})],
            "unknown",
            ["may_require_mfa"],
        ),
        (
            [policy(apps={"includeApplications": [], "applicationFilter": {"mode": "include", "rule": "x"}})],
            "unknown",
            ["may_require_mfa"],
        ),
        (
            [policy(apps={"includeApplications": [APP], "applicationFilter": {"mode": "include", "rule": "x"}})],
            "all_users",
            ["mfa_all_users"],
        ),
        (
            [policy(apps={"includeApplications": [OTHER], "applicationFilter": {"mode": "exclude", "rule": "x"}})],
            "none",
            [],
        ),
        # Strongest wins; report-only and optional never count.
        (
            [
                policy("A", users={"includeRoles": ["r"]}),
                policy("B"),
                policy("C", state="enabledForReportingButNotEnforced"),
            ],
            "all_users",
            ["mfa_all_users", "mfa_some_users", "report_only"],
        ),
        (
            [policy("A", controls=grant("mfa", "compliantDevice")), policy("B", users={"includeRoles": ["r"]})],
            "some_users",
            ["mfa_some_users", "mfa_optional"],
        ),
    ],
)
def test_every_conditional_access_shape(policies, mfa, effects):
    found, why, matched = entra_coverage.mfa_coverage(policies, APP, microsoft=False)
    assert found == mfa and why
    assert [m["effect"] for m in matched] == effects


@pytest.mark.parametrize("group", ["Office365", "MicrosoftAdminPortals"])
@pytest.mark.parametrize(
    "apps",
    [
        {"includeApplications": ["All"]},
        {"includeApplications": [APP]},
    ],
)
def test_excluding_a_microsoft_app_group_makes_a_microsoft_app_unknown(group, apps):
    excluding = [policy(apps={**apps, "excludeApplications": [group]})]
    found, why, matched = entra_coverage.mfa_coverage(excluding, APP, microsoft=True)
    assert found == "unknown" and "excludes the" in why and matched[0]["applies"] == "maybe"
    # A third-party app is never in a Microsoft app group: the policy covers it.
    assert entra_coverage.mfa_coverage(excluding, APP, microsoft=False)[0] == "all_users"


def test_microsoft_app_groups_are_unknown_for_microsoft_apps_only():
    office = [policy(apps={"includeApplications": ["Office365"]})]
    assert entra_coverage.mfa_coverage(office, APP, microsoft=True)[0] == "unknown"
    assert "Office 365" in entra_coverage.mfa_coverage(office, APP, microsoft=True)[1]
    assert entra_coverage.mfa_coverage(office, APP, microsoft=False)[0] == "none"


def test_exclusions_are_counted_and_still_all_users():
    found, why, matched = entra_coverage.mfa_coverage(
        [policy(users={"includeUsers": ["All"], "excludeUsers": ["b1", "b2"], "excludeGroups": ["g"]})], APP, False
    )
    assert found == "all_users" and matched[0]["exclusions"] == 3 and "3 exclusion(s)" in why


@pytest.mark.parametrize(
    "controls, required, optional",
    [
        (None, False, False),
        (grant(), False, False),
        (grant("mfa"), True, False),
        (grant("mfa", operator="AND"), True, False),
        (grant("mfa", "domainJoinedDevice"), False, True),
        (grant("compliantDevice", "domainJoinedDevice", operator="AND"), False, False),
        (grant(strength={"requirementsSatisfied": "mfa"}), True, False),
        (grant(strength={"requirementsSatisfied": "none"}), False, False),
        (grant("compliantDevice", strength={"requirementsSatisfied": "mfa"}), False, True),
    ],
)
def test_what_counts_as_requiring_mfa(controls, required, optional):
    assert entra_coverage.requires_mfa(controls) == (required, optional)


# ---- the run -------------------------------------------------------------------------------------------------------


EXPECTED = {
    # app: (sso, scim, mfa)
    "Workday": ("saml", "on", "all_users"),
    "Zoom": ("saml", "off", "none"),  # null mode with a SAML certificate; paused; report-only policy only
    "ServiceNow": ("unknown", "off", "none"),  # null mode; quarantine; MFA or a compliant device
    "Expense Hub": ("oidc", "off", "all_users"),  # schedule disabled; authentication strength
    "Finance Ledger SSO": ("password", "on", "some_users"),  # NotRun counts as on; administrators only
    "Contract Vault": ("none", "off", "unknown"),  # notSupported; no job; an application filter
    "Payroll Sync Daemon": ("none", "off", "none"),
    "Microsoft Teams": ("unknown", "off", "unknown"),  # Office 365 app group
}


def test_a_run_records_coverage_for_linked_apps(a, cov):
    result = run(cov)
    assert result.status == RunStatus.SUCCEEDED, result.error
    found = {r.display_name: (r.sso, r.scim, r.mfa) for r in AppCoverage.objects.filter(connector=cov)}
    assert found == EXPECTED
    assert AppCoverage.objects.filter(connector=cov).count() == len(EXPECTED)  # only the linked apps
    zoom, servicenow = row(cov, "Zoom"), row(cov, "ServiceNow")
    assert zoom.sso_mode == "" and "SAML token signing certificate" in zoom.sso_detail
    assert "paused" in zoom.scim_detail and "report-only" in zoom.mfa_detail
    assert "quarantine" in servicenow.scim_detail and "accepts MFA or another control" in servicenow.mfa_detail
    workday = row(cov, "Workday")
    assert workday.policies[0]["name"] == "Require MFA for Workday" and workday.policies[0]["exclusions"] == 1
    assert workday.scim_checked_at == NOW and workday.seen_at == NOW and workday.first_seen_at == NOW
    assert "filter" in row(cov, "Contract Vault").mfa_detail
    counts = result.counts
    assert counts["kept"] == len(EXPECTED) and counts["seen"] > len(EXPECTED)
    assert counts["ca_policies"] == 8 and counts["scim_read"] == len(EXPECTED)
    assert counts["sso_none"] == 2 and counts["mfa_unknown"] == 2 and counts["new"] == 0
    assert not Observation.objects.filter(connector=cov).exists()  # coverage is never a candidate


def test_the_calls_made(a, cov):
    transport = entra_simulator.Transport(cov)
    run(cov, transport=transport)
    paths = [urllib.parse.urlsplit(u).path for m, u in transport.calls if m == "GET"]
    assert paths.count(CA_PATH) == 1  # read once per run
    assert sum(p.endswith("/synchronization/jobs") for p in paths) == len(EXPECTED)  # one per linked app only
    listing = next(u for m, u in transport.calls if urllib.parse.urlsplit(u).path == "/v1.0/servicePrincipals")
    assert "preferredSingleSignOnMode" in urllib.parse.unquote(listing)
    assert all(m in ("GET", "POST") for m, _u in transport.calls)  # nothing writes to the tenant


def test_an_app_linked_through_its_entra_candidate(a):
    org, admin, editor = a["org"], a["users"]["admin"], a["users"]["editor"]
    entra = discovery.create_connector(
        org, admin, {"source": "entra", "tenant_id": "simulated", "client_id": "", "secret": SECRET}
    )
    connections.run_test(org, admin, entra.microsoft_connection)
    run(Connector.objects.get(pk=entra.pk))
    record = make_record(org, editor, "Ledger (no Entra ID set)")
    candidate = Observation.objects.get(connector_id=entra.pk, display_name="Finance Ledger SSO")
    discovery.link(org, editor, candidate, {"resource": str(record.id)})
    cov = make_coverage(org, admin)
    run(cov)
    block = block_of(org, record)
    assert block["linked_by"] == "candidate" and block["entra_app_id"] == app_id("Finance Ledger SSO")
    assert (block["sso"], block["scim"], block["mfa"]) == ("password", "on", "some_users")
    assert block["connector"] == {"id": str(cov.id), "name": cov.name}


def test_without_p1_sso_and_scim_are_still_reported(a, cov):
    configure(cov, simulated_no_premium=True)
    result = run(cov)
    assert result.status == RunStatus.SUCCEEDED, result.error
    workday = row(cov, "Workday")
    assert (workday.sso, workday.scim, workday.mfa) == ("saml", "on", "unknown")
    assert "needs Microsoft Entra ID P1" in workday.mfa_detail and workday.policies == []
    assert any("P1" in n for n in result.counts["notes"]) and "ca_policies" not in result.counts
    assert AppCoverage.objects.filter(connector=cov, mfa="unknown").count() == len(EXPECTED)


def test_without_policy_read_all_mfa_is_unknown_and_named(a, cov):
    granted = [p for p in entra_simulator.GRANTED if p != "Policy.Read.All"]
    configure(cov, simulated_permissions=granted)
    result = run(cov)
    assert result.status == RunStatus.SUCCEEDED
    assert any("Policy.Read.All" in n for n in result.counts["notes"])
    assert set(AppCoverage.objects.filter(connector=cov).values_list("mfa", flat=True)) == {"unknown"}
    assert row(cov, "Workday").sso == "saml"


def test_without_synchronization_read_all_provisioning_is_unknown_after_one_refusal(a, cov):
    granted = [p for p in entra_simulator.GRANTED if p != "Synchronization.Read.All"]
    configure(cov, simulated_permissions=granted)
    transport = entra_simulator.Transport(cov)
    result = run(cov, transport=transport)
    assert result.status == RunStatus.SUCCEEDED
    assert sum(u.endswith("/synchronization/jobs") for _m, u in transport.calls) == 1  # stops at the first refusal
    assert set(AppCoverage.objects.filter(connector=cov).values_list("scim", flat=True)) == {"unknown"}
    assert any("Synchronization.Read.All" in n for n in result.counts["notes"])
    assert row(cov, "Workday").mfa == "all_users"


def test_a_404_on_provisioning_jobs_is_off(a, cov):
    path = f"/v1.0/servicePrincipals/{entra_simulator._id('Workday')}/synchronization/jobs"
    configure(cov, simulated_errors={path: 404})
    run(cov)
    assert (row(cov, "Workday").scim, row(cov, "Workday").scim_detail) == ("off", "No provisioning job.")


def test_scope_off_reads_neither(a, records):
    cov = make_coverage(a["org"], a["users"]["admin"], scope={"provisioning": False, "conditional_access": False})
    transport = entra_simulator.Transport(cov)
    run(cov, transport=transport)
    paths = {urllib.parse.urlsplit(u).path for _m, u in transport.calls}
    assert CA_PATH not in paths and not any(p.endswith("/synchronization/jobs") for p in paths)
    workday = row(cov, "Workday")
    assert (workday.sso, workday.scim, workday.mfa) == ("saml", "unknown", "unknown")
    assert "turned off" in workday.scim_detail and "turned off" in workday.mfa_detail


def test_scope_validation(a):
    with pytest.raises(FieldErrors) as refused:
        make_coverage(a["org"], a["users"]["admin"], scope={"provisioning": "yes", "conditional_access": 1})
    assert set(refused.value.errors) == {"scope.provisioning", "scope.conditional_access"}
    with pytest.raises(FieldErrors) as refused:
        make_coverage(a["org"], a["users"]["admin"], scope={"sign_ins": True})
    assert "scope" in refused.value.errors


# ---- throttling ------------------------------------------------------------------------------------------------------


def test_conditional_access_throttling_backs_off_without_retry_after(a, cov, clock):
    configure(cov, simulated_throttle={"path": CA_PATH, "times": 2, "retry_after": None})
    result = run(cov)
    assert result.status == RunStatus.SUCCEEDED
    assert clock["slept"] == [2.0, 4.0]  # its own backoff: no Retry-After to wait
    assert result.counts["ca_retries"] == 2 and row(cov, "Workday").mfa == "all_users"


# ---- transient failures keep the last run's answers ----------------------------------------------------------------


def later_run(cov, clock, hours=24, transport=None):
    clock["now"] = NOW + timedelta(hours=hours)
    return run(cov, now=clock["now"], transport=transport)


@pytest.mark.parametrize(
    "config",
    [
        {"simulated_throttle": {"path": CA_PATH, "times": 50, "retry_after": None}},
        {"simulated_errors": {CA_PATH: 500}},
        {"simulated_errors": {CA_PATH: 503}},
    ],
)
def test_a_transient_conditional_access_failure_keeps_last_runs_answers(a, cov, clock, config):
    run(cov)
    before = {r.app_id: (r.mfa, r.mfa_detail, r.policies) for r in AppCoverage.objects.filter(connector=cov)}
    configure(cov, **config)
    result = later_run(cov, clock)
    assert result.status == RunStatus.SUCCEEDED
    after = {r.app_id: (r.mfa, r.mfa_detail, r.policies) for r in AppCoverage.objects.filter(connector=cov)}
    assert after == before and row(cov, "Payroll Sync Daemon").mfa == "none"  # mfa_missing still fires
    assert result.counts["mfa_carried"] == len(EXPECTED)
    assert any("last run's answers" in n for n in result.counts["notes"])


def test_a_transient_failure_without_an_earlier_answer_is_unknown(a, cov):
    configure(cov, simulated_errors={CA_PATH: 500})
    result = run(cov)
    assert set(AppCoverage.objects.filter(connector=cov).values_list("mfa", flat=True)) == {"unknown"}
    assert "mfa_carried" not in result.counts


def test_an_unreachable_graph_for_conditional_access_keeps_last_runs_answers(a, cov, clock):
    run(cov)

    class Unreachable(entra_simulator.Transport):
        def send(self, method, url, headers, body=None):
            if urllib.parse.urlsplit(url).path == CA_PATH:
                raise graph.Unreachable("no route", ambiguous=False)
            return super().send(method, url, headers, body)

    result = later_run(cov, clock, transport=Unreachable(cov))
    assert result.status == RunStatus.SUCCEEDED and result.counts["mfa_carried"] == len(EXPECTED)
    assert row(cov, "Workday").mfa == "all_users"


@pytest.mark.parametrize("change", ["permission", "licence", "scope"])
def test_a_refusal_or_the_scope_resets_mfa_to_unknown(a, cov, clock, change):
    run(cov)
    if change == "permission":
        configure(cov, simulated_permissions=[p for p in entra_simulator.GRANTED if p != "Policy.Read.All"])
    elif change == "licence":
        configure(cov, simulated_no_premium=True)
    else:
        discovery.update_connector(a["org"], a["users"]["admin"], cov, {"scope": {"conditional_access": False}})
    result = later_run(cov, clock)
    assert set(AppCoverage.objects.filter(connector=cov).values_list("mfa", flat=True)) == {"unknown"}
    assert AppCoverage.objects.filter(connector=cov, policies=[]).count() == len(EXPECTED)
    assert "mfa_carried" not in result.counts


def test_a_server_error_on_one_apps_provisioning_keeps_its_last_answer(a, cov, clock):
    run(cov)
    path = f"/v1.0/servicePrincipals/{entra_simulator._id('Workday')}/synchronization/jobs"
    configure(cov, simulated_errors={path: 500})
    result = later_run(cov, clock)
    workday = row(cov, "Workday")
    assert (workday.scim, workday.scim_checked_at) == ("on", NOW)  # last run's answer and date
    assert row(cov, "Zoom").scim_checked_at == clock["now"]  # the others were read again
    assert result.counts["scim_carried"] == 1
    assert any("1 app(s)" in n for n in result.counts["notes"])


def test_provisioning_throttled_past_the_budget_keeps_last_answers(a, cov, clock):
    run(cov)
    before = {r.app_id: r.scim for r in AppCoverage.objects.filter(connector=cov)}
    first = sorted(AppCoverage.objects.filter(connector=cov), key=lambda r: r.app_id)[0]
    path = f"/v1.0/servicePrincipals/{first.service_principal_id}/synchronization/jobs"
    configure(cov, simulated_throttle={"path": path, "times": 50, "retry_after": "500"})  # longer than a run waits
    result = later_run(cov, clock)
    assert result.status == RunStatus.SUCCEEDED
    assert {r.app_id: r.scim for r in AppCoverage.objects.filter(connector=cov)} == before
    assert result.counts["scim_carried"] == len(EXPECTED) and result.counts["scim_read"] == 0
    assert any("stopped" in n for n in result.counts["notes"])


def test_a_refused_provisioning_read_resets_to_unknown(a, cov, clock):
    run(cov)
    configure(cov, simulated_permissions=[p for p in entra_simulator.GRANTED if p != "Synchronization.Read.All"])
    result = later_run(cov, clock)
    assert set(AppCoverage.objects.filter(connector=cov).values_list("scim", flat=True)) == {"unknown"}
    assert "scim_carried" not in result.counts


def test_conditional_access_throttling_that_wont_stop_leaves_mfa_unknown(a, cov, clock):
    configure(cov, simulated_throttle={"path": CA_PATH, "times": 50, "retry_after": None})
    result = run(cov)
    assert result.status == RunStatus.SUCCEEDED  # never a failed run
    assert clock["slept"] == [2.0, 4.0, 8.0, 16.0]
    assert "throttling" in row(cov, "Workday").mfa_detail and row(cov, "Workday").sso == "saml"
    assert any("throttling" in n for n in result.counts["notes"])


def test_conditional_access_waits_retry_after_when_given(a, cov, clock):
    configure(cov, simulated_throttle={"path": CA_PATH, "times": 1, "retry_after": "7"})
    run(cov)
    assert clock["slept"] == [7.0]


def test_conditional_access_pages_are_paced_on_a_real_tenant(a, cov, clock):
    configure(cov, simulated_ca_page=3)
    client = Client(entra_simulator.Transport(cov), connections.credential(discovery.connection_of(cov)))
    read = entra_coverage.read_policies(client, entra_coverage.CA_PACE)
    assert len(read.policies or []) == 8
    assert clock["slept"] == pytest.approx([1.1, 1.1])  # three pages, a second apart (one request a second)
    assert GRAPH_AUDIENCE.allows(entra_coverage.CA_URL)


# ---- the provisioning cap ------------------------------------------------------------------------------------------


def test_provisioning_reads_are_capped_least_recently_read_first(a, cov, clock, monkeypatch):
    monkeypatch.setattr(entra_coverage, "SCIM_LIMIT", 3)

    def read_by(transport):
        sync = [u for _m, u in transport.calls if u.endswith("/synchronization/jobs")]
        return {u.split("/servicePrincipals/")[1].split("/")[0] for u in sync}

    first = entra_simulator.Transport(cov)
    result = run(cov, transport=first)
    assert len(read_by(first)) == 3 and result.counts["scim_read"] == 3
    assert any("3 of 8" in n for n in result.counts["notes"])
    rows = list(AppCoverage.objects.filter(connector=cov))
    checked = {r.service_principal_id for r in rows if r.scim_checked_at}
    assert checked == read_by(first)
    waiting = [r for r in rows if not r.scim_checked_at]
    assert {r.scim for r in waiting} == {"unknown"} and all("at most 3" in r.scim_detail for r in waiting)
    clock["now"] = NOW + timedelta(days=1)
    second = entra_simulator.Transport(cov)
    run(cov, now=clock["now"], transport=second)
    assert read_by(second).isdisjoint(read_by(first))  # never read first, then the oldest
    kept = AppCoverage.objects.filter(connector=cov, service_principal_id__in=read_by(first))
    assert all(r.scim_checked_at == NOW and r.scim != "unknown" for r in kept)  # the last answer stays
    clock["now"] = NOW + timedelta(days=2)
    third = entra_simulator.Transport(cov)
    run(cov, now=clock["now"], transport=third)
    assert len(read_by(first) | read_by(second) | read_by(third)) == 8
    assert not AppCoverage.objects.filter(connector=cov, scim_checked_at__isnull=True).exists()


# ---- the empty-listing guard -----------------------------------------------------------------------------------------


class EmptyListing(entra_simulator.Transport):
    """A tenant whose service principal listing comes back empty (a broken read, not a real tenant)."""

    def send(self, method, url, headers, body=None):
        if urllib.parse.urlsplit(url).path == "/v1.0/servicePrincipals":
            self.calls.append((method, url))
            return entra_simulator._json(200, {"value": []})
        return super().send(method, url, headers, body)


def test_an_empty_listing_is_refused_while_coverage_exists(a, cov):
    run(cov)
    before = AppCoverage.objects.filter(connector=cov).count()
    result = run(cov, now=NOW + timedelta(hours=1), transport=EmptyListing(cov))
    assert result.status == RunStatus.FAILED
    assert "listed no enterprise applications" in result.error and "Nothing was changed" in result.error
    assert AppCoverage.objects.filter(connector=cov).count() == before
    assert AppCoverage.objects.filter(connector=cov, seen_at=NOW).count() == before


def test_an_empty_listing_without_coverage_is_an_empty_run(a, cov):
    result = run(cov, transport=EmptyListing(cov))
    assert result.status == RunStatus.SUCCEEDED and result.counts["kept"] == 0


def test_an_unlinked_app_loses_its_row(a, cov, records):
    run(cov)
    services.archive_resource(a["org"], a["users"]["editor"], records["Workday HR"])
    result = run(cov, now=NOW + timedelta(hours=1))
    assert result.counts["coverage_removed"] == 1
    assert not AppCoverage.objects.filter(connector=cov, app_id=app_id("Workday")).exists()


# ---- curated data ----------------------------------------------------------------------------------------------------


def test_curated_fields_are_never_written_and_differences_are_shown(a, cov, records):
    org, editor = a["org"], a["users"]["editor"]
    workday = records["Workday HR"]
    services.update_resource(org, editor, workday, {"sso_enabled": False, "scim_provisioning": True})
    services.update_resource(org, editor, records["Contract Vault"], {"sso_enabled": False, "scim_provisioning": False})
    updates = AuditEvent.objects.filter(organization=org, action="resource.updated").count()
    run(cov)
    assert AuditEvent.objects.filter(organization=org, action="resource.updated").count() == updates
    stored = Application.objects.get(resource=workday)
    assert (stored.sso_enabled, stored.scim_provisioning) == (False, True)  # untouched
    block = block_of(org, Resource.objects.get(pk=workday.pk))
    assert block["differs"] == [
        {"field": "sso_enabled", "label": "SSO enabled", "curated": False, "source": True},
    ]
    vault = block_of(org, Resource.objects.get(pk=records["Contract Vault"].pk))
    assert vault["differs"] == []  # Entra agrees: no SSO, no provisioning
    zoom = block_of(org, Resource.objects.get(pk=records["Zoom Meetings"].pk))
    assert zoom["differs"] == [  # not recorded: Entra's answer is offered
        {"field": "sso_enabled", "label": "SSO enabled", "curated": None, "source": True},
        {"field": "scim_provisioning", "label": "SCIM provisioning", "curated": None, "source": False},
    ]
    service_now = block_of(org, Resource.objects.get(pk=records["ServiceNow ITSM"].pk))
    assert [d["field"] for d in service_now["differs"]] == ["scim_provisioning"]  # unknown SSO proposes nothing


def test_password_sso_counts_as_sso_for_the_record():
    assert coverage.source_values("password", "unknown") == {"sso_enabled": True}
    assert coverage.source_values("none", "on") == {"sso_enabled": False, "scim_provisioning": True}
    assert coverage.source_values("unknown", "unknown") == {}


# ---- gaps and data quality -----------------------------------------------------------------------------------------


def test_gap_chips_use_the_ownership_language():
    assert coverage.gaps("none", "off", "none") == ["No SSO", "No SCIM", "No MFA policy"]
    assert coverage.gaps("saml", "on", "some_users") == ["MFA for some users only"]
    assert coverage.gaps("unknown", "on", "all_users") == ["Coverage unknown"]
    assert coverage.gaps("saml", "on", "all_users") == []


def test_the_detail_shows_coverage_and_gaps(a, api, cov, records):
    run(cov)
    body = api(a["users"]["viewer"]).get(f"applications/{records['Payroll Sync'].id}").json_body
    block = body["sso_coverage"]
    assert (block["sso"], block["scim"], block["mfa"]) == ("none", "off", "none")
    assert block["gaps"] == ["No SSO", "No SCIM", "No MFA policy"] and block["linked_by"] == "entra_app_id"
    assert block["seen_at"].endswith("Z") and block["connector"]["id"] == str(cov.id)
    unlinked = api(a["users"]["viewer"]).get(f"applications/{a['app'].id}").json_body["sso_coverage"]
    assert unlinked["entra_app_id"] is None and unlinked["gaps"] == ["Coverage unknown"]
    assert unlinked["sso_detail"] == coverage.NOT_LINKED


def test_unknown_says_why(a, records):
    org = a["org"]
    block = block_of(org, records["Workday HR"])
    assert block["sso_detail"] == coverage.NO_CONNECTOR
    make_coverage(org, a["users"]["admin"])
    assert block_of(org, records["Workday HR"])["mfa_detail"] == coverage.NOT_SEEN


def test_data_quality_rules(a, cov, records):
    org = a["org"]
    run(cov)
    ids = {name: r.id for name, r in records.items()} | {"unlinked": a["app"].id}
    results = quality.evaluate(org, ids.values())

    def failing(name):
        return {r.id for r in results[ids[name]].failing} & {"sso_missing", "mfa_missing", "scim_missing"}

    assert failing("Workday HR") == set()
    assert failing("Contract Vault") == {"sso_missing", "scim_missing"}  # MFA unknown: neither pass nor fail
    assert failing("Payroll Sync") == {"sso_missing", "mfa_missing", "scim_missing"}
    assert failing("Zoom Meetings") == {"mfa_missing", "scim_missing"}
    assert failing("Finance Ledger") == set()  # password SSO; MFA for some users isn't "no MFA"
    assert failing("unlinked") == set()
    rules = {r.id: r for r in quality.RULES}
    assert rules["sso_missing"].label == "No SSO" and rules["mfa_missing"].label == "No MFA policy"
    assert rules["scim_missing"].label == "No SCIM" and all(
        rules[r].capability == "discovery.entra" and rules[r].exceptable
        for r in ("sso_missing", "mfa_missing", "scim_missing")
    )
    failing_ids = set(
        Resource.objects.filter(quality.failing_q(org, rules["mfa_missing"])).values_list("id", flat=True)
    )
    assert failing_ids == {ids["Zoom Meetings"], ids["ServiceNow ITSM"], ids["Payroll Sync"]}


def test_data_quality_rules_need_the_capability(a, cov):
    from core.models import CapabilityOverride

    CapabilityOverride.objects.create(organization=a["org"], capability="discovery.entra", effect="revoke", reason="t")
    forget_organization(a["org"].pk)
    assert not {"sso_missing", "mfa_missing", "scim_missing"} & {r.id for r in quality.active_rules(a["org"])}


def test_the_data_quality_queue_lists_coverage_gaps(a, cov, records):
    from core.dates import org_today

    run(cov)
    rows = {r["rule"]: r for r in quality.attention_rows(a["org"], org_today(a["org"]))}
    assert {"sso_missing", "mfa_missing", "scim_missing"} <= set(rows)
    assert rows["mfa_missing"]["label"] == "No MFA policy" and rows["mfa_missing"]["kind"] == "data_quality"


# ---- the report ------------------------------------------------------------------------------------------------------


def report(api, user, query=""):
    response = api(user).get(f"reports/sso-coverage{'?' + query if query else ''}")
    assert response.status_code == 200, response.content
    return response.json_body


def test_the_report_lists_every_live_application(a, api, cov, records):
    run(cov)
    body = report(api, a["users"]["viewer"])
    rows = body["results"]
    names = [r["record"]["name"] for r in rows]
    assert names[:2] == ["Finance Ledger", "Workday HR"]  # critical first, then by name
    assert names[-1] == "orga App"  # no criticality: last
    assert len(rows) == len(LINKED) + 1
    workday = next(r for r in rows if r["record"]["name"] == "Workday HR")
    assert (workday["sso"], workday["scim"], workday["mfa"], workday["criticality"]) == (
        "saml",
        "on",
        "all_users",
        "critical",
    )
    assert workday["policies"][0]["effect"] == "mfa_all_users" and workday["gaps"] == []
    assert workday["record"] == {"id": str(records["Workday HR"].id), "name": "Workday HR", "kind": "application"}


@pytest.mark.parametrize(
    "query, names",
    [
        ("sso=none", {"Contract Vault", "Payroll Sync"}),
        ("sso=saml,oidc", {"Workday HR", "Zoom Meetings", "Expense Hub"}),
        ("sso=unknown", {"ServiceNow ITSM", "Microsoft Teams", "orga App"}),
        ("scim=on", {"Workday HR", "Finance Ledger"}),
        ("mfa=some_users", {"Finance Ledger"}),
        ("mfa=none&criticality=high", {"Zoom Meetings", "ServiceNow ITSM"}),
        ("criticality=none", {"orga App"}),
        ("gap=no_sso,mfa_partial", {"Contract Vault", "Payroll Sync", "Finance Ledger"}),
        ("gap=unknown&linked=true", {"ServiceNow ITSM", "Contract Vault", "Microsoft Teams"}),
        ("linked=false", {"orga App"}),
        ("q=vault", {"Contract Vault"}),
        (f"q={app_id('Zoom').upper()}", {"Zoom Meetings"}),
    ],
)
def test_report_filters(a, api, cov, query, names):
    run(cov)
    rows = report(api, a["users"]["viewer"], query)["results"]
    assert {r["record"]["name"] for r in rows} == names


@pytest.mark.parametrize(
    "query, field",
    [
        ("sso=maybe", "sso"),
        ("scim=yes", "scim"),
        ("mfa=everyone", "mfa"),
        ("criticality=urgent", "criticality"),
        ("gap=no_owner", "gap"),
        ("linked=yes", "linked"),
        ("owner=1", "owner"),
        ("ordering=vendor", "ordering"),
    ],
)
def test_report_refuses_bad_filters(a, api, query, field):
    response = api(a["users"]["viewer"]).get(f"reports/sso-coverage?{query}")
    assert response.status_code == 400 and field in response.json_body["errors"]


def test_report_orders_by_coverage_and_pages(a, api, cov):
    run(cov)
    viewer = a["users"]["viewer"]
    rows = report(api, viewer, "ordering=mfa,name")["results"]
    assert [r["mfa"] for r in rows][:4] == ["none", "none", "none", "some_users"]
    first = report(api, viewer, "ordering=name&page_size=5")
    second = report(api, viewer, f"ordering=name&page_size=5&cursor={first['next']}")
    names = [r["record"]["name"] for r in first["results"] + second["results"]]
    assert names == sorted(names, key=str.lower) and len(names) == len(LINKED) + 1


def test_report_leaves_out_archived_and_retired(a, api, cov, records):
    org, editor = a["org"], a["users"]["editor"]
    services.archive_resource(org, editor, records["Zoom Meetings"])
    services.update_resource(org, editor, records["Expense Hub"], {"lifecycle": "retired"})
    run(cov)
    names = {r["record"]["name"] for r in report(api, a["users"]["viewer"])["results"]}
    assert "Zoom Meetings" not in names and "Expense Hub" not in names


def test_report_csv_is_audited(a, api, cov):
    run(cov)
    response = api(a["users"]["viewer"]).get("reports/sso-coverage/export.csv?gap=no_mfa&page_size=1")
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    assert 'filename="softwarden-sso-coverage-' in response["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert [r["name"] for r in rows] == ["ServiceNow ITSM", "Zoom Meetings", "Payroll Sync"]
    assert rows[2]["gaps"] == "No SSO; No SCIM; No MFA policy" and rows[2]["sso"] == "none"
    assert rows[0]["policies"] == "MFA or a compliant device for ServiceNow (mfa_optional)"
    event = AuditEvent.objects.get(organization=a["org"], action="export")
    assert event.actor == a["users"]["viewer"]
    assert event.changes == {"kind": "sso_coverage", "rows": 3, "filters": {"gap": "no_mfa"}}
    bad = api(a["users"]["viewer"]).get("reports/sso-coverage/export.csv?mfa=maybe")
    assert bad.status_code == 400 and AuditEvent.objects.filter(action="export").count() == 1


def test_report_roles_and_methods(a, api, cov):
    assert api().get("reports/sso-coverage").status_code in (401, 403)
    assert api().get("reports/sso-coverage/export.csv").status_code in (401, 403)
    for role in ("viewer", "editor", "admin"):
        assert api(a["users"][role]).get("reports/sso-coverage").status_code == 200
    admin = api(a["users"]["admin"])
    assert admin.post("reports/sso-coverage", {}).status_code == 405
    assert admin.delete("reports/sso-coverage/export.csv").status_code == 405


def test_report_and_detail_need_the_capability(a, api, cov, records):
    from core.models import CapabilityOverride

    CapabilityOverride.objects.create(organization=a["org"], capability="discovery.entra", effect="revoke", reason="t")
    forget_organization(a["org"].pk)
    response = api(a["users"]["viewer"]).get("reports/sso-coverage")
    assert response.status_code == 403 and "discovery.entra" in json.dumps(response.json_body)
    detail = api(a["users"]["viewer"]).get(f"applications/{records['Workday HR'].id}").json_body
    assert detail["sso_coverage"] is None
    with pytest.raises(Exception, match="discovery.entra"):
        make_coverage(a["org"], a["users"]["admin"])


def test_the_csv_is_restricted_while_impersonating():
    from core.impersonation_routes import RESTRICTED

    routes = {route for entries in RESTRICTED.values() for _method, route in entries}
    assert "api/v1/reports/sso-coverage/export.csv" in routes


# ---- tenancy -------------------------------------------------------------------------------------------------------


def test_another_organization_sees_nothing(a, b, api, cov):
    run(cov)
    other = api(b["users"]["admin"])
    rows = other.get("reports/sso-coverage").json_body["results"]
    assert [r["record"]["name"] for r in rows] == ["orgb App"]
    lines = list(csv.reader(io.StringIO(other.get("reports/sso-coverage/export.csv").content.decode("utf-8-sig"))))
    assert len(lines) == 2  # the header and their own app
    assert other.get(f"applications/{a['app'].id}").status_code == 404


def test_another_organizations_coverage_never_covers_a_record(a, b, api, cov):
    run(cov)
    theirs = make_record(b["org"], b["users"]["editor"], "Their Workday", entra_app_id=app_id("Workday"))
    block = block_of(b["org"], theirs)
    assert block["sso"] == "unknown" and block["connector"] is None and block["sso_detail"] == coverage.NO_CONNECTOR
    row_ = api(b["users"]["viewer"]).get("reports/sso-coverage?linked=true").json_body["results"][0]
    assert row_["sso"] == "unknown" and row_["policies"] == []
    # Their own connector on the same simulated tenant covers it with their own row.
    mine = make_coverage(b["org"], b["users"]["admin"])
    run(mine)
    assert block_of(b["org"], theirs)["connector"]["id"] == str(mine.id)
    assert AppCoverage.objects.filter(organization=b["org"]).count() == 1
    assert AppCoverage.objects.filter(organization=a["org"]).count() == len(EXPECTED)


def test_another_organizations_links_are_not_read(a, b, cov):
    make_record(b["org"], b["users"]["editor"], "Theirs", entra_app_id=app_id("ServiceNow"))
    assert app_id("ServiceNow") not in entra_coverage.linked_app_ids(a["org"].id) - {
        app_id(app) for app, _c in LINKED.values()
    }
    assert entra_coverage.linked_app_ids(b["org"].id) == {app_id("ServiceNow")}


def _rejected(write):
    with pytest.raises(IntegrityError), transaction.atomic():
        write()
        connection.check_constraints()


def test_database_rejects_coverage_on_another_organizations_connector(a, b, cov):
    _rejected(
        lambda: AppCoverage.objects.create(
            organization=b["org"], connector=cov, app_id=APP, display_name="x", first_seen_at=NOW, seen_at=NOW
        )
    )


def test_deleting_the_connector_removes_its_coverage_and_keeps_records(a, cov, records):
    run(cov)
    discovery.delete_connector(a["org"], a["users"]["admin"], cov)
    assert not AppCoverage.objects.exists()
    assert Resource.objects.filter(id=records["Workday HR"].id).exists()
    assert not ConnectorRun.objects.filter(connector_id=cov.id).exists()


# ---- the access check ----------------------------------------------------------------------------------------------


def steps(result):
    return {s["name"]: s for s in result["steps"]}


def test_access_check_passes_and_names_each_permission(a, cov):
    result = access.check_access(a["org"], a["users"]["admin"], cov).access_check
    assert result["ok"] and result["missing"] == []
    by = steps(result)
    assert by["Read enterprise applications"]["permission"] == "Application.Read.All"
    assert by["Read provisioning jobs"]["permission"] == "Synchronization.Read.All"
    assert by["Read provisioning jobs"]["state"] == "ok"
    assert by["Read conditional access policies"]["permission"] == "Policy.Read.All"
    configure(cov, simulated_permissions=["Application.Read.All", "Policy.Read.All"])
    result = access.check_access(a["org"], a["users"]["admin"], cov).access_check
    assert not result["ok"] and result["missing"] == ["Synchronization.Read.All"]


def test_access_check_warns_of_the_missing_licence_without_failing(a, cov):
    configure(cov, simulated_no_premium=True)
    result = access.check_access(a["org"], a["users"]["admin"], cov).access_check
    step = steps(result)["Read conditional access policies"]
    assert result["ok"] and result["missing"] == []
    assert (step["state"], step["problem"], step["problem_label"]) == ("warning", "missing_licence", "Missing license")
    assert "P1" in step["detail"]


def test_access_check_skips_what_the_scope_leaves_out(a, cov):
    discovery.update_connector(
        a["org"], a["users"]["admin"], cov, {"scope": {"provisioning": False, "conditional_access": False}}
    )
    cov.refresh_from_db()
    configure(cov, simulated_permissions=["Application.Read.All"])
    result = access.check_access(a["org"], a["users"]["admin"], cov).access_check
    by = steps(result)
    assert result["ok"]
    assert by["Read provisioning jobs"]["state"] == "not_used"
    assert by["Read conditional access policies"]["state"] == "not_used"


def test_the_source_lists_its_permissions(a, api):
    sources = {s["id"]: s for s in api(a["users"]["viewer"]).get("discovery/sources").json_body}
    source = sources["entra_coverage"]
    assert source["capability"] == "discovery.entra" and source["available"]
    assert [p["name"] for p in source["permissions"]] == [
        "Application.Read.All",
        "Synchronization.Read.All",
        "Policy.Read.All",
    ]
    assert source["scope_defaults"] == {"provisioning": True, "conditional_access": True}


def test_only_applications_have_coverage(a):
    job = services.create_resource(
        a["org"], a["users"]["editor"], {"name": "Nightly job", "kind": "component", "lifecycle": "active"}
    )
    assert coverage.record_block(a["org"], job) is None


def test_a_disabled_connectors_coverage_isnt_used(a, api, cov, records):
    run(cov)
    Connector.objects.filter(pk=cov.pk).update(enabled=False)
    block = block_of(a["org"], records["Workday HR"])
    assert block["sso"] == "unknown" and block["connector"] is None and block["sso_detail"] == coverage.NO_CONNECTOR
    rows = report(api, a["users"]["viewer"], "sso=saml")["results"]
    assert rows == []


def test_report_and_detail_queries_stay_flat(a, api, cov, records, django_assert_max_num_queries):
    run(cov)
    viewer = api(a["users"]["viewer"])
    viewer.get("reports/sso-coverage")  # the session's first request saves it once
    with CaptureQueriesContext(connection) as before:
        assert viewer.get("reports/sso-coverage").status_code == 200
    for i in range(10):
        make_record(a["org"], a["users"]["editor"], f"Extra {i}", entra_app_id=app_id("Workday"), criticality="low")
        make_record(a["org"], a["users"]["editor"], f"Unlinked {i}")
    with CaptureQueriesContext(connection) as after:
        assert len(viewer.get("reports/sso-coverage").json_body["results"]) == len(LINKED) + 21
    assert len(after) == len(before)  # one query per kind of thing, not per row
    with django_assert_max_num_queries(REPORT_QUERIES):
        viewer.get("reports/sso-coverage?ordering=mfa")
    with django_assert_max_num_queries(DETAIL_QUERIES):
        assert viewer.get(f"applications/{records['Workday HR'].id}").status_code == 200


def test_access_check_names_both_causes_of_another_403(a, cov):
    configure(cov, simulated_permissions=["Application.Read.All", "Synchronization.Read.All"])
    result = access.check_access(a["org"], a["users"]["admin"], cov).access_check
    step = steps(result)["Read conditional access policies"]
    assert step["state"] == "failed" and result["missing"] == ["Policy.Read.All"]
    assert "Policy.Read.All" in step["detail"] and "P1" in step["detail"] and "P1" in step["next"]


def test_the_permissions_carry_their_role_ids():
    from discovery import graph_source

    assert graph_source.POLICY_READ_ALL.role_id == "246dd0d5-5bd0-4def-940b-0421030a5b68"
    assert graph_source.SYNCHRONIZATION_READ_ALL.role_id == "5ba43d2f-fa88-4db2-bd1c-a67c5f0fb1ce"
    assert graph_source.AUDIT_LOG_READ_ALL.role_id == "b0afded3-3588-46d8-8b3d-9842eff778da"
    assert graph_source.LICENSE_ASSIGNMENT_READ_ALL.role_id == "e2f98668-2877-4f38-a2f4-8202e0717aa1"
    assert graph_source.DIRECTORY_READ_ALL.role_id == ""  # not confirmed on Learn yet


# ---- how old a kept answer may be, and whose it is ----------------------------------------------------------------


def test_mfa_checked_at_is_when_the_policies_were_read(a, api, cov, clock, records):
    run(cov)
    assert row(cov, "Workday").mfa_checked_at == NOW
    block = block_of(a["org"], records["Workday HR"])
    assert block["mfa_checked_at"] == "2026-11-09T02:00:00Z" and block["scim_checked_at"] == "2026-11-09T02:00:00Z"
    response = api(a["users"]["viewer"]).get("reports/sso-coverage/export.csv?q=workday")
    line = next(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert line["mfa_checked_at"] == "2026-11-09T02:00:00Z" and line["scim_checked_at"] == "2026-11-09T02:00:00Z"
    # Not read (scope off): no date.
    discovery.update_connector(a["org"], a["users"]["admin"], cov, {"scope": {"conditional_access": False}})
    later_run(cov, clock)
    assert row(cov, "Workday").mfa_checked_at is None


def test_a_carried_mfa_answer_keeps_its_read_date(a, cov, clock):
    run(cov)
    configure(cov, simulated_errors={CA_PATH: 500})
    later_run(cov, clock, hours=6 * 24)
    workday = row(cov, "Workday")
    assert (workday.mfa, workday.mfa_checked_at) == ("all_users", NOW)  # six days: kept, with its age


def test_a_carried_mfa_answer_expires_after_seven_days(a, cov, clock):
    run(cov)
    configure(cov, simulated_errors={CA_PATH: 500})
    later_run(cov, clock, hours=6 * 24)  # kept
    result = later_run(cov, clock, hours=7 * 24)  # seven days after the read: too old
    workday = row(cov, "Workday")
    assert (workday.mfa, workday.mfa_checked_at, workday.policies) == ("unknown", None, [])
    assert "hasn't been read since 2026-11-09" in workday.mfa_detail and "over 7 days" in workday.mfa_detail
    assert "mfa_carried" not in result.counts
    assert row(cov, "Payroll Sync Daemon").mfa == "unknown"  # no stale "No MFA policy" either


def test_a_carried_provisioning_answer_expires_after_seven_days(a, cov, clock):
    run(cov)
    path = f"/v1.0/servicePrincipals/{entra_simulator._id('Workday')}/synchronization/jobs"
    configure(cov, simulated_errors={path: 500})
    later_run(cov, clock, hours=6 * 24)
    assert (row(cov, "Workday").scim, row(cov, "Workday").scim_checked_at) == ("on", NOW)
    result = later_run(cov, clock, hours=8 * 24)
    workday = row(cov, "Workday")
    assert (workday.scim, workday.scim_checked_at) == ("unknown", None)
    assert "hasn't been read since 2026-11-09" in workday.scim_detail
    assert "scim_carried" not in result.counts


def test_provisioning_past_the_cap_keeps_its_answer_whatever_its_age(a, cov, clock, monkeypatch):
    run(cov)
    monkeypatch.setattr(entra_coverage, "SCIM_LIMIT", 0)  # nothing read: every app waits its turn
    result = later_run(cov, clock, hours=30 * 24)
    assert not AppCoverage.objects.filter(connector=cov, scim="unknown").exists()
    assert set(AppCoverage.objects.filter(connector=cov).values_list("scim_checked_at", flat=True)) == {NOW}
    assert "scim_carried" not in result.counts  # the round-robin isn't a failure


OTHER_SP = "99999999-9999-4999-8999-999999999999"


def test_an_answer_for_another_service_principal_is_never_carried(a, cov, clock, monkeypatch):
    """A tenant change (or a re-created enterprise app) is another service principal: tenant A's answers never show
    for tenant B, after a transient failure or on the provisioning cap."""
    run(cov)
    AppCoverage.objects.filter(connector=cov).update(service_principal_id=OTHER_SP)
    sync = f"/v1.0/servicePrincipals/{entra_simulator._id('Workday')}/synchronization/jobs"
    configure(cov, simulated_throttle={"path": CA_PATH, "times": 50, "retry_after": None}, simulated_errors={sync: 500})
    result = later_run(cov, clock)
    rows = list(AppCoverage.objects.filter(connector=cov))
    assert {r.mfa for r in rows} == {"unknown"} and all(r.policies == [] for r in rows)
    assert "mfa_carried" not in result.counts and "scim_carried" not in result.counts
    workday = row(cov, "Workday")
    assert (workday.scim, workday.scim_checked_at) == ("unknown", None)
    assert workday.service_principal_id == entra_simulator._id("Workday")
    # The cap path too.
    AppCoverage.objects.filter(connector=cov).update(service_principal_id=OTHER_SP)
    configure(cov, simulated_throttle={}, simulated_errors={})
    monkeypatch.setattr(entra_coverage, "SCIM_LIMIT", 0)
    later_run(cov, clock, hours=48)
    assert set(AppCoverage.objects.filter(connector=cov).values_list("scim", flat=True)) == {"unknown"}
    assert all("Not read yet" in r.scim_detail for r in AppCoverage.objects.filter(connector=cov))
