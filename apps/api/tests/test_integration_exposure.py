"""No stored secret and no issued token leaves the server through any output surface (acceptance check: "No secret or
token appears in any log line, audit snapshot, API response or export"), across this repository's sources and runs.

Known values are planted everywhere a secret lives: every connection type (Microsoft, Halo) and every channel secret
(Graph mail, Teams and webhook URLs, the webhook signing secret), and every simulator issues a planted token (Graph,
ARM, Dataverse, the Power Platform admin API, Halo, Graph mail). Then every source runs (Entra, Entra licences, Azure,
Power Platform with listed environments and all environments, Halo) through Run now and the schedule as jobs, the access
checks run, runs fail with the simulators echoing the secret (token endpoints) and the bearer token (reads), a run is
failed as stuck, and the reconciliation actions run. The planted values must appear in none of: log records (every
logger, message and attributes), audit events and their snapshots, any API GET response for every role (the URL list
swept, exports included) and its headers, CSV and JSON-lines exports, job payloads, results and errors, schedule
summaries, reminders, the notification log, observations, discovered credentials, runs and access checks.

Not covered, because this repository has no such surface: the configuration models' generic routes (there is
no generic object route here; connection routes are covered by the sweep), managed SSO falling back to .env, and the
.env rollback command."""

import json
import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.urls import get_resolver
from django.utils import timezone

from core import credentials, drf, graph, graph_simulator, halo, jobs, worker
from core.capabilities import set_edition
from core.dates import org_today
from core.models import AuditEvent, HaloConnection, Job, MicrosoftConnection, Schedule
from discovery import assistant, entra_simulator, simulator_base
from discovery import engine as discovery
from discovery import powerplatform_simulator as pp_sim
from discovery.models import (
    AssistantBatch,
    AssistantSuggestion,
    AssistantUsage,
    Connector,
    ConnectorRun,
    DiscoveredCredential,
    Observation,
    RunStatus,
)
from notify import reminders, rules
from notify.models import (
    Delivery,
    DeliveryAttempt,
    EmailSettings,
    Reminder,
    TeamsDestination,
    WebhookDestination,
    WebhookEvent,
)
from tests.conftest import TEST_CREDENTIAL_KEY, drain_jobs, no_digest
from tests.test_drf_resources import KEYED_COMPOSITES, PARAM, fill, keyed_composite_reads, requests_for, walk

SECRETS = {
    "entra": "exposure-entra-secret-Q7v~1001",
    "entra_replaced": "exposure-entra-replaced-Q7v~1002",
    "azure": "exposure-azure-secret-Lp4~1003",
    "halo": "exposure-halo-secret-Hx2-1004",
    "mail": "exposure-mail-secret-Mg5~1005",
    "teams": "https://prod-01.westus.logic.azure.example/workflows/x/invoke?sig=exposure-teams-sig-1006",
    "webhook": "https://hooks.example.test/softwarden?token=exposure-webhook-bearer-1007",
    "signing": "exposure-signing-secret-0123456789-1008",
    "assistant": "sk-ant-exposure-assistant-key-1009",  # the assistant's Anthropic API key
    "signing_rotated": "exposure-rotated-signing-secret-1010",  # a webhook's rotated signing secret
}
FRAGMENTS = ("exposure-teams-sig-1006", "exposure-webhook-bearer-1007")  # the secret parts of the URLs
TOKENS = {
    "graph": "exposure-graph-token-2001",
    "arm": "exposure-arm-token-2002",
    "bap": "exposure-bap-token-2003",
    "dataverse": "exposure-dataverse-token-2004-",
    "halo": "exposure-halo-token-2005",
    "mail": "exposure-mail-token-2006",
}
URLS = [f"https://{pp_sim.CONTOSO}", f"https://{pp_sim.CONTOSO_DEV}"]
ROLES = ("admin", "editor", "viewer")


