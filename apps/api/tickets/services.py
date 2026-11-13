"""Ticket link and ticket connection writes (validated, scoped to the organization and audited in one
transaction). Capability `ticket_links` (community) for everything here; a Halo ticket connection also needs
`discovery.halo`, because it signs in through the organization's Halo connection.

ServiceNow and Halo ticket connections are preview features: each system's capability is in
SYSTEM_CAPABILITY, and a system whose capability the organization lacks (`preview`, off unless the installation sets
SOFTWARDEN_PREVIEW_FEATURES) can't have a connection created, read, changed or tested (403 naming it), isn't in
`systems`, and is never read or written to (refresh, create ticket, the hourly refresh). A connection kept for it is
listed as unavailable and can be deleted, so it never blocks its Halo connection. Its links stay, and links to its
tickets are still pasted, parsed and kept with a status set by hand.

Links (Editors): `create_link`, `update_link`, `delete_link` on an assessment or a renewal; `refresh_link` reads the
ticket's status now through the system's connection; `create_ticket` creates a ticket in the system and links it,
only when the connection's `create_enabled` is on (off by default), and audits every attempt, failed ones included.

Connections (Admins): `create_connection`, `update_connection`, `delete_connection`, `run_test`. The secret is a
Fernet slot (core/credentials.py): write-only, never in a response, an ETag, an audit payload or snapshot, a log line,
a job payload or an export; an audit event only says that it was replaced (`replaced`).

Network calls (refresh, create, test) run outside any transaction; the row is then re-read under a lock and written.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.http import Http404
from django.utils import timezone

from catalog.models import ChangeAssessment
from catalog.services import audit, is_uuid
from core import connections as core_connections
from core import credentials, outbound, simulation
from core.capabilities import DISCOVERY_HALO, PREVIEW, TICKET_LINKS, has_capability, require_capability
from core.connections import ProbeResult
from core.models import HaloConnection
from core.problems import Conflict, FieldErrors
from money.models import RenewalCycle, RenewalObligation

from . import clients, parsing
from .models import (
    CONNECTED_SYSTEMS,
    KEY_MAX,
    TITLE_MAX,
    StatusSource,
    TicketConnection,
    TicketLink,
    TicketStatus,
    TicketSystem,
)

log = logging.getLogger("softwarden.tickets")

NAME_MAX = 100
NOTES_MAX = 5000
SECRET_MAX = 1000
USERNAME_MAX = 254
BASE_URL_MAX = 300
DESCRIPTION_MAX = 5000
CREATE_TITLE_MAX = 255  # Jira's summary limit, the tightest of the three
SECRET_COLUMNS = ("secret_ciphertext", "secret_key_id")


# ---- targets ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """What a link hangs on: an assessment or a renewal."""

    kind: str  # "assessment" | "renewal"
    obj: ChangeAssessment | RenewalObligation

    @property
    def filter(self) -> dict[str, Any]:
        return {"assessment": self.obj} if self.kind == "assessment" else {"obligation": self.obj}

    def title(self) -> str:
        if isinstance(self.obj, ChangeAssessment):
            return self.obj.title
        return f"Renewal: {self.obj.name}"

    def path(self) -> str:
        return f"/{'assessments' if self.kind == 'assessment' else 'renewals'}/{self.obj.id}"


def assessment_target(org, pk) -> Target:
    found = ChangeAssessment.objects.filter(organization=org, pk=pk).first() if is_uuid(pk) else None
    if found is None:
        raise Http404
    return Target("assessment", found)


def renewal_target(org, pk) -> Target:
    found = RenewalObligation.objects.filter(organization=org, pk=pk).first() if is_uuid(pk) else None
    if found is None:
        raise Http404
    return Target("renewal", found)


def _locked_target(org, target: Target) -> Target:
    model = type(target.obj)
    found = model.objects.select_for_update().filter(organization=org, pk=target.obj.pk).first()
    if found is None:
        raise Http404
    return Target(target.kind, found)


def links_of(org, target: Target):
    return TicketLink.objects.filter(organization=org, **target.filter).select_related("cycle", "created_by")


def get_link(org, target: Target, pk) -> TicketLink:
    found = links_of(org, target).filter(pk=pk).first() if is_uuid(pk) else None
    if found is None:
        raise Http404
    return found


# ---- validation ---------------------------------------------------------------------------------------------------


def _text(value: Any, key: str, errors: dict, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        errors[key] = ["Enter text."]
        return ""
    value = " ".join(value.split()) if key in ("title", "name") else value.strip()
    if len(value) > limit:
        errors[key] = [f"Use {limit} characters or fewer."]
    elif required and not value:
        errors[key] = ["This is required."]
    return value


def known_hosts(org) -> dict[str, str]:
    """{host: system} of the organization's ticket connections, so links on their own (custom) domains parse as
    their system."""
    found: dict[str, str] = {}
    for conn in TicketConnection.objects.filter(organization=org).select_related("halo_connection"):
        base = conn.halo_connection.base_url if conn.halo_connection is not None else conn.base_url
        host = parsing.host_of(base)
        if host:
            found[host] = conn.system
    return found


def parse_url(org, url: Any) -> parsing.Parsed:
    return parsing.parse(url, known_hosts(org))


LINK_FIELDS = ("url", "title", "system", "external_key", "status", "cycle")


def _apply_link(org, target: Target, link: TicketLink, data: dict[str, Any], creating: bool) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    for key in sorted(set(data) - set(LINK_FIELDS)):
        errors[key] = ["This isn't a field of a ticket link."]
    before_ticket = (link.system, link.external_key, parsing.host_of(link.url))
    parsed: parsing.Parsed | None = None
    if "url" in data or creating:
        try:
            parsed = parse_url(org, data.get("url"))
        except parsing.InvalidUrl as e:
            errors["url"] = [str(e)]
        else:
            link.url = str(data["url"]).strip()
    if "system" in data:
        if data["system"] not in TicketSystem.values:
            errors["system"] = [f"Choose one of {', '.join(TicketSystem.values)}."]
        else:
            link.system = data["system"]
    elif parsed is not None:
        link.system = parsed.system
    # A new URL brings its own key, unless a key is sent; a key parsed for another system than the link's is dropped.
    if parsed is not None and "external_key" not in data and "system" not in errors:
        link.external_key = parsed.key if parsed.system == link.system else ""
    if "external_key" in data:
        link.external_key = _text(data["external_key"], "external_key", errors, 100)
    if "external_key" not in errors and "system" not in errors:
        problem = parsing.key_problem(link.system, link.external_key)
        if problem:
            errors["external_key"] = [problem]
    if "title" in data:
        link.title = _text(data["title"], "title", errors, TITLE_MAX)
    if "status" in data and data["status"] is None:
        # Back to "read it from the system": the status is unknown until the next read (by hand or the hourly one).
        _reset_status(link)
    elif "status" in data:
        if data["status"] not in TicketStatus.values:
            errors["status"] = [f"Choose one of {', '.join(TicketStatus.values)}."]
        elif data["status"] != link.status or creating:
            link.status = data["status"]
            link.status_source = StatusSource.MANUAL
            link.status_error = ""
            link.external_status = ""
    if "cycle" in data:
        value = data["cycle"]
        if target.kind != "renewal":
            if value not in (None, ""):
                errors["cycle"] = ["Only a renewal's ticket can name a cycle."]
        elif value in (None, ""):
            link.cycle = None
        else:
            cycle = (
                RenewalCycle.objects.filter(organization=org, obligation_id=target.obj.pk, pk=value).first()
                if is_uuid(value)
                else None
            )
            if cycle is None:
                errors["cycle"] = ["Choose one of this renewal's cycles."]
            link.cycle = cycle
    after_ticket = (link.system, link.external_key, parsing.host_of(link.url))
    if not creating and "status" not in data and after_ticket != before_ticket:
        # Another ticket: what was read or set about the old one no longer applies, and it is read again.
        _reset_status(link)
    return errors


def _reset_status(link: TicketLink) -> None:
    link.status = TicketStatus.UNKNOWN
    link.status_source = StatusSource.MANUAL
    link.external_status = ""
    link.status_error = ""
    link.status_checked_at = None


def link_values(link: TicketLink) -> dict[str, Any]:
    return {
        "system": link.system,
        "external_key": link.external_key,
        "url": link.url,
        "title": link.title,
        "status": link.status,
        "cycle": str(link.cycle_id) if link.cycle_id else None,
    }


def read_values(link: TicketLink) -> dict[str, Any]:
    """What the last read left on a link (compared, and audited, when a status is reset to be read again)."""
    return {
        "status_source": link.status_source,
        "status_checked_at": link.status_checked_at.isoformat() if link.status_checked_at else None,
        "external_status": link.external_status,
        "status_error": link.status_error,
    }


def _save_link(link: TicketLink) -> None:
    try:
        with transaction.atomic():
            link.save()
    except IntegrityError:
        raise FieldErrors({"url": ["This ticket is already linked here."]}) from None


# ---- link writes --------------------------------------------------------------------------------------------------


def _locked_link(org, pk) -> TicketLink:
    found = TicketLink.objects.select_for_update().filter(organization=org, pk=pk).first()
    if found is None:  # deleted meanwhile
        raise Http404
    return found


@transaction.atomic
def create_link(org, actor, target: Target, data: dict[str, Any]) -> TicketLink:
    require_capability(org, TICKET_LINKS)
    target = _locked_target(org, target)
    link = TicketLink(organization=org, created_by=actor, **target.filter)
    errors = _apply_link(org, target, link, data, creating=True)
    if "url" not in errors and links_of(org, target).filter(url=link.url).exists():
        errors["url"] = ["This ticket is already linked here."]
    if errors:
        raise FieldErrors(errors)
    _save_link(link)
    audit(org, actor, "ticket_link.created", link, {**link_values(link), "on": target.kind})
    return link


@transaction.atomic
def update_link(org, actor, target: Target, link: TicketLink, data: dict[str, Any]) -> TicketLink:
    require_capability(org, TICKET_LINKS)
    link = _locked_link(org, link.pk)
    # An explicit `status: null` (back to reading it) changes what was read as well as the status: compare that too,
    # so a reset of an unknown status that had been read, or had failed, is saved and audited.
    reset = "status" in data and data["status"] is None

    def values(item: TicketLink) -> dict[str, Any]:
        return {**link_values(item), **read_values(item)} if reset else link_values(item)

    before = values(link)
    errors = _apply_link(org, target, link, data, creating=False)
    if "url" not in errors and links_of(org, target).filter(url=link.url).exclude(pk=link.pk).exists():
        errors["url"] = ["This ticket is already linked here."]
    if errors:
        raise FieldErrors(errors)
    after = values(link)
    changed = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    if not changed:
        return link
    _save_link(link)
    audit(org, actor, "ticket_link.updated", link, changed)
    return link


@transaction.atomic
def delete_link(org, actor, link: TicketLink) -> None:
    require_capability(org, TICKET_LINKS)
    link = _locked_link(org, link.pk)
    audit(org, actor, "ticket_link.deleted", link, link_values(link))
    link.delete()


# ---- connections: validation and writes ----------------------------------------------------------------------------

CONNECTION_FIELDS = (
    "name",
    "enabled",
    "notes",
    "system",
    "base_url",
    "username",
    "secret",
    "halo_connection",
    "create_enabled",
    "create_target",
    "create_issue_type",
)
AUDITED = (
    "name",
    "enabled",
    "notes",
    "system",
    "base_url",
    "username",
    "halo_connection",
    "create_enabled",
    "create_target",
    "create_issue_type",
)
SYSTEM_LABELS = {s.value: s.label for s in CONNECTED_SYSTEMS}
# The capability each connected system needs besides ticket_links. ServiceNow and Halo are preview features
# (launch scope): to release one, set its entry to None.
SYSTEM_CAPABILITY: dict[str, str | None] = {
    TicketSystem.JIRA: None,
    TicketSystem.SERVICENOW: PREVIEW,
    TicketSystem.HALO: PREVIEW,
}


def system_available(org, system: str) -> bool:
    """Whether the organization may use `system`'s ticket connection (its capability; Jira needs none)."""
    capability = SYSTEM_CAPABILITY.get(system)
    return system in SYSTEM_LABELS and (capability is None or has_capability(org, capability))


