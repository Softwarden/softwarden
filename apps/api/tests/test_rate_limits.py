"""Rate limits per API token and per user (core/throttling.py, docs/API.md).

Each token has its own bucket and each session user another, so one script exhausting its token never slows the same
person's other token or their browser. Reads and writes count apart, writes stricter. A refused request is the 429
rate-limited problem with Retry-After. The scoped throttles (search, sign-in) keep working on top."""

import json
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.test import Client

from api import tokens
from catalog import services
from core import counters, throttling
from core.models import Membership


@pytest.fixture(autouse=True)
def empty_cache():
    cache.clear()  # throttles count in the process cache
    yield
    cache.clear()


@pytest.fixture
def small(settings):
    settings.API_READ_RATE = "3/min"
    settings.API_WRITE_RATE = "2/min"
    return settings


def token_client(f, role="editor", read_only=False):
    owner = Membership.objects.get(organization=f["org"], user=f["users"][role])
    expires = services.org_today(f["org"]) + timedelta(days=30)
    _, secret = tokens.create_token(f["org"], f["users"]["admin"], owner, "Script", read_only, expires)
    return Client(HTTP_AUTHORIZATION=f"Bearer {secret}")


def session_client(f, role="editor"):
    client = Client()
    client.force_login(f["users"][role])
    return client


def post_vendor(client, name):
    return client.post("/api/v1/vendors", json.dumps({"name": name}), content_type="application/json")


def assert_limited(response):
    assert response.status_code == 429, response.status_code
    assert response["Content-Type"] == "application/problem+json"
    body = response.json()
    assert body["type"] == "/problems/rate-limited" and body["status"] == 429 and body["title"] == "Too many requests"
    assert int(response["Retry-After"]) >= 1
    return body


def test_the_defaults_are_set_and_writes_are_stricter(settings):
    parse = throttling.ApiRateThrottle().parse_rate
    (reads, read_seconds), (writes, write_seconds) = parse(settings.API_READ_RATE), parse(settings.API_WRITE_RATE)
    assert reads and read_seconds and writes and write_seconds
    assert reads / read_seconds > writes / write_seconds > 0
    assert "core.throttling.ApiRateThrottle" in settings.REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"]


def test_a_token_exhausting_its_reads_leaves_the_users_other_token_and_session_alone(a, small):
    first, second, browser = token_client(a), token_client(a), session_client(a)
    for _ in range(3):
        assert first.get("/api/v1/vendors").status_code == 200
    assert_limited(first.get("/api/v1/vendors"))
    assert_limited(first.get(f"/api/v1/applications/{a['app'].id}"))  # one bucket for every read of the token
    for _ in range(3):
        assert second.get("/api/v1/vendors").status_code == 200  # the same user's other token
        assert browser.get("/api/v1/vendors").status_code == 200  # and their session
    assert_limited(second.get("/api/v1/vendors"))
    assert_limited(browser.get("/api/v1/vendors"))


def test_a_session_exhausting_its_bucket_leaves_the_users_tokens_and_other_users_alone(a, small):
    browser, script, colleague = session_client(a), token_client(a), session_client(a, "viewer")
    for _ in range(3):
        assert browser.get("/api/v1/teams").status_code == 200
    assert_limited(browser.get("/api/v1/teams"))
    assert script.get("/api/v1/teams").status_code == 200
    assert colleague.get("/api/v1/teams").status_code == 200
    browser.logout()
    browser.force_login(a["users"]["editor"])  # a new session of the same user shares the user's bucket
    assert_limited(browser.get("/api/v1/teams"))


def test_writes_have_their_own_stricter_bucket(a, small):
    script = token_client(a)
    for _ in range(3):
        assert script.get("/api/v1/vendors").status_code == 200
    assert_limited(script.get("/api/v1/vendors"))
    assert post_vendor(script, "First").status_code == 201  # reads exhausted, writes still counted apart
    assert post_vendor(script, "Second").status_code == 201
    body = assert_limited(post_vendor(script, "Third"))
    assert "throttled" in body["detail"]
    from catalog.models import Vendor

    assert not Vendor.objects.filter(name="Third").exists()
    assert post_vendor(token_client(a), "Other token").status_code == 201


def test_a_read_only_tokens_refused_writes_stay_403_past_the_write_limit(a, small):
    """The read-only refusal comes first (core/drf.RolePermission, before DRF checks throttles), so refused writes
    aren't counted: past the write limit they are still a 403, never a 429."""
    reader = token_client(a, read_only=True)
    for _ in range(4):
        assert post_vendor(reader, "Nope").status_code == 403


def test_an_empty_rate_means_no_limit(a, settings):
    settings.API_READ_RATE = ""
    script = token_client(a)
    for _ in range(20):
        assert script.get("/api/v1/teams").status_code == 200


def test_signed_out_requests_have_no_api_bucket(a, small, settings):
    anonymous = Client()
    for _ in range(6):
        assert anonymous.get("/api/v1/auth/options").status_code == 200
        assert anonymous.get("/api/v1/vendors").status_code == 403  # not signed in, never 429


