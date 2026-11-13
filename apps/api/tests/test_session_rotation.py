"""Session rotation race: a request sent with a session key that was rotated away (impersonation start and
stop, sign-in, organization switch, sign-out) is anonymous and leaves the session cookie alone, so it can't delete the
newer cookie the rotating response set (core/sessions.py)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.sessions.backends.base import CreateError, UpdateError
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.models import Session
from django.test import Client
from django.utils import timezone

from catalog import admin_services, platform_admin
from core import impersonation, jobs, sessions
from core.models import AuditEvent, Membership
from tests.conftest import Api
from tests.test_platform import PASSWORD, make_staff, start
from tests.test_problems import is_problem

COOKIE = settings.SESSION_COOKIE_NAME
NOT_SIGNED_IN = "/problems/not-signed-in"
MALFORMED = "/problems/malformed-request"


@pytest.fixture(autouse=True)
def empty_cache():
    """Starting an impersonation and signing in share the sign-in throttle, which counts in the process cache."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def staff(db, b):
    user = make_staff()
    Membership.objects.create(user=user, organization=b["org"], role="admin")
    return user


def key(client) -> str:
    return client.cookies[COOKIE].value


def with_cookie(value: str) -> Client:
    """A second client holding only this session cookie: the other tab, or the request still in flight."""
    other = Client()
    other.cookies[COOKIE] = value
    return other


def get(client, url: str):
    return client.get("/api/v1/" + url)


def post(client, url: str, data: dict | None = None):
    return client.post("/api/v1/" + url, json.dumps(data or {}), content_type="application/json")


def assert_cookie_untouched(response):
    assert COOKIE not in response.cookies, response.cookies[COOKIE].output()


def assert_cookie_deleted(response):
    assert COOKIE in response.cookies
    assert response.cookies[COOKIE].value == ""
    assert str(response.cookies[COOKIE]["max-age"]) == "0"


def assert_stale_is_anonymous(old: str):
    """The old key answers as nobody, and the response doesn't touch the cookie."""
    stale = with_cookie(old)
    response = get(stale, "auth/me")
    is_problem(response, 403, NOT_SIGNED_IN)
    assert_cookie_untouched(response)
    response = get(stale, "platform/session")
    assert response.status_code == 403
    assert_cookie_untouched(response)
    # A write as well: refused, and still no cookie change.
    response = post(stale, "applications", {"name": "From the stale tab", "lifecycle": "active"})
    assert response.status_code == 403
    assert_cookie_untouched(response)


def test_impersonation_start_leaves_the_new_cookie_alone(a, staff, api):
    client = api(staff)
    old = key(client.client)
    assert start(client, a["org"], a["users"]["editor"]).status_code == 201
    new = key(client.client)
    assert new != old
    assert_stale_is_anonymous(old)
    me = client.get("auth/me")  # the new cookie still works, as the member
    assert me.status_code == 200 and me.json_body["impersonation"] is not None
    assert me.json_body["user"]["email"] == a["users"]["editor"].email


def test_impersonation_stop_leaves_the_new_cookie_alone(a, b, staff, api):
    """The trace that found it: GET dashboard sent before DELETE platform/impersonation, answered after."""
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    old = key(client.client)
    assert client.delete("platform/impersonation").status_code == 200
    new = key(client.client)
    assert new != old
    response = get(with_cookie(old), "dashboard")
    is_problem(response, 403, NOT_SIGNED_IN)
    assert_cookie_untouched(response)
    assert_stale_is_anonymous(old)
    me = client.get("auth/me")  # signed in as the staff account again, in their own organization
    assert me.status_code == 200 and me.json_body["impersonation"] is None
    assert me.json_body["user"]["email"] == staff.email
    assert impersonation.SESSION_KEY not in client.client.session


def test_sign_in_rotation_leaves_the_new_cookie_alone(a):
    """Signing in over an anonymous session (one that holds values, as single sign-on's start leaves) cycles the key.
    (Django keeps the key when the same account signs in again, and flushes it for another account: next test.)"""
    user = a["users"]["editor"]
    anonymous = sessions.SessionStore()
    anonymous["before_sign_in"] = "value"
    anonymous.create()
    assert anonymous.session_key is not None
    client = with_cookie(anonymous.session_key)
    old = key(client)
    assert get(client, "auth/me").status_code == 403
    response = post(client, "auth/login", {"email": user.email, "password": PASSWORD})
    assert response.status_code == 200
    assert key(client) != old
    assert_stale_is_anonymous(old)
    assert get(client, "auth/me").status_code == 200


