"""Reading (and, when allowed, creating) tickets in Jira, ServiceNow and HaloITSM through a TicketConnection.

Endpoints (docs/PERMISSIONS.md "Ticket connections" records where each was checked):

- Jira Cloud REST v3, HTTP basic auth with the account email and an API token:
  `GET {base}/rest/api/3/issue/{key}?fields=status,summary` (status name and its category key: `new`,
  `indeterminate`, `done`); `GET {base}/rest/api/3/myself` for the connection test; `POST {base}/rest/api/3/issue`
  to create (project key, issue type name, summary, description as an Atlassian Document Format paragraph).
- ServiceNow Table API, HTTP basic auth:
  `GET {base}/api/now/table/{table}?sysparm_query=number={key}&sysparm_fields=number,state,short_description
  &sysparm_display_value=all&sysparm_limit=1` (the table follows the number's prefix: INC incident, CHG change_request,
  RITM sc_req_item, ...); `GET {base}/api/now/table/task?sysparm_limit=1&sysparm_fields=number` for the test;
  `POST {base}/api/now/table/{table}` to create.
- HaloITSM: a client-credentials token from the Halo connection (core/halo.py), then `GET {base}/api/Tickets/{id}`
  (`summary`, `status_id`); `POST {base}/api/Tickets` with a one-ticket array to create.

Every request goes through core/outbound under the service rule (https only, no user name or password in the URL, the
host resolved once and refused when it is loopback, link-local or a metadata address, and private networks too when
SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS is off; no redirects; a wall-clock deadline). The address is always the
connection's own base URL; a link's stored URL is never fetched. Hosts ending `.simulated.invalid` select the
synthetic systems in tickets/simulator.py, so tests and the demo never leave the machine.

Errors are TicketError with a kind and a message in Softwarden's words: never the server's text, a token or a secret.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from core import halo, outbound, simulation
from core.connections import ProbeResult
from core.graph import Reply, Unreachable

from .models import TicketStatus, TicketSystem
from .parsing import servicenow_table

TIMEOUT = 10  # per socket operation
DEADLINE = 20.0  # a whole request
BODY_MAX = 256 * 1024
TITLE_MAX = 300
LABEL_MAX = 100
SIMULATED_SUFFIX = ".simulated.invalid"
HALO_READ_SCOPES = ("read:tickets", "all")


class TicketError(Exception):
    """A ticket couldn't be read or created. `kind`: refused (address not allowed), unreachable, timeout, auth (the
    credentials were refused), forbidden, not_found, busy, bad_answer, not_configured. `ambiguous`: a create may have
    reached the system (the answer was lost). The message is safe to show, store and log."""

    def __init__(self, kind: str, message: str, *, ambiguous: bool = False):
        super().__init__(message)
        self.kind = kind
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class Credential:
    system: str
    base_url: str
    username: str = ""
    secret: str = field(default="", repr=False)
    client_id: str = ""  # Halo
    scope: str = ""  # Halo


@dataclass(frozen=True)
class TicketState:
    status: str
    external_status: str
    title: str


@dataclass(frozen=True)
class Created:
    key: str
    url: str
    state: TicketState


class Transport(Protocol):
    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Reply: ...

    def post_form(self, url: str, form: Mapping[str, str]) -> Reply: ...


class HttpTransport:
    """core/outbound under the service rule."""

    def send(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None) -> Reply:
        return outbound.request(method, url, body, headers, TIMEOUT, DEADLINE, private_ok=True, body_max=BODY_MAX)

    def post_form(self, url: str, form: Mapping[str, str]) -> Reply:
        return halo.HttpTransport().post_form(url, form)


def is_simulated(base_url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return False
    return host.endswith(SIMULATED_SUFFIX)


def transport_for(base_url: str) -> Transport:
    """Overridable in tests. A `*.simulated.invalid` address never leaves the machine, and is refused unless
    core/simulation.allowed()."""
    if is_simulated(base_url):
        if not simulation.allowed():
            raise TicketError("refused", simulation.MESSAGE)
        from . import simulator

        return simulator.Transport()
    return HttpTransport()


# ---- status mapping -----------------------------------------------------------------------------------------------

JIRA_CATEGORIES = {"new": TicketStatus.OPEN, "indeterminate": TicketStatus.IN_PROGRESS}

# ServiceNow state labels (lower case) across incident, change, request, problem and task tables.
SERVICENOW_LABELS = {
    "new": TicketStatus.OPEN,
    "open": TicketStatus.OPEN,
    "draft": TicketStatus.OPEN,
    "in progress": TicketStatus.IN_PROGRESS,
    "work in progress": TicketStatus.IN_PROGRESS,
    "on hold": TicketStatus.IN_PROGRESS,
    "pending": TicketStatus.IN_PROGRESS,
    "awaiting user info": TicketStatus.IN_PROGRESS,
    "assess": TicketStatus.IN_PROGRESS,
    "authorize": TicketStatus.IN_PROGRESS,
    "scheduled": TicketStatus.IN_PROGRESS,
    "implement": TicketStatus.IN_PROGRESS,
    "review": TicketStatus.IN_PROGRESS,
    "root cause analysis": TicketStatus.IN_PROGRESS,
    "fix in progress": TicketStatus.IN_PROGRESS,
    "resolved": TicketStatus.RESOLVED,
    "closed": TicketStatus.CLOSED,
    "closed complete": TicketStatus.CLOSED,
    "closed incomplete": TicketStatus.CLOSED,
    "closed skipped": TicketStatus.CLOSED,
    "complete": TicketStatus.CLOSED,
    "canceled": TicketStatus.CLOSED,
    "cancelled": TicketStatus.CLOSED,
}
# The incident table's state values, for an answer without display values.
SERVICENOW_INCIDENT_VALUES = {
    "1": TicketStatus.OPEN,
    "2": TicketStatus.IN_PROGRESS,
    "3": TicketStatus.IN_PROGRESS,
    "6": TicketStatus.RESOLVED,
    "7": TicketStatus.CLOSED,
    "8": TicketStatus.CLOSED,
}
# Halo's built-in statuses: 1 New and 9 Closed are fixed; every other status is a stage of work in progress.
HALO_NEW, HALO_CLOSED = 1, 9
HALO_LABELS = {
    "new": TicketStatus.OPEN,
    "open": TicketStatus.OPEN,
    "resolved": TicketStatus.RESOLVED,
    "closed": TicketStatus.CLOSED,
    "cancelled": TicketStatus.CLOSED,
    "completed": TicketStatus.CLOSED,
}


def _label(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:LABEL_MAX]


def _title(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:TITLE_MAX]


def jira_status(status: Any) -> tuple[str, str]:
    """(normalised, Jira's status name) from an issue's `fields.status`."""
    if not isinstance(status, dict):
        return TicketStatus.UNKNOWN, ""
    name = _label(status.get("name"))
    category = status.get("statusCategory")
    key = category.get("key") if isinstance(category, dict) else None
    if key == "done":
        return (TicketStatus.RESOLVED if name.lower() == "resolved" else TicketStatus.CLOSED), name
    return JIRA_CATEGORIES.get(key if isinstance(key, str) else "", TicketStatus.UNKNOWN), name


def servicenow_status(state: Any, table: str) -> tuple[str, str]:
    """(normalised, ServiceNow's state label) from `state`, a display string or {value, display_value}."""
    value = ""
    if isinstance(state, dict):
        value = str(state.get("value") or "")
        label = _label(state.get("display_value"))
    else:
        label = _label(state)
    found = SERVICENOW_LABELS.get(label.lower())
    if found is not None:
        return found, label
    if table == "incident" and (value or label) in SERVICENOW_INCIDENT_VALUES:
        return SERVICENOW_INCIDENT_VALUES[value or label], label or value
    return TicketStatus.UNKNOWN, label or value


def halo_status(ticket: dict[str, Any]) -> tuple[str, str]:
    """(normalised, Halo's status name when it sent one) from a ticket."""
    name = _label(ticket.get("status_name") or ticket.get("statusname"))
    if name.lower() in HALO_LABELS:
        return HALO_LABELS[name.lower()], name
    status_id = ticket.get("status_id")
    if isinstance(status_id, bool) or not isinstance(status_id, int):
        return TicketStatus.UNKNOWN, name
    if status_id == HALO_NEW:
        return TicketStatus.OPEN, name or "New"
    if status_id == HALO_CLOSED:
        return TicketStatus.CLOSED, name or "Closed"
    return TicketStatus.IN_PROGRESS, name or f"Status {status_id}"


# ---- requests -----------------------------------------------------------------------------------------------------

NAMES: dict[str, str] = {TicketSystem.JIRA: "Jira", TicketSystem.SERVICENOW: "ServiceNow", TicketSystem.HALO: "Halo"}


def _basic(credential: Credential) -> str:
    raw = f"{credential.username}:{credential.secret}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _url(credential: Credential, path: str, query: Mapping[str, str] | None = None) -> str:
    url = credential.base_url.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(dict(query), safe=",=")
    return url


def _send(
    transport: Transport, credential: Credential, method: str, url: str, headers: dict[str, str], body: Any = None
) -> Reply:
    name = NAMES.get(credential.system, "The system")
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers = {**headers, "Content-Type": "application/json"}
    try:
        return transport.send(method, url, {"Accept": "application/json", **headers}, data)
    except outbound.UnsafeDestination:
        raise TicketError("refused", outbound.SERVICE_REFUSED) from None
    except Unreachable as error:
        timed_out = "within" in str(error) or "Timeout" in str(error)
        kind = "timeout" if timed_out else "unreachable"
        what = f"{name} didn't answer in time." if timed_out else f"Couldn't reach {name} from this server."
        raise TicketError(kind, what, ambiguous=error.ambiguous) from None


def _check(reply: Reply, credential: Credential, *, what: str = "the ticket") -> Any:
    """The JSON body of a successful answer; TicketError otherwise (never with the server's text)."""
    name = NAMES.get(credential.system, "The system")
    if reply.status == 401:
        raise TicketError("auth", f"{name} refused the connection's credentials.")
    if reply.status == 403:
        raise TicketError("forbidden", f"{name} refused access to {what}: check the account's permissions.")
    if reply.status == 404:
        raise TicketError("not_found", f"{name} has no {what} with this key, or the account can't see it.")
    if reply.status == 429 or reply.status >= 500:
        raise TicketError("busy", f"{name} is busy or unavailable ({reply.status}). Try again later.")
    if not reply.ok:
        raise TicketError("bad_answer", f"{name} refused the request ({reply.status}).")
    body = reply.json()
    if body is None:
        raise TicketError("bad_answer", f"{name} answered, but not with the expected JSON.")
    return body


def _halo_token(transport: Transport, credential: Credential) -> str:
    try:
        body = halo.request_token(
            transport, credential.base_url, credential.client_id, credential.secret, credential.scope
        )
    except halo.HaloError as error:
        if error.refused:
            raise TicketError("refused", outbound.SERVICE_REFUSED) from None
        if error.status == 0:
            raise TicketError("unreachable", "Couldn't reach Halo from this server.") from None
        if error.status in (400, 401):
            raise TicketError("auth", "Halo refused the Halo connection's client ID, secret or scope.") from None
        raise TicketError("busy", f"Halo refused the token request ({error.status}).") from None
    return str(body["access_token"])


def halo_token(credential: Credential, transport: Transport | None = None) -> str:
    """A Halo bearer token through the Halo connection, so a run of reads asks for one only once."""
    return _halo_token(transport or transport_for(credential.base_url), credential)


def fetch(
    credential: Credential, key: str, transport: Transport | None = None, *, token: str | None = None
) -> TicketState:
    """The ticket's normalised status, the system's status name and the title. `token`: a Halo token already
    issued in this run (else one is requested)."""
    transport = transport or transport_for(credential.base_url)
    if credential.system == TicketSystem.JIRA:
        url = _url(credential, f"/rest/api/3/issue/{urllib.parse.quote(key)}", {"fields": "status,summary"})
        body = _check(_send(transport, credential, "GET", url, {"Authorization": _basic(credential)}), credential)
        fields = body.get("fields") if isinstance(body, dict) else None
        if not isinstance(fields, dict):
            raise TicketError("bad_answer", "Jira answered without the issue's fields.")
        status, label = jira_status(fields.get("status"))
        return TicketState(status, label, _title(fields.get("summary")))
    if credential.system == TicketSystem.SERVICENOW:
        table = servicenow_table(key)
        url = _url(
            credential,
            f"/api/now/table/{table}",
            {
                "sysparm_query": f"number={key}",
                "sysparm_fields": "number,state,short_description",
                "sysparm_display_value": "all",
                "sysparm_limit": "1",
            },
        )
        body = _check(_send(transport, credential, "GET", url, {"Authorization": _basic(credential)}), credential)
        rows = body.get("result") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise TicketError("bad_answer", "ServiceNow answered without a result list.")
        if not rows or not isinstance(rows[0], dict):
            raise TicketError("not_found", "ServiceNow has no ticket with this number, or the account can't see it.")
        row = rows[0]
        status, label = servicenow_status(row.get("state"), table)
        summary = row.get("short_description")
        if isinstance(summary, dict):
            summary = summary.get("display_value") or summary.get("value")
        return TicketState(status, label, _title(summary))
    if credential.system == TicketSystem.HALO:
        token = token or _halo_token(transport, credential)
        url = _url(credential, f"/api/Tickets/{urllib.parse.quote(key)}")
        body = _check(_send(transport, credential, "GET", url, {"Authorization": f"Bearer {token}"}), credential)
        if not isinstance(body, dict):
            raise TicketError("bad_answer", "Halo answered without the ticket.")
        status, label = halo_status(body)
        return TicketState(status, label, _title(body.get("summary")))
    raise TicketError("not_configured", "This system can't be read.")


def _adf(text: str) -> dict[str, Any]:
    """A plain paragraph per line in Atlassian Document Format (Jira REST v3's description)."""
    lines = [line for line in text.splitlines() if line.strip()] or [""]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}] if line else []} for line in lines
        ],
    }


