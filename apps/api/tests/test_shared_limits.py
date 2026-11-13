"""Every rate limit counts in the shared PostgreSQL
counters (core/counters.py), so it holds whichever process serves the request; failed sign-ins are also counted per
account, from any address, with an exponential delay (core/signin_limits.py); refused bearer tokens are limited per
address; single sign-on and invitations have their own scopes.

"Two processes" here are two separate per-process caches (LocMemCache, Django's default, which each gunicorn worker
has its own of), swapped in turn under every module that ever kept a limit in the cache. A limit counted per process
lets twice the requests through; a shared one refuses request n+1 whichever "process" serves it."""

from __future__ import annotations

import itertools
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from django.core.cache.backends.locmem import LocMemCache
from django.db import connection
from django.test import Client

PASSWORD = "correct-horse-battery"
START = 1_800_000_000 - (1_800_000_000 % 3600)  # the start of an hour, so every window here starts with the test


class TwoProcesses:
    """Serve each request from the next of two per-process caches in turn, or from the one named."""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.caches = [LocMemCache(f"process-{n}", {}) for n in (1, 2)]
        self.turn = itertools.cycle((0, 1))

    def use(self, n: int) -> None:
        from rest_framework.throttling import SimpleRateThrottle

        from core import bootstrap
        from softwarden import ee

        cache = self.caches[n]
        self.monkeypatch.setattr(SimpleRateThrottle, "cache", cache)
        self.monkeypatch.setattr(bootstrap, "cache", cache, raising=False)
        if ee.INSTALLED:  # the optional package's SCIM tokens
            from ee.scim import tokens

            self.monkeypatch.setattr(tokens, "cache", cache, raising=False)

    def next(self) -> int:
        n = next(self.turn)
        self.use(n)
        return n


@pytest.fixture
def processes(monkeypatch):
    return TwoProcesses(monkeypatch)


@pytest.fixture
def clock(monkeypatch):
    """The counters' clock, held at the start of an hour; tests move it with now[0] += seconds."""
    now = [float(START)]
    try:
        from core import counters
    except ImportError:  # an older release had no shared counters to hold still
        return now
    monkeypatch.setattr(counters, "clock", lambda: now[0])
    return now


def sign_in(email, password="wrong-password-here", address="192.0.2.10", **extra):  # noqa: S107  (synthetic)
    return Client().post(
        "/api/v1/auth/login",
        json.dumps({"email": email, "password": password}),
        content_type="application/json",
        REMOTE_ADDR=address,
        **extra,
    )


def assert_limited(response):
    assert response.status_code == 429, (response.status_code, response.content[:200])
    assert response.json()["type"] == "/problems/rate-limited"
    assert int(response["Retry-After"]) >= 1
    return response


# ---- request n+1 is refused whichever process serves it --------------------------------------------------------------


def test_the_sign_in_rate_per_address_holds_across_processes(a, processes, clock):
    """10 sign-ins a minute per address (LOGIN_THROTTLE_RATE). Each attempt names another unknown email, so only the
    address's limit is in play. Per process, the 11th and 12th would each be a process's 6th."""
    for n in range(10):
        processes.next()
        assert sign_in(f"nobody{n}@x.test").status_code == 403
    for n in (0, 1):
        processes.use(n)
        assert_limited(sign_in("nobody-else@x.test"))
    processes.use(0)
    assert sign_in("nobody-else@x.test", address="192.0.2.11").status_code == 403  # another address is untouched


def test_the_api_rate_holds_across_processes(a, api, processes, clock, settings):
    settings.API_READ_RATE = "3/min"
    client = Client()
    client.force_login(a["users"]["editor"])
    for _ in range(3):
        processes.next()
        assert client.get("/api/v1/vendors").status_code == 200
    for n in (0, 1):
        processes.use(n)
        assert_limited(client.get("/api/v1/vendors"))


