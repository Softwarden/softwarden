#!/usr/bin/env bash
# Upgrade rehearsal from the previous release: a backup of the previous tagged release restores and migrates on
# this commit. CI's `migrate-from-previous` job runs it;
# run it locally the same way from anywhere in the repository:
#
#   ops/ci/restore-previous.sh [--ref REF] [--keep-images]
#
# 1. Picks the previous release: the newest v* tag that isn't this commit or after it. Until the first release is
#    tagged there is none, and the rehearsal skips (exit 0, with a notice). --ref overrides it.
# 2. Checks it out with `git worktree` into a temporary folder and builds its image; builds this checkout's image
#    (uncommitted changes included).
# 3. Starts the previous release as its own Compose project (sw-restore-prev-<id>: its own containers and volumes,
#    never the default project's), with seed_demo data plus one attachment file and one encrypted credential (a
#    Teams address), a second organization with its own Admin, a notification rule named like a built-in this release
#    adds, a webhook endpoint and a queued job, lists every record, contract and membership, and backs it up
#    with that release's ops/backup.sh.
# 4. Starts this image as a second project (sw-restore-cur-<id>), rehearses the set into a scratch database
#    (ops/restore.sh --rehearse --migrate), restores it for real (ops/restore.sh --yes: the migrate service migrates the restored
#    database as it starts), then runs migrate, upgrade_check --manifest, verify_attachments (the seeded file is
#    there and matches), decrypts the seeded credential with the carried keys, checks the second organization (the
#    built-in renamed around its rule, the webhook's secrets, the job still queued and runnable) and that no record,
#    contract or membership was lost or moved, and runs doctor against it.
# 5. Removes both projects with their volumes (by project name only), the worktree, the temporary folder and the two
#    image tags it made (--keep-images keeps the tags). Exits non-zero if any step failed, after a summary.
#
# Needs bash, git, docker with Compose v2, openssl and GNU coreutils. Linux (CI), and Git Bash on Windows with Docker
# Desktop (this script sets SOFTWARDEN_ALLOW_NOACL_KEY=1 there, so ops/common.sh skips the key file's mode check:
# NTFS keeps no Unix modes). A previous release whose compose.yaml still publishes its database (before
# the container hardening) publishes it on 127.0.0.1 port RESTORE_PREV_DB_PORT (default 55434); this commit's publishes
# none, so RESTORE_CUR_DB_PORT (55435) is only a guard against a clash. Caddy isn't started, so ports 80 and 443 are
# left alone. Never reads stdin.
set -euo pipefail
exec </dev/null  # nothing below may wait on a terminal: every step reads files or pipes it is given

# What the previous release is seeded with beyond seed_demo, and checked after the restore.
# Synthetic values: the address is under e2e.test, a name that never resolves. Uses
# only APIs the previous release has (catalog.attachments.upload_file, core.credentials.slot_set and slot_value).
CHECK_NAME="Restore rehearsal check"
CHECK_URL="https://restore.e2e.test/workflows/RESTORE-REHEARSAL-0001"
CHECK_FILE_TEXT="Softwarden restore rehearsal attachment"
SEED_PY="
from django.core.files.uploadedfile import SimpleUploadedFile
from catalog import attachments
from catalog.models import Resource
from core import credentials
from notify.models import TeamsDestination
app = Resource.objects.filter(kind='application').order_by('name').first()
assert app is not None, 'seed_demo made no application'
org = app.organization
upload = SimpleUploadedFile('restore-check.txt', b'$CHECK_FILE_TEXT\n', content_type='text/plain')
attachments.upload_file(org, None, app, upload, {'title': '$CHECK_NAME'})
teams = TeamsDestination(organization=org, name='$CHECK_NAME', url_host='restore.e2e.test')
credentials.slot_set(teams, 'url', '$CHECK_URL')
teams.save()
print('seeded an attachment and a credential in', org.slug)
"
CHECK_PY="
from django.core.files.storage import default_storage
from catalog.models import Attachment
from core import credentials
from notify.models import TeamsDestination
found = Attachment.objects.get(title='$CHECK_NAME')
with default_storage.open(found.storage_name) as f:
    assert f.read() == b'$CHECK_FILE_TEXT\n', 'the attachment file differs'
teams = TeamsDestination.objects.get(name='$CHECK_NAME')
assert credentials.slot_value(teams, 'url') == '$CHECK_URL', 'the credential decrypted to something else'
print('the seeded attachment and credential came through the restore')
"