def require_system(org, system: str) -> None:
    """403 problem naming the capability when the organization may not use `system`'s ticket connection."""
    capability = SYSTEM_CAPABILITY.get(system)
    if capability is not None:
        require_capability(org, capability)


def available_systems(org) -> list[str]:
    """The connected systems the organization may use, in CONNECTED_SYSTEMS order."""
    return [system for system in SYSTEM_LABELS if system_available(org, system)]


def connection_values(conn: TicketConnection) -> dict[str, Any]:
    """The audited settings: never the secret slot."""
    values = {f: getattr(conn, f) for f in AUDITED if f != "halo_connection"}
    values["halo_connection"] = str(conn.halo_connection_id) if conn.halo_connection_id else None
    return values


def _bool(data: dict, key: str, errors: dict, default: bool) -> bool:
    value = data[key]
    if not isinstance(value, bool):
        errors[key] = ["Choose yes or no."]
        return default
    return value


# A stored token or password is only ever sent to the host it was entered for.
ADDRESS_MOVED = "The address now points at another host: enter the API token or password again to send it there."


def _apply_connection(org, conn: TicketConnection, data: dict[str, Any], creating: bool) -> tuple[dict, list[str]]:
    errors: dict[str, list[str]] = {}
    for key in sorted(set(data) - set(CONNECTION_FIELDS)):
        errors[key] = ["This isn't a setting of a ticket connection."]
    if creating:
        system = data.get("system")
        if system not in SYSTEM_LABELS:
            errors["system"] = [f"Choose one of {', '.join(available_systems(org))}."]
            return errors, []
        require_system(org, system)
        conn.system = system
        if TicketConnection.objects.filter(organization=org, system=system).exists():
            errors["system"] = [f"This organization already has a {SYSTEM_LABELS[system]} ticket connection."]
    elif "system" in data and data["system"] != conn.system:
        errors["system"] = ["The system can't be changed; delete the connection and add another."]
    is_halo = conn.system == TicketSystem.HALO
    if is_halo:
        require_capability(org, DISCOVERY_HALO)
    if "name" in data or creating:
        conn.name = _text(data.get("name") or SYSTEM_LABELS.get(conn.system, ""), "name", errors, NAME_MAX, True)
    if "enabled" in data:
        conn.enabled = _bool(data, "enabled", errors, conn.enabled)
    if "notes" in data:
        conn.notes = _text(data["notes"], "notes", errors, NOTES_MAX)
    if "create_enabled" in data:
        conn.create_enabled = _bool(data, "create_enabled", errors, conn.create_enabled)
    if "create_target" in data:
        conn.create_target = _text(data["create_target"], "create_target", errors, 100)
    if "create_issue_type" in data:
        conn.create_issue_type = _text(data["create_issue_type"], "create_issue_type", errors, 60)
    replaced: list[str] = []
    if is_halo:
        for key in ("base_url", "username", "secret"):
            if key in data:
                errors[key] = ["A Halo ticket connection uses its Halo connection's address and credentials."]
        if "halo_connection" in data or creating:
            value = data.get("halo_connection")
            found = (
                HaloConnection.objects.select_for_update().filter(organization=org, pk=str(value)).first()
                if is_uuid(value)
                else None
            )
            if found is None:
                errors["halo_connection"] = ["Choose an existing Halo connection."]
            elif clients.is_simulated(found.base_url) and not simulation.allowed():
                errors["halo_connection"] = [simulation.MESSAGE]
            conn.halo_connection = found
        if "create_target" not in errors and conn.create_target and not re.fullmatch(r"[0-9]{1,9}", conn.create_target):
            errors["create_target"] = ["Enter the Halo ticket type ID, digits only."]
    else:
        if data.get("halo_connection") not in (None, ""):
            errors["halo_connection"] = ["Only a Halo ticket connection uses a Halo connection."]
        destination_before, had_secret = outbound.origin(conn.base_url), bool(conn.secret_ciphertext)
        if "base_url" in data or creating:
            url = _text(data.get("base_url"), "base_url", errors, BASE_URL_MAX, required=True)
            if "base_url" not in errors and clients.is_simulated(url) and not simulation.allowed():
                errors["base_url"] = [simulation.MESSAGE]
            elif "base_url" not in errors:
                try:
                    outbound.check_service_url(url, max_length=BASE_URL_MAX)
                except outbound.UnsafeDestination as e:
                    errors["base_url"] = [str(e)]
            conn.base_url = url.rstrip("/")
        if "username" in data or creating:
            what = "the Atlassian account email" if conn.system == TicketSystem.JIRA else "the user name"
            conn.username = _text(data.get("username"), "username", errors, USERNAME_MAX)
            if "username" not in errors and not conn.username:
                errors["username"] = [f"Enter {what}."]
        if "secret" in data:
            value = data["secret"]
            if not isinstance(value, str) or not value.strip():
                errors["secret"] = ["Enter the API token or password, or leave the field out to keep the stored one."]
            elif len(value.strip()) > SECRET_MAX:
                errors["secret"] = [f"Use {SECRET_MAX} characters or fewer."]
            else:
                try:
                    credentials.slot_set(conn, "secret", value.strip())
                    replaced = ["secret"]
                except credentials.CredentialError as e:
                    errors["secret"] = [str(e)]
        elif creating:
            errors["secret"] = ["Enter the API token or password."]
        elif had_secret and "base_url" not in errors:
            if outbound.secret_must_follow(destination_before, outbound.origin(conn.base_url)):
                errors["secret"] = [ADDRESS_MOVED]
        if conn.system == TicketSystem.JIRA and conn.create_target:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,49}", conn.create_target):
                errors.setdefault("create_target", ["Enter the Jira project key, such as ITOPS."])
        if conn.system == TicketSystem.SERVICENOW and conn.create_target:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", conn.create_target):
                errors.setdefault("create_target", ["Enter the ServiceNow table name, such as incident."])
    return errors, replaced


