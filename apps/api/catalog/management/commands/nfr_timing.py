"""Time the performance-sensitive endpoints against a running dev server (10 concurrent workers). Reads, except the
new-assessment check, which creates assessments (and their snapshots) in the synthetic scale organization."""

import http.cookiejar
import json
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from django.core.management.base import BaseCommand, CommandError

from .seed_scale import ADMIN_EMAIL, PASSWORD

LIST_DETAIL_P95_MS = 300
NEEDS_ATTENTION_P95_MS = 500
RENEWALS_P95_MS = 500  # 300 renewals and about 1,000 cycles added to the dependency size
DIGEST_RUN_S = 30  # one organization's digest run, built and handed to the mail backend
DISCOVERY_P95_MS = 500  # the candidates list (default status) with 2,000 observations
DISCOVERY_RUN_S = 60  # a 2,000-object simulated run
DISCOVERY_RUN_OBJECTS = 2000
EXPLORER_P95_MS = 500  # depth All at 1,000 resources, 2,000 relationships and 300 integrations
NEW_ASSESSMENT_P95_MS = 1000  # a new assessment, which computes and saves its snapshot, at the same size
ASSESSMENT_DETAIL_P95_MS = (
    1500  # no stated target: GET of a hub's assessment (about 1,200 nodes, 2.4 MB); one request takes about 80 ms
)
HUBS = 10
DATA_QUALITY_P95_MS = 500  # data quality: the summary (dashboard panel) and the rules list, every live record
SEARCH_TERMS = ["payroll", "zylker ledger", "helpdesk", "contoso", "sync", "portal", "badge", "reprt"]
GLOBAL_SEARCH_P95_MS = 300  # A vendor name returns applications and agents (and contracts)
# Global search: company names shared by vendors, AI providers and record names (applications, agents and their
# models match), a full vendor name, the broadest word (every vendor), palette prefixes (one letter: the first
# keystroke) and a typo (the fallback).
GLOBAL_SEARCH_TERMS = ["contoso", "fabrikam", "northwind", "litware", "{vendor}", "software", "zylk", "contso", "a"]


