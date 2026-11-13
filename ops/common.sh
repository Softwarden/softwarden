#!/usr/bin/env bash
# Shared by backup.sh and restore.sh. Sourced, not run.
# Needs: bash, docker with the Compose plugin, openssl, sha256sum, GNU date/stat/head.

# docker compose, with Git Bash's path rewriting (/tmp/x -> C:/...) switched off for docker only; harmless on Linux.
dc() { MSYS_NO_PATHCONV=1 docker compose "$@"; }

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# Load ops/backup.env if present, without letting it override variables already set in the environment. The Compose
# variables are kept too: a caller that aims these scripts at one project (ops/ci/restore-previous.sh) must never be
# redirected to another, such as the host's real stack, by a backup.env that names it.
load_config() {
  local script_dir="$1" saved v
  saved=$(for v in SOFTWARDEN_DIR BACKUP_DEST BACKUP_KEY_FILE BACKUP_KEEP_DAILY BACKUP_KEEP_WEEKLY \
    COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_PROFILES; do
    if [ -n "${!v+x}" ]; then printf 'export %s=%q\n' "$v" "${!v}"; fi
  done)
  if [ -f "$script_dir/backup.env" ]; then
    # shellcheck disable=SC1091
    . "$script_dir/backup.env"
  fi
  eval "$saved"
  SOFTWARDEN_DIR="${SOFTWARDEN_DIR:-$PWD}"
}

# Absolute, symlink-resolved path of an existing directory.
abs_dir() { (cd "$1" 2>/dev/null && pwd -P); }

enter_project() {
  [ -d "$SOFTWARDEN_DIR" ] || die "SOFTWARDEN_DIR is not a directory: $SOFTWARDEN_DIR"
  cd "$SOFTWARDEN_DIR" || die "cannot enter $SOFTWARDEN_DIR"
  if [ ! -f compose.yaml ] && [ ! -f docker-compose.yml ]; then
    die "no compose.yaml in $SOFTWARDEN_DIR (run from the Compose project directory or set SOFTWARDEN_DIR)"
  fi
  PROJECT_DIR=$(pwd -P)
}

