"""The Okta and Google Workspace discovery sources (discovery/okta.py, discovery/google_workspace.py),
their connections (core/okta.py, core/google_workspace.py, core/connections.py), the shared client
(discovery/rest_source.py) and their simulators. Nothing here reaches the network: the reserved hosts select the
simulators, and the address guard is exercised with a patched resolver that never connects."""

from __future__ import annotations

import json
import logging
import socket
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from catalog import org_lifecycle
from catalog.models import Resource
from core import capabilities as caps
from core import connections, credentials, google_workspace, graph, okta, outbound
from core.models import AuditEvent, GoogleWorkspaceConnection, OktaConnection
from core.problems import Conflict, FieldErrors
from discovery import access, okta_simulator, rest_source
from discovery import engine as discovery
from discovery import google_workspace as gw_source
from discovery import google_workspace_simulator as gw_sim
from discovery import okta as okta_source
from discovery.graph_source import SourceError
from discovery.models import Connector, ConnectorRun, Observation, RunStatus, SourceDifference
from tests.conftest import drain_jobs, revoke_capability

# Okta, Google Workspace, ServiceNow and Halo are preview features (launch scope, off by default): this module
# covers them with SOFTWARDEN_PREVIEW_FEATURES on. tests/test_preview_features.py covers them off.
pytestmark = pytest.mark.usefixtures("preview_on")

ADMIN_EMAIL = f"admin@{google_workspace.SIMULATED_DOMAIN}"
SA_EMAIL = "softwarden-discovery@contoso-prj.iam.gserviceaccount.com"
SA_CLIENT = "112233445566778899001"
KEY_ID = "0123456789abcdef0123456789abcdef01234567"
NOW = 1_800_000_000.0
PEOPLE_TRACES = (
    [email for email, _ in gw_sim.PEOPLE.values()] + [p for _, p in gw_sim.PEOPLE.values()] + ["203.0.113.7"]
)


# ---- keys and helpers ------------------------------------------------------------------------------------------------


def _pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def pem(rsa_key) -> str:
    return _pem(rsa_key)


def key_file(pem: str, *, key_id: str = KEY_ID, **changes: Any) -> str:
    data = {
        "type": "service_account",
        "project_id": "contoso-prj",
        "private_key_id": key_id,
        "private_key": pem,
        "client_email": SA_EMAIL,
        "client_id": SA_CLIENT,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    return json.dumps({**data, **changes})


def okta_connection(org, actor, pem, test=True, **extra):
    data = {
        "name": f"Okta {uuid.uuid4().hex[:6]}",
        "base_url": okta.SIMULATED_URL,
        "client_id": "0oasoftwarden",
        "secret": pem,
        **extra,
    }
    conn = connections.create_connection(org, actor, connections.OKTA, data)
    if test:
        conn = connections.run_test(org, actor, conn)
    return conn


def google_connection(org, actor, pem, test=True, **extra):
    data = {
        "name": f"Workspace {uuid.uuid4().hex[:6]}",
        "admin_email": ADMIN_EMAIL,
        "secret": key_file(pem),
        **extra,
    }
    conn = connections.create_connection(org, actor, connections.GOOGLE, data)
    if test:
        conn = connections.run_test(org, actor, conn)
    return conn


def make(org, actor, source, conn, scope=None, name=None) -> Connector:
    data: dict[str, Any] = {
        "source": source,
        "name": name or f"{source} {uuid.uuid4().hex[:4]}",
        f"{connections.kind_of(conn).key}_connection": str(conn.id),
    }
    if scope is not None:
        data["scope"] = scope
    return Connector.objects.get(pk=discovery.create_connector(org, actor, data).pk)


def configure(connector, **config) -> Connector:
    connector.config = {**connector.config, **config}
    connector.save(update_fields=["config"])
    return connector


def change(connector, *changes) -> Connector:
    return configure(connector, simulator_changes=[*connector.config.get("simulator_changes", []), *changes])


def names(connector, live=True) -> list[str]:
    qs = Observation.objects.filter(connector=connector)
    if live:
        qs = qs.filter(gone_at__isnull=True, out_of_scope="")
    return sorted(qs.values_list("display_name", flat=True))


def obs(connector, name) -> Observation:
    return Observation.objects.get(connector=connector, display_name=name)


@pytest.fixture
def ent(a):
    a["org"], _ = caps.set_edition(a["org"], "enterprise")
    return a


@pytest.fixture
def ok(ent, pem):
    admin = ent["users"]["admin"]
    return make(ent["org"], admin, "okta", okta_connection(ent["org"], admin, pem))


@pytest.fixture
def gw(ent, pem):
    admin = ent["users"]["admin"]
    return make(ent["org"], admin, "google_workspace", google_connection(ent["org"], admin, pem))


@pytest.fixture
def no_sleep(monkeypatch):
    clock: dict[str, Any] = {"now": 1000.0, "slept": []}

    def sleep(seconds):
        clock["slept"].append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(graph, "sleep", sleep)
    monkeypatch.setattr(graph, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(rest_source, "wall", lambda: NOW)
    return clock


# ---- keys, assertions, addresses (core/okta.py, core/google_workspace.py) ---------------------------------------------


def test_okta_signs_a_client_assertion_with_rsa_or_ec(rsa_key, pem):
    key = okta.load_key(pem)
    url = okta.token_url(okta.SIMULATED_URL)
    signed = okta.client_assertion("0oasoftwarden", url, key, "kid-1")
    claims = jwt.decode(signed, rsa_key.public_key(), algorithms=["RS256"], audience=url)
    assert claims["iss"] == claims["sub"] == "0oasoftwarden" and claims["exp"] - claims["iat"] == 300
    assert jwt.get_unverified_header(signed)["kid"] == "kid-1"
    ec_key = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(ECAlgorithm.to_jwk(ec_key)) | {"kid": "ec-1"}
    loaded = okta.load_key(json.dumps(jwk))
    assert loaded.algorithm == "ES256" and loaded.jwk_kid == "ec-1"
    signed = okta.client_assertion("0oasoftwarden", url, loaded)
    assert jwt.decode(signed, ec_key.public_key(), algorithms=["ES256"], audience=url)["sub"] == "0oasoftwarden"
    assert jwt.get_unverified_header(signed)["kid"] == "ec-1"


def test_okta_refuses_keys_it_cannot_use_without_quoting_them(rsa_key, pem):
    public_jwk = RSAAlgorithm.to_jwk(rsa_key.public_key())
    small = _pem(rsa.generate_private_key(public_exponent=65537, key_size=1024))  # noqa: S505 (refused, as it should be)
    for value in ("", "not a key", public_jwk, small, "{not json", pem.replace("PRIVATE", "PUBLIC")):
        with pytest.raises(okta.KeyProblem) as e:
            okta.load_key(value)
        assert "MII" not in str(e.value) and (not value or value not in str(e.value))


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://contoso.okta.com", True),
        ("https://contoso.okta.com/", True),
        ("https://contoso.oktapreview.com", True),
        ("https://contoso.okta-emea.com", True),
        ("https://sso.contoso.example", True),  # a custom domain
        ("https://okta.simulated.invalid", True),
        ("https://contoso.okta.com.", True),  # one trailing dot names the same host
        ("https://contoso-admin.okta.com.", False),
        ("https://okta.com.", False),
        ("http://contoso.okta.com", False),
        ("https://contoso-admin.okta.com", False),  # the Admin Console host, not the org
        ("https://a.b.okta.com", False),
        ("https://okta.com", False),
        ("https://contoso.okta.com/oauth2/default", False),
        ("https://contoso.okta.com:8443", False),
        ("https://contoso.okta.com?x=1", False),
        ("https://user:pass@contoso.okta.com", False),
        ("https://localhost", False),
        ("https://okta.internal", False),
        ("https://127.0.0.1", False),
        ("https://8.8.8.8", False),  # an IP address, even a public one
        ("https://10.0.0.5", False),
    ],
)
def test_the_okta_address_is_https_on_an_okta_or_custom_public_host(url, ok):
    if ok:
        assert okta.check_domain(url).startswith("https://")
    else:
        with pytest.raises(outbound.UnsafeDestination):
            okta.check_domain(url)


