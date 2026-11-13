"""Per-organization single sign-on (core/sso.py, core/oidc.py, core/oidc_discovery.py): generic OpenID
Connect from the discovery document with presets for Entra, Okta and Google Workspace; the draft / test / activate
flow; the session pin; break-glass SOFTWARDEN_FORCE_LOCAL_LOGIN and `sso_recovery`. Everything goes through the
in-process simulated identity providers (core/oidc_simulator.py); the suite refuses any real network call
(tests/conftest.py no_identity_provider_network)."""

import io
import json
import urllib.parse

import pytest
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from core import oidc_discovery, oidc_simulator, sso
from core.models import AuditEvent, SsoDomain, SsoSettings, SsoStatus, UserIdentity
from tests.sso_helpers import (
    OKTA,
    SECRET,
    activate,
    authorize,
    callback,
    sign_in,
    sign_in_with,
)

OKTA_ISSUER = f"https://{oidc_simulator.OKTA_DOMAIN}/oauth2/default"
ENTRA_ISSUER = f"https://login.microsoftonline.com/{oidc_simulator.TENANT_ID}/v2.0"
ROUTES = ("sso/settings", "sso/settings/test", "sso/settings/activate", "sso/settings/disable")


@pytest.fixture(autouse=True)
def fresh_throttle(monkeypatch):
    """Start and callback share local sign-in's throttle (10 a minute, tested on its own below); these journeys sign
    in many times, so the rate is lifted and each test starts with an empty count."""
    from django.core.cache import cache
    from rest_framework.throttling import ScopedRateThrottle

    monkeypatch.setattr(
        ScopedRateThrottle, "THROTTLE_RATES", {**ScopedRateThrottle.THROTTLE_RATES, "login": "1000/min"}
    )
    cache.clear()


def me(client):
    return client.get("/api/v1/auth/me").json()


def options(query=""):
    return Client().get("/api/v1/auth/options" + query).json()


def follow_test(client, authorize_url, email):
    """Sign in at the simulated provider during a test sign-in; returns the callback's redirect."""
    url = urllib.parse.urlsplit(authorize_url)
    assert url.path == "/api/v1/auth/oidc/simulator"
    params = dict(urllib.parse.parse_qsl(url.query))
    back = client.post(url.path, {**params, "email": email, "action": "sign_in"})
    return callback(client, back["Location"])


def okta_draft(admin, **extra):
    body = {"provider": "okta", **OKTA, "client_id": "0oa-simulated", "client_secret": SECRET, **extra}
    response = admin.patch("sso/settings", body)
    assert response.status_code == 200, response.json_body
    return response.json_body


# ---- two organizations, two providers ------------------------------------------------------------------------------


def test_two_organizations_sign_in_through_two_providers(a, b, simulator):
    activate(a["org"], "entra")
    activate(b["org"], "okta")
    SsoDomain.objects.create(organization=b["org"], domain="orgb.test", verified_at=timezone.now())

    by_slug = options("?org=orga")
    assert by_slug["entra"] is True and by_slug["sso"]["provider"] == "entra"
    assert by_slug["sso"]["start_url"] == "/api/v1/auth/oidc/start?org=orga"
    by_email = options("?email=editor@orgb.test")
    assert by_email["entra"] is False and by_email["sso"]["provider"] == "okta"
    assert by_email["sso"]["label"] == "Okta"
    assert by_email["sso"]["start_url"] == "/api/v1/auth/oidc/start?email=editor%40orgb.test"
    assert options()["sso"] is None  # two organizations and no hint: password sign-in only

    entra_client, okta_client = Client(), Client()
    assert authorize(Client(), {"org": "orga"})["idp"] == "entra"
    assert sign_in(entra_client, "editor@orga.test", query={"org": "orga"})["Location"] == "/renewals"
    assert me(entra_client)["organization"]["name"] == "orga"
    params = authorize(Client(), {"email": "editor@orgb.test"})
    assert params["idp"] == "okta" and params["login_hint"] == "editor@orgb.test" and "prompt" not in params
    assert sign_in(okta_client, "editor@orgb.test", query={"email": "editor@orgb.test"})["Location"] == "/renewals"
    assert me(okta_client)["organization"]["name"] == "orgb"

    pins = {i.user_id: (i.issuer, i.subject) for i in UserIdentity.objects.all()}
    assert pins[a["users"]["editor"].id] == (ENTRA_ISSUER, oidc_simulator.object_id("editor@orga.test"))
    assert pins[b["users"]["editor"].id] == (OKTA_ISSUER, oidc_simulator.subject("okta", "editor@orgb.test"))
    methods = set(AuditEvent.objects.filter(action="user.login").values_list("changes__method", flat=True))
    assert methods == {"entra", "okta"}
    # One organization's provider never signs in another organization's member.
    assert sign_in(Client(), "viewer@orgb.test", query={"org": "orga"})["Location"].startswith(
        "/login?error=no_account"
    )


def test_google_workspace_and_generic_oidc(a, b, simulator):
    activate(a["org"], "google")  # Workspace domain orga.test
    activate(b["org"], "oidc")  # the Okta-shaped simulator through its discovery URL
    google = Client()
    # Google may send iss without the scheme and email_verified as a string; both are accepted and pinned the same.
    done = sign_in_with(
        google, "admin@orga.test", {"iss": "accounts.google.com", "email_verified": "true"}, {"org": "orga"}
    )
    assert done["Location"] == "/dashboard"
    identity = UserIdentity.objects.get(user=a["users"]["admin"])
    assert identity.issuer == "https://accounts.google.com"
    assert sign_in(Client(), "admin@orga.test", query={"org": "orga"})["Location"] == "/renewals"  # the same pin
    assert sign_in(Client(), "admin@orgb.test", query={"org": "orgb"})["Location"] == "/renewals"
    assert UserIdentity.objects.get(user=b["users"]["admin"]).issuer == OKTA_ISSUER


# ---- refusals, each with its code ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider,overrides,code",
    [
        ("okta", {"iss": "https://elsewhere.simulated.invalid"}, "issuer"),
        ("okta", {"aud": "someone-else"}, "audience"),
        ("okta", {"azp": "someone-else"}, "audience"),
        ("okta", {"nonce": "replayed"}, "nonce"),
        ("okta", {"email_verified": False}, "email_unverified"),
        ("okta", {"email_verified": "false"}, "email_unverified"),
        ("google", {"hd": "elsewhere.test"}, "hosted_domain"),
        ("google", {"hd": None}, "hosted_domain"),
        ("google", {"email_verified": False}, "email_unverified"),
        ("google", {"iss": "https://accounts.example"}, "issuer"),
        ("entra", {"tid": "11111111-0000-4000-8000-000000000000"}, "tenant"),
        ("oidc", {"email_verified": "no"}, "email_unverified"),
        # Algorithm confusion: the document lists RS256 only; HS256 signed with the client secret, or no signature.
        ("okta", {"_alg": "HS256"}, "unsupported_algorithm"),
        ("okta", {"_alg": "none"}, "unsupported_algorithm"),
        ("entra", {"_alg": "HS256"}, "unsupported_algorithm"),
    ],
)
def test_id_token_checks_refuse_with_a_code(a, simulator, provider, overrides, code):
    activate(a["org"], provider)
    done = sign_in_with(Client(), "editor@orga.test", overrides, {"org": "orga"})
    assert done["Location"] == f"/login?error={code}&org=orga"  # the SSO button comes back
    assert not UserIdentity.objects.exists()


