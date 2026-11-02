# Week 4 Plan — Data Preparation + Retrieval (Part B)

## Context

Part A (Weeks 1–3) is done and merged to `main`. Part B adds the GenAI layer that
`SmartFoodOps - Part B.pdf` asks for: semantic search + RAG over menus, restaurants, reviews and
order history (Week 4), and a streaming AI assistant with recommendations, order explanations and
restaurant content generation (Week 5). The work is split into two weeks so each one can be
reviewed by the mentor on its own (Guidelines §2).

Decided with the user: **Qdrant** as the vector DB, **Anthropic Claude** as the LLM (Sonnet 5.5
for answers, Haiku 5.5 for cheap steps — used from Week 5), **local fastembed** for embeddings
(bge-small, 384-dim, no key needed), and a **real review feature** in restaurant-service.

Working rule: build step by step, verify each step in Docker, and ask before committing. All
steps go on `week4/retrieval-layer`, cut from `main`. `week5/ai-assistant` is cut from it later.

### What exists today, and what's missing

- Have: restaurant `city/lat/lng/cuisine_type/avg_prep_time_minutes/active_order_count`, menu item
  `description/price/is_available/stock_count/version`, the outbox → relay → Kafka path with Schema
  Registry (`libs/events`), the consumer framework with DLQ and idempotency (`sfo_events/consumer.py`,
  `idempotency.py`), SSE through the gateway (`api-gateway/app/proxy.py::stream_tracking`,
  `tracking-service/app/sse.py`), the order tracking internal API
  (`order-service/app/api/internal.py`), delivery by-order with ETAs and deadline flags, reason
  codes (`sfo_events/reasons.py`), the Mongo-backed analytics-service, and the OTel/Prometheus/Loki/Grafana stack.
- Missing: restaurant-service has an `OutboxEvent` table but **publishes nothing**, so there's no
  `restaurant-events` topic and nothing to trigger re-indexing. There are **no reviews or ratings**,
  no dietary or spice attributes, and the seed creates only **1 restaurant** (too little data for
  semantic search to be meaningful).

## 1. Design decisions

| # | Decision | Why |
|---|---|---|
| B4-D1 | **New `ai-service`** (FastAPI, same layout as the other services: `config.py`, `db.py`, `auth_deps.py`, `api/`, `consumer.py`, `celery_app.py`) with its own **Postgres `ai-db`** (conversations, content drafts, AI interaction log, outbox) | Keeps database-per-service |
| B4-D2 | **Qdrant** container. Collections: `menu_items`, `restaurants`, `reviews`, `order_history`. Payload fields: `restaurant_id, city, geo (lat/lon), cuisine, price, is_available, dietary_tags, spice_level, source_version` | Payload filters (geo radius, availability, price, dietary) run inside the vector query |
| B4-D3 | **Hybrid search**: dense bge-small vectors (fastembed, local ONNX) plus a sparse BM25 vector, fused with RRF through Qdrant's query API. Embedding runs in a thread pool | No API key or per-call cost. BM25 catches exact dish names ("biryani") that dense vectors blur |
| B4-D4 | **Indexing is event-driven**: restaurant-service outbox → `restaurant-events` → ai-service indexer consumer → chunk → embed → upsert. Order history is indexed from `order-events` (`OrderDelivered`) | Reuses the Week 3 outbox/relay/consumer path; the index follows the source of truth without polling |
| B4-D5 | **Idempotent indexer**: point IDs are deterministic (uuid5 of entity id); an upsert is skipped if the stored `source_version` is ≥ the incoming one; deletes are tombstones; Qdrant outages are retried and rewound, never sent to the DLQ (same rule as W3-D5) | Replays and out-of-order events can't regress the index. An infrastructure outage isn't a poison message |
| B4-D6 | **Collection name includes the embedding model version**; `python -m app.reindex` backfills from a restaurant-service internal export endpoint and order-service history | Swapping models is a clean cut-over, and the first deploy has a full index |
| B4-D7 | **Search uses `qdrant-client` + `fastembed` directly**, not LangChain's vector-store wrapper | Full control of filters and repeat-safe indexing |
| B4-D8 | **The gateway routes `/v1/ai/*` to ai-service** (added to `_UPSTREAM_POLICIES`). `ANTHROPIC_API_KEY` goes in `env.example` only; the user adds it to `.env` | Same entry point and auth as every other service; no secret in the repo |
| B4-D9 | **Rating aggregate stored as `rating_sum` + `review_count`**, bumped by one relative `UPDATE` in the review's transaction; `avg_rating` is derived in the response | Two reviews committing at once both count. Storing the average itself would need a read-modify-write that loses one of them |
| B4-D10 | **Seed history goes in through dev-only import commands** (`order-service app.import_history`, `restaurant-service app.import_reviews`, run by `seed.py` via `docker compose exec`, like `create_admin`). Orders are written delivered with normal pricing and a backdated status trail; fixed uuid5 IDs make re-runs skip them | Search needs ~200 reviews on delivered orders over past months. The real saga takes minutes per order and stamps everything "now". These orders have no payments, deliveries or events, so analytics doesn't count them; the step-3 backfill indexes them |

