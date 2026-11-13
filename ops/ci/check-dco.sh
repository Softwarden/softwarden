#!/usr/bin/env bash
# Fails when a commit in a range has no Developer Certificate of Origin sign-off
# (CONTRIBUTING.md, "Sign your commits"). CI's dco job runs it on every pull request's commits; run it locally on
# your branch before you push:
#
#   bash ops/ci/check-dco.sh <base> [head]     for example: bash ops/ci/check-dco.sh origin/main HEAD
#
# Every commit in base..head that is not a merge must carry a trailer `Signed-off-by: Name <email>` whose email is
# the commit author's (case-insensitive), as `git commit -s` writes it. A commit with a sign-off by someone else only
# (a co-author's, a bot's) fails: the author certifies their own contribution. Exit 1 lists each failing commit.
set -euo pipefail

base=${1:?usage: check-dco.sh <base> [head]}
head=${2:-HEAD}
problems=0

while IFS= read -r sha; do
  [ -n "$sha" ] || continue
  author=$(git log -1 --format='%ae' "$sha" | tr '[:upper:]' '[:lower:]')
  subject=$(git log -1 --format='%s' "$sha")
  signoffs=$(git log -1 --format='%(trailers:key=Signed-off-by,valueonly)' "$sha" | tr '[:upper:]' '[:lower:]')
  if [ -z "$signoffs" ]; then
    echo "::error::${sha:0:12} \"$subject\" has no Signed-off-by line (commit with git commit -s)"
    problems=$((problems + 1))
  elif ! printf '%s\n' "$signoffs" | grep -qF "<$author>"; then
    echo "::error::${sha:0:12} \"$subject\" is not signed off by its author <$author>"
    problems=$((problems + 1))
  fi
done < <(git rev-list --no-merges "$base..$head")

if [ "$problems" -gt 0 ]; then
  echo "DCO: $problems commit(s) without the author's sign-off. Fix with: git rebase --signoff $base"
  exit 1
fi
echo "DCO: every commit in $base..$head is signed off by its author."
