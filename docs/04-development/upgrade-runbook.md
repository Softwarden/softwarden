# Upgrade Softwarden

This runbook takes an installation from one release to the next. Sign-in is unavailable while the app is stopped and the migrations run, usually a minute or two.

**The rules:**

- Take a fresh backup before every upgrade.
- Pin the image tag in `.env`; never run `latest`.
- Migrations only go forward. **Rolling back means restoring the backup taken before the upgrade**, never running migrations backwards.

## Before you start (5 min)

- Read the release notes for anything that needs a setting changed.
- Pick a quiet time.
- Have the off-host backup location and your copy of `.env` at hand.

## Steps

### 1. Write the manifest (1 min)

The manifest records what must survive the upgrade:

- every record review, every completed renewal cycle and the audit log so far, each as a count and a checksum;
- every record, contract and membership by its ID, with its organization (and a record's kind, a membership's user; not its role, which may change).

The rows are compared one by one in step 5. One added after the manifest is fine (the app keeps running until step 4), but one missing, or now in another organization, fails the check. So a record, contract or membership someone deletes between this step and the upgrade also shows as missing: write the manifest right before the backup in step 2, and ask people not to delete anything until the upgrade is done.

The manifest also records the latest applied migration of each Softwarden app (`"migrations": {"api": "0001_initial", "catalog": "0019_split_apps", "core": "0001_initial", ...}`).

```bash
cd ~/softwarden
docker compose exec app python manage.py upgrade_check --write-manifest /tmp/before.json
docker compose cp app:/tmp/before.json ./upgrade-$(date +%F)-before.json
```

### 2. Back up (5 min)

Take an encrypted backup set with the worker ([the backup runbook](backup-runbook.md) restores it), and keep a copy of `.env`:

```bash
docker compose exec worker python manage.py backup_now --inline
cp .env softwarden-$(date +%F).env
```

Copy both off the host before going on. Without them there is no way back.

### 3. Pin and fetch the new release (2 min)

Change one line in `.env`, then fetch that image **before** step 3a, so the pre-check runs the new code.

With the published release images:

```bash
# .env
SOFTWARDEN_IMAGE=ghcr.io/softwarden/softwarden:<new tag>
```

```bash
docker compose pull --ignore-buildable
```

From source, check out the release and build it under a local tag:

```bash
git fetch --tags && git checkout <new tag>
# .env: SOFTWARDEN_IMAGE=softwarden:<new tag>
docker compose build
```

Never use `latest`: the tag in `.env` is the record of what is running.