def test_state_and_pkce_are_checked(a, simulator):
    activate(a["org"], "okta")
    client = Client()
    params = authorize(client, {"org": "orga"})
    code = oidc_simulator.issue_code(params, "editor@orga.test", idp="okta")
    assert (
        client.get(f"/api/v1/auth/oidc/callback?code={code}&state=forged")["Location"]
        == "/login?error=expired&org=orga"
    )
    # The transaction is single-use: the right state after a refusal is expired too.
    state = urllib.parse.quote(params["state"], safe="")
    assert client.get(f"/api/v1/auth/oidc/callback?code={code}&state={state}")["Location"] == "/login?error=expired"
    # A code issued for another PKCE challenge is refused by the provider (invalid_grant).
    client = Client()
    params = authorize(client, {"org": "orga"})
    code = oidc_simulator.issue_code({**params, "code_challenge": "not-the-challenge"}, "editor@orga.test", idp="okta")
    state = urllib.parse.quote(params["state"], safe="")
    assert client.get(f"/api/v1/auth/oidc/callback?code={code}&state={state}")["Location"].startswith(
        "/login?error=token_refused"
    )


def test_discovery_document_checks(a, simulator):
    """The discovery document must name the expected issuer and an accepted algorithm, and must be found."""
    activate(a["org"], "okta")
    idp = oidc_simulator.IDPS["okta"]
    idp.doc_overrides = {"issuer": "https://elsewhere.simulated.invalid"}
    assert Client().get("/api/v1/auth/oidc/start?org=orga")["Location"] == "/login?error=provider_error&org=orga"
    config = sso.effective(a["org"])
    assert config is not None
    with pytest.raises(oidc_discovery.ProviderError) as refused:
        oidc_discovery.metadata(config.provider)
    assert refused.value.code == "issuer_mismatch"
    for overrides, code in (
        ({"id_token_signing_alg_values_supported": ["HS256", "none"]}, "unsupported_algorithm"),
        ({"token_endpoint": "http://okta.simulated.invalid/token"}, "discovery_invalid"),
    ):
        oidc_discovery.clear_cache()
        idp.doc_overrides = overrides
        with pytest.raises(oidc_discovery.ProviderError) as refused:
            oidc_discovery.metadata(config.provider)
        assert refused.value.code == code


def test_a_generic_providers_token_endpoint_is_on_the_discovery_documents_origin(a, simulator):
    """The generic OpenID Connect preset sends the client secret only to the discovery URL's own origin, so a
    document served from another path on a shared host can't send it elsewhere. The named presets keep their
    providers' documented hosts (Google's token endpoint is on another host than its discovery document)."""
    activate(a["org"], "oidc")
    idp = oidc_simulator.IDPS["okta"]  # the generic preset reads the simulated Okta server's document
    config = sso.effective(a["org"])
    assert config is not None
    assert oidc_discovery.metadata(config.provider).token_endpoint.startswith(f"https://{oidc_simulator.OKTA_DOMAIN}/")
    for elsewhere in ("https://evil.simulated.invalid/token", f"https://{oidc_simulator.OKTA_DOMAIN}:8443/token"):
        oidc_discovery.clear_cache()
        idp.doc_overrides = {"token_endpoint": elsewhere}
        with pytest.raises(oidc_discovery.ProviderError) as refused:
            oidc_discovery.metadata(config.provider)
        message = str(refused.value)
        assert refused.value.code == "discovery_invalid" and "token endpoint" in message
        # The Admin learns which host and why (review follow-up): the token endpoint's host and the discovery host.
        assert urllib.parse.urlsplit(elsewhere).netloc in message and oidc_simulator.OKTA_DOMAIN in message
        assert "client secret" in message
    oidc_discovery.clear_cache()
    idp.doc_overrides = {}
    activate(a["org"], "google")
    google = sso.effective(a["org"])
    assert google is not None and oidc_discovery.metadata(google.provider).token_endpoint.startswith(
        "https://oauth2.google"
    )


def test_an_unknown_key_id_refetches_the_key_set_once(a, simulator, monkeypatch):
    activate(a["org"], "okta")
    idp = oidc_simulator.IDPS["okta"]
    assert sign_in(Client(), "editor@orga.test", query={"org": "orga"})["Location"] == "/renewals"
    assert idp.key_fetches == 1
    assert sign_in(Client(), "editor@orga.test", query={"org": "orga"})["Location"] == "/renewals"
    assert idp.key_fetches == 1  # cached
    oidc_simulator.rotate("okta")  # the provider signs with a new key the cached set lacks
    assert sign_in(Client(), "editor@orga.test", query={"org": "orga"})["Location"] == "/renewals"
    assert idp.key_fetches == 2  # one refetch
    assert sign_in(Client(), "viewer@orga.test", query={"org": "orga"})["Location"] == "/renewals"
    assert idp.key_fetches == 2
    # A key the provider never publishes: one refetch, then refused.
    oidc_simulator.rotate("okta")
    published = idp.keys[:-1]
    real_jwks = idp.jwks
    monkeypatch.setattr(idp, "jwks", lambda: {"keys": [k for k in real_jwks()["keys"] if k["kid"] != idp.keys[-1][0]]})
    assert published  # the earlier keys stay published
    assert sign_in(Client(), "editor@orga.test", query={"org": "orga"})["Location"].startswith(
        "/login?error=unknown_key"
    )
    assert idp.key_fetches == 3


# ---- the draft, its test and activation ----------------------------------------------------------------------------


def test_a_draft_is_tested_with_a_real_sign_in_then_activated(a, api, simulator):
    admin_user = a["users"]["admin"]
    admin = api(admin_user)
    body = okta_draft(admin)
    assert body["status"] == "legacy" and body["active"] is None
    draft = body["draft"]
    assert (
        draft["revision"] == 1 and draft["secret_set"] is True and draft["tested"] is False and draft["missing"] == {}
    )
    assert draft["discovery_url"] == oidc_simulator.IDPS["okta"].discovery_url and draft["issuer"] == OKTA_ISSUER
    assert body["redirect_uri"] == "http://testserver/api/v1/auth/oidc/callback"
    assert options("?org=orga")["sso"] is None  # nothing signs people in yet

    # Not tested: refused.
    refused = admin.post("sso/settings/activate", {"draft_revision": 1})
    assert refused.status_code == 409 and refused.json_body["type"] == "/problems/sso-test-required"

    started = admin.post("sso/settings/test", {"draft_revision": 1})
    assert started.status_code == 200, started.json_body
    done = follow_test(admin.client, started.json_body["authorize_url"], "admin@orga.test")
    assert done["Location"] == "/admin/sign-in?sso_test=passed"
    # The test signed nobody in and pinned nothing: the session is still the Admin's own, as before.
    assert me(admin.client)["user"]["id"] == admin_user.id
    assert not UserIdentity.objects.exists()
    assert not AuditEvent.objects.filter(action="user.login").exists()
    test = admin.get("sso/settings").json_body["draft"]
    assert test["tested"] is True and test["test"]["ok"] is True and test["test"]["revision"] == 1
    assert test["test"]["by"]["id"] == admin_user.id and "admin@orga.test" in test["test"]["detail"]

    # A saved change invalidates the test: the stale revision can't be activated.
    changed = admin.patch("sso/settings", {"client_id": "0oa-other"}).json_body["draft"]
    assert changed["revision"] == 2 and changed["tested"] is False
    stale = admin.post("sso/settings/activate", {"draft_revision": 2})
    assert stale.status_code == 409 and stale.json_body["type"] == "/problems/sso-test-required"
    old = admin.post("sso/settings/activate", {"draft_revision": 1})
    assert old.status_code == 409 and old.json_body["type"] == "/problems/sso-draft-changed"
    admin.patch("sso/settings", {"client_id": "0oa-simulated"})  # back, as revision 3

    started = admin.post("sso/settings/test", {"draft_revision": 3}).json_body
    assert follow_test(admin.client, started["authorize_url"], "admin@orga.test")["Location"].endswith("passed")
    active = admin.post("sso/settings/activate", {"draft_revision": 3})
    assert active.status_code == 200, active.json_body
    body = active.json_body
    assert body["status"] == "active" and body["active"]["revision"] == 1 and body["active"]["secret_set"] is True
    assert body["active"]["activated_by"]["id"] == admin_user.id and body["effective"]["source"] == "active"
    assert options("?org=orga")["sso"]["provider"] == "okta"
    assert sign_in(Client(), "editor@orga.test", query={"org": "orga"})["Location"] == "/renewals"

    actions = list(AuditEvent.objects.filter(action__startswith="sso_settings.").values_list("action", flat=True))
    assert actions.count("sso_settings.updated") == 3 and actions.count("sso_settings.tested") == 2
    assert actions.count("sso_settings.activated") == 1


