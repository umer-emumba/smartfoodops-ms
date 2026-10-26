# Week 2 Implementation Plan — Order Lifecycle + Delivery Workflow

Status: **done** (steps 0–8, 2026-10-12; started 2026-10-09). Supersedes the Week 2 section of the original
Part A plan; it folds in the design changes from the Week 1 production review. Work goes
step by step (§3); `technical-documentation.md` and `architecture.md` are updated to match
what's actually built, and `PROGRESS.md` tracks progress.

**Goal (Guidelines, Week 2):** a full order → payment → restaurant acceptance → rider
assignment → pickup → delivery lifecycle through the gateway, with correct compensation
on rejection, timeout, failure and cancellation, plus live tracking for the customer.
PRD coverage: FR-7 to FR-18 (FR-19/20 events and notifications are written to the outbox
here; Week 3 publishes and consumes them). Requirement-by-requirement mapping: §8.

---

## 1. Design decisions (changes from the original plan)

| # | Decision | Why |
|---|---|---|
| D1 | **Each service runs its own Temporal worker on its own task queue**; the saga workflow lives in `order-service` and schedules activities onto `restaurant-activities`, `payment-activities`, `delivery-activities`, `order-activities` | Activities run next to the data they change — no internal HTTP/REST layer for saga steps, each service controls its own concurrency, Temporal supplies retries/timeouts end to end |
| D2 | **Workers run inside the service process in development** (started from the app's lifespan, like the refresh-token purge), and as **separate processes in production** (`python -m app.worker`, same image) — chosen by one setting, `TEMPORAL_WORKER_MODE=embedded\|standalone` | Development: no extra containers for no benefit. Production: separate processes give the API its own CPU (one core per Python process), and API and workers scale independently |
| D3 | **`POST /v1/orders` returns immediately** (`201`, `status=created`) after committing the order and starting the workflow; progress is pushed via tracking (§7) and visible on `GET /v1/orders/{id}` | The saga can wait minutes for a restaurant — the HTTP request must not |
| D4 | **Workflow ID = `order-{order_id}`**, started right after the order commits; a **reconciler** re-starts workflows for any `created` order older than 60s (start is idempotent by ID) | Closes the commit-then-start dual-write gap: a crash between the two can't strand an order |
| D5 | **Stock and capacity are reserved atomically** in one `restaurant_db` transaction: `UPDATE … WHERE stock_count >= :qty` and `active_order_count < max_concurrent_orders`, recorded in a `stock_reservations` row keyed by `order_id` | No overselling under concurrency; the reservation row makes reserve/release idempotent across activity retries |
| D6 | **Rider live positions in Redis GEO** (`GEOADD`/`GEOSEARCH`); Postgres `riders.last_lat/lng` written at most every 30s per rider; assignment = `GEOSEARCH` nearest + one atomic `UPDATE riders … WHERE status='available' RETURNING` claim | Indexed proximity search in microseconds instead of a full haversine scan, and ~30× fewer Postgres writes from location pings |
| D7 | **Location pings never enter the workflow** — only status transitions are signals | Keeps workflow history small (Temporal caps it at ~50k events) |
| D8 | **Live tracking = SSE, via a new stateless `tracking-service` that only talks to Redis.** Publishers (order-service, delivery-service) `PUBLISH` each update to `tracking:order:{id}` and also `SET` the order's latest state (status, rider position, `customer_id`, `restaurant_admin_id`) in `tracking:order:{id}:state`; the tracking-service reads that key for both the ownership check and the on-connect snapshot. The gateway gets a streaming proxy for it | No database and no calls to other services, so it's cheap to run and doesn't depend on them being up; each replica only receives messages for the connections it holds; long-lived connections are isolated from REST traffic. One small container |
| D9 | **Activity contracts live in a new `libs/contracts`** (task-queue names, activity/signal names, input/output dataclasses) | The workflow in order-service and the activities in other services share one typed contract — plumbing only, no business logic (same rule as `libs/events`) |
| D10 | **Restaurant ownership is snapshotted onto the order** (`orders.restaurant_admin_id`, from the quote) | Order-service can authorize restaurant-admin actions locally, without a cross-service call per request |
| D11 | **Restaurants get new orders pushed**, on a live feed from the same tracking-service (`tracking:restaurant:{id}`) | Part A: "Restaurant must receive order notification" — polling a queue isn't a notification. Push to the restaurant dashboard is real-time UI; Week 3's notification-service adds email/SMS/push on top |
| D12 | **A rider who drops out after accepting is replaced** until pickup — explicit "can't complete" from the rider, or no location ping for 3 min | Part A: "failure recovery and reassignment". After pickup the food is with the rider, so it becomes a support case, not an automatic reassignment |
| D13 | **Post-delivery refunds run as their own short workflow** (`OrderRefundWorkflow`, ID `refund-{order_id}`), started by a system admin | The order saga has finished by then (it can't be signalled). Every cancellation before delivery is a *void* (payment captured only at delivery), so this is the path that exercises a real refund |

---

## 2. End-to-end flow

```
POST /v1/orders ──► order row (created) + OrderPlaced outbox ──► start workflow order-{id}
                                                                       │
OrderSagaWorkflow ─────────────────────────────────────────────────────┘
  1. restaurant.reserve_order(order, items)          stock + capacity, atomic   ─┐
  2. restaurant.redeem_promotion(code)  [if promo]   atomic times_used++         │ compensation
  3. order.apply_pricing(subtotal, discount, fees)   tax + delivery fee          │ (reverse order):
  4. payment.authorize(order, amount)                simulated card               │ void/refund payment
  5. order.transition → awaiting_restaurant_acceptance                           │ release promotion
  6. wait: signal restaurant_response | cancel | timeout (5 min)                 │ release reservation
       rejected / timeout / cancel ──► compensate ──► rejected | cancelled      ─┘
  7. order.transition → confirmed
  8. delivery.create_delivery(pickup, dropoff)
  9. loop (max 5 offers / 10 min):
       delivery.offer_to_nearest_rider → wait: signal rider_response | timeout (60s)
       declined/expired ──► delivery.release_rider, reassignment_count++
     no rider ──► compensate ──► cancelled (status_reason=no_rider_available)
 10. delivery.compute_eta → order.transition → assigned (RiderAssigned)
 11. wait: signal picked_up | cancel | rider_dropped   (cancel allowed until pickup)
       rider_dropped ──► delivery.release_rider(dropped) ──► back to step 9
 12. order.transition → picked_up; restaurant.complete_reservation (frees capacity)
 13. wait: signal delivered
 14. payment.capture; order.transition → delivered; delivery.release_rider
```

Signals into the workflow come from REST endpoints in the owning service (restaurant
response → order-service; rider accept/pickup/delivered/drop-out → delivery-service;
customer cancel → order-service), each using a Temporal client to
`signal_workflow("order-{id}")`. A stale-rider detector in delivery-service's worker sends
`rider_dropped` too (D12).

After delivery, a system admin can refund (D13):

```
POST /v1/orders/{id}/refund ──► start OrderRefundWorkflow refund-{order_id}
  1. payment.refund(order_id, amount)          RefundProcessed (outbox)
  2. order.record_refund(order_id, amount)     refunded_amount, history row
```

---

## 3. Build order

Each step is mergeable on its own and ends green (`scripts/check.sh` + smoke test).
Size: S ≈ ½ day, M ≈ 1 day, L ≈ 1½–2 days.

| Step | What | Size | Depends on |
|---|---|---|---|
| 0 | Temporal foundations | S | — |
| 1 | Order state machine + restaurant-admin views | M | 0 |
| 2 | restaurant-service activities (reserve/release/complete, promotions) | M | 0 |
| 3 | payment-service activities | S | 0 |
| 4 | Saga part 1: order → payment → restaurant decision, cancel, compensation | L | 1, 2, 3 |
| 5 | delivery-service: Redis GEO, offers, rider endpoints, ETA | L | 0 |
| 6 | Saga part 2: assignment loop, rider drop-out, pickup, delivery, settlement, late cancel, post-delivery refund | L | 4, 5 |
| 7 | Live feeds: gateway streaming proxy + tracking-service (customer order tracking, restaurant new-order feed) | M | 5 |
| 8 | End-to-end lifecycle scenarios, seed data, docs and diagrams | M | all |

If time runs short, cut in this order: distance-based delivery fee (use a flat fee),
promotions in the saga (step 2b), stale-location detection (keep the explicit rider
drop-out), tracking-service as a separate service (fall back to SSE endpoints in
delivery-service and order-service).

---

### Step 0 — Temporal foundations

**Build**
- Add `temporalio` to order/restaurant/payment/delivery services.
- `libs/contracts` (`sfo_contracts`): task-queue constants, activity and signal names,
  input/output dataclasses for every activity in this plan.
- `sfo_common.temporal`: `connect_client()` (retry until Temporal is reachable, honours
  `TEMPORAL_ADDRESS`), `run_worker(task_queue, workflows, activities)` with graceful shutdown.
- Each of order/restaurant/payment/delivery gets `app/worker.py`: registers its workflows
  and activities on its task queue. `TEMPORAL_WORKER_MODE=embedded` (development default)
  starts it from the app's lifespan; `standalone` is for running `python -m app.worker`
  as its own process in production. No new containers; the four services gain
  `depends_on: temporal`.
- Activity errors: business failures raise `ApplicationError(non_retryable=True)` with a
  `type` (`OutOfStock`, `CapacityFull`, `PaymentDeclined`, …); infrastructure errors are
  retried (exponential backoff, max 5 attempts per activity).

**Test**
- Unit: a trivial workflow + activity run in `WorkflowEnvironment.start_time_skipping()`.
- Compose: all four services' workers polling their task queues (visible in the Temporal
  UI, `:8088`); `standalone` mode starts the same worker from the command line.

### Step 1 — Order state machine + restaurant-admin views (order-service)

**Build**
- `app/state_machine.py`: allowed transitions as data
  (`created → awaiting_restaurant_acceptance → confirmed → assigned → picked_up → delivered`,
  `→ rejected` from `created`/`awaiting_…`, `→ cancelled` from any state before `picked_up`).
- Activity `transition_order(order_id, to_status, reason, actor)`: checks the transition,
  sets timestamps (`confirmed_at`, …), appends `order_status_history`, writes the matching
  outbox event (`OrderConfirmed`, `OrderRejected`, `OrderCancelled`, `RiderAssigned`,
  `OrderPickedUp`, `OrderDelivered`) — all in one transaction, guarded by the `version`
  lock. Every transition also publishes a status message to `tracking:order:{id}`, and
  reaching `awaiting_restaurant_acceptance` (or a cancellation) publishes to
  `tracking:restaurant:{restaurant_id}` (D11).
  **Idempotent:** already in `to_status` → no-op success.
- Activity `apply_pricing(order_id, discount, tax, delivery_fee)` → writes the pricing
  columns and `total_amount`.
- Activity `record_refund(order_id, amount, reason, actor)` → adds to
  `orders.refunded_amount`, sets `refunded_at`, appends a history row (status stays
  `delivered`; the refund is a fact about the order, not a new lifecycle state).
- Quote response gains `admin_user_id`, `lat`, `lng` → stored on the order
  (`restaurant_admin_id`, used for D10; coordinates passed to the saga for delivery).

**Endpoints**
| Method & path | Who | Purpose |
|---|---|---|
| `GET /v1/orders/restaurant/{restaurant_id}?status=` | owning `restaurant_admin`, `system_admin` | Restaurant's order queue (paginated) |
| `POST /v1/orders/{id}/restaurant-response` `{decision: accept\|reject, reason?}` | owning `restaurant_admin` | Signals the workflow; 409 unless `awaiting_restaurant_acceptance` |
| `POST /v1/orders/{id}/cancel` `{reason?}` | owning customer, `system_admin` | Signals the workflow; 409 once `picked_up` or terminal |
| `POST /v1/orders/{id}/refund` `{amount?, reason}` | `system_admin` | Starts `OrderRefundWorkflow` (step 6); 409 unless `delivered`; `amount` ≤ captured − already refunded |

**Data model** — migration `0004`:
- `orders.restaurant_admin_id` (nullable for pre-existing rows), `orders.payment_method`,
  `orders.refunded_amount NUMERIC(10,2) DEFAULT 0`, `orders.refunded_at`.
- Index `(restaurant_id, status, created_at, id)` for the restaurant queue.

**Test**
- Unit: every allowed transition passes, every other one raises; table-driven.
- Activity: idempotent re-run; stale `version` → conflict; history + outbox row written
  in the same transaction (real-DB test, see §6).
- API: ownership (another restaurant's admin → 403), wrong state → 409.

### Step 2 — restaurant-service activities

**Build** (worker on `restaurant-activities`)
- `reserve_order(order_id, restaurant_id, items)`: one transaction —
  1. `UPDATE restaurants SET active_order_count = active_order_count + 1 WHERE id = :id AND status = 'active' AND is_accepting_orders AND active_order_count < max_concurrent_orders` → 0 rows ⇒ `CapacityFull` / `RestaurantUnavailable`;
  2. per item `UPDATE menu_items SET stock_count = stock_count - :qty, version = version + 1 WHERE id = :item AND restaurant_id = :rid AND is_available AND (stock_count IS NULL OR stock_count >= :qty)` (NULL = unlimited, left NULL) → 0 rows ⇒ `OutOfStock`;
  3. insert `stock_reservations(order_id, …, status='reserved')`.
  Idempotent: an existing reservation for `order_id` is returned as-is.
- `release_reservation(order_id)`: if `reserved` → restore stock, decrement capacity,
  `status='released'`; otherwise no-op.
- `complete_reservation(order_id)`: on pickup → decrement capacity, `status='consumed'`
  (stock stays consumed).
- `redeem_promotion(order_id, restaurant_id, code)`: atomic `UPDATE promotions SET times_used = times_used + 1 WHERE … is_active AND now() BETWEEN valid_from AND valid_until AND (max_uses IS NULL OR times_used < max_uses) RETURNING discount_type, discount_value` + `promotion_redemptions` row; `release_promotion(order_id)` reverses it.
- All of these invalidate the public menu cache for the restaurant (stock changed).

**Data model** — migration `0004`:
- `restaurants.active_order_count INT NOT NULL DEFAULT 0`.
- `stock_reservations` (`order_id` PK, `restaurant_id`, `items` JSON, `status`, timestamps).
- `promotion_redemptions` (`order_id` PK, `promotion_id`, `status`, timestamps).

**Test**
- **Concurrency (real DB):** 20 concurrent `reserve_order` calls for an item with stock 5
  → exactly 5 succeed, stock ends at 0, never negative. Same for capacity.
- Idempotency: reserve twice → one reservation, stock decremented once; release twice →
  restored once.
- Promotion: expired / exhausted / inactive → non-retryable error; `max_uses=1` under two
  concurrent redemptions → exactly one succeeds.

### Step 3 — payment-service activities

**Build** (worker on `payment-activities`)
- State machine: `authorized → captured → refunded`, `authorized → voided`, `declined`.
- `authorize(order_id, customer_id, amount, method)`: creates the payment row
  (idempotency key `{order_id}`); simulation: `card_simulated` approves,
  `card_simulated_decline` declines (non-retryable `PaymentDeclined`) — deterministic so
  tests can exercise the failure path. Outbox `PaymentSuccessful` on approval.
- `capture(order_id)`, `void(order_id)`, `refund(order_id, amount?)` (`RefundProcessed`
  outbox; partial refunds allowed, total ≤ captured). Each is a no-op if the payment is
  already in the target state; `refund` is idempotent per refund workflow ID.

**Data model** — migration `0002`: `payments.captured_at`, `payments.voided_at`.
`OrderCreate` gains optional `payment_method: Literal["card_simulated", "card_simulated_decline"]`.

**Test**
- Unit: transition table; amounts as `Decimal`.
- Activity: authorize twice → one row; capture after void → non-retryable error;
  refund amount ≤ captured amount.

### Step 4 — Saga part 1 (order-service)

**Build**
- `OrderSagaWorkflow` steps 1–7 of §2, with compensation on every failure path
  (compensations run in reverse, are themselves idempotent activities, and are retried
  until they succeed).
- Pricing (pure functions, deterministic, configurable): tax = `TAX_RATE` × (subtotal −
  discount); delivery fee = `BASE_DELIVERY_FEE` + `PER_KM_FEE` × haversine km
  (placeholders: 5%, 100 PKR, 30 PKR/km — **values to confirm with the mentor**).
- `POST /v1/orders`: after commit, `start_workflow(id="order-{id}")`; an
  "already started" error is success.
- Reconciler (in order-service's worker, every 30s): `created` orders older than 60s → start
  workflow (idempotent).
- Signals: `restaurant_response`, `cancel`. Restaurant decision timeout: 5 min
  (`RESTAURANT_ACCEPT_TIMEOUT`).

**Test** (time-skipping test environment, activities mocked)
- Happy path to `confirmed`; activities called in order with the right inputs.
- `OutOfStock` → `rejected`, nothing to compensate; `PaymentDeclined` → reservation and
  promotion released, order `rejected` with `status_reason`.
- Restaurant rejects → payment voided, reservation released, `rejected`.
- No response in 5 min (time skipped) → same compensation, `status_reason=restaurant_timeout`.
- Customer cancels while awaiting → `cancelled`, payment voided, stock restored.
- A compensation activity failing twice then succeeding → workflow still ends correctly.
- Reconciler: an order committed without a workflow gets one within 60s.

### Step 5 — delivery-service: GEO, offers, rider endpoints, ETA

**Build**
- Location ingestion (D6): `PATCH /v1/riders/me/location` → `GEOADD riders:available` when
  the rider is `available`; Postgres `last_lat/lng/last_location_at` only if the last write
  is > 30s old; Mongo ping history unchanged. Going `busy`/`offline` → `ZREM`.
  If the rider has an active delivery, publish the position to `tracking:order:{id}`.
- Activities (worker on `delivery-activities`):
  - `create_delivery(order_id, pickup, dropoff)` — idempotent on `deliveries.order_id`.
  - `offer_to_nearest_rider(delivery_id, exclude_rider_ids)` — `GEOSEARCH` within 5 km,
    nearest first; claim with `UPDATE riders SET status='busy' WHERE id=:id AND status='available' AND verification_status='approved' RETURNING id`; on success `ZREM`, delivery → `offered`, `delivery_offers` row, `offer_expires_at`. Returns `None` if nobody is free.
  - `release_rider(delivery_id, outcome)` — rider back to `available` (+ `GEOADD`),
    offer marked `declined`/`expired`, `reassignment_count += 1`.
  - `compute_eta(delivery_id)` — pickup ETA = max(rider → restaurant travel time, remaining
    prep time from `avg_prep_time_minutes`); dropoff ETA = pickup ETA + restaurant →
    dropoff travel time; speed per `vehicle_type`.
- Every delivery status change appends `delivery_status_history`, writes a
  `DeliveryStatusUpdated` outbox row, and publishes to `tracking:order:{id}`.
- Stale-rider detector (in delivery-service's worker, every 30s): deliveries in `assigned` whose
  rider's last ping is older than 3 min → `rider_dropped` signal (D12). Riders send pings
  every 5–15s, so 3 min is ~12+ missed pings.

**Endpoints** (rider role, ownership-checked)
| Method & path | Purpose |
|---|---|
| `GET /v1/riders/me/offer` | The current offer, if any (pickup/dropoff, distance, expiry) |
| `POST /v1/riders/me/deliveries/{id}/accept` \| `/decline` | Signals `rider_response` |
| `POST /v1/riders/me/deliveries/{id}/drop-out` `{reason}` | Rider can't complete (before pickup only) → signals `rider_dropped` (D12) |
| `POST /v1/riders/me/deliveries/{id}/picked-up` | Delivery → `picked_up`, signals the workflow |
| `POST /v1/riders/me/deliveries/{id}/delivered` | Delivery → `delivered`, signals the workflow |
| `GET /v1/deliveries/by-order/{order_id}` | Customer (own order) / admin: status, rider, ETAs |

**Data model** — migration `0004`:
- `riders.last_location_at`.
- `deliveries`: `offer_expires_at`, `dropoff_eta`, `cancelled_at`.
- `delivery_offers` (`id`, `delivery_id`, `rider_id`, `status`, `offered_at`,
  `responded_at`) — audit trail + rider-utilization/reassignment analytics.
- `delivery_status_history` (same shape as the other audit tables).

**Test**
- Unit: ETA and haversine with known distances; per-vehicle speeds.
- Redis (real Redis in compose): nearest of three riders is chosen; riders > 5 km excluded.
- **Concurrency:** two simultaneous offers can't claim the same rider (one gets `None`
  or the next rider).
- Endpoints: a rider can't accept another rider's offer (403); accepting an expired offer
  → 409; drop-out after pickup → 409.
- Detector: an assigned delivery whose rider stopped pinging 3+ min ago is signalled once;
  a rider pinging normally is never signalled.

### Step 6 — Saga part 2

**Build**
- Steps 8–14 of §2: offer loop (max 5 offers or 10 minutes), `rider_response` /
  `picked_up` / `delivered` signals, payment capture on delivery, capacity freed at pickup.
- Cancel after confirmation (allowed until pickup), compensating by stage:

| Cancelled at | Payment | Stock / capacity | Rider | Promotion |
|---|---|---|---|---|
| `confirmed` (no rider yet) | void | release | — | release |
| `assigned` | void | release | release | release |
| `picked_up` onwards | not customer-cancellable (support refund) | — | — | — |

- No rider found → same as cancel at `confirmed`, `status_reason=no_rider_available`.
- `rider_dropped` while `assigned` → rider released (offer outcome `dropped`), order stays
  `assigned` and the offer loop runs again (the self-loop in the state machine); a new
  `RiderAssigned` is emitted when someone accepts. Counts towards the 5-offer limit.
- Post-delivery refund: `OrderRefundWorkflow` (D13) — `payment.refund` then
  `order.record_refund`, both idempotent; `RefundProcessed` from payment-service.

**Test** (time-skipping env)
- Rider declines → next rider offered, `reassignment_count == 1`.
- Offer expires (time skipped 60s) → next rider; all 5 fail → `cancelled`, full compensation.
- Cancel at `confirmed` and at `assigned` → exactly the compensations in the table above.
- `delivered` → capture called once; rider released; order `delivered`.
- Cancel signal after `picked_up` → ignored (API already returns 409).
- `rider_dropped` after assignment → rider released, next rider offered, order reaches
  `delivered`; `rider_dropped` after `picked_up` → ignored.
- Refund workflow: full and partial refund; running it twice refunds once; refund larger
  than captured → rejected before the workflow starts.

### Step 7 — Live feeds (SSE): customer tracking + restaurant new orders

**Build**
- **Gateway streaming proxy** for `/v1/tracking/*`: forwards with `httpx` streaming, no read
  timeout, connection pool of its own (bulkhead), pure-ASGI so nothing buffers.
- **`tracking-service`** (new, stateless, no database, talks only to Redis — D8):
  `GET /v1/tracking/orders/{id}` (SSE). On connect: verify the JWT, read
  `tracking:order:{id}:state` — the caller must be its `customer_id` (or an admin) — send it
  as the snapshot event, then relay messages from Redis channel `tracking:order:{id}`;
  heartbeat comment every 15s; closes on `delivered`/`cancelled`/`rejected`.
- `GET /v1/tracking/restaurants/{restaurant_id}` (SSE, D11): owning `restaurant_admin` only,
  checked against `restaurant:{id}:owner` (written by restaurant-service when the
  restaurant is created). On connect, a snapshot of orders awaiting a decision from the set
  `tracking:restaurant:{id}:pending`; then every new order and cancellation from
  `tracking:restaurant:{id}`. The restaurant dashboard accepts/rejects through the
  existing `restaurant-response` endpoint.
- Publishers keep the Redis state current alongside every `PUBLISH`:
  - order-service `transition_order`: status into `tracking:order:{id}:state`;
    `SADD`/`SREM` on `tracking:restaurant:{id}:pending` when an order enters/leaves
    `awaiting_restaurant_acceptance`;
  - delivery-service: rider position and delivery status into the same state key.
  State keys expire 1 hour after the order reaches a terminal status. Messages are small
  JSON: `{type: "status"|"location", …}`.
- Fallback if a state key is missing (e.g. Redis restarted mid-order): one call to
  order-service `GET /internal/orders/{id}` / restaurant-service
  `GET /internal/restaurants/{id}/owner`, which also re-populates the key.

**Test**
- Unit: SSE event formatting; ownership checks (another customer → 403, another
  restaurant's admin → 403, both before streaming).
- A new order appears on the restaurant feed within a second of reaching
  `awaiting_restaurant_acceptance`.
- Integration: publish to the channel → the connected client receives it; disconnect
  unsubscribes (no leaked subscriptions).
- Gateway: a streamed response arrives incrementally (first event before the stream ends).

### Step 8 — End-to-end scenarios, seed data, docs

**Build**
- `seed.py`: a second approved rider (needed for reassignment), stock on one menu item.
- `scripts/smoke_test.py`: lifecycle scenarios (below), plus a `--lifecycle` flag since
  they take longer than the quick checks.
- Docs: `technical-documentation.md` (saga, compensation, delivery, refunds, tracking as
  built; assumptions in §9), `architecture.md` (new endpoints, workers, tracking-service),
  `PROGRESS.md`, README.
- Diagrams (Guidelines ask for them explicitly): README architecture diagram
  (order + delivery flow, workers, tracking-service); `diagrams.md` component diagram
  (replace the old "Kafka → gateway" tracking arrow with Redis Pub/Sub → tracking-service)
  and new sequence diagrams for the order lifecycle, delivery assignment with
  reassignment, cancellation, and refund; ER diagrams for the new tables.

**Test — lifecycle scenarios through the gateway**
1. Happy path: order → restaurant accepts → rider accepts → picked up → delivered;
   payment `captured`, stock decremented once, capacity back to 0, SSE client saw every
   status in order.
2. Restaurant rejects → `rejected`, payment `voided`, stock restored.
3. Payment declined (`card_simulated_decline`) → `rejected`, stock restored.
4. Cancel after confirmation (rider assigned) → `cancelled`, payment voided, stock
   restored, rider `available` again.
5. Rider 1 declines → rider 2 offered and accepts → delivered (`reassignment_count = 1`).
6. Out of stock: two customers race for the last unit → one order proceeds, one is `rejected`.
7. Rider drops out after accepting → another rider is assigned → delivered.
8. Post-delivery refund (partial) by an admin → payment `refunded_amount` updated,
   `RefundProcessed` outbox row, order `refunded_amount` matches.
9. Restaurant feed: a restaurant admin connected to the feed sees the new order arrive.

---

## 4. New and changed endpoints (summary)

| Service | Endpoint | New/changed |
|---|---|---|
| order | `POST /v1/orders` | Changed: starts the workflow, returns at `created`; accepts `payment_method` |
| order | `GET /v1/orders/restaurant/{restaurant_id}` | New |
| order | `POST /v1/orders/{id}/restaurant-response` | New |
| order | `POST /v1/orders/{id}/cancel` | New |
| order | `POST /v1/orders/{id}/refund` | New (system admin, post-delivery) |
| order | `GET /internal/orders/{id}` | New (tracking fallback when the Redis state key is missing) |
| restaurant | `POST /internal/restaurants/{id}/quote` | Changed: also returns `admin_user_id`, `lat`, `lng` |
| restaurant | `GET /internal/restaurants/{id}/owner` | New (restaurant-feed fallback when `restaurant:{id}:owner` is missing) |
| delivery | `GET /v1/riders/me/offer` | New |
| delivery | `POST /v1/riders/me/deliveries/{id}/accept`, `/decline`, `/drop-out`, `/picked-up`, `/delivered` | New |
| delivery | `GET /v1/deliveries/by-order/{order_id}` | New (gateway registry gains `deliveries`) |
| tracking | `GET /v1/tracking/orders/{id}` (SSE) | New service (gateway registry gains `tracking`) |
| tracking | `GET /v1/tracking/restaurants/{restaurant_id}` (SSE) | New |

## 5. Temporal activities and signals (summary)

| Task queue | Activity | Compensates with |
|---|---|---|
| `restaurant-activities` | `reserve_order` | `release_reservation` |
| | `redeem_promotion` | `release_promotion` |
| | `complete_reservation` | — |
| `payment-activities` | `authorize` | `void` (before capture) / `refund` (after) |
| | `capture` | `refund` |
| `order-activities` | `transition_order`, `apply_pricing`, `record_refund` | — (idempotent state writes) |
| `delivery-activities` | `create_delivery` | `cancel_delivery` |
| | `offer_to_nearest_rider` | `release_rider` |
| | `compute_eta` | — |

Signals on `OrderSagaWorkflow`: `restaurant_response(decision, reason)`,
`rider_response(delivery_id, accepted)`, `rider_dropped(delivery_id, reason)`,
`picked_up()`, `delivered()`, `cancel(reason, actor)`. Query: `status()` (current step,
for debugging). Second workflow: `OrderRefundWorkflow(order_id, amount, reason)` on the
`order-saga` queue.

## 6. Testing approach

| Level | Tooling | Runs in |
|---|---|---|
| Pure logic (state machines, pricing, ETA) | pytest | `check.sh`, every commit |
| Workflow logic incl. timeouts | `temporalio.testing.WorkflowEnvironment.start_time_skipping()`, mocked activities | `check.sh` |
| Activities & repositories against real Postgres/Redis (atomic SQL, concurrency, idempotency) | pytest marker `db`, separate `*_test` databases on the compose Postgres/Redis | `scripts/check.sh --with-db` (needs `docker compose up`) |
| Whole system | `scripts/smoke_test.py --deep --lifecycle` through the gateway | after `docker compose up` |

The concurrency tests (stock, capacity, promotions, rider claims) are the most important
new tests: they prove the atomic SQL actually prevents double-booking, which unit tests
with mocks can't.

## 7. Settings and decisions

Defaults below are what gets built; all numeric ones are config values, easy to change.

| Item | Decision |
|---|---|
| Tax | 5% of (subtotal − discount) — `TAX_RATE` |
| Delivery fee | 100 PKR + 30 PKR per km — `BASE_DELIVERY_FEE`, `PER_KM_FEE` |
| Restaurant decision timeout | 5 min — `RESTAURANT_ACCEPT_TIMEOUT` |
| Rider offer | 60 s per offer, at most 5 offers or 10 min — `RIDER_OFFER_TIMEOUT`, `MAX_RIDER_OFFERS` |
| Stale-rider threshold | 3 min without a location ping — `RIDER_STALE_AFTER` |
| Tracking | Separate `tracking-service`, Redis-only (D8) |
| Temporal workers | In-process in development, standalone in production (D2) |
| Local stack | 19 → **20 containers** (only `tracking-service` is new); Postgres stays one container per service |

## 8. Requirements traceability (Week 2)

**Guidelines — Week 2 scope and deliverables**

| Requirement | Where |
|---|---|
| End-to-end order lifecycle | Steps 4, 6, 8 (scenarios 1–9) |
| Payment authorization simulation | Step 3 |
| Restaurant order acceptance/rejection flow | Steps 1, 4, 7 (feed) |
| Delivery assignment logic (rider allocation) | Steps 5, 6 |
| Workflow orchestration (Temporal) | Steps 0, 4, 6 |
| Order state transitions (created → confirmed → assigned → delivered) | Step 1 (plus `awaiting_restaurant_acceptance`, `picked_up`, `rejected`, `cancelled`) |
| Basic failure handling (retry + rollback) | Activity retry policies (step 0) + compensations (steps 4, 6) |

**Part A — ordering and delivery requirements**

| Requirement | Where |
|---|---|
| Menu availability validated · restaurant capacity checked | Step 2 (atomic reserve) |
| Pricing and discounts applied | Steps 2 (promotions), 4 (tax, delivery fee) |
| Payment authorized · confirmed only after successful validation | Steps 3, 4 |
| Restaurant receives order notification | Step 7 restaurant feed (D11); email/SMS in Week 3 |
| Delivery assignment triggered on confirmation | Step 6 |
| Rider assigned by availability and proximity | Step 5 (Redis GEO, D6) |
| Pickup ETA calculated | Step 5 (`compute_eta`) |
| Pickup tracked · status updates recorded in real time | Steps 5 (history + outbox), 7 (SSE) |
| Delivery completion triggers final settlement | Step 6 (payment capture; see §9) |
| High concurrency · duplicate prevention | Atomic SQL (D5, D6), idempotent activities, workflow ID = order (D4); concurrency tests §6 |
| Idempotent order state updates | `transition_order` (step 1) |
| Failure recovery and reassignment | Steps 6 (decline/expiry/drop-out loop, D12) |
| Real-time tracking updates | Step 7 |
| Cancellation → refund, delivery reassignment, inventory rollback | Step 6 (compensation table, refund workflow D13) |
| Customer notifications · analytics updates | Outbox events written here; consumed in Week 3 |
| Partial failures must not corrupt order state | Compensations + idempotent activities; tests in steps 4, 6 |

## 9. Assumptions (documented, not built)

- **Final settlement = payment capture** on delivery. Splitting the captured amount into
  restaurant payouts, rider earnings and platform commission is a separate finance
  system, out of scope for Part A.
- **Rider routes**: no routing engine — straight-line (haversine) distance and an average
  speed per vehicle type, as already stated in `technical-documentation.md`. Riders don't
  "manage routes" in-app beyond seeing pickup and dropoff coordinates.
- **One refund workflow per order** (`refund-{order_id}`), which may be partial; a
  second refund for the same order would need a refund-request ID in the workflow ID.
- **Drop-out after pickup** is a support case (manual), not automatic reassignment.
