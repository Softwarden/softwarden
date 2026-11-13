"""Every route, every method, with another organization's IDs: the systematic half of the cross-tenant suite.

tests/test_tenancy.py holds hand-written cross-organization cases and the database checks; this walks the URL resolver
instead, so a new viewset that forgets its organization filter fails here whether or not its author wrote a
cross-organization test.

Org B is the seeded demo organization (seed_demo) plus a row of each kind seed_demo doesn't make (through the API as
B's Admin, or the service or job that makes them); the walk test first checks that is true for every tenant model of
every local app (core.local_apps), so a new model fails it until extras() makes one. Org A is a second seed_demo run
with accounts of its own.

Signed in as A's Admin, for every route the resolver has with a path parameter and every method it answers (public,
platform-console and SCIM-token routes are left out: they act in no organization by design and have their own tests):

1. a control request with ids that name nothing, whose SQL is captured: the tables each parameter's value was looked
   up in;
2. for each parameter in turn, the same request with each of B's rows of those tables in that parameter (one per
   combination of kind, source, status and the like, so a route that reads only one kind of row meets it); a parameter
   no query read gets every one of B's ids;
3. for a nested route, the same with a valid parent of A's and each of B's rows as the child.

Each answer must equal the control's (status and body, the ids blanked): any difference is a leak or an existence
oracle. Reading the tables from the SQL keeps the walk to a few thousand requests; the blind version, every B id of
every kind in every parameter (about 37,000 requests, six minutes), found nothing more when it was run once.
What remains sampled: B offers one row per combination of DISCRIMINATORS, so a lookup that also filters on another
field (archived_at, active, lifecycle) could miss the one row offered and answer 404 to both. POST, PUT and PATCH carry
an empty JSON body, so a view that validates its body before it looks its record up wouldn't be tested past its 400:
the walk fails when a control answers anything but 200 or 404, unless OTHER_CONTROLS names the route with its reason.
Bodies are compared with the ids blanked as whole tokens (a small integer id doesn't blank the 4s of a 404)."""

from __future__ import annotations

import io
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import get_resolver
from django.utils import timezone

from catalog import platform_admin, usage
from catalog.models import Resource
from core import capabilities as caps
from core import halo
from core.dates import org_today
from core.drf import PLATFORM, PUBLIC, declared_policy
from core.local_apps import local_models
from core.models import Membership, Organization, Role
from notify import reminders
from tests.api_walk import body_of, rows_of
from tests.conftest import digest_row, drain_jobs
from tests.test_okta_google import google_connection, okta_connection
from tests.test_url_coverage import methods_and_actions, walk

pytestmark = pytest.mark.usefixtures("preview_on")

PREFIX = "/api/v1/"
DEMO = "Contoso (synthetic)"
B_NAME = "Org B (synthetic)"
PARAM = re.compile(r"<(?:(\w+):)?(\w+)>")


def is_tenant(model) -> bool:
    return any(f.name == "organization" for f in model._meta.concrete_fields)


@dataclass
class World:
    a: Organization
    b: Organization
    a_admin: Any


def json_client(user, **options: Any) -> Client:
    client = Client(**options)
    client.force_login(user)
    return client


def send(client: Client, method: str, path: str, body: Any = None):
    if method in ("post", "put", "patch"):
        return getattr(client, method)(
            path, json.dumps(body if body is not None else {}), content_type="application/json"
        )
    return getattr(client, method)(path)


def tenant_rows(org) -> dict[str, list[str]]:
    """{model label: up to one of its primary keys in the organization} for every local tenant model."""
    found = {}
    for model in local_models():
        if is_tenant(model):
            pk = model._meta.pk.attname
            found[model._meta.label] = [
                str(v) for v in model._base_manager.filter(organization=org).values_list(pk, flat=True)[:1]
            ]
    return found


