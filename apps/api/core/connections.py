"""Connections: the credentials
an organization's discovery connectors sign in with. Writes are Admin only (core/connection_viewsets.py); each is
validated, scoped to the organization and audited in one transaction.

- MicrosoftConnection: an Entra app registration with a client secret, signing in with client credentials at
  `login.microsoftonline.com/{tenant}` (core/graph.py; the tenant `simulated` uses core/graph_simulator.py).
- HaloConnection (capability `discovery.halo`, every edition): a HaloITSM API application (core/halo.py), its base URL
  checked by core/outbound.check_service_url on save and again on every address when connecting.
- OktaConnection (capability `discovery.okta`): an Okta API service app signing in with a client
  assertion signed by its private key (core/okta.py); the secret slot holds the private key (PEM or JWK), checked
  on save; the org address is checked by core/okta.check_domain (public hosts only, again on every address).
- GoogleWorkspaceConnection (capability `discovery.google`): a service account with domain-wide
  delegation acting as a Workspace administrator (core/google_workspace.py); the secret slot holds the key file's
  private key and key ID, and the service account's email and client ID are read from the file on save.

The client secret is a Fernet slot (core/credentials.py): write-only, sent only to replace it, never in a response, an
ETag, an audit payload or snapshot, a log line, a job payload or an export; an audit event says that it was replaced.

Revision and test (core.models.Connection): a change to how a connection signs in bumps `revision`; `test_connection`
asks the provider for a token and, on success, records `tested_revision = revision`. `usable` (and so every discovery
run, discovery/engine.py) refuses a connection whose `tested_revision` isn't its `revision`. A Microsoft test proves
the tenant, the app and the secret only: a client-credentials token is issued even when no permission has consent, so
each source's own access check proves its permissions. Token errors are classified by their AADSTS code; the stored result is Softwarden's own words and the code, never Microsoft's or Halo's text.

Simulated connections (`is_simulated`: the tenant `simulated`, the Halo and Okta hosts and the Google domain on
`simulated.invalid`) are refused unless core/simulation.allowed(): on save (a field error), at the test (problem
`simulation_off`), in check_usable (so no run starts) and in the transport selection. One saved while it was allowed
is kept and refuses to run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from catalog.services import audit

from . import credentials, google_workspace, graph, halo, okta, outbound, simulation
from .capabilities import DISCOVERY_GOOGLE, DISCOVERY_HALO, DISCOVERY_OKTA, require_capability
from .models import Connection, GoogleWorkspaceConnection, HaloConnection, MicrosoftConnection, OktaConnection
from .problems import Conflict, FieldErrors

SIMULATED = simulation.SIMULATED
NAME_MAX = 100
NOTES_MAX = 5000
SECRET_MAX = 1000
CLIENT_MAX = {"microsoft": 36, "halo": 200, "okta": 100}
KEY_MAX = 12_000  # a private key (PEM or JWK) or a service account key file
BASE_URL_MAX = 300
SCOPE_MAX = 200
SCOPE = re.compile(r"^[A-Za-z0-9:._/-]+( [A-Za-z0-9:._/-]+)*$")
AADSTS = re.compile(r"AADSTS\d{4,9}")
CODE = re.compile(r"^[A-Za-z0-9_]{1,60}$")


def safe_code(value: Any) -> str:
    """A provider error code fit to store and show: letters, digits and underscores, at most 60; else empty."""
    return value if isinstance(value, str) and CODE.match(value) else ""


# The secret slot is never an ETag input; replacing it still moves the ETag through `secret_set_at`.
SECRET_COLUMNS = ("secret_ciphertext", "secret_key_id")


@dataclass(frozen=True)
class Kind:
    key: str
    model: type[Connection]
    noun: str  # the audit action prefix
    label: str
    capability: str | None
    fields: tuple[str, ...]  # settable values besides name, enabled, notes and secret
    derived: tuple[str, ...] = ()  # values read from the secret when it is entered (shown and audited, not settable)
    secret_label: str = "client secret"  # noqa: S105 (what the `secret` field holds, in messages)


MICROSOFT = Kind(
    "microsoft", MicrosoftConnection, "microsoft_connection", "Microsoft connection", None, ("tenant_id", "client_id")
)
HALO = Kind(
    "halo", HaloConnection, "halo_connection", "Halo connection", DISCOVERY_HALO, ("base_url", "client_id", "scope")
)
OKTA = Kind(
    "okta",
    OktaConnection,
    "okta_connection",
    "Okta connection",
    DISCOVERY_OKTA,
    ("base_url", "client_id", "key_id", "scope"),
    secret_label="private key",  # noqa: S106 (a label, not a password)
)
GOOGLE = Kind(
    "google_workspace",
    GoogleWorkspaceConnection,
    "google_workspace_connection",
    "Google Workspace connection",
    DISCOVERY_GOOGLE,
    ("admin_email",),
    derived=("service_account_email", "service_account_client_id"),
    secret_label="service account key file",  # noqa: S106 (a label, not a password)
)
KINDS = {k.key: k for k in (MICROSOFT, HALO, OKTA, GOOGLE)}
COMMON = ("name", "enabled", "notes")
# Fields a create may leave out (a default applies).
OPTIONAL = ("scope", "key_id")


def kind_of(conn: Connection) -> Kind:
    return next(k for k in KINDS.values() if isinstance(conn, k.model))


def is_simulated(conn: Connection | None) -> bool:
    """Whether `conn` signs in to a simulator (synthetic data, no network) rather than a real tenant or host."""
    if isinstance(conn, MicrosoftConnection):
        return conn.tenant_id == SIMULATED
    if isinstance(conn, HaloConnection):
        return halo.is_simulated(conn.base_url)
    if isinstance(conn, OktaConnection):
        return okta.is_simulated(conn.base_url)
    if isinstance(conn, GoogleWorkspaceConnection):
        return google_workspace.is_simulated(conn.admin_email)
    return False


def require_kind(org, kind: Kind) -> None:
    """403 problem naming the capability when the organization lacks this kind's ."""
    if kind.capability:
        require_capability(org, kind.capability)


