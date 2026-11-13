"""End-of-life tracking via endoflife.date (eol/).

The cache refresh against recorded API answers (tests/fixtures/eol, saved from https://endoflife.date/api/v1 on
2026-11-09): new data, unchanged (304 with the ETag sent back), errors, retries and redirects; the transport going
through core/outbound's public-only rule; status derivation for every shape of the eol and support fields; the
services and their audit; the API (roles, filters, CSV, the record detail, the product search); Needs attention, the
data-quality rule and the reminder facts; tenancy; the disabled mode and the offline import. No test reaches the
network: tests/conftest.py replaces eol.client.network with a refusal, and these tests replace it with FakeEol."""

import io
import json
import re
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from catalog import org_lifecycle, quality, services
from catalog.audit_log import record_events
from catalog.models import Application, Resource
from core import capabilities, jobs, outbound
from core.dates import org_today
from core.graph import Reply, Unreachable
from core.impersonation_routes import ALLOWED_WRITES, RESTRICTED_ROUTES
from core.models import AuditEvent, Job, JobStatus
from core.problems import Conflict, FieldErrors
from eol import attention as eol_attention
from eol import cache, client, reads, reminders
from eol import services as eol_services
from eol.models import EolProduct, EolRelease, EolSyncState, TrackedVersion
from eol.status import (
    END_OF_LIFE,
    SECURITY_ONLY,
    SUPPORTED,
    UNKNOWN,
    derive,
    derive_cycle,
    near,
)
from notify.attention import UNCOUNTED, attention_queues
from notify.digest import PENDING_SECTIONS, SECTION_NAMES, SECTIONS
from tests.conftest import drain_jobs

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).parent / "fixtures" / "eol"
REAL_NETWORK = client.network  # captured at import, before tests/conftest.py replaces it for each test


def fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.json").read_bytes()


def ok(name: str, etag: str = "") -> Reply:
    return Reply(200, {"etag": etag, "content-type": "application/json"}, fixture(name))


def conditional(name: str, etag: str):
    """200 with `etag`, or 304 when the request sends it back."""

    def answer(headers):
        if headers.get("If-None-Match") == etag:
            return Reply(304, {"etag": etag}, b"")
        return ok(name, etag)

    return answer