@pytest.fixture
def planted(monkeypatch):
    """Every simulator issues a planted token instead of its fixed one."""
    monkeypatch.setattr(entra_simulator, "TOKEN", TOKENS["graph"])
    monkeypatch.setattr(simulator_base, "ARM_TOKEN", TOKENS["arm"])
    monkeypatch.setattr(simulator_base, "BAP_TOKEN", TOKENS["bap"])
    monkeypatch.setattr(simulator_base, "DATAVERSE_TOKEN", TOKENS["dataverse"])
    monkeypatch.setattr(halo, "TOKEN", TOKENS["halo"])
    monkeypatch.setattr(graph_simulator, "TOKEN", TOKENS["mail"])


@pytest.fixture
def org(a):
    a["org"], _ = set_edition(a["org"], "enterprise")  # Azure, Power Platform, Halo and the JSON-lines export
    return a


def ciphertexts() -> set[str]:
    """Every stored ciphertext now (its first 40 characters are enough to recognise it)."""
    found = set()
    for model, prefixes in credentials.slot_models():
        for row in model.objects.all():
            for prefix in prefixes:
                value = getattr(row, f"{prefix}_ciphertext", "")
                if value:
                    found.add(value[:40])
    return found


def planted_values(extra=()) -> list[str]:
    return [*SECRETS.values(), *FRAGMENTS, *TOKENS.values(), TEST_CREDENTIAL_KEY, *extra]


def assert_clean(where: str, text: str, values) -> None:
    for value in values:
        assert value not in text, f"{value[:24]}... found in {where}"


class Responses:
    """Keeps every response's body and headers."""

    def __init__(self):
        self.texts: list[str] = []

    def keep(self, response):
        body = b"".join(response.streaming_content) if response.streaming else response.content
        self.texts.append(body.decode(errors="replace"))
        self.texts.append(json.dumps(dict(response.items())))
        return response


def get_paths(f, user) -> list[str]:
    """Every GET route of the API: the viewsets' (IDs from objects made in f's organization), the keyed composites'
    and every route without an ID."""
    paths = {path for method, path, _, _ in requests_for(f, user) if method == "get"}
    for route, cls, kind in keyed_composite_reads():
        paths.add(fill(route, KEYED_COMPOSITES[cls.__name__, kind](f, user)))
    for route, callback in walk(get_resolver().url_patterns):
        cls = getattr(callback, "cls", None)
        if not route.startswith("api/") or PARAM.findall(route) or cls is None:
            continue
        if issubclass(cls, drf.TenantViewSet):
            continue  # done above
        if hasattr(cls, "get") or "get" in (getattr(callback, "actions", None) or {}):
            paths.add("/" + route)
    return sorted(paths)


def make_connectors(admin, f):
    """Connectors for every source, through the API, on planted connections."""
    ms = admin.post("connections/microsoft", {"name": "Entra", "tenant_id": "simulated", "secret": SECRETS["entra"]})
    az = admin.post("connections/microsoft", {"name": "Azure", "tenant_id": "simulated", "secret": SECRETS["azure"]})
    hl = admin.post(
        "connections/halo",
        {"name": "Halo", "base_url": halo.SIMULATED_URL, "client_id": "softwarden", "secret": SECRETS["halo"]},
    )
    for r in (ms, az, hl):
        assert r.status_code == 201, r.json_body
    for kind, r in (("microsoft", ms), ("microsoft", az), ("halo", hl)):
        assert admin.post(f"connections/{kind}/{r.json_body['id']}/test").json_body["test_current"]
    specs = [
        ("entra", "Entra (exposure)", ms, {"include_agent_identities": True}),
        ("entra_licenses", "Licences (exposure)", ms, None),
        ("azure", "Azure (exposure)", az, None),
        ("powerplatform", "Power Platform (exposure)", az, {"environment_urls": URLS, "include_cloud_flows": True}),
        ("powerplatform", "All environments (exposure)", az, {"all_environments": True}),
        ("halo", "Halo (exposure)", hl, None),
    ]
    made = {}
    for source, name, conn, scope in specs:
        kind = "halo" if source == "halo" else "microsoft"
        body = {"source": source, "name": name, f"{kind}_connection": conn.json_body["id"]}
        if scope:
            body["scope"] = scope
        r = admin.post("connectors", body)
        assert r.status_code == 201, (source, r.json_body)
        made[name] = Connector.objects.get(pk=r.json_body["id"])
    return made, {"ms": ms.json_body["id"], "az": az.json_body["id"], "hl": hl.json_body["id"]}