# ---- validation ---------------------------------------------------------------------------------------------------


def _text(value: Any, key: str, errors: dict, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        errors[key] = ["Enter text."]
        return ""
    value = value.strip()
    if len(value) > limit:
        errors[key] = [f"Use {limit} characters or fewer."]
    elif required and not value:
        errors[key] = ["This is required."]
    return value


def _common(org, kind: Kind, conn: Connection, data: dict, errors: dict) -> None:
    if "name" in data:
        name = _text(data["name"], "name", errors, NAME_MAX, required=True)
        if (
            "name" not in errors
            and kind.model.objects.filter(organization=org, name__iexact=name).exclude(pk=conn.pk).exists()
        ):
            errors["name"] = [f"Another {kind.label.lower()} has this name."]
        conn.name = name
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            errors["enabled"] = ["Choose yes or no."]
        else:
            conn.enabled = data["enabled"]
    if "notes" in data:
        conn.notes = _text(data["notes"], "notes", errors, NOTES_MAX)


def _microsoft(conn: MicrosoftConnection, data: dict, errors: dict) -> None:
    if "tenant_id" in data:
        conn.tenant_id = _text(data["tenant_id"], "tenant_id", errors, 253)
        if "tenant_id" not in errors and conn.tenant_id == SIMULATED and not simulation.allowed():
            errors["tenant_id"] = [simulation.MESSAGE]
        elif "tenant_id" not in errors and not (conn.tenant_id == SIMULATED or graph.valid_tenant(conn.tenant_id)):
            errors["tenant_id"] = ["Enter the directory (tenant) ID, a GUID, or the tenant's domain name."]
    if "client_id" in data:
        conn.client_id = _text(data["client_id"], "client_id", errors, CLIENT_MAX["microsoft"])
    if ("tenant_id" in data or "client_id" in data) and "client_id" not in errors:
        if conn.tenant_id != SIMULATED and not graph.GUID.match(conn.client_id):
            errors["client_id"] = ["Enter the application (client) ID, a GUID."]


def _halo(conn: HaloConnection, data: dict, errors: dict) -> None:
    if "base_url" in data:
        url = _text(data["base_url"], "base_url", errors, BASE_URL_MAX, required=True)
        if "base_url" not in errors and halo.is_simulated(url) and not simulation.allowed():
            errors["base_url"] = [simulation.MESSAGE]
        elif "base_url" not in errors:
            try:
                outbound.check_service_url(url, max_length=BASE_URL_MAX)
            except outbound.UnsafeDestination as e:
                errors["base_url"] = [str(e)]
        conn.base_url = url.rstrip("/")
    if "client_id" in data:
        conn.client_id = _text(data["client_id"], "client_id", errors, CLIENT_MAX["halo"], required=True)
    if "scope" in data:
        scope = _text(data["scope"], "scope", errors, SCOPE_MAX) or halo.DEFAULT_SCOPE
        if "scope" not in errors and not SCOPE.match(scope):
            errors["scope"] = ["Enter scopes separated by spaces, such as read:assets."]
        conn.scope = scope


def _okta(conn: OktaConnection, data: dict, errors: dict) -> None:
    if "base_url" in data:
        url = _text(data["base_url"], "base_url", errors, BASE_URL_MAX, required=True)
        if "base_url" not in errors:
            try:
                url = okta.check_domain(url)
            except outbound.UnsafeDestination as e:
                errors["base_url"] = [str(e)]
            else:
                if okta.is_simulated(url) and not simulation.allowed():
                    errors["base_url"] = [simulation.MESSAGE]
        conn.base_url = url.rstrip("/")
    if "client_id" in data:
        conn.client_id = _text(data["client_id"], "client_id", errors, CLIENT_MAX["okta"], required=True)
        if "client_id" not in errors and not okta.CLIENT_ID.match(conn.client_id):
            errors["client_id"] = ["Enter the service app's client ID (letters, digits, - and _)."]
    if "key_id" in data:
        conn.key_id = _text(data["key_id"], "key_id", errors, 200)
        if "key_id" not in errors and conn.key_id and not okta.KEY_ID.match(conn.key_id):
            errors["key_id"] = ["Enter the key's ID (kid) as Okta shows it, or leave it empty."]
    if "scope" in data:
        scope = _text(data["scope"], "scope", errors, SCOPE_MAX)
        if "scope" not in errors:
            try:
                conn.scope = okta.check_scope(scope)
            except ValueError as e:
                errors["scope"] = [str(e)]


def _google(conn: GoogleWorkspaceConnection, data: dict, errors: dict) -> None:
    if "admin_email" in data:
        email = _text(data["admin_email"], "admin_email", errors, 254, required=True)
        if "admin_email" not in errors:
            try:
                email = google_workspace.check_admin_email(email)
            except ValueError as e:
                errors["admin_email"] = [str(e)]
            else:
                if google_workspace.is_simulated(email) and not simulation.allowed():
                    errors["admin_email"] = [simulation.MESSAGE]
        conn.admin_email = email


def _secret_value(kind: Kind, conn: Connection, value: str) -> str:
    """What the secret slot keeps for `value` as entered, checked for the kind (ValueError with a safe message): a
    client secret as is; an Okta private key once it loads; for Google the key file's private key and key ID, the
    service account's email and client ID going to their own columns."""
    if kind is OKTA:
        okta.load_key(value)
        return value
    if kind is GOOGLE:
        assert isinstance(conn, GoogleWorkspaceConnection)
        key = google_workspace.parse_key_file(value)
        conn.service_account_email, conn.service_account_client_id = key.client_email, key.client_id
        return key.secret()
    if len(value) > SECRET_MAX:
        raise ValueError(f"Use {SECRET_MAX} characters or fewer.")
    return value


def _secret(kind: Kind, conn: Connection, data: dict, errors: dict) -> list[str]:
    """Store a new secret when one is sent. Returns ["secret"] when it was replaced. Blank or null is refused: leave
    the field out to keep the stored one. A refused value is never repeated in the message."""
    if "secret" not in data:
        return []
    value = data["secret"]
    if not isinstance(value, str) or not value.strip():
        errors["secret"] = [f"Enter the {kind.secret_label}, or leave the field out to keep the stored one."]
        return []
    if len(value.strip()) > KEY_MAX:
        errors["secret"] = [f"Use {KEY_MAX} characters or fewer."]
        return []
    try:
        credentials.slot_set(conn, "secret", _secret_value(kind, conn, value.strip()))
    except (ValueError, credentials.CredentialError) as e:
        errors["secret"] = [str(e)]
        return []
    return ["secret"]


def _apply(org, kind: Kind, conn: Connection, data: dict) -> tuple[dict, list[str]]:
    errors: dict[str, list[str]] = {}
    for key in sorted(set(data) - set(COMMON) - set(kind.fields) - {"secret"}):
        errors[key] = [f"This isn't a setting of a {kind.label.lower()}."]
    # A kind that sends its secret (Halo) or a signed assertion (Okta) to an address the Admin types keeps the
    # stored secret only while that address names the same scheme, host and port; Microsoft and Google sign in at
    # fixed hosts.
    addressed = "base_url" in kind.fields
    destination_before = outbound.origin(getattr(conn, "base_url", "")) if addressed else None
    had_secret = bool(conn.secret_ciphertext)
    _common(org, kind, conn, data, errors)
    if isinstance(conn, MicrosoftConnection):
        _microsoft(conn, data, errors)
    elif isinstance(conn, OktaConnection):
        _okta(conn, data, errors)
    elif isinstance(conn, GoogleWorkspaceConnection):
        _google(conn, data, errors)
    else:
        assert isinstance(conn, HaloConnection)
        _halo(conn, data, errors)
    replaced = _secret(kind, conn, data, errors)
    if addressed and had_secret and "secret" not in data and "base_url" not in errors:
        if outbound.secret_must_follow(destination_before, outbound.origin(getattr(conn, "base_url", ""))):
            errors["secret"] = [f"The address now points at another host: enter the {kind.secret_label} again."]
    return errors, replaced


def values(conn: Connection) -> dict[str, Any]:
    """The audited settings: never the secret slot."""
    kind = kind_of(conn)
    return {f: getattr(conn, f) for f in (*COMMON, *kind.fields, *kind.derived)}


# ---- writes -------------------------------------------------------------------------------------------------------


@transaction.atomic
def create_connection(org, actor, kind: Kind, data: dict[str, Any]) -> Connection:
    require_kind(org, kind)
    conn = kind.model(organization=org)
    required = {"name": "", **{f: "" for f in kind.fields if f not in OPTIONAL}}
    data = {**required, **data}
    errors, replaced = _apply(org, kind, conn, data)
    if not replaced and "secret" not in errors:
        errors["secret"] = [f"Enter the {kind.secret_label}."]
    if errors:
        raise FieldErrors(errors)
    try:
        with transaction.atomic():
            conn.save()
    except IntegrityError:
        raise FieldErrors({"name": [f"Another {kind.label.lower()} has this name."]}) from None
    audit(org, actor, f"{kind.noun}.created", conn, {**values(conn), "replaced": replaced})
    return conn


@transaction.atomic
def update_connection(org, actor, conn: Connection, data: dict[str, Any]) -> Connection:
    kind = kind_of(conn)
    require_kind(org, kind)
    conn = kind.model.objects.select_for_update().get(organization=org, pk=conn.pk)
    before, before_revision = values(conn), conn.revision
    errors, replaced = _apply(org, kind, conn, data)
    if errors:
        raise FieldErrors(errors)
    after = values(conn)
    changed = {f: [before[f], after[f]] for f in after if before[f] != after[f]}
    if not changed and not replaced:
        return conn
    try:
        with transaction.atomic():
            conn.save()  # bumps the revision when a connection value or the secret changed (Connection.save)
    except IntegrityError:
        raise FieldErrors({"name": [f"Another {kind.label.lower()} has this name."]}) from None
    audit(
        org,
        actor,
        f"{kind.noun}.updated",
        conn,
        {**changed, "replaced": replaced, "revision": [before_revision, conn.revision]},
    )
    return conn


def users(conn: Connection) -> list[dict[str, str]]:
    """The connectors that sign in through `conn`, by name."""
    return [{"id": str(i), "name": n} for i, n in conn.connectors.order_by("name").values_list("id", "name")]  # type: ignore[attr-defined]


@transaction.atomic
def delete_connection(org, actor, conn: Connection) -> None:
    """Refused (409, naming them) while any connector uses the connection."""
    kind = kind_of(conn)
    require_kind(org, kind)
    conn = kind.model.objects.select_for_update().get(organization=org, pk=conn.pk)
    used_by = users(conn)
    if used_by:
        names = ", ".join(u["name"] for u in used_by)
        noun, pronoun = ("a connector", "it") if len(used_by) == 1 else ("connectors", "them")
        raise Conflict(
            f"“{conn.name}” is used by {noun}: {names}. Point {pronoun} at another connection or delete {pronoun} "
            "first.",
            extra={"connectors": used_by},
        )
    # A Halo ticket connection (tickets.TicketConnection) signs in through it too.
    tickets: list[dict[str, str]] = []
    if isinstance(conn, HaloConnection):
        rows = conn.ticket_connections.order_by("name").values_list("id", "name")
        tickets = [{"id": str(i), "name": n} for i, n in rows]
    if tickets:
        raise Conflict(
            f"“{conn.name}” is used by the ticket connection “{tickets[0]['name']}”. Delete that ticket connection "
            "first.",
            extra={"ticket_connections": tickets},
        )
    audit(org, actor, f"{kind.noun}.deleted", conn, {"name": conn.name})
    conn.delete()


def resolve(org, kind: Kind, value: Any, errors: dict, key: str) -> Connection | None:
    """The organization's connection of `kind` with this ID, locked until the caller's transaction ends (so it can't be
    deleted under a connector being pointed at it). Another organization's ID is reported as unknown."""
    from catalog.services import is_uuid

    conn = kind.model.objects.select_for_update().filter(organization=org, pk=value).first() if is_uuid(value) else None
    if conn is None:
        errors[key] = [f"Choose an existing {kind.label.lower()}."]
    return conn


# ---- the connection test ------------------------------------------------------------------------------------------

PROBLEMS = {
    "not_configured": "Not configured",
    "wrong_credentials": "Wrong credentials",
    "missing_permission": "Missing permission",
    "disabled": "Disabled",
    "unreachable": "Couldn't connect",
    "refused": "Address refused",
    "unavailable": "Not found",
    "changed": "Changed during the test",
    "simulation_off": "Simulation not allowed",
    "error": "Unexpected error",
}

# Entra token errors by AADSTS code: (problem, what went wrong, what to do), the
# credential codes plus the consent codes. docs/PERMISSIONS.md, "Connection test", lists them and which were checked on Microsoft Learn.
MICROSOFT_CODES: dict[str, tuple[str, str, str]] = {
    "AADSTS7000215": (
        "wrong_credentials",
        "The client secret is wrong.",
        "Create a new client secret in the app registration and paste its Value (not the Secret ID).",
    ),
    "AADSTS7000222": (
        "wrong_credentials",
        "The client secret has expired.",
        "Create a new client secret in the app registration and replace it here.",
    ),
    "AADSTS700016": (
        "wrong_credentials",
        "No app with this client ID exists in the tenant.",
        "Check the application (client) ID and the directory (tenant) ID.",
    ),
    "AADSTS90002": ("wrong_credentials", "The tenant wasn't found.", "Check the directory (tenant) ID."),
    "AADSTS900023": (
        "wrong_credentials",
        "The tenant ID isn't valid.",
        "Use the directory (tenant) ID, a GUID, or one of the tenant's verified domains.",
    ),
    "AADSTS7000112": (
        "disabled",
        "The app is disabled in the tenant.",
        "Enable it under Enterprise applications in the Microsoft Entra admin center.",
    ),
    "AADSTS65001": (
        "missing_permission",
        "An administrator hasn't consented to the app in this tenant.",
        "Grant admin consent for the app's API permissions (App registrations, API permissions), then test again.",
    ),
    "AADSTS7000229": (
        "missing_permission",
        "The app has no enterprise application (service principal) in this tenant: nobody has consented to it there.",
        "Have an administrator of the tenant grant admin consent to the app, then test again.",
    ),
}


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    problem: str = ""
    code: str = ""  # the provider's error code (an AADSTS code, or an OAuth error such as invalid_client)
    detail: str = ""
    next: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "problem": self.problem,
            "problem_label": PROBLEMS.get(self.problem, ""),
            "code": self.code,
            "detail": self.detail,
            "next": self.next,
        }


