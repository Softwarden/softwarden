"""Sessions whose key was rotated away don't sign the browser out (session rotation race).

The session key changes at sign-in (django.contrib.auth.login), on switching organization, and when an impersonation
starts or stops (core/impersonation.py). Before this module, the old key's row was deleted at once. A request sent with
the old cookie but read after the rotation (another tab, a poll, an upload or a download) found no session, and
Django's SessionMiddleware answered it with `Set-Cookie: sessionid=""`: the browser dropped the new cookie that the
rotating response had just set, and was signed out.

Now the old row stays for ROTATED_SECONDS as a tombstone. Its data is the constant ROTATED, followed by `>` and the
key that replaced it when a key change made it (tombstone()). That is not a signed session, so nothing can decode it
into values, and no value of the old session carries over:

- SessionStore.delete (which flush calls) replaces a live row's data with ROTATED and its expiry with ROTATED_SECONDS
  from now, in one UPDATE, instead of deleting it. An existing tombstone isn't extended and an expired session isn't
  turned into one.
- Loading a tombstone behaves exactly as loading an unknown key (an empty session with no key, so the request is
  anonymous and a write to the session creates a new one), and also sets `rotated_away` on the store.
- Saving over an existing key never writes onto a tombstone: a request that read the old session before the rotation
  and changed it gets UpdateError (Django answers 400, as it did when the row was simply gone) instead of bringing the
  old session back to life.
- Cycling the key first turns the old row into a tombstone, and only if that row was still live (not rotated away or
  signed out by a concurrent request since this one read it) creates the new key with the data. Otherwise it raises
  SessionInterrupted without creating anything, and the store becomes rotated away and empty, so the response
  leaves the cookie alone. SessionMiddleware answers a SessionInterrupted with 400 and its message ("... Reload."),
  logged at INFO without a traceback (interrupted_response): it is an expected race, not a fault. Callers keep the
  key change in the same transaction as what it goes with (the audit of a switch, a password change), so both roll
  back; core/impersonation.Middleware answers 409 /problems/impersonation-ended instead when two requests notice an
  expiry at once. Django's own cycle_key inserts the new row unconditionally, which would bring a signed-out
  session back under a new key.
- SessionMiddleware leaves the cookie alone when the session is empty and `rotated_away`: the response neither deletes
  it nor sets one, so the browser keeps whatever the rotation set. A request on a rotated key that starts a session of
  its own (signing in, a single sign-on start, an invitation preview) sets its own new cookie, as any of those does.
  Every other case is Django's (an unknown, expired or long-gone key is still deleted; signing out still deletes the
  cookie on its own response).

Signing out (flush) leaves a tombstone too: a request sent before the sign-out and answered after it is anonymous and
doesn't touch the cookie, which the sign-out response has already deleted. Signing out always signs out: when another
request changed the key after the sign-out read it (a Stop or an organization switch in another tab), the store says so
(`interrupted`: set by cycle_key or flush finding a tombstone where the live row was, never by loading a tombstone),
and catalog/views.logout_view, for a session request only, follows the tombstones' successors to the browser's current
session, ends it (end_successors) and deletes the cookie itself. A key that was already a tombstone when the request
arrived ends nothing: anyone holding an old key could otherwise end the session it leads to.

A SessionInterrupted from the response's save (the view ran, and its writes may be committed) answers 400 with
SAVE_INTERRUPTED instead, which doesn't claim that nothing changed.

Tombstones live in django_session with every other session, so every gunicorn worker and every pod sees them. They are
never loaded once expired, and the daily `sessions.clear` schedule deletes expired sessions and tombstones alike
(clear_expired)."""

from __future__ import annotations

import logging
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.sessions.backends.base import CreateError, UpdateError
from django.contrib.sessions.backends.db import SessionStore as DbSessionStore
from django.contrib.sessions.exceptions import SessionInterrupted
from django.contrib.sessions.middleware import SessionMiddleware as DjangoSessionMiddleware
from django.db import router
from django.utils import timezone
from django.utils.cache import patch_vary_headers