def configure(connector, **config):
    connector.config = {**connector.config, **config}
    connector.save(update_fields=["config"])


class FakeAnthropic:
    """The assistant's client (discovery/assistant.make_client) without the network: counts, then answers every
    candidate with create. Keeps the keys it was built with, so the sweep knows the planted key was used."""

    keys: list[str] = []

    def __init__(self, key):
        FakeAnthropic.keys.append(key)
        self.messages = self

    def with_options(self, **options):
        return self

    def count_tokens(self, **kwargs):
        return SimpleNamespace(input_tokens=500)

    def create(self, **kwargs):
        rows = json.loads(kwargs["messages"][0]["content"].split("\n", 1)[1])["candidates"]
        text = json.dumps(
            {
                "suggestions": [
                    {"candidate": c["candidate"], "action": "create", "record": "", "confidence": 0.5, "reason": "x"}
                    for c in rows
                ]
            }
        )
        usage = SimpleNamespace(
            input_tokens=500, output_tokens=50, cache_read_input_tokens=0, cache_creation_input_tokens=0
        )
        return SimpleNamespace(
            stop_reason="end_turn", stop_details=None, content=[SimpleNamespace(type="text", text=text)], usage=usage
        )


def webhook_echo(posted: list):
    """channels.post_https for the sweep (webhooks): the real address check, then an answer that echoes the
    address, the signature header and the body back (a 500 first, so it is retried and logged, then 200s)."""
    from core import outbound

    def post(url, body, headers, timeout=15):
        outbound.check_url(url)
        posted.append((url, body, dict(headers)))
        echo = f"{url} {json.dumps(dict(headers))} {body.decode()}".encode()
        return graph.Reply(500 if len(posted) == 1 else 200, {}, echo)

    return post