def test_the_organization_rate_holds_across_processes(a, processes, clock, settings):
    from tests.test_limits import set_limit

    settings.API_READ_RATE = ""
    set_limit(a["org"], "api_rate_read", 2)
    editor, viewer = Client(), Client()
    editor.force_login(a["users"]["editor"])
    viewer.force_login(a["users"]["viewer"])
    processes.next()
    assert editor.get("/api/v1/vendors").status_code == 200
    processes.next()
    assert viewer.get("/api/v1/vendors").status_code == 200  # one bucket for the whole organization
    for n in (0, 1):
        processes.use(n)
        assert_limited(editor.get("/api/v1/vendors"))


def test_the_setup_token_lockout_holds_across_processes(fresh, processes, clock):
    from core import bootstrap
    from tests.test_setup import FORM, browser, post

    token = bootstrap.prepare_token()
    client = browser()
    for _ in range(bootstrap.FAILURE_LIMIT):
        processes.next()
        assert post(client, {**FORM, "token": "y" * 43}).status_code == 403
    for n in (0, 1):
        processes.use(n)
        blocked = post(client, {**FORM, "token": token})  # even the right token
        assert blocked.status_code == 429 and 1 <= int(blocked["Retry-After"]) <= bootstrap.FAILURE_WINDOW


def test_the_signup_rate_holds_across_processes(db, processes, clock, settings):
    settings.SOFTWARDEN_SIGNUP_ENABLED = True
    seen = []
    for n in range(12):
        processes.next()
        response = Client().post(
            "/api/v1/auth/signup", json.dumps({"email": f"x{n}@nowhere.test"}), content_type="application/json"
        )
        seen.append(response.status_code)
    assert 429 not in seen[:10] and seen[10:] == [429, 429], seen


# ---- one account, many addresses -------------------------------------------------------------------------------------


def test_failures_for_one_account_from_many_addresses_are_slowed(a, processes, clock):
    """Five free failures in a window, then each attempt waits 1, 2, 4 ... seconds after the last failure, from any
    address and whichever process serves it; the right password inside the delay is refused too."""
    from core import signin_limits

    email = "editor@orga.test"
    for n in range(signin_limits.ACCOUNT_FREE):
        processes.next()
        assert sign_in(email, address=f"198.51.100.{n}").status_code == 403
    processes.next()
    limited = assert_limited(sign_in(email, PASSWORD, address="198.51.100.200"))
    assert int(limited["Retry-After"]) == 1
    clock[0] += 1
    processes.next()
    assert sign_in(email, address="198.51.100.201").status_code == 403  # the 6th failure
    processes.next()
    assert int(assert_limited(sign_in(email, address="198.51.100.202"))["Retry-After"]) == 2
    clock[0] += 2
    processes.next()
    assert sign_in(email, PASSWORD, address="198.51.100.203").status_code == 200
    # Signing in forgets the failures: the next mistake is free again.
    assert sign_in(email, address="198.51.100.204").status_code == 403
    assert sign_in(email, address="198.51.100.205").status_code == 403


# Starting on an hour, and 20 seconds before a 15-minute and before a 5-minute clock boundary: the account's count is
# not tied to clock-aligned windows, so crossing one resets nothing.
@pytest.mark.parametrize("offset", [0, 900 - 20, 300 - 20])
def test_the_eleventh_failed_sign_in_for_one_account_in_a_minute_is_refused(a, processes, clock, offset):
    """The per-account sign-in limit, for any account (platform staff included): ten failures fit in a minute only by waiting out
    every delay (1 + 2 + 4 + 8 + 16 = 31 seconds), and the eleventh must then wait 32 more, so inside the minute it is
    refused whichever process serves it and from whichever address."""
    from catalog import platform_admin

    staff = a["users"]["admin"]
    platform_admin.grant_staff(staff)
    clock[0] = start = START + offset
    for n in range(10):
        if n >= 5:
            clock[0] += 2 ** (n - 5)  # wait out the delay before this attempt: 1, 2, 4, 8, 16 seconds
        processes.next()
        response = sign_in(staff.email, address=f"203.0.113.{n}")
        assert response.status_code == 403, (n, response.status_code)
    assert clock[0] - start == 31
    clock[0] = start + 59
    for n in (0, 1):
        processes.use(n)
        assert_limited(sign_in(staff.email, PASSWORD, address=f"203.0.113.{100 + n}"))


