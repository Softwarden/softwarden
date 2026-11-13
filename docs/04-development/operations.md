# Operations: logs, health and traces

This page is for whoever runs a Softwarden installation day to day: how to read the logs, what to point a monitor at, how the daily end-of-life data refresh works, and how to send traces to an OpenTelemetry collector.

For a quick check of the whole installation, run `docker compose exec app python manage.py doctor`. Point an outside monitor at `https://<PUBLIC_HOSTNAME>/readyz`, which answers 503 when the database, migrations or the worker aren't right.

## Logs

The app and the worker write one JSON object per line on stdout (`docker compose logs app`, `docker compose logs worker`). Docker rotates the logs at 10 MB × 5 files (compose.yaml `x-logging`).

| Field | When | What |
|---|---|---|
| `ts`, `level`, `logger`, `message` | always | UTC time to the millisecond, the level, the logger name and the message. |
| `request_id` | in a request, job or schedule run | The request's id. It is the `X-Request-ID` header Caddy sets and the response returns, and the id on the request's audit events. Inside a job it is the job's id; inside a schedule run, the run's own id. |
| `org`, `user` | once known | The organization's and the user's ids. The org is filled in when the request's membership is resolved, and for a job, from the job. Never a name or an email address. |
| `path`, `method` | in a request | The path, **never the query string**. |
| `status`, `duration_ms` | on the request line | The app's `softwarden.request` logger writes one line per request when it finishes. Gunicorn's own access log is off. |
| `exc` | on an error | The traceback. |

To follow one request through the app, the worker and the audit log, search for its `X-Request-ID`:

```bash
docker compose logs app worker | grep '"request_id": "<id>"'
```

**What a line never holds.** Logs never contain a query string. The renewals calendar feed carries its secret in one (`?token=`), and runserver's request lines and some exception messages would print it. Every line is also scrubbed of the configured secrets: Django's key, the database password, the credential and backup keys, the ops token and the installation's Anthropic key. They are replaced with `[redacted]` in the message, the request fields and the traceback. The list of secrets is read again at most every 60 seconds, so a key changed in a running process (it isn't, today: every one is read at start) would be redacted within a minute. Django's own loggers go through the same formatter, including `django.server` (runserver's request lines, which carry the query string) and the console handler Django adds in development.

**Levels.** A request that succeeds is logged at INFO, a 4xx at WARNING and a 5xx at ERROR, the levels Django uses. Django's own `Not Found: <path>` line is replaced by the request line, which carries the request's fields. A successful probe (`/healthz`, `/readyz`, `/readyz/web`) is logged at DEBUG, so the container healthcheck doesn't fill the log. A failing probe keeps its level. `SOFTWARDEN_LOG_LEVEL` sets the root level (default `INFO`).

**Requests that never reach Django.** With gunicorn's access log off, a request that never reaches the app has no `softwarden.request` line. Instead:

- A worker killed by gunicorn's 120 s timeout is a `gunicorn.error` line (`WORKER TIMEOUT`), and Caddy logs the 502 it then answers.
- A request gunicorn can't parse is answered 400 by gunicorn and logged by it at DEBUG only (not shown at the default `INFO`).
- Anything Caddy can't pass on (the app restarting, refused or timed-out connections) is a Caddy `http.log.error` line in `docker compose logs proxy`, with the status (502, 504) and the request's method, host and path (the query string is dropped: it can carry a token, an OpenID Connect code or the setup token).

Caddy's error lines carry the full URI, query string included, so they were the one place the calendar feed's secret could still be logged. The Caddyfile's global `log` block filters `request>uri` and deletes the `token` parameter, which leaves the other parameters. If you replace the Caddyfile, or add an access log, keep that filter.

**Local reading.** `SOFTWARDEN_LOG_FORMAT=text` gives `time level logger message [request_id=... org=... path=...]` lines instead, scrubbed the same way. Any value other than `json` or `text` stops start-up.