class FakeEol:
    """Stands in for eol.client.network: answers by path under /api/v1, from a Reply, an exception, a callable taking
    the headers, or a list of those used in turn. Records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers, body_max):
        self.calls.append((url, dict(headers)))
        path = url.removeprefix(client.BASE_URL)
        answer = self.routes.get(path)
        if answer is None:
            return Reply(404, {"content-type": "text/html"}, b"<html>Not found</html>")
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(headers)
        return answer

    @property
    def paths(self):
        return [url.removeprefix(client.BASE_URL) for url, _ in self.calls]


@pytest.fixture
def eol_api(monkeypatch):
    def install(routes):
        fake = FakeEol(routes)
        monkeypatch.setattr(client, "network", fake)
        return fake

    return install


@pytest.fixture
def slept(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(client, "sleep", waits.append)
    return waits


def recorded(eol_api):
    """The recorded list and PostgreSQL, cached through a real refresh (one tracked product)."""
    fake = eol_api(
        {"/products": conditional("products", '"L1"'), "/products/postgresql": conditional("postgresql", '"P1"')}
    )
    return fake


def app(ctx, name="Billing DB", version="", **extra):
    record = services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": "application", "lifecycle": "active", **extra}
    )
    if version:
        Application.objects.filter(resource=record).update(version=version)
    return record


def component(ctx, name="Nightly export"):
    return services.create_resource(
        ctx["org"], ctx["users"]["editor"], {"name": name, "kind": "component", "lifecycle": "active"}
    )


def product(name="testdb", label="TestDB", **releases):
    """A cached product whose releases are {cycle: EolRelease fields}."""
    p = EolProduct.objects.create(name=name, label=label, releases_fetched_at=timezone.now())
    for position, (cycle, fields) in enumerate(releases.items()):
        EolRelease.objects.create(product=p, name=cycle, position=position, **fields)
    return p


def today():
    return timezone.now().date()


# ---- the transport --------------------------------------------------------------------------------------------------


def test_the_guard_refuses_the_network_unless_a_test_patches_it():
    with pytest.raises(AssertionError, match="endoflife.date"):
        client.fetch(client.products_url())


def test_requests_go_through_the_outbound_public_only_rule(monkeypatch, settings):
    seen: dict[str, object] = {}

    def fake_request(method, url, body, headers, timeout, deadline, *, private_ok, body_max):
        seen.update(method=method, url=url, body=body, headers=headers, private_ok=private_ok, body_max=body_max)
        return Reply(200, {}, b"{}")

    monkeypatch.setattr(outbound, "request", fake_request)
    REAL_NETWORK(client.products_url(), {"User-Agent": client.user_agent()}, client.LIST_MAX)
    assert seen["method"] == "GET" and seen["body"] is None and seen["private_ok"] is False
    assert seen["url"] == "https://endoflife.date/api/v1/products" and seen["body_max"] == client.LIST_MAX
    assert client.user_agent() == f"Softwarden/{settings.APP_VERSION} (+{settings.PROJECT_URL})"
    assert settings.PROJECT_URL == "https://github.com/Softwarden/softwarden"
    # and the real guard: a private address is refused before anything is sent
    monkeypatch.undo()
    with pytest.raises(outbound.UnsafeDestination):
        outbound.request("GET", "https://10.0.0.1/api/v1/products", None, {}, private_ok=False)


def test_no_placeholder_repository_address_ships():
    """The API's own code names the real repository, never an `OWNER` placeholder (the address
    the end-of-life refresh sends endoflife.date was one)."""
    api = Path(__file__).resolve().parents[1]
    placeholder = re.compile(r"(?:github\.com|ghcr\.io)/OWNER\b|<repository URL>")
    skip = {".venv", "tests", "node_modules", "__pycache__"}
    found = [
        str(path.relative_to(api))
        for path in api.rglob("*.py")
        if not skip & set(path.relative_to(api).parts) and placeholder.search(path.read_text(encoding="utf-8"))
    ]
    assert not found


def test_fetch_sends_the_user_agent_and_the_etag(eol_api):
    fake = eol_api({"/products": conditional("products", '"L1"')})
    first = client.fetch(client.products_url())
    assert not first.not_modified and first.etag == '"L1"' and first.data["total"] == 10
    second = client.fetch(client.products_url(), etag='"L1"')
    assert second.not_modified and second.data is None
    (_, h1), (_, h2) = fake.calls
    assert h1["User-Agent"].startswith("Softwarden/") and h1["Accept"] == "application/json"
    assert "If-None-Match" not in h1 and h2["If-None-Match"] == '"L1"'


def test_fetch_retries_429_and_5xx_with_backoff_and_retry_after(eol_api, slept):
    eol_api({"/products": [Reply(503, {}, b""), Reply(429, {"retry-after": "7"}, b""), ok("products", '"L1"')]})
    assert client.fetch(client.products_url()).etag == '"L1"'
    assert slept == [2.0, 7.0]  # BACKOFF, then the longer of 4 s and Retry-After


def test_fetch_gives_up_after_its_attempts(eol_api, slept):
    fake = eol_api({"/products": Unreachable("ConnectionRefusedError", ambiguous=False)})
    with pytest.raises(client.FetchError, match="unreachable"):
        client.fetch(client.products_url())
    assert len(fake.calls) == client.ATTEMPTS and slept == [2.0, 4.0]


def test_fetch_refuses_a_long_retry_after_bad_json_a_cut_body_and_other_statuses(eol_api, slept):
    eol_api({"/products": Reply(429, {"retry-after": "3600"}, b"")})
    with pytest.raises(client.FetchError, match="retry after 3600"):
        client.fetch(client.products_url())
    eol_api({"/products": Reply(200, {}, b"<html>")})
    with pytest.raises(client.FetchError, match="isn't JSON"):
        client.fetch(client.products_url())
    eol_api({"/products": Reply(200, {}, b"x" * 100)})
    with pytest.raises(client.FetchError, match="larger"):
        client.fetch(client.products_url(), body_max=100)
    eol_api({"/products": Reply(403, {}, b"")})
    with pytest.raises(client.FetchError, match="HTTP 403"):
        client.fetch(client.products_url())
    with pytest.raises(client.NotFound):
        client.fetch(client.product_url("no-such-thing"))
    assert slept == []  # none of these is retried


def test_fetch_follows_a_rename_on_the_same_host_only(eol_api):
    fake = eol_api(
        {
            "/products/postgres-old": Reply(301, {"location": "/api/v1/products/postgresql"}, b""),
            "/products/postgresql": ok("postgresql", '"P1"'),
            "/products/elsewhere": Reply(301, {"location": "https://evil.example/api/v1/products/x"}, b""),
        }
    )
    assert client.fetch(client.product_url("postgres-old")).data["result"]["name"] == "postgresql"
    assert fake.paths == ["/products/postgres-old", "/products/postgresql"]
    with pytest.raises(client.FetchError, match="another host"):
        client.fetch(client.product_url("elsewhere"))


def test_an_unsafe_destination_is_an_error_not_a_retry(eol_api, slept):
    eol_api({"/products": outbound.UnsafeDestination("refused")})
    with pytest.raises(client.FetchError, match="refused"):
        client.fetch(client.products_url())
    assert slept == []


# ---- the cache refresh ------------------------------------------------------------------------------------------------


def test_a_refresh_fetches_the_list_and_only_the_tracked_products(a, eol_api):
    fake = recorded(eol_api)
    record = app(a, version="16.4")
    eol_services.track(a["org"], a["users"]["editor"], record, {"product": "postgresql"})
    counts = cache.refresh()
    assert fake.paths == ["/products", "/products/postgresql"]  # ubuntu is listed, not tracked: not fetched
    assert counts["products"] == 10 and counts["updated"] == 1 and counts["rederived"] == 1
    pg = EolProduct.objects.get(name="postgresql")
    assert pg.label == "PostgreSQL" and pg.link == "https://endoflife.date/postgresql" and "postgres" in pg.aliases
    assert pg.releases_etag == '"P1"' and pg.releases_fetched_at is not None and pg.error == ""
    assert pg.releases.count() == 32 and EolProduct.objects.get(name="ubuntu").releases.count() == 0
    sixteen = pg.releases.get(name="16")
    assert sixteen.eol_from == date(2028, 11, 9) and sixteen.has_eoas is False and sixteen.latest == "16.15"
    assert TrackedVersion.objects.get(resource=record).cycle == "16"  # derived from the application's version
    state = EolSyncState.objects.get()
    assert state.list_etag == '"L1"' and state.source == "api" and state.last_error == "" and state.last_success_at


def test_an_unchanged_refresh_sends_the_etags_and_keeps_the_data(a, eol_api):
    fake = recorded(eol_api)
    eol_services.track(a["org"], a["users"]["editor"], app(a), {"product": "postgresql", "cycle": "13"})
    cache.refresh()
    before = EolProduct.objects.get(name="postgresql").releases_checked_at
    fake.calls.clear()
    counts = cache.refresh()
    assert counts == {"list_unchanged": 1, "unchanged": 1, "rederived": 0}
    assert [h.get("If-None-Match") for _, h in fake.calls] == ['"L1"', '"P1"']
    pg = EolProduct.objects.get(name="postgresql")
    assert pg.releases.count() == 32 and before is not None and pg.releases_checked_at is not None
    assert pg.releases_checked_at > before


def test_a_failing_product_keeps_its_cached_releases_and_records_why(a, eol_api, slept):
    fake = recorded(eol_api)
    eol_services.track(a["org"], a["users"]["editor"], app(a), {"product": "postgresql"})
    eol_services.track(a["org"], a["users"]["editor"], component(a), {"product": "ubuntu"})
    fake.routes["/products/ubuntu"] = ok("ubuntu", '"U1"')
    cache.refresh()
    fake.routes["/products/postgresql"] = Reply(502, {}, b"")
    fake.routes["/products/ubuntu"] = Reply(404, {}, b"<html>")
    counts = cache.refresh()
    assert counts["failed"] == 1 and counts["not_found"] == 1
    pg, ubuntu = EolProduct.objects.get(name="postgresql"), EolProduct.objects.get(name="ubuntu")
    assert pg.error == "HTTP 502" and pg.releases.count() == 32  # kept
    assert ubuntu.error == "not on endoflife.date (404)" and ubuntu.listed is False
    state = EolSyncState.objects.get()
    assert state.last_error == "2 product(s) failed"


def test_the_job_retries_when_nothing_could_be_read_and_nothing_is_cached(a, eol_api):
    eol_api({"/products": Reply(500, {}, b"")})
    jobs.enqueue(cache.JOB, {})
    (job,) = drain_jobs(cache.JOB)
    job.refresh_from_db()
    assert job.status == JobStatus.QUEUED and job.attempts == 1 and "endoflife.date unreachable" in job.last_error
    assert EolSyncState.objects.get().last_error == "product list: HTTP 500"


def test_a_list_failure_with_a_cache_still_refreshes_the_tracked_products(a, eol_api):
    fake = recorded(eol_api)
    eol_services.track(a["org"], a["users"]["editor"], app(a), {"product": "postgresql"})
    cache.refresh()
    fake.routes["/products"] = Reply(500, {}, b"")
    counts = cache.refresh()
    assert counts["unchanged"] == 1 and EolSyncState.objects.get().last_error == "product list: HTTP 500"


def test_the_schedule_queues_one_job_in_the_long_lane(a):
    assert cache.SCHEDULE in jobs.SCHEDULES and jobs.JOB_KINDS[cache.JOB].lane == jobs.LONG_LANE
    assert cache.scheduled(timezone.now()) == {"queued": 1}
    assert cache.scheduled(timezone.now()) == {"skipped": "already queued"}
    assert Job.objects.filter(kind=cache.JOB, organization=None).count() == 1


def test_a_new_mapping_queues_its_product_never_fetching_in_the_request(a, api):
    response = api(a["users"]["editor"]).patch(f"applications/{a['app'].id}/end-of-life", {"product": "postgresql"})
    assert response.status_code == 200 and response.json_body["status"] == UNKNOWN
    assert "hasn't been downloaded yet" in response.json_body["why"]
    queued = Job.objects.get(kind=cache.JOB, status=JobStatus.QUEUED)
    assert queued.payload == {}  # no product list read yet: a full refresh, which brings the list with this product
    eol_services.track(a["org"], a["users"]["editor"], component(a), {"product": "mysql"})
    assert Job.objects.filter(kind=cache.JOB).count() == 1  # the queued full refresh covers it


def test_first_mapping_then_a_second_product(a, eol_api):
    """The first product's fetch must not make every other product look unknown. The first
    mapping (no list yet) queues a full refresh; afterwards a second product is checked against the list and fetched
    on its own."""
    fake = recorded(eol_api)
    fake.routes["/products/ubuntu"] = ok("ubuntu", '"U1"')
    editor = a["users"]["editor"]
    eol_services.track(a["org"], editor, app(a, version="13.2"), {"product": "postgresql"})
    drain_jobs(cache.JOB)
    assert fake.paths == ["/products", "/products/postgresql"] and cache.list_cached()
    assert TrackedVersion.objects.get().cycle == "13"
    fake.calls.clear()
    second = eol_services.track(a["org"], editor, app(a, "Server", version="22.04.3"), {"product": "Ubuntu-Linux"})
    assert second.product == "ubuntu"  # an alias, checked against the list
    assert Job.objects.get(kind=cache.JOB, status=JobStatus.QUEUED).payload == {"products": ["ubuntu"]}
    drain_jobs(cache.JOB)
    assert fake.paths == ["/products/ubuntu"]
    second.refresh_from_db()
    assert second.cycle == "22.04"
    with pytest.raises(FieldErrors):
        eol_services.track(a["org"], editor, component(a), {"product": "not-on-the-list"})


@pytest.mark.parametrize("how", ["product job", "failed fetch", "single import"])
def test_a_product_row_alone_never_counts_as_the_list(a, eol_api, slept, tmp_path, how):
    """A product fetched or imported on its own, or one whose fetch failed (egress blocked), is an unlisted row: until
    a product list is read, any well-formed product is still accepted."""
    if how == "product job":
        recorded(eol_api)
        cache.refresh(["postgresql"])
    elif how == "failed fetch":
        eol_api({"/products/postgresql": Unreachable("ConnectionRefusedError", ambiguous=False)})
        cache.refresh(["postgresql"])
        assert EolProduct.objects.get(name="postgresql").error.startswith("unreachable")
    else:
        path = tmp_path / "pg.json"
        path.write_bytes(fixture("postgresql"))
        call_command("eol_import", str(path), stdout=io.StringIO())
    assert EolProduct.objects.get(name="postgresql").listed is False and not cache.list_cached()
    row = eol_services.track(a["org"], a["users"]["editor"], component(a), {"product": "mysql"})
    assert row.product == "mysql"


# ---- disabled, and the offline import ---------------------------------------------------------------------------------


def test_disabled_fetches_and_queues_nothing(a, settings, eol_api):
    settings.SOFTWARDEN_EOL_ENABLED = False
    fake = recorded(eol_api)
    eol_services.track(a["org"], a["users"]["editor"], a["app"], {"product": "postgresql"})
    assert cache.refresh() == {"skipped": "disabled"}
    assert cache.scheduled(timezone.now()) == {"skipped": "disabled"}
    assert not Job.objects.filter(kind=cache.JOB).exists() and fake.calls == []
    with pytest.raises(CommandError, match="SOFTWARDEN_EOL_ENABLED=0"):
        call_command("eol_refresh")
    block = reads.record_block(a["org"], a["app"], today())
    assert block is not None and block["status"] == UNKNOWN and "downloads are off" in block["why"]


def full_document() -> dict:
    """What /api/v1/products/full returns, from the recorded list and the two recorded products."""
    listing = json.loads(fixture("products"))
    details = {n: json.loads(fixture(n))["result"] for n in ("postgresql", "ubuntu")}
    listing["result"] = [details.get(p["name"], p) for p in listing["result"]]
    return listing


def test_the_offline_import_loads_a_full_snapshot_and_rederives(a, settings, tmp_path):
    settings.SOFTWARDEN_EOL_ENABLED = False  # air-gapped
    eol_services.track(a["org"], a["users"]["editor"], app(a, version="22.04.3"), {"product": "ubuntu-linux"})
    assert TrackedVersion.objects.get().product == "ubuntu-linux"  # no list cached: any well-formed name
    path = tmp_path / "eol-full.json"
    path.write_text(json.dumps(full_document()), encoding="utf-8")
    out = io.StringIO()
    call_command("eol_import", str(path), stdout=out)
    assert "Imported 10 products and 77 release cycles" in out.getvalue()
    assert EolProduct.objects.get(name="ubuntu").releases.get(name="22.04").eoas_from == date(2024, 9, 30)
    state = EolSyncState.objects.get()
    assert state.source == "import" and state.imported_at is not None
    # The alias stored before the list existed matches no cycle until re-mapped: that is the data-quality gap.
    eol_services.track(
        a["org"], a["users"]["editor"], Resource.objects.get(name="Billing DB"), {"product": "ubuntu-linux"}
    )
    assert TrackedVersion.objects.get().product == "ubuntu" and TrackedVersion.objects.get().cycle == "22.04"


def test_the_offline_import_takes_one_product_and_refuses_anything_else(a, tmp_path):
    path = tmp_path / "pg.json"
    path.write_bytes(fixture("postgresql"))
    call_command("eol_import", str(path), stdout=io.StringIO())
    assert EolProduct.objects.get(name="postgresql").releases.count() == 32
    for content, message in ((b"not json", "isn't JSON"), (b'{"result": 3}', "Not an endoflife.date v1 document")):
        path.write_bytes(content)
        with pytest.raises(CommandError, match=message):
            call_command("eol_import", str(path), stdout=io.StringIO())
    with pytest.raises(CommandError, match="Can't read"):
        call_command("eol_import", str(tmp_path / "missing.json"), stdout=io.StringIO())


def test_parsing_skips_malformed_entries():
    now = timezone.now()
    cache.store_releases(
        "weird",
        {"releases": [{"name": "1", "eolFrom": "not-a-date", "isEol": "yes"}, {"label": "no name"}, "x", None]},
        now,
    )
    (only,) = EolRelease.objects.filter(product_id="weird")
    assert only.name == "1" and only.eol_from is None and only.is_eol is False and only.has_eoas is False
    with pytest.raises(client.FetchError):
        cache.store_list("not a list", now)
    with pytest.raises(client.FetchError):
        cache.store_list([{"name": "Bad Name!"}], now)


# ---- status derivation --------------------------------------------------------------------------------------------------


def release(**fields):
    base = {
        "product_id": "testdb",
        "name": "1",
        "eol_from": None,
        "is_eol": False,
        "has_eoas": False,
        "is_eoas": None,
        "eoas_from": None,
        "has_eoes": False,
        "is_eoes": None,
        "eoes_from": None,
    }
    return SimpleNamespace(**(base | fields))


TODAY = date(2026, 11, 9)


@pytest.mark.parametrize(
    "fields,status,days",
    [
        ({"eol_from": TODAY - timedelta(days=10), "is_eol": True}, END_OF_LIFE, -10),
        ({"eol_from": TODAY, "is_eol": False}, END_OF_LIFE, 0),  # the date wins over a boolean a day old
        ({"eol_from": TODAY + timedelta(days=30), "is_eol": True}, SUPPORTED, 30),  # ... both ways
        ({"eol_from": None, "is_eol": True}, END_OF_LIFE, None),
        ({"eol_from": None, "is_eol": False}, SUPPORTED, None),
        (
            {"eol_from": TODAY + timedelta(days=400), "has_eoas": True, "eoas_from": TODAY - timedelta(days=1)},
            SECURITY_ONLY,
            400,
        ),
        (
            {"eol_from": TODAY + timedelta(days=400), "has_eoas": True, "eoas_from": TODAY + timedelta(days=1)},
            SUPPORTED,
            400,
        ),
        ({"eol_from": TODAY + timedelta(days=400), "has_eoas": True, "is_eoas": True}, SECURITY_ONLY, 400),
        ({"eol_from": TODAY + timedelta(days=400), "has_eoas": True, "is_eoas": False}, SUPPORTED, 400),
        ({"eol_from": None, "is_eol": False, "has_eoas": True, "is_eoas": True}, SECURITY_ONLY, None),
        ({"eol_from": TODAY - timedelta(days=1), "has_eoas": True, "is_eoas": False}, END_OF_LIFE, -1),
    ],
)
def test_status_for_every_shape_of_the_eol_and_support_fields(fields, status, days):
    found = derive(release(**fields), TODAY, product_label="TestDB")
    assert found.status == status and found.days_remaining == days
    assert found.eol_date == fields["eol_from"]


def test_status_words_extended_support_and_unknown():
    gone = derive(
        release(eol_from=TODAY - timedelta(days=1), has_eoes=True, is_eoes=False, eoes_from=TODAY + timedelta(days=99)),
        TODAY,
        product_label="Ubuntu",
        cycle="20.04",
    )
    assert (
        gone.why == "Ubuntu 20.04 reached end of life on 2026-11-08 (1 day ago). Extended support runs to 2027-02-16."
    )
    assert gone.extended_support_until == TODAY + timedelta(days=99)
    ineligible = derive(release(eol_from=TODAY, has_eoes=True, is_eoes=None, eoes_from=None), TODAY)
    assert ineligible.extended_support_until is None and ineligible.why.endswith("reaches end of life today.")
    assert derive(None, TODAY, unknown_why="No version.").status == UNKNOWN
    assert near(derive(release(eol_from=TODAY + timedelta(days=180)), TODAY))
    assert not near(derive(release(eol_from=TODAY + timedelta(days=181)), TODAY))
    assert not near(derive(release(), TODAY))


@pytest.mark.parametrize(
    "version,names,cycle",
    [
        ("16.4", ["17", "16", "15"], "16"),
        ("16", ["17", "16"], "16"),
        ("9.6.24", ["10", "9.6", "9"], "9.6"),
        ("1.10.2", ["1.1", "1.10"], "1.10"),
        ("1.10", ["1.1"], ""),
        ("v3.12.1", ["3.12", "3.11"], "3.12"),
        ("22.04.3 LTS", ["22.04", "22.10"], "22.04"),
        ("2019-CU12", ["2019", "2022"], "2019"),
        ("", ["16"], ""),
        ("unknown", ["16"], ""),
    ],
)
def test_cycle_derivation_from_a_version(version, names, cycle):
    assert derive_cycle(version, names) == cycle


# ---- services and audit ------------------------------------------------------------------------------------------------


def test_tracking_validates_against_the_cached_list_and_releases(a, eol_api):
    recorded(eol_api)
    eol_services.track(a["org"], a["users"]["editor"], component(a, "Seed"), {"product": "postgresql"})
    cache.refresh()
    editor, record = a["users"]["editor"], app(a)
    with pytest.raises(FieldErrors) as refused:
        eol_services.track(a["org"], editor, record, {"product": "oracle-db-9000"})
    assert "has no product" in str(refused.value.errors["product"])
    with pytest.raises(FieldErrors) as refused:
        eol_services.track(a["org"], editor, record, {"product": "postgresql", "cycle": "99"})
    assert "Choose one of the product's release cycles" in str(refused.value.errors["cycle"])
    with pytest.raises(FieldErrors) as refused:
        eol_services.track(a["org"], editor, record, {"product": "Bad Name"})
    assert "product" in refused.value.errors
    with pytest.raises(FieldErrors) as refused:
        eol_services.track(a["org"], editor, record, {"version": "1"})
    assert "product" in refused.value.errors
    with pytest.raises(FieldErrors) as refused:
        eol_services.track(a["org"], editor, record, {"product": "postgresql", "colour": "red"})
    assert "colour" in refused.value.errors
    row = eol_services.track(a["org"], editor, record, {"product": "PG", "cycle": "16"})  # an alias, any case
    assert (row.product, row.cycle, row.cycle_explicit) == ("postgresql", "16", True)
    assert not TrackedVersion.objects.filter(resource=record).exclude(pk=row.pk).exists()


def test_writes_are_audited_with_the_record_as_related_target(a):
    editor, record = a["users"]["editor"], app(a, version="16.4")
    product("postgresql", "PostgreSQL", **{"16": {}, "15": {}})
    row = eol_services.track(a["org"], editor, record, {"product": "postgresql"})
    assert row.cycle == "16" and row.cycle_explicit is False
    eol_services.track(a["org"], editor, record, {"cycle": "15"})
    eol_services.track(a["org"], editor, record, {"cycle": "15"})  # no change: no event
    eol_services.untrack(a["org"], editor, record)
    events = list(AuditEvent.objects.filter(target_type="TrackedVersion").order_by("created_at"))
    assert [e.action for e in events] == [
        "tracked_version.created",
        "tracked_version.updated",
        "tracked_version.deleted",
    ]
    assert all(e.related_target_type == "Resource" and e.related_target_id == record.id for e in events)
    created, _updated, deleted = events
    assert created.postchange is not None and created.postchange["product"] == "postgresql"
    assert created.prechange is None
    assert events[1].changes["cycle"] == ["16", "15"] and events[1].changes["cycle_explicit"] == [False, True]
    assert deleted.prechange is not None and deleted.prechange["cycle"] == "15" and deleted.postchange is None
    # the record's History lists them
    assert AuditEvent.objects.filter(record_events(record), target_type="TrackedVersion").count() == 3


def test_an_application_version_edit_rederives_the_cycle(a):
    editor, record = a["users"]["editor"], app(a, version="15.2")
    product("postgresql", "PostgreSQL", **{"16": {}, "15": {}})
    eol_services.track(a["org"], editor, record, {"product": "postgresql"})
    assert TrackedVersion.objects.get().cycle == "15"
    services.update_resource(a["org"], editor, record, {"version": "16.1"})
    assert TrackedVersion.objects.get().cycle == "16"
    eol_services.track(a["org"], editor, record, {"version": "15.9"})  # its own version wins over the field
    services.update_resource(a["org"], editor, record, {"version": "16.2"})
    assert TrackedVersion.objects.get().cycle == "15"


def test_instances_follow_the_application_and_change_product_with_it(a):
    editor, record = a["users"]["editor"], app(a, version="16.1")
    product("postgresql", "PostgreSQL", **{"16": {}, "13": {}})
    product("mysql", "MySQL", **{"8.0": {}})
    prod = services.create_instance(a["org"], editor, record, {"name": "Production"})
    with pytest.raises(Exception, match="Track the application first"):
        eol_services.track_instance(a["org"], editor, prod, {"version": "13.4"})
    eol_services.track(a["org"], editor, record, {"product": "postgresql"})
    with pytest.raises(FieldErrors) as refused:
        eol_services.track_instance(a["org"], editor, prod, {})
    assert "version" in refused.value.errors
    row = eol_services.track_instance(a["org"], editor, prod, {"version": "13.4"})
    assert (row.product, row.cycle, row.resource_id) == ("postgresql", "13", record.id)
    eol_services.track_instance(a["org"], editor, prod, {"cycle": "13"})
    eol_services.track(a["org"], editor, record, {"product": "mysql"})
    row.refresh_from_db()
    assert (row.product, row.cycle, row.cycle_explicit) == ("mysql", "", False)  # the old product's cycle dropped
    # Deleting the instance takes its own version with it, audited.
    prod_id = prod.id
    services.delete_instance(a["org"], editor, prod)
    assert not TrackedVersion.objects.filter(instance_id=prod_id).exists()
    assert TrackedVersion.objects.filter(resource=record).count() == 1  # the application's own row stays
    assert AuditEvent.objects.filter(action="tracked_version.deleted", target_id=row.id).exists()


def test_deleting_a_tracked_record_removes_its_tracking(a, api):
    record = component(a)
    eol_services.track(a["org"], a["users"]["editor"], record, {"product": "postgresql"})
    assert api(a["users"]["admin"]).delete(f"components/{record.id}").status_code == 204
    assert not TrackedVersion.objects.exists()
    assert AuditEvent.objects.filter(action="tracked_version.deleted").count() == 1


# ---- the API ----------------------------------------------------------------------------------------------------------


def test_record_routes_roles_and_detail(a, api):
    product("postgresql", "PostgreSQL", **{"16": {"eol_from": today() + timedelta(days=700)}})
    record = app(a, version="16.3")
    viewer, editor = api(a["users"]["viewer"]), api(a["users"]["editor"])
    url = f"applications/{record.id}/end-of-life"
    assert viewer.get(url).status_code == 404
    assert viewer.get(f"applications/{record.id}").json_body["end_of_life"] is None
    assert viewer.patch(url, {"product": "postgresql"}).status_code == 403
    body = editor.patch(url, {"product": "postgresql"}).json_body
    assert body["status"] == SUPPORTED and body["cycle"] == "16" and body["cycle_source"] == "derived"
    assert body["version"] == "16.3" and body["version_source"] == "application" and body["days_remaining"] == 700
    assert body["instances"] == [] and body["record"] == {
        "id": str(record.id),
        "name": "Billing DB",
        "kind": "application",
        "criticality": None,
        "business_owner": None,
        "technical_owner": None,
    }
    assert viewer.get(url).json_body == body
    detail = viewer.get(f"applications/{record.id}").json_body["end_of_life"]
    assert detail["status"] == SUPPORTED and detail["product_label"] == "PostgreSQL"
    assert editor.patch(url, {"cycle": "nope"}).status_code == 400
    assert viewer.delete(url).status_code == 403
    assert editor.delete(url).status_code == 204 and editor.delete(url).status_code == 404
    # components have the same route; an application's id there (and the reverse) is 404
    job = component(a)
    assert editor.patch(f"components/{job.id}/end-of-life", {"product": "postgresql", "cycle": "16"}).status_code == 200
    assert editor.get(f"components/{record.id}/end-of-life").status_code == 404
    assert editor.get(f"applications/{job.id}/end-of-life").status_code == 404
    assert viewer.get(f"components/{job.id}").json_body["end_of_life"]["cycle"] == "16"


def test_instance_routes(a, api):
    product("postgresql", "PostgreSQL", **{"16": {}, "13": {"eol_from": today() - timedelta(days=5)}})
    record = app(a, version="16.1")
    prod = services.create_instance(a["org"], a["users"]["editor"], record, {"name": "Production"})
    editor = api(a["users"]["editor"])
    url = f"applications/{record.id}/instances/{prod.id}/end-of-life"
    assert editor.patch(url, {"version": "13.9"}).status_code == 409
    editor.patch(f"applications/{record.id}/end-of-life", {"product": "postgresql"})
    body = editor.patch(url, {"version": "13.9"}).json_body
    assert body["status"] == END_OF_LIFE and body["instance"] == {"id": str(prod.id), "name": "Production"}
    assert body["days_remaining"] == -5
    detail = editor.get(f"applications/{record.id}/end-of-life").json_body
    assert [r["instance"]["name"] for r in detail["instances"]] == ["Production"]
    assert api(a["users"]["viewer"]).patch(url, {"version": "1"}).status_code == 403
    assert editor.delete(url).status_code == 204 and editor.get(url).status_code == 404


def overview_fixture(a):
    t = today()
    product(
        "postgresql",
        "PostgreSQL",
        **{
            "17": {"eol_from": t + timedelta(days=1000)},
            "15": {"eol_from": t + timedelta(days=90)},
            "12": {"eol_from": t - timedelta(days=30), "is_eol": True},
        },
    )
    product("ubuntu", "Ubuntu", **{"22.04": {"eol_from": t + timedelta(days=500), "has_eoas": True, "is_eoas": True}})
    editor = a["users"]["editor"]
    rows = {}
    for name, prod, cycle, kind in (
        ("Old DB", "postgresql", "12", "application"),
        ("Soon DB", "postgresql", "15", "application"),
        ("New DB", "postgresql", "17", "component"),
        ("Server", "ubuntu", "22.04", "component"),
        ("Mystery", "postgresql", "", "application"),
    ):
        record = app(a, name) if kind == "application" else component(a, name)
        rows[name] = eol_services.track(a["org"], editor, record, {"product": prod, "cycle": cycle or None})
    retired = app(a, "Retired DB")
    eol_services.track(a["org"], editor, retired, {"product": "postgresql", "cycle": "12"})
    Resource.objects.filter(pk=retired.pk).update(lifecycle="retired")
    archived = app(a, "Archived DB")
    eol_services.track(a["org"], editor, archived, {"product": "postgresql", "cycle": "12"})
    services.archive_resource(a["org"], editor, archived)
    return rows


def test_the_overview_lists_live_records_soonest_first_with_filters(a, api):
    overview_fixture(a)
    viewer = api(a["users"]["viewer"])
    body = viewer.get("end-of-life").json_body
    names = [r["record"]["name"] for r in body["results"]]
    assert names == ["Old DB", "Soon DB", "Server", "New DB", "Mystery"]  # retired and archived left out
    assert body["counts"] == {"supported": 2, "security_only": 1, "end_of_life": 1, "unknown": 1}
    assert body["today"] == org_today(a["org"]).isoformat() and body["cache"]["enabled"] is True
    assert [
        r["record"]["name"] for r in viewer.get("end-of-life?status=end_of_life,security_only").json_body["results"]
    ] == [
        "Old DB",
        "Server",
    ]
    assert [r["record"]["name"] for r in viewer.get("end-of-life?days=180").json_body["results"]] == [
        "Old DB",
        "Soon DB",
    ]
    assert [r["record"]["name"] for r in viewer.get("end-of-life?product=ubuntu").json_body["results"]] == ["Server"]
    assert {r["record"]["name"] for r in viewer.get("end-of-life?kind=component").json_body["results"]} == {
        "New DB",
        "Server",
    }
    bad = viewer.get("end-of-life?status=dead&days=-1&kind=vendor&colour=red")
    assert bad.status_code == 400 and set(bad.json_body["errors"]) == {"status", "days", "kind", "colour"}


def test_the_csv_export_is_audited_and_restricted_while_impersonating(a, api):
    overview_fixture(a)
    response = api(a["users"]["viewer"]).client.get("/api/v1/end-of-life/export.csv?status=end_of_life")
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    text = response.content.decode("utf-8-sig").splitlines()
    assert text[0].startswith(
        "record,record_kind,record_id,criticality,business_owner,technical_owner,instance,instance_id,product"
    )
    assert len(text) == 2
    assert text[1].startswith("Old DB,application,") and ",end_of_life," in text[1]
    event = AuditEvent.objects.get(action="export")
    assert event.changes["kind"] == "tracked_version" and event.changes["rows"] == 1
    assert event.changes["filters"] == {"status": ["end_of_life"]}
    assert ("GET", "api/v1/end-of-life/export.csv") in RESTRICTED_ROUTES
    assert ("PATCH", "api/v1/applications/<uuid:pk>/end-of-life") in ALLOWED_WRITES


def test_product_search_and_detail(a, api, eol_api):
    recorded(eol_api)
    eol_services.track(a["org"], a["users"]["editor"], app(a), {"product": "postgresql"})
    cache.refresh()
    viewer = api(a["users"]["viewer"])
    found = viewer.get("end-of-life/products?search=post").json_body
    assert found[0]["name"] == "postgresql" and found[0]["releases_cached"] is True and found[0]["tracked"] is True
    assert [p["name"] for p in viewer.get("end-of-life/products?search=pg").json_body] == ["postgresql"]  # an alias
    assert len(viewer.get("end-of-life/products").json_body) == 10
    ubuntu = viewer.get("end-of-life/products?search=ubu").json_body[0]
    assert ubuntu["name"] == "ubuntu" and ubuntu["releases_cached"] is None and ubuntu["tracked"] is False
    detail = viewer.get("end-of-life/products/postgresql").json_body
    assert detail["label"] == "PostgreSQL" and len(detail["releases"]) == 32
    assert detail["releases"][0]["name"] == "18" and detail["releases"][-1]["status"] == END_OF_LIFE
    assert viewer.get("end-of-life/products/nothing-here").status_code == 404


def test_without_the_capability_everything_is_refused_or_empty(a, api, monkeypatch):
    overview_fixture(a)
    record = Resource.objects.get(name="Old DB")
    monkeypatch.setattr(capabilities, "capabilities", lambda org: capabilities.COMMUNITY - {capabilities.EOL})
    viewer = api(a["users"]["viewer"])
    for url in ("end-of-life", "end-of-life/products", f"applications/{record.id}/end-of-life"):
        response = viewer.get(url)
        assert response.status_code == 403 and response.json_body["capability"] == "eol", url
    assert viewer.get(f"applications/{record.id}").json_body["end_of_life"] is None
    queues, _ = attention_queues(a["org"], a["users"]["admin"], today())
    assert queues["end_of_life"] == []
    assert reminders.facts(a["org"], today()) == []


def test_an_unknown_status_says_whether_the_installation_or_the_record_is_why(a, api, settings):
    """unknown_reason (SPA review): a failed download, data not downloaded yet and downloads off are the
    installation's; a cycle endoflife.date doesn't list, a version that matches no cycle and no version are the
    record's."""
    product("postgresql", "PostgreSQL", **{"16": {"eol_from": today() + timedelta(days=300)}})
    editor = a["users"]["editor"]
    cases = {
        "Unlisted cycle": ("postgresql", "16", ""),
        "Odd version": ("postgresql", "", "42.1"),
        "No version": ("postgresql", "", ""),
        "Broken": ("broken", "", "1.0"),
        "Later": ("later", "", "1.0"),
    }
    EolProduct.objects.create(name="broken", label="Broken")
    EolProduct.objects.create(name="later", label="Later")
    for name, (prod, cycle, version) in cases.items():
        eol_services.track(a["org"], editor, app(a, name, version=version), {"product": prod, "cycle": cycle or None})
    # A cycle endoflife.date no longer lists (as after a refresh dropped it).
    TrackedVersion.objects.filter(resource__name="Unlisted cycle").update(cycle="99")
    # A download that failed: checked, never fetched (eol/cache._product_error).
    EolProduct.objects.filter(name="broken").update(error="HTTP 503", releases_checked_at=timezone.now())
    rows = {r["record"]["name"]: r for r in api(a["users"]["viewer"]).get("end-of-life").json_body["results"]}
    assert {n: rows[n]["unknown_reason"] for n in cases} == {
        "Unlisted cycle": "cycle_not_listed",
        "Odd version": "no_version_match",
        "No version": "no_version",
        "Broken": "download_failed",
        "Later": "not_downloaded",
    }
    assert rows["Broken"]["checked_at"] is not None and "couldn't be read: HTTP 503" in rows["Broken"]["why"]
    panel = api(a["users"]["viewer"]).get(f"applications/{Resource.objects.get(name='Broken').id}").json_body
    assert panel["end_of_life"]["unknown_reason"] == "download_failed"
    settings.SOFTWARDEN_EOL_ENABLED = False
    rows = {r["record"]["name"]: r for r in api(a["users"]["viewer"]).get("end-of-life").json_body["results"]}
    assert rows["Later"]["unknown_reason"] == "downloads_off"
    tracked = app(a, "Known", version="16.2")
    eol_services.track(a["org"], editor, tracked, {"product": "postgresql"})
    rows = {r["record"]["name"]: r for r in api(a["users"]["viewer"]).get("end-of-life").json_body["results"]}
    assert rows["Known"]["status"] == SUPPORTED and rows["Known"]["unknown_reason"] is None