def test_okta_scopes_are_read_only():
    assert okta.check_scope("") == "okta.apps.read"
    assert okta.check_scope("okta.users.read okta.apps.read") == "okta.apps.read okta.users.read"
    for bad in ("okta.apps.manage", "okta.users.read", "okta.apps.read okta.groups.manage"):
        with pytest.raises(ValueError):
            okta.check_scope(bad)


def test_google_reads_only_the_key_and_its_id_from_the_key_file(rsa_key, pem):
    key = google_workspace.parse_key_file(key_file(pem))
    assert (key.client_email, key.client_id, key.private_key_id) == (SA_EMAIL, SA_CLIENT, KEY_ID)
    assert set(json.loads(key.secret())) == {"private_key", "private_key_id"}
    key_id, loaded = google_workspace.stored_key(key.secret())
    signed = google_workspace.assertion(SA_EMAIL, key_id, loaded, ADMIN_EMAIL, google_workspace.REPORTS_SCOPE)
    claims = jwt.decode(signed, rsa_key.public_key(), algorithms=["RS256"], audience=google_workspace.TOKEN_URL)
    assert claims["iss"] == SA_EMAIL and claims["sub"] == ADMIN_EMAIL
    assert claims["scope"] == google_workspace.REPORTS_SCOPE and jwt.get_unverified_header(signed)["kid"] == KEY_ID
    for bad in (
        "",
        "{",
        json.dumps({"type": "authorized_user"}),
        key_file(pem, client_email="someone@contoso.example"),
        key_file(pem, client_id="abc"),
        key_file(pem, key_id="nothex!"),
        key_file("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"),
    ):
        with pytest.raises(google_workspace.KeyProblem) as e:
            google_workspace.parse_key_file(bad)
        assert "BEGIN" not in str(e.value)


def test_link_headers_and_repeated_headers_are_read():
    header = (
        '<https://x.okta.com/api/v1/apps?limit=2>; rel="self", <https://x.okta.com/api/v1/apps?after=2>; rel="next"'
    )
    assert rest_source.next_link(header) == "https://x.okta.com/api/v1/apps?after=2"
    assert rest_source.next_link('<https://x.okta.com/api/v1/apps>; rel="self"') == ""
    pairs = [("Link", '<a>; rel="self"'), ("link", '<b>; rel="next"'), ("X-A", "1"), ("x-a", "2")]
    merged = outbound.merged_headers(pairs)
    assert merged == {"link": '<a>; rel="self", <b>; rel="next"', "x-a": "2"}  # only list-valued headers are joined
    assert rest_source.next_link(merged["link"]) == "b"


def test_simulators_are_chosen_by_the_reserved_hosts_only(ok, gw):
    ok_credential = connections.credential(discovery.connection_of(ok))
    gw_credential = connections.credential(discovery.connection_of(gw))
    assert isinstance(okta_source.transport_for(ok, ok_credential), okta_simulator.Transport)
    assert isinstance(gw_source.transport_for(gw, gw_credential), gw_sim.Transport)
    real_okta = connections.OktaCredential("https://contoso.okta.com", "c", "", "okta.apps.read", "k")
    real_google = connections.GoogleCredential("admin@contoso.example", SA_EMAIL, SA_CLIENT, "k")
    assert isinstance(okta_source.transport_for(ok, real_okta), outbound.PublicTransport)
    assert isinstance(gw_source.transport_for(gw, real_google), outbound.PublicTransport)
    assert isinstance(okta.transport_for(okta.SIMULATED_URL), okta.SimulatedTokens)
    assert isinstance(okta.transport_for("https://contoso.okta.com"), outbound.PublicTransport)
    assert isinstance(google_workspace.transport_for(ADMIN_EMAIL), google_workspace.SimulatedTokens)
    assert isinstance(google_workspace.transport_for("admin@contoso.example"), outbound.PublicTransport)


