#!/usr/bin/env bash

# Rebuild the update-dependencies branch from main, and record the commit the
# remote branch points at, so the push replaces exactly that commit.
#
# Every run starts from main, even while a relock pull request is open:
# merging main into an older update branch would merge two resolutions of
# uv.lock line by line, which uv cannot read.
#
# Environment:
#   GITHUB_OUTPUT  receives remote_head: the remote branch's commit, or empty
#                  when the branch does not exist. git reads the remote with
#                  the credentials the checkout stored.

set -euo pipefail

: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"

branch="update-dependencies"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

remote_head="$(git ls-remote --heads origin "refs/heads/${branch}" | cut -f1)"
if [[ -n "${remote_head}" ]]; then
  # The push step compares the rebuilt branch with this commit.
  git fetch --quiet origin "${remote_head}"
fi

git checkout -B "${branch}" origin/main
echo "remote_head=${remote_head}" >> "${GITHUB_OUTPUT}"
