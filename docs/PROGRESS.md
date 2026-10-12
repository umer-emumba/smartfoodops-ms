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
| Week 2 | Order & Delivery Workflow | End-to-end order lifecycle | ☑ |
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

**Input hardening & tracing**:
- [x] `RequestModel` base for all request bodies: unknown fields → 422, whitespace
      trimmed (passwords verbatim), max lengths on every string/list, E.164 phones,
      registration `role` a closed set
- [x] Gateway rejects bodies over 1 MB (413), including chunked uploads
- [x] Pure-ASGI correlation middleware: one JSON access line per request with
      `trace_id`; uvicorn logs routed through JSON; plain access log off
- [x] Trace ID forwarded on order → restaurant calls (shared httpx hook) — verified one
      ID across gateway, order-service and restaurant-service logs
- [x] Docker log rotation (10 MB × 3) on every container

**Tests**: gateway 28, order 18, user 15, restaurant 15 (79 total, was 36); smoke test
46 checks.

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

Detailed plan (build order, endpoints, activities, data model, tests): [week2-plan.md](./week2-plan.md). The checklist below is the original scope; the plan's
steps 0–8 are the working order.

### Plan steps

- [x] **Step 0 — Temporal foundations** (2026-10-09)
  - `libs/contracts` (`sfo_contracts`): task queues, activity/workflow/signal/query names,
    activity and signal dataclasses, `BusinessErrorType` + `business_error()`
    (non-retryable), `ACTIVITY_RETRY_POLICY` (1s doubling, 5 attempts)
  - `sfo_common.temporal` (extra `sfo-common[temporal]`): `connect_client` (retries until
    reachable), `WorkerSpec`/`run_workers` (graceful shutdown, 8s), `embedded_workers`
    (lifespan, auto-restart), `run_standalone` (SIGTERM-aware); pydantic data converter so
    `Decimal`/`datetime` round-trip
  - `app/worker.py` in order (`order-saga` + `order-activities`), restaurant, payment,
    delivery; every worker also serves a `ping` activity. `TEMPORAL_WORKER_MODE`
    (`embedded` default / `standalone`), `depends_on: temporal` in compose
  - Diagnostic `WorkerPingWorkflow` (order-service) pings all four activity queues:
    `docker compose exec temporal temporal workflow execute --type WorkerPingWorkflow --task-queue order-saga`
  - Verified: `check.sh` green (order-service `tests/test_temporal_foundations.py`: ping
    across queues, retry ×3 then success, give-up after 5, business error not retried,
    Decimal round-trip); in Docker all 5 queues have pollers, ping workflow completes,
    `python -m app.worker` standalone polls and exits 0 on SIGTERM, workers survive a
    Temporal restart, smoke test `--deep` 46/46
  - Note: the first test run downloads Temporal's test-server binary (cached after)
- [x] **Step 1 — Order state machine + restaurant-admin views** (2026-10-09)
  - `app/state_machine.py` (transitions as data, `can_cancel`, entered-at timestamp columns)
  - Activities on `order-activities` (`app/activities.py`): `transition_order` (status +
    history + outbox in one transaction, `version`-guarded, idempotent, then Redis publish to
    `tracking:order:{id}` / `tracking:restaurant:{id}`), `apply_pricing` (only while
    `created`), `record_refund` (once per order, D13)
  - Endpoints: `GET /v1/orders/restaurant/{id}?status=`, `POST /v1/orders/{id}/restaurant-response`,
    `POST /v1/orders/{id}/cancel` (both 202 + signal the saga; 409 when no workflow is running);
    owning restaurant admin can read order detail
  - Quote returns `admin_user_id`, `lat`, `lng` → snapshotted on the order; `OrderCreate`
    accepts `payment_method`
  - Migration `0004`: `restaurant_admin_id`, `restaurant_lat/lng`, `payment_method`,
    `refunded_amount`/`refunded_at`, `assigned_at`/`picked_up_at`/`delivered_at`, restaurant
    queue indexes
  - Real-DB tests: compose DBs published on localhost (restaurant 15433, order 15434,
    delivery 15435, payment 15436); `db` pytest marker, off by default;
    `scripts/check.sh --with-db` runs them against `*_test` databases (migrated with Alembic)
  - Verified: `check.sh --with-db` green (order-service 119 tests); in Docker migration 0004
    applied, new endpoints exercised through the gateway, smoke test `--deep` 46/46
  - **Deferred to step 6:** `POST /v1/orders/{id}/refund` — it starts `OrderRefundWorkflow`,
    which is built there
  - **Note for step 4:** the reconciler must not start sagas for old dev/smoke-test orders
    left in `created` (bound it by age, or mark pre-saga orders)
