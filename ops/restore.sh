#!/usr/bin/env bash
# Restore an encrypted Softwarden backup into the Compose stack's database and attachment volume,
# or rehearse one in a scratch database. Linux hosts only (GNU coreutils, bash, docker compose).
#
#   BACKUP_KEY_FILE=/etc/softwarden/backup.key ops/restore.sh --yes [--database NAME] [--no-manifest] [--no-media] <set.dump.enc>
#   BACKUP_KEY_FILE=/etc/softwarden/backup.key ops/restore.sh --rehearse [--migrate] [--database NAME] [--no-media] <set.dump.enc>
#
# <set.dump.enc> is a dump written by ops/backup.sh (softwarden-YYYY-MM-DD.dump.enc) or by the worker
# (daily/softwarden-YYYYMMDDTHHMMSSZ/softwarden-YYYYMMDDTHHMMSSZ.dump.enc); its manifest, media archive and .sha256
# are read from next to it.
#
# THE DEFAULT REPLACES THE DATABASE AND THE ATTACHMENT FILES. It drops and recreates the database (connections are
# closed), runs pg_restore, then replaces the softwarden-media volume's contents with the media archive saved next to
# the dump. It refuses unless --yes is given. Run it from the Compose project directory, or set SOFTWARDEN_DIR.
#
#   --rehearse        prove the backup restores, changing nothing live: no --yes, no service stopped. Restores into a
#                     scratch database (default softwarden_rehearsal), runs `migrate --check`, `upgrade_check
#                     --manifest`, `verify_attachments` and the stored-secrets check against it in one-off app
#                     containers (the media archive goes to a throwaway volume, never the media volume), then
#                     drops it, pass or fail. The checksum file and the manifest are required. Non-interactive; exit 0
#                     only if every step passed.
#   --migrate         with --rehearse: run `migrate` on the scratch database first (a backup of the previous release,
#                     restored under this image: the upgrade rehearsal)
#   --database NAME   restore into NAME instead of the default "softwarden" (a throwaway test database; the
#                     attachment files are left alone then), or name the rehearsal's scratch database
#   --no-manifest     skip the upgrade_check and verify_attachments steps (testing without the app container)
#   --no-media        leave the attachment files as they are (with --rehearse: don't check the media archive)
#   --accept-untagged restore a set without an integrity tag (<set>.sha256.hmac) or checksum file. Only for a set
#                     you know was written before Softwarden 1.0: every set 1.0 or later writes has both, so a
#                     missing one there means the set was altered or is incomplete. Without it such a set is refused.
#
# The integrity tag is an HMAC of the checksum file under the backup key (ops/common.sh): a
# set changed after it was written fails, even when its .sha256 was rewritten to match.
# When restoring the default database, the app and worker are stopped first and started again afterwards (after the
# migrate service has run on the restored database), the manifest saved next to the backup is checked with
# upgrade_check --manifest, verify_attachments checks every attachment file, and the stored-secrets check fails the
# restore (exit 1, after the data is in place) when any stored secret was encrypted under a credential key this stack
# doesn't have: the credential key is never in a backup. For any other database without --rehearse those checks are
# skipped.
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=ops/common.sh
. "$script_dir/common.sh"

yes=0
rehearse=0
migrate=0
want_manifest=1
want_media=1
accept_untagged=0
db_name=""
backup=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) yes=1 ;;
    --rehearse) rehearse=1 ;;
    --migrate) migrate=1 ;;
    --no-manifest) want_manifest=0 ;;
    --no-media) want_media=0 ;;
    --accept-untagged) accept_untagged=1 ;;
    --database) shift; [ $# -gt 0 ] || die "--database needs a name"; db_name="$1" ;;
    -h|--help) sed -n '2,39p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $1 (try --help)" ;;
    *) [ -z "$backup" ] || die "only one backup file may be given"; backup="$1" ;;
  esac
  shift
done

if [ "$rehearse" -eq 1 ]; then
  db_name="${db_name:-softwarden_rehearsal}"
else
  db_name="${db_name:-softwarden}"
fi