## `/healthz/ops`, the operator health view

`GET /healthz/ops` (next to `/healthz`, outside the documented `/api/v1`) returns installation-wide health in one JSON document:

| Key | What |
|---|---|
| `version` | The installed release's version. |
| `checked_at` | When the answer was computed. |
| `status` | `ok`, `degraded` or `unhealthy`, as on the System page (worker heartbeat and schedules), or `migrations_pending`. |
| `database` | `state` and `latency_ms` of a `SELECT 1`. |
| `migrations` | `pending`: how many migrations are not applied. |
| `worker` | The heartbeat: `state` (`ok`, `stale`, `never`), `last_tick`, `age_seconds`, `stale_after_seconds`. |
| `schedules` | `count`, and the names of schedules that are `stuck`, `overdue` or `failing`. |
| `jobs` | `lanes.default` and `lanes.long`, each with `queued` (due now) and `oldest_age_seconds`. Also `failed_24h` and `abandoned_24h`, across every organization. |
| `backup` | `state` (`ok`, `stale`, `never`, `not_configured`), `last_success_at`, `age_seconds`, `stale_after_seconds`, `last_failure_at`, `last_failure` (the stage only), `running`, `key_present`, `key_acknowledged`. |
| `disk` | `free_bytes`, `total_bytes` and `free_percent` for `media`, and for `backups` where that path is a mount point. In the default compose only the worker mounts the backup volume, so the app reports media only. The backup job enforces its own free-space floor. |

It never contains an organization's name or data, nor any secret. The job counts are totals and the schedule names are defined in code.

**Status codes.** The view answers 200 whenever the database answers, and `status` says how things stand. It answers 503 (`database.state: unreachable`) when the database doesn't answer. Keep `/readyz` as the probe to alert on, and use `/healthz/ops` for detail and dashboards.

**Who can open it.** Only two callers get an answer. Everyone else, including Editors, Viewers and a wrong token, gets a 404, so the view doesn't advertise itself.

- A request with `Authorization: Bearer <ops token>`. The comparison is constant-time over SHA-256 digests (so it doesn't reveal the token's length) and needs no database, so the token still works while the database is down.
- A signed-in Admin session, **only while the installation has exactly one organization** that isn't on the cloud edition. The job figures cover every organization, so on a multi-organization installation one tenant's Admin must not see them; there, only the token opens the view. The role is the session's current membership as everywhere else, so an SSO session stays confined to its organization, and an account that must change its password on any membership is refused.

**The ops token.** The `init` service generates it into the secrets volume as `ops_token`. The file is 0600 and owned by the app user, and it is never replaced once written. Read it on the host with:

```bash
docker compose exec app cat /data/secrets/ops_token
```

Or set your own with `SOFTWARDEN_OPS_TOKEN` in `.env`. Then no file is generated, and the variable wins over an existing one. To rotate the generated token, run `docker compose run --rm init sh -c 'rm /data/secrets/ops_token && python -m softwarden.init_secrets'` and then `docker compose restart app worker`. The app reads the token at start. Alternatively, change `SOFTWARDEN_OPS_TOKEN` and restart. The token appears in no response, audit event or log line.

Example from a monitor:

```bash
curl -fsS -H "Authorization: Bearer $OPS_TOKEN" https://softwarden.example.org/healthz/ops
```

**Behind Caddy.** The route is not blocked at the proxy. The app's own check guards it, so an outside monitor can use it over HTTPS and an Admin can open it in the browser. To keep it on the internal network only, add `handle /healthz/ops { respond "Not found" 404 }` above the default `handle` in a Caddyfile of your own. The view then answers only inside the compose network, at `http://app:8000/healthz/ops` with the bearer header.

## Container limits