# ---- connections ----------------------------------------------------------------------------------------------------


def test_okta_connection_test_and_its_failures(ent, pem):
    org, admin = ent["org"], ent["users"]["admin"]
    good = okta_connection(org, admin, pem)
    assert good.test_current and good.probe.ok and good.scope == "okta.apps.read"
    for client_id, problem, code in (
        ("wrong-client", "wrong_credentials", "invalid_client"),
        ("dpop-client", "error", "invalid_dpop_proof"),
    ):
        conn = okta_connection(org, admin, pem, client_id=client_id)
        assert not conn.test_current and conn.probe.problem == problem and conn.probe.code == code
        assert pem.splitlines()[1] not in json.dumps(conn.test_result)
    with pytest.raises(FieldErrors) as e:
        okta_connection(org, admin, pem, scope="okta.apps.manage")
    assert set(e.value.errors) == {"scope"}
    with pytest.raises(FieldErrors) as e:
        okta_connection(org, admin, "not a key", base_url="https://contoso-admin.okta.com")
    assert set(e.value.errors) == {"secret", "base_url"} and "not a key" not in json.dumps(e.value.errors)


def test_google_connection_test_and_its_failures(ent, pem):
    org, admin = ent["org"], ent["users"]["admin"]
    good = google_connection(org, admin, pem)
    assert good.test_current and good.service_account_email == SA_EMAIL and good.service_account_client_id == SA_CLIENT
    assert set(json.loads(credentials.slot_value(good, "secret"))) == {"private_key", "private_key_id"}
    for subject, problem, code in (
        (f"not-delegated@{google_workspace.SIMULATED_DOMAIN}", "missing_permission", "unauthorized_client"),
        (f"nobody@{google_workspace.SIMULATED_DOMAIN}", "wrong_credentials", "invalid_grant"),
    ):
        conn = google_connection(org, admin, pem, admin_email=subject)
        assert not conn.test_current and conn.probe.problem == problem and conn.probe.code == code
    advice = google_connection(org, admin, pem, admin_email=f"not-delegated@{google_workspace.SIMULATED_DOMAIN}")
    assert SA_CLIENT in advice.probe.next and google_workspace.REPORTS_SCOPE in advice.probe.next
    deleted = connections.create_connection(
        org,
        admin,
        connections.GOOGLE,
        {"name": "Deleted key", "admin_email": ADMIN_EMAIL, "secret": key_file(pem, key_id="f" * 40)},
    )
    assert connections.run_test(org, admin, deleted).probe.code == "invalid_grant"
    with pytest.raises(FieldErrors) as e:
        google_connection(org, admin, pem, admin_email="not an email", secret='{"type": "service_account"}')
    assert set(e.value.errors) == {"admin_email", "secret"}


def test_a_changed_key_needs_a_new_test_before_a_run(ent, ok, gw, rsa_key):
    org, admin = ent["org"], ent["users"]["admin"]
    other = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    for connector, secret in ((ok, other), (gw, key_file(other))):
        conn = discovery.connection_of(connector)
        before = conn.revision
        updated = connections.update_connection(org, admin, conn, {"secret": secret})
        assert updated.revision == before + 1 and not updated.test_current
        with pytest.raises(Conflict):
            discovery.request_run(org, admin, connector)
        event = AuditEvent.objects.filter(organization=org, action__endswith="_connection.updated").latest("created_at")
        assert event.changes["replaced"] == ["secret"]


def test_a_custom_okta_domain_that_resolves_to_loopback_is_refused_before_connecting(ent, pem, monkeypatch):
    org, admin = ent["org"], ent["users"]["admin"]
    loopback = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    monkeypatch.setattr(outbound.socket, "getaddrinfo", lambda *a, **k: loopback)

    def never(*args, **kwargs):
        raise AssertionError("a refused address was connected to")

    monkeypatch.setattr(outbound.socket, "create_connection", never)
    conn = okta_connection(org, admin, pem, base_url="https://sso.contoso.example")
    assert conn.probe.problem == "refused" and "loopback" in conn.probe.detail
    connector = make(org, admin, "okta", conn)
    with pytest.raises(SourceError) as e:
        okta_source.fetch(connector, connections.credential_for(conn, pem))
    assert "loopback" in str(e.value)
    result = access.evaluate(connector)
    sign_in = next(s for s in result["steps"] if s["name"] == "Sign in to Okta")
    assert sign_in["state"] == "failed" and sign_in["problem"] == "refused"
    monkeypatch.setattr(outbound.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("169.254.169.254", 443))])
    with pytest.raises(graph.HostRefused):
        outbound.PublicTransport().send("GET", "https://sso.contoso.example/api/v1/apps", {})


# ---- Okta: listing, filters, paging, rate limits ----------------------------------------------------------------------


def test_okta_lists_apps_with_sign_on_mode_and_assigned_users(ok):
    run = discovery.run(ok)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert names(ok) == ["Atlassian Cloud", "Expenses Portal", "Salesforce", "Slack", "Workday", "Zoom"]
    assert run.counts["skipped"] == {"inactive": 1, "okta_app": 2}
    salesforce = obs(ok, "Salesforce").data
    assert salesforce["okta_app_id"] == "0oa1salesforce" and salesforce["sign_on_mode"] == "SAML_2_0"
    assert salesforce["assigned_users"] == 42 and not salesforce["assigned_users_capped"]
    assert salesforce["provisioning"] is True and obs(ok, "Zoom").data["provisioning"] is False
    second = discovery.run(ok)
    assert second.counts["new"] == 0 and second.counts["unchanged"] == 6


