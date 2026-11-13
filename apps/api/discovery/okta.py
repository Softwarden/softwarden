"""The Okta source (capability `discovery.okta`): the applications an
organization runs through Okta, as application candidates with their sign-on mode and assigned-user counts, through
the connector's OktaConnection (core/okta.py: an API service app, private-key client assertion, read-only scopes).

Not Microsoft, so nothing here is on Microsoft Learn. Checked on 2026-11-10 against Okta's Management API OpenAPI
document (okta/okta-management-openapi-spec, `dist/current/management-minimal.yaml`, version 2026.44.0) and developer
guides (docs/PERMISSIONS.md, "Discovery source: `okta`"):

- `GET /api/v1/apps` (listApplications), OAuth scope `okta.apps.read`: a JSON array of applications, `limit` up to 200,
  `includeNonDeleted=true` so inactive apps are listed too (they are out of scope, never "no longer seen"); further
  pages through the `link` header's rel="next" URL, followed as given (Okta: "treat the link as an opaque value").
  Each app has `id`, `name` (the app's key, e.g. `salesforce`), `label` (its display name), `status` (ACTIVE, INACTIVE),
  `signOnMode` (SAML_2_0, OPENID_CONNECT, AUTO_LOGIN, BOOKMARK, ...), `features`, `created`, `lastUpdated`.
- `GET /api/v1/apps/{appId}/users` (listApplicationUsers), the same scope, `limit` up to 500, link-paged: counted per
  kept app when the scope's `assignment_counts` is on (the default), at most USER_PAGES pages per app (beyond that the
  count is "at least"). A refused count is a note, never a failed run, and counting stops COUNT_MARGIN before the
  run's deadline so the listing is applied. No user is stored: only how many.
- Rate limits: X-Rate-Limit-* headers and 429s are honoured (discovery/rest_source.py).
- With OAuth for Okta a scope works within the admin role the service app holds; Read-only Administrator is enough to
  read applications. A 403 on a read therefore names the role.

Each app is an `application` observation keyed by its Okta app ID (`data.okta_app_id`), named by its label. Okta apps
have no owners in the API, so there are no suggested owners. Filters: inactive apps (default on), Okta's own apps
(Admin Console `saasure`, Dashboard `okta_enduser`, Browser Plugin `okta_browser_plugin`; default on), bookmark apps
(default off) and name exclusions. If the listing comes back empty while the connector has live observations, the run
is refused (the empty-listing guard). The host `okta.simulated.invalid` selects the synthetic Okta
(discovery/okta_simulator.py)."""

from __future__ import annotations

import urllib.parse
from typing import Any

from core import connections, okta, simulation

from . import access, rest_source, scopes
from .graph_source import Fetched, Permission, Refused, SignInRefused, SourceError
from .models import ObservationKind

PAGE_SIZE = 200  # listApplications' maximum
PAGE_LIMIT = 100  # 20,000 apps
USER_PAGE_SIZE = 500  # listApplicationUsers' maximum
USER_PAGES = 20  # 10,000 users counted per app at most
# Counting stops this long before the run's deadline, so the listing it already read is applied rather than lost.
COUNT_MARGIN = 5 * 60.0
# Okta's own apps every org has (their `name`): not software the organization chose. Seen in Okta orgs' app lists;
# not in the OpenAPI document, so confirm during live acceptance.
OKTA_APPS = frozenset({"saasure", "okta_enduser", "okta_browser_plugin"})
ASSIGNED = "_softwarden_assigned"  # set on a raw app by fetch: (count, capped); never stored as is
PROVISIONING = frozenset({"PUSH_NEW_USERS", "PUSH_PROFILE_UPDATES", "SCIM_PROVISIONING"})

OKTA_APPS_READ = Permission(
    "okta.apps.read",
    "",
    True,
    "List the org's applications and count each one's assigned users. An OAuth scope granted to the service app "
    "(Okta API Scopes). Read only.",
    False,  # not Microsoft: there is no Learn page; Okta's developer documentation is the reference
    kind="okta_scope",
    advice="In the Okta Admin Console, open the service app, Okta API Scopes, and grant okta.apps.read; then test the "
    "connection and check again.",
)
READ_ONLY_ADMIN = Permission(
    "Read-only Administrator",
    "READ_ONLY_ADMIN",
    True,
    "The admin role the service app holds: with OAuth for Okta a scope only works within the app's admin role, and "
    "Read-only Administrator is enough to read applications and their users.",
    False,
    kind="okta_admin_role",
    advice="In the Okta Admin Console, open the service app, Admin roles, Edit assignments, and assign Read-only "
    "Administrator; then check again.",
)

