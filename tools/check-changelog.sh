#!/usr/bin/env bash
set -euo pipefail

# Refuse a pull request whose merge could not be released: changelog.d must hold
# at least one typed fragment, and pyproject.toml exactly one version line.
#
# A Dependabot pull request that changes nothing but uv.lock needs none: it
# changes no published package's requirements, and tools/update-version.sh
# releases nothing for a merge without fragments.
#
# Environment:
#   PR_AUTHOR  the login of the pull request's author
#   BASE_REF   the branch it merges into, fetched as origin/<BASE_REF>
if [[ "${PR_AUTHOR:-}" == "dependabot[bot]" ]]; then
  : "${BASE_REF:?BASE_REF is required}"
  changed="$(git diff --name-only "origin/${BASE_REF}...HEAD")"
  if [[ "${changed}" == "uv.lock" ]]; then
    echo "Dependabot's pull request changes only uv.lock, which releases nothing; it needs no fragment."
    exit 0
  fi
fi
uv run --no-project python tools/bump-version.py --check