def test_okta_filters_are_scope_options(ent, pem):
    org, admin = ent["org"], ent["users"]["admin"]
    scope = {
        "exclude_inactive": False,
        "exclude_okta_apps": False,
        "exclude_bookmarks": True,
        "assignment_counts": False,
    }
    connector = make(org, admin, "okta", okta_connection(org, admin, pem), scope)
    run = discovery.run(connector)
    assert run.counts["skipped"] == {"bookmark": 1}
    assert "Okta Admin Console" in names(connector) and "Legacy Wiki" in names(connector)
    assert obs(connector, "Slack").data["assigned_users"] is None
    with pytest.raises(FieldErrors) as e:
        discovery.update_connector(org, admin, connector, {"scope": {"exclude_inactive": "yes"}})
    assert "scope.exclude_inactive" in e.value.errors


def test_okta_pages_through_link_headers_for_apps_and_users(ok, monkeypatch):
    configure(ok, simulated_page=3, simulated_size=7, simulated_users={"Workday": 1200})
    transport = okta_simulator.Transport(ok)
    run = discovery.run(ok, transport=transport)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert len(names(ok)) == 13
    app_pages = [u for m, u in transport.calls if m == "GET" and u.split("?")[0].endswith("/api/v1/apps")]
    assert len(app_pages) == 6 and all("after=" in u for u in app_pages[1:])
    assert obs(ok, "Workday").data["assigned_users"] == 1200
    workday_pages = [u for _, u in transport.calls if "/0oa9workday/users" in u]
    assert len(workday_pages) == 3
    monkeypatch.setattr(okta_source, "USER_PAGES", 2)
    discovery.run(ok)
    workday = obs(ok, "Workday").data
    assert workday["assigned_users"] == 1000 and workday["assigned_users_capped"] is True


@pytest.mark.parametrize(("left", "slept"), [(9, [60.0] * 7), (10, [])])
def test_okta_paces_once_less_than_a_tenth_of_the_org_limit_remains(ok, no_sleep, left, slept):
    """The rate limit is the org's, shared with its other integrations: below 10 % of X-Rate-Limit-Limit (100 here),
    each next read waits for X-Rate-Limit-Reset (60 s away) instead of spending the rest."""
    configure(ok, simulated_rate_left=left)
    assert discovery.run(ok).status == RunStatus.SUCCEEDED and no_sleep["slept"] == slept


def test_okta_counting_stops_before_the_deadline_and_keeps_the_listing(ok, no_sleep, monkeypatch):
    counted: list[str] = []
    real = okta_source.count_users

    def slow_count(api, base_url, app_id):
        counted.append(app_id)
        no_sleep["now"] += 36 * 60  # the first count takes the run to 4 minutes before its 40-minute limit
        return real(api, base_url, app_id)

    monkeypatch.setattr(okta_source, "count_users", slow_count)
    run = discovery.run(ok)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert len(names(ok)) == 6 and len(counted) == 1
    assert any(n.startswith("Counting assigned users stopped after 1 application(s)") for n in run.counts["notes"])
    counts = [o.data["assigned_users"] for o in Observation.objects.filter(connector=ok)]
    assert counts.count(None) == 5


def test_a_run_never_keeps_the_key_or_its_lines(ok, gw, pem):
    stored = connections.credential(discovery.connection_of(gw))
    lines = [line for line in pem.splitlines() if not line.startswith("-----")]
    values = connections.redact_values(stored)
    assert stored.secret in values and pem in values and all(line in values for line in lines)
    escaped = json.dumps({"private_key": pem})  # a message could quote the key JSON-escaped, on one line
    redacted = discovery._redact(f"refused {escaped}", stored)
    assert all(line not in redacted for line in lines)
    okta_credential = connections.credential(discovery.connection_of(ok))
    assert all(line not in discovery._redact(" ".join(lines), okta_credential) for line in lines)


def test_okta_waits_for_the_rate_limit_window_when_none_remains(ok, no_sleep):
    configure(ok, simulated_rate_remaining=1)  # every answer says 0 remaining, resetting in 30 seconds
    run = discovery.run(ok)
    assert run.status == RunStatus.SUCCEEDED, run.error
    # One wait before each read after the first: the listing, then 6 apps counted, Workday (640 users) on 2 pages.
    assert no_sleep["slept"] == [30.0] * 7


@pytest.mark.parametrize(("retry_after", "slept"), [(None, [7.0, 7.0]), ("3", [3.0, 3.0])])
def test_okta_429_waits_for_retry_after_or_the_rate_limit_reset(ok, no_sleep, retry_after, slept):
    configure(ok, simulated_throttle={"path": "/api/v1/apps", "times": 2, "retry_after": retry_after})
    assert discovery.run(ok).status == RunStatus.SUCCEEDED and no_sleep["slept"] == slept


def test_okta_a_wait_past_the_limit_stops_the_run(ok, no_sleep):
    configure(ok, simulated_throttle={"path": "/api/v1/apps", "times": 1, "retry_after": "600"})
    run = discovery.run(ok)
    assert run.status == RunStatus.FAILED and "asked to wait 600 seconds" in run.error and no_sleep["slept"] == []


def test_okta_a_next_link_to_another_host_is_refused(ok):
    configure(ok, simulated_page=2, simulated_foreign_next=True)
    transport = okta_simulator.Transport(ok)
    run = discovery.run(ok, transport=transport)
    assert run.status == RunStatus.FAILED and "unexpected host" in run.error
    assert {host for host, _ in transport.sent} == {okta.SIMULATED_HOST}


