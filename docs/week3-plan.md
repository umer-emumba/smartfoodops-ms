# Week 3 Plan — Event-Driven System + Observability

## Context

Week 2 is done (`week2/order-delivery-lifecycle`, 82/82 lifecycle checks). Every domain event
is **already written to an outbox table** in the same transaction as its state change, but
nothing publishes it yet. There are no consumers, the notification-service is a read-only
skeleton, there's no analytics, and observability is only JSON logs with an `X-Request-ID`.

Week 3 (Guidelines §5) needs: Kafka event streaming, event-driven lifecycle updates, Celery
workers, notifications, an analytics pipeline, and Prometheus, OpenTelemetry/Jaeger and logging.
Deliverables: a fully event-driven system, a working async pipeline, an analytics flow,
dashboards, and end-to-end visibility.

Decided with the user: a Celery worker+beat per producing service · JSON Schema (from Pydantic) in
Schema Registry · Loki + Grafana for logs · in-app + simulated email/SMS notifications.

Working rule (memory): build step by step, verify each step in Docker
(`smoke_test.py --deep`), and ask before committing. All steps go on a new branch `week3/event-driven-observability`
cut from the Week 2 branch. The first step copies this plan to `docs/week3-plan.md`, the same way Week 2 had its plan doc.

### What exists today (reuse, don't rebuild)

| Piece | Where |
|---|---|
| Outbox mixin + `new_outbox_row` | `libs/events/src/sfo_events/outbox.py` |
| Event envelope, `EventType`, `Topic` (one per aggregate) | `libs/events/src/sfo_events/schemas.py` |
| Outbox writers | order: `app/repository.py` (every status transition), `app/api/routes.py` (`OrderPlaced`); payment: `app/activities.py` (`PaymentSuccessful`, `RefundProcessed`); delivery: `app/activities.py::_set_status` (`DeliveryStatusUpdated`) |
| Undispatched-outbox partial index | order-service `alembic/versions/0003_…` (payment and delivery still need one) |
| Notification table + list-mine API | `services/notification-service/app/models.py`, `api/routes.py` |
| Embedded/standalone background pattern | `sfo_common.temporal` (`embedded_workers`, `run_standalone`), order-service `app/reconciler.py` |
| JSON logging + redaction + correlation ID | `libs/observability/.../logging.py`, `sfo_common/middleware.py` |
| Kafka 8.3.2, Schema Registry, RabbitMQ 4.3, Mongo 7 | already in `docker-compose.yml`, idle |

Measured memory (`docker stats`, today): about **3.0 GB** used of 7.75 GB. Week 3 adds about 10
containers, estimated at 1–1.3 GB. Re-measure in step 6.

---

## 1. Design decisions

