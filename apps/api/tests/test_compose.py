"""The stack's shape (compose.yaml, docker/Caddyfile, Dockerfile): secrets are generated before PostgreSQL, nothing needs a `.env`, Caddy is the only published
entry point, only the one-shot migrate service migrates and the app prepares the setup token before serving."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]


def services(*files):
    merged: dict = {}
    for name in files:
        data = yaml.safe_load((REPO / name).read_text(encoding="utf-8"))
        for service, config in data["services"].items():
            merged.setdefault(service, {}).update(config)
    return merged


def command(config):
    value = config.get("command") or ""
    return value if isinstance(value, str) else " ".join(value)


def volumes(config):
    return config.get("volumes", [])


def test_init_generates_secrets_before_postgres_and_needs_no_env_file():
    s = services("compose.yaml")
    init = s["init"]
    assert command(init) == "python -m softwarden.init_secrets"
    assert init["user"] == "0:0" and init["restart"] == "no"
    assert init["image"] == s["app"]["image"] and "build" not in init
    assert "softwarden-secrets:/data/secrets" in volumes(init)
    assert "softwarden-db:/pgdata:ro" in volumes(init)
    assert init["environment"]["SOFTWARDEN_PGDATA"] == "/pgdata"
    db = s["db"]
    assert db["depends_on"]["init"]["condition"] == "service_completed_successfully"
    assert "POSTGRES_PASSWORD" not in db["environment"]
    assert db["environment"]["POSTGRES_PASSWORD_FILE"] == "/run/softwarden/postgres_password"
    # The database's own volume, which init fills with a copy of the password alone.
    assert "softwarden-db-secrets:/run/softwarden:ro" in volumes(db)
    assert not any(v.startswith("softwarden-secrets:") for v in volumes(db))
    assert "softwarden-db-secrets:/data/db-secrets" in volumes(init)
    assert init["environment"]["SOFTWARDEN_DB_SECRETS_DIR"] == "/data/db-secrets"
    for name in ("init", "app", "worker"):
        assert s[name]["env_file"] == [{"path": ".env", "required": False}], name


def test_app_and_worker_read_secrets_read_only_and_only_the_app_holds_the_token():
    s = services("compose.yaml")
    for name in ("app", "worker"):
        assert "softwarden-secrets:/data/secrets:ro" in volumes(s[name]), name
        assert s[name]["environment"]["SOFTWARDEN_SECRETS_DIR"] == "/data/secrets"
    assert "softwarden-bootstrap:/data/bootstrap" in volumes(s["app"])
    assert not any("bootstrap" in v for v in volumes(s["worker"]))


def test_no_secret_has_a_default_value_in_compose():
    """Blank defaults only, so the generated files apply; a fixed default would be the same on every install."""
    env = services("compose.yaml")["app"]["environment"]
    for name in ("SOFTWARDEN_SECRET_KEY", "POSTGRES_PASSWORD", "SOFTWARDEN_CREDENTIAL_KEYS", "SOFTWARDEN_SECRETS_KEY"):
        assert env[name] == f"${{{name}:-}}", name


def test_caddy_is_the_only_published_entry_point():
    s = services("compose.yaml")
    assert "ports" not in s["app"] and "ports" not in s["worker"] and "ports" not in s["init"]
    proxy = s["proxy"]
    assert proxy["image"].startswith("caddy:2.11.")
    assert proxy["ports"] == ["${HTTP_PORT:-80}:80", "${HTTPS_PORT:-443}:443"]
    assert proxy["depends_on"]["app"]["condition"] == "service_healthy"
    assert "./docker/Caddyfile:/etc/caddy/Caddyfile:ro" in volumes(proxy)
    assert proxy["environment"]["CADDY_TLS"] == "${CADDY_TLS:-internal}"
    env = s["app"]["environment"]
    assert env["SOFTWARDEN_TRUSTED_PROXIES"] == "${SOFTWARDEN_TRUSTED_PROXIES:-proxy}"
    assert env["SOFTWARDEN_SECURE_COOKIES"] == "${SOFTWARDEN_SECURE_COOKIES:-1}"
    assert env["SOFTWARDEN_BASE_URL"].endswith("https://${PUBLIC_HOSTNAME:-localhost}}")
    # Nothing else is published; compose.dev.yaml publishes the database on loopback for running the API outside
    # Docker.
    assert [name for name, config in s.items() if "ports" in config] == ["proxy"]


def test_every_named_volume_is_declared():
    data = yaml.safe_load((REPO / "compose.yaml").read_text(encoding="utf-8"))
    declared = set(data["volumes"])
    assert {"softwarden-db", "softwarden-secrets", "softwarden-bootstrap", "caddy-data"} <= declared
    for name, config in data["services"].items():
        for entry in volumes(config):
            source = entry.split(":", 1)[0]
            if not source.startswith((".", "/", "$")):
                assert source in declared, (name, entry)


@pytest.mark.parametrize("files", [("compose.yaml",), ("compose.yaml", "compose.dev.yaml")])
def test_only_the_migrate_service_migrates_and_the_app_waits_for_it(files):
    """The migrations run in a one-shot service before the app, so
    a long migration on a slow host never counts against the app's healthcheck and `up --wait` doesn't give up
    (and app replicas never race to migrate)."""
    s = services(*files)
    migrate = s["migrate"]
    assert command(migrate) == "python manage.py migrate --noinput"
    assert migrate["image"] == s["app"]["image"] and "build" not in migrate
    assert migrate["restart"] == "no" and migrate["healthcheck"] == {"disable": True}
    assert migrate["depends_on"]["db"]["condition"] == "service_healthy"
    assert migrate["environment"] == services("compose.yaml")["app"]["environment"]  # the same settings as the app
    assert s["app"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    app = command(s["app"])
    assert app == "sh -c python manage.py bootstrap_admin --prepare && exec gunicorn softwarden.wsgi"
    for name, config in s.items():
        if name != "migrate":
            assert "migrate" not in command(config), name
    assert s["worker"]["depends_on"]["app"]["condition"] == "service_healthy"


def test_the_upgrade_steps_stop_the_old_app_before_the_migrations():
    """`up -d` recreates migrate first and the app only after it, so the old app and worker would serve the old code
    on a changing schema; the documented upgrade stops them first (ops/restore.sh's ordering: test_restore_script)."""
    for path in ("docs/INSTALL.md", "docs/04-development/upgrade-runbook.md"):
        text = (REPO / path).read_text(encoding="utf-8")
        stop = text.index("docker compose stop app worker")
        assert stop < text.index("docker compose up -d --wait", stop), path


def test_the_image_starts_the_same_way():
    """Run on its own, the image migrates before it serves; its healthcheck's start period leaves that room."""
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "python manage.py migrate --noinput && python manage.py bootstrap_admin --prepare && exec gunicorn" in (
        dockerfile
    )
    assert "--start-period=600s" in dockerfile
    assert "COPY ca-certs/ /usr/local/share/ca-certificates/extra/" in dockerfile
    assert "update-ca-certificates" in dockerfile
    assert "/data/secrets /data/bootstrap" in dockerfile


def test_dev_override_publishes_the_debug_port_on_loopback_only():
    s = services("compose.yaml", "compose.dev.yaml")
    assert s["app"]["ports"] == ["127.0.0.1:8080:8000"]
    assert s["db"]["ports"] == ["127.0.0.1:${POSTGRES_PORT:-5434}:5432"]
    assert s["init"]["environment"]["POSTGRES_PASSWORD"] == "${POSTGRES_PASSWORD:-softwarden-dev}"


def test_caddyfile_terminates_tls_and_proxies_to_the_app():
    caddyfile = (REPO / "docker" / "Caddyfile").read_text(encoding="utf-8")
    assert "admin off" in caddyfile and "skip_install_trust" in caddyfile
    assert "{$PUBLIC_HOSTNAME:localhost}" in caddyfile and "tls {$CADDY_TLS:internal}" in caddyfile
    assert "reverse_proxy app:8000" in caddyfile
    for header in ("Strict-Transport-Security", "X-Content-Type-Options", "Referrer-Policy", "-Server"):
        assert header in caddyfile
    # HSTS never for localhost (it would pin every local port to HTTPS), only through the matcher.
    assert "@hsts not host localhost 127.0.0.1 [::1]" in caddyfile
    hsts = [line.strip() for line in caddyfile.splitlines() if "Strict-Transport-Security" in line]
    assert hsts == ['header @hsts Strict-Transport-Security "max-age=31536000"']
    # The HTTP -> HTTPS redirect keeps a non-default HTTPS port.
    assert "auto_https disable_redirects" in caddyfile
    assert "redir https://{host}:{$HTTPS_PORT:443}{uri} permanent" in caddyfile
    assert services("compose.yaml")["proxy"]["environment"]["HTTPS_PORT"] == "${HTTPS_PORT:-443}"


def test_caddy_logs_no_query_string_and_sends_no_server_header_on_the_redirect():
    """Caddy's error log drops the whole query from request>uri (an OIDC code and state,
    the setup token, the calendar feed's token), not only `token`; the plain-HTTP redirect strips `Server` too.
    Checked on caddy 2.11.9: a 502 for /x?code=abc&state=def logs "uri":"/x", and the 301 has no Server header."""
    lines = caddy_lines()
    assert 'request>uri regexp "\\?.*$" ""' in lines
    assert not any("delete token" in line for line in lines)
    redirect = lines[lines.index("http://{$PUBLIC_HOSTNAME:localhost} {") + 1 :]
    assert redirect[: redirect.index("}")] == [
        "header -Server",
        "redir https://{host}:{$HTTPS_PORT:443}{uri} permanent",
    ]


def test_a_credential_key_file_setting_reaches_app_worker_and_init():
    s = services("compose.yaml")
    for name in ("app", "worker", "init"):
        assert s[name]["environment"]["SOFTWARDEN_CREDENTIAL_KEYS_FILE"] == "${SOFTWARDEN_CREDENTIAL_KEYS_FILE:-}", name


def test_the_deployment_mode_reaches_app_and_worker_and_defaults_to_selfhosted():
    """Compose can run cloud mode for evaluation (SOFTWARDEN_DEPLOYMENT=cloud in .env); the web app and the worker must
    agree on it, since both read installation defaults from it."""
    s = services("compose.yaml")
    for name in ("app", "worker"):
        assert s[name]["environment"]["SOFTWARDEN_DEPLOYMENT"] == "${SOFTWARDEN_DEPLOYMENT:-selfhosted}", name


def test_container_files_keep_lf_line_endings():
    attributes = (REPO / ".gitattributes").read_text(encoding="utf-8")
    for pattern in ("*.sh text eol=lf", "Caddyfile text eol=lf"):
        assert pattern in attributes
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "ca-certs/*.crt" in ignored and (REPO / "ca-certs" / "README.md").exists()


def test_worker_healthcheck_follows_the_heartbeat(db):
    """compose.yaml's worker healthcheck (`run_worker --check`), so `docker compose up --wait` can wait for it."""
    from datetime import timedelta

    from django.core.management import call_command
    from django.core.management.base import CommandError
    from django.utils import timezone

    from core import worker

    assert services("compose.yaml")["worker"]["healthcheck"]["test"] == [
        "CMD",
        "python",
        "manage.py",
        "run_worker",
        "--check",
    ]
    with pytest.raises(CommandError, match="never"):
        call_command("run_worker", "--check")
    worker.beat(timezone.now() - timedelta(minutes=10))
    with pytest.raises(CommandError, match="stale"):
        call_command("run_worker", "--check")
    worker.beat()
    call_command("run_worker", "--check")


@pytest.mark.parametrize(
    ("files", "media"),
    [(("compose.yaml",), "softwarden-media"), (("compose.yaml", "compose.dev.yaml"), "softwarden-dev-media")],
)
def test_attachment_files_live_on_a_volume_the_app_and_worker_share(files, media):
    """Attachments: the app writes and serves them, the worker's nightly sweep deletes removed ones. Nothing
    else mounts them, and the image points MEDIA_ROOT there with a private directory a new volume copies."""
    s = services(*files)
    for name in ("app", "worker"):
        assert f"{media}:/data/media" in volumes(s[name]), name
    assert [n for n, config in s.items() if any(f"{media}:" in v for v in volumes(config))] == ["app", "worker"]
    data = yaml.safe_load((REPO / "compose.yaml").read_text(encoding="utf-8"))
    assert "softwarden-media" in data["volumes"]
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "SOFTWARDEN_MEDIA_ROOT=/data/media" in dockerfile
    assert "install -d -m 0700 -o softwarden -g softwarden /data/secrets /data/bootstrap /data/media" in dockerfile


def test_caddy_and_the_app_share_one_request_body_limit():
    """core/upload_limit.py refuses oversize multipart bodies before CSRF parses them; Caddy refuses any body over the
    same SOFTWARDEN_MAX_REQUEST_MB first (defence in depth)."""
    s = services("compose.yaml")
    value = "${SOFTWARDEN_MAX_REQUEST_MB:-26}"
    assert s["app"]["environment"]["SOFTWARDEN_MAX_REQUEST_MB"] == value
    assert s["worker"]["environment"]["SOFTWARDEN_MAX_REQUEST_MB"] == value
    assert s["proxy"]["environment"]["SOFTWARDEN_MAX_REQUEST_MB"] == value
    caddyfile = (REPO / "docker" / "Caddyfile").read_text(encoding="utf-8")
    assert "max_size {$SOFTWARDEN_MAX_REQUEST_MB:26}MiB" in caddyfile  # binary, as the app's limit


def test_slow_request_bodies_never_hold_a_gunicorn_worker():
    """Three clients trickling a request body stopped the app (3 sync workers). Caddy reads
    the whole body (up to the app's own limit) before the request goes upstream, and gunicorn runs threaded workers;
    ops/ci/slowbody-smoke.py proves it against the built stack in CI."""
    proxy = [line for line in caddy_block("handle") if line.startswith("request_buffers")]
    assert proxy == ["request_buffers {$SOFTWARDEN_MAX_REQUEST_MB:26}MiB"]
    gunicorn = (REPO / "apps" / "api" / "gunicorn.conf.py").read_text(encoding="utf-8")
    assert 'worker_class = "gthread"' in gunicorn
    assert 'threads = int(os.environ.get("SOFTWARDEN_THREADS", "8"))' in gunicorn
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "ops/ci/slowbody-smoke.py --connections 3" in workflow
    assert "ops/ci/slowbody-smoke.py --connections 30" in workflow


def test_dev_override_sends_mail_to_a_loopback_mailpit():
    """The dev stack's installation SMTP default is Mailpit, published on loopback only; the shipped compose
    file has no mail catcher."""
    assert "mailpit" not in services("compose.yaml")
    s = services("compose.yaml", "compose.dev.yaml")
    assert s["mailpit"]["image"].startswith("axllent/mailpit:")
    assert s["mailpit"]["ports"] == ["127.0.0.1:8025:8025", "127.0.0.1:1025:1025"]
    assert "profiles" not in s["mailpit"]
    for name in ("app", "worker"):
        env = s[name]["environment"]
        assert env["SOFTWARDEN_SMTP_HOST"] == "${SOFTWARDEN_SMTP_HOST:-mailpit}", name
        assert env["SOFTWARDEN_SMTP_PORT"] == "${SOFTWARDEN_SMTP_PORT:-1025}", name
        assert env["SOFTWARDEN_SMTP_TLS"] == "${SOFTWARDEN_SMTP_TLS:-0}", name


def test_the_mcp_server_is_an_opt_in_profile_behind_caddy_with_no_secrets():
    """MCP (docs/MCP.md): `--profile mcp` only; reached through Caddy at /mcp once the operator opts in, never
    published; it holds no token or secret (each client's own token passes through) and calls the app inside the
    network."""
    data = yaml.safe_load((REPO / "compose.yaml").read_text(encoding="utf-8"))
    mcp = data["services"]["mcp"]
    assert mcp["profiles"] == ["mcp"]
    assert all("profiles" not in config for name, config in data["services"].items() if name != "mcp")
    assert "ports" not in mcp and "volumes" not in mcp and "env_file" not in mcp
    env = mcp["environment"]
    assert env["SOFTWARDEN_URL"] == "http://app:8000"
    assert not any("TOKEN" in name or "SECRET" in name or "KEY" in name for name in env)
    assert env["MCP_ENABLE_WRITES"] == "${MCP_ENABLE_WRITES:-0}"
    assert env["MCP_ALLOWED_HOSTS"].startswith("${PUBLIC_HOSTNAME:-localhost}")
    assert mcp["build"] == {"context": "./mcp", "additional_contexts": {"cacerts": "./ca-certs"}}
    # Caddy routes /mcp only while SOFTWARDEN_MCP_UPSTREAM is set (blank by default): unset, it answers 404 and never
    # resolves an upstream name (the host's DNS search domains could otherwise answer for `mcp`).
    assert data["services"]["proxy"]["environment"]["SOFTWARDEN_MCP_UPSTREAM"] == "${SOFTWARDEN_MCP_UPSTREAM:-}"
    caddyfile = (REPO / "docker" / "Caddyfile").read_text(encoding="utf-8")
    assert '\t\texpression `{env.SOFTWARDEN_MCP_UPSTREAM} != ""`' in caddyfile
    # The MCP server limits refused tokens per client address and believes X-Forwarded-For from anyone in the
    # network ("*", which takes the leftmost entry), so Caddy sends it exactly the one address it resolved.
    assert env["MCP_FORWARDED_ALLOW_IPS"] == "*"
    assert (
        "handle @mcp {\n\t\treverse_proxy {$SOFTWARDEN_MCP_UPSTREAM:mcp-disabled.invalid:9} {\n"
        "\t\t\theader_up X-Forwarded-For {client_ip}\n\t\t}\n\t}"
    ) in caddyfile
    assert '@mcp_off path /mcp /mcp/*\n\thandle @mcp_off {\n\t\trespond "Not found" 404' in caddyfile
    assert "reverse_proxy mcp:8001" not in caddyfile
    dockerfile = (REPO / "mcp" / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["softwarden-mcp", "--transport", "http"]' in dockerfile and "USER softwarden-mcp" in dockerfile


# ---- security headers and timeouts --------

SPA_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; "
    "object-src 'none'"
)


def caddy_lines():
    text = (REPO / "docker" / "Caddyfile").read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def caddy_block(name):
    """The lines inside the first `name {` block of the Caddyfile (comments dropped)."""
    lines, depth = caddy_lines(), 0
    inside: list[str] = []
    start = lines.index(f"{name} {{")
    for line in lines[start + 1 :]:
        if line == "}" and depth == 0:
            return inside
        depth += line.endswith("{") - (line == "}")
        inside.append(line)
    raise AssertionError(f"{name} block is not closed")


def seconds(duration):
    units = {"s": 1, "m": 60, "h": 3600}
    return int(duration[:-1]) * units[duration[-1]]


def test_caddy_sends_the_spa_csp_only_where_the_app_sent_none():
    """`?` sets a header only when the response has none: the app's own CSPs (the attachment sandbox, the SAML bounce
    and the simulator's nonce pages) win, and everything else gets the SPA's. Each `?` header is its own directive: a
    `?` field makes its whole block conditional (Caddy's response `require`), so a shared block would drop the CSP
    whenever Django had sent COOP (found on the running stack)."""
    lines = caddy_lines()
    conditional = [line for line in lines if line.startswith("header ?")]
    assert conditional == [
        f'header ?Content-Security-Policy "{SPA_CSP}"',
        'header ?Permissions-Policy "camera=(), microphone=(), geolocation=()"',
        'header ?Referrer-Policy "same-origin"',  # the SAML bounce's no-referrer wins
        'header ?Cross-Origin-Opener-Policy "same-origin"',
    ]
    assert not any("?" in line for line in caddy_block("header"))
    assert not any("Content-Security-Policy" in line for line in lines if line not in conditional)
    directives = dict(d.strip().split(" ", 1) for d in SPA_CSP.split(";"))
    assert directives["script-src"] == "'self'" and "unsafe-eval" not in SPA_CSP
    assert directives["frame-ancestors"] == "'none'" and directives["object-src"] == "'none'"
    # The unconditional headers stay as they were.
    assert caddy_block("header") == ['X-Content-Type-Options "nosniff"', "-Server", "-Via"]  # no Via: 1.1 Caddy


def test_the_spa_has_no_inline_script_for_the_csp_to_block():
    """script-src 'self' allows files only: index.html loads the theme bootstrap as /theme-init.js, never inline."""
    html = (REPO / "apps" / "web" / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.S)
    assert scripts and all("src=" in attrs and not body.strip() for attrs, body in scripts)
    assert '<script src="/theme-init.js"></script>' in html
    assert "<style" not in html and not re.search(r"<[^>]*\son[a-z]+\s*=", html, flags=re.I)  # no inline handlers
    assert (REPO / "apps" / "web" / "public" / "theme-init.js").is_file()


def test_django_sends_no_header_that_disagrees_with_caddys(settings):
    """SecurityMiddleware's nosniff, Referrer-Policy and COOP match Caddy's values. The app also sends its own
    frame protection: X-Frame-Options DENY says what the CSP's frame-ancestors 'none' says, so the two agree (and
    tests/test_security_headers.py keeps the app's CSP equal to Caddy's)."""
    assert settings.SECURE_CONTENT_TYPE_NOSNIFF is True
    assert settings.SECURE_REFERRER_POLICY == "same-origin"
    assert settings.SECURE_CROSS_ORIGIN_OPENER_POLICY == "same-origin"
    assert "django.middleware.clickjacking.XFrameOptionsMiddleware" in settings.MIDDLEWARE
    assert settings.X_FRAME_OPTIONS == "DENY" and "frame-ancestors 'none'" in SPA_CSP


def test_caddy_has_explicit_server_timeouts_that_fit_the_largest_upload():
    servers = caddy_block("servers")
    assert servers[0] == "timeouts {"
    timeouts = dict(line.split() for line in servers[1:] if line != "}")
    assert set(timeouts) == {"read_header", "read_body", "write", "idle"}
    assert seconds(timeouts["read_header"]) <= 30
    # read_body bounds the whole request: the largest body (26 MiB default) must arrive in it at 1 Mbit/s.
    largest = 26 * 1024 * 1024 * 8 / 1_000_000
    assert seconds(timeouts["read_body"]) > largest
    # write runs from the end of the request headers (Go's WriteTimeout), so it covers reading the body, then
    # gunicorn's whole worker timeout, then the transfer (security nit: at least read_body + 120 s).
    gunicorn = (REPO / "apps" / "api" / "gunicorn.conf.py").read_text(encoding="utf-8")
    assert "timeout = 120" in gunicorn and seconds(timeouts["write"]) >= seconds(timeouts["read_body"]) + 120
    assert 0 < seconds(timeouts["idle"]) <= 300
    # In the global options block, before any site.
    text = (REPO / "docker" / "Caddyfile").read_text(encoding="utf-8")
    assert text.index("\tservers {") < text.index("http://{$PUBLIC_HOSTNAME:localhost} {")


def test_the_dev_stack_never_shares_data_volumes_with_the_plain_stack():
    """Install docs: the plain stack generates a database
    password, the dev stack uses softwarden-dev; on shared volumes, switching between them broke one or the other. The
    dev override replaces every data volume with a softwarden-dev-* one (Caddy's CA volumes may be shared), and lists
    each overridden service's volumes in full, so Compose's merge by container path and a plain list replacement agree."""
    plain, dev = services("compose.yaml"), services("compose.yaml", "compose.dev.yaml")
    override = yaml.safe_load((REPO / "compose.dev.yaml").read_text(encoding="utf-8"))
    declared = set(override["volumes"])
    for name, config in plain.items():
        mounted = {v.split(":")[1]: v.split(":")[0] for v in volumes(dev[name])}
        for entry in volumes(config):
            source, target = entry.split(":")[:2]
            if source.startswith((".", "/", "$", "caddy-")):
                continue
            expected = source.replace("softwarden-", "softwarden-dev-", 1)
            assert mounted.get(target) == expected and expected in declared, (name, entry)
        own = override["services"].get(name, {}).get("volumes")
        if own is not None:
            assert sorted(v.split(":")[1] for v in own) == sorted(v.split(":")[1] for v in volumes(config)), name


# An exact release (caddy:2.11.9, axllent/mailpit:v1.21.8): major.minor.patch, with an optional suffix after a hyphen.
# PostgreSQL numbers its releases major.minor since 10, so postgres:17.11-trixie is exact. postgres:17 or caddy:2 would
# change under a running installation on the next pull. The tag is followed by the digest it resolved to, so a
# re-pushed tag can't change it either; Dependabot moves both.
EXACT_TAG = re.compile(r"^(postgres:\d+\.\d+|[a-z0-9./_-]+:v?\d+\.\d+\.\d+)(-[A-Za-z0-9.]+)*@sha256:[0-9a-f]{64}$")


@pytest.mark.parametrize("name", ["compose.yaml", "compose.dev.yaml"])
def test_no_compose_image_floats_on_a_major_or_minor_tag(name):
    """Upgrade: third-party images are pinned; only the
    Softwarden images, which the operator pins through SOFTWARDEN_IMAGE / SOFTWARDEN_MCP_IMAGE, are variables."""
    data = yaml.safe_load((REPO / name).read_text(encoding="utf-8"))
    checked = 0
    for service, config in data["services"].items():
        image = config.get("image")
        if image is None or image.startswith("${SOFTWARDEN_"):
            continue
        assert EXACT_TAG.match(image), f"{name}: {service} uses {image}; pin an exact release"
        checked += 1
    assert checked  # compose.yaml: db and proxy; compose.dev.yaml: mailpit


def test_the_setup_scripts_check_tls_on_the_dockerfiles_python_image():
    """scripts/setup.sh and setup.ps1 run their TLS check on the image the build uses: the same tag and digest
    as the Dockerfile's FROM python:, which Dependabot updates while it doesn't read the scripts."""
    bases = set(re.findall(r"^FROM (python:\S+)", (REPO / "Dockerfile").read_text(encoding="utf-8"), re.MULTILINE))
    bases |= set(
        re.findall(r"^FROM (python:\S+)", (REPO / "mcp" / "Dockerfile").read_text(encoding="utf-8"), re.MULTILINE)
    )
    assert len(bases) == 1, bases
    sh = re.search(r'^CHECK_IMAGE="([^"]+)"', (REPO / "scripts" / "setup.sh").read_text(encoding="utf-8"), re.MULTILINE)
    ps1 = re.search(
        r'^\$CheckImage = "([^"]+)"', (REPO / "scripts" / "setup.ps1").read_text(encoding="utf-8"), re.MULTILINE
    )
    assert sh and ps1
    assert {sh[1], ps1[1]} == bases


def test_the_dockerfiles_pin_an_exact_python_patch_release():
    for path in ("Dockerfile", "mcp/Dockerfile"):
        bases = re.findall(r"^FROM (python:\S+)", (REPO / path).read_text(encoding="utf-8"), re.MULTILINE)
        assert bases, path
        for base in bases:
            assert re.fullmatch(r"python:3\.13\.\d+-slim-bookworm@sha256:[0-9a-f]{64}", base), f"{path}: {base}"


def test_the_image_ships_no_tests_or_e2e_servers():
    """Tests/ (with the test-only capability URLconf)
    and the e2e servers stay out of the build context, and no shipped module imports them."""
    ignored = {line.strip() for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()}
    assert {"apps/api/tests", "apps/api/e2e_server.py", "apps/api/e2e_db.py"} <= ignored
    api = REPO / "apps" / "api"
    for path in api.rglob("*.py"):
        relative = path.relative_to(api)
        if relative.parts[0] in {"tests", ".venv"} or relative.name in {"e2e_server.py", "e2e_db.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert "from tests" not in text and "import tests" not in text, relative
        assert "import e2e_server" not in text and "import e2e_db" not in text, relative


# ---- container hardening -----------------------------------------------------------
# The Helm chart's pod security, in Compose terms. The app image's services (and the MCP server, init and Caddy) run on
# a read-only root filesystem; every service drops every capability it doesn't use and can't gain privileges; every
# service has memory and process limits, so a runaway process takes down its own container, not the host (and
# PostgreSQL with it).
READ_ONLY = ("init", "migrate", "app", "worker", "mcp", "proxy")
APP_IMAGE = ("init", "migrate", "app", "worker", "mcp")  # the services that run Softwarden's images
TMP = "/tmp"  # noqa: S108  (the path inside the containers)
CAPABILITIES = {
    # Writes the secret files and hands them to uid 10001; reads the database volume (owned by postgres) to tell a new
    # database from an existing one.
    "init": ["CHOWN", "DAC_OVERRIDE", "FOWNER"],
    # The postgres image's entrypoint runs as root to fix the data directory's owner and mode, reads the password file,
    # then drops to the postgres user (gosu).
    "db": ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"],
    # Caddy runs as root in its image and binds 80 and 443.
    "proxy": ["NET_BIND_SERVICE"],
}


def mebibytes(value) -> float:
    """A mem_limit or tmpfs size (`512m`, `1g`, or bytes as `docker compose config` prints them) in MiB."""
    text = str(value).strip().lower()
    units = {"k": 1 / 1024, "m": 1, "g": 1024}
    if text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    return int(text) / (1024 * 1024)


def tmp_size(config) -> float:
    """The size of the /tmp tmpfs, from the short syntax (`/tmp:size=256m`) or `docker compose config`'s long one."""
    for entry in config.get("tmpfs", []):
        path, _, options = str(entry).partition(":")
        if path == TMP:
            return mebibytes(dict(o.split("=") for o in options.split(","))["size"])
    for mount in config.get("volumes", []):
        if isinstance(mount, dict) and mount.get("type") == "tmpfs" and mount.get("target") == TMP:
            return mebibytes(mount["tmpfs"]["size"])
    raise AssertionError("no /tmp tmpfs")


def assert_hardened(name, config):
    assert config.get("security_opt") == ["no-new-privileges:true"], name
    assert config.get("cap_drop") == ["ALL"], name
    assert sorted(config.get("cap_add", [])) == CAPABILITIES.get(name, []), name
    assert int(config["pids_limit"]) > 0, name
    assert mebibytes(config["mem_limit"]) >= 128, name
    if name in READ_ONLY:
        assert config.get("read_only") is True, name
        assert tmp_size(config) > 0, name
    if name in APP_IMAGE:  # an init as PID 1 (signals forwarded, the worker's tool children reaped)
        assert config.get("init") is True, name


def test_every_service_drops_privileges_and_has_limits():
    s = services("compose.yaml")
    assert set(s) == {"init", "db", "migrate", "app", "worker", "mcp", "proxy"}
    for name, config in s.items():
        assert_hardened(name, config)
    # Uploads over 2.5 MB are staged in /tmp (a tmpfs, so counted in the container's memory): room for several at the
    # largest request size, as the Helm chart's tmpSizeLimit.
    for name in ("app", "migrate", "worker"):
        assert tmp_size(s[name]) >= 4 * 26, name
        assert mebibytes(s[name]["mem_limit"]) >= 512 + tmp_size(s[name]), name


def test_the_proxy_has_memory_for_the_request_bodies_it_buffers():
    """Caddy holds each request body in memory (request_buffers, up to SOFTWARDEN_MAX_REQUEST_MB) until it has
    all of it. The limit keeps that inside the proxy, which restarts, and leaves room for 16 of the largest at once."""
    proxy = services("compose.yaml")["proxy"]
    caddyfile = (REPO / "docker" / "Caddyfile").read_text(encoding="utf-8")
    assert "request_buffers {$SOFTWARDEN_MAX_REQUEST_MB:26}MiB" in caddyfile
    assert mebibytes(proxy["mem_limit"]) >= 64 + 16 * 26
    operations = (REPO / "docs" / "04-development" / "operations.md").read_text(encoding="utf-8")
    assert "request_buffers" in operations and "mem_limit" in operations


def test_the_dev_override_keeps_the_hardening():
    dev = services("compose.yaml", "compose.dev.yaml")
    for name in ("init", "db", "migrate", "app", "worker", "proxy"):
        assert_hardened(name, dev[name])


def test_docker_compose_config_keeps_the_hardening():
    """The same through `docker compose config`: Compose's own merge and interpolation."""
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker isn't installed here")
    environ = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "POSTGRES_"))}
    result = subprocess.run(  # noqa: S603
        [docker, "compose", "--profile", "mcp", "config", "--format", "json"],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=environ,
    )
    if result.returncode != 0 and "compose" in result.stderr and "not a docker command" in result.stderr:
        pytest.skip("docker compose isn't installed here")
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)["services"]
    assert set(config) == {"init", "db", "migrate", "app", "worker", "mcp", "proxy"}
    for name, service in config.items():
        assert_hardened(name, service)
    assert [name for name, service in config.items() if service.get("ports")] == ["proxy"]
    assert {m["source"] for m in config["db"]["volumes"]} == {"softwarden-db", "softwarden-db-secrets"}


def test_the_image_has_no_setuid_files_and_no_package_tools():
    """A code-execution bug in the app finds no SUID binary and no uv or pip. uv is bind-mounted into the one
    build step that uses it, never copied into a layer. The clean-stack check looks at a built image."""
    for path in ("Dockerfile", "mcp/Dockerfile"):  # The MCP image gets the same treatment
        dockerfile = (REPO / path).read_text(encoding="utf-8")
        assert "COPY --from=ghcr.io/astral-sh/uv" not in dockerfile, path
        assert re.search(r"^FROM ghcr\.io/astral-sh/uv:\S+@sha256:[0-9a-f]{64} AS uv$", dockerfile, re.MULTILINE), path
        assert "--mount=type=bind,from=uv,source=/uv,target=/usr/local/bin/uv" in dockerfile, path
        assert "python -m pip uninstall --yes pip" in dockerfile, path
        assert "rm -rf /usr/local/lib/python3.13/ensurepip" in dockerfile, path
        assert "-perm /6000 -type f -exec chmod a-s" in dockerfile, path
