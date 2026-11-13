#!/bin/sh
# First-run setup (and safe re-run) for Softwarden on a Linux or macOS host: one command from a fresh clone to the
# first-run page. Same behaviour as scripts/setup.ps1 (Windows).
#
#   sh scripts/setup.sh                    check, build, start, print the setup token
#   sh scripts/setup.sh --trust-local-ca   also offer to trust Caddy's local root CA on this machine (asks first)
#
# 1. Checks Docker and Compose v2, TLS interception (ca-certs/README.md), and that the HTTP/HTTPS ports are free.
# 2. Builds and starts the stack and waits until it is healthy. The containers generate their own secrets on first
#    start (compose.yaml's init service) and the one-shot migrate service applies migrations before the app starts.
# 3. Runs `doctor` (the installation's checks, no secrets).
# 4. On an interactive terminal, runs `bootstrap_admin`: the one-time setup token while setup is open, and the first
#    time the credential encryption key to back up. Nothing secret is printed on a non-interactive run.
#
# Optional: `docker compose up -d --build` does step 2 on its own. Safe to re-run. Changes nothing outside this
# folder and Docker, unless you pass --trust-local-ca and answer yes.

set -eu
cd "$(dirname "$0")/.."
ROOT=$(pwd)
ENV_FILE="$ROOT/.env"
CHECK_IMAGE="python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72"  # the base the build uses: it trusts what the build trusts
# No build provenance attestation: it carries a timestamp, so each rebuild would get a new image digest and compose
# would recreate containers even when nothing changed (re-runs must not).
BUILDX_NO_DEFAULT_ATTESTATIONS=1
export BUILDX_NO_DEFAULT_ATTESTATIONS

TRUST_LOCAL_CA=0
for arg in "$@"; do
    case "$arg" in
        --trust-local-ca) TRUST_LOCAL_CA=1 ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'Unknown option: %s (try --help)\n' "$arg" >&2; exit 2 ;;
    esac
done

step() { printf '\n== %s\n' "$1"; }
info() { printf '   %s\n' "$1"; }
fail() { printf '\nSetup stopped: %s\n' "$1" >&2; exit 1; }
interactive() { [ -t 0 ] && [ -t 1 ]; }

# --- settings: the shell first, then .env (read with builtins only), then the default ---------------------------

# The last `NAME=value` line wins, read as Compose reads .env: a trailing CR (a file saved on Windows) and surrounding
# blanks are dropped, then one pair of matching quotes around the value.
CR=$(printf '\r')
env_get() {
    _value=""
    [ -f "$ENV_FILE" ] || { printf ''; return; }
    while IFS= read -r _line || [ -n "$_line" ]; do
        _line=${_line%"$CR"}
        case "$_line" in
            "$1="*) _value=${_line#"$1="} ;;
        esac
    done < "$ENV_FILE"
    _value=${_value#"${_value%%[![:space:]]*}"}
    _value=${_value%"${_value##*[![:space:]]}"}
    case "$_value" in
        \"*\") _value=${_value#\"}; _value=${_value%\"} ;;
        \'*\') _value=${_value#\'}; _value=${_value%\'} ;;
    esac
    printf '%s' "$_value"
}

setting() {  # setting NAME DEFAULT
    eval "_shell=\${$1:-}"
    if [ -n "$_shell" ]; then printf '%s' "$_shell"; return; fi
    _file=$(env_get "$1")
    if [ -n "$_file" ]; then printf '%s' "$_file"; else printf '%s' "$2"; fi
}

# --- preflight ---------------------------------------------------------------------------------------------------

step "Checking Docker"
command -v docker > /dev/null 2>&1 || fail "Docker isn't installed or isn't on PATH. Install Docker Engine with the Compose plugin and run this again."
COMPOSE_VERSION=$(docker compose version --short 2> /dev/null) || fail "Docker Compose v2 is required ('docker compose version' should report 2.x)."
case "$COMPOSE_VERSION" in
    2.*|v2.*) ;;
    *) fail "Docker Compose v2 is required; found '$COMPOSE_VERSION'." ;;
