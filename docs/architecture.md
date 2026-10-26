# SmartFoodOps — Architecture (Part A)

QuickServe/SmartFoodOps is a microservices-based food ordering and delivery platform.
This document describes the Part A system as built (Weeks 1–3). For the reasoning behind each decision, see
[technical-documentation.md](./technical-documentation.md). For diagrams (microservices
architecture, order-lifecycle sequences, one ER diagram per service database), see
[diagrams.md](./diagrams.md). For
what's done vs. pending, see [PROGRESS.md](./PROGRESS.md).

## Services

Database-per-service — each service owns its data exclusively; no service reads another's
database directly. Cross-service contact only happens through the mechanisms in
[Cross-service communication](#cross-service-communication).

| Service | Owns | Datastore |
|---|---|---|
| `api-gateway` | Routing, JWT verification for private routes (see [Gateway-side auth enforcement](#gateway-side-auth-enforcement)), blocked-user deny-list check, rate limiting, `X-Request-ID` correlation IDs, CORS, Swagger index; streaming proxy for the SSE tracking routes | Redis (rate-limit counters, deny-list reads) only |
| `user-service` | Customers, restaurant-admin & system-admin accounts, auth/JWT issuance, roles | Postgres (`user_db`) |
| `restaurant-service` | Restaurant onboarding + approval audit trail, menus, categories, availability/inventory, promotions; saga activities: atomic stock + capacity reservation, promotion redemption | Postgres (`restaurant_db`) |
| `order-service` | Order creation (server-side pricing from `restaurant-service`, idempotency keys), order history + status audit trail, order state machine, restaurant accept/reject, cancel, refunds; the Temporal **order saga** and refund workflow, plus a reconciler that starts sagas a crash left unstarted | Postgres (`order_db`) |
| `delivery-service` | Rider registration/availability + vetting audit trail, rider location ingestion; deliveries, nearest-rider offers, reassignment, ETA, stale-rider detection | Postgres (`delivery_db`) + Redis (GEO rider pool, live positions) + MongoDB (high-frequency rider location pings, TTL-indexed) |
| `payment-service` | Simulated card payments: authorize, capture, void, full/partial refund — idempotent per order and per refund | Postgres (`payment_db`) |
| `tracking-service` | Live SSE feeds: a customer's order, a restaurant's incoming orders. Stateless; ownership checked from Redis state keys. Its Kafka consumer turns order events into live-feed updates | Redis (Pub/Sub + state keys); consumer group `tracking-service` on `order-events` |
| `notification-service` | Turns order, payment and delivery events into notifications for the customer, restaurant admin and rider (in-app, simulated email and SMS); the in-app inbox. Kafka consumer group `notification-service` | Postgres (`notification_db`) |
| `analytics-service` | The nine Part A metrics, computed from events; read API for system admins and restaurant admins. Kafka consumer group `analytics-service` (also reads the `.dlq` topics) | MongoDB (`analytics`: `order_facts`, `rider_shift_facts`, `failed_events`) |

Each service is its own FastAPI app, own DB, own Dockerfile, own OpenAPI contract. The
four services with saga steps (order, restaurant, payment, delivery) each also run a
Temporal worker — see [Order saga (Temporal)](#order-saga-temporal).

**Workers** — Celery processes from the same image as their service (`celery -A
app.celery_app worker --beat --pool=solo`), each on its own RabbitMQ queue and with its
own beat schedule:

| Container | Runs | Schedule |
|---|---|---|
| `order-worker`, `payment-worker`, `delivery-worker` | the outbox relay: publishes the service's outbox to Kafka (`sfo_events.relay`); purges rows dispatched over 7 days ago | relay every 1s, purge hourly |
| `notification-worker` | `send_notification(id)`: one send attempt, retried with backoff; the sweep that re-enqueues stuck `pending` rows | sweep every 30s |

restaurant-service has an outbox table but writes no events, so it has no relay.

**Platform containers** (`docker-compose.yml`): Postgres per service, MongoDB, Redis,
RabbitMQ, Kafka (KRaft, single broker) + Schema Registry, `kafka-init` (creates the
topics, then exits), Temporal dev server, and for observability Jaeger, Prometheus,
Loki, Grafana Alloy and Grafana — see
[technical-documentation.md#observability](./technical-documentation.md#observability).

## Gateway resilience

`api-gateway` is the single entry point for all client traffic and is fully stateless for
plain REST traffic (JWT verification needs no session, rate-limit counters live in Redis)
— the one exception is an open SSE stream, which is relayed by whichever instance accepted
it until the client disconnects; a reconnecting client can land on any instance and gets a
fresh snapshot (see [Real-time delivery tracking](#real-time-delivery-tracking)). Statelessness is what makes
horizontal scaling behind a load balancer possible at all — but *doing* that scaling is a
deployment-target concern, not something meaningfully demonstrated on a single dev laptop
(multiple containers on one machine don't exercise anything a real multi-host failure
mode would). Locally, `api-gateway` runs as a single container, exposed directly on
`:8080` — no reverse proxy in front of it. See
[technical-documentation.md: Gateway](./technical-documentation.md#gateway-single-entry-point-not-a-spof)
for the actual production design (Kubernetes `Service` + `Deployment`/`HPA`) and why a
local Traefik-based version of it was removed rather than kept as a demo.

## Cross-service communication

- **Sync REST (httpx, async)** for direct request/response needs:
  `order-service` → `restaurant-service` at order creation, to fetch live prices,
  availability, restaurant status, owner and location in one call to
  `POST /internal/restaurants/{id}/quote` (only the ordered items, not the full menu;
  `order-service/app/restaurant_client.py`, 5s timeout, clean 503 if unreachable). Made
  *after* the order-service request has released its DB connection, so a slow
  `restaurant-service` can't pin order-service's connection pool. The only other internal
  calls are `tracking-service`'s fallbacks when a Redis state key is missing
  (`GET /internal/orders/{id}/tracking`, `GET /internal/restaurants/{id}/owner`).
  `/internal/*` routes are service-to-service only: the gateway forwards `/v1/*`
  exclusively, so clients can't reach them.
- **Temporal** owns the **order saga** and post-delivery refunds — see
  [Order saga (Temporal)](#order-saga-temporal).
- **Redis Pub/Sub** carries live tracking to whoever is watching: order status changes
  arrive through Kafka (`tracking-service`'s consumer applies and publishes them),
  delivery details and rider positions are published by delivery-service directly — see
  [Real-time delivery tracking](#real-time-delivery-tracking).
- **Kafka + Schema Registry** owns the domain event backbone — decoupled fan-out to
  `notification-service` and `analytics-service`. Every event is written as an outbox
  row in the same transaction as the change it describes, and a relay worker publishes
  it (the transactional outbox pattern). See [Events](#events) below.
- **Celery + RabbitMQ** owns short, single-service background jobs: the outbox relay and
  sending notifications (see **Workers** above).

### Events

| Topic (3 partitions, key = aggregate ID) | Event | Producer | Payload highlights | Consumed by |
|---|---|---|---|---|
| `order-events` | `OrderPlaced` | order-service (`POST /v1/orders`) | customer, restaurant, restaurant admin, items, `total_amount` (still the subtotal: the saga adds tax and fee later) | notification, analytics |
| | `OrderAwaitingRestaurant` | saga transition | `restaurant_admin_id`, total | notification (restaurant), analytics |
| | `OrderConfirmed`, `OrderRejected`, `OrderCancelled`, `RiderAssigned`, `OrderPickedUp`, `OrderDelivered` | saga transitions (`transition_order`) | from/to status, reason, customer, restaurant admin, total | notification, analytics |
| `payment-events` | `PaymentSuccessful` | payment-service (authorize) | payment, customer, amount, status | — (published for future consumers; an authorization isn't worth a message, and analytics doesn't need it) |
| | `RefundProcessed` | payment-service (refund) | amount, customer | notification, analytics |
| `delivery-events` | `DeliveryStatusUpdated` | delivery-service (every delivery status) | from/to status, `rider_user_id`, customer, reason | notification (rider), analytics |
| | `RiderShiftChanged` | delivery-service (offline ↔ on shift: status endpoint, suspension, drop-out) | on/off shift, reason | analytics |

Every topic has a `<topic>.dlq` twin for messages a consumer gave up on; messages keep
their key and headers, plus `dlq.*` headers (error, error type, consumer group, original
topic/partition/offset, attempts). Message headers also carry `event_type`, the
correlation ID and the W3C `traceparent`. Schemas: JSON Schema generated from the
Pydantic payload models (`libs/events/src/sfo_events/payloads.py`), registered under
`TopicRecordNameStrategy` with `BACKWARD` compatibility; snapshots in
`libs/events/schemas/`.
- **Redis** also holds the gateway's rate-limit counters and the blocked-user deny-list,
  the public menu cache, and the rider assignment pool (GEO set `riders:available`).
  Rider assignment needs no distributed lock: the nearest candidate comes from
  `GEOSEARCH`, and the claim is one atomic `UPDATE riders … WHERE status = 'available'`
  in `delivery_db`, so two deliveries can never get the same rider. Order idempotency
  keys live in `order_db` (unique per customer), not Redis — see
  [technical-documentation.md#failure-handling](./technical-documentation.md#failure-handling).
  Redis persists with AOF (`everysec`) on a volume, so the deny-list and counters survive
  a restart.

## Order saga (Temporal)

`OrderSagaWorkflow` (workflow ID `order-{order_id}`) lives in `order-service` and runs on
the `order-saga` task queue. Each step is an activity that runs **in the service that owns
the data** — each of the four services runs its own worker on its own task queue — so saga
steps need no internal REST API, and Temporal supplies retries and timeouts end to end.
Activity and signal contracts (names, task queues, input/output models) are shared through
`libs/contracts` (`sfo_contracts`).

| Task queue | Activity | Compensated by |
|---|---|---|
| `restaurant-activities` | `reserve_order` (stock + capacity, atomic) | `release_reservation` |
| | `redeem_promotion` | `release_promotion` |
| | `complete_reservation` (pickup: capacity freed) | — |
| `payment-activities` | `authorize` | `void` |
| | `capture` (on delivery), `refund` (refund workflow) | — |
| `order-activities` | `transition_order`, `apply_pricing`, `record_refund` | — (idempotent state writes) |
| `delivery-activities` | `create_delivery` | `cancel_delivery` (also frees the rider) |
| | `offer_to_nearest_rider` | `release_rider` |
| | `transition_delivery`, `compute_eta` | — |

Signals: `restaurant_response`, `rider_response`, `rider_dropped`, `picked_up`,
`delivered`, `cancel` — sent by the REST endpoint that owns the action (restaurant
response and cancel → order-service; rider actions → delivery-service), and by
delivery-service's stale-rider detector. Query: `status` (current step). A second
workflow, `OrderRefundWorkflow` (`refund-{order_id}`), handles post-delivery refunds.

- **Retries**: activities retry 5 times (1s, doubling); business failures (out of stock,
  card declined, …) are non-retryable and go straight to compensation. Compensations and
  every step after pickup retry until they succeed.
- **Workers**: started from the service's lifespan in development
  (`TEMPORAL_WORKER_MODE=embedded`), or as their own process in production
  (`python -m app.worker`, same image, `standalone`), so API and workers scale separately.
- **Starting the saga**: `POST /v1/orders` commits the order, then starts the workflow and
  returns `201 created`. If Temporal is down at that moment the order is still created; a
  reconciler next to the order-service worker starts sagas for `created` orders aged
  60s–1h (start is idempotent by workflow ID).
- **Timeouts and limits** (order-service settings): restaurant decision 5 min, rider
  offer 60s, at most 5 offers or 10 min of searching (looking again every 30s while no
  rider is free), maximum delivery distance 15 km (`MAX_DELIVERY_KM`, straight line,
  checked at order creation). Delivery-service: a rider with no location ping for 3 min
  counts as dropped out.

### Order and delivery endpoints

| Service | Endpoint | Who |
|---|---|---|
| order | `POST /v1/orders` — returns at `created`, starts the saga; `payment_method` `card_simulated` (approves) or `card_simulated_decline` | customer |
| order | `GET /v1/orders/restaurant/{restaurant_id}?status=` | owning restaurant admin, system admin |
| order | `POST /v1/orders/{id}/restaurant-response` `{decision, reason?}` → 202 | owning restaurant admin |
| order | `POST /v1/orders/{id}/cancel` `{reason?}` → 202, until pickup | ordering customer, system admin |
| order | `POST /v1/orders/{id}/refund` `{amount?, reason}` → 202, delivered orders, once | system admin |
| delivery | `GET /v1/riders/me/offer` | rider |
| delivery | `POST /v1/riders/me/deliveries/{id}/accept`, `/decline`, `/drop-out`, `/picked-up`, `/delivered` → 202 | the rider it's offered/assigned to |
| delivery | `GET /v1/deliveries/by-order/{order_id}` | ordering customer, the rider, system admin |
| tracking | `GET /v1/tracking/orders/{id}` (SSE) | ordering customer, system admin |
| tracking | `GET /v1/tracking/restaurants/{restaurant_id}` (SSE) | owning restaurant admin, system admin |

Every action endpoint checks ownership and state, then signals the saga and returns 202;
only the saga changes order and delivery status, so a rider's pickup can't race a
customer's cancellation.

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
  `restaurants.active_order_count` (capacity in use: reserved, not yet picked up),
  `stock_reservations` / `promotion_redemptions` (one row per order: what the saga took,
  so reserve and release are idempotent),
  `promotions` (discount codes — backs "pricing and discounts must be applied"; full CRUD;
  `code` unique **per restaurant**, not globally, so two restaurants can each run their own
  `"WELCOME10"`), `outbox_events`.
- **`order_db`**: `orders` (**order_number** — human-facing sequential ID separate from the
  UUID; **delivery_address_text/lat/lng** — snapshotted at order time, not a live
  reference; **subtotal/tax_amount/delivery_fee/discount_amount/promo_code/total_amount**
  — full pricing breakdown; **restaurant_admin_id/restaurant_lat/lng** — snapshotted from
  the quote, so restaurant-admin actions are authorized locally; **payment_method**;
  **refunded_amount/refunded_at**; **status_reason**;
  **confirmed_at/rejected_at/cancelled_at/assigned_at/picked_up_at/delivered_at**;
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
  `deliveries` (incl. **customer_id**; **pickup_lat/lng, dropoff_lat/lng, distance_km**
  snapshotted at creation; **ready_at, offer_expires_at, pickup_eta, dropoff_eta**;
  **assigned_at/picked_up_at/delivered_at/cancelled_at**; **reassignment_count**;
  `version` column), `delivery_offers` (every offer and how it ended),
  `delivery_status_history` (audit trail), `rider_verification_history` (audit trail),
  `outbox_events`. High-frequency rider location pings go to MongoDB
  (`delivery_db.rider_location_pings`, TTL-indexed at 24h via `app/mongo.py`, written on
  every `PATCH /v1/riders/me/location` call), not Postgres. The live position is in Redis
  (`rider:{id}:location`, and the GEO set `riders:available` while the rider is free);
  `riders.last_lat/lng/last_location_at` is a copy written at most every 30s. `_id` is `sfo_common.new_id()`, same as
  every other ID in the system (see
  [technical-documentation.md](./technical-documentation.md#primary-keys-uuidv7-generated-in-python-not-v4-not-db-side)).
- **`payment_db`**: `payments` (incl. **customer_id** [opaque, added for ownership checks
  on `GET /v1/payments/by-order/{id}` — see
  [Authorization audit](#authorization-audit-2026-09-29)], **payment_method,
  failure_reason, refunded_amount/refunded_at, authorized_at/captured_at/voided_at**,
  `idempotency_key`), `refunds` (one row per refund workflow ID, so a retried refund
  can't pay out twice), `outbox_events`.
- **`notification_db`**: `notifications` (one row per recipient and channel: **user_id,
  recipient_role, event_id** (the event that caused it), **related_order_id**, type,
  channel `in_app | email | sms`, content, status `pending | sent | failed`,
  **retry_count/last_error/last_attempt_at, sent_at/read_at**); `processed_events`
  (`event_id` PK, consumer, processed_at — consumer dedup, written in the same
  transaction as the notifications).
- **`analytics`** (MongoDB, analytics-service): `order_facts` (one document per order,
  `_id` = order ID — milestone timestamps, restaurant, amount, reasons, current delivery
  and reassignments, refunds), `rider_shift_facts` (one per on/off-shift change),
  `failed_events` (one per dead-lettered message). Non-authoritative: rebuilt by
  replaying the topics.
- **`outbox_events`** (order, payment, delivery and restaurant DBs): id, aggregate_id,
  event_type, payload, correlation_id, created_at, **dispatched_at** (null until
  relayed; partial index on undispatched rows), **trace_context** (the writer's
  `traceparent`), **publish_attempts/last_error** (set when Kafka refused it).

**Audit trail**: the requirements call for "order history and audit trails." Each service
that owns a status-bearing entity keeps an append-only history table, written **in the same
transaction** as the status change so the trail can never disagree with the current status:

| Table | Records | Read via |
|---|---|---|
| `order_db.order_status_history` | every order transition, with the actor (customer, restaurant admin, or the saga) and reason | embedded in every `OrderResponse` as `status_history` |
| `delivery_db.delivery_status_history` | every delivery transition, with the rider and reason (declined, expired, dropped…) | — (audit/debugging) |
| `restaurant_db.restaurant_status_history` | onboarding, approve, suspend — actor, role, optional reason | `GET /v1/restaurants/{id}/status-history` (owner or `system_admin`) |
| `delivery_db.rider_verification_history` | registration, approve, suspend | `GET /v1/riders/{id}/verification-history` (`system_admin`) |

Each row stores `from_status`, `to_status`, `actor_id`, `actor_role`, `reason`, and
`created_at`. No-op transitions (approving an already-active restaurant) record nothing.
The Kafka event log adds a second, cross-service, replayable view of the same facts (kept
7 days), but it isn't what the audit trail depends on.

## Auth, token lifetime & blocked-user handling

- **Access tokens**: stateless JWT, 15-minute lifetime, role claims, signed with an
  asymmetric key ([Access-token signing](#access-token-signing)). Verified by every
  service independently (see [Defense-in-depth auth](#defense-in-depth-auth)), not just the
  gateway — signature + expiry only, no DB call.
- **Refresh tokens**: tracked server-side in `user_db` (SHA-256 of the 256-bit random
  secret — a slow hash adds nothing for a secret that can't be guessed), 7-day lifetime, rotated on
  every use via an atomic compare-and-set, so two concurrent refreshes with one token can't
  both succeed. `/v1/auth/refresh` checks the record isn't revoked/expired and checks
  `is_blocked` — the authoritative block check. Presenting an *already-rotated* token again
  is treated as theft: every refresh token for that user is revoked (the real owner just
  logs in again). Expired rows are deleted hourly in small batches by a background task in
  `user-service` (`app/maintenance.py`; a Postgres try-lock means one replica does it per
  run). Revoked-but-unexpired rows are kept, since reuse detection depends on them. Logout requires the token's secret, not just its lookup ID.
- **Credentials**: emails are lowercased at the API boundary; passwords must be 8–128
  characters at registration; login does the same argon2 work for unknown emails as for a
  wrong password, so response time doesn't reveal which emails exist. argon2 runs in a
  worker thread (at most 4 at once per process), never on the event loop — inline, each
  ~50ms hash would stall every other request on that process.
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
  enter the assignment pool. The assignment claim also requires
  `verification_status = 'approved'`, as defense in depth.
- **Creating the first `system_admin`**: since `system_admin` is excluded from
  `PUBLIC_ROLES` (self-registration would let anyone grant themselves the role that can
  block any user and approve/suspend any restaurant or rider), there is **no HTTP
  endpoint** that creates one. It's a command inside `user-service`, run by an operator:
  `docker compose exec -T user-service python -m app.create_admin --email ... --full-name
  ... --phone ... --password-stdin` (`app/create_admin.py`; in Kubernetes, a one-off Job
  or `kubectl exec`). Only someone who can already run commands in that container can
  create an admin, so there is nothing reachable from outside to attack and no shared
  secret to manage. The password is read from stdin or a prompt, never from an argument
  (which would show in `ps` and shell history), and checked by the same rules as
  self-registration. It is **one-shot**: once any `system_admin` exists it exits with code
  3 and creates nothing; a Postgres advisory lock makes two runs at once create only one.
  `scripts/seed.py` runs it for the demo admin (treating exit code 3 as done) and then
  approves the seeded restaurant and riders. This replaced `POST
  /v1/auth/bootstrap-admin`, which was gated by a shared `ADMIN_BOOTSTRAP_SECRET` and
  reachable through the gateway.

## Real-time delivery tracking

- **Rider → server (location ingestion)**: plain REST `PATCH /v1/riders/me/location`,
  ~every 5–15s. Stateless, so any `delivery-service` replica can handle any rider's
  ping — no connection-affinity needed. Each ping updates Redis, appends a Mongo ping,
  and (while the rider has an order) publishes the position to that order's channel.
- **Server → customer and restaurant (live feeds)**: Server-Sent Events from
  `tracking-service`, through the gateway's streaming proxy.
  - `GET /v1/tracking/orders/{id}`: a `snapshot` (order status, delivery status, rider,
    ETAs, last position), then `status`, `delivery` and `location` events; the stream
    ends after `delivered`, `cancelled` or `rejected`.
  - `GET /v1/tracking/restaurants/{id}`: a `snapshot` of orders awaiting a decision, then
    `new_order` and `order_cancelled`. This is how a restaurant "receives the order
    notification" in real time; notification-service also sends it in-app and by SMS.
- **How it's wired**: the latest state lives in Redis (`tracking:order:{id}:state` hash;
  `tracking:restaurant:{id}:pending` set), and each change is `PUBLISH`ed to
  `tracking:order:{id}` / `tracking:restaurant:{id}` together with the state write.
  - **Order status** (placed → … → delivered, rejected, cancelled): from Kafka.
    `tracking-service`'s consumer (group `tracking-service`, `order-events`,
    `app/projection.py`) applies each order event in one Lua script: state, pending set
    and publishes, all or nothing. It never moves an order backwards (statuses only go
    forward), so duplicates, replays and events older than a rebuilt state change
    nothing. While Redis is down the consumer waits and retries (an outage, not a poison
    message), then catches up — a status update can't be lost. Events older than a day
    (their state would have expired) are skipped, so the group can start at the oldest
    events without filling Redis with finished orders. order-service still writes the
    state itself when an order is created (trackable at once) and when rebuilding a
    missing key.
  - **Delivery details** (delivery status, ETAs) and **rider positions**: published by
    delivery-service directly, best-effort — positions arrive every few seconds, and the
    order's own progress (assigned, picked up, delivered) comes through Kafka.
  - Kafka adds up to about a second (the outbox relay runs every second) to a status
    update, for the guarantee that it arrives.

  `tracking-service` subscribes
  *before* reading the snapshot, so nothing falls between the two, and checks ownership
  from the same state (`customer_id`, `restaurant:{id}:owner`) before streaming — 403/404
  are normal JSON errors. If a state key is missing (e.g. Redis was flushed) it rebuilds
  it from order-/restaurant-service's `/internal` endpoints.
- **Scaling**: one shared Redis Pub/Sub connection per `tracking-service` replica; a
  channel is subscribed only while one of that replica's clients watches it, so a replica
  receives only what its own clients need. A client too slow to keep up (100 queued
  events) is disconnected and reconnects for a fresh snapshot. Pub/Sub keeps nothing, and
  redis-py reconnects (and re-subscribes) without telling the caller, so the hub registers
  for every reconnect and read error and then ends all its open streams: each client
  reconnects for a fresh snapshot instead of showing a status it missed. Heartbeat comment every
  15s; the gateway's tracking pool has its own 1,000 connections, so long-lived streams
  can't starve REST traffic.
- **Auth**: bearer header, or `?access_token=` for browser `EventSource` (which can't send
  headers) — the gateway moves it into the `Authorization` header and strips it from the
  forwarded URL, so it isn't logged. Auth is checked when the stream opens; a token
  revoked mid-stream keeps streaming until the client reconnects (access tokens last 15
  min). No per-user cap on concurrent streams yet.

## Gateway-side auth enforcement

Originally, `api-gateway` only *read* a bearer token if present (to check the blocked-user
deny-list — see below); it never rejected a request for a missing/invalid/expired token,
relying entirely on each backend service's own defense-in-depth check to 401 it. That meant
an unauthenticated request to a private route still cost a wasted network hop to a backend
that was always going to reject it.

`app/security.py`'s `authenticate` now verifies signature, expiry and claims at the gateway
itself (via `sfo_auth.decode_access_token`, the same check every backend service runs), and
checks the blocked-user deny-list with the same decoded token, before forwarding — for every route *except* an explicit public-route allowlist
(`_PUBLIC_ROUTES` in the same file): `POST /v1/auth/{register,login,refresh,logout}`
(these authenticate via a refresh token, or issue the token itself — there's no bearer
token to check yet) and the read-only restaurant-browsing
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
be reached within the 1s connect timeout) is a `502 bad_gateway`, one that accepts but
doesn't answer within its timeout (10s for `order-service`, 5s for the rest) is a
`504 gateway_timeout`.

**Per-service connection pools (bulkhead)**: the gateway holds a separate HTTP connection
pool for each backend service (`_UPSTREAM_POLICIES` in `app/proxy.py`), not one shared pool.
A backend that slows down fills only its own pool, so a slow `order-service` can't starve
requests to a healthy `restaurant-service`. A request that can't get a connection within
0.5s gets `503 upstream_unavailable` ("busy, retry") instead of queueing behind the slow
requests. Scaling gateway or backend replicas doesn't replace this: when a service is slow
because of something behind it (its DB, a downstream call), more replicas add more waiting
requests, not more capacity. The gateway never retries: most
requests aren't safe to replay, and the client holds the `Idempotency-Key` where one
matters. The proxy strips hop-by-hop headers (RFC 9110 §7.6.1) in both directions, rejects
`..` path segments, and replaces a malformed client `X-Request-ID`.

This is additive, not a replacement for [Defense-in-depth auth](#defense-in-depth-auth)
below — every service still independently re-verifies the same token regardless of what the
gateway already checked. The gateway check is purely a fast-fail: reject obviously-bad
requests before spending a network hop on a backend call that was always going to fail.

## Access-token signing

Access tokens are signed with **Ed25519** (JWT `alg: EdDSA`); only `user-service` holds
the private key. Every other service, and the gateway, hold public keys only, so a
compromised service can read tokens but cannot forge one. (This replaced a single HS256
secret shared by all nine services, where any one of them could mint an admin token.)

- **Issuing**: `user-service` signs with the key in `JWT_PRIVATE_KEY_FILE` (a Docker secret
  mounted only into that container; `scripts/gen_jwt_key.py` creates the dev key in the
  git-ignored `secrets/`). It loads the key at start-up, so a missing or wrong key stops the
  service instead of failing the first login. Claims: `iss`, `sub`, `role`, `iat`, `exp`.
- **Publishing**: `GET /.well-known/jwks.json` on `user-service` (RFC 7517). It is not under
  `/v1`, so the gateway never routes it; it's reachable by the other services only.
- **Verifying** (`libs/auth`, `sfo_auth.keys.JwksKeyStore`): each process fetches the JWKS
  on the first token it sees (nothing at start-up, so no boot-order dependency on
  `user-service`) and caches it for 5 minutes; after that a cached key keeps verifying
  while the refresh runs in the background, so requests never wait on `user-service` for a
  key they already have. Expiry and `iat` allow 10s of clock skew. The algorithm is pinned to EdDSA and the
  issuer is checked; `alg: none`, HS256 "signed" with the public key, an unknown or missing
  `kid`, a wrong issuer, and an expired token are all rejected (tests in `libs/auth` and
  `api-gateway`).
- **Outages**: if `user-service` is unreachable, cached keys keep verifying tokens for up to
  an hour. With nothing cached, private routes answer **503** (the token may be fine, so
  not 401); public routes keep working, and `get_current_user_optional` treats the caller
  as anonymous (less access, never more). An unknown `kid` triggers at most one refetch
  per 30s, so random key IDs can't flood `user-service`.
- **Rotation** (no downtime): `kid` is the RFC 7638 thumbprint of the public key, so key IDs
  need no configuration.
  1. `scripts/gen_jwt_key.py --next` creates the next key and adds its public half to
     `JWT_ADDITIONAL_PUBLIC_KEYS_FILE`. Restart `user-service`, then wait 5 minutes (the
     cache TTL), so every service has the new key *before* any token is signed with it.
  2. `scripts/gen_jwt_key.py --promote` makes the next key the signing key and publishes the
     old one's public half. Restart `user-service`; tokens signed with the old key still
     verify.
  3. After one token lifetime (15 minutes) empty the additional-keys file and restart. Other
     services drop the old key within 5 minutes of the JWKS changing.
  Skipping step 1's wait still works, but the other services only learn a new `kid` on their
  next allowed refetch (at most every 30s), so new tokens can get 401s until then. Don't
  delete the private key without rotating: every issued token would stop verifying.
- **Known limitation**: services fetch the JWKS over plain HTTP inside the Docker network, so
  whoever can impersonate `user-service` there could serve their own keys. Closing that
  needs authenticated service-to-service transport (mTLS or network policy); the keys
  themselves are public.

## Defense-in-depth auth

Only `api-gateway` verifying JWTs isn't enough — inside Docker Compose, every service is
reachable directly by hostname from any other container. Every service independently
re-verifies the JWT signature (against the public keys, see [Access-token
signing](#access-token-signing)) and role claims via the shared `libs/auth` dependency.

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
  `event_type` in the envelope and in a header, keyed (partitioned) by aggregate ID, plus
  a `.dlq` twin each. Created by `kafka-init`, never auto-created. Consumer group = the
  consuming service's name.
- **Events**: past-tense names (`OrderPlaced`); payloads carry the IDs consumers need
  (customer, restaurant admin, rider user) so no consumer calls another service; a
  payload change must stay backward compatible (`libs/events/tests/test_schema_compat.py`).
- **Temporal task queues**: one per service — `order-saga` (workflows),
  `order-activities`, `restaurant-activities`, `payment-activities`,
  `delivery-activities` (`sfo_contracts.TaskQueue`). Workflow IDs: `order-{id}`,
  `refund-{order_id}`.
- **Request ID (`X-Request-ID`)**: generated at the gateway if absent (a valid client value
  is kept), returned on every response, and forwarded on every downstream call — by the
  gateway, and by service-to-service clients through the shared
  `sfo_common.middleware.propagate_correlation_id` httpx hook. Every JSON log line carries
  it as `request_id`; it's also stored on outbox rows (`correlation_id`) and carried into
  Kafka headers. To debug one request: take `X-Request-ID` from the response, then
  `docker compose logs --no-log-prefix | grep <id>` shows its path through every service.
- **Tracing**: OpenTelemetry, set up once per process in `app/__init__.py`
  (`sfo_observability.setup_tracing`); service name = the compose service
  (`OTEL_SERVICE_NAME`, so `order-worker` isn't reported as order-service). The trace
  follows an order through HTTP, Temporal, the outbox, Kafka and Celery; log lines carry
  `trace_id`/`span_id`. Background pollers run inside `quiet()` so they don't start a
  trace every tick.
- **Metrics**: every process serves Prometheus metrics on `METRICS_PORT` (9100), Temporal
  workers also on 9101 — internal ports only, never routed by the gateway. Names follow
  Prometheus conventions (`_total` counters, `_seconds` units); HTTP metrics are labelled
  by route template, never by raw path; a counter an alert watches is created at 0 when
  the process starts.
- **Logging**: JSON on stdout, never files — Docker rotates it (10 MB × 3 per container,
  `docker-compose.yml`) and Grafana Alloy ships it to Loki. Every service writes one
  access line per request (`logger: sfo.request` — method, path, status, duration;
  health probes skipped); uvicorn's own logs go through the same JSON formatter and its
  plain-text access log is off.
- **Idempotency**: `Idempotency-Key` request header, required on `POST /v1/orders`. Saga
  activities are idempotent by order (one reservation, payment, delivery per order) and
  refunds by refund workflow ID, so a Temporal retry never applies a step twice.
- **Error responses**: `{"error": {"code", "message", "details"}}` envelope, shared handler
  in `libs/common`.
- **API versioning**: all routes under `/v1/...`.
- **Request validation**: every request body is a `sfo_common.schemas.RequestModel` —
  unknown fields are a 422 (not silently dropped), strings are whitespace-trimmed
  (passwords excepted), and every string and list has a max length (IDs ≤ 64, free text
  ≤ 500–1000, ≤ 50 order lines). Phone numbers are E.164; self-registration roles are a
  closed set. The gateway refuses bodies over 1 MB with `413` before buffering them.
- **Pagination**: list endpoints return `{"items": [...], "next_cursor": "..."}`
  (`sfo_common.pagination.Page`), newest first, with a bounded `limit`; pass `?cursor=` from
  the previous page to get the next. Keyset, not offset: ordered by `(created_at, id)` —
  `id` breaks ties, and `created_at` never changes, so rows can't be skipped or repeated
  between pages, even while new rows are being inserted. Each list has a matching
  `(filter columns…, created_at, id)` index. Small bounded lists (a restaurant's menu and
  categories, an owner's restaurants, a user's addresses) aren't paginated.
- **Rate limiting**: keyed by user ID for requests with a valid token, by client IP
  otherwise — per-IP alone would throttle unrelated customers behind one mobile-carrier
  IP, and user+IP would hand a user a fresh limit every time their IP changes.
  `/v1/auth/login` and `/v1/auth/register` share a stricter, separate limit. Fails open if Redis is unreachable (the blocked-user check fails closed).
  The rate-limit counter and the blocked-user lookup go to Redis in one pipelined
  round-trip per request.
- **Database connections**: explicit per-replica pool (`DB_POOL_SIZE` 10 +
  `DB_MAX_OVERFLOW` 5), 3s pool wait then `503 service_busy`, Postgres
  `statement_timeout` 5s and `idle_in_transaction_session_timeout` 15s
  (`sfo_common.db.make_engine`). Total connections per database = replicas × 15.
- **Logging hygiene**: passwords, tokens, and payment details are never logged, enforced
  via a shared log-sanitizing helper in `libs/observability`.
- **Gateway scope**: routing, auth, rate limiting, CORS, Swagger proxy/index, streaming
  proxy for SSE — no cross-service response aggregation in Part A.
- **Health checks**: every service exposes `GET /livez` (process is up — no dependency
  checks, so a DB outage never makes an orchestrator restart every replica at once) and
  `GET /readyz` (dependencies reachable — a failing replica is taken out of load balancing,
  not restarted). `GET /health` is the same as `/readyz`, kept for `docker-compose.yml`'s
  healthchecks and `depends_on: condition: service_healthy`.
- **Public-read caching**: `restaurant-service` caches an *active* restaurant's detail,
  menu and categories in Redis (60s TTL, `app/cache.py`), invalidated after every write to
  that restaurant; those responses and the public restaurant list also carry
  `Cache-Control: public, max-age=30`, while an owner/admin viewing a pending/suspended
  restaurant gets `private, no-store`. A Redis error is just a cache miss. Displayed prices
  may be up to a TTL stale; the price charged is always re-read uncached by the order
  quote.
- **CORS**: handled once, at `api-gateway` only.
- **Seed/demo data**: `scripts/seed.py` creates a demo restaurant + menu, two approved
  riders (reassignment needs a second one), a customer and the system admin.

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
    prometheus/    # prometheus.yml (scrape targets), rules.yml (alerts)
    grafana/       # provisioning/ (data sources, dashboards), dashboards/*.json,
                    # build_dashboards.py (generates the dashboards)
    alloy/         # config.alloy: Docker logs → Loki
  libs/
    common/        # FastAPI app factory, config, error handlers, IDs, pagination,
                    # Temporal client/worker helpers, test helpers
    auth/           # JWT issue/verify, role dependency, deny-list
    contracts/      # saga contracts: task queues, activity/signal names and models
    events/         # event envelope + typed payloads, outbox mixin, Kafka (de)serializers,
                    # outbox relay, consumer loop, idempotency, pipeline metrics;
                    # schemas/ (JSON Schema snapshots)
    observability/  # JSON logging with redaction; OpenTelemetry + Prometheus wiring
  services/
    api-gateway/
    user-service/
    restaurant-service/
    order-service/        # app/ (API, activities); app/workflows/ (order saga, refund)
    delivery-service/
    payment-service/
    tracking-service/     # SSE feeds, Redis only
    notification-service/
    analytics-service/    # Kafka consumer → Mongo read model; metrics API
  docs/
    PRD.md
    architecture.md
    technical-documentation.md
    diagrams.md
    PROGRESS.md
  scripts/
    seed.py
    smoke_test.py   # end-to-end through the gateway (--deep, --lifecycle, --events)
    check.sh        # lint, types, tests (--with-db: real Postgres/Redis)
  README.md
```

Each service directory: `pyproject.toml` (workspace member), `Dockerfile`,
`entrypoint.sh` (runs `alembic upgrade head`, then uvicorn — safe with many replicas
starting at once: migrations take a Postgres advisory lock, so one replica migrates and
the rest wait, then find nothing to do; see `sfo_common.db.acquire_migration_lock`.
Migrations must stay backward-compatible with the previous release, since old replicas
keep serving during a rolling deploy), `env.example`, `app/` (API
routers, models, schemas, repository; `activities.py` and `worker.py` in services with saga
steps), `alembic/` + `alembic.ini`, `tests/`. `tracking-service` has no database, so no
`alembic/`.