@transaction.atomic
def create_connection(org, actor, data: dict[str, Any]) -> TicketConnection:
    require_capability(org, TICKET_LINKS)
    conn = TicketConnection(organization=org)
    errors, replaced = _apply_connection(org, conn, data, creating=True)
    if errors:
        raise FieldErrors(errors)
    try:
        with transaction.atomic():
            conn.save()
    except IntegrityError:
        raise FieldErrors({"system": ["This organization already has a ticket connection for this system."]}) from None
    audit(org, actor, "ticket_connection.created", conn, {**connection_values(conn), "replaced": replaced})
    return conn


@transaction.atomic
def update_connection(org, actor, conn: TicketConnection, data: dict[str, Any]) -> TicketConnection:
    require_capability(org, TICKET_LINKS)
    require_system(org, conn.system)
    conn = TicketConnection.objects.select_for_update().get(organization=org, pk=conn.pk)
    before, before_revision = connection_values(conn), conn.revision
    errors, replaced = _apply_connection(org, conn, data, creating=False)
    if errors:
        raise FieldErrors(errors)
    after = connection_values(conn)
    changed = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    if not changed and not replaced:
        return conn
    conn.save()  # bumps the revision when a sign-in value or the secret changed (core Connection.save)
    audit(
        org,
        actor,
        "ticket_connection.updated",
        conn,
        {**changed, "replaced": replaced, "revision": [before_revision, conn.revision]},
    )
    return conn