def soon(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


# ---- seeding -------------------------------------------------------------------------------------------------------


def seed_demo() -> tuple[Organization, Any]:
    call_command("seed_demo", stdout=io.StringIO())
    org = Organization.objects.get(name=DEMO)
    org, _ = caps.set_edition(org, "enterprise")
    admin = Membership.objects.filter(organization=org, role=Role.ADMIN).select_related("user")[0].user
    return org, admin


def make_b() -> tuple[Organization, Any]:
    """The demo organization renamed, and its accounts renamed out of the way, so a second seed_demo makes org A with
    accounts of its own (never members of B)."""
    org, admin = seed_demo()
    Organization.objects.filter(pk=org.pk).update(name=B_NAME)
    org.refresh_from_db()
    for user in get_user_model().objects.filter(membership__organization=org):
        user.username = user.email = "b-" + user.username
        user.save(update_fields=["username", "email"])
    admin.refresh_from_db()
    return org, admin


class Seeder:
    """The organization's Admin making rows through the API; a refusal fails the test, naming the call."""

    def __init__(self, admin):
        self.client = json_client(admin)

    def call(self, method: str, path: str, body: Any = None) -> Any:
        response = send(self.client, method, PREFIX + path, body)
        assert response.status_code in (200, 201, 202, 204), (method, path, response.content[:400])
        return body_of(response)

    def upload(self, path: str, data: dict[str, Any]) -> Any:
        response = self.client.post(PREFIX + path, data)
        assert response.status_code == 201, (path, response.content[:400])
        return body_of(response)

    def rows(self, path: str) -> list[Any]:
        return rows_of(body_of(self.client.get(PREFIX + path)))


def extras(org, admin) -> None:
    """One row of each kind seed_demo makes none of: through the API as the organization's Admin where a route makes
    it (connections and connectors on the simulated hosts, which answer in-process), else through the service or the
    job that does."""
    api = Seeder(admin)
    app = api.rows("applications")[0]["id"]
    person = api.rows("people")[0]["id"]
    member = api.rows("users")[-1]["id"]
    api.upload(f"applications/{app}/attachments", {"file": SimpleUploadedFile("notes.txt", b"Notes")})
    api.call("post", "api-tokens", {"user": member, "name": "Walk token", "read_only": True, "expires_on": soon(60)})
    api.call("post", "invitations", {"email": f"invitee-{org.pk.hex[:6]}@example.test", "role": "viewer"})
    saved = api.call("post", "saved-filters", {"name": "Walked", "kind": "application", "query": {}, "shared": True})
    api.call("post", "saved-view-subscriptions", {"saved_view": saved["id"], "weekday": 1})
    hook = {"url": "https://hooks.example.test/in", "signing_secret": "walk-signing-secret-0123456789"}
    api.call("post", "webhooks", {"name": "Walked", **hook, "events": ["record.*"]})
    teams = api.call(
        "post",
        "notification-settings/teams-destinations",
        {"name": "Walk Teams", "url": "https://prod-01.westeurope.logic.azure.com/workflows/abc/triggers/manual"},
    )
    destination = api.call("post", "notification-settings/webhook-destinations", {"name": "Walk hook", **hook})
    rule = {
        "name": "Walk rule",
        "source_types": ["contract"],
        "lead_times_days": [30],
        "people": [person],
        "escalation_people": [person],
        "teams_destinations": [teams["id"]],
        "webhook_destinations": [destination["id"]],
    }
    api.call("post", "notification-rules", rule)
    api.call("post", "calendar-feed")
    api.call("patch", "auth/me/preferences", {"digest_enabled": False})
    smtp = {"transport": "smtp", "smtp_host": "smtp.example.test", "from_address": "it@example.test"}
    api.call("patch", "notification-settings/email", smtp)
    api.call("patch", "assistant/settings", {"enabled": True, "api_key": "sk-ant-walk-key-0000000000000"})
    sso = {
        "provider": "oidc",
        "discovery_url": "https://idp.example.test/.well-known/openid-configuration",
        "client_id": "walk-client",
        "client_secret": "walk-sso-client-secret",
        "sign_in_domains": [f"walk-{org.pk.hex[:8]}.example.test"],
    }
    api.call("patch", "sso/settings", sso)
    api.call("post", "organization/exports")
    jira = {"base_url": "https://jira.example.test", "username": "bot@example.test", "secret": "walk-jira-secret"}
    api.call("post", "ticket-connections", {"system": "jira", **jira})
    for parent in ("assessments", "renewals"):
        api.call(
            "post", f"{parent}/{api.rows(parent)[0]['id']}/ticket-links", {"url": f"{jira['base_url']}/browse/IT-1"}
        )
    api.call("patch", f"applications/{app}/end-of-life", {"product": "postgresql", "version": "16.4"})
    attention = body_of(api.client.get(PREFIX + "needs-attention?scope=all"))
    queue, items = next((q, i) for q, i in attention.items() if isinstance(i, list) and i and i[0].get("id"))
    snooze = {"subject_type": queue, "subject_id": str(items[0]["id"]), "kind": "snooze", "until": soon(30)}
    api.call("post", "needs-attention/snoozes", snooze)
    csv = SimpleUploadedFile("walk.csv", b"name,lifecycle\nWalk Imported,active\n", content_type="text/csv")
    api.upload("imports", {"file": csv, "kind": "application"})
    halo_data = {"name": "Walk Halo", "base_url": halo.SIMULATED_URL, "client_id": "sw", "secret": "walk-halo-secret"}
    api.call("post", "connections/halo", halo_data)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    okta_connection(org, admin, pem, test=False)
    google_connection(org, admin, pem, test=False)
    microsoft = api.rows("connections/microsoft")[0]["id"]
    for source in ("entra_licenses", "entra_shadow_it", "entra_coverage", "entra_licence_usage"):
        data = {"source": source, "name": f"Walk {source}", "microsoft_connection": microsoft, "schedule": "manual"}
        api.call("post", f"connectors/{api.call('post', 'connectors', data)['id']}/run")
    # A platform operator's override and limit for the organization, its usage snapshot, reminders and a digest.
    staff = get_user_model().objects.create_user(username=f"staff-{org.pk.hex[:6]}@example.test")
    platform_admin.grant_staff(staff)
    platform_admin.set_override(staff, org, {"capability": "ai_agents", "effect": "grant", "reason": "Walk"})
    platform_admin.set_limit(staff, org, {"limit": "records", "value": 100000, "reason": "Walk"})
    usage.refresh(org.pk)
    reminders.generate(org, org_today(org))
    digest_row(org, admin)
    drain_jobs()  # the connector runs, the export, the assistant batch the settings queued (refused offline)


def internal_rows(org) -> None:
    """Rows only a sign-in, a sweep, a sync, the model provider or the delivery worker writes, made directly (as their
    own tests make them): no route creates them, but the walk still offers their ids to every route."""
    from catalog.models import RetiredFile
    from core.models import InstallationEmail, PendingAccess, SamlAssertion
    from discovery.models import AssistantSuggestion, AssistantUsage, Observation
    from imports.models import ExternalIdentifier, ImportSource
    from money.models import LicensePool, RenewalCycle, SeatCountReview
    from notify.models import Delivery, DeliveryAttempt, ReminderDispatch, WebhookEvent

    now = timezone.now()
    RetiredFile.objects.create(organization=org, name="walk-retired.txt", retired_at=now)
    PendingAccess.objects.create(
        organization=org, issuer="https://idp.example.test", subject="walk", email="pending@example.test",
        first_seen=now, last_seen=now,
    )  # fmt: skip
    SamlAssertion.objects.create(
        organization=org, issuer="https://idp.example.test", assertion_id="walk", expires_at=now + timedelta(hours=1)
    )
    observation = Observation.objects.filter(organization=org)[0]
    used = AssistantUsage.objects.create(organization=org, model="claude-walk", outcome="succeeded")
    AssistantSuggestion.objects.create(
        organization=org, observation=observation, fingerprint="walk", model="claude-walk", state="failed", usage=used
    )
    ExternalIdentifier.objects.create(
        organization=org,
        source=ImportSource.objects.filter(organization=org)[0],
        external_key="WALK-1",
        resource=Resource.objects.filter(organization=org)[0],
    )
    pool = LicensePool.objects.filter(organization=org)[0]
    SeatCountReview.objects.create(
        organization=org, pool=pool, observation=observation, decision="dismissed", fingerprint="walk"
    )
    delivery = Delivery.objects.filter(organization=org)[0]
    DeliveryAttempt.objects.create(organization=org, delivery=delivery, number=1, started_at=now, outcome="sent")
    WebhookEvent.objects.create(organization=org, event_type="record.created", content={})
    cycle = RenewalCycle.objects.filter(organization=org)[0]
    ReminderDispatch.objects.create(organization=org, cycle=cycle, threshold_days=30)
    InstallationEmail.objects.create(organization=org, kind="invitation")  # the send budget's ledger


def seed_pair() -> World:
    b, b_admin = make_b()
    extras(b, b_admin)
    internal_rows(b)
    a, a_admin = seed_demo()
    return World(a=a, b=b, a_admin=a_admin)


# ---- the walk ------------------------------------------------------------------------------------------------------

# A parameter's value in the control request: names nothing (a string parameter mostly carries an id, so a UUID too).
NOBODY = "6f1d2c3b-0000-4000-8000-00000000f27a"
RANDOM = {"uuid": NOBODY, "int": "987654321", "str": NOBODY, "slug": "walk-none"}


@dataclass
class Route:
    template: str  # the resolver's route, e.g. api/v1/applications/<uuid:pk>/instances/<uuid:instance_pk>
    method: str
    params: list[tuple[str, str]]  # (converter, name)

    def fill(self, values: list[str]) -> str:
        it = iter(values)
        return "/" + PARAM.sub(lambda _m: next(it), self.template)

    def __str__(self) -> str:
        return f"{self.method.upper()} /{self.template}"


def org_routes() -> list[Route]:
    """Every (route, method) with a path parameter that acts in the signed-in organization: not public (sign-in, SAML,
    the SCIM service's own bearer token, the SPA) and not the platform console (installation operators)."""
    found = []
    for template, callback in walk(get_resolver().url_patterns):
        params = [(conv or "str", name) for conv, name in PARAM.findall(template)]
        policy = declared_policy(callback) if getattr(callback, "cls", None) is not None else None
        if not params or policy is None:
            continue
        for method, action in methods_and_actions(callback):
            if policy.role_for(method, action) not in (PUBLIC, PLATFORM):
                found.append(Route(template, method, params))
    return found


# A model's rows differ in kind (an application or a component, a licence or an app observation, a Jira or a Halo
# connection) and a route may look up only one kind, so the walk offers one row of each combination of these fields.
DISCRIMINATORS = {"kind", "source", "system", "status", "channel", "state", "provider", "change_type", "decision"}
PER_MODEL = 12


def rows_by_table(org, members: list[int]) -> dict[str, list[str]]:
    """{database table: values} the walk offers for a path parameter that the route looked up in that table: for each
    tenant model of the organization, one primary key of each combination of its DISCRIMINATORS (up to PER_MODEL);
    the organization's members' user ids; the organization's own id and slug."""
    found: dict[str, list[str]] = {}
    for model in local_models():
        if not is_tenant(model):
            continue
        pk = model._meta.pk.attname
        fields = [f.attname for f in model._meta.concrete_fields if f.name in DISCRIMINATORS]
        seen: set[tuple[Any, ...]] = set()
        keys: list[str] = []
        for row in model._base_manager.filter(organization=org).order_by(pk).values(pk, *fields)[:2000]:
            combo = tuple(row[f] for f in fields)
            if combo not in seen and len(keys) < PER_MODEL:
                seen.add(combo)
                keys.append(str(row[pk]))
        found.setdefault(model._meta.db_table, []).extend(keys)
    found[get_user_model()._meta.db_table] = [str(m) for m in members]
    found[Organization._meta.db_table] = [str(org.pk), str(org.slug)]
    return found


def fits(converter: str, value: str) -> bool:
    if converter == "int":
        return value.isdigit()
    if converter == "uuid":
        try:
            uuid.UUID(value)
        except ValueError:
            return False
    return True


VOLATILE = re.compile(r'"(request_id|instance|trace_id)":\s*"[^"]*",?\s*')
QUOTED = re.compile(r'"(\w+)"')


def shape(response, sent: list[str]) -> tuple[int, str]:
    """What two answers are compared on: the status, and the body with the ids sent blanked out."""
    if getattr(response, "streaming", False):
        return response.status_code, "<stream>"
    text = response.content.decode("utf-8", "replace")
    for value in sorted(set(sent), key=len, reverse=True):
        # Whole tokens only: a small integer id ("4") must not blank the 4s of `"status": 404`.
        text = re.sub(rf"(?<![\w-]){re.escape(value)}(?![\w-])", "<ID>", text)
    return response.status_code, VOLATILE.sub("", text)


@dataclass
class Control:
    """The answer to a request whose parameters name nothing of theirs, and for each parameter the tables the request
    looked its value up in (from the SQL it ran)."""

    answer: tuple[int, str]
    tables: list[set[str]]


class Walk:
    def __init__(self, client: Client, theirs: dict[str, list[str]], ours: dict[str, list[str]]):
        self.client = client
        self.theirs = theirs
        self.ours = ours
        self.requests = 0
        self.probed = 0  # parameters the walk sent their ids in
        self.unread: list[str] = []  # parameters no query read (the request ended before, or the value isn't an id)
        self.controls: dict[str, int] = {}  # a control answering neither 200 nor 404: its route and status
        self.leaks: list[str] = []
        self._parents: dict[str, str | None] = {}

    def call(self, route: Route, values: list[str]):
        self.requests += 1
        return send(self.client, route.method, route.fill(values))

    def control(self, route: Route, values: list[str]) -> Control:
        with CaptureQueriesContext(connection) as queries:
            answer = shape(self.call(route, values), values)
        sql = [q["sql"] for q in queries.captured_queries]

        def read(value: str, text: str) -> bool:  # the SQL Django logs writes a UUID as 32 hex digits
            return value in text or value.replace("-", "") in text

        tables = [
            {t for text in sql if read(value, text) for t in QUOTED.findall(text) if t in self.theirs}
            for value in values
        ]
        return Control(answer, tables)

    def ours_for(self, converter: str) -> list[str]:
        return [v for values in self.ours.values() for v in values if fits(converter, v)]

    def parent(self, route: Route, routes: list[Route]) -> str | None:
        """A valid id of ours for the route's first parameter: one the shortest GET route through it answers 200."""
        head = route.template[: route.template.index(">") + 1]
        if head not in self._parents:
            self._parents[head] = None
            probes = sorted(
                (r for r in routes if r.method == "get" and r.template.startswith(head) and len(r.params) == 1),
                key=lambda r: len(r.template),
            )
            if probes:
                probe = probes[0]
                for value in self.ours_for(probe.params[0][0]):
                    self.requests += 1
                    if send(self.client, "get", probe.fill([value])).status_code == 200:
                        self._parents[head] = value
                        break
        return self._parents[head]

    def probe(self, route: Route, control: list[str], positions: range) -> None:
        """Each of their values that fits a parameter and belongs to a table the control looked that parameter up in,
        in that parameter, the rest as in the control; each answer must be the control's."""
        found = self.control(route, control)
        if found.answer[0] not in (200, 404):
            self.controls[f"{route}{' (our parent)' if positions.start else ''}"] = found.answer[0]
        for i in positions:
            converter = route.params[i][0]
            values = {v for table in found.tables[i] for v in self.theirs[table] if fits(converter, v)}
            if not values:  # no query read it: send every value of theirs that fits, as a blind walk would
                self.unread.append(f"{route} {route.params[i][1]}")
                values = {v for rows in self.theirs.values() for v in rows if fits(converter, v)}
            self.probed += 1
            for value in sorted(values):
                case = [*control[:i], value, *control[i + 1 :]]
                got = shape(self.call(route, case), case)
                if got != found.answer:
                    self.leaks.append(
                        f"{route} with their {value} as {route.params[i][1]}: {got[0]} {got[1][:160]!r}; "
                        f"the control answers {found.answer[0]}"
                    )

    def check(self, route: Route, routes: list[Route]) -> None:
        randoms = [RANDOM[conv] for conv, _ in route.params]
        self.probe(route, randoms, range(len(randoms)))
        if len(route.params) > 1 and (parent := self.parent(route, routes)) is not None:
            self.probe(route, [parent, *randoms[1:]], range(len(randoms) - 1, len(randoms)))


# Controls that may answer something other than 200 or 404, and why the walk still reaches the lookup behind them:
# {"POST /api/v1/<route>": (status, reason)}; a route's control through our own parent ends in " (our parent)".
# None does today: every control either found nothing (404) or answered without reading an id (200).
OTHER_CONTROLS: dict[str, tuple[int, str]] = {}


@pytest.fixture
def world(db, settings):
    settings.API_READ_RATE = settings.API_WRITE_RATE = ""  # the walk is thousands of requests a minute
    return seed_pair()


def test_shape_blanks_whole_ids_only():
    class Answer:
        status_code = 404
        content = b'{"type": "/problems/not-found", "status": 404, "detail": "No 4 here", "id": "4"}'

    assert shape(Answer(), ["4"]) == (
        404,
        '{"type": "/problems/not-found", "status": 404, "detail": "No <ID> here", "id": "<ID>"}',
    )
    Answer.content = f'{{"id": "{NOBODY}", "parent": "{NOBODY}-x"}}'.encode()
    assert shape(Answer(), [NOBODY])[1] == f'{{"id": "<ID>", "parent": "{NOBODY}-x"}}'


def test_no_route_answers_another_organizations_ids_differently(world, monkeypatch):
    from rest_framework.views import APIView

    # The walk offers B's ids of every kind: a new tenant model gets a row in extras() or internal_rows().
    # The SCIM rows (kept tables, forward-only migrations) are written only by the optional `ee` package.
    missing = [label for label, found in tenant_rows(world.b).items() if not found]
    assert sorted(m for m in missing if not m.startswith("core.Scim")) == []

    monkeypatch.setattr(APIView, "check_throttles", lambda self, request: None)  # upload, search and sign-in scopes
    users = get_user_model().objects
    walker = Walk(
        json_client(world.a_admin, raise_request_exception=False),  # a 500 is an answer to compare, not a crash
        rows_by_table(world.b, list(users.filter(membership__organization=world.b).values_list("id", flat=True))),
        rows_by_table(world.a, list(users.filter(membership__organization=world.a).values_list("id", flat=True))),
    )
    routes = org_routes()
    started = time.monotonic()
    for route in routes:
        walker.check(route, routes)
    print(
        f"walked {len(routes)} method-routes in {time.monotonic() - started:.0f} s: {walker.requests} requests, "
        f"their ids sent in {walker.probed} parameters; {len(walker.unread)} parameters never read: {walker.unread}"
    )
    # The walk saw the API, not an empty resolver, and most parameters were looked up in a table it has rows of.
    assert len(routes) > 250 and walker.probed > 250
    assert walker.leaks == [], "\n".join(walker.leaks[:40])
    # A control that answers neither 200 nor 404 may have stopped before its lookup (a body validated first, a
    # conflict, a refusal), which would hide a leak behind the same answer: each is named with the reason it is safe.
    assert walker.controls == {route: status for route, (status, _why) in OTHER_CONTROLS.items()}
    # Still A's Admin at the end: a route that signed the walk out would have made every later control and case the
    # same refusal, and the walk would have passed without testing them.
    me = walker.client.get("/api/v1/auth/me")
    assert me.status_code == 200 and json.loads(me.content)["organization"]["id"] == str(world.a.pk)
