# Install Softwarden

This guide takes you from a fresh **Ubuntu Server 24.04** VM to a signed-in Admin with nightly backups running. You need to be comfortable with a Linux shell, Docker and DNS. When you finish, [the first-run guide](FIRST_RUN.md) connects Microsoft Entra discovery.

The two guides together should take **under 30 minutes from `git clone` to discovery configured**, using the published release image (step 4b). Each step shows roughly how long it takes. Building from source (step 4a), which is the only way until release 1.0.0 is published, adds about 10 minutes.

**Until 1.0.0 is published,** the tag `v1.0.0` and the image `ghcr.io/softwarden/softwarden:1.0.0` don't exist yet. Follow the source path: stay on `main` in step 2, leave `SOFTWARDEN_IMAGE` out in step 3, and build in step 4a. The release-image path (step 4b) works once the release is out.

The commands are Ubuntu's. On Windows, `scripts/setup.ps1` (PowerShell 5.1 or 7, with Docker Desktop) runs step 4 with the same checks as `scripts/setup.sh`, for trying Softwarden on a workstation; a server install is Linux.

Docker Compose is the supported way to install. There is also a [Helm chart](../deploy/helm/softwarden/README.md) for Kubernetes. To run Softwarden as a hosted, multi-organization service (public signup, managed PostgreSQL, scaled workers), follow [the cloud deployment guide](CLOUD.md) instead. Cloud mode is not supported for production in 1.0: evaluate it with test data only, and run one installation per organization ([why](CLOUD.md#not-supported-for-production-in-10)).

**Reference pages:**

- [ENV.md](ENV.md): every setting;
- [CLOUD.md](CLOUD.md): the hosted (cloud) deployment;
- [PERMISSIONS.md](PERMISSIONS.md): every Microsoft permission;
- [the backup runbook](04-development/backup-runbook.md);
- [the upgrade runbook](04-development/upgrade-runbook.md);
- [the SSO recovery runbook](04-development/sso-recovery-runbook.md);
- [SECURITY.md](../SECURITY.md).

**What has been tested.** These steps were written from the Compose files and management commands and checked against release 1.0.0. A complete run on a fresh VM, timed end to end, hasn't been recorded yet, and the Let's Encrypt option hasn't been tried on a VM.

## Installing with Helm (Kubernetes)

The [chart's README](../deploy/helm/softwarden/README.md) has the steps. One thing to know before you install: the chart creates an ingress-only NetworkPolicy by default (`networkPolicy.enabled`), so the app answers only to the namespace of your ingress controller, `ingress-nginx` unless you change it. On another controller (k3s's Traefik in `kube-system`, `traefik`, `openshift-ingress`, a Gateway API controller) or behind a cloud load balancer that sends traffic straight to pods (GKE NEG, AWS Load Balancer Controller in IP mode, AGIC), set `networkPolicy.ingressNamespaceSelector` or `networkPolicy.extraIngress`, or turn the policy off with `networkPolicy.enabled=false`; otherwise the site will not answer. The notes printed after `helm install` show the selector in use.

## What you need

- **A VM.** 2 vCPU, 4 GB RAM and 20 GB disk, running Ubuntu Server 24.04 LTS. Any distribution with Docker Engine works; the commands below are Ubuntu's.
- **A DNS name** for it, such as `softwarden.example.com`, and ports 80 and 443 reachable from the people who will use it.
- **A certificate.** Either your own, Let's Encrypt (the VM must then be reachable from the internet), or Caddy's local CA. With the local CA, browsers warn until you trust it.
- **For discovery ([FIRST_RUN.md](FIRST_RUN.md)):** someone who can create an app registration and grant admin consent in your Microsoft Entra tenant.

After the images are pulled or built, Softwarden itself makes no outbound connection except to the services you configure (Microsoft Graph, your SMTP server) and, once a day from the worker, `https://endoflife.date` for public end-of-life dates (`SOFTWARDEN_EOL_ENABLED=0` turns that off): no CDN, telemetry or external fonts.

## 1. Install Docker (4 min)

Use Docker's own apt repository, as [Docker's guide for Ubuntu](https://docs.docker.com/engine/install/ubuntu/) describes:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"    # then log out and back in, so `docker` works without sudo
docker compose version             # 2.x
sudo systemctl is-enabled docker   # "enabled"; if not: sudo systemctl enable --now docker containerd
```

Every Softwarden container has `restart: unless-stopped`, so it comes back after a reboot, but only when the Docker service itself starts at boot. Docker's Ubuntu packages enable it; the last command checks.

## 2. Get the code (1 min)

```bash
git clone https://github.com/Softwarden/softwarden.git ~/softwarden
cd ~/softwarden
```

Once release 1.0.0 is published, check out the release you install: `git checkout v1.0.0` (`git tag` lists them). Until then, stay on `main`.

All later commands run in `~/softwarden`. Keep this folder even with the release image: `compose.yaml` mounts `docker/Caddyfile` from it into the proxy. Planned after 1.0: the Caddyfile inside an image, so that a release install needs only `compose.yaml`; until then, the clone is part of the install.

## 3. Name, certificate and image (2 min)

No settings file is needed. On first start, the one-shot `init` service generates this installation's secrets into the `softwarden-secrets` volume:

- the Django key;
- the database password;
- the credential encryption key;
- the backup key.

What you do set goes in `.env`, next to `compose.yaml`:

```bash
cat > .env <<'EOF'
# The name people open. The app's allowed host, CSRF origin and links follow it.
PUBLIC_HOSTNAME=softwarden.example.com
EOF
chmod 600 .env
```

That is all a source build (step 4a) needs: it tags its image `softwarden:local`.

**With the release image (step 4b, once 1.0.0 is published),** also pin the release you run; step 4b pulls it, and the upgrade runbook changes this line:

```bash
echo 'SOFTWARDEN_IMAGE=ghcr.io/softwarden/softwarden:1.0.0' >> .env
```

The release workflow (`.github/workflows/release.yml`) of [Softwarden/softwarden](https://github.com/Softwarden/softwarden) pushes each `v*` tag to `ghcr.io/softwarden/softwarden` (the app, also used by `init` and `worker`) and `ghcr.io/softwarden/softwarden-mcp` (the optional MCP server, `SOFTWARDEN_MCP_IMAGE`), tagged `1.0.0`, `1.0` and `latest`. Pin the full version, never `latest`. The published images are linux/amd64 only; on an arm64 host, build from source (step 4a). Each image carries an SBOM and a signed provenance attestation: `gh attestation verify oci://ghcr.io/softwarden/softwarden:1.0.0 --owner Softwarden` checks it was built by that repository's workflow.

Then choose the certificate:

- **Your own certificate:** copy `cert.pem` (with its chain) and `key.pem` into `./certs/`, and add `CADDY_TLS=/certs/cert.pem /certs/key.pem` to `.env`.
- **Let's Encrypt:** add `CADDY_TLS=you@example.com` to `.env`. Caddy then requests a certificate for `PUBLIC_HOSTNAME`. This needs ports 80 and 443 open to the internet. It hasn't been tried on a VM yet.
- **Caddy's local CA** (the default): add nothing. Browsers warn until each user's machine trusts the CA. After step 4, copy the root certificate off the VM:

  ```bash
  docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt ./softwarden-root.crt
  ```

  Copy `softwarden-root.crt` to each workstation (for example with `scp`), then import it there:

  - **Windows**, in an elevated PowerShell: `Import-Certificate -FilePath softwarden-root.crt -CertStoreLocation Cert:\LocalMachine\Root`
  - **macOS:** `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain softwarden-root.crt`
  - **Ubuntu or Debian:** `sudo cp softwarden-root.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates`

  Firefox keeps its own store: import the file under Settings › Privacy & Security › Certificates. `scripts/setup.sh --trust-local-ca` only trusts the CA on the machine the stack runs on.

Everything else is optional: SMTP for the daily digest, other ports, attachment limits. [ENV.md](ENV.md) lists every setting with its default. For email, set `SOFTWARDEN_SMTP_HOST`, `SOFTWARDEN_SMTP_USER`, `SOFTWARDEN_SMTP_PASSWORD` and `SOFTWARDEN_EMAIL_FROM` now, or later per organization in Administration › Email settings.

If the network inspects TLS (a corporate proxy), put its root CA in `ca-certs/` before building (see `ca-certs/README.md`). `scripts/setup.sh` detects this.

### 3a. DNS and firewall (part of the 2 minutes, with your DNS and cloud console at hand)

- **DNS:** before step 4, create the A record (and AAAA, for IPv6) for `PUBLIC_HOSTNAME`, pointing at the VM. Let's Encrypt needs it to resolve from the internet. Check it from your workstation with `nslookup softwarden.example.com`.
- **Cloud firewall:** allow inbound TCP 80 and 443 in the VM's security group or network security group (Azure NSG, AWS security group, GCP firewall rule), from the networks your users are on. Keep SSH (22) limited to your admins.
- **On the VM:** Ubuntu's `ufw` is off by default. If you turn it on, Docker's published ports bypass its rules (Docker writes its own iptables rules), so the cloud firewall is what counts for 80 and 443. Nothing else is published: PostgreSQL is reachable only inside the Compose network.
- **No DNS name yet?** Use the VM's IP address as `PUBLIC_HOSTNAME` (for example `PUBLIC_HOSTNAME=10.20.30.40`) with Caddy's local CA; Let's Encrypt doesn't issue certificates for IP addresses. Switch to the name later by changing `PUBLIC_HOSTNAME` and running `docker compose up -d`.

## 4. Start it

### 4a. Build from source (12 min, mostly the first build)

```bash
sh scripts/setup.sh                # checks Docker, TLS interception and ports, builds, starts, waits, runs doctor
# or, without the checks:
docker compose up -d --build --wait
```

`scripts/setup.sh` is the recommended way: it stops early, with the fix, on what most often breaks a first install (no Compose v2, a TLS-inspecting proxy, a port in use).

### 4b. Or with the release image (2 min, once 1.0.0 is published)

```bash
docker compose pull --ignore-buildable
docker compose up -d --wait
```

The services start in this order:

1. `init` generates any missing secret and exits;
2. `db`;
3. `migrate`, which applies the migrations and exits;
4. `app`, which prepares the first-run token and serves;
5. `worker` and `proxy` (Caddy).

Only `proxy` is published. Every container runs with no-new-privileges, only the Linux capabilities it uses, and a memory limit, and the app's containers run on a read-only filesystem (`SECURITY.md`, "Containers"; the limits and how to change them are in [the operations guide](04-development/operations.md#container-limits)). If `init` exits with an error, `docker compose logs init` says what to do.

### 4c. Check it (1 min)

```bash
docker compose exec app python manage.py doctor
```

`doctor` prints one line per check: `OK`, `WARN` or `FAIL`, with a `fix:` line for anything not OK. It never prints a secret, and it exits 1 on any failure (`--json` for scripts). On a fresh install, expect:

- `OK` for database, migrations, mode, worker, schedules, credential keys, email and the DNS resolver;
- `WARN` for setup (still open), the backup key (not yet copied off the host) and the backup (none yet);
- `OK` for `base_url` when `PUBLIC_HOSTNAME` names this server. While it is `localhost` (the default, as on a trial install), `base_url` is `WARN`: the certificate and every link are for a name only this machine can open.

Email is `OK` at this point because no organization exists yet to send anything. Steps 5 to 7 clear the setup and backup warnings. After step 5, `doctor` also lists your organization: `OK` for sign-in (password only), and `WARN` for discovery until [FIRST_RUN.md](FIRST_RUN.md) is done. From step 5, email is `WARN` until SMTP is set, in `.env` or per organization in Administration › Email settings. It doesn't block anything: digests and reminders are recorded as not sent, and every action stays in Needs attention.

## 5. Create the organization and first Admin (3 min)

```bash
docker compose exec app python manage.py bootstrap_admin
```

It prints the address to open (`https://<PUBLIC_HOSTNAME>/setup`) and a one-time token, valid for one hour. Run it again for a new one.

**With a non-default `HTTPS_PORT`** (for example `8443`), the printed address leaves the port out: open `https://<PUBLIC_HOSTNAME>:8443/setup` instead. To make it, and every link in email, carry the port, set `SOFTWARDEN_BASE_URL=https://<PUBLIC_HOSTNAME>:8443` in `.env` and run `docker compose up -d` ([ENV.md](ENV.md)).

**The first time, it also prints the credential encryption key.** Copy it now into your password manager or vault, off this VM. It encrypts every secret typed into Softwarden (connection client secrets, SMTP passwords). It is not in database backups, and `docker compose down -v` deletes it. It is shown once. Afterwards, `docker compose exec app cat /data/secrets/credential_keys` shows it again. Planned after 1.0: a `recovery_kit` command that prints both recovery keys (this one and the backup key of step 6) in one step; until then, copy this one now and the backup key in step 6.

Open the address. Accept the certificate warning if you use the local CA. Enter:

- the token;
- the organization name;
- the timezone (for example `America/New_York`);
- the Admin's email, display name and password (12 or more characters).

You are signed in as that Admin, who is also the IT coordinator. Setup then closes for good.

To create the Admin from the terminal instead, run `docker compose exec -it app python manage.py bootstrap_admin --create` (it prompts).

## 6. Copy the backup key off the host (2 min)

The worker backs up the database and attachment files every night at 02:30 UTC. The sets are encrypted with the generated `backup_key`, which lives on this same host. **Lose the key and every backup is unreadable.**

```bash
docker compose exec worker cat /data/secrets/backup_key
```

Store the output in the same password manager or vault as the credential key, kept by two people. Then tell Softwarden it's done, so the System page and `doctor` stop warning. Use the backup key acknowledgment on Administration › System status. From a terminal, the API does the same with an Admin's read-write API token (Administration › API tokens):

```bash
curl -fsS -X POST -H "Authorization: Bearer sw_<token>" https://<PUBLIC_HOSTNAME>/api/v1/system/backup/acknowledge-key
```

Add `-k` to `curl` while you use Caddy's local CA. If you'd rather keep the key in a file you manage, set `SOFTWARDEN_BACKUP_KEY_FILE` instead (see [the backup runbook](04-development/backup-runbook.md)).

## 7. Take the first backup (2 min)

```bash
docker compose exec worker python manage.py backup_now --inline
```

Planned after 1.0: the first backup queued automatically when setup closes. Until then, run this step yourself; the nightly backup otherwise first runs at 02:30 UTC.

It ends with the set's name, and exits 0 only if the set was published. `doctor` then shows `OK` for backup and the backup key. Administration › System status shows the last good backup's age; `GET /api/v1/system` (Admin) has the same `backup` block.

The sets stay in the `softwarden-backups` volume on this VM's disk. Before you load real data:

- copy them off the host, or move them to another disk;
- rehearse a restore with `ops/restore.sh --rehearse`.

The backup runbook has both.

## 8. Set up discovery

Continue with [FIRST_RUN.md](FIRST_RUN.md). It creates the Entra app registration and the connection, then runs the first discovery.

## Upgrade

Every upgrade follows [the upgrade runbook](04-development/upgrade-runbook.md). Planned after 1.0: an `ops/upgrade.sh` that runs these steps in order and stops at the first failure; until then, run them one by one as listed. The short version:

```bash
# 1. What must survive, copied out of the container (recreating it in step 4 empties its /tmp)
docker compose exec app python manage.py upgrade_check --write-manifest /tmp/before.json
docker compose cp app:/tmp/before.json ./upgrade-before.json
# 2. A fresh backup, always
docker compose exec worker python manage.py backup_now --inline
# 3. Fetch the new image, then pre-check the data with it, before anything migrates
#    edit .env: SOFTWARDEN_IMAGE=ghcr.io/softwarden/softwarden:<new tag>
docker compose pull --ignore-buildable   # from source: git fetch --tags && git checkout <new tag> && docker compose build
docker compose run --rm --no-deps app python manage.py upgrade_check --precheck
# 4. Stop the old app and worker, so nothing serves the old code while the schema changes, then start the new image
#    (the one-shot migrate service applies the migrations, then the app starts) and check
docker compose stop app worker
docker compose up -d --wait
docker compose exec app python manage.py doctor
docker compose cp ./upgrade-before.json app:/tmp/before.json
docker compose exec app python manage.py upgrade_check --manifest /tmp/before.json
```

Migrations only go forward. Rolling back means restoring the backup taken before the upgrade.

## Day to day

- `docker compose exec app python manage.py doctor`: the whole setup at a glance.
- `https://<PUBLIC_HOSTNAME>/readyz`: point an outside monitor here. It answers 503 when the database, migrations or the worker aren't right.
- `docker compose logs app worker`: JSON log lines, rotated at 10 MB × 5 files.
- `docker compose down` stops the stack and keeps the data. **`docker compose down -v` deletes every volume:** the database, the attachment files, the backups and the generated keys.

## If something goes wrong

| Symptom | Check |
|---|---|
| `doctor` or `/readyz` says the database is unreachable, or migrations are pending | `docker compose logs db migrate app`. The one-shot `migrate` service applies migrations before the app starts; a failed migration stops there, and its log names the problem. |
| `docker compose up -d --wait` fails, or stops at `migrate`, on the first run or an upgrade | Run `docker compose ps -a` and `docker compose logs migrate app`. If `migrate` exited 0 and the app is only slow to answer, run `docker compose up -d --wait` again: nothing is lost, and finished migrations aren't repeated. If `migrate` exited non-zero, its log names the failing migration; fix the cause (often the database password or disk space) and run the command again. On a busy 2 vCPU VM the first run's migrations take a few minutes, and the app waits for them. |
| `init` exits with "The database already exists, but its password is missing" | The database volume predates generated secrets. Put its password in `.env` as `POSTGRES_PASSWORD` once, then `docker compose up -d`. |
| `init` exits with "POSTGRES_PASSWORD ... differs" | `.env` (or `compose.dev.yaml`) sets a password the database wasn't created with. Remove it, or set the database's password. |
| `app` restarts in a loop | `docker compose logs app`. It's usually a database password mismatch. "Set POSTGRES_PASSWORD" means no password was found at all (outside Compose, set it; Compose's `init` service generates one into the secrets volume). "SOFTWARDEN_DEBUG=1 is for local development only" means `.env` turns development mode on: remove it. |
| `doctor` says `database_password` FAIL | The database password is the published development one. Change the role's password and set `POSTGRES_PASSWORD` to it for app, worker and migrations. |
| `doctor` says `demo_accounts` WARN (also on Admin › System status) | `seed_demo`'s `@example.test` accounts still have the published demo password. Deactivate or remove them, or give each a new password (`manage.py reset_admin <email> --any-member`). |
| `doctor` says `base_url` FAIL, or the browser shows "Bad Request (400)" | `PUBLIC_HOSTNAME` isn't the name in the browser. Set it in `.env`, then `docker compose up -d`. |
| Sign-in fails with "CSRF" in the logs | `SOFTWARDEN_CSRF_TRUSTED_ORIGINS` must match the browser's address exactly, `https://` included. Leave it unset to follow `PUBLIC_HOSTNAME`. |
| `doctor` says `worker` FAIL | `docker compose logs worker`. Digests, connector runs and backups wait for it; every action is still in Needs attention. |
| `doctor` says `schedules` FAIL (overdue or stuck) | The worker is alive, but a schedule hasn't run on time, usually because a long job is holding it. Administration › System status shows each schedule's last start and error. |
| `doctor` says `backup_key` or `backup` WARN or FAIL | See steps 6 and 7 and the backup runbook. The worker log has the failing stage: `docker compose logs worker \| grep backup`. |
| `doctor` says `stored_dates` WARN | Some records hold a date outside 1900-01-01 to 9998-12-31, from a restore or a direct database edit (the API refuses such dates). The message names the fields; correct each date on its record. Until then, a renewal with such a date shows no deadline and sends no reminder. |
| `doctor` says `stored_secrets` WARN, System status says "Secrets unreadable", or `ops/restore.sh` fails at "checking the stored secrets" | A restore or a new secrets volume lost the key that encrypted them, and the credential key is in no backup. Bring the old `credential_keys` from its off-host copy and put it in `SOFTWARDEN_CREDENTIAL_KEYS` (after the current key, if anything was saved since), then run `docker compose up -d`. Or enter those secrets again. |
| The setup token is refused | It expires after an hour. After five wrong tries from one address, the page waits five minutes. Run `bootstrap_admin` again. |
| Locked out of the only Admin account | `docker compose exec app python manage.py reset_admin <email>` prints a temporary password. |
| Single sign-on is broken and nobody can sign in | Follow [the SSO recovery runbook](04-development/sso-recovery-runbook.md). |
| `app` logs "Trusted proxy 'proxy' does not resolve" once at start-up | Expected until Caddy runs; the app retries every 15 s. If it persists, check that `proxy` is running. |
| The browser warns about the certificate | Expected with Caddy's local CA. Use your own certificate or Let's Encrypt (step 3), or trust the local CA on your machine. |