@transaction.atomic
def delete_connection(org, actor, conn: TicketConnection) -> None:
    """Also for a system the organization can't use now (kept from when preview features were on): deleting it is
    always allowed, so it never blocks its Halo connection's delete. The audit says it was `kept`."""
    require_capability(org, TICKET_LINKS)
    conn = TicketConnection.objects.select_for_update().get(organization=org, pk=conn.pk)
    kept = {"kept": True} if not system_available(org, conn.system) else {}
    audit(org, actor, "ticket_connection.deleted", conn, {"name": conn.name, "system": conn.system, **kept})
    conn.delete()


# ---- using a connection -------------------------------------------------------------------------------------------


class NotUsable(Exception):
    """Why a connection can't be used now. Safe to show, store and log."""


def is_simulated(conn: TicketConnection) -> bool:
    """Whether the connection reads a synthetic system (its own address, or its Halo connection's)."""
    halo = conn.halo_connection if conn.system == TicketSystem.HALO else None
    return clients.is_simulated(halo.base_url if halo is not None else conn.base_url)


def _check_usable(conn: TicketConnection | None, system: str) -> TicketConnection:
    label = SYSTEM_LABELS.get(system, system)
    if conn is None:
        raise NotUsable(f"No {label} ticket connection is set up. An Admin can add one under Connections.")
    if not system_available(conn.organization, conn.system):
        raise NotUsable(f"{label} ticket connections are a preview feature this installation hasn't turned on.")
    if not conn.enabled:
        raise NotUsable(f"The {label} ticket connection is turned off.")
    if is_simulated(conn) and not simulation.allowed():
        raise NotUsable(simulation.refused(conn.name))
    if conn.system == TicketSystem.HALO:
        if not has_capability(conn.organization, DISCOVERY_HALO):
            raise NotUsable("This edition doesn't include the Halo connection (discovery.halo).")
    elif not conn.secret_ciphertext:
        raise NotUsable(f"The {label} ticket connection has no API token or password.")
    if not conn.test_current:
        raise NotUsable(f"The {label} ticket connection hasn't passed a test since it last changed. Test it first.")
    return conn


