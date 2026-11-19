# Environment reference

Every setting Softwarden reads from its environment: the default, what it does, which service reads it and whether it is a secret. Use it when you write `.env`, or when `doctor` or a log message names a setting. Most installations set only `PUBLIC_HOSTNAME`, `SOFTWARDEN_IMAGE`, the certificate (`CADDY_TLS`) and, for email, the SMTP settings ([install guide](INSTALL.md)).

The project's tests check that this list and the code agree, so every setting the code reads is here.

## How settings reach the containers

- **No `.env` is needed.** On first start the one-shot `init` service generates the secrets (table below) into the `softwarden-secrets` volume. A value set in `.env` or the shell wins over a generated one.
- **`.env` sits next to `compose.yaml`.** Compose reads it for the `${...}` values in `compose.yaml`, and passes every line of it into `init`, `app` and `worker` (`env_file`). So any variable below marked *app* or *worker* can go in `.env`, even if `compose.yaml` doesn't name it. Run `chmod 600 .env` once it holds a password.
- **The shell environment reaches the containers only through `compose.yaml`'s `${...}` values.** A variable `compose.yaml` doesn't name must go in `.env` (or a `compose.override.yaml` `environment:` entry).
- After changing `.env`, run `docker compose up -d`. Compose recreates the containers whose settings changed.
- **Defaults.** Two defaults are given where they differ: the one on the Docker stack (set by `compose.yaml` or the `Dockerfile`), and the one outside Docker (`settings.py`, for `runserver` and the tests).

**Read by** names the service: `app` (web and API, gunicorn), `worker` (schedules and jobs), `init` (generates secrets), `proxy` (Caddy), `mcp` (the MCP server, `--profile mcp`), or `compose` (used only by `compose.yaml` itself).

### Generated secrets

| File in `softwarden-secrets` | Overridden by | Purpose |
|---|---|---|
| `django_secret_key` | `SOFTWARDEN_SECRET_KEY` | Django's signing key (sessions, CSRF). |
| `postgres_password` | `POSTGRES_PASSWORD` | The database password. PostgreSQL reads it once, when the database is created. |
| `credential_keys` | `SOFTWARDEN_CREDENTIAL_KEYS`, `SOFTWARDEN_CREDENTIAL_KEYS_FILE` | The Fernet key that encrypts secrets typed into Softwarden. |
| `backup_key` | `SOFTWARDEN_BACKUP_KEY_FILE` | The key the nightly backups are encrypted with. |
| `ops_token` | `SOFTWARDEN_OPS_TOKEN` | The bearer token for `/healthz/ops`, the operator health view. Read it with `docker compose exec app cat /data/secrets/ops_token`. |

`bootstrap_admin` prints the credential key once. The backup key is copied off the host as [the install guide](INSTALL.md) says. Neither is in a database backup, and `docker compose down -v` deletes both.