# Every route and capability, the preview features (Okta, Google Workspace, ServiceNow, Halo) included.
@pytest.mark.usefixtures("preview_on")
def test_no_secret_or_token_leaves_through_any_surface(
    org, api, planted, caplog, monkeypatch, django_capture_on_commit_callbacks
):
    caplog.set_level(logging.DEBUG)
    a = org
    out = Responses()
    no_digest()  # the sweep makes its own digest row (tests/test_drf_resources.py _digest)
    admin = api(a["users"]["admin"])
    editor = api(a["users"]["editor"])

    # Channels: Graph mail (tested: the mail simulator issues a token), Teams and a signed webhook.
    out.keep(
        admin.patch(
            "notification-settings/email",
            {
                "transport": "graph",
                "graph_tenant_id": "simulated",
                "graph_client_id": "11111111-2222-4333-8444-555555555555",
                "graph_secret": SECRETS["mail"],
                "sender_mailbox": "softwarden@contoso.example",
            },
        )
    )
    out.keep(admin.post("notification-settings/email/test-connection"))
    out.keep(admin.post("notification-settings/teams-destinations", {"name": "IT Ops", "url": SECRETS["teams"]}))
    out.keep(
        admin.post(
            "notification-settings/webhook-destinations",
            {"name": "Hook", "url": SECRETS["webhook"], "signing_secret": SECRETS["signing"]},
        )
    )
    assert EmailSettings.objects.filter(organization=a["org"]).exists()
    assert TeamsDestination.objects.exists() and WebhookDestination.objects.exists()
    # The webhook subscribes to every family and its secret is rotated (both sign during the grace period);
    # the events below reach it through a transport that echoes everything back.
    from notify import channels

    posted: list = []
    monkeypatch.setattr(channels, "post_https", webhook_echo(posted))
    hook = WebhookDestination.objects.get(organization=a["org"])
    out.keep(
        admin.patch(f"webhooks/{hook.id}", {"events": ["record.*", "relationship.*", "discovery.*", "reminder.*"]})
    )
    out.keep(admin.post(f"webhooks/{hook.id}/rotate-secret", {"signing_secret": SECRETS["signing_rotated"]}))
    out.keep(admin.post(f"webhooks/{hook.id}/ping"))

    # Connections and a connector for every source; access checks; Run now for every one, run as jobs.
    made, conns = make_connectors(admin, a)
    for c in made.values():
        out.keep(admin.post(f"connectors/{c.id}/access-check"))
        out.keep(admin.post(f"connectors/{c.id}/run"))
    with django_capture_on_commit_callbacks(execute=True):  # the reminder flushes once each run commits
        drain_jobs()
    runs = ConnectorRun.objects.filter(connector__in=made.values())
    assert {r.status for r in runs} == {RunStatus.SUCCEEDED}, [(r.connector.name, r.error) for r in runs]

    # Reconciliation: link (with a source difference), ignore and apply it, create, dismiss, undo, missing kept.
    entra_c = made["Entra (exposure)"]
    zoom = Observation.objects.get(connector=entra_c, display_name="Zoom")
    out.keep(editor.post(f"discovery/observations/{zoom.id}/link", {"resource": str(a["app"].id)}))
    out.keep(editor.get(f"discovery/observations/{zoom.id}/drift"))
    out.keep(editor.post(f"discovery/observations/{zoom.id}/drift/ignore", {"fields": ["description"]}))
    drift = editor.get(f"discovery/observations/{zoom.id}/drift").json_body
    name_row = next(d for d in drift["differences"] if d["field"] == "name")
    out.keep(
        editor.post(
            f"discovery/observations/{zoom.id}/drift/apply",
            {"fields": ["name"], "seen": {"name": name_row["source"]}},
        )
    )
    hub = Observation.objects.get(connector=entra_c, display_name="Expense Hub")
    with django_capture_on_commit_callbacks(execute=True):  # the reminder flush (record.created: the outbox)
        out.keep(editor.post(f"discovery/observations/{hub.id}/create", {}))
    snow = Observation.objects.get(connector=entra_c, display_name="ServiceNow")
    out.keep(editor.post(f"discovery/observations/{snow.id}/dismiss", {"reason": "Test registration"}))
    out.keep(editor.post(f"discovery/observations/{snow.id}/reopen", {}))
    configure(entra_c, simulator_changes=[{"op": "remove", "id": "Expense Hub"}])
    out.keep(admin.post(f"connectors/{entra_c.id}/run"))
    drain_jobs()
    out.keep(editor.post(f"discovery/observations/{hub.id}/missing/acknowledge", {"note": "Moved"}))

    # Failures: the token endpoints echo the secret, the reads echo the bearer token; access checks see them too.
    configure(entra_c, simulated_token_echo=True)
    configure(made["Licences (exposure)"], simulated_errors={"/v1.0/subscribedSkus": 500})
    configure(made["Azure (exposure)"], simulated_errors={"/subscriptions": 400})
    configure(made["Power Platform (exposure)"], simulated_token_echo=True)
    configure(
        made["All environments (exposure)"],
        simulated_errors={"/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments": 400},
    )
    configure(made["Halo (exposure)"], simulated_errors={halo.ASSETS_PATH: 400})
    for c in made.values():
        out.keep(admin.post(f"connectors/{c.id}/access-check"))
        out.keep(admin.post(f"connectors/{c.id}/run"))
    drain_jobs()
    failed = ConnectorRun.objects.filter(connector__in=made.values(), status=RunStatus.FAILED)
    assert failed.count() >= 4, [(r.connector.name, r.status, r.error) for r in ConnectorRun.objects.all()]
    # A failing connection test echoing the secret, then a replaced secret.
    out.keep(admin.patch(f"connections/microsoft/{conns['ms']}", {"secret": SECRETS["entra_replaced"]}))
    configure(entra_c, simulated_token_echo=False)
    # A run whose worker stalled: its lease runs out and the sweep fails it.
    stalled = discovery.request_run(a["org"], a["users"]["admin"], made["Halo (exposure)"])
    job = jobs.claim(ids=[stalled.job_id])
    ConnectorRun.objects.filter(id=stalled.id).update(
        status=RunStatus.RUNNING, started_at=timezone.now(), lease_expires=timezone.now() - timedelta(minutes=1)
    )
    assert discovery.fail_stuck() == 1
    jobs.abandon_expired(timezone.now() + discovery.RUN_LEASE + timedelta(minutes=1))
    assert Job.objects.get(id=job.id).status == "abandoned"

    # The schedule queues, the worker tick runs schedules and jobs (the webhook deliveries among them); reminders and
    # notification deliveries.
    worker.tick()
    reminders.generate(a["org"], org_today(a["org"]))
    rules.run_for_org(a["org"], timezone.now())
    drain_jobs()
    assert Reminder.objects.filter(organization=a["org"]).exists()

    # The assistant: its key planted, a batch of suggestions made with it, an Editor accepting one.
    FakeAnthropic.keys = []
    monkeypatch.setattr(assistant, "make_client", FakeAnthropic)
    out.keep(admin.patch("assistant/settings", {"enabled": True, "api_key": SECRETS["assistant"]}))
    out.keep(editor.post("assistant/batches", {"connector": str(made["Halo (exposure)"].id)}))
    drain_jobs()
    assert FakeAnthropic.keys == [SECRETS["assistant"]]
    suggested = AssistantSuggestion.objects.filter(state="ready").first()
    assert suggested is not None
    out.keep(
        editor.post(
            f"discovery/observations/{suggested.observation_id}/assistant/accept", {"suggestion": str(suggested.id)}
        )
    )

    # Every GET route for every role, exports and run history included.
    paths = get_paths(a, a["users"]["admin"])
    extra = [
        "/api/v1/discovery/runs?page_size=200",
        "/api/v1/discovery/candidates?status=all",
        "/api/v1/discovery/credentials?state=all",
        "/api/v1/notification-log?page_size=200",
        "/api/v1/webhook-deliveries?page_size=200",
        *(f"/api/v1/webhook-deliveries/{d.id}" for d in Delivery.objects.filter(channel="webhook")),
        "/api/v1/search?q=Entra",
        "/api/v1/search?q=Halo",
        "/api/v1/composite/search?q=Entra",
        f"/api/v1/composite/dependencies?target={a['app'].id}&direction=both&depth=all",
        "/api/v1/composite/expiring-credentials?within_days=365&limit=200",
        *(f"/api/v1/connectors/{c.id}" for c in made.values()),
        *(f"/api/v1/connectors/{c.id}/runs" for c in made.values()),
        *(f"/api/v1/discovery/observations/{o}" for o in (zoom.id, hub.id, snow.id)),
    ]
    assert len(paths) > 100
    admin_status: dict[str, int] = {}
    for role in ROLES:
        client = api(a["users"][role]).client
        for path in [*paths, *extra]:
            response = out.keep(client.get(path))
            if role == "admin":
                admin_status[path] = response.status_code
    # Not vacuous: the Admin pass reads (almost) everything, and what it read is the planted organization's.
    refused = {p: s for p, s in admin_status.items() if s != 200}
    unexpected = {p: s for p, s in refused.items() if ADMIN_NOT_200.get(p, (0,))[0] != s}
    assert not unexpected, json.dumps(unexpected)
    assert len(admin_status) - len(refused) > 100
    seen = "\n".join(out.texts)
    for marker in [*made, *(str(r.id) for r in ConnectorRun.objects.filter(connector__in=made.values()))]:
        assert marker in seen, f"{marker} never appeared: the sweep didn't read the planted organization"
    assert "Zoom" in seen and "IT Ops" in seen and "secret_set_on" in seen
    exports = [
        api(a["users"]["admin"]).client.get(p)
        for p in ("/api/v1/audit-events/export.csv", "/api/v1/audit-events/export.jsonl")
    ]
    assert all(r.status_code == 200 for r in exports)
    for r in exports:
        out.keep(r)
    # The platform console sees every organization, so it must leak nothing either.
    from catalog import platform_admin, usage

    staff = get_user_model().objects.create_user(username="ops@x.test", email="ops@x.test", password="ops-password-1")
    platform_admin.grant_staff(staff)
    usage.refresh_all(timezone.now())
    console = api(staff).client
    for path in (
        "/api/v1/platform/session",
        "/api/v1/platform/organizations",
        f"/api/v1/platform/organizations/{a['org'].id}",
        f"/api/v1/platform/organizations/{a['org'].id}/capability-overrides",
        "/api/v1/platform/audit-events",
        "/api/v1/platform/capabilities",
        "/api/v1/platform/impersonation",
        "/api/v1/platform/second-factor",
        "/api/v1/platform/system",
        "/api/v1/platform/sign-in-domains?domain=orga.test",
    ):
        response = out.keep(console.get(path))
        assert response.status_code == 200, path

    # Everywhere a value could have been kept.
    events = AuditEvent.objects.all()
    assert {
        "connector.run_requested",
        "connector.access_checked",
        "observation.linked",
        "microsoft_connection.updated",
    } <= {e.action for e in events}
    records = [json.dumps(record.__dict__, default=str) for record in caplog.records]
    stored = {
        "log records": "\n".join([caplog.text, *records]),
        "audit": json.dumps([[e.action, e.changes, e.prechange, e.postchange] for e in events], default=str),
        "responses": "\n".join(out.texts),
        "jobs": json.dumps(list(Job.objects.values("kind", "payload", "result", "last_error")), default=str),
        "schedules": json.dumps(list(Schedule.objects.values("name", "last_result", "last_error")), default=str),
        "reminders": json.dumps(list(Reminder.objects.values()), default=str),
        "notification log": json.dumps(list(Delivery.objects.values()), default=str),
        "delivery attempts": json.dumps(list(DeliveryAttempt.objects.values()), default=str),
        "webhook outbox": json.dumps(list(WebhookEvent.objects.values()), default=str),
        "observations": json.dumps(list(Observation.objects.values("display_name", "data")), default=str),
        "credentials": json.dumps(list(DiscoveredCredential.objects.values()), default=str),
        "runs": json.dumps(list(ConnectorRun.objects.values("error", "counts")), default=str),
        "assistant": json.dumps(
            [
                list(AssistantBatch.objects.values()),
                list(AssistantUsage.objects.values()),
                list(AssistantSuggestion.objects.values()),
            ],
            default=str,
        ),
        "connectors": json.dumps(list(Connector.objects.values("access_check")), default=str),
        "connection tests": json.dumps(
            list(MicrosoftConnection.objects.values("test_result"))
            + list(HaloConnection.objects.values("test_result")),
            default=str,
        ),
    }
    values = planted_values(ciphertexts())
    for where, text in stored.items():
        assert_clean(where, text, values)
    # The job payloads carry IDs only.
    for job in Job.objects.filter(kind=discovery.RUN_JOB):
        assert set(job.payload) == {"connector_id"}
    for job in Job.objects.filter(kind=assistant.JOB):
        assert set(job.payload) == {"batch_id"}
    # The webhook received events, signed with both secrets, and an echoed answer was logged (and scrubbed).
    posted_types = {json.loads(body)["type"] for _url, body, _headers in posted}
    assert {"ping", "discovery.candidate", "record.created"} <= posted_types, posted_types
    assert all(h["X-Softwarden-Signature"].count("v1=") == 2 for _u, _b, h in posted)
    echoed = DeliveryAttempt.objects.filter(status_code=500).exclude(response_excerpt="")
    assert echoed.exists() and all("[redacted]" in row.response_excerpt for row in echoed)


