#!/usr/bin/env python3
"""Fails when a tracked Markdown file has a broken relative link or anchor.
Standard library only, so it runs on any runner with python3 and git.

  python3 ops/ci/check-doc-links.py            exit 1 and list each broken link, exit 0 when there are none

What is checked, in every Markdown file git tracks: each inline link `[text](target)` outside fenced code whose
target is not a URL (http, https, mailto, data, tel). The target, resolved against the file's folder (or the
repository root when it starts with /), must be a tracked file or a folder that holds one. A `#fragment` on a
Markdown target must be one of that file's heading anchors (GitHub's slug rules, with -1, -2 for repeats) or an
explicit <a name|id="...">. Images are not checked. tests/test_doc_links.py runs this in the pytest suite.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

REPO = Path(__file__).resolve().parents[2]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^\s*(```|~~~).*?^\s*\1", re.S | re.M)
EXTERNAL = re.compile(r"^(https?:|mailto:|data:|tel:)")
HEADING = re.compile(r"\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
NAMED_ANCHOR = re.compile(r"<a\s+(?:name|id)=[\"']([^\"']+)")


def tracked_files(repo: Path = REPO) -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def slug(heading: str) -> str:
    text = heading.strip().replace("`", "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text).lower()
    return "".join(ch for ch in text if ch.isalnum() or ch in " -_").replace(" ", "-")


def anchors(text: str) -> set[str]:
    found: set[str] = set()
    seen: dict[str, int] = {}
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEADING.match(line)
        if m:
            a = slug(m[2])
            n = seen.get(a, 0)
            seen[a] = n + 1
            found.add(a if n == 0 else f"{a}-{n}")
        found.update(NAMED_ANCHOR.findall(line))
    return found


def normalise(path: str) -> str:
    parts: list[str] = []
    for part in path.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    return "/".join(parts)


def broken_links(repo: Path = REPO) -> list[str]:
    files = tracked_files(repo)
    present = set(files)
    folders = {"/".join(f.split("/")[:i]) for f in files for i in range(1, f.count("/") + 1)}
    cache: dict[str, set[str]] = {}
    problems: list[str] = []
    for md in (f for f in files if f.endswith(".md")):
        text = (repo / md).read_text(encoding="utf-8", errors="replace")
        base = str(Path(md).parent.as_posix())
        for m in LINK.finditer(FENCE.sub("", text)):
            href = m[1].strip("<>")
            if EXTERNAL.match(href):
                continue
            target, _, fragment = href.partition("#")
            target = unquote(target)
            if not target:
                resolved = md
            elif target.startswith("/"):
                resolved = normalise(target)
            else:
                resolved = normalise(f"{base}/{target}" if base != "." else target)
            if resolved not in present and resolved not in folders:
                problems.append(f"{md}: {href} (no such file)")
                continue
            if fragment and resolved.endswith(".md"):
                if resolved not in cache:
                    cache[resolved] = anchors((repo / resolved).read_text(encoding="utf-8", errors="replace"))
                if unquote(fragment).lower() not in cache[resolved]:
                    problems.append(f"{md}: {href} (no such anchor)")
    return problems


def main() -> int:
    problems = broken_links()
    for p in problems:
        print(p)
    print(f"{len(problems)} broken link(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