def test_a_broken_draft_fails_its_test_with_a_recorded_detail(a, api, simulator):
    admin = api(a["users"]["admin"])
    # A mistyped Okta domain: its discovery document isn't there, so the test fails before leaving.
    okta_draft(admin, okta_domain="typo.simulated.invalid")
    started = admin.post("sso/settings/test", {"draft_revision": 1}).json_body
    assert started["authorize_url"] is None
    test = started["settings"]["draft"]["test"]
    assert test["ok"] is False and test["code"] == "fetch_failed" and test["revision"] == 1
    assert test["detail"].startswith("The test didn't start: The identity provider answered 404")
    assert admin.post("sso/settings/activate", {"draft_revision": 1}).status_code == 409

    # A rejected client secret: the round trip fails at the code exchange.
    admin.patch("sso/settings", {**OKTA, "client_secret": oidc_simulator.REJECTED_SECRET})
    started = admin.post("sso/settings/test", {"draft_revision": 2}).json_body
    done = follow_test(admin.client, started["authorize_url"], "admin@orga.test")
    assert done["Location"] == "/admin/sign-in?sso_test=failed&reason=token_refused"
    test = admin.get("sso/settings").json_body["draft"]["test"]
    assert test["ok"] is False and test["code"] == "token_refused" and "invalid_client" in test["detail"]
    assert admin.post("sso/settings/activate", {"draft_revision": 2}).status_code == 409

    # Signing in as someone else proves nothing about the Admin's own account.
    admin.patch("sso/settings", {"client_secret": SECRET})
    started = admin.post("sso/settings/test", {"draft_revision": 3}).json_body
    done = follow_test(admin.client, started["authorize_url"], "editor@orga.test")
    assert done["Location"] == "/admin/sign-in?sso_test=failed&reason=other_account"
    assert admin.get("sso/settings").json_body["draft"]["tested"] is False
    events = AuditEvent.objects.filter(action="sso_settings.tested").order_by("created_at")
    assert [e.changes["code"] for e in events] == ["fetch_failed", "token_refused", "other_account"]


def test_draft_validation_and_the_secret_stays_write_only(a, api, simulator):
    admin = api(a["users"]["admin"])
    cases = [
        ({"provider": "ldap"}, "provider"),  # saml is a provider (the optional ee package's)
        ({"provider": "entra", "tenant_id": "common"}, "tenant_id"),
        ({"provider": "entra", "tenant_id": "contoso.onmicrosoft.com"}, "tenant_id"),
        ({"provider": "entra", "tenant_id": "simulated", "client_id": "not-a-guid"}, "client_id"),
        ({"okta_domain": "https://example.okta.com"}, "okta_domain"),
        ({"okta_authorization_server": "bad server"}, "okta_authorization_server"),
        ({"discovery_url": "http://idp.example.com/.well-known/openid-configuration"}, "discovery_url"),
        ({"discovery_url": "https://idp.example.com/oauth"}, "discovery_url"),
        ({"hosted_domains": ["not a domain"]}, "hosted_domains"),
        ({"client_secret": ""}, "client_secret"),
        ({"sign_in_domains": "orga.test"}, "sign_in_domains"),
        ({"active": True}, "active"),
    ]
    for data, field in cases:
        response = admin.patch("sso/settings", data)
        assert response.status_code == 400 and field in response.json_body["errors"], (data, response.json_body)
    assert not SsoSettings.objects.filter(organization=a["org"], draft_revision__gt=0).exists()

    entra = admin.patch(
        "sso/settings",
        {
            "provider": "entra",
            "tenant_id": "0A1B2C3D-0000-4000-8000-000000000001",
            "client_id": "11111111-2222-4333-8444-555555555555",
        },
    ).json_body["draft"]
    assert entra["discovery_url"] == (
        "https://login.microsoftonline.com/0a1b2c3d-0000-4000-8000-000000000001/v2.0/.well-known/openid-configuration"
    )
    assert entra["missing"] == {"client_secret": ["Enter the client secret."]}
    google = admin.patch("sso/settings", {"provider": "google", "hosted_domains": ["OrgA.test", "orga.test"]}).json_body
    assert (
        google["draft"]["hosted_domains"] == ["orga.test"]
        and google["draft"]["issuer"] == "https://accounts.google.com"
    )
    custom = admin.patch(
        "sso/settings",
        {"provider": "okta", "okta_domain": "example.okta.com", "okta_authorization_server": "aus1"},
    ).json_body["draft"]
    assert custom["issuer"] == "https://example.okta.com/oauth2/aus1"
    org_server = admin.patch("sso/settings", {"okta_authorization_server": ""}).json_body["draft"]
    assert org_server["discovery_url"] == "https://example.okta.com/.well-known/openid-configuration"
    generic = admin.patch(
        "sso/settings",
        {"provider": "oidc", "discovery_url": "https://idp.example.com/realms/it/.well-known/openid-configuration"},
    ).json_body["draft"]
    assert generic["issuer"] == "https://idp.example.com/realms/it"

    # The secret: write-only, never in a response, an ETag input or the audit log.
    first = admin.get("sso/settings")
    saved = admin.patch("sso/settings", {"client_secret": "a-very-distinct-client-secret"})
    assert saved.json_body["draft"]["secret_set"] is True and saved.json_body["draft"]["secret_set_at"]
    assert "a-very-distinct-client-secret" not in saved.content.decode()
    assert saved["ETag"] != first["ETag"]
    events = AuditEvent.objects.filter(action="sso_settings.updated")
    dumped = json.dumps([[e.changes, e.prechange, e.postchange] for e in events], default=str)
    assert "a-very-distinct-client-secret" not in dumped and "ciphertext" not in dumped
    assert events.order_by("-created_at")[0].changes["replaced"] == ["client_secret"]


def test_if_match_guards_the_draft(a, api, simulator):
    admin = api(a["users"]["admin"])
    tag = admin.get("sso/settings")["ETag"]
    client = admin.client
    patch = lambda data, etag: client.patch(  # noqa: E731
        "/api/v1/sso/settings", data=json.dumps(data), content_type="application/json", HTTP_IF_MATCH=etag
    )
    moved = patch({"provider": "okta"}, tag)
    assert moved.status_code == 200 and moved["ETag"] != tag
    stale = patch({"provider": "google"}, tag)
    assert stale.status_code == 412 and stale.json()["type"] == "/problems/precondition-failed"