**The other images are pinned too.** Each release pins its database and proxy images, so the same release always runs the same ones: `compose.yaml` names `postgres:17.11-trixie` and `caddy:2.11.9`, and both Softwarden images build on `python:3.13.18-slim-bookworm` (the development stack's Mailpit is `v1.21.8`). Each is named by its tag followed by the digest the tag resolved to (`postgres:17.11-trixie@sha256:…`), so even a re-pushed tag can't change what runs: Docker uses the digest, and the tag is there for people to read. The PostgreSQL image stays on Debian trixie, because changing the C library under an existing data directory can change text sorting and invalidate indexes. A new release may move these pins forward. A PostgreSQL minor version (17.11 to 17.12) needs no action. A move to another Debian base or PostgreSQL major version is called out in the release notes with its own steps.

### 3a. Pre-check the data (1 min)

Run the new image's pre-check **before** starting it. `run` replaces the image's command, so nothing migrates:

```bash
docker compose run --rm --no-deps app python manage.py upgrade_check --precheck
```

It ends with "Ready to migrate.", or lists rows that would stop a pending migration and exits 1. If it lists rows, fix them as in [Cross-organization references](#cross-organization-references) before step 4.

### 4. Start it (3 min)

```bash
docker compose stop app worker           # nothing serves the old code while the schema changes
docker compose up -d --wait              # migrate runs and exits 0, then app, worker and proxy start
docker compose ps -a                     # migrate "Exited (0)", app "healthy", worker "healthy"
curl -fsS https://<PUBLIC_HOSTNAME>/readyz   # {"status": "ok", ... "migrations": "ok"}; 503 for up to a minute until the worker ticks (add -k with Caddy's local CA)
```

The one-shot `migrate` service runs the new migrations, and the app starts only after it exits 0 (if it doesn't, see `docker compose logs migrate`). The old app and worker are stopped first, because `up -d` would otherwise keep them running on the old code while the schema changes under them. Some migrations take longer on a large installation: see [Long-running migrations](#long-running-migrations).

### 5. Check it (2 min)

```bash
docker compose cp ./upgrade-$(date +%F)-before.json app:/tmp/before.json
docker compose exec app python manage.py upgrade_check --manifest /tmp/before.json
```

Every line should start with ✓ (or OK on a console that can't show the check mark, such as a Windows console on cp1252; a failure is ✗ or FAIL), ending with "All upgrade checks passed."

It checks, as the first Admin and through the real web stack: the sign-in page, Needs attention, a record with its reviews, a renewal with completion evidence, the worker, that the reviews, renewal completions and audit events in the manifest are unchanged, and that every record, contract and membership in it is still there, in its organization. "Database migrated" lists the latest migration of every app. "Migrations at or past the manifest" confirms that no app is behind what the manifest recorded. An app behind it means the database is older than the manifest (the wrong backup was restored), and the check fails.

Then run `docker compose exec app python manage.py doctor`. It should show no `FAIL`, and the worker, schedules and backup should be `OK`. Sign in yourself and open Needs attention.

Keep a note of each upgrade: the date, the versions from and to, the backup used, and the result of the checks.

## Roll back

If step 4 or 5 fails and the fix isn't obvious:

1. Put the previous `SOFTWARDEN_IMAGE` back in `.env`.
2. Restore the step 2 backup with `ops/restore.sh --yes <set>.dump.enc` ([backup runbook](backup-runbook.md), "Restore"). A backup taken by a release before 1.0 has no integrity tag, so its restore also needs `--accept-untagged` (only for such a set: every set from 1.0 on has a tag).
3. Run `docker compose up -d`.
4. Check that nothing was lost, with the manifest from step 1, as in step 5:

   ```bash
   docker compose cp ./upgrade-$(date +%F)-before.json app:/tmp/before.json
   docker compose exec app python manage.py upgrade_check --manifest /tmp/before.json
   ```

   Every line should start with ✓, ending with "All upgrade checks passed."

Work done between the backup and the rollback is lost; tell the people who use Softwarden what to enter again.

## Long-running migrations

A few migrations rewrite whole tables and block reads and writes while they run. The known one is catalog `0031_search_index` (global search). It adds a stored generated `search_vector` column to 11 tables (`catalog_resource`, `catalog_application`, `catalog_component`, `catalog_aiagent`, `catalog_integration`, `catalog_vendor`, `catalog_tag`, `catalog_aiprovider`, `catalog_aimodel`, `catalog_person`, `catalog_attachment`). Adding a stored generated column rewrites each table under an `ACCESS EXCLUSIVE` lock, and the migration then builds its GIN and trigram indexes without `CONCURRENTLY`. While it runs, every read and write of those tables waits, so the app is effectively unavailable. With a few thousand records it takes seconds. On a large installation, apply it in a maintenance window, and let step 4 finish before you announce the upgrade done. It runs in one transaction, so an interrupted run changes nothing.

## Cross-organization references

The migrations catalog `0021_composite_keys` and imports `0002_importrow_tenancy` add composite foreign keys: each reference must point at a row of the same organization. The application has always checked this, so a normal installation has no such rows. A database edited by hand or by a script may have some, and then the migration stops with an `IntegrityError` naming the constraint. Each migration runs in a single transaction, so nothing has changed, but the app won't start until the rows are fixed. `upgrade_check --precheck` (step 3a) runs these same queries while the migrations are pending; plain `upgrade_check` also runs them as long as either migration is unapplied.

Run a query with `docker compose exec -T db psql -U softwarden softwarden -c "<query>"`. Each returns the IDs of the offending rows:

| Constraint | Reference | Find the rows |
|---|---|---|
| `catalog_person_org_department_fk` | Person.department | `SELECT p.id FROM catalog_person p JOIN catalog_department d ON d.id = p.department_id WHERE d.organization_id <> p.organization_id;` |
| `catalog_resource_org_business_owner_fk` | Resource.business_owner | `SELECT r.id FROM catalog_resource r JOIN catalog_person t ON t.id = r.business_owner_id WHERE t.organization_id <> r.organization_id;` |
| `catalog_resource_org_technical_owner_fk` | Resource.technical_owner | `SELECT r.id FROM catalog_resource r JOIN catalog_person t ON t.id = r.technical_owner_id WHERE t.organization_id <> r.organization_id;` |
| `catalog_resource_org_support_team_fk` | Resource.support_team | `SELECT r.id FROM catalog_resource r JOIN catalog_team t ON t.id = r.support_team_id WHERE t.organization_id <> r.organization_id;` |
| `catalog_resource_org_created_by_import_fk` | Resource.created_by_import | `SELECT r.id FROM catalog_resource r JOIN catalog_importrun t ON t.id = r.created_by_import_id WHERE t.organization_id <> r.organization_id;` |
| `catalog_application_org_vendor_fk` | Application.vendor | `SELECT a.resource_id FROM catalog_application a JOIN catalog_resource r ON r.id = a.resource_id JOIN catalog_vendor v ON v.id = a.vendor_id WHERE v.organization_id <> r.organization_id;` |
| `catalog_recordreview_org_resource_fk` | RecordReview.resource | `SELECT x.id FROM catalog_recordreview x JOIN catalog_resource t ON t.id = x.resource_id WHERE t.organization_id <> x.organization_id;` |
| `catalog_recordreview_org_confirmed_by_fk` | RecordReview.confirmed_by | `SELECT x.id FROM catalog_recordreview x JOIN catalog_person t ON t.id = x.confirmed_by_id WHERE t.organization_id <> x.organization_id;` |
| `catalog_importrow_org_matched_resource_fk` | ImportRow.matched_resource | `SELECT x.id FROM catalog_importrow x JOIN catalog_importrun run ON run.id = x.run_id JOIN catalog_resource t ON t.id = x.matched_resource_id WHERE t.organization_id <> run.organization_id;` |
| `catalog_importrow_org_possible_match_fk` | ImportRow.possible_match | as above with `possible_match_id` |
| `catalog_importrow_org_result_resource_fk` | ImportRow.result_resource | as above with `result_resource_id` |

The `organization` columns that catalog `0020` and imports `0002` add are copied from the parent row (the Resource, the ImportRun), and the run and resource keys of those rows are built from the same value, so those constraints can't fail.

To fix a row, point it at the right organization's row, or clear the reference where the field allows it (owners, team, department, vendor, `created_by_import` and the three ImportRow resources are nullable). A RecordReview pointing at another organization's record or Person was written wrongly. Keep the backup from step 2, and note what you changed. Then run step 3a again.

## Restore drill

Restore a backup to a fresh stack before you load real data, then every quarter: see [the backup runbook](backup-runbook.md#restore-drill-before-real-data-then-quarterly).