def test_okta_the_same_page_twice_stops_the_run(ok):
    configure(ok, simulated_page=2, simulated_repeat_next=True)
    run = discovery.run(ok)
    assert run.status == RunStatus.FAILED and "same page again" in run.error


def test_okta_rename_is_changed_delete_is_gone_and_inactive_is_out_of_scope(ok):
    discovery.run(ok)
    change(ok, {"op": "rename", "id": "Slack", "value": "Slack Enterprise Grid"})
    change(ok, {"op": "remove", "id": "Zoom"}, {"op": "inactive", "id": "Workday", "value": True})
    run = discovery.run(ok)
    assert run.counts["changed"] == 1 and run.counts["gone"] == 1 and run.counts["out_of_scope"] == 1
    assert obs(ok, "Slack Enterprise Grid").external_id == "0oa2slack"
    assert obs(ok, "Zoom").gone_at is not None
    workday = obs(ok, "Workday")
    assert workday.gone_at is None and workday.out_of_scope == "inactive"


def test_okta_a_refused_user_count_is_a_note_not_a_failure(ok):
    configure(ok, simulated_users_denied=True)
    run = discovery.run(ok)
    assert run.status == RunStatus.SUCCEEDED
    assert any("weren't counted (403 E0000006)" in n for n in run.counts["notes"])
    assert obs(ok, "Salesforce").data["assigned_users"] is None


def test_okta_a_refused_listing_names_the_admin_role(ok):
    configure(ok, simulated_denied=True)
    run = discovery.run(ok)
    assert run.status == RunStatus.FAILED and "403 E0000006" in run.error and "Read-only Administrator" in run.error


def test_okta_access_check(ent, ok):
    org, admin = ent["org"], ent["users"]["admin"]
    result = access.evaluate(ok)
    assert result["ok"] and [s["name"] for s in result["steps"]] == [
        "Connection",
        "Sign in to Okta",
        "Read applications",
        "Read application users",
    ]
    configure(ok, simulated_users_denied=True)
    result = access.evaluate(ok)
    assert not result["ok"] and result["missing"] == ["Read-only Administrator"]
    configure(ok, simulated_users_denied=False, simulated_denied=True)
    steps = {s["name"]: s for s in access.evaluate(ok)["steps"]}
    assert steps["Read applications"]["problem"] == "missing_permission"
    assert steps["Read application users"]["state"] == "skipped"
    configure(ok, simulated_denied=False)
    connector = discovery.update_connector(org, admin, ok, {"scope": {"assignment_counts": False}})
    steps = {s["name"]: s for s in access.evaluate(connector)["steps"]}
    assert steps["Read application users"]["state"] == "not_used"
    connections.update_connection(org, admin, ok.okta_connection, {"client_id": "wrong-client"})
    steps = {s["name"]: s for s in access.evaluate(Connector.objects.get(pk=ok.pk))["steps"]}
    assert (
        steps["Sign in to Okta"]["problem"] == "wrong_credentials" and steps["Read applications"]["state"] == "skipped"
    )


# ---- Google Workspace ---------------------------------------------------------------------------------------------------


def test_google_lists_authorized_apps_as_aggregates(gw):
    run = discovery.run(gw)
    assert run.status == RunStatus.SUCCEEDED, run.error
    assert names(gw) == ["Calendly", "Dropbox for Gmail", "Grammarly", "Notion", "Zapier"]
    notion = obs(gw, "Notion")
    assert notion.external_id == "1001-notion.apps.googleusercontent.com"
    assert notion.data["users"] == 3 and notion.data["grants"] == 4 and notion.data["days"] == 30
    assert notion.data["client_type"] == "WEB" and not notion.data["risky"]
    zapier = obs(gw, "Zapier").data
    assert zapier["risky"] and zapier["risky_scopes"] == [gw_sim.DRIVE, gw_sim.GMAIL_MODIFY]
    assert set(notion.data) == {
        "client_id",
        "client_type",
        "users",
        "grants",
        "last_authorized_at",
        "days",
        "scopes",
        "risky_scopes",
        "risky",
        "description",
        "enterprise_app",
    }


def test_google_window_and_minimum_people_are_scope_options(ent, gw):
    org, admin = ent["org"], ent["users"]["admin"]
    connector = discovery.update_connector(org, admin, gw, {"scope": {"days": 60, "min_users": 2}})
    run = discovery.run(connector)
    assert names(connector) == ["Calendly", "Notion", "Zapier"] and run.counts["skipped"] == {"few_users": 3}
    for bad in ({"days": 0}, {"days": 181}, {"days": True}, {"min_users": 0}):
        with pytest.raises(FieldErrors):
            discovery.update_connector(org, admin, connector, {"scope": bad})


def test_google_pages_through_next_page_tokens(gw):
    configure(gw, simulated_page=2, simulated_size=3)
    transport = gw_sim.Transport(gw)
    run = discovery.run(gw, transport=transport)
    assert run.status == RunStatus.SUCCEEDED and len(names(gw)) == 8
    pages = [u for m, u in transport.calls if m == "GET"]
    assert len(pages) == 7 and all("pageToken=" in u for u in pages[1:])
    assert obs(gw, "Notion").data["users"] == 3


@pytest.mark.parametrize(
    ("throttle", "slept"),
    [({"times": 2}, [5.0, 10.0]), ({"times": 1, "status": 429, "retry_after": "4"}, [4.0])],
)
def test_google_quota_errors_back_off_or_honour_retry_after(gw, no_sleep, throttle, slept):
    configure(gw, simulated_throttle={"path": gw_sim.PATH, **throttle})
    assert discovery.run(gw).status == RunStatus.SUCCEEDED and no_sleep["slept"] == slept