def test_failures_and_logs_never_include_the_secret_or_the_token(org, planted, caplog):
    """For each source: the token endpoint echoes the
    secret, then the read echoes the bearer token. The run fails readably and keeps neither."""
    caplog.set_level(logging.DEBUG)
    a = org
    admin = api_client(a)
    made, _ = make_connectors(admin, a)
    for c in made.values():
        configure(c, simulated_token_echo=True)
    for c in made.values():
        run = discovery.run(Connector.objects.select_related("organization").get(pk=c.pk))
        assert run.status == RunStatus.FAILED, (c.name, run.counts)  # every token endpoint refused, echoing the secret
    for c in made.values():
        configure(c, simulated_token_echo=False, simulated_errors=READ_ERRORS)
        run = discovery.run(Connector.objects.select_related("organization").get(pk=c.pk))
        assert run.status == RunStatus.FAILED, (c.name, run.counts)  # every read refused, echoing the bearer token
    runs = list(ConnectorRun.objects.values("error", "counts", "status"))
    assert len(runs) == 2 * len(made)
    text = "\n".join([caplog.text, *(json.dumps(r.__dict__, default=str) for r in caplog.records), json.dumps(runs)])
    assert_clean("failed runs and logs", text, planted_values(ciphertexts()))


def api_client(a):
    from tests.conftest import Api

    return Api(a["users"]["admin"])