esac
docker info > /dev/null 2>&1 || fail "The Docker engine isn't running, or this user can't reach it (add it to the docker group, or run with sudo)."
info "Docker Compose $COMPOSE_VERSION"

# --- TLS interception --------------------------------------------------------------------------------------------

TLS_CHECK='
import glob, socket, ssl, sys
host = sys.argv[1]
for path in glob.glob("/extra/*.crt"):  # every file must be a usable certificate
    try:
        ssl.create_default_context().load_verify_locations(path)
    except (ssl.SSLError, OSError):
        print("badfile", path.rsplit("/", 1)[-1]); sys.exit()
def connect(extra):
    ctx = ssl.create_default_context()
    if extra:
        for path in glob.glob("/extra/*.crt"):
            ctx.load_verify_locations(path)
    with socket.create_connection((host, 443), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=host):
            pass
try:
    connect(False); print("ok")
except ssl.SSLCertVerificationError:
    try:
        connect(True); print("ok-extra")
    except ssl.SSLCertVerificationError as exc:
        print("intercepted", exc.verify_message)
    except OSError as exc:
        print("unreachable", exc)
except OSError as exc:
    print("unreachable", exc)
'

step "Checking TLS (can the build download what it needs?)"
TLS_HOST=${SOFTWARDEN_TLS_CHECK_HOST:-github.com}
docker pull -q "$CHECK_IMAGE" > /dev/null 2>&1 || fail "Couldn't pull $CHECK_IMAGE. Check this machine's internet access."
TLS=$(printf '%s' "$TLS_CHECK" | docker run --rm -i -v "$ROOT/ca-certs:/extra:ro" "$CHECK_IMAGE" python - "$TLS_HOST" 2>&1 | tail -n 1)
case "$TLS" in
    ok) info "Public certificates: no interception." ;;
    ok-extra) info "TLS is inspected on this network; the root in ca-certs/ is trusted." ;;
    badfile*) fail "ca-certs/${TLS#badfile } isn't a PEM certificate. Replace it with the root CA as PEM (text starting -----BEGIN CERTIFICATE-----); see ca-certs/README.md." ;;
    intercepted*)
        info "This network inspects TLS: $TLS_HOST doesn't present a public certificate (${TLS#intercepted })."
        fail "Ask IT for the root CA your proxy uses, save it as PEM in ca-certs/<name>.crt (see ca-certs/README.md), and run this again. On a Windows machine that trusts it, scripts/setup.ps1 can export it for you." ;;
    *) fail "Couldn't reach $TLS_HOST over HTTPS ($TLS). Check this machine's internet access." ;;
esac

# --- ports -------------------------------------------------------------------------------------------------------

port_in_use() {
    if command -v ss > /dev/null 2>&1; then
        ss -Hltn 2> /dev/null | awk '{print $4}' | grep -Eq "[:.]$1\$"
    elif command -v netstat > /dev/null 2>&1; then
        netstat -an 2> /dev/null | grep -i listen | grep -Eq "[:.]$1[[:space:]]"
    else
        return 2
    fi
}

HTTP_PORT=$(setting HTTP_PORT 80)
HTTPS_PORT=$(setting HTTPS_PORT 443)
step "Checking ports"
if [ -n "$(docker compose ps -q proxy 2> /dev/null)" ]; then
    info "Softwarden's proxy is already running on its ports."
