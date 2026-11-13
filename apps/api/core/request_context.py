"""The unit of work audit events are grouped in: one HTTP request, one job, one schedule run or
one management command.

Each unit has a request id, written on every AuditEvent it produces, and a store of "before" snapshots
(catalog/snapshots.py), so an event can carry the target as it was when the unit first touched it.

- HTTP: RequestIdMiddleware opens a unit per request. The id is the X-Request-ID header when the request came through
  a trusted proxy (settings.TRUSTED_PROXIES; Caddy sets the header to its own request UUID and overwrites a client's),
  otherwise a new UUID. The response carries it back as X-Request-ID. The proxy check (which may resolve a name) runs
  only when the header is present and a UUID, and never for the health probes.
- Worker: core/jobs.py opens a unit per job (the job's id) and per schedule run (a new UUID).
- Anything else (a management command, the shell): the first audit event picks a new id, which then holds for the
  rest of the process's context, so one command's events share an id.

Log lines (softwarden/logs.py) carry the same id, plus the fields of the current `log_context`: for a request the
path (never the query string, where the calendar feed's secret travels) and method, then the organization and user
ids once known (`note_log`, called by core.access when the membership is found); for a job its organization id.
RequestIdMiddleware writes one `softwarden.request` line per request with its status and duration.
"""

import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from django.utils.functional import SimpleLazyObject, empty

from .middleware import PROBE_PATHS, is_trusted_proxy

request_log = logging.getLogger("softwarden.request")

HEADER = "X-Request-ID"

_request_id: ContextVar[uuid.UUID | None] = ContextVar("softwarden_request_id", default=None)
_snapshots: ContextVar[dict[Any, Any] | None] = ContextVar("softwarden_snapshots", default=None)
# Who an actor-less write is attributed to (acting_via): merged into every audit payload written meanwhile.
_via: ContextVar[dict[str, Any] | None] = ContextVar("softwarden_audit_via", default=None)
# The platform staff account (User id) a request impersonates for (core/impersonation.py): written on every audit
# event as `impersonated_by` meanwhile.
_impersonator: ContextVar[int | None] = ContextVar("softwarden_impersonator", default=None)
# What every log line written in the current request or job adds (log_context): ids and the path, never a name.
_log: ContextVar[dict[str, str] | None] = ContextVar("softwarden_log_fields", default=None)
LOG_FIELDS = ("request_id", "org", "user", "path", "method")


def current_request_id() -> uuid.UUID:
    """The current unit's id; outside any unit (a command), a new one that the rest of the context keeps."""
    value = _request_id.get()
    if value is None:
        value = uuid.uuid4()
        _request_id.set(value)
    return value


def snapshot_store() -> dict[Any, Any]:
    """The current unit's "before" snapshots, keyed by (model label, primary key). Outside any unit, one store for the
    rest of the context (a command)."""
    store = _snapshots.get()
    if store is None:
        store = {}
        _snapshots.set(store)
    return store


def organization_cache(org_id) -> dict[str, Any]:
    """Facts about one organization that a unit reads more than once (for example whether its compliance profile is
    on, catalog/compliance.py), keyed by the reader. Whatever changes an organization's edition or settings calls
    forget_organization, so the rest of the unit reads them again."""
    return snapshot_store().setdefault(("organization", str(org_id)), {})


def forget_organization(org_id) -> None:
    snapshot_store().pop(("organization", str(org_id)), None)


@contextmanager
def unit_of_work(request_id: uuid.UUID | None = None) -> Iterator[uuid.UUID]:
    """A fresh request id and snapshot store until the block ends; the previous ones come back afterwards."""
    value = request_id or uuid.uuid4()
    id_token = _request_id.set(value)
    store_token = _snapshots.set({})
    via_token = _via.set(_via.get())  # what note_via adds inside the unit ends with it
    try:
        yield value
    finally:
        _via.reset(via_token)
        _snapshots.reset(store_token)
        _request_id.reset(id_token)


@contextmanager
def acting_via(**fields: Any) -> Iterator[None]:
    """Attribute every audit event written in the block to a non-person actor, for example a SCIM token
    (`via="scim", scim_client=<token name>`, ee/scim): catalog/services.audit and audit_account add these fields to the
    event's payload, nested writes (a membership removed, an invitation made) included. The event's actor stays null,
    as for the CLI and jobs. Keys must avoid the audit denylist words (catalog/redaction.py)."""
    token = _via.set({**(_via.get() or {}), **fields})
    try:
        yield
    finally:
        _via.reset(token)


