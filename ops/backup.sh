#!/usr/bin/env bash
# Nightly encrypted backup of the Softwarden database and attachment files.
#
#   BACKUP_DEST=/mnt/backups BACKUP_KEY_FILE=/etc/softwarden/backup.key ops/backup.sh [--no-manifest]
#
# Run from the Compose project directory, or set SOFTWARDEN_DIR. Settings come from the environment or from
# ops/backup.env (the environment wins).
#
#   BACKUP_DEST         where backups go: a share or folder your organization provides          required
#   BACKUP_KEY_FILE     the encryption key file; outside the project directory, mode 600        required
#   SOFTWARDEN_DIR      the Compose project directory                                           default: current directory
#   BACKUP_KEEP_DAILY   daily backups kept                                                      default: 14
#   BACKUP_KEEP_WEEKLY  Sunday backups kept                                                     default: 8
#   BACKUP_DATE         YYYY-MM-DD to stamp the files with; for testing retention only
#
# --no-manifest skips the upgrade_check manifest (for testing without the app container). A real backup needs it.
# --no-media skips the attachment files (the softwarden-media volume). A real backup needs them.
# Nothing readable is written to disk: pg_dump and the media tar stream straight into openssl, and the manifest is
# encrypted from a private scratch directory that is removed on exit. The key is never printed.
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source=ops/common.sh
. "$script_dir/common.sh"

want_manifest=1
want_media=1
for arg in "$@"; do
  case "$arg" in
    --no-manifest) want_manifest=0 ;;
    --no-media) want_media=0 ;;
    -h|--help) sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $arg (try --help)" ;;
  esac
done

step="starting"
part_file=""
on_exit() {
  local rc=$?
  cleanup_scratch
  if [ -n "$part_file" ]; then rm -f -- "${part_file:?}"; fi
  if [ "$rc" -ne 0 ]; then log "FAILED during: $step (exit $rc)" >&2; fi
  exit "$rc"
}
trap on_exit EXIT

load_config "$script_dir"
KEEP_DAILY="${BACKUP_KEEP_DAILY:-14}"
KEEP_WEEKLY="${BACKUP_KEEP_WEEKLY:-8}"
[[ "$KEEP_DAILY" =~ ^[0-9]+$ && "$KEEP_DAILY" -ge 1 ]] || die "BACKUP_KEEP_DAILY must be a positive number"
[[ "$KEEP_WEEKLY" =~ ^[0-9]+$ && "$KEEP_WEEKLY" -ge 1 ]] || die "BACKUP_KEEP_WEEKLY must be a positive number"

