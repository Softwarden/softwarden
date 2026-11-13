"""Session rotation race, with real concurrency: two database connections in two threads, interleaved with
events, so the row locks and commits are PostgreSQL's own (core/sessions.py), sign-out
included."""

from __future__ import annotations

import json
import threading
from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.sessions.backends.base import UpdateError
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.db import connection, transaction
from django.test import Client
from django.utils import timezone

from catalog import admin_services, platform_admin
from core import impersonation, sessions
from core.models import AuditEvent, Membership
from tests.test_platform import make_staff, start

COOKIE = settings.SESSION_COOKIE_NAME
WAIT = 10

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def empty_cache():
    """Starting an impersonation shares the sign-in throttle, which counts in the process cache."""
    cache.clear()
    yield
    cache.clear()


def in_thread(fn, out: dict, name: str) -> threading.Thread:
    """Run fn on its own thread (its own database connection); its result or exception lands in out[name]."""

    def run():
        try:
            out[name] = fn()
        except BaseException as e:  # noqa: BLE001  # the test asserts on it
            out[name] = e
        finally:
            connection.close()

    thread = threading.Thread(target=run, name=name)
    thread.start()
    return thread


def make_session(**data) -> str:
    store = sessions.SessionStore()
    for k, v in data.items():
        store[k] = v
    store.create()
    assert store.session_key is not None
    return store.session_key


def live_sessions() -> int:
    return (
        Session.objects.exclude(session_data__startswith=sessions.ROTATED)
        .filter(expire_date__gt=timezone.now())
        .count()
    )


def is_tombstone(session_key: str) -> bool:
    return sessions.is_tombstone(Session.objects.get(session_key=session_key).session_data)


def impersonating_staff(a, api):
    """A staff account (with its own membership) impersonating a's editor; (client, session key)."""
    staff = make_staff()
    Membership.objects.create(user=staff, organization=a["org"], role="admin")
    client = api(staff)
    assert start(client, a["org"], a["users"]["editor"]).status_code == 201
    return client, client.client.cookies[COOKIE].value


def expire(session_key: str) -> None:
    store = sessions.SessionStore(session_key)
    state = store[impersonation.SESSION_KEY]
    past = (timezone.now() - timedelta(minutes=1)).isoformat()
    store[impersonation.SESSION_KEY] = {**state, "expires_at": past}
    store.save()


# ---- the store ------------------------------------------------------------------------------------------------------


def test_blocked_cycle_sees_committed_tombstone():
    key = make_session(_auth_user_id="1", x="y")
    loaded = threading.Event()
    go = threading.Event()
    out: dict = {}

    def b():
        st = sessions.SessionStore(key)
        assert st["x"] == "y"
        loaded.set()
        go.wait(WAIT)
        st.cycle_key()  # blocks on the row lock while A's transaction is open
        return st.session_key

    t = in_thread(b, out, "b")
    loaded.wait(WAIT)
    with transaction.atomic():
        a = sessions.SessionStore(key)
        a["x"]
        a.cycle_key()
        go.set()
        t.join(1.5)
        assert t.is_alive(), "B should be blocked on the row lock"
    t.join(WAIT)
    assert isinstance(out["b"], SessionInterrupted), out
    assert is_tombstone(key)
    assert live_sessions() == 1  # only A's new session


def test_blocked_cycle_proceeds_when_a_rolls_back():
    key = make_session(x="y")
    loaded = threading.Event()
    go = threading.Event()
    out: dict = {}

    def b():
        st = sessions.SessionStore(key)
        st["x"]
        loaded.set()
        go.wait(WAIT)
        st.cycle_key()
        return st.session_key

    t = in_thread(b, out, "b")
    loaded.wait(WAIT)
    try:
        with transaction.atomic():
            a = sessions.SessionStore(key)
            a["x"]
            a.cycle_key()
            go.set()
            t.join(1.5)
            raise RuntimeError("rollback")
    except RuntimeError:
        pass
    t.join(WAIT)
    assert isinstance(out["b"], str) and out["b"] != key, out
    assert is_tombstone(key)
    assert sessions.SessionStore(out["b"])["x"] == "y"