def test_guessing_across_a_clock_boundary_gains_nothing(a, clock):
    """A probe: starting 20 seconds before a 15-minute boundary and retrying as soon as Retry-After
    allows, 14 attempts take far longer than a minute (no reset at the boundary): 5 free, then 1 + 2 + ... + 256."""
    clock[0] = start = START + 900 - 20
    statuses = []
    for n in range(14):
        response = sign_in("editor@orga.test", address=f"203.0.113.{n}")
        if response.status_code == 429:
            clock[0] += int(response["Retry-After"])
            response = sign_in("editor@orga.test", address=f"203.0.113.{n}")
        statuses.append((clock[0] - start, response.status_code))
    assert all(status == 403 for _, status in statuses)
    assert statuses[-1][0] >= 2**9 - 1  # 1 + 2 + ... + 256 seconds of waiting
    assert len([at for at, _ in statuses if at < 60]) <= 10


def test_the_count_lapses_after_fifteen_quiet_minutes_and_the_operator_can_clear_one(a, clock):
    import io

    from django.core.management import call_command
    from django.core.management.base import CommandError

    from core import signin_limits
    from core.models import PlatformAuditEvent

    for n in range(signin_limits.ACCOUNT_FREE):
        sign_in("editor@orga.test", address=f"203.0.113.{n}")
    assert_limited(sign_in("editor@orga.test", PASSWORD))
    clock[0] += signin_limits.ACCOUNT_IDLE  # fifteen minutes with no attempt
    assert sign_in("editor@orga.test", PASSWORD).status_code == 200
    for n in range(signin_limits.ACCOUNT_FREE):
        sign_in("editor@orga.test", address=f"203.0.113.{n}")
    assert_limited(sign_in("editor@orga.test", PASSWORD))
    with pytest.raises(CommandError, match="reason"):
        call_command("signin_limits", "forget", "editor@orga.test")
    out = io.StringIO()
    call_command("signin_limits", "forget", "Editor@Orga.test", "--reason", "Owner locked out; called back", stdout=out)
    assert "Cleared" in out.getvalue()
    assert sign_in("editor@orga.test", PASSWORD).status_code == 200
    event = PlatformAuditEvent.objects.get(action="signin.failures_forgotten")
    assert event.actor is None and event.details["via"] == "cli" and event.details["had_failures"] is True
    assert event.details["user"] == a["users"]["editor"].pk
    # One account only: another's count is untouched.
    for n in range(signin_limits.ACCOUNT_FREE):
        sign_in("viewer@orga.test", address=f"203.0.113.{n}")
    call_command("signin_limits", "forget", "editor@orga.test", "--reason", "Again", stdout=io.StringIO())
    assert_limited(sign_in("viewer@orga.test", PASSWORD))


