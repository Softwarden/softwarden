"""Walk every GET operation the served OpenAPI schema declares, against the seeded demo organization (seed_demo), as
its Admin (and the platform console's routes as a platform staff account). Path parameters are filled from the list
one level up (the first few rows' ids), so a detail or nested route is called with real records. A route the walk
can't reach (no row to fill its path, or it answers the plain call with a redirect or a refusal) must be in UNREACHABLE
with the reason, and both tests assert the walk's unreachable set is exactly that, so a new route nobody can reach
fails instead of being silently skipped. tests/test_api_contract.py validates each response against its declared schema;
tests/test_unknown_params.py checks that each refuses a parameter it doesn't take."""

from __future__ import annotations

import io
import json
import re
from typing import Any
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client

SCHEMA_URL = "/api/v1/schema"
PREFIX = "/api/v1/"


def seeded_admin() -> Client:
    """The demo organization on the enterprise edition (every capability's routes answer), signed in as its Admin.
    Callers turn the preview features on (the `preview_on` fixture) so those routes answer too."""
    from core.capabilities import set_edition
    from core.models import Organization

    call_command("seed_demo", stdout=io.StringIO())
    set_edition(Organization.objects.get(name="Contoso (synthetic)"), "enterprise")
    client = Client()
    client.force_login(get_user_model().objects.get(username="admin@example.test"))
    return client


PLATFORM = PREFIX + "platform/"


def platform_staff() -> Client:
    """A platform staff account with no membership, signed in with a session (the console's only way in)."""
    from core.models import PlatformStaff

    user = get_user_model().objects.create_user(username="staff@example.test", email="staff@example.test")
    PlatformStaff.objects.create(user=user)
    client = Client()
    client.force_login(user)
    return client


def seeded_walker() -> Walker:
    """The walk over the seeded demo organization, plus one attachment, saved filter and webhook (seed_demo makes
    none), so their detail routes are reached too."""
    walker = Walker(seeded_admin(), platform_staff())
    client = walker.client
    app = walker.candidates(PREFIX + "applications", "id")[0]
    made = client.post(f"{PREFIX}applications/{app}/attachments", {"file": SimpleUploadedFile("notes.txt", b"Notes")})
    assert made.status_code == 201, made.content
    walker.listed_at[PREFIX + "attachments"] = f"{PREFIX}applications/{app}/attachments"
    for path, body in (
        ("saved-filters", {"name": "Walked", "kind": "application", "query": {}}),
        (
            "webhooks",
            {
                "name": "Walked",
                "url": "https://hooks.example.test/in",
                "signing_secret": "walk-signing-secret-0123456789",
                "events": ["record.*"],
            },
        ),
    ):
        made = client.post(PREFIX + path, json.dumps(body), content_type="application/json")
        assert made.status_code == 201, made.content
    walker.listed_at[PREFIX + "saved-filters"] = PREFIX + "saved-filters?kind=application"
    walker._cache.clear()  # the lists read while finding the application are stale now
    return walker


# Every GET route the walk doesn't reach with a 2xx answer, and why. Keep it current: the tests fail when a route
# joins or leaves this set.
UNREACHABLE = {
    "/api/v1/admin/digests/{id}": "no digest: the worker builds them",
    "/api/v1/api-tokens/{id}": "no API token: seed_demo makes none",
    "/api/v1/applications/{id}/end-of-life": "404: no end of life tracked (the endoflife.date cache is empty offline)",
    "/api/v1/applications/{id}/instances/{instance_pk}/end-of-life": "404: no end of life tracked (cache empty offline)",
    "/api/v1/components/{id}/end-of-life": "404: no end of life tracked (the endoflife.date cache is empty offline)",
    "/api/v1/end-of-life/products/{name}": "no product: the endoflife.date cache is empty offline",
    "/api/v1/assessments/{id}/ticket-links/{link_pk}": "no ticket link: seed_demo makes none",
    "/api/v1/renewals/{id}/ticket-links/{link_pk}": "no ticket link: seed_demo makes none",
    "/api/v1/assistant/batches/{id}": "no assistant batch: one needs the model provider",
    "/api/v1/auth/oidc/callback": "302: a redirect back to the sign-in page without a pending sign-in",
    "/api/v1/auth/oidc/start": "302: a browser redirect",
    "/api/v1/calendar/renewals.ics": "404: needs a feed's secret token, shown once when the feed is created",
    "/api/v1/components/{id}/reviews/{review_pk}": "no component review: seed_demo reviews applications only",
    "/api/v1/connections/google_workspace/{id}": "no Google Workspace connection: seed_demo makes none",
    "/api/v1/connections/halo/{id}": "no Halo connection: seed_demo makes none",
    "/api/v1/connections/okta/{id}": "no Okta connection: seed_demo makes none",
    "/api/v1/discovery/observations/{id}/drift": "409: the first observation isn't linked to a record",
    "/api/v1/imports/{id}": "no import run: seed_demo makes none",
    "/api/v1/imports/{id}/errors.csv": "no import run: seed_demo makes none",
    "/api/v1/imports/{id}/rows": "no import run: seed_demo makes none",
    "/api/v1/invitations/{id}": "no pending invitation: seed_demo makes none",
    "/api/v1/needs-attention/snoozes/{id}": "no snooze: seed_demo makes none",
    "/api/v1/notification-log/{id}": "no delivery: the worker sends them",
    "/api/v1/notification-settings/teams-destinations/{id}": "no Teams destination: seed_demo makes none",
    "/api/v1/organization/exports/{id}": "no organization export: the worker builds them",
    "/api/v1/organization/exports/{id}/download": "no organization export: the worker builds them",
    "/api/v1/pending-access/{id}": "no pending access request: seed_demo makes none",
    "/api/v1/reminders/{id}": "no reminder: the worker regenerates them",
    "/api/v1/reports/licence-waste/{sku_id}": "no Entra license usage run: seed_demo makes none",
    "/api/v1/reports/licence-waste/{sku_id}/export.csv": "no Entra license usage run: seed_demo makes none",
    "/api/v1/saved-view-subscriptions/{id}": "no saved view subscription: seed_demo makes none",
    "/api/v1/ticket-connections/{id}": "no ticket connection: seed_demo makes none",
    "/api/v1/webhook-deliveries/{id}": "no webhook delivery: the worker sends them",
}


