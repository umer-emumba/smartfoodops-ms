# SmartFoodOps — Progress

Read this file first when resuming work in a new session — it reflects actual build state,
not intent. See [architecture.md](./architecture.md) for the system design,
[technical-documentation.md](./technical-documentation.md) for the reasoning behind it, and
[diagrams.md](./diagrams.md) for the microservices architecture + per-service ER diagrams.

## Part A — Weekly Tracking Table

| Week | Focus Area | What to Aim For | Done |
|---|---|---|---|
| Week 1 | Foundation & Core Services | APIs + DB + architecture setup | ☐ |
| Week 2 | Order & Delivery Workflow | End-to-end order lifecycle | ☐ |
| Week 3 | Events & Observability | Async processing + monitoring | ☐ |

## Week 1 — Foundation + Core Services

- [x] `docs/architecture.md`, `docs/technical-documentation.md`, `docs/PROGRESS.md` written
- [x] Monorepo scaffolded (`pyproject.toml` workspace root, `libs/`, `services/`)
- [x] `docker-compose.yml`: per-service Postgres, Mongo, Redis, RabbitMQ, Kafka+Schema
      Registry, Temporal server+UI+its own Postgres, Traefik
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
- [x] Each service has a passing `tests/test_health.py`; all 8 services import cleanly
      and were individually `uv sync`'d and smoke-tested outside Docker
- [ ] Deliverable check: `docker compose up` brings the system up healthy end-to-end
      (build in progress — verify via `docker compose ps` + the seed script, then check
      this off)

### Known gaps (found during onboarding-flow review) — now fixed

- [x] `GET /v1/restaurants/mine` — added, lists all restaurants owned by the calling
      `restaurant_admin` regardless of status