def connection_for(org, system: str) -> TicketConnection | None:
    return (
        TicketConnection.objects.filter(organization=org, system=system)
        .select_related("halo_connection", "organization")
        .first()
    )


def systems(org) -> list[dict[str, Any]]:
    """What each connected system does for this organization, for every member (GET /ticket-links/systems): `reads`
    when its ticket connection is usable (on, with its secret or its Halo edition, tested at its current revision), so
    statuses are read; `creates` when it also allows creating (and, for Jira, names a project). Never a connection's
    settings."""
    require_capability(org, TICKET_LINKS)
    rows = []
    for system in available_systems(org):
        label = SYSTEM_LABELS[system]
        conn = connection_for(org, system)
        try:
            _check_usable(conn, system)
            reads = True
        except NotUsable:
            reads = False
        creates = (
            reads
            and conn is not None
            and conn.create_enabled
            and (system != TicketSystem.JIRA or bool(conn.create_target))
        )
        rows.append({"system": system, "label": label, "reads": reads, "creates": creates})
    return rows


def credential_of(conn: TicketConnection, *, tested: bool = True) -> clients.Credential:
    """The connection's sign-in values with the decrypted secret. NotUsable otherwise. `tested=False` for the test
    itself (which proves them)."""
    if tested:
        _check_usable(conn, conn.system)
    if conn.system == TicketSystem.HALO:
        try:
            halo = core_connections.credential(conn.halo_connection)
        except core_connections.NotUsable as e:
            raise NotUsable(f"The Halo connection can't be used: {e}") from None
        assert isinstance(halo, core_connections.HaloCredential)
        return clients.Credential(
            TicketSystem.HALO, halo.base_url, secret=halo.secret, client_id=halo.client_id, scope=halo.scope
        )
    if not conn.secret_ciphertext:
        raise NotUsable("No API token or password is stored. Enter it.")
    try:
        secret = credentials.slot_value(conn, "secret")
    except credentials.CredentialError as e:
        raise NotUsable(str(e)) from None
    return clients.Credential(conn.system, conn.base_url, conn.username, secret)


