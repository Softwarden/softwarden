# Softwarden Helm chart

Docker Compose is the primary way to install Softwarden ([docs/INSTALL.md](../../../docs/INSTALL.md)). This chart is the secondary one, for teams that already run Kubernetes.

It runs the same image as Compose:

- `app`: gunicorn, which serves the web app and the API;
- `worker`: schedules, queued jobs and the nightly encrypted backup;
- a pre-install / pre-upgrade migration Job;
- optionally, the MCP server.

**What's different from Compose:**

- **PostgreSQL 17 is yours to provide.** The chart bundles none. Use a managed server, an operator such as CloudNativePG, or another chart.
- **TLS terminates at your ingress controller.** Caddy isn't used.
- **The secrets come from Kubernetes Secrets.** The Compose `init` service isn't used.

**Status:** CI's `deploy-config` job runs `helm lint` and `helm template` with the default values, with `examples/external-postgres.yaml` and with `values-cloud.yaml`, and checks that unsafe cloud values are refused. All passed locally with Helm 3.19.0 (in the `alpine/helm` container). `apps/api/tests/test_helm_chart.py` checks the chart's structure. Not yet installed on a cluster. The image names are the published ones, `ghcr.io/softwarden/softwarden` and `ghcr.io/softwarden/softwarden-mcp`; point `image.repository` (and `mcp.image.repository`) at your own registry if you build the image yourself. The published images are linux/amd64 only; on arm64 nodes, build the image from source (docs/INSTALL.md step 4b) and push it to your own registry.

## Before you install

1. **A database and a role.** Create an empty database `softwarden` owned by a role `softwarden` with a password, on PostgreSQL 17.
2. **The database password**, in a Secret named by `database.existingSecret` (default `softwarden-database`, key `password`):

   ```bash
   kubectl create namespace softwarden
   kubectl -n softwarden create secret generic softwarden-database --from-literal=password='<the role password>'
   ```

3. **The installation's keys.** Either let the chart generate them at first install (see "Generated keys" below), or create them yourself and set `secrets.existingSecret`. Creating them yourself is recommended: you hold the copy before anything is encrypted.

   ```bash
   python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > django_secret_key
   python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())' > credential_keys   # a Fernet key
   python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > backup_key
   python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ops_token
   kubectl -n softwarden create secret generic softwarden-keys \
     --from-file=django_secret_key --from-file=credential_keys --from-file=backup_key --from-file=ops_token
   ```

   Store `credential_keys` and `backup_key` in your password manager or vault, off the cluster, then delete the local files. Without `credential_keys`, no secret saved in Softwarden can be read (connection client secrets, SMTP passwords). Without `backup_key`, no backup can be restored. Neither key is inside a database backup.

| Key | What it is | Compose equivalent |
|---|---|---|
| `django_secret_key` | Django's signing key (required) | `SOFTWARDEN_SECRET_KEY` |
| `credential_keys` | Fernet keys, one per line, newest first | `SOFTWARDEN_CREDENTIAL_KEYS` |
| `backup_key` | The nightly backups' encryption key | `SOFTWARDEN_BACKUP_KEY_FILE` |
| `ops_token` | Bearer token for `/healthz/ops` (optional) | `SOFTWARDEN_OPS_TOKEN` |

The pods copy these files into a private in-memory volume at `/data/secrets`, readable only by the app user (uid 10001). An init container does the copy. The app then reads them exactly as it reads the Compose secrets volume.

## Install

```bash
helm install softwarden deploy/helm/softwarden -n softwarden \
  --set publicHostname=softwarden.example.com \
  --set image.repository=ghcr.io/softwarden/softwarden \
  --set image.tag=1.0.0 \
  --set secrets.existingSecret=softwarden-keys \
  --set database.host=<postgres host>
```

The image `ghcr.io/softwarden/softwarden:1.0.0` exists once release 1.0.0 is published. Until then, build the image from the repository (`docker build -t <your registry>/softwarden:<tag> .`), push it to a registry the cluster can pull from, and set `image.repository` and `image.tag` to it.