def test_sign_in_as_someone_else_leaves_the_new_cookie_alone(a):
    """Signing in as another account flushes the old session: also a tombstone, not a deletion."""
    client = Client()
    client.force_login(a["users"]["viewer"])
    old = key(client)
    editor = a["users"]["editor"]
    assert post(client, "auth/login", {"email": editor.email, "password": PASSWORD}).status_code == 200
    assert_stale_is_anonymous(old)
    assert get(client, "auth/me").json()["user"]["email"] == editor.email


def test_organization_switch_leaves_the_new_cookie_alone(a, b):
    user = a["users"]["editor"]
    Membership.objects.create(user=user, organization=b["org"], role="viewer")
    client = Client()
    client.force_login(user)
    old = key(client)
    response = post(client, "auth/me/organization", {"organization_id": str(b["org"].id)})
    assert response.status_code == 200
    assert key(client) != old
    assert_stale_is_anonymous(old)
    assert get(client, "auth/me").json()["organization"]["id"] == str(b["org"].id)


def test_the_tombstone_holds_only_its_successor_and_expires(a, staff, api):
    client = api(staff)
    old = key(client.client)
    start(client, a["org"], a["users"]["editor"])
    row = Session.objects.get(session_key=old)
    assert row.session_data == f"{sessions.ROTATED}>{key(client.client)}"  # no session values, only the next key
    assert sessions.successor_of(row.session_data) == key(client.client)
    left = row.expire_date - timezone.now()
    assert timedelta(seconds=sessions.ROTATED_SECONDS - 10) < left <= timedelta(seconds=sessions.ROTATED_SECONDS)
    store = sessions.SessionStore(old)
    assert dict(store.items()) == {} and store.rotated_away and store.session_key is None


def test_an_expired_tombstone_is_deleted_like_any_unknown_cookie(a, staff, api):
    client = api(staff)
    old = key(client.client)
    start(client, a["org"], a["users"]["editor"])
    Session.objects.filter(session_key=old).update(expire_date=timezone.now() - timedelta(seconds=1))
    response = get(with_cookie(old), "auth/me")
    is_problem(response, 403, NOT_SIGNED_IN)
    assert_cookie_deleted(response)


def test_an_unknown_cookie_is_still_deleted(db):
    response = get(with_cookie("x" * 32), "auth/me")
    is_problem(response, 403, NOT_SIGNED_IN)
    assert_cookie_deleted(response)


def test_an_expired_session_is_still_deleted(a):
    client = Client()
    client.force_login(a["users"]["editor"])
    old = key(client)
    Session.objects.filter(session_key=old).update(expire_date=timezone.now() - timedelta(seconds=1))
    response = get(with_cookie(old), "auth/me")
    is_problem(response, 403, NOT_SIGNED_IN)
    assert_cookie_deleted(response)


def test_signing_out_still_deletes_the_cookie(a):
    client = Client()
    client.force_login(a["users"]["editor"])
    old = key(client)
    response = post(client, "auth/logout")
    assert response.status_code == 204
    assert_cookie_deleted(response)
    # A request sent before the sign-out and answered after it: nobody, and no cookie change (already deleted).
    assert_stale_is_anonymous(old)


