#!/usr/bin/env bash

# Start a new relock branch from the main checkout, named for this moment in
# UTC, and record its name. Each run proposes its relock on a branch of its
# own, so no run rewrites a branch someone may have pushed to.
#
# Environment:
#   GITHUB_OUTPUT  receives branch: the new branch's name

set -euo pipefail

: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"

branch="bot/update-dependencies/$(date -u +%Y-%m-%d-%H%M%S)"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git checkout -b "${branch}"
echo "branch=${branch}" >> "${GITHUB_OUTPUT}"
