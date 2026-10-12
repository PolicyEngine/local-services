#!/usr/bin/env bash
set -euo pipefail

: "${TEST_DATABASE_URL:?Set the disposable PostgreSQL test database URL}"
expected_commit=280459c8d782658b3fb6098b27e9acba11af29d8
test "$(git -C .collector rev-parse HEAD)" = "$expected_commit"
uv sync --locked
uv pip install -e .collector/telemetry-service
uv pip install --reinstall --no-deps dist/*.whl
# --no-sync is necessary: these tests must use the installed wheels, not workspace sources.
uv run --no-sync pytest tests/test_collector_contract.py
