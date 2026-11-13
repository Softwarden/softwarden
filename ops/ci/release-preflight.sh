#!/usr/bin/env bash
# The release workflow's checks (.github/workflows/release.yml). Each subcommand exits
# non-zero with a "::error::" line when the release must not go ahead, so nothing is pushed:
#
#   ops/ci/release-preflight.sh version <tag>
#       The tag is vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-PRE (SemVer 2.0: no leading zeros, dot-separated
#       pre-release identifiers). Prints "version=X.Y.Z" and "prerelease=true|false" for $GITHUB_OUTPUT.
#   ops/ci/release-preflight.sh changelog-date <version> [CHANGELOG.md]
#       CHANGELOG.md has "## [<version>] - YYYY-MM-DD" with a real calendar date that isn't after tomorrow (UTC;
#       RELEASE_TODAY=YYYY-MM-DD overrides today, for tests). The placeholder "unreleased (set on tag day)" fails.
#   ops/ci/release-preflight.sh repo <version> <commit> [<main ref>]
#       <commit> is <main ref> (default origin/main) or one of its ancestors, and the version is the one in
#       apps/api/pyproject.toml ([project] version, PEP 440 spelling accepted: 1.1.0rc1 for 1.1.0-rc.1) and in
#       deploy/helm/softwarden/Chart.yaml (appVersion). Run from the repository root with full history.
#   ops/ci/release-preflight.sh unpublished <image:tag>...
#       None of these exists in the registry yet: a re-run never replaces a published version. An answer other than
#       "not found" (no login, network) fails too, so an unknown state never counts as unpublished. One exception, the
#       first release: before a package exists GHCR may answer "denied" or 403 rather than "not found", so for a
#       ghcr.io reference that answer counts as unpublished only when the GitHub packages API answers 404 for the
#       package under the owner as an organization and as a user (`gh api`, GH_TOKEN with packages: read).
#       RELEASE_IMAGETOOLS and RELEASE_GH override the inspect command (default: docker buildx imagetools inspect)
#       and gh, for tests.
#   ops/ci/release-preflight.sh tags <version>   (the repository's v* tags on stdin, one per line)
#       Prints the tags the images get: <version> always; for a stable version also MAJOR.MINOR when it is the highest
#       stable release of that line, and latest when it is the highest stable release of all. A pre-release gets only
#       its own tag.
set -euo pipefail

fail() { echo "::error::$*" >&2; exit 1; }

NUM='(0|[1-9][0-9]*)'
# One pre-release identifier: alphanumerics and hyphens; a purely numeric one has no leading zero.
is_prerelease() {
  local ident
  local -a idents
  [ -n "$1" ] || return 1
  IFS=. read -r -a idents <<< "$1"
  [ "${1: -1}" != "." ] || return 1
  for ident in "${idents[@]}"; do
    [[ "$ident" =~ ^[0-9A-Za-z-]+$ ]] || return 1
    if [[ "$ident" =~ ^[0-9]+$ ]] && [[ "$ident" =~ ^0[0-9] ]]; then return 1; fi
  done
}

is_version() {
  local v=$1 core pre=""
  core=${v%%-*}
  [[ "$v" == *-* ]] && pre=${v#*-}
  [[ "$core" =~ ^$NUM\.$NUM\.$NUM$ ]] || return 1
  if [[ "$v" == *-* ]]; then is_prerelease "$pre" || return 1; fi
}

is_stable() { [[ "$1" =~ ^$NUM\.$NUM\.$NUM$ ]]; }

# 1.1.0-rc.1 -> 1.1.0rc1, 1.1.0-beta.2 -> 1.1.0b2, 1.1.0-alpha1 -> 1.1.0a1; anything else unchanged.
pep440() {
  local v=$1
  if [[ "$v" =~ ^([0-9]+\.[0-9]+\.[0-9]+)-(alpha|a|beta|b|rc|c)\.?([0-9]+)$ ]]; then
    local kind=${BASH_REMATCH[2]}
    case $kind in alpha) kind=a ;; beta) kind=b ;; c) kind=rc ;; esac
    echo "${BASH_REMATCH[1]}${kind}${BASH_REMATCH[3]}"
  else
    echo "$v"
  fi
}