MICROSOFT_OK = ProbeResult(
    True,
    detail="Microsoft issued a token: the tenant, the app and its client secret are right.",
    next="Each discovery source's access check confirms the permissions it needs; a token alone doesn't prove them.",
)
HALO_OK = ProbeResult(True, detail="Halo issued a token for this client and scope.")
OKTA_OK = ProbeResult(
    True,
    detail="Okta issued a token: the address, the service app, its key and the scopes are right.",
    next="The connector's access check confirms the app's admin role can read applications.",
)
GOOGLE_OK = ProbeResult(
    True,
    detail="Google issued a token acting as the administrator: the key, domain-wide delegation and the scope are "
    "right.",
    next="The connector's access check confirms the administrator can read the OAuth token audit log.",
)


# Overridable in tests: the transport for a tenant or a Halo address. `simulated` and halo.simulated.invalid never
# leave the machine, and are refused (simulation.NotAllowed, or the provider's refused error) unless allowed.
def microsoft_transport(tenant: str) -> graph.Transport:
    if tenant == SIMULATED:
        simulation.require()
        from . import graph_simulator

        return graph_simulator.Transport()
    return graph.HttpTransport()


def halo_transport(base_url: str) -> halo.Transport:
    return halo.transport_for(base_url)


def okta_transport(base_url: str):
    return okta.transport_for(base_url)


