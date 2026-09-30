# SmartFoodOps

A microservices-based food ordering and delivery platform (Part A of the SmartFoodOps
assignment — core platform + distributed systems; the GenAI layer is Part B, built later
on top of this).

See [docs/architecture.md](docs/architecture.md) for the full system design,
[docs/technical-documentation.md](docs/technical-documentation.md) for the reasoning
behind each decision, [docs/diagrams.md](docs/diagrams.md) for the microservices
architecture diagram and per-service ER diagrams, and [docs/PROGRESS.md](docs/PROGRESS.md)
for current build status — **read PROGRESS.md first** when picking this project back up in
a new session.

## Architecture at a glance

```
                         ┌────────────┐
                         │  Traefik   │  (load balancer, scales api-gateway
                         │            │   horizontally — no SPOF)
                         └─────┬──────┘
                               │
                         ┌─────▼──────┐
                         │ api-gateway│  routing, JWT + deny-list check,
                         │            │  rate limiting, CORS, Swagger index,
                         └──┬───┬───┬─┘  tracking WebSocket/SSE (Week 2)
                            │   │   │
       ┌────────────────────┘   │   └────────────────────┐
       │                        │                         │
┌──────▼──────┐          ┌──────▼──────┐           ┌──────▼──────┐
│ user-service│          │ restaurant- │           │order-service│──► Temporal
│  (own DB)   │          │  service    │           │  (own DB)   │    (order saga,
└─────────────┘          │  (own DB)   │           └──────┬──────┘     Week 2)
                          └─────────────┘                  │
       ┌─────────────────────────────────┬─────────────────┴──────────┐
       │                                 │                            │
┌──────▼──────┐                  ┌───────▼──────┐            ┌────────▼───────┐
│delivery-svc │                  │payment-service│            │notification-svc│
│ (DB + Mongo)│                  │   (own DB)    │            │    (own DB)    │
└─────────────┘                  └───────────────┘            └────────────────┘

   All domain events flow through Kafka (order-events / delivery-events /
   payment-events) via the transactional outbox pattern — see architecture.md.
```

Every service owns its own database — no service reads another's DB directly.
Cross-service contact happens only via sync REST (with retry/circuit-breaker), the
Temporal order saga, Kafka domain events, or Celery background jobs. Full rationale in
[docs/architecture.md](docs/architecture.md).

## Core workflows

- **Order lifecycle**: `created → awaiting_restaurant_acceptance → confirmed → assigned →
  picked_up → delivered`, plus `rejected`/`cancelled`. Orchestrated by a Temporal saga in
  `order-service` starting Week 2 (Week 1 has order creation only).
- **Delivery lifecycle**: rider registration/availability → proximity-based assignment →
  pickup → delivery, with real-time customer tracking split by traffic shape (REST
  ingestion from riders, WebSocket/SSE push to customers) — see
  [architecture.md#real-time-delivery-tracking](docs/architecture.md#real-time-delivery-tracking).
- **Cancellation/refund**: Temporal signal-driven compensation (release stock, refund
  payment, unassign rider) depending on how far the order had progressed — Week 2.

## Tech stack

Python, FastAPI, PostgreSQL (database-per-service), MongoDB, Redis, Celery + RabbitMQ,
Kafka + Schema Registry, Temporal, Prometheus + Grafana, Jaeger, OpenTelemetry, Docker
Compose, Traefik. Dependency management via `uv` workspaces. Full library-level choices
(ORM, JWT, Kafka client, etc.) with rationale in
[docs/technical-documentation.md](docs/technical-documentation.md#library--tooling-choices).

## Local setup

Requires Docker + Docker Compose, and [uv](https://docs.astral.sh/uv/) if you want to run
anything outside containers (tests, the seed script).

1. For each service under `services/`, copy its `env.example` to `.env` (dummy local
   values are already fine for dev — see
   [architecture.md#conventions](docs/architecture.md#conventions) for why these are
   named `env.example` rather than the more common `.env.example`):

   ```bash
   for d in services/*/; do [ -f "$d/env.example" ] && cp "$d/env.example" "$d/.env"; done
   ```

2. Bring the whole stack up:

   ```bash
   docker compose up --build
   ```

   This starts per-service Postgres containers, Mongo, Redis, RabbitMQ, Kafka + Schema
   Registry, Temporal (+ UI on `:8088`), Traefik (`:8080`), and all app services. Each
   service runs its own Alembic migrations on startup. Wait for `docker compose ps` to
   show everything healthy.

3. Seed demo data (a restaurant, menu, rider, and customer) through the gateway:

   ```bash
   uv run --group dev python scripts/seed.py
   ```

4. Everything is reachable through the gateway at `http://localhost:8080`. Try:

   ```bash
   curl http://localhost:8080/v1/restaurants
   ```

To scale the gateway horizontally (demonstrating the no-SPOF design):

```bash
docker compose up --scale api-gateway=3
```

### Running tests for a single service

```bash
cd services/user-service
uv sync
uv run --group dev pytest
```

(Each service is an independent `uv` project within the workspace — running `uv sync`
inside one service directory resyncs the shared workspace `.venv` to that service's own
dependency closure. This is expected, not a bug — see
[docs/architecture.md](docs/architecture.md) for why each service is a self-contained
package.)

## API overview

Every service auto-generates its own OpenAPI/Swagger docs (no hand-maintained endpoint
list to go stale) — browse them all from one place once the stack is up:

**http://localhost:8080/docs**

Or hit a specific service's docs directly, e.g. `http://localhost:8080/docs/order-service`.

Clients only ever talk to the gateway — it routes each `/v1/{resource}/...` call to the
service that owns that resource (see the routing table in
[docs/architecture.md](docs/architecture.md#conventions)). Resource → owning service:

| Resource prefix | Service |
|---|---|
| `/v1/auth`, `/v1/users` | user-service |
| `/v1/restaurants` | restaurant-service |
| `/v1/orders` | order-service |
| `/v1/riders` | delivery-service |
| `/v1/payments` | payment-service |
| `/v1/notifications` | notification-service |
