# Softwarden web/API image: the built SPA and the Django API in one container, one origin.
# The same image runs the worker and, as root, the one-shot `init` service that generates secrets (compose.yaml).

FROM node:22.22-bookworm-slim@sha256:e21fc383b50d5347dc7a9f1cae45b8f4e2f0d39f7ade28e4eef7d2934522b752 AS web
WORKDIR /src
# Roots of TLS-inspecting networks (ca-certs/README.md), trusted by npm when any are present.
COPY ca-certs/ /tmp/ca-certs/
COPY apps/web/package.json apps/web/package-lock.json ./
RUN set -e; \
    if ls /tmp/ca-certs/*.crt > /dev/null 2>&1; then \
      cat /tmp/ca-certs/*.crt > /tmp/extra-ca.pem; export NODE_EXTRA_CA_CERTS=/tmp/extra-ca.pem; \
    fi; \
    npm ci
COPY apps/web/ ./
# The permissions reference and the API guide bundle docs/PERMISSIONS.md and docs/API.md through the `@docs` alias
# (vite.config.ts), which resolves to /docs from this stage's /src. Only those files are in the build context
# (.dockerignore).
COPY docs/PERMISSIONS.md docs/API.md /docs/
RUN npm run build
# The npm half of THIRD_PARTY_NOTICES: every production package in package-lock.json, with its license texts.
RUN node scripts/third-party-notices.mjs . > /src/third-party-npm.txt

# uv installs the Python dependencies below; it is bind-mounted into that one step, never copied into the image, so a
# code-execution bug in the app finds no package installer.
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

# An exact patch release (upgrade runbook, "Pinned images"): rebuilding the same commit gives the same Python.
FROM python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
# uv and Python's ssl use the system trust store (Debian's public roots plus any ca-certs/*.crt) instead of bundled
# lists, so a TLS-inspecting network's root works for package downloads and for Microsoft Graph at runtime.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NATIVE_TLS=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PATH="/app/.venv/bin:$PATH" \
    SOFTWARDEN_DEBUG=0 \
    SOFTWARDEN_SPA_DIR=/app/web \
    SOFTWARDEN_SECRETS_DIR=/data/secrets \
    SOFTWARDEN_BOOTSTRAP_TOKEN_FILE=/data/bootstrap/setup-token \
    SOFTWARDEN_MEDIA_ROOT=/data/media \
    SOFTWARDEN_BACKUP_DIR=/data/backups
COPY ca-certs/ /usr/local/share/ca-certificates/extra/
RUN update-ca-certificates
# The worker's nightly backups (apps/api/core/backup.py) need pg_dump/pg_restore matching the postgres:17 server, and
# openssl for the encryption ops/backup.sh uses. Debian bookworm ships PostgreSQL 15 only, so the client comes from the
# PostgreSQL project's apt repository (PGDG). Its signing key is committed in the repository, not fetched at build time:
# docker/pgdg/ACCC4CF8.asc, fingerprint B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8 (checked by tests/test_backup_stack.py;
# apt reads an armored key named in signed-by, so no gnupg in the image).
COPY docker/pgdg/ACCC4CF8.asc /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
RUN set -e; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-client-17 openssl; \
    rm -rf /var/lib/apt/lists/*; \
    pg_dump --version | grep -q " 17\."
WORKDIR /app/api
COPY apps/api/pyproject.toml apps/api/uv.lock ./
# WITH_OTEL=1 adds the optional OpenTelemetry trace export (the `otel` extra, apps/api/softwarden/otel.py); it stays
# off until OTEL_EXPORTER_OTLP_ENDPOINT is set. The default image doesn't carry it.
ARG WITH_OTEL=0
# uv's download cache lives in a BuildKit cache mount, so it never ends up in a layer either.
RUN --mount=type=bind,from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    if [ "$WITH_OTEL" = "1" ]; then uv sync --frozen --no-dev --no-install-project --extra otel; \
    else uv sync --frozen --no-dev --no-install-project; fi
COPY apps/api/ ./
COPY --from=web /src/dist /app/web
# THIRD_PARTY_NOTICES: every package in `uv export --no-dev` (read from this venv with
# importlib.metadata; one missing fails the build) and every production npm package the SPA is built from, with their
# license texts. Served at /THIRD_PARTY_NOTICES.txt from the SPA folder (WhiteNoise) and linked at
# /app/THIRD_PARTY_NOTICES. The script and the npm list are bind-mounted, never copied into the image.
RUN --mount=type=bind,from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=docker/third_party_notices.py,target=/tmp/notices/third_party_notices.py \
    --mount=type=bind,from=web,source=/src/third-party-npm.txt,target=/tmp/notices/npm.txt \
    set -e; \
    if [ "$WITH_OTEL" = "1" ]; then extra="--extra otel"; else extra=""; fi; \
    uv export --frozen --no-dev --no-hashes --no-emit-project $extra > /tmp/requirements.txt; \
    python /tmp/notices/third_party_notices.py --requirements /tmp/requirements.txt --npm /tmp/notices/npm.txt \
      --out /app/web/THIRD_PARTY_NOTICES.txt; \
    rm /tmp/requirements.txt; \
    ln -s web/THIRD_PARTY_NOTICES.txt /app/THIRD_PARTY_NOTICES
# uid 10001 is softwarden/init_secrets.APP_UID: the generated secrets belong to this user (tests/test_init_secrets.py).
# /data/secrets (read-only to the app in compose), /data/bootstrap (the one-time setup token), /data/media (attachment
# files) and /data/backups (the worker's backup sets) are private to it; a new named volume takes this owner and mode.
# /data/rehearsal is where `ops/restore.sh --rehearse` unpacks a media archive, in a throwaway volume (the root
# filesystem is read-only in compose and /tmp is a small tmpfs).
# Then, for a code-execution bug in the app: no SUID or SGID file to climb with (compose also
# sets no-new-privileges), and no pip or ensurepip (uv was never copied in; apps use the venv).
RUN useradd --system --create-home --uid 10001 softwarden \
 && install -d -m 0700 -o softwarden -g softwarden /data/secrets /data/bootstrap /data/media /data/backups \
    /data/rehearsal \
 && /usr/local/bin/python -m pip uninstall --yes pip \
 && rm -rf /usr/local/lib/python3.13/ensurepip \
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} + \
 && ! find / -xdev -perm /6000 -type f | grep -q .
USER softwarden
EXPOSE 8000
# /readyz/web, not /readyz: the worker waits for this healthcheck, so it must not wait for the worker. The long start
# period covers this CMD's migrations (an upgrade's can take minutes on a 2 vCPU VM): failures
# inside it don't count, and the first healthy probe ends it. compose.yaml runs the migrations in its own one-shot
# migrate service and the Helm chart in a Job, so their app only starts gunicorn.
HEALTHCHECK --interval=10s --timeout=5s --start-period=600s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz/web', timeout=4).status == 200 else 1)"]
# Migrations are forward-only and run on start, so an upgrade is pull + up (after a backup). While first-run setup
# is open, bootstrap_admin --prepare writes the one-time token (`bootstrap_admin` on the host prints it).
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py bootstrap_admin --prepare && exec gunicorn softwarden.wsgi"]