def google_transport(admin_email: str):
    return google_workspace.transport_for(admin_email)


def classify_microsoft(error: graph.GraphError) -> ProbeResult:
    """A refused token request, by its AADSTS code (in the error description) or else its OAuth error."""
    found = AADSTS.search(error.message or "")
    code = safe_code(found.group(0) if found else error.code)
    if code in MICROSOFT_CODES:
        problem, detail, advice = MICROSOFT_CODES[code]
        return ProbeResult(False, problem, code, detail, advice)
    if error.status in (429, 500, 502, 503, 504):
        return ProbeResult(
            False,
            "unreachable",
            code,
            f"The Microsoft sign-in service is busy or unavailable ({error.status}).",
            "Try again in a few minutes.",
        )
    if error.code in ("invalid_client", "unauthorized_client"):
        return ProbeResult(
            False,
            "wrong_credentials",
            code,
            "Microsoft refused the app's credentials.",
            "Check the directory (tenant) ID, the application (client) ID and the client secret.",
        )
    return ProbeResult(
        False,
        "error",
        code,
        f"Microsoft refused the token request ({error.status}{', ' + code if code else ''}).",
        "Check the directory (tenant) ID, the application (client) ID and the client secret.",
    )


def _secret_or_result(conn: Connection) -> tuple[str, ProbeResult | None]:
    label = kind_of(conn).secret_label
    if not conn.secret_ciphertext:
        return "", ProbeResult(False, "not_configured", detail=f"No {label} is stored.", next=f"Enter the {label}.")
    try:
        return credentials.slot_value(conn, "secret"), None
    except credentials.CredentialError as e:
        return "", ProbeResult(False, "not_configured", detail=str(e), next=f"Enter the {label} again.")