@pytest.mark.django_db(transaction=True)
def test_parallel_attempts_for_one_account_are_counted_before_any_is_checked(monkeypatch):
    """A race: twenty wrong passwords for one account at once, each slow to check. Counting before
    checking, under the counter row's lock, lets five through and refuses the rest (it used to let all twenty)."""
    import time

    from catalog import views
    from core import counters, signin_limits
    from tests.conftest import make_org

    make_org("orga")
    real = views.authenticate

    def slow(*args, **kwargs):
        time.sleep(0.3)  # every request has been through the account check before any finishes
        return real(*args, **kwargs)

    monkeypatch.setattr(views, "authenticate", slow)
    results: list[int] = []
    start = threading.Barrier(20, timeout=30)

    def go(i):
        try:
            start.wait()
            results.append(sign_in("editor@orga.test", address=f"198.51.100.{i}").status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=go, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        assert sorted(results) == [403] * signin_limits.ACCOUNT_FREE + [429] * (20 - signin_limits.ACCOUNT_FREE)
    finally:
        counters.reset()


# ---- refused bearer tokens -------------------------------------------------------------------------------------------


def test_the_31st_invalid_token_in_a_minute_from_one_address_is_429(a, processes, clock):
    from tests.test_rate_limits import token_client

    valid = token_client(a)
    for n in range(30):
        processes.next()
        response = Client().get(
            "/api/v1/vendors", HTTP_AUTHORIZATION=f"Bearer sw_not-a-token-{n:037d}", REMOTE_ADDR="192.0.2.50"
        )
        assert response.status_code == 401, (n, response.status_code)
    for n in (0, 1):
        processes.use(n)
        refused = Client().get("/api/v1/vendors", HTTP_AUTHORIZATION="Bearer sw_" + "z" * 40, REMOTE_ADDR="192.0.2.50")
        assert_limited(refused)
    # A valid token from the same address still works (an office or the MCP server shares it with the guesser), and
    # another address's bad token is still a plain 401.
    assert valid.get("/api/v1/vendors", REMOTE_ADDR="192.0.2.50").status_code == 200
    other = Client().get("/api/v1/vendors", HTTP_AUTHORIZATION="Bearer sw_" + "y" * 40, REMOTE_ADDR="192.0.2.51")
    assert other.status_code == 401
    clock[0] += 60  # the next window
    again = Client().get("/api/v1/vendors", HTTP_AUTHORIZATION="Bearer sw_" + "z" * 40, REMOTE_ADDR="192.0.2.50")
    assert again.status_code == 401


# ---- scopes of their own ---------------------------------------------------------------------------------------------


def test_sso_and_invitations_have_scopes_of_their_own(a, clock):
    """Ten wrong passwords from an address used to spend the throttle that single sign-on callbacks and invitation
    links share; now each has its own."""
    from core import oidc_views
    from softwarden import ee

    for n in range(10):
        sign_in(f"nobody{n}@x.test")
    assert_limited(sign_in("nobody@x.test"))
    preview = Client().post(
        "/api/v1/auth/invitations/preview",
        json.dumps({"token": "z" * 43}),
        content_type="application/json",
        REMOTE_ADDR="192.0.2.10",
    )
    assert preview.status_code == 404  # unknown invitation, not 429
    views = [oidc_views.start, oidc_views.callback]
    if ee.INSTALLED:  # the optional package's SAML sign-in
        from ee.saml import views as saml_views

        views += [saml_views.start, saml_views.acs]
    scopes = {getattr(view.cls, "throttle_scope", None) for view in views}
    assert scopes == {"sso"}


# ---- No lost updates; no made-up addresses --------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_concurrent_counts_are_never_lost():
    """The cache's add-then-incr could lose a count under a concurrent burst. One upsert can't: 4 threads x 25
    hits on their own connections end at exactly 100."""
    from core import counters

    errors = []

    def burst():
        try:
            for _ in range(25):
                counters.hit("test-burst", "one key", 3600, now=START)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=burst) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert counters.peek("test-burst", "one key", 3600, now=START).count == 100
    counters.reset()


def test_made_up_forwarded_addresses_share_the_proxy_written_bucket(a, clock, settings):
    """Behind a proxy that keeps a client's X-Forwarded-For (Caddy with `trusted_proxies` but not
    `trusted_proxies_strict` appends to it), only the last entry, the one the proxy wrote, is the client's address.
    A client writing a new address on the left each time still spends one bucket."""
    settings.TRUSTED_PROXIES = ["127.0.0.1"]
    for n in range(10):
        response = sign_in(f"nobody{n}@x.test", address="127.0.0.1", HTTP_X_FORWARDED_FOR=f"10.9.{n}.1, 198.51.100.7")
        assert response.status_code == 403
    assert_limited(sign_in("x@x.test", address="127.0.0.1", HTTP_X_FORWARDED_FOR="10.9.99.1, 198.51.100.7"))
    other = sign_in("x@x.test", address="127.0.0.1", HTTP_X_FORWARDED_FOR="10.9.99.1, 198.51.100.8")
    assert other.status_code == 403


