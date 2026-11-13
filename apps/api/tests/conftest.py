import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from catalog.models import Application, Person, Resource, Team
from core.models import AuditEvent, Membership, Organization, Role
from core.request_context import unit_of_work
from tests.sso_helpers import legacy_entra, simulator  # noqa: F401  (fixtures for the single sign-on tests)


@pytest.fixture(autouse=True)
def fast_password_hasher(settings):
    """Every fixture organization creates three users; full PBKDF2 hashing made the suite take minutes.
    Tests only, with synthetic passwords."""
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


# The words an audit payload's and a snapshot's keys are redacted for, written out here rather than imported
# from catalog/redaction.py: a word dropped from the code then fails the sweep below and
# tests/test_redaction_denylists.py instead of leaving the check along with the code.
AUDIT_SECRET_WORDS = (
    "password",
    "token",
    "secret",
    "session",
    "csrf",
    "cookie",
    "authorization",
    "api_key",
    "ciphertext",
    "licence_key",
    "license_key",
)
SNAPSHOT_SECRET_WORDS = (*AUDIT_SECRET_WORDS, "hash", "key_id", "credential", "private_key", "salt", "webhook")
REDACTED_VALUE = "[redacted]"


def named_like_a_secret(key, words) -> bool:
    return any(word in str(key).lower() for word in words)


def snapshot_problems(event):
    """What is wrong with an audit event: a key named like a secret whose value isn't
    redacted, at any depth, in its snapshots (the snapshot words) or its payload (the audit words), or a target type the
    audit log can't filter by."""
    from catalog.audit_log import TARGET_TYPES

    problems = []

    def walk(value, path, words):
        if isinstance(value, dict):
            for k, v in value.items():
                if named_like_a_secret(k, words) and v != REDACTED_VALUE:
                    problems.append(f"{event.action}: {path}.{k}")
                walk(v, f"{path}.{k}", words)
        elif isinstance(value, list):
            for v in value:
                walk(v, path, words)

    walk(event.prechange, "prechange", SNAPSHOT_SECRET_WORDS)
    walk(event.postchange, "postchange", SNAPSHOT_SECRET_WORDS)
    walk(event.changes, "changes", AUDIT_SECRET_WORDS)
    for kind in (event.target_type, event.related_target_type):
        if kind and kind not in TARGET_TYPES:
            problems.append(f"{event.action}: target type {kind} is not in audit_log.TARGET_TYPES")
    return problems


@pytest.fixture(autouse=True)
def audit_unit_and_secret_sweep():
    """Each test is one unit of work (core/request_context.py), as a request or a job is, and every audit event any
    test writes is swept: a snapshot holding a field named like a secret fails the test that wrote it. The sweep listens
    to post_save, so rows inserted with bulk_create (seed_scale's synthetic events, a few tests' fixtures) aren't swept;
    services write events one at a time through AuditEvent.objects.create, which is."""
    from django.db.models.signals import post_save

    found: list[str] = []

    def check(sender, instance, **kwargs):
        found.extend(snapshot_problems(instance))

    post_save.connect(check, sender=AuditEvent, weak=False, dispatch_uid="audit-secret-sweep")
    try:
        with unit_of_work():
            yield
    finally:
        post_save.disconnect(sender=AuditEvent, dispatch_uid="audit-secret-sweep")
    assert not found, f"Audit snapshots hold secret-looking fields: {found}"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "one_source_of_truth: after the test, check that every renewal has at most one driving contract and that its "
        "open cycle and auto-renewal match that contract (tests/conftest.py one_source_of_truth)",
    )


