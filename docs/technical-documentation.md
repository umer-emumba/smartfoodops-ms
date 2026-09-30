# SmartFoodOps — Technical Documentation (Part A)

This document captures the *why* behind the decisions in [architecture.md](./architecture.md):
key tradeoffs, rejected alternatives, and what's implemented vs. explicitly deferred. For
current build status, see [PROGRESS.md](./PROGRESS.md).

## Why microservices, not a modular monolith

Company requirement: true microservices, production-oriented, able to demonstrate handling
high traffic — not a monolith with internal module boundaries. Each of the 8 services below
is independently deployable with its own database, so no service's failure or scaling need
is coupled to another's.

## Gateway: single entry point, not a SPOF

A single `api-gateway` instance would mean every client request depends on one container.
Since JWT verification needs no server-side session and rate-limit counters already live in
Redis (not gateway memory), the gateway is safe to run as multiple stateless replicas.
Traefik sits in front, load-balancing across replicas and removing unhealthy ones via health
checks. Cost: one more container + a `/health` endpoint — worth it given the stated
high-traffic goal.

## Inter-service communication: REST over gRPC

**Decision**: synchronous inter-service calls (mainly the order saga's activities) use
REST/JSON over async `httpx`, not gRPC.

**Why REST was chosen**: gRPC's latency/throughput benefit (binary protobuf, HTTP/2
multiplexing) isn't observable running on local Docker Compose for a mentor demo — there's
no real production-scale QPS here to benefit from it. Meanwhile it costs real engineering
time on a 3-week solo build: `.proto` schemas + codegen tooling, `restaurant-service`/
`payment-service`/`delivery-service` each running two server protocols (REST for
client-facing reads, gRPC for internal saga calls), and — the biggest ongoing cost —
protobuf messages don't interoperate with the Pydantic models used everywhere else, so
every gRPC boundary needs hand-written conversion glue permanently, not just at setup.
Python's gRPC support itself is mature (`grpc.aio` is asyncio-native, fits the async stack
fine) — the cost is the protobuf/Pydantic mismatch, not gRPC-in-Python maturity.

**Roadmap**: if this ever needed to handle real production-scale QPS, the order saga's
synchronous activity calls (order→restaurant, order→payment, order→delivery) are the
specific place to swap REST for gRPC — documented here so the tradeoff is visible, not
revisited from scratch later.

## Why both Temporal and Kafka (not just one)

Temporal owns the order saga's *control flow* — steps that must happen in order, with
compensation if any step fails (payment authorized but restaurant rejects → refund; rider
assignment fails → retry/reassign). This needs a "did this succeed, yes/no, right now"
answer to proceed, which fits Temporal's activity model, not an async event.

Kafka owns *decoupled fan-out* — facts that other services react to without being part of
the saga's control flow (`notification-service`, `analytics-service`). If notifications
were a synchronous saga step, a notification outage would block order confirmation, which
is wrong — a customer's order shouldn't fail because SMS delivery is down.

**Reliability**: transactional outbox pattern — each producing service writes its event row
in the same DB transaction as its state change, avoiding a dual-write bug without needing a
CDC tool like Debezium. A Celery-beat periodic task per service polls the outbox and
publishes to Kafka, marking rows dispatched.

## Restaurant accept/reject: a Temporal signal-with-timeout

The Guidelines explicitly call for a restaurant order acceptance/rejection flow, not just
automatic confirmation after payment. This is a natural fit for Temporal's signal+timeout
primitive: the saga notifies the restaurant and waits on an accept/reject signal with a
bounded timeout; no response in time auto-rejects (treated the same as an explicit
rejection — same compensation path as a cancellation).

## ORM: SQLAlchemy 2.0 async, not SQLModel

