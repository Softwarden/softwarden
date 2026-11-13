# Catalog as code

Softwarden can write its catalog as a tree of YAML files, one per record, and read such a tree back. Keep the tree in
a git repository and catalog changes become pull requests: someone edits a file, a reviewer reads the diff, CI shows
what applying it would change, and merging applies it. The app stays the system of record: every change still goes
through the same validation, limits and audit log as an edit on screen.

Catalog as code is in every edition (capability `catalog_code`) and is for Admins only.

In the app, **Admin › Catalog as code** (`/admin/catalog-code`) downloads the export, and plans and applies an
uploaded zip, or one or more YAML files chosen on their own (the browser puts them in a zip first; leave prune off
then). The plan lists every change with its fields before and after, the errors and the warnings; apply sends the
plan's hash, so a catalog changed in between is refused and planned again.

## The tree

```
catalog.yaml                      the manifest: which kinds the tree holds, whether it has People
applications/crm.yaml
components/nightly-sync.yaml
ai-agents/helpdesk-bot.yaml
integrations/crm-to-payroll.yaml
relationships/component-nightly-sync--depends-on--application-crm--production.yaml
relationship-types/feeds_reports.yaml
vendors/contoso.yaml
teams/it-applications.yaml
people/dana-example-test.yaml     only with --include-people
departments/finance.yaml
categories/crm-and-sales.yaml
tags/customer.yaml
custom-fields/contract_ref.yaml
ai-providers/anthropic.yaml
ai-models/anthropic-claude-5-5.yaml
```

A file's name is only where it lives: what the file is about is in its content (its `kind` and natural key), so a
file can be renamed or moved, and a repository's own layout (a `catalog/` folder, GitHub's `repo-main/` in a zip)
doesn't matter. Files that aren't `.yaml` or `.yml`, and hidden folders such as `.git/`, are ignored.

## A file

```yaml
apiVersion: softwarden/v1
kind: Application
metadata:
  slug: crm
  name: CRM
  description: |-
    Customers.
    All of them.
  tags:
    - Customer
    - tier-1
spec:
  lifecycle: active
  criticality: high
  businessOwner: dana@example.test
  technicalOwner: priya@example.test
  supportTeam: IT Applications
  department: Finance
  vendor: Contoso
  category: CRM and sales
  version: '10.2'
  ssoEnabled: true
  entraAppId: aaaaaaaa-0000-0000-0000-000000000001
  instances:
    - name: Production
      environment: prod
      url: https://crm.example
      status: operational
    - name: Sandbox
  customFields:
    contract_ref: C-42
    tier: gold
status:
  attachments:
    - title: Runbook
      category: sop_runbook
      kind: link
      url: https://wiki.example/crm
```

- `apiVersion` is `softwarden/v1`. `kind` is one of the kinds below.
- `metadata` holds the record's natural key and its labels; `spec` everything else. Field names are the API's in
  camelCase (`business_owner` is `businessOwner`); custom field values sit under `spec.customFields` by the field's
  `name`.
- `status` is read-only: the export writes attachment metadata there (title, category, kind, link or file name,
  content type, size, SHA-256; never the file), and applying ignores it.
- **The file is the whole record.** A field left out means its empty value (or default), so deleting a line clears the
  field. The export leaves out empty fields and defaults, so files stay short.
- **Stable.** Keys are always in the same order (the order in the tables below), lists are sorted (tags, members,
  models, instances, attachments), nothing volatile is written (no `updated_at`, ETag or revision), multi-line text is
  a literal block, and lines are never folded. The same records always give the same bytes, and the export zip has
  fixed timestamps, so an unchanged catalog exports to an unchanged tree and a commit holds only real changes.

### Natural keys and references

Records are matched by natural key, never by name (imports never match by name either):

| Kind | Folder | Key (`metadata`) |
|---|---|---|
| Application, Component, AIAgent, Integration | `applications`, `components`, `ai-agents`, `integrations` | `slug` (lower-case letters, digits, `-` and `_`; unique per kind; kept when the record is created) |
| Relationship | `relationships` | its `spec.type`, `source`, `sourceInstance`, `target`, `targetInstance` together |
| RelationshipType | `relationship-types` | `slug` |
| CustomField | `custom-fields` | `name` |
| Vendor, Team, Department, Category, Tag, AIProvider | `vendors`, `teams`, `departments`, `categories`, `tags`, `ai-providers` | `name` (ignoring case) |
| AIModel | `ai-models` | `provider`, `name` and `version` |
| Person | `people` | `email`; a person without one by `id`, their Softwarden ID |