- [x] **Step 2 — restaurant-service activities** (2026-10-09)
  - Activities on `restaurant-activities` (`app/activities.py`): `reserve_order` (capacity
    slot + stock in one transaction via conditional UPDATEs; all or nothing),
    `release_reservation`, `complete_reservation` (pickup: frees capacity, stock stays used),
    `redeem_promotion` / `release_promotion` (atomic `times_used`)
  - Idempotent via one `stock_reservations` / `promotion_redemptions` row per order; a
    release that arrives before the reserve leaves a `released` tombstone, so a late reserve
    is a no-op (it returns `status="released"` — the saga must treat that as not reserved)
  - Rows locked in a fixed order (restaurant, then items by id) — no deadlocks
  - Stock changes invalidate the public menu cache
  - Migration `0004`: `restaurants.active_order_count`, `stock_reservations`,
    `promotion_redemptions`
  - `sfo_common.testing.prepare_test_database` shared by services' `db` test fixtures
  - Contract fix: promotion `discount_type` is `percentage | fixed_amount`
  - Verified: 22 real-DB tests — 20 concurrent orders for stock 5 → exactly 5 succeed;
    capacity 3 of 10; 5 concurrent retries of one order reserve once; 20 orders with opposite
    line order don't deadlock; `max_uses=1` under two concurrent redemptions → one. Mutation
    check: removing the stock guard makes the oversell test fail (20 of 20 succeed). Docker:
    migration applied, worker up, ping workflow and smoke test `--deep` 46/46
- [x] **Step 3 — payment-service activities** (2026-10-12)
  - `app/state_machine.py`: `authorized → captured → refunded`, `authorized → voided`,
    `declined` (a partial refund also ends `refunded`; `refunded_amount` says how much)
  - Activities on `payment-activities` (`app/activities.py`): `authorize` (simulated card:
    `card_simulated` approves, `card_simulated_decline` declines → recorded + non-retryable
    `PaymentDeclined`; `PaymentSuccessful` outbox on approval), `capture`, `void`, `refund`
    (full or partial, total ≤ captured, `RefundProcessed` outbox)
  - Idempotent: one payment per order (`idempotency_key` = order_id), one `refunds` row per
    refund workflow ID; payment row locked per call. A void before any authorize leaves a
    `voided` placeholder, so a late authorize returns `status="voided"` (saga: not authorized)
  - Migration `0002`: `payments.captured_at`/`voided_at`, `refunds` table (beyond the plan's
    list — makes refunds idempotent per workflow ID)
  - Verified: 39 tests (transition table + real-DB): authorize ×2 → one row/event; 5
    concurrent authorizes → one payment (caught a real bug: duplicate failed outside the
    IntegrityError handler); decline stays declined on retry; capture after void → error;
    partial refunds; 5 concurrent 300 refunds on 1000 → exactly 3. Mutation check: without
    the row lock all 5 refunds succeed (1,500 on 1,000). Docker: migration applied, ping
    workflow, smoke test `--deep` 46/46