SQLModel merges the DB table model and API schema into one class, but that breaks down once
a service needs to hide internal columns (`version`, `hashed_password`, audit fields) from
its API response — separate Pydantic input/output schemas end up being written anyway, on
top of SQLModel's wrapper. It also doesn't cleanly expose SQLAlchemy's `version_id_col`
optimistic-locking mapper config or the raw Core-style queries needed for rider assignment
and outbox polling (`SELECT ... FOR UPDATE SKIP LOCKED`) — both required here — so
`order-service`/`delivery-service`/`payment-service` would drop to plain SQLAlchemy anyway.
Rather than mixing ORM patterns across 8 services for one engineer to maintain, SQLAlchemy
2.0 async + Alembic is used everywhere.

## Kafka client: confluent-kafka-python, not aiokafka

aiokafka is asyncio-native, which would fit the rest of the async stack. But Kafka
producers/consumers here run as their own dedicated processes (not inside a FastAPI request
path), so there's no event-loop conflict to avoid — and confluent-kafka-python (the
official librdkafka-backed client) has first-class, well-documented Confluent Schema
Registry integration, which the tech stack explicitly calls for. Chose the stronger Schema
Registry story over the async-native fit, since the latter provides no real benefit here.

## Auth: 15-minute access tokens, rotating refresh tokens, Redis deny-list

Blocking a user (ban, fraud, etc.) needs to take effect close to instantly, but checking a
DB on every single request just to verify "not blocked" would make `user-service` a hard
synchronous dependency for every request through the gateway — a worse SPOF than the
gateway itself. Instead:

1. Access tokens are short-lived (15 min) stateless JWTs — fast to verify, no DB call.
2. Refresh tokens are **not** stateless JWTs — they're tracked server-side in `user_db`
   (hashed), so they can be explicitly revoked and rotated. Each refresh issues a new
   refresh token and revokes the old one — enables instant logout and theft detection (a
   reused, already-rotated-out refresh token signals compromise).
3. Blocking writes the user's ID to a Redis deny-list with **TTL = access-token lifetime**.
   The gateway checks this on every request. The TTL is safe specifically because it's
   bounded by the access token's own lifetime: once the Redis entry expires, the
   corresponding access token has also expired, and getting a new one requires `/v1/auth/refresh`,
   which checks `user_db.is_blocked` directly — the authoritative, non-cached check. So the
   deny-list only ever needs to cover the current token's remaining lifetime, not the block
   indefinitely — there's no window where both the cache and the DB have "forgotten" a block.

## Defense-in-depth auth

Docker Compose puts every service on the same network, reachable by hostname from any other
container — trusting the gateway alone to gate access assumes no request can ever bypass
it, which isn't guaranteed (misconfiguration, routing bugs, a compromised container). Every
service re-verifies the JWT independently via the shared `libs/auth` dependency — low
effort (one shared library call), standard zero-trust-internal-network practice.

## Real-time tracking: why ingestion and fan-out use different transports

Riders and customers have different traffic shapes. Rider location pings are
bounded-frequency (~5–15s) and many-to-one — REST POST keeps `delivery-service` fully
stateless and lets any replica handle any rider's ping, no connection-affinity bookkeeping.
Making rider ingestion a persistent connection would trade a small HTTP-overhead saving for
a much bigger cost: `delivery-service` becoming stateful purely to hold thousands of open
sockets.

Customer tracking is one-to-one and benefits from push, so it uses WebSocket/SSE — but held
by `api-gateway` (already the client-facing edge), fed by a Kafka topic `delivery-service`
publishes to. This keeps `delivery-service` stateless while still giving customers instant
updates; the only "sticky" state is a customer's live-tracking session on whichever gateway
replica accepted its handshake, which doesn't affect the gateway's statelessness story for
plain REST traffic.

## Primary keys: UUIDv7 generated in Python, not v4, not DB-side

**Decision**: every ID in the system (`sfo_common.new_id()`) is a UUIDv7 (RFC 9562,
time-ordered — the leading bits encode a millisecond timestamp), generated in the
application layer via the `uuid-utils` package, not Postgres's native `uuidv7()`
(available since PG18) and not the old `uuid.uuid4()` (fully random).