New resource use is roughly Qdrant ~200 MB plus ai-service with the ONNX model ~350 MB, on top
of today's ~4 GiB stack.

## 2. Build order

### Step 0 — Foundations
- Add `qdrant` and `ai-db` services to `docker-compose.yml`, plus an `ai-service` skeleton
  (health/readiness checks covering Qdrant + DB, OTel through `libs/observability`, JWT through `libs/auth`).
- Gateway: add `ai-service` to `_UPSTREAM_POLICIES` and the routes.
- Write `docs/week4-plan.md`. `ANTHROPIC_API_KEY` goes in `env.example` only; you add it to
  `.env` yourself.

### Step 1 — Richer domain data (restaurant-service)
- Migration: add `menu_items.dietary_tags` (JSON list: vegetarian, vegan, halal, gluten_free…),
  `spice_level` (0–3) and `calories` (nullable), plus a `restaurants.description` column.
  Expose them on the create/update schemas.
- **Reviews:** a `reviews` table with `(order_id PK, customer_id, restaurant_id, rating 1–5,
  text, item_ids, created_at)`.
  - `POST /v1/restaurants/{id}/reviews` is customer-only. It's allowed only for an order that is
    delivered and belongs to that customer, checked through order-service's internal API, with one
    review per order.
  - `GET /v1/restaurants/{id}/reviews` is paginated.
  - Store rating aggregates (`avg_rating`, `review_count`) on the restaurant.
- `scripts/seed.py`: add a fixture file (`scripts/fixtures/catalog.json`) with ~10 restaurants
  across 2 cities, ~120 items with realistic descriptions, tags and prices, ~200 reviews, and a
  past-order history for the demo customer. Keep the seed re-runnable.

### Step 2 — `restaurant-events` topic
- Add `Topic.RESTAURANT` and these events: `RestaurantUpserted`, `RestaurantStatusChanged`,
  `MenuItemUpserted`, `MenuItemDeleted`, `ReviewSubmitted`. Payloads go in `sfo_events/payloads.py`,
  JSON schemas are generated from Pydantic, keyed by restaurant_id.
- Write to the outbox in the same transaction as each repository write in
  `restaurant-service/app/repository.py`, including stock/availability flips (`is_available`).
- Restaurant-service needs a **relay worker** (Celery worker+beat, the same as the order, payment
  and delivery workers) and a compose entry.

### Step 3 — Chunking + embedding pipeline (ai-service)
- `app/documents.py` builds one text document per entity, prefixed with its context:
  - menu item: restaurant name, cuisine, category, name, description, tags, spice level, price
  - restaurant profile: description plus top cuisines
  - review: split into ~300-token pieces with an overlap if long
  - customer order history: a rolling summary per customer of recent items, cuisines and spend
- `app/embedding.py` wraps fastembed (dense + BM25 sparse) and runs it in a thread pool.
- `app/indexer.py` is a Kafka consumer on `restaurant-events` + `order-events` that reuses the
  `sfo_events` consumer. It's idempotent:
  - point IDs are deterministic (uuid5 of entity id)
  - an upsert is skipped if the stored `source_version` is ≥ the incoming one
  - deletes are tombstones
  - Qdrant outages are retried and rewound, never sent to the DLQ (same rule as W3-D5)
- Backfill: `python -m app.reindex` pages through a new restaurant-service internal export
  endpoint (`/internal/catalog/export`) and order-service history. It's used after first
  deploy and when the embedding model changes (the collection name includes the model version
  so swapping is a clean cut-over).

### Step 4 — Retrieval API
- `GET /v1/ai/search?q=&kind=item|restaurant|review|cuisine&lat=&lng=&radius_km=&max_price=&dietary=&restaurant_id=&limit=`
  runs a hybrid query with RRF fusion and payload filters (geo radius, `is_available=true`,
  restaurant status active).
- `GET /v1/ai/search/history` searches the calling customer's own order history, always filtered
  by `customer_id`.
- `app/retrieval.py` holds the reusable `retrieve(query, filters) -> list[Hit]` that Week 5's graph calls.

### Step 5 — Verification, metrics, docs
- Prometheus: embed latency, index lag (event time → indexed), Qdrant query latency, and
  indexer DLQ/retry counters. Add an "AI / Retrieval" Grafana dashboard.
- **Retrieval eval:** `scripts/eval_retrieval.py` runs a golden set of ~30 queries ("something
  spicy under 800", "light vegetarian lunch", "biryani near F-7") against expected IDs and reports
  recall@5 and MRR. Run it in `check.sh --with-ai`.
- Add `smoke_test.py --ai`: change a menu item → it shows up in search within N seconds.
  Mark it unavailable → it drops out.
- Drills: Qdrant down (the indexer catches up with 0 DLQ), and a full reindex while serving.
- Docs: architecture (AI layer), technical-documentation (chunking/embedding/consistency
  trade-offs), PRD Part B sections, PROGRESS Week 4 table.

## 3. Week 4 deliverables (Part B PDF)

| Deliverable | Step |
|---|---|
| Embedding pipeline | 2–3 |
| Vector DB populated | 3 (backfill) |
| Semantic search | 4 |
| Retrieval API | 4 |