def _probe_microsoft(conn: MicrosoftConnection) -> ProbeResult:
    secret, problem = _secret_or_result(conn)
    if problem is not None:
        return problem
    try:
        graph.request_token(microsoft_transport(conn.tenant_id), conn.tenant_id, conn.client_id, secret)
    except graph.GraphError as e:
        return classify_microsoft(e)
    except graph.Unreachable:
        return ProbeResult(
            False,
            "unreachable",
            detail="Couldn't reach the Microsoft sign-in service from this server.",
            next="Check the server's outbound internet access (proxy, firewall) and try again.",
        )
    return MICROSOFT_OK


def classify_halo(error: halo.HaloError) -> ProbeResult:
    if error.refused:
        return ProbeResult(
            False,
            "refused",
            detail=error.reason or outbound.SERVICE_REFUSED,
            next="Use the Halo instance's own address; loopback, link-local and metadata addresses aren't allowed.",
        )
    if error.status == 0:
        return ProbeResult(
            False,
            "unreachable",
            detail="Couldn't reach the Halo instance from this server.",
            next="Check the Halo URL and that this server can reach it.",
        )
    if error.code == "invalid_scope":
        return ProbeResult(
            False,
            "missing_permission",
            error.code,
            "Halo refused the scope.",
            "Check the scope here and the API application's permissions in Halo.",
        )
    if error.code in ("invalid_client", "unauthorized_client") or error.status in (400, 401):
        return ProbeResult(
            False,
            "wrong_credentials",
            error.code,
            "Halo refused the client ID or secret.",
            "Check the client ID and secret of the Halo API application (client credentials).",
        )
    if error.status == 404:
        return ProbeResult(
            False,
            "unavailable",
            error.code,
            "The address answered, but not as a Halo token endpoint.",
            "Check the Halo URL, for example https://yourcompany.haloitsm.com.",
        )
    if error.status in (429, 500, 502, 503, 504):
        return ProbeResult(
            False, "unreachable", error.code, f"Halo is busy or unavailable ({error.status}).", "Try again later."
        )
    return ProbeResult(
        False,
        "error",
        error.code,
        f"Halo refused the token request ({error.status}).",
        "Check the URL, client ID, secret and scope.",
    )