| # | Decision | Why |
|---|---|---|
| W3-D1 | **Relay = Celery beat + worker per producing service** (`order-worker`, `payment-worker`, `delivery-worker`; same image, `celery -A app.celery_app worker --beat --pool=solo`). Beat triggers `relay_outbox` every 1s. Each worker reads only its own DB | Keeps database-per-service. One beat per service means one relay per outbox, which keeps per-aggregate ordering simple |
| W3-D2 | **Relay loop**: take `pg_try_advisory_lock(<service relay key>)` (skip this tick if it's held) → `SELECT … WHERE dispatched_at IS NULL ORDER BY created_at, id LIMIT 200` → produce all → `flush()` → set `dispatched_at` → commit → unlock. Producer: `enable.idempotence=true`, `acks=all`, key = `aggregate_id`. The task has `expires` = interval, so ticks never pile up in RabbitMQ | At-least-once delivery with no lost events. **Not `SKIP LOCKED`**: with two relays it would split one order's events across them and break per-order ordering. The advisory lock means only one relay per service at a time, even with more worker replicas. A crash after produce but before commit causes a re-publish, which consumers dedupe (W3-D4) |
| W3-D2a | **Celery tasks are sync, but our DB layer is async-only (`asyncpg`; no psycopg anywhere)**. Each worker process keeps one event loop + one async engine, created in `worker_process_init`. Tasks run `loop.run_until_complete(relay())`. Beat's schedule file goes in `/tmp` (the container runs as non-root) | Reuses the existing models and sessions with no second driver. A fresh `asyncio.run` per tick would rebuild the engine/pool every second |
| W3-D3 | **Typed payload model per event type** (Pydantic, in `libs/events`). JSON Schema serializer, `TopicRecordNameStrategy` (several event types per topic), `BACKWARD` compatibility. Schemas are registered at relay start-up | Keeps the Week 2 one-topic-per-aggregate rule. Typed contracts catch a bad payload at the producer, and the registry blocks breaking changes |
| W3-D4 | **Idempotent consumers**: each consumer service's DB stores its business effect plus a `processed_events(event_id PK, consumer)` row **in one transaction**, then commits the Kafka offset (manual commit, `enable.auto.commit=false`) | Exactly-once *effect* on top of at-least-once delivery. Replaying offsets causes no duplicates |
| W3-D5 | **Consumer failures**: retry in-process 3× with backoff. After that, publish to `<topic>.dlq` with error headers, then commit and move on | One poison message can't block a partition. The DLQ count feeds the "Failed Events" metric |
| W3-D6 | **Kafka consumers run embedded in the service process in dev** (`KAFKA_CONSUMER_MODE=embedded\|standalone`, blocking `confluent_kafka.Consumer` in a thread started from lifespan) | Same reasoning as Week 2's D2 for Temporal workers: no extra containers in dev, and they can run separately in production |
| W3-D7 | **Notifications**: the consumer writes `notifications` rows (`pending`) and then enqueues a Celery task `send_notification(id)` on RabbitMQ. A separate `notification-worker` sends them: in-app is marked `sent`; email/SMS go through a fake sender with a configurable failure rate, `autoretry_for`, exponential backoff, max 5 tries, then `failed`. A beat sweep re-enqueues `pending` rows older than 60s | The sweep closes the commit-then-enqueue gap. The fake failures actually exercise Celery retries |
| W3-D8 | **Enrich events at the source**: order transition events add `restaurant_admin_id` and `total_amount` (both already on `orders`). Delivery events add `rider_user_id` (from `riders.user_id`) and `customer_id`. **No `occurred_at` in payloads**: the relay builds the envelope's `occurred_at` from the outbox row's `created_at` | Consumers never call other services to find a recipient or dimension. Kafka stays the only coupling |
| W3-D8a | **New event `OrderAwaitingRestaurant`**: add it to `_TRANSITION_EVENTS` (`order-service/app/repository.py`). Today `awaiting_restaurant_acceptance` is an "internal step, no event". The restaurant's "new order" notification comes from this event, **not** from `OrderPlaced` | `OrderPlaced` fires before stock and payment are checked, so the restaurant would be notified about orders that then fail on a declined card or no stock |
| W3-D8b | **Rider notifications come only from delivery events**: `DeliveryStatusUpdated` with `offered`, `assigned` or `cancelled`. order-service never stores the rider (`orders` has no rider column) | Only delivery-service knows who the rider is |
| W3-D8c | **New event `RiderShiftChanged`**: written in `PATCH /riders/me/status` (`delivery-service/app/api/routes.py:111`) in the same transaction, **only when the rider goes offline ↔ on shift**. The saga's `available ↔ busy` claims don't emit it | Utilization = time on deliveries ÷ time on shift. Busy/available flips would flood the topic for no gain |
| W3-D9 | **analytics-service (new)** with a MongoDB `analytics` DB. It keeps one `order_facts` document per order, upserted with `$set`/`$max` on timestamps (idempotent by construction — Mongo here is a single node, so there are no multi-doc transactions), plus `rider_shift_facts`. Metrics are computed with aggregation pipelines at read time | Every required metric can be derived from per-order facts. Upserts are naturally replay-safe. Mongo is non-authoritative, as the architecture already says |
| W3-D10 | **The analytics consumer also updates Prometheus business metrics** (`orders_total{restaurant}`, `delivery_duration_seconds` histogram, cancellations, acceptances) | Grafana gets live business panels without a Mongo datasource plugin. The Mongo API stays the source for exact historical numbers |
| W3-D11 | **Tracing: OpenTelemetry SDK → OTLP directly to Jaeger v2** (`jaegertracing/jaeger:2.x`, in-memory store). No separate OTel Collector | Jaeger v2 *is* an OTel Collector distribution, so one container fewer. A collector can be inserted later without code changes |
| W3-D12 | **Trace propagation over every hop**: HTTP (FastAPI + httpx instrumentation); Temporal (`temporalio.contrib.opentelemetry.TracingInterceptor` on client and workers); outbox (new `trace_context` column stores the W3C `traceparent` at write time); Kafka (relay puts it in message headers, consumer continues the trace); Celery (instrumentor) | Gives one trace for a whole order: gateway → order → saga → restaurant/payment/delivery activities → outbox → Kafka → notification/analytics → Celery send |
| W3-D13 | **Logs carry `trace_id`/`span_id` from OTel** (the existing `X-Request-ID` stays as `request_id`). Grafana Alloy tails Docker logs into Loki. Grafana links Loki → Jaeger by `trace_id` and Jaeger → Loki back | "Logging" becomes searchable across services in one place, and you can jump from a log line to its trace |
| W3-D14 | **Metrics**: `prometheus-client` `/metrics` on each service's internal port (the gateway does **not** route it). Includes HTTP RED middleware (route template, method, status) plus domain metrics. Celery workers expose a metrics HTTP port (solo/threads pool, so no multiprocess mode is needed). Temporal dev server `--metrics-port`, Temporal SDK metrics, and RabbitMQ's built-in Prometheus plugin are also scraped | Every flow the PDF names (ordering, payment, assignment, tracking, notifications, background jobs) has metrics that show where it fails |
| W3-D15 | **Topics are created by a one-shot `kafka-init` container**: 3 partitions each for `order-events`, `delivery-events`, `payment-events`, plus `.dlq` versions, 7-day retention | Explicit partitions instead of auto-create. Partitioning by aggregate ID lets consumers scale out |

### Event → consumer map

| Event | notification-service (recipients) | analytics-service |
|---|---|---|
| `OrderPlaced` | customer ("order received") | placed_at, restaurant, amount, hour bucket |
| `OrderAwaitingRestaurant` (new) | restaurant admin ("new order — accept within 5 min") | sent_to_restaurant_at |
| `OrderConfirmed` / `OrderRejected` | customer (rejected: wording varies by `reason`) | accepted / rejected + `reason` |
| `RiderAssigned`, `OrderPickedUp` | customer | assigned_at, picked_up_at |
| `OrderDelivered` | customer, restaurant admin | delivered_at → delivery time, success |
| `OrderCancelled` | customer, restaurant admin | cancellation rate |
| `PaymentSuccessful`, `RefundProcessed` | customer (refund only; authorize isn't worth a message) | refunds |
| `DeliveryStatusUpdated` | rider: `offered` ("new delivery offer"), `assigned`, `cancelled` | reassignment count, deliveries started/completed |
| `RiderShiftChanged` (new) | — | on-shift time → rider utilization |
| `*.dlq` topics | — | failed-events count |

### Metric definitions (analytics API + dashboards)

Total orders · orders per restaurant · peak-hour load (orders per hour of day) · average delivery
time (placed → delivered) · rider utilization (time on deliveries ÷ time on shift) · cancellation
rate · restaurant acceptance rate (confirmed ÷ (confirmed + rejected with reason `restaurant_rejected`
or `restaurant_timeout`; `out_of_stock`/`payment_declined` never reach the restaurant, so they're excluded) · delivery
success rate (delivered ÷ deliveries started) · failed events (DLQ + sagas ending `system_error`
+ Celery tasks that ended `failed`).

---

## 2. Build order

### Step 0 — Event contracts, Kafka plumbing, branch + plan doc
- Branch `week3/event-driven-observability`. Copy this plan to `docs/week3-plan.md`
- `libs/events`: payload models per `EventType`. Add `RiderShiftChanged`. Make `new_outbox_row` validate the payload against its model. Add `kafka.py` (producer factory, JSON Schema serializer/deserializer, header helpers) and `consumer.py` (a generic idempotent consumer loop: poll → deserialize → handler(session, event) → `processed_events` → commit → offset commit; retries + DLQ per W3-D5)
- Dependencies (`uv add`): `confluent-kafka[schemaregistry,json]`, `celery`
- `kafka-init` one-shot container (W3-D15)
- Enrich the payloads (W3-D8) in the existing writers listed above. Add `OrderAwaitingRestaurant` (W3-D8a) and `RiderShiftChanged` (W3-D8c)
- Tests: payload model round-trip, schema-compatibility check, and unit tests for the consumer loop with a fake consumer (duplicate → no-op, poison → DLQ)

### Step 1 — Outbox relay (order, payment, delivery)
- Per service: `app/celery_app.py` (broker = RabbitMQ, beat schedule), `app/relay.py` (W3-D2), and a purge task for rows dispatched more than 7 days ago
- Alembic: add `trace_context` (nullable), `publish_attempts` and `last_error` to `outbox_events`; add the partial index on `dispatched_at IS NULL` for payment and delivery
- Compose: `order-worker`, `payment-worker`, `delivery-worker` (same image as the service, different command), `depends_on` kafka-init, schema-registry, rabbitmq
- Verify: place an order → `kafka-console-consumer` shows `OrderPlaced` → … → `OrderDelivered` in order for that key; all outbox rows dispatched; **stop Kafka → orders still succeed, backlog grows → start Kafka → backlog drains, no gaps**

### Step 2 — notification-service: consumer + Celery dispatch
- Alembic: `processed_events`. Add `recipient_role` and `event_id` to `notifications`, with a unique constraint on `(event_id, user_id, channel)`
- `app/consumer.py`: handlers per the map above, built from templates in `app/templates.py`
- `app/celery_app.py` + `app/tasks.py` (`send_notification`, `sweep_pending`); `app/senders.py` (in-app, fake email/SMS with `NOTIFY_FAKE_FAILURE_RATE`)
- API: add `POST /v1/notifications/{id}/read` and an unread filter on the existing list
- Compose: `notification-worker`, and give `notification-service` `depends_on` kafka and rabbitmq
- Tests: handler unit tests per event; real-DB test that a replayed event causes no duplicate rows

### Step 3 — analytics-service (new)
- Scaffold like the other services (pyproject in the workspace, Dockerfile, config, health, auth deps). No Postgres; Mongo DB `analytics`
- Consumer group `analytics-service` on all three topics plus the DLQs → `order_facts`, `rider_shift_facts`, `failed_events` (W3-D9); Prometheus business metrics (W3-D10)
- API (gateway route `/v1/analytics/*`): `GET /summary?from&to` (system_admin), `GET /restaurants/{id}` (system_admin or the owning restaurant_admin; ownership comes from the restaurant_admin_id carried in the order facts), `GET /peak-hours`
- Tests: aggregation pipelines against fixture facts; replaying an event leaves the metrics unchanged

### Step 4 — OpenTelemetry tracing + Jaeger
- `libs/observability`: `setup_tracing(service_name, app=None, engine=None)` with FastAPI, httpx, SQLAlchemy, redis, pymongo and Celery instrumentors; `OTEL_*` env (endpoint, `parentbased_traceidratio` sampler, 1.0 in dev); the SSE stream route is excluded from long spans
- `sfo_common.temporal`: add the `TracingInterceptor` in `connect_client` / `WorkerSpec`, and add `opentelemetry` to the sandbox `with_passthrough_modules` list (`temporal.py:47`), or workflows fail to import it
- Activity-written outbox rows currently get `correlation_id=None` (there's no request context inside a Temporal activity). The `trace_context` column fixes that, since the interceptor carries the trace into activities
- Outbox: `new_outbox_row` captures the current `traceparent` into `trace_context`. The relay injects it into Kafka headers, and the consumer loop extracts it, so a consumer span joins the trace
- Logging formatter: today's `trace_id` field actually holds the `X-Request-ID`. Rename it to `request_id` and put the real OTel `trace_id`/`span_id` alongside it. `smoke_test.py:174-199` matches on the value, so it keeps working; the docs that mention the field get updated
- Compose: `jaeger` (UI `127.0.0.1:16686`, OTLP 4317 internal)
- Verify: Jaeger shows **one order's trace** with spans from gateway, order, restaurant, payment, delivery, relay, notification, analytics and notification-worker

### Step 5 — Metrics, dashboards, logs
- `libs/observability/metrics.py`: RED middleware plus a `/metrics` route helper. Domain metrics: saga outcomes by terminal status/reason, activity failures, outbox backlog plus age of the oldest undispatched row (per service), relay publish ok/failed, consumer processed/retried/DLQ'd per group, Celery task success/retry/failure, notification sends by channel and status, active SSE streams, rider pool size by status
- Temporal SDK runtime metrics → Prometheus. Temporal server `--metrics-port`. RabbitMQ `:15692`
- `infra/prometheus/prometheus.yml` + `rules.yml` (alerts: outbox age > 30s, DLQ > 0, 5xx rate > 1%, saga `system_error` > 0)
- `infra/grafana/provisioning/` with datasources (Prometheus, Jaeger, Loki with the `trace_id` derived field) and dashboards: **Service overview (RED)**, **Order & delivery business**, **Event pipeline** (outbox lag, Kafka consume rate, DLQ, Celery), **Logs**
- `infra/alloy/config.alloy` (Docker discovery → Loki; read-only Docker socket mount, local dev only)
- Compose: `prometheus` (`127.0.0.1:9090`), `grafana` (`127.0.0.1:3000`, anonymous viewer in dev), `loki`, `alloy`

### Step 6 — End-to-end scenarios + failure drills
- `scripts/smoke_test.py --events`: run the lifecycle happy path and the cancellation path, then check notifications for customer, restaurant admin and rider; analytics summary deltas; that the Jaeger API (`/api/traces`) returns one trace covering ≥ 8 services; and that the Prometheus outbox backlog is back to 0
- Drills (scripted where practical, documented otherwise):
  1. Kafka down → orders keep flowing, outbox backlog grows and then drains
  2. Consumer offsets reset to earliest → zero duplicate notifications or analytics changes
  3. Poison message → lands in the DLQ, appears on the dashboard, the partition keeps moving
  4. notification-worker killed mid-send → the task is retried and the notification ends `sent` once
  5. Fake email failure rate 50% → retries visible in Grafana, terminal `failed` after 5
- Re-measure `docker stats`. Run `scripts/check.sh` (ruff, mypy, tests, `--with-db`)

### Step 7 — Docs (Part A complete)
- `technical-documentation.md`: event-driven design as built, consumer idempotency, DLQ, observability architecture, tradeoffs (JSON Schema vs Avro, embedded consumers, no collector, Mongo upsert facts vs transactions)
- `architecture.md`: services (analytics, workers), topics/events catalog, metrics/trace conventions, repo layout (`infra/`)
- `diagrams.md`: event flow (outbox → relay → Kafka → consumers → Celery), trace path, observability topology
- README: new containers, URLs (Grafana, Jaeger, Prometheus, RabbitMQ), `--events`
- PRD traceability, PROGRESS.md Week 3 checklist ticked, Week 3 ☑

---

## 2a. Plan review against the code (2026-10-15)

| # | Plan said | Code reality | Fix (folded in above) |
|---|---|---|---|
| 1 | `SKIP LOCKED` makes a 2nd relay safe | It would break per-order event ordering | Advisory lock, one relay per service (W3-D2) |
| 2 | Celery relay reads the DB | Only `asyncpg` is installed; Celery tasks are sync | Per-process loop + engine (W3-D2a) |
| 3 | Restaurant notified on `OrderPlaced` | Fires before stock/payment checks; `awaiting_restaurant_acceptance` emits nothing (`repository.py:12-20`) | New `OrderAwaitingRestaurant` event (W3-D8a) |
| 4 | Rider notified from `OrderCancelled` | `orders` has no rider column | Rider notifications come from delivery events (W3-D8b) |
| 5 | Add `occurred_at` to payloads | The envelope already has it, from outbox `created_at` | Dropped (W3-D8) |
| 6 | `RiderShiftChanged` "from shift endpoints" | One endpoint (`PATCH /riders/me/status`); the saga also flips busy/available | Emit only on offline ↔ on-shift (W3-D8c) |
| 7 | Acceptance rate = confirmed ÷ (confirmed + rejected) | Rejections have 6 reasons, and only 2 are the restaurant's | Defined by `reason` (metric definitions) |
| 8 | Logs gain `trace_id` | The field name `trace_id` is already used for the request ID | Rename to `request_id` (step 4) |
| 9 | Temporal tracing interceptor | The workflow sandbox blocks non-passthrough modules | Add `opentelemetry` to passthrough (step 4) |
| 10 | Analytics joins on restaurant | `deliveries` has no `restaurant_id` | Join by `order_id` in `order_facts` (already per-order) |

Confirmed OK: `create_app(lifespan=…)` exists for the embedded consumers; uvicorn runs one process per container (no Prometheus multiprocess mode needed); the gateway proxies only `/v1/{resource}`, so `/metrics` isn't exposed publicly; restaurant-service has an outbox table but writes no events (no relay needed there).

## 3. Settings (defaults)

| Setting | Default |
|---|---|
| `OUTBOX_RELAY_INTERVAL_SECONDS` / batch | 1 / 200 |
| `OUTBOX_RETENTION_DAYS` | 7 |
| `KAFKA_CONSUMER_MODE` | `embedded` |
| Consumer retries before DLQ | 3 (backoff 0.5s doubling) |
| Notification send retries | 5 (exponential, max 60s) |
| `NOTIFY_FAKE_FAILURE_RATE` | 0.1 |
| Pending-notification sweep | every 30s, rows older than 60s |
| `OTEL_TRACES_SAMPLER_ARG` | 1.0 (dev) |
| Topic partitions / retention | 3 / 7 days |

Note: the `.env` files are off-limits to tool edits — each step lists the lines you need to add.

## 4. Requirements traceability (Week 3)

| Guideline / PDF item | Step |
|---|---|
| Kafka-based event streaming (+ Schema Registry) | 0, 1 |
| Event-driven order lifecycle updates; PDF §4 event list | 0, 1, 2, 3 |
| Celery workers for async processing | 1, 2 |
| Notification system (order + delivery updates) | 2 |
| Analytics pipeline; PDF §5 metrics (all 9) | 3, 5 |
| Logging / Prometheus / OpenTelemetry + Jaeger | 4, 5 |
| PDF §4 "traceable, recoverable, no double-processing, spikes" | W3-D2/D4/D5, 4, 6 |
| PDF §6 diagnose failures in ordering, payment, assignment, tracking, notifications, background jobs | 5 (dashboards + alerts), 6 |
| Docs complete and explainable | 7 |

## 5. Verification (overall)

1. `docker compose up -d --build` → every container healthy; `kafka-init` exits 0
2. `uv run scripts/smoke_test.py --deep --lifecycle --events` all pass (Week 2's 82 checks still pass)
3. Open Jaeger → find the order's trace; Grafana dashboards show the order, delivery time and outbox lag at 0; clicking a log line's `trace_id` in Loki opens the trace
4. Failure drills 1–5 from step 6 behave as described
5. `scripts/check.sh --with-db` is green (ruff, mypy, pytest)

## Out of scope (kept on the backlog)
Week 2 known gaps (re-offering to a rider who declined, auto-offline for riders who miss offers, a second `RiderAssigned` on
replacement), the Week 1 "not bugs" list (RS256, network segmentation, etc.), Kafka broker JMX
metrics, Alertmanager routing, and Part B.