def test_save_after_committed_rotation_on_another_connection():
    key = make_session(x="y")
    loaded = threading.Event()
    go = threading.Event()
    out: dict = {}

    def b():
        st = sessions.SessionStore(key)
        st["x"]
        loaded.set()
        go.wait(WAIT)
        st["x"] = "late"
        st.save()

    t = in_thread(b, out, "b")
    loaded.wait(WAIT)
    sessions.SessionStore().delete(key)
    go.set()
    t.join(WAIT)
    assert isinstance(out["b"], UpdateError), out
    assert is_tombstone(key)


# ---- over HTTP ------------------------------------------------------------------------------------------------------


def test_http_switch_racing_real_sign_out(a, b, monkeypatch):
    user = a["users"]["editor"]
    Membership.objects.create(user=user, organization=b["org"], role="viewer")
    tab_a = Client()
    tab_a.force_login(user)
    old = tab_a.cookies[COOKIE].value
    tab_b = Client()
    tab_b.cookies[COOKIE] = old
    reached = threading.Event()
    go = threading.Event()
    out: dict = {}
    original = admin_services.switch_organization

    def wait_then_switch(*args, **kw):
        reached.set()
        go.wait(WAIT)
        return original(*args, **kw)

    monkeypatch.setattr(admin_services, "switch_organization", wait_then_switch)
    body = json.dumps({"organization_id": str(b["org"].id)})
    t = in_thread(
        lambda: tab_b.post("/api/v1/auth/me/organization", body, content_type="application/json"), out, "switch"
    )
    assert reached.wait(WAIT)
    r = tab_a.post("/api/v1/auth/logout", "{}", content_type="application/json")
    assert r.status_code == 204
    go.set()
    t.join(15)
    resp = out["switch"]
    assert not isinstance(resp, BaseException), resp
    assert resp.status_code == 400, (resp.status_code, resp.content)
    assert resp.json()["type"] == "/problems/malformed-request"
    assert resp.json()["detail"] == sessions.INTERRUPTED  # the reason reaches the client: "... Reload."
    assert COOKIE not in resp.cookies
    assert is_tombstone(old)
    assert live_sessions() == 0
    assert not AuditEvent.objects.filter(action="user.organization_switched").exists()


def test_two_reads_notice_expiry_together(a, api, monkeypatch):
    """Both read the expired impersonation; the one that loses the key change answers 409 impersonation-ended (the SPA
    reloads on it), not a generic 400, and the end is audited once."""
    _, k = impersonating_staff(a, api)
    expire(k)
    barrier = threading.Barrier(2, timeout=WAIT)
    original = platform_admin.platform_audit

    def audit_then_wait(*args, **kw):
        r = original(*args, **kw)
        barrier.wait()
        return r

    monkeypatch.setattr(platform_admin, "platform_audit", audit_then_wait)
    out: dict = {}
    tabs = []
    for n in ("one", "two"):
        cl = Client()
        cl.cookies[COOKIE] = k
        tabs.append(in_thread(lambda cl=cl: cl.get("/api/v1/auth/me"), out, n))
    for t in tabs:
        t.join(20)
    responses = [out[n] for n in ("one", "two")]
    assert not any(isinstance(r, BaseException) for r in responses), responses
    assert sorted(r.status_code for r in responses) == [200, 409]
    lost = next(r for r in responses if r.status_code == 409)
    assert lost.json()["type"] == impersonation.IMPERSONATION_ENDED
    assert COOKIE not in lost.cookies  # the winner's new cookie stays
    assert AuditEvent.objects.filter(action="impersonation.expired").count() == 1


