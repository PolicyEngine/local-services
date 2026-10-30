#!/usr/bin/env bash
set -euo pipefail

# Release both Python distributions: bump both pyproject.toml files and the
# emitter's interface pin from the pending fragment types, collect the
# fragments into CHANGELOG.md, and record both new versions in uv.lock for
# `uv sync --locked`.
#
# A merge without fragments, such as a Dependabot pull request that changes
# only uv.lock, releases nothing: the script changes no file, and push.yaml
# then makes no version commit, so nothing is tagged or published.
#
# Environment:
#   GITHUB_OUTPUT  receives released: true when there is a release to commit

released() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "released=$1" >> "${GITHUB_OUTPUT}"
  fi
}

if [[ -z "$(find changelog.d -type f ! -name .gitkeep -print -quit)" ]]; then
  echo "No changelog fragment: this merge releases nothing."
  released false
  exit 0
fi
version="$(uv run --no-project python tools/bump-version.py)"
uvx --from "towncrier==26.9.0" towncrier build --yes --version "$version"
uv lock
released true
