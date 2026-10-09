#!/bin/sh
# Everything CI should gate on: lint, formatting, types, and each service's
# tests. The pre-commit hook runs the fast subset (lint, format, types).
#
#   scripts/check.sh            unit tests only
#   scripts/check.sh --with-db  also the `db`-marked tests, against the compose
#                               Postgres (needs `docker compose up`)
set -e
cd "$(dirname "$0")/.."

markers="not db"
if [ "$1" = "--with-db" ]; then
    markers="db or not db"
fi

uv run --frozen ruff check libs services scripts
uv run --frozen ruff format --check libs services scripts
./scripts/typecheck.sh
for service in services/*/; do
    echo "pytest: $service"
    (cd "$service" && uv run --frozen pytest -q -m "$markers")
done
