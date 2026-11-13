"""A stored write-only secret is only ever sent where the Admin who entered it pointed it. Changing the address a
connection sends its secret to (another scheme, host or port; for SMTP also turning TLS off) without entering the
secret again is a 400 naming the secret field, and nothing is saved. With the secret in the same request the change is
saved; name, notes and enabled edits and a path-only change of the address keep the stored secret.

Covered: ticket connections (Jira, ServiceNow; tickets/services.py), Halo and Okta connections (core/connections.py),
the SMTP password (notify/channel_services.py) and the SSO draft client secret (core/sso.py)."""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from core import capabilities as caps
from core import credentials, halo, oidc_simulator, okta, outbound
from core.models import AuditEvent, HaloConnection, OktaConnection, SsoSettings
from notify.models import EmailSettings
from tests.sso_helpers import OKTA, SECRET
from tickets import simulator
from tickets.models import TicketConnection

pytestmark = pytest.mark.usefixtures("preview_on")


def _pem(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


TOKEN = "f06-original-token"  # noqa: S105 (synthetic)
NEW_TOKEN = "f06-new-token"  # noqa: S105 (synthetic)
ELSEWHERE = "https://attacker.example"


def refused(call, field):
    """`call()` is a 400 naming only `field`, and writes no audit event."""
    events = AuditEvent.objects.count()
    response = call()
    assert response.status_code == 400 and set(response.json_body["errors"]) == {field}, response.json_body
    assert AuditEvent.objects.count() == events
    return response


# ---- the comparison -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after", "same"),
    [
        ("https://jira.contoso.example", "https://jira.contoso.example/jira", True),
        ("https://jira.contoso.example", "https://JIRA.Contoso.Example.:443/", True),
        ("https://jira.contoso.example", "https://jira.contoso.example:8443", False),
        ("https://jira.contoso.example", "http://jira.contoso.example", False),
        ("https://jira.contoso.example", "https://jira.contoso.example.attacker.example", False),
        ("https://jira.contoso.example", ELSEWHERE, False),
    ],
)
def test_origins_compare_scheme_host_and_port_only(before, after, same):
    assert (outbound.origin(before) == outbound.origin(after)) is same


def test_a_first_destination_needs_the_secret_and_an_empty_one_does_not():
    assert outbound.secret_must_follow(None, outbound.origin(ELSEWHERE))
    assert not outbound.secret_must_follow(outbound.origin(ELSEWHERE), None)


# ---- ticket connections ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("system", "base_url"), [("jira", simulator.JIRA_URL), ("servicenow", simulator.SERVICENOW_URL)]
)
def test_a_ticket_connection_needs_its_secret_again_for_another_host(a, api, system, base_url):
    admin = api(a["users"]["admin"])
    made = admin.post(
        "ticket-connections", {"system": system, "base_url": base_url, "username": "svc", "secret": TOKEN}
    )
    assert made.status_code == 201, made.json_body
    url = f"ticket-connections/{made.json_body['id']}"

    refused(lambda: admin.patch(url, {"base_url": ELSEWHERE}), "secret")
    stored = TicketConnection.objects.get()
    assert stored.base_url == base_url and credentials.slot_value(stored, "secret") == TOKEN

    for edit in ({"name": "Renamed"}, {"notes": "Owned by IT"}, {"enabled": False}, {"base_url": base_url + "/x"}):
        kept = admin.patch(url, edit)
        assert kept.status_code == 200, (edit, kept.json_body)
    assert credentials.slot_value(TicketConnection.objects.get(), "secret") == TOKEN

    both = admin.patch(url, {"base_url": ELSEWHERE, "secret": NEW_TOKEN})
    assert both.status_code == 200, both.json_body
    stored = TicketConnection.objects.get()
    assert stored.base_url == ELSEWHERE and credentials.slot_value(stored, "secret") == NEW_TOKEN


# ---- Halo and Okta connections --------------------------------------------------------------------------------------


@pytest.fixture
def ent(a):
    a["org"], _ = caps.set_edition(a["org"], "enterprise")
    return a


