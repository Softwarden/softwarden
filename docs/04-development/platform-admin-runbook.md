# Platform admin console runbook (cloud)

The platform admin console lets the people who run a hosted Softwarden see and support its organizations: every
organization with its usage, its edition, its scheduled deletion, a suspension, per-organization capability overrides
(feature flags) and impersonation of a member. It never adds a step to a customer's own journey: signup, invitations,
Entra, discovery and reminders work without it.

The API is under `/api/v1/platform/` (docs/API.md, "Platform admin console"). The SPA screens are a separate package.
Deploying the hosted service itself (managed PostgreSQL, scaling, backups, upgrades, `SOFTWARDEN_DEPLOYMENT=cloud`) is
[docs/CLOUD.md](../CLOUD.md).

## Who is platform staff

Platform staff is a separate list (`core_platformstaff`), changed only from the command line:

```bash
docker compose exec app python manage.py platform_staff grant ops@example.com    # the account must exist and be active
docker compose exec app python manage.py platform_staff list
docker compose exec app python manage.py platform_staff revoke ops@example.com
```

Each grant and revoke is written to the platform audit stream (`staff.granted`, `staff.revoked`). With the second
factor on (cloud mode), have the new staff member set it up right after the grant (Second factor, below): until they
do, whoever first signs in with the password sets it up. From the host, `platform_staff enroll <email>` prints the
secret once (an `otpauth://` URI) to hand over in person; their first code at the console confirms it. Django's `is_staff`
and `createsuperuser` play no part: an account is staff only through this command, or through first-run setup in cloud
mode.

**The first staff account on a new cloud installation.** `platform_staff grant` needs an existing account, and a fresh
installation has none. In cloud mode (`SOFTWARDEN_DEPLOYMENT=cloud`) first-run setup has no web page and no token:
`manage.py bootstrap_admin --create` asks for an email, a display name and a password and creates that account as
platform staff in no organization (`staff.granted`, `via: bootstrap`), then closes setup (docs/CLOUD.md, First run). In
cloud mode `manage.py doctor` fails until at least one active staff account exists.

Staff status gives **no role in any organization**. A staff account with no membership signs in at the normal sign-in
page; the sign-in answers `platform/session`'s body, `auth/me` answers 403 (no organization) and only the platform
routes work. A staff account that is also a member of an organization works there with that membership's role, as
anyone else.

Platform routes need a signed-in **session**. An API token is always refused, even one owned by staff.

**Staff sign in with their password only.** No organization's Admin controls a staff account, even one that is a
member of that organization: its password reset, active flag and identity unlink are refused there (409, "belongs to
the service operator's staff"), and single sign-on never links or signs in a staff account (refusal code
`platform_staff`), since the organization's Admin controls its identity provider. The platform routes refuse a
session signed in through an organization's single sign-on and an account whose password must change first. Every
staff password sign-in is written to the platform stream (`staff.signed_in`, `staff.sign_in_failed`). Failed sign-ins
are slowed per account from any address (docs/API.md "Rate limits"), and in cloud mode the console also needs a
second factor (below).

## Second factor

With `SOFTWARDEN_PLATFORM_TOTP` on (the default in cloud mode; off by default self-hosted, where you can turn it on), a
staff account needs a time-based one-time code (TOTP: any authenticator app, such as Microsoft Authenticator, Google
Authenticator, 1Password or Bitwarden) as well as its password before the platform console opens. Until then the
session reaches only `platform/session` (its `second_factor` says `required`, `enrolled`, `verified`) and
`platform/second-factor`; every other platform route answers `403 /problems/second-factor-required` with `enrolled`
saying which step comes next.

In the console this is one page before anything else: **Set up your second factor** at the first sign-in (Show the
setup key, shown once as the base32 key and the `otpauth://` address; there is no QR image, so type the key into the
app or open the address on the device that has it; then the first code), and **Enter your authenticator code** at
every later sign-in. The Impersonate dialog asks for a fresh code too: one just used to sign in isn't accepted again.