# More than one small demo organization: a second organization with its own Admin, a
# notification rule named like a built-in this release adds (the migration that seeds it must rename the built-in, not
# stop or touch the organization's rule), a webhook endpoint with an encrypted address and signing secret, and a job
# queued under the previous release for a month from now (still queued, and a kind this release still runs). The new
# built-ins come from this checkout's notify/rule_services.BUILTINS (read from the current image), less the ones the
# previous release already gives a new organization; when there are none, no clash is seeded and the check says so.
SECOND_ORG="Restore rehearsal second organization"
SECOND_ADMIN="second-admin@restore.e2e.test"
HOOK_URL="https://hooks.restore.e2e.test/softwarden/RESTORE-REHEARSAL-0002"
HOOK_SECRET="restore-rehearsal-signing-secret-0002"
QUEUED_KIND="notify.deliver"
BUILTINS_PY='
import ast, json
tree = ast.parse(open("notify/rule_services.py", encoding="utf-8").read())
found = []
for node in tree.body:
    target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
    if isinstance(target, ast.Name) and target.id == "BUILTINS":
        for item in node.value.elts:
            key, name = item.elts[0], item.elts[1]
            if isinstance(key, ast.Constant) and isinstance(name, ast.Constant):
                found.append([key.value, name.value])
print(json.dumps(found))
'
SEED_MORE_PY="
import json, os
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.utils import timezone
from core import credentials, jobs
from core.models import Membership, Organization, Role
from notify.models import NotificationRule, WebhookDestination
other = Organization.objects.create(name='$SECOND_ORG')
admin = get_user_model().objects.create_user(username='$SECOND_ADMIN', email='$SECOND_ADMIN')
Membership.objects.create(organization=other, user=admin, role=Role.ADMIN)
rules = NotificationRule.objects.filter(organization=other)
have = set(rules.values_list('builtin_key', flat=True))
taken = {n.lower() for n in rules.values_list('name', flat=True)}
new = [(k, n) for k, n in json.loads(os.environ['SW_BUILTINS']) if k not in have and n.lower() not in taken]
if new:
    key, name = new[0]
    NotificationRule.objects.create(organization=other, name=name, kind='reminders', source_types=['custom_date'],
                                    lead_times_days=[14], escalation_days=[])
    print('a rule named like the new built-in', key, repr(name))
else:
    print('this upgrade adds no built-in rule: no clash seeded')
hook = WebhookDestination(organization=other, name='$CHECK_NAME', url_host='hooks.restore.e2e.test', events=['record.*'])
credentials.slot_set(hook, 'url', '$HOOK_URL')
credentials.slot_set(hook, 'signing_secret', '$HOOK_SECRET')
hook.save()
job = jobs.enqueue('$QUEUED_KIND', {'rehearsal': 'queued before the upgrade'}, organization=other,
                   run_after=timezone.now() + timedelta(days=30))
print('seeded', other.slug, 'with an Admin, a webhook endpoint and queued job', job.id)
"
CHECK_MORE_PY="
import json, os
from core import credentials, jobs
from core.models import Job, Membership, Organization, Role
from notify.models import NotificationRule, WebhookDestination
other = Organization.objects.get(name='$SECOND_ORG')
assert Membership.objects.filter(organization=other, user__email='$SECOND_ADMIN', role=Role.ADMIN).exists(), 'its Admin'
names = {k: n for k, n in json.loads(os.environ['SW_BUILTINS'])}
mine = NotificationRule.objects.filter(organization=other, builtin_key='', name__in=names.values())
for rule in mine:
    key = next(k for k, n in names.items() if n == rule.name)
    builtin = NotificationRule.objects.get(organization=other, builtin_key=key)
    assert builtin.name.startswith(rule.name + ' (built-in'), ('the built-in', key, 'is named', builtin.name)
    assert rule.kind == 'reminders' and list(rule.source_types) == ['custom_date'], 'the organization rule changed'
    print('the organization rule', repr(rule.name), 'kept its name; the new built-in is', repr(builtin.name))
if not mine:
    print('no clashing rule was seeded (this upgrade adds no built-in rule)')
for key in names:
    assert NotificationRule.objects.filter(organization=other, builtin_key=key).exists(), ('built-in missing', key)