A reference names the other record the same way:

- a record: `<kind>:<slug>` with kind `application`, `component`, `ai_agent` or `integration` (`application:crm`); an
  instance of it by name in the field beside it (`sourceInstance: Production`);
- a person: their email, or their Softwarden ID. Without `--include-people` the export names people by Softwarden ID
  only, so the tree holds no personal data; that ID is only known in the organization that exported it;
- a team, department, vendor, category or AI provider: its name; an AI model: `{provider, name, version}`;
- a relationship type: its slug.

References resolve only inside the organization being applied to, and to records in the same tree: another
organization's slug, name or ID is simply not found.

A Person file without an email, keyed by a Softwarden ID the organization doesn't have (the tree came from another
one), is matched by the person's exact name, and the person is created only when nobody has that name; the plan warns
which happened. For the rest of that apply, the other files' references to that ID point at the matched or new person,
so applying the same tree twice converges instead of creating people again. The rules that keep a name from merging
two people:

- **A name match is reference-only.** It links the tree's references (owners, members, account managers) to the
  existing person and never changes that person's fields: a difference in title, department or active is a plan
  warning ("Not changed (matched by name only)"), not an update. A Person file updates a person only when it matches
  by email or by a Softwarden ID the organization knows. A person matched by name only is never edited from the
  tree; to manage them from the tree, add their email (or re-export so the file carries their id).
- Only people who were there before the apply can be matched by name, never one an earlier file of the same tree just
  created.
- Two such files (no email, unknown ID) with the same name are both errors, as is a name that several people here
  share, or a namesake here who has an email: add `metadata.email` to say who is meant.
- Two files that resolve to the same person (say one by ID and one by name) are an error: "This is the same Person as
  …". The same holds for every kind.

A Person file can't remove or change the email of the person its ID names: when that person has one, the file must
give the same. `metadata.id` must be a UUID.

### What is exported

| Kind | metadata | spec |
|---|---|---|
| Application | slug, name, description, tags | lifecycle, criticality, businessOwner, technicalOwner, supportTeam, department, costCenter, dataClassification, vendor, category, purpose, builtBy, licenseModel, deploymentModel, version, primaryUrl, adminUrl, documentationUrl, supportUrl, authenticationMethod, ssoEnabled, scimProvisioning, entraAppId, servicePrincipalId, instances (name, environment, url, status), the compliance fields, customFields, archived |
| Component | slug, name, description, tags | lifecycle, criticality, the owners, supportTeam, department, costCenter, dataClassification, componentType, purpose, builtBy, licenseModel, hosting, environment, hostRuntime, scheduleOrTrigger, repositoryUrl, documentationUrl, runbookUrl, azureResourceId, ppEnvironmentId, externalId, the compliance fields, customFields, archived |
| AIAgent | slug, name, description, tags | lifecycle, criticality, the owners, supportTeam, department, costCenter, dataClassification, purpose, platform, modelsUsed, environment, ppEnvironmentId, channels, knowledgeSources, toolsConnectors, authMode, runAsIdentity, dataAccessScope, humanInLoop, actionsCanWrite, riskClassification, repoUrl, externalId, the compliance fields, customFields, archived |
| Integration | slug, name, description | lifecycle, source, sourceInstance, target, targetInstance, technicalOwner, supportTeam, dataClassification, dataCategories (a list in a fixed order), dataPurpose, protocol, schedule, direction, authMethod, documentationUrl, monitoringUrl, customFields, archived |
| Relationship | | type, source, sourceInstance, target, targetInstance, purpose |
| RelationshipType | slug | name, inverseName, description, isDependency |
| CustomField | name | label, type, description, choices, required, default, appliesTo, weight, isReminderSource |
| Vendor | name | website, supportUrl, portalUrl, supportEmail, supportPhone, accountManager, notes, customFields |
| Team | name | description, members |
| Department | name | |
| Category | name | description |
| Tag | name | color, description |
| AIProvider | name | website |
| AIModel | provider, name, version | modality, hostingRegion |
| Person (with `--include-people`) | email or id, name | title, department, active |

The compliance fields (isGxp, validationStatus, validationDocUrl, lastPeriodicReview, nextPeriodicReview, riskTier)
are written, and read, only while the organization's compliance profile is on. Kinds behind a capability the
organization lacks are left out: AI agents, providers and models (`ai_agents`), custom fields (`custom_fields`),
relationships and their types (`relationships`), attachment metadata (`attachments`).