def test_discovery_jobs_get_ids_not_credentials(org, api, planted):
    """Run now and the schedule queue jobs whose payload is the
    connector's ID only; no secret, ciphertext or token in any job column."""
    a = org
    admin = api(a["users"]["admin"])
    made, _ = make_connectors(admin, a)
    for c in made.values():
        assert admin.post(f"connectors/{c.id}/run").status_code == 202
    queued = list(Job.objects.filter(kind=discovery.RUN_JOB))
    assert len(queued) == len(made)
    assert all(job.payload == {"connector_id": str(job.payload["connector_id"])} for job in queued)
    drain_jobs()
    text = json.dumps(list(Job.objects.values()), default=str)
    assert_clean("jobs", text, planted_values(ciphertexts()))


def test_local_sign_in_and_reads_survive_a_missing_credential_key(org, api, monkeypatch):
    """With the key gone, password sign-in and every read
    still work; a run says the secret can't be read, in words, and nothing tries a fallback."""
    a = org
    admin = api(a["users"]["admin"])
    made, conns = make_connectors(admin, a)
    monkeypatch.delenv("SOFTWARDEN_CREDENTIAL_KEYS")
    client = api()
    good = client.post("auth/login", {"email": "viewer@orga.test", "password": "correct-horse-battery"})
    assert good.status_code == 200
    for url in ("connectors", f"connections/microsoft/{conns['ms']}", "discovery/runs", "notification-settings/email"):
        response = admin.get(url)
        assert response.status_code == 200, url
        assert_clean(url, response.content.decode(), planted_values())
    run = discovery.run(Connector.objects.select_related("organization").get(pk=made["Entra (exposure)"].pk))
    assert run.status == RunStatus.FAILED and "can't be read" in run.error


