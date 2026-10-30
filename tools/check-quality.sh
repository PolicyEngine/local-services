#!/usr/bin/env bash
set -euo pipefail

uv run --locked ruff check .
uv run --locked ruff format --check .
for script in tools/*.sh; do
  bash -n "$script"
done
