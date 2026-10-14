#!/usr/bin/env bash
set -euo pipefail

# Release the Python distribution: bump pyproject.toml from the pending fragment
# types, collect the fragments into CHANGELOG.md, and record the emitter's own
# new version in uv.lock for `uv sync --locked`.
version="$(uv run --no-project python tools/bump-version.py)"
uvx --from "towncrier==26.9.0" towncrier build --yes --version "$version"
uv lock
