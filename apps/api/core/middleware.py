"""Forwarded headers are believed only from the reverse proxy.

Caddy (compose.yaml `proxy`) terminates TLS and sets X-Forwarded-For to the client's address and X-Forwarded-Proto to
`https`. Anything else that reaches the app could send the same headers to fake a client address (and dodge a rate
limit) or claim HTTPS. So this middleware, first in the stack, removes them unless the immediate peer is in
settings.TRUSTED_PROXIES; after it, SECURE_PROXY_SSL_HEADER and the last X-Forwarded-For address can be trusted.

Proxy names (compose's `proxy`) are resolved, and a name lookup must never hold requests up: at first start Compose
starts Caddy only after this container is healthy, so `proxy` doesn't resolve yet, and on Docker Desktop an
unresolvable name takes seconds (a start-up deadlock found in testing). Hence:
- The check runs only when a request carries a header it would decide about (X-Forwarded-*, X-Request-ID), so the
  container healthcheck and other direct requests never resolve anything; the health probes never consult it at all.
- A good answer is kept RESOLVE_TTL seconds, a failed or empty one FAILURE_TTL seconds (short, so a proxy that has
  just started is trusted soon, but a missing one costs at most one lookup per window).
- One background thread per name does the lookup (never one per request). The request that starts it waits at most
  RESOLVE_WAIT seconds; it and any request arriving meanwhile then use the last known answer until the lookup
  finishes, but never one older than STALE_LIMIT (then, and after a failed lookup, trust nobody).
- A failing name is logged once at warning when it starts failing, and once at info when it resolves again."""

import ipaddress
import logging
import socket
import threading
import time

from django.conf import settings

logger = logging.getLogger(__name__)

FORWARDED = ("HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_PROTO", "HTTP_X_FORWARDED_HOST", "HTTP_X_FORWARDED_PORT")
# Liveness and readiness probes never depend on proxy resolution: they ignore forwarded headers (they use none).
PROBE_PATHS = frozenset({"/healthz", "/readyz", "/readyz/web"})
RESOLVE_TTL = 30.0  # seconds; the proxy container's address can change when it is recreated
FAILURE_TTL = 15.0  # seconds; a name that didn't resolve is asked again after this
RESOLVE_WAIT = 1.0  # seconds a request waits for a lookup in progress before using the last known answer

Networks = list[ipaddress.IPv4Network | ipaddress.IPv6Network]
LogLine = tuple[int, str, tuple[object, ...]] | None

_now = time.monotonic  # module-level so tests can move the clock without touching time.monotonic
_lock = threading.Lock()
# name -> (when it was resolved or failed, networks); [] while the name fails. Fresh for RESOLVE_TTL after a good
# answer and FAILURE_TTL after a failure; a stale good answer is still served while a refresh runs, up to STALE_LIMIT.
_resolved: dict[str, tuple[float, Networks]] = {}
_pending: dict[str, threading.Thread] = {}  # name -> its lookup in progress
_failing: set[str] = set()  # names whose last lookup failed (already logged)
STALE_LIMIT = 2 * RESOLVE_TTL


def _lookup(entry: str) -> Networks:
    try:
        infos = socket.getaddrinfo(entry, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    nets: Networks = []
    for info in infos:
        try:
            # An IPv6 link-local result can carry a zone ("fe80::1%eth0"); the zone isn't part of the address.
            nets.append(ipaddress.ip_network(str(info[4][0]).split("%", 1)[0]))
        except ValueError:
            continue
    return nets


def _record(entry: str, nets: Networks) -> LogLine:
    """Store an answer (caller holds _lock); returns the log line to emit once the lock is released, if any."""
    _resolved[entry] = (_now(), nets)
    if nets:
        if entry in _failing:
            _failing.discard(entry)
            return (logging.INFO, "Trusted proxy %r resolves again", (entry,))
        return None
    # Unknown name (no proxy here yet): trust nothing for it until it resolves.
    if entry in _failing:
        return None
    _failing.add(entry)
    return (
        logging.WARNING,
        "Trusted proxy %r does not resolve; forwarded headers from it are ignored until it does (retried every %d s). "
        "Set SOFTWARDEN_TRUSTED_PROXIES to an address or range to avoid lookups.",
        (entry, FAILURE_TTL),
    )


def _emit(line: LogLine) -> None:
    if line is not None:
        logger.log(line[0], line[1], *line[2])


def _refresh(entry: str) -> None:
    try:
        nets = _lookup(entry)
    except Exception:  # never let the lookup thread die without recording an answer
        nets = []
    with _lock:
        line = _record(entry, nets)
        _pending.pop(entry, None)
    _emit(line)


def _usable(cached: tuple[float, Networks] | None) -> Networks:
    """The last known answer while a refresh runs, unless it is older than STALE_LIMIT."""
    if cached is None or _now() - cached[0] >= STALE_LIMIT:
        return []
    return cached[1]


def _networks(entry: str) -> Networks:
    try:
        return [ipaddress.ip_network(entry, strict=False)]
    except ValueError:
        pass
    with _lock:
        cached = _resolved.get(entry)
        if cached and _now() - cached[0] < (RESOLVE_TTL if cached[1] else FAILURE_TTL):
            return cached[1]
        if entry in _pending:  # another request is already waiting on this lookup: don't wait as well
            return _usable(cached)
        thread = threading.Thread(target=_refresh, args=(entry,), name=f"resolve-{entry}", daemon=True)
        started = True
        line: LogLine = None
        try:
            thread.start()
        except RuntimeError:  # can't start a thread (interpreter shutting down, thread limit): a failed lookup
            started, line = False, _record(entry, [])
        else:
            _pending[entry] = thread  # the thread's _refresh waits for this lock, so it can't finish first
    if not started:
        _emit(line)
        return []
    thread.join(RESOLVE_WAIT)
    with _lock:
        cached = _resolved.get(entry)
        return _usable(cached)  # fresh, or the last known answer while a slow lookup finishes


def is_trusted_proxy(address: str) -> bool:
    if not settings.TRUSTED_PROXIES:
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for entry in settings.TRUSTED_PROXIES for net in _networks(entry))


class TrustedProxyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        meta = request.META
        # Only a request carrying a forwarded header needs the check (and maybe a name lookup); probes skip it.
        if any(name in meta for name in FORWARDED) and (
            request.path_info in PROBE_PATHS or not is_trusted_proxy(meta.get("REMOTE_ADDR", ""))
        ):
            for name in FORWARDED:
                meta.pop(name, None)
        return self.get_response(request)


def client_ip(request) -> str:
    """The client's address: the last X-Forwarded-For entry (what the trusted proxy saw; the middleware removed the
    header otherwise), else the peer address. Same rule as DRF's throttles with NUM_PROXIES = 1. No lookup here: the
    middleware already decided."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return request.META.get("REMOTE_ADDR", "") or "unknown"