# ---- Live tests -------------------------------------------------------------------------------------------------------
# `@pytest.mark.live` tests call a real Microsoft tenant (tests/test_live_entra.py). They run only with `--live` AND the
# credentials in the environment, so the default gate (no flag) never reaches the network even on a machine that has
# the variables set. docs/DEVELOPMENT.md "Live tests" says how to run them against a test tenant you are allowed to read (never production).
LIVE_REQUIRED = ("SOFTWARDEN_LIVE_TENANT_ID", "SOFTWARDEN_LIVE_CLIENT_ID", "SOFTWARDEN_LIVE_CLIENT_SECRET")
LIVE_OPTIONAL = ("SOFTWARDEN_LIVE_SUBSCRIPTION_ID", "SOFTWARDEN_LIVE_DATAVERSE_URL")


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run the tests marked live against a real tenant (needs SOFTWARDEN_LIVE_TENANT_ID, _CLIENT_ID and "
        "_CLIENT_SECRET).",
    )


def live_skip_reason(live_flag: bool) -> str:
    """Why live tests are skipped now, or "" when they may run."""
    import os

    if not live_flag:
        return "live test: run with --live and real credentials in the environment (docs/DEVELOPMENT.md, Live tests)"
    missing = [name for name in LIVE_REQUIRED if not os.environ.get(name)]
    if missing:
        return f"live test: set {', '.join(missing)}"
    return ""


def pytest_collection_modifyitems(config, items):
    reason = live_skip_reason(bool(config.getoption("--live")))
    if not reason:
        return
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip)


def source_of_truth_problems() -> list[str]:
    """What breaks the one source of truth (money/contracts.py) in the database now: a renewal with two linked
    contracts that could both drive it outside one chain of terms (so `contracts._newest`'s tie-break would be
    reached), or a driven renewal whose open cycle (renewal date, notice period, no entered deadline) or auto-renewal
    differs from its driving contract's."""
    from money import contracts
    from money.models import Contract, RenewalCycle, RenewalObligation

    problems = []
    for obligation in RenewalObligation.objects.all():
        linked = list(Contract.objects.filter(renewal_obligation=obligation))
        eligible = [c for c in linked if contracts.can_drive(c)]
        renewed = {c.predecessor_id for c in eligible if c.predecessor_id is not None}
        heads = [c for c in eligible if c.id not in renewed]
        if len(heads) > 1:
            problems.append(f"{obligation.name}: {len(heads)} drivers ({', '.join(c.name for c in heads)})")
            continue
        if not heads:
            continue
        driving = heads[0]
        if obligation.auto_renew != driving.auto_renew:
            problems.append(f"{obligation.name}: auto_renew {obligation.auto_renew} != {driving.auto_renew}")
        cycle = RenewalCycle.objects.filter(obligation=obligation, completed_on__isnull=True).first()
        if cycle is None:  # completed, the next not established yet: it takes the driver's dates when it is
            continue
        wanted = {
            "renewal_date": driving.end_date,
            "notice_value": driving.notice_value,
            "notice_unit": driving.notice_unit or "",
            "explicit_deadline": None,
        }
        for field, value in wanted.items():
            if getattr(cycle, field) != value:
                problems.append(f"{obligation.name}: cycle {field} {getattr(cycle, field)!r} != {value!r}")
    return problems


@pytest.fixture(autouse=True)
def one_source_of_truth(request):
    """Opt in with `pytest.mark.one_source_of_truth` (the contracts and renewal-action tests do, module-wide): after
    each such test (links, unlinks, re-points, archive, restore, status changes, renewals, races) the one source of
    truth holds in the database (source_of_truth_problems)."""
    yield
    if request.node.get_closest_marker("one_source_of_truth") is not None:
        problems = source_of_truth_problems()
        assert not problems, problems


TEST_CREDENTIAL_KEY = "Hy0wJ7m3c5m8v1oNNpS7Ad0b2Qj0Zq3D3qPZfC0J0nA="  # a synthetic Fernet key, tests only