def test_sign_in_domains_are_unique_on_the_installation(a, b, api, simulator):
    admin_a, admin_b = api(a["users"]["admin"]), api(b["users"]["admin"])
    claimed = admin_a.patch("sso/settings", {"sign_in_domains": ["OrgA.test", "a.example"]}).json_body
    assert claimed["sign_in_domains"] == ["a.example", "orga.test"]
    assert claimed["draft"]["revision"] == 0  # domains aren't part of the draft
    taken = admin_b.patch("sso/settings", {"sign_in_domains": ["orgb.test", "orga.test"]})
    assert taken.status_code == 400 and "orga.test" in taken.json_body["errors"]["sign_in_domains"][0]
    assert list(SsoDomain.objects.filter(organization=b["org"])) == []
    with pytest.raises(IntegrityError), transaction.atomic():
        SsoDomain.objects.create(organization=b["org"], domain="a.example", verified_at=timezone.now())
    admin_a.patch("sso/settings", {"sign_in_domains": ["a.example"]})
    assert admin_b.patch("sso/settings", {"sign_in_domains": ["orga.test"]}).status_code == 200


def test_unknown_organizations_look_like_password_only_ones(a, b, simulator):
    activate(b["org"], "okta")
    SsoDomain.objects.create(organization=b["org"], domain="orgb.test", verified_at=timezone.now())
    password_only = options("?org=orga")
    assert password_only == {
        "entra": False,
        "setup_pending": False,
        "password": True,
        "signup_enabled": False,
        "password_reset_enabled": False,  # no email in the test settings
        "sso": None,
    }
    for query in ("?org=no-such-org", "?email=someone@unknown.test", "?email=not-an-email", "?email=editor@orga.test"):
        assert options(query) == password_only, query
    # The answer only echoes what was sent, as for an organization without SSO.
    refused = Client().get("/api/v1/auth/oidc/start?org=no-such-org")["Location"]
    assert refused == "/login?error=not_configured&org=no-such-org"


def test_legacy_settings_serve_a_single_organization_only(a, simulator, legacy_entra, api):
    assert options()["entra"] is True and options("?org=orga")["entra"] is True
    # The organization's own active settings win over the environment.
    activate(a["org"], "okta")
    assert options()["sso"]["provider"] == "okta"
    # Turned off here means off, the environment included.
    sso.disable(a["org"], a["users"]["admin"])
    assert options()["sso"] is None
    sso.use_env(a["org"], a["users"]["admin"], via="api")
    assert options()["entra"] is True
    from tests.conftest import make_org

    make_org("orgc")  # a second organization: the legacy settings stop applying
    assert options()["sso"] is None and options("?org=orga")["sso"] is None


# ---- a change between start and callback ---------------------------------------------------------------------------


def test_a_configuration_change_during_sign_in_is_refused(a, api, simulator):
    activate(a["org"], "okta")
    client = Client()
    params = authorize(client, {"org": "orga"})
    activate(a["org"], "okta")  # re-activated: a new active revision
    back = client.post("/api/v1/auth/oidc/simulator", {**params, "email": "editor@orga.test", "action": "sign_in"})
    assert callback(client, back["Location"])["Location"] == "/login?error=changed&org=orga&next=%2Frenewals"
    # Turned off in between: refused the same way.
    client = Client()
    params = authorize(client, {"org": "orga"})
    sso.disable(a["org"], a["users"]["admin"])
    back = client.post("/api/v1/auth/oidc/simulator", {**params, "email": "editor@orga.test", "action": "sign_in"})
    assert callback(client, back["Location"])["Location"].startswith("/login?error=changed")
    assert not UserIdentity.objects.exists()


def test_a_draft_saved_during_its_test_is_not_marked_tested(a, api, simulator):
    admin = api(a["users"]["admin"])
    okta_draft(admin)
    started = admin.post("sso/settings/test", {"draft_revision": 1}).json_body
    admin.patch("sso/settings", {"client_id": "0oa-simulated-2"})
    done = follow_test(admin.client, started["authorize_url"], "admin@orga.test")
    assert done["Location"] == "/admin/sign-in?sso_test=failed&reason=changed"
    draft = admin.get("sso/settings").json_body["draft"]
    assert draft["revision"] == 2 and draft["test"]["ok"] is None and draft["tested"] is False


def test_a_test_needs_the_admin_session_that_started_it(a, api, simulator):
    from core.models import Membership

    admin = api(a["users"]["admin"])
    okta_draft(admin)
    started = admin.post("sso/settings/test", {"draft_revision": 1}).json_body
    Membership.objects.filter(user=a["users"]["admin"], organization=a["org"]).update(role="editor")
    done = follow_test(admin.client, started["authorize_url"], "admin@orga.test")
    assert done["Location"] == "/admin/sign-in?sso_test=failed&reason=test_session"
    assert SsoSettings.objects.get(organization=a["org"]).draft_test_ok is None


# ---- break-glass and recovery --------------------------------------------------------------------------------------


def test_force_local_login_blocks_every_sso_route(a, api, simulator, settings):
    activate(a["org"], "okta")
    admin = api(a["users"]["admin"])
    pending = Client()
    params = authorize(pending, {"org": "orga"})  # started before the switch
    settings.SOFTWARDEN_FORCE_LOCAL_LOGIN = True

    assert options("?org=orga") == {
        "entra": False,
        "setup_pending": False,
        "password": True,
        "signup_enabled": False,
        "password_reset_enabled": False,  # no email in the test settings
        "sso": None,
    }
    assert Client().get("/api/v1/auth/oidc/start?org=orga")["Location"] == "/login?error=not_configured&org=orga"
    assert Client().get("/api/v1/auth/oidc/simulator?idp=okta&redirect_uri=x").status_code == 404
    code = oidc_simulator.issue_code(params, "editor@orga.test", idp="okta")
    state = urllib.parse.quote(params["state"], safe="")
    refused = pending.get(f"/api/v1/auth/oidc/callback?code={code}&state={state}")
    assert refused["Location"].startswith("/login?error=not_configured")
    body = admin.get("sso/settings").json_body
    assert body["force_local_login"] is True and body["effective"]["source"] == "force_local"
    for route, data in (("sso/settings/test", {"draft_revision": 1}), ("sso/settings/activate", {"draft_revision": 1})):
        response = admin.post(route, data)
        assert response.status_code == 409 and response.json_body["type"] == "/problems/sso-local-only", route
    login = Client().post(
        "/api/v1/auth/login",
        {"email": "admin@orga.test", "password": "correct-horse-battery"},
        content_type="application/json",
    )
    assert login.status_code == 200  # password sign-in is never removed
    assert not UserIdentity.objects.exists()


def run_recovery(*args):
    out = io.StringIO()
    call_command("sso_recovery", *args, stdout=out)
    return out.getvalue()


def test_sso_recovery_shows_and_disables(a, b, simulator):
    activate(a["org"], "entra")
    activate(b["org"], "okta")
    shown = run_recovery()
    assert "orga: active entra (status active)" in shown and "orgb: active okta (status active)" in shown
    assert "Admins who can sign in with a password: 1" in shown and SECRET not in shown

    out = run_recovery("--org", "orga", "--disable")
    assert "orga: single sign-on turned off." in out
    assert sso.effective(a["org"]) is None and sso.effective(b["org"]) is not None
    event = AuditEvent.objects.get(action="sso_settings.disabled", organization=a["org"])
    assert event.actor is None and event.changes == {"status": ["active", "disabled"], "via": "cli"}

    out = run_recovery("--all", "--disable")
    assert "orga: single sign-on already off." in out and "orgb: single sign-on turned off." in out
    assert sso.effective(b["org"]) is None
    assert AuditEvent.objects.filter(action="sso_settings.disabled").count() == 2  # one per change, in each org

    for bad in (["--disable"], ["--org", "orga"], ["--all", "--use-env"], ["--org", "nope", "--disable"]):
        with pytest.raises(CommandError):
            run_recovery(*bad)