# A tombstone's session_data. Not a signed value, so Django's decode could never turn it into session values.
ROTATED = "softwarden:rotated"
# How long a rotated key is recognised: a request sent with it this long after the rotation still leaves the cookie
# alone. The session is read when a request starts (core/impersonation.Middleware reads it before any view), so this
# only has to cover requests queued in the proxy and the workers, not the length of an upload or a download.
ROTATED_SECONDS = 120
logger = logging.getLogger("softwarden.sessions")
INTERRUPTED = (
    "This session was signed out or changed by another request (another tab) after this one began, so nothing was "
    "changed. Reload."
)
# For a SessionInterrupted from the response's save (SessionMiddleware.process_response): the view has run and its
# writes may be committed, so "nothing was changed" would be wrong.
SAVE_INTERRUPTED = "Your change may have been saved, but this tab's session changed; reload before trying again."


def tombstone(successor: str | None = None) -> str:
    """A tombstone's session_data: ROTATED, then `>` and the key that replaced it when a key change made it."""
    return ROTATED if not successor else f"{ROTATED}>{successor}"


def is_tombstone(session_data: str) -> bool:
    return session_data.startswith(ROTATED)


def successor_of(session_data: str) -> str | None:
    """The key that replaced a tombstone's session (cycle_key), or None (flush, or not a tombstone)."""
    if not is_tombstone(session_data):
        return None
    return session_data[len(ROTATED) + 1 :] or None


def end_successors(session_key: str | None, hops: int = 10) -> None:
    """End the live session this browser's key leads to: follow the tombstones' successors from `session_key` (one
    per key change, at most `hops`) and turn the first live session found into a tombstone. For a sign-out whose own
    key was changed by a concurrent request (a Stop in another tab): the browser's current session ends too."""
    model = SessionStore.get_model_class()
    store = SessionStore()
    for _ in range(hops):
        if not session_key:
            return
        row = model.objects.filter(session_key=session_key).only("session_data").first()
        if row is None:
            return
        if not is_tombstone(row.session_data):
            store.delete(session_key)
            return
        session_key = successor_of(row.session_data)


class SessionStore(DbSessionStore):
    """Django's database session store, keeping a rotated-away key as a tombstone (see the module docstring)."""

    rotated_away = False
    # Set only when this request read the session live and another request then changed its key or signed it out
    # (cycle_key or delete finding a tombstone where the live row was). Loading a tombstone never sets it: a key
    # someone else holds must not let a request end the session it leads to (catalog/views.logout_view).
    interrupted = False

    def _tombstone(self, row):
        """None (and `rotated_away`, and no key) when `row` is a tombstone; else the row."""
        if row is not None and is_tombstone(row.session_data):
            self.rotated_away = True
            self._session_key = None
            return None
        return row

    def _get_session_from_db(self):
        return self._tombstone(super()._get_session_from_db())  # type: ignore[misc]  # not in django-stubs

    async def _aget_session_from_db(self):
        return self._tombstone(await super()._aget_session_from_db())  # type: ignore[misc]  # not in django-stubs

    def _entomb(self, session_key: str, successor: str | None = None) -> int:
        """Turn a live session's row into a tombstone in one UPDATE, naming the key that replaced it (cycle_key) or
        none (flush); the number of rows changed (0 or 1)."""
        now = timezone.now()
        return (
            self.model.objects.filter(session_key=session_key, expire_date__gt=now)
            .exclude(session_data__startswith=ROTATED)
            .update(session_data=tombstone(successor), expire_date=now + timedelta(seconds=ROTATED_SECONDS))
        )

    def delete(self, session_key=None):
        own = session_key is None
        if own:
            if self.session_key is None:
                return
            session_key = self.session_key
        if not self._entomb(session_key) and own:
            # This request's own session (flush, at sign-out): when another request has made it a tombstone since this
            # one read it (an organization switch in another tab), say so; an expired or missing row isn't that.
            row = self.model.objects.filter(session_key=session_key).only("session_data").first()
            if row is not None and is_tombstone(row.session_data):
                self.interrupted = True

    async def adelete(self, session_key=None):
        await sync_to_async(self.delete)(session_key)

    def cycle_key(self):
        """Django's, except that the old row must still be live: it becomes a tombstone first, and a session rotated
        away or signed out by a concurrent request since this one read it is never created again under a new key."""
        data = dict(self.items())
        old = self.session_key
        new = self._get_new_session_key()  # type: ignore[attr-defined]  # Django's, not in django-stubs
        if old is not None and not self._entomb(old, successor=new):
            self._session_key = None
            self._session_cache = {}
            self.rotated_away = True
            self.interrupted = True
            raise SessionInterrupted(INTERRUPTED)
        self._session_cache = data
        while True:
            self._session_key = new
            try:
                self.save(must_create=True)
            except CreateError:  # another request took the key meanwhile: pick another, and point the tombstone at it
                new = self._get_new_session_key()  # type: ignore[attr-defined]
                if old is not None:
                    self.model.objects.filter(session_key=old).update(session_data=tombstone(new))
                continue
            break
        self.modified = True

    async def acycle_key(self):
        await sync_to_async(self.cycle_key)()

    def save(self, must_create=False):
        if must_create or self.session_key is None:
            return super().save(must_create=must_create)
        obj = self.create_model_instance(dict(self.items()))
        using = router.db_for_write(self.model, instance=obj)
        updated = (
            self.model.objects.using(using)
            .filter(session_key=obj.session_key)
            .exclude(session_data__startswith=ROTATED)
            .update(session_data=obj.session_data, expire_date=obj.expire_date)
        )
        if not updated:  # gone, or rotated away while this request ran: never written back
            raise UpdateError
        return None

    async def asave(self, must_create=False):
        if must_create or self.session_key is None:
            return await super().asave(must_create=must_create)
        return await sync_to_async(self.save)(must_create)


