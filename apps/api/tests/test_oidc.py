"""Entra OIDC sign-in through the simulated identity provider, on the installation's legacy
SOFTWARDEN_OIDC_* settings (one organization) or an organization's own active settings (the per-organization
flow, the other providers and the settings API are in tests/test_sso.py)."""

import time
import urllib.parse
from unittest import mock

import jwt
import pytest
from django.test import Client

from core import oidc, oidc_discovery, oidc_simulator
from core.models import AuditEvent, UserIdentity
from tests.sso_helpers import CLIENT_ID, activate, sign_in

ENTRA_ISSUER = f"https://login.microsoftonline.com/{oidc_simulator.TENANT_ID}/v2.0"


@pytest.fixture(autouse=True)
def fresh_throttle(monkeypatch):
    """Start and callback share local sign-in's throttle (10 a minute, tested in tests/test_sso.py); these journeys sign
    in many times, so the rate is lifted and each test starts with an empty count."""
    from django.core.cache import cache
    from rest_framework.throttling import ScopedRateThrottle

    monkeypatch.setattr(
        ScopedRateThrottle, "THROTTLE_RATES", {**ScopedRateThrottle.THROTTLE_RATES, "login": "1000/min"}
    )
    cache.clear()


@pytest.fixture
def entra(legacy_entra):
    """The legacy installation-wide Entra settings of earlier versions."""


def test_options(a, api, entra, monkeypatch):
    # setup_pending: the organization exists, so first-run setup is closed (DRF layer C: folded in from GET setup).
    body = api().get("auth/options").json_body
    assert body["entra"] is True and body["setup_pending"] is False and body["password"] is True
    assert body["sso"] == {"provider": "entra", "label": "Microsoft Entra ID", "start_url": "/api/v1/auth/oidc/start"}
    monkeypatch.delenv("SOFTWARDEN_OIDC_CLIENT_SECRET")
    assert api().get("auth/options").json_body == {
        "entra": False,
        "setup_pending": False,
        "password": True,
        "signup_enabled": False,
        "password_reset_enabled": False,  # no email in the test settings
        "sso": None,
    }


def test_simulated_tenant_is_refused_outside_development(a, api, entra, settings):
    settings.DEBUG = False
    assert api().get("auth/options").json_body["entra"] is False
    assert Client().get("/api/v1/auth/oidc/simulator?redirect_uri=x").status_code == 404


def test_first_sign_in_matches_by_email_and_pins_the_identity(a, entra):
    editor = a["users"]["editor"]
    client = Client()
    done = sign_in(client, "EDITOR@orga.test")
    assert done.status_code == 302 and done["Location"] == "/renewals"
    me = client.get("/api/v1/auth/me").json()
    assert me["user"]["id"] == editor.id and me["role"] == "editor"  # the existing role
    identity = UserIdentity.objects.get(user=editor)
    # Pinned on (issuer, subject); for Entra that is the tenant's issuer and the object ID, as before.
    assert (identity.issuer, identity.subject) == (ENTRA_ISSUER, oidc_simulator.object_id("editor@orga.test"))
    assert AuditEvent.objects.filter(action="user.login", changes__method="entra").count() == 1
    assert AuditEvent.objects.filter(action="user.identity_linked").count() == 1
    # Later sign-ins use the pinned identity, even if the account's email changes.
    editor.email = editor.username = "renamed@orga.test"
    editor.save()
    assert sign_in(Client(), "editor@orga.test")["Location"] == "/renewals"


def test_unknown_identity_is_refused_and_no_user_is_created(a, entra):
    from django.contrib.auth import get_user_model

    before = get_user_model().objects.count()
    done = sign_in(Client(), "stranger@elsewhere.test")
    assert done["Location"] == "/login?error=no_account&next=%2Frenewals"  # trying again goes where they were going
    assert get_user_model().objects.count() == before
    assert not UserIdentity.objects.exists()


def test_inactive_user_is_refused_and_audited(a, entra):
    viewer = a["users"]["viewer"]
    viewer.is_active = False
    viewer.save()
    assert sign_in(Client(), "viewer@orga.test", next_path="/")["Location"] == "/login?error=inactive"
    failed = AuditEvent.objects.get(action="user.login_failed")
    assert failed.changes == {"user": viewer.id, "method": "entra"}


def test_state_and_next_are_checked(a, entra):
    client = Client()
    client.get("/api/v1/auth/oidc/start")
    assert client.get("/api/v1/auth/oidc/callback?code=x&state=forged")["Location"] == "/login?error=expired"
    # Off-site targets are dropped: the person lands where no page was asked for, the dashboard.
    assert sign_in(Client(), "admin@orga.test", next_path="//evil.example/x")["Location"] == "/dashboard"
    assert sign_in(Client(), "admin@orga.test", next_path="https://evil.example/x")["Location"] == "/dashboard"
    # Control characters and backslashes anywhere are refused: browsers would read these as "//evil.example".
    for sneaky in (
        "/\t/evil.example",
        "/\n/evil.example",
        "/\\evil.example",
        "/x\\..\\/evil.example",
        "/\x00/evil.example",
    ):
        assert sign_in(Client(), "admin@orga.test", next_path=sneaky)["Location"] == "/dashboard", sneaky


