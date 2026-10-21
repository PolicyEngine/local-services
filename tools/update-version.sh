#!/usr/bin/env bash
set -euo pipefail

# Release both Python distributions: bump both pyproject.toml files and the
# emitter's interface pin from the pending fragment types, collect the
# fragments into CHANGELOG.md, and record both new versions in uv.lock for
# `uv sync --locked`.
version="$(uv run --no-project python tools/bump-version.py)"
uvx --from "towncrier==26.9.0" towncrier build --yes --version "$version"
uv lock