## Address, TLS and proxy

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `PUBLIC_HOSTNAME` | `localhost` | The DNS name people open. Caddy serves it, and the app's allowed host, CSRF origin and base URL follow it unless set themselves. | compose, proxy, mcp | No |
| `HTTP_PORT` | `80` | Host port for plain HTTP, which only redirects to HTTPS. | compose | No |
| `HTTPS_PORT` | `443` | Host port for HTTPS. If you change it, set `SOFTWARDEN_BASE_URL=https://host:port` too. | compose, proxy | No |
| `CADDY_TLS` | `internal` | The argument of Caddy's `tls` directive. `internal` uses Caddy's own local CA, so browsers warn. `/certs/cert.pem /certs/key.pem` uses your certificate files (`TLS_CERT_DIR`). An email address, such as `you@example.com`, gets a Let's Encrypt certificate for `PUBLIC_HOSTNAME`; ports 80 and 443 must then be reachable from the internet. The Let's Encrypt form hasn't been tried on a VM yet. | proxy | No |
| `TLS_CERT_DIR` | `./certs` | Host folder mounted at `/certs` in the proxy, for `CADDY_TLS`. | compose | No |
| `SOFTWARDEN_ALLOWED_HOSTS` | Docker: `${PUBLIC_HOSTNAME},127.0.0.1`. Outside: `localhost,127.0.0.1` | Host names the app answers, comma-separated. | app, worker | No |
| `SOFTWARDEN_CSRF_TRUSTED_ORIGINS` | Docker: `https://${PUBLIC_HOSTNAME}`. Outside: `http://localhost:5173,http://127.0.0.1:5173` | Origins allowed to send signed-in writes. Must match the browser's address exactly. | app | No |
| `SOFTWARDEN_BASE_URL` | Docker: `https://${PUBLIC_HOSTNAME}`. Outside: `http://localhost:8080` | The address people browse to. Email links and the SSO redirect start with it. | app, worker | No |
| `SOFTWARDEN_SECURE_COOKIES` | `1` (`0` when `SOFTWARDEN_DEBUG=1`). `compose.dev.yaml`: `0` | Marks the session and CSRF cookies Secure (HTTPS only). `0` is the explicit opt-out for a production install served over plain HTTP, which `doctor` warns about. An explicit `1` together with `SOFTWARDEN_DEBUG=1` stops start-up (Compose and Helm set `1`). | app | No |
| `SOFTWARDEN_HSTS_SECONDS` | `0` (off). Helm: `31536000` while the ingress has TLS | The app sends `Strict-Transport-Security: max-age=<this>` on HTTPS requests. Leave it unset on Compose: Caddy sends HSTS for a real host name and never for localhost. | app | No |
| `SOFTWARDEN_TRUSTED_PROXIES` | Docker: `proxy`. Outside: empty (trust none) | Host names, addresses or CIDR ranges allowed to set `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Request-ID`. | app | No |
| `SOFTWARDEN_MAX_REQUEST_MB` | Docker: `26`. Outside: `SOFTWARDEN_ATTACHMENT_MAX_MB` + 1, at least 6 | The largest request body read at all, in MiB. Caddy enforces the same limit. Keep it at least the attachment limit + 1. | app, proxy | No |
| `SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS` | `1`; `0` in cloud mode | Whether an organization's own SMTP relay or Halo URL may be on a private network. Off for a hosted, multi-organization install (cloud mode's default; `doctor` warns if it is on there). | app, worker | No |
| `SOFTWARDEN_SMTP_ANY_PORT` | `1`; `0` in cloud mode | Whether an organization's own SMTP settings may name any port. Off, only 25, 465, 587 and 2525 (or the security setting's usual port) are accepted, so a test connection can't probe other ports on the server's network. | app | No |

## Secrets and keys

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `SOFTWARDEN_SECRET_KEY` | the generated `django_secret_key` | Django's secret key. The app refuses to start without one when debug is off. | init, app, worker | **Yes** |
| `SOFTWARDEN_CREDENTIAL_KEYS` | the generated `credential_keys` | Fernet keys, comma-separated and newest first. The first encrypts; every key decrypts. | init, app, worker | **Yes** |
| `SOFTWARDEN_CREDENTIAL_KEYS_FILE` | empty | Path *inside the containers* to a file of keys, one per line. It wins over the variable above. Mount the file into `app`, `worker` and `init` yourself. | init, app, worker | No (a path; the file is) |
| `SOFTWARDEN_SECRETS_KEY` | empty | An older key from before `SOFTWARDEN_CREDENTIAL_KEYS`. It still decrypts what it encrypted and never encrypts anything new. | app, worker | **Yes** |
| `SOFTWARDEN_SECRETS_DIR` | `/data/secrets` | Where the generated secret files are. | init, app, worker | No |
| `SOFTWARDEN_DB_SECRETS_DIR` | unset (`/data/db-secrets` in compose) | The database container's own secrets volume: `init` copies `postgres_password` there and nothing else, so the db container never mounts the other secrets. Unset, no copy is made. | init | No |
| `SOFTWARDEN_PGDATA` | `/pgdata` | The database volume, mounted read-only into `init`, so it can tell a new database from an existing one. | init | No |
| `SOFTWARDEN_OPS_TOKEN` | the generated `ops_token`, else empty | Bearer token for `/healthz/ops`. On a single-organization installation an Admin session also opens it. | init, app, worker | **Yes** |
| `BOOTSTRAP_ADMIN_PASSWORD` | empty | The first Admin's password for `bootstrap_admin --create --no-input` (unattended setup). In cloud mode, the platform staff account's. | app (command) | **Yes** |
| `BOOTSTRAP_ADMIN_EMAIL` | empty | With `bootstrap_admin --create --no-input`: the first account's email when `--email` isn't given (for a Helm Job or other unattended run). `--email` wins. | app (command) | No |
| `BOOTSTRAP_ADMIN_NAME` | empty | As above, the display name when `--name` isn't given. `--name` wins. | app (command) | No |
| `SOFTWARDEN_BOOTSTRAP_TOKEN_FILE` | Docker: `/data/bootstrap/setup-token`. Outside: `apps/api/.bootstrap/setup-token` | The private file holding the one-time first-run setup token. | app | No (a path) |