def test_google_an_app_nobody_authorized_lately_is_out_of_scope_not_gone(gw):
    discovery.run(gw)
    change(gw, {"op": "remove", "id": "Grammarly"})
    run = discovery.run(gw)
    grammarly = obs(gw, "Grammarly")
    assert run.counts["gone"] == 0 and grammarly.gone_at is None and grammarly.out_of_scope == "not_recent"
    configure(gw, simulator_changes=[])
    discovery.run(gw)
    assert obs(gw, "Grammarly").out_of_scope == ""


def test_google_usage_is_no_change_but_a_new_high_risk_scope_brings_a_dismissal_back(ent, gw):
    org, editor = ent["org"], ent["users"]["editor"]
    discovery.run(gw)
    notion = obs(gw, "Notion")
    discovery.dismiss(org, editor, notion, {"reason": "Approved for marketing"})
    change(gw, {"op": "grant", "id": "Notion", "value": "dee"})
    run = discovery.run(gw)
    assert run.counts["changed"] == 0 and obs(gw, "Notion").data["users"] == 4
    assert discovery.status_of(obs(gw, "Notion")) == "dismissed"
    change(gw, {"op": "scope", "id": "Notion", "value": "https://www.googleapis.com/auth/gmail.readonly"})
    discovery.run(gw)
    assert discovery.status_of(obs(gw, "Notion")) == "changed"


def test_google_access_check(ent, gw):
    org, admin = ent["org"], ent["users"]["admin"]
    result = access.evaluate(gw)
    assert result["ok"] and [s["name"] for s in result["steps"]] == [
        "Connection",
        "Sign in to Google Workspace",
        "Read the OAuth token audit log",
    ]
    configure(gw, simulated_denied=True)
    result = access.evaluate(gw)
    assert not result["ok"] and result["missing"] == ["Reports admin privilege"]
    run = discovery.run(gw)
    assert run.status == RunStatus.FAILED and "403 PERMISSION_DENIED" in run.error
    configure(gw, simulated_denied=False)
    subject = f"not-delegated@{google_workspace.SIMULATED_DOMAIN}"
    connections.update_connection(org, admin, gw.google_workspace_connection, {"admin_email": subject})
    result = access.evaluate(Connector.objects.get(pk=gw.pk))
    assert result["missing"] == [google_workspace.REPORTS_SCOPE]


# ---- the empty-listing guard ------------------------------------------------------------------------------------------


def test_an_empty_okta_listing_is_refused_while_live_observations_exist(ok):
    connector = ok
    assert discovery.run(connector).status == RunStatus.SUCCEEDED
    before = list(Observation.objects.filter(connector=connector).values_list("id", "gone_at", "out_of_scope"))
    configure(connector, simulated_empty=True)
    run = discovery.run(connector)
    assert run.status == RunStatus.FAILED and "came back empty" in run.error and "Nothing was changed" in run.error
    after = list(Observation.objects.filter(connector=connector).values_list("id", "gone_at", "out_of_scope"))
    assert sorted(after) == sorted(before)
    assert 'run it once with "accept empty listing"' in run.error
    # An Admin accepts the empty listing for one run: it succeeds and the apps go.
    accepted = discovery.run(connector, accept_empty_listing=True)
    assert accepted.status == RunStatus.SUCCEEDED and accepted.counts["gone"] == len(before)


def test_an_empty_google_window_is_a_note_and_changes_nothing(gw):
    """`authorize` fires only on consent: a quiet window in a small tenant is normal, not a sign of a broken read."""
    assert discovery.run(gw).status == RunStatus.SUCCEEDED
    fields = ("id", "gone_at", "out_of_scope", "out_of_scope_at", "last_changed_at", "data")
    before = list(Observation.objects.filter(connector=gw).order_by("id").values_list(*fields))
    configure(gw, simulated_empty=True)
    run = discovery.run(gw)
    assert run.status == RunStatus.SUCCEEDED and run.counts["notes"] == [
        "No authorization in the last 30 days: nothing was changed."
    ]
    assert run.counts["gone"] == 0 and run.counts["not_read"] == 5 and "out_of_scope" not in run.counts
    assert list(Observation.objects.filter(connector=gw).order_by("id").values_list(*fields)) == before


@pytest.mark.parametrize("fixture", ["ok", "gw"])
def test_an_empty_listing_without_observations_is_an_empty_run(request, fixture):
    connector = configure(request.getfixturevalue(fixture), simulated_empty=True)
    run = discovery.run(connector)
    assert run.status == RunStatus.SUCCEEDED and run.counts["seen"] == 0


# ---- candidates, curated data, the API -------------------------------------------------------------------------------


def test_candidates_reach_the_queue_and_curated_data_is_never_overwritten(ent, api, ok, gw):
    org, editor = ent["org"], ent["users"]["editor"]
    discovery.run(ok)
    discovery.run(gw)
    page = api(ent["users"]["viewer"]).get(f"discovery/candidates?connector={ok.id}").json_body
    salesforce = next(r for r in page["results"] if r["name"] == "Salesforce")
    assert salesforce["source"] == "okta" and salesforce["status"] == "new"
    assert salesforce["details"]["assigned_users"] == 42 and salesforce["details"]["okta_app_id"] == "0oa1salesforce"
    page = api(ent["users"]["viewer"]).get(f"discovery/candidates?connector={gw.id}").json_body
    zapier = next(r for r in page["results"] if r["name"] == "Zapier")
    assert zapier["source"] == "google_workspace" and zapier["details"]["risky"] and zapier["details"]["users"] == 2

    _, record = discovery.create_record(org, editor, obs(ok, "Salesforce"), {})
    linked = Resource.objects.create(organization=org, kind="application", name="Zapier (approved)")
    discovery.link(org, editor, obs(gw, "Zapier"), {"resource": str(linked.id)})
    change(ok, {"op": "rename", "id": "Salesforce", "value": "Salesforce CRM"})
    change(gw, {"op": "rename", "id": "Zapier", "value": "Zapier Automation"})
    discovery.run(ok)
    discovery.run(gw)
    record.refresh_from_db()
    linked.refresh_from_db()
    assert record.name == "Salesforce" and linked.name == "Zapier (approved)"
    differs = set(SourceDifference.objects.filter(organization=org).values_list("field", "source_value"))
    assert ("name", "Salesforce CRM") in differs and ("name", "Zapier Automation") in differs
    change(ok, {"op": "remove", "id": "Salesforce CRM"})
    discovery.run(ok)
    record.refresh_from_db()
    assert record.archived_at is None and obs(ok, "Salesforce CRM").gone_at is not None