def test_sign_out_racing_stop(a, api, monkeypatch):
    """Sign-out read the impersonating session; Stop (another tab) changed the key before sign-out could. Sign-out
    still answers 204, deletes the cookie and ends the session Stop created: nothing of this browser stays signed in."""
    c, k = impersonating_staff(a, api)
    reached = threading.Event()
    go = threading.Event()
    out: dict = {}
    original = platform_admin.platform_audit

    def pause(*args, **kw):
        r = original(*args, **kw)
        if threading.current_thread().name == "logout":
            reached.set()
            go.wait(WAIT)
        return r

    monkeypatch.setattr(platform_admin, "platform_audit", pause)
    tab_b = Client()
    tab_b.cookies[COOKIE] = k
    t = in_thread(lambda: tab_b.post("/api/v1/auth/logout", "{}", content_type="application/json"), out, "logout")
    assert reached.wait(WAIT)
    stop = c.client.delete("/api/v1/platform/impersonation")
    assert stop.status_code == 200
    stopped_key = c.client.cookies[COOKIE].value
    assert stopped_key != k
    go.set()
    t.join(15)
    r = out["logout"]
    assert not isinstance(r, BaseException), r
    assert r.status_code == 204, (r.status_code, r.content)
    assert r.cookies[COOKIE].value == "" and str(r.cookies[COOKIE]["max-age"]) == "0"
    assert is_tombstone(k) and is_tombstone(stopped_key)
    assert live_sessions() == 0
    assert AuditEvent.objects.filter(action="impersonation.stopped").count() == 1  # Stop's; sign-out's rolled back
    assert c.client.get("/api/v1/platform/session").status_code == 403  # Stop's cookie no longer signs anyone in


def test_sign_out_losing_an_expiry_to_a_read(a, api, monkeypatch):
    """Both notice the expiry; the read wins the key change, then sign-out's end loses in the impersonation
    middleware. Sign-out isn't refused: 204, cookie deleted, and the read's new session ended too."""
    _, k = impersonating_staff(a, api)
    expire(k)
    read_done = threading.Event()
    reached = threading.Event()
    out: dict = {}
    original = platform_admin.platform_audit

    def pause(*args, **kw):
        r = original(*args, **kw)
        if threading.current_thread().name == "logout":
            reached.set()
            read_done.wait(WAIT)  # the read finishes (and commits its key change) first
        return r

    monkeypatch.setattr(platform_admin, "platform_audit", pause)
    tab_out = Client()
    tab_out.cookies[COOKIE] = k
    logout = in_thread(
        lambda: tab_out.post("/api/v1/auth/logout", "{}", content_type="application/json"), out, "logout"
    )
    assert reached.wait(WAIT)
    tab_read = Client()
    tab_read.cookies[COOKIE] = k
    read = tab_read.get("/api/v1/auth/me")
    read_done.set()
    logout.join(15)
    assert read.status_code == 200
    r = out["logout"]
    assert not isinstance(r, BaseException), r
    assert r.status_code == 204, (r.status_code, r.content)
    assert r.cookies[COOKIE].value == "" and str(r.cookies[COOKIE]["max-age"]) == "0"
    assert live_sessions() == 0
    assert AuditEvent.objects.filter(action="impersonation.expired").count() == 1


def test_sign_out_racing_an_organization_switch(a, b, monkeypatch):
    """Sign-out read the session; an organization switch (another tab) changed the key before sign-out's flush. The
    flush finds a tombstone, so sign-out ends the switch's new session too: 204, cookie deleted, nothing live."""
    from catalog import views

    user = a["users"]["editor"]
    Membership.objects.create(user=user, organization=b["org"], role="viewer")
    tab_out = Client()
    tab_out.force_login(user)
    k = tab_out.cookies[COOKIE].value
    tab_switch = Client()
    tab_switch.cookies[COOKIE] = k
    reached = threading.Event()
    go = threading.Event()
    out: dict = {}
    original = views.logout

    def wait_then_logout(request):
        reached.set()
        go.wait(WAIT)  # the session is read (signed in); the switch commits its key change now
        return original(request)

    monkeypatch.setattr(views, "logout", wait_then_logout)
    t = in_thread(lambda: tab_out.post("/api/v1/auth/logout", "{}", content_type="application/json"), out, "logout")
    assert reached.wait(WAIT)
    body = json.dumps({"organization_id": str(b["org"].id)})
    switched = tab_switch.post("/api/v1/auth/me/organization", body, content_type="application/json")
    assert switched.status_code == 200
    new = tab_switch.cookies[COOKIE].value
    assert new != k
    go.set()
    t.join(15)
    r = out["logout"]
    assert not isinstance(r, BaseException), r
    assert r.status_code == 204, (r.status_code, r.content)
    assert r.cookies[COOKIE].value == "" and str(r.cookies[COOKIE]["max-age"]) == "0"
    assert is_tombstone(k) and is_tombstone(new)
    assert live_sessions() == 0
    assert tab_switch.get("/api/v1/auth/me").status_code == 403