def test_the_overview_carries_criticality_and_owners_and_filters_by_them(a, api):
    overview_fixture(a)
    dana, priya = a["people"]["dana"], a["people"]["priya"]
    Resource.objects.filter(name="Old DB").update(criticality="critical", business_owner=dana)
    Resource.objects.filter(name="Soon DB").update(criticality="high", technical_owner=dana)
    Resource.objects.filter(name="Server").update(technical_owner=priya)
    viewer = api(a["users"]["viewer"])
    rows = {r["record"]["name"]: r["record"] for r in viewer.get("end-of-life").json_body["results"]}
    assert rows["Old DB"]["criticality"] == "critical" and rows["Server"]["criticality"] is None
    assert rows["Old DB"]["business_owner"] == {"id": str(dana.id), "name": "Dana Ortiz"}
    assert rows["Old DB"]["technical_owner"] is None
    names = lambda q: [r["record"]["name"] for r in viewer.get(f"end-of-life?{q}").json_body["results"]]  # noqa: E731
    assert names("criticality=critical,high") == ["Old DB", "Soon DB"]
    assert names("criticality=none") == ["Server", "New DB", "Mystery"]
    assert names(f"owner={dana.id}") == ["Old DB", "Soon DB"]
    assert names(f"owner={priya.id}&criticality=high") == []
    bad = viewer.get("end-of-life?criticality=urgent&owner=nobody")
    assert bad.status_code == 400 and set(bad.json_body["errors"]) == {"criticality", "owner"}
    response = viewer.client.get(f"/api/v1/end-of-life/export.csv?owner={dana.id}")
    lines = response.content.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("record,record_kind,record_id,criticality,business_owner,technical_owner,instance")
    assert len(lines) == 3 and ",critical,Dana Ortiz,," in lines[1]


