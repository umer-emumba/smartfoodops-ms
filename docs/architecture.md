# SmartFoodOps — Architecture (Part A)

QuickServe/SmartFoodOps is a microservices-based food ordering and delivery platform.
This document describes the target Part A design. Anything not yet built is marked
**(Week 2)** or **(Week 3)**; unmarked items exist in the code today. For the reasoning behind each decision, see
[technical-documentation.md](./technical-documentation.md). For diagrams (microservices
architecture + one ER diagram per service database), see [diagrams.md](./diagrams.md). For
what's done vs. pending, see [PROGRESS.md](./PROGRESS.md).

## Services

Database-per-service — each service owns its data exclusively; no service reads another's
database directly. Cross-service contact only happens through the mechanisms in
[Cross-service communication](#cross-service-communication).

| Service | Owns | Datastore |
|---|---|---|
| `api-gateway` | Routing, JWT verification for private routes (see [Gateway-side auth enforcement](#gateway-side-auth-enforcement)), blocked-user deny-list check, rate limiting, `X-Request-ID` correlation IDs, CORS, Swagger index; customer-facing WebSocket/SSE tracking fanout **(Week 2)** | Redis (rate-limit counters, deny-list reads) only |
| `user-service` | Customers, restaurant-admin & system-admin accounts, auth/JWT issuance, roles | Postgres (`user_db`) |
| `restaurant-service` | Restaurant onboarding + approval audit trail, menus, categories, availability/inventory, promotions | Postgres (`restaurant_db`) |
| `order-service` | Order creation (server-side pricing from `restaurant-service`, idempotency keys), order history + status audit trail; state machine transitions and the Temporal saga worker **(Week 2)** | Postgres (`order_db`) |
| `delivery-service` | Rider registration/availability + vetting audit trail, rider location ingestion; assignment logic, ETA, delivery state **(Week 2)** | Postgres (`delivery_db`) + MongoDB (high-frequency rider location pings, TTL-indexed) |
| `payment-service` | Payment records (read-only in Week 1); authorization/capture/refund simulation with idempotent ops **(Week 2)** | Postgres (`payment_db`) |
| `notification-service` | Notification records (list-mine in Week 1); order/delivery notification dispatch, simulated email/SMS/push **(Week 3)** | Postgres (`notification_db`) |
| `analytics-service` (Week 3) | Event/metrics aggregation for the required analytics list | MongoDB (raw event log + rollups) |

Each service is its own FastAPI app, own DB, own Dockerfile, own OpenAPI contract.

## Gateway resilience

`api-gateway` is the single entry point for all client traffic and is fully stateless for
plain REST traffic (JWT verification needs no session, rate-limit counters live in Redis)
— the one exception is a customer's live-order WebSocket/SSE session, which sticks to
whichever instance accepted the handshake (see
[Real-time delivery tracking](#real-time-delivery-tracking)). Statelessness is what makes
horizontal scaling behind a load balancer possible at all — but *doing* that scaling is a
deployment-target concern, not something meaningfully demonstrated on a single dev laptop
(multiple containers on one machine don't exercise anything a real multi-host failure
mode would). Locally, `api-gateway` runs as a single container, exposed directly on
`:8080` — no reverse proxy in front of it. See
[technical-documentation.md: Gateway](./technical-documentation.md#gateway-single-entry-point-not-a-spof)
for the actual production design (Kubernetes `Service` + `Deployment`/`HPA`) and why a
local Traefik-based version of it was removed rather than kept as a demo.

## Cross-service communication

- **Sync REST (httpx, async)** for direct request/response needs. Built today:
  `order-service` → `restaurant-service` at order creation, to fetch live prices,
  availability, and restaurant status (`order-service/app/restaurant_client.py`, 5s
  timeout, clean 503 if unreachable, no retry yet). **(Week 2)**: these calls move into
  Temporal activities, which supply retry/backoff; `payment-service` authorization is
  added the same way.
- **Temporal (Week 2)** owns the **order saga**: `order-service`'s worker orchestrates validate
  availability/capacity → validate stock → price → authorize payment → notify restaurant
  and wait on an accept/reject signal (with timeout) → on acceptance: confirm order →
  assign rider → ETA → schedule notification. Compensating activities (release stock,
  refund, unassign rider) run on rejection, timeout, failure, or cancellation.
- **Kafka + Schema Registry (Week 3)** owns the domain event backbone: `OrderPlaced`,
  `OrderConfirmed`, `PaymentSuccessful`, `RiderAssigned`, `OrderPickedUp`, `OrderDelivered`,
  `OrderCancelled`, `RefundProcessed` — for decoupled fan-out to `notification-service` and
  `analytics-service`, via the transactional outbox pattern (see technical-documentation.md).
  Built today: `order-service` writes an `OrderPlaced` outbox row in the same transaction
  as the order; nothing publishes outbox rows to Kafka until Week 3's relay.
- **Celery + RabbitMQ (Week 3)** owns short, single-service, fire-and-forget jobs: the outbox relay,
  and `notification-service`'s actual dispatch task.
- **Redis** holds the gateway's rate-limit counters and the blocked-user deny-list. Rider
  assignment deliberately does *not* use a Redis lock **(Week 2)** — `riders` lives in one
  Postgres database, so `SELECT … FOR UPDATE SKIP LOCKED` is enough. Order idempotency
  keys live in `order_db` (unique per customer), not Redis — see
  [technical-documentation.md#failure-handling](./technical-documentation.md#failure-handling).

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
  `restaurant_status_history` (append-only audit trail — see **Audit trail** below),
  `promotions` (discount codes — backs "pricing and discounts must be applied"; full CRUD;
  `code` unique **per restaurant**, not globally, so two restaurants can each run their own
  `"WELCOME10"`), `outbox_events`.
- **`order_db`**: `orders` (**order_number** — human-facing sequential ID separate from the
  UUID; **delivery_address_text/lat/lng** — snapshotted at order time, not a live
  reference; **subtotal/tax_amount/delivery_fee/discount_amount/promo_code/total_amount**
  — full pricing breakdown; **status_reason**; **confirmed_at/rejected_at/cancelled_at**;
  status: `created → awaiting_restaurant_acceptance → confirmed → assigned → picked_up →
  delivered`, plus `rejected`/`cancelled`; `version` column; **idempotency_key/
  request_hash**, unique per customer), `order_status_history` (audit trail), `order_items` (incl.
  **item_name_snapshot** — restaurant-service's menu can change or delete the item later;
  cross-service joins aren't possible anyway), `outbox_events`.
- **`delivery_db`**: `riders` (incl. **vehicle_type/license_plate** onboarding fields,
  **last_lat/last_lng** as real numeric columns, not a string blob — spatially queryable,
  **verification_status**: `pending_approval` (default) → `approved` or `suspended`, kept
  separate from `status` [self-reported availability] — see
  [Restaurant & rider approval](#restaurant--rider-approval)),
  `deliveries` (incl. **pickup_lat/lng, dropoff_lat/lng, distance_km** snapshotted at
  assignment time; **assigned_at/picked_up_at**; **reassignment_count**; `version` column),
  `rider_verification_history` (audit trail), `outbox_events`. High-frequency rider location pings go to MongoDB
  (`delivery_db.rider_location_pings`, TTL-indexed at 24h via `app/mongo.py`, written on
  every `PATCH /v1/riders/me/location` call), not Postgres — `riders.last_lat/lng` is just
  the latest known position for assignment queries. `_id` is `sfo_common.new_id()`, same as
  every other ID in the system (see
  [technical-documentation.md](./technical-documentation.md#primary-keys-uuidv7-generated-in-python-not-v4-not-db-side)).
- **`payment_db`**: `payments` (incl. **customer_id** [opaque, added for ownership checks
  on `GET /v1/payments/by-order/{id}` — see
  [Authorization audit](#authorization-audit-2026-09-29)], **payment_method,
  failure_reason, refunded_amount/refunded_at, authorized_at**, `idempotency_key`),
  `outbox_events`.
- **`notification_db`**: `notifications` (incl. **related_order_id** — links back to what
  it's about, **sent_at/read_at, retry_count/last_error**).
- Week 3: `processed_events` table/collection in every Kafka-consuming service (consumer
  dedup); `analytics-service`'s Mongo gets `raw_events` + per-metric rollups.

**Audit trail**: the requirements call for "order history and audit trails." Each service
that owns a status-bearing entity keeps an append-only history table, written **in the same
transaction** as the status change so the trail can never disagree with the current status:

| Table | Records | Read via |
|---|---|---|
| `order_db.order_status_history` | every order transition (Week 1: the initial `created` row; Week 2's saga appends the rest) | embedded in every `OrderResponse` as `status_history` |
| `restaurant_db.restaurant_status_history` | onboarding, approve, suspend — actor, role, optional reason | `GET /v1/restaurants/{id}/status-history` (owner or `system_admin`) |
| `delivery_db.rider_verification_history` | registration, approve, suspend | `GET /v1/riders/{id}/verification-history` (`system_admin`) |

Each row stores `from_status`, `to_status`, `actor_id`, `actor_role`, `reason`, and
`created_at`. No-op transitions (approving an already-active restaurant) record nothing.
**(Week 3)** The Kafka event log adds a second, cross-service, replayable view of the same
facts, but it isn't what the audit trail depends on.

## Auth, token lifetime & blocked-user handling

- **Access tokens**: stateless JWT, 15-minute lifetime, role claims. Verified by every
  service independently (see [Defense-in-depth auth](#defense-in-depth-auth)), not just the
  gateway — signature + expiry only, no DB call.
- **Refresh tokens**: tracked server-side in `user_db` (hashed), 7-day lifetime, rotated on
  every use via an atomic compare-and-set, so two concurrent refreshes with one token can't
  both succeed. `/v1/auth/refresh` checks the record isn't revoked/expired and checks
  `is_blocked` — the authoritative block check. Presenting an *already-rotated* token again
  is treated as theft: every refresh token for that user is revoked (the real owner just
  logs in again). Logout requires the token's secret, not just its lookup ID.
- **Credentials**: emails are lowercased at the API boundary; passwords must be 8–128
  characters at registration; login does the same argon2 work for unknown emails as for a
  wrong password, so response time doesn't reveal which emails exist.
- **Blocked-user enforcement**: `user-service` writes a blocked user's ID into a Redis
  deny-list key with TTL = access-token lifetime (15 min), *before* committing
  `is_blocked` in the DB, so a Redis failure can't leave a block that live tokens ignore. The
  gateway checks this deny-list on every request that carries a valid token, and fails
  closed (503) if Redis is unreachable. Once the entry expires, the user's access token has also expired, and
  getting a new one requires a refresh call that hits the authoritative DB check — so the
  deny-list never needs to cover the block indefinitely, only the current token's lifetime.

## Restaurant & rider approval

Restaurants and riders both start unapproved and must be reviewed by a `system_admin`
before they're operationally trusted:

- **Restaurant**: `POST /v1/restaurants` creates it with `status = "pending_approval"`
  (the client cannot set `status` directly). `system_admin`-only
  `POST /v1/restaurants/{id}/approve` → `active`, or `.../suspend` → `suspended`, each
  with an optional `{"reason": ...}` body recorded in the audit trail (one
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
  A rider can only set `status` to `available`/`busy` once approved (`offline` is always
  allowed), and suspending a rider forces them `offline`, so unvetted riders never
  enter the assignment pool. **Week 2 note**: the assignment query should still filter on
  `verification_status == "approved"` as well, as defense in depth.
- **Bootstrapping the first `system_admin`**: since `system_admin` is excluded from
  `PUBLIC_ROLES` (self-registration would let anyone grant themselves the role that can
  block any user and approve/suspend any restaurant or rider), the only way one is created
  is `POST /v1/auth/bootstrap-admin` on `user-service`, gated by a shared secret
  (`ADMIN_BOOTSTRAP_SECRET`, compared with `hmac.compare_digest`) sent as an
  `X-Bootstrap-Secret` header — not JWT-gated, since no admin session can exist yet to
  gate it with. It is **one-shot**: once any `system_admin` exists it returns 409, so a
  leaked secret can't mint more admins later. The gateway gives it the same strict
  per-IP rate limit as login. `user-service` refuses to start if
  `ENVIRONMENT != local` and the secret is still the public dummy value from
  `env.example`. `scripts/seed.py` calls this to create the demo admin (and tolerates
  the 409 on re-runs) and approve the seeded restaurant/rider.

## Real-time delivery tracking

- **Rider → server (location ingestion)**: plain REST `PATCH /v1/riders/me/location`,
  ~every 5–15s — built in Week 1 (updates `riders.last_lat/lng`, appends a Mongo ping). Stateless, so any
  `delivery-service` replica can handle any rider's ping — no connection-affinity needed.
- **Server → customer (live tracking) (Week 2/3)**: `delivery-service` publishes
  `DeliveryStatusUpdated` events to Kafka; `api-gateway` holds customer WebSocket/SSE
  connections and each replica independently consumes that topic, forwarding matching
  updates to its own locally-held sockets.

## Gateway-side auth enforcement

Originally, `api-gateway` only *read* a bearer token if present (to check the blocked-user
deny-list — see below); it never rejected a request for a missing/invalid/expired token,
relying entirely on each backend service's own defense-in-depth check to 401 it. That meant
an unauthenticated request to a private route still cost a wasted network hop to a backend
that was always going to reject it.

`app/security.py`'s `authenticate` now verifies signature, expiry and claims at the gateway
itself (via `sfo_auth.decode_access_token`, the same check every backend service runs), and
checks the blocked-user deny-list with the same decoded token, before forwarding — for every route *except* an explicit public-route allowlist
(`_PUBLIC_ROUTES` in the same file): `POST /v1/auth/{register,login,refresh,logout,
bootstrap-admin}` (these authenticate via a refresh token, shared secret, or issue the
token itself — there's no bearer token to check yet) and the read-only restaurant-browsing
routes (`GET /v1/restaurants`, `/{id}`, `/{id}/menu`, `/{id}/menu/categories`, `/{id}/promotions` — public by
design). Everything else needs a valid bearer token to even reach a backend service.

This is a **fail-closed allowlist**, deliberately: a newly added private route needs no
gateway change to stay protected, but a newly added public route does (a one-line addition
to `_PUBLIC_ROUTES`). The restaurant-browsing patterns distinguish a real ID from
`mine`/`admin` by matching the UUID shape specifically, not just "any single path segment"
— this is what stops `GET /v1/restaurants/mine` or `/admin` from being misclassified as
public just because they look like `/restaurants/{something}` at the gateway's routing
level.

It runs inside the route, not as middleware, so its errors (401/403, or 503 if the deny-list
is unreachable) go through the shared exception handlers and get the normal JSON envelope and
CORS headers. As middleware, a blocked user got a plain-text 500.

**Backend failures** are mapped the same way: a backend that refuses connections (or can't
be reached within the 2s connect timeout) is a `502 bad_gateway`, one that accepts but
doesn't answer within 10s is a `504 gateway_timeout`. The gateway never retries: most
requests aren't safe to replay, and the client holds the `Idempotency-Key` where one
matters. The proxy strips hop-by-hop headers (RFC 9110 §7.6.1) in both directions, rejects
`..` path segments, and replaces a malformed client `X-Request-ID`.

This is additive, not a replacement for [Defense-in-depth auth](#defense-in-depth-auth)
below — every service still independently re-verifies the same token regardless of what the
gateway already checked. The gateway check is purely a fast-fail: reject obviously-bad
requests before spending a network hop on a backend call that was always going to fail.

## Defense-in-depth auth

Only `api-gateway` verifying JWTs isn't enough — inside Docker Compose, every service is
reachable directly by hostname from any other container. Every service independently
re-verifies the JWT signature and role claims via the shared `libs/auth` dependency.

## Authorization audit (2026-09-29)

A full pass over every route in all 7 services (the gateway plus six domain services) found and fixed three gaps:

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

- **Env vars/secrets**: `env.example` per service (no leading dot, so it can't be confused
  with a real `.env` by tooling that treats `.env*` files as secrets), documenting required vars; real `.env` gitignored, dummy local values only, developer
  copies `env.example` → `.env` before running Compose. `DATABASE_URL`:
  `postgresql+asyncpg://user:pass@host:5432/dbname`.
- **Kafka topics**: one per aggregate (`order-events`, `delivery-events`, `payment-events`),
  `event_type` field inside the payload, partitioned by aggregate ID.
- **Temporal task queue (Week 2)**: single queue, `order-saga-queue`.
- **Correlation ID**: `X-Request-ID`, generated at the gateway if absent, propagated on
  every downstream call, included in structured logs, and stored on outbox rows;
  carried into Kafka headers **(Week 3)**.
- **Idempotency**: `Idempotency-Key` request header, required on `POST /v1/orders` today;
  extended to payment operations **(Week 2)**.
- **Error responses**: `{"error": {"code", "message", "details"}}` envelope, shared handler
  in `libs/common`.
- **API versioning**: all routes under `/v1/...`.
- **Pagination**: Week 1 list endpoints take a bounded `limit` (`1 ≤ limit ≤ 100/200`) and
  return newest-first. Cursor-based (keyset) pagination via `sfo_common.pagination.Page`
  is **(Week 2)** — the helper exists but isn't wired into routes yet.
- **Rate limiting**: `/v1/auth/login`, `/v1/auth/register`, and
  `/v1/auth/bootstrap-admin` share a stricter, separate per-IP limit than general traffic.
- **Logging hygiene**: passwords, tokens, and payment details are never logged, enforced
  via a shared log-sanitizing helper in `libs/observability`.
- **Gateway scope**: routing, auth, rate limiting, CORS, Swagger proxy/index, tracking
  WebSocket/SSE fanout **(Week 2)** — no cross-service response aggregation in Part A.
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
  infra/                          # (Week 3) prometheus/ grafana/ otel-collector/
  libs/
    common/        # FastAPI app factory, config, error handlers, IDs, pagination helper
    auth/           # JWT issue/verify, role dependency, deny-list
    events/         # pydantic event schemas + outbox mixin; kafka producer/consumer (Week 3)
    observability/  # JSON logging with redaction; OTel + Prometheus wiring (Week 3)
  services/
    api-gateway/
    user-service/
    restaurant-service/
    order-service/        # app/ (API); worker/ (Temporal workflow+activities, Week 2)
    delivery-service/
    payment-service/
    notification-service/
    analytics-service/    # added Week 3
  docs/
    PRD.md
    architecture.md
    technical-documentation.md
    diagrams.md
    PROGRESS.md
  scripts/
    seed.py
  README.md
```

Each service directory: `pyproject.toml` (workspace member), `Dockerfile`,
`entrypoint.sh` (runs `alembic upgrade head`, then uvicorn), `env.example`, `app/` (API
routers, models, schemas, repository), `alembic/` + `alembic.ini`, `tests/`.