## Database

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `POSTGRES_PASSWORD` | the generated `postgres_password`. Outside Compose: none, and start-up stops without one; `softwarden-dev` only with `SOFTWARDEN_DEBUG=1` | The database password. `init` copies it into the secrets volume while no database exists yet. Once a database exists, a different value stops start-up. `doctor` fails on `softwarden-dev` outside development. An install using passwordless libpq authentication (peer or trust over a socket, client certificates) sets any non-empty value, which that authentication ignores. | init, app, worker | **Yes** |
| `POSTGRES_HOST` | Docker: `db`. Outside: `127.0.0.1` | The database host. | app, worker | No |
| `POSTGRES_PORT` | Docker: `5432` inside the stack. Outside: `5434` | Outside Docker, the port the API connects to. In `compose.dev.yaml` it sets the loopback host port the `db` service is published on (default `5434`); `compose.yaml` doesn't publish the database. The app inside the stack always uses 5432. | app, worker, compose | No |
| `POSTGRES_DB` | `softwarden` | The database name. The bundled `db` service always creates `softwarden`, so change it only for an external PostgreSQL. | app, worker | No |
| `POSTGRES_USER` | `softwarden` | The database role, as for `POSTGRES_DB`. | app, worker | No |
| `SOFTWARDEN_DB_CONN_MAX_AGE` | `60`; `0` under `SOFTWARDEN_DEBUG` (except in cloud mode) | Seconds a connection is kept for reuse; a kept connection is checked before reuse. 0 opens a new one per request, which costs a TCP and authentication handshake (TLS too for a managed server) and leaves a socket in TIME_WAIT: under load that ran Windows hosts short of ephemeral ports. Each gunicorn thread keeps its own, and the age is checked only when a request starts and ends, so an idle thread keeps it indefinitely: an app container settles at `SOFTWARDEN_WORKERS` x `SOFTWARDEN_THREADS` (24 by default) plus a few for the worker. Keep PostgreSQL's `max_connections` (100 in the bundled `db`) above the total for every app replica. Development servers keep none, since `runserver` starts a thread per request. | app, worker | No |
| `POSTGRES_SSLMODE` | empty (libpq's `PGSSLMODE`, else `prefer`) | libpq's `sslmode` for the app's connection: `disable`, `allow`, `prefer`, `require`, `verify-ca` or `verify-full`. Use `require` or stronger for a managed server; cloud mode's `doctor` fails anything weaker. The worker's `pg_dump` and `pg_restore` get the same value. | app, worker | No |
| `POSTGRES_SSLROOTCERT` | empty | Path inside the containers to the server's CA bundle, for `verify-ca` and `verify-full`. Mount the file yourself (Helm: `extraVolumes`). | app, worker | No (a path) |
| `SOFTWARDEN_DB_JIT` | `off` | `off` sends `-c jit=off` for the app's sessions. Set `on` behind a pooler that refuses the `options` parameter (PgBouncer, Azure's built-in pooler); then run `ALTER ROLE ... SET jit = off` on the server instead. | app, worker | No |

libpq's own variables also apply, because the app (psycopg) and the worker's `pg_dump` and `pg_restore` all use libpq:

- `PGSSLMODE`, `PGSSLROOTCERT`, `PGSSLCERT` and `PGSSLKEY` for a server that requires TLS. The backup passes exactly these four to its tools, and no other inherited variable. `POSTGRES_SSLMODE` and `POSTGRES_SSLROOTCERT` win over the first two, for the app and the tools alike.
- `PGCONNECT_TIMEOUT`. `manage.py doctor` sets it to 5 seconds for its own run, unless it is already set.

Mount any certificate files into `app` and `worker` yourself. The Helm chart does this through `extraVolumes` and `extraVolumeMounts`.

