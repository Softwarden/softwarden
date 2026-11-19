# The Softwarden API

This guide is for anyone who scripts against Softwarden or connects another tool to it. Everything the web app does
goes through the REST API under `/api/v1/`, so anything you can do on screen you can also do with an API token.

This page covers what every endpoint shares: signing in and tokens, errors, rate limits, revisions, paging and
webhooks, plus the features whose rules are easiest to read in one place. For the routes one by one, open
**API reference** in the app (`/admin/api`), which draws this installation's OpenAPI 3 description. You can also
download that description at `GET /api/v1/schema?format=json` (any signed-in account; `?format=yaml` or no parameter
for YAML). [DEVELOPMENT.md](DEVELOPMENT.md#the-rest-api)'s "The REST API" section describes the resources.

The API and these documents use American spelling. A few identifiers keep a British spelling from before 1.0: the
paths `/reports/licence-waste` (with `/{sku_id}` and their `export.csv`), `/license-pools/{id}/licence-waste` and
`/contracts/{id}/licence-waste`, the discovery source `entra_licence_usage` (beside `entra_licenses`), the offboarding
footprint section `licences`, the status value `cancelled` (shown as "Canceled"), and export file names built from
them. They are part of the 1.0 API as written; use them exactly so (see Compatibility).

## Compatibility

Everything under `/api/v1/` that the OpenAPI schema of a 1.x release describes keeps working through 1.x: paths,
methods, parameters, field names and types, enum values, problem types and status codes. Within 1.x the API may add
routes, optional parameters, response fields, enum values and problem types, so a client ignores fields it doesn't
know and handles an enum value or problem type it doesn't know as "other". A field or route to be removed is first
marked `deprecated` in the schema and named in the CHANGELOG for at least one minor release, and goes only in the next
major version, under `/api/v2/`. Behavior the schema doesn't describe (the wording of `detail`, the order of rows a
list doesn't promise, rate-limit numbers) can change in any release. Routes under `/api/v1/e2e/` exist only in test
installations and are not part of the API.

## Signing in

**Sessions (the web app).** `POST /api/v1/auth/login` with `{"email", "password"}` sets the session cookie; Entra or
another OIDC provider signs in through `/api/v1/auth/oidc/start`; `GET /api/v1/auth/options?org=<slug>` gives the
button and its `start_url`. Every write (POST, PATCH, PUT, DELETE) from a session must carry the `X-CSRFToken` header with the
`csrftoken` cookie's value (`GET /api/v1/auth/csrf` sets it). Without it the write is refused with
`403 /problems/csrf-failed` and nothing changes.

`POST /api/v1/auth/login` itself, sent before there is a session, isn't CSRF-checked. What keeps another site from
signing a browser in to an account of its choosing (login CSRF) is that sign-in takes JSON only: a page on another
origin can send a cross-site POST only as a form (`text/plain`, urlencoded or multipart), which is
`415 /problems/unsupported-media-type`, and `application/json` needs a CORS preflight the API never grants
(`tests/test_admin.py` pins both). Keep it that way: don't add another parser or CORS headers to this route.

`GET /api/v1/auth/options?org=<slug>` or `?email=<address>` answers which sign-in button to show. An unknown
organization or domain answers the same as one with password sign-in only (`sso: null`), so it tells nobody which
organizations exist; it does say whether a known one uses single sign-on and with which provider, which the sign-in page
needs. Lookups (`org` or `email` given) count in the `sso` rate limit (`SOFTWARDEN_SSO_THROTTLE_RATE`, 120 a minute per address; then
`429`). The plain page load (no `org` or `email`) isn't counted.

**Session key changes.** The session key changes when you sign in, switch organization, change your password, sign
out, and when platform staff start or stop impersonating. The old key is kept for 120 seconds as a tombstone that holds
no session values, only the key that replaced it. A request still carrying it (another tab, a
poll, an upload sent just before) is answered as not signed in (`403 /problems/not-signed-in`, or the route's usual
refusal), and its response doesn't delete the session cookie, so it can't sign the browser out by wiping the new
cookie. Retry it with the current cookie. Such a request sets a cookie only when it starts a session of its own
(signing in, a single sign-on start, an invitation preview), as those always do. Races between two requests that both
read the session before either changed its key:

- An organization switch, a password change, or an impersonation start or stop that loses (another tab changed the key
  first, for example by signing out) answers `400 /problems/malformed-request` with the detail "This session was signed
  out or changed by another request (another tab) after this one began, so nothing was changed. Reload." Nothing is
  saved or audited: the password stays as it was and the switch doesn't happen. The server logs it at INFO, without a
  traceback.
- Any other request that changed session values and loses the session's save answers the same 400 with "Your change
  may have been saved, but this tab's session changed; reload before trying again.": its own changes may be committed,
  so reload and check before retrying.
- When two requests notice that an impersonation expired at the same time, the one that loses answers
  `409 /problems/impersonation-ended` (reload), and the end is audited once.
- Signing out always signs out, even when it races: if another request (a Stop or an organization switch in another
  tab, or a read noticing an expiry) changed the key after the sign-out read the session, sign-out still answers 204,
  deletes the cookie, and ends the session that the other request created for this browser. Only that race does this:
  a sign-out sent with a key that was already rotated away is not signed in (403) and ends nothing, and an API
  token's sign-out never ends a browser session through a cookie sent with it.

An unknown or expired cookie, or one rotated away longer ago, is deleted as before. The daily `sessions.clear` schedule
deletes expired sessions and tombstones.

**API tokens (scripts, the MCP server).** An Admin creates a token in Administration › API tokens, or with
`POST /api/v1/api-tokens` from a signed-in session: an owner (a member of the organization), a name, an expiry within
365 days and whether it is read-only (the default). The secret, `sw_` and 40 characters, is in that response only;
only its SHA-256 is stored. Send it as:

```
Authorization: Bearer sw_...
```

A token acts as its owner, with the owner's role, in the token's organization only. It is never read from a query
string or a cookie, and token requests need no CSRF header. An unknown, revoked or expired token, or one whose owner
is no longer an active member, is `401 /problems/invalid-token` with `WWW-Authenticate: Bearer`.

**Read-only tokens.** A read-only token may call every read its owner's role allows. Every write to a signed-in route
is refused with `403 /problems/read-only-token` ("This token is read-only."), before the role check. Public routes
(sign-in, setup) don't refuse it, and a method a route doesn't have answers 405 first.

**Session only.** API tokens (`/api-tokens`) and calendar feeds (`/calendar-feed`) are credentials themselves, so they
are managed from a signed-in session only: a token is refused there (403 `/problems/forbidden`), even on a read.

**Sign-in authority is session only.** Any API token, read-write and Admin included, is refused (403
`/problems/forbidden`, "This changes who can sign in ...") on every route that issues or changes sign-in authority,
before the role check: `POST /users` and `POST /invitations` (the link), `POST /invitations/{id}/resend`, `POST
/users/{id}/reset-password` (the temporary password), `PATCH /users/{id}` (role and active flag), `POST
/pending-access/{id}/approve`, every write under `/sso/settings` (`PATCH`, `test`, `activate`, `disable`,
`domains/verify`), `POST /auth/password`, and a `PATCH /organization` whose body has a sign-in setting
(`sso_auto_provision`, `scim_default_role`, `scim_role_groups`; the whole body is refused and nothing is saved, while
a body of other settings works with a token). A leaked token can otherwise become a browser session that outlives it.
Reads of those resources, removing a member (`DELETE /users/{id}`), unlinking identities and anonymizing a person
stay open to a token. The list is `core/impersonation_routes.SESSION_ONLY_ROUTES` (the sign-in categories of the
impersonation list, minus `TOKEN_MAY_USE` with a reason for each).

**Roles.** Every route declares its minimum role, and that declaration is what enforces it: Viewer reads,
Editor writes, Admin deletes records and runs administration. The OpenAPI summary of an operation names the role where
it isn't the default. A request that isn't signed in is `403 /problems/not-signed-in` (the session API has no
authentication challenge to send, so it isn't a 401).

## Password reset by email

Offered whenever the installation can send email (`SOFTWARDEN_SMTP_HOST`, or a configured backend); `GET
/api/v1/auth/options` reports `password_reset_enabled`. Without email both routes answer
`503 /problems/password-reset-unavailable` and an operator resets with `manage.py reset_admin`. Both routes are public,
need the CSRF token even with nobody signed in, and are throttled per client address.

- `POST /auth/password-reset {email}`: `202 {detail}`, the same answer for any email. An active account that signs in
  with a password and belongs to an organization is emailed `{SOFTWARDEN_BASE_URL}/reset-password#<token>` (single
  use, one hour, only its SHA-256 stored, the token in the fragment). Nothing is sent to an unknown address, a
  deactivated account, an account that signs in only through single sign-on (it has no password), platform
  staff (they reset through `manage.py reset_admin`), or an account sent a link in the last minute or three in the last
  hour (counted in the database). A message the server refuses changes nothing in the answer. The request does the
  same work for every address (it queues a `password_reset.send` job carrying the account's id or null, nothing
  else), and the worker decides and sends on its next tick, within about a minute. Rate
  `SOFTWARDEN_PASSWORD_RESET_THROTTLE_RATE` (`10/hour`).
- `POST /auth/password-reset/confirm {token, password}`: `204`. Sets the password (Django's validators; a refused one
  is `400` and the link stays usable), marks every open link of the account used, clears a forced password change, and
  ends every session of the account (each is signed out on its next request). Nobody is signed in: sign in with the
  new password. `404 /problems/password-reset-link-unavailable` for an unknown, used, replaced or expired link. A
  password changed or reset any other way also ends the open links. Sign-in rate.

Audited in each of the account's organizations: `user.password_reset_requested` (a link was sent; no actor) and
`user.password_reset_completed`.

## Signup and the organization lifecycle (hosted service)

**Signup** is off unless the installation sets `SOFTWARDEN_SIGNUP_ENABLED=1`, and needs email (`SOFTWARDEN_SMTP_HOST`)
to confirm addresses. `GET /api/v1/auth/options` reports `signup_enabled`. All three routes are public, need the CSRF
token even with nobody signed in, and are throttled per client address (the same peer and trusted-proxy rules as
sign-in):

- `POST /auth/signup {email, name, organization_name, password, timezone?}`: `202 {detail}`. Emails a link
  `{SOFTWARDEN_BASE_URL}/signup/verify#<token>` (single use, 24 hours, only its SHA-256 stored). Nothing is created
  yet. The answer is the same whether or not the email has an account; an existing account is emailed a short note
  instead of a link. Signing up again replaces the link. At most one message per address per minute and five per hour;
  an expired signup starts afresh. A refused message (503) keeps no new link and uses no cooldown. Names with line
  breaks or other control characters, or a web address or domain name (`https://`, `www.`, `example.com`), are
  refused. The new organization's slug is never a reserved word (`api`, `admin`, `scim`, `platform`, ...): "API"
  becomes `api-2`. Rate `SOFTWARDEN_SIGNUP_THROTTLE_RATE` (`10/hour`), shared with
  resend. A deactivated account that belongs to no organization (what a deleted organization leaves) doesn't hold its
  address: confirming retires the old account (it stays, inactive, as `retired+<id>@invalid`, for other
  organizations' history) and creates a fresh one.
- `POST /auth/signup/resend {email}`: `202 {detail}`, the same for any email; a new link, the old one stops working.
- `POST /auth/signup/verify {token, password}`: the password chosen at signup is asked again (so a stranger who
  signed up with your address can't hand you an account with their password). Creates the account, a new organization
  in the `cloud` edition and your Admin membership, signs this browser in and answers `me`. Sign-in rate.

Refusals: `404 /problems/signup-disabled` (off), `503 /problems/signup-unavailable` (on, but no email),
`503 /problems/email-failed` (the server refused the message just now), `404 /problems/signup-link-unavailable`
(unknown, used, replaced or expired link), `409` when the email got an account meanwhile, `503 /problems/signup-paused`
(signup and confirm, while `SOFTWARDEN_SIGNUP_DAILY_LIMIT` organizations, 200 by default, were created in the last 24
hours; counted from the database; a pending signup keeps its link and can be confirmed later).

**Inviting** (`POST /invitations`, `POST /invitations/{id}/resend`) also emails the link to the invitee, in every
edition: through the organization's own email channel when it has one (Graph or SMTP in its email settings), else the
installation's (`SOFTWARDEN_SMTP_HOST`), else not at all. `emailed` in the response says whether the server accepted
it. The copy-once `link` is returned either way. Accepting for an address held by a deactivated account in no
organization retires that account and creates a fresh one with the name and password given.

**Send budget and Subject.** Through the installation's own mail server (the organization has no mail server of
its own in Email settings), an invitation email's Subject is `Your invitation to Softwarden`, without the organization's
name, and each invitation or resend counts against the organization's `invitation_emails_hour` and
`invitation_emails_day` limits (cloud: 20 and 100, 5 and 20 until a second member is active; docs/CAPABILITIES.md
"Limits"). Past one, the create or resend is `409 /problems/limit-reached` naming it, and nothing is written or sent.
An organization name that reads as a web address (`://`, `www.`, a dotted host name) becomes "an organization" in the
body too. Through the organization's own mail server neither applies; where more than one organization shares the
installation, invitations use the installation's server whenever it can send, so the budget applies there. An
organization's SMTP settings can't name the installation's own host (`SOFTWARDEN_SMTP_HOST`): that would send through
the service's server without the budget (400 on `smtp_host`).

**Proof of the mailbox.** On an installation with more than one organization, or in cloud mode, the copy-once
`link` joins an account that exists (accepted while signed in as it) but can't create one: a new account is created
only through the link in the invitation email, whose token is the same token followed by an HMAC of it under the
installation's secret key (`SECRET_KEY`, with `SECRET_KEY_FALLBACKS` honored), so it proves the invited mailbox.
`POST /auth/invitations/preview` says which link it was given: `can_create_account` (true for the emailed link, and
for either link on a single-organization installation) and `account_exists`, which is `null` from a link that can't
create an account. Accepting with that link while nobody is signed in answers
`403 /problems/invitation-emailed-link-required`, whether the address has an account or not. Neither the invitation
(`POST /invitations` answers `201` for any address that isn't already a member here, a deactivated account elsewhere
included) nor the copy-once link's preview depends on whether the address has an account elsewhere. A
single-organization installation keeps the copy-once link as it was. The link that proves the mailbox only ever goes
through the installation's own mail server (`SOFTWARDEN_SMTP_*`), even for an organization with Graph or SMTP settings
of its own: that server is its Admin's, who could read the message there. A proof link a single-organization
installation sent through the organization's own server stops creating accounts as soon as a second organization
exists (the invitation records which server carried it; a resend then goes through the installation's server). Through
an organization's own server the
invitation carries the copy-once link and says it can't create an account. So an installation with more than one
organization whose own mail server isn't configured can't create accounts through invitations: configure
`SOFTWARDEN_SMTP_HOST`, or the operator creates them.

**Accounts an organization made.** An account created by approving pending access has no password
and was never proved by its address's owner. Until its owner proves the mailbox (it has no password, no
`MailboxProof` and isn't platform staff), it doesn't hold its address, however many organizations it belongs to: a signup for the address sends a confirmation link (not the "you already
have an account" note), and an emailed invitation link creates a new account. Confirming either retires the old
account in its organization: it is deactivated, its username and email become `retired+<id>@invalid`, its sign-in
identities are unlinked and its membership stays as history; that organization records `user.address_reclaimed`
(the old account and `via`, never the new owner). `POST /users/{id}/reset-password` refuses an account with no
password (`409`), as it refuses your own account (change your own password with `POST /auth/password` instead),
so no Admin can give such an account a password (`GET /users` says which members have none:
`has_password` false, null for an account shared with another organization), and approving pending access for a platform staff
account is `409 /problems/invitation-required`. Signed in as itself, such an account
joins another organization only with the emailed invitation link (which records its proof); with the copy-once link
the answer is `403 /problems/invitation-emailed-link-required`. The emailed link's preview names the organization that
made such an account, `organization_account: {organization, sign_in_url}`, so its owner can sign in there instead of
retiring it; the copy-once link's preview never does.

**Deactivating a member** (`PATCH /users/{id} {active: false}`) ends every session the
account has at once (`sessions_ended` in the `user.updated` event), so reactivating it later doesn't bring an old
cookie back: the person signs in again. An Admin who is demoted, deactivated or removed loses
what stood on their authority: the API tokens they made for other members and the invitations they made that are still
open are revoked (`token.revoked` and `invitation.revoked` with `reason` `creator_no_longer_admin` or
`creator_removed`; `api_access_revoked` and `invitations_revoked` in the event). Their API tokens are not revoked: they are refused while the account is
inactive and work again on reactivation. When an account is deactivated because it was compromised, revoke its tokens
too (`POST /api-tokens/{id}/revoke`) before reactivating it.

**Export all data** (Admin, capability `org.export`, every edition): `POST /organization/exports` answers `201` with
the export (`queued`); the worker writes a zip of `manifest.json`, `organization.json`, `users.jsonl`,
`data/<app>.<Model>.jsonl` for every model with an organization and `attachments/<id>/<name>`. Credential
ciphertexts and their key ids, token hashes and prefixes, password hashes, worker leases and sign-in identities are
never in it. `GET /organization/exports[/{id}]` shows it; `download_url` (`GET /organization/exports/{id}/download`,
audited) works for 7 days once it is `ready`. One export at a time (`409`; a nightly backup in flight doesn't
count). `purpose` says where an export came from: `export` (an Admin asked), `deletion` (the final export),
`backup` (the nightly tenant backup, below) or `operator` (the service operator made it because the organization asked
through support; `requested_by` is the operator's account). Every purpose is listed and downloaded the same way, by
Admins only, as far as the organization's capabilities allow: `export` needs `org.export`, `deletion` `org.export` or `org.self_delete`, `backup`
needs `org.export` or `org.tenant_backups`, and `operator` is always shown (any other is a 404). Asking for one
(`POST`) needs `org.export`; downloading is refused while platform staff impersonate (`403 /problems/impersonation-restricted`). Asking for one and downloading it need a signed-in session: an API token, read-only or not, gets `403` (listing and
status stay readable). The zips are not encrypted on the media volume (SECURITY.md, "The media volume").

**Tenant backups** (capability `org.tenant_backups`, the cloud edition): every night the worker makes a `backup`
export of each organization that has the capability and is neither suspended nor scheduled for deletion. Each
organization's backup starts at a fixed offset within a four-hour window, so a large installation doesn't build every
zip at once. Each backup holds the attachment files too, so N backups take about N times the organization's
data and attachments on the media volume; the installation backup leaves `exports/` out. A backup has
`expires_at: null`: the newest `SOFTWARDEN_TENANT_BACKUP_RETENTION` (7) ready backups are
kept, with as many failed ones, and older rows and files are removed (`organization.backups_pruned`). Restoring one
organization from a backup isn't built; see the platform admin runbook.

**Limits.** `GET /organization/limits` (any member) lists each limit with `limit` (null:
unlimited), the edition's `default`, `overridden` (the service operator set this organization's value) and `usage`
counted for the response (null for the API rates):

| Limit | Counts | Community (and `enterprise`) | Cloud |
|---|---|---|---|
| `records` | catalog records of every kind, archived included | unlimited | 5,000 |
| `other_records` | every other row people create, counted together: people, teams, departments, categories, vendors, tags, relationships and their types, custom fields, saved views, AI providers and models, contracts and their lines, license pools, renewals, notification rules and destinations, API tokens, import sources (`catalog/usage.OTHER_RECORD_MODELS`) | unlimited | 50,000 |
| `connectors` | discovery connectors | unlimited | 10 |
| `members` | active members | unlimited | 50 |
| `attachment_bytes` | stored attachment files, in bytes | unlimited | 5 GB |
| `api_rate_read` | API reads per minute, every token and user of the organization together | unlimited | 1,200 |
| `api_rate_write` | API writes per minute, the same way | unlimited | 300 |

A create that would pass a limit is refused with `409 /problems/limit-reached`, with `limit`, `maximum`, `usage` and
`adding` (how many the request would add): a record (`POST` of applications, components, integrations or AI agents,
or creating one from a discovery candidate), an import commit (checked for the whole batch before anything is
written, so an import that would pass the limit is refused whole and stays committable), a connector, an invitation or
its acceptance, approving pending access, reactivating a deactivated member (`PATCH users/{id} {active: true}`) and
an attachment upload.
In the OpenAPI description this problem has its own schema, `LimitReachedProblem` (`limit` is the limit's name,
`LimitNameEnum`; the shared Problem's `limit` is the too-many-events number), and those operations declare their
409 as `ProblemOrLimitReached`, one of Problem or LimitReachedProblem.
Discovery's review queue isn't limited, and updates and deletes never are. A limit lowered below the usage removes
nothing; creates are refused until the usage is under it again. The API rates are the organization's own bucket
(Rate limits, below).

**Delete the organization** (Admin, capability `org.self_delete`, cloud): `POST /organization/deletion {confirm,
password}` with the organization's slug typed in `confirm` and your current password (an account without one, single
sign-on only, must have signed in in this session within 15 minutes): `403 /problems/reauthentication-required`
otherwise; sign-in rate; session only (a token is refused). It schedules the deletion
`SOFTWARDEN_ORG_DELETION_GRACE_DAYS` (30, at least 1) days ahead and queues a final export; once the deletion is due no
new export can be asked for (`409`), and the purge waits for an export still being built. If the final export
fails, one more is queued at once (early in the grace period); only a second failure is final. The purge never removes
an organization whose final export became ready less than 7 days ago: it postpones `due_at` so the export can be
downloaded for 7 days (`organization.deletion_postponed`). Admins see the final export (`status`, `download_url`) in
`organization_deletion.final_export` of `GET /auth/me`.
`GET /organization/deletion` has the state (`available`, `scheduled`, `due_at`, `requested_by`, `grace_days`,
`backups_kept_days`, `export`); every member sees `organization_deletion` in `GET /auth/me` (and `deletion` in `GET
/organization`). `DELETE /organization/deletion` cancels until then. Afterwards the worker removes every row of the
organization (its audit log too) and its files; accounts left with no organization (and not platform staff) are
erased, not deleted: deactivated, email and username `retired+<id>@invalid`, name "Former member", password unusable,
sign-in identities removed. The platform keeps a tombstone (the former id, a hash of the
name, who asked and when). Backups taken before the purge keep the organization's rows, files and export zips until
backup retention removes those sets (`backups_kept_days`: 56 with the default 14 daily and 8 weekly sets), and
restoring one brings the organization back.

## Platform admin console (hosted service)

For the people who run a hosted Softwarden; customers never need it. Every route under `/api/v1/platform/` declares
the access kind `platform`: a signed-in **session** of platform staff (`manage.py platform_staff grant <email>`).
Anyone else gets `403 /problems/forbidden` ("Only platform staff can do this."), and an API token is always refused,
even one owned by staff, and so are a session signed in through an organization's single sign-on and an account
whose password must change. Staff status gives no role in any organization, and no organization's Admin can reset,
deactivate or unlink a staff account (409), nor sign it in through their single sign-on (code `platform_staff`). A staff account with no membership can sign
in (`POST /auth/login` answers `platform/session`'s body, with `platform_staff: true`); its `auth/me` is 403. Writes
need a `reason` (up to 500 characters) and are audited in the platform stream and, when they touch an organization,
in its own audit log as well (actor the staff account, `via: platform` and the reason in the payload). The runbook is
`docs/04-development/platform-admin-runbook.md`.

**Second factor.** With `SOFTWARDEN_PLATFORM_TOTP` on (the default in cloud mode), a staff session that signed
in with its password reaches only `platform/session` and the three `platform/second-factor` routes until it sends a
current TOTP code; every other platform route answers `403 /problems/second-factor-required` with `enrolled` (false:
set it up first). `POST platform/second-factor/enrollment` returns `{secret, otpauth_uri}` once (base32, and the URI
for a QR code; stored Fernet-encrypted, never shown again; 409 once set up). `POST platform/second-factor/verify
{code}` accepts a code once, within 30 seconds either side of now: it confirms a new enrollment and opens the console
in this session (400 for a wrong or used code; 429 after five wrong codes in a row, per staff account, until five minutes after the last).
`POST platform/impersonation` needs a current `code` as well. A lost device is reset by the operator with
`manage.py platform_staff reset-second-factor <email> --reason ...` (audited). Every step is in the platform stream
(`staff.second_factor_enrollment_started`, `_enrolled`, `_verified`, `_failed`, `_reset`).

| Route | What |
|---|---|
| `GET platform/session` | `{user, platform_staff, impersonation, second_factor: {required, enrolled, verified}}`: the staff account signed in (also while impersonating) |
| `GET platform/second-factor` | `{required, enrolled, verified}` (reachable before the second factor) |
| `POST platform/second-factor/enrollment` | a new TOTP secret, shown once (reachable before the second factor; 409 once set up) |
| `POST platform/second-factor/verify {code}` | accept a code: confirms the enrollment, opens the console in this session |
| `GET platform/organizations` | keyset pages (`PaginatedPlatformOrganizationList`) of `{id, name, slug, edition, timezone, created_at, member_count, last_activity_at, deletion, suspended_at, usage}`; filters `q`, `edition`, `deletion` (`scheduled` or `none`), `suspended` (`true` or `false`); `ordering` of `name`, `slug`, `created_at` |
| `GET platform/organizations/{id}` | the same plus `suspended_reason`, `members` (user ID, name, email, role, active, last_login, platform_staff), `capabilities` (`edition`, `effective`, `overrides`), `live_usage` (counted now) and `recent_events` |
| `POST platform/organizations/{id}/edition` | `{edition, reason}`; the same edition writes nothing |
| `POST platform/organizations/{id}/cancel-deletion` | `{reason}`; 409 when none is scheduled |
| `POST platform/organizations/{id}/suspend`, `.../unsuspend` | `{reason}`; 409 when already in that state |
| `GET`, `POST platform/organizations/{id}/capability-overrides` | the list; `{capability, effect, reason, expires_at?}` with `effect` `grant` or `revoke` answers 201 and replaces an override of that name |
| `POST platform/organizations/{id}/capability-overrides/{override_id}/remove` | `{reason}`; 204 |
| `GET`, `POST platform/organizations/{id}/limits` | `{limits, overrides}` (each limit now, its default and the usage counted now); `{limit, value, reason, expires_at?}` with `value` null for unlimited (an API rate is at least 1; suspend the organization to stop it) answers 201 and replaces an override of that limit |
| `POST platform/organizations/{id}/limits/{name}/remove` | `{reason}`; 204; 404 when there is no override of that limit |
| `POST platform/organizations/{id}/exports` | `{reason}`: an export the organization asked for through support (purpose `operator`); 201 with the export, 409 while another is being prepared. Only the organization's Admins can download it: no platform route returns the file |
| `GET platform/capabilities` | every capability name an override takes, `[{name, editions, gated}]`: the editions that include it (`community`, `cloud`, and the reserved `enterprise`) and whether community lacks it; read-only |
| `GET platform/audit-events` | keyset pages (`PaginatedPlatformAuditEventList`) of `{id, at, actor, actor_email, action, label, organization_id, details, request_id}`; filters `organization`, `action`, `actor` |
| `GET`, `POST`, `DELETE platform/impersonation` | the session's impersonation; start `{organization_id, user_id, reason, password}` (201); stop |

No platform response carries a secret: no credential ciphertexts, token hashes, password hashes, connector
configuration or session values.

**Usage** is the hourly snapshot of the `platform.usage` schedule (`usage: null` before its first run): `counts`
(`records` by kind, `records_total`, `members`, `active_members`, `connectors`, `api_tokens`, `attachments`,
`attachment_bytes`, `exports`, `jobs_30d`), `last_activity_at`, `last_sign_in_at`, `computed_at`.

**Capability overrides** apply from the next request: `capabilities` in `auth/me` and every capability gate are the
edition's set plus live grants minus live revokes. An unknown name is a field error. An override past its `expires_at`
is listed with `live: false` and no longer applies.

**Suspension.** While an organization is suspended its members get `403 /problems/organization-suspended` on every
request except `auth/me` (which shows `organization_suspended: {suspended_at}`), sign-out and switching to another of
their organizations; its API tokens and calendar feed are refused; the worker leaves its jobs queued. A
scheduled deletion is paused (the platform console's `deletion.paused`): nothing is purged while suspended, and on
unsuspend `due_at` becomes now plus the time that was left when the suspension began, at least a day.

**Impersonation.** Starting needs your password (or a single sign-on sign-in in this session within 15 minutes;
and, with the second factor on, a current `code`;
`403 /problems/reauthentication-required` otherwise; sign-in rate), a reason, the organization and the member's user
ID. Platform staff and deactivated accounts can't be impersonated (`403 /problems/impersonation-restricted`), nor a
member of a suspended organization (409). It lasts `SOFTWARDEN_IMPERSONATION_MINUTES` (30) and works in the same
browser session: you act as the member, in that organization only (their other organizations are hidden and
switching is a 404), with their role, and `auth/me` returns
`impersonation: {id, by, reason, started_at, expires_at}` for a permanent banner. Every write is audited as the member
with `impersonated_by` set to you (the audit log shows both). Closed by default: a write is allowed only when
it is on the list of writes allowed while impersonating; any other write, the routes that handle credentials, members,
identities, single sign-on (Organization settings' `sso_auto_provision`
included), tokens, secrets, exports and the organization's deletion (whatever the method), and every
platform route but the session and the impersonation's own status and stop, answer
`403 /problems/impersonation-restricted` (the list is in the runbook). A write on the request that notices the
impersonation ended answers `409 /problems/impersonation-ended` and doesn't run. When a read noticed the end instead (the web app's `auth/me` poll, another tab), the next write outside the console answers the same 409 once, however much later, so a form left open in the member's organization can't save into your own; signing out (which clears it with the session) and platform console writes (which leave it in place) are never refused that way.

**Which impersonation a tab is in.** Every impersonation has an `id` (`POST platform/impersonation` and `auth/me`
return it). Tabs share the session and the CSRF cookie, so a write may say which impersonation its tab believes in
with the header `X-Softwarden-Impersonation`: the id, or `none` when the tab believes it isn't impersonating. On a
write (reads, `auth/logout` and the platform routes, Stop included, ignore it):

- an id that isn't the session's current impersonation (an earlier one, one you stopped, or not an id) answers
  `409 /problems/impersonation-ended` and doesn't run. It leaves the after-read mark above in place: that mark
  protects a tab that doesn't send the header (an older version of the web app), which may still be open beside this one;
- `none` while the session is impersonating answers `409 /problems/impersonation-started` and doesn't run: the tab
  doesn't know about the impersonation; reload;
- the matching id, or `none` when not impersonating, runs. A tab that sends the header has said where it is, so the
  after-read mark (kept for clients that don't send it) neither refuses nor is cleared by its writes;
- without the header (API tokens, scripts, older clients) nothing changes.

A write on the request that notices the end is refused as before, whatever the header says. These checks run before
the CSRF check, so a cross-site write with a forged header gets this 409 rather than a 403; it still does nothing. It
ends when you stop, on the first request after it expires, when you sign out,
when your staff status is revoked or when the member becomes unavailable; your own session comes back exactly as it
was. The session key and the CSRF token change at start and stop: read the CSRF cookie again. Start, stop and expiry
are audited in both streams.


## Errors: problem details

Every error is [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem details, `Content-Type:
application/problem+json`, whatever the route or its success media type (CSV exports and the ICS feed included):

```json
{
  "type": "/problems/invalid-request",
  "title": "Invalid request",
  "status": 400,
  "detail": "Some fields need attention.",
  "errors": {"name": ["Enter a name."], "lifecycle": ["Choose a lifecycle."]}
}
```

- `type` names what went wrong; branch on it rather than on `detail`. It is one of the types below.
- `title` is the same for every problem of a type; `detail` says what happened this time and is safe to show.
- `errors` is `{field or query parameter: [messages]}` when the request had something to fix; `non_field` holds what
  belongs to no one field.
- Some types add extension members, listed with them. The OpenAPI `Problem` schema lists them and allows others.
- Input no field can hold is refused before anything reads it, as `/problems/invalid-request` naming the field or
  parameter: a NUL character (`\u0000` in JSON, `%00` in a URL) or an unpaired UTF-16 surrogate in any string of a
  body or any query parameter, a body nested more than 32 levels deep, and whole numbers that aren't ASCII digits
  (`²`) or have more than 18 of them. The message never repeats the value. A value the server still can't store or
  compute with is the same 400 with a generic `detail`; a 500 is a bug.
- A text field takes a string or null, and null (like `""`) clears an optional one. A number, boolean, list or
  object sent for a text field is a 400 on that field and changes nothing; it is never stored as empty. A whole
  number (`renewal_lead_days`, seats, a notice period) is refused as `true` or `12.5`, never read as 1 or 12. A choice
  (a tag color, a model's modality, a notice unit) takes one of its values or null, and a flag (`active`) takes
  `true` or `false` only: `0`, `"false"` or `[]` is a 400, never read as "clear" or as true.
- A field a PATCH or POST doesn't know is ignored, not refused (except on ticket links): check the response, which
  shows what was stored.

| Type | Status | When | Extension members |
|---|---|---|---|
| `/problems/invalid-request` | 400 | A field, filter, ordering, cursor or page size needs fixing. | `errors` |
| `/problems/malformed-request` | 400 | The body isn't valid JSON, or Django refused the request before routing. | |
| `/problems/too-many-events` | 400 | An audit export would hold more than one file's limit. | `count`, `limit` |
| `/problems/invalid-token` | 401 | The API token is unknown, revoked or expired, or its owner left. | |
| `/problems/invalid-credentials` | 403 | Sign-in with an email and password that don't match an active account. | |
| `/problems/not-signed-in` | 403 | No session and no token on a signed-in route. | |
| `/problems/forbidden` | 403 | The role can't do this, or a token used a session-only route. | |
| `/problems/read-only-token` | 403 | A read-only token tried to write. | |
| `/problems/csrf-failed` | 403 | A session write without a valid `X-CSRFToken`. | |
| `/problems/capability-required` | 403 | The organization's edition lacks a feature. | `capability` |
| `/problems/impersonation-restricted` | 403 | Platform staff impersonating a member asked for something an impersonation never does (credentials, identities, single sign-on, tokens, secrets, deletion, the platform console), or tried to impersonate staff or a deactivated account. | |
| `/problems/organization-suspended` | 403 | The current organization is suspended by the service operator. | |
| `/problems/second-factor-required` | 403 | Platform staff signed in with a password but haven't sent a TOTP code in this session. | `enrolled` |
| `/problems/limit-reached` | 409 | A create would pass one of the organization's limits (records, connectors, members, attachment bytes, invitation emails in the last hour or day). Schema `LimitReachedProblem`. | `limit` (the limit's name), `maximum`, `usage`, `adding` |
| `/problems/reauthentication-required` | 403 | Deleting the organization without your current password, or (single sign-on only) without a sign-in in the last 15 minutes. | |
| `/problems/not-found` | 404 | No such record (another organization's is the same; an ID that isn't a UUID says the same), or no route at that path under /api/. | |
| `/problems/invitation-unavailable` | 404 | An invitation link that is unknown, expired, used or revoked, or whose account is deactivated: one answer for all. | |
| `/problems/signup-disabled` | 404 | Public signup is off on this installation. | |
| `/problems/signup-link-unavailable` | 404 | A signup confirmation link that is unknown, used, replaced or expired. | |
| `/problems/password-reset-link-unavailable` | 404 | A password reset link that is unknown, used, replaced or expired, or whose account can't be reset any more. | |
| `/problems/method-not-allowed` | 405 | The route doesn't take that method (the `Allow` header lists those it does). | |
| `/problems/not-acceptable` | 406 | `Accept` names no media type the route can answer. | |
| `/problems/conflict` | 409 | The change conflicts with stored data: a refused delete, a state that doesn't allow it. | `blocking_references`, `blocking_names` on a refused delete |
| `/problems/impersonation-started` | 409 | The write said `X-Softwarden-Impersonation: none`, but platform staff started impersonating in this session since the tab loaded; nothing was run. Reload. | |
| `/problems/impersonation-ended` | 409 | Platform staff's impersonation ended (time limit or access changed) before this write, which wasn't run: on the request that noticed it, on the first write outside the platform console after a read noticed it, or a write whose `X-Softwarden-Impersonation` names an impersonation that isn't the session's current one. | |
| `/problems/duplicate-name` | 409 | Another record of the same kind (application, component, AI agent, integration) has that name; send `confirm_duplicate: true` to keep both. Names that must be unique (a vendor's, a team's, a connector's, a person's email) are a 400 `/problems/invalid-request` naming the field instead, with nothing to confirm. | `duplicate_of` |
| `/problems/duplicate-relationship` | 409 | That link is already recorded. | |
| `/problems/stale-value` | 409 | An expected value (Undo) no longer matches. | `fields` |
| `/problems/retirement-needs-acknowledgment` | 409 | Retiring something active records depend on. | `dependents`, `not_required` |
| `/problems/core-relationship-type` | 409 | A core relationship type can't be changed that way. | |
| `/problems/import-refused` | 409 | A CSV import commit was refused; nothing was saved. | `run` and row details |
| `/problems/catalog-code-invalid` | 400 | A catalog-as-code apply whose files have errors; nothing was written. | `file_errors` (each `file`, `line`, `path`, `message`) |
| `/problems/catalog-code-stale` | 409 | A catalog-as-code apply whose `plan_hash` no longer matches: the catalog or the files changed since the plan. Plan again. | |
| `/problems/reassign-stale` | 409 | A reassign sent with a dry run's `plan_hash` whose rows changed since that dry run (a record came to hold, or stopped holding, a chosen role). Nothing was saved. Preview again. | |
| `/problems/sso-draft-changed` | 409 | The single sign-on draft changed since you read it (test or activate named another `draft_revision`). | `revision` (the current draft revision) |
| `/problems/sso-test-required` | 409 | Activation of a draft without a passed test of its current revision. | |
| `/problems/sso-local-only` | 409 | `SOFTWARDEN_FORCE_LOCAL_LOGIN` is set: test sign-ins and activation are refused. | |
| `/problems/invitation-wrong-account` | 409 | Accepting an invitation while signed in as an account other than the invited email's. | |
| `/problems/invitation-required` | 409 | Approving pending access for an email whose account belongs to another organization (even if also a member here): invite them instead. | |
| `/problems/invitation-other-sign-in` | 409 | Accepting an invitation from a session signed in through another organization's single sign-on. | |
| `/problems/invitation-emailed-link-required` | 403 | Accepting an invitation with nobody signed in, from the link the inviting Admin was shown, where a new account needs the emailed link (more than one organization, or cloud mode). The same answer whether or not the address has an account. | |
| `/problems/length-required` | 411 | A multipart upload without `Content-Length`. | |
| `/problems/precondition-failed` | 412 | `If-Match` doesn't match the record's current revision. | `revision`, and the `ETag` header |
| `/problems/precondition-required` | 428 | A signed-in session's write that takes `If-Match` sent none (`SOFTWARDEN_REQUIRE_IF_MATCH`, on by default). Nothing was changed. | |
| `/problems/content-too-large` | 413 | The body is over the installation's limit (uploads: `SOFTWARDEN_MAX_REQUEST_MB`; other bodies: Django's `DATA_UPLOAD_MAX_MEMORY_SIZE`, 2.5 MB). | |
| `/problems/unsupported-media-type` | 415 | The body isn't JSON (or multipart where an upload is taken), or declares a charset other than UTF-8 (RFC 8259; US-ASCII, a subset, is accepted). | |
| `/problems/rate-limited` | 429 | A rate limit was reached; see the `Retry-After` header. | |
| `/problems/server-error` | 500 | Something failed that the API didn't expect: treat it as a bug and report it with the request's `X-Request-ID`. Input the API can't use is a 400 and an unreachable database a 503, never this. The response never carries the exception, a stack or a secret; the server log has it, under the same `X-Request-ID`. | |
| `/problems/database-unavailable` | 503 | The API can't reach its database: the connection was refused or lost, the server is shutting down or starting (SQLSTATE 08xxx, 57P0x), or it has no connections left (53300). Nothing was done; retry after the `Retry-After` header (30 seconds). A deadlock, a serialization failure, a full disk or out-of-memory is a 500, not this. | |
| `/problems/signup-unavailable` | 503 | Signup is on but the installation can't send email to confirm addresses. | |
| `/problems/email-failed` | 503 | The confirmation email couldn't be sent just now. | |
| `/problems/signup-paused` | 503 | The installation created its daily ceiling of new organizations (`SOFTWARDEN_SIGNUP_DAILY_LIMIT`); try later. | |
| `/problems/password-reset-unavailable` | 503 | The installation can't send email, so it can't reset passwords by email. | |

Under `/api/`, the errors Django answers itself are problems too: a path no route matches (404), an unhandled
exception (500), a request refused before routing (400) and a CSRF failure outside the API views.
Outside `/api/`, the web app answers every other path.

## Rate limits

Every signed-in request counts against one bucket:

- a request with an API token counts against **that token's** bucket;
- a session request counts against **the user's** bucket, shared by their browser tabs.

So one script that exhausts its token never slows the same person's other tokens or their browser. Every request
acting in an organization also counts against **the organization's** bucket, shared by all its tokens and users, at
its `api_rate_read` and `api_rate_write` limits (Limits, above; cloud 1,200 reads and 300 writes a minute,
community (and `enterprise`) unlimited by default, in which case this bucket is skipped). Reads (GET, HEAD, OPTIONS) and
writes count apart, and writes are stricter:

| Setting | Environment variable | Default |
|---|---|---|
| `API_READ_RATE` | `SOFTWARDEN_API_READ_RATE` | `1200/min` |
| `API_WRITE_RATE` | `SOFTWARDEN_API_WRITE_RATE` | `240/min` |

Rates use DRF's syntax (`n/sec`, `n/min`, `n/hour`, `n/day`); an empty value turns that limit off. Every limit is
counted in the database (core/counters.py), in fixed windows of the rate's period starting on the clock (a minute
limit resets at each whole minute), so it is the installation's limit however many processes and pods serve requests.
A request over the limit is refused with `429 /problems/rate-limited` and a `Retry-After` header (seconds until the
window ends). Wait that long, then retry.

These scoped limits apply on top: global search and `composite/search` (`SOFTWARDEN_SEARCH_RATE`, 600/min per user),
file uploads (`SOFTWARDEN_ATTACHMENT_UPLOAD_RATE`, 60/hour per user), sign-in (`LOGIN_THROTTLE_RATE`, 10/min per
address), single sign-on starts and callbacks (`SOFTWARDEN_SSO_THROTTLE_RATE`, 120/min per address), invitation
links (`SOFTWARDEN_INVITATION_THROTTLE_RATE`, 30/min per address), signup (`SOFTWARDEN_SIGNUP_THROTTLE_RATE`),
first-run setup (five bad tokens per address in a five-minute window) and
the calendar feed (`SOFTWARDEN_CALENDAR_FEED_RATE` per feed, `SOFTWARDEN_CALENDAR_FEED_ADDRESS_RATE` per address).
Requests that aren't signed in have no API bucket.

**Failed sign-ins per account.** Besides the address limit, every password attempt for an email is counted under
that email (whether or not an account has it), from any address, before the password is checked, so attempts sent
in parallel are decided one after the other. The first five cost nothing; after that each attempt must wait 1, 2, 4,
8 ... seconds (at most 5 minutes) after the last, and an attempt inside the wait is refused with 429 and
`Retry-After` before the password is checked, the right one included. The count isn't tied to clock-aligned windows:
it lapses only after 15 minutes with no attempt for that email, or when a sign-in succeeds. Ten failures take at
least 31 seconds, the eleventh can't come within a minute of the first, and sustained guessing gets one try every 5
minutes, however many addresses it comes from. Starting an impersonation counts the staff account's password the
same way, and so does every other check of a current password: changing your password (`POST
/auth/password`, `current_password`) and asking for the organization's deletion (`POST /organization/deletion`,
`password`) share the account's count with sign-in, a match forgets it, and each wrong one is audited as
`user.password_check_failed` (the purpose, never the password). Anyone who knows an email can make its owner wait up to 5 minutes between attempts (an accepted trade-off:
unknown emails wait the same, so the delay tells nobody which emails have accounts); an operator clears one account
with `manage.py signin_limits forget <email> --reason ...`, audited in the platform stream.

**Refused bearer tokens per address.** An API token that is unknown, revoked or expired, or whose owner is no
longer active, counts against the client's address. Past `SOFTWARDEN_INVALID_TOKEN_RATE` (30/min) such a request is
answered `429` instead of `401` until the window ends. A valid token is looked up first and never counted or
refused, so a script sharing an address with someone guessing keeps working.

**What a limit costs.** Each counted request writes one row with one statement
(`INSERT ... ON CONFLICT DO UPDATE ... RETURNING`; after the first request in a window it is an in-place update). A
signed-in API request pays one such write for its token or user bucket, plus one for the organization's when its
organization has an API rate limit (cloud). Search, uploads and the calendar feed add one each. A password sign-in
pays one write for the address and one short transaction for the account (insert if missing, lock and read the row,
update it), and a successful one also deletes the account's row. A valid API token pays nothing extra; a refused one
pays one write. Counter keys are a keyed hash (HMAC-SHA256 under the installation's secret key), so no address or
email is stored. Rows whose window ended, or whose count lapsed, are deleted every 10 minutes (`counters.prune`).

## Revisions: ETag and If-Match

Reading one record (any detail route) returns `ETag: "<revision>"`, a fingerprint of its stored fields (with a record's
extension and tags, a renewal's cycles, a contract's lines); every write returns the new one. A `201` that
creates a record with a URL of its own answers with the record, its `ETag` and `Location`, that URL: every
collection's `POST` (records, vendors, contracts, API tokens, attachments, organization exports, saved views, ...),
an invitation (`POST invitations` or `POST users`: the invitation's URL), a contract's renewal successor
(`POST contracts/{id}/renew`), a pool made from an unbound SKU, an import rerun, a ticket link (and a created
ticket), and your calendar feed (`calendar-feed`). The other `201`s have no `Location`, because what they create has
no URL of its own: a renewal's next cycle (`POST renewals/{id}/cycles` answers the renewal), an assessment snapshot
(`POST assessments/{id}/refresh` answers the assessment), a record created from a discovery candidate (the answer is
the observation; the record is its `decision.resource`), a link confirmation, and the platform console's capability
and limit overrides, organization exports and second-factor enrollment. Send the `ETag` back as
`If-Match` on PATCH or DELETE (and with a renewal's ETag on its cycle edit): the change applies only if nobody changed
the record since. Otherwise nothing changes and the answer is `412 /problems/precondition-failed` with the current
`revision` in the body and the `ETag` header. A signed-in session (the web app) must send it: every write that takes
`If-Match` refuses a session request without it with `428 /problems/precondition-required` and changes nothing
(`SOFTWARDEN_REQUIRE_IF_MATCH`, on by default; `0` turns it off: see [the environment reference](ENV.md)). An API token's write without `If-Match`
is unconditional. `If-Match: *` matches any revision. Weak tags (`W/"..."`)
never match. Archive and restore don't take `If-Match`; the action POSTs that do say so in their OpenAPI
description: a renewal's cycle edit, a contract's renew, a license pool's bind, a reminder's snooze, acknowledge,
reopen and resolve, a webhook's secret rotation, discovery's drift apply and missing archive (the record's ETag), and a
candidate's dismiss (the candidate's own ETag from `GET discovery/observations/{id}`).
End-of-life tracking (`GET|PATCH|DELETE .../end-of-life`) and ticket links (`.../ticket-links/{id}`) return the row's ETag and
take `If-Match` on PATCH and DELETE; a PATCH that starts end-of-life tracking is a create and needs none.

## Lists and pagination

The long lists (applications, components, AI agents, integrations, contracts, license pools, reminders, the
notification log, data-quality failures and exceptions, discovery runs, the audit log and each record's history) are
keyset pages:

```json
{"results": [...], "next": "eyJ...", "previous": null}
```

Pass `next` or `previous` back as `?cursor=`. `page_size` is 50 by default, at most 200: a larger value is a 400
naming `page_size`, never clamped (the audit log and record histories take `cursor` only). The discovery candidate
queue (`GET discovery/candidates`) is different: 50 rows a page by source name, with `counts`, and two ways to page.
`next_after`, passed back as `?after=`, is a keyset and stays stable while candidates are decided; scripts and the MCP
server walk the queue with it. `next`, passed back as `?cursor=`, is an offset the SPA shows positions with ("from
number 51"); a candidate decided on an earlier page shifts the rest, so a walk with it skips one row per decision.
`ordering` takes the fields the
list documents (comma-separated, `-` for descending); `id` breaks ties, and a cursor only works with the ordering it
came from. Pages stay stable while rows are added or removed. Short lists (people, teams, vendors, tags, rules,
destinations, ...) are plain arrays, still filtered and ordered. Every list and every other read (a record, a report,
a preview, a CSV export, a settings page) refuses a parameter it doesn't take with a 400 naming it, so a typo
(`reports/spend?bye=vendor`, `license-pools/totals?vendor=...`) never silently returns the default or everything. The
exceptions are the sign-in redirects (`auth/oidc/start`, `auth/oidc/callback`), the
calendar subscription `calendar/renewals.ics` (a calendar app can't show a 400) and `health`, which answer whatever
comes with them and declare no 400 for it.

## Timestamps

Timestamps are RFC 3339 in UTC with a `Z` suffix (`2026-11-07T08:00:00.123456Z`). Two fields are in the
organization's timezone on purpose and keep its offset: an audit row's `at` and the preferences' `next_digest_at`. Dates are `YYYY-MM-DD`, in the
organization's timezone where they are "today" (`as_of` in the composite endpoints).

Every date the API stores (renewal and cycle dates, contract terms, decisions and completions, reviews, assessments,
vendor risk dates and documents, custom date fields, compliance dates, and the same columns in a CSV import) must be
between `1900-01-01` and `9998-12-31`. Anything else is a 400 naming the field: `"Enter a date between 1900-01-01 and
9998-12-31."` (a value that isn't `YYYY-MM-DD` keeps `"Enter a date as YYYY-MM-DD."`). So `0001-01-01`, which .NET
and Go clients send for an unset date, is refused rather than stored: send `null` or leave the field out. A date the
API works out (a vendor document's expiry from its validity) is refused the same way, naming the date it counts from.
A row stored outside the window before 1.0, or by hand, never fails a list: its renewal shows no deadline, with the
reason in `deadline.explanation`, and `manage.py doctor` lists it (`stored_dates`). Dates in query filters are not
stored and aren't held to the window.

## The composite namespace

`GET /api/v1/composite/...` holds compact, stable answers for scripts and the MCP server: `search`, `deadlines`,
`renewals` (with who owns each one), `spend`, `dependencies`, `discovery-summary`, `data-gaps` and
`expiring-credentials`. Each is GET only, any role, with a session or any token (read-only included), and reads the
same data as the screen that shows the same thing. `deadlines`, `renewals`,
`data-gaps` and `expiring-credentials` share an envelope: `as_of` (today in the organization's timezone), `count`
(every match), `truncated` and `results`, capped by `limit` (default 50, at most 200). The others have their own
shape: `search` has `count`, `truncated` and `results` with `query`, `by_kind`, `fuzzy` and `ignored`, and no `as_of`;
`spend` has `as_of`, `by`, `totals` and `groups` (no `count` or `results`); `dependencies` has `target`, `depends_on`,
`dependents`, `data_flows`, `summary` and `count` (no `as_of` or `results`); `discovery-summary` has `as_of`,
`counts`, `connectors` and `connectors_total`. Every one has `truncated`. The OpenAPI schema describes each.
References are `{id, name}`, and every record carries its `path` under /api/v1/; an `expiring-credentials` row is a
credential, not a record, so its `path` is on its `record`. A bad
parameter is a 400 problem naming it; a missing capability is a 403 capability problem. `renewals` also takes
`basis`: `action_deadline` (default; `within_days` filters the cancel-or-decide date) or `term_end` (it filters
`term_ends`, ended terms with an open action included, soonest term end first). The [MCP server](MCP.md) reads them with an API token.

## Webhooks

Softwarden posts signed events to HTTPS endpoints an Admin registers (Administration › Webhooks). Routes need the
Admin role, with a session or an Admin's API token, and the `webhooks` capability (every edition).

| Route | What it does |
|---|---|
| `GET, POST /webhooks` | List endpoints; add one: `name`, `description`, `url` (https), `signing_secret` (16 to 1,000 characters), `events`, `enabled` |
| `GET, PATCH, DELETE /webhooks/{id}` | Read, change (ETag and If-Match), delete; `url` and `signing_secret` are write-only, sent only to replace them |
| `POST /webhooks/{id}/ping` | Send a signed `ping` now; the answer is returned inline with the delivery and its attempt |
| `POST /webhooks/{id}/rotate-secret` | `{"signing_secret": "...", "grace_period_hours": 24}` (0 to 168; If-Match) |
| `GET /webhooks/event-types` | The event types, their families and what each means |
| `GET /webhook-deliveries` | The delivery log, newest first; filters `webhook`, `status`, `event` (types or `record.*`), `kind`, `from`, `to` |
| `GET /webhook-deliveries/{id}` | One delivery: its `payload` (the JSON posted) and `attempt_log` |
| `POST /webhook-deliveries/{id}/resend` | Queue the same payload again as a new delivery (202) |

An endpoint is shown with `url_host` only (the address can carry a token, so it is a secret), `signing_secret_set_on`,
`previous_secret_expires_at` during a rotation, `events`, `last_delivery` (`at`, `outcome`, `status_code`,
`delivery`), `consecutive_failures`, and `disabled_at` with `disabled_reason` when Softwarden turned it off. An endpoint
is the same row as a notification webhook destination (`/notification-settings/webhook-destinations`), so a notification
rule can also post its reminder notices (`reminder.due`, `reminder.overdue`) to it; those come from the rule, not from
`events`. `events` and `description` are set at `/webhooks` only: the notification settings route refuses them (400).

### Events

Subscribe to a type or a whole family (`record.*`). Types:

| Family | Types | `data` |
|---|---|---|
| `record.*` | `record.created`, `record.updated`, `record.archived`, `record.restored`, `record.deleted` | `record` {id, kind, name, lifecycle, archived, path}; `changes` on updated |
| `relationship.*` | `relationship.created`, `relationship.updated`, `relationship.deleted` | `relationship` {id, type, source, source_instance, target, target_instance, purpose}; `changes` on updated |
| `renewal.*` | `renewal.created`, `.updated`, `.archived`, `.restored`, `.deleted`, `.decided`, `.completed` | `renewal` {id, name, vendor, auto_renew, archived, path}, `cycle` {id, renewal_date, decision, decided_on, completed_on} or null; `changes` on updated and decided |
| `discovery.*` | `discovery.candidate` (a source found an object not in the catalog) | `observation` {id, kind, display_name, external_id, ai_agent, first_seen_at, connector {id, name, source}, path} |
| `reminder.*` | `reminder.created`, `.snoozed`, `.acknowledged`, `.reopened`, `.resolved` | `reminder` {id, source_type, source_id, source_field, label, title, due_date, status, snoozed_until} |

Records are applications, components, integrations and AI agents; contracts and license pools send no events yet.
`changes` is `{field: {"before": ..., "after": ...}}` from the change's audit event: references are IDs, and a field
named like a secret, key, hash, credential or webhook is never included. An edit that changed nothing sends nothing.
`path` is the record's route under `/api/v1/` (and the web app's); null once deleted.

### The request

```http
POST /your/path HTTP/1.1
Content-Type: application/json
User-Agent: Softwarden-Notifications/1
X-Softwarden-Event: record.updated
X-Softwarden-Delivery: 3f0c5f0e-8e2b-4d3c-9a51-7d2f2b0c9b61
X-Softwarden-Signature: t=1794040800,v1=5d41402abc4b2a76b9719d911017c592...

{"id":"9c1e...","type":"record.updated","occurred_at":"2026-11-07T08:00:00.123456Z",
 "organization":{"id":"..."},"actor":{"id":7,"name":"Dana Ortiz"},
 "data":{"record":{"id":"...","kind":"application","name":"Payroll","lifecycle":"active","archived":false,
 "path":"/applications/..."},"changes":{"name":{"before":"Payrol","after":"Payroll"}}}}
```

`id` is the event's id: the same for every endpoint that receives it and kept by a resend. `X-Softwarden-Delivery` is
this delivery's id: the same on every retry of it, new on a resend. Use it to drop a repeat; a retry can follow a post
your endpoint received but didn't answer in time. `actor` is `{id, name}` for a person, `"system"` for the worker,
discovery and the reminder generator (`name` is the person's full name, possibly empty; never an email address). A
`ping` has `data` `{message, webhook {id, name}}`.

Events can arrive out of order: each delivery is retried on its own, and two changes in quick succession may be posted
in either order. Order by `occurred_at` (and re-read the record through the API when the latest state matters).

### Verifying a webhook

`X-Softwarden-Signature` is `t=<unix seconds>` followed by one or more `v1=<hex HMAC-SHA256>` of the string
`"<t>." + <the raw body>` under your signing secret. During a rotation's grace period there are two `v1=` (the new
secret's first): accept the request when any one matches. Compare in constant time, and refuse a `t` more than five
minutes from your clock to stop replays. Compute over the raw bytes received, before parsing the JSON.

Python:

```python
import hashlib, hmac, time

HEX = set("0123456789abcdef")

def verify(secret: str, header: str, body: bytes, tolerance: int = 300) -> bool:
    pairs = [part.split("=", 1) for part in header.split(",") if "=" in part]
    stamps = [value for key, value in pairs if key == "t"]
    if len(stamps) != 1 or not (stamps[0].isascii() and stamps[0].isdigit()):
        return False
    if abs(time.time() - int(stamps[0])) > tolerance:
        return False
    expected = hmac.new(secret.encode(), stamps[0].encode() + b"." + body, hashlib.sha256).hexdigest()
    given = [v for k, v in pairs if k == "v1" and len(v) == 64 and set(v) <= HEX]
    return any(hmac.compare_digest(expected, value) for value in given)
```

Node:

```js
const crypto = require("node:crypto");

function verify(secret, header, body /* Buffer */, toleranceSeconds = 300) {
  const pairs = header.split(",").map((part) => part.split("=", 2));
  const stamps = pairs.filter(([key]) => key === "t").map(([, value]) => value);
  if (stamps.length !== 1 || !/^\d+$/.test(stamps[0])) return false;
  if (Math.abs(Date.now() / 1000 - Number(stamps[0])) > toleranceSeconds) return false;
  const expected = crypto.createHmac("sha256", secret).update(`${stamps[0]}.`).update(body).digest();
  return pairs
    .filter(([key]) => key === "v1")
    .some(([, value]) => {
      if (!/^[0-9a-f]{64}$/.test(value)) return false;
      const given = Buffer.from(value, "hex");
      return given.length === expected.length && crypto.timingSafeEqual(given, expected);
    });
}
```


### Delivery, retries and turning an endpoint off

Events are recorded in an outbox in the same transaction as the change, so a change that fails sends nothing, a change
that is saved always sends its events (even if the server stops right after), and your request never waits for an
endpoint; the worker queues and posts them on its next tick (every minute). Each endpoint's deliveries are queued in the
order the events were recorded (a long transaction, such as an import, can commit its events after later ones
from short transactions, so this is not strict event order across records); a delivery that is waiting for a retry doesn't hold back later ones, so use `occurred_at` to
order what you receive. Each post has a 10 s timeout per network
step and 30 s in all. The address is checked again on every send: https only, and refused when the name resolves to a
private, loopback, link-local or cloud metadata address; redirects are never followed.

| Answer | Outcome |
|---|---|
| 2xx | Sent |
| 410 Gone | Failed, not retried; the endpoint is turned off |
| 3xx | Failed, not retried (redirects aren't followed) |
| 429 or 503 with `Retry-After` | Retried after it (at most an hour) |
| any other status, a timeout, no connection | Retried after 1 min, 5 min, 30 min, 2 h, 6 h (each ±10 %), up to `SOFTWARDEN_WEBHOOK_MAX_ATTEMPTS` attempts for an event (default 6; later retries wait 6 h). A notification rule's notice keeps 4 attempts (1 min, 5 min, 30 min); a ping is tried once |
| the worker stopped mid-send | Logged as an attempt ("worker stopped mid-send") and retried on the same schedule while attempts are left |
| address refused by the SSRF guard | Failed, not retried |

Only event deliveries (kind `event`) count towards turning an endpoint off. When `SOFTWARDEN_WEBHOOK_DISABLE_AFTER`
event deliveries (default 5; 0 never) in a row end failed after their retries, or an event delivery is answered 410,
Softwarden turns the endpoint off: `enabled` false, `disabled_at` and `disabled_reason` set, an audit event
`webhook_destination.disabled` (no actor), and the Admin banner (`GET /auth/me` `webhook_problem`). A ping or a rule's
notice that fails, even with a 410, fails only itself. Changing `enabled` either way by PATCH clears the reason; turning
it on also clears the count. Events from while it was off are not sent (resend them from the log).

Every attempt is a row of the delivery's `attempt_log`: `number`, `started_at`, `duration_ms`, `outcome` (`sent`,
`retrying`, `failed`, `skipped`), `status_code`, `response_excerpt` (the first 500 characters of the answer, with the
address, the signing secrets and any configured secret replaced by `[redacted]`), `error` and `next_attempt_at`.

**Retention.** Finished deliveries of every channel, with their attempts, are deleted by the daily `notify.retention`
schedule: sent ones 30 days after they were queued (`SOFTWARDEN_DELIVERY_RETENTION_SENT_DAYS`), failed and skipped ones
after 90 (`SOFTWARDEN_DELIVERY_RETENTION_FAILED_DAYS`). A live reminder's notices (the rules' record of what was sent)
and a digest's delivery are kept. An event too large to store (256 KB) is sent with `changes` cut to
`{"truncated": true, "fields": [...]}`.

## The vendor risk file

Capability `vendor_risk` (community) on every route; Viewers read and export, Editors and Admins write.

- `GET, PATCH /vendors/{id}/risk`: the vendor's risk profile (tier, `processes_personal_data`, the questionnaire, the
  assurance report, the DPA, the subprocessor list, notes; attachments by the vendor's own attachment IDs). The first
  PATCH creates it; before that it reads as the defaults with `id` null. `assurance_expires_on` is worked out (issue
  date plus `assurance_validity_months`) and refused when sent. ETag and If-Match as on any record, also before the
  profile exists. The response adds `assurance_status` (`missing`, `current`, `expiring`, `expired`), `gaps` and
  document counts per status.
- `GET, POST /vendors/{id}/risk-documents` (a plain array, the soonest expiry first) and `GET, PATCH, DELETE
  /vendor-risk-documents/{id}`: dated documents (`soc2`, `iso27001`, `pen_test`, `questionnaire`, `dpa`, `insurance`,
  `other`). Send `expires_on`, or `issued_on` with `validity_months` (then the expiry is worked out), not both;
  `soc2` and `iso27001` documents must have one or the other. `status` is derived when read: `expired` after the
  expiry date, `expiring` from the organization's largest reminder threshold before it, else `current` (also when
  undated). `superseded` is true when a later item of the same kind replaces it, like for like and in both
  directions: SOC 2 documents with the profile's SOC 2 report, ISO 27001 documents with an ISO 27001 report, every
  other kind on its own (`other` documents and an `other` report never). A SOC 2 report and an ISO 27001 certificate
  held together are both in force. A superseded item raises no reminder and isn't counted; ties on the expiry date go
  to the item added later.
- Writes refuse fields they don't take (400 naming each, "This field isn't accepted here."); a PATCH that changes
  nothing writes nothing (no event, same ETag). The profile's report has an expiry only with an `assurance_type`.
- `GET /vendor-risk-documents`: every vendor's documents in keyset pages; filters `vendor`, `kind`, `status`, `tier`
  (the vendor's; `none` for not assessed), `expiring_within` (days) and `superseded` (`false`: only documents in
  force, what reminders, counts and the overview read; `true`: only replaced ones). Without `superseded` the list is
  the whole register, replaced documents included, so `status` and `expiring_within` match them too: the list is
  where old reports are found, and every row says whether it is superseded. Ordering `expiry`, `created_at`,
  `updated_at`. `/vendor-risk-documents/export.csv` takes the same filters.
- `GET /vendors/risk-overview`: every vendor with its tier, assurance, document statuses, `next_expiry` and gaps
  (`no_current_assurance`, `dpa_missing`), the riskiest first; filters `tier`, `gap`, `expiring_within`, `expired`.
  `/vendors/risk-overview/export.csv` is one row per vendor with the same filters. Both exports are audited
  (`export`) and refused while a platform operator impersonates.
- `GET /vendors/{id}` carries `risk`, the summary (null without the capability).

Expiry dates are reminders of source `vendor_risk` (`/reminders?source_type=vendor_risk`; the reminder's `path` is
the vendor's page, `/vendors/{id}` in the web app), and the gaps are the Needs attention queue `vendor_risk`, counted in
`counts` and the digest's "Vendor risk" section (its rows link to `/vendors/{id}?tab=risk`).

## The data-flow map

Where classified data goes. Capability `data_flows` (every edition); every member reads and exports. An integration's
classification and data categories are edited on the integration itself (Editor):

- `PATCH /integrations/{id}` (and `POST /integrations`) take `data_categories`: a list of `personal`,
  `special_category`, `financial`, `credentials`, `health`, `customer_content` and `other`, kept in that order and once
  each; null or `[]` clears them; anything else is a 400 on the field. Reads add `contains_personal_data` (derived:
  personal, special-category or health). `GET /integrations?data_category=personal,health` lists integrations carrying any.
- `GET /reports/data-flows`: every live integration (not archived, not retired) as a flow, the most sensitive first:
  `source` and `target` (each with its record's `classification`, its `instance`, an application's `vendor` and, with
  `vendor_risk`, the vendor's `vendor_risk` facts: `dpa`, `dpa_status`, `assurance`, `risk_tier`, `processes_personal_data`),
  `direction`, `classification`, `categories`, `contains_personal_data`, `auth_method`, `technical_owner` and `risks`,
  with a `summary` over the flows returned. Filters: `min_classification` (a classification and every higher one),
  `category` (comma-separated: any), `source`, `target`, `record` (either end; another organization's ID is a 404) and
  `risk` (comma-separated: any). An unknown parameter or a bad value is a 400 naming it.
- Risks, each naming the receiving end (`target`; on a two-way flow `source` too): `no_dpa` (confidential or
  restricted data, or personal data at any classification, to an end whose vendor has no signed DPA or unexpired DPA
  document; a DPA marked `not_applicable` answers for confidential data that isn't personal, never for personal data; only with `vendor_risk`, and the
  response's `vendor_risk` says whether it was judged), `no_auth_method` (an end with a vendor, and authentication is
  not recorded or is `none`), `down_classification` (the flow is classified above the receiving record; an
  unclassified record isn't compared).
- `GET /reports/data-flows/graph`: the same flows, same filters, for Cytoscape in the record map's shape: `nodes` (the
  records at the ends, with `classification`, `weight` 0 to 4, `vendor` and `risky`) and `edges` (one per flow:
  `classification`, `weight`, `categories`, `risks`, and a `label` of its categories, else its name), at most 1,000
  flows (`truncated`), and a `legend`.
- `GET /reports/data-flows/export.csv`: one row per flow with the same filters; vendor DPA and assurance cells are
  `yes` or `no` for both ends, empty without a vendor or `vendor_risk`. Audited (`export`, kind `data_flows`) and refused while a
  platform operator impersonates.

Known limit: `GET /reports/data-flows` and its CSV return every matching flow in one response, with no cap and no
paging (only the graph stops at 1,000). An organization has hundreds of integrations at most; narrow a larger set with
the filters.

Flows with a risk are the Needs attention queue `data_flows` (routed to the integration's IT steward; counted in
`counts` and the digest's "Data flows" section). Two data-quality rules check integrations: `flow_classification` and
`flow_categories`.

## Offboarding and access review

No capability of its own (every edition): each part follows the capability of what it shows.

- `GET /people/{id}/footprint` (any role): where the person appears, as `sections` in a fixed order (`records`,
  `ai_agents`, `integrations`, `it_steward`, `teams`, `vendors`, `contracts`, `renewals`, `reviews`, `assessments`,
  `quality_exceptions`, `notification_rules`, `discovery`, `entra_owners`, `licences`, `account`), each `{key, label,
  available, reason, count, items}`; an item is `{type, id, name, path, roles, archived, detail}` (`path`: the web app's
  page). AI agents include those whose `run_as_identity` is the person's email (or their account's) or Entra object
  ID; `entra_owners` are discovered apps whose Entra owners match them by object ID or email. A section is
  `available: false` with its `reason` when the organization lacks its capability, when it is Admin-only (`account`:
  membership, IT coordinator, live API tokens and calendar feed, saved views and subscriptions; `notification_rules`),
  when it needs a user account the person doesn't have, and always for `licences` ("Licenses: not read; needs
  User.Read.All": Softwarden doesn't read per-user license assignment). Archived rows are included and marked.
  `person.account` (their user account's email, active flag and role) is given to Admins only.
  `GET /people/{id}/footprint.csv` is one row per item (an unavailable section is a `not_available` row with the
  reason); audited (`export`, kind `footprint_row`, `rows` the CSV rows written, the person's ID as the filter) and refused while a platform
  operator impersonates.
- `POST /people/{id}/reassign {to, roles, dry_run}` (Editor, Admin): moves the chosen roles to `to`, an active person
  of this organization other than this one. Roles: `business_owner`, `technical_owner`, `it_steward`,
  `vendor_account_manager`, `renewal_responsible`, `renewal_followup`, `contract_renewal_owner`,
  `quality_exception_owner`, `notification_rules` (Admin only). `it_steward` and `renewal_followup` need `to`'s user
  account to be an active Admin or Editor here. Each row goes through its own service (one audit event per row, with
  `via: offboarding` and `offboarding_person`), in one transaction; if any row is refused, nothing is saved and the
  400 problem lists the refused `rows`. One `person.reassigned` event sums it up (none when nothing changed).
  Archived records, renewals and contracts are skipped (`status: skipped` with the `reason`); records of a kind the
  organization lacks the capability for aren't listed; team memberships aren't moved; nothing is deleted.
  `dry_run: true` runs the same service calls in a savepoint that is rolled back: rows are `planned`, or `refused` with
  `errors` and `field_errors` when the real run would refuse them (`refused` counts them), and nothing is written.
  Every answer carries `plan_hash` (SHA-256 of the organization, both people, the roles and the sorted type, ID and
  role of every row that would change). A real run may send a dry run's `plan_hash`: when the rows it computes under
  its locks differ, it answers `409 /problems/reassign-stale` and writes nothing (the web app always sends it). `to` takes any UUID spelling (upper case, braces). Bulk action: no If-Match; the people and then the rows are locked
  (FOR NO KEY UPDATE, each table in ID order, contracts before renewals as a contract edit does) while it runs, so a
  concurrent edit waits rather than deadlocking. Allowed while impersonating (audited with `impersonated_by`).
- `GET /reports/access-review` (any role): every record that isn't archived (`include_retired=true` adds retired ones)
  with its owners (`status` `active`, `inactive`, or `left`: an active person whose account was deactivated or
  removed here), `entra_owners` from linked discovered apps, `flags` (`inactive_owner`, `owner_left`, `no_owner`,
  `entra_owner_mismatch`, informational) and `why` in words; filters `kind`, `flag` (comma-separated, any), `owner` (a
  person's ID) and `include_retired`. `counts` are over the records in scope before the flag filter.
  `GET /reports/access-review/export.csv` takes the same filters; audited and refused while impersonating.

## Ticket links

Capability `ticket_links` (community). Viewers read; Editors and Admins write; connections are Admin only.
ServiceNow and Halo ticket connections are preview features (capability `preview`, only with `SOFTWARDEN_PREVIEW_FEATURES`; [Preview features](CAPABILITIES.md#preview-features)): without it `GET /ticket-links/systems` lists Jira only, `GET /ticket-connections` lists a kept ServiceNow or Halo connection with `available: false`, `DELETE` still removes one (audited with `kept: true`, so it never blocks its Halo connection), and every other route on one, creating one, a refresh and create-ticket for those systems answer 403 naming `preview`. Their links are still pasted and kept, with a status set by hand.

- `GET, POST /assessments/{id}/ticket-links` and `/renewals/{id}/ticket-links` (a plain array, oldest first);
  `GET, PATCH, DELETE .../ticket-links/{link_id}`. `POST {url, title?, system?, external_key?, status?, cycle?}`:
  paste the ticket's https address and the system and key are parsed from known shapes (Jira `/browse/KEY` on any
  host, `selectedIssue=KEY` on `*.atlassian.net`; ServiceNow a task number anywhere in a `*.service-now.com` address;
  Halo `/tickets?id=N` on `*.haloitsm.com`, `*.halopsa.com`, `*.haloservicedesk.com`; the host of any of the
  organization's ticket connections counts as its system), else `other`. `system` and `external_key` override the
  parse. `status` sets the status by hand (`status_source: manual`); `status: null` on PATCH goes back to reading it (`unknown`, unread, until the next read). `cycle` (renewals only) is one of the renewal's
  cycles. The same address twice on one target is refused (400). The response carries `status`, `status_label`,
  `external_status`, `status_checked_at` and `status_error`.
- `POST .../ticket-links/{link_id}/refresh`: reads the ticket's status now through the system's ticket connection, by
  key, from the connection's own address (a link on another site isn't read: `status_error` says so). A failed read is
  recorded in `status_error` and the link returned (200); 409 when the system has no usable connection or the link is
  `other`.
- `POST .../ticket-links/create-ticket {system, title?, description?, cycle?}`: creates a ticket in Jira, ServiceNow or
  Halo and links it (201, `created_in_system: true`). 409 unless an Admin turned on `create_enabled` for that
  connection (off by default) and it passed its test; Jira also needs `create_target` (the project key). Sends only the
  title (default: the assessment's title or "Renewal: <name>") and the description (default: a link back to the
  record). Every attempt is audited; a failure is 409 with `problem` (`auth`, `forbidden`, `not_found`, `busy`,
  `timeout`, `unreachable`, `refused`, `bad_answer`) and says when the ticket may have been created anyway. When the ticket was created but
  can't be stored as a link (an unacceptable key or address, or the assessment, renewal or cycle was deleted
  meanwhile), the answer is 409 `problem: not_linked` with `external_key`, and the audit event says `created: true`,
  so it can be linked by hand. Refused while platform staff impersonate. Deleting a Halo connection a ticket
  connection uses is 409, naming it (`ticket_connections`).
- `GET /ticket-links/parse?url=`: the system and key an address parses to, for the form's preview (no network).
- `GET /ticket-links/systems` (every member): one entry per connected system, `{system, label, reads, creates}`. `reads`: its ticket connection is usable (on, with its secret or the Halo edition, tested at its current revision), so statuses are read; `creates`: it also allows creating tickets (Jira with a project key). Never a connection's settings; the web app shows Create ticket and the link form's "read automatically" from it.
- `GET /assessments/{id}` and `GET /renewals/{id}` carry `ticket_links`. Adding or changing a link doesn't move the
  assessment's or renewal's ETag: links are their own resources, so a link added meanwhile never makes an edit of the
  assessment or renewal a 412.
- `GET, POST /ticket-connections`, `GET, PATCH, DELETE /ticket-connections/{id}`, `POST /ticket-connections/{id}/test`
  (Admin): one per system. Jira: `base_url`, `username` (the account email) and `secret` (an API token); ServiceNow:
  `base_url`, `username`, `secret` (the password); Halo: `halo_connection` (the Halo connection's ID; needs
  `discovery.halo`; its scope must include `read:tickets`, and `edit:tickets` to create). The secret is write-only: send
  it only to replace it; responses carry `secret_set_on`. A `base_url` on another scheme, host or port needs `secret`
  in the same request (400 on `secret` otherwise); a path-only change keeps the stored one. The test signs in and makes one read (Jira `myself`, one
  ServiceNow task, a Halo token with a ticket scope). Secret writes are refused while platform staff impersonate; the
  test is allowed.

## What the API covers

Every model with a write path goes through its whole lifecycle with an API token: create,
read, list, PATCH with If-Match, then archive and restore and delete as the model and the token's role allow. The
write actions of the models below (discovery decisions, reminder actions, digest retry, resend, tag edits) and every
write route outside the viewsets (bulk edit and delete, settings, email tests, assistant batches) run with a token
too, and a read-only token is refused each one. Models that are intentionally not fully writable, and why:

| Model (route) | API | Why |
|---|---|---|
| AuditEvent (`/audit-events`, each record's `/audit`) | list, retrieve, export | Append-only: written by the change it records. |
| Review, LinkConfirmation (`/reviews`, `/confirmations`) | create, list, retrieve | Evidence: recorded, never edited. |
| Assessment (`/assessments`) | create, read, PATCH, status | Closed by status (`cancelled`, `closed`), never deleted: its snapshots are history. |
| Person (`/people`) | create, read, PATCH, anonymize (Admin) | Deactivated or anonymized, never deleted: owners and reviews keep naming them. |
| User membership (`/users`) | read, PATCH (session only), DELETE, reset password (session only), unlink identities | Members join by accepting an invitation (`POST /users` makes one, as `POST /invitations`; session only). DELETE removes a membership only while the user belongs to another organization; otherwise deactivate. |
| Invitation (`/invitations`) | create (session only), list, retrieve, resend (session only), revoke | Never edited; kept once accepted or revoked. The link is in the create and resend responses only. `POST /auth/invitations/preview {token}` and `POST /auth/invitations/accept {token, name?, password?}` serve the link's page (public, CSRF-checked, invitation throttle); the preview's `session` is `none`, `invitee`, `other_account` (accepting answers 409 `/problems/invitation-wrong-account`) or `other_sign_in` (the invited account signed in through another organization's SSO; accepting answers 409 `/problems/invitation-other-sign-in`); the token is never in a URL path because paths are logged. The link is `{BASE_URL}/invite#<token>`. |
| OrgExport (`/organization/exports`) | create, list, retrieve, download (Admin) | Built by the worker; expires after 7 days (a nightly `backup` is kept by count instead). |
| Organization deletion (`/organization/deletion`) | read, schedule (POST), cancel (DELETE) (Admin) | A state of the organization, not a record; the purge is the worker's. |
| PendingSignup (`/auth/signup...`) | signup, resend, verify (public) | Installation-wide until confirmed; never readable. |
| PendingAccess (`/pending-access`) | list, retrieve, approve (session only), decline (DELETE) | Written by single sign-on while `sso_auto_provision` is on. |
| ImportRun (`/imports`) | upload, read, PATCH, commit, cancel, rerun | Never deleted: the 30-day purge removes an import's data. |
| Catalog-as-code (`/catalog-code/export.zip`, `/catalog-code/plan`, `/catalog-code/apply`) | export, plan, apply (Admin, `catalog_code`) | Not a model: the catalog as YAML files (see "Catalog as code" below). |
| Tag (`/tags`) | list, retrieve, PATCH | Created and removed through a record's `tags`. |
| Snooze (`/needs-attention/snoozes`) | create, read, DELETE | Replaced, not edited. |
| ApiToken (`/api-tokens`) | create, list, retrieve, revoke (session only) | Revoked, never edited or deleted, so the audit log keeps naming them. |
| SsoSettings (`/sso/settings`) | read; PATCH (the draft), activate, disable, test sign-in and verify a sign-in domain (`POST /sso/settings/domains/verify {domain}`), every write session only | One per organization, never deleted. A provider, `okta_domain` or `discovery_url` change that moves the discovery document to another scheme, host or port needs `client_secret` in the same request (400 on `client_secret` otherwise). The test is a browser round trip, so it needs a signed-in session; a token is refused (403). The `saml` preset needs `sso.saml`, which no organization has in this release. `sign_in_domain_states` lists each sign-in domain as `verified` or `pending` (with `record_name`, `record_value` and `lapses_at`); with `SOFTWARDEN_SSO_DOMAIN_VERIFICATION` (cloud) a new domain is pending, finds no organization on the sign-in page, and is verified by its DNS TXT record; every refusal to verify is the same 400 on `domain`. |
| Calendar feed (`/calendar-feed`) | create, read, rotate, revoke (session only) | A credential of its member. A password reset or change revokes the account's feeds (`calendar_feed.revoked` with `reason`), and no feed is served while the account must change its password. |
| Connection (`/connections/microsoft`, `/connections/halo`, `/connections/okta` and `/connections/google_workspace`) | create, list, retrieve, PATCH, DELETE, test (Admin; Halo `discovery.halo`, Okta `discovery.okta`, Google Workspace `discovery.google`) | The secret (a client secret, an Okta private key, a Google service account key file) is write-only: `secret_set_on` says when it was replaced. Okta: `base_url`, `client_id`, `key_id`, `scope` (read-only Okta scopes). Google Workspace: `admin_email`; `client_id` and `service_account_email` are read from the key file. A changed sign-in value or secret raises `revision`; no connector runs until `test` passes again. A Halo or Okta `base_url` on another scheme, host or port needs `secret` in the same request (400 on `secret` otherwise). Delete is refused (409) while a connector uses it. |
| Connector (`/connectors`) | CRUD, Run now, access check (Admin writes) | `POST /connectors/{id}/run` takes an optional body `{"accept_empty_listing": true}` (default false): this one run skips the empty-listing guard, so a listing that really came back empty (the last web app of a subscription, every agent of an environment) marks its observations no longer seen and removes their credentials. Recorded on the run (`counts.accept_empty_listing`) and the `connector.run_requested` audit event; scheduled runs never set it, and the next run is guarded again. Any other value is a 400. A run the guard refused has `failure_code: "empty_listing"`, so a client offers this without reading the error's words. |
| Observation, ConnectorRun, DiscoveredCredential (`/discovery/...`) | read, decision actions | Written by discovery; link, create, dismiss, drift and missing actions are audited edits. |
| Shadow IT overview (`GET /discovery/shadow-it`, `GET /discovery/shadow-it/export.csv`) | read, CSV export (any role; capability `discovery.entra`) | Written by `entra_shadow_it` runs: one row per third-party app and connector with users and sign-ins over 30 days, last sign-in, consent (`admin`, `user`, `none`), how many people consented, scopes and high-risk scopes, and `status` (`new`, `linked`, `dismissed`), with the linked `record` and the `candidate` to decide it through. Keyset pages; filters `connector`, `status`, `consent`, `risky`, `min_users`, `q`; ordering `users_30d`, `sign_ins_30d`, `display_name`, `consenting_users`, `first_seen_at`. The CSV takes the same filters (at most 10,000 rows), is audited as `export` (kind `shadow_it`) and is refused while platform staff impersonate. Counts only: never who signed in or consented. |
| SSO and MFA coverage report (`GET /reports/sso-coverage`, `GET /reports/sso-coverage/export.csv`) | read, CSV export (any role; capability `discovery.entra`) | One row per live application (not archived, not retired): `record`, `criticality`, `lifecycle`, and its coverage from `entra_coverage` runs: `entra_app_id` and `linked_by` (`entra_app_id`, `candidate`, or empty when not linked), `sso` (`saml`, `oidc`, `password`, `none`, `unknown`), `scim` (`on`, `off`, `unknown`), `mfa` (`all_users`, `some_users`, `none`, `unknown`, `blocked`: sign-in blocked for all users with no exclusion, no gap), each with its `*_detail`, `scim_checked_at` and `mfa_checked_at` (when they were read; a transient failure keeps an answer up to 7 days) (why; unknown always says why), `policies` (the conditional access policies that cover or may cover it, with their `effect`), `gaps` (No SSO, No SCIM, No MFA policy, MFA for some users only, Coverage unknown), `differs` (the record's own `sso_enabled` / `scim_provisioning` where Entra says otherwise; shown, never applied), `connector`, `seen_at`. Keyset pages, most critical first; filters `sso`, `scim`, `mfa`, `criticality` (with `none`), `gap`, `linked`, `q` (name or app ID); ordering `name`, `criticality`, `sso`, `scim`, `mfa` (worst coverage first ascending). The CSV takes the same filters (at most 10,000 rows), is audited as `export` (kind `sso_coverage`) and is refused while platform staff impersonate. The application detail (`GET /applications/{id}`) carries the same block as `sso_coverage` (null without the capability). |
| License waste report (`GET /reports/licence-waste`, `GET /reports/licence-waste/export.csv`) | read, CSV export (any role; capability `discovery.entra`) | Built from `entra_licence_usage` runs (licensed users), `entra_licenses` observations (seats purchased and assigned) and the license pools bound to each SKU. **Rows:** one per SKU, most reclaimable first. **Counts:** `assigned`, `active`, `new`, `inactive`, `never_signed_in`, `disabled`, `unknown`, `reclaimable` (inactive + disabled), `group_reclaimable` (of those, held only through a group), `guests`, `guests_inactive`, `service_accounts`, plus `seats_purchased`, `seats_assigned` and `seats_unassigned` (null without an Entra licenses connector). **Links:** `applications`, `vendors` and `pools` (with their contract). **Money:** `price` (`currency`, `annual_cost`, `seats`, `annual_unit_price`, `contracts`, `some_pools_unpriced`) or a `gap` (`no_pool`, `no_contract`, `contract_not_live`, `no_cost`, `contract_shared`, `no_seats`, `mixed_currencies`, `contracts_off`, with its words), then `annual_waste` and `annual_unassigned` as strings with two decimals (null with a gap). **Top level:** `days`, `cutoff`, `as_of`, `sign_in_activity` (`known`, `unknown`, `partial`, `none`), `connectors`, `notes`, and a `summary` of the filtered rows: seat counts over every row, priced or not and free SKUs included, `totals` per currency (the only money), and `unpriced`. **Filters:** `days` (7 to 730, the setting's range; default the organization's `inactive_seat_days`), `sku` (IDs or part numbers), `application`, `vendor` (404 for another organization's), `min_inactive`. **CSV:** the same rows, audited as `export` (kind `licence_waste`) and refused while platform staff impersonate. |
| License waste drill-down (`GET /reports/licence-waste/{sku_id}`, `GET /reports/licence-waste/{sku_id}/export.csv`) | read, CSV export (**Admin**; capability `discovery.entra`) | One SKU's licensed users, never-signed-in first, then the longest since a sign-in, at most 5,000 (`total`, `truncated`). **Each user:** `name` (the linked Person's, else "Unlinked user"), `ref` (8 characters of the stored keyed hash, stable across runs), `person`, `status` (`active`, `new`, `inactive`, `disabled`, `unknown`, `service_account`), `never_signed_in`, `last_sign_in_on`, `last_sign_in` (in words, "unknown (needs Entra ID P1 / AuditLog.Read.All)" when not read), `days_since`, `account_enabled`, `guest`, `group_assigned` (held only through a group: reclaimed by group membership), `connector`. No object ID, user principal name or email exists to show. **Filters:** `days`, `status` (default `inactive,disabled`), `user_type` (`member` default, `guest`, `all`). Admin-only because who hasn't signed in is personal data. Both routes are refused while platform staff impersonate; the CSV is audited as `export` (kind `licence_waste_users`, with the SKU). |
| License waste of a pool or contract (`GET /license-pools/{id}/licence-waste`, `GET /contracts/{id}/licence-waste`) | read (any role; capabilities `discovery.entra` and `contracts`) | **Pool:** the report row of the SKU it is bound to (`row`, null when nothing reports it), or `gap` `no_sku_id`. **Contract:** the rows of its pools' SKUs (each SKU's whole row) with `contract_share`: this contract's part of the row's money, weighted by its own annualized cost (`annual_cost` × reclaimable ÷ the seats of every contract pricing the SKU; the parts add up to the row), null when it doesn't price the SKU. `contract_totals` sums the shares; `summary` is the SKUs' whole rows. Filter `days`. Another organization's pool or contract is 404. |
| Reminder (`/reminders`) | read, snooze, acknowledge, reopen, resolve | Generated from dates. |
| VendorRiskProfile (`/vendors/{id}/risk`) | read, PATCH | One per vendor, created by its first PATCH; removed with the vendor, never on its own. |
| Digest, Delivery (`/admin/digests`, `/notification-log`) | read, retry, resend | Written by the worker. |
| Webhook delivery (`/webhook-deliveries`) | read, resend | Written by the worker; endpoints (`/webhooks`) have the whole lifecycle plus ping and rotate-secret. |
| Data-quality failure (`/data-quality/failures`) | read | Computed from the records. |
| End-of-life tracking (`/<applications or components>/{id}/end-of-life`, `/applications/{id}/instances/{instance_id}/end-of-life`; capability `eol`) | read, PATCH (create or change), DELETE | One per record and one per instance at most, so it is a sub-resource, not a list. `PATCH {product, version?, cycle?}`: `product` is required the first time (an endoflife.date name or alias from `GET /end-of-life/products?search=`); `cycle` empty or null derives it from `version`, or from the application's own `version` field. The response is the record's row with `instances`, each with `status` (`supported`, `security_only`, `end_of_life`, `unknown`), `unknown_reason` (for an unknown status: the installation's `not_downloaded`, `download_failed` or `downloads_off`, or the record's `cycle_not_listed`, `no_version_match` or `no_version`; else null), `why`, `eol_date`, `days_remaining`, `support_until`, `extended_support_until`, `latest`. An instance needs its application tracked first (409). The record detail (`GET /applications/{id}`, `/components/{id}`) carries the same block as `end_of_life` (null when not tracked). `GET /end-of-life` lists every tracked version of live records, soonest end of life first, with `status` (comma-separated), `days` (end of life within N days, or past, including end of life with no date), `product`, `kind`, `criticality` (comma-separated, `none` for no criticality) and `owner` (a person as business or technical owner) filters; each row's `record` carries `criticality`, `business_owner` and `technical_owner`; with the counts per status and the cache's state; `GET /end-of-life/export.csv` is the same as CSV (audited as an export, refused while platform staff impersonate). `GET /end-of-life/products/{name}` lists a product's cycles with their status. The product cache is shared by the installation, so whether a product's cycles are cached (`releases_cached`), when they were checked, the last error, a rename (`renamed_to`) and the cycles themselves are given only for products the organization tracks; a product neither in the cached list nor tracked by the organization is a 404. Every read is from the installation's cache; the worker refreshes it daily. Each tracked version with an end-of-life date is a reminder of source `end_of_life` (`/reminders?source_type=end_of_life`; `source_id` is the record, `source_field` `tracked_version:<id>`, `path` the record's page with `#h-eol`), announced by the built-in "End of life" notification rule (90, 30 and 7 days before) to the record's IT steward, else its technical owner's account, else the IT coordinator. Needs attention's `end_of_life` rows carry it as `reminder` (null with no date) instead of listing it under `reminders`; End of life is not a digest section (notify 0012), so nothing is emailed twice. |
| TicketLink (`/assessments/{id}/ticket-links`, `/renewals/{id}/ticket-links`; capability `ticket_links`) | create, list, retrieve, PATCH, DELETE, refresh, create-ticket | Nested under its assessment or renewal (see "Ticket links" above); no If-Match. |
| TicketConnection (`/ticket-connections`) | create, list, retrieve, PATCH, DELETE, test (Admin) | One per system; the secret is write-only, and a `base_url` on another host needs it again. |
| Organization, preferences, email and assistant settings | GET, PATCH | One per organization (or member); no create or delete. |
| Job, Schedule, WorkerHeartbeat | inside `GET /system` | Internal to the worker. `scope` says which view: `installation` (schedules, `backup`, installation-wide `jobs.system`, every warning) for platform staff and for an Admin of the first organization of a self-hosted installation (never on a cloud installation, `SOFTWARDEN_DEPLOYMENT=cloud`); `organization` for every other Admin (the overall `status`, `worker`, this organization's own jobs, and `stored_secrets_unreadable` for its own secrets only; no `schedules`, `backup` or `jobs.system`). Platform staff read the installation view at `GET platform/system`. |
| BackupState (`GET /system` `backup` and `warnings`; `POST /system/backup/acknowledge-key`) | read; acknowledge (Admin) | Installation-wide, written by the worker's nightly backup. `backup` has `state` (`ok`, `stale` after 26 h, `never`, `not_configured`), `last_success_at`, `age_seconds`, `last_set`, `last_size_bytes`, `last_duration_seconds`, `last_failure_at`, `last_failure`, `running`, `destination`, `cron`, `key_source`, `key_present` and `key_acknowledged`; `warnings` lists `backup_stale`, `backup_failed`, `backup_key_missing` and `backup_key_unacknowledged` (warnings never change `status` or `/readyz`). `last_failure` names only the stage and a tool's exit status (the details are in the worker log). Acknowledging the generated backup key is audited (`backup.key_acknowledged`) in every organization and idempotent per key; 409 when the key is configured with `SOFTWARDEN_BACKUP_KEY_FILE` or missing. Nothing derived from the key is ever returned. |

## Catalog as code

The catalog as a tree of YAML files, one per record, for a git repository (the format and a git workflow are in
[CATALOG_AS_CODE.md](CATALOG_AS_CODE.md)). Admin only, capability `catalog_code` (every edition).

- `GET /catalog-code/export.zip[?include_people=true]`: the tree as a zip. The same records give the same bytes.
  Audited (`catalog_code.exported`); refused while platform staff impersonate (`403 /problems/impersonation-restricted`).
  People's names and emails only with `include_people=true`.
- `POST /catalog-code/plan` (multipart: `file`, a zip of the tree; `prune`, true or false; `allow_mass_delete`, true or
  false, see the prune cap below): what applying would do.
  200 with `plan_hash`, `summary` (create, update, delete, unchanged, errors, files), `changes` (each with `file`,
  `kind`, `key`, `action` and `fields`: `path`, `before`, `after`), `errors` and `warnings` (each with `file`, `line`,
  `path`, `message`). Every change is tried through the service functions and rolled back, so the errors are the ones
  apply would meet. Writes nothing.
- `POST /catalog-code/apply` (multipart: `file`, `prune`, `allow_mass_delete`, `plan_hash`): applies the tree in one
  transaction or nothing. A prune that deletes more than 10 records, more than 20% of a kind, or all of a kind, is an error (with
  `mass_delete`, the count) unless `allow_mass_delete` is true ([Catalog as code, "The prune cap"](CATALOG_AS_CODE.md#prune)).
  `409 /problems/catalog-code-stale` when the catalog, the files or `prune` changed since the plan; `400
  /problems/catalog-code-invalid` with `file_errors` when a file has an error; `409 /problems/limit-reached` when the
  records it creates would pass the organization's limit. Each record's audit event carries `via: catalog_code`, and
  one `catalog_code.applied` event sums the run.

A zip that can't be read safely is a `400 /problems/invalid-request` with the reason under `errors.file`: not a zip,
over 16 MB, more than 25,000 entries or 20,000 YAML files or 16 MB uncompressed, an absolute or `..` path, a symbolic
link, an encrypted entry, or nesting deeper than 8 folders. A plan or apply that would create more than 2,000 rows is a
400 too (`errors.file`): the request timeout is 120 s, so a first load that large goes through `manage.py
catalog_apply`, which has no such cap.

## Generated client

The schema is fit for client generation: every operation has a unique `operationId`, typed request and response
bodies, named enum components (`LifecycleEnum`, `RoleEnum`, ...) and the shared `Problem` schema under
`application/problem+json` on every error response, `401` on every operation that authenticates (public ones too: a
bad Bearer token is refused everywhere except setup and the calendar feed, which authenticate nobody), `429` (with
`Retry-After`) on signed-in operations and a `default` problem response everywhere. A field that can be null is
declared `nullable`, and a download (CSV, Excel, JSON lines, zip, calendar) declares its media type with a binary body.
Every GET operation is checked in the project's tests against its declared schema, so the schema matches what the API
answers. Generate a client in any language
from `GET /api/v1/schema?format=json`; for TypeScript, [openapi-typescript](https://openapi-ts.dev/) works.

In the app, **API reference** (`/admin/api`, every signed-in account; the account menu, and the Admin section for
Admins) renders this installation's schema with Scalar, bundled from npm and loaded on first use, with its telemetry,
agent, developer tools, API client, MCP button and remote fonts turned off, so it requests nothing from anyone but this
installation. **API guide** (`/admin/api/guide`) is this page.