@pytest.fixture(autouse=True)
def credential_key(monkeypatch, tmp_path_factory, settings):
    """Stored secrets need a credential key (core/credentials.py); tests that check the missing-key path delete it.
    The secrets directory and the setup token file point at empty private folders, so nothing on this machine (a
    real /data/secrets, an old SOFTWARDEN_SECRETS_KEY) leaks into a test."""
    monkeypatch.setenv("SOFTWARDEN_CREDENTIAL_KEYS", TEST_CREDENTIAL_KEY)
    for name in ("SOFTWARDEN_CREDENTIAL_KEYS_FILE", "SOFTWARDEN_SECRETS_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SOFTWARDEN_SECRETS_DIR", str(tmp_path_factory.mktemp("secrets")))
    settings.SOFTWARDEN_BOOTSTRAP_TOKEN_FILE = str(tmp_path_factory.mktemp("bootstrap") / "setup-token")


@pytest.fixture(autouse=True)
def no_anthropic_api(monkeypatch, settings):
    """The assistant (discovery/assistant.py) never reaches the Anthropic API from a test: the installation key is
    blank and the client factory refuses, unless a test patches it with a fake (tests/test_assistant.py)."""
    from discovery import assistant

    settings.SOFTWARDEN_ASSISTANT_INSTALLATION_KEY = ""
    settings.SOFTWARDEN_ASSISTANT_INSTALLATION_CAP_USD = ""
    settings.SOFTWARDEN_ASSISTANT_MODEL = "claude-sonnet-5-5"
    settings.SOFTWARDEN_ASSISTANT_PRICES = ""

    def refuse(key):
        raise AssertionError("a test reached for the real Anthropic client; patch discovery.assistant.make_client")

    monkeypatch.setattr(assistant, "make_client", refuse)


@pytest.fixture(autouse=True)
def no_identity_provider_network(monkeypatch, settings):
    """Single sign-on never reaches a real identity provider from a test: only the in-process simulator
    answers (core/oidc_simulator.py, fresh keys and codes per test), discovery documents and key sets aren't cached
    across tests, and break-glass is off unless a test turns it on."""
    from core import oidc_discovery, oidc_simulator

    def refuse(method, url, body, headers):
        raise AssertionError(f"a test reached for a real identity provider ({method} {url.split('?')[0]})")

    monkeypatch.setattr(oidc_discovery, "network", refuse)
    settings.SOFTWARDEN_FORCE_LOCAL_LOGIN = False
    oidc_simulator.reset()
    oidc_discovery.clear_cache()
    for name in ("SOFTWARDEN_OIDC_TENANT", "SOFTWARDEN_OIDC_CLIENT_ID", "SOFTWARDEN_OIDC_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_eol_network(monkeypatch, settings):
    """End-of-life data (eol/client.py) never comes from endoflife.date in a test: the transport refuses and
    the backoff doesn't sleep, unless a test patches `eol.client.network` with recorded answers (tests/test_eol.py)."""
    from eol import client

    def refuse(url, headers, body_max):
        raise AssertionError(f"a test reached for endoflife.date ({url}); patch eol.client.network")

    monkeypatch.setattr(client, "network", refuse)
    monkeypatch.setattr(client, "sleep", lambda seconds: None)
    settings.SOFTWARDEN_EOL_ENABLED = True


@pytest.fixture
def fresh(db):
    """A fresh installation: the first-run state row open, no organization or user, an empty bad-token count. Shared:
    a transactional test (test_setup's concurrent setups) flushes every table, the state row included, and
    with --reuse-db that empty table outlives the run, so every module that bootstraps an installation starts here."""
    from django.core.cache import cache

    from core.models import BootstrapState

    BootstrapState.objects.update_or_create(pk=1, defaults={"completed_at": None})
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def fresh_migration_check():
    """The readiness probes keep their migration check for 30 s per process (softwarden/ops.py); every test
    starts without one, so a test that fakes pending migrations sees its own answer."""
    from softwarden import ops

    ops._migration_checks.clear()
    yield
    ops._migration_checks.clear()


@pytest.fixture(autouse=True)
def preview_features_off(settings):
    """The installation's preview features (Okta and Google Workspace discovery, ServiceNow and Halo ticket
    connections; core/capabilities.py) are off, as by default, whatever this machine's environment says. Modules that
    cover those features turn them on with `pytestmark = pytest.mark.usefixtures("preview_on")`."""
    settings.SOFTWARDEN_PREVIEW_FEATURES = False


@pytest.fixture(autouse=True)
def simulation_allowed(settings):
    """The simulated tenants and hosts are allowed (core/simulation.py), as the simulator suites assume, whatever this
    machine's environment says: pytest runs with DEBUG off, so the explicit flag stands in for it. Tests of the
    refusal use `simulation_off`."""
    settings.SOFTWARDEN_ALLOW_SIMULATED = True


@pytest.fixture
def simulation_off(settings, simulation_allowed, monkeypatch):
    """A production server: DEBUG off, not the Playwright server, SOFTWARDEN_ALLOW_SIMULATED unset."""
    settings.DEBUG = False
    settings.SOFTWARDEN_ALLOW_SIMULATED = False
    monkeypatch.delenv("SOFTWARDEN_E2E", raising=False)


@pytest.fixture
def preview_on(settings, preview_features_off):
    """SOFTWARDEN_PREVIEW_FEATURES on for the test (after the autouse default above)."""
    settings.SOFTWARDEN_PREVIEW_FEATURES = True


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path_factory):
    """Attachment files (catalog/attachments.py) go to an empty folder per test, never the checkout's media/."""
    settings.MEDIA_ROOT = str(tmp_path_factory.mktemp("media"))
    return settings.MEDIA_ROOT