**Never exported:** secrets, connector and connection credentials, API and calendar tokens, user accounts and
memberships, a record's IT steward (an account), attachment files, reviews and confirmations (evidence, recorded in the
app), assessments, contracts and license pools, renewals, discovery observations, audit events. `catalog.yaml` says
whether the tree holds People:

```yaml
apiVersion: softwarden/v1
kind: Catalog
spec:
  kinds:
    - Department
    - Category
    - ...
  includesPeople: false
```

## Plan and apply

A **plan** reads the tree, matches each file to its record and compares it field by field. Each change is then tried
through the same service functions the app uses and rolled back, so the plan's errors are exactly the ones apply would
meet; it writes nothing. It returns:

- `changes`: each create, update and delete with its file, kind, key and the fields that change (`path`, `before`,
  `after`, as the file writes them; an instance is one line, `spec.instances[Production]`);
- `errors` and `warnings`: each with the file, the 1-based line, the field path and a message, such as
  `applications/crm.yaml:12 spec.lifecycle: Choose a lifecycle status.`;
- `summary` (create, update, delete, unchanged, errors, files) and `plan_hash`.

**Apply** does the same in one transaction and commits only when no file has an error: each record through its service
function (validation, organization scope, limits, and an audit event marked `via: catalog_code`), plus one
`catalog_code.applied` event with the counts. It must be given the `plan_hash` of the plan that was reviewed, a SHA-256
of the organization's catalog as it was, the files and the prune flag: if anyone edited the catalog since, or the files
differ, apply refuses (`409 /problems/catalog-code-stale`) and nothing is written. Plan again and review.

Before writing anything, a plan or apply locks the existing rows the tree may write: the people its Person files name,
the records and vendors its files name, and with prune every record and vendor of the kinds being pruned. A bulk edit,
bulk delete or reassignment running at the same time waits for the apply, or the apply waits for it; they never block
each other for good. The catalog is read again once the rows are held, and `plan_hash` is taken from that read.

- Records are created with their file's slug. Creating records counts against the organization's records limit for the
  whole tree at once (`409 /problems/limit-reached`, as an import's commit).
- Retiring a record that active records depend on needs an acknowledgment, which a file can't give: that file is an
  error ("Active records depend on this one..."), as in an import. Retire it on its page.
- A file's instances are created or updated by name. An instance the file leaves out is kept (a warning) unless the
  apply prunes.
- A Tag file matches its tag as records' tags do: spaces collapsed and case ignored, so `Foo  Bar` is the existing
  `Foo Bar`. A tag is created, and audited, only when there is none.
- A value the database refuses (too long for its column, out of range) is that file's error.
- Applying never writes discovery observations or reconciliation decisions.
- **Large first loads: use the CLI.** One API request (plan or apply) creates at most 2,000 rows, so it ends well
  inside the server's 120 s request timeout; a tree that would create more is a `400 /problems/invalid-request`
  (`errors.file`) and nothing is written. `manage.py catalog_apply` has no such cap. Updates aren't capped.

### Prune

Without prune nothing is deleted. With prune, records of the tree's kinds that have no file are deleted, through the
same delete services (so a record that reviews, assessments or contracts still reference can't be deleted: that is an
error, and nothing is applied; archive it instead). The kinds are `catalog.yaml`'s `kinds`, or, without a manifest, the
kinds the tree has files for. People and tags are never deleted (anonymize a person in the app; tags have no delete),
nor the three core relationship types. Links go before the records they join, instances before their application. A
custom field's delete line shows how many records and vendors lose their value for it (`values`: before, after 0).

**The prune cap.** A prune that deletes more than 10 records in all, more than 20% of a kind's records (when that is 3
or more), or every record of a kind that has some, is an error unless you allow it, so nothing is applied. A partial checkout, a sparse clone or a truncated
zip (in the worst case only `catalog.yaml`) would otherwise plan to delete every record it is missing. The error names
the count per kind ("This tree with prune deletes 39 records (Application 12 of 12, ...)") and carries `mass_delete`,
the number. When the deletes are meant, plan and apply again with `allow_mass_delete=true` (API) or
`--allow-mass-delete` (CLI); the applied audit event then records `allow_mass_delete`. The flag doesn't change the
plan's `plan_hash`.

## Limits and safety

