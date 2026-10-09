# SmartFoodOps — Product Requirements (Part A)

Part A is the core ordering and delivery backend. Part B (the GenAI layer, Weeks 4–5) builds
on top of it and is out of scope here. This document states *what* the platform must do and
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
| `restaurant_admin` | Onboard restaurants (pending approval), manage menus, categories, availability and promotions, accept or reject orders |
| `rider` | Register (pending approval), set availability, report location, accept deliveries, update pickup and delivery status |
| `system_admin` | Approve or suspend restaurants and riders, block or unblock users, view audit trails |

The first `system_admin` is created through a one-shot bootstrap endpoint. No one can
self-register as an admin.

## 3. Key use cases

- **UC-1 Place an order.** A customer picks items from an active restaurant and submits.
  The system validates availability, stock and capacity, prices the order server-side,
  authorizes payment, and waits for the restaurant to accept.
- **UC-2 Restaurant accepts or rejects.** The restaurant responds within a time limit. A
  rejection or a timeout releases stock and voids the payment.
- **UC-3 Dispatch.** Once the order is confirmed, the nearest approved, available rider is
  offered it. A decline or timeout moves the offer to the next rider.
- **UC-4 Track.** The customer sees live rider location and status until delivery.
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
| NFR-3 | High concurrency at peak times | async I/O throughout, stateless services, Kafka partitioning, `SKIP LOCKED` rider claims |
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
| 4–5 | Part B (GenAI) | out of scope for this document |

## 7. Traceability

Status: ✅ built · 🟡 partial · ⏳ planned (week).

| Req | Where it lives | Week | Status |
|---|---|---|---|
| FR-1 | `user-service`: `auth/register`, `users/me`, `users/me/addresses` | 1 | ✅ |
| FR-2 | `restaurant-service`: `POST restaurants` (pending), `approve`/`suspend`, `mine`, `admin` | 1 | ✅ |
| FR-3 | `restaurant-service`: categories and items CRUD, `is_available`, `stock_count` | 1 | ✅ |
| FR-4 | `delivery-service`: `riders`, `riders/me/status`, `riders/me/location` (+ Mongo ping history), approve/suspend | 1 | ✅ |
| FR-5 | `libs/auth` role dependencies in every service; gateway fail-closed public-route list | 1 | ✅ |
| FR-6 | order history (`GET orders`); audit tables `order_status_history`, `restaurant_status_history`, `rider_verification_history` | 1 | ✅ (order transitions beyond `created` are recorded from W2) |
| FR-7 | `order-service` checks availability and stock against `restaurant-service` at creation; stock *reservation* in the saga | 1 / 2 | 🟡 checked in W1, reserved in W2 |
| FR-8 | `is_accepting_orders` checked at creation; `max_concurrent_orders` cap in the saga | 1 / 2 | 🟡 |
| FR-9 | server-side item pricing (W1); promo, tax and fee step in the saga (W2) | 1 / 2 | 🟡 |
| FR-10 | `payment-service` authorize (simulated), idempotent | 2 | ⏳ |
| FR-11 | order stays `created` until saga validation passes | 2 | ⏳ |
| FR-12 | restaurant new-order live feed (SSE) + `POST orders/{id}/restaurant-response` → Temporal signal with timeout ([week2-plan.md](./week2-plan.md) D11) | 2 | ⏳ |
| FR-13 | saga activity after `confirmed` | 2 | ⏳ |
| FR-14 | `delivery-service` assignment: Redis GEO nearest + atomic Postgres claim, approved + available riders only; reassignment on decline, expiry and drop-out | 2 | ⏳ (vetting gate on availability already enforced in W1) |
| FR-15 | ETA from haversine distance and prep time | 2 | ⏳ |
| FR-16 | delivery status endpoints + `DeliveryStatusUpdated`; SSE via `tracking-service` fed by Redis Pub/Sub | 2 | ⏳ |
| FR-17 | payment capture on `delivered` | 2 | ⏳ |
| FR-18 | cancel signal + compensation matrix ([technical-documentation.md#failure-handling](./technical-documentation.md#failure-handling)); post-delivery refund workflow | 2 | ⏳ |
| FR-19 | event schemas in `libs/events`; outbox rows (`OrderPlaced` written in W1); Kafka relay | 1 / 3 | 🟡 |
| FR-20 | `notification-service` Kafka consumer + Celery dispatch | 3 | ⏳ (schema + list endpoint in W1) |
| FR-21 | `analytics-service` | 3 | ⏳ |
| NFR-1 | order creation is validate-then-single-transaction (W1); saga compensation (W2) | 1 / 2 | 🟡 |
| NFR-2 | order Idempotency-Key (W1); payments and consumers (W2/3) | 1–3 | 🟡 |
| NFR-3 | async I/O; stateless services; per-service gateway connection pools, timeouts, body-size limit; explicit DB pools; Redis cache for public reads; keyset pagination (W1). Atomic reservations, Redis GEO assignment, Kafka partitioning (W2/3) | 1–3 | 🟡 |
| NFR-4 | transactional outbox rows (W1); Temporal workers separate from APIs (W2); Kafka + Celery consumers (W3) | 1–3 | 🟡 |
| NFR-5 | database-per-service, one owner per fact, snapshots on orders, optimistic `version` locks, `Decimal` money end to end | 1 | ✅ |
| NFR-6 | JSON logs with a `trace_id` propagated across services + one access line per request (W1); metrics and tracing (W3) | 1 / 3 | 🟡 |
| NFR-7 | see FR-5; bootstrap-admin one-shot + rate limited; strict request schemas (unknown fields rejected, size limits), 1 MB body limit, argon2 off the event loop | 1 | ✅ |
| NFR-8 | `docker-compose.yml`, `entrypoint.sh` migrations, `scripts/seed.py` | 1 | ✅ |