step="starting"
migrate_step="migrating the restored database"
stopped_app=0
scratch_db=""
on_exit() {
  local rc=$?
  cleanup_scratch
  if [ -n "$scratch_db" ]; then
    if dc exec -T db psql -U softwarden -d postgres -v ON_ERROR_STOP=1 -q \
      -c "DROP DATABASE IF EXISTS \"$scratch_db\" WITH (FORCE)" >/dev/null 2>&1; then
      log "scratch database $scratch_db dropped"
    else
      log "WARNING: could not drop the scratch database $scratch_db; drop it by hand" >&2
    fi
  fi
  if [ "$stopped_app" -eq 1 ] && [ "$step" = "$migrate_step" ]; then
    # Never start this image's app on a schema its migrations didn't finish.
    log "app and worker left STOPPED: the migrations failed (above). Fix the cause, then 'docker compose up -d'." >&2
  elif [ "$stopped_app" -eq 1 ]; then
    dc start app worker >/dev/null 2>&1 || true
  fi
  if [ "$rc" -ne 0 ]; then log "FAILED during: $step (exit $rc)" >&2; fi
  exit "$rc"
}
trap on_exit EXIT

[ -n "$backup" ] || die "give the backup file to restore (see --help)"
[[ "$db_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "--database must be a plain identifier (letters, digits, underscore)"
[ -f "$backup" ] || die "backup file not found: $backup"
case "$backup" in *.dump.enc) ;; *) die "expected a .dump.enc file" ;; esac
if [ "$rehearse" -eq 1 ]; then
  [ "$db_name" != "softwarden" ] || die "--rehearse never touches the live database; give another --database name"
  [ "$yes" -eq 0 ] || die "--rehearse and --yes don't go together: a rehearsal changes nothing live"
  [ "$want_manifest" -eq 1 ] || die "--rehearse needs the manifest; --no-manifest is for a plain restore"
elif [ "$migrate" -eq 1 ]; then
  die "--migrate only goes with --rehearse (a plain restore runs the migrate service before the app starts)"
elif [ "$yes" -ne 1 ]; then
  die "this replaces the database \"$db_name\" with the contents of $(basename "$backup"). Run again with --yes to go ahead."
fi

backup="$(cd "$(dirname "$backup")" && pwd -P)/$(basename "$backup")"
stem="${backup%.dump.enc}"
manifest_enc="$stem.manifest.json.enc"
media_enc="$stem.media.tar.enc"
sums="$stem.sha256"
# The set's name carries the time it was taken (softwarden-YYYYMMDDTHHMMSSZ or softwarden-YYYY-MM-DD), shown in the
# untagged warnings so the operator can tell whether it predates 1.0.
set_label="$(basename "$stem") (taken $(basename "$stem" | sed 's/^softwarden-//'))"

load_config "$script_dir"
enter_project
check_key_file
make_scratch

# Stored secrets (connection client secrets, SMTP and SSO secrets, webhook and Teams URLs) are encrypted with the
# credential key, which lives in the secrets volume or .env, never in a backup set (backup runbook). Restored into a
# stack with another key they can't be read while every other check passes, so the restore
# and the rehearsal both count them with this stack's keys and fail loudly. Arguments: how to run manage.py.
check_stored_secrets() {
  local out unreadable
  out=$("$@" python manage.py shell -c 'from core import credentials; print("unreadable=%d" % credentials.unreadable_count())') \
    || die "could not count the stored secrets this stack's credential key can't read"
  unreadable=$(printf '%s\n' "$out" | sed -n 's/^unreadable=\([0-9][0-9]*\)$/\1/p' | tail -n 1)
  [ -n "$unreadable" ] || die "could not count the stored secrets this stack's credential key can't read"
  if [ "$unreadable" -ne 0 ]; then
    log "ERROR: $unreadable stored secret(s) in the restored database were encrypted under a credential key this" >&2
    log "ERROR: stack doesn't have, so they CAN'T BE READ: connection client secrets, SMTP and SSO secrets, webhook" >&2
    log "ERROR: and Teams URLs. The credential key is in no backup. Bring the old installation's credential_keys from" >&2
    log "ERROR: its off-host copy, set SOFTWARDEN_CREDENTIAL_KEYS (or SOFTWARDEN_CREDENTIAL_KEYS_FILE) in .env to it," >&2
    log "ERROR: newest key first, then 'docker compose up -d' and 'manage.py doctor' (no stored_secrets warning)." >&2
    log "ERROR: Otherwise every one of those secrets must be entered again (backup runbook, restore drill)." >&2
    return 1
  fi
  log "stored secrets: every one opens with this stack's credential key"
}

