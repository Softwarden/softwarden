"""Trusted-proxy name resolution never holds requests up (core/middleware.py).

QA: on a clean Compose start `proxy` doesn't resolve until Caddy runs, Caddy starts only once the app is
healthy, and every request spent ~4 s in getaddrinfo, so the healthcheck never passed. These tests pin the rules that
broke the cycle: no lookup unless a header needs one, failures cached briefly, a bounded wait, probes exempt."""

import logging
import threading
import time
import uuid
from typing import Any

import pytest
from django.test import Client, RequestFactory

from core import middleware

REQUEST_ID = "6f1c1d1e-8d84-4b4e-9d61-2a7c2b3d4e5f"
FORWARDED: dict[str, Any] = {
    "HTTP_X_FORWARDED_FOR": "203.0.113.5",
    "HTTP_X_FORWARDED_PROTO": "https",
    "HTTP_X_REQUEST_ID": REQUEST_ID,
}


def settle():
    """Wait for the lookup in progress, if any."""
    thread = middleware._pending.get("proxy")
    if thread is not None:
        thread.join(5)


@pytest.fixture(autouse=True)
def fresh_resolver(settings):
    def reset():
        for thread in list(middleware._pending.values()):
            thread.join(5)
        middleware._resolved.clear()
        middleware._pending.clear()
        middleware._failing.clear()

    reset()
    settings.TRUSTED_PROXIES = ["proxy"]
    yield
    reset()


class FakeResolver:
    """Stands in for socket.getaddrinfo: counts calls, and answers, fails, or blocks until released."""

    def __init__(self, answer=None, block=False):
        self.calls = 0
        self.answer = answer  # None: the name doesn't resolve
        self.release = threading.Event()
        if not block:
            self.release.set()

    def __call__(self, host, *args, **kwargs):
        self.calls += 1
        self.release.wait(10)
        if self.answer is None:
            raise OSError("Name or service not known")
        return [(None, None, None, "", (addr, 0)) for addr in self.answer]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(middleware, "_now", value)
    return value


def test_a_request_without_forwarded_or_request_id_headers_does_no_lookup(db, monkeypatch):
    def forbidden(*args, **kwargs):
        time.sleep(4)
        raise AssertionError("getaddrinfo called")

    monkeypatch.setattr(middleware.socket, "getaddrinfo", forbidden)
    started = time.monotonic()
    response = Client().get("/api/v1/health")
    assert response.status_code == 200 and uuid.UUID(response["X-Request-ID"])
    # A request id that isn't a UUID would be ignored anyway, so it doesn't need the check either.
    assert Client(HTTP_X_REQUEST_ID="not-a-uuid").get("/api/v1/health")["X-Request-ID"] != "not-a-uuid"
    assert time.monotonic() - started < 1
    assert not middleware._pending and not middleware._resolved