def note_via(**fields: Any) -> None:
    """Like acting_via, for the rest of the current unit of work (the request), when the fields are known only partway
    through it: api/tokens.py notes the API token that authenticated the request, so every audit event the
    request writes names it beside the token's owner as actor. unit_of_work undoes it when the unit ends. Keys must
    avoid the audit denylist words (catalog/redaction.py)."""
    _via.set({**(_via.get() or {}), **fields})


def audit_via() -> dict[str, Any]:
    return dict(_via.get() or {})


@contextmanager
def impersonating(staff_id: int | None) -> Iterator[None]:
    """Every audit event written in the block records `impersonated_by = staff_id` (None: nobody, also inside an
    impersonated request, for the events about the impersonation itself)."""
    token = _impersonator.set(staff_id)
    try:
        yield
    finally:
        _impersonator.reset(token)


def current_impersonator() -> int | None:
    return _impersonator.get()


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Add `fields` (LOG_FIELDS only, None skipped) to every log line written in the block. A nested block copies the
    outer fields, so what it notes stays inside it."""
    unknown = set(fields) - set(LOG_FIELDS)
    if unknown:
        raise ValueError(f"Unknown log fields: {', '.join(sorted(unknown))}")
    token = _log.set({**(_log.get() or {}), **{k: str(v) for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _log.reset(token)


def note_log(**fields: Any) -> None:
    """Add fields to the current log_context once they are known (the organization and user, found after the request
    started). Outside a log_context it does nothing."""
    current = _log.get()
    if current is None:
        return
    unknown = set(fields) - set(LOG_FIELDS)
    if unknown:
        raise ValueError(f"Unknown log fields: {', '.join(sorted(unknown))}")
    current.update({k: str(v) for k, v in fields.items() if v is not None})


def log_fields() -> dict[str, str]:
    """The fields the log formatter adds to a line: the current log_context, whose request id (the request's, the
    job's) wins; else the current unit's id, as in a management command once it has written an audit event. Never
    starts a unit (unlike current_request_id), so logging doesn't change which id a command's audit events get."""
    fields = dict(_log.get() or {})
    request_id = _request_id.get()
    if "request_id" not in fields and request_id is not None:
        fields["request_id"] = str(request_id)
    return fields


def parse_request_id(value: str | None) -> uuid.UUID | None:
    """A forwarded request id, if it is a UUID (canonical or 32 hex digits, as Caddy and nginx send); else None."""
    if not value or len(value) > 64:
        return None
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        return None


def _note_user(request) -> None:
    """The signed-in user's id for the request line, if authentication already ran: a session user the request read,
    or the user DRF authenticated (DRF sets it on the Django request). Never forces a session read of its own."""
    user = request.__dict__.get("user")
    if user is None or (isinstance(user, SimpleLazyObject) and user._wrapped is empty):
        return
    if getattr(user, "is_authenticated", False):
        note_log(user=user.pk)


class RequestIdMiddleware:
    """Second in the stack, after TrustedProxyMiddleware: every request runs in its own unit of work, and ends with one
    `softwarden.request` log line (method, path without its query string, status, duration; a probe that answers well
    at DEBUG, so the container healthcheck doesn't fill the log)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.monotonic()
        forwarded = None
        probe = request.path_info in PROBE_PATHS
        if not probe:
            forwarded = parse_request_id(request.META.get("HTTP_X_REQUEST_ID"))
            if forwarded is not None and not is_trusted_proxy(request.META.get("REMOTE_ADDR", "")):
                forwarded = None
        with (
            unit_of_work(forwarded) as request_id,
            log_context(request_id=request_id, path=request.path, method=request.method),
        ):
            request.request_id = request_id
            response = self.get_response(request)
            _note_user(request)
            duration_ms = round((time.monotonic() - started) * 1000, 1)
            status = response.status_code
            # Django's levels for a 4xx and a 5xx (django.request). This line replaces Django's own "Not Found: ..."
            # one, which it writes after the middleware has finished, without the request's fields; an exception's
            # traceback is still logged by Django, inside the request.
            level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
            response._has_been_logged = True
            request_log.log(
                logging.DEBUG if probe and status < 400 else level,
                "%s %s %s",
                request.method,
                request.path,
                status,
                extra={"status_code": status, "duration_ms": duration_ms},
            )
        response[HEADER] = str(request_id)
        return response