# sha256sum writes "<hash>  <name>", or "<hash> *<name>" in binary mode (Git Bash).
sums_list() { # <file>: is it listed in the checksum file?
  [ -f "$sums" ] && awk -v f="$(basename "$1")" '{ n = $2; sub(/^\*/, "", n); if (n == f) found = 1 } END { exit !found }' "$sums"
}

step="checking the checksum"
if [ -f "$sums" ]; then
  # The tag first: the checksum file is only worth checking against once it is known to be
  # the one written with this backup key; anyone who can write the share could otherwise rewrite both.
  step="checking the integrity tag"
  if [ -f "$sums.hmac" ]; then
    check_sums_tag "$sums" \
      || die "$(basename "$sums").hmac doesn't match: the set was changed after it was written, or this isn't its backup key; nothing was changed"
    log "integrity tag ok"
  elif [ "$accept_untagged" -eq 1 ]; then
    log "WARNING: no $(basename "$sums").hmac; restoring set $set_label untagged (--accept-untagged). Only right for a set written before Softwarden 1.0; the checksum proves the files are whole, not that nobody rewrote them"
  else
    die "no $(basename "$sums").hmac next to set $set_label. Every set written by Softwarden 1.0 or later has one, so for such a set this means it was altered or is incomplete: don't restore it. --accept-untagged is only for a set you know was written before 1.0"
  fi
  step="checking the checksum"
  (cd "$(dirname "$backup")" && sha256sum -c "$(basename "$sums")" >/dev/null) \
    || die "checksum mismatch: the backup files are damaged or changed"
  sums_list "$backup" || die "$(basename "$sums") doesn't list $(basename "$backup")"
  log "sha256 ok"
elif [ "$rehearse" -eq 1 ]; then
  die "no $(basename "$sums") next to the backup: a set without its checksum file is incomplete"
elif [ "$accept_untagged" -eq 1 ]; then
  log "WARNING: no .sha256 file next to set $set_label (--accept-untagged); skipping the checksum. Only right for a set written before Softwarden 1.0"
else
  die "no $(basename "$sums") next to set $set_label, so nothing proves the files are whole and unchanged. Every set written by Softwarden 1.0 or later has one: for such a set it was altered or is incomplete. --accept-untagged is only for a set you know was written before 1.0"
fi

step="checking the database service"
dc exec -T db pg_isready -U softwarden -d postgres >/dev/null \
  || die "the db service is not running or not ready (docker compose up -d db)"

step="checking the backup decrypts"
check_archive "$backup" \
  || die "cannot decrypt the backup with this key, or it is not a pg_dump archive"
log "backup decrypts and lists correctly"

check_media=0
if [ "$want_media" -eq 1 ] && { [ "$db_name" = "softwarden" ] || [ "$rehearse" -eq 1 ]; }; then
  if [ -f "$media_enc" ]; then
    # Before anything is stopped or replaced: the archive must be covered by the checksum file (checked above; tar
    # alone accepts an archive cut off between two entries), and must decrypt in full and read as a tar.
    step="checking the media archive's checksum"
    sums_list "$media_enc" \
      || die "$(basename "$sums") doesn't list $(basename "$media_enc"), so a cut-off archive can't be told apart; nothing was changed (--no-media restores the database alone)"
    step="checking the media archive decrypts"
    aes_decrypt_stdout "$media_enc" | tar -tf - >/dev/null \
      || die "cannot decrypt $(basename "$media_enc") with this key, or it is not a readable tar; nothing was changed"
    check_media=1
    log "media archive decrypts and lists correctly"
  elif [ "$rehearse" -eq 1 ]; then
    die "no $(basename "$media_enc") next to the backup; --no-media rehearses the database alone"
  else
    log "WARNING: no $(basename "$media_enc") next to the backup (made with --no-media, or by a version that didn't back up attachments); attachment files will be left as they are"
  fi