Or use a values file: [`examples/external-postgres.yaml`](examples/external-postgres.yaml) shows a managed PostgreSQL with `sslmode=require`, SMTP, and cert-manager on ingress-nginx. For the hosted, multi-organization service use [`values-cloud.yaml`](values-cloud.yaml) and [docs/CLOUD.md](../../../docs/CLOUD.md); cloud mode is not supported for production in 1.0 (evaluation with test data only).

Then follow the steps the install prints:

1. Wait for the app.
2. Create the first Admin with `kubectl exec -it deploy/softwarden-app -- python manage.py bootstrap_admin --create`. The web setup page needs the one-time token from the same pod, so the command is simpler here. For an unattended run (a Job), add `--no-input` and set `BOOTSTRAP_ADMIN_PASSWORD`, `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_NAME` in its environment (self-hosted also needs `--org-name` and `--timezone`). With `deployment: cloud` the command creates the operator's platform staff account instead ([docs/CLOUD.md](../../../docs/CLOUD.md#first-run)).
3. Run `python manage.py doctor`.
4. Configure discovery ([docs/FIRST_RUN.md](../../../docs/FIRST_RUN.md)).

## External PostgreSQL values

| Value | Default | Meaning |
|---|---|---|
| `database.host` | `softwarden-postgresql` | Host name of the server (`POSTGRES_HOST`). |
| `database.port` | `5432` | Port (`POSTGRES_PORT`). |
| `database.name` | `softwarden` | Database (`POSTGRES_DB`). |
| `database.user` | `softwarden` | Role (`POSTGRES_USER`). |
| `database.existingSecret` | `softwarden-database` | Secret holding the password. Set it to `""` and give `database.password` instead only for a trial: the password then stays in the release's values. |
| `database.existingSecretPasswordKey` | `password` | Key in that Secret. |
| `database.sslmode` | empty (libpq's `prefer`) | `POSTGRES_SSLMODE`. Use `require` or `verify-full` for a managed server; `deployment: cloud` refuses anything weaker. |
| `database.sslrootcert` | empty | `POSTGRES_SSLROOTCERT`: the path of the server's CA bundle inside the containers, mounted with `extraVolumes`. |
| `database.connMaxAge` | empty (60 s) | `SOFTWARDEN_DB_CONN_MAX_AGE`. The age is checked only when a request starts and ends, so an idle gunicorn thread keeps its connection: plan for replicas × `app.gunicornWorkers` × `app.gunicornThreads` connections (with `autoscaling.maxReplicas`, 6 × 3 × 8 = 144 by default) plus a few for the worker, below PostgreSQL's `max_connections`. |
| (a pooler) | none | A PgBouncer or built-in pooler in front of the server must run in **session** mode for app and worker alike. Transaction pooling isn't supported: the backup lock and the Graph mail slots are session advisory locks ([docs/CLOUD.md](../../../docs/CLOUD.md#connection-poolers)). |
| `database.jit` | `off` | `SOFTWARDEN_DB_JIT`. Set `on` behind a pooler that refuses the `options` startup parameter (PgBouncer, Azure's built-in pooler on 6432); then run `ALTER ROLE softwarden SET jit = off;` once. |

The role needs to own its database: migrations create tables, indexes and constraints. It needs no superuser rights. The worker's backup runs `pg_dump` against the same server with the same role.

## Other values

- **`deployment`.** `selfhosted` (default) or `cloud` (`SOFTWARDEN_DEPLOYMENT`). `cloud` also makes the chart refuse to render without `secrets.existingSecret`, `database.existingSecret`, `database.sslmode` of `require` or stronger, and `smtp.host`, plus `smtp.existingSecret` whenever `smtp.user` is set ([docs/CLOUD.md](../../../docs/CLOUD.md)).
- **`worker.replicas`.** The worker scales apart from the app. Several are safe, because each schedule fire and each job runs once. Each worker pod's liveness probe reads its own heartbeat file (`run_worker --check --local`). It stays fresh while the main loop is between ticks or on one schedule run or job for up to 65 minutes, so a worker stuck longer is restarted ([docs/CLOUD.md](../../../docs/CLOUD.md#scaling)). More than one needs ReadWriteMany media and backup volumes.
- **`app.autoscaling`.** An optional HorizontalPodAutoscaler on CPU. While it is on, `app.replicas` is ignored. Turning it on for an existing release removes `replicas` from the Deployment, and Kubernetes resets the Deployment to 1 app pod until the HPA scales it back to `minReplicas` (within a minute or so). Do it outside busy hours.
- **`podDisruptionBudget`.** PodDisruptionBudgets for app and worker (`minAvailable`). Use them with 2 or more replicas.
- **`app.topologySpreadConstraints`, `worker.topologySpreadConstraints`, `worker.affinity`.** Spread the pods across nodes. An entry without a `labelSelector` gets this release's own selector labels for that workload (name, instance, component). With a ReadWriteOnce volume, the worker is pinned beside the app instead.
- **`publicHostname`.** The name people open. The app's allowed hosts, CSRF origin and base URL follow it, as `PUBLIC_HOSTNAME` does in Compose.
- **`trustedProxies`.** Who may set `X-Forwarded-*`. The default is the private ranges. Narrow it to your ingress controller's pod CIDR where you can. The MCP server gets the same list (`MCP_FORWARDED_ALLOW_IPS`): it takes the client's address as the rightmost `X-Forwarded-For` entry not in it, and limits refused API tokens per client address; a list that doesn't cover the ingress pods makes every request count as the ingress's.
- **`smtp.*`.** The installation's default email. Put the password in a Secret (`smtp.existingSecret`).
- **`extraEnv`.** Any other variable from [docs/ENV.md](../../../docs/ENV.md), for example attachment limits or rate limits.
- **`persistence.media`.** The attachment files, shared by app and worker. With `ReadWriteOnce` (the default), the worker is scheduled on the app's node and the app uses the Recreate strategy. For more than one app or worker pod, use a `ReadWriteMany` storage class: the chart refuses to render otherwise, unless you bring your own claim.
- **`persistence.backups`.** The worker's backup sets. Copy them off the cluster as well, for example with a CronJob, or by snapshotting the volume. Both PVCs are kept on `helm uninstall` (`helm.sh/resource-policy: keep`). With `persistence.backups.enabled: false`, there is no backup volume, and `SOFTWARDEN_BACKUP_DIR` is empty in every pod, so the System page and `doctor` say backups aren't configured. Back up another way: managed PostgreSQL backups plus the media volume, or `ops/backup.sh` against the database.
- **`extraVolumes` and `extraVolumeMounts`.** Mount extra files into the app, the worker and the migration job. The usual case is the CA bundle of a PostgreSQL that requires TLS: mount it, set `database.sslmode: verify-full`, and set `database.sslrootcert` to the mounted path. The worker's `pg_dump` and `pg_restore` follow the same settings, and libpq's `PGSSLCERT` and `PGSSLKEY` (in `extraEnv`) as the app does.
- **Keys from `secrets.existingSecret`** are read as provided files: the chart sets `SOFTWARDEN_CREDENTIAL_KEYS_FILE` and `SOFTWARDEN_BACKUP_KEY_FILE` to their copies in `/data/secrets`. You created and stored them yourself, so the System page doesn't ask you to confirm a backup-key copy.
- **Read-only root filesystem.** Every container runs with `readOnlyRootFilesystem: true` and a writable `emptyDir` at `/tmp` (`tmpSizeLimit`, 256Mi), where uploads are staged and the backup keeps its scratch files.
- **`ingress.annotations`.** These default to `nginx.ingress.kubernetes.io/proxy-body-size: 26m`, because ingress-nginx's 1 MB default would refuse attachment uploads, and `nginx.ingress.kubernetes.io/proxy-request-buffering: "on"` (see Request buffering). For another controller, set its equivalent.
- **Request buffering.** The ingress must read each request body in full before it sends the request to the app. gunicorn runs a few processes of 8 threads each (`app.gunicornWorkers`, `app.gunicornThreads`), and a client that declares a body and then sends it one byte at a time holds a thread for as long as it likes: without buffering, a few dozen such connections, unauthenticated, stop the app. ingress-nginx buffers by default (the annotation keeps a cluster-wide `proxy-request-buffering: off` from applying here); on another controller or load balancer, turn its request buffering on. The Compose stack's Caddy does the same with `request_buffers`.
- **`mcp.enabled`.** Runs the MCP server and routes `/mcp` to it through the same Ingress ([docs/MCP.md](../../../docs/MCP.md)).

## Generated keys

Without `secrets.existingSecret`, the chart generates the four keys at first install, in the Secret `<fullname>-secrets` (`softwarden-secrets` for a release named `softwarden`). Without `database.existingSecret`, the password from `database.password` goes into `<fullname>-database`.

Both Secrets are pre-install hooks with `helm.sh/resource-policy: keep`. Helm leaves them alone on `helm upgrade` and in place on `helm uninstall`. Read the keys back now and store them off the cluster; the install's notes print the `kubectl` commands.

**Reinstalling into the same namespace keeps the keys.** Each Secret is looked up first (`lookup`) and its existing values are written back. Only a namespace with no such Secret gets new ones. To start over with new keys, delete both Secrets after `helm uninstall`. The database and the stored secrets encrypted with the old key are then unreadable, so do it only for an empty database.

**GitOps (Argo CD, Flux with rendered manifests) must set `secrets.existingSecret` and `database.existingSecret`.** These tools render the chart without a cluster, where `lookup` finds nothing, and Argo CD re-runs pre-install hooks on every sync. Generated keys would be replaced at each sync, and everything encrypted under the old ones lost.

## Security headers

The app sends its security headers itself ([SECURITY.md](../../../SECURITY.md), "HTTP headers"): `X-Frame-Options: DENY`, the single-page app's `Content-Security-Policy` (attachment downloads send their own instead), `Permissions-Policy`, `X-Content-Type-Options`, `Referrer-Policy` and `Cross-Origin-Opener-Policy`. Nothing needs adding on the ingress.

- **HSTS.** While `ingress.enabled` and `ingress.tls.enabled` are on, the chart sets `SOFTWARDEN_HSTS_SECONDS` to `ingress.hstsSeconds` (default one year) and the app sends `Strict-Transport-Security` on HTTPS requests. Set `ingress.hstsSeconds: 0` if your controller already sends it.
- **Don't add a CSP on the ingress** (for example through an ingress-nginx `custom-headers` ConfigMap). Such headers are added unconditionally, so attachment downloads would carry two policies, and the stricter one wins.
- **The body limit** is the ingress's: the chart's ingress sets `nginx.ingress.kubernetes.io/proxy-body-size: 26m`; set the equivalent on another controller.
- **No service account token** is mounted in the chart's pods (`automountServiceAccountToken: false`); nothing in them calls the Kubernetes API. The chart has no NetworkPolicy; if your cluster enforces them, allow the ingress controller to reach the app on port 8000, and app and worker to reach PostgreSQL, DNS and the outbound services your organizations connect to (Microsoft Graph, SMTP, webhooks).

## Upgrade

**Upgrading to the chart with cloud deployment support, with `secrets.existingSecret` set:** the chart now sets `SOFTWARDEN_CREDENTIAL_KEYS_FILE=/data/secrets/credential_keys` and `SOFTWARDEN_BACKUP_KEY_FILE=/data/secrets/backup_key`. A keys file outranks `SOFTWARDEN_CREDENTIAL_KEYS` given through `extraEnv`. Before upgrading, check that your Secret has a `credential_keys` key holding every key in use (newest first) and a `backup_key`. If it has none, or holds an older key than the one you passed through `extraEnv`, stored secrets (connection client secrets, SMTP passwords, webhook URLs) become unreadable and backups fail. Put the keys into the Secret first, then upgrade.


1. Take a backup: `kubectl exec deploy/softwarden-worker -- python manage.py backup_now --inline`.
2. Run `helm upgrade` with the new `image.tag`.

The migration Job runs first. If it fails, the release fails and the running pods keep the old version. Migrations only go forward, so rolling back means restoring the backup ([upgrade runbook](../../../docs/04-development/upgrade-runbook.md), [backup runbook](../../../docs/04-development/backup-runbook.md)).