- [x] **Step 4 — Saga part 1** (2026-10-12)
  - `OrderSagaWorkflow` (`app/workflows/order_saga.py`): reserve → promotion → pricing →
    authorize → `awaiting_restaurant_acceptance` → wait for accept/reject/cancel/timeout (5 min)
    → `confirmed`. Every forward step registers its compensation before running (safe:
    compensations are idempotent and leave placeholders); compensations run in reverse and
    are retried until they succeed. `status_reason`: `out_of_stock`,
    `restaurant_at_capacity`, `restaurant_unavailable`, `promotion_invalid`,
    `payment_declined`, `restaurant_rejected[: reason]`, `restaurant_timeout`,
    `system_error` (retries exhausted), cancel reason or `cancelled_by_<role>`
  - Pricing (`app/pricing.py`, pure): discount (percentage / fixed, capped at subtotal), tax
    5% of (subtotal − discount), delivery fee 100 + 30/km straight-line (`sfo_common.geo`);
    config captured in the saga input (`OrderSagaInput`, `PricingConfig`)
  - `POST /v1/orders` starts `order-{id}` after commit (reuse policy: allow only after a
    failed run); Temporal down → still 201; idempotent replay of a `created` order re-starts it
  - Reconciler (`app/reconciler.py`, background task next to the workers —
    `sfo_common.temporal` `background=`): every 30s starts sagas for `created` orders aged
    60s–1h; older ones are only logged (not restarted hours late)
  - Restaurant response carries the admin as `actor` (audit trail)
  - Verified: 11 saga tests (time-skipping, faked activities on real queues: happy path,
    no promo, out of stock, decline, retries exhausted, released reservation, restaurant
    reject, timeout, cancel while awaiting, cancel mid-pricing, compensation failing twice);
    pricing unit tests; real-DB tests for order start + reconciler window. Docker, through
    the gateway: accept → `confirmed` (0.2s), decline → `rejected`/stock back, restaurant
    reject → voided/stock back, cancel → `cancelled`/voided, second cancel 409; reconciler
    picked up an order the step 3 smoke test left in `created`; two unanswered orders hit the
    real 5-min timeout → `rejected`/`restaurant_timeout`, reservation released, payment
    voided; smoke test `--deep` 46/46
  - Max delivery distance: `MAX_DELIVERY_KM` (default 15, straight line restaurant →
    drop-off); farther orders get 422 at creation, before anything is written. Found in
    Docker verification: an Islamabad drop-off from a Karachi restaurant (1,144 km) had been
    accepted with a 34,426 PKR fee