def test_probes_stay_fast_and_never_resolve_while_the_proxy_name_is_unresolvable(db, monkeypatch):
    resolver = FakeResolver(block=True)  # every lookup would hang
    monkeypatch.setattr(middleware.socket, "getaddrinfo", resolver)
    client = Client(**FORWARDED)
    started = time.monotonic()
    for path in ("/readyz/web", "/healthz", "/readyz/web"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response["X-Request-ID"] != REQUEST_ID  # probes don't consult the proxy list for it
    assert time.monotonic() - started < 1
    assert resolver.calls == 0
    resolver.release.set()


def test_probes_drop_forwarded_headers_even_from_a_trusted_proxy(settings):
    settings.TRUSTED_PROXIES = ["127.0.0.1"]
    headers: dict[str, Any] = {"HTTP_X_FORWARDED_FOR": "203.0.113.5", "HTTP_X_FORWARDED_PROTO": "https"}
    for path in sorted(middleware.PROBE_PATHS):
        request = RequestFactory().get(path, REMOTE_ADDR="127.0.0.1", **headers)
        middleware.TrustedProxyMiddleware(lambda r: r)(request)
        assert not request.is_secure() and middleware.client_ip(request) == "127.0.0.1", path
    request = RequestFactory().get("/api/v1/health", REMOTE_ADDR="127.0.0.1", **headers)  # elsewhere: believed
    middleware.TrustedProxyMiddleware(lambda r: r)(request)
    assert request.is_secure() and middleware.client_ip(request) == "203.0.113.5"


def test_an_unresolvable_proxy_is_looked_up_at_most_once_per_failure_window(db, monkeypatch, clock, caplog):
    resolver = FakeResolver(answer=None)
    monkeypatch.setattr(middleware.socket, "getaddrinfo", resolver)
    client = Client(**FORWARDED)
    with caplog.at_level(logging.INFO, logger="core.middleware"):
        for _ in range(5):
            response = client.get("/api/v1/health")
            assert response["X-Request-ID"] != REQUEST_ID  # not trusted: the name doesn't resolve
        assert resolver.calls == 1
        clock.now += middleware.FAILURE_TTL - 1
        client.get("/api/v1/health")
        assert resolver.calls == 1
        clock.now += 2  # the failure window is over: asked again, once
        client.get("/api/v1/health")
        client.get("/api/v1/health")
        assert resolver.calls == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "does not resolve" in r.getMessage()]
    assert len(warnings) == 1  # logged when it starts failing, not per request or per retry


def test_once_the_proxy_resolves_it_is_trusted_and_cached_for_the_success_window(db, monkeypatch, clock, caplog):
    resolver = FakeResolver(answer=None)
    monkeypatch.setattr(middleware.socket, "getaddrinfo", resolver)
    client = Client(**FORWARDED)
    client.get("/api/v1/health")
    resolver.answer = ["not-an-address", "fe80::1%eth0", "127.0.0.1"]  # the proxy is up (test client's address)
    clock.now += middleware.FAILURE_TTL + 1
    with caplog.at_level(logging.INFO, logger="core.middleware"):
        assert client.get("/api/v1/health")["X-Request-ID"] == REQUEST_ID
    assert any("resolves again" in r.getMessage() for r in caplog.records)
    assert middleware.is_trusted_proxy("fe80::1")  # the zone is dropped, bad entries skipped
    clock.now += middleware.RESOLVE_TTL - 1
    assert client.get("/api/v1/health")["X-Request-ID"] == REQUEST_ID
    assert resolver.calls == 2


def test_a_slow_lookup_holds_a_request_at_most_the_resolve_wait_and_runs_once(db, monkeypatch):
    monkeypatch.setattr(middleware, "RESOLVE_WAIT", 0.05)
    resolver = FakeResolver(answer=["127.0.0.1"], block=True)
    monkeypatch.setattr(middleware.socket, "getaddrinfo", resolver)
    client = Client(**FORWARDED)
    started = time.monotonic()
    first = client.get("/api/v1/health")
    assert first.status_code == 200 and first["X-Request-ID"] != REQUEST_ID  # no answer yet: trust nobody
    waited = time.monotonic() - started
    assert middleware.RESOLVE_WAIT <= waited < 1  # the request that started the lookup waited, boundedly
    monkeypatch.setattr(middleware, "RESOLVE_WAIT", 5.0)  # requests arriving meanwhile don't wait at all
    started = time.monotonic()
    for _ in range(3):
        assert client.get("/api/v1/health")["X-Request-ID"] != REQUEST_ID
    assert time.monotonic() - started < 1
    assert resolver.calls == 1  # one lookup thread for the name, not one per request
    resolver.release.set()
    settle()
    assert client.get("/api/v1/health")["X-Request-ID"] == REQUEST_ID
    assert resolver.calls == 1