def _probe_halo(conn: HaloConnection) -> ProbeResult:
    secret, problem = _secret_or_result(conn)
    if problem is not None:
        return problem
    try:
        halo.request_token(halo_transport(conn.base_url), conn.base_url, conn.client_id, secret, conn.scope)
    except halo.HaloError as e:
        return classify_halo(e)
    return HALO_OK


def classify_okta(error: okta.OktaError) -> ProbeResult:
    """A refused Okta token request, by its OAuth error code (Okta's own text is never kept)."""
    if error.refused:
        return ProbeResult(
            False,
            "refused",
            detail=error.reason or outbound.SERVICE_REFUSED,
            next="Use your Okta org's public address; private, loopback and metadata addresses aren't allowed.",
        )
    if error.status == 0:
        return ProbeResult(
            False,
            "unreachable",
            detail="Couldn't reach Okta from this server.",
            next="Check the Okta address and the server's outbound internet access.",
        )
    if error.code == "invalid_scope":
        return ProbeResult(
            False,
            "missing_permission",
            error.code,
            "Okta refused the scopes: the service app hasn't been granted them.",
            "In the Okta Admin Console, open the service app, Okta API Scopes, and grant okta.apps.read; then test "
            "again.",
        )
    if error.code in ("invalid_dpop_proof", "use_dpop_nonce"):
        return ProbeResult(
            False,
            "error",
            error.code,
            "Okta asked for a DPoP proof, which Softwarden doesn't send.",
            "In the service app's General settings, turn off Require Demonstrating Proof of Possession (DPoP) header "
            "in token requests, then test again.",
        )
    if error.code in ("invalid_client", "unauthorized_client") or error.status in (400, 401):
        return ProbeResult(
            False,
            "wrong_credentials",
            error.code,
            "Okta refused the service app's client ID or key.",
            "Check the client ID, that the app's public key (its JWKS) matches the private key here, the key ID, and "
            "that the app's client authentication is Public key / Private key.",
        )
    if error.status == 404:
        return ProbeResult(
            False,
            "unavailable",
            error.code,
            "The address answered, but not as an Okta org's token endpoint.",
            "Check the Okta address, for example https://yourcompany.okta.com.",
        )
    if error.status in (429, 500, 502, 503, 504):
        return ProbeResult(
            False, "unreachable", error.code, f"Okta is busy or unavailable ({error.status}).", "Try again later."
        )
    return ProbeResult(
        False, "error", error.code, f"Okta refused the token request ({error.status}).", "Check the connection."
    )