def test_a_days_window_keeps_end_of_life_with_no_date(a, api):
    product("legacy", "Legacy", **{"1": {"is_eol": True}, "2": {"eol_from": today() + timedelta(days=500)}})
    editor = a["users"]["editor"]
    eol_services.track(a["org"], editor, app(a, "Old one"), {"product": "legacy", "cycle": "1"})
    eol_services.track(a["org"], editor, app(a, "New one"), {"product": "legacy", "cycle": "2"})
    viewer = api(a["users"]["viewer"])
    for days in (0, 90):
        found = viewer.get(f"end-of-life?days={days}").json_body["results"]
        assert [(r["record"]["name"], r["status"], r["eol_date"]) for r in found] == [("Old one", END_OF_LIFE, None)]


def test_the_digest_has_no_end_of_life_section(a):
    """End of life left the digest when its reminders arrived (notify 0012): the built-in End of
    life rule emails each version, so the digest would repeat it. The queue stays on the page."""
    from notify import digest

    overview_fixture(a)
    editor = a["users"]["editor"]
    assert "end_of_life" not in SECTION_NAMES and "end_of_life" in digest.NOT_SECTIONS
    items, _ = digest.plan(a["org"], today())
    entries = items.get(editor.id, [])
    assert not [e for e in entries if e[0] == "end_of_life"]
    if entries:
        _, text, html = digest.render(a["org"], editor, entries, today())
        assert "END OF LIFE" not in text and "#h-eol" not in text and "#h-eol" not in html  # Old DB is a Review


