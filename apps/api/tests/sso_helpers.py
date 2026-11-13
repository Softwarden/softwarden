"""Helpers for the single sign-on tests (tests/test_oidc.py, tests/test_sso.py): sign in through the simulated identity
providers (core/oidc_simulator.py) and set an organization's settings up directly."""

import urllib.parse
from typing import Any

import pytest
from django.test import Client

from core import credentials, oidc_discovery, oidc_simulator
from core.models import SsoSettings, SsoStatus

CLIENT_ID = "11111111-2222-4333-8444-555555555555"
SECRET = "synthetic-oidc-secret"  # noqa: S105  (a synthetic client secret for the simulator)
OKTA = {"okta_domain": oidc_simulator.OKTA_DOMAIN, "okta_authorization_server": "default"}


@pytest.fixture
def simulator(settings):
    """The simulated identity providers are allowed only in local development and the e2e server."""
    settings.DEBUG = True
    settings.SOFTWARDEN_BASE_URL = "http://testserver"


@pytest.fixture
def legacy_entra(simulator, monkeypatch):
    """The installation's legacy Entra settings, through the simulated tenant."""
    monkeypatch.setenv("SOFTWARDEN_OIDC_TENANT", "simulated")
    monkeypatch.setenv("SOFTWARDEN_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("SOFTWARDEN_OIDC_CLIENT_SECRET", SECRET)


def activate(org, provider="entra", client_id=CLIENT_ID, secret=SECRET, **values):
    """Give `org` active settings for one of the simulated providers, as an activated tested draft would be (set up
    directly; tests/test_sso.py drives the real draft / test / activate flow)."""
    presets: dict[str, dict[str, Any]] = {
        "entra": {"tenant": "simulated"},
        "okta": {"okta_domain": oidc_simulator.OKTA_DOMAIN, "okta_server": "default"},
        "google": {"hosted_domains": [f"{org.slug}.test"]},
        "oidc": {"discovery_url": oidc_simulator.IDPS["okta"].discovery_url},
    }
    defaults = presets[provider] | values
    discovery_url, issuer = oidc_discovery.urls(
        provider,
        defaults.get("tenant", ""),
        defaults.get("okta_domain", ""),
        defaults.get("okta_server", ""),
        defaults.get("discovery_url", ""),
    )
    row, _ = SsoSettings.objects.get_or_create(organization=org)
    for prefix in ("", "draft_"):
        setattr(row, f"{prefix}provider", provider)
        for key, value in defaults.items():
            setattr(row, f"{prefix}{key}", value)
        setattr(row, f"{prefix}discovery_url", discovery_url)
        setattr(row, f"{prefix}issuer", issuer)
        setattr(row, f"{prefix}client_id", client_id)
        credentials.slot_set(row, f"{prefix}secret", secret)
    row.status = SsoStatus.ACTIVE
    row.active_revision += 1
    row.draft_revision = row.draft_revision or 1
    row.save()
    return row


def authorize(client, query=None, next_path="/renewals"):
    """Start a sign-in; returns the simulator's authorize parameters."""
    params = dict(query or {})
    if next_path is not None:
        params["next"] = next_path
    start = client.get("/api/v1/auth/oidc/start" + ("?" + urllib.parse.urlencode(params) if params else ""))
    assert start.status_code == 302, start.content
    url = urllib.parse.urlsplit(start["Location"])
    assert url.path == "/api/v1/auth/oidc/simulator", start["Location"]
    found = dict(urllib.parse.parse_qsl(url.query))
    assert found["code_challenge_method"] == "S256" and found["scope"] == "openid profile email"
    return found


def callback(client, location):
    return client.get(location.replace("http://testserver", ""))


def sign_in(client, email, next_path="/renewals", query=None):
    """Start, sign in at the simulated provider, follow the callback. Returns the final redirect."""
    params = authorize(client, query, next_path)
    back = client.post("/api/v1/auth/oidc/simulator", {**params, "email": email, "action": "sign_in"})
    assert back.status_code == 302
    return callback(client, back["Location"])


def sign_in_with(client, email, overrides, query=None, next_path=None):
    """Sign in with an ID token whose claims are changed by `overrides` (the simulator's code, as its page would issue
    it)."""
    params = authorize(client, query, next_path)
    code = oidc_simulator.issue_code(params, email, idp=params["idp"], overrides=overrides)
    state = urllib.parse.quote(params["state"], safe="")
    return client.get(f"/api/v1/auth/oidc/callback?code={code}&state={state}")


def fresh():
    return Client()