KINDS = [ObservationKind.APPLICATION]
CONNECTION = "okta"
CONFIG_FIELDS: tuple[str, ...] = ()
SCOPE_DEFAULTS: dict[str, Any] = {
    "exclude_inactive": True,
    "exclude_okta_apps": True,
    "exclude_bookmarks": False,
    "assignment_counts": True,
    "name_exclusions": [],
}
FILTERS = {
    "no_id": "no ID in Okta",
    "inactive": "inactive in Okta",
    "okta_app": "Okta's own app (Admin Console, Dashboard, Browser Plugin)",
    "bookmark": "a bookmark app (a link, no sign-in)",
    "name_exclusions": "name excluded",
}
PERMISSIONS = [OKTA_APPS_READ, READ_ONLY_ADMIN]
DETAILS = (
    "okta_app_id",
    "app_name",
    "sign_on_mode",
    "status",
    "provisioning",
    "assigned_users",
    "assigned_users_capped",
    "created",
)
# Data keys that change with use, not with the app: never a change of a candidate (discovery/engine._apply).
VOLATILE = ("assigned_users", "assigned_users_capped", "last_updated")
BOOLEANS = ("exclude_inactive", "exclude_okta_apps", "exclude_bookmarks", "assignment_counts")


def simulator(connector):
    from . import okta_simulator

    return okta_simulator.Transport(connector)


def scope_of(connector) -> dict[str, Any]:
    return dict(SCOPE_DEFAULTS) | dict(connector.scope or {})


def validate(config, scope, errors):
    for key in BOOLEANS:
        if key in scope and not isinstance(scope[key], bool):
            errors[f"scope.{key}"] = ["Choose yes or no."]
    scopes.validate_names(scope, errors)


# ---- reading -------------------------------------------------------------------------------------------------------


def _error_code(body: Any) -> str:
    """Okta's error body: {"errorCode": "E0000006", "errorSummary": ...}; only the code is read."""
    return str(body.get("errorCode") or "") if isinstance(body, dict) else ""


def service(base_url: str) -> rest_source.Service:
    host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    return rest_source.Service("Okta", frozenset({(host, 443)}), 2.0, _error_code)


def transport_for(connector, credential, transport=None):
    """`transport` (tests), the synthetic Okta for its reserved host (SourceError unless core/simulation.allowed()),
    else core/outbound under the public rule."""
    if transport is not None:
        return transport
    if okta.is_simulated(credential.base_url):
        if not simulation.allowed():
            raise SourceError(simulation.MESSAGE)
        return simulator(connector)
    return okta.transport_for(credential.base_url)


def client(connector, credential, transport=None, **limits) -> rest_source.Api:
    transport = transport_for(connector, credential, transport)

    def sign_in() -> str:
        try:
            body = okta.request_token(
                transport,
                credential.base_url,
                credential.client_id,
                credential.secret,
                credential.key_id,
                credential.scope,
            )
        except okta.KeyProblem as e:
            raise SignInRefused(str(e), "not_configured") from None
        except okta.OktaError as e:
            result = connections.classify_okta(e)
            raise SignInRefused(
                f"Okta refused to sign in: {result.detail} {result.next}".strip(), result.problem, result.code
            ) from None
        return str(body["access_token"])

    return rest_source.Api(transport, service(credential.base_url), sign_in, **limits)


def apps_url(base_url: str, limit: int = PAGE_SIZE) -> str:
    return f"{base_url.rstrip('/')}{okta.APPS_PATH}?limit={limit}&includeNonDeleted=true"


def users_url(base_url: str, app_id: str, limit: int = USER_PAGE_SIZE) -> str:
    return f"{base_url.rstrip('/')}{okta.APPS_PATH}/{urllib.parse.quote(app_id, safe='')}/users?limit={limit}"


def count_users(api: rest_source.Api, base_url: str, app_id: str) -> tuple[int, bool]:
    """How many users are assigned to the app (directly or through a group), and whether the count stopped at
    USER_PAGES pages (so it is "at least")."""
    url = users_url(base_url, app_id)
    count = 0
    for _ in range(USER_PAGES):
        answer = api.get(url, "count an application's assigned users", READ_ONLY_ADMIN)
        if not isinstance(answer.body, list):
            raise SourceError("Okta answered without the expected list while counting an application's users.")
        count += len(answer.body)
        url = rest_source.next_link(answer.headers.get("link", ""))
        if not url:
            return count, False
    return count, True