# ---- Needs attention, data quality, reminders ---------------------------------------------------------------------------


def test_needs_attention_lists_end_of_life_and_near_versions_counted_not_in_the_digest(a, api):
    rows = overview_fixture(a)
    Resource.objects.filter(name="Soon DB").update(it_steward=None)
    body = api(a["users"]["admin"]).get("needs-attention?scope=all").json_body
    found = body["end_of_life"]
    assert [r["record"]["name"] for r in found] == ["Old DB", "Soon DB"]
    old = found[0]
    assert old["id"] == str(rows["Old DB"].id) and old["status"] == END_OF_LIFE and old["days_remaining"] == -30
    assert old["steward"]["email"] == a["users"]["editor"].email and old["kind"] == "end_of_life"
    assert found[1]["steward"]["coordinator"] is True  # no steward: the coordinator catches it
    # Counted since the EOL calendar's SPA package; not a digest section since its reminders (notify 0012).
    assert "end_of_life" not in UNCOUNTED and "end_of_life" not in PENDING_SECTIONS
    assert "end_of_life" not in SECTION_NAMES and "end_of_life" not in [q for q, _ in SECTIONS]
    queues = {k: v for k, v in body.items() if isinstance(v, list) and k not in UNCOUNTED}
    assert "end_of_life" in queues and body["counts"]["all"] == sum(len(v) for v in queues.values())
    assert len(eol_attention.attention_rows(a["org"], today())) == 2


