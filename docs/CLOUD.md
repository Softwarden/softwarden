# Hosted (cloud) deployment

This guide runs Softwarden as a hosted, multi-organization service: the same image as every other install, behind a managed PostgreSQL 17, with the web app and the worker scaled separately, and `SOFTWARDEN_DEPLOYMENT=cloud`. Customers sign up, create their own organizations (edition `cloud`), invite colleagues, connect Entra and get reminders without the operator doing anything.

It uses the [Helm chart](../deploy/helm/softwarden/README.md) with [`values-cloud.yaml`](../deploy/helm/softwarden/values-cloud.yaml). For a single team's own server, use [INSTALL.md](INSTALL.md) instead. Every setting is in [ENV.md](ENV.md). The [platform admin runbook](04-development/platform-admin-runbook.md) covers running the service once it is up.

**Status:** written November 8, 2026. CI renders and lints the chart with these values. They haven't been installed on a cluster yet.

## Not supported for production in 1.0

**Cloud mode is not supported for production in Softwarden 1.0.** Use it to evaluate the hosted service, with test organizations and test addresses only. Don't open signup to the public or put customers' data in it. For real use, run 1.0 self-hosted, one installation per organization ([INSTALL.md](INSTALL.md)). The gaps below also apply to a self-hosted installation shared by organizations that don't trust each other.

Cloud mode assumes the organizations on one installation don't trust each other, and 1.0 doesn't hold up to that yet (taking an email address and sending spam through invitations are fixed):

- **Platform staff:** the console needs a TOTP code as well as the password in cloud mode (`SOFTWARDEN_PLATFORM_TOTP`; enrollment at the first console sign-in, again at every impersonation; platform admin runbook, "Second factor"), and failed sign-ins are limited per account across every pod. There is no WebAuthn or hardware key yet, and one staff account still reaches every organization.

If you run cloud mode at all, put the platform console behind your own access proxy or VPN: let only your staff reach `/platform` and `/api/v1/platform/` (the console, and impersonation through it), and refuse those paths to everyone else at the ingress.

## Not built yet: billing and a transactional email provider

- **Billing (Stripe) is not built.** No subscription, trial or dunning exists. An organization's capabilities and limits change only through its edition and the platform console's overrides: `POST platform/organizations/{id}/edition`, capability overrides and limit overrides (platform admin runbook). Signup always creates edition `cloud` organizations.
- **There is no transactional email provider integration.** All installation email (signup confirmations, invitations, digests and reminders for organizations without their own sender) goes through SMTP, the `SOFTWARDEN_SMTP_*` settings. Most transactional providers offer an SMTP endpoint, and that works.

## Prerequisites

- **Kubernetes 1.25 or later and Helm 3.** CI uses Helm 3.19.
- **A managed PostgreSQL 17 that requires TLS**, such as Azure Database for PostgreSQL flexible server, Amazon RDS or Cloud SQL. Create an empty database `softwarden` owned by a role `softwarden`. The role needs no superuser rights. Have the server's CA bundle ready for `verify-full`.
- **An SMTP relay** with a user name and password. Signup refuses to start without email, because it confirms every address.
- **An ingress controller with TLS**, for example ingress-nginx with cert-manager. TLS ends there. The app sends its own security headers, and HSTS while the chart's ingress has TLS; don't add a CSP on the ingress (chart README, "Security headers").
- **A ReadWriteMany storage class**, such as Azure Files, Amazon EFS, Filestore or NFS, for two volumes:
  - **media**: attachment files, tenant backups and exports, stored unencrypted by Softwarden: use a class that encrypts at rest and limit who can mount or snapshot it (SECURITY.md, "The media volume");
  - **backups**: the installation's nightly sets.

  With two or more app or worker pods, every pod mounts them, and the chart refuses to render with ReadWriteOnce. **Object storage for media is not built.** An RWX volume is required.