- [x] **Step 5 — delivery-service: GEO, offers, rider endpoints, ETA** (2026-10-12)
  - Delivery state machine (`app/state_machine.py`): `pending_assignment → offered →
    assigned → picked_up → delivered`, back to `pending_assignment` on decline/expiry/drop-out,
    `cancelled` before pickup
  - Redis (`app/rider_locations.py`): GEO set `riders:available` (assignment pool) +
    `rider:{id}:location` (latest position, any status, 10-min TTL). Location pings update
    Redis every time, Postgres `last_lat/lng/last_location_at` at most every 30s, and publish
    to `tracking:order:{id}` while the rider has the order; status changes add/remove the
    rider from the pool and are refused (409) while the rider holds a delivery
  - Activities on `delivery-activities`: `create_delivery`, `offer_to_nearest_rider`
    (GEOSEARCH 5 km nearest first + atomic `UPDATE riders … WHERE status='available'` claim;
    stale pool entries skipped and removed), `release_rider` (declined / expired / dropped
    → back to `pending_assignment`, `reassignment_count + 1`; dropped rider goes offline;
    completed), `transition_delivery` (assigned / picked_up / delivered), `compute_eta`,
    `cancel_delivery` (by order; placeholder if not created yet). Every change: history row
    + `DeliveryStatusUpdated` outbox + tracking publish
  - **Only the saga changes delivery status** (deviation from the plan, where the picked-up /
    delivered endpoints wrote it): rider endpoints check ownership/state, signal the saga,
    return 202 — same pattern as orders, so rider actions can't race a cancellation
  - Endpoints: `GET /v1/riders/me/offer`, `POST /v1/riders/me/deliveries/{id}/accept |
    decline | drop-out | picked-up | delivered`, `GET /v1/deliveries/by-order/{order_id}`
    (customer, holding rider, system_admin); gateway routes `deliveries`
  - ETA (`app/eta.py`): straight line × 1.3, bike 12 / scooter 25 / car 30 km/h; pickup never
    before the food's `ready_at` (step 6 passes it from the restaurant's prep time)
  - Stale-rider detector (`app/stale_riders.py`, background next to the worker): assigned
    riders silent ≥ 3 min (and assigned ≥ 3 min) → `rider_dropped` signal, once per
    (delivery, rider) via a Redis marker; never after pickup
  - Migration `0004`: `riders.last_location_at`; `deliveries.customer_id`, `ready_at`,
    `offer_expires_at`, `dropoff_eta`, `cancelled_at`; `delivery_offers`,
    `delivery_status_history`. Contracts: `TransitionDeliveryInput`, `CreateDeliveryInput`
    gains `customer_id`/`ready_at`, `InvalidDeliveryState`; shared
    `sfo_common.temporal.signal_order_saga`
  - Redis published on localhost:16379 for tests (DB 15)
  - Verified: 78 tests (state machine + ETA unit; real Postgres + Redis: nearest of three,
    radius, exclusions, stale pool entries, one rider / two concurrent deliveries → one
    offer, 5 riders / 8 deliveries → 5 distinct, release outcomes, full lifecycle + history +
    outbox, cancel, ETA; endpoints: 403 other rider, 409 expired, 409 drop-out after pickup,
    location throttling, tracking publish; detector signals once, never after pickup).
    Mutation check: without the availability guard both deliveries get the same rider.
    Docker: migration applied, ping → GEO pool, GEOSEARCH finds the rider 0.17 km from the
    restaurant, Postgres throttled, offline → out of pool, gateway routes `/v1/deliveries`;
    smoke test `--deep` 46/46
  - Note: Redis has no volume in compose — a restart (as when its port was published) drops
    the JWT deny-list, rate-limit counters and caches