def test_every_role_lands_on_the_dashboard_unless_a_page_was_asked_for(a, entra):
    # Owner decision 2026-11-05: no page asked for (no next, or "/") means the dashboard, for every role.
    for email in ("admin@orga.test", "editor@orga.test", "viewer@orga.test"):
        assert sign_in(Client(), email, next_path=None)["Location"] == "/dashboard"
        assert sign_in(Client(), email, next_path="/")["Location"] == "/dashboard"
    # A shared link still wins, query string included.
    assert (
        sign_in(Client(), "viewer@orga.test", next_path="/renewals?status=open")["Location"] == "/renewals?status=open"
    )


def test_cancel(a, entra):
    client = Client()
    start = client.get("/api/v1/auth/oidc/start")
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(start["Location"]).query))
    back = client.post("/api/v1/auth/oidc/simulator", {**params, "action": "cancel"})
    assert client.get(back["Location"].replace("http://testserver", ""))["Location"] == "/login?error=cancelled"


def test_tokens_are_validated(a, entra):
    """A token for another audience, tenant or nonce, expired, without the object ID, or signed by another key, is
    refused (each with its own code)."""
    idp = oidc_simulator.IDPS["entra"]
    now = int(time.time())
    good = {
        "iss": idp.issuer,
        "aud": CLIENT_ID,
        "sub": "x",
        "oid": "x",
        "tid": oidc_simulator.TENANT_ID,
        "nonce": "n",
        "iat": now,
        "exp": now + 60,
    }

    def finish_with(claims, key=None, kid=None):
        token = jwt.encode(claims, key or idp.keys[-1][1], algorithm="RS256", headers={"kid": kid or idp.keys[-1][0]})
        session: dict = {}
        oidc.start(session, a["org"], "/")
        session["oidc"] |= {"state": "s", "nonce": "n"}
        with mock.patch.object(oidc_discovery, "exchange", lambda *args: token):
            return oidc.finish(session, {"state": "s", "code": "c"})

    assert finish_with(good)[0]["oid"] == "x"
    from cryptography.hazmat.primitives.asymmetric import rsa

    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for bad, code in (
        ({**good, "aud": "someone-else"}, "audience"),
        ({**good, "tid": "another-tenant"}, "tenant"),
        ({**good, "nonce": "replayed"}, "nonce"),
        ({**good, "exp": now - 600}, "token_expired"),
        ({k: v for k, v in good.items() if k != "oid"}, "missing_claim"),
        ({**good, "iss": "https://login.microsoftonline.com/another/v2.0"}, "issuer"),
    ):
        with pytest.raises(oidc.SignInRefused) as refused:
            finish_with(bad)
        assert refused.value.code == code, bad
    with pytest.raises(oidc.SignInRefused) as refused:
        finish_with(good, key=other_key)
    assert refused.value.code == "signature"


def test_local_sign_in_still_works(a, api, entra):
    r = Client().post(
        "/api/v1/auth/login",
        {"email": "admin@orga.test", "password": "correct-horse-battery"},
        content_type="application/json",
    )
    assert r.status_code == 200


def test_admin_unlinks(a, api, entra):
    sign_in(Client(), "editor@orga.test")
    editor = a["users"]["editor"]
    users = api(a["users"]["admin"]).get("users").json_body
    assert next(u for u in users if u["id"] == editor.id)["entra_linked"] is True
    assert api(a["users"]["editor"]).delete(f"users/{editor.id}/identity").status_code == 403
    assert api(a["users"]["admin"]).delete(f"users/{editor.id}/identity").status_code == 204
    assert not UserIdentity.objects.filter(user=editor).exists()
    assert AuditEvent.objects.filter(action="user.identity_unlinked").exists()


def test_unlink_keeps_the_identities_when_its_audit_event_fails(a, api, entra, monkeypatch):
    # The delete and its audit event are one transaction; a failure writing the event leaves the link in place.
    sign_in(Client(), "editor@orga.test")
    editor = a["users"]["editor"]
    membership = a["org"].memberships.get(user=editor)

    def broken(*args, **kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(oidc.services, "audit", broken)
    with pytest.raises(RuntimeError):
        oidc.unlink(a["org"], a["users"]["admin"], membership)
    assert UserIdentity.objects.filter(user=editor).exists()
    assert not AuditEvent.objects.filter(action="user.identity_unlinked").exists()


def test_cross_tenant_unlink(a, b, api, simulator):
    # Two organizations: the legacy settings serve one organization only, so org A has settings of its own.
    activate(a["org"])
    sign_in(Client(), "editor@orga.test", query={"org": a["org"].slug})
    assert UserIdentity.objects.filter(user=a["users"]["editor"]).exists()
    assert api(b["users"]["admin"]).delete(f"users/{a['users']['editor'].id}/identity").status_code == 404