def digest_run_seconds(org):
    """Build and send one digest run for the organization in process, through the in-memory mail backend, then remove
    what it wrote (the run, its digests and the reminders it fired) so the check can be repeated."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from django.test import override_settings

    from notify import digest
    from notify.models import ReminderDispatch

    zone = ZoneInfo(org.timezone)
    today = datetime.now(zone).date()
    digest.discard_day(org, today)
    with override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", SOFTWARDEN_EMAIL_CONFIGURED=True
    ):
        start = time.perf_counter()
        run = digest.run_for_org(org, datetime.combine(today, org.digest_time, tzinfo=zone) + timedelta(minutes=1))
        digest.send_now(org, today, datetime.now(zone) + timedelta(days=1))
        seconds = time.perf_counter() - start
    if run is not None:
        ReminderDispatch.objects.filter(digest__run=run).delete()
        digest.discard_day(org, today)
    return seconds


def discovery_run_seconds(org, actor):
    """Run a throwaway Entra connector on the simulated tenant with 2,000 generated apps in process (the worker runs
    connectors, not a request), which is the first-run insert path, then remove it with its observations."""
    from core import connections
    from discovery import engine as discovery
    from discovery.models import Connector, RunStatus

    connector = discovery.create_connector(
        org,
        actor,
        {"name": "NFR timing run (synthetic)", "tenant_id": "simulated", "secret": "synthetic-timing-secret"},
    )
    try:
        connector.config = {**connector.config, "simulated_size": DISCOVERY_RUN_OBJECTS}
        connector.save(update_fields=["config"])
        connections.run_test(org, actor, connector.microsoft_connection)  # a run needs a tested connection
        start = time.perf_counter()
        run = discovery.run(connector, requested_by=actor)
        seconds = time.perf_counter() - start
        ok = run is not None and run.status == RunStatus.SUCCEEDED
    finally:
        discovery.delete_connector(org, actor, Connector.objects.get(id=connector.id))
        connections.delete_connection(org, actor, connector.microsoft_connection)
    return seconds, ok


def percentile(values, pct):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(pct / 100 * len(ordered) + 0.5) - 1))]


class Command(BaseCommand):
    help = (
        "Time the application list, component list, integration list, search, global search (vendor and company names, "
        "p95 < 300 ms), application detail, Needs attention (All) and "
        "dependency explorer endpoints with concurrent workers against a RUNNING server (default http://127.0.0.1:8010; start it with "
        "'manage.py runserver 127.0.0.1:8010' after 'seed_scale'). Signs in once and shares the session across "
        "workers. Reports p50/p95 and PASS/FAIL against list/detail p95 < 300 ms, Needs attention p95 < 500 ms and "
        "explorer p95 < 500 ms (the ten most linked records, depth All, both directions, instances shown) and "
        "new-assessment p95 < 1 s (POST /assessments on the ten most linked records; this one WRITES "
        "--assessments assessments, with snapshots, into the scale organization) and assessment detail p95 < 1.5 s "
        "(GET of those, snapshot rows and link states included), discovery candidates p95 < 500 ms, and, in process, a "
        "2,000-object simulated discovery run < 60 s (a throwaway connector, deleted afterwards). "
        "Also the audit log at 50,000 events (first page, filtered, next page, a record's history, one event's diff), "
        "list/detail p95 < 300 ms; the data-quality summary and rules p95 < 500 ms, and records failing a check and "
        "the application list filtered and ordered by completeness p95 < 300 ms. Exits non-zero on FAIL only with "
        "--strict."
    )

    def add_arguments(self, parser):
        parser.add_argument("--base-url", default="http://127.0.0.1:8010")
        parser.add_argument("--email", default=ADMIN_EMAIL)
        parser.add_argument("--password", default=PASSWORD)
        parser.add_argument("--workers", type=int, default=10)
        parser.add_argument("--requests", type=int, default=200, help="Requests per endpoint (default 200).")
        parser.add_argument(
            "--assessments", type=int, default=30, help="Assessments to create for the new-assessment check."
        )
        parser.add_argument("--strict", action="store_true", help="Exit non-zero if any target fails.")

    def handle(self, *args, base_url, email, password, workers, requests, assessments, strict, **options):
        from core import simulation

        # Its discovery run timing uses the simulated tenant: refuse before timing anything.
        if not simulation.allowed():
            raise CommandError(simulation.command_refused("nfr_timing"))
        base = base_url.rstrip("/") + "/api/v1/"
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

        def call(path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"}
            token = next((c.value for c in jar if c.name == "csrftoken"), None)
            if token and path != "auth/login":
                headers["X-CSRFToken"] = token  # session writes need the CSRF token
            req = urllib.request.Request(base + path, data=data, headers=headers)  # noqa: S310 (operator-supplied URL)
            with opener.open(req, timeout=30) as response:
                return json.loads(response.read() or "null")

        try:
            call("auth/csrf")
            call("auth/login", {"email": email, "password": password})
            call("auth/csrf")  # a new session gets a new token
            first_page = call("applications?page_size=200")["results"]
        except (urllib.error.URLError, OSError) as exc:
            raise CommandError(f"Could not sign in at {base}: {exc}. Is the server running and seeded?") from exc
        if not first_page:
            raise CommandError("No applications found. Run 'manage.py seed_scale' first.")
        ids = [r["id"] for r in first_page]
        # The explorer's worst cases: the most linked records (the busiest hubs reach the most of the graph).
        degree: dict = {}
        for rel in call("relationships"):
            for end in ("source", "target"):
                degree[rel[end]["id"]] = degree.get(rel[end]["id"], 0) + 1
        hubs = sorted(degree, key=lambda i: -degree[i])[:HUBS]
        vendor_name = next((v["name"] for v in call("vendors")), "contoso")  # a full vendor name for global search

        # (label, p95 target in ms, path for request i, JSON body for request i or None for GET, request count)
        plans: list[tuple] = [
            ("application list", LIST_DETAIL_P95_MS, lambda i: "applications", None, requests),
            ("component list", LIST_DETAIL_P95_MS, lambda i: "components", None, requests),
            ("integration list", LIST_DETAIL_P95_MS, lambda i: "integrations", None, requests),
            (
                "search",
                LIST_DETAIL_P95_MS,
                lambda i: f"applications?q={SEARCH_TERMS[i % len(SEARCH_TERMS)].replace(' ', '%20')}",
                None,
                requests,
            ),
            (
                "global search",
                GLOBAL_SEARCH_P95_MS,
                lambda i: (
                    "search?q="
                    + urllib.parse.quote(GLOBAL_SEARCH_TERMS[i % len(GLOBAL_SEARCH_TERMS)].format(vendor=vendor_name))
                ),
                None,
                requests,
            ),
            ("application detail", LIST_DETAIL_P95_MS, lambda i: f"applications/{ids[i % len(ids)]}", None, requests),
            ("needs attention (all)", NEEDS_ATTENTION_P95_MS, lambda i: "needs-attention?scope=all", None, requests),
            ("renewals list", RENEWALS_P95_MS, lambda i: "renewals", None, requests),
            ("discovery candidates", DISCOVERY_P95_MS, lambda i: "discovery/candidates", None, requests),
            # Data quality: one grouped aggregate over every live record; failing records and the score as
            # a list filter and ordering
            ("data quality summary", DATA_QUALITY_P95_MS, lambda i: "data-quality/summary", None, requests),
            ("data quality rules", DATA_QUALITY_P95_MS, lambda i: "data-quality/rules", None, requests),
            (
                "data quality failures",
                LIST_DETAIL_P95_MS,
                lambda i: "data-quality/failures?rule=data_classification",
                None,
                requests,
            ),
            (
                "completeness list",
                LIST_DETAIL_P95_MS,
                lambda i: "applications?completeness__lt=100&ordering=completeness",
                None,
                requests,
            ),
        ]
        # The audit log at 50,000 events: the first page, filtered pages, a record's History and
        # one event with its diff. The filters rotate over the kinds the log offers.
        log = call("audit-events")["results"]
        if log:
            events = [e["id"] for e in log]
            requests_ids = [e["request_id"] for e in log if e["request_id"]] or [events[0]]
            filtered = [
                lambda i: "audit-events?action=resource.updated",
                lambda i: "audit-events?action=user&from=2026-01-01",
                lambda i: f"audit-events?target={ids[i % len(ids)]}",
                lambda i: f"audit-events?related_target={ids[i % len(ids)]}",
                lambda i: f"audit-events?target_type=RecordReview&to={time.strftime('%Y-%m-%d')}",
                lambda i: f"audit-events?request_id={requests_ids[i % len(requests_ids)]}",
            ]
            plans += [
                ("audit log", LIST_DETAIL_P95_MS, lambda i: "audit-events", None, requests),
                ("audit log filtered", LIST_DETAIL_P95_MS, lambda i: filtered[i % len(filtered)](i), None, requests),
                (
                    "audit log next page",
                    LIST_DETAIL_P95_MS,
                    lambda i: f"audit-events?cursor={events[-1]}",
                    None,
                    requests,
                ),
                (
                    "record history",
                    LIST_DETAIL_P95_MS,
                    lambda i: f"applications/{ids[i % len(ids)]}/audit",
                    None,
                    requests,
                ),
                (
                    "audit event diff",
                    LIST_DETAIL_P95_MS,
                    lambda i: f"audit-events/{events[i % len(events)]}",
                    None,
                    requests,
                ),
            ]
        if hubs:
            plans.append(
                (
                    "dependencies (all)",
                    EXPLORER_P95_MS,
                    lambda i: (
                        f"dependencies?target={hubs[i % len(hubs)]}&direction=both&depth=all"
                        "&include_retired=true&show_instances=true"
                    ),
                    None,
                    requests,
                )
            )
            plans.append(
                (
                    "new assessment",
                    NEW_ASSESSMENT_P95_MS,
                    lambda i: "assessments",
                    lambda i: {
                        "target": hubs[i % len(hubs)],
                        "change_type": "change",
                        "title": f"NFR timing {i} (synthetic)",
                    },
                    assessments,
                )
            )
            made: list = []

            def made_assessments():
                if not made:  # read once, after the new-assessment check has made them
                    made.extend(a["id"] for a in call("assessments?q=NFR%20timing"))
                return made

            plans.append(
                (
                    "assessment detail",
                    ASSESSMENT_DETAIL_P95_MS,
                    lambda i: f"assessments/{made_assessments()[i % len(made_assessments())]}",
                    None,
                    requests,
                )
            )

        def timed(request):
            path, body = request
            start = time.perf_counter()
            try:
                call(path, body)
                ok = True
            except (urllib.error.URLError, OSError):
                ok = False
            return (time.perf_counter() - start) * 1000, ok

        self.stdout.write(f"{requests} requests per endpoint, {workers} workers, {base}")
        self.stdout.write(f"{'endpoint':<24}{'p50 ms':>9}{'p95 ms':>9}{'max ms':>9}{'errors':>8}{'target':>9}  result")
        failed = False
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for label, target, path_for, body_for, count in plans:

                def request(i, path_for=path_for, body_for=body_for):
                    return path_for(i), body_for(i) if body_for else None

                list(pool.map(timed, [request(i) for i in range(workers)]))  # warm up
                results = list(pool.map(timed, [request(i) for i in range(count)]))
                millis = [m for m, _ in results]
                errors = sum(1 for _, ok in results if not ok)
                p50, p95 = statistics.median(millis), percentile(millis, 95)
                passed = p95 < target and errors == 0
                failed |= not passed
                self.stdout.write(
                    f"{label:<24}{p50:>9.0f}{p95:>9.0f}{max(millis):>9.0f}{errors:>8}{'<' + str(target):>9}  "
                    + ("PASS" if passed else "FAIL")
                )
        failed |= not self.time_digest_run(email)
        failed |= not self.time_discovery_run(email)
        self.stdout.write(self.style.ERROR("FAIL") if failed else self.style.SUCCESS("All targets met."))
        if failed and strict:
            raise CommandError("One or more targets failed.")

    def time_discovery_run(self, email):
        """In process: a 2,000-object simulated run of a throwaway connector in the signed-in user's
        organization (it is deleted afterwards)."""
        from core.models import Membership

        membership = (
            Membership.objects.select_related("organization", "user")
            .filter(user__username=email)
            .order_by("created_at", "id")
            .first()
        )
        if membership is None:
            self.stdout.write("discovery run: skipped (the account has no organization in this database)")
            return True
        seconds, ok = discovery_run_seconds(membership.organization, membership.user)
        passed = ok and seconds < DISCOVERY_RUN_S
        self.stdout.write(
            f"{'discovery run':<24}{seconds:>9.1f} s{'':>16}{'<' + str(DISCOVERY_RUN_S) + ' s':>11}  "
            + ("PASS" if passed else "FAIL")
        )
        return passed

    def time_digest_run(self, email):
        """In process (the worker builds digests, not a request): one digest run for the signed-in user's
        organization."""
        from core.models import Membership

        membership = (
            Membership.objects.select_related("organization")
            .filter(user__username=email)
            .order_by("created_at", "id")
            .first()
        )
        if membership is None:
            self.stdout.write("digest run: skipped (the account has no organization in this database)")
            return True
        seconds = digest_run_seconds(membership.organization)
        passed = seconds < DIGEST_RUN_S
        self.stdout.write(
            f"{'digest run':<24}{seconds:>9.1f} s{'':>16}{'<' + str(DIGEST_RUN_S) + ' s':>11}  "
            + ("PASS" if passed else "FAIL")
        )
        return passed
