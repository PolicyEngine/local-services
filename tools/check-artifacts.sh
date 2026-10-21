#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
# Build each distribution's wheel from its source archive to check that the
# libraries, Alembic migrations and protocol fixtures are included. Remove
# artifacts from earlier builds first, once: with --all-packages, uv's option
# to clear the output directory empties it for each package, so one build
# could delete the other's artifacts.
rm -rf dist
uv build --all-packages --out-dir dist
uv run --no-sync twine check --strict dist/*
uv run --no-sync python tools/check-installed.py