TEST_FIELDS = ["tested_at", "test_ok", "test_result", "tested_by", "tested_revision", "updated_at"]


def run_test(org, actor, conn: TicketConnection) -> TicketConnection:
    """Sign in and make one read, outside any transaction, then record the outcome as core/connections.run_test does:
    only a success at an unchanged revision sets `tested_revision`."""
    require_capability(org, TICKET_LINKS)
    found = TicketConnection.objects.filter(organization=org, pk=conn.pk).select_related("halo_connection").first()
    if found is None:
        raise Http404
    conn = found
    require_system(org, conn.system)
    started_at = conn.revision
    if conn.system == TicketSystem.HALO:
        require_capability(org, DISCOVERY_HALO)
    try:
        if is_simulated(conn) and not simulation.allowed():
            result = core_connections.SIMULATION_OFF
        else:
            result = clients.probe(credential_of(conn, tested=False))
    except NotUsable as e:
        result = ProbeResult(False, "not_configured", detail=str(e), next="Complete the connection, then test it.")
    with transaction.atomic():
        row = TicketConnection.objects.select_for_update().filter(organization=org, pk=conn.pk).first()
        if row is None:
            raise Http404
        if row.revision != started_at:
            result = ProbeResult(
                False, "changed", detail="The connection changed while it was being tested.", next="Test it again."
            )
        else:
            row.tested_at, row.test_ok, row.tested_by = timezone.now(), result.ok, actor
            row.test_result = result.as_dict()
            row.tested_revision = row.revision if result.ok else None
            row.save(update_fields=TEST_FIELDS)
        row.probe = result
        audit(
            org,
            actor,
            "ticket_connection.tested",
            row,
            {"ok": result.ok, "problem": result.problem, "revision": row.revision},
        )
    return row


# ---- status refresh -----------------------------------------------------------------------------------------------


def _site_matches(link: TicketLink, credential: clients.Credential) -> bool:
    return parsing.host_of(link.url) == parsing.host_of(credential.base_url)


