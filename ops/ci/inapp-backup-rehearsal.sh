#!/usr/bin/env bash
# The worker's own backup, restored: on a running Compose stack, seed demo data and one
# attachment file, take a set with `manage.py backup_now --inline` in the worker (apps/api/core/backup.py, the real
# pg_dump 17 in the image), copy the set and the generated backup key out as the backup runbook says, then prove it
# with `ops/restore.sh --rehearse`: restore into a scratch database, migrate --check, upgrade_check --manifest against
# the set's own manifest, verify_attachments against its media archive, and the stored-secrets check. Nothing live
# changes. CI's compose-first-run job runs it after the stack is up; run it locally the same way:
#
#   docker compose up -d --build --wait && ops/ci/inapp-backup-rehearsal.sh
#
# It acts on the Compose project the environment names (COMPOSE_PROJECT_NAME, COMPOSE_FILE), else this repository's
# directory, like ops/restore.sh. The stack's data gains seed_demo's records: use a throwaway stack. Exits non-zero
# unless the rehearsal passed and checked at least one attachment file and the manifest.
set -euo pipefail
exec </dev/null

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
repo=$(cd "$script_dir/../.." && pwd -P)
# shellcheck source=ops/common.sh
. "$repo/ops/common.sh"  # log, die, dc
cd "$repo"
export SOFTWARDEN_DIR="$repo"

# A path docker.exe understands (C:/... under Git Bash; unchanged on Linux).
native() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

work=$(mktemp -d "${TMPDIR:-/tmp}/sw-inapp-backup.XXXXXX")  # outside the project: restore.sh refuses a key inside it
chmod 700 "$work"
trap 'rm -rf -- "${work:?}"' EXIT

CHECK_TEXT="Softwarden in-app backup rehearsal attachment"
SEED_PY="
from django.core.files.uploadedfile import SimpleUploadedFile
from catalog import attachments
from catalog.models import Resource
app = Resource.objects.filter(kind='application').order_by('name').first()
assert app is not None, 'seed_demo made no application'
upload = SimpleUploadedFile('inapp-backup-check.txt', b'$CHECK_TEXT\n', content_type='text/plain')
attachments.upload_file(app.organization, None, app, upload, {'title': 'In-app backup rehearsal'})
print('seeded an attachment on', app.name)
"

log "=== seeding demo data and one attachment file"
# The demo's connector is on the simulated tenant, which a production-mode stack allows only with this flag.
dc exec -T -e SOFTWARDEN_ALLOW_SIMULATED=1 app python manage.py seed_demo
dc exec -T app python manage.py shell -c "$SEED_PY"

log "=== backup_now --inline in the worker"
dc exec -T worker python manage.py backup_now --inline
set_name=$(dc exec -T worker sh -c 'ls -1 /data/backups/daily | grep "^softwarden-" | sort | tail -n 1' | tr -d '\r')
[ -n "$set_name" ] || die "the worker published no set under /data/backups/daily"
log "set: $set_name"

log "=== copying the set and the backup key off the stack (backup runbook, Restoring a worker set)"
dc cp "worker:/data/backups/daily/$set_name" "$(native "$work")/"
dc cp worker:/data/secrets/backup_key "$(native "$work/backup.key")"
chmod 600 "$work/backup.key"
ls -l "$work/$set_name"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) export SOFTWARDEN_ALLOW_NOACL_KEY=1 ;;  # NTFS keeps no modes (ops/common.sh check_key_file)
esac

log "=== ops/restore.sh --rehearse"
if ! BACKUP_KEY_FILE="$work/backup.key" bash "$repo/ops/restore.sh" --rehearse "$work/$set_name/$set_name.dump.enc" \
  2>&1 | tee "$work/rehearsal.log"; then
  die "the rehearsal failed (see above)"
fi

# The rehearsal passing isn't enough: it must have checked something.
files=$(sed -n 's/.*All \([0-9][0-9]*\) attachment files are present and match.*/\1/p' "$work/rehearsal.log" | tail -n 1)
[ "${files:-0}" -ge 1 ] || die "the rehearsal checked no attachment file"
grep -q "Audit events intact" "$work/rehearsal.log" || die "the rehearsal didn't check the set's manifest"
grep -q "integrity tag ok" "$work/rehearsal.log" || die "the rehearsal didn't check the set's integrity tag"
log "PASSED: the worker's set $set_name restores; $files attachment file(s) and the manifest checked"