- [x] **Step 6 — Saga part 2** (2026-10-12)
  - `OrderSagaWorkflow` continues after `confirmed`: create delivery (`ready_at` = now +
    restaurant prep time, from `reserve_order`) → offer to the nearest rider, one at a time;
    decline / 60s expiry → release and offer the next (each rider offered once; while nobody
    is free, look again every 30s) → at most 5 offers or 10 min, then `cancelled` /
    `no_rider_available` → accepted: delivery `assigned`, ETA, order `assigned` → pickup:
    order `picked_up`, kitchen capacity freed → delivered: payment captured, order
    `delivered`, rider freed
  - Rider drop-out (endpoint or stale-rider detector) before pickup → rider off shift,
    next rider; the order stays `assigned` (reassignment visible in delivery history and
    `DeliveryStatusUpdated` events — no second `RiderAssigned`, a deviation from the plan)
  - Cancel until pickup undoes everything (cancel delivery — releases the rider —, void,
    release promotion, release reservation). After pickup nothing is undone; cancel/drop-out
    are ignored and every remaining step retries until it succeeds. Signals that arrive
    together apply in arrival order (pickup-then-cancel → delivered; cancel-then-pickup →
    cancelled)
  - Signals carry `rider_id`: a late answer from a rider whose offer expired is ignored
  - `OrderRefundWorkflow` (`refund-{order_id}`, D13): `payment.refund` (refund ID = workflow
    ID) → `order.record_refund`; `POST /v1/orders/{id}/refund {amount?, reason}`
    (system_admin, delivered only, ≤ paid, one per order) — moved here from step 1
  - Contracts: `ReserveOrderResult.prep_time_minutes`, `OfferRiderInput.offer_timeout_seconds`,
    rider settings in `OrderSagaInput` (`RIDER_OFFER_TIMEOUT_SECONDS`, `MAX_RIDER_OFFERS`,
    `RIDER_SEARCH_TIMEOUT_SECONDS`, `RIDER_SEARCH_RETRY_SECONDS` in order-service settings)
  - Redis persistence: AOF (`everysec`) on a `redis-data` volume — the JWT deny-list and
    rate-limit counters survive restarts
  - Verified: 16 new saga tests (time-skipping, faked activities): full lifecycle + single
    capture, decline → next rider, expiry → next rider, late answer from an expired rider
    ignored, drop-out replaced, drop-out after pickup ignored, 5 failed offers → cancelled,
    nobody free for 10 min → cancelled (20 searches), rider found after a wait, cancel while
    searching / after assignment / after pickup (ignored) / before pickup arrives, capture
    retried past 5 attempts, refund + failed refund; refund endpoint real-DB tests. Saga test
    fix: each test terminates the workflows it started (left-over ones hung later tests)
  - Verified live in Docker through the gateway (two approved riders): nearest rider (0.3 km)
    offered → accept → ETAs set → pickup frees capacity → cancel after pickup 409 →
    delivered, payment captured, history `created → … → delivered`, rider back in the pool;
    decline → second rider offered (`reassignment_count` 1); drop-out → dropped rider
    offline, other rider assigned, order stays `assigned`; cancel while searching and after
    assignment → payment voided, delivery cancelled, rider freed, capacity and stock
    restored; partial refund → order and payment `refunded_amount` 5.00, one
    `RefundProcessed` row, second refund 409. No workflow-task failures or errors in the
    logs; smoke test `--deep` 46/46. (Note: a rider who declined an order is never
    re-offered it — with two riders, a decline then a drop-out leaves the saga searching
    until the 10-min limit)
- [x] **Step 7 — Live feeds (SSE)** (2026-10-12)
  - New **`tracking-service`** (container 20; stateless, Redis only, no database):
    `GET /v1/tracking/orders/{id}` (ordering customer or system_admin) — `snapshot`, then
    `status` / `delivery` / `location` events, ends after delivered/cancelled/rejected;
    `GET /v1/tracking/restaurants/{id}` (owning restaurant admin or system_admin) —
    `snapshot` of orders awaiting a decision, then `new_order` / `order_cancelled`.
    Ownership checked before streaming (normal 403/404 JSON); heartbeat comment every 15s
  - One shared Redis Pub/Sub connection per replica (`app/hub.py`): a channel is subscribed
    only while a local client watches it; a client whose queue fills (100) is disconnected
    and reconnects for a fresh snapshot. Subscribes before reading the snapshot (no gap)
  - State keys kept by the publishers: `tracking:order:{id}:state` hash — order-service
    (status, owners; at creation and every transition; 24h, 1h once finished) and
    delivery-service (delivery status, rider, ETAs, rider position; `EXPIRE NX`);
    `tracking:restaurant:{id}:pending` set; `restaurant:{id}:owner` (restaurant-service, on
    creation). Fallbacks when a key is missing: order-service
    `GET /internal/orders/{id}/tracking`, restaurant-service
    `GET /internal/restaurants/{id}/owner` — both rebuild the key
  - **api-gateway**: streaming route for `/v1/tracking/*` (relays bytes as they arrive, own
    pool of 1000 connections, read timeout 45s = 3 missed heartbeats, closes upstream on
    client disconnect); `?access_token=` accepted on tracking routes (EventSource can't send
    headers) — moved into the Authorization header and stripped from the forwarded URL
    (`app/query_token.py`); registry gains `tracking`
  - `sfo_common.testing.serve` — real local HTTP server for streaming tests
  - Local setup: `services/tracking-service/.env` (from `env.example`, with the same
    `JWT_SECRET` as the other services)
  - Verified: tracking-service 15 tests (real Redis + real server: snapshot → live updates
    → closes on delivered, heartbeat, 403/404/401 before streaming, missing state rebuilt,
    disconnect unsubscribes, two clients share one Redis subscription, restaurant feed gets
    a new order < 1s, slow client flagged); gateway 5 streaming tests (query token, errors
    pass through, events arrive incrementally — real servers both sides); state-key tests in
    order/delivery/restaurant services. Mutation check: without the unsubscribe the
    disconnect test fails. Docker, through the gateway during a real order: restaurant feed
    (query token) got `new_order` at the same moment as the customer's `awaiting` update;
    customer saw snapshot → awaiting → confirmed → assigned → picked_up → delivered, delivery
    updates, ETA and 2 rider positions, stream closed on delivered; another user 403; no
    Redis subscriptions left; owner key rebuilt via fallback; no tokens in logs; smoke test
    `--deep` 46/46
  - Not done: a token revoked mid-stream isn't re-checked (auth is at connect; access
    tokens last 15 min); no per-user cap on concurrent streams