def test_sso_recovery_hands_back_to_the_environment(a, simulator, legacy_entra):
    activate(a["org"], "okta")
    assert options()["sso"]["provider"] == "okta"
    out = run_recovery("--org", "orga", "--use-env")
    assert "orga: single sign-on handed back to SOFTWARDEN_OIDC_*." in out and "legacy entra" in out
    assert options()["sso"]["provider"] == "entra"
    event = AuditEvent.objects.get(action="sso_settings.legacy_restored")
    assert event.changes == {"status": ["active", "legacy"], "via": "cli"}
    # The locked-out Admin is back in through the environment's provider, or with their password.
    assert sign_in(Client(), "admin@orga.test")["Location"] == "/renewals"


def test_sso_recovery_warns_without_a_password_admin(a, simulator):
    admin = a["users"]["admin"]
    admin.set_unusable_password()
    admin.save()
    assert "Recover one with `manage.py reset_admin <email>`" in run_recovery()


# ---- tenancy and roles ---------------------------------------------------------------------------------------------


def test_an_admin_never_reads_or_changes_another_organizations_settings(a, b, api, simulator):
    theirs = activate(b["org"], "okta")
    SsoDomain.objects.create(organization=b["org"], domain="orgb.test", verified_at=timezone.now())
    admin_a = api(a["users"]["admin"])
    mine = admin_a.get("sso/settings").json_body
    assert mine["status"] == "legacy" and mine["active"] is None and mine["sign_in_domains"] == []
    assert mine["draft"]["client_id"] == "" and theirs.client_id not in json.dumps(mine)
    admin_a.patch("sso/settings", {"provider": "entra", "tenant_id": "simulated", "client_id": theirs.client_id})
    assert admin_a.post("sso/settings/activate", {"draft_revision": 1}).status_code == 409
    admin_a.post("sso/settings/disable")
    row = SsoSettings.objects.get(organization=b["org"])
    assert (row.status, row.draft_provider, row.active_revision) == (SsoStatus.ACTIVE, "okta", theirs.active_revision)
    assert not AuditEvent.objects.filter(organization=b["org"], action__startswith="sso_settings.").exists()


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_the_settings_are_admin_only(a, api, simulator, role):
    client = api(a["users"][role])
    assert client.get("sso/settings").status_code == 403
    for route in ROUTES:
        response = client.post(route, {"draft_revision": 1}) if route != "sso/settings" else client.patch(route, {})
        assert response.status_code == 403 and response["Content-Type"] == "application/problem+json", route
    assert api().get("sso/settings").status_code == 403


# ---- the identity migration ----------------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_existing_entra_identities_move_to_issuer_and_subject():
    from django.contrib.auth import get_user_model
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before = [("core", "0010_halo_scope_read_assets")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old = executor.loader.project_state(before).apps.get_model("core", "UserIdentity")
    user = get_user_model().objects.create_user(username="pinned@example.test", password="x")
    try:
        old.objects.create(
            user_id=user.id, tenant_id=oidc_simulator.TENANT_ID.upper(), object_id="oid-1"
        )  # lower-cased
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        moved = UserIdentity.objects.get(user=user)
        assert (moved.issuer, moved.subject) == (ENTRA_ISSUER, "oid-1")
    finally:
        UserIdentity.objects.filter(user=user).delete()
        user.delete()


# ---- confinement, domains, throttling, the e2e hook -----------------------------------------------------------------


def _password_sign_in(client, email):
    return client.post(
        "/api/v1/auth/login", {"email": email, "password": "correct-horse-battery"}, content_type="application/json"
    )


def test_another_organizations_provider_cannot_take_over_an_account(a, b, api, simulator):
    """Org A's editor is also in org B (only by accepting B's invitation), and B's Admin points B's
    SSO at a provider they control: the first sign-in's email link is refused, because the account is in another
    organization too."""
    from core.models import Membership

    victim = a["users"]["editor"]
    invited = api(b["users"]["admin"]).post("users", {"email": victim.email, "role": "viewer"})
    assert invited.status_code == 201, invited.json_body
    assert not Membership.objects.filter(user=victim, organization=b["org"]).exists()  # an invitation adds nobody
    Membership.objects.create(user=victim, organization=b["org"], role="viewer")  # as if the victim had accepted
    activate(b["org"], "oidc")  # no email_verified needed; the provider says whatever B's Admin wants
    attacker = Client()
    done = sign_in(attacker, victim.email, query={"org": "orgb"})
    assert done["Location"].startswith("/login?error=no_account")
    assert not UserIdentity.objects.filter(user=victim).exists()
    assert attacker.get("/api/v1/auth/me").status_code == 403  # nobody signed in


def test_an_sso_session_reaches_only_the_organization_that_signed_it_in(a, b, simulator):
    """Even a pinned identity from org B's provider reaches org B only: the switch to org A is refused as if A didn't
    exist, me lists only B, and A's data is out of reach. A password sign-in lifts the confinement."""
    from core.models import Membership

    shared = b["users"]["editor"]
    activate(b["org"], "okta")
    assert sign_in(Client(), shared.email, query={"org": "orgb"})["Location"] == "/renewals"  # pinned while in B only
    Membership.objects.create(user=shared, organization=a["org"], role="admin")  # later joins A, as an Admin
    client = Client()
    assert sign_in(client, shared.email, query={"org": "orgb"})["Location"] == "/renewals"
    body = me(client)
    assert body["organization"]["name"] == "orgb"
    assert [m["name"] for m in body["organizations"]] == ["orgb"]
    switch = client.post(
        "/api/v1/auth/me/organization", {"organization_id": str(a["org"].id)}, content_type="application/json"
    )
    assert switch.status_code == 404
    assert me(client)["organization"]["name"] == "orgb"
    assert client.get(f"/api/v1/applications/{a['app'].id}").status_code == 404
    # A password sign-in in the same browser reaches every membership again.
    assert _password_sign_in(client, shared.email).status_code == 200
    names = {m["name"] for m in me(client)["organizations"]}
    assert names == {"orga", "orgb"}
    switched = client.post(
        "/api/v1/auth/me/organization", {"organization_id": str(a["org"].id)}, content_type="application/json"
    )
    assert switched.status_code == 200


def test_sso_recovery_moves_and_releases_domains(a, b, simulator):
    SsoDomain.objects.create(organization=b["org"], domain="orga.test", verified_at=timezone.now())  # squatted by org B
    out = run_recovery("--move-domain", "OrgA.test", "--org", "orga")
    assert "orga.test: now signs in to orga from orgb." in out
    assert SsoDomain.objects.get(domain="orga.test").organization_id == a["org"].id
    events = {e.organization_id: e.changes for e in AuditEvent.objects.filter(action="sso_settings.updated")}
    assert events[b["org"].id] == {"sign_in_domains": [["orga.test"], []], "via": "cli"}
    assert events[a["org"].id] == {"sign_in_domains": [[], ["orga.test"]], "via": "cli"}
    run_recovery("--move-domain", "orga.test", "--org", "orga")  # already there: no change, no event
    assert AuditEvent.objects.filter(action="sso_settings.updated").count() == 2
    out = run_recovery("--release-domain", "orga.test")
    assert "orga.test: released (was orga)." in out and not SsoDomain.objects.exists()
    assert AuditEvent.objects.filter(action="sso_settings.updated", organization=a["org"]).count() == 2
    assert "no organization had it" in run_recovery("--release-domain", "orga.test")
    for bad in (
        ["--move-domain", "orga.test"],
        ["--move-domain", "orga.test", "--all"],
        ["--release-domain", "orga.test", "--org", "orga"],
        ["--move-domain", "not a domain", "--org", "orga"],
        ["--move-domain", "orga.test", "--org", "nope"],
    ):
        with pytest.raises(CommandError):
            run_recovery(*bad)


def test_start_is_throttled_and_a_failed_discovery_fetch_is_cached(a, simulator, monkeypatch):
    from django.core.cache import cache
    from rest_framework.throttling import ScopedRateThrottle

    from core import counters

    activate(a["org"], "okta", okta_domain="down.simulated.invalid")
    sends = []
    real = oidc_discovery.send

    def counting(*args, **kwargs):
        sends.append(args[1])
        return real(*args, **kwargs)

    monkeypatch.setattr(oidc_discovery, "send", counting)
    for _ in range(3):
        refused = Client().get("/api/v1/auth/oidc/start?org=orga&next=/renewals")["Location"]
        assert refused == "/login?error=provider_error&org=orga&next=%2Frenewals"  # a retry goes back to the page
    assert len(sends) == 1  # the failure is answered from the cache for 30 s
    # Single sign-on has its own scope, `sso`, counted in the shared counters.
    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {**ScopedRateThrottle.THROTTLE_RATES, "sso": "2/min"})
    counters.reset()
    cache.clear()
    client = Client()
    assert client.get("/api/v1/auth/oidc/start?org=orga").status_code == 302
    assert client.get("/api/v1/auth/oidc/start?org=orga").status_code == 302
    assert client.get("/api/v1/auth/oidc/start?org=orga").status_code == 429