def create(
    credential: Credential,
    *,
    target: str,
    issue_type: str,
    title: str,
    description: str,
    transport: Transport | None = None,
) -> Created:
    """Create one ticket. TicketError (with `ambiguous` when it may have been created anyway) otherwise."""
    transport = transport or transport_for(credential.base_url)
    base = credential.base_url.rstrip("/")
    if credential.system == TicketSystem.JIRA:
        payload: dict[str, Any] = {
            "fields": {
                "project": {"key": target},
                "issuetype": {"name": issue_type or "Task"},
                "summary": title,
                "description": _adf(description),
            }
        }
        url = _url(credential, "/rest/api/3/issue")
        reply = _send(transport, credential, "POST", url, {"Authorization": _basic(credential)}, payload)
        body = _check(reply, credential, what="the project or issue type")
        key = body.get("key") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key:
            raise TicketError("bad_answer", "Jira answered without the new issue's key.", ambiguous=True)
        return Created(key, f"{base}/browse/{key}", TicketState(TicketStatus.OPEN, "", _title(title)))
    if credential.system == TicketSystem.SERVICENOW:
        table = target or "incident"
        url = _url(
            credential,
            f"/api/now/table/{urllib.parse.quote(table)}",
            {"sysparm_fields": "number,sys_id,state,short_description", "sysparm_display_value": "all"},
        )
        payload = {"short_description": title, "description": description}
        reply = _send(transport, credential, "POST", url, {"Authorization": _basic(credential)}, payload)
        body = _check(reply, credential, what="the table")
        row = body.get("result") if isinstance(body, dict) else None
        if not isinstance(row, dict):
            raise TicketError("bad_answer", "ServiceNow answered without the new ticket.", ambiguous=True)
        number, sys_id = row.get("number"), row.get("sys_id")
        if isinstance(number, dict):
            number = number.get("value")
        if isinstance(sys_id, dict):
            sys_id = sys_id.get("value")
        if not isinstance(number, str) or not number or not isinstance(sys_id, str) or not sys_id.isalnum():
            raise TicketError("bad_answer", "ServiceNow answered without the new ticket's number.", ambiguous=True)
        status, label = servicenow_status(row.get("state"), table)
        link = f"{base}/{table}.do?sys_id={sys_id}"
        return Created(number, link, TicketState(status, label, _title(title)))
    if credential.system == TicketSystem.HALO:
        token = _halo_token(transport, credential)
        ticket: dict[str, Any] = {"summary": title, "details": description}
        if target:
            ticket["tickettype_id"] = int(target)
        url = _url(credential, "/api/Tickets")
        reply = _send(transport, credential, "POST", url, {"Authorization": f"Bearer {token}"}, [ticket])
        body = _check(reply, credential, what="the ticket type")
        if isinstance(body, list) and body:
            body = body[0]
        ticket_id = body.get("id") if isinstance(body, dict) else None
        if isinstance(ticket_id, bool) or not isinstance(ticket_id, int) or ticket_id <= 0:
            raise TicketError("bad_answer", "Halo answered without the new ticket's number.", ambiguous=True)
        assert isinstance(body, dict)
        status, label = halo_status(body)
        return Created(str(ticket_id), f"{base}/tickets?id={ticket_id}", TicketState(status, label, _title(title)))
    raise TicketError("not_configured", "This system can't create tickets.")


