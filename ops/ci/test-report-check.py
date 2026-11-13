#!/usr/bin/env python3
"""Reads a test run's machine report and fails the CI job on what the run's own exit status lets through.
Standard library only, so it runs on any runner with python3.

  python3 ops/ci/test-report-check.py playwright <results.json>
      Exit 1 when Playwright's JSON report lists a flaky test (failed, then passed on a retry) or a failed one. CI
      keeps one retry so a flake is named as a flake, with its first failure's error, instead of passing quietly: the
      retry tells you which kind of red it is, never turns it green. Exit 2 when the report is missing or unreadable,
      lists no tests, has top-level errors (the run never got to its tests), or its stats count more flaky or failed
      tests than it lists.

  python3 ops/ci/test-report-check.py pytest-skips <junit.xml> [--allow PREFIX ...]
      Exit 1 when pytest's JUnit XML report has a skipped test whose reason doesn't start with one of the allowed
      prefixes. CI allows only "live test:" (the tests against a real tenant, tests/conftest.py), so a test that
      quietly skips on the runner (a missing tool, an old client) fails the job instead. An expected failure
      (pytest's xfail, which JUnit also writes as <skipped type="pytest.xfail">) is not a skip: it ran and failed as
      its marker says, and a strict one that starts passing fails the run itself. Each is named, not failed.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET  # noqa: S405 - our own pytest's report, not untrusted input
from collections.abc import Iterator
from pathlib import Path


def playwright_tests(report: dict) -> Iterator[tuple[str, dict]]:
    """(title path, test) for every test in a Playwright JSON report, at any suite depth."""

    def walk(suite: dict, path: list[str]) -> Iterator[tuple[str, dict]]:
        here = [*path, suite["title"]] if suite.get("title") else path
        for spec in suite.get("specs", []):
            where = f"{spec.get('file', suite.get('file', '?'))}:{spec.get('line', '?')}"
            for test in spec.get("tests", []):
                title = " > ".join([*here[1:], spec.get("title", "?")]) if len(here) > 1 else spec.get("title", "?")
                yield f"[{test.get('projectName', '?')}] {where} > {title}", test
        for child in suite.get("suites", []):
            yield from walk(child, here)

    for suite in report.get("suites", []):
        yield from walk(suite, [])


def first_error(test: dict) -> str:
    for result in test.get("results", []):
        error = result.get("error") or next(iter(result.get("errors") or []), None)
        if error:
            message = str(error.get("message", "")).strip().splitlines()
            return message[0][:300] if message else ""
    return ""


def check_playwright(path: Path) -> int:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"::error::can't read the Playwright report {path}: {e}")
        return 2
    if not isinstance(report, dict):
        print(f"::error::{path} isn't a Playwright JSON report")
        return 2
    counts: dict[str, int] = {}
    flaky, failed = [], []
    for title, test in playwright_tests(report):
        status = test.get("status", "?")
        counts[status] = counts.get(status, 0) + 1
        if status == "flaky":
            flaky.append((title, test))
        elif status == "unexpected":
            failed.append((title, test))
    print(
        "Playwright: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) if counts else "Playwright: no tests"
    )
    # A run that never got to its tests (a web server that didn't start, a config or spec that doesn't load) writes a
    # report with no tests and top-level errors; that is a failed run, not a clean one (review follow-up).
    errors = [
        (str(e.get("message", e) if isinstance(e, dict) else e).strip().splitlines() or ["(no message)"])[0][:300]
        for e in report.get("errors") or []
    ]
    for message in errors:
        print(f"::error::the run itself failed: {message}")
    if not counts:
        print("::error::the report lists no tests")
    if errors or not counts:
        return 2
    for title, test in flaky:
        print(f"::error::flaky (failed, then passed on retry): {title}: {first_error(test)}")
    for title, test in failed:
        print(f"::error::failed: {title}: {first_error(test)}")
    # Playwright's own totals must agree with what was found, so a report shape this script misreads can't pass.
    stats = report.get("stats") or {}
    for key, found in (("flaky", len(flaky)), ("unexpected", len(failed))):
        if int(stats.get(key) or 0) > found:
            print(f"::error::the report's stats count {stats[key]} {key} test(s), but only {found} were found")
            return 2
    if flaky or failed:
        print(f"{len(flaky)} flaky and {len(failed)} failed test(s). A flaky test is a failure: fix its cause.")
        return 1
    return 0


def check_pytest_skips(path: Path, allowed: list[str]) -> int:
    try:
        root = ET.parse(path).getroot()  # noqa: S314
    except (OSError, ET.ParseError) as e:
        print(f"::error::can't read the pytest report {path}: {e}")
        return 2
    total, skipped, unexpected, xfailed = 0, 0, [], []
    for case in root.iter("testcase"):
        total += 1
        skip = case.find("skipped")
        if skip is None:
            continue
        name, reason = f"{case.get('classname', '?')}::{case.get('name', '?')}", skip.get("message", "")
        if skip.get("type") == "pytest.xfail":
            xfailed.append((name, reason))
            continue
        skipped += 1
        if not any(reason.startswith(prefix) for prefix in allowed):
            unexpected.append((name, reason))
    print(f"pytest: {total} tests, {skipped} skipped, {skipped - len(unexpected)} with an allowed reason")
    for name, reason in xfailed:
        print(f"::notice::expected failure (xfail), not a skip: {name}: {reason}")
    for name, reason in unexpected:
        print(f"::error::skipped in CI: {name}: {reason}")
    if unexpected:
        allowed_text = ", ".join(repr(p) for p in allowed) or "none"
        print(f"{len(unexpected)} test(s) skipped for a reason CI doesn't allow (allowed: {allowed_text}).")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    # Test titles and error messages can hold any character; a Windows console on cp1252 would otherwise stop the
    # check with UnicodeEncodeError (review follow-up). CI's runner is UTF-8 either way.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="kind", required=True)
    pw = sub.add_parser("playwright", help="fail on a flaky or failed test in a Playwright JSON report")
    pw.add_argument("report", type=Path)
    py = sub.add_parser("pytest-skips", help="fail on a skip whose reason isn't allowed, in a pytest JUnit report")
    py.add_argument("report", type=Path)
    py.add_argument("--allow", action="append", default=[], metavar="PREFIX", help="an allowed skip-reason prefix")
    args = parser.parse_args(argv)
    if args.kind == "playwright":
        return check_playwright(args.report)
    return check_pytest_skips(args.report, args.allow)


if __name__ == "__main__":
    sys.exit(main())
