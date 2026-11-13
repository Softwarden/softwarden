# Microsoft permissions

This page lists every permission Softwarden asks for in Microsoft Entra ID, Azure, Power Platform and Exchange Online: which feature needs it, what it lets Softwarden read, whether it is optional, and what happens without it. It also lists the other services that receive data or credentials from Softwarden (HaloITSM, Jira, ServiceNow, Okta, Google Workspace, endoflife.date and Anthropic), so you can review everything Softwarden can reach in one place.

Use it when you create app registrations, grant admin consent, or review Softwarden's access. The guided setup in the app (Discovery › Set up a discovery source) links to the section for each source.

## Principles

- **Read-only.** Every discovery permission reads. No Softwarden feature writes to Entra, Azure, Power Platform or Halo. The one permission that sends anything, `Mail.Send`, is limited to a single shared mailbox through Exchange RBAC for Applications.
- **You grant, Softwarden doesn't.** Softwarden never grants permissions or consent itself. Its settings pages and the guided setup explain what to grant, and an administrator grants it.
- **Separate app registrations.** Use one registration for sign-in, with delegated permissions only, and a separate one for each background purpose (discovery, email). Each registration then holds only its own permissions. A sign-in registration holds delegated permissions only as long as it isn't shared with discovery: the older installation-wide sign-in settings (`SOFTWARDEN_OIDC_*`) were documented as the discovery connector's registration, so give per-organization sign-in a registration of its own.
- **Secrets are write-only.** Client secrets and keys you enter in Softwarden are encrypted with Fernet under the credential key (`SOFTWARDEN_CREDENTIAL_KEYS`, or the key generated on first start; an older `SOFTWARDEN_SECRETS_KEY` only decrypts). The ID of the key is stored beside each value. Secrets never appear in responses, audit snapshots, logs, exports or job arguments.
- **Checks need no extra permission.** Connection tests and access checks make the same reads as a run, limited to one item. A token alone doesn't prove that every subscription, environment or mailbox works, so results say which parts were checked.

## What has been tested

Softwarden includes simulators of each service it talks to, and most features in this release have been tested only against them. Plan your first live run with that in mind.

| Feature | Tested against |
|---|---|
| Discovery sources `entra`, `entra_licenses` and `azure` | Simulated tenant, and one live run against a Microsoft development tenant during development: the access check, the app listing with credential expiry, seat counts, and an Azure Reader read. |
| Discovery sources `entra_shadow_it`, `entra_coverage`, `entra_licence_usage` and `powerplatform` | Simulated tenant only. Nothing has contacted a real tenant. |
| Sign-in with OpenID Connect (Entra, Okta, Google Workspace, generic) | Simulated identity providers only |
| Email through Microsoft Graph | Simulated tenant only |
| Teams notifications | Mocked only. Not yet run against a real workflow. |
| Discovery source `halo`, ticket connections (Jira, ServiceNow, Halo), discovery sources `okta` and `google_workspace` | Synthetic versions of those systems only |
| End-of-life data from endoflife.date | Recorded responses only |
| AI assistant (Anthropic) | A mocked SDK only. No real API call has been made. |

Some details below aren't documented by Microsoft or the other vendor. Where that matters, the section says what Softwarden assumes until it is checked on a real tenant.