- [x] `system_admin` restaurant management — `restaurants.status` now defaults to
      `pending_approval` (was `active`); added `POST /v1/restaurants/{id}/approve`,
      `POST /v1/restaurants/{id}/suspend`, and `GET /v1/restaurants/admin?status=` (all
      `system_admin`-only). See [architecture.md#restaurant--rider-approval](./architecture.md#restaurant--rider-approval).
- [x] `system_admin` rider management — added `riders.verification_status`
      (`pending_approval`/`approved`/`suspended`, separate from the rider's own
      availability `status`), `POST /v1/riders/{id}/approve`, `.../suspend`, and
      `GET /v1/riders/admin?verification_status=`
- [x] `POST /v1/auth/bootstrap-admin` on `user-service` — the only way a `system_admin`
      account is created (shared-secret-gated, `ADMIN_BOOTSTRAP_SECRET` env var);
      `scripts/seed.py` now bootstraps a demo admin and approves the seeded
      restaurant/rider through it
- [ ] **Follow-up before next Docker verification**: `user-service`'s `env.example` gained
      `ADMIN_BOOTSTRAP_SECRET` — re-run the `cp env.example .env` loop (README's Local
      setup step 1) so the real `.env` picks it up, or `Settings()` will fail to load
      (`admin_bootstrap_secret` is a required field, no default)
- [ ] Week 2 follow-up: rider assignment logic must filter on
      `verification_status == "approved"`, not just `status == "available"` — noted in
      architecture.md, not wired yet since assignment logic itself doesn't exist until
      Week 2

### Authorization audit (2026-09-29) — three gaps found and fixed

- [x] Restaurant self-approval loophole — `status` removed from `RestaurantUpdate`
      (was reachable via the owner's own generic `PATCH /v1/restaurants/{id}`)
- [x] Payment ownership check — added `payments.customer_id`; `GET
      /v1/payments/by-order/{id}` now 403s unless caller is the order's customer or a
      `system_admin`
- [x] Pending/suspended restaurants readable by direct ID — `GET /v1/restaurants/{id}`,
      `.../menu`, `.../promotions` now 404 non-`active` restaurants for everyone except
      the owning `restaurant_admin`/`system_admin`; added `get_current_user_optional` to
      `libs/auth` to support this without breaking public browsing
- See [architecture.md#authorization-audit-2026-09-29](./architecture.md#authorization-audit-2026-09-29)
      for full detail
- [x] Gateway-side auth enforcement — `api-gateway` previously only *read* a bearer token
      (for the deny-list check), never rejected a missing/invalid one, relying entirely on
      the backend to 401 it. Added `require_valid_bearer_token` + a fail-closed
      `_PUBLIC_ROUTES` allowlist in `app/security.py`, wired into `proxy.py`'s
      `forward_request` — private routes are now rejected at the gateway before reaching a
      backend. See [architecture.md#gateway-side-auth-enforcement](./architecture.md#gateway-side-auth-enforcement).

### Restaurant management completeness (2026-09-29) — menu CRUD was only partial

Per the Week 1 plan's own line item ("menu CRUD with categories/availability"), this was
never fully closed out. Fixed:

- [x] Menu categories: was create-only; added `GET`/`PATCH`/`DELETE` (delete blocked with a
      409 while the category still has menu items, rather than a raw FK error)
- [x] Menu items: added `DELETE` (hard delete — consistent with `order_items` already
      snapshotting `item_name_snapshot` specifically to survive this)
- [x] `create_item` now validates `category_id` actually belongs to the target restaurant
      (was trusted blindly — a restaurant_admin could previously attach an item to another
      restaurant's category)
- [x] Promotions: added `PATCH`/`DELETE`; `create_promotion` now checks for a duplicate
      code within the same restaurant before insert (proper 409, not a raw DB error)
- [x] **Bug fix**: `promotions.code` was `unique=True` globally — two different
      restaurants couldn't both use `"WELCOME10"`. Changed to a composite unique constraint
      on `(restaurant_id, code)`
- [x] `PromotionCreate` now validates `valid_until > valid_from`; same check re-applied in
      `update_promotion` after a partial update

### Source-document re-check (2026-09-29) — cross-checked against the actual PDFs, not just this file

Re-read `Guidelines.pdf`/`Part A.pdf` directly rather than relying on this file's own
tracking. Confirmed everything above is accurate and found two real gaps:

- [x] **No git repository** — fixed: `git init`, initial commit capturing everything built
      so far, pushed to `origin main`
      (https://github.com/umer-emumba/smartfoodops-ms). Commit history from this point
      forward will be incremental per the Guidelines' requirement — this first commit is
      necessarily a single large snapshot since the work already existed unversioned.
- [x] **MongoDB was declared but never used** — Guidelines' Week 1 scope lists "Database
      schema design (PostgreSQL + NoSQL)," but `delivery-service` only had `motor` as a
      dependency and `MONGO_URL` configured, no actual client or collection. Fixed: added
      `app/mongo.py` (Motor client, `rider_location_pings` collection with a 24h TTL index
      + a `rider_id` index, `check_mongo_ready`), wired into `main.py`'s lifespan (index
      creation on startup) and readiness check, and `PATCH /v1/riders/me/location` now
      writes a ping document on every call, not just updating `riders.last_lat/lng` in
      Postgres. See [architecture.md#services](./architecture.md#services).

### Order-creation input validation (2026-09-29)

`POST /v1/orders` accepted negative/zero quantities, negative unit prices, and an empty
`items` list — no financial exposure yet since payment isn't wired up, but bad data could
still land in `order_db`. Fixed with Pydantic field constraints in `OrderCreate`/
`OrderItemCreate`: `quantity: Field(gt=0)`, `unit_price: Field(ge=0)`,
`items: Field(min_length=1)`. Verified all four bad-input cases are now rejected and the
valid case still passes.

## Week 2 — Order Lifecycle + Delivery Workflow

- [ ] `order-service/worker`: Temporal `OrderSagaWorkflow` (availability/capacity → stock →
      price → payment → restaurant accept/reject signal+timeout → confirm → assign rider →
      ETA → notify) with compensation
- [ ] `POST /v1/orders/{id}/restaurant-response` (accept/reject)
- [ ] Order state machine: `created → awaiting_restaurant_acceptance → confirmed →
      assigned → picked_up → delivered` + `rejected`/`cancelled`, `version` optimistic lock
- [ ] `delivery-service`: proximity rider assignment, ETA, delivery state machine,
      reassignment on failure, Redis lock, REST location-ping ingestion,
      `DeliveryStatusUpdated` publishing
- [ ] `api-gateway`: customer WebSocket/SSE tracking endpoint
- [ ] `payment-service`: authorize/capture/refund with idempotency-key handling
- [ ] Cancellation/refund flow via Temporal signal + compensation
- [ ] Deliverable check: full order → delivery → completion lifecycle end-to-end, incl. a
      cancellation-after-confirmation path with correct compensation
- [ ] Docs updated to reflect what was actually built

## Week 3 — Event-Driven System + Observability

- [ ] Kafka event schemas (Schema Registry), transactional outbox + Celery-beat relay per
      service
- [ ] `notification-service`: Kafka consumer group + Celery dispatch task
- [ ] `analytics-service`: Kafka consumer aggregating required metrics into Mongo, read API
- [ ] Idempotent consumers (`processed_events` dedup)
- [ ] OpenTelemetry across every service + gateway + Temporal worker + Kafka
      producers/consumers
- [ ] Prometheus `/metrics` per service; Grafana dashboards in `infra/grafana/`
- [ ] Jaeger: one order's full trace across every service it touched
- [ ] Deliverable check: fully event-driven, end-to-end traceable system; dashboards
      enabled
- [ ] Docs fully reflect the finished Part A system, ready for mentor review

## Documentation & Submission

- [x] `README.md`: overview, architecture diagram, core workflows, tech stack, local
      setup, API overview
- [x] `docs/technical-documentation.md` complete (service breakdown, lifecycle flows,
      event-driven design, failure handling, consistency approach, tradeoffs)