def test_a_fixed_discovery_document_is_used_once_the_failure_lapses(a, simulator, monkeypatch):
    """Only a checked document is cached: a provider that fixes its document is seen once the 30 s failure entry
    lapses, not after five minutes."""
    activate(a["org"], "okta")
    idp = oidc_simulator.IDPS["okta"]
    idp.doc_overrides = {"issuer": "https://elsewhere.simulated.invalid"}
    assert Client().get("/api/v1/auth/oidc/start?org=orga")["Location"].startswith("/login?error=provider_error")
    idp.doc_overrides = {}
    later = oidc_discovery.time.monotonic() + oidc_discovery.FAILURE_TTL + 1
    monkeypatch.setattr(oidc_discovery.time, "monotonic", lambda: later)
    assert sign_in(Client(), "editor@orga.test", query={"org": "orga"})["Location"] == "/renewals"


def test_unlink_names_the_removed_issuers(a, api, simulator):
    activate(a["org"], "okta")
    sign_in(Client(), "editor@orga.test", query={"org": "orga"})
    editor = a["users"]["editor"]
    assert api(a["users"]["admin"]).delete(f"users/{editor.id}/identity").status_code == 204
    event = AuditEvent.objects.get(action="user.identity_unlinked")
    assert event.changes == {"user": editor.id, "method": "sso", "issuers": [OKTA_ISSUER]}


def test_the_e2e_sso_seed_hook(a, simulator):
    """api/e2e_views.sso_seed (the Playwright Entra journey): routed only for the _e2e database, Admin only,
    idempotent, and it makes the seed organization's sign-in page offer Entra through the simulated tenant."""
    from django.contrib.auth import get_user_model
    from django.contrib.sessions.backends.db import SessionStore
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api import e2e_views
    from core.models import Membership

    assert Client().post("/api/v1/e2e/sso-seed").status_code == 404  # not routed outside the e2e server
    seed = get_user_model().objects.create_user(username="editor@example.test", email="editor@example.test")
    Membership.objects.create(user=seed, organization=a["org"], role="editor")

    def call(user):
        request = APIRequestFactory().post("/api/v1/e2e/sso-seed")
        request.session = SessionStore()
        force_authenticate(request, user=user)
        return e2e_views.sso_seed(request)

    assert call(a["users"]["editor"]).status_code == 403
    assert call(a["users"]["admin"]).data == {"org": "orga"}
    assert call(a["users"]["admin"]).data == {"org": "orga"}
    row = SsoSettings.objects.get(organization=a["org"])
    assert (row.status, row.provider, row.tenant, row.active_revision) == ("active", "entra", "simulated", 1)
    assert options("?org=orga")["sso"]["provider"] == "entra"
    assert sign_in(Client(), "editor@example.test", query={"org": "orga"})["Location"] == "/renewals"


def test_a_refusal_carries_the_organization_or_email_and_the_page_back(a, b, simulator):
    """/login?error=<code>&org=<slug>&next=<path>: the sign-in page shows the SSO button again and a retry goes back
    to the page, from start's refusals and the callback's alike."""
    activate(b["org"], "okta")
    SsoDomain.objects.create(organization=b["org"], domain="orgb.test", verified_at=timezone.now())
    unknown = Client().get("/api/v1/auth/oidc/start?org=orga&next=/renewals")  # org A has no SSO
    assert unknown["Location"] == "/login?error=not_configured&org=orga&next=%2Frenewals"
    by_org = sign_in(Client(), "stranger@orgb.test", next_path="/renewals", query={"org": "orgb"})
    assert by_org["Location"] == "/login?error=no_account&org=orgb&next=%2Frenewals"
    by_email = sign_in(Client(), "stranger@orgb.test", next_path="/renewals", query={"email": "editor@orgb.test"})
    assert by_email["Location"] == "/login?error=no_account&email=editor%40orgb.test&next=%2Frenewals"
    claims = sign_in_with(Client(), "editor@orgb.test", {"aud": "x"}, {"org": "orgb"}, next_path="/vendors")
    assert claims["Location"] == "/login?error=audience&org=orgb&next=%2Fvendors"


# ---- Sign-in domains need proof in cloud mode ------------------------------------------------------------------


@pytest.fixture
def verifying(settings):
    """SOFTWARDEN_SSO_DOMAIN_VERIFICATION, as in cloud mode; the simulated resolver answers only what a test publishes."""
    from core import dns_txt

    settings.SOFTWARDEN_SSO_DOMAIN_VERIFICATION = True
    assert dns_txt.simulated()  # tests/settings.py: no test resolves real DNS
    dns_txt.SIMULATED.clear()
    yield settings
    dns_txt.SIMULATED.clear()


def publish(state):
    from core import dns_txt

    dns_txt.SIMULATED[state["record_name"]] = ["v=spf1 -all", state["record_value"]]


def state_of(body, domain):
    return next(s for s in body["sign_in_domain_states"] if s["domain"] == domain)


def test_cloud_mode_verifies_domains_by_default():
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = "import django; django.setup(); from django.conf import settings as s; print(s.SOFTWARDEN_SSO_DOMAIN_VERIFICATION)"
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "softwarden.settings", "SOFTWARDEN_DEBUG": "1"}
    env.pop("SOFTWARDEN_SSO_DOMAIN_VERIFICATION", None)
    root = Path(__file__).resolve().parents[1]
    for deployment, expected in (("cloud", "True"), ("selfhosted", "False")):
        env["SOFTWARDEN_DEPLOYMENT"] = deployment
        out = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True)  # noqa: S603
        assert out.stdout.strip().splitlines()[-1] == expected, out.stderr


