# Editions and capabilities

Every organization has an edition, `community` or `cloud`. This page says what is included, how the app and the API tell an organization that it lacks a feature, and how an operator sets an organization's edition. It also covers the features that ship as previews, and the limits each edition sets.

## What Community includes

The community edition is everything in this release:

- **The catalog:** applications, components, AI agents, integrations, people, teams and vendors, with relationships, dependencies and change assessments, record reviews and staleness, custom fields, attachments, and the data-quality rules and score.
- **Money and dates:** contracts, contract lines and license pools, renewals, the daily digest and Needs attention, and end-of-life tracking of on-premises software versions (from endoflife.date).
- **Discovery from Microsoft Entra ID:** enterprise apps and app registrations, license seat counts, shadow IT from sign-in logs and consent grants, SSO and MFA coverage, and license waste.
- **Discovery from Azure, Power Platform and HaloITSM:** Logic Apps, Function Apps, App Services and Automation runbooks; Copilot Studio agents and cloud flows; HaloITSM assets.
- **Discovery from Okta and Google Workspace**, as preview features (see [Preview features](#preview-features)).
- **The AI assistant**, which suggests how to handle discovery candidates (off until an Admin turns it on).
- **Risk and governance:** the vendor risk file, the data-flow map, offboarding and access review, the in-app audit log with its CSV export, the audit log as JSON lines for a SIEM, and the compliance profile (extra validation and review fields, off for each organization until an Admin turns it on).
- **Working with other tools:** CSV import and export, API tokens, signed event webhooks, catalog as code (the catalog as YAML files in git), ticket links to Jira, single sign-on with OpenID Connect (Entra ID, Okta, Google Workspace or any OIDC provider), and an export of all the organization's data.

## Planned, not in this release

SAML 2.0 single sign-on and SCIM 2.0 provisioning of users and groups are planned for a later Enterprise edition. They aren't part of this release, and no organization has them.

## Other editions

The cloud edition is for a hosted, multi-organization service: public signup creates organizations in it, and it adds what such a service needs (self-service deletion, per-organization nightly backups and limits). The edition field also accepts `enterprise`, a value reserved for a planned Enterprise edition. In this release an `enterprise` organization has exactly the community features, plus per-organization nightly backups (`org.tenant_backups`), and the community limits. You don't need to use it. The full mapping is in [the capability reference](#capability-reference) below.

## How editions work

Each edition maps to a set of capability names, such as `contracts` or `assistant`. Every feature checks a capability name, never the edition. An organization's capabilities are its edition's set, plus or minus any per-organization overrides (below).

- `GET /api/v1/auth/me` returns `edition` and `capabilities` (sorted) for the current organization. The web app shows or hides features by `capabilities`.
- A request for a feature the organization doesn't have gets 403:

  ```json
  {
    "type": "/problems/capability-required",
    "title": "This feature isn't enabled for this organization.",
    "status": 403,
    "detail": "This needs the billing capability, which this organization's edition doesn't include.",
    "capability": "billing"
  }
  ```

  The same answer comes when platform staff turned the feature off for the organization (below). The role is checked first, so a Viewer refused for their role gets the role message.

## Capability reference

The last column says how each feature is closed off in the API when an organization doesn't have it.

| Capability | Community | Cloud | Gate in the API today |
|---|:-:|:-:|---|
| `catalog` (applications, components, integrations, people, teams, vendors) | yes | yes | none (always on) |
| `relationships` (relationships, dependencies, change assessments) | yes | yes | none |
| `reviews` (record reviews, staleness) | yes | yes | none |
| `renewals` | yes | yes | none |
| `notifications.digest` (daily digest, Needs attention snoozes) | yes | yes | none |
| `discovery.entra` | yes | yes | `POST /connectors` with `source=entra`, `entra_licenses`, `entra_shadow_it`, `entra_coverage` or `entra_licence_usage` (that one also needs `preview`, below); `GET /discovery/shadow-it`, `GET /reports/sso-coverage`, `GET /reports/licence-waste` and their `export.csv`, the license waste drill-down `GET /reports/licence-waste/{sku_id}` (Admin) and its CSV, `GET /license-pools/{id}/licence-waste` and `GET /contracts/{id}/licence-waste` (`policy(capability=discovery.entra)`; the last two also need `contracts`, and license waste is priced only with `contracts`); the application detail's `sso_coverage`; the data-quality rules `sso_missing`, `mfa_missing`, `scim_missing`; the offboarding footprint's licenses section. License waste reads the same Entra tenant as the other Entra sources, so it is in every edition. |
| `discovery.azure` (Logic Apps, Function Apps, App Services, Automation runbooks) | yes | yes | `POST /connectors` with `source=azure`, a run request, every run and every access check |
| `discovery.powerplatform` (Copilot Studio agents and cloud flows) | yes | yes | `POST /connectors` with `source=powerplatform`, a run request, every run and every access check |
| `discovery.halo` (HaloITSM assets) | yes | yes | every `/connections/halo` route (Admin); `POST /connectors` with `source=halo`, a run request, every run and every access check; a Halo ticket connection |
| `imports` (CSV import and export) | yes | yes | none |
| `api.tokens` | yes | yes | none |
| `audit.log` (in-app audit log, its CSV export) | yes | yes | `GET /audit-events/export.csv` (`required_capability`; Admin) |
| `sso.oidc` (OpenID Connect single sign-on: Entra ID, Okta, Google Workspace, any OIDC provider) | yes | yes | every `/sso/settings...` route (`policy(capability=sso.oidc)`); without it `auth/options` offers no SSO and starting a sign-in is refused |
| `ai_agents` (AI agents, AI providers and models, the models-in-use report) | yes | yes | every `/ai-agents`, `/ai-providers` and `/ai-models` route (`required_capability`), so an edition without it gets the capability problem; every edition has it today. Global search leaves agents out without it (403 when `kinds` names `ai_agent`). |
| `custom_fields` (custom field definitions and values) | yes | yes | every `/custom-fields` route (`required_capability`); values on records go through the records' own routes; global search ignores custom field values without it |
| `attachments` (files and links on records and vendors) | yes | yes | every `/<records>/{id}/attachments`, `/vendors/{id}/attachments` and `/attachments/{id}` route (`required_capability`); an AI agent's also need `ai_agents`; global search leaves attachments out without it (403 when `kinds` names `attachment`) |
| `data_quality` (data-quality rules, exceptions, the completeness score and the Data quality queue) | yes | yes | every `/data-quality/...` route (`required_capability`); without it no rule is on, so records' `quality` lists no failing check and Needs attention leaves the Data quality queue empty. Rules inside it are gated by their own capability: the AI agent checks by `ai_agents`, the compliance checks by `compliance_profile` and the profile being on. |
| `contracts` (contracts, contract lines and license pools) | yes | yes | every `/contracts...` and `/license-pools...` route (`required_capability`), contract attachments and their audit routes. Without it: global search leaves contracts out (403 when `kinds` names `contract`), a record's Map leaves contracts and license pools out, attachments on contracts are left out of search, the data-quality check "Not on a contract" is unavailable, a record's History leaves out its contract-line events, custom fields can't be newly applied to `contract` or `license_pool` (fields that already apply keep them), and contract and license pool date fields are no reminder source. |
| `webhooks` (signed event webhooks: endpoints, subscriptions, the delivery log) | yes | yes | every `/webhooks...` and `/webhook-deliveries...` route (`policy(capability=webhooks)`); without it no event is queued. Webhook destinations named by notification rules (`/notification-settings/webhook-destinations`) aren't gated. |
| `vendor_risk` (the vendor risk file: profile, risk documents, their expiry reminders, the risk overview and its gaps) | yes | yes | every `/vendors/{id}/risk...`, `/vendor-risk-documents...` and `/vendors/risk-overview...` route (`required_capability` / `policy(capability=vendor_risk)`); without it the vendor detail's `risk` is null and there are no `vendor_risk` reminders, Vendor risk queue rows or vendor data-quality checks |
| `org.export` (export all of the organization's data as a zip) | yes | yes | `POST /organization/exports` (Admin). Listing and downloading go by each export's purpose: an Admin's own exports need `org.export`; the final export before deletion needs `org.export` or `org.self_delete` (the deletion banner links it); nightly backups need `org.export` or `org.tenant_backups`; an export the operator made at the organization's request (`operator`) is always listed and downloadable by its Admins. Any other is a 404. |
| `eol` (end-of-life tracking of on-premises software versions through endoflife.date) | yes | yes | every `/end-of-life...` route and each record's `/<records>/{id}/end-of-life` and instance `.../end-of-life` route (`policy(capability=eol)`); without it a record's `end_of_life` is null, Needs attention's End of life queue is empty and the data-quality check "End-of-life cycle unknown" is unavailable |
| `catalog_code` (catalog as code: the catalog as YAML files in git) | yes | yes | `GET /catalog-code/export.zip`, `POST /catalog-code/plan` and `POST /catalog-code/apply` (`policy(capability=catalog_code)`, Admin) and the `catalog_export` and `catalog_apply` commands. It writes only through the same services and limits as the app. Inside it, each kind keeps its own gate: AI agents, providers and models need `ai_agents`, custom field definitions and values `custom_fields`, relationships and their types `relationships`, attachment metadata `attachments`, and the compliance fields the profile being on. A file of a kind the organization lacks is that file's error, and the export leaves it out. |
| `data_flows` (the data-flow map: where classified data goes, risky flows, its graph and CSV) | yes | yes | `GET /reports/data-flows`, `/reports/data-flows/graph` and `/reports/data-flows/export.csv` (`policy(capability=data_flows)`, any role); without it Needs attention's Data flows queue is empty and integrations aren't checked by data quality (the rules "Data flow not classified" and "No data categories" are unavailable). An integration's `data_categories` are edited on the integration in every edition, like its classification. |
| `ticket_links` (Jira, ServiceNow and Halo ticket links on assessments and renewals, their status refresh and opt-in ticket creation) | yes | yes | every `/assessments/{id}/ticket-links...`, `/renewals/{id}/ticket-links...`, `/ticket-links/parse` and `/ticket-connections...` route (`policy(capability=ticket_links)`); without it the assessment and renewal details' `ticket_links` are empty and the hourly refresh reads nothing. A ServiceNow or Halo ticket connection also needs `preview` (a preview feature), and a Halo one `discovery.halo`, because it signs in through the Halo connection. Links to ServiceNow and Halo tickets can be pasted, parsed and kept with a status set by hand either way. |
| `preview` (the installation's preview features are on) | yes | yes | Only while the installation sets `SOFTWARDEN_PREVIEW_FEATURES` (below): without the setting no organization has it, whatever an override says. ServiceNow and Halo ticket connections need it beside `ticket_links`. Without it, `GET /ticket-connections` lists a kept ServiceNow or Halo connection with `available: false` and `DELETE` still removes it, while every other `/ticket-connections...` route on one, creating one, a link refresh and create-ticket for those systems are 403 naming `preview`; `GET /ticket-links/systems` lists Jira only, and the hourly refresh skips them. The web app reads it to hide the preview features rather than present them as another edition's. |
| `assistant` (Claude suggests link, create or dismiss for discovery candidates) | yes | yes | every `/assistant/...` route (`policy(capability=assistant)`) and `POST /discovery/observations/{id}/assistant/accept` and `/reject`; without it candidates' `assistant_suggestion` is null, no batch is queued after a run, and a queued batch fails with `capability` |
| `discovery.okta` (Okta applications; a preview feature) | yes | yes | every `/connections/okta` route (`policy(capability=discovery.okta)`, Admin), `POST /connectors` with `source=okta`, a run request, every run and every access check |
| `discovery.google` (third-party apps from the Google Workspace OAuth token audit log; a preview feature) | yes | yes | every `/connections/google_workspace` route (`policy(capability=discovery.google)`, Admin), `POST /connectors` with `source=google_workspace`, a run request, every run and every access check. Each discovery source has its own capability, so Google Workspace has its own name rather than riding on `discovery.okta`. |
| `audit.export` (the audit log as JSON lines for a SIEM, with snapshots) | yes | yes | `GET /audit-events/export.jsonl` (`required_capability`; Admin) |
| `compliance_profile` | yes | yes | `PATCH /organization` with `compliance_profile_enabled` (Admin); while it is off, record responses omit the fields, and writes or filters naming them get a field error |
| `billing` |  | yes | not built |
| `org.self_delete` (an Admin deletes their own organization after an export and a grace period) |  | yes | `POST /organization/deletion` (checked after the role check). Reading the state and canceling aren't gated, so a scheduled deletion can always be canceled. Self-hosted operators remove an organization themselves. |
| `org.tenant_backups` (a nightly export of the organization, kept for `SOFTWARDEN_TENANT_BACKUP_RETENTION` nights; the `cloud` edition; also an organization whose edition is set to the reserved value `enterprise`) |  | yes | the nightly `org.tenant_backups` schedule queues backups only for organizations with it; backups are listed and downloaded with the exports, with this capability or `org.export` |

Community lists only features that exist. SAML single sign-on (`sso.saml`) and SCIM provisioning (`scim`) are planned for a later Enterprise edition: no organization has them in this release, and their routes aren't served. Audit events are kept forever in every edition: configurable retention of audit events and notification logs isn't built yet, so no edition includes it.

## Setting the edition

Only an operator sets an organization's edition. No API endpoint for organization members writes it:

```bash
docker compose exec app python manage.py set_edition --list                 # slug, edition, name
docker compose exec app python manage.py set_edition <org-slug> cloud        # audited as organization.edition_changed
```

On a development checkout, run the same commands in `apps/api` as `uv run python manage.py ...`, with `SOFTWARDEN_DEBUG=1` set ([DEVELOPMENT.md](DEVELOPMENT.md#run-it-locally)).

On a hosted installation, platform staff can do the same from the platform admin console (`POST /api/v1/platform/organizations/{id}/edition`, with a reason; audited in the organization and in the platform stream).

## Per-organization overrides (feature flags)

Platform staff can grant or revoke one capability for one organization, with a reason and an optional expiry (`POST /api/v1/platform/organizations/{id}/capability-overrides`; see the platform admin runbook, `docs/04-development/platform-admin-runbook.md`). The organization's capabilities are then the edition's set, **plus** live grants, **minus** live revokes, and every gate, `auth/me` included, follows. The name must be one in the reference above. Overrides are read once per request or job, so a change applies from the next request, well within a minute. Overrides change capability names only; nothing compares editions.

## Preview features

Version 1.0.0 ships some features as previews, hidden unless the installation turns them on: Okta and Google Workspace discovery, and ServiceNow and Halo ticket connections. One installation setting turns them on: `SOFTWARDEN_PREVIEW_FEATURES` ([environment reference](ENV.md); off by default; set it in the Compose `.env`, or in Helm's `extraEnv`).

- **While the setting is off,** no organization has `preview`, `discovery.okta` or `discovery.google`, whatever its edition or a platform override says. With the setting on, they follow the edition and overrides as usual (every edition has Okta and Google), and a platform revoke of `preview` hides them for one organization.
- **Every gate follows:** `auth/me`, the routes (`403 /problems/capability-required`, whose detail says it is a preview feature this installation hasn't turned on), the schedules and the web app. The app hides the Okta and Google sources, setup cards and connection tabs, and the ServiceNow and Halo ticket connections, rather than presenting them as another edition's.
- **Preview sources:** the Microsoft Entra license usage source (`entra_licence_usage`) keeps its capability, `discovery.entra`, but is offered only as a preview until the license waste report has a screen. Without `preview`, `POST /connectors` with that source is the same 403 naming `preview`, and `GET /discovery/sources` leaves it out. A license usage connector made while `preview` was on keeps `available: true` and keeps running, since its own capability is unchanged.
- **Nothing is deleted** when the setting is off.
  - Okta and Google connectors stay in `GET /connectors` with `available: false` (as any connector whose source the organization lost). They can be turned off or deleted, never changed, run, checked or scheduled. Their runs and observations stay. `doctor` reports them as "Unavailable (preview feature off)", the Discovery summary marks them `available: false`, and `GET /discovery/sources` leaves the Okta and Google sources out. Okta and Google connections stay in the database, out of reach of the API.
  - ServiceNow and Halo ticket connections are listed with `available: false` (the admin page says "Kept from a preview install") and can only be deleted. That deletion is audited with `kept: true`, so a kept Halo connection never blocks deleting its Halo connection. ServiceNow and Halo links stay, and new ones can be pasted, with a status set by hand.
  - Turning the setting on gives the capabilities back, and everything works again, unchanged.

## Limits

Beside what an organization may use, each edition caps how much it may create (an organization set to `enterprise` has the community limits): `records`, `other_records` (people, vendors, contracts and the other secondary rows, together), `connectors`, `members` (active), `attachment_bytes`, and its API rates `api_rate_read` and `api_rate_write` (requests per minute for the organization as a whole). The defaults:

|  Limit | Community | Cloud |
|---|:-:|:-:|
|  `records` | unlimited | 5,000 |
|  `other_records` | unlimited | 50,000 |
|  `connectors` | unlimited | 10 |
|  `members` | unlimited | 50 |
|  `attachment_bytes` | unlimited | 5 GB |
|  `api_rate_read` | unlimited | 1,200/min |
|  `api_rate_write` | unlimited | 300/min |
|  `invitation_emails_hour` | unlimited | 20 (5 until a second member) |
|  `invitation_emails_day` | unlimited | 100 (20 until a second member) |

The two invitation email budgets count the invitation emails (new and resent) an organization sent through the installation's own mail server in the last hour and the last 24 hours. An organization with its own mail server (Graph or SMTP in Email settings) isn't counted on a single-organization installation; with more than one organization, invitations go through the installation's server whenever it can send, and they count. Email settings refuse the installation's own SMTP host. Until a second member is active, the cloud values are capped at 5 and 20, unless platform staff set an override of that limit. Past a budget, `POST /invitations` and `POST /invitations/{id}/resend` answer `409 /problems/limit-reached`, and nothing is written or sent.

- Platform staff can override one organization's value, with a reason and an optional expiry (`POST /api/v1/platform/organizations/{id}/limits`; `value: null` for unlimited; an API rate is at least 1).
- A create past a limit is refused with `409 /problems/limit-reached`, naming the limit, its maximum and the usage. Reactivating a deactivated member counts as adding one. Other updates and deletes never are refused, and lowering a limit removes nothing.
- Members read their limits and usage at `GET /api/v1/organization/limits`. [The API guide](API.md) ("Limits") lists every create that is checked.
- The operator's break-glass recovery (`manage.py reset_admin`) can reactivate an account past the members limit. It removes nothing, and new member creates stay refused until usage is back under the limit.

There is no license key verification yet. Nothing sets a license key in this release (`PATCH /organization` ignores `licence_key`), so an operator chooses an edition with `manage.py set_edition` alone.

## Configuring the assistant

The assistant suggests linking, creating or dismissing undecided discovery candidates, with a confidence and a reason. Editors decide (Accept, Reject, or any decision as usual), and nothing is ever applied on its own.

1. An Admin opens the assistant settings (Administration › Assistant, or `GET/PATCH /api/v1/assistant/settings`, If-Match supported), enters an Anthropic API key (`api_key`, write-only; or leaves it empty to use the installation's key, when the installation offers one), sets the monthly cap (`monthly_cap_usd`, default 10.00 USD) and turns it on (`enabled: true`).
2. Every successful discovery run that sees new or changed objects queues a batch for its connector. An Editor can ask for one with `POST /api/v1/assistant/batches` (`{"connector": id}`, or all). `GET /api/v1/assistant/batches` shows what each batch did and why one stopped (`stop_reason: cap_reached` is "cap reached").
3. `GET /api/v1/assistant/usage?month=YYYY-MM` (Admin) lists every API call with its four token counts and cost, and the month's total against the cap.

Installation settings ([environment reference](ENV.md)):

- `SOFTWARDEN_ASSISTANT_MODEL` (default `claude-sonnet-5-5`);
- `SOFTWARDEN_ASSISTANT_PRICES`: JSON, USD per million tokens per model (`input`, `output`, optional `cache_read` and `cache_write`). A model with no price is refused;
- `ANTHROPIC_API_KEY`: the installation's key, for organizations without their own;
- `SOFTWARDEN_ASSISTANT_INSTALLATION_CAP_USD`: the most each organization may spend on the installation's key per month.

The installation key is used only when that ceiling is set. An organization on it is held to the lower of its own cap and the ceiling (`month.cap_usd`; `installation_key_available` and `installation_cap_usd` in the settings). What is sent to Anthropic, and what never is: [Microsoft permissions, "AI assistant: Anthropic"](PERMISSIONS.md#ai-assistant-anthropic-off-by-default).
