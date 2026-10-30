#!/usr/bin/env bash

# Push the relock branch, open its pull request, and close the earlier relock
# pull requests nobody else pushed to.
#
# - The branch is new, so the push is a plain push: if a branch of that name
#   exists, the push fails and nothing else happens.
# - The description's first line is "Fixes #N", naming the oldest open issue
#   titled "Update locked dependencies", or a new one when none is open, as
#   every pull request's must. "Fixes #N" closes it when the pull request
#   merges, so the next relock gets a new issue.
# - An earlier relock pull request, from a bot/update-dependencies/ branch of
#   this repository into main, is closed with a comment naming the new one
#   when its branch holds only the relock's own commit. When the branch holds
#   any other commit, it stays open, and the new description lists it.
#
# Environment:
#   GITHUB_REPOSITORY  owner/name of this repository (set by the runner)
#   BRANCH             the branch relock-prepare-branch.sh started
#   GH_TOKEN           read by gh, and by git through the credential helper
#                      below, for this one push: a token that can push
#                      branches and write issues and pull requests

set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${BRANCH:?BRANCH is required}"

BRANCH_PREFIX="bot/update-dependencies/"
TITLE="Update locked dependencies"
ISSUE_BODY="Tracks the automated weekly relock from update-dependencies.yaml. The relock pull request names this issue on the first line of its description and closes it when it merges."

if [[ "${BRANCH}" != "${BRANCH_PREFIX}"* ]]; then
  echo "${BRANCH} is not a relock branch (${BRANCH_PREFIX}...)" >&2
  exit 1
fi

# The helper hands git the token from the environment; it is never written
# to git's configuration.
# shellcheck disable=SC2016  # git's shell, not this one, expands ${GH_TOKEN}
git -c credential.helper= \
  -c 'credential.helper=!f() { echo username=x-access-token; echo "password=${GH_TOKEN}"; }; f' \
  push origin "HEAD:refs/heads/${BRANCH}"

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
      --jq ".[] | select(.pull_request == null and .title == \"${TITLE}\") | .number"
  )"
  if [[ -n "${open_issues}" ]]; then
    issue_number="$(sort -n <<<"${open_issues}")"
    issue_number="${issue_number%%$'\n'*}"
  else
    issue_url="$(
      gh issue create \
        --repo "${GITHUB_REPOSITORY}" \
        --title "${TITLE}" \
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

ISSUE_NUMBER=""
find_or_open_tracking_issue

# Each earlier relock pull request as "<number> <superseded|kept>": superseded
# when its branch holds one commit, the relock's own.
earlier="$(
  gh pr list \
    --repo "${GITHUB_REPOSITORY}" \
    --base main \
    --state open \
    --limit 100 \
    --json number,headRefName,isCrossRepository,commits \
    --jq ".[]
      | select((.isCrossRepository | not) and (.headRefName | startswith(\"${BRANCH_PREFIX}\")))
      | \"\(.number) \(if (.commits | length) == 1 and (.commits[0].messageHeadline | startswith(\"${TITLE} (\")) then \"superseded\" else \"kept\" end)\""
)"
superseded=()
kept=()
while read -r number fate; do
  [[ -n "${number}" ]] || continue
  if [[ "${fate}" == "superseded" ]]; then
    superseded+=("${number}")
  else
    kept+=("#${number}")
  fi
done <<<"${earlier}"

description="$(
  printf 'Fixes #%s\n\n' "${ISSUE_NUMBER}"
  cat <<'DESCRIPTION'
Automated weekly relock by `.github/workflows/update-dependencies.yaml`. `uv lock --upgrade` updates `uv.lock` to the newest versions `pyproject.toml` allows, so this pull request changes only `uv.lock` and adds a changelog fragment. CI on this pull request tests the upgraded versions.

Each run opens a new relock pull request from a branch of its own, and closes the earlier ones unless someone pushed to their branches.
DESCRIPTION
  if ((${#kept[@]} > 0)); then
    kept_list="$(IFS=,; echo "${kept[*]}")"
    printf '\nEarlier relock pull requests stay open, because their branches hold commits the relock did not make: %s.\n' "${kept_list//,/, }"
  fi
)"

pull_request_url="$(
  gh pr create \
    --repo "${GITHUB_REPOSITORY}" \
    --title "${TITLE}" \
    --body-file - \
    --base main \
    --head "${BRANCH}" \
    <<<"${description}"
)"
pull_request="${pull_request_url##*/}"
echo "Opened #${pull_request} from ${BRANCH}."

for number in ${superseded[@]+"${superseded[@]}"}; do
  gh pr close "${number}" \
    --repo "${GITHUB_REPOSITORY}" \
    --comment "Superseded by #${pull_request}, which relocks uv.lock from the current main."
done