- **Optional:** the Kubernetes metrics server, for the app's HorizontalPodAutoscaler.

## What cloud mode changes

`SOFTWARDEN_DEPLOYMENT=cloud` (Helm `deployment: cloud`) changes installation defaults. An explicit variable always wins. It never sets an organization's edition: editions are `core/capabilities.py`'s alone.

| | Self-hosted (default) | Cloud |
|---|---|---|
| Public signup (`SOFTWARDEN_SIGNUP_ENABLED`) | off | on |
| An organization's SMTP relay or Halo URL on a private network (`SOFTWARDEN_ALLOW_PRIVATE_SERVICE_HOSTS`) | allowed | refused |
| An organization's SMTP port other than 25, 465, 587 or 2525 (`SOFTWARDEN_SMTP_ANY_PORT`) | allowed | refused |
| Database connections kept (`SOFTWARDEN_DB_CONN_MAX_AGE`) | 60 s, checked before reuse (0 under `SOFTWARDEN_DEBUG`) | 60 s, checked before reuse |
| First-run setup | `/setup` with a one-time token, or `bootstrap_admin --create`: the first organization and its Admin | no web setup and no token. `bootstrap_admin --create` makes the operator's **platform staff** account, in no organization, while no staff account exists |
| Platform staff second factor (`SOFTWARDEN_PLATFORM_TOTP`) | off | on: a TOTP code as well as the password for the console and for impersonation |
| `manage.py doctor` | installation checks | plus the cloud checks below |
| Helm | anything goes | refuses generated keys, a database password in values, `sslmode` weaker than `require`, no SMTP host, and an SMTP user without a password Secret |