`compose.yaml` gives every service a memory limit (`mem_limit`) and a process limit (`pids_limit`), so a runaway process stops its own container (Docker restarts it) instead of taking the host's memory and PostgreSQL with it. The app image's services and the MCP server also run on a read-only root filesystem with a tmpfs `/tmp`; a tmpfs lives in memory and counts toward its container's limit. `docker stats` shows each container's use against its limit; an out-of-memory restart shows as `OOMKilled: true` in `docker inspect`.

| Service | `mem_limit` | `pids_limit` | `/tmp` (tmpfs) | Sized for |
| --- | --- | --- | --- | --- |
| `app` | 1536m | 512 | 256m | gunicorn's 3 workers x 8 threads (`SOFTWARDEN_WORKERS`, `SOFTWARDEN_THREADS`), and uploads over 2.5 MB, which are staged in `/tmp` (up to `SOFTWARDEN_MAX_REQUEST_MB` each) |
| `worker` | 1g | 256 | 128m | one job at a time, and the backup's `pg_dump`, `tar` and `openssl` (they stream to the backups volume; `/tmp` holds only a small scratch folder) |
| `migrate` | 1g | 128 | 256m | one `migrate` run |
| `db` | 2g | 256 | (not read-only) | PostgreSQL's defaults: 128 MB shared buffers, 100 connections, one process each |
| `proxy` | 512m | 256 | 16m | Caddy, and the request bodies it buffers (below) |
| `mcp` | 256m | 128 | 16m | the MCP server |
| `init` | 256m | 64 | 16m | the one-shot secret generation |

**The proxy and request bodies.** Caddy reads each request's whole body into memory before it opens the upstream request (`request_buffers` in `docker/Caddyfile`, up to `SOFTWARDEN_MAX_REQUEST_MB`, 26 MiB by default), so a slow upload ties up only the proxy, never a gunicorn thread. Its memory is therefore about 64 MiB plus `SOFTWARDEN_MAX_REQUEST_MB` for each upload in flight at once: 512m leaves room for 16 of the largest at once. Caddy sets its Go memory limit from the container's, so it collects garbage harder before it reaches it. Past the limit the proxy is restarted and the requests in flight fail; the app, the database and the host are not touched. If you raise `SOFTWARDEN_MAX_REQUEST_MB`, raise the proxy's `mem_limit` to about 64 MiB plus 16 times the new value, and the app's `/tmp` to at least 4 times it (and its `mem_limit` by the same amount).

**Anyone who can reach the proxy can fill that memory.** The bodies are buffered before the app sees the request, so before anyone is authenticated: about 16 clients sending the largest body at once, slowly, use the proxy's 512m, and it is restarted (`restart: unless-stopped`); the requests in flight then fail. The app, the database and the host are not affected. The pinned Caddy build has no per-address rate or concurrency limit (`caddy list-modules` on `caddy:2.11.9` lists no rate-limit handler; Caddy's `rate_limit` is the third-party `caddy-ratelimit` module and needs a custom build), so Softwarden doesn't set one. Where the proxy is reachable from the internet, limit connections per address in front of it: a firewall rule (for example nftables `ct count over 20` per source address on ports 80 and 443), your cloud load balancer or WAF, or a Caddy built with `caddy-ratelimit`. Lowering `SOFTWARDEN_MAX_REQUEST_MB` lowers each body's share.

**Changing a limit.** Put the new value in a `compose.override.yaml` next to `compose.yaml` (Compose reads it automatically), then `docker compose up -d`:

```yaml
services:
  proxy:
    mem_limit: 1g
  app:
    mem_limit: 2g
    tmpfs: ["/tmp:size=512m"]
```

## Schedules that fail

A schedule run that fails is not retried: the schedule fires again at its next cron time, which is the next day for the daily ones (`digest.build`, `jobs.prune`, `backup.nightly` and the others). The System page, `/healthz/ops` (`schedules.failing`) and `manage.py doctor` show it as failing until a run succeeds. Once the cause is fixed, make it fire on the worker's next tick instead of waiting (replace the name):

