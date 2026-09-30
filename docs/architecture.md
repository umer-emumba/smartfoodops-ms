# SmartFoodOps — Architecture (Part A)

QuickServe/SmartFoodOps is a microservices-based food ordering and delivery platform.
This document describes the system as built. For the reasoning behind each decision, see
[technical-documentation.md](./technical-documentation.md). For diagrams (microservices
architecture + one ER diagram per service database), see [diagrams.md](./diagrams.md). For
what's done vs. pending, see [PROGRESS.md](./PROGRESS.md).

## Services

Database-per-service — each service owns its data exclusively; no service reads another's
database directly. Cross-service contact only happens through the mechanisms in
[Cross-service communication](#cross-service-communication).

| Service | Owns | Datastore |
|---|---|---|
| `api-gateway` | Routing, JWT verification for private routes (see [Gateway-side auth enforcement](#gateway-side-auth-enforcement)), rate limiting, request tracing, CORS, Swagger index, customer-facing WebSocket/SSE tracking fanout | Redis (rate-limit counters) only |
| `user-service` | Customers, restaurant-admin & system-admin accounts, auth/JWT issuance, roles | Postgres (`user_db`) |
| `restaurant-service` | Restaurant profiles, menus, categories, availability/inventory | Postgres (`restaurant_db`) |
| `order-service` | Order lifecycle state machine, pricing, order history/audit; hosts the Temporal saga worker | Postgres (`order_db`) |
| `delivery-service` | Rider registration/availability, assignment logic, ETA, delivery state, rider location ingestion | Postgres (`delivery_db`) + MongoDB (high-frequency rider location pings, TTL-indexed) |
| `payment-service` | Payment authorization/capture/refund simulation, idempotent payment ops | Postgres (`payment_db`) |
| `notification-service` | Order/delivery notification dispatch (simulated email/SMS/push) | Postgres (`notification_db`) |
| `analytics-service` (Week 3) | Event/metrics aggregation for the required analytics list | MongoDB (raw event log + rollups) |

Each service is its own FastAPI app, own DB, own Dockerfile, own OpenAPI contract.

## Gateway resilience

`api-gateway` is the single entry point for all client traffic, so it runs as multiple
stateless replicas (`docker compose up --scale api-gateway=N`) behind **Traefik**, which
auto-discovers replicas via Docker service discovery, round-robins across them, and polls
each replica's `/health` endpoint, pulling unhealthy instances out of rotation within
seconds. The gateway is stateless for plain REST traffic (JWT verification needs no
session, rate-limit counters live in Redis) — the one exception is a customer's live-order
WebSocket/SSE session, which sticks to whichever replica accepted the handshake (see
[Real-time delivery tracking](#real-time-delivery-tracking)).

## Cross-service communication

- **Sync REST (httpx, async)** for direct request/response needs — e.g. Temporal
  activities calling `restaurant-service` to validate stock or `payment-service` to
  authorize. Wrapped with timeout + retry/backoff + circuit breaker (`tenacity`).
- **Temporal** owns the **order saga**: `order-service`'s worker orchestrates validate
  availability/capacity → validate stock → price → authorize payment → notify restaurant
  and wait on an accept/reject signal (with timeout) → on acceptance: confirm order →
  assign rider → ETA → schedule notification. Compensating activities (release stock,
  refund, unassign rider) run on rejection, timeout, failure, or cancellation.
- **Kafka + Schema Registry** owns the domain event backbone: `OrderPlaced`,
  `OrderConfirmed`, `PaymentSuccessful`, `RiderAssigned`, `OrderPickedUp`, `OrderDelivered`,
  `OrderCancelled`, `RefundProcessed` — for decoupled fan-out to `notification-service` and
  `analytics-service`, via the transactional outbox pattern (see technical-documentation.md).
- **Celery + RabbitMQ** owns short, single-service, fire-and-forget jobs: the outbox relay,
  and `notification-service`'s actual dispatch task.
- **Redis** is the read-through cache for hot reads, the idempotency-key store, the
  rider-assignment distributed lock, and the gateway's rate-limit counters.

## Data model (entity-level; see each service's `alembic/` for exact schema)

No cross-service foreign keys — a service referencing another service's entity stores an
opaque ID, never an enforced FK. Every primary/foreign key (and Kafka event ID) is
generated via `sfo_common.new_id()` — **UUIDv7**, not v4 — for B-tree index locality under
high write volume; see technical-documentation.md for why.

- **`user_db`**: `users` (id, email, **full_name, phone_number** — required onboarding
  fields, **email_verified/phone_verified**, role, is_blocked, timestamps); `refresh_tokens`
  (id, user_id, token_hash, expires_at, revoked_at); `addresses` (saved customer delivery
  addresses — label, line1/line2, city, **lat/lng**, is_default; orders snapshot these
  fields rather than referencing a row live, since a saved address can change later).
- **`restaurant_db`**: `restaurants` (incl. **lat/lng** — required for proximity/ETA,
  **contact_phone, cuisine_type**, **is_accepting_orders/max_concurrent_orders/
  avg_prep_time_minutes** — the PRD's "capacity must be checked", **status**:
  `pending_approval` (default on creation) → `active` (system_admin-approved) or
  `suspended` — see [Restaurant & rider approval](#restaurant--rider-approval)),
  `menu_categories` (full CRUD; delete blocked while it still has menu items),
  `menu_items` (incl. `version` for optimistic locking on stock; full CRUD, and
  `category_id` is checked against the owning restaurant on create — not just trusted),
  `promotions` (discount codes — backs "pricing and discounts must be applied"; full CRUD;
  `code` unique **per restaurant**, not globally, so two restaurants can each run their own
  `"WELCOME10"`), `outbox_events`.
- **`order_db`**: `orders` (**order_number** — human-facing sequential ID separate from the
  UUID; **delivery_address_text/lat/lng** — snapshotted at order time, not a live
  reference; **subtotal/tax_amount/delivery_fee/discount_amount/promo_code/total_amount**
  — full pricing breakdown; **status_reason**; **confirmed_at/rejected_at/cancelled_at**;
  status: `created → awaiting_restaurant_acceptance → confirmed → assigned → picked_up →
  delivered`, plus `rejected`/`cancelled`; `version` column), `order_items` (incl.
  **item_name_snapshot** — restaurant-service's menu can change or delete the item later;
  cross-service joins aren't possible anyway), `outbox_events`.
- **`delivery_db`**: `riders` (incl. **vehicle_type/license_plate** onboarding fields,
  **last_lat/last_lng** as real numeric columns, not a string blob — spatially queryable,
  **verification_status**: `pending_approval` (default) → `approved` or `suspended`, kept
  separate from `status` [self-reported availability] — see
  [Restaurant & rider approval](#restaurant--rider-approval)),
  `deliveries` (incl. **pickup_lat/lng, dropoff_lat/lng, distance_km** snapshotted at
  assignment time; **assigned_at/picked_up_at**; **reassignment_count**; `version` column),
  `outbox_events`. High-frequency rider location pings go to MongoDB
  (`delivery_db.rider_location_pings`, TTL-indexed at 24h via `app/mongo.py`, written on
  every `PATCH /v1/riders/me/location` call), not Postgres — `riders.last_lat/lng` is just
  the latest known position for assignment queries. `_id` is `sfo_common.new_id()`, same as
  every other ID in the system (see technical-documentation.md#primary-keys).
- **`payment_db`**: `payments` (incl. **customer_id** [opaque, added for ownership checks
  on `GET /v1/payments/by-order/{id}` — see
  [Authorization audit](#authorization-audit-2026-09-29)], **payment_method,
  failure_reason, refunded_amount/refunded_at, authorized_at**, `idempotency_key`),
  `outbox_events`.
- **`notification_db`**: `notifications` (incl. **related_order_id** — links back to what
  it's about, **sent_at/read_at, retry_count/last_error**).
- Week 3: `processed_events` table/collection in every Kafka-consuming service (consumer
  dedup); `analytics-service`'s Mongo gets `raw_events` + per-metric rollups.

**Audit trail**: the PRD requires "order history and audit trails." Rather than a separate
`audit_log` table per entity, this is satisfied by the Kafka event log (every state change
is a published domain event, replayable) plus each table's own timestamp columns above —
deliberate, not an oversight; documented here so it isn't mistaken for a gap.

## Auth, token lifetime & blocked-user handling

- **Access tokens**: stateless JWT, 15-minute lifetime, role claims. Verified by every
  service independently (see [Defense-in-depth auth](#defense-in-depth-auth)), not just the
  gateway — signature + expiry only, no DB call.
- **Refresh tokens**: tracked server-side in `user_db` (hashed), 7-day lifetime, rotated on
  every use. `/v1/auth/refresh` checks the record isn't revoked/expired and checks
  `is_blocked` — the authoritative block check.
- **Blocked-user enforcement**: `user-service` writes a blocked user's ID into a Redis
  deny-list key with TTL = access-token lifetime (15 min). The gateway checks this deny-list
  on every request. Once the entry expires, the user's access token has also expired, and
  getting a new one requires a refresh call that hits the authoritative DB check — so the
  deny-list never needs to cover the block indefinitely, only the current token's lifetime.

## Restaurant & rider approval

Restaurants and riders both start unapproved and must be reviewed by a `system_admin`
before they're operationally trusted:

- **Restaurant**: `POST /v1/restaurants` creates it with `status = "pending_approval"`
  (the client cannot set `status` directly). `system_admin`-only
  `POST /v1/restaurants/{id}/approve` → `active`, or `.../suspend` → `suspended` (one
  action covers both "reject a pending restaurant" and "suspend an already-active one" —
  both mean "not allowed to operate"). `GET /v1/restaurants` (public) only ever returns
  `active` restaurants; `GET /v1/restaurants/mine` (the owning `restaurant_admin`) and
  `GET /v1/restaurants/admin` (`system_admin`, optionally `?status=`) are the only ways to
  see pending/suspended ones.
- **Rider**: `POST /v1/riders` creates it with `verification_status = "pending_approval"`,
  deliberately a separate field from `status` (the rider's own available/busy/offline
  toggle) — a rider being approved and a rider being currently available are independent
  facts. `system_admin`-only `POST /v1/riders/{id}/approve` / `.../suspend`, and
  `GET /v1/riders/admin` (optionally `?verification_status=`) to review pending riders.
  **Week 2 note**: the proximity-based assignment query must filter on
  `verification_status == "approved"` in addition to `status == "available"` — not wired
  yet, since assignment logic itself doesn't exist until Week 2.
- **Bootstrapping the first `system_admin`**: since `system_admin` is excluded from
  `PUBLIC_ROLES` (self-registration would let anyone grant themselves the role that can
  block any user and approve/suspend any restaurant or rider), the only way one is created
  is `POST /v1/auth/bootstrap-admin` on `user-service`, gated by a shared secret
  (`ADMIN_BOOTSTRAP_SECRET`, compared with `hmac.compare_digest`) sent as an
  `X-Bootstrap-Secret` header — not JWT-gated, since no admin session can exist yet to
  gate it with. `scripts/seed.py` calls this to create the demo admin and approve the
  seeded restaurant/rider.

## Real-time delivery tracking

- **Rider → server (location ingestion)**: plain REST POST, ~every 5–15s. Stateless, so any
  `delivery-service` replica can handle any rider's ping — no connection-affinity needed.
- **Server → customer (live tracking)**: `delivery-service` publishes
  `DeliveryStatusUpdated` events to Kafka; `api-gateway` holds customer WebSocket/SSE
  connections and each replica independently consumes that topic, forwarding matching
  updates to its own locally-held sockets.

## Gateway-side auth enforcement

Originally, `api-gateway` only *read* a bearer token if present (to check the blocked-user
deny-list — see below); it never rejected a request for a missing/invalid/expired token,
relying entirely on each backend service's own defense-in-depth check to 401 it. That meant
an unauthenticated request to a private route still cost a wasted network hop to a backend
that was always going to reject it.

`app/security.py`'s `require_valid_bearer_token` now verifies signature + expiry at the
gateway itself (via `sfo_auth.decode_access_token`, the same check every backend service
runs) before forwarding — for every route *except* an explicit public-route allowlist
(`_PUBLIC_ROUTES` in the same file): `POST /v1/auth/{register,login,refresh,logout,
bootstrap-admin}` (these authenticate via a refresh token, shared secret, or issue the
token itself — there's no bearer token to check yet) and the read-only restaurant-browsing
routes (`GET /v1/restaurants`, `/{id}`, `/{id}/menu`, `/{id}/promotions` — public by
design). Everything else needs a valid bearer token to even reach a backend service.

This is a **fail-closed allowlist**, deliberately: a newly added private route needs no
gateway change to stay protected, but a newly added public route does (a one-line addition
to `_PUBLIC_ROUTES`). The restaurant-browsing patterns distinguish a real ID from
`mine`/`admin` by matching the UUID shape specifically, not just "any single path segment"
— this is what stops `GET /v1/restaurants/mine` or `/admin` from being misclassified as
public just because they look like `/restaurants/{something}` at the gateway's routing
level.

This is additive, not a replacement for [Defense-in-depth auth](#defense-in-depth-auth)
below — every service still independently re-verifies the same token regardless of what the
gateway already checked. The gateway check is purely a fast-fail: reject obviously-bad
requests before spending a network hop on a backend call that was always going to fail.

## Defense-in-depth auth

Only `api-gateway` verifying JWTs isn't enough — inside Docker Compose, every service is
reachable directly by hostname from any other container. Every service independently
re-verifies the JWT signature and role claims via the shared `libs/auth` dependency.

## Authorization audit (2026-09-29)

A full pass over every route in all 8 services found and fixed three gaps:

1. **Restaurant self-approval loophole**: `RestaurantUpdate` (behind the generic
   `PATCH /v1/restaurants/{id}`, open to the restaurant's own `restaurant_admin`) used to
   include `status`, so an owner could PATCH their own way to `"active"`, bypassing
   [Restaurant & rider approval](#restaurant--rider-approval) entirely. Fixed by removing
   `status` from `RestaurantUpdate` — it can now only change via the `system_admin`-only
   `/approve`/`/suspend` actions.
2. **Payment records had no ownership check**: `GET /v1/payments/by-order/{order_id}`
   required only *a* valid JWT, any role, no check that the caller was the customer who
   placed that order — any authenticated user could read anyone's payment amount/method/
   failure reason by guessing an `order_id`. Fixed by adding an opaque `customer_id` to
   `payments` and checking `payment.customer_id == current.id` (or `system_admin`) in the
   route.
3. **Pending/suspended restaurants were readable by direct ID**: `GET
   /v1/restaurants/{id}`, `.../menu`, and `.../promotions` are intentionally public for
   browsing `active` restaurants, but didn't check `status` at all — a `pending_approval`
   or `suspended` restaurant's full profile/menu was visible to anyone with its ID, even
   though it's correctly excluded from the public list. Fixed with a shared
   `_ensure_restaurant_visible` check (404, not 403, so a suspended restaurant's existence
   isn't confirmed to an outsider either) — visible to the public only when `active`,
   otherwise only to its own `restaurant_admin` or a `system_admin`. This needed a new
   `get_current_user_optional` dependency in `libs/auth` (returns `None` instead of
   raising when no/invalid bearer token is present), since these routes must stay
   reachable without auth for the normal public-browsing case.

## Conventions

- **Env vars/secrets**: `env.example` (no leading dot — the harness's permission settings
  block writing `.env*` files, even templates, as a secrets-safety guard) per service,
  documenting required vars; real `.env` gitignored, dummy local values only, developer
  copies `env.example` → `.env` before running Compose. `DATABASE_URL`:
  `postgresql+asyncpg://user:pass@host:5432/dbname`.
- **Kafka topics**: one per aggregate (`order-events`, `delivery-events`, `payment-events`),
  `event_type` field inside the payload, partitioned by aggregate ID.
- **Temporal task queue**: single queue, `order-saga-queue`.
- **Correlation ID**: `X-Request-ID`, generated at the gateway if absent, propagated on
  every downstream call and into Kafka headers, included in structured logs.
- **Idempotency**: `Idempotency-Key` request header on retryable mutating endpoints.
- **Error responses**: `{"error": {"code", "message", "details"}}` envelope, shared handler
  in `libs/common`.
- **API versioning**: all routes under `/v1/...`.
- **Pagination**: cursor-based (keyset) on all list endpoints, not offset/limit.
- **Rate limiting**: `/v1/auth/login` and `/v1/auth/register` get stricter, separate
  per-IP limits than general traffic.
- **Logging hygiene**: passwords, tokens, and payment details are never logged, enforced
  via a shared log-sanitizing helper in `libs/observability`.
- **Gateway scope**: routing, auth, rate limiting, CORS, Swagger proxy/index, tracking
  WebSocket/SSE fanout — no cross-service response aggregation in Part A.
- **Health checks**: every service exposes `GET /health`; `docker-compose.yml` uses
  `depends_on: condition: service_healthy`.
- **CORS**: handled once, at `api-gateway` only.
- **Seed/demo data**: `scripts/seed.py` creates a demo restaurant + menu + rider + customer.

## API documentation

FastAPI auto-generates OpenAPI from route signatures + Pydantic models — Swagger UI at
`/docs`, ReDoc at `/redoc`, spec at `/openapi.json` per service, no extra library.
`api-gateway` proxies each service's docs under `/docs/<service-name>` plus an index page.

## Repo layout

```
smartfoodops/
  pyproject.toml                 # uv workspace root
  docker-compose.yml
  infra/
    prometheus/  grafana/  otel-collector/
  libs/
    common/        # FastAPI app factory, config, error handlers
    auth/           # JWT issue/verify, role dependency
    events/         # pydantic event schemas, kafka producer/consumer, outbox helper
    observability/  # OTel + logging + Prometheus wiring
  services/
    api-gateway/
    user-service/
    restaurant-service/
    order-service/        # app/ (API) + worker/ (Temporal workflow+activities)
    delivery-service/
    payment-service/
    notification-service/
    analytics-service/    # added Week 3
  docs/
    architecture.md
    technical-documentation.md
    PROGRESS.md
  scripts/
    seed.py
  README.md
```

Each service directory: `pyproject.toml` (workspace member), `Dockerfile`, `app/` (api
routers, models, schemas, domain services, repository), `alembic/` migrations, `tests/`.
