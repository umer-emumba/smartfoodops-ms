#!/bin/sh
# Everything CI should gate on: lint, formatting, types, and each service's
# tests. The pre-commit hook runs the fast subset (lint, format, types).
set -e
cd "$(dirname "$0")/.."

uv run --frozen ruff check libs services scripts
uv run --frozen ruff format --check libs services scripts
./scripts/typecheck.sh
for service in services/*/; do
    echo "pytest: $service"
    (cd "$service" && uv run --frozen pytest -q)
done
