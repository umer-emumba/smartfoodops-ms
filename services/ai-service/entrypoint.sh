#!/bin/sh
set -e
# Container start-up, in order: 1) bring this service's own database up to
# date (replicas queue on a Postgres lock, see sfo_common.db), 2) start the
# API. `set -e` means a failed migration stops the container before it serves
# anything. The Temporal worker / Kafka consumer start later, inside the API
# process (lifespan in app/main.py).
alembic upgrade head
# exec: uvicorn replaces the shell as PID 1, so docker stop's SIGTERM reaches it.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
