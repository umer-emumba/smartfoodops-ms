# SmartFoodOps — Progress

Read this file first when resuming work in a new session — it reflects actual build state,
not intent. See [PRD.md](./PRD.md) for requirements + traceability,
[architecture.md](./architecture.md) for the system design,
[technical-documentation.md](./technical-documentation.md) for the reasoning behind it, and
[diagrams.md](./diagrams.md) for the diagrams. How each step was built — bugs found,
reviews, verification runs — is in the git history and commit messages.

## Part A — Weekly Tracking Table

| Week | Focus Area | What to Aim For | Done |
|---|---|---|---|
| Week 1 | Foundation & Core Services | APIs + DB + architecture setup | ☑ |
| Week 2 | Order & Delivery Workflow | End-to-end order lifecycle | ☑ |
| Week 3 | Events & Observability | Async processing + monitoring | ☑ |

## Week 1 — Foundation + Core Services

- [x] `docs/architecture.md`, `docs/technical-documentation.md`, `docs/PROGRESS.md` written
- [x] Monorepo scaffolded (`pyproject.toml` workspace root, `libs/`, `services/`)
- [x] `docker-compose.yml`: per-service Postgres, Mongo, Redis, RabbitMQ, Kafka+Schema
      Registry, Temporal server+UI+its own Postgres (Traefik tried, then removed:
      technical-documentation.md#gateway-single-entry-point-not-a-spof)
- [x] `libs/common`, `libs/auth`, `libs/events`, `libs/observability` (basic setup)
- [x] Data model outline → real Alembic migrations, one per service
- [x] `user-service`: registration, login issuing JWT access+refresh tokens, roles,
      refresh rotation, block/unblock (system_admin)
- [x] `restaurant-service`: onboarding, menu CRUD with categories/availability
- [x] `order-service`: skeleton API, basic order-creation endpoint
- [x] `delivery-service`: rider registration/status skeleton
- [x] `payment-service`: read-only skeleton (`Payment` schema + get-by-order)
- [x] `notification-service`: read-only skeleton (`Notification` schema + list-mine)
- [x] `api-gateway`: resource-based routing/proxy, JWT+deny-list check, Redis rate
      limiting (stricter on auth routes), CORS, Swagger proxy/index
- [x] Every service: `GET /health` + compose healthchecks/`depends_on`
- [x] `scripts/seed.py`: demo restaurant + menu + rider + customer
- [x] Each service has a passing `tests/test_health.py`; all 7 services import cleanly
      and were individually `uv sync`'d and smoke-tested outside Docker
- [x] Deliverable check: `docker compose up` brings the system up healthy end-to-end —
      verified via `docker compose ps` (every container with a healthcheck reports
      `healthy`) and a full `scripts/seed.py` run through the gateway (bootstrap-admin →
      customer/restaurant-admin/rider registration → restaurant creation/approval → menu →
      rider approval, all succeeded).

## Week 2 — Order Lifecycle + Delivery Workflow

Plan and design decisions (D1…D13, cited from code comments):
[week2-plan.md](./week2-plan.md). Verified with `smoke_test.py --deep --lifecycle`.

**Known gaps** (technical-documentation.md#assumptions): nothing takes a rider off shift
for ignoring offers; a rider replacement keeps the order `assigned` with no second
`RiderAssigned`; SSE auth is checked at connect only; no per-user cap on concurrent
streams; one refund per order. (A rider who declined is now re-offered after 2 min —
`d580bc7`.)

### Original scope checklist

- [x] `order-service`: Temporal `OrderSagaWorkflow` (availability/capacity → stock →
      price → payment → restaurant accept/reject signal+timeout → confirm → assign rider →
      ETA; notifications are outbox events, sent in Week 3) with compensation
- [x] `POST /v1/orders/{id}/restaurant-response` (accept/reject)
- [x] Order state machine: `created → awaiting_restaurant_acceptance → confirmed →
      assigned → picked_up → delivered` + `rejected`/`cancelled`, `version` optimistic lock
- [x] `delivery-service`: proximity rider assignment (built as Redis GEO + an atomic
      conditional `UPDATE`, not `SKIP LOCKED`; approved + available only), rider
      offer/accept with timeout, ETA, delivery state machine, reassignment on failure,
      `DeliveryStatusUpdated` outbox rows
- [x] Stock reservation/release activities in `restaurant-service`; capacity check against
      `max_concurrent_orders`; promo/tax/fee pricing step
- [x] Cursor pagination on list endpoints (`sfo_common.pagination.Page`) — done early, Week 1 production pass
- [x] Customer SSE tracking (built as `tracking-service` behind a gateway streaming proxy)
- [x] `payment-service`: authorize/capture/void/refund with idempotency-key handling
- [x] Cancellation/refund flow via Temporal signal + compensation
- [x] Deliverable check: full order → delivery → completion lifecycle end-to-end, incl. a
      cancellation-after-confirmation path with correct compensation
      (`smoke_test.py --lifecycle`)
- [x] Docs updated to reflect what was actually built

## Week 3 — Event-Driven System + Observability

Plan and design decisions (W3-D1…D15, cited from code comments), event → consumer map and
metric definitions: [week3-plan.md](./week3-plan.md). As built:
[technical-documentation.md#event-driven-design-temporal-kafka-and-celery](./technical-documentation.md#event-driven-design-temporal-kafka-and-celery)
and [#observability](./technical-documentation.md#observability). Verified with
`smoke_test.py --deep --lifecycle --events` and the failure drills.

**Week 3 known limits** (documented in technical-documentation.md): email/SMS are
simulated, with no contact details; alerts aren't routed to anyone (no Alertmanager);
Jaeger keeps traces in memory; no Kafka broker metrics; consumers run inside the API
processes in dev.

### Original scope checklist

- [x] Kafka event schemas (Schema Registry), transactional outbox + Celery-beat relay per
      service
- [x] `notification-service`: Kafka consumer group + Celery dispatch task
- [x] `analytics-service`: Kafka consumer aggregating required metrics into Mongo, read API
- [x] Idempotent consumers (`processed_events` dedup; idempotent upserts in analytics)
- [x] OpenTelemetry across every service + gateway + Temporal worker + Kafka
      producers/consumers
- [x] Prometheus metrics per service (internal port, not `/metrics` on the API port);
      Grafana dashboards in `infra/grafana/`
- [x] Jaeger: one order's full trace across every service it touched
- [x] Deliverable check: fully event-driven, end-to-end traceable system; dashboards
      enabled (`smoke_test.py --events`, failure drills)
- [x] Docs fully reflect the finished Part A system, ready for mentor review

## Improvements after Week 3

Built after Part A was complete; each commit message has the details.

- [x] Asymmetric JWT signing: Ed25519 + JWKS from `user-service` instead of a shared
      HS256 secret, with key rotation (`a991fbc`)
- [x] First admin created by a `create_admin` command, not an endpoint; `seed.py`
      re-runnable (`3e96d9e`)
- [x] Live-feed order status driven from Kafka (tracking-service consumer), streams
      resynced after a Pub/Sub reconnect (`6c164b5`)
- [x] Rider live feed; delivery status and ETAs reach the live feeds through Kafka
      (`1f5ba9a`)
- [x] Rider search re-offers riders who turned it down (after 2 min) and widens the
      radius 5 → 8 → 12 km (`d580bc7`)
- [x] Saga: a confirmed order whose later step fails ends `cancelled`, not `rejected`
      (`ab73ef1`)
- [x] Restaurant marks food ready (`DeliveryFoodReady`); pickup and delivery deadlines
      in the saga, `OrdersOverdue` alert, admin `mark-delivered` (`22c887c`)
- [x] React demo UI with a portal per role; static test UI moved to `demo-ui/static`
      (`a1d5c44`)

## Open items

- [x] ~~Streaming proxy (for SSE tracking)~~ — built in Week 2
- [x] ~~RS256 instead of one shared HS256 secret~~ — built as Ed25519 + JWKS (`a991fbc`)
- [ ] Declarative gateway route-policy table (public / rate bucket / timeout per route)
- [ ] Compose network segmentation (per-service data networks); RabbitMQ/Redis credentials
- [ ] Rate limiting behind a load balancer: set uvicorn `FORWARDED_ALLOW_IPS` to the LB's
      address so the client IP (not the LB's) is the rate-limit key
- [ ] `user-service` migration declares email/phone uniqueness twice (column + index)
- [ ] Secrets manager for the signing key and service credentials (deferred)

## Documentation & Submission

- [x] `README.md`: overview, architecture diagram + order/delivery sequence diagram, core
      workflows, tech stack, local setup, API overview
- [x] `docs/technical-documentation.md` has every required section (service breakdown,
      lifecycle flows, event-driven design, failure handling, consistency approach,
      tradeoffs) — all of it matches what's built (Weeks 1–3)
- [x] `docs/PRD.md`: use cases, functional + non-functional requirements, milestones,
      traceability (update status column each week)