A connection pooler (PgBouncer, a managed server's built-in pooler) must run in **session** mode for the app and the worker: both take session advisory locks (the backup lock, the Graph mail slots), which transaction pooling would leak ([the cloud guide](CLOUD.md#connection-poolers)).

Server settings on a managed PostgreSQL: never set `idle_in_transaction_session_timeout` below 20 seconds for the app's
role. Public signup sends its confirmation email inside its transaction (so a refused message rolls the new link back),
and holds it for up to 20 seconds against a slow SMTP server (`notify/transactional.DEADLINE`).

## Email (installation default)

An organization can send through its own Microsoft Graph or SMTP settings instead (Administration › Email settings). These variables apply to every organization that uses the installation default.

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `SOFTWARDEN_SMTP_HOST` | empty (email off) | The SMTP server. While it is empty, digests and reminders are recorded as not sent, and every action stays in Needs attention. | app, worker | No |
| `SOFTWARDEN_SMTP_PORT` | `587` | SMTP port. | app, worker | No |
| `SOFTWARDEN_SMTP_USER` | empty | SMTP user name. | app, worker | No |
| `SOFTWARDEN_SMTP_PASSWORD` | empty | SMTP password. It is never logged or returned. | app, worker | **Yes** |
| `SOFTWARDEN_SMTP_TLS` | `1` | Use STARTTLS. | app, worker | No |
| `SOFTWARDEN_EMAIL_FROM` | `Softwarden <softwarden@localhost>` | The From address. | app, worker | No |
| `SOFTWARDEN_EMAIL_BACKEND` | the SMTP backend while `SOFTWARDEN_SMTP_HOST` is set, else none | A Django email backend path. For development and tests only. | app, worker | No |

## Backups

Read by the worker's nightly backup. The app reads them too, to show backup status on the System page. Details are in [the backup runbook](04-development/backup-runbook.md).

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `SOFTWARDEN_BACKUP_DIR` | Docker: `/data/backups` (the `softwarden-backups` volume). Outside: empty (not configured) | Where backup sets are written. | worker, app | No |
| `SOFTWARDEN_BACKUP_KEY_FILE` | the generated `backup_key` | Path inside the containers to your own key file. Mount it into `worker` and `app`, mode 600, owned by uid 10001. | init, app, worker | No (a path; the file is) |
| `SOFTWARDEN_BACKUP_CRON` | `30 2 * * *` | When the nightly backup runs, as five-field cron in UTC. | worker, app | No |
| `SOFTWARDEN_BACKUP_KEEP_DAILY` | `14` | Daily sets kept. | worker | No |
| `SOFTWARDEN_BACKUP_KEEP_WEEKLY` | `8` | Weekly (Sunday) sets kept. | worker | No |
| `SOFTWARDEN_BACKUP_STALE_HOURS` | `26` | After this many hours with no good backup, the System page warns. | app, worker | No |
| `SOFTWARDEN_BACKUP_MIN_FREE_MB` | unset: the larger of 2 GB and 10 % of the disk | Free space that must remain on the backup disk after a set. | worker | No |
| `SOFTWARDEN_BACKUP_REQUIRE_MOUNT` | `1` | Refuse a destination that isn't a mounted volume. Set 0 for development only. | worker | No |
| `SOFTWARDEN_BACKUP_PG_DUMP` | `pg_dump` | The `pg_dump` command. | worker | No |
| `SOFTWARDEN_BACKUP_PG_RESTORE` | `pg_restore` | The `pg_restore` command, used to verify each dump. | worker | No |
| `SOFTWARDEN_BACKUP_OPENSSL` | `openssl` | The `openssl` command. | worker | No |

## Host scripts

`ops/backup.sh` and `ops/restore.sh` run on the host, not in a container, as the alternative to the worker's backups ([the backup runbook](04-development/backup-runbook.md)). They read these variables from the environment or from `ops/backup.env` (copy `ops/backup.env.example`); a variable already set in the environment wins over the file. The containers never read them.

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `BACKUP_DEST` | none (required by `backup.sh`) | The folder the backup sets are written to, ideally a mounted off-host share. | host scripts | No |
| `BACKUP_KEY_FILE` | none (required) | The key file the sets are encrypted with: mode 600, outside the project folder, never in `.env` or `BACKUP_DEST`. | host scripts | The file is |
| `BACKUP_KEEP_DAILY` | `14` | Daily sets `backup.sh` keeps. | host scripts | No |
| `BACKUP_KEEP_WEEKLY` | `8` | Weekly (Sunday) sets `backup.sh` keeps. | host scripts | No |
| `SOFTWARDEN_DIR` | the current folder | The Compose project folder (the one with `compose.yaml`) the scripts act on. | host scripts | No |
| `BACKUP_DATE` | today | `YYYY-MM-DD` to stamp a set with. For testing retention only. | `backup.sh` | No |
| `SOFTWARDEN_ALLOW_NOACL_KEY` | unset | `1` accepts a key file whose mode can't be checked, under Git Bash on Windows only (NTFS keeps no Unix modes; the folder's Windows ACL must protect the key). Ignored on Linux, where the mode check always applies. | host scripts | No |