hook = WebhookDestination.objects.get(organization=other, name='$CHECK_NAME')
assert credentials.slot_value(hook, 'url') == '$HOOK_URL', 'the webhook address decrypted to something else'
assert credentials.slot_value(hook, 'signing_secret') == '$HOOK_SECRET', 'the signing secret decrypted to something else'
assert list(hook.events) == ['record.*'], hook.events
queued = Job.objects.get(organization=other, kind='$QUEUED_KIND')
assert queued.status == 'queued' and queued.payload == {'rehearsal': 'queued before the upgrade'}, queued.status
assert '$QUEUED_KIND' in jobs.JOB_KINDS, 'this release no longer runs the queued kind'
print('the second organization, its Admin, rules, webhook endpoint and queued job came through the upgrade')
"
# Every record, contract and membership of the previous release, by key, and checked after the upgrade: none may be
# lost or moved to another organization. The previous release's own manifest may predate these parts (upgrade_check
# writes them), so the rehearsal takes this list itself. With SW_ROWS=check it reads that list from stdin.
ROWS_PY="
import json, os, sys
from catalog.models import Resource
from core.models import Membership
from money.models import Contract
def rows(qs, *fields):
    return sorted([str(v) for v in row] for row in qs.values_list('id', *fields))
now = {
    'records': rows(Resource.objects, 'organization_id', 'kind'),
    'contracts': rows(Contract.objects, 'organization_id'),
    'memberships': rows(Membership.objects, 'organization_id', 'user_id'),
}
if os.environ.get('SW_ROWS') != 'check':
    print('ROWS ' + json.dumps(now))
else:
    before = json.loads(sys.stdin.read())
    for part, old in before.items():
        have = {tuple(r) for r in now[part]}
        lost = [r[0] for r in old if tuple(r) not in have]
        assert not lost, (part, len(lost), 'lost or moved', lost[:5])
        print(part, 'intact:', len(old), 'before,', len(now[part]), 'now')
"

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
repo=$(git -C "$script_dir" rev-parse --show-toplevel)
# shellcheck source=ops/common.sh
. "$repo/ops/common.sh"  # log, die, dc (docker compose), aes_decrypt

ref=""
keep_images=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) shift; [ $# -gt 0 ] || die "--ref needs a tag or commit"; ref="$1" ;;
    --keep-images) keep_images=1 ;;
    -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

# A path docker.exe understands (C:/... under Git Bash; unchanged on Linux).
native() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

run_id="$(date -u +%Y%m%d%H%M%S)-$$"
prev_project="sw-restore-prev-$run_id"
cur_project="sw-restore-cur-$run_id"
prev_port="${RESTORE_PREV_DB_PORT:-55434}"
cur_port="${RESTORE_CUR_DB_PORT:-55435}"
work=""
prev_dir=""
prev_image=""
cur_image="softwarden-restore:cur-$run_id"
prev_up=0
cur_up=0
started_at=$(date +%s)
summary=()
step=""
step_started=0

# Compose in one project: the directory holds its compose.yaml; nothing else (an override, a profile, another project
# name in the shell) leaks in. The current project also gets the previous installation's credential keys.
prev_dc() {
  (cd "$prev_dir" && COMPOSE_PROJECT_NAME="$prev_project" COMPOSE_FILE=compose.yaml COMPOSE_PROFILES="" \
    SOFTWARDEN_IMAGE="$prev_image" POSTGRES_PORT="$prev_port" dc "$@")
}
cur_dc() {
  (cd "$repo" && COMPOSE_PROJECT_NAME="$cur_project" COMPOSE_FILE=compose.yaml COMPOSE_PROFILES="" \
    SOFTWARDEN_IMAGE="$cur_image" POSTGRES_PORT="$cur_port" SOFTWARDEN_CREDENTIAL_KEYS="${carried_keys:-}" dc "$@")
}
# ops/backup.sh and ops/restore.sh call plain `docker compose` from SOFTWARDEN_DIR; these variables aim it. Through
# env, so a caller can add its own NAME=value arguments before the command.
prev_ops() {
  env COMPOSE_PROJECT_NAME="$prev_project" COMPOSE_FILE=compose.yaml COMPOSE_PROFILES="" SOFTWARDEN_IMAGE="$prev_image" \
    POSTGRES_PORT="$prev_port" SOFTWARDEN_DIR="$prev_dir" "$@"
}
cur_ops() {
  env COMPOSE_PROJECT_NAME="$cur_project" COMPOSE_FILE=compose.yaml COMPOSE_PROFILES="" SOFTWARDEN_IMAGE="$cur_image" \
    POSTGRES_PORT="$cur_port" SOFTWARDEN_CREDENTIAL_KEYS="${carried_keys:-}" SOFTWARDEN_DIR="$repo" "$@"
}