# The key file must exist, be readable, not be readable by group/others, and live outside the project directory.
check_key_file() {
  [ -n "${BACKUP_KEY_FILE:-}" ] || die "BACKUP_KEY_FILE is not set"
  [ -f "$BACKUP_KEY_FILE" ] || die "key file not found: $BACKUP_KEY_FILE"
  [ -r "$BACKUP_KEY_FILE" ] || die "key file is not readable: $BACKUP_KEY_FILE"
  [ -s "$BACKUP_KEY_FILE" ] || die "key file is empty: $BACKUP_KEY_FILE"
  # openssl and the integrity tag use the first line only; an empty one would be a key of nothing.
  [ -n "$(backup_key_line)" ] || die "key file's first line is empty: $BACKUP_KEY_FILE"
  local mode key_dir
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
      # Git Bash on Windows (a developer machine, ops/ci/restore-previous.sh): NTFS is mounted noacl there, so chmod
      # does nothing and stat reports 644 for every file. The mode can't be checked; the folder's Windows ACL is what
      # protects the key. Skipping the check is opt-in (SOFTWARDEN_ALLOW_NOACL_KEY=1, which restore-previous.sh sets),
      # so a script run by hand there doesn't silently accept a key it can't vouch for. Servers are Linux, where the
      # check below always applies.
      [ "${SOFTWARDEN_ALLOW_NOACL_KEY:-}" = "1" ]         || die "Windows (Git Bash) keeps no file modes, so the key file's permissions can't be checked; set SOFTWARDEN_ALLOW_NOACL_KEY=1 to accept that (the folder's Windows ACL must protect the key)"
      log "WARNING: Windows (Git Bash) doesn't keep file modes, so the key file's permissions aren't checked (SOFTWARDEN_ALLOW_NOACL_KEY=1)" >&2 ;;
    *)
      mode=$(stat -c %a "$BACKUP_KEY_FILE")
      case "$mode" in
        *00) ;;
        *) die "key file permissions are $mode; group and others must have none (chmod 600 $BACKUP_KEY_FILE)" ;;
      esac ;;
  esac
  key_dir=$(abs_dir "$(dirname "$BACKUP_KEY_FILE")") || die "cannot resolve the key file directory"
  case "$key_dir/" in
    "$PROJECT_DIR"/*) die "key file is inside the project directory ($PROJECT_DIR); keep it outside, and off the host too" ;;
  esac
}

# The -pass argument for openssl. Under Git Bash a "file:/tmp/..." value is not translated for a native openssl, so
# convert it there; on Linux this is just file:$BACKUP_KEY_FILE.
key_pass_arg() {
  if command -v cygpath >/dev/null 2>&1; then printf 'file:%s' "$(cygpath -m "$BACKUP_KEY_FILE")"; else printf 'file:%s' "$BACKUP_KEY_FILE"; fi
}

# openssl with fixed parameters.
aes_encrypt() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass "$(key_pass_arg)" -in "$1" -out "$2"; }
aes_decrypt() { openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass "$(key_pass_arg)" -in "$1" -out "$2"; }
aes_decrypt_stdout() { openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass "$(key_pass_arg)" -in "$1"; }
aes_encrypt_stdin() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass "$(key_pass_arg)" -out "$1"; }

# The integrity tag. openssl enc's CBC mode has no authentication, and the .sha256 file
# beside a set can be rewritten by anyone who can write the share, so `<set>.sha256.hmac` holds
# "HMAC-SHA256 <hex>": an HMAC-SHA256 of the .sha256 file's bytes under a key derived from the backup key,
# SHA-256("softwarden-backup-sums-hmac" NUL <the key file's first line, trailing CR/LF removed>), the line openssl uses.
# Only someone with the backup key can write a tag that matches. apps/api/core/backup.py writes the same tag.
# Computed with bash builtins and sha256sum on pipes: the key never appears on a command line.
TAG_LABEL="HMAC-SHA256"
backup_key_line() {
  local line=""
  IFS= read -r line < "$BACKUP_KEY_FILE" || [ -n "$line" ]
  while [[ "$line" == *$'\r' ]]; do line=${line%$'\r'}; done
  printf '%s' "$line"
}
hex_to_escapes() { # <hex> -> printf escapes for its bytes, in REPLY
  local hex="$1" i out=""
  for ((i = 0; i < ${#hex}; i += 2)); do out+="\\x${hex:i:2}"; done
  REPLY="$out"
}
sums_tag() { # <file>: prints the tag's hex for the file's bytes
  local line key ipad="" opad="" i b esc inner
  line=$(backup_key_line)
  [ -n "$line" ] || return 1  # an empty key line, refused as core/backup.check_key refuses it
  key=$({ printf 'softwarden-backup-sums-hmac\0'; printf '%s' "$line"; } | sha256sum | cut -c1-64)
  for ((i = 0; i < 64; i++)); do
    if [ "$i" -lt 32 ]; then b=$((16#${key:2*i:2})); else b=0; fi
    printf -v esc '\\x%02x' $((b ^ 0x36)); ipad+="$esc"
    printf -v esc '\\x%02x' $((b ^ 0x5c)); opad+="$esc"
  done
  # shellcheck disable=SC2059  # the escapes are the format on purpose
  inner=$({ printf "$ipad"; cat -- "$1"; } | sha256sum | cut -c1-64)
  hex_to_escapes "$inner"
  # shellcheck disable=SC2059
  { printf "$opad"; printf "$REPLY"; } | sha256sum | cut -c1-64
}
write_sums_tag() { # <sums file>: writes <sums file>.hmac
  local tag
  tag=$(sums_tag "$1") || return 1
  [ ${#tag} -eq 64 ] || return 1
  printf '%s %s\n' "$TAG_LABEL" "$tag" > "$1.hmac.part" && mv -f "$1.hmac.part" "$1.hmac"
}
check_sums_tag() { # <sums file>: 0 when <sums file>.hmac matches it under this key
  local want got label
  [ -f "$1.hmac" ] || return 1
  read -r label want < "$1.hmac" || return 1
  [ "$label" = "$TAG_LABEL" ] || return 1
  got=$(sums_tag "$1") || return 1
  [ "${#got}" -eq 64 ] && [ "$got" = "$want" ]
}

# A private scratch directory, removed on exit.
make_scratch() {
  SCRATCH=$(mktemp -d)
  chmod 700 "$SCRATCH"
}
cleanup_scratch() {
  if [ -n "${SCRATCH:-}" ] && [ -d "$SCRATCH" ]; then rm -rf -- "${SCRATCH:?}"; fi
  return 0
}

# Prove an encrypted dump is usable without writing it anywhere: a full decrypt (openssl checks the final block, so a
# truncated or corrupted file fails) and a pg_restore table-of-contents listing (only a correct key gives one).
# pg_restore -l stops reading early, so its pipe is allowed to break and the listing text is what is checked.
check_archive() { # <file.dump.enc>
  local toc
  aes_decrypt_stdout "$1" >/dev/null 2>&1 || return 1
  toc=$(aes_decrypt_stdout "$1" 2>/dev/null | dc exec -T db pg_restore -l 2>/dev/null || true)
  case "$toc" in *"TOC Entries"*) return 0 ;; *) return 1 ;; esac
}
