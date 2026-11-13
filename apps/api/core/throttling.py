"""API rate limits per token and per user (docs/API.md "Rate limits").

Every signed-in request counts against one bucket, chosen by how it authenticated:
- an API token: the token's own bucket (`token:<id>`), so one script exhausting its token never slows the same
  person's other tokens or their browser session;
- a session: the user's bucket (`user:<id>`), shared by that user's tabs.

Reads (GET, HEAD, OPTIONS) and writes have separate buckets, and writes are stricter: settings.API_READ_RATE and
API_WRITE_RATE (SOFTWARDEN_API_READ_RATE, SOFTWARDEN_API_WRITE_RATE; DRF rate syntax such as `1200/min`; empty: no
limit). Requests that aren't signed in (sign-in, setup, the calendar feed, health) are left to their own scoped
throttles. A refused request is the 429 `/problems/rate-limited` problem with Retry-After (core/problems.py).

Beside those, every request acting in an organization also counts against that organization's bucket (`org:<id>`,
OrgRateThrottle), at the rate of its `api_rate_read` / `api_rate_write` limit (core/limits.py). Only when the
limit is set: community and enterprise are unlimited by default, cloud has 1,200 reads and 300 writes a minute.

Every throttle here counts in the shared PostgreSQL counters (core/counters.py), not in
Django's per-process cache: SharedRateThrottle replaces DRF's history-in-the-cache `allow_request` with one counter
upsert per request, so a limit holds whichever worker or pod serves the request. The windows are fixed (aligned to
the rate's period), not DRF's sliding history. ScopedThrottle and UserThrottle are DRF's ScopedRateThrottle and
UserRateThrottle counted the same way; every view throttle in the project uses one of them. The scoped throttles a
view sets for itself (search, uploads, sign-in) add to the API buckets: `with_api_limits` keeps them in the list.

Client addresses are DRF's `get_ident` with NUM_PROXIES = 1: the last X-Forwarded-For entry, which is the one the
trusted proxy wrote (core/middleware.TrustedProxyMiddleware drops the header from anyone else), never an entry a
client wrote further left.

Kept free of model and DRF view imports: DRF loads DEFAULT_THROTTLE_CLASSES while defining APIView."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle, UserRateThrottle

from . import counters

SAFE = frozenset({"GET", "HEAD", "OPTIONS"})
COUNTER_SCOPE = "throttle"  # counters scope: `throttle:<the throttle's scope>`, e.g. `throttle:login`


class SharedRateThrottle(SimpleRateThrottle):
    """DRF's SimpleRateThrottle counted in the shared counters (core/counters.py): `get_cache_key` names the bucket as
    before, and the bucket's count for the current window is one upsert. Refused requests count too (it makes no
    difference within a fixed window). `wait` is the seconds until the window ends."""

    retry_after: int | None = None
    num_requests: int | None
    duration: int | None

    def allow_request(self, request, view):
        if self.rate is None:
            return True
        key = self.get_cache_key(request, view)
        if key is None or self.num_requests is None or self.duration is None:
            return True
        scope = f"{COUNTER_SCOPE}:{getattr(self, 'scope', None) or type(self).__name__}"[:40]
        result = counters.hit(scope, key, int(self.duration), int(self.num_requests))
        self.retry_after = result.retry_after
        return result.allowed

    def wait(self):
        return self.retry_after


class ScopedThrottle(ScopedRateThrottle, SharedRateThrottle):
    """DRF's ScopedRateThrottle (the view's `throttle_scope`, its rate in DEFAULT_THROTTLE_RATES; the user's bucket when
    signed in, the client address's otherwise), counted in the shared counters."""


class LookupThrottle(ScopedThrottle):
    """ScopedThrottle for `auth/options` lookups only: a request naming an organization (`org`) or an
    email domain (`email`) is counted; the sign-in page's plain load isn't."""

    def allow_request(self, request, view):
        params = request.query_params
        if not (params.get("org") or params.get("email")):
            return True
        return super().allow_request(request, view)


class UserThrottle(UserRateThrottle, SharedRateThrottle):
    """DRF's UserRateThrottle (the user's bucket, or the address's when nobody is signed in), counted in the shared
    counters."""


def bucket(request) -> str | None:
    """`token:<id>` for an API token, `user:<id>` for a session, None when nobody is signed in."""
    token = getattr(request, "auth", None)
    token_id = getattr(token, "id", None)
    if token_id is not None and getattr(token, "token_hash", None) is not None:  # an api.models.ApiToken
        return f"token:{token_id}"
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return f"user:{user.pk}"
    return None


class ApiRateThrottle(SharedRateThrottle):
    """The read or write bucket of the request's token or user."""

    cache_format = "throttle_api_%(scope)s_%(ident)s"
    rate: str | None

    def __init__(self):  # the rate depends on the request's method, so it is read in allow_request
        self.rate = None
        self.num_requests, self.duration = None, None

    @staticmethod
    def scope_for(request) -> str:
        return "read" if request.method in SAFE else "write"

    def get_rate(self):
        return None

    def allow_request(self, request, view):
        self.scope = self.scope_for(request)
        self.rate = (settings.API_READ_RATE if self.scope == "read" else settings.API_WRITE_RATE) or None
        if self.rate is None:
            return True
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        ident = bucket(request)
        if ident is None:
            return None  # not signed in: no API bucket
        return self.cache_format % {"scope": self.scope, "ident": ident}


class OrgRateThrottle(SharedRateThrottle):
    """The organization's own bucket (`org:<id>`, limits): every token and user of one organization together,
    at the rate of its `api_rate_read` or `api_rate_write` limit (core/limits.py, requests per minute), on top of the
    token or user bucket above. Skipped when that limit is None (unlimited, as in community and enterprise by
    default) and for requests acting in no organization (sign-in, the account's own routes, the platform console).
    The organization is the membership the request's permission check found (core/access.current_membership), so it
    costs no query beyond its counter."""

    cache_format = "throttle_org_%(scope)s_%(ident)s"
    rate: str | None

    def __init__(self):
        self.rate = None
        self.num_requests, self.duration = None, None

    def get_rate(self):
        return None

    def allow_request(self, request, view):
        membership = getattr(request, "softwarden_membership", None)
        if membership is None:
            return True
        from . import limits  # models: not at import time (see the module docstring)

        self.scope = ApiRateThrottle.scope_for(request)
        name = limits.API_RATE_READ if self.scope == "read" else limits.API_RATE_WRITE
        per_minute = limits.limits(membership.organization)[name]
        if per_minute is None:
            return True
        self.organization_id = membership.organization_id
        self.rate = f"{int(per_minute)}/min"
        self.num_requests, self.duration = int(per_minute), 60
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": f"org:{self.organization_id}"}


def with_api_limits(*throttles: Any) -> list[Any]:
    """A view's throttle_classes: the API buckets (token or user, and organization) plus the view's own scoped
    throttles."""
    return [ApiRateThrottle, OrgRateThrottle, *throttles]