```bash
docker compose exec worker python manage.py shell -c "from django.utils import timezone; from core.models import Schedule; print(Schedule.objects.filter(name='digest.build').update(next_due=timezone.now()))"
```

It prints `1` when the name matched. The run takes the schedule's lease as usual, so with two workers it still runs once.

## Sessions

Sessions live in the database (`django_session`). When a session key changes (sign-in, organization switch, password
change, impersonation start or stop, sign-out), the old key stays for two minutes as a tombstone with no session
values, so a request still carrying it doesn't sign the browser out ([the API guide](../API.md#signing-in), "Session
key changes"). A request that loses such a race is logged at INFO by `softwarden.sessions`, not as an error. The worker's
`sessions.clear` schedule (daily at 03:50 UTC) deletes expired sessions and tombstones; it shows on the System page with
the other schedules. Nothing else deletes them, so a stopped worker lets the table grow but changes nothing for users.

## End-of-life data (endoflife.date)

End-of-life tracking reads public data from endoflife.date into an installation-wide cache: the product list (`eol_eolproduct`), the release cycles of the products some organization tracks (`eol_eolrelease`) and the cache's own state (`eol_eolsyncstate`). The screens and the API read only the cache, so a slow or unreachable endoflife.date never slows a page.

**The daily refresh.** The `eol.refresh` schedule (04:45 UTC; endoflife.date regenerates its data around 00:10 UTC) queues an `eol.refresh` job in the worker's long lane. The job fetches `https://endoflife.date/api/v1/products`, then `https://endoflife.date/api/v1/products/<name>` for each tracked product, one at a time. Every address the host resolves to must be public, and the connection is pinned to the address checked. Each request has a 15 s socket timeout, a 45 s deadline per request and a body cap (4 MB for the list, 2 MB per product). It sends `If-None-Match` with the ETag it last got, so an unchanged document is a 304 with no body, and the User-Agent `Softwarden/<version> (+https://github.com/Softwarden/softwarden)` (the `PROJECT_URL` setting).

**When something fails.** A 429 or 5xx, or an unreachable host, is retried twice (after 2 s and 4 s, or after `Retry-After` up to 60 s); a 301 is followed on the same host only. A product that fails keeps its cached cycles and records why (`EolProduct.error`, shown as the reason an unknown status is unknown); the others go on. The job fails, and is retried 1 and 4 minutes later, only when the product list can't be read and nothing is cached.

**New products.** When someone maps a product whose cycles aren't cached yet, a job for just that product is queued, so its status appears within a worker tick; while no product list has ever been read, it queues a full refresh instead, which brings the list too. A product answer that can't be stored (not a JSON object, a missing `releases`) is that product's error and the run goes on; a malformed list keeps the cached one. A product whose address redirects to a new name keeps its cycles under the old one and records the new name (`renamed_to`, shown on the tracked version as `product_renamed_to`), so it can be re-mapped.

**Reminders.** After each run the derived cycles of the products touched are recomputed; an end-of-life date that moved moves its reminder at the next hourly `reminders.generate` run, and the built-in "End of life" notification rule announces it (90, 30 and 7 days before, then every 30 days from the end-of-life date while the version is still end of life; a cycle endoflife.date flags end of life with no date counts from the day the version was tracked). Acknowledging a version's reminder on Needs attention stops its repeats; snoozing pauses them through the snooze date. An organization whose End of life rule already had escalation steps keeps `escalate_when_acknowledged` on, so acknowledging doesn't stop its repeats there.

The System page lists the schedule with the others; `GET /api/v1/end-of-life` shows the cache's `last_run_at`, `last_success_at` and `last_error`.

**Refresh now:** `docker compose exec worker python manage.py eol_refresh` (the list and every tracked product), or `--product postgresql` (repeatable) for some products only. It prints the counts: `updated`, `unchanged`, `failed`, `not_found`, `rederived`.

