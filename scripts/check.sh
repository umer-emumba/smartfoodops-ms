#!/bin/sh
# Everything CI should gate on: lint, formatting, types, and each service's
# (and shared lib's) tests. The pre-commit hook runs the fast subset (lint, format, types).
#
#   scripts/check.sh            unit tests only
#   scripts/check.sh --with-db  also the `db`-marked tests, against the compose
#                               Postgres, Mongo and Qdrant (needs `docker compose up`)
#   scripts/check.sh --with-ai  also the retrieval eval against the running,
#                               seeded and indexed stack (docs/week4-plan.md step 5)
# The two flags combine.
set -e
cd "$(dirname "$0")/.."

markers="not db"
with_ai=""
for arg in "$@"; do
    case "$arg" in
        --with-db) markers="db or not db" ;;
        --with-ai) with_ai=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

uv run --frozen ruff check libs services scripts
uv run --frozen ruff format --check libs services scripts
./scripts/typecheck.sh
for project in libs/*/ services/*/; do
    [ -d "$project/tests" ] || continue
    echo "pytest: $project"
    (cd "$project" && uv run --frozen pytest -q -m "$markers")
done

if [ -n "$with_ai" ]; then
    echo "retrieval eval"
    uv run --frozen --group dev python scripts/eval_retrieval.py --check
fi
