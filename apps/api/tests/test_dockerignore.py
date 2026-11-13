""".dockerignore keeps one machine's local state out of the image. The Dockerfile copies the
whole of apps/api, so anything git ignores there (attachment files and organization exports under media/, the
first-run setup token under .bootstrap/, .env files, logs) would otherwise ride along into an image someone builds
where the API has run, and from there to everyone who pulls it.

Docker's rules (moby/patternmatcher) are implemented here: patterns are paths from the context root, `*` and `?` stay
inside one path segment, `**` crosses any number of them, a pattern that matches a directory excludes everything in
it, `!` re-includes, and the last matching pattern wins."""

import re
import shutil
import subprocess
from functools import cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
API = "apps/api"


@cache
def dockerignore_rules() -> tuple[tuple[re.Pattern[str], bool], ...]:
    rules = []
    for raw in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pattern = line[1:].strip() if negate else line
        pattern = pattern.strip("/")
        regex = ""
        i = 0
        while i < len(pattern):
            if pattern.startswith("**/", i):
                regex, i = regex + "(?:.*/)?", i + 3
            elif pattern.startswith("**", i):
                regex, i = regex + ".*", i + 2
            elif pattern[i] == "*":
                regex, i = regex + "[^/]*", i + 1
            elif pattern[i] == "?":
                regex, i = regex + "[^/]", i + 1
            else:
                regex, i = regex + re.escape(pattern[i]), i + 1
        rules.append((re.compile(regex), negate))
    return tuple(rules)


def excluded(path: str) -> bool:
    """True when Docker leaves `path` (relative to the repository root) out of the build context."""
    parts = path.strip("/").split("/")
    result = False
    for regex, negate in dockerignore_rules():
        # A pattern matching the path or any directory above it applies (patternmatcher.MatchesOrParentMatches).
        if any(regex.fullmatch("/".join(parts[:n])) for n in range(1, len(parts) + 1)):
            result = not negate
    return result


def candidates_from_gitignore() -> set[str]:
    """A path under apps/api for every .gitignore pattern that could apply there: wildcards filled in, and a file
    inside it in case the pattern names a directory. git decides which of them it actually ignores."""
    paths = set()
    for raw in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "!")):
            continue
        pattern = line.strip("/")
        if "/" in pattern:  # anchored at the root: only patterns for apps/api itself
            if not pattern.startswith(f"{API}/"):
                continue
            path = pattern
        else:
            path = f"{API}/{pattern}"
        path = path.replace("**", "x").replace("*", "x").replace("?", "x")
        paths |= {path, f"{path}/file", f"{API}/core/{path.removeprefix(API + '/')}"}
    return paths


def git_ignored(paths) -> set[str]:
    git = shutil.which("git")
    if git is None or not (REPO / ".git").exists():
        pytest.skip("needs git and a checkout")
    result = subprocess.run(  # noqa: S603
        [git, "-C", str(REPO), "check-ignore", "--no-index", "--stdin", "-z"],
        input="\0".join(sorted(paths)).encode(),  # bytes and NUL-separated: no newline translation on Windows
        capture_output=True,
    )
    assert result.returncode in (0, 1), result.stderr
    return {path for path in result.stdout.decode().split("\0") if path}


def test_every_path_git_ignores_under_the_api_stays_out_of_the_image():
    planted = {
        f"{API}/media/attachments/org/file.pdf",
        f"{API}/media/exports/org/export.zip",
        f"{API}/.bootstrap/e2e/setup-token",
        f"{API}/.env",
        f"{API}/.env.production",
        f"{API}/server.log",
    }
    ignored = git_ignored(candidates_from_gitignore() | planted)
    assert planted <= ignored  # the git side of the check still sees what the review found
    leaks = sorted(path for path in ignored if not excluded(path))
    assert not leaks, f"git-ignored but sent to the Docker build context: {leaks}"


def test_local_state_outside_the_api_stays_out_of_the_image():
    for path in (
        ".env",
        ".env.local",
        "mcp/.env",
        "apps/web/.env.local",
        "review-scratch/ws10-ops/app.log",
        "certs/key.pem",
        "backups/softwarden-2026-11-11.dump.enc",
        "apps/web/npm-debug.log",
    ):
        assert excluded(path), path


def test_what_the_build_needs_is_still_sent():
    for path in (
        f"{API}/manage.py",
        f"{API}/pyproject.toml",
        f"{API}/uv.lock",
        f"{API}/gunicorn.conf.py",
        f"{API}/core/models.py",
        f"{API}/core/media.py",  # a module named like an ignored folder is not that folder
        "apps/web/package.json",
        "apps/web/src/main.tsx",
        "docs/PERMISSIONS.md",
        "docs/API.md",
        "ca-certs/corporate-root.crt",  # a network's root is meant to be built in (ca-certs/README.md)
        "docker/pgdg/ACCC4CF8.asc",
        ".env.example",
    ):
        assert not excluded(path), path


def test_the_matcher_follows_dockers_rules():
    rules = dockerignore_rules()
    assert rules  # parsed
    assert excluded("apps/api/tests/test_x.py") and excluded("apps/api/core/__pycache__/x.pyc")
    assert excluded("docs/04-development/operations.md") and not excluded("docs/API.md")


def test_the_test_only_views_stay_out_of_the_image():
    """The Playwright-only routes aren't built in; their importer is guarded."""
    assert excluded(f"{API}/api/e2e_views.py")
    assert not excluded(f"{API}/api/urls.py")