def read_status(
    link: TicketLink, credential: clients.Credential, *, token: str | None = None
) -> tuple[clients.TicketState | None, str, str]:
    """(the ticket's state, "", "") or (None, why it couldn't be read, the TicketError kind or ""). Network; call
    outside a transaction. `token`: a Halo token issued once for a run of reads."""
    if not link.external_key:
        return None, "This link has no ticket key. Enter the key to read its status.", ""
    if not _site_matches(link, credential):
        label = SYSTEM_LABELS.get(link.system, link.system)
        return None, f"This ticket isn't on the {label} site the connection reads.", ""
    try:
        return clients.fetch(credential, link.external_key, token=token), "", ""
    except clients.TicketError as e:
        return None, str(e), e.kind


def store_status(org, actor, link_id, state: clients.TicketState | None, error: str, now=None) -> TicketLink | None:
    """Record a read: the status, the system's status name, the title when the link has none (a curated title is
    never overwritten), the check time and the error. Audited (`ticket_link.status_changed`) only when the status
    moves. None when the link was deleted meanwhile."""
    now = now or timezone.now()
    with transaction.atomic():
        link = TicketLink.objects.select_for_update().filter(organization=org, pk=link_id).first()
        if link is None:
            return None
        before = link.status
        link.status_checked_at = now
        link.status_error = error[:300]
        if state is not None:
            link.status = state.status
            link.status_source = StatusSource.REFRESHED
            link.external_status = state.external_status
            if not link.title and state.title:
                link.title = state.title
        link.save()
        if link.status != before:
            audit(
                org,
                actor,
                "ticket_link.status_changed",
                link,
                {"status": [before, link.status], "external_status": link.external_status, "system": link.system},
            )
    return link


def refresh_link(org, actor, link: TicketLink) -> TicketLink:
    """Read one link's status now (an Editor's refresh). A connection that can't be used, or a read that fails, is
    recorded on the link as its error; the response is the link either way."""
    require_capability(org, TICKET_LINKS)
    if link.system not in SYSTEM_LABELS:
        raise Conflict("Only Jira, ServiceNow and Halo tickets can be read; set this one's status by hand.")
    require_system(org, link.system)
    conn = connection_for(org, link.system)
    try:
        if conn is None:
            _check_usable(None, link.system)  # raises: no connection for this system
        assert conn is not None
        credential = credential_of(conn)
    except NotUsable as e:
        raise Conflict(str(e)) from None
    state, error, _kind = read_status(link, credential)
    stored = store_status(org, actor, link.pk, state, error)
    if stored is None:
        raise Http404
    return stored


# ---- create a ticket ----------------------------------------------------------------------------------------------

CREATE_FIELDS = ("system", "title", "description", "cycle")


def _default_description(target: Target) -> str:
    base = getattr(settings, "SOFTWARDEN_BASE_URL", "").rstrip("/")
    what = "Change assessment" if target.kind == "assessment" else "Renewal"
    return f"{what} in Softwarden: {base}{target.path()}" if base else f"{what} in Softwarden."


class _NotStored(Exception):
    """A created ticket can't be stored as a link (its key or address isn't one we accept, or its cycle is gone)."""


def _printable(value: str, limit: int) -> str:
    return "".join(c for c in value if c.isprintable())[:limit]


def _store_created(org, actor, target: Target, system: str, conn, created, title: str, cycle_value) -> TicketLink:
    if not created.key or len(created.key) > KEY_MAX or parsing.key_problem(system, created.key):
        raise _NotStored("the system answered with a key we don't accept")
    try:
        parsing.check_url(created.url)
    except parsing.InvalidUrl:
        raise _NotStored("the ticket's address isn't one we accept") from None
    with transaction.atomic():
        locked = _locked_target(org, target)
        if (
            cycle_value
            and not RenewalCycle.objects.filter(organization=org, obligation_id=locked.obj.pk, pk=cycle_value).exists()
        ):
            raise _NotStored("the renewal's cycle was deleted meanwhile")
        link = TicketLink(
            organization=org,
            created_by=actor,
            system=system,
            external_key=created.key,
            url=created.url,
            title=_printable(created.state.title or title, TITLE_MAX),
            status=created.state.status,
            status_source=StatusSource.REFRESHED,
            external_status=created.state.external_status,
            status_checked_at=timezone.now(),
            created_in_system=True,
            cycle_id=cycle_value or None,
            **locked.filter,
        )
        _save_link(link)
        connection.check_constraints(table_names=[TicketLink._meta.db_table])  # the deferred keys, now
        audit(
            org,
            actor,
            "ticket_link.ticket_created",
            link,
            {**link_values(link), "on": target.kind, "connection": str(conn.id)},
        )
    return link