def test_the_api_creates_both_connections_and_connectors(ent, api, pem):
    admin = api(ent["users"]["admin"])
    made = admin.post(
        "connections/okta",
        {"name": "Okta", "base_url": "https://contoso.okta.com/", "client_id": "0oa1", "secret": pem},
    )
    assert made.status_code == 201, made.json_body
    body = made.json_body
    assert (
        body["base_url"] == "https://contoso.okta.com" and body["scope"] == "okta.apps.read" and not body["simulated"]
    )
    assert "secret" not in body and body["secret_set_on"]
    google = admin.post(
        "connections/google_workspace", {"name": "Workspace", "admin_email": ADMIN_EMAIL, "secret": key_file(pem)}
    )
    assert google.status_code == 201, google.json_body
    assert google.json_body["client_id"] == SA_CLIENT and google.json_body["service_account_email"] == SA_EMAIL
    assert google.json_body["simulated"]
    tested = admin.post(f"connections/google_workspace/{google.json_body['id']}/test")
    assert tested.status_code == 200 and tested.json_body["result"]["ok"] and tested.json_body["test_current"]
    connector = admin.post(
        "connectors", {"source": "google_workspace", "google_workspace_connection": google.json_body["id"]}
    )
    assert connector.status_code == 201, connector.json_body
    assert connector.json_body["connection"]["kind"] == "google_workspace" and connector.json_body["simulated"]
    assert connector.json_body["scope"] == {"days": 30, "min_users": 1, "name_exclusions": []}
    assert [p["kind"] for p in connector.json_body["permissions"]] == ["google_scope", "google_admin_role"]
    wrong = admin.post("connectors", {"source": "okta", "google_workspace_connection": google.json_body["id"]})
    assert wrong.status_code == 400
    sources = {s["id"]: s for s in api(ent["users"]["viewer"]).get("discovery/sources").json_body}
    assert sources["okta"]["connection_kind"] == "okta" and sources["okta"]["capability"] == "discovery.okta"
    assert sources["google_workspace"]["connection_kind"] == "google_workspace"
    assert sources["google_workspace"]["capability"] == "discovery.google" and sources["okta"]["available"]


def test_roles_and_capabilities(a, api, pem):
    # Every edition has both (with preview features on); a connector made first, then both revoked for the organization.
    admin = a["users"]["admin"]
    connector = make(a["org"], admin, "okta", okta_connection(a["org"], admin, pem))
    for name in ("discovery.okta", "discovery.google"):
        revoke_capability(a["org"], name)
    for role in ("viewer", "editor"):
        for path in ("connections/okta", "connections/google_workspace"):
            response = api(a["users"][role]).get(path)
            assert response.status_code == 403 and "capability" not in response.json_body
    for path, capability in (
        ("connections/okta", "discovery.okta"),
        ("connections/google_workspace", "discovery.google"),
    ):
        response = api(a["users"]["admin"]).get(path)
        assert response.status_code == 403 and response.json_body["capability"] == capability
    for source, capability in (("okta", "discovery.okta"), ("google_workspace", "discovery.google")):
        response = api(a["users"]["admin"]).post("connectors", {"source": source})
        assert response.status_code == 403 and response.json_body["capability"] == capability
    run = discovery.run(Connector.objects.select_related("organization").get(pk=connector.pk))
    assert run.status == RunStatus.FAILED and "discovery.okta" in run.error


def test_another_organization_cannot_use_or_see_them(ent, b, api, ok, gw):
    other, _ = caps.set_edition(b["org"], "enterprise")
    for source, key, connector in (
        ("okta", "okta_connection", ok),
        ("google_workspace", "google_workspace_connection", gw),
    ):
        with pytest.raises(FieldErrors) as e:
            discovery.create_connector(
                other, b["users"]["admin"], {"source": source, key: str(getattr(connector, f"{key}_id"))}
            )
        assert key in e.value.errors
        kind = connections.KINDS[source]
        path = f"connections/{kind.key}/{getattr(connector, f'{key}_id')}"
        their_admin = api(b["users"]["admin"])
        assert their_admin.get(path).status_code == 404 and their_admin.post(f"{path}/test").status_code == 404
        assert their_admin.get(f"connectors/{connector.id}").status_code == 404
    assert api(b["users"]["admin"]).get("connections/okta").json_body == []


# ---- secrets and people never leave ---------------------------------------------------------------------------------