1. **Enroll at the first console sign-in.** `POST platform/second-factor/enrollment` answers the secret once, as base32
   and as an `otpauth://` URI to show as a QR code. Add it to the authenticator app, then send a current code with
   `POST platform/second-factor/verify {code}`: that confirms the enrollment and opens the console in this session.
   Asking for a new secret before confirming replaces the first; once confirmed, enrollment is refused (409).
2. **Every later sign-in.** Sign in with the password, then `POST platform/second-factor/verify {code}`.
3. **Impersonation** needs a current code as well: `POST platform/impersonation {..., password, code}`.

A code is accepted once and within 30 seconds either side of now. Use the next code if you just used one (signing in
and impersonating within the same 30 seconds needs two codes). Every code is counted before it is checked; after five
wrong codes in a row further codes are refused (429, `Retry-After`) for that staff account until five minutes after
the last one counted, from every pod. An accepted code clears the count.

A staff member whose password attempts are being delayed by someone else's guesses (`Retry-After` on sign-in) can be
cleared with `manage.py signin_limits forget <email> --reason ...` (audited, `signin.failures_forgotten`).

The secret is stored encrypted under `SOFTWARDEN_CREDENTIAL_KEYS` (credentials slot `totp` on `core_platformstaff`),
re-encrypted by `rotate_credential_keys` like every stored secret, and never shown again, logged or written to the
audit stream. Each step is in the platform audit stream: `staff.second_factor_enrollment_started`,
`staff.second_factor_enrolled`, `staff.second_factor_verified`, `staff.second_factor_failed`.

Until the console has its own screens for this (the API is there; the SPA's enrollment and code pages come later),
a staff member in cloud mode enrolls and verifies through those routes with their signed-in session.

**A lost or replaced device.** Check the person's identity out of band (a call to a known number, your usual
identity check), then, on the host:

```bash
docker compose exec app python manage.py platform_staff reset-second-factor ops@example.com --reason "Lost phone; identity checked by call"
# Helm: kubectl exec -it deploy/softwarden-app -- python manage.py platform_staff reset-second-factor ...
```

The reason is required and goes into the platform audit stream (`staff.second_factor_reset`, no actor, `via: cli`).
The enrollment is cleared: every session it verified needs the second factor again, and the staff member enrolls
afresh at their next console sign-in. Their password is unchanged; if the device was stolen rather than lost, also
have them change the password. `platform_staff list` shows each staff account's second factor (set up, or not set
up). A staff member whose secret no configured credential key can open (the keys changed) is reset the same way.

## Organizations and usage

- `GET platform/organizations`: every organization, its edition, time zone, creation time, member count, last activity,
  deletion and suspension state, and the usage snapshot. Filter with `q`, `edition`, `deletion=scheduled|none`,
  `suspended=true|false`.
- `GET platform/organizations/{id}`: members (with user IDs, for impersonation), the edition's capabilities, the
  effective ones and the overrides, the usage snapshot plus counts taken now, and the newest platform events.
