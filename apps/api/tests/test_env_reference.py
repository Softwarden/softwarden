"""docs/ENV.md lists every environment variable the installation reads, and nothing it no longer
reads.

Read means: an `os.environ` / `os.getenv` / `env.get` / `env_flag` / `env_list` / `secret` / `_get(environ, ...)` call
with a literal name, or a module constant whose value is one (`KEYS_ENV = "SOFTWARDEN_CREDENTIAL_KEYS"`), in the API's
and the MCP server's own Python (not tests); a `${NAME...}` interpolation in compose.yaml or compose.dev.yaml (what an
operator can set in `.env`); a `{$NAME}` placeholder or `{env.NAME}` in docker/Caddyfile; a `${NAME:-...}`, `${NAME-...}`,
`${NAME:?...}` or `${NAME+x}` expansion in the host scripts ops/*.sh (their settings, ENV.md "Host scripts"); and
an `ARG NAME` in the Dockerfiles (build arguments). ENV.md's tables name each variable in backticks in their first
column.

A read through a variable (`os.environ.get(name)` in a loop, core/backup.py's PG_TLS_ENV tuple) is invisible to this
scan: name such variables in a module constant that matches, or document them in ENV.md's prose (libpq's PG* variables
are, as libpq's own)."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
API = REPO / "apps" / "api"
ENV_MD = REPO / "docs" / "ENV.md"

NAME = r"([A-Z][A-Z0-9_]*[A-Z0-9])"
PY_READS = [
    re.compile(
        r"(?:os\.environ\.get|os\.getenv|os\.environ\.setdefault|env\.get|env_flag|env_list|secret)\(\s*[\"']"
        + NAME
        + r"[\"']"
    ),
    re.compile(r"os\.environ\[\s*[\"']" + NAME + r"[\"']\s*\]"),
    # init_secrets.py reads through a mapping it is given: _get(environ, "NAME").
    re.compile(r"_get\(\s*\w+\s*,\s*[\"']" + NAME + r"[\"']"),
    # A module constant whose value is one of the installation's variable names (KEYS_ENV, otel.ENDPOINT).
    re.compile(
        r"^[A-Z][A-Z0-9_]*\s*=\s*[\"']((?:SOFTWARDEN|POSTGRES|OTEL|MCP|ANTHROPIC|BOOTSTRAP)_[A-Z0-9_]*[A-Z0-9])[\"']",
        re.MULTILINE,
    ),
]
COMPOSE_READ = re.compile(r"\$\{" + NAME + r"(?=[:}\-?])")
CADDY_READS = [re.compile(r"\{\$" + NAME + r"[:}]"), re.compile(r"\{env\." + NAME + r"\}")]
# Process plumbing, not settings.
IGNORED = {"DJANGO_SETTINGS_MODULE", "PATH", "HOME", "TEMP", "TMP", "TMPDIR", "SYSTEMROOT"}
# A host script reads its settings with a default or a guard; a plain `$NAME` is the script's own variable.
SHELL_READ = re.compile(r"\$\{" + NAME + r"(?=:-|:\?|-|\+)")
SHELL_OWN = {"SCRATCH"}  # ops/common.sh's temporary folder, set by the scripts themselves
DOCKER_ARG = re.compile(r"^ARG\s+" + NAME + r"\b", re.MULTILINE)
ROW = re.compile(r"^\|\s*`" + NAME + r"`\s*\|", re.MULTILINE)


def python_sources():
    roots = [API, REPO / "mcp" / "softwarden_mcp"]
    skip = {".venv", "tests", "node_modules", "__pycache__", "migrations"}
    for root in roots:
        for path in root.rglob("*.py"):
            if not skip & set(path.relative_to(root).parts):
                yield path


def read_names() -> dict[str, set[str]]:
    """{name: the files that read it}."""
    found: dict[str, set[str]] = {}

    def add(name, path):
        if name not in IGNORED:
            found.setdefault(name, set()).add(str(path.relative_to(REPO)).replace("\\", "/"))

    for path in python_sources():
        text = path.read_text(encoding="utf-8")
        for pattern in PY_READS:
            for name in pattern.findall(text):
                add(name, path)
    for compose in ("compose.yaml", "compose.dev.yaml"):
        path = REPO / compose
        for name in COMPOSE_READ.findall(path.read_text(encoding="utf-8")):
            add(name, path)
    caddyfile = REPO / "docker" / "Caddyfile"
    text = caddyfile.read_text(encoding="utf-8")
    for pattern in CADDY_READS:
        for name in pattern.findall(text):
            add(name, caddyfile)
    for script in sorted((REPO / "ops").glob("*.sh")):
        for name in SHELL_READ.findall(script.read_text(encoding="utf-8")):
            if name not in SHELL_OWN:
                add(name, script)
    for dockerfile in (REPO / "Dockerfile", REPO / "mcp" / "Dockerfile"):
        for name in DOCKER_ARG.findall(dockerfile.read_text(encoding="utf-8")):
            add(name, dockerfile)
    return found


def documented() -> list[str]:
    return ROW.findall(ENV_MD.read_text(encoding="utf-8"))


def test_the_scan_finds_the_known_readers():
    """Guards the scan itself: a pattern that silently stopped matching would let both directions pass."""
    names = read_names()
    for expected, where in [
        ("SOFTWARDEN_SECRET_KEY", "apps/api/softwarden/settings.py"),
        ("SOFTWARDEN_PGDATA", "apps/api/softwarden/init_secrets.py"),
        ("SOFTWARDEN_CREDENTIAL_KEYS", "apps/api/core/credentials.py"),
        ("SOFTWARDEN_BACKUP_REQUIRE_MOUNT", "apps/api/softwarden/settings.py"),
        ("PUBLIC_HOSTNAME", "compose.yaml"),
        ("CADDY_TLS", "docker/Caddyfile"),
        ("SOFTWARDEN_MCP_UPSTREAM", "docker/Caddyfile"),
        ("SOFTWARDEN_URL", "mcp/softwarden_mcp/client.py"),
        ("BOOTSTRAP_ADMIN_PASSWORD", "apps/api/core/management/commands/bootstrap_admin.py"),
        ("BACKUP_DEST", "ops/backup.sh"),
        ("SOFTWARDEN_ALLOW_NOACL_KEY", "ops/common.sh"),
        ("WITH_OTEL", "Dockerfile"),
    ]:
        assert where in names.get(expected, set()), (expected, names.get(expected))


def test_every_variable_read_is_in_env_md():
    listed = set(documented())
    missing = {name: sorted(files) for name, files in read_names().items() if name not in listed}
    assert not missing, f"Add these to docs/ENV.md (name, default, purpose, read by, secret): {missing}"


def test_env_md_lists_nothing_that_is_no_longer_read():
    read = set(read_names())
    stale = sorted(name for name in documented() if name not in read)
    assert not stale, f"docs/ENV.md lists variables nothing reads any more: {stale}"


def test_env_md_lists_each_variable_once():
    names = documented()
    assert len(names) == len(set(names)), sorted({n for n in names if names.count(n) > 1})