# Every source's first read answers with an error body echoing the bearer token.
READ_ERRORS = {
    "/v1.0/servicePrincipals": 401,
    "/v1.0/subscribedSkus": 401,
    "/subscriptions": 400,
    "/api/data/v9.2/bots": 400,
    "/api/data/v9.2/workflows": 400,
    "/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments": 400,
    halo.ASSETS_PATH: 400,
}


# Admin GET routes that answer something other than 200 in the sweep, each for a reason unrelated to secrets: they need
# a parameter or a state the sweep's organization doesn't have. A route not listed here that stops answering 200 fails
# the sweep, so a 403 or 500 can't hide a surface.
ADMIN_NOT_200: dict[str, tuple[int, str]] = {
    "/api/v1/auth/csrf": (204, "sets the CSRF cookie, no body"),
    "/api/v1/auth/oidc/start": (302, "redirects to the sign-in page: Entra sign-in isn't configured here"),
    "/api/v1/auth/oidc/callback": (302, "redirects to the sign-in page without a code and state"),
    "/api/v1/auth/saml/start": (302, "redirects to the sign-in page: SAML sign-in isn't configured here"),
    "/api/v1/calendar/renewals.ics": (404, "needs a feed token in the query"),
    "/api/v1/dependencies": (400, "needs a target record (the keyed composite routes read it with one)"),
    "/api/v1/search": (400, "needs q; read with q=Entra and q=Halo below"),
    "/api/v1/composite/search": (400, "needs q; read with q=Entra below"),
    "/api/v1/composite/dependencies": (400, "needs a target record; read with the organization's application below"),
    "/api/v1/ticket-links/parse": (400, "needs url (ticket links): it only parses an address, no data"),
    # Platform admin console: platform staff only; a staff session reads them (and the detail) below.
    "/api/v1/platform/session": (403, "platform staff only; read by staff below"),
    "/api/v1/platform/organizations": (403, "platform staff only; read by staff below"),
    "/api/v1/platform/capabilities": (403, "platform staff only; read by staff below"),
    "/api/v1/platform/audit-events": (403, "platform staff only; read by staff below"),
    "/api/v1/platform/impersonation": (403, "platform staff only; read by staff below"),
    "/api/v1/platform/second-factor": (403, "platform staff only; read by staff below"),
    "/api/v1/platform/system": (403, "platform staff only; read by staff below"),
    "/api/v1/platform/sign-in-domains": (403, "platform staff only; read by staff below"),
}