# ---- the connection test ------------------------------------------------------------------------------------------

PROBE_PROBLEMS = {
    "refused": "refused",
    "unreachable": "unreachable",
    "timeout": "unreachable",
    "auth": "wrong_credentials",
    "forbidden": "missing_permission",
    "not_found": "unavailable",
    "busy": "unreachable",
    "bad_answer": "error",
    "not_configured": "not_configured",
}
PROBE_NEXT = {
    "refused": "Use the system's own https address; loopback, link-local and metadata addresses aren't allowed.",
    "unreachable": "Check the address and that this server can reach it.",
    "timeout": "Check the address and that this server can reach it, then try again.",
    "auth": "Check the user name (the account email for Jira) and the API token or password.",
    "forbidden": "Give the account read access to the tickets (Jira: Browse projects; ServiceNow: the task tables).",
    "not_found": "Check the address, for example https://yourcompany.atlassian.net.",
    "busy": "Try again in a few minutes.",
    "bad_answer": "Check that the address is the system's own address.",
    "not_configured": "Complete the connection's settings.",
}


def probe(credential: Credential, transport: Transport | None = None) -> ProbeResult:
    """Sign in and make one harmless read: Jira `myself`, one ServiceNow task, a Halo token with a ticket scope."""
    try:
        transport = transport or transport_for(credential.base_url)
        if credential.system == TicketSystem.JIRA:
            url = _url(credential, "/rest/api/3/myself")
            _check(_send(transport, credential, "GET", url, {"Authorization": _basic(credential)}), credential)
            return ProbeResult(True, detail="Jira accepted the email and API token.")
        if credential.system == TicketSystem.SERVICENOW:
            query = {"sysparm_limit": "1", "sysparm_fields": "number"}
            url = _url(credential, "/api/now/table/task", query)
            reply = _send(transport, credential, "GET", url, {"Authorization": _basic(credential)})
            _check(reply, credential, what="the task table")
            return ProbeResult(True, detail="ServiceNow accepted the user and can read the task table.")
        if credential.system == TicketSystem.HALO:
            scopes = set(credential.scope.split())
            if not scopes & set(HALO_READ_SCOPES):
                return ProbeResult(
                    False,
                    "missing_permission",
                    detail="The Halo connection's scope doesn't include read:tickets.",
                    next="Add read:tickets (and edit:tickets to create tickets) to the Halo connection's scope and "
                    "the API application in Halo, then test both connections again.",
                )
            _halo_token(transport, credential)
            return ProbeResult(True, detail="Halo issued a token with a ticket scope.")
    except TicketError as error:
        return ProbeResult(False, PROBE_PROBLEMS[error.kind], detail=str(error), next=PROBE_NEXT[error.kind])
    return ProbeResult(
        False, "not_configured", detail="This system can't be tested.", next=PROBE_NEXT["not_configured"]
    )