def test_the_data_quality_rule_flags_a_tracked_version_with_no_known_cycle(a):
    product("postgresql", "PostgreSQL", **{"16": {}})
    editor = a["users"]["editor"]
    good, bad, pending = app(a, "Good", version="16.1"), app(a, "Bad", version="99.1"), component(a, "Pending")
    for record in (good, bad):
        eol_services.track(a["org"], editor, record, {"product": "postgresql"})
    EolProduct.objects.create(name="notcachedyet", label="Listed, releases not fetched")
    eol_services.track(a["org"], editor, pending, {"product": "notcachedyet"})  # no data yet: not the record's gap

    def failing(record):
        return [r.id for r in quality.evaluate(a["org"], [record.id])[record.id].failing if r.id == "eol_cycle_unknown"]

    assert failing(good) == [] and failing(bad) == ["eol_cycle_unknown"] and failing(pending) == []
    untracked = quality.evaluate(a["org"], [a["app"].id])[a["app"].id]
    assert "eol_cycle_unknown" not in [r.id for r in untracked.failing]
    assert quality.BY_ID["eol_cycle_unknown"].spec()["applies"] == {"field": "end_of_life", "test": "is", "value": True}


def test_reminder_facts_are_ready_for_the_generator(a):
    rows = overview_fixture(a)
    facts = {f.title: f for f in reminders.facts(a["org"], today())}
    assert set(facts) == {"Old DB", "Soon DB", "New DB", "Server"}  # Mystery has no date; retired, archived left out
    old = facts["Old DB"]
    record = Resource.objects.get(name="Old DB").id
    assert old.source_type == "end_of_life" and old.due_date == today() - timedelta(days=30)
    assert old.label == "End of life: PostgreSQL 12" and old.resource_id == record
    # Keyed by the record, one occurrence per tracked version (notify 0012).
    assert old.source_id == record and old.source_field == f"tracked_version:{rows['Old DB'].id}"