Okta and Google Workspace discovery, and ServiceNow and Halo ticket connections, are **preview features** in 1.0.0. Softwarden makes no request to those services unless the installation sets `SOFTWARDEN_PREVIEW_FEATURES` ([preview features](CAPABILITIES.md#preview-features)).

To try a source without contacting anyone, use the simulated tenant: see [the first-run guide](FIRST_RUN.md) and `SOFTWARDEN_ALLOW_SIMULATED` in [the environment reference](ENV.md).

## Summary

| Area | API | Permission or grant | Type | Needed for | Required |
|---|---|---|---|---|---|
| Sign-in (Entra OIDC) | Microsoft identity platform (listed under Microsoft Graph in the portal) | `openid` | Delegated | Single sign-on per organization | Yes, for Entra sign-in |
| Sign-in (Entra OIDC) | Microsoft identity platform (listed under Microsoft Graph in the portal) | `profile` | Delegated | Name claims in the ID token | Yes, for Entra sign-in |
| Sign-in (Entra OIDC) | Microsoft identity platform (listed under Microsoft Graph in the portal) | `email` | Delegated | Email claim, used to match a first sign-in to an existing member | Yes, for Entra sign-in |
| Sign-in (Entra OIDC) | Microsoft Graph | `User.Read` | Delegated | Nothing: added by default to new registrations, never requested or called. You can remove it. | No |
| Discovery: `entra` | Microsoft Graph | `Application.Read.All` | Application | Enterprise apps, app registrations, their owners (IDs) and credential end dates | Yes |
| Discovery: `entra` | Microsoft Graph | `User.ReadBasic.All` | Application | Owners' names and emails. Without it, owners show as "owner not resolved". | No |
| Discovery: `entra` | Microsoft Graph | `AgentIdentity.Read.All` | Application | Entra Agent ID agent identities as AI agent candidates (scope `include_agent_identities`) | No, off by default |
| Discovery: `entra_licenses` | Microsoft Graph | `LicenseAssignment.Read.All` | Application | Seat counts per SKU, for license pools | Yes |
| Discovery: `entra_shadow_it` | Microsoft Graph | `AuditLog.Read.All` | Application | Sign-in logs: users and sign-ins per third-party app. Needs Entra ID P1 or P2. | Yes, unless the scope's `sign_ins` is off |
| Discovery: `entra_shadow_it` | Microsoft Graph | `Directory.Read.All` | Application | OAuth consent grants, the service principals they name, the tenant's own ID | Yes |
| Discovery: `entra_coverage` | Microsoft Graph | `Application.Read.All` | Application | Each linked enterprise app's single sign-on mode (`preferredSingleSignOnMode`) | Yes |
| Discovery: `entra_coverage` | Microsoft Graph | `Synchronization.Read.All` | Application | Provisioning (SCIM) jobs of each linked enterprise app | Yes, unless the scope's `provisioning` is off |
| Discovery: `entra_coverage` | Microsoft Graph | `Policy.Read.All` | Application | Conditional access policies: which apps require MFA, and for whom. Needs Entra ID P1. | Yes, unless the scope's `conditional_access` is off |
| Discovery: `entra_licence_usage` | Microsoft Graph | `User.Read.All` | Application | Each licensed user's `assignedLicenses`, `accountEnabled`, `userType` and `createdDateTime`, for license waste. Mail and user principal name are read only to link people and tell service accounts apart, and are never stored. **A tenant-wide admin consent.** | Yes |
| Discovery: `entra_licence_usage` | Microsoft Graph | `AuditLog.Read.All` | Application | Each licensed user's `signInActivity.lastSuccessfulSignInDateTime`. Needs Entra ID P1. Without it the last sign-in is unknown. **A tenant-wide admin consent.** | No; off with the scope's `sign_in_activity` |
| Discovery: `azure` | Azure Resource Manager | Azure RBAC **Reader** (`acdd72a7-3385-48ef-bd42-f606fba81ae7`) per subscription or management group | Azure role assignment | Logic Apps, Function Apps, App Services (optional), Automation runbooks | Yes |
| Discovery: `powerplatform` | Dataverse Web API | An application user with a read-only role (`prvReadbot`, optionally `prvReadWorkflow`), or **Service Reader** if that isn't enough | Dataverse security role | Copilot Studio agents, optionally cloud flows | Yes |
| Discovery: `powerplatform` | Power Platform admin API (BAP) | Power Platform management app registration | Admin-equivalent registration | Listing all environments (scope `all_environments`) | No, off by default |
| Discovery: `halo` | HaloITSM API | Halo API application, scope `read:assets` (the default) | Halo client credentials | Assets | Yes |
| Discovery: `okta` (preview) | Okta Management API | OAuth 2.0 service app with scope `okta.apps.read` and the admin role **Read-only Administrator** | Okta client credentials with a private-key client assertion | Applications with their sign-on mode and assigned-user counts | Yes |
| Discovery: `google_workspace` (preview) | Google Admin SDK Reports API | Service account with **domain-wide delegation** (a tenant-wide grant by a super administrator), scope `https://www.googleapis.com/auth/admin.reports.audit.readonly`, acting as an administrator with the Reports privilege | Google service account JWT bearer | Third-party apps people authorized (OAuth token audit log), as totals | Yes |
| Email via Graph | Microsoft Graph | `Mail.Send` through **Exchange RBAC for Applications**, limited to one mailbox | Application (Exchange RBAC) | Reminder, digest, subscription and test emails | Only to send through Graph |
| Teams notifications | Teams Workflows webhook | None | n/a | Adaptive Cards posted to a channel | n/a |
| Sign-in (generic OIDC) | Entra, Okta, Google Workspace, any OIDC provider | `openid`, `profile`, `email` | Delegated | Single sign-on per organization, with presets | Yes, for that sign-in |
| End-of-life data (capability `eol`) | endoflife.date API v1 (`https://endoflife.date/api/v1`: `GET /products`, `GET /products/{product}`; `/products/full` for the offline import) | None: public, unauthenticated, MIT-licensed data | Not Microsoft | Release cycles and end-of-life dates of the products organizations track. The worker sends only product names in the URL and a `Softwarden/<version>` User-Agent, never organization data. | On by default; `SOFTWARDEN_EOL_ENABLED=0` turns it off |
| AI assistant | Anthropic Messages API (`api.anthropic.com`: `/v1/messages`, `/v1/messages/count_tokens`) | An Anthropic API key: the organization's, else the installation's `ANTHROPIC_API_KEY`, only with `SOFTWARDEN_ASSISTANT_INSTALLATION_CAP_USD` set | Not Microsoft | Link, create or dismiss suggestions for discovery candidates (what is sent is listed below) | No, off by default |

### The tokens Softwarden asks Microsoft for

- For sign-in: the delegated scopes `openid profile email`.
- For discovery and email, client-credentials tokens:
  - one for `https://graph.microsoft.com/.default` (Graph);
  - one for `https://management.azure.com/.default` (the `azure` source);
  - one per Dataverse environment, `https://<env>.crm[N].dynamics.com/.default`, and with All environments one for `https://service.powerapps.com/.default` (the `powerplatform` source).

Each token is sent only to its own resource's host.

The Graph token carries the application permissions you consented to: `Application.Read.All` (`entra`, and `entra_coverage`), `LicenseAssignment.Read.All` (`entra_licenses`), `AuditLog.Read.All` and `Directory.Read.All` (`entra_shadow_it`), `Synchronization.Read.All` and `Policy.Read.All` (`entra_coverage`), `User.Read.All` (`entra_licence_usage`), and optionally `User.ReadBasic.All` and `AgentIdentity.Read.All`. Email goes through Graph `sendMail` per organization (client credentials, `Mail.Send` through Exchange RBAC for Applications), on the same token endpoint and Graph host, with SMTP as the fallback.

---

## Sign-in: Entra OIDC

Each organization can sign in with OpenID Connect, set up in Administration › Sign-in.

**How it works.** Softwarden uses the authorization code flow with PKCE (S256), state and nonce. It reads the provider's discovery document and signing keys. It accepts RS256 or ES256 keys only, and only those the document lists; on an unknown key ID it fetches the key set once more. The ID token is checked for issuer, audience (and `azp` when present), expiry, nonce and the preset's own rules.

**The Entra preset** takes your tenant GUID and builds `https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration`. It never uses `common`, `organizations` or `consumers`. The document's issuer and the token's issuer must be `https://login.microsoftonline.com/{tenant}/v2.0`, and the token's `tid` must be the tenant. An identity is pinned on `tid` + `oid`, because `sub` is different for every app. The client secret is sent in the form body (`client_secret_post`).

**What to enter in Entra.** The redirect URI is `{SOFTWARDEN_BASE_URL}/api/v1/auth/oidc/callback`.

The installation-wide `SOFTWARDEN_OIDC_TENANT`, `SOFTWARDEN_OIDC_CLIENT_ID` and `SOFTWARDEN_OIDC_CLIENT_SECRET` still work as a legacy Entra configuration for an installation with a single organization.

| API | Permission | Type | Why | Admin consent |
|---|---|---|---|---|
| Microsoft identity platform (listed under Microsoft Graph in the portal) | `openid` | Delegated | OIDC sign-in | Recommended: grant it for the tenant so users aren't prompted |
| Microsoft identity platform (listed under Microsoft Graph in the portal) | `profile` | Delegated | Name claims in the ID token | Recommended |
| Microsoft identity platform (listed under Microsoft Graph in the portal) | `email` | Delegated | Email claim, used once to pin the identity to an existing member | Recommended |
| Microsoft Graph | `User.Read` | Delegated | Added by default to new registrations. Softwarden reads claims from the ID token and doesn't call Graph, so you can remove it. | Not requested |

- No `offline_access`, and no tokens are stored. The sign-in registration needs no application permissions, **as long as it is a separate registration** from discovery. There are no group claims: roles are assigned in Softwarden.
- Sign-in matches an existing member of the organization only, and nobody is created by signing in. With auto-provisioning on (`sso_auto_provision` in Organization settings, off by default), someone who matches no member is recorded as a pending access request, with no account and no role, for an Admin to approve or decline.
- **Linking a member on the first sign-in.** A returning identity is matched on `tid` + `oid` only. A first sign-in links an existing member by the `email` claim only, and only when the token is a member's of the pinned tenant (`idp` absent, equal to `iss` or naming the tenant; `acct` absent or 0). `preferred_username` never links, because Microsoft documents it as mutable and not for authorization decisions ([ID token claims reference](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference), [optional claims reference](https://learn.microsoft.com/en-us/entra/identity-platform/optional-claims-reference)). Anything else waits in pending access for an Admin. This needs no extra permission or scope.
- The test sign-in (draft, test, activate) is a real delegated sign-in with the same scopes, and needs no extra permission.
- **Okta and Google Workspace** need no Microsoft permission.
  - Okta: discovery at `https://{domain}/.well-known/openid-configuration` (the org authorization server, issuer `https://{domain}`) or `https://{domain}/oauth2/{id}/.well-known/openid-configuration` (a custom server, issuer `https://{domain}/oauth2/{id}`; `default` for the default one). Pinned on `iss` + `sub`. `email_verified` is required.
  - Google Workspace: discovery at `https://accounts.google.com/.well-known/openid-configuration`. `iss` is `https://accounts.google.com` or `accounts.google.com` (both accepted, pinned as the first). Pinned on `sub`. `hd` must be one of your configured Workspace domains (checked on the server). `email_verified` is required, also when sent as the string "true".
  - Both use the scopes `openid profile email`. The secret goes by HTTP Basic (`client_secret_basic`) when the discovery document lists it, else in the form body.
- **Generic OpenID Connect.** The discovery document's `token_endpoint`, where the client secret is sent, must be on the discovery URL's own origin (scheme, host and port). Otherwise the document is refused (`discovery_invalid`) and nothing is sent. The named presets keep their providers' documented hosts (Google's token endpoint is on another host than its discovery document). **Known incompatibility:** Amazon Cognito serves discovery on `cognito-idp.<region>.amazonaws.com` and its token endpoint on `<domain>.auth.<region>.amazoncognito.com`, so it can't be used with the generic preset; the refusal names both hosts.

## Discovery source: `entra`

Lists your enterprise applications and app registrations, with their owners and the end dates of their client secrets and certificates. Capability `discovery.entra`, in every edition.

Softwarden signs in as the app with client credentials, against `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token` with scope `https://graph.microsoft.com/.default`, using the connector's tested Microsoft connection. [The first-run guide](FIRST_RUN.md) walks through the registration and consent.

| API | Permission | Type | Why | Admin consent | Required |
|---|---|---|---|---|---|
| Microsoft Graph | `Application.Read.All` (app role ID `9a5d68dd-52b0-4cc2-bd40-abcf44ac3a30`) | Application | List enterprise applications and app registrations with their owners (IDs) and the end dates of their `passwordCredentials` and `keyCredentials` | Yes | Yes |
| Microsoft Graph | `User.ReadBasic.All` (app role ID `97235f07-e226-4f63-ace3-39588e11d3a1`) | Application | Fill in owners' names and emails | Yes | No |
| Microsoft Graph | `AgentIdentity.Read.All` (app role ID `b2b8f011-2898-4234-9092-5059f6c1ebfa`) | Application | `GET /servicePrincipals/microsoft.graph.agentIdentity` (agent identities) | Yes | No, off by default |

What a run calls:

```
GET https://graph.microsoft.com/v1.0/servicePrincipals?$expand=owners($select=id,displayName,mail,userPrincipalName)&$top=100
GET https://graph.microsoft.com/v1.0/applications?$expand=owners($select=id,displayName,mail,userPrincipalName)&$top=100
GET https://graph.microsoft.com/v1.0/servicePrincipals/microsoft.graph.agentIdentity?$top=100     only with scope include_agent_identities
GET https://graph.microsoft.com/v1.0/{applications|servicePrincipals}/{id}/owners?$select=id,displayName,mail,userPrincipalName&$top=100
    only for an object whose expanded owners hit the 20-item $expand limit
    (every list follows @odata.nextLink, only on graph.microsoft.com; a full listing every run, no delta query)
```

- **One candidate per application.** A registration and its enterprise app are one observation, matched by application (client) ID (`appId`). An agent identity is matched by its object ID.
- **Credentials.** The end dates of `passwordCredentials` and `keyCredentials` come with the default properties of both lists. The lists use no `$select`, because `$select=keyCredentials` is throttled to 150 requests a minute per tenant. Key material is null on lists, and the secret's `hint` (its first three characters) is never stored. Each credential (`keyId`) is kept once: a certificate-backed credential appears in both lists, and the registration's copy wins over the enterprise app's, a certificate over a secret. Softwarden stores the end date's UTC day.
- **Owners.** Owners are expanded on each list. With `Application.Read.All` alone, a user owner comes back with its ID and type and every other property empty. The run notes how many owners came back unnamed, and each shows as the gap "owner not resolved". Owners are matched to People by Entra object ID (a Person whose user signed in with Entra under that ID), else by email. Graph's `$expand` returns at most 20 related objects and no next link, so an object that comes back with 20 owners has its owners read again from `/{applications|servicePrincipals}/{id}/owners`, paged, with the same permission. Only those objects cost an extra call. `$expand=owners` was already in use on service principals; that it behaves the same on `/applications` is assumed until checked on a real tenant.
- **Microsoft's own apps** are skipped by default and counted under "published by Microsoft": those whose `appOwnerOrganizationId` is in the scope's `first_party_tenants`. The defaults are `f8cdef31-a31e-4b4a-93e4-5f571e91255a` (Microsoft Services, documented by Microsoft) and `72f988bf-86f1-41af-91ab-2d7cd011db47` (Microsoft's corporate tenant, not documented by Microsoft). You can edit the list per connector. Managed identities, legacy principals and social identity provider principals are skipped as "not an application", and disabled apps are skipped by default.
- **AI agents.** An object becomes an `ai_agent` candidate when it has one of the tags `AgentCreatedBy:CopilotStudio`, `AgenticApp`, `AIAgentBuilder`, `AgenticInstance` or `power-virtual-agents-*` (documented by Microsoft), or when its `servicePrincipalType` is `ServiceIdentity` or `#microsoft.graph.agentIdentity`. Microsoft has no tag named `AIAgent`. Microsoft doesn't say whether `Application.Read.All` alone returns agent identities in `GET /servicePrincipals`, so both paths are handled as one observation (same object ID), and `include_agent_identities` decides for both: while it is off, an agent identity from either list is skipped and counted as "agent identity (not included)". Agent identity blueprints (`#microsoft.graph.agentIdentityBlueprint` in `/applications`, the matching blueprint principal in `/servicePrincipals`) are kept as applications flagged `agent_blueprint`, never as agents, and the run notes how many it saw. How they list on a real tenant hasn't been checked yet.
- **Throttling and time limits.** A `429` or `503` is retried after `Retry-After` seconds, or else with exponential backoff (2, 4, 8 … seconds, capped at 120), at most 8 retries per request and 15 minutes of waiting per run. A `Retry-After` above 120 seconds stops the run instead, since retrying early only counts against the limit. Every run also has a 40-minute wall-clock limit for requests and waits together, checked before each request, so a slow or throttled tenant can't hold the worker past its one-hour limit for a stuck run. Either way the run fails with a readable reason and changes nothing.
- **401** means the token wasn't accepted (authentication, not a permission). The run signs in again once and retries. A second 401 fails with "didn't accept the app's token".
- **Errors name the permission.** For example, `403 Authorization_RequestDenied` reads "Microsoft Graph refused to list app registrations (403 Authorization_RequestDenied): the app needs the application permission Application.Read.All with admin consent". Messages carry the status and Graph's `error.code`, never Microsoft's message text, a token or a secret.
- **Empty listing guard.** If a list comes back empty (`200` with an empty `value`: revoked consent, a filtered or faulty page) while live observations or discovered credentials came from it, the run fails with "Nothing was changed", naming the list. No observation goes "no longer seen" or loses its enterprise app or registration, and no discovered credential is removed. This holds for both lists together and for each on its own: the enterprise app list alone would make apps without a registration disappear, and the app registration list alone would remove the registrations' credentials.
  - The same guard covers `entra_licenses` and `halo` (their one list), `azure` (each resource list per subscription and Automation account) and `powerplatform` (each environment's agent and flow lists).
  - Observations already gone, in a scope the run couldn't read, or out of the connector's scope don't count, and a new connector has nothing to lose.
  - **When everything really was deleted,** the refusal says what to do: an Admin runs the connector once with "accept empty listing" (`accept_empty_listing: true` on `POST /connectors/{id}/run`). That one run skips the guard, marks those observations "no longer seen" and removes their credentials. It is recorded on the run (`counts.accept_empty_listing`) and in the `connector.run_requested` audit event. Scheduled runs never set it, and later runs are guarded again. Deleting the connector and adding it again also works, but loses its candidates' links to records and its dismissals.

## Discovery source: `entra_licenses`

Reads the seat counts of your tenant's subscribed SKUs, for license pools. It is its own connector (capability `discovery.entra`), usually on the same Microsoft connection as `entra`.

```
GET https://graph.microsoft.com/v1.0/subscribedSkus?$select=skuId,skuPartNumber,appliesTo,capabilityStatus,consumedUnits,prepaidUnits
```

| API | Permission | Type | Why | Admin consent | Required |
|---|---|---|---|---|---|
| Microsoft Graph | `LicenseAssignment.Read.All` (app role ID `e2f98668-2877-4f38-a2f4-8202e0717aa1`) | Application | One `license` observation per SKU, with seat counts | Yes | Yes |

`LicenseAssignment.Read.All` is the least-privileged permission for this list. The higher `Directory.Read.All`, `Organization.Read.All` and their write forms aren't needed.

- The list supports `$select` only (no `$filter`). Microsoft documents no paging, but a next link would be followed (on graph.microsoft.com only).
- **Seats.** Purchased is `prepaidUnits.enabled`, assigned is `consumedUnits`. `warning`, `suspended` and `lockedOut` are stored beside them (`seats_warning`, `seats_suspended`, `seats_locked_out`) and never count as purchased, so a suspended SKU shows 0 purchased.
- Counts are assignments, not usage. Discovery updates seat counts only on a license pool explicitly bound to the SKU with seat sync on (audited as a system edit). SKUs that no pool has are listed as unbound proposals.

## Discovery source: `entra_shadow_it`

Finds third-party apps people sign in to, or consented to, that nobody registered in the catalog, and shows them as discovery candidates with usage counts. It is its own connector (capability `discovery.entra`), usually on the same Microsoft connection as `entra`. It is separate from `entra` so that an organization without Entra ID P1 or P2, or without these consents, can leave it off.

```
GET https://graph.microsoft.com/v1.0/organization?$select=id                                   the tenant's own ID
GET https://graph.microsoft.com/v1.0/servicePrincipals?$select=id,appId,displayName,appOwnerOrganizationId,publisherName,verifiedPublisher,servicePrincipalType,homepage,accountEnabled,description&$top=999
GET https://graph.microsoft.com/v1.0/oauth2PermissionGrants
GET https://graph.microsoft.com/v1.0/auditLogs/signIns?$filter=createdDateTime ge {start} and createdDateTime le {end}&$top=1000
    (every list follows @odata.nextLink, only on graph.microsoft.com; the sign-in read only with the scope's sign_ins on)
```

| Endpoint | Permission | Type | License | Retention | Throttling and paging | Required |
|---|---|---|---|---|---|---|
| `GET /v1.0/auditLogs/signIns` | `AuditLog.Read.All` (least privileged; app role ID `b0afded3-3588-46d8-8b3d-9842eff778da`) | Application | Microsoft Entra ID **P1 or P2** in the tenant. Without it Graph answers `403 Authentication_RequestFromNonPremiumTenantOrB2CTenant`. | 30 days with P1 or P2 (7 days on Free, which can't read the API anyway) | 5 requests per 10 seconds per app per tenant. `$top` at most 1000, newest first. Always filtered by a `createdDateTime` window, or the read times out. Supported filters: `createdDateTime ge/le`, `appId eq`, `appDisplayName eq/startsWith`, `userId eq`. `$select` isn't documented, so it isn't used. No server-side totals. | Yes, unless the scope's **Read sign-in logs** (`sign_ins`) is off |
| `GET /v1.0/oauth2PermissionGrants` | `Directory.Read.All` (least privileged) | Application | None | Current state, not a log | `$filter` with `eq` only; follow the next link | Yes |
| `GET /v1.0/servicePrincipals` (`$select`) | `Directory.Read.All` (`Application.Read.All` also allows it) | Application | None | Current state | `$top` 999; follow the next link | Yes, through `Directory.Read.All` |
| `GET /v1.0/organization?$select=id` | `Directory.Read.All` | Application | None | n/a | One call per run | Yes, through `Directory.Read.All` |

Microsoft lists `Application.Read.All` as the least-privileged permission for the service principal list and `Organization.Read.All` for the organization read. That `Directory.Read.All` covers both is assumed; the access check proves it either way.

- **What a run reads.** A run counts sign-ins from where the last run stopped (every sign-in created before that point was counted) up to 15 minutes ago, and never further back than a rolling window of exactly 30 UTC days (today and the 29 days before, from midnight UTC), which is within Entra's 30-day retention. A first run reads the whole window.
  - It reads in windows, starting at the size the last run ended with (3 days at first).
  - A throttled first page (`429`, `503`) is waited out (`Retry-After`, else exponential backoff, within the run's throttling budget) and the same window is read again.
  - A window is **halved** (down to an hour) and read again when any page answers `504`, or when it has more than 2,000 pages of 1,000.
  - When a window can't finish within the run's time (30 minutes of reading, checked before every page against the time a page has taken, or the run's 40-minute limit), the run stops there and stores the halved size for the next run.
  - After a complete window, the size doubles back towards 3 days, but only when a window twice as long (about twice its pages, at the time a page has taken so far) fits in the time left.
  - Later pages are retried like every Graph read. On a real tenant, reads are spaced 2.2 seconds apart (5 per 10 seconds per app, with a margin for the `entra` connector on the same registration).
  - A window counts only when it was read completely, and the run's starting point moves to the end of the last complete one, so a later run never counts a sign-in twice.
  - A run that stops after at least one complete window (out of time, a throttle it can't wait out, a `500` or `502`) keeps what it counted, says so in its notes, and the next run continues. A run that runs out of time before any window completes succeeds with a note and a smaller window for the next run, so a large tenant advances run by run. A run whose first window fails for another reason fails and stores nothing.
  - Graph v1.0 returns interactive and federated sign-ins only, and those are what is counted. Failed sign-ins (`status.errorCode` not 0) are counted apart and don't count as use.
  - The 15-minute lag is Softwarden's assumption about log latency; Microsoft doesn't document it.
- **What counts as third-party.** An app whose service principal's `appOwnerOrganizationId` isn't your tenant's ID and isn't in the scope's `first_party_tenants` (the same list and defaults as the `entra` source). An app with no service principal in the tenant (typically a Microsoft app) is skipped as "no enterprise app", your tenant's own apps as "published by this tenant", managed identities and legacy principals as "not an application".
- **Consent.** `consentType` `AllPrincipals` is admin consent (`principalId` null), `Principal` is one user's. `clientId` is the client's service principal object ID, mapped to its application ID through the service principal list. `scope` is space-separated.
- **High-risk scopes** (`risky_scopes`), compared ignoring case: `offline_access`, `Mail.Read`, `Mail.ReadWrite`, `Mail.Send` and their `.Shared` forms, `MailboxSettings.ReadWrite`, `Files.Read.All`, `Files.ReadWrite.All`, `Sites.Read.All`, `Sites.ReadWrite.All`, `Sites.Manage.All`, `Sites.FullControl.All`, `Notes.ReadWrite.All`, `Calendars.ReadWrite`, `Contacts.ReadWrite`, `User.ReadWrite.All`, `Group.ReadWrite.All`, `Application.ReadWrite.All`, `AppRoleAssignment.ReadWrite.All`, `full_access_as_user`, `EWS.AccessAsUser.All`, and anything starting with `Directory.` or `RoleManagement.`.
- **Privacy: what is stored.**
  - Per connector: where the last run stopped, and a random 32-byte key that never appears in a response, log, audit event or organization export.
  - Per third-party app per UTC day: the successful sign-in count, the distinct users that day, and a HyperLogLog sketch (4,096 one-byte registers, zlib-packed) of HMAC-SHA256(key, user object ID). Distinct users over several days are counted from the sketches, with about 1.6% standard error (exact in practice for small numbers). Days older than the rolling 30 are deleted by the next run.
  - Per app, the overview: users and sign-ins over 30 days, last sign-in time, consent type, how many people consented, and the scopes.
  - **Never stored:** sign-in rows or their IDs, user IDs, names, addresses, IP addresses, locations, devices, correlation IDs, or who consented. User IDs are hashed into the sketch in memory and dropped. A sketch can't list its users. With the key, someone could test whether one known user is likely among an app's users on a given day, which is why the key never leaves the database.
- **Apps already in the catalog** (a live application record with the app's Entra application ID, or an observation of an `entra` connector) aren't candidates. They are skipped as "already in the catalog" and kept in the overview with their usage. Nothing writes to Entra or to a curated record.
- **Inactive apps aren't gone.** An app this connector saw before, whose enterprise app is still in the tenant but which had no sign-in in the rolling window and has no consent grant, is skipped as "inactive" (or "not read" with `sign_ins` off). Its candidate is out of scope, never "no longer seen", so a record linked to it isn't flagged missing. An app whose enterprise app was deleted from the tenant becomes "no longer seen" at the next run, whatever its recent usage, so a record linked to it shows as missing.
- **Dismissed candidates** come back only when their risk rises: a more permissive consent type, or a high-risk scope they never had. Usage, a lost scope or a reduced consent never bring one back, and usage changes never count as a change of the candidate.
- **Errors.** `403 Authorization_RequestDenied` names the permission (`AuditLog.Read.All` for the sign-in logs, `Directory.Read.All` for the rest). Without a P1 or P2 license the run says: "Microsoft Graph refused to read the sign-in logs (403 Authentication_RequestFromNonPremiumTenantOrB2CTenant): reading them needs a Microsoft Entra ID P1 or P2 license in the tenant, and this tenant has none. Add one, or turn off Read sign-in logs in this connector's scope to find shadow IT from consent grants only." Either way the run fails and stores nothing.

## Discovery source: `entra_coverage`

For every catalog application linked to an Entra app (its `entra_app_id`, or a linked `entra` or `entra_shadow_it` candidate), records whether it is behind single sign-on, whether SCIM provisioning is on, and whether conditional access requires MFA, for all users or only some. It is its own connector (capability `discovery.entra`), usually on the same Microsoft connection as `entra`, and separate so that an organization without Entra ID P1, or without these consents, can leave it off. It writes nothing to Entra and never writes a catalog record.

```
GET https://graph.microsoft.com/v1.0/servicePrincipals?$select=id,appId,displayName,appOwnerOrganizationId,servicePrincipalType,preferredSingleSignOnMode,preferredTokenSigningKeyThumbprint,samlSingleSignOnSettings&$top=999
GET https://graph.microsoft.com/v1.0/servicePrincipals/{id}/synchronization/jobs          per linked app, at most 100 a run
GET https://graph.microsoft.com/v1.0/identity/conditionalAccess/policies                 once a run
    (every list follows @odata.nextLink, only on graph.microsoft.com)
```

| Endpoint | Permission | Type | License | Throttling and paging | Required |
|---|---|---|---|---|---|
| `GET /v1.0/servicePrincipals` (`$select` with `preferredSingleSignOnMode`) | `Application.Read.All` (held by `entra`) | Application | None | `$top` 999; follow the next link | Yes |
| `GET /v1.0/servicePrincipals/{id}/synchronization/jobs` | `Synchronization.Read.All` (app role ID `5ba43d2f-fa88-4db2-bd1c-a67c5f0fb1ce`). **Never** `Application.ReadWrite.OwnedBy`, which writes. | Application | None | One call per service principal, so at most 100 a run, least recently read first; the others keep their last answer. Graph defaults (`Retry-After` honored). | Yes, unless the scope's **Read provisioning (SCIM) jobs** (`provisioning`) is off |
| `GET /v1.0/identity/conditionalAccess/policies` | `Policy.Read.All` (app role ID `246dd0d5-5bd0-4def-940b-0421030a5b68`) | Application | Microsoft Entra ID **P1** (P2 for risk conditions) | **1 request per second per tenant, and a 429 carries no `Retry-After`.** Read once a run, pages 1.1 seconds apart. A throttled read backs off on its own (2, 4, 8, 16 seconds; a `Retry-After` is honored if one ever comes), then gives up for that run. | Yes, unless the scope's **Read conditional access policies** (`conditional_access`) is off |

- **Single sign-on** (`sso`) is `saml`, `oidc`, `password`, `none` (`notSupported`) or `unknown`. `password` is Entra's password-based SSO: the app is behind My Apps but not federated, and it counts as SSO for the record's `sso_enabled`, as Entra counts it. Microsoft documents that `preferredSingleSignOnMode` is null for older SAML and OIDC apps, so a null mode is **unknown, never "no SSO"**, unless the app has a SAML token signing certificate or SAML settings (`preferredTokenSigningKeyThumbprint`, `samlSingleSignOnSettings`), which make it `saml`.
- **Provisioning** (`scim`) is on when a job's `schedule.state` is Active and its `status.code` is neither Paused nor Quarantine. It is off with no job (an empty list, or a 404), a paused or quarantined job, or a schedule that isn't active.
- **Conditional access** (`mfa`), per app:
  - Only `enabled` policies enforce. Report-only policies are listed as `report_only`; disabled ones are ignored.
  - The app must be in `conditions.applications.includeApplications` (its app ID or `All`) and not in `excludeApplications`. `Office365` and `MicrosoftAdminPortals` may cover a Microsoft-published app but never a third-party one, so including or excluding either makes a Microsoft app unknown and leaves a third-party app as the rest of the policy says. An `applicationFilter` can't be evaluated, so a policy whose coverage of the app depends on one is unknown.
  - `conditions.users.includeUsers` `All` is all users. Exclusions, such as break-glass accounts, are counted and shown, not judged. Roles (an admin-only policy), groups, guests or listed people are some users. So is a policy limited to some locations, device platforms, client app types, devices (a device filter, or the legacy `includeDevices` / `excludeDevices`), sign-in, user, service principal or insider risk levels, authentication flows (device code flow, authentication transfer), workload identities, or any condition Softwarden doesn't know: an unknown shape can never give "all users". Users `None` covers nobody.
  - An enabled policy whose grant is `block` has the effect `blocked`. One that blocks all users with no exclusion and no narrowing makes the app `blocked`, not a missing MFA policy. A block with exclusions ("everyone except Sales", or break-glass accounts) decides nothing, and the other policies say what the excluded people get.
  - MFA is required when `grantControls.builtInControls` contains `mfa` or `authenticationStrength.requirementsSatisfied` is `mfa`, unless the operator is `OR` with another control beside it (then MFA is optional). Graph writes `OR` even for a single control, which still requires MFA.
  - The result: `blocked` if one policy blocks everyone, else `all_users` if one requires MFA of everyone, else `some_users`, else `unknown` if a filter or group decides it, else `none`.
- **Missing license or permission doesn't fail the run.** In a tenant without P1, or without `Policy.Read.All` (any other 403), SSO and provisioning are still reported, MFA is "unknown" with the reason ("needs Microsoft Entra ID P1"), and the run's notes say so. Microsoft doesn't document the error code for a tenant without P1 on this endpoint; Softwarden expects `403 Authentication_RequestFromNonPremiumTenantOrB2CTenant`, as for the sign-in logs, and treats any 403 the same way. The access check shows the missing license as a warning (`missing_licence`), not a failure, and names both possible causes (the permission or the P1 license) for any other 403 on conditional access. A refused provisioning read (`Synchronization.Read.All` missing) stops the provisioning reads for that run (provisioning unknown, with a note). A 404 means "no provisioning job".
- **Brief outages keep the last answers.** A conditional access read that stays throttled past its retries, answers a 5xx, or can't be reached keeps every app's last `mfa`, detail and policies. A provisioning read that answers a 5xx keeps that app's last answer, and throttling past the run's budget or an unreachable Graph stops the provisioning reads and keeps the rest's last answers. An answer is kept only for the same service principal (a tenant change or a re-created app counts as another one) and for at most 7 days after it was read (`mfa_checked_at`, `scim_checked_at`); after that it becomes unknown, saying since when. Apps waiting their turn past the provisioning cap keep their answer whatever its age. The run notes what was carried over. Only a 401 or 403, or turning that part of the scope off, resets the answers to unknown.
- **Security defaults** (MFA without conditional access, in tenants without P1) aren't read.
- **Empty listing guard.** If the service principal listing comes back empty while the connector has coverage from earlier runs, the run fails with "Nothing was changed" rather than deleting every row.

## Discovery source: `entra_licence_usage`

Stores which licenses each licensed user holds and the date of their last successful sign-in, for the license waste report (`GET /api/v1/reports/licence-waste`). It is its own connector (capability `discovery.entra`), usually on the same Microsoft connection as `entra` and `entra_licenses`. It is the first source that reads per-user data, so it is separate from `entra_licenses` (seat counts, `LicenseAssignment.Read.All`): an organization that only wants seat counts never grants these consents.

**Both permissions are tenant-wide admin consents.** `User.Read.All` lets the app read every user's full profile in the tenant, and `AuditLog.Read.All` every audit and sign-in log. A Privileged Role Administrator (or Global Administrator) must grant them in the app registration before a live run. Softwarden never asks for them by itself. To count licenses without sign-in activity, grant only `User.Read.All`.

```
GET https://graph.microsoft.com/v1.0/users?$select=id,accountEnabled,assignedLicenses,licenseAssignmentStates,userType,createdDateTime,mail,userPrincipalName,signInActivity&$top=500
GET https://graph.microsoft.com/v1.0/users?$select=id,accountEnabled,assignedLicenses,licenseAssignmentStates,userType,createdDateTime,mail,userPrincipalName&$top=999
    (the second only when the first is refused with 403, runs past 25 minutes (it is paced at ten pages a minute),
     or the scope's sign_in_activity is off; both follow @odata.nextLink, only on graph.microsoft.com)
```

| Endpoint | Permission | Type | License | Throttling and paging | Required |
|---|---|---|---|---|---|
| `GET /v1.0/users` with `$select=...assignedLicenses...` | `User.Read.All` (app role ID `df021288-bdef-4463-88db-98f22de89214`). This is the least-privileged permission that returns `assignedLicenses`: `User.ReadBasic.All` doesn't, and `LicenseAssignment.Read.All` isn't listed for it. | Application | None | `$top` 999; follow the next link; Graph defaults (`Retry-After` honored within the run's budget) | Yes |
| `GET /v1.0/users` with `signInActivity` in `$select` | `AuditLog.Read.All` (app role ID `b0afded3-3588-46d8-8b3d-9842eff778da`) as well as `User.Read.All` | Application | Microsoft Entra ID **P1** (or P2). Without it Graph answers `403 Authentication_RequestFromNonPremiumTenantOrB2CTenant`. | **At most 500 users a page and 10 requests a minute per tenant.** On a real tenant pages are read 6.5 seconds apart, and a 429 waits for `Retry-After` within the run's throttling budget. Graph may return fewer users per page than `$top` with `signInActivity` (older Microsoft documentation said 120), so the read is bounded by users and time, never by pages: at most 100,000 users a run, and the read stops before a page that would start past 25 minutes (about 230 pages: 115,000 users at 500 a page, 27,000 at 120). The run then reads the users again without `signInActivity` (unpaced, `$top` 999) within the rest of its 40-minute limit, and notes that the last sign-in is unknown this run. | No: optional, off with the scope's **Read last sign-in** (`sign_in_activity`) |

Notes from Microsoft's documentation: `assignedLicenses` is returned only when named in `$select`. `users/{id}/licenseDetails` has no application permission, so it is never read. Softwarden uses `lastSuccessfulSignInDateTime` (interactive or non-interactive, successful sign-ins only; `lastSignInDateTime` includes failures), which isn't backfilled before December 2023.

- **Without P1 or the AuditLog consent, the run still works.** A 403 on the read with `signInActivity` (the license code, or `Authorization_RequestDenied`) makes the run read the users again without it. Licenses are counted, every last sign-in is "unknown (needs Entra ID P1 / AuditLog.Read.All)", nothing is counted as inactive, and the run's note names the cause. The access check shows the missing license as a warning (`missing_licence`) and a missing `AuditLog.Read.All` as optional. Without `User.Read.All` the run fails and names it. A Graph listing of users without `assignedLicenses` is refused rather than read as "nobody is licensed".
- **Privacy: what is stored.** Only licensed users are stored, one row each, holding:
  - an HMAC-SHA256 of the user's object ID, keyed by the installation's secret key and the connector (never the object ID itself);
  - the SKU IDs;
  - whether the account is enabled, whether it is a guest (`userType` Guest, or `#EXT#` in the user principal name), and whether it is a service account (by the scope's user principal name prefixes or object IDs);
  - the **date** (not the time) of the last successful sign-in and of creation, and whether any sign-in was recorded;
  - the Person in Softwarden it is linked to.

  **Never stored:** a name, an email address, a user principal name, the object ID, an IP address, a location or a device. Mail and user principal name are held in memory during the run only, to link a member to a Person (by email, the user principal name before mail; guests are linked by identity only, and an ambiguous match is left unlinked) and to match service-account prefixes. The object ID is used only to link through a Person's Entra sign-in identity in this tenant, and to hash.

  Every run rewrites the connector's rows, so the next run deletes any user who is no longer licensed or no longer in the tenant, and deleting the connector deletes them all. The rows are left out of organization exports and tenant backups (personal data the next run reads again), and deleting the organization removes them.
- **Who sees what.** The report, its CSV, and the license pool and contract blocks are counts and money, open to any role. The per-SKU list of users is Admin-only. It shows a linked Person's name, otherwise "Unlinked user" and the first 8 characters of the keyed hash. Its CSV is audited, and both are refused while platform staff impersonate a member. The offboarding footprint lists the SKUs a Person holds, without sign-in dates; until this source has run, it says "Licenses: not read; needs User.Read.All".
- **Group-based licenses.** `assignedLicenses` includes licenses inherited from groups. A seat held only through a group can be reclaimed only by changing the user's group membership, not by removing the license from the user. The run therefore also reads `licenseAssignmentStates` (same `User.Read.All`) and keeps which SKUs a user holds only through groups (`assignedByGroup` on every state of the SKU). The per-SKU list flags them as `group_assigned`.
- **One connector per tenant.** A second `entra_licence_usage` connector on the same connection or tenant ID is refused, since it would count every licensed user twice. Each run also reads the `tid` claim of its access token (the token is read, not verified, and Softwarden reads nothing else from it) and stores a keyed hash of the tenant (`tenant_key`). A run is refused, changing nothing, when another enabled license usage connector's last successful run read the same tenant. That catches a tenant named by domain on one connection and by GUID on another, or changed later in Connections.
- **Empty listing guard.** If the user listing comes back empty, or with no licensed user, while the connector has rows from earlier runs, the run fails with "Nothing was changed" rather than deleting them.

## Discovery source: `azure`

Capability `discovery.azure`, in every edition. Lists Logic Apps, Function Apps, App Services (optional) and Automation runbooks. Softwarden signs in with client credentials from the connector's tested Microsoft connection, scope `https://management.azure.com/.default`. The token is sent only to `management.azure.com`: every request and every `nextLink` is checked before the token is attached. No Graph permission is needed.

```
GET https://management.azure.com/subscriptions?api-version=2022-12-01                                    no subscription chosen: every one the app has a role on
GET https://management.azure.com/subscriptions/{id}?api-version=2022-12-01                               each chosen subscription
GET https://management.azure.com/subscriptions/{id}/providers/Microsoft.Logic/workflows?api-version=2019-05-01
GET https://management.azure.com/subscriptions/{id}/providers/Microsoft.Web/sites?api-version=2024-04-01
GET https://management.azure.com/subscriptions/{id}/providers/Microsoft.Automation/automationAccounts?api-version=2024-10-23
GET https://management.azure.com{automation account id}/runbooks?api-version=2024-10-23
    (each list follows nextLink, only on management.azure.com; a full listing every run)
```

| API | Grant | Type | Scope | Why |
|---|---|---|---|---|
| Azure Resource Manager | **Reader** (role definition ID `acdd72a7-3385-48ef-bd42-f606fba81ae7`, action `*/read`) | Azure RBAC role assignment | Each subscription to read, or a management group above them (inherited) | List subscriptions, Logic Apps, web apps, Automation accounts and runbooks |

To assign it:

```bash
az role assignment create --assignee <app client id> --role Reader --scope /subscriptions/<subscription id>
# or once for a management group: --scope /providers/Microsoft.Management/managementGroups/<group id>
```

- **Per-type lists, not the generic Resources list.** The generic list returns no `properties`, so descriptions would come out empty. Resource Graph isn't used.
- **Subscriptions.** With none chosen, every subscription that Subscriptions - List returns is read, so Reader on a management group covers all the subscriptions under it (no management-group API call is made). Only Enabled, Warned and PastDue subscriptions are read; others are noted, and their candidates keep their state. A chosen subscription the app can't read is noted, and its candidates keep their state. Subscriptions - Get answers `404 SubscriptionNotFound` for a subscription the app has no role on, reported as "no Reader on this subscription, or the ID is wrong".
- **Each resource type is read on its own.** One list refused leaves that type's candidates as they were while the others update. A resource provider the subscription never registered (`409 MissingSubscriptionRegistration`) is an empty list, noted (and `ok` in the access check). The run fails only when nothing could be read.
- **Web apps by `kind`.** `workflowapp` is read as a Standard Logic App, `functionapp` (or `functionapp,linux`) as a Function App, and `app`, `api` or no kind as an App Service (kept only with scope `include_app_services`). Any other kind is skipped as "not recognized", and the run notes the kind strings. Microsoft documents only the `APP_KIND` setting `workflowApp` for Standard Logic Apps, so the `workflowapp` kind, and the kinds of Flex Consumption apps, haven't been checked on a real subscription yet.
- **Errors.** A missing role is `403 AuthorizationFailed`. The run and the access check name the action and scope Azure gave (and nothing else of Microsoft's message) and the fix: "Assign the Azure role Reader to the connection's app on subscription X". Role changes can take minutes to reach tokens. `429` and `503` are retried after `Retry-After`, as for Graph.
- **Kept:** name, resource ID, type, subscription, resource group, location, tags (50 at most), state, kind, runbook type and description, managed identity type. **Never kept:** a Logic App's definition or a site's configuration, since either can hold secrets.

## Discovery source: `powerplatform`

Capability `discovery.powerplatform`, in every edition. Lists Copilot Studio agents as AI agent candidates, and optionally cloud flows. Power Platform doesn't use Entra API permissions for service principals: access is a Dataverse application user with a security role.

**Least privilege (the default): environments chosen by URL** (scope `environment_urls`, `https://<name>.crm[N].dynamics.com`; any other address is refused when saved). Each environment gets its own token, scope `https://<env host>/.default`, sent only to that host.

```
GET {env}/api/data/v9.2/bots?$select=botid,name,schemaname,statecode,statuscode,authenticationmode,accesscontrolpolicy,language,publishedon,componentstate,_ownerid_value
    Prefer: odata.include-annotations="OData.Community.Display.V1.FormattedValue",odata.maxpagesize=500
GET {env}/api/data/v9.2/workflows?$select=workflowid,name,statecode,description,_ownerid_value&$filter=category eq 5     only with include_cloud_flows
    (each follows @odata.nextLink, only on that environment's host)
```

| API | Grant | Type | Why | Required |
|---|---|---|---|---|
| Dataverse Web API | An application user in each environment with a read-only role: read on **Agent** (`bot`, `prvReadbot`), organization level | Dataverse security role | Copilot Studio agents as AI agent candidates | Yes |
| Dataverse Web API | The same role: read on **Process** (`workflow`, `prvReadWorkflow`) | Dataverse security role | Cloud flows (category 5, Modern Flow) | No; scope `include_cloud_flows` |
| Dataverse Web API | The predefined **Service Reader** role, only if the read-only role proves insufficient | Dataverse security role | Fallback | No |

Microsoft doesn't document `prvReadbot` and `prvReadWorkflow` as the privileges this needs. On a fresh install, a custom read-only role alone has been seen not to be enough, and Service Reader was needed.

- **Setup.** Register the app in Entra, then in the Power Platform admin center open Environment › Settings › Users + permissions › Application users › New app user, with the business unit and the security role above. Avoid System Administrator.
- **Errors.** Dataverse answers a caller that isn't a user of the environment with `401`, and a missing privilege with `403` (types such as `PrivilegeDenied` and `AccessDenied`; codes are often empty or hex). Both read "needs an application user with read on Agent (bot) rows", naming the privilege when Dataverse's message names one. A Dataverse host the tenant doesn't know fails sign-in (Softwarden expects `AADSTS500011`), shown as "not found: check the address".
- An environment that can't be read (refused, no token, unreachable, throttled out) is noted, and its candidates keep their state. Only the run's 40-minute limit stops the whole run. Flows that can't be read are a note, and the agents are still read. Rows without an ID are skipped and counted ("no ID"). Agents in component state Deleted (2, 3) are skipped. A flow's `clientdata` (its definition) is never read. Workflow `statecode` values are read as 0 Draft, 1 Activated, 2 Suspended.
- An environment chosen by URL is named by its host; its environment ID is known only through All environments.

**All environments (opt-in, scope `all_environments`): the Power Platform admin API (BAP).**

```
GET https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/scopes/admin/environments?api-version=2020-10-01     token for https://service.powerapps.com/
```

| API | Grant | Type | Why | Required |
|---|---|---|---|---|
| Power Platform admin API (BAP) | Register the app for the admin API: `PUT https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/adminApplications/{client id}?api-version=2020-10-01` by an administrator signed in as themselves, or `pac admin create-service-principal` ([Power Platform service principal](https://learn.microsoft.com/en-us/power-platform/admin/powerplatform-api-create-service-principal)) | Admin-equivalent registration | List the tenant's environments | No, off by default |

- **Weigh this before you turn it on.** Microsoft treats such an app like a user with the Power Platform Administrator role, and granular roles can't limit it. The app shows this warning, and choosing environments by URL stays the default.
- The newer Power Platform API (`api.powerplatform.com`, `EnvironmentManagement.Environments.Read`) isn't used, because its service-principal roles couldn't be confirmed.
- An environment's `instanceUrl` is used only when it is a Dataverse host; any other is noted and never sent a token. Environments without a Dataverse database are noted, and if no environment is left to read, the run fails with those notes. If the list is refused (`401` after a fresh token, or `403`: not registered), the run fails, because an environment it would have read could otherwise look gone. The access check names "Power Platform management app".

## Discovery source: `halo`

Capability `discovery.halo`, in every edition. Not Microsoft; listed so that every outbound credential is in one place. Reads HaloITSM assets as application candidates, with their Halo asset ID, through the connector's Halo connection (encrypted secret). The API conventions come from Halo's integration guides; your instance's own `{base}/apidoc` is the reference.

```
POST {base}/auth/token                                   client credentials, scope default "read:assets"
GET  {base}/api/Asset?paginate=true&page_size=100&page_no=N     {"record_count", "assets": [...]}, until a short page or record_count
```

| API | Grant | Why | Required |
|---|---|---|---|
| HaloITSM API | An API application (client credentials) with read access to assets, scope `read:assets` (the default). A connection saved before `read:assets` became the default keeps `read:assets read:tickets` until you change it. Ticket links need `read:tickets` (see below). | Assets as application candidates | Yes |

- **The address.** `base_url` must be https, with no user name, password, query or fragment. Private networks are allowed (an on-premises Halo) unless the installation sets `SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS=false` (a hosted, multi-organization installation), which also refuses RFC 1918, ULA and carrier-grade NAT addresses. Loopback, link-local (169.254.169.254 included), cloud metadata (`fd00:ec2::254`, `100.100.100.200`), unspecified, multicast and reserved addresses are always refused: for a literal address when you save, and for every address the name resolves to when connecting (the token request and every asset read). The request goes to the checked address, with no redirects and a deadline. The token is sent only to the connection's own host and port.
- A refused read (`403`) is "missing permission: read:assets"; `401` means the token wasn't accepted. `429` is retried after `Retry-After`. Assets without an ID are skipped and counted. Paging stops, with a note, if Halo answers a page with the same first asset as the page before.
- **Kept:** the asset ID, key field (else inventory number), asset type, inventory number, client, site, inactive. Inactive assets are skipped by default, and scope `asset_types` limits the types.

## Ticket connections: Jira, ServiceNow, Halo

Capability `ticket_links`. Not Microsoft, and no Microsoft permission is involved; listed so that every outbound credential is in one place. Jira and ServiceNow connections keep an encrypted secret; Halo uses the Halo connection above.

ServiceNow and Halo ticket connections are preview features in 1.0.0: Softwarden makes no request to either unless the installation sets `SOFTWARDEN_PREVIEW_FEATURES` ([preview features](CAPABILITIES.md#preview-features)). The same applies to Okta and Google Workspace below.

```
Jira Cloud (REST v3, basic auth: account email + API token)
GET  {base}/rest/api/3/issue/{key}?fields=status,summary       status name, statusCategory.key, summary
GET  {base}/rest/api/3/myself                                    the connection test
POST {base}/rest/api/3/issue                                     create (opt-in): project key, issue type, summary, ADF description
ServiceNow (Table API, basic auth)
GET  {base}/api/now/table/{table}?sysparm_query=number={key}&sysparm_fields=number,state,short_description&sysparm_display_value=all&sysparm_limit=1
GET  {base}/api/now/table/task?sysparm_limit=1&sysparm_fields=number      the connection test
POST {base}/api/now/table/{table}                                create (opt-in): short_description, description
HaloITSM (client credentials through the Halo connection)
POST {base}/auth/token                                           scope from the Halo connection (must include read:tickets)
GET  {base}/api/Tickets/{id}                                     summary, status_id (status_name when sent)
POST {base}/api/Tickets                                          create (opt-in): [{summary, details, tickettype_id}]
```

| System | Grant | Why |
|---|---|---|
| Jira Cloud | An Atlassian account's API token (basic auth). The account needs Browse projects on the projects whose issues are linked, and Create issues on the create project if creating is on. | Read linked issues' status; optionally create an issue |
| ServiceNow | A user with read access to the task tables (`incident`, `change_request`, `sc_req_item`, `sc_task`, `problem`, `task`), and create on the create table if creating is on (basic auth) | Read linked tickets' state; optionally create one |
| HaloITSM | The Halo connection's API application with `read:tickets` (and `edit:tickets` to create) | Read linked tickets' status; optionally create one |

- **What has been checked.** The Jira endpoints and basic auth with an API token follow Atlassian's documentation; the status category keys `new`, `indeterminate` and `done` are Jira's long-standing keys. ServiceNow's Table API parameters (`sysparm_query`, `sysparm_fields`, `sysparm_display_value`, `sysparm_limit`) and its state labels, and Halo's `GET /api/Tickets/{id}`, `POST /api/Tickets` and status IDs (1 New, 9 Closed), are still to be confirmed on a real system.
- **Addresses.** Every request follows the same rule as the Halo source: https only; no user name or password in the URL; loopback, link-local (169.254.169.254 included), cloud metadata, unspecified, multicast and reserved addresses always refused, and private networks too when `SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS=false`. This is checked for a literal address when you save and for every address the name resolves to when connecting. The request goes to the checked address with no redirects, a 10-second socket timeout and a 20-second deadline. The address is always the connection's own base URL: a link's stored URL is never fetched, and a link on another site isn't read with the connection's credentials.
- **Errors** are stored in Softwarden's own words only: never the server's text, the Authorization header, a token or the secret. Job payloads carry no secret; the job reads the connection when it runs.
- **The hourly refresh** runs in the worker's long lane with a 180-second budget per run and one Halo token per run. It stops reading a system at its first timeout, missing answer, busy answer, refused access, refused credentials or refused address, so a slow system can't hold up the worker or another organization's reads.
- **Creating tickets** is off by default per connection (`create_enabled`), needs the connection tested at its current revision, and every attempt is audited (`ticket_link.ticket_created`, `ticket_link.create_failed`). Only the title and description are sent.

## Discovery source: `okta`

Capability `discovery.okta`, a preview feature in 1.0.0. Not Microsoft; listed so that every outbound credential is in one place. Lists your Okta applications with their sign-on mode and assigned-user counts, through the connector's Okta connection. The private key is encrypted and write-only.

```
POST https://{okta domain}/oauth2/v1/token          grant_type=client_credentials, scope=okta.apps.read,
                                                     client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer,
                                                     client_assertion=<JWT signed with the service app's private key>
GET  https://{okta domain}/api/v1/apps?limit=200&includeNonDeleted=true       then the link header's rel="next"
GET  https://{okta domain}/api/v1/apps/{appId}/users?limit=500                per kept app, while assignment_counts is on
```

| API | Grant | Why |
|---|---|---|
| Okta Management API | OAuth 2.0 scope `okta.apps.read`, granted to the API service app (Okta API Scopes) | List applications and count their assigned users. Read only. `okta.users.read` is accepted on the connection but not needed; any scope that isn't read-only is refused. |
| Okta admin roles | **Read-only Administrator** (`READ_ONLY_ADMIN`) assigned to the service app (Admin roles) | A scope works only within the app's admin role. Read-only Administrator is the least-privileged standard role that reads applications and their users ([Okta help: standard administrator roles](https://help.okta.com/en-us/content/topics/security/administrators-read-only-admin.htm)). Confirm it on your org when you first connect. |

How it signs in, from Okta's documentation ([Implement OAuth for Okta with a service app](https://developer.okta.com/docs/guides/implement-oauth-for-okta-serviceapp/main/)): client credentials with `private_key_jwt`, the only client authentication for service apps that get Okta-scoped tokens. The assertion's `iss` and `sub` are the client ID and its `aud` is `https://{okta domain}/oauth2/v1/token`, valid for at most an hour; tokens last an hour. Pages follow the `link` header's `rel="next"` URL as an opaque value ([pagination](https://developer.okta.com/docs/api/#pagination)).

- **Why a service app, not an SSWS API token.** An SSWS token carries every permission of the administrator who created it, isn't scoped, and is a bearer secret valid until revoked. The service app is scoped to `okta.apps.read`, bounded by a read-only admin role, and its private key never leaves Softwarden: only a five-minute signed assertion is sent. The key is a PEM private key (PKCS#8 or PKCS#1) or a private JWK: RSA of 2048 bits or more (RS256), or EC P-256 (ES256). `key_id` is its `kid` when the app has several keys; a JWK's own `kid` is used otherwise.
- **DPoP.** Okta can require Demonstrating Proof of Possession per app ([DPoP](https://developer.okta.com/docs/guides/dpop/oktaresourceserver/main/)). Softwarden sends no DPoP proof, so leave "Require Demonstrating Proof of Possession (DPoP) header in token requests" off; the connection test says so.
- **The address.** `https://{org}.okta.com`, `.oktapreview.com` or `.okta-emea.com` (one label, never the `-admin` console host or the bare suffix), or a custom domain. https on port 443, with no path, query, user name or IP address. The name is resolved once and refused unless every address is public (a custom domain resolving to loopback, RFC 1918, link-local or a metadata address is refused before anything is sent), and the request goes to the checked address with no redirects and a deadline. The token goes only to that host on 443, also for next-page links.
- **Rate limits.** The limit belongs to your Okta org and is shared with every other integration ([rate limit best practices](https://developer.okta.com/docs/reference/rl-best-practices/)). Once `X-Rate-Limit-Remaining` falls below 10% of `X-Rate-Limit-Limit` (or reaches 0 without a limit header), the next request waits until `X-Rate-Limit-Reset`, leaving the rest to the others. A 429 waits for `Retry-After` if given, else until the reset, within the run's waiting budget. A wait over two minutes stops the run, and the next run tries again.
- **Errors.** Token: `invalid_client` or `unauthorized_client` means a wrong client ID or key; `invalid_scope` means the scope isn't granted; `invalid_dpop_proof` means DPoP is required. Reads: 403 `E0000006` means the admin role can't read (the message names Read-only Administrator); a 401 signs in again once. A refused user count is a note, never a failed run.
- **Kept:** the app's ID, label, key (`name`), sign-on mode, status, whether it has provisioning features, created and last-updated times, and the assigned-user count (a number only, never a user). Okta apps have no owners in the API. Inactive apps and Okta's own apps (`saasure`, `okta_enduser`, `okta_browser_plugin`) are skipped by default; bookmark apps are optional; you can exclude names.
- **Empty listing guard.** An empty application list while the connector has live observations fails the run with "Nothing was changed". If every application really was deleted, an Admin runs the connector once with "accept empty listing" (see the `entra` section).

## Discovery source: `google_workspace`

Capability `discovery.google`, a preview feature in 1.0.0. Not Microsoft. Finds third-party apps people authorized in Google Workspace, from the OAuth token audit log, through the connector's Google Workspace connection.

```
POST https://oauth2.googleapis.com/token            grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer, assertion=<JWT>
GET  https://admin.googleapis.com/admin/reports/v1/activity/users/all/applications/token
     ?eventName=authorize&startTime={now - days}&endTime={now}&maxResults=1000[&pageToken=...]
```

| API | Grant | Why |
|---|---|---|
| Admin SDK Reports API | `https://www.googleapis.com/auth/admin.reports.audit.readonly` through **domain-wide delegation** of the service account's client ID | Read the OAuth token audit log: which third-party apps people authorized, with which scopes. Read only. |
| Google Admin console | The administrator the service account acts as has the **Reports** privilege (a super administrator has it) | The Reports API answers only to administrators allowed to read audit reports. Confirm the exact privilege on your Workspace when you first connect. |

How it signs in and reads, from Google's documentation:

- [Using OAuth 2.0 for server to server applications](https://developers.google.com/identity/protocols/oauth2/service-account): `POST https://oauth2.googleapis.com/token` with `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer` and an RS256 assertion (`kid` the key ID; claims `iss` the service account, `sub` the user to act as, `scope`, `aud` the token URL, `exp` at most an hour after `iat`).
- [Reports API: activities.list](https://developers.google.com/admin-sdk/reports/v1/reference/activities/list) with `eventName`, `startTime`, `endTime`, `maxResults` (default and maximum 1000) and `pageToken` / `nextPageToken`. Data goes back 180 days.
- [OAuth Token Audit Activity Events](https://developers.google.com/admin-sdk/reports/v1/appendix/activity/token): the `authorize` event ("A user authorized access to an application for their data") with `app_name`, `client_id`, `client_type` and `scope`.
- [Reports API limits and quotas](https://developers.google.com/admin-sdk/reports/v1/limits): 2,400 queries per minute per user per project, and for activities.list also 250 filter queries per minute (`eventName` makes it one). Quota errors are 503, retried with exponential backoff.
- [Directory API limits and quotas](https://developers.google.com/workspace/admin/directory/v1/limits) (checked 2026-11-12): the Admin SDK's quota error reasons are 403 `userRateLimitExceeded`, 403 `quotaExceeded` and 429 `rateLimitExceeded`, each "retry using exponential back-off". The source retries those three like a 429 or 503; any other 403 (`forbidden`, `insufficientPermissions`) is reported as the missing Reports privilege.
- [Reports API scopes](https://developers.google.com/admin-sdk/reports/auth) and [Control API access with domain-wide delegation](https://support.google.com/a/answer/162106).

Details:

- **Domain-wide delegation is a tenant-wide grant.** It lets the service account act as any user in the Workspace for the scopes listed, so only a super administrator can make it, and Softwarden never makes it. Authorize exactly one scope, `https://www.googleapis.com/auth/admin.reports.audit.readonly`, for the service account's numeric client ID (shown on the connection), under Security › Access and data control › API controls › Domain-wide delegation. It can take from minutes to a day to apply. Use a service account made for Softwarden only.
- **Why the token audit log.** It is the least-privileged way to see third-party apps: one read-only scope and one paged list for the whole domain. The Directory API's per-user `tokens.list` would need `admin.directory.user.security` (which can also revoke tokens) and a call per user. `admin.directory.user.readonly` isn't needed.
- **The key.** You paste the service account key file (JSON) once. Softwarden keeps only its private key and key ID (encrypted, write-only, never returned, logged or exported) and shows its `client_email` and `client_id`. The file's `token_uri` is ignored: tokens are only requested from `oauth2.googleapis.com`. Addresses follow the same public-only rule as Okta, and the token goes only to `admin.googleapis.com`.
- **Privacy.** Only app-level totals are stored: the app's name, client ID and type, how many distinct people authorized it in the window, how many authorizations, when the last one was, the scopes granted and the high-risk ones. No event, email, profile ID or IP address is stored, logged or kept past the run. Distinct people are counted in memory with HMAC-SHA256 under a key made for the run and discarded with it, and the window is read again every run, so no per-person state is kept.
- **Window and scope.** `days` (30 by default, 1 to 180), `min_users` (1 by default), and name exclusions. An app nobody authorized in the window is out of scope ("no authorization in the reporting window"), never "no longer seen", since its grants may still be live. High-risk scopes: full mail (`https://mail.google.com/`, `gmail.*` except `gmail.labels`), full Drive (`drive`, `drive.readonly`), `admin.*` and `cloud-platform`. A dismissed candidate comes back when it gets a new high-risk scope.
- **Errors.** Token errors are `unauthorized_client` (the client isn't authorized under domain-wide delegation, or was authorized by email instead of the numeric client ID), `access_denied` (a scope isn't authorized) and `invalid_grant` (unknown user to act as, a deleted or wrong key, or clock skew); the advice names the client ID and the scope. Reads: 403 means the administrator lacks the Reports privilege, except Google's quota errors (below); a 401 signs in again once; 503 and 429 back off.
- **No empty listing guard.** `authorize` fires only when someone consents, so a window with no authorization at all is normal in a small Workspace. Such a run succeeds with the note "No authorization in the last N days: nothing was changed", and every observation keeps its state.

## Email via Microsoft Graph

Each organization can send email through Graph `sendMail` as the application, from one mailbox, with SMTP as the fallback. Both are set per organization in Administration › Email settings (`PATCH /api/v1/notification-settings/email`). The Graph client secret and the SMTP password are encrypted and write-only. An organization without settings, or with transport `installation_default`, uses the server's `SOFTWARDEN_SMTP_*`.

```
POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token     client credentials, scope https://graph.microsoft.com/.default
POST https://graph.microsoft.com/v1.0/users/{sender mailbox}/sendMail
     {"message": {"subject", "body": {"contentType": "HTML", "content"},
                  "toRecipients": [{"emailAddress": {"address"}}],
                  "internetMessageHeaders": [{"name": "x-softwarden-notification-id", "value": <delivery id>}]},
      "saveToSentItems": false}
     headers: client-request-id: <a GUID per attempt, stored on the delivery>, return-client-request-id: true
```

| API | Permission | Type | How it's granted |
|---|---|---|---|
| Microsoft Graph | `Mail.Send` | Application, through **Exchange RBAC for Applications** (role `Application Mail.Send`) | An Exchange management scope on the sender mailbox. **Don't** consent `Mail.Send` in Entra API permissions. |

References: [user: sendMail](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0) and [RBAC for Applications](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac).

- **Why not Entra.** Entra and Exchange grants add up, so an Entra `Mail.Send` consent lets the app send as anyone in the tenant, whatever the Exchange scope says. Remove any Entra `Mail.Send` application permission and its consent. RBAC for Applications replaces Application Access Policies.
- **Setup.** An Exchange admin with Organization Management, or an Entra Exchange Administrator, runs this in Exchange Online PowerShell:

```powershell
Connect-ExchangeOnline
# IDs from Entra > Enterprise applications (not App registrations)
New-ServicePrincipal -AppId <app client id> -ObjectId <enterprise app object id> -DisplayName "softwarden-mail"
New-ManagementScope -Name "softwarden sender" -RecipientRestrictionFilter "PrimarySmtpAddress -eq 'softwarden@contoso.com'"
New-ManagementRoleAssignment -App <enterprise app object id> -Role "Application Mail.Send" -CustomResourceScope "softwarden sender"
Test-ServicePrincipalAuthorization -Identity <enterprise app object id> -Resource softwarden@contoso.com    # InScope = True
Test-ServicePrincipalAuthorization -Identity <enterprise app object id> -Resource someone-else@contoso.com # InScope = False
```

- Exchange caches app permissions for 30 minutes to 2 hours, so a fresh grant can fail a test email for a while. A license-free shared mailbox (50 GB or less, with sign-in blocked) should work as the sender; this hasn't been checked in a real tenant yet.
- **What Softwarden checks.** *Test connection* only gets a token. Microsoft issues one even when no permission is granted, so it proves the tenant, client ID and secret, not the mailbox scope. *Send test email* sends a real message to the signed-in Admin's own address, and that is what proves the scope: a `403 ErrorAccessDenied` names the RBAC role and scope to check.
- **Answers.** Success is `202 Accepted` with an empty body: accepted, not yet delivered. Errors are `{"error": {"code", "message", "innerError" | "innererror"}}`, read ignoring case and matched on `error.code`. `429` and `503` carry `Retry-After`.
- **Limits and retries.** Graph allows 10,000 requests per 10 minutes and 4 concurrent requests per app and mailbox. Softwarden sends at most 4 at a time per sender mailbox across all workers, and honors `Retry-After` (capped at an hour), else backs off 1, 4 and 16 minutes, 4 attempts in all. **Graph has no idempotency header**, so Softwarden prevents duplicates itself: each delivery has a unique key written before the call, and it never retries after an ambiguous failure (a timeout or a dropped connection after the request was written, or a 5xx other than 503). Such a delivery is marked failed with the reason, and an Admin can check and resend it. Only `429` and `503` ("not accepted") are retried.

### SMTP fallback

For other SMTP servers (a relay, a hosted mail service). An organization's host may be a private relay, but loopback, link-local (cloud metadata) and unspecified addresses are refused, also after DNS resolution and inside IPv6 forms. The server's own `SOFTWARDEN_SMTP_HOST` isn't restricted. You set the host, port (default 587 for STARTTLS, 465 for implicit TLS, 25 for none), security, an optional user name and password, and the from address. No Microsoft permission.

**SMTP AUTH with a password against Exchange Online is being retired**: off by default for existing tenants from the end of December 2026, and removed in the second half of 2027 ([Exchange team blog](https://techcommunity.microsoft.com/blog/exchange/updated-exchange-online-smtp-auth-basic-authentication-deprecation-timeline/4489835)). For Microsoft 365, use Graph.

## Teams notifications

**No Graph permission and no Entra registration.** Softwarden posts Adaptive Cards to a Teams channel through a Workflows webhook ([incoming webhooks](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook), [connectors](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/connectors-using)).

- **Setup.** Office 365 Connectors were retired in May 2026, and Softwarden refuses their addresses (`*.webhook.office.com`, `outlook.office.com`). In the channel, choose `...` › Workflows › "Send webhook alerts to a channel" › Save, and copy the link. Add a co-owner to the workflow, because a workflow belongs to the person who made it. Paste the link into Administration › Teams destinations (`POST /api/v1/notification-settings/teams-destinations`).
- The link is a bearer secret. It is stored encrypted and write-only (only its host is shown), and is never logged, audited or put in a job argument.
- The payload:

```json
{"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": null,
  "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.2",
              "body": [...], "actions": [{"type": "Action.OpenUrl", ...}]}}]}
```

- Cards use any element except `Action.Submit`, and Markdown rather than HTML. The request body stays under **28 KB** (long lists end with "and N more"). More than 4 requests per second is throttled, so Softwarden spaces posts to one destination and honors `429` with `Retry-After`.
- Posts go to the address checked when the name was resolved (no second lookup), with a 30-second deadline. IPv6 forms that embed or translate IPv4 (NAT64, IPv4-compatible, SIIT, 6to4, Teredo) are refused.
- **Microsoft doesn't document these, so Softwarden assumes them** until checked against a real workflow: the URL host pattern (any `https` host is accepted; private, loopback and link-local addresses are refused, and checked again at send time after DNS resolution) and the success status (any 2xx counts as accepted).

## Generic webhooks

Not Microsoft; listed so that every outbound destination is in one place. A webhook destination is an https URL (encrypted, write-only, with the same address checks as Teams) and a signing secret (encrypted, write-only, 16 characters or more). Each POST carries `X-Softwarden-Delivery` (the delivery ID), `X-Softwarden-Event` and `X-Softwarden-Signature`. How to verify the signature, and the body's format, are in [the API guide](API.md#verifying-a-webhook).

## Connection test (built)

`POST /api/v1/connections/microsoft/{id}/test` and `POST /api/v1/connections/halo/{id}/test` (Admin) ask for a token with the stored values and nothing else. **The test needs no permission beyond the token, and proves none.** Microsoft issues a client-credentials token even when no API permission has admin consent, so a passing test proves the tenant, the application (client) ID and the client secret only. Each source's access check (below) proves its permissions.

```
POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
     grant_type=client_credentials, client_id, client_secret, scope=https://graph.microsoft.com/.default
POST {halo base_url}/auth/token
     grant_type=client_credentials, client_id, client_secret, scope (default "read:assets")
```

Global Azure only: national clouds and certificate credentials aren't offered.

A refused Microsoft token request is classified by the AADSTS code at the start of `error_description`. Softwarden stores and shows its own wording plus the code; Microsoft's message is never kept, shown or logged.

| Code | Problem | Meaning |
|---|---|---|
| AADSTS7000215 | wrong credentials | Invalid client secret (often the Secret ID pasted instead of its Value) |
| AADSTS7000222 | wrong credentials | The client secret has expired |
| AADSTS700016 | wrong credentials | No application with this client ID in the tenant |
| AADSTS90002 | wrong credentials | Tenant not found |
| AADSTS900023 | wrong credentials | The tenant identifier isn't valid |
| AADSTS7000112 | disabled | The application is disabled in the tenant |
| AADSTS65001 | missing permission | Consent missing: no administrator has consented to the app |
| AADSTS7000229 | missing permission | Consent missing: the app has no service principal in the tenant |
| other `invalid_client` / `unauthorized_client` | wrong credentials | A general refusal of the app's credentials |
| 429, 5xx | couldn't connect | The sign-in service is busy; try later |

Microsoft's list of codes is at https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes. Halo errors are classified by HTTP status and the OAuth `error` (`invalid_client`, `invalid_scope`).

When a connection's sign-in values or secret change, it gets a new revision, and no connector runs on it until it passes the test again.

## Access checks and guided setup

Access checks use the permissions above and nothing more. `POST /api/v1/connectors/{id}/access-check` (Admin, audited as `connector.access_checked` with the permissions missing) signs in with the connector's connection (no passing connection test needed), then makes one one-item read per permission, outside any transaction, and stores the result on the connector (`access_check`).

- A sign-in failure is classified by AADSTS code, as in the connection test, and skips the reads.
- A refused read (`403`, Graph's `Authorization_RequestDenied`, "Insufficient privileges to complete the operation.") is "missing permission", naming the permission. A `401` is an authentication problem (after one fresh sign-in).
- An optional permission that isn't granted is `optional_missing`. Any other failure of an optional read (404, 429, 5xx) is `warning`. Neither fails the check.
- The agent identity read is `not_used` (not called) while the connector's scope leaves `include_agent_identities` off.
- The source's capability is checked first (403).
- The Connection step names the connection only, since the result is readable by every role.
- For `azure`, `powerplatform` and `halo` there is one sign-in step per audience, then the reads below. At most 10 subscriptions or environments are probed; the rest are `not_checked`, and a run reports each.
- Throttling waits at most a minute, and a check takes three minutes at most.

| Source | Calls (and the permission each proves) |
|---|---|
| `entra` | `GET /v1.0/applications?$top=1&$select=id` and `GET /v1.0/servicePrincipals?$top=1&$select=id` (`Application.Read.All`); `GET /v1.0/users?$top=1&$select=id` (`User.ReadBasic.All`, optional); `GET /v1.0/servicePrincipals/microsoft.graph.agentIdentity?$top=1&$select=id` (`AgentIdentity.Read.All`, optional) |
| `entra_licenses` | `GET /v1.0/subscribedSkus?$select=skuId` (`LicenseAssignment.Read.All`). Microsoft doesn't document the exact `error.code` for a refusal here (`Authorization_RequestDenied` or `Forbidden` expected), so any 401 or 403 counts. |
| `entra_shadow_it` | `GET /v1.0/organization?$select=id`, `GET /v1.0/servicePrincipals?$top=1&$select=id` and `GET /v1.0/oauth2PermissionGrants` (`Directory.Read.All`); `GET /v1.0/auditLogs/signIns?$filter=createdDateTime ge {an hour ago} and createdDateTime le {now}&$top=1` (`AuditLog.Read.All`; `not_used` while the scope's `sign_ins` is off). A tenant without P1 or P2 (`403 Authentication_RequestFromNonPremiumTenantOrB2CTenant`) is a failed step with the problem `missing_licence` ("Missing license"), not a missing permission. |
| `entra_coverage` | `GET /v1.0/servicePrincipals?$top=1&$select=id` (`Application.Read.All`); `GET /v1.0/servicePrincipals/{the first one}/synchronization/jobs` (`Synchronization.Read.All`; a 404 passes, since that app just has none; `not_used` while `provisioning` is off); `GET /v1.0/identity/conditionalAccess/policies` (`Policy.Read.All`; `not_used` while `conditional_access` is off). A tenant without P1 is a **warning** with the problem `missing_licence`, not a failure: runs still report SSO and provisioning. |
| `azure` | Token for Azure Resource Manager; `GET /subscriptions/{id}?api-version=2022-12-01` per chosen subscription, else `GET /subscriptions?api-version=2022-12-01` (none listed: "no role on any"); then, per subscription, the reads the scope uses: Logic workflows (`$top=1`), web apps, Automation accounts, and the first account's runbooks (Reader). A `403 AuthorizationFailed` names the action and scope Azure gave, and the fix (Reader on that subscription). A subscription that isn't Enabled, Warned or PastDue is a warning. |
| `powerplatform` | With All environments: a token for the admin API, then its environments list (the registration, required then). Per environment: a token for that environment, `GET {url}/api/data/v9.2/bots?$top=1&$select=botid` (`prvReadbot`), and with cloud flows `GET {url}/api/data/v9.2/workflows?$top=1&$select=workflowid&$filter=category eq 5` (`prvReadWorkflow`, optional; `not_used` while flows are off). 401, 403 and the privilege errors are "needs an application user with read on ...", naming the privilege when Dataverse does. |
| `halo` | Halo token, then `GET /api/Asset?paginate=true&page_size=1&page_no=1` (`read:assets`) |
| `okta` | Okta token (a refused scope names `okta.apps.read`), then `GET /api/v1/apps?limit=1&includeNonDeleted=true` and `GET /api/v1/apps/{the first one}/users?limit=1` (Read-only Administrator; `not_used` while `assignment_counts` is off) |
| `google_workspace` | Google token acting as the administrator (`unauthorized_client` names the Reports scope), then `GET .../applications/token?eventName=authorize&startTime={a day ago}&endTime={now}&maxResults=1` (the Reports admin privilege) |

**The guided setup script** is shown, never run by Softwarden: an administrator runs it in Azure Cloud Shell. It registers a single-tenant app, adds only the Graph application permissions of the sources you chose, grants admin consent, assigns Reader on the listed subscriptions (Azure only) and creates a one-year secret. Power Platform application users are added by hand.

## AI assistant: Anthropic (off by default)

Capability `assistant`. This isn't a Microsoft permission, but it is data leaving Softwarden for Anthropic, so it is listed here.

An Admin turns it on with an Anthropic API key (encrypted, write-only). Without one, the installation's `ANTHROPIC_API_KEY` is offered, only when `SOFTWARDEN_ASSISTANT_INSTALLATION_CAP_USD` caps each organization's spend on it. Calls go through the official `anthropic` Python SDK to the Messages API at `api.anthropic.com`:

- `POST /v1/messages/count_tokens`, to reserve the worst-case cost against the monthly cap before each call;
- `POST /v1/messages`, with structured JSON output, effort low, and the model `SOFTWARDEN_ASSISTANT_MODEL` (default `claude-sonnet-5-5`).

The base URL is fixed (`https://api.anthropic.com`), so `ANTHROPIC_BASE_URL` and SDK profiles are ignored. The key travels in the SDK's `x-api-key` header only. The SDK's and its HTTP clients' loggers (`anthropic`, `httpx2`, `httpcore2`, `httpx`, `httpcore`) are pinned to WARNING, because at DEBUG they log the whole request.

| Sent per candidate (at most 5 per call, 50 per batch) | Never sent |
|---|---|
| Its source name (200 characters), observation kind (application, component, ai_agent) and source type (entra, azure, powerplatform, halo) | Any ID: observation, record, connector, tenant, client, application, object, resource or external IDs (records offered are named by a short handle such as `c1-r2`) |
| Description (300 characters), except an Entra description taken from a service principal's `notes`; publisher (300 characters), except a `*.onmicrosoft.com` tenant domain; homepage as scheme, host and path only (no query or fragment; http and https only); agent type, component type and enabled flag; each only when the source reported it | Entra service principal `notes`, homepage query strings and fragments, `*.onmicrosoft.com` publisher domains, owners (names, emails, object IDs), credentials (key IDs, names, hints, expiry), sign-in audience, tags, connection settings and secrets |
| Up to 3 existing records Softwarden suggests for it: name, kind, name similarity | Contracts, costs, people, attachments, notes, custom fields, the audit log, anything of another organization |

The key in use and the installation's configured secrets are also removed from the text sent, as a second line of defense. Stored: per call the model, the four token counts, the cost, the outcome and the candidate count; per candidate the suggestion. Never the prompt, the response text, an API error message or the key. The system prompt is the same for every call and comes first, with the candidate data last. It is shorter than the model's cacheable minimum (1,024 tokens), so it carries no `cache_control` marker and nothing is cached today; the marker is added automatically if the prompt grows past that.

## End-of-life data (endoflife.date)

Capability `eol`, every edition. Not Microsoft, and no credential. Once a day the worker reads release cycles and end-of-life dates from the public endoflife.date API (`https://endoflife.date/api/v1`: `GET /products`, then `GET /products/{product}` for each product an organization tracks). It sends only product names in the URL and a `Softwarden/<version>` User-Agent, never organization data. Set `SOFTWARDEN_EOL_ENABLED=0` to turn it off and load the data by hand instead ([operations guide](04-development/operations.md#end-of-life-data-endoflifedate)).