def test_signing_out_while_impersonating_still_deletes_the_cookie(a, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    old = key(client.client)
    response = post(client.client, "auth/logout")
    assert response.status_code == 204
    assert_cookie_deleted(response)
    assert_stale_is_anonymous(old)


def test_signing_in_with_a_stale_cookie_gets_a_fresh_session(a, staff, api):
    """A request on a rotated key that starts a session of its own (signing in) sets its cookie, as any sign-in."""
    client = api(staff)
    old = key(client.client)
    start(client, a["org"], a["users"]["editor"])
    stale = with_cookie(old)
    viewer = a["users"]["viewer"]
    response = post(stale, "auth/login", {"email": viewer.email, "password": PASSWORD})
    assert response.status_code == 200
    fresh = response.cookies[COOKIE].value
    assert fresh and fresh != old
    assert sessions.is_tombstone(Session.objects.get(session_key=old).session_data)
    assert get(stale, "auth/me").json()["user"]["email"] == viewer.email


def test_a_request_that_read_the_old_session_cannot_write_it_back(a, staff, api):
    """A request that loaded the session before the rotation and changed it afterwards must not resurrect the old key
    with its authority: the save is refused (Django answers 400, as when the row was deleted)."""
    client = api(staff)
    old = key(client.client)
    in_flight = sessions.SessionStore(old)
    assert in_flight.get("_auth_user_id") == str(staff.pk)  # read before the rotation
    start(client, a["org"], a["users"]["editor"])
    in_flight["late"] = "value"
    with pytest.raises(UpdateError):
        in_flight.save()
    assert sessions.is_tombstone(Session.objects.get(session_key=old).session_data)
    is_problem(get(with_cookie(old), "auth/me"), 403, NOT_SIGNED_IN)


def test_an_ordinary_session_still_saves(a):
    client = Client()
    client.force_login(a["users"]["editor"])
    store = sessions.SessionStore(key(client))
    store["note"] = "kept"
    store.save()
    assert sessions.SessionStore(key(client))["note"] == "kept"


def test_the_installation_uses_these_sessions():
    assert settings.SESSION_ENGINE == "core.sessions"
    assert "core.sessions.SessionMiddleware" in settings.MIDDLEWARE
    assert "django.contrib.sessions.middleware.SessionMiddleware" not in settings.MIDDLEWARE


# ---- cycling a key that another request rotated meanwhile -----------------------------------------------------------


def test_cycle_key_after_a_concurrent_sign_out_creates_nothing(a):
    """Tab B read the session, tab A signed out: B's key change must not sign the browser back in under a new key."""
    client = Client()
    client.force_login(a["users"]["editor"])
    old = key(client)
    in_flight = sessions.SessionStore(old)
    assert in_flight.get("_auth_user_id") == str(a["users"]["editor"].pk)
    assert post(client, "auth/logout").status_code == 204
    rows = Session.objects.count()
    with pytest.raises(SessionInterrupted):
        in_flight.cycle_key()
    assert Session.objects.count() == rows
    assert sessions.is_tombstone(Session.objects.get(session_key=old).session_data)
    assert in_flight.rotated_away and in_flight.is_empty() and in_flight.session_key is None


def test_cycle_key_after_a_concurrent_impersonation_stop_creates_nothing(a, staff, api):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    in_flight = sessions.SessionStore(key(client.client))
    assert in_flight.get(impersonation.SESSION_KEY) is not None
    assert client.delete("platform/impersonation").status_code == 200
    rows = Session.objects.count()
    with pytest.raises(SessionInterrupted):
        in_flight.cycle_key()
    assert Session.objects.count() == rows


def test_cycle_key_of_a_live_session_tombstones_it_first(a):
    client = Client()
    client.force_login(a["users"]["editor"])
    old = key(client)
    store = sessions.SessionStore(old)
    store["kept"] = "value"
    store.cycle_key()
    assert store.session_key not in (None, old)
    assert sessions.is_tombstone(Session.objects.get(session_key=old).session_data)
    assert sessions.SessionStore(store.session_key)["kept"] == "value"


def test_cycle_key_of_a_tombstone_gives_a_fresh_empty_session(a, staff, api):
    client = api(staff)
    old = key(client.client)
    start(client, a["org"], a["users"]["editor"])
    store = sessions.SessionStore(old)
    store.cycle_key()
    assert store.session_key not in (None, old)
    assert dict(sessions.SessionStore(store.session_key).items()) == {}
    assert sessions.is_tombstone(Session.objects.get(session_key=old).session_data)


def test_password_change_racing_a_key_change_answers_400_and_keeps_the_password(a, monkeypatch, caplog):
    """The password is saved in the same transaction as the session's key change: when another request changed the
    key first, both roll back, so a 400 never follows a saved password. The reason reaches the client, and the race is
    logged at INFO without a traceback."""
    user = a["users"]["editor"]
    client = Client()
    client.force_login(user)
    old = key(client)
    original = admin_services.change_password

    def key_changed_meanwhile(*args, **kwargs):
        original(*args, **kwargs)
        sessions.SessionStore().delete(old)

    monkeypatch.setattr(admin_services, "change_password", key_changed_meanwhile)
    caplog.set_level("INFO", logger="softwarden.sessions")
    response = post(client, "auth/password", {"current_password": PASSWORD, "new_password": "a-new-long-passphrase"})
    body = is_problem(response, 400, MALFORMED)
    assert body["detail"] == sessions.INTERRUPTED and body["detail"].endswith("Reload.")
    assert_cookie_untouched(response)
    user.refresh_from_db()
    assert user.check_password(PASSWORD) and not user.check_password("a-new-long-passphrase")
    logged = [r for r in caplog.records if r.name == "softwarden.sessions"]
    assert len(logged) == 1 and logged[0].levelname == "INFO" and logged[0].exc_info is None
    assert not [r for r in caplog.records if r.name.startswith("django.security")]


def test_organization_switch_racing_a_sign_out_answers_400_and_changes_nothing(a, b, monkeypatch):
    """Over HTTP: the switch reads the session, then another tab signs out (simulated between the read and the key
    change). The switch answers 400, isn't audited, creates no session and leaves the cookie alone."""
    user = a["users"]["editor"]
    Membership.objects.create(user=user, organization=b["org"], role="viewer")
    client = Client()
    client.force_login(user)
    old = key(client)
    original = admin_services.switch_organization

    def signed_out_meanwhile(*args, **kwargs):
        original(*args, **kwargs)
        sessions.SessionStore().delete(old)  # what another tab's sign-out does to the row

    monkeypatch.setattr(admin_services, "switch_organization", signed_out_meanwhile)
    rows = Session.objects.count()
    response = post(client, "auth/me/organization", {"organization_id": str(b["org"].id)})
    is_problem(response, 400, MALFORMED)
    assert_cookie_untouched(response)
    assert Session.objects.count() == rows
    assert not AuditEvent.objects.filter(action="user.organization_switched").exists()
    # (The simulated sign-out ran on this test's one connection, inside the view's transaction, so it was rolled back
    # with it; a real one in another tab is committed and stays signed out.)


def test_impersonation_stop_racing_a_sign_out_answers_400_and_is_not_audited(a, staff, api, monkeypatch):
    client = api(staff)
    start(client, a["org"], a["users"]["editor"])
    current = key(client.client)
    original = platform_admin.platform_audit

    def signed_out_meanwhile(*args, **kwargs):
        original(*args, **kwargs)
        sessions.SessionStore().delete(current)

    monkeypatch.setattr(platform_admin, "platform_audit", signed_out_meanwhile)
    rows = Session.objects.count()
    response = client.client.delete("/api/v1/platform/impersonation")
    is_problem(response, 400, MALFORMED)
    assert_cookie_untouched(response)
    assert Session.objects.count() == rows
    assert not AuditEvent.objects.filter(action="impersonation.stopped").exists()


# ---- the store's other paths ----------------------------------------------------------------------------------------


def test_delete_neither_extends_a_tombstone_nor_entombs_an_expired_session(a):
    client = Client()
    client.force_login(a["users"]["editor"])
    old = key(client)
    sessions.SessionStore().delete(old)
    soon = timezone.now() + timedelta(seconds=5)
    Session.objects.filter(session_key=old).update(expire_date=soon)
    sessions.SessionStore().delete(old)
    assert Session.objects.get(session_key=old).expire_date == soon
    other = Client()
    other.force_login(a["users"]["viewer"])
    expired = key(other)
    past = timezone.now() - timedelta(seconds=1)
    Session.objects.filter(session_key=expired).update(expire_date=past)
    sessions.SessionStore().delete(expired)
    row = Session.objects.get(session_key=expired)
    assert not sessions.is_tombstone(row.session_data) and row.expire_date == past


def test_creating_a_session_under_a_tombstone_key_is_refused(a, staff, api):
    client = api(staff)
    old = key(client.client)
    start(client, a["org"], a["users"]["editor"])
    store = sessions.SessionStore(old)
    with pytest.raises(CreateError):
        store.save(must_create=True)
    assert sessions.is_tombstone(Session.objects.get(session_key=old).session_data)


def test_the_async_paths_keep_tombstones_too(db):
    async def round_trip():
        store = sessions.SessionStore()
        store["value"] = 1
        await store.asave()
        made = store.session_key
        assert made is not None
        loaded = sessions.SessionStore(made)
        assert (await loaded.aget("value")) == 1
        loaded["value"] = 2
        await loaded.asave()
        assert (await sessions.SessionStore(made).aget("value")) == 2
        await loaded.adelete()
        gone = sessions.SessionStore(made)
        assert (await gone._aget_session_from_db()) is None and gone.rotated_away and gone.session_key is None
        loaded["value"] = 3
        try:
            await loaded.asave()
        except UpdateError:
            pass
        else:
            raise AssertionError("an asave onto a tombstone must be refused")
        cycled = sessions.SessionStore(made)
        await cycled.acycle_key()  # a tombstone cycles into a fresh, empty session
        return made, cycled.session_key

    made, fresh = async_to_sync(round_trip)()
    assert sessions.is_tombstone(Session.objects.get(session_key=made).session_data)
    assert fresh not in (None, made) and dict(sessions.SessionStore(fresh).items()) == {}


# ---- the daily clean-up ---------------------------------------------------------------------------------------------


def test_the_daily_schedule_deletes_expired_sessions_and_tombstones_only(a, staff, api):
    definition = jobs.SCHEDULES["sessions.clear"]
    assert definition.func is sessions.clear_expired and definition.cron == "50 3 * * *"
    client = api(staff)
    old = key(client.client)
    start(client, a["org"], a["users"]["editor"])  # old becomes a tombstone, the new key is live
    live = key(client.client)
    expired = Client()
    expired.force_login(a["users"]["viewer"])
    stale = key(expired)
    Session.objects.filter(session_key=stale).update(expire_date=timezone.now() - timedelta(days=1))
    assert sessions.clear_expired(timezone.now()) == {"sessions_deleted": 1}
    assert not Session.objects.filter(session_key=stale).exists()
    assert Session.objects.filter(session_key=old).exists()  # not yet expired
    later = timezone.now() + timedelta(seconds=sessions.ROTATED_SECONDS + 1)
    assert sessions.clear_expired(later)["sessions_deleted"] >= 1
    assert not Session.objects.filter(session_key=old).exists()
    assert Session.objects.filter(session_key=live).exists()


# ---- a sign-out ends a successor only for its own interrupted session ------------------------------------------------


def write_token(a, role="editor") -> str:
    """A write API token of a's member (the attacker in these tests: another account than the browser's)."""
    from tests.test_tokens import expiry

    body = Api(a["users"]["admin"]).post(
        "api-tokens",
        {"user": a["users"][role].id, "name": "Script", "read_only": False, "expires_on": expiry(a["org"])},
    )
    assert body.status_code == 201, body.json_body
    return body.json_body["secret"]


def test_a_token_sign_out_with_someone_elses_old_cookie_leaves_their_session_live(a, b, api):
    """A token holder who learned another browser's rotated-away key sends it beside the token: the sign-out is the
    token's, and the browser's current session (the key's successor) stays signed in."""
    victim = make_staff(org=b["org"])
    browser = api(victim)
    old = key(browser.client)
    start(browser, b["org"], b["users"]["editor"])
    current = key(browser.client)
    attacker = Client(enforce_csrf_checks=True, HTTP_AUTHORIZATION=f"Bearer {write_token(a)}")
    attacker.cookies[COOKIE] = old
    response = post(attacker, "auth/logout")
    assert response.status_code in (204, 403), response.content
    assert not sessions.is_tombstone(Session.objects.get(session_key=current).session_data)
    assert browser.get("auth/me").status_code == 200


def test_a_stale_cookie_sign_out_is_refused_and_leaves_the_current_session(a, staff, api):
    browser = api(staff)
    old = key(browser.client)
    start(browser, a["org"], a["users"]["editor"])
    current = key(browser.client)
    response = post(with_cookie(old), "auth/logout")
    is_problem(response, 403, NOT_SIGNED_IN)
    assert_cookie_untouched(response)
    assert not sessions.is_tombstone(Session.objects.get(session_key=current).session_data)
    assert browser.get("auth/me").status_code == 200


def test_loading_a_tombstone_is_not_an_interruption(a, staff, api):
    browser = api(staff)
    old = key(browser.client)
    start(browser, a["org"], a["users"]["editor"])
    store = sessions.SessionStore(old)
    store.load()
    store.flush()
    assert store.rotated_away and not store.interrupted


def test_a_save_losing_the_race_says_the_change_may_be_saved(a, caplog):
    """The response's save finds the session rotated away: the view has run, so the 400 doesn't claim nothing
    changed."""
    from django.http import HttpResponse
    from django.test import RequestFactory

    client = Client()
    client.force_login(a["users"]["editor"])
    old = key(client)
    request = RequestFactory().post("/api/v1/applications")
    request.COOKIES[COOKIE] = old
    middleware = sessions.SessionMiddleware(lambda r: HttpResponse(status=201))
    middleware.process_request(request)
    request.session["late"] = "value"  # read live, then changed by the view
    sessions.SessionStore().delete(old)  # another tab signs out meanwhile
    caplog.set_level("INFO", logger="softwarden.sessions")
    response = middleware.process_response(request, HttpResponse(status=201))
    assert response.status_code == 400
    assert json.loads(response.content)["detail"] == sessions.SAVE_INTERRUPTED
    assert COOKIE not in response.cookies
    assert [r.levelname for r in caplog.records if r.name == "softwarden.sessions"] == ["INFO"]