# ---- tenancy --------------------------------------------------------------------------------------------------------------


def test_another_organizations_records_and_rows_are_404(a, b, api):
    product("postgresql", "PostgreSQL", **{"16": {}})
    theirs = app(b, "Their DB", version="16.1")
    instance = services.create_instance(b["org"], b["users"]["editor"], theirs, {"name": "Production"})
    eol_services.track(b["org"], b["users"]["editor"], theirs, {"product": "postgresql"})
    client = api(a["users"]["admin"])
    before = AuditEvent.objects.count()
    for method in ("get", "patch", "delete"):
        for url in (
            f"applications/{theirs.id}/end-of-life",
            f"applications/{theirs.id}/instances/{instance.id}/end-of-life",
            f"applications/{a['app'].id}/instances/{instance.id}/end-of-life",  # our app, their instance
        ):
            response = (
                getattr(client, method)(url, {"product": "postgresql"})
                if method == "patch"
                else getattr(client, method)(url)
            )
            assert response.status_code == 404, (method, url)
    assert AuditEvent.objects.count() == before and TrackedVersion.objects.count() == 1
    assert client.get("end-of-life").json_body["results"] == []
    assert client.get("needs-attention?scope=all").json_body["end_of_life"] == []


def _rejected(write):
    with pytest.raises(IntegrityError), transaction.atomic():
        write()
        connection.check_constraints()


def test_the_database_keeps_a_tracked_version_in_its_organization(a, b):
    theirs = app(b, "Their DB")
    _rejected(lambda: TrackedVersion.objects.create(organization=a["org"], resource=theirs, product="postgresql"))
    # an instance of another application of the same organization: the three-column key refuses it
    other = app(a, "Other")
    instance = services.create_instance(a["org"], a["users"]["editor"], other, {"name": "Production"})
    _rejected(
        lambda: TrackedVersion.objects.create(
            organization=a["org"], resource=a["app"], instance=instance, product="postgresql"
        )
    )


def test_tracked_versions_are_exported_and_the_cache_is_not():
    labels = {m._meta.label for m in org_lifecycle.tenant_models()}
    assert "eol.TrackedVersion" in labels
    assert not labels & {"eol.EolProduct", "eol.EolRelease", "eol.EolSyncState"}


# ---- review follow-ups -----------------------------------------------------------------------------------------------


def test_what_another_organization_tracks_isnt_shown(a, b, api, eol_api):
    """The cache is shared: org b sees no cached state, check time, error or cycles for a product only a tracks, and
    a product row that exists only because a tracks it (not in the list) is a 404 and absent from b's search."""
    fake = recorded(eol_api)
    fake.routes["/products/in-house-erp"] = Reply(500, {}, b"")
    eol_services.track(a["org"], a["users"]["editor"], app(a), {"product": "postgresql"})
    cache.refresh()
    EolProduct.objects.create(name="in-house-erp", listed=False, error="HTTP 500")
    TrackedVersion.objects.create(organization=a["org"], resource=component(a), product="in-house-erp")
    theirs = api(b["users"]["viewer"])
    listed = {p["name"]: p for p in theirs.get("end-of-life/products").json_body}
    assert listed["postgresql"]["releases_cached"] is None and listed["postgresql"]["renamed_to"] is None
    assert "in-house-erp" not in listed
    assert theirs.get("end-of-life/products?search=in-house").json_body == []
    detail = theirs.get("end-of-life/products/postgresql").json_body
    assert detail["releases"] == [] and detail["checked_at"] is None and detail["error"] is None
    assert theirs.get("end-of-life/products/in-house-erp").status_code == 404
    ours = api(a["users"]["viewer"])
    assert ours.get("end-of-life/products/in-house-erp").json_body["error"] == "HTTP 500"
    assert len(ours.get("end-of-life/products/postgresql").json_body["releases"]) == 32