def test_an_unverified_domain_is_pending_and_finds_nobody(a, b, api, simulator, verifying):
    """A squatter's claim routes no sign-in, blocks nobody, and the claim itself says nothing about others."""
    activate(a["org"], "okta")
    activate(b["org"], "okta")
    squatter, owner = api(a["users"]["admin"]), api(b["users"]["admin"])
    body = squatter.patch("sso/settings", {"sign_in_domains": ["Victim-Corp.example"]}).json_body
    assert body["sign_in_domains"] == ["victim-corp.example"]
    pending = state_of(body, "victim-corp.example")
    assert pending["status"] == "pending" and pending["verified_at"] is None and pending["lapses_at"]
    assert pending["record_name"] == "_softwarden-challenge.victim-corp.example"
    assert pending["record_value"].startswith("softwarden-domain-verification=")
    assert options("?email=x@victim-corp.example")["sso"] is None
    assert sso.org_for_sign_in(email="x@victim-corp.example") is None
    # The real owner claims it too: accepted the same way, pending, with a challenge of its own.
    claimed = owner.patch("sso/settings", {"sign_in_domains": ["victim-corp.example"]}).json_body
    mine = state_of(claimed, "victim-corp.example")
    assert mine["status"] == "pending" and mine["record_value"] != pending["record_value"]
    assert options("?email=x@victim-corp.example")["sso"] is None
    assert sso.org_for_sign_in(email="x@victim-corp.example") is None
    # The owner publishes its record and verifies: now the sign-in page answers for the domain with its settings.
    publish(mine)
    verified = owner.post("sso/settings/domains/verify", {"domain": "victim-corp.example"})
    assert verified.status_code == 200 and verified.json_body["status"] == "verified"
    assert verified.json_body["record_name"] is None and verified.json_body["record_value"] is None
    assert options("?email=x@victim-corp.example")["sso"] is not None
    assert sso.org_for_sign_in(email="x@victim-corp.example") == b["org"]
    event = AuditEvent.objects.filter(organization=b["org"], action="sso_settings.updated").latest("created_at")
    assert event.changes == {"sign_in_domain_verified": "victim-corp.example"}
    again = owner.post("sso/settings/domains/verify", {"domain": "victim-corp.example"})
    assert again.status_code == 200 and again.json_body["status"] == "verified"  # idempotent, not audited again
    verified_events = AuditEvent.objects.filter(organization=b["org"], changes__has_key="sign_in_domain_verified")
    assert verified_events.count() == 1


def test_a_taken_domain_is_refused_in_the_words_of_an_unverifiable_one(a, b, api, verifying, monkeypatch):
    """No record, a DNS failure and "another organization holds it" are the same 400, so the refusal is no oracle
    for "is company X a customer"."""
    from core import dns_txt

    squatter, owner = api(a["users"]["admin"]), api(b["users"]["admin"])
    first = squatter.patch("sso/settings", {"sign_in_domains": ["contoso.example"]}).json_body
    claim = state_of(first, "contoso.example")
    unverifiable = squatter.post("sso/settings/domains/verify", {"domain": "contoso.example"})
    assert unverifiable.status_code == 400
    mine = state_of(owner.patch("sso/settings", {"sign_in_domains": ["contoso.example"]}).json_body, "contoso.example")
    publish(mine)
    assert owner.post("sso/settings/domains/verify", {"domain": "contoso.example"}).status_code == 200
    # The squatter now even publishes its own record: the domain is taken, refused in the same words.
    publish(claim)
    taken = squatter.post("sso/settings/domains/verify", {"domain": "contoso.example"})
    assert taken.status_code == 400 and taken.json_body == unverifiable.json_body
    assert taken.json_body["errors"] == {"domain": [sso.DOMAIN_UNCONFIRMED.format(domain="contoso.example")]}
    assert "another organization" not in json.dumps(taken.json_body).lower()

    def broken(name):
        raise dns_txt.DnsError("timeout")

    monkeypatch.setattr(dns_txt, "txt_records", broken)
    squatter.patch("sso/settings", {"sign_in_domains": ["contoso.example", "fabrikam.example"]})
    failed = squatter.post("sso/settings/domains/verify", {"domain": "fabrikam.example"})
    assert failed.status_code == 400
    assert failed.json_body["errors"] == {"domain": [sso.DOMAIN_UNCONFIRMED.format(domain="fabrikam.example")]}
    assert SsoDomain.objects.get(organization=a["org"], domain="contoso.example").verified_at is None
    # A domain this organization never claimed is a plain field error about its own list.
    other = squatter.post("sso/settings/domains/verify", {"domain": "unclaimed.example"})
    assert other.status_code == 400 and "sign_in_domains" in other.json_body["errors"]["domain"][0]


def test_a_draft_only_claim_lapses(a, b, verifying):
    from datetime import timedelta

    old = sso._new_domain(a["org"], "stale.example")
    fresh = sso._new_domain(b["org"], "stale.example")
    kept = SsoDomain.objects.create(organization=a["org"], domain="kept.example", verified_at=timezone.now())
    SsoDomain.objects.filter(pk__in=[old.pk, kept.pk]).update(created_at=timezone.now() - timedelta(days=15))
    assert sso.lapse_pending() == {"lapsed": 1}
    assert set(SsoDomain.objects.values_list("pk", flat=True)) == {fresh.pk, kept.pk}
    event = AuditEvent.objects.get(organization=a["org"], action="sso_settings.updated")
    assert event.actor is None and event.changes == {"sign_in_domains_lapsed": ["stale.example"], "via": "schedule"}
    assert sso.lapse_pending() == {"lapsed": 0}


def test_the_platform_console_moves_and_releases_a_domain(a, b, api, verifying):
    from core.models import PlatformAuditEvent
    from tests.test_platform import make_staff

    held = SsoDomain.objects.create(organization=a["org"], domain="bigcustomer.example", verified_at=timezone.now())
    sso._new_domain(b["org"], "bigcustomer.example")  # the real customer's pending claim
    console = api(make_staff())
    claims = console.get("platform/sign-in-domains?domain=BigCustomer.example").json_body
    assert [(c["organization"]["slug"], c["status"]) for c in claims] == [("orga", "verified"), ("orgb", "pending")]
    assert console.get("platform/sign-in-domains?domain=not a domain").status_code == 400
    body = {"domain": "bigcustomer.example", "organization_id": str(b["org"].id)}
    assert console.post("platform/sign-in-domains/move", body).status_code == 400  # a reason is required
    moved = console.post("platform/sign-in-domains/move", {**body, "reason": "Ticket 7: DNS proves orgb"})
    assert moved.status_code == 200 and moved.json_body["status"] == "verified"
    assert moved.json_body["organization"]["id"] == str(b["org"].id)
    assert not SsoDomain.objects.filter(pk=held.pk).exists()
    assert SsoDomain.objects.get(organization=b["org"]).verified_at is not None
    lost = AuditEvent.objects.get(organization=a["org"], action="sso_settings.updated")
    assert lost.changes["sign_in_domains"] == [["bigcustomer.example"], []] and lost.changes["via"] == "platform"
    gained = AuditEvent.objects.get(organization=b["org"], action="sso_settings.updated")
    assert gained.changes["sign_in_domain_verified"] == "bigcustomer.example"
    platform = PlatformAuditEvent.objects.get(action="sign_in_domain.moved")
    assert platform.organization_id == b["org"].id and platform.details["from"] == str(a["org"].id)
    released = console.post("platform/sign-in-domains/release", {"domain": "bigcustomer.example", "reason": "Asked"})
    assert released.status_code == 204 and not SsoDomain.objects.exists()
    assert PlatformAuditEvent.objects.get(action="sign_in_domain.released").organization_id == b["org"].id
    gone = console.post("platform/sign-in-domains/release", {"domain": "bigcustomer.example", "reason": "Again"})
    assert gone.status_code == 404
    assert api(a["users"]["admin"]).get("platform/sign-in-domains?domain=x.example").status_code == 403