def create_ticket(org, actor, target: Target, data: dict[str, Any]) -> TicketLink:
    """Create a ticket in Jira, ServiceNow or Halo for an assessment or a renewal and link it. Refused unless the
    system's connection allows creating (`create_enabled`) and is usable. The request to the system runs outside any
    transaction; a failure is audited (`ticket_link.create_failed`, with whether the ticket may exist anyway) and
    answered 409; a success is audited as `ticket_link.ticket_created` with the new link."""
    require_capability(org, TICKET_LINKS)
    errors: dict[str, list[str]] = {}
    for key in sorted(set(data) - set(CREATE_FIELDS)):
        errors[key] = ["This isn't a field of a new ticket."]
    system = data.get("system")
    if system not in SYSTEM_LABELS:
        errors["system"] = [f"Choose one of {', '.join(available_systems(org))}."]
    else:
        require_system(org, system)  # a preview system the installation keeps off: refused before anything else
    title = _text(data.get("title") or target.title(), "title", errors, CREATE_TITLE_MAX, required=True)
    description = _text(data.get("description") or _default_description(target), "description", errors, DESCRIPTION_MAX)
    cycle_value = data.get("cycle")
    if cycle_value not in (None, "") and target.kind != "renewal":
        errors["cycle"] = ["Only a renewal's ticket can name a cycle."]
    elif cycle_value not in (None, "") and not (
        is_uuid(cycle_value)
        and RenewalCycle.objects.filter(organization=org, obligation_id=target.obj.pk, pk=cycle_value).exists()
    ):
        errors["cycle"] = ["Choose one of this renewal's cycles."]
    if errors:
        raise FieldErrors(errors)
    assert isinstance(system, str)
    conn = connection_for(org, system)
    label = SYSTEM_LABELS[system]
    if conn is None or not conn.create_enabled:
        raise Conflict(
            f"Creating {label} tickets is turned off. An Admin can allow it on the {label} ticket connection."
        )
    if system == TicketSystem.JIRA and not conn.create_target:
        raise Conflict("The Jira ticket connection has no project key for new tickets. An Admin can set one.")
    try:
        credential = credential_of(conn)
    except NotUsable as e:
        raise Conflict(str(e)) from None
    try:
        created = clients.create(
            credential,
            target=conn.create_target,
            issue_type=conn.create_issue_type,
            title=title,
            description=description,
        )
    except clients.TicketError as error:
        with transaction.atomic():
            audit(
                org,
                actor,
                "ticket_link.create_failed",
                target.obj,
                {"system": system, "problem": error.kind, "ambiguous": error.ambiguous, "title": title},
            )
        maybe = " It may have been created anyway: check before trying again." if error.ambiguous else ""
        raise Conflict(f"The {label} ticket wasn't created: {error}{maybe}", extra={"problem": error.kind}) from None
    # The ticket now exists in the system. Whatever goes wrong from here, it is audited and named in the answer, so
    # it can be linked by hand.
    try:
        link = _store_created(org, actor, target, system, conn, created, title, cycle_value)
    except (_NotStored, Http404, FieldErrors, DatabaseError) as error:
        key = _printable(created.key, KEY_MAX)
        reason = str(error) if isinstance(error, _NotStored) else type(error).__name__
        # Logged first (without the key, only its length), so there is a trace even if the audit write fails too.
        log.warning(
            "ticket created but not linked org=%s system=%s key_length=%s reason=%s",
            org.pk,
            system,
            len(created.key),
            reason,
        )
        with transaction.atomic():
            audit(
                org,
                actor,
                "ticket_link.create_failed",
                target.obj,
                {"system": system, "problem": "not_linked", "created": True, "external_key": key, "title": title},
            )
        raise Conflict(
            f"The {label} ticket {key} was created, but couldn't be linked here. Link it by hand.",
            extra={"problem": "not_linked", "external_key": key},
        ) from None
    log.info("ticket created org=%s system=%s link=%s", org.pk, system, link.pk)
    return link
