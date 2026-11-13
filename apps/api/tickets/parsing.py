"""Which system a ticket URL belongs to and the ticket's key, from known URL shapes (no network).

- Jira: `/browse/KEY` on any host (Jira Cloud and Data Center); on `*.atlassian.net` also `selectedIssue=KEY` (boards,
  issue navigator) and a key as the last path segment (service desk queues). A key is a project key (a letter, then
  letters, digits or underscores) and a number: `ITOPS-123`.
- ServiceNow: `*.service-now.com` and `*.servicenowservices.com`. The key is a task number found anywhere in the
  decoded URL (`INC0012345`, `CHG0030001`, `RITM0010001`, ...). A URL that only carries a sys_id (`incident.do?sys_id=`)
  is still ServiceNow, with no key: enter the number as the key.
- HaloITSM: `*.haloitsm.com`, `*.halopsa.com`, `*.haloservicedesk.com`. The key is the ticket number: `id=` on
  `/tickets`, `/ticket` or `/portal/ticket` pages, or the last path segment of `/tickets/123`.
- The host of any of the organization's ticket connections counts as that connection's system (a custom domain).
- Anything else is `other`, with no key; an Editor may still enter one.

A link URL must be https, at most 2,048 characters, with a host and without a user name or password. The URL is only
stored and shown: no request ever goes to it (a refresh reads the ticket by key from the connection's own address)."""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

from core import outbound

from .models import URL_MAX, TicketSystem

# RFC 3986: unreserved, reserved and percent-escapes. The query and fragment may also hold ^ | { }, which real
# addresses carry unencoded (ServiceNow encoded queries: sysparm_query=active=true^number=INC0012345). A backslash,
# quotes, angle brackets and backticks are refused everywhere.
URL_CHARS = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+$")
QUERY_CHARS = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%^|{}]*$")
# Underscores are allowed, as connection addresses allow them (intranet host names).
HOST_LABEL = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
REFUSED_EVERYWHERE = set('\\"<>`')
JIRA_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,49}-[1-9][0-9]{0,15}$")
SERVICENOW_NUMBER = re.compile(r"(?<![A-Za-z0-9])((?:INC|CHG|RITM|REQ|PRB|SCTASK|CTASK|PTASK|TASK)[0-9]{7,})(?![0-9])")
HALO_ID = re.compile(r"^[1-9][0-9]{0,15}$")
JIRA_HOSTS = (".atlassian.net", ".jira.com")
SERVICENOW_HOSTS = (".service-now.com", ".servicenowservices.com")
HALO_HOSTS = (".haloitsm.com", ".halopsa.com", ".haloservicedesk.com")
HALO_PATHS = ("/tickets", "/ticket", "/portal/ticket", "/portal/tickets")

# ServiceNow number prefixes and the table each lives in (a number is read from its own table, so its state is read
# against that table's choice list).
SERVICENOW_TABLES = {
    "INC": "incident",
    "CHG": "change_request",
    "RITM": "sc_req_item",
    "REQ": "sc_request",
    "PRB": "problem",
    "SCTASK": "sc_task",
    "CTASK": "change_task",
    "PTASK": "problem_task",
    "TASK": "task",
}


class InvalidUrl(ValueError):
    """The text isn't an acceptable link URL. The message is safe to show."""


@dataclass(frozen=True)
class Parsed:
    system: str
    key: str
    host: str


def check_url(url: object) -> urllib.parse.SplitResult:
    if not isinstance(url, str) or not url.strip():
        raise InvalidUrl("Enter the ticket's address.")
    url = url.strip()
    if len(url) > URL_MAX:
        raise InvalidUrl(f"Use an address of at most {URL_MAX} characters.")
    if any(ord(c) < 33 for c in url):
        raise InvalidUrl("Remove spaces and control characters from the address.")
    if not url.isascii():
        raise InvalidUrl("Paste the address as the browser copies it, with other characters percent-encoded.")
    # The path part ends at the first ? (or at # when no ? comes before it).
    head, _, tail = url.partition("?") if "?" in url.split("#", 1)[0] else url.partition("#")
    if REFUSED_EVERYWHERE & set(url) or not URL_CHARS.match(head) or not QUERY_CHARS.match(tail):
        # A backslash (browsers read it as a slash, so the host shown isn't the host opened), quotes, angle brackets
        # and backticks anywhere; ^ | { } outside the query and fragment.
        raise InvalidUrl(
            "Remove characters an address can't hold: \\, \", <, > and ` anywhere, and ^, |, { and } outside the part "
            "after ? or #."
        )
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # noqa: B018 (raises ValueError for a bad port)
    except ValueError:
        raise InvalidUrl("Enter a valid https:// address.") from None
    if parts.scheme.lower() != "https":
        raise InvalidUrl("Use an https:// address.")
    if not parts.hostname:
        raise InvalidUrl("Enter a valid https:// address.")
    if parts.username is not None or parts.password is not None:
        raise InvalidUrl("Leave the user name and password out of the address.")
    if not _valid_host(parts):
        raise InvalidUrl("Enter a valid host name in the address.")
    return parts