- **YAML** is parsed safely: no Python objects, no tags other than the plain scalars, no anchors or aliases (so no
  alias expansion bombs), no merge keys, no duplicate keys, one document per file, at most 12 levels of nesting,
  20,000 values and 512 KB per file, and 400,000 values in the whole tree. A malformed number or boolean (`!!int abc`,
  `!!bool maybe`, a number over 30 characters), NaN and infinity are that file's error. A value that looks like a number
  but is meant as text must be quoted (`version: '10.2'`); the export quotes them.
- **The zip** (API) is refused before anything is parsed when it is over 16 MB, lists more than 25,000 entries, holds
  more than 20,000 YAML files or more than 16 MB of them uncompressed (counted as they inflate, an oversize file's bytes
  included, never from the sizes the zip claims), has an absolute path, a drive letter, a `..` part, a symbolic link, an
  encrypted entry, two paths that differ only in case, or nests deeper than 8 folders. The same rules apply to a
  directory given to the CLI, which also never follows a link; `catalog_export` refuses to write when a kind folder or
  a file it would write is a symbolic link, before writing anything.
- **The hash** names the organization too, so a plan made for one organization never applies to another.
- **Who:** Admins. The export is audited (`catalog_code.exported`) and refused while platform staff impersonate; plan
  and apply are allowed for support work, and every event they write records the impersonation.

## The API and the CLI

```bash
# Export (people only when asked for)
curl -H "Authorization: Bearer sw_ADMIN_TOKEN" -o catalog.zip "https://softwarden.example/api/v1/catalog-code/export.zip"
# Plan, then apply what was planned
curl -H "Authorization: Bearer sw_ADMIN_TOKEN" -F file=@catalog.zip -F prune=false https://softwarden.example/api/v1/catalog-code/plan
curl -H "Authorization: Bearer sw_ADMIN_TOKEN" -F file=@catalog.zip -F prune=false -F plan_hash=<plan_hash> https://softwarden.example/api/v1/catalog-code/apply
```

```bash
# From a development checkout (docs/DEVELOPMENT.md, Run it locally). On the Compose stack, run the same commands as
# `docker compose exec app python manage.py ...` with a --dir inside the container, and copy it out with docker compose cp.
cd apps/api
export SOFTWARDEN_DEBUG=1
uv run python manage.py catalog_export --org acme --dir ../../../acme-catalog [--include-people]
uv run python manage.py catalog_apply --org acme --dir ../../../acme-catalog --dry-run [--prune] [--json]
uv run python manage.py catalog_apply --org acme --dir ../../../acme-catalog [--prune] --expect-hash <plan_hash>
uv run python manage.py catalog_apply --org acme --dir ../../../acme-catalog [--prune] --no-plan-check
# a prune past the cap (more than 10 records, 20% of a kind, or all of a kind) also needs --allow-mass-delete
```

`catalog_export` removes YAML files left in the kind folders by an earlier export, so a deleted record disappears from
the tree; it leaves anything else (a README, `.git`) alone. Applying with `catalog_apply` needs `--expect-hash` (the
`plan_hash` of the dry run you reviewed), or `--no-plan-check` to say on purpose that there is no reviewed plan (it then
plans and applies in one go). It exits non-zero when the plan has errors or the hash doesn't match. Both commands are
audited with `interface: cli` and no actor.

## A git workflow

1. **Start the repository** from the app's current state:

   ```bash
   git init acme-catalog
   manage.py catalog_export --org acme --dir acme-catalog
   cd acme-catalog && git add -A && git commit -m "Catalog as exported"
   ```

2. **Change by pull request.** Edit `applications/crm.yaml` (say `lifecycle: deprecated`), or add
   `applications/new-tool.yaml`, on a branch, and open a pull request. The diff is the change.

3. **CI plans it** on the pull request and posts the plan for the reviewer, and keeps the hash:

   ```bash
   manage.py catalog_apply --org acme --dir . --dry-run --json > plan.json
   jq -r .plan_hash plan.json > plan-hash.txt
   ```

   A plan with errors fails the check, and each error names its file and line.

4. **Merging applies exactly what was reviewed:**

   ```bash
   manage.py catalog_apply --org acme --dir . --expect-hash "$(cat plan-hash.txt)"
   ```

   If someone edited the catalog in the app after the plan, the hash no longer matches and nothing is applied: plan
   again, review, merge.

5. **Pull the app's changes back** regularly (edits made on screen, records discovery created): run `catalog_export`
   into the repository and commit what changed. Because the export is stable, the commit holds only those changes.

Use `--prune` once the repository is the place records are removed: then deleting a file deletes the record.
