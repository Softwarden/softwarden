# Back up and restore Softwarden

This runbook is for whoever looks after a Softwarden installation. It explains what is backed up, how to keep the backup key safe, how to get backups off the host, and how to restore, rehearse a restore and run a restore drill.

**Targets:** RPO 24 hours (at most one night of work lost), RTO 1 business day (back in service the next working day).

**In short:** the worker takes an encrypted backup every night with nothing to set up. You must copy the backup key off the host, and copy the backups off the host too, because by default both live on the same disk as the database.

**What has been tested.** The worker's backup has been run in the image against PostgreSQL 17, and its set was decrypted, restored into a scratch database and checked (`migrate --check`, `upgrade_check --manifest`, `verify_attachments`) by hand. The host scripts were tested on a development machine against synthetic data. A run of the host script's nightly timer, and a full restore drill on a server, haven't been recorded yet.

## Two ways to take backups, one format

- **The worker takes them** (the default): nothing to set up, and the status shows on the System page.
- **The host scripts** (`ops/backup.sh` with its systemd timer) are an alternative, for a destination only the host can reach.

Both write the same encrypted files, so `ops/restore.sh` restores a set from either.

## The worker's nightly backup (default)

Every night at 02:30 UTC (`SOFTWARDEN_BACKUP_CRON`, five-field cron in UTC) the worker takes a backup. It writes into the `softwarden-backups` volume, `/data/backups` in the worker, as the image user (uid 10001):

```
/data/backups/daily/softwarden-20261107T023000Z/
    softwarden-20261107T023000Z.manifest.json.enc   upgrade_check --write-manifest, written just before the dump
    softwarden-20261107T023000Z.dump.enc            pg_dump -Fc
    softwarden-20261107T023000Z.media.tar.enc       the attachment files (media/, without .incoming)
    softwarden-20261107T023000Z.sha256              sha256sum lines for the three files
    softwarden-20261107T023000Z.sha256.hmac         the integrity tag: an HMAC of the .sha256 under the backup key
/data/backups/weekly/softwarden-20261108T023000Z/  a Sunday's (UTC) set, hard-linked from daily/
```

- **Encrypted the same way as the host script:** `openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass file:<key>`. `pg_dump` writes straight into `openssl`, and the media tar is streamed into it, so no readable dump touches the disk. The key goes to `openssl` by file, and the database password to `pg_dump` through a private 0600 `PGPASSFILE`: never on a command line, in a job's arguments, in a log line or on the System page.
- **Tamper-evident:** `openssl enc` (CBC) has no authentication of its own, and anyone who can write the share could rewrite both a file and its `.sha256`. So each set also carries `<set>.sha256.hmac`, `HMAC-SHA256 <hex>`: an HMAC-SHA256 of the `.sha256` file under a key derived from the backup key (`SHA-256("softwarden-backup-sums-hmac" NUL <key line>)`; `core/backup.sums_tag`, `ops/common.sh` `sums_tag`). `ops/restore.sh` checks it before the checksums, so a changed set fails even with a matching `.sha256`. Sets written before 1.0 have no tag: see [Restore](#restore).
- **Checked before it is published:** the dump is decrypted in full and listed with `pg_restore --list`, and the media archive is decrypted and read entry by entry.
- **Published in one step:** a set is built in `daily/.in-progress-<id>/`, the `.sha256` is written last, then the folder is renamed into place. A run that fails publishes nothing (its folder is removed; a crash's leftover is removed at the next run). A set without its `.sha256`, or whose `.sha256` doesn't list every file, is incomplete and never counted.
- **One at a time:** the nightly job and `backup_now --inline` can't overlap. Two sets in the same second get `-2`, `-3` suffixes. If the worker stops mid-run, the tools are stopped and nothing is published.
- **Database and files agree:** the dump is taken first, then the files. A file deleted after the dump stays on the volume for `SOFTWARDEN_ATTACHMENT_RETENTION_DAYS` (7), so every file the dump names is in the archive.
- **Disk safety:** the `softwarden-backups` volume is on the same disk as the database by default, and a full disk stops PostgreSQL too. Before writing, a run checks the destination's free space. It refuses when the free space minus an estimate (the last set's size × 1.2; on the first run, the database size plus the attachment files) would fall below a floor: `SOFTWARDEN_BACKUP_MIN_FREE_MB`, or by default the larger of 2 GB and 10% of the disk. The refusal is recorded as a failure (`backup_failed` on the System page). Move the sets to another disk (below) rather than lowering the floor.
- **Only into a mounted volume:** a run refuses a destination that isn't a mount point (`SOFTWARDEN_BACKUP_REQUIRE_MOUNT`, on by default). The app container has no backup volume, so `backup_now` there would write into the container's own layer and lose the set with it: run it in the worker.
- **Retention:** after each good set, the newest 14 daily and 8 weekly complete sets are kept (`SOFTWARDEN_BACKUP_KEEP_DAILY`, `SOFTWARDEN_BACKUP_KEEP_WEEKLY`). The newest complete set is never deleted.
- **Organization exports aren't in the media archive.** `media/exports/` (organizations' data exports and nightly tenant backups) is left out by both the worker and `ops/backup.sh`. Those zips are rebuilt from the database and the attachments, are short-lived, and would multiply every set by the number of tenant backups kept. After a restore their rows remain but the files are gone, so a download answers 404 until the next nightly backup or a new export. `verify_attachments` and `upgrade_check` don't look at them.