def _probe_okta(conn: OktaConnection) -> ProbeResult:
    secret, problem = _secret_or_result(conn)
    if problem is not None:
        return problem
    try:
        okta.request_token(
            okta_transport(conn.base_url), conn.base_url, conn.client_id, secret, conn.key_id, conn.scope
        )
    except okta.KeyProblem as e:
        return ProbeResult(False, "not_configured", detail=str(e), next="Enter the private key again.")
    except okta.OktaError as e:
        return classify_okta(e)
    return OKTA_OK


def classify_google(error: google_workspace.GoogleError, client_id: str = "") -> ProbeResult:
    """A refused Google token request, by its OAuth error code (Google's own text is never kept). `client_id` is the
    service account's, named in the domain-wide delegation advice."""
    client = client_id or "the service account's client ID"
    if error.refused:
        return ProbeResult(False, "refused", detail=error.reason or "Refused to call Google.")
    if error.status == 0:
        return ProbeResult(
            False,
            "unreachable",
            detail="Couldn't reach Google from this server.",
            next="Check the server's outbound internet access (proxy, firewall) and try again.",
        )
    if error.code in ("unauthorized_client", "access_denied"):
        return ProbeResult(
            False,
            "missing_permission",
            error.code,
            "Google refused: the service account isn't authorized for the Reports scope under domain-wide delegation.",
            f"A super administrator adds client ID {client} under Security, Access and data control, API controls, "
            f"Domain-wide delegation, with the scope {google_workspace.REPORTS_SCOPE}. It can take from minutes to "
            "a day to apply.",
        )
    if error.code == "invalid_grant":
        return ProbeResult(
            False,
            "wrong_credentials",
            error.code,
            "Google refused the sign-in: the administrator address doesn't exist, the key was deleted, disabled or "
            "expired, or this server's clock is off.",
            "Check the administrator's address, create a new key for the service account if needed, and check the "
            "server's time.",
        )
    if error.code == "invalid_client" or error.status in (400, 401):
        return ProbeResult(
            False,
            "wrong_credentials",
            error.code,
            "Google refused the service account's key.",
            "Enter the key file again, from the service account it belongs to.",
        )
    if error.status in (429, 500, 502, 503, 504):
        return ProbeResult(
            False, "unreachable", error.code, f"Google is busy or unavailable ({error.status}).", "Try again later."
        )
    return ProbeResult(
        False, "error", error.code, f"Google refused the token request ({error.status}).", "Check the connection."
    )


def _probe_google(conn: GoogleWorkspaceConnection) -> ProbeResult:
    secret, problem = _secret_or_result(conn)
    if problem is not None:
        return problem
    try:
        google_workspace.request_token(
            google_transport(conn.admin_email), conn.service_account_email, secret, conn.admin_email
        )
    except google_workspace.KeyProblem as e:
        return ProbeResult(False, "not_configured", detail=str(e), next="Enter the key file again.")
    except google_workspace.GoogleError as e:
        return classify_google(e, conn.service_account_client_id)
    return GOOGLE_OK


TEST_FIELDS = ["tested_at", "test_ok", "test_result", "tested_by", "tested_revision", "updated_at"]
SIMULATION_OFF = ProbeResult(False, "simulation_off", detail=simulation.MESSAGE, next=simulation.NEXT)


def run_test(org, actor, conn: Connection) -> Connection:
    """Ask the provider for a token, outside any transaction, then record the result. Only a success at an unchanged
    revision sets `tested_revision`; a failure clears it. A disabled connection can be tested too."""
    kind = kind_of(conn)
    require_kind(org, kind)
    conn = _get_or_404(kind.model.objects.filter(organization=org, pk=conn.pk))
    started_at = conn.revision
    if is_simulated(conn) and not simulation.allowed():
        result = SIMULATION_OFF
    elif isinstance(conn, MicrosoftConnection):
        result = _probe_microsoft(conn)
    elif isinstance(conn, OktaConnection):
        result = _probe_okta(conn)
    elif isinstance(conn, GoogleWorkspaceConnection):
        result = _probe_google(conn)
    else:
        assert isinstance(conn, HaloConnection)
        result = _probe_halo(conn)
    with transaction.atomic():
        # Deleted during the test: 404, as for any other organization's or unknown ID.
        row = _get_or_404(kind.model.objects.select_for_update().filter(organization=org, pk=conn.pk))
        if row.revision != started_at:
            # Changed during the test: the outcome describes values that are gone, so the stored test state (which
            # may be a newer test's) stays as it is; only this response and the audit event say "changed".
            result = ProbeResult(
                False, "changed", detail="The connection changed while it was being tested.", next="Test it again."
            )
        else:
            row.tested_at, row.test_ok, row.tested_by = timezone.now(), result.ok, actor
            row.test_result = result.as_dict()
            row.tested_revision = row.revision if result.ok else None
            row.save(update_fields=TEST_FIELDS)
        row.probe = result  # this call's outcome, for the response
        audit(
            org,
            actor,
            f"{kind.noun}.tested",
            row,
            {"ok": result.ok, "problem": result.problem, "code": result.code, "revision": row.revision},
        )
    return row