def test_a_halo_connection_needs_its_client_secret_again_for_another_host(ent, api):
    admin = api(ent["users"]["admin"])
    made = admin.post(
        "connections/halo", {"name": "Halo", "base_url": halo.SIMULATED_URL, "client_id": "sw", "secret": TOKEN}
    )
    assert made.status_code == 201, made.json_body
    url = f"connections/halo/{made.json_body['id']}"

    moved = refused(lambda: admin.patch(url, {"base_url": ELSEWHERE}), "secret")
    assert "client secret" in moved.json_body["errors"]["secret"][0]
    assert HaloConnection.objects.get().base_url == halo.SIMULATED_URL

    for edit in ({"name": "Renamed"}, {"notes": "n"}, {"enabled": False}, {"base_url": halo.SIMULATED_URL + "/api"}):
        assert admin.patch(url, edit).status_code == 200, edit
    assert credentials.slot_value(HaloConnection.objects.get(), "secret") == TOKEN

    both = admin.patch(url, {"base_url": ELSEWHERE, "secret": NEW_TOKEN})
    assert both.status_code == 200, both.json_body
    stored = HaloConnection.objects.get()
    assert stored.base_url == ELSEWHERE and credentials.slot_value(stored, "secret") == NEW_TOKEN
    assert AuditEvent.objects.filter(action="halo_connection.updated").latest("created_at").changes["replaced"] == [
        "secret"
    ]


def test_an_okta_connection_needs_its_private_key_again_for_another_org(ent, api):
    pem, other = _pem(ec.generate_private_key(ec.SECP256R1())), _pem(ec.generate_private_key(ec.SECP256R1()))
    admin = api(ent["users"]["admin"])
    made = admin.post(
        "connections/okta", {"name": "Okta", "base_url": okta.SIMULATED_URL, "client_id": "0oa1", "secret": pem}
    )
    assert made.status_code == 201, made.json_body
    url = f"connections/okta/{made.json_body['id']}"

    moved = refused(lambda: admin.patch(url, {"base_url": "https://contoso.okta.com"}), "secret")
    assert "private key" in moved.json_body["errors"]["secret"][0]
    assert admin.patch(url, {"name": "Renamed", "notes": "n", "enabled": False}).status_code == 200
    assert credentials.slot_value(OktaConnection.objects.get(), "secret") == pem.strip()
    both = admin.patch(url, {"base_url": "https://contoso.okta.com", "secret": other})
    assert both.status_code == 200, both.json_body
    assert credentials.slot_value(OktaConnection.objects.get(), "secret") == other.strip()


# ---- the SMTP password ----------------------------------------------------------------------------------------------

SMTP = {
    "transport": "smtp",
    "smtp_host": "smtp.contoso.example",
    "smtp_security": "starttls",
    "smtp_username": "relay",
    "smtp_password": TOKEN,
    "from_address": "it@contoso.example",
}


@pytest.mark.parametrize(
    "change",
    [
        {"smtp_host": "mail.attacker.example"},
        {"smtp_port": 2525},
        {"smtp_security": "none"},
        {"smtp_host": "mail.attacker.example", "smtp_port": 25, "smtp_security": "none"},
    ],
)
def test_the_smtp_password_is_needed_again_for_another_server_or_without_tls(a, api, change):
    admin = api(a["users"]["admin"])
    assert admin.patch("notification-settings/email", SMTP).status_code == 200
    refused(lambda: admin.patch("notification-settings/email", change), "smtp_password")
    row = EmailSettings.objects.get()
    assert row.smtp_host == SMTP["smtp_host"] and row.smtp_security == "starttls" and row.smtp_port is None
    assert credentials.slot_value(row, "smtp_password") == TOKEN

    both = admin.patch("notification-settings/email", {**change, "smtp_password": NEW_TOKEN})
    assert both.status_code == 200, both.json_body
    assert credentials.slot_value(EmailSettings.objects.get(), "smtp_password") == NEW_TOKEN


def test_smtp_edits_that_keep_the_server_keep_the_password(a, api):
    admin = api(a["users"]["admin"])
    assert admin.patch("notification-settings/email", SMTP).status_code == 200
    for edit in (
        {"smtp_host": "SMTP.Contoso.example"},  # the same host
        {"smtp_port": 587},  # STARTTLS's usual port, the one already used
        {"smtp_username": "relay2", "from_address": "alerts@contoso.example"},
    ):
        kept = admin.patch("notification-settings/email", edit)
        assert kept.status_code == 200, (edit, kept.json_body)
    assert credentials.slot_value(EmailSettings.objects.get(), "smtp_password") == TOKEN
    # Removing the password needs nothing else; so does moving the server once no password is stored.
    assert (
        admin.patch("notification-settings/email", {"smtp_host": "x.example", "smtp_password": None}).status_code == 200
    )
    assert (
        admin.patch("notification-settings/email", {"smtp_host": "y.example", "smtp_security": "none"}).status_code
        == 200
    )