- [x] **Step 8 — End-to-end scenarios, seed data, docs** (2026-10-12)
  - `scripts/smoke_test.py --lifecycle`: whole orders through the gateway — (1) happy path
    watched on both live feeds (restaurant feed with the token in the URL), nearest rider,
    ETAs, every status in order, stream closes itself, payment captured, stock −1 once,
    capacity released, rider available; (2) restaurant rejects; (3) card declined;
    (4) cancel after assignment; (5) nearest rider declines → rider 2 delivers
    (`reassignment_count` 1); (6) two orders race for the last unit → one proceeds, one
    `out_of_stock`, stock 0; (7) rider drops out → rider 2 delivers; (8) partial refund,
    second refund 409; (9) restaurant feed — folded into (1). Polls wait out the gateway's
    per-user rate limit; each scenario starts with both riders on shift; anything a failed
    scenario leaves open is cancelled. With `--deep` also capacity and the
    `RefundProcessed` outbox row
  - The quick checks now cancel their own order (it used to hold stock for the 5-minute
    restaurant timeout)
  - `seed.py`: a second approved rider (`rider2@demo.smartfoodops.example`, scooter)
  - Docs updated to as built: `technical-documentation.md` (lifecycle, delivery
    orchestration, failure handling and compensation, tracking rationale — SSE +
    Redis Pub/Sub instead of the gateway consuming Kafka, assumptions), `architecture.md`
    (services, new "Order saga (Temporal)" section with activities, signals, settings and
    the endpoint table; tracking; data model; conventions; repo layout), `diagrams.md`
    (component diagram; sequence diagrams for the lifecycle, assignment/reassignment,
    cancellation, refund; ER diagrams for the new tables and columns), README (diagram,
    flow, `--lifecycle`, `--with-db`, `JWT_SECRET` note for tracking-service), PRD
    traceability
  - Verified: `smoke_test.py --deep --lifecycle` 82/82 in Docker (first run 80/82 — both
    failures were script bugs: no location pings after accepting, and waiting on
    `rider_id`, which is set at offer time, instead of `assigned`); afterwards no open
    orders, both riders available, capacity back to 0

**Week 2 known gaps** (documented in technical-documentation.md#assumptions): a rider who
declined isn't re-offered that order; nothing takes a rider off shift for ignoring
offers; a rider replacement keeps the order `assigned` with no second `RiderAssigned`;
SSE auth is checked at connect only; no per-user cap on concurrent streams; one refund
per order.

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
      tradeoffs) — Week 2 sections match what's built; Week 3 sections are design
- [x] `docs/PRD.md`: use cases, functional + non-functional requirements, milestones,
      traceability (update status column each week)