def test_the_search_throttle_still_applies_on_top(a, settings):
    settings.API_READ_RATE = "100/min"
    settings.SEARCH_RATE = "2/min"
    script = token_client(a)
    for path in ("/api/v1/search?q=app", "/api/v1/composite/search?q=app"):
        counters.reset()  # the two search routes share one bucket (counted in the database)
        assert script.get(path).status_code == 200
        assert script.get(path).status_code == 200
        assert_limited(script.get(path))
    assert script.get("/api/v1/teams").status_code == 200  # other reads are within the API bucket


def test_search_counts_against_the_api_bucket_too(a, settings):
    settings.API_READ_RATE = "2/min"
    settings.SEARCH_RATE = "100/min"
    script = token_client(a)
    assert script.get("/api/v1/search?q=app").status_code == 200
    assert script.get("/api/v1/teams").status_code == 200
    assert_limited(script.get("/api/v1/search?q=app"))


def test_the_sign_in_throttle_still_applies(db, settings):
    cache.clear()
    for _ in range(10):
        Client().post("/api/v1/auth/login", json.dumps({"email": "x@y.test", "password": "nope"}), "application/json")
    response = Client().post(
        "/api/v1/auth/login", json.dumps({"email": "x@y.test", "password": "nope"}), "application/json"
    )
    assert_limited(response)


def bad_sign_in(client, email=None, **extra):
    """A wrong password. Each call names another unknown email unless told, so only the address's limit is in play
    (failures for one email are slowed on their own, tests/test_shared_limits.py)."""
    global _tries
    _tries += 1
    body = json.dumps({"email": email or f"x{_tries}@y.test", "password": "nope"})
    return client.post("/api/v1/auth/login", body, "application/json", **extra)


_tries = 0


def test_a_forged_forwarded_address_does_not_dodge_the_sign_in_limit(db, settings):
    """From an untrusted peer X-Forwarded-For is dropped (core/middleware.py), so every guess counts against the peer's
    own address whatever header it sends (SECURITY.md, rate limits)."""
    settings.TRUSTED_PROXIES = []
    client = Client()
    for n in range(10):
        bad_sign_in(client, HTTP_X_FORWARDED_FOR=f"198.51.100.{n}")
    assert_limited(bad_sign_in(client, HTTP_X_FORWARDED_FOR="198.51.100.99"))


def test_behind_the_trusted_proxy_each_client_address_has_its_own_sign_in_limit(db, settings):
    settings.TRUSTED_PROXIES = ["127.0.0.1"]
    client = Client()
    for _ in range(10):
        bad_sign_in(client, HTTP_X_FORWARDED_FOR="198.51.100.1")
    assert_limited(bad_sign_in(client, HTTP_X_FORWARDED_FOR="198.51.100.1"))
    assert bad_sign_in(client, HTTP_X_FORWARDED_FOR="198.51.100.2").status_code == 403  # a wrong password, not 429


def test_every_sign_in_route_has_its_scope_in_the_shared_counters():
    """Routes that check a password share the per-address `login` bucket, so moving between them buys no extra
    guesses. Single sign-on (`sso`) and invitation links (`invitation`) have scopes of their own: neither
    takes a password (an invitation token is 256 bits), and sharing let ten wrong passwords from an office address
    block its single sign-on. Every one counts in the database (core/throttling.ScopedThrottle)."""
    from catalog import access_viewsets, signup_views, views
    from core import oidc_views
    from core.throttling import ScopedThrottle
    from softwarden import ee

    routes = {
        views.login_view: "login",
        signup_views.signup_verify: "login",
        oidc_views.start: "sso",
        oidc_views.callback: "sso",
        access_viewsets.invitation_preview: "invitation",
        access_viewsets.invitation_accept: "invitation",
    }
    if ee.INSTALLED:  # the optional package's SAML sign-in
        from ee.saml import views as saml_views

        routes |= {saml_views.start: "sso", saml_views.acs: "sso"}
    for route, scope in routes.items():
        cls = route.cls
        assert getattr(cls, "throttle_scope", None) == scope, route
        assert ScopedThrottle in cls.throttle_classes, route


def test_the_bucket_is_the_token_or_the_user():
    class Token:
        id = "abc"
        token_hash = "h"

    class User:
        pk = 7
        is_authenticated = True

    class Request:
        def __init__(self, auth, user):
            self.auth, self.user = auth, user

    class Anonymous:
        is_authenticated = False

    assert throttling.bucket(Request(Token(), User())) == "token:abc"
    assert throttling.bucket(Request(None, User())) == "user:7"
    assert throttling.bucket(Request(None, Anonymous())) is None


def test_rate_limits_are_documented_on_every_signed_in_operation(a, api):
    doc = json.loads(api(a["users"]["viewer"]).client.get("/api/v1/schema?format=json").content)
    signed_in = [
        op
        for operations in doc["paths"].values()
        for method, op in operations.items()
        if method in ("get", "post", "patch", "put", "delete") and any(op.get("security") or [])
    ]
    assert len(signed_in) > 300
    for op in signed_in:
        limited = op["responses"]["429"]
        assert "Retry-After" in limited["headers"], op["operationId"]
        assert limited["content"]["application/problem+json"]["schema"]["$ref"].endswith("/Problem")
        assert "401" in op["responses"], op["operationId"]