# A parent path whose IDs come from another list (the observations are the candidate queue's rows).
LISTED_AT = {
    "/api/v1/discovery/observations": "/api/v1/discovery/candidates?status=all",
}

# Values for a query parameter an operation requires, so the call reaches the view rather than its 400.
REQUIRED_VALUES = {
    "q": "pay",
    "kind": "application",
    "url": "https://example.atlassian.net/browse/IT-1",
    "domain": "example.com",  # platform/sign-in-domains
}


def body_of(response) -> Any:
    if getattr(response, "streaming", False) or not response.content:
        return None
    if "json" not in response.get("Content-Type", ""):
        return None
    try:
        return json.loads(response.content)
    except ValueError:
        return None


def rows_of(body: Any) -> list[Any]:
    """The rows of a list response: a plain array, a page's results, or the first list of records in an object."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("results", "items", "rows", "data"):
            if isinstance(body.get(key), list):
                return body[key]
        for value in body.values():
            if isinstance(value, list) and value and isinstance(value[0], dict) and "id" in value[0]:
                return value
    return []


class Walker:
    def __init__(self, client: Client, platform: Client | None = None):
        self.client = client
        self.platform = platform or client
        self.schema = json.loads(client.get(SCHEMA_URL, {"format": "json"}).content)
        self.gets = {
            path: item["get"] for path, item in self.schema["paths"].items() if "get" in item and path != SCHEMA_URL
        }
        self._cache: dict[str, Any] = {}
        self.listed_at = dict(LISTED_AT)

    def fetch(self, path: str):
        if path not in self._cache:
            self._cache[path] = self.client_for(path).get(path)
        return self._cache[path]

    def client_for(self, path: str) -> Client:
        return self.platform if path.startswith(PLATFORM) else self.client

    def candidates(self, prefix: str, param: str) -> list[str]:
        response = self.fetch(self.listed_at.get(prefix, prefix))
        if response.status_code != 200:
            return []
        found = []
        for row in rows_of(body_of(response)):
            if isinstance(row, dict):
                if str(row.get("id", "")).startswith("builtin:"):  # a built-in saved view has no detail route
                    continue
                for key in (param, "id", "key", "slug", "sku_id", "name"):
                    if row.get(key) not in (None, ""):
                        found.append(str(row[key]))
                        break
            elif isinstance(row, str):
                found.append(row)
        return found[:6]

    def concrete_paths(self, template: str, limit: int = 3) -> list[str]:
        """Up to `limit` concrete paths for a template, depth first through its path parameters."""
        parts = template.split("/")
        done: list[str] = []

        def walk(i: int, built: list[str]) -> None:
            if len(done) >= limit:
                return
            if i == len(parts):
                done.append("/".join(built))
                return
            match = re.fullmatch(r"\{(\w+)\}", parts[i])
            if not match:
                walk(i + 1, [*built, parts[i]])
                return
            for value in self.candidates("/".join(built), match.group(1)):
                walk(i + 1, [*built, value])
                if len(done) >= limit:
                    return

        walk(0, [])
        return done

    def required_query(self, operation: dict[str, Any]) -> dict[str, str]:
        """Plausible values for the query parameters the operation requires (`target`: an application)."""
        found = {}
        for param in operation.get("parameters", []):
            if param.get("in") == "query" and param.get("required"):
                name = param["name"]
                if name == "target":
                    found[name] = (self.candidates(PREFIX + "applications", "id") or [""])[0]
                else:
                    found[name] = REQUIRED_VALUES.get(name, "x")
        return found

    def operations(self, limit: int = 3):
        """(template, operation, [concrete URLs]) for every GET, each URL with the query parameters the operation
        requires; the list is empty when no path could be filled."""
        for template in sorted(self.gets):
            operation = self.gets[template]
            paths = [template] if "{" not in template else self.concrete_paths(template, limit)
            query = urlencode(self.required_query(operation))
            yield template, operation, [f"{path}?{query}" if query else path for path in paths]

    def unreached(self) -> set[str]:
        """The templates the walk can't call with a 2xx answer: no row fills the path, or the first URL answers a
        redirect, a refusal or a not-found."""
        return {
            template
            for template, _operation, urls in self.operations(limit=1)
            if not urls or not 200 <= self.fetch(urls[0]).status_code < 300
        }