**Air-gapped installs.** Set `SOFTWARDEN_EOL_ENABLED=0`: nothing is fetched or queued, and `eol_refresh` refuses. On a machine with internet access, save the whole data set (every product with its cycles, a few MB) and load it on the server, as often as you like:

```bash
curl -fsSL -o eol-full.json https://endoflife.date/api/v1/products/full
docker compose exec -T worker python manage.py eol_import - < eol-full.json
```

`eol_import` also takes `/api/v1/products` (the list only) or `/api/v1/products/<name>` (one product), and a file path instead of `-`. It replaces the cycles of the products in the file and recomputes the derived cycles; `source` becomes `import`. Until a list has been read (a refresh, or importing `/products` or `/products/full`), mapping accepts any well-formed product name, and its status stays unknown until data for it arrives; a file that isn't an endoflife.date document is refused with a message and nothing is stored.

**What leaves the installation:** product names in the URL path and the User-Agent. No organization, record or user data is sent. The data is endoflife.date's, under the MIT license.

## OpenTelemetry traces (optional)

Trace export has been tested with an in-memory exporter, but not yet against a real collector.

The default image does not include OpenTelemetry. Trace export is on only when both of these hold:

1. The image is built with the `otel` extra: `docker compose build --build-arg WITH_OTEL=1`. Outside Docker, run `uv sync --extra otel`. The extra contains `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, `opentelemetry-instrumentation-django` and `opentelemetry-instrumentation-psycopg`.
2. `OTEL_EXPORTER_OTLP_ENDPOINT` is set for `app` and `worker`, for example `http://otel-collector:4318`. compose.yaml passes this variable through.

If the variable is unset, nothing from `opentelemetry` is imported. If the variable is set but the extra is missing, the process logs one warning and runs untraced.

When on:

- Each gunicorn worker traces every request (Django instrumentation) and every query (psycopg instrumentation).
- The worker traces each job as a `job <kind>` span, and its queries under it. A query outside any job (the tick's own polling) isn't traced, so the worker doesn't send a root trace every few seconds. `OTEL_TRACES_SAMPLER` still decides for everything else.
- Request and job spans carry `softwarden.request_id`, the same id as the log lines and the audit events.
- The service name is `OTEL_SERVICE_NAME`, else `softwarden-web` or `softwarden-worker`.

The exporter is OTLP over HTTP/protobuf, so gRPC is not supported (that would need `grpcio` in the image). Set the SDK's other standard variables in `.env`; both containers read it. These include `OTEL_EXPORTER_OTLP_HEADERS` for a collector's API key, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_CERTIFICATE`, `OTEL_TRACES_SAMPLER` and `OTEL_TRACES_SAMPLER_ARG`.

**What leaves the process.** Every span passes through a scrubbing exporter before it is sent. Its name, every string attribute, every event's attributes (an exception's `exception.message` and `exception.stacktrace`) and its status description get the same scrub as a log line: query strings cut and configured secrets replaced with `[redacted]`. `url.query` is emptied, and the request hooks cut the query from `http.target`, `http.url` and `url.full` as soon as the span starts. Database spans carry each statement with its `%s` placeholders, never the parameter values, and the SQL commenter is off. Nothing else from a request body or header is recorded.

**Don't capture headers.** The instrumentation can record request and response headers through `OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST` and `..._SERVER_RESPONSE`. Leave them unset. If you need one, never list `Authorization` (API and ops tokens), `Cookie` or `Set-Cookie` (the session) or `X-CSRFToken`, and set `OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS` to `authorization,cookie,set-cookie,x-csrftoken`. The scrubbing exporter removes only the secrets it knows, and an API token or a session cookie isn't among them. The assistant's Anthropic client keeps its own tracing off, with no spans, no content and no `traceparent` header, whatever the `ANTHROPIC_OPEN_TELEMETRY_*` variables say.