- `GET platform/capabilities`: every capability name you can grant or revoke, with the editions that include it
  (the override dialog's list).

The `platform.usage` schedule (hourly at :05) counts each organization's records by kind, members, connectors, live
API tokens, attachments and their bytes, exports, jobs finished in the last 30 days, last activity and last sign-in,
into `core_organizationusage`. A new organization shows `usage: null` until the next run. Per-organization limits
use the same counters (`catalog/usage.py`) but count live at create time, never from the snapshot.

## Actions

Every action needs a **reason** (up to 500 characters). Each is written to the platform audit stream and, because it
touches the organization, also to that organization's own audit log with you as the actor, `via: platform` and the
reason: its Admins see that the operator acted and why.

| Action | Route | Notes |
|---|---|---|
| Change the edition | `POST platform/organizations/{id}/edition {edition, reason}` | The same as `manage.py set_edition`, which still works. The same edition writes nothing. |
| Cancel a scheduled deletion | `POST platform/organizations/{id}/cancel-deletion {reason}` | 409 when none is scheduled. There is no platform delete: an organization goes only through its own deletion (export, grace period, purge). |
| Suspend | `POST platform/organizations/{id}/suspend {reason}` | Members get `403 /problems/organization-suspended` on every request but `auth/me` (which shows `organization_suspended`) and switching to another organization; API tokens and the calendar feed are refused too; the worker leaves the organization's jobs queued (discovery runs, notification deliveries, exports), and the per-organization schedules skip it (digests, notification rules, saved view subscriptions, reminders, contract terms, connector runs; its webhook events wait in the outbox), so nothing floods out on unsuspend. Nothing is removed. A scheduled deletion is paused: the purge skips a suspended organization even past its due date, and the response's `deletion.paused` says so. Its Admins can't cancel while suspended, so cancel it here if that is what they ask. |
| Unsuspend | `POST platform/organizations/{id}/unsuspend {reason}` | Held jobs run on the next worker tick. A paused deletion resumes with the time it had left when the suspension began, and at least a day (`organization.deletion_postponed`, reason `suspension`). |
| Grant or revoke a capability | `POST platform/organizations/{id}/capability-overrides {capability, effect, reason, expires_at?}` | `effect` is `grant` or `revoke`; the name must be one in docs/CAPABILITIES.md. Replaces an override of the same name. Applies from the next request. |
| Remove an override | `POST platform/organizations/{id}/capability-overrides/{override id}/remove {reason}` | The capability follows the edition again. |
| Set a limit | `POST platform/organizations/{id}/limits {limit, value, reason, expires_at?}` | `limit` is one of `records`, `other_records`, `connectors`, `members`, `attachment_bytes`, `api_rate_read`, `api_rate_write`, `invitation_emails_hour`, `invitation_emails_day`; `value` is the maximum (bytes for attachments, requests per minute for the rates, at least 1 for a rate: suspend an organization to stop it), `null` for unlimited. Replaces an override of the same limit; applies from the next request. A value below the current usage removes nothing: the organization's creates are refused (`409 /problems/limit-reached`) until it is under the limit again. |
| Remove a limit override | `POST platform/organizations/{id}/limits/{limit}/remove {reason}` | The edition's default applies again. 404 when there is none. |
| Make an export the customer asked for | `POST platform/organizations/{id}/exports {reason}` | For a customer who asks through support (their Admins can also export themselves). It appears in their export list as "Requested by the operator" (`purpose: operator`), even if `org.export` is revoked, and **only their Admins can download it**; no platform route returns the file, and you can't download it while impersonating. 409 while another export is being prepared. |

An expired override stays listed with `live: false` and no longer applies; remove it to tidy up. The same holds for
limit overrides.

**Sign-in domains**. In cloud mode an organization's sign-in domain is pending until its
DNS TXT record verifies it, and only one organization holds a verified domain; a refusal to verify never says another
organization holds it, so a customer who can't verify their own domain comes to support. Check who controls the
domain (ask them to publish the TXT record their Admin > Single sign-on shows), then use the console's **Sign-in
domains** page (`/platform/sign-in-domains?domain=`: look the domain up, then "Move to this organization…" on a
pending claim or "Release <domain>…", each with a reason) or these routes:

| Action | Route | Notes |
|---|---|---|
| See who claims a domain | `GET platform/sign-in-domains?domain=` | Every claim: the verified holder first, then pending ones with when they were made. |
| Move a domain | `POST platform/sign-in-domains/move {domain, organization_id, reason}` | The organization gets it verified (its pending claim is verified, or one is made); the previous verified holder loses it. Audited in both organizations and the platform stream (`sign_in_domain.moved`). The same as `sso_recovery --move-domain`. |
| Release a domain | `POST platform/sign-in-domains/release {domain, reason}` | Removes the verified claim (else the oldest pending one), so the rightful organization can verify it. 404 when nobody claims it. Audited (`sign_in_domain.released`). |

## Limits

`GET platform/organizations/{id}/limits` shows each limit's value now, the edition's default, whether you overrode it
and the usage counted now. The defaults are in `core/limits.EDITION_LIMITS`: community and enterprise unlimited,
cloud 5,000 records (archived included), 10 connectors, 50 active members, 5 GB of attachment files, 1,200 API reads
and 300 API writes a minute for the organization as a whole, and 20 invitation emails an hour and 100 a day through
the installation's mail server (5 and 20 until a second member is active). A new cloud customer's first steps (signup,
invitations, an Entra connector, discovery, the first renewal reminder) fit well within them.

**Invitation email budget.** A customer who needs to invite a whole team at once gets past the new-organization
cap, or the hourly or daily budget, when you set an override of `invitation_emails_hour` or `invitation_emails_day`
(with a reason; audited in both streams); any override of a budget replaces the new-organization cap for it. The usage
is the organization's `core_installationemail` rows in the window, counted in the database. An organization with its
own mail server in Email settings isn't counted.

What is checked at create time: records (a create, an import commit, which is refused whole before anything is
written, and creating a record from a discovery candidate), connectors, members (an invitation and its acceptance,
approving pending access, signup, and reactivating a deactivated member) and attachment uploads. Discovery's review queue is never
limited. The API rates are a bucket per organization on top of the per-token and per-user buckets; like every rate limit
it is counted in the database, so it holds across every app process and pod (docs/API.md "Rate limits").

## Tenant backups

Organizations with `org.tenant_backups` (cloud and enterprise, or a grant) get a nightly backup: the
`org.tenant_backups` schedule (01:10) queues a `backup` export for each such organization that isn't suspended or
scheduled for deletion, starting at a fixed offset within four hours (by its id), so the worker builds them one after
another rather than all at once. The newest `SOFTWARDEN_TENANT_BACKUP_RETENTION` (7, 1 to 90) ready backups of each
organization are kept, and as many failed ones; older rows and files are removed (`organization.backups_pruned` in its
audit log). The organization's Admins list and download them with their exports.

Disk: each backup is a zip of the organization's rows and **all its attachment files**, so an organization takes about N × (its database rows, compressed, plus its attachments) on the media volume, N being the retention. For example, an organization with 2 GB of attachments and 50 MB of compressed rows at the default 7 keeps about 14.4 GB of backups. Plan the media volume for the sum over organizations with the capability. The installation backup leaves `exports/` out of its media archive, so tenant backups don't multiply the installation sets. A tenant backup is the
organization's data export format; it is not the installation backup (`docs/04-development/backup-runbook.md`),
which stays the way to recover the whole service.

**Restoring one organization.** There is no import from an export yet (a later item). To bring back one tenant's
data, decrypt the installation backup set from before the loss and `pg_restore` it into a separate scratch database
(the steps `ops/restore.sh --rehearse` runs, in the backup runbook, but keeping the database instead of dropping it),
then copy that organization's rows across, or give the customer what they need from it. Never restore the installation backup over the live database for one tenant: every
other organization would lose its changes since then.

**A purge that keeps failing.** The `org.purge_deleted` schedule (every 15 minutes) takes up to five due organizations
per run, least recently tried first, so one that keeps failing doesn't stop the others. After three failures in a row
for one organization the schedule's run fails with `PurgeKeepsFailing` (System status, `GET platform/system`,
`/healthz/ops` shows `degraded`, `manage.py doctor` names the schedule) and the organization's `deletion.purge_failures`
in the console counts them. The worker log has the organization ids and the error (`organization purge failed`).
Fix the cause; the next clean run clears the alert. A suspended organization is never purged (its deletion waits).

