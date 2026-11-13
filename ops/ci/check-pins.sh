#!/usr/bin/env bash
# Fails when anything the build or CI runs is named by a tag someone else can move. CI's
# deploy-config job runs it; run it locally the same way:
#
#   ops/ci/check-pins.sh [repository root]
#
# Checked:
#   - every `uses:` in .github/workflows/*.yml names a full 40-character commit SHA with the version in a comment
#     (`uses: actions/checkout@<sha>  # v4.4.0`); a local `./` workflow or action is fine;
#   - every `image:` in the workflows and in compose*.yaml carries a digest (`postgres:17.11-trixie@sha256:...`),
#     except the Softwarden images the operator chooses (`${SOFTWARDEN_IMAGE...}`, `${SOFTWARDEN_MCP_IMAGE...}`);
#   - every FROM and COPY --from in Dockerfile and mcp/Dockerfile names an image by digest, or an earlier build stage;
#   - every `uvx` tool in the workflows has a version (`uvx pip-audit@2.10.1`);
#   - every apt source (`deb ...`) in the workflows and Dockerfiles names its signing key with `signed-by=`, and no
#     signing key is fetched at run time (`apt-key add`, `add-apt-repository`, a download piped into gpg or apt-key,
#     or a curl/wget of a key file: .asc, .gpg, a keyring or keys/ path): the keys are
#     committed (docker/pgdg/ACCC4CF8.asc, its fingerprint checked by apps/api/tests/test_backup_stack.py), so a
#     moved or replaced key can't slip in (the api job's postgresql-client-17).
# Dependabot (.github/dependabot.yml) proposes the updates; this script only refuses what isn't pinned.
set -euo pipefail

root=${1:-.}
cd "$root"
problems=0
problem() { echo "::error::$*"; problems=$((problems + 1)); }

shopt -s nullglob
workflows=(.github/workflows/*.yml .github/workflows/*.yaml)

for file in "${workflows[@]}"; do
  while IFS= read -r hit; do
    line=${hit%%:*}; text=${hit#*:}
    ref=$(printf '%s' "$text" | sed -E 's/^[[:space:]]*(-[[:space:]]*)?uses:[[:space:]]*//; s/[[:space:]]*#.*$//; s/^["'\'']//; s/["'\'']$//')
    case $ref in
      ./*) continue ;;
    esac
    if ! [[ "$ref" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$ ]]; then
      problem "$file:$line: $ref isn't pinned to a full commit SHA"
    elif ! [[ "$text" =~ \#[[:space:]]*v?[0-9]+(\.[0-9]+)* ]]; then
      problem "$file:$line: $ref has no '# vX.Y.Z' comment naming its version"
    fi
  done < <(grep -nE '^[[:space:]]*(-[[:space:]]*)?uses:' "$file" || true)
  while IFS= read -r hit; do
    problem "$file:${hit%%:*}: uvx tool without a version: ${hit#*:}"
  done < <(grep -nE '^[^#]*(^|[[:space:]])uvx[[:space:]]+[A-Za-z0-9_.-]+([[:space:]]|$)' "$file" || true)
done

# A service or job container's `image:` key; `- image:` starts a matrix entry (release.yml's image names), not one.
for file in "${workflows[@]}" compose*.yaml compose*.yml; do
  while IFS= read -r hit; do
    line=${hit%%:*}; text=${hit#*:}
    image=$(printf '%s' "$text" | sed -E 's/^[[:space:]]*image:[[:space:]]*//; s/[[:space:]]+#.*$//; s/^["'\'']//; s/["'\'']$//')
    case $image in
      "\${SOFTWARDEN_IMAGE"* | "\${SOFTWARDEN_MCP_IMAGE"*) continue ;;
    esac
    [[ "$image" =~ @sha256:[0-9a-f]{64}$ ]] || problem "$file:$line: image $image has no @sha256: digest"
  done < <(grep -nE '^[[:space:]]*image:[[:space:]]*[^[:space:]]' "$file" || true)
done

for file in Dockerfile mcp/Dockerfile; do
  [ -f "$file" ] || continue
  stages=" "  # the build stages named so far (FROM ... AS <name>), and their numbers
  count=0
  while IFS= read -r hit; do
    line=${hit%%:*}; text=${hit#*:}
    read -r -a words <<< "$text"
    ref=""
    if [ "${words[0]^^}" = FROM ]; then
      for word in "${words[@]:1}"; do
        case $word in --*) ;; *) ref=$word; break ;; esac
      done
      name=$count
      if [ ${#words[@]} -ge 3 ] && [ "${words[-2]^^}" = AS ]; then name="$count ${words[-1]}"; fi
      count=$((count + 1))
    else
      for word in "${words[@]:1}"; do
        case $word in --from=*) ref=${word#--from=} ;; esac
      done
      name=""
    fi
    if [ "$ref" != scratch ] && [[ "$stages" != *" $ref "* ]] && ! [[ "$ref" =~ @sha256:[0-9a-f]{64}$ ]]; then
      problem "$file:$line: $ref has no @sha256: digest"
    fi
    [ -z "$name" ] || stages+="$name "
  done < <(grep -niE '^[[:space:]]*(FROM[[:space:]]|COPY[[:space:]].*--from=)' "$file" || true)
done

# apt sources and their keys, wherever a package repository is added.
for file in "${workflows[@]}" Dockerfile mcp/Dockerfile; do
  [ -f "$file" ] || continue
  while IFS= read -r hit; do
    case ${hit#*:} in
      *signed-by=*) ;;
      *) problem "$file:${hit%%:*}: apt source without signed-by= (name a key committed in the repository)" ;;
    esac
  done < <(grep -nE '^[^#]*(^|["'\''[:space:]])deb[[:space:]]+(\[|https?://)' "$file" || true)
  while IFS= read -r hit; do
    problem "$file:${hit%%:*}: signing key fetched at run time; commit it and name it with signed-by=: ${hit#*:}"
  # apt-key add; a download piped into gpg or apt-key; a download of a key file (.asc, .gpg, a keyring or keys/ path,
  # saved with -o and named in signed-by=); and add-apt-repository, which fetches the PPA's key itself.
  done < <(grep -nE '^[^#]*(apt-key[[:space:]]+add|add-apt-repository|(curl|wget)[^|#]*\|[[:space:]]*(sudo[[:space:]]+)?(gpg|apt-key)|(curl|wget)[^|#]*(\.asc|\.gpg|keyring|/keys?/))' "$file" || true)
done

if [ "$problems" -gt 0 ]; then
  echo "$problems unpinned reference(s): pin each to a commit SHA or an image digest (Dependabot keeps them current)" >&2
  exit 1
fi
echo "every action, image and uvx tool is pinned, and every apt source names a committed key"
