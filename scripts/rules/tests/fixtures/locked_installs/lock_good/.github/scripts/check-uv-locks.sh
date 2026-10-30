#!/usr/bin/env bash
set -euo pipefail

for project in app; do
  uv lock --check --project "$project"
done
