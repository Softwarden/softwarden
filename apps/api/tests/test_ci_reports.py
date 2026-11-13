"""ops/ci/test-report-check.py: CI fails when Playwright's JSON report lists a flaky test,
and when pytest skips a test for any reason but the live-tenant one. The reports here have the shapes Playwright's json
reporter and pytest's --junitxml write."""

import json
import os
import subprocess
import sys

import pytest

from tests.test_compose import REPO
from tests.test_upgrade_ci import CI, steps_text, workflow

SCRIPT = REPO / "ops" / "ci" / "test-report-check.py"


def run(*args):
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, check=False
    )
    return result.returncode, result.stdout + result.stderr


def result(status, message=None):
    return {"status": status, "retry": 0, "error": {"message": message} if message else None, "errors": []}


def pw_report(*tests):
    """A report with one file suite, a describe block, and one spec per (title, status, results)."""
    specs = [
        {
            "title": title,
            "file": "search.spec.ts",
            "line": 20 + i,
            "ok": status in ("expected", "flaky", "skipped"),
            "tests": [{"projectName": "chromium", "status": status, "results": results}],
        }
        for i, (title, status, results) in enumerate(tests)
    ]
    return {
        "suites": [
            {
                "title": "search.spec.ts",
                "file": "search.spec.ts",
                "specs": specs[:1],
                "suites": [{"title": "Palette", "file": "search.spec.ts", "specs": specs[1:], "suites": []}],
            }
        ],
        "stats": {},
    }


def test_a_clean_playwright_report_passes(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps(pw_report(("one", "expected", [result("passed")]), ("two", "skipped", []))))
    code, out = run("playwright", path)
    assert code == 0, out
    assert "1 expected, 1 skipped" in out


def test_a_flaky_playwright_test_fails_the_job_and_is_named(tmp_path):
    path = tmp_path / "r.json"
    flaky = [result("failed", "Error: expect(received).toBeGreaterThanOrEqual(expected)\nmore"), result("passed")]
    path.write_text(json.dumps(pw_report(("steady", "expected", [result("passed")]), ("wobbly", "flaky", flaky))))
    code, out = run("playwright", path)
    assert code == 1
    assert "::error::flaky (failed, then passed on retry): [chromium] search.spec.ts:21 > Palette > wobbly" in out
    assert "toBeGreaterThanOrEqual(expected)" in out and "more" not in out  # the first line of the first error
    assert "1 flaky and 0 failed" in out