class Api:
    """JSON test client for one signed-in user."""

    def __init__(self, user=None, enforce_csrf=False):
        self.client = Client(enforce_csrf_checks=enforce_csrf)
        if user is not None:
            self.client.force_login(user)

    def _call(self, method, url, data=None):
        kwargs = {"content_type": "application/json"}
        if data is not None:
            kwargs["data"] = json.dumps(data)
        response = getattr(self.client, method)("/api/v1/" + url, **kwargs)
        is_json = "json" in response.get("Content-Type", "")  # an export answers with a file
        response.json_body = response.json() if not response.streaming and response.content and is_json else None
        return response

    def get(self, url):
        return self._call("get", url)

    def post(self, url, data=None):
        return self._call("post", url, data or {})

    def delete(self, url, data=None):
        return self._call("delete", url)

    def patch(self, url, data=None):
        return self._call("patch", url, data or {})


def rel_type(org, slug="depends_on"):
    """The organization's relationship type with this slug (the core types are seeded with every organization)."""
    from catalog.models import RelationshipType

    return RelationshipType.objects.get(organization=org, slug=slug)


def make_org(name):
    org = Organization.objects.create(name=name)
    users = {}
    for role in Role.values:
        user = get_user_model().objects.create_user(
            username=f"{role}@{name}.test", email=f"{role}@{name}.test", password="correct-horse-battery"
        )
        Membership.objects.create(user=user, organization=org, role=role)
        users[role] = user
    org.coordinator = users["admin"]
    org.save()
    people = {
        "dana": Person.objects.create(organization=org, name="Dana Ortiz"),
        "priya": Person.objects.create(organization=org, name="Priya Raman"),
        "gone": Person.objects.create(organization=org, name="Former person", active=False),
    }
    team = Team.objects.create(organization=org, name="IT Applications")
    app = Resource.objects.create(
        organization=org, name=f"{name} App", slug=f"{name}-app", lifecycle="active", it_steward=users["editor"]
    )
    Application.objects.create(resource=app, organization=org)
    return {"org": org, "users": users, "people": people, "team": team, "app": app}


@pytest.fixture
def a(db):
    return make_org("orga")


@pytest.fixture
def b(db):
    return make_org("orgb")


@pytest.fixture
def api():
    return lambda user=None, **kw: Api(user, **kw)