step="checking settings"
[ -n "${BACKUP_DEST:-}" ] || die "BACKUP_DEST is not set"
enter_project
check_key_file
mkdir -p "$BACKUP_DEST/daily" "$BACKUP_DEST/weekly"
if [ ! -w "$BACKUP_DEST/daily" ] || [ ! -w "$BACKUP_DEST/weekly" ]; then die "BACKUP_DEST is not writable: $BACKUP_DEST"; fi
dest=$(abs_dir "$BACKUP_DEST")
case "$dest/" in
  "$PROJECT_DIR"/*) die "BACKUP_DEST is inside the project directory; use a separate disk or share" ;;
esac
day="${BACKUP_DATE:-$(date +%F)}"
[[ "$day" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "BACKUP_DATE must look like 2026-11-03"
weekday=$(date -d "$day" +%u)
name="softwarden-$day"
daily="$dest/daily"
weekly="$dest/weekly"
log "settings ok: project $PROJECT_DIR, destination $dest, key file checked"

make_scratch

step="checking the database service"
dc exec -T db pg_isready -U softwarden -d softwarden >/dev/null || die "the db service is not running or not ready"
log "database is up"

# The manifest goes first: it records what must survive, as of just before the dump.
if [ "$want_manifest" -eq 1 ]; then
  step="writing the upgrade_check manifest"
  dc exec -T app python manage.py upgrade_check --write-manifest /tmp/manifest.json >/dev/null \
    || die "upgrade_check --write-manifest failed (is the app service running? use --no-manifest only for tests)"
  dc exec -T app cat /tmp/manifest.json > "$SCRATCH/manifest.json"
  dc exec -T app rm -f /tmp/manifest.json
  [ -s "$SCRATCH/manifest.json" ] || die "the manifest is empty"
  step="encrypting the manifest"
  part_file="$daily/$name.manifest.json.enc.part"
  aes_encrypt "$SCRATCH/manifest.json" "$part_file"
  mv -f "$part_file" "$daily/$name.manifest.json.enc"
  part_file=""
  log "manifest written and encrypted: $name.manifest.json.enc"
else
  log "WARNING: manifest skipped (--no-manifest); this backup cannot be checked after a restore"
fi

step="dumping the database"
part_file="$daily/$name.dump.enc.part"
dc exec -T db pg_dump -U softwarden -Fc softwarden \
  | aes_encrypt_stdin "$part_file"
[ -s "$part_file" ] || die "the encrypted dump is empty"
log "pg_dump encrypted: $(wc -c < "$part_file" | tr -d ' ') bytes"

step="verifying the encrypted dump"
check_archive "$part_file" \
  || die "the encrypted dump does not decrypt to a readable archive"
mv -f "$part_file" "$daily/$name.dump.enc"
part_file=""
log "dump decrypts and lists correctly"

write_checksums() { # <directory>
  local d="$1" files=("$name.dump.enc")
  if [ -f "$d/$name.manifest.json.enc" ]; then files+=("$name.manifest.json.enc"); fi
  if [ -f "$d/$name.media.tar.enc" ]; then files+=("$name.media.tar.enc"); fi
  (cd "$d" && sha256sum "${files[@]}" > "$name.sha256")
  write_sums_tag "$d/$name.sha256"  # the integrity tag under the backup key (ops/common.sh)
}

# The dump's checksum first, so a failure in the media step can't leave a good dump without one. An archive left by an
# earlier run of the same day is removed first, so it can't be paired with this dump.
step="writing checksums"
rm -f -- "$daily/$name.media.tar.enc"
write_checksums "$daily"
log "sha256 written: $name.sha256"

# Attachment files, taken right after the dump: a deleted attachment's file stays on the volume for the retention
# window (SOFTWARDEN_ATTACHMENT_RETENTION_DAYS), so every file this dump names is in this archive. A one-off container
# of the app image reads the volume as the app user, whether or not the app is running; tar streams into openssl.
# GNU tar exits 1 when a file changed while it was read (an upload during the backup): that file is in the archive as
# it was, which is fine, so 1 counts as success and only 2 or more fails the step. media/exports (organization data
# exports and tenant backups, rebuilt from the database and attachments) is left out, as the worker's backup does.
if [ "$want_media" -eq 1 ]; then
  step="archiving attachment files"
  part_file="$daily/$name.media.tar.enc.part"
  dc run --rm --no-deps -T --entrypoint sh app -c \
    'tar --warning=no-file-changed --exclude=media/.incoming --exclude=media/exports -C /data -cf - media; rc=$?; [ "$rc" -le 1 ] || exit "$rc"' \
    | aes_encrypt_stdin "$part_file"
  [ -s "$part_file" ] || die "the encrypted media archive is empty"
  step="verifying the encrypted media archive"
  aes_decrypt_stdout "$part_file" | tar -tf - >/dev/null \
    || die "the encrypted media archive does not decrypt to a readable tar"
  mv -f "$part_file" "$daily/$name.media.tar.enc"
  part_file=""
  step="writing checksums with the media archive"
  write_checksums "$daily"
  log "attachment files archived and encrypted: $name.media.tar.enc (sha256 updated)"
else
  log "WARNING: attachment files skipped (--no-media); a restore of this backup has no attachment files"
fi

if [ "$weekday" = "7" ]; then
  step="copying to weekly"
  cp -f "$daily/$name.dump.enc" "$weekly/"
  if [ -f "$daily/$name.manifest.json.enc" ]; then cp -f "$daily/$name.manifest.json.enc" "$weekly/"; fi
  if [ -f "$daily/$name.media.tar.enc" ]; then cp -f "$daily/$name.media.tar.enc" "$weekly/"; fi
  write_checksums "$weekly"
  log "Sunday: copied to weekly"
fi

# Keep the newest N dumps (by the date in the file name); drop the manifest and checksum of every dump removed.
prune() { # <directory> <keep> <label>
  local d="$1" keep="$2" label="$3" f stem removed=0
  while IFS= read -r f; do
    stem="${f%.dump.enc}"
    rm -f -- "${stem:?}.dump.enc" "${stem:?}.manifest.json.enc" "${stem:?}.media.tar.enc" "${stem:?}.sha256" "${stem:?}.sha256.hmac"
    removed=$((removed + 1))
  done < <(find "$d" -maxdepth 1 -name 'softwarden-*.dump.enc' | sort | head -n "-$keep")
  log "pruned $label: removed $removed, keeping up to $keep"
}

step="pruning old backups"
prune "$daily" "$KEEP_DAILY" daily
prune "$weekly" "$KEEP_WEEKLY" weekly

step="done"
log "backup complete: $daily/$name.dump.enc"
