#!/bin/sh
# mypy per service, not over the whole repo: every service has its own top-level
# `app` package, which mypy would otherwise see as one duplicated module.
set -e
cd "$(dirname "$0")/.."

uv run --frozen mypy libs/*/src
for service in services/*/; do
    echo "mypy: $service"
    (cd "$service" && uv run --frozen mypy app)
done