The scripts also pass `COMPOSE_PROJECT_NAME`, `COMPOSE_FILE` and `COMPOSE_PROFILES` through to `docker compose`, which reads them as [Docker documents](https://docs.docker.com/compose/how-tos/environment-variables/envvars/).

## Attachments

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `SOFTWARDEN_MEDIA_ROOT` | Docker: `/data/media` (the `softwarden-media` volume). Outside: `apps/api/media` | Where attachment files are stored. | app, worker | No |
| `SOFTWARDEN_ATTACHMENT_MAX_MB` | `25` | The largest attachment. Above 25, raise `SOFTWARDEN_MAX_REQUEST_MB` too. | app | No |
| `SOFTWARDEN_ATTACHMENT_EXTENSIONS` | `pdf,docx,xlsx,pptx,vsdx,doc,xls,png,jpg,jpeg,gif,txt,csv,md,json,zip` | Accepted types. Only types with a content check are allowed, so never html or svg. | app | No |
| `SOFTWARDEN_ATTACHMENT_QUOTA_MB` | `10240` | Storage per organization. 0 means no quota. | app | No |
| `SOFTWARDEN_ATTACHMENT_RETENTION_DAYS` | `7` | Days a deleted attachment's file is kept before the nightly sweep removes it. | worker | No |
| `SOFTWARDEN_ATTACHMENT_UPLOAD_RATE` | `60/hour` | Uploads per user. | app | No |

## Rate limits

DRF rate syntax: `n/sec`, `n/min`, `n/hour` or `n/day`. Every limit is counted in the database, in fixed windows of the rate's period, so it is the installation's limit whatever the number of processes and pods. An empty value means no limit, except for the sign-in limit. Failed sign-ins are also limited per account, from any address: after five attempts each waits 1, 2, 4 ... seconds after the last, at most 5 minutes; the count lapses after 15 minutes with no attempt or on a successful sign-in (docs/API.md "Rate limits"; not configurable; `manage.py signin_limits forget <email> --reason ...` clears one account).

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `LOGIN_THROTTLE_RATE` | `10/min` | Sign-in attempts per client address. Also confirming a signup link, and (per account) asking to delete an organization or starting an impersonation. | app | No |
| `SOFTWARDEN_SSO_THROTTLE_RATE` | `120/min` | Single sign-on starts and callbacks (OpenID Connect) per client address. | app | No |
| `SOFTWARDEN_INVITATION_THROTTLE_RATE` | `30/min` | Invitation link previews and acceptances per client address. | app | No |
| `SOFTWARDEN_INVALID_TOKEN_RATE` | `30/min` | Refused API tokens (unknown, revoked, expired) per client address. Past it a refused token gets 429 instead of 401; a valid token is never refused for it. | app | No |
| `SOFTWARDEN_SIGNUP_THROTTLE_RATE` | `10/hour` | Public signups and confirmation resends per client address (cloud). | app | No |
| `SOFTWARDEN_PASSWORD_RESET_THROTTLE_RATE` | `10/hour` | Requests for a password reset link per client address. Using a link counts on `LOGIN_THROTTLE_RATE`. | app | No |
| `SOFTWARDEN_API_READ_RATE` | `1200/min` | API reads per token or session user. | app | No |
| `SOFTWARDEN_API_WRITE_RATE` | `240/min` | API writes per token or session user. | app | No |
| `SOFTWARDEN_SEARCH_RATE` | `600/min` | Global search requests per user. | app | No |
| `SOFTWARDEN_CALENDAR_FEED_RATE` | `60/hour` | Requests per renewals calendar feed. | app | No |
| `SOFTWARDEN_CALENDAR_FEED_ADDRESS_RATE` | `1200/hour` | Calendar feed requests per client address, across all feeds. | app | No |

## Sign-in

Each organization sets up its own single sign-on in Administration › Sign-in. The variables below are installation-wide.

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `SOFTWARDEN_REQUIRE_IF_MATCH` | on | On: a signed-in session's PATCH or DELETE (and every action that takes `If-Match`) without `If-Match` is refused with `428 /problems/precondition-required`, so a form that never read the record's ETag can't silently overwrite someone else's change. The web app sends the ETag from every edit form. API tokens (scripts, the MCP server) keep the unconditional write. `0` turns it off, for a browser-session client of your own that doesn't send `If-Match` yet ([API revisions](API.md#revisions-etag-and-if-match)). | app | No |
| `SOFTWARDEN_FORCE_LOCAL_LOGIN` | off | Break-glass. Hides and refuses every single sign-on route; password sign-in always works. See [the SSO recovery runbook](04-development/sso-recovery-runbook.md). | app | No |
| `SOFTWARDEN_SSO_DOMAIN_VERIFICATION` | off; on in cloud mode | A sign-in domain an organization claims stays pending (it finds no organization on the sign-in page and blocks nobody) until a DNS TXT record `_softwarden-challenge.<domain>` proves the organization controls it (`POST sso/settings/domains/verify`); unverified claims lapse after 14 days (removing a domain and adding it again makes a new claim, whose 14 days start again, so the lapse clears forgotten claims rather than capping how long one organization can keep one pending). Off: a claim applies at once and the first organization keeps the domain. | app | No |
| `SOFTWARDEN_DNS_RESOLVER` | `simulated` with DEBUG on, else empty | The resolver for those TXT lookups: `host` or `host:port`; empty uses the first `nameserver` in `/etc/resolv.conf`; `simulated` makes no network call (development and tests). Standard library UDP, then TCP for a truncated answer, 3 s per attempt; only an answer from that resolver's address and port to this exact question counts. `doctor` warns when it is `simulated` with `SOFTWARDEN_DEBUG` off. | app | No |
| `SOFTWARDEN_OIDC_TENANT` | empty | **Deprecated.** Legacy installation-wide Entra sign-in: the tenant GUID. Used only by a single-organization install that hasn't activated its own settings. | app | No |
| `SOFTWARDEN_OIDC_CLIENT_ID` | empty | **Deprecated.** The legacy sign-in app's client ID. | app | No |
| `SOFTWARDEN_OIDC_CLIENT_SECRET` | empty | **Deprecated.** The legacy sign-in app's client secret. | app | **Yes** |

## Cloud: deployment mode, signup and organization lifecycle

For a hosted, multi-organization service ([the cloud guide](CLOUD.md)). A self-hosted install leaves all of these at their defaults.

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `SOFTWARDEN_DEPLOYMENT` | `selfhosted` | `selfhosted` or `cloud` (anything else stops start-up). `cloud` is the hosted, multi-organization service: public signup and kept database connections default on, private service hosts default off, first-run setup creates the operator's platform staff account (no organization, no web setup), and `doctor` adds the hosted checks. It never sets an organization's edition: signup creates `cloud` organizations whatever the mode. Helm: `deployment`. Compose passes it through from `.env` for evaluating cloud mode on one host. Cloud mode is not supported for production in 1.0 ([the cloud guide](CLOUD.md#not-supported-for-production-in-10)). | app, worker | No |
| `SOFTWARDEN_SIGNUP_ENABLED` | off; on in cloud mode | Public signup at `/signup`: anyone can create an account and a new organization (edition `cloud`) after confirming their email. Needs email (`SOFTWARDEN_SMTP_HOST`); without it signup refuses with a clear problem. | app | No |
| `SOFTWARDEN_SIGNUP_DAILY_LIMIT` | `200` | While signup is on, at most this many organizations are created in 24 hours (counted from the database, so every pod agrees); past it signing up and confirming answer `503 /problems/signup-paused` and pending signups keep their links. `0` turns the ceiling off. | app | No |
| `SOFTWARDEN_ORG_DELETION_GRACE_DAYS` | `30` (at least 1) | Days between an Admin asking to delete their organization (capability `org.self_delete`) and the worker removing it. An Admin can cancel until then. | app, worker | No |
| `SOFTWARDEN_IMPERSONATION_MINUTES` | `30` (1 to 240) | How long one impersonation by platform staff lasts (platform admin console); it ends on the first request after this. | app | No |
| `SOFTWARDEN_PLATFORM_TOTP` | off; on in cloud mode | Platform staff need a code from an authenticator app (TOTP) as well as their password before the platform console opens, and again to start an impersonation (platform admin runbook, "Second factor"). Turn it on for a self-hosted installation shared by organizations that don't trust each other. | app | No |
| `SOFTWARDEN_TENANT_BACKUP_RETENTION` | `7` (1 to 90) | How many nightly backups of each organization are kept (tenant backups, capability `org.tenant_backups`, in the cloud edition and the reserved `enterprise` value); older backup rows and files are removed. Each backup holds the organization's attachments, so the media volume needs about this many times each such organization's data plus attachments (the platform admin runbook, `docs/04-development/platform-admin-runbook.md`, "Tenant backups"). | app, worker | No |

## Notifications and webhooks

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `SOFTWARDEN_WEBHOOK_MAX_ATTEMPTS` | `6` | Attempts per webhook event delivery, from 1 to 20. | worker | No |
| `SOFTWARDEN_WEBHOOK_DISABLE_AFTER` | `5` | An endpoint turns off after this many failed deliveries in a row. 0 means never. | worker | No |
| `SOFTWARDEN_DELIVERY_RETENTION_SENT_DAYS` | `30` | Days sent notification deliveries are kept. | worker | No |
| `SOFTWARDEN_DELIVERY_RETENTION_FAILED_DAYS` | `90` | Days failed or skipped deliveries are kept. | worker | No |

## End-of-life data (endoflife.date)

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `SOFTWARDEN_EOL_ENABLED` | `1` | Whether the worker's daily `eol.refresh` job reads `https://endoflife.date/api/v1` (the product list, then the release cycles of the products some organization tracks) and whether a new mapping queues its product. Set `0` on an air-gapped install and load a saved snapshot with `manage.py eol_import` instead ([operations](04-development/operations.md#end-of-life-data-endoflifedate)). The screens always read the cached data, never the network. | app, worker | No |

## Preview features (off by default)

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `SOFTWARDEN_PREVIEW_FEATURES` | off | Turns on the preview features for every organization: Okta and Google Workspace discovery (capabilities `discovery.okta` and `discovery.google`, in every edition but still in preview), new Microsoft Entra license usage connectors and ServiceNow and Halo ticket connections (capability `preview`). Off, they are hidden whatever an edition or override says: their routes answer 403, the schedules skip them, and stored connectors, connections and links are kept for when it is turned on ([capabilities](CAPABILITIES.md#preview-features)). | app, worker | No |

## The assistant (off by default)

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `ANTHROPIC_API_KEY` | empty | The installation's Anthropic key, for organizations that enter none of their own. It is used only while `SOFTWARDEN_ASSISTANT_INSTALLATION_CAP_USD` is set. | app, worker | **Yes** |
| `SOFTWARDEN_ASSISTANT_MODEL` | `claude-sonnet-5-5` | The Claude model used for suggestions. | app, worker | No |
| `SOFTWARDEN_ASSISTANT_PRICES` | empty | Price overrides as JSON, in USD per million tokens, for a model that isn't built in. | app, worker | No |
| `SOFTWARDEN_ASSISTANT_INSTALLATION_CAP_USD` | empty | Monthly ceiling per organization on the installation's key. While it is unset, the key is never used. | app, worker | No |

## Processes, logs and images

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `SOFTWARDEN_DEBUG` | `0` (the image sets `0` too) | Development mode: `1` turns it on (the development secret key and credential key, the sign-in simulators, and the simulated discovery and ticket connections). Set it for local development only (README, Run it locally); never on a server. Off, the API refuses to start without `SOFTWARDEN_SECRET_KEY` or the generated key, and without a database password. On, it refuses to start with `SOFTWARDEN_DEPLOYMENT=cloud` or an explicit `SOFTWARDEN_SECURE_COOKIES=1` (which Compose and Helm set), so it can't be turned on by mistake in a real installation's `.env`. | app, worker | No |
| `SOFTWARDEN_WORKERS` | `3` | gunicorn worker processes. | app | No |
| `SOFTWARDEN_THREADS` | `8` | Threads per gunicorn worker process (`gthread`). Each thread may keep a database connection, so the app can hold `SOFTWARDEN_WORKERS` × this many (24 by default; plan `max_connections` for it: [the cloud guide](CLOUD.md), "Database connections"). The proxy in front must buffer request bodies (Caddy's `request_buffers`, an ingress's request buffering). Helm: `app.gunicornThreads`. | app | No |
| `SOFTWARDEN_WORKER_LONG_JOBS` | `2` | Long jobs (discovery runs) the worker runs at once, 1 or more. | worker | No |
| `SOFTWARDEN_SPA_DIR` | Docker: `/app/web`. Outside: `apps/web/dist` | The built single-page app. | app | No |
| `SOFTWARDEN_LOG_FORMAT` | `json` | `json` lines, or `text` for local reading. | app, worker | No |
| `SOFTWARDEN_LOG_LEVEL` | `INFO` | The root log level. | app, worker | No |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | empty (tracing off) | OpenTelemetry OTLP/HTTP endpoint. It needs the image built with `WITH_OTEL=1`. The SDK's other standard `OTEL_*` variables apply as it documents them. | app, worker | No |
| `OTEL_SERVICE_NAME` | `softwarden-web` or `softwarden-worker` | The service name in traces. | app, worker | No |
| `SOFTWARDEN_IMAGE` | `softwarden:local` | The image `init`, `app` and `worker` run. On a server, pin a release tag (see [the upgrade runbook](04-development/upgrade-runbook.md)). | compose | No |
| `SOFTWARDEN_MCP_IMAGE` | `softwarden-mcp:local` | The MCP server's image. | compose | No |

### Build arguments

Set with `docker build --build-arg NAME=value`, or `docker compose build --build-arg NAME=value`. They change what goes into the image, not how it runs.

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `WITH_OTEL` | `0` | `1` installs the optional OpenTelemetry trace export (the `otel` extra) into the image, for `OTEL_EXPORTER_OTLP_ENDPOINT`. The published images are built without it. | `Dockerfile` | No |

## The MCP server

Applies only with `docker compose --profile mcp up -d`, or to the `softwarden-mcp` command on a desktop. See [the MCP server guide](MCP.md).

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `SOFTWARDEN_MCP_UPSTREAM` | empty (`/mcp` answers 404) | Where Caddy sends `/mcp`. Set `mcp:8001` with the profile. | proxy | No |
| `SOFTWARDEN_URL` | Docker: `http://app:8000`. Desktop: required | The Softwarden address the MCP server calls. | mcp | No |
| `SOFTWARDEN_TOKEN` | empty | Desktop (stdio) only: the person's API token (`sw_...`). Over HTTP, each client sends its own. | mcp | **Yes** |
| `SOFTWARDEN_VERIFY_TLS` | `true` | `true`, `false` (development only) or the path of a CA bundle. | mcp | No |
| `SOFTWARDEN_HOST_HEADER` | Docker: `${PUBLIC_HOSTNAME}`. Desktop: none | The Host header sent while calling `app:8000` inside the stack. | mcp | No |
| `MCP_HOST` | Docker: `0.0.0.0`. Desktop: `127.0.0.1` | HTTP bind address. | mcp | No |
| `MCP_PORT` | `8001` | HTTP port. | mcp | No |
| `MCP_ENABLE_WRITES` | `0` | `1` registers the write tools. Each still needs a token that isn't read-only, and If-Match. | mcp | No |
| `MCP_ALLOWED_HOSTS` | Docker: `${PUBLIC_HOSTNAME}` with any port. Desktop: loopback names | Host headers accepted (the DNS rebinding guard). | mcp | No |
| `MCP_FORWARDED_ALLOW_IPS` | empty. Docker: `*`. Helm: `trustedProxies` | Proxies whose `X-Forwarded-For` names the client (addresses, CIDRs or `*`), so refused tokens are limited per client address. Empty and not bound to loopback: no per-address limit. | mcp | No |
| `MCP_ALLOWED_ORIGINS` | Docker: `https://${PUBLIC_HOSTNAME}` with any port. Desktop: loopback origins | Browser origins accepted. | mcp | No |

## Development and tests only

| Variable | Default | Purpose | Read by | Secret |
|---|---|---|---|---|
| `SOFTWARDEN_ALLOW_SIMULATED` | off (`compose.dev.yaml`: `1`) | Allows the simulated tenants and hosts outside development: the Microsoft tenant ID `simulated` (connections, discovery, Graph email) and `halo.`, `okta.`, `jira.` and `servicenow.simulated.invalid` and an administrator in `google.simulated.invalid`. They never leave the machine and fill the catalog with synthetic Contoso data, so a production install leaves this off. Off, and outside `SOFTWARDEN_DEBUG=1` and the Playwright server, a simulated value is refused on save (a field error naming this variable), at the connection test (`simulation_off`), and when a discovery run, a ticket refresh or a Graph email starts (the run fails with the reason); connections saved while it was on are kept and never run, and `doctor` warns about their connectors. `seed_demo` (all but `--accounts-only`), `seed_scale` and `simulate_entra_change` refuse without it, before writing anything. Set it for the Docker demo (README, "With the demo data"). It never turns on the OpenID Connect sign-in simulator, which stays DEBUG or Playwright only. | app, worker | No |
| `SOFTWARDEN_E2E` | empty | `1` turns on the Playwright-only routes and simulators, and only against a database whose name ends in `_e2e`. | app | No |

Not listed in the tables, because they aren't installation settings:

- the live-test credentials (`SOFTWARDEN_LIVE_*`, [DEVELOPMENT.md](DEVELOPMENT.md#live-tests-real-tenant-never-in-the-default-run) "Live tests");
- the setup scripts' `SOFTWARDEN_TLS_CHECK_HOST`;
- the Playwright ports (`apps/web`);
- the variables the PostgreSQL image reads (`compose.yaml` fixes them).