cmd_version() {
  [ $# -eq 1 ] || fail "usage: $0 version <tag>"
  local tag=$1 version
  [[ "$tag" == v* ]] || fail "$tag isn't a version tag (vX.Y.Z or vX.Y.Z-pre)"
  version=${tag#v}
  is_version "$version" || fail "$tag isn't a version tag (vX.Y.Z or vX.Y.Z-pre, no leading zeros)"
  echo "version=$version"
  if [[ "$version" == *-* ]]; then echo "prerelease=true"; else echo "prerelease=false"; fi
}

cmd_changelog_date() {
  [ $# -ge 1 ] && [ $# -le 2 ] || fail "usage: $0 changelog-date <version> [CHANGELOG.md]"
  local version=$1 file=${2:-CHANGELOG.md} line date today limit
  [ -f "$file" ] || fail "no $file"
  line=$(grep -F -- "## [$version] - " "$file" | head -n 1 || true)
  [[ "$line" =~ ^"## ["(.*)"] - "([0-9]{4}-[0-9]{2}-[0-9]{2})[[:space:]]*$ ]] && [ "${BASH_REMATCH[1]}" = "$version" ] \
    || fail "CHANGELOG.md has no '## [$version] - YYYY-MM-DD' heading: set the release date"
  date=${BASH_REMATCH[2]}
  [ "$(date -u -d "$date" +%F 2>/dev/null || true)" = "$date" ] || fail "CHANGELOG.md's date for $version, $date, isn't a calendar date"
  today=${RELEASE_TODAY:-$(date -u +%F)}
  limit=$(date -u -d "$today + 1 day" +%F)
  [[ ! "$date" > "$limit" ]] || fail "CHANGELOG.md's date for $version, $date, is in the future"
}

cmd_repo() {
  [ $# -ge 2 ] && [ $# -le 3 ] || fail "usage: $0 repo <version> <commit> [<main ref>]"
  local version=$1 commit=$2 main=${3:-origin/main} py chart
  git rev-parse --verify --quiet "$main^{commit}" > /dev/null || fail "$main isn't known here (fetch with full history)"
  git merge-base --is-ancestor "$commit" "$main" \
    || fail "the tagged commit $commit isn't on $main: release only commits that are on main"
  py=$(awk '/^\[/{inside = ($0 == "[project]")} inside && /^version *=/ {gsub(/^version *= *"|" *$/, ""); print; exit}' \
    apps/api/pyproject.toml)
  [ "$py" = "$version" ] || [ "$py" = "$(pep440 "$version")" ] \
    || fail "apps/api/pyproject.toml says version $py, the tag says $version"
  chart=$(sed -n -E 's/^appVersion: *"?([^"]*)"? *$/\1/p' deploy/helm/softwarden/Chart.yaml | head -n 1)
  [ "$chart" = "$version" ] || fail "deploy/helm/softwarden/Chart.yaml says appVersion $chart, the tag says $version"
}

# Does the GitHub packages API say there is no container package for ghcr.io/<owner>/<name>[:tag]? Both the
# organization and the user form must answer 404 (the owner is one or the other).
package_absent() {
  local ref=$1 path owner name kind out
  local -a gh
  read -r -a gh <<< "${RELEASE_GH:-gh}"
  [[ "$ref" == ghcr.io/*/* ]] || return 1
  path=${ref#ghcr.io/}
  path=${path%%[:@]*}
  owner=${path%%/*}
  name=${path#*/}
  name=${name//\//%2F}
  for kind in orgs users; do
    if out=$("${gh[@]}" api "/$kind/$owner/packages/container/$name" 2>&1); then return 1; fi
    printf '%s' "$out" | grep -qE 'HTTP 404|Not Found' || return 1
  done
}

cmd_unpublished() {
  [ $# -ge 1 ] || fail "usage: $0 unpublished <image:tag>..."
  local ref out
  local -a inspect
  read -r -a inspect <<< "${RELEASE_IMAGETOOLS:-docker buildx imagetools inspect}"
  for ref in "$@"; do
    if out=$("${inspect[@]}" "$ref" 2>&1); then
      fail "$ref is already published: a version is never replaced (tag a new patch version instead)"
    fi
    if printf '%s' "$out" | grep -qiE 'not found|manifest unknown|name unknown'; then
      echo "$ref: not published yet"
    elif package_absent "$ref"; then
      echo "$ref: not published yet (no such package: the first release)"
    else
      fail "can't tell whether $ref exists: $out"
    fi
  done
}

# Is $1 strictly greater than $2? Both MAJOR.MINOR.PATCH.
newer() {
  local a b i
  IFS=. read -r -a a <<< "$1"
  IFS=. read -r -a b <<< "$2"
  for i in 0 1 2; do
    if (( 10#${a[i]} > 10#${b[i]} )); then return 0; fi
    if (( 10#${a[i]} < 10#${b[i]} )); then return 1; fi
  done
  return 1
}

cmd_tags() {
  [ $# -eq 1 ] || fail "usage: $0 tags <version> < tag list"
  local version=$1 tag other line_highest=1 highest=1
  is_version "$version" || fail "$version isn't a version"
  echo "$version"
  is_stable "$version" || return 0
  while IFS= read -r tag; do
    tag=${tag%$'\r'}
    other=${tag#v}
    if [[ "$tag" != v* ]] || ! is_stable "$other"; then continue; fi
    if newer "$other" "$version"; then
      highest=0
      [ "${other%.*}" = "${version%.*}" ] && line_highest=0
    fi
  done
  if [ $line_highest -eq 1 ]; then echo "${version%.*}"; fi
  if [ $highest -eq 1 ]; then echo "latest"; fi
}

[ $# -ge 1 ] || fail "usage: $0 version|changelog-date|repo|unpublished|tags ..."
command=$1; shift
case $command in
  version) cmd_version "$@" ;;
  changelog-date) cmd_changelog_date "$@" ;;
  repo) cmd_repo "$@" ;;
  unpublished) cmd_unpublished "$@" ;;
  tags) cmd_tags "$@" ;;
  *) fail "unknown subcommand $command" ;;
esac