def fetch(connector, credential, transport=None):
    """Every application (inactive ones too), and when the scope counts assignments, each kept app's user count."""
    api = client(connector, credential, transport)
    apps = api.get_link_pages(apps_url(credential.base_url), "list applications", READ_ONLY_ADMIN, PAGE_LIMIT)
    rest_source.guard_empty(connector, len(apps), "Okta's application listing")
    scope = scope_of(connector)
    notes: list[str] = []
    if scope["assignment_counts"]:
        counted = 0
        for app in apps:
            if skip_reason(map_object(app), scope) is not None:
                continue
            if api.time_left() < COUNT_MARGIN:
                notes.append(
                    f"Counting assigned users stopped after {counted} application(s): the run was near its time "
                    "limit. The apps are listed; the next run counts again."
                )
                break
            try:
                app[ASSIGNED] = count_users(api, credential.base_url, str(app["id"]))
                counted += 1
            except Refused as e:
                if e.status in (401, 403):
                    notes.append(
                        f"Assigned users weren't counted ({e.status} {e.code}".strip()
                        + "): the service app's admin role can't read application users. The apps are listed."
                    )
                    break
                notes.append(f"Assigned users couldn't be counted for one application ({e.status} {e.code}).")
            except SourceError as e:  # throttled out, unreachable, the deadline: the listing is kept
                notes.append(f"Counting assigned users stopped after {counted} application(s): {e}")
                break
    return Fetched(apps, notes)


# ---- mapping -------------------------------------------------------------------------------------------------------


def map_object(raw):
    app_id = str(raw.get("id") or "")[:50]
    name = str(raw.get("label") or raw.get("name") or f"Okta app {app_id}")
    status = str(raw.get("status") or "")[:20]
    features = {f for f in raw.get("features") or [] if isinstance(f, str)}
    assigned = raw.get(ASSIGNED)
    return {
        "kind": ObservationKind.APPLICATION,
        "external_id": app_id,
        "display_name": name[:400],
        "ai_agent": False,
        "data": {
            "okta_app_id": app_id,
            "app_name": str(raw.get("name") or "")[:100],
            "sign_on_mode": str(raw.get("signOnMode") or "")[:40],
            "status": status,
            "inactive": status != "ACTIVE",
            "provisioning": bool(features & PROVISIONING),
            "assigned_users": assigned[0] if assigned else None,
            "assigned_users_capped": bool(assigned and assigned[1]),
            "created": str(raw.get("created") or "")[:40],
            "last_updated": str(raw.get("lastUpdated") or "")[:40],
            "description": "",
            "enterprise_app": False,
        },
    }


def skip_reason(values, scope):
    scope = dict(SCOPE_DEFAULTS) | dict(scope or {})
    data = values["data"]
    if not data["okta_app_id"]:
        return "no_id"
    if scope["exclude_inactive"] and data["inactive"]:
        return "inactive"
    if scope["exclude_okta_apps"] and data["app_name"] in OKTA_APPS:
        return "okta_app"
    if scope["exclude_bookmarks"] and data["sign_on_mode"] == "BOOKMARK":
        return "bookmark"
    return "name_exclusions" if scopes.excluded_by_name(values["display_name"], scope) else None


def relevant(values):
    """What brings a dismissed candidate back: its name, how people sign in to it, or its becoming active again."""
    data = values["data"]
    return {"name": values["display_name"], "sign_on_mode": data["sign_on_mode"], "inactive": data["inactive"]}


def drift_values(observation, kind) -> dict[str, Any]:
    """Okta speaks only for the name on a linked record (discovery/drift.py)."""
    return {"name": observation.display_name}


# ---- the access check ------------------------------------------------------------------------------------------------


def check_steps(connector, scope, credential, transport=None) -> list[dict[str, Any]]:
    """Sign in to Okta, read one application, then (when the scope counts assignments) one of its users."""
    api = client(
        connector, credential, transport, budget_seconds=access.BUDGET_SECONDS, deadline=access.DEADLINE_SECONDS
    )
    steps = [rest_source.sign_in_step(api, OKTA_APPS_READ)]
    if steps[-1]["state"] != "ok":
        return steps + [access.skipped("Read applications", READ_ONLY_ADMIN)]
    step, body = rest_source.probe(api, "Read applications", apps_url(credential.base_url, 1), READ_ONLY_ADMIN)
    steps.append(step)
    if not scope.get("assignment_counts"):
        not_counting = access.step(
            "api",
            "Read application users",
            "not_used",
            permission=READ_ONLY_ADMIN,
            detail="Not checked: the connector's scope doesn't count assigned users (turn on assignment counts).",
        )
        return steps + [not_counting]
    first = next((a for a in body or [] if isinstance(a, dict) and a.get("id")), None)
    if first is None:
        why = "reading applications failed" if step["state"] != "ok" else "the org has no application to read"
        return steps + [access.skipped("Read application users", READ_ONLY_ADMIN, why)]
    users = users_url(credential.base_url, str(first["id"]), 1)
    return steps + [rest_source.probe(api, "Read application users", users, READ_ONLY_ADMIN)[0]]