**Status.** Administration › System status shows the last good backup's time, age, size and set name, and the last failure (`GET /api/v1/system`, `backup`).

- After 26 hours without a good backup (`SOFTWARDEN_BACKUP_STALE_HOURS`) it shows the `backup_stale` warning. A run that failed after the last good one shows `backup_failed`.
- Only the installation's operator sees it: platform staff (also at `GET /api/v1/platform/system`) and, on a self-hosted installation (not cloud mode), the Admins of its first organization, the one `bootstrap_admin` made. The Admins of any other organization get the System page's `organization` view, with no backup block and no backup warnings.
- It names only the stage and a tool's exit status (for example `pg_dump exited with status 1.`). The tool's own message, with secrets removed, is in the worker log: `docker compose logs worker | grep backup`.
- If a Sunday set can't also be kept as the week's, the daily set still counts and the log says why.
- Warnings never fail `/readyz`.

**Take one now** (before an upgrade, or to test):

```bash
docker compose exec worker python manage.py backup_now --inline   # in this process; exit 0 only if the set was published
docker compose exec worker python manage.py backup_now             # queue the job and wait for the worker
```

**The key.** On first start, the init service generates `backup_key` in the `softwarden-secrets` volume, unless `SOFTWARDEN_BACKUP_KEY_FILE` names your own key file (a path inside the containers; mount it into `worker` and `app` yourself, read-only, mode 600, owned by uid 10001). **Lose the key and every backup is unreadable, and the generated key lives on the same host as the backups.**

