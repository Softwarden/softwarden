# CLAUDE.md — Softwarden

The open-source system of record for the software, SaaS, integrations and AI agents an IT team runs. "NetBox for software."

This file is for AI coding assistants working in this repository. People and agents follow the same rules:
[CONTRIBUTING.md](CONTRIBUTING.md) is the contributor guide, [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) explains how to
run Softwarden from source, what each part of the repository does, and the data and UI rules every change follows.

## Stack (do not swap without asking)
Python 3.13, Django 5.2 LTS, DRF + drf-spectacular, psycopg 3, PostgreSQL 17. Node 22, Vite, React 18, TypeScript, Tailwind v4, shadcn/ui, react-router 6, TanStack Query and Table, cmdk, Cytoscape, Recharts. PostgreSQL-backed worker (no Redis, no Celery). Docker Compose with Caddy. uv, ruff, mypy, pytest, Playwright with axe.

## Architecture rules
- Every tenant table: UUID id, `organization_id`, `UNIQUE (organization_id, id)`, composite FKs. Cross-tenant suite stays green.
- Every write goes through a service function that validates, scopes to the organization and writes an AuditEvent in the same transaction. Views never save domain models.
- Every endpoint declares its minimum role (Admin / Editor / Viewer). The URL coverage test fails otherwise.
- Features are gated by `capabilities(org)` names, never by edition strings in views.
- UI-entered secrets are Fernet-encrypted under `SOFTWARDEN_CREDENTIAL_KEYS`, write-only, and never appear in audit snapshots, exports, logs, API responses or task arguments.
- Discovery never overwrites curated data; observations go to a queue and differences are shown, applying one is an audited edit.
- Compliance fields are a per-organization profile, off by default.

## Working rules
1. Tests are required for models, services, permissions, endpoints, jobs, notifications and reconciliation. The full CI gate passes before a change is done.
2. Every model change ships with a migration. Never edit an applied migration. Migrations are forward-only.
3. Verify Microsoft endpoints and permissions on Microsoft Learn before implementing; record them in `docs/PERMISSIONS.md`.
4. Follow the UI rules in `docs/DEVELOPMENT.md`: tokens only, status as glyph plus text, named gaps, loading / empty / error / viewer states on every screen, filters in the URL, WCAG 2.2 AA.
5. Prefer direct, explicit code. A new dependency needs a reason in the commit message.
6. Update the README, `docs/DEVELOPMENT.md`, `docs/API.md`, `docs/ENV.md` and the runbooks in the same commit as the change they describe.

A comment says the reason for a rule in words; it doesn't cite tracker or decision ids.

## Commands (keep current)
```bash
docker compose up -d --build                      # full stack at https://localhost via Caddy; no .env needed (secrets generated into a volume)
docker compose exec app python manage.py bootstrap_admin   # first run: prints setup URL + token (and the credential key once)
docker compose -f compose.yaml -f compose.dev.yaml up -d db   # dev Postgres 17 only, on 127.0.0.1:5434, password softwarden-dev
docker compose -f compose.yaml -f compose.dev.yaml up -d --build   # dev stack also published on http://127.0.0.1:8080
cd apps/api && export SOFTWARDEN_DEBUG=1 && uv sync && uv run python manage.py migrate && uv run python manage.py seed_demo
cd apps/api && SOFTWARDEN_DEBUG=1 uv run python manage.py runserver 127.0.0.1:8010
cd apps/api && SOFTWARDEN_DEBUG=1 uv run python manage.py run_worker  # one tick a minute; --once for one tick
cd apps/web && npm install && npm run dev          # http://localhost:5173, proxies /api
cd apps/api && uv run ruff check . && uv run ruff format --check . && uv run mypy --no-incremental . && SOFTWARDEN_DEBUG=1 uv run python manage.py makemigrations --check --dry-run && uv run pytest -q
cd apps/web && npm run lint && npm run build && npm run size && npm run e2e
```

Local `manage.py` runs need `SOFTWARDEN_DEBUG=1` (it defaults to off); pytest sets it itself through `tests/settings.py`. Simulated tenants work under DEBUG, or on a demo server with `SOFTWARDEN_ALLOW_SIMULATED=1`.

## CI gate
ruff, ruff format, mypy, `makemigrations --check`, pytest, eslint, tsc, vite build under 230 KB gzipped, docker build, Playwright smoke, cross-tenant suite, URL coverage test.