def _get_or_404(queryset):
    from django.http import Http404

    found = queryset.first()
    if found is None:
        raise Http404
    return found


# ---- use ------------------------------------------------------------------------------------------------------------


class NotUsable(Exception):
    """Why a connection can't be used now. The message is safe to show and log: it never contains a secret."""


@dataclass(frozen=True)
class MicrosoftCredential:
    tenant_id: str
    client_id: str
    secret: str = field(repr=False)


@dataclass(frozen=True)
class HaloCredential:
    base_url: str
    client_id: str
    secret: str = field(repr=False)
    scope: str = halo.DEFAULT_SCOPE


@dataclass(frozen=True)
class OktaCredential:
    base_url: str
    client_id: str
    key_id: str
    scope: str
    secret: str = field(repr=False)  # the private key


@dataclass(frozen=True)
class GoogleCredential:
    admin_email: str
    client_email: str
    client_id: str  # the service account's numeric client ID (named in advice)
    secret: str = field(repr=False)  # the private key and its ID (google_workspace.ServiceAccountKey.secret)


Credential = MicrosoftCredential | HaloCredential | OktaCredential | GoogleCredential


def redact_values(credential) -> list[str]:
    """What a run's messages and logs must never carry for this credential (discovery/engine._redact): the secret as
    stored, and for a private key (Okta's PEM, the PEM inside Google's stored JSON) each line of its body, since a
    message could quote it in another form (JSON-escaped, one line)."""
    secret = getattr(credential, "secret", "") or ""
    values = [secret]
    pem = secret
    if secret.lstrip().startswith("{"):
        try:
            data = json.loads(secret)
        except ValueError:
            data = {}
        pem = str(data.get("private_key") or "") if isinstance(data, dict) else ""
        values.append(pem)
    values += [line.strip() for line in pem.splitlines() if line.strip() and not line.startswith("-----")]
    return [v for v in values if v]


def check_usable(conn: Connection | None) -> None:
    """NotUsable unless `conn` is on, isn't a simulated one this server refuses, has a secret and passed its test at its
    current revision."""
    if conn is None:
        raise NotUsable("No connection is chosen for this connector. Choose one in the connector's settings.")
    if not conn.enabled:
        raise NotUsable(f"The connection “{conn.name}” is turned off. Turn it on, or choose another connection.")
    if is_simulated(conn) and not simulation.allowed():
        raise NotUsable(simulation.refused(conn.name))
    if not conn.secret_ciphertext:
        raise NotUsable(
            f"The connection “{conn.name}” has no {kind_of(conn).secret_label}. Enter it, then test the connection."
        )
    if conn.test_current:
        return
    if conn.tested_at is None:
        raise NotUsable(f"The connection “{conn.name}” hasn't been tested. Test it before running this connector.")
    if not conn.test_ok:
        raise NotUsable(
            f"The connection “{conn.name}” failed its last test. Fix it and test it again before running this "
            "connector."
        )
    raise NotUsable(
        f"The connection “{conn.name}” changed after its last successful test (revision {conn.revision}; tested at "
        f"revision {conn.tested_revision}). Test it again before running this connector."
    )


def credential_for(conn: Connection, secret: str) -> Credential:
    """The sign-in values of `conn` with its decrypted `secret` (the caller decrypted it)."""
    if isinstance(conn, HaloConnection):
        return HaloCredential(conn.base_url, conn.client_id, secret, conn.scope or halo.DEFAULT_SCOPE)
    if isinstance(conn, OktaConnection):
        return OktaCredential(conn.base_url, conn.client_id, conn.key_id, conn.scope or okta.DEFAULT_SCOPE, secret)
    if isinstance(conn, GoogleWorkspaceConnection):
        return GoogleCredential(conn.admin_email, conn.service_account_email, conn.service_account_client_id, secret)
    assert isinstance(conn, MicrosoftConnection)
    return MicrosoftCredential(conn.tenant_id, conn.client_id, secret)


def credential(conn: Connection | None) -> Credential:
    """The sign-in values and decrypted secret of a usable connection of either kind. NotUsable otherwise."""
    check_usable(conn)
    assert conn is not None
    try:
        secret = credentials.slot_value(conn, "secret")
    except credentials.CredentialError as e:
        raise NotUsable(str(e)) from None
    return credential_for(conn, secret)


def microsoft_credential(conn: MicrosoftConnection | None) -> MicrosoftCredential:
    """The tenant, client and decrypted secret of a usable connection. NotUsable otherwise."""
    check_usable(conn)
    assert conn is not None
    try:
        secret = credentials.slot_value(conn, "secret")
    except credentials.CredentialError as e:
        raise NotUsable(str(e)) from None
    return MicrosoftCredential(conn.tenant_id, conn.client_id, secret)