def test_self_hosted_domains_apply_at_once_and_refuse_neutrally(a, b, api):
    """Without verification (self-hosted) a claim is verified at once and the first organization keeps it; the refusal
    names no organization."""
    first = api(a["users"]["admin"]).patch("sso/settings", {"sign_in_domains": ["shared.example"]}).json_body
    assert state_of(first, "shared.example")["status"] == "verified"
    refused = api(b["users"]["admin"]).patch("sso/settings", {"sign_in_domains": ["shared.example"]})
    assert refused.status_code == 400
    assert refused.json_body["errors"]["sign_in_domains"] == [sso.DOMAIN_UNAVAILABLE.format(domain="shared.example")]
    assert "another organization" not in json.dumps(refused.json_body).lower()


NAME = "_softwarden-challenge.example.com"


def dns_answer(query: bytes, *values: str, qid=None, flags=0x8180) -> bytes:
    """A resolver's answer to `query` (from core/dns_txt._query): its question echoed, one TXT record per value."""
    import struct

    question = query[12:-11]  # without the OPT record
    records = b""
    for value in values:
        data = bytes([len(value)]) + value.encode()
        records += b"\xc0\x0c" + struct.pack("!HHIH", 16, 1, 60, len(data)) + data
    ident = struct.unpack("!H", query[:2])[0] if qid is None else qid
    return struct.pack("!HHHHHH", ident, flags, 1, len(values), 0, 0) + question + records


def test_dns_answers_are_parsed_from_the_wire_format():
    """core/dns_txt.parse on a real-shaped answer: a compressed owner name, a CNAME on the way and a TXT record split
    in two strings; NXDOMAIN is no record; another query's answer, a server failure and a cut answer are errors."""
    import struct

    from core import dns_txt

    qid, query = dns_txt._query(NAME)
    question = query[12:-11]  # the question, without the OPT record
    header = struct.pack("!HHHHHH", qid, 0x8180, 1, 2, 0, 0)
    target = b"\x03txt\x07example\x03com\x00"
    cname = b"\xc0\x0c" + struct.pack("!HHIH", 5, 1, 60, len(target)) + target
    strings = b"\x05hello" + b"\x06-world"
    txt = b"\xc0\x0c" + struct.pack("!HHIH", 16, 1, 60, len(strings)) + strings
    assert dns_txt.parse(header + question + cname + txt, qid, NAME) == ["hello-world"]
    assert dns_txt.parse(struct.pack("!HHHHHH", qid, 0x8183, 1, 0, 0, 0) + question, qid, NAME) == []
    with pytest.raises(dns_txt.DnsError):
        dns_txt.parse(header + question + cname + txt, (qid + 1) % 65536, NAME)
    with pytest.raises(dns_txt.DnsError):
        dns_txt.parse(struct.pack("!HHHHHH", qid, 0x8182, 1, 0, 0, 0) + question, qid, NAME)  # SERVFAIL
    with pytest.raises(dns_txt.DnsError):
        dns_txt.parse(header + question + b"\xc0", qid, NAME)  # cut short


def test_a_dns_answer_must_answer_this_question():
    """Review follow-up: exactly one question, this name (any letter case) and type TXT; a TXT string running past its
    record is an error, not a short read."""
    import struct

    from core import dns_txt

    qid, query = dns_txt._query(NAME)
    good = dns_answer(query, "v=1")
    assert dns_txt.parse(good, qid, NAME.upper()) == ["v=1"]  # case-insensitive
    _, other = dns_txt._query("_softwarden-challenge.example.org")
    with pytest.raises(dns_txt.DnsError):
        dns_txt.parse(dns_answer(other, "v=1", qid=qid), qid, NAME)  # another name
    a_record = good.replace(struct.pack("!HH", 16, 1) + b"\xc0\x0c", struct.pack("!HH", 1, 1) + b"\xc0\x0c", 1)
    with pytest.raises(dns_txt.DnsError):
        dns_txt.parse(a_record, qid, NAME)  # QTYPE A
    for count in (0, 2):
        with pytest.raises(dns_txt.DnsError):
            dns_txt.parse(good[:4] + struct.pack("!H", count) + good[6:], qid, NAME)
    overrun = good[:-4] + b"\x09v=1"  # the string says 9 bytes; the record holds 3
    with pytest.raises(dns_txt.DnsError):
        dns_txt.parse(overrun, qid, NAME)


@pytest.mark.parametrize("value", ["10.0.0.2:dns", "10.0.0.2:0", "10.0.0.2:70000", "[::1]:x"])
def test_a_bad_resolver_port_is_a_failed_lookup_not_an_error(value, settings):
    from core import dns_txt

    settings.SOFTWARDEN_DNS_RESOLVER = value
    with pytest.raises(dns_txt.DnsError):
        dns_txt.txt_records(NAME)


class FakeResolver:
    """A UDP "resolver" on 127.0.0.1 for one query (never real DNS): `script(query, client, own, other)` sends what it
    wants from its own socket or from another one."""

    def __init__(self, script):
        import socket
        import threading

        self.own = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.own.bind(("127.0.0.1", 0))
        self.other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.other.bind(("127.0.0.1", 0))
        self.port = self.own.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, args=(script,), daemon=True)
        self.thread.start()

    def serve(self, script):
        self.own.settimeout(5)
        try:
            query, client = self.own.recvfrom(4096)
            script(query, client, self.own, self.other)
        except OSError:
            pass

    def close(self):
        self.thread.join(5)
        self.own.close()
        self.other.close()


def test_only_the_resolver_itself_can_answer(monkeypatch, settings):
    """Review follow-up: a datagram with the right ID from another address or port is ignored; the resolver's own
    answer is read."""
    from core import dns_txt

    def script(query, client, own, other):
        other.sendto(dns_answer(query, "softwarden-domain-verification=forged"), client)
        own.sendto(dns_answer(query, "softwarden-domain-verification=real"), client)

    resolver = FakeResolver(script)
    try:
        settings.SOFTWARDEN_DNS_RESOLVER = f"127.0.0.1:{resolver.port}"
        assert dns_txt.txt_records(NAME) == ["softwarden-domain-verification=real"]
    finally:
        resolver.close()


def test_stray_datagrams_cannot_stretch_the_timeout(monkeypatch, settings):
    """Review follow-up: one deadline per attempt; datagrams with another ID keep arriving, yet the lookup fails once
    TIMEOUT has passed instead of waiting TIMEOUT again after each."""
    import struct
    import time

    from core import dns_txt

    monkeypatch.setattr(dns_txt, "TIMEOUT", 0.6)

    def script(query, client, own, other):
        stray = dns_answer(query, "x", qid=(struct.unpack("!H", query[:2])[0] + 1) % 65536)
        for _ in range(12):
            own.sendto(stray, client)
            time.sleep(0.2)

    resolver = FakeResolver(script)
    try:
        settings.SOFTWARDEN_DNS_RESOLVER = f"127.0.0.1:{resolver.port}"
        started = time.monotonic()
        with pytest.raises(dns_txt.DnsError):
            dns_txt.txt_records(NAME)
        assert time.monotonic() - started < 1.5
    finally:
        resolver.close()