def test_the_sweep_catches_a_planted_value_positive_control(org, api, planted):
    """The checks can fail: a planted secret or token in a response, a log line or a stored row is found."""
    for value in planted_values():
        with pytest.raises(AssertionError, match="found in control"):
            assert_clean("control", f"prefix {value} suffix", planted_values())
    out = Responses()
    from django.http import HttpResponse

    out.keep(HttpResponse(f"<p>{SECRETS['halo']}</p>", headers={"X-Token": TOKENS["arm"]}))
    with pytest.raises(AssertionError):
        assert_clean("control response", "\n".join(out.texts), [SECRETS["halo"]])
    with pytest.raises(AssertionError):
        assert_clean("control headers", "\n".join(out.texts), [TOKENS["arm"]])


class Exploding:
    """A transport that issues the token, then raises an exception whose message holds the secret and the token."""

    def __init__(self, inner):
        self.inner = inner

    def send(self, method, url, headers, body=None):
        if method == "POST":
            return self.inner.send(method, url, headers, body)
        raise RuntimeError(f"socket said {SECRETS['entra']} with {headers.get('Authorization', '')}")


def test_an_unexpected_error_never_logs_or_stores_its_message(org, api, planted, caplog, monkeypatch):
    """An unexpected exception's text is never logged or stored, only its type and the stack; the
    run says "Unexpected error (RuntimeError)". The tokens issued during the run are redacted too."""
    from discovery import graph_source

    caplog.set_level(logging.DEBUG)
    a = org
    admin = api(a["users"]["admin"])
    made, _ = make_connectors(admin, a)
    real = graph_source.entra_simulator

    def exploding(connector):
        return Exploding(real(connector))

    monkeypatch.setattr(graph_source, "entra_simulator", exploding)
    entra_c = made["Entra (exposure)"]
    assert admin.post(f"connectors/{entra_c.id}/run").status_code == 202
    drain_jobs()
    run = ConnectorRun.objects.get(connector=entra_c)
    assert run.status == RunStatus.FAILED and run.error == "Unexpected error (RuntimeError). See the worker log."
    assert any("type=RuntimeError" in r.getMessage() for r in caplog.records)
    text = "\n".join(
        [
            caplog.text,
            *(json.dumps(r.__dict__, default=str) for r in caplog.records),
            json.dumps(list(ConnectorRun.objects.values("error", "counts")), default=str),
            json.dumps(list(Job.objects.values("payload", "result", "last_error")), default=str),
            admin.get(f"discovery/runs?connector={entra_c.id}").content.decode(),
        ]
    )
    assert_clean("an unexpected error", text, planted_values(ciphertexts()))


def test_the_redaction_covers_the_tokens_issued_during_the_run():
    from discovery import engine as discovery_engine

    text = f"a {TOKENS['graph']} and {SECRETS['entra']}"
    credential = type("C", (), {"secret": SECRETS["entra"]})()
    assert discovery_engine._redact(text, credential, [TOKENS["graph"]]) == "a [redacted] and [redacted]"
