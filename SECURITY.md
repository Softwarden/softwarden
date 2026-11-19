# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 1.x (the latest minor release) | Yes: security fixes are released as patch versions |
| Before 1.0 | No |

Fixes go into the latest 1.x minor release. Upgrade with the upgrade runbook (`docs/04-development/upgrade-runbook.md`) to get them.

## Reporting a vulnerability

Report a vulnerability privately, in either of two ways:
- **GitHub private vulnerability reporting on this repository** (preferred): open the Security tab and choose "Report a vulnerability";
- email to **security@softwarden.dev**.

Please don't open a public issue, discussion or pull request for it.

Please include:
- the version, or the commit if you run from source;
- the steps to reproduce it, and what an attacker gains (another organization's data, a higher role, a secret);
- a proof of concept against synthetic data only. Never use a real Microsoft tenant or real people's data.

**Response targets** (working days from the report):
- acknowledgment: 3 days;
- first assessment, with a severity and whether we accept it: 10 days;
- fix or mitigation: 30 days for critical and high severity, the next release for medium and low.

We'll keep you told of progress, agree a disclosure date with you, and credit you in the release notes unless you'd rather not be named.

## Scope

In scope:
- this repository's code: the API (`apps/api`), the web app (`apps/web`) and the MCP server (`mcp/`);
- the shipped deployment files: `compose.yaml`, `Dockerfile`, `docker/Caddyfile` and the scripts in `ops/` and `scripts/`.

Of particular interest: reading or changing another organization's data, getting a higher role than the one granted, getting a stored secret back out (connector credentials, SMTP passwords, API or calendar-feed tokens), bypassing the sign-in or SSO checks, and stored content running as script in the browser.

Out of scope:
- vulnerabilities in third-party dependencies with no demonstrated effect on Softwarden (report those upstream);
- an installation that has changed the shipped configuration, such as no TLS, DEBUG on, the simulators enabled or the app port published without the proxy;
- the development stack (`compose.dev.yaml`) and the local test identity providers, which are for development only;
- denial of service by volume, missing headers that have no demonstrated impact, and reports produced only by automated scanners.

## Security model in brief

**Tenancy and roles.** Every tenant table carries `organization_id`, and composite foreign keys keep rows in their organization at the database level. A cross-tenant test suite checks every model. Every endpoint declares its minimum role (Viewer, Editor, Admin), and a test fails for any route that doesn't. Writes go through service functions that check the organization and write an audit event in the same transaction.

**Secrets.**
- Secrets entered in the UI (connector credentials, SMTP passwords, assistant keys) are Fernet-encrypted under `SOFTWARDEN_CREDENTIAL_KEYS`. They are write-only: no API response, audit snapshot, export, log line or background-job argument contains them. As a safety net, every log line is scrubbed (`softwarden/logs.py`): query strings are cut, configured secrets are replaced in their plain and encoded forms, and secret shapes (URL userinfo, absolute URL paths, Bearer and Basic credentials, product tokens, `password=`-style pairs) become `[redacted]`. Caddy's error log drops the whole query string from the request URI.
- A stored secret is only sent where it was entered for. Changing the address a connection sends it to (a ticket, Halo or Okta connection's base URL; the SMTP host, port, or turning SMTP security off; the SSO provider, Okta domain or discovery URL) to another scheme, host or port is refused unless the secret is entered again in the same request. A path-only change, and name, notes or enabled edits, keep it. Microsoft and Google connections sign in at fixed hosts.
- An organization's own SMTP connection never falls back to the installation's SMTP user name and password (`SOFTWARDEN_SMTP_USER`, `SOFTWARDEN_SMTP_PASSWORD`): with no user name or no password saved, it doesn't sign in at all, even when its host is the installation's.
- API and calendar-feed tokens are stored as hashes and shown once, when they are created.
- The installation's own secrets are generated on first start into a private volume. The database container gets a copy of the database password alone, in a volume of its own.
- Rotation is in the README.

**Sign-in authority.** An API token never issues or changes sign-in authority: inviting someone, resending an invitation, resetting a member's password, changing a member's role or active flag, approving pending access, changing or activating single sign-on, changing the organization's sign-in settings (auto-provisioning, SCIM's roles) and changing your own password all need a signed-in session (an API token, Admin and read-write included, gets `403`), so a leaked token can't become a browser session that outlives it. Every check of a current password (changing it, re-authenticating to delete the organization) counts with the account's failed sign-ins below and is audited when wrong. An Admin can't reset their own password. When an Admin is demoted, deactivated or removed, the API tokens they made for others and their open invitations are revoked, and their SCIM tokens stop working (`doctor` warns about such tokens). A password reset or change revokes the account's calendar feeds.

**Single sign-on linking.** A returning identity is matched on its pinned issuer and subject only. A first sign-in links an existing member by email only where the provider vouches for the address: Entra, a member of the pinned tenant (never a guest's email or `preferred_username`); Okta and Google, `email_verified`; the generic OpenID Connect preset, `email_verified` true too. Anything else waits in pending access for an Admin. The generic preset refuses Microsoft Entra ID's sign-in hosts (national clouds included), so Entra always goes through the preset that checks the tenant.

**Development mode and the simulators.** The sign-in simulators sign anyone in as any email, so they run only with `SOFTWARDEN_DEBUG=1` or in the Playwright server, and only for their own reserved issuers (the synthetic Entra tenant, `*.simulated.invalid`), never for a real provider's. `SOFTWARDEN_DEBUG=1` refuses to start in cloud mode or with `SOFTWARDEN_SECURE_COOKIES=1`, which Compose and Helm set. Outside development the app refuses to start without a database password, and `doctor` fails on the development one. `seed_demo` runs only where simulators are allowed or on a Playwright `_e2e` database, never resets an existing account's password or membership, and `doctor` and the System page warn while a demo account keeps the published password.

**Containers (Compose).** Only the proxy is published on the host; PostgreSQL is reachable only inside the Compose network (`compose.dev.yaml` publishes it on 127.0.0.1 for development). No container can gain privileges (`no-new-privileges`). The app, worker, migrate and MCP containers drop every Linux capability, run as an unprivileged user under a minimal init (`init: true`, so signals are forwarded and exited child processes reaped) and have a read-only root filesystem with a tmpfs `/tmp`. Backup set files are mode 0600 in 0700 folders. The image has no SUID or SGID files, no `pip` and no `uv`. The services that run as root keep only the capabilities they use: `init` (`CHOWN`, `DAC_OVERRIDE`, `FOWNER`), `db` (those three plus `SETUID` and `SETGID`, to drop to the postgres user) and the proxy (`NET_BIND_SERVICE`). Every service has a memory and a process limit (`docs/04-development/operations.md`, "Container limits"). The Helm chart sets the equivalent pod security context.

**HTTP headers.** The app sends these itself on every response (`apps/api/softwarden/security_headers.py`), so they are there whatever proxy is in front:
- `X-Frame-Options: DENY`.
- `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'`. Inline styles are allowed for the `<style>` elements that the UI library and the API reference inject at run time. Responses with a policy of their own keep only that one: attachment downloads (`default-src 'none'; sandbox`).
- `Permissions-Policy: camera=(), microphone=(), geolocation=()`, `Cross-Origin-Opener-Policy: same-origin`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`.
- `Strict-Transport-Security` on HTTPS requests, only when `SOFTWARDEN_HSTS_SECONDS` is set.

On Compose, Caddy (`docker/Caddyfile`) is the only published entry point. It terminates TLS, sends the same CSP, `Permissions-Policy`, `Referrer-Policy` and COOP only where a response has none (so in practice on Caddy's own responses), sends `Strict-Transport-Security` (one year) for any host name other than localhost, and removes the `Server` header (on the HTTP redirect too) and the `Via` header its proxy would add.

Under the Helm chart, TLS terminates at your ingress controller and Caddy isn't used. The headers above come from the app; the chart sets `SOFTWARDEN_HSTS_SECONDS` (one year, `ingress.hstsSeconds`) while the ingress has TLS. Don't add a CSP on the ingress: pages with their own policy would carry two. The chart's pods don't mount a service account token. The server timeouts and body limit below are Caddy's; under Helm, set your ingress controller's (the chart's ingress sets the body limit and request buffering for ingress-nginx).

**Server timeouts.** Caddy uses the following timeouts:
- 10 s to read request headers;
- 5 minutes to read a whole request, which is enough for the largest upload at about 1 Mbit/s;
- 10 minutes to write a response;
- 2 minutes for idle keep-alive connections.

Caddy refuses a request body over `SOFTWARDEN_MAX_REQUEST_MB` before it reaches the app. The app refuses an oversized multipart request before reading it. Caddy holds each body in memory until it has all of it, before the app authenticates anyone, and the stock build has no per-address limit: an unauthenticated client can use up the proxy's memory limit and get it restarted, nothing more. Add a per-address connection limit in front of an internet-facing proxy (`docs/04-development/operations.md`, "Container limits").

**Rate limits.** All limits are counted in the database, so each is the installation's whatever the number of processes and pods, and answer `429` with `Retry-After`. The defaults can be changed by environment variable:

| What | Default | Keyed on |
| --- | --- | --- |
| Password sign-in (and confirming a signup) | 10/min (`LOGIN_THROTTLE_RATE`) | client address |
| Failed sign-ins for one account (and wrong current passwords: password change, organization deletion) | 5 free, then a wait of 1, 2, 4 ... seconds (at most 5 minutes) after each attempt; lapses after 15 quiet minutes or a successful sign-in (fixed in code) | the email, from any address |
| Single sign-on start and callback (OIDC) | 120/min (`SOFTWARDEN_SSO_THROTTLE_RATE`) | client address |
| Invitation links | 30/min (`SOFTWARDEN_INVITATION_THROTTLE_RATE`) | client address |
| Refused bearer tokens (API) | 30/min, then `429` instead of `401` (`SOFTWARDEN_INVALID_TOKEN_RATE`) | client address |
| First-run setup token | 5 wrong tokens per 5 minutes (fixed in code) | client address |
| API reads / writes | 1200/min / 240/min (`SOFTWARDEN_API_READ_RATE`, `SOFTWARDEN_API_WRITE_RATE`) | API token, else user |
| Search | 600/min (`SOFTWARDEN_SEARCH_RATE`) | user |
| File uploads | 60/hour (`SOFTWARDEN_ATTACHMENT_UPLOAD_RATE`) | user |
| Calendar feed | 60/hour per feed, 1200/hour per address (`SOFTWARDEN_CALENDAR_FEED_RATE`, `SOFTWARDEN_CALENDAR_FEED_ADDRESS_RATE`) | feed token, client address |

Counters are kept in the database under a keyed hash (HMAC-SHA256 under the installation's secret key) of the address, email or token they count. The per-account delay is an accepted availability trade-off: anyone who knows an email can make its owner wait up to five minutes between sign-in attempts. Unknown emails are delayed the same way, so the delay says nothing about which emails have accounts. An operator clears one account with `manage.py signin_limits forget <email> --reason ...` (audited).

The client address is the peer address. `X-Forwarded-For` counts only when the request came from `SOFTWARDEN_TRUSTED_PROXIES` (the Compose proxy), so a forged header doesn't buy a new allowance.

**Uploads.**
- An attachment's extension must be on the allowed list, and its content must match that extension: PDF, Office Open XML and legacy Office, PNG, JPEG, GIF, ZIP (with a limit on the number of entries), or UTF-8 text.
- The browser's content type is ignored.
- HTML, SVG, scripts and programs can never be allowed.
- Files are stored outside any static route. They are downloaded only through a role-checked endpoint, always as attachments, with the content type the check found, `nosniff`, the sandboxing CSP and `Cache-Control: private, no-store`.

**The media volume is not encrypted by Softwarden.** It holds attachment files and, under `exports/`, each organization's data exports and tenant backups as plain zips (mode 0600, folders 0700): the organization's records, audit log and member emails, never credential ciphertexts, token hashes or password hashes. Treat it like the database: only the app and worker containers (uid 10001) mount it; encrypt it at rest in the platform (an encrypted disk or a storage class with encryption, which managed RWX classes such as Azure Files, EFS and Filestore have on by default); keep host and cluster access to it to the operators who may read every tenant's data; and keep its snapshots under the same rules. The installation backup leaves `exports/` out and encrypts the rest. A full export is requested and downloaded only from a signed-in Admin session (an API token gets `403`), never while platform staff impersonate, and every download is audited.

**Discovery.** Microsoft connectors read only, with the permissions listed in `docs/PERMISSIONS.md`. Observations never overwrite curated data.