def digest_row(org, user, status="failed", attempts=0, last_error="", for_date=None, run=None):
    """A built digest and its delivery (a digest is a delivery of kind digest), in a given state."""
    from core.dates import org_today
    from notify.digest import delivery_key
    from notify.models import Delivery, Digest, DigestRun

    day = for_date or org_today(org)
    run = run or DigestRun.objects.get_or_create(organization=org, for_date=day)[0]
    delivery = Delivery.objects.create(
        organization=org,
        channel="email",
        kind="digest",
        status={"pending": "queued"}.get(status, status),
        dedupe_key=delivery_key(user.id, day),
        recipient=user.email,
        subject="s",
        content={"text": "t", "html": "h"},
        attempts=attempts,
        last_error=last_error,
    )
    return Digest.objects.create(organization=org, run=run, user=user, for_date=day, delivery=delivery)


def drain_jobs(kind=None):
    """Claim and run every due job (only `kind`, when given), as the worker's tick would, and return them. A connector run is a `discovery.run` job: Run now and the schedule only queue it."""
    from core import jobs
    from core.models import Job, JobStatus

    done = []
    while True:
        ids = None
        if kind is not None:
            ids = list(Job.objects.filter(kind=kind, status=JobStatus.QUEUED).values_list("id", flat=True))
            if not ids:
                break
        job = jobs.claim(ids=ids)
        if job is None:
            break
        jobs.run(job)
        done.append(job)
    return done


def no_digest():
    """Turn the Daily digest rule off everywhere. A digest is a delivery job, so a test that counts what
    a worker tick runs would otherwise see one more job whenever the suite runs after the digest time (08:00)."""
    from notify.models import NotificationRule

    NotificationRule.objects.filter(builtin_key="daily_digest").update(enabled=False)


def quiet_schedules(*keep, only=None):
    """Turn off every registered schedule but `keep` (by name), or with `only` just those, the way no_digest turns the
    digest off: a test that ticks the worker at the real time of day and counts what the tick ran would otherwise also
    see whichever app schedule is due that minute (eol.refresh at 04:45 queues a job, backup.nightly at 02:30 starts a
    backup). The rows are made as the tick's own sync would make them, then disabled; a schedule a
    test registers later is unaffected."""
    from django.utils import timezone

    from core import jobs
    from core.models import Schedule

    jobs.sync_schedules(timezone.now())
    rows = Schedule.objects.filter(name__in=only) if only is not None else Schedule.objects.exclude(name__in=keep)
    rows.update(enabled=False)


def pinned_now(time_of_day):
    """The first UTC moment at `time_of_day` ("HH:MM:SS") at or after the real now: never earlier than the real
    clock, so a row a field default stamped with the real time (Job.run_after, created_at) is never in the future."""
    from datetime import UTC, datetime, time, timedelta

    from django.utils import timezone

    real = timezone.now()
    moment = datetime.combine(real.date(), time.fromisoformat(time_of_day), tzinfo=UTC)
    return moment if moment >= real else moment + timedelta(days=1)


@pytest.fixture
def clock(request, monkeypatch):
    """Pin `django.utils.timezone.now` for one test: parametrize indirectly with a UTC time of day ("04:45:00") or None
    for the real clock. The time stands still, so a test pinned just before a schedule's minute or the digest time
    stays there and one pinned inside it stays inside. Returns the pinned moment (None when real)."""
    from django.utils import timezone

    time_of_day = getattr(request, "param", None)
    if time_of_day is None:
        return None
    moment = pinned_now(time_of_day)
    monkeypatch.setattr(timezone, "now", lambda: moment)
    return moment


# The moments the time-of-day tests are pinned to: just before and inside eol.refresh's minute
# (04:45 UTC), and just before and at the fixture organizations' digest time (08:00 UTC), plus the real clock.
TICK_MOMENTS = [None, "04:44:59", "04:45:00", "04:45:30", "07:59:59", "08:00:00", "23:59:59"]


def revoke_capability(org, name):
    """Take one capability away from `org`, as a platform revoke does: every edition has every feature in this
    repository, so this is how a test shows a feature's gate."""
    from core.capabilities import forget_organization
    from core.models import CapabilityOverride

    CapabilityOverride.objects.create(organization=org, capability=name, effect="revoke", reason="test")
    forget_organization(org.pk)
