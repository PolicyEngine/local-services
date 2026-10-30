#!/usr/bin/env bash

# After the relock changed files on main, make sure an open relock pull
# request exists and that the first line of its description names its
# tracking issue, as every pull request's must.
#
# The tracking issue is the oldest open issue titled "Update locked
# dependencies", or a new one when none is open. "Fixes #N" closes it when the
# pull request merges, so the next relock gets a new issue.
#
# - Without an open relock pull request, open one.
# - With one, keep it, and replace its description when it differs from the
#   current one. Each run rebuilds the branch from main, so the description is
#   the workflow's, not a reviewer's.
#
# Either way the description is "Fixes #N", a blank line and the summary.
#
# Environment:
#   GITHUB_REPOSITORY  owner/name of this repository (set by the runner)
#   GH_TOKEN           read by gh: a token that can read and write issues and
#                      pull requests

set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

BRANCH="update-dependencies"
ISSUE_TITLE="Update locked dependencies"
ISSUE_BODY="Tracks the automated weekly relock from update-dependencies.yaml. The relock pull request names this issue on the first line of its description and closes it when it merges."

# Set ISSUE_NUMBER to the oldest open tracking issue, opening one when none is
# open. The issues API lists pull requests too; they have a pull_request
# field. This sets a variable rather than printing, because bash turns off
# set -e inside $(...), and a failed lookup must not open an issue.
find_or_open_tracking_issue() {
  local open_issues
  local issue_number
  local issue_url

  open_issues="$(
    gh api --paginate \
      "repos/${GITHUB_REPOSITORY}/issues?state=open&per_page=100" \
      --jq ".[] | select(.pull_request == null and .title == \"${ISSUE_TITLE}\") | .number"
  )"
  if [[ -n "${open_issues}" ]]; then
    issue_number="$(sort -n <<<"${open_issues}")"
    issue_number="${issue_number%%$'\n'*}"
  else
    issue_url="$(
      gh issue create \
        --repo "${GITHUB_REPOSITORY}" \
        --title "${ISSUE_TITLE}" \
        --body "${ISSUE_BODY}"
    )"
    issue_number="${issue_url##*/}"
  fi

  if [[ ! "${issue_number}" =~ ^[0-9]+$ ]]; then
    echo "Could not resolve the relock tracking issue: ${issue_number}" >&2
    exit 1
  fi
  ISSUE_NUMBER="${issue_number}"
}

# Print the relock pull request's description, naming the tracking issue first.
relock_description() {
  printf 'Fixes #%s\n\n' "${ISSUE_NUMBER}"
  cat <<'DESCRIPTION'
Automated weekly relock by `.github/workflows/update-dependencies.yaml`. `uv lock --upgrade` updates `uv.lock` to the newest versions `pyproject.toml` allows, so this pull request changes only `uv.lock` and adds a changelog fragment. CI on this pull request tests the upgraded versions.

Each run rebuilds `update-dependencies` from `main` and replaces it, so commits pushed to this branch by hand are lost on the next run.
DESCRIPTION
}

# --head matches a branch of the same name in a fork too; only this
# repository's branch is the relock branch.
open_pull_request="$(
  gh pr list \
    --repo "${GITHUB_REPOSITORY}" \
    --base main \
    --head "${BRANCH}" \
    --state open \
    --json number,isCrossRepository \
    --jq 'map(select(.isCrossRepository | not)) | .[0].number // empty'
)"
ISSUE_NUMBER=""
find_or_open_tracking_issue
description="$(relock_description)"

if [[ -z "${open_pull_request}" ]]; then
  gh pr create \
    --repo "${GITHUB_REPOSITORY}" \
    --title "${ISSUE_TITLE}" \
    --body-file - \
    --base main \
    --head "${BRANCH}" \
    <<<"${description}"
  exit 0
fi

current_description="$(
  gh pr view "${open_pull_request}" \
    --repo "${GITHUB_REPOSITORY}" \
    --json body \
    --jq '.body'
)"
if [[ "${current_description}" == "${description}" ]]; then
  echo "Pull request #${open_pull_request} already has the current description."
  exit 0
fi

gh pr edit "${open_pull_request}" \
  --repo "${GITHUB_REPOSITORY}" \
  --body-file - \
  <<<"${description}"
echo "Pull request #${open_pull_request} now names tracking issue #${ISSUE_NUMBER}."
