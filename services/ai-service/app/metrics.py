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
    # written (embedded) | payload_only (same text: payload replaced, vectors
    # kept) | stale (an equal or newer version is stored)
    ["collection", "result"],
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
KITCHEN_LOAD_LOOKUPS = Counter(
    "ai_kitchen_load_lookups_total",
    "Live kitchen-load reads for restaurant suggestions (failed: ranked without load)",
    ["result"],
)
LIVE_QUOTES = Counter(
    "ai_live_quotes_total",
    "Live price/availability reads for the assistant's check (failed: the index's copy was used)",
    ["result"],
)
# --- The assistant (docs/week5-plan.md step 2). ---
ASSISTANT_INTENTS = Counter(
    "ai_assistant_intents_total", "Assistant messages by intent", ["intent"]
)
ASSISTANT_OUTCOMES = Counter(
    "ai_assistant_outcomes_total",
    "How picks ended: retried (check failed once), fallback (search results shown), "
    "no_match (model found nothing fitting), nothing_found (search found nothing)",
    ["outcome"],
)
ASSISTANT_TURNS = Counter(
    "ai_assistant_turns_total", "Assistant turns by how they ended", ["status"]
)
ASSISTANT_MADE_UP_IDS = Counter(
    "ai_assistant_made_up_ids_total",
    "IDs the model picked that weren't among its candidates (caught by the check)",
)

# --- LLM calls (docs/week5-plan.md B5-D1, B5-D10), recorded for every call
# by app/llm.py's callback, whichever provider and step made it. ---

LLM_CALLS = Counter(
    "ai_llm_calls_total",
    "LLM calls by provider, role, model and outcome",
    # result: ok | rate_limited | unavailable | auth | bad_request | bad_output
    ["provider", "role", "model", "result"],
)
LLM_SECONDS = Histogram(
    "ai_llm_call_seconds",
    "Wall time of one LLM call (whole response, streamed or not)",
    ["provider", "role", "model"],
    buckets=(0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60),
)
LLM_TOKENS = Counter(
    "ai_llm_tokens_total",
    "Tokens billed by the provider",
    # input (uncached) | output | cache_read | cache_write
    ["provider", "model", "kind"],
)
LLM_COST_USD = Counter(
    "ai_llm_cost_usd_total",
    "Estimated spend from the price table in app/llm.py (0 for unpriced models)",
    ["provider", "model"],
)
