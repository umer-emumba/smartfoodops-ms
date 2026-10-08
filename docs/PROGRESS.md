# SmartFoodOps — Progress

Read this file first when resuming work in a new session — it reflects actual build state,
not intent. See [PRD.md](./PRD.md) for requirements + traceability,
[architecture.md](./architecture.md) for the system design,
[technical-documentation.md](./technical-documentation.md) for the reasoning behind it, and
[diagrams.md](./diagrams.md) for the microservices architecture + per-service ER diagrams.

## Part A — Weekly Tracking Table

| Week | Focus Area | What to Aim For | Done |
|---|---|---|---|
| Week 1 | Foundation & Core Services | APIs + DB + architecture setup | ☑ |
| Week 2 | Order & Delivery Workflow | End-to-end order lifecycle | ☐ |
| Week 3 | Events & Observability | Async processing + monitoring | ☐ |

## Week 1 — Foundation + Core Services

- [x] `docs/architecture.md`, `docs/technical-documentation.md`, `docs/PROGRESS.md` written
- [x] Monorepo scaffolded (`pyproject.toml` workspace root, `libs/`, `services/`)
- [x] `docker-compose.yml`: per-service Postgres, Mongo, Redis, RabbitMQ, Kafka+Schema
      Registry, Temporal server+UI+its own Postgres (Traefik removed — see
      Docker-verification entry below)
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
      rider approval, all succeeded). See "Docker verification" below for what it actually
      took to get here — several real bugs only surfaced once requests could reach the
      gateway for the first time.

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
- [x] Re-ran the `cp env.example .env` loop before Docker verification, as noted below —
      confirmed `ADMIN_BOOTSTRAP_SECRET` and all other newly-added vars were picked up
- [x] Unvetted riders kept out of the assignment pool at the source (2026-09-30): a rider
      can only set `status` to `available`/`busy` once `verification_status == "approved"`,
      and suspension forces `offline`. Week 2's assignment query should still filter on
      `verification_status` as defense in depth.

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

### Docker verification (2026-09-30) — first real end-to-end run, several latent bugs found

Getting `docker compose up` + `scripts/seed.py` to actually pass required fixing four
separate real bugs, none of them Traefik-related (see next section for that). Every one
of these was latent since Week 1 began, only surfacing now because this was the first time
a request ever made it all the way through the gateway to a backend service and back:

- [x] **`bitnami/kafka:3.7` no longer resolvable** — Bitnami moved older free-tier tags
      behind a paywall after this compose file was written. Replaced with
      `confluentinc/cp-kafka:7.7.0` (version-matched to `cp-schema-registry`, same vendor
      already in use), env vars converted from `KAFKA_CFG_*` (Bitnami) to plain `KAFKA_*`
      (Confluent) KRaft config, added `KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR`/
      `..._TRANSACTION_STATE_LOG_REPLICATION_FACTOR`/`..._MIN_ISR` set to `1` (Confluent's
      defaults assume a 3-broker cluster), fixed `CLUSTER_ID`, healthcheck command changed
      from `kafka-topics.sh` (Bitnami) to `kafka-topics` (Confluent, no `.sh`).
- [x] **`fastapi-limiter`'s `RateLimiter` dependency crashed every rate-limited request**
      with `AttributeError: '_IncludedRouter' object has no attribute 'path'` — it iterates
      `request.app.routes` expecting every entry to be a plain `APIRoute`, which no longer
      holds against this FastAPI version's routing internals. Replaced with an in-house
      `app/rate_limit.py` (~40 lines, one atomic Redis Lua `INCR`+`PEXPIRE`) that doesn't
      touch FastAPI route internals at all. Added `TooManyRequestsError` (429) to
      `sfo_common.errors` for a consistent error envelope. `fastapi-limiter` dependency
      removed entirely. Verified against the real running Redis: allows exactly `times`
      requests per window, blocks the rest.