**After a purge.** The purge removes the organization's rows, files and tenant backups, and erases the accounts that
belonged to it alone (email and username `retired+<id>@invalid`, name "Former member", password unusable, sign-in
identities and sessions removed); an account with another membership, or a platform staff account, is kept as it is. The platform
audit stream names members by user id only; `impersonation.started` rows written before 1.0 may still hold the member's email, and stay (the stream is append-only). A purged organization's data stays in the installation backups taken before the purge until those sets are pruned: with the default retention of 14 daily and 8 weekly sets, for up to eight weeks. The managed database's point-in-time
recovery keeps it for its own window. An erasure request for a purged organization is answered with these two dates;
restoring a set from that period brings the organization and its members' accounts back. The purge runs again by
itself when the restored row still has its due date; a set taken before the deletion was asked for has none, so after
such a restore schedule the deletion again.

## Impersonation

Use it to see what a member sees, or to fix something for them, when they ask.

1. Find the member's user ID in `GET platform/organizations/{id}` (`members`). Platform staff and deactivated accounts
   can't be impersonated, nor can a member of a suspended organization.
2. `POST platform/impersonation {organization_id, user_id, reason, password, code}` (`code` while the second factor is
   on): your own password, or, if your account
   signs in only with single sign-on, a sign-in in this browser within the last 15 minutes. It shares the sign-in
   rate limit.