begin() {
  step="$1"
  step_started=$(date +%s)
  log "=== $step"
}
done_step() {
  summary+=("PASS  $step ($(( $(date +%s) - step_started ))s)")
  step=""
}

# Removes only what this run created: the two projects (containers, networks, volumes, found by project name), the
# worktree, the temporary folder and the image tags.
remove_project() { # <project> <compose function>
  local name="$1" fn="$2" leftover
  "$fn" down --volumes --remove-orphans --timeout 30 >/dev/null 2>&1 || true
  leftover=$(docker ps -aq --filter "label=com.docker.compose.project=$name")
  if [ -n "$leftover" ]; then
    # shellcheck disable=SC2086
    docker rm -f $leftover >/dev/null 2>&1 || true
  fi
  leftover=$(docker volume ls -q --filter "label=com.docker.compose.project=$name")
  if [ -n "$leftover" ]; then
    # shellcheck disable=SC2086
    docker volume rm -f $leftover >/dev/null 2>&1 || true
  fi
  if [ -n "$(docker volume ls -q --filter "label=com.docker.compose.project=$name")" ]; then
    log "WARNING: volumes of $name are left; remove them with: docker volume ls --filter label=com.docker.compose.project=$name" >&2
  fi
}

on_exit() {
  local rc=$?
  set +e
  if [ -n "$step" ]; then summary+=("FAIL  $step ($(( $(date +%s) - step_started ))s)"); fi
  log "=== tearing down"
  if [ "$cur_up" -eq 1 ]; then
    if [ "$rc" -ne 0 ]; then cur_dc logs --no-color --tail 80 migrate app worker 2>&1 | sed 's/^/  current | /'; fi
    remove_project "$cur_project" cur_dc
  fi
  if [ "$prev_up" -eq 1 ]; then
    if [ "$rc" -ne 0 ]; then prev_dc logs --no-color --tail 80 app 2>&1 | sed 's/^/  previous | /'; fi
    remove_project "$prev_project" prev_dc
  fi
  if [ -n "$prev_dir" ] && [ -d "$prev_dir" ]; then
    git -C "$repo" worktree remove --force "$prev_dir" >/dev/null 2>&1 || true
  fi
  git -C "$repo" worktree prune >/dev/null 2>&1 || true
  if [ -n "$work" ] && [ -d "$work" ]; then rm -rf -- "${work:?}"; fi
  if [ "$keep_images" -eq 0 ]; then
    for image in "$prev_image" "$cur_image"; do
      if [ -n "$image" ]; then docker image rm "$image" >/dev/null 2>&1 || true; fi
    done
  fi
  printf '\n'
  log "=== summary: upgrade rehearsal ${prev_label:-?} -> ${cur_label:-?}"
  local line
  for line in "${summary[@]}"; do printf '  %s\n' "$line"; done
  if [ "$rc" -eq 0 ]; then
    log "PASSED in $(( $(date +%s) - started_at ))s: a backup of ${prev_label:-?} restores and migrates under this image"
  else
    log "FAILED (exit $rc) after $(( $(date +%s) - started_at ))s" >&2
  fi
  exit "$rc"
}
if [ -z "$ref" ]; then
  # Only releases this commit descends from (--merged HEAD: not a tag on another branch, such as a newer hotfix), and
  # not one at this commit (--no-contains HEAD): on the v1.1.0 tag, the previous release is v1.0.0.
  ref=$(git -C "$repo" -c versionsort.suffix=- tag --list 'v*' --sort=-v:refname --merged HEAD --no-contains HEAD | head -n 1)
  if [ -z "$ref" ]; then
    # Before the first release there is nothing to upgrade from. Skip, before anything is built or started.
    notice="no v* release tag before this commit, so there is no previous release to upgrade from; until the first release is tagged, the upgrade rehearsal skips (--ref REF rehearses from a particular commit)"
    if [ -n "${GITHUB_ACTIONS:-}" ]; then printf '::notice title=Upgrade rehearsal skipped::%s\n' "$notice"; fi
    log "SKIPPED: $notice"
    exit 0
  fi
fi
trap on_exit EXIT

begin "choosing the previous release"
command -v docker >/dev/null 2>&1 || die "docker isn't installed"
docker compose version >/dev/null 2>&1 || die "the Docker Compose v2 plugin isn't installed"
command -v openssl >/dev/null 2>&1 || die "openssl isn't installed"
prev_sha=$(git -C "$repo" rev-parse --verify --quiet "$ref^{commit}") \
  || die "can't find $ref (CI: check out with fetch-depth 0 so tags and history are there)"
