# SmartFoodOps — Product Requirements

Part A (Weeks 1–3) is the core ordering and delivery backend. Part B (the GenAI layer,
Weeks 4–5) builds on top of it: Week 4 is data preparation and retrieval (semantic search),
in [§8](#8-part-b--week-4-data-preparation-and-retrieval); Week 5, the AI assistant, will be
added when it's built. This document states *what* the platform must do and
traces each requirement to where it's built. For *how*, see
[architecture.md](./architecture.md) and
[technical-documentation.md](./technical-documentation.md). For live status, see
[PROGRESS.md](./PROGRESS.md).

## 1. Problem

QuickServe's current systems are slow and partly manual at checkout. They hold inconsistent
menu and inventory data, slow down at peak hours, dispatch riders inefficiently, send
notifications unreliably, and spread analytics across separate systems. SmartFoodOps
replaces them with a backend that stays correct under partial failure and scales to
lunch- and dinner-time peaks.

## 2. Users and roles

| Role | Can |
|---|---|
| `customer` | Register, browse active restaurants and menus, place and track orders, view order history, cancel before pickup |
| `restaurant_admin` | Onboard restaurants (pending approval), manage menus, categories, availability and promotions, accept or reject orders, mark food ready |
| `rider` | Register (pending approval), set availability, report location, accept deliveries, update pickup and delivery status |
| `system_admin` | Approve or suspend restaurants and riders, block or unblock users, view audit trails, refund delivered orders, mark an overdue order delivered |

The first `system_admin` is created by a one-shot command run inside `user-service`
(there is no endpoint for it). No one can self-register as an admin.

## 3. Key use cases

- **UC-1 Place an order.** A customer picks items from an active restaurant and submits.
  The system validates availability, stock and capacity, prices the order server-side,
  authorizes payment, and waits for the restaurant to accept.
- **UC-2 Restaurant accepts or rejects.** The restaurant responds within a time limit. A
  rejection or a timeout releases stock and voids the payment.
- **UC-3 Dispatch.** Once the order is confirmed, the nearest approved, available rider is
  offered it. A decline or timeout moves the offer to the next rider; riders who declined
  are asked again after a short cooldown, and the search widens if nobody nearby is free.
- **UC-4 Track.** The customer sees live rider location and status until delivery,
  including when the restaurant marks the food ready.
- **UC-5 Complete.** Delivery triggers payment capture (final settlement) and frees the rider.
- **UC-6 Cancel and refund.** The customer cancels before pickup. Compensation depends on
  how far the order had progressed.
- **UC-7 Onboard and vet.** Restaurants and riders register as pending, and a
  `system_admin` approves or suspends them, with every decision recorded.
- **UC-8 Operate.** Operators view the analytics metrics and diagnose failed workflows.

## 4. Functional requirements

| ID | Requirement | Source (Part A spec) |
|---|---|---|
| FR-1 | Customer registration and profile management | §1 |
| FR-2 | Restaurant onboarding and profile setup, with admin approval | §1 |
| FR-3 | Menu management with categories and availability | §1 |
| FR-4 | Rider registration and availability tracking | §1 |
| FR-5 | Role-based access: customer, restaurant admin, rider, system admin | §1 |
| FR-6 | Order history and audit trails | §1 |
| FR-7 | Validate menu availability at order time | §2 |
| FR-8 | Check restaurant capacity | §2 |
| FR-9 | Apply pricing and discounts | §2 |
| FR-10 | Authorize payment | §2 |
| FR-11 | Confirm an order only after successful validation | §2 |
| FR-12 | Notify the restaurant of new orders; restaurant accepts or rejects | §2, Guidelines W2 |
| FR-13 | Trigger delivery assignment on confirmation | §2 |
| FR-14 | Assign a rider by availability and proximity; reassign on failure | §3 |
| FR-15 | Calculate pickup ETA | §3 |
| FR-16 | Track pickup and record delivery status updates in real time | §3 |
| FR-17 | Delivery completion triggers final settlement | §3 |
| FR-18 | Cancellation with refund, delivery reassignment and inventory rollback | Business goals |
| FR-19 | Domain events: placed, confirmed, payment successful, rider assigned, picked up, delivered, cancelled, refund processed | §4 |
| FR-20 | Notifications for order and delivery updates | §4, Guidelines W3 |
| FR-21 | Analytics: total orders, orders per restaurant, peak-hour load, average delivery time, rider utilization, cancellation rate, acceptance rate, delivery success rate, failed events | §5 |

## 5. Non-functional requirements

| ID | Requirement | Approach |
|---|---|---|
| NFR-1 | Partial failures must not corrupt order state | local ACID transactions, Temporal saga with compensation, validate-before-write |
| NFR-2 | Idempotent state updates; no duplicates or double-processing | Idempotency-Key on order creation, unique natural keys, `processed_events` consumer dedup |
| NFR-3 | High concurrency at peak times | async I/O throughout, stateless services, Kafka partitioning, atomic conditional updates for stock, capacity and rider claims |
| NFR-4 | Background tasks run independently of user flows, are traceable, and are recoverable | Kafka + Celery off the request path; transactional outbox; correlation IDs |
| NFR-5 | Consistent, durable, queryable operational data | database-per-service with one owner per fact; snapshots; optimistic locking |
| NFR-6 | Failures diagnosable in ordering, payment, assignment, tracking, notifications and background jobs | structured JSON logs with correlation IDs; Prometheus + Grafana; OpenTelemetry + Jaeger |
| NFR-7 | Security | JWT at the gateway and in every service, role checks, ownership checks, blocked-user deny-list, rate limiting, argon2 password hashing |
| NFR-8 | Reproducible local environment | Docker Compose with health-gated startup, per-service migrations, seed script |

## 6. Milestones

| Week | Milestone | Exit criteria |
|---|---|---|
| 1 | Foundation and core services | services scaffolded, schemas finalized (Postgres + Mongo), basic APIs incl. order creation working, `docker compose up` healthy, architecture diagram |
| 2 | Order and delivery workflow | Temporal saga end to end (created → delivered), accept/reject, rider assignment and reassignment, payment simulation, cancellation with compensation |
| 3 | Events and observability | outbox → Kafka relay, notification and analytics consumers, Celery workers, Prometheus/Grafana dashboards, Jaeger traces across services |
| 4 | Part B: data preparation and retrieval | embedding pipeline, vector DB populated, semantic search, retrieval API ([§8](#8-part-b--week-4-data-preparation-and-retrieval)) |
| 5 | Part B: AI assistant and streaming | to be specified with Week 5 |

## 7. Traceability

Status: ✅ built · 🟡 partial · ⏳ planned (week).

| Req | Where it lives | Week | Status |
|---|---|---|---|
| FR-1 | `user-service`: `auth/register`, `users/me`, `users/me/addresses` | 1 | ✅ |
| FR-2 | `restaurant-service`: `POST restaurants` (pending), `approve`/`suspend`, `mine`, `admin` | 1 | ✅ |
| FR-3 | `restaurant-service`: categories and items CRUD, `is_available`, `stock_count` | 1 | ✅ |
| FR-4 | `delivery-service`: `riders`, `riders/me/status`, `riders/me/location` (+ Mongo ping history), approve/suspend | 1 | ✅ |
| FR-5 | `libs/auth` role dependencies in every service; gateway fail-closed public-route list | 1 | ✅ |
| FR-6 | order history (`GET orders`); audit tables `order_status_history` (every transition, with actor and reason), `delivery_status_history`, `restaurant_status_history`, `rider_verification_history` | 1 / 2 | ✅ |
| FR-7 | availability and stock checked at creation; stock reserved atomically in the saga (`reserve_order`) | 1 / 2 | ✅ |
| FR-8 | `is_accepting_orders` checked at creation; `max_concurrent_orders` enforced atomically in the saga, freed at pickup | 1 / 2 | ✅ |
| FR-9 | server-side item pricing; promotion redemption, 5% tax and distance-based delivery fee in the saga | 1 / 2 | ✅ |
| FR-10 | `payment-service` `authorize` (simulated card), idempotent per order | 2 | ✅ |
| FR-11 | order stays `created` until reservation, pricing and payment succeed; `confirmed` only after the restaurant accepts | 2 | ✅ |
| FR-12 | restaurant new-order live feed (SSE, `tracking-service`) and a notification (in-app + SMS, from `OrderAwaitingRestaurant`); `POST orders/{id}/restaurant-response` → Temporal signal, 5-min timeout | 2 / 3 | ✅ |
| FR-13 | saga creates the delivery and starts offering riders on `confirmed` | 2 | ✅ |
| FR-14 | Redis GEO nearest + atomic Postgres claim, approved + available riders only; reassignment on decline, 60s expiry and drop-out (explicit or 3 min without a ping) | 2 | ✅ |
| FR-15 | pickup and dropoff ETA from distance, vehicle speed and restaurant prep time; recomputed when the restaurant marks the food ready (`POST orders/{id}/food-ready`) | 2 | ✅ |
| FR-16 | rider pickup/delivered endpoints; `delivery_status_history` + `DeliveryStatusUpdated`, `DeliveryFoodReady`; live SSE feed via `tracking-service` (Redis Pub/Sub); pickup and delivery deadlines (late rider replaced, kitchen timeout cancels, overdue delivery flagged; admin `mark-delivered`) | 2 | ✅ |
| FR-17 | payment captured on `delivered` | 2 | ✅ |
| FR-18 | cancel until pickup + saga compensation (void, release stock/promotion, free the rider); rider reassignment; post-delivery refund workflow | 2 | ✅ |
| FR-19 | every event written as an outbox row in the same transaction as its change; published by Celery relay workers (`order-`, `payment-`, `delivery-worker`) to Kafka, typed JSON Schemas in Schema Registry (`libs/events`); plus `OrderAwaitingRestaurant` and `RiderShiftChanged` | 1–3 | ✅ |
| FR-20 | `notification-service` Kafka consumer → in-app, email and SMS (simulated) for customer, restaurant admin and rider; `notification-worker` (Celery) sends with retries, then `failed`; in-app inbox `GET /v1/notifications` | 3 | ✅ |
| FR-21 | `analytics-service`: Kafka consumer → MongoDB read model; all nine metrics at `GET /v1/analytics/summary`, per restaurant, peak hours; live counters on the Grafana *Orders & deliveries* dashboard | 3 | ✅ |
| NFR-1 | order creation is validate-then-single-transaction; saga compensation, idempotent compensations, retries until done after pickup; events can't be lost (outbox) | 1–3 | ✅ |
| NFR-2 | order Idempotency-Key; workflow ID per order; saga activities idempotent per order / refund; consumers dedupe (`processed_events`, idempotent upserts) — a full replay changes nothing (drill 2) | 1–3 | ✅ |
| NFR-3 | async I/O; stateless services; per-service gateway pools, timeouts, body-size limit; explicit DB pools; Redis cache for public reads; keyset pagination (W1). Atomic reservations and rider claims, Redis GEO assignment, SSE in a separate service (W2). Kafka partitioning by aggregate ID, workers scaled apart from APIs, notification sends off the request path (W3) | 1–3 | ✅ |
| NFR-4 | outbox relay and consumers off the request path; traceable (one trace per order, `trace_id` in every log line); recoverable (outbox retries, consumer retries and DLQ, outages waited out, Celery late acks + sweep) — verified by the failure drills | 1–3 | ✅ |
| NFR-5 | database-per-service, one owner per fact, snapshots on orders, optimistic `version` locks, `Decimal` money end to end; Mongo analytics non-authoritative | 1–3 | ✅ |
| NFR-6 | JSON logs with `request_id` + `trace_id` in Loki; OpenTelemetry traces in Jaeger; Prometheus metrics for every flow, 4 Grafana dashboards, 8 alerts | 1 / 3 | ✅ |
| NFR-7 | see FR-5; first admin created by a one-shot command, not an endpoint; strict request schemas (unknown fields rejected, size limits), 1 MB body limit, argon2 off the event loop; metrics and observability UIs not exposed beyond localhost | 1 / 3 | ✅ |
| NFR-8 | `docker-compose.yml` (30 containers, health-gated), `entrypoint.sh` migrations, `scripts/seed.py`, `scripts/smoke_test.py --deep --lifecycle --events`, `scripts/check.sh --with-db` | 1–3 | ✅ |

## 8. Part B — Week 4: data preparation and retrieval

**Problem.** Customers can only browse restaurant by restaurant; they can't ask for
*"something spicy under 800 near F-7"* or *"a light vegetarian lunch"*, and nothing uses
what reviews say or what a customer has ordered before. Week 4 makes the catalog,
reviews and order history searchable by meaning; Week 5's assistant answers on top of it.

**Use cases**

- **UC-9 Search the catalog.** Anyone describes what they want in plain words and gets
  matching dishes, restaurants, reviews or cuisines — only from active restaurants, only
  dishes orderable now — optionally near a location, under a price, or meeting dietary
  needs.
- **UC-10 Search my history.** A customer finds a past order by what was in it.
- **UC-11 Review an order.** A customer rates and reviews a delivered order, once;
  restaurants show their average rating.
- **UC-12 Keep search current.** A menu or restaurant change is searchable within seconds,
  without anyone re-running anything.

| ID | Requirement | Source (Part B spec) |
|---|---|---|
| FR-B1 | Richer domain data: dietary tags, spice level, calories, restaurant descriptions, reviews with ratings, enough seed data for search to be meaningful | Week 4: data preparation |
| FR-B2 | Embedding pipeline: documents per entity, chunked, embedded and stored | Week 4: embedding pipeline |
| FR-B3 | Vector DB populated from the existing data | Week 4: vector DB populated |
| FR-B4 | Semantic search over menus, restaurants, reviews and order history | Week 4: semantic search |
| FR-B5 | Retrieval API with filters, reusable by the assistant | Week 4: retrieval API |
| NFR-B1 | Index follows the source of truth automatically, consistently, and survives a vector DB outage | Guidelines (event-driven, reliability) |
| NFR-B2 | Retrieval quality is measured, not assumed | Guidelines (verification) |
| NFR-B3 | Search never exposes what a customer couldn't see or order; history is private | Part A NFR-7 |

**Traceability**

| Req | Where it lives | Status |
|---|---|---|
| FR-B1 | `restaurant-service` migration 0006, `POST/GET restaurants/{id}/reviews` (delivered order only, checked with order-service; one per order; atomic rating aggregate); `scripts/fixtures/catalog.json` (10 restaurants, 124 items, 200 reviews) | ✅ |
| FR-B2 | `ai-service` `documents.py` (context-prefixed documents, review chunking), `embedding.py` (bge-small + BM25, baked into the image), `indexer.py` on `restaurant-events` + `OrderDelivered` | ✅ |
| FR-B3 | `python -m app.reindex` from `/internal/catalog/export` and `/internal/orders/delivered`; 124 items, 10 restaurants, 212 review chunks, 258 orders + 41 customer summaries | ✅ |
| FR-B4 | `GET /v1/ai/search?kind=item\|restaurant\|review\|cuisine`, `GET /v1/ai/search/history` | ✅ |
| FR-B5 | `retrieve(query, filters)` (`app/retrieval.py`): hybrid dense + BM25, RRF, geo/price/dietary/restaurant filters | ✅ |
| NFR-B1 | outbox → `restaurant-events` with per-restaurant `source_version`; conditional upserts, tombstones, outage retry (no DLQ); edit → searchable in 0.4–1.7 s; drills: Qdrant down, reindex while serving | ✅ |
| NFR-B2 | `scripts/eval_retrieval.py`, 32 golden queries: recall@5 0.902, MRR 0.953; `check.sh --with-ai`; `smoke_test.py --ai` | ✅ |
| NFR-B3 | visibility filters inside `retrieve()`; history filtered by the caller's ID, customer-only; review events carry no customer ID | ✅ |