fi

if [ "$rehearse" -eq 1 ]; then
  [ -f "$manifest_enc" ] || die "no manifest next to the backup ($(basename "$manifest_enc")); a rehearsal checks against it"
  step="decrypting the manifest"
  aes_decrypt "$manifest_enc" "$SCRATCH/manifest.json"
fi

if [ "$db_name" = "softwarden" ]; then
  step="stopping app and worker"
  if dc ps --status running --services | grep -qx app; then
    dc stop app worker >/dev/null
    stopped_app=1
    log "app and worker stopped"
  fi
fi

if [ "$rehearse" -eq 1 ]; then
  # A rehearsal drops its scratch database, so it may only reuse its own default one, never another existing database.
  step="checking the scratch database name"
  exists=$(dc exec -T db psql -U softwarden -d postgres -tA -v ON_ERROR_STOP=1 \
    -c "SELECT 1 FROM pg_database WHERE datname = '$db_name'")
  if [ -n "$exists" ] && [ "$db_name" != "softwarden_rehearsal" ]; then
    die "a database named $db_name already exists; a rehearsal drops its scratch database, so pick a new --database name"
  fi
fi

step="recreating database $db_name"
if [ "$rehearse" -eq 1 ]; then scratch_db="$db_name"; fi  # dropped on exit, pass or fail
dc exec -T db psql -U softwarden -d postgres -v ON_ERROR_STOP=1 -q \
  -c "DROP DATABASE IF EXISTS \"$db_name\" WITH (FORCE)" \
  -c "CREATE DATABASE \"$db_name\" OWNER softwarden" >/dev/null
log "database $db_name recreated"

step="restoring with pg_restore"
aes_decrypt_stdout "$backup" | dc exec -T db pg_restore -U softwarden -d "$db_name" --no-owner --exit-on-error
log "pg_restore finished into $db_name"

# The rehearsal: every check in one-off app containers pointed at the scratch database (POSTGRES_DB), so the running
# app and worker are untouched. The media archive is unpacked into a throwaway volume of its container and checked there.
if [ "$rehearse" -eq 1 ]; then
  rehearse_run() { dc run --rm --no-deps -T -e POSTGRES_DB="$db_name" "$@"; }
  if [ "$migrate" -eq 1 ]; then
    step="migrating the scratch database"
    log "running migrate on $db_name (output follows)"
    rehearse_run app python manage.py migrate --noinput
  fi
  step="checking migrations (migrate --check)"
  rehearse_run app python manage.py migrate --check \
    || die "the restored database has unapplied migrations for this image (a backup of an older release needs --migrate)"
  log "migrations are all applied"
  step="checking the manifest (upgrade_check --manifest)"
  log "running upgrade_check --manifest on $db_name (output follows)"
  rehearse_run --entrypoint sh app -c 'cat > /tmp/manifest.json && exec python manage.py upgrade_check --manifest /tmp/manifest.json' \
    < "$SCRATCH/manifest.json" \
    || die "upgrade_check failed: the restored database does not match the manifest"
  if [ "$check_media" -eq 1 ]; then
    step="verifying attachment files from the media archive"
    log "running verify_attachments against the unpacked media archive (output follows)"
    # Into a throwaway volume at /data/rehearsal (the image's empty folder, owned by the app user), removed with the
    # container: the app's root filesystem is read-only and its /tmp a small tmpfs.
    aes_decrypt_stdout "$media_enc" \
      | rehearse_run -v /data/rehearsal -e SOFTWARDEN_MEDIA_ROOT=/data/rehearsal/media --entrypoint sh app -c \
        'set -e; tar -C /data/rehearsal -xf -; exec python manage.py verify_attachments' \
      || die "verify_attachments found attachment files missing from or changed in the media archive"
  else
    log "WARNING: attachment files not checked (--no-media)"
  fi
  step="checking the stored secrets open with this stack's credential key"
  check_stored_secrets rehearse_run app \
    || die "the set restores, but its stored secrets can't be read with this stack's credential key (see above)"
  step="dropping the scratch database"
  log "rehearsal passed: $(basename "$backup") restores, migrates, matches its manifest and its attachment files, and its stored secrets open"
  exit 0