@pytest.mark.parametrize("shape", ["not an object", "impossible timestamp"])
def test_one_malformed_product_answer_doesnt_stop_the_refresh(a, eol_api, shape):
    fake = recorded(eol_api)
    fake.routes["/products/ubuntu"] = ok("ubuntu", '"U1"')
    editor = a["users"]["editor"]
    eol_services.track(a["org"], editor, app(a), {"product": "postgresql"})
    eol_services.track(a["org"], editor, component(a), {"product": "ubuntu"})
    if shape == "not an object":
        fake.routes["/products/postgresql"] = Reply(200, {"etag": '"X"'}, b"[1, 2, 3]")
    else:
        document = json.loads(fixture("postgresql"))
        document["last_modified"] = "2026-13-45T25:00:00+00:00"
        fake.routes["/products/postgresql"] = Reply(200, {"etag": '"X"'}, json.dumps(document).encode())
    counts = cache.refresh()
    ubuntu, pg = EolProduct.objects.get(name="ubuntu"), EolProduct.objects.get(name="postgresql")
    assert ubuntu.releases.count() == 45  # the other product still refreshed
    if shape == "not an object":
        assert counts["failed"] == 1 and counts["updated"] == 1 and pg.error == "the product has an unexpected shape"
    else:
        assert counts["updated"] == 2 and pg.releases.count() == 32 and pg.source_modified_at is None


def test_a_malformed_list_keeps_the_cache_and_still_refreshes_products(a, eol_api):
    fake = recorded(eol_api)
    eol_services.track(a["org"], a["users"]["editor"], app(a), {"product": "postgresql"})
    cache.refresh()
    fake.routes["/products"] = Reply(200, {"etag": '"L2"'}, b'{"result": "nope"}')
    counts = cache.refresh()
    assert counts["unchanged"] == 1 and EolProduct.objects.filter(listed=True).count() == 10
    assert EolSyncState.objects.get().last_error == "product list: the product list has an unexpected shape"


def test_the_import_refuses_a_malformed_file_in_words_and_stores_nothing(a, tmp_path, monkeypatch):
    path = tmp_path / "eol.json"
    path.write_text("[1, 2]", encoding="utf-8")  # an array, not an API document
    with pytest.raises(CommandError, match="Not an endoflife.date v1 document"):
        call_command("eol_import", str(path), stdout=io.StringIO())
    document = json.loads(fixture("postgresql"))
    document["last_modified"] = "2026-13-45T25:00:00+00:00"  # impossible: imported, with no modified time
    path.write_text(json.dumps(document), encoding="utf-8")
    call_command("eol_import", str(path), stdout=io.StringIO())
    assert EolProduct.objects.get(name="postgresql").source_modified_at is None
    path.write_text(json.dumps(full_document()), encoding="utf-8")

    def broken(item, position):
        raise TypeError("unexpected")

    monkeypatch.setattr(cache, "parse_release", broken)
    with pytest.raises(CommandError, match="couldn't be read as endoflife.date data"):
        call_command("eol_import", str(path), stdout=io.StringIO())
    assert not EolProduct.objects.filter(name="ubuntu").exists()  # all or nothing


@pytest.mark.parametrize("cached", [False, True])
def test_a_product_change_clears_an_explicit_cycle_the_new_product_doesnt_list(a, cached):
    product("postgresql", "PostgreSQL", **{"16": {}})
    if cached:
        product("mysql", "MySQL", **{"8.0": {}, "16": {}})
    editor, record = a["users"]["editor"], app(a, version="8.0.36")
    prod = services.create_instance(a["org"], editor, record, {"name": "Production"})
    eol_services.track(a["org"], editor, record, {"product": "postgresql", "cycle": "16"})
    instance_row = eol_services.track_instance(a["org"], editor, prod, {"cycle": "16"})
    row = eol_services.track(a["org"], editor, record, {"product": "mysql"})
    instance_row.refresh_from_db()
    if cached:  # the new product's cached releases list "16": kept, explicitly
        assert (row.cycle, row.cycle_explicit) == ("16", True)
        assert (instance_row.cycle, instance_row.cycle_explicit) == ("16", True)
    else:  # not cached: cleared and derived (nothing to derive from yet)
        assert (row.cycle, row.cycle_explicit) == ("", False)
        assert (instance_row.cycle, instance_row.cycle_explicit) == ("", False)


def test_rederive_streams_in_chunks_and_never_overwrites_an_explicit_cycle(a, monkeypatch):
    product("postgresql", "PostgreSQL", **{"16": {}, "15": {}})
    monkeypatch.setattr(cache, "REDERIVE_CHUNK", 2)
    monkeypatch.setattr(cache, "REDERIVE_BATCH", 2)
    rows = [
        TrackedVersion.objects.create(
            organization=a["org"], resource=component(a, f"Job {i}"), product="postgresql", version="16.1"
        )
        for i in range(5)
    ]
    assert cache.rederive(["postgresql"]) == 5
    assert set(TrackedVersion.objects.values_list("cycle", flat=True)) == {"16"}
    # A person set an explicit cycle after the run read the row: the run's write leaves it alone.
    TrackedVersion.objects.filter(pk=rows[0].pk).update(cycle="15", cycle_explicit=True)
    assert cache._write_cycles({"16": [rows[0].pk]}) == 0
    assert TrackedVersion.objects.get(pk=rows[0].pk).cycle == "15"


def test_a_renamed_product_keeps_its_releases_and_notes_the_new_name(a, api, eol_api):
    eol_api(
        {
            "/products/postgres-old": Reply(301, {"location": "/api/v1/products/postgresql"}, b""),
            "/products/postgresql": ok("postgresql", '"P1"'),
        }
    )
    eol_services.track(a["org"], a["users"]["editor"], component(a), {"product": "postgres-old", "cycle": "16"})
    cache.refresh(["postgres-old"])
    old = EolProduct.objects.get(name="postgres-old")
    assert old.renamed_to == "postgresql" and old.releases.count() == 32
    body = api(a["users"]["viewer"]).get("end-of-life").json_body["results"][0]
    assert body["product_renamed_to"] == "postgresql" and body["cycle"] == "16"
    assert api(a["users"]["viewer"]).get("end-of-life/products/postgres-old").json_body["renamed_to"] == "postgresql"


def test_a_first_create_that_loses_a_race_is_a_409(a):
    record = component(a)
    TrackedVersion.objects.create(organization=a["org"], resource=record, product="postgresql")
    late = TrackedVersion(organization=a["org"], resource=record, product="mysql")
    with pytest.raises(Conflict, match="Reload it and try again"):
        eol_services._save(late, True)
    assert TrackedVersion.objects.get(resource=record).product == "postgresql"
