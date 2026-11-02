"""ai-service's own Prometheus metrics (docs/week4-plan.md step 5), served
on the process's metrics port (sfo_observability.metrics) and shown on the
Grafana "AI / Retrieval" dashboard. The indexer's consumption, retries and
dead letters are the shared consumer metrics, group `ai-indexer`
(sfo_events.metrics)."""

from prometheus_client import Counter, Histogram

EMBED_SECONDS = Histogram(
    "ai_embed_seconds",
    "Time to embed one batch (dense + BM25)",
    ["purpose"],  # documents | query
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
EMBEDDED_TEXTS = Counter("ai_embedded_texts_total", "Texts embedded", ["purpose"])
INDEX_LAG_SECONDS = Histogram(
    "ai_index_lag_seconds",
    "From the event (its outbox row's time) to its index write, live events only",
    ["event_type"],
    buckets=(0.25, 0.5, 1, 2, 3, 5, 10, 30, 60, 300, 1800),
)
INDEX_WRITES = Counter(
    "ai_index_writes_total",
    "Documents offered to the index, by collection and what happened",
    ["collection", "result"],  # written | stale (an equal or newer version is stored)
)
ORDERS_SKIPPED = Counter(
    "ai_orders_skipped_total", "Delivered orders not indexed: their restaurant isn't indexed"
)
QUERY_SECONDS = Histogram(
    "ai_qdrant_query_seconds",
    "Qdrant hybrid query time (embedding excluded)",
    ["kind"],
    buckets=(0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1),
)
SEARCHES = Counter(
    "ai_searches_total", "Searches served, by kind and whether anything matched", ["kind", "result"]
)
