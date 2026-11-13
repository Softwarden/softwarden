"""The repository documents the SPA bundles carry nothing that identifies the machine they were
written on or the owner's Microsoft tenant: no drive-letter path and none of the real tenant, subscription or app
registration IDs used during development. Whatever the web build imports with `@docs/<file>?raw` ships to every user's
browser, so the documents are found from the imports, and the two known ones must be among them."""

import hashlib
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WEB_SRC = REPO / "apps" / "web" / "src"
RAW_IMPORT = re.compile(r"""from\s+["']@docs/([^"'?]+)\?raw["']""")
# A lone drive letter with either slash (C:\ or C:/), so the "s:/" in "https://" is not one.
DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
# The development IDs (the dev tenant, the Azure subscription and two app registrations) by the SHA-256 of their first
# block, so this file doesn't repeat them: a placeholder such as <dev-tenant-id> goes in their place.
HEX_BLOCK = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}(?![0-9a-f])")
DEV_ID_HASHES = {"940e6ca16b9a6b28", "d80ee488706851e3", "c5a805518d99e09c", "5da6ee35e2670476"}


def is_dev_id(block: str) -> bool:
    return hashlib.sha256(block.encode()).hexdigest()[:16] in DEV_ID_HASHES


def bundled_docs() -> set[str]:
    found: set[str] = set()
    for path in WEB_SRC.rglob("*.ts*"):
        found.update(RAW_IMPORT.findall(path.read_text(encoding="utf-8")))
    return found


def problems_in(text: str) -> list[str]:
    problems = []
    for number, line in enumerate(text.splitlines(), start=1):
        if DRIVE_PATH.search(line):
            problems.append(f"line {number}: a drive-letter path")
        if any(is_dev_id(block) for block in HEX_BLOCK.findall(line.lower())):
            problems.append(f"line {number}: a development tenant, subscription or app ID")
    return problems


def test_the_bundled_docs_are_found():
    assert {"PERMISSIONS.md", "API.md"} <= bundled_docs()


def test_bundled_docs_carry_no_local_path_or_development_id():
    found = {name: problems_in((REPO / "docs" / name).read_text(encoding="utf-8")) for name in sorted(bundled_docs())}
    assert not {name: problems for name, problems in found.items() if problems}


def test_the_check_catches_what_it_looks_for():
    assert problems_in("Adapted from an earlier version (`" + "C:" + "\\Users\\someone\\dev\\stackbox`)") == [
        "line 1: a drive-letter path"
    ]
    assert problems_in("Adapted from " + "D:" + "/work/stackbox/docs") == ["line 1: a drive-letter path"]
    assert problems_in("see https://learn.microsoft.com/ and `apps/api`, tenant <dev-tenant-id>") == []
    # A synthetic ID block that hashes to none of them passes; the hashes are of 8-character lowercase hex blocks.
    assert problems_in("appId 11111111-2222-4333-8444-555555555555") == []
    assert all(len(h) == 16 for h in DEV_ID_HASHES)
    assert RAW_IMPORT.findall('import doc from "@docs/PERMISSIONS.md?raw";') == ["PERMISSIONS.md"]
