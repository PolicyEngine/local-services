#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
# Build each distribution's wheel from its source archive to check that the
# libraries, Alembic migrations and protocol fixtures are included. Remove
# artifacts from earlier builds first.
uv build --all-packages --out-dir dist --clear
uv run --no-sync twine check --strict dist/*
uv run --no-sync python tools/check-installed.py