def _valid_host(parts: urllib.parse.SplitResult) -> bool:
    """A DNS name (letters, digits, dots and hyphens, as outbound.ascii_host leaves it) or a bracketed IP literal."""
    host = parts.hostname or ""
    netloc_host = parts.netloc.rsplit("@", 1)[-1]
    if netloc_host.startswith("["):
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return False
        return True
    try:
        encoded = outbound.ascii_host(host)
    except outbound.UnsafeDestination:
        return False
    labels = encoded.rstrip(".").split(".")
    return all(HOST_LABEL.match(label) for label in labels)


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _jira_key(parts: urllib.parse.SplitResult, jira_site: bool) -> str:
    """The key from /browse/KEY anywhere; on a known Jira site also from selectedIssue= or the last path segment."""
    segments = [s for s in urllib.parse.unquote(parts.path).split("/") if s]
    for i, segment in enumerate(segments[:-1]):
        if segment == "browse" and JIRA_KEY.match(segments[i + 1]):
            return segments[i + 1]
    if jira_site:
        query = urllib.parse.parse_qs(parts.query)
        for value in query.get("selectedIssue", []):
            if JIRA_KEY.match(value):
                return value
        if segments and JIRA_KEY.match(segments[-1]):
            return segments[-1]
    return ""


def _servicenow_key(url: str) -> str:
    text = url
    for _ in range(3):  # classic UI URLs nest an encoded URL inside another
        text = urllib.parse.unquote(text)
    found = SERVICENOW_NUMBER.search(text)
    return found.group(1) if found else ""


def _halo_key(parts: urllib.parse.SplitResult) -> str:
    path = parts.path.rstrip("/").lower()
    query = urllib.parse.parse_qs(parts.query)
    if path in HALO_PATHS:
        for value in query.get("id", []):
            if HALO_ID.match(value):
                return value
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) >= 2 and segments[-2].lower() in ("tickets", "ticket") and HALO_ID.match(segments[-1]):
        return segments[-1]
    return ""


def parse(url: object, known_hosts: Mapping[str, str] | None = None) -> Parsed:
    """The system and key of a ticket URL (InvalidUrl when it isn't an acceptable https URL). `known_hosts` maps the
    hosts of the organization's ticket connections to their system, so a custom domain (Jira Data Center, a Halo
    domain of its own, a ServiceNow custom URL) parses as its system."""
    parts = check_url(url)
    assert isinstance(url, str)
    url = url.strip()
    host = (parts.hostname or "").lower().rstrip(".")
    known = (known_hosts or {}).get(host)
    if host.endswith(SERVICENOW_HOSTS) or known == TicketSystem.SERVICENOW:
        return Parsed(TicketSystem.SERVICENOW, _servicenow_key(url), host)
    if host.endswith(HALO_HOSTS) or known == TicketSystem.HALO:
        return Parsed(TicketSystem.HALO, _halo_key(parts), host)
    key = _jira_key(parts, host.endswith(JIRA_HOSTS) or known == TicketSystem.JIRA)
    if key or host.endswith(JIRA_HOSTS) or known == TicketSystem.JIRA:
        return Parsed(TicketSystem.JIRA, key, host)
    return Parsed(TicketSystem.OTHER, "", host)


def key_problem(system: str, key: str) -> str:
    """Why `key` isn't a valid key for `system`, or "" (an empty key is allowed: the link is then manual only)."""
    if not key:
        return ""
    if system == TicketSystem.JIRA and not JIRA_KEY.match(key):
        return "Enter a Jira issue key such as ITOPS-123."
    if system == TicketSystem.SERVICENOW and not SERVICENOW_NUMBER.fullmatch(key):
        return "Enter a ServiceNow number such as INC0012345."
    if system == TicketSystem.HALO and not HALO_ID.match(key):
        return "Enter the Halo ticket number, digits only."
    if len(key) > 100 or any(ord(c) < 32 for c in key):
        return "Use 100 characters or fewer, without control characters."
    return ""


def servicenow_table(number: str) -> str:
    match = re.match(r"^[A-Z]+", number)
    return SERVICENOW_TABLES.get(match.group(0) if match else "", "task")