def test_keys_tokens_and_people_never_reach_responses_audit_logs_runs_or_exports(ent, api, pem, caplog):
    from tests.test_org_lifecycle import export_now, zip_of

    caplog.set_level(logging.DEBUG)
    org = ent["org"]
    admin = api(ent["users"]["admin"])
    seen: list[str] = []

    def keep(response):
        seen.append(
            response.content.decode() if not response.streaming else b"".join(response.streaming_content).decode()
        )
        seen.append(json.dumps(dict(response.items())))
        return response

    file = key_file(pem)
    o = keep(
        admin.post(
            "connections/okta", {"name": "Okta", "base_url": okta.SIMULATED_URL, "client_id": "0oa1", "secret": pem}
        )
    )
    g = keep(
        admin.post("connections/google_workspace", {"name": "Workspace", "admin_email": ADMIN_EMAIL, "secret": file})
    )
    o_url, g_url = f"connections/okta/{o.json_body['id']}", f"connections/google_workspace/{g.json_body['id']}"
    keep(admin.post(f"{o_url}/test"))
    keep(admin.post(f"{g_url}/test"))
    oc = keep(admin.post("connectors", {"source": "okta", "okta_connection": o.json_body["id"]}))
    gc = keep(
        admin.post("connectors", {"source": "google_workspace", "google_workspace_connection": g.json_body["id"]})
    )
    for c in (oc, gc):
        keep(admin.post(f"connectors/{c.json_body['id']}/run"))
        keep(admin.post(f"connectors/{c.json_body['id']}/access-check"))
    drain_jobs()
    assert set(ConnectorRun.objects.values_list("status", flat=True)) == {RunStatus.SUCCEEDED}
    # Failing runs whose services echo the bearer token or the client assertion.
    for c, path in ((oc, "/api/v1/apps"), (gc, gw_sim.PATH)):
        connector = configure(Connector.objects.get(pk=c.json_body["id"]), simulated_errors={path: 400})
        assert discovery.run(connector).status == RunStatus.FAILED
        configure(connector, simulated_errors={}, simulated_token_echo=True)
        assert discovery.run(connector).status == RunStatus.FAILED
        configure(connector, simulated_token_echo=False)
    keep(admin.patch(o_url, {"secret": pem}))
    keep(admin.patch(g_url, {"secret": file}))
    for url in (
        "connections/okta",
        "connections/google_workspace",
        o_url,
        g_url,
        "connectors",
        "discovery/candidates?status=all",
    ):
        keep(admin.get(url))
    for c in (oc, gc):
        keep(admin.get(f"connectors/{c.json_body['id']}/runs"))
    keep(admin.client.get("/api/v1/audit-events/export.csv"))
    keep(admin.client.get("/api/v1/audit-events/export.jsonl"))
    export = export_now(api, ent["users"]["admin"])
    archive = zip_of(api(ent["users"]["admin"]).client.get(export["download_url"]))
    exported = b"".join(archive.read(n) for n in archive.namelist()).decode("utf-8", "replace")
    assert "data/core.OktaConnection.jsonl" in archive.namelist()
    assert "data/core.GoogleWorkspaceConnection.jsonl" in archive.namelist()
    row = json.loads(archive.read("data/core.GoogleWorkspaceConnection.jsonl").splitlines()[0])
    assert row["service_account_client_id"] == SA_CLIENT and not {k for k in row if k.startswith("secret_")} - {
        "secret_set_at"
    }

    events = list(AuditEvent.objects.filter(organization=org))
    assert {e.action for e in events} >= {
        "okta_connection.created",
        "okta_connection.tested",
        "okta_connection.updated",
        "google_workspace_connection.created",
        "google_workspace_connection.tested",
        "google_workspace_connection.updated",
    }
    ciphertexts = {
        OktaConnection.objects.get(pk=o.json_body["id"]).secret_ciphertext,
        GoogleWorkspaceConnection.objects.get(pk=g.json_body["id"]).secret_ciphertext,
    }
    stored = {
        "audit": json.dumps([[e.changes, e.prechange, e.postchange] for e in events], default=str),
        "runs": json.dumps(list(ConnectorRun.objects.values("error", "counts")), default=str),
        "observations": json.dumps(
            list(Observation.objects.values("data", "display_name", "external_id")), default=str
        ),
        "connectors": json.dumps(list(Connector.objects.values("access_check", "config", "scope")), default=str),
        "connections": json.dumps(
            list(OktaConnection.objects.values("test_result"))
            + list(GoogleWorkspaceConnection.objects.values("test_result"))
        ),
        "logs": caplog.text,
        "responses": "\n".join(seen),
        "export": exported,
    }
    key_body = "".join(pem.splitlines()[1:-1])
    secrets = [key_body[:40], key_body[-40:], okta.TOKEN, google_workspace.TOKEN, "eyJ", "Bearer ", *ciphertexts]
    for where, text in stored.items():
        for value in (*secrets, *PEOPLE_TRACES):
            if where == "responses" and value == "Bearer ":
                continue  # response bodies never hold a token; "Bearer " only matters in stored text
            assert value not in text, f"{value[:16]}... found in {where}"


def test_the_purge_removes_the_connections_and_connectors(a, api, pem):
    from django.utils import timezone

    from tests.test_org_lifecycle import _age_final_export, _due_now, ask, cloud

    org = cloud(a["org"])
    o = OktaConnection.objects.create(organization=org, name="Okta", base_url=okta.SIMULATED_URL, client_id="c")
    g = GoogleWorkspaceConnection.objects.create(organization=org, name="Workspace", admin_email=ADMIN_EMAIL)
    credentials.slot_set(o, "secret", pem)
    o.save()
    Connector.objects.create(organization=org, source="okta", name="Okta", okta_connection=o)
    Connector.objects.create(organization=org, source="google_workspace", name="Gw", google_workspace_connection=g)
    assert ask(api, a["users"]["admin"]).status_code == 200
    drain_jobs(org_lifecycle.EXPORT_JOB)
    _due_now(org)
    _age_final_export(org)
    assert org_lifecycle.purge_due(timezone.now()) == {"purged": 1, "failed": 0}
    assert not OktaConnection.objects.filter(pk=o.pk).exists()
    assert not GoogleWorkspaceConnection.objects.filter(pk=g.pk).exists()
    assert not Connector.objects.filter(organization_id=org.pk).exists()


def test_a_connection_in_use_cannot_be_deleted(ent, ok, gw):
    org, admin = ent["org"], ent["users"]["admin"]
    for connector in (ok, gw):
        with pytest.raises(Conflict):
            connections.delete_connection(org, admin, discovery.connection_of(connector))