# ---- the counters themselves -----------------------------------------------------------------------------------------


def test_counters_store_no_address_or_email_and_are_pruned(a, clock):
    """Keys are HMAC-SHA256 under SECRET_KEY (salted_hmac with the module's own salt), not a plain hash a list of
    addresses or emails could be matched against."""
    import hashlib

    from core import counters, jobs
    from core.models import RateCounter

    sign_in("editor@orga.test", address="192.0.2.77")
    rows = list(RateCounter.objects.values_list("id", "scope", "key"))
    assert {scope for _, scope, _ in rows} == {"throttle:login", "signin-account"}
    text = repr(rows)
    assert "192.0.2.77" not in text and "editor@orga.test" not in text
    plain = hashlib.sha256(b"editor@orga.test").hexdigest()[:32]
    assert plain not in text and counters.digest("editor@orga.test") in text
    definition = jobs.SCHEDULES["counters.prune"]
    assert definition.func is counters.prune and definition.cron == "*/10 * * * *"
    assert counters.prune(datetime.fromtimestamp(START + 59, UTC)) == {"rate_counters_pruned": 0}
    assert counters.prune(datetime.fromtimestamp(START + 900, UTC)) == {"rate_counters_pruned": len(rows)}
    assert not RateCounter.objects.exists()


def test_every_throttle_counts_in_the_shared_counters():
    """No view may go back to DRF's per-process throttles: only core/throttling.py imports rest_framework.throttling,
    and every throttle it defines for views is a SharedRateThrottle."""
    root = Path(__file__).resolve().parent.parent
    found = []
    for path in root.rglob("*.py"):
        parts = path.relative_to(root).parts
        if parts[0] in {".venv", "tests"} or "migrations" in parts:
            continue
        if "rest_framework.throttling" in path.read_text(encoding="utf-8") and parts != ("core", "throttling.py"):
            found.append("/".join(parts))
    assert found == []
    from catalog.attachment_viewsets import UploadThrottle
    from core.search_views import SearchThrottle
    from core.throttling import ApiRateThrottle, OrgRateThrottle, ScopedThrottle, SharedRateThrottle
    from money.report_views import FeedAddressThrottle, FeedThrottle

    for cls in (UploadThrottle, SearchThrottle, ApiRateThrottle, OrgRateThrottle, ScopedThrottle, FeedThrottle):
        assert issubclass(cls, SharedRateThrottle), cls
    assert issubclass(FeedAddressThrottle, SharedRateThrottle)


def test_doctor_warns_in_cloud_mode_when_every_sign_in_comes_from_one_address(a, clock, settings):
    from core import doctor

    settings.TRUSTED_PROXIES = []
    assert doctor.check_cloud_rate_limits().status == doctor.WARN
    settings.TRUSTED_PROXIES = ["127.0.0.1"]
    assert doctor.check_cloud_rate_limits().status == doctor.OK
    for n in range(doctor.ONE_ADDRESS_MINIMUM):
        clock[0] = START + 60 * n  # one a minute, so the address's own limit never refuses
        sign_in(f"nobody{n}@x.test", address="192.0.2.90")
    check = doctor.check_cloud_rate_limits()
    assert check.status == doctor.WARN and "one client address" in check.message
    sign_in("someone@x.test", address="192.0.2.91")
    assert doctor.check_cloud_rate_limits().status == doctor.OK
    assert ("cloud: rate limits", doctor.check_cloud_rate_limits) in doctor.CLOUD_CHECKS


@pytest.fixture
def fresh(db):
    """A fresh installation (as tests/test_setup.py's): the setup state row open, no organization or user."""
    from core.models import BootstrapState

    BootstrapState.objects.update_or_create(pk=1, defaults={"completed_at": None})
