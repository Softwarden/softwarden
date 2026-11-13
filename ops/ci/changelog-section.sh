#!/usr/bin/env bash
# Print one release's notes from CHANGELOG.md, for the GitHub Release body (.github/workflows/release.yml).
#
#   ops/ci/changelog-section.sh <version> [CHANGELOG.md]
#
# <version> is 1.0.0 or v1.0.0. The section is the heading "## [1.0.0]" or "## 1.0.0" (optionally followed by
# " - <date>") up to the next "## " heading, without the heading itself and without leading or trailing blank lines.
# Exits 1, printing nothing, when there is no such section or it is empty: a tag without written notes isn't released.
set -euo pipefail

[ $# -ge 1 ] && [ $# -le 2 ] || { echo "usage: $0 <version> [CHANGELOG.md]" >&2; exit 2; }
version="${1#v}"
file="${2:-CHANGELOG.md}"
[ -f "$file" ] || { echo "no $file" >&2; exit 1; }

notes=$(awk -v v="$version" '
  function is_heading(line,   rest) {
    if (line !~ /^## /) return 0
    rest = substr(line, 4)
    sub(/^\[/, "", rest)
    return index(rest, v) == 1 && substr(rest, length(v) + 1) ~ /^(\]|[ \t]|$)/
  }
  /^## / { if (inside) exit; if (is_heading($0)) { inside = 1; next } }
  inside { print }
' "$file" | sed -e '/./,$!d')  # drops leading blank lines; $( ) drops the trailing ones

if [ -z "$(printf '%s' "$notes" | tr -d '[:space:]')" ]; then
  echo "CHANGELOG has no notes for $version (add a \"## [$version] - YYYY-MM-DD\" section)" >&2
  exit 1
fi
printf '%s\n' "$notes"