def clear_expired(now=None):
    """Delete expired sessions and tombstones (Django's clear_expired, counted). A daily schedule (core/apps.py)."""
    now = now or timezone.now()
    deleted, _ = SessionStore.get_model_class().objects.filter(expire_date__lt=now).delete()
    return {"sessions_deleted": deleted}


def delete_for_users(user_ids, now=None) -> int:
    """Delete every live session signed in as one of `user_ids` (an organization's purge erasing its accounts,
    catalog/org_lifecycle._release_accounts). Sessions aren't indexed by user, so this decodes
    each live one; purges are rare. Tombstones decode to nothing and are left to expire. Returns how many it deleted."""
    wanted = {str(pk) for pk in user_ids}
    if not wanted:
        return 0
    model = SessionStore.get_model_class()
    store = SessionStore()
    doomed = []
    live = model.objects.filter(expire_date__gte=now or timezone.now()).values_list("session_key", "session_data")
    for key, data in live.iterator(chunk_size=2000):
        if is_tombstone(data):
            continue
        if str(store.decode(data).get("_auth_user_id", "")) in wanted:
            doomed.append(key)
    deleted, _ = model.objects.filter(session_key__in=doomed).delete()
    return deleted


def delete_cookie(response) -> None:
    """Delete the session cookie on `response`, with the attributes SessionMiddleware sets it with."""
    response.delete_cookie(
        settings.SESSION_COOKIE_NAME,
        path=settings.SESSION_COOKIE_PATH,
        domain=settings.SESSION_COOKIE_DOMAIN,
        samesite=settings.SESSION_COOKIE_SAMESITE,
    )


def interrupted_response(request, message: str):
    """The answer to a request whose session another request changed meanwhile: 400 with the reason (which ends in
    reloading), logged at INFO without a traceback, because it is an expected race, not a fault."""
    from .problems import MALFORMED_REQUEST, is_api, problem_response

    logger.info("Session changed by another request: %s %s", request.method, request.path)
    if not is_api(request):
        from django.http import HttpResponseBadRequest

        return HttpResponseBadRequest(message)
    return problem_response(400, message, MALFORMED_REQUEST)


class SessionMiddleware(DjangoSessionMiddleware):
    """Django's, except that a request whose session key was rotated away doesn't delete the session cookie, and a
    SessionInterrupted (a view's key change, or the response's save, losing a race) answers interrupted_response."""

    def process_exception(self, request, exception):
        if isinstance(exception, SessionInterrupted):
            return interrupted_response(request, str(exception))
        return None

    def process_response(self, request, response):
        session = getattr(request, "session", None)
        if (
            session is not None
            and getattr(session, "rotated_away", False)
            and settings.SESSION_COOKIE_NAME in request.COOKIES
            and session.is_empty()
        ):
            if session.accessed:
                patch_vary_headers(response, ("Cookie",))
            return response
        try:
            return super().process_response(request, response)
        except SessionInterrupted:  # the save found the session rotated away: the session isn't written, no cookie
            return interrupted_response(request, SAVE_INTERRUPTED)