Helm is the supported way to run cloud mode as a hosted service. To evaluate it on one host, Compose works too: put `SOFTWARDEN_DEPLOYMENT=cloud` in `.env` (Compose passes it to the app and the worker; the default is `selfhosted`) and run `docker compose up -d --build`, then `docker compose exec -it app python manage.py bootstrap_admin --create`. Signup needs email, so set `SOFTWARDEN_SMTP_HOST` as well (or use the dev stack's Mailpit). Compose has none of the Helm chart's cloud guards (generated keys, a bundled database without TLS), so `doctor` reports the hosted checks it fails; don't serve customers from it.

Why setup creates platform staff: on a hosted service the operator's first account must not be a tenant. Under the old behavior it would have been a community-edition organization owned by the operator, created through a public page. Customers' organizations come from signup.

## Secrets

Create every Secret before installing. The chart reads them by name and never holds a value. Generate the keys on a trusted machine, store `credential_keys` and `backup_key` in your vault, then delete the local files.

```bash
kubectl create namespace softwarden
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > django_secret_key
python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())' > credential_keys
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > backup_key
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ops_token
kubectl -n softwarden create secret generic softwarden-keys \
  --from-file=django_secret_key --from-file=credential_keys --from-file=backup_key --from-file=ops_token
kubectl -n softwarden create secret generic softwarden-database --from-literal=password='<the role password>'
kubectl -n softwarden create secret generic softwarden-smtp --from-literal=smtp-password='<the SMTP password>'
kubectl -n softwarden create secret generic softwarden-pg-root-ca --from-file=root.crt=<the server CA bundle>
```

| Secret | Keys | Read as |
|---|---|---|
| `softwarden-keys` (`secrets.existingSecret`) | `django_secret_key`, `credential_keys` (Fernet keys, one per line, newest first), `backup_key`, `ops_token` | files in `/data/secrets`. With an existing Secret the chart sets `SOFTWARDEN_CREDENTIAL_KEYS_FILE` and `SOFTWARDEN_BACKUP_KEY_FILE` to them, so the keys count as provided, not generated. |
| `softwarden-database` (`database.existingSecret`) | `password` | `POSTGRES_PASSWORD` |
| `softwarden-smtp` (`smtp.existingSecret`) | `smtp-password` | `SOFTWARDEN_SMTP_PASSWORD` |
| `softwarden-pg-root-ca` (`extraVolumes`) | `root.crt` | mounted at `/etc/softwarden/pg`, read through `POSTGRES_SSLROOTCERT` |

Without `credential_keys`, no secret stored in Softwarden can be read: client secrets, SMTP passwords, webhook URLs. Without `backup_key`, no installation backup can be restored. Neither key is in any backup. To rotate the credential key, put the new key first and keep the old one after it. Then run `manage.py rotate_credential_keys`, which re-encrypts every stored secret under the new key.

## Install

Copy `values-cloud.yaml` and replace every placeholder:

- the image owner and tag;
- `publicHostname`;
- the database host;
- the SMTP host and From address;
- the storage classes;
- the issuer.

Signup is on by default in cloud mode, so a visitor could sign up before you create the platform staff account. That doesn't block you (first run, step 1), but to keep the service closed until you are ready, install with signup off, adding this to `extraEnv`, and remove it after the first run:

```yaml
extraEnv:
  - name: SOFTWARDEN_SIGNUP_ENABLED
    value: "0"
```

Then:

```bash
helm upgrade --install softwarden deploy/helm/softwarden -n softwarden -f my-values-cloud.yaml
kubectl -n softwarden rollout status deploy/softwarden-app
```

The migration Job runs first (see [Migrations](#migrations)), then the app and worker Deployments start.

## First run

1. **Create the operator's platform staff account.** It belongs to no organization. You type the email, display name and password at the prompts:

   ```bash
   kubectl -n softwarden exec -it deploy/softwarden-app -- python manage.py bootstrap_admin --create
   ```

   For an unattended run (a Kubernetes Job, a script), add `--no-input`. The password comes only from `BOOTSTRAP_ADMIN_PASSWORD` in that command's environment. The email and display name come from `--email ops@example.com --name "Ops"`, or, when those options are left out, from `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_NAME`; an option wins over its variable. A missing or invalid value stops the command with nothing created, naming the option or variable to set. The grant is written to the platform audit stream (`staff.granted`, `via: bootstrap`).

   It works while no platform staff account exists, even if customers have already signed up. The email must not belong to an existing account (use `platform_staff grant` for that). Once a staff account exists, `--create` is refused. Two runs at the same time create one account.
2. **Add more staff.** Each person needs an active account first, for example by signing up or being invited. Then run:

   ```bash
   kubectl -n softwarden exec deploy/softwarden-app -- python manage.py platform_staff grant colleague@example.com
   ```

   The same command makes an existing account staff when you switch an installation that already has accounts to cloud mode.
3. **Check the installation.** Nothing secret is printed.

   ```bash
   kubectl -n softwarden exec deploy/softwarden-app -- python manage.py doctor
   ```

4. **Sign in** at `https://<publicHostname>` with the staff account to use the platform console.
5. **Sign up as a customer**, in another browser, at `/signup` to check the whole path.

### The cloud checks in `doctor`

In cloud mode `doctor` adds these checks to the installation ones. The worker's heartbeat is the `worker` check every installation runs: it fails until a worker has ticked.

| Check | Fails (or warns) when |
|---|---|
| `cloud: database TLS` | the connection's `sslmode` is weaker than `require` (with no mode set, a `system` root certificate counts as `verify-full`, as in libpq 16+), or this session isn't encrypted (`pg_stat_ssl`; behind a session-mode pooler that is the pooler-to-server leg) |
| `cloud: email` | the installation can't send email (signup would refuse) |
| `cloud: signup` | warns: signup is off |
| `cloud: credential keys` | the key is generated into a volume or is the development key, instead of coming from `SOFTWARDEN_CREDENTIAL_KEYS(_FILE)` |
| `cloud: base URL` | `SOFTWARDEN_BASE_URL` isn't a public `https://` address |
| `cloud: platform staff` | no active platform staff account exists |
| `cloud: private service hosts` | warns: organizations may point their SMTP relay or Halo URL at private addresses |
| `cloud: rate limits` | warns: no trusted proxy is listed (every request then has the ingress's address, so per-address limits are shared by every client), or the sign-in attempts still counted (the last ten minutes or so) all came from one client address, which means a load balancer in front of the trusted proxy is being taken for the client |
| `cloud: platform second factor` | `SOFTWARDEN_PLATFORM_TOTP` is off (fails), or an active staff account hasn't set up its second factor yet (warns: whoever signs in first with its password sets it up) |

## Scaling

**The web app.** Set `app.replicas`, or turn on `app.autoscaling` (an HPA on CPU, 2 to 6 replicas at 70 % by default), and `app.gunicornWorkers` per pod. The PodDisruptionBudget keeps `minAvailable` pods through node drains. Rate limits are counted in the database, so a limit holds across every process and pod (docs/API.md "Rate limits"); each counted request costs one single-row upsert.

**The worker** is its own Deployment, with its own `worker.replicas`, resources, `worker.longJobs` and affinity. Two replicas give failover. More only help when the job queue is long. Several workers are safe:

- **Jobs** are claimed with `SELECT ... FOR UPDATE SKIP LOCKED` and run under a lease token (`core/jobs.py`). A worker that lost its lease records nothing, and an expired lease is marked abandoned and retried. `tests/test_jobs.py::test_concurrent_workers_run_each_job_exactly_once` runs three workers on one queue.
- **Schedules** fire through one conditional `UPDATE` that moves `next_due` and takes the schedule's lease in the same statement, so exactly one worker runs each fire. No advisory lock is used. `test_a_schedule_fires_once_under_two_workers` and `test_two_whole_ticks_at_once_fire_each_schedule_once` cover it.
- **The nightly installation backup** also takes a session advisory lock, so only one runs at a time.
- **Liveness is per pod.** Every worker writes the same database heartbeat row, which `/readyz` and `run_worker --check` read, so that row can't tell a stuck replica from a healthy one. Each worker process also keeps a file in its own `/tmp`, and the liveness probe, `run_worker --check --local`, fails once that file is 180 s old (it needs no database). The main loop touches the file at the start of each tick. The heartbeat thread touches it every 30 s after a beat that reached the database, but only while the main loop is between ticks for no longer than its interval plus 60 s (`--interval`, 60 s by default; a hang in the loop outside a tick counts as stuck too) or on one step for at most **65 minutes**. A step is the tick's housekeeping, one schedule run or one job. 65 minutes is a schedule run's one-hour ceiling (`SCHEDULE_CEILING`) plus one five-minute schedule lease, well above a job's 10-minute lease. So a long digest, purge or usage run keeps its pod. A main loop stuck on one step past 65 minutes stops refreshing the file, and the pod is restarted about three to five minutes later (180 s plus the probe's three 30 s failures). By then the jobs module already treats the step as stuck: its lease is no longer renewed, and another worker may run it again.

**Database connections.** Plan `max_connections` for the sum of:

- app pods × `app.gunicornWorkers` × `app.gunicornThreads` (`SOFTWARDEN_THREADS`): each gunicorn thread keeps its own connection, for 60 s (`SOFTWARDEN_DB_CONN_MAX_AGE`);
- worker pods × (2 + `longJobs`): the tick, the heartbeat thread and each long-job thread;
- the migration Job (1);
- `kubectl exec` sessions;
- headroom.

The chart's defaults (3 workers × 8 threads = 24 per app pod) with two pods of each come to about 48 + 8 = 56 connections. At the autoscaler's maximum of 6 app pods that is 144 + 8 = 152, over PostgreSQL's default `max_connections` of 100. Either raise `max_connections` (most managed servers size it by tier), lower `app.gunicornThreads` (4 threads halve it), cap `app.autoscaling.maxReplicas`, or put a **session-mode** pooler in front (below). A thread opens its connection only when it serves a request, so the sum is a ceiling under full load, not the idle count.

## Connection poolers

The app doesn't assume a pooler, and connects to PostgreSQL directly by default. If you put PgBouncer (or Azure's built-in pooler on port 6432) in front of the server, it must run in **session** mode, for the web app and the worker alike. Transaction pooling isn't supported.

Why session mode: the app and the worker take **session advisory locks** (`pg_try_advisory_lock`) and release them in a later statement:

- the installation backup's lock (`core/backup.py`), in the worker;
- the four Graph mail slots per sender mailbox (`notify/channels.py` `graph_slot`). They are taken wherever mail goes out through Microsoft Graph: in the worker, and in a web request too (an invitation email sent after its commit, or an Admin's test send).

Under transaction pooling, the lock and its unlock can run on different server connections. The slot would then stay taken until the pooler recycled that connection. `tests/test_cloud_deployment.py` lists these call sites, and fails when one is added, so this section is revisited.

With a session-mode pooler, set `database.jit: "on"` if it refuses the `options` startup parameter, and run `ALTER ROLE softwarden SET jit = off;` once. Count one server connection per client connection when sizing the pool (see the connection budget above).

Transaction-scoped advisory locks (`pg_advisory_xact_lock`: first-run setup, the per-organization limits, attachment quotas, reminder generation, backup requests) and row locks (`SELECT ... FOR UPDATE`) would be fine either way.

## Migrations

Migrations run in a Helm **pre-install / pre-upgrade hook Job** (`templates/migrate-job.yaml`), never in the app pods:

- **Why not an init container in the app pods:** every replica would run `migrate` at the same moment, and Django's `migrate` takes no lock. The hook Job runs once per release, before any new pod starts.
- **One pod at a time:** `parallelism: 1`, `completions: 1`, and `backoffLimit: 1`. A retry starts only after the failed pod has ended.
- **One release at a time:** Helm locks the release while an upgrade is pending, so two `helm upgrade` runs can't overlap.
- **A failed migration fails the release**, and the running pods stay on the previous version. Migrations are forward-only, so going back means restoring a backup.
- **Same connection:** the Job uses the app's database settings (the server, or a session-mode pooler).
- **Readiness:** the app's readiness probe (`/readyz/web`) checks that every migration is applied, so a pod with a newer image than its schema never takes traffic.

During a rolling upgrade, pods running the previous release serve against the new schema for the minute the rollout takes. Most migrations only add, which old code tolerates. When the changelog says a release's migrations aren't backward compatible, scale the app to zero before upgrading, or accept the errors during the rollout.

## Backups

Three layers, each covering what the others don't:

1. **Managed PostgreSQL point-in-time recovery**, which your provider runs. It restores the database to any second in its retention window. It doesn't cover the media volume, and it is tied to that provider and region. Turn it on.
2. **Installation backups**: the worker's nightly encrypted set (`SOFTWARDEN_BACKUP_CRON`, 02:30 UTC). It holds a `pg_dump` and a media archive that leaves out `exports/`, on the `backups` volume, kept 14 daily and 8 weekly. It is the way to recover the whole service, or to move it to another provider. Copy the sets off the cluster as well, for example with a CronJob or volume snapshots. See [Restoring](#restoring). Take one on demand:

   ```bash
   kubectl -n softwarden exec deploy/softwarden-worker -- python manage.py backup_now --inline
   ```

3. **Tenant backups**: a nightly zip per organization with `org.tenant_backups` (the cloud edition), on the media volume, with `SOFTWARDEN_TENANT_BACKUP_RETENTION` kept. Organizations' Admins download their own. Size the media volume for them (platform admin runbook, Tenant backups).

PITR doesn't replace installation backups. It has no media, and you can't restore it elsewhere. Installation backups don't replace PITR either: they lose up to a day.

**Deleted organizations in backups.** When an organization deletes itself, the purge removes its rows, files and tenant backups, and erases the accounts that belonged to it alone (email and username become `retired+<id>@invalid`, the name "Former member", the password unusable). A purged organization's data stays in the installation backups taken before the purge until those sets are pruned: with the default retention of 14 daily and 8 weekly sets, for up to eight weeks. Your provider's point-in-time recovery keeps it for its own retention window too. Tell customers this in your terms, and never restore an installation set for one organization over the live database (see the platform admin runbook). `GET /api/v1/organization/deletion` gives the deleting organization's Admins the number (`backups_kept_days`) for the deletion dialog.

### Restoring

**No scripted Kubernetes restore exists.** `ops/restore.sh` and its rehearsal mode ([backup runbook](04-development/backup-runbook.md)) are written for the Compose stack. On a hosted installation:

- **The database: use your provider's point-in-time recovery first.** Restore to a new server just before the loss, point `database.host` at it, and keep the media volume as it is.
- **From an installation set,** when PITR can't help (a wiped region, a move to another provider), by hand:
  1. Scale the app and worker to 0: `kubectl -n softwarden scale deploy/softwarden-app deploy/softwarden-worker --replicas=0`.
  2. On a trusted machine with the backup key, check the set (its integrity tag with `. ops/common.sh; BACKUP_KEY_FILE=backup_key check_sums_tag <set>.sha256`, then `sha256sum -c <set>.sha256`) and decrypt it, as `ops/common.sh` does: `openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass file:backup_key -in <set>.dump.enc -out <set>.dump`, and the same for `.media.tar.enc` and `.manifest.json.enc`.
  3. Create an **empty** database owned by the `softwarden` role, then load the dump with the TLS settings the app uses: `PGSSLMODE=verify-full PGSSLROOTCERT=root.crt pg_restore --no-owner --exit-on-error -h <host> -U softwarden -d softwarden <set>.dump`.
  4. Repopulate the media volume from `<set>.media.tar` (its top-level folder is `media/`), for example from a pod that mounts the claim. Files must belong to uid 10001.
  5. Run the migration Job, with one app pod for the checks, no worker, and no ingress, so nobody reaches the restored data and no schedule runs against it before the checks pass. `helm upgrade` sets `replicas` from its values, so pass them: `helm upgrade softwarden deploy/helm/softwarden -n softwarden -f my-values-cloud.yaml --set app.replicas=1 --set app.autoscaling.enabled=false --set worker.replicas=0 --set ingress.enabled=false`.
  6. Run the checks in that app pod. Its root filesystem is read-only, so copy the decrypted manifest into its `/tmp` first: `kubectl -n softwarden cp <set>.manifest.json <app pod>:/tmp/manifest.json`, then `kubectl -n softwarden exec <app pod> -- python manage.py upgrade_check --manifest /tmp/manifest.json` and `... python manage.py doctor`. `doctor`'s `worker` check fails here, as expected: step 5 scaled the worker to 0. It passes after step 7.
  7. Bring everything back with your normal values: `helm upgrade softwarden deploy/helm/softwarden -n softwarden -f my-values-cloud.yaml`.
- **The credential keys** must be the ones in use when the set was taken; they aren't in any backup (Secrets).

## Upgrades

1. Read the release's CHANGELOG entry and the [upgrade runbook](04-development/upgrade-runbook.md).
2. Take a backup: `kubectl -n softwarden exec deploy/softwarden-worker -- python manage.py backup_now --inline`.
3. Change `image.tag` in your values file and run `helm upgrade softwarden deploy/helm/softwarden -n softwarden -f my-values-cloud.yaml`.
4. Check that the migration Job succeeded, the rollout finished, and `manage.py doctor` is clean.

On SIGTERM the workers stop claiming and finish the job in hand (90 s grace). An interrupted long job is retried under its lease.