def test_emptying_the_smtp_host_then_setting_another_still_needs_the_password(a, api):
    """Two steps can't get around it: under Graph an empty SMTP host is allowed, and a later host is a change."""
    admin = api(a["users"]["admin"])
    assert admin.patch("notification-settings/email", SMTP).status_code == 200
    graph = {
        "transport": "graph",
        "graph_tenant_id": "simulated",
        "graph_client_id": "11111111-2222-4333-8444-555555555555",
        "graph_secret": "f06-graph-secret",
        "sender_mailbox": "softwarden@contoso.example",
    }
    emptied = admin.patch("notification-settings/email", {**graph, "smtp_host": ""})
    assert emptied.status_code == 200, emptied.json_body
    refused(lambda: admin.patch("notification-settings/email", {"smtp_host": "mail.attacker.example"}), "smtp_password")
    row = EmailSettings.objects.get()
    assert row.smtp_host == "" and credentials.slot_value(row, "smtp_password") == TOKEN


# ---- the SSO draft client secret ------------------------------------------------------------------------------------

GENERIC = "https://idp.contoso.example/realms/it/.well-known/openid-configuration"


@pytest.mark.parametrize(
    "change",
    [
        {"discovery_url": "https://idp.attacker.example/.well-known/openid-configuration"},
        {"okta_domain": "attacker.okta.com"},
        {"provider": "oidc", "discovery_url": "https://idp.attacker.example/.well-known/openid-configuration"},
    ],
)
def test_the_sso_client_secret_is_needed_again_for_another_identity_provider(a, api, simulator, change):
    admin = api(a["users"]["admin"])
    start = {"provider": "okta", **OKTA, "client_id": "0oa-simulated", "client_secret": SECRET}
    if "discovery_url" in change and "provider" not in change:
        start = {"provider": "oidc", "discovery_url": GENERIC, "client_id": "c1", "client_secret": SECRET}
    assert admin.patch("sso/settings", start).status_code == 200
    before = SsoSettings.objects.get(organization=a["org"])

    refused(lambda: admin.patch("sso/settings", change), "client_secret")
    row = SsoSettings.objects.get(organization=a["org"])
    assert row.draft_discovery_url == before.draft_discovery_url and row.draft_revision == before.draft_revision

    both = admin.patch("sso/settings", {**change, "client_secret": "f06-new-client-secret"})
    assert both.status_code == 200, both.json_body
    assert credentials.slot_value(SsoSettings.objects.get(organization=a["org"]), "draft_secret") == (
        "f06-new-client-secret"
    )


def test_sso_edits_that_keep_the_identity_provider_keep_the_client_secret(a, api, simulator):
    admin = api(a["users"]["admin"])
    assert (
        admin.patch(
            "sso/settings", {"provider": "okta", **OKTA, "client_id": "0oa-simulated", "client_secret": SECRET}
        ).status_code
        == 200
    )
    for edit in (
        {"client_id": "0oa-other"},
        {"okta_authorization_server": "aus1"},  # the same Okta host, another path
        {"okta_domain": oidc_simulator.OKTA_DOMAIN.upper()},
    ):
        kept = admin.patch("sso/settings", edit)
        assert kept.status_code == 200, (edit, kept.json_body)
    assert credentials.slot_value(SsoSettings.objects.get(organization=a["org"]), "draft_secret") == SECRET


def test_emptying_the_discovery_url_then_setting_another_still_needs_the_client_secret(a, api, simulator):
    admin = api(a["users"]["admin"])
    start = {"provider": "oidc", "discovery_url": GENERIC, "client_id": "c1", "client_secret": SECRET}
    assert admin.patch("sso/settings", start).status_code == 200
    emptied = admin.patch("sso/settings", {"discovery_url": ""})
    assert emptied.status_code == 200, emptied.json_body
    evil = "https://idp.attacker.example/.well-known/openid-configuration"
    refused(lambda: admin.patch("sso/settings", {"discovery_url": evil}), "client_secret")
    assert SsoSettings.objects.get(organization=a["org"]).draft_discovery_url == ""