prev_short=$(git -C "$repo" rev-parse --short "$prev_sha")
prev_label="$ref ($prev_short)"
cur_label="$(git -C "$repo" rev-parse --short HEAD)"
if [ -n "$(git -C "$repo" status --porcelain --untracked-files=no)" ]; then cur_label="$cur_label with uncommitted changes"; fi
prev_image="softwarden-restore:prev-$prev_short-$run_id"
if [ -f "$repo/.env" ]; then log "note: $repo/.env exists and applies to the current project (as it would on a server)"; fi
log "previous release: $prev_label; current: $cur_label; projects $prev_project and $cur_project"
done_step

begin "checking out the previous release"
work=$(mktemp -d "${TMPDIR:-/tmp}/sw-restore-previous.XXXXXX")
chmod 700 "$work"
prev_dir="$work/previous"
git -C "$repo" worktree add --detach --quiet "$prev_dir" "$prev_sha"
# A TLS-inspecting network's roots (ca-certs/README.md) are git-ignored; the old build needs them as much as the new.
for crt in "$repo"/ca-certs/*.crt; do
  if [ -f "$crt" ]; then cp -- "$crt" "$prev_dir/ca-certs/"; fi
done
mkdir -p "$work/backups" "$work/keys" "$work/scratch"
chmod 700 "$work/keys" "$work/scratch"
done_step

begin "checking that every command aims at this run's projects"
# What ops/backup.sh and ops/restore.sh will resolve, through the same common.sh load_config (which reads an
# ops/backup.env), and what this script's own compose calls resolve. Anything else (the host's real stack) stops here.
resolved_name() { # <ops function> <project directory>
  # shellcheck disable=SC2016  # expanded by the inner bash
  "$1" bash -c 'set -euo pipefail; . "$0/ops/common.sh"; load_config "$0/ops"; enter_project; dc config | sed -n "s/^name: //p" | head -n 1' "$2"
}
for dir in "$repo" "$prev_dir"; do
  if [ -f "$dir/ops/backup.env" ]; then log "note: $dir/ops/backup.env exists; its Compose settings are overridden for this run"; fi
done
[ "$(resolved_name prev_ops "$prev_dir")" = "$prev_project" ] || die "the previous release's ops scripts don't resolve to $prev_project"
[ "$(resolved_name cur_ops "$repo")" = "$cur_project" ] || die "this checkout's ops scripts don't resolve to $cur_project"
[ "$(prev_dc config | sed -n 's/^name: //p' | head -n 1)" = "$prev_project" ] || die "compose doesn't resolve to $prev_project"
[ "$(cur_dc config | sed -n 's/^name: //p' | head -n 1)" = "$cur_project" ] || die "compose doesn't resolve to $cur_project"
log "the ops scripts and compose resolve to $prev_project and $cur_project"
done_step

begin "building the previous image ($prev_image)"
docker build --quiet --tag "$prev_image" "$(native "$prev_dir")" >/dev/null
done_step

begin "building the current image ($cur_image)"
docker build --quiet --tag "$cur_image" "$(native "$repo")" >/dev/null
done_step

begin "starting the previous release with demo data"
prev_up=1
prev_dc up --detach --wait --wait-timeout 300 --no-build app worker
# The demo's connector is on the simulated tenant, which a release with the simulated-tenant guard allows only with this flag.
prev_dc exec -T -e SOFTWARDEN_ALLOW_SIMULATED=1 app python manage.py seed_demo
prev_dc exec -T app python manage.py shell -c "$SEED_PY"
done_step

begin "a second organization, a rule named like a new built-in, a webhook endpoint and a queued job"
# The built-ins this release has, read from its own image (no database: the module is parsed, not imported).
builtins_json=$(MSYS_NO_PATHCONV=1 docker run --rm --network none --entrypoint python "$cur_image" -c "$BUILTINS_PY" | tr -d '\r')
log "this release's built-in rules: $builtins_json"
prev_dc exec -T -e SW_BUILTINS="$builtins_json" app python manage.py shell -c "$SEED_MORE_PY"
prev_dc exec -T app python manage.py shell -c "$ROWS_PY" | sed -n 's/^ROWS //p' | tr -d '\r' > "$work/scratch/rows.json"
[ -s "$work/scratch/rows.json" ] || die "couldn't list the previous release's records, contracts and memberships"
done_step

begin "backing up the previous release (its ops/backup.sh)"
# The previous release's own script, which matches its compose.yaml; a release without one (none so far) falls back
# to this checkout's. Both write the same set: softwarden-<date>.dump.enc, .manifest.json.enc, .media.tar.enc, .sha256.
backup_script="$prev_dir/ops/backup.sh"
[ -f "$backup_script" ] || backup_script="$repo/ops/backup.sh"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    # Older releases refuse every key file under Git Bash (NTFS keeps no Unix modes, so it always looks like
    # 644); this checkout's common.sh skips that one check there and is otherwise the same helpers. Linux (CI) runs
    # the previous release's scripts unchanged.
    if [ -f "$prev_dir/ops/common.sh" ]; then cp -- "$repo/ops/common.sh" "$prev_dir/ops/common.sh"; fi
    export SOFTWARDEN_ALLOW_NOACL_KEY=1  # this checkout's common.sh skips the mode check only when asked
    log "Git Bash: the previous release's backup.sh runs with this checkout's ops/common.sh (the key mode check)" ;;
esac
key="$work/keys/backup.key"
openssl rand -base64 48 > "$key"
chmod 600 "$key"
export BACKUP_KEY_FILE="$key"
prev_ops BACKUP_DEST="$work/backups" bash "$backup_script"
set_file=$(find "$work/backups/daily" -maxdepth 1 -name 'softwarden-*.dump.enc' | sort | tail -n 1)
[ -n "$set_file" ] || die "the backup wrote no dump"
log "backup set: $(basename "$set_file")"
# A release before integrity tags writes no <set>.sha256.hmac; restore.sh refuses such a
# set unless told it comes from trusted storage, as an operator restoring an old set would.
untagged=()
if [ ! -f "${set_file%.dump.enc}.sha256.hmac" ]; then
  untagged=(--accept-untagged)
  log "the previous release writes no integrity tag: restoring with --accept-untagged"
fi
done_step

begin "carrying the previous installation's credential keys over"
# A restored installation keeps its credential keys (backup runbook); the current project gets them as
# SOFTWARDEN_CREDENTIAL_KEYS. Held in this process only, never printed.
carried_keys=$(prev_dc exec -T app cat /data/secrets/credential_keys | tr '\n' ',')
[ -n "$carried_keys" ] || die "the previous installation has no generated credential keys"
done_step

begin "stopping the previous release"
prev_dc down --volumes --remove-orphans --timeout 30 >/dev/null 2>&1
prev_up=0
done_step

begin "starting the current image on an empty database"
cur_up=1
cur_dc up --detach --wait --wait-timeout 300 --no-build app worker
done_step

begin "rehearsing the restore (restore.sh --rehearse --migrate)"
cur_ops bash "$repo/ops/restore.sh" "${untagged[@]}" --rehearse --migrate "$set_file"
done_step

begin "restoring for real (restore.sh --yes; it runs the migrate service before the app starts)"
cur_ops bash "$repo/ops/restore.sh" "${untagged[@]}" --yes "$set_file"
done_step

begin "waiting for the app and worker, then migrate"
cur_dc up --detach --wait --wait-timeout 300 --no-build app worker
cur_dc exec -T app python manage.py migrate --noinput
cur_dc exec -T app python manage.py migrate --check
done_step

begin "upgrade_check --manifest against the restored database"
aes_decrypt "${set_file%.dump.enc}.manifest.json.enc" "$work/scratch/manifest.json"
# shellcheck disable=SC2016  # expanded by the container's shell
cur_dc exec -T app sh -c \
  'cat > /tmp/previous-manifest.json; python manage.py upgrade_check --manifest /tmp/previous-manifest.json; rc=$?; rm -f /tmp/previous-manifest.json; exit $rc' \
  < "$work/scratch/manifest.json"
done_step

begin "the seeded attachment and credential (verify_attachments, a decrypt with the carried keys)"
cur_dc exec -T app python manage.py verify_attachments
cur_dc exec -T app python manage.py shell -c "$CHECK_PY"
done_step

begin "the second organization, the renamed built-in, the webhook endpoint and the queued job"
cur_dc exec -T -e SW_BUILTINS="$builtins_json" app python manage.py shell -c "$CHECK_MORE_PY"
done_step

begin "every record, contract and membership of the previous release is still there, in its organization"
cur_dc exec -T -e SW_ROWS=check app python manage.py shell -c "$ROWS_PY" < "$work/scratch/rows.json"
done_step

begin "doctor"
cur_dc exec -T app python manage.py doctor
done_step