**Why v7 over v4**: random v4 keys scatter inserts across a B-tree index in random order,
causing page splits and worse cache locality as write volume grows. v7's time-ordering
keeps inserts mostly appending to the end of the index — better under the sustained write
load this system is meant to demonstrate handling.

**Why generated in Python rather than by Postgres**: the ordering benefit comes from the
UUID *value* itself (the embedded timestamp), not from where it was computed — a
Python-generated v7 ID is exactly as index-friendly as a Postgres-generated one. But not
every ID in this system lives in Postgres: Kafka event IDs (`sfo_events`) and future Mongo
document IDs (`delivery-service` rider pings, `analytics-service`) are generated outside
any Postgres connection, so they need Python-side generation regardless. Generating every
ID the same way — one shared `sfo_common.new_id()` helper — keeps one consistent, sortable
ID scheme across Postgres rows, Kafka events, and Mongo documents, rather than Postgres
rows being v7 while everything else stays v4. It also means the ID is known in memory
before any DB round-trip, which matters for the outbox pattern (the business row and its
outbox row need the same `aggregate_id` in one transaction) and for populating
relationships before commit. Staying on `postgres:16-alpine` (no need to adopt PG18) was a
consequence of this, not the goal itself.

**Note**: the request correlation ID (`sfo_common.middleware`) is deliberately still a
plain `uuid.uuid4()` — it's never a DB key, so there's nothing to gain from making it
sortable.

## Database-per-service: separate containers, not shared instance + logical DBs

Chose separate Postgres containers per service over one Postgres instance with per-service
logical databases. The latter is lighter on local resources, but a single Postgres crash
would then take down all services' data layer at once — undercutting the isolation
deliberately built everywhere else (gateway HA, no cross-service DB access). Accepted
tradeoff: heavier local resource usage (~6 Postgres containers), no added engineering cost
(same migrations either way).

## Production/high-traffic: implemented vs. documented roadmap

**Implemented**: fully async I/O stack (FastAPI + asyncpg/SQLAlchemy async + async httpx),
stateless services scalable via `docker compose up --scale <svc>=N`, Kafka partitioning for
parallel consumer scale-out, Celery worker pools scaled independently of API processes,
Redis caching + rate limiting, retry/backoff/circuit-breaker on inter-service calls,
targeted DB indexes per hot query path.

**Documented roadmap, not built** (out of scope for a Docker Compose dev/demo environment,
but service boundaries are drawn so these wouldn't require a redesign later): read
replicas, a real load balancer/service mesh beyond Traefik, Kubernetes, gRPC for the saga's
synchronous calls.

## Library & tooling choices

| Concern | Choice | Why |
|---|---|---|
| ORM | SQLAlchemy 2.0 (async) | See rationale above |
| Migrations | Alembic | Standard SQLAlchemy companion, async-capable env |
| Request/response validation | Pydantic v2 | Ships with FastAPI |
| Config/env loading | pydantic-settings | Typed per-service env config |
| Sync inter-service HTTP | httpx (async) | See REST-vs-gRPC rationale above |
| Retry/circuit breaker | tenacity | Wraps all inter-service calls |
| Password hashing | argon2-cffi (via `pwdlib`) | `passlib` is effectively unmaintained |
| JWT | PyJWT | Simpler, actively maintained vs. `python-jose` |
| Redis client | redis-py (`redis.asyncio`) | Official, async-native since v4.2 |
| MongoDB client | Motor | Official async MongoDB driver |
| Gateway rate limiting | fastapi-limiter (Redis-backed) | Needs shared state across gateway replicas |
| Kafka client | confluent-kafka-python | See rationale above |
| Testing | pytest + pytest-asyncio + httpx.AsyncClient (ASGITransport) + testcontainers | Isolated integration tests, plus docker-compose smoke tests |
| Lint/format/types | ruff + mypy | Fast, replaces flake8+isort+black in one tool |
