#!/usr/bin/env python3
"""`npm audit` at moderate and above, with an explicit, reasoned allow-list. npm has no
way to accept one advisory, so CI's audit job reads `npm audit --json` and this script decides:

  npm audit --omit=dev --json > audit.json || true
  python3 ops/ci/npm-audit-check.py audit.json apps/web/audit-allowlist.json

Exit 1 when an advisory at or above the allow-list's "level" isn't listed, or a listed one is past its "review_by"
date (so an accepted risk is looked at again, not forgotten). A listed advisory npm no longer reports is printed as
stale, to be removed; it doesn't fail the job. Exit 2 when either file is unreadable, or the report isn't an audit result
(npm couldn't reach the registry: an "error" key; no auditReportVersion or vulnerabilities). Standard library only.

The allow-list: {"level": "moderate", "allow": [{"advisory": "GHSA-...", "package": "...", "reason": "...",
"review_by": "YYYY-MM-DD"}]}. Every field is required; the reason says why the advisory doesn't apply here.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

LEVELS = ["info", "low", "moderate", "high", "critical"]
FIELDS = ("advisory", "package", "reason", "review_by")


def advisory_id(url: str) -> str:
    """GHSA-xxxx-xxxx-xxxx from https://github.com/advisories/GHSA-xxxx-xxxx-xxxx."""
    return url.rstrip("/").rsplit("/", 1)[-1]


def advisories(report: dict) -> dict[str, dict]:
    """{advisory id: {package, title, severity, range}} for every advisory npm reports (npm 7+ JSON, auditReportVersion
    2). A package listed only because a dependency is vulnerable names that dependency in `via`; it adds nothing."""
    found: dict[str, dict] = {}
    for name, vulnerability in (report.get("vulnerabilities") or {}).items():
        for via in vulnerability.get("via", []):
            if isinstance(via, dict):
                key = advisory_id(str(via.get("url") or via.get("source")))
                found[key] = {
                    "package": via.get("name", name),
                    "title": via.get("title", ""),
                    "severity": via.get("severity", "critical"),
                    "range": via.get("range", ""),
                }
    return found


def not_a_report(report: object) -> str:
    """Why `report` isn't an npm audit result, or "". CI discards npm's exit status (`|| true`: it is non-zero whenever
    anything is reported), so a registry that can't be reached ({"message": ..., "error": {...}}) or an empty file
    must fail here rather than read as no advisories (review follow-up)."""
    if not isinstance(report, dict):
        return "not a JSON object"
    if "error" in report:
        error = report["error"]
        summary = error.get("summary") or error.get("code") if isinstance(error, dict) else error
        return f"npm reported an error: {summary or report.get('message', '?')}"
    if "auditReportVersion" not in report:
        return "no auditReportVersion"
    if not isinstance(report.get("vulnerabilities"), dict):
        return "no vulnerabilities object"
    return ""


def check(report: dict, allowlist: dict, today: date) -> tuple[int, list[str]]:
    lines: list[str] = []
    level = allowlist.get("level", "moderate")
    if level not in LEVELS:
        return 2, [f"::error::unknown level {level!r} in the allow-list (one of {', '.join(LEVELS)})"]
    allowed: dict[str, dict] = {}
    failed = 0
    for entry in allowlist.get("allow", []):
        missing = [f for f in FIELDS if not str(entry.get(f, "")).strip()]
        if missing:
            lines.append(f"::error::allow-list entry {entry.get('advisory', '?')} lacks {', '.join(missing)}")
            failed += 1
            continue
        allowed[entry["advisory"]] = entry
    found = advisories(report)
    at_level = {k: v for k, v in found.items() if LEVELS.index(v["severity"]) >= LEVELS.index(level)}
    for key, adv in sorted(at_level.items()):
        entry = allowed.get(key)
        if entry is None:
            lines.append(f"::error::{adv['severity']}: {adv['package']} {adv['range']}: {adv['title']} ({key})")
            failed += 1
        elif date.fromisoformat(entry["review_by"]) < today:
            lines.append(
                f"::error::the allow-list's acceptance of {key} ({adv['package']}) expired on "
                f"{entry['review_by']}: review it again, then update or remove the entry"
            )
            failed += 1
        else:
            lines.append(
                f"allowed until {entry['review_by']}: {adv['severity']} {adv['package']} {key}: {entry['reason']}"
            )
    for key in sorted(set(allowed) - set(found)):
        lines.append(f"::notice::{key} is on the allow-list but npm no longer reports it: remove the entry")
    lines.append(f"npm audit: {len(found)} advisories, {len(at_level)} at {level} or above, {failed} not accepted")
    return (1 if failed else 0), lines


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    try:
        report = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
        allowlist = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"::error::can't read the audit report or the allow-list: {e}")
        return 2
    problem = not_a_report(report)
    if problem:
        print(f"::error::npm audit didn't produce a report ({problem}); the audit didn't run, so it can't pass")
        return 2
    code, lines = check(report, allowlist, date.today())
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
