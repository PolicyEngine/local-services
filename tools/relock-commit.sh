#!/usr/bin/env bash

# Commit the relocked uv.lock on the relock branch, with the relock's
# changelog fragment, since every pull request adds one. When the relock
# changed nothing, commit nothing.
#
# Environment:
#   GITHUB_OUTPUT  receives changes_detected: true when there is a relock to
#                  propose

set -euo pipefail

: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"

if [[ -z "$(git status --porcelain)" ]]; then
  echo "uv.lock is already up to date"
  echo "changes_detected=false" >> "${GITHUB_OUTPUT}"
  exit 0
fi

mkdir -p changelog.d
echo "Updated the locked dependencies in \`uv.lock\` to the newest versions the requirements allow." \
  > changelog.d/update-dependencies.changed.md
git add --all
git commit -m "Update locked dependencies ($(date -u +%Y-%m-%d))"
echo "changes_detected=true" >> "${GITHUB_OUTPUT}"