Until an Admin confirms that a copy is off the host, the System page shows "Backup key is stored on this host: copy it somewhere safe" (`backup_key_unacknowledged`). Copy the key, store it as [the key section](#the-key-read-this-first) says, then acknowledge it on the System page (`POST /api/v1/system/backup/acknowledge-key`, by an Admin who sees the installation view; other Admins get 403). Platform staff can acknowledge it at `POST /api/v1/platform/system/backup/acknowledge-key`. The acknowledgment is audited as `backup.key_acknowledged` in every organization, since the key belongs to the whole installation: the Admin is named in their own organization, and shown as `via: another_admin` in the others. A new key brings the warning back.

```bash
docker compose cp worker:/data/secrets/backup_key ./backup.key   # then move it off the host and delete this copy
```

**Getting sets off the host.** The volume is on the same disk as the database. Either:

- copy the newest set somewhere else each day (`docker compose cp worker:/data/backups/daily/<set> /mnt/offsite/`), or
- put the sets on another disk from the start. In a `compose.override.yaml`, replace the worker's `softwarden-backups:/data/backups` with a bind mount of a directory owned by uid 10001 (`sudo install -d -m 700 -o 10001 -g 10001 /mnt/softwarden-backups`, then `- /mnt/softwarden-backups:/data/backups`).

The worker refuses a destination it can't write, and the System page shows why.

**Restoring a worker set.** Copy the set's folder and the key to the host (as above), then run `ops/restore.sh --yes <folder>/<set>.dump.enc` exactly as for a host-script backup ([Restore](#restore)), or prove it first with [`--rehearse`](#rehearsal-restore-into-a-scratch-database). To try the whole sequence safely, run it on a throwaway stack: `backup_now --inline`, copy the set and the key out, then `ops/restore.sh --rehearse`.

## What is backed up

- **The database.** One `pg_dump -Fc` holds every record, review, renewal, import run, attachment row and the audit log. Uploaded import files and their raw rows are in the database until they are purged after 30 days.
- **The attachment files:** the `softwarden-media` volume, `/data/media` in `app` and `worker`. Attachment rows are in the dump, but their files are not, so the backup archives the volume right after the dump. (`ops/backup.sh` runs `tar` in a one-off container of the app image, streamed straight into `openssl` with the same key and parameters, so no readable copy touches the disk.) A dump and the archive taken after it always agree, because a deleted attachment's file stays on the volume for `SOFTWARDEN_ATTACHMENT_RETENTION_DAYS` (7, longer than any backup run) before the nightly `attachments.sweep` removes it. `--no-media` skips the archive, for tests only. `docker compose down -v` deletes the volume with the others.
- **A manifest.** `upgrade_check --write-manifest` records what must survive (reviews, renewal completions, audit events; every record, contract and membership by ID). It is what proves a restore worked.
- **Not in the dump: the secrets.** The `softwarden-secrets` volume holds the generated Django key, database password and credential encryption key (`/data/secrets/credential_keys`), and `.env` holds any you set yourself (including an older `SOFTWARDEN_SECRETS_KEY`).
  - Copy the credential key off the host when it is first shown (`bootstrap_admin` prints it once) and after every rotation. Copy `.env` whenever it changes.
  - Without the credential key, a restored database can't read any stored secret, so connector client secrets must be entered again.
  - Without the database password, the app can't connect to the old volume. A restore into a fresh database uses the new one.
  - `docker compose down -v` deletes the secrets volume with the database volume.
- **Not in the dump: the images.** Compose builds or pulls them again. `SOFTWARDEN_IMAGE` in `.env` names the release to run.

The host script (`ops/backup.sh`) produces, each night, under `$BACKUP_DEST/daily/`:

| File | Contents |
|---|---|
| `softwarden-YYYY-MM-DD.dump.enc` | `pg_dump -Fc`, encrypted with AES-256 |
| `softwarden-YYYY-MM-DD.manifest.json.enc` | the `upgrade_check` manifest, encrypted |
| `softwarden-YYYY-MM-DD.media.tar.enc` | the attachment files (`tar` of the `softwarden-media` volume), encrypted |
| `softwarden-YYYY-MM-DD.sha256` | checksums of the files above |
| `softwarden-YYYY-MM-DD.sha256.hmac` | the integrity tag of the `.sha256` under the backup key (as the worker's) |

On Sundays the same files are also copied to `$BACKUP_DEST/weekly/`.

## Retention

14 daily and 8 weekly (Sunday) backups. After each run the script deletes the older ones, with their manifest and checksum files. That is about two months of history, and each backup is about the size of the compressed database. Agree any change to `BACKUP_KEEP_DAILY` / `BACKUP_KEEP_WEEKLY` with whoever owns the data.

## The key (read this first)

Backups are encrypted with `openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt` and a key file. **Lose the key and every backup is unreadable.**

- For the host script, generate it once: `sudo install -d -m 700 /etc/softwarden && openssl rand -base64 48 | sudo tee /etc/softwarden/backup.key >/dev/null && sudo chmod 600 /etc/softwarden/backup.key`.
- Keep a second copy **off the host and away from the backups**: your organization's password manager or a sealed vault, held by the person responsible for the data and one other person. A backup share that also holds the key protects nothing.
- The key never goes in the repository, in `.env`, or in `BACKUP_DEST`. The scripts refuse to run if the key file is missing, empty, readable by group or others, or inside the project directory.
- To rotate it: take a new backup with the new key, and keep the old key until the last backup made with it has been pruned (about 8 weeks).

## Set up the host script (alternative; 30 min, once)

1. **Pick the destination.** A mounted share or any folder you are given, on a different disk from the VM's own. Check that the user who will run the job can write there.
2. **Create the key** as above, and put the off-host copy away now, before the first backup.
3. **Write the settings.** Either set the variables in the systemd unit (step 5) or copy the example:

   ```bash
   cd ~/softwarden
   cp ops/backup.env.example ops/backup.env
   chmod 600 ops/backup.env
   # edit BACKUP_DEST and BACKUP_KEY_FILE
   ```

   | Variable | Meaning | Default |
   |---|---|---|
   | `BACKUP_DEST` | where backups go | required |
   | `BACKUP_KEY_FILE` | the key file | required |
   | `SOFTWARDEN_DIR` | the Compose project directory | the current directory |
   | `BACKUP_KEEP_DAILY` / `BACKUP_KEEP_WEEKLY` | how many to keep | 14 / 8 |

   `ops/backup.env` holds paths, not secrets, but keep it out of the repository. Variables already set in the environment win over the file.
4. **Run it once by hand** from the project directory, with the stack running (`app` and `db` up):

   ```bash
   cd ~/softwarden
   ops/backup.sh
   ```

   It prints one line per step, ending `backup complete`. The exit code is 0 only if every step passed. Check `$BACKUP_DEST/daily/` for the files.
5. **Schedule it.** Edit the three paths marked CHANGE in `ops/softwarden-backup.service`, then:

   ```bash
   sudo cp ops/softwarden-backup.service ops/softwarden-backup.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now softwarden-backup.timer
   systemctl list-timers softwarden-backup      # next run about 02:30, up to 15 minutes later
   ```

   The service user needs the `docker` group, read access to the key file, and write access to `BACKUP_DEST`.
6. **Test the timer's path:** `sudo systemctl start softwarden-backup.service`, then `journalctl -u softwarden-backup -n 20`.

## The host script's nightly job

`ops/backup.sh` does the following, in order, and stops with a non-zero exit on the first failure:

1. checks the key file and destination, and that the database is up;
2. writes the manifest with `upgrade_check --write-manifest` and encrypts it;
3. streams `pg_dump -Fc` through `docker compose exec -T db` straight into `openssl`, so no readable dump touches the disk;
4. decrypts the result and lists its contents with `pg_restore -l` to prove it's usable;
5. writes the checksum file for the dump (and manifest), so a later failure can't leave the dump without one;
6. streams a `tar` of the attachment volume (a one-off `docker compose run` of the app image) straight into `openssl`, then decrypts it and lists it with `tar -t` to prove it's usable, and rewrites the checksum file to include it. A file that changed while it was read (an upload during the backup) is kept as read and is not an error (`--warning=no-file-changed`, tar's exit 1). On Sundays it copies everything to `weekly/`;
7. prunes to 14 daily and 8 weekly (each with its manifest, media archive and checksums).

Nothing secret is printed. Output goes to the journal; unlike the worker's backup, this one doesn't show on the System page. **Someone has to look:** a missed night is invisible unless a person checks. Every week, run `systemctl status softwarden-backup.service` and `ls -l $BACKUP_DEST/daily | tail`, or have your monitoring alert when the newest file is over 26 hours old.

Before an upgrade, run `ops/backup.sh` by hand first. It replaces the `pg_dump` command in the [upgrade runbook](upgrade-runbook.md).

## Restore

This **replaces the database**. For a real recovery, tell the people who use Softwarden first: work done after the backup is lost.

```bash
cd ~/softwarden
docker compose up -d db
BACKUP_KEY_FILE=/etc/softwarden/backup.key \
  ops/restore.sh --yes $BACKUP_DEST/daily/softwarden-<date>.dump.enc
```

**On Windows (Git Bash, a developer machine; servers are Linux).** Two things differ. The key file must be outside the project folder, as on Linux (`BACKUP_KEY_FILE=/c/Users/<you>/softwarden-keys/backup.key`, not under `~/softwarden`). And NTFS keeps no Unix file modes, so the scripts can't check that the key file is private and refuse to run until you accept that with `SOFTWARDEN_ALLOW_NOACL_KEY=1` in the same command; the folder's Windows permissions must then keep the key private ([ENV.md](../ENV.md#host-scripts)).

It refuses without `--yes`. It then:

1. checks the integrity tag (`<set>.sha256.hmac`, an HMAC of the checksum file under the backup key), then the checksum file, and that the key opens the backup. A tag that doesn't match means the set was changed after it was written, or the key is another one: the script stops and changes nothing. A set without a tag or checksum file is refused too. Every set Softwarden 1.0 or later writes has both, so a missing one on such a set means it was altered or is incomplete: don't restore it. `--accept-untagged` is only for a set you know was written before 1.0 (the warning prints the time in the set's name), and from storage only you can write;
2. stops `app` and `worker`, drops and recreates the `softwarden` database, and runs `pg_restore`;
3. restores the attachment files, only with the default database:
   - Before stopping anything, it checks that the checksum file lists the media archive and that the archive decrypts in full and reads as a tar. Otherwise it stops and changes nothing (`--no-media` restores the database alone).
   - With app and worker stopped, it extracts the archive into a staging folder inside the volume (`/data/media/.incoming`). Only once that has succeeded does it remove the old files and move the staged ones into place, as the app user, keeping owner and mode.
   - A failed or cut-off stream never deletes the current files, and a leftover `.incoming` is cleared by the next attempt. A backup with no media archive is restored without one, and the script says so;
4. starts `app` and `worker`, waits for `app` to be healthy, runs `upgrade_check --manifest` with the manifest saved next to the backup, and then `verify_attachments`, which checks every attachment file's size and SHA-256 and fails the restore, naming any file that is missing or changed;
5. counts the stored secrets this stack's credential key can't open. Any at all fails the restore (exit 1), with a message saying which key is missing.

Every line should start with ✓ (or OK on a console that can't show the check mark, such as a Windows console on cp1252), and the script ends "restore complete; the manifest check, the attachment check and the stored-secrets check passed". Then sign in and open Needs attention. If the host is new, put the previous `.env` back first.

**The credential key isn't in any backup.** If the stack you restore onto has a different credential key from the one the data was saved with, every stored secret (connection client secrets, SMTP and SSO secrets, webhook and Teams URLs) is unreadable. Set `SOFTWARDEN_CREDENTIAL_KEYS=<the old credential key>` in `.env` before you restore (several keys: comma-separated, newest first).

**Attachment files.** With `--database NAME` the attachment volume is left alone, because it belongs to the live database. To check files by hand at any time, run `docker compose exec app python manage.py verify_attachments` (non-zero exit on a problem). Files that no row references (from a later archive) are harmless; the sweep removes them after the retention window.

`--database NAME` restores into a different database (for example `softwarden_restore_test`, to try a backup safely on a live stack). The manifest check is skipped then, because the app only looks at its own database. `--no-manifest` skips it; use that only when testing.

## Rehearsal: restore into a scratch database

`--rehearse` proves that a backup restores, without touching anything live: no `--yes`, no service stopped, and the live database and attachment volume untouched. It asks nothing and exits non-zero on the first failed step, so you can run it from a script or CI.

```bash
docker compose up -d db
BACKUP_KEY_FILE=/etc/softwarden/backup.key ops/restore.sh --rehearse ./softwarden-20261107T023000Z/softwarden-20261107T023000Z.dump.enc
```

It checks the integrity tag (`--accept-untagged` for a set written before 1.0) and the `.sha256` (required), and that the key opens the dump and the media archive. It restores into a scratch database (`softwarden_rehearsal`, or a `--database NAME` that doesn't exist yet: a rehearsal refuses to reuse any other existing database, since it drops its scratch database). Then, in one-off app containers pointed at that database, it runs:

- `migrate --check`: every migration of this image is applied;
- `upgrade_check --manifest` with the manifest saved in the set: nothing was lost;
- `verify_attachments` against the media archive unpacked inside the container: every attachment file is present and matches its size and SHA-256;
- finally, a count of the stored secrets this stack's credential key can't open. Any at all fails the rehearsal: the set's secrets were encrypted under a key this stack doesn't have.

The scratch database is dropped at the end, pass or fail. `--no-media` rehearses the database alone.

**Upgrade rehearsal.** A backup of the previous release restored under a new image has migrations to apply: add `--migrate` to run `migrate` on the scratch database first, then the same checks.

Linux only, like the other scripts (bash, GNU coreutils, `docker compose`). The one-off containers unpack the media archive into their own `/tmp`, so Docker's storage needs that much free space.

## Restore drill (before real data, then quarterly)

Restore to a fresh Compose stack before you load real data, then every quarter, on the version you are running. Aim to finish within a working day.

1. Bring three things to a spare VM (or a second project directory with its own volumes), each from its **off-host** copy, never from the live host: the latest nightly backup files, the backup key, and the credential key (`credential_keys`, the key `bootstrap_admin` printed once; on a default install it lives in the `softwarden-secrets` volume, not in `.env`, and **no backup contains it**). Bring a copy of `.env` too, if you keep one.
2. Follow [the install guide](../INSTALL.md) up to step 4, with the same `SOFTWARDEN_IMAGE` and that `.env`, plus `SOFTWARDEN_CREDENTIAL_KEYS=<the old credential key>` in it (several keys: comma-separated, newest first). Without it, the new stack generates a key of its own, and every stored secret in the restored data (connection client secrets, SMTP and SSO secrets, webhook and Teams URLs) is unreadable. Don't run `bootstrap_admin`. A rehearsal on the live stack (`ops/restore.sh --rehearse <set>`) is a quick check between drills, but it doesn't replace the drill.
3. Start only the database, then restore: `docker compose up -d db`, then `ops/restore.sh --yes <backup>`. Time it from the start of step 1. The script ends by counting the stored secrets this stack's credential key can't open; any at all fails it (exit 1), with a message saying which key is missing.
4. Confirm the manifest check passes (every line ✓) and that the script ends "restore complete; the manifest check, the attachment check and the stored-secrets check passed". Run `docker compose exec app python manage.py doctor`: there should be no `stored_secrets` warning. Sign in with an existing Admin, check that Administration › System status shows no "Secrets unreadable" warning, open Needs attention, open one record with its reviews, and run **Test connection** on one discovery connection or send a test email (either reads a stored secret).
5. Record the date, the backup used, the time taken and the result, and schedule the next drill for three months out. A pass means every check is ✓, **there is no `stored_secrets` warning**, and sign-in works. Data that restores without its secrets is a failed drill: the off-host copy of the credential key is missing or out of date.
6. Tear down the drill stack (`docker compose down -v`) and delete the copied backup files from the spare machine.

A failed drill is a finding, not a mark against anyone: fix the cause, note it, and run the drill again.

## Before real data

Before you load real data, make sure that:

1. **a restore drill has passed,** and the nightly backup has run successfully for at least a week;
2. **someone is named as responsible for the data:** they hold the off-host copy of the key, are told when a backup fails, and decide what happens to the data if you stop using Softwarden.

Until then, use synthetic data only.