- [x] **Gateway's own catch-all route 307-redirected bare-resource calls** —
      `POST /v1/restaurants` (no trailing path segment) hit Starlette's `redirect_slashes`
      because `/v1/{resource}/{rest_of_path:path}` requires a `/` after `{resource}` to
      match at all. `httpx.Client` (and most strict HTTP clients, especially for
      non-`GET` methods) doesn't auto-follow redirects by default, so this silently broke
      every "create X" call made straight through the gateway. Fixed by registering a
      second explicit route for the bare `/v1/{resource}` path pointing at the same
      handler (`app/proxy.py`), rather than relying on the client to follow a redirect.
- [x] **Seed script's demo emails used `.local`, rejected by `email-validator`** —
      `@demo.smartfoodops.local` failed Pydantic's `EmailStr` validation ("special-use or
      reserved name," since `.local` is reserved for mDNS, RFC 6762). Switched to
      `.example` — RFC 2606's reserved-for-documentation domain, which `email-validator`
      doesn't treat as special-use (Pydantic's `EmailStr` runs it with
      `check_deliverability` off, so no DNS lookup is involved).

### Traefik removed from local dev (2026-09-30)

`api-gateway` originally ran as multiple Docker Compose replicas behind Traefik
(auto-discovering replicas via Docker labels), specifically to demonstrate "no SPOF" for
the gateway. Getting Traefik working locally hit Docker Desktop's Enhanced Container
Isolation blocking exactly the Docker-socket access its auto-discovery needs — but the
deeper issue, raised by the user, is that three containers on one laptop don't actually
exercise the failure mode HA is for; a real instance dying and traffic rerouting around it
is a multi-host phenomenon that a single-machine `--scale` demo doesn't meaningfully prove.
Removed Traefik entirely; `api-gateway` now runs as a single container, exposed directly
on `:8080`. Gateway HA is now documented purely as the production (Kubernetes
`Deployment`/`HPA` + `Service`) design — see
[technical-documentation.md#gateway-single-entry-point-not-a-spof](./technical-documentation.md#gateway-single-entry-point-not-a-spof) —
not something faked locally. `docker-compose.yml`, `architecture.md`, `diagrams.md`, and
`README.md` all updated to match.

### Week 1 requirements review (2026-09-30) — code + docs re-checked against both PDFs

A full review of every service and doc against `Guidelines.pdf`/`Part A.pdf` found four
Week-1 gaps in the code plus several doc inaccuracies. All four code gaps are fixed and
verified end-to-end through the gateway on the running Compose stack:

- [x] **Order creation trusted client prices** — `OrderItemCreate` no longer accepts
      `unit_price`/`item_name`. `order-service` now fetches the restaurant and menu from
      `restaurant-service` (`app/restaurant_client.py`) and rejects: non-active restaurant
      (404), `is_accepting_orders = false` (409), items not on that restaurant's menu (422),
      unavailable or out-of-stock items (409, stock summed across split lines).
      Upstream unreachable → 503 (new `ServiceUnavailableError` in `sfo_common`). Totals
      are computed in `Decimal`. `quantity` capped at 50; lat/lng range-checked.
      Verified: a request with `unit_price: 0` was priced from the menu instead.
- [x] **No idempotency on `POST /v1/orders`** — `Idempotency-Key` header now required;
      `orders.idempotency_key` + `request_hash`, unique per customer (migration `0002`).
      Same key + same body → original order (200); different body → 409; concurrent
      duplicate → caught by the unique constraint and replayed.
- [x] **No audit trail** — added `order_status_history`, `restaurant_status_history`,
      `rider_verification_history` (migrations `0002` in order/restaurant/delivery
      services), each written in the same transaction as the status change; approve/
      suspend accept an optional `{"reason"}`. Read via `status_history` on
      `OrderResponse`, `GET /v1/restaurants/{id}/status-history`,
      `GET /v1/riders/{id}/verification-history`. `order-service` also now writes an
      `OrderPlaced` outbox row with every order (first real use of the outbox).
- [x] **Bootstrap-admin could mint unlimited admins** — now one-shot (409 once any
      `system_admin` exists), secret compared as bytes, strict auth rate limit at the
      gateway, and `user-service` refuses to start with the dummy secret outside
      `ENVIRONMENT=local`.
- [x] Small fixes alongside: `limit` query params now `ge=1` everywhere (negative → 422,
      was 500); rider `status`/`vehicle_type` validated as literals.
- [x] `order-service/tests/test_order_validation.py` — 7 unit tests for the new
      validation (stubbed `restaurant-service`).
- [x] Docs: added [PRD.md](./PRD.md); added service breakdown, order lifecycle state
      machine, delivery orchestration, Temporal/Kafka/Celery design, failure handling,
      data consistency, and assumptions sections to
      [technical-documentation.md](./technical-documentation.md); marked every
      not-yet-built item **(Week 2)**/**(Week 3)** across README/architecture/tech doc;
      order + delivery sequence diagram in README; fixed diagram edges and ER diagrams;
      fixed broken doc anchors referenced from code comments.

**Local upgrade note**: `services/order-service/.env` doesn't need changing:
`RESTAURANT_SERVICE_URL` has a working default and was added to `env.example`.

### Bug-fix pass (2026-09-30) — review backlog cleared

Every item below was fixed, covered by a test, and verified end-to-end through the gateway
on the rebuilt Compose stack (`docker compose up -d --build`).

**api-gateway**
- [x] Blocked user got a plain-text 500 (deny-list `ForbiddenError` raised inside
      `BaseHTTPMiddleware`) → the middleware is gone; `security.authenticate` decodes the
      token once and checks the deny-list inside the route → proper **403** envelope with
      CORS headers. Deny-list unreachable → **503** (fails closed).
- [x] Backend down/slow → plain-text 500 → **502** `bad_gateway` / **504**
      `gateway_timeout`. Pooled client with a 2s connect / 10s read timeout, closed on
      shutdown.
- [x] `/docs` showed FastAPI's own Swagger page, shadowing the cross-service index →
      `create_app(docs_url=None)` for the gateway.
- [x] Hop-by-hop headers (RFC 9110 §7.6.1) and `Connection`-listed headers stripped in
      both directions; stale `content-encoding` dropped from responses.
- [x] `..` path segments rejected (404) — httpx would otherwise collapse them and reach
      backend paths outside `/v1/{resource}/`.
- [x] Malformed client `X-Request-ID` (log injection) replaced with a generated one.
- [x] `/docs/{service}/openapi.json` → 502 instead of a crash when a backend is down.

**Shared libs**
- [x] Shared 422 handler crashed into a **500** for *every* custom-validator error
      (Pydantic puts the `ValueError` object in `ctx`, which isn't JSON-serializable) —
      found during this pass; this also affected the existing promotion date check.
- [x] Catch-all `Exception` handler: any unhandled bug returns the JSON envelope.
- [x] JWTs: `sub`/`role`/`exp`/`iat` required; a correctly signed token with an unknown
      role is now a 401, not a 500.
- [x] Malformed pagination cursor → 422, not 500. Bootstrap secret added to log redaction.

**user-service**
- [x] Duplicate phone number / concurrent registration → **409** (pre-check + unique
      constraint catch), not a 500.
- [x] Refresh-token reuse detection (reusing a rotated token revokes all of the user's
      refresh tokens) and an atomic compare-and-set on rotation, so concurrent refreshes
      can't both win.
- [x] Logout requires the refresh token's secret, not just its lookup ID.
- [x] Block writes the Redis deny-list *before* committing, so a Redis failure can't leave
      a block that live tokens ignore.
- [x] Emails lowercased; password 8–128 chars at registration; constant-work login for
      unknown emails (no timing leak); address lat/lng range-checked.

**restaurant-service**
- [x] Explicit `null` in PATCH on a NOT NULL field → 422 (was 500); `null` still allowed
      where it means something (`stock_count` = unlimited, `max_uses`, `cuisine_type`).
- [x] Promotion datetimes must be timezone-aware (naive vs aware comparison was a 500);
      rules re-checked after a partial PATCH; percentage discount capped at 100;
      `discount_type` validated.
- [x] Money inputs are `Decimal` with `Numeric(10,2)` bounds; `ge=0`/`ge=1` on price,
      stock, capacity, prep time, max uses; lat/lng ranges.
- [x] Duplicate promo code race → 409. Public promotion list shows only currently valid
      codes; owner/admin see all. Model's promotions unique index now matches migration
      `0001` (no autogenerate drift).

**delivery-service**
- [x] Concurrent rider registration → 409. Mongo ping write is best-effort (logged), with a
      2s server-selection timeout — a Mongo outage no longer fails an already-committed
      location update, nor stalls it 30s.

**Infra**
- [x] Published ports (gateway `8080`, Temporal UI `8088`, RabbitMQ admin `15672`) bound to
      `127.0.0.1` — previously reachable from the whole LAN, and Temporal UI has no auth.
- [x] Dockerfiles: `uv.lock` copied + `uv sync --frozen` (reproducible), no `uv` at runtime
      (entrypoints call `.venv/bin` directly — container start never re-resolves deps),
      non-root `app` user (uid 10001), root `.dockerignore` keeps `.env` files out of images
      (verified inside a running container).

**Tests**: gateway 14 (proxy failure modes, blocked user, header stripping, `..`, `/docs`,
unknown-role token), restaurant 8 (schema rules + 422-handler regression), user 3, order 8.

**Behaviour changes to be aware of**: accounts created before this with mixed-case emails
can't log in until their stored email is lowercased (the seed data is already lowercase).
Reusing an old refresh token now logs that user out everywhere.

### Authorization review (2026-09-30)

Live-tested 24 role/ownership boundaries through the gateway (cross-owner edits of
restaurants/categories/items/promotions, cross-customer order access, self-approval,
role escalation at registration, admin-only routes) — all behaved correctly. Fixed:

- [x] `GET /v1/restaurants/{id}/menu/categories` was public in restaurant-service but
      missing from the gateway's public-route list, so anonymous browsing got a 401
- [x] Admins had no way to find users to block — added `GET /v1/users` (filter by `role`,
      `is_blocked`, `q` search over email/name/phone) and `GET /v1/users/{id}`, both
      `system_admin`-only
- [x] Block guards: an admin can't block their own account (bootstrap is one-shot, so this
      could lock the platform out) or another `system_admin`
- [x] Admins could only open orders by ID — added `GET /v1/orders/admin` (filter by
      `status`, `restaurant_id`, `customer_id`)
- [ ] Week 2: restaurant admins need to list their restaurant's orders (for accept/reject)

### Production-readiness pass (2026-10-08)

Week 1 scope only; Week 2/3 design items are tracked separately.

- [x] Gateway: separate connection pool per backend service (bulkhead), 0.5s pool wait →
      503, per-service read timeouts (10s order-service, 5s others), 1s connect
- [x] Gateway rate limiting keyed by user ID when the token is valid, IP otherwise; fails
      open on Redis errors (was an unhandled 500); TTL returned by the Lua script
- [x] argon2 runs in a worker thread (max 4 concurrent per process) — measured ~36ms per
      verify locally, which previously blocked the whole event loop
- [x] Refresh-token secrets hashed with SHA-256 instead of argon2
- [x] Order creation: one `POST /internal/restaurants/{id}/quote` call (was two GETs,
      including the full menu), made after releasing the DB connection
- [x] Explicit DB pool + Postgres `statement_timeout` / `idle_in_transaction_session_timeout`;
      pool exhaustion → 503 instead of 500
- [x] Alembic migrations take a Postgres advisory lock — replicas can start together
- [x] Keyset pagination (`Page` envelope, `?cursor=`) on users/admin, orders (mine, admin),
      restaurants (public, admin), riders/admin, notifications; new composite indexes
      (+ partial index on undispatched outbox rows in order_db)
- [x] Admin user search escapes `%`/`_`/`\`, and `q` is length-checked after stripping
- [x] Order statuses defined once (`OrderStatus` StrEnum)
- [x] `/livez` (no dependency checks) and `/readyz` on every service; `/health` = `/readyz`
- [x] Gateway: rate-limit counter + blocked-user lookup in one pipelined Redis round-trip
      (block-list key defined once in `sfo_auth.denylist.blocked_key`)
- [x] Expired refresh tokens purged hourly (batched, one replica per run via try-lock);
      `refresh_tokens.expires_at` index
- [x] restaurant-service: Redis cache for an active restaurant's detail/menu/categories
      (60s TTL, invalidated on every write, fail-open), `Cache-Control` on public reads

**Tooling & deprecations**:
- [x] Money typed `Decimal` end to end (`sfo_common.money.MoneyAmount` keeps JSON numbers)
- [x] ruff clean (was 215 findings) and `ruff format`-ed; mypy clean per service with
      `disallow_untyped_defs`; `.pre-commit-config.yaml` + `scripts/check.sh`
- [x] delivery-service: Motor (deprecated) → PyMongo's `AsyncMongoClient`; Mongo client
      now closed on shutdown
- [x] Tests: no deprecated Starlette `TestClient`; pytest-asyncio `asyncio_mode = "auto"`
- [x] Dev JWT secret in `env.example` lengthened to ≥ 32 bytes (update local `.env` files)
- [x] Infra images: deprecated `temporalio/auto-setup:1.24` + its Postgres + `temporalio/ui`
      → one `temporalio/temporal:1.9.1` CLI dev server (SQLite on a volume, UI still on
      `:8088`); `cp-kafka`/`cp-schema-registry` 7.7.0 → 8.3.2 (7.7 standard support ended
      2026-07-26); `rabbitmq:3` → `4.3.6` (3.13 is commercial-support only); Schema Registry
      got a healthcheck. All tags pinned to a patch version.

**Tests**: gateway 23, order 15, user 7, restaurant 14 (62 total, was 36).

**Breaking changes**: list endpoints now return `{"items", "next_cursor"}` instead of a bare
array. Refresh tokens issued before this change no longer verify (they were argon2-hashed),
so existing sessions must log in again.

### Not bugs — deliberately left for a separate design step

- [ ] Streaming proxy (needed for Week 2 WebSocket/SSE tracking), pure-ASGI middleware
- [ ] Declarative gateway route-policy table (public / rate bucket / timeout per route)
- [ ] RS256 (user-service signs; others verify with the public key) instead of one shared
      HS256 secret
- [ ] Compose network segmentation (per-service data networks); RabbitMQ/Redis credentials
- [ ] Rate limiting behind a load balancer: set uvicorn `FORWARDED_ALLOW_IPS` to the LB's
      address so the client IP (not the LB's) is the rate-limit key
- [ ] `user-service` migration declares email/phone uniqueness twice (column + index)

## Week 2 — Order Lifecycle + Delivery Workflow

- [ ] `order-service/worker`: Temporal `OrderSagaWorkflow` (availability/capacity → stock →
      price → payment → restaurant accept/reject signal+timeout → confirm → assign rider →
      ETA → notify) with compensation
- [ ] `POST /v1/orders/{id}/restaurant-response` (accept/reject)
- [ ] Order state machine: `created → awaiting_restaurant_acceptance → confirmed →
      assigned → picked_up → delivered` + `rejected`/`cancelled`, `version` optimistic lock
- [ ] `delivery-service`: proximity rider assignment (`SELECT … FOR UPDATE SKIP LOCKED`,
      approved + available only), rider offer/accept with timeout, ETA, delivery state
      machine, reassignment on failure, `DeliveryStatusUpdated` publishing (location-ping
      ingestion already built in Week 1)
- [ ] Stock reservation/release activities in `restaurant-service`; capacity check against
      `max_concurrent_orders`; promo/tax/fee pricing step
- [ ] Cursor pagination on list endpoints (`sfo_common.pagination.Page`)
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

- [x] `README.md`: overview, architecture diagram + order/delivery sequence diagram, core
      workflows, tech stack, local setup, API overview
- [x] `docs/technical-documentation.md` has every required section (service breakdown,
      lifecycle flows, event-driven design, failure handling, consistency approach,
      tradeoffs) — Week 2/3 sections are design, to be updated to match what's built
- [x] `docs/PRD.md`: use cases, functional + non-functional requirements, milestones,
      traceability (update status column each week)