def test_a_failed_playwright_test_is_named_too(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(json.dumps(pw_report(("broken", "unexpected", [result("failed", "Timeout 60000ms exceeded.")]))))
    code, out = run("playwright", path)
    assert code == 1 and "::error::failed: [chromium] search.spec.ts:20 > broken: Timeout 60000ms exceeded." in out


def test_a_missing_or_broken_playwright_report_is_its_own_failure(tmp_path):
    assert run("playwright", tmp_path / "absent.json")[0] == 2
    (tmp_path / "bad.json").write_text("{not json")
    assert run("playwright", tmp_path / "bad.json")[0] == 2


JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4" skipped="{skipped}">
<testcase classname="tests.test_a" name="test_runs" time="0.1"/>
<testcase classname="tests.test_live_entra" name="test_live_one" time="0">
  <skipped type="pytest.skip" message="live test: run with --live and real credentials in the environment">x</skipped>
</testcase>
<testcase classname="tests.test_live_entra" name="test_live_two" time="0">
  <skipped type="pytest.skip" message="live test: set SOFTWARDEN_LIVE_TENANT_ID">x</skipped>
</testcase>
{extra}
</testsuite></testsuites>
"""


def test_only_live_skips_pass(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text(JUNIT.format(skipped=2, extra=""))
    code, out = run("pytest-skips", path, "--allow", "live test:")
    assert code == 0, out
    assert "3 tests, 2 skipped, 2 with an allowed reason" in out


def test_any_other_skip_fails_and_is_named(tmp_path):
    path = tmp_path / "junit.xml"
    extra = (
        '<testcase classname="tests.test_backup" name="test_a_full_backup_with_real_pg_dump" time="0">'
        '<skipped type="pytest.skip" message="pg_dump 16 can\'t dump a PostgreSQL 17 server">x</skipped></testcase>'
    )
    path.write_text(JUNIT.format(skipped=3, extra=extra))
    code, out = run("pytest-skips", path, "--allow", "live test:")
    assert code == 1
    assert "::error::skipped in CI: tests.test_backup::test_a_full_backup_with_real_pg_dump: pg_dump 16" in out
    # With no prefix allowed, the live skips fail too.
    assert run("pytest-skips", path)[0] == 1


def test_an_expected_failure_is_named_but_not_a_skip(tmp_path):
    # pytest writes an xfail as <skipped type="pytest.xfail">; it ran and failed as marked (vendor fixture VF-5).
    path = tmp_path / "junit.xml"
    extra = (
        '<testcase classname="tests.test_vendor_fixtures" name="test_google_403" time="0">'
        '<skipped type="pytest.xfail" message="VF-5: Google documents 403 quota errors">x</skipped></testcase>'
    )
    path.write_text(JUNIT.format(skipped=3, extra=extra))
    code, out = run("pytest-skips", path, "--allow", "live test:")
    assert code == 0, out
    assert "4 tests, 2 skipped, 2 with an allowed reason" in out
    assert "::notice::expected failure (xfail), not a skip: tests.test_vendor_fixtures::test_google_403: VF-5" in out


def test_a_missing_pytest_report_is_its_own_failure(tmp_path):
    assert run("pytest-skips", tmp_path / "absent.xml", "--allow", "live test:")[0] == 2


def test_the_live_skip_reason_is_the_prefix_ci_allows():
    """The prefix CI allows is the one tests/conftest.py gives every live-test skip."""
    from tests.conftest import live_skip_reason

    assert live_skip_reason(False).startswith("live test:")
    api = steps_text(workflow(CI)["jobs"]["api"])
    assert 'ops/ci/test-report-check.py pytest-skips "$RUNNER_TEMP/pytest.xml" --allow "live test:"' in api


@pytest.mark.parametrize("job", ["e2e"])
def test_ci_checks_the_playwright_report_of_every_shard(job):
    text = steps_text(workflow(CI)["jobs"][job])
    assert "ops/ci/test-report-check.py playwright apps/web/test-results/results.json" in text


# ---- npm audit at moderate, with an allow-list (ops/ci/npm-audit-check.py) ------------------------------------------

AUDIT_SCRIPT = REPO / "ops" / "ci" / "npm-audit-check.py"
ALLOWLIST = REPO / "apps" / "web" / "audit-allowlist.json"


def npm_report(*advisories):
    """`npm audit --json` (auditReportVersion 2): each advisory under its package, and a dependent naming it."""
    vulnerabilities: dict = {}
    for ghsa, package, severity in advisories:
        via = {
            "source": 1,
            "name": package,
            "title": f"{ghsa} title",
            "url": f"https://github.com/advisories/{ghsa}",
            "severity": severity,
            "range": "<9",
        }
        vulnerabilities.setdefault(package, {"name": package, "severity": severity, "via": []})["via"].append(via)
        vulnerabilities[f"{package}-dom"] = {"name": f"{package}-dom", "severity": severity, "via": [package]}
    return {"auditReportVersion": 2, "vulnerabilities": vulnerabilities}


def audit(tmp_path, report, allow, level="moderate"):
    (tmp_path / "audit.json").write_text(json.dumps(report))
    (tmp_path / "allow.json").write_text(json.dumps({"level": level, "allow": allow}))
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(AUDIT_SCRIPT), str(tmp_path / "audit.json"), str(tmp_path / "allow.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout


def entry(ghsa, review_by="2999-01-01", **extra):
    return {"advisory": ghsa, "package": "react-router", "reason": "not here", "review_by": review_by, **extra}


def test_npm_audit_fails_on_an_unlisted_moderate_advisory(tmp_path):
    code, out = audit(tmp_path, npm_report(("GHSA-aaaa-bbbb-cccc", "left-pad", "moderate")), [])
    assert code == 1
    assert "::error::moderate: left-pad <9: GHSA-aaaa-bbbb-cccc title (GHSA-aaaa-bbbb-cccc)" in out
    # Below the level: not counted.
    assert audit(tmp_path, npm_report(("GHSA-aaaa-bbbb-cccc", "left-pad", "low")), [])[0] == 0


def test_npm_audit_passes_a_listed_advisory_until_its_review_date(tmp_path):
    report = npm_report(("GHSA-wrjc-x8rr-h8h6", "react-router", "moderate"))
    code, out = audit(tmp_path, report, [entry("GHSA-wrjc-x8rr-h8h6")])
    assert code == 0, out
    assert "allowed until 2999-01-01: moderate react-router GHSA-wrjc-x8rr-h8h6: not here" in out
    code, out = audit(tmp_path, report, [entry("GHSA-wrjc-x8rr-h8h6", review_by="2001-01-01")])
    assert code == 1 and "expired on 2001-01-01" in out
    # A listed advisory still fails a high one next to it.
    both = npm_report(("GHSA-wrjc-x8rr-h8h6", "react-router", "moderate"), ("GHSA-zzzz-zzzz-zzzz", "vite", "high"))
    assert audit(tmp_path, both, [entry("GHSA-wrjc-x8rr-h8h6")])[0] == 1


def test_npm_audit_allow_list_entries_need_a_reason_and_stale_ones_are_named(tmp_path):
    report = npm_report(("GHSA-wrjc-x8rr-h8h6", "react-router", "moderate"))
    code, out = audit(tmp_path, report, [entry("GHSA-wrjc-x8rr-h8h6", reason=" ")])
    assert code == 1 and "lacks reason" in out
    code, out = audit(tmp_path, npm_report(), [entry("GHSA-wrjc-x8rr-h8h6")])
    assert code == 0 and "::notice::GHSA-wrjc-x8rr-h8h6 is on the allow-list but npm no longer reports it" in out


def test_the_allow_list_accepts_only_the_two_react_router_advisories_with_reasons():
    body = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    assert body["level"] == "moderate"
    assert {e["advisory"] for e in body["allow"]} == {"GHSA-wrjc-x8rr-h8h6", "GHSA-337j-9hxr-rhxg"}
    for e in body["allow"]:
        assert e["package"] == "react-router" and len(e["reason"]) > 80 and e["review_by"]


def test_ci_audits_weekly_at_moderate_through_the_allow_list():
    data = workflow(CI)
    assert data["on"]["schedule"] == [{"cron": "17 5 * * 1"}]  # Mondays, so a new advisory shows between pushes
    jobs = data["jobs"]
    text = steps_text(jobs["audit"])
    assert "npm audit --omit=dev --json" in text
    assert "ops/ci/npm-audit-check.py" in text and "apps/web/audit-allowlist.json" in text
    assert "--audit-level=high" not in text
    # The schedule runs the audit alone; every other job is for pushes and pull requests (the DCO check, for
    # pull requests only, which leaves the schedule out as well).
    for name, job in jobs.items():
        if name == "dco":
            assert job.get("if") == "github.event_name == 'pull_request'", name
        elif name != "audit":
            assert job.get("if") == "github.event_name != 'schedule'", name


def test_every_ci_job_has_a_timeout():
    for name, job in workflow(CI)["jobs"].items():
        assert isinstance(job.get("timeout-minutes"), int) and 5 <= job["timeout-minutes"] <= 120, name


def test_the_otel_build_has_its_own_leg_and_its_tests_run_there():
    """The WITH_OTEL=1 image is built, and the tests marked `otel` run with the extra (none may skip) and inside
    that image; the api job, without the extra, deselects them rather than skipping them."""
    jobs = workflow(CI)["jobs"]
    image = jobs["image"]
    assert image["strategy"]["matrix"]["otel"] == ["0", "1"]
    text = steps_text(image)
    for part in (
        "docker build --build-arg WITH_OTEL=${{ matrix.otel }} --tag softwarden:ci .",
        "uv sync --frozen --extra otel",
        'uv run pytest -q -rs -m otel --junitxml="$RUNNER_TEMP/pytest-otel.xml"',
        'ops/ci/test-report-check.py pytest-skips "$RUNNER_TEMP/pytest-otel.xml"\n',
        "bash ops/ci/otel-image-probe.sh softwarden:ci",
    ):
        assert part in text, part
    assert 'uv run pytest -q -rs -m "not otel"' in steps_text(jobs["api"])


def test_the_upgrade_rehearsal_seeds_more_than_one_small_organization():
    script = (REPO / "ops" / "ci" / "restore-previous.sh").read_text(encoding="utf-8")
    for part in (
        "Organization.objects.create(name='$SECOND_ORG')",
        "Membership.objects.create(organization=other, user=admin, role=Role.ADMIN)",
        "NotificationRule.objects.create(organization=other, name=name",  # named like a built-in this release adds
        "credentials.slot_set(hook, 'signing_secret'",
        "jobs.enqueue('$QUEUED_KIND'",
        'shell -c "$SEED_MORE_PY"',
        'shell -c "$CHECK_MORE_PY"',
        'SW_ROWS=check app python manage.py shell -c "$ROWS_PY" < "$work/scratch/rows.json"',
    ):
        assert part in script, part
    assert script.index('"$SEED_MORE_PY"') < script.index('bash "$backup_script"')  # seeded before the backup


# ---- review follow-ups: a run or an audit that never happened must not pass ----------------------------------------


@pytest.mark.parametrize(
    "report",
    [
        {"suites": [], "stats": {"expected": 0}},  # no tests at all
        {"suites": [], "errors": [{"message": "Error: Process from config.webServer exited early.\nmore"}]},
        {**pw_report(("one", "expected", [result("passed")])), "errors": [{"message": "Error: a spec didn't load"}]},
        [],  # not a report
    ],
)
def test_a_playwright_report_without_tests_or_with_run_errors_fails(tmp_path, report):
    path = tmp_path / "r.json"
    path.write_text(json.dumps(report))
    code, out = run("playwright", path)
    assert code == 2, out
    if isinstance(report, dict) and report.get("errors"):
        assert "::error::the run itself failed: Error: " in out and "more" not in out


def test_a_playwright_report_whose_stats_count_more_failures_than_it_lists_fails(tmp_path):
    report = pw_report(("one", "expected", [result("passed")]))
    report["stats"] = {"expected": 1, "flaky": 2}
    path = tmp_path / "r.json"
    path.write_text(json.dumps(report))
    code, out = run("playwright", path)
    assert code == 2 and "stats count 2 flaky test(s), but only 0 were found" in out, out


def test_the_report_check_survives_a_console_that_cant_show_the_titles(tmp_path):
    """A Windows console on cp1252 can't encode every character a title or an error may hold."""
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps(pw_report(("journey ✓ › 日本", "flaky", [result("failed", "Error: ✗ 失敗"), result("passed")])))
    )
    result_ = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), "playwright", str(path)],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"},
        check=False,
    )
    out = result_.stdout.decode("cp1252") + result_.stderr.decode("cp1252", "replace")
    assert result_.returncode == 1, out
    assert "UnicodeEncodeError" not in out and "::error::flaky (failed, then passed on retry):" in out


@pytest.mark.parametrize(
    "report",
    [
        # What npm audit writes when it can't reach the registry (npm 10, --json); CI discards npm's exit status.
        {
            "message": "request to https://registry.npmjs.org/-/npm/v1/security/advisories/bulk failed",
            "error": {"code": "ENOTFOUND", "summary": "request to https://registry.npmjs.org/... failed", "detail": ""},
        },
        {},
        {"auditReportVersion": 2},
        {"vulnerabilities": {}},
        [],
    ],
)
def test_npm_audit_check_fails_when_npm_produced_no_audit(tmp_path, report):
    (tmp_path / "audit.json").write_text(json.dumps(report))
    (tmp_path / "allow.json").write_text(json.dumps({"level": "moderate", "allow": []}))
    result_ = subprocess.run(  # noqa: S603
        [sys.executable, str(AUDIT_SCRIPT), str(tmp_path / "audit.json"), str(tmp_path / "allow.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result_.returncode == 2, result_.stdout
    assert "::error::npm audit didn't produce a report" in result_.stdout
    if "error" in report:
        assert "npm reported an error: request to https://registry.npmjs.org/... failed" in result_.stdout