else
    for pair in "HTTP_PORT:$HTTP_PORT" "HTTPS_PORT:$HTTPS_PORT"; do
        name=${pair%%:*}
        port=${pair#*:}
        set +e
        port_in_use "$port"
        status=$?
        set -e
        if [ "$status" = 0 ]; then
            fail "Port $port is in use. Stop what's using it, or set $name in .env to a free port (e.g. 8443) and run this again."
        elif [ "$status" = 2 ]; then
            info "Can't check port $port (no ss or netstat); continuing."
        fi
    done
    info "Ports $HTTP_PORT and $HTTPS_PORT are free."
fi

# --- start -------------------------------------------------------------------------------------------------------

step "Building and starting Softwarden (the first build takes a few minutes)"
# `--wait` waits for every healthcheck, the worker's heartbeat included (compose.yaml); the exit code is what counts.
if ! docker compose up -d --build --wait --wait-timeout 600; then
    docker compose ps
    docker compose logs --tail 40 init app
    fail "Softwarden didn't become healthy. The output above shows why."
fi

HOST=$(setting PUBLIC_HOSTNAME localhost)
URL="https://$HOST"
if [ "$HTTPS_PORT" != 443 ]; then URL="$URL:$HTTPS_PORT"; fi

FACTS=$(docker compose exec -T app python manage.py setup_status 2>&1 || true)
# The whole setup at a glance (no secrets): a FAIL line says what to fix; warnings about first-run setup and the first
# backup are expected on a new install (docs/INSTALL.md, step 4c).
step "Checking the installation (manage.py doctor)"
docker compose exec -T app python manage.py doctor || info "doctor found a failure above; its fix line says what to do."
step "Ready"
case "$FACTS" in
    *setup_pending=1*) info "Open $URL, accept the certificate warning (Caddy's local CA), and enter the one-time token below." ;;
    *) info "Softwarden is running at $URL. First-run setup is already done; sign in." ;;
esac
if interactive; then
    # The token while setup is open and, the first time only, the credential key to back up.
    docker compose exec app python manage.py bootstrap_admin
else
    info "To see the setup token and, the first time, the credential key, run in an interactive terminal:"
    info "  docker compose exec app python manage.py bootstrap_admin"
fi
if [ "$HTTPS_PORT" != 443 ]; then
    info "(The address it prints assumes port 443; use $URL, or set SOFTWARDEN_BASE_URL=$URL in .env.)"
fi

printf '\n'
info "Back up the credential encryption key off this machine (a password manager). Database backups don't include"
info "it, and without it no secret saved in Softwarden can be read. 'docker compose down -v' deletes it."

# --- optional: trust Caddy's local root CA on this machine -------------------------------------------------------

if [ "$TRUST_LOCAL_CA" = 1 ]; then
    step "Trusting Caddy's local root CA (optional)"
    CA_FILE="$ROOT/certs/softwarden-local-root.crt"
    mkdir -p "$ROOT/certs"
    docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt "$CA_FILE" > /dev/null \
        || fail "Couldn't copy Caddy's root certificate (is CADDY_TLS 'internal'?)."
    if command -v update-ca-certificates > /dev/null 2>&1; then
        MODE=debian
        PLAN="sudo cp \"$CA_FILE\" /usr/local/share/ca-certificates/softwarden-local-root.crt && sudo update-ca-certificates"
    elif command -v security > /dev/null 2>&1; then
        MODE=macos
        PLAN="sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain \"$CA_FILE\""
    else
        info "Saved the root certificate to $CA_FILE. Add it to this machine's trust store by hand."
        exit 0
    fi
    info "This will make this machine trust every certificate Caddy's local CA issues, by running:"
    info "  $PLAN"
    info "Firefox keeps its own store; import $CA_FILE there if you use it. Undo by removing the certificate."
    if ! interactive; then fail "Not an interactive terminal; nothing was changed."; fi
    printf '   Go ahead? [y/N] '
    read -r answer
    case "$answer" in
        y|Y|yes|YES)
            # Run directly with the path as one argument (PLAN is only shown), so no path character is interpreted.
            if [ "$MODE" = debian ]; then
                sudo cp "$CA_FILE" /usr/local/share/ca-certificates/softwarden-local-root.crt && sudo update-ca-certificates
            else
                sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain "$CA_FILE"
            fi
            info "Trusted. Restart the browser." ;;
        *) info "Nothing was changed." ;;
    esac
fi