fi

# Attachment files: only with the default database (a test database must not replace the live files), and while the
# app and worker are stopped, in one-off app containers (as the app user, uid 10001, which owns the volume).
# 1. Extract into a staging directory inside the volume. The pipeline fails (pipefail) if openssl or tar fails, and
#    then the current files are untouched; a leftover .incoming is cleared by the next attempt (the sweep only walks
#    attachments/, so it is never mistaken for attachment files).
# 2. Only then swap: remove everything but .incoming, move the staged files into place, drop .incoming. A move within
#    one volume keeps each file's owner and mode; tar -p kept the archived modes.
if [ "$check_media" -eq 1 ] && [ "$rehearse" -eq 0 ] && [ "$db_name" = "softwarden" ]; then
  step="staging attachment files"
  aes_decrypt_stdout "$media_enc" \
    | dc run --rm --no-deps -T --entrypoint sh app -c \
      'set -e; rm -rf /data/media/.incoming; mkdir -m 700 /data/media/.incoming; tar -C /data/media/.incoming -xpf -; test -d /data/media/.incoming/media'
  log "attachment files staged"
  step="replacing attachment files"
  dc run --rm --no-deps -T --entrypoint sh app -c \
    'set -e; cd /data/media; find . -mindepth 1 -maxdepth 1 ! -name .incoming -exec rm -rf {} +; find .incoming/media -mindepth 1 -maxdepth 1 ! -name .incoming -exec mv {} . \;; rm -rf .incoming'
  log "attachment files restored from $(basename "$media_enc")"
fi

if [ "$stopped_app" -eq 1 ]; then
  # The app no longer migrates as it starts (compose.yaml's one-shot migrate service does):
  # a set from an older release gets this image's migrations here, before the app serves it.
  step="$migrate_step"
  log "running the migrate service (output follows)"
  dc run --rm --no-deps -T migrate
  step="starting app and worker"
  dc start app worker >/dev/null
  stopped_app=0
  log "app and worker started"
fi

if [ "$want_manifest" -eq 0 ]; then
  log "manifest check skipped (--no-manifest)"
  log "restore complete"
  exit 0
fi
if [ "$db_name" != "softwarden" ]; then
  log "manifest check skipped: the app checks its configured database, not $db_name (--rehearse checks a scratch database)"
  log "restore complete"
  exit 0
fi

step="checking the manifest"
[ -f "$manifest_enc" ] || die "no manifest next to the backup ($(basename "$manifest_enc")); rerun with --no-manifest to skip, but then nothing proves the restore"
aes_decrypt "$manifest_enc" "$SCRATCH/manifest.json"
log "waiting for the app to be healthy"
health=""
for _ in $(seq 1 36); do
  health=$(dc ps --format '{{.Health}}' app 2>/dev/null || true)
  [ "$health" = "healthy" ] && break
  sleep 5
done
[ "$health" = "healthy" ] || die "the app did not become healthy within 3 minutes (docker compose logs app)"
dc exec -T app sh -c 'cat > /tmp/restored-manifest.json' < "$SCRATCH/manifest.json"
log "running upgrade_check --manifest (output follows)"
rc=0
dc exec -T app python manage.py upgrade_check --manifest /tmp/restored-manifest.json || rc=$?
dc exec -T app rm -f /tmp/restored-manifest.json || true
if [ "$rc" -ne 0 ]; then die "upgrade_check failed (exit $rc): the restore does not match the manifest"; fi
step="verifying attachment files"
log "running verify_attachments (output follows)"
dc exec -T app python manage.py verify_attachments \
  || die "verify_attachments found missing or changed attachment files (see above)"
step="checking the stored secrets open with this stack's credential key"
check_stored_secrets dc exec -T app \
  || die "the data is restored, but its stored secrets can't be read until the old credential key is configured (see above)"
log "restore complete; the manifest check, the attachment check and the stored-secrets check passed"