3. In the same browser session you now act as the member, **in that organization only**, with their role: their
   other organizations aren't listed in `auth/me` and switching to one is a 404, as for a session signed in through
   an organization's single sign-on. `auth/me` carries `impersonation: {id, by, reason, started_at, expires_at}`; the SPA
   shows a permanent banner.
4. It ends when you stop (`DELETE platform/impersonation`), on the first request after
   `SOFTWARDEN_IMPERSONATION_MINUTES` (30 by default, 1 to 240), when you sign out, when your staff status is revoked,
   or when the member is deactivated, removed from the organization or made staff. Stopping puts your own session back
   exactly as it was (your current organization included). The session key and the CSRF token change at start and
   stop; a request another tab sent with the old key just before is answered as not signed in and leaves the new
   cookie alone (docs/API.md, Session key changes), so it doesn't sign the browser out. A write on the request that notices the end (expired, or access changed) is not run as you instead: it
   answers `409 /problems/impersonation-ended`; reload. When a read noticed the end instead (the banner's poll,
   another tab), the first write outside the console gets the same 409, once, however much later, so a form still
   open in the member's organization can't save into your own organization. Signing out is never refused (and clears
   it with the session); console writes (in `platform/`, Stop included) run and leave it in place.
5. Each impersonation has an id. The SPA sends it on every write as `X-Softwarden-Impersonation` (`none` when it
   isn't impersonating): JSON requests, uploads and imports alike. The value is the impersonation that tab is
   showing, never read from the shared session. A tab changes it only together with what it shows: when a background
   reload (its poll, another tab's message) finds a different impersonation, or none, the tab moves with it (home with
   the banner, or back to the console) and says why, so a form filled for one identity is never saved as another. On
   either 409 the tab reloads, moves the same way, and says the change wasn't made. Clients that don't send the
   header are checked as before. A tab left open from an earlier impersonation, or from before you stopped, gets
   `409 /problems/impersonation-ended` instead of writing as the new member or as you; a tab that doesn't know an
   impersonation started gets `409 /problems/impersonation-started`. Reload either. Stop and signing out always work.

Every write you make meanwhile is audited as the member **and** records `impersonated_by` (you); the organization's
audit log, its CSV and JSON lines exports show both. Background work you queue (Run now, an assistant batch) records
you on its audit events too. Start, stop and expiry are written to both streams.

**Closed by default.** While impersonating, a write is allowed only if `core/impersonation_routes.ALLOWED_WRITES` lists
it (the ordinary record, settings and discovery work of the member's role); any other write, and every route below
whatever the method (HEAD counts as GET), answers `403 /problems/impersonation-restricted` before the view runs. A new
write route fails the test suite until it is placed on one list or the other.

**Never allowed while impersonating** (`core/impersonation_routes.RESTRICTED`):

| Group | Routes |
|---|---|
| Passwords | `POST auth/password`, `POST users/{id}/reset-password` |
| Members | `PATCH users/{id}` (role, active flag), `DELETE users/{id}`, `POST people/{id}/anonymize` |
| Identities and sign-in | `DELETE users/{id}/identity`, `DELETE users/{id}/identities/{identity_id}`, `POST users` and `POST invitations` (invite), `POST invitations/{id}/resend`, `POST pending-access/{id}/approve`, `POST auth/login`, `GET auth/oidc/start`, `GET auth/oidc/callback`, `POST auth/invitations/accept`, `POST auth/signup/verify`, `POST auth/signup`, `POST auth/signup/resend`, `POST setup` |
| Single sign-on settings | `PATCH sso/settings`, `POST sso/settings/test`, `POST sso/settings/activate`, `POST sso/settings/disable`; and `PATCH organization` when the body has `sso_auto_provision` (`core/impersonation_routes.RESTRICTED_SETTINGS`; its other settings stay allowed) |
| Tokens | `POST api-tokens`, `POST calendar-feed`, `POST calendar-feed/rotate`, `POST api-tokens/{id}/revoke` |
| Secrets | `POST connections/microsoft`, `PATCH connections/microsoft/{id}`, `POST connections/halo`, `PATCH connections/halo/{id}`, `PATCH notification-settings/email`, `POST`/`PATCH notification-settings/teams-destinations[/{id}]`, `POST`/`PATCH notification-settings/webhook-destinations[/{id}]`, `POST webhooks`, `PATCH webhooks/{id}`, `POST webhooks/{id}/rotate-secret`, `PATCH assistant/settings`, and `DELETE` of `connections/microsoft/{id}`, `connections/halo/{id}`, `notification-settings/email`, `notification-settings/teams-destinations/{id}`, `notification-settings/webhook-destinations/{id}`, `webhooks/{id}`; `POST system/backup/acknowledge-key` |
| Data leaving the organization | `POST organization/exports`, `GET organization/exports/{id}/download`, `GET audit-events/export.csv`, `GET audit-events/export.jsonl`, and every bulk export: `GET {applications, components, ai-agents, contracts}/export.csv` and `.xlsx`, `GET license-pools/export.csv`, `GET reports/{license-utilization, renewals-calendar, spend}/export.csv`. Single-record reads stay allowed, a single attachment download included (`core/impersonation_routes.ALLOWED_READS`) |
| Organization deletion | `POST organization/deletion`, `DELETE organization/deletion` |
| The console | every platform route but `GET platform/session` and `GET`/`DELETE platform/impersonation`: stop first (so no impersonation inside an impersonation) |

## The platform audit stream

`GET platform/audit-events` (filters `organization`, `action`, `actor`): `staff.granted`, `staff.revoked`,
`staff.signed_in`, `staff.sign_in_failed`,
`organization.edition_changed`, `organization.deletion_cancelled`, `organization.suspended`,
`organization.unsuspended`, `capability.granted`, `capability.revoked`, `capability.override_removed`,
`limit.set`, `limit.override_removed`, `export.requested`, `impersonation.started`, `impersonation.stopped`, `impersonation.expired`, `backup.key_acknowledged`. Installation-wide: the events keep the
organization's ID after it is purged, and the acting account's email after the account is removed. Append-only in the
database (a trigger refuses UPDATE and DELETE; removing an account only nulls `actor`).

## The installation's System status

`GET platform/system` is the System page's `installation` view: the worker, every schedule's last run (including
`org.purge_deleted` and `platform.usage`), the nightly backup and its warnings, installation-wide jobs and every
warning. `POST platform/system/backup/acknowledge-key` confirms the generated backup key is copied off the host
(audited here and, with no actor and `via: platform`, in every organization). A tenant's Admin sees only the
`organization` view of `GET system` (their own jobs and secrets) and gets 403 on the acknowledgment.
Staff who are also an organization's Admin get the installation view there too. The console's **System** page
(`/platform/system`) shows it, with the acknowledgment behind its confirmation dialog.

## Instance-wide flags

There are none yet: nothing in the platform needs a flag that isn't a capability. Per-organization behavior is a
capability override.