def test_a_slow_refresh_keeps_the_last_known_answer(monkeypatch, clock):
    monkeypatch.setattr(middleware, "RESOLVE_WAIT", 0.05)
    monkeypatch.setattr(middleware.socket, "getaddrinfo", FakeResolver(answer=["10.1.2.3"]))
    assert middleware.is_trusted_proxy("10.1.2.3")
    slow = FakeResolver(answer=["10.1.2.4"], block=True)
    monkeypatch.setattr(middleware.socket, "getaddrinfo", slow)
    clock.now += middleware.RESOLVE_TTL + 1
    assert middleware.is_trusted_proxy("10.1.2.3")  # stale but known, while the refresh runs
    slow.release.set()
    settle()
    assert middleware.is_trusted_proxy("10.1.2.4") and not middleware.is_trusted_proxy("10.1.2.3")


def test_a_stale_answer_is_not_served_past_the_stale_limit(monkeypatch, clock):
    monkeypatch.setattr(middleware, "RESOLVE_WAIT", 0.05)
    monkeypatch.setattr(middleware.socket, "getaddrinfo", FakeResolver(answer=["10.1.2.3"]))
    assert middleware.is_trusted_proxy("10.1.2.3")
    slow = FakeResolver(answer=["10.1.2.3"], block=True)
    monkeypatch.setattr(middleware.socket, "getaddrinfo", slow)
    clock.now += middleware.RESOLVE_TTL + 1
    assert middleware.is_trusted_proxy("10.1.2.3")  # starts the refresh; stale answer served
    clock.now += middleware.STALE_LIMIT - middleware.RESOLVE_TTL - 2
    assert middleware.is_trusted_proxy("10.1.2.3")  # just under the limit
    clock.now += 1
    assert not middleware.is_trusted_proxy("10.1.2.3")  # refresh still running, answer too old: trust nobody
    assert slow.calls == 1
    slow.release.set()
    settle()


def test_a_failed_refresh_stops_trusting_the_old_address(monkeypatch, clock):
    monkeypatch.setattr(middleware.socket, "getaddrinfo", FakeResolver(answer=["10.1.2.3"]))
    assert middleware.is_trusted_proxy("10.1.2.3")
    clock.now += middleware.RESOLVE_TTL + 1
    monkeypatch.setattr(middleware.socket, "getaddrinfo", FakeResolver(answer=None))
    middleware.is_trusted_proxy("10.1.2.3")
    settle()
    assert not middleware.is_trusted_proxy("10.1.2.3")


def test_an_unexpected_lookup_error_clears_the_pending_lookup_and_trusts_nobody(monkeypatch):
    def broken(entry):
        raise RuntimeError("boom")

    monkeypatch.setattr(middleware, "_lookup", broken)
    assert not middleware.is_trusted_proxy("10.1.2.3")
    settle()
    assert "proxy" not in middleware._pending and middleware._resolved["proxy"][1] == []


def test_a_thread_that_cannot_start_counts_as_a_failed_lookup(monkeypatch, clock, caplog):
    original = threading.Thread

    class Unstartable(threading.Thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(middleware.threading, "Thread", Unstartable)
    with caplog.at_level(logging.WARNING, logger="core.middleware"):
        assert not middleware.is_trusted_proxy("10.1.2.3")
    assert not middleware._pending and middleware._resolved["proxy"] == (clock.now, [])
    assert any("does not resolve" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(middleware.threading, "Thread", original)
    resolver = FakeResolver(answer=["10.1.2.3"])
    monkeypatch.setattr(middleware.socket, "getaddrinfo", resolver)
    assert not middleware.is_trusted_proxy("10.1.2.3") and resolver.calls == 0  # inside the failure window
    clock.now += middleware.FAILURE_TTL
    assert middleware.is_trusted_proxy("10.1.2.3") and resolver.calls == 1  # not stuck pending


def test_no_trusted_proxies_configured_means_no_lookup(monkeypatch, settings):
    settings.TRUSTED_PROXIES = []
    resolver = FakeResolver(answer=["127.0.0.1"])
    monkeypatch.setattr(middleware.socket, "getaddrinfo", resolver)
    assert not middleware.is_trusted_proxy("127.0.0.1")
    settings.TRUSTED_PROXIES = ["10.0.0.0/8"]  # ranges and addresses never resolve
    assert middleware.is_trusted_proxy("10.4.5.6")
    assert resolver.calls == 0
